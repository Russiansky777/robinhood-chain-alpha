#!/usr/bin/env python3
"""Задача C -- живой замер спреда ПРЯМО СЕЙЧАС (владелец, п.1, 2026-09-05).

РЕАЛЬНАЯ НАХОДКА (taskC_edge_probe_result.json): все 4 изначально
совпавшие игры к моменту разведки уже `status: finalized` на Kalshi
(yes_ask=no_ask=1.00 -- замороженное пост-матчевое состояние, НЕ живая
котировка). Матчинг+диагностика+разведка заняли ~50 минут реального
времени -- ровно за это время все 4 игры начались и закончились.
Считать "спред" на замороженных $1.00/$1.00 котировках значило бы
подделывать данные, а не измерять их.

Также найдено: у Polymarket для этих EPL-матчей НЕТ moneyline
("team wins") рынка вообще -- только Draw (соответствует Kalshi "Tie
is the result") и O/U/exact-score рынки. Сопоставимая по семантике
пара реально существует только для: (а) Tie на Kalshi <-> Draw на
Polymarket (EPL), (б) один боец против другого на Kalshi <-> общий
fight-winner рынок на Polymarket (UFC).

Эта версия ищет РЕАЛЬНО ЖИВЫЕ (Kalshi status=open, ещё не начались или
идут) игры из тех же 6 серий, сопоставляет Tie/Draw и UFC-fight пары,
и считает формулу владельца на них -- честно, только если оба стакана
реально не пустые."""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from taskC_sports_matcher import KALSHI_SERIES, fetch_polymarket_bulk, normalize  # noqa: E402

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskC-edge-live/1.0"}
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CLOB_BASE = "https://clob.polymarket.com"
OUT_PATH = Path("data/p3_guard_cache/taskC_edge_live_now_result.json")


def kalshi_fee(price_dollars: float) -> float:
    """Реальная формула комиссии Kalshi (владелец, п.1): ceil(0.07*P*(1-P)*100)/100,
    P -- цена в долларах (0..1)."""
    return math.ceil(0.07 * price_dollars * (1 - price_dollars) * 100) / 100


def fetch_live_kalshi_games() -> list[dict]:
    """Реальные Kalshi-игры со статусом open (ещё торгуемые) -- НЕ settled,
    НЕ finalized."""
    games: dict[str, dict] = {}
    for series in KALSHI_SERIES:
        r = requests.get(f"{KALSHI_BASE}/markets", params={"series_ticker": series, "limit": 100, "status": "open"},
                          headers=HEADERS, timeout=20)
        if r.status_code != 200:
            continue
        for m in r.json().get("markets", []):
            if m.get("status") != "active":  # реально живой, не просто "open" статус запроса
                continue
            ev = m.get("event_ticker")
            if ev not in games:
                games[ev] = {"event_ticker": ev, "series": series, "close_time": m.get("close_time"), "outcomes": []}
            games[ev]["outcomes"].append({"ticker": m.get("ticker"), "title": m.get("title"),
                                           "yes_bid_dollars": m.get("yes_bid_dollars"), "yes_ask_dollars": m.get("yes_ask_dollars"),
                                           "no_bid_dollars": m.get("no_bid_dollars"), "no_ask_dollars": m.get("no_ask_dollars")})
        time.sleep(0.3)
    return list(games.values())


