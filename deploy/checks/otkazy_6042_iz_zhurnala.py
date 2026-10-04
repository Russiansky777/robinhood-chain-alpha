#!/usr/bin/env python3
"""Отказы 6042 (BuySlippageBelowMinTokensOut) по журналам хоста -- выжимка.

ЗАЧЕМ (слово владельца 04.10, вечер). Два отказа 6042 подряд на cand1_03 /
6qudAN2k. Нужно: сколько таких отказов за сутки полосы по группам и источникам,
какая это доля попыток покупки на кривой, и по последним случаям -- слоты,
ожидаемый выход, минимум выхода и допуск группы.

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
# Код ошибки программы: программа посчитала выход МЕНЬШЕ нашего min_tokens_out.
КОД_6042 = 6042

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


def есть_6042(запись: dict) -> bool:
    """Ошибка 6042 в записи позиции или решения.

    Ищется В ТЕКСТЕ ошибки, а не по вложенной раскладке: служба кладёт её
    двумя видами -- словарём chain_err и строкой lane_bought_why_not
    ("транзакция упала: {...}"), и номер инструкции в раскладке меняется.
    Чтобы «6042» в чужом поле (минт, подпись) не прошло за ошибку, смотрим
    ТОЛЬКО поля ошибки.
    """
    куски = []
    for поле in ("chain_err", "lane_bought_why_not", "why_not", "closed_reason",
                 "lane_not_landed_why", "err"):
        зн = запись.get(поле)
        if зн:
            куски.append(зн if isinstance(зн, str)
                         else json.dumps(зн, ensure_ascii=False))
    текст = " ".join(куски)
    if not текст:
        return False
    # "Custom": 6042 / Custom: 6042 / {'Custom': 6042}
    return bool(re.search(r"Custom['\"]?\s*:\s*%d\b" % КОД_6042, текст))


def кривая_ли(запись: dict) -> bool:
    """Покупка по кривой pump.fun -- по адресу программы пула из записи."""
    for поле in ("program", "pool_program", "stroitel", "строитель"):
        if str(запись.get(поле) or "") == КРИВАЯ_PUMPFUN:
            return True
    return False


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


def разобрать(позиции, *, порог: float) -> dict:
    """Чистая функция: итог по потоку записей позиций."""
    из_: dict = {"порог_ts": порог, "позиций_просмотрено": 0,
                 "попыток_полосы": 0, "попыток_кривой": 0,
                 "отказов_6042": 0, "по_группам": {}, "по_источникам": {},
                 "случаи": []}
    for з in позиции:
        из_["позиций_просмотрено"] += 1
        т = з.get("ts_intent")
        if isinstance(т, (int, float)) and float(т) < порог:
            continue
        if not попытка_покупки(з):
            continue
        из_["попыток_полосы"] += 1
        на_кривой = кривая_ли(з)
        if на_кривой:
            из_["попыток_кривой"] += 1
        if not есть_6042(з):
            continue
        из_["отказов_6042"] += 1
        гр = str(з.get("lane_group") or "?")
        ист = str(з.get("source") or "?")
        г = из_["по_группам"].setdefault(гр, {"всего": 0, "на_кривой": 0})
        г["всего"] += 1
        г["на_кривой"] += 1 if на_кривой else 0
        и_ = из_["по_источникам"].setdefault(ист, {"всего": 0, "группы": {}})
        и_["всего"] += 1
        и_["группы"][гр] = и_["группы"].get(гр, 0) + 1
        случай = {к: з.get(к) for к in ПОЛЯ_СЛУЧАЯ if к in з}
        случай["на_кривой"] = на_кривой
        # ДОЛЯ МИНИМУМА ОТ ОЖИДАЕМОГО -- ЭТО И ЕСТЬ ПРИМЕНЁННЫЙ ДОПУСК, взятый
        # из тех самых чисел, которыми подписана транзакция, а не из файла
        # групп. Расходится с политикой -- значит в бой уехало другое число.
        ож, мин = з.get("lane_expected_out"), з.get("lane_min_out")
        if isinstance(ож, int) and isinstance(мин, int) and ож > 0:
            случай["dolya_min_ot_ozhid"] = round(мин / ож, 6)
            случай["dopusk_iz_bajtov_pct"] = round((1 - мин / ож) * 100, 3)
        из_["случаи"].append(случай)
    из_["случаи"].sort(key=lambda с: float(с.get("ts_intent") or 0), reverse=True)
    if из_["попыток_кривой"]:
        на_кр = sum(г["на_кривой"] for г in из_["по_группам"].values())
        из_["dolya_6042_ot_popytok_krivoj_pct"] = round(
            на_кр / из_["попыток_кривой"] * 100, 3)
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

    # --- узнавание ошибки: оба вида записи и чужое «6042» ---
    сверить("ошибка словарём chain_err узнаётся",
            есть_6042({"chain_err": {"InstructionError": [6, {"Custom": 6042}]}}),
            True)
    сверить("ошибка строкой lane_bought_why_not узнаётся",
            есть_6042({"lane_bought_why_not":
                       'транзакция упала: {"InstructionError": [6, {"Custom": 6042}]}'}),
            True)
    сверить("номер инструкции не важен",
            есть_6042({"chain_err": {"InstructionError": [3, {"Custom": 6042}]}}),
            True)
    сверить("чужой код 6040 за 6042 не принимается",
            есть_6042({"chain_err": {"InstructionError": [6, {"Custom": 6040}]}}),
            False)
    сверить("код 60420 за 6042 не принимается (граница слова)",
            есть_6042({"chain_err": {"InstructionError": [6, {"Custom": 60420}]}}),
            False)
    сверить("«6042» в минте за ошибку не принимается",
            есть_6042({"mint": "6042abc", "chain_err": None}), False)
    сверить("пустая запись -- не ошибка", есть_6042({}), False)

    # --- кривая и попытка ---
    сверить("кривая узнаётся по полю program",
            кривая_ли({"program": КРИВАЯ_PUMPFUN}), True)
    сверить("Pump AMM за кривую не принимается",
            кривая_ли({"program": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"}),
            False)
    сверить("попытка -- только у полосы и только со следом отправки",
            (попытка_покупки({"lane": "own_send", "lane_signature": "S"}),
             попытка_покупки({"lane": "own_send"}),
             попытка_покупки({"lane_signature": "S"})),
            (True, False, False))

    # --- свод ---
    пз = [
        {"lane": "own_send", "lane_group": "cand1_03", "source": "6qudAN2k",
         "program": КРИВАЯ_PUMPFUN, "lane_signature": "S1", "ts_intent": 200.0,
         "chain_err": {"InstructionError": [6, {"Custom": 6042}]},
         "lane_expected_out": 1000, "lane_min_out": 650, "mint": "M1"},
        {"lane": "own_send", "lane_group": "cand1_03", "source": "6qudAN2k",
         "program": КРИВАЯ_PUMPFUN, "lane_signature": "S2", "ts_intent": 300.0,
         "lane_bought_why_not": 'транзакция упала: {"InstructionError": [6, {"Custom": 6042}]}',
         "lane_expected_out": 2000, "lane_min_out": 1300, "mint": "M2"},
        # удачная покупка по кривой -- в попытки идёт, в отказы нет
        {"lane": "own_send", "lane_group": "cand2", "source": "ДРУГОЙ",
         "program": КРИВАЯ_PUMPFUN, "lane_signature": "S3", "ts_intent": 250.0},
        # отказ 6042 НЕ на кривой -- считается в отказы, но не в долю кривой
        {"lane": "own_send", "lane_group": "cand2", "source": "ДРУГОЙ",
         "program": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
         "lane_signature": "S4", "ts_intent": 260.0,
         "chain_err": {"InstructionError": [6, {"Custom": 6042}]}},
        # до порога -- не берём вовсе
        {"lane": "own_send", "lane_group": "cand1_03", "source": "6qudAN2k",
         "program": КРИВАЯ_PUMPFUN, "lane_signature": "S0", "ts_intent": 100.0,
         "chain_err": {"InstructionError": [6, {"Custom": 6042}]}},
        # чужая позиция (площадка) -- не попытка полосы
        {"lane_group": "cand2", "lane_signature": "X", "ts_intent": 280.0},
    ]
    и = разобрать(пз, порог=150.0)
    сверить("попыток полосы за окно", и["попыток_полосы"], 4)
    сверить("попыток по кривой за окно", и["попыток_кривой"], 3)
    сверить("отказов 6042 за окно", и["отказов_6042"], 3)
    сверить("по группам", и["по_группам"],
            {"cand1_03": {"всего": 2, "на_кривой": 2},
             "cand2": {"всего": 1, "на_кривой": 0}})
    сверить("по источникам", и["по_источникам"],
            {"6qudAN2k": {"всего": 2, "группы": {"cand1_03": 2}},
             "ДРУГОЙ": {"всего": 1, "группы": {"cand2": 1}}})
    сверить("доля 6042 от попыток кривой -- по отказам НА КРИВОЙ",
            и["dolya_6042_ot_popytok_krivoj_pct"], round(2 / 3 * 100, 3))
    _по_минту = {с.get("mint"): с for с in и["случаи"]}
    сверить("допуск из подписанных байтов посчитан у обоих случаев кривой",
            (_по_минту["M2"]["dopusk_iz_bajtov_pct"],
             _по_минту["M1"]["dopusk_iz_bajtov_pct"]), (35.0, 35.0))
    # БЕЗ ЧИСЕЛ ВЫХОДА ДОЛИ НЕТ, А НЕ НОЛЬ: ноль читался бы как «допуск 0 %».
    сверить("у случая без expected_out доли допуска нет вовсе",
            "dopusk_iz_bajtov_pct" in [с for с in и["случаи"]
                                        if с.get("lane_signature") == "S4"][0],
            False)
    сверить("случаи отданы от свежих к старым",
            [с["mint"] for с in и["случаи"] if с.get("mint")], ["M2", "M1"])
    сверить("запись до порога в случаи не попала",
            any(с.get("lane_signature") == "S0" for с in и["случаи"]), False)
    # ПУСТОЙ ПОТОК -- НУЛИ И НИ ОДНОЙ ДОЛИ, а не падение на делении.
    п = разобрать([], порог=0.0)
    сверить("пустой поток: нули и доли нет",
            (п["отказов_6042"], "dolya_6042_ot_popytok_krivoj_pct" in п),
            (0, False))
    print(f"проверок {проверок}, прошло {прошло}, "
          f"не прошло {проверок - прошло}")
    return 0 if прошло == проверок else 1


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default=os.environ.get("BLOOM_STATE_DIR")
                   or "/home/bot/bloom_executor_live_data")
    р.add_argument("--since-utc", required=True)
    р.add_argument("--out", default="/tmp/otkazy_6042.json")
    а = р.parse_args()
    порог = в_секунды(а.since_utc)

    # ПОСЛЕДНЯЯ ЗАПИСЬ ПО cid -- ПРАВДА. positions.jsonl дописывается на каждое
    # изменение позиции, и ранняя строка той же покупки ошибки ещё не знает.
    по_cid: dict = {}
    for путь in файлы(а.state_dir, "positions.jsonl"):
        for з in строки(путь):
            cid = з.get("client_order_id")
            if cid:
                по_cid[cid] = {**(по_cid.get(cid) or {}), **з}
    итог = разобрать(по_cid.values(), порог=порог)
    итог["since_utc"] = а.since_utc
    итог["state_dir"] = а.state_dir
    итог["snjato_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False, indent=1)
    print(f"позиций {итог['позиций_просмотрено']}, попыток полосы "
          f"{итог['попыток_полосы']}, по кривой {итог['попыток_кривой']}, "
          f"отказов 6042 {итог['отказов_6042']}")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_самопроверка())
    sys.exit(main())
