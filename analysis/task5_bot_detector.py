#!/usr/bin/env python3
"""Задача 5, живой бот -- детекция расхождения между пулами одной пары
ПОСЛЕ каждого свопа, и фильтр входа. Владелец, 2026-09-11/12:
"пары и маршруты предрассчитаны" -- в этой версии предрасчитаны сами
токены (WETH/USDG) и порог, а конкретные пулы бот находит на старте
(bootstrap_registry_from_rpc), не пересчитывая маршруты в горячем пути
при каждом сообщении фида (маршрут между двумя пулами одной пары --
фиксированная структура, меняется только цена)."""
from __future__ import annotations

import time
from dataclasses import dataclass

from task5_bot_config import (
    ASSUMED_REVERT_RATE,
    ENTRY_THRESHOLD_USD,
    MIN_DIVERGENCE_AGE_BLOCKS,
    USDG_DECIMALS,
    WETH_DECIMALS,
)
from task5_bot_pool_state import PoolRegistry, V3PoolState


@dataclass
class Opportunity:
    detected_at_wall: float
    detection_sequence_number: int
    pool_a: str
    pool_b: str
    expected_capture_usd: float
    expected_capture_after_gas_and_reverts_usd: float
    catalyst_sequence_number: int | None  # какое сообщение фида вызвало расхождение, если известно
    divergence_age_blocks: int  # сколько блоков расхождение уже существует (для фильтра ">=1")


def _normalized_price(pool: V3PoolState, token0_decimals: int, token1_decimals: int) -> float | None:
    raw = pool.implied_price_token1_per_token0()
    if raw is None:
        return None
    return raw * (10 ** (token0_decimals - token1_decimals))


def check_pair_for_divergence(
    registry: PoolRegistry,
    token0: str,
    token1: str,
    token0_decimals: int,
    token1_decimals: int,
    trigger_sequence_number: int,
    assumed_gas_cost_usd: float,
) -> Opportunity | None:
    """Вызывается ПОСЛЕ применения каждого свопа к реестру -- сравнивает
    подразумеваемые цены всех известных пулов этой пары, без сети,
    только на данных, уже лежащих в памяти."""
    pools = registry.pools_for_pair(token0, token1)
    if len(pools) < 2:
        return None

    priced = []
    for p in pools:
        price = _normalized_price(p, token0_decimals, token1_decimals)
        if price is not None:
            priced.append((p, price))
    if len(priced) < 2:
        return None

    priced.sort(key=lambda x: x[1])
    cheap_pool, cheap_price = priced[0]
    expensive_pool, expensive_price = priced[-1]
    if cheap_price <= 0:
        return None

    rel_divergence = (expensive_price - cheap_price) / cheap_price
    if rel_divergence <= 0:
        return None

    # Владелец: "только там, где расхождение старше одного блока" --
    # бот НЕ участвует, если обе стороны были обновлены В ТОМ ЖЕ блоке,
    # что и триггер (т.е. расхождение возникло только что, distance=0).
    trigger_block = trigger_sequence_number  # sequenceNumber == block number на этом фиде (см. п.2 отчёта)
    age_a = trigger_block - (cheap_pool.last_update_block or trigger_block)
    age_b = trigger_block - (expensive_pool.last_update_block or trigger_block)
    divergence_age_blocks = min(age_a, age_b)
    if divergence_age_blocks < MIN_DIVERGENCE_AGE_BLOCKS:
        return None

    # Грубая оценка размера захвата -- в этой версии НЕ моделирует
    # глубину ликвидности точно (нужен полный tick-by-tick симулятор,
    # следующая итерация); использует относительное расхождение x
    # эвристический масштаб на основе liquidity как первое приближение.
    # ЧЕСТНО: это упрощение, будет калиброваться в течение недели 1
    # dry-run против реально наблюдаемых исходов.
    liquidity_proxy = min(cheap_pool.liquidity or 0, expensive_pool.liquidity or 0)
    notional_proxy_usd = min(liquidity_proxy / 1e18, 5000.0) if liquidity_proxy else 0.0
    expected_capture_usd = rel_divergence * notional_proxy_usd
    if expected_capture_usd <= 0:
        return None

    expected_after_reverts = expected_capture_usd * (1 - ASSUMED_REVERT_RATE) - assumed_gas_cost_usd
    if expected_after_reverts < ENTRY_THRESHOLD_USD:
        return None

    return Opportunity(
        detected_at_wall=time.time(),
        detection_sequence_number=trigger_sequence_number,
        pool_a=cheap_pool.address,
        pool_b=expensive_pool.address,
        expected_capture_usd=expected_capture_usd,
        expected_capture_after_gas_and_reverts_usd=expected_after_reverts,
        catalyst_sequence_number=trigger_sequence_number,
        divergence_age_blocks=divergence_age_blocks,
    )
