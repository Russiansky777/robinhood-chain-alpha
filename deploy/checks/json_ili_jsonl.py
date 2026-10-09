#!/usr/bin/env python3
"""Разбирается ли файл: .json -- целиком, .jsonl -- по строке на объект.

ЗАЧЕМ. Проверка забранных с хоста файлов звала json.load на всё содержимое, а
в списке забора есть журнал тревог trevogi.jsonl -- это JSON Lines, и
json.load падает на второй строке ("Extra data: line 2 column 1"). Прогон
живучести из-за этого краснел при полностью здоровом хосте, и красный становился
обычным делом -- а это хуже отсутствующей проверки.

Проверка НЕ ослаблена: .jsonl разбирается тем же разбором, каким написан, и
падает на ПЕРВОЙ непригодной строке, назвав её номер. Пустые строки пропускаются
(их пишет любая дописывающая служба), а файл без ни одной годной строки --
отказ: "забран пустой журнал" прошло бы молча.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def разобрать(путь: Path) -> dict:
    из_ = {"файл": str(путь), "вид": None, "объектов": 0, "why_not": None}
    if not путь.exists():
        из_["why_not"] = "файла нет"
        return из_
    текст = путь.read_text(encoding="utf-8", errors="replace")
    if путь.suffix == ".jsonl":
        из_["вид"] = "jsonl"
        for н, строка in enumerate(текст.splitlines(), 1):
            if not строка.strip():
                continue
            try:
                json.loads(строка)
            except ValueError as сбой:
                из_["why_not"] = f"строка {н} не json: {сбой}"
                return из_
            из_["объектов"] += 1
        if not из_["объектов"]:
            из_["why_not"] = "ни одной годной строки -- пустой журнал"
        return из_
    из_["вид"] = "json"
    try:
        json.loads(текст)
    except ValueError as сбой:
        из_["why_not"] = f"не json: {сбой}"
        return из_
    из_["объектов"] = 1
    return из_


def самопроверка() -> int:
    import tempfile

    сбоев = всего = 0

    def chk(имя: str, ок: bool, что=None) -> None:
        nonlocal сбоев, всего
        всего += 1
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}")
        if not ок:
            сбоев += 1
            if что is not None:
                print(f"         {что!r}")

    with tempfile.TemporaryDirectory() as д:
        к = Path(д)
        (к / "a.json").write_text('{"x": 1}', encoding="utf-8")
        chk("целый json принимается", разобрать(к / "a.json")["why_not"] is None)
        (к / "b.json").write_text('{"x": 1}\n{"y": 2}\n', encoding="utf-8")
        chk("ДОКАЗАННЫЙ КРАСНЫЙ: две строки в .json -- отказ (ровно так и падал "
            "прогон живучести)", разобрать(к / "b.json")["why_not"] is not None)
        (к / "c.jsonl").write_text('{"x": 1}\n\n{"y": 2}\n', encoding="utf-8")
        р = разобрать(к / "c.jsonl")
        chk("тот же файл как .jsonl принимается, пустая строка пропущена, "
            "объектов два", р["why_not"] is None and р["объектов"] == 2, р)
        (к / "d.jsonl").write_text('{"x": 1}\nне json\n', encoding="utf-8")
        р = разобрать(к / "d.jsonl")
        chk("ДОКАЗАННЫЙ КРАСНЫЙ: битая строка .jsonl названа номером",
            р["why_not"] is not None and "строка 2" in р["why_not"], р)
        (к / "e.jsonl").write_text("\n\n", encoding="utf-8")
        chk("ДОКАЗАННЫЙ КРАСНЫЙ: журнал без годных строк -- отказ, а не зелень",
            разобрать(к / "e.jsonl")["why_not"] is not None)
        chk("ДОКАЗАННЫЙ КРАСНЫЙ: файла нет -- отказ словами",
            разобрать(к / "нет.json")["why_not"] == "файла нет")
    print(f"самопроверка json_ili_jsonl: {всего - сбоев}/{всего} пройдено")
    return 1 if сбоев else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(самопроверка())
    пути = [а for а in sys.argv[1:] if not а.startswith("--")]
    if not пути:
        print("нужен путь к файлу .json или .jsonl", file=sys.stderr)
        raise SystemExit(2)
    код = 0
    for п in пути:
        р = разобрать(Path(п))
        if р["why_not"]:
            print(f"{п}: {р['why_not']}", file=sys.stderr)
            код = 1
        else:
            print(f"{п}: {р['вид']}, объектов {р['объектов']}")
    raise SystemExit(код)
