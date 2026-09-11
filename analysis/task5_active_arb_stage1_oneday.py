#!/usr/bin/env python3
"""Задача 5 владельца, Шаг 1 -- ОБЯЗАТЕЛЬНАЯ оценка на ОДНОМ дне перед
30-дневным прогоном. v3 этого скрипта (2026-09-11, после реального
разбора владельцем причины $178.8M/день в v2): добавлены ТРИ фильтра
владельца, применяются ВМЕСТЕ:

1. Только КАНОНИЧЕСКИЙ Factory (`0x1f7d7550b1b028f7571e69a784071f0205fd2efa`,
   PROJECT_STATE) -- отсекает ~430k спам-фабрик (Шаг 0b).
2. Реальная ликвидность: >=4 различных адреса-контрагента в пуле за
   день (владелец: "пул, где торгуют <=3 адреса -- не рынок").
3. Исключение самоторговли (тот же метод, что уже применялся к
   0x65050a на этой же цепи, см. P5/docs/PROJECT_STATE.md):
   (a) исполнитель (evt_tx_from) -- LP-провайдер (`owner` из реального
       Mint-события, Шаг 0c) этого же пула, ИЛИ
   (b) доля свопов исполнителя в этом пуле за этот день > 50%.

v4 (`uniswap_v4_robinhood.swaps`) не имеет ни классического Factory
(singleton PoolManager, уже трактуется как канонический), ни Mint-
события в этой форме (другая модель ликвидности) -- фильтр 1/3a к нему
не применяется; фильтры 2/3b применяются с `sender` как ПРИБЛИЖЕНИЕM
исполнителя (дешевле, чем join на transactions для ВСЕХ v4-свопов
дня, не только кандидатов в арбитраж) -- честно помечено, тот же
класс оговорки, что уже принят в этом репозитории для `sender` в
Uniswap-событиях (dune_pool_volume_query1.py: "sender -- это msg.sender
ПУЛА... может отражать 'все ходят через один роутер'").

Санитарная проверка владельца: суммарная прибыль за день не может
правдоподобно превышать разумную долю оборота цепи (~$1.5B/день на
пике) -- если total_profit_usd > $1-2M, результат ЯВНО помечается как
всё ещё контаминированный, НЕ как факт."""
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

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_active_arb_stage1_oneday_result.json")
NAMESPACE_BUDGET = 400.0

WETH = "0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH_USDG_POOL = "52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
WETH_DECIMALS, USDG_DECIMALS = 18, 6
CANONICAL_FACTORY = "1f7d7550b1b028f7571e69a784071f0205fd2efa"
DUST_THRESHOLD_USD = 1.0
MIN_DISTINCT_TRADERS_PER_POOL_DAY = 4  # владелец: "<=3 -- не рынок"
MAX_DOMINANT_SHARE = 0.5  # владелец: "> 50%"

PREREG_THRESHOLD_USD_PER_DAY = 500.0
SANITY_MAX_PLAUSIBLE_DAILY_PROFIT_USD = 2_000_000.0  # владелец, 2026-09-11: явный санитарный потолок


