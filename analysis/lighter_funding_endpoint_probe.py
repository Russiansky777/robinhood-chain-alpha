#!/usr/bin/env python3
"""Разведка ПЕРЕД написанием логгера фандинга: `/api/v1/fundings`
подтверждён на mainnet.zklighter.elliot.ai (P4, analysis/p4_lighter_markets.py),
НЕ подтверждён на api.rh.lighter.xyz (Robinhood-инстанс, ТОТ ЖЕ хост, что
хедж P5 и что используется в data/funding_pairs.json) -- разные деплои
одного и того же ПО, эндпоинты не гарантированно идентичны без проверки.
api.rh.lighter.xyz заблокирован из интерактивной песочницы (тот же прокси-
блок, что sec.gov/hyperliquid.xyz) -- проверяем реально через GH Actions."""
from __future__ import annotations

import json
from pathlib import Path

import requests

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-funding-logger-probe/1.0"}

BTC_MARKET_ID = 1  # data/funding_pairs.json, cohort=primary


def probe(path: str, params: dict) -> dict:
    try:
        r = requests.get(f"{LIGHTER_API_BASE}{path}", params=params, headers=HEADERS, timeout=20)
        body = None
        try:
            body = r.json()
        except Exception:
            body = r.text[:1000]
        return {"status": r.status_code, "body_preview": json.dumps(body, default=str)[:2000]}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:500]}


def run() -> int:
    result = {}
    print("=== 1. /api/v1/fundings (per-market hourly funding history) ===")
    result["fundings"] = probe("/api/v1/fundings", {"market_id": BTC_MARKET_ID, "resolution": "1h", "count_back": 3})
    print(json.dumps(result["fundings"], indent=2, ensure_ascii=False))

    print("\n=== 2. /api/v1/orderBookOrders (depth for +-0.5% calc) ===")
    result["orderBookOrders"] = probe("/api/v1/orderBookOrders", {"market_id": BTC_MARKET_ID, "limit": 50})
    print(json.dumps(result["orderBookOrders"], indent=2, ensure_ascii=False))

    print("\n=== 3. /api/v1/orderBookDetails (mark price, 24h volume, possible base_interest_rate) ===")
    result["orderBookDetails"] = probe("/api/v1/orderBookDetails", {"filter": "all"})
    # усечь -- список рынков большой, нужен только BTC для проверки
    try:
        body = json.loads(result["orderBookDetails"]["body_preview"].replace("'", '"')) if False else None
    except Exception:
        body = None
    print(f"status={result['orderBookDetails'].get('status')}, len_preview={len(result['orderBookDetails'].get('body_preview', ''))}")

    Path("data/p3_guard_cache/lighter_funding_endpoint_probe_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
