#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ДВИЖЕНИЕ КОШЕЛЬКА ПОЛОСЫ ЗА ОКНО -- по цепи, поштучно, только чтение.

ЗАЧЕМ. Правило 11 ищет продажу позиции в истории её токенового счёта и берёт
оттуда подпись. Если тем же счётом потом занимался кто-то ещё -- владелец своим
ключом через Jupiter или прогон закрытия пустых счетов -- в строку попадает НЕ
та транзакция, и "вернулось" выходит нулём. 29.09 в окне 22:00Z это дало пять
позиций с "вернулось -5e-06" и удержанием 16-21 тысячи слотов, чего при
удержании 12 слотов быть не может.

ПОЭТОМУ здесь считается не позиция, а КОШЕЛЁК: каждая его транзакция за окно, в
ней -- нативная дельта кошелька и изменения токеновых остатков ЕГО счетов. Кто
подписал, по коду не видно и выдумывать этого нельзя: видно программы, минты и
числа. Классификация -- только по тому, что есть в транзакции.

Только чтение: getSignaturesForAddress и getTransaction. Ни подписи, ни отправки.
Измерительный код (Правило 8): самопроверок нет, числа берутся с цепи.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import time

HELIUS = "https://mainnet.helius-rpc.com"
ЛАМПОРТ = 1_000_000_000
# Программы, по которым видно, ЧЕМ шла сделка. Список -- для подписи строки,
# решений по нему не принимается.
ИМЕНА = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "Token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022",
    "11111111111111111111111111111111": "System",
    "ComputeBudget111111111111111111111111111111": "ComputeBudget",
}


def урл() -> str:
    к = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    return f"{HELIUS}/?api-key={к}"


def зов(метод: str, параметры, *, таймаут: float = 30.0) -> dict:
    import urllib.request  # noqa: PLC0415

    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    зпр = urllib.request.Request(урл(), data=тело,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
            о = json.loads(отв.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
    if "error" in о:
        return {"ok": False, "why_not": f"RPC: {str(о['error'])[:200]}"}
    return {"ok": True, "result": о.get("result")}


def utc(т) -> str | None:
    if not isinstance(т, (int, float)) or т <= 0:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(т)))


def в_секундах(с: str) -> float:
    """Строка UTC -> секунды эпохи. ТОЛЬКО calendar.timegm.

    ПОЧЕМУ НЕ mktime МИНУС time.timezone. mktime читает время как МЕСТНОЕ, а
    time.timezone -- сдвиг ЗИМНЕЙ зоны; на хосте в CEST это давало ошибку в
    час, и первый прогон 01:30Z взял окно 21:00-23:00Z вместо 22:00-00:00Z.
    Час сдвига в окне суток полосы -- это чужие сделки в счёте.
    """
    return float(calendar.timegm(time.strptime(с.replace("Z", ""),
                                                "%Y-%m-%dT%H:%M:%S")))


