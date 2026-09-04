#!/usr/bin/env python3
"""Владелец, 2026-09-04 (скринер пулов, п.3): wash_share через Dune для
3 кандидатов, прошедших порог -- ВСЕ на Base (aerodrome-slipstream x2,
uniswap-v3 x1). Реальные имена схем/таблиц Dune для Base Uniswap v3 /
Aerodrome Slipstream НЕ известны заранее -- не угадываем по аналогии с
Robinhood Chain (`uniswap_v3_robinhood.uniswapv3pool_evt_swap`), схема
именования может отличаться для мультичейн-проекта. LIMIT 100 разведка
(владелец, правило "сначала LIMIT 100"), тот же namespace/бюджет, что
funding_mozila_block2 (Query 3 / скринер), ~248/250 оставалось на
момент запуска."""
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

OUT_PATH = Path("data/p3_guard_cache/dune_base_schema_discovery_result.json")


def q(client: DuneClient, name: str, sql: str, estimated_credits: float,
      expected_max_rows: int = 100, expected_columns: int = 10) -> dict:
    try:
        df = client.run_sql_cached(name=name, sql=sql, estimated_credits=estimated_credits,
                                    expected_max_rows=expected_max_rows, expected_columns=expected_columns)
        rows = df.to_dict(orient="records") if df is not None else None
        print(f"[base_discovery] {name}: {len(df) if df is not None else 0} строк")
        if df is not None and len(df):
            print(df.to_string())
        return {"rows": rows, "n_rows": len(df) if df is not None else 0}
    except SystemExit as exc:
        print(f"[base_discovery] {name} остановлен гвардом: {exc}")
        return {"stopped": True, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        print(f"[base_discovery] {name} УПАЛ: {exc}")
        return {"failed": True, "reason": str(exc)[:2000]}


def run() -> int:
    ensure_namespace("funding_mozila_block2", 250.0)
    client = DuneClient()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": {}}

    print("=== 1. Схемы, похожие на 'base' + 'uniswap' ===")
    result["steps"]["uniswap_base_schemas"] = q(client, "base_uniswap_schemas", """
        SELECT DISTINCT table_schema
        FROM information_schema.tables
        WHERE LOWER(table_schema) LIKE '%uniswap%' AND LOWER(table_schema) LIKE '%base%'
        LIMIT 100
    """, 2.0)

    print("\n=== 2. Схемы, похожие на 'aerodrome' ===")
    result["steps"]["aerodrome_schemas"] = q(client, "aerodrome_schemas", """
        SELECT DISTINCT table_schema
        FROM information_schema.tables
        WHERE LOWER(table_schema) LIKE '%aerodrome%'
        LIMIT 100
    """, 2.0)

    print("\n=== 3. Узкий поиск evt_swap среди найденных кандидатов (после просмотра шага 1-2 в логе) ===")
    # Первый прогон -- только 1 и 2 (дёшево, узнаём реальные имена схем).
    # Точечный evt_swap-запрос -- отдельным скриптом после того, как эти
    # два шага покажут реальные названия (не гадаем заранее).

    state = load_state()
    print(f"\n=== Остаток funding_mozila_block2: {250.0 - state['funding_mozila_block2']['spent']:.2f} из 250 ===")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
