#!/usr/bin/env python3
"""Владелец, 2026-09-18: досчёт к solana_29_candidates_flow.py -- ВСЕ 29
кандидатов (и контроль-лидер) оказались на 100% USDC-котируемыми
первыми входами (n_first_entries_sol_quoted=0 у КАЖДОГО), поэтому порог
"первые входы с тратой >=4.3 SOL" из исходного задания буквально даёт
0 везде -- не баг (реальные суммы: 5-15000 USDC у нескольких
кандидатов), а честный факт про эту выборку трейдеров. Раз порог 4.3
SOL смысленно относится к размеру входа В SOL-ЭКВИВАЛЕНТЕ (это и есть
"Copy Buy Range" в DBot), конвертируем USDC-суммы в SOL по РЕАЛЬНОМУ
историческому курсу SOL/USD на момент каждой сделки (block_time), а не
по одной фиксированной оценке -- через тот же пул SOL/USDC
(3ucNos4NbumP...) и метод линейной интерполяции минутных свечей, что
уже используется в solana_buyer200_fast_price.py (SOL_USDC_POOL) для
основного конвейера -- один запрос диапазона на весь 7-суточный период,
без листания на цепи.

Только чтение локального data/solana_29_candidates_flow_7d.json +
GeckoTerminal. Пересчитывает и ДОБАВЛЯЕТ поля (sol_equivalent,
ge_4_3_sol_equivalent) -- ничего не перезаписывает и не выбрасывает."""
from __future__ import annotations

import bisect
import hashlib
import json
import time
from decimal import Decimal as D
from pathlib import Path
from statistics import median

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_PATH = REPO_ROOT / "data" / "solana_29_candidates_flow_7d.json"
OUT_PATH = REPO_ROOT / "data" / "solana_29_candidates_flow_7d_sol_equiv.json"
CACHE_DIR = REPO_ROOT / "data" / "solana_buyer_200" / "rpc_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
SOL_USDC_POOL = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_THRESHOLD = D("4.3")


def gecko_get(path: str, params: dict) -> dict:
    cache_key = f"{path}_{json.dumps(params, sort_keys=True)}"
    cache_f = CACHE_DIR / f"solequiv_{hashlib.sha256(cache_key.encode()).hexdigest()[:32]}.json"
    if cache_f.exists():
        try:
            return json.loads(cache_f.read_text())
        except (ValueError, OSError):
            pass
    backoff = 1.0
    for _ in range(10):
        try:
            resp = requests.get(f"{GECKO_BASE}{path}", params=params, timeout=30,
                                 headers={"Accept": "application/json"})
        except Exception:  # noqa: BLE001
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        if resp.status_code == 429:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        if not resp.ok:
            return {"http_status": resp.status_code}
        data = resp.json()
        cache_f.write_text(json.dumps(data))
        return {"http_status": 200, "body": data}
    return {"http_status": None}


