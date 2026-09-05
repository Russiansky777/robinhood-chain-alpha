#!/usr/bin/env python3
"""Задача C -- сопоставление спортивных рынков Kalshi <-> Polymarket по
играм за 30 дней (владелец, 2026-09-05). Только чтение, публичные API.

Kalshi: 6 реально подтверждённых игровых серий (`taskC_kalshi_series_
probe_result.json`, 2026-09-05) -- KXNFLGAME/KXMLBGAME/KXNBAGAME/
KXNCAAFGAME/KXEPLGAME/KXUFCFIGHT. Каждая игра -- один `event_ticker` с
2-3 рынками ("Team X wins"/"Team Y wins"/"Tie is the result") --
группируем по event_ticker, чтобы получить пару команд + время игры.

Polymarket: `tag_slug=`/`search=` РЕАЛЬНО не фильтруют (см. разведку
2026-09-05) -- рабочий путь: bulk-fetch active+closed рынков (большой
limit, сортировка по объёму/дате), клиентская фильтрация по наличию
названий команд обеих сторон в question/slug + близость даты
(разница c Kalshi close_time <= 2 дня, с запасом на разницу таймзон/
формата даты события).

Окно: 30 дней назад -> 7 дней вперёд от текущей даты (прошедшие
рассчитанные игры + ближайшие предстоящие, где ещё есть реальная
котировка на обеих площадках)."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskC-sports-matcher/1.0"}
OUT_PATH = Path("data/p3_guard_cache/taskC_sports_matcher_result.json")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_BASE = "https://gamma-api.polymarket.com"

KALSHI_SERIES = ["KXNFLGAME", "KXMLBGAME", "KXNBAGAME", "KXNCAAFGAME", "KXEPLGAME", "KXUFCFIGHT"]
WINDOW_DAYS_BACK = 30
WINDOW_DAYS_FWD = 7

# "Team X wins" / "Tie is the result" -- реальный формат title, встреченный в разведке
TITLE_WINS_RE = re.compile(r"^(.+?)\s+wins$", re.IGNORECASE)


def fetch_kalshi_games(series_ticker: str) -> list[dict]:
    games: dict[str, dict] = {}
    now = datetime.now(timezone.utc)
    min_close = now - timedelta(days=WINDOW_DAYS_BACK)
    max_close = now + timedelta(days=WINDOW_DAYS_FWD)
    for status in ("open", "settled"):
        cursor = None
        pages = 0
        while pages < 10:  # предохранитель -- максимум 1000 рынков на серию
            params = {"series_ticker": series_ticker, "limit": 100, "status": status}
            if cursor:
                params["cursor"] = cursor
            r = requests.get(f"{KALSHI_BASE}/markets", params=params, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                break
            body = r.json()
            markets = body.get("markets", [])
            if not markets:
                break
            for m in markets:
                close_time_str = m.get("close_time")
                if not close_time_str:
                    continue
                try:
                    close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if not (min_close <= close_time <= max_close):
                    continue
                ev = m.get("event_ticker")
                if ev not in games:
                    games[ev] = {"event_ticker": ev, "series": series_ticker, "close_time": close_time_str, "outcomes": []}
                title = m.get("title", "")
                win_match = TITLE_WINS_RE.match(title)
                team = win_match.group(1) if win_match else title
                games[ev]["outcomes"].append({"ticker": m.get("ticker"), "title": title, "team": team,
                                               "yes_bid": m.get("yes_bid_dollars"), "yes_ask": m.get("yes_ask_dollars")})
            cursor = body.get("cursor")
            pages += 1
            if not cursor:
                break
            time.sleep(0.3)
    return list(games.values())


def fetch_polymarket_bulk(max_pages: int = 6, page_size: int = 100) -> list[dict]:
    """Bulk-fetch -- ЕДИНСТВЕННЫЙ реально работающий путь (tag_slug/search
    не фильтруют, см. taskC_polymarket_btc_probe_result.json /
    taskC_sports_match_probe_result.json). ДВА среза, не один -- реальная
    находка первого прогона (2026-09-05): сортировка ТОЛЬКО по объёму
    почти не даёт NFL/NBA/UFC (игры ещё не начались, объём низкий) и
    систематически промахивается по конкретной дате в MLB-сериях (3
    игры одних команд подряд -- под объёмом всплывает только одна,
    остальные не видны matcher'у) -- поэтому дополнительно сортируем
    по endDate (ближайшие по времени рынки), с фильтром на реальное
    окно дат, чтобы конкретные игры (не только самые объёмные) попали
    в выборку."""
    out = {}
    for offset in range(0, max_pages * page_size, page_size):
        r = requests.get(f"{POLYMARKET_BASE}/markets", params={
            "limit": page_size, "offset": offset, "active": "true", "closed": "false",
            "order": "volume24hr", "ascending": "false",
        }, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            break
        body = r.json()
        if not isinstance(body, list) or not body:
            break
        for m in body:
            if m.get("slug"):
                out[m["slug"]] = m
        time.sleep(0.3)
    for offset in range(0, max_pages * page_size, page_size):
        r = requests.get(f"{POLYMARKET_BASE}/markets", params={
            "limit": page_size, "offset": offset, "active": "true", "closed": "false",
            "order": "endDate", "ascending": "true",
        }, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            break
        body = r.json()
        if not isinstance(body, list) or not body:
            break
        for m in body:
            if m.get("slug"):
                out[m["slug"]] = m
        time.sleep(0.3)
    return list(out.values())


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


DATE_DIFF_CONFIDENT_DAYS = 0.5  # владелец не задавал число -- MLB-серии реально дают
# несколько игр одних команд подряд (найдено 2026-09-05: 48/52 "совпадений"
# первого прогона были на самом деле ДРУГОЙ игрой той же серии, diff>=1 день) --
# без строгого порога matcher производит вводящие в заблуждение пары, честно
# отбрасываем кандидатов без блика в пределах суток, не подсовываем "похоже, но не то"


def match_game_to_polymarket(game: dict, pm_markets: list[dict]) -> list[dict]:
    teams = [normalize(o["team"]) for o in game["outcomes"] if o["team"] not in ("Tie is the result",)]
    if len(teams) < 2:
        return []
    try:
        game_date = datetime.fromisoformat(game["close_time"].replace("Z", "+00:00"))
    except ValueError:
        return []
    candidates, near_misses = [], 0
    for pm in pm_markets:
        q = normalize(pm.get("question", "") + " " + (pm.get("slug", "") or ""))
        if all(t and t in q for t in teams):
            end_date_str = pm.get("endDate")
            date_diff_days = None
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    date_diff_days = abs((end_date - game_date).total_seconds()) / 86400
                except ValueError:
                    pass
            if date_diff_days is None or date_diff_days > DATE_DIFF_CONFIDENT_DAYS:
                near_misses += 1  # команды совпали, дата нет -- честно считаем отдельно, не подсовываем как матч
                continue
            candidates.append({"question": pm.get("question"), "slug": pm.get("slug"),
                                "endDate": end_date_str, "date_diff_days": date_diff_days,
                                "outcomePrices": pm.get("outcomePrices"), "volume": pm.get("volumeNum")})
    candidates.sort(key=lambda c: c["date_diff_days"])
    return candidates, near_misses


def run() -> int:
    print("[matcher] Kalshi -- реальные игры за окно...")
    all_games = []
    for series in KALSHI_SERIES:
        games = fetch_kalshi_games(series)
        print(f"  {series}: {len(games)} реальных игр в окне")
        all_games.extend(games)
    print(f"[matcher] всего реальных игр Kalshi: {len(all_games)}")

    print("\n[matcher] Polymarket -- bulk-fetch активных рынков...")
    pm_markets = fetch_polymarket_bulk()
    print(f"[matcher] реальных активных рынков Polymarket загружено: {len(pm_markets)}")

    print("\n[matcher] сопоставление по названиям команд + дате (строгий порог "
          f"{DATE_DIFF_CONFIDENT_DAYS} дня)...")
    matched, total_near_misses = [], 0
    for game in all_games:
        cands, near_misses = match_game_to_polymarket(game, pm_markets)
        total_near_misses += near_misses
        if cands:
            matched.append({"kalshi_event": game["event_ticker"], "series": game["series"],
                             "close_time": game["close_time"],
                             "teams": [o["team"] for o in game["outcomes"]],
                             "polymarket_candidates": cands[:3]})

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_days_back": WINDOW_DAYS_BACK, "window_days_fwd": WINDOW_DAYS_FWD,
        "date_diff_confident_threshold_days": DATE_DIFF_CONFIDENT_DAYS,
        "n_kalshi_games": len(all_games), "n_polymarket_markets_scanned": len(pm_markets),
        "n_matched_confident": len(matched),
        "n_team_matched_but_date_rejected": total_near_misses,
        "matched": matched,
        "unmatched_sample": [g["event_ticker"] for g in all_games if g["event_ticker"] not in {m["kalshi_event"] for m in matched}][:30],
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    print(f"\n[matcher] РЕАЛЬНО СОПОСТАВЛЕНО (уверенно, diff<={DATE_DIFF_CONFIDENT_DAYS}д): {len(matched)} из {len(all_games)} игр Kalshi")
    print(f"[matcher] команды совпали, но дата НЕ прошла порог (вероятно другая игра той же серии): {total_near_misses}")
    print("\n=== первые 20 пар для ручной проверки владельцем ===")
    for m in matched[:20]:
        best = m["polymarket_candidates"][0]
        print(f"  Kalshi {m['kalshi_event']} ({m['teams']}, {m['close_time']}) <-> "
              f"Polymarket '{best['question']}' (endDate={best['endDate']}, diff={best['date_diff_days']}д)")
    print(f"\n[matcher] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
