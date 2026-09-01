#!/usr/bin/env python3
"""Sprint R1, Шаг 2: КРИТИЧНОЕ расхождение (run #18/19) -- проверяем
торговлю "флагманскими" тикерами (те, у кого есть активный Chainlink-
фид: AAPL, TSLA, NVDA...) НАПРЯМУЮ по symbol в dex.trades, минуя
ончейн-реестр rwa_stock_factory_robinhood (который эти токены,
похоже, не покрывает -- 0 пересечений по тикеру, см. run #18/19).

Использование: python analysis/r1_flagship_recon.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintR1")
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from config import CONFIG
from dune_client import DuneClient
from run_pipeline import read_sql

SQL = read_sql("r1/r1_flagship_trades_by_symbol")


def main() -> int:
    client = DuneClient()
    print("===== r1_flagship_trades_by_symbol (оценка 8.0) =====")
    qid = client.create_query("r1_flagship_trades_by_symbol", SQL)
    df = client.run_sql_cached(
        "r1_flagship_trades_by_symbol", SQL, query_id=qid, estimated_credits=8.0,
        expected_max_rows=2000, expected_columns=5,
    )
    if df is None or not len(df):
        print("[r1_flagship_recon] Пусто -- флагманские тикеры (с фидами) ВООБЩЕ не "
              "торгуются в dex.trades за весь период. Это означало бы, что реальная "
              "торговля идёт ТОЛЬКО по токенам из factory-реестра (без фидов), а "
              "фиды покрывают токены, которые никто не торгует -- гейт разведки "
              "фактически провален (нет пересечения объём+фид ни в одну сторону).")
        return 1

    out_path = Path(CONFIG.r1_cache_dir) / "r1_flagship_trades_by_symbol.csv"
    df.to_csv(out_path, index=False)
    print(f"[r1_flagship_recon] {len(df)} строк (symbol x token_address) -- "
          f"МНОГО адресов на символ (копии/пародии) -- см. {out_path}.")

    # По символу: суммарный объём/сделки (все адреса вместе) + топ-адрес
    # по объёму (вероятный кандидат на "настоящий" контракт, требует
    # отдельной проверки против rwa_robinhood -- не принимается на веру).
    by_symbol = df.groupby("symbol").agg(
        n_addresses=("token_address", "nunique"),
        n_trades=("n_trades", "sum"),
        vol_usd=("vol_usd", "sum"),
        n_trades_closed_hours=("n_trades_closed_hours", "sum"),
    ).sort_values("vol_usd", ascending=False)
    top_addr = df.sort_values("vol_usd", ascending=False).drop_duplicates("symbol").set_index("symbol")["token_address"]
    by_symbol["top_address_by_vol"] = top_addr
    print("\n-- По символу (все адреса суммарно) --")
    print(by_symbol.to_string())

    n_pass22 = ((by_symbol["n_trades"] >= 100) & (by_symbol["vol_usd"] >= 10_000)).sum()
    total_closed = df["n_trades_closed_hours"].sum()
    total_vol = df["vol_usd"].sum()
    n_symbols_traded = df["symbol"].nunique()
    print(f"\n[r1_flagship_recon] Флагманских тикеров с ЛЮБОЙ торговлей (под этим именем, "
          f"любым адресом): {n_symbols_traded} / 31. Проходят §2.2 по сумме всех адресов: "
          f"{n_pass22}. Сделок в закрытые часы всего: {total_closed}. "
          f"Суммарный объём: ${total_vol:,.2f}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
