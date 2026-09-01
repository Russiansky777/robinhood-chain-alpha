#!/usr/bin/env python3
"""Sprint R1, Шаг 1: разведка (лимит этапа <=15 кредитов, см.
docs/R1_DESIGN.md). НЕ меняет §2 (заморожен) -- собирает факты для
подраздела "Механика" и ищет источник реестра Chainlink-фидов.

Владелец, п.1 Шага 1: "дёшево из Dune" -- (а) по каким сток-токенам
есть DEX-сделки; (б) профиль объёма по часам недели; (в) частота
обновлений фидов. Начинается с самого дешёвого шага: имена таблиц
внутри схем chainlink_robinhood / rwa_stock_factory_robinhood /
rwa_robinhood (найдены в уже закэшированном общем скане схем Sprint
G1, `g1_schemas_like_robinhood_distinct` -- ПЕРЕИСПОЛЬЗУЕТСЯ, не
платится заново) -- если Dune уже держит декодированный реестр
Chainlink-фидов для этого чейна, это надёжнее и дешевле, чем парсинг
клиентского JS-виджета докс (см. `r1_scrape_stock_tokens.py`, run #7 --
таблица фидов на docs.chain.link грузится client-side, недоступна
простым HTTP-запросом).

Использование: python analysis/r1_recon.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintR1")
sys.path.insert(0, str(Path(__file__).parent))

from dune_client import DuneClient
from run_pipeline import read_sql

SCHEMA_DRILLDOWN_SQL = read_sql("r1/r1_schema_drilldown")
COLUMNS_PROBE_SQL = read_sql("r1/r1_columns_probe")
FEED_ACTIVITY_SQL = read_sql("r1/r1_feed_activity")
STOCK_TOKEN_DEPLOYMENTS_SQL = read_sql("r1/r1_stock_token_deployments")


def main() -> int:
    client = DuneClient()

    print("===== r1_schema_drilldown (оценка 4.0) =====")
    qid = client.create_query("r1_schema_drilldown", SCHEMA_DRILLDOWN_SQL)
    df = client.run_sql_cached(
        "r1_schema_drilldown", SCHEMA_DRILLDOWN_SQL, query_id=qid, estimated_credits=4.0,
        expected_max_rows=500, expected_columns=2,
    )
    if df is not None and len(df):
        print(df.to_string(max_rows=500))
    else:
        print("(пусто)")

    # run #9 нашёл chainlink_robinhood.dualaggregator_evt_answerupdated
    # (декодированный Chainlink-агрегатор -- реестр фидов через
    # contract_address) и rwa_stock_factory_robinhood.factory_deployer_evt_deployed
    # (фабрика деплоя сток-токенов) -- колонки перед платным запросом.
    print("\n===== r1_columns_probe (оценка 3.0) =====")
    qid2 = client.create_query("r1_columns_probe", COLUMNS_PROBE_SQL)
    df2 = client.run_sql_cached(
        "r1_columns_probe", COLUMNS_PROBE_SQL, query_id=qid2, estimated_credits=3.0,
        expected_max_rows=60, expected_columns=5,
    )
    if df2 is not None and len(df2):
        print(df2.to_string(max_rows=60))
    else:
        print("(пусто)")

    # Реестр деплоя сток-токенов -- ончейн, symbol/name/stock(адрес)/uid.
    print("\n===== r1_stock_token_deployments (оценка 6.0) =====")
    qid3 = client.create_query("r1_stock_token_deployments", STOCK_TOKEN_DEPLOYMENTS_SQL)
    df3 = client.run_sql_cached(
        "r1_stock_token_deployments", STOCK_TOKEN_DEPLOYMENTS_SQL, query_id=qid3, estimated_credits=6.0,
        expected_max_rows=2000, expected_columns=6,
    )
    n_tokens = 0 if df3 is None else len(df3)
    print(f"[r1_recon] Задеплоено сток-токенов (июль-август, ончейн): {n_tokens}")
    if df3 is not None and len(df3):
        print(df3.head(20).to_string())

    # Активность Chainlink-фидов -- обновления/закрытые часы.
    print("\n===== r1_feed_activity (оценка 8.0) =====")
    qid4 = client.create_query("r1_feed_activity", FEED_ACTIVITY_SQL)
    df4 = client.run_sql_cached(
        "r1_feed_activity", FEED_ACTIVITY_SQL, query_id=qid4, estimated_credits=8.0,
        expected_max_rows=1000, expected_columns=5,
    )
    n_feeds = 0 if df4 is None else len(df4)
    print(f"[r1_recon] Активных Chainlink-фидов (июль-август): {n_feeds}")
    if df4 is not None and len(df4):
        print(df4.head(20).to_string())
        total_updates = df4["n_updates"].sum()
        total_outside = df4["n_updates_outside_market_hours"].sum()
        print(f"[r1_recon] Всего обновлений: {total_updates}, из них вне торговых часов: "
              f"{total_outside} ({total_outside / total_updates:.1%})" if total_updates else "")

    print("\n[r1_recon] Готово. См. вывод выше -- обновите docs/R1_DESIGN.md, "
          "\"Механика\", по фактам о доступных таблицах Chainlink/RWA-фабрики.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
