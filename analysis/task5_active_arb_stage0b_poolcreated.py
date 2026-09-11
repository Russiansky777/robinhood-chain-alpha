#!/usr/bin/env python3
"""Задача 5, Шаг 0b -- точечный дозапрос: реальные колонки
`uniswap_v3_robinhood.uniswapv3factory_evt_poolcreated` (Шаг 0 взял не
те 3 factory-таблицы по алфавиту -- call_createpool/enablefeeamount/
feeamounttickspacing, а не сам PoolCreated event) + реальное число
строк (нужно понять, стоит ли материализовать всю таблицу для join)."""
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

OUT_PATH = Path("data/p3_guard_cache/task5_active_arb_stage0b_poolcreated_result.json")


def run() -> int:
    ensure_namespace("task5_active_arb_mozila", 700.0)
    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    sql_cols = """select column_name, data_type
from information_schema.columns
where table_schema = 'uniswap_v3_robinhood' and table_name = 'uniswapv3factory_evt_poolcreated'
order by ordinal_position
limit 100"""
    qid1 = client.create_query("task5_v3_poolcreated_columns", sql_cols)
    df1 = client.run_sql_cached("task5_v3_poolcreated_columns", sql_cols, query_id=qid1,
                                 estimated_credits=2.0, expected_max_rows=100, expected_columns=2)
    cols = df1.to_dict("records") if df1 is not None else []
    out["poolcreated_columns"] = cols
    print(f"[stage0b] uniswapv3factory_evt_poolcreated колонки: {cols}")

    sql_count = "select count(*) as n from uniswap_v3_robinhood.uniswapv3factory_evt_poolcreated"
    qid2 = client.create_query("task5_v3_poolcreated_count", sql_count)
    df2 = client.run_sql_cached("task5_v3_poolcreated_count", sql_count, query_id=qid2,
                                 estimated_credits=3.0, expected_max_rows=5, expected_columns=1)
    n = int(df2["n"].iloc[0]) if df2 is not None and len(df2) else None
    out["n_pools_created"] = n
    print(f"[stage0b] реальное число созданных v3-пулов: {n}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    out["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[stage0b] итого потрачено: {total_cost:.2f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
