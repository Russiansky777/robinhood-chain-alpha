#!/usr/bin/env python3
"""Одноразовая коррекция: `margin_call_price` во ВСЕХ уже собранных
строках data/p5_fee_accrual.jsonl считался НЕВЕРНОЙ формулой (через
`lighter_mark_price_now` вместо `avg_entry_price`) -- см. исправленный
вывод в p5_live_position_snapshot.py (задача LVR, 2026-09-04, найдено
численно: (collateral+unrealized_pnl)-cross_initial_margin_requirement
совпало с реальным available_balance с точностью до 6 знака, а версия
через P_now давала расхождение ~$14, это не шум).

`avg_entry_price` (2506.43), `real_hedge_size_eth` (0.0377) и `leverage`
(3.0003...) НЕ менялись ни разу (ребалансов не было) -- меняется только
`collateral_usd`, который восстанавливается из git history полного
отчёта на момент каждой точки (реальный, не переисполненный задним
числом).

Только чтение git history + перезапись data/p5_fee_accrual.jsonl.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ACCRUAL_LOG_PATH = Path("data/p5_fee_accrual.jsonl")

HISTORICAL_REVISIONS = {
    "2026-09-03T22:38:12Z": "8576798",
    "2026-09-03T22:54:10Z": "5fcb8f5",
    "2026-09-04T01:48:49Z": "c98742c",
    "2026-09-04T02:44:32Z": "ccea6fa",
    "2026-09-04T03:44:18Z": "205ab66",
    "2026-09-04T04:45:40Z": "7f0c53d",
    "2026-09-04T05:44:44Z": "d960084",
    "2026-09-04T06:45:23Z": "cc2c681",
    "2026-09-04T07:44:16Z": "1dcfe95",
    "2026-09-04T08:44:42Z": "2d30833",
    "2026-09-04T09:44:54Z": "f66567f",
    "2026-09-04T10:44:01Z": "ecc6dae",
    "2026-09-04T10:59:48Z": "f90a8a1",
}


def collateral_and_hedge_params_at(rev: str) -> dict:
    raw = subprocess.run(["git", "show", f"{rev}:data/p3_guard_cache/p5_live_snapshot_1000756_result.json"],
                          capture_output=True, text=True, check=True).stdout
    lh = json.loads(raw)["lighter_hedge_now"]
    return {"collateral_usd": lh["collateral_usd"], "avg_entry_price_usd": lh["avg_entry_price_usd"],
            "position_size_eth": lh["position_size_eth"], "leverage": lh["current_leverage"]["leverage"]}


def correct_margin_call_price(collateral_usd: float, avg_entry_price: float, size: float, leverage: float) -> float:
    return (collateral_usd + size * avg_entry_price) / (size * (1 + 1 / leverage))


def run() -> int:
    if not ACCRUAL_LOG_PATH.exists():
        print("[fix_margin_call] data/p5_fee_accrual.jsonl не найден.")
        return 1
    rows = [json.loads(line) for line in ACCRUAL_LOG_PATH.read_text().splitlines() if line.strip()]
    fixed = 0
    for row in rows:
        ts = row["timestamp_utc"]
        rev = HISTORICAL_REVISIONS.get(ts)
        if rev is None:
            print(f"[fix_margin_call] нет ревизии для {ts} -- пропущена.")
            continue
        params = collateral_and_hedge_params_at(rev)
        old_value = row.get("margin_call_price")
        new_value = correct_margin_call_price(params["collateral_usd"], params["avg_entry_price_usd"],
                                                params["position_size_eth"], params["leverage"])
        row["margin_call_price"] = new_value
        print(f"[fix_margin_call] {ts}: {old_value} -> {new_value} (diff {new_value - old_value if old_value else None})")
        fixed += 1
    ACCRUAL_LOG_PATH.write_text("\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows) + "\n")
    print(f"[fix_margin_call] исправлено строк: {fixed}/{len(rows)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
