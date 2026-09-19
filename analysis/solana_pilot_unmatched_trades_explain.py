#!/usr/bin/env python3
"""Владелец, 2026-09-19: Task 4 -- из 6 исполненных сделок пилота только
3 соответствуют сигналам лидера >=4.3 SOL (solana_pilot_signal_capture_rate_v2.json).
По остальным трём (уже известен leader_signature из ведомости) -- какая
сделка лидера её вызвала, её реальный размер (classify(), тот же метод,
что и весь основной конвейер) и первый вход или докупка."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_buyer200_select_extend import classify  # noqa: E402
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_pilot_unmatched_trades_explain.json"
LEDGER_PATH = REPO_ROOT / "data" / "solana_dbot_realized_ledger.json"
CAPTURE_V2_PATH = REPO_ROOT / "data" / "solana_pilot_signal_capture_rate_v2.json"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"


def gecko_get(path: str, params: dict) -> dict:
    resp = requests.get(f"{GECKO_BASE}{path}", params=params, timeout=30, headers={"Accept": "application/json"})
    return {"http_status": resp.status_code, "body": resp.json() if resp.ok else None}


def sol_usd_price_at(t: int) -> float | None:
    r = gecko_get("/networks/solana/pools/3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv/ohlcv/minute",
                  {"aggregate": 1, "before_timestamp": t + 3600, "limit": 200, "currency": "usd"})
    rows = (((r.get("body") or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    if not rows:
        return None
    rows = sorted(rows, key=lambda c: c[0])
    import bisect
    from decimal import Decimal as D
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


def main() -> None:
    ledger = json.loads(LEDGER_PATH.read_text())
    capture = json.loads(CAPTURE_V2_PATH.read_text()) if CAPTURE_V2_PATH.exists() else {}
    matched_mints = {m["mint"] for m in capture.get("matched_with_classify_explanation", [])}

    pilot_trades = [t for t in ledger.get("trades", []) if t.get("label") == "pilot" and t.get("status") == "ok"]
    unmatched = [t for t in pilot_trades if t.get("mint") not in matched_mints]

    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "n_pilot_trades_total": len(pilot_trades), "n_matched_ge_4_3": len(matched_mints),
                     "n_unmatched": len(unmatched), "unmatched_explained": []}

    for t in unmatched:
        leader_sig = t.get("leader_signature")
        entry: dict = {"our_mint": t["mint"], "our_buy_signature": t["buy_signature"], "leader_signature": leader_sig}
        if not leader_sig:
            entry["status"] = "no_leader_signature_in_ledger"
            result["unmatched_explained"].append(entry)
            continue
        tx = fp.get_transaction(leader_sig)
        if not tx:
            entry["status"] = "leader_tx_fetch_failed"
            result["unmatched_explained"].append(entry)
            continue
        row, check = classify(tx, {"transactionIndex": None})
        entry["leader_tx_block_time_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tx.get("blockTime", 0)))
        if row:
            entry["classify_zero_balance_first_entry"] = row.get("zero_balance")
            entry["leader_usdc_spent"] = row.get("usdc_spent")
            sol_usd = sol_usd_price_at(tx.get("blockTime"))
            entry["sol_usd_rate"] = sol_usd
            entry["leader_sol_equivalent"] = (float(row["usdc_spent"]) / sol_usd) if row.get("usdc_spent") and sol_usd else None
            entry["status"] = "ok"
            entry["explanation"] = (
                ("ПЕРВЫЙ ВХОД" if row.get("zero_balance") else "ДОКУПКА") +
                f", реальный размер {entry['leader_sol_equivalent']:.3f} SOL-экв -- " +
                ("НИЖЕ порога 4.3 SOL, значит Copy Buy Range в DBot скопировал сигнал МЕНЬШЕГО размера, чем "
                 "порог сита -- проверить фактическую настройку Copy Buy Range задачи." if entry["leader_sol_equivalent"] and entry["leader_sol_equivalent"] < 4.3
                 else "размер >=4.3, но не попал в 5 канонических сигналов capture_rate_v2 -- см. отдельно, возможно вне окна сканирования.")
            )
        else:
            entry["status"] = "classify_returned_none"
            entry["classify_check"] = check
        result["unmatched_explained"].append(entry)
        print(f"[unmatched] {t['mint'][:10]}.. -> leader {leader_sig[:15]}.. {entry.get('explanation', entry.get('status'))}", flush=True)

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[unmatched] записано в {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
