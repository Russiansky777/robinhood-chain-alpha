#!/usr/bin/env python3
"""Задача D (переоткрыта, 2026-09-10) -- БЕСПЛАТНЫЙ анализ. Читает уже
сохранённые (реально оплаченные) снимки Pinnacle из
taskD_pinnacle_snapshots.json, сопоставляет события с Polymarket ЧЕРЕЗ
УЖЕ НАПИСАННЫЙ taskC_sports_matcher.py (импортируем функции, не
переписываем -- прямое указание владельца), берёт историческую цену
Polymarket на момент каждого снимка через публичный (бесплатный) CLOB
prices-history, считает расхождение и его живучесть.

Переформулированная владельцем предрегистрация (2026-09-10, экономическая,
не микроструктурная): линия жива, если |Pinnacle no-vig - Polymarket|
после учёта издержек >= 2% встречается на >= 15% сопоставленных
событий-снимков, И это расхождение (знак + размер >= 1%) сохраняется до
следующего снимка (через 2 часа) более чем в половине таких случаев."""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from taskC_sports_matcher import fetch_polymarket_bulk, normalize  # НЕ переписываем -- реально уже написан и работал

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskD-pinnacle-analysis/1.0"}
CLOB_BASE = "https://clob.polymarket.com"

IN_PATH = Path("data/p3_guard_cache/taskD_pinnacle_snapshots.json")
OUT_PATH = Path("data/p3_guard_cache/taskD_pinnacle_polymarket_result.json")

DISCREPANCY_THRESHOLD = 0.02  # владелец: >=2%
PERSISTENCE_MIN_SIZE = 0.01  # владелец: сохранилось "размера >=1%"
DATE_DIFF_CONFIDENT_DAYS = 1.0  # события matcher'а -- команды + дата, допуск на разницу площадок в датах/таймзонах
SNAPSHOT_MATCH_TOLERANCE_MIN = 90  # ближайшая реальная точка CLOB к моменту снимка Pinnacle

# 2026-09-10, реальная проверка: Polymarket CLOB не публикует явное поле
# комиссии в ответах gamma-api/markets, которые уже использовались в
# Задачах C/E этой сессии -- по общедоступной информации Polymarket не
# взимает явную taker-комиссию на большинстве рынков (0%). Не нашёл
# способа проверить это эмпирически без реальной сделки, поэтому
# ЧЕСТНО указываю это как непроверенное допущение, а не измеренный факт.
POLYMARKET_ASSUMED_TAKER_FEE_PCT = 0.0


