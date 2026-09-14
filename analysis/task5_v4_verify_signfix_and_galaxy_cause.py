#!/usr/bin/env python3
"""Проверка исправления знака (task5_v4_item3_in_window_control_trade.py::
full_fund_flow_check, коммит после 2b4ab85 -- см. докстринг правки в самом
файле) на ДВУХ транзакциях этого раунда + полный разбор транзакции-причины
GALAXY (0xfaffa3d3...) для объяснения соотношения 3.829843/3.810694/0.019149.

Также перепроверка receipt (статус/блок/transactionIndex) для ВСЕХ
ключевых хэшей этого расследования -- по свежему eth_getTransactionReceipt,
не по ранее сохранённым числам."""
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

KEY_TX_HASHES = {
    "arb_galaxy": "0xf4c3fe75c05fedb7a8e0b01cc44203bdec27dc83724cce8a6839841e97117ffd",
    "cause_galaxy": "0xfaffa3d3986a25bf53180d19812b300335b50cb448258e79df0f85a845f8eaeb",
    "arb_mixed_v3v4": "0x90fe304f43209732e4607840e5b602ca74081f75dca26be2c4072c6ba8f745d3",
}


def recheck_receipt(tx_hash: str) -> dict:
    receipt = _rpc_call("eth_getTransactionReceipt", [tx_hash])
    if receipt is None:
        return {"ok": False, "error": "рецепт не найден"}
    return {"ok": True, "status": int(receipt["status"], 16), "block_number": int(receipt["blockNumber"], 16),
            "transaction_index": int(receipt["transactionIndex"], 16), "n_logs": len(receipt["logs"])}


def main() -> None:
    result: dict = {"script_version": "task5_v4_verify_signfix_and_galaxy_cause.py@this-commit"}

    result["receipt_recheck"] = {label: recheck_receipt(h) for label, h in KEY_TX_HASHES.items()}

    # --- Проверка исправленного знака на обеих сделках этого раунда ---
    for label, key in (("arb_galaxy", "arb_galaxy"), ("arb_mixed_v3v4", "arb_mixed_v3v4")):
        ff = full_fund_flow_check(KEY_TX_HASHES[key])
        result[f"fund_flow_fixed_{label}"] = {
            "net_trader_flow_by_token_raw": ff.get("net_trader_flow_by_token_raw"),
            "nonzero_net_flow_tokens": ff.get("nonzero_net_flow_tokens"),
            "fully_valid_closed_cycle": ff.get("fully_valid_closed_cycle"),
            "base_token": ff.get("base_token"),
            "verdict": ff.get("verdict"),
        }

    # --- Полный разбор транзакции-причины GALAXY (0xfaffa3d3...) ---
    cause_ff = full_fund_flow_check(KEY_TX_HASHES["cause_galaxy"])
    result["cause_galaxy_fund_flow"] = cause_ff

    legs = cause_ff.get("legs", [])
    unique_pool_ids, seen = [], set()
    for leg in legs:
        pid = leg.get("pool_id")
        if pid and pid not in seen:
            seen.add(pid)
            unique_pool_ids.append(pid)
    block_number = cause_ff.get("block_number") or _rpc_call("eth_getTransactionReceipt", [KEY_TX_HASHES["cause_galaxy"]])
    block_number = int(_rpc_call("eth_getTransactionReceipt", [KEY_TX_HASHES["cause_galaxy"]])["blockNumber"], 16)
    init_by_pool = {pid: fetch_initialize_event(pid, block_number) for pid in unique_pool_ids}
    route_full = []
    for leg in legs:
        init = init_by_pool.get(leg.get("pool_id"))
        route_full.append({**leg, "fee": init.get("fee") if init else None,
                            "tick_spacing": init.get("tick_spacing") if init else None,
                            "hooks": init.get("hooks") if init else None})
    result["cause_galaxy_route_full"] = route_full
    result["cause_galaxy_block_number"] = block_number

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_verify_signfix_and_galaxy_cause_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[verify_signfix] сохранено: {out_path}")


if __name__ == "__main__":
    main()
