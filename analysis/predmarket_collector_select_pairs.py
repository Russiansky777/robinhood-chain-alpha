#!/usr/bin/env python3
"""Владелец (2026-09-17, продолжение): сборщик 2, ежедневный шаг --
обновление списка сопоставленных пар Kalshi<->Polymarket.

ЧЕСТНАЯ НАХОДКА (реальный первый прогон дал 0 пар, владелец потребовал
разобраться СЕГОДНЯ): историческая проверка того же матчера
(`data/p3_guard_cache/taskC_sports_matcher_result.json`, 2026-09-05)
дала **2** уверенных совпадения из 815 игр (256 "команды совпали, дата
нет") -- НЕ 4, как вспоминалось; то есть выход матчера уже тогда был
низким, а не "много -> ноль". Сегодня (12 дней спустя, окно 30 назад/7
вперёд) -- 0 из 1091 игр (n_team_matched_but_date_rejected раньше НЕ
персистировался в этом сборщике -- теперь сохраняется явно, см. ниже,
для честного сравнения на следующих прогонах). Метод сопоставления НЕ
менялся (тот же импорт из taskC_sports_matcher.py, тот же порог
0.5 дня, та же приоритизация gameStartTime над endDate).

ВЛАДЕЛЕЦ ЯВНО РАЗРЕШИЛ ФОЛБЭК: "если сопоставление в принципе не
выходит -- снимать котировки БЕЗ сопоставления". Реализовано: ПОМИМО
`pairs` (сопоставленные, могут быть пустыми) -- ДВА
независимых списка ликвидных рынков без пары:
  - `standalone_kalshi`: все исходы уже реально загруженных спортивных
    игр, у которых есть РЕАЛЬНАЯ двусторонняя котировка (yes_bid И
    yes_ask оба присутствуют -- признак живого рынка, не просто
    листинга) -- 0 доп. запросов, те же данные, что уже загружены для
    матчинга.
  - `standalone_polymarket`: топ-N по `volume24hr` (первый проход
    fetch_polymarket_bulk уже отсортирован по этому полю по убыванию)
    -- тоже 0 доп. запросов.
Оба списка ограничены разумным числом (см. константы), чтобы снимок
каждые 5 минут физически укладывался по времени.

Результат: `data/predmarket_collector/matched_pairs.json`."""
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
MAX_STANDALONE_KALSHI = 150
MAX_STANDALONE_POLYMARKET = 100


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
    total_near_misses = 0
    near_miss_samples = []
    for game in all_games:
        cands, near_misses, samples = match_game_to_polymarket(game, pm_markets)
        total_near_misses += near_misses
        if len(near_miss_samples) < 20:
            near_miss_samples.extend(samples)
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
    print(f"[select_pairs] сопоставлено пар: {len(pairs)}, "
          f"команды совпали/дата нет: {total_near_misses}")

    # РЕАЛЬНАЯ находка -- match_game_to_polymarket не возвращает clobTokenIds
    # в своём candidate dict -- нужен отдельный точечный лукап по slug.
    pm_by_slug = {m.get("slug"): m for m in pm_markets if m.get("slug")}
    for p in pairs:
        pm = pm_by_slug.get(p["polymarket_slug"])
        p["polymarket_clobTokenIds_raw"] = pm.get("clobTokenIds") if pm else None
        p["polymarket_outcomes"] = pm.get("outcomes") if pm else None

    # --- Фолбэк, явно разрешённый владельцем: ликвидные рынки БЕЗ пары ---
    standalone_kalshi = []
    for game in all_games:
        for o in game["outcomes"]:
            if o.get("yes_bid") is not None and o.get("yes_ask") is not None:
                standalone_kalshi.append({
                    "ticker": o["ticker"], "team": o["team"], "series": game["series"],
                    "kalshi_event_ticker": game["event_ticker"], "close_time": game["close_time"],
                })
            if len(standalone_kalshi) >= MAX_STANDALONE_KALSHI:
                break
        if len(standalone_kalshi) >= MAX_STANDALONE_KALSHI:
            break

    # первый проход fetch_polymarket_bulk (active=true, order=volume24hr desc) --
    # уже самые ликвидные РЕАЛЬНО активные рынки Polymarket по факту сортировки
    # источника, не наше предположение.
    standalone_polymarket = []
    for m in pm_markets:
        if not m.get("clobTokenIds") or not m.get("slug"):
            continue
        standalone_polymarket.append({
            "slug": m["slug"], "question": m.get("question"),
            "clobTokenIds_raw": m.get("clobTokenIds"), "outcomes_raw": m.get("outcomes"),
            "volumeNum": m.get("volumeNum"),
        })
        if len(standalone_polymarket) >= MAX_STANDALONE_POLYMARKET:
            break

    out = {
        "generated_at_utc": now, "n_kalshi_games_scanned": len(all_games),
        "n_polymarket_markets_scanned": len(pm_markets), "n_pairs": len(pairs), "pairs": pairs,
        "n_team_matched_but_date_rejected": total_near_misses,
        "near_miss_samples": near_miss_samples[:20],
        "n_standalone_kalshi": len(standalone_kalshi), "standalone_kalshi": standalone_kalshi,
        "n_standalone_polymarket": len(standalone_polymarket), "standalone_polymarket": standalone_polymarket,
    }
    out_path = out_dir.joinpath("matched_pairs.json")
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[select_pairs] пар={len(pairs)} standalone_kalshi={len(standalone_kalshi)} "
          f"standalone_polymarket={len(standalone_polymarket)} -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
