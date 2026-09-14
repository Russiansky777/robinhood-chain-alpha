#!/usr/bin/env python3
"""Разбор конкретной транзакции (владелец, измерение реакции конкурента --
БЕЗ условий "маршрут поддерживается нашим ботом" и "попадание в окно
пилота" -- это отдельная задача от предыдущих контрольных примеров).

Первый результат: статус/блок/позиция, полный маршрут, фактический
размер, движение средств исполнителя и получателя прибыли (отдельно газ
и выплаты хукам), подтверждение типа операции.

read-only. Переиспользует уже написанную full_fund_flow_check()
(task5_v4_item3_in_window_control_trade.py) для честного разбора
потоков (чистый Swap-уровневый поток по токенам + ВСЕ ERC20 Transfer
рецепта, включая получателей, отличных от tx.from) -- не копирует её
логику заново. Дополнительно -- полные параметры каждого пула маршрута
(fee/tickSpacing/hooks) через fetch_initialize_event, которых
full_fund_flow_check() сама в leg не кладёт."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call  # noqa: E402
from task5_v4_hook_route_audit import fetch_initialize_event  # noqa: E402
from task5_v4_item3_in_window_control_trade import full_fund_flow_check  # noqa: E402

TX_HASH = "0x90fe304f43209732e4607840e5b602ca74081f75dca26be2c4072c6ba8f745d3"
NATIVE = "0x0000000000000000000000000000000000000000"


def main() -> None:
    result: dict = {"tx_hash": TX_HASH}

    tx = _rpc_call("eth_getTransactionByHash", [TX_HASH])
    receipt = _rpc_call("eth_getTransactionReceipt", [TX_HASH])
    if tx is None or receipt is None:
        result["error"] = "транзакция/рецепт не найдены этим RPC-провайдером"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        _save(result)
        return

    result["status"] = int(receipt["status"], 16)
    result["block_number"] = int(receipt["blockNumber"], 16)
    result["transaction_index"] = int(receipt["transactionIndex"], 16)
    result["tx_from"] = tx["from"].lower()
    result["tx_to"] = (tx.get("to") or "").lower()
    result["tx_value_wei"] = int(tx.get("value", "0x0"), 16)
    result["gas_used"] = int(receipt["gasUsed"], 16)
    eff_price = receipt.get("effectiveGasPrice")
    result["effective_gas_price_wei"] = int(eff_price, 16) if eff_price else None
    if result["effective_gas_price_wei"] is not None:
        result["gas_cost_native_wei"] = result["gas_used"] * result["effective_gas_price_wei"]
        result["gas_cost_native_eth"] = result["gas_cost_native_wei"] / 1e18

    if result["status"] != 1:
        result["verdict"] = "reverted -- не арбитраж по определению (движения средств не было, кроме газа)"
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        _save(result)
        return

    ff = full_fund_flow_check(TX_HASH)
    result["fund_flow"] = ff

    legs = ff.get("legs", [])
    # Полные параметры каждого пула маршрута (fee/tickSpacing/hooks) --
    # full_fund_flow_check() сама их в leg не кладёт (только currency0/1).
    route_full: list[dict] = []
    unique_pool_ids = []
    seen = set()
    for leg in legs:
        pid = leg.get("pool_id")
        if pid and pid not in seen:
            seen.add(pid)
            unique_pool_ids.append(pid)
    init_by_pool = {}
    for pid in unique_pool_ids:
        init = fetch_initialize_event(pid, result["block_number"])
        init_by_pool[pid] = init
    for leg in legs:
        init = init_by_pool.get(leg.get("pool_id"))
        route_full.append({
            **leg,
            "fee": init.get("fee") if init else None,
            "tick_spacing": init.get("tick_spacing") if init else None,
            "hooks": init.get("hooks") if init else None,
        })
    result["route_full"] = route_full
    result["n_legs"] = len(route_full)

    if route_full:
        first_leg = route_full[0]
        a0, a1 = first_leg.get("amount0"), first_leg.get("amount1")
        if a0 is not None and a0 > 0:
            actual_size_raw, actual_size_currency = a0, first_leg.get("currency0")
        else:
            actual_size_raw, actual_size_currency = a1, first_leg.get("currency1")
        result["actual_size_first_leg_raw"] = actual_size_raw
        result["actual_size_first_leg_currency"] = actual_size_currency

    hook_addresses = sorted({leg["hooks"] for leg in route_full if leg.get("hooks") and leg["hooks"].lower() != NATIVE})
    result["hook_addresses_in_route"] = hook_addresses
    net_by_addr = ff.get("net_transfer_by_address_token_raw", {})
    result["hook_related_transfers"] = {addr: net_by_addr.get(addr.lower(), {}) for addr in hook_addresses}

    result["beneficiaries_positive_net_transfer"] = ff.get("positive_net_transfer_addresses", {})
    result["executor_address_checked"] = ff.get("executor_address_checked")
    result["net_trader_flow_by_token_raw"] = ff.get("net_trader_flow_by_token_raw")
    result["closed_cycle_confirmed"] = ff.get("fully_valid_closed_cycle")
    result["base_token"] = ff.get("base_token")
    result["verdict"] = ff.get("verdict")
    result["other_token_spend_by_executor"] = ff.get("other_token_spend_by_executor")

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    _save(result)


def _save(result: dict) -> None:
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_tx_90fe304f_first_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[tx_first_result] сохранено: {out_path}")


if __name__ == "__main__":
    main()
