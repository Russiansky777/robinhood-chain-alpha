#!/usr/bin/env python3
"""CU, сожжённые покупками полосы, ПО СТРОИТЕЛЯМ: p50/p99 и падения по лимиту.

ЗАЧЕМ. Предел CU покупки один на всех строителей -- BLOOM_LANE_CU_LIMIT,
140 000 единиц. Владелец спрашивает: сколько на самом деле жжёт каждый
строитель (bonding / pump_amm / cpmm / damm2 / dbc / clmm / two_step), и не
стоит ли двухшаговая сборка у предела. Если стоит -- предел поднимается ПО
СТРОИТЕЛЮ (p99 + 10 %), а не общий: общий подъём дороже для всех остальных.

Только чтение: журнал позиций (потоком), getSignatureStatuses пакетом и
getTransaction по севшим подписям. Ни подписи, ни отправки, ни ордера.
"""
from __future__ import annotations

import argparse
import calendar
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Адреса строителей -> имена, которыми их называет владелец.
СТРОИТЕЛИ = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "bonding",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pump_amm",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "cpmm",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "damm2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "dbc",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "clmm",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "launchlab",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "dlmm",
}
# Признаки того, что узел отказал ИМЕННО по вычислительному лимиту.
СЛЕДЫ_ЛИМИТА = ("ComputeBudgetExceeded", "exceeded CUs meter",
                 "exceeded maximum number of instructions",
                 "Computational budget exceeded")


def строки(путь: str):
    """Журнал позиций потоком: файл идёт на сотни тысяч строк."""
    try:
        with open(путь, encoding="utf-8", errors="replace") as ф:
            for с in ф:
                с = с.strip()
                if not с.startswith("{"):
                    continue
                try:
                    yield json.loads(с)
                except ValueError:
                    continue
    except OSError:
        return


def позиции_полосы(state_dir: str, с_ts: float) -> dict:
    """Сделки полосы с момента с_ts: cid -> накопленная запись."""
    из_: dict = {}
    for путь in sorted(glob.glob(str(Path(state_dir) / "positions.jsonl*"))):
        for r in строки(путь):
            cid = r.get("client_order_id")
            if not cid:
                continue
            в = из_.setdefault(cid, {})
            for к, зн in r.items():
                if зн is not None:
                    в[к] = зн
    отбор = {}
    for cid, п in из_.items():
        if not п.get("lane"):
            continue
        т = п.get("ts_sent") or п.get("ts_intent")
        if not isinstance(т, (int, float)) or float(т) < с_ts:
            continue
        отбор[cid] = п
    return отбор


def варианты_подписей(п: dict) -> list:
    """Все подписи варианта покупки: садится одна, а принята может быть другая."""
    в = [п.get("lane_landed_signature"), п.get("lane_signature"),
         п.get("lane_signature_accepted_first")]
    в += list(п.get("lane_pool_candidates") or [])
    в.append(п.get("lane_signature_local"))
    return list(dict.fromkeys([x for x in в if isinstance(x, str) and x]))


def имя_строителя(п: dict) -> str:
    """Строитель сделки: two_step отдельно от пула второго шага."""
    if п.get("lane_two_step") or п.get("two_step_used"):
        return "two_step"
    прог = п.get("program") or п.get("pool_program")
    return СТРОИТЕЛИ.get(прог, прог or "строителя в записи нет")


class Узел:
    """Минимальный клиент JSON-RPC: только два метода, только чтение."""

    def __init__(self, url: str, *, потолок_версии: int = 1) -> None:
        self.url = url
        self.потолок_версии = потолок_версии
        self.вызовов = 0

    def зов(self, метод: str, параметры: list) -> dict:
        тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                            "params": параметры}).encode()
        зап = urllib.request.Request(
            self.url, data=тело, headers={"Content-Type": "application/json"})
        self.вызовов += 1
        with urllib.request.urlopen(зап, timeout=30) as о:
            ответ = json.loads(о.read().decode())
        if "error" in ответ:
            raise RuntimeError(f"{метод}: {str(ответ['error'])[:160]}")
        return ответ.get("result") or {}

    def статусы(self, подписи: list) -> list:
        return (self.зов("getSignatureStatuses",
                          [подписи[:256], {"searchTransactionHistory": True}])
                or {}).get("value") or []

    def транзакция(self, подпись: str) -> dict:
        return self.зов("getTransaction",
                         [подпись, {"encoding": "json",
                                     "maxSupportedTransactionVersion":
                                         self.потолок_версии}]) or {}


