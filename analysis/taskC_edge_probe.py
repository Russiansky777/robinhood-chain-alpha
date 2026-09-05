#!/usr/bin/env python3
"""Задача C -- разведка перед измерением экономики (владелец, 2026-09-05,
"сопоставление готово, экономика не измерена"). Только чтение.

Отвечает на 4 фактических вопроса, ни один не гадаем:
1. Полный (без cap на 3) список Polymarket-рынков по каждой из 4
   совпавших игр -- нужен, чтобы найти РЕАЛЬНЫЙ moneyline/winner/draw
   рынок для каждого исхода Kalshi (matched-список хранил только
   top-3 по date_diff, могли отрезать нужный тип рынка).
2. Реальная структура Kalshi orderbook (`/markets/{ticker}/orderbook`)
   -- сырой формат (yes/no бид-массивы), чтобы правильно вывести ask.
3. Реальная структура Polymarket CLOB order book
   (`clob.polymarket.com/book?token_id=...`) для реального clobTokenId.
4. Есть ли у Polymarket рынков поле `gameStartTime` (или дата в
   description) -- для MLB-серий, где team-name+endDate неоднозначны.
5. Отдают ли обе площадки историю цен на ЗАКРЫТЫЕ рынки -- Kalshi
   candlesticks/trades, Polymarket prices-history."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from taskC_sports_matcher import fetch_polymarket_bulk, normalize  # noqa: E402

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskC-edge-probe/1.0"}
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
OUT_PATH = Path("data/p3_guard_cache/taskC_edge_probe_result.json")

MATCHER_RESULT_PATH = Path("data/p3_guard_cache/taskC_sports_matcher_result.json")


def kalshi_get(path: str, params: dict | None = None):
    r = requests.get(f"{KALSHI_BASE}{path}", params=params or {}, headers=HEADERS, timeout=20)
    return r.status_code, (r.json() if r.status_code == 200 else r.text[:500])


def pm_get(base: str, path: str, params: dict | None = None):
    r = requests.get(f"{base}{path}", params=params or {}, headers=HEADERS, timeout=20)
    return r.status_code, (r.json() if r.status_code == 200 else r.text[:500])


def run() -> int:
    out = {}
    matcher_result = json.loads(MATCHER_RESULT_PATH.read_text())
    games = matcher_result["matched"]

    # --- 1. Kalshi: полный список рынков (все исходы) по каждому event_ticker ---
    print("[probe] --- 1. Kalshi -- полные исходы по event_ticker ---")
    out["kalshi_full_events"] = {}
    for g in games:
        ev = g["kalshi_event"]
        status, body = kalshi_get("/markets", {"event_ticker": ev})
        markets = body.get("markets", []) if status == 200 else []
        out["kalshi_full_events"][ev] = [{
            "ticker": m.get("ticker"), "title": m.get("title"),
            "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
            "no_bid": m.get("no_bid"), "no_ask": m.get("no_ask"),
            "yes_bid_dollars": m.get("yes_bid_dollars"), "yes_ask_dollars": m.get("yes_ask_dollars"),
            "no_bid_dollars": m.get("no_bid_dollars"), "no_ask_dollars": m.get("no_ask_dollars"),
            "status": m.get("status"),
        } for m in markets]
        print(f"  {ev}: {len(markets)} рынков (исходов)")
        time.sleep(0.3)

    # --- 2. Kalshi orderbook -- сырая структура для первого тикера первой игры ---
    print("\n[probe] --- 2. Kalshi orderbook -- сырая структура ---")
    first_ticker = None
    for ev, markets in out["kalshi_full_events"].items():
        if markets:
            first_ticker = markets[0]["ticker"]
            break
    out["kalshi_orderbook_sample"] = None
    if first_ticker:
        status, body = kalshi_get(f"/markets/{first_ticker}/orderbook")
        out["kalshi_orderbook_sample"] = {"ticker": first_ticker, "status": status, "body": body}
        print(f"  {first_ticker}: status={status}")
        print(json.dumps(body, indent=2, ensure_ascii=False)[:1500])

    # --- 3. Polymarket -- ПОЛНЫЙ список рынков по каждой игре (без cap на 3) ---
    print("\n[probe] --- 3. Polymarket -- полный список рынков по игре (без cap) ---")
    pm_markets = fetch_polymarket_bulk()
    print(f"[probe] реальных Polymarket-рынков загружено: {len(pm_markets)}")
    out["polymarket_full_candidates"] = {}
    first_clob_token = None
    for g in games:
        teams = [normalize(t) for t in g["teams"] if t != "Tie is the result"]
        full_cands = []
        for pm in pm_markets:
            q = normalize(pm.get("question", "") + " " + (pm.get("slug", "") or ""))
            if all(t and t in q for t in teams):
                full_cands.append({
                    "question": pm.get("question"), "slug": pm.get("slug"),
                    "outcomes": pm.get("outcomes"), "outcomePrices": pm.get("outcomePrices"),
                    "clobTokenIds": pm.get("clobTokenIds"), "endDate": pm.get("endDate"),
                    "gameStartTime": pm.get("gameStartTime"), "description": (pm.get("description") or "")[:200],
                    "closed": pm.get("closed"), "active": pm.get("active"),
                })
                if first_clob_token is None and pm.get("clobTokenIds"):
                    try:
                        ids = json.loads(pm["clobTokenIds"]) if isinstance(pm["clobTokenIds"], str) else pm["clobTokenIds"]
                        if ids:
                            first_clob_token = ids[0]
                    except (ValueError, TypeError):
                        pass
        out["polymarket_full_candidates"][g["kalshi_event"]] = full_cands
        print(f"  {g['kalshi_event']} ({g['teams']}): {len(full_cands)} реальных PM-рынков найдено")
        for c in full_cands:
            print(f"     '{c['question']}' outcomes={c['outcomes']} gameStartTime={c['gameStartTime']}")

    # --- 4. Polymarket CLOB order book -- сырая структура ---
    print("\n[probe] --- 4. Polymarket CLOB order book -- сырая структура ---")
    out["clob_orderbook_sample"] = None
    if first_clob_token:
        status, body = pm_get(CLOB_BASE, "/book", {"token_id": first_clob_token})
        out["clob_orderbook_sample"] = {"token_id": first_clob_token, "status": status, "body": body}
        print(f"  token_id={first_clob_token}: status={status}")
        print(json.dumps(body, indent=2, ensure_ascii=False)[:1500])

    # --- 5a. MLB gameStartTime -- прямой поиск известного MLB-матчапа (near-miss из прошлого прогона) ---
    print("\n[probe] --- 5a. MLB -- проверка gameStartTime/description на реальном примере ---")
    mlb_cands = [pm for pm in pm_markets if "torontobluejays" in normalize(pm.get("question", "") + pm.get("slug", "") or "")
                 and "kansascityroyals" in normalize(pm.get("question", "") + (pm.get("slug", "") or ""))]
    out["mlb_gamestarttime_check"] = [{
        "question": m.get("question"), "slug": m.get("slug"), "endDate": m.get("endDate"),
        "gameStartTime": m.get("gameStartTime"), "startDate": m.get("startDate"),
        "description": (m.get("description") or "")[:300],
    } for m in mlb_cands]
    print(json.dumps(out["mlb_gamestarttime_check"], indent=2, ensure_ascii=False)[:3000])

    # --- 5b. История цен на ЗАКРЫТЫЕ рынки -- обе площадки ---
    print("\n[probe] --- 5b. История цен на closed-рынках ---")
    # Kalshi: берём любой settled market из уже известных серий (реальный старый тикер)
    status, body = kalshi_get("/markets", {"series_ticker": "KXNFLGAME", "limit": 1, "status": "settled"})
    settled_ticker = None
    if status == 200 and body.get("markets"):
        settled_ticker = body["markets"][0]["ticker"]
    out["kalshi_history_check"] = {"settled_ticker": settled_ticker}
    if settled_ticker:
        # candlesticks -- реальный путь требует series_ticker отдельно
        series_ticker = settled_ticker.split("-")[0]
        status_c, body_c = kalshi_get(f"/series/{series_ticker}/markets/{settled_ticker}/candlesticks",
                                       {"start_ts": 1735689600, "end_ts": int(time.time()), "period_interval": 60})
        out["kalshi_history_check"]["candlesticks_status"] = status_c
        out["kalshi_history_check"]["candlesticks_sample"] = str(body_c)[:1000]
        status_t, body_t = kalshi_get("/markets/trades", {"ticker": settled_ticker, "limit": 5})
        out["kalshi_history_check"]["trades_status"] = status_t
        out["kalshi_history_check"]["trades_sample"] = str(body_t)[:1000]
        print(f"  Kalshi {settled_ticker}: candlesticks status={status_c}, trades status={status_t}")

    # Polymarket: берём любой closed market из bulk (сейчас все свежие -- ищем явно closed=true)
    closed_pm = next((m for m in pm_markets if m.get("closed")), None)
    out["polymarket_history_check"] = {"slug": closed_pm.get("slug") if closed_pm else None}
    if closed_pm and closed_pm.get("clobTokenIds"):
        try:
            ids = json.loads(closed_pm["clobTokenIds"]) if isinstance(closed_pm["clobTokenIds"], str) else closed_pm["clobTokenIds"]
            token = ids[0] if ids else None
        except (ValueError, TypeError):
            token = None
        if token:
            status_h, body_h = pm_get(CLOB_BASE, "/prices-history", {"market": token, "fidelity": 60})
            out["polymarket_history_check"]["status"] = status_h
            out["polymarket_history_check"]["sample"] = str(body_h)[:1000]
            print(f"  Polymarket {closed_pm['slug']}: prices-history status={status_h}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[probe] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
