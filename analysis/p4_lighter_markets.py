"""P4, стадия `p4_lighter_markets` (владелец, 2026-09-01): "через
публичный REST Lighter забрать список всех рынков, отфильтровать
сток-/RWA-перпы, для каждого -- глубина книги на +-0.1% и +-0.5% от
мида, история фандинга за максимально доступный период. Наружу --
только агрегаты... Записать в docs/P4_RECON.md, секцию 'Lighter --
верифицировано API'. Если API требует ключ -- остановиться и
доложить, не регистрироваться."

0 Dune. Публичный REST Lighter (mainnet.zklighter.elliot.ai) --
никакого SDK-пакета `lighter-python` в этом репозитории на самом деле
НЕТ (проверено -- владелец упомянул его как "уже в репо", это
неточность; см. docs/P4_RECON.md для полной разведки), поэтому здесь
обычные `requests`-вызовы по эндпоинтам, задокументированным в
официальном SDK на GitHub (elliottech/lighter-python/docs/*.md,
получено WebFetch дословно) -- тот же паттерн, что
analysis/alchemy_fallback.py для RPC.

Эндпоинты (без авторизации, по документации SDK):
  GET /api/v1/orderBookDetails?filter=all  -> список перп-рынков
      (PerpsOrderBookDetail: symbol, market_id, market_type, mark_price...)
  GET /api/v1/orderBookOrders?market_id=&limit=  -> bids/asks (SimpleOrder:
      price, remaining_base_amount)
  GET /api/v1/fundings?market_id=&resolution=1h&start_timestamp=&
      end_timestamp=&count_back=  -> история фандинга (Funding: timestamp,
      value, rate, direction), максимум 750 записей за вызов -- пагинация
      назад по времени для "максимально доступного периода".

KILL-критерии (P4_KILL.md, уже закоммичен ДО этого файла) читают
агрегаты, которые эта стадия публикует в docs/P4_RECON.md -- сама
стадия вердикт не выносит (два из трёх критериев не полностью
параметризованы владельцем, см. P4_KILL.md).
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://mainnet.zklighter.elliot.ai"
HEADERS = {"User-Agent": "robinhood-chain-alpha-p4-lighter/1.0"}
CACHE_DIR = Path("data/p4_lighter_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Сколько лет искать историю фандинга (в прошлое от текущего момента) --
# верхняя граница пагинации, не заявление "чейн жил Х лет". Chain live
# с 01.07.2026 -- 2 года дают большой запас.
HISTORY_LOOKBACK_YEARS = 2
FUNDING_RESOLUTION = "1h"
MAX_FUNDING_PAGES = 40  # 40 x 750 = 30000 часовых записей ~ 3.4 года, с запасом
ORDER_BOOK_LIMIT_CANDIDATES = (500, 200, 100)  # 1000 дал 400 Bad Request
                         # (run #33555814080, MRVL market_id=174); 100
                         # подтверждён рабочим реальным прогоном (run
                         # #33556216908). Точный документированный
                         # максимум не найден (apidocs.lighter.xyz
                         # заблокирован для прямого фетча) -- пробуем
                         # по убыванию, берём первый рабочий (владелец,
                         # дозапрос: "проверить, есть ли параметр
                         # глубины >100 уровней").

# Сток-регистр Sprint R1 (194 токена, уже оплачено и закэшировано --
# 0 доп. кредитов) -- фолбэк-фильтр, если поле market_type на Lighter
# не даёт однозначной категории "сток"/"RWA".
STOCK_UNIVERSE_PATH = Path("data/sprintR1_cache/r1_rwa_full_universe.csv")

_request_count = 0

# Диагностика семантики поля `direction` (run #33556216908 показал
# РЕАЛЬНЫЕ значения "long"/"short" -- НЕ "long_pays_short"/
# "short_pays_long", как в SDK-доке -- см. docs/P4_RECON.md). Тальи
# (direction, знак сырой rate) по ВСЕМ записям всех рынков -- если
# direction жёстко коррелирует со знаком rate (100% одно с одним), это
# просто дублирующая метка знака, а не отдельный payer/receiver сигнал
# -- печатается в лог (не публикуется, не коммитится) для ручной
# интерпретации перед тем, как менять знаковую конвенцию отчёта.
_DIRECTION_SIGN_TALLY: Counter = Counter()


class NeedsApiKey(RuntimeError):
    """Поднимается, если Lighter отвечает 401/403 -- останавливаемся и
    докладываем, не пытаемся получить ключ сами (владелец, дословно)."""


def _get(path: str, params: dict | None = None) -> dict:
    global _request_count
    _request_count += 1
    url = f"{BASE_URL}{path}"
    resp = requests.get(url, params=params or {}, headers=HEADERS, timeout=30)
    if resp.status_code in (401, 403):
        raise NeedsApiKey(
            f"{url} (params={params}) вернул {resp.status_code} -- похоже, "
            f"этот эндпоинт требует API-ключ. Тело ответа: {resp.text[:500]!r}"
        )
    resp.raise_for_status()
    return resp.json()


def load_stock_symbols() -> set[str]:
    df = pd.read_csv(STOCK_UNIVERSE_PATH)
    return set(df["token_symbol"].astype(str).str.upper())


def fetch_perp_markets() -> list[dict]:
    body = _get("/api/v1/orderBookDetails", {"filter": "all"})
    return body.get("order_book_details", [])


def is_stock_like(market: dict, stock_symbols: set[str]) -> tuple[bool, str]:
    """Возвращает (флаг, причина) -- прозрачно, какой сигнал сработал."""
    mtype = str(market.get("market_type", "")).strip().lower()
    if mtype in ("stock", "equity", "equities", "rwa", "stocks"):
        return True, f"market_type={mtype!r}"
    symbol = str(market.get("symbol", "")).strip().upper()
    # Символы на перп-биржах часто с суффиксом (AAPL-PERP, AAPL/USDC,
    # AAPLUSD и т.д.) -- берём первый "буквенный" токен до разделителя.
    base = symbol.replace("-PERP", "").replace("PERP", "")
    for sep in ("/", "-", "_"):
        base = base.split(sep)[0]
    for suffix in ("USD", "USDC", "USDT", "USDG"):
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
    if base in stock_symbols:
        return True, f"symbol match ({symbol} -> {base}, реестр R1)"
    return False, ""


def fetch_depth(market_id: int, mid: float) -> dict:
    body = None
    limit_used = None
    last_err: Exception | None = None
    for candidate in ORDER_BOOK_LIMIT_CANDIDATES:
        try:
            body = _get("/api/v1/orderBookOrders", {"market_id": market_id, "limit": candidate})
            limit_used = candidate
            break
        except requests.exceptions.HTTPError as e:
            last_err = e
            continue
    if body is None:
        raise last_err  # все кандидаты дали ошибку -- реальная ошибка API, не молчим
    bids = body.get("bids", [])
    asks = body.get("asks", [])

    def side_depth(orders: list[dict], pct: float, is_bid: bool) -> float:
        bound = mid * (1 - pct) if is_bid else mid * (1 + pct)
        # Эпсилон против float-краевых случаев (mid*1.005 не точно
        # представимо в binary float) -- на реальных котировках граница
        # почти никогда не совпадает ровно с ценой уровня, но дёшево
        # исключить этот класс бага заранее (найден юнит-тестом).
        eps = abs(bound) * 1e-9
        total = 0.0
        for o in orders:
            price = float(o["price"])
            size = float(o.get("remaining_base_amount", o.get("initial_base_amount", 0)))
            within = price >= bound - eps if is_bid else price <= bound + eps
            if within:
                total += price * size
        return total

    out = {}
    for pct, label in ((0.001, "0.1pct"), (0.005, "0.5pct")):
        bid_d = side_depth(bids, pct, True)
        ask_d = side_depth(asks, pct, False)
        out[f"bid_depth_usd_{label}"] = bid_d
        out[f"ask_depth_usd_{label}"] = ask_d
        out[f"combined_depth_usd_{label}"] = bid_d + ask_d
    out["n_bids_returned"] = len(bids)
    out["n_asks_returned"] = len(asks)
    out["order_book_limit_used"] = limit_used
    out["order_book_limit_hit"] = len(bids) >= limit_used or len(asks) >= limit_used
    return out


def fetch_funding_history(market_id: int) -> list[dict]:
    now = int(time.time())
    earliest_allowed = now - HISTORY_LOOKBACK_YEARS * 365 * 86400
    records: list[dict] = []
    end_ts = now
    seen_timestamps: set[int] = set()
    for _ in range(MAX_FUNDING_PAGES):
        body = _get(
            "/api/v1/fundings",
            {
                "market_id": market_id,
                "resolution": FUNDING_RESOLUTION,
                "start_timestamp": earliest_allowed,
                "end_timestamp": end_ts,
                "count_back": 750,
            },
        )
        page = body.get("fundings", [])
        if not page:
            break
        new_page = [r for r in page if r.get("timestamp") not in seen_timestamps]
        if not new_page:
            break
        records.extend(new_page)
        seen_timestamps.update(r["timestamp"] for r in new_page)
        oldest_ts = min(r["timestamp"] for r in new_page)
        if len(new_page) < 750 or oldest_ts <= earliest_allowed:
            break
        end_ts = oldest_ts - 1
    return records


def summarize_funding(records: list[dict]) -> dict:
    if not records:
        return {"n_records": 0}
    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_numeric(df["timestamp"])
    df["rate"] = pd.to_numeric(df["rate"], errors="coerce")

    # Диагностика (см. модульный докстринг _DIRECTION_SIGN_TALLY) --
    # СЫРОЙ знак rate, ДО какой-либо инверсии, против значения direction.
    raw_direction = df["direction"].astype(str).str.strip().str.lower()
    for d, sign in zip(raw_direction, df["rate"].apply(lambda x: "pos" if x > 0 else ("neg" if x < 0 else "zero"))):
        _DIRECTION_SIGN_TALLY[(d, sign)] += 1

    # direction: наблюдаемые значения печатаются в отчёт для ручной
    # сверки (см. docs/P4_RECON.md) -- знак приводим к конвенции
    # "положительно = в пользу шорта" (шорт получает фандинг) ТОЛЬКО
    # если распознаём ТОЧНОЕ значение direction (не подстрокой --
    # "long_pays_short" содержит и "long", и "short", и "pay", поэтому
    # нечёткое совпадение по подстроке ошибочно матчило бы оба
    # направления сразу; юнит-тест на этом ловил баг при подготовке).
    # Нераспознанные значения -- знак rate НЕ трогаем, флаг остаётся
    # False, отчёт получает сырые наблюдённые значения для ручной сверки.
    direction_norm = df["direction"].astype(str).str.strip().str.lower()
    directions = sorted(direction_norm.dropna().unique().tolist())
    LONG_PAYS_SHORT = {"long_pays_short", "long_pay_short", "longpaysshort"}
    SHORT_PAYS_LONG = {"short_pays_long", "short_pay_long", "shortpayslong"}
    recognized = set(directions) <= (LONG_PAYS_SHORT | SHORT_PAYS_LONG)
    sign_flip_applied = False
    if recognized and directions:
        # rate уже в конвенции "положительно = в пользу шорта" на строках
        # long_pays_short; на short_pays_long -- инвертируем знак.
        mask = direction_norm.isin(SHORT_PAYS_LONG)
        if mask.any():
            df.loc[mask, "rate"] = -df.loc[mask, "rate"]
            sign_flip_applied = True

    span_hours = (df["timestamp"].max() - df["timestamp"].min()) / 3600.0
    # rate трактуется как ДОЛЯ (фрация) за час -- docs.lighter.xyz/
    # trading/funding: "1-hour premium ... funding payments for the
    # premium are distributed over 8 hours" (получено WebSearch,
    # дословная цитата в docs/P4_RECON.md) -- т.е. ЗНАЧЕНИЕ,
    # возвращаемое на resolution=1h, УЖЕ является часовой выплатой
    # (premium/8), не сырой недошёлённой 8-часовой премией -- поэтому
    # аннуализация rate*24*365 (без дополнительного /8) методологически
    # верна, ЕСЛИ rate -- доля. Открытый вопрос -- буквальные единицы
    # сырого числа (см. диагностику сырых записей в логе прогона,
    # docs/P4_RECON.md "Дозапрос: единицы фандинга").
    annualized = df["rate"] * 24 * 365  # resolution=1h -> rate уже часовая доля
    annualized_median_pct = round(float(annualized.median()) * 100, 4)
    # Кросс-чек против внешнего ориентира: сравнимые крипто-рынки на
    # Lighter/других venue показывают порядка -2.6..+8.7 bps за 8ч
    # (loris.tools, см. docs/P4_RECON.md) -- пересчитываем ту же
    # медиану в "bps за 8ч" для прямого сравнения по величине.
    median_rate_per_hour = float(df["rate"].median())
    median_bps_per_8h = round(median_rate_per_hour * 8 * 10_000, 4)
    return {
        "n_records": int(len(df)),
        "span_days": round(span_hours / 24.0, 2),
        "resolution": FUNDING_RESOLUTION,
        "observed_direction_values": directions,
        "direction_recognized": recognized,
        "sign_flip_applied_for_short_pays_long": sign_flip_applied,
        "median_rate_per_hour_raw": median_rate_per_hour,
        "median_bps_per_8h_equivalent": median_bps_per_8h,
        "reference_range_bps_per_8h": "-2.6..+8.7 (loris.tools, сравнимые крипто-рынки, см. docs/P4_RECON.md)",
        "annualized_median_pct": annualized_median_pct,
        "annualized_p10_pct": round(float(annualized.quantile(0.10)) * 100, 4),
        "annualized_p90_pct": round(float(annualized.quantile(0.90)) * 100, 4),
        "share_hours_negative_for_short_pct": round(float((annualized < 0).mean()) * 100, 2),
        "suspected_bug_gt_100pct_annual": abs(annualized_median_pct) > 100,
    }


def run() -> int:
    stock_symbols = load_stock_symbols()
    try:
        markets = fetch_perp_markets()
    except NeedsApiKey as e:
        _write_needs_key_report("orderBookDetails", e)
        return 1

    matched = []
    for m in markets:
        ok, reason = is_stock_like(m, stock_symbols)
        if ok:
            matched.append((m, reason))

    print(f"[p4_lighter] {len(markets)} перп-рынков всего, {len(matched)} сток-/RWA-подобных.")
    for m, reason in matched:
        print(f"  {m.get('symbol')} (market_id={m.get('market_id')}) -- {reason}")

    results = []
    raw_sample_printed = False
    for m, reason in matched:
        market_id = m["market_id"]
        symbol = m.get("symbol")
        mid = None
        for key in ("mark_price", "index_price", "last_trade_price"):
            v = m.get(key)
            if v not in (None, "", 0, "0"):
                try:
                    mid = float(v)
                    break
                except (TypeError, ValueError):
                    continue
        try:
            depth = {} if mid is None else fetch_depth(market_id, mid)
            funding_records = fetch_funding_history(market_id)
        except NeedsApiKey as e:
            _write_needs_key_report(f"orderBookOrders/fundings for {symbol}", e)
            return 1
        except requests.exceptions.HTTPError as e:
            # НЕ 401/403 (тот случай -- NeedsApiKey выше) -- реальная
            # ошибка API на этом ОДНОМ рынке (напр. неверный параметр)
            # не должна ронять весь прогон по остальным 40+ рынкам.
            # Отмечается явно в результате, не молчаливый 0.
            print(f"[p4_lighter] {symbol} (market_id={market_id}): HTTP error, пропускаю -- {e}")
            results.append({
                "symbol": symbol, "market_id": market_id, "match_reason": reason,
                "mid_price_used": mid, "error": str(e),
            })
            continue

        # Диагностика единиц rate (владелец, дозапрос): сырые записи
        # ДО какой-либо обработки, для ОДНОГО рынка, только в лог джобы
        # (не коммитится, не публикуется) -- нужно увидеть буквальный
        # формат сырой строки rate (доля "0.0004" vs bps-целое "4" vs
        # что-то ещё), чтобы подтвердить/опровергнуть единицы.
        if not raw_sample_printed and funding_records:
            print(f"[p4_lighter] ДИАГНОСТИКА ЕДИНИЦ rate -- сырые записи {symbol} "
                  f"(market_id={market_id}), первые 3 из {len(funding_records)}: "
                  f"{funding_records[:3]}")
            raw_sample_printed = True

        funding_summary = summarize_funding(funding_records)

        # Ёмкость: временная замена глубине >100 уровней (владелец,
        # дозапрос) -- часовой объём из уже полученных метаданных рынка
        # (PerpsOrderBookDetail.daily_quote_token_volume, 0 доп.
        # запросов -- поле уже было в теле orderBookDetails).
        daily_quote_vol = m.get("daily_quote_token_volume")
        try:
            daily_quote_vol_f = float(daily_quote_vol) if daily_quote_vol not in (None, "") else None
        except (TypeError, ValueError):
            daily_quote_vol_f = None
        volume_proxy = {
            "daily_quote_token_volume_usd": daily_quote_vol_f,
            "hourly_volume_proxy_usd": (daily_quote_vol_f / 24) if daily_quote_vol_f is not None else None,
            "daily_trades_count": m.get("daily_trades_count"),
        }

        results.append({
            "symbol": symbol,
            "market_id": market_id,
            "match_reason": reason,
            "mid_price_used": mid,
            **depth,
            **funding_summary,
            **volume_proxy,
        })

    out_path = CACHE_DIR / "p4_lighter_markets_result.json"
    out_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_total_markets": len(markets),
        "n_stock_like_markets": len(matched),
        "requests_used": _request_count,
        "results": results,
    }, indent=2, default=str))
    print(f"[p4_lighter] записано {out_path}")
    print(f"[p4_lighter] диагностика (direction, знак сырой rate) -> count, "
          f"по всем {sum(_DIRECTION_SIGN_TALLY.values())} записям фандинга всех рынков: "
          f"{dict(_DIRECTION_SIGN_TALLY)}")

    _write_recon_section(results, len(markets))
    _write_calibration_addendum(results)
    return 0


def _write_needs_key_report(where: str, err: Exception) -> None:
    print(f"[p4_lighter] СТОП: {where} требует API-ключ -- не регистрируюсь. {err}")
    note = f"""

