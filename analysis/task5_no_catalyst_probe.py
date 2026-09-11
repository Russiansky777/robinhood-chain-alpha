#!/usr/bin/env python3
"""Задача 5, дозапрос владельца 2026-09-11 ("без катализатора", шаг 0):
дёшево (information_schema) проверить реальные таблицы Burn (v3) и
модификации ликвидности (v4, PoolManager) ДО того, как строить полный
запрос расширения "проверить Mint/Burn" -- v3 Mint уже найден раньше
(task5_active_arb_stage0c_mint_schema.py, uniswapv3pool_evt_mint), но
Burn НЕ искался вообще, а v4 схема (uniswap_v4_robinhood) для событий
ликвидности не проверялась ни разу. Без этого шага пришлось бы гадать
имена таблиц/колонок в платном запросе -- ровно то, чего проект избегает
с самого начала (см. task5_active_arb_stage0_schema.py)."""
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

OUT_PATH = Path("data/p3_guard_cache/task5_no_catalyst_probe_result.json")
NAMESPACE_BUDGET = 1650.0


def run() -> int:
    ensure_namespace("task5_active_arb_mozila", NAMESPACE_BUDGET)
    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def run_step(step_name: str, sql: str, est: float, max_rows: int = 100, max_cols: int = 3) -> list[dict]:
        qid = client.create_query(step_name, sql)
        df = client.run_sql_cached(step_name, sql, query_id=qid, estimated_credits=est,
                                    expected_max_rows=max_rows, expected_columns=max_cols)
        rows = df.to_dict("records") if df is not None else []
        cost = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == step_name), None)
        print(f"[no_catalyst_probe] {step_name}: {len(rows)} строк, стоимость={cost}")
        return rows

    print("=== 1. v3 Burn-таблицы ===")
    sql_burn = """select table_name
from information_schema.tables
where table_schema = 'uniswap_v3_robinhood' and lower(table_name) like '%evt_burn%'
order by table_name
limit 20"""
    v3_burn_tables = run_step("task5_v3_burn_tables", sql_burn, 2.0, max_cols=1)
    out["v3_burn_tables"] = v3_burn_tables

    burn_cols_by_table = {}
    for row in v3_burn_tables[:2]:
        t = row.get("table_name")
        if not t:
            continue
        sql_cols = f"""select column_name, data_type
from information_schema.columns
where table_schema = 'uniswap_v3_robinhood' and table_name = '{t}'
order by ordinal_position
limit 50"""
        cols = run_step(f"task5_v3_burn_cols_{t}"[:100], sql_cols, 2.0, max_cols=2)
        burn_cols_by_table[t] = cols
    out["v3_burn_columns"] = burn_cols_by_table

    print("\n=== 2. Все таблицы схемы uniswap_v4_robinhood (для поиска модификации ликвидности) ===")
    sql_v4_tables = """select table_name
from information_schema.tables
where table_schema = 'uniswap_v4_robinhood'
order by table_name
limit 100"""
    v4_tables = run_step("task5_v4_all_tables", sql_v4_tables, 2.0, max_rows=100, max_cols=1)
    out["v4_all_tables"] = v4_tables

    liquidity_candidates = [
        row.get("table_name") for row in v4_tables
        if row.get("table_name") and any(
            kw in row["table_name"].lower() for kw in ("liquidity", "modify", "mint", "burn")
        )
    ]
    out["v4_liquidity_table_candidates"] = liquidity_candidates
    print(f"[no_catalyst_probe] v4 кандидаты на ликвидность: {liquidity_candidates}")

    v4_cols_by_table = {}
    for t in liquidity_candidates[:3]:
        sql_cols = f"""select column_name, data_type
from information_schema.columns
where table_schema = 'uniswap_v4_robinhood' and table_name = '{t}'
order by ordinal_position
limit 50"""
        cols = run_step(f"task5_v4_liq_cols_{t}"[:100], sql_cols, 2.0, max_cols=2)
        v4_cols_by_table[t] = cols
    out["v4_liquidity_columns"] = v4_cols_by_table

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    out["total_cost_credits"] = total_cost
    print(f"\n[no_catalyst_probe] ИТОГО: {total_cost:.2f} кредитов")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[no_catalyst_probe] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
