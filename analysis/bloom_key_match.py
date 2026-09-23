#!/usr/bin/env python3
"""Тот ли ключ: публичный ключ из секрета против кошелька исполнителя.

Отдельный файл, а не heredoc в шаге прогона: вложенный heredoc внутри
"run: |" уже ломался -- закрывающая метка идёт с отступом, и оболочка её не
узнаёт. Печатается только публичный ключ; сам секрет не выводится нигде,
даже в тексте ошибки.

Код возврата: 0 -- ключ принадлежит кошельку исполнителя, 1 -- нет.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_exec_state as ST  # noqa: E402
import bloom_jupiter_sell as J  # noqa: E402


def main() -> int:
    r = J.ключ_от_нашего_кошелька(ST.EXECUTOR_WALLET)
    print(f"публичный ключ из секрета: {r.get('pubkey')}")
    print(f"кошелёк исполнителя:       {ST.EXECUTOR_WALLET}")
    if not r.get("ok"):
        print(f"СБОЙ: {r.get('why_not')}")
        return 1
    print("ключ принадлежит кошельку исполнителя -- путь можно включать")
    return 0


if __name__ == "__main__":
    sys.exit(main())
