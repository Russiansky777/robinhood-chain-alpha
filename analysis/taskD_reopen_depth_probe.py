#!/usr/bin/env python3
"""Задача D переоткрыта повторно (владелец, 2026-09-10): не откладывать
до октября -- выгрузить СЫГРАННЫЕ сезоны (NFL сен-дек 2025, NBA мар-май
2026, EPL/UFC), кредиты (~9129 остаток) не экономить. ПЕРЕД дорогим
фетчем -- дешёвая реальная проверка (эта задача):

1. Реальная глубина истории The Odds API: до какой даты назад
   /v4/historical/.../events реально отдаёт данные (1 кредит/вызов,
   бисекция по нескольким датам -- не гадаем, не по документации).
2. Реальные ключи спорта для EPL/UFC (`/v4/sports`, бесплатно) --
   не берём общеизвестные ключи по памяти, а конфирмируем разведкой.
3. Реальное покрытие Pinnacle по EPL/UFC (1 вызов /events на спорт,
   1 кредит) -- прежде чем планировать дорогой снимочный фетч."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskD-reopen-depth-probe/1.0"}
BASE = "https://api.the-odds-api.com/v4"
API_KEY = os.environ.get("THE_ODDS_API", "")
OUT_PATH = Path("data/p3_guard_cache/taskD_reopen_depth_probe_result.json")


def call(path: str, params: dict) -> tuple[int, dict | list | None, dict]:
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
        print("[reopen_probe] THE_ODDS_API не найден.")
        return 1

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # 1. Реальные ключи спорта
    status, sports, usage = call("/sports", {"all": "true"})
    out["sports_call_status"] = status
    out["sports_usage_after"] = usage
    epl_keys = [s for s in (sports or []) if isinstance(s, dict) and
                ("premier league" in (s.get("title") or "").lower() or "epl" in (s.get("key") or "").lower())]
    ufc_keys = [s for s in (sports or []) if isinstance(s, dict) and
                ("mma" in (s.get("key") or "").lower() or "ufc" in (s.get("title") or "").lower())]
    out["epl_candidate_keys"] = epl_keys
    out["ufc_candidate_keys"] = ufc_keys
    print(f"[reopen_probe] EPL кандидаты: {epl_keys}")
    print(f"[reopen_probe] UFC кандидаты: {ufc_keys}")

    # 2. Реальная глубина истории -- бисекция по /events (1 кредит/вызов), спорт=NFL (сезон сен-дек 2025)
    probe_dates = [
        "2025-09-15T12:00:00Z", "2025-11-01T12:00:00Z", "2025-12-15T12:00:00Z",
        "2025-08-01T12:00:00Z", "2025-06-01T12:00:00Z", "2025-01-01T12:00:00Z",
    ]
    depth_results = []
    for d in probe_dates:
        status, body, usage = call("/historical/sports/americanfootball_nfl/events", {"date": d})
        n_events = len(body.get("data", [])) if isinstance(body, dict) else None
        depth_results.append({"date": d, "status": status, "n_events": n_events, "usage_after": usage})
        print(f"[reopen_probe] глубина NFL {d}: status={status} n_events={n_events} used={usage.get('used')}")
        time.sleep(0.3)
    out["nfl_depth_probe"] = depth_results

    # 3. Реальное покрытие Pinnacle EPL/UFC -- один /events (1 кредит) на каждый реально найденный ключ,
    # затем один /odds с bookmakers=pinnacle (10 кредитов) ТОЛЬКО если events нашлись
    coverage = {}
    for label, keys in (("epl", epl_keys), ("ufc", ufc_keys)):
        if not keys:
            coverage[label] = {"blocker": "реальный ключ спорта не найден в /v4/sports"}
            continue
        key = keys[0]["key"]
        test_date = "2025-10-15T12:00:00Z"  # середина сезона EPL 2025/26 и активный период UFC
        status, body, usage = call(f"/historical/sports/{key}/events", {"date": test_date})
        n_events = len(body.get("data", [])) if isinstance(body, dict) else None
        entry = {"key": key, "events_status": status, "n_events": n_events, "events_usage_after": usage}
        if n_events:
            status2, body2, usage2 = call(f"/historical/sports/{key}/odds", {
                "regions": "eu", "markets": "h2h", "bookmakers": "pinnacle",
                "date": test_date, "oddsFormat": "decimal",
            })
            n_with_pinnacle = 0
            if isinstance(body2, dict):
                for ev in body2.get("data", []):
                    if any(b.get("key") == "pinnacle" for b in ev.get("bookmakers", [])):
                        n_with_pinnacle += 1
            entry.update({"odds_status": status2, "n_events_with_pinnacle": n_with_pinnacle, "odds_usage_after": usage2})
        coverage[label] = entry
        print(f"[reopen_probe] покрытие {label} ({key}): {entry}")
        time.sleep(0.3)
    out["pinnacle_coverage"] = coverage

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[reopen_probe] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
