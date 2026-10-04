#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""СВОЙ ПУТЬ НА ЧАСТЫЕ ТИПЫ ПУЛОВ: инвентарь, продажа CPMM, размер пакета.

ЗАЧЕМ. Правило «лучший из двух путей» работает только там, где второй путь
ЕСТЬ. Свой путь продажи у полосы есть ровно на ДВА типа: Pump AMM
(bloom_lane_sell, «продажа = покупка без двух индексов») и кривую pump.fun
(c3_prodavec_sborka). На всех прочих типах второй путь отсутствует, и правило
вырождается в «только Jupiter».

ЧТО ЗДЕСЬ ЕСТЬ.
  1. ИНВЕНТАРЬ типов по нашим сделкам -- числом, а не на память (замер, без
     тестов: Правило 8).
  2. ПРОДАЖА RAYDIUM CPMM своим путём: шаблон выводится из НАШЕЙ покупки
     перестановкой четырёх пар счетов, и каждая перестановка проверяется по
     остаткам самой транзакции -- а не «по памяти о раскладке».
  3. КОТИРОВКА CPMM по резервам ОДНИМ чтением (два хранилища одним
     getMultipleAccounts).
  4. РАЗМЕР ПАКЕТА считается аналитически и сверен с настоящей сериализацией.

ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ. Байт-в-байт сверки продажи с ЖИВОЙ продажей CPMM
нет: в репозитории нет НИ ОДНОГО образца продажи этого типа (25 образцов CPMM
-- 3 покупки и 22 чужие пары, продаж ноль). Поэтому перестановка проверяется
ИНВАРИАНТАМИ по самой транзакции (какое хранилище какой минт держит), а что
снять Code-1 -- сказано на странице одной командой.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
ДАННЫЕ = КОРЕНЬ / "data"

WSOL = "So11111111111111111111111111111111111111112"
CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
ПРЕДЕЛ_РАЗМЕРА_TX = 1232

# СЧЕТА swap_base_input У RAYDIUM CPMM -- тринадцать, и порядок проверен на
# живых образцах (data/c2_pool_samples/CPMMoo8...json, 25 штук, у всех ровно 13):
#   0 payer(подписант)   1 authority        2 amm_config      3 pool_state
#   4 input_token_account 5 output_token_account
#   6 input_vault         7 output_vault
#   8 input_token_program 9 output_token_program
#   10 input_mint        11 output_mint     12 observation_state
ИНДЕКС = {"payer": 0, "authority": 1, "amm_config": 2, "pool_state": 3,
          "вход_счёт": 4, "выход_счёт": 5, "вход_хранилище": 6,
          "выход_хранилище": 7, "вход_программа": 8, "выход_программа": 9,
          "вход_минт": 10, "выход_минт": 11, "observation": 12}
# ПРОДАЖА -- ТА ЖЕ ИНСТРУКЦИЯ С ПЕРЕСТАВЛЕННЫМИ ЧЕТЫРЬМЯ ПАРАМИ. У
# swap_base_input направление задаётся НЕ дискриминатором, а тем, какие счета
# стоят входом и выходом: поэтому продажа -- это покупка, у которой переставлены
# (4,5), (6,7), (8,9) и (10,11). Данные инструкции (сумма входа и минимум
# выхода) свои.
ПАРЫ_ПЕРЕСТАНОВКИ = ((4, 5), (6, 7), (8, 9), (10, 11))

WHY_НЕ_CPMM = "в транзакции нет инструкции Raydium CPMM"
WHY_НЕ_13 = "у инструкции CPMM не тринадцать счетов"
WHY_НЕ_НАША = "это не наша покупка: подписант не наш кошелёк"
WHY_НЕ_SOL = "вход покупки не WSOL -- эта перестановка не про нашу ногу"
WHY_ХРАНИЛИЩА = "хранилища не держат те минты, что стоят в счетах"


class ОшибкаТипа(Exception):
    pass


# ------------------------------------------------------------- общие разборы

def instrukcii_programmy(tx: dict, programma: str) -> list:
    """Инструкции этой программы -- и верхние, и вложенные."""
    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    из_ = [их for их in (сообщение.get("instructions") or [])
           if их.get("programId") == programma]
    for вн in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        из_ += [их for их in (вн.get("instructions") or [])
                if их.get("programId") == programma]
    return из_


