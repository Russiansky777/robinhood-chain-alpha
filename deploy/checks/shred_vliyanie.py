#!/usr/bin/env python3
"""Влияние слушателя шредов на хост: три числа владельца (26.09 вечером).

  1) скорость порта хоста и входящий трафик за 60 с;
  2) потери UDP в буфере ядра (InErrors / RcvbufErrors) -- накопленные и за те
     же 60 с;
  3) медиана и p90 net_slot_age детектора ЗА ЧАС ДО и ЗА ЧАС ПОСЛЕ старта
     слушателя: если возраст сигнала вырос, служба мешает торговому пути.

Только чтение. Ничего не меняет, служб не касается. Всё считается на хосте:
/proc/net/dev и /proc/net/snmp вместо ifstat и netstat (их может не быть), и
журнал решений читается ПОТОКОМ.
"""
import argparse
import calendar
import glob
import gzip
import json
import os
import statistics
import time

ПОРОГ_РОСТА = 1.25  # во сколько раз медиана "после" считается ростом


def окно(момент: str) -> float:
    return calendar.timegm(time.strptime(момент, "%Y-%m-%dT%H:%M:%SZ"))


def внешний_интерфейс() -> str:
    """Интерфейс, через который идёт маршрут по умолчанию."""
    try:
        with open("/proc/net/route", encoding="utf-8") as ф:
            for стр in ф.read().splitlines()[1:]:
                поля = стр.split()
                if len(поля) > 2 and поля[1] == "00000000":
                    return поля[0]
    except OSError:
        pass
    return "eth0"


def скорость_порта(иф: str):
    """Мбит/с из /sys (то же, что печатает ethtool), или None."""
    try:
        with open(f"/sys/class/net/{иф}/speed", encoding="utf-8") as ф:
            з = int(ф.read().strip())
            return з if з > 0 else None
    except (OSError, ValueError):
        return None


def счётчики_интерфейса(иф: str) -> dict:
    """Байты и пакеты интерфейса -- то, что показывает ifstat."""
    with open("/proc/net/dev", encoding="utf-8") as ф:
        for стр in ф:
            if стр.strip().startswith(иф + ":"):
                ч = стр.split(":", 1)[1].split()
                return {"rx_bytes": int(ч[0]), "rx_packets": int(ч[1]),
                        "rx_errs": int(ч[2]), "rx_drop": int(ч[3]),
                        "tx_bytes": int(ч[8]), "tx_packets": int(ч[9])}
    return {}


def udp_счётчики() -> dict:
    """Udp: из /proc/net/snmp -- то же, что netstat -su."""
    из_ = {}
    try:
        with open("/proc/net/snmp", encoding="utf-8") as ф:
            строки = ф.read().splitlines()
    except OSError:
        return из_
    заголовки = None
    for стр in строки:
        if стр.startswith("Udp:"):
            если = стр.split()[1:]
            if заголовки is None:
                заголовки = если
            else:
                из_ = {к: int(v) for к, v in zip(заголовки, если)}
                заголовки = None
    return из_


