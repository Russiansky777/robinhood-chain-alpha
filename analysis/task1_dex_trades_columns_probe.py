#!/usr/bin/env python3
"""Задача 1: реальная проверка типов колонок dex.trades (0/минимальные
кредиты, метаданные) -- см. sql/task1/task1_dex_trades_columns_probe.sql,
докстринг там же."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task1_weekend_gap")
sys.path.insert(0, str(Path(__file__).parent))

from dune_client import DuneClient  # noqa: E402
from run_pipeline import read_sql  # noqa: E402
import credit_guard  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task1_dex_trades_columns_probe_result.json")


def run() -> int:
    credit_guard.ensure_namespace("task1_weekend_gap", 250.0)
    sql = read_sql("task1/task1_dex_trades_columns_probe")
    client = DuneClient()
    qid = client.create_query("task1_dex_trades_columns_probe", sql)
    df = client.run_sql_cached("task1_dex_trades_columns_probe", sql, query_id=qid,
                                 estimated_credits=1.0, expected_max_rows=50, expected_columns=5)
    if df is None or not len(df):
        print("[probe] пусто -- dex.trades не найден в information_schema? Разобраться.")
        return 1
    print(df.to_string(index=False))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(df.to_json(orient="records", indent=2))
    print(f"\n[probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
