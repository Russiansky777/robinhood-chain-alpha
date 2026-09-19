#!/usr/bin/env python3
"""Разовая: определить минт CC по транзакции AKBot-сделки (20 WSOL -> CC),
т.к. в транзакции ЛИДЕРА (JFPJEgfXH2PE...) автодетект по балансу
LEADER_WALLET не сработал -- честно ищем по кошельку DT8hib... в ЕГО
собственной подписи."""
from __future__ import annotations

import json
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_resolve_mint_probe_result.json"
AKBOT_SIG = "65WA3e1VhP8Ynsyg1Jv7onsvDciZSFB7zNzwvpHXQ56gzg4KgqteQr57qKX8q8hTBehyW6mkx2E8sKN66oHQ1Q6t"
DT8HIB = "DT8hib8jY4CGJcmcqcinVGYh5zzVPZAV3iosdQF9a6jX"
LEADER_SIG = "JFPJEgfXH2PEfssK799nob9KE6J5Lk4PLcD1xRqqieTUP6rxksXTZAR7eA1jrVU7ejychsqwf6JvN2pd4M5hAqY"


def main() -> None:
    tx = fp.get_transaction(AKBOT_SIG)
    result = {"akbot_signature": AKBOT_SIG, "wallet": DT8HIB}
    if tx is None:
        result["HONEST_ANSWER"] = "getTransaction(AKBOT_SIG) вернул null"
        OUT_PATH.write_text(json.dumps(result, indent=2))
        return
    meta = tx.get("meta") or {}
    pre = {b["mint"]: D(b["uiTokenAmount"]["amount"]) / D(10) ** b["uiTokenAmount"]["decimals"]
           for b in (meta.get("preTokenBalances") or []) if b.get("owner") == DT8HIB}
    post = {b["mint"]: D(b["uiTokenAmount"]["amount"]) / D(10) ** b["uiTokenAmount"]["decimals"]
            for b in (meta.get("postTokenBalances") or []) if b.get("owner") == DT8HIB}
    deltas = {m: str(post.get(m, D(0)) - pre.get(m, D(0))) for m in set(pre) | set(post)}
    result["akbot_tx_slot"] = tx["slot"]
    result["akbot_tx_block_time_utc"] = tx.get("blockTime")
    result["dt8hib_token_balance_deltas"] = deltas
    result["programs_top_level"] = sorted({ix.get("programId") for ix in tx["transaction"]["message"]["instructions"] if isinstance(ix, dict)})

    leader_tx = fp.get_transaction(LEADER_SIG)
    if leader_tx:
        lmeta = leader_tx.get("meta") or {}
        result["leader_tx_slot"] = leader_tx["slot"]
        result["leader_tx_all_preTokenBalances_owners"] = sorted({b.get("owner") for b in (lmeta.get("preTokenBalances") or [])})
        result["leader_tx_all_postTokenBalances_owners"] = sorted({b.get("owner") for b in (lmeta.get("postTokenBalances") or [])})
        result["leader_tx_programs_top_level"] = sorted({ix.get("programId") for ix in leader_tx["transaction"]["message"]["instructions"] if isinstance(ix, dict)})
        result["leader_tx_err"] = lmeta.get("err")

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
