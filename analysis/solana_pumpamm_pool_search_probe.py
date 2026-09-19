#!/usr/bin/env python3
"""Разовая, быстрая: проверить, находит ли getSignaturesForAddress по
АДРЕСУ ПУЛА (не минта) сделку AKBot -- без дорогого getBlock-скана
целых блоков (тот подход оказался неприемлемо медленным на мейннете,
тысячи транзакций на слот даже на уровне detail='accounts'). Если
поиск по пулу сам по себе находит AKBot -- getBlock-скан не нужен
вообще, п.2 закрывается дешёвым способом."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_pumpamm_pool_search_probe_result.json"

LEADER_SIG = "JFPJEgfXH2PEfssK799nob9KE6J5Lk4PLcD1xRqqieTUP6rxksXTZAR7eA1jrVU7ejychsqwf6JvN2pd4M5hAqY"
AKBOT_SIG = "65WA3e1VhP8Ynsyg1Jv7onsvDciZSFB7zNzwvpHXQ56gzg4KgqteQr57qKX8q8hTBehyW6mkx2E8sKN66oHQ1Q6t"
MINT = "98rAFRoTjWVS6au9zRHcLuc3NdfaXQzvR6wJLKu2pump"
POOL = "CMhv5RKL7axdiMoaesxqJLvAb7rJiSbR7RnH6eMPvqtS"


def fetch_signatures_in_slot_window(address: str, ref_time: int, lo_slot: int, hi_slot: int, time_buffer_s: int = 30) -> list[dict]:
    anchor_sig, _ = fp.find_anchor_after(ref_time + time_buffer_s, safety_margin_s=5)
    hist: list[dict] = []
    before = anchor_sig
    while True:
        page = fp.get_signatures_for_address(address, before=before, limit=1000)
        if not page:
            break
        hist.extend(page)
        oldest = page[-1].get("blockTime")
        before = page[-1]["signature"]
        if oldest is not None and oldest <= ref_time - time_buffer_s:
            break
        if len(page) < 1000:
            break
    return [h for h in hist if h.get("slot") is not None and lo_slot <= h["slot"] <= hi_slot]


def main() -> None:
    leader_tx = fp.get_transaction(LEADER_SIG)
    leader_slot = leader_tx["slot"]
    leader_time = leader_tx["blockTime"]
    lo_slot, hi_slot = leader_slot - 3, leader_slot + 7

    mint_sigs = fetch_signatures_in_slot_window(MINT, leader_time, lo_slot, hi_slot)
    pool_sigs = fetch_signatures_in_slot_window(POOL, leader_time, lo_slot, hi_slot)

    mint_has_akbot = any(s["signature"] == AKBOT_SIG for s in mint_sigs)
    pool_has_akbot = any(s["signature"] == AKBOT_SIG for s in pool_sigs)

    result = {
        "leader_slot": leader_slot, "window": [lo_slot, hi_slot],
        "n_sigs_via_mint": len(mint_sigs), "mint_search_finds_akbot": mint_has_akbot,
        "n_sigs_via_pool": len(pool_sigs), "pool_search_finds_akbot": pool_has_akbot,
        "pool_sigs_slots": sorted({s["slot"] for s in pool_sigs}),
        "conclusion": ("поиск по пулу сам находит AKBot -- getBlock-скан не нужен" if pool_has_akbot
                       else "поиск по пулу тоже НЕ находит AKBot -- нужен другой метод (не block-скан целых блоков)"),
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
