#!/usr/bin/env python3
"""Владелец, 2026-09-19: доля пойманных сигналов пилота (Task 4).

С момента старта пилота (реальное пополнение боевого кошелька,
data/solana_dbot_realized_ledger.json -> balance_reconciliation.pilot.
funding_events) по сейчас: сколько первых входов лидера >=4.3 SOL-
эквивалента было на цепи, и сколько из них пилот реально исполнил
(сравнение по mint+времени с уже известными 6 реальными сделками
пилота). Классификация первых входов лидера -- ОБЯЗАТЕЛЬНО через
classify() из основного конвейера (solana_buyer200_select_extend.py),
не заново -- установленное в этой сессии правило против airdrop-бага.

Причины пропуска -- по логу DBot. УЖЕ ПРОВЕРЕНО в этой сессии
(data/solana_dbot_failure_pointcheck_result.json): follow_orders/records,
copy_logs, task_logs, error_logs, follow_records, notifications -- все
401 (недоступны с этим ключом), swap_orders?state=fail -- 200, но 0
строк. Значит API DBot НЕ отдаёт нам, но не выдумываем причины --
честно перечисляем пропущенные сигналы (минт+время+подпись лидера) для
проверки владельцем на дашборде DBot напрямую."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_buyer200_select_extend import classify  # noqa: E402
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_pilot_signal_capture_rate.json"
LEDGER_PATH = REPO_ROOT / "data" / "solana_dbot_realized_ledger.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
SOL_THRESHOLD = 4.3
MATCH_WINDOW_S = 120  # пилот копирует лидера с известной задержкой ~4-7с -- 120с щедрый допуск


def gecko_get(path: str, params: dict) -> dict:
    try:
        resp = requests.get(f"{GECKO_BASE}{path}", params=params, timeout=30, headers={"Accept": "application/json"})
        return {"http_status": resp.status_code, "body": resp.json() if resp.ok else None}
    except Exception as exc:  # noqa: BLE001
        return {"http_status": None, "exception": str(exc)[:200]}


def sol_usd_price_at(t: int) -> float | None:
    r = gecko_get("/networks/solana/pools/3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv/ohlcv/minute",
                  {"aggregate": 1, "before_timestamp": t + 3600, "limit": 200, "currency": "usd"})
    rows = (((r.get("body") or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    if not rows:
        return None
    rows = sorted(rows, key=lambda c: c[0])
    import bisect
    times = [c[0] + 60 for c in rows]
    idx = bisect.bisect_right(times, t)
    if idx == 0:
        return rows[0][4]
    if idx >= len(rows):
        return rows[-1][4]
    t0, c0 = times[idx - 1], D(str(rows[idx - 1][4]))
    t1, c1 = times[idx], D(str(rows[idx][4]))
    if t1 == t0:
        return float(c0)
    frac = D(t - t0) / D(t1 - t0)
    return float(c0 + (c1 - c0) * frac)


def scan_leader_first_entries(lo_time: int, hi_time: int) -> list[dict]:
    """classify() по каждой tx лидера в окне -- те же правила, что основной
    конвейер (MIN_SPEND=500 USDC, ровно один положительный минт, Swap/Buy
    в логах). Обрабатываем КАЖДУЮ tx сразу по ходу сканирования (не
    накапливаем полные объекты -- см. установленный в этой сессии урок с
    211МБ-файлом)."""
    entries = []
    before = None
    n_scanned = 0
    for _ in range(200):
        batch = fp.get_signatures_for_address(LEADER_WALLET, before=before, limit=1000)
        if not batch:
            break
        stop = False
        for s in batch:
            bt = s.get("blockTime")
            if bt is None:
                continue
            if bt > hi_time:
                continue
            if bt < lo_time:
                stop = True
                break
            if s.get("err") is not None:
                continue
            tx = fp.get_transaction(s["signature"])
            n_scanned += 1
            if tx is None:
                continue
            row, _check = classify(tx, {"transactionIndex": None})
            if row and row.get("zero_balance"):
                entries.append({"signature": s["signature"], "block_time": bt, "mint": row["mint"],
                                 "usdc_spent": row.get("usdc_spent"), "entry_usdc": row.get("entry_usdc")})
        if stop:
            break
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break
    return entries, n_scanned


def main() -> None:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    now = int(time.time())

    ledger = json.loads(LEDGER_PATH.read_text()) if LEDGER_PATH.exists() else {}
    pilot_recon = (ledger.get("balance_reconciliation") or {}).get("pilot") or {}
    funding = pilot_recon.get("funding_events") or []
    if not funding:
        result["HONEST_ANSWER"] = "нет funding_events пилота в ведомости -- не могу определить старт, ничего не выдумываю."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[capture_rate] " + result["HONEST_ANSWER"], flush=True)
        return
    pilot_start = max(funding, key=lambda e: e["sol_change"])["block_time"]  # крупнейшее реальное пополнение
    result["pilot_start_time"] = pilot_start
    result["pilot_start_note"] = "время крупнейшего реального пополнения боевого кошелька пилота (все funding_events см. ниже)"
    result["all_funding_events"] = funding
    result["window_hours"] = round((now - pilot_start) / 3600, 2)
    print(f"[capture_rate] окно: {result['window_hours']}ч (с {pilot_start} по {now})", flush=True)

    entries, n_scanned = scan_leader_first_entries(pilot_start, now)
    result["n_leader_tx_scanned"] = n_scanned
    result["n_leader_first_entries_all_sizes"] = len(entries)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[capture_rate] лидер: просканировано {n_scanned} tx, первых входов всего {len(entries)}", flush=True)

    big_entries = []
    for e in entries:
        if not e.get("entry_usdc") or not e.get("usdc_spent"):
            continue
        sol_usd = sol_usd_price_at(e["block_time"])
        if not sol_usd:
            continue
        sol_equiv = float(e["usdc_spent"]) / sol_usd
        e["sol_equivalent"] = sol_equiv
        if sol_equiv >= SOL_THRESHOLD:
            big_entries.append(e)
    result["n_leader_first_entries_ge_4_3_sol"] = len(big_entries)
    result["leader_first_entries_ge_4_3_sol_per_day"] = round(len(big_entries) / (result["window_hours"] / 24), 3) if result["window_hours"] else None
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[capture_rate] первых входов >=4.3 SOL-экв.: {len(big_entries)} "
          f"({result['leader_first_entries_ge_4_3_sol_per_day']}/сутки)", flush=True)

    our_trades = [t for t in ledger.get("trades", []) if t.get("label") == "pilot"]
    result["n_pilot_trades_executed"] = len(our_trades)

    matched, missed = [], []
    used = set()
    for e in big_entries:
        hit = None
        for i, t in enumerate(our_trades):
            if i in used:
                continue
            if t.get("mint") == e["mint"] and abs(t.get("buy_block_time", 0) - e["block_time"]) <= MATCH_WINDOW_S:
                hit = i
                break
        if hit is not None:
            used.add(hit)
            matched.append({"leader_entry": e, "our_trade": our_trades[hit]})
        else:
            missed.append(e)

    result["n_matched"] = len(matched)
    result["n_missed"] = len(missed)
    result["capture_rate"] = round(len(matched) / len(big_entries), 4) if big_entries else None
    result["missed_signals"] = [{"mint": m["mint"], "block_time": m["block_time"], "leader_signature": m["signature"],
                                  "sol_equivalent": m.get("sol_equivalent")} for m in missed]
    result["n_pilot_trades_not_matched_to_any_ge_4_3_leader_entry"] = len(our_trades) - len(used)
    result["HONEST_NOTE_on_miss_reasons"] = (
        "DBot API не отдаёт нам причину отказа по конкретному сигналу -- ПРОВЕРЕНО ранее в этой сессии "
        "(data/solana_dbot_failure_pointcheck_result.json): /automation/follow_orders/records, copy_logs, "
        "task_logs, error_logs, follow_records, notifications -- все 401; swap_orders?state=fail -- 200, но "
        "0 строк. Единственный источник причины (already holds / slippage / tx error / вообще не было "
        "записи) -- дашборд DBot, доступный только владельцу. Список missed_signals (минт+время+подпись "
        "лидера) выше -- для проверки каждого случая на дашборде вручную, причины не выдумываем."
    )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[capture_rate] поймано {result['n_matched']}/{len(big_entries)} = {result['capture_rate']}", flush=True)


if __name__ == "__main__":
    main()