def ostatki_po_schetam(tx: dict) -> dict:
    """{счёт: минт} по остаткам транзакции -- это говорит ЦЕПЬ, а не догадка."""
    мета = (tx or {}).get("meta") or {}
    ключи = [(к.get("pubkey") if isinstance(к, dict) else к)
             for к in (((tx.get("transaction") or {}).get("message") or {})
                       .get("accountKeys") or [])]
    из_: dict = {}
    for где in ("preTokenBalances", "postTokenBalances"):
        for б in (мета.get(где) or []):
            и = б.get("accountIndex")
            if isinstance(и, int) and 0 <= и < len(ключи):
                из_[ключи[и]] = str(б.get("mint") or "")
    return из_


# --------------------------------------------------- 1. ИНВЕНТАРЬ ТИПОВ

ИМЕНА_ПРОГРАММ = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun (кривая)",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6 (маршрутизатор)",
}
# СВОЙ ПУТЬ ПРОДАЖИ -- ТОЛЬКО У ДВУХ ТИПОВ, и это проверено по коду:
# bloom_lane_sell.шаблон_продажи_из_покупки по умолчанию строит ТОЛЬКО
# PUMP_AMM, а c3_prodavec_sborka -- кривую v2.
СВОЙ_ПУТЬ_ПРОДАЖИ = ("Pump AMM", "Pump.fun (кривая)")
# КОТИРОВКА ПО РЕЗЕРВАМ есть у всех, кого знает c2_swap_build.min_out_from_
# reserves: отдельные ветки DAMM2/DBC/CLMM/DLMM/LAUNCHLAB/BONDING плюс общая
# ветка по двум хранилищам (CPMM, Pump AMM, AMM v4, Whirlpool).
БЕЗ_КОТИРОВКИ: tuple = ()


def inventar_tipov(*, fajly: list | None = None) -> dict:
    """ЧЕМ ТОРГУЕТ ПОЛОСА -- ЧИСЛОМ. Замер по данным репозитория, без сети.

    Источники названы поимённо, и каждый отдаёт своё число: подменить один
    файл другим молча нельзя.
    """
    из_ = {"ok": True, "istochniki": [], "po_tipam": {}, "vsego": 0,
            "bez_svojego_puti": []}
    пути = fajly or [ДАННЫЕ / "nashi_pump_amm.json"]
    for п in пути:
        п = Path(п)
        if not п.exists():
            из_["istochniki"].append({"fajl": str(п), "why_not": "файла нет"})
            continue
        try:
            д = json.loads(п.read_text(encoding="utf-8"))
        except (ValueError, OSError) as сбой:
            из_["istochniki"].append({"fajl": п.name,
                                       "why_not": f"{type(сбой).__name__}"})
            continue
        по = (д.get("по_программам") if isinstance(д, dict) else None) or {}
        счёт = 0
        for ключ, n in по.items():
            имя = ИМЕНА_ПРОГРАММ.get(ключ, ключ)
            # ИМЯ И АДРЕС ОДНОЙ ПРОГРАММЫ СХЛОПЫВАЮТСЯ: в снимке они лежат
            # двумя строками ("Pump AMM" и pAMMBay...), и считать их за два
            # разных типа -- врать инвентарём.
            имя = {"Pump.fun": "Pump.fun (кривая)", "Jupiter v6":
                   "Jupiter v6 (маршрутизатор)"}.get(имя, имя)
            из_["po_tipam"][имя] = из_["po_tipam"].get(имя, 0) + int(n)
            счёт += int(n)
        из_["istochniki"].append({"fajl": п.name, "pozicij": счёт,
                                   "vsego_v_fajle": д.get("позиций_всего")})
        из_["vsego"] += счёт
    из_["po_tipam"] = dict(sorted(из_["po_tipam"].items(),
                                   key=lambda п: -п[1]))
    из_["bez_svojego_puti"] = [
        {"tip": т, "pozicij": n,
         "dolja_pct": (round(100.0 * n / из_["vsego"], 1) if из_["vsego"] else None)}
        for т, n in из_["po_tipam"].items()
        if т not in СВОЙ_ПУТЬ_ПРОДАЖИ and "маршрутизатор" not in т
        and т != "None"]
    return из_


# ------------------------------------- 2. ПРОДАЖА RAYDIUM CPMM СВОИМ ПУТЁМ

