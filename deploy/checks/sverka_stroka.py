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
    # ЧЬЯ ЭТО ТРАТА. Крупная сумма без имени кошелька читается как ошибка
    # счёта: 26.09 весь свод сделал один кошелёк 19 покупками одного минта за
    # 25 секунд. Поэтому в строке сразу назван самый крупный плательщик.
    по_к = d.get("трата_по_кошелькам_sol") or {}
    чей = ""
    if по_к:
        к1, с1 = list(по_к.items())[0]
        if float(с1 or 0) > 0:
            чей = f" (из них {round(float(с1), 3)} -- кошелёк {к1[:8]})"
    стейблы = с.get("трата_dbot_в_стейблах") or {}
    стб = ("; в стейблах " + ", ".join(f"{round(float(v), 2)} {к[:4]}"
                                        for к, v in стейблы.items())
           if стейблы else "")
    return (f"Сверка с DBot ({задачи}), {окно}: его сделок {с.get('сделок_dbot')}, "
            f"видели {с.get('из_них_видели')}, купили {с.get('из_них_купили')}; "
            f"наших {с.get('наших_сделок')}, из них DBot тоже взял "
            f"{с.get('из_них_dbot_тоже_взял')}; наш итог {с.get('наш_итог_sol')} SOL, "
            f"трата DBot {с.get('трата_dbot_sol')} SOL{чей}{стб}. "
            f"По причинам: {хвост}.")


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
