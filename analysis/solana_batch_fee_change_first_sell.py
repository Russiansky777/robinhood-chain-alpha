#!/usr/bin/env python3
"""Владелец: подтвердить, что и на ПРОДАЖЕ (не только на покупке) после
смены на Priority Fee/Bribery Tip 0.0051/0.0051 (14:05 Мадрид/12:05 UTC)
в сеть уходит тот же порядок комиссии. Ведомость
(data/solana_dbot_realized_ledger.json) уже показывает первую закрытую
после смены сделку -- BATCH-4, sell_block_time=1789827614 (после
cutoff), sell_signature известен -- просто разбираем ЭТУ подпись через
RPC, честно, без выдумывания."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_batch_fee_change_first_trade import extract_compute_budget, extract_all_system_transfers  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_batch_fee_change_first_sell_result.json"
WALLET = "EjeXrxabRKmwLxvfQdXYN3oD3d3uWe2fuqqA5Qda2p8N"  # BATCH-4
SIG = "4Nx5eDun2gCaYGYfCrATAcYMFrLbjkXCb7FbJZ3eXxq9qg1QuHzm6T7UUNh7truuE66pvT8xpEVhdCKGkWw2UB4o"


def main() -> None:
    tx = fp.get_transaction(SIG)
    if tx is None:
        result = {"HONEST_ANSWER": "getTransaction вернул null"}
        OUT_PATH.write_text(json.dumps(result, indent=2))
        return
    meta = tx.get("meta") or {}
    cb = extract_compute_budget(tx)
    transfers = extract_all_system_transfers(tx, WALLET)
    result = {
        "task": "BATCH-4 (первая продажа после смены комиссии)", "wallet": WALLET, "signature": SIG,
        "block_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tx["blockTime"])),
        "network_fee_sol": (meta.get("fee") or 0) / 1e9, "network_fee_lamports": meta.get("fee"),
        "compute_unit_limit": cb["compute_unit_limit"],
        "compute_unit_price_microlamports": cb["compute_unit_price_microlamports"],
        "all_system_transfers_from_wallet": transfers,
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
