#!/usr/bin/env python3
"""P6 -- пересчёт базиса cbBTC/BTC (владелец, 2026-09-04, после первого
результата analysis/p6_cbbtc_btc_basis.py): нужны 3 доп. статистики,
которых не было в исходном результате (тот сохранял только summary,
не сырой почасовой ряд/объёмы) --

  1. число часов из 2160 с |базис| > 1% (сырой почасовой ряд),
  2. максимальная серия таких часов подряд,
  3. базис по СУТОЧНОМУ VWAP за 90 дней (max, число дней > 1%).

Поскольку исходный p6_cbbtc_btc_basis_result.json не сохранил сырой
выровненный ряд и объёмы (только max/mean/min по нему), честно
пересчитать "без новых запросов" из уже сохранённого нечем -- сырых
точек на диске нет. Сделан РОВНО ОДИН дополнительный проход тем же
методом/параметрами (не новое исследование, не другой источник) --
и в этот раз сырой выровненный ряд (с объёмами) СОХРАНЯЕТСЯ на диск,
чтобы больше такой необходимости не было.

Kill-критерий №3 P6 (владелец, 2026-09-04, переписан): "суточный
VWAP-базис cbBTC/BTC > 2% дольше 24 часов подряд". Часовые выбросы
тонкого пула критерием не являются. Оперативно: VWAP считается по
календарным суткам (UTC) -- "дольше 24 часов подряд" => 2 и более
ПОДРЯД идущих суток с |суточный VWAP-базис| > 2%.
"""
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
POOL_ADDRESS = "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb"  # USDC-CBBTC, aerodrome-slipstream (тот же пул, что p6_cbbtc_btc_basis.py)
DAYS = 90
TARGET_CANDLES = DAYS * 24
MAX_PAGES = 4

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

VWAP_REPORT_THRESHOLD_PCT = 1.0   # для пункта (c) отчёта -- дни > 1%
KILL_CRITERION_THRESHOLD_PCT = 2.0  # для НОВОГО kill-критерия №3
KILL_CRITERION_MIN_CONSECUTIVE_DAYS = 2  # "дольше 24 часов подряд" на суточном VWAP => 2+ суток подряд

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


def fetch_cbbtc_ohlcv_with_volume() -> list[tuple[int, float, float]]:
    """(timestamp, close_price_usdc, volume) -- volume как есть из GT (вес
    для VWAP не требует единичной конвертации, только внутренняя
    сопоставимость точек одного ряда)."""
    all_rows: dict[int, tuple[float, float]] = {}
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
            all_rows[int(row[0])] = (float(row[4]), float(row[5]))  # close, volume
        oldest_ts = min(int(row[0]) for row in rows)
        if before_ts is not None and oldest_ts >= before_ts:
            break
        before_ts = oldest_ts
        if len(rows) < 1000 or len(all_rows) >= TARGET_CANDLES:
            break
    return sorted((ts, c, v) for ts, (c, v) in all_rows.items())


def fetch_btc_market_chart_with_volume() -> list[tuple[int, float, float]]:
    r = requests.get(f"{COINGECKO_BASE}/coins/bitcoin/market_chart",
                      params={"vs_currency": "usd", "days": DAYS}, timeout=30,
                      headers={"User-Agent": "robinhood-chain-alpha-p6/1.0"})
    r.raise_for_status()
    body = r.json()
    prices = {int(ts / 1000): float(p) for ts, p in body["prices"]}
    vols = {int(ts / 1000): float(v) for ts, v in body.get("total_volumes", [])}
    out = []
    for ts, p in prices.items():
        # ближайший volume-таймстамп (CoinGecko отдаёт prices/total_volumes
        # с одними и теми же временными метками в market_chart -- прямое
        # совпадение по ключу ожидается почти всегда)
        v = vols.get(ts)
        if v is None:
            nearest = min(vols.keys(), key=lambda t: abs(t - ts)) if vols else None
            v = vols.get(nearest, 0.0) if nearest is not None else 0.0
        out.append((ts, p, v))
    return sorted(out)


def align(cbbtc: list[tuple[int, float, float]], btc: list[tuple[int, float, float]], tolerance_s: int = 1800) -> list[dict]:
    btc_sorted = btc
    out = []
    bi = 0
    for ts, cbbtc_price, cbbtc_vol in cbbtc:
        while bi < len(btc_sorted) - 1 and abs(btc_sorted[bi + 1][0] - ts) < abs(btc_sorted[bi][0] - ts):
            bi += 1
        btc_ts, btc_price, btc_vol = btc_sorted[bi]
        if abs(btc_ts - ts) > tolerance_s:
            continue
        basis_pct = (cbbtc_price / btc_price - 1) * 100
        out.append({"timestamp_unix": ts, "timestamp_utc": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                     "cbbtc_price_usd": cbbtc_price, "cbbtc_volume": cbbtc_vol,
                     "btc_price_usd": btc_price, "btc_volume_usd": btc_vol, "basis_pct": basis_pct})
    return out


