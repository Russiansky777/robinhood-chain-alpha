#!/usr/bin/env python3
"""Владелец, п.2 ускорения крауд-скана: после 4 параллельных шардов
(strategy.matrix в .github/workflows/crowd_night.yml) собирает общий
data/night_status.json (прогресс) и объединённый data/solana_fomo_crowd.json
(все готовые кошельки всех шардов) -- отдельный шаг, идущий ПОСЛЕ всех
4 шардов (needs: shard_scan), чтобы не было гонки записи в общий файл
между параллельными job'ами (каждый шард пишет только СВОИ два файла:
data/night_status_shard_N.json и data/solana_fomo_crowd_shard_N.json)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_crowd_night import TOTAL_SHARDS, load_ranked_wallets  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUS_PATH = REPO_ROOT / "data" / "night_status.json"
CROWD_PATH = REPO_ROOT / "data" / "solana_fomo_crowd.json"


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_url() -> str | None:
    server, repo, rid = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{rid}" if server and repo and rid else None


def main() -> None:
    total_wallets = len(load_ranked_wallets())  # SHARD_ID не задан в этом скрипте -- полный список 90

    shards = []
    crowd_all: dict = {}
    for shard_id in range(TOTAL_SHARDS):
        status_path = REPO_ROOT / "data" / f"night_status_shard_{shard_id}.json"
        crowd_path = REPO_ROOT / "data" / f"solana_fomo_crowd_shard_{shard_id}.json"
        status = {}
        if status_path.exists():
            try:
                status = json.loads(status_path.read_text())
            except (ValueError, OSError):
                status = {}
        crowd = {}
        if crowd_path.exists():
            try:
                crowd = json.loads(crowd_path.read_text())
            except (ValueError, OSError):
                crowd = {}
        crowd_all.update(crowd)
        shards.append({
            "shard_id": shard_id,
            "stage": status.get("stage", "scan"),
            "done_wallets": len(crowd),
            "total_wallets": status.get("total_wallets"),
            "last_error": status.get("last_error"),
            "updated_utc": status.get("updated_utc"),
            "run_url": status.get("run_url"),
        })

    done_wallets = len(crowd_all)
    all_done = all(s["stage"] == "done" for s in shards)
    result = {
        "stage": "done" if all_done else "scan",
        "done_wallets": done_wallets,
        "total_wallets": total_wallets,
        "shards": shards,
        "updated_utc": now_utc(),
        "run_url": run_url(),
    }
    STATUS_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    CROWD_PATH.write_text(json.dumps(crowd_all, ensure_ascii=False, indent=2, default=str))
    print(f"[crowd_night_aggregate] done_wallets={done_wallets}/{total_wallets} stage={result['stage']} "
          f"(по шардам: {[(s['shard_id'], s['done_wallets'], s['stage']) for s in shards]})", flush=True)


if __name__ == "__main__":
    main()
