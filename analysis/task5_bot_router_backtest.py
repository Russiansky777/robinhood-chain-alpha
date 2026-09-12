#!/usr/bin/env python3
"""Задача 5, минимальный Путь А -- офлайн-сравнение ДВУХ версий триггера
(владелец, 2026-09-12, "усиление", п.3): "оценка по пулу с порогом $2000"
против "триггер по сдвигу цены >= 0.3%" -- на уже захваченном дампе
(`data/task5_bot_router_capture/dump.jsonl.gz`) и реестре пулов.

Владелец, п.4: "429 -- тот же урок, что с фидом: bootstrap один раз,
кешировать реестр на диск, не перезапрашивать RPC каждый прогон." --
по умолчанию скрипт грузит реестр из кеша (`--use-cache`, файл
`POOL_REGISTRY_CACHE_PATH`), и делает РЕАЛЬНЫЙ RPC bootstrap ТОЛЬКО если
кеша ещё нет или явно передан `--refresh-cache` -- не бьёт RPC на
каждый прогон, как это дважды сделал прошлый раунд (2x 429 подряд)."""
from __future__ import annotations

import gzip
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import (
    PATH_A_MIN_NOTIONAL_USD,
    PATH_A_MIN_PRICE_IMPACT_FRACTION,
    POOL_REGISTRY_CACHE_PATH,
    RPC_URL_MAINNET,
    USDG,
    WETH,
)
from task5_bot_detector import (
    _price_amount_via_pool,
    check_router_triggered_opportunity,
    current_weth_usd_price,
)
from task5_bot_pool_state import (
    bootstrap_registry_from_rpc,
    load_registry_cache,
    populate_initial_prices,
    save_registry_cache,
)
from task5_bot_router_decode import KNOWN_SELF_TRADE_ADDRESSES, KNOWN_SWAP_SELECTORS, decode_calldata

DEFAULT_DUMP = "data/task5_bot_router_capture/dump.jsonl.gz"


def _find_touched_pools(dump_path: str, registry) -> list:
    """Владелец, 2026-09-12 (реальная находка после 2 прогонов): free tier
    Alchemy физически не успевает оценить ВСЕ ~1140 пулов (CU/s потолок,
    ~50% в лучшем случае) -- для офлайн-анализа КОНКРЕТНОГО дампа нужны
    цены ТОЛЬКО тех пулов, что реально в нём затронуты (на порядок меньше,
    233 совпадения на ~1140 пулов). Один проход по дампу, БЕЗ сети (только
    локальный decode_calldata + find_pool_by_tokens_fee)."""
    touched: dict[str, object] = {}
    with gzip.open(dump_path, "rt") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            to_addr = row["to"]
            if to_addr in KNOWN_SELF_TRADE_ADDRESSES:
                continue
            data_hex = row.get("data") or "0x"
            selector = ("0x" + data_hex[2:10]) if len(data_hex) >= 10 else None
            if selector not in KNOWN_SWAP_SELECTORS:
                continue
            for intent in decode_calldata(to_addr, data_hex):
                if intent.token_in is None or intent.token_out is None or intent.fee is None:
                    continue
                pool = registry.find_pool_by_tokens_fee(intent.token_in, intent.token_out, intent.fee)
                if pool is not None:
                    touched[pool.address.lower()] = pool
    return list(touched.values())


def get_registry(use_cache: bool, refresh_cache: bool, cache_path: str, dump_path: str):
    if refresh_cache or not use_cache or not Path(cache_path).exists():
        print(f"[backtest] bootstrap реестра через РЕАЛЬНЫЙ RPC "
              f"({'--refresh-cache' if refresh_cache else 'кеша нет'}), БЕЗ eager-оценки цен "
              f"(только PoolCreated-скан, один eth_getLogs)...")
        registry = bootstrap_registry_from_rpc(rpc_url=RPC_URL_MAINNET, populate_prices=False)
        touched_pools = _find_touched_pools(dump_path, registry)
        # WETH/USDG-референс нужен ВСЕГДА (current_weth_usd_price -- для конвертации
        # $-суммы через ЛЮБОЙ другой затронутый пул, см. _price_amount_via_pool) --
        # добавляем явно, даже если сам дамп не тронул именно эту пару напрямую.
        touched_addrs = {p.address.lower() for p in touched_pools}
        for p in registry.pools_for_pair(WETH, USDG):
            if p.address.lower() not in touched_addrs:
                touched_pools.append(p)
                touched_addrs.add(p.address.lower())
        print(f"[backtest] реально затронутых пулов в дампе (+ WETH/USDG референс): {len(touched_pools)} из "
              f"{len(registry.by_address)} (оцениваем цену ТОЛЬКО их -- на порядок меньше запросов)")
        price_stats = populate_initial_prices(registry, rpc_url=RPC_URL_MAINNET, pools_subset=touched_pools)
        print(f"[backtest] populate_initial_prices (subset): {price_stats['n_ok']}/{price_stats['n_pools']} "
              f"успешно, {price_stats['n_error']} ошибок, примеры: {price_stats['errors_sample']}", file=sys.stderr)
        save_registry_cache(registry, cache_path)
        print(f"[backtest] реестр сохранён в кеш: {cache_path}")
    else:
        print(f"[backtest] реестр загружен ИЗ КЕША (офлайн, RPC не вызывался): {cache_path}")
        registry = load_registry_cache(cache_path)
    return registry