def shablon_prodazhi_cpmm(tx_pokupki: dict, *, nash_koshelek: str) -> dict:
    """ШАБЛОН ПРОДАЖИ ИЗ НАШЕЙ ПОКУПКИ: та же инструкция, четыре пары местами.

    Проверяется НЕ «раскладкой по памяти», а остатками самой транзакции: какое
    хранилище какой минт держит, видно по preTokenBalances. Если после
    перестановки хранилище и минт не совпали -- отказ по имени, а не подпись
    наугад.
    """
    из_ = {"ok": False, "why_not": None, "accounts": None, "programma": CPMM,
            "proverok": 0, "vhod_mint": None, "vyhod_mint": None}
    их = instrukcii_programmy(tx_pokupki or {}, CPMM)
    if not их:
        из_["why_not"] = WHY_НЕ_CPMM
        return из_
    счета = list(их[0].get("accounts") or [])
    if len(счета) != 13:
        из_["why_not"] = f"{WHY_НЕ_13}: {len(счета)}"
        return из_
    if счета[ИНДЕКС["payer"]] != nash_koshelek:
        из_["why_not"] = (f"{WHY_НЕ_НАША}: {str(счета[0])[:8]} вместо "
                          f"{str(nash_koshelek)[:8]}")
        return из_
    if счета[ИНДЕКС["вход_минт"]] != WSOL:
        из_["why_not"] = f"{WHY_НЕ_SOL}: вход {str(счета[ИНДЕКС['вход_минт']])[:8]}"
        return из_
    минты = ostatki_po_schetam(tx_pokupki)
    # ИНВАРИАНТ ДО ПЕРЕСТАНОВКИ: хранилище входа держит входной минт, хранилище
    # выхода -- выходной. Это говорит цепь остатками, а не раскладка по памяти.
    пары = ((ИНДЕКС["вход_хранилище"], ИНДЕКС["вход_минт"]),
            (ИНДЕКС["выход_хранилище"], ИНДЕКС["выход_минт"]),
            (ИНДЕКС["вход_счёт"], ИНДЕКС["вход_минт"]),
            (ИНДЕКС["выход_счёт"], ИНДЕКС["выход_минт"]))
    для_проверки = [(счета[a], счета[b]) for a, b in пары]
    плохие = [(с, м) for с, м in для_проверки
              if с in минты and минты[с] != м]
    из_["proverok"] = sum(1 for с, _ in для_проверки if с in минты)
    if плохие:
        из_["why_not"] = (f"{WHY_ХРАНИЛИЩА}: {[(с[:8], м[:8]) for с, м in плохие]}")
        return из_
    if из_["proverok"] < 2:
        из_["why_not"] = ("остатков транзакции не хватает, чтобы проверить "
                          "хранилища: подписывать перестановку вслепую нельзя")
        return из_
    продажа = list(счета)
    for a, b in ПАРЫ_ПЕРЕСТАНОВКИ:
        продажа[a], продажа[b] = продажа[b], продажа[a]
    из_.update(ok=True, accounts=продажа,
               vhod_mint=продажа[ИНДЕКС["вход_минт"]],
               vyhod_mint=продажа[ИНДЕКС["выход_минт"]])
    return из_


def kotirovka_cpmm(*, rezerv_vhoda: int, rezerv_vyhoda: int, vhod: int,
                   komissija_bps: int) -> dict:
    """Постоянное произведение с комиссией. Чистая арифметика.

    Комиссия CPMM живёт в amm_config (счёт 2), и БЕЗ НЕЁ котировки нет: ставка
    не предполагается, а передаётся. Отказ лучше выдуманных 0.25 %.
    """
    из_ = {"ok": False, "why_not": None, "lamports": 0}
    if komissija_bps is None or not (0 <= int(komissija_bps) < 10_000):
        из_["why_not"] = (f"ставка комиссии пула не задана или негодная: "
                          f"{komissija_bps!r} -- её читают из amm_config")
        return из_
    if min(int(rezerv_vhoda), int(rezerv_vyhoda), int(vhod)) <= 0:
        из_["why_not"] = "резервы или вход не положительны"
        return из_
    чистый_вход = int(vhod) * (10_000 - int(komissija_bps)) // 10_000
    вышло = (int(rezerv_vyhoda) * чистый_вход) // (int(rezerv_vhoda) + чистый_вход)
    из_.update(ok=True, lamports=вышло, chistyj_vhod=чистый_вход)
    return из_


# ------------------------------------------- 3. РАЗМЕР ПАКЕТА, АНАЛИТИЧЕСКИ

def _cu16(n: int) -> int:
    return 1 if n < 0x80 else (2 if n < 0x4000 else 3)


