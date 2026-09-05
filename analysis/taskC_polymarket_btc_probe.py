#!/usr/bin/env python3
"""Задача C, разведка -- реальная структура BTC-рынков на Polymarket:
пороговые (threshold, как Kalshi KXBTC) или направленные (up/down,
как уже увиденные 5-минутные "Dogecoin Up or Down")? Без этого нельзя
честно решить, сопоставимы ли вообще BTC-часовые между площадками "как
есть", или это структурно разные типы ставок."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskC-polymarket-btc-probe/1.0"}
OUT_PATH = Path("data/p3_guard_cache/taskC_polymarket_btc_probe_result.json")
BASE = "https://gamma-api.polymarket.com"


def run() -> int:
    out = {}
    # Полнотекстовый поиск по вопросу -- реальный параметр Gamma API
    r = requests.get(f"{BASE}/markets", params={"limit": 20, "active": "true", "closed": "false",
                                                  "search": "bitcoin hourly"}, headers=HEADERS, timeout=20)
    out["search_bitcoin_hourly"] = {"status": r.status_code}
    try:
        body = r.json()
        out["search_bitcoin_hourly"]["n"] = len(body) if isinstance(body, list) else None
        out["search_bitcoin_hourly"]["questions"] = [m.get("question") for m in body][:20] if isinstance(body, list) else body
    except Exception as e:  # noqa: BLE001
        out["search_bitcoin_hourly"]["error"] = str(e)[:200]
    time.sleep(0.5)

    r2 = requests.get(f"{BASE}/markets", params={"limit": 20, "active": "true", "closed": "false",
                                                    "search": "bitcoin price"}, headers=HEADERS, timeout=20)
    out["search_bitcoin_price"] = {"status": r2.status_code}
    try:
        body2 = r2.json()
        out["search_bitcoin_price"]["n"] = len(body2) if isinstance(body2, list) else None
        out["search_bitcoin_price"]["questions"] = [m.get("question") for m in body2][:20] if isinstance(body2, list) else body2
    except Exception as e:  # noqa: BLE001
        out["search_bitcoin_price"]["error"] = str(e)[:200]
    time.sleep(0.5)

    r3 = requests.get(f"{BASE}/events", params={"limit": 10, "active": "true", "closed": "false",
                                                   "tag_slug": "bitcoin"}, headers=HEADERS, timeout=20)
    out["events_tag_bitcoin"] = {"status": r3.status_code}
    try:
        body3 = r3.json()
        out["events_tag_bitcoin"]["n"] = len(body3) if isinstance(body3, list) else None
        out["events_tag_bitcoin"]["titles"] = [e.get("title") for e in body3][:20] if isinstance(body3, list) else body3
    except Exception as e:  # noqa: BLE001
        out["events_tag_bitcoin"]["error"] = str(e)[:200]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str)[:4000])
    print(f"\n[polymarket_btc_probe] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
