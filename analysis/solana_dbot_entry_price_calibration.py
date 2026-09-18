#!/usr/bin/env python3
"""Владелец, 2026-09-19: откалибровать базу цены по реальным сделкам.

Для завершённых сделок пилота (4, уже собраны и верифицированы в
data/solana_dbot_pilot_summary.json) и BATCH-1 (свежий скан того же
метода на его кошельке): по каждой -- НАША цена факт. исполнения, цена
СДЕЛКИ ЛИДЕРА (та же транзакция, что мы копируем) и котировка на +5с
после входа лидера. Единицы -- лидер платит в USDC, мы -- в SOL:
конвертируем нашу SOL-цену в USDC-эквивалент по РЕАЛЬНОМУ историческому
курсу SOL/USD (тот же метод, что solana_29_candidates_usdc_to_sol.py --
пул SOL/USDC 3ucNos4NbumP..., минутные свечи GeckoTerminal).

Маршрут и котировка +5с строятся ТЕМ ЖЕ методом, что и весь конвейер
(engine.decode_tx + plan_route_for_purchase + find_price_at из
solana_buyer200_select_extend.py/solana_buyer200_fast_price.py) -- не
переписан заново.

Ответ: медиана и разброс "на сколько % выше сделки лидера мы входим
фактически" -- отдельно от "на сколько % выше сделки лидера котировка
+5с" -- это и определяет, какая база корректна для таблицы горизонтов."""
from __future__ import annotations

import bisect
import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path
from statistics import median

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_buyer200_select_extend import plan_route_for_purchase, POOL_META_PATH, ROUTE_META_PATH  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dbot_entry_price_calibration.json"
BASELINE_PATH = REPO_ROOT / "data" / "dbot_sieve_baseline.json"

USDC_MINT = fp.USDC
SOL_MINT = "So11111111111111111111111111111111111111112"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
SOL_USDC_POOL = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"


def wallet_mint_deltas(tx: dict, wallet: str) -> dict:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return {"increased": [], "decreased": [], "pre_balances": {}, "deltas": {}}
    pre_tb, post_tb = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []

    def bals(rows):
        a: dict[str, D] = {}
        for b in rows:
            if b.get("owner") == wallet:
                amt = b["uiTokenAmount"]
                a[b["mint"]] = a.get(b["mint"], D(0)) + D(amt["amount"]) / D(10) ** amt["decimals"]
        return a

    pre, post = bals(pre_tb), bals(post_tb)
    delta = {k: post.get(k, D(0)) - pre.get(k, D(0)) for k in set(pre) | set(post)}
    return {"increased": [k for k, v in delta.items() if v > 0], "decreased": [k for k, v in delta.items() if v < 0],
            "pre_balances": {k: str(v) for k, v in pre.items()}, "deltas": {k: str(v) for k, v in delta.items()}}


def wallet_sol_delta(tx: dict, wallet: str) -> float | None:
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    idx = None
    for i, k in enumerate(keys):
        pk = k.get("pubkey") if isinstance(k, dict) else k
        if pk == wallet:
            idx = i
            break
    if idx is None:
        return None
    meta = tx.get("meta") or {}
    pre_list, post_list = meta.get("preBalances") or [], meta.get("postBalances") or []
    if idx >= len(pre_list) or idx >= len(post_list):
        return None
    return (post_list[idx] - pre_list[idx]) / 1e9


def is_signer(tx: dict, wallet: str) -> bool:
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    return any(isinstance(k, dict) and k.get("signer") and k.get("pubkey") == wallet for k in keys)