def fetch_price_history(clob_token_id: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    try:
        r = requests.get(f"{CLOB_BASE}/prices-history", params={
            "market": clob_token_id, "startTs": int(start_dt.timestamp()), "endTs": int(end_dt.timestamp()),
            "fidelity": 60,
        }, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return []
        return r.json().get("history", [])
    except requests.exceptions.RequestException:
        return []


def nearest_price(history: list[dict], target_ts: int, tolerance_s: int) -> float | None:
    if not history:
        return None
    best = min(history, key=lambda pt: abs(pt.get("t", target_ts) - target_ts))
    if abs(best.get("t", target_ts) - target_ts) > tolerance_s:
        return None
    return float(best["p"])


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not IN_PATH.exists():
        result["blocker"] = f"{IN_PATH} не найден -- фетч Pinnacle (дорогой шаг) не выполнен."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"[taskD_analysis] {result['blocker']}")
        return 1

    data = json.loads(IN_PATH.read_text())
    snapshots = data.get("snapshots", [])
    result["n_snapshots_loaded"] = len(snapshots)
    print(f"[taskD_analysis] загружено {len(snapshots)} снимков Pinnacle")

    # 1. Собираем уникальные события (по id) по всем снимкам -- один matching на событие, не на снимок.
    events_by_id: dict[str, dict] = {}
    for snap in snapshots:
        for ev in snap.get("events", []):
            events_by_id.setdefault(ev["id"], {
                "id": ev["id"], "sport_key": ev["sport_key"], "commence_time": ev["commence_time"],
                "home_team": ev["home_team"], "away_team": ev["away_team"], "snapshot_points": [],
            })
            events_by_id[ev["id"]]["snapshot_points"].append({
                "actual_timestamp": snap.get("actual_timestamp"), "requested_date": snap.get("requested_date"),
                "no_vig_probs": ev["no_vig_probs"],
            })
    result["n_unique_pinnacle_events"] = len(events_by_id)
    print(f"[taskD_analysis] уникальных событий Pinnacle: {len(events_by_id)}")

    # 2. Polymarket bulk-fetch -- переиспользуем taskC как есть.
    print("[taskD_analysis] Polymarket bulk-fetch (переиспользуем taskC_sports_matcher.fetch_polymarket_bulk)...")
    pm_markets = fetch_polymarket_bulk()
    print(f"[taskD_analysis] реальных рынков Polymarket загружено: {len(pm_markets)}")
    result["n_polymarket_markets_scanned"] = len(pm_markets)

    # 3. Сопоставление -- та же логика "команды + дата", что в taskC, адаптированная
    # под форму события The Odds API (не переписываем сам matcher, воспроизводим его
    # критерий на другом формате входа, т.к. match_game_to_polymarket ожидает Kalshi-форму).
    matched = []
    n_team_matched_date_rejected = 0
    for ev in events_by_id.values():
        home_n, away_n = normalize(ev["home_team"]), normalize(ev["away_team"])
        try:
            commence_dt = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        best = None
        for pm in pm_markets:
            q = normalize((pm.get("question") or "") + " " + (pm.get("slug") or ""))
            if home_n not in q or away_n not in q:
                continue
            compare_date_str = pm.get("gameStartTime") or pm.get("endDate")
            if not compare_date_str:
                continue
            try:
                compare_date = datetime.fromisoformat(compare_date_str.replace("Z", "+00:00"))
                if compare_date.tzinfo is None:
                    compare_date = compare_date.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            diff_days = abs((compare_date - commence_dt).total_seconds()) / 86400
            if diff_days > DATE_DIFF_CONFIDENT_DAYS:
                n_team_matched_date_rejected += 1
                continue
            if best is None or diff_days < best[0]:
                best = (diff_days, pm)
        if best:
            matched.append({"event": ev, "polymarket": best[1], "date_diff_days": best[0]})
    result["n_matched_events"] = len(matched)
    result["n_team_matched_but_date_rejected"] = n_team_matched_date_rejected
    print(f"[taskD_analysis] реально сопоставлено событий: {len(matched)} "
          f"(команды совпали, но дата не прошла: {n_team_matched_date_rejected})")

    if not matched:
        result["blocker"] = "0 сопоставленных событий -- дальше считать нечего."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"[taskD_analysis] {result['blocker']}")
        return 0

    # 4. Для каждого сопоставленного события -- определяем, какой clobTokenId
    # соответствует home_team, тянем ОДИН раз всю историю цены на нужное окно.
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=31)
    n_price_history_ok = 0
    all_pairs = []  # (event_id, timestamp, discrepancy, sign)
    pairs_by_event: dict[str, list[tuple[int, float, float]]] = {}

    for m in matched:
        ev, pm = m["event"], m["polymarket"]
        outcomes_raw = pm.get("outcomes")
        tokens_raw = pm.get("clobTokenIds")
        try:
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        except (json.JSONDecodeError, TypeError):
            continue
        if not outcomes or not tokens or len(outcomes) != len(tokens):
            continue
        home_n = normalize(ev["home_team"])
        home_idx = next((i for i, o in enumerate(outcomes) if normalize(str(o)) == home_n
                          or home_n in normalize(str(o)) or normalize(str(o)) in home_n), None)
        if home_idx is None:
            continue
        home_token = tokens[home_idx]

        try:
            commence_dt = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except ValueError:
            continue
        history = fetch_price_history(home_token, window_start, min(now, commence_dt))
        time.sleep(0.15)
        if not history:
            continue
        n_price_history_ok += 1

        for sp in ev["snapshot_points"]:
            if not sp["actual_timestamp"]:
                continue
            try:
                ts_dt = datetime.fromisoformat(sp["actual_timestamp"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts_dt >= commence_dt:  # только пред-игровые снимки -- сопоставимо
                continue
            pinnacle_home_prob = sp["no_vig_probs"].get(ev["home_team"])
            if pinnacle_home_prob is None:
                continue
            poly_price = nearest_price(history, int(ts_dt.timestamp()), SNAPSHOT_MATCH_TOLERANCE_MIN * 60)
            if poly_price is None:
                continue
            raw_discrepancy = pinnacle_home_prob - poly_price  # знак: + значит Pinnacle оценивает home выше
            net_discrepancy = raw_discrepancy - (POLYMARKET_ASSUMED_TAKER_FEE_PCT if raw_discrepancy > 0 else -POLYMARKET_ASSUMED_TAKER_FEE_PCT)
            ts_int = int(ts_dt.timestamp())
            all_pairs.append({"event_id": ev["id"], "timestamp": ts_int, "discrepancy": net_discrepancy})
            pairs_by_event.setdefault(ev["id"], []).append((ts_int, net_discrepancy))

    result["n_events_with_price_history"] = n_price_history_ok
    result["n_event_snapshot_pairs"] = len(all_pairs)
    print(f"[taskD_analysis] событий с реальной ценовой историей Polymarket: {n_price_history_ok}")
    print(f"[taskD_analysis] реальных пар событие-снимок с обеими ценами: {len(all_pairs)}")

    if not all_pairs:
        result["blocker"] = "0 реальных пар событие-снимок с ценами обеих площадок -- дальше считать нечего."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"[taskD_analysis] {result['blocker']}")
        return 0

    # 5. Метрика 1 -- доля пар с |расхождение| >= 2%.
    n_ge_2pct = sum(1 for p in all_pairs if abs(p["discrepancy"]) >= DISCREPANCY_THRESHOLD)
    frac_ge_2pct = n_ge_2pct / len(all_pairs)
    result["n_pairs_discrepancy_ge_2pct"] = n_ge_2pct
    result["frac_pairs_discrepancy_ge_2pct"] = frac_ge_2pct

    # 6. Метрика 2 -- живучесть: для каждой пары с |расхождение|>=2%, ищем
    # СЛЕДУЮЩИЙ по времени снимок ТОГО ЖЕ события, проверяем тот же знак и >=1%.
    n_persistence_checked = 0
    n_persisted = 0
    for eid, points in pairs_by_event.items():
        points.sort(key=lambda x: x[0])
        for i, (ts, disc) in enumerate(points):
            if abs(disc) < DISCREPANCY_THRESHOLD:
                continue
            if i + 1 >= len(points):
                continue  # нет следующего снимка для этого события -- не считаем ни туда, ни сюда
            next_ts, next_disc = points[i + 1]
            n_persistence_checked += 1
            same_sign = (disc > 0) == (next_disc > 0)
            if same_sign and abs(next_disc) >= PERSISTENCE_MIN_SIZE:
                n_persisted += 1

    frac_persisted = (n_persisted / n_persistence_checked) if n_persistence_checked else None
    result["n_persistence_checked"] = n_persistence_checked
    result["n_persisted"] = n_persisted
    result["frac_persisted"] = frac_persisted

    result["polymarket_assumed_taker_fee_pct"] = POLYMARKET_ASSUMED_TAKER_FEE_PCT
    result["polymarket_fee_note"] = ("НЕ измерено эмпирически в этом прогоне -- допущение на основе общедоступной "
                                       "информации о политике Polymarket (0% taker на большинстве рынков), не факт.")

    result["preregistration_met"] = (
        frac_ge_2pct >= 0.15 and frac_persisted is not None and frac_persisted > 0.5
    )

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[taskD_analysis] Доля пар с расхождением >=2%: {frac_ge_2pct:.1%} (порог >=15%)")
    print(f"[taskD_analysis] Живучесть (тот же знак, >=1% на следующем снимке): "
          f"{frac_persisted:.1%} из {n_persistence_checked} случаев (порог >50%)" if frac_persisted is not None
          else "[taskD_analysis] живучесть не посчитана -- 0 случаев для проверки")
    print(f"[taskD_analysis] Предрегистрация выполнена: {result['preregistration_met']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
