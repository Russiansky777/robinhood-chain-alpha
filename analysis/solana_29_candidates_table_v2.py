#!/usr/bin/env python3
"""Владелец, 2026-09-19: доработка таблицы 29 кандидатов.

1. Порог 2 SOL-эквивалент (реальный Copy Buy Range живых задач) рядом с
   4.3 (порог задания) -- ДВЕ колонки, не замена.
2. Колонка BATCH-1/BATCH-2/не в живых задачах + ремарка -- сопоставление
   по адресу с data/dbot_sieve_baseline.json.
3. Предыдущая таблица была написана вручную (сгруппированные строки в
   прозе) и содержала арифметическую ошибку при подсчёте -- реальные
   данные: 29 кошельков, 12 с нулевой ставкой, 17 с ненулевой (12+17=29,
   не "17+13=30", как получилось при ручной группировке). Эта версия
   строит таблицу программно из данных, ошибку такого рода исключает.
   Размеры отдельных первых входов (sol_equivalent на событие) СОХРАНЕНЫ
   в data/solana_29_candidates_flow_7d_sol_equiv.json -- пересчёт на
   пороге 2 SOL сделан по уже собранным данным, досканирование не
   потребовалось."""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FLOW_PATH = REPO_ROOT / "data" / "solana_29_candidates_flow_7d_sol_equiv.json"
BASELINE_PATH = REPO_ROOT / "data" / "dbot_sieve_baseline.json"
OUT_PATH = REPO_ROOT / "data" / "solana_29_candidates_table_v2.json"

THRESHOLDS = [2.0, 4.3]


def main() -> None:
    flow = json.loads(FLOW_PATH.read_text())
    baseline = json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.exists() else {}

    addr_to_batch: dict[str, dict] = {}
    for batch_name, b in (baseline.get("batches") or {}).items():
        for t in b.get("tracked_wallets", []):
            addr_to_batch[t["address"]] = {"batch": batch_name, "remark": t.get("remark")}

    rows = []
    for addr, v in flow["wallets"].items():
        if v is None:
            rows.append({"address": addr, "status": "not_done"})
            continue
        events = v.get("first_entry_events") or []
        days = v.get("days_covered") or 7
        row = {"address": addr, **addr_to_batch.get(addr, {"batch": "не в живых задачах", "remark": None})}
        for th in THRESHOLDS:
            sizes = [e["sol_equivalent"] for e in events if e.get("sol_equivalent") is not None]
            n_ge = sum(1 for s in sizes if s >= th)
            key = f"{th}".rstrip("0").rstrip(".")
            row[f"n_ge_{key}"] = n_ge
            row[f"per_day_ge_{key}"] = round(n_ge / days, 3) if days else None
        row["n_first_entries_total"] = len(events)
        row["n_sol_equivalent_computed"] = sum(1 for e in events if e.get("sol_equivalent") is not None)
        row["days_covered"] = days
        rows.append(row)

    n_total = len(rows)
    n_done = sum(1 for r in rows if r.get("status") != "not_done")
    n_zero_ge43 = sum(1 for r in rows if r.get("per_day_ge_4.3") == 0)
    n_nonzero_ge43 = sum(1 for r in rows if (r.get("per_day_ge_4.3") or 0) > 0)
    print(f"[table_v2] всего кошельков: {n_total}, готово: {n_done}, "
          f"ge4.3=0: {n_zero_ge43}, ge4.3>0: {n_nonzero_ge43} (сумма={n_zero_ge43 + n_nonzero_ge43})", flush=True)

    rows_done = [r for r in rows if r.get("status") != "not_done"]
    rows_done.sort(key=lambda r: r.get("per_day_ge_4.3") or 0, reverse=True)

    sum_ge2 = round(sum(r.get("per_day_ge_2") or 0 for r in rows_done), 3)
    sum_ge43 = round(sum(r.get("per_day_ge_4.3") or 0 for r in rows_done), 3)

    print(f"{'кошелёк':<46} {'батч':>18} {'ремарка':>16} {'ge2/сут':>8} {'ge4.3/сут':>10}")
    for r in rows_done:
        print(f"{r['address']:<46} {r.get('batch',''):>18} {str(r.get('remark') or ''):>16} "
              f"{r.get('per_day_ge_2', 0):>8.3f} {r.get('per_day_ge_4.3', 0):>10.3f}")
    print(f"\nSUM ge2/сут: {sum_ge2}   SUM ge4.3/сут: {sum_ge43}")

    out = {"thresholds": THRESHOLDS, "n_wallets_total": n_total, "n_wallets_done": n_done,
           "n_zero_ge_4.3": n_zero_ge43, "n_nonzero_ge_4.3": n_nonzero_ge43,
           "sum_per_day_ge_2": sum_ge2, "sum_per_day_ge_4.3": sum_ge43,
           "rows": rows_done}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\n[table_v2] записано в {OUT_PATH}")


if __name__ == "__main__":
    main()
