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
    откуда: dict = {}

    def _odin(а) -> bool:
        """Вывести ОДИН счёт, если уже можно. Нельзя -- False, без ошибки."""
        имя = а["name"]
        if имя in готово:
            return False
        if а.get("address"):
            готово[имя], откуда[имя] = а["address"], ПРОВ_КОНСТАНТА
            return True
        if имя in kontekst and kontekst[имя]:
            готово[имя], откуда[имя] = str(kontekst[имя]), ПРОВ_ПЕРЕДАН
            return True
        if а.get("pda"):
            п = а["pda"]
            try:
                семена = [_semja(с, готово, kontekst, константы)
                          for с in п.get("seeds") or []]
            except ОшибкаСборки:
                return False
            прог = сама
            пр = п.get("program") or {}
            if пр.get("value"):
                прог = b58e(bytes(пр["value"]))
            elif пр.get("path"):
                найдена = (готово.get(пр["path"]) or kontekst.get(пр["path"])
                           or константы.get(пр["path"]))
                if not найдена:
                    return False
                прог = найдена
            готово[имя], откуда[имя] = pda(семена, прог)[0], ПРОВ_PDA
            return True
        if имя in КАК_ATA:
            вл, тп, мн = КАК_ATA[имя]
            if any(не_задан(и, готово, kontekst) for и in (вл, тп, мн)):
                return False
            готово[имя] = ata(_znach(вл, готово, kontekst),
                              _znach(тп, готово, kontekst),
                              _znach(мн, готово, kontekst))
            откуда[имя] = ПРОВ_ATA
            return True
        return False

    # ВЫВОД ИДЁТ ДО УПОРА, А НЕ ОДНИМ ПРОХОДОМ СВЕРХУ ВНИЗ. Порядок счетов в
    # IDL не обязан совпадать с порядком вывода: у migrate_v2 счёт `pool`
    # (место 10) выводится ИЗ `pool_authority` (место 11), то есть из того,
    # что стоит ПОЗЖЕ. Одним проходом `pool` не выводился вовсе, и сборка
    # падала «семя 'pool_authority'» на всех 24 настоящих переездах с цепи.
    # Поэтому проходы повторяются, пока хоть один счёт выводится; что не
    # вывелось после этого -- то не выводится в принципе, и о нём отказ по
    # имени, а не дырка.
    while any([_odin(а) for а in их["accounts"]]):
        pass
    нет = [а["name"] for а in их["accounts"] if а["name"] not in готово]
    if нет:
        raise ОшибкаСборки(f"{WHY_НЕТ_В_КОНТЕКСТЕ}: {нет if len(нет) > 1 else нет[0]}")
    строки = [{"imja": а["name"], "pubkey": готово[а["name"]],
               "isSigner": bool(а.get("signer")),
               "isWritable": bool(а.get("writable")),
               "otkuda": откуда[а["name"]]} for а in их["accounts"]]
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
            # ХВОСТ ЦЕПИ. У развёрнутой программы счетов бывает БОЛЬШЕ, чем
            # в закреплённом IDL: у migrate_v2 на цепи 29 против 27, у
            # pump_amm.buy_exact_quote_in -- 25--26 против 23. Лишние стоят
            # В КОНЦЕ, и это ИЗМЕРЕНО, а не предположено: на 24 настоящих
            # переездах выведенные счета мест 0..26 сошлись 456 из 456.
            # Поэтому «длиннее IDL» -- это хвост с названными адресами, а
            # «короче IDL» -- раскладка СТАРШЕ нашего IDL, и там имена мест
            # уже не те. Склеивать эти два случая нельзя.
            хвост = len(счета) - len(идл["accounts"])
            из_.update(ok=True, variant=вариант, argumenty=арг,
                       scheta=по_именам, schetov=len(счета),
                       schetov_idl=len(идл["accounts"]),
                       schetov_sovpalo=len(счета) == len(идл["accounts"]),
                       schetov_hvost=хвост,
                       hvost=(счета[len(идл["accounts"]):] if хвост > 0 else []),
                       # ИМЕНА МЕСТ IDL ЕСТЬ ДЛЯ ВСЕХ -- и только. Это
                       # СРАВНЕНИЕ ДЛИН, а не доказанный префикс: цел он или
                       # нет, показывает ТОЛЬКО сверка выведенных адресов с
                       # цепью. Раньше поле звалось prefiks_polon и обещало
                       # больше, чем проверяет.
                       imena_mest_jest=хвост >= 0)
            return из_
    из_["why_not"] = из_["why_not"] or "инструкции pump в транзакции нет"
    return из_


# ------------------------------- п.3: покупка в свежий пул по переезду

ВАРИАНТЫ_ПЕРЕЕЗДА = ("migrate", "migrate_v2")


def programma_tokena_iz_tranzakcii(tx: dict, mint: str) -> str | None:
    """ПРОГРАММА ТОКЕНА ЭТОГО МИНТА -- ИЗ САМОЙ ТРАНЗАКЦИИ.

    ТРЕТИЙ источник рядом с двумя у `programma_tokena_minta` (сигнал
    детектора и владелец счёта минта с цепи): когда транзакция уже на
    руках, сети не нужно вовсе. Годится как `iz_signala` для него.

    jsonParsed кладёт `programId` рядом с каждым остатком токена
    (meta.preTokenBalances / postTokenBalances) -- это и есть владелец минта.
    Нет остатка по этому минту -- None, и тогда звать сборщик нельзя: он
    откажет по имени счёта, и это правильнее, чем взять SPL наугад.
    """
    мета = (tx or {}).get("meta") or {}
    for ключ in ("preTokenBalances", "postTokenBalances"):
        for б in мета.get(ключ) or []:
            if б.get("mint") == mint and б.get("programId"):
                return str(б["programId"])
    return None


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


