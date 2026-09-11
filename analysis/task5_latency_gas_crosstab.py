#!/usr/bin/env python3
"""Задача 5, дозапрос владельца 2026-09-11 (после измерений 1-3): реальный
кросс-таб "расстояние до катализатора" x "размер прибыли" и "расстояние" x
"выживаемость при росте газа" -- проверка гипотезы "мелкие захваты, не
взятые коллоцированными ботами (расстояние >=1 блок), умирают от газа
ПЕРВЫМИ, поэтому доступная неколлоцированному игроку часть приза исчезает
после 29.09 быстрее, чем весь приз в среднем".

ЧЕСТНАЯ ОГОВОРКА ПО ДНЮ: владелец просил "по прибыльным транзакциям за
09.09-10.09" -- ОДНАКО измерение 1 (латентная подпись, расстояние до
катализатора) реально считалось ТОЛЬКО для 2026-09-10 (self-join на 15
блоков вглубь -- дорогая часть, 233.22 кредита за один день), а измерение
3 (газовая чувствительность) считалось для 09-09 И 09-10, но БЕЗ
расстояния до катализатора вообще (агрегат по неделям, не по дистанции).
Нет готового датасета, где для одной и той же транзакции одновременно
известны И distance_bucket, И profit_usd_at_gwei -- эти два измерения
никогда не считались в одном проходе. Комбинировать по одному дню
(09-09) с другим (09-10) "на глаз" значило бы взять расстояние с одного
дня, а газ -- с другого, для РАЗНЫХ транзакций -- то есть подделать
данные, чего делать нельзя.

Реальное решение: пересчитать ОБА измерения ЗАНОВО В ОДНОМ ПРОХОДЕ, но
только для ОДНОГО дня (2026-09-10, тот же день, что и измерение 1 --
прямая сравнимость с уже опубликованным распределением по дистанциям).
Считать оба дня удвоило бы стоимость самого дорогого узла (self-join
катализатора, ~233 кредита за день) сверх потолка "300 кредитов на
блок" -- расширение на 09-09 возможно отдельным дозапросом, если
владелец подтвердит бюджет."""
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
os.environ.setdefault("CREDIT_GUARD_SANITY_MAX_ESTIMATE", "300")  # тот же потолок, что и у измерения 1

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_latency_gas_crosstab_result.json")
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
GAS_PRICE_POINTS_GWEI = [0.1, 0.5, 1.0]
# Владелец: "$10 / $10-100 / >$100" -- бакеты по РЕАЛЬНОМУ profit_usd
# транзакции (не по нотионалу свопа) -- гипотеза именно про размер
# ЗАХВАЧЕННОЙ прибыли, а не про размер сделки.
SIZE_BUCKET_CASE = """case
    when pt.profit_usd < 10.0 then '<$10'
    when pt.profit_usd < 100.0 then '$10-100'
    else '>$100'
end"""

# Тот же день, что и измерение 1 (task5_latency_signature.py, 2026-09-10) --
# см. docstring выше.
TARGET_DAY_START = "2026-09-10 00:00:00"
TARGET_DAY_END = "2026-09-11 00:00:00"


