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
LAUNCH_TX_GAS_SQL = read_sql("sc1/sc1_v1_launch_tx_gas")


def sc1_spent() -> float:
    state = cg.load_state()
    return state.get("sprintSC1", {}).get("spent", 0.0)


def stage_cluster(client: DuneClient) -> int:
    print("===== sc1_transactions_columns (оценка 3.0) =====")
    qid = client.create_query("sc1_transactions_columns", TX_COLUMNS_SQL)
    df = client.run_sql_cached(
        "sc1_transactions_columns", TX_COLUMNS_SQL, query_id=qid, estimated_credits=3.0,
        expected_max_rows=30, expected_columns=3,
    )
    if df is not None and len(df):
        print(df.to_string())

    print("\n===== sc1_v1_launch_tx_gas (оценка 8.0) =====")
    qid2 = client.create_query("sc1_v1_launch_tx_gas", LAUNCH_TX_GAS_SQL)
    df2 = client.run_sql_cached(
        "sc1_v1_launch_tx_gas", LAUNCH_TX_GAS_SQL, query_id=qid2, estimated_credits=8.0,
        expected_max_rows=45_000, expected_columns=7,
    )
    if df2 is None or not len(df2):
        print("[sc1_pipeline] ПУСТО -- нет транзакций к фабрике в окне. Стоп.")
        return 1

    print(f"[sc1_pipeline] Транзакций к PonsLaunchFactory V1 в окне: {len(df2)} "
          f"(ожидали ~39680, если что-то ещё шло в этот контракт помимо launch()).")
    n_nonzero_value = (df2["value"].astype(float) > 0).sum()
    print(f"[sc1_pipeline] launchFee: {n_nonzero_value} из {len(df2)} транзакций с value > 0 "
          f"({'launchFee, похоже, НЕНУЛЕВОЙ' if n_nonzero_value > 0 else 'launchFee = 0 у всех проверенных'}).")
    if n_nonzero_value > 0:
        nz = df2[df2["value"].astype(float) > 0]["value"].astype(float)
        print(f"  value (native) при ненулевых: min={nz.min()}, median={nz.median()}, max={nz.max()}")
    print(f"[sc1_pipeline] gas_used: median={df2['gas_used'].median()}, "
          f"mean={df2['gas_used'].mean():.1f}, min={df2['gas_used'].min()}, max={df2['gas_used'].max()}")
    print(f"[sc1_pipeline] gas_price ФАКТИЧЕСКИЙ (в период вейвера, НЕ пост-вейверная цена -- "
          f"критерий требует другую): median={df2['gas_price'].median()}")

    out_file = CACHE_DIR / "sc1_v1_launch_tx_gas_merged.csv"
    df2.to_csv(out_file, index=False)
    client._commit_permanent(out_file, f"sprintSC1_cache: gas/value по транзакциям launch() V1 [automated]")
    print(f"[sc1_pipeline] Записано: {out_file}")

    remaining = 20.0 - sc1_spent()
    print(f"\n[sc1_pipeline] Остаток бюджета SC1 после этого шага: {remaining:.2f} из 20.0.")
    print("[sc1_pipeline] Funding-parent (Уровень 2) -- следующий вызов, отдельно "
          "(бюджетная проекция джойна transactions x transactions не сделана заранее, "
          "запрос дороже -- проверить остаток перед вызовом).")
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
