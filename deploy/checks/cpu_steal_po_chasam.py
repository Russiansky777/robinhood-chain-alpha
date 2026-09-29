#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Steal и занятость процессора ХОСТА, снимок в час, строкой в журнал.

ЗАЧЕМ (слово владельца 29.09 ночью, п.5): "steal и загрузка ядра детектора по
часам". Такого источника в репозитории не было: deploy/checks/skorost_i_cpu.py
делает ОДИН замер и steal не снимает вовсе, а /proc/stat с момента загрузки даёт
среднее за неделю -- по нему не видно, был ли steal в минуту сделки.

КАК СЧИТАЕТСЯ. /proc/stat и /proc/<pid>/stat -- СЧЁТЧИКИ С ЗАГРУЗКИ, а не
проценты. Процент за час получается только разностью двух снимков, поэтому здесь
пишется снимок, а доли считаются между соседними строками журнала: предыдущая
строка читается из того же файла. Первый снимок доли не даёт вовсе -- и говорит
об этом словами, а не нулём.

ЧТО В СТРОКЕ. utc, счётчики /proc/stat (user, nice, system, idle, iowait, irq,
softirq, steal), такты процессов bloom-detector и bloom-seller (utime+stime),
число ядер, uptime. Плюс доли за интервал, когда есть предыдущая строка.