def kalshi_orderbook(ticker: str) -> dict:
    r = requests.get(f"{KALSHI_BASE}/markets/{ticker}/orderbook", headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return {}
    return r.json().get("orderbook_fp", {})


def kalshi_best_yes_no_ask(ticker: str) -> tuple[float | None, float | None]:
    """Реальный стакан -- yes_dollars/no_dollars это БИДЫ на каждую сторону
    (подтверждено эмпирически, taskC_edge_probe_result.json). ask одной
    стороны = 1 - лучший бид другой стороны (комплементарная книга)."""
    ob = kalshi_orderbook(ticker)
    yes_bids = ob.get("yes_dollars") or []
    no_bids = ob.get("no_dollars") or []
    best_yes_bid = max((float(p) for p, _ in yes_bids), default=None)
    best_no_bid = max((float(p) for p, _ in no_bids), default=None)
    yes_ask = round(1 - best_no_bid, 4) if best_no_bid is not None else None
    no_ask = round(1 - best_yes_bid, 4) if best_yes_bid is not None else None
    return yes_ask, no_ask


def clob_best_ask(token_id: str) -> float | None:
    r = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return None
    asks = r.json().get("asks", [])
    if not asks:
        return None
    return min(float(a["price"]) for a in asks)


def compute_spread(yes_k_ask: float, no_k_ask: float, yes_p_ask: float, no_p_ask: float) -> dict:
    combo_a = yes_k_ask + kalshi_fee(yes_k_ask) + no_p_ask
    combo_b = yes_p_ask + no_k_ask + kalshi_fee(no_k_ask)
    return {"yes_k_ask": yes_k_ask, "no_k_ask": no_k_ask, "yes_p_ask": yes_p_ask, "no_p_ask": no_p_ask,
            "combo_a_yesK_noP": round(combo_a, 4), "combo_b_yesP_noK": round(combo_b, 4),
            "min_cost": round(min(combo_a, combo_b), 4), "is_arb": min(combo_a, combo_b) < 1.0}


def run() -> int:
    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    print("[live] Kalshi -- реально живые (status=active) игры сейчас...")
    live_games = fetch_live_kalshi_games()
    print(f"[live] реально живых игр (все серии): {len(live_games)}")
    out["n_live_kalshi_games"] = len(live_games)

    print("[live] Polymarket -- bulk-fetch (active) для сопоставления...")
    pm_markets = fetch_polymarket_bulk()
    print(f"[live] реальных Polymarket-рынков: {len(pm_markets)}")

    results = []
    for game in live_games:
        tie_ticker = next((o["ticker"] for o in game["outcomes"] if o["title"] == "Tie is the result"), None)
        team_names = [o["title"].replace(" wins", "") for o in game["outcomes"] if o["title"] != "Tie is the result"]

        # --- EPL: Tie (Kalshi) <-> Draw (Polymarket) ---
        if tie_ticker and len(team_names) == 2:
            teams_norm = [normalize(t) for t in team_names]
            pm_draw = next((pm for pm in pm_markets
                             if all(t in normalize(pm.get("question", "") + (pm.get("slug", "") or "")) for t in teams_norm)
                             and "draw" in normalize(pm.get("question", ""))), None)
            if pm_draw:
                yes_k_ask, no_k_ask = kalshi_best_yes_no_ask(tie_ticker)
                try:
                    token_ids = json.loads(pm_draw["clobTokenIds"]) if isinstance(pm_draw["clobTokenIds"], str) else pm_draw["clobTokenIds"]
                except (ValueError, TypeError, KeyError):
                    token_ids = None
                yes_p_ask = clob_best_ask(token_ids[0]) if token_ids else None
                no_p_ask = clob_best_ask(token_ids[1]) if token_ids and len(token_ids) > 1 else None
                entry = {"kalshi_event": game["event_ticker"], "type": "tie_vs_draw",
                          "kalshi_tie_ticker": tie_ticker, "polymarket_question": pm_draw.get("question"),
                          "yes_k_ask": yes_k_ask, "no_k_ask": no_k_ask, "yes_p_ask": yes_p_ask, "no_p_ask": no_p_ask}
                if None not in (yes_k_ask, no_k_ask, yes_p_ask, no_p_ask):
                    entry["spread"] = compute_spread(yes_k_ask, no_k_ask, yes_p_ask, no_p_ask)
                else:
                    entry["spread"] = None
                    entry["reason_no_spread"] = "пустой стакан (нет реальных котировок) на одной из сторон -- честно не считаем"
                results.append(entry)
                print(f"[live] {game['event_ticker']} Tie<->Draw: {entry}")

        # --- UFC: fighter A wins <-> fight-winner market (Polymarket, 2 outcomes = 2 бойца) ---
        if game["series"] == "KXUFCFIGHT" and len(team_names) == 2:
            teams_norm = [normalize(t) for t in team_names]
            pm_fight = next((pm for pm in pm_markets
                              if all(t in normalize(pm.get("question", "") + (pm.get("slug", "") or "")) for t in teams_norm)), None)
            if pm_fight:
                fighter_a_ticker = next(o["ticker"] for o in game["outcomes"] if o["title"] == f"{team_names[0]} wins")
                yes_k_ask, no_k_ask = kalshi_best_yes_no_ask(fighter_a_ticker)
                try:
                    token_ids = json.loads(pm_fight["clobTokenIds"]) if isinstance(pm_fight["clobTokenIds"], str) else pm_fight["clobTokenIds"]
                except (ValueError, TypeError, KeyError):
                    token_ids = None
                yes_p_ask = clob_best_ask(token_ids[0]) if token_ids else None
                no_p_ask = clob_best_ask(token_ids[1]) if token_ids and len(token_ids) > 1 else None
                entry = {"kalshi_event": game["event_ticker"], "type": "fight_winner",
                          "kalshi_ticker": fighter_a_ticker, "polymarket_question": pm_fight.get("question"),
                          "yes_k_ask": yes_k_ask, "no_k_ask": no_k_ask, "yes_p_ask": yes_p_ask, "no_p_ask": no_p_ask}
                if None not in (yes_k_ask, no_k_ask, yes_p_ask, no_p_ask):
                    entry["spread"] = compute_spread(yes_k_ask, no_k_ask, yes_p_ask, no_p_ask)
                else:
                    entry["spread"] = None
                    entry["reason_no_spread"] = "пустой стакан на одной из сторон -- честно не считаем"
                results.append(entry)
                print(f"[live] {game['event_ticker']} fight_winner: {entry}")

    out["results"] = results
    out["n_with_real_spread"] = sum(1 for r in results if r.get("spread"))
    out["n_arb_found"] = sum(1 for r in results if r.get("spread") and r["spread"]["is_arb"])
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[live] пар с реальным (не пустым) стаканом на обеих площадках: {out['n_with_real_spread']} из {len(results)}")
    print(f"[live] найдено арбитражных возможностей (min_cost<$1.00): {out['n_arb_found']}")
    print(f"[live] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
