#!/usr/bin/env python3
"""Задача 5, Путь А -- офлайн-прогон на ЧИСТОМ УНИВЕРСУМЕ (владелец,
2026-09-12, после разбора пула `0x08d77885...`): "пул 0x08d77885... --
шиткоин без ликвидности, детекции на нём -- не вилки, а чужой объём в
пустом пуле. Универсум сужаем: только пулы из Dune-карты (45 с реальными
захватами) плюс WETH/USDG, и обязательно -- у пары есть второй пул с
liquidity > 0 в реестре. Пулы, где свопы идут от 1-2 адресов, исключать
как накрутку."

Три РЕАЛЬНЫХ, честных фильтра, применяются последовательно, каждый
логируется отдельно (какие пулы и почему выпали -- не молча):

1. **Только Dune-карта (`data/p3_guard_cache/task5_pool_map_result.json`,
   45 пулов, реальный источник -- см. `task5_pool_map.py`).** Наш
   `PoolRegistry`/`V3PoolState` поддерживает ТОЛЬКО архитектуру v3
   (адрес = сам контракт пула) -- 30 из 45 являются v4 (Uniswap v4
   singleton PoolManager, отдельный, несовместимый ABI, честный,
   известный gap, см. PROJECT_STATE.md) -- физически не могут участвовать
   в этом прогоне, не наш выбор их выкидывать, а архитектурное
   ограничение. Явный WETH/USDG (`WETH_USDG_POOL`) уже входит в эти 15
   v3-пулов Dune-карты (проверено, не задваивается).

2. **"У пары есть второй пул с liquidity > 0 в реестре."** Для каждого из
   15 Dune-v3 пулов ищем ВСЕ пулы той же пары (token0,token1, любой
   порядок, любой fee) во ВСЁМ реестре (1142 пула, не только Dune-карте).
   Если пар с таким же token0/token1 в реестре ровно 1 (сам этот пул) --
   исключаем: реального "второго независимого источника цены" для этой
   пары не существует физически, значит и сам критерий сравнения цен
   между пулами неприменим. Если есть >=2 -- нужно, чтобы ХОТЯ БЫ ОДИН
   имел `liquidity > 0` (не 0, не None) -- иначе как с `0x08d77885...`
   (единственный пул пары, liquidity=0) реального сравнения тоже нет.

3. **"Пулы, где свопы идут от 1-2 адресов -- исключать как накрутку."**
   Считаем МНОЖЕСТВО реальных РАЗЛИЧНЫХ адресов-отправителей (`from`,
   восстановлен ecrecover'ом по подписи уже отправленной публичной
   транзакции -- см. `_recover_sender` в `task5_bot_feed_client.py`, БЕЗ
   сети, НЕ подпись/отправка), которые реально коснулись пула в дампе
   (только среди decoded-намерений с `token_in/out/fee` -- не весь трафик
   на этот `to`). <= 2 различных отправителей -- исключаем как
   вероятную накрутку объёма (само-торговлю/боты одного оператора).
   Честно: если для какого-то касания `from` не восстановился (None,
   нестандартная транзакция) -- такое касание не увеличивает счётчик
   уникальных адресов ни в какую сторону, отдельно логируется
   `n_touches_with_unknown_sender`."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import (
    PATH_A_MIN_NOTIONAL_USD,
    PATH_A_MIN_PRICE_IMPACT_FRACTION,
    POOL_REGISTRY_CACHE_PATH,
    RPC_URL_MAINNET,
    WETH_USDG_POOL,
)
from task5_bot_detector import (
    _price_amount_via_pool,
    check_router_triggered_opportunity,
    current_weth_usd_price,
)
from task5_bot_pool_state import load_registry_cache, populate_initial_prices, save_registry_cache
from task5_bot_router_decode import KNOWN_SELF_TRADE_ADDRESSES, KNOWN_SWAP_SELECTORS, decode_calldata

DEFAULT_DUMP = "data/task5_bot_router_capture/dump.jsonl.gz"
DUNE_POOL_MAP_PATH = "data/p3_guard_cache/task5_pool_map_result.json"
MIN_DISTINCT_SENDERS = 3  # владелец: "1-2 адреса -- накрутка" -> нужно строго >= 3


def _pair_key(t0: str, t1: str) -> tuple[str, str]:
    return tuple(sorted([t0.lower(), t1.lower()]))


def build_clean_universe(registry, dune_map_path: str, price_missing: bool, rpc_url: str) -> dict:
    dune = json.loads(Path(dune_map_path).read_text())["pools"]
    dune_v3 = [p for p in dune if p.get("version") == "v3"]
    dune_v4_count = sum(1 for p in dune if p.get("version") == "v4")

    dune_v3_addrs = []
    for p in dune_v3:
        addr = p["pool_key"]
        if not addr.startswith("0x"):
            addr = "0x" + addr
        dune_v3_addrs.append(addr.lower())

    # --- фильтр 2: у пары есть >=2 пула в РЕЕСТРЕ, хотя бы у одного liquidity>0 ---
    by_pair: dict[tuple, list] = {}
    for p in registry.by_address.values():
        by_pair.setdefault(_pair_key(p.token0, p.token1), []).append(p)

    # какие Dune-v3 пулы требуют дозаливки цены (пара имеет >=2 пула в реестре,
    # но НИ У ОДНОГО пока нет данных liquidity) -- только эти, не вся вселенная
    need_pricing = []
    for addr in dune_v3_addrs:
        pool = registry.by_address.get(addr)
        if pool is None:
            continue
        siblings = by_pair.get(_pair_key(pool.token0, pool.token1), [])
        if len(siblings) >= 2 and all(s.liquidity in (None,) for s in siblings):
            need_pricing.extend(siblings)

    price_stats = None
    if price_missing and need_pricing:
        # де-дуп по адресу
        uniq = {p.address.lower(): p for p in need_pricing}
        price_stats = populate_initial_prices(registry, rpc_url=rpc_url, pools_subset=list(uniq.values()))
        # пересобрать by_pair -- liquidity могла обновиться
        by_pair = {}
        for p in registry.by_address.values():
            by_pair.setdefault(_pair_key(p.token0, p.token1), []).append(p)

    kept = []
    dropped_no_second_pool = []
    dropped_no_positive_liquidity = []
    for addr in dune_v3_addrs:
        pool = registry.by_address.get(addr)
        if pool is None:
            dropped_no_second_pool.append({"address": addr, "reason": "не найден в реестре вообще"})
            continue
        siblings = by_pair.get(_pair_key(pool.token0, pool.token1), [])
        others = [s for s in siblings if s.address.lower() != addr]
        if not others:
            dropped_no_second_pool.append({
                "address": addr, "token0": pool.token0, "token1": pool.token1, "fee": pool.fee,
                "reason": "единственный пул этой пары во всём реестре (1142 пула) -- второго нет",
            })
            continue
        any_positive_liquidity = any((s.liquidity or 0) > 0 for s in siblings)
        if not any_positive_liquidity:
            dropped_no_positive_liquidity.append({
                "address": addr, "token0": pool.token0, "token1": pool.token1, "fee": pool.fee,
                "siblings": [{"address": s.address, "liquidity": s.liquidity} for s in siblings],
                "reason": "второй пул пары есть, но ни у одного (включая этот) liquidity>0",
            })
            continue
        kept.append(pool)

    return {
        "dune_total_pools": len(dune),
        "dune_v3_pools": len(dune_v3),
        "dune_v4_pools_excluded_architecture": dune_v4_count,
        "weth_usdg_pool_in_dune_v3_list": WETH_USDG_POOL.lower() in dune_v3_addrs,
        "price_stats_for_missing_liquidity_check": price_stats,
        "kept_pools": kept,
        "dropped_no_second_pool_in_registry": dropped_no_second_pool,
        "dropped_no_positive_liquidity_anywhere": dropped_no_positive_liquidity,
    }


def _touches_by_pool(dump_path: str, registry, candidate_addrs: set[str]) -> dict[str, list[dict]]:
    """Один проход по дампу -- для КАЖДОГО кандидата собираем список
    затрагивающих его decoded-намерений вместе с `from` строки дампа
    (для подсчёта уникальных отправителей, фильтр 3)."""
    touches: dict[str, list[dict]] = {a: [] for a in candidate_addrs}
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
                if pool is None:
                    continue
                addr = pool.address.lower()
                if addr not in touches:
                    continue
                touches[addr].append({
                    "sequence_number": row.get("sequence_number"),
                    "from": row.get("from"),
                    "router": to_addr,
                    "selector": selector,
                    "intent": intent,
                })
    return touches


def run(dump_path: str, registry, kept_pools: list, min_notional_usd: float,
        min_price_impact_fraction: float) -> dict:
    eth_price = current_weth_usd_price(registry)
    candidate_addrs = {p.address.lower() for p in kept_pools}
    touches = _touches_by_pool(dump_path, registry, candidate_addrs)

    # --- фильтр 3: >=3 различных отправителей среди РЕАЛЬНО восстановленных ---
    wash_report = {}
    final_pools = []
    for pool in kept_pools:
        addr = pool.address.lower()
        rows = touches.get(addr, [])
        senders_known = {r["from"] for r in rows if r.get("from")}
        n_unknown = sum(1 for r in rows if not r.get("from"))
        wash_report[addr] = {
            "n_touches": len(rows),
            "n_distinct_senders_known": len(senders_known),
            "n_touches_with_unknown_sender": n_unknown,
            "senders": sorted(senders_known),
        }
        if len(senders_known) >= MIN_DISTINCT_SENDERS:
            final_pools.append(pool)

    dropped_wash = {a: v for a, v in wash_report.items()
                    if a not in {p.address.lower() for p in final_pools}}

    notional_hits = []
    impact_hits = []
    n_rows = 0
    for pool in final_pools:
        addr = pool.address.lower()
        for r in touches.get(addr, []):
            n_rows += 1
            intent = r["intent"]
            if intent.amount_in is None:
                continue
            usd_via_pool = _price_amount_via_pool(pool, intent.token_in, intent.amount_in, eth_price)
            if usd_via_pool is not None and usd_via_pool >= min_notional_usd:
                notional_hits.append({
                    "sequence_number": r["sequence_number"], "router": r["router"],
                    "function": KNOWN_SWAP_SELECTORS.get(r["selector"]), "pool": pool.address,
                    "from": r.get("from"), "token_in": intent.token_in, "token_out": intent.token_out,
                    "fee": intent.fee, "amount_in_raw": intent.amount_in,
                    "usd_via_pool_approx": round(usd_via_pool, 2),
                })
            opp = check_router_triggered_opportunity(
                registry, pool, intent.token_in, intent.token_out, intent.amount_in,
                trigger_sequence_number=r["sequence_number"] or 0,
                min_price_impact_fraction=min_price_impact_fraction,
                assumed_gas_cost_usd=0.3, exit_token=intent.token_out,
                router_to=r["router"], router_function_label=KNOWN_SWAP_SELECTORS.get(r["selector"]),
            )
            if opp is not None:
                impact_hits.append({
                    "sequence_number": r["sequence_number"], "router": r["router"],
                    "function": KNOWN_SWAP_SELECTORS.get(r["selector"]), "pool": pool.address,
                    "from": r.get("from"), "token_in": intent.token_in, "token_out": intent.token_out,
                    "fee": intent.fee, "amount_in_raw": intent.amount_in,
                    "price_impact_fraction_approx": round(opp.price_impact_fraction_approx, 6),
                })

    notional_hits.sort(key=lambda r: -r["usd_via_pool_approx"])
    impact_hits.sort(key=lambda r: -r["price_impact_fraction_approx"])

    return {
        "current_weth_usd_price": eth_price,
        "n_candidate_pools_before_wash_filter": len(kept_pools),
        "n_final_pools_after_wash_filter": len(final_pools),
        "final_pool_addresses": [p.address for p in final_pools],
        "wash_filter_report_per_pool": wash_report,
        "dropped_as_wash_trading": dropped_wash,
        "n_swap_touches_on_final_universe": n_rows,
        "version1_notional_usd": {"n_hits": len(notional_hits), "hits": notional_hits},
        "version2_price_impact": {"n_hits": len(impact_hits), "hits": impact_hits},
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", type=str, default=DEFAULT_DUMP)
    ap.add_argument("--out", type=str, default="data/task5_bot_router_backtest_clean_universe_result.json")
    ap.add_argument("--cache", type=str, default=POOL_REGISTRY_CACHE_PATH)
    ap.add_argument("--dune-map", type=str, default=DUNE_POOL_MAP_PATH)
    ap.add_argument("--price-missing", action="store_true",
                     help="Реально дозалить через RPC liquidity для Dune-v3 пулов, у которых пара имеет "
                          ">=2 пула в реестре, но ни один ещё не оценен (иначе -- честно исключить как "
                          "'нет данных', не гадать).")
    ap.add_argument("--min-notional-usd", type=float, default=PATH_A_MIN_NOTIONAL_USD)
    ap.add_argument("--min-price-impact", type=float, default=PATH_A_MIN_PRICE_IMPACT_FRACTION)
    args = ap.parse_args()

    if not Path(args.dump).exists():
        print(f"[clean_universe] ЧЕСТНО: дамп {args.dump} не найден.", file=sys.stderr)
        raise SystemExit(1)
    if not Path(args.cache).exists():
        print(f"[clean_universe] ЧЕСТНО: кеш реестра {args.cache} не найден -- нужен предыдущий bootstrap.",
              file=sys.stderr)
        raise SystemExit(1)

    registry = load_registry_cache(args.cache)
    universe = build_clean_universe(registry, args.dune_map, price_missing=args.price_missing,
                                     rpc_url=RPC_URL_MAINNET)
    print(f"[clean_universe] Dune-карта: {universe['dune_total_pools']} пулов всего "
          f"({universe['dune_v3_pools']} v3 -- поддерживаются, {universe['dune_v4_pools_excluded_architecture']} "
          f"v4 -- архитектурно не поддерживаются нашим PoolRegistry, честный gap)")
    print(f"[clean_universe] после фильтра 'второй пул с liquidity>0': "
          f"{len(universe['kept_pools'])} осталось, "
          f"{len(universe['dropped_no_second_pool_in_registry'])} выпали (нет второго пула), "
          f"{len(universe['dropped_no_positive_liquidity_anywhere'])} выпали (второй пул есть, liquidity нигде >0)")
    if universe["price_stats_for_missing_liquidity_check"]:
        print(f"[clean_universe] дозалив liquidity для проверки фильтра 2: "
              f"{universe['price_stats_for_missing_liquidity_check']}")
    save_registry_cache(registry, args.cache)

    if not universe["kept_pools"]:
        print("[clean_universe] ЧЕСТНО: после фильтра 2 не осталось ни одного пула -- дальше считать нечего.")
        result = {**universe, "kept_pools": [p.address for p in universe["kept_pools"]]}
        text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
        raise SystemExit(0)

    result = run(args.dump, registry, universe["kept_pools"], args.min_notional_usd, args.min_price_impact)
    result["universe_build"] = {
        "dune_total_pools": universe["dune_total_pools"],
        "dune_v3_pools": universe["dune_v3_pools"],
        "dune_v4_pools_excluded_architecture": universe["dune_v4_pools_excluded_architecture"],
        "dropped_no_second_pool_in_registry": universe["dropped_no_second_pool_in_registry"],
        "dropped_no_positive_liquidity_anywhere": universe["dropped_no_positive_liquidity_anywhere"],
    }
    print(f"[clean_universe] пулов до фильтра накрутки (>= {MIN_DISTINCT_SENDERS} отправителей): "
          f"{result['n_candidate_pools_before_wash_filter']}, после: {result['n_final_pools_after_wash_filter']}")
    print(f"[clean_universe] версия 1 (notional >= ${args.min_notional_usd}): {result['version1_notional_usd']['n_hits']} попаданий")
    print(f"[clean_universe] версия 2 (price-impact >= {args.min_price_impact}): {result['version2_price_impact']['n_hits']} попаданий")

    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text)
    print(f"[clean_universe] результат записан в {args.out}")
