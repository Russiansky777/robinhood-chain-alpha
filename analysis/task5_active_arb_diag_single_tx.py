#!/usr/bin/env python3
"""Задача 5 -- точечная диагностика конкретной аномалии ($885k от ОДНОЙ
транзакции, адрес B8F305F27CCC...), ПОСЛЕ того как три фильтра владельца
почти не изменили итог ($178.8M -> $166.5M). Запрос идёт К УЖЕ
ОПЛАЧЕННОМУ материализованному query_id=8673700 (task5_arb_detect_
oneday_v3, 27.10 кредита уже потрачено) -- эта диагностика тянет
единицы строк из уже посчитанного результата, почти бесплатно."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task5_active_arb_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_active_arb_diag_single_tx_result.json")
DETECT_QID = 8673700

TARGET_EXECUTORS = [
    "B8F305F27CCC406373DE0082CC06CB1D065504EA",  # $885k от 1 транзакции
    "C021A7001E3A4E8BC2F4473AF7A20D857A03F59C",  # $963k от 4 транзакций
    "0A45DCD332083D412C047F7A20CE804A3FC32C25",  # топ-1, $1.43M от 990
]


def run() -> int:
    ensure_namespace("task5_active_arb_mozila", 700.0)
    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    addr_list = ", ".join(f"'{a}'" for a in TARGET_EXECUTORS)
    sql = f"""
select tx_hash, block_number, block_time, n_legs, n_pools, executor, profit_usd
from query_{DETECT_QID}
where executor in ({addr_list})
order by profit_usd desc
limit 200
"""
    qid = client.create_query("task5_diag_single_tx", sql)
    df = client.run_sql_cached("task5_diag_single_tx", sql, query_id=qid,
                                estimated_credits=1.0, expected_max_rows=200, expected_columns=7)
    rows = df.to_dict("records") if df is not None else []
    out["rows"] = rows
    print(f"[diag] найдено {len(rows)} строк для целевых адресов")
    for r in rows[:30]:
        print(f"  {r}")

    # n_legs distribution among the largest single transactions (independent of address)
    sql2 = f"""
select n_legs, n_pools, count(*) as n_txs, sum(profit_usd) as total_profit_usd,
       max(profit_usd) as max_profit_usd
from query_{DETECT_QID}
where profit_usd > 100000
group by n_legs, n_pools
order by total_profit_usd desc
limit 100
"""
    qid2 = client.create_query("task5_diag_biglegs_dist", sql2)
    df2 = client.run_sql_cached("task5_diag_biglegs_dist", sql2, query_id=qid2,
                                 estimated_credits=3.0, expected_max_rows=100, expected_columns=5)
    rows2 = df2.to_dict("records") if df2 is not None else []
    out["biglegs_distribution_over_100k"] = rows2
    print(f"\n[diag] распределение >$100k профита по (n_legs, n_pools):")
    for r in rows2[:30]:
        print(f"  {r}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    out["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[diag] итого: {total_cost:.4f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
