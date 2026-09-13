#!/usr/bin/env python3
"""Задача 5, десятый раунд (правка владельца): проверка КОНКРЕТНОЙ tx,
которую владелец привёл как пример инверсии знака --
0xde38133b2cdf7f3b6307225fce0fad6c0914796a48290dcb5083fe51abdbdf71.

Эта tx НЕ найдена в уже сохранённом task5_v4_item3_in_window_control_
trade_result.json (601 кандидат окна пилота) -- либо вне окна, либо не
распознана сканом как многоходовая. Это ОДИН точечный RPC-запрос по
ОДНОМУ известному хешу (eth_getTransactionByHash/Receipt + Initialize
для встреченных pool_id) -- НЕ широкий скан (владелец явно запретил
именно широкий скан, не точечный запрос по конкретному хешу, который он
сам и привёл)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import json  # noqa: E402

from alchemy_fallback import _rpc_call  # noqa: E402
from task5_v4_hook_route_audit import fetch_initialize_event  # noqa: E402
from task5_v4_pool_math import decode_v4_swap_log_data  # noqa: E402
from task5_v4_competitor_trade_scan import SWAP_TOPIC0  # noqa: E402

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
NATIVE = "0x0000000000000000000000000000000000000000"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
TX_HASH = "0xde38133b2cdf7f3b6307225fce0fad6c0914796a48290dcb5083fe51abdbdf71"


def decode_receipt_transfers_all(receipt: dict) -> list[dict]:
    out = []
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if not topics or topics[0].lower() != TRANSFER_TOPIC0 or len(topics) < 3:
            continue
        frm = "0x" + topics[1][-40:]
        to = "0x" + topics[2][-40:]
        amount = int(log["data"], 16) if log["data"] not in ("0x", "") else 0
        out.append({"token": log["address"].lower(), "from": frm.lower(), "to": to.lower(), "amount": amount})
    return out


def main() -> None:
    result: dict = {"tx_hash": TX_HASH}
    tx = _rpc_call("eth_getTransactionByHash", [TX_HASH])
    receipt = _rpc_call("eth_getTransactionReceipt", [TX_HASH])
    if tx is None or receipt is None:
        result["ok"] = False
        result["error"] = "tx/receipt не найдены на реальной цепи этим точечным запросом"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    result["block_number"] = int(receipt["blockNumber"], 16)
    result["tx_from"] = tx["from"].lower()
    result["tx_to"] = (tx.get("to") or "").lower()
    result["status"] = int(receipt["status"], 16)

    swap_logs = [l for l in receipt["logs"]
                 if l["address"].lower() == POOL_MANAGER.lower() and l["topics"]
                 and l["topics"][0].lower() == SWAP_TOPIC0.lower()]
    legs = []
    net_corrected: dict[str, int] = {}
    for log in sorted(swap_logs, key=lambda l: int(l["logIndex"], 16)):
        pool_id_hex = log["topics"][1]
        decoded = decode_v4_swap_log_data(log["data"])
        init = fetch_initialize_event(pool_id_hex, result["block_number"])
        if init is None:
            legs.append({"pool_id": pool_id_hex, **decoded, "currency0": None, "currency1": None,
                         "note": "Initialize не найден"})
            continue
        c0, c1 = init["currency0"].lower(), init["currency1"].lower()
        # ИСПРАВЛЕНО (правка владельца): ПРЯМОЕ суммирование, БЕЗ
        # унарного минуса -- decoded amount0/amount1 УЖЕ являются
        # собственной (трейдера) дельтой этого события, не дельтой пула.
        net_corrected[c0] = net_corrected.get(c0, 0) + decoded["amount0"]
        net_corrected[c1] = net_corrected.get(c1, 0) + decoded["amount1"]
        legs.append({"pool_id": pool_id_hex, "currency0": c0, "currency1": c1, "hooks": init.get("hooks"), **decoded})
    result["legs"] = legs
    result["corrected_net_flow_all_tokens"] = net_corrected
    nz = {t: v for t, v in net_corrected.items() if v != 0}
    result["corrected_nonzero_tokens"] = nz

    all_transfers = decode_receipt_transfers_all(receipt)
    result["all_transfers_in_receipt"] = all_transfers
    net_by_addr_token: dict[str, dict[str, int]] = {}
    for t in all_transfers:
        net_by_addr_token.setdefault(t["from"], {})[t["token"]] = \
            net_by_addr_token.setdefault(t["from"], {}).get(t["token"], 0) - t["amount"]
        net_by_addr_token.setdefault(t["to"], {})[t["token"]] = \
            net_by_addr_token.setdefault(t["to"], {}).get(t["token"], 0) + t["amount"]
    positive_addrs = {a: {tok: amt for tok, amt in toks.items() if amt > 0} for a, toks in net_by_addr_token.items()}
    positive_addrs = {a: t for a, t in positive_addrs.items() if t}
    result["positive_net_transfer_addresses"] = positive_addrs

    weth_touched = any(t["token"] == WETH.lower() for t in all_transfers)
    result["weth_touched_but_not_in_decoded_legs"] = weth_touched and WETH.lower() not in net_corrected
    result["any_leg_undecoded"] = any(l.get("currency0") is None for l in legs)

    result["ok"] = True
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
