#!/usr/bin/env python3
"""Sprint SC1: Шаг 2 (кластеризация) / Шаг 3-4 (экономика) / Шаг 5
(отчёт). См. docs/SC1_NOTE.md.

Использование: python analysis/sc1_pipeline.py --stage cluster|economics|report
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintSC1")
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

import credit_guard as cg
from dune_client import DuneClient
from run_pipeline import read_sql

CACHE_DIR = Path("data/sprintSC1_cache")
AUGUST_LAUNCHES_PATH = CACHE_DIR / "sc1_august_launches_decoded.csv"

TX_COLUMNS_SQL = read_sql("sc1/sc1_transactions_columns")
LAUNCH_TX_GAS_AGG_SQL = read_sql("sc1/sc1_v1_launch_tx_gas_agg")
FUNDING_PARENT_SQL = read_sql("sc1/sc1_funding_parent")


def sc1_spent() -> float:
    state = cg.load_state()
    return state.get("sprintSC1", {}).get("spent", 0.0)


def stage_cluster(client: DuneClient) -> int:
    print("===== sc1_transactions_columns (оценка 3.0) =====")
    qid = client.create_query("sc1_transactions_columns", TX_COLUMNS_SQL)
    df = client.run_sql_cached(
        "sc1_transactions_columns", TX_COLUMNS_SQL, query_id=qid, estimated_credits=3.0,
        expected_max_rows=60, expected_columns=3,
    )
    if df is not None and len(df):
        print(df.to_string())
    else:
        print("(пусто)")

    # run #6/#7: построчное чтение ~39680 строк x 7 колонок стоило бы
    # ~11.2 кредита чтения -- гард отказал ДО оплаты (правильно; execute
    # уже был оплачен -- 0.70, урок учтён). Владелец сам предусмотрел
    # такой случай (§1 Шаг1: "выборка/агрегат, если трейсы дороги") --
    # агрегат на стороне Dune вместо построчного чтения.
    print("\n===== sc1_v1_launch_tx_gas_agg (оценка 3.0, агрегат вместо построчного чтения) =====")
    qid2 = client.create_query("sc1_v1_launch_tx_gas_agg", LAUNCH_TX_GAS_AGG_SQL)
    df2 = client.run_sql_cached(
        "sc1_v1_launch_tx_gas_agg", LAUNCH_TX_GAS_AGG_SQL, query_id=qid2, estimated_credits=3.0,
        expected_max_rows=2, expected_columns=10,
    )
    if df2 is None or not len(df2):
        print("[sc1_pipeline] ПУСТО -- нет транзакций к фабрике в окне. Стоп.")
        return 1

    row = df2.iloc[0]
    print(f"[sc1_pipeline] Транзакций к PonsLaunchFactory V1 в окне: {row['n_tx']} "
          f"успешных={row['n_success']} (ожидали ~39680 успешных).")
    print(f"[sc1_pipeline] launchFee: {row['n_nonzero_value']} из {row['n_success']} успешных транзакций "
          f"с value > 0 ({'НЕНУЛЕВОЙ' if row['n_nonzero_value'] else '= 0 у всех'}).")
    if row["n_nonzero_value"]:
        print(f"  value (native) при ненулевых: median={row['value_median_when_nonzero']}, "
              f"max={row['value_max']}")
    print(f"[sc1_pipeline] gas_used: median={row['gas_used_median']}, mean={row['gas_used_mean']:.1f}, "
          f"min={row['gas_used_min']}, max={row['gas_used_max']}")
    print(f"[sc1_pipeline] gas_price ФАКТИЧЕСКИЙ (в период вейвера, НЕ пост-вейверная цена -- "
          f"критерий требует другую, см. далее): median={row['gas_price_median']}")

    out_file = CACHE_DIR / "sc1_v1_launch_tx_gas_agg.csv"
    df2.to_csv(out_file, index=False)
    client._commit_permanent(out_file, f"sprintSC1_cache: агрегат gas/value по транзакциям launch() V1 [automated]")
    print(f"[sc1_pipeline] Записано: {out_file}")

    remaining = 20.0 - sc1_spent()
    print(f"\n[sc1_pipeline] Остаток бюджета SC1 после gas-агрегата: {remaining:.2f} из 20.0.")

    # Уровень 2: funding parent. Дороже (JOIN transactions x transactions),
    # но выдача -- только 2 колонки (не 7, как в неудачном run #6/#7) --
    # читаем ~14.5k строк x 2 колонки, оценка чтения ~1.2 кредита, не ~11.
    print("\n===== sc1_funding_parent (оценка 8.0, JOIN, урезанная выдача) =====")
    qid3 = client.create_query("sc1_funding_parent", FUNDING_PARENT_SQL)
    df3 = client.run_sql_cached(
        "sc1_funding_parent", FUNDING_PARENT_SQL, query_id=qid3, estimated_credits=8.0,
        expected_max_rows=20_000, expected_columns=2,
    )
    if df3 is None or not len(df3):
        print("[sc1_pipeline] ПУСТО -- funding-parent не найден ни для одного деплоера "
              "(либо все финансировались до 01.07, либо джойн не сработал). "
              "Уровень 2 (склейка кластеров) невозможен без него -- STOP.")
        return 1

    print(f"[sc1_pipeline] funding_parent найден для {len(df3)} деплоеров.")
    out_file = CACHE_DIR / "sc1_funding_parent.csv"
    df3.to_csv(out_file, index=False)
    client._commit_permanent(out_file, f"sprintSC1_cache: funding_parent по деплоерам [automated]")
    print(f"[sc1_pipeline] Записано: {out_file}")

    remaining2 = 20.0 - sc1_spent()
    print(f"\n[sc1_pipeline] Остаток бюджета SC1 после Шага 2: {remaining2:.2f} из 20.0.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=["cluster", "economics", "report"])
    args = parser.parse_args()

    client = DuneClient()

    if args.stage == "cluster":
        return stage_cluster(client)
    elif args.stage == "economics":
        print("[sc1_pipeline] economics: не реализовано в этом коммите.")
        return 1
    else:
        print("[sc1_pipeline] report: не реализовано в этом коммите.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
