#!/usr/bin/env python3
"""Задача 5, владелец 2026-09-11 (новый порядок -- ПЕРВЫМ, до выборки 2000
хэшей): пересечение группы 'none' (без катализатора) с известными
адресами накрутки, для 2026-09-10.

ЧЕСТНАЯ ОГОВОРКА ПЕРЕД ЗАПУСКОМ: владелец назвал это "локально, ноль
кредитов" -- но у нас НЕТ локально скачанных данных на уровне
(транзакция, исполнитель, дистанция) для 09-10: и измерение 1
(латентность), и кросс-таб дистанция x газ отдавали Dune только УЖЕ
АГРЕГИРОВАННЫЕ строки (group by на стороне Dune, executor туда не
попадал вообще). Пересечь дистанцию с конкретными адресами без нового
запроса невозможно -- это НЕ ноль кредитов, а тот же дорогой self-join
катализатора, что и раньше (~230-250 кредитов, тот же порядок, что
измерение 1 и кросс-таб). Явно доложено владельцу в ответе, здесь -- сам
запрос, реальный, не угадывание.

Известные адреса накрутки (реальные, из УЖЕ полученных данных этого
проекта -- НЕ придуманы для этого скрипта):
  - 0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc -- доминирующий
    self-trading бот пула P5 (52.5% свопов пула, 0 Mint-событий, см.
    dune_query1_volume_result.json, docs/PROJECT_STATE.md).
  - 0xcaf681a66d020601342297493863e78c959e5cb2,
    0x39b38686a19836ac10162c490e4558e120cbbe5f -- из того же реального
    top-volume списка (dune_query1_volume_result.json).
  - 0xe492912f37c2a4eca45d42dc67548f4c6cd7ce2b -- из
    fomo_forensics_trading_venue_result.json.
  - 0x8876789976decbfcbbbe364623c63652db8c0904 -- из
    asset_consolidation_rh_router_verify_result.json /
    fomo_forensics_pool_dominant_event_sample_result.json.

Плюс генерический признак "профиль накрутки" -- адрес с >= 100
КВАЛИФИЦИРУЮЩИМИСЯ (>=2 затронутых пула, т.е. уже прошедшими фильтр
диверсификации/доминирования) закрыто-циклическими транзакциями за ДЕНЬ
(владелец: "сотни tx/день") -- ЧЕСТНО не то же самое, что "зеркальный
объём" (это потребовало бы sender==recipient на уровне каждого свопа,
не сделано здесь ради экономии кредитов), но разумный дешёвый прокси
внутри уже оплаченного прохода."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task5_active_arb_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")
os.environ.setdefault("CREDIT_GUARD_SANITY_MAX_ESTIMATE", "300")

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_none_bucket_wash_overlap_result.json")
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
LARGE_SWAP_USD = 5000.0
LOOKBACK_BLOCKS = 15
HIGH_FREQ_THRESHOLD = 100  # владелец: "сотни tx/день"

KNOWN_WASH_ADDRESSES = [
    "65050a9b7e5075a2ba5ced7b1b64ee66262c40dc",
    "caf681a66d020601342297493863e78c959e5cb2",
    "39b38686a19836ac10162c490e4558e120cbbe5f",
    "e492912f37c2a4eca45d42dc67548f4c6cd7ce2b",
    "8876789976decbfcbbbe364623c63652db8c0904",
]

TARGET_DAY_START = "2026-09-10 00:00:00"
TARGET_DAY_END = "2026-09-11 00:00:00"


def build_sql(day_start: str, day_end: str) -> str:
    wash_list = ", ".join(f"upper('{a}')" for a in KNOWN_WASH_ADDRESSES)
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
    select tx_hash, min(block_number) as block_number,
           max(executor) as v3_executor,
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
executor_daily_counts as (
    select executor, count(*) as n_qualifying_txs
    from profitable_txs
    group by executor
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
final_rows as (
    select pt.tx_hash, pt.executor, pt.profit_usd, nc.min_distance, dc.n_qualifying_txs,
        case
            when nc.min_distance is null then 'none'
            when nc.min_distance = 0 then '0'
            when nc.min_distance = 1 then '1'
            when nc.min_distance = 2 then '2'
            else '3+'
        end as distance_bucket,
        case
            when upper(pt.executor) in ({wash_list}) then 'known_wash'
            when dc.n_qualifying_txs >= {HIGH_FREQ_THRESHOLD} then 'high_freq_other'
            else 'other'
        end as category
    from (select distinct tx_hash, executor, profit_usd from profitable_pool_touch) pt
    inner join executor_daily_counts dc on dc.executor = pt.executor
    left join nearest_catalyst nc on nc.tx_hash = pt.tx_hash
)
select distance_bucket, category,
    count(*) as n_txs,
    count(distinct executor) as n_distinct_executors,
    sum(profit_usd) as total_profit_usd
from final_rows
group by distance_bucket, category
order by distance_bucket, category
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
    print(f"[none_wash_overlap] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "large_swap_usd_threshold": LARGE_SWAP_USD,
        "lookback_blocks": LOOKBACK_BLOCKS,
        "high_freq_threshold_tx_per_day": HIGH_FREQ_THRESHOLD,
        "known_wash_addresses": KNOWN_WASH_ADDRESSES,
        "target_day_start_utc": TARGET_DAY_START,
        "target_day_end_utc": TARGET_DAY_END,
    }

    day_start, day_end = TARGET_DAY_START, TARGET_DAY_END

    print("=== A. Реальная медианная цена ETH/USDG ===")
    sql_price = build_price_sql(day_start, day_end)
    qid_price = client.create_query("task5_nonewash_eth_price", sql_price)
    df_price = client.run_sql_cached("task5_nonewash_eth_price", sql_price, query_id=qid_price,
                                      estimated_credits=3.0, expected_max_rows=5, expected_columns=1)
    eth_price_usdg = None
    if df_price is not None and len(df_price) and df_price["median_weth_price_in_usdg"].iloc[0] is not None:
        eth_price_usdg = float(df_price["median_weth_price_in_usdg"].iloc[0])
    result["eth_price_usdg"] = eth_price_usdg
    if eth_price_usdg is None:
        result["blocker"] = "цена не получена"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1

    print("\n=== B. Пересечение none/0/1/2/3+ с известной накруткой ===")
    sql = build_sql(day_start, day_end).replace("{eth_price}", repr(eth_price_usdg))
    qid = client.create_query("task5_none_wash_overlap_v1", sql)
    df = client.run_sql_cached("task5_none_wash_overlap_v1", sql, query_id=qid,
                                estimated_credits=250.0, expected_max_rows=20, expected_columns=5)
    rows = df.to_dict("records") if df is not None else []
    result["distribution"] = rows
    print(f"[none_wash_overlap] строк: {len(rows)}")
    for r in rows:
        print(f"  {r}")

    def is_missing(v: object) -> bool:
        return v is None or v != v

    none_rows = [r for r in rows if r.get("distance_bucket") == "none"]
    none_total_usd = sum(r["total_profit_usd"] for r in none_rows)
    none_wash_usd = sum(r["total_profit_usd"] for r in none_rows if r.get("category") == "known_wash")
    none_highfreq_usd = sum(r["total_profit_usd"] for r in none_rows if r.get("category") == "high_freq_other")
    none_total_tx = sum(r["n_txs"] for r in none_rows)
    none_wash_tx = sum(r["n_txs"] for r in none_rows if r.get("category") == "known_wash")
    none_highfreq_tx = sum(r["n_txs"] for r in none_rows if r.get("category") == "high_freq_other")

    summary = {}
    for bucket in ("0", "1", "2", "3+", "none"):
        brows = [r for r in rows if r.get("distance_bucket") == bucket]
        total_usd = sum(r["total_profit_usd"] for r in brows)
        wash_usd = sum(r["total_profit_usd"] for r in brows if r.get("category") == "known_wash")
        hf_usd = sum(r["total_profit_usd"] for r in brows if r.get("category") == "high_freq_other")
        total_tx = sum(r["n_txs"] for r in brows)
        wash_tx = sum(r["n_txs"] for r in brows if r.get("category") == "known_wash")
        hf_tx = sum(r["n_txs"] for r in brows if r.get("category") == "high_freq_other")
        summary[bucket] = {
            "total_usd": total_usd,
            "known_wash_usd": wash_usd,
            "known_wash_share_usd": (wash_usd / total_usd) if total_usd else None,
            "high_freq_other_usd": hf_usd,
            "high_freq_other_share_usd": (hf_usd / total_usd) if total_usd else None,
            "combined_wash_plus_highfreq_share_usd": ((wash_usd + hf_usd) / total_usd) if total_usd else None,
            "total_tx": total_tx,
            "known_wash_tx": wash_tx,
            "known_wash_share_tx": (wash_tx / total_tx) if total_tx else None,
            "high_freq_other_tx": hf_tx,
            "high_freq_other_share_tx": (hf_tx / total_tx) if total_tx else None,
        }
    result["summary_by_bucket"] = summary
    result["none_bucket_wash_share_usd"] = (none_wash_usd / none_total_usd) if none_total_usd else None
    result["none_bucket_combined_share_usd"] = (
        (none_wash_usd + none_highfreq_usd) / none_total_usd if none_total_usd else None
    )
    result["verdict_gt_50pct"] = bool(
        none_total_usd and (none_wash_usd + none_highfreq_usd) / none_total_usd > 0.5
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"[none_wash_overlap] ВЕРДИКТ (>50% 'none' -- внутренние циклы накрутки): {result['verdict_gt_50pct']}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    result["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[none_wash_overlap] итого: {total_cost:.4f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
