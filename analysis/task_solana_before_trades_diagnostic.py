#!/usr/bin/env python3
"""Владелец, 2026-09-17: Solana, точечная диагностика -- реальный прогон
узкого окна после фикса extract_trade (требование именно QUOTE_MINT=GLDx)
дал 0 сделок ДО и ПОСЛЕ покупки, резко отличаясь от прежних 216-286 (те
были артефактом бага, уже исправленного и подтверждённого регрессионным
тестом). Прежде чем делать вывод "в этом пуле сделок не было" -- дёшево
перепроверить: 2 сигнатуры "до" пришли ИЗ РЕАЛЬНОЙ истории адреса пула
(getSignaturesForAddress, а не из мейннет-скана блоков) -- надёжный
сигнал релевантности. Печатаем их СЫРЫЕ preTokenBalances/postTokenBalances
без фильтрации, чтобы увидеть, есть ли там вообще GLDx или нет."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task_solana_wallet_first_buy import rpc_call  # noqa: E402
from task_solana_price_horizons_and_wave import POOL_ADDRESS, QUOTE_MINT, TOKEN_MINT  # noqa: E402

OUT_PATH = Path("data/task_solana_before_trades_diagnostic_result.json")
ANCHOR_SIGNATURE = "2NkPm8GfVw2FYBHrbLbhUrwJGmGECYh4t89oMdCnEsnAXGK8qu4BoTfpNVKEHZLWgBfGYCpZ2oUCBFLW35m8XCNS"
ANCHOR_TIME = 1789600539
WINDOW_S = 300


def main() -> None:
    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "pool_address": POOL_ADDRESS, "quote_mint": QUOTE_MINT, "token_mint": TOKEN_MINT}
    sigs = rpc_call("getSignaturesForAddress", [POOL_ADDRESS, {"before": ANCHOR_SIGNATURE, "limit": 1000}]) or []
    in_window = [s for s in sigs if s.get("blockTime") and ANCHOR_TIME - WINDOW_S <= s["blockTime"] < ANCHOR_TIME
                 and s.get("err") is None]
    out["n_signatures_fetched"] = len(sigs)
    out["n_in_window"] = len(in_window)
    out["in_window_signatures"] = in_window

    details = []
    for s in in_window:
        tx = rpc_call("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        if tx is None:
            details.append({"signature": s["signature"], "error": "getTransaction вернул None"})
            continue
        meta = tx.get("meta") or {}
        pre_tb, post_tb = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []
        mints_seen = sorted({r.get("mint") for r in pre_tb + post_tb if r.get("mint")})
        program_ids = sorted({i.get("programId") for i in
                               tx.get("transaction", {}).get("message", {}).get("instructions", [])
                               if i.get("programId")})
        details.append({
            "signature": s["signature"], "blockTime": s.get("blockTime"), "slot": tx.get("slot"),
            "err": meta.get("err"), "mints_seen_in_balances": mints_seen,
            "has_quote_mint": QUOTE_MINT in mints_seen, "has_token_mint": TOKEN_MINT in mints_seen,
            "program_ids": program_ids,
            "pre_token_balances": pre_tb, "post_token_balances": post_tb,
        })
        time.sleep(0.6)
    out["details"] = details
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[diag] {len(details)} транзакций разобрано, записано {OUT_PATH}")
    for d in details:
        print(f"  {d['signature'][:20]}: has_quote_mint={d.get('has_quote_mint')} "
              f"has_token_mint={d.get('has_token_mint')} mints={d.get('mints_seen_in_balances')} "
              f"programs={d.get('program_ids')}")


if __name__ == "__main__":
    main()
