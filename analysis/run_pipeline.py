#!/usr/bin/env python3
"""Точка входа: смоук-тест на одном дне -> (если дёшево) полный пайплайн
на июле/августе -> docs/RESULTS.md.

Требует DUNE_API_KEY в .env / env. Если его нет — падает сразу с понятным
сообщением, ничего не выполняя (см. docs/DATA_ACCESS.md).

Дизайн на 2026-08-31 (после реального прогона на Dune, см.
docs/DATA_ACCESS.md): единственный источник данных — dex.trades,
никаких сырых таблиц чейна. Перед тем как гонять полный период,
пайплайн ВСЕГДА сначала прогоняет себя целиком на одном дне
(CONFIG.smoke_date) и меряет фактическую стоимость каждого запроса в
кредитах (Dune API отдаёт `execution_cost_credits` в статусе
исполнения). Полный прогон запускается автоматически только если смоук
уложился в CONFIG.smoke_credit_budget (default 120) — иначе пайплайн
останавливается и печатает таблицу "запрос -> кредиты" на решение
человека.

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
    ссылки на сохранённый запрос: `query_<numeric_id>`."""
    for name, qid in query_ids.items():
        sql = sql.replace(f"query_{name}", f"query_{qid}")
    return sql


def q_ts(date_str: str) -> str:
    """'2026-07-01' -> "'2026-07-01 00:00:00'" -- готовый timestamp-литерал."""
    return f"'{date_str} 00:00:00'"


def q_list(items: list[str]) -> str:
    """['WETH','USDC'] -> "'WETH','USDC'" -- готовый список для IN(...) / array[...]."""
    if not items:
        # Заведомо несуществующий адрес -- пустой IN(...) невалиден в SQL,
        # а этот список никогда ни с чем не совпадёт.
        return "'0x0000000000000000000000000000000000000000'"
    return ",".join("'" + str(x).replace("'", "''") + "'" for x in items)


def next_day(date_str: str) -> str:
    d = dt.date.fromisoformat(date_str)
    return (d + dt.timedelta(days=1)).isoformat()


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


