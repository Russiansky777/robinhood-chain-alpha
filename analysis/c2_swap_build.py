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
    return out


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


def swap_instruction(tpl: dict, tx: dict, user: str, arg0: int, arg1: int,
                     keep_source_ix: bool = False) -> Instruction:
    subs = user_accounts(tpl, tx, user)
    if not subs:
        raise ValueError("роли счетов не восстановились")
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


def build_buy(tpl: dict, tx: dict, *, user: str, payer: str, amount_in: int, min_out: int,
              cu_units: int = 200_000, cu_price_micro: int = 0, tip: tuple | None = None,
              wrap_sol: bool = True, nonce: tuple | None = None,
              tip_first: bool = False) -> dict:
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
    if mv["quote_mint"] == C.WSOL and wrap_sol and amount_in:
        wsol_ata = ata(user, C.WSOL, mv["quote_program"])
        ixs += [sol_transfer(user, wsol_ata, amount_in), sync_native(wsol_ata)]
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
    msg = MessageV0.try_compile(Pubkey.from_string(payer), ixs, [], Hash.default())
    n_sig = msg.header.num_required_signatures
    vtx = VersionedTransaction.populate(msg, [Signature.default()] * n_sig)
    raw = bytes(vtx)
    return {"tx_base64": base64.b64encode(raw).decode(), "size": len(raw),
            "n_instructions": len(ixs), "quote_mint": mv["quote_mint"], "base_mint": mv["base_mint"]}


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
    из_ = CL.минимум_clmm(тело=тело, вход_источника=вход, выход_источника=выход,
                           аргумент_входа=tpl.get("arg0") or 0,
                           наш_вход=amount_in, проскальзывание=slippage,
                           ставка_1e6=ставка_1e6, шаг_тика=шаг_тика)
    if из_.get("ok"):
        из_["source_in"] = вход
        из_["source_out"] = выход
        из_["pool"] = пул
        # Счёт amm_config -- чтобы вызывающий знал, чью ставку кэшировать.
        из_["amm_config"] = (tpl["accounts"][1] if len(tpl["accounts"]) > 1
                             else None)
    return из_


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
        return {"ok": False, "why_not": "сосредоточенная ликвидность: резервы цену не дают"}
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
    a_eff = D(amount_in) * f
    expected = D(y1) * a_eff / (D(x1) + a_eff)
    mn = int(expected * D(1 - slippage))
    return {"ok": True, "fee_factor": float(f), "reserves_after": [x1, y1],
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
        ev["virtual_base"] - ev["real_base_after"], ev["virtual_quote"] + ev["real_quote_after"]],
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

        # --- МИНИМУМ ВЫХОДА CLMM. Ставку комиссии пула модель НЕ УГАДЫВАЕТ:
        # здесь она берётся из самих образцов -- у одного и того же счёта
        # amm_config доля комиссии, решённая по сделке, повторяется ТОЧНО на
        # разных пулах, и это повторяющееся значение и есть ставка конфига.
        # В боевом пути ставка придёт из счёта amm_config; этот подбор -- только
        # чтобы проверить математику на живых сделках.
        import collections as _кол  # noqa: PLC0415
        по_конфигу = _кол.defaultdict(list)
        for x in обр_clmm:
            tx_ = x.get("tx") or {}
            tpl_ = extract_template(tx_, CLMM, x.get("pool_vault"))
            if not tpl_.get("ok") or len(tpl_["accounts"]) < 3:
                continue
            из_ = clmm_min_out(tpl_, tx_, 10_000_000, 0.02)
            if из_.get("why_not") != "ставка комиссии пула CLMM неизвестна (нет amm_config)":
                continue
            mv_ = mints_and_vaults(tpl_, tx_)
            строки_ = {r["account"]: r for r in C.token_rows(tx_).values()}
            qv_ = строки_.get((mv_ or {}).get("quote_vault"))
            bv_ = строки_.get((mv_ or {}).get("base_vault"))
            if not qv_ or not bv_:
                continue
            вх_, вых_ = qv_["post"] - qv_["pre"], bv_["pre"] - bv_["post"]
            тело_ = b""
            for т2 in _CL.тела_событий_clmm(tx_):
                if b58encode(_CL.событие_clmm(т2)["пул"]) == tpl_["accounts"][2]:
                    тело_ = т2
                    break
            if not тело_ or вх_ <= 0 or вых_ <= 0:
                continue
            с2 = _CL.событие_clmm(тело_)
            база_a = not с2["сторона_0_вход"]
            P1_ = _CL.цена_до_по_выходу(выход=вых_, цена_после=с2["цена_после"],
                                        L=с2["ликвидность"], база_это_a=база_a)
            if P1_ is None or P1_ <= 0:
                continue
            Д = _CL.D
            if база_a:
                чист = Д(с2["ликвидность"]) * (Д(с2["цена_после"]) - P1_) / Д(_CL.Q)
            else:
                чист = (Д(с2["ликвидность"]) * Д(_CL.Q) * (P1_ - Д(с2["цена_после"]))
                        / (P1_ * Д(с2["цена_после"])))
            доля = 1 - чист / Д(вх_)
            по_конфигу[tpl_["accounts"][1]].append(
                (round(float(доля), 6), tpl_, tx_, x.get("pool_vault")))
        ставки = {}
        for конфиг_, лист_ in по_конфигу.items():
            счёт_ = _кол.Counter(з[0] for з in лист_)
            знач_, сколько_ = счёт_.most_common(1)[0]
            пулов_ = {з[3] for з in лист_ if з[0] == знач_}
            if сколько_ >= 2 and len(пулов_) >= 2 and знач_ > 0:
                ставки[конфиг_] = int(round(знач_ * 1_000_000))
        checks.append((f"CLMM: ставка комиссии повторилась точно у {len(ставки)} "
                       f"конфигов на разных пулах: "
                       f"{sorted(ставки.values())} миллионных",
                       len(ставки) >= 3))
        сошлось_ = отказ_границы_ = прочий_отказ_ = 0
        худшее_ = 0.0
        for конфиг_, лист_ in по_конфигу.items():
            ставка_ = ставки.get(конфиг_)
            if ставка_ is None:
                continue
            for _, tpl_, tx_, _пв in лист_:
                р = clmm_min_out(tpl_, tx_, 10_000_000, 0.02, ставка_1e6=ставка_)
                if р.get("ok"):
                    сошлось_ += 1
                    худшее_ = max(худшее_, р["model_error"])
                elif "перешла границу диапазона" in (р.get("why_not") or ""):
                    отказ_границы_ += 1
                else:
                    прочий_отказ_ += 1
        checks.append((f"CLMM: минимум выхода посчитан у {сошлось_} живых сделок "
                       f"(наибольшее расхождение модели {худшее_:.2e}), отказ "
                       f"«перешла границу диапазона» у {отказ_границы_}, прочих "
                       f"отказов {прочий_отказ_}",
                       сошлось_ >= 15 and прочий_отказ_ == 0
                       and худшее_ < float(_CL.ПРЕДЕЛ_РАСХОЖДЕНИЯ_CLMM)))
        # Минимум обязан быть НИЖЕ ожидания ровно на проскальзывание, а ожидание
        # -- расти с входом: это проверка самой кривой, а не разбора.
        for конфиг_, лист_ in по_конфигу.items():
            ставка_ = ставки.get(конфиг_)
            if ставка_ is None:
                continue
            пара = None
            for _, tpl_, tx_, _пв in лист_:
                м1 = clmm_min_out(tpl_, tx_, 10_000_000, 0.02, ставка_1e6=ставка_)
                м2 = clmm_min_out(tpl_, tx_, 20_000_000, 0.02, ставка_1e6=ставка_)
                if м1.get("ok") and м2.get("ok"):
                    пара = (м1, м2)
                    break
            if пара:
                м1, м2 = пара
                checks.append((f"CLMM: вдвое больший вход даёт больший выход "
                               f"({м1['expected_out']} -> {м2['expected_out']}), "
                               f"минимум ниже ожидания на проскальзывание "
                               f"({м1['min_out']} < {м1['expected_out']})",
                               м2["expected_out"] > м1["expected_out"]
                               and м1["min_out"] < м1["expected_out"]
                               and м1["min_out"] >= int(м1["expected_out"] * 0.97)))
                checks.append((f"CLMM: без ставки комиссии -- отказ, а не догадка: "
                               f"{clmm_min_out(пара[0] and tpl_, tx_, 10_000_000, 0.02).get('why_not')}",
                               clmm_min_out(tpl_, tx_, 10_000_000, 0.02).get("ok")
                               is False))
                break
        # Раскладка amm_config -- ЗАЯВКА: проверяется, что разбор отвергает
        # мусор и что нули не проходят за ставку.
        checks.append(("CLMM: разбор amm_config отвергает пустой счёт и короткий",
                       _CL.конфиг_clmm(bytes(57)) == {}
                       and _CL.конфиг_clmm(b"\x00" * 10) == {}))
        _д = bytearray(60)
        _д[47:51] = (2500).to_bytes(4, "little")
        _д[51:53] = (60).to_bytes(2, "little")
        checks.append((f"CLMM: разбор amm_config даёт ставку и шаг тика "
                       f"{_CL.конфиг_clmm(bytes(_д))}",
                       _CL.конфиг_clmm(bytes(_д)).get("ставка_1e6") == 2500
                       and _CL.конфиг_clmm(bytes(_д)).get("шаг_тика") == 60))

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
        наши_места = set(сп["user"]) | {i for i, _, _ in сп["user_ata"]} \
            | {i for i, _ in сп["pda"]}
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
    bad_n = 0
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
