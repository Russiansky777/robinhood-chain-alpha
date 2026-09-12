#!/usr/bin/env python3
"""Задача 5, $5-тест -- подготовка `CycleParams` для ОДНОЙ реальной пары
V3-пулов WETH/USDG, с `minProfit` заведомо больше любого возможного
захвата -- ожидаемый результат: `executeCycle()` реально проходит
flash-swap в pool A, встречный своп в pool B, гасит долг, и только
ПОСЛЕ этого откатывается целиком с `InsufficientProfit(uint256,uint256,
uint256)` (селектор `0xc39ba758`) на финальной проверке. Инвентарь не
теряется сверх газа неудачной попытки (см. `contracts/ClosedCycleExecutorV3.sol`).

Дай-раннер сегодня снова получил HTTP 403 на фиде (0 реальных детекций
за это подключение) -- пара пулов и их ТЕКУЩИЕ цены здесь взяты НЕ из
детекции, а из (а) уже задокументированных в `task5_bot_config.py`
реальных V3-пулов WETH/USDG (результат реального Dune-запроса
`task5_pool_map.py`, см. `TASK5_KNOWN_PROFITABLE_POOLS`/`WETH_USDG_POOL`)
и (б) СВЕЖЕГО `eth_call` slot0()/liquidity() на эти же адреса ПРЯМО
СЕЙЧАС через RPC -- цены реальные и текущие, только не пришли из
живого фида, а не выдуманы задним числом.

Только read-only RPC (`eth_call`) для чтения slot0/liquidity/decimals --
НИКАКОЙ подписи/отправки. Сборку и отправку транзакции делает владелец
своим скриптом, этот файл только пишет `data/task5_test_params.json`."""
from __future__ import annotations

import json
import os
import time

from eth_abi import encode

from task5_bot_config import (
    EXECUTOR_CONTRACT_ADDRESS_MAINNET,
    RPC_URL_MAINNET,
    USDG,
    USDG_DECIMALS,
    WETH,
    WETH_DECIMALS,
    WETH_USDG_POOL,
)
from task5_bot_pool_state import PoolRegistry, V3PoolState, _rpc_call, populate_initial_prices
from task5_bot_route_precompute import EXECUTE_CYCLE_SELECTOR, RouteSkeleton, fill_cycle_params

# Второй реальный WETH/USDG v3-пул (см. TASK5_KNOWN_PROFITABLE_POOLS
# в task5_bot_config.py, тот же реальный результат task5_pool_map.py).
POOL_B_ADDRESS = "0x69BFAF19C9F377BB306A89AED9F6B07E2C1A8D9A"

TARGET_NOTIONAL_USD = 5.0
# Заведомо недостижимый minProfit для гарантированного InsufficientProfit:
# 10x весь занятый нотионал (в exitToken) -- ни один реальный своп не
# может вернуть больше, чем на порядок больше самого одолженного тела.
MIN_PROFIT_MULTIPLE_OF_NOTIONAL = 10


def _normalized_price(pool: V3PoolState, token0_decimals: int, token1_decimals: int) -> float | None:
    raw = pool.implied_price_token1_per_token0()
    if raw is None:
        return None
    return raw * (10 ** (token0_decimals - token1_decimals))


