#!/usr/bin/env python3
"""Sprint G1 v2 -- Шаг 2 (смоук) -> Шаг 3 (полный прогон) -> Шаг 4
(статистика/вердикт) -> Шаг 5 (отчёт), автономно одним запуском после
смоука (владелец, 2026-09-01, решение 4: "После смоука -- автономно до
конца Шага 4"). Возвратные условия (владелец): аналитическое N < 200;
перерасход >2x факт/оценка; результаты Шага 4 готовы (нормальное
завершение, не ошибка).

Решения владельца, зафиксированные в этом прогоне (НЕ меняют §2 --
мех "Механика детекции"/"Механика возвратов", см. docs/G1_DESIGN.md):
1. Граница периода НЕ расширяется за 29.08.2026 23:59:59 (config.
   g1_period_end) несмотря на видимый плотный хвост градуаций 30.08+ --
   data-dependent решение о границе выборки запрещено §2.1.
2. N в §2.7 = событий, ФАКТИЧЕСКИ вошедших в расчёт доходностей:
   пост-фильтр §2.2 МИНУС исключённые по пустому entry-окну §2.3
   ("аналитическое N"). N < 200 -> UNDERPOWERED.
3. Смоук-день = 27.08.2026 (108 градуаций -- владелец допустил 26 или
   27, выбран 27 как более плотный рабочий день без граничных эффектов
   крайнего дня периода, 29.08).
4. После смоука -- автономно к полному прогону -> статистике -> отчёту,
   без нового ручного запуска, если смоук в норме (факт <= 2x оценки,
   данные осмысленны).

Использование: python analysis/sprint_g1.py --stage smoke|full|report
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintG1")
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from config import CONFIG
from dune_client import DuneClient
from credit_guard import load_state
from g1_common import fmt_ts
from g1_pipeline import (
    load_full_v2_events, run_extract, apply_filters, compute_returns, build_quote_distribution_query,
    QUOTE_DISTRIBUTION_WINDOW_S,
)
from g1_stats import run_horizon_stats, HorizonResult

SMOKE_DAY = "2026-08-27"  # см. docstring, п.3


def print_ledger(ledger: list[dict], title: str) -> float:
    total = sum((row["credits"] or 0.0) for row in ledger)
    print(f"\n----- {title}: запрос -> кредиты -----")
    for row in ledger:
        tag = " (кэш)" if row["cached"] else ""
        cost = row["credits"] if row["credits"] is not None else "n/a"
        print(f"  {row['name']:<40} {cost}{tag}")
    print(f"  {'ИТОГО':<40} {total:.3f}")
    return total


def parse_t0(df: pd.DataFrame) -> pd.DataFrame:
    """Dune отдаёт block_time как '... UTC' -- pandas парсит это как
    tz-aware; config.g1_period_end -- наивный литерал. Нормализуем к
    наивным UTC-меткам сразу здесь (не в 10 местах ниже), иначе
    сравнение/арифметика с period_end падает (поймано dry-run'ом ДО
    первого платного запроса)."""
    df = df.copy()
    t0 = pd.to_datetime(df["t0"])
    if getattr(t0.dt, "tz", None) is not None:
        t0 = t0.dt.tz_localize(None)
    df["t0"] = t0
    return df


def analytic_n(filtered: pd.DataFrame) -> pd.DataFrame:
    return filtered[filtered["pass_filter"] & filtered["pass_entry"]]


def quote_distribution_step(client: DuneClient, events: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """Владелец, деливерабл смоука п.3: распределение quote-токенов по
    ВСЕМ 896 событиям (не только смоук-день). Возвращает (df, non_weth_flag)."""
    sql = build_quote_distribution_query(events)
    qid = client.create_query("g1_v2_quote_distribution", sql)
    # Оценка поднята с 15.0 (run #16, 56.49 факт против 15.0 -- >2x, гард
    # остановил пайплайн) после того как запрос получил границу по block_time
    # (см. build_quote_distribution_query) -- теперь окно на порядок уже
    # (48ч на событие вместо всей истории), но откалиброванных чисел под ЭТУ
    # форму запроса ещё нет, поэтому оценка с запасом, не заниженная повторно.
    df = client.run_sql_cached(
        "g1_v2_quote_distribution", sql, query_id=qid, estimated_credits=25.0,
        expected_max_rows=50, expected_columns=4,
    )
    if df is None or len(df) == 0:
        print("[quote_distribution] ПУСТО -- неожиданно (свопы уже подтверждены ранее). Продолжаю без этой заметки.")
        return pd.DataFrame(), False
    df = df.sort_values("n_tokens", ascending=False).reset_index(drop=True)
    total_tokens_seen = df["n_tokens"].sum()  # токен может встретиться в >1 строке, если торговался против >1 quote
    weth_like = df[df["quote_symbol"].astype(str).str.upper().isin(["WETH", "ETH"])]
    weth_tokens = int(weth_like["n_tokens"].sum()) if len(weth_like) else 0
    non_weth_share = 1 - (weth_tokens / total_tokens_seen) if total_tokens_seen else 0.0
    print(f"[quote_distribution] non-WETH доля (по n_tokens, токен может считаться в нескольких quote): {non_weth_share:.1%}")
    print(df.to_string(index=False))
    return df, non_weth_share > 0.10


def quote_manual_check_step(client: DuneClient, events: pd.DataFrame, quote_df: pd.DataFrame) -> pd.DataFrame | None:
    """Только если non_weth_share > 10% (владелец, п.3): выборка нескольких
    сток-квотных событий для ручной сверки USD-нормировки."""
    non_weth_symbols = [
        s for s in quote_df["quote_symbol"].astype(str).tolist()
        if s.upper() not in ("WETH", "ETH", "(NULL/UNKNOWN)")
    ][:5]
    if not non_weth_symbols:
        print("[quote_manual_check] non-WETH доля >10%, но не нашлось конкретных не-ETH символов для выборки -- пропускаю.")
        return None
    symbols_sql = ",".join("'" + s.replace("'", "''") + "'" for s in non_weth_symbols)
    tokens = sorted(events["token"].unique())
    addr_list = ", ".join(f"0x{t.removeprefix('0x')}" for t in tokens)
    # Границы по block_time ОБЯЗАТЕЛЬНЫ (см. run #16: build_quote_distribution_query
    # без такой границы просканировала всю историю dex.trades, факт 56.49 вместо
    # заявленных 15.0) -- тот же диапазон, что и quote_distribution.
    t0_min = events["t0"].min()
    t0_max_bound = pd.Timestamp(events["t0"].max()) + pd.Timedelta(seconds=QUOTE_DISTRIBUTION_WINDOW_S + 60)
    sql = f"""-- Ручная сверка USD-нормировки на сток-квотных событиях (owner, п.3)