WHY_НЕТ_ПРОГРАММЫ_ТОКЕНА = (
    "программа токена базового минта не задана: её нельзя подставить "
    "классическим Token -- она входит СЕМЕНЕМ в associated_base_bonding_curve "
    "и associated_base_user, и у минта Token-2022 адреса выйдут чужие")
ПРОГРАММЫ_ТОКЕНА = (ПРОГ_ТОКЕНА, ПРОГ_ТОКЕНА_2022)


def programma_tokena_minta(mint: str, *, iz_signala: str | None = None,
                           chitatel=None) -> str:
    """Программа-владелец счёта минта: из сигнала или с цепи. Иначе ОТКАЗ.

    ЗАЧЕМ (слово владельца 09.10, п.1). 09.10 симуляция дала три красных на
    отказанных сигналах vol_4vw: AnchorError 3012 AccountNotInitialized по
    associated_base_bonding_curve и associated_base_user. Причина не в
    раскладке -- в семенах: программа токена входит в них третьим семенем
    (КАК_ATA и pda-семена IDL), а у этих минтов токен Token-2022
    (TokenzQdB…), тогда как обёртки сборки подставляли классический Token
    молча, значением по умолчанию. Адрес выходил чужой, счёт по нему не
    существует -- программа и отвечала "не инициализирован".
    ПОЭТОМУ ЗНАЧЕНИЯ ПО УМОЛЧАНИЮ ЗДЕСЬ БОЛЬШЕ НЕТ: не знаем программу --
    отказываемся по имени, а не угадываем.

    iz_signala -- поле token_program решения детектора (он читает его из
    транзакции источника). chitatel -- вызываемое, получающее владельца счёта
    минта с цепи (getAccountInfo.owner).
    """
    if iz_signala and str(iz_signala) in ПРОГРАММЫ_ТОКЕНА:
        return str(iz_signala)
    if iz_signala:
        raise ОшибкаСборки(
            f"{WHY_НЕТ_ПРОГРАММЫ_ТОКЕНА}: в сигнале стоит {iz_signala!r}, "
            "а это не Token и не Token-2022")
    if chitatel is not None:
        владелец = chitatel(mint)
        if владелец and str(владелец) in ПРОГРАММЫ_ТОКЕНА:
            return str(владелец)
        raise ОшибкаСборки(
            f"{WHY_НЕТ_ПРОГРАММЫ_ТОКЕНА}: владелец счёта минта {mint} "
            f"прочитан как {владелец!r}")
    raise ОшибкаСборки(WHY_НЕТ_ПРОГРАММЫ_ТОКЕНА)


def _проверить_программу_токена(base_token_program: str | None) -> str:
    if not base_token_program:
        raise ОшибкаСборки(WHY_НЕТ_ПРОГРАММЫ_ТОКЕНА)
    if str(base_token_program) not in ПРОГРАММЫ_ТОКЕНА:
        raise ОшибкаСборки(
            f"{WHY_НЕТ_ПРОГРАММЫ_ТОКЕНА}: передано {base_token_program!r}")
    return str(base_token_program)


def pokupka_krivoj_v3(*, base_mint: str, user: str, buyback_fee_recipient: str,
                      spendable_quote_in: int, min_tokens_out: int,
                      quote_mint: str = WSOL,
                      base_token_program: str | None = None,
                      quote_token_program: str = ПРОГ_ТОКЕНА,
                      partial_fill: bool = False) -> dict:
    """НАША ПОКУПКА кривой разновидностью, которую программа принимает сейчас.

    base_token_program ОБЯЗАТЕЛЕН: см. programma_tokena_minta. quote_token_program
    остаётся классическим Token -- котировка у нас WSOL, а он классический.
    """
    base_token_program = _проверить_программу_токена(base_token_program)
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
                       base_token_program: str | None = None,
                       quote_token_program: str = ПРОГ_ТОКЕНА) -> dict:
    """НАША ПРОДАЖА кривой: sell_v3, те же 17 счетов.

    base_token_program ОБЯЗАТЕЛЕН по той же причине, что и в покупке: он семя
    у associated_base_*. Продажа Token-2022 классической программой собрала бы
    чужой счёт и не продала бы ничего.
    """
    base_token_program = _проверить_программу_токена(base_token_program)
    к = {"base_mint": base_mint, "quote_mint": quote_mint,
         "base_token_program": base_token_program,
         "quote_token_program": quote_token_program, "user": user,
         "buyback_fee_recipient": buyback_fee_recipient}
    return sobrat("pump", "sell_v3", к,
                  {"amount": int(amount), "min_sol_output": int(min_sol_output)})


def prodazha_krivoj_v2(*, base_mint: str, user: str, fee_recipient: str,
                       buyback_fee_recipient: str, creator: str, amount: int,
                       min_sol_output: int, quote_mint: str = WSOL,
                       base_token_program: str | None = None,
                       quote_token_program: str = ПРОГ_ТОКЕНА) -> dict:
    """ПРОДАЖА sell_v2 -- 26 счетов; `creator` берётся из события сделки.

    base_token_program ОБЯЗАТЕЛЕН -- то же семя, та же цена ошибки.
    """
    base_token_program = _проверить_программу_токена(base_token_program)
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
    # ПРОГРАММЫ ТОКЕНА ПРОВЕРЯЮТСЯ, А НЕ ТОЛЬКО ПРИСУТСТВУЮТ. У AMM они тоже
    # входят СЕМЕНАМИ в счета пула и пользователя; непустая строка, которая не
    # является ни Token, ни Token-2022, дала бы тот же молчаливо чужой адрес,
    # что и умолчание у кривой. Проверка та же, что у обёрток кривой.
    кон = dict(kontekst)
    for имя in ("base_token_program", "quote_token_program"):
        кон[имя] = _проверить_программу_токена(кон.get(имя))
    return sobrat("pump_amm", variant, кон, argumenty)


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

