#!/usr/bin/env python3
"""Задача 5, измерение 3 -- Stage 2, N дней с разбивкой по неделям и
границей 24.08, плюс чувствительность к газу.

Параметризуемый скрипт (через переменные окружения TASK5_S2_DAYS,
TASK5_S2_END_DATE) -- используется И для калибровочного теста на
малом окне (2-3 дня, чтобы реально измерить стоимость ПЕРЕД полным
30-дневным прогоном, как явно просил владелец: "оценка перед каждым
запросом"), И для финального прогона, если калибровка покажет, что
бюджет позволяет.

К формуле v4 (closed-cycle net-flow, см. task5_active_arb_stage1_oneday.py)
добавлено:
1. `week_bucket` -- номер недели относительно границы 24.08 (владелец),
   т.е. floor((date - 2026-08-24) / 7).
2. `gas_used` (реальный, из robinhood.transactions, уже джойнится для
   резолва исполнителя v4-only транзакций -- переиспользуем тот же join).
3. Пересчёт прибыли при гипотетической цене газа 0.1 / 0.5 / 1 gwei:
   profit_usd_at_X_gwei = profit_usd - (gas_used * X * 1e9 / 1e18 * eth_price).
   ВАЖНО: субсидированный газ ДО 29.09 означает, что реальная цена газа
   сейчас может быть намного ниже этих гипотетических уровней -- эта
   колонка честно показывает, что останется, если субсидия исчезнет,
   а не то, что происходит сейчас."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task5_active_arb_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")
os.environ.setdefault("CREDIT_GUARD_SANITY_MAX_ESTIMATE", "300")

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_stage2_multiday_result.json")
NAMESPACE_BUDGET = 1650.0

WETH = "0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "5fc5360d0400a0fd4f2af552add042d716f1d168"
ZERO_ADDR = "0000000000000000000000000000000000000000"
WETH_DECIMALS, USDG_DECIMALS = 18, 6
CANONICAL_FACTORY = "1f7d7550b1b028f7571e69a784071f0205fd2efa"
DUST_THRESHOLD_USD = 1.0
DUST_REL_EPS = 1e-6
MIN_DISTINCT_TRADERS_PER_POOL_DAY = 4
MAX_DOMINANT_SHARE = 0.5
WEEK_BOUNDARY_DATE = "2026-08-24"
GAS_PRICE_POINTS_GWEI = [0.1, 0.5, 1.0]


def build_sql(day_start: str, day_end: str) -> str:
    gas_cols = ",\n        ".join(
        f"m.profit_usd - (coalesce(m.gas_used, 0) * {g} * 1e9 / 1e{WETH_DECIMALS} * {{eth_price}}) as profit_usd_at_{str(g).replace('.', '_')}gwei"
        for g in GAS_PRICE_POINTS_GWEI
    )
    return f"""