def build_detect_sql(day_start: str, day_end: str) -> str:
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
v3_swaps_raw as (
    select
        s.evt_tx_hash as tx_hash,
        s.evt_block_number as block_number,
        s.evt_block_time as block_time,
        s.evt_block_date as block_date,
        s.contract_address as pool_addr,
        to_hex(s.contract_address) as pool_key,
        s.evt_tx_from as executor_raw,
        to_hex(s.evt_tx_from) as executor,
        case
            when cp.token0 = from_hex('{WETH}') then cast(-s.amount0 as double) / 1e{WETH_DECIMALS}
            when cp.token1 = from_hex('{WETH}') then cast(-s.amount1 as double) / 1e{WETH_DECIMALS}
            else 0.0
        end as weth_delta,
        case
            when cp.token0 = from_hex('{USDG}') then cast(-s.amount0 as double) / 1e{USDG_DECIMALS}
            when cp.token1 = from_hex('{USDG}') then cast(-s.amount1 as double) / 1e{USDG_DECIMALS}
            else 0.0
        end as usdg_delta
    from uniswap_v3_robinhood.uniswapv3pool_evt_swap s
    inner join canonical_pools cp on cp.pool = s.contract_address
    where s.evt_block_time >= timestamp '{day_start}' and s.evt_block_time < timestamp '{day_end}'
),
v3_pool_day_diversity as (
    select pool_addr, block_date, count(distinct executor_raw) as n_distinct_traders
    from v3_swaps_raw
    group by pool_addr, block_date
),
v3_pool_day_address_counts as (
    select pool_addr, block_date, executor_raw, count(*) as n_swaps
    from v3_swaps_raw
    group by pool_addr, block_date, executor_raw
),
v3_pool_day_totals as (
    select pool_addr, block_date, sum(n_swaps) as total_swaps
    from v3_pool_day_address_counts
    group by pool_addr, block_date
),
v3_legs as (
    select r.tx_hash, r.block_number, r.block_time, r.pool_key, r.executor, r.weth_delta, r.usdg_delta
    from v3_swaps_raw r
    inner join v3_pool_day_diversity div
        on div.pool_addr = r.pool_addr and div.block_date = r.block_date
        and div.n_distinct_traders >= {MIN_DISTINCT_TRADERS_PER_POOL_DAY}
    inner join v3_pool_day_address_counts cnt
        on cnt.pool_addr = r.pool_addr and cnt.block_date = r.block_date and cnt.executor_raw = r.executor_raw
    inner join v3_pool_day_totals tot
        on tot.pool_addr = r.pool_addr and tot.block_date = r.block_date
    left join lp_providers lp
        on lp.pool = r.pool_addr and lp.lp_address = r.executor_raw
    where lp.lp_address is null
      and cast(cnt.n_swaps as double) / tot.total_swaps <= {MAX_DOMINANT_SHARE}
),
v4_swaps_raw as (
    select
        w.tx_hash as tx_hash,
        w.block_number as block_number,
        w.block_time as block_time,
        w.block_date as block_date,
        concat(to_hex(least(w.token_bought_address, w.token_sold_address)), '-',
               to_hex(greatest(w.token_bought_address, w.token_sold_address)), '-',
               cast(w.fee as varchar), '-', to_hex(w.hooks)) as pool_key,
        w.sender as executor_raw,
        cast(null as varchar) as executor,
        case
            when w.token_bought_address = from_hex('{WETH}') then cast(w.token_bought_amount_raw as double) / 1e{WETH_DECIMALS}
            when w.token_sold_address = from_hex('{WETH}') then -cast(w.token_sold_amount_raw as double) / 1e{WETH_DECIMALS}
            else 0.0
        end as weth_delta,
        case
            when w.token_bought_address = from_hex('{USDG}') then cast(w.token_bought_amount_raw as double) / 1e{USDG_DECIMALS}
            when w.token_sold_address = from_hex('{USDG}') then -cast(w.token_sold_amount_raw as double) / 1e{USDG_DECIMALS}
            else 0.0
        end as usdg_delta
    from uniswap_v4_robinhood.swaps w
    where w.block_time >= timestamp '{day_start}' and w.block_time < timestamp '{day_end}'
),
v4_pool_day_diversity as (
    select pool_key, block_date, count(distinct executor_raw) as n_distinct_traders
    from v4_swaps_raw
    group by pool_key, block_date
),
v4_pool_day_address_counts as (
    select pool_key, block_date, executor_raw, count(*) as n_swaps
    from v4_swaps_raw
    group by pool_key, block_date, executor_raw
),
v4_pool_day_totals as (
    select pool_key, block_date, sum(n_swaps) as total_swaps
    from v4_pool_day_address_counts
    group by pool_key, block_date
),
v4_legs as (
    select r.tx_hash, r.block_number, r.block_time, r.pool_key, r.executor, r.weth_delta, r.usdg_delta
    from v4_swaps_raw r
    inner join v4_pool_day_diversity div
        on div.pool_key = r.pool_key and div.block_date = r.block_date
        and div.n_distinct_traders >= {MIN_DISTINCT_TRADERS_PER_POOL_DAY}
    inner join v4_pool_day_address_counts cnt
        on cnt.pool_key = r.pool_key and cnt.block_date = r.block_date and cnt.executor_raw = r.executor_raw
    inner join v4_pool_day_totals tot
        on tot.pool_key = r.pool_key and tot.block_date = r.block_date
    where cast(cnt.n_swaps as double) / tot.total_swaps <= {MAX_DOMINANT_SHARE}
),
all_legs as (
    select * from v3_legs
    union all
    select * from v4_legs
),
arb_candidates as (
    select tx_hash
    from all_legs
    group by tx_hash
    having count(distinct pool_key) >= 2
),
arb_agg as (
    select
        l.tx_hash,
        min(l.block_number) as block_number,
        min(l.block_time) as block_time,
        count(*) as n_legs,
        count(distinct l.pool_key) as n_pools,
        sum(l.weth_delta) as profit_weth,
        sum(l.usdg_delta) as profit_usdg,
        max(l.executor) as v3_executor
    from all_legs l
    inner join arb_candidates c on c.tx_hash = l.tx_hash
    group by l.tx_hash
)
select
    a.tx_hash, a.block_number, a.block_time, a.n_legs, a.n_pools,
    coalesce(a.v3_executor, to_hex(t."from")) as executor,
    hour(a.block_time) as hour_utc,
    a.profit_usdg + a.profit_weth * {{eth_price}} as profit_usd
