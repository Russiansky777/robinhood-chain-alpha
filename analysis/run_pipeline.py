#!/usr/bin/env python3
"""Точка входа: гоняет весь пайплайн Sprint 1 (гейты 0-5) и пишет
docs/RESULTS.md.

Требует DUNE_API_KEY в .env / env. Если его нет — падает сразу с понятным
сообщением, ничего не выполняя (см. docs/DATA_ACCESS.md).

Все `{{param}}` в sql/*.sql рендерятся здесь, в Python, литеральными
значениями ДО отправки в Dune (см. dune_client.render_sql) — Dune видит
уже готовый SQL без плейсхолдеров, поэтому ничего не требуется отдельно
объявлять на стороне Dune API.

Использование:
    python analysis/run_pipeline.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from config import CONFIG
from cohort_builder import build_cohorts, merge_august_pnl
from stats_test import run_full_test
from dune_client import DuneClient, DuneCreditsExhausted, DuneRateLimited, render_sql

ROOT = Path(__file__).parent.parent
SQL_DIR = ROOT / CONFIG.sql_dir


def read_sql(name: str) -> str:
    return (SQL_DIR / f"{name}.sql").read_text()


def substitute_query_refs(sql: str, query_ids: dict[str, int]) -> str:
    """Заменяет `query_XX_name` (человекочитаемые ссылки в наших .sql
    файлах на другие сохранённые запросы) на реальный синтаксис Dune для
    ссылки на сохранённый запрос: `query_<numeric_id>`. Это НЕ то же
    самое, что {{param}}-плейсхолдеры (см. render_sql в dune_client.py) —
    ссылки между запросами являются законной частью Dune SQL и не требуют
    отдельного объявления.
    """
    for name, qid in query_ids.items():
        sql = sql.replace(f"query_{name}", f"query_{qid}")
    return sql


def q_ts(date_str: str) -> str:
    """'2026-07-01' -> "'2026-07-01 00:00:00'" -- готовый timestamp-литерал."""
    return f"'{date_str} 00:00:00'"


def q_list(items: list[str]) -> str:
    """['WETH','USDC'] -> "'WETH','USDC'" -- готовый список для IN(...) / array[...]."""
    return ",".join("'" + str(x).replace("'", "''") + "'" for x in items)


def fmt_usd(x: float) -> str:
    return f"${x:,.0f}"


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def build_top20_table(cohort_a_with_august: pd.DataFrame) -> str:
    top20 = cohort_a_with_august.sort_values("realized_pnl_usd_august", ascending=False).head(20)
    lines = ["| # | Address | July PnL | August PnL | July trades | July tokens |", "|---|---|---|---|---|---|"]
    for i, row in enumerate(top20.itertuples(), start=1):
        lines.append(
            f"| {i} | `{row.wallet_address}` | {fmt_usd(row.realized_pnl_usd)} | "
            f"{fmt_usd(row.realized_pnl_usd_august)} | {row.trade_count} | {row.unique_tokens_traded} |"
        )
    return "\n".join(lines)


def render_report(context: dict[str, str]) -> str:
    template = (ROOT / CONFIG.report_template).read_text()
    for key, value in context.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def main() -> int:
    if not CONFIG.dune_api_key:
        print(
            "ОШИБКА: DUNE_API_KEY не задан. Пайплайн не выполнялся, "
            "docs/RESULTS.md не менялся.\n"
            "  1) cp .env.example .env && впишите ключ, либо\n"
            "  2) задайте секрет DUNE_API_KEY в GitHub Actions и запустите "
            "workflow run_pipeline.yml.\n"
            "Подробности: docs/DATA_ACCESS.md",
            file=sys.stderr,
        )
        return 1

    client = DuneClient()
    query_ids: dict[str, int] = {}
    base_tokens_sql = q_list(list(CONFIG.base_token_symbols))

    def run_named(step_name: str, sql: str) -> pd.DataFrame:
        """create_query + run_sql_cached, попутно запоминает query_id для
        последующих cross-query ссылок. Ошибки 402/429 всплывают наружу
        и ловятся в main() отдельно, чтобы явно доложить прогресс."""
        qid = client.create_query(step_name, sql)
        query_ids[step_name] = qid
        return client.run_sql_cached(step_name, sql, query_id=qid)

    # --- Гейт 0/1: сырые данные июля ---
    print("== Шаг 1: pool creation blocks ==")
    pool_sql = render_sql(read_sql("01_pool_creation_blocks"), {"sniper_block_window": CONFIG.sniper_block_window})
    df_pools = run_named("01_pool_creation_blocks", pool_sql)
    print(f"  {len(df_pools)} пулов создано в периоде покрытия запроса.")

    print("== Шаг 2: сырые свопы, июль ==")
    swaps_sql = render_sql(
        read_sql("02_swaps_raw_july"),
        {"start_date": q_ts(CONFIG.train_start), "end_date": q_ts(CONFIG.train_end)},
    )
    df_swaps_july = run_named("02_swaps_raw_july", swaps_sql)
    print(f"  {len(df_swaps_july)} свопов в июле.")

    print("== Шаг 3: агрегация по кошельку, июль ==")
    agg_sql = render_sql(
        substitute_query_refs(read_sql("03_wallet_agg_july"), query_ids),
        {"base_token_symbols": base_tokens_sql},
    )
    df_agg_july = run_named("03_wallet_agg_july", agg_sql)
    print(f"  {len(df_agg_july)} уникальных кошельков-трейдеров в июле.")

    print("== Шаг 4: исключение снайперов/инсайдеров ==")
    excl_sql = substitute_query_refs(read_sql("04_sniper_insider_exclusions"), query_ids)
    df_excluded = run_named("04_sniper_insider_exclusions", excl_sql)
    print(f"  {len(df_excluded)} адресов помечено как снайперы/инсайдеры.")

    def run_gate5(min_trades: int) -> pd.DataFrame:
        sql = render_sql(
            substitute_query_refs(read_sql("05_final_cohort_pool_july"), query_ids),
            {"min_trades": min_trades, "min_unique_tokens": CONFIG.min_unique_tokens},
        )
        return run_named(f"05_final_cohort_pool_july_mt{min_trades}", sql)

    print(f"== Шаг 5: гейт шума (MIN_TRADES={CONFIG.min_trades}) ==")
    df_gated = run_gate5(CONFIG.min_trades)
    print(f"  {len(df_gated)} кошельков прошли гейты 1-2.")

    # --- Гейт 3: когорты ---
    print("== Шаг 6: сборка когорт А/Б ==")
    cohort_a, cohort_b = build_cohorts(df_gated)
    print(f"  Когорта А: {len(cohort_a)}, Когорта Б: {len(cohort_b)}")

    full_pool_spearman = os.environ.get("FULL_POOL_SPEARMAN", "true").lower() == "true"
    wallets_for_august = (
        df_gated["wallet_address"].tolist()
        if full_pool_spearman
        else pd.concat([cohort_a, cohort_b])["wallet_address"].tolist()
    )
    if not full_pool_spearman:
        print(
            "  [ЭКОНОМИЯ КРЕДИТОВ] FULL_POOL_SPEARMAN=false — Spearman "
            "считается только по когортам А+Б, не по всему гейтованному "
            "пулу (дешевле, но менее строго). См. docs/README.md Гейт 5."
        )

    def run_august(step_name: str, wallets: list[str]) -> pd.DataFrame:
        sql = render_sql(
            read_sql("06_wallet_agg_august"),
            {
                "start_date": q_ts(CONFIG.test_start),
                "end_date": q_ts(CONFIG.test_end),
                "base_token_symbols": base_tokens_sql,
                "cohort_wallets": q_list(wallets),
            },
        )
        return run_named(step_name, sql)

    # --- Гейт 4: PnL за август ---
    print(f"== Шаг 7: агрегация по кошельку, август ({len(wallets_for_august)} адресов) ==")
    df_august = run_august("06_wallet_agg_august", wallets_for_august)
    print(f"  {len(df_august)} кошельков из выборки совершили ≥1 своп в августе.")

    cohort_a = merge_august_pnl(cohort_a, df_august)
    cohort_b = merge_august_pnl(cohort_b, df_august)

    if full_pool_spearman:
        full_gated_with_aug = merge_august_pnl(df_gated, df_august)
        all_july = full_gated_with_aug["realized_pnl_usd"].to_numpy()
        all_aug = full_gated_with_aug["realized_pnl_usd_august"].to_numpy()
    else:
        both = pd.concat([cohort_a, cohort_b])
        all_july = both["realized_pnl_usd"].to_numpy()
        all_aug = both["realized_pnl_usd_august"].to_numpy()

    # --- Гейт 5: статистика ---
    print("== Шаг 8: статистический тест ==")
    result = run_full_test(cohort_a, cohort_b, all_july, all_aug, alpha=CONFIG.significance_alpha)
    print(f"  Вердикт: {result.verdict}")
    print(f"  {result.verdict_reasoning}")

    # --- Sensitivity: MIN_TRADES 10 vs 15 (дешёвый прогон только ради N + p) ---
    print("== Шаг 9: sensitivity MIN_TRADES=15 ==")
    sens_n_10, sens_p_10 = len(df_gated), result.mannwhitney_p_one_sided
    try:
        df_gated_15 = run_gate5(15)
        cohort_a_15, cohort_b_15 = build_cohorts(df_gated_15)
        wallets_15 = pd.concat([cohort_a_15, cohort_b_15])["wallet_address"].tolist()
        df_august_15 = run_august("06_wallet_agg_august_mt15", wallets_15)
        cohort_a_15 = merge_august_pnl(cohort_a_15, df_august_15)
        cohort_b_15 = merge_august_pnl(cohort_b_15, df_august_15)
        result_15 = run_full_test(cohort_a_15, cohort_b_15, alpha=CONFIG.significance_alpha)
        sens_n_15, sens_p_15 = len(df_gated_15), result_15.mannwhitney_p_one_sided
    except (DuneCreditsExhausted, DuneRateLimited) as e:
        print(f"  Sensitivity-прогон пропущен (кредиты/лимит): {e}")
        sens_n_15, sens_p_15 = "n/a (см. лог)", "n/a"

    # --- Отчёт ---
    print("== Шаг 10: рендер docs/RESULTS.md ==")
    context = {
        "GENERATED_AT": dt.datetime.utcnow().isoformat() + "Z",
        "MIN_TRADES": CONFIG.min_trades,
        "MIN_UNIQUE_TOKENS": CONFIG.min_unique_tokens,
        "SNIPER_BLOCK_WINDOW": CONFIG.sniper_block_window,
        "COHORT_SIZE": CONFIG.cohort_size,
        "N_A": len(cohort_a),
        "N_B": len(cohort_b),
        "JULY_MEDIAN_A": fmt_usd(cohort_a["realized_pnl_usd"].median()),
        "JULY_MEDIAN_B": fmt_usd(cohort_b["realized_pnl_usd"].median()),
        "AUG_MEDIAN_A": fmt_usd(cohort_a["realized_pnl_usd_august"].median()),
        "AUG_MEDIAN_B": fmt_usd(cohort_b["realized_pnl_usd_august"].median()),
        "AUG_MEAN_A": fmt_usd(cohort_a["realized_pnl_usd_august"].mean()),
        "AUG_MEAN_B": fmt_usd(cohort_b["realized_pnl_usd_august"].mean()),
        "PCT_PROFITABLE_A": fmt_pct(result.pct_profitable_a),
        "PCT_PROFITABLE_B": fmt_pct(result.pct_profitable_b),
        "JULY_TRADES_MEDIAN_A": cohort_a["trade_count"].median(),
        "JULY_TRADES_MEDIAN_B": cohort_b["trade_count"].median(),
        "JULY_TOKENS_MEDIAN_A": cohort_a["unique_tokens_traded"].median(),
        "JULY_TOKENS_MEDIAN_B": cohort_b["unique_tokens_traded"].median(),
        "TOTAL_GATED": len(df_gated),
        "TOTAL_EXCLUDED": len(df_excluded),
        "MWU_U": f"{result.mannwhitney_u:.1f}",
        "MWU_P": f"{result.mannwhitney_p_one_sided:.5f}",
        "EFFECT_SIZE": f"{result.rank_biserial_effect_size:.3f}",
        "BOOT_CI_LOW": fmt_usd(result.bootstrap_ci_low),
        "BOOT_CI_HIGH": fmt_usd(result.bootstrap_ci_high),
        "FISHER_P": f"{result.fisher_p:.5f}",
        "SPEARMAN_RHO": f"{result.spearman_rho:.3f}",
        "SPEARMAN_P": f"{result.spearman_p:.5f}",
        "TOP20_TABLE": build_top20_table(cohort_a),
        "VERDICT": result.verdict,
        "VERDICT_REASONING": result.verdict_reasoning,
        "SENS_10_N": sens_n_10,
        "SENS_10_P": f"{sens_p_10:.5f}" if isinstance(sens_p_10, float) else sens_p_10,
        "SENS_15_N": sens_n_15,
        "SENS_15_P": f"{sens_p_15:.5f}" if isinstance(sens_p_15, float) else sens_p_15,
    }
    report = render_report(context)
    (ROOT / CONFIG.results_doc).write_text(report)
    print(f"  Записано в {CONFIG.results_doc}")
    print(f"\n[run_pipeline] Dune executions в этом прогоне: {client.executions_this_run}")
    print("[run_pipeline] Готово.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DuneCreditsExhausted, DuneRateLimited) as e:
        print(f"\n[run_pipeline] ОСТАНОВЛЕНО: {e}", file=sys.stderr)
        raise SystemExit(1)
