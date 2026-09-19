#!/usr/bin/env python3
"""Владелец, 2026-09-19: пересчёт захвата пилота по КАНОНИЧЕСКОМУ
определению сигнала (Task 2, solana_signal_definition.py) -- без
MIN_SPEND=500 USDC и без "ровно один минт" (это ограничения classify()).
classify() запускается ПАРАЛЛЕЛЬНО на тех же транзакциях ТОЛЬКО для
объяснения расхождения (почему старое определение считало иначе), не
как определение сигнала.

По каждой из 6 исполненных сделок пилота -- какой сигнал источника её
вызвал (размер в SOL, время), и результат classify() на той же
транзакции (посчитал бы он её сигналом, и если нет -- почему конкретно:
spent<500 USDC, >1 положительный минт, или лог без Swap/Buy).

По каждому пропуску -- полный минт, полная подпись лидера, время UTC."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from decimal import Decimal as D

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_signal_definition import find_canonical_signals  # noqa: E402
from solana_buyer200_select_extend import classify  # noqa: E402
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_pilot_signal_capture_rate_v2.json"
LEDGER_PATH = REPO_ROOT / "data" / "solana_dbot_realized_ledger.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
SOL_THRESHOLD = 4.3
MATCH_WINDOW_S = 120


def gecko_get(path: str, params: dict) -> dict:
    try:
        resp = requests.get(f"{GECKO_BASE}{path}", params=params, timeout=30, headers={"Accept": "application/json"})
        return {"http_status": resp.status_code, "body": resp.json() if resp.ok else None}
    except Exception as exc:  # noqa: BLE001
        return {"http_status": None, "exception": str(exc)[:200]}


_sol_cache: dict[int, float | None] = {}


def sol_usd_price_at(t: int) -> float | None:
    if t in _sol_cache:
        return _sol_cache[t]
    r = gecko_get("/networks/solana/pools/3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv/ohlcv/minute",
                  {"aggregate": 1, "before_timestamp": t + 3600, "limit": 200, "currency": "usd"})
    rows = (((r.get("body") or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    if not rows:
        _sol_cache[t] = None
        return None
    rows = sorted(rows, key=lambda c: c[0])
    import bisect
    times = [c[0] + 60 for c in rows]
    idx = bisect.bisect_right(times, t)
    if idx == 0:
        v = rows[0][4]
    elif idx >= len(rows):
        v = rows[-1][4]
    else:
        t0, c0 = times[idx - 1], D(str(rows[idx - 1][4]))
        t1, c1 = times[idx], D(str(rows[idx][4]))
        v = float(c0) if t1 == t0 else float(c0 + (c1 - c0) * (D(t - t0) / D(t1 - t0)))
    _sol_cache[t] = v
    return v


def classify_explain(tx: dict) -> dict:
    """classify() на той же tx -- посчитал бы её сигналом (по старому
    определению), и если нет -- честная причина."""
    row, check = classify(tx, {"transactionIndex": None})
    if row:
        return {"classify_would_count": True, "classify_zero_balance": row.get("zero_balance")}
    reason = "неизвестно (см. check)"
    status = (check or {}).get("status")
    if status:
        reason = status
    return {"classify_would_count": False, "classify_reason": reason, "classify_check": check}


def scan_leader_window(lo_time: int, hi_time: int) -> tuple[list[dict], list[dict], int]:
    """Возвращает (canonical_signals, classify_first_entries, n_scanned) --
    оба определения на ОДНОМ проходе по транзакциям, для честного
    попарного сравнения."""
    canonical, classify_hits = [], []
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
            sigs = find_canonical_signals(tx, LEADER_WALLET, SOL_THRESHOLD, sol_usd_price_at)
            for sig in sigs:
                canonical.append({**sig, "signature": s["signature"]})
            row, _check = classify(tx, {"transactionIndex": None})
            if row and row.get("zero_balance"):
                classify_hits.append({"signature": s["signature"], "block_time": bt, "mint": row["mint"],
                                       "usdc_spent": row.get("usdc_spent")})
        if stop:
            break
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break
    return canonical, classify_hits, n_scanned


def main() -> None:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "definition": "канонический сигнал: новый минт (pre=0) + signer в любой позиции + "
                                    f"spend>={SOL_THRESHOLD} SOL-экв, БЕЗ MIN_SPEND/без условия одного минта"}
    now = int(time.time())

    ledger = json.loads(LEDGER_PATH.read_text()) if LEDGER_PATH.exists() else {}
    pilot_recon = (ledger.get("balance_reconciliation") or {}).get("pilot") or {}
    funding = pilot_recon.get("funding_events") or []
    if not funding:
        result["HONEST_ANSWER"] = "нет funding_events пилота -- не могу определить старт."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    pilot_start = max(funding, key=lambda e: e["sol_change"])["block_time"]
    result["pilot_start_time"] = pilot_start
    result["window_hours"] = round((now - pilot_start) / 3600, 2)

    canonical, classify_hits, n_scanned = scan_leader_window(pilot_start, now)
    result["n_leader_tx_scanned"] = n_scanned
    result["n_canonical_signals_ge_4_3"] = len(canonical)
    result["canonical_signals_per_day"] = round(len(canonical) / (result["window_hours"] / 24), 3) if result["window_hours"] else None
    result["n_classify_first_entries_all_sizes"] = len(classify_hits)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[capture_v2] окно {result['window_hours']}ч: канонических сигналов>=4.3={len(canonical)} "
          f"({result['canonical_signals_per_day']}/сутки), classify()-первых входов всего={len(classify_hits)}", flush=True)

    our_trades = [t for t in ledger.get("trades", []) if t.get("label") == "pilot"]
    matched, missed = [], []
    used = set()
    for sig in canonical:
        hit = None
        for i, t in enumerate(our_trades):
            if i in used:
                continue
            if t.get("mint") == sig["mint"] and abs(t.get("buy_block_time", 0) - sig["block_time"]) <= MATCH_WINDOW_S:
                hit = i
                break
        if hit is not None:
            used.add(hit)
            leader_tx = fp.get_transaction(sig["signature"])
            explain = classify_explain(leader_tx) if leader_tx else {"classify_would_count": None}
            matched.append({"mint": sig["mint"], "sol_equivalent": sig["sol_equivalent"],
                             "leader_signature": sig["signature"], "block_time": sig["block_time"],
                             "our_trade_buy_signature": our_trades[hit]["buy_signature"], **explain})
        else:
            missed.append(sig)

    result["n_matched"] = len(matched)
    result["n_missed"] = len(missed)
    result["capture_rate"] = round(len(matched) / len(canonical), 4) if canonical else None
    result["matched_with_classify_explanation"] = matched
    result["missed_signals_full_detail"] = [
        {"mint": m["mint"], "leader_signature": m["signature"],
         "block_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(m["block_time"])),
         "sol_equivalent": m["sol_equivalent"]} for m in missed
    ]
    result["n_pilot_trades_not_matched"] = len(our_trades) - len(used)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[capture_v2] поймано {len(matched)}/{len(canonical)} = {result['capture_rate']}", flush=True)

    # --- Расхождение 11.7 vs 32/сутки ---
    old_v1_path = REPO_ROOT / "data" / "solana_pilot_signal_capture_rate.json"
    old_rate = None
    if old_v1_path.exists():
        old_rate = json.loads(old_v1_path.read_text()).get("leader_first_entries_ge_4_3_sol_per_day")
    result["reconciliation_11_7_vs_32"] = {
        "old_classify_based_rate_per_day": old_rate,
        "new_canonical_rate_per_day": result["canonical_signals_per_day"],
        "user_recalled_rate_per_day": 32,
        "note": (
            "Канонический (без MIN_SPEND/без 'один минт') даёт "
            f"{result['canonical_signals_per_day']}/сутки против classify()-based {old_rate}/сутки на ТОМ ЖЕ окне -- "
            + ("определение объясняет часть расхождения, но НЕ доводит до 32/сутки -- честно: остаточная разница "
               "не объяснена определением, возможно другой порог/окно у владельца, либо реальная суточная "
               "вариативность (10.25ч -- очень короткое и нерепрезентативное окно)."
               if result["canonical_signals_per_day"] and abs(result["canonical_signals_per_day"] - 32) > 5
               else "определение почти полностью объясняет расхождение.")
        ),
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[capture_v2] {result['reconciliation_11_7_vs_32']['note']}", flush=True)


if __name__ == "__main__":
    main()
