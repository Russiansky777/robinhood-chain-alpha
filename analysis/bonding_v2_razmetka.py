#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Раскладка покупки кривой pump.fun BuyExactQuoteInV2 -- по цепи, без денег.

ЗАЧЕМ. 28.09 семь копий источника 6qudAN2k сели с ошибкой: программа кривой
сказала прямо --

  AnchorError caused by account: associated_user_volume_accumulator.
  Error Code: ConstraintSeeds. Error Number: 2006.
  Left: 6HX8mmdYYYZ79YMETXcLUydFq9Eef1Dy8SyRZx9Zu2Mh   (дали мы)
  Right: FJiTxtBCCeQPyXJ1RPbYPaNoM2dSvpwcqvdBaRGNhvu2  (ждала программа)

Пара одна и та же во всех семи и от минта не зависит, значит счёт выводится от
КОШЕЛЬКА. Этот модуль выясняет по цепи, а не догадкой:

  1. на каком МЕСТЕ в инструкции стоит этот счёт у нас и что там у источника;
  2. какие семена его дают -- сначала из IDL программы (если он выложен на
     цепи), иначе перебором по живым сделкам разных подписантов: годными
     считаются только те семена, что дают ОДНОВРЕМЕННО ключ источника у
     источника и ключ, который ждала программа, у нас.

Ничего не подписывает и не отправляет: только getTransaction, getAccountInfo и
getSignaturesForAddress.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import zlib

from solders.pubkey import Pubkey

BONDING = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
HELIUS = "https://mainnet.helius-rpc.com"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def урл() -> str:
    к = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    return f"{HELIUS}/?api-key={к}"


def зов(метод: str, параметры, *, таймаут: float = 60.0) -> dict:
    import urllib.request  # noqa: PLC0415

    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    try:
        зпр = urllib.request.Request(урл(), data=тело,
                                      headers={"content-type": "application/json"})
        with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
            д = json.loads(отв.read())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
    if "error" in д:
        return {"ok": False, "why_not": str(д["error"])[:200]}
    return {"ok": True, "result": д.get("result"), "why_not": None}


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    сырое = n.to_bytes((n.bit_length() + 7) // 8, "big")
    ноли = len(s) - len(s.lstrip("1"))
    return b"\x00" * ноли + сырое


def ключи_сообщения(tx: dict) -> list:
    сооб = ((tx or {}).get("transaction") or {}).get("message") or {}
    сырые = сооб.get("accountKeys") or []
    из_ = [к.get("pubkey") if isinstance(к, dict) else к for к in сырые]
    загр = ((tx or {}).get("meta") or {}).get("loadedAddresses") or {}
    return из_ + list(загр.get("writable") or []) + list(загр.get("readonly") or [])


def инструкции(tx: dict) -> list:
    сооб = ((tx or {}).get("transaction") or {}).get("message") or {}
    из_ = list(сооб.get("instructions") or [])
    for г in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        из_ += list(г.get("instructions") or [])
    return [и for и in из_ if isinstance(и, dict) and isinstance(и.get("accounts"), list)]


def инструкция_кривой(tx: dict) -> dict | None:
    """Инструкция программы кривой с её счетами и дискриминатором."""
    for и in инструкции(tx):
        if и.get("programId") != BONDING:
            continue
        данные = и.get("data") or ""
        сырое = b58decode(данные) if данные else b""
        return {"accounts": list(и["accounts"]), "disc": сырое[:8].hex(),
                 "n": len(и["accounts"])}
    return None


def имя_инструкции(tx: dict) -> str | None:
    for стр in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        если = "Program log: Instruction: "
        if str(стр).startswith(если):
            return str(стр)[len(если):][:60]
    return None


def ata(владелец: str, минт: str, программа: str) -> str:
    return str(Pubkey.find_program_address(
        [bytes(Pubkey.from_string(владелец)), bytes(Pubkey.from_string(программа)),
         bytes(Pubkey.from_string(минт))], Pubkey.from_string(ATA_PROGRAM))[0])


def пда(семена: list, программа: str = BONDING) -> str | None:
    try:
        return str(Pubkey.find_program_address(семена, Pubkey.from_string(программа))[0])
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ IDL с цепи

def адрес_idl(программа: str) -> str | None:
    """Адрес IDL-счёта Anchor: PDA от пустых семян, потом ["anchor:idl", база]."""
    try:
        прог = Pubkey.from_string(программа)
        база, _ = Pubkey.find_program_address([], прог)
        адрес = Pubkey.find_program_address([b"anchor:idl", bytes(база)], прог)[0]
        return str(адрес)
    except Exception:  # noqa: BLE001
        return None


def idl_с_цепи(программа: str = BONDING) -> dict:
    """IDL программы прямо со счёта: 8 байт метки, 32 автор, 4 длина, дальше zlib."""
    адрес = адрес_idl(программа)
    if not адрес:
        return {"ok": False, "why_not": "адрес IDL-счёта не вывелся", "адрес": None}
    о = зов("getAccountInfo", [адрес, {"encoding": "base64"}])
    if not о["ok"]:
        return {"ok": False, "why_not": о["why_not"], "адрес": адрес}
    зн = (о["result"] or {}).get("value")
    if not зн:
        return {"ok": False, "why_not": "IDL-счёта на цепи нет", "адрес": адрес}
    import base64  # noqa: PLC0415

    сырое = base64.b64decode((зн.get("data") or ["", ""])[0])
    if len(сырое) < 44:
        return {"ok": False, "why_not": f"в счёте {len(сырое)} байт", "адрес": адрес}
    длина = int.from_bytes(сырое[40:44], "little")
    тело = сырое[44:44 + длина] if длина else сырое[44:]
    for попытка in (тело, сырое[44:]):
        try:
            текст = zlib.decompress(попытка).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        try:
            return {"ok": True, "адрес": адрес, "idl": json.loads(текст), "why_not": None}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "адрес": адрес,
                     "why_not": f"IDL распакован, но не JSON: {exc}"[:160]}
    return {"ok": False, "адрес": адрес, "why_not": "тело IDL не распаковалось zlib"}


