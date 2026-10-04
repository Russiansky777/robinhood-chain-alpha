#!/usr/bin/env python3
"""Отказы по допуску цены (6042 кривой и 6040 Pump AMM) -- выжимка с хоста.

ЗАЧЕМ (слово владельца 04.10, вечер, и дополнение к нему). Три отказа по
допуску за 17 минут: два 6042 на cand1_03 / 6qudAN2k (кривая pump.fun,
BuySlippageBelowMinTokensOut) и один 6040 на lane_s0 / Fvkc2thk (по IDL --
Pump AMM, BuySlippageBelowMinBaseAmountOut). Нужно: сколько таких отказов за
сутки полосы И за прошлые сутки, по группам и источникам, какая это доля
попыток покупки на ЭТИХ типах пулов, участились ли они вечером, и выросла ли
задержка «слот источника -> наш слот» после сегодняшних деплоев.

ОКНА ПЕРЕДАЮТСЯ ФАЙЛОМ, А НЕ ДОВОДАМИ. В команду root на торговом хосте не
должно попасть ни одного знака из входов прогона -- так же сделана правка файла
групп после разбора дыры 04.10.

ЗДЕСЬ ТОЛЬКО ВЫЖИМКА ИЗ ЖУРНАЛОВ. Цепь спрашивает вторая часть на облачном
бегунке: ключ узла для этого разбора на хосте не нужен.

ТОЛЬКО ЧТЕНИЕ, потоком: журналы в память не грузим, на хосте ничего не пишем
кроме файла выжимки в /tmp.

СУТКИ ПОЛОСЫ -- ОТ 22:00Z (полночь Мадрида), как их считает сама служба. Порог
передаётся доводом, чтобы в этом файле не появилось второго написания правила.
"""
from __future__ import annotations

import argparse
import calendar
import gzip
import json
import os
import re
import sys
import time

# Программа кривой pump.fun. Имя константы и значение -- те же, что у сборщика
# (c2_swap_build.BONDING); здесь оно повторено ОДНОЙ строкой нарочно: выжимка
# ходит на хост отдельным файлом и модулей службы не импортирует.
КРИВАЯ_PUMPFUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# Коды отказов ПО ДОПУСКУ ЦЕНЫ, по типу пула. Имя кода -- из IDL программы;
# номер инструкции в раскладке ошибки меняется и в признак не идёт.
КОДЫ_ДОПУСКА = {
    6042: {"tip": "bonding", "imya": "BuySlippageBelowMinTokensOut",
            "programma": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"},
    6040: {"tip": "pump_amm", "imya": "BuySlippageBelowMinBaseAmountOut",
            "programma": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"},
}
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# Типы пулов, по которым считается доля отказов: только те, у которых такой код
# вообще есть. Доля от ВСЕХ попыток полосы ответила бы на другой вопрос.
ТИПЫ_ДОЛИ = {"bonding": КРИВАЯ_PUMPFUN, "pump_amm": PUMP_AMM}

ПОЛЯ_СЛУЧАЯ = (
    "client_order_id", "mint", "token_name", "lane", "lane_group", "source",
    "source_sig", "source_slot", "sol_in", "state", "chain_ok",
    "ts_intent", "ts_intent_utc", "program", "program_gen",
    "lane_signature", "lane_signature_local", "lane_landed_signature",
    "lane_landed_slot", "lane_bought_raw", "lane_bought_why_not",
    "lane_expected_out", "lane_min_out", "lane_build_ms",
    "lane_priority_lamports", "lane_tips_total_sol",
    "our_block_index", "our_block_total", "source_block_index",
    "source_block_total", "lane_buy_fee_sol", "lane_buy_native_sol",
    "closed_reason", "ix_raznovidnost", "own_sell_raznovidnost",
)


def строки(путь):
    откр = gzip.open if путь.endswith(".gz") else open
    with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
        for ln in ф:
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


def файлы(каталог, имя):
    из_ = [os.path.join(каталог, имя)]
    try:
        из_ += sorted(os.path.join(каталог, ф) for ф in os.listdir(каталог)
                      if ф.startswith(имя + ".") and ф.endswith(".gz"))
    except OSError:
        pass
    return [п for п in из_ if os.path.exists(п)]


def в_секунды(s: str) -> float:
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))


