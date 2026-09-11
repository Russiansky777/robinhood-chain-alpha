#!/usr/bin/env python3
"""Задача 5, измерение 1 (главное, владелец 2026-09-11) -- латентная
подпись: для каждой прибыльной замкнутой транзакции дня найти
ПРЕДШЕСТВУЮЩИЙ крупный своп (>$5k) в ЛЮБОМ из затронутых этой
транзакцией пулов, расстояние в блоках. Распределение денег ($
профита), взвешенное по этому расстоянию: доля в том же блоке / +1 /
+2 / +3 и далее -- отдельно для топ-10 исполнителей дня и для хвоста.
Это прямой ответ на вопрос "есть ли приз для неколлоцированного
игрока": если деньги концентрируются на расстоянии 0-1 блок, у
неколлоцированного игрока шансов нет; если типично 2-3+ -- есть окно.

Определение "крупного свопа" (>$5k) -- НЕ ограничено качественными
фильтрами владельца (диверсификация/самоторговля) -- катализатором
может быть ЛЮБОЙ адрес, не только "квалифицированные" трейдеры. Размер
в USD считается ТОЛЬКО для свопов, где одна из сторон -- WETH,
address(0) (нативный ETH в v4) или USDG (те же три ценимых актива, что
и в формуле профита) -- своп в пуле без ценимой стороны честно
исключён из категории "крупный" (не может быть оценён, не гадаем).

Множество пулов-кандидатов для поиска катализатора СОКРАЩЕНО до
пулов, реально затронутых хотя бы одной прибыльной замкнутой
транзакцией дня (a не весь день целиком) -- это на порядок меньше и
не требует пересканирования всего дня второй раз вслепую.

Lookback = 15 блоков (владелец просил бакеты 0/+1/+2/+3-и-далее --
что угодно за пределами 3 уже попадает в "3+", 15 блоков даёт большой
запас без раздувания self-join)."""
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
os.environ.setdefault("CREDIT_GUARD_SANITY_MAX_ESTIMATE", "300")  # владелец, 2026-09-11: "потолок на блок 300"

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_latency_signature_result.json")
NAMESPACE_BUDGET = 1000.0

WETH = "0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "5fc5360d0400a0fd4f2af552add042d716f1d168"
ZERO_ADDR = "0000000000000000000000000000000000000000"
WETH_DECIMALS, USDG_DECIMALS = 18, 6
CANONICAL_FACTORY = "1f7d7550b1b028f7571e69a784071f0205fd2efa"
DUST_THRESHOLD_USD = 1.0
DUST_REL_EPS = 1e-6
MIN_DISTINCT_TRADERS_PER_POOL_DAY = 4
MAX_DOMINANT_SHARE = 0.5
LARGE_SWAP_USD = 5000.0
LOOKBACK_BLOCKS = 15

# Реальные топ-10 исполнителей дня (task5_arb_top_executors_oneday_v4_9edca3a5c64e4abd.csv,
# сортировка по total_profit_usd desc) -- захардкожено намеренно: список уже реально
# получен и оплачен, пересчитывать его здесь заново было бы лишней тратой.
TOP10_EXECUTORS = [
    "824ABDA7E8BFCF47C543761C50BC7BF6756DEDA9",
    "D83E60D273FD0CB3B05C2611DB940FECB7B6CA64",
    "DA8EF690D9C9B6C7DB1A5F95943C838309306B03",
    "FDE88016A65B2371F2C6E4F698A0C79598E46435",
    "FE68082A448F17A3D1700957B9F63176CECD334E",
    "C27A2B4BD5B374C3CB3C711E17901628824F8258",
    "B4864F7F85EB2FDBD58B4293A2B9BD684FB13336",
    "7660E7E9E674EFDF5CE56B14593DCB7482FB95FF",
    "4A5CCF5AC7CFA7B752E5FE0B019245A9AB3A6331",
    "E3DCD9730081A5521F434303BB86D2613786CA83",
]