from arb_agg a
left join robinhood.transactions t
    on t.hash = a.tx_hash
    and t.block_date >= date '{day_start[:10]}' and t.block_date <= date '{day_end[:10]}'
"""


SUMMARY_SQL = f"""
select
    count(*) as n_arb_txs,
    count(*) filter (where profit_usd > {DUST_THRESHOLD_USD}) as n_profitable_arb_txs,
    sum(profit_usd) filter (where profit_usd > {DUST_THRESHOLD_USD}) as total_profit_usd,
    approx_percentile(profit_usd, 0.5) filter (where profit_usd > {DUST_THRESHOLD_USD}) as median_profit_usd,
    approx_percentile(profit_usd, 0.9) filter (where profit_usd > {DUST_THRESHOLD_USD}) as p90_profit_usd,
    count(distinct executor) filter (where profit_usd > {DUST_THRESHOLD_USD}) as n_distinct_profitable_executors
from query_DETECT_ID
"""

TOP_EXECUTORS_SQL = f"""
select executor, sum(profit_usd) as total_profit_usd, count(*) as n_profitable_txs
from query_DETECT_ID
where profit_usd > {DUST_THRESHOLD_USD} and executor is not null
group by executor
order by total_profit_usd desc
limit 500
"""

HOURLY_SQL = f"""
select hour_utc, count(*) as n_profitable_txs, sum(profit_usd) as total_profit_usd
from query_DETECT_ID
where profit_usd > {DUST_THRESHOLD_USD}
group by hour_utc
order by hour_utc
"""


def build_price_sql(day_start: str, day_end: str) -> str:
    return f"""
select approx_percentile(
    power(cast(sqrtpricex96 as double) / power(2, 96), 2) * power(10, {18 - USDG_DECIMALS}),
    0.5
) as median_weth_price_in_usdg
from uniswap_v3_robinhood.uniswapv3pool_evt_swap
where contract_address = from_hex('{WETH_USDG_POOL}')
  and evt_block_time >= timestamp '{day_start}' and evt_block_time < timestamp '{day_end}'
