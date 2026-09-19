#!/usr/bin/env python3
"""Владелец, 2026-09-19: "первый вход" -- ОДНО определение: баланс минта
у кошелька ДО транзакции = 0 (так же работает фильтр DBot "already
holds"). Реализация Фазы 2 (v1 и v2) вместо этого использовала
СТАТЕФУЛ множество "seen_mints", которое никогда не сбрасывается --
подтверждённый реальный баг: перезаход после ПОЛНОГО выхода (продал в
0, потом купил снова) неверно размечался как докупка (k=2,4,6,8...),
а не как новый первый вход. Проверено на лидере: из 105 RPC-эталонных
первых входов >=4.3 SOL 102 есть в наших данных, но 32 из них
размечены у нас как докупки -- эти 32/105 (~30%) и объясняют основной
разрыв с RPC-методом (13.6/сутки).

Владелец также верно указал возможную ОБРАТНУЮ ошибку: минт мог быть
куплен ДО начала окна истории (за пределами 7-суточного лукбэка) и всё
ещё лежать на балансе -- тогда наша текущая разметка first_entry=True
(k=1) тоже неверна. Поэтому pre_amt выгружается для ВСЕХ событий (и
"докупок", и "первых входов" по старой разметке), не только для
подозрительных.

Выгрузка -- по ОДНОМУ дню за запрос (партиция по block_time),
bounded IN-джойн по уже известным (tx_id, mint, trader) -- тот же
дешёвый паттерн, что дал 300/300 на эталонном шлюзе Источника B
(bounded equi-join по known tx_id, НЕ открытый скан по минту/времени,
как в Фазе 3 -- отсюда ожидание совсем другой, низкой стоимости).
Сначала ОДИН день -- честный замер кредита/день и прогноз на
остальные, до продолжения. Сырьё сохраняется в репозиторий СРАЗУ
после каждого дня, до обработки.

pre_amt отныне ВСЕГДА сохраняется в итоговых событиях -- без него
разметку first_entry/k проверить нельзя (прямое указание владельца)."""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
EVENTS_PATH = REPO_ROOT / "data" / "solana_phase2_events_v2.json"
OUT_PATH = REPO_ROOT / "data" / "solana_phase2_preamt_fetch_result.json"
RAW_DIR = REPO_ROOT / "data" / "solana_phase2_preamt_raw"

TX_TABLE = "solana.transactions"
MAX_TOTAL_COST_CREDITS = 150.0
FATAL_ERROR_MARKERS = ("RESOURCES_CAP_REACHED", "exceed your configured", "datapoint limit")


