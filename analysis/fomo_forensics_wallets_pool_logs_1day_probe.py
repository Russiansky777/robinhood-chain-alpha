#!/usr/bin/env python3
"""Форензика fomo, 2026-09-06 -- владелец: dex.trades НЕ источник (шаг 1
показал ~$53k за 90д по всем сетям и кошелькам суммарно -- не источник
PnL в миллионы). Реальные сделки идут через кастомный контракт
0x8366a39cc670b4001a1121b8f6a443a643e40951 на Robinhood Chain, которого
в dex.trades нет (подтверждено бесплатной ABI-пробой -- не Uniswap
V2/V3, свой интерфейс). Единственный путь -- сырые логи
`robinhood.logs`, фильтр ОДНОВРЕМЕННО по этому контракту (contract_address)
и по шести адресам кошельков (в topic1/topic2/topic3 -- ABI контракта
неизвестна, не знаем, в какой позиции индексируется адрес трейдера, не
гадаем -- проверяем все три позиции через OR).

Сначала -- ТОЛЬКО count(*) за ОДИН день, для оценки объёма перед тем,
как считать (и тратить) на полное 30-дневное окно. Владелец: "если
оценка полного окна больше 60 кредитов -- парковать до 14.09"."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "fomo_forensics_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_wallets_pool_logs_1day_probe_result.json")
NAMESPACE_BUDGET = 350.0
FULL_WINDOW_DAYS = 30
FULL_WINDOW_COST_PARK_THRESHOLD = 60.0  # владелец: порог, выше которого -- парковать до 14.09

POOL_CONTRACT_NO_0X = "8366a39cc670b4001a1121b8f6a443a643e40951"
WALLETS = {
    "unipcs": "0x0a6EBEd0155EDB4b21D92AD02897A626CD90119E",
    "ogle": "0x1Bcc5f67CD17e13770F199fA03bC043b0cde1143",
    "avast": "0xcc0C581613DFd4ACe7c8686668427236f8BD5cC5",
    "frogman": "0x14AA2A71dbb5eF87b81F92205E2699AA4aa65794",
    "DumbCrayonEater": "0x8f62a08537cede87d511aca6436274ab4ca080a3",
    "vee": "0xa0670863bd5cd0d60022bab2eed78e81e1a06bce",
}
WALLETS_LOWER_NO_0X = [a[2:].lower() for a in WALLETS.values()]


def run() -> int:
    ensure_namespace("fomo_forensics_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[pool_logs_1day] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()
    addrs_sql = ", ".join(f"'{a}'" for a in WALLETS_LOWER_NO_0X)

    # Оценка ДО запуска: единственная таблица (robinhood.logs, не dex.trades --
    # правило dex.trades+blockchain='robinhood' здесь не применяется, текста
    # 'dex.trades' в SQL нет), фильтр по ОДНОМУ конкретному contract_address
    # (высокоселективно) И по шести адресам в topics (доп. сужение) за 1 день --
    # структурно похоже на уже проверенные дешёвые point-запросы к robinhood.logs
    # в этой же сессии (topic0-разведка, ~5-17 кредитов за LIMIT 20 без
    # ограничения по contract_address вообще). Здесь запрос СИЛЬНЕЕ сужен
    # (один контракт + 1 день) -- оценка с запасом: 10.
    sql = f"""select count(*) as n_logs
from robinhood.logs
where lower(to_hex(contract_address)) = '{POOL_CONTRACT_NO_0X}'
    and block_time >= now() - interval '1' day
    and (
        substr(lower(to_hex(topic1)), 25, 40) in ({addrs_sql})
        or substr(lower(to_hex(topic2)), 25, 40) in ({addrs_sql})
        or substr(lower(to_hex(topic3)), 25, 40) in ({addrs_sql})
    )"""
    print("[pool_logs_1day] SQL (только count, 1 день, contract_address + 6 адресов в topics):")
    print(sql)
    print("[pool_logs_1day] обоснование оценки до запуска: одна таблица (не dex.trades), "
          "высокоселективный фильтр по ОДНОМУ contract_address + 1 день -- оценка 10.0")

    # Также считаем БЕЗ фильтра по адресам -- сколько логов контракт даёт за
    # день ВООБЩЕ (это позволит понять, большая ли доля -- наша, или наши
    # 6 адресов -- капля в море активности контракта).
    sql_all = f"""select count(*) as n_logs_all
from robinhood.logs
where lower(to_hex(contract_address)) = '{POOL_CONTRACT_NO_0X}'
    and block_time >= now() - interval '1' day"""

    qid = client.create_query("fomo_pool_logs_1day_wallets", sql)
    df = client.run_sql_cached("fomo_pool_logs_1day_wallets", sql, query_id=qid,
                                estimated_credits=10.0, expected_max_rows=1, expected_columns=1)
    cost1 = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == "fomo_pool_logs_1day_wallets"), None)
    n_logs_wallets = df["n_logs"].iloc[0] if df is not None and not df.empty else None
    print(f"[pool_logs_1day] РЕАЛЬНО: n_logs (наши 6 адресов, 1 день) = {n_logs_wallets}, стоимость = {cost1}")

    qid_all = client.create_query("fomo_pool_logs_1day_all", sql_all)
    df_all = client.run_sql_cached("fomo_pool_logs_1day_all", sql_all, query_id=qid_all,
                                    estimated_credits=10.0, expected_max_rows=1, expected_columns=1)
    cost2 = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == "fomo_pool_logs_1day_all"), None)
    n_logs_all = df_all["n_logs_all"].iloc[0] if df_all is not None and not df_all.empty else None
    print(f"[pool_logs_1day] РЕАЛЬНО: n_logs_all (контракт целиком, 1 день) = {n_logs_all}, стоимость = {cost2}")

    total_spent = (cost1 or 0) + (cost2 or 0)
    est_full_window_cost = total_spent * FULL_WINDOW_DAYS
    print(f"\n[pool_logs_1day] потрачено на разведку (2 запроса, 1 день): {total_spent:.2f}")
    print(f"[pool_logs_1day] ГРУБАЯ линейная экстраполяция на {FULL_WINDOW_DAYS} дней: "
          f"~{est_full_window_cost:.2f} кредитов (реальный масштаб может отличаться -- активность "
          f"не обязательно равномерна по дням, это оценка, не факт)")

    should_park = est_full_window_cost > FULL_WINDOW_COST_PARK_THRESHOLD
    print(f"[pool_logs_1day] порог владельца для парковки: {FULL_WINDOW_COST_PARK_THRESHOLD} -- "
          f"{'ПРЕВЫШЕН, парковать до 14.09' if should_park else 'не превышен, можно продолжать к шагу 2'}")

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pool_contract": "0x" + POOL_CONTRACT_NO_0X,
        "wallets": WALLETS,
        "n_logs_wallets_1day": int(n_logs_wallets) if n_logs_wallets is not None else None,
        "n_logs_all_1day": int(n_logs_all) if n_logs_all is not None else None,
        "cost_wallets_query": cost1,
        "cost_all_query": cost2,
        "total_probe_cost": total_spent,
        "est_full_30d_window_cost": est_full_window_cost,
        "park_threshold": FULL_WINDOW_COST_PARK_THRESHOLD,
        "should_park_until_2026_09_14": should_park,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[pool_logs_1day] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