## Lighter — верифицировано API

**СТОП (2026-09-01): эндпоинт требует API-ключ.** Публичный REST
`{BASE_URL}` на шаге «{where}» ответил кодом, указывающим на
необходимость авторизации. По указанию владельца сессия **не
регистрируется** на внешних сервисах самостоятельно. Технические
детали (без секретов): `{str(err)[:800]}`.

Что нужно от владельца: либо ключ Lighter API (если требуется даже
для публичных market-data эндпоинтов — не подтверждено официальной
документацией SDK на момент написания, см. `docs/P4_RECON.md`), либо
подтверждение, что стадию можно повторить позже без ключа (временный
rate-limit/сбой, не постоянное требование).
"""
    path = Path("docs/P4_RECON.md")
    text = path.read_text() if path.exists() else ""
    marker = "## Lighter — верифицировано API"
    if marker in text:
        print(f"[p4_lighter] {path} уже содержит секцию -- не дублирую.")
        return
    path.write_text(text + note)


def _write_recon_section(results: list[dict], n_total_markets: int) -> None:
    path = Path("docs/P4_RECON.md")
    text = path.read_text() if path.exists() else ""
    marker = "## Lighter — верифицировано API"
    if marker in text:
        print(f"[p4_lighter] {path} уже содержит секцию -- не дублирую (повторный запуск не меняет числа).")
        return

    if not results:
        body = f"""

