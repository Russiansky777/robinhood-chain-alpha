#!/usr/bin/env python3
"""Распределение времени ответа Bloom с заданной метки (только чтение).

ЗАЧЕМ. Пункт 4 ночного плана владельца 26.09: "распределение времени ответа
Bloom с 11:33Z (медиана, p90, > 1 с)". 11:33Z -- метка, с которой площадка
работает на боевом ключе после правки; числа до неё в счёт не идут.

ОТКУДА ЧИСЛО. Поле bloom_ms позиции: его ставит analysis/bloom_api.py в момент
получения ответа Bloom (от отправки запроса до ответа), и детектор переносит
его в позицию и в журнал решений. Ничего не пересчитывается заново: пересчёт
по меткам времени дал бы другой отрезок (в него вошла бы и наша сборка).

ЖУРНАЛЫ ЧИТАЮТСЯ ПОТОКОМ, строка за строкой (слово владельца: "журналы больше
не читать в память"), включая ротированные .gz. Ни ключей, ни сделок.
"""
from __future__ import annotations

import argparse
import calendar
import glob
import gzip
import json
import os
import sys
import time

ПОРОГ_СЕКУНДА_МС = 1000.0


def метка_в_секунды(текст: str) -> float:
    """'2026-09-26T11:33:00Z' -> секунды UTC. Только timegm: хост живёт в CEST,
    и mktime сдвинул бы окно на два часа."""
    т = (текст or "").strip().replace("Z", "").replace("z", "")
    return calendar.timegm(time.strptime(т, "%Y-%m-%dT%H:%M:%S"))


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    try:
        with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
            for стр in ф:
                стр = стр.strip()
                if стр:
                    yield стр
    except OSError as exc:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {путь} не прочитан ({type(exc).__name__})",
              file=sys.stderr)


def файлы(каталог: str, имя: str) -> list:
    """Сам журнал и его ротированные копии -- по возрастанию имени."""
    из_ = sorted(glob.glob(os.path.join(каталог, имя + "*")))
    основной = os.path.join(каталог, имя)
    if основной in из_:
        из_.remove(основной)
        из_.append(основной)
    return из_


def время_записи(з: dict) -> float | None:
    """Метка записи в секундах UTC. Порядок полей -- от точного к грубому."""
    for поле in ("ts_bloom_resp", "ts_accepted", "ts_sent", "ts_intent", "ts"):
        зн = з.get(поле)
        if isinstance(зн, (int, float)) and зн > 1_600_000_000:
            return float(зн)
    текст = з.get("ts_utc") or з.get("utc")
    if isinstance(текст, str) and текст.endswith("Z"):
        try:
            return метка_в_секунды(текст)
        except ValueError:
            return None
    return None


def квантиль(значения: list, доля: float) -> float | None:
    """Ближайший ранг, без интерполяции: числа и так в миллисекундах."""
    if not значения:
        return None
    з = sorted(значения)
    и = min(len(з) - 1, max(0, int(round(доля * (len(з) - 1)))))
    return з[и]


def собрать(каталог: str, с_метки: float) -> dict:
    """{ключ позиции: bloom_ms} по всем журналам, дубли по cid не двоятся."""
    по_cid: dict = {}
    вне_окна = 0
    без_числа = 0
    просмотрено = 0
    источники = {}
    for имя in ("positions.jsonl", "decisions.jsonl"):
        for путь in файлы(каталог, имя):
            в_файле = 0
            for стр in строки(путь):
                просмотрено += 1
                try:
                    з = json.loads(стр)
                except ValueError:
                    continue
                if not isinstance(з, dict):
                    continue
                мс = з.get("bloom_ms")
                if not isinstance(мс, (int, float)):
                    без_числа += 1
                    continue
                т = время_записи(з)
                if т is None or т < с_метки:
                    вне_окна += 1
                    continue
                cid = (з.get("client_order_id") or з.get("cid")
                       or з.get("signature") or f"{путь}:{просмотрено}")
                # Позиция точнее решения: у неё bloom_ms уже перенесён и не
                # перезаписывается повтором. Первое встреченное значение и
                # остаётся -- второе для того же cid было бы тем же числом.
                if cid not in по_cid:
                    по_cid[cid] = float(мс)
                    в_файле += 1
            if в_файле:
                источники[os.path.basename(путь)] = в_файле
    return {"по_cid": по_cid, "вне_окна": вне_окна, "без_числа": без_числа,
            "просмотрено": просмотрено, "источники": источники}


def доклад(каталог: str, с_текст: str) -> dict:
    с_метки = метка_в_секунды(с_текст)
    с = собрать(каталог, с_метки)
    з = list(с["по_cid"].values())
    медленных = [м for м in з if м > ПОРОГ_СЕКУНДА_МС]
    ведра = {"<=200": 0, "200-400": 0, "400-700": 0, "700-1000": 0,
             "1000-2000": 0, ">2000": 0}
    for м in з:
        if м <= 200:
            ведра["<=200"] += 1
        elif м <= 400:
            ведра["200-400"] += 1
        elif м <= 700:
            ведра["400-700"] += 1
        elif м <= 1000:
            ведра["700-1000"] += 1
        elif м <= 2000:
            ведра["1000-2000"] += 1
        else:
            ведра[">2000"] += 1
    return {
        "с": с_текст,
        "ответов": len(з),
        "медиана_мс": квантиль(з, 0.5),
        "p90_мс": квантиль(з, 0.9),
        "p99_мс": квантиль(з, 0.99),
        "минимум_мс": min(з) if з else None,
        "максимум_мс": max(з) if з else None,
        "больше_секунды": len(медленных),
        "доля_больше_секунды": (round(len(медленных) / len(з), 4) if з else None),
        "ведра_мс": ведра,
        "строк_просмотрено": с["просмотрено"],
        "записей_вне_окна": с["вне_окна"],
        "записей_без_bloom_ms": с["без_числа"],
        "по_журналам": с["источники"],
    }


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--s", default="2026-09-26T11:33:00Z",
                   help="метка UTC, с которой считать")
    р.add_argument("--out", default="")
    а = р.parse_args()
    д = доклад(а.state_dir, а.s)
    текст = json.dumps(д, ensure_ascii=False, indent=1)
    print(текст)
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            ф.write(текст + "\n")
    if not д["ответов"]:
        print("ЧИСЕЛ НЕТ: ни одной записи с bloom_ms в окне -- доклад пуст",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
