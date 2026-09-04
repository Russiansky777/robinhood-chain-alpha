#!/usr/bin/env python3
"""P6, шаг 1 (владелец, 2026-09-04, ДО входа): базис cbBTC/BTC за 90
дней. Источники:
  - cbBTC цена: GT OHLCV на РЕАЛЬНОМ пуле USDC-CBBTC
    (0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb, Base) -- currency=token,
    token=quote даёт цену cbBTC в USDC (~USD, USDC квази-стабилен),
    тот же метод/параметры, что уже проверены в analysis/pool_screener_sigma_lvr.py.
  - BTC цена: CoinGecko `/coins/bitcoin/market_chart` -- реальный,
    независимый от Base/GT источник (уже используется в проекте, см.
    docs/P4_RECON.md, "цена LIT/USD, CoinGecko coins/lighter/history").

Максимальное отклонение cbBTC от BTC за 90 дней + поведение в дни
просадок BTC (просадка -- реальная, из самого ряда, не предполагается
заранее какой день). Если когда-либо >1% -- явно помечается REPORT_ONLY,
это меняет конструкцию хеджа (см. docs/P6_HEDGED_LP.md)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

GT_BASE = "https://api.geckoterminal.com/api/v2"
MIN_REQUEST_INTERVAL_S = 2.6
RATE_LIMIT_BACKOFF_S = 65.0
RATE_LIMIT_MAX_RETRIES = 2
HEADERS_GT = {"Accept": "application/json;version=20230302", "User-Agent": "robinhood-chain-alpha-p6/1.0"}

POOL_NETWORK = "base"
POOL_ADDRESS = "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb"  # USDC-CBBTC, aerodrome-slipstream, реальный (RPC-подтверждённые decimals 6/8)
DAYS = 90
TARGET_CANDLES = DAYS * 24
MAX_PAGES = 4  # 4x1000 = 4000 часов ~ 166 дней, с запасом над нужными 90*24=2160

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

_last_gt_call = 0.0


def _throttle_gt() -> None:
    global _last_gt_call
    wait = _last_gt_call + MIN_REQUEST_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_gt_call = time.monotonic()


def _get_gt(url: str, params: dict) -> tuple[int, dict]:
    status, body = None, None
    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        _throttle_gt()
        r = requests.get(url, params=params, headers=HEADERS_GT, timeout=30)
        status, body = r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)
        if status == 429 and attempt < RATE_LIMIT_MAX_RETRIES:
            print(f"    GT 429, жду {RATE_LIMIT_BACKOFF_S:.0f}с")
            time.sleep(RATE_LIMIT_BACKOFF_S)
            continue
        return status, body
    return status, body


def fetch_cbbtc_ohlcv() -> list[tuple[int, float]]:
    all_rows: dict[int, float] = {}
    before_ts: int | None = None
    for page in range(MAX_PAGES):
        params = {"aggregate": 1, "limit": 1000, "currency": "token", "token": "quote", "include_empty_intervals": "true"}
        if before_ts is not None:
            params["before_timestamp"] = before_ts
        status, body = _get_gt(f"{GT_BASE}/networks/{POOL_NETWORK}/pools/{POOL_ADDRESS}/ohlcv/hour", params)
        if status != 200:
            print(f"    ohlcv страница {page}: HTTP {status} -- {str(body)[:300]}")
            break
        rows = body.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        print(f"    ohlcv страница {page}: {len(rows)} свечей (before_timestamp={before_ts})")
        if not rows:
            break
        for row in rows:
            all_rows[int(row[0])] = float(row[4])  # close
        oldest_ts = min(int(row[0]) for row in rows)
        if before_ts is not None and oldest_ts >= before_ts:
            break
        before_ts = oldest_ts
        if len(rows) < 1000 or len(all_rows) >= TARGET_CANDLES:
            break
    return sorted(all_rows.items())


def fetch_btc_market_chart() -> list[tuple[int, float]]:
    r = requests.get(f"{COINGECKO_BASE}/coins/bitcoin/market_chart",
                      params={"vs_currency": "usd", "days": DAYS}, timeout=30,
                      headers={"User-Agent": "robinhood-chain-alpha-p6/1.0"})
    r.raise_for_status()
    prices = r.json()["prices"]  # [[ms_timestamp, price], ...]
    return sorted((int(ts / 1000), float(p)) for ts, p in prices)


def align_and_compute_basis(cbbtc: list[tuple[int, float]], btc: list[tuple[int, float]], tolerance_s: int = 1800) -> list[dict]:
    """Для каждой cbBTC-точки ищем ближайшую BTC-точку в пределах
    tolerance_s (CoinGecko отдаёт точки не строго на границе часа для
    90-дневного окна -- обычно ~1ч интервал, но не гарантированно
    выровнено с GT)."""
    btc_sorted = btc
    out = []
    bi = 0
    for ts, cbbtc_price in cbbtc:
        while bi < len(btc_sorted) - 1 and abs(btc_sorted[bi + 1][0] - ts) < abs(btc_sorted[bi][0] - ts):
            bi += 1
        btc_ts, btc_price = btc_sorted[bi]
        if abs(btc_ts - ts) > tolerance_s:
            continue
        basis_pct = (cbbtc_price / btc_price - 1) * 100
        out.append({"timestamp_unix": ts, "timestamp_utc": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                     "cbbtc_price_usd": cbbtc_price, "btc_price_usd": btc_price, "basis_pct": basis_pct})
    return out


def run() -> int:
    print("=== 1. cbBTC price (GT, реальный пул USDC-CBBTC, 90 дней часовых свечей) ===")
    cbbtc = fetch_cbbtc_ohlcv()
    print(f"[p6_basis] cbBTC: {len(cbbtc)} точек, {cbbtc[0][0] if cbbtc else None} .. {cbbtc[-1][0] if cbbtc else None}")

    print("\n=== 2. BTC price (CoinGecko, 90 дней) ===")
    btc = fetch_btc_market_chart()
    print(f"[p6_basis] BTC: {len(btc)} точек, {btc[0][0] if btc else None} .. {btc[-1][0] if btc else None}")

    print("\n=== 3. Выравнивание + базис ===")
    aligned = align_and_compute_basis(cbbtc, btc)
    print(f"[p6_basis] выровнено точек: {len(aligned)} из {len(cbbtc)} cbBTC-точек")

    if not aligned:
        print("[p6_basis] нет выровненных точек -- нечего анализировать")
        Path("data/p3_guard_cache/p6_cbbtc_btc_basis_result.json").write_text(json.dumps({"error": "no aligned points"}, indent=2))
        return 1

    basis_values = [a["basis_pct"] for a in aligned]
    max_abs_basis = max(abs(b) for b in basis_values)
    max_point = max(aligned, key=lambda a: abs(a["basis_pct"]))

    # Дни просадки BTC -- реальные дневные доходности из самого ряда BTC,
    # не предположенные заранее. "Просадка" = день с доходностью < -3%
    # (обычный порог для BTC daily move, не подогнан под цель).
    daily_btc: dict[str, list[float]] = {}
    for ts, price in btc:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        daily_btc.setdefault(day, []).append(price)
    daily_closes = {d: prices[-1] for d, prices in daily_btc.items()}
    days_sorted = sorted(daily_closes.keys())
    daily_returns = {}
    for i in range(1, len(days_sorted)):
        d0, d1 = days_sorted[i - 1], days_sorted[i]
        daily_returns[d1] = (daily_closes[d1] / daily_closes[d0] - 1) * 100
    drawdown_days = sorted([d for d, r in daily_returns.items() if r < -3.0], key=lambda d: daily_returns[d])

    basis_on_drawdown_days = []
    for d in drawdown_days:
        day_points = [a for a in aligned if a["timestamp_utc"].startswith(d)]
        if day_points:
            worst = max(day_points, key=lambda a: abs(a["basis_pct"]))
            basis_on_drawdown_days.append({"date": d, "btc_daily_return_pct": daily_returns[d],
                                            "max_abs_basis_pct_that_day": max(abs(a["basis_pct"]) for a in day_points),
                                            "worst_point": worst})

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_cbbtc_points": len(cbbtc), "n_btc_points": len(btc), "n_aligned": len(aligned),
        "max_abs_basis_pct_90d": max_abs_basis, "max_basis_point": max_point,
        "exceeded_1pct_threshold": max_abs_basis > 1.0,
        "n_drawdown_days_gt3pct": len(drawdown_days),
        "basis_on_drawdown_days": basis_on_drawdown_days,
        "basis_summary_pct": {
            "mean": sum(basis_values) / len(basis_values),
            "min": min(basis_values), "max": max(basis_values),
        },
    }
    print(f"\n[p6_basis] Максимальное |базис| за 90д: {max_abs_basis:.4f}% (в момент {max_point['timestamp_utc']})")
    print(f"[p6_basis] Превысило 1%? {'ДА -- см. docs/P6_HEDGED_LP.md, меняет конструкцию хеджа' if max_abs_basis > 1.0 else 'нет'}")
    print(f"[p6_basis] Дней просадки BTC (<-3%/день): {len(drawdown_days)}")
    for d in basis_on_drawdown_days:
        print(f"    {d['date']}: BTC {d['btc_daily_return_pct']:.2f}%, макс |базис| в этот день = {d['max_abs_basis_pct_that_day']:.4f}%")

    Path("data/p3_guard_cache/p6_cbbtc_btc_basis_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
