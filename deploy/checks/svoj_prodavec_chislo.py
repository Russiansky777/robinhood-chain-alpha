#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ЧИСЛО ДЛЯ ШТАБА: свой продавец против ЖИВЫХ продаж -- по каждому типу пула.

ЭТО ЗАМЕР, А НЕ МОДУЛЬ. По каждому типу пула берутся живые сделки из образцов
репозитория и сборов Code-2, среди них находятся ПАРЫ «покупка и продажа одного
и того же пула», и по каждой паре проверяется одно: собранная нашим зеркалом
продажа (из покупки) даёт ТОТ ЖЕ набор счетов и ту же инструкцию, что настоящая
продажа этого пула.

ПОЧЕМУ ПАРАМИ, А НЕ ПО ОДНОЙ ПРОДАЖЕ. Зеркало строится ИЗ ПОКУПКИ; проверять его
на той же продаже, из которой оно и собрано, значило бы проверять само себя.
Пара «чужая покупка -> чужая продажа того же пула» -- единственная честная сверка,
которую можно сделать офлайн.

ЧТО СЧИТАЕТСЯ СОВПАДЕНИЕМ. Длина списка счетов, имя инструкции и КАЖДОЕ место,
кроме НАШИХ: покупатель и продавец -- разные кошельки, поэтому места подписанта и
его токеновых счетов обязаны различаться, и различаться ТОЛЬКО они. Что место
наше, доказывается по самой сделке: адрес принадлежит покупателю (его кошелёк или
его токеновый счёт в балансах), а у продажи на этом месте стоит счёт продавца.
Расхождение на ЧУЖОМ месте -- провал, и он печатается подписью, а не числом.

Запуск: python3 deploy/checks/svoj_prodavec_chislo.py [--sbory <каталог>] [--out <файл>]
Выход 1 -- есть расхождения вне наших мест.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[2]
# МЕСТО САМОГО ПУЛА В ИНСТРУКЦИИ -- по раскладкам, которые стоят в коде:
#   CLMM  c2_cl_quote.СМ_ПУЛ_В_ИНСТРУКЦИИ_CLMM = 2
#   DBC   c2_dbc_stroitel.РАСКЛАДКА["пул"] = 2
#   Whirlpool c2_whirlpool_stroitel.РАСКЛАДКИ: swap -> 2, swap_v2 -> 4
#   DLMM  c2_dlmm_bez_chteniy: пул -- счёт 0 инструкции
#   CPMM  раскладка Raydium: 0 payer, 1 authority, 2 amm_config, 3 pool_state
#   AMM v4 и DAMM v2 -- место 1
#   Pump AMM -- место 0
МЕСТО_ПУЛА = {
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": 2,
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": 2,
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": 0,
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": 3,
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": 1,
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": 1,
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": 0,
}
МЕСТО_ПУЛА_WHIRLPOOL = {11: 2, 15: 4}
WHIRLPOOL = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"


def _модули(путь=None):
    sys.path.insert(0, str(Path(путь) if путь else КОРЕНЬ / "analysis"))
    import c2_common as C  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    import c3_svoj_prodavec as P  # noqa: PLC0415
    return C, B, P


def адрес_пула(программа: str, счета: list):
    if программа == WHIRLPOOL:
        м = МЕСТО_ПУЛА_WHIRLPOOL.get(len(счета))
    else:
        м = МЕСТО_ПУЛА.get(программа)
    return счета[м] if (м is not None and len(счета) > м) else None


def направление(C, tx: dict, счета: list) -> str | None:
    """'продажа' | 'покупка' по движению WSOL на счетах инструкции."""
    строки = {r["account"]: r for r in C.token_rows(tx).values()}
    сч = set(счета)
    пришло = sum(r["post"] - r["pre"] for a, r in строки.items()
                 if a in сч and r.get("mint") == C.WSOL and r["post"] > r["pre"])
    ушло = sum(r["pre"] - r["post"] for a, r in строки.items()
               if a in сч and r.get("mint") == C.WSOL and r["pre"] > r["post"])
    if ушло > пришло and ушло > 0:
        return "продажа"
    if пришло > ушло and пришло > 0:
        return "покупка"
    return None