def разбор(tx: dict, кошелёк: str) -> dict:
    """Что эта транзакция сделала с кошельком: нативная дельта и токены."""
    из_: dict = {"натив_лампорты": None, "токены": [], "программы": [],
                 "err": None, "fee": None}
    мета = (tx or {}).get("meta") or {}
    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    из_["err"] = мета.get("err")
    из_["fee"] = мета.get("fee")

    ключи = [(к.get("pubkey") if isinstance(к, dict) else к)
              for к in (сообщение.get("accountKeys") or [])]
    if кошелёк in ключи:
        и = ключи.index(кошелёк)
        до, после = (мета.get("preBalances") or []), (мета.get("postBalances") or [])
        if и < len(до) and и < len(после):
            из_["натив_лампорты"] = int(после[и]) - int(до[и])

    # Токеновые остатки НАШИХ счетов: owner == кошелёк. Индексы совпадают у
    # pre и post не всегда -- сопоставляем по (accountIndex, mint).
    def карта(список):
        из2 = {}
        for з in (список or []):
            if (з.get("owner") or "") != кошелёк:
                continue
            сумма = ((з.get("uiTokenAmount") or {}).get("amount"))
            из2[(з.get("accountIndex"), з.get("mint"))] = int(сумма or 0)
        return из2

    до_т, после_т = карта(мета.get("preTokenBalances")), карта(мета.get("postTokenBalances"))
    for ключ in sorted(set(до_т) | set(после_т), key=lambda к: (к[0] or 0, к[1] or "")):
        было, стало = до_т.get(ключ, 0), после_т.get(ключ, 0)
        if было == стало:
            continue
        из_["токены"].append({"минт": ключ[1], "было": было, "стало": стало,
                               "дельта": стало - было})

    прг = set()
    for и in (сообщение.get("instructions") or []):
        п = и.get("programId") or (ключи[и["programIdIndex"]]
                                    if isinstance(и.get("programIdIndex"), int)
                                    and и["programIdIndex"] < len(ключи) else None)
        if п:
            прг.add(п)
    for в in (мета.get("innerInstructions") or []):
        for и in (в.get("instructions") or []):
            п = и.get("programId")
            if п:
                прг.add(п)
    из_["программы"] = sorted(ИМЕНА.get(п, п[:8]) for п in прг)
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--koshelek", required=True)
    р.add_argument("--s", required=True, help="с какого времени UTC, 2026-09-28T22:00:00Z")
    р.add_argument("--po", default="", help="до какого времени UTC (пусто -- до сейчас)")
    р.add_argument("--podpisej", type=int, default=100, help="сколько подписей тянуть")
    р.add_argument("--out", default="")
    а = р.parse_args()

    с_ts = в_секундах(а.s)
    по_ts = в_секундах(а.po) if а.po else None
    о = зов("getSignaturesForAddress", [а.koshelek, {"limit": int(а.podpisej)}])
    if not о.get("ok"):
        print(f"СБОЙ: подписи не прочитаны: {о.get('why_not')}")
        return 2
    подписи = []
    for з in (о.get("result") or []):
        bt = з.get("blockTime")
        if not isinstance(bt, (int, float)):
            continue
        if float(bt) < с_ts or (по_ts is not None and float(bt) > по_ts):
            continue
        подписи.append({"подпись": з.get("signature"), "ts": float(bt),
                         "slot": з.get("slot"), "err": з.get("err")})
    подписи.sort(key=lambda з: з["ts"])
    print(f"КОШЕЛЁК {а.koshelek}")
    print(f"ОКНО {а.s} -> {а.po or 'сейчас'} | транзакций в окне {len(подписи)} "
          f"(из {len(о.get('result') or [])} прочитанных подписей)")

    строки = []
    сумма_натив = 0
    for з in подписи:
        т = зов("getTransaction", [з["подпись"],
                                    {"encoding": "jsonParsed",
                                     "maxSupportedTransactionVersion": 0,
                                     "commitment": "finalized"}])
        if not т.get("ok") or not т.get("result"):
            print(f"  {utc(з['ts'])} {з['подпись'][:8]} НЕ ПРОЧИТАНА: "
                  f"{т.get('why_not') or 'узел вернул пусто'}")
            строки.append({**з, "прочитана": False,
                            "why_not": т.get("why_not") or "пусто"})
            continue
        р_ = разбор(т["result"], а.koshelek)
        нат = р_.get("натив_лампорты")
        if isinstance(нат, int):
            сумма_натив += нат
        токены = ", ".join(f"{str(х['минт'])[:8]} {х['дельта']:+d}" for х in р_["токены"]) \
            or "токены не двигались"
        print(f"  {utc(з['ts'])} slot {з['slot']} {з['подпись'][:10]} "
              f"натив {(нат / ЛАМПОРТ if isinstance(нат, int) else None)} SOL"
              + (f" | ОШИБКА {str(р_['err'])[:60]}" if р_.get("err") else ""))
        print(f"        {токены}")
        print(f"        программы: {', '.join(р_['программы'])}")
        строки.append({**з, "прочитана": True,
                        "натив_sol": (round(нат / ЛАМПОРТ, 9) if isinstance(нат, int) else None),
                        "токены": р_["токены"], "программы": р_["программы"],
                        "err": р_.get("err"), "fee": р_.get("fee")})

    print(f"СУММА НАТИВНОЙ ДЕЛЬТЫ КОШЕЛЬКА ЗА ОКНО: {round(сумма_натив / ЛАМПОРТ, 9)} SOL")
    print("ВАЖНО: это движение КОШЕЛЬКА, а не счёт торговли. Вывод владельца и "
          "возврат ренты входят сюда наравне со сделками -- разделять их по "
          "программам и минтам, а не по этому числу.")
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump({"кошелёк": а.koshelek, "с": а.s, "по": а.po or None,
                        "транзакций": len(подписи),
                        "сумма_натив_sol": round(сумма_натив / ЛАМПОРТ, 9),
                        "строки": строки}, ф, ensure_ascii=False, indent=1)
        print("записано:", а.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
