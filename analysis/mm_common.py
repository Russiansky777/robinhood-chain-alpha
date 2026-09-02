"""Общий модуль для новой задачи (владелец, 2026-09-02): "экономика
маркетмейкинга сток-токенов в закрытые часы с хеджем перпом". Публичный
RPC + API Lighter, 0 Dune, 0 капитала, только измерение.

Переиспользует УЖЕ ОПЛАЧЕННЫЕ и провалидированные реестры прошлых
спринтов вместо нового discovery с нуля (владелец: "никогда не
выдумывай данные, всегда ищи реальные источники" -- эти данные УЖЕ
реальные, добытые прямыми запросами в прошлых сессиях):

- `data/sprintR1_cache/r1_token_feed_map.csv` -- 32 сток-токена с
  подтверждёнными Chainlink-подобными price-feed адресами (Sprint R1,
  дословный источник -- см. docs/R1_DESIGN.md). Нужен как "reference
  price" для realized spread / markout.
- `data/p4_lighter_cache/p4_lighter_markets_result.json` -- 45 рынков
  Lighter, сопоставленных с реестром сток-токенов R1 (Sprint P4, реальный
  прогон против `mainnet.zklighter.elliot.ai`). Нужен для хеджа --
  токен без активного перп-рынка на Lighter не годится как кандидат
  этой задачи (хедж физически невозможен).

Пересечение (feed + Lighter market) -- рабочая вселенная кандидатов на
топ-5. Токены вне пересечения НЕ исключаются из discovery полностью
(это может исказить "определить топ-5 из данных, а не предположить"),
но не могут пройти дальше в п.2-5 задания без хеджа.
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

R1_FEED_MAP_PATH = Path("data/sprintR1_cache/r1_token_feed_map.csv")
P4_LIGHTER_MARKETS_PATH = Path("data/p4_lighter_cache/p4_lighter_markets_result.json")

NY_TZ = ZoneInfo("America/New_York")

# Окно задачи (владелец, п.1): 01.07.2026 - 01.09.2026.
WINDOW_START_UTC = datetime(2026, 7, 1, tzinfo=ZoneInfo("UTC"))
WINDOW_END_UTC = datetime(2026, 9, 1, tzinfo=ZoneInfo("UTC"))

# NYSE-праздники, ПОЛНОСТЬЮ попадающие в окно 01.07-01.09.2026 --
# посчитано, не угадано: 2026-07-04 (День независимости) -- суббота
# (python datetime.date(2026,7,4).strftime('%A') == 'Saturday',
# проверено). Стандартное правило NYSE-обсервации: если праздник выпадает
# на субботу, биржа закрыта в ПРЕДЫДУЩУЮ пятницу -- 2026-07-03. Labor Day
# (первый понедельник сентября, 2026-09-07) уже ВНЕ окна (окно кончается
# 01.09) -- не включён. Других федеральных/биржевых праздников в
# июле-августе нет.
NYSE_FULL_DAY_HOLIDAYS_IN_WINDOW = {date(2026, 7, 3)}


def load_stock_universe() -> dict[str, dict]:
    """token_address (lowercase) -> {symbol, feed_address, decimals}."""
    out = {}
    with open(R1_FEED_MAP_PATH, newline="") as f:
        for row in csv.DictReader(f):
            out[row["token_address"].lower()] = {
                "symbol": row["symbol"],
                "feed_address": row["feed_address"].lower(),
                "decimals": int(row["decimals"]),
                "description": row["description"],
            }
    return out


def load_lighter_stock_markets() -> dict[str, dict]:
    """symbol -> запись из Sprint P4 (market_id, funding, depth, объём...)."""
    data = json.loads(P4_LIGHTER_MARKETS_PATH.read_text())
    return {r["symbol"]: r for r in data["results"]}


def eligible_universe() -> dict[str, dict]:
    """symbol -> {token_address, feed_address, decimals, lighter_market_id,
    ...} -- ТОЛЬКО токены, у которых есть И onchain price-feed (R1), И
    активный перп-рынок на Lighter (P4) -- хедж без рынка невозможен."""
    stock = load_stock_universe()
    lighter = load_lighter_stock_markets()
    by_symbol = {v["symbol"]: {"token_address": addr, **v} for addr, v in stock.items()}
    out = {}
    for sym, tok in by_symbol.items():
        if sym in lighter:
            out[sym] = {**tok, "lighter": lighter[sym]}
    return out


def classify_regime(ts_utc: datetime) -> str:
    """Классификация US-рынка по UTC-времени сделки: regular/pre/after/
    weekend/holiday. Границы (общепринятые, NYSE/Nasdaq extended hours):
    regular 09:30-16:00 ET, pre-market 04:00-09:30 ET, after-hours
    16:00-20:00 ET -- всё Пн-Пт, не праздник. Вне 04:00-20:00 ET в будний
    день -- тоже "closed" (ночные часы) -- отдельный бакет владельцем не
    запрошен явно, относим к ближайшему смыслу: п.1 просит "regular/pre/
    after/weekend/holiday" -- 5 бакетов, ночные будние часы вне 4:00-20:00
    попадают в holiday-подобный "closed" бакет -- используем 'overnight'
    как честное шестое имя вместо искусственного впихивания в 5, если
    такие сделки вообще найдутся (не выдумываем принудительное
    соответствие 5 бакетам, если реальность не укладывается)."""
    if ts_utc.tzinfo is None:
        raise ValueError("classify_regime требует timezone-aware datetime (UTC)")
    ny = ts_utc.astimezone(NY_TZ)
    d = ny.date()
    if d.weekday() >= 5:  # Sat=5, Sun=6
        return "weekend"
    if d in NYSE_FULL_DAY_HOLIDAYS_IN_WINDOW:
        return "holiday"
    t = ny.time()
    if t >= datetime.strptime("09:30", "%H:%M").time() and t < datetime.strptime("16:00", "%H:%M").time():
        return "regular"
    if t >= datetime.strptime("04:00", "%H:%M").time() and t < datetime.strptime("09:30", "%H:%M").time():
        return "pre"
    if t >= datetime.strptime("16:00", "%H:%M").time() and t < datetime.strptime("20:00", "%H:%M").time():
        return "after"
    return "overnight"


CLOSED_REGIMES = {"pre", "after", "weekend", "holiday", "overnight"}
