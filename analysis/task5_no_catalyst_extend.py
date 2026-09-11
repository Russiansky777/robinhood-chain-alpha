#!/usr/bin/env python3
"""Задача 5, дозапрос владельца 2026-09-11 ("без катализатора"): для
2026-09-10 -- ТОЛЬКО Ступень A относительно измерения 1 (порог катализатора
$5000 -> $500, окно 15 блоков БЕЗ ИЗМЕНЕНИЙ). Та же SQL-структура, что и
task5_latency_signature.py, единственное отличие -- LARGE_SWAP_USD.

ЧЕСТНО НЕ СДЕЛАНО В ЭТОМ ПРОГОНЕ (и почему):

- Ступень B владельца ("расширить окно до 100 блоков"): self-join
  катализатора масштабируется примерно пропорционально ширине окна --
  15->100 блоков это ~6.7x. Измерение 1 (это же окно=15, порог=$5000)
  стоило 233.22 кредита целиком (включая базовую воронку ~57, см.
  task5_active_arb_stage1_oneday.py). Честная оценка для окна=100 --
  много СОТЕН кредитов (могла быть и 500-800), что превышает НЕ ТОЛЬКО
  потолок этого пространства (120), но и общий потолок "300 на блок" для
  трёх измерений. Не запущено -- решение о бюджете за владельцем.
- Ступень C ("проверить Mint/Burn"): требует реальных имён таблиц/колонок
  Burn (v3) и модификации ликвидности (v4) -- см. отдельный дешёвый
  скрипт task5_no_catalyst_probe.py, который нужно прогнать и прочитать
  ПЕРЕД тем, как писать эту часть (иначе -- угадывание имён таблиц,
  чего проект избегает с самого начала).

Пространство `task5_no_catalyst` создано с ЖЁСТКИМ потолком 120.0 --
credit_guard откажет исполнять ЛЮБОЙ запрос этого скрипта, если оценка
(честная, не заниженная искусственно) вместе с уже потраченным в этом
пространстве (включая task5_no_catalyst_probe.py) превысит 120."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task5_no_catalyst")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")
os.environ.setdefault("CREDIT_GUARD_SANITY_MAX_ESTIMATE", "300")

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_no_catalyst_extend_result.json")
NAMESPACE_BUDGET = 120.0  # владелец, 2026-09-11: "потолок 120" -- жёсткий потолок ИМЕННО этого пространства

WETH = "0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "5fc5360d0400a0fd4f2af552add042d716f1d168"
ZERO_ADDR = "0000000000000000000000000000000000000000"
WETH_DECIMALS, USDG_DECIMALS = 18, 6
CANONICAL_FACTORY = "1f7d7550b1b028f7571e69a784071f0205fd2efa"
DUST_THRESHOLD_USD = 1.0
DUST_REL_EPS = 1e-6
MIN_DISTINCT_TRADERS_PER_POOL_DAY = 4
MAX_DOMINANT_SHARE = 0.5
# Ступень A владельца: порог понижен с $5000 (измерение 1) до $500. Окно --
# БЕЗ ИЗМЕНЕНИЙ (15 блоков), см. docstring про Ступень B.
LARGE_SWAP_USD = 500.0
LOOKBACK_BLOCKS = 15

TARGET_DAY_START = "2026-09-10 00:00:00"
TARGET_DAY_END = "2026-09-11 00:00:00"

# Реальный результат измерения 1 (порог $5000, то же окно/день) -- для
# честного сравнения "доля объяснённых долларов ДО/ПОСЛЕ расширения",
# см. data/p3_guard_cache/task5_latency_signature_result.json.
BASELINE_NONE_BUCKET_USD = 106996.42914095546 + 31593.06966612351  # tail + top10, distance_bucket='none'
BASELINE_TOTAL_USD = (
    71703.02997834956 + 10658.052624598342 + 2148.2199366512873 + 6956.5477226022995 + 106996.42914095546
    + 43046.253010301094 + 4427.070839023741 + 310.5011108015113 + 5452.689560200912 + 31593.06966612351
)


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
    select p.tx_hash, p.profit_usd,
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
    select pt.tx_hash, pt.profit_usd, nc.min_distance,
        case
            when nc.min_distance is null then 'none'
            when nc.min_distance = 0 then '0'
            when nc.min_distance = 1 then '1'
            when nc.min_distance = 2 then '2'
            else '3+'
        end as distance_bucket
    from (select distinct tx_hash, profit_usd from profitable_pool_touch) pt
    left join nearest_catalyst nc on nc.tx_hash = pt.tx_hash
)
select distance_bucket, count(*) as n_txs, sum(profit_usd) as total_profit_usd
from final_rows
group by distance_bucket
order by distance_bucket
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
    print(f"[no_catalyst_extend] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")
    print(f"[no_catalyst_extend] потолок пространства '{ns}': {NAMESPACE_BUDGET} (владелец, 2026-09-11)")

    client = DuneClient()
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stage": "A: порог $500, окно 15 блоков (без изменений) -- см. docstring про B/C, не сделаны здесь",
        "large_swap_usd_threshold": LARGE_SWAP_USD,
        "lookback_blocks": LOOKBACK_BLOCKS,
        "target_day_start_utc": TARGET_DAY_START,
        "target_day_end_utc": TARGET_DAY_END,
        "baseline_measurement1_none_bucket_usd": BASELINE_NONE_BUCKET_USD,
        "baseline_measurement1_total_usd": BASELINE_TOTAL_USD,
    }

    day_start, day_end = TARGET_DAY_START, TARGET_DAY_END

    print("=== A. Реальная медианная цена ETH/USDG (тот же день) ===")
    sql_price = build_price_sql(day_start, day_end)
    qid_price = client.create_query("task5_nocat_eth_price", sql_price)
    df_price = client.run_sql_cached("task5_nocat_eth_price", sql_price, query_id=qid_price,
                                      estimated_credits=3.0, expected_max_rows=5, expected_columns=1)
    eth_price_usdg = None
    if df_price is not None and len(df_price) and df_price["median_weth_price_in_usdg"].iloc[0] is not None:
        eth_price_usdg = float(df_price["median_weth_price_in_usdg"].iloc[0])
    result["eth_price_usdg"] = eth_price_usdg
    if eth_price_usdg is None:
        result["blocker"] = "цена не получена"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1

    print("\n=== B. Ступень A: порог $500, окно 15 блоков ===")
    sql = build_sql(day_start, day_end).replace("{eth_price}", repr(eth_price_usdg))
    qid = client.create_query("task5_nocat_stageA_v1", sql)
    # Честная оценка: та же структура, что измерение 1 (233.22 за $5000/15
    # блоков) -- при более низком пороге кандидатов "крупных" свопов больше,
    # ожидаем СРАВНИМЫЙ или несколько выше порядок стоимости, не радикально
    # другой (окно НЕ меняется -- главный ценовой драйвер self-join не тронут).
    df = client.run_sql_cached("task5_nocat_stageA_v1", sql, query_id=qid,
                                estimated_credits=260.0, expected_max_rows=10, expected_columns=3)
    rows = df.to_dict("records") if df is not None else []
    result["stageA_distribution"] = rows
    print(f"[no_catalyst_extend] строк: {len(rows)}")
    for r in rows:
        print(f"  {r}")

    none_row = next((r for r in rows if r.get("distance_bucket") == "none"), None)
    new_none_usd = (none_row or {}).get("total_profit_usd") or 0.0
    newly_explained_usd = BASELINE_NONE_BUCKET_USD - new_none_usd
    result["stageA_none_bucket_usd_after"] = new_none_usd
    result["stageA_newly_explained_usd"] = newly_explained_usd
    result["stageA_share_of_baseline_none_now_explained"] = (
        newly_explained_usd / BASELINE_NONE_BUCKET_USD if BASELINE_NONE_BUCKET_USD else None
    )
    print(f"[no_catalyst_extend] было необъяснено (порог $5000): ${BASELINE_NONE_BUCKET_USD:,.2f}")
    print(f"[no_catalyst_extend] осталось необъяснено (порог $500, то же окно): ${new_none_usd:,.2f}")
    print(f"[no_catalyst_extend] доля объяснённых после понижения порога: "
          f"{result['stageA_share_of_baseline_none_now_explained']}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    result["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[no_catalyst_extend] итого: {total_cost:.4f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
