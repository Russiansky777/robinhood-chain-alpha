#!/usr/bin/env python3
"""Владелец, 2026-09-19: диагностика -- почему solana_pilot_signal_capture_rate_v2.py
не нашёл 2 реальных сигнала лидера (GtDZKAqvMZ.../EgP1f5J9LD..., оба
classify()-подтверждённые первые входы >=4.3 SOL, обе транзакции ВНУТРИ
просканированного окна). Проверяем find_canonical_signals напрямую на
этих же двух транзакциях лидера, печатаем каждый промежуточный шаг."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_signal_definition import is_signer_anywhere, wallet_token_pre_post, find_canonical_signals  # noqa: E402
from solana_buyer200_select_extend import classify  # noqa: E402
import solana_buyer200_fast_price as fp  # noqa: E402
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_capture_v2_miss_diagnostic.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
CASES = [
    {"mint": "GtDZKAqvMZMnti46ZewMiXCa4oXF4bZxwQPoKzXPFxZn",
     "leader_signature": "4vPsRrt1cNtqxChbZFKDnDTTN85BweBn3cRSsRfVmjXvs4e1pg39S9cEaXei2XDDHi3JwF1benw4bDmfPCWGFLdn"},
    {"mint": "EgP1f5J9LDn1fuTMfrrgT5ZRMiLBitYdkmDs818BS44k",
     "leader_signature": "5jpYqPa3yaTuMBaSc8JndfTPfMkEGnN7tLBGyZiwUQtCjUUUFz5iAhjUcczTCzDFbJyiYX1CzMBXhPzQfhUEK7SD"},
]


def sol_usd_price_at(t: int) -> float | None:
    resp = requests.get("https://api.geckoterminal.com/api/v2/networks/solana/pools/"
                         "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv/ohlcv/minute",
                         params={"aggregate": 1, "before_timestamp": t + 3600, "limit": 200, "currency": "usd"},
                         timeout=30, headers={"Accept": "application/json"})
    if not resp.ok:
        return None
    rows = ((resp.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    if not rows:
        return None
    rows = sorted(rows, key=lambda c: c[0])
    return rows[-1][4]


def main() -> None:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "cases": []}
    for c in CASES:
        tx = fp.get_transaction(c["leader_signature"])
        entry: dict = {"mint": c["mint"], "leader_signature": c["leader_signature"]}
        if not tx:
            entry["status"] = "tx_fetch_failed"
            result["cases"].append(entry)
            continue
        entry["block_time"] = tx.get("blockTime")
        entry["is_signer_anywhere"] = is_signer_anywhere(tx, LEADER_WALLET)
        bals = wallet_token_pre_post(tx, LEADER_WALLET)
        pre = bals.get("pre", {})
        post = bals.get("post", {})
        entry["pre_balance_for_mint"] = str(pre.get(c["mint"], "0"))
        entry["post_balance_for_mint"] = str(post.get(c["mint"], "0"))
        entry["mint_in_post_keys"] = c["mint"] in post
        entry["mint_in_pre_keys"] = c["mint"] in pre
        entry["all_post_mints"] = list(post.keys())

        row, check = classify(tx, {"transactionIndex": None})
        entry["classify_row"] = {"mint": row.get("mint"), "zero_balance": row.get("zero_balance"),
                                  "usdc_spent": row.get("usdc_spent")} if row else None
        entry["classify_check"] = check

        canonical = find_canonical_signals(tx, LEADER_WALLET, 4.3, sol_usd_price_at)
        entry["find_canonical_signals_result"] = canonical
        entry["status"] = "ok"
        result["cases"].append(entry)
        print(f"[diag] {c['mint'][:10]}.. is_signer={entry['is_signer_anywhere']} "
              f"pre={entry['pre_balance_for_mint']} post={entry['post_balance_for_mint']} "
              f"canonical_result={canonical} classify={entry['classify_row']}", flush=True)

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[diag] записано в {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
