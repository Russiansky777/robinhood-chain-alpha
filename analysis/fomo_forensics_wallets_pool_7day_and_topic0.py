#!/usr/bin/env python3
"""Форензика fomo, продолжение -- 1-дневная проба дала 0 совпадений
кошельков (при 4 962 343 логах контракта целиком за тот же день,
extrapolated 30д ~32.6 кредита, порог владельца 60 не превышен).
Перед полной 30-дневной выгрузкой -- два дешёвых шага:

1. count(*) за 7 дней с фильтром по кошелькам -- сигнал, реальны ли
   вообще совпадения на более длинном окне (0 за 1 день мог быть
   просто тихим днём для шести конкретных адресов, контракт делится
   на очень много трейдеров судя по объёму).
2. Сэмпл структуры логов контракта (LIMIT 20, БЕЗ фильтра по кошелькам --
   дёшево при высокоселективном contract_address) -- какие topic0
   реально существуют, чтобы знать, ЧТО декодировать на следующем шаге
   (не знаем ABI контракта заранее, не гадаем)."""
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

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_wallets_pool_7day_and_topic0_result.json")
NAMESPACE_BUDGET = 350.0

POOL_CONTRACT_NO_0X = "8366a39cc670b4001a1121b8f6a443a643e40951"
WALLETS_LOWER_NO_0X = [
    "0a6ebed0155edb4b21d92ad02897a626cd90119e",
    "1bcc5f67cd17e13770f199fa03bc043b0cde1143",
    "cc0c581613dfd4ace7c8686668427236f8bd5cc5",
    "14aa2a71dbb5ef87b81f92205e2699aa4aa65794",
    "8f62a08537cede87d511aca6436274ab4ca080a3",
    "a0670863bd5cd0d60022bab2eed78e81e1a06bce",
]


def run() -> int:
    ensure_namespace("fomo_forensics_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[pool_7day] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()
    addrs_sql = ", ".join(f"'{a}'" for a in WALLETS_LOWER_NO_0X)

    sql_7d = f"""select count(*) as n_logs
from robinhood.logs
where lower(to_hex(contract_address)) = '{POOL_CONTRACT_NO_0X}'
    and block_time >= now() - interval '7' day
    and (
        substr(lower(to_hex(topic1)), 25, 40) in ({addrs_sql})
        or substr(lower(to_hex(topic2)), 25, 40) in ({addrs_sql})
        or substr(lower(to_hex(topic3)), 25, 40) in ({addrs_sql})
    )"""
    qid1 = client.create_query("fomo_pool_7day_wallets", sql_7d)
    df1 = client.run_sql_cached("fomo_pool_7day_wallets", sql_7d, query_id=qid1,
                                 estimated_credits=10.0, expected_max_rows=1, expected_columns=1)
    cost1 = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == "fomo_pool_7day_wallets"), None)
    n_7d = int(df1["n_logs"].iloc[0]) if df1 is not None and not df1.empty else None
    print(f"[pool_7day] РЕАЛЬНО: n_logs (6 адресов, 7 дней) = {n_7d}, стоимость = {cost1}")

    sql_topic0 = f"""select lower(to_hex(topic0)) as topic0, count(*) as n
from robinhood.logs
where lower(to_hex(contract_address)) = '{POOL_CONTRACT_NO_0X}'
    and block_time >= now() - interval '1' day
group by lower(to_hex(topic0))
order by n desc
limit 20"""
    qid2 = client.create_query("fomo_pool_topic0_distribution", sql_topic0)
    df2 = client.run_sql_cached("fomo_pool_topic0_distribution", sql_topic0, query_id=qid2,
                                 estimated_credits=10.0, expected_max_rows=20, expected_columns=2)
    cost2 = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == "fomo_pool_topic0_distribution"), None)
    topic0_dist = df2.to_dict("records") if df2 is not None else []
    print(f"[pool_7day] РЕАЛЬНОЕ распределение topic0 (1 день, весь контракт): {topic0_dist}")
    print(f"[pool_7day] стоимость topic0-разведки = {cost2}")

    total = (cost1 or 0) + (cost2 or 0)
    print(f"\n[pool_7day] суммарно потрачено на этом шаге: {total:.2f}")

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_logs_wallets_7day": n_7d,
        "cost_7day_query": cost1,
        "topic0_distribution_1day": topic0_dist,
        "cost_topic0_query": cost2,
        "total_cost": total,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[pool_7day] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