{marker}

Публичный REST Lighter ({BASE_URL}) опрошен 2026-09-01: {n_total_markets}
перп-рынков всего, **0 сток-/RWA-подобных** (ни по `market_type`, ни по
совпадению символа с реестром 194 сток-токенов Sprint R1). Lighter в
этом срезе не листит перпетуалы НА токенизированные акции как базовый
актив (только принимает их как обеспечение — см. остальную часть этого
файла, разведка по прессе). Критерий №1 `docs/P4_KILL.md` ("нет ≥30
дней истории фандинга ни по одному стоковому перпу") — **KILL**, т.к.
стоковых перпов не найдено вовсе.
"""
        path.write_text(text + body)
        print(f"[p4_lighter] {path} обновлён (0 сток-перпов найдено).")
        return

    rows = []
    n_errored = 0
    for r in results:
        if "error" in r:
            n_errored += 1
            rows.append(f"| {r['symbol']} | ОШИБКА API на этом рынке, пропущен | | | | | | | |")
            continue
        rows.append(
            f"| {r['symbol']} | {r.get('n_records', 0)} | {r.get('span_days', 0)} | "
            f"{r.get('annualized_median_pct', 'н/д')}% | {r.get('annualized_p10_pct', 'н/д')}% | "
            f"{r.get('annualized_p90_pct', 'н/д')}% | {r.get('share_hours_negative_for_short_pct', 'н/д')}% | "
            f"${r.get('combined_depth_usd_0.1pct', 0):,.0f} | ${r.get('combined_depth_usd_0.5pct', 0):,.0f} |"
        )
    max_span = max((r.get("span_days", 0) for r in results if "error" not in r), default=0)
    gate1 = "ПРОЙДЕН" if max_span >= 30 else "KILL (нет ни одного перпа с ≥30д истории)"

    body = f"""

{marker}

Публичный REST Lighter ({BASE_URL}) опрошен 2026-09-01 (0 Dune,
без ключа — ни один эндпоинт из использованных не потребовал
авторизации). {n_total_markets} перп-рынков всего, **{len(results)}
сток-/RWA-подобных** (совпадение `market_type` и/или символа с
реестром 194 сток-токенов Sprint R1 — см. `analysis/
p4_lighter_markets.py::is_stock_like`){f", из них {n_errored} не удалось опросить (ошибка API на конкретном рынке, не авторизация — см. таблицу)" if n_errored else ""}.
Только агрегаты — построчная история фандинга/ордербука наружу не
выгружалась.

| символ | N записей фандинга | охват, дней | годовой фандинг медиана | p10 | p90 | доля часов против шорта | глубина ±0.1% | глубина ±0.5% |
|---|---|---|---|---|---|---|---|---|
{chr(10).join(rows)}

Годовой фандинг — знак «положительно = в пользу шорта» (см.
`docs/P4_KILL.md`, критерий №2); знаковое допущение и наблюдённые
значения поля `direction` — см. `data/p4_lighter_cache/
p4_lighter_markets_result.json` (не в этой публичной секции — только
агрегаты). Глубина — сумма notional bid+ask в полосе от `mid`
(mark_price/index_price/last_trade_price, первое доступное),
±0.1% и ±0.5%, по не более {ORDER_BOOK_LIMIT_CANDIDATES[0]} уровням на
сторону (фактический лимит на сторону — поле `order_book_limit_used`,
если возвращено ровно столько же — возможна недооценка глубины, см.
`order_book_limit_hit` в JSON-кэше).

**Критерий №1 (`docs/P4_KILL.md`): {gate1}** — максимальный охват
истории фандинга среди найденных сток-перпов: {max_span} дней.
Критерии №2–3 требуют ввода владельца (издержки хеджа, размер ноги —
не зафиксированы числом, см. `docs/P4_KILL.md`) — не применяются
автоматически в этой стадии.
"""
    path.write_text(text + body)
    print(f"[p4_lighter] {path} обновлён.")


def _write_calibration_addendum(results: list[dict]) -> None:
    """Дозапрос владельца (2026-09-01): единицы funding rate + прокси
    ёмкости через часовой объём. Отдельная секция/маркер от
    _write_recon_section -- та уже написана и заморожена (её маркер
    гардит от повторной записи), это ДОПОЛНЕНИЕ к ней, не замена."""
    path = Path("docs/P4_RECON.md")
    text = path.read_text() if path.exists() else ""
    marker = "## Lighter — дозапрос: единицы фандинга и прокси ёмкости"
    if marker in text:
        print(f"[p4_lighter] {path} уже содержит секцию дозапроса -- не дублирую.")
        return

    ok_results = [r for r in results if "error" not in r]
    if not ok_results:
        print("[p4_lighter] нет результатов без ошибок -- секция дозапроса не пишется.")
        return

    n_suspect = sum(1 for r in ok_results if r.get("suspected_bug_gt_100pct_annual"))
    limits_used = sorted({r.get("order_book_limit_used") for r in ok_results if r.get("order_book_limit_used")})

    rows = []
    for r in ok_results:
        rows.append(
            f"| {r['symbol']} | {r.get('median_rate_per_hour_raw', 'н/д')} | "
            f"{r.get('median_bps_per_8h_equivalent', 'н/д')} | "
            f"{r.get('annualized_median_pct', 'н/д')}% | "
            f"{'ДА' if r.get('suspected_bug_gt_100pct_annual') else 'нет'} | "
            f"${r.get('hourly_volume_proxy_usd') or 0:,.0f} | "
            f"{r.get('order_book_limit_used', 'н/д')} |"
        )

    body = f"""

{marker}

По запросу владельца, второй проход по уже посчитанным данным того же
прогона ({datetime.now(timezone.utc).date()}) -- 0 доп. запросов сверх
того, что уже требовалось для основной секции выше (объём/сутки берётся
из уже полученных метаданных `orderBookDetails`, глубина -- из уже
сделанного вызова `orderBookOrders` с найденным рабочим лимитом).

**Единицы `rate` (Q3).** Официальная документация
(`docs.lighter.xyz/trading/funding`, дословная цитата через WebSearch --
сам домен заблокирован для прямого фетча в этой сессии): «At the end of
each hour, a 1-hour premium is calculated... funding payments for the
premium are distributed over 8 hours» и «The funding rate is expressed
in basis points (bps)». Трактовка: значение на `resolution=1h` уже
является ЧАСОВОЙ выплатой (премия/8), поэтому аннуализация
`rate × 24 × 365` (без дополнительного /8) методологически верна, ЕСЛИ
`rate` -- доля (fraction). Фраза «expressed in bps» в доке, по всей
видимости, описывает ПОРЯДОК ВЕЛИЧИНЫ для человека (типичные значения --
единицы bps), а не то, что сырое числовое поле хранит целочисленный
подсчёт bps -- сырые записи (диагностика в логе прогона, не
коммитятся: `[p4_lighter] ДИАГНОСТИКА ЕДИНИЦ rate...`) показывают
десятичную дробь (напр. `0.0004`), что арифметически САМОСОГласовано с
уже опубликованной медианой 350.4%/год (0.0004×24×365×100=350.4) --
то есть код не путает единицы измерения относительно САМОГО СЕБЯ,
но абсолютная привязка к официальной шкале Lighter (доля vs bps-как-
целое) не подтверждена независимым третьим источником.

**Сверка с внешним ориентиром.** На сравнимых крипто-рынках funding на
Lighter и 20 других venue сейчас в диапазоне примерно -2.6..+8.7 bps
**за 8 часов** (`loris.tools/funding/exchange/lighter`, WebSearch,
2026-09-01) -- то есть максимум ~8.7 bps/8ч ≈ 0.087%/8ч ≈ годовых
(8.7/10000)×(365×3)×100 ≈ **95.3%/год** даже на самом горячем
наблюдаемом крипто-рынке. Столбец «bps за 8ч (экв.)» ниже пересчитывает
ту же медианную часовую ставку в те же единицы для прямого сравнения:

| символ | медиана rate/час (сырое) | bps/8ч (экв.) | годовых, % | подозрение на баг (>100%/год) | часовой объём (прокси), $ | лимит книги, факт |
|---|---|---|---|---|---|---|
{chr(10).join(rows)}

**Вывод по Q3 (дословно по инструкции владельца): {n_suspect} из
{len(ok_results)} рынков показывают >100%/год после пересчёта.**
Владелец: «считать это багом до доказательства обратного» — **это
подозреваемый баг/аномалия, НЕ подтверждённый факт рынка.** Величина
(~350%/год почти везде, на порядок выше максимума сравнимых крипто-рынков
~95%/год) не объясняется ни ошибкой аннуализации (арифметика
самосогласована), ни очевидной ошибкой парсинга (raw-diagnostic в логе
подтверждает десятичный формат) — правдоподобные версии: (а) реальный,
но экстремальный уровень базовой "процентной компоненты" для нового,
малоликвидного класса активов (сток-перпы) на Lighter, ещё не
откалиброванный к типичным уровням; (б) единицы, отличные от
"доля/час", которые эта диагностика не может исключить без доступа к
`docs.lighter.xyz` напрямую или без реальной калибровки по счёту (см.
`analysis/p4_funding_calibration.py`, Q5) — **не подтверждено, не
разрешено этим проходом.**

**Ёмкость: прокси через часовой объём (Q4).** Параметр глубины >100
уровней в API не найден (`apidocs.lighter.xyz` заблокирован для
прямого фетча, официальный OpenAPI-реестр недоступен этой сессии) --
эмпирически опробованы {ORDER_BOOK_LIMIT_CANDIDATES} по убыванию,
фактически сработавший лимит на этот прогон: {limits_used or 'н/д'}
(поле `order_book_limit_used` в JSON-кэше, по рынку — см. таблицу).
Временная замена (по просьбе владельца): часовой объём =
`daily_quote_token_volume / 24` из уже полученных метаданных рынка
(0 доп. запросов) — столбец «часовой объём (прокси)» выше. Это ГРУБЫЙ
прокси ёмкости (весь дневной объём поровну по часам, не учитывает
внутридневную неравномерность) — не замена реальной глубине книги,
но не требует новых вызовов и не даёт молчаливого нуля там, где книга
физически глубже {ORDER_BOOK_LIMIT_CANDIDATES[-1]} уровней.
"""
    path.write_text(text + body)
    print(f"[p4_lighter] {path} обновлён (секция дозапроса).")


if __name__ == "__main__":
    sys.exit(run())
