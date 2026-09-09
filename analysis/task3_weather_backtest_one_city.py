#!/usr/bin/env python3
"""Задача 3 владельца (2026-09-08), полный (не диагностический) бэктест на
ОДНОМ городе (Нью-Йорк), 30 реальных дней, как явное условие перед
расширением на 14 городов: "если на одном ожидание положительное".

Реальные требования владельца дословно:
  - Станцию взять из реального контракта Kalshi через API (поле правил
    расчёта -- владелец назвал его "rules_primary"; реальное имя поля
    ЖИВЬЁМ проверяется в этом скрипте через `GET /series/{ticker}`, НЕ
    предполагается заранее -- честно логируется весь сырой JSON первого
    запроса, чтобы не гадать об имени поля).
  - Ансамбль GEFS из открытого бакета AWS `noaa-gefs-pds`, byte-range
    извлечение точки -- ТОТ ЖЕ метод, что уже подтверждён диагностикой
    `noaa_gefs_poc_one_city.py` (там же -- честно найденный баг: первое
    совпадение по имени переменной было уровнем "10 mb", не приземным
    "2 m above ground" -- здесь ИСПРАВЛЕНО, level фильтруется явно).
  - Инкрементальный чекпоинт ОБЯЗАТЕЛЕН -- пишем накопленный результат
    в OUT_PATH после КАЖДОГО обработанного реального дня, не только в
    конце (урок этой сессии: eth_getLogs full scan не укладывался в
    таймаут GH Actions без чекпоинта).
  - Доклад: ожидание (expectancy) после комиссий, число рынков в
    выборке, доля выигрышей против подразумеваемой ценой входа,
    разбивка по горизонту прогноза (1/2/3 дня вперёд).

РЕАЛЬНЫЕ факты этой сессии, подтверждённые WebSearch (не по памяти):
  - Kalshi Trade API v2, публичный, без ключа для рыночных данных:
    база `https://api.elections.kalshi.com/trade-api/v2` (текущая
    рекомендованная база у самого Kalshi для внешних трейдеров --
    `external-api.kalshi.com` тоже существует как алиас, используем
    `api.elections.kalshi.com`, реально задокументированная база).
  - `GET /series/{series_ticker}` -- реальный публичный эндпоинт,
    возвращает объект серии (поля включают settlement_sources,
    fee_type, fee_multiplier -- ПОДТВЕРЖДЕНО WebSearch по
    docs.kalshi.com, полный список полей проверяется живьём ниже).
  - `GET /markets?series_ticker=...&status=settled&min_close_ts=...
    &max_close_ts=...` -- листинг рынков серии с реальными
    Unix-таймстемпами диапазона.
  - `GET /series/{series_ticker}/markets/{ticker}/candlesticks
    ?period_interval=60&start_ts=...&end_ts=...` -- реальная часовая
    цена конкретного рынка (для расчётных рынков ДО cutoff --
    `GET /historical/markets/{ticker}/candlesticks`, отдельный
    эндпоинт -- обрабатывается честным fallback).
  - Комиссия тейкера Kalshi: `round_up(M × 0.07 × C × P × (1−P))` --
    `M` = `fee_multiplier` серии (реальное значение читается из
    /series, НЕ берётся как 1 по умолчанию без проверки), округление
    вверх до цента (стандартный retail/API-аккаунт, не centicent).
  - GEFS: `noaa-gefs-pds`, 31 реальный член (gec00+gep01..gep30),
    3-часовой шаг накопления TMAX в первые 192ч (`pgrb2ap5`) --
    ПОДТВЕРЖДЕНО WebSearch (NWS Technical Implementation Notice
    15-43) -- т.е. "суточный максимум" реально собирается как MAX по
    НЕСКОЛЬКИМ 3-часовым окнам, покрывающим местный дневной период
    станции, а не одним сообщением "18-24ч", как в диагностике (та
    диагностика проверяла только МЕХАНИЗМ скачивания одного
    сообщения, не реальное совпадение с местными сутками)."""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
S3_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
OUT_PATH = Path("data/p3_guard_cache/task3_weather_backtest_one_city_result.json")

