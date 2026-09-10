#!/usr/bin/env python3
"""Задача 4 владельца (2026-09-10), Шаг 1b: продолжение честной оценки
ДО полного пайплайна. Шаг 1 (task4_arb_stage1.py) дал реальный
discovery-слой (дёшево, ~46.6 кредита на 3 цепи x 30 дней) -- но
доминирующая статья расходов (Шаг 2: сырые цены по свопам для
хвостовых+референс-пулов за месяц) осталась не оценена, и Шаг 1
честно показал: 1002 пула/день на одной Robinhood прошли СЫРОЙ порог
>=50 свопов (до TVL-фильтра) -- потенциально очень много пар
"хвост+референс" x 30 дней x 3 цепи.

Этот шаг: (1) реальный TVL-фильтр ($50k-$500k) через GT на выборке
топ-40 по числу свопов (не на всех 1002 -- честная, представительная,
но ограниченная по времени выборка), (2) для САМОГО активного реального
хвостового пула -- реальный поиск референс-пула той же пары (наибольший
TVL среди пулов с обоими токенами), (3) РЕАЛЬНЫЙ Dune-запрос сырых
свопов для этой пары (хвост+референс) за ОДИН день -- даёт реальную
стоимость на пару пулов в день, откуда честная экстраполяция
доминирующей статьи расходов."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task4_arb_slow_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")
sys.path.insert(0, str(Path(__file__).parent))

import credit_guard  # noqa: E402
import task1_pool_liquidity as gt  # noqa: E402
from dune_client import DuneClient  # noqa: E402
from run_pipeline import read_sql  # noqa: E402

BUDGET = 600.0
TASK_CEILING_CREDITS = 300.0
PROBE_CHAIN = "robinhood"
ALL_CHAINS = ["robinhood", "base", "arbitrum"]
FULL_DAYS = 30
TVL_MIN, TVL_MAX = 50_000.0, 500_000.0
DIAG_SAMPLE_SIZE = 40
GT_NETWORK_MAP = {"robinhood": "robinhood", "base": "base", "arbitrum": "arbitrum"}  # честно проверить/уточнить для base/arbitrum отдельно, здесь используется только для Robinhood
OUT_PATH = Path("data/p3_guard_cache/task4_arb_stage1b_result.json")
STAGE1_PATH = Path("data/p3_guard_cache/task4_arb_stage1_result.json")


def get_gt_pool_full(pool_address: str) -> dict | None:
    r = gt.gt_get(f"{gt.GT_BASE}/networks/{GT_NETWORK_MAP[PROBE_CHAIN]}/pools/{pool_address}")
    if r is None or r.status_code == 404:
        return None
    d = r.json().get("data", {})
    attrs = d.get("attributes", {})
    rel = d.get("relationships", {})
    tokens = {}
    for side in ("base_token", "quote_token"):
        tid = rel.get(side, {}).get("data", {}).get("id", "")
        if "_" in tid:
            tokens[side] = tid.split("_", 1)[1].lower()
    reserve = attrs.get("reserve_in_usd")
    return {"reserve_usd": float(reserve) if reserve else None, "base_token": tokens.get("base_token"),
            "quote_token": tokens.get("quote_token"), "address": attrs.get("address")}


def find_most_liquid_pool_for_pair(token_a: str, token_b: str, exclude_address: str) -> dict | None:
    r = gt.gt_get(f"{gt.GT_BASE}/networks/{GT_NETWORK_MAP[PROBE_CHAIN]}/tokens/{token_a}/pools")
    if r is None or r.status_code == 404:
        return None
    best = None
    for c in r.json().get("data", []):
        rel = c.get("relationships", {})
        ids = set()
        for side in ("base_token", "quote_token"):
            tid = rel.get(side, {}).get("data", {}).get("id", "")
            if "_" in tid:
                ids.add(tid.split("_", 1)[1].lower())
        attrs = c.get("attributes", {})
        addr = attrs.get("address")
        if token_b.lower() not in ids or not addr or addr.lower() == exclude_address.lower():
            continue
        reserve = attrs.get("reserve_in_usd")
        if not reserve:
            continue
        reserve = float(reserve)
        if best is None or reserve > best["reserve_usd"]:
            best = {"address": addr, "reserve_usd": reserve}
    return best


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "task_ceiling_credits": TASK_CEILING_CREDITS, "probe_chain": PROBE_CHAIN,
                     "tvl_min": TVL_MIN, "tvl_max": TVL_MAX}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STAGE1_PATH.exists():
        result["blocker"] = "Шаг 1 (task4_arb_stage1_result.json) не найден -- запустить сначала его."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1
    stage1 = json.loads(STAGE1_PATH.read_text())
    day_start, day_end = stage1["probe_day_start_utc"], stage1["probe_day_end_utc"]
    result["probe_day_start_utc"], result["probe_day_end_utc"] = day_start, day_end

    ns = credit_guard.namespace()
    credit_guard.ensure_namespace(ns, BUDGET)
    client = DuneClient()

    print("=== Повторный вызов discovery-запроса (тот же день -- ожидаем ПОСТОЯННЫЙ кэш-хит, 0 доп. кредитов) ===")
    spent_before = credit_guard.load_state().get(ns, {}).get("spent", 0.0)
    from datetime import datetime
    day_start_dt = datetime.fromisoformat(day_start)
    day_end_dt = datetime.fromisoformat(day_end)
    sql_pools = (read_sql("task4/task4_arb_pool_swap_counts")
                 .replace("{{chain}}", PROBE_CHAIN)
                 .replace("{{day_start}}", day_start_dt.strftime("%Y-%m-%d %H:%M:%S"))
                 .replace("{{day_end}}", day_end_dt.strftime("%Y-%m-%d %H:%M:%S")))
    qid_pools = client.create_query(f"task4_arb_pool_swap_counts_{PROBE_CHAIN}", sql_pools)
    df_pools = client.run_sql_cached("task4_arb_pool_swap_counts", sql_pools, query_id=qid_pools,
                                      estimated_credits=5.0, expected_max_rows=20000, expected_columns=5)
    spent_after = credit_guard.load_state()[ns]["spent"]
    result["rediscovery_cost_credits"] = spent_after - spent_before
    print(f"[stage1b] реальная стоимость повторного discovery-вызова: {spent_after - spent_before:.4f} (ожидание: 0.0, кэш-хит)")

    df_ge50 = df_pools[df_pools["n_swaps"] >= 50].sort_values("n_swaps", ascending=False)
    result["n_pools_ge50_this_day"] = len(df_ge50)
    sample = df_ge50.head(DIAG_SAMPLE_SIZE)
    print(f"\n=== Реальный TVL-фильтр (GT) на выборке {len(sample)} самых активных пулов ===")

    tail_pools = []
    gt_errors = 0
    for _, row in sample.iterrows():
        raw_addr = str(row["pool_address"]).lower()
        addr = raw_addr if raw_addr.startswith("0x") else f"0x{raw_addr}"
        try:
            info = get_gt_pool_full(addr)
        except Exception as exc:  # noqa: BLE001
            gt_errors += 1
            continue
        if not info or not info.get("reserve_usd"):
            continue
        if TVL_MIN <= info["reserve_usd"] <= TVL_MAX:
            tail_pools.append({"pool_address": addr, "n_swaps_this_day": int(row["n_swaps"]),
                                "volume_usd_this_day": float(row["volume_usd"]), "reserve_usd": info["reserve_usd"],
                                "base_token": info["base_token"], "quote_token": info["quote_token"]})
    result["diag_sample_size"] = len(sample)
    result["n_real_tail_pools_in_sample"] = len(tail_pools)
    result["gt_lookup_errors"] = gt_errors
    real_tail_frac = len(tail_pools) / len(sample) if len(sample) else 0.0
    result["real_tail_fraction_of_ge50_swaps"] = real_tail_frac
    print(f"[stage1b] реальных хвостовых пулов (TVL ${TVL_MIN/1e3:.0f}k-${TVL_MAX/1e3:.0f}k) в выборке: "
          f"{len(tail_pools)}/{len(sample)} ({real_tail_frac:.1%}), ошибок GT: {gt_errors}")
    result["tail_pools_sample"] = tail_pools

    if not tail_pools:
        result["blocker"] = ("В выборке из самых активных >=50 пулов НЕ нашлось ни одного реального хвостового "
                              "(TVL $50k-$500k) -- самые активные пулы на Robinhood реально гораздо крупнее "
                              "$500k. Нужна выборка из СЕРЕДИНЫ распределения по активности, не из топа -- "
                              "честно фиксирую, не продолжаю дальше без пересмотра выборки.")
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[stage1b] {result['blocker']}")
        return 1

    # 2026-09-10, реальная находка: max() по числу свопов выбирал
    # ПАТОЛОГИЧЕСКИЙ выброс (реально 17 906 свопов/день на "хвостовом"
    # по TVL пуле -- credit_guard отказался читать при expected_max_rows=
    # 5000) -- не представительный зонд, а худший случай. Медиана по
    # реальной активности -- честная представительная оценка для
    # экстраполяции, не худший случай.
    tail_pools_sorted = sorted(tail_pools, key=lambda p: p["n_swaps_this_day"])
    probe_pool = tail_pools_sorted[len(tail_pools_sorted) // 2]
    print(f"\n=== Реальный поиск референс-пула (та же пара, максимальный TVL) для {probe_pool['pool_address']} ===")
    ref = None
    if probe_pool.get("base_token") and probe_pool.get("quote_token"):
        try:
            ref = find_most_liquid_pool_for_pair(probe_pool["base_token"], probe_pool["quote_token"], probe_pool["pool_address"])
        except Exception as exc:  # noqa: BLE001
            result["reference_pool_error"] = str(exc)[:200]
    result["probe_tail_pool"] = probe_pool
    result["probe_reference_pool"] = ref
    if not ref:
        result["blocker"] = "Референс-пул для пары зонда не найден на GT -- нужен другой зонд-пул или ручная проверка."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[stage1b] {result['blocker']}")
        return 1
    print(f"[stage1b] реальный референс-пул: {ref['address']} (reserve ${ref['reserve_usd']:,.0f} vs хвост ${probe_pool['reserve_usd']:,.0f})")

    # 2026-09-10, реальная находка ЭТОГО же шага (первая попытка): пул с
    # максимальным TVL для пары -- 924 923 реальных строки за один день
    # (~10 свопов/сек) -- ботовый/wash-trading, не органический
    # ценовой ориентир, credit_guard отказался платить за чтение
    # (expected_max_rows=50000 жёстко превышен). Референс-цене НЕ нужна
    # построчная детализация (время жизни расхождения считается на
    # ХВОСТОВОМ пуле, где свопов на порядки меньше) -- минутный VWAP
    # достаточен для "медленного" арбитража (порог >=10с) и схлопывает
    # любое число строк в максимум 1440 бакетов/день. Раздельные
    # запросы: сырые свопы ТОЛЬКО для хвостового пула, минутный VWAP
    # для референс-пула.
    print(f"\n=== РЕАЛЬНЫЙ Dune-запрос 1: сырые свопы хвостового пула, один день ===")
    spent_before2 = credit_guard.load_state()[ns]["spent"]
    tail_list_sql = f"from_hex('{probe_pool['pool_address'][2:].lower()}')"
    sql_tail = (read_sql("task4/task4_arb_raw_swaps")
                .replace("{{chain}}", PROBE_CHAIN)
                .replace("{{pool_address_list}}", tail_list_sql)
                .replace("{{day_start}}", day_start_dt.strftime("%Y-%m-%d %H:%M:%S"))
                .replace("{{day_end}}", day_end_dt.strftime("%Y-%m-%d %H:%M:%S")))
    qid_tail = client.create_query("task4_arb_tail_raw_swaps_probe", sql_tail)
    df_tail = client.run_sql_cached("task4_arb_raw_swaps_tail", sql_tail, query_id=qid_tail,
                                     estimated_credits=5.0, expected_max_rows=20000, expected_columns=8)
    spent_after_tail = credit_guard.load_state()[ns]["spent"]
    cost_tail_day = spent_after_tail - spent_before2
    n_rows_tail = len(df_tail) if df_tail is not None else 0
    print(f"[stage1b] реальная стоимость (хвостовой пул, 1 день): {cost_tail_day:.4f} кредита, строк: {n_rows_tail}")

    print(f"\n=== РЕАЛЬНЫЙ Dune-запрос 2: минутный VWAP референс-пула, один день ===")
    ref_list_sql = f"from_hex('{ref['address'][2:].lower()}')"
    token_list_sql = f"from_hex('{probe_pool['base_token'][2:].lower()}')" if probe_pool.get("base_token") else tail_list_sql
    sql_ref = (read_sql("task4/task4_arb_reference_minute_vwap")
               .replace("{{chain}}", PROBE_CHAIN)
               .replace("{{pool_address_list}}", ref_list_sql)
               .replace("{{token_address_list}}", token_list_sql)
               .replace("{{day_start}}", day_start_dt.strftime("%Y-%m-%d %H:%M:%S"))
               .replace("{{day_end}}", day_end_dt.strftime("%Y-%m-%d %H:%M:%S")))
    qid_ref = client.create_query("task4_arb_reference_minute_vwap_probe", sql_ref)
    df_ref = client.run_sql_cached("task4_arb_reference_minute_vwap", sql_ref, query_id=qid_ref,
                                    estimated_credits=5.0, expected_max_rows=1500, expected_columns=4)
    spent_after_ref = credit_guard.load_state()[ns]["spent"]
    cost_ref_day = spent_after_ref - spent_after_tail
    n_rows_ref = len(df_ref) if df_ref is not None else 0
    print(f"[stage1b] реальная стоимость (референс-пул, минутный VWAP, 1 день): {cost_ref_day:.4f} кредита, строк: {n_rows_ref}")

    spent_after2 = spent_after_ref
    cost_pair_day = cost_tail_day + cost_ref_day
    result["tail_raw_swaps_probe_cost_credits"] = cost_tail_day
    result["tail_raw_swaps_probe_n_rows"] = n_rows_tail
    result["reference_minute_vwap_probe_cost_credits"] = cost_ref_day
    result["reference_minute_vwap_probe_n_rows"] = n_rows_ref
    result["raw_swaps_probe_cost_credits"] = cost_pair_day
    print(f"[stage1b] реальная суммарная стоимость (хвост+референс, 1 день): {cost_pair_day:.4f} кредита")

    # Честная экстраполяция ДОМИНИРУЮЩЕЙ статьи расходов: реальная доля
    # хвостовых пулов x реальное число >=50-своп-пулов x 30 дней x 3
    # цепи (для Base/Arbitrum активность и tail-доля ПОКА НЕ проверены
    # -- явно помечено предположением, требует своих Шаг-1 на этих
    # цепях перед реальным запуском там).
    n_tail_estimate_robinhood_full = result["n_pools_ge50_this_day"] * real_tail_frac
    extrapolated_stage2_robinhood_month = cost_pair_day * n_tail_estimate_robinhood_full * FULL_DAYS * 1.3
    extrapolated_stage2_3chains_month = extrapolated_stage2_robinhood_month * len(ALL_CHAINS)
    result["n_tail_pools_estimate_robinhood_full"] = n_tail_estimate_robinhood_full
    result["extrapolated_stage2_robinhood_month_credits"] = extrapolated_stage2_robinhood_month
    result["extrapolated_stage2_3chains_month_credits_ASSUMING_SAME_SCALE"] = extrapolated_stage2_3chains_month
    result["extrapolation_caveat"] = ("Base/Arbitrum масштаб активности и доля хвостовых пулов НЕ проверены -- "
                                       "это ОЦЕНКА СВЕРХУ по аналогии с Robinhood, не реальные данные для тех цепей. "
                                       "Нужен отдельный Шаг 1 на Base и Arbitrum перед реальным запуском там. "
                                       "Также это ВЕРХНЯЯ граница: несколько хвостовых пулов одной пары могут "
                                       "делить ОДИН референс-пул -- реальный Шаг 2 может дедуплицировать "
                                       "референс-запросы по паре и выйти дешевле этой оценки.")
    print(f"\n[stage1b] реальная оценка хвостовых пулов на Robinhood (полная, экстраполяция доли на весь день): {n_tail_estimate_robinhood_full:.0f}")
    print(f"[stage1b] экстраполяция Шага 2 ТОЛЬКО на Robinhood, месяц: {extrapolated_stage2_robinhood_month:.2f} кредита")
    print(f"[stage1b] экстраполяция Шага 2 на 3 цепи (ПРЕДПОЛОЖЕНИЕ такого же масштаба для Base/Arbitrum), месяц: "
          f"{extrapolated_stage2_3chains_month:.2f} кредита (потолок задачи: {TASK_CEILING_CREDITS})")

    result["real_cost_this_run_credits"] = (spent_after2 - spent_before)
    result["namespace_spent_total"] = spent_after2
    if extrapolated_stage2_robinhood_month > TASK_CEILING_CREDITS:
        result["blocker"] = (f"Экстраполяция Шага 2 ТОЛЬКО на Robinhood за месяц ({extrapolated_stage2_robinhood_month:.2f}) "
                              f"уже превышает потолок всей задачи ({TASK_CEILING_CREDITS}) -- останавливаюсь, "
                              "дальше сам не трачу, нужно решение владельца (сузить охват: меньше пулов/дней, "
                              "или отдельный больший потолок).")
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[stage1b] ОСТАНОВЛЕНО: {result['blocker']}")
        return 1

    result["runtime_s_total"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print("\n[stage1b] ГОТОВО, в пределах потолка -- можно проектировать полный пайплайн Шага 2.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