def семена_из_idl(idl: dict, имя: str) -> dict:
    """Семена счетов названной инструкции, как их объявил сам IDL."""
    из_ = {}
    for и in (idl or {}).get("instructions") or []:
        если = str(и.get("name") or "")
        if если.replace("_", "").lower() != имя.replace("_", "").lower():
            continue
        for место, счёт in enumerate(и.get("accounts") or []):
            пда_ = счёт.get("pda") or {}
            из_[счёт.get("name")] = {"место": место, "семена": пда_.get("seeds"),
                                      "программа": пда_.get("program")}
        break
    return из_


# ------------------------------------------------- перебор семян по живым фактам

СТРОКИ_СЕМЯН = [
    "user_volume_accumulator", "global_volume_accumulator",
    "associated_user_volume_acc", "assoc_user_volume_accumulator",
    "associated_user_volume", "associated_user", "user_volume_acc",
    "user_volume", "volume_accumulator", "user_accumulator",
    "associated_volume_accumulator", "user_volume_accumulator_v2",
    "global_user_volume_accumulator", "auva", "avua", "uva", "associated",
]


def перебор_семян(пары: list, *, программы: list | None = None) -> list:
    """Семена, которые дают ожидаемый ключ для КАЖДОЙ пары (кошелёк, ключ).

    пары -- [(кошелёк, ожидаемый ключ), ...]. Годным считается только тот
    вариант, что сошёлся на всех парах: одна пара может совпасть случайно.
    """
    программы = программы or [BONDING]
    глоб = {п: пда([b"global_volume_accumulator"], п) for п in программы}
    годные = []
    for прог in программы:
        г = глоб.get(прог)
        for строка in СТРОКИ_СЕМЯН:
            семя = строка.encode()
            if len(семя) > 32:
                continue
            наборы = {
                "семя+кошелёк": lambda к, с=семя: [с, bytes(Pubkey.from_string(к))],
                "кошелёк+семя": lambda к, с=семя: [bytes(Pubkey.from_string(к)), с],
            }
            if г:
                наборы["семя+глобальный+кошелёк"] = (
                    lambda к, с=семя, гг=г: [с, bytes(Pubkey.from_string(гг)),
                                              bytes(Pubkey.from_string(к))])
                наборы["семя+кошелёк+глобальный"] = (
                    lambda к, с=семя, гг=г: [с, bytes(Pubkey.from_string(к)),
                                              bytes(Pubkey.from_string(гг))])
            for опис, сборка in наборы.items():
                if all(пда(сборка(к), прог) == ждём for к, ждём in пары):
                    годные.append({"программа": прог, "семя": строка, "вид": опис})
    return годные


# --------------------------------------------------------------- разбор сделки

