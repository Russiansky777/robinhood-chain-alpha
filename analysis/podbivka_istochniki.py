#!/usr/bin/env python3
"""Подбивка, п.2: список НАШИХ источников -- все адреса, включая снятые.

Откуда (только файлы репозитория, без сети):
  * живая конфигурация групп      data/sources_2026-09-25.json (groups.*.addresses,
    speed_only.snipers, а также снятые: by_signal, dropped_by_credits);
  * снимок задач DBot              data/final/20260923T145755Z/konfig.json (targetIds);
  * журнал сделок DBot             data/solana_trades_all.json (source_address) --
    туда попадают и источники снятых задач.
Группа для п.5: speed_only / BATCH-3-5 / остальные.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--konfig", default=str(КОРЕНЬ / "data" / "sources_2026-09-25.json"))
    р.add_argument("--snimok", default=str(КОРЕНЬ / "data" / "final" / "20260923T145755Z" / "konfig.json"))
    р.add_argument("--ledger", default=str(КОРЕНЬ / "data" / "solana_trades_all.json"))
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "podbivka" / "istochniki.json"))
    а = р.parse_args()
    ист: dict = {}

    def добавить(адрес: str, откуда: str, **поля) -> None:
        if not адрес:
            return
        з = ист.setdefault(адрес, {"address": адрес, "откуда": [], "задачи": [], "группы": [],
                                   "снят": [], "сделок_в_журнале": 0})
        if откуда not in з["откуда"]:
            з["откуда"].append(откуда)
        for к, v in поля.items():
            if isinstance(з.get(к), list):
                if v not in з[к]:
                    з[к].append(v)
            else:
                з[к] = v

    кф = json.loads(Path(а.konfig).read_text(encoding="utf-8"))
    for имя, гр in (кф.get("groups") or {}).items():
        for адрес in гр.get("addresses") or []:
            добавить(адрес, "живая конфигурация", группы=имя)
        for x in гр.get("snipers") or []:
            добавить(x.get("address"), "живая конфигурация", группы=имя)
        for x in гр.get("by_signal") or []:
            добавить(x.get("address"), "живая конфигурация (снят)", группы=имя,
                     снят=x.get("why") or "by_signal")
        for x in гр.get("dropped_by_credits") or []:
            добавить(x.get("address"), "живая конфигурация (снят)", группы=имя,
                     снят="dropped_by_credits: " + (x.get("why") or ""))
    сн = json.loads(Path(а.snimok).read_text(encoding="utf-8"))
    for т in (сн.get("тело") or {}).get("res") or []:
        for адрес in т.get("targetIds") or []:
            добавить(адрес, "снимок задач 23.09", задачи=т.get("name"))
            if not т.get("enabled"):
                добавить(адрес, "снимок задач 23.09", снят=f"задача {т.get('name')} выключена")
    for с in json.loads(Path(а.ledger).read_text(encoding="utf-8")):
        адрес = с.get("source_address")
        if not адрес:
            continue
        добавить(адрес, "журнал сделок DBot", задачи=с.get("task_name"))
        ист[адрес]["сделок_в_журнале"] += 1
    for з in ист.values():
        живой = "живая конфигурация" in з["откуда"] or "снимок задач 23.09" in з["откуда"]
        if not живой and "журнал сделок DBot" in з["откуда"]:
            з["снят"].append("есть только в журнале сделок: задача снята до снимка 23.09")
        if "speed_only" in з["группы"]:
            з["группа_п5"] = "speed_only"
        elif any(t in ("BATCH-3", "BATCH-5") for t in з["задачи"]):
            з["группа_п5"] = "BATCH-3-5"
        else:
            з["группа_п5"] = "остальные"
    список = sorted(ист.values(), key=lambda з: (-з["сделок_в_журнале"], з["address"]))
    свод = {"источников": len(список),
            "по_группе_п5": {г: sum(1 for з in список if з["группа_п5"] == г)
                             for г in ("speed_only", "BATCH-3-5", "остальные")},
            "снятых": sum(1 for з in список if з["снят"]),
            "по_откуда": {о: sum(1 for з in список if о in з["откуда"])
                          for о in ("живая конфигурация", "живая конфигурация (снят)",
                                    "снимок задач 23.09", "журнал сделок DBot")}}
    Path(а.out).write_text(json.dumps({"свод": свод, "источники": список}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print("источники п.2:", json.dumps(свод, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