def build_sql(day_start: str, day_end: str) -> str:
    gas_cols = ",\n        ".join(
        f"fr.profit_usd - (coalesce(fr.gas_used, 0) * {g} * 1e9 / 1e{WETH_DECIMALS} * {{eth_price}}) "
        f"as profit_usd_at_{str(g).replace('.', '_')}gwei"
        for g in GAS_PRICE_POINTS_GWEI
    )
    survive_cols = ",\n    ".join(
        f"count(*) filter (where profit_usd_at_{str(g).replace('.', '_')}gwei > {DUST_THRESHOLD_USD}) "
        f"as n_survive_{str(g).replace('.', '_')}gwei"
        for g in GAS_PRICE_POINTS_GWEI
    )
    sum_cols = ",\n    ".join(
        f"sum(profit_usd_at_{str(g).replace('.', '_')}gwei) as total_profit_usd_at_{str(g).replace('.', '_')}gwei"
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
        t.gas_used as gas_used,
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
    select tx_hash, block_number, executor, profit_usd, gas_used, touched_pools
    from profit_calc
    where is_closed_cycle and is_priced_exit and profit_usd > {DUST_THRESHOLD_USD}
),
profitable_pool_touch as (
    select tx_hash, block_number, executor, profit_usd, gas_used, pk as pool_key
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
final_rows_base as (
    select pt.tx_hash, pt.executor, pt.profit_usd, pt.gas_used, nc.min_distance,
        case
            when nc.min_distance is null then 'none'
            when nc.min_distance = 0 then '0'
            when nc.min_distance = 1 then '1'
            when nc.min_distance = 2 then '2'
            else '3+'
        end as distance_bucket,
        {SIZE_BUCKET_CASE} as size_bucket
    from (select distinct tx_hash, executor, profit_usd, gas_used from profitable_pool_touch) pt
    left join nearest_catalyst nc on nc.tx_hash = pt.tx_hash
),
final_rows as (
    select fr.tx_hash, fr.distance_bucket, fr.size_bucket, fr.profit_usd, fr.gas_used,
        {gas_cols}
    from final_rows_base fr
)
select
    distance_bucket,
    size_bucket,
    count(*) as n_txs,
    sum(profit_usd) as total_profit_usd,
    {sum_cols},
    {survive_cols}
from final_rows
group by grouping sets ((distance_bucket, size_bucket), (distance_bucket))
order by distance_bucket, size_bucket
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
    print(f"[latency_gas_crosstab] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "large_swap_usd_threshold": LARGE_SWAP_USD,
        "lookback_blocks": LOOKBACK_BLOCKS,
        "gas_price_points_gwei": GAS_PRICE_POINTS_GWEI,
        "target_day_start_utc": TARGET_DAY_START,
        "target_day_end_utc": TARGET_DAY_END,
        "note": (
            "Один день (2026-09-10, тот же, что измерение 1 латентной подписи) -- "
            "НЕ 09-09..09-10, как буквально просил владелец. Причина: расстояние-до-"
            "катализатора и газовая чувствительность никогда не считались в одном "
            "проходе, их пришлось пересчитать заново вместе; удвоение на два дня "
            "удвоило бы стоимость самого дорогого узла (self-join катализатора) сверх "
            "потолка 300 кредитов на блок. См. docstring скрипта."
        ),
    }

    day_start, day_end = TARGET_DAY_START, TARGET_DAY_END

    print("=== A. Реальная медианная цена ETH/USDG (тот же день) ===")
    sql_price = build_price_sql(day_start, day_end)
    qid_price = client.create_query("task5_latgas_eth_price", sql_price)
    df_price = client.run_sql_cached("task5_latgas_eth_price", sql_price, query_id=qid_price,
                                      estimated_credits=3.0, expected_max_rows=5, expected_columns=1)
    eth_price_usdg = None
    if df_price is not None and len(df_price) and df_price["median_weth_price_in_usdg"].iloc[0] is not None:
        eth_price_usdg = float(df_price["median_weth_price_in_usdg"].iloc[0])
    result["eth_price_usdg"] = eth_price_usdg
    print(f"[latency_gas_crosstab] цена ETH/USDG: {eth_price_usdg}")
    if eth_price_usdg is None:
        result["blocker"] = "цена не получена"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1

    print("\n=== B. Кросс-таб дистанция x размер + дистанция x выживаемость при газе ===")
    sql = build_sql(day_start, day_end).replace("{eth_price}", repr(eth_price_usdg))
    qid = client.create_query("task5_latency_gas_crosstab_v1", sql)
    df = client.run_sql_cached("task5_latency_gas_crosstab_v1", sql, query_id=qid,
                                estimated_credits=280.0, expected_max_rows=30, expected_columns=10)
    rows = df.to_dict("records") if df is not None else []
    print(f"[latency_gas_crosstab] строк: {len(rows)}")
    for r in rows:
        print(f"  {r}")

    # РЕАЛЬНЫЙ БАГ (найден 2026-09-11, первый прогон): pandas отдаёт SQL NULL
    # в строковой колонке size_bucket как float NaN, а не Python None --
    # `v is None` НЕ ловит NaN (`NaN is not None` -- True), из-за чего все
    # 20 строк (включая grouping-sets агрегат по всем размерам) попадали в
    # table1, а table2 оставалась пустой. `v != v` -- истинно только для NaN
    # (в т.ч. без импорта math), безопасно и для строк, и для None.
    def _is_all_sizes(v: object) -> bool:
        return v is None or v != v

    # Table 1: distance x size (size_bucket не null)
    table1 = [r for r in rows if not _is_all_sizes(r.get("size_bucket"))]
    # Table 2: distance-only (size_bucket null -- grouping-sets агрегат по всем размерам)
    table2 = [r for r in rows if _is_all_sizes(r.get("size_bucket"))]

    result["table1_distance_x_size"] = table1
    result["table2_distance_x_gas_survival"] = table2

    # Ключевое число: $ в блоках >=1 (доступно без колокации), при 1.0 gwei,
    # и для сравнения -- то же при текущем субсидированном газе (0 gwei -- profit_usd как есть).
    ge1_rows = [r for r in table2 if r.get("distance_bucket") in ("1", "2", "3+")]
    total_raw_ge1 = sum(r.get("total_profit_usd") or 0.0 for r in ge1_rows)
    total_at_1gwei_ge1 = sum(r.get("total_profit_usd_at_1_0gwei") or 0.0 for r in ge1_rows)
    none_row = next((r for r in table2 if r.get("distance_bucket") == "none"), None)
    total_raw_all = sum(r.get("total_profit_usd") or 0.0 for r in table2)
    total_at_1gwei_all = sum(r.get("total_profit_usd_at_1_0gwei") or 0.0 for r in table2)

    key_numbers = {
        "total_profit_usd_distance_ge1_raw": total_raw_ge1,
        "total_profit_usd_distance_ge1_at_1_0gwei": total_at_1gwei_ge1,
        "share_surviving_at_1gwei_distance_ge1": (total_at_1gwei_ge1 / total_raw_ge1) if total_raw_ge1 else None,
        "total_profit_usd_all_raw": total_raw_all,
        "total_profit_usd_all_at_1_0gwei": total_at_1gwei_all,
        "share_surviving_at_1gwei_all": (total_at_1gwei_all / total_raw_all) if total_raw_all else None,
        "none_bucket_raw": (none_row or {}).get("total_profit_usd"),
        "none_bucket_at_1_0gwei": (none_row or {}).get("total_profit_usd_at_1_0gwei"),
        "note": (
            ">=1 блок = distance_bucket in ('1','2','3+') -- намеренно БЕЗ 'none' "
            "(транзакции без идентифицируемого катализатора -- отдельная, не "
            "колокационная категория, см. измерение 1). 'none' приведён отдельно "
            "для полноты, не включён в 'ge1'."
        ),
    }
    result["key_numbers_ge1_block_at_gas"] = key_numbers
    print(f"[latency_gas_crosstab] ключевые числа: {json.dumps(key_numbers, indent=2, ensure_ascii=False)}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    result["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[latency_gas_crosstab] итого: {total_cost:.4f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
