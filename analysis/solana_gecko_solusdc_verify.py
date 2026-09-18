#!/usr/bin/env python3
"""Проверка идеи владельца: заменить ончейн-листание пула SOL/USDC
(3ucNos4NbumP..., ~300-470k подписей за окно) на минутные свечи
GeckoTerminal -- один(-несколько, при пагинации) запрос на ВЕСЬ период
сбора вместо повторного полного листания под каждый разнесённый по
времени кластер покупок.

НЕ трогает работающий расширенный прогон -- отдельный скрипт, читает
уже посчитанные ончейн точки из step_extended_result.json (у которых
последняя нога -- пул 3ucNos4NbumP, подтверждённый SOL/USDC) и сверяет
их с ценой по свече GeckoTerminal на тот же target_time. Порог
приемлемости -- поставлен владельцем: доли процента. Больше -- не
внедрять, честно доложить."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = REPO_ROOT / "data" / "solana_buyer_200"
CACHE_DIR = OUT_ROOT / "rpc_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SOL_USDC_POOL = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"  # подтверждён на цепи: m0=SOL, m1=USDC
GECKO_BASE = "https://api.geckoterminal.com/api/v2"


def gecko_get(path: str, params: dict) -> dict:
    cache_key = CACHE_DIR / f"gecko_{path.replace('/', '_')}_{json.dumps(params, sort_keys=True)}.json"
    if cache_key.exists():
        try:
            return json.loads(cache_key.read_text())
        except (ValueError, OSError):
            pass
    backoff = 1.0
    for attempt in range(10):
        try:
            resp = requests.get(f"{GECKO_BASE}{path}", params=params, timeout=30,
                                 headers={"Accept": "application/json"})
        except Exception as exc:  # noqa: BLE001
            print(f"[gecko] network error (попытка {attempt+1}): {exc}", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        if resp.status_code == 429:
            print(f"[gecko] 429 rate limit (попытка {attempt+1}), жду {backoff:.0f}с", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        cache_key.write_text(json.dumps(data))
        return data
    raise RuntimeError(f"GeckoTerminal {path} исчерпал попытки")


def fetch_minute_ohlcv(pool: str, before_ts: int, pages: int = 3) -> list[tuple[int, float, float, float, float, float]]:
    """Пагинация назад по before_timestamp, aggregate=1 (минутные свечи),
    limit=1000 (макс. на бесплатном тарифе, судя по документации)."""
    all_rows: list[list] = []
    cursor = before_ts
    for _ in range(pages):
        data = gecko_get(f"/networks/solana/pools/{pool}/ohlcv/minute",
                          {"aggregate": 1, "before_timestamp": cursor, "limit": 1000, "currency": "usd"})
        rows = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        if not rows:
            break
        all_rows.extend(rows)
        oldest_ts = min(r[0] for r in rows)
        if oldest_ts >= cursor:
            break
        cursor = oldest_ts
        if len(rows) < 1000:
            break
    all_rows.sort(key=lambda r: r[0])
    return all_rows


def price_at(rows: list, t: int) -> tuple[float, int] | None:
    """Последняя свеча с open_time<=t -- close этой свечи. rows
    отсортированы по времени по возрастанию."""
    import bisect
    times = [r[0] for r in rows]
    idx = bisect.bisect_right(times, t) - 1
    if idx < 0:
        return None
    return rows[idx][4], rows[idx][0]  # close, candle_open_time


if __name__ == "__main__":
    result_path = OUT_ROOT / "step_extended_result.json"
    data = json.loads(result_path.read_text())

    candidates = []
    for c in data:
        if c.get("mine_status") != "ok":
            continue
        legs = c.get("legs", [])
        if len(legs) >= 1 and legs[-1].get("pool", "") == SOL_USDC_POOL:
            candidates.append(c)

    print(f"[verify] найдено {len(candidates)} уже посчитанных ончейн точек с ногой SOL/USDC", flush=True)
    if not candidates:
        print("[verify] нет данных для проверки -- выхожу", flush=True)
        sys.exit(1)

    # Разброс по подписям, не по 6 подряд одной покупки -- берём каждую 15-ю
    # плюс первую и последнюю, до 8 штук.
    step = max(len(candidates) // 8, 1)
    sample = candidates[::step][:8]

    times = [c["legs"][-1]["target"] for c in candidates]
    lo_t, hi_t = min(times), max(times)
    print(f"[verify] загружаю минутные свечи GeckoTerminal для {SOL_USDC_POOL[:12]}.. "
          f"на диапазон [{lo_t},{hi_t}] ({(hi_t-lo_t)/3600:.1f}ч), before_timestamp={hi_t+120}", flush=True)
    rows = fetch_minute_ohlcv(SOL_USDC_POOL, before_ts=hi_t + 120, pages=15)
    print(f"[verify] получено {len(rows)} минутных свечей, диапазон "
          f"[{rows[0][0] if rows else None},{rows[-1][0] if rows else None}]", flush=True)

    report = []
    for c in sample:
        last_leg = c["legs"][-1]
        t = last_leg["target"]
        onchain_price = D(last_leg["event"]["p1_per_0"])
        pa = price_at(rows, t)
        if pa is None:
            report.append(dict(signature=c["signature"][:12], seconds=c["seconds"], target=t,
                                onchain=str(onchain_price), gecko=None, status="no_candle"))
            continue
        gecko_close, candle_t = pa
        gecko_price = D(str(gecko_close))
        diff_pct = abs(gecko_price - onchain_price) / onchain_price * 100
        report.append(dict(signature=c["signature"][:12], seconds=c["seconds"], target=t,
                            candle_age_s=t - candle_t, onchain=str(onchain_price), gecko=str(gecko_price),
                            diff_pct=str(diff_pct)))
        print(f"[verify] {c['signature'][:12]}.. sec={c['seconds']} t={t} onchain={onchain_price} "
              f"gecko={gecko_price} (свеча {t - candle_t}с назад) diff={diff_pct:.4f}%", flush=True)

    out_path = OUT_ROOT / "gecko_solusdc_verify_result.json"
    out_path.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    diffs = [D(r["diff_pct"]) for r in report if r.get("diff_pct") is not None]
    print(json.dumps({
        "n_compared": len(diffs),
        "max_diff_pct": str(max(diffs)) if diffs else None,
        "mean_diff_pct": str(sum(diffs) / len(diffs)) if diffs else None,
        "out_path": str(out_path),
    }, indent=2))
