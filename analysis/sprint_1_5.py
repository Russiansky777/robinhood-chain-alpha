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

РЕВИЗИЯ 2 (см. docs/COST_POSTMORTEM.md): первая попытка этого скрипта
читала полный построчный результат 03_wallet_agg_july через API (1.21M
строк) -- оказалось, что API Result Read биллится ОТДЕЛЬНО от execute()
по объёму данных (163.98 кредита ЗА ОДНО такое чтение). Архитектура
переписана вокруг принципа «сырые данные не покидают Dune»: гейт 1
(снайперы, обе версии окна), гейт 2, капы копируемости и отбор когорт
(топ-200 + псевдослучайные 200 через детерминированный хэш) считаются
ОДНИМ SQL-запросом на стороне Dune (sql/03b_cohort_selection.sql),
который ссылается на уже материализованные (из Sprint 1, БЕЗ повторного
execute) query_01/02/03 через query_<id> и возвращает наружу только
строки кошельков, попавших хоть в одну когорту (~тысячи строк). Сводка
по капам (sql/03c) и гистограмма снайперов (sql/03d) -- отдельные
маленькие агрегатные запросы (единицы строк на выходе), не выгрузка
сырья.

Персистентный бюджетный гард (analysis/credit_guard.py) проверяет
ПЕРЕД каждым execute() И перед каждым чтением результата (по оценке
объёма данных), коммитит data/credits_spent.json после каждой платной
операции. Жёсткий лимит на остаток Sprint 1.5: 150 кредитов.

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
from cohort_builder import merge_august_pnl
from credit_guard import BudgetGuardStop
from dune_client import DuneClient, DuneCreditsExhausted, DuneRateLimited, render_sql
from run_pipeline import read_sql, substitute_query_refs, q_ts, q_list, q_addr_list, fmt_usd, fmt_pct
from stats_test import two_part_analysis, TwoPartResult

ROOT = Path(__file__).parent.parent
RESULTS_PATH = ROOT / CONFIG.results_doc
SECTION_MARKER = "## Sprint 1.5"

# Пессимистичные оценки стоимости execute() для гарда (см.
# docs/COST_POSTMORTEM.md, ревизия 2, смета перед запуском). 03b
# заведомо самый тяжёлый шаг (окна row_number() по ~1.2M строк 8 раз) --
# пользователь задал жёсткий пост-хок потолок 60 на его РЕАЛЬНУЮ
# стоимость (см. main(): проверка после run_sql_cached).
STEP_ESTIMATES = {
    "03b_cohort_selection": 45.0,   # ожидание ~25-35, потолок отдельно проверяется в 60
    "03c_cap_summary": 8.0,         # ожидание ~3-6
    "03d_sniper_histogram": 6.0,    # ожидание ~2-5
    "06_wallet_agg_august": 4.0,    # см. Sprint 1: 1.27 на ~400 адресов
}
COHORT_SEED = "sprint15-seed42"
STEP03B_HARD_CAP = 60.0  # см. п.4 задания пользователя: стоп-и-доклад, если факт > 60


class Step03bTooExpensive(Exception):
    def __init__(self, actual: float):
        self.actual = actual
        super().__init__(f"03b_cohort_selection стоил {actual:.2f} > потолок {STEP03B_HARD_CAP}")


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


def print_ledger_md(ledger: list[dict]) -> str:
    lines = ["| Запрос | Кредиты (execute) | Кэш |", "|---|---|---|"]
    for row in ledger:
        lines.append(f"| {row['name']} | {row['credits'] if row['credits'] is not None else 'n/a'} | {'да' if row['cached'] else 'нет'} |")
    return "\n".join(lines)


def build_cap_section(df_cap: pd.DataFrame) -> str:
    lines = [
        "### Фильтр копируемости (боты/HFT/маркет-мейкеры вне популяции)\n",
        "Посчитано серверным SQL (`sql/03c_cap_summary.sql`) по всем 4 "
        "комбинациям sniper-окно×кап -- не только по первичной, в отличие "
        "от ревизии 1.\n",
        "| Комбинация | Прошли гейты 1-2 и снайпер-фильтр | Срезано капом | PnL срезанных | % July PnL сети |",
        "|---|---|---|---|---|",
    ]
    for row in df_cap.itertuples():
        pct_net = (row.cut_pnl_usd / row.total_network_pnl_usd * 100) if row.total_network_pnl_usd else float("nan")
        lines.append(
            f"| {row.combo} | {row.n_gated} | {row.n_cut} | {fmt_usd(row.cut_pnl_usd)} | {pct_net:.2f}% |"
        )
    return "\n".join(lines)