def транзакции(путь: str) -> list:
    д = json.loads(Path(путь).read_text(encoding="utf-8"))
    if isinstance(д, list):
        return [x.get("tx") for x in д
                if isinstance(x, dict) and isinstance(x.get("tx"), dict)]
    for ключ in ("снимки", "ряды", "сигналы", "инструкции"):
        ряды = д.get(ключ)
        if isinstance(ряды, list):
            из_ = [(x.get("транзакция") or x.get("tx")) for x in ряды
                   if isinstance(x, dict)]
            из_ = [t for t in из_ if isinstance(t, dict)]
            if из_:
                return из_
    return []


def источники(сборы: str | None) -> list:
    сп = sorted(glob.glob(str(КОРЕНЬ / "data" / "c2_pool_samples" / "*.json")))
    сп += sorted(glob.glob(str(КОРЕНЬ / "data" / "c3_usdc_noga" / "*.json")))
    if сборы:
        сп += sorted(glob.glob(str(Path(сборы) / "*.json")))
    return сп


def наши_места(C, B, tx: dict, счета: list, минты: set) -> set:
    """Места, занятые ТОРГОВЦЕМ этой сделки: подписант и его токеновые счета.

    По балансам ИЛИ по выводу ATA. Второй путь обязателен: продавец часто
    ЗАКРЫВАЕТ токеновый счёт в той же транзакции, и тогда его в балансах нет
    вовсе -- место выглядело бы чужим, а оно наше.
    """
    свои = set()
    строки = {r["account"]: r for r in C.token_rows(tx).values()}
    подписанты = set(C.signers(tx))
    ата = set()
    for п in подписанты:
        for м in минты:
            if not м:
                continue
            for прог in (B.TOKEN_PROGRAM, "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
                try:
                    ата.add(B.ata(п, м, прог))
                except Exception:  # noqa: BLE001
                    pass
    for и, а in enumerate(счета):
        if а in подписанты or а in ата:
            свои.add(и)
            continue
        вл = (строки.get(а) or {}).get("owner")
        if вл and вл in подписанты:
            свои.add(и)
    return свои


def пары(C, B, сборы) -> dict:
    """{программа: {пул: {'покупка': [tx, счета], 'продажа': [...]}}}"""
    из_ = collections.defaultdict(lambda: collections.defaultdict(dict))
    for путь in источники(сборы):
        try:
            txs = транзакции(путь)
        except Exception:  # noqa: BLE001
            continue
        for tx in txs:
            for ix in B.all_instructions(tx):
                прог = ix.get("programId")
                if прог not in МЕСТО_ПУЛА and прог != WHIRLPOOL:
                    continue
                счета = list(ix.get("accounts") or [])
                пул = адрес_пула(прог, счета)
                if not пул:
                    continue
                н = направление(C, tx, счета)
                if not н:
                    continue
                # ПАРА -- ТОЛЬКО ОДНОЙ РАЗНОВИДНОСТИ ИНСТРУКЦИИ И ОДНОЙ ДЛИНЫ.
                # swap и swap_v2 у CLMM -- разные списки счетов, а у DLMM в хвосте
                # разное число массивов корзин: сравнивать их между собой значит
                # сравнивать разные инструкции.
                # ПАРА -- ОДНОГО ПУЛА. По разновидности инструкции пары НЕ
                # отбираются: у Pump AMM продажа -- это ДРУГАЯ инструкция (sell
                # против buy), и требовать совпадения дискриминаторов значило бы
                # не найти ни одной пары. Разновидность сверяется в самой сверке:
                # зеркало обязано собрать ту инструкцию, которой продают.
                ключ = пул
                из_[прог][ключ].setdefault(н, (tx, счета))
    return из_


# РАЗРЕШЁННЫЕ РАСХОЖДЕНИЯ: места, которым программа позволяет быть РАЗНЫМИ у
# разных торговцев. Не "исключения ради зелени", а измеренный факт с источником.
# МАССИВЫ ТИКОВ И КОРЗИН -- МЕСТА, КОТОРЫЕ ЗАВИСЯТ ОТ СОСТОЯНИЯ, А НЕ ОТ
# НАПРАВЛЕНИЯ. Они разные у любых двух сделок одного пула, и в бою их заменяет
# котировщик (clmm: шаблон_с_нынешними_массивами, dlmm: то же). Поэтому хвост
# сравнивать нельзя -- его длина и содержимое печатаются отдельно.
# Границы -- из раскладок самих модулей, а не на глаз:
#   CLMM  c2_clmm_stroitel.РАСКЛАДКИ[вид]["остаток_с"]: swap 10, swap_v2 13
#   Whirlpool c2_whirlpool_stroitel.РАСКЛАДКИ[вид]["массивы"]: swap (7,8,9)+оракул,
#             swap_v2 (11,12,13)+оракул -> хвост с 7 и с 11
#   DLMM  c2_swap_build.DYN: последнее постоянное место 12 -> хвост с 13
ХВОСТ_С = {
    ("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", None): {10: 10, 13: 13},
    ("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", 11): 7,
    ("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", 15): 11,
    ("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo", None): 13,
}


def хвост_с(прог: str, счетов: int) -> int | None:
    """С какого места начинается хвост массивов. None -- хвоста у типа нет."""
    if прог == "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc":
        return ХВОСТ_С.get((прог, счетов))
    if прог == "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo":
        return 13 if счетов > 13 else None
    if прог == "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK":
        # swap -- 10 постоянных мест, swap_v2 -- 13; по длине их не различить
        # надёжно, поэтому берётся меньшая граница: лишнего в сверку не попадёт.
        return 10 if счетов > 10 else None
    return None


# МЕСТА ТОРГОВЦА ПО РАСКЛАДКЕ -- для способов, где шаблон переносится как есть.
# Там наш кошелёк никуда не подставляется (счета уже наши), поэтому "наше место"
# определяется раскладкой, а не владельцем счёта: у сделки МАРШРУТИЗАТОРА на
# месте торговца стоит его PDA, и по подписанту его не узнать.
# Числа -- из c2_swap_build: SPECS[...]["user"] и ["user_ata"] у CPMM и Pump AMM,
# у DYN-типов -- ключи user_accounts, замеренные на живых сделках
# (DLMM [4,5,10], DAMM v2 [2,3,8]).
МЕСТА_ТОРГОВЦА = {
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": (4, 5, 10),
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": (2, 3, 8),
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": (0, 4, 5),
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": (1, 5, 6),
}


РАЗРЕШЁННЫЕ = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": {
        9: "получатель протокольной комиссии -- программа разрешает любого из "
           "списка (docs/prodazha_pump_amm_raskladka.md, замер Code-1)",
        10: "токеновый счёт получателя протокольной комиссии -- тот же список",
    },
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": {
        9: "host_fee_in -- НЕОБЯЗАТЕЛЬНЫЙ счёт реферала, его выбирает торговец. "
           "Замер по живым сделкам DLMM: на этом месте стоит сама программа "
           "(условие «нет реферала») в 169 случаях из 176, в остальных -- разные "
           "счета разных торговцев",
    },
}