# ------------------------- ЧТЕНИЕ ПОЛЕЙ СЧЁТА ПО РАСКЛАДКЕ IDL
# ЗАЧЕМ. Два нужных нам значения лежат НЕ В ТРАНЗАКЦИИ, а В СЧЕТАХ:
#   * `bonding_curve.creator` -- семя хранилища создателя у кривой (у него
#     есть обход: кривая кладёт создателя в событие сделки);
#   * `Pool.coin_creator` -- семя `coin_creator_vault_authority` и
#     `coin_creator_vault_ata` у Pump AMM, и обхода у него НЕТ. Пока его не
#     прочитать, НИ ОДИН образец AMM собрать нельзя: на 42 живых
#     инструкциях AMM из репозитория сборка падала ровно этими двумя
#     именами. Поэтому счёт пула читается с цепи, а поле берётся по
#     раскладке IDL, а не по смещению, вбитому руками.
РАЗМЕР_ПРОСТОГО = {"u8": 1, "i8": 1, "bool": 1, "u16": 2, "i16": 2,
                   "u32": 4, "i32": 4, "f32": 4, "u64": 8, "i64": 8,
                   "f64": 8, "u128": 16, "i128": 16, "pubkey": 32,
                   "publicKey": 32}


def razmer_tipa(тип) -> int | None:
    """Размер типа в байтах. Переменная длина -- None, и дальше читать нельзя."""
    if isinstance(тип, str):
        return РАЗМЕР_ПРОСТОГО.get(тип)
    if isinstance(тип, dict):
        if "array" in тип:
            внутри, сколько = тип["array"]
            р = razmer_tipa(внутри)
            return None if р is None else р * int(сколько)
        # string, vec, option, defined -- длина переменная или зависит от
        # другого типа. Угадывать её нельзя: вернём None и откажем по имени.
    return None


def smeshchenije_polja(imja_programmy: str, tip: str, pole: str) -> int:
    """СМЕЩЕНИЕ ПОЛЯ В ТЕЛЕ СЧЁТА -- ПОСЧИТАНО ПО IDL, а не вбито руками.

    Восемь байт дискриминатора счёта учтены. До нужного поля встретился
    тип переменной длины -- отказ по имени: дальше смещение не определено.
    """
    идл = zagruzit_idl()[imja_programmy]
    т = (идл.get("tipy") or {}).get(tip)
    поля = (((т or {}).get("type") or {}).get("fields")) or []
    if not поля:
        raise ОшибкаСборки(f"в IDL {imja_programmy} нет типа {tip}")
    сдвиг = 8
    for п in поля:
        if п["name"] == pole:
            return сдвиг
        р = razmer_tipa(п["type"])
        if р is None:
            raise ОшибкаСборки(
                f"{tip}.{pole}: до него стоит поле {п['name']} переменной "
                f"длины ({п['type']!r}) -- смещение не определено")
        сдвиг += р
    raise ОшибкаСборки(f"в типе {tip} нет поля {pole}")


def pubkey_polja(imja_programmy: str, tip: str, pole: str, dannye) -> str:
    """Pubkey из тела счёта. `dannye` -- base64 или байты, как отдаёт узел."""
    import base64  # noqa: PLC0415

    б = (base64.b64decode(dannye) if isinstance(dannye, str) else bytes(dannye))
    см = smeshchenije_polja(imja_programmy, tip, pole)
    if len(б) < см + 32:
        raise ОшибкаСборки(f"тело счёта короче, чем {tip}.{pole}: "
                           f"{len(б)} байт, нужно {см + 32}")
    return b58e(б[см:см + 32])


# ХВОСТ ЦЕПИ: СКОЛЬКО СЧЕТОВ У РАЗВЁРНУТОЙ ПРОГРАММЫ СВЕРХ IDL.
# ИЗМЕРЕНО, А НЕ ОБЪЯВЛЕНО (09.10):
#   * migrate_v2 -- 29 против 27 на 24 настоящих переездах с цепи
#     (data/pereezdy_po_cepi.json, снял Code-1). Оба лишних ВЫВОДЯТСЯ:
#       место 27 = boost_vault_authority = PDA(["boost_vault", pool]) под
#                  pAMMBay -- сошлось 24 из 24 (имя взято из pump_amm IDL,
#                  инструкции boost_buy_and_burn и init_boost);
#       место 28 = ATA(место 27, quote_token_program, quote_mint)
#                  -- сошлось 24 из 24.
#   * у кривой pump.buy, pump.buy_exact_sol_in, pump.sell -- хвост 2;
#   * у Pump AMM buy, buy_exact_quote_in, sell -- хвост 3 (у двух образцов
#     buy_exact_quote_in из 28 -- 2: на цепи живут обе длины).
# ПРЕФИКС ПРИ ЭТОМ ЦЕЛ: выведенные счета мест 0..N-1 сошлись адрес в адрес
# 383 из 383 на живых образцах репозитория и 456 из 456 на переездах.
# Поэтому «длиннее IDL» -- это ХВОСТ, а не другая раскладка. «Короче IDL»
# -- наоборот, РАСКЛАДКА СТАРШЕ нашего IDL, и там имена мест уже не те.
#
# ГДЕ ЭТО ДОКАЗАНО, А ГДЕ ТОЛЬКО ПОСЧИТАНО -- РАЗНИЦА НАЗВАНА ЧЕСТНО.
# Из тех 383 выведенных счетов 381 -- У КРИВОЙ, и только 2 -- у Pump AMM, да
# и те из close_user_volume_accumulator, а не из торговой разновидности. У
# ВСЕХ 42 живых торговых инструкций AMM сборка не доходит до сверки вовсе:
# им нужен Pool.coin_creator, которого в транзакции нет (см. pubkey_polja).
# Значит ТРИ НИЖНИЕ СТРОКИ ЭТОЙ ТАБЛИЦЫ (pump_amm) стоят на ОДНОМ ЛИШЬ
# ПОДСЧЁТЕ ЧИСЛА СЧЕТОВ, и целость их префикса НЕ проверена ни на одном
# образце. Пока счёт пула не читается с цепи, брать раскладку AMM по хвосту
# НЕЛЬЗЯ -- число здесь только для переписи, не для отправки.
ХВОСТ_ПО_ЦЕПИ = {("pump", "migrate_v2"): 2, ("pump", "buy"): 2,
                  ("pump", "buy_exact_sol_in"): 2, ("pump", "sell"): 2,
                  ("pump_amm", "buy"): 3, ("pump_amm", "sell"): 3,
                  ("pump_amm", "buy_exact_quote_in"): 3}


