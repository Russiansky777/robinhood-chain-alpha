#!/usr/bin/env python3
"""Задача 5 владельца (2026-09-10) -- размер приза на активных ценовых
расхождениях (атомарный внутриблочный арбитраж, НЕ путать с закрытой
Задачей 4 -- та была межцепочечный пассивный арбитраж). Только измерение,
без вложений в инфраструктуру.

Шаг 0 (дёшево, information_schema, ~10-15 кредитов) -- ПЕРЕД любым
содержательным запросом, подтвердить реальные схемы:

1. Реальные колонки `uniswap_v3_robinhood.uniswapv3pool_evt_swap` --
   уже известен факт существования таблицы (dune_mozila_schema_recon,
   2026-09-04), но НЕ её точные колонки. v3 evt_swap стандартно даёт
   amount0/amount1 (сырые, знаковые) БЕЗ идентичности token0/token1 --
   нужна отдельная таблица создания пулов для маппинга адрес пула ->
   (token0, token1, fee).
2. Поиск таблицы PoolCreated/Factory в схеме `uniswap_v3_robinhood` --
   для маппинга contract_address -> token0/token1 (нужно для расчёта
   прибыли в ETH/USDG по v3-ногам мультихопа).
3. Реальное существование и колонки таблицы транзакций цепи Robinhood
   (`robinhood.transactions` или аналог) -- нужен `tx.from` (реальный
   EOA-исполнитель), т.к. `sender`/`taker`/`recipient` в самих Swap-
   событиях -- это адреса УЧАСТНИКОВ ВНУТРИ вызова (часто роутер/сам
   пул), не обязательно топ-level отправитель транзакции, который и
   получает прибыль/несёт риск.

Уже известно из прежних прогонов (НЕ перепроверяется здесь, экономим
кредиты):
  - `uniswap_v4_robinhood.swaps` -- реальные колонки: blockchain,
    project, version, block_month, block_date, block_time,
    block_number, token_bought_amount_raw, token_sold_amount_raw,
    token_bought_address, token_sold_address, taker, maker,
    project_contract_address, tx_hash, evt_index, sender, hooks, fee,
    liquidity, sqrtpricex96, tick, call_trace_address. НЕТ amount_usd
    (сырые объёмы). `project_contract_address` -- singleton PoolManager
    (ОДИН адрес на все пары v4), НЕ per-pool -- различать v4-пулы нужно
    по (token_bought_address, token_sold_address, hooks, fee), не по
    contract_address.
  - WETH на Robinhood chain: 0x0bd7d308f8e1639fab988df18a8011f41eacad73
    (analysis/asset_consolidation_dryrun.py, реальный, использован в
    живой транзакции).
  - USDG на Robinhood chain: 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168
    (тот же источник).
  - `dex.trades(blockchain='robinhood')` -- под форс-гейтом
    credit_guard (DEX_TRADES_ROBINHOOD_FORCED_ESTIMATE), владелец явно
    запретил его использовать здесь -- работаем напрямую с
    декодированными схемами uniswap_v3_robinhood/uniswap_v4_robinhood,
    узкими предикатами."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task5_active_arb_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_active_arb_stage0_schema_result.json")
NAMESPACE_BUDGET = 1000.0  # поднято с 400.0 (2026-09-11, см. task5_active_arb_stage1_oneday.py) --
# владелец: "бюджет свободный в рамках остатка Mozila" -- технический потолок пространства


def run() -> int:
    ensure_namespace("task5_active_arb_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[task5_stage0] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def run_step(step_name: str, sql: str, est: float, max_rows: int = 100, max_cols: int = 10) -> list[dict]:
        qid = client.create_query(step_name, sql)
        df = client.run_sql_cached(step_name, sql, query_id=qid, estimated_credits=est,
                                    expected_max_rows=max_rows, expected_columns=max_cols)
        rows = df.to_dict("records") if df is not None else []
        cost = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == step_name), None)
        print(f"[task5_stage0] {step_name}: {len(rows)} строк, стоимость={cost}")
        return rows

    print("=== 1. Реальные колонки uniswap_v3_robinhood.uniswapv3pool_evt_swap ===")
    sql1 = """select column_name, data_type
from information_schema.columns
where table_schema = 'uniswap_v3_robinhood' and table_name = 'uniswapv3pool_evt_swap'
order by ordinal_position
limit 100"""
    v3_cols = run_step("task5_v3_evt_swap_columns", sql1, 2.0, max_rows=100, max_cols=2)
    out["v3_evt_swap_columns"] = v3_cols

    print("\n=== 2. Поиск таблиц Factory/PoolCreated в схеме uniswap_v3_robinhood ===")
    sql2 = """select table_name
from information_schema.tables
where table_schema = 'uniswap_v3_robinhood'
  and (lower(table_name) like '%factory%' or lower(table_name) like '%poolcreated%' or lower(table_name) like '%created%')
order by table_name
limit 100"""
    v3_factory_tables = run_step("task5_v3_factory_tables", sql2, 2.0, max_rows=100, max_cols=1)
    out["v3_factory_table_candidates"] = v3_factory_tables

    # Если факторка нашлась -- сразу проверяем её реальные колонки (то же самое исполнение, дёшево)
    factory_cols_by_table = {}
    for row in v3_factory_tables[:3]:
        t = row.get("table_name")
        if not t:
            continue
        sql_fc = f"""select column_name, data_type
from information_schema.columns
where table_schema = 'uniswap_v3_robinhood' and table_name = '{t}'
order by ordinal_position
limit 100"""
        cols = run_step(f"task5_v3_factory_cols_{t}"[:100], sql_fc, 2.0, max_rows=100, max_cols=2)
        factory_cols_by_table[t] = cols
    out["v3_factory_table_columns"] = factory_cols_by_table

    print("\n=== 3. Поиск таблицы транзакций цепи robinhood (для tx.from -- реальный исполнитель) ===")
    sql3 = """select table_schema, table_name
from information_schema.tables
where lower(table_schema) like '%robinhood%'
  and (lower(table_name) = 'transactions' or lower(table_name) like '%transaction%')
order by table_schema, table_name
limit 100"""
    tx_tables = run_step("task5_tx_table_search", sql3, 2.0, max_rows=100, max_cols=2)
    out["transactions_table_candidates"] = tx_tables

    tx_cols_by_table = {}
    for row in tx_tables[:3]:
        sch, t = row.get("table_schema"), row.get("table_name")
        if not sch or not t:
            continue
        sql_tc = f"""select column_name, data_type
from information_schema.columns
where table_schema = '{sch}' and table_name = '{t}'
order by ordinal_position
limit 100"""
        cols = run_step(f"task5_tx_cols_{sch}_{t}"[:100], sql_tc, 2.0, max_rows=100, max_cols=2)
        tx_cols_by_table[f"{sch}.{t}"] = cols
    out["transactions_table_columns"] = tx_cols_by_table

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    out["total_stage0_cost_credits"] = total_cost
    print(f"\n[task5_stage0] ИТОГО потрачено на Шаг 0: {total_cost:.2f} кредитов")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[task5_stage0] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