def scan_one_completed_trade(wallet: str, cutoff_time: int) -> dict | None:
    """Первая найденная пара buy->sell для этого кошелька в окне --
    тот же метод, что scan_pilot_trades/pair_trades в
    solana_dbot_pilot_summary.py, инлайн для BATCH-1 (другой кошелёк)."""
    events, before = [], None
    for _ in range(15):
        batch = fp.get_signatures_for_address(wallet, before=before)
        if not batch:
            break
        stop = False
        for s in batch:
            bt = s.get("blockTime")
            if bt is None:
                continue
            if bt < cutoff_time:
                stop = True
                break
            if s.get("err") is not None:
                continue
            tx = fp.get_transaction(s["signature"])
            if tx is None:
                continue
            deltas = wallet_mint_deltas(tx, wallet)
            sol_change = wallet_sol_delta(tx, wallet) or 0.0
            for mint in deltas["increased"]:
                if mint in (USDC_MINT, SOL_MINT) or sol_change >= -0.001:
                    continue
                events.append({"signature": s["signature"], "block_time": bt, "mint": mint, "direction": "buy", "tx": tx})
            for mint in deltas["decreased"]:
                if mint in (USDC_MINT, SOL_MINT) or sol_change <= 0.001:
                    continue
                events.append({"signature": s["signature"], "block_time": bt, "mint": mint, "direction": "sell", "tx": tx})
        if stop:
            break
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break
    by_mint: dict[str, list] = {}
    for e in events:
        by_mint.setdefault(e["mint"], []).append(e)
    for mint, evs in by_mint.items():
        evs.sort(key=lambda e: e["block_time"])
        buys = [e for e in evs if e["direction"] == "buy"]
        sells = [e for e in evs if e["direction"] == "sell"]
        if buys and sells and sells[0]["block_time"] >= buys[0]["block_time"]:
            b, s = buys[0], sells[0]
            tokens_bought = float(D(wallet_mint_deltas(b["tx"], wallet)["deltas"].get(mint, "0")))
            sol_out = -(wallet_sol_delta(b["tx"], wallet) or 0)
            return {"mint": mint, "buy_signature": b["signature"], "buy_block_time": b["block_time"],
                    "tokens_bought": tokens_bought, "total_sol_out_buy": sol_out}
    return None