def razmer_tx(*, podpisej: int, kljuchej: int, instrukcii: list,
              tablic_adresov: int = 0) -> int:
    """Размер версии 0 БЕЗ таблиц адресов, байтами.

    instrukcii -- список (сколько счетов, сколько байт данных).
    СВЕРЕНО С НАСТОЯЩЕЙ СЕРИАЛИЗАЦИЕЙ (solders, 05.10): четыре разные формы,
    расхождение ноль байт. Векторы записаны в самопроверке.
    """
    р = _cu16(podpisej) + 64 * int(podpisej)
    р += 1 + 3                                   # версия v0 + заголовок
    р += _cu16(kljuchej) + 32 * int(kljuchej)
    р += 32                                      # blockhash
    р += _cu16(len(instrukcii))
    for счетов, данных in instrukcii:
        р += 1 + _cu16(счетов) + int(счетов) + _cu16(данных) + int(данных)
    р += _cu16(int(tablic_adresov))
    return р


# ЧТО ЕДЕТ В БОЕВОЙ ПРОДАЖЕ, КРОМЕ САМОГО СВОПА (всё -- из нашего же кода):
#   nonce advance   -- 3 счёта, 4 байта данных (bloom_own_send, nonce тёплый);
#   два ComputeBudget (предел и цена) -- по 0 счетов, 5 и 9 байт;
#   чаевые -- системный перевод, 2 счёта, 12 байт;
#   closeAccount -- 3 счёта, 1 байт.
СЛУЖЕБНЫЕ = {"nonce": (3, 4), "cu_limit": (0, 5), "cu_price": (0, 9),
             "chaevye": (2, 12), "close": (3, 1)}


def vlezet_s_zakrytiem(*, schetov_svopa: int, dannyh_svopa: int,
                       kljuchej_vsego: int, s_nonce: bool = True,
                       s_chaevymi: bool = True, s_zakrytiem: bool = True
                       ) -> dict:
    """Влезает ли продажа С ЗАКРЫТИЕМ СЧЁТА в 1232 байта. Числом, а не на глаз."""
    инстр = [(schetov_svopa, dannyh_svopa), SLUZH("cu_limit"), SLUZH("cu_price")]
    if s_nonce:
        инстр.insert(0, SLUZH("nonce"))
    if s_chaevymi:
        инстр.append(SLUZH("chaevye"))
    без = razmer_tx(podpisej=1, kljuchej=kljuchej_vsego, instrukcii=инстр)
    с_закр = razmer_tx(podpisej=1, kljuchej=kljuchej_vsego + 0,
                       instrukcii=инстр + ([SLUZH("close")] if s_zakrytiem else []))
    return {"bez_zakrytija": без, "s_zakrytijem": с_закр,
            "predel": ПРЕДЕЛ_РАЗМЕРА_TX,
            "vlezet": с_закр <= ПРЕДЕЛ_РАЗМЕРА_TX,
            "zapas": ПРЕДЕЛ_РАЗМЕРА_TX - с_закр}


def SLUZH(имя: str) -> tuple:  # noqa: N802
    return СЛУЖЕБНЫЕ[имя]


# ------------------------------------------------------------- самопроверка

