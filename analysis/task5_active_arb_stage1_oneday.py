#!/usr/bin/env python3
"""Задача 5 владельца, Шаг 1 -- ОБЯЗАТЕЛЬНАЯ оценка на ОДНОМ дне перед
30-дневным прогоном.

v4 этого скрипта (2026-09-11) -- ПЕРЕПИСАНА САМА ФОРМУЛА ПРИБЫЛИ, не
добавлен ещё один фильтр пула. Причина: диагностика на реальной
транзакции 0xf342eb74...d73f8 (см. docs/PROJECT_STATE.md, "Задача 5 --
гипотеза владельца о слепоте формулы ПОДТВЕРЖДЕНА") показала, что
старая формула (net-flow только по WETH/USDG) слепа СИСТЕМНО к любому
токену вне этих двух -- включая нативный ETH в v4 (address(0)) и любой
промежуточный токен многохоповой транзакции. В этой транзакции
обнулился ровно ОДИН из пяти затронутых токенов; формула засчитала
$885,502 "прибыли" по WETH-ноге, полностью проигнорировав крупный
невозмещённый отток другого токена -- это НЕ проверенная прибыль от
арбитража, а неизвестно что.

Новое определение (владелец, 2026-09-11, дословно): "профит
засчитывается только если net-flow обнуляется (в пределах разумного
эпсилон -- пыль округления) по всем токенам, кроме ровно одного.
Профит = net-flow этого одного токена × его цена." Это формальное
определение замкнутого арбитражного цикла: вошёл в один актив, прошёл
цепочку свопов, вышел в тот же (или другой) один актив -- все
промежуточные токены должны обнулиться.

Реализация:
1. Net-flow считается по КАЖДОМУ токену, затронутому транзакцией
   (не только WETH/USDG) -- включая address(0) как отдельный токен
   для нативного ETH в v4 (НЕ объединяется с WETH -- владелец явно
   попросил считать их раздельно; это может НЕДО-считать реальные
   циклы, которые заканчиваются unwrap'ом WETH->ETH, но это
   консервативная ошибка (недосчёт), не фабрикация профита).
2. "Обнулился" = |net_flow_raw| / gross_raw <= DUST_REL_EPS, где
   gross_raw = сумма |delta| по всем плечам этого токена в этой
   транзакции. Это ОТНОШЕНИЕ в сырых единицах одного и того же
   токена -- не зависит от decimals (не нужно их знать/угадывать для
   токенов, чья идентичность не установлена, как Token X в диагностике).
   DUST_REL_EPS=1e-6 выбран из первых принципов: атомарный мультихоп-
   роутер передаёт amountOut хопа N как amountIn хопа N+1 БЕЗ потерь,
   кроме округления AMM-математики на уровне wei -- относительная
   погрешность такого рода на много порядков меньше 1e-6 для любой
   сделки разумного размера; реальный найденный residual (Token X,
   ~100% от gross) на много порядков БОЛЬШЕ этого порога -- граница не
   пограничная.
3. Профит в USD считается ТОЛЬКО если единственный необнулившийся
   токен -- WETH, address(0) (нативный ETH, цена = цена WETH) или USDG
   -- единственные токены с реальной, не выдуманной ценой в этом
   проекте. Если единственный необнулившийся токен -- что-то другое
   (как Token X в диагностике) -- транзакция помечается
   `is_closed_cycle=true, is_priced_exit=false` и НЕ участвует в
   total_profit_usd (честно исключена, не оценена наугад).

Три фильтра владельца (канонический Factory, >=4 трейдера/пул/день,
исключение самоторговли) СОХРАНЕНЫ как пред-фильтр КАЧЕСТВА ПУЛА (это
отдельная, всё ещё действительная забота про спам/wash-trading), но
больше НЕ являются защитой от переоценки прибыли -- эту роль теперь
играет net-flow-closed-cycle проверка.

Реальная находка при первом прогоне (2026-09-11): первая версия
разбивки на токены строила v3_flows/v4_flows через UNION ALL с ДВУМЯ
ссылками на один и тот же v3_swaps_filtered/v4_swaps_filtered, плюс
ещё одна ссылка в отдельном swaps_level -- три пересчёта всей цепочки
фильтров (диверсификация/доминирование/LP-join) на каждую версию.
Реальная стоимость материализации оказалась 129.60 кредита против
оценённых 40 (втрое дороже) -- тот же класс бага, что уже
задокументирован в credit_guard.py (03c_cap_summary: "движок Dune не
делит вычисление общих CTE между ветками UNION ALL"). Исправлено:
`CROSS JOIN UNNEST` разбивает 1 строку свопа на 2 строки потока ОДНИМ
проходом, без повторной ссылки на CTE; `tx_meta` тоже переведён на
единый источник `all_flows` (count(*)/2 для n_legs, т.к. на каждый
исходный своп теперь ровно 2 строки).

Санитарная проверка владельца (не изменилась): суммарная прибыль за
день не может правдоподобно превышать разумную долю оборота цепи --
если total_profit_usd > $1-2M, результат ЯВНО помечается как всё ещё
контаминированный."""
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
NAMESPACE_BUDGET = 700.0  # поднято с 400.0 (2026-09-11): реальный расход достиг 336.96/400.0
# после легитимного (не ошибочного) овеrrun-стопа на первой версии разбивки по
# токенам (129.60 вместо оценённых 40 -- см. докстринг выше) -- общий цикл
# Mozila (лимит 2000, реально потрачено ~717) имеет достаточный запас,
# поднятие лимита ПРОСТРАНСТВА -- явное решение, не тихий обход гарда
# (docstring credit_guard.ensure_namespace прямо это разрешает: "аналог
# поднятия лимита Sprint 1.5").