def build_histogram_section(df_hist: pd.DataFrame) -> str:
    total = int(df_hist["n_wallets"].sum())
    lines = [
        f"### Профиль исключённых снайперов (гейт @{CONFIG.sniper_time_window_minutes}мин, {total} кошельков)\n",
        "Распределение общего числа сделок за июль среди кошельков, "
        "исключённых временным суррогатом гейта 1 (первый своп в первые "
        f"{CONFIG.sniper_time_window_minutes} минут жизни пула). Посчитано серверным "
        "SQL (`sql/03d_sniper_histogram.sql`) -- бакеты, не выгрузка построчного списка.\n",
        "| Бакет сделок/июль | Кошельков | Доля |",
        "|---|---|---|",
    ]
    order = {"1-2": 0, "3-10": 1, "11-100": 2, "100+": 3}
    for row in sorted(df_hist.itertuples(), key=lambda r: order.get(r.bucket, 99)):
        pct = row.n_wallets / total * 100 if total else float("nan")
        lines.append(f"| {row.bucket} | {row.n_wallets} | {pct:.1f}% |")
    return "\n".join(lines)


def main() -> int:
    if not CONFIG.dune_api_key:
        print("ОШИБКА: DUNE_API_KEY не задан.", file=sys.stderr)
        return 1

    client = DuneClient()
    query_ids: dict[str, int] = {}
    train_window = {"start_date": q_ts(CONFIG.train_start), "end_date": q_ts(CONFIG.train_end)}
    test_window = {"start_date": q_ts(CONFIG.test_start), "end_date": q_ts(CONFIG.test_end)}
    sections: list[str] = []

    combo_params = {
        "sniper_window_primary_minutes": CONFIG.sniper_time_window_minutes,
        "sniper_window_sensitivity_minutes": CONFIG.sniper_time_window_minutes_sensitivity,
        "cap_primary": CONFIG.copyability_max_trades,
        "cap_sensitivity": CONFIG.copyability_max_trades_sensitivity,
        "min_trades": CONFIG.min_trades,
        "min_unique_tokens": CONFIG.min_unique_tokens,
    }

    def substitute_refs(sql: str) -> str:
        return substitute_query_refs(sql, query_ids)

    def run_named(step_key: str, sql: str, expected_max_rows: int, expected_columns: int) -> pd.DataFrame:
        qid = client.create_query(step_key, sql)
        return client.run_sql_cached(
            step_key, sql, query_id=qid,
            estimated_credits=STEP_ESTIMATES.get(step_key),
            expected_max_rows=expected_max_rows, expected_columns=expected_columns,
        )

    try:
        # ============ 0. Pre-flight: query_id должны быть УЖЕ материализованы ============
        # НИКАКОГО execute здесь -- create_query с require_cached=True либо
        # возвращает существующий query_id (из query_id_map.json,
        # см. dune_client.py), либо падает немедленно, ДО единого платного
        # вызова. Это гарантирует, что 03b/03c/03d будут ссылаться на уже
        # оплаченные в Sprint 1 результаты 02/01/03, а не тихо создадут
        # новый (пустой) query и не заставят Dune пере-исполнить 02.
        print("== Pre-flight: 02/01/03 должны быть в query_id_map.json (без нового execute) ==")
        query_ids["02_swaps_raw_july"] = client.create_query(
            "02_swaps_raw_july", render_sql(read_sql("02_swaps_raw_july"), train_window), require_cached=True
        )
        query_ids["01_pool_creation_blocks"] = client.create_query(
            "01_pool_creation_blocks", substitute_refs(read_sql("01_pool_creation_blocks")), require_cached=True
        )
        # 03 использует {{base_token_symbols}} как unnest(array[...]) -- та же
        # подстановка, что и в Sprint 1 (run_pipeline.py:q_list), нужна
        # побитово идентичная SQL-строка, чтобы content-hash совпал.
        base_tokens_sql = q_list(list(CONFIG.base_token_symbols))
        query_ids["03_wallet_agg_july"] = client.create_query(
            "03_wallet_agg_july",
            render_sql(substitute_refs(read_sql("03_wallet_agg_july")), {"base_token_symbols": base_tokens_sql}),
            require_cached=True,
        )
        query_ids["03_wallet_agg_july"] = client.create_query(
            "03_wallet_agg_july",
            render_sql(substitute_refs(read_sql("03_wallet_agg_july")), {"base_token_symbols": base_tokens_sql}),
            require_cached=True,
        )
        print(f"  query_ids: 02={query_ids['02_swaps_raw_july']}, 01={query_ids['01_pool_creation_blocks']}, 03={query_ids['03_wallet_agg_july']}")
        print("  Pre-flight OK -- 02/01/03 уже материализованы на Dune, execute для них не требуется.")

        # ============ 1. 03b: гейт 1+2, капы, когорты -- одним запросом ============
        sql_03b = render_sql(
            substitute_refs(read_sql("03b_cohort_selection")),
            {**combo_params, "cohort_size": CONFIG.cohort_size, "cohort_seed": COHORT_SEED},
        )
        df_cohorts = run_named("03b_cohort_selection", sql_03b, expected_max_rows=2000, expected_columns=12)
        actual_03b_cost = client.credit_ledger[-1]["credits"] or 0.0
        print(f"  03b_cohort_selection: реальная стоимость execute = {actual_03b_cost:.2f} (потолок {STEP03B_HARD_CAP})")
        if actual_03b_cost > STEP03B_HARD_CAP:
            raise Step03bTooExpensive(actual_03b_cost)
        print(f"  03b вернул {len(df_cohorts)} строк (кошельки хотя бы в одной когорте)")

        # ============ 2. 03c: сводка по капам (для отчёта) ============
        sql_03c = render_sql(substitute_refs(read_sql("03c_cap_summary")), combo_params)
        df_cap = run_named("03c_cap_summary", sql_03c, expected_max_rows=10, expected_columns=5)

        # ============ 3. 03d: гистограмма снайперов (первичное окно) ============
        sql_03d = render_sql(
            substitute_refs(read_sql("03d_sniper_histogram")),
            {"sniper_window_primary_minutes": CONFIG.sniper_time_window_minutes},
        )
        df_hist = run_named("03d_sniper_histogram", sql_03d, expected_max_rows=10, expected_columns=2)

        # ============ 4. Когорты из df_cohorts (флаговые колонки) ============
        def cohort(flag_col: str) -> pd.DataFrame:
            return df_cohorts[df_cohorts[flag_col] == 1].copy()

        cohort_a_5_1500, cohort_b_5_1500 = cohort("cohort_a_5_1500"), cohort("cohort_b_5_1500")
        cohort_a_5_3000, cohort_b_5_3000 = cohort("cohort_a_5_3000"), cohort("cohort_b_5_3000")
        cohort_a_1_1500, cohort_b_1_1500 = cohort("cohort_a_1_1500"), cohort("cohort_b_1_1500")
        cohort_a_1_3000, cohort_b_1_3000 = cohort("cohort_a_1_3000"), cohort("cohort_b_1_3000")
        for label, df in [
            ("A 5/1500", cohort_a_5_1500), ("B 5/1500", cohort_b_5_1500),
            ("A 5/3000", cohort_a_5_3000), ("B 5/3000", cohort_b_5_3000),
            ("A 1/1500", cohort_a_1_1500), ("B 1/1500", cohort_b_1_1500),
            ("A 1/3000", cohort_a_1_3000), ("B 1/3000", cohort_b_1_3000),
        ]:
            print(f"  Когорта {label}: {len(df)} кошельков (ожидалось {CONFIG.cohort_size})")

        # ============ 5. Августовский PnL -- 06 по union-списку когорт ============
        def run_august(step_key: str, wallets: list[str]) -> pd.DataFrame:
            sql = render_sql(
                read_sql("06_wallet_agg_august"),
                {**test_window, "base_token_symbols": base_tokens_sql, "cohort_wallets": q_addr_list(wallets)},
            )
            return run_named(step_key, sql, expected_max_rows=2000, expected_columns=4)

        wallets_5m = pd.concat([cohort_a_5_1500, cohort_b_5_1500, cohort_a_5_3000, cohort_b_5_3000])["wallet_address"].unique().tolist()
        df_august_5m = run_august("06_wallet_agg_august_sniper5m", wallets_5m)
        print(f"  {len(df_august_5m)} из {len(wallets_5m)} кошельков (sniper=5мин) торговали в августе")

        wallets_1m = pd.concat([cohort_a_1_1500, cohort_b_1_1500, cohort_a_1_3000, cohort_b_1_3000])["wallet_address"].unique().tolist()
        df_august_1m = run_august("06_wallet_agg_august_sniper1m", wallets_1m)
        print(f"  {len(df_august_1m)} из {len(wallets_1m)} кошельков (sniper=1мин) торговали в августе")

        cohort_a_5_1500, cohort_b_5_1500 = merge_august_pnl(cohort_a_5_1500, df_august_5m), merge_august_pnl(cohort_b_5_1500, df_august_5m)
        cohort_a_5_3000, cohort_b_5_3000 = merge_august_pnl(cohort_a_5_3000, df_august_5m), merge_august_pnl(cohort_b_5_3000, df_august_5m)
        cohort_a_1_1500, cohort_b_1_1500 = merge_august_pnl(cohort_a_1_1500, df_august_1m), merge_august_pnl(cohort_b_1_1500, df_august_1m)
        cohort_a_1_3000, cohort_b_1_3000 = merge_august_pnl(cohort_a_1_3000, df_august_1m), merge_august_pnl(cohort_b_1_3000, df_august_1m)

        # ============ 6. Двухчастный тест на все 4 ячейки ============
        result_5_1500 = two_part_analysis(cohort_a_5_1500, cohort_b_5_1500, CONFIG.part1_alpha, CONFIG.part1_min_lift, CONFIG.part2_alpha)
        result_5_3000 = two_part_analysis(cohort_a_5_3000, cohort_b_5_3000, CONFIG.part1_alpha, CONFIG.part1_min_lift, CONFIG.part2_alpha)
        result_1_1500 = two_part_analysis(cohort_a_1_1500, cohort_b_1_1500, CONFIG.part1_alpha, CONFIG.part1_min_lift, CONFIG.part2_alpha)
        result_1_3000 = two_part_analysis(cohort_a_1_3000, cohort_b_1_3000, CONFIG.part1_alpha, CONFIG.part1_min_lift, CONFIG.part2_alpha)
        print(f"  ПЕРВИЧНЫЙ результат (sniper=5мин, cap=1500): {result_5_1500.verdict}")

        cells = {
            "sniper=5мин, cap=1500 (ПЕРВИЧНАЯ)": result_5_1500,
            "sniper=5мин, cap=3000": result_5_3000,
            "sniper=1мин, cap=1500": result_1_1500,
            "sniper=1мин, cap=3000": result_1_3000,
        }
        signs = {k: np.sign(r.prop_positive_a - r.prop_positive_b) if not np.isnan(r.prop_positive_a - r.prop_positive_b) else np.nan for k, r in cells.items()}
        robust = len(set(s for s in signs.values() if not np.isnan(s))) <= 1 and not any(np.isnan(s) for s in signs.values())

        # ============ 7. Сборка секций отчёта ============
        sections.append(f"{SECTION_MARKER}\n\nСгенерировано `analysis/sprint_1_5.py` (ревизия 2) в {dt.datetime.utcnow().isoformat()}Z.\n")
        sections.append(
            "### Дизайн (ревизия 2 -- см. docs/COST_POSTMORTEM.md)\n\n"
            f"- Гейт снайперов (первичный): {CONFIG.sniper_time_window_minutes} мин; sensitivity: {CONFIG.sniper_time_window_minutes_sensitivity} мин\n"
            f"- Кап копируемости (первичный): >{CONFIG.copyability_max_trades} сделок/июль исключены; sensitivity: >{CONFIG.copyability_max_trades_sensitivity}\n"
            f"- Первичный критерий (Часть 1): Fisher one-sided p<{CONFIG.part1_alpha} И лифт≥{CONFIG.part1_min_lift}x\n"
            f"- Вторичный критерий (Часть 2): медиана А>Б среди августовски-активных (p<{CONFIG.part2_alpha} — информативно, не обязательно)\n"
            "- Гейт 1/2, капы и отбор когорт (топ-200 + псевдослучайные 200 через "
            "детерминированный хэш адреса) считаются одним серверным SQL-запросом "
            "(`sql/03b_cohort_selection.sql`) -- полный построчный результат 03 "
            "(1.21M строк) через API НЕ читается ни разу.\n"
            f"- Реальная стоимость execute() 03b: {actual_03b_cost:.2f} кредитов."
        )
        sections.append(build_cap_section(df_cap))
        sections.append("### Основной результат (sniper=5мин, cap=1500 сделок) -- ПЕРВИЧНЫЙ\n\n" + two_part_md("sniper=5мин, cap=1500", result_5_1500))
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
            + f"\n\n**Знак результата Части 1 устойчив во всех 4 комбинациях: {'ДА' if robust else 'НЕТ'}**."
        )
        sections.append(build_histogram_section(df_hist))
        sections.append("### Топ-20 персистентных кошельков (очищенная когорта А, sniper=5мин, cap=1500, по августовскому PnL)\n\n" + build_top20(cohort_a_5_1500))

        final_verdict = result_5_1500.verdict
        sections.append(
            "### Итоговый вердикт Sprint 1.5\n\n"
            f"**{final_verdict}**\n\n"
            f"{result_5_1500.verdict_reasoning}\n\n"
            f"Устойчивость по sensitivity-сетке: {'подтверждена (знак не меняется)' if robust else 'НЕ подтверждена -- знак результата Части 1 меняется между ячейками, см. таблицу выше'}."
        )

        run_total = sum((row["credits"] or 0.0) for row in client.credit_ledger)
        sections.append(
            "### Стоимость Sprint 1.5 (ревизия 2)\n\n"
            f"Потрачено на execute() в этом прогоне: **{run_total:.2f}** кредитов "
            f"(execute 03b={actual_03b_cost:.2f}, потолок {STEP03B_HARD_CAP}). Полный "
            "построчный результат 03 (163.98 кредита за чтение в ревизии 1) не читался "
            "ни разу -- все чтения этого прогона ограничены тысячами строк максимум "
            "(см. data/credits_spent.json для точного леджера execute+чтений).\n\n"
            + print_ledger_md(client.credit_ledger)
        )

        write_results(sections)
        print(f"\n[sprint_1_5] Готово. Execute-стоимость этого прогона: {run_total:.2f} кредитов.")
        return 0

    except Step03bTooExpensive as e:
        sections.append(
            f"\n\n> **ОСТАНОВЛЕНО: 03b_cohort_selection стоил {e.actual:.2f} кредитов, "
            f"что выше согласованного потолка {STEP03B_HARD_CAP}.** Деньги за 03b уже "
            "потрачены (Dune не даёт предварительной оценки стоимости execute), но "
            "дальнейшие шаги (03c/03d/06×2) остановлены, чтобы не наращивать перерасход. "
            "Требуется решение пользователя перед продолжением."
        )
        write_results(sections)
        print(f"\n[sprint_1_5] {e}", file=sys.stderr)
        return 4
    except BudgetGuardStop as e:
        sections.append(
            "\n\n> **ОСТАНОВЛЕНО персистентным бюджетным гардом** (analysis/credit_guard.py) "
            "-- см. data/credits_spent.json и вывод выше для точной причины (execute или "
            "чтение результата, какая оценка и какой лимит превышен)."
        )
        write_results(sections)
        print(f"\n[sprint_1_5] Остановлено бюджетным гардом (см. вывод выше).", file=sys.stderr)
        return 3
    except (DuneCreditsExhausted, DuneRateLimited) as e:
        sections.append(f"\n\n> **ОСТАНОВЛЕНО: {e}**")
        write_results(sections)
        return 1


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