def build_sql(day_start: str, day_end: str) -> str:
    top10_list = ", ".join(f"'{a}'" for a in TOP10_EXECUTORS)
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
        s.evt_tx_hash as tx_hash, s.evt_block_number as block_number, s.evt_block_date as block_date,
        s.contract_address as pool_addr, to_hex(s.contract_address) as pool_key,
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
    select r.tx_hash, r.block_number, r.pool_key, r.executor, r.token0, r.token1, r.amount0, r.amount1
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
        w.tx_hash as tx_hash, w.block_number as block_number, w.block_date as block_date,
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
    select r.tx_hash, r.block_number, r.pool_key, r.token_bought, r.token_sold, r.amount_bought, r.amount_sold
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
    select tx_hash, block_number, pool_key, executor, token, delta_raw
    from v3_swaps_filtered
    cross join unnest(array[token0, token1], array[cast(-amount0 as double), cast(-amount1 as double)]) as u(token, delta_raw)
),
v4_flows as (
    select tx_hash, block_number, pool_key, cast(null as varchar) as executor, token, delta_raw
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
tx_meta as (
    select tx_hash, min(block_number) as block_number, max(executor) as v3_executor,
           array_agg(distinct pool_key) as touched_pools
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
profit_calc as (
    select
        m.tx_hash, m.block_number, m.touched_pools,
        coalesce(m.v3_executor, to_hex(t."from")) as executor,
        (ce.exit_token is not null) as is_closed_cycle,
        coalesce(ce.exit_token in (from_hex('{WETH}'), from_hex('{ZERO_ADDR}'), from_hex('{USDG}')), false) as is_priced_exit,
        case
            when ce.exit_token = from_hex('{WETH}') then ce.exit_net_flow_raw / 1e{WETH_DECIMALS} * {{eth_price}}
            when ce.exit_token = from_hex('{ZERO_ADDR}') then ce.exit_net_flow_raw / 1e{WETH_DECIMALS} * {{eth_price}}
            when ce.exit_token = from_hex('{USDG}') then ce.exit_net_flow_raw / 1e{USDG_DECIMALS}
            else cast(null as double)
        end as profit_usd
    from tx_meta m
    inner join tx_nondust_count n on n.tx_hash = m.tx_hash
    left join closed_cycle_exit ce on ce.tx_hash = m.tx_hash
    left join robinhood.transactions t
        on t.hash = m.tx_hash and t.block_date >= date '{day_start[:10]}' and t.block_date <= date '{day_end[:10]}'
),
profitable_txs as (
    select tx_hash, block_number, executor, profit_usd, touched_pools
    from profit_calc
    where is_closed_cycle and is_priced_exit and profit_usd > {DUST_THRESHOLD_USD}
),
profitable_pool_touch as (
    select tx_hash, block_number, executor, profit_usd, pk as pool_key
    from profitable_txs
    cross join unnest(touched_pools) as u(pk)
),
touched_pool_set as (
    select distinct pool_key from profitable_pool_touch
),
v3_all_swaps_priced as (
    select s.evt_tx_hash as tx_hash, s.evt_block_number as block_number, to_hex(s.contract_address) as pool_key,
        case
            when cp.token0 = from_hex('{WETH}') then abs(cast(s.amount0 as double)) / 1e{WETH_DECIMALS} * {{eth_price}}
            when cp.token1 = from_hex('{WETH}') then abs(cast(s.amount1 as double)) / 1e{WETH_DECIMALS} * {{eth_price}}
            when cp.token0 = from_hex('{USDG}') then abs(cast(s.amount0 as double)) / 1e{USDG_DECIMALS}
            when cp.token1 = from_hex('{USDG}') then abs(cast(s.amount1 as double)) / 1e{USDG_DECIMALS}
            else cast(null as double)
        end as notional_usd
    from uniswap_v3_robinhood.uniswapv3pool_evt_swap s
    inner join canonical_pools cp on cp.pool = s.contract_address
    where s.evt_block_time >= timestamp '{day_start}' and s.evt_block_time < timestamp '{day_end}'
      and to_hex(s.contract_address) in (select pool_key from touched_pool_set)
),
v4_all_swaps_priced as (
    select w.tx_hash as tx_hash, w.block_number as block_number,
        concat(to_hex(least(w.token_bought_address, w.token_sold_address)), '-',
               to_hex(greatest(w.token_bought_address, w.token_sold_address)), '-',
               cast(w.fee as varchar), '-', to_hex(w.hooks)) as pool_key,
        case
            when w.token_bought_address = from_hex('{WETH}') or w.token_bought_address = from_hex('{ZERO_ADDR}')
                then cast(w.token_bought_amount_raw as double) / 1e{WETH_DECIMALS} * {{eth_price}}
            when w.token_sold_address = from_hex('{WETH}') or w.token_sold_address = from_hex('{ZERO_ADDR}')
                then cast(w.token_sold_amount_raw as double) / 1e{WETH_DECIMALS} * {{eth_price}}
            when w.token_bought_address = from_hex('{USDG}') then cast(w.token_bought_amount_raw as double) / 1e{USDG_DECIMALS}
            when w.token_sold_address = from_hex('{USDG}') then cast(w.token_sold_amount_raw as double) / 1e{USDG_DECIMALS}
            else cast(null as double)
        end as notional_usd
    from uniswap_v4_robinhood.swaps w
    where w.block_time >= timestamp '{day_start}' and w.block_time < timestamp '{day_end}'
      and concat(to_hex(least(w.token_bought_address, w.token_sold_address)), '-',
                 to_hex(greatest(w.token_bought_address, w.token_sold_address)), '-',
                 cast(w.fee as varchar), '-', to_hex(w.hooks)) in (select pool_key from touched_pool_set)
),
all_priced_swaps as (
    select * from v3_all_swaps_priced
    union all
    select * from v4_all_swaps_priced
),
large_swaps as (
    select tx_hash, block_number, pool_key, notional_usd
    from all_priced_swaps
    where notional_usd > {LARGE_SWAP_USD}
),
catalyst_candidates as (
    select p.tx_hash, p.executor, p.profit_usd,
           (p.block_number - l.block_number) as block_distance
    from profitable_pool_touch p
    inner join large_swaps l
        on l.pool_key = p.pool_key
        and l.block_number <= p.block_number
        and l.block_number >= p.block_number - {LOOKBACK_BLOCKS}
),
nearest_catalyst as (
    select tx_hash, min(block_distance) as min_distance
    from catalyst_candidates
    group by tx_hash
),
final_rows as (
    select pt.tx_hash, pt.executor, pt.profit_usd, nc.min_distance,
        case
            when nc.min_distance is null then 'none'
            when nc.min_distance = 0 then '0'
            when nc.min_distance = 1 then '1'
            when nc.min_distance = 2 then '2'
            else '3+'
        end as distance_bucket,
        case when pt.executor in ({top10_list}) then 'top10' else 'tail' end as executor_tier
    from (select distinct tx_hash, executor, profit_usd from profitable_pool_touch) pt
    left join nearest_catalyst nc on nc.tx_hash = pt.tx_hash
)
select executor_tier, distance_bucket, count(*) as n_txs, sum(profit_usd) as total_profit_usd
from final_rows
group by executor_tier, distance_bucket
order by executor_tier, distance_bucket
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
    print(f"[latency] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "large_swap_usd_threshold": LARGE_SWAP_USD, "lookback_blocks": LOOKBACK_BLOCKS,
                     "top10_executors": TOP10_EXECUTORS}

    now = datetime.now(timezone.utc)
    day_end_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_dt = day_end_dt - timedelta(days=1)
    day_start = day_start_dt.strftime("%Y-%m-%d %H:%M:%S")
    day_end = day_end_dt.strftime("%Y-%m-%d %H:%M:%S")
    result["probe_day_start_utc"] = day_start
    result["probe_day_end_utc"] = day_end

    print("=== A. Реальная медианная цена ETH/USDG (тот же день) ===")
    sql_price = build_price_sql(day_start, day_end)
    qid_price = client.create_query("task5_latency_eth_price", sql_price)
    df_price = client.run_sql_cached("task5_latency_eth_price", sql_price, query_id=qid_price,
                                      estimated_credits=3.0, expected_max_rows=5, expected_columns=1)
    eth_price_usdg = None
    if df_price is not None and len(df_price) and df_price["median_weth_price_in_usdg"].iloc[0] is not None:
        eth_price_usdg = float(df_price["median_weth_price_in_usdg"].iloc[0])
    result["eth_price_usdg"] = eth_price_usdg
    print(f"[latency] цена ETH/USDG: {eth_price_usdg}")
    if eth_price_usdg is None:
        result["blocker"] = "цена не получена"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1

    print("\n=== B. Латентная подпись (материализация + чтение сводки) ===")
    sql = build_sql(day_start, day_end).replace("{eth_price}", repr(eth_price_usdg))
    qid = client.create_query("task5_latency_signature_v1", sql)
    df = client.run_sql_cached("task5_latency_signature_v1", sql, query_id=qid,
                                estimated_credits=250.0, expected_max_rows=20, expected_columns=4)
    rows = df.to_dict("records") if df is not None else []
    result["distribution"] = rows
    print(f"[latency] строк: {len(rows)}")
    for r in rows:
        print(f"  {r}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    result["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[latency] итого: {total_cost:.4f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
