#!/usr/bin/env python3
"""Скринер пулов, шаг 3: σ (реализованная, 30 дней, часовые свечи GT) и
LVR для каждого резолвленного AMM-пула (data/p3_guard_cache/
pool_screener_resolve_pools_result.json, только match_quality=plausible).

Переиспользует ТОЧНО тот же паттерн, что analysis/p5_gt_pool_history.py
(уже проверен на пуле ETH/USDG): `currency=token, token=quote` -- цена
в единицах ВТОРОГО актива пары (натуральный обменный курс между двумя
ногами пула), а НЕ USD-цена одной ноги. Это осознанный выбор, не
недосмотр -- LVR определяется волатильностью ИМЕННО этого обменного
курса (цены одного актива пула в единицах другого), а не USD-уровня
какой-то одной ноги по отдельности. `include_empty_intervals=true`
ОБЯЗАТЕЛЕН (см. docs/PROJECT_STATE.md §6.1) -- без него таймстемпы
"едут" при отсутствии сделок в интервале.

Формула LVR (полный диапазон, Milionis et al. -- тот же источник, что
уже задокументирован в analysis/p5_live_position_snapshot.py для LVR
позиции 1000756): LVR-темп как ДОЛЯ TVL в год = sigma_annualized^2 / 8.
Это НЕ absolute-$ формула из p5_live_position_snapshot.py
(continuous_lvr_theoretical_usd, привязана к конкретному L/P нашей
живой позиции) -- здесь нужна именно нормированная (per-$-TVL) версия,
сравнимая с apyBase (тоже % от TVL/год), чтобы отношение apyBase/LVR
было безразмерным и сравнимым МЕЖДУ разными пулами с разным размером.

apyBase/apyReward -- РАЗДЕЛЬНО, как их сообщает DefiLlama (владелец:
"apyBase и apyReward отдельно, как отчитывает DefiLlama") -- ratio
считается ТОЛЬКО от apyBase (органическая комиссия, не эмиссия), это и
есть проверка "живёт ли пул без стимулов".

Диапазон ×k (P5-подобная узкая полоса, не только full-range) -- НЕ
считается в этом скрипте: ширина диапазона специфична для КОНКРЕТНОЙ
будущей позиции (зависит от выбранного кандидата), не для скрининга
~20 кандидатов разом. Откладывается до момента, когда конкретный
кандидат пройдёт порог и станет предметом отдельного решения (тот же
порядок, что и entry_cost/wash_share/Lighter-доступность)."""
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

TARGET_DAYS = 30
TARGET_CANDLES = TARGET_DAYS * 24  # часовые свечи
MAX_PAGES = 3  # 3x1000 -- с большим запасом над 720 нужными
LVR_APY_THRESHOLD = 2.0

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
    d = json.load(open("data/p3_guard_cache/pool_screener_resolve_pools_result.json"))
    plausible = [c for c in d["candidates"] if c["gt_resolution"].get("match_quality") == "plausible"]
    print(f"[sigma_lvr] {len(plausible)} кандидатов с plausible-резолвингом из {d['n_amm_candidates']}")

    results = []
    for i, c in enumerate(plausible):
        network = c["gt_resolution"]["network"]
        address = c["gt_resolution"]["resolved_pool_address"]
        print(f"\n=== {i+1}/{len(plausible)}: {c['chain']} {c['project']} {c['symbol']} ({address}) ===")
        entry = {**c}
        try:
            rows = fetch_ohlcv_paginated(network, address)
            if len(rows) < TARGET_CANDLES * 0.5:
                entry["sigma_error"] = f"недостаточно свечей ({len(rows)} из ожидаемых ~{TARGET_CANDLES}) -- пул моложе 30 дней или тонкий по сделкам"
                entry["n_candles"] = len(rows)
            else:
                closes = [float(r[4]) for r in rows]
                rets = log_returns(closes)
                sigma = annualized_sigma(rets, periods_per_year=24 * 365)
                entry["n_candles"] = len(rows)
                entry["n_returns"] = len(rets)
                entry["sigma_realized_annualized"] = sigma
                if sigma is not None:
                    lvr_full_range_frac = (sigma ** 2) / 8
                    entry["lvr_full_range_annualized_frac"] = lvr_full_range_frac
                    apy_base = c.get("apyBase") or 0.0
                    entry["apy_base_over_lvr_full_range"] = (apy_base / 100.0) / lvr_full_range_frac if lvr_full_range_frac else None
                    entry["passes_threshold_2x"] = (
                        entry["apy_base_over_lvr_full_range"] is not None and entry["apy_base_over_lvr_full_range"] >= LVR_APY_THRESHOLD
                    )
                entry["sigma_error"] = None
        except Exception as exc:  # noqa: BLE001
            entry["sigma_error"] = str(exc)[:500]
        print(f"    sigma={entry.get('sigma_realized_annualized')}, ratio={entry.get('apy_base_over_lvr_full_range')}, "
              f"passes={entry.get('passes_threshold_2x')}, error={entry.get('sigma_error')}")
        results.append(entry)

    ok = [r for r in results if r.get("apy_base_over_lvr_full_range") is not None]
    ok.sort(key=lambda r: -r["apy_base_over_lvr_full_range"])
    n_pass = sum(1 for r in ok if r["passes_threshold_2x"])

    print(f"\n[sigma_lvr] посчитано σ/LVR для {len(ok)}/{len(plausible)} кандидатов; "
          f"проходят порог apyBase/LVR>={LVR_APY_THRESHOLD}: {n_pass}")
    print("\n=== Отсортировано по apyBase/LVR_full_range (убывание) ===")
    for r in ok:
        apy_base_str = f"{r.get('apyBase'):.2f}%" if r.get("apyBase") is not None else "н/д"
        print(f"  {r['chain']:10} {r['project']:22} {r['symbol']:22} apyBase={apy_base_str} "
              f"sigma={r['sigma_realized_annualized']:.4f} LVR={r['lvr_full_range_annualized_frac']*100:.4f}% "
              f"ratio={r['apy_base_over_lvr_full_range']:.2f} pass={r['passes_threshold_2x']}")

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_plausible_candidates": len(plausible),
        "n_sigma_computed": len(ok),
        "n_pass_threshold_2x": n_pass,
        "lvr_apy_threshold": LVR_APY_THRESHOLD,
        "candidates": results,
    }
    Path("data/p3_guard_cache/pool_screener_sigma_lvr_result.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
