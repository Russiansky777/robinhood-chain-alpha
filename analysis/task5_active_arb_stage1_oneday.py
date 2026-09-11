#!/usr/bin/env python3
"""Задача 5 владельца (2026-09-10), Шаг 1 -- ОБЯЗАТЕЛЬНАЯ оценка
стоимости на ОДНОМ дне перед полным 30-дневным прогоном. Считает
атомарный внутриблочный арбитраж (транзакции с >=2 свопами РАЗНЫХ
пулов) на Robinhood chain, используя ТОЛЬКО декодированные схемы
(uniswap_v3_robinhood + uniswap_v4_robinhood), без dex.trades (владелец
явно запретил -- узкие предикаты напрямую по декодированным таблицам).

Реальные схемы, подтверждённые Шагом 0/0b (не по памяти):
  - `uniswap_v3_robinhood.uniswapv3pool_evt_swap`: contract_address
    (per-pool!), evt_tx_hash, evt_tx_from (реальный EOA-исполнитель,
    ГОТОВ на самой таблице, JOIN на transactions не нужен для v3-ноги),
    evt_block_number/time/date, amount0/amount1 (int256, знаковые, с
    точки зрения ПУЛА -- знак трейдера = минус).
  - `uniswap_v3_robinhood.uniswapv3factory_evt_poolcreated`: pool,
    token0, token1, fee -- РЕАЛЬНО 430 001 строка (Шаг 0b) -- это спам-
    фабрики на дешёвом L2 (владелец, PROJECT_STATE: канонический адрес
    Factory `0x1f7d7550b1b028f7571e69a784071f0205fd2efa`, НЕ CREATE2
    Uniswap Labs), НЕ 430k реальных рынков. НЕ вытягиваем в Python
    (слишком много) -- JOIN делается на стороне Dune по конкретным
    адресам пулов, встретившимся в свопах этого дня, что естественно
    сужает выборку без явного WHERE на фабрику.
  - `uniswap_v4_robinhood.swaps`: tx_hash, block_number/time/date,
    token_bought_address/token_sold_address (уже разрешены, без
    двусмысленности знака), token_bought_amount_raw/token_sold_amount_raw,
    hooks, fee. `project_contract_address` -- singleton PoolManager
    (НЕ per-pool) -- ключ пула строится как
    (least/greatest(token_bought,token_sold), hooks, fee). НЕТ прямого
    tx_from на этой таблице -- исполнитель для v4-ветки берётся отдельным
    JOIN на `robinhood.transactions` (только для уже отфильтрованного
    малого набора tx_hash с >=2 пулами, не для всех свопов).
  - `robinhood.transactions`: hash, from, to, block_date -- реальные
    колонки (Шаг 0).
  - WETH: 0x0bd7d308f8e1639fab988df18a8011f41eacad73 (18 decimals).
    USDG: 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168 (6 decimals) --
    оба адреса и decimals реально использованы в живой транзакции
    (analysis/asset_consolidation_dryrun.py), не выдуманы.
  - Формула цены из sqrtPriceX96 (уже установлена в этом репозитории,
    analysis/pool_screener_concentration.py): price_raw =
    (sqrtPriceX96/2**96)**2, затем *10**(dec0-dec1) для цены token0 в
    token1 human-readable единицах.

Профиль расходов Шага 1 -- ЯВНО раздельные пункты, каждый со своей
реальной стоимостью (не одна общая сумма):
  A. Основной запрос -- детекция атомарных tx + прибыль (WETH/USDG нетто-
     поток) + исполнитель + время. Один день.
  B. Реальная цена ETH/USDG на этот день (для перевода WETH-прибыли в $)
     -- маленький отдельный запрос на известный пул WETH/USDG
     (0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca).
  Механизм (п.5 задания, "доля прибыльных tx в течение 1-2 блоков после
  свопа >$10k") -- ОТДЕЛЬНО оценивается ПОСЛЕ того, как известна
  реальная стоимость A+B (может оказаться доминирующей статьёй, как
  Шаг 2 в Задаче 4 -- не считаем её здесь вслепую)."""
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

