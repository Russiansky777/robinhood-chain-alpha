#!/usr/bin/env python3
"""Диагностика: для ПЕРВОГО захвата из task5_bot_known_answer_test.py
(0x7AF12717..., блок 58958203) распечатать ВСЕ транзакции блока (index,
hash, to, 4-байтный селектор) -- проверить руками, баг ли в сопоставлении
катализатора, или реально нет ни одного узнаваемого свопа перед arb-tx
в том же блоке."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import RPC_URL_MAINNET
from task5_bot_pool_state import _rpc_call_with_provider_fallback
from task5_bot_router_decode import KNOWN_SWAP_SELECTORS

TARGET_TX = "0x7AF12717684AEA0384AD50B9F99270853132B30BA786325F88B590CD64321F82".lower()
TARGET_BLOCK = 58958203


def _rpc(method, params):
    return _rpc_call_with_provider_fallback(method, params, RPC_URL_MAINNET, timeout=20.0)


if __name__ == "__main__":
    block = _rpc("eth_getBlockByNumber", [hex(TARGET_BLOCK), True])
    txs = block.get("transactions", [])
    print(f"block {TARGET_BLOCK}: {len(txs)} transactions")
    arb_idx = None
    rows = []
    for i, tx in enumerate(txs):
        h = (tx.get("hash") or "").lower()
        to = (tx.get("to") or "").lower()
        data = tx.get("input") or tx.get("data") or "0x"
        selector = ("0x" + data[2:10]) if len(data) >= 10 else None
        known = KNOWN_SWAP_SELECTORS.get(selector)
        if h == TARGET_TX:
            arb_idx = i
        rows.append({"i": i, "hash": h, "to": to, "selector": selector, "known_fn": known,
                      "data_len": (len(data) - 2) // 2})
    print(f"arb tx index in block: {arb_idx}")
    out = {"block": TARGET_BLOCK, "n_txs": len(txs), "arb_index": arb_idx, "txs": rows}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    Path("data/task5_bot_known_answer_diag_block_result.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False))