Только чтение /proc. Ни сети, ни ключей, ни единой записи куда-либо, кроме
своего журнала.
"""

from __future__ import annotations

import argparse
import json
import os
import time

ЖУРНАЛ_ПО_УМОЛЧАНИЮ = "data/cpu_steal_po_chasam.jsonl"
ПОЛЯ_STAT = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def счётчики_cpu(путь: str = "/proc/stat") -> dict:
    """Первая строка /proc/stat -- сумма по всем ядрам, в тактах с загрузки."""
    из_ = {"ok": False, "why_not": None}
    try:
        with open(путь, encoding="utf-8") as ф:
            for стр in ф:
                if not стр.startswith("cpu "):
                    continue
                ч = стр.split()[1:]
                for и, имя in enumerate(ПОЛЯ_STAT):
                    из_[имя] = int(ч[и]) if и < len(ч) else None
                из_["всего"] = sum(int(x) for x in ч[:8] if x.isdigit())
                из_["ok"] = True
                break
        if not из_["ok"]:
            из_["why_not"] = "в /proc/stat нет строки 'cpu '"
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return из_


def такты_процесса(имя_службы: str) -> dict:
    """utime+stime процесса службы. Процесс не найден -- причина, а не ноль."""
    из_ = {"служба": имя_службы, "pid": None, "такты": None, "why_not": None}
    pid = None
    try:
        # Пид берётся у systemd: искать по имени в ps значит однажды найти
        # чужой процесс с похожей строкой.
        import subprocess  # noqa: PLC0415

        от = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value",
                              имя_службы], capture_output=True, text=True,
                             timeout=5)
        pid = int((от.stdout or "0").strip() or 0)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"systemctl не ответил: {type(exc).__name__}"
        return из_
    if not pid:
        из_["why_not"] = "у службы нет главного процесса (не запущена)"
        return из_
    из_["pid"] = pid
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as ф:
            ч = ф.read().split()
        # utime -- поле 14, stime -- 15 (нумерация с единицы), но имя процесса
        # может содержать пробелы и скобки, поэтому считаем от ПОСЛЕДНЕЙ скобки.
        сырое = " ".join(ч)
        хвост = сырое[сырое.rindex(")") + 1:].split()
        из_["такты"] = int(хвост[11]) + int(хвост[12])
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return из_


def последняя_строка(путь: str) -> dict | None:
    """Предыдущий снимок из журнала. Журнала нет -- None, и это не ошибка."""
    try:
        размер = os.path.getsize(путь)
    except Exception:  # noqa: BLE001
        return None
    try:
        with open(путь, "rb") as ф:
            # Читаем хвост: журнал растёт, и перечитывать его целиком незачем.
            ф.seek(max(0, размер - 65536))
            хвост = ф.read().decode("utf-8", errors="replace").strip().split("\n")
        for стр in reversed(хвост):
            стр = стр.strip()
            if not стр:
                continue
            try:
                return json.loads(стр)
            except ValueError:
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def доли(теперь: dict, прежде: dict | None) -> dict:
    """Проценты за интервал между двумя снимками. Один снимок -- не проценты."""
    из_ = {"интервал_с": None, "steal_проц": None, "занято_проц": None,
            "iowait_проц": None, "почему": None, "по_службам_проц": {}}
    if not прежде:
        из_["почему"] = ("это первый снимок: доли считаются разностью двух, "
                          "одного снимка мало")
        return из_
    т1, т0 = (теперь.get("cpu") or {}), (прежде.get("cpu") or {})
    if not т1.get("ok") or not т0.get("ok"):
        из_["почему"] = "счётчики /proc/stat не прочитаны в одном из снимков"
        return из_
    всего = (т1.get("всего") or 0) - (т0.get("всего") or 0)
    if всего <= 0:
        из_["почему"] = ("счётчики не выросли: хост перезагружался или снимки "
                          "сделаны в одну секунду")
        return из_
    из_["интервал_с"] = round(float(теперь.get("utc_ts") or 0)
                               - float(прежде.get("utc_ts") or 0), 1)
    из_["steal_проц"] = round(100.0 * ((т1.get("steal") or 0) - (т0.get("steal") or 0))
                               / всего, 4)
    из_["iowait_проц"] = round(100.0 * ((т1.get("iowait") or 0) - (т0.get("iowait") or 0))
                                / всего, 4)
    из_["занято_проц"] = round(100.0 * (всего - ((т1.get("idle") or 0)
                                                  - (т0.get("idle") or 0))) / всего, 4)
    # ДОЛЯ СЛУЖБЫ СЧИТАЕТСЯ ОТ ОДНОГО ЯДРА, а не от всех: "загрузка ядра
    # детектора" -- это именно про ядро, и 100 % значит одно ядро целиком.
    ядер = теперь.get("ядер") or 1
    для_ядра = всего / max(1, int(ядер))
    for имя, т in (теперь.get("службы") or {}).items():
        было = ((прежде.get("службы") or {}).get(имя) or {}).get("такты")
        стало = (т or {}).get("такты")
        if not isinstance(было, int) or not isinstance(стало, int) or стало < было:
            из_["по_службам_проц"][имя] = None
            continue
        из_["по_службам_проц"][имя] = round(100.0 * (стало - было) / max(1.0, для_ядра), 3)
    return из_


def снимок(службы: list) -> dict:
    из_ = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "utc_ts": time.time(),
            "ядер": (os.cpu_count() or 1),
            "cpu": счётчики_cpu(),
            "службы": {},
            "uptime_s": None}
    try:
        with open("/proc/uptime", encoding="utf-8") as ф:
            из_["uptime_s"] = float(ф.read().split()[0])
    except Exception:  # noqa: BLE001
        из_["uptime_s"] = None
    for имя in службы:
        из_["службы"][имя] = такты_процесса(имя)
    return из_


def main() -> int:
    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--zhurnal", default=ЖУРНАЛ_ПО_УМОЛЧАНИЮ)
    р.add_argument("--sluzhby", default="bloom-detector,bloom-seller")
    р.add_argument("--self-test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return self_test()
    службы = [ч.strip() for ч in а.sluzhby.split(",") if ч.strip()]
    прежде = последняя_строка(а.zhurnal)
    теперь = снимок(службы)
    теперь["доли_от_прошлого"] = доли(теперь, прежде)
    каталог = os.path.dirname(os.path.abspath(а.zhurnal))
    if каталог:
        os.makedirs(каталог, exist_ok=True)
    with open(а.zhurnal, "a", encoding="utf-8") as ф:
        ф.write(json.dumps(теперь, ensure_ascii=False) + "\n")
    д = теперь["доли_от_прошлого"]
    служебное = ", ".join(f"{и}: {з}%" for и, з in (д.get("по_службам_проц") or {}).items())
    print(f"CPU/STEAL {теперь['utc']}: steal {д.get('steal_проц')} %, "
           f"занято {д.get('занято_проц')} %, iowait {д.get('iowait_проц')} %, "
           f"интервал {д.get('интервал_с')} с, ядер {теперь['ядер']}"
           + (f" | {служебное}" if служебное else "")
           + (f" | {д.get('почему')}" if д.get("почему") else ""))
    print(f"журнал: {а.zhurnal}")
    return 0


def self_test() -> int:
    """Чистая арифметика долей: она и есть то, что может соврать незаметно."""
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    def снимок_из(такты_всего, idle, steal, iowait, служба, ts, ядер=4):
        return {"utc_ts": ts, "ядер": ядер,
                 "cpu": {"ok": True, "всего": такты_всего, "idle": idle,
                          "steal": steal, "iowait": iowait},
                 "службы": {"bloom-detector": {"такты": служба}}}

    п = снимок_из(1000, 800, 10, 5, 100, 1000.0)
    т = снимок_из(2000, 1500, 30, 15, 200, 4600.0)
    д = доли(т, п)
    chk("steal за интервал -- 20 из 1000 тактов, то есть 2 %",
        д["steal_проц"] == 2.0, д["steal_проц"])
    chk("занято -- 1000 минус 700 простоя, то есть 30 %",
        д["занято_проц"] == 30.0, д["занято_проц"])
    chk("iowait считается тем же правилом", д["iowait_проц"] == 1.0, д["iowait_проц"])
    chk("интервал в секундах взят из метки времени",
        д["интервал_с"] == 3600.0, д["интервал_с"])
    # 100 тактов службы при 1000 тактах на 4 ядра -- это 40 % ОДНОГО ядра.
    chk("доля службы считается от ОДНОГО ядра, а не от всех",
        д["по_службам_проц"]["bloom-detector"] == 40.0,
        д["по_службам_проц"])
    chk("первый снимок долей не даёт и говорит почему",
        доли(т, None)["steal_проц"] is None
        and "первый снимок" in (доли(т, None)["почему"] or ""), доли(т, None))
    # Перезагрузка хоста: счётчики упали -- не считаем отрицательные проценты.
    д_пере = доли(снимок_из(500, 400, 5, 2, 50, 5000.0), т)
    chk("счётчики не выросли -- доли None и причина, а не минус",
        д_пере["steal_проц"] is None and "перезагру" in (д_пере["почему"] or ""),
        д_пере)
    # Такты службы упали (служба перезапущена) -- её доля None, не минус.
    т2 = снимок_из(3000, 2000, 40, 20, 50, 8200.0)
    chk("служба перезапущена -- её доля None, а не отрицательная",
        доли(т2, т)["по_службам_проц"]["bloom-detector"] is None,
        доли(т2, т)["по_службам_проц"])
    # Счётчики самого хоста читаются: это уже не арифметика, но проверить дёшево.
    c = счётчики_cpu()
    chk("счётчики /proc/stat читаются на этой машине",
        c["ok"] and isinstance(c.get("steal"), int), c.get("why_not"))
    print(f"самопроверка steal по часам: {всего[1]}/{всего[0]}"
           f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    return 0 if всего[1] == всего[0] else 1


if __name__ == "__main__":
    raise SystemExit(main())
