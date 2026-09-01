#!/usr/bin/env python3
"""Sprint R1, Шаг 2: rwa_robinhood.balances (run #21, курируемая
Dune-таблица с ui_multiplier/balance_usd/price_source -- явный маркер
настоящих RWA-контрактов) -- достаём (1) авторитетные адреса для наших
31 известных тикеров и (2) ПОЛНЫЙ реестр всех RWA-токенов, которые
Dune отслеживает (потенциальная замена факторному реестру
rwa_stock_factory_robinhood, который не покрывает флагманы -- run
#18/19).

Использование: python analysis/r1_rwa_authoritative.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintR1")
sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG
from dune_client import DuneClient
from run_pipeline import read_sql

CACHE_DIR = Path(CONFIG.r1_cache_dir)
AUTH_SQL = read_sql("r1/r1_rwa_authoritative_tokens")
UNIVERSE_SQL = read_sql("r1/r1_rwa_full_universe")


def main() -> int:
    client = DuneClient()

    print("===== r1_rwa_authoritative_tokens (оценка 3.0) =====")
    qid1 = client.create_query("r1_rwa_authoritative_tokens", AUTH_SQL)
    df1 = client.run_sql_cached(
        "r1_rwa_authoritative_tokens", AUTH_SQL, query_id=qid1, estimated_credits=3.0,
        expected_max_rows=60, expected_columns=4,
    )
    if df1 is not None and len(df1):
        print(df1.to_string(max_rows=60))
        print(f"\n[r1_rwa_authoritative] {df1['token_symbol'].nunique()} / 31 наших "
              f"известных тикеров найдены в rwa_robinhood.balances (авторитетно).")
    else:
        print("[r1_rwa_authoritative] Пусто -- наши 31 тикер вообще не в этой таблице.")

    print("\n===== r1_rwa_full_universe (оценка 15.0, calibration risk -- большая таблица) =====")
    qid2 = client.create_query("r1_rwa_full_universe", UNIVERSE_SQL)
    df2 = client.run_sql_cached(
        "r1_rwa_full_universe", UNIVERSE_SQL, query_id=qid2, estimated_credits=15.0,
        expected_max_rows=500, expected_columns=6,
    )
    if df2 is not None and len(df2):
        out2 = CACHE_DIR / "r1_rwa_full_universe.csv"
        df2.to_csv(out2, index=False)
        print(df2.to_string(max_rows=200))
        print(f"\n[r1_rwa_authoritative] Полный реестр RWA-токенов: {len(df2)} символов. "
              f"Записано: {out2}")
    else:
        print("[r1_rwa_authoritative] Пусто.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
