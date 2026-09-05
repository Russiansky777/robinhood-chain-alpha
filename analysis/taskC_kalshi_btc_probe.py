#!/usr/bin/env python3
"""Задача C, разведка -- реальные BTC-часовые серии на Kalshi (владелец:
"сопоставить рынки за 30 дней -- спорт и BTC-часовые как самые
ликвидные"). Ищем series_ticker с BTC в названии/тикере, реальные
активные рынки, формат strike/цены."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskC-kalshi-btc-probe/1.0"}
OUT_PATH = Path("data/p3_guard_cache/taskC_kalshi_btc_probe_result.json")
BASE = "https://api.elections.kalshi.com/trade-api/v2"


def run() -> int:
    out = {}
    r = requests.get(f"{BASE}/series", params={"category": "Crypto"}, headers=HEADERS, timeout=20)
    out["series_crypto"] = {"status": r.status_code}
    try:
        body = r.json()
        out["series_crypto"]["n_series"] = len(body.get("series", []))
        out["series_crypto"]["tickers"] = [s.get("ticker") for s in body.get("series", [])][:40]
    except Exception as e:  # noqa: BLE001
        out["series_crypto"]["error"] = str(e)[:200]
        out["series_crypto"]["raw_head"] = r.text[:500]
    time.sleep(0.5)

    # Реальные активные рынки, фильтр по тикеру серии с BTC (если нашли выше)
    r2 = requests.get(f"{BASE}/markets", params={"series_ticker": "KXBTC", "limit": 10, "status": "open"},
                       headers=HEADERS, timeout=20)
    out["markets_kxbtc"] = {"status": r2.status_code}
    try:
        body2 = r2.json()
        out["markets_kxbtc"]["n_markets"] = len(body2.get("markets", []))
        out["markets_kxbtc"]["sample"] = body2.get("markets", [])[:2]
    except Exception as e:  # noqa: BLE001
        out["markets_kxbtc"]["error"] = str(e)[:200]
        out["markets_kxbtc"]["raw_head"] = r2.text[:500]
    time.sleep(0.5)

    # История сделок по конкретному рынку (если есть хоть один в выборке)
    ticker = None
    try:
        mkts = out["markets_kxbtc"].get("sample") or []
        if mkts:
            ticker = mkts[0].get("ticker")
    except Exception:
        pass
    if ticker:
        r3 = requests.get(f"{BASE}/markets/{ticker}/trades", params={"limit": 5}, headers=HEADERS, timeout=20)
        out["trades_sample"] = {"status": r3.status_code, "ticker": ticker}
        try:
            body3 = r3.json()
            out["trades_sample"]["keys"] = list(body3.keys())
            out["trades_sample"]["sample"] = body3.get("trades", [])[:3]
        except Exception as e:  # noqa: BLE001
            out["trades_sample"]["error"] = str(e)[:200]
            out["trades_sample"]["raw_head"] = r3.text[:500]
        time.sleep(0.5)
        r4 = requests.get(f"{BASE}/markets/{ticker}/orderbook", headers=HEADERS, timeout=20)
        out["orderbook_sample"] = {"status": r4.status_code, "ticker": ticker}
        try:
            out["orderbook_sample"]["body"] = r4.json()
        except Exception as e:  # noqa: BLE001
            out["orderbook_sample"]["error"] = str(e)[:200]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str)[:4000])
    print(f"\n[kalshi_btc_probe] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
