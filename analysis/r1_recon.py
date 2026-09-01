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

    print("\n[r1_recon] Готово. См. вывод выше -- обновите docs/R1_DESIGN.md, "
          "\"Механика\", по фактам о доступных таблицах Chainlink/RWA-фабрики.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