select dt.token_bought_symbol, dt.token_sold_symbol, dt.token_bought_amount,
    dt.token_sold_amount, dt.amount_usd, dt.block_time
from dex.trades dt
where dt.blockchain = 'robinhood' and dt.version = '4'
    and (dt.token_bought_address in ({addr_list}) or dt.token_sold_address in ({addr_list}))
    and (dt.token_bought_symbol in ({symbols_sql}) or dt.token_sold_symbol in ({symbols_sql}))
    and dt.amount_usd is not null
    and dt.block_time >= timestamp '{fmt_ts(t0_min)}'
    and dt.block_time <= timestamp '{fmt_ts(t0_max_bound)}'
limit 10
"""
    qid = client.create_query("g1_v2_quote_manual_check", sql)
    df = client.run_sql_cached(
        "g1_v2_quote_manual_check", sql, query_id=qid, estimated_credits=5.0,
        expected_max_rows=10, expected_columns=6,
    )
    if df is not None and len(df):
        print("[quote_manual_check] Сэмпл сток-квотных сделок (ручная сверка амаунт_usd на разумность):")
        print(df.to_string(index=False))
    return df


def run_smoke(client: DuneClient, events: pd.DataFrame) -> tuple[bool, dict]:
    ledger_start = len(client.credit_ledger)

    quote_df, needs_manual_check = quote_distribution_step(client, events)
    declared_estimate = 25.0  # quote_distribution (см. quote_distribution_step)
    if needs_manual_check and len(quote_df):
        quote_manual_check_step(client, events, quote_df)
        declared_estimate += 5.0

    smoke_events = events[events["t0"].dt.strftime("%Y-%m-%d") == SMOKE_DAY].reset_index(drop=True)
    print(f"\n[smoke] День {SMOKE_DAY}: {len(smoke_events)} событий-кандидатов (из 896).")
    if len(smoke_events) == 0:
        print("[smoke] СТОП: на выбранный день нет событий -- расходится с посуточным агрегатом (run #13). Возврат в штаб.")
        return False, {}

    smoke_extract_estimate = 15.0
    declared_estimate += smoke_extract_estimate
    df_smoke_raw = run_extract(client, "g1_v2_smoke_extract", smoke_events, smoke_extract_estimate)
    if df_smoke_raw is None or len(df_smoke_raw) == 0:
        print("[smoke] СТОП: экстракт пуст -- неожиданно. Возврат в штаб.")
        return False, {}
    df_smoke = apply_filters(parse_t0(df_smoke_raw))

    n_pass_filter = int(df_smoke["pass_filter"].sum())
    analytic = analytic_n(df_smoke)
    n_analytic = len(analytic)
    empty_entry_share = (
        1 - (n_analytic / n_pass_filter) if n_pass_filter else float("nan")
    )
    print(
        f"[smoke] N сырых={len(df_smoke)}, пост-фильтр(§2.2)={n_pass_filter}, "
        f"аналитическое (минус пустой entry)={n_analytic} "
        f"(доля пустых entry-окон среди прошедших §2.2: {empty_entry_share:.1%})"
    )
    sanity_ok = n_pass_filter > 0 and analytic["entry_vwap"].notna().any()

    smoke_ledger = client.credit_ledger[ledger_start:]
    smoke_actual = print_ledger(smoke_ledger, "СМОУК-ТЕСТ (день {})".format(SMOKE_DAY))
    within_2x = smoke_actual <= 2 * declared_estimate

    print(
        f"\n[smoke] Факт {smoke_actual:.3f} vs заявленная оценка {declared_estimate:.1f} "
        f"(<=2x: {within_2x}); данные осмысленны: {sanity_ok}."
    )
    ok = sanity_ok and within_2x
    return ok, {
        "n_pass_filter": n_pass_filter, "n_analytic": n_analytic,
        "empty_entry_share": empty_entry_share, "actual_credits": smoke_actual,
        "declared_estimate": declared_estimate, "quote_df": quote_df,
    }


def compute_stats_bundle(filtered: pd.DataFrame) -> dict:
    """filtered: df с pass_filter/pass_entry/entry_vwap/exit_*/t0. Считает
    базовую статистику (§2.6) + робастность (a/b/c) + вердикт (§2.7)."""
    horizons = CONFIG.g1_horizons_s

    returns_base = compute_returns(filtered, CONFIG.g1_cost_scenario_base, horizons)
    results_base = run_horizon_stats(
        returns_base, CONFIG.g1_bh_alpha, CONFIG.g1_bootstrap_n, CONFIG.g1_trimmed_mean_pct,
    )

    # --- (a) календарные половины: реальный период выборки -- ОТ ПЕРВОЙ
    # v2-градуации (§2.1: "или с первой градуации, если она позже 01.07"),
    # НЕ 01.07 -- первая v2-градуация 04.08.2026, что позже. ---
    period_start = filtered["t0"].min()
    period_end = pd.Timestamp(CONFIG.g1_period_end)
    midpoint = period_start + (period_end - period_start) / 2
    half1 = filtered[filtered["t0"] < midpoint]
    half2 = filtered[filtered["t0"] >= midpoint]
    half1_analytic = analytic_n(half1)
    half2_analytic = analytic_n(half2)
    returns_half1 = compute_returns(half1, CONFIG.g1_cost_scenario_base, horizons)
    returns_half2 = compute_returns(half2, CONFIG.g1_cost_scenario_base, horizons)
    half_medians = {
        h: (
            float(np.median(returns_half1[h])) if len(returns_half1[h]) else float("nan"),
            float(np.median(returns_half2[h])) if len(returns_half2[h]) else float("nan"),
        )
        for h in horizons
    }

    # --- (b) сценарии стоимости 1%/5% ---
    cost_scenario_medians: dict[float, dict[int, float]] = {}
    for c in CONFIG.g1_cost_scenarios:
        r = compute_returns(filtered, c, horizons)
        cost_scenario_medians[c] = {h: (float(np.median(r[h])) if len(r[h]) else float("nan")) for h in horizons}

    # --- (c) стресс-сценарий S ---
    returns_stress = compute_returns(filtered, CONFIG.g1_cost_scenario_base, horizons, stress=True)
    stress_medians = {h: (float(np.median(returns_stress[h])) if len(returns_stress[h]) else float("nan")) for h in horizons}

    # --- Вердикт §2.7 (заморожен, только применение к базовой стоимости) ---
    candidates = []
    for r in results_base:
        if r.horizon_s < CONFIG.g1_go_min_horizon_s or not r.significant:
            continue
        if r.median < CONFIG.g1_go_min_median_pct:
            continue
        m1, m2 = half_medians[r.horizon_s]
        if not (m1 > 0 and m2 > 0):
            continue
        candidates.append(r)
    any_significant = any(r.significant for r in results_base)
    if candidates:
        best = candidates[0]
        verdict = "GO"
        reasoning = (
            f"h*={best.horizon_s}с: BH-q={best.q_bh:.4f} < 0.05, медиана нетто={best.median:.4f} "
            f">= +{CONFIG.g1_go_min_median_pct}, обе половины периода положительны "
            f"({half_medians[best.horizon_s][0]:.4f} / {half_medians[best.horizon_s][1]:.4f})."
        )
    elif not any_significant:
        verdict = "KILL"
        reasoning = "Ни один горизонт не значим по BH (q>=0.05) при базовой стоимости 3% -- линия graduation-momentum закрывается, ретест в этой формулировке запрещён (§2.7)."
    else:
        verdict = "GRAY"
        reasoning = "Есть значимость на каком-то горизонте, но нарушено одно из остальных условий GO (медиана<+2%, провал одной из календарных половин, или значимость только на h<1мин) -- решение владельца, дефолт закрыть (§2.7)."

    return {
        "results_base": results_base,
        "period_start": period_start, "period_end": period_end, "midpoint": midpoint,
        "half1_n_raw": len(half1), "half1_n_pass_filter": int(half1["pass_filter"].sum()), "half1_n_analytic": len(half1_analytic),
        "half2_n_raw": len(half2), "half2_n_pass_filter": int(half2["pass_filter"].sum()), "half2_n_analytic": len(half2_analytic),
        "half_medians": half_medians,
        "cost_scenario_medians": cost_scenario_medians,
        "stress_medians": stress_medians,
        "verdict": verdict, "reasoning": reasoning,
    }


def run_full(client: DuneClient, events: pd.DataFrame) -> tuple[int, dict | None]:
    ledger_start = len(client.credit_ledger)
    full_estimate = 35.0
    df_full_raw = run_extract(client, "g1_v2_full_extract", events, full_estimate)
    if df_full_raw is None or len(df_full_raw) == 0:
        print("[full] СТОП: полный экстракт пуст -- неожиданно. Возврат в штаб.")
        return 1, None
    filtered = apply_filters(parse_t0(df_full_raw))

    n_raw = len(filtered)
    n_pass_filter = int(filtered["pass_filter"].sum())
    analytic = analytic_n(filtered)
    n_analytic = len(analytic)
    excluded_entry_share = 1 - (n_analytic / n_pass_filter) if n_pass_filter else float("nan")

    print(
        f"\n[full] N сырых(дедуп по token)={n_raw}, пост-фильтр §2.2={n_pass_filter}, "
        f"АНАЛИТИЧЕСКОЕ N (пост-фильтр минус пустой entry, §2.3, владелец п.2) = {n_analytic} "
        f"(доля исключённых по пустому entry: {excluded_entry_share:.1%})"
    )
    if excluded_entry_share > CONFIG.g1_excluded_events_max_share:
        print(
            f"[full] ПРИМЕЧАНИЕ: доля исключённых по пустому entry-окну ({excluded_entry_share:.1%}) "
            f"> {CONFIG.g1_excluded_events_max_share:.0%} -- фиксируется как ограничение выборки (§2.3), не блокирует."
        )

    full_ledger = client.credit_ledger[ledger_start:]
    full_actual = print_ledger(full_ledger, "ПОЛНЫЙ ПРОГОН")

    if n_analytic < CONFIG.g1_min_n_events:
        print(
            f"\n[full] АНАЛИТИЧЕСКОЕ N = {n_analytic} < {CONFIG.g1_min_n_events} -- UNDERPOWERED. "
            "Вердикт НЕ выносится (§2.7). Возврат в штаб -- решение владельца (продлить период / закрыть)."
        )
        return 3, {"filtered": filtered, "n_raw": n_raw, "n_pass_filter": n_pass_filter, "n_analytic": n_analytic, "full_actual": full_actual, "underpowered": True}

    print(f"\n[full] АНАЛИТИЧЕСКОЕ N = {n_analytic} >= {CONFIG.g1_min_n_events} -- ГЕЙТ ПРОЙДЕН, считаю статистику §2.6.")
    stats_bundle = compute_stats_bundle(filtered)
    stats_bundle.update({
        "filtered": filtered, "n_raw": n_raw, "n_pass_filter": n_pass_filter, "n_analytic": n_analytic,
        "excluded_entry_share": excluded_entry_share, "full_actual": full_actual, "underpowered": False,
    })
    print(f"\n[full] ВЕРДИКТ: {stats_bundle['verdict']}\n{stats_bundle['reasoning']}")
    return 0, stats_bundle


def fmt_pct(x: float) -> str:
    return "n/a" if (x is None or (isinstance(x, float) and np.isnan(x))) else f"{x*100:.2f}%"


def fmt4(x: float) -> str:
    return "n/a" if (x is None or (isinstance(x, float) and np.isnan(x))) else f"{x:.4f}"


def render_horizon_table(results: list[HorizonResult]) -> str:
    lines = ["| h | N | n+ | медиана(лог) | p(sign) | BH-q | значим? | усечён.среднее | 95% CI |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(
            f"| {r.horizon_s}с | {r.n} | {r.n_pos} | {fmt4(r.median)} | {fmt4(r.p_one_sided)} | "
            f"{fmt4(r.q_bh)} | {'ДА' if r.significant else 'нет'} | {fmt4(r.trimmed_mean)} | "
            f"[{fmt4(r.boot_ci_low)}; {fmt4(r.boot_ci_high)}] |"
        )
    return "\n".join(lines)


def render_half_table(half_medians: dict[int, tuple[float, float]]) -> str:
    lines = ["| h | медиана 1-я половина | медиана 2-я половина | обе > 0? |", "|---|---|---|---|"]
    for h, (m1, m2) in half_medians.items():
        both_pos = (not np.isnan(m1)) and (not np.isnan(m2)) and m1 > 0 and m2 > 0
        lines.append(f"| {h}с | {fmt4(m1)} | {fmt4(m2)} | {'да' if both_pos else 'нет'} |")
    return "\n".join(lines)


def render_cost_scenario_table(cost_scenario_medians: dict[float, dict[int, float]]) -> str:
    horizons = CONFIG.g1_horizons_s
    header = "| сценарий c | " + " | ".join(f"{h}с" for h in horizons) + " |"
    sep = "|---|" + "---|" * len(horizons)
    lines = [header, sep]
    for c, meds in sorted(cost_scenario_medians.items()):
        row = f"| {c*100:.0f}% | " + " | ".join(fmt4(meds[h]) for h in horizons) + " |"
        lines.append(row)
    return "\n".join(lines)


def render_stress_table(stress_medians: dict[int, float]) -> str:
    horizons = CONFIG.g1_horizons_s
    header = "| горизонт | " + " | ".join(f"{h}с" for h in horizons) + " |"
    sep = "|---|" + "---|" * len(horizons)
    row = "| медиана (стресс S) | " + " | ".join(fmt4(stress_medians[h]) for h in horizons) + " |"
    return "\n".join([header, sep, row])


def write_design_addendum(smoke_info: dict, full_info: dict) -> None:
    design_path = Path(CONFIG.g1_design_doc)
    text = design_path.read_text()
    marker = "## Механика выгрузки доходностей v2 (§2.3, владелец, 2026-09-01)"
    if marker in text:
        print(f"[sprint_g1] {design_path} уже содержит секцию -- не дублирую.")
        return
    note = f"""

