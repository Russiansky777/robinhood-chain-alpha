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

from dune_client import DuneClient
from run_pipeline import read_sql

SQL = read_sql("r1/r1_flagship_trades_by_symbol")


def main() -> int:
    client = DuneClient()
    print("===== r1_flagship_trades_by_symbol (оценка 8.0) =====")
    qid = client.create_query("r1_flagship_trades_by_symbol", SQL)
    df = client.run_sql_cached(
        "r1_flagship_trades_by_symbol", SQL, query_id=qid, estimated_credits=8.0,
        expected_max_rows=200, expected_columns=5,
    )
    if df is None or not len(df):
        print("[r1_flagship_recon] Пусто -- флагманские тикеры (с фидами) ВООБЩЕ не "
              "торгуются в dex.trades за весь период. Это означало бы, что реальная "
              "торговля идёт ТОЛЬКО по токенам из factory-реестра (без фидов), а "
              "фиды покрывают токены, которые никто не торгует -- гейт разведки "
              "фактически провален (нет пересечения объём+фид ни в одну сторону).")
        return 1

    print(df.to_string(max_rows=200))
    n_pass22 = ((df["n_trades"] >= 100) & (df["vol_usd"] >= 10_000)).sum()
    total_closed = df["n_trades_closed_hours"].sum()
    total_vol = df["vol_usd"].sum()
    print(f"\n[r1_flagship_recon] Флагманских тикеров с торговлей: {len(df)} / 31. "
          f"Проходят §2.2 (>=100 сделок И >=$10k): {n_pass22}. "
          f"Сделок в закрытые часы всего: {total_closed}. Суммарный объём: ${total_vol:,.2f}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
