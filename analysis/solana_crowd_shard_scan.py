#!/usr/bin/env python3
"""Владелец, ночное задание п.2: скан "толпы" по 90 кошелькам из
data/solana_fomo_passed.json, шарды по 4 (round-robin по рангу Fomo,
как в скане 217), тот же исправленный способ записи (fetch+reset вместо
git rebase -- см. solana_batch5_shard_scan.py, там же найден и
исправлен баг с зависанием rebase на общем файле).

Запускается ТОЛЬКО если data/solana_crowd_control_result.json уже
существует с DECISION=go (иначе -- честный отказ, RPC не тратим)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_crowd_common import analyze_wallet  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTROL_PATH = REPO_ROOT / "data" / "solana_crowd_control_result.json"
PASSED_PATH = REPO_ROOT / "data" / "solana_fomo_passed.json"

TOTAL_SHARDS = 4
TIME_BUDGET_S = 45 * 60
COMMIT_INTERVAL_S = 60


def load_passed_ranked() -> list[dict]:
    rows = []
    for line in PASSED_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    rows.sort(key=lambda r: r.get("fomo_rank", 10**9))
    return rows


def main() -> None:
    shard_id = int(os.environ.get("SHARD_ID", "0"))
    out_path = REPO_ROOT / "data" / f"solana_crowd_shard_{shard_id}.json"

    if not CONTROL_PATH.exists() or json.loads(CONTROL_PATH.read_text()).get("DECISION") != "go":
        print(f"[crowd_shard {shard_id}] контроль ещё не 'go' -- честно ничего не делаю", flush=True)
        out_path.write_text(json.dumps({"shard_id": shard_id, "HONEST_ANSWER": "контроль не пройден/не готов, скан не запускался"}, ensure_ascii=False, indent=2))
        fp._git_commit_progress(f"crowd_shard_{shard_id}_skip", [out_path])
        return

    ranked = load_passed_ranked()
    shard_wallets = [r for idx, r in enumerate(ranked) if idx % TOTAL_SHARDS == shard_id]
    print(f"[crowd_shard {shard_id}] моих кошельков: {len(shard_wallets)} из {len(ranked)}", flush=True)

    result = json.loads(out_path.read_text()) if out_path.exists() else {"shard_id": shard_id, "wallets": {}}
    wallets = result["wallets"]
    started_at = time.monotonic()
    last_commit_at = started_at
    n_done = 0
    for row in shard_wallets:
        addr = row["address"]
        if addr in wallets:
            continue
        if time.monotonic() - started_at > TIME_BUDGET_S:
            print(f"[crowd_shard {shard_id}] бюджет времени исчерпан -- остальное на следующий прогон", flush=True)
            break
        r = analyze_wallet(addr, row.get("name"), min_sol=2.0)
        r["fomo_rank"] = row.get("fomo_rank")
        wallets[addr] = r
        n_done += 1
        print(f"[crowd_shard {shard_id}] {row.get('name')} ({addr[:10]}..): n={r['n_purchases_analyzed']} "
              f"unresolved={r['n_unresolved']} empty_share={r['empty_share']} "
              f"медиана_роста={r['median_growth_pct_30s']}", flush=True)
        if time.monotonic() - last_commit_at > COMMIT_INTERVAL_S:
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            fp._git_commit_progress(f"crowd_shard_{shard_id}", [out_path])
            last_commit_at = time.monotonic()

    result["n_shard_total"] = len(shard_wallets)
    result["n_shard_scanned"] = len(wallets)
    result["shard_complete"] = len(wallets) >= len(shard_wallets)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    fp._git_commit_progress(f"crowd_shard_{shard_id}_final", [out_path])
    print(f"[crowd_shard {shard_id}] итог: {n_done} сейчас, всего {len(wallets)}/{len(shard_wallets)}, "
          f"complete={result['shard_complete']}", flush=True)


if __name__ == "__main__":
    main()
