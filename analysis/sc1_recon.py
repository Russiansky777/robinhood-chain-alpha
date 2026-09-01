#!/usr/bin/env python3
"""Sprint SC1, Шаг 1: разведка (0 кредитов уже потрачено локально на
декодирование уже закэшированных TokenLaunched-логов -- см.
docs/SC1_NOTE.md). Первый платный запрос: имена таблиц в схеме
`robinhood` -- нужно найти таблицу нативных ETH-переводов/транзакций
для funding-parent склейки (Шаг 2).

Использование: python analysis/sc1_recon.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintSC1")
sys.path.insert(0, str(Path(__file__).parent))

from dune_client import DuneClient
from run_pipeline import read_sql

SCHEMA_DRILLDOWN_SQL = read_sql("sc1/sc1_schema_drilldown")


def main() -> int:
    client = DuneClient()

    print("===== sc1_schema_drilldown (оценка 3.0) =====")
    qid = client.create_query("sc1_schema_drilldown", SCHEMA_DRILLDOWN_SQL)
    df = client.run_sql_cached(
        "sc1_schema_drilldown", SCHEMA_DRILLDOWN_SQL, query_id=qid, estimated_credits=3.0,
        expected_max_rows=100, expected_columns=2,
    )
    if df is not None and len(df):
        print(df.to_string(max_rows=100))
    else:
        print("(пусто)")

    print("\n[sc1_recon] Готово. См. вывод выше -- нужна таблица нативных "
          "ETH-переводов/транзакций для funding-parent склейки (Шаг 2).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
