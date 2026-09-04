#!/usr/bin/env python3
"""Владелец, 2026-09-04: разрешены траты на содержательные Dune-запросы,
лимит 300 кредитов на блок. Запрос 1 (первый по порядку, "решает больше
всего") -- органический ли объём пула ETH/USDG.

Таблица: uniswap_v3_robinhood.uniswapv3pool_evt_swap (найдена п.1
разведки -- 21 837 100 строк для нашего адреса, все 9 форков-конкурентов
дали 0, см. data/p3_guard_cache/dune_mozila_schema_recon_result.json).
token0=WETH (18 decimals), token1=USDG (6 decimals) -- см.
analysis/p5_live_precheck.py::WETH_DECIMALS/USDG_DECIMALS,
fees0_eth/fees1_usdg в p5_live_position_snapshot.py. Знак amount0/amount1
в Swap-событии -- изменение БАЛАНСА ПУЛА (положительное = пул получил
токен, трейдер его продал; отрицательное = пул отдал, трейдер купил).
volume_usd приближённо = ABS(amount1)/1e6 (нога USDG, стейбл ~$1).

Правило владельца (п.1): каждый запрос -- сначала LIMIT (структура/
синтаксис), потом содержательный. Сначала окно 7 дней (последний,
самый "горячий" период) -- измерить факт, экстраполировать на полный
период, полный прогон только если экстраполяция в лимите.

sender -- ВАЖНАЯ методологическая оговорка (см. отчёт в чат): в
Swap-событии Uniswap V3 sender -- это msg.sender ПУЛА, т.е. чаще всего
адрес РОУТЕРА, вызвавшего swap от имени пользователя, а НЕ конечный
трейдер. recipient -- получатель выходного токена, обычно ближе к
конечному пользователю (хотя тоже может быть контрактом-агрегатором).
Считаем ОБА отдельно (как просил владелец), но интерпретируем top-N по
sender осторожно -- концентрация там может отражать "все ходят через
один роутер", а не wash-trading.

Отдельный леджер: CREDIT_GUARD_NAMESPACE=funding_mozila_content,
CREDIT_GUARD_FILE=data/credits_spent_mozila.json (тот же файл, что
разведка -- НО отдельное пространство с лимитом 300, не смешивается с
50-кредитным разведочным).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "funding_mozila_content")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace, load_state, remaining_cycle_budget
from dune_client import DuneClient

OUT_PATH = Path("data/p3_guard_cache/dune_query1_volume_result.json")
POOL_ADDRESS = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
POOL_TABLE = "uniswap_v3_robinhood.uniswapv3pool_evt_swap"
CONTENT_BUDGET = 300.0
KILL_TOP3_SHARE = 0.50  # владелец: топ-3 адреса > 50% объёма -> инсентивный режим


def ensure_content_namespace() -> None:
    ensure_namespace("funding_mozila_content", CONTENT_BUDGET)


def q(client: DuneClient, name: str, sql: str, estimated_credits: float,
      expected_max_rows: int = 200, expected_columns: int = 10) -> dict:
    """Общая обёртка -- реальная ошибка Dune не роняет весь скрипт без
    следа, LIMIT/оценка -- на вызывающей стороне."""
    t0 = time.time()
    try:
        df = client.run_sql_cached(
            name=name, sql=sql, estimated_credits=estimated_credits,
            expected_max_rows=expected_max_rows, expected_columns=expected_columns,
        )
        rows = df.to_dict(orient="records") if df is not None else None
        print(f"[q1] {name}: {len(df) if df is not None else 0} строк, {time.time()-t0:.1f}с")
        return {"rows": rows, "n_rows": len(df) if df is not None else 0}
    except SystemExit as exc:
        print(f"[q1] {name} остановлен гвардом: {exc}")
        return {"stopped": True, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        print(f"[q1] {name} УПАЛ: {exc}")
        return {"failed": True, "reason": str(exc)[:2000]}


def spent_so_far() -> float:
    return load_state()["funding_mozila_content"]["spent"]


def run() -> int:
    ensure_content_namespace()
    client = DuneClient()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": {}}

    print("=== 0. Peek: реальные колонки/значения (SELECT *, LIMIT 5 -- не гадаем регистр колонок типа sqrtPriceX96) ===")
    peek_sql = f"""
    SELECT *
    FROM {POOL_TABLE}
    WHERE contract_address = {POOL_ADDRESS}
    ORDER BY evt_block_time DESC
    LIMIT 5
    """
    result["steps"]["peek"] = q(client, "q1_peek_columns", peek_sql, 3.0, expected_max_rows=5, expected_columns=20)
    before_content_spend = spent_so_far()
    print(f"[q1] стоимость peek: {before_content_spend:.3f} кредитов")

    # --- 7-дневное окно: содержательные метрики (каждая -- отдельный
    # execute, не UNION ALL -- избегаем пересчёта общей CTE в каждой
    # ветке, см. docstring/credit_guard.check_sql_sanity) ---
    window_filter = f"contract_address = {POOL_ADDRESS} AND evt_block_time >= NOW() - INTERVAL '7' DAY"

    print("\n=== 1. Объём по sender (топ-20, 7 дней) ===")
    by_sender_sql = f"""
    SELECT sender, COUNT(*) AS n_swaps, SUM(ABS(amount1))/1e6 AS volume_usd
    FROM {POOL_TABLE}
    WHERE {window_filter}
    GROUP BY sender
    ORDER BY volume_usd DESC
    LIMIT 20
    """
    result["steps"]["by_sender_7d"] = q(client, "q1_by_sender_7d", by_sender_sql, 5.0, expected_max_rows=20, expected_columns=3)

    print("\n=== 2. Объём по recipient (топ-20, 7 дней) ===")
    by_recipient_sql = f"""
    SELECT recipient, COUNT(*) AS n_swaps, SUM(ABS(amount1))/1e6 AS volume_usd
    FROM {POOL_TABLE}
    WHERE {window_filter}
    GROUP BY recipient
    ORDER BY volume_usd DESC
    LIMIT 20
    """
    result["steps"]["by_recipient_7d"] = q(client, "q1_by_recipient_7d", by_recipient_sql, 5.0, expected_max_rows=20, expected_columns=3)

    print("\n=== 3. Совокупный объём + число свопов за 7 дней (для доли топ-N) ===")
    total_sql = f"""
    SELECT COUNT(*) AS n_swaps, SUM(ABS(amount1))/1e6 AS total_volume_usd,
           COUNT(DISTINCT sender) AS n_distinct_sender, COUNT(DISTINCT recipient) AS n_distinct_recipient
    FROM {POOL_TABLE}
    WHERE {window_filter}
    """
    result["steps"]["totals_7d"] = q(client, "q1_totals_7d", total_sql, 3.0, expected_max_rows=1, expected_columns=4)

    print("\n=== 4. Активные адреса в сутки (recipient, 7 дней) ===")
    daily_active_sql = f"""
    SELECT DATE(evt_block_time) AS day, COUNT(DISTINCT recipient) AS n_active_recipients,
           COUNT(DISTINCT sender) AS n_active_senders, COUNT(*) AS n_swaps
    FROM {POOL_TABLE}
    WHERE {window_filter}
    GROUP BY DATE(evt_block_time)
    ORDER BY day
    """
    result["steps"]["daily_active_7d"] = q(client, "q1_daily_active_7d", daily_active_sql, 5.0, expected_max_rows=10, expected_columns=4)

    print("\n=== 5. Round-trip внутри часа (по recipient, определение: и покупка, и продажа WETH в одном часовом бакете) ===")
    roundtrip_sql = f"""
    WITH swaps AS (
      SELECT recipient AS addr,
             DATE_TRUNC('hour', evt_block_time) AS hour_bucket,
             CASE WHEN amount0 > 0 THEN 1 ELSE 0 END AS sold_weth,
             CASE WHEN amount0 < 0 THEN 1 ELSE 0 END AS bought_weth
      FROM {POOL_TABLE}
      WHERE {window_filter}
    ),
    addr_hour AS (
      SELECT addr, hour_bucket, MAX(sold_weth) AS any_sell, MAX(bought_weth) AS any_buy
      FROM swaps GROUP BY addr, hour_bucket
    ),
    addr_summary AS (
      SELECT addr, MAX(CASE WHEN any_sell = 1 AND any_buy = 1 THEN 1 ELSE 0 END) AS ever_roundtrip
      FROM addr_hour GROUP BY addr
    )
    SELECT COUNT(*) AS n_addresses, SUM(ever_roundtrip) AS n_roundtrip_addresses
    FROM addr_summary
    """
    result["steps"]["roundtrip_7d"] = q(client, "q1_roundtrip_7d", roundtrip_sql, 8.0, expected_max_rows=1, expected_columns=2)

    print("\n=== 6. Распределение размера свопа (перцентили, 7 дней) ===")
    dist_sql = f"""
    SELECT
      approx_percentile(ABS(amount1)/1e6, 0.5) AS p50_usd,
      approx_percentile(ABS(amount1)/1e6, 0.9) AS p90_usd,
      approx_percentile(ABS(amount1)/1e6, 0.99) AS p99_usd,
      approx_percentile(ABS(amount1)/1e6, 0.95) AS p95_usd,
      MAX(ABS(amount1)/1e6) AS max_usd,
      MIN(ABS(amount1)/1e6) AS min_usd
    FROM {POOL_TABLE}
    WHERE {window_filter}
    """
    result["steps"]["swap_size_dist_7d"] = q(client, "q1_swap_size_dist_7d", dist_sql, 5.0, expected_max_rows=1, expected_columns=6)

    spend_after_7d = spent_so_far()
    cost_7d_block = spend_after_7d - before_content_spend
    print(f"\n=== Стоимость блока 7-дневных запросов: {cost_7d_block:.3f} кредитов ===")

    print("\n=== 7. Недельный объём ПО ВСЕЙ истории (для даты скачка $5M/ч -> $32M/ч) -- отдельно, полный скан один раз ===")
    weekly_sql = f"""
    SELECT DATE_TRUNC('week', evt_block_time) AS week_start,
           COUNT(*) AS n_swaps, SUM(ABS(amount1))/1e6 AS volume_usd,
           SUM(ABS(amount1))/1e6 / (7*24) AS avg_hourly_volume_usd
    FROM {POOL_TABLE}
    WHERE contract_address = {POOL_ADDRESS}
    GROUP BY DATE_TRUNC('week', evt_block_time)
    ORDER BY week_start
    """
    result["steps"]["weekly_volume_full_history"] = q(
        client, "q1_weekly_volume_full_history", weekly_sql, 15.0, expected_max_rows=20, expected_columns=4,
    )

    # --- Дополнение владельца, 2026-09-04 (дёшево): Mint-события DOMINANT_ADDRESS
    # в нашем пуле + сколько ликвидности держит; топ-5 по объёму по неделям ---
    DOMINANT_ADDRESS = "0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc"

    print(f"\n=== 8. Peek Mint-таблицы (SELECT *, LIMIT 3 -- структура/регистр колонок) ===")
    mint_peek_sql = f"""
    SELECT * FROM {POOL_TABLE.rsplit('_evt_swap', 1)[0]}_evt_mint
    WHERE contract_address = {POOL_ADDRESS}
    LIMIT 3
    """
    result["steps"]["mint_peek"] = q(client, "q1_mint_peek", mint_peek_sql, 3.0, expected_max_rows=3, expected_columns=20)

    print(f"\n=== 9. Mint-события {DOMINANT_ADDRESS} в нашем пуле (owner ИЛИ sender) ===")
    mint_addr_sql = f"""
    SELECT COUNT(*) AS n_mints, SUM(amount) AS total_liquidity_minted_raw,
           SUM(amount0)/1e18 AS total_weth_deposited, SUM(amount1)/1e6 AS total_usdg_deposited
    FROM {POOL_TABLE.rsplit('_evt_swap', 1)[0]}_evt_mint
    WHERE contract_address = {POOL_ADDRESS}
      AND (owner = {DOMINANT_ADDRESS} OR sender = {DOMINANT_ADDRESS})
    """
    result["steps"]["mint_dominant_address"] = q(
        client, "q1_mint_dominant_address", mint_addr_sql, 5.0, expected_max_rows=1, expected_columns=4,
    )

    print("\n=== 10. Топ-5 адресов по объёму (sender) по неделям, полная история ===")
    top5_weekly_sql = f"""
    WITH weekly AS (
      SELECT DATE_TRUNC('week', evt_block_time) AS week_start, sender,
             SUM(ABS(amount1))/1e6 AS volume_usd
      FROM {POOL_TABLE}
      WHERE contract_address = {POOL_ADDRESS}
      GROUP BY DATE_TRUNC('week', evt_block_time), sender
    ),
    ranked AS (
      SELECT week_start, sender, volume_usd,
             ROW_NUMBER() OVER (PARTITION BY week_start ORDER BY volume_usd DESC) AS rnk
      FROM weekly
    )
    SELECT week_start, sender, volume_usd, rnk
    FROM ranked
    WHERE rnk <= 5
    ORDER BY week_start, rnk
    """
    result["steps"]["top5_by_week"] = q(
        client, "q1_top5_by_week", top5_weekly_sql, 15.0, expected_max_rows=100, expected_columns=4,
    )

    total_spent = spent_so_far()
    print(f"\n=== Итого потрачено в блоке содержательных запросов (funding_mozila_content): "
          f"{total_spent:.3f} из {CONTENT_BUDGET} ===")

    # --- Kill-проверка (владелец, предрегистрирован заранее) ---
    by_sender_rows = (result["steps"]["by_sender_7d"] or {}).get("rows") or []
    totals_rows = (result["steps"]["totals_7d"] or {}).get("rows") or []
    kill_check = {"note": "не удалось посчитать -- см. rows выше"}
    if by_sender_rows and totals_rows:
        total_vol = totals_rows[0].get("total_volume_usd")
        if total_vol:
            top3_vol = sum(r["volume_usd"] for r in sorted(by_sender_rows, key=lambda r: -r["volume_usd"])[:3])
            top3_share = top3_vol / total_vol
            kill_check = {
                "top3_sender_volume_usd": top3_vol, "total_volume_usd": total_vol,
                "top3_share": top3_share, "threshold": KILL_TOP3_SHARE,
                "kill_triggered": top3_share > KILL_TOP3_SHARE,
                "caveat": "top3 по SENDER -- методологически может отражать 'все ходят через один роутер', "
                          "не обязательно wash-trading. См. отдельно top3 по RECIPIENT для контраста.",
            }
    result["kill_check_top3_sender_share"] = kill_check
    print(f"\n[q1] KILL-проверка (топ-3 sender): {kill_check}")

    result["spent_this_run"] = total_spent
    result["remaining_content_budget"] = CONTENT_BUDGET - total_spent
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[q1] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
