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
    USDG,
    USDG_DECIMALS,
    WETH,
    WETH_DECIMALS,
)
from task5_bot_pool_state import PoolRegistry, V3PoolState


@dataclass
class Opportunity:
    detected_at_wall: float
    detection_sequence_number: int
    pool_a: str
    pool_b: str
    exit_token: str  # WETH или USDG -- в каком токене меряется профит (для контракта, см. executor.py)
    expected_capture_usd: float
    expected_capture_after_gas_and_reverts_usd: float
    catalyst_sequence_number: int | None  # какое сообщение фида вызвало расхождение, если известно
    divergence_age_blocks: int  # сколько блоков расхождение уже существует (для фильтра ">=1")
    forced_min_profit_wei: int | None = None  # ТОЛЬКО для task5_bot_intentional_loss_test.py --
    # заведомо недостижимый minProfit для проверки revert-on-loss, None в обычной работе (contract requires 0)
    # --- Путь А, минимальный (владелец, 2026-09-12, "добавка") -- ТОЛЬКО для
    # телеметрии/логов ("роутер, функция, пул, сумма, направление"), не влияют
    # на исполнение цикла (poolA/poolB/exit_token/minProfit -- те же поля, что
    # и для detector-based Opportunity выше). trigger_kind различает происхождение
    # записи в attempts.jsonl без изменения формата остальных полей.
    trigger_kind: str = "divergence"  # "divergence" (Swap-событие/цена в реестре) | "router_calldata" (Путь А)
    router_to: str | None = None
    router_function_label: str | None = None
    touched_pool: str | None = None
    touched_zero_for_one: bool | None = None
    touched_amount_in_human: float | None = None
    touched_amount_in_usd_approx: float | None = None
    price_impact_fraction_approx: float | None = None  # владелец, 2026-09-12, "усиление": amountIn/liquidity


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
    exit_token: str,
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
        exit_token=exit_token,
        expected_capture_usd=expected_capture_usd,
        expected_capture_after_gas_and_reverts_usd=expected_after_reverts,
        catalyst_sequence_number=trigger_sequence_number,
        divergence_age_blocks=divergence_age_blocks,
    )


def current_weth_usd_price(registry: PoolRegistry) -> float | None:
    """Живая (не архивная) цена ETH/USDG -- первый известный пул пары
    WETH/USDG с уже заполненным `sqrt_price_x96` (bootstrap/фид). Владелец,
    2026-09-12, 'добавка' (Путь А): нужна для $-оценки amountIn ИЗ CALLDATA
    РОУТЕРА ДО исполнения -- в отличие от `task5_bot_router_histogram.py`
    (офлайн-анализ, там цена -- архивная константа с явной пометкой), здесь
    бот уже держит реальную текущую цену в памяти (тот же принцип, что и
    остальной горячий путь -- без сети)."""
    for pool in registry.pools_for_pair(WETH, USDG):
        price = _normalized_price(pool, WETH_DECIMALS, USDG_DECIMALS)
        if price is not None and price > 0:
            return price
    return None


def _price_amount_via_pool(pool: V3PoolState, token_in: str, amount_in_raw: int,
                            eth_usd_price: float | None) -> float | None:
    """Владелец, 2026-09-12 ('усиление'): "Оценка по пулу, а не по tokenIn --
    пул найден -> его цена из slot0 известна -> конвертировать amountIn в $
    через неё, независимо от направления. Это даёт все 233." -- в отличие от
    прежней версии (которая оценивала в $ ТОЛЬКО если сам `token_in` --
    буквально WETH/USDG), здесь используется РЕАЛЬНАЯ подразумеваемая цена
    ЗАТРОНУТОГО пула (`implied_price_token1_per_token0()`, raw-соотношение
    token1/token0 БЕЗ поправки на decimals -- поправка не нужна, она
    одинаково входит и выходит при переводе через одну и ту же пару) --
    конвертирует `amountIn` в RAW-эквивалент ПРОТИВОПОЛОЖНОЙ стороны пула,
    затем ищет, какая из двух сторон пула (token0 ИЛИ token1) -- WETH/USDG,
    и переводит именно её RAW-количество в $. Честно возвращает None, если
    НИ ОДНА сторона пула не WETH/USDG (курс не известен без внешней цены)."""
    raw_ratio = pool.implied_price_token1_per_token0()  # token1_raw / token0_raw
    if raw_ratio is None or raw_ratio <= 0:
        return None
    token_in_l = token_in.lower()
    if token_in_l == pool.token0.lower():
        amount_token0_raw = amount_in_raw
        amount_token1_raw = amount_in_raw * raw_ratio
    elif token_in_l == pool.token1.lower():
        amount_token1_raw = amount_in_raw
        amount_token0_raw = amount_in_raw / raw_ratio
    else:
        return None  # честно: tokenIn calldata не совпадает ни с одной стороной ЭТОГО пула

    t0, t1 = pool.token0.lower(), pool.token1.lower()
    if t0 == WETH.lower() and eth_usd_price is not None:
        return amount_token0_raw / (10 ** WETH_DECIMALS) * eth_usd_price
    if t0 == USDG.lower():
        return amount_token0_raw / (10 ** USDG_DECIMALS)
    if t1 == WETH.lower() and eth_usd_price is not None:
        return amount_token1_raw / (10 ** WETH_DECIMALS) * eth_usd_price
    if t1 == USDG.lower():
        return amount_token1_raw / (10 ** USDG_DECIMALS)
    return None  # честно: ни одна сторона пула -- не WETH/USDG, курс не известен, не гадаем


