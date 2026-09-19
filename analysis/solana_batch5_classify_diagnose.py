#!/usr/bin/env python3
"""Разовая диагностика: почему classify_tx даёт kind=first_entry, но
spend_sol_equiv=None (подразумевает stable_only=True) на ИЗВЕСТНО
SOL-номинированных сделках лидера. Печатает все промежуточные
величины для одной известной подписи (по УЖЕ известной подписи, не
новый скан)."""
from __future__ import annotations

import json
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_entry_log import tx_signers  # noqa: E402
from solana_batch5_rpc_check import mint_balance_map, tx_program_ids, DEX_PROGRAMS, WSOL, USDC, USDT, STABLE_MINTS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_batch5_classify_diagnose_result.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"

SIG = json.loads((REPO_ROOT / "data/solana_entry_log_egp1f5j9ld.json").read_text())["leader_signature"]


def main() -> None:
    tx = fp.get_transaction(SIG)
    out: dict = {"signature": SIG, "wallet": LEADER_WALLET}
    if tx is None:
        out["HONEST_ANSWER"] = "getTransaction вернул null"
        OUT_PATH.write_text(json.dumps(out, indent=2))
        return

    meta = tx.get("meta") or {}
    out["err"] = meta.get("err")
    signers = tx_signers(tx)
    out["signers"] = signers
    out["wallet_is_signer"] = LEADER_WALLET in signers
    out["dex_programs_present"] = sorted(tx_program_ids(tx) & DEX_PROGRAMS)

    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    out["wallet_in_account_keys"] = LEADER_WALLET in keys
    if LEADER_WALLET in keys:
        idx = keys.index(LEADER_WALLET)
        pre_balances, post_balances = meta.get("preBalances") or [], meta.get("postBalances") or []
        out["account_index"] = idx
        out["pre_sol_lamports"] = pre_balances[idx] if idx < len(pre_balances) else None
        out["post_sol_lamports"] = post_balances[idx] if idx < len(post_balances) else None

    pre_tb = mint_balance_map(meta.get("preTokenBalances"), LEADER_WALLET)
    post_tb = mint_balance_map(meta.get("postTokenBalances"), LEADER_WALLET)
    out["pre_token_balances_by_mint"] = {k: str(v) for k, v in pre_tb.items()}
    out["post_token_balances_by_mint"] = {k: str(v) for k, v in post_tb.items()}

    bought = []
    for mint, post_amt in post_tb.items():
        if mint in STABLE_MINTS or mint == WSOL:
            continue
        pre_amt = pre_tb.get(mint, D(0))
        if pre_amt == 0 and post_amt > 0:
            bought.append((mint, str(post_amt)))
    out["bought_candidates"] = bought

    # честный дамп preTokenBalances/postTokenBalances СЫРЬЁМ -- сколько записей вообще есть по кошельку
    out["raw_pre_token_balances_all_owners_count"] = len(meta.get("preTokenBalances") or [])
    out["raw_post_token_balances_all_owners_count"] = len(meta.get("postTokenBalances") or [])
    out["raw_pre_token_balances_for_wallet"] = [b for b in (meta.get("preTokenBalances") or []) if b.get("owner") == LEADER_WALLET]
    out["raw_post_token_balances_for_wallet"] = [b for b in (meta.get("postTokenBalances") or []) if b.get("owner") == LEADER_WALLET]

    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
