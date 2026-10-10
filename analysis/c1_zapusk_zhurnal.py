#!/usr/bin/env python3
"""ТЕНЕВОЙ ЖУРНАЛ ЗАПУСКОВ pump.fun: создания токенов, БЕЗ ПОКУПОК.

ЗАДАНИЕ ВЛАДЕЛЬЦА (09.10, п.7). Детектор видит создание токена (create /
create_v2 программы 6EF8...) и пишет: время, слот создания, купил ли создатель
в транзакции создания и сколько SOL, наш слот готовности к отправке и сколько
слотов от создания до нашей возможной посадки. Покупок нет ни одной.

ПОДПИСКА -- САМАЯ УЗКАЯ, И ЭТО ИЗМЕРЕНО, А НЕ ВЫБРАНО НА ВКУС. Подписаться на
всю программу кривой значит получать КАЖДУЮ покупку и продажу всех её минтов --
это десятки сообщений в секунду и счёт Helius за трафик. Нужен счёт, который
есть ТОЛЬКО в транзакциях создания. Такой счёт в IDL один: mint_authority, PDA
по семени "mint-authority". Он стоит в create (место 1) и в create_v2 (место 1)
и НЕ встречается ни в одной из остальных 54 инструкций pump -- проверка
odin_tolko_u_sozdanij это и считает по самому IDL, а не по памяти.

ЧЕГО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ: не покупает, не продаёт, не трогает группы и
политики, не зовёт узел. Он разбирает уже полученную транзакцию и пишет строку
в журнал. Включение торговли по созданиям -- отдельная группа zapusk_dev и
только по слову владельца (п.8), здесь её нет.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

ПРОГ_КРИВОЙ = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
СОЗДАНИЯ = ("create", "create_v2")
# Покупка кривой в ТОЙ ЖЕ транзакции -- любая разновидность по именам IDL.
ПОКУПКИ = ("buy_exact_quote_in_v3", "buy_v3", "buy_exact_quote_in_v2", "buy_v2")
# ПОКУПКИ, КОТОРЫМИ ПОЛОСА УЖЕ ХОДИЛА НА ДЕНЬГАХ. Сигналом запуска годится
# только такая разновидность: слово владельца 10.10 п.2 -- "покупка через уже
# проверенный путь кривой v3". v2 остаётся отказом по имени, а не тихой
# попыткой на непроверенном пути.
ПОКУПКИ_ОБКАТАННЫЕ = ("buy_exact_quote_in_v3", "buy_v3")
WSOL = "So11111111111111111111111111111111111111112"
# Причины отказа сигнала запуска -- названные и перечислимые: по ним считается
# воронка, а строка why_not читается человеком.
ОТКАЗ_НЕТ_СОЗДАНИЯ = "net_sozdaniya"
ОТКАЗ_НЕ_КУПИЛ = "sozdatel_ne_kupil"
ОТКАЗ_MAYHEM = "mayhem"
ОТКАЗ_КОТИРОВКА = "kotirovka_ne_sol"
ОТКАЗ_РАЗНОВИДНОСТЬ = "razvidnost_ne_obkatana"
ОТКАЗ_НЕТ_ПОЛЕЙ = "net_poley_sdelki"
# ИМЯ ГРУППЫ ЗАПУСКОВ -- ОДНО НА ДВА МОДУЛЯ. Детектор спрашивает её у файла
# групп, полоса отдаёт её правилом членства; два написанных руками имени
# разошлись бы молча, и покупка по созданию ушла бы в чужую группу.
ГРУППА_ЗАПУСКОВ_ИМЯ = "zapusk_dev"
ИМЯ_ЖУРНАЛА = "zapusk_zhurnal.jsonl"
# Длина слота, если замера нет. ЧИСЛО НЕ ПОДСТАВЛЯЕТСЯ МОЛЧА: когда замера нет,
# отрыв в слотах отдаётся None с названной причиной, а не считается по
# константе. Константа нужна только для самопроверки.
ДЛИНА_СЛОТА_ПО_УМОЛЧАНИЮ_S = 0.274
WHY_НЕТ_ДЛИНЫ = "длина слота не измерена -- отрыв в слотах не считаем"


def _сборщик():
    try:
        import c3_pump_sborka as PS  # noqa: PLC0415

        return PS
    except Exception:  # noqa: BLE001
        return None


def pda_mint_authority() -> str:
    """PDA ["mint-authority"] программы кривой. Выводится, а не зашит.

    Зашитый адрес разошёлся бы с программой молча; вывод по семени из IDL
    проверяется самопроверкой против того же IDL.
    """
    from solders.pubkey import Pubkey  # noqa: PLC0415

    пда, _ = Pubkey.find_program_address([b"mint-authority"],
                                         Pubkey.from_string(ПРОГ_КРИВОЙ))
    return str(пда)


def odin_tolko_u_sozdanij() -> dict:
    """Счёт mint_authority встречается ТОЛЬКО у create и create_v2?

    Считается по самому IDL: если программа однажды добавит его ещё куда-то,
    подписка перестанет быть узкой, и узнать это надо проверкой, а не по
    расходу Helius через сутки.
    """
    из_ = {"ok": False, "gde": [], "instrukcij": 0, "why_not": None}
    PS = _сборщик()
    if PS is None:
        из_["why_not"] = "сборщик по IDL не доступен"
        return из_
    try:
        ix = PS.zagruzit_idl()["pump"]["ix"]
    except Exception as сбой:  # noqa: BLE001
        из_["why_not"] = f"IDL не читается: {type(сбой).__name__}"
        return из_
    из_["instrukcij"] = len(ix)
    for имя, зн in ix.items():
        if any(а["name"] == "mint_authority" for а in зн["accounts"]):
            из_["gde"].append(имя)
    из_["gde"].sort()
    из_["ok"] = из_["gde"] == sorted(СОЗДАНИЯ)
    if not из_["ok"]:
        из_["why_not"] = (f"mint_authority встречается в {из_['gde']}, а не "
                          f"только в {sorted(СОЗДАНИЯ)} -- подписка на него "
                          "больше не узкая")
    return из_


def _instrukcii(tx: dict) -> list:
    """Внешние и внутренние инструкции одним списком, с адресами счетов."""
    соо = ((tx or {}).get("transaction") or {}).get("message") or {}
    ключи = [k.get("pubkey") if isinstance(k, dict) else k
             for k in (соо.get("accountKeys") or [])]
    сырые = list(соо.get("instructions") or [])
    for вн in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        сырые.extend(вн.get("instructions") or [])
    из_ = []
    for и in сырые:
        счета = [ключи[с] if isinstance(с, int) and с < len(ключи) else с
                 for с in (и.get("accounts") or [])]
        из_.append({"programId": и.get("programId"), "data": и.get("data"),
                    "accounts": счета})
    return из_


def kodirovka_ne_ta(инстр: list) -> str | None:
    """Инструкции есть, а программ у них нет -- пришла ЧУЖАЯ кодировка.

    ЗАЧЕМ ОТДЕЛЬНОЙ ПРОВЕРКОЙ. Разбор читает и["programId"], а это поле есть
    только у encoding="jsonParsed". Подписка просит именно его, но стоит
    кому-то подать транзакцию с encoding "json" или "base64" -- и у КАЖДОЙ
    инструкции programId выйдет None, ни одна не совпадёт с программой кривой,
    и разбор честно скажет "создания в транзакции нет". Отличить "её там нет"
    от "мы её не умеем прочитать" было бы НЕЧЕМ.

    Этот репозиторий на этом уже обжигался: 28.09 своя продажа НИ РАЗУ не
    нашла нашу покупку по той же причине (bloom_lane_sell:598).
    """
    if not инстр:
        return None
    if any(и.get("programId") for и in инстр):
        return None
    return (f"у всех {len(инстр)} инструкций нет programId -- пришла не "
            "jsonParsed: разбор создания невозможен, а не «создания нет»")


def _po_imenam(PS, имя: str, счета: list) -> dict:
    имена = [а["name"] for а in PS.zagruzit_idl()["pump"]["ix"][имя]["accounts"]]
    return dict(zip(имена, счета, strict=False))


def sol_sozdatelya(tx: dict, sozdatel: str) -> dict:
    """Сколько SOL создатель отдал в транзакции создания -- ПО БАЛАНСАМ.

    Считается дельта нативного баланса создателя (preBalances/postBalances):
    это измерение, а не аргумент инструкции. Аргумент max_sol_cost у buy_v*
    это ПОТОЛОК, а не трата, и выдать его за трату значило бы соврать; у
    buy_exact_quote_in_* первый аргумент и есть трата, но и он не включает
    ренту счетов и комиссию, которые создатель тоже платит.

    Из дельты вычитается комиссия транзакции: она есть в meta.fee и платится
    первым подписантом. Остаток -- это и есть "сколько он вложил".
    """
    из_ = {"lamportov": None, "sol": None, "why_not": None, "fee": None}
    мета = (tx or {}).get("meta") or {}
    соо = ((tx or {}).get("transaction") or {}).get("message") or {}
    ключи = [k.get("pubkey") if isinstance(k, dict) else k
             for k in (соо.get("accountKeys") or [])]
    до, после = мета.get("preBalances"), мета.get("postBalances")
    if not (isinstance(до, list) and isinstance(после, list) and ключи):
        из_["why_not"] = "балансов в транзакции нет"
        return из_
    if sozdatel not in ключи:
        из_["why_not"] = "создателя нет в списке счетов"
        return из_
    и = ключи.index(sozdatel)
    if и >= len(до) or и >= len(после):
        из_["why_not"] = "списки балансов короче списка счетов"
        return из_
    комиссия = int(мета.get("fee") or 0)
    из_["fee"] = комиссия
    ушло = int(до[и]) - int(после[и])
    # Комиссию платит ПЕРВЫЙ подписант. Если создатель -- он, комиссия входит
    # в дельту и её надо вычесть; иначе нет.
    if и == 0:
        ушло -= комиссия
    из_["lamportov"] = ушло
    из_["sol"] = round(ушло / 1e9, 9)
    return из_


def razbor_sozdanija(tx: dict, *, slot_sozdanija=None) -> dict:
    """Создание токена в этой транзакции: минт, создатель, его покупка.

    Возвращает ok=False с названной причиной, если создания в транзакции нет --
    молча пустую запись не отдаёт.
    """
    из_ = {"ok": False, "why_not": None, "sozdanie": None, "mint": None,
           "sozdatel": None, "slot_sozdanija": slot_sozdanija,
           "kupil_v_sozdanii": False, "pokupka": None,
           "sol_sozdatelya": None, "lamportov_sozdatelya": None,
           "is_mayhem_mode": None, "quote_mint": None, "quote_sol": None,
           "base_token_program": None}
    PS = _сборщик()
    if PS is None:
        из_["why_not"] = "сборщик по IDL не доступен"
        return из_
    try:
        ix = PS.zagruzit_idl()["pump"]["ix"]
    except Exception as сбой:  # noqa: BLE001
        из_["why_not"] = f"IDL не читается: {type(сбой).__name__}"
        return из_
    по_диску = {зн["disc"].hex(): имя for имя, зн in ix.items()}
    инстр = _instrukcii(tx)
    не_та = kodirovka_ne_ta(инстр)
    if не_та:
        из_["why_not"] = не_та
        из_["kodirovka_ne_ta"] = True
        return из_
    покупки = []
    for и in инстр:
        if и.get("programId") != ПРОГ_КРИВОЙ or not isinstance(и.get("data"), str):
            continue
        try:
            сырые = PS.b58d(и["data"])
        except Exception:  # noqa: BLE001
            continue
        имя = по_диску.get(сырые[:8].hex())
        if имя in СОЗДАНИЯ and из_["sozdanie"] is None:
            по_именам = _po_imenam(PS, имя, и["accounts"])
            из_.update(ok=True, sozdanie=имя, mint=по_именам.get("mint"),
                       sozdatel=по_именам.get("user"))
            # MAYHEM -- ИЗ АРГУМЕНТОВ САМОГО СОЗДАНИЯ, без единого вызова сети.
            м = mayhem_iz_dannyh(сырые, имя)
            из_["is_mayhem_mode"] = м["is_mayhem_mode"]
            if м["why_not"]:
                из_["mayhem_why_not"] = м["why_not"]
        elif имя in ПОКУПКИ:
            по_именам = _po_imenam(PS, имя, и["accounts"])
            покупки.append({"imya": имя, "mint": по_именам.get("base_mint"),
                            "user": по_именам.get("user"),
                            # КОТИРОВКА И ПРОГРАММА БАЗЫ -- ИЗ СЧЕТОВ ПОКУПКИ
                            # ПО ИМЕНАМ IDL. В самом создании котировки нет
                            # вовсе (она живёт в счёте кривой), а в покупке
                            # создателя она стоит местом -- и читается даром.
                            "quote_mint": по_именам.get("quote_mint"),
                            "base_token_program": по_именам.get(
                                "base_token_program")})
    if not из_["ok"]:
        из_["why_not"] = "create/create_v2 в транзакции не найдена"
        return из_
    # ПОКУПКА СОЗДАТЕЛЯ -- ЭТО ПОКУПКА ЭТОГО МИНТА ЭТИМ ЖЕ КОШЕЛЬКОМ. Любая
    # покупка в транзакции создания не считается: там бывает и чужая.
    своя = [п for п in покупки
            if п["mint"] == из_["mint"] and п["user"] == из_["sozdatel"]]
    if своя:
        из_["kupil_v_sozdanii"] = True
        из_["pokupka"] = своя[0]["imya"]
        из_["quote_mint"] = своя[0].get("quote_mint")
        из_["quote_sol"] = (своя[0].get("quote_mint") == WSOL
                            if своя[0].get("quote_mint") else None)
        из_["base_token_program"] = своя[0].get("base_token_program")
        д = sol_sozdatelya(tx, из_["sozdatel"] or "")
        из_["sol_sozdatelya"] = д["sol"]
        из_["lamportov_sozdatelya"] = д["lamportov"]
        if д["why_not"]:
            из_["sol_why_not"] = д["why_not"]
    return из_


def _borsh_stroka(данные: bytes, сдвиг: int) -> tuple:
    """Строка borsh: u32 длины и байты. Отдаёт (следующий сдвиг, длина)."""
    if сдвиг + 4 > len(данные):
        return (None, None)
    длина = int.from_bytes(данные[сдвиг:сдвиг + 4], "little")
    конец = сдвиг + 4 + длина
    if длина > len(данные) or конец > len(данные):
        return (None, None)
    return (конец, длина)


def dannye_sozdanija(razvidnost: str, *, name: str = "Имя", symbol: str = "ТКН",
                     uri: str = "https://пример/метаданные.json",
                     creator: bytes = b"\x07" * 32, mayhem: bool = False) -> bytes:
    """Аргументы создания раскладкой borsh -- ДЛЯ САМОПРОВЕРКИ И СВЕРКИ.

    Нужна отдельной функцией, а не внутри теста: разбор и сборка одних и тех же
    байтов обязаны жить рядом, иначе проверка начнёт подтверждать сама себя на
    байтах, каких в цепи не бывает. Строки берутся РАЗНОЙ длины нарочно: на
    строках одной длины зашитое смещение прошло бы проверку.
    """
    из_ = b""
    for т in (name, symbol, uri):
        б = т.encode("utf-8")
        из_ += len(б).to_bytes(4, "little") + б
    из_ += bytes(creator)
    if razvidnost == "create_v2":
        из_ += bytes([1 if mayhem else 0])
        из_ += bytes([0, 0, 0])          # три Option*, все None
    return из_


def mayhem_iz_dannyh(данные: bytes, razvidnost: str) -> dict:
    """is_mayhem_mode из аргументов create_v2. Разбор ПОСЛЕДОВАТЕЛЬНЫЙ.

    СМЕЩЕНИЕ НЕЛЬЗЯ ЗАШИТЬ ЧИСЛОМ. Аргументы create_v2 по IDL: name, symbol,
    uri (три строки borsh -- u32 длины плюс байты), creator (32 байта), и уже
    затем is_mayhem_mode (1 байт). Имя, тикер и ссылка у каждого токена своей
    длины, поэтому байт признака у каждого создания стоит на СВОЁМ месте, и
    любое зашитое число читало бы чужой байт -- то есть иногда выдавало бы
    mayhem там, где его нет, и наоборот.

    У create (v1) аргумента нет вовсе: name, symbol, uri, creator и всё. Это
    НЕ "mayhem выключен", а "поля нет" -- и отдаётся None с причиной.
    """
    из_ = {"is_mayhem_mode": None, "why_not": None, "smeshchenie": None}
    if razvidnost != "create_v2":
        из_["why_not"] = (f"у {razvidnost} аргумента is_mayhem_mode нет вовсе "
                          "-- поля нет, а не выключено")
        return из_
    сдвиг = 8                                   # дискриминатор Anchor
    for какая in ("name", "symbol", "uri"):
        сдвиг, _дл = _borsh_stroka(данные, сдвиг)
        if сдвиг is None:
            из_["why_not"] = f"аргументы обрываются на строке {какая}"
            return из_
    сдвиг += 32                                 # creator -- pubkey
    if сдвиг >= len(данные):
        из_["why_not"] = "аргументы обрываются до is_mayhem_mode"
        return из_
    байт = данные[сдвиг]
    if байт not in (0, 1):
        # BOOL В BORSH -- ЭТО 0 ИЛИ 1. Любое иное значение значит, что разбор
        # уехал: молча считать его истиной нельзя.
        из_["why_not"] = (f"на месте is_mayhem_mode байт {байт} -- это не bool, "
                          "разбор аргументов уехал")
        из_["smeshchenie"] = сдвиг
        return из_
    из_.update(is_mayhem_mode=bool(байт), smeshchenie=сдвиг)
    return из_


def signal_zapuska(tx: dict, *, podpis: str | None = None,
                   slot: int | None = None) -> dict:
    """Годится ли это создание в СИГНАЛ ПОКУПКИ. Все отказы -- ДО подписи.

    Условия владельца (10.10, п.2), в этом порядке, и каждое названо своим
    кодом отказа:
      1. в транзакции есть create или create_v2;
      2. СОЗДАТЕЛЬ КУПИЛ в этой же транзакции -- иначе покупать не за кем;
      3. не mayhem-режим;
      4. котировка кривой -- SOL (WSOL): полоса платит солами, и на кривой с
         чужой котировкой программа ответила бы 6004 уже после оплаты чаевых
         (замер 09.10 на отказанных сигналах vol_4vw);
      5. разновидность покупки -- из обкатанных v3: покупка идёт уже
         проверенным путём, а не новым.

    Эта функция НЕ покупает и НЕ подписывает: она только отвечает да/нет и
    отдаёт поля, которых ждёт денежный путь. Торговлю включает отдельная
    дверь, и только по слову владельца.
    """
    из_ = {"ok": False, "why_not": None, "otkaz_vid": None,
           "signature": podpis, "slot_sozdanija": slot,
           "istochnik": None, "mint": None, "sozdanie": None,
           "pokupka": None, "is_mayhem_mode": None, "quote_mint": None,
           "base_token_program": None, "sol_sozdatelya": None,
           "lamportov_sozdatelya": None}
    р = razbor_sozdanija(tx, slot_sozdanija=slot)
    for поле in ("sozdanie", "mint", "is_mayhem_mode", "quote_mint",
                  "base_token_program", "sol_sozdatelya",
                  "lamportov_sozdatelya", "pokupka"):
        из_[поле] = р.get(поле)
    из_["istochnik"] = р.get("sozdatel")
    из_["slot_sozdanija"] = р.get("slot_sozdanija")
    if not р.get("ok"):
        из_.update(why_not=р.get("why_not"), otkaz_vid=ОТКАЗ_НЕТ_СОЗДАНИЯ)
        return из_
    if not р.get("kupil_v_sozdanii"):
        из_.update(why_not="создатель в транзакции создания не покупал",
                   otkaz_vid=ОТКАЗ_НЕ_КУПИЛ)
        return из_
    if р.get("is_mayhem_mode") is True:
        из_.update(why_not="mayhem-режим -- по слову владельца пропускаем",
                   otkaz_vid=ОТКАЗ_MAYHEM)
        return из_
    if р.get("is_mayhem_mode") is None:
        # НЕ ЗНАЕМ -- ЗНАЧИТ НЕ ИДЁМ. Пропускать mayhem велено явно, и
        # "признак не прочитался" это не "mayhem выключен".
        из_.update(why_not=(f"mayhem не прочитан: "
                            f"{р.get('mayhem_why_not') or 'причина не названа'}"
                            " -- не знаем, значит не идём"),
                   otkaz_vid=ОТКАЗ_MAYHEM)
        return из_
    if not р.get("quote_mint"):
        из_.update(why_not="котировки кривой в покупке создателя нет",
                   otkaz_vid=ОТКАЗ_НЕТ_ПОЛЕЙ)
        return из_
    if р.get("quote_mint") != WSOL:
        из_.update(why_not=(f"котировка кривой {str(р['quote_mint'])[:12]} не "
                            "SOL -- полоса платит солами, купить нечем"),
                   otkaz_vid=ОТКАЗ_КОТИРОВКА)
        return из_
    if р.get("pokupka") not in ПОКУПКИ_ОБКАТАННЫЕ:
        из_.update(why_not=(f"разновидность покупки {р.get('pokupka')} не из "
                            f"обкатанных {list(ПОКУПКИ_ОБКАТАННЫЕ)}"),
                   otkaz_vid=ОТКАЗ_РАЗНОВИДНОСТЬ)
        return из_
    if not (из_["istochnik"] and из_["mint"] and из_["base_token_program"]):
        из_.update(why_not="в сделке нет создателя, минта или программы базы",
                   otkaz_vid=ОТКАЗ_НЕТ_ПОЛЕЙ)
        return из_
    из_["ok"] = True
    return из_


def nash_otryv(*, slot_sozdanija, slot_seti, vozrast_slota_s,
               gotov_cherez_s, dlina_slota_s=None) -> dict:
    """Наш слот готовности к отправке и отрыв от создания, в слотах.

    ЧТО ИЗ ЧЕГО. slot_seti -- голова цепи, какой её знает детектор, и она
    СТАРЕЕТ: vozrast_slota_s говорит, насколько. gotov_cherez_s -- сколько
    прошло от получения сообщения до момента, когда отправлять уже можно.
    Голова на момент готовности = slot_seti + (возраст + готовность) / длина
    слота. Отрыв = этот слот минус слот создания, плюс один: раньше
    СЛЕДУЮЩЕГО слота наша транзакция в блок не попадёт.

    Длины слота нет -- отдаём None с причиной. Подставить константу значило бы
    выдать предположение за замер.
    """
    из_ = {"nash_slot_gotovnosti": None, "otryv_slotov": None,
           "dlina_slota_s": dlina_slota_s, "why_not": None}
    if not isinstance(slot_sozdanija, int) or slot_sozdanija <= 0:
        из_["why_not"] = "слот создания не известен"
        return из_
    if not (isinstance(dlina_slota_s, (int, float)) and dlina_slota_s > 0):
        из_["why_not"] = WHY_НЕТ_ДЛИНЫ
        return из_
    голова = slot_sozdanija
    if isinstance(slot_seti, int) and slot_seti > 0:
        прошло = float(vozrast_slota_s or 0.0) + float(gotov_cherez_s or 0.0)
        голова = max(slot_sozdanija, slot_seti + int(прошло / dlina_slota_s))
    else:
        из_["why_not"] = ("слот сети не известен -- готовность считаем от "
                          "слота создания, то есть НИЖНЕЙ границей")
        голова = slot_sozdanija + int(float(gotov_cherez_s or 0.0)
                                      / dlina_slota_s)
    из_["nash_slot_gotovnosti"] = голова + 1
    из_["otryv_slotov"] = из_["nash_slot_gotovnosti"] - slot_sozdanija
    return из_


def put_zhurnala(state_dir=None) -> Path:
    база = state_dir or os.environ.get("BLOOM_STATE_DIR") or "."
    return Path(база) / ИМЯ_ЖУРНАЛА


def zapisat(zapis: dict, *, state_dir=None) -> dict:
    """Строка в журнал. Дописыванием, одной строкой, без чтения файла."""
    из_ = {"ok": False, "fajl": None, "why_not": None}
    try:
        п = put_zhurnala(state_dir)
        п.parent.mkdir(parents=True, exist_ok=True)
        with п.open("a", encoding="utf-8") as ф:
            ф.write(json.dumps(zapis, ensure_ascii=False) + "\n")
        из_.update(ok=True, fajl=str(п))
    except OSError as сбой:
        из_["why_not"] = f"журнал не записан: {type(сбой).__name__}: {сбой}"
    return из_


def svod(stroki, *, s=None, do=None) -> dict:
    """Свод журнала за окно: созданий, из них с покупкой создателя, наш отрыв.

    Чистая функция: на вход строки файла, на выход числа. Время сравнивается
    как строка ISO с Z -- она лексикографически упорядочена.
    """
    из_ = {"s": s, "do": do, "strok_vsego": 0, "sozdanij": 0,
           "s_pokupkoj_sozdatelya": 0, "sol_sozdateley": 0.0,
           "otryv_slotov": {"min": None, "max": None, "mediana": None,
                            "bez_chisla": 0},
           # РАСХОД HELIUS -- ИЗ САМИХ ЗАПИСЕЙ, А НЕ ИЗ СЧЁТЧИКА ПРОЦЕССА.
           # Счётчик обнуляется перезапуском службы, и "расход за первый час"
           # после деплоя стал бы меньше правды. Байты лежат в каждой строке,
           # тариф тот же, что у общего учёта: 2 кредита за 0.1 МиБ с
           # округлением вверх.
           "bajt": 0, "kreditov": 0, "bez_bajt": 0,
           "pervaja_utc": None, "poslednjaja_utc": None, "razvidnosti": {}}
    отрывы = []
    for сырая in stroki:
        из_["strok_vsego"] += 1
        с_ = сырая.strip() if isinstance(сырая, str) else ""
        if not с_:
            continue
        try:
            з = json.loads(с_)
        except ValueError:
            continue
        if not isinstance(з, dict) or not з.get("sozdanie"):
            continue
        т = str(з.get("ts_utc") or "")
        if s and т and т < s:
            continue
        if do and т and т > do:
            continue
        из_["sozdanij"] += 1
        if т:
            из_["pervaja_utc"] = (т if из_["pervaja_utc"] is None
                                  else min(из_["pervaja_utc"], т))
            из_["poslednjaja_utc"] = (т if из_["poslednjaja_utc"] is None
                                      else max(из_["poslednjaja_utc"], т))
        из_["razvidnosti"][з["sozdanie"]] = (
            из_["razvidnosti"].get(з["sozdanie"], 0) + 1)
        if з.get("kupil_v_sozdanii"):
            из_["s_pokupkoj_sozdatelya"] += 1
            if isinstance(з.get("sol_sozdatelya"), (int, float)):
                из_["sol_sozdateley"] += float(з["sol_sozdatelya"])
        б = з.get("bajt")
        if isinstance(б, int) and б > 0:
            из_["bajt"] += б
        else:
            из_["bez_bajt"] += 1
        о = з.get("otryv_slotov")
        if isinstance(о, int):
            отрывы.append(о)
        else:
            из_["otryv_slotov"]["bez_chisla"] += 1
    из_["sol_sozdateley"] = round(из_["sol_sozdateley"], 9)
    из_["kreditov"] = -(-из_["bajt"] // 104858) * 2 if из_["bajt"] else 0
    из_["mb"] = round(из_["bajt"] / 1048576, 3)
    if отрывы:
        отрывы.sort()
        из_["otryv_slotov"].update(min=отрывы[0], max=отрывы[-1],
                                   mediana=отрывы[len(отрывы) // 2])
    return из_


def zapis_iz_uvedomlenija(res: dict, *, t_polucheno=None, slot_seti=None,
                          vozrast_slota_s=None, dlina_slota_s=None,
                          bajt=None) -> dict:
    """Готовая строка журнала из уведомления transactionSubscribe.

    Одна функция на весь путь: разбор создания, покупка создателя, наш отрыв.
    Детектору остаётся позвать её и записать результат.
    """
    соо = res if isinstance(res, dict) else {}
    tx = соо.get("transaction") if isinstance(соо.get("transaction"), dict) else соо
    слот = соо.get("slot")
    if not isinstance(слот, int):
        слот = (соо.get("context") or {}).get("slot")
    р = razbor_sozdanija(tx, slot_sozdanija=слот if isinstance(слот, int) else None)
    тепер = time.time()
    готов = (тепер - float(t_polucheno)) if t_polucheno else 0.0
    о = nash_otryv(slot_sozdanija=р.get("slot_sozdanija"), slot_seti=slot_seti,
                   vozrast_slota_s=vozrast_slota_s, gotov_cherez_s=готов,
                   dlina_slota_s=dlina_slota_s)
    # ПОДПИСЬ СОЗДАНИЯ -- В ЗАПИСЬ. Без неё строку журнала не привязать к цепи
    # вовсе: ни проверить разбор, ни прогнать сигнал денежным путём, ни отдать
    # Code-2 подпись создания рядом с подписями сделки.
    подпись = соо.get("signature")
    if not подпись:
        подпись = (((соо.get("transaction") or {}).get("transaction") or {})
                   .get("signatures") or [None])[0]
    if not подпись:
        подпись = ((tx or {}).get("transaction") or {}).get("signatures", [None])[0]
    р.update(signature=подпись,
             signature_why_not=(None if подпись else
                                "подписи создания в уведомлении не нашлось"),
             ts_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(тепер)),
             gotov_cherez_ms=round(готов * 1000, 1),
             nash_slot_seti=slot_seti, net_slot_age_s=vozrast_slota_s,
             nash_slot_gotovnosti=о["nash_slot_gotovnosti"],
             otryv_slotov=о["otryv_slotov"],
             dlina_slota_s=о["dlina_slota_s"],
             otryv_why_not=о["why_not"], bajt=bajt,
             pokupok_net=True)
    return р


# ------------------------------------------------------------- самопроверка

ЖДЁМ_ПРОВЕРОК = 38


def self_test() -> int:  # noqa: C901, PLR0915
    сбоев = всего = 0

    def chk(имя: str, ок: bool, что=None) -> None:
        nonlocal сбоев, всего
        всего += 1
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}")
        if not ок:
            сбоев += 1
            if что is not None:
                print(f"         {что!r}")

    PS = _сборщик()
    if PS is None:
        print("СБОЙ: сборщик по IDL не доступен -- проверять нечего")
        return 1
    ix = PS.zagruzit_idl()["pump"]["ix"]

    у = odin_tolko_u_sozdanij()
    chk("mint_authority есть ТОЛЬКО у create и create_v2 -- подписка на него "
        f"самая узкая (инструкций в IDL {у['instrukcij']})",
        у["ok"] and у["gde"] == ["create", "create_v2"], у)
    chk("он стоит местом 1 у обеих -- то есть присутствует в КАЖДОМ создании, "
        "а не у части",
        [а["name"] for а in ix["create"]["accounts"]][1] == "mint_authority"
        and [а["name"] for а in ix["create_v2"]["accounts"]][1]
        == "mint_authority")
    try:
        пда = pda_mint_authority()
        chk("PDA выводится по семени 'mint-authority' программы кривой",
            isinstance(пда, str) and len(пда) >= 32, пда)
        chk("адрес PDA не зашит в модуль числом -- он выведен",
            пда not in Path(__file__).read_text(encoding="utf-8"), пда)
    except ImportError:
        chk("solders на машине нет -- PDA не проверялся (пропуск, не зелёный)",
            False)

    def _тx(*, создание="create", минт="M" * 44, создатель="U" * 44,
            покупка=None, покупка_минт=None, покупка_user=None, mayhem=False,
            котировка=None,
            до=(2_000_000_000, 0), после=(1_000_000_000, 0), fee=5000):
        """Транзакция в виде ответа узла. Счета -- по именам IDL."""
        инстр = []
        имена = [а["name"] for а in ix[создание]["accounts"]]
        счета = []
        for имя in имена:
            счета.append({"mint": минт, "user": создатель}.get(имя, imya_zapolnitel(имя)))
        # ДАННЫЕ СОЗДАНИЯ -- НАСТОЯЩЕЙ РАСКЛАДКОЙ borsh, а не нулями: иначе
        # разбор is_mayhem_mode проверялся бы на том, чего в цепи не бывает.
        инстр.append({"programId": ПРОГ_КРИВОЙ,
                      "data": PS.b58e(ix[создание]["disc"]
                                      + dannye_sozdanija(создание,
                                                         mayhem=mayhem)),
                      "accounts": счета})
        if покупка:
            имена_п = [а["name"] for а in ix[покупка]["accounts"]]
            счета_п = []
            for имя in имена_п:
                счета_п.append({"base_mint": покупка_минт or минт,
                                "user": покупка_user or создатель,
                                "quote_mint": котировка or WSOL,
                                "base_token_program": PS.ПРОГ_ТОКЕНА_2022}.get(
                                    имя, imya_zapolnitel(имя)))
            инстр.append({"programId": ПРОГ_КРИВОЙ,
                          "data": PS.b58e(ix[покупка]["disc"] + b"\x00" * 17),
                          "accounts": счета_п})
        ключи = [создатель, минт]
        return {"transaction": {"message": {
            "accountKeys": [{"pubkey": к} for к in ключи],
            "instructions": [{"programId": и["programId"], "data": и["data"],
                              "accounts": и["accounts"]} for и in инстр]}},
                "meta": {"innerInstructions": [], "fee": fee,
                         "preBalances": list(до), "postBalances": list(после)}}

    def imya_zapolnitel(имя: str) -> str:
        """Заполнитель адреса по имени счёта -- чтобы разбор брал ИМЕНА."""
        return имя[:1].upper() + "z" * 43

    р = razbor_sozdanija(_тx(), slot_sozdanija=100)
    chk("создание разобрано: имя, минт, создатель, слот",
        р["ok"] and р["sozdanie"] == "create" and р["mint"] == "M" * 44
        and р["sozdatel"] == "U" * 44 and р["slot_sozdanija"] == 100, р)
    chk("создатель НЕ покупал -- признак False, и покупки нет",
        р["kupil_v_sozdanii"] is False and р["pokupka"] is None)

    р2 = razbor_sozdanija(_тx(создание="create_v2",
                              покупка="buy_exact_quote_in_v3"),
                          slot_sozdanija=101)
    chk("create_v2 с покупкой создателя: признак True и имя покупки названо",
        р2["ok"] and р2["sozdanie"] == "create_v2"
        and р2["kupil_v_sozdanii"] is True
        and р2["pokupka"] == "buy_exact_quote_in_v3", р2)
    chk("SOL создателя считается ПО БАЛАНСАМ минус комиссия: 2.0 -> 1.0 при "
        "комиссии 5000 даёт 0.999995",
        р2["sol_sozdatelya"] == 0.999995, р2["sol_sozdatelya"])

    р3 = razbor_sozdanija(_тx(покупка="buy_exact_quote_in_v3",
                              покупка_user="X" * 44), slot_sozdanija=102)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: покупка ЧУЖИМ кошельком в транзакции создания за "
        "покупку создателя не считается",
        р3["ok"] and р3["kupil_v_sozdanii"] is False, р3)
    р4 = razbor_sozdanija(_тx(покупка="buy_exact_quote_in_v3",
                              покупка_минт="N" * 44), slot_sozdanija=103)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: покупка ДРУГОГО минта тем же кошельком тоже не "
        "считается", р4["ok"] and р4["kupil_v_sozdanii"] is False, р4)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: транзакция без создания -- отказ по имени, а не "
        "пустая запись",
        razbor_sozdanija({"transaction": {"message": {"accountKeys": [],
                                                      "instructions": []}}})
        ["why_not"] == "create/create_v2 в транзакции не найдена")

    о = nash_otryv(slot_sozdanija=1000, slot_seti=1000, vozrast_slota_s=0.0,
                   gotov_cherez_s=0.0, dlina_slota_s=0.274)
    chk("готовность в том же слоте -- посадка в следующем, отрыв 1",
        о["nash_slot_gotovnosti"] == 1001 and о["otryv_slotov"] == 1, о)
    о2 = nash_otryv(slot_sozdanija=1000, slot_seti=1000, vozrast_slota_s=0.5,
                    gotov_cherez_s=0.3, dlina_slota_s=0.274)
    chk("0.8 с при слоте 0.274 с -- это два слота, отрыв 3",
        о2["otryv_slotov"] == 3, о2)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: без измеренной длины слота отрыв None с причиной, "
        "а не по константе",
        nash_otryv(slot_sozdanija=1000, slot_seti=1000, vozrast_slota_s=0,
                   gotov_cherez_s=0)["why_not"] == WHY_НЕТ_ДЛИНЫ)
    о3 = nash_otryv(slot_sozdanija=1000, slot_seti=None, vozrast_slota_s=None,
                    gotov_cherez_s=0.0, dlina_slota_s=0.274)
    chk("слота сети нет -- отрыв считается НИЖНЕЙ границей, и это сказано",
        о3["otryv_slotov"] == 1 and "НИЖНЕЙ" in (о3["why_not"] or ""), о3)

    chk("ДОКАЗАННЫЙ КРАСНЫЙ: инструкции есть, а программ у них нет -- это "
        "ЧУЖАЯ КОДИРОВКА, и так и сказано, а не «создания нет» (ровно так "
        "28.09 своя продажа ни разу не нашла нашу покупку)",
        razbor_sozdanija({"transaction": {"message": {
            "accountKeys": [{"pubkey": "A" * 44}],
            "instructions": [{"data": "xx", "accounts": [0]},
                             {"data": "yy", "accounts": [0]}]}}}
        ).get("kodirovka_ne_ta") is True)
    chk("пустой список инструкций чужой кодировкой НЕ зовётся",
        kodirovka_ne_ta([]) is None
        and kodirovka_ne_ta([{"programId": ПРОГ_КРИВОЙ}]) is None)
    # --- MAYHEM ИЗ АРГУМЕНТОВ (слово владельца 10.10, п.2)
    _д2 = dannye_sozdanija("create_v2", mayhem=False)
    _д2м = dannye_sozdanija("create_v2", mayhem=True)
    chk("is_mayhem_mode читается из аргументов create_v2: выключен и включен",
        mayhem_iz_dannyh(b"\x00" * 8 + _д2, "create_v2")["is_mayhem_mode"] is False
        and mayhem_iz_dannyh(b"\x00" * 8 + _д2м, "create_v2"
                              )["is_mayhem_mode"] is True)
    chk("смещение признака СЧИТАЕТСЯ по строкам, а не зашито: имя длиннее -- "
        "и байт признака уезжает ровно на разницу",
        mayhem_iz_dannyh(b"\x00" * 8 + dannye_sozdanija(
            "create_v2", name="Имя подлиннее", mayhem=True), "create_v2"
        )["smeshchenie"]
        - mayhem_iz_dannyh(b"\x00" * 8 + _д2м, "create_v2")["smeshchenie"]
        == len("Имя подлиннее".encode()) - len("Имя".encode()))
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: у create (v1) аргумента нет вовсе -- None и "
        "причина, а не «mayhem выключен»",
        mayhem_iz_dannyh(b"\x00" * 8 + dannye_sozdanija("create"), "create")
        ["is_mayhem_mode"] is None)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: обрезанные аргументы -- отказ по имени, а не "
        "чужой байт за признак",
        mayhem_iz_dannyh(b"\x00" * 12, "create_v2")["why_not"] is not None)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: на месте признака не 0 и не 1 -- разбор уехал, и "
        "это сказано, а не принято за истину",
        mayhem_iz_dannyh(b"\x00" * 8 + dannye_sozdanija("create")
                          + bytes([7]), "create_v2")["is_mayhem_mode"] is None)
    # --- СИГНАЛ ЗАПУСКА: каждое условие владельца -- своим отказом
    _сг = signal_zapuska(_тx(создание="create_v2",
                             покупка="buy_exact_quote_in_v3"),
                         podpis="ПОДПИСЬ", slot=777)
    chk("годный сигнал: создатель купил v3, не mayhem, котировка SOL -- "
        "источником идёт СОЗДАТЕЛЬ, и поля денежного пути на месте",
        _сг["ok"] and _сг["istochnik"] == "U" * 44 and _сг["mint"] == "M" * 44
        and _сг["slot_sozdanija"] == 777
        and _сг["base_token_program"] == PS.ПРОГ_ТОКЕНА_2022, _сг)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: создатель не покупал -- отказ sozdatel_ne_kupil",
        signal_zapuska(_тx(создание="create_v2"))["otkaz_vid"]
        == ОТКАЗ_НЕ_КУПИЛ)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: mayhem-режим -- отказ mayhem",
        signal_zapuska(_тx(создание="create_v2", mayhem=True,
                            покупка="buy_exact_quote_in_v3"))["otkaz_vid"]
        == ОТКАЗ_MAYHEM)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: create (v1) -- признака mayhem нет, и сигнал НЕ "
        "идёт: не знаем значит не идём",
        signal_zapuska(_тx(создание="create",
                            покупка="buy_exact_quote_in_v3"))["otkaz_vid"]
        == ОТКАЗ_MAYHEM)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: котировка кривой не SOL -- отказ "
        "kotirovka_ne_sol, с минтом котировки в причине",
        signal_zapuska(_тx(создание="create_v2",
                            покупка="buy_exact_quote_in_v3",
                            котировка="CARDSccUMFKohQ5h1Kw6bjnMuaDfKtyzNHBP9R9N1C2x"
                            ))["otkaz_vid"] == ОТКАЗ_КОТИРОВКА)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: покупка v2 -- разновидность не обкатана, сигнал "
        "не идёт непроверенным путём",
        signal_zapuska(_тx(создание="create_v2",
                            покупка="buy_exact_quote_in_v2"))["otkaz_vid"]
        == ОТКАЗ_РАЗНОВИДНОСТЬ)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: покупка чужого минта в транзакции создания за "
        "покупку создателя не считается и сигналом не становится",
        signal_zapuska(_тx(создание="create_v2",
                            покупка="buy_exact_quote_in_v3",
                            покупка_минт="N" * 44))["otkaz_vid"]
        == ОТКАЗ_НЕ_КУПИЛ)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: в транзакции нет создания -- отказ net_sozdaniya",
        signal_zapuska({"transaction": {"message": {"accountKeys": [],
                                                     "instructions": []}}}
                        )["otkaz_vid"] == ОТКАЗ_НЕТ_СОЗДАНИЯ)
    chk("ни один отказ сигнала не оставляет ok=True",
        all(not signal_zapuska(т)["ok"] for т in (
            _тx(создание="create_v2"),
            _тx(создание="create_v2", mayhem=True,
                покупка="buy_exact_quote_in_v3"),
            _тx(создание="create", покупка="buy_exact_quote_in_v3"),
            _тx(создание="create_v2", покупка="buy_exact_quote_in_v2"))))
    # РАБОЧАЯ ЧАСТЬ ФАЙЛА -- БЕЗ ТЕЛА САМОПРОВЕРКИ. Искать запретные слова во
    # всём файле нельзя: они есть в самой этой проверке, и она краснела бы
    # всегда. Ровно на таком же расколе по чужому имени функции держалась тихая
    # зелень в выгрузке сделок (правка 10.10).
    _рабочая = Path(__file__).read_text(encoding="utf-8").split(
        "def self_test")[0]
    chk("раскол исходника отделил самопроверку от рабочего кода",
        "chk(" not in _рабочая and "def signal_zapuska" in _рабочая)
    chk("в рабочей части модуля нет ни отправки, ни подписи -- сигнал только "
        "отвечает да/нет",
        not any(с in _рабочая for с in ("sendTransaction", "Keypair",
                                         "sign_message", "partial_sign")),
        [с for с in ("sendTransaction", "Keypair", "sign_message",
                      "partial_sign") if с in _рабочая])
    стр = [json.dumps({"ts_utc": "2026-10-09T18:00:00Z", "sozdanie": "create",
                       "kupil_v_sozdanii": True, "sol_sozdatelya": 0.5,
                       "otryv_slotov": 2}),
           json.dumps({"ts_utc": "2026-10-09T18:30:00Z", "sozdanie": "create_v2",
                       "kupil_v_sozdanii": False, "otryv_slotov": 4}),
           json.dumps({"ts_utc": "2026-10-09T19:30:00Z", "sozdanie": "create",
                       "kupil_v_sozdanii": True, "sol_sozdatelya": 1.5,
                       "otryv_slotov": None}),
           "не json", ""]
    с = svod(стр, s="2026-10-09T18:00:00Z", do="2026-10-09T19:00:00Z")
    chk("свод за час: созданий 2, с покупкой создателя 1, SOL 0.5, отрыв 2..4",
        с["sozdanij"] == 2 and с["s_pokupkoj_sozdatelya"] == 1
        and с["sol_sozdateley"] == 0.5
        and с["otryv_slotov"]["min"] == 2 and с["otryv_slotov"]["max"] == 4, с)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: запись без числа отрыва считается отдельно, а не "
        "как ноль",
        svod(стр)["otryv_slotov"]["bez_chisla"] == 1, svod(стр))
    стр_б = [json.dumps({"ts_utc": "2026-10-09T18:00:00Z", "sozdanie": "create",
                         "bajt": 104858}),
             json.dumps({"ts_utc": "2026-10-09T18:01:00Z", "sozdanie": "create",
                         "bajt": 1})]
    сб = svod(стр_б)
    chk("расход считается ИЗ ЗАПИСЕЙ: 104 859 байт -- это 4 кредита (две "
        "порции по 0.1 МиБ с округлением вверх), а не счётчик процесса",
        сб["bajt"] == 104859 and сб["kreditov"] == 4, сб)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: запись без байтов считается отдельно, а не как "
        "ноль расхода",
        svod([json.dumps({"ts_utc": "2026-10-09T18:00:00Z",
                          "sozdanie": "create"})])["bez_bajt"] == 1)
    chk("разновидности создания названы числами",
        svod(стр)["razvidnosti"] == {"create": 2, "create_v2": 1},
        svod(стр)["razvidnosti"])

    print(f"самопроверка журнала запусков: {всего - сбоев}/{всего} пройдено, "
          f"ждали {ЖДЁМ_ПРОВЕРОК}")
    return 1 if (сбоев or всего != ЖДЁМ_ПРОВЕРОК) else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    if "--pda" in sys.argv:
        print(pda_mint_authority())
        print(json.dumps(odin_tolko_u_sozdanij(), ensure_ascii=False))
        raise SystemExit(0)
    if "--svod" in sys.argv:
        д = [а for а in sys.argv[1:] if not а.startswith("--")]
        п = Path(д[0]) if д else put_zhurnala()
        с = д[1] if len(д) > 1 else None
        до = д[2] if len(д) > 2 else None
        with п.open(encoding="utf-8", errors="replace") as ф:
            print(json.dumps(svod(ф, s=с, do=до), ensure_ascii=False, indent=1))
        raise SystemExit(0)
    print("нужен --self-test, --pda или --svod <файл> [с] [до]", file=sys.stderr)
    raise SystemExit(2)
