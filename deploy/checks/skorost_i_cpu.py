#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Скорость полосы до и после границы + чем занят процессор хоста.

ЗАЧЕМ (слово владельца 28.09): "«увидели» и «решили» p50/p90 по сделкам до
18:00Z и после; загрузка CPU хоста и доля зонда преконфов и сторожа в ней. Если
зонд стоит детектору > 10 мс -- остановить его до утра и сказать".

Что берётся и откуда, без досчёта:
  * увидели -- поле seen_lag_ms позиции (оценка по слотам);
  * решили  -- signal_recv_ts -> ts_intent;
  * собрали -- ts_intent -> ts_sent;
  * процессор -- utime+stime из /proc/<pid>/stat на двух замерах и общее время
    из /proc/stat: доля каждого процесса считается от ОДНОГО ядра и от всей
    машины отдельно, иначе "50 %" ничего не значит.

Только чтение: журнал позиций читается потоком, сеть не нужна вовсе.
"""

from __future__ import annotations

import argparse
import calendar
import glob
import gzip
import json
import os
import time
from pathlib import Path


def разобрать_время(строка: str) -> float | None:
    if not строка:
        return None
    т = строка.strip().replace("Z", "")
    for вид in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                 "%Y-%m-%d"):
        try:
            return float(calendar.timegm(time.strptime(т, вид)))
        except ValueError:
            continue
    return None


def строки(путь: str):
    открыть = gzip.open if путь.endswith(".gz") else open
    try:
        ф = открыть(путь, "rt", encoding="utf-8")
    except Exception:  # noqa: BLE001
        return
    with ф:
        for строка in ф:
            строка = строка.strip()
            if not строка:
                continue
            try:
                yield json.loads(строка)
            except ValueError:
                continue


def позиции(state_dir: str):
    пути = sorted(glob.glob(os.path.join(state_dir, "positions*.jsonl*")))
    видели: dict = {}
    for п in пути:
        for зап in строки(п):
            cid = зап.get("client_order_id") or зап.get("cid")
            if not cid:
                continue
            было = видели.get(cid) or {}
            было.update({к: v for к, v in зап.items() if v is not None})
            видели[cid] = было
    return list(видели.values())


def процентиль(ряд: list, доля: float):
    ч = sorted(x for x in ряд if isinstance(x, (int, float)))
    if not ч:
        return None
    место = min(len(ч) - 1, max(0, int(round(доля * (len(ч) - 1)))))
    return round(float(ч[место]), 1)


def мс(a, b):
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    if not a or not b:
        return None
    return round((float(b) - float(a)) * 1000.0, 1)


def окно(поз_ряд: list, *, с: float, по: float) -> dict:
    увидели, решили, собрали = [], [], []
    сделок = 0
    for п in поз_ряд:
        тс = п.get("ts_intent")
        if not isinstance(тс, (int, float)) or not (с <= float(тс) < по):
            continue
        сделок += 1
        if isinstance(п.get("seen_lag_ms"), (int, float)):
            увидели.append(float(п["seen_lag_ms"]))
        р = мс(п.get("signal_recv_ts"), п.get("ts_intent"))
        if р is not None:
            решили.append(р)
        сб = мс(п.get("ts_intent"), п.get("ts_sent"))
        if сб is not None:
            собрали.append(сб)
    из_ = {"сделок": сделок}
    for имя, ряд in (("увидели", увидели), ("решили", решили),
                      ("собрали", собрали)):
        из_[имя] = {"n": len(ряд), "p50": процентиль(ряд, 0.5),
                     "p90": процентиль(ряд, 0.9),
                     "min": (round(min(ряд), 1) if ряд else None),
                     "max": (round(max(ряд), 1) if ряд else None)}
    return из_


# ----------------------------------------------------------------- процессор

def такты_в_секунде() -> float:
    try:
        return float(os.sysconf("SC_CLK_TCK")) or 100.0
    except Exception:  # noqa: BLE001
        return 100.0


def ядер() -> int:
    try:
        return os.cpu_count() or 1
    except Exception:  # noqa: BLE001
        return 1


def процессы(имена: list) -> dict:
    """{pid: (имя, командная строка)} по подстрокам имён."""
    из_ = {}
    for путь in glob.glob("/proc/[0-9]*"):
        pid = os.path.basename(путь)
        try:
            cmd = Path(путь, "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            continue
        if not cmd:
            continue
        for имя in имена:
            if имя in cmd:
                из_[pid] = (имя, cmd[:160])
                break
    return из_


def такты_процесса(pid: str):
    try:
        поля = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[-1].split()
    except Exception:  # noqa: BLE001
        return None
    try:
        # После ") " поле 1 -- state, utime это 12-е, stime 13-е (нумерация с 1).
        utime, stime = float(поля[11]), float(поля[12])
    except Exception:  # noqa: BLE001
        return None
    return utime + stime


def такты_машины():
    try:
        первая = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    except Exception:  # noqa: BLE001
        return None, None
    ч = [float(x) for x in первая.split()[1:]]
    всего = sum(ч)
    простой = ч[3] + (ч[4] if len(ч) > 4 else 0.0)
    return всего, простой


def загрузка(имена: list, *, секунд: float) -> dict:
    найдены = процессы(имена)
    т0 = {pid: такты_процесса(pid) for pid in найдены}
    м0, п0 = такты_машины()
    time.sleep(секунд)
    т1 = {pid: такты_процесса(pid) for pid in найдены}
    м1, п1 = такты_машины()
    тк = такты_в_секунде()
    к = ядер()
    из_ = {"секунд": секунд, "ядер": к, "процессы": [], "машина": None}
    if м0 is not None and м1 is not None and м1 > м0:
        занято = (м1 - м0) - (п1 - п0)
        из_["машина"] = {"занято_проц": round(100.0 * занято / (м1 - м0), 1),
                          "простой_проц": round(100.0 * (п1 - п0) / (м1 - м0), 1)}
    try:
        из_["load_avg"] = [round(x, 2) for x in os.getloadavg()]
    except Exception:  # noqa: BLE001
        из_["load_avg"] = None
    for pid, (имя, cmd) in sorted(найдены.items()):
        а, б = т0.get(pid), т1.get(pid)
        if а is None or б is None:
            continue
        ядро_проц = 100.0 * (б - а) / тк / секунд
        из_["процессы"].append({
            "pid": int(pid), "имя": имя, "команда": cmd,
            "проц_одного_ядра": round(ядро_проц, 1),
            "проц_всей_машины": round(ядро_проц / max(1, к), 1)})
    из_["процессы"].sort(key=lambda x: -x["проц_одного_ядра"])
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--s", default="", help="начало окна (пусто -- 00:00 суток)")
    р.add_argument("--granica", default="2026-09-28T18:00:00Z")
    р.add_argument("--po", default="", help="конец (пусто -- сейчас)")
    р.add_argument("--cpu-sekund", type=float, default=300.0)
    р.add_argument("--imena", default=("bloom_detector.py,bloom_seller.py,"
                                        "triton_preconfs_probe.py"))
    р.add_argument("--out", default="")
    а = р.parse_args()

    гр = разобрать_время(а.granica)
    начало = разобрать_время(а.s) if а.s else (гр - 18 * 3600 if гр else None)
    конец = разобрать_время(а.po) if а.po else time.time()
    if гр is None or начало is None:
        print("СБОЙ: границу окна не разобрать")
        return 1
    поз = позиции(а.state_dir)
    итог = {"с": а.s or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(начало)),
             "граница": а.granica,
             "по": а.po or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(конец)),
             "позиций_в_журнале": len(поз),
             "до_границы": окно(поз, с=начало, по=гр),
             "после_границы": окно(поз, с=гр, по=конец)}
    print(json.dumps({к: итог[к] for к in ("с", "граница", "по",
                                            "позиций_в_журнале")},
                     ensure_ascii=False))
    for имя in ("до_границы", "после_границы"):
        о = итог[имя]
        print(f"{имя}: сделок {о['сделок']}")
        for поле in ("увидели", "решили", "собрали"):
            з = о[поле]
            print(f"   {поле:8s} n={з['n']:3d} p50={з['p50']} p90={з['p90']} "
                  f"min={з['min']} max={з['max']}")
    итог["процессор"] = загрузка([x.strip() for x in а.imena.split(",") if x.strip()],
                                  секунд=а.cpu_sekund)
    ц = итог["процессор"]
    print(f"процессор: ядер {ц['ядер']}, занято {(ц.get('машина') or {}).get('занято_проц')} %, "
          f"load {ц.get('load_avg')}, замер {ц['секунд']:.0f} с")
    for п in ц["процессы"]:
        print(f"   {п['имя']:26s} pid {п['pid']:6d} "
              f"{п['проц_одного_ядра']:6.1f} % одного ядра "
              f"({п['проц_всей_машины']:.1f} % машины)")
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
        print("записано:", а.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
