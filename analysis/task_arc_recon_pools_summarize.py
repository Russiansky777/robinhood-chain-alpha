#!/usr/bin/env python3
"""Компаньон task_arc_recon_pools.py -- предыдущий прогон уже сохранил
data/task_arc_recon_pools_result.json НА ЭТОМ ЖЕ хосте (NL VPS), но лог
GH Actions с полным stdout (>400К символов) не влезает в разовую выборку
лога. Этот скрипт НЕ делает новых RPC-вызовов -- только читает уже
сохранённый реальный файл и печатает компактную агрегатную сводку,
достаточную, чтобы не тянуть весь JSON заново."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

RESULT_PATH = Path(__file__).parent.parent.joinpath("data", "task_arc_recon_pools_result.json")


def main() -> None:
    if not RESULT_PATH.exists():
        print(json.dumps({"error": f"{RESULT_PATH} не найден на этом хосте -- прошлый прогон "
                                    f"не оставил файл здесь (мог быть на другом раннере)."}))
        return

    data = json.loads(RESULT_PATH.read_text())
    pools = data.get("initialize_events", {}).get("pools", [])

    currency0_counts = Counter(p["currency0"] for p in pools)
    currency1_set = {p["currency1"] for p in pools}
    blocks = [p["block_number"] for p in pools]
    tx_hashes = {p["tx_hash"] for p in pools}

    summary = {
        "source_file": str(RESULT_PATH),
        "probed_at_utc": data.get("probed_at_utc"),
        "rpc": data.get("rpc"),
        "latest_block": data.get("latest_block"),
        "max_working_chunk_for_initialize": data.get("max_working_chunk_for_initialize"),
        "window": data.get("window"),
        "n_found_top_level": data.get("initialize_events", {}).get("n_found"),
        "n_chunk_errors_top_level": data.get("initialize_events", {}).get("n_chunk_errors"),
        "n_pools_in_list": len(pools),
        "n_distinct_currency0": len(currency0_counts),
        "currency0_counts": dict(currency0_counts.most_common(10)),
        "n_distinct_currency1": len(currency1_set),
        "n_distinct_tx_hashes": len(tx_hashes),
        "n_txs_with_multiple_pools": sum(1 for tx, cnt in Counter(p["tx_hash"] for p in pools).items() if cnt > 1),
        "block_range": {"min": min(blocks) if blocks else None, "max": max(blocks) if blocks else None,
                         "span": (max(blocks) - min(blocks)) if blocks else None},
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    Path(__file__).parent.parent.joinpath("data", "task_arc_recon_pools_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
