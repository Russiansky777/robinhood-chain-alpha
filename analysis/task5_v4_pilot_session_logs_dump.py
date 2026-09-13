#!/usr/bin/env python3
"""ЧИСТО ЧИТАЮЩИЙ скрипт: печатает ПОЛНОЕ, построчное содержимое двух
файлов-срезов ТОЛЬКО этой торговой сессии (созданных
task5_v4_pilot_session_stats.py) -- для передачи владельцу как "полные
журналы текущей сессии". Ничего не пишет, новых файлов не создаёт,
новых RPC/скана/пилота не запускает."""
from pathlib import Path

REASON = Path("/home/bot/data/task5_v4_pilot_session_only_no_send_log.jsonl")
ATTEMPTS = Path("/home/bot/data/task5_v4_pilot_session_only_attempts.jsonl")


def main() -> None:
    print("=== BEGIN task5_v4_pilot_session_only_no_send_log.jsonl ===")
    if REASON.exists():
        print(REASON.read_text(), end="")
    else:
        print(f"(файл не найден: {REASON})")
    print("=== END task5_v4_pilot_session_only_no_send_log.jsonl ===")
    print("=== BEGIN task5_v4_pilot_session_only_attempts.jsonl ===")
    if ATTEMPTS.exists():
        print(ATTEMPTS.read_text(), end="")
    else:
        print(f"(файл не найден: {ATTEMPTS})")
    print("=== END task5_v4_pilot_session_only_attempts.jsonl ===")


if __name__ == "__main__":
    main()
