#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ЧИСЛО ДЛЯ ШТАБА: USDC-НОГА ПО ТИПАМ ПУЛОВ -- сколько сигналов собралось бы.

ЭТО ЗАМЕР ПО ОБРАЗЦАМ CODE-2, А НЕ МОДУЛЬ. Образцы -- выход
analysis/podbivka_usdc_noga.py (ветка claude/podbivka): сигналы «котировка USDC в
пуле НЕ Raydium CPMM и НЕ Pump AMM» денежных групп 28--30.09 вместе с СЫРЫМИ
транзакциями покупок. На момент написания файла
data/podbivka/usdc_noga_dlya_code3.json НЕ СУЩЕСТВУЕТ: сборщик в ветке есть, выход
не записан. Поэтому прогон здесь -- условие, а не действие: появится файл -- три
числа по каждому типу выдаст эта страница, и типы, у которых их нет, готовыми не
считаются.

ЧТО СЧИТАЕТСЯ -- ПО КАЖДОМУ ТИПУ ТРИ ЧИСЛА, и складывать их нельзя:
  1. СИГНАЛОВ -- сколько в образцах, и у скольких есть транзакция.
  2. СТОРОНА USDC РАЗОБРАЛАСЬ -- c2_usdc_noga.storona() назвала вход USDC, базу и
     программы сторон. Отказ тут -- это отказ ПОЛОСЫ от сигнала, с причиной
     словами (продажа у типа без переворота стороны, не тот пул, точный выход).
  3. БАЙТ В БАЙТ -- пересобранная инструкция совпала с инструкцией ИСТОЧНИКА
     целиком, либо разошлась ТОЛЬКО на наших местах (там у источника стоит счёт
     того же минта, но не наш ATA: его собственный счёт не-ATA либо счёт
     маршрутизатора). Расхождение ВНЕ наших мест -- провал, и он печатается
     подписью сделки, а не числом.
     ОТДЕЛЬНЫМ ЧИСЛОМ -- РАЗВЁРНУТЫЕ: когда сделка источника была ПРОДАЖЕЙ,
     байт в байт с его инструкцией нет и быть не может (стороны переставлены),
     и сверка идёт с шаблоном ПОКУПКИ. Складывать эти два числа нельзя.
  4. ЦЕНА СОБЫТИЯ -- посчиталась или «своп источника шёл в другую сторону».

Наши места находятся ЗАМЕРОМ: та же инструкция собирается вторым кошельком, и
поехавшие места -- это и есть места подстановки. По памяти их тут не берут.

