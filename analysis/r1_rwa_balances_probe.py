#!/usr/bin/env python3
"""Sprint R1, Шаг 2: run #20 показал 1780 строк для 31 "флагманского"
тикера в dex.trades -- скорее всего, много копий/пародий с тем же
именем (обычная практика мем-токенов). Проверяем rwa_robinhood.balances
(и enriched-варианты) -- вероятно, курируемый Dune Spellbook список
ТОЛЬКО настоящих RWA-контрактов Robinhood, без копий.

Использование: python analysis/r1_rwa_balances_probe.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintR1")
sys.path.insert(0, str(Path(__file__).parent))

from dune_client import DuneClient
from run_pipeline import read_sql

SQL = read_sql("r1/r1_rwa_balances_columns_probe")


def main() -> int:
    client = DuneClient()
    print("===== r1_rwa_balances_columns_probe (оценка 3.0) =====")
    qid = client.create_query("r1_rwa_balances_columns_probe", SQL)
    df = client.run_sql_cached(
        "r1_rwa_balances_columns_probe", SQL, query_id=qid, estimated_credits=3.0,
        expected_max_rows=100, expected_columns=5,
    )
    if df is not None and len(df):
        print(df.to_string(max_rows=100))
    else:
        print("(пусто)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
