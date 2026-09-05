#!/usr/bin/env python3
"""Задача C -- подтвердить РЕАЛЬНЫЕ тикеры серий для игровых (game-
winner) рынков по нескольким видам спорта на Kalshi, ДО построения
matcher'а (не гадаем по аналогии с KXNFLGAME)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskC-kalshi-series-probe/1.0"}
OUT_PATH = Path("data/p3_guard_cache/taskC_kalshi_series_probe_result.json")
BASE = "https://api.elections.kalshi.com/trade-api/v2"

CANDIDATE_TICKERS = ["KXNFLGAME", "KXMLBGAME", "KXNBAGAME", "KXNHLGAME", "KXNCAAFGAME", "KXNCAAMBGAME", "KXEPLGAME", "KXUFCFIGHT", "KXSOCCERGAME"]


def run() -> int:
    out = {}
    for ticker in CANDIDATE_TICKERS:
        r = requests.get(f"{BASE}/markets", params={"series_ticker": ticker, "limit": 5, "status": "open"},
                          headers=HEADERS, timeout=20)
        entry = {"status": r.status_code}
        try:
            body = r.json()
            entry["n_markets"] = len(body.get("markets", []))
            entry["sample_titles"] = [m.get("title") for m in body.get("markets", [])[:5]]
            entry["sample_event_tickers"] = [m.get("event_ticker") for m in body.get("markets", [])[:5]]
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)[:200]
        out[ticker] = entry
        print(f"{ticker}: status={entry['status']} n={entry.get('n_markets')} sample={entry.get('sample_titles')}")
        time.sleep(0.4)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[series_probe] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