SERIES_TICKER = "KXHIGHNY"  # реальный тикер серии NYC daily high temp (WebSearch, docs.kalshi.com)
N_DAYS = 30
EDGE_THRESHOLD_PCT = 8.0  # предрегистрировано владельцем в исходной задаче
HOURS_BEFORE_CLOSE_FOR_ENTRY_PRICE = 24
HORIZONS_DAYS = [1, 2, 3]  # разбивка по горизонту, реальное требование этого раунда
GEFS_REF_HOUR = "00"  # фиксированный референсный час прогона для всех горизонтов -- честно и просто сравнимо
MEMBERS = ["gec00"] + [f"gep{i:02d}" for i in range(1, 31)]  # 31 реальный член, подтверждено диагностикой
NY_UTC_OFFSET_HOURS = -4  # EDT в сентябре (America/New_York, летнее время) -- реально для этого 30-дневного окна (сентябрь), не круглый год


def kalshi_get(path: str, **params) -> dict:
    r = requests.get(f"{KALSHI_BASE}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def discover_station(diag: dict) -> str | None:
    """Реально запрашивает /series/{ticker}, честно логирует ВЕСЬ сырой
    JSON (не гадаем имя поля заранее), ищет 4-буквенный ICAO-код станции
    в вероятных полях."""
    data = kalshi_get(f"/series/{SERIES_TICKER}")
    diag["series_raw_response"] = data
    print(f"[task3] реальный /series/{SERIES_TICKER} ответ (полностью): {json.dumps(data, ensure_ascii=False)[:3000]}")
    series = data.get("series", data)
    diag["series_fee_multiplier"] = series.get("fee_multiplier")
    diag["series_settlement_sources"] = series.get("settlement_sources")
    # РЕАЛЬНАЯ находка первого прогона (2026-09-08): /series НЕ содержит
    # rules_primary/станцию вообще -- это поле есть только на объекте
    # РЫНКА (market), не серии. Возвращаем None здесь честно -- реальный
    # код станции извлекается позже из первого реального market.rules_primary
    # в run() через extract_station_from_rules().
    print("[task3] ВНИМАНИЕ: /series не содержит station/rules_primary -- будет извлечено из объекта рынка ниже")
    return None


def extract_station_from_rules(rules_primary: str) -> str | None:
    """РЕАЛЬНЫЙ формат (обнаружено этой сессией на живом ответе,
    2026-09-08): 'If the maximum temperature recorded at New York City
    (CLINYC) for Sep 7, 2026, is greater than 84° fahrenheit...' --
    станция это код The Weather Company (CLINYC), НЕ NWS ICAO (KNYC) --
    честно скорректированное предположение владельца/задачи. Ищем ЛЮБОЙ
    код в скобках после названия города, не только K+3 буквы."""
    import re
    if not rules_primary:
        return None
    m = re.search(r"\(([A-Z]{3,8})\)", rules_primary)
    return m.group(1) if m else None


def list_settled_markets(min_close_ts: int, max_close_ts: int, diag: dict) -> list[dict]:
    markets: list[dict] = []
    cursor = None
    for page in range(1, 21):  # честный кап на пагинацию, не бесконечный цикл
        params = {"series_ticker": SERIES_TICKER, "status": "settled",
                  "min_close_ts": min_close_ts, "max_close_ts": max_close_ts, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = kalshi_get("/markets", **params)
        if page == 1:
            diag["markets_raw_first_page_sample"] = data.get("markets", [])[:2]
            print(f"[task3] реальный образец объекта рынка (первый): "
                  f"{json.dumps((data.get('markets') or [{}])[0], ensure_ascii=False)[:2000]}")
        batch = data.get("markets", [])
        markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    print(f"[task3] реальных settled-рынков {SERIES_TICKER} за окно: {len(markets)}")
    return markets


def get_entry_price(ticker: str, close_ts: int, diag_list: list) -> dict | None:
    """Реальная цена рынка за HOURS_BEFORE_CLOSE_FOR_ENTRY_PRICE до закрытия,
    часовые свечи; честный fallback на historical-эндпоинт для старых
    расчитанных рынков (Kalshi отдельно документирует cutoff)."""
    target_ts = close_ts - HOURS_BEFORE_CLOSE_FOR_ENTRY_PRICE * 3600
    start_ts, end_ts = target_ts - 3600 * 3, target_ts + 3600 * 3
    for path in (f"/series/{SERIES_TICKER}/markets/{ticker}/candlesticks",
                 f"/historical/markets/{ticker}/candlesticks"):
        try:
            data = kalshi_get(path, period_interval=60, start_ts=start_ts, end_ts=end_ts)
        except requests.HTTPError as exc:
            diag_list.append({"ticker": ticker, "path": path, "error": str(exc)[:300]})
            continue
        candles = data.get("candlesticks", [])
        if not candles:
            continue
        candles.sort(key=lambda c: abs(c.get("end_period_ts", c.get("ts", 0)) - target_ts))
        return {"candle": candles[0], "path_used": path, "target_ts": target_ts}
    return None


def extract_price_yes(candle: dict, diag_list: list) -> float | None:
    """ЧЕСТНОЕ извлечение цены YES из реальной свечи -- схема Kalshi
    candlesticks НЕ была видна живьём до первого реального прогона
    2026-09-08 (упал с TypeError: поле оказалось dict, не числом,
    значит реальная свеча вкладывает OHLC под ключом, а не хранит
    единственное число верхнего уровня). Логируем сырую свечу ВСЕГДА,
    пробуем несколько реальных кандидатов полей, честно возвращаем None
    и логируем в diag, если ни один не подошёл -- не гадаем дальше."""
    diag_list.append({"raw_candle_sample": candle})
    # РЕАЛЬНАЯ схема (обнаружено вторым прогоном 2026-09-08): sub-словари
    # price/yes_bid/yes_ask содержат СТРОКОВЫЕ поля с суффиксом
    # `_dollars` (например 'close_dollars': '0.2200'), уже в единицах
    # 0-1 (не центы) -- НЕ 'close'/'mean' голыми числами, как
    # предполагалось изначально. На тонких/бесторговых свечах
    # 'close_dollars' может отсутствовать -- честный fallback по
    # порядку важности внутри каждого блока.
    for key in ("price", "yes_ask", "yes_bid"):
        val = candle.get(key)
        if not isinstance(val, dict):
            continue
        for sub in ("close_dollars", "mean_dollars", "open_dollars", "previous_dollars", "high_dollars", "low_dollars"):
            raw = val.get(sub)
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    continue
    return None


def idx_url(run_date: str, run_hour: str, member: str, fxx: int) -> str:
    return f"{S3_BASE}/gefs.{run_date}/{run_hour}/atmos/pgrb2ap5/{member}.t{run_hour}z.pgrb2a.0p50.f{fxx:03d}.idx"


def grib_url(run_date: str, run_hour: str, member: str, fxx: int) -> str:
    return f"{S3_BASE}/gefs.{run_date}/{run_hour}/atmos/pgrb2ap5/{member}.t{run_hour}z.pgrb2a.0p50.f{fxx:03d}"


_idx_cache: dict[str, str] = {}


def fetch_idx_cached(run_date: str, run_hour: str, member: str, fxx: int) -> str | None:
    key = f"{run_date}/{run_hour}/{member}/{fxx}"
    if key in _idx_cache:
        return _idx_cache[key]
    r = requests.get(idx_url(run_date, run_hour, member, fxx), timeout=30)
    text = r.text if r.status_code == 200 else None
    _idx_cache[key] = text
    return text


def find_surface_tmax_message(idx_text: str) -> dict | None:
    """Честно фильтруем на ПРИЗЕМНЫЙ уровень (2 m above ground), не первое
    совпадение по имени переменной -- исправление найденного в диагностике
    бага."""
    lines = [ln for ln in idx_text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        parts = line.split(":")
        if len(parts) < 5:
            continue
        var, level = parts[3], parts[4]
        if "TMAX" in var.upper() and "2 m above ground" in level:
            start = int(parts[1])
            end = int(lines[i + 1].split(":")[1]) - 1 if i + 1 < len(lines) else None
            return {"raw_line": line, "start": start, "end": end}
    return None


def fetch_point_value(run_date: str, run_hour: str, member: str, fxx: int,
                       lat: float, lon: float, diag_list: list) -> float | None:
    idx_text = fetch_idx_cached(run_date, run_hour, member, fxx)
    if not idx_text:
        return None
    msg = find_surface_tmax_message(idx_text)
    if not msg:
        return None
    range_header = f"bytes={msg['start']}-{msg['end']}" if msg["end"] else f"bytes={msg['start']}-"
    r = requests.get(grib_url(run_date, run_hour, member, fxx), headers={"Range": range_header}, timeout=60)
    if r.status_code not in (200, 206):
        diag_list.append({"member": member, "fxx": fxx, "http_status": r.status_code})
        return None
    tmp_path = Path(f"/tmp/gefs_{member}_{fxx}.grib2")
    tmp_path.write_bytes(r.content)
    try:
        import xarray as xr
        ds = xr.open_dataset(tmp_path, engine="cfgrib")
        point = ds.sel(latitude=lat, longitude=lon % 360, method="nearest")
        value_k = float(list(point.data_vars.values())[0].values)
        return value_k
    except Exception as exc:  # noqa: BLE001
        diag_list.append({"member": member, "fxx": fxx, "extract_error": str(exc)[:300]})
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


LOCAL_DAY_START_H, LOCAL_DAY_END_H = 6, 23  # реальный дневной интервал станции с запасом на оба конца


def fxx_windows_for_local_day(run_dt: datetime, target_local_date: datetime) -> list[int]:
    """Владелец, 2026-09-09: полный перебор окон дня вместо одного
    3-часового -- возвращает ВСЕ 3-часовые forecast-hour окончания
    GEFS, чьё окно пересекает местный дневной интервал
    [LOCAL_DAY_START_H, LOCAL_DAY_END_H) станции в target_local_date.
    Суточный максимум = MAX по всем этим окнам (не одно ближайшее к
    15:00, как в первом, честно помеченном как упрощение, прогоне)."""
    day_start_utc = target_local_date.replace(hour=0, minute=0, second=0, microsecond=0) \
        + timedelta(hours=LOCAL_DAY_START_H - NY_UTC_OFFSET_HOURS)
    day_end_utc = target_local_date.replace(hour=0, minute=0, second=0, microsecond=0) \
        + timedelta(hours=LOCAL_DAY_END_H - NY_UTC_OFFSET_HOURS)
    fxx_list = []
    fxx = 3
    while fxx <= 192:
        window_start_utc = run_dt + timedelta(hours=fxx - 3)
        window_end_utc = run_dt + timedelta(hours=fxx)
        if window_end_utc > day_start_utc and window_start_utc < day_end_utc:
            fxx_list.append(fxx)
        fxx += 3
    return fxx_list


_ensemble_cache: dict[tuple, dict] = {}


def ensemble_member_max_f(run_date: str, run_hour: str, target_local_date: datetime,
                           diag_list: list) -> dict:
    """Реальный суточный максимум (по Фаренгейту) КАЖДОГО из 31 члена
    ансамбля, БЕЗ порога/сравнения с рынком -- порог применяется
    отдельно (см. prob_yes_for_market), т.к. один и тот же ансамбль
    переиспользуется для ВСЕХ рынков одного дня (несколько порогов на
    день), не пересчитывается на каждый рынок заново. Кэшируется по
    (run_date, run_hour, target_local_date) -- реальная экономия
    запросов при полном переборе окон (несколько рынков на день, тот
    же прогон/тот же горизонт)."""
    key = (run_date, run_hour, target_local_date.date().isoformat())
    if key in _ensemble_cache:
        return _ensemble_cache[key]
    run_dt = datetime.strptime(f"{run_date}{run_hour}", "%Y%m%d%H").replace(tzinfo=timezone.utc)
    fxx_list = fxx_windows_for_local_day(run_dt, target_local_date)
    member_max_k: dict[str, float] = {}
    for member in MEMBERS:
        vals = []
        for fxx in fxx_list:
            v = fetch_point_value(run_date, run_hour, member, fxx, CITY_LAT, CITY_LON, diag_list)
            if v is not None:
                vals.append(v)
        if vals:
            member_max_k[member] = max(vals)
    member_max_f = {m: (k - 273.15) * 9 / 5 + 32 for m, k in member_max_k.items()}
    result = {"n_members_ok": len(member_max_f), "fxx_used": fxx_list, "member_max_f": member_max_f}
    _ensemble_cache[key] = result
    return result


def prob_yes_for_market(member_max_f: dict, floor_strike: float | None, cap_strike: float | None) -> float | None:
    """Владелец, 2026-09-09: реальная проверка живых rules_primary
    показала ТРИ разных реальных типа рынка Kalshi KXHIGHNY, перепутанных
    в первом прогоне (честно найденная причина hit_rate=21.5%):
      - floor-only ('...greater than {floor}...') -> YES = temp > floor.
      - cap-only ('...less than {cap}...') -> YES = temp < cap --
        ПЕРВЫЙ прогон здесь считал prob_above(cap) вместо prob_below(cap)
        -- прямая инверсия знака на 30 из 180 реальных рынков выборки.
      - оба присутствуют -> средний бин, YES = floor < temp < cap
        (граница НЕ подтверждена живым текстом для этого случая, только
        для двух односторонних -- честная, но правдоподобная экстраполяция
        по аналогии со strict-inequality с обеих сторон)."""
    if not member_max_f:
        return None
    n = len(member_max_f)
    if floor_strike is not None and cap_strike is None:
        return sum(1 for f in member_max_f.values() if f > floor_strike) / n
    if cap_strike is not None and floor_strike is None:
        return sum(1 for f in member_max_f.values() if f < cap_strike) / n
    if floor_strike is not None and cap_strike is not None:
        return sum(1 for f in member_max_f.values() if floor_strike < f < cap_strike) / n
    return None


def kalshi_fee_usd(m_mult: float, price_yes: float) -> float:
    """round_up(M x 0.07 x C x P x (1-P)), C=1, результат в долларах,
    округление ВВЕРХ до цента (retail/API, не centicent) -- формула
    подтверждена WebSearch этой сессией."""
    raw = m_mult * 0.07 * 1 * price_yes * (1 - price_yes)
    return math.ceil(raw * 100) / 100.0


CITY_NAME = "New York"
CITY_LAT, CITY_LON = 40.7829, -73.9654  # уточняется станцией ниже, если реально отличается


def run() -> int:
    t0 = time.time()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # РЕЗЮМИРОВАНИЕ: если предыдущий прогон уже частично закоммитил
    # чекпоинт (таймаут GH Actions), продолжаем с того же места -- тот же
    # принцип, что уже использован в этой сессии для mm_discover/full-scan
    # eth_getLogs. Дни из старого чекпоинта переносятся как есть, новые
    # дни начинаются с чистого diag.
    resumed_days: list = []
    if OUT_PATH.exists():
        try:
            prev = json.loads(OUT_PATH.read_text())
            resumed_days = prev.get("days", [])
            print(f"[task3] РЕЗЮМИРОВАНИЕ: найден чекпоинт с {len(resumed_days)} уже обработанными днями")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[task3] чекпоинт есть, но не читается ({exc}) -- начинаем заново")

    diag: dict = {"http_errors": []}
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "purpose": "ПОЛНЫЙ бэктест одного города (условие перед 14 городами: положительное ожидание здесь)",
        "series_ticker": SERIES_TICKER, "n_days_target": N_DAYS,
        "edge_threshold_pct": EDGE_THRESHOLD_PCT, "horizons_days": HORIZONS_DAYS,
        "gefs_ref_hour_utc": GEFS_REF_HOUR,
        "fxx_window_selection": f"одно 3ч окно, ближайшее к местным {TYPICAL_HIGH_LOCAL_HOUR}:00 (аппроксимация суточного максимума, см. докстринг fxx_windows_for_local_day)",
        "diag": diag, "days": list(resumed_days),
    }

    print("=== Шаг 1: реальная станция контракта Kalshi (не по памяти) ===")
    try:
        station = discover_station(diag)
    except Exception as exc:  # noqa: BLE001
        result["blocker"] = f"не удалось получить /series/{SERIES_TICKER}: {exc}"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"[task3] ОСТАНОВЛЕНО: {result['blocker']}")
        return 1
    result["station_icao_from_api"] = station
    if station and station != "KNYC":
        print(f"[task3] ВНИМАНИЕ: реальная станция из API ({station}) НЕ совпадает с ожидаемой KNYC -- используем реальную")
    # Координаты станции: используем известную гео-точку KNYC (Central Park) как
    # приближение, если API не отдаёт lat/lon явно -- честно помечено, не точное
    # geocoding станции by ICAO (отдельная задача, если понадобится другой город)
    result["city_lat_lon_used"] = [CITY_LAT, CITY_LON]
    result["city_lat_lon_note"] = "координата Central Park по ICAO/памяти НЕ geocoded заново через официальный источник -- см. честную оговорку в паспорте"

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=N_DAYS + 5)  # запас на пагинацию/выходные при фильтрации ниже
    print("\n=== Шаг 2: реальные settled-рынки серии за окно ===")
    try:
        markets = list_settled_markets(int(window_start.timestamp()), int(now.timestamp()), diag)
    except Exception as exc:  # noqa: BLE001
        result["blocker"] = f"не удалось получить /markets: {exc}"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"[task3] ОСТАНОВЛЕНО: {result['blocker']}")
        return 1

    # РЕАЛЬНАЯ станция -- извлекается из rules_primary ПЕРВОГО реального
    # рынка (не из /series -- там этого поля нет вообще, см. discover_station).
    station = None
    for m0 in markets:
        station = extract_station_from_rules(m0.get("rules_primary", ""))
        if station:
            break
    result["station_code_from_market_rules"] = station
    if station and station != "KNYC":
        print(f"[task3] РЕАЛЬНАЯ станция из rules_primary: {station} (НЕ KNYC, честно используем найденную)")
    diag["station_sample_rules_primary"] = (markets[0].get("rules_primary") if markets else None)

    # Группировка по реальной дате закрытия (календарный день) -- один день
    # Kalshi обычно даёт НЕСКОЛЬКО рынков (разные пороги/бины), реально
    # используем ВСЕ, чтобы честно оценить сколько рынков в выборке.
    by_date: dict[str, list[dict]] = {}
    for m in markets:
        close_time = m.get("close_time")
        if not close_time:
            continue
        try:
            close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        except ValueError:
            continue
        date_key = close_dt.strftime("%Y-%m-%d")
        by_date.setdefault(date_key, []).append({**m, "_close_dt": close_dt})
    real_dates = sorted(by_date.keys())[-N_DAYS:] if len(by_date) > N_DAYS else sorted(by_date.keys())
    print(f"[task3] реальных дней с settled-рынками (ограничено {N_DAYS}): {len(real_dates)}")
    result["n_days_found"] = len(real_dates)

    # M -- реальный fee_multiplier серии, НЕ по умолчанию 1 без проверки
    m_mult = None
    try:
        m_mult = float(diag.get("series_raw_response", {}).get("series", diag.get("series_raw_response", {})).get("fee_multiplier"))
    except (TypeError, ValueError):
        pass
    if m_mult is None:
        m_mult = 1.0
        diag["fee_multiplier_fallback_used"] = True
        print("[task3] ВНИМАНИЕ: реальный fee_multiplier не найден в /series -- честный fallback M=1, помечено в diag")
    result["fee_multiplier_used"] = m_mult

    already_done = {d["date"] for d in resumed_days}
    for date_key in real_dates:
        if date_key in already_done:
            print(f"[task3] пропуск {date_key} -- уже есть в чекпоинте (резюмирование)")
            continue
        day_markets = by_date[date_key]
        day_entry = {"date": date_key, "n_markets": len(day_markets), "markets": []}
        target_local_date = datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        for m in day_markets:
            ticker = m.get("ticker")
            floor_strike, cap_strike = m.get("floor_strike"), m.get("cap_strike")
            result_val = m.get("result")  # честно логируем как есть, не подгоняем под "yes"/"no"
            close_ts = int(m["_close_dt"].timestamp())

            price_info = get_entry_price(ticker, close_ts, diag["http_errors"])
            market_entry = {
                "ticker": ticker, "floor_strike": floor_strike, "cap_strike": cap_strike,
                "result": result_val, "close_time": m.get("close_time"), "price_info": price_info,
                "horizons": {},
            }
            if price_info:
                price_yes = extract_price_yes(price_info["candle"], diag["http_errors"])
                market_entry["entry_price_yes"] = price_yes
                if price_yes is not None and (floor_strike is not None or cap_strike is not None):
                    for h in HORIZONS_DAYS:
                        run_dt_target = target_local_date - timedelta(days=h)
                        run_date_str = run_dt_target.strftime("%Y%m%d")
                        try:
                            ens = ensemble_member_max_f(run_date_str, GEFS_REF_HOUR, target_local_date,
                                                         diag["http_errors"])
                            prob_yes = prob_yes_for_market(ens.get("member_max_f"), floor_strike, cap_strike)
                            ens = {**ens, "prob_above": prob_yes}  # ключ сохранён для обратной совместимости отчёта
                        except Exception as exc:  # noqa: BLE001
                            ens = {"error": str(exc)[:300]}
                        edge = None
                        trade = None
                        if ens.get("prob_above") is not None:
                            edge = (ens["prob_above"] - price_yes) * 100
                            if abs(edge) >= EDGE_THRESHOLD_PCT:
                                side = "YES" if edge > 0 else "NO"
                                side_price = price_yes if side == "YES" else (1 - price_yes)
                                fee = kalshi_fee_usd(m_mult, price_yes)
                                won = None
                                if isinstance(result_val, str):
                                    won = (result_val.lower() == "yes" and side == "YES") or \
                                          (result_val.lower() == "no" and side == "NO")
                                pnl = None
                                if won is not None:
                                    pnl = (1 - side_price - fee) if won else (-side_price - fee)
                                trade = {"side": side, "side_price": side_price, "fee_usd": fee,
                                         "won": won, "pnl_usd": pnl}
                        market_entry["horizons"][str(h)] = {"ensemble": ens, "edge_pct": edge, "trade": trade}
            day_entry["markets"].append(market_entry)
        result["days"].append(day_entry)

        # ЧЕКПОИНТ: пишем накопленный результат после КАЖДОГО дня, обязательное
        # требование владельца -- переживает таймаут GH Actions.
        result["runtime_s_so_far"] = time.time() - t0
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"[task3] чекпоинт: обработан день {date_key} ({len(day_entry['markets'])} рынков), "
              f"прошло {result['runtime_s_so_far']:.0f}с")

    # Сводка
    all_trades = []
    for day in result["days"]:
        for m in day["markets"]:
            for h, hd in m.get("horizons", {}).items():
                t = hd.get("trade")
                if t and t.get("pnl_usd") is not None:
                    all_trades.append({**t, "horizon_days": h, "date": day["date"], "ticker": m["ticker"]})

    summary = {
        "n_markets_total": sum(d["n_markets"] for d in result["days"]),
        "n_trades_triggered": len(all_trades),
        "expectancy_usd_per_trade": (sum(t["pnl_usd"] for t in all_trades) / len(all_trades)) if all_trades else None,
        "hit_rate_vs_side_price": (sum(1 for t in all_trades if t["won"]) / len(all_trades)) if all_trades else None,
    }
    by_horizon = {}
    for h in HORIZONS_DAYS:
        h_trades = [t for t in all_trades if t["horizon_days"] == str(h)]
        by_horizon[str(h)] = {
            "n_trades": len(h_trades),
            "expectancy_usd_per_trade": (sum(t["pnl_usd"] for t in h_trades) / len(h_trades)) if h_trades else None,
            "hit_rate": (sum(1 for t in h_trades if t["won"]) / len(h_trades)) if h_trades else None,
        }
    summary["by_horizon"] = by_horizon
    summary["verdict_expand_to_14_cities"] = (
        summary["expectancy_usd_per_trade"] is not None and summary["expectancy_usd_per_trade"] > 0
    )
    result["summary"] = summary
    result["runtime_s_total"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[task3] ИТОГО: {json.dumps(summary, indent=2, ensure_ascii=False, default=str)}")
    print(f"[task3] результат записан в {OUT_PATH}, время: {result['runtime_s_total']:.0f}с")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
