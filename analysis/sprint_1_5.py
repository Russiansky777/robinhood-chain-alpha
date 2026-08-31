#!/usr/bin/env python3
"""Sprint 1.5: финальный до-тест линии копитрейдинга.

Контекст: Sprint 1 дал вердикт НЕТ по исходной гипотезе (location-shift
тест по всей когорте), но содержал два измерительных дефекта: (1) боты/
контракты с нечеловеческим числом сделок в популяции искажали PnL, (2)
Fisher-сравнение долей профитных не отделяло "торговал ли вообще" от
"был ли прибылен, когда торговал". Sprint 1.5 фиксирует оба и прогоняет
ОДИН финальный тест с критериями, зафиксированными до просмотра данных.
Это последняя итерация: при провале линия копитрейдинга закрывается.
См. docs/README.md, "Sprint 1.5", за полным обоснованием дизайна.

Бюджет: CONFIG.sprint15_credit_budget (default 250, см. .env.example).
Реальный расход отслеживается по ходу дела; если очередной дорогой шаг
рискует увести кумулятивный расход за бюджет, скрипт останавливается
ПЕРЕД ним и пишет частичный отчёт с чёткой пометкой, что именно не
досчитано -- не тратит дальше молча.

Экономия: 03 (агрегация по кошельку за июль, вся сеть) и 04@5мин
(снайпер-исключения при базовом окне) переиспользуются из Sprint 1 --
либо из закоммиченных /data/sprint1_reused/*.csv.gz, либо (первый раз)
через дешёвое чтение уже существующего execution_id без пересчёта. См.
analysis/recover_sprint1.py.

Использование:
    python analysis/sprint_1_5.py
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from config import CONFIG
from cohort_builder import build_cohorts, merge_august_pnl
from dune_client import DuneClient, DuneCreditsExhausted, DuneRateLimited, render_sql
from recover_sprint1 import recover_baseline, SPRINT1_ORIGINAL_COST
from run_pipeline import (
    read_sql,
    substitute_query_refs,
    q_ts,
    q_list,
    q_addr_list,
    next_day,
    fmt_usd,
    fmt_pct,
    print_ledger,
)
from stats_test import two_part_analysis, TwoPartResult

ROOT = Path(__file__).parent.parent
RESULTS_PATH = ROOT / CONFIG.results_doc
SECTION_MARKER = "## Sprint 1.5"

# Пессимистичные оценки стоимости КАЖДОГО шага (для персистентного
# гарда analysis/credit_guard.py -- проверка ПЕРЕД execute), взятые из
# реально залогированных стоимостей идентичных/аналогичных запросов
# Sprint 1 (см. docs/COST_POSTMORTEM.md) + запас ~15-20%. 02 на полном
# месяце доминирует весь бюджет остатка Sprint 1.5 -- см. инвентаризацию
# в docs/COST_POSTMORTEM.md перед запуском.
STEP_ESTIMATES = {
    "02_swaps_raw_july": 125.0,       # run #13: 102.8; run #12: 102.6 -- запас на рост данных
    "01_pool_creation_blocks": 1.0,   # run #13: 0.43; всегда <1
    "03_wallet_agg_july": 30.0,       # run #13: 25.5 -- только если recover_baseline не сработал
    "04_sniper_insider_exclusions": 3.0,  # run #13: 1.9 (5мин); та же форма запроса для 1мин
    "06_wallet_agg_august": 4.0,      # run #13: 1.27 на ~400 адресов; здесь объединение до ~800
}


class BudgetStop(Exception):
    def __init__(self, spent: float, note: str):
        self.spent = spent
        self.note = note
        super().__init__(f"Бюджет Sprint 1.5 ({CONFIG.sprint15_credit_budget}) исчерпан на {spent:.2f}: {note}")


def total_spent(client: DuneClient) -> float:
    return sum((row["credits"] or 0.0) for row in client.credit_ledger)


def budget_gate(client: DuneClient, note: str) -> float:
    spent = total_spent(client)
    if spent >= CONFIG.sprint15_credit_budget:
        raise BudgetStop(spent, note)
    return spent


# ---------- гейт 2 (порог сделок/токенов) и капы -- чистый Python ----------


def gate2_filter(df_agg: pd.DataFrame, df_excluded: pd.DataFrame, min_trades: int, min_tokens: int) -> pd.DataFrame:
    """Реплика sql/05_final_cohort_pool_july.sql на Python: не требует
    отдельного запроса к Dune, раз 03 (df_agg) и 04 (df_excluded) уже
    получены как DataFrame -- см. docstring модуля."""
    excluded_set = set(df_excluded["wallet_address"])
    mask = (
        (~df_agg["wallet_address"].isin(excluded_set))
        & (df_agg["trade_count"] >= min_trades)
        & (df_agg["unique_tokens_traded"] >= min_tokens)
    )
    return df_agg[mask].copy()


def apply_trade_cap(df_gated: pd.DataFrame, max_trades: int, df_agg_full: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    over_cap = df_gated[df_gated["trade_count"] > max_trades]
    kept = df_gated[df_gated["trade_count"] <= max_trades].copy()
    total_network_pnl = float(df_agg_full["realized_pnl_usd"].sum())
    total_gated_pnl = float(df_gated["realized_pnl_usd"].sum())
    cut_pnl = float(over_cap["realized_pnl_usd"].sum())
    return kept, {
        "max_trades": max_trades,
        "n_before": len(df_gated),
        "n_cut": len(over_cap),
        "n_kept": len(kept),
        "cut_pnl_usd": cut_pnl,
        "total_network_pnl_usd": total_network_pnl,
        "total_gated_pnl_usd": total_gated_pnl,
        "pct_of_network_pnl": (cut_pnl / total_network_pnl * 100) if total_network_pnl else float("nan"),
        "pct_of_gated_pnl": (cut_pnl / total_gated_pnl * 100) if total_gated_pnl else float("nan"),
    }


def sniper_histogram(df_excluded: pd.DataFrame, df_agg_full: pd.DataFrame) -> pd.Series:
    excl_trades = df_agg_full[df_agg_full["wallet_address"].isin(set(df_excluded["wallet_address"]))]["trade_count"]
    bins = [1, 3, 11, 101, np.inf]
    labels = ["1-2", "3-10", "11-100", "100+"]
    bucketed = pd.cut(excl_trades, bins=bins, right=False, labels=labels)
    return bucketed.value_counts().reindex(labels).fillna(0).astype(int)


def build_top20(cohort_a_with_august: pd.DataFrame) -> str:
    top20 = cohort_a_with_august.sort_values("realized_pnl_usd_august", ascending=False).head(20)
    lines = [
        "| # | Address | July PnL | August PnL | Сделок (июль) | Сделок (август) |",
        "|---|---|---|---|---|---|",
    ]
    for i, row in enumerate(top20.itertuples(), start=1):
        lines.append(
            f"| {i} | `{row.wallet_address}` | {fmt_usd(row.realized_pnl_usd)} | "
            f"{fmt_usd(row.realized_pnl_usd_august)} | {row.trade_count} | {row.trade_count_august} |"
        )
    return "\n".join(lines)


def fmt_lift(x: float) -> str:
    if np.isnan(x):
        return "n/a"
    if np.isinf(x):
        return "∞"
    return f"{x:.2f}x"


def two_part_md(label: str, r: TwoPartResult) -> str:
    return (
        f"**{label}**\n\n"
        f"- Часть 0 (описательно): активны в августе — А {r.n_active_a}/{r.n_a} ({fmt_pct(r.pct_active_a)}), "
        f"Б {r.n_active_b}/{r.n_b} ({fmt_pct(r.pct_active_b)})\n"
        f"- Часть 1 (ПЕРВИЧНЫЙ, среди августовски-активных): доля профитных "
        f"А={fmt_pct(r.prop_positive_a)} vs Б={fmt_pct(r.prop_positive_b)}, лифт={fmt_lift(r.lift)}, "
        f"Fisher one-sided p={r.fisher_p:.5f} (нужно p<{CONFIG.part1_alpha} и лифт≥{CONFIG.part1_min_lift}x) "
        f"→ **{'ПРОШЛА' if r.part1_pass else 'НЕ ПРОШЛА'}**\n"
        f"- Часть 2 (вторичный, среди августовски-активных): медиана А=${r.median_active_a:,.0f} vs "
        f"Б=${r.median_active_b:,.0f} ({'А>Б' if r.part2_directional else 'А≤Б'}), "
        f"Mann-Whitney U={r.mwu_u:.1f}, p={r.mwu_p:.5f} "
        f"({'значим' if r.part2_significant else 'не значим'} при {CONFIG.part2_alpha})\n"
        f"- **Вердикт ячейки: {r.verdict}**"
    )


def main() -> int:
    if not CONFIG.dune_api_key:
        print("ОШИБКА: DUNE_API_KEY не задан.", file=sys.stderr)
        return 1

    client = DuneClient()
    query_ids: dict[str, int] = {}
    base_tokens_sql = q_list(list(CONFIG.base_token_symbols))
    train_window = {"start_date": q_ts(CONFIG.train_start), "end_date": q_ts(CONFIG.train_end)}
    test_window = {"start_date": q_ts(CONFIG.test_start), "end_date": q_ts(CONFIG.test_end)}
    sections: list[str] = []  # markdown-куски -- накапливаем по ходу, чтобы частичный стоп не терял уже готовое

    def _estimate_for(step_key: str) -> float:
        for prefix, est in STEP_ESTIMATES.items():
            if step_key.startswith(prefix):
                return est
        from credit_guard import DEFAULT_ESTIMATE
        return DEFAULT_ESTIMATE

    def run_named(step_key: str, sql: str, fetch_results: bool = True, ref_key: str | None = None):
        qid = client.create_query(step_key, sql)
        query_ids[ref_key or step_key] = qid
        try:
            return client.run_sql_cached(
                step_key, sql, query_id=qid, fetch_results=fetch_results,
                estimated_credits=_estimate_for(step_key),
            )
        except (DuneCreditsExhausted, DuneRateLimited) as e:
            spent = total_spent(client)
            print(f"\n[sprint_1_5] ОСТАНОВЛЕНО на шаге '{step_key}' (402/429): {e}\nПотрачено: {spent:.2f}", file=sys.stderr)
            raise

    def run_august(step_key: str, wallets: list[str]) -> pd.DataFrame:
        sql = render_sql(
            read_sql("06_wallet_agg_august"),
            {**test_window, "base_token_symbols": base_tokens_sql, "cohort_wallets": q_addr_list(wallets)},
        )
        return run_named(step_key, sql)

    try:
        # ============ 1. Обязательная база: 02+01 материализуются заново ============
        print("== 02 (свопы июля) + 01 (рождение пулов) -- нельзя переиспользовать, execution_id не логировался ==")
        run_named("02_swaps_raw_july", render_sql(read_sql("02_swaps_raw_july"), train_window), fetch_results=False)
        run_named("01_pool_creation_blocks", substitute_query_refs(read_sql("01_pool_creation_blocks"), query_ids), fetch_results=False)
        spent = budget_gate(client, "после 02+01 (обязательная база)")
        print(f"  Потрачено на 02+01: {spent:.2f} кредитов")

        # ============ 2. Переиспользуем 03 (+04@5мин, +05@5мин как cross-check) ============
        recovered = recover_baseline(client)
        if recovered.recovered:
            df_agg_july = recovered.df_agg_july
            df_excluded_5m = recovered.df_excluded_5m
            recovery_note = recovered.note
            recovery_savings = recovered.savings_credits
            if recovered.query_id_03 is not None:
                query_ids["03_wallet_agg_july"] = recovered.query_id_03
        else:
            print("  [recover] Пересчитываю 03 и 04@5мин с нуля (fallback).")
            df_agg_july = run_named(
                "03_wallet_agg_july",
                render_sql(substitute_query_refs(read_sql("03_wallet_agg_july"), query_ids), {"base_token_symbols": base_tokens_sql}),
            )
            df_excluded_5m = run_named(
                "04_sniper_insider_exclusions_5m",
                render_sql(substitute_query_refs(read_sql("04_sniper_insider_exclusions"), query_ids), {"sniper_time_window_minutes": CONFIG.sniper_time_window_minutes}),
                ref_key="04_sniper_insider_exclusions",
            )
            recovery_note = "Переиспользование не сработало -- пересчитано с нуля."
            recovery_savings = 0.0
        print(f"  Всего июльских трейдеров (03): {len(df_agg_july)}; исключено снайперов @5мин (04): {len(df_excluded_5m)}")
        spent = budget_gate(client, "после восстановления/пересчёта 03+04@5мин")

        # ============ 3. Гейт 2 (Python) + фильтр копируемости для sniper=5мин ============
        df_gated_5m = gate2_filter(df_agg_july, df_excluded_5m, CONFIG.min_trades, CONFIG.min_unique_tokens)
        print(f"  Прошли гейты 1-2 @5мин (до фильтра копируемости): {len(df_gated_5m)}")

        def cohorts_for(df_gated: pd.DataFrame, max_trades: int, label: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
            kept, cap_stats = apply_trade_cap(df_gated, max_trades, df_agg_july)
            print(
                f"  [{label}] Фильтр копируемости (>{max_trades} сделок/июль): срезано "
                f"{cap_stats['n_cut']} кошельков ({cap_stats['pct_of_network_pnl']:.2f}% July PnL сети, "
                f"{cap_stats['pct_of_gated_pnl']:.2f}% PnL гейтованной популяции), осталось {cap_stats['n_kept']}"
            )
            a, b = build_cohorts(kept, cohort_size=CONFIG.cohort_size)
            return a, b, cap_stats

        cohort_a_5_1500, cohort_b_5_1500, cap_stats_5_1500 = cohorts_for(df_gated_5m, CONFIG.copyability_max_trades, "sniper=5мин, cap=1500")
        cohort_a_5_3000, cohort_b_5_3000, cap_stats_5_3000 = cohorts_for(df_gated_5m, CONFIG.copyability_max_trades_sensitivity, "sniper=5мин, cap=3000")

        wallets_5m = pd.concat([cohort_a_5_1500, cohort_b_5_1500, cohort_a_5_3000, cohort_b_5_3000])["wallet_address"].unique().tolist()
        spent = budget_gate(client, "перед August-запросом (sniper=5мин)")
        df_august_5m = run_august("06_wallet_agg_august_sniper5m", wallets_5m)
        print(f"  {len(df_august_5m)} из {len(wallets_5m)} кошельков (sniper=5мин) торговали в августе")

        def with_august(a: pd.DataFrame, b: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
            return merge_august_pnl(a, df_august_5m), merge_august_pnl(b, df_august_5m)

        cohort_a_5_1500, cohort_b_5_1500 = with_august(cohort_a_5_1500, cohort_b_5_1500)
        cohort_a_5_3000, cohort_b_5_3000 = with_august(cohort_a_5_3000, cohort_b_5_3000)

        result_5_1500 = two_part_analysis(cohort_a_5_1500, cohort_b_5_1500, CONFIG.part1_alpha, CONFIG.part1_min_lift, CONFIG.part2_alpha)
        result_5_3000 = two_part_analysis(cohort_a_5_3000, cohort_b_5_3000, CONFIG.part1_alpha, CONFIG.part1_min_lift, CONFIG.part2_alpha)
        print(f"  ПЕРВИЧНЫЙ результат (sniper=5мин, cap=1500): {result_5_1500.verdict}")

        sections.append(f"{SECTION_MARKER}\n\nСгенерировано `analysis/sprint_1_5.py` в {dt.datetime.utcnow().isoformat()}Z.\n")
        sections.append(build_intro_section(recovered, recovery_note, recovery_savings))
        sections.append(build_cap_section(cap_stats_5_1500, cap_stats_5_3000))
        sections.append("### Основной результат (sniper=5мин, cap=1500 сделок) -- ПЕРВИЧНЫЙ\n\n" + two_part_md("sniper=5мин, cap=1500", result_5_1500))

        # ============ 4. Sensitivity: sniper=1мин ============
        # Гейт 2 для этой ветки тоже считается в Python (gate2_filter) поверх
        # уже имеющегося df_agg_july -- 04@1мин ссылается только на 01/02
        # (см. sql/04_sniper_insider_exclusions.sql), не на 03, так что
        # отсутствие query_id_03 при восстановлении из локальных файлов
        # здесь ни на что не влияет.
        spent = budget_gate(client, "перед sniper=1мин веткой (04@1мин)")
        df_excluded_1m = run_named(
            "04_sniper_insider_exclusions_1m",
            render_sql(substitute_query_refs(read_sql("04_sniper_insider_exclusions"), query_ids), {"sniper_time_window_minutes": CONFIG.sniper_time_window_minutes_sensitivity}),
            ref_key="04_sniper_insider_exclusions",
        )
        print(f"  Исключено снайперов @1мин (04): {len(df_excluded_1m)}")
        df_gated_1m = gate2_filter(df_agg_july, df_excluded_1m, CONFIG.min_trades, CONFIG.min_unique_tokens)
        print(f"  Прошли гейты 1-2 @1мин (до фильтра копируемости): {len(df_gated_1m)}")

        cohort_a_1_1500, cohort_b_1_1500, cap_stats_1_1500 = cohorts_for(df_gated_1m, CONFIG.copyability_max_trades, "sniper=1мин, cap=1500")
        cohort_a_1_3000, cohort_b_1_3000, cap_stats_1_3000 = cohorts_for(df_gated_1m, CONFIG.copyability_max_trades_sensitivity, "sniper=1мин, cap=3000")

        wallets_1m = pd.concat([cohort_a_1_1500, cohort_b_1_1500, cohort_a_1_3000, cohort_b_1_3000])["wallet_address"].unique().tolist()
        spent = budget_gate(client, "перед August-запросом (sniper=1мин)")
        df_august_1m = run_august("06_wallet_agg_august_sniper1m", wallets_1m)

        cohort_a_1_1500, cohort_b_1_1500 = merge_august_pnl(cohort_a_1_1500, df_august_1m), merge_august_pnl(cohort_b_1_1500, df_august_1m)
        cohort_a_1_3000, cohort_b_1_3000 = merge_august_pnl(cohort_a_1_3000, df_august_1m), merge_august_pnl(cohort_b_1_3000, df_august_1m)

        result_1_1500 = two_part_analysis(cohort_a_1_1500, cohort_b_1_1500, CONFIG.part1_alpha, CONFIG.part1_min_lift, CONFIG.part2_alpha)
        result_1_3000 = two_part_analysis(cohort_a_1_3000, cohort_b_1_3000, CONFIG.part1_alpha, CONFIG.part1_min_lift, CONFIG.part2_alpha)

        cells = {
            "sniper=5мин, cap=1500 (ПЕРВИЧНАЯ)": result_5_1500,
            "sniper=5мин, cap=3000": result_5_3000,
            "sniper=1мин, cap=1500": result_1_1500,
            "sniper=1мин, cap=3000": result_1_3000,
        }
        signs = {k: np.sign(r.prop_positive_a - r.prop_positive_b) if not np.isnan(r.prop_positive_a - r.prop_positive_b) else np.nan for k, r in cells.items()}
        robust = len(set(s for s in signs.values() if not np.isnan(s))) <= 1 and not any(np.isnan(s) for s in signs.values())

        sections.append(
            "### Sensitivity-сетка (2×2: снайпер-гейт × кап сделок)\n\n"
            "| Ячейка | Активных А/Б | Доля профитных А vs Б | Лифт | Fisher p | Часть 1 | Медиана А vs Б | MWU p | Вердикт ячейки |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
            + "\n".join(
                f"| {k} | {r.n_active_a}/{r.n_active_b} | {fmt_pct(r.prop_positive_a)} vs {fmt_pct(r.prop_positive_b)} | "
                f"{fmt_lift(r.lift)} | {r.fisher_p:.5f} | {'✅' if r.part1_pass else '❌'} | "
                f"${r.median_active_a:,.0f} vs ${r.median_active_b:,.0f} | {r.mwu_p:.5f} | **{r.verdict}** |"
                for k, r in cells.items()
            )
            + f"\n\n**Знак результата Части 1 устойчив во всех 4 комбинациях: {'ДА' if robust else 'НЕТ'}** "
            f"(доля профитных А {'>' if all(s > 0 for s in signs.values() if not np.isnan(s)) else '<= для части ячеек, см. таблицу' } доли профитных Б в каждой ячейке)."
        )

        # ============ 5. Гистограмма профиля снайперов (@5мин, бесплатно) ============
        hist = sniper_histogram(df_excluded_5m, df_agg_july)
        sections.append(
            "### Профиль исключённых снайперов (гейт @5мин, {} кошельков)\n\n".format(len(df_excluded_5m))
            + "Распределение общего числа сделок за июль среди кошельков, "
            "исключённых временным суррогатом гейта 1 (первый своп в первые "
            "5 минут жизни пула):\n\n"
            "| Бакет сделок/июль | Кошельков | Доля |\n|---|---|---|\n"
            + "\n".join(f"| {b} | {int(hist[b])} | {hist[b]/len(df_excluded_5m)*100:.1f}% | " for b in hist.index)
        )

        # ============ 6. Топ-20 очищенной когорты А (primary cell) ============
        sections.append("### Топ-20 персистентных кошельков (очищенная когорта А, sniper=5мин, cap=1500, по августовскому PnL)\n\n" + build_top20(cohort_a_5_1500))

        # ============ 7. Итоговый вердикт спринта ============
        final_verdict = result_5_1500.verdict
        sections.append(
            "### Итоговый вердикт Sprint 1.5\n\n"
            f"**{final_verdict}**\n\n"
            f"{result_5_1500.verdict_reasoning}\n\n"
            f"Устойчивость по sensitivity-сетке: {'подтверждена (знак не меняется)' if robust else 'НЕ подтверждена -- знак результата Части 1 меняется между ячейками, см. таблицу выше'}."
        )

        full_ledger_total = total_spent(client)
        sections.append(
            "### Стоимость Sprint 1.5\n\n"
            f"Потрачено кредитов в этом прогоне: **{full_ledger_total:.2f}** "
            f"(бюджет {CONFIG.sprint15_credit_budget}). Сэкономлено переиспользованием "
            f"результатов Sprint 1 (03 + 04@5мин, вместо пересчёта): **~{recovery_savings:.2f}** кредитов.\n\n"
            + print_ledger_md(client.credit_ledger)
        )

        write_results(sections)
        print(f"\n[sprint_1_5] Готово. Итого потрачено: {full_ledger_total:.2f} кредитов.")
        return 0

    except BudgetStop as e:
        sections.append(
            f"\n\n> **ОСТАНОВЛЕНО по бюджету на {e.spent:.2f} из {CONFIG.sprint15_credit_budget} кредитов** "
            f"перед шагом: {e.note}. Приведённые выше разделы (если есть) — то, что успело досчитаться."
        )
        write_results(sections)
        print(f"\n[sprint_1_5] {e}", file=sys.stderr)
        return 3
    except (DuneCreditsExhausted, DuneRateLimited) as e:
        sections.append(f"\n\n> **ОСТАНОВЛЕНО: {e}**")
        write_results(sections)
        return 1


def build_intro_section(recovered, recovery_note: str, savings: float) -> str:
    lines = [
        "### Дизайн и статус переиспользования данных Sprint 1",
        "",
        f"- Гейт снайперов (первичный): {CONFIG.sniper_time_window_minutes} мин; sensitivity: {CONFIG.sniper_time_window_minutes_sensitivity} мин",
        f"- Кап копируемости (первичный): >{CONFIG.copyability_max_trades} сделок/июль исключены; sensitivity: >{CONFIG.copyability_max_trades_sensitivity}",
        f"- Первичный критерий (Часть 1): Fisher one-sided p<{CONFIG.part1_alpha} И лифт≥{CONFIG.part1_min_lift}x",
        f"- Вторичный критерий (Часть 2): медиана А>Б среди августовски-активных (p<{CONFIG.part2_alpha} — informativно, не обязательно)",
        "",
        f"**Переиспользование результатов Sprint 1:** {recovery_note}",
    ]
    if recovered.recovered:
        source = "закоммиченных /data/sprint1_reused/*.csv.gz (без обращения к Dune)" if recovered.from_local_files else "execution_id Sprint 1 через Dune API (status+results, без пересчёта)"
        lines.append(f"Источник: {source}. Сэкономлено против полного пересчёта: ~{savings:.2f} кредитов.")
    return "\n".join(lines)


def build_cap_section(cap_1500: dict, cap_3000: dict) -> str:
    return (
        "### Фильтр копируемости (боты/HFT/маркет-мейкеры вне популяции)\n\n"
        "| Кап (сделок/июль) | Кошельков до | Срезано | Осталось | PnL срезанных | % July PnL сети | % PnL гейтованной популяции |\n"
        "|---|---|---|---|---|---|---|\n"
        f"| {cap_1500['max_trades']} (первичный) | {cap_1500['n_before']} | {cap_1500['n_cut']} | {cap_1500['n_kept']} | "
        f"{fmt_usd(cap_1500['cut_pnl_usd'])} | {cap_1500['pct_of_network_pnl']:.2f}% | {cap_1500['pct_of_gated_pnl']:.2f}% |\n"
        f"| {cap_3000['max_trades']} (sensitivity) | {cap_3000['n_before']} | {cap_3000['n_cut']} | {cap_3000['n_kept']} | "
        f"{fmt_usd(cap_3000['cut_pnl_usd'])} | {cap_3000['pct_of_network_pnl']:.2f}% | {cap_3000['pct_of_gated_pnl']:.2f}% |"
    )


def print_ledger_md(ledger: list[dict]) -> str:
    lines = ["| Запрос | Кредиты | Кэш |", "|---|---|---|"]
    for row in ledger:
        lines.append(f"| {row['name']} | {row['credits'] if row['credits'] is not None else 'n/a'} | {'да' if row['cached'] else 'нет'} |")
    return "\n".join(lines)


def write_results(sections: list[str]) -> None:
    existing = RESULTS_PATH.read_text() if RESULTS_PATH.exists() else ""
    marker_pos = existing.find(SECTION_MARKER)
    if marker_pos != -1:
        existing = existing[:marker_pos].rstrip() + "\n\n"
    else:
        existing = existing.rstrip() + "\n\n"
    new_content = existing + "\n\n".join(sections) + "\n"
    RESULTS_PATH.write_text(new_content)


if __name__ == "__main__":
    raise SystemExit(main())