def day_str(epoch: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


def day_bounds(day: str) -> tuple[int, int]:
    lo = int(time.mktime(time.strptime(day, "%Y-%m-%d")))
    return lo, lo + 86400


def flatten_all_events(events_by_wallet: dict) -> list[dict]:
    flat = []
    for wallet, events in events_by_wallet.items():
        for e in events:
            flat.append({**e, "wallet": wallet})
    return flat


def build_preamt_sql(day_events: list[dict], lo: int, hi: int) -> str:
    values = ",".join(
        f"('{e['tx_id']}', '{e['mint']}', '{e['wallet']}')" for e in day_events
    )
    return (
        f"WITH pairs(tx_id, mint, trader) AS (VALUES {values}) "
        f"SELECT t.id AS tx_id, p.mint, p.trader, "
        "reduce(filter(t.pre_token_balances, x -> x[2] = p.mint AND x[3] = p.trader), "
        "CAST(0 AS DOUBLE), (s, x) -> s + try_cast(x[4] AS DOUBLE), s -> s) AS pre_amt "
        f"FROM {TX_TABLE} t JOIN pairs p ON t.id = p.tx_id "
        f"WHERE t.block_time BETWEEN from_unixtime({lo}) AND from_unixtime({hi})"
    )


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    d = json.loads(EVENTS_PATH.read_text())
    flat = flatten_all_events(d["events_by_wallet"])
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in flat:
        by_day[day_str(e["block_time_epoch"])].append(e)
    days = sorted(by_day.keys())

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery, "n_events_total": len(flat),
                     "days": days, "n_events_per_day": {k: len(v) for k, v in by_day.items()}}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_JANA_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    running_cost = 0.0
    day_meta = []
    preamt_by_tx: dict[str, float] = {}
    for i, day in enumerate(days):
        lo, hi = day_bounds(day)
        day_events = by_day[day]
        sql = build_preamt_sql(day_events, lo, hi)
        r = probe.run_sql_sync(f"phase2_preamt_{day}", sql, timeout_s=600)
        meta = {k: v for k, v in r.items() if k != "rows"}
        cost = (meta.get("status_meta") or {}).get("execution_cost_credits", 0) or 0
        running_cost += cost
        day_meta.append({"day": day, "n_events": len(day_events), "status": r.get("status"),
                          "n_rows": r.get("n_rows"), "cost_credits": cost})
        result["days_processed"] = day_meta
        result["total_cost_credits_so_far"] = running_cost
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(f"[preamt] день {day} ({i+1}/{len(days)}): status={r.get('status')} n_rows={r.get('n_rows')} "
              f"cost={cost} running_total={running_cost:.2f}", flush=True)

        if r.get("status") != "ok":
            err_text = json.dumps(meta, default=str)
            is_fatal = any(m in err_text for m in FATAL_ERROR_MARKERS)
            result["FINAL_ANSWER"] = f"ОСТАНОВЛЕНО на дне {day} ({'внешний блокер' if is_fatal else r.get('status')})."
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print("[preamt] " + result["FINAL_ANSWER"], flush=True)
            return

        raw_path = RAW_DIR / f"{day}.json.gz"
        with gzip.open(raw_path, "wt", encoding="utf-8") as fh:
            json.dump({"day": day, "sql": sql, "rows": r["rows"]}, fh, default=str)
        for row in r["rows"]:
            preamt_by_tx[row["tx_id"]] = row["pre_amt"]

        if i == 0:
            per_event = cost / len(day_events) if day_events else 0
            projected_total = per_event * len(flat)
            result["cost_per_event_from_first_day"] = per_event
            result["cost_per_day_first_day"] = cost
            result["projected_total_cost_credits"] = projected_total
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print(f"[preamt] прогноз после дня 1: {cost:.2f} кредита/день x {len(days)} дней "
                  f"(≈{per_event:.4f}/событие x {len(flat)} событий) ≈ {projected_total:.2f} кредитов", flush=True)
            if projected_total > MAX_TOTAL_COST_CREDITS:
                result["FINAL_ANSWER"] = (f"ОСТАНОВЛЕНО после дня 1 -- прогноз ({projected_total:.2f}) "
                                           f"превышает потолок {MAX_TOTAL_COST_CREDITS}. Нужно решение владельца.")
                OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                print("[preamt] " + result["FINAL_ANSWER"], flush=True)
                return

        if running_cost > MAX_TOTAL_COST_CREDITS:
            result["FINAL_ANSWER"] = f"ОСТАНОВЛЕНО -- потолок {MAX_TOTAL_COST_CREDITS} кредитов превышен ({running_cost:.2f})."
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print("[preamt] " + result["FINAL_ANSWER"], flush=True)
            return

    # --- все дни получены: переразметка first_entry/k по pre_amt ---
    result["n_preamt_fetched"] = len(preamt_by_tx)
    n_missing_preamt = sum(1 for e in flat if e["tx_id"] not in preamt_by_tx)
    result["n_missing_preamt"] = n_missing_preamt

    by_wallet_mint: dict[tuple, list] = defaultdict(list)
    for e in flat:
        by_wallet_mint[(e["wallet"], e["mint"])].append(e)

    n_first_entry_changed_to_true = 0
    n_first_entry_changed_to_false = 0
    final_by_wallet: dict[str, list] = defaultdict(list)
    EPS = 1e-9
    for (wallet, mint), evs in by_wallet_mint.items():
        evs.sort(key=lambda e: e["block_time_epoch"])
        current_k = 0
        for e in evs:
            pre_amt = preamt_by_tx.get(e["tx_id"])
            old_first = e["first_entry"]
            if pre_amt is None:
                new_e = {**e, "pre_amt": None, "first_entry": old_first, "k": e["k"], "preamt_missing": True}
                final_by_wallet[wallet].append(new_e)
                continue
            if pre_amt <= EPS:
                current_k = 1
            else:
                current_k += 1
            new_first = current_k == 1
            if new_first and not old_first:
                n_first_entry_changed_to_true += 1
            elif not new_first and old_first:
                n_first_entry_changed_to_false += 1
            final_by_wallet[wallet].append({**e, "pre_amt": pre_amt, "first_entry": new_first, "k": current_k,
                                             "preamt_missing": False})

    result["n_first_entry_changed_to_true"] = n_first_entry_changed_to_true
    result["n_first_entry_changed_to_false"] = n_first_entry_changed_to_false
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[preamt] переразметка: {n_first_entry_changed_to_true} стали первым входом, "
          f"{n_first_entry_changed_to_false} перестали быть первым входом, "
          f"{n_missing_preamt} без pre_amt (не тронуты)", flush=True)

    out_events_path = REPO_ROOT / "data" / "solana_phase2_events_v3.json"
    out_full = {**{k: v for k, v in d.items() if k != "events_by_wallet"},
                "preamt_fetch_meta": result, "events_by_wallet": dict(final_by_wallet)}
    out_events_path.write_text(json.dumps(out_full, ensure_ascii=False, indent=2, default=str))
    print(f"[preamt] записано {out_events_path}", flush=True)


if __name__ == "__main__":
    main()
