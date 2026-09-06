#!/usr/bin/env python3
"""«Токенизированные акции на других сетях — тонкие пулы», окна/метрики
(владелец, 2026-09-06, п.2, продолжение после `rwa_tokenized_stocks_
thin_pool_discovery.py`). Реальный, address-verified universe после
разведки (см. docs/PROJECT_STATE.md) — ЕДИНСТВЕННЫЕ 4 тикера с реальной
живой ликвидностью в полосе $50k-$500k: xStocks на Solana — AAPLx,
TSLAx, NVDAx, COINx. Backed b-токены мертвы, Dinari TSLA.d — 0 пулов,
Ondo AAPLon — пулы тоньше пола. Ветка "поиск по строке тикера" честно
отброшена как загрязнённая (см. паспорт) — не используется.

Метод (класс «отдельные акции», та же спецификация, что уже применена
в `sleeping_refs_stocks_metrics.py` для Lighter/xyz): X = пт 20:00 ->
вс 19:55 ET, Z1 = вс 20:00 -> 21:00 ET, Z2 = 21:00 ET -> пн 9:30 ET,
Y = yfinance закрытие пт -> открытие пн. Окна/as-of-цена/корреляции/
sign-flip-перестановочный тест — те же функции из
`sleeping_refs_metrics_lib.py`/`compute_metrics` из
`sleeping_refs_stocks_metrics.py` (переиспользуется напрямую, не
копируется).

Источники данных (оба реальные, публичные, бесплатные, без ключа):
  1. Цена пула — GeckoTerminal OHLCV (`/networks/solana/pools/{addr}/
     ohlcv/hour`, currency=usd), тот же метод пагинации, что уже
     подтверждён в `p5_gt_pool_history.py`.
  2. Round-trip $500/$5000 — Jupiter Aggregator Quote API
     (`quote-api.jup.ag/v6/quote`, реальный публичный роутер по всем
     Solana DEX, включая наш пул) — купить на $500/$5000 USDC,
     затем продать ПОЛУЧЕННОЕ количество токена обратно в USDC (без
     предположений о decimals токена — берём raw `outAmount` первой
     котировки напрямую как `amount` второй), разница — реальная
     стоимость round-trip (проскальзывание + комиссии реального
     маршрута, не оценка по TVL). USDC-mint на Solana — канонический,
     публично задокументированный Circle-адрес
     `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` (не изобретаем —
     это тот же адрес, что фигурирует во ВСЕХ найденных пулах как
     "USDC"-нога, подтверждаем эмпирически успешностью самих котировок).

ЧЕСТНАЯ ОГОВОРКА про N: с 2026-07-01 по сегодня — около 9-10 реальных
завершённых выходных на тикер, т.е. per-тикер N < 30 предрегистрационного
порога владельца почти наверняка НЕ будет достигнут — предрегистрация
проверяется на ПУЛЕ из всех 4 тикеров (cluster permutation test,
кластер = тикер), тем же методом, что уже используется в
`sleeping_refs_stocks_metrics.py` для 23-25 тикеров Lighter/xyz."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from sleeping_refs_metrics_lib import real_fridays_since, yfinance_daily, friday_monday_gap  # noqa: E402
from sleeping_refs_stocks_metrics import compute_metrics  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/rwa_xstocks_windows_metrics_result.json")
CACHE_DIR = Path("data/sleeping_refs_cache")
GT_BASE = "https://api.geckoterminal.com/api/v2"
JUP_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"  # РЕАЛЬНЫЙ фикс после первого прогона:
# quote-api.jup.ag/v6 -- NameResolutionError (DNS не резолвит вообще, не просто 4xx/5xx).
# WebSearch этой сессии подтвердил: v6/quote-api.jup.ag -- deprecated legacy-эндпоинт,
# текущий бесплатный (без API-ключа) путь -- lite-api.jup.ag/swap/v1/quote
# (https://dev.jup.ag/api-reference/swap/quote, тот же набор параметров).
NETWORK = "solana"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # канонический USDC-mint на Solana -- см. докстринг
USDC_DECIMALS = 6  # стандарт USDC везде, не предположение конкретно для этого проекта

MIN_REQUEST_INTERVAL_S = 2.6
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "robinhood-chain-alpha-rwa-xstocks/1.0"}

# Реальные адреса (эта сессия, WebSearch + rwa_tokenized_stocks_thin_pool_discovery_result.json) --
# по каждому тикеру выбран САМЫЙ ЛИКВИДНЫЙ /USDC-пул ИЗ ТЕХ, ЧТО РЕАЛЬНО ПОПАЛИ В ПОЛОСУ $50k-$500k
# (не любой пул с самым большим объёмом вообще -- именно тонкий, это и есть предмет проверки).
TICKERS = {
    "AAPLx": {"pool": "EHdow7Yhmr1ac8Qff9Co1LhSosr38puA6zLd4cbJLdpV", "mint": "XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp", "underlying": "AAPL"},
    "TSLAx": {"pool": "9p7abUFv31ycgu9kckvnoqMMvBy67dqTDM2m6HP9xokN", "mint": "XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB", "underlying": "TSLA"},
    "NVDAx": {"pool": "6R4r93V5fcMzc13CL2enEepDSYcr4Qx3ptZBDwudTXCo", "mint": "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh", "underlying": "NVDA"},
    "COINx": {"pool": "5pobXosbeGSCRG3HdaRiw2TaihDDQMXkHFVbUTF24weM", "mint": "Xs7ZdzSHLU9ftNJsii5fCeJhoRWSC32SQGzGQtePxNu", "underlying": "COIN"},
}

_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _last_call + MIN_REQUEST_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _get(url: str, params: dict | None = None) -> tuple[int, dict | str]:
    last_exc: Exception | None = None
    for attempt in range(3):
        _throttle()
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                wait = 10 * (attempt + 1)
                print(f"    сетевая ошибка ({exc.__class__.__name__}), жду {wait}с и повторяю")
                time.sleep(wait)
                continue
            return 0, f"сетевая ошибка после 3 попыток: {exc}"
        try:
            body = r.json()
        except ValueError:
            body = r.text[:500]
        if r.status_code == 429 and attempt < 2:
            print(f"    429, жду 65с и повторяю")
            time.sleep(65)
            continue
        return r.status_code, body
    return 0, f"сетевая ошибка после 3 попыток: {last_exc}"


def fetch_ohlcv_hourly(pool_address: str, min_start_utc: pd.Timestamp, max_pages: int = 4) -> list[list]:
    """Часовые OHLCV в USD (currency=usd -- прямая цена в долларах, не в
    токене quote -- все наши пулы уже выбраны как /USDC, поэтому это
    эквивалентно, но currency=usd честнее -- не зависит от того, что
    USDC==$1 на GT-стороне). Пагинация -- тот же метод, что
    `p5_gt_pool_history.py` (before_timestamp, подтверждённый рабочим)."""
    all_rows: dict[int, list] = {}
    before_ts: int | None = None
    for page in range(max_pages):
        params = {"aggregate": 1, "limit": 1000, "currency": "usd", "include_empty_intervals": "true"}
        if before_ts is not None:
            params["before_timestamp"] = before_ts
        status, body = _get(f"{GT_BASE}/networks/{NETWORK}/pools/{pool_address}/ohlcv/hour", params=params)
        if status != 200 or not isinstance(body, dict):
            print(f"    ohlcv страница {page}: HTTP {status} -- {str(body)[:300]}")
            break
        rows = body.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        print(f"    ohlcv страница {page}: {len(rows)} свечей (before_timestamp={before_ts})")
        if not rows:
            break
        for row in rows:
            all_rows[int(row[0])] = row
        oldest_ts = min(int(row[0]) for row in rows)
        if before_ts is not None and oldest_ts >= before_ts:
            break
        before_ts = oldest_ts
        if len(rows) < 1000:
            break
        if oldest_ts <= min_start_utc.timestamp():
            break
    return sorted(all_rows.values(), key=lambda r: r[0])


def jupiter_quote(input_mint: str, output_mint: str, amount_raw: int) -> int | None:
    status, body = _get(JUP_QUOTE_URL, params={
        "inputMint": input_mint, "outputMint": output_mint, "amount": amount_raw, "slippageBps": 300,
    })
    if status != 200 or not isinstance(body, dict) or "outAmount" not in body:
        print(f"    Jupiter quote {input_mint[:6]}->{output_mint[:6]} amount={amount_raw}: HTTP {status} -- {str(body)[:300]}")
        return None
    return int(body["outAmount"])


def round_trip_pct(mint: str, usd_notional: float) -> float | None:
    """Реальный round-trip (купить -> сразу продать обратно) через
    Jupiter -- best-route по ВСЕМ Solana DEX (не только наш конкретный
    тонкий пул), т.е. это реальная исполнимая стоимость для трейдера,
    не оценка по TVL. usd_notional -> raw USDC (6 decimals, см.
    докстринг)."""
    usdc_in = int(round(usd_notional * (10 ** USDC_DECIMALS)))
    token_out = jupiter_quote(USDC_MINT, mint, usdc_in)
    if token_out is None or token_out <= 0:
        return None
    usdc_back = jupiter_quote(mint, USDC_MINT, token_out)
    if usdc_back is None:
        return None
    return float((usdc_in - usdc_back) / usdc_in * 100.0)


def save_candles_csv(ticker: str, rows: list[list]) -> Path:
    path = CACHE_DIR / f"rwa_xstocks_1h_{ticker}.csv"
    df = pd.DataFrame(rows, columns=["ts_s", "o", "h", "l", "c", "v"])
    df["t"] = (df["ts_s"].astype("int64") * 1000)
    df = df[["t", "o", "h", "l", "c", "v"]]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return path


def run() -> int:
    now_utc = datetime.now(timezone.utc)
    all_fridays = real_fridays_since("2026-07-01", now_utc)
    print(f"[rwa_xstocks] реальных завершённых выходных с 2026-07-01: {len(all_fridays)} -- {all_fridays}")
    min_start_utc = pd.Timestamp("2026-07-01T00:00:00Z")

    universe_meta: dict[str, dict] = {}
    candles_by_ticker: dict[str, pd.DataFrame] = {}
    for ticker, meta in TICKERS.items():
        print(f"\n=== {ticker} (pool {meta['pool']}) ===")
        rows = fetch_ohlcv_hourly(meta["pool"], min_start_utc)
        print(f"  реальных часовых свечей: {len(rows)}")
        if not rows:
            universe_meta[ticker] = {"error": "нет реальных свечей вообще"}
            continue
        csv_path = save_candles_csv(ticker, rows)
        from sleeping_refs_metrics_lib import load_hourly_csv
        candles_by_ticker[ticker] = load_hourly_csv(csv_path)

        rt500 = round_trip_pct(meta["mint"], 500.0)
        rt5000 = round_trip_pct(meta["mint"], 5000.0)
        print(f"  round-trip $500: {rt500}%, $5000: {rt5000}% (реальные котировки Jupiter)")
        universe_meta[ticker] = {"csv": str(csv_path), "round_trip_pct_500": rt500, "round_trip_pct_5000": rt5000,
                                  "underlying": meta["underlying"], "n_candles": len(rows)}

    from sleeping_refs_metrics_lib import price_asof, stock_class_windows

    y_cache: dict[str, pd.DataFrame | None] = {}
    rows_out: list[dict] = []
    drop_reasons: dict[str, int] = {}

    def bump(reason: str) -> None:
        drop_reasons[reason] = drop_reasons.get(reason, 0) + 1

    y_start = all_fridays[0] if all_fridays else "2026-07-01"
    y_end = (datetime.strptime(all_fridays[-1], "%Y-%m-%d") + pd.Timedelta(days=4)).strftime("%Y-%m-%d") if all_fridays else "2026-07-01"

    for ticker, meta in TICKERS.items():
        if ticker not in candles_by_ticker:
            continue
        candles = candles_by_ticker[ticker]
        underlying = meta["underlying"]
        if underlying not in y_cache:
            y_cache[underlying] = yfinance_daily(underlying, y_start, y_end)
            time.sleep(0.3)
        daily = y_cache[underlying]

        for friday in all_fridays:
            w = stock_class_windows(friday)
            x_start_p = price_asof(candles, w["x_start"])
            x_end_p = price_asof(candles, w["x_end"])
            z1_start_p = price_asof(candles, w["z1_start"])
            z1_end_p = price_asof(candles, w["z1_end"])
            z2_start_p = price_asof(candles, w["z2_start"])
            z2_end_p = price_asof(candles, w["z2_end"])
            if None in (x_start_p, x_end_p, z1_start_p, z1_end_p, z2_start_p, z2_end_p):
                bump(f"{ticker}: нет реальных свечей в пределах допуска для {friday}")
                continue
            if daily is None:
                bump(f"{ticker}: yfinance не вернул дневные данные для {underlying}")
                continue
            y = friday_monday_gap(daily, friday)
            if y.get("gap") is None:
                bump(f"{ticker}: Y недоступен для {friday} -- {y.get('reason')}")
                continue
            rows_out.append({
                "venue": "xstocks_solana", "symbol": ticker, "friday": friday,
                "X": x_end_p / x_start_p - 1, "Z1": z1_end_p / z1_start_p - 1,
                "Z2": z2_end_p / z2_start_p - 1, "Y": y["gap"],
                "round_trip_pct_500": universe_meta[ticker].get("round_trip_pct_500"),
                "round_trip_pct_5000": universe_meta[ticker].get("round_trip_pct_5000"),
            })

    df = pd.DataFrame(rows_out)
    print(f"\n[rwa_xstocks] реальных строк (тикер x выходные) с полными X/Z1/Z2/Y: {len(df)}")
    metrics = compute_metrics(df, "xstocks_solana")
    for k in ("corr_X_Y", "corr_X_Z1", "corr_X_Z2", "sign_opposite_fraction_X_Y",
              "sign_opposite_fraction_X_Z1", "sign_opposite_fraction_X_Z2",
              "mean_frac_abs_z1_gt_roundtrip_500", "mean_frac_abs_z1_gt_roundtrip_5000", "preregistered_verdict"):
        if k in metrics:
            print(f"  {k}: {metrics[k]}")

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "weekends_considered": all_fridays,
        "universe_meta": universe_meta,
        "drop_reasons": drop_reasons,
        "metrics": metrics,
        "rows": df.to_dict("records"),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[rwa_xstocks] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
