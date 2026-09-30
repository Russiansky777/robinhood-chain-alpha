#!/usr/bin/env python3
"""Ставка комиссии пула Pump AMM: где она лежит в счетах (только чтение).

ЗАЧЕМ. Своя продажа одной ногой считает минимум выхода по ЖИВЫМ резервам
(x*y=k), и для этого нужна доля, которая НЕ доходит до кривой -- комиссия пула.
Выдумывать её нельзя: занизишь -- минимум выйдет выше настоящего и продажа не
сядет, завысишь -- примем хуже рынка. У покупки она калибруется по сделке
источника, у продажи источника нет вовсе.

КАК ВЫВОДИТСЯ, А НЕ УГАДЫВАЕТСЯ. Два независимых числа сводятся вместе:

1. ПО НАШИМ ЖЕ ПОКУПКАМ. В покупке известны живые резервы ДО (pre у хранилищ
   пула), сколько котировки вошло и сколько базы вышло. Из x*y=k однозначно
   решается чистый вход, а значит и доля комиссии:
       net = out * quote_res / (base_res - out),   f = 1 - net / gross.
2. ПО СЧЕТАМ ПУЛА И global config. Их данные читаются как есть, и ПЕРЕБИРАЮТСЯ
   ВСЕ смещения: ищем поля, чьё значение совпадает с решённой долей в базисных
   пунктах (1e-4) или в миллионных (1e-6) У ВСЕХ образцов сразу.

Совпало ровно одно смещение у всех сделок -- оно и есть ставка. Совпало
несколько или ни одного -- прогон честно это печатает, и сборщик продажи
по-прежнему не пишется.

Ни одной отправки, ни одной подписи. Только getTransaction и
getMultipleAccounts.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from decimal import Decimal as D
from pathlib import Path

PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
ДИСК_ПОКУПКИ = "c62e1552b4d9e870"
# Индексы ролей в инструкции ПОКУПКИ Pump AMM -- проверены на живых сделках
# (c2_swap_build.SPECS): пул 0, global config 2, хранилище базы 7, котировки 8.
СМ_ПУЛ, СМ_КОНФИГ, СМ_ХРАН_БАЗЫ, СМ_ХРАН_КОТИРОВКИ = 0, 2, 7, 8


def узел() -> str:
    ключ = (os.environ.get("HELIUS_API_KEY2")
            or os.environ.get("HELIUS_API_KEY") or "")
    if not ключ:
        raise SystemExit("SBOY: net HELIUS_API_KEY v okruzhenii")
    return f"https://mainnet.helius-rpc.com/?api-key={ключ}"


def rpc(метод: str, параметры):
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    зп = urllib.request.Request(узел(), data=тело,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(зп, timeout=30) as от:
        о = json.loads(от.read().decode())
    if "error" in о:
        raise RuntimeError(str(о["error"])[:200])
    return о.get("result")


def b58decode(s: str) -> bytes:
    алф = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    for ch in s:
        n = n * 58 + алф.index(ch)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + b


def инструкции(tx: dict) -> list:
    сооб = ((tx or {}).get("transaction") or {}).get("message") or {}
    из_ = list(сооб.get("instructions") or [])
    for г in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        из_ += list(г.get("instructions") or [])
    return [i for i in из_ if isinstance(i, dict) and isinstance(i.get("accounts"), list)]


def остатки_счетов(tx: dict) -> dict:
    """{счёт: (pre, post)} по токен-балансам транзакции, СЫРЫМИ числами."""
    мета = (tx or {}).get("meta") or {}
    ключи = [k.get("pubkey") if isinstance(k, dict) else k
             for k in (((tx or {}).get("transaction") or {}).get("message") or {})
             .get("accountKeys") or []]
    из_: dict = {}
    for имя, поле in (("pre", "preTokenBalances"), ("post", "postTokenBalances")):
        for з in мета.get(поле) or []:
            и = з.get("accountIndex")
            счёт = ключи[и] if isinstance(и, int) and и < len(ключи) else None
            сумма = ((з.get("uiTokenAmount") or {}).get("amount"))
            if счёт is None or сумма is None:
                continue
            пара = из_.setdefault(счёт, {"pre": 0, "post": 0})
            пара[имя] = int(сумма)
    return из_


def комиссия_по_счетам(tx: dict) -> dict:
    """Комиссия, УПЛАЧЕННАЯ по нашей покупке -- по дельтам ВСЕХ счетов инструкции.

    ПОЧЕМУ ТАК, А НЕ ИЗ КРИВОЙ. Первая попытка решала долю из x*y=k по дельте
    хранилища котировки и получила от 1 % до 18 % -- разные числа у каждого пула
    и зависящие от размера сделки, то есть в «долю» попало собственное скольжение
    кривой. Причина: у Pump AMM комиссия НЕ ВХОДИТ в хранилище пула, она уходит
    отдельными переводами на счета получателей. Значит мерить её надо там, куда
    она уходит: по токен-счетам инструкции, которые ПРИБАВИЛИ котировку и не
    являются хранилищем пула.

    Возвращает уплаченную комиссию в сырых единицах котировки и её долю от того,
    что списалось с нашего счёта котировки.
    """
    for ix in инструкции(tx):
        if ix.get("programId") != PUMP_AMM:
            continue
        сырое = b58decode(ix.get("data") or "")
        if сырое[:8].hex() != ДИСК_ПОКУПКИ:
            continue
        сч = ix["accounts"]
        if len(сч) <= СМ_ХРАН_КОТИРОВКИ:
            continue
        ост = остатки_счетов(tx)
        минт_кот = сч[4] if len(сч) > 4 else None
        хран_кот = сч[СМ_ХРАН_КОТИРОВКИ]
        наш_кот = сч[6] if len(сч) > 6 else None
        в_пул = (ост.get(хран_кот, {}).get("post", 0)
                 - ост.get(хран_кот, {}).get("pre", 0))
        # ПОЛУЧАТЕЛИ КОМИССИИ: любой счёт инструкции, который котировку
        # ПРИБАВИЛ и при этом не хранилище пула и не наш собственный счёт.
        комиссия, куда = 0, []
        for i, а in enumerate(сч):
            if i in (СМ_ХРАН_КОТИРОВКИ,) or а == наш_кот:
                continue
            п = ост.get(а)
            if not п:
                continue
            д = п["post"] - п["pre"]
            if д > 0:
                комиссия += д
                куда.append({"место": i, "счёт": а, "прибавил": д})
        if в_пул <= 0:
            continue
        return {"пул": сч[СМ_ПУЛ], "конфиг": сч[СМ_КОНФИГ],
                "минт_котировки": минт_кот,
                "в_пул": в_пул, "комиссия": комиссия, "куда": куда,
                "доля_от_чистого": (D(комиссия) / D(в_пул)) if в_пул else None,
                "доля_от_полного": (D(комиссия) / D(в_пул + комиссия))
                if (в_пул + комиссия) else None}
    return {}


def доля_по_покупке(tx: dict) -> dict:
    """Доля комиссии, решённая по одной нашей покупке. {} -- не решается."""
    for ix in инструкции(tx):
        if ix.get("programId") != PUMP_AMM:
            continue
        сырое = b58decode(ix.get("data") or "")
        if сырое[:8].hex() != ДИСК_ПОКУПКИ:
            continue
        сч = ix["accounts"]
        if len(сч) <= СМ_ХРАН_КОТИРОВКИ:
            continue
        ост = остатки_счетов(tx)
        хб, хк = ост.get(сч[СМ_ХРАН_БАЗЫ]), ост.get(сч[СМ_ХРАН_КОТИРОВКИ])
        if not хб or not хк:
            continue
        база_до, кот_до = хб["pre"], хк["pre"]
        вышло = хб["pre"] - хб["post"]
        вошло = хк["post"] - хк["pre"]
        if min(база_до, кот_до, вышло, вошло) <= 0 or вышло >= база_до:
            continue
        чистый = D(вышло) * D(кот_до) / (D(база_до) - D(вышло))
        доля = 1 - чистый / D(вошло)
        return {"пул": сч[СМ_ПУЛ], "конфиг": сч[СМ_КОНФИГ],
                "доля": доля, "вошло": вошло, "вышло": вышло,
                "база_до": база_до, "кот_до": кот_до}
    return {}


def поля_счёта(данные: bytes) -> dict:
    """Все целые поля счёта по всем смещениям: {(размер, смещение): значение}."""
    из_ = {}
    for размер in (2, 4, 8):
        for см in range(0, max(0, len(данные) - размер + 1)):
            из_[(размер, см)] = int.from_bytes(данные[см:см + размер], "little")
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--podpisi", required=True, help="подписи НАШИХ покупок Pump AMM")
    р.add_argument("--out", default="/tmp/pump_amm_komissiya.json")
    а = р.parse_args()
    список = [x.strip() for x in а.podpisi.split(",") if x.strip()]
    образцы = []
    for сг in список:
        try:
            tx = rpc("getTransaction", [сг, {"encoding": "jsonParsed",
                                              "maxSupportedTransactionVersion": 1,
                                              "commitment": "confirmed"}])
        except Exception as exc:  # noqa: BLE001
            print(f"{сг[:10]}: не прочитана ({type(exc).__name__})")
            continue
        з = комиссия_по_счетам(tx or {})
        if not з:
            print(f"{сг[:10]}: покупка Pump AMM не разобралась")
            continue
        з["подпись"] = сг
        # ДВЕ ДОЛИ, И СВЕРЯЮТСЯ ОБЕ. Комиссия может считаться и от суммы,
        # дошедшей до пула, и от полной суммы, списанной с нас: какая из них
        # выражена в базисных пунктах конфига -- ровно это и выясняется.
        з["доля"] = з["доля_от_полного"]
        образцы.append(з)
        print(f"{сг[:10]}: пул {з['пул'][:8]} в пул {з['в_пул']} "
              f"комиссия {з['комиссия']} "
              f"доля_от_полного {float(з['доля_от_полного']):.8f} "
              f"доля_от_чистого {float(з['доля_от_чистого']):.8f} "
              f"получателей {len(з['куда'])}")
    if not образцы:
        print("SBOY: ni odnoy razobrannoy pokupki")
        return 1
    # Счета пулов и конфигов -- одним запросом.
    адреса = list(dict.fromkeys([з["пул"] for з in образцы]
                                 + [з["конфиг"] for з in образцы]))
    данные = {}
    for i in range(0, len(адреса), 100):
        часть = адреса[i:i + 100]
        от = rpc("getMultipleAccounts", [часть, {"encoding": "base64"}]) or {}
        for адрес, v in zip(часть, (от.get("value") or [])):
            d64 = (((v or {}).get("data") or [None])[0])
            if d64:
                данные[адрес] = base64.b64decode(d64)
    # ПЕРЕБОР СМЕЩЕНИЙ. Смещение годится, если у КАЖДОГО образца значение в нём
    # даёт решённую долю: в базисных пунктах (1e-4) или в миллионных (1e-6).
    итог = {"образцов": len(образцы), "совпало": {},
             "доли": [{"подпись": з["подпись"], "пул": з["пул"],
                        "в_пул": з["в_пул"], "комиссия": з["комиссия"],
                        "доля_от_полного": float(з["доля_от_полного"]),
                        "доля_от_чистого": float(з["доля_от_чистого"]),
                        "куда": з["куда"]} for з in образцы]}
    for где in ("пул", "конфиг"):
        общие = None
        for з in образцы:
            сырое = данные.get(з[где])
            if not сырое:
                общие = set()
                break
            свои = set()
            цели = []
            for имя_доли in ("доля_от_полного", "доля_от_чистого"):
                д = з.get(имя_доли)
                if д is None:
                    continue
                цели.append((имя_доли, "bp", int((д * 10_000).to_integral_value())))
                цели.append((имя_доли, "ppm", int((д * 1_000_000).to_integral_value())))
            for (размер, см), знач in поля_счёта(сырое).items():
                for имя_доли, вид, цель in цели:
                    # Допуск ОДНА единица: доля считается по целым числам цепи,
                    # и округление последнего знака законно.
                    if abs(знач - цель) <= 1:
                        свои.add((размер, см, вид, имя_доли))
            общие = свои if общие is None else (общие & свои)
        итог["совпало"][где] = sorted([list(x) for x in (общие or set())])
        print(f"{где}: смещений, годных ВСЕМ образцам: {len(общие or set())} "
              f"-> {sorted(общие or set())}")
    итог["байт"] = {а_: len(д) for а_, д in данные.items()}
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(json.dumps({к: итог[к] for к in ("образцов", "совпало")},
                      ensure_ascii=False))
    return 0


def self_test() -> int:
    пройдено = провалено = 0

    def chk(что, ок, факт=""):
        nonlocal пройдено, провалено
        if ок:
            пройдено += 1
            print(f"  [ok  ] {что}")
        else:
            провалено += 1
            print(f"  [ПРОВАЛ] {что}: {факт}")

    # Доля решается ТОЧНО на придуманных, но согласованных числах: берём
    # резервы и долю, считаем выход по кривой и проверяем, что решение
    # возвращает ту же долю.
    база_до, кот_до, вошло, доля = 1_000_000_000, 500_000_000, 1_000_000, D("0.003")
    чистый = D(вошло) * (1 - доля)
    вышло = int(D(база_до) * чистый / (D(кот_до) + чистый))
    tx = {"transaction": {"message": {
            "accountKeys": [{"pubkey": f"A{i}"} for i in range(12)],
            "instructions": [{"programId": PUMP_AMM, "accounts": [f"A{i}" for i in range(12)],
                               "data": "1"}]}},
          "meta": {"preTokenBalances": [
                     {"accountIndex": 7, "uiTokenAmount": {"amount": str(база_до)}},
                     {"accountIndex": 8, "uiTokenAmount": {"amount": str(кот_до)}}],
                   "postTokenBalances": [
                     {"accountIndex": 7, "uiTokenAmount": {"amount": str(база_до - вышло)}},
                     {"accountIndex": 8, "uiTokenAmount": {"amount": str(кот_до + вошло)}}],
                   "innerInstructions": []}}
    # Данные инструкции подменяем на нужный дискриминатор.
    import types  # noqa: PLC0415
    глоб = globals()
    было = глоб["b58decode"]
    глоб["b58decode"] = lambda s: bytes.fromhex(ДИСК_ПОКУПКИ) + bytes(16)
    try:
        з = доля_по_покупке(tx)
    finally:
        глоб["b58decode"] = было
    chk(f"доля комиссии решается по покупке: {float(з.get('доля', -1)):.6f}",
        з and abs(float(з["доля"]) - 0.003) < 1e-6, з.get("доля"))
    chk("роли взяты по индексам покупки", з.get("пул") == "A0" and з.get("конфиг") == "A2")
    # Перебор смещений находит поле, положенное в известное место.
    д = bytearray(64)
    д[16:24] = (30).to_bytes(8, "little")
    поля = поля_счёта(bytes(д))
    chk("перебор смещений видит u64 на месте 16", поля[(8, 16)] == 30, поля[(8, 16)])
    chk("и не выдумывает поле за пределом", (8, 57) not in поля)
    print(f"самопроверка ставки Pump AMM: {пройдено}/{пройдено + провалено} пройдено")
    return 0 if провалено == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else main())
