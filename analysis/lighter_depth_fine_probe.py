#!/usr/bin/env python3
"""Владелец, 2026-09-03: сравнить программный вызов (lighter-python SDK,
create_market_order = IOC с worst_price 1-2%) с ручным через фронтенд
(открылся сразу и полностью). Volume Quota исключена документацией
(Premium-only, rate-limit на отправку, не на филл) -- см.
lighter_volume_quota_probe_result.json.

Следующая гипотеза: IOC-ордер исполняет только то, что реально доступно
В ПРЕДЕЛАХ заданной worst_price -- если ликвидность у самого топа книги
(в пределах 1-2%) тоньше, чем грубый замер на фиксированных 0.5%
(p5_live_step0.py::fetch_eth_perp_depth), это полностью объясняет
частичные филлы БЕЗ квоты. Здесь -- мелкая сетка процентов (0.1%, 0.25%,
0.5%, 1%, 2%, 5%) на РЕАЛЬНОМ текущем стакане, публичный эндпоинт,
ключи не нужны, ордеров нет.

ВАЖНО: владелец только что вручную открыл шорт ETH через фронтенд --
этот скрипт НИЧЕГО не пишет, не трогает позиции, только читает
публичный orderBookOrders.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/lighter_depth_fine_probe_result.json")
LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
BANDS_PCT = [0.001, 0.0025, 0.005, 0.01, 0.02, 0.05]


def run() -> int:
    t0 = time.time()
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
    r.raise_for_status()
    markets = r.json().get("order_book_details", [])
    eth = next((m for m in markets if str(m.get("symbol", "")).upper() == "ETH"), None)
    if eth is None:
        print("[depth_fine_probe] ETH-рынок не найден")
        return 1
    mid = float(eth["mark_price"])

    rr = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookOrders", params={"market_id": eth["market_id"], "limit": 500}, timeout=20)
    rr.raise_for_status()
    body = rr.json()
    bids = sorted(body.get("bids", []), key=lambda o: float(o["price"]), reverse=True)
    asks = sorted(body.get("asks", []), key=lambda o: float(o["price"]))

    def depth_within(orders, pct, is_bid):
        bound = mid * (1 - pct) if is_bid else mid * (1 + pct)
        total_base, total_usd = 0.0, 0.0
        for o in orders:
            price = float(o["price"])
            size = float(o.get("remaining_base_amount", o.get("initial_base_amount", 0)))
            within = price >= bound if is_bid else price <= bound
            if within:
                total_base += size
                total_usd += price * size
        return total_base, total_usd

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mid_price": mid, "n_bids_total": len(bids), "n_asks_total": len(asks),
        "best_bid": float(bids[0]["price"]) if bids else None,
        "best_ask": float(asks[0]["price"]) if asks else None,
        "bands": {},
    }
    print(f"[depth_fine_probe] mid={mid} best_bid={result['best_bid']} best_ask={result['best_ask']} "
          f"n_bids={len(bids)} n_asks={len(asks)}")
    for pct in BANDS_PCT:
        bid_base, bid_usd = depth_within(bids, pct, True)
        ask_base, ask_usd = depth_within(asks, pct, False)
        result["bands"][f"{pct*100:.2f}%"] = {
            "bid_depth_eth": bid_base, "bid_depth_usd": bid_usd,
            "ask_depth_eth": ask_base, "ask_depth_usd": ask_usd,
        }
        print(f"[depth_fine_probe] +-{pct*100:.2f}%: bid={bid_base:.4f} ETH (${bid_usd:.0f}) "
              f"ask={ask_base:.4f} ETH (${ask_usd:.0f})")

    # Наши реальные размеры для сравнения
    result["our_orders"] = {
        "attempt2_size_eth": 0.03649432253555083, "attempt2_slippage_used": 0.01,
        "attempt3_size_eth": 0.03650316484173225, "attempt3_slippage_used": 0.01,
        "flatten_size_eth": 0.0100, "flatten_slippage_used": 0.02,
    }

    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[depth_fine_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
