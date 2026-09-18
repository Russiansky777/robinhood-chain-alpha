#!/usr/bin/env python3
"""Владелец, 2026-09-18: сводная таблица по горизонтам 5/15/30/60/180/300/
900с/1ч/6ч/24ч для всех 300 отобранных покупок (первые входы и докупки
отдельно). Короткие горизонты (5-300с) уже считаются на цепи в
step_extended_result.json (см. solana_buyer200_fast_price.py) -- этот
скрипт добавляет ДЛИННЫЕ (900с/1ч/6ч/24ч) ТОЛЬКО через GeckoTerminal, как
и было изначально задумано в комментарии HORIZONS_SECONDS того файла
("длинные горизонты отдельно, через GeckoTerminal") -- не трогаем уже
идущий ончейн-прогон, отдельный независимый проход.

Метод -- СОБСТВЕННЫЙ пул купленного минта (не универсальная пара
SOL/USDC), минутные свечи в USD (тот же паттерн, что gecko_find_pool/
gecko_price_after в solana_dbot_pilot_summary.py и _gecko_fetch_range в
solana_buyer200_fast_price.py -- переиспользован, не изобретён заново).

Честные ограничения (не молчим о них):
- Цена в свече первой минуты входа МОЖЕТ включать движение ДО нашей
  покупки (минутная гранулярность, свеча = весь интервал) -- поэтому
  минимум (просадка) считается ТОЛЬКО по свечам, начавшимся В МОМЕНТ
  входа или позже (candle_ts >= entry_time), не по самой входной свече.
  Это НЕДООЦЕНИВАЕТ просадку в первые до-60с, но никогда не приписывает
  нам чужое движение до входа.
- Если ближайшая свеча к горизонту старше 30 минут (минт затих/умер до
  этого горизонта) -- статус stale_extrapolation с реальным разрывом в
  секундах, а не молчаливая экстраполяция как "ok".
- Пул на GeckoTerminal может не найтись вообще (слишком новый/неликвидный
  токен) -- статус no_pool_found, честно, без выдумывания цены."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = REPO_ROOT / "data" / "solana_buyer_200"
CACHE_DIR = OUT_ROOT / "rpc_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_PATH = OUT_ROOT / "long_horizons_result.json"

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
LONG_HORIZONS_SECONDS = [900, 3600, 21600, 86400]
STALE_GAP_S = 30 * 60  # честный порог "минт затих до этого горизонта"
TOTAL_TIME_BUDGET_S = 18 * 60
COMMIT_INTERVAL_S = 90


def gecko_get(path: str, params: dict) -> dict:
    cache_key = f"gecko_long_{path.replace('/', '_')}_{json.dumps(params, sort_keys=True)}"
    import hashlib
    cache_f = CACHE_DIR / f"{hashlib.sha256(cache_key.encode()).hexdigest()[:32]}.json"
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
            return {"http_status": resp.status_code, "body_preview": resp.text[:300]}
        data = resp.json()
        cache_f.write_text(json.dumps(data))
        return {"http_status": 200, "body": data}
    return {"http_status": None, "exception": "исчерпаны попытки"}


_pool_cache: dict[str, str | None] = {}


def find_pool(mint: str) -> str | None:
    if mint in _pool_cache:
        return _pool_cache[mint]
    r = gecko_get(f"/networks/solana/tokens/{mint}/pools", {})
    data = ((r.get("body") or {}).get("data")) or []
    if r.get("http_status") != 200 or not data:
        _pool_cache[mint] = None
        return None

    def liq(p):
        try:
            return float((p.get("attributes") or {}).get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            return 0.0

    data.sort(key=liq, reverse=True)
    pool = (data[0].get("attributes") or {}).get("address")
    _pool_cache[mint] = pool
    return pool


def fetch_candles(pool: str, lo_time: int, hi_time: int) -> list[list]:
    """Минутные свечи пула на [lo_time, hi_time] -- та же пагинация, что
    _gecko_fetch_range в solana_buyer200_fast_price.py (доказано: 5.76
    суток за ~2.5 минуты на одном пуле), но per-mint пул, не универсальная
    пара."""
    all_rows: list[list] = []
    cursor = hi_time + 120
    for _ in range(60):
        r = gecko_get(f"/networks/solana/pools/{pool}/ohlcv/minute",
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


def price_at(candles: list[list], t: int) -> dict:
    """Линейная интерполяция между закрытиями (закрытие = цена на конец
    минуты, ts+60) -- тот же метод, что gecko_price_interpolated в
    solana_buyer200_fast_price.py."""
    if not candles:
        return {"status": "no_candles"}
    times = [c[0] + 60 for c in candles]
    import bisect
    idx = bisect.bisect_right(times, t)
    if idx == 0:
        gap = times[0] - t
        return {"status": "ok" if gap <= STALE_GAP_S else "stale_extrapolation",
                "price_usd": candles[0][4], "gap_seconds": gap, "extrap": "before_first"}
    if idx >= len(candles):
        gap = t - times[-1]
        return {"status": "ok" if gap <= STALE_GAP_S else "stale_extrapolation",
                "price_usd": candles[-1][4], "gap_seconds": gap, "extrap": "after_last"}
    t0, c0 = times[idx - 1], D(str(candles[idx - 1][4]))
    t1, c1 = times[idx], D(str(candles[idx][4]))
    if t1 == t0:
        return {"status": "ok", "price_usd": float(c0)}
    frac = D(t - t0) / D(t1 - t0)
    price = c0 + (c1 - c0) * frac
    return {"status": "ok", "price_usd": float(price)}


def min_low_from(candles: list[list], entry_time: int, horizon_end: int) -> float | None:
    """Минимум low ТОЛЬКО по свечам, начавшимся В МОМЕНТ входа или позже
    (candle_ts >= entry_time) и закончившимся до конца горизонта -- см.
    честное ограничение в docstring модуля (не приписываем чужое
    движение до нашего входа)."""
    lows = [c[3] for c in candles if entry_time <= c[0] < horizon_end]
    return min(lows) if lows else None


def process_signature(sig: str, mint: str, entry_time: int) -> dict:
    pool = find_pool(mint)
    if not pool:
        return {sec: {"status": "no_pool_found", "mint": mint} for sec in LONG_HORIZONS_SECONDS}
    candles = fetch_candles(pool, entry_time, entry_time + max(LONG_HORIZONS_SECONDS))
    out = {}
    for sec in LONG_HORIZONS_SECONDS:
        t = entry_time + sec
        p = price_at(candles, t)
        p["pool"] = pool
        low = min_low_from(candles, entry_time, t)
        p["min_low_usd_from_entry"] = low
        out[sec] = p
    return out


def main() -> None:
    rows = json.loads((OUT_ROOT / "selected_300.json").read_text())
    data: dict[str, dict] = {}
    if OUT_PATH.exists():
        try:
            data = json.loads(OUT_PATH.read_text())
        except (ValueError, OSError):
            data = {}

    started_at = time.monotonic()
    last_commit_at = started_at
    n_done = sum(1 for r in rows if r["signature"] in data)
    print(f"[long_horizons] всего покупок: {len(rows)}, уже готово: {n_done}", flush=True)

    for row in rows:
        sig = row["signature"]
        if sig in data:
            continue
        if time.monotonic() - started_at > TOTAL_TIME_BUDGET_S:
            print("[long_horizons] бюджет времени исчерпан -- остальное на следующий прогон", flush=True)
            break
        result = process_signature(sig, row["mint"], row["time"])
        data[sig] = {str(sec): v for sec, v in result.items()}
        statuses = {sec: v.get("status") for sec, v in result.items()}
        print(f"[long_horizons] {sig[:12]}.. mint={row['mint'][:8]}.. {statuses}", flush=True)
        if time.monotonic() - last_commit_at > COMMIT_INTERVAL_S:
            OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
            fp._git_commit_progress("long_horizons", [OUT_PATH])
            last_commit_at = time.monotonic()

    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    n_done = sum(1 for r in rows if r["signature"] in data)
    print(f"[long_horizons] прогон завершён: {n_done}/{len(rows)} готово", flush=True)


if __name__ == "__main__":
    main()
