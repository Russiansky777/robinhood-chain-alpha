#!/usr/bin/env python3
"""Владелец, 2026-09-04: "витрина GeckoTerminal отдаёт TVL $8.96M и объём
24ч $79.13M (144.87K транзакций), расхождение до 10x по объёму с ранее
записанными $823M (docs/P5_HEDGED_LP.md). Перечитать /pools/{addr} и
записать актуальные цифры с таймстемпом."

Реальный запрос к GeckoTerminal (не переиспользуем цифры, которые
владелец продиктовал в задаче -- перечитываем сами, свежим таймстемпом,
чтобы не выдавать чужое наблюдение за собственную проверку).

Слаг сети -- "robinhood" (НЕ "robinhood-chain" -- реальная проверка
списка сетей нужна была бы, но владелец уже дал верный слаг явно в этой
же задаче, используем его как факт, не изобретаем свой).

Только чтение (публичный REST, без ключа, без ордеров/транзакций).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/gt_pool_verify_probe_result.json")
GT_BASE = "https://api.geckoterminal.com/api/v2"
NETWORK_SLUG = "robinhood"
POOL_ADDR = "0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca"
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "robinhood-chain-alpha-p5/1.0"}


def get(url: str, params: dict | None = None) -> dict:
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    entry = {"url": r.url, "status_code": r.status_code}
    try:
        entry["body"] = r.json()
    except Exception as e:  # noqa: BLE001
        entry["body_text"] = r.text[:2000]
        entry["parse_error"] = str(e)
    return entry


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "network_slug": NETWORK_SLUG, "pool_address": POOL_ADDR}

    print(f"=== GET /networks/{NETWORK_SLUG}/pools/{POOL_ADDR} ===")
    pool = get(f"{GT_BASE}/networks/{NETWORK_SLUG}/pools/{POOL_ADDR}")
    result["pool_endpoint"] = pool
    print(f"[gt_pool_verify] status={pool['status_code']}")

    attrs = {}
    if pool.get("status_code") == 200:
        attrs = pool.get("body", {}).get("data", {}).get("attributes", {})
        summary = {
            "name": attrs.get("name"),
            "base_token_price_usd": attrs.get("base_token_price_usd"),
            "quote_token_price_usd": attrs.get("quote_token_price_usd"),
            "base_token_price_native_currency": attrs.get("base_token_price_native_currency"),
            "quote_token_price_native_currency": attrs.get("quote_token_price_native_currency"),
            "reserve_in_usd": attrs.get("reserve_in_usd"),
            "fdv_usd": attrs.get("fdv_usd"),
            "market_cap_usd": attrs.get("market_cap_usd"),
            "volume_usd": attrs.get("volume_usd"),
            "transactions": attrs.get("transactions"),
            "price_change_percentage": attrs.get("price_change_percentage"),
        }
        result["summary"] = summary
        print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
    else:
        print(f"[gt_pool_verify] тело: {pool.get('body', pool.get('body_text'))}")

    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[gt_pool_verify] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
