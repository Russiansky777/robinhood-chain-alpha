#!/usr/bin/env python3
"""Диагностика: task_arc_arb_our_share.py дал абсурдное значение
2.315493617749717e+79 USDC для пары токена 0xa163d7624da3b5d9182c50eab
5b8cd247ae861bb, повторяющееся ИДЕНТИЧНО на 18 разных блоках (не
непрерывный эпизод, а разрозненные однoблочные вспышки) -- это
явно артефакт, не реальная возможность. Разбираем на реальных данных,
не гадаем."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from task_arc_arb_our_share import (  # noqa: E402
    rpc, extsload_calldata_1slot, decode_slot0_result, state_slot_int,
    usdc_per_token_human, POOL_MANAGER, USDC_ERC20,
)

TOKEN = "0xa163d7624da3b5d9182c50eab5b8cd247ae861bb"
POOL_A = "0x7f7e2f6d88eb8ce9c08a9906b6402a99d9f68003d33a2b3c909d20026e609c32"
POOL_B = "0xec409959e32bc7d29d969165e9eae8b6225f50c0ac2b38df3783320a419663ca"
FLAGGED_BLOCK = 21241014
NEIGHBOR_BLOCK = 21241015  # соседний блок из того же окна, НЕ помеченный как возможность


def main() -> None:
    out: dict = {}

    reg_path = Path("/home/bot/robinhood-chain-alpha/data/task_arc_recon_pools_result.json")
    if not reg_path.exists():
        reg_path = Path(__file__).parent.parent.joinpath("data", "task_arc_recon_pools_result.json")
    obj = json.loads(reg_path.read_text())
    pools = obj.get("initialize_events", {}).get("pools", [])
    rows_for_token = [r for r in pools if r["currency0"].lower() == TOKEN or r["currency1"].lower() == TOKEN]
    out["raw_registry_rows_for_token"] = rows_for_token

    dec_body = rpc("eth_call", [{"to": TOKEN, "data": "0x313ce567"}, "latest"])
    sym_body = rpc("eth_call", [{"to": TOKEN, "data": "0x95d89b41"}, "latest"])
    out["live_decimals_call"] = dec_body
    out["live_symbol_call_raw"] = sym_body

    for label, block in [("flagged_block", FLAGGED_BLOCK), ("neighbor_block", NEIGHBOR_BLOCK)]:
        block_hex = hex(block)
        row = {}
        for pool_label, pool_id in [("pool_A", POOL_A), ("pool_B", POOL_B)]:
            slot_int = state_slot_int(pool_id)
            body = rpc("eth_call", [{"to": POOL_MANAGER, "data": extsload_calldata_1slot(slot_int)}, block_hex])
            raw_result = body.get("result")
            slot0 = decode_slot0_result(raw_result)
            row[pool_label] = {"raw_result": raw_result, "decoded": slot0}
        out[label] = row

    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    data_dir = Path("/home/bot/robinhood-chain-alpha/data")
    if not data_dir.exists():
        data_dir = Path(__file__).parent.parent.joinpath("data")
    data_dir.joinpath("task_arc_arb_our_share_diag_result.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
