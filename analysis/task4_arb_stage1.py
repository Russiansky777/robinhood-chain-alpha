#!/usr/bin/env python3
"""Задача 4 владельца (2026-09-10), медленный арбитраж, потолок 300
кредитов Mozila: "Оценка стоимости на одной цепи за один день до
полного." Это Шаг 1 -- НЕ полный пайплайн, а именно требуемая
предварительная оценка:

  (a) реальный полный список колонок dex.trades (нужен столбец адреса
      ТРЕЙДЕРА для концентрации "кто закрывает расхождения" -- не
      гадаем имя по памяти, см. task4_dex_trades_full_columns_probe.sql);
  (b) реальное число свопов и объём по каждому пулу на ОДНОЙ цепи
      (robinhood) за ОДИН день -- это даёт (i) реальную стоимость
      "одна цепь, один день" для экстраполяции на полный месяц x 3
      цепи, и (ii) реальных кандидатов в "хвостовые" пулы (TVL-фильтр
      $50k-$500k применяется отдельно через GT, бесплатно, после этого
      шага -- здесь только активность по Dune).

Полный пайплайн (цена после каждого свопа против референс-пула,
время жизни расхождений, концентрация адресов, доход хвоста) --
ОТДЕЛЬНЫЙ следующий шаг, запускается только после честной оценки
здесь, в рамках потолка 300 (владелец, 2026-09-10)."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task4_arb_slow_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")
sys.path.insert(0, str(Path(__file__).parent))

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402
from run_pipeline import read_sql  # noqa: E402

BUDGET = 600.0  # технический потолок namespace (переживает форс-250 gate), НЕ настоящий потолок задачи
TASK_CEILING_CREDITS = 300.0  # владелец, 2026-09-10: настоящий потолок "медленного арбитража"
PROBE_CHAIN = "robinhood"  # владелец: "оценка на одной цепи" -- берём Robinhood (уже инструментирована в этом репо)
ALL_CHAINS = ["robinhood", "base", "arbitrum"]
FULL_DAYS = 30  # "доход ... за месяц"
OUT_PATH = Path("data/p3_guard_cache/task4_arb_stage1_result.json")


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "task_ceiling_credits": TASK_CEILING_CREDITS, "probe_chain": PROBE_CHAIN,
                     "all_chains": ALL_CHAINS, "full_days": FULL_DAYS}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    ns = credit_guard.namespace()
    credit_guard.ensure_namespace(ns, BUDGET)
    client = DuneClient()

    print("=== Шаг A: реальный полный список колонок dex.trades (метаданные, ~0 кредитов) ===")
    spent_before_a = credit_guard.load_state().get(ns, {}).get("spent", 0.0)
    sql_cols = read_sql("task4/task4_dex_trades_full_columns_probe")
    qid_cols = client.create_query("task4_dex_trades_full_columns_probe", sql_cols)
    df_cols = client.run_sql_cached("task4_dex_trades_full_columns_probe", sql_cols, query_id=qid_cols,
                                     estimated_credits=1.0, expected_max_rows=80, expected_columns=3)
    spent_after_a = credit_guard.load_state()[ns]["spent"]
    cost_a = spent_after_a - spent_before_a
    result["step_a_cost_credits"] = cost_a
    if df_cols is None or not len(df_cols):
        result["blocker"] = "Шаг A: information_schema.columns вернул пусто -- dex.trades не найден? Разобраться, не гадать."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1
    all_columns = df_cols["column_name"].tolist()
    result["dex_trades_all_columns"] = all_columns
    print(f"[task4_stage1] реальные колонки dex.trades ({len(all_columns)}): {all_columns}")

    known = {"blockchain", "project", "version", "block_time", "block_date", "block_month", "block_number",
             "token_bought_address", "token_sold_address", "token_bought_amount", "token_sold_amount",
             "token_bought_symbol", "token_sold_symbol", "amount_usd", "project_contract_address",
             "tx_hash", "evt_index", "trace_address", "unique_trade_id", "token_pair"}
    address_like = [c for c in all_columns if "address" in c.lower() or c.lower() in
                    {"tx_from", "tx_to", "taker", "maker", "trader", "sender", "recipient"}]
    trader_candidates = [c for c in address_like if c not in
                         {"token_bought_address", "token_sold_address", "project_contract_address"}]
    result["trader_address_column_candidates"] = trader_candidates
    print(f"[task4_stage1] реальные кандидаты на столбец адреса трейдера: {trader_candidates}")
    print(f"[task4_stage1] колонки НЕ в нашем известном списке (для ручной проверки): {sorted(set(all_columns) - known)}")

    print(f"\n=== Шаг B: реальная активность по пулам, цепь={PROBE_CHAIN}, ОДИН день ===")
    now = datetime.now(timezone.utc)
    day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)  # вчера целиком UTC -- кэш-стабильно в течение суток
    day_start = day_end - timedelta(days=1)
    result["probe_day_start_utc"] = day_start.isoformat()
    result["probe_day_end_utc"] = day_end.isoformat()

    spent_before_b = credit_guard.load_state()[ns]["spent"]
    sql_pools = (read_sql("task4/task4_arb_pool_swap_counts")
                 .replace("{{chain}}", PROBE_CHAIN)
                 .replace("{{day_start}}", day_start.strftime("%Y-%m-%d %H:%M:%S"))
                 .replace("{{day_end}}", day_end.strftime("%Y-%m-%d %H:%M:%S")))
    qid_pools = client.create_query(f"task4_arb_pool_swap_counts_{PROBE_CHAIN}", sql_pools)
    naive_estimate = 5.0  # честная малая оценка -- один день, групповая агрегация, тот же класс, что task1/task2 daily-пробы
    df_pools = client.run_sql_cached("task4_arb_pool_swap_counts", sql_pools, query_id=qid_pools,
                                      estimated_credits=naive_estimate, expected_max_rows=20000, expected_columns=5)
    spent_after_b = credit_guard.load_state()[ns]["spent"]
    cost_b = spent_after_b - spent_before_b
    result["step_b_cost_credits"] = cost_b
    result["real_cost_this_run_credits"] = cost_a + cost_b
    result["namespace_spent_total"] = spent_after_b

    n_candidates = len(df_pools) if df_pools is not None else 0
    result["n_pool_candidates_one_day_ge10_swaps"] = n_candidates
    print(f"[task4_stage1] реальных пулов с >=10 свопов за день на {PROBE_CHAIN}: {n_candidates}")
    if df_pools is not None and len(df_pools):
        df_pools_sorted = df_pools.sort_values("n_swaps", ascending=False)
        result["top20_pools_by_swaps"] = df_pools_sorted.head(20).to_dict(orient="records")
        n_ge50 = int((df_pools["n_swaps"] >= 50).sum())
        result["n_pools_ge50_swaps_this_day"] = n_ge50
        print(f"[task4_stage1] из них >=50 свопов за этот день (владельческий порог 'хвоста', до TVL-фильтра): {n_ge50}")

    # Честная экстраполяция ТОЛЬКО этого discovery-слоя (Шаг B) -- НЕ
    # включает Шаг 2 (raw per-swap цены для tail+референс пулов), тот
    # оценивается отдельно ПОСЛЕ того, как реальный список кандидатов
    # пройдёт TVL-фильтр через GT (бесплатно, следующий шаг, не Dune).
    extrapolated_discovery_full = cost_b * FULL_DAYS * len(ALL_CHAINS) * 1.3
    result["extrapolated_discovery_layer_full_month_3chains_credits"] = extrapolated_discovery_full
    print(f"\n[task4_stage1] реальная стоимость Шага B (1 цепь, 1 день): {cost_b:.4f} кредита")
    print(f"[task4_stage1] экстраполяция ТОЛЬКО discovery-слоя (3 цепи x {FULL_DAYS} дней, +30% запас): "
          f"{extrapolated_discovery_full:.2f} кредита (потолок задачи: {TASK_CEILING_CREDITS})")
    result["note"] = ("Это оценка ТОЛЬКО discovery-слоя (подсчёт свопов по пулам). Доминирующая статья "
                       "расходов -- Шаг 2 (сырые цены по свопам для хвостовых+референс-пулов за месяц) -- "
                       "оценивается отдельно, после TVL-фильтра кандидатов через GT (бесплатно).")
    if extrapolated_discovery_full > TASK_CEILING_CREDITS:
        result["blocker"] = (f"Экстраполяция discovery-слоя ({extrapolated_discovery_full:.2f}) уже превышает "
                              f"потолок задачи ({TASK_CEILING_CREDITS}) САМА ПО СЕБЕ, до учёта Шага 2 -- "
                              "останавливаюсь, дальше сам не трачу, нужно решение владельца.")
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[task4_stage1] ОСТАНОВЛЕНО: {result['blocker']}")
        return 1

    result["runtime_s_total"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[task4_stage1] ГОТОВО. В пределах потолка на discovery-слое -- следующий шаг: TVL-фильтр "
          f"кандидатов через GT (бесплатно), затем оценка Шага 2 на самом активном хвостовом пуле.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