def процентиль(числа: list, доля: float):
    """p50/p99 без numpy: ближайший ранг, как считают все прочие прогоны."""
    ч = sorted(x for x in числа if isinstance(x, (int, float)))
    if not ч:
        return None
    к = max(0, min(len(ч) - 1, int(round(доля * (len(ч) - 1)))))
    return ч[к]


def свод(замеры: list, предел: int) -> dict:
    """Один строитель: сколько сделок, p50/p99 CU, падения по лимиту."""
    cu = [з["cu"] for з in замеры if з.get("cu") is not None]
    p50, p99 = процентиль(cu, 0.5), процентиль(cu, 0.99)
    из_ = {"сделок": len(замеры), "с_числом_cu": len(cu),
            "p50": p50, "p99": p99, "макс": max(cu) if cu else None,
            "падений_по_лимиту": sum(1 for з in замеры if з.get("по_лимиту")),
            "у_предела": None, "предложить_предел": None}
    if p99 is not None:
        из_["у_предела"] = p99 >= 0.9 * предел
        из_["предложить_предел"] = int(round(p99 * 1.1))
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--since", default="",
                    help="UTC как 2026-09-28T00:21:00Z, или -24h, или пусто (сутки)")
    р.add_argument("--predel", type=int,
                    default=int(os.environ.get("BLOOM_LANE_CU_LIMIT") or 140000))
    р.add_argument("--out", default="")
    р.add_argument("--self-test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return самопроверка()
    с_ts = разобрать_момент(а.since)
    поз = позиции_полосы(а.state_dir, с_ts)
    ключ = os.environ.get("HELIUS_API_KEY") or ""
    if not ключ:
        print("SBOY: HELIUS_API_KEY ne zadan")
        return 2
    узел = Узел(f"https://mainnet.helius-rpc.com/?api-key={ключ}")
    # СЕВШИЕ ПОДПИСИ -- ОДНИМ ПАКЕТОМ НА ВСЕ СДЕЛКИ. Полоса шлёт до шести
    # вариантов на одном nonce, садится один; getSignatureStatuses берёт до 256
    # подписей за вызов, поэтому весь день укладывается в один-два вызова.
    все_подписи, чьи = [], {}
    for cid, п in поз.items():
        for с in варианты_подписей(п):
            если_нет = чьи.setdefault(с, cid)
            if если_нет == cid and с not in все_подписи:
                все_подписи.append(с)
    севшие = {}
    for н in range(0, len(все_подписи), 256):
        кусок = все_подписи[н:н + 256]
        try:
            значения = узел.статусы(кусок)
        except Exception as exc:  # noqa: BLE001
            print(f"statusy ne otdalis: {type(exc).__name__}: {str(exc)[:120]}")
            значения = []
        for подпись, з in zip(кусок, значения):
            if isinstance(з, dict):
                севшие.setdefault(чьи[подпись], {"signature": подпись,
                                                  "slot": з.get("slot"),
                                                  "err": з.get("err")})
    по_строителям: dict = {}
    ряды = []
    for cid, п in sorted(поз.items()):
        строитель = имя_строителя(п)
        села = севшие.get(cid) or {}
        запись = {"cid": cid, "строитель": строитель,
                   "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(float(п.get("ts_sent")
                                                            or п.get("ts_intent") or 0))),
                   "signature": села.get("signature"), "slot": села.get("slot"),
                   "cu": None, "по_лимиту": False, "why_not": None}
        if not села.get("signature"):
            запись["why_not"] = "севшей подписи в цепи нет"
        else:
            try:
                tx = узел.транзакция(села["signature"])
            except Exception as exc:  # noqa: BLE001
                tx = {}
                запись["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            meta = (tx or {}).get("meta") or {}
            запись["cu"] = meta.get("computeUnitsConsumed")
            текст = json.dumps({"err": meta.get("err"),
                                 "logs": meta.get("logMessages") or []},
                                ensure_ascii=False)
            запись["по_лимиту"] = any(сл in текст for сл in СЛЕДЫ_ЛИМИТА)
            if запись["cu"] is None and not запись["why_not"]:
                запись["why_not"] = "узел не отдал computeUnitsConsumed"
        ряды.append(запись)
        по_строителям.setdefault(строитель, []).append(запись)
    итог = {"с": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(с_ts)),
             "предел_cu": а.predel, "сделок": len(ряды),
             "вызовов_узла": узел.вызовов, "только_чтение": True,
             "по_строителям": {к: свод(v, а.predel)
                                for к, v in sorted(по_строителям.items())},
             "ряды": ряды}
    print(json.dumps({к: итог[к] for к in
                       ("с", "предел_cu", "сделок", "вызовов_узла", "по_строителям")},
                      ensure_ascii=False, indent=1))
    if а.out:
        Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 0


