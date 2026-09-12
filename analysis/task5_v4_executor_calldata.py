#!/usr/bin/env python3
"""Задача 5, живой пилот -- ABI-калдата executeCycle(CycleParams) для
ClosedCycleExecutorV4, вызываемая горячим путём. Использует `eth_abi`
(уже зависимость проекта, тот же пакет, что deploy_executor.sh) вместо
ручного кодирования вложенных tuple[]/bytes -- меньше риска ошибки в
коде, который готовит калдату для РЕАЛЬНОЙ отправки.

Это НЕ логика подписи/отправки -- только сборка байт вызова функции
контракта (chartered в границы этой сессии, см. чат-подтверждение
владельца: "ты только вызываешь send_cycle"). Подпись/отправка --
исключительно task5_bot_sender.py::Sender.send_cycle(to, calldata, gas_limit)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import eth_utils
from eth_abi import encode

LEG_TUPLE_TYPE = "(address,address,uint24,int24,address,bool,uint160,bytes)"
CYCLE_PARAMS_TYPE = f"({LEG_TUPLE_TYPE}[],int256,address,uint256)"

EXECUTE_CYCLE_SELECTOR = eth_utils.function_signature_to_4byte_selector(
    f"executeCycle({CYCLE_PARAMS_TYPE})"
)

MIN_SQRT_PRICE = 4295128739
MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342


def default_sqrt_price_limit(zero_for_one: bool) -> int:
    """Максимально permissive лимит для данного направления -- см.
    Uniswap/v4-core/libraries/TickMath.sol (сверено WebFetch этой
    сессией). НЕ ноль -- вне допустимого диапазона, своп ревертнет."""
    return (MIN_SQRT_PRICE + 1) if zero_for_one else (MAX_SQRT_PRICE - 1)


def build_execute_cycle_calldata(route, first_amount_specified: int, min_profit: int,
                                  sqrt_price_limits: list[int] | None = None) -> bytes:
    """route -- RouteCycle (task5_v4_route_registry.py). sqrt_price_limits
    -- по одному на плечо; если не задано, берутся максимально permissive
    (default_sqrt_price_limit) -- та же логика, что во всех fork-тестах
    Этапа 2 этой сессии."""
    if sqrt_price_limits is None:
        sqrt_price_limits = [default_sqrt_price_limit(leg.zero_for_one) for leg in route.legs]
    if len(sqrt_price_limits) != len(route.legs):
        raise ValueError("sqrt_price_limits должен быть по одному на плечо")

    legs_tuples = []
    for leg, limit in zip(route.legs, sqrt_price_limits):
        legs_tuples.append((
            leg.currency0, leg.currency1, leg.fee, leg.tick_spacing, leg.hooks,
            leg.zero_for_one, limit, b"",
        ))
    cycle_params = (legs_tuples, first_amount_specified, route.exit_token, min_profit)
    encoded_args = encode([CYCLE_PARAMS_TYPE], [cycle_params])
    return EXECUTE_CYCLE_SELECTOR + encoded_args


if __name__ == "__main__":
    # Самопроверка структуры (без сети): селектор, длина, декодируемость обратно.
    from eth_abi import decode
    from task5_v4_route_registry import seed_routes

    routes = seed_routes()
    route = routes[0]  # USDG<->MOSIAI
    calldata = build_execute_cycle_calldata(route, -9437184, 0)
    print("selector:", EXECUTE_CYCLE_SELECTOR.hex())
    print("calldata len bytes:", len(calldata))
    decoded = decode([CYCLE_PARAMS_TYPE], calldata[4:])
    print("round-trip decode OK:", decoded)
    expected_legs = tuple(
        (leg.currency0, leg.currency1, leg.fee, leg.tick_spacing, leg.hooks,
         leg.zero_for_one, default_sqrt_price_limit(leg.zero_for_one), b"")
        for leg in route.legs
    )
    assert decoded[0][0] == expected_legs, (decoded[0][0], expected_legs)
    assert decoded[0][1] == -9437184
    assert decoded[0][2].lower() == route.exit_token.lower()
    assert decoded[0][3] == 0
    print("самопроверка структуры: OK")