with canonical_pools as (
    select pool, token0, token1
    from uniswap_v3_robinhood.uniswapv3factory_evt_poolcreated
    where contract_address = from_hex('{CANONICAL_FACTORY}')
),
lp_providers as (
    select distinct m.contract_address as pool, m.evt_tx_from as lp_address
    from uniswap_v3_robinhood.uniswapv3pool_evt_mint m
    where m.contract_address in (select pool from canonical_pools)
),
v3_swaps_qual as (
    select
        s.evt_tx_hash as tx_hash, s.evt_block_number as block_number, s.evt_block_time as block_time,
        s.evt_block_date as block_date, s.contract_address as pool_addr, to_hex(s.contract_address) as pool_key,
        s.evt_tx_from as executor_raw, to_hex(s.evt_tx_from) as executor,
        cp.token0 as token0, cp.token1 as token1, s.amount0 as amount0, s.amount1 as amount1
    from uniswap_v3_robinhood.uniswapv3pool_evt_swap s
    inner join canonical_pools cp on cp.pool = s.contract_address
    where s.evt_block_time >= timestamp '{day_start}' and s.evt_block_time < timestamp '{day_end}'
),
v3_pool_day_diversity as (
    select pool_addr, block_date, count(distinct executor_raw) as n_distinct_traders
    from v3_swaps_qual group by pool_addr, block_date
),
v3_pool_day_address_counts as (
    select pool_addr, block_date, executor_raw, count(*) as n_swaps
    from v3_swaps_qual group by pool_addr, block_date, executor_raw
),
v3_pool_day_totals as (
    select pool_addr, block_date, sum(n_swaps) as total_swaps
    from v3_pool_day_address_counts group by pool_addr, block_date
),
v3_swaps_filtered as (
    select r.tx_hash, r.block_number, r.block_time, r.block_date, r.pool_key, r.executor, r.token0, r.token1, r.amount0, r.amount1
    from v3_swaps_qual r
    inner join v3_pool_day_diversity div
        on div.pool_addr = r.pool_addr and div.block_date = r.block_date
        and div.n_distinct_traders >= {MIN_DISTINCT_TRADERS_PER_POOL_DAY}
    inner join v3_pool_day_address_counts cnt
        on cnt.pool_addr = r.pool_addr and cnt.block_date = r.block_date and cnt.executor_raw = r.executor_raw
    inner join v3_pool_day_totals tot
        on tot.pool_addr = r.pool_addr and tot.block_date = r.block_date
    left join lp_providers lp on lp.pool = r.pool_addr and lp.lp_address = r.executor_raw
    where lp.lp_address is null and cast(cnt.n_swaps as double) / tot.total_swaps <= {MAX_DOMINANT_SHARE}
),
v4_swaps_qual as (
    select
        w.tx_hash as tx_hash, w.block_number as block_number, w.block_time as block_time, w.block_date as block_date,
        concat(to_hex(least(w.token_bought_address, w.token_sold_address)), '-',
               to_hex(greatest(w.token_bought_address, w.token_sold_address)), '-',
               cast(w.fee as varchar), '-', to_hex(w.hooks)) as pool_key,
        w.sender as executor_raw,
        w.token_bought_address as token_bought, w.token_sold_address as token_sold,
        w.token_bought_amount_raw as amount_bought, w.token_sold_amount_raw as amount_sold
    from uniswap_v4_robinhood.swaps w
    where w.block_time >= timestamp '{day_start}' and w.block_time < timestamp '{day_end}'
),
v4_pool_day_diversity as (
    select pool_key, block_date, count(distinct executor_raw) as n_distinct_traders
    from v4_swaps_qual group by pool_key, block_date
),
v4_pool_day_address_counts as (
    select pool_key, block_date, executor_raw, count(*) as n_swaps
    from v4_swaps_qual group by pool_key, block_date, executor_raw
),
v4_pool_day_totals as (
    select pool_key, block_date, sum(n_swaps) as total_swaps
    from v4_pool_day_address_counts group by pool_key, block_date
),
v4_swaps_filtered as (
    select r.tx_hash, r.block_number, r.block_time, r.block_date, r.pool_key, r.token_bought, r.token_sold, r.amount_bought, r.amount_sold
    from v4_swaps_qual r
    inner join v4_pool_day_diversity div
        on div.pool_key = r.pool_key and div.block_date = r.block_date
        and div.n_distinct_traders >= {MIN_DISTINCT_TRADERS_PER_POOL_DAY}
    inner join v4_pool_day_address_counts cnt
        on cnt.pool_key = r.pool_key and cnt.block_date = r.block_date and cnt.executor_raw = r.executor_raw
    inner join v4_pool_day_totals tot
        on tot.pool_key = r.pool_key and tot.block_date = r.block_date
    where cast(cnt.n_swaps as double) / tot.total_swaps <= {MAX_DOMINANT_SHARE}
),
v3_flows as (
    select tx_hash, block_number, block_time, block_date, pool_key, executor, token, delta_raw
    from v3_swaps_filtered
    cross join unnest(array[token0, token1], array[cast(-amount0 as double), cast(-amount1 as double)]) as u(token, delta_raw)
),
v4_flows as (
    select tx_hash, block_number, block_time, block_date, pool_key, cast(null as varchar) as executor, token, delta_raw
    from v4_swaps_filtered
    cross join unnest(array[token_bought, token_sold], array[cast(amount_bought as double), -cast(amount_sold as double)]) as u(token, delta_raw)
),
all_flows as (
    select * from v3_flows
    union all
    select * from v4_flows
),
flow_annotated as (
    select f.*, count(distinct pool_key) over (partition by tx_hash) as n_pools_tx
    from all_flows f
),
qualifying_flows as (
    select * from flow_annotated where n_pools_tx >= 2
),
tx_meta_pre as (
    select tx_hash, min(block_number) as block_number, min(block_time) as block_time, min(block_date) as block_date,
           count(*) / 2 as n_legs, max(n_pools_tx) as n_pools, max(executor) as v3_executor
    from qualifying_flows
    group by tx_hash
),
tx_token_flow as (
    select tx_hash, token, sum(delta_raw) as net_flow_raw, sum(abs(delta_raw)) as gross_raw
    from qualifying_flows
    group by tx_hash, token
),
tx_token_flagged as (
    select tx_hash, token, net_flow_raw, gross_raw,
        case when gross_raw > 0 and abs(net_flow_raw) / gross_raw > {DUST_REL_EPS} then 1 else 0 end as is_nondust
    from tx_token_flow
),
tx_nondust_count as (
    select tx_hash, sum(is_nondust) as n_nondust_tokens
    from tx_token_flagged group by tx_hash
),
closed_cycle_exit as (
    select f.tx_hash, f.token as exit_token, f.net_flow_raw as exit_net_flow_raw
    from tx_token_flagged f
    inner join tx_nondust_count n on n.tx_hash = f.tx_hash and n.n_nondust_tokens = 1
    where f.is_nondust = 1
),
profit_pre as (
    select
        m.tx_hash, m.block_number, m.block_time, m.block_date, m.n_legs, m.n_pools,
        coalesce(m.v3_executor, to_hex(t."from")) as executor,
        t.gas_used as gas_used,
        (ce.exit_token is not null) as is_closed_cycle,
        coalesce(ce.exit_token in (from_hex('{WETH}'), from_hex('{ZERO_ADDR}'), from_hex('{USDG}')), false) as is_priced_exit,
        case
            when ce.exit_token = from_hex('{WETH}') then ce.exit_net_flow_raw / 1e{WETH_DECIMALS} * {{eth_price}}
            when ce.exit_token = from_hex('{ZERO_ADDR}') then ce.exit_net_flow_raw / 1e{WETH_DECIMALS} * {{eth_price}}
            when ce.exit_token = from_hex('{USDG}') then ce.exit_net_flow_raw / 1e{USDG_DECIMALS}
            else cast(null as double)
        end as profit_usd
    from tx_meta_pre m
    inner join tx_nondust_count n on n.tx_hash = m.tx_hash
    left join closed_cycle_exit ce on ce.tx_hash = m.tx_hash
    left join robinhood.transactions t
        on t.hash = m.tx_hash and t.block_date >= date '{day_start[:10]}' and t.block_date <= date '{day_end[:10]}'
)
select
    m.block_date, m.executor, m.n_legs, m.n_pools, m.gas_used,
    cast(date_diff('day', date '{WEEK_BOUNDARY_DATE}', m.block_date) / 7 as integer) as week_bucket,
    m.profit_usd,
    {gas_cols}
