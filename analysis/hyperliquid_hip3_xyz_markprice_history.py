#!/usr/bin/env python3
"""«Спящие референсы», п.2 продолжение -- реальный HIP-3 perp-dex "xyz"
(обнаружен в hyperliquid_hip3_stock_perp_discovery_result.json: 119
реальных активов, 23 из них пересекаются по тикеру с уже известными 37
сток-перпами Lighter) -- часовая история mark price с 05.07 + текущая
глубина книги (round-trip $500/$5000), для ПРЯМОГО структурного
сравнения с Lighter. Дополнительно: GOLD/SILVER/EUR/GBP/JPY/DXY/SP500
-- реальные активы того же dex, бесплатный задел на будущие платные
пункты 3 (золото) и 5 (EUR) спецификации -- НЕ гадаем состав, берём
только то, что реально нашли в предыдущем шаге.

Реальные эндпоинты (тот же хост, что funding_historical_backfill.py --
POST https://api.hyperliquid.xyz/info, без ключей):
  - {"type": "candleSnapshot", "req": {"coin", "interval", "startTime",
    "endTime"}} -- форма ответа проверяется эмпирически по первому
    реальному вызову (печатаем сырой пример перед циклом), не
    предполагаем заранее.
  - {"type": "l2Book", "coin"} -- реальный снимок стакана (levels:
    [bids, asks], каждый уровень {px, sz, n}).

Ноль кредитов Dune."""
from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HYPERLIQUID_API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-sleeping-refs/1.0", "Content-Type": "application/json"}
OUT_DIR = Path("data/sleeping_refs_cache")
OUT_DIR.mkdir(parents=True, exist_ok=True)
RESULT_PATH = Path("data/p3_guard_cache/hyperliquid_hip3_xyz_markprice_history_result.json")
DISCOVERY_PATH = Path("data/p3_guard_cache/hyperliquid_hip3_stock_perp_discovery_result.json")
LIGHTER_SYMBOLS_PATH = Path("data/p3_guard_cache/lighter_stock_perp_funding_result.json")

HISTORY_START_UTC = datetime(2026, 7, 5, tzinfo=timezone.utc)
INTERVAL = "1h"
DEPTH_NOTIONALS_USD = (500.0, 5000.0)
BONUS_SYMBOLS = ("GOLD", "SILVER", "EUR", "GBP", "JPY", "DXY", "SP500")
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_S = 3.0


def _post_info(body: dict) -> requests.Response:
    last_exc = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            r = requests.post(f"{HYPERLIQUID_API_BASE}/info", headers=HEADERS,
                               data=json.dumps(body), timeout=30)
            if r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"HTTP {r.status_code}")
            return r
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            time.sleep(RETRY_BACKOFF_S * (2 ** attempt))
    raise RuntimeError(f"не удалось получить ответ после {RETRY_ATTEMPTS} попыток: {last_exc}")


def select_symbols() -> list[str]:
    disc = json.loads(DISCOVERY_PATH.read_text())
    xyz_names = disc["steps"]["meta_by_dex"]["xyz"]["asset_names"]  # "xyz:TICKER"
    xyz_tickers = {n.split(":", 1)[1] for n in xyz_names}

    lighter = json.loads(LIGHTER_SYMBOLS_PATH.read_text())
    lighter_syms = {r["symbol"].upper() for r in lighter["results"]}

    overlap = sorted(xyz_tickers & lighter_syms)
    bonus = sorted(t for t in BONUS_SYMBOLS if t in xyz_tickers)
    print(f"[xyz_markprice] реальное пересечение с Lighter ({len(overlap)}): {overlap}")
    print(f"[xyz_markprice] реальные бонусные активы (golд/forex/индекс), найдены в том же dex ({len(bonus)}): {bonus}")
    return sorted(set(overlap) | set(bonus))


def fetch_candles(coin: str) -> list[dict]:
    since_ms = int(HISTORY_START_UTC.timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    r = _post_info({"type": "candleSnapshot",
                    "req": {"coin": coin, "interval": INTERVAL, "startTime": since_ms, "endTime": now_ms}})
    if r.status_code != 200:
        print(f"    {coin}: HTTP {r.status_code} -- {r.text[:200]}")
        return []
    try:
        data = r.json()
    except ValueError:
        print(f"    {coin}: не-JSON ответ -- {r.text[:200]}")
        return []
    if not isinstance(data, list):
        print(f"    {coin}: неожиданная форма ответа (не список) -- {str(data)[:300]}")
        return []
    return data


def fetch_round_trip_cost(coin: str, mid_price: float) -> dict:
    r = _post_info({"type": "l2Book", "coin": coin})
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    body = r.json()
    levels = body.get("levels", []) if isinstance(body, dict) else []
    if len(levels) != 2:
        return {"error": f"неожиданная форма levels: {str(levels)[:200]}"}
    bids, asks = levels[0], levels[1]

    def walk(book_levels: list[dict], notional_usd: float) -> float | None:
        acc_notional, acc_qty = 0.0, 0.0
        for lvl in book_levels:
            price, qty = float(lvl["px"]), float(lvl["sz"])
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

    out: dict = {}
    for notional in DEPTH_NOTIONALS_USD:
        avg_ask = walk(asks, notional)
        avg_bid = walk(bids, notional)
        if avg_ask is None or avg_bid is None or not mid_price:
            out[f"round_trip_cost_pct_{int(notional)}"] = None
            out[f"book_thin_at_{int(notional)}"] = True
        else:
            out[f"round_trip_cost_pct_{int(notional)}"] = (avg_ask - avg_bid) / mid_price * 100
            out[f"book_thin_at_{int(notional)}"] = False
    return out


def run() -> int:
    symbols = select_symbols()
    print(f"\n[xyz_markprice] реальных активов dex \"xyz\" к выгрузке: {len(symbols)}")

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "history_start_utc": HISTORY_START_UTC.isoformat(), "interval": INTERVAL,
                 "dex": "xyz", "assets": {}}

    first_call_printed = False
    for i, ticker in enumerate(symbols):
        coin = f"xyz:{ticker}"
        print(f"[{i+1}/{len(symbols)}] {coin}...")
        candles = fetch_candles(coin)
        if not first_call_printed and candles:
            print("    реальный пример первой свечи (форма ответа, не гадаем):")
            print("   ", json.dumps(candles[0], ensure_ascii=False))
            first_call_printed = True
        print(f"    {len(candles)} реальных часовых свечей")

        mid_price = float(candles[-1]["c"]) if candles else 0.0
        depth = fetch_round_trip_cost(coin, mid_price) if mid_price else {"error": "no candles/mid"}
        print(f"    mid={mid_price}, round-trip: {depth}")

        if candles:
            csv_path = OUT_DIR / f"hl_xyz_markprice_1h_{ticker}.csv"
            with open(csv_path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["t", "o", "h", "l", "c"])
                w.writeheader()
                for row in candles:
                    w.writerow({"t": row.get("t"), "o": row.get("o"), "h": row.get("h"),
                                "l": row.get("l"), "c": row.get("c")})

        out["assets"][ticker] = {
            "coin": coin, "n_candles": len(candles),
            "first_ts": candles[0]["t"] if candles else None,
            "last_ts": candles[-1]["t"] if candles else None,
            "mid_price_now": mid_price,
            "in_lighter_universe": ticker not in BONUS_SYMBOLS,
            **depth,
        }
        time.sleep(0.3)

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[xyz_markprice] результат записан в {RESULT_PATH}, сырые свечи -- в {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
