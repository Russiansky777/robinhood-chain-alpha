#!/usr/bin/env python3
"""Задача 5, живой бот -- владелец 2026-09-12: реальная карта пулов, где
за 09-10.09 были прибыльные захваты >=$10 в блоках >=1 (distance >= 1,
т.е. НЕ блок 0 и НЕ 'none' -- буквально как сформулировал владелец: "в
блоках >=1 (distance >= 1)"). По каждому пулу: адрес/pool_key, пара,
число таких захватов, суммарная прибыль, средний размер.

Результат идёт в `analysis/task5_bot_config.py` как СТАРТОВЫЙ УНИВЕРСУМ
пулов для бота -- RPC-скан (`task5_bot_pool_state.bootstrap_registry_
from_rpc`) остаётся ДОПОЛНЕНИЕМ (полное покрытие канонических v3-пулов
на случай новых), не заменой этого списка.

ЧЕСТНАЯ ОГОВОРКА про двойной счёт: замкнутый цикл арбитража по
определению касается >=2 пулов (n_pools_tx>=2 в фильтре этого проекта).
Один и тот же захват учитывается В КАЖДОМ из затронутых пулов (иначе
пул, который был лишь ВТОРОЙ ногой цикла, никогда бы не попал в список,
хотя без него цикл не существовал бы) -- то есть total_profit_usd,
просуммированный по всем строкам этого отчёта, БОЛЬШЕ, чем реальная
сумма профита по уникальным транзакциям (двух-трёхкратно, по числу ног
типичного цикла). Это НЕ ошибка, а сознательный выбор ради ответа на
реальный вопрос "в каких пулах бот должен смотреть", не "сколько денег
всего".

Оценка стоимости на коротком окне ПЕРЕД полным днём (владелец,
2026-09-12) -- см. TASK5_POOLMAP_WINDOW_HOURS/END_HOUR ниже."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task5_active_arb_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")
os.environ.setdefault("CREDIT_GUARD_SANITY_MAX_ESTIMATE", "300")  # владелец, 2026-09-12: "потолок -- 300"

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_pool_map_result.json")
NAMESPACE_BUDGET = 2400.0  # поднято с 1650.0 (2026-09-12) -- реальный прогон упёрся именно в этот
# внутренний потолок пространства (1621.60 потрачено из 1650.0), а НЕ в реальный остаток цикла
# (490 из лимита 2493.6) -- намеренный запас, реальный цикл остаётся единственной твёрдой границей

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
MIN_CAPTURE_USD = 10.0  # владелец: "прибыльные захваты >=$10"

DAY_START = "2026-09-10 00:00:00"
DAY_END = "2026-09-11 00:00:00"

# Короткое окно для калибровки стоимости ДО полного дня -- по умолчанию
# первые 3 часа (владелец: "оценка стоимости на коротком окне перед
# полным"). Полный день запускается ОТДЕЛЬНЫМ прогоном
# (TASK5_POOLMAP_FULL_DAY=1) после честной проверки, что оценка по
# короткому окну безопасно экстраполируется под потолок 300.
WINDOW_HOURS = int(os.environ.get("TASK5_POOLMAP_WINDOW_HOURS", "3"))
FULL_DAY = os.environ.get("TASK5_POOLMAP_FULL_DAY", "0") == "1"


def build_sql(day_start: str, day_end: str) -> str:
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
),
profitable_txs as (
    select tx_hash, block_number, profit_usd, touched_pools
    from profit_calc
    where is_closed_cycle and is_priced_exit and profit_usd > {MIN_CAPTURE_USD}
),
profitable_pool_touch as (
    select tx_hash, block_number, profit_usd, pk as pool_key
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
    select p.tx_hash,
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
qualifying as (
    -- владелец, буквально: "в блоках >=1 (distance >= 1)" -- ИСКЛЮЧАЕТ
    -- и блок 0, и 'none' (min_distance is null): нужна ИЗВЕСТНАЯ
    -- дистанция >= 1, не отсутствие катализатора.
    select pt.pool_key, pt.profit_usd
    from profitable_pool_touch pt
    inner join nearest_catalyst nc on nc.tx_hash = pt.tx_hash
    where nc.min_distance >= 1
),
pool_pair_map as (
    select to_hex(pool) as pool_key, to_hex(token0) as token0, to_hex(token1) as token1, 'v3' as version
    from canonical_pools
)
select
    q.pool_key,
    coalesce(m.token0, split_part(q.pool_key, '-', 1)) as token0,
    coalesce(m.token1, split_part(q.pool_key, '-', 2)) as token1,
    coalesce(m.version, 'v4') as version,
    count(*) as n_captures,
    sum(q.profit_usd) as total_profit_usd,
    avg(q.profit_usd) as avg_profit_usd
from qualifying q
left join pool_pair_map m on m.pool_key = q.pool_key
group by q.pool_key, m.token0, m.token1, m.version
order by total_profit_usd desc
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
    print(f"[pool_map] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    if FULL_DAY:
        day_start, day_end = DAY_START, DAY_END
        window_label = "полный день 09-10"
    else:
        from datetime import datetime, timedelta
        start_dt = datetime.strptime(DAY_START, "%Y-%m-%d %H:%M:%S")
        end_dt = start_dt + timedelta(hours=WINDOW_HOURS)
        day_start, day_end = start_dt.strftime("%Y-%m-%d %H:%M:%S"), end_dt.strftime("%Y-%m-%d %H:%M:%S")
        window_label = f"калибровка, первые {WINDOW_HOURS}ч 09-10"

    client = DuneClient()
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_label": window_label,
        "day_start": day_start,
        "day_end": day_end,
        "min_capture_usd": MIN_CAPTURE_USD,
        "note_double_counting": (
            "Один захват учитывается в КАЖДОМ затронутом пуле (n_pools_tx>=2) -- "
            "сумма total_profit_usd по всем строкам БОЛЬШЕ реальной суммы по "
            "уникальным транзакциям. Осознанный выбор -- см. docstring скрипта."
        ),
    }

    print(f"=== A. Цена ETH/USDG ({window_label}) ===")
    sql_price = build_price_sql(day_start, day_end)
    qid_price = client.create_query("task5_poolmap_eth_price", sql_price)
    df_price = client.run_sql_cached("task5_poolmap_eth_price", sql_price, query_id=qid_price,
                                      estimated_credits=3.0, expected_max_rows=5, expected_columns=1)
    eth_price_usdg = None
    if df_price is not None and len(df_price) and df_price["median_weth_price_in_usdg"].iloc[0] is not None:
        eth_price_usdg = float(df_price["median_weth_price_in_usdg"].iloc[0])
    result["eth_price_usdg"] = eth_price_usdg
    if eth_price_usdg is None:
        result["blocker"] = "цена не получена"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1

    print(f"\n=== B. Карта пулов ({window_label}) ===")
    sql = build_sql(day_start, day_end).replace("{eth_price}", repr(eth_price_usdg))
    name = "task5_pool_map_full" if FULL_DAY else f"task5_pool_map_calib_{WINDOW_HOURS}h"
    qid = client.create_query(name, sql)
    est = 280.0 if FULL_DAY else max(20.0, 280.0 * WINDOW_HOURS / 24.0 * 3)  # честный запас x3 на калибровке
    df = client.run_sql_cached(name, sql, query_id=qid, estimated_credits=est,
                                expected_max_rows=200, expected_columns=7)
    rows = df.to_dict("records") if df is not None else []
    result["pools"] = rows
    print(f"[pool_map] пулов найдено: {len(rows)}")
    for r in rows[:20]:
        print(f"  {r}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    result["total_cost_credits"] = total_cost
    if not FULL_DAY:
        extrapolated_full_day = total_cost * 24.0 / WINDOW_HOURS
        result["extrapolated_full_day_cost_credits"] = extrapolated_full_day
        print(f"[pool_map] калибровка: {total_cost:.2f} за {WINDOW_HOURS}ч -> "
              f"экстраполяция на полный день: {extrapolated_full_day:.2f}")
        print(f"[pool_map] {'БЕЗОПАСНО' if extrapolated_full_day < 280 else 'ВНИМАНИЕ, БЛИЗКО К ПОТОЛКУ 300'} "
              "запускать полный день (TASK5_POOLMAP_FULL_DAY=1)")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[pool_map] итого: {total_cost:.4f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
