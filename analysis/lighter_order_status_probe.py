#!/usr/bin/env python3
"""Реальный статус трёх неудачных хедж-ордеров через ПРАВИЛЬНЫЙ параметр
(найден из настоящего OpenAPI-схемы, .md-докстринг accountOrders,
2026-09-03): требуется `client_order_indexes` (МНОЖЕСТВЕННОЕ число,
через запятую, до 20 штук), не `client_order_index`/`order_index`,
которые пробовались раньше и давали 400.

Реальный enum статуса заказа (из той же схемы, дословно):
in-progress, pending, open, filled, canceled, canceled-post-only,
canceled-reduce-only, canceled-position-not-allowed,
canceled-margin-not-allowed, canceled-too-much-slippage,
canceled-not-enough-liquidity, canceled-self-trade, canceled-expired,
canceled-oco, canceled-child, canceled-liquidation,
canceled-invalid-balance.

Только чтение, ордеров нет.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/lighter_order_status_probe_result.json")
LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
ACCOUNT_INDEX = 22012
CLIENT_ORDER_INDEXES = [1788454912, 1788456703, 1788458953]  # attempt2 (0%), attempt3 (27%), attempt4 (0%, 5% slip)


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    params = {"account_index": ACCOUNT_INDEX, "client_order_indexes": ",".join(str(x) for x in CLIENT_ORDER_INDEXES)}
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/accountOrders", params=params, timeout=20)
    result["url"] = r.url
    result["status_code"] = r.status_code
    try:
        result["body"] = r.json()
    except Exception as e:  # noqa: BLE001
        result["body_text"] = r.text[:3000]
        result["parse_error"] = str(e)
    print(f"[order_status_probe] status={r.status_code}")
    print(json.dumps(result.get("body", result.get("body_text")), indent=2, default=str, ensure_ascii=False)[:4000])

    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[order_status_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