def текст_ошибки(запись: dict) -> str:
    """Склеенный текст ТОЛЬКО полей ошибки.

    Служба кладёт ошибку двумя видами -- словарём chain_err и строкой
    lane_bought_why_not ("транзакция упала: {...}"). Смотреть весь JSON записи
    нельзя: «6042» встречается в минтах и подписях и прошло бы за ошибку.
    """
    куски = []
    for поле in ("chain_err", "lane_bought_why_not", "why_not", "closed_reason",
                 "lane_not_landed_why", "err"):
        зн = запись.get(поле)
        if зн:
            куски.append(зн if isinstance(зн, str)
                         else json.dumps(зн, ensure_ascii=False))
    return " ".join(куски)


def код_допуска(запись: dict):
    """Код отказа по допуску цены из записи, или None.

    Номер инструкции в раскладке ошибки не важен и в признак не идёт: у кривой
    это 6, у Pump AMM 7, и завтра может быть другой. Граница слова обязательна:
    60420 -- не 6042.
    """
    текст = текст_ошибки(запись)
    if not текст:
        return None
    for код in КОДЫ_ДОПУСКА:
        if re.search(r"Custom['\"]?\s*:\s*%d(?!\d)" % код, текст):
            return код
    return None


def номер_инструкции(запись: dict):
    """Номер инструкции из раскладки ошибки -- для отчёта, не для признака."""
    м = re.search(r"InstructionError['\"]?\s*:\s*\[\s*(\d+)",
                  текст_ошибки(запись))
    return int(м.group(1)) if м else None


def тип_пула(запись: dict):
    """Имя типа пула по адресу программы из записи, или None.

    Берутся только те типы, у которых есть свой код отказа по допуску: доля
    считается от попыток НА ЭТИХ типах, а не от всех попыток полосы.
    """
    for поле in ("program", "pool_program", "stroitel", "строитель"):
        адрес = str(запись.get(поле) or "")
        for имя, прог in ТИПЫ_ДОЛИ.items():
            if адрес == прог:
                return имя
    return None


def попытка_покупки(запись: dict) -> bool:
    """Полоса ПЫТАЛАСЬ отправить покупку по этой позиции.

    Признак -- след отправки в записи, а не состояние: позиция могла уже
    закрыться, а нам важно, была ли попытка. Отказ 6042 приходит ПОСЛЕ
    отправки, поэтому доля считается именно от попыток.
    """
    if str(запись.get("lane") or "") != "own_send":
        return False
    return any(запись.get(п) for п in
               ("lane_signature", "lane_signature_local", "lane_landed_signature",
                "lane_bought_try_ts", "ts_sent"))


def процентиль(ряд: list, доля: float):
    """Процентиль по отсортированному ряду, ближайшим рангом. Пусто -- None."""
    if not ряд:
        return None
    р = sorted(ряд)
    и = min(len(р) - 1, max(0, int(round(доля * (len(р) - 1)))))
    return р[и]


def задержка_слотов(запись: dict):
    """Сколько слотов прошло от покупки источника до НАШЕЙ транзакции.

    Считается только когда известны ОБА слота и наш не раньше источника:
    отрицательное значило бы, что мы сели до него, а это не задержка, а ошибка
    чтения -- и молча считать её нулём нельзя.
    """
    с, н = запись.get("source_slot"), запись.get("lane_landed_slot")
    if not isinstance(с, int) or not isinstance(н, int) or н < с:
        return None
    return н - с


