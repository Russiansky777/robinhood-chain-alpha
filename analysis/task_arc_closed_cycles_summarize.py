#!/usr/bin/env python3
"""Полный stdout task_arc_closed_cycle_detector.py (267 циклов) не влезает
в разовую выборку лога GH Actions. Компаньон без новых RPC-вызовов --
читает уже сохранённый data/task_arc_closed_cycles_result.json на этом
же VPS и печатает компактную сводку по окнам + все верифицированные
циклы (без полного route -- только ключевые поля)."""
from __future__ import annotations

import json
from pathlib import Path

RESULT_PATH = Path(__file__).parent.parent.joinpath("data", "task_arc_closed_cycles_result.json")


def main() -> None:
    data = json.loads(RESULT_PATH.read_text())
    summary = {
        "probed_at_utc": data.get("probed_at_utc"),
        "pool_map_seed": data.get("pool_map_seed"),
        "total_rpc_calls_used": data.get("total_rpc_calls_used"),
        "summary": data.get("summary"),
        "preregistered_verdict": data.get("preregistered_verdict"),
        "top_profitable_cycles_tx_hashes": data.get("top_profitable_cycles_tx_hashes"),
        "windows_meta": [],
    }
    for w in data.get("windows", []):
        summary["windows_meta"].append({
            "label": w["label"], "from_block": w["from_block"], "to_block": w["to_block"],
            "pool_map_scan_n_found": w["pool_map_scan"]["n_found"],
            "swap_scan_n_found": w["swap_scan"]["n_found"],
            "topology_filter": w["topology_filter"],
            "verification": w["verification"],
            "receipt_budget_remaining_this_window": w.get("receipt_budget_remaining_this_window"),
            "n_verified_cycles_this_window": len(w.get("verified_cycles", [])),
        })

    compact_cycles = []
    for w in data.get("windows", []):
        for c in w.get("verified_cycles", []):
            compact_cycles.append({
                "window": w["label"], "tx_hash": c["tx_hash"], "block_number": c["block_number"],
                "n_legs": c["n_legs"], "profit_usdc_net": c["profit_usdc_net"],
                "senders": c["senders"], "tx_from": c["tx_from"],
                "uses_aka_fun_hook": c["uses_aka_fun_hook"],
                "has_liquidity_zero_leg": c["has_liquidity_zero_leg"],
                "pool_age_blocks_at_cycle": c["pool_age_blocks_at_cycle"],
            })
    summary["all_verified_cycles_compact"] = compact_cycles

    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    Path(__file__).parent.parent.joinpath("data", "task_arc_closed_cycles_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