Запуск: python3 deploy/checks/usdc_noga_chislo.py [--obrazcy <путь>] [--out <файл>]
Выход 2 -- файла образцов нет (молчаливый пропуск считается провалом).
"""
from __future__ import annotations

import argparse
import collections
import json
import struct
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[2]
ПО_УМОЛЧАНИЮ = КОРЕНЬ / "data" / "podbivka" / "usdc_noga_dlya_code3.json"
ДРУГОЙ_КОШЕЛЁК = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


def _модули(путь: str | None = None):
    sys.path.insert(0, str(Path(путь) if путь else КОРЕНЬ / "analysis"))
    import c2_common as C  # noqa: PLC0415
    import c2_usdc_noga as U  # noqa: PLC0415
    return C, U


def строка(сигнал: dict, C, U) -> dict:
    """Одна строка замера по одному сигналу образцов."""
    из_ = {"signature": сигнал.get("signature"), "тип": сигнал.get("тип"),
            "группа": сигнал.get("группа"), "пул": сигнал.get("пул"),
            "минт": сигнал.get("минт_токена"), "usdc": сигнал.get("usdc"),
            "сторона": None, "почему_сторона": None, "цена": None,
            "почему_цена": None, "байт_в_байт": None, "расхождения": None,
            "вне_наших_мест": None}
    tx = сигнал.get("tx")
    if not isinstance(tx, dict) or "meta" not in tx:
        из_["почему_сторона"] = "в образце нет транзакции целиком"
        return из_
    прог = сигнал.get("программа_пула")
    ст = U.storona(tx, programma=прог, pul=сигнал.get("пул"))
    из_["сторона"] = bool(ст.get("ok"))
    из_["почему_сторона"] = ст.get("why_not")
    if not ст.get("ok"):
        return из_
    из_["развёрнут"] = ст.get("razvernut")
    ц = U.cena_sobytiya(tx, ст["tpl"], base_mint=ст["base_mint"])
    из_["цена"] = bool(ц.get("ok"))
    из_["почему_цена"] = ц.get("why_not")
    u = U.polzovatel_istochnika(ст, tx)
    if not u:
        из_["почему_сторона"] = "пользователь источника в инструкции не найден"
        return из_
    src = ст["src_tpl"]
    a0, a1 = struct.unpack_from("<QQ", src["data"], 8)
    наш = U.instrukciya_nogi(ст, user=u, amount_in=a0, min_out=a1,
                             tx_istochnika=tx, kak_u_istochnika=True)
    его = list(ст["tpl"]["accounts"])
    получили = [str(м.pubkey) for м in наш.accounts]
    данные_те_же = bytes(наш.data) == bytes(src["data"])
    if данные_те_же and получили == его:
        из_["байт_в_байт"] = True
        из_["расхождения"] = []
        из_["вне_наших_мест"] = []
        return из_
    другой = U.instrukciya_nogi(ст, user=ДРУГОЙ_КОШЕЛЁК, amount_in=a0, min_out=a1,
                                tx_istochnika=tx, kak_u_istochnika=True)
    иначе = [str(м.pubkey) for м in другой.accounts]
    наши_места = {и for и, (x, y) in enumerate(zip(получили, иначе)) if x != y}
    строки_баланса = {r["account"]: r for r in C.token_rows(tx).values()}
    разн = [и for и, (a, b) in enumerate(zip(его, получили)) if a != b]
    вне = [и for и in разн
           if и not in наши_места
           or (строки_баланса.get(его[и]) or {}).get("mint") not in (U.USDC,
                                                                      ст["base_mint"])]
    из_["байт_в_байт"] = bool(данные_те_же and разн and not вне)
    из_["расхождения"] = разн
    из_["вне_наших_мест"] = вне
    return из_


def свод(строки: list) -> dict:
    по_типам = collections.OrderedDict()
    for с in строки:
        т = с.get("тип") or "прочее"
        д = по_типам.setdefault(т, {"сигналов": 0, "с_транзакцией": 0,
                                     "сторона": 0, "цена": 0, "байт_в_байт": 0,
                                     "развёрнутых_сверено": 0,
                                     "вне_наших_мест": 0, "причины": {}})
        д["сигналов"] += 1
        if с.get("сторона") is not None:
            д["с_транзакцией"] += 1
        if с.get("сторона"):
            д["сторона"] += 1
        else:
            п = (с.get("почему_сторона") or "")[:60]
            д["причины"][п] = д["причины"].get(п, 0) + 1
        if с.get("цена"):
            д["цена"] += 1
        if с.get("байт_в_байт"):
            д["развёрнутых_сверено" if с.get("развёрнут") else "байт_в_байт"] += 1
        if с.get("вне_наших_мест"):
            д["вне_наших_мест"] += 1
    return по_типам


def печать(св: dict, строки: list) -> None:
    print("USDC-нога по типам пулов: сигналов / с транзакцией / сторона USDC / "
          "цена события / байт в байт с источником / развёрнутых сверено с "
          "шаблоном покупки / расхождения вне наших мест")
    for т, д in св.items():
        print(f"  {т:<12} {д['сигналов']:>4} {д['с_транзакцией']:>5} "
              f"{д['сторона']:>5} {д['цена']:>5} {д['байт_в_байт']:>5} "
              f"{д['развёрнутых_сверено']:>5} {д['вне_наших_мест']:>5}")
        for п, н in sorted(д["причины"].items(), key=lambda x: -x[1])[:3]:
            print(f"      отказ x{н}: {п}")
    плохие = [с for с in строки if с.get("вне_наших_мест")]
    if плохие:
        print("  РАСХОЖДЕНИЯ ВНЕ НАШИХ МЕСТ -- тип не готов:")
        for с in плохие[:10]:
            print(f"      {с['тип']} {str(с['signature'])[:24]} места {с['вне_наших_мест']}")


def main() -> int:
    п = argparse.ArgumentParser()
    п.add_argument("--obrazcy", default=str(ПО_УМОЛЧАНИЮ))
    п.add_argument("--moduli", default=None)
    п.add_argument("--out", default=None)
    а = п.parse_args()
    if not Path(а.obrazcy).exists():
        print(f"образцов нет: {а.obrazcy}\n"
              f"  их пишет analysis/podbivka_usdc_noga.py в ветке Code-2 "
              f"claude/podbivka; без них типы готовыми не считаются")
        return 2
    C, U = _модули(а.moduli)
    with open(а.obrazcy, encoding="utf-8") as ф:
        д = json.load(ф)
    сигналы = д.get("сигналы") or []
    строки = [строка(с, C, U) for с in сигналы]
    св = свод(строки)
    печать(св, строки)
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump({"свод": св, "строки": строки}, ф, ensure_ascii=False, indent=1)
        print(f"  отчёт: {а.out}")
    return 1 if any(с.get("вне_наших_мест") for с in строки) else 0


if __name__ == "__main__":
    raise SystemExit(main())