def сверить(C, B, P, прог: str, пул: str, покупка, продажа) -> dict:
    """Одна пара: зеркало из покупки против настоящей продажи того же пула."""
    tx_п, счета_п = покупка
    tx_s, счета_s = продажа
    из_ = {"пул": пул, "ok": False, "why_not": None, "мест": None,
            "разошлось": None, "вне_наших": None, "имя": None,
            "диск_зеркала": None, "диск_продажи": None,
            "разновидность_разная": None}
    ш = P.шаблон_продажи(tx_п, программа=прог)
    if not ш.get("ok"):
        из_["why_not"] = ш.get("why_not")
        return из_
    из_["имя"] = ш.get("имя_инструкции")
    # РАЗНОВИДНОСТЬ -- ПО БАЙТАМ СОБРАННОЙ ИНСТРУКЦИИ, а не по имени в шаблоне.
    из_["диск_продажи"] = B.b58decode(_данные_ix(tx_s, счета_s, прог))[:8].hex()
    try:
        и_зерк = P.инструкция_продажи(ш, наш_кошелёк=счета_s[0], база_в=1,
                                      минимум_выхода=0)
        из_["диск_зеркала"] = bytes(и_зерк.data)[:8].hex()
    except Exception as сбой:  # noqa: BLE001
        из_["why_not"] = f"инструкция зеркала не собралась: {type(сбой).__name__}"
        return из_
    # У неанкорных программ (AMM v4, кривая) в первых восьми байтах лежат и
    # аргументы, поэтому сравнивается только ТЕГ -- первый байт.
    длина_тега = 1 if прог in НЕАНКОРНЫЕ else 8
    если_разные = (из_["диск_зеркала"][:2 * длина_тега]
                   != из_["диск_продажи"][:2 * длина_тега])
    из_["разновидность_разная"] = если_разные
    if если_разные:
        из_["why_not"] = (f"продают другой разновидностью: зеркало "
                           f"{из_['диск_зеркала'][:2 * длина_тега]}, продажа "
                           f"{из_['диск_продажи'][:2 * длина_тега]}")
        return из_
    наши = ш["accounts"]
    из_["мест"] = (len(наши), len(счета_s))
    хв = хвост_с(прог, len(наши))
    предел = min(хв if хв is not None else len(наши), len(наши), len(счета_s))
    из_["хвост_с"] = хв
    разн = [и for и, (a, b) in enumerate(zip(наши, счета_s)) if a != b and и < предел]
    минты = _минты_пула(C, B, tx_п, наши) | _минты_пула(C, B, tx_s, счета_s)
    по_раскладке = (set(МЕСТА_ТОРГОВЦА.get(прог) or ())
                    | места_строителя(прог, len(счета_s)))
    мои_места = (наши_места(C, B, tx_п, наши, минты) | подставляемые(B, ш, tx_п)
                 | по_раскладке)
    его_места = наши_места(C, B, tx_s, счета_s, минты) | по_раскладке
    разрешено = РАЗРЕШЁННЫЕ.get(прог) or {}
    вне = [и for и in разн
           if и not in разрешено
           and not (и in мои_места and и in его_места)]
    из_.update(разошлось=разн, вне_наших=вне, ok=not вне,
               разрешённых=[и for и in разн if и in разрешено])
    if вне:
        из_["why_not"] = f"расхождение вне наших мест: {вне}"
    return из_