def check_router_triggered_opportunity(
    registry: PoolRegistry,
    touched_pool: V3PoolState,
    token_in: str,
    token_out: str,
    amount_in_raw: int,
    trigger_sequence_number: int,
    min_price_impact_fraction: float,
    assumed_gas_cost_usd: float,
    exit_token: str,
    router_to: str,
    router_function_label: str | None,
) -> Opportunity | None:
    """Задача 5, минимальный Путь А (владелец, 2026-09-12, 'усиление').
    "Триггер -- по ожидаемому сдвигу цены, не по размеру в долларах. Считать
    из amountIn и liquidity: сдвиг = amountIn/liquidity, порог >= 0.3%. Это
    убирает произвольные $2000 и ловит тонкие пулы, где двигают малые суммы
    -- именно там и живёт наша ниша. Точная формула не нужна -- грубой
    оценки достаточно, контракт всё равно проверит сам."

    В отличие от `check_pair_for_divergence` (требует УЖЕ измеримое
    расхождение цен), этот триггер срабатывает на ОЖИДАЕМОМ СДВИГЕ цены ДО
    исполнения -- поэтому здесь НЕТ проверки `rel_divergence > 0`/
    `divergence_age_blocks` (физически не измеримы для события, которое ещё
    не исполнилось). Безопасность капитала обеспечивает КОНТРАКТ (revert при
    недостижимом minProfit), не эта проверка.

    ЧЕСТНАЯ ОГОВОРКА про формулу: `amountIn / liquidity` -- НЕ настоящая
    формула сдвига цены Uniswap V3 (та требует tick-by-tick интеграции по
    ликвидности в диапазоне, не сделана в этой сессии) -- грубый, безразмерный
    прокси, намеренно принятый владельцем взамен точности."""
    if len(registry.pools_for_pair(token_in, token_out)) < 2:
        return None  # нет второй ноги цикла для этой пары в нашей вселенной -- нечего строить

    if touched_pool.liquidity is None or touched_pool.liquidity <= 0:
        return None  # честно: без liquidity сдвиг не оценить, не гадаем

    price_impact_fraction = amount_in_raw / touched_pool.liquidity
    if price_impact_fraction < min_price_impact_fraction:
        return None

    pools = registry.pools_for_pair(token_in, token_out)
    priced = [(p, _normalized_price(p, WETH_DECIMALS, USDG_DECIMALS)) for p in pools]
    priced = [(p, pr) for p, pr in priced if pr is not None]
    if len(priced) < 2:
        return None
    priced.sort(key=lambda x: x[1])
    cheap_pool, _ = priced[0]
    expensive_pool, _ = priced[-1]
    if cheap_pool.address.lower() == expensive_pool.address.lower():
        return None

    token_in_l = token_in.lower()
    zero_for_one = token_in_l == touched_pool.token0.lower()
    eth_usd_price = current_weth_usd_price(registry)
    amount_in_usd_approx = _price_amount_via_pool(touched_pool, token_in, amount_in_raw, eth_usd_price)
    if token_in_l == WETH.lower():
        amount_in_human = amount_in_raw / (10 ** WETH_DECIMALS)
    elif token_in_l == USDG.lower():
        amount_in_human = amount_in_raw / (10 ** USDG_DECIMALS)
    else:
        amount_in_human = None  # честно: decimals токена вне WETH/USDG здесь не известны, не гадаем

    # Владелец: "точность не нужна" -- НЕ моделируем итоговый профит по
    # размеру сдвига (нет симулятора price-impact в этой сессии), контракт
    # сам откатит цикл целиком, если реальной прибыли не наберётся до
    # ENTRY_THRESHOLD_USD (тот же пол, что и divergence-путь, не ноль).
    return Opportunity(
        detected_at_wall=time.time(),
        detection_sequence_number=trigger_sequence_number,
        pool_a=cheap_pool.address,
        pool_b=expensive_pool.address,
        exit_token=exit_token,
        expected_capture_usd=amount_in_usd_approx or 0.0,  # информационно, может быть 0.0 если не оценено
        expected_capture_after_gas_and_reverts_usd=ENTRY_THRESHOLD_USD,  # пол minProfit -- контракт проверит реальность
        catalyst_sequence_number=trigger_sequence_number,
        divergence_age_blocks=0,  # не применимо к этому триггеру -- расхождение ещё не исполнилось
        trigger_kind="router_calldata",
        router_to=router_to,
        router_function_label=router_function_label,
        touched_pool=touched_pool.address,
        touched_zero_for_one=zero_for_one,
        touched_amount_in_human=amount_in_human,
        touched_amount_in_usd_approx=amount_in_usd_approx,
        price_impact_fraction_approx=price_impact_fraction,
    )
