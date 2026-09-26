#!/usr/bin/env python3
"""Сколько сигналов за сутки взяли бы НОВЫЕ сборщики (только чтение).

ЗАЧЕМ. Ночью 27.09 добавлены: разновидность инструкции кривой pump.fun
c2ab1c46684d5b2f, Meteora DAMM v2 (цена по событию свопа), Meteora DBC. Число к
утреннему докладу: сколько сигналов 26.09 из-за ЭТИХ причин не были взяты.

ЧТО СЧИТАЕТСЯ. Журнал решений на хосте, потоком. По каждой подписи источника
собирается: решение (buy или skip и почему) и строка тени (why_not, программа
пула, минт котировки, маршрут). Дальше считаются ровно те отказы, которые
сборщики закрывают, и отдельно -- сколько из них по сигналам, где решение было
BUY (то есть фильтры задачи и наши пределы прошли: такой сигнал полоса взяла бы
сразу, если бы сборщик умел этот пул).

ЧЕГО ЭТО ЧИСЛО НЕ ЗНАЕТ. Комиссии пула в журнале нет, а полоса теперь
отказывается от пула с комиссией выше потолка -- значит верхняя оценка. И
котировка не в SOL по-прежнему не берётся: такие строки считаются отдельно.

Ни ключей, ни сделок, ни сети.
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

WSOL = "So11111111111111111111111111111111111111112"
НАТИВНАЯ = "native_sol"
# Программы, которые полоса умеет ПОСЛЕ ночных правок (флаги -- отдельно).
ПРОГРАММЫ = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM (был)",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM (был)",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "кривая pump.fun (флаг)",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2 (флаг, ночь)",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC (флаг, ночь)",
}
# Причины отказа, которые ночные сборщики закрывают.
ЗАКРЫТЫЕ = (
    "тип пула вне полосы",
    "разновидность инструкции кривой не известна: c2ab1c46684d5b2f",
    "сосредоточенная ликвидность: резервы цену не дают",
)


def метка(текст: str) -> float:
    return calendar.timegm(time.strptime(текст.replace("Z", ""), "%Y-%m-%dT%H:%M:%S"))


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    try:
        with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
            for с in ф:
                с = с.strip()
                if с:
                    yield с
    except OSError as exc:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {путь} ({type(exc).__name__})", file=sys.stderr)


def файлы(каталог: str) -> list:
    из_ = sorted(glob.glob(os.path.join(каталог, "decisions.jsonl*")))
    осн = os.path.join(каталог, "decisions.jsonl")
    if осн in из_:
        из_.remove(осн)
        из_.append(осн)
    return из_


def время(з: dict) -> float | None:
    т = з.get("t_recv_utc") or з.get("ts_utc")
    if isinstance(т, str) and т.endswith("Z"):
        try:
            return метка(т)
        except ValueError:
            return None
    зн = з.get("t_recv_ts")
    return float(зн) if isinstance(зн, (int, float)) else None


def собрать(каталог: str, с_: float, по_: float) -> dict:
    по_подписи: dict = {}
    просмотрено = 0
    for путь in файлы(каталог):
        for с in строки(путь):
            просмотрено += 1
            try:
                з = json.loads(с)
            except ValueError:
                continue
            if not isinstance(з, dict):
                continue
            подпись = з.get("signature")
            if not подпись:
                continue
            т = время(з)
            if т is not None and not (с_ <= т < по_):
                continue
            в = по_подписи.setdefault(подпись, {})
            if з.get("stage") == "shadow":
                в["shadow"] = {k: з.get(k) for k in
                                ("why_not", "pool_program", "quote_mint", "route")}
            elif з.get("action") in ("buy", "skip"):
                # Последнее решение по подписи и есть решение: повторы бывают.
                в["решение"] = {"action": з.get("action"), "reason": з.get("reason"),
                                 "source": з.get("source"), "mint": з.get("mint")}
            if з.get("stage") == "build" and з.get("why_not"):
                в.setdefault("build", {})["why_not"] = з["why_not"]
                в.setdefault("build", {})["group"] = з.get("group")
    return {"по_подписи": по_подписи, "просмотрено": просмотрено}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--s", default="2026-09-25T22:00:00Z", help="начало окна UTC")
    р.add_argument("--po", default="2026-09-26T22:00:00Z", help="конец окна UTC")
    а = р.parse_args()
    д = собрать(а.state_dir, метка(а.s), метка(а.po))
    по_причине: dict = {}
    по_программе: dict = {}
    взяли_бы = {"всего": 0, "решение_buy": 0, "котировка_sol": 0,
                 "по_программе": {}}
    сигналов = 0
    for подпись, в in д["по_подписи"].items():
        тень = в.get("shadow") or {}
        почему = str(тень.get("why_not") or "")
        if not почему:
            continue
        сигналов += 1
        ключ = почему.split(":")[0][:60]
        по_причине[ключ] = по_причине.get(ключ, 0) + 1
        прог = тень.get("pool_program") or "нет"
        по_программе[прог] = по_программе.get(прог, 0) + 1
        закрыто = any(з in почему for з in ЗАКРЫТЫЕ)
        умеем = прог in ПРОГРАММЫ
        котировка_sol = тень.get("quote_mint") in (WSOL, НАТИВНАЯ)
        один_шаг = (тень.get("route") or "one_hop") == "one_hop"
        if закрыто and умеем and котировка_sol and один_шаг:
            взяли_бы["всего"] += 1
            взяли_бы["по_программе"][ПРОГРАММЫ[прог]] = \
                взяли_бы["по_программе"].get(ПРОГРАММЫ[прог], 0) + 1
            if котировка_sol:
                взяли_бы["котировка_sol"] += 1
            if (в.get("решение") or {}).get("action") == "buy":
                взяли_бы["решение_buy"] += 1
    из_ = {"окно": [а.s, а.po], "строк_просмотрено": д["просмотрено"],
            "подписей_в_окне": len(д["по_подписи"]),
            "сигналов_с_отказом_тени": сигналов,
            "по_причине": dict(sorted(по_причине.items(), key=lambda x: -x[1])),
            "по_программе_пула": dict(sorted(по_программе.items(),
                                              key=lambda x: -x[1])),
            "взяли_бы_новые_сборщики": взяли_бы,
            "оговорка": ("верхняя оценка: комиссии пула в журнале нет, а полоса "
                          "теперь отказывается от пула с комиссией выше потолка "
                          "BLOOM_LANE_MAX_POOL_FEE")}
    print(json.dumps(из_, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