def разобрать(позиции, *, окна: dict) -> dict:
    """Чистая функция: свод по НЕСКОЛЬКИМ окнам плюс общий список случаев.

    окна -- {имя: (с_ts, до_ts)}; до_ts=None значит «до сейчас». Позиция
    попадает в каждое окно, которому подходит её ts_intent: окна нарочно могут
    пересекаться (сутки и вечер этих же суток).
    """
    из_: dict = {"позиций_просмотрено": 0, "okna": {}, "случаи": []}
    пусто = {"попыток_полосы": 0, "попыток_po_tipam": {},
             "otkazov": {}, "po_gruppam": {}, "po_istochnikam": {},
             "zaderzhka_slotov": []}
    for имя in окна:
        из_["okna"][имя] = {к: (dict(v) if isinstance(v, dict)
                                else list(v) if isinstance(v, list) else v)
                            for к, v in пусто.items()}
    for з in позиции:
        из_["позиций_просмотрено"] += 1
        if not попытка_покупки(з):
            continue
        т = з.get("ts_intent")
        if not isinstance(т, (int, float)):
            continue
        т = float(т)
        тип = тип_пула(з)
        код = код_допуска(з)
        зад = задержка_слотов(з)
        for имя, (с_, до_) in окна.items():
            if т < float(с_) or (до_ is not None and т > float(до_)):
                continue
            о = из_["okna"][имя]
            о["попыток_полосы"] += 1
            if тип:
                о["попыток_po_tipam"][тип] = о["попыток_po_tipam"].get(тип, 0) + 1
            if зад is not None:
                о["zaderzhka_slotov"].append(зад)
            if код is None:
                continue
            к_ = str(код)
            о["otkazov"][к_] = о["otkazov"].get(к_, 0) + 1
            гр = str(з.get("lane_group") or "?")
            о["po_gruppam"].setdefault(гр, {})[к_] = \
                о["po_gruppam"].setdefault(гр, {}).get(к_, 0) + 1
            ист = str(з.get("source") or "?")
            о["po_istochnikam"].setdefault(ист, {})[к_] = \
                о["po_istochnikam"].setdefault(ист, {}).get(к_, 0) + 1
        if код is None:
            continue
        случай = {к: з.get(к) for к in ПОЛЯ_СЛУЧАЯ if к in з}
        случай["kod"] = код
        случай["kod_imya"] = КОДЫ_ДОПУСКА[код]["imya"]
        случай["tip_pula_po_zapisi"] = тип
        случай["programma_koda_po_idl"] = КОДЫ_ДОПУСКА[код]["programma"]
        случай["nomer_instrukcii"] = номер_инструкции(з)
        случай["zaderzhka_slotov"] = зад
        ож, мин = з.get("lane_expected_out"), з.get("lane_min_out")
        if isinstance(ож, int) and isinstance(мин, int) and ож > 0:
            случай["dolya_min_ot_ozhid"] = round(мин / ож, 6)
            случай["dopusk_iz_bajtov_pct"] = round((1 - мин / ож) * 100, 3)
        из_["случаи"].append(случай)
    из_["случаи"].sort(key=lambda с: float(с.get("ts_intent") or 0), reverse=True)
    # ИТОГИ ПО ОКНАМ: доли и процентили считаются ПОСЛЕ прохода, по собранным
    # рядам, а не накапливаются по дороге.
    for имя, о in из_["okna"].items():
        ряд = о.pop("zaderzhka_slotov")
        о["zaderzhka"] = {"n": len(ряд), "mediana": процентиль(ряд, 0.5),
                          "p90": процентиль(ряд, 0.9),
                          "max": (max(ряд) if ряд else None)}
        доли = {}
        for код, вид in КОДЫ_ДОПУСКА.items():
            попыток = о["попыток_po_tipam"].get(вид["tip"], 0)
            отказов = о["otkazov"].get(str(код), 0)
            доли[str(код)] = {"tip": вид["tip"], "popytok": попыток,
                              "otkazov": отказов,
                              "dolya_pct": (round(отказов / попыток * 100, 3)
                                            if попыток else None)}
        о["doli"] = доли
    return из_