def fetch_sol_usd_candles(lo_time: int, hi_time: int) -> list[list]:
    all_rows: list[list] = []
    cursor = hi_time + 120
    for _ in range(60):
        r = gecko_get(f"/networks/solana/pools/{SOL_USDC_POOL}/ohlcv/minute",
                      {"aggregate": 1, "before_timestamp": cursor, "limit": 1000, "currency": "usd"})
        if r.get("http_status") != 200:
            break
        rows = (((r.get("body") or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not rows:
            break
        all_rows.extend(rows)
        oldest_ts = min(row[0] for row in rows)
        if oldest_ts <= lo_time or oldest_ts >= cursor:
            break
        cursor = oldest_ts
        if len(rows) < 1000:
            break
    all_rows.sort(key=lambda row: row[0])
    return all_rows


def sol_usd_price_at(candles: list[list], t: int) -> float | None:
    if not candles:
        return None
    times = [c[0] + 60 for c in candles]
    idx = bisect.bisect_right(times, t)
    if idx == 0:
        return candles[0][4]
    if idx >= len(candles):
        return candles[-1][4]
    t0, c0 = times[idx - 1], D(str(candles[idx - 1][4]))
    t1, c1 = times[idx], D(str(candles[idx][4]))
    if t1 == t0:
        return float(c0)
    frac = D(t - t0) / D(t1 - t0)
    return float(c0 + (c1 - c0) * frac)


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def recompute(entry: dict, candles: list[list]) -> dict:
    events = entry.get("first_entry_events") or []
    n_ge = 0
    sol_equiv_sizes = []
    for e in events:
        sol_eq = None
        if e.get("quote_mint") == "SOL" and e.get("sol_spent") is not None:
            sol_eq = float(e["sol_spent"])
        elif e.get("quote_mint") == USDC_MINT and e.get("quote_amount") is not None:
            price = sol_usd_price_at(candles, e["block_time"])
            if price and price > 0:
                sol_eq = float(e["quote_amount"]) / price
        e["sol_equivalent"] = sol_eq
        if sol_eq is not None:
            sol_equiv_sizes.append(sol_eq)
            if sol_eq >= float(SOL_THRESHOLD):
                n_ge += 1
    days = entry.get("days_covered") or 0
    return {
        "n_first_entries_sol_equivalent_computed": len(sol_equiv_sizes),
        "n_first_entries_ge_4_3_sol_equivalent": n_ge,
        "sol_equivalent_median": median(sol_equiv_sizes) if sol_equiv_sizes else None,
        "sol_equivalent_p25": pct(sol_equiv_sizes, 0.25),
        "sol_equivalent_p75": pct(sol_equiv_sizes, 0.75),
        "sol_equivalent_min": min(sol_equiv_sizes) if sol_equiv_sizes else None,
        "sol_equivalent_max": max(sol_equiv_sizes) if sol_equiv_sizes else None,
        "first_entries_ge_4_3_sol_equivalent_per_day": round(n_ge / days, 3) if days else None,
    }


def main() -> None:
    data = json.loads(IN_PATH.read_text())
    all_entries = [data["leader_control"]] + [v for v in data["wallets"].values() if v]
    all_events = [e for entry in all_entries for e in (entry.get("first_entry_events") or [])]
    if not all_events:
        print("[usdc_to_sol] нет ни одного first_entry_event -- нечего конвертировать", flush=True)
        return
    lo_time = min(e["block_time"] for e in all_events) - 120
    hi_time = max(e["block_time"] for e in all_events) + 120
    print(f"[usdc_to_sol] диапазон SOL/USD свечей: [{lo_time},{hi_time}] "
          f"({(hi_time - lo_time) / 86400:.2f} суток)", flush=True)
    candles = fetch_sol_usd_candles(lo_time, hi_time)
    print(f"[usdc_to_sol] получено {len(candles)} минутных свечей SOL/USDC", flush=True)

    data["leader_control"].update(recompute(data["leader_control"], candles))
    for addr, entry in data["wallets"].items():
        if entry:
            entry.update(recompute(entry, candles))

    total_ge_per_day = sum((v.get("first_entries_ge_4_3_sol_equivalent_per_day") or 0)
                            for v in data["wallets"].values() if v)
    data["summary_sol_equivalent"] = {
        "note": "первые входы, оплаченные не-SOL (USDC), пересчитаны в SOL-эквивалент по реальному "
                "историческому курсу SOL/USD (GeckoTerminal, пул SOL/USDC) на момент каждой сделки",
        "sum_first_entries_ge_4_3_sol_equivalent_per_day_across_29": round(total_ge_per_day, 3),
        "implied_deposit_sol_at_0_1_per_signal": round(total_ge_per_day * 0.1, 3),
    }
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    print(f"[usdc_to_sol] записано в {OUT_PATH}", flush=True)
    print(f"[usdc_to_sol] лидер (контроль): ge_4.3_sol_equiv_per_day="
          f"{data['leader_control'].get('first_entries_ge_4_3_sol_equivalent_per_day')}", flush=True)
    print(f"[usdc_to_sol] сумма по 29: {data['summary_sol_equivalent']}", flush=True)


if __name__ == "__main__":
    main()