def longest_consecutive_run(flags: list[bool]) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def run() -> int:
    print("=== 1. cbBTC price+volume (GT, реальный пул USDC-CBBTC, 90 дней часовых свечей) ===")
    cbbtc = fetch_cbbtc_ohlcv_with_volume()
    print(f"[p6_basis_recompute] cbBTC: {len(cbbtc)} точек")

    print("\n=== 2. BTC price+volume (CoinGecko, 90 дней) ===")
    btc = fetch_btc_market_chart_with_volume()
    print(f"[p6_basis_recompute] BTC: {len(btc)} точек")

    print("\n=== 3. Выравнивание ===")
    aligned = align(cbbtc, btc)
    print(f"[p6_basis_recompute] выровнено точек: {len(aligned)}")

    if not aligned:
        Path("data/p3_guard_cache/p6_basis_recompute_result.json").write_text(json.dumps({"error": "no aligned points"}, indent=2))
        return 1

    # --- (a)/(b): сырой почасовой ряд, порог 1% ---
    over_1pct_flags = [abs(a["basis_pct"]) > 1.0 for a in aligned]
    n_hours_gt_1pct = sum(over_1pct_flags)
    max_consecutive_hours_gt_1pct = longest_consecutive_run(over_1pct_flags)

    # --- (c): суточный VWAP-базис (календарные сутки UTC) ---
    by_day: dict[str, list[dict]] = {}
    for a in aligned:
        day = a["timestamp_utc"][:10]
        by_day.setdefault(day, []).append(a)

    daily_vwap: list[dict] = []
    for day in sorted(by_day.keys()):
        pts = by_day[day]
        cbbtc_vol_sum = sum(p["cbbtc_volume"] for p in pts)
        btc_vol_sum = sum(p["btc_volume_usd"] for p in pts)
        if cbbtc_vol_sum <= 0 or btc_vol_sum <= 0:
            # честный null -- не подменяем нулевой реальный объём фиктивным весом
            daily_vwap.append({"date": day, "n_hours": len(pts), "vwap_basis_pct": None,
                                "note": "нулевой объём за сутки хотя бы на одной ноге -- VWAP не считается"})
            continue
        vwap_cbbtc = sum(p["cbbtc_price_usd"] * p["cbbtc_volume"] for p in pts) / cbbtc_vol_sum
        vwap_btc = sum(p["btc_price_usd"] * p["btc_volume_usd"] for p in pts) / btc_vol_sum
        vwap_basis_pct = (vwap_cbbtc / vwap_btc - 1) * 100
        daily_vwap.append({"date": day, "n_hours": len(pts), "vwap_cbbtc_usd": vwap_cbbtc,
                            "vwap_btc_usd": vwap_btc, "vwap_basis_pct": vwap_basis_pct})

    valid_days = [d for d in daily_vwap if d["vwap_basis_pct"] is not None]
    max_abs_daily_vwap = max((abs(d["vwap_basis_pct"]) for d in valid_days), default=None)
    max_abs_daily_vwap_day = max(valid_days, key=lambda d: abs(d["vwap_basis_pct"])) if valid_days else None
    n_days_gt_report_threshold = sum(1 for d in valid_days if abs(d["vwap_basis_pct"]) > VWAP_REPORT_THRESHOLD_PCT)

    # --- новый kill-критерий №3: суточный VWAP > 2%, дольше 24ч подряд (>=2 суток подряд) ---
    kill_flags = [(d["vwap_basis_pct"] is not None and abs(d["vwap_basis_pct"]) > KILL_CRITERION_THRESHOLD_PCT) for d in daily_vwap]
    max_consecutive_days_gt_2pct = longest_consecutive_run(kill_flags)
    kill_criterion_3_triggered = max_consecutive_days_gt_2pct >= KILL_CRITERION_MIN_CONSECUTIVE_DAYS
    n_days_gt_2pct = sum(kill_flags)

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method_note": ("Один дополнительный проход тем же методом/параметрами, что "
                         "p6_cbbtc_btc_basis.py -- исходный результат не сохранял сырой "
                         "ряд/объёмы, пересчитать без единого нового запроса было нечем. "
                         "Сырой ряд с объёмами сохранён ниже для будущих пересчётов."),
        "n_aligned": len(aligned),
        "raw_hourly_over_1pct": {
            "n_hours_abs_basis_gt_1pct": n_hours_gt_1pct,
            "n_hours_total": len(aligned),
            "max_consecutive_hours_gt_1pct": max_consecutive_hours_gt_1pct,
        },
        "daily_vwap_basis": {
            "days": daily_vwap,
            "max_abs_daily_vwap_basis_pct": max_abs_daily_vwap,
            "max_abs_daily_vwap_basis_day": max_abs_daily_vwap_day,
            "n_days_gt_1pct_report_threshold": n_days_gt_report_threshold,
            "n_valid_days": len(valid_days),
            "n_days_no_volume": len(daily_vwap) - len(valid_days),
        },
        "kill_criterion_3_rewritten": {
            "definition": "суточный (календарные сутки UTC) VWAP-базис cbBTC/BTC > 2%, дольше 24 часов подряд (>=2 суток подряд)",
            "threshold_pct": KILL_CRITERION_THRESHOLD_PCT,
            "n_days_gt_2pct": n_days_gt_2pct,
            "max_consecutive_days_gt_2pct": max_consecutive_days_gt_2pct,
            "triggered": kill_criterion_3_triggered,
        },
        "raw_aligned_series_with_volumes": aligned,
    }

    print(f"\n[p6_basis_recompute] часов с |базис|>1%: {n_hours_gt_1pct}/{len(aligned)}, макс. серия подряд: {max_consecutive_hours_gt_1pct}ч")
    print(f"[p6_basis_recompute] суточный VWAP-базис: max |{max_abs_daily_vwap:.4f}%| "
          f"({max_abs_daily_vwap_day['date'] if max_abs_daily_vwap_day else None}), "
          f"дней > {VWAP_REPORT_THRESHOLD_PCT}%: {n_days_gt_report_threshold}/{len(valid_days)}")
    print(f"[p6_basis_recompute] НОВЫЙ kill-критерий №3: дней > {KILL_CRITERION_THRESHOLD_PCT}% = {n_days_gt_2pct}, "
          f"макс. серия подряд = {max_consecutive_days_gt_2pct} суток -- "
          f"{'СРАБОТАЛ' if kill_criterion_3_triggered else 'НЕ сработал'}")

    Path("data/p3_guard_cache/p6_basis_recompute_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
