#!/usr/bin/env python3
"""Задача 5, измерение 2 -- дозапрос: реальные колонки robinhood.contracts
и robinhood.creation_traces (найдены в task5_operators_schema_probe) --
нужны для честного EOA-vs-контракт различения топ-50 исполнителей."""
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

OUT_PATH = Path("data/p3_guard_cache/task5_operators_contracts_cols_result.json")


def run() -> int:
    ensure_namespace("task5_active_arb_mozila", 1000.0)
    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def run_step(step_name: str, sql: str, est: float, max_rows: int = 100, max_cols: int = 10) -> list[dict]:
        qid = client.create_query(step_name, sql)
        df = client.run_sql_cached(step_name, sql, query_id=qid, estimated_credits=est,
                                    expected_max_rows=max_rows, expected_columns=max_cols)
        rows = df.to_dict("records") if df is not None else []
        print(f"[op_cols] {step_name}: {len(rows)} строк")
        return rows

    for t in ("contracts", "creation_traces"):
        sql = f"""select column_name, data_type
from information_schema.columns
where table_schema = 'robinhood' and table_name = '{t}'
order by ordinal_position
limit 60"""
        cols = run_step(f"task5_op_{t}_cols", sql, 2.0, max_rows=60, max_cols=2)
        out[f"{t}_columns"] = cols

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    out["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[op_cols] итого: {total_cost:.2f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
