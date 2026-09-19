#!/usr/bin/env python3
"""Владелец, 2026-09-19: исправление формулы депозита (Task 5).

СТАРАЯ (неверная) формула: депозит = сигналов/сутки x 0.1 SOL -- неверна,
т.к. при удержании ~35с капитал возвращается быстро и НЕ завязан 1:1 на
каждый сигнал за сутки.

ПРАВИЛЬНАЯ модель:
  требуемый капитал = ПИКОВОЕ число ОДНОВРЕМЕННО открытых позиций x 0.1 SOL
  дневная стоимость  = сигналов/сутки x фиксированная стоимость одного round-trip

Пиковая одновременность оценивается ЭМПИРИЧЕСКИ: берём все first-entry
события всех 29 кошельков (пул, из уже собранных данных
solana_29_candidates_flow_7d_sol_equiv.json, timestamps реальные, не
выдуманные), фильтруем по порогу (2 или 4.3 SOL-эквивалент), считаем
интервал удержания HOLD_SECONDS для каждого сигнала как отдельную
позицию и sweep-line считаем максимум одновременно открытых.

Стоимость round-trip -- из РЕАЛЬНОЙ ведомости сделок
(data/solana_dbot_realized_ledger.json): среднее (tip_buy+tip_sell+
network_fee_buy+network_fee_sell) по всем закрытым сделкам с известными
чаевыми/комиссией."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

REPO_ROOT = Path(__file__).resolve().parent.parent
FLOW_PATH = REPO_ROOT / "data" / "solana_29_candidates_flow_7d_sol_equiv.json"
LEDGER_PATH = REPO_ROOT / "data" / "solana_dbot_realized_ledger.json"
TABLE_V2_PATH = REPO_ROOT / "data" / "solana_29_candidates_table_v2.json"
OUT_PATH = REPO_ROOT / "data" / "solana_deposit_sizing_v2.json"

THRESHOLDS = [2.0, 4.3]
HOLD_SECONDS = 35  # эмпирически: 28.8с (pnlOrderExpireDelta) + ~5-7с лаг входа/выхода, см. пилот n=6 (33-36с)
POSITION_SIZE_SOL = 0.1


def peak_concurrency(events: list[tuple[int, float]], threshold: float, hold_s: int) -> dict:
    """events: [(block_time, sol_equivalent), ...]. Считает пиковое число
    одновременно 'открытых' (в течение hold_s секунд после входа) позиций
    среди событий с sol_equivalent >= threshold -- честный sweep-line, не
    оценка на глаз."""
    filtered = sorted(t for t, s in events if s is not None and s >= threshold)
    if not filtered:
        return {"n_events": 0, "peak_concurrency": 0, "peak_at_time": None}
    changes = []
    for t in filtered:
        changes.append((t, 1))
        changes.append((t + hold_s, -1))
    changes.sort(key=lambda c: (c[0], c[1]))  # закрытие раньше открытия при равенстве времени
    cur = peak = 0
    peak_t = None
    for t, delta in changes:
        cur += delta
        if cur > peak:
            peak = cur
            peak_t = t
    return {"n_events": len(filtered), "peak_concurrency": peak, "peak_at_time": peak_t}


def main() -> None:
    flow = json.loads(FLOW_PATH.read_text())
    all_events: list[tuple[int, float]] = []
    for addr, v in flow["wallets"].items():
        if not v:
            continue
        for e in (v.get("first_entry_events") or []):
            if e.get("block_time") is not None:
                all_events.append((e["block_time"], e.get("sol_equivalent")))

    result: dict = {"n_wallets_pooled": sum(1 for v in flow["wallets"].values() if v),
                     "n_total_first_entry_events_all_sizes": len(all_events),
                     "hold_seconds_assumption": HOLD_SECONDS,
                     "position_size_sol": POSITION_SIZE_SOL, "thresholds": {}}

    signals_per_day = {}
    if TABLE_V2_PATH.exists():
        tv2 = json.loads(TABLE_V2_PATH.read_text())
        signals_per_day["2"] = tv2.get("sum_per_day_ge_2")
        signals_per_day["4.3"] = tv2.get("sum_per_day_ge_4.3")

    round_trip_cost_sol = None
    n_cost_samples = 0
    if LEDGER_PATH.exists():
        ledger = json.loads(LEDGER_PATH.read_text())
        costs = []
        for t in ledger.get("trades", []):
            if t.get("status") == "ok" and all(k in t for k in
                    ("tip_buy_sol", "tip_sell_sol", "network_fee_buy_sol", "network_fee_sell_sol")):
                costs.append(t["tip_buy_sol"] + t["tip_sell_sol"] + t["network_fee_buy_sol"] + t["network_fee_sell_sol"])
        if costs:
            round_trip_cost_sol = mean(costs)
            n_cost_samples = len(costs)
    result["round_trip_cost_sol"] = round_trip_cost_sol
    result["round_trip_cost_source"] = f"среднее по {n_cost_samples} реальным закрытым сделкам (пилот+BATCH-1) из solana_dbot_realized_ledger.json" if n_cost_samples else "нет данных -- ведомость сделок пуста/недоступна"

    for th in THRESHOLDS:
        key = f"{th}".rstrip("0").rstrip(".")
        pc = peak_concurrency(all_events, th, HOLD_SECONDS)
        spd = signals_per_day.get(key)
        required_capital_sol = pc["peak_concurrency"] * POSITION_SIZE_SOL
        daily_cost_sol = spd * round_trip_cost_sol if (spd is not None and round_trip_cost_sol is not None) else None
        result["thresholds"][key] = {
            **pc,
            "signals_per_day_sum_29_wallets": spd,
            "required_capital_sol_peak_concurrency_x_0.1": round(required_capital_sol, 3),
            "daily_cost_sol_signals_x_round_trip_cost": round(daily_cost_sol, 4) if daily_cost_sol is not None else None,
            "OLD_WRONG_FORMULA_signals_x_0.1_for_comparison": round(spd * POSITION_SIZE_SOL, 3) if spd is not None else None,
        }
        print(f"[deposit_v2] порог {th}: пик.одновременность={pc['peak_concurrency']} "
              f"(из {pc['n_events']} событий >= порога) -> капитал={required_capital_sol:.3f} SOL, "
              f"сигналов/сутки={spd}, дневная стоимость={daily_cost_sol}", flush=True)

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[deposit_v2] записано в {OUT_PATH}")


if __name__ == "__main__":
    main()
