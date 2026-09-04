#!/usr/bin/env python3
"""Владелец, 2026-09-04, запрос 3 (сток-токены на выходных vs EDGAR).

SEC EDGAR: бесплатно, без ключа. User-Agent ОБЯЗАТЕЛЕН с реальным
контактом (владелец: "SamanaIn Samana11@gmail.com") -- при проблемах
SEC блокирует по IP, это боевой хост. Лимит 10 запросов/с -- честный
рейт-лимитер ниже (макс. 8/с с запасом).

Точный момент появления информации -- ACCEPTANCE-DATETIME из SGML-
заголовка полного текстового файла заявки (submissions.json даёt
только filingDate, дату без времени -- недостаточно для окна
"первые 4 часа после подачи"). Это стандартный, документированный
способ получить точное время приёмки EDGAR.

Окно "тёмное" (акция не торгуется НИГДЕ, владелец): пятница 20:00 ET
-> воскресенье 20:00 ET (будние ночи покрыты Robinhood 24h Market
через Blue Ocean ATS) + праздники NYSE. Праздники считаются
алгоритмически по стандартным правилам NYSE (см. nyse_holidays_us),
не выдуманы и не взяты из памяти как список дат -- фиксированные
даты/N-й день недели месяца, кроме Страстной пятницы (вычисляется
через алгоритм Пасхи).

Список тикеров -- РЕАЛЬНЫЙ полный реестр (203 токена, Dune
`rwa_stock_factory_robinhood.factory_deployer_evt_deployed`, см.
`data/rwa_stock_token_registry.json`) -- заменяет предварительный
список из 9 тикеров прошлой P4-разведки. Часть тикеров -- ETF/облигации
(QQQ, SPY, GLD, SLV, BND, SHY, SGOV, VTI, SMH, SOXX, XLK и др.) -- они
не подают 8-K по определению (регулируются иначе, N-CEN/N-PORT), для
них просто не будет находок, это не ошибка и не повод исключать их из
списка заранее.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

OUT_PATH = Path("data/p3_guard_cache/edgar_8k_result.json")
USER_AGENT = "SamanaIn Samana11@gmail.com"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
REQUEST_INTERVAL_S = 1.0 / 8  # владелец: лимит 10/с, берём 8/с с запасом
ET = ZoneInfo("America/New_York")

REGISTRY_PATH = Path("data/rwa_stock_token_registry.json")


def load_tickers() -> list[str]:
    if REGISTRY_PATH.exists():
        reg = json.loads(REGISTRY_PATH.read_text())
        return sorted(reg["tokens"].keys())
    # Резервный список, только если реестр почему-то ещё не собран --
    # см. HISTORY: 9 тикеров прошлой P4-разведки, честно меньше реальности.
    return ["NVDA", "QQQ", "RDDT", "COST", "GME", "RBLX", "LLY", "SPY", "MSTR"]


PROVISIONAL_TICKERS = load_tickers()

# Наблюдаемое окно данных на цепи (первый своп нашего типа ~2026-07-06,
# см. data/p3_guard_cache/dune_query1_volume_result.json weekly_volume_full_history).
FILING_DATE_FROM = date(2026, 7, 1)

_last_request_at = 0.0


def _rate_limited_get(url: str, **kwargs) -> requests.Response:
    global _last_request_at
    wait = _last_request_at + REQUEST_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("timeout", 30)
    resp = requests.get(url, **kwargs)
    _last_request_at = time.monotonic()
    return resp


def easter_sunday(year: int) -> date:
    """Алгоритм Гаусса/анонимный григорианский -- дата католической Пасхи."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-й (1-based) заданный weekday (0=понедельник) месяца."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def observed(d: date) -> date:
    """Если праздник выпадает на выходной -- NYSE переносит на ближайший будний день."""
    if d.weekday() == 5:  # суббота -> пятница
        return d - timedelta(days=1)
    if d.weekday() == 6:  # воскресенье -> понедельник
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> list[date]:
    """Стандартные праздники NYSE, вычислены алгоритмически (не список
    дат из памяти) -- Новый год, MLK (3-й пн января), День Вашингтона
    (3-й пн февраля), Страстная пятница (Пасха - 2 дня), День памяти
    (последний пн мая), Джунтинт (19 июня), День независимости (4 июля),
    День труда (1-й пн сентября), День благодарения (4-й чт ноября),
    Рождество (25 декабря)."""
    easter = easter_sunday(year)
    return sorted([
        observed(date(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        easter - timedelta(days=2),
        last_weekday(year, 5, 0),
        observed(date(year, 6, 19)),
        observed(date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed(date(year, 12, 25)),
    ])


def is_dark_window(dt_utc: datetime, holidays_by_year: dict[int, list[date]]) -> bool:
    """Тёмное окно: пятница 20:00 ET -> воскресенье 20:00 ET, плюс
    расширение на праздники NYSE (будний праздник, примыкающий к
    выходным, продлевает тёмное окно на весь этот день)."""
    dt_et = dt_utc.astimezone(ET)
    wd = dt_et.weekday()  # 0=Mon ... 4=Fri, 5=Sat, 6=Sun
    t = dt_et.time()
    if wd == 4 and t.hour >= 20:  # пятница после 20:00
        return True
    if wd == 5:  # суббота целиком
        return True
    if wd == 6 and t.hour < 20:  # воскресенье до 20:00
        return True
    holidays = holidays_by_year.get(dt_et.year, [])
    if dt_et.date() in holidays:
        return True
    return False


def get_cik_map(tickers: list[str]) -> dict[str, dict]:
    resp = _rate_limited_get("https://www.sec.gov/files/company_tickers.json")
    resp.raise_for_status()
    data = resp.json()
    by_ticker = {v["ticker"].upper(): v for v in data.values()}
    result = {}
    for t in tickers:
        entry = by_ticker.get(t.upper())
        if entry:
            result[t] = {"cik": entry["cik_str"], "title": entry["title"]}
        else:
            result[t] = {"cik": None, "title": None, "note": "тикер не найден в company_tickers.json"}
    return result


def get_8k_filings(cik: int, from_date: date) -> list[dict]:
    cik10 = str(cik).zfill(10)
    try:
        resp = _rate_limited_get(f"https://data.sec.gov/submissions/CIK{cik10}.json")
    except requests.exceptions.RequestException as exc:
        return [{"error": f"сеть: {exc}"}]
    if resp.status_code != 200:
        return [{"error": f"status={resp.status_code}"}]
    data = resp.json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates_ = recent.get("filingDate", [])
    accns = recent.get("accessionNumber", [])
    out = []
    for form, fdate, accn in zip(forms, dates_, accns):
        if form != "8-K":
            continue
        d = datetime.strptime(fdate, "%Y-%m-%d").date()
        if d < from_date:
            continue
        out.append({"form": form, "filing_date": fdate, "accession": accn})
    return out


def get_acceptance_datetime(cik: int, accession: str) -> str | None:
    """ACCEPTANCE-DATETIME из SGML-заголовка полного текстового файла --
    единственный точный источник времени (не только даты). Полный .txt
    может весить мегабайты (приложенные exhibit'ы) -- заголовок всегда
    в первых нескольких КБ, Range-запрос вместо скачивания всего файла
    (реальная причина первого падения: ReadTimeout на большом файле,
    30с не хватило). Любая сетевая ошибка здесь -- None, не крашит
    остальной прогон (см. вызывающий код)."""
    # ВАЖНО (найдено 2026-09-04 через analysis/edgar_acceptance_debug.py,
    # 5-way URL probe): плоский .txt-путь требует accession С ДЕФИСАМИ.
    # accn_nodash (без дефисов) верен для имени ПОДКАТАЛОГА заявки, но
    # не для этого плоского файла -- со strip'ом дефисов EDGAR отдаёт 404
    # молча (для этого URL), из-за чего был 0/379 в первом полном прогоне.
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}.txt"
    try:
        # Accept-Encoding: identity -- ВАЖНО вместе с Range. gzip + partial
        # byte-range не декомпрессируется (частичный сжатый поток), и
        # requests либо отдаёт мусор, либо пустой text без исключения --
        # реальная причина 0/379 в первом прогоне после фикса таймаута
        # (никаких ошибок в логе не было, просто ни разу не совпал regex).
        resp = _rate_limited_get(
            url, headers={**HEADERS, "Range": "bytes=0-8192", "Accept-Encoding": "identity"}, timeout=15,
        )
    except requests.exceptions.RequestException as exc:
        print(f"    [edgar] acceptance-datetime сеть-ошибка {accession}: {exc}")
        return None
    if resp.status_code not in (200, 206):
        print(f"    [edgar] acceptance-datetime {accession}: HTTP {resp.status_code}")
        return None
    m = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", resp.text)
    if not m:
        print(f"    [edgar] acceptance-datetime {accession}: паттерн не найден в первых {len(resp.content)} байтах")
        return None
    raw = m.group(1)
    # ВАЖНО: ACCEPTANCE-DATETIME в SGML-заголовке EDGAR -- локальное
    # время Eastern (ET), НЕ UTC (задокументированное поведение EDGAR,
    # не предположение) -- критично для точной классификации "тёмного
    # окна", которое само определено в ET.
    dt_et = datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=ET)
    return dt_et.astimezone(timezone.utc).isoformat()


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "user_agent": USER_AGENT, "tickers_provisional": PROVISIONAL_TICKERS}

    holidays_by_year = {y: [str(d) for d in nyse_holidays(y)] for y in (2026, 2027)}
    result["nyse_holidays_computed"] = holidays_by_year

    print("=== 1. Ticker -> CIK ===")
    cik_map = get_cik_map(PROVISIONAL_TICKERS)
    result["cik_map"] = cik_map
    for t, v in cik_map.items():
        print(f"  {t}: CIK={v.get('cik')} title={v.get('title')}")

    print("\n=== 2. 8-K filings + ACCEPTANCE-DATETIME по каждому тикеру ===")
    filings_by_ticker: dict[str, list[dict]] = {}

    def _save_partial() -> None:
        """Пишем результат ПОСЛЕ КАЖДОГО тикера -- реальный сбой
        2026-09-04 (ReadTimeout на одном большом .txt) уронил весь
        прогон без единой сохранённой строки, хотя десятки тикеров уже
        были обработаны. Теперь падение теряет максимум один тикер."""
        result["filings_by_ticker"] = filings_by_ticker
        result["runtime_s"] = time.time() - t0
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    for ticker, info in cik_map.items():
        cik = info.get("cik")
        if not cik:
            filings_by_ticker[ticker] = []
            _save_partial()
            continue
        try:
            filings = get_8k_filings(cik, FILING_DATE_FROM)
            for f in filings:
                if "error" in f:
                    continue
                f["acceptance_datetime_utc_naive"] = get_acceptance_datetime(cik, f["accession"])
        except Exception as exc:  # noqa: BLE001 -- один тикер не должен ронять весь прогон
            filings = [{"error": f"необработанное исключение: {exc}"}]
            print(f"  {ticker}: ОШИБКА {exc}")
        filings_by_ticker[ticker] = filings
        print(f"  {ticker}: {len(filings)} форм 8-K с {FILING_DATE_FROM}")
        _save_partial()

    print(f"\n[edgar] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
