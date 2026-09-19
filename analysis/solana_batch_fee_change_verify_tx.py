#!/usr/bin/env python3
"""Владелец, 2026-09-19: найденная "первая сделка" BATCH-3 после смены
комиссии (19g2RXZpDHfe281ci71ULkGqtn6nq2DTvvhSWYSuPjj6CLtEVS4hKGjke5x6R
Rcd5fCxsbxWZsUww5D3hvMq11d) подозрительна: network_fee=0.000015 SOL
(похоже на 3 подписи x 5000 лампорт, БЕЗ приоритетной наценки),
compute_unit_limit/price = null, все system-transfer от кошелька =
пусто. Для настоящей DBot-сделки под новым конфигом (Priority Fee
0.0051 + Bribery Tip 0.0051) ожидались бы ComputeBudget-инструкции и
чаевый transfer. Проверяем -- что это за транзакция на самом деле
(список программ/типов инструкций), не выдаём её честно за ответ на
вопрос владельца, если это не реальная сделка."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_batch_fee_change_verify_tx.json"
SIG = "19g2RXZpDHfe281ci71ULkGqtn6nq2DTvvhSWYSuPjj6CLtEVS4hKGjke5x6RRcd5fCxsbxWZsUww5D3hvMq11d"
WALLET = "BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu"


def main() -> None:
    tx = fp.get_transaction(SIG)
    if tx is None:
        Path(OUT_PATH).write_text(json.dumps({"HONEST_ANSWER": "getTransaction вернул null"}, indent=2))
        return
    meta = tx.get("meta") or {}
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    inner = meta.get("innerInstructions") or []

    def summarize(ix_list):
        out = []
        for ix in ix_list:
            if not isinstance(ix, dict):
                continue
            parsed = ix.get("parsed")
            out.append({"program": ix.get("program"), "programId": ix.get("programId"),
                        "type": (parsed or {}).get("type") if isinstance(parsed, dict) else None,
                        "info": (parsed or {}).get("info") if isinstance(parsed, dict) else ix.get("data")})
        return out

    pre_tb = meta.get("preTokenBalances") or []
    post_tb = meta.get("postTokenBalances") or []
    wallet_pre = [b for b in pre_tb if b.get("owner") == WALLET]
    wallet_post = [b for b in post_tb if b.get("owner") == WALLET]

    result = {
        "signature": SIG,
        "err": meta.get("err"),
        "fee_lamports": meta.get("fee"),
        "n_top_level_instructions": len(instrs),
        "top_level_instructions": summarize(instrs),
        "inner_instruction_groups": [{"index": g.get("index"), "instructions": summarize(g.get("instructions", []))}
                                      for g in inner if isinstance(g, dict)],
        "wallet_pre_token_balances": wallet_pre,
        "wallet_post_token_balances": wallet_post,
        "log_messages_sample": (meta.get("logMessages") or [])[:20],
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