def строки_журнала(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
        for ln in ф:
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


def свод(ряд: list, имя: str) -> dict:
    р = sorted(ряд)
    def проц(доля):
        if not р:
            return None
        и = min(len(р) - 1, max(0, int(round(доля * (len(р) - 1)))))
        return round(р[и], 3)
    return {"имя": имя, "n": len(р),
            "медиана_с": round(statistics.median(р), 3) if р else None,
            "p90_с": проц(0.9), "макс_с": round(max(р), 3) if р else None}


def возраст_сигнала(каталог: str, старт: float, часов: float) -> dict:
    """net_slot_age_s за час ДО и час ПОСЛЕ момента старта слушателя."""
    окно_с = часов * 3600.0
    до, после = [], []
    пути = sorted(glob.glob(os.path.join(каталог, "decisions.jsonl.*.gz")))
    пути.append(os.path.join(каталог, "decisions.jsonl"))
    строк = 0
    for путь in пути:
        if not os.path.exists(путь):
            continue
        for з in строки_журнала(путь):
            в = з.get("net_slot_age_s")
            if в is None:
                continue
            т = з.get("t_decide_ts") or з.get("t_recv_ts")
            if т is None:
                continue
            строк += 1
            т = float(т)
            if старт - окно_с <= т < старт:
                до.append(float(в))
            elif старт <= т < старт + окно_с:
                после.append(float(в))
    с_до, с_после = свод(до, "час до старта"), свод(после, "час после старта")
    вывод = "числа не сравнимы: в одном из окон решений нет"
    вырос = None
    if с_до["медиана_с"] and с_после["медиана_с"]:
        вырос = с_после["медиана_с"] > с_до["медиана_с"] * ПОРОГ_РОСТА
        вывод = ("ВЫРОС: медиана после больше медианы до более чем в "
                 f"{ПОРОГ_РОСТА} раза" if вырос
                 else "не вырос: медиана после в пределах порога")
    return {"строк_с_возрастом": строк, "до": с_до, "после": с_после,
            "вырос": вырос, "вывод": вывод}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--start-utc", required=True,
                   help="момент старта слушателя шредов (ГГГГ-ММ-ДДTЧЧ:ММ:ССZ)")
    р.add_argument("--hours", type=float, default=1.0)
    р.add_argument("--seconds", type=float, default=60.0,
                   help="окно замера трафика и потерь")
    а = р.parse_args()

    иф = внешний_интерфейс()
    до_иф, до_udp = счётчики_интерфейса(иф), udp_счётчики()
    т0 = time.monotonic()
    time.sleep(а.seconds)
    прошло = time.monotonic() - т0
    после_иф, после_udp = счётчики_интерфейса(иф), udp_счётчики()

    d_rx = после_иф.get("rx_bytes", 0) - до_иф.get("rx_bytes", 0)
    d_pk = после_иф.get("rx_packets", 0) - до_иф.get("rx_packets", 0)
    трафик = {
        "интерфейс": иф,
        "скорость_порта_мбит": скорость_порта(иф),
        "окно_с": round(прошло, 1),
        "входящих_байт": d_rx,
        "входящих_мбит_с": round(d_rx * 8 / прошло / 1e6, 3) if прошло else None,
        "входящих_пакетов": d_pk,
        "пакетов_в_с": round(d_pk / прошло, 1) if прошло else None,
        "доля_полосы": (round(d_rx * 8 / прошло / 1e6 / скорость_порта(иф), 4)
                        if (прошло and скорость_порта(иф)) else None),
        "rx_errs_накоплено": после_иф.get("rx_errs"),
        "rx_drop_накоплено": после_иф.get("rx_drop"),
        "rx_drop_за_окно": (после_иф.get("rx_drop", 0) - до_иф.get("rx_drop", 0)),
    }
    udp = {
        "InErrors_накоплено": после_udp.get("InErrors"),
        "RcvbufErrors_накоплено": после_udp.get("RcvbufErrors"),
        "InErrors_за_окно": (после_udp.get("InErrors", 0) - до_udp.get("InErrors", 0)),
        "RcvbufErrors_за_окно": (после_udp.get("RcvbufErrors", 0)
                                  - до_udp.get("RcvbufErrors", 0)),
        "InDatagrams_за_окно": (после_udp.get("InDatagrams", 0)
                                 - до_udp.get("InDatagrams", 0)),
        "оговорка": ("накопленные счётчики -- с загрузки машины, не с старта "
                      "службы: за окно измерена скорость потерь"),
    }
    возраст = возраст_сигнала(а.state_dir, окно(а.start_utc), а.hours)
    итог = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "chislo_1_trafik": трафик, "chislo_2_udp": udp,
            "chislo_3_vozrast_signala": возраст}
    print(json.dumps(итог, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
