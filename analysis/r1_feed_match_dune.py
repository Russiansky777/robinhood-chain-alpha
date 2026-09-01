#!/usr/bin/env python3
"""Sprint R1, Шаг 2: token <-> Chainlink-фид, decimals -- ПОЛНОСТЬЮ на
Dune (decoded call-трейсы chainlink_robinhood.dualaggregator_call_
decimals/description, найдены run #17), БЕЗ RPC (ALCHEMY_API_KEY не
настроен в секретах репозитория -- см. run #14,
analysis/r1_feed_match.py остаётся нерабочим фолбэком на случай, если
ключ появится).

Использование: python analysis/r1_feed_match_dune.py
"""
from __future__ import annotations

import glob
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintR1")
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from config import CONFIG
from dune_client import DuneClient
from run_pipeline import read_sql

CACHE_DIR = Path(CONFIG.r1_cache_dir)
FEED_DECIMALS_DESC_SQL = read_sql("r1/r1_feed_decimals_description")


def _latest(glob_pat: str) -> Path:
    matches = sorted(glob.glob(str(CACHE_DIR / glob_pat)))
    if not matches:
        raise FileNotFoundError(f"Не найден кэш по шаблону {glob_pat} в {CACHE_DIR}")
    return Path(matches[-1])


def extract_ticker(desc) -> str | None:
    if not isinstance(desc, str) or not desc:
        return None
    m = re.match(r"\s*([A-Za-z.]+)\s*[/\-]", desc)
    return m.group(1).upper() if m else desc.strip().upper()


def main() -> int:
    client = DuneClient()
    print("===== r1_feed_decimals_description (оценка 5.0) =====")
    qid = client.create_query("r1_feed_decimals_description", FEED_DECIMALS_DESC_SQL)
    df = client.run_sql_cached(
        "r1_feed_decimals_description", FEED_DECIMALS_DESC_SQL, query_id=qid, estimated_credits=5.0,
        expected_max_rows=200, expected_columns=4,
    )
    if df is None or not len(df):
        print("[r1_feed_match_dune] Пусто -- ни один decimals()/description() вызов "
              "не декодирован (возможно, эти функции никогда не вызывались ИЗ другого "
              "контракта в транзакции, только off-chain eth_call, который трейсы не видят). "
              "Нужен RPC-путь или инференс decimals по правдоподобию цены.")
        return 1

    out_raw = CACHE_DIR / "r1_feed_decimals_description.csv"
    df.to_csv(out_raw, index=False)
    print(df.to_string(max_rows=200))
    print(f"\n[r1_feed_match_dune] Записано: {out_raw}")

    n_feeds = df["feed_address"].nunique()
    n_with_desc = df["description"].notna().sum()
    decimals_vals = df["decimals"].dropna().unique()
    print(f"\n[r1_feed_match_dune] Уникальных фидов с decoded decimals(): {n_feeds}. "
          f"С decoded description(): {n_with_desc}. Наблюдаемые значения decimals(): "
          f"{sorted(decimals_vals)}.")

    tokens = pd.read_csv(_latest("r1_stock_token_deployments_*.csv"))
    tokens["symbol_upper"] = tokens["symbol"].str.upper()
    df["ticker"] = df["description"].apply(extract_ticker)

    merged = df.merge(tokens, left_on="ticker", right_on="symbol_upper", how="inner")
    out_map = CACHE_DIR / "r1_feed_token_map.csv"
    merged[["token_address", "symbol", "feed_address", "decimals", "description"]].drop_duplicates(
        subset=["token_address"]
    ).to_csv(out_map, index=False)

    print(f"\n[r1_feed_match_dune] Сопоставлено {merged['token_address'].nunique()} токенов "
          f"из {n_feeds} фидов с decoded description(). Записано: {out_map}")
    if len(merged):
        print(merged[["symbol", "feed_address", "decimals", "description"]]
              .drop_duplicates().to_string(index=False))
    return 0 if len(merged) else 1


if __name__ == "__main__":
    raise SystemExit(main())