WETH = "0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "5fc5360d0400a0fd4f2af552add042d716f1d168"
ZERO_ADDR = "0000000000000000000000000000000000000000"  # нативный ETH, конвенция Uniswap v4 (Currency.wrap(address(0)))
WETH_USDG_POOL = "52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
WETH_DECIMALS, USDG_DECIMALS = 18, 6
CANONICAL_FACTORY = "1f7d7550b1b028f7571e69a784071f0205fd2efa"
DUST_THRESHOLD_USD = 1.0  # порог "профит не шум" в USD -- как и раньше
DUST_REL_EPS = 1e-6  # порог "токен обнулился" -- ОТНОШЕНИЕ net/gross в сырых единицах, см. докстринг
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
v3_swaps_qual as (
    select
        s.evt_tx_hash as tx_hash,
        s.evt_block_number as block_number,
        s.evt_block_time as block_time,
        s.evt_block_date as block_date,
        s.contract_address as pool_addr,
        to_hex(s.contract_address) as pool_key,
        s.evt_tx_from as executor_raw,
        to_hex(s.evt_tx_from) as executor,
        cp.token0 as token0,
        cp.token1 as token1,
        s.amount0 as amount0,
        s.amount1 as amount1
    from uniswap_v3_robinhood.uniswapv3pool_evt_swap s
    inner join canonical_pools cp on cp.pool = s.contract_address
    where s.evt_block_time >= timestamp '{day_start}' and s.evt_block_time < timestamp '{day_end}'
),
v3_pool_day_diversity as (
    select pool_addr, block_date, count(distinct executor_raw) as n_distinct_traders
    from v3_swaps_qual
    group by pool_addr, block_date
),
v3_pool_day_address_counts as (
    select pool_addr, block_date, executor_raw, count(*) as n_swaps
    from v3_swaps_qual
    group by pool_addr, block_date, executor_raw
),
v3_pool_day_totals as (
    select pool_addr, block_date, sum(n_swaps) as total_swaps
    from v3_pool_day_address_counts
    group by pool_addr, block_date
),
v3_swaps_filtered as (
    select r.tx_hash, r.block_number, r.block_time, r.pool_key, r.executor, r.token0, r.token1, r.amount0, r.amount1
    from v3_swaps_qual r
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
v4_swaps_qual as (
    select
        w.tx_hash as tx_hash,
        w.block_number as block_number,
        w.block_time as block_time,
        w.block_date as block_date,
        concat(to_hex(least(w.token_bought_address, w.token_sold_address)), '-',
               to_hex(greatest(w.token_bought_address, w.token_sold_address)), '-',
               cast(w.fee as varchar), '-', to_hex(w.hooks)) as pool_key,
        w.sender as executor_raw,
        w.token_bought_address as token_bought,
        w.token_sold_address as token_sold,
        w.token_bought_amount_raw as amount_bought,
        w.token_sold_amount_raw as amount_sold
    from uniswap_v4_robinhood.swaps w
    where w.block_time >= timestamp '{day_start}' and w.block_time < timestamp '{day_end}'
),
v4_pool_day_diversity as (
    select pool_key, block_date, count(distinct executor_raw) as n_distinct_traders
    from v4_swaps_qual
    group by pool_key, block_date
),
v4_pool_day_address_counts as (
    select pool_key, block_date, executor_raw, count(*) as n_swaps
    from v4_swaps_qual
    group by pool_key, block_date, executor_raw
),
v4_pool_day_totals as (
    select pool_key, block_date, sum(n_swaps) as total_swaps
    from v4_pool_day_address_counts
    group by pool_key, block_date
),
v4_swaps_filtered as (
    select r.tx_hash, r.block_number, r.block_time, r.pool_key, r.token_bought, r.token_sold, r.amount_bought, r.amount_sold
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
    -- ВАЖНО (реальная находка, 2026-09-11): раньше здесь было
    -- UNION ALL из ДВУХ select-ов, каждый заново ссылавшийся на
    -- v3_swaps_filtered -- ровно тот же класс бага, что уже
    -- задокументирован в credit_guard.py (03c_cap_summary, ревизия 2:
    -- "движок Dune не делит вычисление общих CTE между ветками
    -- UNION ALL, а пересчитывает его в каждой ветке"). Вместе с
    -- дублирующей ссылкой в swaps_level это давало ТРИ пересчёта
    -- всей цепочки фильтров (диверсификация/доминирование/LP-join) на
    -- v3_swaps_filtered -- реально стоило 129.60 кредита вместо
    -- оценённых 40 (в ~3 раза дороже старой формулы без разбивки по
    -- токенам). Фикс: CROSS JOIN UNNEST разбивает 1 строку свопа на 2
    -- строки потока ОДНИМ проходом, без повторной ссылки на CTE.
    select tx_hash, block_number, block_time, pool_key, executor, token, delta_raw
    from v3_swaps_filtered
    cross join unnest(
        array[token0, token1],
        array[cast(-amount0 as double), cast(-amount1 as double)]
    ) as u(token, delta_raw)
),
v4_flows as (
    select tx_hash, block_number, block_time, pool_key, cast(null as varchar) as executor, token, delta_raw
    from v4_swaps_filtered
    cross join unnest(
        array[token_bought, token_sold],
        array[cast(amount_bought as double), -cast(amount_sold as double)]
    ) as u(token, delta_raw)
),
all_flows as (
    select * from v3_flows
    union all
    select * from v4_flows
),
-- ВАЖНО (реальная находка #2, 2026-09-11): предыдущая версия сканировала
-- all_flows ТРИ раза (arb_candidates, tx_meta, tx_token_flow каждый со
-- своим join к arb_candidates) -- реальная стоимость упала со 129.60
-- только до 80.86 (всё ещё > порога 80 = 2x оценки, овеrrun сработал повторно).
-- Здесь считаем n_pools_tx ОДНИМ оконным проходом по all_flows и
-- фильтруем один раз -- tx_meta/tx_token_flow читают уже отфильтрованный
-- qualifying_flows, не all_flows заново.
flow_annotated as (
    select f.*, count(distinct pool_key) over (partition by tx_hash) as n_pools_tx
    from all_flows f
),
qualifying_flows as (
    select * from flow_annotated where n_pools_tx >= 2
),
tx_meta as (
    select
        tx_hash,
        min(block_number) as block_number,
        min(block_time) as block_time,
        count(*) / 2 as n_legs,  -- ровно 2 строки на исходный своп, см. выше
        max(n_pools_tx) as n_pools,
        max(executor) as v3_executor
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
    select tx_hash, sum(is_nondust) as n_nondust_tokens, count(*) as n_tokens_touched
    from tx_token_flagged
    group by tx_hash
),
closed_cycle_exit as (
    select f.tx_hash, f.token as exit_token, f.net_flow_raw as exit_net_flow_raw
    from tx_token_flagged f
    inner join tx_nondust_count n on n.tx_hash = f.tx_hash and n.n_nondust_tokens = 1
    where f.is_nondust = 1
)
select
    m.tx_hash,
    m.block_number,
    m.block_time,
    m.n_legs,
    m.n_pools,
    coalesce(m.v3_executor, to_hex(t."from")) as executor,
    hour(m.block_time) as hour_utc,
    n.n_nondust_tokens,
    n.n_tokens_touched,
    (ce.exit_token is not null) as is_closed_cycle,
    to_hex(ce.exit_token) as exit_token_hex,
    ce.exit_net_flow_raw,
    case
        when ce.exit_token = from_hex('{WETH}') then ce.exit_net_flow_raw / 1e{WETH_DECIMALS} * {{eth_price}}
        when ce.exit_token = from_hex('{ZERO_ADDR}') then ce.exit_net_flow_raw / 1e{WETH_DECIMALS} * {{eth_price}}
        when ce.exit_token = from_hex('{USDG}') then ce.exit_net_flow_raw / 1e{USDG_DECIMALS}
        else cast(null as double)
    end as profit_usd,
    coalesce(ce.exit_token in (from_hex('{WETH}'), from_hex('{ZERO_ADDR}'), from_hex('{USDG}')), false) as is_priced_exit
from tx_meta m
inner join tx_nondust_count n on n.tx_hash = m.tx_hash
left join closed_cycle_exit ce on ce.tx_hash = m.tx_hash
left join robinhood.transactions t
    on t.hash = m.tx_hash
    and t.block_date >= date '{day_start[:10]}' and t.block_date <= date '{day_end[:10]}'
"""


SUMMARY_SQL = f"""
select
    count(*) as n_candidates_total,
    count(*) filter (where is_closed_cycle) as n_closed_cycle,
    count(*) filter (where is_closed_cycle and is_priced_exit) as n_closed_cycle_priced,
    count(*) filter (where is_closed_cycle and is_priced_exit and profit_usd > {DUST_THRESHOLD_USD}) as n_profitable_arb_txs,
    sum(profit_usd) filter (where is_closed_cycle and is_priced_exit and profit_usd > {DUST_THRESHOLD_USD}) as total_profit_usd,
    approx_percentile(profit_usd, 0.5) filter (where is_closed_cycle and is_priced_exit and profit_usd > {DUST_THRESHOLD_USD}) as median_profit_usd,
    approx_percentile(profit_usd, 0.9) filter (where is_closed_cycle and is_priced_exit and profit_usd > {DUST_THRESHOLD_USD}) as p90_profit_usd,
    count(distinct executor) filter (where is_closed_cycle and is_priced_exit and profit_usd > {DUST_THRESHOLD_USD}) as n_distinct_profitable_executors
from query_DETECT_ID
"""

TOP_EXECUTORS_SQL = f"""
select executor, sum(profit_usd) as total_profit_usd, count(*) as n_profitable_txs
from query_DETECT_ID
where is_closed_cycle and is_priced_exit and profit_usd > {DUST_THRESHOLD_USD} and executor is not null
group by executor
order by total_profit_usd desc
limit 500
"""

HOURLY_SQL = f"""
select hour_utc, count(*) as n_profitable_txs, sum(profit_usd) as total_profit_usd
from query_DETECT_ID
where is_closed_cycle and is_priced_exit and profit_usd > {DUST_THRESHOLD_USD}
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
                     "formula_version": "v4_closed_cycle_all_tokens",
                     "formula_note": ("Профит засчитывается только если net-flow обнуляется "
                                      "(|net|/gross <= 1e-6) по всем токенам транзакции, кроме "
                                      "ровно одного (включая address(0) как отдельный нативный ETH "
                                      "в v4, не объединён с WETH). Профит = net-flow этого токена x "
                                      "его цена, ТОЛЬКО если это WETH/nativeETH/USDG -- иначе "
                                      "is_closed_cycle=true, is_priced_exit=false, не в total_profit_usd."),
                     "filters_applied": {
                         "canonical_factory_only": CANONICAL_FACTORY,
                         "min_distinct_traders_per_pool_day": MIN_DISTINCT_TRADERS_PER_POOL_DAY,
                         "max_dominant_share": MAX_DOMINANT_SHARE,
                         "note": "Эти три фильтра -- пред-фильтр качества пула (спам-фабрики, wash-trading), "
                                 "больше НЕ единственная защита от переоценки прибыли -- эту роль теперь "
                                 "играет net-flow-closed-cycle проверка (см. formula_note).",
                         "v4_note": "v4 не имеет Factory/Mint в этой форме -- фильтр канонической фабрики/LP только "
                                     "v3, фильтры диверсификации/доминирования на v4 используют 'sender' как "
                                     "приближение исполнителя.",
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
    qid_price = client.create_query("task5_eth_usdg_price_oneday_v4", sql_price)
    df_price = client.run_sql_cached("task5_eth_usdg_price_oneday_v4", sql_price, query_id=qid_price,
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

    print(f"\n=== A. Материализация детекции, ФОРМУЛА v4 (closed-cycle net-flow по всем токенам) ===")
    spent_before_a = credit_guard.load_state()[ns]["spent"]
    sql_detect = build_detect_sql(day_start, day_end).replace("{eth_price}", repr(eth_price_usdg))
    qid_detect = client.create_query("task5_arb_detect_oneday_v4", sql_detect)
    client.run_sql_cached("task5_arb_detect_oneday_v4", sql_detect, query_id=qid_detect,
                           estimated_credits=40.0, fetch_results=False)
    spent_after_a = credit_guard.load_state()[ns]["spent"]
    cost_a = spent_after_a - spent_before_a
    result["step_a_materialize_cost_credits"] = cost_a
    print(f"[task5_stage1] Шаг A (материализация, формула v4) стоимость: {cost_a:.4f}, query_id={qid_detect}")

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

    print(f"\n=== A1. Дневная сводка (воронка: кандидаты -> закрытый цикл -> оценённый -> прибыльный) ===")
    summary_rows = run_followup("task5_arb_summary_oneday_v4", SUMMARY_SQL, max_rows=5, max_cols=8, est=35.0)
    if summary_rows:
        result.update(summary_rows[0])
        s = summary_rows[0]
        print(f"[task5_stage1] воронка: кандидатов={s.get('n_candidates_total')}, "
              f"закрытый цикл={s.get('n_closed_cycle')}, "
              f"закрытый+оценён={s.get('n_closed_cycle_priced')}, "
              f"прибыльных={s.get('n_profitable_arb_txs')}")

    print(f"\n=== A2. Топ-500 исполнителей ===")
    executor_rows = run_followup("task5_arb_top_executors_oneday_v4", TOP_EXECUTORS_SQL, max_rows=500, max_cols=3, est=35.0)
    result["top_executors_by_profit_usd"] = executor_rows

    print(f"\n=== A3. Почасовое распределение ===")
    hourly_rows = run_followup("task5_arb_hourly_oneday_v4", HOURLY_SQL, max_rows=30, max_cols=3, est=35.0)
    result["hourly_distribution"] = hourly_rows

    total_cost = (result.get("step_b_cost_credits", 0.0) + result.get("step_a_materialize_cost_credits", 0.0)
                  + result.get("task5_arb_summary_oneday_v4_cost_credits", 0.0)
                  + result.get("task5_arb_top_executors_oneday_v4_cost_credits", 0.0)
                  + result.get("task5_arb_hourly_oneday_v4_cost_credits", 0.0))
    result["total_cost_this_run_credits"] = total_cost
    result["extrapolated_30day_credits"] = total_cost * 30 * 1.3
    print(f"\n[task5_stage1] РЕАЛЬНАЯ суммарная стоимость (формула v4, 1 день): {total_cost:.4f}")
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
                f"{SANITY_MAX_PLAUSIBLE_DAILY_PROFIT_USD:,.0f} -- результат ВСЁ ЕЩЁ контаминирован даже "
                "после фикса формулы -- есть ЕЩЁ один источник загрязнения, НЕ докладывать как факт, "
                "нужен дальнейший разбор, не 30-дневный прогон."
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
