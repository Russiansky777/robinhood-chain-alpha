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

import pandas as pd

from dune_client import DuneClient
from run_pipeline import read_sql

FEE_SCHEMA_PROBE_SQL = read_sql("sc1/sc1_v1_fee_schema_probe")
UNISWAP_V3_TABLES_SQL = read_sql("sc1/sc1_uniswap_v3_tables")
POOL_CREATED_COLUMNS_SQL = read_sql("sc1/sc1_pool_created_columns")
POOL_FEES_SQL = read_sql("sc1/sc1_v1_pool_fees")

AUGUST_LAUNCHES_PATH = Path("data/sprintSC1_cache/sc1_august_launches_decoded.csv")


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

    print("\n===== sc1_pool_created_columns (оценка 3.0) =====")
    qid3 = client.create_query("sc1_pool_created_columns", POOL_CREATED_COLUMNS_SQL)
    df3 = client.run_sql_cached(
        "sc1_pool_created_columns", POOL_CREATED_COLUMNS_SQL, query_id=qid3, estimated_credits=3.0,
        expected_max_rows=20, expected_columns=3,
    )
    if df3 is not None and len(df3):
        print(df3.to_string())

    # Локально: все 39680 августовских V1-запусков используют один и
    # тот же launch_config_id=0/dex_id=0 -- вероятно, один fee-тир, но
    # проверяем ВСЕМИ PoolCreated в окне, не выборкой (владелец: "не
    # гадай").
    print("\n===== sc1_v1_pool_fees (все PoolCreated 01-13.08, оценка 8.0) =====")
    qid4 = client.create_query("sc1_v1_pool_fees", POOL_FEES_SQL)
    df4 = client.run_sql_cached(
        "sc1_v1_pool_fees", POOL_FEES_SQL, query_id=qid4, estimated_credits=8.0,
        expected_max_rows=200_000, expected_columns=4,
    )
    if df4 is not None and len(df4):
        print(f"Всего PoolCreated в окне: {len(df4)}")
        if AUGUST_LAUNCHES_PATH.exists():
            launches = pd.read_csv(AUGUST_LAUNCHES_PATH)
            our_pools = set(launches["pool"].str.lower())
            df4["pool_l"] = df4["pool"].astype(str).str.lower()
            ours = df4[df4["pool_l"].isin(our_pools)]
            print(f"Из них -- пулы наших августовских V1-запусков: {len(ours)} / {len(our_pools)}")
            print("Распределение fee-тиров среди НАШИХ пулов:")
            print(ours["fee"].value_counts().to_string())
        else:
            print("Распределение fee-тиров (все пулы в окне, без фильтра по нашим адресам):")
            print(df4["fee"].value_counts().to_string())
    else:
        print("(пусто -- 0 PoolCreated в окне)")

    print("\n[sc1_v1_fee_discovery] Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