def hvost_pereezda(*, pool: str, quote_mint: str, quote_token_program: str) -> list:
    """ДВА ЛИШНИХ СЧЁТА migrate_v2 -- выведены, а не скопированы."""
    авторитет, _ = pda([b"boost_vault", b58d(pool)], ПРОГ_AMM)
    return [авторитет, ata(авторитет, quote_token_program, quote_mint)]


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


ТОРГОВЫЕ_ВАРИАНТЫ = tuple(sorted({
    ("pump", "buy"), ("pump", "buy_v2"), ("pump", "buy_v3"),
    ("pump", "buy_exact_sol_in"), ("pump", "buy_exact_quote_in"),
    ("pump", "buy_exact_quote_in_v2"), ("pump", "buy_exact_quote_in_v3"),
    ("pump", "sell"), ("pump", "sell_v2"), ("pump", "sell_v3"),
    ("pump_amm", "buy"), ("pump_amm", "buy_v2"),
    ("pump_amm", "buy_exact_quote_in"), ("pump_amm", "buy_exact_quote_in_v2"),
    ("pump_amm", "sell"), ("pump_amm", "sell_v2"),
}))


def perepis_raskladok(*, tranzakcii: list | None = None) -> dict:
    """ПЕРЕПИСЬ: ЧТО НА ЦЕПИ ПРОТИВ ТОГО, ЧТО В IDL -- по каждому варианту.

    Для каждой пары (программа, разновидность) считается, сколько живых
    инструкций пришло с каким ЧИСЛОМ СЧЕТОВ и какой ДЛИНОЙ ДАННЫХ, и это
    ставится рядом с числами закреплённого IDL. Приговор по варианту:
      * `po_idl`     -- живое совпало с IDL;
      * `hvost_cepi` -- счетов БОЛЬШЕ, лишние в конце, префикс выведен;
      * `starshe_idl`-- счетов или байт МЕНЬШЕ: раскладка старше нашего IDL;
      * `raznocenie` -- на цепи живут сразу несколько длин.
    Ничего не усредняется и не округляется: в ответе стоят все встреченные
    пары (счетов, байт) со своими числами.
    """
    тх = zhivyje_tranzakcii() if tranzakcii is None else tranzakcii
    из_: dict = {}
    for tx in тх:
        perepis_dobavit_tx(из_, tx)
    return prigovory_perepisi(из_)