def разбор(подпись: str, *, кошелёк: str | None = None,
            минт: str | None = None) -> dict:
    о = зов("getTransaction", [подпись, {"encoding": "jsonParsed",
                                          "maxSupportedTransactionVersion": 1,
                                          "commitment": "confirmed"}])
    if not о["ok"]:
        return {"ok": False, "подпись": подпись, "why_not": о["why_not"]}
    tx = о["result"]
    if not tx:
        return {"ok": False, "подпись": подпись, "why_not": "транзакции нет на цепи"}
    ик = инструкция_кривой(tx)
    if not ик:
        return {"ok": False, "подпись": подпись,
                 "why_not": "инструкции программы кривой в транзакции нет"}
    ключи = ключи_сообщения(tx)
    сооб = ((tx or {}).get("transaction") or {}).get("message") or {}
    сколько_подписей = ((сооб.get("header") or {}).get("numRequiredSignatures") or 1)
    подписант = ключи[0] if ключи else None
    к = кошелёк or подписант
    метки = {}
    if к:
        метки[к] = "подписант (кошелёк)"
        for имя, семя in (("user_volume_accumulator", b"user_volume_accumulator"),):
            зн = пда([семя, bytes(Pubkey.from_string(к))])
            if зн:
                метки.setdefault(зн, f"PDA [{имя}, кошелёк]")
        if минт:
            for пр, имя in ((TOKEN, "SPL"), (TOKEN22, "Token-2022")):
                метки.setdefault(ata(к, минт, пр), f"ATA кошелька ({имя})")
    глоб = пда([b"global_volume_accumulator"])
    if глоб:
        метки.setdefault(глоб, "PDA [global_volume_accumulator]")
    места = []
    for место, ключ in enumerate(ик["accounts"]):
        места.append({"место": место, "ключ": ключ, "метка": метки.get(ключ)})
    return {"ok": True, "подпись": подпись, "слот": tx.get("slot"),
             "имя_инструкции": имя_инструкции(tx), "disc": ик["disc"],
             "счетов": ик["n"], "подписант": подписант,
             "подписантов": сколько_подписей,
             "ошибка": ((tx.get("meta") or {}).get("err")),
             "места": места}


def найти_у_источника(источник: str, минт: str, *, предел: int = 200) -> dict:
    """Покупка источника по этому минту: его же инструкция кривой."""
    о = зов("getSignaturesForAddress", [источник, {"limit": предел}])
    if not о["ok"]:
        return {"ok": False, "why_not": о["why_not"]}
    for зап in о["result"] or []:
        подпись = зап.get("signature")
        if not подпись:
            continue
        р = разбор(подпись, кошелёк=источник, минт=минт)
        if not р.get("ok"):
            continue
        if минт and минт not in [м["ключ"] for м in р["места"]]:
            continue
        return {"ok": True, "разбор": р}
    return {"ok": False, "why_not": "покупки источника по этому минту не нашлось"}


def владельцы(ключи: list) -> dict:
    """Кто владеет счетами и сколько в них данных -- словами цепи.

    Это отвечает на вопрос, какая ПРОГРАММА выводит спорный счёт: угадывать
    семена без этого бессмысленно.
    """
    из_ = {}
    for начало in range(0, len(ключи), 100):
        часть = ключи[начало:начало + 100]
        о = зов("getMultipleAccounts", [часть, {"encoding": "base64"}])
        if not о["ok"]:
            for к in часть:
                из_[к] = {"why_not": о["why_not"]}
            continue
        значения = (о["result"] or {}).get("value") or []
        for к, зн in zip(часть, значения):
            if not зн:
                из_[к] = {"есть": False}
                continue
            данные = (зн.get("data") or ["", ""])[0]
            import base64  # noqa: PLC0415
            сырое = base64.b64decode(данные) if данные else b""
            из_[к] = {"есть": True, "владелец": зн.get("owner"),
                       "лампортов": зн.get("lamports"), "байт": len(сырое),
                       "данные_b64": данные if len(сырое) <= 256 else None}
    return из_


def найти_ключи_в_данных(данные_b64: str | None, кандидаты: dict) -> list:
    """Какие известные адреса лежат в данных счёта и по каким смещениям."""
    if not данные_b64:
        return []
    import base64  # noqa: PLC0415
    сырое = base64.b64decode(данные_b64)
    из_ = []
    for имя, адрес in кандидаты.items():
        сдвиг = сырое.find(bytes(Pubkey.from_string(адрес)))
        if сдвиг >= 0:
            из_.append({"имя": имя, "адрес": адрес, "смещение": сдвиг})
    return из_


