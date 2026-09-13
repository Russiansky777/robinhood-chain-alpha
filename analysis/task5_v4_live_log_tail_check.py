#!/usr/bin/env python3
"""Задача 5, подтверждение запуска: хвост реального лога текущего LIVE
на Ohio (read-only, cat -- не трогает файл)."""
from __future__ import annotations

import glob
import json
import os

LOG_GLOB = "/home/bot/data/task5_v4_hotpath_live_*.log"


def main() -> None:
    logs = sorted(glob.glob(LOG_GLOB), key=os.path.getmtime)
    result = {"all_log_files": logs}
    if logs:
        newest = logs[-1]
        with open(newest, "r", errors="replace") as fh:
            lines = fh.readlines()
        result["newest_log_file"] = newest
        result["tail_lines"] = lines[-40:]
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
