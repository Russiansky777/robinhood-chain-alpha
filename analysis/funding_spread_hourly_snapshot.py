#!/usr/bin/env python3
"""Владелец, 2026-09-04: часовой сборщик спреда фандинга Lighter <->
Hyperliquid по паре ETH/USDG-хеджа (тот же хост Lighter, что P5,
api.rh.lighter.xyz). Пары уже отобраны и ЗАФИКСИРОВАНЫ
(data/funding_pairs.json, analysis/funding_pairs_select.py) -- этот
скрипт их не меняет, только читает.

Источники (оба -- реальные прямые факты, не производные):
  - Lighter: `/api/v1/fundings` (market_id, resolution=1h,
    start_timestamp, end_timestamp, count_back) -- ПОДТВЕРЖДЕНО реальным
    прогоном 2026-09-04 (analysis/lighter_funding_endpoint_probe.py,
    data/p3_guard_cache/lighter_funding_endpoint_probe_result.json):
    HTTP 200, реальные записи {timestamp, value, rate, direction}.
    ВАЖНО: этот эндпоинт требует ВСЕ пять параметров -- без
    start_timestamp/end_timestamp отдаёт 400 "invalid param" (первая
    попытка разведки упала именно на этом). Последняя запись = текущая
    часовая ставка. `rate` -- уже ПРОЦЕНТ за час, не доля (3 независимых
    доказательства, docs/P4_RECON.md, "дозапрос: единицы фандинга") --
    здесь используется как есть, без домножения/деления.
  - Hyperliquid: POST /info {"type":"metaAndAssetCtxs"} -> `funding`
    (уже часовая ставка по документации/спецификации владельца, "уже
    hourly") и `dayNtlVlm`/`markPx`; POST /info {"type":"l2Book"} для
    глубины книги.

НЕ используется: любая производная формула funding rate из mark/index
price (premium/8 + interestRateComponent) -- эта формула в
docs/P4_RECON.md сама помечена как НЕ подтверждённая независимым
источником (единицы/абсолютная привязка к шкале Lighter под вопросом).
Раз прямой эндпоинт fundings реально работает на этом хосте, производная
формула не нужна вообще.

Резилентность (владелец): падение ОДНОЙ биржи -> null + error в
соответствующих полях этой пары, цикл ПРОДОЛЖАЕТСЯ (не падает целиком);
retry с экспоненциальным backoff на транспортном уровне (не на падении
самих данных -- 4xx не ретраится, это не транзиентная ошибка).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

PAIRS_PATH = Path("data/funding_pairs.json")
OUT_PATH = Path("data/funding_spread.jsonl")

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
HYPERLIQUID_API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-funding-logger/1.0"}

DEPTH_BAND_PCT = 0.5  # +-0.5% от mid price, владелец
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE_S = 2.0


def _retry_request(method: str, url: str, **kwargs) -> requests.Response:
    """Ретрай с экспоненциальным backoff -- ТОЛЬКО на транспортные/5xx
    сбои (сеть недоступна, таймаут, 5xx) -- 4xx (например, реальная
    ошибка параметров) не ретраится, это не транзиентная проблема."""
    last_exc: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.request(method, url, timeout=20, **kwargs)
            if resp.status_code >= 500:
                raise requests.exceptions.HTTPError(f"HTTP {resp.status_code}", response=resp)
            return resp
        except (requests.exceptions.RequestException,) as exc:
            last_exc = exc
            if attempt < RETRY_ATTEMPTS - 1:
                sleep_s = RETRY_BACKOFF_BASE_S * (2 ** attempt)
                print(f"    [funding_spread] попытка {attempt + 1}/{RETRY_ATTEMPTS} для {url} упала ({exc}), сон {sleep_s:.0f}с")
                time.sleep(sleep_s)
    raise last_exc  # type: ignore[misc]


def load_pairs() -> list[dict]:
    if not PAIRS_PATH.exists():
        raise FileNotFoundError(f"{PAIRS_PATH} не существует -- сначала отбор пар (funding_pairs_select.py)")
    return json.loads(PAIRS_PATH.read_text())["pairs"]


# ---------- Lighter ----------

def fetch_lighter_order_book_details() -> dict:
    """Один вызов на ВСЕ рынки -- переиспользуется для каждой пары,
    не бьёт API по разу на пару (владелец: не бить лимит вслепую)."""
    resp = _retry_request("GET", f"{LIGHTER_API_BASE}/api/v1/orderBookDetails",
                           params={"filter": "all"}, headers=HEADERS)
    resp.raise_for_status()
    body = resp.json()
    return {m["market_id"]: m for m in body.get("order_book_details", [])}


def fetch_lighter_latest_funding(market_id: int) -> dict:
    """Требует ВСЕ пять параметров (см. докстринг модуля) -- без
    start_timestamp/end_timestamp реальный API отдаёт 400."""
    now = int(time.time())
    resp = _retry_request("GET", f"{LIGHTER_API_BASE}/api/v1/fundings", headers=HEADERS, params={
        "market_id": market_id, "resolution": "1h",
        "start_timestamp": now - 3 * 3600, "end_timestamp": now, "count_back": 3,
    })
    resp.raise_for_status()
    body = resp.json()
    records = body.get("fundings", [])
    if not records:
        raise ValueError("пустой список fundings")
    return max(records, key=lambda r: r["timestamp"])


def fetch_lighter_depth_pct(market_id: int, mid_price: float) -> dict:
    resp = _retry_request("GET", f"{LIGHTER_API_BASE}/api/v1/orderBookOrders", headers=HEADERS,
                           params={"market_id": market_id, "limit": 200})
    resp.raise_for_status()
    body = resp.json()
    lo = mid_price * (1 - DEPTH_BAND_PCT / 100)
    hi = mid_price * (1 + DEPTH_BAND_PCT / 100)
    bid_notional = sum(
        float(o["price"]) * float(o["remaining_base_amount"])
        for o in body.get("bids", []) if lo <= float(o["price"]) <= mid_price
    )
    ask_notional = sum(
        float(o["price"]) * float(o["remaining_base_amount"])
        for o in body.get("asks", []) if mid_price <= float(o["price"]) <= hi
    )
    return {"bid_notional_usd": bid_notional, "ask_notional_usd": ask_notional}


def get_lighter_side(pair: dict, ob_details: dict) -> dict:
    market_id = pair["lighter_market_id"]
    side: dict = {"market_id": market_id, "error": None}
    try:
        m = ob_details.get(market_id)
        if m is None:
            raise ValueError(f"market_id {market_id} не найден в orderBookDetails")
        mark_price = float(m["mark_price"])
        side["mark_price"] = mark_price
        side["daily_quote_token_volume_usd"] = float(m.get("daily_quote_token_volume")) if m.get("daily_quote_token_volume") is not None else None
    except Exception as exc:  # noqa: BLE001
        side["mark_price"] = None
        side["daily_quote_token_volume_usd"] = None
        side["error"] = f"orderBookDetails: {exc}"

    try:
        rec = fetch_lighter_latest_funding(market_id)
        side["funding_rate_hourly_pct_raw"] = rec.get("rate")
        side["funding_rate_hourly_pct"] = float(rec["rate"]) if rec.get("rate") is not None else None
        side["funding_direction_raw"] = rec.get("direction")
        side["funding_timestamp_unix"] = rec.get("timestamp")
        side["funding_value_raw"] = rec.get("value")
    except Exception as exc:  # noqa: BLE001
        side["funding_rate_hourly_pct_raw"] = None
        side["funding_rate_hourly_pct"] = None
        side["funding_direction_raw"] = None
        side["funding_timestamp_unix"] = None
        side["funding_value_raw"] = None
        side["error"] = (side.get("error") + "; " if side.get("error") else "") + f"fundings: {exc}"

    try:
        if side.get("mark_price"):
            depth = fetch_lighter_depth_pct(market_id, side["mark_price"])
            side.update(depth)
        else:
            side["bid_notional_usd"] = None
            side["ask_notional_usd"] = None
    except Exception as exc:  # noqa: BLE001
        side["bid_notional_usd"] = None
        side["ask_notional_usd"] = None
        side["error"] = (side.get("error") + "; " if side.get("error") else "") + f"orderBookOrders: {exc}"

    return side


# ---------- Hyperliquid ----------

def fetch_hyperliquid_meta_and_ctxs() -> dict:
    resp = _retry_request("POST", f"{HYPERLIQUID_API_BASE}/info", headers=HEADERS,
                           json={"type": "metaAndAssetCtxs"})
    resp.raise_for_status()
    meta, asset_ctxs = resp.json()
    universe = meta.get("universe", [])
    return {u["name"]: ctx for u, ctx in zip(universe, asset_ctxs)}


def fetch_hyperliquid_depth_pct(coin: str, mid_price: float) -> dict:
    resp = _retry_request("POST", f"{HYPERLIQUID_API_BASE}/info", headers=HEADERS,
                           json={"type": "l2Book", "coin": coin})
    resp.raise_for_status()
    body = resp.json()
    levels = body.get("levels", [[], []])
    bids, asks = levels[0], levels[1]
    lo = mid_price * (1 - DEPTH_BAND_PCT / 100)
    hi = mid_price * (1 + DEPTH_BAND_PCT / 100)
    bid_notional = sum(float(l["px"]) * float(l["sz"]) for l in bids if lo <= float(l["px"]) <= mid_price)
    ask_notional = sum(float(l["px"]) * float(l["sz"]) for l in asks if mid_price <= float(l["px"]) <= hi)
    return {"bid_notional_usd": bid_notional, "ask_notional_usd": ask_notional}


def get_hyperliquid_side(pair: dict, ctxs_by_symbol: dict) -> dict:
    coin = pair["hyperliquid_raw_symbol"]
    side: dict = {"coin": coin, "error": None}
    ctx = ctxs_by_symbol.get(coin)
    if ctx is None:
        side["error"] = f"символ {coin} не найден в metaAndAssetCtxs на момент этого прогона"
        for k in ("mark_price", "day_ntl_vlm_usd", "funding_rate_hourly_pct", "funding_rate_hourly_pct_raw"):
            side[k] = None
    else:
        try:
            side["mark_price"] = float(ctx["markPx"]) if ctx.get("markPx") is not None else None
        except Exception:
            side["mark_price"] = None
        try:
            side["day_ntl_vlm_usd"] = float(ctx["dayNtlVlm"]) if ctx.get("dayNtlVlm") is not None else None
        except Exception:
            side["day_ntl_vlm_usd"] = None
        side["funding_rate_hourly_pct_raw"] = ctx.get("funding")
        try:
            # ВНИМАНИЕ -- ПРЕДПОЛОЖЕНИЕ, НЕ ПОДТВЕРЖДЕНО в этой сессии:
            # funding у Hyperliquid трактуется здесь как ДОЛЯ (fraction)
            # за час, домножается на 100 для сопоставимости с Lighter
            # (там rate уже подтверждён как % -- 3 независимых
            # доказательства, docs/P4_RECON.md). docs.hyperliquid.xyz
            # заблокирован для прямого фетча из этой сессии (см.
            # PROJECT_STATE.md, "Hyperliquid -- юрисдикционные
            # ограничения") -- единица НЕ верифицирована первичным
            # источником здесь и сейчас, только общеизвестная конвенция.
            # `funding_rate_hourly_pct_raw` хранит значение БЕЗ пересчёта --
            # если предположение окажется неверным, raw остаётся
            # источником истины, а не это производное поле.
            side["funding_rate_hourly_pct"] = float(ctx["funding"]) * 100 if ctx.get("funding") is not None else None
            side["funding_unit_assumption"] = "предположение: fraction/час, НЕ верифицировано первичным источником в этой сессии"
        except Exception:
            side["funding_rate_hourly_pct"] = None

    try:
        if side.get("mark_price"):
            depth = fetch_hyperliquid_depth_pct(coin, side["mark_price"])
            side.update(depth)
        else:
            side["bid_notional_usd"] = None
            side["ask_notional_usd"] = None
    except Exception as exc:  # noqa: BLE001
        side["bid_notional_usd"] = None
        side["ask_notional_usd"] = None
        side["error"] = (side.get("error") + "; " if side.get("error") else "") + f"l2Book: {exc}"

    return side


def run() -> int:
    pairs = load_pairs()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    lighter_ob_error = None
    try:
        ob_details = fetch_lighter_order_book_details()
    except Exception as exc:  # noqa: BLE001
        ob_details = {}
        lighter_ob_error = str(exc)
        print(f"[funding_spread] Lighter orderBookDetails УПАЛ ЦЕЛИКОМ (не пропускаем цикл, все пары пойдут с null+error): {exc}")

    hl_error = None
    try:
        ctxs_by_symbol = fetch_hyperliquid_meta_and_ctxs()
    except Exception as exc:  # noqa: BLE001
        ctxs_by_symbol = {}
        hl_error = str(exc)
        print(f"[funding_spread] Hyperliquid metaAndAssetCtxs УПАЛ ЦЕЛИКОМ (не пропускаем цикл, все пары пойдут с null+error): {exc}")

    rows = []
    for pair in pairs:
        symbol = pair["symbol"]
        print(f"=== {symbol} ({pair['cohort']}) ===")
        lighter_side = get_lighter_side(pair, ob_details)
        if lighter_ob_error and lighter_side.get("mark_price") is None:
            lighter_side["error"] = (lighter_side.get("error") + "; " if lighter_side.get("error") else "") + f"orderBookDetails целиком недоступен: {lighter_ob_error}"
        hyperliquid_side = get_hyperliquid_side(pair, ctxs_by_symbol)
        if hl_error:
            hyperliquid_side["error"] = (hyperliquid_side.get("error") + "; " if hyperliquid_side.get("error") else "") + f"metaAndAssetCtxs целиком недоступен: {hl_error}"

        l_rate = lighter_side.get("funding_rate_hourly_pct")
        h_rate = hyperliquid_side.get("funding_rate_hourly_pct")
        spread_pct = (l_rate - h_rate) if (l_rate is not None and h_rate is not None) else None

        row = {
            "timestamp_utc": now_iso,
            "symbol": symbol,
            "cohort": pair["cohort"],
            "lighter": lighter_side,
            "hyperliquid": hyperliquid_side,
            "spread_hourly_pct_lighter_minus_hyperliquid": spread_pct,
            "spread_sign": (None if spread_pct is None else ("lighter_higher" if spread_pct > 0 else ("hyperliquid_higher" if spread_pct < 0 else "equal"))),
            "spread_computation_caveat": (
                "Lighter.funding_rate_hourly_pct подтверждён как % (docs/P4_RECON.md); "
                "Hyperliquid.funding_rate_hourly_pct использует НЕподтверждённое предположение "
                "о единицах (fraction->%, см. hyperliquid.funding_unit_assumption) -- "
                "spread_hourly_pct числовой, но его абсолютная величина зависит от этого "
                "предположения; raw-поля обеих сторон -- источник истины, не это поле."
            ),
        }
        rows.append(row)
        print(f"  Lighter: rate={l_rate}, mark={lighter_side.get('mark_price')}, error={lighter_side.get('error')}")
        print(f"  Hyperliquid: rate={h_rate}, mark={hyperliquid_side.get('mark_price')}, error={hyperliquid_side.get('error')}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("a") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print(f"\n[funding_spread] дописано {len(rows)} строк в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
