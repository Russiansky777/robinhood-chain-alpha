#!/usr/bin/env python3
"""«Спящие референсы» → расхождение площадок, п.4 владельца (2026-09-06):
"глубину стакана обеих взять текущую" -- на $500/$5000 уже есть
(предыдущие шаги), НЕ было на $20000 -- реальный текущий (СЕЙЧАС,
снимок, не исторический) снимок стакана для 23 общих тикеров на ОБЕИХ
площадках, тот же метод round-trip (ходим по книге от лучшей цены,
накапливаем номинал), что уже использован в `lighter_stock_perp_
markprice_history.py`/`hyperliquid_hip3_xyz_markprice_history.py`.

Ноль кредитов Dune -- чистый REST, только чтение."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
HYPERLIQUID_API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-sleeping-refs/1.0"}
HL_HEADERS = {"User-Agent": "robinhood-chain-alpha-sleeping-refs/1.0", "Content-Type": "application/json"}
OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_orderbook_depth_20k_result.json")
LIGHTER_RESULT_PATH = Path("data/p3_guard_cache/lighter_stock_perp_markprice_history_result.json")
XYZ_RESULT_PATH = Path("data/p3_guard_cache/hyperliquid_hip3_xyz_markprice_history_result.json")
DEPTH_NOTIONALS_USD = (500.0, 5000.0, 20000.0)  # 500/5000 -- сверка с уже известным, 20000 -- новое
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_S = 3.0


def retry_get(path: str, params: dict) -> requests.Response:
    last_exc = None
    for i in range(RETRY_ATTEMPTS):
        try:
            r = requests.get(f"{LIGHTER_API_BASE}{path}", params=params, headers=HEADERS, timeout=20)
            if r.status_code == 429:
                time.sleep(2 ** i)
                continue
            return r
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 ** i)
    raise RuntimeError(f"lighter GET не удался: {last_exc}")


def retry_post_hl(body: dict) -> requests.Response:
    last_exc = None
    for i in range(RETRY_ATTEMPTS):
        try:
            r = requests.post(f"{HYPERLIQUID_API_BASE}/info", headers=HL_HEADERS, data=json.dumps(body), timeout=20)
            if r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"HTTP {r.status_code}")
            return r
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            time.sleep(RETRY_BACKOFF_S * (2 ** i))
    raise RuntimeError(f"HL POST не удался: {last_exc}")


def walk_book(levels: list[dict], notional_usd: float, price_key: str, qty_key: str) -> float | None:
    acc_notional, acc_qty = 0.0, 0.0
    for lvl in levels:
        price, qty = float(lvl[price_key]), float(lvl[qty_key])
        level_notional = price * qty
        if acc_notional + level_notional >= notional_usd:
            remaining_notional = notional_usd - acc_notional
            acc_qty += remaining_notional / price
            acc_notional = notional_usd
            break
        acc_notional += level_notional
        acc_qty += qty
    if acc_notional < notional_usd * 0.999 or acc_qty == 0:
        return None
    return acc_notional / acc_qty


def lighter_round_trip(market_id: int, mid_price: float) -> dict:
    r = retry_get("/api/v1/orderBookOrders", {"market_id": market_id, "limit": 200})
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    body = r.json()
    bids = sorted(body.get("bids", []), key=lambda o: -float(o["price"]))
    asks = sorted(body.get("asks", []), key=lambda o: float(o["price"]))
    out: dict = {}
    for notional in DEPTH_NOTIONALS_USD:
        avg_ask = walk_book(asks, notional, "price", "remaining_base_amount")
        avg_bid = walk_book(bids, notional, "price", "remaining_base_amount")
        if avg_ask is None or avg_bid is None or not mid_price:
            out[f"round_trip_cost_pct_{int(notional)}"] = None
        else:
            out[f"round_trip_cost_pct_{int(notional)}"] = (avg_ask - avg_bid) / mid_price * 100
    return out


def xyz_round_trip(ticker: str, mid_price: float) -> dict:
    r = retry_post_hl({"type": "l2Book", "coin": f"xyz:{ticker}"})
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    body = r.json()
    levels = body.get("levels", []) if isinstance(body, dict) else []
    if len(levels) != 2:
        return {"error": f"неожиданная форма levels: {str(levels)[:200]}"}
    bids, asks = levels[0], levels[1]
    out: dict = {}
    for notional in DEPTH_NOTIONALS_USD:
        avg_ask = walk_book(asks, notional, "px", "sz")
        avg_bid = walk_book(bids, notional, "px", "sz")
        if avg_ask is None or avg_bid is None or not mid_price:
            out[f"round_trip_cost_pct_{int(notional)}"] = None
        else:
            out[f"round_trip_cost_pct_{int(notional)}"] = (avg_ask - avg_bid) / mid_price * 100
    return out


def run() -> int:
    lighter = json.loads(LIGHTER_RESULT_PATH.read_text())["markets"]
    xyz = json.loads(XYZ_RESULT_PATH.read_text())["assets"]
    overlap = sorted(t for t, m in xyz.items() if m.get("in_lighter_universe") and t in lighter)
    print(f"[depth_20k] реальный универсум: {len(overlap)} -- {overlap}")

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "depth_notionals_usd": list(DEPTH_NOTIONALS_USD), "assets": {}}

    for i, ticker in enumerate(overlap):
        print(f"[{i+1}/{len(overlap)}] {ticker}...")
        l_meta = lighter[ticker]
        l_depth = lighter_round_trip(l_meta["market_id"], l_meta.get("mid_price_now") or 0)
        print(f"    lighter: {l_depth}")
        time.sleep(0.2)

        x_meta = xyz[ticker]
        x_depth = xyz_round_trip(ticker, x_meta.get("mid_price_now") or 0)
        print(f"    xyz: {x_depth}")
        time.sleep(0.2)

        out["assets"][ticker] = {"lighter": l_depth, "xyz": x_depth}

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[depth_20k] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
