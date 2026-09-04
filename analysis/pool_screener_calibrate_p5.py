#!/usr/bin/env python3
"""Скринер пулов -- КАЛИБРОВКА (владелец, 2026-09-04, п.1): прогнать наш
собственный пул ETH/USDG (0x52e65b17..., позиция 1000756) через ТОЧНО ТУ
ЖЕ методику, что analysis/pool_screener_sigma_lvr.py применяет к 23
кандидатам -- full-range LVR (sigma^2/8) от 30-дневной GT sigma, ratio =
apyBase_equivalent/LVR -- и сравнить с РЕАЛЬНЫМ, известным по живой
позиции отношением `fee_lvr_ratio_at_hist_sigma` (уже учитывает ФАКТИЧЕСКИЙ
узкий диапазон позиции, не full-range допущение).

apyBase_equivalent (аналог DefiLlama apyBase для пула, которого нет в
DefiLlama) считается из УЖЕ СОБРАННЫХ данных data/p5_fee_accrual.jsonl:
pool_yield_cum = pool_fees_usd_cum / avg_pool_tvl_usd (кумулятивно с
момента открытия позиции), аннуализировано на hours_covered -- ПУЛОВЫЕ
(не "наши") комиссии/TVL, тот же смысл, что apyBase в DefiLlama для
любого другого AMM-пула.

sigma -- ТОЧНО тот же код, что pool_screener_sigma_lvr.py (не
sigma_realized_annualized из p5_fee_accrual.jsonl -- та считается по
ОЧЕНЬ малой выборке точек снимков позиции, ~17ч данных, статистически
ненадёжна, это другая величина)."""
from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path

import requests

GT_BASE = "https://api.geckoterminal.com/api/v2"
MIN_REQUEST_INTERVAL_S = 2.6
RATE_LIMIT_BACKOFF_S = 65.0
RATE_LIMIT_MAX_RETRIES = 2
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "robinhood-chain-alpha-screener/1.0"}

NETWORK = "robinhood"  # подтверждено data/p3_guard_cache/p5_gt_pool_history_result.json
POOL_ADDRESS = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
TARGET_CANDLES = 30 * 24
MAX_PAGES = 3

_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _last_call + MIN_REQUEST_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _get(url: str, params: dict | None = None) -> tuple[int, dict | str]:
    status, body = None, None
    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        _throttle()
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        try:
            status, body = r.status_code, r.json()
        except ValueError:
            status, body = r.status_code, r.text[:500]
        if status == 429 and attempt < RATE_LIMIT_MAX_RETRIES:
            print(f"    429, жду {RATE_LIMIT_BACKOFF_S:.0f}с и повторяю")
            time.sleep(RATE_LIMIT_BACKOFF_S)
            continue
        return status, body
    return status, body


def fetch_ohlcv_paginated(network: str, address: str) -> list[list]:
    all_rows: dict[int, list] = {}
    before_ts: int | None = None
    for page in range(MAX_PAGES):
        params = {"aggregate": 1, "limit": 1000, "currency": "token", "token": "quote",
                  "include_empty_intervals": "true"}
        if before_ts is not None:
            params["before_timestamp"] = before_ts
        status, body = _get(f"{GT_BASE}/networks/{network}/pools/{address}/ohlcv/hour", params=params)
        if status != 200:
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
        if len(all_rows) >= TARGET_CANDLES:
            break
    return [all_rows[ts] for ts in sorted(all_rows.keys())]


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]


def annualized_sigma(rets: list[float], periods_per_year: float) -> float | None:
    if len(rets) < 2:
        return None
    return statistics.stdev(rets) * math.sqrt(periods_per_year)


def run() -> int:
    print(f"=== 1. GT OHLCV (30д, часовые, {NETWORK}/{POOL_ADDRESS}) -- ТОТ ЖЕ метод, что скринер ===")
    rows = fetch_ohlcv_paginated(NETWORK, POOL_ADDRESS)
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "n_candles": len(rows)}

    if len(rows) < TARGET_CANDLES * 0.3:
        result["error"] = (f"мало свечей ({len(rows)} из ожидаемых ~{TARGET_CANDLES}) -- НАША позиция открыта "
                            "2026-09-03, но сам ПУЛ мог существовать раньше; если свечей действительно мало -- "
                            "либо пул моложе 30 дней, либо тонкий по сделкам, честно не додумываем причину")
        print(f"[calibrate] {result['error']}")
        sigma_screener = None
    else:
        closes = [float(r[4]) for r in rows]
        rets = log_returns(closes)
        sigma_screener = annualized_sigma(rets, periods_per_year=24 * 365)
        result["n_returns"] = len(rets)
        result["sigma_screener_annualized"] = sigma_screener
        print(f"[calibrate] sigma (метод скринера, {len(rets)} возвратов) = {sigma_screener}")

    # Реальные данные позиции -- уже собраны, не новый запрос.
    accrual_rows = [json.loads(l) for l in Path("data/p5_fee_accrual.jsonl").read_text().splitlines() if l.strip()]
    latest = accrual_rows[-1]
    result["latest_accrual_timestamp"] = latest["timestamp_utc"]
    result["real_fee_lvr_ratio_at_hist_sigma"] = latest.get("fee_lvr_ratio_at_hist_sigma")
    result["real_fee_lvr_ratio_raw_sigma"] = latest.get("fee_lvr_ratio")
    result["real_sigma_realized_annualized_tiny_sample"] = latest.get("sigma_realized_annualized")
    result["pool_fees_usd_cum"] = latest.get("pool_fees_usd_cum")
    result["avg_pool_tvl_usd"] = latest.get("avg_pool_tvl_usd")
    result["hours_covered"] = latest.get("hours_covered")

    pool_fees = latest.get("pool_fees_usd_cum")
    avg_tvl = latest.get("avg_pool_tvl_usd")
    hours = latest.get("hours_covered")
    if pool_fees is not None and avg_tvl and hours:
        pool_yield_cum = pool_fees / avg_tvl
        apy_base_equivalent_pct = pool_yield_cum * (365.25 * 24 / hours) * 100
        result["apy_base_equivalent_pct"] = apy_base_equivalent_pct
        print(f"[calibrate] apyBase_equivalent (пуловый, аннуализировано) = {apy_base_equivalent_pct:.2f}%")

        if sigma_screener:
            lvr_full_range_frac = (sigma_screener ** 2) / 8
            result["lvr_full_range_frac_screener"] = lvr_full_range_frac
            ratio_screener = (apy_base_equivalent_pct / 100.0) / lvr_full_range_frac
            result["ratio_screener_method"] = ratio_screener
            real_ratio = result["real_fee_lvr_ratio_at_hist_sigma"]
            if real_ratio:
                result["error_multiplier_screener_over_real"] = ratio_screener / real_ratio
            print(f"[calibrate] LVR_full_range (метод скринера) = {lvr_full_range_frac*100:.4f}%")
            print(f"[calibrate] ratio_screener_method (apyBase_equiv/LVR_full_range) = {ratio_screener:.4f}")
            print(f"[calibrate] РЕАЛЬНЫЙ fee_lvr_ratio_at_hist_sigma (живая позиция, узкий диапазон) = {real_ratio}")
            if real_ratio:
                print(f"[calibrate] МНОЖИТЕЛЬ ОШИБКИ (screener/real) = {result['error_multiplier_screener_over_real']:.2f}x")

    Path("data/p3_guard_cache/pool_screener_calibrate_p5_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )
    print(f"\n[calibrate] записано data/p3_guard_cache/pool_screener_calibrate_p5_result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
