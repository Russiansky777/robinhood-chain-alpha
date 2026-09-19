#!/usr/bin/env python3
"""Владелец, 2026-09-19: Фаза 3 -- цены из dex_solana.trades (БЕЗ джойна
к transactions, только фильтр по минту+времени, как требовалось) для
очищенных событий Фазы 2 (data/solana_phase2_events_clean.json, 5811
событий после отсечки reward/платформенных минтов -- см. docstring
solana_phase2_filter_reward_tokens.py) + три итоговые таблицы для
владельца (докупки, порог+размер, новые кошельки).

Точки на событие: последняя сделка ДО входа, +5с, +30с, +60с, +180с --
для каждой берём ПОСЛЕДНЮЮ известную сделку минта на момент или раньше
горизонта (перенос вперёд при отсутствии сделки В окне -- события не
выбрасываются, недостающая точка помечается price_missing_at_<h> и
считается в отчёте, без тихих потерь).

Батчинг (по BATCH_SIZE событий на VALUES-джойн) -- тот же приём, что уже
спас Фазу 1 (solana_dune_signer_linkage_v5.py) от таймаута 775 272
строк/154МБ на глобальном окне: каждое событие получает СВОЁ узкое окно
[t0-300, t0+185], а не общий диапазон по всем событиям."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
EVENTS_PATH = REPO_ROOT / "data" / "solana_phase2_events_clean.json"
OUT_PATH = REPO_ROOT / "data" / "solana_phase3_result.json"

WSOL_MINT = "So11111111111111111111111111111111111111112"
LOOKBACK_S = 300
HORIZONS = [("plus5", 5), ("plus30", 30), ("plus60", 60), ("plus180", 180)]
MAX_HORIZON_S = 180
FETCH_HI_BUFFER_S = 5
BATCH_SIZE = 500
ROUND_TRIP_COST_FRAC = 0.06  # ~6% на 0.3 SOL размере, по заданию владельца


def flatten_events(events_by_wallet: dict) -> list[dict]:
    flat = []
    eid = 0
    for wallet, events in events_by_wallet.items():
        for e in events:
            flat.append({**e, "wallet": wallet, "event_id": eid})
            eid += 1
    return flat


def build_price_sql(batch: list[dict]) -> str:
    values = ",".join(
        f"({e['event_id']}, '{e['mint']}', {e['block_time_epoch'] - LOOKBACK_S}, "
        f"{e['block_time_epoch'] + MAX_HORIZON_S + FETCH_HI_BUFFER_S})"
        for e in batch
    )
    return (
        f"WITH ev(event_id, mint, lo, hi) AS (VALUES {values}) "
        "SELECT dt.block_time, dt.token_bought_mint_address AS bought_mint, "
        "dt.token_sold_mint_address AS sold_mint, dt.token_bought_amount AS bought_amount, "
        "dt.token_sold_amount AS sold_amount, e.event_id "
        "FROM dex_solana.trades dt JOIN ev e "
        "ON (dt.token_bought_mint_address = e.mint OR dt.token_sold_mint_address = e.mint) "
        "AND dt.block_time BETWEEN from_unixtime(e.lo) AND from_unixtime(e.hi) "
        f"WHERE (dt.token_bought_mint_address = e.mint AND dt.token_sold_mint_address = '{WSOL_MINT}') "
        f"OR (dt.token_sold_mint_address = e.mint AND dt.token_bought_mint_address = '{WSOL_MINT}')"
    )


def parse_bt(s) -> int:
    if isinstance(s, (int, float)):
        return int(s)
    return int(time.mktime(time.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")))


def trade_price_sol(row: dict, mint: str):
    try:
        if row["bought_mint"] == mint and row["sold_mint"] == WSOL_MINT:
            return float(row["sold_amount"]) / float(row["bought_amount"])
        if row["sold_mint"] == mint and row["bought_mint"] == WSOL_MINT:
            return float(row["bought_amount"]) / float(row["sold_amount"])
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None


def compute_event_prices(event: dict, trades: list[dict]) -> dict:
    t0 = event["block_time_epoch"]
    mint = event["mint"]
    points = []
    for row in trades:
        bt = parse_bt(row["block_time"])
        price = trade_price_sol(row, mint)
        if price is not None and price > 0:
            points.append((bt, price))
    points.sort(key=lambda p: p[0])
    times = [p[0] for p in points]
    prices = [p[1] for p in points]

    def last_at_or_before(horizon_time: int, strict: bool = False):
        lo_idx = -1
        for i, t in enumerate(times):
            if (t < horizon_time) if strict else (t <= horizon_time):
                lo_idx = i
            else:
                break
        return prices[lo_idx] if lo_idx >= 0 else None

    result = {"before_entry": last_at_or_before(t0, strict=True)}
    for name, offset in HORIZONS:
        result[name] = last_at_or_before(t0 + offset, strict=False)
    result["n_trades_in_window"] = len(points)
    return result


def build_events_answer_tables(flat_events: list[dict], prices_by_event: dict, wallet_roles: dict) -> dict:
    leader = wallet_roles["leader"][0]
    candidates_29 = set(wallet_roles["candidates_29"])

    def growth(prices: dict, horizon: str):
        base = prices.get("plus5")
        val = prices.get(horizon)
        if not base or not val or base <= 0:
            return None
        return val / base - 1.0

    # --- Q1: докупки (re-buys) по k, лидер отдельно, остальные вместе ---
    def bucket_k(k: int) -> str:
        return "k1" if k == 1 else ("k2" if k == 2 else "k3plus")

    rebuys = {"leader": {}, "others": {}}
    for group_name, wallets_filter in (("leader", {leader}), ("others", None)):
        by_k = {"k1": [], "k2": [], "k3plus": []}
        for e in flat_events:
            if wallets_filter is not None and e["wallet"] not in wallets_filter:
                continue
            if wallets_filter is None and e["wallet"] == leader:
                continue
            p = prices_by_event.get(e["event_id"])
            if not p:
                continue
            by_k[bucket_k(e["k"])].append(e)
        for kname, evs in by_k.items():
            g30 = [growth(prices_by_event[e["event_id"]], "plus30") for e in evs]
            g60 = [growth(prices_by_event[e["event_id"]], "plus60") for e in evs]
            g180 = [growth(prices_by_event[e["event_id"]], "plus180") for e in evs]
            g30v, g60v, g180v = [x for x in g30 if x is not None], [x for x in g60 if x is not None], [x for x in g180 if x is not None]
            rebuys[group_name][kname] = {
                "n": len(evs), "n_priced_30s": len(g30v), "n_priced_60s": len(g60v), "n_priced_180s": len(g180v),
                "median_growth_30s": median(g30v) if g30v else None,
                "median_growth_60s": median(g60v) if g60v else None,
                "median_growth_180s": median(g180v) if g180v else None,
            }
        k2 = rebuys[group_name]["k2"]
        k2["take_k2_rule"] = (k2["median_growth_30s"] is not None and k2["n_priced_30s"] >= 30
                               and (k2["median_growth_30s"] - ROUND_TRIP_COST_FRAC) > 0)

    # --- Q2: порог входа и наш размер, по бакетам размера источника, ТОЛЬКО k=1 ---
    size_buckets = [("2-4.3", 2, 4.3), ("4.3-8", 4.3, 8), ("8-15", 8, 15), ("15+", 15, float("inf"))]
    threshold_table = []
    depth_samples = []
    for name, lo, hi in size_buckets:
        evs = [e for e in flat_events if e["k"] == 1 and lo <= e["spend_sol_equiv"] < hi and e["wallet"] != leader]
        g30 = [growth(prices_by_event.get(e["event_id"], {}), "plus30") for e in evs]
        g60 = [growth(prices_by_event.get(e["event_id"], {}), "plus60") for e in evs]
        g30v, g60v = [x for x in g30 if x is not None], [x for x in g60 if x is not None]
        row = {"bucket_sol": name, "n": len(evs), "n_priced_30s": len(g30v), "n_priced_60s": len(g60v),
               "median_growth_30s": median(g30v) if g30v else None, "median_growth_60s": median(g60v) if g60v else None}
        threshold_table.append(row)
        for e in evs:
            p = prices_by_event.get(e["event_id"])
            if not p or not p.get("before_entry") or not p.get("plus5"):
                continue
            impact = p["plus5"] / p["before_entry"] - 1.0
            if impact > 1e-6:
                depth_samples.append(e["spend_sol_equiv"] / impact)
    threshold_sol = None
    for row in threshold_table:
        if row["median_growth_30s"] is not None and row["median_growth_30s"] >= 2 * ROUND_TRIP_COST_FRAC:
            threshold_sol = row["bucket_sol"].split("-")[0].replace("+", "")
            break
    our_size_estimate = None
    if depth_samples:
        depth_median = median(depth_samples)
        our_size_estimate = {
            "depth_sol_median_ГРУБАЯ_ОЦЕНКА": depth_median, "n_depth_samples": len(depth_samples),
            "size_at_impact_budget": {f"{b}_SOL_impact": round(depth_median * b, 3) for b in (0.005, 0.01, 0.02)},
            "caveat": "грубая оценка глубины из price-impact собственной сделки источника; НЕ калибровано на нашем реальном исполнении",
        }

    # --- Q3: новые кошельки -- частоты + рост, ранг ---
    by_wallet_new: dict[str, list] = {}
    for e in flat_events:
        if e["wallet"] in candidates_29 or e["wallet"] == leader:
            continue
        by_wallet_new.setdefault(e["wallet"], []).append(e)

    days = 7.0
    wallet_rows = []
    for wallet, evs in by_wallet_new.items():
        first_entries = [e for e in evs if e["k"] == 1]
        n_ge2 = sum(1 for e in first_entries if e["spend_sol_equiv"] >= 2)
        n_ge43 = sum(1 for e in first_entries if e["spend_sol_equiv"] >= 4.3)
        g30 = [growth(prices_by_event.get(e["event_id"], {}), "plus30") for e in first_entries]
        g30v = [x for x in g30 if x is not None]
        n = len(g30v)
        row = {"address": wallet, "n_first_entries": len(first_entries),
               "freq_ge2_per_day": round(n_ge2 / days, 3), "freq_ge43_per_day": round(n_ge43 / days, 3),
               "median_growth_30s": median(g30v) if g30v else None, "n_priced_30s": n}
        if n >= 20 and row["median_growth_30s"] is not None:
            row["rank_score"] = row["freq_ge2_per_day"] * row["median_growth_30s"]
        else:
            row["rank_score"] = None
        wallet_rows.append(row)

    ranked = sorted([r for r in wallet_rows if r["rank_score"] is not None], key=lambda r: -r["rank_score"])
    top10 = ranked[:10]

    return {
        "q1_rebuys": rebuys,
        "q2_threshold_and_size": {"table": threshold_table, "threshold_sol_lower_bound": threshold_sol,
                                   "our_size_estimate": our_size_estimate},
        "q3_new_wallets": {"n_wallets_evaluated": len(wallet_rows), "n_eligible_n_ge_20": len(ranked),
                            "top10": top10},
    }


def main() -> None:
    d = json.loads(EVENTS_PATH.read_text())
    flat_events = flatten_events(d["events_by_wallet"])

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery, "n_events_total": len(flat_events)}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    events_by_id = {e["event_id"]: e for e in flat_events}
    prices_by_event: dict[int, dict] = {}
    batch_meta = []
    n_batches = (len(flat_events) + BATCH_SIZE - 1) // BATCH_SIZE
    for bi in range(n_batches):
        batch = flat_events[bi * BATCH_SIZE:(bi + 1) * BATCH_SIZE]
        sql = build_price_sql(batch)
        r = probe.run_sql_sync(f"phase3_prices_batch_{bi}", sql, timeout_s=600)
        meta = {k: v for k, v in r.items() if k != "rows"}
        batch_meta.append(meta)
        print(f"[phase3] батч {bi+1}/{n_batches}: status={r.get('status')} n_rows={r.get('n_rows')} "
              f"credits={meta.get('status_meta', {}).get('execution_cost_credits')}", flush=True)
        if r.get("status") != "ok":
            result["batches"] = batch_meta
            result["FINAL_ANSWER"] = f"Батч {bi} не выполнился ({r.get('status')}) -- Фаза 3 неполная, см. batches."
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            continue

        rows_by_event: dict[int, list] = {}
        for row in r["rows"]:
            rows_by_event.setdefault(row["event_id"], []).append(row)
        for eid, trades in rows_by_event.items():
            ev = events_by_id.get(eid)
            if ev is None:
                continue
            prices_by_event[eid] = compute_event_prices(ev, trades)
        result["batches"] = batch_meta
        result["n_events_priced_so_far"] = len(prices_by_event)
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    n_price_missing_any = sum(1 for eid in events_by_id if eid not in prices_by_event
                               or any(prices_by_event[eid].get(h) is None for h, _ in HORIZONS))
    result["n_events_with_any_missing_horizon"] = n_price_missing_any
    result["n_events_no_trade_data_at_all"] = sum(1 for eid in events_by_id if eid not in prices_by_event)

    tables = build_events_answer_tables(flat_events, prices_by_event, d["wallet_roles"])
    result["answers"] = tables
    result["total_cost_credits"] = sum(
        (m.get("status_meta") or {}).get("execution_cost_credits", 0) for m in batch_meta
    )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[phase3] завершено: {len(prices_by_event)}/{len(flat_events)} событий получили хоть одну ценовую точку, "
          f"стоимость={result['total_cost_credits']:.2f} кредитов", flush=True)


if __name__ == "__main__":
    main()
