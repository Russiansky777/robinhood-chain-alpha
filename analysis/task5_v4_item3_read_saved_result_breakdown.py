#!/usr/bin/env python3
"""Ещё одно чисто читающее (тот же уже сохранённый файл, НИКАКИХ новых
RPC-вызовов, НИКАКОГО пересчёта) уточнение: среди кандидатов с РОВНО
одним ненулевым чистым потоком токена (is_closed_cycle-условие "форма
цикла" выполнено) -- сколько РЕАЛЬНО положительны (потенциальная прибыль,
но, возможно, отсеяны только из-за посторонних Transfer у исполняющего
адреса), а сколько отрицательны (реальный чистый убыток на уровне самих
Swap-дельт, независимо от посторонних Transfer)."""
import json
from pathlib import Path

RESULT_FILE = Path(__file__).parent.parent / "data" / "task5_v4_item3_in_window_control_trade_result.json"


def main() -> None:
    d = json.loads(RESULT_FILE.read_text())
    ffc = d.get("fund_flow_checks") or []

    single_token_positive = []
    single_token_negative = []
    for row in ffc:
        nz = row.get("nonzero_net_flow_tokens")
        if nz is None or len(nz) != 1:
            continue
        token, value = next(iter(nz.items()))
        entry = {
            "tx_hash": row.get("tx_hash"), "block": row.get("block"), "token": token, "value_raw": value,
            "other_token_spend_by_executor": row.get("other_token_spend_by_executor"),
            "fully_valid_closed_cycle": row.get("fully_valid_closed_cycle"),
        }
        (single_token_positive if value > 0 else single_token_negative).append(entry)

    summary = {
        "n_single_nonzero_token_total": len(single_token_positive) + len(single_token_negative),
        "n_single_nonzero_token_POSITIVE": len(single_token_positive),
        "n_single_nonzero_token_NEGATIVE": len(single_token_negative),
        "positive_examples_rejected_reason": [
            {"tx_hash": e["tx_hash"], "block": e["block"], "token": e["token"], "value_raw": e["value_raw"],
             "other_token_spend_by_executor": e["other_token_spend_by_executor"]}
            for e in single_token_positive
        ][:10],
        "negative_examples_sample": [
            {"tx_hash": e["tx_hash"], "block": e["block"], "token": e["token"], "value_raw": e["value_raw"]}
            for e in single_token_negative
        ][:5],
    }
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