DISC_V2 = "c2ab1c46684d5b2f"


def образцы_v2(адрес: str, *, предел: int = 300, сколько: int = 6,
                disc: str = DISC_V2) -> dict:
    """УСПЕШНЫЕ покупки этой разновидности у кошелька: подписант и его места.

    Нужны именно успешные: в них место 21 стоит такое, какое программа
    принимает, и вместе с подписантом даёт пару для вывода семян.
    """
    о = зов("getSignaturesForAddress", [адрес, {"limit": предел}])
    if not о["ok"]:
        return {"ok": False, "why_not": о["why_not"], "образцы": []}
    из_ = []
    for зап in о["result"] or []:
        if len(из_) >= сколько:
            break
        if зап.get("err"):
            continue
        подпись = зап.get("signature")
        if not подпись:
            continue
        р = разбор(подпись, кошелёк=адрес)
        if not р.get("ok") or р.get("disc") != disc or р.get("ошибка"):
            continue
        места = {м["место"]: м["ключ"] for м in р["места"]}
        из_.append({"подпись": подпись, "слот": р["слот"],
                     "подписант": р["подписант"], "счетов": р["счетов"],
                     "место_19": места.get(19), "место_20": места.get(20),
                     "место_21": места.get(21), "место_22": места.get(22)})
    return {"ok": bool(из_), "образцы": из_,
             "why_not": None if из_ else f"успешных покупок disc {disc} не нашлось"}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--podpis", action="append", default=[],
                    help="подпись НАШЕЙ сделки (можно несколько)")
    р.add_argument("--koshelek", default="",
                    help="наш кошелёк (пусто -- подписант сделки)")
    р.add_argument("--mint", default="", help="минт этой сделки (для ATA-метки)")
    р.add_argument("--istochnik", default="",
                    help="адрес источника: найти его покупку того же минта")
    р.add_argument("--zhdali", default="",
                    help="ключ, который ждала программа (из журнала ошибки)")
    р.add_argument("--dali", default="",
                    help="ключ, который дали мы (из журнала ошибки)")
    р.add_argument("--obrazcy", default="",
                    help="адрес: взять его УСПЕШНЫЕ покупки V2 как образцы")
    р.add_argument("--vladelcy", action="store_true",
                    help="спросить у цепи владельца и размер каждого счёта")
    р.add_argument("--schet", action="append", default=[],
                    help="разобрать отдельный счёт (можно несколько)")
    р.add_argument("--out", default="")
    а = р.parse_args()

    из_ = {"idl": None, "sdelki": [], "istochnik": None, "semena": None}

    idl = idl_с_цепи()
    из_["idl"] = {"ok": idl["ok"], "адрес": idl.get("адрес"),
                   "why_not": idl.get("why_not")}
    if idl["ok"]:
        for имя in ("buy_exact_quote_in_v2", "buy", "buy_exact_quote_in"):
            семена = семена_из_idl(idl["idl"], имя)
            if семена:
                из_["idl"][имя] = семена
    print("IDL:", json.dumps(из_["idl"], ensure_ascii=False)[:1500])

    for подпись in а.podpis:
        р_ = разбор(подпись, кошелёк=а.koshelek or None, минт=а.mint or None)
        из_["sdelki"].append(р_)
        if not р_.get("ok"):
            print(f"{подпись[:16]}: {р_['why_not']}")
            continue
        print(f"\n{подпись[:16]} слот {р_['слот']} ix {р_['имя_инструкции']} "
              f"disc {р_['disc']} счетов {р_['счетов']} ошибка {р_['ошибка']}")
        for м in р_["места"]:
            пометка = ""
            if а.dali and м["ключ"] == а.dali:
                пометка = "  <-- ДАЛИ МЫ (на это ругалась программа)"
            if а.zhdali and м["ключ"] == а.zhdali:
                пометка = "  <-- ЖДАЛА ПРОГРАММА"
            print(f"  {м['место']:2d} {м['ключ']} {м['метка'] or ''}{пометка}")

    if а.istochnik and а.mint:
        ои = найти_у_источника(а.istochnik, а.mint)
        из_["istochnik"] = ои
        if ои.get("ok"):
            ри = ои["разбор"]
            print(f"\nисточник {а.istochnik[:8]} слот {ри['слот']} "
                  f"ix {ри['имя_инструкции']} disc {ри['disc']} счетов {ри['счетов']}")
            for м in ри["места"]:
                пометка = "  <-- ЭТОТ КЛЮЧ МЫ И СКОПИРОВАЛИ" if (
                    а.dali and м["ключ"] == а.dali) else ""
                print(f"  {м['место']:2d} {м['ключ']} {м['метка'] or ''}{пометка}")
        else:
            print("\nисточник:", ои.get("why_not"))

    # СЕМЕНА ПО ФАКТАМ: годны только те, что сходятся на ОБЕИХ парах --
    # ключ источника у источника и ожидаемый ключ у нас.
    пары = []
    ри = ((из_.get("istochnik") or {}).get("разбор") or {})
    if а.dali and ри.get("подписант"):
        пары.append((ри["подписант"], а.dali))
    наш = а.koshelek or next((с.get("подписант") for с in из_["sdelki"]
                               if с.get("ok")), None)
    if а.zhdali and наш:
        пары.append((наш, а.zhdali))
    if пары:
        годные = перебор_семян(пары)
        из_["semena"] = {"пары": пары, "годные": годные}
        print("\nсемена по фактам:", json.dumps(годные, ensure_ascii=False)
              if годные else "перебором не нашлось -- нужен IDL или больше образцов")

    # ВЛАДЕЛЬЦЫ СЧЕТОВ: какая программа держит каждое место раскладки.
    if а.vladelcy and из_["sdelki"]:
        ключи = [м["ключ"] for с in из_["sdelki"] if с.get("ok")
                  for м in с["места"]]
        в = владельцы(sorted(set(ключи)))
        из_["vladelcy"] = в
        print("\nвладельцы счетов раскладки:")
        for с in из_["sdelki"]:
            if not с.get("ok"):
                continue
            for м in с["места"]:
                зн = в.get(м["ключ"]) or {}
                print(f"  {м['место']:2d} {м['ключ']} владелец "
                      f"{зн.get('владелец') or ('нет счёта' if зн.get('есть') is False else зн.get('why_not'))} "
                      f"байт {зн.get('байт')}")

    кандидаты = {}
    if а.koshelek:
        кандидаты["наш кошелёк"] = а.koshelek
    if а.istochnik:
        кандидаты["источник"] = а.istochnik
    if а.mint:
        кандидаты["минт"] = а.mint
    if а.schet:
        в = владельцы(list(dict.fromkeys(а.schet)))
        из_["scheta"] = {}
        print("\nотдельные счета:")
        for к in dict.fromkeys(а.schet):
            зн = в.get(к) or {}
            внутри = найти_ключи_в_данных(зн.get("данные_b64"), кандидаты)
            из_["scheta"][к] = {"счёт": зн, "известные_внутри": внутри}
            print(f"  {к}: владелец {зн.get('владелец')} байт {зн.get('байт')} "
                  f"лампортов {зн.get('лампортов')} есть {зн.get('есть')}")
            for н in внутри:
                print(f"      внутри {н['имя']} на смещении {н['смещение']}")

    # ОБРАЗЦЫ УСПЕШНЫХ V2: подписант и его место 21 -- пара для вывода семян.
    if а.obrazcy:
        об = образцы_v2(а.obrazcy)
        из_["obrazcy"] = об
        print("\nобразцы успешных V2:", об.get("why_not") or f"{len(об['образцы'])} шт")
        пары = []
        for о in об.get("образцы") or []:
            своё_20 = пда([b"user_volume_accumulator",
                            bytes(Pubkey.from_string(о["подписант"]))])
            print(f"  {о['подпись'][:16]} слот {о['слот']} подписант {о['подписант']}")
            print(f"     20 {о['место_20']} {'= PDA[user_volume_accumulator, он]' if о['место_20'] == своё_20 else '(не его PDA!)'}")
            print(f"     21 {о['место_21']}")
            if о["место_21"]:
                пары.append((о["подписант"], о["место_21"]))
        наш_ = а.koshelek or None
        if а.zhdali and наш_:
            пары.append((наш_, а.zhdali))
        if пары:
            годные = перебор_семян(
                пары, программы=[BONDING, "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"])
            из_["semena_po_obrazcam"] = {"пары": пары, "годные": годные}
            print("\nсемена по образцам:", json.dumps(годные, ensure_ascii=False)
                  if годные else "перебором не нашлось")

    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(из_, ф, ensure_ascii=False, indent=1)
        print("записано:", а.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
