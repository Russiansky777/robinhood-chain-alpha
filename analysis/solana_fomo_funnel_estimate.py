#!/usr/bin/env python3
"""Владелец, 2026-09-19: дешёвый первый фильтр по всей базе Fomo (Task 5).

Честная оговорка ДО запуска: "число уникальных купленных минтов" и
"сумма в SOL" требуют JOIN dex_solana.trades x solana.transactions --
ТОТ ЖЕ паттерн, что уже дважды упал по timeout на планировщике Dune
(Task 2 прошлого сообщения, шаг4). Здесь НЕ повторяем этот JOIN --
считаем funnel ТОЛЬКО по solana.transactions (агрегат, GROUP BY час +
второй подписант, COUNT(*) как прокси "число сделок"), без сырых строк.
Число кошельков, распределение по частоте, порог >=3/сутки, стоимость --
да. Уникальные минты и сумма SOL -- честно не считаем в этом дешёвом
проходе (см. HONEST_LIMITATION в выводе), это отдельная, более дорогая
задача (JOIN, который пока не удаётся укротить)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_fomo_funnel_estimate.json"
SPONSOR = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
TX_TABLE = "solana.transactions"
FRESHNESS_LAG_S = 6 * 3600
WINDOW_S = 24 * 3600


def main() -> None:
    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery,
                     "HONEST_LIMITATION": (
                         "Уникальные купленные минты и сумма в SOL требуют JOIN dex_solana.trades x "
                         "solana.transactions -- тот же паттерн, что дважды падал по timeout. Здесь этот JOIN "
                         "НЕ повторяем -- funnel посчитан ТОЛЬКО по solana.transactions (COUNT(*) как прокси "
                         "числа сделок на кошелёк), без минтов/сумм. Это смета размера воронки, не паспорта."
                     )}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    now = int(time.time()) - FRESHNESS_LAG_S
    lo, hi = now - WINDOW_S, now
    sql = (
        f"SELECT date_trunc('hour', block_time) AS hour_bucket, "
        f"element_at(filter(signers, x -> x != '{SPONSOR}'), 1) AS trader, count(*) AS n_tx "
        f"FROM {TX_TABLE} WHERE signer = '{SPONSOR}' "
        f"AND block_time BETWEEN from_unixtime({lo}) AND from_unixtime({hi}) "
        f"GROUP BY 1, 2"
    )
    r = probe.run_sql_sync("fomo_funnel_24h_agg", sql, timeout_s=590)
    result["dune_step"] = {k: v for k, v in r.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[funnel] status={r.get('status')} n_rows={r.get('n_rows')}", flush=True)
    if r.get("status") != "ok":
        result["FINAL_ANSWER"] = f"Запрос не выполнился ({r.get('status')}) -- см. dune_step."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[funnel] " + result["FINAL_ANSWER"], flush=True)
        return

    per_trader = Counter()
    per_hour_n_traders = Counter()
    for row in r["rows"]:
        trader = row.get("trader")
        n_tx = row.get("n_tx") or 0
        if trader:
            per_trader[trader] += n_tx
            per_hour_n_traders[row.get("hour_bucket")] += 1

    n_wallets_total = len(per_trader)
    n_ge_3 = sum(1 for v in per_trader.values() if v >= 3)
    n_ge_5 = sum(1 for v in per_trader.values() if v >= 5)
    n_ge_10 = sum(1 for v in per_trader.values() if v >= 10)
    dist_buckets = Counter()
    for v in per_trader.values():
        if v == 1:
            dist_buckets["1"] += 1
        elif v == 2:
            dist_buckets["2"] += 1
        elif 3 <= v <= 4:
            dist_buckets["3-4"] += 1
        elif 5 <= v <= 9:
            dist_buckets["5-9"] += 1
        else:
            dist_buckets["10+"] += 1

    result["n_wallets_total_24h"] = n_wallets_total
    result["n_wallets_ge_3_tx_24h"] = n_ge_3
    result["n_wallets_ge_5_tx_24h"] = n_ge_5
    result["n_wallets_ge_10_tx_24h"] = n_ge_10
    result["distribution_tx_per_wallet"] = dict(dist_buckets)
    result["n_hours_covered"] = len(per_hour_n_traders)
    result["total_tx_24h"] = sum(per_trader.values())

    cost = ((result.get("dune_step") or {}).get("status_meta") or {}).get("execution_cost_credits")
    result["cost_credits_this_query"] = cost
    result["cost_extrapolation_note"] = (
        f"Эта агрегированная 24ч-выборка обошлась в {cost} кредитов -- заметно дешевле, чем сырые строки "
        "(та же 24ч-выборка сырыми строками ранее провалилась по объёму результата). Для регулярного мониторинга "
        "всей воронки Fomo -- ОДИН такой агрегирующий запрос в сутки, без сырых строк, в этом порядке стоимости."
    )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[funnel] кошельков всего={n_wallets_total}, >=3 сделок/сутки={n_ge_3}, >=5={n_ge_5}, >=10={n_ge_10}", flush=True)
    print(f"[funnel] распределение: {dict(dist_buckets)}", flush=True)
    print(f"[funnel] стоимость: {cost} кредитов", flush=True)


if __name__ == "__main__":
    main()
