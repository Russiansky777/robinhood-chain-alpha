#!/usr/bin/env python3
"""Задача D (переоткрыта, 2026-09-10) -- РЕАЛЬНЫЙ ключ куплен (план 20K,
$30/мес, secrets.THE_ODDS_API). Перед любой массовой выгрузкой -- посчитать
бюджет запросов В КРЕДИТАХ.

Владелец прямо просил: "по документации The Odds API: стоимость одного
исторического запроса в кредитах". Документация (the-odds-api.com)
заблокирована сетевой политикой и для sandbox, и для WebFetch -- вместо
того чтобы полагаться на текст документации (который мог устареть, как
уже было с Lyra/Derive в Задаче F), измеряем РЕАЛЬНУЮ стоимость ОДНОГО
минимального исторического запроса напрямую по заголовкам ответа API
(x-requests-used/x-requests-remaining/x-requests-last -- The Odds API
документированно возвращает их на каждый запрос), затем экстраполируем
на весь план (3 вида спорта x 30 дней x N снимков), НЕ выполняя массовую
выгрузку в этом скрипте.

Ключ ТОЛЬКО из переменной окружения THE_ODDS_API (передаётся через GH
Actions secrets) -- нигде не печатается и не пишется в результат."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskD-credit-budget/1.0"}
BASE = "https://api.the-odds-api.com/v4"
OUT_PATH = Path("data/p3_guard_cache/taskD_odds_credit_budget_result.json")

API_KEY = os.environ.get("THE_ODDS_API", "")

SPORTS = ["americanfootball_nfl", "basketball_nba", "baseball_mlb"]
WINDOW_DAYS = 30
MONTHLY_CREDIT_LIMIT = 20000


def usage_headers(r: requests.Response) -> dict:
    # Реальные заголовки The Odds API -- читаем как есть, ничего не
    # предполагаем сверх того, что реально пришло.
    return {
        "x-requests-used": r.headers.get("x-requests-used"),
        "x-requests-remaining": r.headers.get("x-requests-remaining"),
        "x-requests-last": r.headers.get("x-requests-last"),
    }


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not API_KEY:
        result["blocker"] = "THE_ODDS_API не найден в окружении -- секрет не передан или не настроен."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"[taskD_credit_budget] {result['blocker']}")
        return 1

    # 1. Реально бесплатный (или дешёвый) список видов спорта -- подтверждает
    # рабочий ключ и реальные текущие sport-key, не по памяти.
    r0 = requests.get(f"{BASE}/sports", params={"apiKey": API_KEY}, headers=HEADERS, timeout=20)
    result["sports_endpoint_status"] = r0.status_code
    result["sports_endpoint_usage"] = usage_headers(r0)
    if r0.status_code == 200:
        sports_list = r0.json()
        real_sport_keys = {s["key"] for s in sports_list if isinstance(s, dict) and "key" in s}
        result["n_sports_listed"] = len(sports_list)
        result["target_sports_found"] = {s: (s in real_sport_keys) for s in SPORTS}
    else:
        result["sports_endpoint_body_snippet"] = r0.text[:500]
        result["blocker"] = f"GET /sports вернул {r0.status_code} -- ключ невалиден или площадка недоступна, останавливаюсь до выгрузки."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"[taskD_credit_budget] {result['blocker']}")
        return 1

    used_before = int(result["sports_endpoint_usage"]["x-requests-used"] or 0)

    # 2. Список исторических событий для ОДНОГО вида спорта на ОДНУ дату в
    # окне 30 дней -- проверяем, берёт ли этот эндпоинт кредиты (некоторые
    # площадки список событий дают бесплатно, платят только за сами odds).
    probe_date = (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r1 = requests.get(f"{BASE}/historical/sports/{SPORTS[0]}/events", params={
        "apiKey": API_KEY, "date": probe_date,
    }, headers=HEADERS, timeout=20)
    result["historical_events_status"] = r1.status_code
    result["historical_events_usage"] = usage_headers(r1)
    n_events_sample = 0
    sample_event_id = None
    if r1.status_code == 200:
        body1 = r1.json()
        events = body1.get("data", body1) if isinstance(body1, dict) else body1
        if isinstance(events, list):
            n_events_sample = len(events)
            if events:
                sample_event_id = events[0].get("id")
    result["historical_events_n_sample"] = n_events_sample
    result["historical_events_body_snippet"] = r1.text[:600]

    used_after_events = int((result["historical_events_usage"]["x-requests-used"] or used_before))
    result["credits_used_by_events_call"] = used_after_events - used_before

    # 3. РЕАЛЬНЫЙ минимальный исторический odds-запрос -- 1 спорт, 1 регион,
    # 1 маркет (h2h), фильтр на pinnacle. Pinnacle в The Odds API реально
    # относится к региону "eu" (проверяем эмпирически по факту наличия
    # pinnacle в ответе, не по памяти).
    r2 = requests.get(f"{BASE}/historical/sports/{SPORTS[0]}/odds", params={
        "apiKey": API_KEY, "regions": "eu", "markets": "h2h",
        "bookmakers": "pinnacle", "date": probe_date, "oddsFormat": "decimal",
    }, headers=HEADERS, timeout=20)
    result["historical_odds_probe_status"] = r2.status_code
    result["historical_odds_probe_usage"] = usage_headers(r2)
    result["historical_odds_probe_body_snippet"] = r2.text[:800]
    n_events_with_pinnacle = 0
    if r2.status_code == 200:
        body2 = r2.json()
        events2 = body2.get("data", body2) if isinstance(body2, dict) else body2
        if isinstance(events2, list):
            for ev in events2:
                bms = ev.get("bookmakers", []) if isinstance(ev, dict) else []
                if any(b.get("key") == "pinnacle" for b in bms):
                    n_events_with_pinnacle += 1
            result["historical_odds_probe_n_events_total"] = len(events2)
    result["historical_odds_probe_n_events_with_pinnacle"] = n_events_with_pinnacle

    used_after_odds = int((result["historical_odds_probe_usage"]["x-requests-used"] or used_after_events))
    real_credits_per_snapshot_call = used_after_odds - used_after_events
    result["real_credits_per_single_snapshot_call"] = real_credits_per_snapshot_call

    # 4. Экстраполяция на весь план -- 3 вида спорта x 30 дней x N снимков/день,
    # используя РЕАЛЬНУЮ измеренную стоимость одного вызова (не формулу из
    # документации, которую я не смог проверить напрямую).
    result["extrapolation"] = {}
    for snaps_per_day in (1, 2, 4, 6, 8, 12, 24):
        total_calls = len(SPORTS) * WINDOW_DAYS * snaps_per_day
        total_credits = total_calls * real_credits_per_snapshot_call
        result["extrapolation"][f"{snaps_per_day}_snapshots_per_day"] = {
            "total_calls": total_calls,
            "total_credits": total_credits,
            "fits_budget_20000": total_credits <= MONTHLY_CREDIT_LIMIT,
            "pct_of_monthly_limit": round(100 * total_credits / MONTHLY_CREDIT_LIMIT, 1),
        }

    result["total_credits_used_this_probe"] = used_after_odds - used_before
    result["monthly_limit"] = MONTHLY_CREDIT_LIMIT
    result["remaining_after_probe"] = int(result["historical_odds_probe_usage"]["x-requests-remaining"] or -1)

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"[taskD_credit_budget] реальная стоимость 1 снимка (1 спорт, regions=eu, markets=h2h, "
          f"bookmakers=pinnacle): {real_credits_per_snapshot_call} кредитов")
    print(f"[taskD_credit_budget] потрачено этим прогоном разведки: {result['total_credits_used_this_probe']} кредитов")
    print(f"[taskD_credit_budget] остаток лимита после разведки: {result['remaining_after_probe']}")
    for k, v in result["extrapolation"].items():
        print(f"  {k}: {v['total_calls']} вызовов -> {v['total_credits']} кредитов "
              f"({v['pct_of_monthly_limit']}% лимита), укладывается: {v['fits_budget_20000']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
