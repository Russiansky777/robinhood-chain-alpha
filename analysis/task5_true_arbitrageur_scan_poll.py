#!/usr/bin/env python3
"""Read-only опрос статуса фонового поиска, запущенного
task5_true_arbitrageur_scan_launch.py -- НЕ запускает ничего нового, НЕ
трогает бюджеты/критерий самого поиска. Просто смотрит: жив ли процесс,
последние строки лога, появился ли итоговый result.json."""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
LOG_PATH = DATA_DIR / "task5_true_arbitrageur_scan_launch.log"
PID_PATH = DATA_DIR / "task5_true_arbitrageur_scan_launch.pid"
RESULT_PATH = DATA_DIR / "task5_true_arbitrageur_scan_result.json"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def main() -> None:
    status: dict = {}
    if PID_PATH.exists():
        pid = int(PID_PATH.read_text().strip())
        status["pid"] = pid
        status["process_alive"] = _pid_alive(pid)
    else:
        status["pid"] = None
        status["process_alive"] = None
        status["note"] = "лаунчер ещё не запускался или pid-файл отсутствует"

    if LOG_PATH.exists():
        lines = LOG_PATH.read_text().splitlines()
        status["log_n_lines"] = len(lines)
        status["log_last_20_lines"] = lines[-20:]
    else:
        status["log_exists"] = False

    if RESULT_PATH.exists():
        status["result_exists"] = True
        result = json.loads(RESULT_PATH.read_text())
        status["result_summary"] = {
            "window_from_block": result.get("window_from_block"),
            "window_to_block_actually_covered": result.get("window_to_block_actually_covered"),
            "scan_timed_out": result.get("scan_timed_out"),
            "n_tx_with_any_swap": result.get("n_tx_with_any_swap"),
            "n_multi_pool_tx": result.get("n_multi_pool_tx"),
            "n_unique_sender_groups": result.get("n_unique_sender_groups"),
            "n_sender_groups_checked": result.get("n_sender_groups_checked"),
            "n_sender_groups_qualifying": result.get("n_sender_groups_qualifying"),
            "n_qualifying_transactions_total": result.get("n_qualifying_transactions_total"),
            "elapsed_seconds": result.get("elapsed_seconds"),
        }
        status["qualifying_rows"] = result.get("qualifying_rows")
    else:
        status["result_exists"] = False

    print(json.dumps(status, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