def места_строителя(прог: str, счетов: int) -> set:
    """Места торговца ИЗ РАСКЛАДКИ МОДУЛЯ ТИПА -- подписант и его два счёта.

    Берётся не по памяти, а из таблиц самих строителей: c2_ammv4_stroitel.
    РАСКЛАДКА, c2_dbc_stroitel.РАСКЛАДКА, c2_clmm_stroitel.РАСКЛАДКИ,
    c2_whirlpool_stroitel.РАСКЛАДКИ. У сделки маршрутизатора на месте торговца
    стоит его PDA, и по подписанту сделки это место не узнать.
    """
    import importlib  # noqa: PLC0415

    модули = {
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": ("c2_ammv4_stroitel", None),
        "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": ("c2_dbc_stroitel", None),
        "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": ("c2_clmm_stroitel", "мн"),
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": ("c2_whirlpool_stroitel", "мн"),
    }
    пара = модули.get(прог)
    if not пара:
        return set()
    имя, много = пара
    try:
        M = importlib.import_module(имя)
    except Exception:  # noqa: BLE001
        return set()
    раскладки = ([getattr(M, "РАСКЛАДКИ", {}).get(в) or {}
                  for в in (getattr(M, "РАСКЛАДКИ", {}) or {})]
                 if много else [getattr(M, "РАСКЛАДКА", {}) or {}])
    места = set()
    for р in раскладки:
        if много and р.get("счетов") and int(р["счетов"]) != int(счетов):
            continue
        for ключ in ("payer", "подписант", "счёт_входа", "счёт_выхода",
                      "счёт_a", "счёт_b"):
            зн = р.get(ключ)
            if isinstance(зн, int):
                места.add(зн)
    return места


