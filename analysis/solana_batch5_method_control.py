#!/usr/bin/env python3
"""Владелец: контроль метода RPC-скана BATCH-5 ДО продолжения и до любых
выводов. Тот же код (fetch_signatures_last_n_hours/classify_tx из
solana_batch5_rpc_check.py), 72ч, на двух эталонах:
  - лидер -- LEADER_WALLET константа проекта, Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit
    (владелец подтвердил: адрес из прошлого запроса был ошибкой по
    памяти, правильный -- эта константа; прошлый контрольный прогон на
    неверном адресе выброшен).
  - Brez (Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB), уже известный
    кошелёк BATCH-3.

Плюс: для этих 2 эталонов И всех 10 приоритетных BATCH-5 -- сколько
подписей реально получено за 72ч (n_sigs_fetched_72h) и сколько из них
с участием Fomo Co-signer (AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51)
среди подписантов -- ноль при 0 подписей и ноль при 500 подписях, честно,
разные вещи, не смешиваем."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_entry_log import tx_signers  # noqa: E402
from solana_batch5_rpc_check import (  # noqa: E402
    fetch_signatures_last_n_hours, classify_tx, LOOKBACK_S, LOOKBACK_HOURS, PRIORITY_10,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_batch5_method_control_result.json"
FOMO_SPONSOR = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
PROJECT_LEADER_WALLET_CONST = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"

CONTROL_WALLETS = [
    (PROJECT_LEADER_WALLET_CONST, "лидер (LEADER_WALLET)"),
    ("Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB", "Brez (BATCH-3)"),
]


def scan_wallet_diag(address: str) -> dict:
    sigs = fetch_signatures_last_n_hours(address, LOOKBACK_S)
    ok_sigs = [s["signature"] for s in sigs if s.get("err") is None]
    entries, n_multi_mint_skipped, n_tx_fetch_failed, n_fomo_cosigner = [], 0, 0, 0
    for sig in ok_sigs:
        tx = fp.get_transaction(sig)
        if tx is None:
            n_tx_fetch_failed += 1
            continue
        if FOMO_SPONSOR in tx_signers(tx):
            n_fomo_cosigner += 1
        ev = classify_tx(tx, address)
        if ev is None or ev["kind"] == "no_first_entry":
            continue
        if ev["kind"] == "multi_mint_skipped":
            n_multi_mint_skipped += 1
            continue
        entries.append(ev)

    sol_sizes = [e["spend_sol_equiv"] for e in entries if e["spend_sol_equiv"] is not None]
    days = LOOKBACK_HOURS / 24
    return {
        "n_sigs_fetched_72h": len(sigs), "n_sigs_ok": len(ok_sigs), "n_tx_fetch_failed": n_tx_fetch_failed,
        "n_sigs_with_fomo_cosigner": n_fomo_cosigner,
        "n_multi_mint_skipped": n_multi_mint_skipped,
        "n_first_entries_72h": len(entries),
        "n_ge_2sol": sum(1 for s in sol_sizes if s >= 2),
        "n_ge_2sol_per_day": round(sum(1 for s in sol_sizes if s >= 2) / days, 3),
        "n_ge_4_3sol": sum(1 for s in sol_sizes if s >= 4.3),
        "n_ge_15sol": sum(1 for s in sol_sizes if s >= 15),
        "median_spend_sol_equiv": round(median(sol_sizes), 4) if sol_sizes else None,
        "n_priced_sol_equiv": len(sol_sizes),
    }


def main() -> None:
    out: dict = {
        "lookback_hours": LOOKBACK_HOURS,
        "HONEST_NOTE_leader_address": (
            f"Исправлено: предыдущий контрольный прогон использовал ошибочный адрес "
            f"(владелец подтвердил опечатку по памяти) и выброшен. Этот прогон -- "
            f"на LEADER_WALLET константе проекта ({PROJECT_LEADER_WALLET_CONST})."
        ),
        "control": {},
        "priority_10_diagnostics": {},
    }

    print("=== КОНТРОЛЬНЫЕ КОШЕЛЬКИ ===", flush=True)
    for addr, label in CONTROL_WALLETS:
        row = scan_wallet_diag(addr)
        row["label"] = label
        out["control"][addr] = row
        print(f"[control] {label} ({addr[:12]}..): sigs72h={row['n_sigs_fetched_72h']} "
              f"first_entries={row['n_first_entries_72h']} >=2SOL/сутки={row['n_ge_2sol_per_day']} "
              f">=4.3SOL={row['n_ge_4_3sol']} >=15SOL={row['n_ge_15sol']} "
              f"fomo_cosigner_sigs={row['n_sigs_with_fomo_cosigner']}", flush=True)
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))

    print("=== ДИАГНОСТИКА 10 ПРИОРИТЕТНЫХ BATCH-5 (подписи/Fomo Co-signer) ===", flush=True)
    for addr, name in PRIORITY_10:
        row = scan_wallet_diag(addr)
        row["name"] = name
        out["priority_10_diagnostics"][addr] = row
        print(f"[control] {name} ({addr[:12]}..): sigs72h={row['n_sigs_fetched_72h']} "
              f"first_entries={row['n_first_entries_72h']} fomo_cosigner_sigs={row['n_sigs_with_fomo_cosigner']}", flush=True)
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))

    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[control] записано {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
