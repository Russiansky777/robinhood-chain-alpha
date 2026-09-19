#!/usr/bin/env python3
"""Владелец, 2026-09-20: find_canonical_signals работает верно на уже
полученной транзакции (доказано предыдущей диагностикой) -- значит баг
в СКАНИРОВАНИИ/пагинации scan_leader_window, не в классификации. Этот
скрипт повторяет ТУ ЖЕ пагинацию (get_signatures_for_address, before=,
limit=1000) на том же окне и явно проверяет: попадают ли 2 известные
подписи лидера в СЫРОЙ список вообще (до какой-либо классификации), и
на каком именно шаге пагинации."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_capture_scan_pagination_diagnostic.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
KNOWN_SIGS = {
    "4vPsRrt1cNtqxChbZFKDnDTTN85BweBn3cRSsRfVmjXvs4e1pg39S9cEaXei2XDDHi3JwF1benw4bDmfPCWGFLdn": 1789749474,
    "5jpYqPa3yaTuMBaSc8JndfTPfMkEGnN7tLBGyZiwUQtCjUUUFz5iAhjUcczTCzDFbJyiYX1CzMBXhPzQfhUEK7SD": 1789750541,
}


def main() -> None:
    cap = json.loads((REPO_ROOT / "data" / "solana_pilot_signal_capture_rate_v2.json").read_text())
    lo_time = cap["pilot_start_time"]
    hi_time = lo_time + int(cap["window_hours"] * 3600) + 300  # тот же порядок, с запасом

    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "lo_time": lo_time, "hi_time": hi_time, "pages": []}
    before = None
    found_sigs = {}
    n_total_scanned = 0
    stop_reason = None
    for page_i in range(200):
        batch = fp.get_signatures_for_address(LEADER_WALLET, before=before, limit=1000)
        if not batch:
            stop_reason = "empty_batch"
            break
        block_times = [s.get("blockTime") for s in batch if s.get("blockTime") is not None]
        page_info = {"page": page_i, "n_sigs": len(batch),
                     "first_bt": block_times[0] if block_times else None,
                     "last_bt": block_times[-1] if block_times else None}
        for s in batch:
            n_total_scanned += 1
            if s["signature"] in KNOWN_SIGS:
                found_sigs[s["signature"]] = {"page": page_i, "block_time": s.get("blockTime"),
                                               "expected_block_time": KNOWN_SIGS[s["signature"]],
                                               "err": s.get("err")}
        result["pages"].append(page_info)
        print(f"[pagdiag] стр.{page_i}: n={len(batch)} bt=[{page_info['last_bt']}..{page_info['first_bt']}]", flush=True)

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
        if stop:
            stop_reason = "reached_lo_time"
            break
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            stop_reason = "batch_smaller_than_limit"
            break
    else:
        stop_reason = "page_budget_exhausted_200"

    result["stop_reason"] = stop_reason
    result["n_total_scanned"] = n_total_scanned
    result["n_pages"] = len(result["pages"])
    result["known_sigs_found_in_raw_pagination"] = found_sigs
    result["known_sigs_missing_from_raw_pagination"] = [s for s in KNOWN_SIGS if s not in found_sigs]

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[pagdiag] stop_reason={stop_reason} n_pages={len(result['pages'])} n_scanned={n_total_scanned}", flush=True)
    print(f"[pagdiag] известные подписи найдены в сырой пагинации: {found_sigs}", flush=True)
    print(f"[pagdiag] известные подписи ОТСУТСТВУЮТ в сырой пагинации: {result['known_sigs_missing_from_raw_pagination']}", flush=True)


if __name__ == "__main__":
    main()
