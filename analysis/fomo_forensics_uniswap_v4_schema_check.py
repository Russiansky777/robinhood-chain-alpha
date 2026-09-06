#!/usr/bin/env python3
"""Форензика fomo, 2026-09-06 -- владелец, п.2: есть ли схема
`uniswap_v4_robinhood` и таблица Swap-событий в ней. Только
information_schema, без данных (бесплатно/дёшево -- метаданные, не
скан логов). Схема `uniswap_v4_robinhood` уже видна в реальном списке
distinct-схем `%robinhood%` (dune_mozila_schema_recon_result.json) --
здесь проверяем её РЕАЛЬНЫЕ таблицы/колонки, не просто факт
существования имени."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "fomo_forensics_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_uniswap_v4_schema_check_result.json")
NAMESPACE_BUDGET = 350.0
SCHEMA = "uniswap_v4_robinhood"


def run() -> int:
    ensure_namespace("fomo_forensics_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[v4_schema] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()

    sql_tables = f"""select table_name
from information_schema.tables
where table_schema = '{SCHEMA}'
order by table_name
limit 100"""
    qid1 = client.create_query("fomo_v4_tables", sql_tables)
    df1 = client.run_sql_cached("fomo_v4_tables", sql_tables, query_id=qid1,
                                 estimated_credits=2.0, expected_max_rows=100, expected_columns=1)
    tables = df1["table_name"].tolist() if df1 is not None and "table_name" in df1.columns else []
    print(f"[v4_schema] реальные таблицы схемы {SCHEMA}: {tables}")

    swap_tables = [t for t in tables if "swap" in t.lower()]
    print(f"[v4_schema] таблицы-кандидаты на Swap-события: {swap_tables}")

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "schema": SCHEMA, "n_tables": len(tables), "tables": tables,
                 "swap_table_candidates": swap_tables, "swap_columns": {}}

    for t in swap_tables[:3]:
        sql_cols = f"""select column_name, data_type
from information_schema.columns
where table_schema = '{SCHEMA}' and table_name = '{t}'
order by ordinal_position
limit 100"""
        qid2 = client.create_query(f"fomo_v4_cols_{t}"[:100], sql_cols)
        df2 = client.run_sql_cached(f"fomo_v4_cols_{t}"[:100], sql_cols, query_id=qid2,
                                     estimated_credits=2.0, expected_max_rows=100, expected_columns=2)
        cols = df2.to_dict("records") if df2 is not None else []
        print(f"[v4_schema]   {t}: {cols}")
        out["swap_columns"][t] = cols

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[v4_schema] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