def perepis_dobavit_tx(stroki: dict, tx: dict) -> dict:
    """ОДНА ТРАНЗАКЦИЯ В ПЕРЕПИСЬ: КАЖДАЯ её инструкция pump, а не первая.

    ОТДЕЛЬНОЙ ФУНКЦИЕЙ -- ЧТОБЫ ЖИВОЙ ПРОГОН И ОБРАЗЦЫ РЕПОЗИТОРИЯ СЧИТАЛИСЬ
    ОДНИМ И ТЕМ ЖЕ КОДОМ. Живой прогон раньше складывал в перепись то, что
    вернул `razbor_istochnika`, а он отдаёт ПЕРВУЮ подходящую инструкцию и
    только из девяти наших разновидностей. Значит:
      * транзакция с двумя инструкциями pump считалась за одну;
      * всё, что вне девяти (в том числе СТАРЫЕ имена AMM, которыми цепь
        торгует чаще всего), не попадало в перепись ВОВСЕ и нигде не
        числилось -- молчаливый пропуск.
    Здесь обходятся все инструкции обеих программ, и строка создаётся на
    любую узнанную разновидность, а не только на нашу.
    """
    идл = zagruzit_idl()
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
            if б[:8] == АНКОР_СОБЫТИЕ_ДИСК:
                # ЭТО НЕ ИНСТРУКЦИЯ, А СОБЫТИЕ. Anchor печатает события
                # самовызовом программы к себе же, и у такого «вызова» свой
                # дискриминатор e445a52e51cb9a1d. В перепись раскладок ему
                # нельзя: раскладки у события нет. Но и выбрасывать молча
                # нельзя -- считается отдельной строкой, своим именем.
                стр = stroki.setdefault(f"{имя_п}.sobytije_anchor", {
                    "programma": имя_п, "variant": None,
                    "disc": б[:8].hex(), "schetov_idl": None,
                    "bajt_idl": None, "obrazcov": 0, "pary": {},
                    "torgovyj": False, "sobytije": True})
                стр["obrazcov"] += 1
                continue
            вариант = variant_po_disku(имя_п, б[:8])
            if not вариант:
                # НЕУЗНАННАЯ РАЗНОВИДНОСТЬ ТОЖЕ СЧИТАЕТСЯ -- своим
                # дискриминатором. Иначе новая инструкция программы прошла бы
                # мимо переписи незамеченной, а это как раз то, что надо
                # увидеть первым.
                ключ = f"{имя_п}.disc:{б[:8].hex()}"
                стр = stroki.setdefault(ключ, {
                    "programma": имя_п, "variant": None,
                    "disc": б[:8].hex(), "schetov_idl": None,
                    "bajt_idl": None, "obrazcov": 0, "pary": {},
                    "torgovyj": False, "neizvestnaja": True})
                стр["obrazcov"] += 1
                пара = f"{len(их.get('accounts') or [])}/{len(б)}"
                стр["pary"][пара] = стр["pary"].get(пара, 0) + 1
                continue
            ключ = f"{имя_п}.{вариант}"
            спец = идл[имя_п]["ix"][вариант]
            стр = stroki.setdefault(ключ, {
                "programma": имя_п, "variant": вариант,
                "schetov_idl": len(спец["accounts"]),
                "bajt_idl": _bajt_po_idl(имя_п, вариант),
                "obrazcov": 0, "pary": {}, "torgovyj":
                    (имя_п, вариант) in ТОРГОВЫЕ_ВАРИАНТЫ})
            стр["obrazcov"] += 1
            пара = f"{len(их.get('accounts') or [])}/{len(б)}"
            стр["pary"][пара] = стр["pary"].get(пара, 0) + 1
    return stroki


def pustaja_stroka_perepisi(imja_programmy: str, variant: str) -> dict:
    """Пустая строка переписи -- с числами IDL, но без ни одного образца."""
    спец = zagruzit_idl()[imja_programmy]["ix"][variant]
    return {"programma": imja_programmy, "variant": variant,
            "schetov_idl": len(спец["accounts"]),
            "bajt_idl": _bajt_po_idl(imja_programmy, variant),
            "obrazcov": 0, "pary": {},
            "torgovyj": (imja_programmy, variant) in ТОРГОВЫЕ_ВАРИАНТЫ}


def prigovory_perepisi(из_: dict) -> dict:
    """ПРИГОВОР ПО КАЖДОЙ СТРОКЕ. Отдельной функцией -- чтобы живой прогон
    копил пары по ходу чтения, а приговор ставился тем же кодом, что и
    на образцах репозитория."""
    for стр in из_.values():
        if стр.get("sobytije"):
            стр.update(hvost_cepi=[], hvost_po_zamery=None,
                       raznica_schetov=[], raznica_bajt=[],
                       prigovor="sobytije_a_ne_instrukcija")
            continue
        if стр.get("neizvestnaja"):
            # ЧИСЕЛ IDL ДЛЯ НЕЁ НЕТ -- и приговор не выдумывается.
            стр.update(hvost_cepi=[], hvost_po_zamery=None,
                       raznica_schetov=[], raznica_bajt=[],
                       prigovor="v_idl_jejo_net")
            continue
        if not стр["pary"]:
            стр.update(hvost_cepi=[], hvost_po_zamery=ХВОСТ_ПО_ЦЕПИ.get(
                (стр["programma"], стр["variant"])),
                raznica_schetov=[], raznica_bajt=[],
                prigovor="obrazcov_net")
            continue
        счета = {int(п.split("/")[0]) for п in стр["pary"]}
        байты = {int(п.split("/")[1]) for п in стр["pary"]}
        дс = счета - {стр["schetov_idl"]}
        дб = байты - {стр["bajt_idl"]}
        стр["hvost_cepi"] = sorted(с - стр["schetov_idl"] for с in счета
                                   if с > стр["schetov_idl"])
        стр["hvost_po_zamery"] = ХВОСТ_ПО_ЦЕПИ.get(
            (стр["programma"], стр["variant"]))
        стр["raznica_schetov"] = sorted(с - стр["schetov_idl"] for с in дс)
        стр["raznica_bajt"] = sorted(
            б - стр["bajt_idl"] for б in дб if стр["bajt_idl"] is not None)
        # ПРИГОВОР ПО ТОМУ, ЧТО ВИДНО, а не по догадке, какая сторона старше.
        if not дс and not дб:
            стр["prigovor"] = "po_idl"
        elif len(стр["pary"]) > 1:
            стр["prigovor"] = "raznocenie"
        elif дс and not дб and all(с > стр["schetov_idl"] for с in дс):
            стр["prigovor"] = "hvost_cepi"
        elif дб and not дс:
            стр["prigovor"] = "dlina_dannyh_ne_ta"
        else:
            стр["prigovor"] = "rashoditsja_po_oboim"
    return из_


def _bajt_po_idl(imja_programmy: str, variant: str) -> int | None:
    """Сколько байт данных у этой разновидности ПО IDL. Не сложилось -- None."""
    их = zagruzit_idl()[imja_programmy]["ix"][variant]
    try:
        return len(zakodirovat_argumenty(
            imja_programmy, variant, {а["name"]: 0 for а in их["args"]}))
    except ОшибкаСборки:
        return None


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