{marker}

**Метод (мех, не критерий):** один SQL-проход на набор событий --
JOIN dex.trades по адресу ТОКЕНА (не пула, v4 не гранулярен по пулу в
dex.trades) в общем окне (t0; t0+{86400+8640}с], дальше все пре-/entry-/exit-
агрегаты (§2.2/§2.3) считаются `filter (where ...)` в ОДНОМ проходе --
наружу уходит один агрегат на событие (token, t0, n_buys_pre, vol_usd_pre,
entry_n, entry_vwap, exit_n_h/exit_vwap_h x10 горизонтов), не сырые
свопы (правило "сырые данные не покидают Dune"). Exit(h) без сделок в
окне -> LOCF (цена последней сделки <= t0+h+delta) через `max_by(price,
block_time) filter (...)`, согласно §2.3 буквально.

**N в §2.7 (владелец, staff decision, применение §2.2, НЕ изменение
критерия):** N = событий, ФАКТИЧЕСКИ вошедших в расчёт доходностей --
пост-фильтр §2.2 МИНУС события с пустым entry-окном (t0+30с; t0+90с]
(§2.3, "нет сделок в окне -> событие исключается как неисполнимое").

**Смоук-день:** {SMOKE_DAY} (владелец допустил 26 или 27.08 -- выбран
27, более плотный рабочий день, 108 градуаций, без граничных эффектов
последнего дня периода 29.08). Смоук: N пост-фильтр={smoke_info.get('n_pass_filter')},
N аналитическое={smoke_info.get('n_analytic')}, доля пустых entry-окон
={fmt_pct(smoke_info.get('empty_entry_share'))}, факт кредитов
={smoke_info.get('actual_credits', 0):.3f} vs заявленная оценка
{smoke_info.get('declared_estimate', 0):.1f} -- в норме, продолжено
автономно к полному прогону (владелец, решение 4).

