#!/usr/bin/env python3
"""II.7 -- ОДИН СЧЁТ ПО ПАРАМ: полоса и Bloom на одном и том же сигнале.

ЗАЧЕМ. 26.09 владелец нашёл расхождение: доклад 00:33Z говорил 33 пары, а
получасовая проверка рубильника -- 31. Оба числа были неверны, и причина одна:
счёт шёл по ВРЕМЕНИ СУТОК строкой ("11:19" >= "20:07:00"), а не по полной
метке. После полуночи такое сравнение отбрасывает все девять покупок 26.09.
Здесь счёт только по полной метке UTC, и это единственное место, откуда
берутся числа по парам -- чтобы "рубильник" и II.7 считали одинаково.

ЧТО СЧИТАЕМ. Вход -- готовая таблица покупок полосы (data/lane_table.md),
которую строит lane_table.py по журналу позиций с хоста (вместе с
ротированными). Ничего не достраиваем: если в строке нет пары Bloom или нет
места в блоке -- строка так и считается, без пары и без места.

ГЕЙТ A ("в цели") -- то же правило, что у lane_table: S+0 в любом месте ИЛИ
S+1 с местом не хуже 100. Применяется к обеим сторонам пары одинаково; для
полосы результат сверяется со столбцом "в цели" самой таблицы, и расхождение
печатается как расхождение, а не заминается.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

ГОЛОВА = "| время UTC"
ПОРОГ_МЕСТА = 100


def строки_таблицы(путь: Path) -> list:
    текст = путь.read_text(encoding="utf-8", errors="replace").split("\n")
    заголовок = [с for с in текст if с.startswith(ГОЛОВА)]
    if not заголовок:
        raise SystemExit(f"СБОЙ: в {путь} нет заголовка таблицы")
    поля = [к.strip() for к in заголовок[0].strip("|").split("|")]
    из_ = []
    for с in текст:
        # Строки данных начинаются с полной метки UTC -- по ней же и фильтруем.
        if not с.startswith("| 2026-"):
            continue
        из_.append(dict(zip(поля, [к.strip() for к in с.strip("|").split("|")])))
    return из_


def место(текст: str):
    м = re.search(r"(\d+)\s*/\s*(\d+)", текст or "")
    return (int(м.group(1)), int(м.group(2))) if м else (None, None)


def сдвиг(текст: str):
    м = re.search(r"S\+(\d+)", текст or "")
    return int(м.group(1)) if м else None


def в_цели(с: int | None, м: int | None) -> bool | None:
    if с is None:
        return None
    if с == 0:
        return True
    if с == 1 and м is not None:
        return м <= ПОРОГ_МЕСТА
    return False


def пары(ряды: list, *, с_utc: str) -> list:
    из_ = []
    for р in ряды:
        когда = р.get("время UTC", "")
        if с_utc and когда < с_utc:
            continue
        наше_место, _ = место(р.get("место в блоке", ""))
        пара = р.get("пара Bloom (S+N, место)", "")
        б_место, _ = место(пара)
        из_.append({
            "utc": когда,
            "группа": р.get("группа"),
            "наш_сдвиг": сдвиг(р.get("S+N", "")),
            "наше_место": наше_место,
            "bloom_сдвиг": сдвиг(пара),
            "bloom_место": б_место,
            "есть_пара": bool(пара) and пара not in ("—", "-", "нет"),
            "в_цели_таблицы": (р.get("в цели", "") or "").startswith("ДА"),
        })
    return из_


def свод(ряд: list) -> dict:
    n = len(ряд)
    наш_ц = [з for з in ряд if в_цели(з["наш_сдвиг"], з["наше_место"])]
    б_ц = [з for з in ряд if в_цели(з["bloom_сдвиг"], з["bloom_место"])]
    расхождение = sum(1 for з in ряд
                      if bool(в_цели(з["наш_сдвиг"], з["наше_место"]))
                      != з["в_цели_таблицы"])
    сдвиги = [з["наш_сдвиг"] - з["bloom_сдвиг"] for з in ряд
              if з["наш_сдвиг"] is not None and з["bloom_сдвиг"] is not None]
    один_слот = [з for з in ряд
                 if з["наш_сдвиг"] is not None
                 and з["наш_сдвиг"] == з["bloom_сдвиг"]
                 and з["наше_место"] is not None and з["bloom_место"] is not None]
    места = [з["наше_место"] - з["bloom_место"] for з in один_слот]
    по_группам: dict = {}
    for з in ряд:
        г = по_группам.setdefault(з["группа"] or "без группы",
                                  {"пар": 0, "полоса_s0": 0, "bloom_s0": 0,
                                   "полоса_в_цели": 0, "bloom_в_цели": 0})
        г["пар"] += 1
        г["полоса_s0"] += int(з["наш_сдвиг"] == 0)
        г["bloom_s0"] += int(з["bloom_сдвиг"] == 0)
        г["полоса_в_цели"] += int(bool(в_цели(з["наш_сдвиг"], з["наше_место"])))
        г["bloom_в_цели"] += int(bool(в_цели(з["bloom_сдвиг"], з["bloom_место"])))
    return {
        "покупок_полосы": n,
        "с_парой_bloom": sum(1 for з in ряд if з["есть_пара"]),
        "полоса_s0": sum(1 for з in ряд if з["наш_сдвиг"] == 0),
        "bloom_s0": sum(1 for з in ряд if з["bloom_сдвиг"] == 0),
        "полоса_в_цели": len(наш_ц),
        "bloom_в_цели": len(б_ц),
        "расхождение_с_таблицей": расхождение,
        "разница_сдвига": ({"n": len(сдвиги),
                            "медиана": statistics.median(сдвиги),
                            "распределение": {str(к): сдвиги.count(к)
                                              for к in sorted(set(сдвиги))}}
                           if сдвиги else None),
        "разница_места_в_общем_слоте": ({"n": len(места),
                                         "медиана": statistics.median(места),
                                         "мин": min(места), "макс": max(места),
                                         "полоса_впереди": sum(1 for x in места if x < 0),
                                         "полоса_позади": sum(1 for x in места if x > 0)}
                                        if места else None),
        "по_группам": по_группам,
    }


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--table", default="data/lane_table.md")
    р.add_argument("--since", default="", help="полная метка UTC, например 2026-09-25T20:07:00Z")
    р.add_argument("--out", default="data/lane_pairs_gate.json")
    а = р.parse_args()

    ряд = пары(строки_таблицы(Path(а.table)), с_utc=а.since)
    с = свод(ряд)
    Path(а.out).parent.mkdir(parents=True, exist_ok=True)
    Path(а.out).write_text(
        json.dumps({"since": а.since, "table": а.table, "сводка": с, "пары": ряд},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(с, ensure_ascii=False, indent=1))
    if с["расхождение_с_таблицей"]:
        print(f"ВНИМАНИЕ: гейт A разошёлся со столбцом таблицы на "
              f"{с['расхождение_с_таблицей']} строках -- разбирать, а не усреднять")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
