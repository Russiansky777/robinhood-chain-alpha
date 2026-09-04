#!/usr/bin/env python3
"""Владелец, 2026-09-04: разведка для запроса 3 (сток-токены на выходных
vs EDGAR 8-K) -- какие таблицы реально есть в rwa_robinhood /
rwa_stock_factory_robinhood, какие тикеры/адреса токенов задеплоены,
где их пулы (для цены/объёма своп-данных).

Отдельный бюджет: CREDIT_GUARD_NAMESPACE=funding_mozila_block2 (250
кредитов, владелец 2026-09-04, "на всё вышеперечисленное" -- НЕ
смешивается ни со старым разведочным (funding_mozila, 50), ни с
funding_mozila_content (300, запрос 1)).

Только чтение, LIMIT на каждом шаге (правило владельца).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "funding_mozila_block2")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace, load_state
from dune_client import DuneClient

OUT_PATH = Path("data/p3_guard_cache/dune_stock_token_recon_result.json")
BLOCK2_BUDGET = 250.0


def q(client: DuneClient, name: str, sql: str, estimated_credits: float,
      expected_max_rows: int = 200, expected_columns: int = 15) -> dict:
    try:
        df = client.run_sql_cached(
            name=name, sql=sql, estimated_credits=estimated_credits,
            expected_max_rows=expected_max_rows, expected_columns=expected_columns,
        )
        rows = df.to_dict(orient="records") if df is not None else None
        print(f"[stock_recon] {name}: {len(df) if df is not None else 0} строк")
        return {"rows": rows, "n_rows": len(df) if df is not None else 0}
    except SystemExit as exc:
        print(f"[stock_recon] {name} остановлен гвардом: {exc}")
        return {"stopped": True, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        print(f"[stock_recon] {name} УПАЛ: {exc}")
        return {"failed": True, "reason": str(exc)[:2000]}


def run() -> int:
    ensure_namespace("funding_mozila_block2", BLOCK2_BUDGET)
    client = DuneClient()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": {}}

    print("=== 1. Таблицы в rwa_robinhood / rwa_stock_factory_robinhood / midas_rwa_robinhood ===")
    tables_sql = """
    SELECT table_schema, table_name
    FROM information_schema.tables
    WHERE table_schema IN ('rwa_robinhood', 'rwa_stock_factory_robinhood', 'midas_rwa_robinhood')
    LIMIT 100
    """
    result["steps"]["rwa_tables"] = q(client, "stock_rwa_tables", tables_sql, 3.0, expected_max_rows=100, expected_columns=2)

    print("\n=== 2. Peek rwa_stock_factory_robinhood -- пробуем evt_* таблицы, ищем деплой токенов ===")
    factory_evt_sql = """
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = 'rwa_stock_factory_robinhood' AND LOWER(table_name) LIKE '%evt_%'
    LIMIT 100
    """
    result["steps"]["factory_evt_tables"] = q(
        client, "stock_factory_evt_tables", factory_evt_sql, 3.0, expected_max_rows=100, expected_columns=1,
    )

    total_spent = load_state()["funding_mozila_block2"]["spent"]
    print(f"\n=== Потрачено funding_mozila_block2: {total_spent:.3f} из {BLOCK2_BUDGET} ===")
    result["spent_this_run"] = total_spent
    result["remaining_budget"] = BLOCK2_BUDGET - total_spent

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[stock_recon] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
