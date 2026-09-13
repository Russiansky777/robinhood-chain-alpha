#!/usr/bin/env python3
"""Дамп ТОЛЬКО реальных расчётов (calc_duration_s is not None) из уже
сохранённого session2b_only_no_send_log.jsonl -- компактный JSONL,
без пересчёта, без нового скана."""
import json

PATH = "/home/bot/robinhood-chain-alpha/data/task5_v4_pilot_session2b_only_no_send_log.jsonl"
OUT = "/home/bot/robinhood-chain-alpha/data/task5_v4_pilot_session2b_real_calc_only.jsonl"

n = 0
with open(PATH) as fh, open(OUT, "w") as out:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("calc_duration_s") is not None:
            out.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
            n += 1

print(f"n_real_calc_written={n}")
import os
print(f"out_size_bytes={os.path.getsize(OUT)}")
