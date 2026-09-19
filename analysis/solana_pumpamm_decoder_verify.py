#!/usr/bin/env python3
"""Разовая: прогнать обновлённый engine.decode_tx (с поддержкой Pump.fun
AMM) на 3 известных транзакциях и сверить с эталоном -- честная
проверка перед тем, как встраивать декодер в остальной пайплайн.
Эталон: AKBot 65WA3e1VhP8Y... даёт 20 WSOL -> 14 263 324.112826 CC
(уже подтверждено балансами в data/solana_resolve_mint_probe_result.json)."""
from __future__ import annotations

import json
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_buyer200_fast_price import PRIOR_ROOT  # noqa: E402

sys.path.insert(0, str(PRIOR_ROOT))
import engine  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_pumpamm_decoder_verify_result.json"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

SIGS = {
    "leader": "JFPJEgfXH2PEfssK799nob9KE6J5Lk4PLcD1xRqqieTUP6rxksXTZAR7eA1jrVU7ejychsqwf6JvN2pd4M5hAqY",
    "akbot_known_20wsol_to_14263324cc": "65WA3e1VhP8Ynsyg1Jv7onsvDciZSFB7zNzwvpHXQ56gzg4KgqteQr57qKX8q8hTBehyW6mkx2E8sKN66oHQ1Q6t",
    "ours_batch3": "5QJGgyXEuYZQXqyqv2QEzzPdvBoQTN3whnSUt2qJsj6KbwR6SgkpXi9SU5tju2g3gRz3NtnjSxkcGXZmdAT19sZ6",
}
EXPECTED_AKBOT_CC = D("14263324.112826")
EXPECTED_AKBOT_WSOL = D("20")


def main() -> None:
    result = {}
    for label, sig in SIGS.items():
        tx = fp.get_transaction(sig)
        if tx is None:
            result[label] = {"HONEST_ANSWER": "getTransaction вернул null"}
            continue
        events = engine.decode_tx(tx)
        pamm_events = [e for e in events if e.get("kind") == "pamm"]
        row = {"signature": sig, "n_pamm_events": len(pamm_events), "events": pamm_events}
        if label == "akbot_known_20wsol_to_14263324cc" and pamm_events:
            e = pamm_events[0]
            d0, d1 = e.get("d0"), e.get("d1")
            if d0 is not None and d1 is not None:
                got_cc = D(e["base_amount_raw"]) / D(10) ** d1
                got_wsol = D(e["quote_amount_raw"]) / D(10) ** d0
                row["ground_truth_check"] = {
                    "expected_cc": str(EXPECTED_AKBOT_CC), "got_cc": str(got_cc), "cc_match": got_cc == EXPECTED_AKBOT_CC,
                    "expected_wsol": str(EXPECTED_AKBOT_WSOL), "got_wsol": str(got_wsol), "wsol_match": got_wsol == EXPECTED_AKBOT_WSOL,
                }
        result[label] = row
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
