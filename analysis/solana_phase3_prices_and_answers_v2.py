#!/usr/bin/env python3
"""Владелец, 2026-09-19: Фаза 3 v2 -- по инструкции владельца после
провала полного прогона (батч 500 событий = 339 кредитов и упал; батч
20 событий сразу упал по лимиту датапоинтов биллинг-цикла -- см.
data/solana_phase3_result.json). НЕ дispatch'ится, пока владелец не
подтвердит явно снятие лимита Dune.

Требует РЕЗУЛЬТАТ Фазы 2 v2 (data/solana_phase2_events_v2.json,
многоминтовый фикс -- solana_phase2_reclassify_v2.py) -- запускается
ПОСЛЕ него, не на старых (заниженных) частотах.

Два изменения по прямой инструкции владельца:
  1. Выборка вместо всех событий -- лидер: все k=1 и k=2 (не выборка);
     остальные: 800 случайных k=1 со стратификацией по бакетам размера
     (мин. 60/бакет), 300 k=2, 150 k=3+; плюс до 30 k=1 на кошелёк для
     топ-30 по частоте (для ранга) -- сверх основной выборки, не
     задвоенно (пересечение с основной выборкой не считается дважды).
  2. Рост считается ВНУТРИ SQL (max_by(...) FILTER(...) -- Trino/Presto
     аггрегат "значение при максимальном времени, удовлетворяющем
     условию", классический greatest-n-per-group без построчной выдачи
     сделок) -- на событие возвращается СТРОГО 5 чисел (цена до входа,
     +5/+30/+60/+180с), а не список сделок. Это резко сокращает объём
     выгружаемого результата (там, где раньше был явный риск/факт
     срабатывания лимита датапоинтов на чтение).

Сначала батч из 20 событий -- честно измеряем кредиты/событие, даём
прогноз на всю выборку, и ТОЛЬКО если прогноз укладывается в потолок
(150 кредитов на прогон, тот же что уже стоит) -- продолжаем."""
from __future__ import annotations

import json
import os
import random
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
EVENTS_PATH = REPO_ROOT / "data" / "solana_phase2_events_v2.json"
OUT_PATH = REPO_ROOT / "data" / "solana_phase3_result_v2.json"

WSOL_MINT = "So11111111111111111111111111111111111111112"
LOOKBACK_S = 300
HORIZONS = [("plus5", 5), ("plus30", 30), ("plus60", 60), ("plus180", 180)]
MAX_HORIZON_S = 180
FETCH_HI_BUFFER_S = 5
BATCH_SIZE = 20  # первый тестовый батч -- см. докстринг
MAX_TOTAL_COST_CREDITS = 150.0
FATAL_ERROR_MARKERS = ("RESOURCES_CAP_REACHED", "exceed your configured", "datapoint limit")
ROUND_TRIP_COST_FRAC = 0.06
SIZE_BUCKETS = [("2-4.3", 2, 4.3), ("4.3-8", 4.3, 8), ("8-15", 8, 15), ("15+", 15, float("inf"))]
SEED = 20260919  # воспроизводимая случайная выборка


def size_bucket(spend: float) -> str | None:
    for name, lo, hi in SIZE_BUCKETS:
        if lo <= spend < hi:
            return name
    return None


