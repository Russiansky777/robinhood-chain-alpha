#!/usr/bin/env python3
"""Разведка ПЕРЕД полным историческим бэкфиллом (владелец, 2026-09-05):
`fundingHistory` Hyperliquid ещё НЕ использовался в этом проекте (только
`metaAndAssetCtxs` для текущей ставки, funding_spread_hourly_snapshot.py)
-- реальная форма ответа/пагинация не подтверждены. docs.hyperliquid.xyz
заблокирован для прямого фетча (тот же прокси-блок, что sec.gov) --
проверяем реальным вызовом через GH Actions, не по памяти/документации.

Lighter `/api/v1/fundings` пагинация НАЗАД (count_back=750) уже реально
подтверждена и работает (p4_lighter_markets.py::fetch_funding_history,
lighter_funding_endpoint_probe.py) -- здесь только сверяем реальное
самое РАННЕЕ доступное значение timestamp для BTC (market_id=1), чтобы
знать, докуда реально можно бэкфиллить, не предполагая произвольную
глубину."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
HYPERLIQUID_API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-funding-backfill-probe/1.0"}


def run() -> int:
    result = {}

    print("=== 1. Hyperliquid fundingHistory -- реальная форма ответа (BTC, короткое окно) ===")
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 6 * 3600 * 1000  # последние 6 часов -- дёшево, только форма ответа
    r = requests.post(f"{HYPERLIQUID_API_BASE}/info", headers=HEADERS,
                       json={"type": "fundingHistory", "coin": "BTC", "startTime": start_ms, "endTime": now_ms}, timeout=20)
    body = None
    try:
        body = r.json()
    except Exception:
        body = r.text[:1000]
    result["hl_funding_history_short_window"] = {"status": r.status_code, "n_records": len(body) if isinstance(body, list) else None,
                                                    "body_preview": json.dumps(body, default=str)[:2000]}
    print(json.dumps(result["hl_funding_history_short_window"], indent=2, ensure_ascii=False))

    print("\n=== 2. Hyperliquid fundingHistory -- длинное окно (с 2026-07-01), проверка реального лимита страницы ===")
    jul1_ms = int(time.mktime(time.strptime("2026-07-01", "%Y-%m-%d"))) * 1000
    r2 = requests.post(f"{HYPERLIQUID_API_BASE}/info", headers=HEADERS,
                        json={"type": "fundingHistory", "coin": "BTC", "startTime": jul1_ms, "endTime": now_ms}, timeout=30)
    body2 = None
    try:
        body2 = r2.json()
    except Exception:
        body2 = r2.text[:1000]
    n2 = len(body2) if isinstance(body2, list) else None
    first_rec = body2[0] if isinstance(body2, list) and body2 else None
    last_rec = body2[-1] if isinstance(body2, list) and body2 else None
    result["hl_funding_history_long_window"] = {
        "status": r2.status_code, "n_records": n2, "first_record": first_rec, "last_record": last_rec,
        "expected_hours_if_uncapped": (now_ms - jul1_ms) / 3_600_000,
    }
    print(json.dumps(result["hl_funding_history_long_window"], indent=2, ensure_ascii=False, default=str))

    print("\n=== 3. Lighter -- реальный самый ранний доступный timestamp для BTC (market_id=1) ===")
    # Пагинация НАЗАД той же логикой, что p4_lighter_markets.py, но
    # только до первого КОРОТКОГО/пустого ответа -- не весь бэкфилл,
    # только чтобы узнать earliest.
    end_ts = int(time.time())
    earliest_seen = None
    pages_checked = 0
    for _ in range(20):
        resp = requests.get(f"{LIGHTER_API_BASE}/api/v1/fundings", headers=HEADERS, params={
            "market_id": 1, "resolution": "1h", "start_timestamp": 0, "end_timestamp": end_ts, "count_back": 750,
        }, timeout=20)
        pages_checked += 1
        if resp.status_code != 200:
            break
        page = resp.json().get("fundings", [])
        if not page:
            break
        oldest_ts = min(r["timestamp"] for r in page)
        earliest_seen = oldest_ts
        if len(page) < 750:
            break
        end_ts = oldest_ts - 1
    result["lighter_earliest_probe"] = {
        "earliest_timestamp_unix": earliest_seen,
        "earliest_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(earliest_seen)) if earliest_seen else None,
        "pages_checked": pages_checked,
    }
    print(json.dumps(result["lighter_earliest_probe"], indent=2, ensure_ascii=False))

    Path("data/p3_guard_cache/funding_historical_probe_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )
    print("\n[probe] записано data/p3_guard_cache/funding_historical_probe_result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
