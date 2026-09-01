#!/usr/bin/env python3
"""Sprint R1, Шаг 2 (переигровка Шага 1 recon с правильной вселенной):
run #18-22 показали, что факторный реестр rwa_stock_factory_robinhood
(102 токена, использованный в Шаге 1) НЕ покрывает флагманские тикеры
с активными Chainlink-фидами (AAPL, TSLA, NVDA...) -- 0 пересечений.
Авторитетный источник -- rwa_robinhood.balances (курируемая
Dune-таблица, ui_multiplier/balance_usd/price_source): 194 реальных
RWA-токена с holders/адресами. Перегоняем гейт §2.2/§1 на этой
вселенной.

Использование: python analysis/r1_recon_v2.py
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintR1")
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from config import CONFIG
from dune_client import DuneClient, render_sql
from run_pipeline import read_sql

CACHE_DIR = Path(CONFIG.r1_cache_dir)
TMPL = read_sql("r1/r1_universe_trades_v2")


def _latest(glob_pat: str) -> Path:
    matches = sorted(glob.glob(str(CACHE_DIR / glob_pat)))
    if not matches:
        raise FileNotFoundError(f"Не найден кэш по шаблону {glob_pat} в {CACHE_DIR}")
    return Path(matches[-1])


def main() -> int:
    universe = pd.read_csv(_latest("r1_rwa_full_universe.csv") if
                            (CACHE_DIR / "r1_rwa_full_universe.csv").exists()
                            else _latest("r1_rwa_full_universe_*.csv"))
    addr_list = ", ".join(f"0x{str(a).lower().replace('0x', '')}" for a in universe["token_address"])
    sql = render_sql(TMPL, {"token_address_list": addr_list})

    client = DuneClient()
    print(f"===== r1_universe_trades_v2 ({len(universe)} авторитетных RWA-токенов, оценка 15.0) =====")
    qid = client.create_query("r1_universe_trades_v2", sql)
    df = client.run_sql_cached(
        "r1_universe_trades_v2", sql, query_id=qid, estimated_credits=15.0,
        expected_max_rows=250, expected_columns=4,
    )
    if df is None or not len(df):
        print("[r1_recon_v2] Пусто -- ни один авторитетный RWA-токен не торгуется в dex.trades.")
        return 1

    merged = df.merge(universe[["token_address", "token_symbol"]], on="token_address", how="left")
    out = CACHE_DIR / "r1_universe_trades_v2.csv"
    merged.to_csv(out, index=False)
    print(merged.sort_values("vol_usd", ascending=False).to_string(max_rows=250))

    n_pass22 = ((merged["n_trades"] >= 100) & (merged["vol_usd"] >= 10_000)).sum()
    total_closed = merged["n_trades_closed_hours"].sum()
    total_vol = merged["vol_usd"].sum()
    print(f"\n[r1_recon_v2] ГЕЙТ РАЗВЕДКИ (переигровка): {merged['token_symbol'].nunique()} / "
          f"{len(universe)} авторитетных RWA-токенов торгуются. Проходят §2.2 (>=100 сделок И "
          f">=$10k): {n_pass22} (нужно >=15). Сделок в закрытые часы всего: {total_closed} "
          f"(нужно >=50). Суммарный объём: ${total_vol:,.2f}.")
    if n_pass22 >= 15 and total_closed >= 50:
        print("[r1_recon_v2] ГЕЙТ ПРОЙДЕН (на правильной вселенной).")
    else:
        print("[r1_recon_v2] ГЕЙТ НЕ ПРОЙДЕН.")

    # Пересечение с 31 известными активными фидами -- какие из ЛИКВИДНЫХ
    # токенов реально имеют фид для anchor F(i,t) (§2.3).
    feed_symbols = {'AAPL','TSLA','NVDA','MSFT','AMZN','META','GOOGL','COIN',
        'PLTR','MSTR','GME','AMD','INTC','MU','ORCL','RGTI','RKLB','IONQ','CRCL','SLV','SGOV',
        'EWY','QQQ','USAR','DELL','CLSK','NBIS','CRWV','USO','SNDK','SPY'}
    pass22_df = merged[(merged["n_trades"] >= 100) & (merged["vol_usd"] >= 10_000)]
    with_feed = pass22_df[pass22_df["token_symbol"].isin(feed_symbols)]
    print(f"\n[r1_recon_v2] Из прошедших §2.2 токенов -- с известным активным Chainlink-фидом "
          f"(нужен для anchor §2.3): {len(with_feed)} / {len(pass22_df)}.")
    print(with_feed[["token_symbol", "token_address", "n_trades", "vol_usd", "n_trades_closed_hours"]]
          .sort_values("vol_usd", ascending=False).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