from profit_pre m
where m.is_closed_cycle and m.is_priced_exit and m.profit_usd > {DUST_THRESHOLD_USD}
"""


SUMMARY_SQL_TEMPLATE = """
select block_date, week_bucket,
    count(*) as n_profitable_txs,
    sum(profit_usd) as total_profit_usd,
    sum(profit_usd_at_0_1gwei) as total_profit_usd_at_0_1gwei,
    sum(profit_usd_at_0_5gwei) as total_profit_usd_at_0_5gwei,
    sum(profit_usd_at_1_0gwei) as total_profit_usd_at_1_0gwei,
    count(*) filter (where profit_usd_at_0_1gwei > 1.0) as n_survive_0_1gwei,
    count(*) filter (where profit_usd_at_0_5gwei > 1.0) as n_survive_0_5gwei,
    count(*) filter (where profit_usd_at_1_0gwei > 1.0) as n_survive_1_0gwei
from query_DETECT_ID
group by block_date, week_bucket
order by block_date
"""


def build_price_sql(day_start: str, day_end: str) -> str:
    weth_usdg_pool = "52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
    return f"""
select approx_percentile(
    power(cast(sqrtpricex96 as double) / power(2, 96), 2) * power(10, {18 - USDG_DECIMALS}),
    0.5
) as median_weth_price_in_usdg
from uniswap_v3_robinhood.uniswapv3pool_evt_swap
where contract_address = from_hex('{weth_usdg_pool}')
  and evt_block_time >= timestamp '{day_start}' and evt_block_time < timestamp '{day_end}'
