#!/usr/bin/env python3
"""Разбор нового кандидата для измерения реакции конкурента (владелец:
"если по предыдущему кандидату уже восстановлена причина -- закончи
его; если ещё нет -- сначала проверь этот receipt"). Токен
0x34031E1C8B4e6bC8f9e7D2cd88cA6aad08772F29 -- по наблюдению владельца,
меньше транзакций, пример может быть проще.

Первый результат: статус/блок/позиция, полный маршрут, фактический
размер, честное движение средств (переиспользует full_fund_flow_check
-- Swap-уровневый поток + ВСЕ ERC20 Transfer рецепта, включая
получателей помимо tx.from), отдельно газ и переводы на адреса хуков
маршрута, классификация тип операции. Без условия "маршрут поддержан
нашим ботом". read-only, контракт/Sender не тронуты, LIVE не
запускался."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call, topic0  # noqa: E402
from task5_v4_hook_route_audit import fetch_initialize_event  # noqa: E402
from task5_v4_item3_in_window_control_trade import full_fund_flow_check  # noqa: E402

TX_HASH = "0xf4c3fe75c05fedb7a8e0b01cc44203bdec27dc83724cce8a6839841e97117ffd"
TOKEN_OF_INTEREST = "0x34031E1C8B4e6bC8f9e7D2cd88cA6aad08772F29".lower()
NATIVE = "0x0000000000000000000000000000000000000000"
V3_SWAP_TOPIC0 = topic0("Swap(address,address,int256,int256,uint160,uint128,int24)")


def to_signed(v: int, bits: int = 256) -> int:
    return v - (1 << bits) if v >= (1 << (bits - 1)) else v


def main() -> None:
    result: dict = {"tx_hash": TX_HASH, "token_of_interest": TOKEN_OF_INTEREST}

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

    # Полный дамп логов рецепта -- сразу, чтобы не пропустить нестандартные
    # события (напр. V3-стиля Swap у контрагента, если он есть).
    all_logs = sorted(
        [{"log_index": int(l["logIndex"], 16), "address": l["address"].lower(),
          "topic0": l["topics"][0] if l["topics"] else None, "n_topics": len(l["topics"]),
          "data_len_bytes": (len(l["data"]) - 2) // 2 if l["data"] not in ("0x", "") else 0}
         for l in receipt["logs"]],
        key=lambda x: x["log_index"],
    )
    result["all_logs_summary"] = all_logs

    if result["status"] != 1:
        result["verdict"] = "reverted -- не арбитраж по определению (движения средств не было, кроме газа)"
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        _save(result)
        return

    ff = full_fund_flow_check(TX_HASH)
    result["fund_flow"] = ff

    legs = ff.get("legs", [])
    unique_pool_ids, seen = [], set()
    for leg in legs:
        pid = leg.get("pool_id")
        if pid and pid not in seen:
            seen.add(pid)
            unique_pool_ids.append(pid)
    init_by_pool = {pid: fetch_initialize_event(pid, result["block_number"]) for pid in unique_pool_ids}
    route_full = []
    for leg in legs:
        init = init_by_pool.get(leg.get("pool_id"))
        route_full.append({**leg, "fee": init.get("fee") if init else None,
                            "tick_spacing": init.get("tick_spacing") if init else None,
                            "hooks": init.get("hooks") if init else None})
    result["route_full"] = route_full
    result["n_legs"] = len(route_full)

    if route_full:
        first_leg = route_full[0]
        a0, a1 = first_leg.get("amount0"), first_leg.get("amount1")
        if a0 is not None and a0 > 0:
            result["actual_size_first_leg_raw"] = a0
            result["actual_size_first_leg_currency"] = first_leg.get("currency0")
        else:
            result["actual_size_first_leg_raw"] = a1
            result["actual_size_first_leg_currency"] = first_leg.get("currency1")

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

    # Любые адреса, замешанные в переводах, помимо executor/tx.from --
    # проверка "есть ли ещё один венчур/контрагент, который сам эмитировал
    # СВОЙ Swap-лог (V3-стиля)" -- та же проверка, что и для 0x90fe304f.
    other_addresses = set()
    for t in ff.get("all_transfers_in_receipt", []):
        other_addresses.add(t["from"])
        other_addresses.add(t["to"])
    other_addresses -= {result["tx_from"], result["tx_to"]}
    for leg in route_full:
        other_addresses.discard((leg.get("currency0") or "").lower())
        other_addresses.discard((leg.get("currency1") or "").lower())
    result["other_addresses_seen_in_transfers"] = sorted(other_addresses)

    v3_style_logs = {}
    for addr in other_addresses:
        addr_logs = [l for l in receipt["logs"] if l["address"].lower() == addr]
        v3_matches = [l for l in addr_logs if l["topics"] and l["topics"][0].lower() == V3_SWAP_TOPIC0.lower()]
        if v3_matches:
            log = v3_matches[0]
            sender = "0x" + log["topics"][1][-40:]
            recipient = "0x" + log["topics"][2][-40:]
            data = log["data"][2:]
            words = [data[i:i + 64] for i in range(0, len(data), 64)]
            v3_style_logs[addr] = {
                "sender": sender, "recipient": recipient,
                "amount0": to_signed(int(words[0], 16)), "amount1": to_signed(int(words[1], 16)),
                "sqrt_price_x96": int(words[2], 16), "liquidity": int(words[3], 16),
                "tick": to_signed(int(words[4], 16), 24) if len(words) > 4 else None,
            }
    result["v3_style_swap_logs_from_other_addresses"] = v3_style_logs

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    _save(result)


def _save(result: dict) -> None:
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_tx_f4c3fe75_first_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[tx_first_result] сохранено: {out_path}")


if __name__ == "__main__":
    main()