def разобрать_момент(текст: str) -> float:
    """'-24h', ISO с Z, или пусто (сутки назад) -> отметка времени."""
    т = (текст or "").strip()
    if not т:
        return time.time() - 24 * 3600
    if т.startswith("-") and т.endswith(("h", "m")):
        число = float(т[1:-1])
        return time.time() - число * (3600 if т.endswith("h") else 60)
    # ВРЕМЯ UTC РАЗБИРАЕТСЯ КАК UTC. mktime считает строку местным временем
    # хоста (Амстердам, летом +2), и окно уезжало на час-два: прогон, которому
    # сказали "с 00:21Z", брал сделки с 23:21Z предыдущих суток.
    for формат in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M"):
        try:
            return float(calendar.timegm(time.strptime(т, формат)))
        except ValueError:
            continue
    print(f"moment ne razobran: {т!r} -- berem sutki")
    return time.time() - 24 * 3600


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

    chk("p50 по пяти числам -- середина", процентиль([1, 2, 3, 4, 5], 0.5) == 3)
    chk("p99 по пяти числам -- максимум", процентиль([1, 2, 3, 4, 5], 0.99) == 5)
    chk("пустой список процентиля не даёт", процентиль([], 0.5) is None)
    с = свод([{"cu": 100000}, {"cu": 139000, "по_лимиту": True}], 140000)
    chk(f"у предела видно ({с['p99']} из 140000)", с["у_предела"] is True)
    chk(f"предложение по строителю p99+10% = {с['предложить_предел']}",
        с["предложить_предел"] == int(round(139000 * 1.1)))
    chk("падение по лимиту сосчитано", с["падений_по_лимиту"] == 1)
    chk("имя строителя по адресу pump_amm",
        имя_строителя({"program": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"})
        == "pump_amm")
    chk("двухшаговая считается отдельно от пула",
        имя_строителя({"program": "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
                        "lane_two_step": True}) == "two_step")
    chk("время UTC разбирается как UTC, а не как местное хоста",
        разобрать_момент("2026-09-28T00:21:00Z") == 1790554860.0,
        разобрать_момент("2026-09-28T00:21:00Z"))
    chk("варианты подписей не двоятся и без пустых",
        варианты_подписей({"lane_signature": "A", "lane_pool_candidates": ["A", "B"],
                            "lane_signature_local": None}) == ["A", "B"])
    print(f"самопроверка CU по строителям: {всего - сбоев}/{всего} пройдено")
    return 1 if сбоев else 0


if __name__ == "__main__":
    sys.exit(main())
