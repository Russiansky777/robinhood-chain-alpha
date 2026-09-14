#!/usr/bin/env python3
"""Владелец: "переходим к ранее подготовленному независимому поиску
арбитражников" -- task5_true_arbitrageur_scan.py уже написан, с уже
заданными бюджетами (TIME_BUDGET_LOG_SCAN_S=1500с, TIME_BUDGET_TOTAL_S=
2100с) -- ЭТИ лимиты НЕ трогаются, сам скрипт поиска НЕ редактируется.

Проблема: 2100с (35мин) > 30-минутный таймаут GH Actions job, которым
запускаются read-only скрипты этого проекта на Ohio -- прямой синхронный
запуск через существующий workflow будет убит ДО завершения, БЕЗ единого
сохранённого результата (скрипт пишет JSON только в самом конце).

Этот лаунчер -- ТОЛЬКО механизм фонового запуска (setsid + nohup-подобное
отсоединение от SSH-сессии, чтобы истечение 30-минутного workflow-job не
убило процесс) -- сам поиск (алгоритм/критерий/бюджеты) НЕ изменён ни на
байт. Лаунчер завершается почти мгновенно, реальный скрипт продолжает
работать на Ohio в фоне; прогресс/результат проверяются отдельным,
последующим read-only опросом (task5_true_arbitrageur_scan_poll.py)."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
LOG_PATH = Path(__file__).parent.parent / "data" / "task5_true_arbitrageur_scan_launch.log"
PID_PATH = Path(__file__).parent.parent / "data" / "task5_true_arbitrageur_scan_launch.pid"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def main() -> None:
    log_f = open(LOG_PATH, "w")
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "task5_true_arbitrageur_scan.py")],
        stdout=log_f, stderr=subprocess.STDOUT,
        cwd=str(HERE), start_new_session=True,  # отдельная сессия -- переживает завершение SSH/GH Actions job
    )
    PID_PATH.write_text(str(proc.pid))
    print(f"[launch] запущен фоново, pid={proc.pid}, лог={LOG_PATH}, started_at_unix={time.time()}")
    print("[launch] лаунчер завершается немедленно -- реальный скрипт продолжает работу в фоне на Ohio "
          "(собственные бюджеты TIME_BUDGET_LOG_SCAN_S/TIME_BUDGET_TOTAL_S не изменены).")


if __name__ == "__main__":
    main()