def build_sample(events_by_wallet: dict, wallet_roles: dict) -> list[dict]:
    rng = random.Random(SEED)
    leader = wallet_roles["leader"][0]
    candidates_29 = set(wallet_roles["candidates_29"])

    flat = []
    eid = 0
    for wallet, events in events_by_wallet.items():
        for e in events:
            flat.append({**e, "wallet": wallet, "event_id": eid})
            eid += 1

    leader_evs = [e for e in flat if e["wallet"] == leader and e["k"] in (1, 2)]

    other_evs = [e for e in flat if e["wallet"] != leader]
    k1 = [e for e in other_evs if e["k"] == 1]
    k2 = [e for e in other_evs if e["k"] == 2]
    k3p = [e for e in other_evs if e["k"] >= 3]

    by_bucket: dict[str, list] = defaultdict(list)
    for e in k1:
        b = size_bucket(e["spend_sol_equiv"])
        if b:
            by_bucket[b].append(e)
    target_per_bucket = 800 // len(SIZE_BUCKETS)
    k1_sample = []
    for name, _, _ in SIZE_BUCKETS:
        pool = by_bucket.get(name, [])
        n_take = max(60, target_per_bucket) if len(pool) >= max(60, target_per_bucket) else len(pool)
        k1_sample.extend(rng.sample(pool, n_take) if len(pool) >= n_take else pool)

    k2_sample = rng.sample(k2, min(300, len(k2)))
    k3p_sample = rng.sample(k3p, min(150, len(k3p)))

    # топ-30 новых кошельков по частоте (freq_ge2/day, k=1, исключая лидера и 29 кандидатов)
    by_wallet_new: dict[str, list] = defaultdict(list)
    for e in other_evs:
        if e["wallet"] not in candidates_29:
            by_wallet_new[e["wallet"]].append(e)
    freqs = []
    for w, evs in by_wallet_new.items():
        fe = [e for e in evs if e["k"] == 1]
        n_ge2 = sum(1 for e in fe if e["spend_sol_equiv"] >= 2)
        freqs.append((w, n_ge2 / 7.0))
    freqs.sort(key=lambda x: -x[1])
    top30_wallets = {w for w, _ in freqs[:30]}
    top30_extra = []
    for w in top30_wallets:
        wk1 = [e for e in by_wallet_new[w] if e["k"] == 1]
        top30_extra.extend(wk1[:30])

    seen_ids = set()
    sample = []
    for e in leader_evs + k1_sample + k2_sample + k3p_sample + top30_extra:
        if e["event_id"] not in seen_ids:
            seen_ids.add(e["event_id"])
            sample.append(e)
    return sample


def build_price_sql(batch: list[dict]) -> str:
    values = ",".join(
        f"({e['event_id']}, '{e['mint']}', {e['block_time_epoch'] - LOOKBACK_S}, "
        f"{e['block_time_epoch'] + MAX_HORIZON_S + FETCH_HI_BUFFER_S}, {e['block_time_epoch']})"
        for e in batch
    )
    select_cols = ("dt.block_time, dt.token_bought_mint_address AS bought_mint, "
                   "dt.token_sold_mint_address AS sold_mint, dt.token_bought_amount AS bought_amount, "
                   "dt.token_sold_amount AS sold_amount, e.event_id, e.t0")
    matched = (
        f"WITH ev(event_id, mint, lo, hi, t0) AS (VALUES {values}), "
        "raw AS ("
        f"SELECT {select_cols} FROM dex_solana.trades dt JOIN ev e "
        f"ON dt.token_bought_mint_address = e.mint AND dt.token_sold_mint_address = '{WSOL_MINT}' "
        "AND dt.block_time BETWEEN from_unixtime(e.lo) AND from_unixtime(e.hi) "
        "UNION ALL "
        f"SELECT {select_cols} FROM dex_solana.trades dt JOIN ev e "
        f"ON dt.token_sold_mint_address = e.mint AND dt.token_bought_mint_address = '{WSOL_MINT}' "
        "AND dt.block_time BETWEEN from_unixtime(e.lo) AND from_unixtime(e.hi)"
        "), priced AS ("
        f"SELECT event_id, t0, block_time, "
        f"CASE WHEN bought_mint = '{WSOL_MINT}' THEN try_cast(bought_amount AS double) / NULLIF(try_cast(sold_amount AS double), 0) "
        f"ELSE try_cast(sold_amount AS double) / NULLIF(try_cast(bought_amount AS double), 0) END AS price "
        "FROM raw)"
    )
    return (
        matched + " "
        "SELECT event_id, "
        "max_by(price, block_time) FILTER (WHERE block_time < from_unixtime(t0)) AS price_before, "
        "max_by(price, block_time) FILTER (WHERE block_time <= from_unixtime(t0+5)) AS price_plus5, "
        "max_by(price, block_time) FILTER (WHERE block_time <= from_unixtime(t0+30)) AS price_plus30, "
        "max_by(price, block_time) FILTER (WHERE block_time <= from_unixtime(t0+60)) AS price_plus60, "
        "max_by(price, block_time) FILTER (WHERE block_time <= from_unixtime(t0+180)) AS price_plus180 "
        "FROM priced GROUP BY event_id"
    )


