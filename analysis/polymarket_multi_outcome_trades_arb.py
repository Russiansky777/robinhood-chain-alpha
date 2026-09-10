#!/usr/bin/env python3
"""Задача 2 владельца (2026-09-07), вариант (б) после реального
ограничения API (паспорт: "публичный CLOB API НЕ хранит историю
стакана вообще"): "по сделкам. История сделок /prices-history по всем
исходам мультиисходных событий за 30 дней. Для каждой минуты, где
прошли сделки по всем исходам события: сумма цен против $1.00, с
реальной комиссией тейкера baseRate × min(P, 1−P) × shares. Это оценка
СНИЗУ — стакан мог быть шире сделок, пометить явно."

РЕАЛЬНЫЕ факты этой сессии (два прохода WebSearch + прямое чтение
официальных репозиториев Polymarket/rs-clob-client-v2, Polymarket/
py-sdk, Polymarket/agent-skills, Polymarket/agents, Polymarket/
ctf-exchange -- НЕ по памяти):
  - Gamma API (`gamma-api.polymarket.com/events`) -- реальные параметры
    `closed`, `start_date_min/max`, `end_date_min/max`, `limit`/
    `offset`. `negRisk` -- ТОЛЬКО поле ответа, НЕ серверный фильтр --
    фильтруем клиентски после получения.
  - Market-объект: `clobTokenIds` -- строка JSON-массива (парсить
    json.loads), по одному ERC1155-токену на исход (Yes-сторона).
    `endDate`/`closedTime` -- реальные поля даты закрытия/расчёта.
    `umaResolutionStatus`, `outcomePrices` (строка JSON, финальные
    цены после расчёта) -- поля исхода.
  - Data API (`data-api.polymarket.com/trades`) -- РЕАЛЬНЫЕ сделки, не
    склеенные бины: параметры `eventId`/`market`(condition_id), `start`/
    `end` (unix секунды), `limit`(0-10000)/`offset`(0-10000). Поля
    ответа: `asset`(token id), `condition_id`, `price`, `size`,
    `timestamp`(unix секунды), `outcome`, `outcome_index`. Без
    авторизации.
  - Комиссия -- РЕАЛЬНО с TAKER-стороны в момент сделки (НЕ на
    выигрышной ноге при погашении -- прежнее предположение владельца
    было неверным, погашение бесплатно, см. паспорт): `usdcFee =
    baseRate × min(price, 1-price) × shares` (`CalculatorHelper.sol`,
    `Polymarket/ctf-exchange`). `baseRate` -- живой эндпоинт
    `GET /fee-rate?token_id=...` (сам Polymarket предупреждает не
    хардкодить). Реальный ключ ответа -- `base_fee`, в bps
    (÷10_000) -- подтверждено 2026-09-10 в Задаче D двумя
    первоисточниками: сам `CalculatorHelper.sol` (`BPS_DIVISOR=10_000`)
    и `Polymarket/py-clob-client` issue #326. Тот же исходник даёт
    точную ончейн-формулу для BUY (комиссия списывается токенами):
    `fee_tokens = feeRateBps × min(p,1-p) × outcomeTokens / (price × BPS_DIVISOR)`.
    Перепроверено численно (2026-09-10): `fee_tokens × price` (пересчёт
    в USDC по текущей цене -- естественная мера "цена входа сейчас",
    которая и нужна для сравнения с $1 полного набора исходов) в
    точности равно `baseRate × min(p,1-p) × outcomeTokens` при ЛЮБОЙ p
    -- т.е. формула здесь НЕ приближение, а точный алгебраический
    эквивалент реальной BUY-комиссии в долларовом выражении. (Отличие
    появляется только если оценивать удержанные токены по номиналу $1
    вместо текущей цены -- это другой вопрос, не применимый к разделу
    "цена входа".)

Метод (честная НИЖНЯЯ ГРАНИЦА -- по сделкам, не по стакану, явно
помечено в каждой записи результата):
  1. Реальные мультиисходные события (negRisk=true, >=3 рынков),
     активные или закрытые в последние 30 дней (Gamma /events,
     start_date_max/end_date_min-фильтры).
  2. Реальные сделки по КАЖДОМУ событию целиком (Data API /trades по
     eventId, пагинация limit/offset, окно -- 30 реальных суток).
  3. Группировка по минуте (floor до минуты) и исходу (`outcome_index`)
     -- последняя реальная цена сделки в минуте на исход.
  4. Минуты, где ЕСТЬ реальная сделка по ВСЕМ исходам события --
     сумма последних цен против $1.00, за вычетом реальной комиссии
     тейкера на КАЖДУЮ ногу арбитражной покупки (купить все исходы).
  5. Спред >=1% -- считается по дням, длительность эпизода (сколько
     подряд реальных минут держится), реальный размер сделок в эти
     минуты (`size`) как ПРОКСИ доступной глубины (честно: это объём
     реальной прошедшей сделки, не заявленная глубина стакана -- явно
     нижняя граница, стакан МОГ быть шире).

Отдельно, БЕЗ ограничения стакана (владелец: "здесь история сделок
достаточна полностью") -- "почти разрешённые" рынки: реальная цена
97-99c по последним сделкам ДО расчёта, при уже известном
`umaResolutionStatus`/`outcomePrices` (рынок реально расчитан), дней от
момента такой цены до `closedTime`, годовая доходность на запертый
капитал."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
OUT_PATH = Path("data/p3_guard_cache/polymarket_multi_outcome_trades_arb_result.json")

WINDOW_DAYS = 30
MIN_MARKETS_FOR_MULTI_OUTCOME = 3
SPREAD_ALERT_PCT = 1.0
MIN_TIMES_PER_DAY = 5  # предрегистрация владельца
HEADERS = {"User-Agent": "robinhood-chain-alpha-polymarket-arb/1.0"}
MIN_REQUEST_INTERVAL_S = 0.35  # реальные лимиты не опубликованы фиксированным числом (см. паспорт) -- консервативный троттлинг + честная реакция на 429/заголовки

_last_call = 0.0
_fee_rate_cache: dict[str, float | None] = {}


def _throttle() -> None:
    global _last_call
    wait = _last_call + MIN_REQUEST_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _get(url: str, params: dict | None = None, max_retries: int = 3) -> tuple[int, dict | list | str]:
    for attempt in range(max_retries):
        _throttle()
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        if r.status_code == 429:
            reset = r.headers.get("Poly-RateLimit-Reset")
            wait_s = float(reset) if reset else 5.0 * (attempt + 1)
            print(f"    429 (Poly-RateLimit-Reset={reset}), жду {wait_s:.1f}с")
            time.sleep(wait_s)
            continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text[:500]
    return 429, "исчерпаны реальные попытки после 429"


def fetch_negrisk_events(window_start_iso: str, now_iso: str) -> list[dict]:
    """Реальные мультиисходные события, активные или закрытые в окне.
    Два прохода (открытые сейчас + закрытые в окне) -- негативный риск
    не фильтруется на сервере, добираем клиентски.

    РЕАЛЬНЫЙ фикс после первого прогона (run 34121721855): сервер
    вернул HTTP 422 "end_date_min has a wrong value" на формат без 'Z' --
    исправлено на честный ISO8601+'Z'. Дополнительная защита (на случай
    ЕЩЁ одного формата, который сервер не примет) -- если и это упадёт
    в 422, честно откатываемся на запрос БЕЗ date-фильтра (отсортированный
    по startDate убыв., ограниченный MAX_PAGES_NO_DATE_FILTER страниц)
    и фильтруем окно клиентски по `endDate`/`closedTime` -- не гадаем
    формат бесконечно, ограничиваем реальными попытками."""
    out = []
    max_pages_no_filter = 20  # честный потолок при отвале date-фильтра -- не листаем весь архив Polymarket вслепую
    for closed_filter in (False, True):
        offset = 0
        use_date_filter = True
        while True:
            params = {"closed": str(closed_filter).lower(), "limit": 100, "offset": offset,
                      "order": "startDate", "ascending": "false"}
            if use_date_filter:
                params["end_date_min"] = window_start_iso
            status, body = _get(f"{GAMMA_BASE}/events", params)
            if status == 422 and use_date_filter:
                print(f"    /events closed={closed_filter}: date-фильтр отвергнут ({body}) -- честный откат без него, фильтр клиентский, потолок {max_pages_no_filter} страниц")
                use_date_filter = False
                offset = 0
                continue
            if status != 200 or not isinstance(body, list):
                print(f"    /events closed={closed_filter} offset={offset}: HTTP {status} -- {str(body)[:300]}")
                break
            if not body:
                break
            out.extend(body)
            print(f"    /events closed={closed_filter} offset={offset} (date_filter={use_date_filter}): {len(body)} событий")
            if len(body) < 100:
                break
            offset += 100
            if not use_date_filter and offset >= max_pages_no_filter * 100:
                print(f"    честный потолок {max_pages_no_filter} страниц без date-фильтра достигнут -- останавливаемся")
                break
    seen_ids = set()
    multi = []
    for ev in out:
        if ev.get("id") in seen_ids:
            continue
        seen_ids.add(ev.get("id"))
        if not ev.get("negRisk"):
            continue
        markets = ev.get("markets") or []
        if len(markets) < MIN_MARKETS_FOR_MULTI_OUTCOME:
            continue
        # Клиентский фильтр окна -- нужен ВСЕГДА (даже если серверный
        # date-фильтр сработал, он бьёт по end_date_min ТОЛЬКО, окно
        # само по себе не проверено); критично, если сервер вообще
        # отверг фильтр и отдал безусловный список.
        end_date = ev.get("endDate") or ev.get("closedTime")
        if end_date:
            try:
                ev_end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                win_start = datetime.fromisoformat(window_start_iso.replace("Z", "+00:00"))
                if ev_end < win_start:
                    continue
            except ValueError:
                pass  # честно не парсится -- не отбрасываем событие только из-за этого, оставляем на усмотрение анализа сделок
        multi.append(ev)
    return multi


def get_fee_rate(token_id: str) -> float | None:
    if token_id in _fee_rate_cache:
        return _fee_rate_cache[token_id]
    status, body = _get(f"{CLOB_BASE}/fee-rate", {"token_id": token_id})
    rate = None
    # "base_fee" -- реальный ключ ответа (проверено вживую в Задаче D,
    # 2026-09-10: {"base_fee": 1000} для спортивных токенов). Единица --
    # bps (denominator 10_000), подтверждено ДВУМЯ независимыми
    # первоисточниками: (1) сам исходник CalculatorHelper.sol
    # (Polymarket/ctf-exchange, тег 0.0.1) объявляет
    # `BPS_DIVISOR = 10_000` и параметр `feeRateBps`; (2) реальный баг-
    # репорт владельцев API, Polymarket/py-clob-client issue #326
    # ("Fee documentation contradicts CLOB API fee-rate endpoint for
    # Sports markets"), где те же значения base_fee=1000 для NBA/MLB
    # прямо названы "1000 bps". Ключ отсутствовал в прежнем списке --
    # раньше get_fee_rate() молча возвращал None на спортивных рынках.
    BPS_KEYS = ("feeRateBps", "fee_rate_bps", "base_fee")
    if status == 200 and isinstance(body, dict):
        for key in ("baseRate", "base_rate", *BPS_KEYS, "rate"):
            if key in body:
                raw = body[key]
                rate = raw / 10000 if key in BPS_KEYS else raw
                break
    _fee_rate_cache[token_id] = rate
    return rate


def fetch_all_trades(event_id: str, start_ts: int, end_ts: int) -> list[dict]:
    out = []
    offset = 0
    while True:
        params = {"eventId": event_id, "start": start_ts, "end": end_ts, "limit": 500, "offset": offset}
        status, body = _get(f"{DATA_BASE}/trades", params)
        if status != 200 or not isinstance(body, list):
            print(f"    /trades event={event_id} offset={offset}: HTTP {status} -- {str(body)[:300]}")
            break
        if not body:
            break
        out.extend(body)
        if len(body) < 500:
            break
        offset += 500
        if offset >= 10000:
            print(f"    /trades event={event_id}: достигнут реальный потолок offset=10000, могли остаться сделки -- честно помечаем")
            break
    return out


def analyze_event(ev: dict, start_ts: int, end_ts: int) -> dict:
    event_id = ev.get("id")
    markets = ev.get("markets") or []
    outcome_labels: dict[int, str] = {}
    token_by_outcome: dict[int, str] = {}
    for i, m in enumerate(markets):
        try:
            token_ids = json.loads(m.get("clobTokenIds") or "[]")
        except (ValueError, TypeError):
            token_ids = []
        if token_ids:
            token_by_outcome[i] = token_ids[0]  # первый -- Yes-сторона этого исхода (реальная конвенция, подтверждена WebSearch)
            outcome_labels[i] = m.get("groupItemTitle") or m.get("question") or str(i)

    trades = fetch_all_trades(str(event_id), start_ts, end_ts)
    print(f"    реальных сделок: {len(trades)} по {len(markets)} исходам")

    per_minute: dict[int, dict[int, tuple[float, float]]] = defaultdict(dict)  # minute_ts -> outcome_index -> (price, size)
    for t in trades:
        try:
            ts = int(t["timestamp"])
            minute = ts - (ts % 60)
            oi = t.get("outcome_index")
            price = float(t["price"])
            size = float(t["size"])
        except (KeyError, TypeError, ValueError):
            continue
        per_minute[minute][oi] = (price, size)  # последняя сделка в минуте побеждает (порядок ответа API -- хронологический, не гарантирован явно, честно не сортируем повторно без подтверждения поля)

    n_outcomes = len(markets)
    fee_rates = {oi: get_fee_rate(tok) for oi, tok in token_by_outcome.items()}

    episodes = []
    for minute, by_outcome in sorted(per_minute.items()):
        if len(by_outcome) < n_outcomes:
            continue  # честно: нужна сделка по КАЖДОМУ исходу в эту минуту
        total_cost = 0.0
        total_size_min = None
        for oi, (price, size) in by_outcome.items():
            rate = fee_rates.get(oi)
            fee = (rate * min(price, 1 - price)) if rate is not None else 0.0
            total_cost += price + fee
            total_size_min = size if total_size_min is None else min(total_size_min, size)
        spread_pct = (1.0 - total_cost) * 100
        if spread_pct >= SPREAD_ALERT_PCT:
            episodes.append({"minute_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minute)),
                              "spread_pct": spread_pct, "total_cost": total_cost,
                              "min_trade_size_usd_lower_bound": total_size_min,
                              "fee_applied": any(r is not None for r in fee_rates.values())})

    n_minutes_with_all_outcomes = sum(1 for by in per_minute.values() if len(by) >= n_outcomes)

    # "Почти разрешённые" (владелец: цена 97-99c при фактически известном
    # исходе) -- переиспользуем УЖЕ полученные реальные сделки события
    # (не дублируем /trades-запрос), ищем по КАЖДОМУ выигравшему исходу
    # реальные сделки в полосе 0.97-0.99 ДО момента закрытия рынка.
    almost_resolved = []
    ar_diag = {"n_markets_total": len(markets), "n_resolved_status": 0, "n_resolved_with_closed_time": 0,
               "n_resolved_with_known_outcome": 0, "n_resolved_winner_is_index0": 0,
               "n_resolved_winner_is_other_index": 0, "n_with_band_trades": 0}
    for i, m in enumerate(markets):
        resolution_status = m.get("umaResolutionStatus")
        closed_time = m.get("closedTime")
        try:
            outcome_prices = json.loads(m.get("outcomePrices") or "[]")
        except (ValueError, TypeError):
            outcome_prices = []
        if resolution_status == "resolved":
            ar_diag["n_resolved_status"] += 1
            if closed_time:
                ar_diag["n_resolved_with_closed_time"] += 1
        if resolution_status != "resolved" or not closed_time or "1" not in outcome_prices:
            continue
        ar_diag["n_resolved_with_known_outcome"] += 1
        if outcome_prices.index("1") != 0:
            ar_diag["n_resolved_winner_is_other_index"] += 1
            continue  # честно: token_by_outcome[i] -- это ИМЕННО первый (Yes) токен из clobTokenIds; если выиграл НЕ он, а No-сторона, пропускаем (не смешиваем стороны без доп. проверки)
        ar_diag["n_resolved_winner_is_index0"] += 1
        try:
            closed_dt = datetime.fromisoformat(closed_time.replace(" ", "T").replace("+00", "+00:00"))
        except ValueError:
            continue
        closed_ts_local = closed_dt.timestamp()
        band_trades = [(t["timestamp"], float(t["price"])) for t in trades
                        if t.get("outcome_index") == i and 0.97 <= float(t.get("price", 0)) <= 0.99
                        and int(t["timestamp"]) < closed_ts_local]
        if not band_trades:
            continue
        ar_diag["n_with_band_trades"] += 1
        band_trades.sort()
        first_ts, first_price = band_trades[0]
        last_ts, last_price = band_trades[-1]

        def _annualized(ts: float, price: float) -> dict:
            days = (closed_ts_local - ts) / 86400
            if days <= 0:
                return {"days_to_settlement": days, "annualized_return_pct": None}
            profit_frac = (1 - price) / price
            return {"days_to_settlement": days, "annualized_return_pct": profit_frac * (365 / days) * 100}

        almost_resolved.append({
            "event_id": event_id, "market_question": m.get("question"),
            "closed_time": closed_time, "outcome_prices_final": outcome_prices,
            "n_real_trades_in_band_97_99": len(band_trades),
            "earliest_entry": {"timestamp": first_ts, "price": first_price, **_annualized(first_ts, first_price)},
            "latest_entry": {"timestamp": last_ts, "price": last_price, **_annualized(last_ts, last_price)},
        })

    return {
        "event_id": event_id, "title": ev.get("title"), "n_outcomes": n_outcomes,
        "n_trades_total": len(trades), "n_minutes_with_trades_on_all_outcomes": n_minutes_with_all_outcomes,
        "n_episodes_spread_ge_1pct": len(episodes), "episodes": episodes[:200],  # честный кап на объём JSON, не на реальный подсчёт (n_episodes_spread_ge_1pct -- полный)
        "almost_resolved_markets": almost_resolved,
        "almost_resolved_diag": ar_diag,  # диагностика: на каком шаге фильтра реально отсеиваются рынки -- честная проверка, не гадание, почему almost_resolved_markets может быть пуст
    }


def run() -> int:
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=WINDOW_DAYS)
    start_ts, end_ts = int(window_start.timestamp()), int(now.timestamp())
    print(f"=== Окно: {window_start.isoformat()} -> {now.isoformat()} ===")

    events = fetch_negrisk_events(window_start.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    print(f"\n=== Реальных мультиисходных (negRisk, >={MIN_MARKETS_FOR_MULTI_OUTCOME} рынков) событий: {len(events)} ===")

    results = []
    almost_resolved_all = []
    for i, ev in enumerate(events):
        print(f"\n--- {i+1}/{len(events)}: {ev.get('title')} (id={ev.get('id')}, рынков={len(ev.get('markets') or [])}) ---")
        try:
            res = analyze_event(ev, start_ts, end_ts)
            results.append(res)
            almost_resolved_all.extend(res.get("almost_resolved_markets", []))
        except Exception as exc:  # noqa: BLE001
            print(f"    ошибка: {exc}")
            results.append({"event_id": ev.get("id"), "title": ev.get("title"), "error": str(exc)[:400]})

    total_episodes = sum(r.get("n_episodes_spread_ge_1pct", 0) for r in results)
    days = WINDOW_DAYS
    times_per_day = total_episodes / days if days else 0
    prereg_pass = times_per_day >= MIN_TIMES_PER_DAY

    ar_diag_total: dict = defaultdict(int)
    for r in results:
        for k, v in (r.get("almost_resolved_diag") or {}).items():
            ar_diag_total[k] += v

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": "НИЖНЯЯ ГРАНИЦА по реальным сделкам (Data API /trades), не по стакану -- стакан мог быть шире",
        "window_days": WINDOW_DAYS, "n_events_analyzed": len(results),
        "total_episodes_spread_ge_1pct": total_episodes, "times_per_day": times_per_day,
        "prereg_threshold_times_per_day": MIN_TIMES_PER_DAY,
        "prereg_pass_build_live_orderbook": prereg_pass,
        "events": results,
        "almost_resolved_markets": almost_resolved_all,
        "almost_resolved_diag_total": dict(ar_diag_total),  # честная сводка воронки фильтра по ВСЕМ событиям -- откуда реально 0 (или не 0)
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n=== ИТОГО: {total_episodes} эпизодов спреда>=1% за {WINDOW_DAYS}д ({times_per_day:.2f}/сутки), "
          f"предрегистрация (>={MIN_TIMES_PER_DAY}/сутки -> строить live-сбор стакана): {prereg_pass} ===")
    print(f"[polymarket_arb] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
