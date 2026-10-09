#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""СБОРЩИКИ ПОД НОВЫЕ ИНСТРУКЦИИ PUMP.FUN: кривая v2/v3 и Pump AMM v2.

ЗАЧЕМ. 08.10 в 23:27--23:38Z полоса отказала 29 раз подряд на одной строке:
«разновидность инструкции кривой не известна: e1f7501ed5b38488». Это
buy_exact_quote_in_v3 -- вариант, на который перешли источники. Пока
строитель его не знает, каждый такой сигнал пропускается молча для рынка и
громко для журнала.

ОТКУДА РАСКЛАДКИ. ТОЛЬКО из публичного IDL pump.fun, и он ЗАКРЕПЛЁН В
РЕПОЗИТОРИИ (data/idl/pump.json, data/idl/pump_amm.json, снято 09.10 с
raw.githubusercontent.com/pump-fun/pump-public-docs). Ни одного счёта и ни
одного дискриминатора по памяти: дискриминатор каждой инструкции модуль
ПЕРЕСЧИТЫВАЕТ сам по схеме Anchor -- sha256("global:<имя>")[:8] -- и сверяет
с тем, что записан в IDL. Разошлись -- отказ.

ЛОВУШКА, КОТОРАЯ СТОИТ ДЕНЕГ, И ОНА ЗДЕСЬ ЗАКРЫТА. Дискриминатор Anchor
считается ТОЛЬКО по имени, поэтому У ДВУХ РАЗНЫХ ПРОГРАММ ОДИНАКОВЫЕ ВОСЕМЬ
БАЙТ. buy_exact_quote_in_v2 -- это c2ab1c46684d5b2f и у кривой, и у Pump AMM,
НО у кривой 27 счетов, а у AMM 17. То же у buy_v2 (27/17), sell_v2 (26/17),
buy и sell. Разбирать инструкцию по одному дискриминатору -- значит однажды
собрать чужую раскладку на деньгах. Здесь ключ ВСЕГДА (программа,
дискриминатор).

ЧЕГО ЗДЕСЬ НЕТ. Ни сети, ни ключей, ни подписи, ни отправки: модуль отдаёт
счета, байты данных и провенанс каждого счёта («выведен PDA», «из
транзакции источника», «наш»). Подписывает и шлёт тот, у кого ключ.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
ИДЛ_ФАЙЛЫ = {"pump": "data/idl/pump.json", "pump_amm": "data/idl/pump_amm.json"}

ПРОГ_КРИВОЙ = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
ПРОГ_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
ПРОГ_ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
ПРОГ_ТОКЕНА = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ПРОГ_ТОКЕНА_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
WSOL = "So11111111111111111111111111111111111111112"
СИСТЕМНАЯ = "11111111111111111111111111111111"

WHY_НЕТ_IDL = "IDL не закреплён в репозитории"
WHY_НЕТ_ВАРИАНТА = "такой разновидности в IDL нет"
WHY_НЕТ_В_КОНТЕКСТЕ = "счёт не выводится и не передан"
WHY_ДИСК = "дискриминатор IDL не сошёлся с посчитанным по имени"

_АЛФАВИТ = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


class ОшибкаСборки(Exception):
    pass


# -------------------------------------------------------------- base58 и PDA

