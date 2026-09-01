#!/usr/bin/env python3
"""Sprint SC1: владелец, уточнение -- НЕ переносить V2-цифру комиссии
(0.7%) на V1. Источник (GitHub, contractsV1/src/PonsLaunchFactory.sol,
запрошено дословно): V1 creator revenue = "trading fees on the locked
position" -- LP-комиссии на ОДНОСТОРОННЕЙ Uniswap V3 позиции создателя
(полный саплай с первого блока). Fee-тир -- поле `poolFee` (uint24) в
DexConfig, КОНФИГУРИРУЕМОЕ per-launch (не единая константа, как V2).
Плюс `launchFee` -- плоский native-сбор в пользу protocolFeeRecipient
(издержка создателя, не доход).

Этот скрипт ищет, откуда взять ФАКТИЧЕСКИЙ fee-тир пулов V1-запусков
августа (а не гадать) -- метаданные схем, дёшево.

Использование: python analysis/sc1_v1_fee_discovery.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintSC1")
sys.path.insert(0, str(Path(__file__).parent))

from dune_client import DuneClient
from run_pipeline import read_sql

FEE_SCHEMA_PROBE_SQL = read_sql("sc1/sc1_v1_fee_schema_probe")
UNISWAP_V3_TABLES_SQL = read_sql("sc1/sc1_uniswap_v3_tables")


def main() -> int:
    client = DuneClient()

    print("===== sc1_v1_fee_schema_probe (dex.trades колонки, оценка 3.0) =====")
    qid = client.create_query("sc1_v1_fee_schema_probe", FEE_SCHEMA_PROBE_SQL)
    df = client.run_sql_cached(
        "sc1_v1_fee_schema_probe", FEE_SCHEMA_PROBE_SQL, query_id=qid, estimated_credits=3.0,
        expected_max_rows=30, expected_columns=5,
    )
    if df is not None and len(df):
        print(df.to_string())
    else:
        print("(пусто)")

    print("\n===== sc1_uniswap_v3_tables (оценка 3.0) =====")
    qid2 = client.create_query("sc1_uniswap_v3_tables", UNISWAP_V3_TABLES_SQL)
    df2 = client.run_sql_cached(
        "sc1_uniswap_v3_tables", UNISWAP_V3_TABLES_SQL, query_id=qid2, estimated_credits=3.0,
        expected_max_rows=100, expected_columns=2,
    )
    if df2 is not None and len(df2):
        print(df2.to_string(max_rows=100))
    else:
        print("(пусто)")

    print("\n[sc1_v1_fee_discovery] Готово. Ищем таблицу с колонкой fee "
          "(uint24, тир пула) по адресу пула -- следующий шаг: агрегат "
          "фактических fee-тиров по пулам V1-запусков августа.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
