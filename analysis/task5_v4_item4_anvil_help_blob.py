#!/usr/bin/env python3
"""Задача 5, восьмой раунд (пункт 4): быстрая, дешёвая проверка --
есть ли у ЭТОЙ версии anvil (1.8.1) отдельный флаг для управления
excess-blob-gas/blob-base-fee окружения форка, вместо смены hardfork'а
целиком (та смена либо ломает TSTORE/TLOAD (V4 flash-accounting), либо
не устраняет "Excess blob gas not set" -- см. task5_v4_item4_
insufficientfunds_trace.py, история коммитов). Просто печатает --help
целиком и grep по 'blob' -- ничего не запускает, не форкает."""
import subprocess
from pathlib import Path

FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
ANVIL = str(FOUNDRY_BIN / "anvil")

if __name__ == "__main__":
    proc = subprocess.run([ANVIL, "--help"], capture_output=True, text=True, timeout=20)
    full = proc.stdout + proc.stderr
    print("=== ПОЛНЫЙ --help (для протокола) ===")
    print(full)
    print("=== СТРОКИ, СОДЕРЖАЩИЕ 'blob' (регистронезависимо) ===")
    for line in full.splitlines():
        if "blob" in line.lower():
            print(line)
