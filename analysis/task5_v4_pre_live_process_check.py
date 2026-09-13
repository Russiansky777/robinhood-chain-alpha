#!/usr/bin/env python3
"""Задача 5, перед повторным LIVE: независимая проверка, что НИКАКОЙ
другой task5_v4_hotpath.py процесс сейчас не работает на Ohio -- по
реальному ps, не по статусу GitHub-джобы (тот же принцип, что и во всех
предыдущих проверках этой сессии). Также перечисляет все сохранённые
*.pid файлы предыдущих LIVE-запусков и их фактическую живость."""
from __future__ import annotations

import glob
import json
import os
import subprocess
from pathlib import Path


def main() -> None:
    result: dict = {}

    ps_out = subprocess.run(["ps", "-eo", "pid,ppid,stat,etime,cmd"], capture_output=True, text=True, timeout=15)
    matching = [line for line in ps_out.stdout.splitlines() if "task5_v4_hotpath.py" in line and "grep" not in line]
    result["ps_matching_hotpath_processes"] = matching
    result["any_hotpath_process_alive"] = len(matching) > 0

    pid_files = sorted(glob.glob("/home/bot/data/task5_v4_hotpath_live_*.pid"))
    pid_status = {}
    for pf in pid_files:
        try:
            pid = int(open(pf).read().strip())
        except Exception as exc:  # noqa: BLE001
            pid_status[pf] = {"error": str(exc)}
            continue
        alive = os.path.exists(f"/proc/{pid}") and subprocess.run(["kill", "-0", str(pid)],
                                                                    capture_output=True).returncode == 0
        pid_status[pf] = {"pid": pid, "alive": alive}
    result["known_pid_files"] = pid_status

    stop_file_present = os.path.exists("/etc/bot/STOP")
    result["stop_file_present"] = stop_file_present

    print(json.dumps(result, indent=2, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_pre_live_process_check_result.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
