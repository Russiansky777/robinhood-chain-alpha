#!/usr/bin/env python3
"""Задача 5: ЧИСТО ЧИТАЮЩАЯ проверка состояния ТЕКУЩЕГО LIVE-пилота на
Ohio (по прямому указанию владельца: "без остановки и перезапуска",
"код не менять, новый пилот не запускать"). Ничего не пишет, ничего не
шлёт, ничего не останавливает -- только читает уже существующие файлы
(лог процесса, состояние бюджета) и опрашивает PID через `ps` (обычный
системный вызов, не влияет на процесс).

RUN_TAG -- жёстко задан ЗДЕСЬ (не параметр workflow), т.к. это ТЕКУЩИЙ,
уже запущенный владельцем прогон (run_id=34778703033, run_attempt=1,
запущен 2026-09-13T19:46:23Z, commit f474571...) -- НЕ новый запуск."""
import json
import subprocess
import time
from pathlib import Path

RUN_TAG = "run34778703033_1"
LOG_FILE = Path(f"/home/bot/data/task5_v4_hotpath_live_{RUN_TAG}.log")
PID_FILE = Path(f"/home/bot/data/task5_v4_hotpath_live_{RUN_TAG}.pid")
BUDGET_STATE_FILE = Path("/home/bot/data/task5_v4_pilot_budget_state.json")
REASON_LOG_FILE = Path("/home/bot/data/task5_v4_pilot_no_send_log.jsonl")
ATTEMPT_TABLE_FILE = Path("/home/bot/data/task5_v4_pilot_attempts.jsonl")


def tail_lines(path: Path, n: int) -> list[str]:
    if not path.exists():
        return [f"(файл не найден: {path})"]
    with path.open("r", errors="replace") as fh:
        lines = fh.readlines()
    return [l.rstrip("\n") for l in lines[-n:]]


def last_jsonl_ts(path: Path) -> dict | None:
    if not path.exists():
        return None
    last = None
    with path.open("r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                last = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
    return last


def main() -> None:
    result: dict = {"run_tag": RUN_TAG, "checked_at_wall": time.time()}

    result["log_file"] = str(LOG_FILE)
    result["log_file_exists"] = LOG_FILE.exists()
    result["log_last_80_lines"] = tail_lines(LOG_FILE, 80)

    result["pid_file"] = str(PID_FILE)
    pid = None
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
    result["pid_from_file"] = pid

    if pid:
        # `ps -p PID -o ...` -- обычный read-only опрос состояния процесса,
        # НЕ влияет на сам процесс (kill -0, который использует workflow,
        # тоже read-only, но ps даёт STAT -- узнать zombie/sleeping/running).
        ps_proc = subprocess.run(["ps", "-p", pid, "-o", "pid,ppid,stat,etime,%cpu,%mem,cmd", "--no-headers"],
                                  capture_output=True, text=True, timeout=10)
        result["ps_returncode"] = ps_proc.returncode
        result["ps_output"] = ps_proc.stdout.strip() or ps_proc.stderr.strip()
        result["process_found_by_ps"] = ps_proc.returncode == 0 and bool(ps_proc.stdout.strip())
        # /proc/<pid>/status -- дополнительно, честная сверка State: (R/S/D/Z/T)
        proc_status_path = Path(f"/proc/{pid}/status")
        if proc_status_path.exists():
            status_lines = proc_status_path.read_text().splitlines()
            state_line = next((l for l in status_lines if l.startswith("State:")), None)
            result["proc_status_state_line"] = state_line
    else:
        result["ps_output"] = None
        result["process_found_by_ps"] = None

    if BUDGET_STATE_FILE.exists():
        budget = json.loads(BUDGET_STATE_FILE.read_text())
        result["budget_state_file"] = str(BUDGET_STATE_FILE)
        result["pilot_started_at"] = budget.get("pilot_started_at")
        result["pilot_completed"] = budget.get("pilot_completed")
        result["pilot_completed_reason"] = budget.get("pilot_completed_reason")
        result["pending"] = budget.get("pending")
        result["cumulative_net_pnl_usd"] = budget.get("cumulative_net_pnl_usd")
        result["halted"] = budget.get("halted")
        result["halt_reason"] = budget.get("halt_reason")
        if budget.get("pilot_started_at"):
            result["elapsed_since_pilot_started_s"] = time.time() - budget["pilot_started_at"]
    else:
        result["budget_state_file_exists"] = False

    last_reason = last_jsonl_ts(REASON_LOG_FILE)
    last_attempt = last_jsonl_ts(ATTEMPT_TABLE_FILE)
    result["last_reason_log_entry"] = last_reason
    result["last_attempt_table_entry"] = last_attempt
    now = time.time()
    if last_reason and last_reason.get("ts_wall"):
        result["seconds_since_last_reason_log_entry"] = now - last_reason["ts_wall"]
    if last_attempt and last_attempt.get("ts_wall"):
        result["seconds_since_last_attempt_table_entry"] = now - last_attempt["ts_wall"]

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