def leader_buy_for_mint(mint: str, before_time: int, wallet: str) -> dict | None:
    """Тот же метод, что leader_entry_before в solana_dbot_pilot_summary.py
    -- ATA лидера для минта -> последняя покупка на момент <= before_time."""
    ata_result = fp.rpc_call("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
    accounts = (ata_result or {}).get("value", [])
    for acc in accounts:
        ata = acc["pubkey"]
        sigs, before = [], None
        for _ in range(10):
            batch = fp.rpc_call("getSignaturesForAddress", [ata, {"limit": 1000, "before": before}]) or []
            if not batch:
                break
            sigs.extend(batch)
            if batch[-1].get("blockTime") and batch[-1]["blockTime"] < before_time - 3600:
                break
            before = batch[-1]["signature"]
            if len(batch) < 1000:
                break
        candidates = [s for s in sigs if s.get("blockTime") and s["blockTime"] <= before_time and s.get("err") is None]
        candidates.sort(key=lambda s: s["blockTime"], reverse=True)
        for s in candidates[:5]:
            tx = fp.get_transaction(s["signature"])
            if not tx:
                continue
            deltas = wallet_mint_deltas(tx, wallet)
            if mint not in deltas["increased"]:
                continue
            usdc_delta = deltas["deltas"].get(USDC_MINT)
            paid_usdc = float(-D(usdc_delta)) if usdc_delta and D(usdc_delta) < 0 else None
            tokens_received = float(D(deltas["deltas"][mint]))
            return {"signature": s["signature"], "block_time": tx.get("blockTime"), "tx": tx,
                    "paid_usdc": paid_usdc, "tokens_received": tokens_received}
    return None


_sol_candles_cache: list[list] | None = None


def gecko_get(path: str, params: dict) -> dict:
    resp = requests.get(f"{GECKO_BASE}{path}", params=params, timeout=30, headers={"Accept": "application/json"})
    return {"http_status": resp.status_code, "body": resp.json() if resp.ok else None}


def sol_usd_price_at(t: int) -> float | None:
    global _sol_candles_cache
    if _sol_candles_cache is None:
        r = gecko_get(f"/networks/solana/pools/{SOL_USDC_POOL}/ohlcv/minute",
                      {"aggregate": 1, "before_timestamp": t + 3600, "limit": 200, "currency": "usd"})
        rows = (((r.get("body") or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        _sol_candles_cache = sorted(rows, key=lambda r: r[0])
    rows = _sol_candles_cache
    if not rows:
        return None
    times = [c[0] + 60 for c in rows]
    idx = bisect.bisect_right(times, t)
    if idx == 0:
        return rows[0][4]
    if idx >= len(rows):
        return rows[-1][4]
    t0, c0 = times[idx - 1], D(str(rows[idx - 1][4]))
    t1, c1 = times[idx], D(str(rows[idx][4]))
    if t1 == t0:
        return float(c0)
    frac = D(t - t0) / D(t1 - t0)
    return float(c0 + (c1 - c0) * frac)


def analyze_trade(label: str, mint: str, our_sig: str, our_block_time: int, our_tokens: float,
                   our_sol_spent: float, leader_buy: dict) -> dict:
    global_meta = json.loads(POOL_META_PATH.read_text()) if POOL_META_PATH.exists() else {}
    global_meta.update(json.loads(ROUTE_META_PATH.read_text()) if ROUTE_META_PATH.exists() else {})

    leader_tx = leader_buy["tx"]
    leader_time = leader_buy["block_time"]
    leader_price_usdc = (leader_buy["paid_usdc"] / leader_buy["tokens_received"]
                          if leader_buy.get("paid_usdc") and leader_buy.get("tokens_received") else None)

    our_price_sol = our_sol_spent / our_tokens if our_tokens else None
    sol_usd_at_our_time = sol_usd_price_at(our_block_time)
    our_price_usdc_equiv = our_price_sol * sol_usd_at_our_time if our_price_sol and sol_usd_at_our_time else None

    route, meta_updated = plan_route_for_purchase(mint, leader_tx, global_meta)
    price_at_5s_usdc = None
    route_status = "no_route" if not route else "ok"
    if route:
        lo, hi = leader_time, leader_time + 5 + 60
        try:
            value = D(1)
            ok = True
            for leg in route:
                p = fp.find_price_at(leg["pool"], leader_time + 5, lo, hi)
                if p.get("status") != "ok":
                    ok = False
                    break
                e = p["event"]
                v = D(e["p1_per_0"])
                if leg["from"] == e["m0"] and leg["to"] == e["m1"]:
                    value *= v
                elif leg["from"] == e["m1"] and leg["to"] == e["m0"]:
                    value /= v
                else:
                    ok = False
                    break
            if ok:
                price_at_5s_usdc = float(value)
            else:
                route_status = "price_at_5s_failed"
        except RuntimeError as exc:
            route_status = f"rpc_error: {str(exc)[:150]}"

    out = {
        "label": label, "mint": mint, "our_signature": our_sig,
        "leader_signature": leader_buy["signature"], "leader_price_usdc": leader_price_usdc,
        "our_price_sol": our_price_sol, "sol_usd_rate_at_our_time": sol_usd_at_our_time,
        "our_price_usdc_equiv": our_price_usdc_equiv,
        "price_at_5s_after_leader_usdc": price_at_5s_usdc, "route_status": route_status,
    }
    if leader_price_usdc and our_price_usdc_equiv:
        out["our_vs_leader_pct"] = (our_price_usdc_equiv / leader_price_usdc - 1) * 100
    if leader_price_usdc and price_at_5s_usdc:
        out["plus5s_vs_leader_pct"] = (price_at_5s_usdc / leader_price_usdc - 1) * 100
    if our_price_usdc_equiv and price_at_5s_usdc:
        out["our_vs_plus5s_pct"] = (our_price_usdc_equiv / price_at_5s_usdc - 1) * 100
    return out


def main() -> None:
    results = []

    pilot_summary_path = REPO_ROOT / "data" / "solana_dbot_pilot_summary.json"
    if pilot_summary_path.exists():
        pilot = json.loads(pilot_summary_path.read_text())
        for t in pilot.get("trades", []):
            if t.get("status") != "ok" or not t.get("leader", {}).get("signature"):
                continue
            leader_tx = fp.get_transaction(t["leader"]["signature"])
            if leader_tx is None:
                continue
            leader_buy = {"signature": t["leader"]["signature"], "block_time": t["leader"]["block_time"],
                          "tx": leader_tx, "paid_usdc": t["leader"].get("paid_usdc"),
                          "tokens_received": t["leader"].get("tokens_received")}
            r = analyze_trade("pilot", t["mint"], t["buy_signature"], t["buy_block_time"],
                              t["tokens_bought"], t["total_sol_out_buy"], leader_buy)
            results.append(r)
            print(f"[entry_calib] pilot {t['mint'][:10]}.. our_vs_leader={r.get('our_vs_leader_pct')} "
                  f"plus5s_vs_leader={r.get('plus5s_vs_leader_pct')}", flush=True)

    if BASELINE_PATH.exists():
        baseline = json.loads(BASELINE_PATH.read_text())
        b1 = (baseline.get("batches") or {}).get("BATCH-1")
        if b1 and b1.get("wallet_address"):
            cutoff = int(time.time()) - 6 * 3600  # окно с запуска BATCH-1 (~00:35), с запасом
            trade = scan_one_completed_trade(b1["wallet_address"], cutoff)
            if trade:
                leader_buy = leader_buy_for_mint(trade["mint"], trade["buy_block_time"], LEADER_WALLET)
                if leader_buy:
                    r = analyze_trade("BATCH-1", trade["mint"], trade["buy_signature"], trade["buy_block_time"],
                                      trade["tokens_bought"], trade["total_sol_out_buy"], leader_buy)
                    results.append(r)
                    print(f"[entry_calib] BATCH-1 {trade['mint'][:10]}.. our_vs_leader={r.get('our_vs_leader_pct')} "
                          f"plus5s_vs_leader={r.get('plus5s_vs_leader_pct')}", flush=True)
                else:
                    print("[entry_calib] BATCH-1: сделка найдена, но покупка лидера по этому минту не найдена", flush=True)
            else:
                print("[entry_calib] BATCH-1: завершённая пара buy->sell не найдена в окне", flush=True)

    our_vs_leader_vals = [r["our_vs_leader_pct"] for r in results if "our_vs_leader_pct" in r]
    plus5s_vs_leader_vals = [r["plus5s_vs_leader_pct"] for r in results if "plus5s_vs_leader_pct" in r]

    summary = {
        "n_trades_analyzed": len(results),
        "n_our_vs_leader_computed": len(our_vs_leader_vals),
        "median_our_vs_leader_pct": median(our_vs_leader_vals) if our_vs_leader_vals else None,
        "our_vs_leader_pct_values": our_vs_leader_vals,
        "n_plus5s_vs_leader_computed": len(plus5s_vs_leader_vals),
        "median_plus5s_vs_leader_pct": median(plus5s_vs_leader_vals) if plus5s_vs_leader_vals else None,
        "plus5s_vs_leader_pct_values": plus5s_vs_leader_vals,
    }
    if our_vs_leader_vals and plus5s_vs_leader_vals:
        summary["conclusion"] = (
            "наш вход БЛИЖЕ К ЛИДЕРУ, чем к +5с -- база +5с ЗАНИЖАЕТ доходность, таблицу горизонтов "
            "нужно пересчитать от цены лидера/входа" if abs(summary["median_our_vs_leader_pct"]) < abs(summary["median_plus5s_vs_leader_pct"])
            else "наш вход БЛИЖЕ К КОТИРОВКЕ +5с -- текущая база верна, вопрос закрыт"
        )

    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "trades": results, "summary": summary}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[entry_calib] записано в {OUT_PATH}: {summary}", flush=True)


if __name__ == "__main__":
    main()
