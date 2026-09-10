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
from polymarket_multi_outcome_trades_arb import get_fee_rate, CLOB_BASE as ARB_CLOB_BASE, HEADERS as ARB_HEADERS
# Задача 2: реальная формула baseRate x min(P,1-P) x shares,
# GET /fee-rate?token_id=... -- живой эндпоинт, Polymarket прямо предупреждает не хардкодить процент

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskD-pinnacle-analysis/1.0"}
CLOB_BASE = "https://clob.polymarket.com"

IN_PATH = Path("data/p3_guard_cache/taskD_pinnacle_snapshots.json")
OUT_PATH = Path("data/p3_guard_cache/taskD_pinnacle_polymarket_result.json")

DISCREPANCY_THRESHOLD = 0.02  # владелец: >=2%
PERSISTENCE_MIN_SIZE = 0.01  # владелец: сохранилось "размера >=1%"
DATE_DIFF_CONFIDENT_DAYS = 1.0  # события matcher'а -- команды + дата, допуск на разницу площадок в датах/таймзонах
SNAPSHOT_MATCH_TOLERANCE_MIN = 90  # ближайшая реальная точка CLOB к моменту снимка Pinnacle

# 2026-09-10, владелец: в Задаче 2 УЖЕ выяснено (прямое чтение исходника
# CalculatorHelper.sol, Polymarket/ctf-exchange) -- комиссия берётся
# ТОЛЬКО с TAKER-стороны В МОМЕНТ СДЕЛКИ: usdcFee = baseRate x
# min(price, 1-price) x shares. Погашение выигравших токенов бесплатно
# (комиссии на исходе нет) -- поэтому для стратегии "войти сейчас,
# держать до расчёта" учитываем РОВНО ОДНУ комиссию входа, не round-trip.
# baseRate -- РЕАЛЬНЫЙ, живой (`GET /fee-rate?token_id=...`), НЕ
# захардкожен -- Polymarket явно предупреждает это не делать. Проверяем
# per-event на реальных спортивных токенах (не только 15-мин крипто,
# как в Задаче 2) -- формула в исходнике не привязана к типу рынка,
# но фактическое ЗНАЧЕНИЕ baseRate может отличаться, поэтому меряем
# заново для каждого сопоставленного события, не переносим число оттуда.
_fee_rate_by_event: dict[str, float | None] = {}


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
    # 2026-09-10, владелец: 2.3% сопоставления (10/430) заподозрено как
    # проблема -- диагностика на 20 случайных событиях
    # (taskD_matching_diagnostic.py, реальный прогон) дала: 16/20 --
    # рынка на Polymarket реально нет (в основном MLB regular season,
    # проверено узким независимым поиском +/-3д), 4/20 -- "матчер не
    # находит", из них РОВНО 2 -- чистая, однозначная евиденция реального
    # бага нормализации: NFL-рынки Polymarket называют команды КОРОТКИМ
    # nickname'ом без города ("Broncos vs. Chiefs", slug=nfl-den-kc-...),
    # а строгий predicate требовал ПОЛНОЕ имя команды с городом
    # ("kansascitychiefs") как целую подстроку -- никогда не совпадёт.
    # (Остальные 2 "matcher_misses_it" из диагностики -- MLB O/U-рынок с
    # РЕАЛЬНО другой игровой датой в slug, отклонён датой корректно, не
    # баг нормализации -- не чиним.)
    # Точечный фикс: fallback на nickname (последнее значимое слово
    # названия команды) ТОЛЬКО когда полное имя не совпало, тот же
    # порог даты -- не ослабляем дисамбигуацию по дате, расширяем
    # ТОЛЬКО способ найти команду в тексте.
    def team_nickname(team: str) -> str:
        words = [w for w in re.sub(r"[^a-zA-Z0-9 ]", " ", team).lower().split()]
        return normalize(words[-1]) if words else ""

    matched = []
    n_team_matched_date_rejected = 0
    n_matched_via_nickname_fallback = 0
    for ev in events_by_id.values():
        home_n, away_n = normalize(ev["home_team"]), normalize(ev["away_team"])
        home_nick, away_nick = team_nickname(ev["home_team"]), team_nickname(ev["away_team"])
        try:
            commence_dt = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        best = None
        for pm in pm_markets:
            q = normalize((pm.get("question") or "") + " " + (pm.get("slug") or ""))
            via_nickname = False
            if home_n in q and away_n in q:
                pass
            elif home_nick and away_nick and home_nick in q and away_nick in q:
                via_nickname = True
            else:
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
                best = (diff_days, pm, via_nickname)
        if best:
            matched.append({"event": ev, "polymarket": best[1], "date_diff_days": best[0], "matched_via_nickname_fallback": best[2]})
            if best[2]:
                n_matched_via_nickname_fallback += 1
    result["n_matched_events"] = len(matched)
    result["n_matched_via_nickname_fallback"] = n_matched_via_nickname_fallback
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
    #
    # 2026-09-10, реальная находка первого прогона: фиксированное окно
    # [now-31d; commence] дало n_events_with_price_history=0 из 10 -- тот
    # же паттерн, что уже был найден в Задаче E (`prices-history` реально
    # возвращает 200 OK с ПУСТОЙ history, если окно предшествует созданию
    # рынка). Спортивные moneyline-рынки Polymarket создаются незадолго до
    # игры, не за 31 день. Первая точечная правка по факту диагноза (не
    # вторая вслепую): окно теперь per-event, от САМОЙ РАННЕЙ реальной
    # точки снимка Pinnacle для этого события (с запасом 2 дня), а не от
    # фиксированной глобальной даты.
    now = datetime.now(timezone.utc)
    n_price_history_ok = 0
    n_price_history_empty_diag = []
    fee_rate_raw_diag = []
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

        # Реальный, живой baseRate для ЭТОГО конкретного спортивного токена --
        # не хардкодим, не переносим число из Задачи 2 (там были 15-мин крипто
        # рынки, здесь спорт -- проверяем заново, применимость формулы та же
        # (исходник не различает тип рынка), но фактическое значение могло
        # отличаться).
        fee_rate = get_fee_rate(home_token)
        _fee_rate_by_event[ev["id"]] = fee_rate
        if fee_rate is None and len(fee_rate_raw_diag) < 5:
            # 2026-09-10, диагностика (не патч) -- get_fee_rate() молча
            # возвращает None на ЛЮБой причине отказа (не-200, не-dict,
            # нет ожидаемого ключа). Реально нужно увидеть, какая именно,
            # прежде чем считать издержки нулевыми.
            try:
                r_diag = requests.get(f"{ARB_CLOB_BASE}/fee-rate", params={"token_id": home_token},
                                       headers=ARB_HEADERS, timeout=15)
                diag_entry = {"event_id": ev["id"], "token_id": home_token,
                              "status": r_diag.status_code, "body_snippet": r_diag.text[:300]}
                # 2026-09-10, доп. диагностика -- независимый источник для
                # МАСШТАБА значения base_fee (не гадаем делитель): паспорт
                # проекта уже упоминает CLOB-поля maker_base_fee/taker_base_fee
                # на market-эндпоинте -- сверяем то же самое число с другого
                # реального эндпоинта.
                try:
                    r_mkt = requests.get(f"{ARB_CLOB_BASE}/markets/{home_token}", headers=ARB_HEADERS, timeout=15)
                    diag_entry["market_endpoint_status"] = r_mkt.status_code
                    diag_entry["market_endpoint_body_snippet"] = r_mkt.text[:500]
                except requests.exceptions.RequestException as exc2:
                    diag_entry["market_endpoint_exception"] = str(exc2)[:150]
                fee_rate_raw_diag.append(diag_entry)
            except requests.exceptions.RequestException as exc:
                fee_rate_raw_diag.append({"event_id": ev["id"], "token_id": home_token, "exception": str(exc)[:200]})
        time.sleep(0.1)

        try:
            commence_dt = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except ValueError:
            continue

        snap_timestamps = []
        for sp in ev["snapshot_points"]:
            if sp.get("actual_timestamp"):
                try:
                    snap_timestamps.append(datetime.fromisoformat(sp["actual_timestamp"].replace("Z", "+00:00")))
                except ValueError:
                    pass
        earliest_snap = min(snap_timestamps) if snap_timestamps else (commence_dt - timedelta(days=7))
        window_start_event = earliest_snap - timedelta(days=2)  # запас на неточность создания рынка
        window_end_event = min(now, commence_dt)

        history = fetch_price_history(home_token, window_start_event, window_end_event)
        time.sleep(0.15)
        if not history:
            if len(n_price_history_empty_diag) < 5:
                n_price_history_empty_diag.append({
                    "event_id": ev["id"], "home_token": home_token,
                    "window_start": window_start_event.isoformat(), "window_end": window_end_event.isoformat(),
                    "commence_time": ev["commence_time"],
                })
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
            # Реальная формула (Задача 2, CalculatorHelper.sol): комиссия входа
            # = baseRate x min(P, 1-P), P -- цена ноги, которую покупаем (сторона
            # Polymarket, которую мы считаем неверно оценённой). Погашение
            # бесплатно (установлено в Задаче 2) -- ОДНА комиссия на вход, не
            # round-trip, для стратегии "войти и держать до расчёта".
            fee_rate = _fee_rate_by_event.get(ev["id"])
            real_fee = (fee_rate * min(poly_price, 1 - poly_price)) if fee_rate is not None else 0.0
            net_discrepancy = raw_discrepancy - real_fee if raw_discrepancy > 0 else raw_discrepancy + real_fee
            ts_int = int(ts_dt.timestamp())
            all_pairs.append({"event_id": ev["id"], "timestamp": ts_int, "discrepancy": net_discrepancy,
                               "raw_discrepancy": raw_discrepancy, "fee_rate_used": fee_rate})
            pairs_by_event.setdefault(ev["id"], []).append((ts_int, net_discrepancy))

    result["n_events_with_price_history"] = n_price_history_ok
    result["n_event_snapshot_pairs"] = len(all_pairs)
    result["price_history_empty_diagnostics"] = n_price_history_empty_diag
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

    real_fee_rates = [v for v in _fee_rate_by_event.values() if v is not None]
    n_events_fee_rate_unavailable = sum(1 for v in _fee_rate_by_event.values() if v is None)
    result["polymarket_fee_formula"] = "usdcFee = baseRate x min(price, 1-price) x shares (CalculatorHelper.sol, Polymarket/ctf-exchange) -- Задача 2"
    result["polymarket_fee_note"] = ("baseRate измерен ЖИВЬЁМ (GET /fee-rate?token_id=...) для каждого реального "
                                       "сопоставленного спортивного события в этом прогоне -- НЕ хардкожен и НЕ "
                                       "перенесён из Задачи 2 (там были 15-мин крипто-рынки). Формула из исходника "
                                       "не различает тип рынка -- применена как есть; фактическое значение baseRate "
                                       "проверено заново на спортивных токенах. Одна комиссия на вход (не round-trip) "
                                       "-- погашение выигравших токенов бесплатно (установлено в Задаче 2).")
    result["n_events_with_real_fee_rate"] = len(real_fee_rates)
    result["n_events_fee_rate_unavailable"] = n_events_fee_rate_unavailable
    if real_fee_rates:
        result["real_fee_rate_min"] = min(real_fee_rates)
        result["real_fee_rate_max"] = max(real_fee_rates)
        result["real_fee_rate_mean"] = sum(real_fee_rates) / len(real_fee_rates)
        result["real_fee_rate_distinct_values"] = sorted(set(real_fee_rates))
    else:
        result["fee_rate_blocker"] = ("Эндпоинт /fee-rate не вернул значение ни для одного сопоставленного "
                                        "события -- расхождение считается БЕЗ вычета комиссии (fee=0), явно "
                                        "занижает реальные издержки, честно помечено.")
        result["fee_rate_raw_diagnostics"] = fee_rate_raw_diag

    result["preregistration_met"] = (
        frac_ge_2pct >= 0.15 and frac_persisted is not None and frac_persisted > 0.5
    )
    # Владелец явно просил учесть реальную комиссию перед сравнением с порогом --
    # если baseRate НИ РАЗУ не измерен, результат посчитан на завышенном
    # (без вычета издержек) расхождении и НЕ является ответом на его вопрос,
    # даже если формально preregistration_met=true.
    result["preregistration_met_reliable"] = len(real_fee_rates) > 0

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[taskD_analysis] Доля пар с расхождением >=2%: {frac_ge_2pct:.1%} (порог >=15%)")
    print(f"[taskD_analysis] Живучесть (тот же знак, >=1% на следующем снимке): "
          f"{frac_persisted:.1%} из {n_persistence_checked} случаев (порог >50%)" if frac_persisted is not None
          else "[taskD_analysis] живучесть не посчитана -- 0 случаев для проверки")
    if real_fee_rates:
        print(f"[taskD_analysis] Реальный baseRate на спортивных рынках: min={result['real_fee_rate_min']}, "
              f"max={result['real_fee_rate_max']}, mean={result['real_fee_rate_mean']:.6f} "
              f"({len(real_fee_rates)}/{len(_fee_rate_by_event)} событий, значения: {result['real_fee_rate_distinct_values']})")
    else:
        print(f"[taskD_analysis] baseRate НЕ получен ни для одного события ({n_events_fee_rate_unavailable} "
              "попыток) -- расхождение считается без вычета комиссии, честно занижает издержки")
    print(f"[taskD_analysis] Предрегистрация выполнена: {result['preregistration_met']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
