#!/usr/bin/env python3
"""Sprint R1, Шаг 2: КРИТИЧНОЕ расхождение (run #18) -- 23 реально
торгуемых токена (§2.2) НЕ пересекаются ни по одному тикеру с 31
активным Chainlink-фидом. Проверяем схему rwa_robinhood (упомянута в
run #9 рядом с rwa_stock_factory_robinhood) на наличие decoded
таблиц, которые могли бы дать АВТОРИТЕТНУЮ связь токен->фид напрямую
с контракта, вместо ненадёжного сопоставления по текстовому тикеру.

Использование: python analysis/r1_rwa_schema_probe.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintR1")
sys.path.insert(0, str(Path(__file__).parent))

from dune_client import DuneClient
from run_pipeline import read_sql

PROBE_SQL = read_sql("r1/r1_rwa_robinhood_schema_probe")


def main() -> int:
    client = DuneClient()
    print("===== r1_rwa_robinhood_schema_probe (оценка 3.0) =====")
    qid = client.create_query("r1_rwa_robinhood_schema_probe", PROBE_SQL)
    df = client.run_sql_cached(
        "r1_rwa_robinhood_schema_probe", PROBE_SQL, query_id=qid, estimated_credits=3.0,
        expected_max_rows=500, expected_columns=2,
    )
    if df is not None and len(df):
        print(df.to_string(max_rows=500))
        print(f"\n[r1_rwa_schema_probe] Найдено {len(df)} таблиц в rwa_robinhood.")
    else:
        print("[r1_rwa_schema_probe] Пусто -- схема rwa_robinhood не существует или недоступна.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
