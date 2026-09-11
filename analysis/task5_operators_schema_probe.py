#!/usr/bin/env python3
"""Задача 5, измерение 2 (операторы) -- ДЁШЕВЫЙ information_schema-разведка
ПЕРЕД содержательным запросом: какие таблицы вообще есть для (а) байткод/
EOA-vs-контракт, (б) переводы/транзакции для поиска первого входящего
перевода (источник фондирования). НЕ гадаем про схему -- как и во всех
предыдущих Шагах 0 этой задачи."""
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

OUT_PATH = Path("data/p3_guard_cache/task5_operators_schema_probe_result.json")


def run() -> int:
    ensure_namespace("task5_active_arb_mozila", 1000.0)
    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def run_step(step_name: str, sql: str, est: float, max_rows: int = 100, max_cols: int = 10) -> list[dict]:
        qid = client.create_query(step_name, sql)
        df = client.run_sql_cached(step_name, sql, query_id=qid, estimated_credits=est,
                                    expected_max_rows=max_rows, expected_columns=max_cols)
        rows = df.to_dict("records") if df is not None else []
        print(f"[operators_schema] {step_name}: {len(rows)} строк")
        return rows

    print("=== 1. Таблицы схемы robinhood с 'contract'/'creation'/'code' в имени ===")
    sql1 = """select table_schema, table_name
from information_schema.tables
where lower(table_schema) like '%robinhood%'
  and (lower(table_name) like '%contract%' or lower(table_name) like '%creation%' or lower(table_name) like '%code%' or lower(table_name) like '%trace%')
order by table_schema, table_name
limit 100"""
    contract_tables = run_step("task5_op_contract_tables", sql1, 2.0, max_rows=100, max_cols=2)
    out["contract_table_candidates"] = contract_tables

    cols_by_table = {}
    for row in contract_tables[:6]:
        sch, t = row.get("table_schema"), row.get("table_name")
        if not sch or not t:
            continue
        sql_c = f"""select column_name, data_type
from information_schema.columns
where table_schema = '{sch}' and table_name = '{t}'
order by ordinal_position
limit 50"""
        cols = run_step(f"task5_op_cols_{sch}_{t}"[:100], sql_c, 2.0, max_rows=50, max_cols=2)
        cols_by_table[f"{sch}.{t}"] = cols
    out["contract_table_columns"] = cols_by_table

    print("\n=== 2. Реальные колонки robinhood.transactions (уже известны, но перепроверим value/nonce для funding) ===")
    sql2 = """select column_name, data_type
from information_schema.columns
where table_schema = 'robinhood' and table_name = 'transactions'
order by ordinal_position
limit 50"""
    tx_cols = run_step("task5_op_tx_cols", sql2, 2.0, max_rows=50, max_cols=2)
    out["robinhood_transactions_columns"] = tx_cols

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    out["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[operators_schema] итого: {total_cost:.2f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
