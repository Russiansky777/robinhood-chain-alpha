#!/usr/bin/env python3
"""Один точечный реплей (владелец, текущий разбор InsufficientFunds для
route_7aebd805z0_19285e18z0_24107d15z1, 5 000 000 raw USDG, сигнал
62335119, блок estimateGas 62335187): "разрешён один точечный реплей
этого случая: Quoter и наш контракт на одном состоянии и с одним
размером".

Это read-only: НЕ отправка, НЕ форк, НЕ anvil -- ДВА прямых исторических
eth_call (котировка на 62335119 и на 62335187, тот же route+размер) +
ОДИН eth_estimateGas на 62335187 с РЕАЛЬНОЙ calldata (та же сборка, что
_evaluate_and_maybe_send), чтобы напрямую увидеть: (а) сохранился ли
знак прибыли между блоком исходного полного перебора (62335119) и
блоком, на котором реально был вызван estimateGas (62335187); (б)
воспроизводится ли РЕАЛЬНЫЙ revert 0x356680b7 на историческом состоянии
(не только по журналу).

Дополнительно (пункт 5 разбора): тот же route_id, ДРУГОЙ случай той же
сессии, где estimateGas РЕАЛЬНО прошёл, а итог отрицательный после газа
(лог: "профит после газа -0.044474 <= 0 на согласованный блок 62335094,
сигнал был на 62335060", тот же размер 5 000 000). ReasonLog не хранит
profit_before_gas/gas_estimate отдельными полями (только объединённое
число "после газа" в тексте detail) -- здесь их извлекаем НАПРЯМУЮ из
исторического состояния тем же методом, чтобы показать раздельно.

Контракт/Sender/очереди/бюджет НЕ меняются -- ничего не отправляется в
сеть, только eth_call/eth_estimateGas (read-only)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import task5_v4_hotpath as hp  # noqa: E402
from task5_v4_route_registry import load_registry_state  # noqa: E402

ROUTE_REGISTRY_STATE_FILE = os.environ.get(
    "ROUTE_REGISTRY_STATE_FILE", "/home/bot/data/task5_v4_route_registry_state.json")

CONTRACT_ADDRESS = "0xAB24907bceEF4EDC366a1E4DfA15ed8aA5fdfe71"
FROM_ADDRESS = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"

ROUTE_ID = "route_7aebd805z0_19285e18z0_24107d15z1"


def _quote_case(route, amount_in: int, block_number: int) -> dict:
    hp._reset_rpc_call_count()
    q = hp.quote_route_at_size(route, amount_in, block_number)
    q["_rpc_calls"] = hp._read_rpc_call_count()
    q["_block"] = block_number
    q["_amount_in"] = amount_in
    return q


def _full_pipeline_at(route, amount_in: int, block_number: int) -> dict:
    """Тот же порядок вызовов, что _evaluate_and_maybe_send, но с ФИКСИРОВАННЫМ
    (не пересчитанным) размером -- воспроизводим ИМЕННО состояние на block_number,
    не давая коду самому заново выбрать latest."""
    out: dict = {"block_number": block_number, "amount_in": amount_in}
    quote = hp.quote_route_at_size(route, amount_in, block_number)
    out["quote"] = quote
    if not quote["ok"]:
        return out
    exit_decimals = 6 if route.exit_token.lower() == hp.USDG.lower() else 18
    out["profit_before_gas"] = quote["profit_raw"] / 10**exit_decimals

    first_amount_specified = -amount_in
    calldata = hp.build_execute_cycle_calldata(route, first_amount_specified, min_profit=hp.MIN_PROFIT_FLOOR_RAW)
    out["calldata_hex"] = "0x" + calldata.hex()

    gas_res = hp.estimate_gas(CONTRACT_ADDRESS, calldata, FROM_ADDRESS, block_number=block_number)
    out["estimate_gas"] = gas_res
    if not gas_res["ok"]:
        return out

    try:
        gas_price = int(hp.rpc_call_trading_path("eth_gasPrice", []), 16)
    except Exception as exc:  # noqa: BLE001
        out["gas_price_error"] = str(exc)
        return out
    weth_usdg_price = hp.current_weth_usdg_price()
    out["gas_price_wei"] = gas_price
    out["weth_usdg_price"] = weth_usdg_price
    gas_cost_eth = gas_res["gas_estimate"] * gas_price / 1e18
    out["gas_cost_eth"] = gas_cost_eth
    if route.exit_token.lower() == hp.NATIVE:
        out["profit_after_gas"] = out["profit_before_gas"] - gas_cost_eth
    elif weth_usdg_price is not None:
        out["profit_after_gas"] = out["profit_before_gas"] - gas_cost_eth * weth_usdg_price
    else:
        out["profit_after_gas"] = None
        out["profit_after_gas_error"] = "нет живой цены WETH/USDG"
    return out


def main() -> None:
    result: dict = {"route_id": ROUTE_ID}

    loaded = load_registry_state(ROUTE_REGISTRY_STATE_FILE)
    if loaded is None:
        result["registry_load_error"] = f"не удалось прочитать {ROUTE_REGISTRY_STATE_FILE}"
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return
    registry, cursor_block = loaded
    result["registry_cursor_block"] = cursor_block

    route = registry.routes.get(ROUTE_ID)
    if route is None:
        result["route_found_in_saved_registry"] = False
        result["note"] = ("маршрут отсутствует в СОХРАНЁННОМ (финальном) состоянии реестра -- "
                           "повторная сборка route/legs из журнала здесь НЕ делается (риск ошибки "
                           "в PoolKey/hooks); без него нельзя выполнить реальный eth_call")
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return
    result["route_found_in_saved_registry"] = True
    result["route_label"] = route.label
    result["route_legs"] = [
        {"pool_id": leg.pool_id_hex, "zero_for_one": leg.zero_for_one, "hooks": leg.hooks,
         "fee": leg.fee, "tick_spacing": leg.tick_spacing}
        for leg in route.legs
    ]

    # --- Случай A: сигнал 62335119 -> quote_block 62335187 (0x356680b7) ---
    caseA: dict = {"size_in_raw": 5_000_000, "signal_block": 62335119, "estimate_gas_block_in_log": 62335187}
    try:
        recompute_at_signal = hp.recompute_route(route, 62335119)
        caseA["recompute_route_at_signal_block"] = recompute_at_signal
    except Exception as exc:  # noqa: BLE001
        caseA["recompute_route_at_signal_block_error"] = str(exc)

    try:
        caseA["quote_at_signal_block_fixed_5m"] = _quote_case(route, 5_000_000, 62335119)
    except Exception as exc:  # noqa: BLE001
        caseA["quote_at_signal_block_fixed_5m_error"] = str(exc)

    try:
        caseA["quote_at_estimategas_block_fixed_5m"] = _quote_case(route, 5_000_000, 62335187)
    except Exception as exc:  # noqa: BLE001
        caseA["quote_at_estimategas_block_fixed_5m_error"] = str(exc)

    try:
        caseA["estimate_gas_replay_at_62335187"] = _full_pipeline_at(route, 5_000_000, 62335187)
    except Exception as exc:  # noqa: BLE001
        caseA["estimate_gas_replay_at_62335187_error"] = str(exc)

    result["case_A_insufficientfunds"] = caseA

    # --- Случай B (пункт 5): тот же route_id, тот же размер, сигнал 62335060,
    # согласованный блок 62335094, estimateGas реально ПРОШЁЛ, итог после газа
    # отрицательный (-0.044474 по журналу) -- восстановить profit_before_gas и
    # gas_estimate ОТДЕЛЬНО, напрямую из исторического состояния. ---
    caseB: dict = {"size_in_raw": 5_000_000, "signal_block": 62335060, "estimate_gas_block_in_log": 62335094,
                   "logged_profit_after_gas": -0.044474}
    try:
        caseB["full_pipeline_at_62335094"] = _full_pipeline_at(route, 5_000_000, 62335094)
    except Exception as exc:  # noqa: BLE001
        caseB["full_pipeline_at_62335094_error"] = str(exc)

    result["case_B_positive_before_gas_negative_after"] = caseB

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_item4_two_block_quote_compare_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[item4_compare] сохранено: {out_path}")


if __name__ == "__main__":
    main()
