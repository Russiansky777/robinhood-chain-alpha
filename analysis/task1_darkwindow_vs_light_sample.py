#!/usr/bin/env python3
"""Задача 1, п.3 -- ДОПОЛНЕНИЕ владельца (2026-09-06, после того как
полный скан 08-28 реально не окупился -- 118k свопов за один тёмный
интервал, тот же класс объёма, что убил `dex.trades`): "тёмное окно
ПРОТИВ светлого" -- честного полного скана обоих окон по всем 33 пулам
владелец больше не просит. Дословно: "добрать выборкой: один случайный
час из каждого тёмного окна по 3-4 пулам, не всё окно."

Метод -- ЧЕСТНАЯ, СИММЕТРИЧНАЯ выборка (не full scan): по каждому из 8
реальных выходных (07-10..08-28) -- ОДИН случайный час внутри Z
(тёмное, вс20:00->пн9:30 ET) И ОДИН случайный час внутри X (светлое,
пт20:00->вс19:55 ET), на ТЕХ ЖЕ 4 самых объёмных пулах (NVDA/SPCX/SPY/
GME -- реально топ-4 по total_vol_usd в task1_liquidity_probe2_result.
json среди 33 сопоставленных тикеров). Одинаковый метод на обоих окнах
-- иначе сравнение нечестное (тёмное окно в основном прогоне мерилось
ПОЛНОСТЬЮ и по ВСЕМ 33 пулам, светлое вообще не мерилось -- нельзя
сравнивать частичное с нулём).

Random seed фиксирован (эта сессия, 2026-09-06) -- воспроизводимо, не
подгоняется под желаемый результат постфактум."""
from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import UNISWAP_V3_SWAP_SIG, _chunked_get_logs, topic0  # noqa: E402
from sleeping_refs_metrics_lib import et_to_utc  # noqa: E402
from task1_darkwindow_trader_probe import KNOWN_P5_BOT, find_block_by_timestamp, topic_to_address  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task1_darkwindow_vs_light_sample_result.json")
SWAP_TOPIC0 = topic0(UNISWAP_V3_SWAP_SIG)
SEED = 20260906

# Топ-4 по объёму среди 33 сопоставленных тикеров (task1_liquidity_probe2_result.json) -- см. докстринг.
SAMPLE_POOLS = {
    "NVDA": "0xd4eb21209c4d6093f80b5b84f5c45cc093ea14a3",
    "SPCX": "0xc61284332117c3fb23a2a56cceffd07f7af60029",
    "SPY": "0xddcbba3666f578e3f09516f21ff85bfee859ab5e",
    "GME": "0xe2b46c905e12ab8e2f864e4821a4325884c1b126",
}
WEEKENDS = ["2026-07-10", "2026-07-17", "2026-07-24", "2026-07-31", "2026-08-07", "2026-08-14", "2026-08-21", "2026-08-28"]


def fetch_hour_swaps(from_block: int, to_block: int) -> list[dict]:
    addrs = list(SAMPLE_POOLS.values())
    logs = list(_chunked_get_logs(from_block, to_block, [SWAP_TOPIC0], chunk_size=100_000, address=addrs))
    addr_to_symbol = {v.lower(): k for k, v in SAMPLE_POOLS.items()}
    out = []
    for lg in logs:
        topics = lg.get("topics", [])
        if len(topics) < 3:
            continue
        out.append({
            "symbol": addr_to_symbol.get(lg["address"].lower(), "?"),
            "sender": topic_to_address(topics[1]), "recipient": topic_to_address(topics[2]),
        })
    return out


def summarize(swaps: list[dict], label: str) -> dict:
    from collections import Counter
    n = len(swaps)
    both = Counter()
    for s in swaps:
        both[s["sender"]] += 1
        both[s["recipient"]] += 1
    self_trade = sum(1 for s in swaps if s["sender"] == s["recipient"])
    p5_bot_hits = sum(1 for s in swaps if KNOWN_P5_BOT in (s["sender"], s["recipient"]))
    top10 = both.most_common(10)
    top3_share = sum(c for _, c in both.most_common(3)) / (2 * n) if n else None
    return {
        "label": label, "n_swaps": n, "n_distinct_addresses": len(both),
        "self_trade_fraction": self_trade / n if n else None,
        "p5_bot_hits": p5_bot_hits, "p5_bot_fraction": p5_bot_hits / n if n else None,
        "top3_share_of_role_slots": top3_share,
        "top10_addresses": [{"address": a, "n_role_slots": c} for a, c in top10],
    }


def run() -> int:
    rng = random.Random(SEED)
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": "1 случайный час на окно на выходные, 4 самых объёмных пула (NVDA/SPCX/SPY/GME), seed=" + str(SEED),
        "sample_pools": SAMPLE_POOLS, "per_weekend": {},
    }
    all_z: list[dict] = []
    all_x: list[dict] = []

    for friday in WEEKENDS:
        y, m, d = (int(v) for v in friday.split("-"))
        fri = datetime(y, m, d)
        sun = fri + timedelta(days=2)
        mon = fri + timedelta(days=3)
        z_start = et_to_utc(sun.year, sun.month, sun.day, 20, 0)
        z_end = et_to_utc(mon.year, mon.month, mon.day, 9, 30)
        x_start = et_to_utc(fri.year, fri.month, fri.day, 20, 0)
        x_end = et_to_utc(sun.year, sun.month, sun.day, 19, 55)

        z_hour_start = z_start + timedelta(seconds=rng.uniform(0, (z_end - z_start).total_seconds() - 3600))
        x_hour_start = x_start + timedelta(seconds=rng.uniform(0, (x_end - x_start).total_seconds() - 3600))

        print(f"\n=== {friday} ===")
        z_lo = find_block_by_timestamp(int(z_hour_start.timestamp()))
        z_hi = find_block_by_timestamp(int((z_hour_start + timedelta(hours=1)).timestamp()))
        z_swaps = fetch_hour_swaps(z_lo, z_hi)
        print(f"  Z-час [{z_hour_start.isoformat()}]: блоки [{z_lo};{z_hi}], реальных свопов {len(z_swaps)}")

        x_lo = find_block_by_timestamp(int(x_hour_start.timestamp()))
        x_hi = find_block_by_timestamp(int((x_hour_start + timedelta(hours=1)).timestamp()))
        x_swaps = fetch_hour_swaps(x_lo, x_hi)
        print(f"  X-час [{x_hour_start.isoformat()}]: блоки [{x_lo};{x_hi}], реальных свопов {len(x_swaps)}")

        all_z.extend(z_swaps)
        all_x.extend(x_swaps)
        result["per_weekend"][friday] = {
            "z_hour_utc": z_hour_start.isoformat(), "x_hour_utc": x_hour_start.isoformat(),
            "z_summary": summarize(z_swaps, f"{friday} Z-час"), "x_summary": summarize(x_swaps, f"{friday} X-час"),
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    print("\n=== ПУЛ ПО ВСЕМ 8 ВЫБОРКАМ ===")
    z_pooled = summarize(all_z, "Z-часы pooled")
    x_pooled = summarize(all_x, "X-часы pooled")
    result["pooled"] = {"Z": z_pooled, "X": x_pooled}
    for k in ("n_swaps", "n_distinct_addresses", "self_trade_fraction", "p5_bot_fraction", "top3_share_of_role_slots"):
        print(f"  Z: {k} = {z_pooled[k]}   |   X: {k} = {x_pooled[k]}")

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[sample] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
