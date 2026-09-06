#!/usr/bin/env python3
"""«Спящие референсы», расширение п.2 (владелец, 2026-09-06, метрики по
классу «индексы/товары/форекс»): реальный HIP-3 dex "xyz" содержит ещё
7 нужных активов, история которых НЕ была снята в предыдущем шаге
(тот прогон брал только GOLD/SILVER/EUR/GBP/JPY/DXY/SP500) --
JP225/NIFTY/COPPER/SMH/SOXL/XBI/XLE. Реальный список подтверждён в
`hyperliquid_hip3_stock_perp_discovery_result.json` (119 активов dex
"xyz"), тянем ТОЛЬКО эти 7, не переисполняя уже полученные 30.

Тот же реальный эндпоинт/метод, что `hyperliquid_hip3_xyz_markprice_
history.py` (candleSnapshot -- весь диапазон одним вызовом, подтверждено
эмпирически в предыдущем прогоне; l2Book -- round-trip на $500/$5000).
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
RESULT_PATH = Path("data/p3_guard_cache/hyperliquid_hip3_xyz_extra_assets_history_result.json")
DISCOVERY_PATH = Path("data/p3_guard_cache/hyperliquid_hip3_stock_perp_discovery_result.json")

HISTORY_START_UTC = datetime(2026, 7, 5, tzinfo=timezone.utc)
INTERVAL = "1h"
DEPTH_NOTIONALS_USD = (500.0, 5000.0)
EXTRA_TICKERS = ("JP225", "NIFTY", "COPPER", "SMH", "SOXL", "XBI", "XLE")
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


def verify_real_membership() -> None:
    disc = json.loads(DISCOVERY_PATH.read_text())
    xyz_tickers = {n.split(":", 1)[1] for n in disc["steps"]["meta_by_dex"]["xyz"]["asset_names"]}
    missing = [t for t in EXTRA_TICKERS if t not in xyz_tickers]
    if missing:
        raise SystemExit(f"[extra_assets] реально ОТСУТСТВУЮТ в dex xyz (не гадаем, останавливаемся): {missing}")
    print(f"[extra_assets] все {len(EXTRA_TICKERS)} реально подтверждены в списке активов dex \"xyz\"")


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
    verify_real_membership()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "history_start_utc": HISTORY_START_UTC.isoformat(), "interval": INTERVAL,
                 "dex": "xyz", "assets": {}}

    for i, ticker in enumerate(EXTRA_TICKERS):
        coin = f"xyz:{ticker}"
        print(f"[{i+1}/{len(EXTRA_TICKERS)}] {coin}...")
        candles = fetch_candles(coin)
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
            **depth,
        }
        time.sleep(0.3)

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[extra_assets] результат записан в {RESULT_PATH}, сырые свечи -- в {OUT_DIR}/ "
          f"(общий файл именования hl_xyz_markprice_1h_<TICKER>.csv -- совпадает со schema основного шага)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
