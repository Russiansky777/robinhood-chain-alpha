#!/usr/bin/env python3
"""Разовая: полный сырой дамп трёх известных транзакций Pump.fun AMM
(pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA), чтобы реверс-инжинирить
формат инструкции/лога для декодера -- без выдумывания, только то, что
реально в транзакции. AKBot-транзакция даёт ИЗВЕСТНЫЙ результат (20
WSOL -> 14 263 324.112826 CC, уже подтверждено балансами) -- эталон для
проверки любой извлечённой логики.

getTransaction здесь с encoding=jsonParsed -- accounts/programId в
инструкциях УЖЕ разрешены RPC в реальные адреса (не индексы), даже для
непарсенных (неизвестных RPC) программ -- парсится только семантика
(поле 'parsed'), не адресация."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_pumpamm_raw_dump_result.json"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

SIGS = {
    "leader": "JFPJEgfXH2PEfssK799nob9KE6J5Lk4PLcD1xRqqieTUP6rxksXTZAR7eA1jrVU7ejychsqwf6JvN2pd4M5hAqY",
    "akbot_known_20wsol_to_14263324cc": "65WA3e1VhP8Ynsyg1Jv7onsvDciZSFB7zNzwvpHXQ56gzg4KgqteQr57qKX8q8hTBehyW6mkx2E8sKN66oHQ1Q6t",
    "ours_batch3": "5QJGgyXEuYZQXqyqv2QEzzPdvBoQTN3whnSUt2qJsj6KbwR6SgkpXi9SU5tju2g3gRz3NtnjSxkcGXZmdAT19sZ6",
}


def dump_tx(sig: str) -> dict:
    tx = fp.get_transaction(sig)
    if tx is None:
        return {"signature": sig, "HONEST_ANSWER": "getTransaction вернул null"}
    meta = tx.get("meta") or {}
    msg = tx["transaction"]["message"]
    top_ix = msg.get("instructions", [])
    inner = meta.get("innerInstructions") or []

    def describe_ix(ix):
        return {"programId": ix.get("programId"), "data": ix.get("data"),
                "parsed": ix.get("parsed"), "accounts": ix.get("accounts")}

    pump_top = [describe_ix(ix) for ix in top_ix if isinstance(ix, dict) and ix.get("programId") == PUMP_AMM_PROGRAM]
    pump_inner = []
    for grp in inner:
        for ix in grp.get("instructions", []):
            if isinstance(ix, dict) and ix.get("programId") == PUMP_AMM_PROGRAM:
                pump_inner.append({"group_index": grp.get("index"), **describe_ix(ix)})

    log_lines = meta.get("logMessages") or []
    program_data_logs = [ln for ln in log_lines if ln.startswith("Program data:")]

    pre_tb = meta.get("preTokenBalances") or []
    post_tb = meta.get("postTokenBalances") or []

    return {
        "signature": sig, "slot": tx["slot"], "err": meta.get("err"),
        "all_top_level_program_ids": sorted({ix.get("programId") for ix in top_ix if isinstance(ix, dict)}),
        "pump_amm_top_level_instructions": pump_top,
        "pump_amm_inner_instructions": pump_inner,
        "all_program_data_logs": program_data_logs,
        "all_log_messages": log_lines,
        "preTokenBalances": pre_tb, "postTokenBalances": post_tb,
        "loadedAddresses": meta.get("loadedAddresses"),
    }


def main() -> None:
    result = {label: dump_tx(sig) for label, sig in SIGS.items()}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    for label, r in result.items():
        print(f"=== {label} ===", flush=True)
        print(f"  n_pump_top={len(r.get('pump_amm_top_level_instructions', []))} "
              f"n_pump_inner={len(r.get('pump_amm_inner_instructions', []))} "
              f"n_program_data_logs={len(r.get('all_program_data_logs', []))}", flush=True)


if __name__ == "__main__":
    main()
