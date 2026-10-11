#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ЗОНД «ВИДЕТЬ РАНЬШЕ» -- НЕСКОЛЬКО СТРОК в журнал прогона, и один признак.

Два дела, и оба нужны, чтобы не держать python внутри YAML (такая вставка уже
дважды ломала разбор файла прогона):
* без ключей -- печатает состояние зонда строками;
* с `--dostignuto` -- КОД ВОЗВРАТА 0, если цель набрана, и 1 иначе. По нему
  прогон решает, пора ли выключать зонд, и это решение становится проверяемым,
  а не «на глаз по выводу».
"""
from __future__ import annotations

import json
import sys


def прочитать(путь: str) -> dict:
    try:
        with open(путь, encoding="utf-8") as ф:
            return json.load(ф)
    except (OSError, ValueError) as сбой:
        return {"why_not": f"признак не прочитан: {type(сбой).__name__}: {сбой}"}


def достигнуто(д: dict) -> bool:
    """Цель набрана: транзакций в ОБОИХ потоках не меньше цели."""
    было = д.get("tranzakcij_v_oboih")
    цель = д.get("cel")
    if not isinstance(было, int) or not isinstance(цель, int) or цель <= 0:
        return False
    return было >= цель


def строки(д: dict) -> list:
    из_ = []
    if д.get("why_not"):
        из_.append(f"zond: {д['why_not']}")
    из_.append(f"zond: tranzakcij v oboih potokah {д.get('tranzakcij_v_oboih')} "
                f"iz {д.get('cel')}; soobshchenij ws {д.get('soobshchenij_ws')}, "
                f"pre {д.get('soobshchenij_pre')}")
    из_.append(f"zond: tolko ws {д.get('podpisej_tolko_ws')}, "
                f"tolko pre {д.get('podpisej_tolko_pre')}")
    из_.append(f"zond: mediana {д.get('mediana_ms')} ms, p90 {д.get('p90_ms')} ms, "
                f"min {д.get('min_ms')}, max {д.get('max_ms')}, "
                f"dolja pre ranshe {д.get('pre_ranshe_dolya')}")
    отказы = д.get("otkazy") or {}
    for имя, причина in отказы.items():
        if причина:
            из_.append(f"zond: OTKAZ potoka {имя}: {причина}")
    из_.append(f"zond: vyvod: {д.get('vyvod')}")
    из_.append(f"zond: cel nabrana: {'da' if достигнуто(д) else 'net'}")
    return из_


def самопроверка() -> int:
    import tempfile  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    проверки = []

    def chk(имя, ок, что=None):
        проверки.append((имя, bool(ок), что))

    полный = {"tranzakcij_v_oboih": 30, "cel": 30, "soobshchenij_ws": 44,
              "soobshchenij_pre": 41, "podpisej_tolko_ws": 3,
              "podpisej_tolko_pre": 1, "mediana_ms": 27.5, "p90_ms": 41.2,
              "min_ms": 4.0, "max_ms": 55.0, "pre_ranshe_dolya": 0.97,
              "otkazy": {"ws": None, "pre": None},
              "vyvod": "preprocessed раньше на 27.5 мс"}
    т = "\n".join(строки(полный))
    chk("строки несут все числа свода", all(з in т for з in
        ("30 iz 30", "27.5", "41.2", "0.97", "44", "41")), т)
    chk("цель набрана -- да", достигнуто(полный) is True)
    chk("цель не набрана при недоборе",
        достигнуто({**полный, "tranzakcij_v_oboih": 29}) is False)
    chk("нет чисел -- цель НЕ считается набранной",
        достигнуто({"tranzakcij_v_oboih": None, "cel": 30}) is False
        and достигнуто({}) is False)
    chk("нулевая цель не объявляется набранной",
        достигнуто({"tranzakcij_v_oboih": 0, "cel": 0}) is False)
    отказ = {**полный, "tranzakcij_v_oboih": 0, "mediana_ms": None,
             "otkazy": {"ws": None, "pre": "план отказал: -32601"}}
    chk("отказ потока попадает в строки отдельной строкой",
        any("OTKAZ potoka pre" in с and "-32601" in с for с in строки(отказ)),
        строки(отказ))
    with tempfile.TemporaryDirectory() as кат:
        chk("нет файла -- причина словами, а не падение",
            "не прочитан" in (прочитать(str(Path(кат) / "net"))["why_not"]),
            прочитать(str(Path(кат) / "net"))["why_not"])
        (Path(кат) / "k.json").write_text("{не json", encoding="utf-8")
        chk("битый признак -- причина словами",
            "не прочитан" in (прочитать(str(Path(кат) / "k.json"))["why_not"]),
            прочитать(str(Path(кат) / "k.json"))["why_not"])
        chk("битый признак целью НЕ считается",
            достигнуто(прочитать(str(Path(кат) / "k.json"))) is False)
    плохих = [(и, ч) for и, ок, ч in проверки if not ок]
    for и, ок, ч in проверки:
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {и}" + ("" if ок else f" -> {ч}"))
    print(f"самопроверка строк зонда: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(самопроверка())
    пути = [а for а in sys.argv[1:] if not а.startswith("--")]
    if not пути:
        print("нужно: preprocessed_stroka.py [--dostignuto] <put>")
        sys.exit(2)
    д = прочитать(пути[0])
    if "--dostignuto" in sys.argv:
        sys.exit(0 if достигнуто(д) else 1)
    for с in строки(д):
        print(с)
