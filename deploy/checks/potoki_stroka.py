#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ПОТОКИ ОДНОГО ПРОЦЕССА -- НЕСКОЛЬКИМИ СТРОКАМИ в журнал прогона.

Нужен ровно для того, чтобы не держать многострочный python внутри YAML: такая
вставка 10.10 ломала разбор файла прогона дважды, и второй раз -- в этом же
файле. Здесь отдельный модуль, который читает свод и печатает строки; битый или
отсутствующий файл он называет словами, а прогон не роняет.
"""
from __future__ import annotations

import json
import sys

СТРОК_ПО_УМОЛЧАНИЮ = 14


def строки(путь: str, имя: str, сколько: int = СТРОК_ПО_УМОЛЧАНИЮ) -> list:
    try:
        with open(путь, encoding="utf-8") as ф:
            д = json.load(ф)
    except (OSError, ValueError) as сбой:
        return [f"{имя}: svod ne prochitan: {type(сбой).__name__}: {сбой}"]
    из_ = [f"{имя}: pid {д.get('pid')}, potokov {д.get('potokov')}, "
            f"zhgut {д.get('zhgut_potokov')} {д.get('zhgut_imena')}",
            f"{имя}: summa {д.get('summa_proc_odnogo_yadra')} % odnogo yadra, "
            f"bez imeni {д.get('bez_imeni_potokov')}, "
            f"okno {д.get('sekund')} s, yader {д.get('yader')}",
            f"{имя}: vyvod: {д.get('vyvod')}"]
    if д.get("why_not"):
        из_.append(f"{имя}: why_not: {д['why_not']}")
    for р in (д.get("ryady") or [])[:max(1, int(сколько))]:
        хвост = f"  <- {р['pochemu']}" if р.get("pochemu") else ""
        из_.append(f"   {str(р.get('proc_odnogo_yadra')):>9} % odnogo yadra  "
                    f"tid {str(р.get('tid')):<8} {р.get('imya')}{хвост}")
    return из_


def самопроверка() -> int:
    import tempfile  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    проверки = []

    def chk(имя, ок, что=None):
        проверки.append((имя, bool(ок), что))

    with tempfile.TemporaryDirectory() as кат:
        п = Path(кат) / "s.json"
        п.write_text(json.dumps({
            "pid": 123, "potokov": 7, "zhgut_potokov": 2,
            "zhgut_imena": ["glavnyj", "kolco"], "sekund": 10.0, "yader": 2,
            "summa_proc_odnogo_yadra": 12.5, "bez_imeni_potokov": 3,
            "vyvod": "жгущих потоков 2: смена шага даёт около 1 мс",
            "ryady": [{"tid": 1, "imya": "glavnyj", "proc_odnogo_yadra": 9.1,
                        "pochemu": None},
                       {"tid": 2, "imya": "novyj", "proc_odnogo_yadra": None,
                        "pochemu": "поток появился внутри окна"}]}),
                      encoding="utf-8")
        с = строки(str(п), "bloom_detector")
        текст = "\n".join(с)
        chk("строки несут pid, число потоков, жгущих и их имена",
            "pid 123" in текст and "potokov 7" in текст
            and "glavnyj" in текст and "kolco" in текст, текст)
        chk("вывод и окно замера в строках есть",
            "vyvod:" in текст and "okno 10.0 s" in текст, текст)
        chk("поток, появившийся в окне, назван причиной, а не нулём",
            "None % odnogo yadra" in текст
            and "внутри окна" in текст, текст)
        chk("нет файла -- строка говорит это, а не падает",
            "ne prochitan" in строки(str(Path(кат) / "net.json"), "x")[0])
        (Path(кат) / "krivoj.json").write_text("{не json", encoding="utf-8")
        chk("битый свод -- строка говорит это",
            "ne prochitan" in строки(str(Path(кат) / "krivoj.json"), "x")[0])
        п2 = Path(кат) / "pusto.json"
        п2.write_text(json.dumps({"why_not": "процесса нет"}), encoding="utf-8")
        chk("нет процесса -- причина попадает в строки отдельно",
            any("why_not: процесса нет" in с for с in строки(str(п2), "x")),
            строки(str(п2), "x"))
        chk("число строк ограничивается -- журнал прогона не заливается",
            len(строки(str(п), "x", сколько=1)) == 4, строки(str(п), "x", 1))
    плохих = [(и, ч) for и, ок, ч in проверки if not ок]
    for и, ок, ч in проверки:
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {и}" + ("" if ок else f" -> {ч}"))
    print(f"самопроверка строк потоков: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(самопроверка())
    if len(sys.argv) < 3:
        print("нужно: potoki_stroka.py <put> <imya>")
        sys.exit(2)
    for с in строки(sys.argv[1], sys.argv[2]):
        print(с)