def print_ledger(ledger: list[dict], title: str) -> float:
    total = sum((row["credits"] or 0.0) for row in ledger)
    print(f"\n----- {title}: запрос -> кредиты -----")
    for row in ledger:
        tag = " (кэш)" if row["cached"] else ""
        cost = row["credits"] if row["credits"] is not None else "n/a"
        print(f"  {row['name']:<40} {cost}{tag}")
    print(f"  {'ИТОГО':<40} {total:.3f}")
    return total


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

    def run_named(step_key: str, sql: str, fetch_results: bool = True) -> pd.DataFrame | None:
        qid = client.create_query(step_key, sql)
        query_ids[step_key] = qid
        try:
            return client.run_sql_cached(step_key, sql, query_id=qid, fetch_results=fetch_results)
        except (DuneCreditsExhausted, DuneRateLimited) as e:
            total_so_far = print_ledger(client.credit_ledger, "Потрачено до остановки")
            print(
                f"\n[run_pipeline] ОСТАНОВЛЕНО на шаге '{step_key}': {e}\n"
                f"Итого потрачено в этом прогоне до остановки: {total_so_far:.3f} кредитов.\n"
                f"Не ретраю автоматически. Ждём решения.",
                file=sys.stderr,
            )
            raise

    def run_gates(window: dict[str, str], min_trades: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Шаги 1-5 (dex.trades-only, см. sql/00_notes.md) для окна
        [start_date, end_date). Возвращает (df_agg, df_excluded, df_gated).
        """
        # fetch_results=False на 02 и 01: их DataFrame нигде не используется
        # в Python напрямую (03/04 обращаются к ним через query_XX ссылки на
        # стороне Dune) -- а сырые свопы даже за один день оказались
        # огромными (1.8 GiB, см. docs/DATA_ACCESS.md), нефрагментированная
        # выгрузка такого объёма падает на самом API.
        run_named("02_swaps_raw_july", render_sql(read_sql("02_swaps_raw_july"), window), fetch_results=False)
        run_named(
            "01_pool_creation_blocks",
            substitute_query_refs(read_sql("01_pool_creation_blocks"), query_ids),
            fetch_results=False,
        )
        df_agg = run_named(
            "03_wallet_agg_july",
            render_sql(substitute_query_refs(read_sql("03_wallet_agg_july"), query_ids), {"base_token_symbols": base_tokens_sql}),
        )
        df_excluded = run_named(
            "04_sniper_insider_exclusions",
            render_sql(
                substitute_query_refs(read_sql("04_sniper_insider_exclusions"), query_ids),
                {"sniper_time_window_minutes": CONFIG.sniper_time_window_minutes},
            ),
        )
        df_gated = run_named(
            f"05_final_cohort_pool_july_mt{min_trades}",
            render_sql(
                substitute_query_refs(read_sql("05_final_cohort_pool_july"), query_ids),
                {"min_trades": min_trades, "min_unique_tokens": CONFIG.min_unique_tokens},
            ),
        )
        return df_agg, df_excluded, df_gated

    def run_august(step_key: str, wallets: list[str], window: dict[str, str]) -> pd.DataFrame:
        sql = render_sql(
            read_sql("06_wallet_agg_august"),
            {**window, "base_token_symbols": base_tokens_sql, "cohort_wallets": q_list(wallets)},
        )
        return run_named(step_key, sql)

    # ================= ФАЗА 1: СМОУК-ТЕСТ =================
    print("=" * 70)
    print(f"ФАЗА 1: СМОУК-ТЕСТ на {CONFIG.smoke_date} (один день) — измеряем")
    print("реальную стоимость каждого запроса перед масштабированием.")
    print("=" * 70)

    smoke_window = {"start_date": q_ts(CONFIG.smoke_date), "end_date": q_ts(next_day(CONFIG.smoke_date))}
    try:
        df_agg_smoke, _df_excl_smoke, _df_gated_smoke = run_gates(smoke_window, CONFIG.min_trades)
        smoke_wallets = df_agg_smoke["wallet_address"].head(5).tolist() if len(df_agg_smoke) else []
        if smoke_wallets:
            run_august("06_wallet_agg_august_SMOKE", smoke_wallets, smoke_window)
        else:
            print(f"[smoke] На {CONFIG.smoke_date} не нашлось ни одного кошелька в dex.trades — шаг 06 пропущен в смоуке.")
    except (DuneCreditsExhausted, DuneRateLimited):
        return 1

    smoke_ledger = list(client.credit_ledger)
    client.credit_ledger.clear()
    smoke_total = print_ledger(smoke_ledger, "СМОУК-ТЕСТ")

    if smoke_total > CONFIG.smoke_credit_budget:
        print(
            f"\n[run_pipeline] СТОП: смоук-тест стоил {smoke_total:.3f} кредитов "
            f"(бюджет — {CONFIG.smoke_credit_budget}). Полный прогон НЕ запущен, "
            f"docs/RESULTS.md не менялся. Ждём решения — можно поднять "
            f"SMOKE_CREDIT_BUDGET, если это осознанно приемлемо, или сначала "
            f"разобраться, какой конкретно шаг из таблицы выше дорогой."
        )
        return 3

    print(
        f"\n[run_pipeline] Смоук уложился в бюджет ({smoke_total:.3f} <= "
        f"{CONFIG.smoke_credit_budget} кредитов) — продолжаю на полном периоде "
        f"(июль -> когорты, август -> проверка персистентности)."
    )

    # ================= ФАЗА 2: ПОЛНЫЙ ПРОГОН =================
    train_window = {"start_date": q_ts(CONFIG.train_start), "end_date": q_ts(CONFIG.train_end)}
    test_window = {"start_date": q_ts(CONFIG.test_start), "end_date": q_ts(CONFIG.test_end)}

    print("\n" + "=" * 70)
    print(f"ФАЗА 2: ПОЛНЫЙ ПРОГОН — июль {CONFIG.train_start}..{CONFIG.train_end}, "
          f"август {CONFIG.test_start}..{CONFIG.test_end}")
    print("=" * 70)

    try:
        df_agg_july, df_excluded, df_gated = run_gates(train_window, CONFIG.min_trades)
    except (DuneCreditsExhausted, DuneRateLimited):
        return 1
    print(f"  Июльских трейдеров всего: {len(df_agg_july)}; исключено как снайперы: {len(df_excluded)}; "
          f"прошли гейты 1-2: {len(df_gated)}")

    cohort_a, cohort_b = build_cohorts(df_gated)
    print(f"  Когорта А: {len(cohort_a)}, Когорта Б: {len(cohort_b)}")

    full_pool_spearman = os.environ.get("FULL_POOL_SPEARMAN", "true").lower() == "true"
    wallets_for_august = (
        df_gated["wallet_address"].tolist()
        if full_pool_spearman
        else pd.concat([cohort_a, cohort_b])["wallet_address"].tolist()
    )
    if not full_pool_spearman:
        print("  [ЭКОНОМИЯ КРЕДИТОВ] FULL_POOL_SPEARMAN=false — Spearman только по когортам А+Б.")

    try:
        df_august = run_august("06_wallet_agg_august", wallets_for_august, test_window)
    except (DuneCreditsExhausted, DuneRateLimited):
        return 1
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

    print("\n== Статистический тест ==")
    result = run_full_test(cohort_a, cohort_b, all_july, all_aug, alpha=CONFIG.significance_alpha)
    print(f"  Вердикт: {result.verdict}")
    print(f"  {result.verdict_reasoning}")

    # --- Sensitivity: MIN_TRADES 10 vs 15 (переиспользует кэш 01-04, свежий только 05+06) ---
    print("\n== Sensitivity MIN_TRADES=15 ==")
    sens_n_10, sens_p_10 = len(df_gated), result.mannwhitney_p_one_sided
    try:
        _agg15, _excl15, df_gated_15 = run_gates(train_window, 15)
        cohort_a_15, cohort_b_15 = build_cohorts(df_gated_15)
        wallets_15 = pd.concat([cohort_a_15, cohort_b_15])["wallet_address"].tolist()
        df_august_15 = run_august("06_wallet_agg_august_mt15", wallets_15, test_window)
        cohort_a_15 = merge_august_pnl(cohort_a_15, df_august_15)
        cohort_b_15 = merge_august_pnl(cohort_b_15, df_august_15)
        result_15 = run_full_test(cohort_a_15, cohort_b_15, alpha=CONFIG.significance_alpha)
        sens_n_15, sens_p_15 = len(df_gated_15), result_15.mannwhitney_p_one_sided
    except (DuneCreditsExhausted, DuneRateLimited) as e:
        print(f"  Sensitivity-прогон пропущен (кредиты/лимит): {e}. Основной результат (MIN_TRADES=10) не затронут.")
        sens_n_15, sens_p_15 = "n/a (см. лог)", "n/a"

    full_ledger = list(client.credit_ledger)
    full_total = print_ledger(full_ledger, "ПОЛНЫЙ ПРОГОН")
    print(f"\n[run_pipeline] Итого за оба прогона (смоук + полный): {smoke_total + full_total:.3f} кредитов.")

    # --- Отчёт ---
    print("\n== Рендер docs/RESULTS.md ==")
    context = {
        "GENERATED_AT": dt.datetime.utcnow().isoformat() + "Z",
        "MIN_TRADES": CONFIG.min_trades,
        "MIN_UNIQUE_TOKENS": CONFIG.min_unique_tokens,
        "SNIPER_BLOCK_WINDOW": f"{CONFIG.sniper_time_window_minutes} минут (временной суррогат, см. docs/README.md)",
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
    print("[run_pipeline] Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