def growth(prices: dict, horizon: str):
    base, val = prices.get("price_plus5"), prices.get(horizon)
    if not base or not val or base <= 0:
        return None
    return val / base - 1.0


def build_answer_tables(sample: list[dict], prices_by_event: dict, wallet_roles: dict) -> dict:
    leader = wallet_roles["leader"][0]

    def bucket_k(k):
        return "k1" if k == 1 else ("k2" if k == 2 else "k3plus")

    rebuys = {"leader": {}, "others": {}}
    for group_name, is_leader in (("leader", True), ("others", False)):
        by_k = {"k1": [], "k2": [], "k3plus": []}
        for e in sample:
            if (e["wallet"] == leader) != is_leader:
                continue
            p = prices_by_event.get(e["event_id"])
            if p:
                by_k[bucket_k(e["k"])].append(e)
        for kname, evs in by_k.items():
            gs = {h: [x for x in (growth(prices_by_event[e["event_id"]], h) for e in evs) if x is not None]
                  for h in ("price_plus30", "price_plus60", "price_plus180")}
            rebuys[group_name][kname] = {"n": len(evs),
                                          **{f"n_priced_{h.replace('price_', '')}": len(v) for h, v in gs.items()},
                                          **{f"median_growth_{h.replace('price_', '')}": (median(v) if v else None) for h, v in gs.items()}}
        k2 = rebuys[group_name]["k2"]
        k2["take_k2_rule"] = (k2.get("median_growth_plus30") is not None and k2["n_priced_plus30"] >= 30
                               and (k2["median_growth_plus30"] - ROUND_TRIP_COST_FRAC) > 0)

    threshold_table = []
    for name, lo, hi in SIZE_BUCKETS:
        evs = [e for e in sample if e["k"] == 1 and e["wallet"] != leader and lo <= e["spend_sol_equiv"] < hi
               and e["event_id"] in prices_by_event]
        g30 = [x for x in (growth(prices_by_event[e["event_id"]], "price_plus30") for e in evs) if x is not None]
        g60 = [x for x in (growth(prices_by_event[e["event_id"]], "price_plus60") for e in evs) if x is not None]
        threshold_table.append({"bucket_sol": name, "n": len(evs), "n_priced_30s": len(g30), "n_priced_60s": len(g60),
                                 "median_growth_30s": median(g30) if g30 else None,
                                 "median_growth_60s": median(g60) if g60 else None})
    threshold_sol = next((r["bucket_sol"] for r in threshold_table
                          if r["median_growth_30s"] is not None and r["median_growth_30s"] >= 2 * ROUND_TRIP_COST_FRAC), None)

    by_wallet_new: dict[str, list] = defaultdict(list)
    candidates_29 = set(wallet_roles["candidates_29"])
    for e in sample:
        if e["wallet"] != leader and e["wallet"] not in candidates_29:
            by_wallet_new[e["wallet"]].append(e)
    wallet_rows = []
    for w, evs in by_wallet_new.items():
        fe = [e for e in evs if e["k"] == 1]
        n_ge2 = sum(1 for e in fe if e["spend_sol_equiv"] >= 2)
        g30 = [x for x in (growth(prices_by_event[e["event_id"]], "price_plus30") for e in fe if e["event_id"] in prices_by_event) if x is not None]
        row = {"address": w, "freq_ge2_per_day": round(n_ge2 / 7.0, 3), "n_priced_30s": len(g30),
               "median_growth_30s": median(g30) if g30 else None}
        row["rank_score"] = (row["freq_ge2_per_day"] * row["median_growth_30s"]
                              if len(g30) >= 20 and row["median_growth_30s"] is not None else None)
        wallet_rows.append(row)
    ranked = sorted([r for r in wallet_rows if r["rank_score"] is not None], key=lambda r: -r["rank_score"])

    return {"q1_rebuys": rebuys,
            "q2_threshold_and_size": {"table": threshold_table, "threshold_sol_lower_bound": threshold_sol},
            "q3_new_wallets": {"n_wallets_evaluated": len(wallet_rows), "top10": ranked[:10]}}


