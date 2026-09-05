#!/usr/bin/env python3
"""Задача C, разведка -- реальные форматы спортивных рынков на Kalshi и
Polymarket бок о бок (владелец: "сопоставить рынки за 30 дней -- спорт
и BTC-часовые как самые ликвидные, сопоставление по названию с ручной
проверкой первых 20"). Без этого нельзя честно написать matcher --
нужно увидеть реальные title/ticker с обеих сторон для одних и тех же
видов спорта."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskC-sports-match-probe/1.0"}
OUT_PATH = Path("data/p3_guard_cache/taskC_sports_match_probe_result.json")


def run() -> int:
    out = {}

    # Kalshi -- реальные series по спорту (NFL/MLB/NBA), затем реальные
    # активные рынки внутри одной серии, полные тикеры + названия команд.
    r = requests.get("https://api.elections.kalshi.com/trade-api/v2/series",
                      params={"category": "Sports"}, headers=HEADERS, timeout=20)
    out["kalshi_sports_series"] = {"status": r.status_code}
    try:
        body = r.json()
        out["kalshi_sports_series"]["tickers"] = [s.get("ticker") for s in body.get("series", [])][:60]
    except Exception as e:  # noqa: BLE001
        out["kalshi_sports_series"]["error"] = str(e)[:200]
    time.sleep(0.5)

    r2 = requests.get("https://api.elections.kalshi.com/trade-api/v2/markets",
                       params={"series_ticker": "KXNFLGAME", "limit": 10, "status": "open"},
                       headers=HEADERS, timeout=20)
    out["kalshi_nfl_markets"] = {"status": r2.status_code}
    try:
        body2 = r2.json()
        out["kalshi_nfl_markets"]["n"] = len(body2.get("markets", []))
        out["kalshi_nfl_markets"]["sample"] = [
            {"ticker": m.get("ticker"), "title": m.get("title"), "subtitle": m.get("subtitle"),
             "event_ticker": m.get("event_ticker"), "close_time": m.get("close_time"),
             "yes_bid": m.get("yes_bid_dollars"), "yes_ask": m.get("yes_ask_dollars")}
            for m in body2.get("markets", [])[:10]
        ]
    except Exception as e:  # noqa: BLE001
        out["kalshi_nfl_markets"]["error"] = str(e)[:200]
    time.sleep(0.5)

    # Polymarket -- реальные активные спортивные рынки, отсортированные
    # по объёму (tag_slug фильтр -- проверим, работает ли лучше, чем
    # search=, который ранее оказался нерабочим).
    r3 = requests.get("https://gamma-api.polymarket.com/markets",
                       params={"limit": 20, "active": "true", "closed": "false", "tag_slug": "nfl"},
                       headers=HEADERS, timeout=20)
    out["polymarket_nfl_tag"] = {"status": r3.status_code}
    try:
        body3 = r3.json()
        out["polymarket_nfl_tag"]["n"] = len(body3) if isinstance(body3, list) else None
        out["polymarket_nfl_tag"]["sample"] = [
            {"question": m.get("question"), "slug": m.get("slug"), "endDate": m.get("endDate"),
             "outcomePrices": m.get("outcomePrices")}
            for m in body3[:10]
        ] if isinstance(body3, list) else body3
    except Exception as e:  # noqa: BLE001
        out["polymarket_nfl_tag"]["error"] = str(e)[:200]
    time.sleep(0.5)

    # Общий список тегов Polymarket -- реальные slug'и, чтобы не гадать
    # правильное имя тега для каждого вида спорта.
    r4 = requests.get("https://gamma-api.polymarket.com/tags", params={"limit": 100}, headers=HEADERS, timeout=20)
    out["polymarket_tags"] = {"status": r4.status_code}
    try:
        body4 = r4.json()
        out["polymarket_tags"]["slugs"] = [t.get("slug") for t in body4][:100] if isinstance(body4, list) else body4
    except Exception as e:  # noqa: BLE001
        out["polymarket_tags"]["error"] = str(e)[:200]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str)[:5000])
    print(f"\n[sports_match_probe] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
