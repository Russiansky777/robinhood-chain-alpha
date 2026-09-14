#!/usr/bin/env python3
"""Компактная сводка уже восстановленного data/task5_true_arbitrageur_
scan_result.json -- БЕЗ сети, ничего не запускает."""
from __future__ import annotations

import json
from pathlib import Path

RESULT_PATH = Path(__file__).parent.parent / "data" / "task5_true_arbitrageur_scan_result.json"


def main() -> None:
    r = json.loads(RESULT_PATH.read_text())
    keys = ["window_from_block", "window_to_block_requested", "window_to_block_actually_covered",
            "scan_timed_out", "n_tx_with_any_swap", "n_multi_pool_tx", "n_unique_sender_groups",
            "n_sender_groups_checked", "n_sender_groups_qualifying", "current_weth_usdg_price",
            "n_qualifying_transactions_total", "by_contract", "elapsed_seconds"]
    summary = {k: r[k] for k in keys if k in r}
    rows = r.get("qualifying_rows", [])
    summary["n_qualifying_rows"] = len(rows)
    summary["unique_tx_hashes"] = sorted({row["tx_hash"] for row in rows})
    summary["unique_contracts"] = sorted({row["contract"] for row in rows})
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
