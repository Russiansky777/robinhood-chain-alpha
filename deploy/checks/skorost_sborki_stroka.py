#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ОДНА СТРОКА ПРО ОДНУ МАШИНУ -- чтобы в журнале прогона было видно сразу.

Нужен ровно для того, чтобы не держать многострочный python внутри YAML: такая
вставка 10.10 уже сломала разбор файла прогона. Здесь -- отдельный модуль,
который читает свод одной машины и печатает одну строку. Если свода нет или он
битый, строка так и говорит, а прогон не падает: одна недоступная машина не
должна уносить три остальные.
"""
from __future__ import annotations

import json
import sys


def строка(путь: str, имя: str) -> str:
    try:
        with open(путь, encoding="utf-8") as ф:
            д = json.load(ф)
    except (OSError, ValueError) as сбой:
        return f"{имя}: svod ne prochitan: {type(сбой).__name__}: {сбой}"
    сб = д.get("sborka") or {}
    ж = (д.get("zhelezo") or {}).get("processor") or {}
    пт = ((д.get("zhelezo") or {}).get("python") or {}).get("versiya")
    пол = (сб.get("polnaya_ms") or {}).get("med")
    заг = (сб.get("iz_zagotovki_ms") or {}).get("med")
    эт = ((д.get("etalon") or {}).get("cikl_python") or {}).get("med")
    return (f"{имя}: polnaya {пол} ms, iz zagotovki {заг} ms, "
            f"etalon cikl {эт} ms, python {пт}, "
            f"cpu {ж.get('model')} ({ж.get('yader')} yad.), "
            f"why_not {сб.get('why_not')}")


def самопроверка() -> int:
    import tempfile  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    проверки = []

    def chk(имя, ок, что=None):
        проверки.append((имя, bool(ок), что))

    with tempfile.TemporaryDirectory() as кат:
        п = Path(кат) / "s.json"
        п.write_text(json.dumps({
            "zhelezo": {"processor": {"model": "CPU X", "yader": 2},
                         "python": {"versiya": "3.11.0"}},
            "etalon": {"cikl_python": {"med": 9.0}},
            "sborka": {"polnaya_ms": {"med": 0.7},
                        "iz_zagotovki_ms": {"med": 0.17}, "why_not": None}}),
                      encoding="utf-8")
        с = строка(str(п), "kontejner")
        chk("строка несёт оба числа, эталон, питон и процессор",
            all(т in с for т in ("0.7", "0.17", "9.0", "3.11.0", "CPU X")), с)
        chk("нет файла -- строка говорит это, а не падает",
            "ne prochitan" in строка(str(Path(кат) / "net.json"), "ams"))
        (Path(кат) / "krivoj.json").write_text("{не json", encoding="utf-8")
        chk("битый свод -- строка говорит это",
            "ne prochitan" in строка(str(Path(кат) / "krivoj.json"), "miami"))
        п2 = Path(кат) / "bez.json"
        п2.write_text(json.dumps({"sborka": {"why_not": "solders net"}}),
                       encoding="utf-8")
        chk("машина без числа -- в строке стоит причина, а не ноль",
            "solders net" in строка(str(п2), "miami")
            and "polnaya None" in строка(str(п2), "miami"), строка(str(п2), "miami"))
    плохих = [(и, ч) for и, ок, ч in проверки if not ок]
    for и, ок, ч in проверки:
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {и}" + ("" if ок else f" -> {ч}"))
    print(f"самопроверка строки машины: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(самопроверка())
    if len(sys.argv) < 3:
        print("нужно: skorost_sborki_stroka.py <put> <imya>")
        sys.exit(2)
    print(строка(sys.argv[1], sys.argv[2]))
