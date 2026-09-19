#!/usr/bin/env python3
"""Владелец, 2026-09-20: перед тем, как писать полный запрос гейта C(a)
(баланс до/после в solana.transactions), дёшево смотрим РЕАЛЬНУЮ форму
pre_token_balances/post_token_balances/account_keys/signers на ОДНОЙ
известной транзакции лидера с гарантированно непустыми токен-балансами
(из наших же 300 покупок) -- чтобы не угадывать имена полей структуры
и не сжечь дорогой запрос на неверный SQL."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_balance_schema_probe.json"

# JLHP1V9e... -- реальная покупка лидера из selected_300.json, гарантированно
# меняет и SOL/native, и токен-баланс минта.
KNOWN_TX = "JLHP1V9ediM8QETprKpTAC7MbKxWrc8J6MFkraKR1BqCea8t54HQQ58pCnPuZahtGxCmpqakSvbBqJ1SnSf8oWm"
KNOWN_SLOT = 447870555


def main() -> None:
    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"step0_key_discovery": discovery}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    sql = (f"SELECT id, block_slot, account_keys, signers, pre_balances, post_balances, "
           f"pre_token_balances, post_token_balances FROM solana.transactions "
           f"WHERE block_slot BETWEEN {KNOWN_SLOT - 100} AND {KNOWN_SLOT + 100} AND id = '{KNOWN_TX}'")
    r = probe.run_sql_sync("balance_schema_probe", sql, timeout_s=120)
    result["dune_step"] = {k: v for k, v in r.items() if k != "rows"}
    if r.get("status") == "ok" and r.get("rows"):
        result["sample_row"] = r["rows"][0]
        result["column_names"] = (r.get("metadata") or {}).get("column_names")
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[schema_probe] status={r.get('status')} n_rows={r.get('n_rows')}", flush=True)
    if r.get("status") == "ok" and r.get("rows"):
        print(json.dumps(r["rows"][0], indent=2, default=str)[:3000], flush=True)


if __name__ == "__main__":
    main()