"""


def run() -> int:
    ns = credit_guard.namespace()
    credit_guard.ensure_namespace(ns, NAMESPACE_BUDGET)
    remaining = credit_guard.remaining_cycle_budget(credit_guard.load_state())
    print(f"[task5_stage1] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "filters_applied": {
                         "canonical_factory_only": CANONICAL_FACTORY,
                         "min_distinct_traders_per_pool_day": MIN_DISTINCT_TRADERS_PER_POOL_DAY,
                         "max_dominant_share": MAX_DOMINANT_SHARE,
                         "v4_note": "v4 не имеет Factory/Mint в этой форме -- фильтр 1/3a только v3, "
                                     "фильтры 2/3b на v4 используют 'sender' как приближение исполнителя "
                                     "(дешевле полного join на transactions для всех свопов дня)",
                     }}

    now = datetime.now(timezone.utc)
    day_end_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_dt = day_end_dt - timedelta(days=1)
    day_start = day_start_dt.strftime("%Y-%m-%d %H:%M:%S")
    day_end = day_end_dt.strftime("%Y-%m-%d %H:%M:%S")
    result["probe_day_start_utc"] = day_start
    result["probe_day_end_utc"] = day_end

    print(f"\n=== B. Реальная медианная цена ETH/USDG ===")
    spent_before_b = credit_guard.load_state()[ns]["spent"]
    sql_price = build_price_sql(day_start, day_end)
    qid_price = client.create_query("task5_eth_usdg_price_oneday_v3", sql_price)
    df_price = client.run_sql_cached("task5_eth_usdg_price_oneday_v3", sql_price, query_id=qid_price,
                                      estimated_credits=3.0, expected_max_rows=5, expected_columns=1)
    spent_after_b = credit_guard.load_state()[ns]["spent"]
    cost_b = spent_after_b - spent_before_b
    eth_price_usdg = None
    if df_price is not None and len(df_price) and df_price["median_weth_price_in_usdg"].iloc[0] is not None:
        eth_price_usdg = float(df_price["median_weth_price_in_usdg"].iloc[0])
    result["step_b_cost_credits"] = cost_b
    result["eth_price_usdg_median_this_day"] = eth_price_usdg
    print(f"[task5_stage1] Шаг B стоимость: {cost_b:.4f}, медианная цена ETH/USDG: {eth_price_usdg}")
    if eth_price_usdg is None:
        result["blocker"] = "Реальная цена ETH/USDG не получена -- останов."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"[task5_stage1] {result['blocker']}")
        return 1

    print(f"\n=== A. Материализация детекции С ТРЕМЯ ФИЛЬТРАМИ (fetch_results=False) ===")
    spent_before_a = credit_guard.load_state()[ns]["spent"]
    sql_detect = build_detect_sql(day_start, day_end).replace("{eth_price}", repr(eth_price_usdg))
    qid_detect = client.create_query("task5_arb_detect_oneday_v3", sql_detect)
    client.run_sql_cached("task5_arb_detect_oneday_v3", sql_detect, query_id=qid_detect,
                           estimated_credits=30.0, fetch_results=False)
    spent_after_a = credit_guard.load_state()[ns]["spent"]
    cost_a = spent_after_a - spent_before_a
    result["step_a_materialize_cost_credits"] = cost_a
    print(f"[task5_stage1] Шаг A (материализация, с фильтрами) стоимость: {cost_a:.4f}, query_id={qid_detect}")

    def run_followup(name: str, sql_template: str, max_rows: int, max_cols: int, est: float) -> list[dict]:
        sql = sql_template.replace("query_DETECT_ID", f"query_{qid_detect}")
        spent_before = credit_guard.load_state()[ns]["spent"]
        qid = client.create_query(name, sql)
        df = client.run_sql_cached(name, sql, query_id=qid, estimated_credits=est,
                                    expected_max_rows=max_rows, expected_columns=max_cols)
        spent_after = credit_guard.load_state()[ns]["spent"]
        cost = spent_after - spent_before
        rows = df.to_dict("records") if df is not None else []
        print(f"[task5_stage1] {name}: стоимость={cost:.4f}, строк={len(rows)}")
        result[f"{name}_cost_credits"] = cost
        return rows

    print(f"\n=== A1. Дневная сводка ===")
    summary_rows = run_followup("task5_arb_summary_oneday_v3", SUMMARY_SQL, max_rows=5, max_cols=6, est=3.0)
    if summary_rows:
        result.update(summary_rows[0])

    print(f"\n=== A2. Топ-500 исполнителей ===")
    executor_rows = run_followup("task5_arb_top_executors_oneday_v3", TOP_EXECUTORS_SQL, max_rows=500, max_cols=3, est=3.0)
    result["top_executors_by_profit_usd"] = executor_rows

    print(f"\n=== A3. Почасовое распределение ===")
    hourly_rows = run_followup("task5_arb_hourly_oneday_v3", HOURLY_SQL, max_rows=30, max_cols=3, est=3.0)
    result["hourly_distribution"] = hourly_rows

    total_cost = (result.get("step_b_cost_credits", 0.0) + result.get("step_a_materialize_cost_credits", 0.0)
                  + result.get("task5_arb_summary_oneday_v3_cost_credits", 0.0)
                  + result.get("task5_arb_top_executors_oneday_v3_cost_credits", 0.0)
                  + result.get("task5_arb_hourly_oneday_v3_cost_credits", 0.0))
    result["total_cost_this_run_credits"] = total_cost
    result["extrapolated_30day_credits"] = total_cost * 30 * 1.3
    print(f"\n[task5_stage1] РЕАЛЬНАЯ суммарная стоимость (с фильтрами, 1 день): {total_cost:.4f}")
    print(f"[task5_stage1] экстраполяция на 30 дней (+30% запас): {result['extrapolated_30day_credits']:.2f}")

    if executor_rows:
        sorted_execs = sorted(executor_rows, key=lambda r: -r["total_profit_usd"])
        if len(sorted_execs) >= 2:
            positions_2_10 = sorted_execs[1:10]
            result["sum_profit_usd_positions_2_10_this_day"] = sum(r["total_profit_usd"] for r in positions_2_10)
        else:
            result["sum_profit_usd_positions_2_10_this_day"] = 0.0

    result["preregistration_threshold_usd_per_day"] = PREREG_THRESHOLD_USD_PER_DAY

    total_profit = result.get("total_profit_usd")
    result["sanity_check_max_plausible_usd"] = SANITY_MAX_PLAUSIBLE_DAILY_PROFIT_USD
    if total_profit is not None:
        result["sanity_check_passed"] = total_profit <= SANITY_MAX_PLAUSIBLE_DAILY_PROFIT_USD
        if not result["sanity_check_passed"]:
            result["sanity_check_verdict"] = (
                f"ПРОВАЛЕНА: total_profit_usd={total_profit:,.2f} > "
                f"{SANITY_MAX_PLAUSIBLE_DAILY_PROFIT_USD:,.0f} -- результат ВСЁ ЕЩЁ контаминирован, "
                "НЕ докладывать как факт, нужен дальнейший разбор методологии, не 30-дневный прогон."
            )
        else:
            result["sanity_check_verdict"] = "ПРОЙДЕНА -- результат правдоподобен по масштабу, можно рассматривать как реальную оценку."
        print(f"\n[task5_stage1] САНИТАРНАЯ ПРОВЕРКА: {result['sanity_check_verdict']}")

    result["preregistration_note"] = ("Это ОДИН день -- предрегистрация требует устойчивого суточного факта, "
                                       "честная проверка порога только после 30-дневного прогона (медиана по дням).")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"[task5_stage1] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
