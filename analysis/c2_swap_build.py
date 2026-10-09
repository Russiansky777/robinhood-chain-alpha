#!/usr/bin/env python3
"""Задача D (C2): своя сборка покупки «котировка -> токен» БЕЗ отправки в сеть.

Ключи кошелька не используются нигде: пользователь -- ключ, сгенерированный
на лету, симуляция -- simulateTransaction с sigVerify=false и
replaceRecentBlockhash=true. Метода отправки транзакции в модуле нет.

Покрытые типы пулов (задача D, шаг 1: 80.5 % сигналов):
  Pump AMM  pAMMBay6...  buy_exact_quote_in(spendable_quote_in, min_base_amount_out)
  Raydium CPMM CPMMoo8L... swap_base_input(amount_in, minimum_amount_out)
  Meteora DAMM v2 cpamdpZ... swap(amount_in, minimum_amount_out)

Откуда формат, без выдумывания:
* дискриминатор -- правило Anchor sha256("global:<имя>")[:8]; совпадение
  проверено на настоящих транзакциях источников (data/c2_pool_samples/);
* порядок счетов и роли -- из инструкции источника в его транзакции;
  пользовательские счета заменяются по ролям, которые проверены на цепи:
  у Pump AMM счёт 20 -- PDA ["user_volume_accumulator", user] (25 из 25),
  токен-счета пользователя -- ATA (owner, программа токена, минт);
* аргументы -- два u64 после дискриминатора; сверено с движением хранилищ.
Главная самопроверка: из шаблона и адреса самого источника сборщик обязан
восстановить его инструкцию ТОЧНО (счета и данные).

Минимум токенов -- по резервам пула ПОСЛЕ сделки источника
(postTokenBalances хранилищ его транзакции), формула x*y=k; доля,
доходящая до пула из нашей траты (комиссии), калибруется на сделке
самого источника: f = x0*dy/((y0-dy)*spent). Для DAMM v2 (сосредоточенная
ликвидность) резервы цены не дают -- минимум там не выдаётся.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import struct
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402

from solders.hash import Hash  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

SERVICE = "c2_build"
SAMPLES_DIR = C.DATA / "c2_pool_samples"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
DAMM2 = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
# Meteora DBC -- кривая запуска (dynamic bonding curve) ДО перехода в DAMM v2.
DBC = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
# Orca Whirlpool -- ЗАПИСИ В SPECS У НЕГО НЕТ И НЕ ДОЛЖНО БЫТЬ: счета
# делятся на A/B, а не на вход/выход, и roles_damm2 положил бы наш ATA не на
# то место (у программы стоит constraint
# token_owner_account_a.mint == whirlpool.token_mint_a). Он идёт СВОИМ
# строителем через развилку в bloom_own_send.собрать(). Адрес нужен ГЕЙТУ
# полосы: типы_полосы и имя_строителя_по_адресу читают его через
# getattr(B, ИМЯ_КОНСТАНТЫ) -- без константы тип отпадает МОЛЧА, отказом
# "тип пула вне полосы", неотличимым от "сигналов не было".
WHIRLPOOL = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
# Raydium AMM v4 -- КОНСТАНТА НУЖНА ГЕЙТУ, А ЗАПИСИ В SPECS У ТИПА НЕТ И НЕ БУДЕТ.
# Он идёт своим строителем (c2_ammv4_stroitel, Code-3): раскладка счетов у него
# своя, а гейт полосы (типы_полосы -> getattr(B, ИМЯ)) без константы не пустил бы
# тип вовсе -- отказ "тип пула вне полосы" пришёл бы ещё до реестра строителей.
# Адрес взят программно из модуля Code-3 и сверен с метками детектора.
AMMV4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
# Pump.fun bonding curve -- кривая ДО миграции в Pump AMM. Отличий от прочих
# типов три, и все три money-path: (1) котировка -- НАТИВНЫЙ SOL, токенового
# счёта котировки нет вовсе, оборачивать нечего; (2) инструкция buy -- «точный
# выход»: первый аргумент это КОЛИЧЕСТВО ТОКЕНОВ (он же наш минимум), второй --
# предел траты SOL (он же наша трата); (3) цена берётся из события сделки,
# как у Launchlab. Раскладка 18 счетов установлена по 5 настоящим покупкам
# в data/c2_pool_samples/6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P.json.
BONDING = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# РАЗНОВИДНОСТИ ИНСТРУКЦИИ КРИВОЙ. Имён у них мы не знаем (перебор
# sha256("global:<имя>") по сотням правдоподобных имён ничего не дал), и
# выдумывать их не стали: дискриминатор берётся из НАСТОЯЩЕЙ сделки источника и
# переносится в нашу сборку как есть. Значение аргументов установлено по живым
# сделкам, а не по документации:
#   66063d1201daebea -- 18 счетов, "точный выход": (сколько токенов, предел SOL);
#   38fc74089edfcd5f -- 18 счетов, ТОТ ЖЕ порядок счетов, но "точный вход":
#     (сколько лампортов тратим, минимум токенов). Проверено на трёх живых
#     сделках: первый аргумент был ровно 50 000 000 = 0.05 SOL, а событие
#     сделки дало sol_amount + комиссии = ровно этот аргумент.
# Замер 26.09 05:0xZ: из 37 свежих сигналов кривой 31 -- 38fc7408, 5 -- вариант
# с котировкой НЕ в SOL (27 счетов), 1 -- ещё один (26 счетов), buy -- НИ ОДНОГО.
#   c2ab1c46684d5b2f -- 27 счетов, "точный вход" и КОТИРОВКА ОТДЕЛЬНЫМ МИНТОМ:
#     у кривой появился токеновый счёт котировки (WSOL или иной минт), то есть
#     нативной котировки здесь нет и SOL надо оборачивать, как в обычном пуле.
#     Раскладка снята с ЖИВЫХ сделок (data/c2_curve_variant_samples.json, три
#     покупки 26.09 плюс одна с токеновой котировкой в образцах программы):
#     0 global, 1 базовый минт, 2 минт котировки, 3 программа базового токена,
#     4 программа котировки, 5 ATA-программа, 6-9 получатели комиссий и их
#     счета, 10 счёт кривой (PDA bonding-curve+минт), 11 её базовое хранилище,
#     12 её хранилище котировки, 13 НАШ кошелёк (подписант), 14 НАШ базовый ATA,
#     15 НАШ ATA котировки, 16-18 счета кривой и конфигурация, 19 global_volume_
#     accumulator, 20 НАШ user_volume_accumulator, 21-22 счета создателя,
#     23 программа комиссий, 24 системная, 25 event_authority, 26 сама
#     программа. Индексы 13/14/15/20 проверены выводом PDA и ATA из подписанта
#     сделки: совпали ровно на этих местах.
#     Хвост данных -- один байт (0x01) -- переносится как есть.
BONDING_DISCS = {
    "66063d1201daebea": {"exact_out": True},
    "38fc74089edfcd5f": {"exact_out": False},
    "c2ab1c46684d5b2f": {
        "exact_out": False,
        "spec": {"n_accounts": 27, "user": [13],
                  "user_ata": [(14, 1, 3), (15, 2, 4)],
                  "pda": [(20, [b"user_volume_accumulator", "USER"])],
                  # МЕСТО 21 -- СВЯЗАННЫЙ НАКОПИТЕЛЬ, И ОН ВЫВОДИТСЯ. Здесь
                  # стояло "ключ не выводится семенами, берётся из состояния", и
                  # это было неправдой: он ATA накопителя места 20 (а тот уже
                  # выводится PDA выше) для минта котировки и программы токена
                  # котировки. Сверено с ЦЕПЬЮ на нашей же живой покупке
                  # Nhe6axYT1eHBUBdAmQGg… (27 счетов, слот 452705255): место 20
                  # и место 21 совпали байт в байт, и ATA берёт программу токена
                  # МЕСТА 4, а не места 3 (место 3 -- программа базы, у пампового
                  # минта это Token-2022, и с ней выходил другой адрес).
                  #
                  # ЗАЧЕМ ВЫВОД, А НЕ ПАМЯТЬ. Файл bonding_v2_assoc.json
                  # пополняется ИЗ ОШИБКИ ПРОГРАММЫ, то есть пара учится только
                  # когда покупка один раз упала на цепи, и помнится ПО
                  # КОШЕЛЬКУ. Полоса переехала на 4dPZMbRe -- запомненного для
                  # него нет ничего, и 03.10 это дало 16 отказов "роли счетов не
                  # восстановились" у источника группы cand1_03 с билетом 0.5.
                  # (место, место_минта_котировки, место_программы_котировки)
                  "assoc_uva": (21, 2, 4),
                  "base_mint": 1, "quote_mint": 2, "base_vault": 11,
                  "quote_vault": 12, "native_quote": False},
    },
}
# Пулы, где направление задаётся тем, в какое хранилище пришла котировка:
# индексы входа/выхода пользователя, хранилищ a/b, их минтов и программ токена.
DYN = {
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": {"in": 2, "out": 3, "va": 4, "vb": 5, "ma": 6,
                                                    "mb": 7, "pa": 9, "pb": 10},
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": {"in": 4, "out": 5, "va": 2, "vb": 3, "ma": 6,
                                                    "mb": 7, "pa": 11, "pb": 12},
    # Raydium CLMM swap_v2: 3/4 -- счета пользователя вход/выход, 5/6 --
    # хранилища вход/выход, 11/12 -- их минты; программа токена у swap_v2
    # общая парой (8 -- SPL, 9 -- Token-2022), своя у каждого минта берётся
    # из балансов транзакции (programId).
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": {"in": 3, "out": 4, "va": 5, "vb": 6, "ma": 11,
                                                     "mb": 12, "pa": None, "pb": None},
}
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# TOKEN-2022 -- ОТДЕЛЬНАЯ ПРОГРАММА, И ОНА МЕНЯЕТ АДРЕС ATA. Нужна здесь имени
# ради: у связанного накопителя кривой v2 котировка бывает минтом Token-2022
# (живая пара хоста XsCPL9dN…), и ATA считается ЕЮ, а не классической.
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM = "11111111111111111111111111111111"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + b


def b58encode(b: bytes) -> str:
    """Адрес из 32 байт. Своим кодером: сторонней base58 в окружении службы нет.

    Нужен там, где адрес приходит СЫРЫМИ БАЙТАМИ -- например, пул внутри
    события свопа CLMM, которое лежит в логах.
    """
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    ведущие = 0
    for x in b:
        if x != 0:
            break
        ведущие += 1
    return "1" * ведущие + s


def disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


# Роли по программам. user -- подписант-владелец; user_ata -- (индекс счёта,
# индекс счёта минта, индекс счёта программы токена); pda -- счета,
# выводимые из пользователя.
SPECS = {
    # СЧЕТОВ У PUMP AMM БЫВАЕТ 25 ИЛИ 26. Замер 28.09 по пулу USDC/SOL
    # (nML7msD1MiJHxFvhv4po1u6C4KpWr64ugKqc75DMuD2): девять покупок той же
    # инструкцией buy_exact_quote_in идут с 25 счетами, и раскладка совпадает с
    # 26-счётной ПОЗИЦИЯ В ПОЗИЦИЮ на индексах 0..24 -- у длинной просто есть
    # 26-й счёт в хвосте. Все роли, которые мы подставляем (подписант 1, наши
    # ATA 5 и 6, PDA 20, минты 3 и 4, хранилища 7 и 8), лежат в этих 25, а
    # хвост переносится из настоящей сделки как есть. Пока предел стоял ровно
    # 26, шаблон первой ноги SOL -> USDC не извлекался вовсе.
    PUMP_AMM: {"label": "Pump AMM", "ix": "buy_exact_quote_in", "alt": ["buy"],
               "n_accounts": None, "min_accounts": 25, "max_accounts": 26,
               "user": [1], "user_ata": [(5, 3, 11), (6, 4, 12)],
               "pda": [(20, [b"user_volume_accumulator", "USER"])],
               "base_mint": 3, "quote_mint": 4, "base_vault": 7, "quote_vault": 8},
    CPMM: {"label": "Raydium CPMM", "ix": "swap_base_input", "alt": [], "n_accounts": 13,
           "user": [0], "user_ata": [(4, 10, 8), (5, 11, 9)], "pda": [],
           "base_mint": 11, "quote_mint": 10, "base_vault": 7, "quote_vault": 6},
    LAUNCHLAB: {"label": "Raydium Launchlab", "ix": "buy_exact_in", "alt": [], "n_accounts": 18,
                "user": [0], "user_ata": [(5, 9, 11), (6, 10, 12)], "pda": [], "tail": True,
                "base_mint": 9, "quote_mint": 10, "base_vault": 7, "quote_vault": 8},
    DLMM: {"label": "Meteora DLMM", "ix": "swap2", "alt": ["swap"], "n_accounts": None, "min_accounts": 16,
           "user": [10], "user_ata": "dyn", "pda": [], "tail": True,
           "base_mint": None, "quote_mint": None, "base_vault": None, "quote_vault": None},
    CLMM: {"label": "Raydium CLMM", "ix": "swap_v2", "alt": [], "n_accounts": None, "min_accounts": 13,
           "user": [0], "user_ata": "dyn", "pda": [], "tail": True,
           "base_mint": None, "quote_mint": None, "base_vault": None, "quote_vault": None},
    # Счета buy у кривой (проверено на 5 настоящих покупках, вывод счётов сошёлся
    # у всех): 0 global, 1 получатель комиссии, 2 минт, 3 счёт кривой
    # (PDA bonding-curve+минт, он же держит SOL), 4 ATA кривой, 5 НАШ ATA токена,
    # 6 НАШ кошелёк (подписант), 7 системная, 8 программа токена (бывает и
    # Token-2022), 9 хранилище создателя, 10 event_authority, 11 сама программа,
    # 12 global_volume_accumulator, 13 НАШ user_volume_accumulator (PDA от нас),
    # 14 fee_config, 15 программа комиссий, 16-17 счета создателя. Подставляем
    # только 5, 6 и 13 -- остальное переносится из сделки источника как есть.
    BONDING: {"label": "Pump.fun bonding curve", "ix": "buy", "alt": [], "n_accounts": 18,
              "user": [6], "user_ata": [(5, 2, 8)],
              "pda": [(13, [b"user_volume_accumulator", "USER"])],
              "base_mint": 2, "quote_mint": None, "base_vault": 4, "quote_vault": 3,
              "native_quote": True, "exact_out": True},
    # У DAMM v2 в живых сделках ДВЕ инструкции: swap и swap2 (у второй ещё один
    # байт хвоста). Счетов бывает 14 или 15: пятнадцатый -- Sysvar instructions
    # в САМОМ КОНЦЕ, и счета 0..13 при этом совпадают дословно (проверено на
    # образцах 1, 5, 14). Поэтому раскладка одна, а число счетов -- 14 или 15;
    # шестнадцатого в живых сделках не встречалось, и вслепую его не берём.
    # METEORA DBC. Раскладка 15 счетов установлена по ДВУМ живым сделкам и по
    # внутренним переводам этих же транзакций (видно, какой счёт платил
    # котировкой и какой получил базу):
    #   0 распорядитель хранилищ, 1 конфигурация, 2 сам пул (его адрес лежит и в
    #   событии), 3 НАШ счёт котировки, 4 НАШ счёт базы, 5 базовое хранилище
    #   пула, 6 хранилище котировки, 7 базовый минт, 8 минт котировки, 9 НАШ
    #   кошелёк (подписант), 10 программа базового токена, 11 программа
    #   котировки, 12 место реферала (там стоит сама программа), 13
    #   event_authority, 14 сама программа.
    # Подставляем только 3, 4 и 9 -- остальное переносится из сделки источника.
    DBC: {"label": "Meteora DBC", "ix": "swap", "alt": [], "n_accounts": 15,
          "user": [9], "user_ata": [(4, 7, 10), (3, 8, 11)], "pda": [],
          "base_mint": 7, "quote_mint": 8, "base_vault": 5, "quote_vault": 6},
    DAMM2: {"label": "Meteora DAMM v2", "ix": "swap", "alt": ["swap2"],
            "n_accounts": None, "min_accounts": 14, "max_accounts": 15,
            "user": [8], "user_ata": "dyn", "pda": [],
            "base_mint": None, "quote_mint": None, "base_vault": None, "quote_vault": None},
}


# РАСКЛАДКА ПРОДАЖИ. Пока здесь только Pump AMM: у него продажа -- ДРУГАЯ
# инструкция (sell, дискриминатор 33e685a4017f83ad), а не та же с обратными
# счетами. Раскладка выведена по НАШЕЙ СОБСТВЕННОЙ продаже 22:21:42Z
# (teg1M4QY..., 24 счёта, продажа 6XnC2Y -> SOL через Jupiter, внутри которой
# лежит настоящая инструкция пула):
#   0 пул, 1 наш кошелёк (подписант), 2 общая настройка, 3 минт токена,
#   4 минт котировки (WSOL), 5 НАШ счёт токена, 6 НАШ счёт WSOL,
#   7 хранилище токена пула, 8 хранилище котировки пула, 9 получатель комиссии,
#   10 его счёт WSOL, 11 программа токена базы, 12 программа токена котировки,
#   13 системная, 14 программа ATA, 15 event_authority, 16 сама программа,
#   17..23 счета создателя и комиссий -- переносятся как есть.
# ОТ ПОКУПКИ ОТЛИЧАЕТСЯ отсутствием user_volume_accumulator (он только у
# покупки) -- поэтому счетов 24, а не 26, и pda здесь пустой.
SPECS_ПРОДАЖИ = {
    PUMP_AMM: {"label": "Pump AMM продажа", "ix": "sell", "alt": [],
               "n_accounts": None, "min_accounts": 23, "max_accounts": 24,
               "user": [1], "user_ata": [(5, 3, 11), (6, 4, 12)], "pda": [],
               "base_mint": 3, "quote_mint": 4, "base_vault": 7, "quote_vault": 8},
}


# ---------------------------------------------------------------- КРИВАЯ ПО IDL
#
# ЗАЧЕМ (слово штаба 09.10). 08.10 полоса встала: источники ушли на
# buy_exact_quote_in_v3 (дискриминатор e1f7501ed5b38488, СЕМНАДЦАТЬ счетов), а
# BONDING_DISCS такого ключа не знает -- отказ "разновидность инструкции кривой
# не известна". Старый путь переносит наши счета ПО НОМЕРАМ МЕСТ сделки
# источника, и для новой раскладки это не работает в принципе: мест другое число
# и стоят они иначе.
#
# ЗДЕСЬ ДОБАВЛЕН ВТОРОЙ ПУТЬ: раскладка берётся ПО ИМЕНАМ из закреплённого IDL
# (c3_pump_sborka), а не по номерам. Старый путь не тронут -- он по-прежнему
# первый, и отказ на неизвестном ключе остался: он теперь ловит то, чего нет НИ
# в таблице, НИ в IDL.
#
# ЧТО ПРИХОДИТ ИЗ СДЕЛКИ ИСТОЧНИКА И НЕ ВЫВОДИТСЯ НИКАК: buyback_fee_recipient.
# В IDL его нет ни константой, ни PDA, а адрес наугад послал бы комиссию
# чужому -- поэтому он берётся из счетов источника по ИМЕНИ из IDL.
КРИВАЯ_ПО_IDL_ПОКУПКА = ("buy_exact_quote_in_v3", "buy_v3",
                         "buy_exact_quote_in_v2", "buy_v2")
КРИВАЯ_ПО_IDL_ПРОДАЖА = ("sell_v3", "sell_v2")
# «Точный выход» -- это buy_v*: первым аргументом идёт КОЛИЧЕСТВО ТОКЕНА, а
# вторым предел траты. У buy_exact_quote_in_* наоборот: первым трата.
КРИВАЯ_IDL_ТОЧНЫЙ_ВЫХОД = {"buy_v3": True, "buy_v2": True,
                           "buy_exact_quote_in_v3": False,
                           "buy_exact_quote_in_v2": False,
                           "sell_v3": False, "sell_v2": False}


def _сборщик_idl():
    """c3_pump_sborka или None. Нет его -- старый путь работает как работал."""
    try:
        import c3_pump_sborka as PS  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    return PS


def вариант_кривой_по_idl(disc_hex: str, *, продажа: bool = False):
    """Имя разновидности кривой по дискриминатору -- из IDL, не из памяти."""
    PS = _сборщик_idl()
    if PS is None:
        return None
    try:
        имя = PS.variant_po_disku("pump", bytes.fromhex(disc_hex))
    except Exception:  # noqa: BLE001
        return None
    годные = КРИВАЯ_ПО_IDL_ПРОДАЖА if продажа else КРИВАЯ_ПО_IDL_ПОКУПКА
    return имя if имя in годные else None


def спец_из_idl(вариант: str) -> dict:
    """Позиционная раскладка ИЗ ИМЁН IDL -- только для mints_and_vaults.

    Сама инструкция собирается по именам (см. swap_instruction), а эта раскладка
    нужна лишь затем, чтобы build_buy узнал минты, программы токенов и
    хранилища кривой и создал ATA -- тем же кодом, что и для прочих типов пулов.
    """
    PS = _сборщик_idl()
    если_нет = {}
    if PS is None:
        return если_нет
    try:
        имена = [а["name"] for а in PS.zagruzit_idl()["pump"]["ix"][вариант]["accounts"]]
    except Exception:  # noqa: BLE001
        return если_нет
    и = {имя: н for н, имя in enumerate(имена)}
    нужно = ("base_mint", "quote_mint", "base_token_program",
             "quote_token_program", "user", "associated_base_user",
             "associated_quote_user", "associated_base_bonding_curve",
             "associated_quote_bonding_curve")
    if any(к not in и for к in нужно):
        return если_нет
    return {"n_accounts": len(имена), "user": [и["user"]],
            "user_ata": [(и["associated_base_user"], и["base_mint"],
                          и["base_token_program"]),
                         (и["associated_quote_user"], и["quote_mint"],
                          и["quote_token_program"])],
            "base_mint": и["base_mint"], "quote_mint": и["quote_mint"],
            "base_vault": и["associated_base_bonding_curve"],
            "quote_vault": и["associated_quote_bonding_curve"],
            "native_quote": False, "pda": [], "assoc_uva": None}


def _по_именам_idl(вариант: str, accounts: list) -> dict:
    """{имя IDL: адрес} для счетов сделки источника."""
    PS = _сборщик_idl()
    if PS is None:
        return {}
    try:
        имена = [а["name"] for а in PS.zagruzit_idl()["pump"]["ix"][вариант]["accounts"]]
    except Exception:  # noqa: BLE001
        return {}
    return dict(zip(имена, accounts, strict=False))


def spec_of(tpl: dict) -> dict:
    """Раскладка счетов для ЭТОГО шаблона: у кривой она зависит от разновидности.

    Разновидность c2ab1c46684d5b2f кладёт наши счета на другие места и имеет
    отдельный минт котировки; спутать её с 18-счётной значило бы подставить наш
    кошелёк не туда, то есть подписать покупку с чужими счетами.
    """
    if tpl.get("продажа"):
        return dict(SPECS_ПРОДАЖИ[tpl["program"]])
    s = dict(SPECS[tpl["program"]])
    if tpl.get("program") == BONDING:
        # ШАБЛОН ПО IDL -- СВОЯ РАСКЛАДКА ПО ИМЕНАМ. Её ждёт только
        # mints_and_vaults: сама инструкция собирается в swap_instruction
        # сборщиком по именам, а не подстановкой мест.
        if tpl.get("spec_idl"):
            s.update(tpl["spec_idl"])
            return s
        вар = BONDING_DISCS.get(tpl.get("ix")) or {}
        if вар.get("spec"):
            s.update(вар["spec"])
    return s


def all_instructions(tx: dict) -> list:
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    out = list(msg.get("instructions") or [])
    for g in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        out += list(g.get("instructions") or [])
    return [ix for ix in out if isinstance(ix, dict) and isinstance(ix.get("accounts"), list)]


def writable_map(tx: dict) -> dict:
    raw = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return {k["pubkey"]: bool(k.get("writable")) for k in raw if isinstance(k, dict)}


def ata(owner: str, mint: str, token_program: str) -> str:
    return str(Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)),
         bytes(Pubkey.from_string(mint))], Pubkey.from_string(ATA_PROGRAM))[0])


def pda(seeds: list, user: str, program: str) -> str:
    raw = [bytes(Pubkey.from_string(user)) if s == "USER" else s for s in seeds]
    return str(Pubkey.find_program_address(raw, Pubkey.from_string(program))[0])


def extract_template(tx: dict, program: str, pool_vault: str, *,
                     продажа: bool = False) -> dict:
    """Инструкция пула в транзакции: счета, данные, аргументы.

    продажа=True берёт раскладку ПРОДАЖИ (SPECS_ПРОДАЖИ): у Pump AMM это другая
    инструкция, и путать её с покупкой нельзя -- аргументы стоят на тех же
    местах, но смысл обратный (сколько токена отдаём и сколько котировки хотим
    минимум).
    """
    spec = (SPECS_ПРОДАЖИ if продажа else SPECS).get(program)
    if spec is None:
        return {"ok": False,
                "why_not": ("продажа этого типа пула не покрыта" if продажа
                             else "тип пула не покрыт")}
    want = disc(spec["ix"])
    for ix in all_instructions(tx):
        if ix.get("programId") != program or pool_vault not in ix["accounts"]:
            continue
        data = b58decode(ix["data"])
        # КРИВАЯ: разновидность узнаём по дискриминатору настоящей сделки, а не
        # по имени. Неизвестный дискриминатор -- отказ с ним же в причине.
        if program == BONDING:
            ключ = data[:8].hex()
            вид = BONDING_DISCS.get(ключ)
            if вид is None:
                # ВТОРОЙ ПУТЬ: РАСКЛАДКА ПО ИМЕНАМ IDL. Старый отказ остаётся
                # ниже и ловит то, чего нет НИ в таблице, НИ в IDL.
                вар_idl = вариант_кривой_по_idl(ключ, продажа=bool(продажа))
                сп_idl = спец_из_idl(вар_idl) if вар_idl else {}
                if вар_idl and сп_idl:
                    ждём_idl = сп_idl["n_accounts"]
                    if len(ix["accounts"]) != ждём_idl:
                        return {"ok": False,
                                "why_not": (f"{вар_idl}: счетов "
                                            f"{len(ix['accounts'])}, по IDL "
                                            f"{ждём_idl}")}
                    по_именам = _по_именам_idl(вар_idl, list(ix["accounts"]))
                    выкуп = по_именам.get("buyback_fee_recipient")
                    if not выкуп:
                        # НЕ ВЫВОДИТСЯ НИКАК: ни константой, ни PDA. Адрес
                        # наугад послал бы комиссию чужому.
                        return {"ok": False,
                                "why_not": (f"{вар_idl}: в сделке источника нет "
                                            "buyback_fee_recipient")}
                    a0_i, a1_i = struct.unpack("<QQ", data[8:24])
                    return {"ok": True, "program": program, "ix": ключ,
                            "po_idl": вар_idl, "spec_idl": сп_idl,
                            "buyback_fee_recipient": выкуп,
                            "accounts": list(ix["accounts"]), "data": data,
                            "arg0": a0_i, "arg1": a1_i,
                            "writable": writable_map(tx),
                            "signers": sorted(C.signers(tx)),
                            "продажа": bool(продажа),
                            "exact_out": КРИВАЯ_IDL_ТОЧНЫЙ_ВЫХОД[вар_idl]}
                return {"ok": False, "why_not": f"разновидность инструкции кривой не известна: {ключ}"}
            # СКОЛЬКО СЧЕТОВ -- ПО РАЗНОВИДНОСТИ: у 18-счётной и 27-счётной они
            # разные, и общее число из SPECS годится только для первой.
            ждём_счетов = (вид.get("spec") or {}).get("n_accounts", spec["n_accounts"])
            if ждём_счетов is not None and len(ix["accounts"]) != ждём_счетов:
                return {"ok": False,
                        "why_not": f"счетов {len(ix['accounts'])}, ожидалось {ждём_счетов}"}
            a0_, a1_ = struct.unpack("<QQ", data[8:24])
            return {"ok": True, "program": program, "ix": ключ, "accounts": list(ix["accounts"]),
                    "data": data, "arg0": a0_, "arg1": a1_, "writable": writable_map(tx),
                    "signers": sorted(C.signers(tx)), "exact_out": bool(вид["exact_out"])}
        кандидаты = [spec["ix"], *list(spec.get("alt") or []),
                     "swap2", "swap_base_output", "swap"]
        name = next((n for n in кандидаты if data[:8] == disc(n)), data[:8].hex())
        if data[:8] != want and name not in spec["alt"]:
            return {"ok": False, "why_not": f"у источника инструкция {name}, не {spec['ix']}"}
        ждём = spec["n_accounts"]
        макс = spec.get("max_accounts")
        if ждём is not None and len(ix["accounts"]) != ждём or \
                len(ix["accounts"]) < (spec.get("min_accounts") or 0) or \
                (макс is not None and len(ix["accounts"]) > макс):
            return {"ok": False,
                    "why_not": f"счетов {len(ix['accounts'])}, ожидалось "
                                f"{ждём if ждём is not None else (spec.get('min_accounts'), макс)}"}
        a0, a1 = struct.unpack("<QQ", data[8:24])
        # DLMM swap (v1): счета 0..12 те же, что у swap2 (проверено на
        # настоящих транзакциях), данные -- дискриминатор + 2 u64 без хвоста.
        if program == CLMM and name != spec["ix"] or program == DLMM and name not in ("swap2", "swap"):
            return {"ok": False, "why_not": f"у источника инструкция {name}, не {spec['ix']}"}
        # CLMM: хвост -- sqrt_price_limit_x64 (u128) + is_base_input (bool).
        # Хвост переносится в нашу сборку как есть, поэтому берём только
        # «точный вход без ценового лимита».
        if program == CLMM and data[24:] != bytes(16) + b"\x01":
            return {"ok": False, "why_not": "у источника CLMM не «точный вход без лимита цены»"}
        return {"ok": True, "program": program, "ix": name, "accounts": list(ix["accounts"]),
                "data": data, "arg0": a0, "arg1": a1, "writable": writable_map(tx),
                "signers": sorted(C.signers(tx)), "продажа": bool(продажа)}
    return {"ok": False, "why_not": "инструкция пула с этим хранилищем не найдена"}


def roles_damm2(tpl: dict, tx: dict) -> dict:
    """DAMM v2 / DLMM: вход -- то хранилище, куда в сделке пришла котировка."""
    acc = tpl["accounts"]
    k = DYN[tpl["program"]]
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    va, vb = rows.get(acc[k["va"]]), rows.get(acc[k["vb"]])
    if not va or not vb:
        return {}
    a_in = (acc[k["ma"]] == tpl["force_in"]) if tpl.get("force_in") else (va["post"] - va["pre"]) > 0
    in_mint, out_mint = (acc[k["ma"]], acc[k["mb"]]) if a_in else (acc[k["mb"]], acc[k["ma"]])
    if k["pa"] is None:
        progs = {b["mint"]: b.get("programId") for side in ("preTokenBalances", "postTokenBalances")
                 for b in ((tx.get("meta") or {}).get(side) or [])}
        pa, pb = progs.get(acc[k["ma"]]), progs.get(acc[k["mb"]])
        if not pa or not pb:
            return {}
    else:
        pa, pb = acc[k["pa"]], acc[k["pb"]]
    in_prog, out_prog = (pa, pb) if a_in else (pb, pa)
    return {"in": (k["in"], in_mint, in_prog), "out": (k["out"], out_mint, out_prog),
            "quote_vault": acc[k["va"]] if a_in else acc[k["vb"]],
            "base_vault": acc[k["vb"]] if a_in else acc[k["va"]]}


def flip_template(tpl: dict, in_mint: str) -> dict:
    """Шаблон ценозависимого пула из сделки обратного направления: вход --
    in_mint. DLMM: хранилища и минты в инструкции по порядку пула, достаточно
    указать вход. CLMM swap_v2: хранилища/минты по направлению -- меняются
    местами 5<->6 и 11<->12. Массивы бинов/тиков остаются от чужой сделки
    (начинаются с текущего) -- годность проверяет симуляция."""
    t = dict(tpl, force_in=in_mint, flipped=True)
    if tpl["program"] == CLMM:
        a = list(tpl["accounts"])
        a[5], a[6], a[11], a[12] = a[6], a[5], a[12], a[11]
        t["accounts"] = a
    return t


# ------------------------------------- СВЯЗАННЫЙ НАКОПИТЕЛЬ 27-счётной кривой
#
# 28.09 семь наших копий источника 6qudAN2k сели с ошибкой: программа кривой
# сказала прямо --
#   AnchorError caused by account: associated_user_volume_accumulator.
#   Error Code: ConstraintSeeds. Error Number: 2006.
# Место 20 (наш user_volume_accumulator) мы подставляли верно, а место 21 --
# СВЯЗАННЫЙ накопитель -- переносили из сделки источника, то есть отдавали
# программе ЧУЖОЙ счёт.
#
# ОТКУДА БЕРЁМ ПРАВИЛЬНЫЙ КЛЮЧ, не выдумывая семена. IDL программы на цепи не
# выложен (счёт IDL пуст), а перебор строковых семян по трём фактам цепи
# (кошелёк+котировка -> ключ) не дал ни одного совпадения. Зато сама программа
# печатает ожидаемый адрес в ошибке, и по фактам он зависит ТОЛЬКО от
# пользователя и минта котировки: у нас один и тот же ключ на семи разных
# базовых минтах, а у источника ключ меняется вместе с котировкой (WSOL против
# XsCPL9dN...). Поэтому ключи живут в состоянии службы, а не в коде: файл
# заполняется тем, что назвала цепь.
#
# ЕСЛИ КЛЮЧА НЕТ -- сборка ОТКАЗЫВАЕТ. Отправить чужой накопитель значит
# заранее сжечь комиссию: именно так ушло 0.007035 SOL за 28.09.
ASSOC_UVA_ПО_УМОЛЧАНИЮ = "/home/bot/bloom_executor_live_data/bonding_v2_assoc.json"
_ASSOC_UVA_КЭШ: dict = {}


def assoc_uva_файл() -> str:
    return os.environ.get("BLOOM_ASSOC_UVA_FILE") or ASSOC_UVA_ПО_УМОЛЧАНИЮ


def assoc_uva_забыть() -> None:
    """Сбросить память процесса о файле (нужно самопроверке и после правки)."""
    _ASSOC_UVA_КЭШ.clear()


def assoc_uva_все() -> dict:
    путь = assoc_uva_файл()
    if путь in _ASSOC_UVA_КЭШ:
        return _ASSOC_UVA_КЭШ[путь]
    try:
        with open(путь, encoding="utf-8") as ф:
            зн = json.load(ф)
        если = зн if isinstance(зн, dict) else {}
    except Exception:  # noqa: BLE001
        если = {}
    _ASSOC_UVA_КЭШ[путь] = если
    return если


def assoc_uva(пользователь: str, минт_котировки: str) -> str | None:
    """Ключ связанного накопителя для этой пары -- только из состояния."""
    по_кошельку = (assoc_uva_все().get(пользователь) or {})
    зн = по_кошельку.get(минт_котировки)
    return зн if isinstance(зн, str) and зн else None


def assoc_uva_запомнить(пользователь: str, минт_котировки: str, ключ: str,
                         *, путь: str | None = None) -> dict:
    """Записать ключ, названный цепью. Пишем атомарно и без потери чужих пар."""
    путь = путь or assoc_uva_файл()
    все = {}
    try:
        with open(путь, encoding="utf-8") as ф:
            зн = json.load(ф)
        все = зн if isinstance(зн, dict) else {}
    except Exception:  # noqa: BLE001
        все = {}
    все.setdefault(пользователь, {})[минт_котировки] = ключ
    врем = f"{путь}.tmp"
    with open(врем, "w", encoding="utf-8") as ф:
        json.dump(все, ф, ensure_ascii=False, indent=1)
    os.replace(врем, путь)
    _ASSOC_UVA_КЭШ[путь] = все
    return все


def assoc_uva_из_логов(логи) -> str | None:
    """Ожидаемый адрес связанного накопителя из ошибки программы.

    Anchor печатает: строку с именем счёта и Error Code: ConstraintSeeds,
    затем "Left:" (что дали) и "Right:" (что ждали). Берём Right и только
    для нашего счёта -- чужие ошибки сюда не относятся.
    """
    строки = [str(л) for л in (логи or [])]
    for i, л in enumerate(строки):
        if ("associated_user_volume_accumulator" not in л
                or "ConstraintSeeds" not in л):
            continue
        for j in range(i, min(i + 8, len(строки))):
            if строки[j].strip().endswith("Right:") and j + 1 < len(строки):
                хвост = строки[j + 1].split()
                return хвост[-1] if хвост else None
    return None


def user_accounts(tpl: dict, tx: dict, user: str) -> dict:
    """{индекс: адрес} для пользовательских счетов шаблона при данном user."""
    spec = spec_of(tpl)
    acc = tpl["accounts"]
    out = {i: user for i in spec["user"]}
    if spec["user_ata"] == "dyn":
        r = roles_damm2(tpl, tx)
        if not r:
            return {}
        for i, mint, prog in (r["in"], r["out"]):
            out[i] = ata(user, mint, prog)
    else:
        for i, mi, pi in spec["user_ata"]:
            out[i] = ata(user, acc[mi], acc[pi])
    for i, seeds in spec["pda"]:
        out[i] = pda(seeds, user, tpl["program"])
    ас = spec.get("assoc_uva")
    if ас:
        место, место_котировки = ас[0], ас[1]
        место_программы = ас[2] if len(ас) > 2 else None
        # ПЕРЕСБОРКА СДЕЛКИ САМОГО ИСТОЧНИКА: на месте 21 уже стоит ЕГО
        # накопитель, и подставлять туда ничего не надо -- иначе самопроверка
        # "восстанови инструкцию источника точно" стала бы ложной.
        свой = acc[spec["user"][0]] == user if spec.get("user") else False
        if свой:
            out[место] = acc[место]
            return out
        # СВЯЗАННЫЙ НАКОПИТЕЛЬ ВЫВОДИТСЯ, а не вспоминается. Он ATA накопителя
        # объёма (место уже выведено PDA в цикле выше) для минта котировки и
        # программы токена котировки. Сверено с цепью байт в байт.
        выведен = None
        if место_программы is not None:
            нак = out.get(_место_накопителя(spec))
            if нак:
                выведен = ata(нак, acc[место_котировки], acc[место_программы])
        помним = assoc_uva(user, acc[место_котировки])
        # ЗАПОМНЕННОЕ -- ТОЛЬКО СВЕРКА (слово владельца 03.10). Совпало -- идём
        # выведенным; РАСХОЖДЕНИЕ -- отказ, а не тихий выбор одного из двух:
        # разойтись они могут лишь если неверна формула или испорчен файл, и оба
        # случая кончаются подписью не того счёта.
        if выведен and помним and выведен != помним:
            return {}
        ключ = выведен or помним
        if not ключ:
            # ЧЕСТНЫЙ ОТКАЗ. Чужой накопитель программа не примет, и покупка
            # сгорит на комиссии. Пусть лучше сделка не соберётся.
            return {}
        out[место] = ключ
    return out


СЕМЯ_НАКОПИТЕЛЯ = b"user_volume_accumulator"


def _место_накопителя(spec: dict) -> int | None:
    """Место накопителя объёма в раскладке -- ПО СЕМЕНИ, а не числом 20.

    Число здесь разошлось бы со спецификацией молча, а связанный накопитель
    выводится ИЗ НЕГО: ошибка в месте дала бы подпись не того счёта.
    """
    for i, семена in (spec.get("pda") or []):
        if семена and семена[0] == СЕМЯ_НАКОПИТЕЛЯ:
            return i
    return None


def assoc_uva_вывод(пользователь: str, spec: dict, acc: list,
                     программа: str) -> dict:
    """Связанный накопитель ВЫВОДОМ, рядом с запомненным -- для сверки.

    Отдельной функцией, чтобы сверку можно было прогнать снаружи, не собирая
    покупку: именно её владелец и просил прогнать read-only по всем парам,
    которые в файле УЖЕ лежат, прежде чем пускать вывод в службу.
    """
    из_ = {"ok": False, "выведен": None, "помним": None, "сошлось": None,
            "why_not": None, "минт_котировки": None}
    ас = (spec or {}).get("assoc_uva")
    if not ас or len(ас) < 3:
        из_["why_not"] = "у раскладки нет места программы котировки"
        return из_
    нак_место = _место_накопителя(spec)
    if нак_место is None:
        из_["why_not"] = f"в раскладке нет PDA с семенем {СЕМЯ_НАКОПИТЕЛЯ!r}"
        return из_
    try:
        # ПРОГРАММА -- АРГУМЕНТОМ, а не acc[-1]: последнее место раскладки это
        # сама программа лишь у этой разновидности, и опереться на порядок
        # счетов значило бы вывести PDA в чужой программе.
        нак = pda([СЕМЯ_НАКОПИТЕЛЯ, "USER"], пользователь, программа)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"накопитель не вывелся: {type(exc).__name__}"
        return из_
    из_["минт_котировки"] = acc[ас[1]]
    из_["выведен"] = ata(нак, acc[ас[1]], acc[ас[2]])
    из_["помним"] = assoc_uva(пользователь, acc[ас[1]])
    # СОШЛОСЬ: True -- совпало с запомненным, False -- разошлось, None --
    # запомненного нет вовсе (это НЕ расхождение: у нового кошелька файл пуст).
    из_["сошлось"] = (None if not из_["помним"]
                       else из_["выведен"] == из_["помним"])
    из_["ok"] = из_["сошлось"] in (None, True)
    if из_["сошлось"] is False:
        из_["why_not"] = ("выведенный и запомненный накопители РАСХОДЯТСЯ -- "
                           "подписывать нельзя")
    return из_


def mints_and_vaults(tpl: dict, tx: dict) -> dict:
    spec = spec_of(tpl)
    acc = tpl["accounts"]
    if spec["user_ata"] == "dyn":
        r = roles_damm2(tpl, tx)
        if not r:
            return {}
        return {"quote_mint": r["in"][1], "base_mint": r["out"][1], "quote_program": r["in"][2],
                "base_program": r["out"][2], "quote_vault": r["quote_vault"],
                "base_vault": r["base_vault"]}
    if spec.get("native_quote"):
        # Котировка -- нативный SOL: минта и программы токена у неё нет,
        # «хранилище котировки» это сам счёт кривой (лампорты лежат на нём).
        ba_ = next(x for x in spec["user_ata"] if x[1] == spec["base_mint"])
        return {"quote_mint": C.NATIVE_QUOTE, "base_mint": acc[spec["base_mint"]],
                "quote_program": None, "base_program": acc[ba_[2]],
                "quote_vault": acc[spec["quote_vault"]], "base_vault": acc[spec["base_vault"]]}
    qa = next(x for x in spec["user_ata"] if x[1] == spec["quote_mint"])
    ba = next(x for x in spec["user_ata"] if x[1] == spec["base_mint"])
    return {"quote_mint": acc[spec["quote_mint"]], "base_mint": acc[spec["base_mint"]],
            "quote_program": acc[qa[2]], "base_program": acc[ba[2]],
            "quote_vault": acc[spec["quote_vault"]], "base_vault": acc[spec["base_vault"]]}


def инструкция_кривой_по_idl(tpl: dict, tx: dict, user: str,
                             arg0: int, arg1: int) -> Instruction:
    """НАША инструкция кривой, собранная ПО ИМЕНАМ IDL, а не подстановкой мест.

    Все счета выводит сборщик (константы IDL, PDA по семенам IDL, ATA), наше --
    кошелёк. Из сделки источника берётся ровно одно значение, которое не
    выводится никак: buyback_fee_recipient. Программа токена БАЗОВОГО минта
    берётся из той же сделки (у пампового минта это обычно Token-2022) и
    входит СЕМЕНЕМ в associated_base_*: подставить её классическим Token значит
    собрать чужие адреса, и программа отвечает 3012 AccountNotInitialized.
    """
    PS = _сборщик_idl()
    if PS is None:
        raise ValueError("сборщик по IDL не доступен: c3_pump_sborka не найден")
    вар = tpl["po_idl"]
    по_именам = _по_именам_idl(вар, tpl["accounts"])
    выкуп = tpl.get("buyback_fee_recipient") or по_именам.get("buyback_fee_recipient")
    if not выкуп:
        raise ValueError("нет buyback_fee_recipient: он не выводится ни "
                         "константой, ни PDA")
    # КОТИРОВКА КРИВОЙ ОБЯЗАНА БЫТЬ WSOL, И ОТКАЗ ЗДЕСЬ ДЕШЕВЛЕ ОТКАЗА
    # ПРОГРАММЫ. Измерено 09.10 на отказанных сигналах vol_4vw: у трёх кривых
    # котировка не SOL (CARDSccUMFKo..., SPCXxcqXj6e5...), и программа отвечала
    # 6004 MintDoesNotMatchBondingCurve -- тем же номером, что при отсутствии
    # счёта кривой. Собрать такую покупку мы не можем по сути, а не по
    # раскладке: полоса платит СОЛАМИ, а этой кривой нужен её токен. Прежде
    # такая сборка уходила в сеть и стоила чаевых и приоритета на каждом
    # сигнале; теперь отказ идёт ДО подписи и называет минт котировки.
    кот_источника = по_именам.get("quote_mint")
    if кот_источника and кот_источника != C.WSOL:
        raise ValueError(
            f"котировка кривой {кот_источника[:12]} не WSOL -- полоса платит "
            f"солами, купить на этой кривой нечем (программа ответила бы 6004)")
    тп_базы = PS.programma_tokena_minta(
        по_именам.get("base_mint") or "",
        iz_signala=по_именам.get("base_token_program"))
    общее = {"base_mint": по_именам.get("base_mint"), "user": user,
             "buyback_fee_recipient": выкуп, "base_token_program": тп_базы}
    if вар == "buy_exact_quote_in_v3":
        собрано = PS.pokupka_krivoj_v3(spendable_quote_in=int(arg0),
                                       min_tokens_out=int(arg1), **общее)
    elif вар == "sell_v3":
        собрано = PS.prodazha_krivoj_v3(amount=int(arg0),
                                        min_sol_output=int(arg1), **общее)
    elif вар in ("buy_v3", "buy_exact_quote_in_v2", "buy_v2", "sell_v2"):
        # ОСТАЛЬНЫЕ -- ТЕМ ЖЕ sobrat, ПО ИМЕНАМ АРГУМЕНТОВ ИЗ IDL. Своей
        # обёртки у них нет, и выдумывать имена аргументов нельзя: они берутся
        # из того же IDL, по порядку, и это ровно два u64 у всех шести.
        арг_имена = [а["name"] for а in
                     PS.zagruzit_idl()["pump"]["ix"][вар]["args"]]
        арг = {арг_имена[0]: int(arg0), арг_имена[1]: int(arg1)}
        if len(арг_имена) > 2:
            арг[арг_имена[2]] = False   # partial_fill: как у нашей v3-обёртки
        кон = dict(общее)
        кон["quote_mint"] = по_именам.get("quote_mint") or C.WSOL
        кон["quote_token_program"] = по_именам.get("quote_token_program") or TOKEN_PROGRAM
        # У v2 нужны ещё fee_recipient и создатель кривой: оба есть в сделке
        # источника (создатель -- семенем creator_vault), и оба берутся оттуда.
        for имя in ("fee_recipient", "sharing_config"):
            if по_именам.get(имя):
                кон[имя] = по_именам[имя]
        созд = PS.creator_iz_sobytija(tx) if hasattr(PS, "creator_iz_sobytija") else None
        if созд:
            кон["bonding_curve.creator"] = созд
        собрано = PS.sobrat("pump", вар, кон, арг)
    else:
        raise ValueError(f"разновидность {вар} по IDL не собираем")
    return Instruction(
        Pubkey.from_string(собрано["programma"]),
        bytes.fromhex(собрано["data_hex"]),
        [AccountMeta(Pubkey.from_string(а["pubkey"]),
                     bool(а["isSigner"]), bool(а["isWritable"]))
         for а in собрано["accounts"]])


def swap_instruction(tpl: dict, tx: dict, user: str, arg0: int, arg1: int,
                     keep_source_ix: bool = False) -> Instruction:
    # ШАБЛОН ПО IDL -- СБОРКА ПО ИМЕНАМ. Подстановка мест для него не годится:
    # у v3 семнадцать счетов и другие места, и именно на этом встала полоса.
    if tpl.get("po_idl") and not keep_source_ix:
        return инструкция_кривой_по_idl(tpl, tx, user, arg0, arg1)
    subs = user_accounts(tpl, tx, user)
    if not subs:
        # ПРИЧИНУ НАДО НАЗВАТЬ. Этот отказ выходит наружу строкой записи решения,
        # и 03.10 он дал 16 отказов у источника группы cand1_03 в виде голого
        # "сборка: ValueError: роли счетов не восстановились" -- по такой строке
        # нельзя отличить испорченную память накопителя от чего угодно другого.
        _сп = spec_of(tpl)
        _поч = None
        if _сп.get("assoc_uva"):
            _св = assoc_uva_вывод(user, _сп, tpl["accounts"], tpl["program"])
            _поч = _св.get("why_not")
        raise ValueError("роли счетов не восстановились"
                         + (f": {_поч}" if _поч else ""))
    metas = []
    for i, a in enumerate(tpl["accounts"]):
        addr = subs.get(i, a)
        is_user = i in subs
        metas.append(AccountMeta(Pubkey.from_string(addr),
                                 is_signer=(addr == user),
                                 is_writable=is_user or tpl["writable"].get(a, False)))
    if keep_source_ix:   # только для самопроверки: инструкция источника как есть
        data = tpl["data"][:8] + struct.pack("<QQ", arg0, arg1) + tpl["data"][24:]
    else:                # наша сборка: основная инструкция типа, два u64 (+ хвост источника,
                         # у Launchlab это share_fee_rate u64)
        if tpl["program"] == BONDING:
            # Дискриминатор -- ИЗ СДЕЛКИ ИСТОЧНИКА: имени разновидности мы не
            # знаем, а угадывать его на деньгах нельзя. Хвост (у части сделок
            # один лишний байт) переносится как есть.
            return Instruction(Pubkey.from_string(tpl["program"]),
                               tpl["data"][:8] + struct.pack("<QQ", arg0, arg1) + tpl["data"][24:],
                               metas)
        # ИМЯ ИНСТРУКЦИИ -- КАК У ИСТОЧНИКА там, где у программы их несколько
        # (DLMM: swap/swap2; DAMM v2: swap/swap2). Собрать swap со счетами
        # swap2 значило бы отдать программе не тот список счетов.
        ix_name = (tpl["ix"] if tpl["program"] in (DLMM, DAMM2)
                   else SPECS[tpl["program"]]["ix"])
        data = disc(ix_name) + struct.pack("<QQ", arg0, arg1)
        # ХВОСТ -- ТОЛЬКО ТОТ, ЧТО БЫЛ У ИСТОЧНИКА, и только когда мы собираем
        # ту же инструкцию: у swap2 это один байт, у CLMM -- лимит цены с
        # признаком, у Launchlab -- доля комиссии.
        if tpl["data"][24:] and (spec_of(tpl).get("tail") or ix_name == tpl["ix"]):
            data += tpl["data"][24:]
    return Instruction(Pubkey.from_string(tpl["program"]), data, metas)


def ata_idempotent(payer: str, owner: str, mint: str, token_program: str) -> Instruction:
    return Instruction(Pubkey.from_string(ATA_PROGRAM), bytes([1]), [
        AccountMeta(Pubkey.from_string(payer), True, True),
        AccountMeta(Pubkey.from_string(ata(owner, mint, token_program)), False, True),
        AccountMeta(Pubkey.from_string(owner), False, False),
        AccountMeta(Pubkey.from_string(mint), False, False),
        AccountMeta(Pubkey.from_string(SYSTEM), False, False),
        AccountMeta(Pubkey.from_string(token_program), False, False)])


def cu_limit(units: int) -> Instruction:
    return Instruction(Pubkey.from_string(COMPUTE_BUDGET), bytes([2]) + struct.pack("<I", units), [])


def cu_price(micro_lamports: int) -> Instruction:
    return Instruction(Pubkey.from_string(COMPUTE_BUDGET), bytes([3]) + struct.pack("<Q", micro_lamports), [])


def sol_transfer(src: str, dst: str, lamports: int) -> Instruction:
    return Instruction(Pubkey.from_string(SYSTEM), struct.pack("<IQ", 2, lamports), [
        AccountMeta(Pubkey.from_string(src), True, True),
        AccountMeta(Pubkey.from_string(dst), False, True)])


# СЛУЖЕБНЫЕ АДРЕСА, нужные долговечному nonce. RecentBlockhashes -- зашитый
# системный аккаунт (SysvarRecentB1ockHashes11111111111111111111), он обязателен
# в инструкции AdvanceNonceAccount по описанию системной программы Solana.
SYSVAR_RECENT_BLOCKHASHES = "SysvarRecentB1ockHashes11111111111111111111"


def advance_nonce(nonce_account: str, authority: str) -> Instruction:
    """AdvanceNonceAccount: код 4 системной программы, три аккаунта по порядку.

    ЗАЧЕМ ОНА. Долговечный nonce даёт то, чего не даёт обычный blockhash: ДВЕ
    РАЗНЫЕ транзакции с одним и тем же nonce не могут исполниться обе -- первая
    его сдвигает, вторая становится недействительной. Это и есть замок пула по
    варианту с отдельной транзакцией на каждого отправителя: у каждого свои
    чаевые (значит пакет меньше и предел 1232 байта не жмёт), а купить можно
    только один раз.

    Инструкция обязана быть ПЕРВОЙ в транзакции, а recent_blockhash сообщения --
    это хеш, сохранённый в самом аккаунте nonce. Оба условия -- требования
    системной программы, а не наш выбор.
    """
    return Instruction(Pubkey.from_string(SYSTEM), struct.pack("<I", 4), [
        AccountMeta(Pubkey.from_string(nonce_account), False, True),
        AccountMeta(Pubkey.from_string(SYSVAR_RECENT_BLOCKHASHES), False, False),
        AccountMeta(Pubkey.from_string(authority), True, False)])


def sync_native(account: str) -> Instruction:
    return Instruction(Pubkey.from_string(TOKEN_PROGRAM), bytes([17]),
                       [AccountMeta(Pubkey.from_string(account), False, True)])


def close_account(account: str, destination: str, authority: str,
                  token_program: str = TOKEN_PROGRAM) -> Instruction:
    """CloseAccount (9) ПРОГРАММОЙ ТОКЕНА СЧЁТА: остаток и рента -- в destination.

    ПРОГРАММА -- АРГУМЕНТОМ, А НЕ КОНСТАНТОЙ. Счёт токена принадлежит той
    программе, которой принадлежит минт; у пампового минта это обычно
    Token-2022. Закрыть счёт Token-2022 классической программой нельзя: она не
    владелец счёта и ответит ошибкой. Умолчание -- классический Token, потому
    что единственный счёт, который закрывает покупка, это WSOL, а WSOL
    классический; у минта сделки программа приходит из раскладки.

    ЗАЧЕМ В ПОКУПКЕ. Обёртка SOL создаёт счёт WSOL и кладёт на него ровно
    amount_in. Если своп эти лампорты НЕ забрал (а у пула с нативной котировкой
    он их и не трогает), они остаются лежать на счёте WSOL: в балансе кошелька
    их уже нет, а в сделке они не участвовали. Измерено 01.10 на покупке полосы
    tvwftKB3 (15:58:39Z, кривая pump.fun, сделка 0.1): нативная дельта
    -0.20500728 при дельте WSOL +0.1 -- ровно размер сделки встал в обёртке и
    простоял 31 минуту, пока его не вернуло закрытие счёта ВНУТРИ ЧУЖОЙ продажи
    (DBT 16:29:23Z), где эти 0.1 и были записаны как выручка той сделки.

    Поэтому закрытие стоит в той же транзакции, что обёртка: не забрал своп --
    деньги вернулись тем же действием, которым ушли.
    """
    return Instruction(Pubkey.from_string(token_program), bytes([9]), [
        AccountMeta(Pubkey.from_string(account), False, True),
        AccountMeta(Pubkey.from_string(destination), False, True),
        AccountMeta(Pubkey.from_string(authority), True, False)])


def build_buy(tpl: dict, tx: dict, *, user: str, payer: str, amount_in: int, min_out: int,
              cu_units: int = 200_000, cu_price_micro: int = 0, tip: tuple | None = None,
              wrap_sol: bool = True, nonce: tuple | None = None,
              tip_first: bool = False, close_wsol: bool = True) -> dict:
    """Инструкции покупки: compute budget, ATA (идемпотентно), обёртка SOL
    (если котировка WSOL и wrap_sol), своп, tip отдельным параметром
    (адрес, лампорты) -- адрес tip не зашит.

    nonce=(аккаунт, распорядитель) добавляет AdvanceNonceAccount ПЕРВОЙ
    инструкцией: так делается вариант пула, где на каждого отправителя своя
    транзакция со своими чаевыми, а исполниться может только одна.
    """
    mv = mints_and_vaults(tpl, tx)
    if not mv:
        raise ValueError("минты/хранилища не восстановились")
    ixs = []
    if nonce:
        # ПЕРВОЙ -- и никак иначе: системная программа принимает
        # AdvanceNonceAccount только как первую инструкцию транзакции.
        ixs.append(advance_nonce(str(nonce[0]), str(nonce[1])))
    ixs.append(cu_limit(cu_units))
    if cu_price_micro:
        ixs.append(cu_price(cu_price_micro))
    ixs.append(ata_idempotent(payer, user, mv["base_mint"], mv["base_program"]))
    if mv["quote_program"]:   # нативная котировка (кривая pump.fun): счёта нет
        ixs.append(ata_idempotent(payer, user, mv["quote_mint"], mv["quote_program"]))
    обёрнут_wsol = None
    if mv["quote_mint"] == C.WSOL and wrap_sol and amount_in:
        wsol_ata = ata(user, C.WSOL, mv["quote_program"])
        ixs += [sol_transfer(user, wsol_ata, amount_in), sync_native(wsol_ata)]
        обёрнут_wsol = wsol_ata
    # ЧАЕВЫЕ ПЕРЕД СВОПОМ -- по требованию отправителя. 0slot в письме 25.09:
    # "инструкцию чаевых ставить в начало транзакции" (он ищет её там). Ставим
    # сразу после бюджета вычислений и nonce: раньше нельзя -- nonce обязан быть
    # первой инструкцией по правилу системной программы.
    def _чаевые_инструкции(tip_):
        пары_ = (tip_ if isinstance(tip_, (list, tuple)) and tip_
                 and isinstance(tip_[0], (list, tuple)) else [tip_])
        return [sol_transfer(payer, а_, int(л_)) for а_, л_ in пары_]

    if tip and tip_first:
        ixs += _чаевые_инструкции(tip)
    точный_выход = (tpl.get("exact_out") if tpl.get("exact_out") is not None
                    else spec_of(tpl).get("exact_out"))
    if точный_выход:
        # «Точный выход»: программа сама считает цену наших min_out токенов и
        # отказывается, если она выше amount_in. То есть оба денежных предела --
        # минимум токенов и максимум траты -- стоят в одной инструкции.
        ixs.append(swap_instruction(tpl, tx, user, min_out, amount_in))
    else:
        ixs.append(swap_instruction(tpl, tx, user, amount_in, min_out))
    if tip and tip_first:
        tip = None
    if tip:
        # ЧАЕВЫХ МОЖЕТ БЫТЬ НЕСКОЛЬКО. Боевой пул отправителей (слово
        # владельца 25.09) посылает ОДНУ подписанную покупку сразу всеми
        # сервисами, а каждый сервис проверяет СВОИ чаевые и ниже своего
        # минимума молча отбрасывает транзакцию. Поэтому tip -- это либо пара
        # (адрес, лампорты), либо список таких пар; проверка суммы и списка
        # получателей -- у вызывающего (bloom_own_send), здесь только сборка.
        пары = (tip if isinstance(tip, (list, tuple))
                and tip and isinstance(tip[0], (list, tuple)) else [tip])
        for адрес_ч, лам_ч in пары:
            ixs.append(sol_transfer(payer, адрес_ч, int(лам_ч)))
    # РАСПАКОВКА В ТОЙ ЖЕ ТРАНЗАКЦИИ (слово владельца 01.10, вечер). Закрытие
    # идёт ПОСЛЕ свопа: своп берёт из счёта WSOL столько, сколько ему нужно, а
    # всё, что осталось, вместе с рентой счёта возвращается в кошелёк тем же
    # действием. Не обёртывали -- закрывать нечего, и инструкции нет вовсе.
    if обёрнут_wsol and close_wsol:
        # ПРОГРАММА -- ИЗ РАСКЛАДКИ (место quote_token_program), а не константой.
        ixs.append(close_account(обёрнут_wsol, user, user,
                                 mv["quote_program"] or TOKEN_PROGRAM))
    msg = MessageV0.try_compile(Pubkey.from_string(payer), ixs, [], Hash.default())
    n_sig = msg.header.num_required_signatures
    vtx = VersionedTransaction.populate(msg, [Signature.default()] * n_sig)
    raw = bytes(vtx)
    return {"tx_base64": base64.b64encode(raw).decode(), "size": len(raw),
            "n_instructions": len(ixs), "quote_mint": mv["quote_mint"],
            "base_mint": mv["base_mint"],
            # ОБЁРТКА И ЕЁ ЗАКРЫТИЕ -- В ОТЧЁТ СБОРКИ, чтобы по записи позиции
            # было видно: обёртывали ли и вернули ли то, что своп не забрал.
            "wrapped_wsol": обёрнут_wsol,
            "closed_wsol": bool(обёрнут_wsol and close_wsol)}


# ------------------------------------------------------------ минимум по резервам

def damm2_min_out(tpl: dict, tx: dict, amount_in: int, slippage: float) -> dict:
    """Минимум выхода в DAMM v2: кривая восстанавливается по сделке источника.

    Резервы в хранилищах у сосредоточенной ликвидности цену не дают, поэтому
    цена берётся из события свопа (next_sqrt_price -- число из цепи) и пары
    "вход/выход" этой же сделки. Проверки события -- в c2_cl_quote: заявленный
    вход обязан совпасть с аргументом инструкции, выход -- с движением
    хранилища, а решённая кривая -- воспроизвести выход источника.
    """
    mv = mints_and_vaults(tpl, tx)
    if not mv:
        return {"ok": False, "why_not": "минты и хранилища не восстановились"}
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    qv, bv = rows.get(mv["quote_vault"]), rows.get(mv["base_vault"])
    if not qv or not bv:
        return {"ok": False, "why_not": "хранилищ пула нет в балансах транзакции"}
    вход = qv["post"] - qv["pre"]
    выход = bv["pre"] - bv["post"]
    if вход <= 0 or выход <= 0:
        return {"ok": False,
                "why_not": "сделка источника не покупка по этим хранилищам"}
    try:
        import c2_cl_quote as CL  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"нет модуля цены ({type(exc).__name__})"}
    # ПУЛ -- счёт 1 инструкции DAMM v2 (проверено на живых сделках): по нему
    # событие привязывается к НАШЕМУ пулу, а не к первому в маршруте.
    пул = tpl["accounts"][1] if len(tpl["accounts"]) > 1 else None
    тело = CL.тело_события(tx, tpl["program"], all_instructions, b58decode,
                            пул=пул)
    база_это_a = mv["base_mint"] == tpl["accounts"][DYN[tpl["program"]]["ma"]]
    из_ = CL.минимум(тело=тело, вход_источника=вход, выход_источника=выход,
                      аргумент_входа=tpl["arg0"], база_это_a=база_это_a,
                      наш_вход=amount_in, проскальзывание=slippage)
    if из_.get("ok"):
        из_["source_in"] = вход
        из_["source_out"] = выход
    return из_


def dbc_min_out(tpl: dict, tx: dict, amount_in: int, slippage: float) -> dict:
    """Минимум выхода в Meteora DBC -- та же математика cpAMM, своя схема события.

    Кривая запуска считается по цене после сделки источника (u128 из события) и
    по паре "вход/выход" этой же сделки. Проверки те же: заявленный вход
    совпадает с аргументом инструкции, выход -- с движением хранилища, сдвиг
    цены физически возможен, модель воспроизводит сделку источника.

    ОТДЕЛЬНОЕ ПРЕДУПРЕЖДЕНИЕ. Схема события снята с ДВУХ живых сделок; у кривой
    запуска бывают участки ликвидности, и на большом входе экстраполяция по
    одному участку может врать. Поэтому флаг LANE_DBC включается только после
    симуляции на хосте, и размер сделки там 0.01 SOL.
    """
    mv = mints_and_vaults(tpl, tx)
    if not mv:
        return {"ok": False, "why_not": "минты и хранилища не восстановились"}
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    qv, bv = rows.get(mv["quote_vault"]), rows.get(mv["base_vault"])
    if not qv or not bv:
        return {"ok": False, "why_not": "хранилищ пула нет в балансах транзакции"}
    вход = qv["post"] - qv["pre"]
    выход = bv["pre"] - bv["post"]
    if вход <= 0 or выход <= 0:
        return {"ok": False,
                "why_not": "сделка источника не покупка по этим хранилищам"}
    try:
        import c2_cl_quote as CL  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"нет модуля цены ({type(exc).__name__})"}
    схема = CL.СХЕМЫ["DBC"]
    # Пул у DBC -- счёт 2 инструкции (его адрес лежит и в теле события).
    пул = tpl["accounts"][2] if len(tpl["accounts"]) > 2 else None
    тело = CL.тело_события(tx, tpl["program"], all_instructions, b58decode,
                            пул=пул, длина=схема["длина"])
    из_ = CL.минимум(тело=тело, вход_источника=вход, выход_источника=выход,
                      аргумент_входа=tpl["arg0"], база_это_a=True,
                      наш_вход=amount_in, проскальзывание=slippage, схема=схема)
    if из_.get("ok"):
        из_["source_in"] = вход
        из_["source_out"] = выход
    return из_


# ------------------------------------------- ставки комиссии пулов Raydium CLMM
# ЗАЧЕМ ЗДЕСЬ СЛОВАРЬ. Ставка комиссии пула CLMM лежит в счёте amm_config, а не
# в сделке: прочитать её -- сетевой запрос, а на горячем пути (решение ->
# отправка) сетевых запросов нет и быть не может. Поэтому ставки живут в
# памяти: словарь наполняет ФОНОВОЕ чтение (детектор), а горячий путь только
# смотрит в него. Нет ставки -- честный отказ, и адрес конфига кладётся в
# НУЖНЫ_КОНФИГИ_CLMM, чтобы фон прочитал его к следующему сигналу.
#
# ПОЧЕМУ МОЖНО КЭШИРОВАТЬ НАВСЕГДА. amm_config -- счёт НАСТРОЕК уровня комиссии
# (их у Raydium единицы на всю программу), а не состояние пула: ставка и шаг
# тика в нём не меняются от сделок. Обновление раз в час фоном -- с запасом.
СТАВКИ_CLMM: dict = {}
НУЖНЫ_КОНФИГИ_CLMM: set = set()
# Конфиги, про которые узел ОТВЕТИЛ, а ставка не разобралась (счёта нет, длина
# не та). Второй раз их не спрашиваем: это поломка кода или адреса, а не
# сетевая помеха, и кредит на неё каждые двадцать секунд тратить незачем.
КОНФИГИ_БЕЗ_СТАВКИ: set = set()
ФАЙЛ_КОНФИГОВ_CLMM = "clmm_konfigi.json"


def загрузить_ставки_clmm(файл=None) -> int:
    """Ставки из файла прогона чтения счетов amm_config. Вернёт сколько взято."""
    п = Path(файл) if файл else (C.DATA / ФАЙЛ_КОНФИГОВ_CLMM)
    if not п.exists():
        return 0
    try:
        д = json.loads(п.read_text(encoding="utf-8")).get("конфиги") or {}
    except Exception:  # noqa: BLE001
        return 0
    взято = 0
    for адрес, з in д.items():
        р = (з or {}).get("разбор") or {}
        if р.get("ставка_1e6") and р.get("шаг_тика"):
            СТАВКИ_CLMM[адрес] = {"ставка_1e6": int(р["ставка_1e6"]),
                                  "шаг_тика": int(р["шаг_тика"])}
            взято += 1
    return взято


def clmm_min_out(tpl: dict, tx: dict, amount_in: int, slippage: float,
                  ставка_1e6: int | None = None,
                  шаг_тика: int | None = None) -> dict:
    """Минимум выхода в Raydium CLMM: L и цена -- из события свопа в ЛОГАХ.

    Отличие от DAMM v2: событие CLMM отдаёт ликвидность L прямо, поэтому кривая
    не решается из пары "вход/выход", а ПРОВЕРЯЕТСЯ -- сделка источника обязана
    воспроизвестись при ставке комиссии пула. Не воспроизвелась -- сделка
    источника перешла границу диапазона ликвидности, и наша L неизвестна.

    ставка_1e6 -- ставка комиссии пула в миллионных долях, из счёта amm_config
    (он стоит в самой инструкции свопа под индексом 1). Её НЕЛЬЗЯ решить по
    одной сделке: у сделки, перешедшей границу диапазона, решённая доля уходит
    на проценты в сторону (измерено: у одной живой сделки конфига с 0.25 %
    решается 10 %). Поэтому без ставки -- отказ, а не догадка.
    """
    mv = mints_and_vaults(tpl, tx)
    if not mv:
        return {"ok": False, "why_not": "минты и хранилища не восстановились"}
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    qv, bv = rows.get(mv["quote_vault"]), rows.get(mv["base_vault"])
    if not qv or not bv:
        return {"ok": False, "why_not": "хранилищ пула нет в балансах транзакции"}
    вход = qv["post"] - qv["pre"]
    выход = bv["pre"] - bv["post"]
    if вход <= 0 or выход <= 0:
        return {"ok": False,
                "why_not": "сделка источника не покупка по этим хранилищам"}
    try:
        import c2_cl_quote as CL  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"нет модуля цены ({type(exc).__name__})"}
    пул = (tpl["accounts"][CL.СМ_ПУЛ_В_ИНСТРУКЦИИ_CLMM]
           if len(tpl["accounts"]) > CL.СМ_ПУЛ_В_ИНСТРУКЦИИ_CLMM else None)
    if not пул:
        return {"ok": False, "why_not": "в инструкции CLMM нет счёта пула"}
    тела = CL.тела_событий_clmm(tx)
    тело = b""
    for т_ in тела:
        if b58encode(CL.событие_clmm(т_)["пул"]) == пул:
            тело = т_
            break
    конфиг = tpl["accounts"][1] if len(tpl["accounts"]) > 1 else None
    if ставка_1e6 is None and конфиг:
        из_кэша = СТАВКИ_CLMM.get(конфиг)
        if из_кэша:
            ставка_1e6 = из_кэша["ставка_1e6"]
            if шаг_тика is None:
                шаг_тика = из_кэша["шаг_тика"]
        else:
            # Фону -- задание на следующий сигнал; этот отказывается честно.
            if конфиг not in КОНФИГИ_БЕЗ_СТАВКИ:
                НУЖНЫ_КОНФИГИ_CLMM.add(конфиг)
            return {"ok": False,
                    "why_not": f"ставка комиссии пула CLMM неизвестна: конфиг "
                               f"{конфиг} ещё не прочитан",
                    "amm_config": конфиг}
    из_ = CL.минимум_clmm(тело=тело, вход_источника=вход, выход_источника=выход,
                           аргумент_входа=tpl.get("arg0") or 0,
                           наш_вход=amount_in, проскальзывание=slippage,
                           ставка_1e6=ставка_1e6, шаг_тика=шаг_тика)
    if из_.get("ok"):
        из_["source_in"] = вход
        из_["source_out"] = выход
        из_["pool"] = пул
        # Счёт amm_config -- чтобы вызывающий знал, чью ставку кэшировать.
        из_["amm_config"] = конфиг
    return из_




def шаблон_с_нынешними_массивами(tpl: dict, *, массивы_шаблона: list,
                                  массивы_сейчас: list,
                                  дописывать: bool = False) -> dict:
    """Шаблон DLMM, у которого массивы корзин заменены на НЫНЕШНИЕ.

    ЗАЧЕМ. Сборка своей покупки берёт список счетов из сделки источника. У DLMM
    в хвосте этого списка стоят массивы корзин, а цена у DLMM живёт в корзинах:
    к нашему времени активная корзина уже другая. 29.09 замер по 10 живым
    покупкам показал это числом -- у 6 из 10 в шаблоне НЕ ОСТАЛОСЬ ни одного
    нужного сейчас адреса, и симуляция падала 0 из 3. Отсюда же ответ на
    предложение брать массивы источника с запасом +-1 сосед: накрывает 3 из 10
    (сдвиги -8..+3), то есть фиксированный запас не годится вовсе.

    ПОЧЕМУ ЗАМЕНА ПО ИМЕНИ, А НЕ ПО МЕСТУ. Границу "фиксированные счета кончились,
    дальше массивы" по числу счетов не угадать: у swap и swap2 она разная, и
    ошибка на единицу подставит массив на место служебного счёта. Поэтому
    вызывающий передаёт САМИ адреса массивов шаблона (их он знает точно: адрес
    массива -- PDA от пула и индекса), и заменяются ровно те места, где они
    стоят, в том же порядке.

    ПОРЯДОК массивы_сейчас -- ТОТ, В КОТОРОМ ИХ ОТДАЁТ КОТИРОВКА, а не по
    возрастанию индекса. Котировка (нужные_массивы -> quote.rs
    get_bin_array_pubkeys_for_swap) перечисляет их ПО ХОДУ СВОПА: первым идёт
    массив активной корзины, дальше те, куда своп пойдёт. Это важно именно при
    обрезке: мест у источника бывает меньше, чем нужно сейчас (29.09 -- у 7 из
    10 живых покупок), и обрезать надо С КОНЦА, оставив активный массив.
    Сортировка по индексу оставила бы САМЫЙ ДАЛЬНИЙ -- то есть отдала бы
    программе массив, в котором свопу делать нечего.

    ЧЕГО ФУНКЦИЯ НЕ ДЕЛАЕТ. Она не добавляет счетов: сколько массивов было у
    источника, столько и останется. Если нынешних нужно больше, лишние
    названы в "не_влезло" -- и это ОТКАЗ для денежного пути, а не повод
    отправить покупку без нужного массива.
    """
    из_ = {"ok": False, "why_not": None, "шаблон": None,
            "массивов_шаблона": len(массивы_шаблона or []),
            "массивов_сейчас": len(массивы_сейчас or []),
            "подставлено": 0, "не_влезло": [], "места": [],
            "дописано": 0, "хвост": None}
    if (tpl or {}).get("program") != DLMM:
        из_["why_not"] = "подстановка массивов корзин бывает только у DLMM"
        return из_
    шаб = [а for а in (массивы_шаблона or []) if а]
    сейч = [а for а in (массивы_сейчас or []) if а]
    if not шаб:
        из_["why_not"] = "массивы корзин шаблона не названы"
        return из_
    if not сейч:
        из_["why_not"] = "нынешних массивов корзин не дали"
        return из_
    набор = set(шаб)
    места = [и for и, а in enumerate(tpl.get("accounts") or []) if а in набор]
    if len(места) != len(набор):
        из_["why_not"] = (f"массивов шаблона {len(набор)}, а мест в списке счетов "
                           f"{len(места)} -- список массивов не от этой сделки")
        return из_
    места.sort()
    сколько = min(len(места), len(сейч))
    новые = list(tpl.get("accounts") or [])
    записываемые = dict(tpl.get("writable") or {})
    for к in range(сколько):
        старый = новые[места[к]]
        новый = сейч[к]
        новые[места[к]] = новый
        # Массив корзин ЗАПИСЫВАЕМЫЙ: своп меняет корзины. Флаг берётся у того
        # массива, чьё место занимаем, а по умолчанию True -- иначе программа
        # получит массив только на чтение и откажет.
        записываемые[новый] = bool(записываемые.get(старый, True))
    лишние = list(сейч[сколько:])
    # ДОПИСАТЬ ХВОСТ, А НЕ ОТКАЗАТЬ. У DLMM массивы корзин -- это ХВОСТ списка
    # счетов инструкции (remaining accounts), и программа берёт их столько,
    # сколько дано. Значит, когда мест у источника меньше, чем нужно сейчас,
    # недостающие можно ДОПИСАТЬ в конец -- но только если места массивов и
    # правда образуют непрерывный хвост, кончающийся последним счётом. Если
    # между ними или после них стоит служебный счёт, дописывание сдвинуло бы
    # разбор аргументов у программы, и это была бы потеря денег, а не промах.
    хвост = bool(места) and места[-1] == len(новые) - 1 \
        and места == list(range(места[0], места[-1] + 1))
    из_["хвост"] = хвост
    if лишние and дописывать and хвост:
        for адрес in лишние:
            новые.append(адрес)
            записываемые[адрес] = True
        из_["дописано"] = len(лишние)
        лишние = []
    из_.update(ok=True, подставлено=сколько, места=места,
                не_влезло=лишние,
                шаблон=dict(tpl, accounts=новые, writable=записываемые))
    return из_


ОКНО_ИНДЕКСОВ_МАССИВОВ = 64


def массивы_корзин_шаблона(tpl: dict, пул: str, *, индексы_сейчас: list,
                            окно: int = ОКНО_ИНДЕКСОВ_МАССИВОВ) -> list:
    """Адреса массивов корзин, которые СТОЯТ В ШАБЛОНЕ (то есть были у источника).

    ПОЧЕМУ ПОИСКОМ, А НЕ ПО МЕСТУ. Границу "фиксированные счета кончились,
    дальше массивы" по числу счетов не угадать: у swap и swap2 она разная
    (см. шаблон_с_нынешними_массивами). Зато адрес массива -- PDA от пула и
    индекса, поэтому индексы источника восстанавливаются ТОЧНО: перебираем
    окно индексов вокруг нынешних и смотрим, какие из полученных адресов есть
    в списке счетов инструкции. Ровно так это считает проверка без денег
    (deploy/checks/dlmm_vosstanovlenie.py), и числа обеих сторон обязаны
    совпадать -- поэтому счёт один и живёт здесь.

    Возвращает адреса ПО ВОЗРАСТАНИЮ ИНДЕКСА. Порядок здесь не важен:
    шаблон_с_нынешними_массивами ищет их МЕСТА в списке счетов и сортирует
    места сама.
    """
    из_: list = []
    if (tpl or {}).get("program") != DLMM or not пул:
        return из_
    нужные = [int(и) for и in (индексы_сейчас or [])]
    if not нужные:
        return из_
    try:
        import podbivka_dlmm_quote as Q  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return из_
    счета = set(tpl.get("accounts") or [])
    if not счета:
        return из_
    for и in range(min(нужные) - int(окно), max(нужные) + int(окно) + 1):
        try:
            а = Q.адрес_массива(пул, и)
        except Exception:  # noqa: BLE001
            continue
        if а in счета:
            из_.append(а)
    return из_


# ---------------------------------------------------------- Meteora DLMM
# ЦЕНА У DLMM ЖИВЁТ В КОРЗИНАХ, а не в хранилищах: у каждой корзины своя цена, и
# по остаткам хранилищ активную корзину не увидеть. Найдено 28.09 по девяти
# живым сделкам (docs/dlmm_sobytie_i_cena.md, разбор analysis/c2_dlmm_recon.py):
#   * события DLMM НЕТ в "Program data:" вовсе -- оно уходит по CPI, внутренней
#     инструкцией к самой программе DLMM с дискриминатором Anchor
#     e445a52e51cb9a1d, дальше дискриминатор события и тело;
#   * раскладка тела подтверждена фактом: lb_pair совпал со счётом 0 вызова
#     свопа 12 из 12 шагов, amount_in и amount_out -- с переводами того же
#     вызова 12 из 12;
#   * цена корзины в СЫРЫХ единицах равна (1 + bin_step/10000)^bin: проверено
#     двумя независимыми способами (отношением цен двух сделок одного пула и
#     шагом корзины из счёта пула).
DLMM_CPI_ДИСК = bytes.fromhex("e445a52e51cb9a1d")
DLMM_СВОП_ДИСК = bytes.fromhex("516ce3becdd00ac4")
DLMM_ДЛИНА_СВОПА = 145
# Шаг корзины лежит в счёте пула (смещения 73 и 80, согласны у 11 пулов из 11),
# и его читает отдельный прогон -- в горячем пути счетов не спрашиваем. Пул без
# прочитанного шага -- ОТКАЗ, как у CLMM отказ без ставки комиссии.
СТУПЕНИ_DLMM: dict = {}
НУЖНЫ_ПУЛЫ_DLMM: set = set()
ПУЛЫ_БЕЗ_СТУПЕНИ: set = set()
ФАЙЛ_СТУПЕНЕЙ_DLMM = "dlmm_shag_korziny.json"
# Запас на одну корзину с каждой стороны при проверке модели: цена сделки
# считается по целым лампортам, а границы -- точные степени.
ЗАПАС_КОРЗИН_DLMM = 1


def загрузить_ступени_dlmm(файл=None) -> int:
    """Шаги корзин из файла прогона вывода. Вернёт, сколько пулов взято."""
    п = Path(файл) if файл else (C.DATA / ФАЙЛ_СТУПЕНЕЙ_DLMM)
    if not п.exists():
        return 0
    try:
        д = json.loads(п.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return 0
    по_пулам = д.get("шаг_корзины_по_пулам") or {}
    if not по_пулам:
        # Старый вид отчёта: согласие смещений там ещё не сведено в одно поле.
        годные = д.get("годные_смещения") or []
        if годные and all(г.get("шаги_корзин") == годные[0].get("шаги_корзин")
                          for г in годные):
            по_пулам = годные[0].get("шаги_корзин") or {}
    взято = 0
    for пул, шк in по_пулам.items():
        try:
            шк = int(шк)
        except (TypeError, ValueError):
            continue
        if 1 <= шк <= 2000:
            СТУПЕНИ_DLMM[пул] = шк
            взято += 1
    return взято


# СМЕЩЕНИЯ ШАГА КОРЗИНЫ В СЧЁТЕ ПУЛА. Выведены прогоном по цепи на 11 пулах:
# уцелели ровно два места, и они дали у каждого пула одно и то же число (у
# lbPair шаг лежит числом и байтами семени PDA). Читаем ОБА и требуем согласия:
# если места спорят, значит счёт не тот или раскладка изменилась, и брать
# нельзя. Длина счёта у всех одиннадцати -- 904 байта.
СМ_ШАГА_КОРЗИНЫ_DLMM = (73, 80)
ДЛИНА_СЧЁТА_ПУЛА_DLMM = 904


def шаг_корзины_из_счёта(данные: bytes):
    """Шаг корзины из счёта пула DLMM. None -- счёт не тот или места спорят."""
    if not данные or len(данные) < max(СМ_ШАГА_КОРЗИНЫ_DLMM) + 2:
        return None
    значения = {struct.unpack_from("<H", данные, см)[0]
                for см in СМ_ШАГА_КОРЗИНЫ_DLMM}
    if len(значения) != 1:
        return None
    шк = значения.pop()
    return шк if 1 <= шк <= 2000 else None


def событие_dlmm(данные: bytes) -> dict | None:
    """Событие свопа DLMM из данных внутренней инструкции или None."""
    if (len(данные) != DLMM_ДЛИНА_СВОПА or данные[:8] != DLMM_CPI_ДИСК
            or данные[8:16] != DLMM_СВОП_ДИСК):
        return None
    т = данные[16:]
    начало, конец = struct.unpack_from("<ii", т, 64)
    вошло, вышло = struct.unpack_from("<QQ", т, 72)
    комиссия, протокол = struct.unpack_from("<QQ", т, 89)
    return {"пул": b58encode(т[0:32]), "start_bin_id": начало,
            "end_bin_id": конец, "amount_in": вошло, "amount_out": вышло,
            "swap_for_y": т[88], "fee": комиссия, "protocol_fee": протокол}


def события_dlmm(tx: dict) -> list:
    """Все события свопа DLMM транзакции. Роутер трогает по нескольку пулов."""
    из_ = []
    for группа in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        for их in группа.get("instructions") or []:
            if (их.get("programId") or их.get("program")) != DLMM:
                continue
            с_ = событие_dlmm(b58decode(их.get("data") or ""))
            if с_ is not None:
                из_.append(с_)
    return из_


def цена_корзины_dlmm(шаг_корзины: int, корзина: int) -> D:
    """Цена корзины в СЫРЫХ единицах: (1 + bin_step/10000)^bin."""
    return (D(1) + D(int(шаг_корзины)) / D(10000)) ** int(корзина)


def dlmm_min_out(tpl: dict, tx: dict, amount_in: int, slippage: float,
                  шаг_корзины: int | None = None) -> dict:
    """Минимум выхода в Meteora DLMM: цена -- по КОНЕЧНОЙ корзине сделки.

    Наша покупка идёт сразу после сделки источника, то есть с той корзины, на
    которой он остановился. Цена конечной корзины -- худшая из тех, что получил
    он, и именно она наша. Кончится корзина -- минимум выхода не выполнится и
    цепь откажет: это промах, а не потеря.

    БЕЗ ШАГА КОРЗИНЫ -- ОТКАЗ. Он лежит в счёте пула и читается отдельным
    прогоном; догадка о нём означала бы минимум ниже настоящего.
    """
    пул = tpl["accounts"][0] if (tpl.get("accounts") or []) else None
    if not пул:
        return {"ok": False, "why_not": "в инструкции DLMM нет счёта пула"}
    события = [с for с in события_dlmm(tx) if с["пул"] == пул]
    if not события:
        return {"ok": False, "why_not": "нет события свопа DLMM по нашему пулу"}
    mv = mints_and_vaults(tpl, tx)
    if not mv:
        return {"ok": False, "why_not": "минты и хранилища не восстановились"}
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    qv, bv = rows.get(mv["quote_vault"]), rows.get(mv["base_vault"])
    if not qv or not bv:
        return {"ok": False, "why_not": "хранилищ пула нет в балансах транзакции"}
    вход = qv["post"] - qv["pre"]
    выход = bv["pre"] - bv["post"]
    if вход <= 0 or выход <= 0:
        return {"ok": False,
                "why_not": "сделка источника не покупка по этим хранилищам"}
    # СОБЫТИЕ ОБЯЗАНО ОБЪЯСНЯТЬ ХРАНИЛИЩА. Роутер заходит в один пул и дважды;
    # тогда движение хранилищ -- сумма двух шагов, и к одному событию не
    # относится. Такую сделку не считаем вовсе.
    событие = next((с for с in события
                    if с["amount_in"] == вход and с["amount_out"] == выход), None)
    if событие is None:
        return {"ok": False,
                "why_not": "событие DLMM не сходится с хранилищами: сделка "
                           "источника прошла этот пул не один раз"}
    if шаг_корзины is None:
        шаг_корзины = СТУПЕНИ_DLMM.get(пул)
    if not шаг_корзины:
        if пул not in ПУЛЫ_БЕЗ_СТУПЕНИ:
            НУЖНЫ_ПУЛЫ_DLMM.add(пул)
        return {"ok": False,
                "why_not": f"шаг корзины DLMM неизвестен: пул {пул} ещё не "
                           f"прочитан", "pool": пул}
    низ_к = min(событие["start_bin_id"], событие["end_bin_id"])
    верх_к = max(событие["start_bin_id"], событие["end_bin_id"])
    ставка = (D(событие["fee"]) / D(событие["amount_in"])
              if событие["amount_in"] else D(0))
    if not (D(0) <= ставка < D(1)):
        return {"ok": False, "why_not": "доля комиссии из события вне смысла"}
    средняя = D(событие["amount_in"] - событие["fee"]) / D(событие["amount_out"])
    низ = цена_корзины_dlmm(шаг_корзины, низ_к - ЗАПАС_КОРЗИН_DLMM)
    верх = цена_корзины_dlmm(шаг_корзины, верх_к + ЗАПАС_КОРЗИН_DLMM)
    # МОДЕЛЬ ОБЯЗАНА ВОСПРОИЗВЕСТИ СДЕЛКУ ИСТОЧНИКА. Средняя цена сделки лежит
    # между ценами первой и последней корзины -- иначе прочитан не тот шаг или
    # не то событие, и считать нашу цену нечем.
    if not (низ <= средняя <= верх):
        return {"ok": False,
                "why_not": f"модель корзин не воспроизвела сделку источника "
                           f"(шаг {шаг_корзины}, корзины {низ_к}..{верх_к})",
                "pool": пул}
    цена_нам = цена_корзины_dlmm(шаг_корзины, верх_к)
    if цена_нам <= 0:
        return {"ok": False, "why_not": "цена конечной корзины не считается"}
    ожидаем = D(amount_in) * (D(1) - ставка) / цена_нам
    минимум = int(ожидаем * (D(1) - D(str(slippage))))
    if минимум <= 0:
        return {"ok": False, "why_not": "минимум выхода вышел нулевым"}
    return {"ok": True, "pool": пул, "bin_step": int(шаг_корзины),
            "start_bin_id": событие["start_bin_id"],
            "end_bin_id": событие["end_bin_id"],
            "fee_share": float(ставка), "source_in": вход, "source_out": выход,
            "bin_price": float(цена_нам), "expected_out": int(ожидаем),
            "min_out": минимум}


def min_out_from_reserves(tpl: dict, tx: dict, amount_in: int, slippage: float) -> dict:
    """Минимум токенов по резервам ПОСЛЕ сделки источника, x*y=k.

    f -- доля траты, доходящая до пула, калиброванная на сделке источника по
    его резервам ДО и его дельтам: f = x0*dy/((y0-dy)*spent), где spent --
    всё, что ушло из котировки в счета этой инструкции (пул + комиссии)."""
    if tpl["program"] == DAMM2:
        # DAMM v2: цена -- по событию свопа и сделке источника (см. c2_cl_quote).
        return damm2_min_out(tpl, tx, amount_in, slippage)
    if tpl["program"] == DBC:
        return dbc_min_out(tpl, tx, amount_in, slippage)
    if tpl["program"] == CLMM:
        # Кривая CLMM считается, но ставка комиссии живёт в amm_config:
        # пока её не передали, денежный путь честно отказывает.
        return clmm_min_out(tpl, tx, amount_in, slippage)
    if tpl["program"] == DLMM:
        # Цена корзины, а не резервы: у DLMM резервы цену действительно не
        # дают, и раньше здесь стоял честный отказ. Теперь считается по
        # событию свопа и шагу корзины из счёта пула.
        return dlmm_min_out(tpl, tx, amount_in, slippage)
    if tpl["program"] == LAUNCHLAB:
        return launchlab_min_out(tx, amount_in, slippage)
    if tpl["program"] == BONDING:
        return bonding_min_out(tx, tpl["accounts"][spec_of(tpl)["base_mint"]],
                               amount_in, slippage)
    mv = mints_and_vaults(tpl, tx)
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    qv, bv = rows.get(mv["quote_vault"]), rows.get(mv["base_vault"])
    if not qv or not bv:
        return {"ok": False, "why_not": "хранилищ нет в балансах транзакции"}
    x0, y0, x1, y1 = qv["pre"], bv["pre"], qv["post"], bv["post"]
    dy = y0 - y1
    spent = sum(r["post"] - r["pre"] for a, r in rows.items()
                if a in tpl["accounts"] and r["mint"] == mv["quote_mint"] and r["post"] > r["pre"])
    if dy <= 0 or spent <= 0 or y0 <= dy:
        return {"ok": False, "why_not": "сделка источника не покупка по этим хранилищам"}
    f = D(x0) * D(dy) / (D(y0 - dy) * D(spent))
    # f > 1 ЗНАЧИТ "ПОКУПКА ВЫШЕ КРИВОЙ БЕЗ КОМИССИИ", И ЭТО НАЗЫВАЕТСЯ ПОЛЕМ, А
    # НЕ ПРАВИТСЯ ЧИСЛОМ. Замер Code-3 на живых образцах 02.10: в хранилище
    # Raydium CPMM лежат ещё не снятые protocol_fees/fund_fees, резерв меньше
    # остатка счёта, и у 9 из 25 образцов f доходит до 1.088 (у образца 22 весь
    # spent вошёл в хранилище котировки целиком -- комиссии со входа не было
    # вовсе). Ограничить f единицей значило бы ОПУСТИТЬ минимум выхода покупки,
    # то есть ослабить проскальзывание на денежном пути, а «проскальзывание и
    # порог 30 % не трогать» -- правило 4. Правильная правка -- вычесть эти два
    # поля из x0/x1, и для неё нужно читать счёт пула. Поэтому здесь ровно
    # честное поле: круг Code-3 отказывает по имени, а не прячет, и решение по
    # числу остаётся за владельцем.
    a_eff = D(amount_in) * f
    expected = D(y1) * a_eff / (D(x1) + a_eff)
    mn = int(expected * D(1 - slippage))
    return {"ok": True, "fee_factor": float(f), "reserves_after": [x1, y1],
            "f_vyshe_krivoj": bool(f > 1),
            "expected_out": int(expected), "min_out": mn}


# ------------------------------------------------------------ Launchlab: виртуальные резервы

# Событие сделки Launchlab: дискриминатор Anchor sha256("event:TradeEvent")[:8]
# = bddb7fd34ee661ee (совпадает с «Program data» настоящих транзакций).
# Раскладка u64 после pool_state (32 байта) установлена по данным: реальные
# резервы до/после сходятся с движением хранилищ, выход сделки источника
# воспроизводится формулой кривой ТОЧНО (самопроверка на всех образцах).
LL_EVENT_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]
LL_FIELDS = ("total_base_sell", "virtual_base", "virtual_quote", "real_base_before",
             "real_quote_before", "real_base_after", "real_quote_after", "amount_in", "amount_out",
             "protocol_fee", "platform_fee")


def launchlab_event(tx: dict) -> dict | None:
    for ln in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        if not ln.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(ln[len("Program data: "):].strip())
        except ValueError:
            continue
        if raw[:8] != LL_EVENT_DISC or len(raw) < 40 + 8 * len(LL_FIELDS):
            continue
        vals = struct.unpack("<" + "Q" * len(LL_FIELDS), raw[40:40 + 8 * len(LL_FIELDS)])
        return dict(zip(LL_FIELDS, vals))
    return None


def launchlab_out(ev: dict, amount_in: int, after: bool, fee_rate: D) -> D:
    rb = ev["real_base_after"] if after else ev["real_base_before"]
    rq = ev["real_quote_after"] if after else ev["real_quote_before"]
    base_res, quote_res = D(ev["virtual_base"] - rb), D(ev["virtual_quote"] + rq)
    net = D(amount_in) * (1 - fee_rate)
    return base_res * net / (quote_res + net)


def launchlab_min_out(tx: dict, amount_in: int, slippage: float) -> dict:
    ev = launchlab_event(tx)
    if not ev:
        return {"ok": False, "why_not": "нет события TradeEvent Launchlab в логах"}
    fee = ev["amount_in"] - (ev["real_quote_after"] - ev["real_quote_before"])
    fee_rate = D(fee) / D(ev["amount_in"]) if ev["amount_in"] else D(0)
    exp = launchlab_out(ev, amount_in, True, fee_rate)
    return {"ok": True, "fee_rate": float(fee_rate), "virtual_reserves_after": [
        # ПОРЯДОК -- [КОТИРОВКА, БАЗА], как у двух соседей: min_out_from_reserves
        # отдаёт [x1, y1] (вход-котировка, выход-база), bonding_min_out --
        # [virtual_sol_reserves, virtual_token_reserves]. Здесь порядок был
        # обратный, и правило тонкого пула в bloom_lane_two_step становилось
        # СЛЕПЫМ на LaunchLab: оно читает рез[0] как котировку, делит на
        # 10**q_dec и умножает на цену ноги. Замер Code-3 на образце
        # 3vzWr8fvcP6RK8 (price_sol 0.008361, q_dec 6): база 661 046 006 523 532
        # даёт 5 527 021 SOL-экв. при пороге тонкого пула 30, то есть наценка не
        # срабатывала НИКОГДА; настоящая котировка 499 800 652 -- это 4.18
        # SOL-экв., и пул тонкий. Заодно pool_reserve_quote_raw в журнале
        # решений писал резерв БАЗЫ вместо котировки.
        ev["virtual_quote"] + ev["real_quote_after"],
        ev["virtual_base"] - ev["real_base_after"]],
        "expected_out": int(exp), "min_out": int(exp * D(1 - slippage))}


# ------------------------------------------------------------ Pump.fun: кривая до миграции

# Событие сделки кривой -- тоже "event:TradeEvent" (дискриминатор тот же, что у
# Launchlab), но раскладка другая и длина у настоящих покупок 374 байта:
#   минт(32) sol(8) токены(8) покупка(1) кошелёк(32) время(8)
#   вирт_sol(8) вирт_токены(8) реал_sol(8) реал_токены(8)
#   получатель_комиссии(32) bps(8) комиссия(8) создатель(32) bps(8) комиссия(8)
# Раскладка НЕ угадана: у программы встречается и другая, длинная запись (389
# байт в образце HzfxBXKq), поэтому каждое разобранное событие обязано
# воспроизвести собственную сделку по кривой -- иначе оно не берётся вовсе.
PF_EVENT_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]
PF_MIN_EVENT = 8 + 217


def pump_trade_event(tx: dict, mint: str | None = None) -> dict | None:
    """Событие покупки по кривой pump.fun из логов сделки источника.

    Виртуальные резервы в событии -- ПОСЛЕ сделки источника, то есть ровно та
    цена, по которой считаем свою покупку следом. None -- событие не найдено,
    не разобралось или не воспроизвело собственную сделку."""
    for ln in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        if not ln.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(ln[len("Program data: "):].strip())
        except ValueError:
            continue
        if raw[:8] != PF_EVENT_DISC or len(raw) < PF_MIN_EVENT:
            continue
        b = raw[8:]
        try:
            ev_mint = str(Pubkey(bytes(b[0:32])))
            sol, tok = struct.unpack_from("<QQ", b, 32)
            is_buy = b[48]
            user = str(Pubkey(bytes(b[49:81])))
            vs, vt, rs, rt = struct.unpack_from("<QQQQ", b, 89)
            fee_bps, fee = struct.unpack_from("<QQ", b, 153)
            cr_bps, cr_fee = struct.unpack_from("<QQ", b, 201)
        except (struct.error, ValueError):
            continue
        if mint is not None and ev_mint != mint:
            continue
        if is_buy != 1 or sol <= 0 or tok <= 0 or vs <= sol or vt <= 0:
            continue
        vs0, vt0 = vs - sol, vt + tok
        # ПРОВЕРКА ЗДЕСЬ ЛОВИТ НЕ ТУ РАСКЛАДКУ, А НЕ ОКРУГЛЕНИЕ ПРОГРАММЫ.
        # У разновидности "точный выход" формула воспроизводит выход ТОЧНО
        # (отклонение 0 на пяти сделках), у "точного входа" -- с отклонением
        # 2e-8 (13 163 токена из 650 млрд, замер 26.09): целочисленная
        # арифметика программы округляет иначе. Требовать бит в бит значит
        # отказываться от живой разновидности; поэтому допуск 1e-6, а неверная
        # раскладка даёт расхождение на порядки и всё равно не проходит.
        пред = vt0 * sol // (vs0 + sol)
        if tok <= 0 or abs(пред - tok) > max(1, tok // 1_000_000):
            continue
        if -(-sol * fee_bps // 10_000) != fee or -(-sol * cr_bps // 10_000) != cr_fee:
            continue                                   # поля комиссий не на месте
        return {"mint": ev_mint, "user": user, "sol_amount": sol, "token_amount": tok,
                "virtual_sol_reserves": vs, "virtual_token_reserves": vt,
                "real_sol_reserves": rs, "real_token_reserves": rt,
                "virtual_before": [vs0, vt0], "fee_bps": fee_bps, "fee": fee,
                "creator_fee_bps": cr_bps, "creator_fee": cr_fee}
    return None


def pump_net_to_curve(budget: int, fee_bps: int, creator_bps: int) -> int:
    """Сколько лампортов дойдёт до кривой, если ВСЯ трата вместе с комиссиями не
    должна превысить budget. Комиссии программа считает вверх (ceil) -- это
    проверено на настоящих сделках, поэтому подбор идёт по целым лампортам."""
    if budget <= 0 or fee_bps < 0 or creator_bps < 0:
        return 0

    def всего(n: int) -> int:
        return n + -(-n * fee_bps // 10_000) + -(-n * creator_bps // 10_000)

    net = budget * 10_000 // (10_000 + fee_bps + creator_bps)
    while net > 0 and всего(net) > budget:
        net -= 1
    while всего(net + 1) <= budget:
        net += 1
    return net


def bonding_min_out(tx: dict, mint: str | None, amount_in: int, slippage: float) -> dict:
    """Минимум токенов на кривой pump.fun при трате amount_in лампортов ВСЕГО
    (вместе с комиссиями программы). Возвращает и трату до кривой, и предел."""
    ev = pump_trade_event(tx, mint)
    if not ev:
        return {"ok": False, "why_not": "нет события сделки кривой pump.fun в логах"}
    net = pump_net_to_curve(amount_in, ev["fee_bps"], ev["creator_fee_bps"])
    if net <= 0:
        return {"ok": False, "why_not": "трата меньше комиссий кривой"}
    expected = ev["virtual_token_reserves"] * net // (ev["virtual_sol_reserves"] + net)
    return {"ok": True, "fee_bps": ev["fee_bps"], "creator_fee_bps": ev["creator_fee_bps"],
            "sol_to_curve": net, "max_sol_cost": amount_in,
            "virtual_reserves_after": [ev["virtual_sol_reserves"], ev["virtual_token_reserves"]],
            "expected_out": int(expected), "min_out": int(D(expected) * D(1 - slippage))}


# ------------------------------------------------------------ самопроверка

def load_samples(program: str) -> list:
    p = SAMPLES_DIR / f"{program}.json"
    own = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    if program == BONDING:
        # Живые разновидности инструкции кривой лежат отдельным файлом: их
        # собрал прогон проверки симуляцией с настоящих сигналов.
        вар = C.DATA / "c2_curve_variant_samples.json"
        if вар.exists():
            д = json.loads(вар.read_text(encoding="utf-8"))
            for ключ, лист in д.items():
                if ключ not in BONDING_DISCS:
                    continue
                for x in лист:
                    if x.get("pool_vault") and x.get("mint") and x.get("tx"):
                        own.append({"source": x.get("source"), "mint": x["mint"],
                                    "pool_vault": x["pool_vault"], "quote_mint": None,
                                    "tx": x["tx"]})
        return own
    if program not in (DLMM, CLMM):
        return own
    # DLMM и CLMM встречаются и как шаг чужих маршрутов (SOL -> xStock и т.п.)
    # -- эти инструкции тоже настоящие, берём их в проверку.
    n_min, ix_name, vi = (16, "swap2", 2) if program == DLMM else (13, "swap_v2", 5)
    seen = {(C.first_signature(x["tx"]), x["pool_vault"]) for x in own}
    extra = []
    for f in sorted(SAMPLES_DIR.glob("*.json")):
        if f.name == p.name:
            continue
        for x in json.loads(f.read_text(encoding="utf-8")):
            for ix in all_instructions(x["tx"]):
                if ix.get("programId") != program or len(ix["accounts"]) < n_min:
                    continue
                if b58decode(ix["data"])[:8] not in ((disc(ix_name), disc("swap")) if program == DLMM
                                                     else (disc(ix_name),)):
                    continue
                key = (C.first_signature(x["tx"]), ix["accounts"][vi])
                if key in seen:
                    continue
                seen.add(key)
                extra.append({"tx": x["tx"], "pool_vault": ix["accounts"][vi], "source": x["source"],
                              "mint": None, "quote_mint": None})
    return own + extra


def rebuild_check(s: dict, program: str) -> dict:
    """Восстановить инструкцию источника из шаблона и его же адреса."""
    tx = s["tx"]
    tpl = extract_template(tx, program, s["pool_vault"])
    if not tpl["ok"]:
        return {"ok": None, "why": tpl["why_not"]}
    spec_t = spec_of(tpl)
    user = tpl["accounts"][spec_t["user"][0]]
    try:
        ix = swap_instruction(tpl, tx, user, tpl["arg0"], tpl["arg1"], keep_source_ix=True)
    except ValueError as exc:
        return {"ok": False, "why": str(exc)}
    got = [str(m.pubkey) for m in ix.accounts]
    diff = [i for i, (g, w) in enumerate(zip(got, tpl["accounts"])) if g != w]
    same_data = bytes(ix.data) == tpl["data"]
    # Счёт пользователя у источника -- не ATA (временный счёт роутера):
    # сверять не с чем, это не ошибка сборщика, а другой счёт у источника.
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    user_tok = {i for i in user_accounts(tpl, tx, user)} - set(spec_t["user"])
    not_ata = [i for i in diff if i in user_tok and (tpl["accounts"][i] not in rows
                                                     or rows[tpl["accounts"][i]]["owner"] == user)]
    if diff and set(diff) == set(not_ata) and same_data:
        return {"ok": None, "why": "у источника токен-счёт не ATA", "diff_idx": diff}
    # Платит роутер (его PDA -- «пользователь» инструкции, не подписант), а
    # выход приходит на счёт самого источника: пользователь и владелец
    # выходного счёта разные -- сверять ATA пользователя не с чем.
    if diff and user not in tpl["signers"] and set(diff) <= user_tok and same_data:
        return {"ok": None, "why": "платит роутер, токен-счёт чужой", "diff_idx": diff}
    # Подписант платит сам, а выход приходит на счёт ДРУГОГО владельца
    # (получатель не подписант): наш ATA сверять не с чем.
    foreign = [i for i in diff if i in user_tok and tpl["accounts"][i] in rows
               and rows[tpl["accounts"][i]]["owner"] != user]
    if diff and set(diff) == set(foreign) and same_data:
        return {"ok": None, "why": "выход источника на счёт другого владельца", "diff_idx": diff}
    return {"ok": not diff and same_data, "diff_idx": diff, "same_data": same_data,
            "ix": tpl["ix"], "user_is_signer": user in tpl["signers"]}


def self_test() -> int:
    checks = []
    # СОСТОЯНИЕ СЛУЖБЫ САМОПРОВЕРКА НЕ ТРОГАЕТ: связанные накопители пишем в
    # временный файл, а не в файл хоста.
    import tempfile as _tfm  # noqa: PLC0415
    _проб_файл = os.path.join(_tfm.mkdtemp(), "assoc.json")
    _env_было = os.environ.get("BLOOM_ASSOC_UVA_FILE")
    os.environ["BLOOM_ASSOC_UVA_FILE"] = _проб_файл
    assoc_uva_забыть()
    # --- CLMM: СОБЫТИЕ СВОПА ИЗ ЛОГОВ, сверенное с самой сделкой. Это первый
    # шаг строителя Raydium CLMM (очередь владельца, второй номер): без события
    # минимума выхода у сосредоточенной ликвидности не посчитать, а угадывать
    # раскладку на деньгах нельзя.
    try:
        import c2_cl_quote as _CL  # noqa: PLC0415
        обр_clmm = load_samples(CLMM)
    except Exception:  # noqa: BLE001
        обр_clmm = []
    if обр_clmm:
        нашлось = сошлось = с_налогом = 0
        for x in обр_clmm:
            tx_ = x.get("tx") or {}
            пул_ = None
            for ix in all_instructions(tx_):
                if (ix.get("programId") == CLMM
                        and x.get("pool_vault") in (ix.get("accounts") or [])):
                    сч = ix["accounts"]
                    if len(сч) > _CL.СМ_ПУЛ_В_ИНСТРУКЦИИ_CLMM:
                        пул_ = сч[_CL.СМ_ПУЛ_В_ИНСТРУКЦИИ_CLMM]
                    break
            if not пул_:
                continue
            с_ = _CL.событие_пула_clmm(tx_, пул_, b58encode)
            if not с_:
                continue
            нашлось += 1
            # СВЕРКА С САМОЙ СДЕЛКОЙ: одна из двух сумм события обязана совпасть
            # с движением хранилища пула по цепи. Не совпало -- раскладка не та.
            строки_ = {r["account"]: r for r in C.token_rows(tx_).values()}
            r_ = строки_.get(x.get("pool_vault"))
            if not r_:
                continue
            # post/pre у token_rows -- СЫРЫЕ числа: умножать их на знаки ещё раз
            # значит сравнивать событие с числом в миллион раз больше.
            дельта = abs(int(r_["post"]) - int(r_["pre"]))
            # НАЛОГ ПЕРЕВОДА (Token-2022) событие называет отдельно, а хранилище
            # видит сумму ПОСЛЕ него: поэтому сходиться может и amount, и
            # amount ± налог. Считаем и то, сколько сошлось только с налогом --
            # это число говорит, что поле налога прочитано верно.
            свои = {с_["amount_0"], с_["amount_1"],
                    с_["amount_0"] - с_["fee_0"], с_["amount_0"] + с_["fee_0"],
                    с_["amount_1"] - с_["fee_1"], с_["amount_1"] + с_["fee_1"]}
            если_сошлось = дельта in свои
            сошлось += int(если_сошлось)
            if если_сошлось and дельта not in (с_["amount_0"], с_["amount_1"]):
                с_налогом += 1
        checks.append((f"CLMM: событие свопа найдено у {нашлось} из {len(обр_clmm)} "
                       f"образцов, и у {сошлось} из {нашлось} сумма события сошлась "
                       f"с движением хранилища пула по цепи (из них {с_налогом} -- "
                       f"только с учётом налога перевода из того же события)",
                       нашлось >= 4 and сошлось >= нашлось - 1))
        for x in обр_clmm[:1]:
            tx_ = x.get("tx") or {}
            тела_ = _CL.тела_событий_clmm(tx_)
            if тела_:
                с1 = _CL.событие_clmm(тела_[0])
                checks.append((f"CLMM: у события есть ликвидность ({с1['ликвидность']}) "
                               f"и цена после сделки ({с1['цена_после']}) -- решать L "
                               f"из входа и выхода, как у DAMM v2, не нужно",
                               с1["ликвидность"] > 0 and с1["цена_после"] > 0))

        # --- ТИК CLMM: номер тика из события против номера, посчитанного из
        # цены того же события. Это проверка тик-математики на ЦЕПИ, а не на
        # придуманных числах: своих констант из чужого кода здесь нет.
        сошлось_тик = всего_тик = 0
        for x in обр_clmm:
            tx_ = x.get("tx") or {}
            for т_ in _CL.тела_событий_clmm(tx_):
                с_ = _CL.событие_clmm(т_)
                if с_["цена_после"] <= 0:
                    continue
                всего_тик += 1
                сошлось_тик += int(_CL.цена_по_тику(с_["тик"]) <= с_["цена_после"]
                                   < _CL.цена_по_тику(с_["тик"] + 1)
                                   and _CL.тик_по_цене(с_["цена_после"]) == с_["тик"])
        checks.append((f"CLMM: номер тика из цены сошёлся с номером из события у "
                       f"{сошлось_тик} из {всего_тик} событий",
                       всего_тик >= 30 and сошлось_тик == всего_тик))

        # --- МИНИМУМ ВЫХОДА CLMM. Ставка комиссии пула ПРОЧИТАНА ИЗ ЦЕПИ:
        # файл data/clmm_konfigi.json -- это счета amm_config всех конфигов
        # наших образцов, снятые прогоном deploy/checks/nogi_vse_kotirovki.py
        # (getMultipleAccounts, только чтение). Раскладка счёта подтверждена, а
        # не заявлена: у четырёх конфигов ставка была независимо решена по живым
        # сделкам (1000, 2500, 10000, 40000 миллионных) и прочитанный
        # trade_fee_rate совпал с решённым числом у всех четырёх, а ещё у трёх
        # (500, 2000, 1500) совпала ставка отдельных сделок. Шаг тика вышел
        # согласованным с уровнем комиссии (0.05 % -- 1, 0.1..0.2 % -- 10,
        # 0.25..0.4 % -- 60, 1..4 % -- 120), что подделать смещением нельзя.
        ф_кон = C.DATA / "clmm_konfigi.json"
        ставки_цепи = {}
        if ф_кон.exists():
            try:
                _д = json.loads(ф_кон.read_text(encoding="utf-8"))["конфиги"]
                ставки_цепи = {а: (з.get("разбор") or {}) for а, з in _д.items()
                               if (з.get("разбор") or {}).get("ставка_1e6")}
            except Exception as exc:  # noqa: BLE001
                checks.append((f"файл счетов amm_config не читается: "
                                f"{type(exc).__name__}", False))
        checks.append((f"CLMM: ставок комиссии, прочитанных из цепи: "
                       f"{len(ставки_цепи)} ({sorted(з['ставка_1e6'] for з in ставки_цепи.values())} "
                       f"миллионных, шаги тика "
                       f"{sorted({з['шаг_тика'] for з in ставки_цепи.values()})})",
                       len(ставки_цепи) >= 4))
        сошлось_ = отказ_границы_ = прочий_отказ_ = отказ_края_ = 0
        худшее_ = 0.0
        двигаем_ = []
        пара_для_кривой = None
        for x in обр_clmm:
            tx_ = x.get("tx") or {}
            tpl_ = extract_template(tx_, CLMM, x.get("pool_vault"))
            if not tpl_.get("ok") or len(tpl_["accounts"]) < 3:
                continue
            к_ = ставки_цепи.get(tpl_["accounts"][1])
            if not к_:
                continue
            р = clmm_min_out(tpl_, tx_, 10_000_000, 0.02,
                             ставка_1e6=к_["ставка_1e6"], шаг_тика=к_["шаг_тика"])
            if р.get("ok"):
                сошлось_ += 1
                худшее_ = max(худшее_, р["model_error"])
                двигаем_.append((р["ticks_we_move"], р["ticks_to_array_edge"]))
                if пара_для_кривой is None:
                    м2 = clmm_min_out(tpl_, tx_, 20_000_000, 0.02,
                                      ставка_1e6=к_["ставка_1e6"])
                    if м2.get("ok"):
                        пара_для_кривой = (р, м2, tpl_, tx_, к_["ставка_1e6"])
            elif "перешла границу диапазона" in (р.get("why_not") or ""):
                отказ_границы_ += 1
            elif "за край массива тиков" in (р.get("why_not") or ""):
                отказ_края_ += 1
            else:
                прочий_отказ_ += 1
        if ставки_цепи:
            checks.append((f"CLMM: минимум выхода посчитан у {сошлось_} живых сделок "
                           f"по ставке ИЗ ЦЕПИ (наибольшее расхождение модели "
                           f"{худшее_:.2e}), отказ «перешла границу диапазона» у "
                           f"{отказ_границы_}, отказ «за край массива тиков» у "
                           f"{отказ_края_}, прочих отказов {прочий_отказ_}",
                           сошлось_ >= 15 and прочий_отказ_ == 0
                           and худшее_ < float(_CL.ПРЕДЕЛ_РАСХОЖДЕНИЯ_CLMM)))
            # НАШ РАЗМЕР ЦЕНУ ПОЧТИ НЕ ДВИГАЕТ -- значит риск тик-массивов у
            # сделки 0.01 SOL не теоретический, а измеренный: если бы наша
            # покупка перескакивала границу массива, её нужного массива могло
            # не быть в счетах источника, и сделка бы не села.
            if двигаем_:
                нули = [т_ for т_, _ in двигаем_ if т_ == 0]
                края = [к for _, к in двигаем_ if к is not None]
                checks.append((f"CLMM: наш вход 0.01 SOL двигает цену на 0 тиков у "
                               f"{len(нули)} из {len(двигаем_)} сделок, больше всего "
                               f"{max(т_ or 0 for т_, _ in двигаем_)} тиков, и ни одна "
                               f"принятая покупка не выходит за край массива "
                               f"(наименьший запас {min(края) if края else None}, "
                               f"отказов по краю {отказ_края_})",
                               bool(края) and min(края) >= 0 and отказ_края_ >= 1))
        if пара_для_кривой:
            м1, м2, tpl_, tx_, ст_ = пара_для_кривой
            checks.append((f"CLMM: вдвое больший вход даёт больший выход "
                           f"({м1['expected_out']} -> {м2['expected_out']}), "
                           f"минимум ниже ожидания на проскальзывание "
                           f"({м1['min_out']} < {м1['expected_out']})",
                           м2["expected_out"] > м1["expected_out"]
                           and м1["min_out"] < м1["expected_out"]
                           and м1["min_out"] >= int(м1["expected_out"] * 0.97)))
            checks.append((f"CLMM: без ставки комиссии -- отказ, а не догадка: "
                           f"{clmm_min_out(tpl_, tx_, 10_000_000, 0.02).get('why_not')}",
                           clmm_min_out(tpl_, tx_, 10_000_000, 0.02).get("ok")
                           is False))
            checks.append((f"CLMM: нелепая ставка комиссии -- отказ: "
                           f"{clmm_min_out(tpl_, tx_, 10_000_000, 0.02, ставка_1e6=900_000).get('why_not')}",
                           clmm_min_out(tpl_, tx_, 10_000_000, 0.02,
                                        ставка_1e6=900_000).get("ok") is False))
        # Раскладка amm_config: разбор обязан отвергать мусор.
        checks.append(("CLMM: разбор amm_config отвергает пустой счёт и короткий",
                       _CL.конфиг_clmm(bytes(57)) == {}
                       and _CL.конфиг_clmm(b"\x00" * 10) == {}))
        # КЭШ СТАВОК: горячий путь смотрит в словарь, а незнакомый конфиг
        # кладёт заданием фону -- и отказывает, а не покупает по догадке.
        СТАВКИ_CLMM.clear()
        НУЖНЫ_КОНФИГИ_CLMM.clear()
        взято_ = загрузить_ставки_clmm()
        checks.append((f"CLMM: ставок в кэш из файла загружено {взято_}",
                       взято_ >= 4))
        если_кэш = None
        for x in обр_clmm:
            tx_ = x.get("tx") or {}
            tpl_ = extract_template(tx_, CLMM, x.get("pool_vault"))
            if not tpl_.get("ok") or len(tpl_["accounts"]) < 3:
                continue
            if tpl_["accounts"][1] in СТАВКИ_CLMM:
                если_кэш = (tpl_, tx_, tpl_["accounts"][1])
                break
        if если_кэш:
            tpl_, tx_, конфиг_ = если_кэш
            без_ = clmm_min_out(tpl_, tx_, 10_000_000, 0.02)
            ст_ = СТАВКИ_CLMM.pop(конфиг_)
            после_ = clmm_min_out(tpl_, tx_, 10_000_000, 0.02)
            checks.append((f"CLMM: со ставкой в кэше минимум считается "
                           f"({без_.get('ok')}, ставка {без_.get('fee_rate_1e6')}), "
                           f"без неё -- отказ ({после_.get('why_not')})",
                           без_.get("ok") is True
                           and без_.get("fee_rate_1e6") == ст_["ставка_1e6"]
                           and после_.get("ok") is False))
            checks.append((f"CLMM: незнакомый конфиг попал заданием фону: "
                           f"{конфиг_ in НУЖНЫ_КОНФИГИ_CLMM}",
                           конфиг_ in НУЖНЫ_КОНФИГИ_CLMM))
            СТАВКИ_CLMM[конфиг_] = ст_
            НУЖНЫ_КОНФИГИ_CLMM.discard(конфиг_)
        _д2 = bytearray(60)
        _д2[47:51] = (2500).to_bytes(4, "little")
        _д2[51:53] = (60).to_bytes(2, "little")
        checks.append((f"CLMM: разбор amm_config даёт ставку и шаг тика "
                       f"{_CL.конфиг_clmm(bytes(_д2))}",
                       _CL.конфиг_clmm(bytes(_д2)).get("ставка_1e6") == 2500
                       and _CL.конфиг_clmm(bytes(_д2)).get("шаг_тика") == 60))

    # --- ПРОДАЖА PUMP AMM: раскладка выведена по НАШЕЙ СОБСТВЕННОЙ продаже.
    # Файл образцов собирает прогон deploy/checks/nogi_vse_kotirovki.py по
    # подписям наших продаж: внутри транзакции Jupiter лежит настоящая
    # инструкция пула, и именно её раскладку мы обязаны воспроизвести.
    ф_прод = C.DATA / "obrazcy_vidov_instrukciy.json"
    if ф_прод.exists():
        try:
            обр_прод = json.loads(ф_прод.read_text(encoding="utf-8"))["obrazcy"]
        except Exception as exc:  # noqa: BLE001
            обр_прод = {}
            checks.append((f"файл образцов видов инструкций не читается: "
                            f"{type(exc).__name__}", False))
        продажи = [x for x in обр_прод.values()
                   if x.get("program") == PUMP_AMM and x.get("disc") == disc("sell").hex()]
        checks.append((f"настоящих продаж Pump AMM в образцах: {len(продажи)}",
                       len(продажи) >= 1))
        for з in продажи:
            tpl = extract_template(з["tx"], PUMP_AMM, з["accounts"][7], продажа=True)
            checks.append((f"продажа Pump AMM разбирается как шаблон: {tpl.get('ok')} "
                           f"({tpl.get('ix')}, счетов {len(tpl.get('accounts') or [])})",
                           bool(tpl.get("ok")) and tpl.get("ix") == "sell"))
            if not tpl.get("ok"):
                continue
            mv = mints_and_vaults(tpl, з["tx"])
            checks.append((f"у продажи роли на своих местах: база {str(mv.get('base_mint'))[:6]}, "
                           f"котировка {str(mv.get('quote_mint'))[:6]}, хранилища "
                           f"{str(mv.get('base_vault'))[:6]} / {str(mv.get('quote_vault'))[:6]}",
                           mv.get("quote_mint") == C.WSOL
                           and mv.get("base_mint") not in (None, C.WSOL)
                           and mv.get("base_vault") == з["accounts"][7]
                           and mv.get("quote_vault") == з["accounts"][8]))
            checks.append((f"аргументы продажи: отдаём {tpl['arg0']} сырых токена, "
                           f"минимум котировки {tpl['arg1']}",
                           isinstance(tpl["arg0"], int) and tpl["arg0"] > 0))
            как_покупка = extract_template(з["tx"], PUMP_AMM, з["accounts"][7])
            checks.append(("та же транзакция как ПОКУПКА не разбирается (инструкции "
                           "не путаются): " + str(как_покупка.get("why_not"))[:60],
                           как_покупка.get("ok") is False))
    else:
        checks.append(("файла образцов видов инструкций нет -- продажу Pump AMM "
                        "проверять нечем (соберётся прогоном по подписям продаж)",
                        True))
    for program, spec in SPECS.items():
        sm = load_samples(program)
        res = [rebuild_check(s, program) for s in sm]
        ok = [r for r in res if r["ok"]]
        skipped = [r for r in res if r["ok"] is None]
        bad = [r for r in res if r["ok"] is False]
        # Несовпадение допустимо только там, где у источника счета не ATA
        # (роутер со своими временными счетами) -- это видно по индексам.
        checks.append((f"{spec['label']}: из {len(sm)} настоящих транзакций восстановлено точно {len(ok)}, "
                       f"не сверяемо {len(skipped)} ({sorted({r['why'] for r in skipped})}), "
                       f"расхождений {len(bad)} {[r.get('diff_idx') for r in bad]}",
                       (len(ok) >= 10 or (program == LAUNCHLAB and len(ok) >= 8)
                        or (program == BONDING and len(ok) >= 4)
                        # DBC: в репозитории ДВЕ живые сделки этой программы.
                        # Порог честный -- два, и это мало: поэтому флаг LANE_DBC
                        # включается только после симуляции на хосте.
                        or (program == DBC and len(ok) >= 2)) and not bad))
        # Launchlab: в образцах задачи D всего 12 транзакций; добор до 10+
        # точных делает c2_swap_sim на раннере (сделки Launchlab из задачи A).
        t0 = time.perf_counter()
        n = 0
        for s in sm:
            tpl = extract_template(s["tx"], program, s["pool_vault"])
            if not tpl["ok"]:
                continue
            kp = Keypair()
            # ЗАПОМИНАТЬ ЗДЕСЬ БОЛЬШЕ НЕЧЕГО: место 21 выводится (ATA
            # накопителя места 20 для минта котировки), и для любого кошелька,
            # включая случайный, выходит сам. Прежняя фикстура кладбила в память
            # накопитель ИСТОЧНИКА под наш случайный кошелёк -- то есть заведомо
            # чужой ключ; со сверкой вывода это стало расхождением и отказом,
            # чем и показало, что фикстура была неправдой.
            b = build_buy(tpl, s["tx"], user=str(kp.pubkey()), payer=s["source"] or str(kp.pubkey()), amount_in=10_000_000,
                          min_out=1, cu_price_micro=100_000, tip=None)
            n += 1
            assert b["size"] <= 1232, b["size"]
        dt = (time.perf_counter() - t0) * 1000 / max(1, n)
        # Кривая pump.fun: в образцах репозитория 5 настоящих покупок, одна из
        # них другой разновидности инструкции (27 счетов) -- порог 4.
        порог_сборки = 4 if program == BONDING else (2 if program == DBC else 10)
        checks.append((f"{spec['label']}: сборка {n} транзакций, среднее {dt:.2f} мс, размер <= 1232",
                       n >= порог_сборки))
    v1 = v1_ok = 0
    for s_ in load_samples(DLMM):
        tpl = extract_template(s_["tx"], DLMM, s_["pool_vault"])
        if tpl.get("ok") and tpl["ix"] == "swap":
            v1 += 1
            ix = swap_instruction(tpl, s_["tx"], tpl["accounts"][10], tpl["arg0"], tpl["arg1"])
            v1_ok += bytes(ix.data) == tpl["data"]
    checks.append((f"DLMM swap v1: наши данные совпадают с данными источника байт в байт: {v1_ok} из {v1}",
                   v1 >= 5 and v1_ok == v1))
    # минимум по резервам: на настоящей покупке Pump AMM формула с f должна
    # вернуть сделку самого источника (его трата -> его токены).
    sm = load_samples(PUMP_AMM)
    errs = []
    for s in sm:
        tpl = extract_template(s["tx"], PUMP_AMM, s["pool_vault"])
        if not tpl["ok"]:
            continue
        mv = mints_and_vaults(tpl, s["tx"])
        rows = {r["account"]: r for r in C.token_rows(s["tx"]).values()}
        qv, bv = rows[mv["quote_vault"]], rows[mv["base_vault"]]
        spent = sum(r["post"] - r["pre"] for a, r in rows.items()
                    if a in tpl["accounts"] and r["mint"] == mv["quote_mint"] and r["post"] > r["pre"])
        f = D(qv["pre"]) * D(bv["pre"] - bv["post"]) / (D(bv["post"]) * D(spent))
        pred = D(bv["pre"]) * D(spent) * f / (D(qv["pre"]) + D(spent) * f)
        errs.append(abs(float(pred) / (bv["pre"] - bv["post"]) - 1))
    ll = []
    for s in load_samples(LAUNCHLAB):
        ev = launchlab_event(s["tx"])
        if not ev:
            continue
        fee = ev["amount_in"] - (ev["real_quote_after"] - ev["real_quote_before"])
        pred = launchlab_out(ev, ev["amount_in"], False, D(fee) / D(ev["amount_in"]))
        ll.append(abs(float(pred) - ev["amount_out"]) / ev["amount_out"])
        rows = {r["account"]: r for r in C.token_rows(s["tx"]).values()}
        bv = rows.get(s["pool_vault"])
        if bv and bv["pre"] - bv["post"] != ev["amount_out"]:
            ll.append(1.0)
    checks.append((f"Launchlab: кривая на виртуальных резервах из события воспроизводит выход "
                   f"источника ({len(ll)} проверок, макс. отклонение {max(ll) if ll else None})",
                   len(ll) >= 10 and max(ll) < 1e-6))
    checks.append((f"формула x*y=k с калибровкой воспроизводит сделку источника ({len(errs)} сделок, "
                   f"макс. отклонение {max(errs) if errs else None})", errs and max(errs) < 1e-9))
    # ---- MEТEORA DLMM: цена корзины (строитель N4). Проверяется ДЕНЕЖНОЕ:
    # минимум считается, он НЕ оптимистичнее сделки источника (наша цена не
    # лучше его средней, потому что мы идём следом), без шага корзины отказ,
    # при неверном шаге отказ, и событие обязано сходиться с хранилищами.
    взято_dlmm = загрузить_ступени_dlmm()
    dl_ок = dl_хуже = 0
    dl_отказы = []
    for s in load_samples(DLMM):
        t_ = extract_template(s["tx"], DLMM, s["pool_vault"])
        if not t_.get("ok"):
            continue
        р = min_out_from_reserves(t_, s["tx"], 10_000_000, 0.35)
        if not р.get("ok"):
            dl_отказы.append(р.get("why_not"))
            continue
        dl_ок += 1
        # Наша цена -- цена КОНЕЧНОЙ корзины, то есть не лучше средней цены
        # источника: иначе минимум был бы выше настоящего.
        средняя = (р["source_in"] * (1 - р["fee_share"])) / р["source_out"]
        if р["bin_price"] >= средняя * 0.999999:
            dl_хуже += 1
    checks.append((f"DLMM: минимум выхода посчитан по {dl_ок} живым сделкам "
                   f"(шагов корзин из файла {взято_dlmm}), отказов "
                   f"{len(dl_отказы)} {sorted(set(dl_отказы))[:2]}",
                   dl_ок >= 8 and взято_dlmm >= 8))
    checks.append((f"DLMM: наша цена не оптимистичнее средней цены источника "
                   f"({dl_хуже} из {dl_ок})", dl_ок and dl_хуже == dl_ок))
    обр_dl = load_samples(DLMM)
    if обр_dl:
        s0 = обр_dl[0]
        t0 = extract_template(s0["tx"], DLMM, s0["pool_vault"])
        пул0 = t0["accounts"][0]
        был = СТУПЕНИ_DLMM.pop(пул0, None)
        НУЖНЫ_ПУЛЫ_DLMM.discard(пул0)
        без_шага = min_out_from_reserves(t0, s0["tx"], 10_000_000, 0.35)
        checks.append((f"DLMM без прочитанного шага корзины -- отказ: "
                       f"{str(без_шага.get('why_not'))[:60]}",
                       без_шага.get("ok") is False
                       and пул0 in НУЖНЫ_ПУЛЫ_DLMM))
        if был is not None:
            СТУПЕНИ_DLMM[пул0] = был
        НУЖНЫ_ПУЛЫ_DLMM.discard(пул0)
        # НЕВЕРНЫЙ ШАГ КОРЗИНЫ модель обязана отвергнуть: средняя цена сделки
        # источника перестаёт лежать между ценами его корзин.
        неверный = dlmm_min_out(t0, s0["tx"], 10_000_000, 0.35, шаг_корзины=20)
        checks.append((f"DLMM при неверном шаге корзины -- отказ: "
                       f"{str(неверный.get('why_not'))[:60]}",
                       неверный.get("ok") is False))
        # Событие без совпадения с хранилищами -- отказ (роутер прошёл пул дважды).
        tx_чужое = json.loads(json.dumps(s0["tx"]))
        for г in ((tx_чужое.get("meta") or {}).get("innerInstructions") or []):
            г["instructions"] = [их for их in (г.get("instructions") or [])
                                 if событие_dlmm(b58decode(их.get("data") or ""))
                                 is None]
        нет_события = min_out_from_reserves(t0, tx_чужое, 10_000_000, 0.35)
        checks.append((f"DLMM без события по нашему пулу -- отказ: "
                       f"{str(нет_события.get('why_not'))[:60]}",
                       нет_события.get("ok") is False))
    # ---- кривая pump.fun: только денежный путь (цена, минимум, предел траты)
    pf_n = pf_exact = 0
    pf_откл = []
    for s in load_samples(BONDING):
        ev = pump_trade_event(s["tx"], s.get("mint"))
        if not ev:
            continue
        pf_n += 1
        vs0, vt0 = ev["virtual_before"]
        пред = vt0 * ev["sol_amount"] // (vs0 + ev["sol_amount"])
        pf_exact += пред == ev["token_amount"]
        pf_откл.append(abs(пред - ev["token_amount"]) / ev["token_amount"])
    # Точно в токен -- у разновидности "точный выход"; у "точного входа"
    # программа округляет иначе, и отклонение измерено, а не допущено на глаз.
    checks.append((f"кривая pump.fun: событие воспроизводит сделку источника "
                   f"(сделок {pf_n}, точно в токен {pf_exact}, макс. отклонение "
                   f"{max(pf_откл) if pf_откл else None})",
                   pf_n >= 6 and pf_exact >= 5 and pf_откл and max(pf_откл) < 1e-6))
    # Трата: подбор до кривой обязан быть ПЛОТНЫМ снизу и не вылезать сверху.
    tight = []
    for bps, cbps in ((95, 30), (100, 0), (0, 0), (500, 250)):
        for budget in (10_000_000, 1_000_000, 123_456_789, 5_001):
            net = pump_net_to_curve(budget, bps, cbps)
            всего = lambda n: n + -(-n * bps // 10_000) + -(-n * cbps // 10_000)  # noqa: E731
            tight.append(net > 0 and всего(net) <= budget < всего(net + 1))
    checks.append((f"трата до кривой: комиссии вверх, предел не превышен, плотно снизу "
                   f"({sum(tight)} из {len(tight)})", all(tight)))
    # Сборка покупки: нативная котировка -- ни обёртки SOL, ни ATA котировки;
    # аргументы инструкции -- (минимум токенов, предел траты), и не наоборот.
    pf_build = []
    for s in load_samples(BONDING):
        tpl = extract_template(s["tx"], BONDING, s["pool_vault"])
        if not tpl["ok"]:
            continue
        mo = min_out_from_reserves(tpl, s["tx"], 10_000_000, 0.35)
        if not mo["ok"]:
            # Цены нет только там, где котировка не в SOL (см. проверку
            # монотонности ниже): собирать покупку без минимума нельзя, и
            # такая сделка полосе не годится. Это не провал сборщика, но и
            # молча пропускать нельзя -- отказ обязан быть назван словами.
            mv_н = mints_and_vaults(tpl, s["tx"])
            не_sol = bool(mv_н) and mv_н["quote_mint"] not in (C.NATIVE_QUOTE, C.WSOL)
            pf_build.append((s.get("mint"), не_sol and bool(mo.get("why_not"))))
            continue
        kp = Keypair()
        me = str(kp.pubkey())
        # Запоминать нечего: место 21 выводится (см. assoc_uva_вывод).
        # Порядок аргументов -- по разновидности: "точный выход" это
        # (минимум токенов, предел траты), "точный вход" -- (трата, минимум).
        ожид = ((mo["min_out"], 10_000_000) if tpl.get("exact_out")
                else (10_000_000, mo["min_out"]))
        ix = swap_instruction(tpl, s["tx"], me, ожид[0], ожид[1])
        got = [str(m.pubkey) for m in ix.accounts]
        подставлено = {i for i, (g, w) in enumerate(zip(got, tpl["accounts"])) if g != w}
        args = struct.unpack("<QQ", bytes(ix.data)[8:24])
        b = build_buy(tpl, s["tx"], user=me, payer=me, amount_in=10_000_000,
                      min_out=mo["min_out"], cu_price_micro=10_000, tip=None)
        raw = base64.b64decode(b["tx_base64"])
        # ЕСТЬ ЛИ ОБЁРТКА SOL -- по СЧЕТАМ собранной транзакции, а не поиском
        # строки в байтах: адреса в транзакции лежат 32 байтами, и поиск
        # "So111..." по сырым байтам не находил ничего никогда, то есть
        # проверка обёртки до 27.09 была пустой.
        ключи_сборки = {str(k) for k in
                        VersionedTransaction.from_bytes(raw).message.account_keys}
        # НАШИ МЕСТА И ОБЁРТКА SOL -- ПО РАЗНОВИДНОСТИ. У 18-счётной котировка
        # нативная: ни ATA котировки, ни обёртки. У 27-счётной котировка --
        # отдельный минт (в живых сделках WSOL), значит обёртка ОБЯЗАНА быть, и
        # наши счета стоят на 13/14/15/20. Спутать их -- подписать покупку с
        # чужими счетами, поэтому проверяется каждая разновидность отдельно.
        сп = spec_of(tpl)
        # МЕСТО СВЯЗАННОГО НАКОПИТЕЛЯ -- ТОЖЕ НАШЕ. Прежде оно сюда не входило, и
        # проверка это терпела лишь потому, что в память фикстура кладбила
        # накопитель ИСТОЧНИКА: адрес не менялся, и место не считалось
        # подставленным. Теперь место 21 выводится из НАШЕГО накопителя, адрес
        # честно другой -- и проверка обязана ждать его среди наших.
        наши_места = set(сп["user"]) | {i for i, _, _ in сп["user_ata"]} \
            | {i for i, _ in сп["pda"]}
        if сп.get("assoc_uva"):
            наши_места.add(сп["assoc_uva"][0])
        нативная = bool(сп.get("native_quote"))
        обёртка_нужна = (not нативная) and b["quote_mint"] == C.WSOL
        pf_build.append((s.get("mint"), подставлено == наши_места
                         and args == ожид
                         and bytes(ix.data)[:8].hex() in BONDING_DISCS
                         and (b["quote_mint"] == C.NATIVE_QUOTE if нативная
                               else b["quote_mint"] != C.NATIVE_QUOTE)
                         and ((C.WSOL in ключи_сборки) if обёртка_нужна
                               else (C.WSOL not in ключи_сборки))
                         and 0 < mo["min_out"] < mo["expected_out"]
                         and mo["sol_to_curve"] < 10_000_000
                         and len(ix.accounts) == сп["n_accounts"]))
    # --- РАСПАКОВКА WSOL В ТОЙ ЖЕ ТРАНЗАКЦИИ (слово владельца 01.10, вечер).
    # Измерено на живой покупке полосы tvwftKB3 (15:58:39Z, программа кривой
    # 6EF8, сделка 0.1): обёртка была, а своп взял SOL НАТИВНО -- и 0.1 остались
    # лежать на счёте WSOL. Поэтому закрытие обязано стоять в той же сборке.
    закр_проб = []
    for s in load_samples(BONDING):
        tpl = extract_template(s["tx"], BONDING, s["pool_vault"])
        if not tpl.get("ok"):
            continue
        me_ = tpl["accounts"][spec_of(tpl)["user"][0]]
        mo_ = min_out_from_reserves(tpl, s["tx"], 10_000_000, 0.35)
        if not mo_.get("ok"):
            continue
        # Запоминать нечего: место 21 выводится (см. assoc_uva_вывод).
        б_ = build_buy(tpl, s["tx"], user=me_, payer=me_, amount_in=10_000_000,
                        min_out=mo_["min_out"], tip=None)
        б_нет = build_buy(tpl, s["tx"], user=me_, payer=me_, amount_in=10_000_000,
                           min_out=mo_["min_out"], tip=None, close_wsol=False)
        обёрнут = bool(б_.get("wrapped_wsol"))
        # ЗАКРЫТИЕ ЕСТЬ ТОГДА И ТОЛЬКО ТОГДА, КОГДА БЫЛА ОБЁРТКА, и оно ровно
        # одно: лишнее закрытие чужого счёта -- это чужие деньги.
        закр_проб.append((
            б_["closed_wsol"] == обёрнут
            and б_нет["closed_wsol"] is False
            and (б_["n_instructions"] == б_нет["n_instructions"] + (1 if обёрнут else 0))
            and (б_["wrapped_wsol"] == ata(me_, C.WSOL, spec_of(tpl).get("quote_program")
                                            or TOKEN_PROGRAM)
                  if обёрнут else б_["wrapped_wsol"] is None)))
    checks.append((f"распаковка WSOL: закрытие ровно там, где была обёртка "
                    f"({sum(1 for o in закр_проб if o)} из {len(закр_проб)})",
                    len(закр_проб) >= 2 and all(закр_проб)))

    checks.append((f"покупка на кривой: подставлены только наши счета по разновидности, "
                   f"аргументы по разновидности, обёртка SOL там, где котировка минтом "
                   f"({sum(1 for _, o in pf_build if o)} из {len(pf_build)})",
                   len(pf_build) >= 4 and all(o for _, o in pf_build)))
    # РАЗНОВИДНОСТИ: обе живые должны и извлекаться, и собираться, и данные
    # нашей сборки при аргументах источника должны совпасть с его данными
    # байт в байт -- это и есть доказательство, что мы поняли инструкцию.
    по_видам: dict = {}
    for s in load_samples(BONDING):
        tpl = extract_template(s["tx"], BONDING, s["pool_vault"])
        if not tpl.get("ok"):
            continue
        ix_ = swap_instruction(tpl, s["tx"], tpl["accounts"][spec_of(tpl)["user"][0]],
                               tpl["arg0"], tpl["arg1"])
        б = по_видам.setdefault(tpl["ix"], {"n": 0, "байт_в_байт": 0, "точный_выход": tpl["exact_out"]})
        б["n"] += 1
        б["байт_в_байт"] += bytes(ix_.data) == tpl["data"]
    checks.append(("разновидности кривой: " + json.dumps(по_видам, ensure_ascii=False),
                   len(по_видам) >= 2 and all(v["n"] == v["байт_в_байт"] for v in по_видам.values())
                   and all(v["n"] >= 1 for v in по_видам.values())))
    # Больше траты -- больше токенов, и минимум всегда ниже ожидания.
    #
    # ЦЕНА ТРЕБУЕТСЯ ТАМ, ГДЕ КОТИРОВКА В SOL. У кривой встречаются сделки с
    # котировкой ЧУЖИМ токеном (образец HzfxBXKq: котировка HiMSSzz…) -- у них
    # раскладка события другая, событие не разбирается, и цены у нас нет. Полоса
    # такие сигналы всё равно не берёт (её правило -- один шаг с котировкой SOL),
    # поэтому проверка требует ЧЕСТНОГО ОТКАЗА с причиной, а не числа: выдать
    # там цену значило бы посчитать её по неизвестной раскладке.
    mono = []
    for s in load_samples(BONDING):
        tpl = extract_template(s["tx"], BONDING, s["pool_vault"])
        if not tpl["ok"]:
            continue
        mv_ = mints_and_vaults(tpl, s["tx"])
        котировка_sol = bool(mv_) and mv_["quote_mint"] in (C.NATIVE_QUOTE, C.WSOL)
        outs = [min_out_from_reserves(tpl, s["tx"], a, 0.35) for a in (1_000_000, 10_000_000, 100_000_000)]
        if not all(o["ok"] for o in outs):
            # Отказ годится только при котировке НЕ в SOL и только со словами.
            mono.append((not котировка_sol)
                        and all(str(o.get("why_not") or "") for o in outs))
            continue
        mono.append(котировка_sol
                    and all(outs[i]["expected_out"] < outs[i + 1]["expected_out"] for i in (0, 1))
                    and all(o["min_out"] < o["expected_out"] for o in outs))
    checks.append((f"цена кривой растёт с тратой, минимум ниже ожидания ({sum(mono)} из {len(mono)})",
                   len(mono) >= 4 and all(mono)))

    # ---------------------------------------------------- DAMM v2: цена по событию
    # Это ДЕНЕЖНЫЙ путь: по этим числам полоса считает минимум выхода. Проверки:
    #   (1) решённая кривая воспроизводит выход самой сделки источника;
    #   (2) наше ожидание НИЖЕ наивной оценки по соотношению -- иначе мы считали
    #       бы, что после покупки источника цена не сдвинулась;
    #   (3) больше траты -- больше выхода, минимум ниже ожидания;
    #   (4) событие берётся по НАШЕМУ пулу, чужое не годится;
    #   (5) комиссия пула видна в ответе (у свежих пулов Meteora она бывает
    #       десятками процентов -- решение "дорого" принимает полоса).
    d2_модель, d2_ниже, d2_моно, d2_комиссии = [], [], [], []
    for s_ in load_samples(DAMM2):
        tpl = extract_template(s_["tx"], DAMM2, s_["pool_vault"])
        if not tpl.get("ok"):
            continue
        r1 = min_out_from_reserves(tpl, s_["tx"], 10_000_000, 0.35)
        if not r1.get("ok"):
            d2_модель.append(False)
            continue
        d2_модель.append(r1["model_error"] < 1e-7)
        наив = r1["source_out"] * 10_000_000 / r1["source_in"]
        d2_ниже.append(0 < r1["expected_out"] < наив)
        d2_комиссии.append(0.0 <= r1["fee_share"] < 0.60)
        ряд = [min_out_from_reserves(tpl, s_["tx"], a, 0.35)
               for a in (1_000_000, 10_000_000, 100_000_000)]
        d2_моно.append(all(x.get("ok") for x in ряд)
                       and all(ряд[i]["expected_out"] < ряд[i + 1]["expected_out"]
                               for i in (0, 1))
                       and all(0 < x["min_out"] < x["expected_out"] for x in ряд))
    checks.append((f"DAMM v2: кривая по событию воспроизводит сделку источника "
                   f"({sum(d2_модель)} из {len(d2_модель)})",
                   len(d2_модель) >= 10 and all(d2_модель)))
    checks.append((f"DAMM v2: наше ожидание ниже наивной оценки по соотношению "
                   f"({sum(d2_ниже)} из {len(d2_ниже)})",
                   len(d2_ниже) >= 10 and all(d2_ниже)))
    checks.append((f"DAMM v2: больше траты -- больше выхода, минимум ниже ожидания "
                   f"({sum(d2_моно)} из {len(d2_моно)})",
                   len(d2_моно) >= 10 and all(d2_моно)))
    checks.append((f"DAMM v2: комиссия пула посчитана и названа ({sum(d2_комиссии)} "
                   f"из {len(d2_комиссии)})", len(d2_комиссии) >= 10
                   and all(d2_комиссии)))
    # (4) Чужой пул: событие с другим адресом пула не берётся вовсе.
    чужой = None
    for s_ in load_samples(DAMM2):
        tpl = extract_template(s_["tx"], DAMM2, s_["pool_vault"])
        if not tpl.get("ok"):
            continue
        import c2_cl_quote as CL_  # noqa: PLC0415

        своё = CL_.тело_события(s_["tx"], DAMM2, all_instructions, b58decode,
                                 пул=tpl["accounts"][1])
        не_своё = CL_.тело_события(s_["tx"], DAMM2, all_instructions, b58decode,
                                    пул="11111111111111111111111111111111")
        чужой = (своё is not None and не_своё is None)
        break
    checks.append(("DAMM v2: событие берётся по нашему пулу, чужое не берётся",
                   bool(чужой)))

    # ------------------------------------------------- DBC: цена по событию кривой
    dbc_модель, dbc_ниже, dbc_моно, dbc_ком = [], [], [], []
    for s_ in load_samples(DBC):
        tpl = extract_template(s_["tx"], DBC, s_["pool_vault"])
        if not tpl.get("ok"):
            continue
        r1 = min_out_from_reserves(tpl, s_["tx"], 10_000_000, 0.35)
        if not r1.get("ok"):
            dbc_модель.append(False)
            continue
        dbc_модель.append(r1["model_error"] < 1e-7 and 1.0 < r1["price_shift"] <= 8.0)
        наив = r1["source_out"] * 10_000_000 / r1["source_in"]
        dbc_ниже.append(0 < r1["expected_out"] < наив)
        # Комиссия кривой у обеих живых сделок -- 2 % с точностью до округления
        # вверх (2.0012 %): проверяем именно это узкое окно, а не "какое-нибудь
        # число" -- выход за него значил бы другой разбор события.
        dbc_ком.append(0.0200 <= r1["fee_share"] <= 0.0201)
        ряд = [min_out_from_reserves(tpl, s_["tx"], a, 0.35)
               for a in (1_000_000, 10_000_000, 100_000_000)]
        dbc_моно.append(all(x.get("ok") for x in ряд)
                        and all(ряд[i]["expected_out"] < ряд[i + 1]["expected_out"]
                                for i in (0, 1))
                        and all(0 < x["min_out"] < x["expected_out"] for x in ряд))
    checks.append((f"DBC: кривая по событию воспроизводит сделку источника и сдвиг "
                   f"цены в разумных границах ({sum(dbc_модель)} из {len(dbc_модель)})",
                   len(dbc_модель) >= 2 and all(dbc_модель)))
    checks.append((f"DBC: наше ожидание ниже наивной оценки по соотношению "
                   f"({sum(dbc_ниже)} из {len(dbc_ниже)})",
                   len(dbc_ниже) >= 2 and all(dbc_ниже)))
    checks.append((f"DBC: больше траты -- больше выхода, минимум ниже ожидания "
                   f"({sum(dbc_моно)} из {len(dbc_моно)})",
                   len(dbc_моно) >= 2 and all(dbc_моно)))
    checks.append((f"DBC: комиссия кривой 2 % (с округлением вверх) у обеих живых сделок "
                   f"({sum(dbc_ком)} из {len(dbc_ком)})",
                   len(dbc_ком) >= 2 and all(dbc_ком)))
    # --- СВЯЗАННЫЙ НАКОПИТЕЛЬ 27-счётной кривой (28.09: семь сгоревших покупок).
    # Проверяем три вещи: ключ из ошибки программы читается; при известном ключе
    # он встаёт РОВНО на место 21; при неизвестной котировке сборка отказывает.
    логи_обр = [
        "Program log: Instruction: BuyExactQuoteInV2",
        "Program log: AnchorError caused by account: associated_user_volume_accumulator."
        " Error Code: ConstraintSeeds. Error Number: 2006. Error Message: A seeds"
        " constraint was violated.",
        "Program log: Left:",
        "Program log: 6HX8mmdYYYZ79YMETXcLUydFq9Eef1Dy8SyRZx9Zu2Mh",
        "Program log: Right:",
        "Program log: FJiTxtBCCeQPyXJ1RPbYPaNoM2dSvpwcqvdBaRGNhvu2",
    ]
    checks.append(("кривая V2: ожидаемый накопитель читается из ошибки программы",
                   assoc_uva_из_логов(логи_обр)
                   == "FJiTxtBCCeQPyXJ1RPbYPaNoM2dSvpwcqvdBaRGNhvu2"))
    checks.append(("кривая V2: чужая ошибка ConstraintSeeds ключа не даёт",
                   assoc_uva_из_логов([
                       "Program log: AnchorError caused by account: creator_vault."
                       " Error Code: ConstraintSeeds. Error Number: 2006.",
                       "Program log: Left:", "Program log: 11111111111111111111111111111111",
                       "Program log: Right:", "Program log: So11111111111111111111111111111111111111112",
                   ]) is None))
    # Шаблон 27 счетов: только места, которые нужны подстановке, реальные.
    _наш = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"
    _минт = "7ncdD1BiWSPG6U1SqtuopyLeRdyNSBwt4x18xnL69XMR"
    _wsol = "So11111111111111111111111111111111111111112"
    _счета = [SYSTEM] * 27
    _счета[1], _счета[2] = _минт, _wsol
    _счета[3], _счета[4] = TOKEN_PROGRAM, TOKEN_PROGRAM
    _шаблон = {"program": BONDING, "ix": "c2ab1c46684d5b2f", "accounts": _счета,
                "data": b"\x00" * 25, "writable": {}, "arg0": 1, "arg1": 1}
    _файл = os.path.join(os.path.dirname(_проб_файл), "assoc_proba.json")
    os.environ["BLOOM_ASSOC_UVA_FILE"] = _файл
    assoc_uva_забыть()
    # БЕЗ ЗАПОМНЕННОГО КЛЮЧА СБОРКА ТЕПЕРЬ ИДЁТ -- и это починка, а не
    # послабление. Прежде здесь стоял отказ, потому что ключ умели только
    # вспоминать; 03.10 это дало 16 отказов на новом кошельке полосы. Ключ
    # выводится, и выведенный сверен с ЦЕПЬЮ отдельной проверкой выше.
    _роли_без = user_accounts(_шаблон, {}, _наш)
    checks.append(("кривая V2: БЕЗ запомненного ключа сборка идёт -- место 21 "
                   "выведено",
                   bool(_роли_без)
                   and _роли_без.get(21) == ata(
                       pda([b"user_volume_accumulator", "USER"], _наш, BONDING),
                       _wsol, TOKEN_PROGRAM)))
    assoc_uva_запомнить(_наш, _wsol, "FJiTxtBCCeQPyXJ1RPbYPaNoM2dSvpwcqvdBaRGNhvu2",
                         путь=_файл)
    _роли = user_accounts(_шаблон, {}, _наш)
    checks.append(("кривая V2: известный ключ встаёт на место 21, а место 20 "
                   "остаётся нашим PDA",
                   _роли.get(21) == "FJiTxtBCCeQPyXJ1RPbYPaNoM2dSvpwcqvdBaRGNhvu2"
                   and _роли.get(20) == pda([b"user_volume_accumulator", "USER"],
                                             _наш, BONDING)
                   and _роли.get(13) == _наш))
    # У ДРУГОЙ КОТИРОВКИ -- СВОЙ ВЫВЕДЕННЫЙ КЛЮЧ, и он ДРУГОЙ: накопитель тот
    # же, а ATA считается для своего минта. Прежде здесь был отказ "своего ключа
    # нет", и ровно он и держал полосу на новом кошельке.
    _друг_минт = "XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN"
    _счета[2] = _друг_минт
    _роли_др = user_accounts(dict(_шаблон, accounts=list(_счета)), {}, _наш)
    checks.append(("кривая V2: у другой котировки свой выведенный ключ, и он иной",
                   bool(_роли_др)
                   and _роли_др.get(21) == ata(
                       pda([b"user_volume_accumulator", "USER"], _наш, BONDING),
                       _друг_минт, TOKEN_PROGRAM)
                   and _роли_др.get(21) != _роли_без.get(21)))
    _счета[2] = _wsol
    if _env_было is None:
        os.environ.pop("BLOOM_ASSOC_UVA_FILE", None)
    else:
        os.environ["BLOOM_ASSOC_UVA_FILE"] = _env_было
    assoc_uva_забыть()

    # --- ПОДСТАНОВКА НЫНЕШНИХ МАССИВОВ КОРЗИН DLMM (п.4 слова владельца 29.09).
    # Проверяется на выдуманном шаблоне: адреса тут -- просто строки, никакой
    # цепи функция не касается, а вся её работа -- список счетов и флаги записи.
    _шаб_dlmm = {"program": DLMM, "ix": "swap2",
                  "accounts": ["A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7",
                                "A8", "A9", "A10", "A11", "A12", "A13", "A14",
                                "A15", "МАС1", "МАС2"],
                  "writable": {"A0": False, "МАС1": True, "МАС2": True},
                  "data": b"\x00" * 26, "arg0": 1, "arg1": 2}
    _п1 = шаблон_с_нынешними_массивами(_шаб_dlmm, массивы_шаблона=["МАС1", "МАС2"],
                                        массивы_сейчас=["НОВ1", "НОВ2"])
    checks.append(("DLMM: массивы корзин заменены ровно на своих местах",
                   _п1["ok"] is True and _п1["подставлено"] == 2
                   and _п1["шаблон"]["accounts"][16] == "НОВ1"
                   and _п1["шаблон"]["accounts"][17] == "НОВ2"))
    checks.append(("и остальные 16 счетов не тронуты",
                   _п1["ok"] is True
                   and _п1["шаблон"]["accounts"][:16] == _шаб_dlmm["accounts"][:16]))
    checks.append(("новые массивы записываемые -- иначе программа откажет",
                   _п1["ok"] is True
                   and _п1["шаблон"]["writable"].get("НОВ1") is True
                   and _п1["шаблон"]["writable"].get("НОВ2") is True))
    checks.append(("исходный шаблон НЕ изменён -- у вызывающего он остаётся целым",
                   _шаб_dlmm["accounts"][16] == "МАС1"
                   and _шаб_dlmm["accounts"][17] == "МАС2"
                   and "НОВ1" not in _шаб_dlmm["writable"]))
    _п2 = шаблон_с_нынешними_массивами(_шаб_dlmm, массивы_шаблона=["МАС1", "МАС2"],
                                        массивы_сейчас=["НОВ1"])
    checks.append(("нынешних меньше -- подставлен один, второе место шаблона осталось",
                   _п2["ok"] is True and _п2["подставлено"] == 1
                   and _п2["шаблон"]["accounts"][16] == "НОВ1"
                   and _п2["шаблон"]["accounts"][17] == "МАС2"))
    _п3 = шаблон_с_нынешними_массивами(_шаб_dlmm, массивы_шаблона=["МАС1", "МАС2"],
                                        массивы_сейчас=["НОВ1", "НОВ2", "НОВ3"])
    checks.append(("нынешних больше -- лишний НАЗВАН в не_влезло, а не выброшен молча",
                   _п3["ok"] is True and _п3["подставлено"] == 2
                   and _п3["не_влезло"] == ["НОВ3"]))
    _п4 = шаблон_с_нынешними_массивами(dict(_шаб_dlmm, program=CLMM),
                                        массивы_шаблона=["МАС1"],
                                        массивы_сейчас=["НОВ1"])
    checks.append(("не DLMM -- отказ словами, а не тихая подстановка",
                   _п4["ok"] is False and "только у DLMM" in (_п4["why_not"] or "")))
    _п5 = шаблон_с_нынешними_массивами(_шаб_dlmm, массивы_шаблона=["ЧУЖОЙ"],
                                        массивы_сейчас=["НОВ1"])
    checks.append(("массив шаблона не из этой сделки -- отказ, шаблон не собран",
                   _п5["ok"] is False and _п5["шаблон"] is None
                   and "не от этой сделки" in (_п5["why_not"] or "")))
    checks.append(("пустые списки -- отказ с причиной, а не пустая подстановка",
                   шаблон_с_нынешними_массивами(
                       _шаб_dlmm, массивы_шаблона=[], массивы_сейчас=["Н"])["ok"] is False
                   and шаблон_с_нынешними_массивами(
                       _шаб_dlmm, массивы_шаблона=["МАС1"], массивы_сейчас=[])["ok"] is False))
    # И САМОЕ ГЛАВНОЕ: собранная из поправленного шаблона инструкция несёт
    # нынешние массивы, а данные у неё те же -- счетов не прибавилось.
    checks.append(("порядок мест возрастающий -- массивы не переставляются местами",
                   _п1["места"] == [16, 17]))
    # ОБРЕЗКА ИДЁТ С КОНЦА, И ЭТО ГЛАВНОЕ. Котировка отдаёт массивы по ходу
    # свопа, активный первым; мест у источника бывает меньше, чем нужно сейчас
    # (29.09 -- у 7 из 10 живых покупок). Если обрезать с начала или сортировать
    # по индексу, у программы останется САМЫЙ ДАЛЬНИЙ массив вместо активного.
    _шаб1 = {"program": DLMM, "ix": "swap2",
              "accounts": ["A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8",
                            "A9", "A10", "A11", "A12", "A13", "A14", "A15", "МАС1"],
              "writable": {"МАС1": True}, "data": b"\x00" * 26,
              "arg0": 1, "arg1": 2}
    _п6 = шаблон_с_нынешними_массивами(
        _шаб1, массивы_шаблона=["МАС1"],
        массивы_сейчас=["АКТИВНЫЙ", "ДАЛЬШЕ1", "ДАЛЬШЕ2"])
    checks.append(("одно место и три нынешних -- остаётся АКТИВНЫЙ (первый), "
                   "а не дальний",
                   _п6["ok"] is True and _п6["подставлено"] == 1
                   and _п6["шаблон"]["accounts"][16] == "АКТИВНЫЙ"
                   and _п6["не_влезло"] == ["ДАЛЬШЕ1", "ДАЛЬШЕ2"]))
    # ДОПИСЫВАНИЕ ХВОСТА. Массивы корзин у DLMM -- remaining accounts, и когда
    # мест у источника меньше нужного, недостающие дописываются в конец. Но
    # ТОЛЬКО если места массивов -- настоящий непрерывный хвост.
    _п7 = шаблон_с_нынешними_массивами(
        _шаб1, массивы_шаблона=["МАС1"],
        массивы_сейчас=["АКТИВНЫЙ", "ДАЛЬШЕ1", "ДАЛЬШЕ2"], дописывать=True)
    checks.append(("хвост дописан: три нынешних влезли в одно место плюс два",
                   _п7["ok"] is True and _п7["дописано"] == 2
                   and _п7["не_влезло"] == []
                   and _п7["шаблон"]["accounts"][16:] == ["АКТИВНЫЙ", "ДАЛЬШЕ1",
                                                            "ДАЛЬШЕ2"]))
    checks.append(("дописанные массивы записываемые -- иначе программа откажет",
                   _п7["ok"] is True
                   and _п7["шаблон"]["writable"].get("ДАЛЬШЕ1") is True
                   and _п7["шаблон"]["writable"].get("ДАЛЬШЕ2") is True))
    checks.append(("исходный шаблон дописыванием не испорчен",
                   _шаб1["accounts"][-1] == "МАС1" and len(_шаб1["accounts"]) == 17))
    _шаб_не_хвост = {"ok": True, "program": DLMM, "ix": "swap2",
                      "accounts": ["ПУЛ", "A1", "A2", "A3", "A4", "A5", "A6", "A7",
                                    "A8", "A9", "A10", "A11", "A12", "A13", "A14",
                                    "МАС1", "СЛУЖЕБНЫЙ"],
                      "writable": {"МАС1": True}, "data": b"\x00" * 26,
                      "arg0": 1, "arg1": 2}
    _п8 = шаблон_с_нынешними_массивами(
        _шаб_не_хвост, массивы_шаблона=["МАС1"],
        массивы_сейчас=["АКТИВНЫЙ", "ДАЛЬШЕ1"], дописывать=True)
    checks.append(("массив НЕ последний счёт -- не дописываем, остаётся не_влезло",
                   _п8["ok"] is True and _п8["дописано"] == 0
                   and _п8["хвост"] is False
                   and _п8["не_влезло"] == ["ДАЛЬШЕ1"]))
    _п9 = шаблон_с_нынешними_массивами(
        _шаб1, массивы_шаблона=["МАС1"],
        массивы_сейчас=["АКТИВНЫЙ", "ДАЛЬШЕ1"], дописывать=False)
    checks.append(("без разрешения дописывать -- поведение прежнее",
                   _п9["ok"] is True and _п9["дописано"] == 0
                   and _п9["не_влезло"] == ["ДАЛЬШЕ1"]))

    bad_n = 0
    # ---- СВЯЗАННЫЙ НАКОПИТЕЛЬ ВЫВОДИТСЯ, И СВЕРЕН С ЦЕПЬЮ ----------------
    # ЗАЧЕМ ЭТИ ПРОВЕРКИ. До 03.10 место 21 брали ТОЛЬКО из запомненного файла,
    # а файл помнит ПО КОШЕЛЬКУ и пополняется из ошибки программы: после
    # переезда полосы на новый кошелёк он пуст, и 03.10 это дало 16 отказов
    # "роли счетов не восстановились" у источника группы cand1_03 (билет 0.5).
    #
    # СВЕРКА ИМЕННО С ЦЕПЬЮ, А НЕ С САМОЙ ФОРМУЛОЙ. Проверить вывод его же
    # выводом значит не проверить ничего. Берётся НАША живая покупка
    # Nhe6axYT1eHBUBdAmQGg… (27 счетов, слот 452705255, кошелёк полосы на месте
    # 13) из data/krivaya_27_mesta.json -- то есть байты, которые уже прошли
    # программу: если формула не та, программа бы их не приняла.
    _обр = None
    try:
        import json as _js  # noqa: PLC0415
        import pathlib as _pl  # noqa: PLC0415

        for _к in ("data/krivaya_27_mesta.json", "../data/krivaya_27_mesta.json"):
            _п = _pl.Path(__file__).resolve().parent.parent / _к.split("/", 1)[-1] \
                 if _к.startswith("../") else _pl.Path(_к)
            if _п.exists():
                _обр = _js.loads(_п.read_text(encoding="utf-8"))
                break
    except Exception:  # noqa: BLE001
        _обр = None
    if not (_обр or {}).get("покупка", {}).get("accounts"):
        print("    [ждёт] образца живой 27-счётной покупки нет рядом -- сверка "
              "вывода накопителя с ЦЕПЬЮ здесь непроверяема")
    else:
        _acc = _обр["покупка"]["accounts"]
        _спец = BONDING_DISCS["c2ab1c46684d5b2f"]["spec"]
        _кош = _acc[_спец["user"][0]]
        checks.append(("образец -- 27-счётная покупка кривой нашим кошельком",
                       len(_acc) == 27
                       and _обр["покупка"]["disc"] == "c2ab1c46684d5b2f"))
        import tempfile as _tf3  # noqa: PLC0415
        _файл_св = os.path.join(_tf3.mkdtemp(), "sverka.json")
        _env_св = os.environ.get("BLOOM_ASSOC_UVA_FILE")
        os.environ["BLOOM_ASSOC_UVA_FILE"] = _файл_св
        assoc_uva_забыть()
        _р = assoc_uva_вывод(_кош, _спец, _acc, BONDING)
        checks.append(("накопитель объёма выведен ровно как в ЦЕПИ (место 20)",
                       pda([СЕМЯ_НАКОПИТЕЛЯ, "USER"], _кош, BONDING)
                       == _acc[_место_накопителя(_спец)]))
        checks.append(("СВЯЗАННЫЙ накопитель выведен ровно как в ЦЕПИ (место 21)",
                       _р["выведен"] == _acc[_спец["assoc_uva"][0]]))
        checks.append(("и запомненного для него нет -- сверка молчит, а не врёт",
                       _р["сошлось"] is None and _р["ok"] is True))
        # ПРОГРАММА ТОКЕНА -- МЕСТА 4, А НЕ 3. У пампового минта место 3 это
        # Token-2022 (программа БАЗЫ), и с ней выходит другой адрес: проверяем,
        # что раскладка называет именно место котировки.
        checks.append(("ATA берёт программу котировки (место 4), не базы (место 3)",
                       _спец["assoc_uva"][2] == 4
                       and ata(_acc[_место_накопителя(_спец)], _acc[2], _acc[3])
                       != _acc[_спец["assoc_uva"][0]]))
        # ПАРЫ С ХОСТА -- СВЕРКА, КОТОРУЮ ВЕЛЕЛ ВЛАДЕЛЕЦ (03.10, п.4): вывести
        # формулой каждую пару, которая в bonding_v2_assoc.json УЖЕ лежит, и
        # сравнить. Прогон живучести 12:51Z отдал файл целиком, обе пары
        # сошлись, и ВТОРАЯ закрывает случай, которого в проверке выше нет:
        # котировка XsCPL9dN… -- минт Token-2022, и её ATA считается ДРУГОЙ
        # программой. Ключи в файл попали из ошибки программы, то есть названы
        # цепью; значит это сверка с цепью, а не с самой формулой.
        _ПАРЫ_ХОСТА = {
            "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x": {
                ("So11111111111111111111111111111111111111112", TOKEN_PROGRAM):
                    "FJiTxtBCCeQPyXJ1RPbYPaNoM2dSvpwcqvdBaRGNhvu2",
                ("XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN", TOKEN_2022):
                    "4NLZ5bLDDB7CckoqqFoMopxk59S63jkpd67AK4JaQqwy",
            },
        }
        _сошлось_х = _всего_х = 0
        for _к_х, _п_х in _ПАРЫ_ХОСТА.items():
            _нак_х = pda([СЕМЯ_НАКОПИТЕЛЯ, "USER"], _к_х, BONDING)
            for (_м_х, _пр_х), _помним_х in _п_х.items():
                _всего_х += 1
                _сошлось_х += ata(_нак_х, _м_х, _пр_х) == _помним_х
        checks.append((f"вывод сошёлся с КАЖДОЙ запомненной парой хоста "
                       f"({_сошлось_х} из {_всего_х})",
                       _всего_х == 2 and _сошлось_х == _всего_х))
        # И ИМЕННО ПРОГРАММА РЕШАЕТ: та же пара с классической программой даёт
        # ДРУГОЙ адрес. Без места программы в раскладке мы подписали бы его.
        checks.append(("у котировки Token-2022 классическая программа даёт ДРУГОЙ "
                       "адрес -- место программы в раскладке обязательно",
                       ata(pda([СЕМЯ_НАКОПИТЕЛЯ, "USER"],
                               "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x",
                               BONDING),
                           "XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN",
                           TOKEN_PROGRAM)
                       != "4NLZ5bLDDB7CckoqqFoMopxk59S63jkpd67AK4JaQqwy"))
        checks.append(("место накопителя найдено ПО СЕМЕНИ, а не числом",
                       _место_накопителя(_спец) == 20
                       and _место_накопителя({"pda": []}) is None))
        # РАСХОЖДЕНИЕ ВЫВОДА И ПАМЯТИ -- ОТКАЗ, А НЕ ВЫБОР ОДНОГО ИЗ ДВУХ.
        assoc_uva_запомнить(_кош, _acc[2], "11111111111111111111111111111111",
                             путь=_файл_св)
        assoc_uva_забыть()
        _р2 = assoc_uva_вывод(_кош, _спец, _acc, BONDING)
        checks.append(("битая память против вывода -- сверка говорит РАСХОЖДЕНИЕ",
                       _р2["сошлось"] is False and _р2["ok"] is False
                       and "РАСХОДЯТСЯ" in (_р2["why_not"] or "")))
        # ix -- ДИСКРИМИНАТОР: spec_of ищет раскладку кривой именно по нему
        # (BONDING_DISCS.get(tpl["ix"])), и со словом "buy" раскладка 27 счетов
        # не нашлась бы, а проверка молча мерила бы 18-счётную.
        # ix -- ДИСКРИМИНАТОР: spec_of ищет раскладку кривой именно по нему
        # (BONDING_DISCS.get(tpl["ix"])), и со словом "buy" раскладка 27 счетов
        # не нашлась бы, а проверка молча мерила бы 18-счётную.
        _шб = {"program": BONDING, "accounts": list(_acc), "writable": {},
                "ix": "c2ab1c46684d5b2f", "data": b"\x00" * 25,
                "arg0": 1, "arg1": 1}
        # РАСХОЖДЕНИЕ ПРОВЕРЯЕМ НА ЧУЖОМ КОШЕЛЬКЕ: у своего (место 13) ветка
        # "пересборка сделки источника" берёт место 21 как есть и до сверки не
        # доходит -- проверка прошла бы, ничего не проверив.
        _чуж_кош = "21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w"
        assoc_uva_запомнить(_чуж_кош, _acc[2],
                             "11111111111111111111111111111111", путь=_файл_св)
        assoc_uva_забыть()
        checks.append(("и сборка при расхождении НЕ собирается (пустые роли)",
                       user_accounts(_шб, {}, _чуж_кош) == {}))
        # ПАМЯТЬ СОВПАЛА -- ИДЁМ, И ИМЕННО ВЫВЕДЕННЫМ.
        assoc_uva_запомнить(_кош, _acc[2], _acc[_спец["assoc_uva"][0]],
                             путь=_файл_св)
        assoc_uva_забыть()
        _р3 = assoc_uva_вывод(_кош, _спец, _acc, BONDING)
        checks.append(("память совпала с выводом -- сверка пройдена",
                       _р3["сошлось"] is True and _р3["ok"] is True))
        # ПЕРЕСБОРКА СДЕЛКИ ИСТОЧНИКА не меняется: его накопитель остаётся свой.
        _рол = user_accounts(_шб, {}, _кош)
        checks.append(("пересборка сделки самого источника место 21 НЕ трогает",
                       _рол.get(_спец["assoc_uva"][0]) == _acc[_спец["assoc_uva"][0]]))
        # ЧУЖОЙ КОШЕЛЁК БЕЗ ПАМЯТИ -- теперь СОБИРАЕТСЯ (это и есть починка).
        assoc_uva_забыть()
        import tempfile as _tf2  # noqa: PLC0415
        _пустой = os.path.join(_tf2.mkdtemp(), "pusto.json")
        _было2 = os.environ.get("BLOOM_ASSOC_UVA_FILE")
        os.environ["BLOOM_ASSOC_UVA_FILE"] = _пустой
        assoc_uva_забыть()
        _чужой = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"
        if _чужой == _кош:
            _чужой = "21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w"
        _рол2 = user_accounts(_шб, {}, _чужой)
        checks.append(("кошелёк БЕЗ запомненной пары теперь собирается -- это и "
                       "есть починка 16 отказов",
                       bool(_рол2)
                       and _рол2.get(_спец["assoc_uva"][0])
                       == ata(pda([СЕМЯ_НАКОПИТЕЛЯ, "USER"], _чужой, BONDING),
                               _acc[2], _acc[4])))
        if _env_св is None:
            os.environ.pop("BLOOM_ASSOC_UVA_FILE", None)
        else:
            os.environ["BLOOM_ASSOC_UVA_FILE"] = _env_св
        assoc_uva_забыть()

    # --- ПОРЯДОК РЕЗЕРВОВ: [КОТИРОВКА, БАЗА] У ВСЕХ ТРЁХ (диф №5 Code-3,
    # слово владельца 03.10). Правило тонкого пула в bloom_lane_two_step читает
    # рез[0] КАК КОТИРОВКУ, и у LaunchLab порядок был обратным -- правило было
    # слепым, а pool_reserve_quote_raw писал резерв базы. Проверка стоит на
    # ЖИВОМ образце и сверяет порядок с двумя соседями: правка в любом из трёх
    # мест теперь ломает её громко, а не меняет поведение полосы молча.
    _ll_ф = (Path(__file__).resolve().parent.parent / "data" / "c2_pool_samples"
             / "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj.json")
    _ll_ев = None
    if _ll_ф.exists():
        try:
            _ll_обр = json.loads(_ll_ф.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            _ll_обр = []
        _ll_tx = (_ll_обр[0] or {}).get("tx") if _ll_обр else None
        _ll_ев = launchlab_event(_ll_tx) if _ll_tx else None
    checks.append(("живой образец LaunchLab на месте и событие разобралось",
                   bool(_ll_ев)))
    if _ll_ев:
        _ll_мо = launchlab_min_out(_ll_tx, 10_000_000, 0.3)
        _рез = _ll_мо.get("virtual_reserves_after") or []
        _кот = _ll_ев["virtual_quote"] + _ll_ев["real_quote_after"]
        _баз = _ll_ев["virtual_base"] - _ll_ев["real_base_after"]
        checks.append(("LaunchLab: рез[0] -- КОТИРОВКА, а не база",
                       len(_рез) == 2 and _рез[0] == _кот == 499800652))
        checks.append(("LaunchLab: рез[1] -- база",
                       len(_рез) == 2 and _рез[1] == _баз == 661046006523532))
        # ЗАМЕР ВОСПРОИЗВЕДЁН СВОИМ СЧЁТОМ, А НЕ ПЕРЕПИСАН. По цене статичного
        # шаблона ноги (price_sol 0.008361, q_dec 6) котировка даёт 4.1788
        # SOL-экв., база -- 5 527 005.66. У Code-3 во второй клетке стоит
        # 5 527 021: расхождение 15 единиц на пятом знаке от округления цены, и
        # я ставлю СВОЁ число, а не его. Для правила это без разницы: порог
        # тонкого пула 30, и разница между 4.18 и 5.5 млн -- шесть порядков.
        _цена, _qdec = 0.008361, 6
        checks.append((
            "правило тонкого пула видит 4.18 SOL-экв. (пул тонкий), а не 5.5 млн",
            abs(_рез[0] / 10 ** _qdec * _цена - 4.1788) < 0.001
            and _рез[0] / 10 ** _qdec * _цена < 30.0
            and 5.5e6 < _рез[1] / 10 ** _qdec * _цена < 5.6e6))
    # СОСЕДИ: порядок -- это именно текст возврата, собрать событие кривой тут
    # нечем, поэтому сверяется текст.
    _ист = Path(__file__).resolve().read_text(encoding="utf-8")
    checks.append((
        "кривая отдаёт [virtual_sol_reserves, virtual_token_reserves]",
        '"virtual_reserves_after": [ev["virtual_sol_reserves"], '
        'ev["virtual_token_reserves"]]' in _ист))
    checks.append((
        "min_out_from_reserves отдаёт [x1, y1] -- вход-котировка, выход-база",
        '"reserves_after": [x1, y1]' in _ист
        and "expected = D(y1) * a_eff / (D(x1) + a_eff)" in _ист))
    # КОТИРОВКА НЕ WSOL -- ОТКАЗ ДО ПОДПИСИ. Собрать такое нельзя по сути:
    # полоса платит солами. Проверяется настоящей сборкой по IDL на шаблоне,
    # где котировка подменена, -- а не текстом.
    _сб_idl = _сборщик_idl()
    if _сб_idl is not None:
        try:
            _имена_v3 = [а["name"] for а in _сб_idl.zagruzit_idl()["pump"]["ix"]
                         ["buy_exact_quote_in_v3"]["accounts"]]
            _сч = ["11111111111111111111111111111111"] * len(_имена_v3)
            _сч[_имена_v3.index("base_mint")] = \
                "So11111111111111111111111111111111111111112"
            _сч[_имена_v3.index("quote_mint")] = \
                "CARDSccUMFKohQ5h1Kw6bjnMuaDfKtyzNHBP9R9N1C2x"
            _сч[_имена_v3.index("base_token_program")] = TOKEN_PROGRAM
            _сч[_имена_v3.index("buyback_fee_recipient")] = \
                "11111111111111111111111111111112"
            _шб = {"po_idl": "buy_exact_quote_in_v3", "accounts": _сч,
                   "buyback_fee_recipient": "11111111111111111111111111111112"}
            _пойм = None
            try:
                инструкция_кривой_по_idl(_шб, {}, "11111111111111111111111111111113",
                                          1, 1)
            except ValueError as _e:
                _пойм = str(_e)
            checks.append((
                "ДОКАЗАННЫЙ КРАСНЫЙ: котировка кривой не WSOL -- отказ ДО "
                "подписи, с минтом котировки в причине (прежде такая сборка "
                "уходила в сеть и стоила чаевых)",
                bool(_пойм) and "не WSOL" in _пойм and "CARDS" in _пойм))
        except Exception as _e_кот:  # noqa: BLE001
            checks.append((f"проверка котировки не состоялась: "
                           f"{type(_e_кот).__name__}", False))

    for name, ok in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}")
        bad_n += (not ok)
    print(f"самопроверка c2_swap_build: {len(checks) - bad_n}/{len(checks)} пройдено")
    return 0 if bad_n == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    raise SystemExit(self_test() if a.self_test else 0)