def run(dump_path: str, registry, min_notional_usd: float, min_price_impact_fraction: float) -> dict:
    eth_price = current_weth_usd_price(registry)
    print(f"[backtest] пулов в реестре: {len(registry.by_address)}, текущая цена WETH/USDG: {eth_price}")

    weth_usdg_pools = registry.pools_for_pair(WETH, USDG)
    weth_usdg_debug = [
        {"address": p.address, "fee": p.fee, "sqrt_price_x96": p.sqrt_price_x96, "liquidity": p.liquidity}
        for p in weth_usdg_pools
    ]

    n_rows = 0
    n_selector_match = 0
    n_intents_decoded_full = 0
    n_pool_found = 0
    n_priced_via_pool = 0  # НОВОЕ (владелец, "усиление"): цена через сам пул, не через tokenIn

    notional_hits = []  # версия "$2000 через цену пула"
    impact_hits = []  # версия "сдвиг >= 0.3%"

    with gzip.open(dump_path, "rt") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            n_rows += 1
            to_addr = row["to"]
            if to_addr in KNOWN_SELF_TRADE_ADDRESSES:
                continue
            data_hex = row.get("data") or "0x"
            selector = ("0x" + data_hex[2:10]) if len(data_hex) >= 10 else None
            if selector not in KNOWN_SWAP_SELECTORS:
                continue
            n_selector_match += 1
            for intent in decode_calldata(to_addr, data_hex):
                if None in (intent.token_in, intent.token_out, intent.fee, intent.amount_in):
                    continue
                n_intents_decoded_full += 1

                touched_pool = registry.find_pool_by_tokens_fee(intent.token_in, intent.token_out, intent.fee)
                if touched_pool is None:
                    continue
                n_pool_found += 1

                # --- Версия 1: оценка по пулу (не по tokenIn), порог $2000 ---
                usd_via_pool = _price_amount_via_pool(touched_pool, intent.token_in, intent.amount_in, eth_price)
                if usd_via_pool is not None:
                    n_priced_via_pool += 1
                    if usd_via_pool >= min_notional_usd:
                        notional_hits.append({
                            "sequence_number": row.get("sequence_number"), "router": to_addr,
                            "function": KNOWN_SWAP_SELECTORS.get(selector),
                            "pool": touched_pool.address, "token_in": intent.token_in,
                            "token_out": intent.token_out, "fee": intent.fee,
                            "amount_in_raw": intent.amount_in, "usd_via_pool_approx": round(usd_via_pool, 2),
                        })

                # --- Версия 2: триггер по сдвигу цены (amountIn/liquidity) >= 0.3% ---
                opp = check_router_triggered_opportunity(
                    registry, touched_pool, intent.token_in, intent.token_out, intent.amount_in,
                    trigger_sequence_number=row.get("sequence_number", 0),
                    min_price_impact_fraction=min_price_impact_fraction,
                    assumed_gas_cost_usd=0.3,
                    exit_token=WETH,
                    router_to=to_addr,
                    router_function_label=KNOWN_SWAP_SELECTORS.get(selector),
                )
                if opp is not None:
                    impact_hits.append({
                        "sequence_number": row.get("sequence_number"), "router": opp.router_to,
                        "function": opp.router_function_label, "pool": opp.touched_pool,
                        "token_in": intent.token_in, "token_out": intent.token_out, "fee": intent.fee,
                        "amount_in_raw": intent.amount_in,
                        "price_impact_fraction_approx": round(opp.price_impact_fraction_approx, 6),
                        "usd_via_pool_approx": (round(opp.touched_amount_in_usd_approx, 2)
                                                if opp.touched_amount_in_usd_approx is not None else None),
                    })

    notional_hits.sort(key=lambda r: -r["usd_via_pool_approx"])
    impact_hits.sort(key=lambda r: -r["price_impact_fraction_approx"])

    return {
        "dump_path": dump_path,
        "n_pools_in_registry": len(registry.by_address),
        "current_weth_usd_price": eth_price,
        "weth_usdg_pools_in_registry_debug": weth_usdg_debug,
        "min_notional_usd": min_notional_usd,
        "min_price_impact_fraction": min_price_impact_fraction,
        "n_rows_total": n_rows,
        "n_selector_match": n_selector_match,
        "n_intents_decoded_full": n_intents_decoded_full,
        "n_pool_found_in_registry": n_pool_found,
        "n_priced_via_pool": n_priced_via_pool,  # владелец: "это даёт все 233" -- проверяем реально
        "version1_notional_usd": {
            "n_hits": len(notional_hits),
            "top10": notional_hits[:10],
        },
        "version2_price_impact": {
            "n_hits": len(impact_hits),
            "top10": impact_hits[:10],
        },
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", type=str, default=DEFAULT_DUMP)
    ap.add_argument("--out", type=str, default="data/task5_bot_router_backtest_result.json")
    ap.add_argument("--cache", type=str, default=POOL_REGISTRY_CACHE_PATH)
    ap.add_argument("--refresh-cache", action="store_true",
                     help="Принудительно пересобрать реестр через RPC, даже если кеш уже есть.")
    ap.add_argument("--min-notional-usd", type=float, default=PATH_A_MIN_NOTIONAL_USD)
    ap.add_argument("--min-price-impact", type=float, default=PATH_A_MIN_PRICE_IMPACT_FRACTION)
    args = ap.parse_args()

    if not Path(args.dump).exists():
        print(f"[backtest] ЧЕСТНО: дамп {args.dump} не найден.", file=sys.stderr)
        raise SystemExit(1)

    registry = get_registry(use_cache=True, refresh_cache=args.refresh_cache, cache_path=args.cache,
                             dump_path=args.dump)
    result = run(args.dump, registry, args.min_notional_usd, args.min_price_impact)
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text)
    print(f"[backtest] результат записан в {args.out}")
