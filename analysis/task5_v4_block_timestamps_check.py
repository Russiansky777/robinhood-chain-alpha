#!/usr/bin/env python3
"""Реальные timestamp блоков (eth_getBlockByNumber) для обоих примеров --
только это поле, без интерпретации точности выше секунды (у EVM-блоков
timestamp -- целые секунды; расстояние конкурента в блоках НЕ равно
миллисекундам)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import get_block  # noqa: E402

BLOCKS = [62371949, 62371950, 62371951, 62373269, 62373270, 62373271]


def main() -> None:
    result = {}
    for b in BLOCKS:
        blk = get_block(b)
        result[b] = {"timestamp_unix": int(blk["timestamp"], 16)} if blk else {"error": "not found"}
    ordered = sorted(result.items())
    for i in range(1, len(ordered)):
        prev_b, prev = ordered[i - 1]
        cur_b, cur = ordered[i]
        if "timestamp_unix" in prev and "timestamp_unix" in cur and cur_b == prev_b + 1:
            cur["delta_seconds_from_prev_block"] = cur["timestamp_unix"] - prev["timestamp_unix"]
    print(json.dumps(result, indent=2, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_block_timestamps_check_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"[block_timestamps] сохранено: {out_path}")


if __name__ == "__main__":
    main()
