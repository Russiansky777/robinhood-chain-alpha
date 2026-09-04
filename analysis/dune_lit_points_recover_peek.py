#!/usr/bin/env python3
"""Восстановление двух peek-запросов из analysis/dune_lit_points_weekly.py,
которые реально ИСПОЛНИЛИСЬ и были оплачены (0.287 + 0.018 кредита,
execution_id 01M1PPC8WFQ2MP98QFT9C33Z3A / 01M1PPCF2RX741F128VW6MTWF5,
см. лог run 33899068539), но чтение результата отклонил гвард --
expected_columns был занижен (15/10 задекларировано, реально 22/12).
Через fetch_existing() читаем УЖЕ оплаченный execution_id заново
(без повторного execute -- не платим за исполнение дважды), с
исправленной декларацией колонок, чтобы увидеть реальные имена
столбцов перед тем, как писать агрегирующий запрос по отправителям."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "lit_points_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from dune_client import DuneClient

OUT_PATH = Path("data/p3_guard_cache/dune_lit_points_recover_peek_result.json")

TOKENS_TRANSFERS_EXEC = "01M1PPC8WFQ2MP98QFT9C33Z3A"  # 22 колонки реально
ERC20_EVT_TRANSFER_EXEC = "01M1PPCF2RX741F128VW6MTWF5"  # 12 колонок реально


def run() -> int:
    client = DuneClient()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": {}}

    for step_name, execution_id, expected_columns in (
        ("lit_peek_tokens_transfers_recovered", TOKENS_TRANSFERS_EXEC, 25),
        ("lit_peek_erc20_evt_transfer_recovered", ERC20_EVT_TRANSFER_EXEC, 15),
    ):
        try:
            df, status, stats = client.fetch_existing(
                execution_id, name=step_name, expected_max_rows=5, expected_columns=expected_columns,
            )
            result["steps"][step_name] = {
                "columns": list(df.columns), "rows": df.to_dict(orient="records"), "n_rows": len(df),
            }
            print(f"[lit_points_recover] {step_name}: колонки = {list(df.columns)}")
            print(df.to_string())
        except Exception as exc:  # noqa: BLE001
            result["steps"][step_name] = {"failed": True, "reason": str(exc)[:2000]}
            print(f"[lit_points_recover] {step_name} УПАЛ: {exc}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[lit_points_recover] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