def b58d(s: str) -> bytes:
    н = 0
    for з in str(s):
        if з not in _АЛФАВИТ:
            raise ОшибкаСборки(f"не base58: {з!r} в {str(s)[:12]}…")
        н = н * 58 + _АЛФАВИТ.index(з)
    б = н.to_bytes((н.bit_length() + 7) // 8, "big") if н else b""
    return b"\x00" * (len(str(s)) - len(str(s).lstrip("1"))) + б


def b58e(b: bytes) -> str:
    н = int.from_bytes(b, "big")
    из_ = ""
    while н:
        н, о = divmod(н, 58)
        из_ = _АЛФАВИТ[о] + из_
    return "1" * (len(b) - len(bytes(b).lstrip(b"\x00"))) + из_


def na_krivoj(b: bytes) -> bool:
    """Лежит ли 32 байта на кривой ed25519. Для PDA нужен ответ «нет».

    Чистая арифметика по модулю, без внешних библиотек: solders на бегунке
    может не быть, а раскладка счетов нужна и там. Сверено с solders на живых
    адресах -- совпало, включая bump.
    """
    if len(b) != 32:
        return False
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return (b[31] >> 7) == 0
    x = pow(x2, (_P + 3) // 8, _P)
    if x * x % _P != x2:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
        if x * x % _P != x2:
            return False
    return True


def pda(semena: list, programma: str) -> tuple:
    """Адрес программы по семенам -- тем же перебором bump, что и у Solana."""
    прог = b58d(programma)
    for бамп in range(255, -1, -1):
        х = hashlib.sha256()
        for семя in semena:
            х.update(семя if isinstance(семя, (bytes, bytearray)) else b58d(семя))
        х.update(bytes([бамп]))
        х.update(прог)
        х.update(b"ProgramDerivedAddress")
        к = х.digest()
        if not na_krivoj(к):
            return b58e(к), бамп
    raise ОшибкаСборки("PDA не нашёлся ни при одном bump")


def ata(vladelec: str, programma_tokena: str, mint: str) -> str:
    """ATA по семенам программы ATA: (владелец, программа токена, минт)."""
    return pda([vladelec, programma_tokena, mint], ПРОГ_ATA)[0]


# ------------------------------------------------------------------- IDL

_ИДЛ: dict = {}


def zagruzit_idl(*, zanovo: bool = False) -> dict:
    """IDL из репозитория. Дискриминаторы ПЕРЕСЧИТЫВАЮТСЯ и сверяются."""
    if _ИДЛ and not zanovo:
        return _ИДЛ
    из_ = {}
    for имя, путь in ИДЛ_ФАЙЛЫ.items():
        п = КОРЕНЬ / путь
        if not п.exists():
            raise ОшибкаСборки(f"{WHY_НЕТ_IDL}: {путь}")
        д = json.loads(п.read_text(encoding="utf-8"))
        ин = {}
        расхождения = []
        for их in д.get("instructions") or []:
            имя_и = их["name"]
            объявлен = bytes(их.get("discriminator") or b"")
            посчитан = hashlib.sha256(f"global:{имя_и}".encode()).digest()[:8]
            if объявлен != посчитан:
                расхождения.append(имя_и)
            ин[имя_и] = {"disc": посчитан, "accounts": их.get("accounts") or [],
                          "args": их.get("args") or []}
        если = {"adres": д.get("address"), "ix": ин, "rashozhdenija": расхождения,
                "tipy": {т["name"]: т for т in (д.get("types") or [])}}
        из_[имя] = если
    _ИДЛ.clear()
    _ИДЛ.update(из_)
    return _ИДЛ


def programma_po_imeni(imja: str) -> str:
    return {"pump": ПРОГ_КРИВОЙ, "pump_amm": ПРОГ_AMM}[imja]


def variant_po_disku(imja_programmy: str, disc: bytes) -> str | None:
    """Имя разновидности по (ПРОГРАММА, дискриминатор). Ключ -- пара."""
    идл = zagruzit_idl()[imja_programmy]
    for имя, их in идл["ix"].items():
        if их["disc"] == bytes(disc)[:8]:
            return имя
    return None


# --------------------------------------------------------- данные инструкции

def zakodirovat_argumenty(imja_programmy: str, variant: str, znachenija: dict) -> bytes:
    """Байты инструкции: дискриминатор плюс аргументы в порядке IDL.

    Поддержаны ровно те типы, что встречаются у наших разновидностей: u64,
    bool и OptionBool (это СТРУКТУРА с одним полем bool, а не Option -- один
    байт, и перепутать их значит собрать на один байт короче).
    """
    идл = zagruzit_idl()[imja_programmy]
    их = идл["ix"].get(variant)
    if not их:
        raise ОшибкаСборки(f"{WHY_НЕТ_ВАРИАНТА}: {imja_programmy}.{variant}")
    из_ = bytearray(их["disc"])
    for арг in их["args"]:
        имя, тип = арг["name"], арг["type"]
        if имя not in znachenija:
            raise ОшибкаСборки(f"аргумент не задан: {variant}.{имя}")
        з = znachenija[имя]
        if тип == "u64":
            из_ += struct.pack("<Q", int(з))
        elif тип == "bool":
            из_ += bytes([1 if з else 0])
        elif isinstance(тип, dict) and (тип.get("defined") or {}).get("name") == "OptionBool":
            из_ += bytes([1 if з else 0])
        else:
            raise ОшибкаСборки(f"тип аргумента не поддержан: {имя}: {тип!r}")
    return bytes(из_)


def razobrat_argumenty(imja_programmy: str, variant: str, dannye: bytes) -> dict:
    """Обратно: из байтов -- значения. Нужна для разбора сделки источника."""
    идл = zagruzit_idl()[imja_programmy]
    их = идл["ix"].get(variant)
    if not их:
        raise ОшибкаСборки(f"{WHY_НЕТ_ВАРИАНТА}: {imja_programmy}.{variant}")
    б = bytes(dannye)
    if б[:8] != их["disc"]:
        raise ОшибкаСборки("дискриминатор данных не тот")
    м = 8
    из_ = {}
    for арг in их["args"]:
        имя, тип = арг["name"], арг["type"]
        if тип == "u64":
            из_[имя] = struct.unpack_from("<Q", б, м)[0]
            м += 8
        elif тип == "bool" or (isinstance(тип, dict)
                               and (тип.get("defined") or {}).get("name") == "OptionBool"):
            из_[имя] = bool(б[м]) if м < len(б) else None
            м += 1
        else:
            raise ОшибкаСборки(f"тип аргумента не поддержан: {имя}: {тип!r}")
    из_["_bajt"] = м
    из_["_hvost"] = len(б) - м
    return из_


# ------------------------------------------------------- сборка счетов

ПРОВ_КОНСТАНТА = "константа IDL"
ПРОВ_PDA = "выведен PDA по семенам IDL"
ПРОВ_ATA = "выведен ATA (в IDL семян нет -- наше правило, сверено с цепью)"
ПРОВ_ПЕРЕДАН = "передан в контексте"

# ИМЕНА, КОТОРЫЕ ВЫВОДЯТСЯ КАК ATA ВЛАДЕЛЬЦА, ХОТЯ В IDL СЕМЯН НЕТ.
# Пара «владелец, минт» для каждого -- и ни одного имени наугад: все четыре
# сверены с живыми транзакциями из репозитория.
КАК_ATA = {
    "associated_base_user": ("user", "base_token_program", "base_mint"),
    "associated_quote_user": ("user", "quote_token_program", "quote_mint"),
    "user_base_token_account": ("user", "base_token_program", "base_mint"),
    "user_quote_token_account": ("user", "quote_token_program", "quote_mint"),
}


def _semja(s: dict, gotovo: dict, kontekst: dict,
           konstanty: dict | None = None) -> bytes:
    вид = s.get("kind")
    if вид == "const":
        return bytes(s.get("value") or b"")
    путь = s.get("path") or ""
    # ПУСТОЕ ЗНАЧЕНИЕ -- ЭТО ОТСУТСТВУЮЩЕЕ ЗНАЧЕНИЕ. Пока проверки на пустоту
    # здесь не было, creator="" давал b58d("") = пустое семя, и PDA выводился
    # БЕЗ ОШИБКИ -- то есть хранилище создателя считалось не то, и комиссия
    # создателя ушла бы чужому адресу. Молча считать такое нельзя.
    for откуда in ((konstanty or {}), gotovo, kontekst):
        if путь in откуда:
            if not откуда[путь]:
                raise ОшибкаСборки(
                    f"{WHY_НЕТ_В_КОНТЕКСТЕ}: семя {путь!r} передано пустым")
            return b58d(откуда[путь])
    # Путь через точку -- поле СЧЁТА (например bonding_curve.creator). Его в
    # байтах транзакции нет: это чтение счёта, и оно должно прийти в контексте
    # ровно под этим именем. Выдумывать его нельзя.
    raise ОшибкаСборки(f"{WHY_НЕТ_В_КОНТЕКСТЕ}: семя {путь!r}")


def sobrat_scheta(imja_programmy: str, variant: str, kontekst: dict) -> dict:
    """Счета разновидности В ПОРЯДКЕ IDL, с провенансом каждого."""
    идл = zagruzit_idl()[imja_programmy]
    их = идл["ix"].get(variant)
    if not их:
        raise ОшибкаСборки(f"{WHY_НЕТ_ВАРИАНТА}: {imja_programmy}.{variant}")
    сама = programma_po_imeni(imja_programmy)
    # ПОСТОЯННЫЕ АДРЕСА САМОГО IDL СОБИРАЮТСЯ ЗАРАНЕЕ. У fee_config семена
    # считаются ПОД ЧУЖОЙ ПРОГРАММОЙ, и её имя (fee_program) стоит в списке
    # ПОЗЖЕ него. Без этого прохода программа бралась своя, и fee_config
    # выводился не тот -- ровно на этом месте у меня и разошлись 11 живых
    # транзакций из 11, пока проход не появился.
    константы = {а["name"]: а["address"] for а in их["accounts"]
                 if а.get("address")}
    готово: dict = {}
    строки = []
    for а in их["accounts"]:
        имя = а["name"]
        адрес = провенанс = None
        if а.get("address"):
            адрес, провенанс = а["address"], ПРОВ_КОНСТАНТА
        elif имя in kontekst and kontekst[имя]:
            адрес, провенанс = str(kontekst[имя]), ПРОВ_ПЕРЕДАН
        elif а.get("pda"):
            п = а["pda"]
            семена = [_semja(с, готово, kontekst, константы)
                      for с in п.get("seeds") or []]
            прог = сама
            пр = п.get("program") or {}
            if пр.get("value"):
                прог = b58e(bytes(пр["value"]))
            elif пр.get("path"):
                прог = (готово.get(пр["path"]) or kontekst.get(пр["path"])
                        or константы.get(пр["path"]) or сама)
            адрес, _ = pda(семена, прог)
            провенанс = ПРОВ_PDA
        elif имя in КАК_ATA:
            вл, тп, мн = КАК_ATA[имя]
            нужно = [и for и in (вл, тп, мн) if не_задан(и, готово, kontekst)]
            if нужно:
                raise ОшибкаСборки(f"{WHY_НЕТ_В_КОНТЕКСТЕ}: для {имя} нет {нужно}")
            адрес = ata(_znach(вл, готово, kontekst), _znach(тп, готово, kontekst),
                        _znach(мн, готово, kontekst))
            провенанс = ПРОВ_ATA
        else:
            raise ОшибкаСборки(f"{WHY_НЕТ_В_КОНТЕКСТЕ}: {имя}")
        готово[имя] = адрес
        строки.append({"imja": имя, "pubkey": адрес,
                        "isSigner": bool(а.get("signer")),
                        "isWritable": bool(а.get("writable")),
                        "otkuda": провенанс})
    return {"programma": сама, "variant": variant, "accounts": строки,
            "schetov": len(строки)}


def не_задан(имя: str, gotovo: dict, kontekst: dict) -> bool:
    return not (gotovo.get(имя) or kontekst.get(имя))


def _znach(имя: str, gotovo: dict, kontekst: dict) -> str:
    return str(gotovo.get(имя) or kontekst.get(имя))


def sobrat(imja_programmy: str, variant: str, kontekst: dict,
           argumenty: dict) -> dict:
    """ОДНА ИНСТРУКЦИЯ ЦЕЛИКОМ: счета, байты данных, провенанс. Без подписи."""
    сч = sobrat_scheta(imja_programmy, variant, kontekst)
    д = zakodirovat_argumenty(imja_programmy, variant, argumenty)
    сч.update(data_hex=д.hex(), data_b58=b58e(д), bajt_dannyh=len(д),
              argumenty=dict(argumenty))
    return сч


# ------------------------------------------- разбор инструкции источника

def instrukcii_programmy(tx: dict, programma: str) -> list:
    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    из_ = [и for и in (сообщение.get("instructions") or [])
           if и.get("programId") == programma]
    for вн in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        из_ += [и for и in (вн.get("instructions") or [])
                if и.get("programId") == programma]
    return из_


def razbor_istochnika(tx: dict, *, imja_programmy: str | None = None) -> dict:
    """ЧТО СДЕЛАЛ ИСТОЧНИК: разновидность, аргументы и счета ПО ИМЕНАМ IDL.

    Ключ разбора -- ПАРА (программа, дискриминатор), а не один дискриминатор:
    у кривой и у AMM они совпадают при разных раскладках.
    """
    из_ = {"ok": False, "why_not": None, "programma": None, "variant": None,
            "argumenty": None, "scheta": {}, "schetov": 0, "disc": None}
    пары = ([(imja_programmy, programma_po_imeni(imja_programmy))]
            if imja_programmy else [("pump", ПРОГ_КРИВОЙ), ("pump_amm", ПРОГ_AMM)])
    for имя_п, адрес_п in пары:
        for их in instrukcii_programmy(tx, адрес_п):
            дан = их.get("data")
            if not дан:
                continue
            try:
                б = b58d(дан)
            except ОшибкаСборки:
                continue
            if len(б) < 8:
                continue
            вариант = variant_po_disku(имя_п, б[:8])
            из_.update(programma=имя_п, disc=б[:8].hex())
            if not вариант:
                из_["why_not"] = (f"разновидность {имя_п} не известна: "
                                   f"{б[:8].hex()}")
                continue
            идл = zagruzit_idl()[имя_п]["ix"][вариант]
            счета = list(их.get("accounts") or [])
            по_именам = {а["name"]: (счета[и] if и < len(счета) else None)
                         for и, а in enumerate(идл["accounts"])}
            try:
                арг = razobrat_argumenty(имя_п, вариант, б)
            except ОшибкаСборки as сбой:
                из_["why_not"] = str(сбой)
                continue
            из_.update(ok=True, variant=вариант, argumenty=арг,
                       scheta=по_именам, schetov=len(счета),
                       schetov_idl=len(идл["accounts"]),
                       schetov_sovpalo=len(счета) == len(идл["accounts"]))
            return из_
    из_["why_not"] = из_["why_not"] or "инструкции pump в транзакции нет"
    return из_


# ------------------------------- п.3: покупка в свежий пул по переезду

ВАРИАНТЫ_ПЕРЕЕЗДА = ("migrate", "migrate_v2")


def kontekst_iz_pereezda(tx: dict, *, nash_koshelek: str) -> dict:
    """КОНТЕКСТ ПОКУПКИ В СВЕЖИЙ ПУЛ -- из самой транзакции переезда.

    Источника тут нет и быть не может: пул только что создан, в нём ещё никто
    не торговал. Все счета берутся из переезда (migrate/migrate_v2 на кривой и
    create_pool Pump AMM в ТОЙ ЖЕ транзакции), а наши -- выводятся.
    """
    из_ = {"ok": False, "why_not": None, "kontekst": {}, "pool": None,
            "variant_pereezda": None}
    переезд = None
    for их in instrukcii_programmy(tx, ПРОГ_КРИВОЙ):
        дан = их.get("data")
        if not дан:
            continue
        try:
            б = b58d(дан)
        except ОшибкаСборки:
            continue
        в = variant_po_disku("pump", б[:8]) if len(б) >= 8 else None
        if в in ВАРИАНТЫ_ПЕРЕЕЗДА:
            переезд = (в, их)
            break
    if not переезд:
        из_["why_not"] = ("в транзакции нет ни migrate, ни migrate_v2 -- это "
                          "не переезд")
        return из_
    создание = None
    for их in instrukcii_programmy(tx, ПРОГ_AMM):
        дан = их.get("data")
        if not дан:
            continue
        try:
            б = b58d(дан)
        except ОшибкаСборки:
            continue
        if len(б) >= 8 and variant_po_disku("pump_amm", б[:8]) == "create_pool":
            создание = их
            break
    if not создание:
        из_["why_not"] = ("в той же транзакции нет create_pool Pump AMM -- "
                          "пула ещё нет, покупать не во что")
        return из_
    идл_с = zagruzit_idl()["pump_amm"]["ix"]["create_pool"]["accounts"]
    счета = list(создание.get("accounts") or [])
    по_именам = {а["name"]: (счета[и] if и < len(счета) else None)
                 for и, а in enumerate(идл_с)}
    нужно = ("pool", "global_config", "base_mint", "quote_mint",
             "base_token_program", "quote_token_program",
             "pool_base_token_account", "pool_quote_token_account")
    нет = [и for и in нужно if not по_именам.get(и)]
    if нет:
        из_["why_not"] = (f"в create_pool не нашлись счета: {нет} -- "
                           f"раскладка переезда другая, вслепую не собираю")
        return из_
    к = {и: по_именам[и] for и in нужно}
    к["user"] = nash_koshelek
    из_.update(ok=True, kontekst=к, pool=к["pool"],
               variant_pereezda=переезд[0])
    return из_


# ------------------------------- сборка по-нашему: покупка и продажа

# ЧТО СТАВИТЬ СЕЙЧАС. 08.10 источники ушли на v3: 26 из 29 отказов полосы --
# e1f7501ed5b38488 (buy_exact_quote_in_v3). У v3 СЕМНАДЦАТЬ счетов, и ВСЕ
# выводятся из шести значений: два минта, две программы токена, наш кошелёк и
# получатель buyback. Транзакция источника для сборки НЕ НУЖНА ВОВСЕ -- это и
# есть главная разница с v2, где счетов 27 и часть берётся из чужой сделки.
НУЖНО_ДЛЯ_КРИВОЙ_V3 = ("base_mint", "quote_mint", "base_token_program",
                       "quote_token_program", "user", "buyback_fee_recipient")
НУЖНО_ДЛЯ_AMM_V2 = ("pool", "user", "global_config", "base_mint", "quote_mint",
                    "base_token_program", "quote_token_program",
                    "pool_base_token_account", "pool_quote_token_account",
                    "buyback_fee_recipient")


def pokupka_krivoj_v3(*, base_mint: str, user: str, buyback_fee_recipient: str,
                      spendable_quote_in: int, min_tokens_out: int,
                      quote_mint: str = WSOL,
                      base_token_program: str = ПРОГ_ТОКЕНА,
                      quote_token_program: str = ПРОГ_ТОКЕНА,
                      partial_fill: bool = False) -> dict:
    """НАША ПОКУПКА кривой разновидностью, которую программа принимает сейчас."""
    к = {"base_mint": base_mint, "quote_mint": quote_mint,
         "base_token_program": base_token_program,
         "quote_token_program": quote_token_program, "user": user,
         "buyback_fee_recipient": buyback_fee_recipient}
    return sobrat("pump", "buy_exact_quote_in_v3", к,
                  {"spendable_quote_in": int(spendable_quote_in),
                   "min_tokens_out": int(min_tokens_out),
                   "partial_fill": bool(partial_fill)})


def prodazha_krivoj_v3(*, base_mint: str, user: str,
                       buyback_fee_recipient: str, amount: int,
                       min_sol_output: int, quote_mint: str = WSOL,
                       base_token_program: str = ПРОГ_ТОКЕНА,
                       quote_token_program: str = ПРОГ_ТОКЕНА) -> dict:
    """НАША ПРОДАЖА кривой: sell_v3, те же 17 счетов."""
    к = {"base_mint": base_mint, "quote_mint": quote_mint,
         "base_token_program": base_token_program,
         "quote_token_program": quote_token_program, "user": user,
         "buyback_fee_recipient": buyback_fee_recipient}
    return sobrat("pump", "sell_v3", к,
                  {"amount": int(amount), "min_sol_output": int(min_sol_output)})


def prodazha_krivoj_v2(*, base_mint: str, user: str, fee_recipient: str,
                       buyback_fee_recipient: str, creator: str, amount: int,
                       min_sol_output: int, quote_mint: str = WSOL,
                       base_token_program: str = ПРОГ_ТОКЕНА,
                       quote_token_program: str = ПРОГ_ТОКЕНА) -> dict:
    """ПРОДАЖА sell_v2 -- 26 счетов; `creator` берётся из события сделки."""
    к = {"base_mint": base_mint, "quote_mint": quote_mint,
         "base_token_program": base_token_program,
         "quote_token_program": quote_token_program, "user": user,
         "fee_recipient": fee_recipient,
         "buyback_fee_recipient": buyback_fee_recipient,
         "bonding_curve.creator": creator}
    return sobrat("pump", "sell_v2", к,
                  {"amount": int(amount), "min_sol_output": int(min_sol_output)})


def amm_v2(*, variant: str, kontekst: dict, argumenty: dict) -> dict:
    """Pump AMM: buy_v2 / buy_exact_quote_in_v2 / sell_v2 -- по 17 счетов."""
    if variant not in ("buy_v2", "buy_exact_quote_in_v2", "sell_v2"):
        raise ОшибкаСборки(f"{WHY_НЕТ_ВАРИАНТА}: pump_amm.{variant}")
    нет = [и for и in НУЖНО_ДЛЯ_AMM_V2 if not kontekst.get(и)]
    if нет:
        raise ОшибкаСборки(f"{WHY_НЕТ_В_КОНТЕКСТЕ}: {нет}")
    return sobrat("pump_amm", variant, kontekst, argumenty)


def pokupka_v_svezhij_pul(tx_pereezda: dict, *, nash_koshelek: str,
                          buyback_fee_recipient: str,
                          spendable_quote_in: int, min_base_amount_out: int,
                          variant: str = "buy_exact_quote_in_v2") -> dict:
    """П.3: ПОКУПКА В ТОЛЬКО ЧТО СОЗДАННЫЙ ПУЛ -- по транзакции переезда."""
    к = kontekst_iz_pereezda(tx_pereezda, nash_koshelek=nash_koshelek)
    if not к["ok"]:
        return {"ok": False, "why_not": к["why_not"]}
    кон = dict(к["kontekst"])
    кон["buyback_fee_recipient"] = buyback_fee_recipient
    из_ = amm_v2(variant=variant, kontekst=кон,
                 argumenty={"spendable_quote_in": int(spendable_quote_in),
                            "min_base_amount_out": int(min_base_amount_out)}
                 if variant == "buy_exact_quote_in_v2" else
                 {"base_amount_out": int(min_base_amount_out),
                  "max_quote_amount_in": int(spendable_quote_in)})
    из_.update(ok=True, pool=к["pool"], variant_pereezda=к["variant_pereezda"])
    return из_


# ------------------------------------------- живые образцы из репозитория

СОБЫТИЕ_СДЕЛКИ_ДИСК = bytes.fromhex("bddb7fd34ee661ee")
АНКОР_СОБЫТИЕ_ДИСК = bytes.fromhex("e445a52e51cb9a1d")
# Смещение поля creator в теле события: mint 32, sol 8, token 8, is_buy 1,
# user 32, timestamp 8, четыре резерва по 8, fee_recipient 32, fee_bps 8, fee 8.
СМЕЩЕНИЕ_CREATOR = 32 + 8 + 8 + 1 + 32 + 8 + 8 * 4 + 32 + 8 + 8


def creator_iz_sobytija(tx: dict) -> str | None:
    """Создатель кривой из события сделки. Его нет в счетах -- только в логе."""
    import base64  # noqa: PLC0415

    for строка in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        if "Program data: " not in строка:
            continue
        try:
            б = base64.b64decode(строка.split("Program data: ", 1)[1])
        except (ValueError, TypeError):
            continue
        if б[:8] == АНКОР_СОБЫТИЕ_ДИСК:
            б = б[8:]
        if б[:8] != СОБЫТИЕ_СДЕЛКИ_ДИСК:
            continue
        т = б[8:]
        if len(т) >= СМЕЩЕНИЕ_CREATOR + 32:
            return b58e(т[СМЕЩЕНИЕ_CREATOR:СМЕЩЕНИЕ_CREATOR + 32])
    return None


def zhivyje_tranzakcii(*, kat: str | Path | None = None) -> list:
    """Все транзакции с инструкциями pump из данных репозитория. Без сети."""
    к = Path(kat) if kat else (КОРЕНЬ / "data")
    найдено = []

    def _обход(о):
        if isinstance(о, dict):
            if isinstance(о.get("meta"), dict) and isinstance(
                    о.get("transaction"), dict):
                найдено.append(о)
                return
            for з in о.values():
                _обход(з)
        elif isinstance(о, list):
            for з in о:
                _обход(з)

    for ф in sorted(к.rglob("*.json")):
        try:
            if ф.stat().st_size > 40_000_000:
                continue
            т = ф.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if (ПРОГ_КРИВОЙ not in т and ПРОГ_AMM not in т) or '"preBalances"' not in т:
            continue
        try:
            _обход(json.loads(т))
        except ValueError:
            continue
    return найдено


def svod_po_zhivym(*, tranzakcii: list | None = None) -> dict:
    """СВОД ПРОВЕРКИ ПО ЖИВЫМ ОБРАЗЦАМ: вариант -> сколько и сколько сошлось.

    Два разных числа, и их нельзя путать:
      * `vyvedeno/soshlos` -- счета, которые модуль ВЫВОДИТ сам, сверены с
        теми, что стоят в настоящей транзакции. Считается только там, где
        живая раскладка совпадает с нынешним IDL по числу счетов;
      * `bajt_v_bajt` -- разобранные аргументы, закодированные обратно, дали
        РОВНО те же байты.
    """
    из_: dict = {}
    for tx in (tranzakcii if tranzakcii is not None else zhivyje_tranzakcii()):
        создатель = creator_iz_sobytija(tx)
        for имя_п, адрес_п in (("pump", ПРОГ_КРИВОЙ), ("pump_amm", ПРОГ_AMM)):
            for их in instrukcii_programmy(tx, адрес_п):
                дан = их.get("data")
                if not дан:
                    continue
                try:
                    б = b58d(дан)
                except ОшибкаСборки:
                    continue
                if len(б) < 8:
                    continue
                вариант = variant_po_disku(имя_п, б[:8])
                if not вариант:
                    continue
                з = из_.setdefault((имя_п, вариант), {
                    "obrazcov": 0, "raskladka_sovpala": 0, "vyvedeno": 0,
                    "soshlos": 0, "bajt_v_bajt": 0, "raznica": []})
                з["obrazcov"] += 1
                идл = zagruzit_idl()[имя_п]["ix"][вариант]
                try:
                    арг = razobrat_argumenty(имя_п, вариант, б)
                    снова = zakodirovat_argumenty(
                        имя_п, вариант,
                        {k: v for k, v in арг.items() if not k.startswith("_")})
                    з["bajt_v_bajt"] += (снова == б and арг["_hvost"] == 0)
                except (ОшибкаСборки, struct.error, IndexError):
                    pass
                счета = list(их.get("accounts") or [])
                if len(счета) != len(идл["accounts"]):
                    continue
                з["raskladka_sovpala"] += 1
                по = {а["name"]: счета[и]
                      for и, а in enumerate(идл["accounts"])}
                кон = {к_: по[к_] for к_ in по if к_ in (
                    "base_mint", "quote_mint", "base_token_program",
                    "quote_token_program", "user", "fee_recipient",
                    "buyback_fee_recipient", "pool", "global_config",
                    "pool_base_token_account", "pool_quote_token_account")}
                if создатель:
                    кон["bonding_curve.creator"] = создатель
                try:
                    соб = sobrat_scheta(имя_п, вариант, кон)
                except ОшибкаСборки:
                    continue
                for а, живой in zip(соб["accounts"], счета):
                    if а["otkuda"] == ПРОВ_ПЕРЕДАН:
                        continue
                    з["vyvedeno"] += 1
                    if а["pubkey"] == живой:
                        з["soshlos"] += 1
                    else:
                        з["raznica"].append((а["imja"], а["pubkey"][:8],
                                             str(живой)[:8]))
    return из_


def krivaja_iz_minta_v_zhivyh(*, tranzakcii: list | None = None) -> dict:
    """Счёт кривой, выведенный ИЗ МИНТА, обязан стоять в живой инструкции.

    Эта проверка идёт по ВСЕМ разновидностям кривой, включая те, чья живая
    раскладка старше нынешнего IDL: минт стоит в них на своём месте, а
    остальное могло поехать.
    """
    из_ = {"sverjeno": 0, "soshlos": 0, "variantov": set()}
    for tx in (tranzakcii if tranzakcii is not None else zhivyje_tranzakcii()):
        for их in instrukcii_programmy(tx, ПРОГ_КРИВОЙ):
            дан = их.get("data")
            if not дан:
                continue
            try:
                б = b58d(дан)
            except ОшибкаСборки:
                continue
            if len(б) < 8:
                continue
            вариант = variant_po_disku("pump", б[:8])
            if not вариант:
                continue
            идл = zagruzit_idl()["pump"]["ix"][вариант]["accounts"]
            счета = list(их.get("accounts") or [])
            по = {а["name"]: (счета[и] if и < len(счета) else None)
                  for и, а in enumerate(идл)}
            минт = по.get("base_mint") or по.get("mint")
            if not минт:
                continue
            кривая, _ = pda([b"bonding-curve", b58d(минт)], ПРОГ_КРИВОЙ)
            из_["sverjeno"] += 1
            из_["soshlos"] += (кривая in счета)
            из_["variantov"].add(вариант)
    из_["variantov"] = sorted(из_["variantov"])
    return из_


# ------------------------------------------------------------- самопроверка

ZHDEM_PROVEROK = 24
# ЧИСЛА ЗАМЕРА ПО ЖИВЫМ ОБРАЗЦАМ РЕПОЗИТОРИЯ (09.10). Меньше -- значит образцы
# подменили или разбор сломался; больше -- значит образцов прибавилось, и это
# тоже надо увидеть, а не проглотить.
ЖДЁМ_ЖИВЫХ = {"tranzakcij": 150, "vyvedeno": 229, "soshlos": 229,
              "bajt_v_bajt": 29, "krivaja_sverjeno": 23, "krivaja_soshlos": 23}
# Отказ полосы 08.10 23:27--23:38Z (журнал Code-1, data/journal_grep.json):
# 29 строк, все с этим дискриминатором, три разных минта.
ОТКАЗ_08_10 = {"disc": "e1f7501ed5b38488", "variant": "buy_exact_quote_in_v3",
               "schetov": 17, "strok": 29,
               "minty": ("835HxB85aavDHapqypzY6Kc6Ss9Jxo1C6Ea7Gv9tNNit",
                         "EUDqn3D6PdFdPJoWz7qqohJntquwHnvy63qZjFEX7Nzj",
                         "G95jsFkxJfTogGKdbEa81An13bYM9NZNeYdCMrJGH5ib")}
НАШИ_ВАРИАНТЫ = (("pump", "buy_v3", 17), ("pump", "buy_exact_quote_in_v3", 17),
                 ("pump", "sell_v3", 17), ("pump", "sell_v2", 26),
                 ("pump", "buy_exact_quote_in_v2", 27),
                 ("pump_amm", "buy_v2", 17),
                 ("pump_amm", "buy_exact_quote_in_v2", 17),
                 ("pump_amm", "sell_v2", 17))


def self_test() -> int:  # noqa: C901, PLR0912, PLR0915
    было, плохо = 0, 0
    упавшие: list = []

    def chk(имя, усл, факт=None):
        nonlocal было, плохо
        было += 1
        if усл:
            print(f"  ok   {имя}")
        else:
            плохо += 1
            упавшие.append(имя)
            print(f" ПЛОХО {имя} -- {факт!r}")

    print("c3_pump_sborka: самопроверка")
    идл = zagruzit_idl()
    # ---------------------------------------------- 1. IDL И ДИСКРИМИНАТОРЫ
    chk("IDL обеих программ закреплён В РЕПОЗИТОРИИ и адреса в нём те самые",
        идл["pump"]["adres"] == ПРОГ_КРИВОЙ
        and идл["pump_amm"]["adres"] == ПРОГ_AMM
        and all((КОРЕНЬ / п).exists() for п in ИДЛ_ФАЙЛЫ.values()),
        (идл["pump"]["adres"], идл["pump_amm"]["adres"]))
    chk(f"дискриминатор КАЖДОЙ инструкции пересчитан по sha256('global:имя') и "
        f"сошёлся с IDL: {len(идл['pump']['ix'])} у кривой и "
        f"{len(идл['pump_amm']['ix'])} у AMM, расхождений ноль",
        not идл["pump"]["rashozhdenija"] and not идл["pump_amm"]["rashozhdenija"],
        (идл["pump"]["rashozhdenija"], идл["pump_amm"]["rashozhdenija"]))
    # ---------------------------------------------- 2. ОТКАЗ 08.10 ОПОЗНАН
    в = variant_po_disku("pump", bytes.fromhex(ОТКАЗ_08_10["disc"]))
    chk(f"дискриминатор живого отказа {ОТКАЗ_08_10['disc']} -- это "
        f"{ОТКАЗ_08_10['variant']} у кривой, и счетов у него "
        f"{ОТКАЗ_08_10['schetov']}",
        в == ОТКАЗ_08_10["variant"]
        and len(идл["pump"]["ix"][в]["accounts"]) == ОТКАЗ_08_10["schetov"], в)
    chk("второй живой отказ c2ab1c46684d5b2f -- это buy_exact_quote_in_v2 "
        "Pump AMM, и у НЕГО тоже 17 счетов",
        variant_po_disku("pump_amm", bytes.fromhex("c2ab1c46684d5b2f"))
        == "buy_exact_quote_in_v2"
        and len(идл["pump_amm"]["ix"]["buy_exact_quote_in_v2"]["accounts"]) == 17,
        None)
    # ---------------------------------------------- 3. ЛОВУШКА ОБЩИХ ДИСКОВ
    общие = []
    for имя_и, их in идл["pump"]["ix"].items():
        for имя_а, иа in идл["pump_amm"]["ix"].items():
            if их["disc"] == иа["disc"]:
                общие.append((имя_и, имя_а, len(их["accounts"]),
                              len(иа["accounts"])))
    разные = [о for о in общие if о[2] != о[3]]
    chk(f"ЛОВУШКА НАЗВАНА ЧИСЛОМ: у двух программ {len(общие)} общих "
        f"дискриминаторов, и у {len(разные)} из них РАЗНОЕ число счетов -- "
        "разбирать по одному дискриминатору значит собрать чужую раскладку",
        len(общие) >= 5 and len(разные) >= 5
        and any(о[0] == "buy_exact_quote_in_v2" and о[2] == 27 and о[3] == 17
                for о in общие), разные[:4])
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: один и тот же дискриминатор даёт РАЗНЫЕ "
        "разновидности в зависимости от программы -- ключ всегда пара",
        variant_po_disku("pump", bytes.fromhex("b817ee6167c5d33d")) == "buy_v2"
        and len(идл["pump"]["ix"]["buy_v2"]["accounts"]) == 27
        and len(идл["pump_amm"]["ix"]["buy_v2"]["accounts"]) == 17, None)
    # ---------------------------------------------- 4. PDA
    гл, бамп = pda([b"global"], ПРОГ_КРИВОЙ)
    со, _ = pda([b"__event_authority"], ПРОГ_КРИВОЙ)
    chk("PDA считается чистой арифметикой без внешних библиотек и даёт те же "
        f"адреса, что и цепь: global {гл[:12]}… bump {бамп}",
        гл == "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf" and бамп == 255
        and со == "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1", (гл, бамп, со))
    chk("точка на кривой ed25519 отличается от не-точки: у PDA ответ обязан "
        "быть «не на кривой»",
        not na_krivoj(b58d(гл)) and na_krivoj(b58d(ПРОГ_КРИВОЙ)), None)
    # ---------------------------------------------- 5. ЖИВЫЕ ОБРАЗЦЫ
    живые = zhivyje_tranzakcii()
    свод = svod_po_zhivym(tranzakcii=живые)
    выведено = sum(з["vyvedeno"] for з in свод.values())
    сошлось = sum(з["soshlos"] for з in свод.values())
    байты = sum(з["bajt_v_bajt"] for з in свод.values())
    расхождения = [р for з in свод.values() for р in з["raznica"]]
    chk(f"живых транзакций с pump в репозитории {ЖДЁМ_ЖИВЫХ['tranzakcij']}",
        len(живые) == ЖДЁМ_ЖИВЫХ["tranzakcij"], len(живые))
    chk(f"ВЫВЕДЕННЫЕ СЧЕТА СВЕРЕНЫ С ЦЕПЬЮ: {ЖДЁМ_ЖИВЫХ['soshlos']} из "
        f"{ЖДЁМ_ЖИВЫХ['vyvedeno']} на живых транзакциях совпали АДРЕС В АДРЕС "
        "(там, где живая раскладка совпадает с нынешним IDL)",
        выведено == ЖДЁМ_ЖИВЫХ["vyvedeno"] and сошлось == ЖДЁМ_ЖИВЫХ["soshlos"]
        and not расхождения, (выведено, сошлось, расхождения[:3]))
    chk(f"аргументы разобраны и закодированы обратно БАЙТ В БАЙТ на "
        f"{ЖДЁМ_ЖИВЫХ['bajt_v_bajt']} живых инструкциях",
        байты == ЖДЁМ_ЖИВЫХ["bajt_v_bajt"], байты)
    км = krivaja_iz_minta_v_zhivyh(tranzakcii=живые)
    chk(f"счёт кривой, выведенный ИЗ МИНТА, стоит в живой инструкции "
        f"{ЖДЁМ_ЖИВЫХ['krivaja_soshlos']} раз из "
        f"{ЖДЁМ_ЖИВЫХ['krivaja_sverjeno']} -- по ПЯТИ разновидностям, включая "
        "те, чья живая раскладка старше нынешнего IDL",
        км["sverjeno"] == ЖДЁМ_ЖИВЫХ["krivaja_sverjeno"]
        and км["soshlos"] == ЖДЁМ_ЖИВЫХ["krivaja_soshlos"]
        and len(км["variantov"]) == 5, км)
    # ---------------------------------------------- 6. НАШИ СБОРКИ
    МИНТ = ОТКАЗ_08_10["minty"][1]
    НАШ = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"
    пок = pokupka_krivoj_v3(base_mint=МИНТ, user=НАШ, buyback_fee_recipient=НАШ,
                            spendable_quote_in=10 ** 8, min_tokens_out=1)
    chk("НАША ПОКУПКА v3 собирается БЕЗ ТРАНЗАКЦИИ ИСТОЧНИКА -- из шести "
        "значений, и в ней ровно 17 счетов с названным провенансом",
        пок["schetov"] == 17
        and all(а["otkuda"] for а in пок["accounts"])
        and sum(1 for а in пок["accounts"] if а["otkuda"] == ПРОВ_ПЕРЕДАН) == 6,
        пок["schetov"])
    chk("данные покупки v3 -- 25 байт: восемь дискриминатора, два u64 и ОДИН "
        "байт OptionBool (это структура с bool, а не Option)",
        пок["bajt_dannyh"] == 25
        and пок["data_hex"].startswith(ОТКАЗ_08_10["disc"]),
        (пок["bajt_dannyh"], пок["data_hex"][:20]))
    прод = prodazha_krivoj_v3(base_mint=МИНТ, user=НАШ,
                              buyback_fee_recipient=НАШ, amount=1000,
                              min_sol_output=1)
    chk("наша продажа sell_v3 -- те же 17 счетов и 24 байта данных",
        прод["schetov"] == 17 and прод["bajt_dannyh"] == 24, прод["bajt_dannyh"])
    плохие_в = []
    for имя_п, вариант, ждём in НАШИ_ВАРИАНТЫ:
        if len(идл[имя_п]["ix"][вариант]["accounts"]) != ждём:
            плохие_в.append((имя_п, вариант, ждём))
    chk("у всех восьми наших разновидностей число счетов такое, как объявлено "
        "(17 у v3 кривой и у v2 AMM, 26 и 27 у старых кривой)", not плохие_в,
        плохие_в)
    круг = razobrat_argumenty("pump", "buy_exact_quote_in_v3",
                              bytes.fromhex(пок["data_hex"]))
    chk("аргументы нашей сборки читаются обратно теми же числами",
        круг["spendable_quote_in"] == 10 ** 8 and круг["min_tokens_out"] == 1
        and круг["_hvost"] == 0, круг)
    # ---------------------------------------------- 7. ДОКАЗАННЫЕ КРАСНЫЕ
    упало = None
    try:
        sobrat_scheta("pump", "buy_exact_quote_in_v3",
                      {"base_mint": МИНТ, "quote_mint": WSOL, "user": НАШ,
                       "base_token_program": ПРОГ_ТОКЕНА,
                       "quote_token_program": ПРОГ_ТОКЕНА})
    except ОшибкаСборки as сбой:
        упало = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: нет получателя buyback -- отказ С ИМЕНЕМ СЧЁТА, "
        "а не сборка с дыркой",
        упало and WHY_НЕТ_В_КОНТЕКСТЕ in упало
        and "buyback_fee_recipient" in упало, упало)
    упало2 = None
    try:
        prodazha_krivoj_v2(base_mint=МИНТ, user=НАШ, fee_recipient=НАШ,
                           buyback_fee_recipient=НАШ, creator="", amount=1,
                           min_sol_output=1)
    except ОшибкаСборки as сбой:
        упало2 = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: sell_v2 без создателя кривой НЕ собирается -- "
        "creator лежит в СЧЁТЕ кривой и в транзакции его нет; выдумать его "
        "значит отправить комиссию создателя не туда",
        упало2 and "creator" in упало2, упало2)
    упало3 = None
    try:
        zakodirovat_argumenty("pump", "buy_exact_quote_in_v3",
                              {"spendable_quote_in": 1})
    except ОшибкаСборки as сбой:
        упало3 = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: не задан аргумент -- отказ по имени, а не нули",
        упало3 and "min_tokens_out" in упало3, упало3)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: незнакомая разновидность названа своим "
        "дискриминатором, а не молча пропущена",
        variant_po_disku("pump", bytes.fromhex("0102030405060708")) is None,
        None)
    # ---------------------------------------------- 8. ПЕРЕЕЗД (п.3)
    переездов = sum(
        1 for tx in живые for их in instrukcii_programmy(tx, ПРОГ_КРИВОЙ)
        if их.get("data") and len(b58d(их["data"])) >= 8
        and variant_po_disku("pump", b58d(их["data"])[:8]) in ВАРИАНТЫ_ПЕРЕЕЗДА)
    chk("образцов переезда (migrate/migrate_v2) в репозитории НОЛЬ -- и это "
        "сказано числом, а не выдано за проверенное",
        переездов == 0, переездов)
    пустой = kontekst_iz_pereezda({}, nash_koshelek=НАШ)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: без migrate в транзакции -- отказ по имени",
        not пустой["ok"] and "не переезд" in (пустой["why_not"] or ""), пустой)
    только_миграция = {"transaction": {"message": {"instructions": [
        {"programId": ПРОГ_КРИВОЙ,
         "data": b58e(zagruzit_idl()["pump"]["ix"]["migrate"]["disc"]),
         "accounts": []}]}}, "meta": {}}
    б_пула = kontekst_iz_pereezda(только_миграция, nash_koshelek=НАШ)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: переезд есть, а create_pool в той же транзакции "
        "нет -- покупать не во что, и это отказ",
        not б_пула["ok"] and "create_pool" in (б_пула["why_not"] or ""), б_пула)

    if упавшие:
        print("УПАЛИ: " + "; ".join(упавшие))
    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным")
        return 1
    return 1 if плохо else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--svod", action="store_true",
                   help="свод по живым образцам репозитория")
    p.add_argument("--sobrat", default=None,
                   help="разновидность: pump.buy_exact_quote_in_v3 и т.п.")
    p.add_argument("--mint", default=None)
    p.add_argument("--koshelek", default=None)
    p.add_argument("--buyback", default=None)
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if a.svod:
        с = svod_po_zhivym()
        print(json.dumps({f"{p_}.{v}": {k: з for k, з in з_.items()
                                        if k != "raznica"}
                          for (p_, v), з_ in с.items()
                          for з in [0]}, ensure_ascii=False, indent=1))
        return 0
    if a.sobrat and a.mint and a.koshelek:
        имя_п, вариант = a.sobrat.split(".", 1)
        из_ = sobrat(имя_п, вариант,
                     {"base_mint": a.mint, "quote_mint": WSOL,
                      "base_token_program": ПРОГ_ТОКЕНА,
                      "quote_token_program": ПРОГ_ТОКЕНА,
                      "user": a.koshelek,
                      "buyback_fee_recipient": a.buyback or a.koshelek},
                     {"spendable_quote_in": 1, "min_tokens_out": 1,
                      "partial_fill": False})
        print(json.dumps(из_, ensure_ascii=False, indent=1))
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
