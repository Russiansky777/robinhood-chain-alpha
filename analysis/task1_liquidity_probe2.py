#!/usr/bin/env python3
"""Задача 1, проверка 1, шаг 2: реальная находка из шага 1 --
dex.trades (полный список колонок) реально содержит
`project_contract_address` (варбинари, адрес пула), `project`,
`version`, `token_pair` -- НЕ используем предположение "TVL только с
живого GT-снимка", сначала проверяем два более точных пути:

1. Реальные адреса пулов для тикеров Задачи 1 -- сгруппировать
   dex.trades по project_contract_address/project/version для токенов
   реестра, увидеть, сколько разных пулов реально стоит за каждым
   тикером (может быть больше одного).
2. Узкий поиск таблиц резервов/ликвидности СХЕМ, где название схемы
   содержит 'robinhood' -- если Dune реально задекодировал события
   Mint/Burn/Sync конкретных пар на этой цепи, это дало бы ИСТОРИЧЕСКИЕ
   резервы на момент каждого окна (не только текущий снимок GT)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task1_liquidity_probe2_result.json")
REGISTRY_PATH = Path("data/rwa_stock_token_registry.json")


def run() -> int:
    client = DuneClient()
    out = {}

    registry = json.loads(REGISTRY_PATH.read_text())["tokens"]
    token_addrs = [t["stock_token_address"] for t in registry.values()]
    token_addrs_hex_list = ",".join(f"from_hex('{a[2:].lower()}')" for a in token_addrs)

    sql1 = f"""select project, version, to_hex(project_contract_address) as pool_address_hex,
       token_bought_symbol, token_sold_symbol, count(*) as n_trades,
       sum(amount_usd) as total_vol_usd, max(block_time) as last_trade
from dex.trades
where blockchain = 'robinhood'
  and (token_bought_address in ({token_addrs_hex_list}) or token_sold_address in ({token_addrs_hex_list}))
  and block_time >= timestamp '2026-07-01 00:00:00'
group by project, version, to_hex(project_contract_address), token_bought_symbol, token_sold_symbol
order by total_vol_usd desc
limit 300"""
    qid1 = client.create_query("task1_pool_addresses_by_token", sql1)
    df1 = client.run_sql_cached("task1_pool_addresses_by_token", sql1, query_id=qid1,
                                 estimated_credits=12.0, expected_max_rows=350, expected_columns=8)
    out["pool_addresses_by_token"] = df1.to_dict("records") if df1 is not None else None
    print(f"[probe2] реальных строк (project x pool x pair): {len(out['pool_addresses_by_token']) if df1 is not None else 0}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))

    sql2 = """select table_schema, table_name
from information_schema.tables
where lower(table_schema) like '%robinhood%'
order by table_schema, table_name
limit 300"""
    qid2 = client.create_query("task1_robinhood_schema_tables", sql2)
    df2 = client.run_sql_cached("task1_robinhood_schema_tables", sql2, query_id=qid2,
                                 estimated_credits=0.5, expected_max_rows=350, expected_columns=2)
    out["robinhood_schema_tables"] = df2.to_dict("records") if df2 is not None else None
    print(f"[probe2] реальных таблиц в схемах *robinhood*: {len(out['robinhood_schema_tables']) if df2 is not None else 0}")

    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[probe2] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
