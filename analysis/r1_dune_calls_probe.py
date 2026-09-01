#!/usr/bin/env python3
"""Sprint R1, Шаг 2: перед тем как городить RPC-путь для decimals()/
description() (ALCHEMY_API_KEY не настроен в секретах репозитория,
run #14 упал) -- проверяем, не декодировал ли Dune уже сами ВЫЗОВЫ
(не только события) на контрактах chainlink_robinhood. Spellbook
иногда генерирует `<contract>_call_<method>` таблицы из трейсов для
верифицированных ABI -- если да, closes decimals-вопрос и, возможно,
token<->feed сопоставление одним и тем же дешёвым запросом, без RPC.

Использование: python analysis/r1_dune_calls_probe.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintR1")
sys.path.insert(0, str(Path(__file__).parent))

from dune_client import DuneClient
from run_pipeline import read_sql

PROBE_SQL = read_sql("r1/r1_decimals_description_probe")
COLUMNS_PROBE_SQL = read_sql("r1/r1_calls_columns_probe")


def main() -> int:
    client = DuneClient()
    print("===== r1_decimals_description_probe (оценка 3.0) =====")
    qid = client.create_query("r1_decimals_description_probe", PROBE_SQL)
    df = client.run_sql_cached(
        "r1_decimals_description_probe", PROBE_SQL, query_id=qid, estimated_credits=3.0,
        expected_max_rows=500, expected_columns=2,
    )
    if df is None or not len(df):
        print("[r1_dune_calls_probe] Пусто -- Dune НЕ декодировал вызовы decimals()/"
              "description() на chainlink_robinhood. Нужен RPC-путь (Alchemy ключ "
              "не настроен -- см. run #14) либо инференс decimals по правдоподобию цены.")
        return 0

    print(df.to_string(max_rows=500))
    hit = df["table_name"].isin(["dualaggregator_call_decimals", "dualaggregator_call_description"])
    print(f"\n[r1_dune_calls_probe] Найдено {len(df)} таблиц-кандидатов, из них "
          f"decimals/description-совпадений: {hit.sum()}.")
    if not hit.any():
        return 0

    # run #16: dualaggregator_call_decimals и dualaggregator_call_description
    # НАЙДЕНЫ -- закрывают decimals-вопрос и token<->feed сопоставление БЕЗ
    # RPC. Колонки перед платным запросом агрегатов по ним.
    print("\n===== r1_calls_columns_probe (оценка 3.0) =====")
    qid2 = client.create_query("r1_calls_columns_probe", COLUMNS_PROBE_SQL)
    df2 = client.run_sql_cached(
        "r1_calls_columns_probe", COLUMNS_PROBE_SQL, query_id=qid2, estimated_credits=3.0,
        expected_max_rows=40, expected_columns=5,
    )
    if df2 is not None and len(df2):
        print(df2.to_string(max_rows=40))
    else:
        print("(пусто)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