**Распределение quote-токенов (все 896 событий):**
{smoke_info.get('quote_df').to_string(index=False) if isinstance(smoke_info.get('quote_df'), pd.DataFrame) and len(smoke_info.get('quote_df')) else '(не получено)'}

**Граница периода НЕ расширена** за 29.08.2026 23:59:59 (config.
g1_period_end) несмотря на видимый плотный хвост градуаций после
30.08 -- data-dependent решение о границе выборки запрещено §2.1
(владелец, решение 1).

**Эффективное начало периода выборки:** 2026-08-04 (дата первой
v2-градуации) -- по §2.1 буквально ("с 01.07.2026, или с первой
градуации, если она позже"), не изменение критерия.

**Не реализовано в этом проходе (§2.5/2.8, эксплораторно, НЕ влияет на
вердикт):** n_scored_wallets_pre / n_hunters100_pre -- нет готового
кэша скоринга факт-прибыльности Sprint 1/1.5, join не строился (не
было ни одного такого кэша в репозитории на момент проверки); режим
рынка (в) -- не запрашивался владельцем в этом проходе, не считался
(дешёвый агрегат, доступен как отдельный follow-up). Fee bps как
колонка per-token -- не добавлена (владелец: только если уже в кэше,
новых запросов не делать; per-token override не подтверждён в
исходниках, есть только глобальные константы хука, уже отражённые в
секции "Перенацеливание на Pons V2" выше).
"""
    design_path.write_text(text + note)
    print(f"[sprint_g1] {design_path} обновлён.")


def write_results_md(full_info: dict, smoke_info: dict) -> None:
    results_path = Path(CONFIG.results_doc)
    text = results_path.read_text() if results_path.exists() else ""
    marker = "## Sprint G1 v2 -- graduation-momentum (Pons V2, PoolGraduated)"
    if marker in text:
        print(f"[sprint_g1] {results_path} уже содержит секцию Sprint G1 -- не дублирую (перезапуск с --stage report не меняет числа).")
        return

    if full_info.get("underpowered"):
        body = f"""

{marker}

**UNDERPOWERED.** Аналитическое N = {full_info['n_analytic']} < {CONFIG.g1_min_n_events}
(§2.7). Вердикт не выносится -- решение владельца (продлить период
выборки / закрыть). N сырых={full_info['n_raw']}, пост-фильтр §2.2=
{full_info['n_pass_filter']}. Фактическая стоимость полного прогона:
{full_info['full_actual']:.3f} кредитов.
"""
        results_path.write_text(text + body)
        print(f"[sprint_g1] {results_path} обновлён (UNDERPOWERED).")
        return

    horizons_table = render_horizon_table(full_info["results_base"])
    half_table = render_half_table(full_info["half_medians"])
    cost_table = render_cost_scenario_table(full_info["cost_scenario_medians"])
    stress_table = render_stress_table(full_info["stress_medians"])

    body = f"""

{marker}

**Пре-регистрация:** `docs/G1_DESIGN.md`, §2 (заморожен 2026-09-01).
Механика детекции/выгрузки -- см. `docs/G1_DESIGN.md`, секции
"Перенацеливание на Pons V2" и "Механика выгрузки доходностей v2".

### Выборка

- Событие градуации: `PoolGraduated` на V2-фабрике `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e`.
- Период: {full_info['period_start']} -> {full_info['period_end']} (эффективное начало -- первая
  v2-градуация, конец -- зафиксирован на Шаге 1, НЕ расширен несмотря на видимый хвост после 30.08).
- N сырых (дедуп по token): {full_info['n_raw']}
- N пост-фильтр §2.2 (buy vol>=$250 И >=3 сделки в (t0;t0+30с]): {full_info['n_pass_filter']}
- N аналитическое (пост-фильтр минус пустой entry-окно §2.3, определение N в §2.7 -- владелец):
  **{full_info['n_analytic']}** (доля исключённых по пустому entry: {fmt_pct(full_info['excluded_entry_share'])})
- N по календарным половинам периода: 1-я половина -- сырых={full_info['half1_n_raw']},
  пост-фильтр={full_info['half1_n_pass_filter']}, аналитическое={full_info['half1_n_analytic']};
  2-я половина -- сырых={full_info['half2_n_raw']}, пост-фильтр={full_info['half2_n_pass_filter']},
  аналитическое={full_info['half2_n_analytic']}.
- Гейт N>={CONFIG.g1_min_n_events} (§2.1/2.7) по аналитическому N: **ПРОЙДЕН**.

### §2.6 Основной тест (базовая стоимость c={CONFIG.g1_cost_scenario_base:.0%}, знаковый тест + BH по {len(CONFIG.g1_horizons_s)} горизонтам, alpha={CONFIG.g1_bh_alpha})

{horizons_table}

### Робастность (а): медианы по календарным половинам периода (базовая стоимость)

{half_table}

### Робастность (б): медианы при сценариях стоимости 1%/3%/5%

{cost_table}

### Робастность (в): стресс-сценарий S (no_exit_liquidity_h -> r=sentinel, см. `analysis/g1_pipeline.py`)

{stress_table}

### Вердикт (§2.7, заморожен)

**{full_info['verdict']}**

{full_info['reasoning']}

### Ограничения (§2.9 + найденные в этом прогоне)

- Налоги токенов/ханипоты, MEV/сэндвич -- не полностью учтены в ретро-издержках (частично покрыты сценарием 5%).
- VWAP != гарантированно исполнимая цена на тонкой ликвидности, особенно для событий с LOCF-фолбэком exit-цены (нет сделок в окне).
- exit-VWAP считается по свопам в v4 hook-пуле (единый PoolManager-контракт, не гранулярен по пулу в dex.trades -- сверка по адресу токена).
- Quote-токен не всегда WETH -- нормировка в USD идёт через курируемый amount_usd Dune, не пересчитывается вручную; распределение quote-токенов -- см. `docs/G1_DESIGN.md`.
- n_scored_wallets_pre / n_hunters100_pre (§2.5/2.8, эксплораторные) -- НЕ реализованы в этом проходе (нет готового скоринг-кэша Sprint 1/1.5), не влияет на вердикт (§2.8).
- Режим рынка (§2.8в) -- не запрашивался, не считался в этом проходе.
- 90-дневный gas waiver сети (до ~29.09.2026) -- вне периода выборки, но существен для решения о live-стратегии (см. `docs/G1_DESIGN.md`).

### Кредитный леджер (смоук + полный прогон)

Смоук ({SMOKE_DAY}): {smoke_info.get('actual_credits', 0):.3f} кредитов.
Полный прогон (896 событий): {full_info['full_actual']:.3f} кредитов.
"""
    results_path.write_text(text + body)
    print(f"[sprint_g1] {results_path} обновлён.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["smoke", "full", "report"], required=True)
    args = ap.parse_args()

    if not CONFIG.dune_api_key:
        print("ОШИБКА: DUNE_API_KEY не задан.", file=sys.stderr)
        return 1

    client = DuneClient()
    events = load_full_v2_events(client)
    print(f"[sprint_g1] Загружено {len(events)} v2-градуаций (кэш g1_v2_graduation_full, 0 кредитов).")

    if args.stage == "report":
        print("[sprint_g1] --stage report вызывается отдельно только если docs/* уже содержат готовые числа "
              "(идемпотентный writer пропустит, если секция уже есть). Полноценный расчёт делает --stage smoke.")
        return 0

    # BudgetGuardStop (перерасход >2x/бюджет) намеренно не перехватывается --
    # это жёсткий стоп самого гарда (см. credit_guard.py), пробрасывается
    # наружу как есть, с уже полным докладом в своём сообщении.
    smoke_ok, smoke_info = run_smoke(client, events)
    if not smoke_ok:
        print("\n[sprint_g1] СМОУК НЕ ПРОШЁЛ -- ждём решения владельца. Полный прогон НЕ запущен.")
        return 3

    print("\n[sprint_g1] Смоук в норме -- продолжаю автономно к полному прогону (владелец, решение 4).")
    rc, full_info = run_full(client, events)
    if full_info is None:
        return rc

    write_design_addendum(smoke_info, full_info)
    write_results_md(full_info, smoke_info)

    state = load_state()
    ns_spent = state.get("sprintG1", {}).get("spent")
    print(f"\n[sprint_g1] ГОТОВО. sprintG1 потрачено всего: {ns_spent}.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
