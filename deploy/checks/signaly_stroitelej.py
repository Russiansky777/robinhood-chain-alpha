#!/usr/bin/env python3
"""Сколько сигналов по каждому строителю пришло за сутки -- ПО ГРУППАМ, числом.

ЗАЧЕМ. Очередь строителей владельца: LaunchLab -> DLMM -> Whirlpool -> AMM v4,
и каждый включается до первой живой сделки. Вопрос "сколько ждать первую сделку
LaunchLab" требует числа, а не впечатления: сколько сигналов по пулам этого
строителя прошло за сутки и по каким группам. Тем же счётом виден и запас по
следующим строителям -- стоит ли их вообще включать.

Считается по журналу решений ПОТОКОМ: адрес программы строителя ищется в строке
подстрокой до разбора JSON (это секунды вместо минут на файле в сотни
мегабайт), а группа берётся из самой записи. Строки теневого замера считаются
ОТДЕЛЬНО от решений по торгующим группам: смешивать их нельзя -- тень не
торгует.

Только чтение: журнал решений. Ни цепи, ни подписи, ни отправки.
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

# Адреса строителей -> имена владельца. Те же, что в c2_swap_build.
СТРОИТЕЛИ = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "bonding",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pump_amm",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "cpmm",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "damm2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "dbc",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "clmm",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "launchlab",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "dlmm",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "whirlpool",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUtu": "amm_v4",
}
# Группы, которые ТОРГУЮТ полосой. Остальные (log_only, off, kandidaty) считаются
# отдельно: сигнал по ним не станет сделкой, сколько его ни ждать.
ТОРГУЮЩИЕ = ("lane_s0", "batch5", "sniper_src", "leader")


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    try:
        with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
            for с in ф:
                с = с.strip()
                if с:
                    yield с
    except OSError:
        return


def момент(текст: str) -> float:
    т = (текст or "").strip()
    if not т:
        return time.time() - 86400.0
    if т.startswith("-") and т.endswith(("h", "m")):
        ч = float(т[1:-1])
        return time.time() - ч * (3600.0 if т.endswith("h") else 60.0)
    for формат in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return float(calendar.timegm(time.strptime(т, формат)))
        except ValueError:
            continue
    return time.time() - 86400.0


def группа_записи(з: dict) -> str:
    for поле in ("lane_group", "group", "source_task"):
        зн = з.get(поле)
        if isinstance(зн, str) and зн:
            return зн
    return "группы в записи нет"


def посчитать(журнал_каталог: str, с_ts: float) -> dict:
    итог = {"с": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(с_ts)),
             "строк_просмотрено": 0, "записей_со_строителем": 0,
             "по_строителям": {}, "только_чтение": True, "why_not": None}
    пути = sorted(glob.glob(os.path.join(журнал_каталог, "decisions.jsonl*")))
    if not пути:
        итог["why_not"] = f"журнала решений нет: {журнал_каталог}"
        return итог
    for путь in пути:
        # Архив старше окна не разжимаем: журнал идёт на сотни мегабайт.
        try:
            if os.path.getmtime(путь) < с_ts - 86400:
                continue
        except OSError:
            pass
        for с in строки(путь):
            итог["строк_просмотрено"] += 1
            # ДЕШЁВЫЙ ОТБОР: есть ли в строке вообще адрес какого-то строителя.
            адрес = None
            for а in СТРОИТЕЛИ:
                if а in с:
                    адрес = а
                    break
            if адрес is None:
                continue
            try:
                з = json.loads(с)
            except ValueError:
                continue
            if not isinstance(з, dict):
                continue
            т = з.get("t_recv_ts")
            if isinstance(т, (int, float)) and float(т) < с_ts:
                continue
            итог["записей_со_строителем"] += 1
            имя = СТРОИТЕЛИ[адрес]
            св = итог["по_строителям"].setdefault(
                имя, {"всего": 0, "по_группам": {}, "торгующих_групп": 0,
                       "стадии": {}, "без_времени": 0})
            св["всего"] += 1
            if not isinstance(т, (int, float)):
                св["без_времени"] += 1
            гр = группа_записи(з)
            св["по_группам"][гр] = int(св["по_группам"].get(гр, 0)) + 1
            if гр in ТОРГУЮЩИЕ:
                св["торгующих_групп"] += 1
            ст = str(з.get("stage") or з.get("action") or "?")[:24]
            св["стадии"][ст] = int(св["стадии"].get(ст, 0)) + 1
    for св in итог["по_строителям"].values():
        св["по_группам"] = dict(sorted(св["по_группам"].items(),
                                        key=lambda т_: -т_[1])[:12])
        св["стадии"] = dict(sorted(св["стадии"].items(), key=lambda т_: -т_[1])[:8])
    итог["по_строителям"] = dict(sorted(итог["по_строителям"].items(),
                                         key=lambda т_: -т_[1]["всего"]))
    return итог


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--since", default="", help="'2026-09-28T00:21:00Z', '-24h' или пусто")
    р.add_argument("--out", default="")
    р.add_argument("--self-test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return самопроверка()
    итог = посчитать(а.state_dir, момент(а.since))
    print(json.dumps(итог, ensure_ascii=False, indent=1)[:6000])
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
    return 0


def самопроверка() -> int:
    сбоев = всего = 0

    def chk(имя: str, ок: bool, что=None) -> None:
        nonlocal сбоев, всего
        всего += 1
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}")
        if not ок:
            сбоев += 1
            if что is not None:
                print(f"         {что!r}")

    import tempfile  # noqa: PLC0415

    ЛЛ = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
    with tempfile.TemporaryDirectory() as d:
        с_ = os.path.join(d, "decisions.jsonl")
        with open(с_, "w", encoding="utf-8") as ф:
            ф.write(json.dumps({"t_recv_ts": 2000.0, "stage": "lane_gate",
                                 "lane_group": "batch5", "pool_program": ЛЛ},
                                ensure_ascii=False) + "\n")
            ф.write(json.dumps({"t_recv_ts": 2001.0, "stage": "shadow",
                                 "pool_program": ЛЛ}, ensure_ascii=False) + "\n")
            ф.write(json.dumps({"t_recv_ts": 500.0, "stage": "lane_gate",
                                 "lane_group": "batch5", "pool_program": ЛЛ},
                                ensure_ascii=False) + "\n")
            ф.write(json.dumps({"t_recv_ts": 2002.0, "stage": "lane_gate",
                                 "lane_group": "log_only"}, ensure_ascii=False) + "\n")
        о = посчитать(d, 1000.0)
        лл = о["по_строителям"].get("launchlab") or {}
        chk(f"строитель найден по адресу, записей {лл.get('всего')}",
            лл.get("всего") == 2, о)
        chk(f"торгующих групп {лл.get('торгующих_групп')} из них",
            лл.get("торгующих_групп") == 1, лл)
        chk("тень считается отдельной стадией, а не группой",
            лл.get("стадии", {}).get("shadow") == 1, лл.get("стадии"))
        chk("запись вне окна не взята", "500" not in json.dumps(о), о)
        chk("строка без адреса строителя не считается",
            "log_only" not in json.dumps(о, ensure_ascii=False), о)
        chk("время UTC разбирается как UTC",
            момент("2026-09-28T00:21:00Z") == 1790554860.0,
            момент("2026-09-28T00:21:00Z"))
    print(f"самопроверка счёта сигналов по строителям: {всего - сбоев}/{всего} пройдено")
    return 1 if сбоев else 0


if __name__ == "__main__":
    sys.exit(main())
