#!/usr/bin/env python3
"""Владелец, 2026-09-19: Фаза 3 v3 -- после v2 (батч 500 событий = 339
кредитов и упал; батч 20 событий по фиксированному размеру = 4.15
кредита/событие, для всей выборки 1871 событий вышло бы ~7772 кредита)
и находки на pre_amt (bounded IN-джойн по дню = 2-5.6 кредита/день
НЕЗАВИСИМО от числа событий в дне -- т.е. стоимость определяется
партицией/сканом, не количеством событий): та же логика применена
здесь. Батчинг -- ПО КАЛЕНДАРНОМУ ДНЮ события (не фиксированным
размером) -- Фаза 3 делает ОТКРЫТЫЙ скан dex_solana.trades по
минту+времени (не bounded IN по known tx_id, как pre_amt), но
группировка по дню всё равно должна сократить пересечение партиций
между батчами по сравнению с батчами фиксированного размера,
нарезающими произвольно поперёк дней.

Требует РЕЗУЛЬТАТ pre_amt-фикса (data/solana_phase2_events_v3.json,
first_entry/k по факту баланса, подтверждённая сходимость с RPC-
эталоном -- лидер 14.57/сутки против цели 13.6, ratio 1.07).

Выборка -- та же, что в v2, по инструкции владельца: лидер -- все
k=1/k=2; остальные -- 800 k=1 стратифицированных (мин 60/бакет), 300
k=2, 150 k=3+, плюс до 30 k=1/кошелёк для топ-30 по частоте. Рост --
внутри SQL (max_by FILTER), 5 чисел на событие.

Первый день -- честный замер кредита/день (не кредита/событие) и
прогноз на все дни выборки, потолок 150/прогон, общий бюджет 1200
(потрачено к этому шагу ~223.7)."""
from __future__ import annotations

import gzip
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
EVENTS_PATH = REPO_ROOT / "data" / "solana_phase2_events_v3.json"
OUT_PATH = REPO_ROOT / "data" / "solana_phase3_result_v3.json"
RAW_DIR = REPO_ROOT / "data" / "solana_phase3_raw_v3"

WSOL_MINT = "So11111111111111111111111111111111111111112"
LOOKBACK_S = 300
HORIZONS = [("plus5", 5), ("plus30", 30), ("plus60", 60), ("plus180", 180)]
MAX_HORIZON_S = 180
FETCH_HI_BUFFER_S = 5
CHUNK_SIZE = 700  # под-батч ВНУТРИ дня, чтобы не упереться в лимит длины SQL (см. preamt/09-17)
MAX_TOTAL_COST_CREDITS = 150.0
FATAL_ERROR_MARKERS = ("RESOURCES_CAP_REACHED", "exceed your configured", "datapoint limit")
ROUND_TRIP_COST_FRAC = 0.06
SIZE_BUCKETS = [("2-4.3", 2, 4.3), ("4.3-8", 4.3, 8), ("8-15", 8, 15), ("15+", 15, float("inf"))]
SEED = 20260919  # воспроизводимая случайная выборка


def day_str(epoch: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


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
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    d = json.loads(EVENTS_PATH.read_text())
    sample = build_sample(d["events_by_wallet"], d["wallet_roles"])
    events_by_id = {e["event_id"]: e for e in sample}

    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in sample:
        by_day[day_str(e["block_time_epoch"])].append(e)
    days = sorted(by_day.keys())

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery, "n_events_sampled": len(sample),
                     "n_events_per_day": {k: len(v) for k, v in by_day.items()}}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_JANA_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    prices_by_event: dict[int, dict] = {}
    day_meta, running_cost = [], 0.0
    first_day_done = False
    for di, day in enumerate(days):
        day_events = by_day[day]
        chunks = [day_events[j:j + CHUNK_SIZE] for j in range(0, len(day_events), CHUNK_SIZE)]
        day_cost, day_rows_all, day_status = 0.0, [], "ok"
        for ci, chunk in enumerate(chunks):
            sql = build_price_sql(chunk)
            r = probe.run_sql_sync(f"phase3v3_{day}_c{ci}", sql, timeout_s=600)
            meta = {k: v for k, v in r.items() if k != "rows"}
            cost = (meta.get("status_meta") or {}).get("execution_cost_credits", 0) or 0
            day_cost += cost
            running_cost += cost
            if r.get("status") != "ok":
                day_status = r.get("status")
                print(f"[phase3v3] день {day} чанк {ci+1}/{len(chunks)}: status={day_status} -- "
                      f"тело: {json.dumps(meta, default=str)[:800]}", flush=True)
                break
            day_rows_all.extend(r["rows"])
            print(f"[phase3v3] день {day} чанк {ci+1}/{len(chunks)}: status=ok n_rows={r.get('n_rows')} "
                  f"cost={cost} running_total={running_cost:.2f}", flush=True)

        day_meta.append({"day": day, "n_events": len(day_events), "n_chunks": len(chunks),
                          "status": day_status, "n_rows": len(day_rows_all), "cost_credits": day_cost})
        result["days_processed"] = day_meta
        result["total_cost_credits_so_far"] = running_cost
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

        if not first_day_done:
            first_day_done = True
            projected_total = day_cost * len(days)  # прогноз по стоимости ДНЯ, не события (см. докстринг)
            result["cost_per_day_first_day"] = day_cost
            result["projected_total_cost_credits"] = projected_total
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print(f"[phase3v3] прогноз после дня 1: {day_cost:.2f} кредита/день x {len(days)} дней "
                  f"≈ {projected_total:.2f} кредитов (потолок {MAX_TOTAL_COST_CREDITS})", flush=True)
            if projected_total > MAX_TOTAL_COST_CREDITS:
                result["FINAL_ANSWER"] = (f"ОСТАНОВЛЕНО после дня 1 -- прогноз ({projected_total:.2f}) "
                                           f"превышает потолок {MAX_TOTAL_COST_CREDITS}. Нужно решение владельца.")
                OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                print("[phase3v3] " + result["FINAL_ANSWER"], flush=True)
                return

        if day_status != "ok":
            err_text = json.dumps(day_meta[-1], default=str)
            is_fatal = any(m in err_text for m in FATAL_ERROR_MARKERS)
            result["FINAL_ANSWER"] = f"ОСТАНОВЛЕНО на дне {day} ({'внешний блокер Dune' if is_fatal else day_status})."
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print("[phase3v3] " + result["FINAL_ANSWER"], flush=True)
            return

        raw_path = RAW_DIR / f"{day}.json.gz"
        with gzip.open(raw_path, "wt", encoding="utf-8") as fh:
            json.dump({"day": day, "n_chunks": len(chunks), "rows": day_rows_all}, fh, default=str)
        for row in day_rows_all:
            prices_by_event[row["event_id"]] = row
        result["n_events_priced_so_far"] = len(prices_by_event)
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

        if running_cost > MAX_TOTAL_COST_CREDITS:
            result["FINAL_ANSWER"] = f"ОСТАНОВЛЕНО -- потолок {MAX_TOTAL_COST_CREDITS} кредитов превышен ({running_cost:.2f})."
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print("[phase3v3] " + result["FINAL_ANSWER"], flush=True)
            return

    result["total_cost_credits"] = running_cost
    result["answers"] = build_answer_tables(sample, prices_by_event, d["wallet_roles"])
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[phase3v3] завершено: {len(prices_by_event)}/{len(sample)} событий получили цены, "
          f"стоимость={running_cost:.2f} кредитов", flush=True)


if __name__ == "__main__":
    main()