"""


def run() -> int:
    ns = credit_guard.namespace()
    credit_guard.ensure_namespace(ns, NAMESPACE_BUDGET)
    remaining = credit_guard.remaining_cycle_budget(credit_guard.load_state())
    print(f"[stage2] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    n_days = int(os.environ.get("TASK5_S2_DAYS", "2"))
    end_date_str = os.environ.get("TASK5_S2_END_DATE", "")
    client = DuneClient()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "n_days_requested": n_days, "week_boundary_date": WEEK_BOUNDARY_DATE,
                     "gas_price_points_gwei": GAS_PRICE_POINTS_GWEI}

    if end_date_str:
        day_end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        day_end_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_dt = day_end_dt - timedelta(days=n_days)
    day_start = day_start_dt.strftime("%Y-%m-%d %H:%M:%S")
    day_end = day_end_dt.strftime("%Y-%m-%d %H:%M:%S")
    result["window_start_utc"] = day_start
    result["window_end_utc"] = day_end
    print(f"[stage2] окно: {day_start} .. {day_end} ({n_days} дней)")

    print("\n=== A. Реальная медианная цена ETH/USDG (за то же окно) ===")
    sql_price = build_price_sql(day_start, day_end)
    qid_price = client.create_query(f"task5_s2_eth_price_{n_days}d", sql_price)
    df_price = client.run_sql_cached(f"task5_s2_eth_price_{n_days}d", sql_price, query_id=qid_price,
                                      estimated_credits=10.0, expected_max_rows=5, expected_columns=1)
    eth_price_usdg = None
    if df_price is not None and len(df_price) and df_price["median_weth_price_in_usdg"].iloc[0] is not None:
        eth_price_usdg = float(df_price["median_weth_price_in_usdg"].iloc[0])
    result["eth_price_usdg"] = eth_price_usdg
    print(f"[stage2] цена ETH/USDG: {eth_price_usdg}")
    if eth_price_usdg is None:
        result["blocker"] = "цена не получена"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1

    print(f"\n=== B. Материализация детекции + чувствительность к газу, {n_days} дней ===")
    sql_detect = build_sql(day_start, day_end).replace("{eth_price}", repr(eth_price_usdg))
    qid_detect = client.create_query(f"task5_s2_detect_{n_days}d", sql_detect)
    client.run_sql_cached(f"task5_s2_detect_{n_days}d", sql_detect, query_id=qid_detect,
                           estimated_credits=280.0, fetch_results=False)
    cost_detect = next((e["credits"] for e in reversed(client.credit_ledger)
                         if e["name"] == f"task5_s2_detect_{n_days}d" and e["op"] == "execute"), None)
    result["step_b_materialize_cost_credits"] = cost_detect
    print(f"[stage2] материализация: {cost_detect}")

    print("\n=== C. Дневная/недельная сводка ===")
    sql_summary = SUMMARY_SQL_TEMPLATE.replace("query_DETECT_ID", f"query_{qid_detect}")
    qid_summary = client.create_query(f"task5_s2_summary_{n_days}d", sql_summary)
    df_summary = client.run_sql_cached(f"task5_s2_summary_{n_days}d", sql_summary, query_id=qid_summary,
                                        estimated_credits=280.0, expected_max_rows=n_days + 5, expected_columns=9)
    rows = df_summary.to_dict("records") if df_summary is not None else []
    result["daily_summary"] = rows
    for r in rows:
        print(f"  {r}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    result["total_cost_credits"] = total_cost
    result["cost_per_day_credits"] = total_cost / n_days if n_days else None
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[stage2] итого: {total_cost:.4f} ({total_cost/n_days:.2f}/день), записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
