#!/usr/bin/env python3
"""Владелец (2026-09-17): сборщик 2, ежедневный шаг -- обновление
списка сопоставленных пар Kalshi<->Polymarket. Переиспользует
`taskC_sports_matcher.py` (fetch_kalshi_games/fetch_polymarket_bulk/
match_game_to_polymarket) -- НЕ дублирует логику сопоставления, но
сохраняет БОЛЬШЕ деталей, чем итоговый отчёт матчера (там per-outcome
Kalshi-тикеры и Polymarket clobTokenIds не персистируются, только
агрегат "какие команды/даты совпали") -- снимку котировок нужны именно
тикеры и token id, не только факт совпадения.

Результат: `data/predmarket_collector/matched_pairs.json` -- список игр,
у каждой: kalshi event_ticker + список исходов (тикер, команда) +
лучший Polymarket-кандидат (slug, clobTokenIds, gameStartTime).
Перезаписывается целиком каждый день (в отличие от сборщика 1, здесь
нет смысла копить историю пар -- сами котировки копятся отдельно,
список пар -- просто "что сейчас сопоставлено")."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from taskC_sports_matcher import (  # noqa: E402
    KALSHI_SERIES, fetch_kalshi_games, fetch_polymarket_bulk, match_game_to_polymarket,
)

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def run() -> int:
    root = find_repo_root()
    out_dir = root.joinpath("data/predmarket_collector")
    out_dir.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    all_games = []
    for series in KALSHI_SERIES:
        games = fetch_kalshi_games(series)
        all_games.extend(games)
        print(f"[select_pairs] {series}: {len(games)} игр в окне")

    pm_markets = fetch_polymarket_bulk()
    print(f"[select_pairs] Polymarket рынков загружено: {len(pm_markets)}")

    pairs = []
    for game in all_games:
        cands, _near, _samples = match_game_to_polymarket(game, pm_markets)
        if not cands:
            continue
        best = cands[0]
        pairs.append({
            "kalshi_event_ticker": game["event_ticker"], "series": game["series"],
            "close_time": game["close_time"],
            "kalshi_outcomes": [{"ticker": o["ticker"], "team": o["team"]} for o in game["outcomes"]],
            "polymarket_slug": best.get("slug"), "polymarket_question": best.get("question"),
            "polymarket_endDate": best.get("endDate"), "polymarket_gameStartTime": best.get("gameStartTime"),
            "polymarket_clobTokenIds_raw": best.get("clobTokenIds"),
            "date_diff_days": best.get("date_diff_days"),
        })

    # РЕАЛЬНАЯ находка -- match_game_to_polymarket не возвращает clobTokenIds
    # в своём candidate dict (только question/slug/endDate/gameStartTime/
    # outcomePrices/volume, см. taskC_sports_matcher.py) -- нужен отдельный
    # точечный лукап по slug, чтобы получить сырые clobTokenIds для CLOB-книги.
    pm_by_slug = {m.get("slug"): m for m in pm_markets if m.get("slug")}
    for p in pairs:
        pm = pm_by_slug.get(p["polymarket_slug"])
        p["polymarket_clobTokenIds_raw"] = pm.get("clobTokenIds") if pm else None
        p["polymarket_outcomes"] = pm.get("outcomes") if pm else None

    out = {"generated_at_utc": now, "n_kalshi_games_scanned": len(all_games),
           "n_polymarket_markets_scanned": len(pm_markets), "n_pairs": len(pairs), "pairs": pairs}
    out_path = out_dir.joinpath("matched_pairs.json")
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[select_pairs] сопоставлено пар: {len(pairs)} -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
