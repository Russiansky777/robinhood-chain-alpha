#!/usr/bin/env python3
"""Задача 5, минимальный Путь А -- честное объяснение результата dry-run
(владелец, 2026-09-12, п.5: "если ноль -- гистограмма должна объяснить,
почему"). ОФЛАЙН по данным (использует УЖЕ захваченный дамп
`data/task5_bot_router_capture/dump.jsonl.gz`, БЕЗ нового подключения к
фиду/relay), но требует ОДИН реальный RPC-вызов на старте --
`bootstrap_registry_from_rpc()` -- чтобы прогнать ТОЧНО ТУ ЖЕ детекцию
(`check_router_triggered_opportunity`), что и живой бот, против уже
реального универсума пулов (не против урезанного статического списка).

Отвечает на вопрос: сколько РЕАЛЬНО декодированных свопов из захвата
прошли бы триггер (пул из универсума + amountIn >= порога), и если
немного/ноль -- показывает распределение $-размеров, чтобы честно
объяснить, а не предположить, почему порог редко/никогда не пробивается
за короткое окно."""
from __future__ import annotations

import gzip
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import PATH_A_MIN_NOTIONAL_USD, RPC_URL_MAINNET, USDG, WETH
from task5_bot_detector import check_router_triggered_opportunity, current_weth_usd_price
from task5_bot_pool_state import bootstrap_registry_from_rpc
from task5_bot_router_decode import KNOWN_SELF_TRADE_ADDRESSES, KNOWN_SWAP_SELECTORS, decode_calldata

DEFAULT_DUMP = "data/task5_bot_router_capture/dump.jsonl.gz"


def run(dump_path: str, min_notional_usd: float = PATH_A_MIN_NOTIONAL_USD) -> dict:
    print("[backtest] bootstrap реестра через РЕАЛЬНЫЙ RPC (единственный сетевой вызов, не фид)...")
    registry = bootstrap_registry_from_rpc(rpc_url=RPC_URL_MAINNET)
    print(f"[backtest] пулов в реестре: {len(registry.by_address)}")
    eth_price = current_weth_usd_price(registry)
    print(f"[backtest] текущая цена WETH/USDG в реестре: {eth_price}")

    # Диагностика: ЧЕСТНО -- если current_weth_usd_price() вернул None,
    # нужно увидеть ПОЧЕМУ (нет пулов пары в реестре вообще, или пулы есть,
    # но sqrt_price_x96 не заполнен) -- не гадаем, смотрим напрямую.
    weth_usdg_pools = registry.pools_for_pair(WETH, USDG)
    weth_usdg_debug = [
        {"address": p.address, "fee": p.fee, "sqrt_price_x96": p.sqrt_price_x96, "liquidity": p.liquidity}
        for p in weth_usdg_pools
    ]
    print(f"[backtest] пулов WETH/USDG в реестре: {len(weth_usdg_pools)}: {weth_usdg_debug}")

    n_rows = 0
    n_selector_match = 0
    n_intents_decoded_full = 0  # token_in/token_out/fee/amount_in все известны
    n_priced = 0  # token_in ∈ {WETH,USDG} -- умеем оценить в $
    n_pool_found = 0  # (token_in,token_out,fee) нашёлся в НАШЕМ реестре
    n_pool_found_and_priced = 0
    n_pair_has_2plus_pools = 0
    n_opportunities = 0
    usd_buckets = Counter()  # для priced-and-pool-found случаев
    opportunities_sample = []
    pool_found_sample = []  # какие ПАРЫ реально совпали с реестром (не обязательно priced)
    priced_sample = []  # какие priced-input свопы НЕ нашли пул (для честного объяснения 0 пересечения)

    def bucket(usd: float) -> str:
        if usd >= 2000:
            return ">=2000"
        if usd >= 500:
            return "500-2000"
        if usd >= 100:
            return "100-500"
        if usd >= 10:
            return "10-100"
        return "<10"

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
                pool_found = touched_pool is not None
                if pool_found:
                    n_pool_found += 1
                    if len(pool_found_sample) < 15:
                        pool_found_sample.append({"token_in": intent.token_in, "token_out": intent.token_out,
                                                   "fee": intent.fee, "pool": touched_pool.address})

                token_in_l = intent.token_in.lower()
                priced_usd = None
                if token_in_l == WETH.lower() and eth_price is not None:
                    priced_usd = intent.amount_in / 1e18 * eth_price
                elif token_in_l == USDG.lower():
                    priced_usd = intent.amount_in / 1e6
                if priced_usd is not None:
                    n_priced += 1
                    if not pool_found and len(priced_sample) < 15:
                        priced_sample.append({"token_in": intent.token_in, "token_out": intent.token_out,
                                               "fee": intent.fee, "amount_usd_approx": round(priced_usd, 2)})
                    if pool_found:
                        n_pool_found_and_priced += 1
                        usd_buckets[bucket(priced_usd)] += 1
                        if len(registry.pools_for_pair(intent.token_in, intent.token_out)) >= 2:
                            n_pair_has_2plus_pools += 1

                if not pool_found:
                    continue
                opp = check_router_triggered_opportunity(
                    registry, touched_pool, intent.token_in, intent.token_out, intent.amount_in,
                    trigger_sequence_number=row.get("sequence_number", 0),
                    min_notional_usd=min_notional_usd,
                    assumed_gas_cost_usd=0.3,
                    exit_token=WETH,
                    router_to=to_addr,
                    router_function_label=KNOWN_SWAP_SELECTORS.get(selector),
                )
                if opp is not None:
                    n_opportunities += 1
                    if len(opportunities_sample) < 20:
                        opportunities_sample.append({
                            "router": opp.router_to, "function": opp.router_function_label,
                            "pool": opp.touched_pool, "zeroForOne": opp.touched_zero_for_one,
                            "amount_in_human": opp.touched_amount_in_human,
                            "amount_in_usd_approx": opp.touched_amount_in_usd_approx,
                        })

    return {
        "dump_path": dump_path,
        "n_pools_in_registry": len(registry.by_address),
        "current_weth_usd_price": eth_price,
        "min_notional_usd": min_notional_usd,
        "n_rows_total": n_rows,
        "n_selector_match": n_selector_match,
        "n_intents_decoded_full": n_intents_decoded_full,
        "n_pool_found_in_registry": n_pool_found,
        "n_priced_weth_or_usdg_input": n_priced,
        "n_pool_found_and_priced": n_pool_found_and_priced,
        "n_pool_found_and_priced_pair_has_2plus_pools": n_pair_has_2plus_pools,
        "usd_size_distribution_of_pool_found_and_priced": dict(usd_buckets),
        "n_opportunities_would_have_fired": n_opportunities,
        "opportunities_sample": opportunities_sample,
        "weth_usdg_pools_in_registry_debug": weth_usdg_debug,
        "pool_found_sample": pool_found_sample,
        "priced_input_but_no_pool_match_sample": priced_sample,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", type=str, default=DEFAULT_DUMP)
    ap.add_argument("--out", type=str, default="data/task5_bot_router_backtest_result.json")
    args = ap.parse_args()

    if not Path(args.dump).exists():
        print(f"[backtest] ЧЕСТНО: дамп {args.dump} не найден.", file=sys.stderr)
        raise SystemExit(1)

    result = run(args.dump)
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text)
    print(f"[backtest] результат записан в {args.out}")
