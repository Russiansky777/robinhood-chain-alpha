#!/usr/bin/env python3
"""Одна строка свода сверки с DBot для Telegram.

Отдельным файлом, а не heredoc внутри прогона: heredoc в YAML уже однажды
ломал разбор workflow_dispatch, и GitHub тогда отказывался парсить файл целиком.
"""
from __future__ import annotations

import json
import sys


def строка(d: dict) -> str:
    с = d.get("свод") or {}
    п = d.get("свод_по_причинам") or {}
    хвост = ", ".join(f"{к} {v['n']}" for к, v in list(п.items())[:5]) or "причин нет"
    окно = " - ".join(str(x) for x in (d.get("окно") or ["?", "?"]))
    задачи = ", ".join(d.get("задачи") or []) or "?"
    return (f"Сверка с DBot ({задачи}), {окно}: его сделок {с.get('сделок_dbot')}, "
            f"видели {с.get('из_них_видели')}, купили {с.get('из_них_купили')}; "
            f"наших {с.get('наших_сделок')}, из них DBot тоже взял "
            f"{с.get('из_них_dbot_тоже_взял')}; наш итог {с.get('наш_итог_sol')} SOL, "
            f"трата DBot {с.get('трата_dbot_sol')} SOL. По причинам: {хвост}.")


def main() -> int:
    if len(sys.argv) < 2:
        print("нужен путь к отчёту сверки", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as ф:
        d = json.load(ф)
    print(строка(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
