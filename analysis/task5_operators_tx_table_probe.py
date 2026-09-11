#!/usr/bin/env python3
"""Задача 5, измерение 2 -- дешёвая проверка реального размера/диапазона
robinhood.transactions ПЕРЕД поиском первого входящего перевода для
топ-50 исполнителей. Урок форензики fomo (см. credit_guard.py, п.2):
OR/IN против многих адресов НЕ гарантированно даёт хэш-джойн -- нужно
знать реальный объём таблицы, прежде чем писать джойн по 50 адресам
без ограничения по дате."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task5_active_arb_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_operators_tx_table_probe_result.json")


def run() -> int:
    ensure_namespace("task5_active_arb_mozila", 1650.0)
    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    sql = """select min(block_date) as min_date, max(block_date) as max_date, approx_distinct(block_date) as n_distinct_days
from robinhood.transactions"""
    qid = client.create_query("task5_op_tx_daterange", sql)
    df = client.run_sql_cached("task5_op_tx_daterange", sql, query_id=qid,
                                estimated_credits=15.0, expected_max_rows=5, expected_columns=3)
    rows = df.to_dict("records") if df is not None else []
    out["date_range"] = rows
    print(f"[tx_probe] диапазон дат: {rows}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    out["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[tx_probe] итого: {total_cost:.2f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