def подставляемые(B, ш: dict, tx: dict) -> set:
    """Места, куда МЫ подставляем свои счета -- замером, а не по памяти.

    Инструкция собирается двумя разными кошельками, и поехавшие места это и есть
    наши. Для способов, где шаблон переносится как есть (iz_pokupki, pary, flip),
    места берутся у c2_swap_build.user_accounts -- той же функции, которой их
    ставит сборка.
    """
    import c3_svoj_prodavec as P  # noqa: PLC0415

    А = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    Б = "D3JuFoSXuWEMUUdCtoB5NYWnN87vjJSHtDP5rTD6qnph"
    try:
        иa = P.инструкция_продажи(ш, наш_кошелёк=А, база_в=1, минимум_выхода=1)
        иb = P.инструкция_продажи(ш, наш_кошелёк=Б, база_в=1, минимум_выхода=1)
        a = [str(m.pubkey) for m in иa.accounts]
        b = [str(m.pubkey) for m in иb.accounts]
        места = {и for и, (x, y) in enumerate(zip(a, b)) if x != y}
        if места:
            return места
    except Exception:  # noqa: BLE001
        pass
    try:
        subs = B.user_accounts(ш["tpl"], tx, А) or {}
        return set(subs)
    except Exception:  # noqa: BLE001
        return set()


# Программы без восьмибайтового дискриминатора Anchor: тег -- один байт.
НЕАНКОРНЫЕ = {"675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
              "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"}


def _данные_ix(tx: dict, счета: list, прог: str) -> str:
    """Данные той инструкции, чьи счета нам передали (base58, как у узла)."""
    import c2_swap_build as B  # noqa: PLC0415

    for ix in B.all_instructions(tx):
        if ix.get("programId") == прог and list(ix.get("accounts") or []) == счета:
            return ix.get("data") or ""
    return ""


def _минты_пула(C, B, tx: dict, счета: list) -> set:
    """Минты, которые стоят в счетах этой инструкции -- для вывода ATA торговца."""
    минты = set()
    строки = {r["account"]: r for r in C.token_rows(tx).values()}
    for а in счета:
        м = (строки.get(а) or {}).get("mint")
        if м:
            минты.add(м)
    # Минт может стоять в инструкции и САМ (CLMM swap_v2, DBC, Pump AMM): тогда
    # его видно как счёт, у которого есть токеновые строки с этим же адресом.
    все_минты = {r.get("mint") for r in строки.values() if r.get("mint")}
    минты |= {а for а in счета if а in все_минты}
    return минты


def main() -> int:
    п = argparse.ArgumentParser()
    п.add_argument("--sbory", default=None,
                    help="каталог со сборами Code-2 (clmm_sbor.json и прочие)")
    п.add_argument("--moduli", default=None)
    п.add_argument("--out", default=None)
    а = п.parse_args()
    C, B, P = _модули(а.moduli)
    гр = пары(C, B, а.sbory)
    строки, свод = [], collections.OrderedDict()
    for прог, т in P.ТИПЫ.items():
        метка = т["метка"]
        пулы = гр.get(прог) or {}
        с_парой = {к: v for к, v in пулы.items()
                   if "покупка" in v and "продажа" in v}
        д = {"способ": т.get("способ") or "нет", "пулов": len(пулы),
             "продаж": sum(1 for v in пулы.values() if "продажа" in v),
             "пар": len(с_парой), "совпало": 0, "вне_наших": 0,
             "почему": collections.Counter()}
        if т.get("способ") is None:
            д["почему"][str(т.get("почему_нет"))[:70]] += 1
        else:
            for пул, v in с_парой.items():
                р = сверить(C, B, P, прог, пул, v["покупка"], v["продажа"])
                строки.append(dict(р, тип=метка))
                if р["ok"]:
                    д["совпало"] += 1
                else:
                    д["почему"][str(р["why_not"])[:70]] += 1
                    if р.get("вне_наших"):
                        д["вне_наших"] += 1
        свод[метка] = д
    print("Свой продавец против живых продаж: тип / способ / пулов / живых продаж "
          "/ пар покупка+продажа / совпало / вне наших мест")
    for метка, д in свод.items():
        print(f"  {метка:<17} {д['способ']:<11} {д['пулов']:>4} {д['продаж']:>4} "
              f"{д['пар']:>4} {д['совпало']:>4} {д['вне_наших']:>4}")
        for ч, н in д["почему"].most_common(3):
            print(f"      x{н}: {ч}")
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump({"свод": {к: dict(v, почему=dict(v["почему"]))
                                 for к, v in свод.items()},
                        "строки": строки}, ф, ensure_ascii=False, indent=1,
                      default=str)
        print(f"  отчёт: {а.out}")
    return 1 if any(д["вне_наших"] for д in свод.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
