#!/usr/bin/env python3
"""Задача 5, Шаг 0c -- точечный дозапрос: реальные колонки Mint-события
v3 (`uniswapv3pool_evt_mint`) -- нужны для фильтра 3 владельца (2026-09-
11): "исключить транзакции, где evt_tx_from совпадает с адресом,
поставившим ликвидность в этот же пул". Дёшево, information_schema."""
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

OUT_PATH = Path("data/p3_guard_cache/task5_active_arb_stage0c_mint_schema_result.json")


def run() -> int:
    ensure_namespace("task5_active_arb_mozila", 700.0)
    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    sql_tables = """select table_name
from information_schema.tables
where table_schema = 'uniswap_v3_robinhood' and lower(table_name) like '%evt_mint%'
order by table_name
limit 20"""
    qid1 = client.create_query("task5_v3_mint_tables", sql_tables)
    df1 = client.run_sql_cached("task5_v3_mint_tables", sql_tables, query_id=qid1,
                                 estimated_credits=2.0, expected_max_rows=20, expected_columns=1)
    tables = df1["table_name"].tolist() if df1 is not None and len(df1) else []
    out["mint_tables"] = tables
    print(f"[stage0c] реальные Mint-таблицы: {tables}")

    cols_by_table = {}
    for t in tables[:2]:
        sql_cols = f"""select column_name, data_type
from information_schema.columns
where table_schema = 'uniswap_v3_robinhood' and table_name = '{t}'
order by ordinal_position
limit 50"""
        qid2 = client.create_query(f"task5_v3_mint_cols_{t}"[:100], sql_cols)
        df2 = client.run_sql_cached(f"task5_v3_mint_cols_{t}"[:100], sql_cols, query_id=qid2,
                                     estimated_credits=2.0, expected_max_rows=50, expected_columns=2)
        cols = df2.to_dict("records") if df2 is not None else []
        cols_by_table[t] = cols
        print(f"[stage0c] {t}: {cols}")
    out["mint_columns"] = cols_by_table

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    out["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[stage0c] итого: {total_cost:.2f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