# 2026-09-10, владелец: "предрегистрация... линия открывается только
# если суммарная прибыль адресов на позициях 2-10 превышает $500/день"
PREREG_THRESHOLD_USD_PER_DAY = 500.0


def build_main_sql(day_start: str, day_end: str) -> str:
    return f"""
with v3_legs as (
    select
        s.evt_tx_hash as tx_hash,
        s.evt_block_number as block_number,
        s.evt_block_time as block_time,
        to_hex(s.contract_address) as pool_key,
        to_hex(s.evt_tx_from) as executor,
        case
            when p.token0 = from_hex('{WETH}') then cast(-s.amount0 as double) / 1e{WETH_DECIMALS}
            when p.token1 = from_hex('{WETH}') then cast(-s.amount1 as double) / 1e{WETH_DECIMALS}
            else 0.0
        end as weth_delta,
        case
            when p.token0 = from_hex('{USDG}') then cast(-s.amount0 as double) / 1e{USDG_DECIMALS}
            when p.token1 = from_hex('{USDG}') then cast(-s.amount1 as double) / 1e{USDG_DECIMALS}
            else 0.0
        end as usdg_delta
    from uniswap_v3_robinhood.uniswapv3pool_evt_swap s
    left join uniswap_v3_robinhood.uniswapv3factory_evt_poolcreated p
        on p.pool = s.contract_address
    where s.evt_block_time >= timestamp '{day_start}' and s.evt_block_time < timestamp '{day_end}'
),
v4_legs as (
    select
        w.tx_hash as tx_hash,
        w.block_number as block_number,
        w.block_time as block_time,
        concat(to_hex(least(w.token_bought_address, w.token_sold_address)), '-',
               to_hex(greatest(w.token_bought_address, w.token_sold_address)), '-',
               cast(w.fee as varchar), '-', to_hex(w.hooks)) as pool_key,
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
    to_hex(a.tx_hash) as tx_hash, a.block_number, a.block_time, a.n_legs, a.n_pools,
    a.profit_weth, a.profit_usdg,
    coalesce(a.v3_executor, to_hex(t."from")) as executor,
    hour(a.block_time) as hour_utc
from arb_agg a
left join robinhood.transactions t
    on t.hash = a.tx_hash
    and t.block_date >= date '{day_start[:10]}' and t.block_date <= date '{day_end[:10]}'
order by a.block_time
limit 20000
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
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    now = datetime.now(timezone.utc)
    day_end_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)  # вчера целиком UTC -- кэш-стабильно
    day_start_dt = day_end_dt - timedelta(days=1)
    day_start = day_start_dt.strftime("%Y-%m-%d %H:%M:%S")
    day_end = day_end_dt.strftime("%Y-%m-%d %H:%M:%S")
    result["probe_day_start_utc"] = day_start
    result["probe_day_end_utc"] = day_end

    print(f"\n=== A. Основной запрос: детекция атомарных tx (>=2 пулов), 1 день ===")
    spent_before_a = credit_guard.load_state()[ns]["spent"]
    sql_main = build_main_sql(day_start, day_end)
    qid_main = client.create_query("task5_arb_detect_oneday", sql_main)
    df_main = client.run_sql_cached("task5_arb_detect_oneday", sql_main, query_id=qid_main,
                                     estimated_credits=15.0, expected_max_rows=20000, expected_columns=9)
    spent_after_a = credit_guard.load_state()[ns]["spent"]
    cost_a = spent_after_a - spent_before_a
    result["step_a_cost_credits"] = cost_a
    print(f"[task5_stage1] Шаг A стоимость: {cost_a:.4f}, строк: {len(df_main) if df_main is not None else 0}")

    print(f"\n=== B. Реальная медианная цена ETH/USDG за этот день (для перевода в $) ===")
    spent_before_b = credit_guard.load_state()[ns]["spent"]
    sql_price = build_price_sql(day_start, day_end)
    qid_price = client.create_query("task5_eth_usdg_price_oneday", sql_price)
    df_price = client.run_sql_cached("task5_eth_usdg_price_oneday", sql_price, query_id=qid_price,
                                      estimated_credits=3.0, expected_max_rows=5, expected_columns=1)
    spent_after_b = credit_guard.load_state()[ns]["spent"]
    cost_b = spent_after_b - spent_before_b
    result["step_b_cost_credits"] = cost_b
    eth_price_usdg = None
    if df_price is not None and len(df_price) and df_price["median_weth_price_in_usdg"].iloc[0] is not None:
        eth_price_usdg = float(df_price["median_weth_price_in_usdg"].iloc[0])
    result["eth_price_usdg_median_this_day"] = eth_price_usdg
    print(f"[task5_stage1] Шаг B стоимость: {cost_b:.4f}, медианная цена ETH/USDG: {eth_price_usdg}")

    total_cost = cost_a + cost_b
    result["total_cost_this_run_credits"] = total_cost
    result["extrapolated_30day_credits"] = total_cost * 30 * 1.3  # +30% запас, тот же множитель, что в Задаче 4
    print(f"\n[task5_stage1] РЕАЛЬНАЯ стоимость Шагов A+B за 1 день: {total_cost:.4f}")
    print(f"[task5_stage1] экстраполяция на 30 дней (+30% запас): {result['extrapolated_30day_credits']:.2f}")

    if df_main is None or not len(df_main):
        result["n_arb_txs"] = 0
        result["note"] = "0 атомарных tx за этот день -- реальный результат, не ошибка (см. df_main)."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[task5_stage1] {result['note']}")
        return 0

    df = df_main.copy()
    # Прибыль в $: USDG ~= $1 (стейблкоин), WETH -- через реальную медианную цену этого дня.
    df["profit_usd_from_weth"] = df["profit_weth"].astype(float) * (eth_price_usdg or 0.0)
    df["profit_usd_from_usdg"] = df["profit_usdg"].astype(float)
    df["profit_usd"] = df["profit_usd_from_weth"] + df["profit_usd_from_usdg"]

    result["n_arb_txs"] = len(df)
    result["n_profitable_arb_txs"] = int((df["profit_usd"] > 0).sum())
    result["n_unprofitable_or_zero_arb_txs"] = int((df["profit_usd"] <= 0).sum())

    profitable = df[df["profit_usd"] > 0]
    if len(profitable):
        result["median_profit_usd_per_profitable_tx"] = float(profitable["profit_usd"].median())
        result["p90_profit_usd_per_profitable_tx"] = float(profitable["profit_usd"].quantile(0.9))
        result["total_profit_usd_this_day"] = float(profitable["profit_usd"].sum())

    # Распределение по адресам-исполнителям -- реальная позиция 1..N по прибыли
    by_executor = profitable.groupby("executor")["profit_usd"].sum().sort_values(ascending=False)
    result["n_distinct_profitable_executors"] = len(by_executor)
    result["top20_executors_by_profit_usd"] = by_executor.head(20).to_dict()
    if len(by_executor) >= 2:
        positions_2_10 = by_executor.iloc[1:10]
        result["sum_profit_usd_positions_2_10_this_day"] = float(positions_2_10.sum())
    else:
        result["sum_profit_usd_positions_2_10_this_day"] = 0.0

    # Частота по времени суток
    result["arb_txs_by_hour_utc"] = df.groupby("hour_utc").size().to_dict()

    result["preregistration_threshold_usd_per_day"] = PREREG_THRESHOLD_USD_PER_DAY
    result["preregistration_note"] = ("Это ОДИН день -- предрегистрация владельца требует суточной суммы, "
                                       "но один день ещё не даёт статистически надёжной оценки -- честная "
                                       "проверка порога только после полного 30-дневного прогона (медиана по дням).")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[task5_stage1] реальных атомарных tx за день: {result['n_arb_txs']}, "
          f"из них прибыльных: {result['n_profitable_arb_txs']}")
    print(f"[task5_stage1] сумма прибыли позиций 2-10 за ЭТОТ день: "
          f"${result['sum_profit_usd_positions_2_10_this_day']:.2f} (порог владельца: ${PREREG_THRESHOLD_USD_PER_DAY}/день, "
          "предварительно, не окончательный вывод по одному дню)")
    print(f"[task5_stage1] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
