#!/usr/bin/env python3
"""Проверка записи трендингов: первые появления токенов и неопределённость момента.

Описательно. Правил не подбирает и вердиктов не выносит.

Что считает. По тонкой выгрузке снимков (data/podbivka/trending_slim/*.jsonl.gz, её
делает podbivka_trending_svod.py на бегунке; если её нет -- читает сырые
data/trending/<источник>/<дата>.jsonl.gz):

  * по каждому списку -- сколько РАЗНЫХ адресов впервые появилось за окно;
  * для каждого первого появления -- сколько времени прошло от ПРЕДЫДУЩЕГО снимка того
    же списка: это и есть наша неопределённость момента появления (распределение --
    медиана, p90, максимум и доли по корзинам);
  * заказ для архива: минт -> метка первого появления, список, ранг, ликвидность по
    источнику. Его читает podbivka_trending_arhiv.py.

Выход: data/podbivka/trending_pervye.json (сводка) и
data/podbivka/trending_zakaz_arhiva.json (минты с метками).
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import statistics as st
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
СЛИМ = П / "trending_slim"
СЫРЬЁ = КОРЕНЬ / "data" / "trending"
КОРЗИНЫ = (30, 60, 120, 300, 900, 3600)
# Адрес у GeckoTerminal -- это пул ("solana_<пул>"), у остальных -- минт либо пара;
# в заказ архива идут только похожие на адрес Solana (base58, 32-44 знака).
B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def похож_на_адрес(x) -> bool:
    return isinstance(x, str) and 32 <= len(x) <= 44 and set(x) <= B58


def ключ_архива(адр: str) -> tuple | None:
    """(адрес, вид) для прохода по архиву: «минт» или «пул».

    GeckoTerminal отдаёт в поле id не минт, а пул: `solana_<адрес пула>`. В архиве пул
    лежит в poolId, поэтому вид надо хранить рядом, иначе такие строки просто не найдутся
    (на 12.10 это 2252 адреса из 3579 -- две трети всех первых появлений).
    """
    if not isinstance(адр, str):
        return None
    if адр.startswith("solana_"):
        х = адр.split("_", 1)[1]
        return (х, "пул") if похож_на_адрес(х) else None
    return (адр, "минт") if похож_на_адрес(адр) else None


def мс(utc: str) -> int:
    return int(time.mktime(time.strptime(utc, "%Y-%m-%dT%H:%M:%SZ")) * 1000)


def снимки(дни: tuple):
    """Снимки из тонкой выгрузки, иначе из сырых файлов. Отдаёт (источник, список, utc, строки)."""
    тонкие = sorted(glob.glob(str(СЛИМ / "*.jsonl.gz")))
    файлы = тонкие or sorted(glob.glob(str(СЫРЬЁ / "*" / "*.jsonl.gz")))
    for ф in файлы:
        имя = Path(ф).name
        if дни and not any(д in имя for д in дни):
            continue
        with gzip.open(ф, "rt", encoding="utf-8") as о:
            for ln in о:
                try:
                    с = json.loads(ln)
                except ValueError:
                    continue
                yield (с.get("источник"), с.get("список"), с.get("utc"),
                       с.get("строки") or [], с.get("код"))


def главное(а) -> int:
    дни = tuple(x for x in (а.dni or "").split(",") if x.strip())
    # снимки по списку -> отсортированные метки; первое появление адреса
    метки: dict = collections.defaultdict(list)
    первое: dict = {}
    строк_всего = 0
    for ист, сп, utc, строки, код in снимки(дни):
        if not (ист and сп and utc):
            continue
        ключ = f"{ист}/{сп}"
        метки[ключ].append(utc)
        строк_всего += 1
        if код != 200:
            continue
        for р in строки:
            адр = р.get("адрес")
            if not адр:
                continue
            к = (ключ, адр)
            if к not in первое:
                первое[к] = {"utc": utc, "ранг": р.get("ранг"),
                             "по_порядку": р.get("по_порядку"),
                             "ликвидность": р.get("ликвидность"),
                             "объём": р.get("объём")}
    for к in метки:
        метки[к] = sorted(set(метки[к]))

    # неопределённость: сколько прошло от предыдущего снимка того же списка
    сводка = {}
    заказ = {}
    for ключ, сп_меток in метки.items():
        инд = {u: i for i, u in enumerate(сп_меток)}
        лаги = []
        свои = [(а_, в_) for (к_, а_), в_ in первое.items() if к_ == ключ]
        for адр, в in свои:
            i = инд.get(в["utc"])
            if i is None:
                continue
            if i == 0:
                лаги.append(None)          # первый снимок окна: до него не видно
                continue
            лаги.append((мс(в["utc"]) - мс(сп_меток[i - 1])) / 1000.0)
            ка = ключ_архива(адр)
            if ка:
                стар = заказ.get(ка[0])
                if not стар or в["utc"] < стар["utc"]:
                    заказ[ка[0]] = {"utc": в["utc"], "вид": ка[1],
                                    "список": ключ, "ранг": в["ранг"],
                                    "по_порядку": в["по_порядку"],
                                    "ликвидность_источника": в["ликвидность"],
                                    "неопределённость_с": (
                                        (мс(в["utc"]) - мс(сп_меток[i - 1])) / 1000.0
                                        if i else None)}
        с_числом = [x for x in лаги if x is not None]
        корз = collections.Counter()
        for x in с_числом:
            for гр in КОРЗИНЫ:
                if x <= гр:
                    корз[f"<= {гр} с"] += 1
                    break
            else:
                корз["> 3600 с"] += 1
        сводка[ключ] = {
            "снимков": len(сп_меток), "первый": сп_меток[0], "последний": сп_меток[-1],
            "адресов_впервые": len(свои),
            "на_первом_снимке": sum(1 for x in лаги if x is None),
            "неопределённость_с": {
                "n": len(с_числом),
                "медиана": round(st.median(с_числом), 1) if с_числом else None,
                "p90": (round(sorted(с_числом)[int(0.9 * (len(с_числом) - 1))], 1)
                        if с_числом else None),
                "макс": round(max(с_числом), 1) if с_числом else None,
                "корзины": dict(корз)},
        }

    тело = {"что": "первые появления токенов в списках и неопределённость момента",
            "описательно": "правил не подбирает, вердиктов не выносит",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "источник_снимков": ("тонкая выгрузка" if glob.glob(str(СЛИМ / "*.jsonl.gz"))
                                 else "сырые файлы ветки"),
            "дни": list(дни), "снимков_прочитано": строк_всего,
            "адресов_впервые_всего": len({а_ for _, а_ in первое}),
            "в_заказ_архива": len(заказ),
            "по_спискам": dict(sorted(сводка.items()))}
    (П / f"trending_pervye{а.metka}.json").write_text(
        json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    (П / f"trending_zakaz_arhiva{а.metka}.json").write_text(
        json.dumps({"что": "минты с метками первого появления для прохода по архиву",
                    "когда": тело["когда"], "минтов": len(заказ), "минты": заказ},
                   ensure_ascii=False), encoding="utf-8")
    for к, v in тело["по_спискам"].items():
        print("%-34s снимков %5d впервые %6d неопр. медиана %s с p90 %s макс %s"
              % (к, v["снимков"], v["адресов_впервые"],
                 v["неопределённость_с"]["медиана"], v["неопределённость_с"]["p90"],
                 v["неопределённость_с"]["макс"]), flush=True)
    print(f"адресов впервые всего {тело['адресов_впервые_всего']}, "
          f"в заказ архива {len(заказ)}", flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--dni", default="")
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