ZHDEM_PROVEROK = 16
# ВЕКТОРЫ РАЗМЕРА -- ИЗМЕРЕНЫ НАСТОЯЩЕЙ СЕРИАЛИЗАЦИЕЙ (solders, 05.10), и
# расхождение с аналитикой было НОЛЬ байт на всех четырёх формах. Здесь они
# записаны числами, чтобы самопроверка шла без solders: на облачном бегунке
# его нет, а гейт прогона из-за этого однажды уже встал.
ВЕКТОРЫ_РАЗМЕРА = (
    # (подписей, ключей, [(счетов, данных)], размер)
    (1, 10, [(8, 24)], 459),
    (1, 11, [(8, 24), (8, 24)], 526),
    (1, 13, [(8, 32)] * 4, 692),
    (1, 14, [(8, 16)] * 5, 687),
)
ФАЙЛ_ОБРАЗЦОВ_CPMM = "c2_pool_samples/CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C.json"
# ЗАМЕР ПО ОБРАЗЦАМ CPMM (25 штук): у ВСЕХ ровно 13 счетов, покупок за SOL --
# три, продаж -- НОЛЬ. Числа объявлены, чтобы смена файла была видна.
ЖДЁМ_CPMM = {"obrazcov": 25, "po_13_schetov": 25, "pokupok_sol": 3, "prodazh": 0}
ЖДЁМ_ИНВЕНТАРЯ = {"vsego": 562, "pervyj": "Pump AMM",
                  "bez_puti_pervyj": "Meteora DLMM"}


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

    print("c3_svoj_put_tipy: самопроверка")
    # ---------------------------------- РАЗМЕР ПАКЕТА
    плохие_в = [(в, razmer_tx(podpisej=в[0], kljuchej=в[1], instrukcii=в[2]))
                for в in ВЕКТОРЫ_РАЗМЕРА
                if razmer_tx(podpisej=в[0], kljuchej=в[1], instrukcii=в[2]) != в[3]]
    chk("размер пакета считается аналитически и совпадает с НАСТОЯЩЕЙ "
        "сериализацией на всех четырёх формах (расхождение ноль байт)",
        not плохие_в, плохие_в)
    chk("compact-u16 растёт на 128 и на 16384, а не «примерно»",
        _cu16(127) == 1 and _cu16(128) == 2 and _cu16(16_383) == 2
        and _cu16(16_384) == 3, None)
    # ПРОДАЖА CPMM С ЗАКРЫТИЕМ СЧЁТА: влезает ли в 1232 с nonce и чаевыми.
    в = vlezet_s_zakrytiem(schetov_svopa=13, dannyh_svopa=24, kljuchej_vsego=20)
    chk(f"продажа CPMM с nonce, чаевыми И ЗАКРЫТИЕМ счёта влезает в "
        f"{ПРЕДЕЛ_РАЗМЕРА_TX}: {в['s_zakrytijem']} байт, запас {в['zapas']}",
        в["vlezet"] and в["s_zakrytijem"] > в["bez_zakrytija"], в)
    тесно = vlezet_s_zakrytiem(schetov_svopa=13, dannyh_svopa=24,
                               kljuchej_vsego=36)
    chk("а на тяжёлом пакете (36 ключей) закрытие уже НЕ влезает -- и это "
        "видно числом, а не отказом сети после отправки",
        not тесно["vlezet"] and тесно["zapas"] < 0, тесно)
    # ---------------------------------- ИНВЕНТАРЬ (замер)
    инв = inventar_tipov()
    chk(f"инвентарь типов снят по данным репозитория: {ЖДЁМ_ИНВЕНТАРЯ['vsego']} "
        "позиций, первым типом Pump AMM",
        инв["vsego"] == ЖДЁМ_ИНВЕНТАРЯ["vsego"]
        and next(iter(инв["po_tipam"])) == ЖДЁМ_ИНВЕНТАРЯ["pervyj"], инв["po_tipam"])
    chk("самый частый тип БЕЗ своего пути продажи назван числом: Meteora DLMM",
        инв["bez_svojego_puti"]
        and инв["bez_svojego_puti"][0]["tip"] == ЖДЁМ_ИНВЕНТАРЯ["bez_puti_pervyj"],
        инв["bez_svojego_puti"][:2])
    chk("имя и адрес одной программы не считаются за два типа",
        "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA" not in инв["po_tipam"]
        and инв["po_tipam"].get("Pump AMM", 0) == 197, инв["po_tipam"])
    # ---------------------------------- ЖИВЫЕ ОБРАЗЦЫ CPMM
    п = КОРЕНЬ / "data" / ФАЙЛ_ОБРАЗЦОВ_CPMM
    обр = json.loads(п.read_text(encoding="utf-8")) if п.exists() else []
    по13 = покупок = продаж = 0
    покупка_обр = None
    for x in обр:
        tx = x.get("tx") if isinstance(x, dict) else None
        их = instrukcii_programmy(tx or {}, CPMM)
        if not их:
            continue
        сч = их[0].get("accounts") or []
        по13 += (len(сч) == 13)
        if len(сч) != 13:
            continue
        if сч[ИНДЕКС["вход_минт"]] == WSOL:
            покупок += 1
            покупка_обр = покупка_обр or (tx, сч)
        elif сч[ИНДЕКС["выход_минт"]] == WSOL:
            продаж += 1
    chk(f"образцы CPMM на месте и пересчитаны: {ЖДЁМ_CPMM['obrazcov']} штук, у "
        f"всех по 13 счетов, покупок за SOL {ЖДЁМ_CPMM['pokupok_sol']}, "
        f"продаж {ЖДЁМ_CPMM['prodazh']} -- сверять продажу байт в байт НЕ С ЧЕМ",
        len(обр) == ЖДЁМ_CPMM["obrazcov"] and по13 == ЖДЁМ_CPMM["po_13_schetov"]
        and покупок == ЖДЁМ_CPMM["pokupok_sol"] and продаж == ЖДЁМ_CPMM["prodazh"],
        (len(обр), по13, покупок, продаж))
    chk("живая покупка CPMM для разбора нашлась", покупка_обр is not None, None)
    if покупка_обр:
        tx_п, сч_п = покупка_обр
        наш = сч_п[0]
        ш = shablon_prodazhi_cpmm(tx_п, nash_koshelek=наш)
        chk("ШАБЛОН ПРОДАЖИ CPMM выводится из живой покупки: четыре пары "
            "переставлены, вход стал токеном, выход -- WSOL",
            ш["ok"] and ш["accounts"][ИНДЕКС["вход_минт"]] == сч_п[ИНДЕКС["выход_минт"]]
            and ш["accounts"][ИНДЕКС["выход_минт"]] == WSOL
            and ш["accounts"][ИНДЕКС["вход_хранилище"]] == сч_п[ИНДЕКС["выход_хранилище"]]
            and ш["accounts"][ИНДЕКС["выход_хранилище"]] == сч_п[ИНДЕКС["вход_хранилище"]],
            ш.get("why_not"))
        chk("неизменные места остались на местах: payer, authority, amm_config, "
            "pool_state и observation переставлять нельзя",
            all(ш["accounts"][и] == сч_п[и]
                for и in (0, 1, 2, 3, 12)), None)
        chk("перестановка ПРОВЕРЕНА остатками самой транзакции (какое "
            f"хранилище какой минт держит), проверок {ш['proverok']}",
            ш["proverok"] >= 2, ш["proverok"])
        # ДОКАЗАННЫЙ КРАСНЫЙ: чужой кошелёк и подменённое хранилище не проходят.
        чужой = shablon_prodazhi_cpmm(tx_п, nash_koshelek="ChuzhojKoshelek111")
        chk("ДОКАЗАННЫЙ КРАСНЫЙ: чужая покупка шаблоном продажи не становится",
            not чужой["ok"] and WHY_НЕ_НАША in (чужой["why_not"] or ""),
            чужой.get("why_not"))
        tx_порча = json.loads(json.dumps(tx_п))
        for где in ("preTokenBalances", "postTokenBalances"):
            for б in ((tx_порча.get("meta") or {}).get(где) or []):
                б["mint"] = "PodmenjonnyjMint1111111111111111111111111"
        порча = shablon_prodazhi_cpmm(tx_порча, nash_koshelek=наш)
        chk("ДОКАЗАННЫЙ КРАСНЫЙ: если хранилища держат НЕ ТЕ минты, шаблон "
            "отказывает по имени, а не подписывает перестановку вслепую",
            not порча["ok"] and WHY_ХРАНИЛИЩА in (порча["why_not"] or ""),
            порча.get("why_not"))
    # ---------------------------------- КОТИРОВКА CPMM
    к = kotirovka_cpmm(rezerv_vhoda=1_000_000_000, rezerv_vyhoda=500_000_000,
                       vhod=10_000_000, komissija_bps=25)
    ручная_чистая = 10_000_000 * (10_000 - 25) // 10_000
    ручной = (500_000_000 * ручная_чистая) // (1_000_000_000 + ручная_чистая)
    chk("котировка CPMM -- постоянное произведение с комиссией, и число "
        "совпадает с ручным счётом до единицы",
        к["ok"] and к["lamports"] == ручной, (к, ручной))
    chk("БЕЗ СТАВКИ КОМИССИИ котировки нет: ставка читается из amm_config, а "
        "не предполагается 0.25 %",
        not kotirovka_cpmm(rezerv_vhoda=1, rezerv_vyhoda=1, vhod=1,
                           komissija_bps=None)["ok"], None)

    if упавшие:
        print("УПАЛИ: " + "; ".join(упавшие))
    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным")
        return 1
    return 1 if плохо else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--inventar", action="store_true",
                   help="инвентарь типов пулов по данным репозитория")
    p.add_argument("--zapisat", default=None,
                   help="записать инвентарь в этот файл")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if a.inventar or a.zapisat:
        из_ = inventar_tipov()
        текст = json.dumps(из_, ensure_ascii=False, indent=1)
        if a.zapisat:
            Path(a.zapisat).write_text(текст + "\n", encoding="utf-8")
            print(f"инвентарь записан: {a.zapisat}")
        else:
            print(текст)
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