# 24 у Code-3 + 7 про программу токена базового минта (правка 09.10, Code-1):
# два адреса одного минта, четыре доказанных красных и два пути получения.
# + 10 на раскладку по цепи против IDL (п.3, правка 09.10, Code-3): измеренная
# программа токена, два смещения по IDL и доказанный красный на переменную
# длину, целость префикса на 383 выведенных счетах, четыре на перепись и
# выведенный хвост переезда.
ZHDEM_PROVEROK = 42
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
    # МИНТЫ ОТКАЗА 08.10 -- Token-2022: так их назвал журнал детектора
    # ("token_program": TokenzQdB…). Поэтому и здесь стоит Token-2022, иначе
    # самопроверка шла бы по случаю, которого в жизни не было.
    пок = pokupka_krivoj_v3(base_mint=МИНТ, user=НАШ, buyback_fee_recipient=НАШ,
                            spendable_quote_in=10 ** 8, min_tokens_out=1,
                            base_token_program=ПРОГ_ТОКЕНА_2022)
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
                              min_sol_output=1,
                              base_token_program=ПРОГ_ТОКЕНА_2022)
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
    # ---------- ПРОГРАММА ТОКЕНА: ДВА АДРЕСА ОДНОГО МИНТА (правка 09.10)
    т2 = pokupka_krivoj_v3(base_mint=МИНТ, user=НАШ, buyback_fee_recipient=НАШ,
                           spendable_quote_in=10 ** 8, min_tokens_out=1,
                           base_token_program=ПРОГ_ТОКЕНА_2022)
    кл = pokupka_krivoj_v3(base_mint=МИНТ, user=НАШ, buyback_fee_recipient=НАШ,
                           spendable_quote_in=10 ** 8, min_tokens_out=1,
                           base_token_program=ПРОГ_ТОКЕНА)
    по_имени_т2 = {а["imja"]: а["pubkey"] for а in т2["accounts"]}
    по_имени_кл = {а["imja"]: а["pubkey"] for а in кл["accounts"]}
    разные = [и for и in ("associated_base_bonding_curve", "associated_base_user")
              if по_имени_т2[и] != по_имени_кл[и]]
    chk("программа токена МЕНЯЕТ адреса: у Token-2022 и классического Token "
        "associated_base_bonding_curve и associated_base_user РАЗНЫЕ -- именно "
        "на этом 09.10 вышли три красных 3012 AccountNotInitialized",
        разные == ["associated_base_bonding_curve", "associated_base_user"],
        разные)
    упало_тп = None
    try:
        pokupka_krivoj_v3(base_mint=МИНТ, user=НАШ, buyback_fee_recipient=НАШ,
                          spendable_quote_in=10 ** 8, min_tokens_out=1)
    except ОшибкаСборки as сбой:
        упало_тп = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: без программы токена покупка НЕ собирается -- "
        "значения по умолчанию больше нет",
        упало_тп and WHY_НЕТ_ПРОГРАММЫ_ТОКЕНА in упало_тп, упало_тп)
    упало_тп2 = None
    try:
        prodazha_krivoj_v3(base_mint=МИНТ, user=НАШ, buyback_fee_recipient=НАШ,
                           amount=1, min_sol_output=1)
    except ОшибкаСборки as сбой:
        упало_тп2 = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: то же у продажи sell_v3",
        упало_тп2 and WHY_НЕТ_ПРОГРАММЫ_ТОКЕНА in упало_тп2, упало_тп2)
    упало_тп3 = None
    try:
        programma_tokena_minta(МИНТ, iz_signala="ЧужаяПрограмма")
    except ОшибкаСборки as сбой:
        упало_тп3 = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: чужая программа из сигнала не принимается",
        упало_тп3 and "не Token и не Token-2022" in упало_тп3, упало_тп3)
    chk("программа токена из сигнала берётся, когда она настоящая",
        programma_tokena_minta(МИНТ, iz_signala=ПРОГ_ТОКЕНА_2022)
        == ПРОГ_ТОКЕНА_2022)
    chk("программа токена читается с цепи владельцем счёта минта",
        programma_tokena_minta(МИНТ, chitatel=lambda _м: ПРОГ_ТОКЕНА_2022)
        == ПРОГ_ТОКЕНА_2022)
    упало_тп4 = None
    try:
        programma_tokena_minta(МИНТ, chitatel=lambda _м: None)
    except ОшибкаСборки as сбой:
        упало_тп4 = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: владелец счёта минта не прочитался -- отказ, "
        "а не классический Token наугад",
        упало_тп4 and WHY_НЕТ_ПРОГРАММЫ_ТОКЕНА in упало_тп4, упало_тп4)

    # ------------- 6в. РАСКЛАДКА ПО ЦЕПИ ПРОТИВ IDL (п.3, правка Code-3)
    # ИЗМЕРЕНИЕ, А НЕ ПАМЯТЬ: какая программа токена базы стоит в живых
    # инструкциях кривой. Code-1 закрыл умолчание; здесь -- число, на
    # котором это стоит.
    тп_живые = {}
    for tx in zhivyje_tranzakcii():
        р = razbor_istochnika(tx)
        if р.get("ok") and р["programma"] == "pump":
            з = (р["scheta"] or {}).get("base_token_program")
            if з:
                тп_живые[з] = тп_живые.get(з, 0) + 1
    chk(f"ИЗМЕРЕНО: у всех {sum(тп_живые.values())} живых инструкций кривой "
        "base_token_program = TokenzQdB… (Token-2022), классический Token не "
        "встретился НИ РАЗУ -- поэтому умолчания у него и не может быть",
        тп_живые.get(ПРОГ_ТОКЕНА_2022) == sum(тп_живые.values())
        and sum(тп_живые.values()) >= 11, тп_живые)
    chk("СМЕЩЕНИЕ creator В СОБЫТИИ СДЕЛКИ, СВЕРЕННОЕ НА 23 ЖИВЫХ "
        f"ИНСТРУКЦИЯХ, ВОСПРОИЗВОДИТСЯ РАСКЛАДКОЙ IDL: {СМЕЩЕНИЕ_CREATOR} + 8 "
        f"байт дискриминатора = {smeshchenije_polja('pump', 'TradeEvent', 'creator')} "
        "-- два независимых пути дали одно число",
        smeshchenije_polja("pump", "TradeEvent", "creator")
        == СМЕЩЕНИЕ_CREATOR + 8,
        (СМЕЩЕНИЕ_CREATOR, smeshchenije_polja("pump", "TradeEvent", "creator")))
    chk("Pool.coin_creator -- семя двух счетов AMM, которых НЕТ в транзакции; "
        "смещение посчитано по IDL: "
        f"{smeshchenije_polja('pump_amm', 'Pool', 'coin_creator')} = 8 "
        "дискриминатора + bump 1 + index 2 + шесть pubkey + lp_supply 8",
        smeshchenije_polja("pump_amm", "Pool", "coin_creator")
        == 8 + 1 + 2 + 32 * 6 + 8)
    упало_пер = None
    try:
        smeshchenije_polja("pump", "TradeEvent", "quote_mint")
    except ОшибкаСборки as сбой:
        упало_пер = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: за полем переменной длины смещение НЕ определено "
        "-- отказ по имени поля, а не чтение наугад",
        упало_пер and "переменной" in упало_пер and "ix_name" in упало_пер,
        упало_пер)
    # ПРЕФИКС ЦЕЛ -- ЭТО И ЕСТЬ ОСНОВАНИЕ СЧИТАТЬ ЛИШНИЕ СЧЕТА ХВОСТОМ.
    # Если бы они вставлялись в середину, наш кошелёк уехал бы на чужое
    # место. Поэтому проверка не на слово, а на все выводимые счета всех
    # живых образцов -- включая те, где счетов БОЛЬШЕ, чем в IDL.
    вывед = сошл = 0
    не_сошл: dict = {}
    for tx in zhivyje_tranzakcii():
        for имя_п, адрес_п in (("pump", ПРОГ_КРИВОЙ), ("pump_amm", ПРОГ_AMM)):
            for их_ in instrukcii_programmy(tx, адрес_п):
                try:
                    б_ = b58d(их_.get("data") or "")
                except ОшибкаСборки:
                    continue
                if len(б_) < 8:
                    continue
                в_ = variant_po_disku(имя_п, б_[:8])
                if not в_:
                    continue
                спец = zagruzit_idl()[имя_п]["ix"][в_]["accounts"]
                сч_ = list(их_.get("accounts") or [])
                if len(сч_) < len(спец):
                    continue
                вывод_имена = {а["name"] for а in спец
                               if а.get("address") or а.get("pda")
                               or а["name"] in КАК_ATA}
                кон_ = {а["name"]: сч_[и] for и, а in enumerate(спец)
                        if а["name"] not in вывод_имена}
                нужен_ = any((с_.get("path") or "") == "bonding_curve.creator"
                             for а in спец
                             for с_ in ((а.get("pda") or {}).get("seeds") or []))
                if нужен_:
                    созд_ = creator_iz_sobytija(tx)
                    if not созд_:
                        continue
                    кон_["bonding_curve.creator"] = созд_
                try:
                    гот_ = sobrat_scheta(имя_п, в_, кон_)
                except ОшибкаСборки:
                    continue
                for и, а in enumerate(гот_["accounts"]):
                    if а["otkuda"] == ПРОВ_ПЕРЕДАН:
                        continue
                    вывед += 1
                    if а["pubkey"] == сч_[и]:
                        сошл += 1
                    else:
                        не_сошл[f"{и}:{а['imja']}"] = (
                            не_сошл.get(f"{и}:{а['imja']}", 0) + 1)
    по_прог_вывед: dict = {}
    for tx in zhivyje_tranzakcii():
        for имя_п, адрес_п in (("pump", ПРОГ_КРИВОЙ), ("pump_amm", ПРОГ_AMM)):
            for их_ in instrukcii_programmy(tx, адрес_п):
                try:
                    б_ = b58d(их_.get("data") or "")
                except ОшибкаСборки:
                    continue
                if len(б_) < 8 or б_[:8] == АНКОР_СОБЫТИЕ_ДИСК:
                    continue
                в_ = variant_po_disku(имя_п, б_[:8])
                if not в_:
                    continue
                спец = zagruzit_idl()[имя_п]["ix"][в_]["accounts"]
                сч_ = list(их_.get("accounts") or [])
                if len(сч_) < len(спец):
                    continue
                вывод_имена = {а["name"] for а in спец
                               if а.get("address") or а.get("pda")
                               or а["name"] in КАК_ATA}
                кон_ = {а["name"]: сч_[и] for и, а in enumerate(спец)
                        if а["name"] not in вывод_имена}
                нужен_ = any((с_.get("path") or "") == "bonding_curve.creator"
                             for а in спец
                             for с_ in ((а.get("pda") or {}).get("seeds") or []))
                if нужен_:
                    созд_ = creator_iz_sobytija(tx)
                    if not созд_:
                        continue
                    кон_["bonding_curve.creator"] = созд_
                try:
                    гот_ = sobrat_scheta(имя_п, в_, кон_)
                except ОшибкаСборки:
                    по_прог_вывед[f"{имя_п}:не собралось"] = (
                        по_прог_вывед.get(f"{имя_п}:не собралось", 0) + 1)
                    continue
                по_прог_вывед[имя_п] = по_прог_вывед.get(имя_п, 0) + sum(
                    1 for а in гот_["accounts"] if а["otkuda"] != ПРОВ_ПЕРЕДАН)
    chk("ЧЕМ ИМЕННО ДОКАЗАН ХВОСТ -- НАЗВАНО ЧЕСТНО: из выведенных счетов "
        f"{по_прог_вывед.get('pump')} у КРИВОЙ и только "
        f"{по_прог_вывед.get('pump_amm')} у Pump AMM, а все "
        f"{по_прог_вывед.get('pump_amm:не собралось')} живых торговых "
        "инструкций AMM до сверки НЕ ДОХОДЯТ (нужен Pool.coin_creator, "
        "которого в транзакции нет). Значит три строки AMM в ХВОСТ_ПО_ЦЕПИ "
        "стоят на ОДНОМ ПОДСЧЁТЕ счетов, и выдавать их за проверенные нельзя",
        по_прог_вывед.get("pump") == 381
        and по_прог_вывед.get("pump_amm") == 2
        and по_прог_вывед.get("pump_amm:не собралось") == 42, по_прог_вывед)
    chk("ПРЕФИКС ЦЕЛ ДАЖЕ ТАМ, ГДЕ СЧЕТОВ БОЛЬШЕ IDL: выведенные счета "
        f"сошлись адрес в адрес {сошл} из {вывед} на живых образцах -- значит "
        "лишние счета стоят В КОНЦЕ, а не вставлены в середину (вставка "
        "увела бы наш кошелёк на чужое место)",
        вывед >= 383 and сошл == вывед, (вывед, сошл, не_сошл))
    пер = perepis_raskladok()
    торг = {к: в for к, в in пер.items() if в["torgovyj"]}
    chk(f"ПЕРЕПИСЬ РАСКЛАДОК: торговых разновидностей в живых образцах "
        f"{len(торг)}, и у КАЖДОЙ названы число счетов и длина данных против "
        "IDL -- ни одной «примерно»",
        len(торг) == 8
        and all(в["pary"] and в["prigovor"] for в in торг.values()), sorted(торг))
    chk("ПЕРЕПИСЬ НАЗЫВАЕТ РАСХОЖДЕНИЕ ЧИСЛОМ: у pump.sell на цепи 16 счетов "
        "против 14 в IDL (хвост 2), у pump_amm.sell -- 24 против 21 (хвост 3), "
        "а у pump.sell_v2 всё по IDL",
        пер["pump.sell"]["hvost_cepi"] == [2]
        and пер["pump_amm.sell"]["hvost_cepi"] == [3]
        and пер["pump.sell_v2"]["prigovor"] == "po_idl",
        {к: пер[к]["pary"] for к in ("pump.sell", "pump_amm.sell",
                                     "pump.sell_v2")})
    chk("ПЕРЕПИСЬ ВИДИТ ДВЕ ДЛИНЫ СРАЗУ: у pump.buy_exact_quote_in_v2 на цепи "
        "живут и 25 байт, и 24 -- приговор «разноцение», а не одна цифра",
        пер["pump.buy_exact_quote_in_v2"]["prigovor"] == "raznocenie"
        and set(пер["pump.buy_exact_quote_in_v2"]["pary"]) == {"27/25", "27/24"},
        пер["pump.buy_exact_quote_in_v2"]["pary"])
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: вариант, которого в образцах нет вовсе, получает "
        "приговор «образцов нет» -- а не «по IDL» молча",
        prigovory_perepisi({"x": pustaja_stroka_perepisi(
            "pump_amm", "sell_v2")})["x"]["prigovor"] == "obrazcov_net")
    # ДВА ЛИШНИХ СЧЁТА migrate_v2 ВЫВОДЯТСЯ, а не копируются. 24 из 24 --
    # сверено на настоящих переездах цепи, см. ХВОСТ_ПО_ЦЕПИ.
    хв = hvost_pereezda(pool="H4MUUPfRze2KHnmDQLrcB6FXxNBe6yvG54NSfH7sw68j",
                        quote_mint=WSOL, quote_token_program=ПРОГ_ТОКЕНА)
    chk("ХВОСТ migrate_v2 (29 на цепи против 27 в IDL) ВЫВОДИТСЯ: "
        "boost_vault_authority = PDA(['boost_vault', pool]) под pAMMBay и его "
        "ATA по котировке -- ровно те адреса, что стоят местами 27 и 28 в "
        "настоящем переезде 31HX7jHP…",
        хв == ["BzoFDKSt4KVSaJrfAt2tVAhsU232eTYj8dDjEL1b3eM7",
               "5UZaWawKHMXoXDoAvpAK99za2zdV6EjHqaVcjeGaoYk4"], хв)
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
                           min_sol_output=1,
                           base_token_program=ПРОГ_ТОКЕНА_2022)
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
    # УМОЛЧАНИЯ ЗДЕСЬ НЕТ ПО ТОЙ ЖЕ ПРИЧИНЕ, ЧТО И В ОБЁРТКАХ: раньше в этой
    # ветке стояло ПРОГ_ТОКЕНА строкой, то есть ручной осмотр раскладки
    # показывал адреса для классического Token, а у пампового минта они другие.
    p.add_argument("--token-program", default=None,
                   help="программа токена базового минта: Token или Token-2022 "
                        "(умолчания нет -- см. programma_tokena_minta)")
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
                      "base_token_program": _проверить_программу_токена(
                          a.token_program),
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
