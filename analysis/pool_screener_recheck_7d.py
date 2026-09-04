#!/usr/bin/env python3
"""Скринер пулов -- владелец, п.4: проверка выброса USDC-CBBTC apyBase=548.58%
(и заодно всех 23 AMM-кандидатов) -- apyBase7d против apyBase (сегодняшний
снимок), объём за 7д против 24ч. Если 548% -- один спайковый день, а не
устойчивый уровень, отбросить как шум.

Реальные поля DefiLlama yields.llama.fi/pools (не выдуманные): apyBase7d,
apyMean30d, volumeUsd7d -- документированная схема агрегатора."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"


def run() -> int:
    prev = json.load(open("data/p3_guard_cache/pool_screener_resolve_pools_result.json"))
    target_ids = {c["pool_id"] for c in prev["candidates"]}
    print(f"[recheck] проверяю {len(target_ids)} пулов (все 23 AMM-кандидата) на apyBase7d/volumeUsd7d")

    r = requests.get(DEFILLAMA_POOLS_URL, timeout=30)
    r.raise_for_status()
    all_pools = r.json().get("data", [])
    by_id = {p["pool"]: p for p in all_pools}

    results = []
    for c in prev["candidates"]:
        p = by_id.get(c["pool_id"])
        if p is None:
            results.append({**c, "recheck_error": "pool_id не найден в свежем снимке DefiLlama (возможно, пул закрыт/переиндексирован)"})
            continue
        vol1d = p.get("volumeUsd1d")
        vol7d = p.get("volumeUsd7d")
        entry = {
            "pool_id": c["pool_id"], "chain": c["chain"], "project": c["project"], "symbol": c["symbol"],
            "apyBase_snapshot_original": c.get("apyBase"),
            "apyBase_now": p.get("apyBase"),
            "apyBase7d_now": p.get("apyBase7d"),
            "apyMean30d_now": p.get("apyMean30d"),
            "volumeUsd1d_now": vol1d,
            "volumeUsd7d_now": vol7d,
            "volumeUsd7d_over_7_vs_1d_ratio": (vol7d / 7 / vol1d) if (vol7d and vol1d) else None,
            "tvlUsd_now": p.get("tvlUsd"),
        }
        # Флаг спайка: сегодняшний apyBase сильно (>2x) выше 7-дневного среднего -- одна аномальная свеча тянет
        # средневзвешенный apyBase (DefiLlama обычно считает его по последним 24ч объёму/комиссии).
        if entry["apyBase_now"] is not None and entry["apyBase7d_now"] is not None and entry["apyBase7d_now"] > 0:
            entry["ratio_apyBase_now_over_7d"] = entry["apyBase_now"] / entry["apyBase7d_now"]
            entry["spike_flag"] = entry["ratio_apyBase_now_over_7d"] > 2.0
        else:
            entry["ratio_apyBase_now_over_7d"] = None
            entry["spike_flag"] = None
        results.append(entry)
        flag = " *** ВЫБРОС ***" if entry.get("spike_flag") else ""
        print(f"  {entry['chain']:10} {entry['project']:22} {entry['symbol']:22} "
              f"apyBase(24ч)={entry['apyBase_now']} apyBase7d={entry['apyBase7d_now']} "
              f"ratio={entry['ratio_apyBase_now_over_7d']}{flag}")

    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "candidates": results}
    Path("data/p3_guard_cache/pool_screener_recheck_7d_result.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