def main() -> None:
    d = json.loads(EVENTS_PATH.read_text())
    sample = build_sample(d["events_by_wallet"], d["wallet_roles"])
    events_by_id = {e["event_id"]: e for e in sample}

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery, "n_events_sampled": len(sample)}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_JANA_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    prices_by_event: dict[int, dict] = {}
    batch_meta, running_cost = [], 0.0
    n_batches = (len(sample) + BATCH_SIZE - 1) // BATCH_SIZE
    projected_reported = False
    for bi in range(n_batches):
        batch = sample[bi * BATCH_SIZE:(bi + 1) * BATCH_SIZE]
        sql = build_price_sql(batch)
        r = probe.run_sql_sync(f"phase3v2_batch_{bi}", sql, timeout_s=600)
        meta = {k: v for k, v in r.items() if k != "rows"}
        batch_meta.append(meta)
        batch_cost = (meta.get("status_meta") or {}).get("execution_cost_credits", 0) or 0
        running_cost += batch_cost
        print(f"[phase3v2] батч {bi+1}/{n_batches}: status={r.get('status')} n_rows={r.get('n_rows')} "
              f"credits={batch_cost} running_total={running_cost:.2f}", flush=True)

        if bi == 0 and batch_cost:
            per_event = batch_cost / len(batch)
            projected_total = per_event * len(sample)
            result["cost_per_event_from_first_batch"] = per_event
            result["projected_total_cost_credits"] = projected_total
            projected_reported = True
            print(f"[phase3v2] прогноз: {per_event:.4f} кредита/событие x {len(sample)} событий = "
                  f"{projected_total:.2f} кредитов (потолок {MAX_TOTAL_COST_CREDITS})", flush=True)
            if projected_total > MAX_TOTAL_COST_CREDITS:
                result["batches"] = batch_meta
                result["FINAL_ANSWER"] = (f"ОСТАНОВЛЕНО после батча 0 -- прогноз стоимости ({projected_total:.2f}) "
                                           f"превышает потолок {MAX_TOTAL_COST_CREDITS}. Нужно решение владельца "
                                           f"(поднять потолок или уменьшить выборку).")
                OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                print("[phase3v2] " + result["FINAL_ANSWER"], flush=True)
                return

        err_text = json.dumps(meta, default=str)
        is_fatal = any(m in err_text for m in FATAL_ERROR_MARKERS)
        if r.get("status") != "ok":
            result["batches"] = batch_meta
            result["total_cost_credits_so_far"] = running_cost
            result["FINAL_ANSWER"] = (f"ОСТАНОВЛЕНО на батче {bi} ({'внешний блокер Dune' if is_fatal else r.get('status')})."
                                       f" Потрачено: {running_cost:.2f} кредитов.")
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print("[phase3v2] " + result["FINAL_ANSWER"], flush=True)
            if is_fatal:
                break
            continue
        if running_cost > MAX_TOTAL_COST_CREDITS:
            result["batches"] = batch_meta
            result["total_cost_credits_so_far"] = running_cost
            result["FINAL_ANSWER"] = f"ОСТАНОВЛЕНО -- потолок {MAX_TOTAL_COST_CREDITS} кредитов превышен ({running_cost:.2f})."
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print("[phase3v2] " + result["FINAL_ANSWER"], flush=True)
            break

        for row in r["rows"]:
            eid = row["event_id"]
            prices_by_event[eid] = row
        result["batches"] = batch_meta
        result["n_events_priced_so_far"] = len(prices_by_event)
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    result["total_cost_credits"] = running_cost
    result["answers"] = build_answer_tables(sample, prices_by_event, d["wallet_roles"])
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[phase3v2] завершено: {len(prices_by_event)}/{len(sample)} событий получили цены, "
          f"стоимость={running_cost:.2f} кредитов", flush=True)


if __name__ == "__main__":
    main()
