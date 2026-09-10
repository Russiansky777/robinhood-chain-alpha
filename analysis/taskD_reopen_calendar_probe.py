#!/usr/bin/env python3
"""Задача D переоткрыта повторно (владелец, 2026-09-10): проверка
гипотезы, что /v4/historical/sports/{sport}/events (1 кредит/вызов)
реально отдаёт календарь СЕЗОНА (много игр вперёд), а не только события
на указанную дату -- реальный результат прошлого прогона (2025-08-01 и
2025-06-01 дали ОДИНАКОВО 272 события NFL) намекает на это. Если
подтвердится -- можно вычислить реальные игровые дни и снимать снимки
ТОЛЬКО вокруг них (день игры + день до), а не по всем календарным дням
подряд, экономя кредиты в разы при той же частоте 12/день внутри
игровых окон."""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskD-reopen-calendar-probe/1.0"}
BASE = "https://api.the-odds-api.com/v4"
API_KEY = os.environ.get("THE_ODDS_API", "")
OUT_PATH = Path("data/p3_guard_cache/taskD_reopen_calendar_probe_result.json")


def call(path: str, params: dict) -> tuple[int, dict | None, dict]:
    p = {"apiKey": API_KEY, **params}
    r = requests.get(f"{BASE}{path}", params=p, headers=HEADERS, timeout=25)
    usage = {"used": r.headers.get("x-requests-used"), "remaining": r.headers.get("x-requests-remaining"),
             "last": r.headers.get("x-requests-last")}
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body, usage


def run() -> int:
    if not API_KEY:
        print("[calendar_probe] THE_ODDS_API не найден.")
        return 1

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    targets = [
        ("americanfootball_nfl", "2025-09-15T12:00:00Z"),
        ("basketball_nba", "2026-03-15T12:00:00Z"),
        ("soccer_epl", "2025-10-15T12:00:00Z"),
        ("mma_mixed_martial_arts", "2025-10-15T12:00:00Z"),
    ]
    for sport, date in targets:
        status, body, usage = call(f"/historical/sports/{sport}/events", {"date": date})
        events = body.get("data", []) if isinstance(body, dict) else []
        commence_dates = sorted({ev.get("commence_time", "")[:10] for ev in events if ev.get("commence_time")})
        out[sport] = {
            "probe_date": date, "status": status, "n_events": len(events), "usage_after": usage,
            "n_distinct_commence_dates": len(commence_dates),
            "earliest_commence_date": commence_dates[0] if commence_dates else None,
            "latest_commence_date": commence_dates[-1] if commence_dates else None,
            "events_per_date_sample": dict(Counter(commence_dates).most_common(10)),
        }
        print(f"[calendar_probe] {sport}: {len(events)} событий, {len(commence_dates)} различных дат "
              f"({commence_dates[0] if commence_dates else '?'} .. {commence_dates[-1] if commence_dates else '?'}), "
              f"used={usage.get('used')}")
        time.sleep(0.3)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[calendar_probe] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
