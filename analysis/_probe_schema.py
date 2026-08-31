#!/usr/bin/env python3
"""Диагностика: точные имена схем/таблиц Dune для Robinhood Chain /
Uniswap. Разовый инструмент — использовался, когда `sql/01_pool_creation_
blocks.sql` упал с "Schema 'uniswap_v3_robinhood_chain' does not exist"
(имя схемы декодированных контрактов оказалось другим, см.
docs/DATA_ACCESS.md). Держим в репозитории на случай, если Dune ещё раз
переименует/перестроит покрытие чейна и потребуется передиагностировать.

Запуск: python analysis/_probe_schema.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dune_client import DuneClient

PROBES = {
    "schemas_like_robinhood": """
        select table_schema, table_name
        from information_schema.tables
        where table_schema like '%robinhood%'
        order by 1, 2
        limit 200
    """,
    "schemas_like_uniswap_v3": """
        select table_schema, table_name
        from information_schema.tables
        where table_schema like '%uniswap_v3%robinhood%'
           or table_schema like '%robinhood%uniswap%'
        order by 1, 2
        limit 200
    """,
    "schemas_like_uniswap_v4": """
        select table_schema, table_name
        from information_schema.tables
        where table_schema like '%uniswap_v4%robinhood%'
           or table_schema like '%robinhood%uniswap%v4%'
        order by 1, 2
        limit 200
    """,
    "dex_trades_has_robinhood": """
        select count(*) as n, min(block_time) as min_t, max(block_time) as max_t
        from dex.trades
        where blockchain = 'robinhood_chain'
    """,
    "dex_trades_blockchain_values_like_robin": """
        select distinct blockchain
        from dex.trades
        where blockchain like '%robin%' or blockchain like '%hood%'
        limit 20
    """,
}


def main() -> int:
    client = DuneClient()
    for name, sql in PROBES.items():
        print(f"\n===== {name} =====")
        try:
            df = client.run_sql_cached(f"_probe_{name}", sql)
            print(df.to_string(max_rows=200))
        except Exception as e:  # noqa: BLE001 -- diagnostic script, want to see all failures
            print(f"FAILED: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
