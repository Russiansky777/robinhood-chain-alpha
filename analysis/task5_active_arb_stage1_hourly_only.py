#!/usr/bin/env python3
"""Задача 5, точечный дозапрос -- почасовое распределение (A3), которое
не поместилось в основной прогон Шага 1 из-за потолка пространства
(587.14 + оценка 35.0 = 622.14 > лимита 600.0 на тот момент -- не
overrun, а обычная нехватка headroom). Ссылается на УЖЕ реально
материализованный (57.05 кредита, подтверждено) query_id=8678582
('task5_arb_detect_oneday_v4') -- та же цена этого класса дозапроса
(~50-60 кредитов), что и у уже успешных summary/executors."""
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

OUT_PATH = Path("data/p3_guard_cache/task5_active_arb_stage1_hourly_only_result.json")
DETECT_QID = 8678582  # task5_arb_detect_oneday_v4, реально материализован за 57.05 кредита
DUST_THRESHOLD_USD = 1.0

HOURLY_SQL = f"""
select hour_utc, count(*) as n_profitable_txs, sum(profit_usd) as total_profit_usd
from query_{DETECT_QID}
where is_closed_cycle and is_priced_exit and profit_usd > {DUST_THRESHOLD_USD}
group by hour_utc
order by hour_utc
"""


def run() -> int:
    ensure_namespace("task5_active_arb_mozila", 700.0)
    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    qid = client.create_query("task5_arb_hourly_oneday_v4", HOURLY_SQL)
    df = client.run_sql_cached("task5_arb_hourly_oneday_v4", HOURLY_SQL, query_id=qid,
                                estimated_credits=35.0, expected_max_rows=30, expected_columns=3)
    rows = df.to_dict("records") if df is not None else []
    out["hourly_distribution"] = rows
    print(f"[hourly_only] найдено {len(rows)} часов")
    for r in rows:
        print(f"  {r}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    out["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[hourly_only] итого: {total_cost:.4f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
