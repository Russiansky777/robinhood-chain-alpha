#!/usr/bin/env python3
"""Задача D переоткрыта повторно (владелец, 2026-09-10): реальный
построитель календаря игр. Разведка (taskD_reopen_calendar_probe.py)
показала -- /v4/historical/sports/{sport}/events отдаёт УЗКОЕ окно
вперёд от даты запроса (не весь сезон), поэтому календарь строится
периодическими дешёвыми вызовами (1 кредит/вызов) с шагом ~7 дней по
всему целевому окну каждого спорта, объединением уникальных дат игр.

Цель -- знать РЕАЛЬНЫЕ игровые дни заранее, чтобы дорогой платный
/odds-фетч (10 кредитов/вызов) снимал 12 снимков/день (каждые 2ч)
ТОЛЬКО в игровые дни (+1 день до), а не по всем календарным дням
подряд -- кратная экономия кредитов при той же частоте снимков."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskD-reopen-calendar/1.0"}
BASE = "https://api.the-odds-api.com/v4"
API_KEY = os.environ.get("THE_ODDS_API", "")
OUT_PATH = Path("data/p3_guard_cache/taskD_reopen_game_calendar_result.json")

# Целевые окна по приоритету владельца (2026-09-10)
TARGETS = {
    "americanfootball_nfl": ("2025-09-01", "2025-12-31"),
    "basketball_nba": ("2026-03-01", "2026-05-31"),
    "soccer_epl": ("2025-08-15", "2026-01-31"),
    "mma_mixed_martial_arts": ("2025-08-15", "2026-06-30"),
}
STEP_DAYS = 7


def call(path: str, params: dict) -> tuple[int, dict | None, dict]:
    p = {"apiKey": API_KEY, **params}
    r = requests.get(f"{BASE}{path}", params=p, headers=HEADERS, timeout=25)
    usage = {"used": r.headers.get("x-requests-used"), "remaining": r.headers.get("x-requests-remaining")}
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body, usage


def run() -> int:
    if not API_KEY:
        print("[calendar] THE_ODDS_API не найден.")
        return 1

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sports": {}}
    total_calls = 0
    last_usage = {}

    for sport, (start_str, end_str) in TARGETS.items():
        start = datetime.fromisoformat(start_str).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(end_str).replace(tzinfo=timezone.utc)
        game_dates: set[str] = set()
        n_calls = 0
        t = start
        while t <= end:
            date_iso = t.strftime("%Y-%m-%dT12:00:00Z")
            status, body, usage = call(f"/historical/sports/{sport}/events", {"date": date_iso})
            n_calls += 1
            total_calls += 1
            last_usage = usage
            if status == 200 and isinstance(body, dict):
                for ev in body.get("data", []):
                    ct = ev.get("commence_time", "")[:10]
                    if ct and start_str <= ct <= end_str:
                        game_dates.add(ct)
            t += timedelta(days=STEP_DAYS)
            time.sleep(0.25)
        sorted_dates = sorted(game_dates)
        out["sports"][sport] = {
            "window": [start_str, end_str], "n_calendar_calls": n_calls,
            "n_distinct_game_dates": len(sorted_dates), "game_dates": sorted_dates,
        }
        print(f"[calendar] {sport}: {n_calls} вызовов /events -> {len(sorted_dates)} реальных игровых дней "
              f"в окне {start_str}..{end_str}")

    out["total_calendar_calls_credits"] = total_calls  # 1 кредит/вызов
    out["usage_after"] = last_usage

    # Реальный кредитный план: снимки 12/день (каждые 2ч), только на игровые дни + день до
    plan = {}
    total_odds_calls = 0
    for sport, info in out["sports"].items():
        days_with_buffer = set()
        for d in info["game_dates"]:
            dt = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
            days_with_buffer.add(d)
            days_with_buffer.add((dt - timedelta(days=1)).strftime("%Y-%m-%d"))
        n_days = len(days_with_buffer)
        n_calls = n_days * 12
        plan[sport] = {"n_game_days": len(info["game_dates"]), "n_days_with_buffer": n_days,
                        "n_odds_calls_at_12_per_day": n_calls, "credits": n_calls * 10}
        total_odds_calls += n_calls
    out["odds_fetch_plan"] = plan
    out["odds_fetch_plan_total_calls"] = total_odds_calls
    out["odds_fetch_plan_total_credits"] = total_odds_calls * 10

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[calendar] план /odds фетча (12/день, игровые дни+день до): {json.dumps(plan, indent=2, ensure_ascii=False)}")
    print(f"[calendar] ИТОГО calendar-вызовов: {total_calls} кредитов; план /odds: {total_odds_calls} вызовов "
          f"= {total_odds_calls * 10} кредитов")
    print(f"[calendar] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