def _самопроверка() -> int:
    проверок = прошло = 0

    def сверить(что, дано, ждали):
        nonlocal проверок, прошло
        проверок += 1
        if дано == ждали:
            прошло += 1
        else:
            print(f"НЕ ПРОШЛО: {что}: дано {дано!r}, ждали {ждали!r}")

    # --- какой именно код, оба вида записи, граница слова ---
    сверить("6042 словарём chain_err",
            код_допуска({"chain_err": {"InstructionError": [6, {"Custom": 6042}]}}),
            6042)
    сверить("6040 словарём chain_err",
            код_допуска({"chain_err": {"InstructionError": [7, {"Custom": 6040}]}}),
            6040)
    сверить("6042 строкой lane_bought_why_not",
            код_допуска({"lane_bought_why_not":
                         'транзакция упала: {"InstructionError": [6, {"Custom": 6042}]}'}),
            6042)
    сверить("номер инструкции на признак не влияет",
            код_допуска({"chain_err": {"InstructionError": [3, {"Custom": 6040}]}}),
            6040)
    сверить("чужой код 6001 не принимается",
            код_допуска({"chain_err": {"InstructionError": [6, {"Custom": 6001}]}}),
            None)
    сверить("60420 за 6042 не принимается (граница слова)",
            код_допуска({"chain_err": {"InstructionError": [6, {"Custom": 60420}]}}),
            None)
    сверить("«6042» в минте за ошибку не принимается",
            код_допуска({"mint": "6042abc", "chain_err": None}), None)
    сверить("номер инструкции читается для отчёта",
            (номер_инструкции({"chain_err": {"InstructionError": [7, {"Custom": 6040}]}}),
             номер_инструкции({})), (7, None))

    # --- тип пула ---
    сверить("типы пулов узнаются по программе",
            (тип_пула({"program": КРИВАЯ_PUMPFUN}),
             тип_пула({"program": PUMP_AMM}),
             тип_пула({"program": "ЧУЖАЯ"})),
            ("bonding", "pump_amm", None))
    сверить("попытка -- только у полосы и только со следом отправки",
            (попытка_покупки({"lane": "own_send", "lane_signature": "S"}),
             попытка_покупки({"lane": "own_send"}),
             попытка_покупки({"lane_signature": "S"})),
            (True, False, False))

    # --- задержка ---
    сверить("задержка -- разница слотов",
            задержка_слотов({"source_slot": 10, "lane_landed_slot": 12}), 2)
    сверить("наш слот РАНЬШЕ источника -- не задержка, а None",
            задержка_слотов({"source_slot": 10, "lane_landed_slot": 9}), None)
    сверить("нет слота -- None, а не ноль",
            задержка_слотов({"source_slot": 10}), None)
    сверить("процентиль пустого ряда -- None",
            (процентиль([], 0.5), процентиль([1, 2, 3, 4, 5], 0.5),
             процентиль([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9)),
            (None, 3, 9))

    # --- свод по окнам ---
    пз = [
        # вчера: одна попытка кривой, один отказ 6042, задержка 1
        {"lane": "own_send", "lane_group": "cand1_03", "source": "A",
         "program": КРИВАЯ_PUMPFUN, "lane_signature": "S1", "ts_intent": 100.0,
         "chain_err": {"InstructionError": [6, {"Custom": 6042}]},
         "source_slot": 10, "lane_landed_slot": 11,
         "lane_expected_out": 1000, "lane_min_out": 650, "mint": "M1"},
        # вчера: удачная покупка кривой, задержка 3
        {"lane": "own_send", "lane_group": "cand1_03", "source": "A",
         "program": КРИВАЯ_PUMPFUN, "lane_signature": "S2", "ts_intent": 110.0,
         "source_slot": 20, "lane_landed_slot": 23},
        # сегодня вечером: отказ 6040 на Pump AMM, задержка 5
        {"lane": "own_send", "lane_group": "lane_s0", "source": "B",
         "program": PUMP_AMM, "lane_signature": "S3", "ts_intent": 300.0,
         "chain_err": {"InstructionError": [7, {"Custom": 6040}]},
         "source_slot": 30, "lane_landed_slot": 35, "mint": "M2"},
        # сегодня вечером: удачная покупка Pump AMM, задержка 7
        {"lane": "own_send", "lane_group": "lane_s0", "source": "B",
         "program": PUMP_AMM, "lane_signature": "S4", "ts_intent": 310.0,
         "source_slot": 40, "lane_landed_slot": 47},
    ]
    окна = {"vchera": (50.0, 200.0), "segodnya": (250.0, None)}
    и = разобрать(пз, окна=окна)
    в, с = и["okna"]["vchera"], и["okna"]["segodnya"]
    сверить("попыток по окнам", (в["попыток_полосы"], с["попыток_полосы"]), (2, 2))
    сверить("попытки по типам пулов разнесены",
            (в["попыток_po_tipam"], с["попыток_po_tipam"]),
            ({"bonding": 2}, {"pump_amm": 2}))
    сверить("отказы по кодам разнесены по окнам",
            (в["otkazov"], с["otkazov"]), ({"6042": 1}, {"6040": 1}))
    сверить("доля 6042 вчера -- от попыток КРИВОЙ",
            в["doli"]["6042"]["dolya_pct"], 50.0)
    сверить("доля 6040 вчера -- попыток Pump AMM не было, доли нет",
            (в["doli"]["6040"]["popytok"], в["doli"]["6040"]["dolya_pct"]),
            (0, None))
    сверить("доля 6040 сегодня -- от попыток Pump AMM",
            с["doli"]["6040"]["dolya_pct"], 50.0)
    сверить("задержка по окнам: медиана и p90",
            (в["zaderzhka"]["n"], в["zaderzhka"]["mediana"],
             с["zaderzhka"]["n"], с["zaderzhka"]["mediana"]),
            (2, 1, 2, 5))
    сверить("группы и источники по окнам",
            (в["po_gruppam"], с["po_istochnikam"]),
            ({"cand1_03": {"6042": 1}}, {"B": {"6040": 1}}))
    сверить("случаи отданы от свежих к старым с кодом и именем",
            [(с_["mint"], с_["kod"], с_["kod_imya"][:8]) for с_ in и["случаи"]],
            [("M2", 6040, "BuySlipp"), ("M1", 6042, "BuySlipp")])
    сверить("номер инструкции и программа кода по IDL названы",
            [(с_["nomer_instrukcii"], с_["programma_koda_po_idl"][:8])
             for с_ in и["случаи"]],
            [(7, "pAMMBay6"), (6, "6EF8rrec")])
    сверить("допуск из подписанных байтов посчитан там, где есть числа",
            [с_.get("dopusk_iz_bajtov_pct") for с_ in и["случаи"]],
            [None, 35.0])
    # ПОЗИЦИЯ ВНЕ ВСЕХ ОКОН в своды не идёт, но в случаи попадает: список
    # случаев общий нарочно -- по нему разбирается цепь.
    и2 = разобрать(пз, окна={"узкое": (305.0, 306.0)})
    сверить("вне окна попыток нет, а случаи остаются все",
            (и2["okna"]["узкое"]["попыток_полосы"], len(и2["случаи"])), (0, 2))
    # ПУСТОЙ ПОТОК -- нули, None в процентилях и ни одного деления на ноль.
    п = разобрать([], окна={"пусто": (0.0, None)})
    сверить("пустой поток: нули и None, без падения",
            (п["okna"]["пусто"]["попыток_полосы"],
             п["okna"]["пусто"]["zaderzhka"]["mediana"],
             п["okna"]["пусто"]["doli"]["6042"]["dolya_pct"]),
            (0, None, None))
    print(f"проверок {проверок}, прошло {прошло}, "
          f"не прошло {проверок - прошло}")
    return 0 if прошло == проверок else 1


def окна_из_файла(путь: str) -> dict:
    """{имя: (с_ts, до_ts)} из файла окон. до='' или null -- до сейчас.

    ОКНА ПРИХОДЯТ ФАЙЛОМ, А НЕ ДОВОДАМИ: в команду root на торговом хосте не
    должно попасть ни одного знака из входов прогона.
    """
    д = json.loads(open(путь, encoding="utf-8").read())
    из_ = {}
    for имя, пара in (д.get("okna") or {}).items():
        с_ = в_секунды(пара[0])
        до_ = в_секунды(пара[1]) if len(пара) > 1 and пара[1] else None
        из_[str(имя)] = (с_, до_)
    if not из_:
        raise ValueError("в файле окон нет раздела okna")
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default=os.environ.get("BLOOM_STATE_DIR")
                   or "/home/bot/bloom_executor_live_data")
    р.add_argument("--okna", required=True, help="файл с окнами")
    р.add_argument("--out", default="/tmp/otkazy_dopuska.json")
    а = р.parse_args()
    окна = окна_из_файла(а.okna)

    # ПОСЛЕДНЯЯ ЗАПИСЬ ПО cid -- ПРАВДА. positions.jsonl дописывается на каждое
    # изменение позиции, и ранняя строка той же покупки ошибки ещё не знает.
    по_cid: dict = {}
    for путь in файлы(а.state_dir, "positions.jsonl"):
        for з in строки(путь):
            cid = з.get("client_order_id")
            if cid:
                по_cid[cid] = {**(по_cid.get(cid) or {}), **з}
    итог = разобрать(по_cid.values(), окна=окна)
    итог["okna_utc"] = {и_: [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(с_)),
                             (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(до_))
                              if до_ else None)]
                        for и_, (с_, до_) in окна.items()}
    итог["state_dir"] = а.state_dir
    итог["snjato_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False, indent=1)
    print(f"позиций {итог['позиций_просмотрено']}, окон {len(окна)}, "
          f"случаев отказа по допуску {len(итог['случаи'])}")
    for и_, о in итог["okna"].items():
        print(f"  {и_}: попыток {о['попыток_полосы']}, отказов {о['otkazov']}, "
              f"задержка медиана {о['zaderzhka']['mediana']} p90 "
              f"{о['zaderzhka']['p90']} (n {о['zaderzhka']['n']})")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_самопроверка())
    sys.exit(main())