def main() -> None:
    registry = PoolRegistry()
    pool_a = V3PoolState(address=WETH_USDG_POOL, token0=WETH, token1=USDG, fee=0)
    pool_b = V3PoolState(address=POOL_B_ADDRESS, token0=WETH, token1=USDG, fee=0)
    registry.register(pool_a)
    registry.register(pool_b)

    latest_block = int(_rpc_call("eth_blockNumber", [], rpc_url=RPC_URL_MAINNET), 16)
    stats = populate_initial_prices(registry, rpc_url=RPC_URL_MAINNET, block_number=latest_block)
    if stats["n_error"] > 0 or pool_a.sqrt_price_x96 is None or pool_b.sqrt_price_x96 is None:
        raise RuntimeError(f"не удалось прочитать реальные цены обоих пулов: {stats}")

    price_a = _normalized_price(pool_a, WETH_DECIMALS, USDG_DECIMALS)
    price_b = _normalized_price(pool_b, WETH_DECIMALS, USDG_DECIMALS)
    if price_a is None or price_b is None:
        raise RuntimeError("implied price None для одного из пулов -- нельзя выбрать cheap/expensive")

    cheap, expensive = (pool_a, pool_b) if price_a <= price_b else (pool_b, pool_a)
    cheap_price = min(price_a, price_b)

    # Реальная текущая цена WETH в USD -- из cheap-пула (тот же способ,
    # что _current_weth_usd_price в task5_bot_executor.py), НЕ выдумана.
    weth_usd_price = cheap_price if cheap.token0.lower() == WETH.lower() else (1.0 / cheap_price if cheap_price else 0.0)
    if weth_usd_price <= 0:
        raise RuntimeError("weth_usd_price <= 0 -- реальная цена не получена")

    notional_wei = int(TARGET_NOTIONAL_USD / weth_usd_price * 1e18)

    skeleton = RouteSkeleton(
        pool_a_address=cheap.address, pool_b_address=expensive.address,
        pool_a_token0=cheap.token0, pool_a_token1=cheap.token1,
        pool_b_token0=expensive.token0, pool_b_token1=expensive.token1,
        exit_token=WETH,  # та же заглушка, что task5_bot_run.py для этой пары
    )
    # Переиспользуем уже протестированную fill_cycle_params() (zeroForOneA,
    # sqrtPriceLimitA/B) -- expected_capture здесь номинальный (1 цент),
    # реальное значение НЕ важно: minProfit ниже принудительно
    # переопределяется заведомо недостижимым числом (см. докстринг модуля),
    # результат fill_cycle_params для minProfit не используется.
    cycle_params = fill_cycle_params(
        skeleton, cheap, expensive, notional_wei,
        expected_capture_after_gas_and_reverts_usd=0.01,
        price_usd_per_exit_token=weth_usd_price,
    )
    min_profit_forced = notional_wei * MIN_PROFIT_MULTIPLE_OF_NOTIONAL
    cycle_params["minProfit"] = min_profit_forced

    encoded = encode(
        ["(address,address,bool,int256,address,uint256,uint160,uint160)"],
        [(cycle_params["poolA"], cycle_params["poolB"], cycle_params["zeroForOneA"],
          cycle_params["amountSpecifiedA"], cycle_params["exitToken"], cycle_params["minProfit"],
          cycle_params["sqrtPriceLimitA"], cycle_params["sqrtPriceLimitB"])],
    )
    calldata = EXECUTE_CYCLE_SELECTOR + encoded.hex()

    result = {
        "test_type": "intentional_revert_insufficient_profit",
        "purpose": "minProfit заведомо больше любого возможного захвата -- ожидаем реальный revert "
                   "InsufficientProfit(uint256,uint256,uint256) ПОСЛЕ реального flash-swap+встречного "
                   "свопа+погашения долга (не ранний revert) -- проверяет полный путь исполнения "
                   "контракта без риска потери инвентаря сверх газа.",
        "contract_address": EXECUTOR_CONTRACT_ADDRESS_MAINNET,
        "execute_cycle_selector": EXECUTE_CYCLE_SELECTOR,
        "expected_revert": {"name": "InsufficientProfit", "selector": "0xc39ba758"},
        "cycle_params": cycle_params,
        "calldata_hex": calldata,
        "pool_a_cheap": {
            "address": cheap.address, "token0": cheap.token0, "token1": cheap.token1,
            "sqrt_price_x96": cheap.sqrt_price_x96, "liquidity": cheap.liquidity,
            "implied_price_weth_usd": weth_usd_price if cheap.token0.lower() == WETH.lower() else None,
        },
        "pool_b_expensive": {
            "address": expensive.address, "token0": expensive.token0, "token1": expensive.token1,
            "sqrt_price_x96": expensive.sqrt_price_x96, "liquidity": expensive.liquidity,
        },
        "notional_usd_target": TARGET_NOTIONAL_USD,
        "notional_wei": notional_wei,
        "weth_usd_price_used": weth_usd_price,
        "min_profit_multiple_of_notional": MIN_PROFIT_MULTIPLE_OF_NOTIONAL,
        "prices_checked_at_block": latest_block,
        "prices_checked_at_unix": time.time(),
        "rpc_url": RPC_URL_MAINNET,
        "notes": [
            "Пара пулов и токены -- реальные, уже задокументированные в task5_bot_config.py "
            "(TASK5_KNOWN_PROFITABLE_POOLS/WETH_USDG_POOL, результат реального Dune-запроса "
            "task5_pool_map.py), НЕ из детекции dry-run (тот прогон снова получил HTTP 403 на "
            "фиде, 0 детекций за это подключение).",
            "sqrt_price_x96/liquidity -- СВЕЖИЕ, прочитаны eth_call ПРЯМО СЕЙЧАС (prices_checked_at_block), "
            "не из старого кэша/детекции.",
            "Само исполнение (подпись/eth_sendRawTransaction) -- НЕ здесь, владелец собирает своим скриптом "
            "(contract_address + calldata_hex уже готовы, либо cycle_params для собственного ABI-энкодинга).",
        ],
    }

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "data", "task5_test_params.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
