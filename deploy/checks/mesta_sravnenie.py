#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""СДВИНУЛИ ЛИ НАС -41 мс: место в блоке по каждой сделке против рубежей.

Слово владельца 10.10 (ночь, п.2): «по каждой сделке -- лаг, индекс/размер
блока, доля; сравнить с 0.384 (ночь) / 0.550 (день) / 0.784 при лаге 0 --
сдвинули ли нас -41 мс и первый принявший».

ОТКУДА РУБЕЖИ. Это наша же доля места в блоке по 74 сделкам 09->10.10
(data/mesta_v_bloke.json, поле nash_dolya): медиана 0.3826 ночью, 0.5409 днём,
0.8072 при лаге 0. Владелец назвал 0.384 / 0.550 / 0.784 -- те же числа по его
окну; рубежи задаются входом, а не зашиты, чтобы сравнивать с ЕГО числами, а
не с моими.

ЧТО СЧИТАЕТСЯ И ЧЕГО ЗДЕСЬ НЕТ. Доля -- это наш индекс, делённый на размер
блока: 0.0 -- первая транзакция блока, 1.0 -- последняя. МЕНЬШЕ ЛУЧШЕ.
Своего счёта доли здесь не заводится: берутся поля выгрузки сделок
(our_block_index / our_block_total / our_block_share), которые пишет сам
детектор по цепи. Два счёта по одним полям разошлись бы с докладом при первой
же правке.

Только чтение: ни одного обращения к цепи, ни одной записи на хост.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

# НОЧЬ И ДЕНЬ -- ПО ЧАСАМ UTC, И ГРАНИЦЫ ТЕ ЖЕ, ЧТО В СЧЁТЕ РУБЕЖЕЙ: день
# 10:00-16:00Z (в эти часы 10.10 шла дневная просадка), остальное -- ночь.
ДЕНЬ_ОТ_ЧАС = 10
ДЕНЬ_ДО_ЧАС = 16
РУБЕЖИ_ПО_УМОЛЧАНИЮ = {"noch": 0.384, "den": 0.550, "lag0": 0.784}


def _час(utc: str | None) -> int | None:
    т = str(utc or "")
    try:
        return int(т[11:13])
    except (ValueError, IndexError):
        return None


def днём(utc: str | None) -> bool | None:
    ч = _час(utc)
    if ч is None:
        return None
    return ДЕНЬ_ОТ_ЧАС <= ч < ДЕНЬ_ДО_ЧАС


def _мед(числа: list):
    ч = [x for x in числа if isinstance(x, (int, float))]
    return round(statistics.median(ч), 4) if ч else None


def _доля(ряд: dict):
    """Доля места в блоке. Берётся из выгрузки; считается только если её нет."""
    д = ряд.get("our_block_share")
    if isinstance(д, (int, float)):
        return round(float(д), 4)
    и, в = ряд.get("our_block_index"), ряд.get("our_block_total")
    if isinstance(и, int) and isinstance(в, int) and в > 0:
        return round(и / в, 4)
    return None


def _лаг(ряд: dict):
    """Лаг в слотах от блока источника. s_plus -- поле выгрузки."""
    for к in ("s_plus", "slotov_posle_istochnika"):
        v = ряд.get(к)
        if isinstance(v, int):
            return v
    и, с = ряд.get("our_slot"), ряд.get("source_slot")
    if isinstance(и, int) and isinstance(с, int):
        return и - с
    return None


def _между(ряд: dict):
    """Транзакций МЕЖДУ источником и нами в ОДНОМ блоке. Иначе None."""
    if _лаг(ряд) != 0:
        return None
    и, с = ряд.get("our_block_index"), ряд.get("source_block_index")
    if not isinstance(и, int) or not isinstance(с, int):
        return None
    # ЗНАК СОХРАНЯЕТСЯ: отрицательное значит, что мы ВПЕРЕДИ источника в его же
    # блоке -- так было ночью 09->10.10 по медиане (наш 516 против 527). Брать
    # модуль значило бы потерять ровно то, что мерим.
    return и - с - 1


def строка_сделки(ряд: dict) -> dict:
    """Одна сделка: лаг, место, доля, подшаг подписи, кто принял и кто довёз."""
    пш = ряд.get("podshagi_ms") if isinstance(ряд.get("podshagi_ms"), dict) else {}
    return {
        "utc": ряд.get("utc"),
        "mint": ряд.get("mint"),
        "lag": _лаг(ряд),
        "nash_index": ряд.get("our_block_index"),
        "block_vsego": ряд.get("our_block_total"),
        "dolya": _доля(ряд),
        "ist_index": ряд.get("source_block_index"),
        # СКОЛЬКО ТРАНЗАКЦИЙ МЕЖДУ НАМИ -- ТОЛЬКО ПРИ ЛАГЕ 0, и это не
        # придирка: при лаге 1 и больше мы в ДРУГОМ блоке, и "между" значило бы
        # остаток блока источника плюс начало нашего -- другое число, которое
        # нельзя класть в тот же столбец. Поэтому при лаге != 0 здесь None, и в
        # своде оно считается отдельной корзиной.
        "mezhdu_nami": _между(ряд),
        # ПОДПИСЬ -- ТОТ САМЫЙ ПОДШАГ, который срезала правка 10.10 (42.9 ->
        # 1.3 мс). Берётся из журнала подшагов, а не считается заново.
        "podpis_ms": (ряд.get("podpis_ms")
                      if ряд.get("podpis_ms") is not None else пш.get("подпись")),
        "vsego_ms": ряд.get("vsego_ms") or пш.get("всего"),
        "reshili_ms": ряд.get("reshili_ms"),
        # ПРИНЯЛ ПЕРВЫМ и ДОВЁЗ -- РАЗНЫЕ СОБЫТИЯ: ответ сервиса против цепи.
        "prinyal_pervym": ряд.get("pool_accepted_first"),
        "dovyoz": ряд.get("otpravitel"),
        "itog_sol": ряд.get("итог_по_цепи_sol") or ряд.get("itog_po_cepi_sol"),
        "noch": (None if днём(ряд.get("utc")) is None
                 else (not днём(ряд.get("utc")))),
    }


def свод(строки: list, рубежи: dict) -> dict:
    """Медианы по ночи, дню и корзинам лага -- и сравнение с рубежами."""
    из_ = {"sdelok": len(строки), "rubezhi": dict(рубежи), "po_vremeni": {},
           "po_lagu": {}, "sdvinulo": {}, "podpis": {}, "prinyali_pervymi": {}}
    ночь = [с for с in строки if с["noch"] is True]
    день = [с for с in строки if с["noch"] is False]
    for имя, гр in (("noch", ночь), ("den", день), ("vse", строки)):
        из_["po_vremeni"][имя] = {"sdelok": len(гр),
                                   "dolya_mediana": _мед([с["dolya"] for с in гр]),
                                   "lag_mediana": _мед([с["lag"] for с in гр]),
                                   "index_mediana": _мед([с["nash_index"]
                                                           for с in гр]),
                                   "block_mediana": _мед([с["block_vsego"]
                                                           for с in гр])}
    for корзина in (0, 1, 2):
        гр = [с for с in строки if с["lag"] == корзина]
        из_["po_lagu"][str(корзина)] = {
            "sdelok": len(гр), "dolya_mediana": _мед([с["dolya"] for с in гр]),
            "itog_sol": _мед([с["itog_sol"] for с in гр])}
    гр3 = [с for с in строки if isinstance(с["lag"], int) and с["lag"] >= 3]
    из_["po_lagu"]["3+"] = {"sdelok": len(гр3),
                             "dolya_mediana": _мед([с["dolya"] for с in гр3]),
                             "itog_sol": _мед([с["itog_sol"] for с in гр3])}
    # СДВИНУЛО ЛИ -- РАЗНИЦА С РУБЕЖОМ, И ЗНАК НАЗВАН СЛОВОМ. Доля МЕНЬШЕ --
    # лучше (мы ближе к началу блока), поэтому "лучше" это отрицательная
    # разница. Без слова знак читался бы наугад.
    пары = (("noch", из_["po_vremeni"]["noch"]["dolya_mediana"]),
            ("den", из_["po_vremeni"]["den"]["dolya_mediana"]),
            ("lag0", из_["po_lagu"]["0"]["dolya_mediana"]))
    for имя, стало in пары:
        было = рубежи.get(имя)
        if стало is None or не_число(было):
            из_["sdvinulo"][имя] = {"bylo": было, "stalo": стало,
                                     "raznica": None,
                                     "slovami": "сделок в этой корзине нет"}
            continue
        разница = round(стало - float(было), 4)
        из_["sdvinulo"][имя] = {
            "bylo": round(float(было), 4), "stalo": стало, "raznica": разница,
            "slovami": ("место НЕ изменилось" if abs(разница) < 0.005
                         else ("сдвинуло ВПЕРЁД (доля меньше -- лучше)"
                               if разница < 0 else
                               "сдвинуло НАЗАД (доля больше -- хуже)"))}
    # МЕЖДУ НАМИ -- СВОЙ СВОД, по тем сделкам, где лаг 0 (в других блоках
    # "между" не определено).
    _между_л0 = [с["mezhdu_nami"] for с in строки if с["lag"] == 0]
    из_["mezhdu_nami"] = {
        "sdelok_lag0": len([с for с in строки if с["lag"] == 0]),
        "schitano": len([x for x in _между_л0 if isinstance(x, int)]),
        "mediana": _мед(_между_л0),
        "vperedi_istochnika": len([x for x in _между_л0
                                    if isinstance(x, int) and x < 0]),
        "pozzhe_istochnika": len([x for x in _между_л0
                                   if isinstance(x, int) and x >= 0])}
    из_["dolya_lag0"] = (round(len([с for с in строки if с["lag"] == 0])
                                / len(строки), 4) if строки else None)
    из_["index_mediany"] = {"ist": _мед([с["ist_index"] for с in строки]),
                             "nash": _мед([с["nash_index"] for с in строки]),
                             "block": _мед([с["block_vsego"] for с in строки])}
    из_["podpis"] = {"mediana_ms": _мед([с["podpis_ms"] for с in строки]),
                      "maks_ms": max([с["podpis_ms"] for с in строки
                                       if isinstance(с["podpis_ms"], (int, float))]
                                      or [0]) or None,
                      "vsego_mediana_ms": _мед([с["vsego_ms"] for с in строки])}
    for с in строки:
        к = с["prinyal_pervym"] or "не назван"
        из_["prinyali_pervymi"][к] = из_["prinyali_pervymi"].get(к, 0) + 1
    из_["dovezli"] = {}
    for с in строки:
        к = с["dovyoz"] or "не назван"
        из_["dovezli"][к] = из_["dovezli"].get(к, 0) + 1
    return из_


def не_число(x) -> bool:
    return not isinstance(x, (int, float)) or isinstance(x, bool)


def разобрать_рубежи(текст: str) -> dict:
    """«noch=0.384,den=0.550,lag0=0.784» -> словарь. Мусор -- отказ."""
    из_ = dict(РУБЕЖИ_ПО_УМОЛЧАНИЮ)
    for кусок in (текст or "").replace(";", ",").split(","):
        к = кусок.strip()
        if not к:
            continue
        if "=" not in к:
            raise ValueError(f"рубеж без знака равенства: {к!r}")
        имя, зн = к.split("=", 1)
        имя = имя.strip()
        if имя not in РУБЕЖИ_ПО_УМОЛЧАНИЮ:
            raise ValueError(f"неизвестный рубеж {имя!r}: "
                             f"бывают {sorted(РУБЕЖИ_ПО_УМОЛЧАНИЮ)}")
        из_[имя] = float(зн.strip())
    return из_


def отобрать(ряды: list, *, группа: str, от_utc: str) -> list:
    """Сделки ОДНОЙ группы, которые УШЛИ, не раньше границы."""
    из_ = []
    for р in ряды:
        if группа and (р.get("group") or "") != группа:
            continue
        if от_utc and str(р.get("utc") or "") < от_utc:
            continue
        # НЕ УШЕДШИЕ СЮДА НЕ ИДУТ: у отказа нет ни места в блоке, ни подписи, и
        # считать его медианой значило бы разбавить замер нулями.
        if not (р.get("buy_sig") or р.get("landed_sig")):
            continue
        из_.append(строка_сделки(р))
    return из_


def самопроверка() -> int:
    проверки: list = []

    def chk(имя: str, ок: bool, что=None) -> None:
        проверки.append((имя, bool(ок), что))

    # ДОЛЯ: из выгрузки, а при её отсутствии -- из индекса и размера.
    chk("доля берётся ИЗ ВЫГРУЗКИ, если она там есть",
        _доля({"our_block_share": 0.25, "our_block_index": 9,
               "our_block_total": 10}) == 0.25)
    chk("нет доли -- считается из индекса и размера",
        _доля({"our_block_index": 1, "our_block_total": 4}) == 0.25)
    chk("нет ни того ни другого -- None, а не ноль",
        _доля({"our_block_index": 1}) is None
        and _доля({"our_block_index": 1, "our_block_total": 0}) is None)
    # ЛАГ: s_plus, затем слоты, затем разность -- и None, а не ноль.
    chk("лаг берётся из s_plus", _лаг({"s_plus": 2, "our_slot": 10,
                                        "source_slot": 1}) == 2)
    chk("нет s_plus -- разность слотов",
        _лаг({"our_slot": 10, "source_slot": 8}) == 2)
    chk("нечего считать -- None, а не ноль", _лаг({}) is None)
    chk("лаг 0 -- это ЧИСЛО НОЛЬ, а не отсутствие",
        _лаг({"s_plus": 0}) == 0)
    # НОЧЬ И ДЕНЬ.
    chk("день -- 10:00-16:00Z, остальное ночь",
        днём("2026-10-10T12:00:00Z") is True
        and днём("2026-10-10T09:59:00Z") is False
        and днём("2026-10-10T16:00:00Z") is False
        and днём("2026-10-10T01:00:00Z") is False)
    chk("без времени -- неизвестно, а не «ночь»", днём(None) is None)
    # ОТБОР: чужая группа и отказ в замер не идут.
    ряды = [
        {"group": "vol_4vw", "utc": "2026-10-10T22:00:00Z", "buy_sig": "A",
         "our_block_index": 10, "our_block_total": 100, "s_plus": 0,
         "podpis_ms": 1.2, "pool_accepted_first": "jito", "otpravitel": "jito",
         "итог_по_цепи_sol": 0.01},
        {"group": "vol_4vw", "utc": "2026-10-10T12:00:00Z", "buy_sig": "B",
         "our_block_index": 60, "our_block_total": 100, "s_plus": 1,
         "podpis_ms": 1.4, "pool_accepted_first": "helius",
         "otpravitel": "astralane", "итог_по_цепи_sol": -0.002},
        {"group": "другая", "utc": "2026-10-10T22:00:00Z", "buy_sig": "C",
         "our_block_index": 1, "our_block_total": 100, "s_plus": 0},
        {"group": "vol_4vw", "utc": "2026-10-10T22:00:00Z",
         "why_not": "отказ", "our_block_index": 1, "our_block_total": 100},
        {"group": "vol_4vw", "utc": "2026-10-10T20:00:00Z", "buy_sig": "D",
         "our_block_index": 5, "our_block_total": 100, "s_plus": 0},
    ]
    отобр = отобрать(ряды, группа="vol_4vw", от_utc="2026-10-10T21:45:00Z")
    chk("отбор берёт ТОЛЬКО свою группу, только ушедшие и только позже границы",
        len(отобр) == 1 and отобр[0]["mint"] is None
        and отобр[0]["utc"] == "2026-10-10T22:00:00Z", отобр)
    все_св = отобрать(ряды, группа="vol_4vw", от_utc="")
    chk("без границы берутся все ушедшие своей группы (три из пяти)",
        len(все_св) == 3, [с["utc"] for с in все_св])
    с_ = свод(все_св, РУБЕЖИ_ПО_УМОЛЧАНИЮ)
    chk("ночь и день разделены по времени, а не свалены вместе",
        с_["po_vremeni"]["noch"]["sdelok"] == 2
        and с_["po_vremeni"]["den"]["sdelok"] == 1, с_["po_vremeni"])
    chk("медиана доли ночью -- по двум ночным (0.10 и 0.05)",
        с_["po_vremeni"]["noch"]["dolya_mediana"] == 0.075,
        с_["po_vremeni"]["noch"])
    # ЗНАК СДВИГА НАЗВАН СЛОВОМ, и слово обязано соответствовать числу.
    chk("доля МЕНЬШЕ рубежа -- сказано «вперёд (лучше)»",
        с_["sdvinulo"]["noch"]["raznica"] < 0
        and "ВПЕРЁД" in с_["sdvinulo"]["noch"]["slovami"],
        с_["sdvinulo"]["noch"])
    хуже = свод([{"utc": "2026-10-10T22:00:00Z", "noch": True, "lag": 1,
                  "dolya": 0.9, "nash_index": 90, "block_vsego": 100,
                  "podpis_ms": 1.0, "vsego_ms": 60, "reshili_ms": 10,
                  "prinyal_pervym": "jito", "dovyoz": "jito",
                  "itog_sol": 0.0, "ist_index": 10}],
                РУБЕЖИ_ПО_УМОЛЧАНИЮ)
    chk("доля БОЛЬШЕ рубежа -- сказано «назад (хуже)»",
        хуже["sdvinulo"]["noch"]["raznica"] > 0
        and "НАЗАД" in хуже["sdvinulo"]["noch"]["slovami"],
        хуже["sdvinulo"]["noch"])
    пусто = свод([], РУБЕЖИ_ПО_УМОЛЧАНИЮ)
    chk("нет сделок -- разница None и сказано словами, а не ноль",
        пусто["sdvinulo"]["den"]["raznica"] is None
        and "нет" in пусто["sdvinulo"]["den"]["slovami"],
        пусто["sdvinulo"]["den"])
    # МЕЖДУ НАМИ: знак сохраняется, и при лаге != 0 это None, а не ноль.
    chk("между нами при лаге 0 -- наш индекс минус индекс источника минус 1",
        _между({"s_plus": 0, "our_block_index": 20,
                 "source_block_index": 10}) == 9)
    chk("мы ВПЕРЕДИ источника -- число ОТРИЦАТЕЛЬНОЕ, а не по модулю "
        "(ночью 09->10.10 так и было: наш 516 против 527)",
        _между({"s_plus": 0, "our_block_index": 10,
                 "source_block_index": 20}) == -11)
    chk("при лаге 1 между нами НЕ считается: это другой блок",
        _между({"s_plus": 1, "our_block_index": 20,
                 "source_block_index": 10}) is None)
    chk("нет индексов -- None, а не ноль",
        _между({"s_plus": 0, "our_block_index": 20}) is None)
    _мн = свод([
        {"utc": "2026-10-10T22:00:00Z", "noch": True, "lag": 0, "dolya": 0.2,
         "nash_index": 10, "block_vsego": 100, "ist_index": 20,
         "mezhdu_nami": -11, "podpis_ms": 1.0, "vsego_ms": 50,
         "reshili_ms": 5, "prinyal_pervym": "jito", "dovyoz": "jito",
         "itog_sol": 0.0},
        {"utc": "2026-10-10T22:01:00Z", "noch": True, "lag": 0, "dolya": 0.6,
         "nash_index": 60, "block_vsego": 100, "ist_index": 20,
         "mezhdu_nami": 39, "podpis_ms": 1.0, "vsego_ms": 50,
         "reshili_ms": 5, "prinyal_pervym": "jito", "dovyoz": "jito",
         "itog_sol": 0.0},
        {"utc": "2026-10-10T22:02:00Z", "noch": True, "lag": 2, "dolya": 0.3,
         "nash_index": 30, "block_vsego": 100, "ist_index": 20,
         "mezhdu_nami": None, "podpis_ms": 1.0, "vsego_ms": 50,
         "reshili_ms": 5, "prinyal_pervym": "jito", "dovyoz": "jito",
         "itog_sol": 0.0}], РУБЕЖИ_ПО_УМОЛЧАНИЮ)
    chk("свод «между нами» считает только лаг 0 и делит впереди/позже",
        _мн["mezhdu_nami"]["sdelok_lag0"] == 2
        and _мн["mezhdu_nami"]["schitano"] == 2
        and _мн["mezhdu_nami"]["mediana"] == 14.0
        and _мн["mezhdu_nami"]["vperedi_istochnika"] == 1
        and _мн["mezhdu_nami"]["pozzhe_istochnika"] == 1,
        _мн["mezhdu_nami"])
    chk("доля лага 0 -- это доля ОТ ВСЕХ сделок, а не от корзины",
        _мн["dolya_lag0"] == round(2 / 3, 4), _мн["dolya_lag0"])
    chk("медианы индексов -- источника, нашего и размера блока",
        _мн["index_mediany"]["ist"] == 20
        and _мн["index_mediany"]["nash"] == 30
        and _мн["index_mediany"]["block"] == 100, _мн["index_mediany"])
    chk("корзины лага считаются отдельно, и 3+ своя",
        с_["po_lagu"]["0"]["sdelok"] == 2 and с_["po_lagu"]["1"]["sdelok"] == 1
        and с_["po_lagu"]["3+"]["sdelok"] == 0, с_["po_lagu"])
    chk("первый принявший и довёзший считаются ОТДЕЛЬНО -- это разные события",
        с_["prinyali_pervymi"].get("helius") == 1
        and с_["dovezli"].get("astralane") == 1
        and с_["dovezli"].get("helius") is None,
        (с_["prinyali_pervymi"], с_["dovezli"]))
    chk("подшаг подписи -- медианой и максимумом",
        с_["podpis"]["mediana_ms"] == 1.3 and с_["podpis"]["maks_ms"] == 1.4,
        с_["podpis"])
    # РУБЕЖИ ВХОДОМ, И МУСОР -- ОТКАЗ.
    chk("рубежи разбираются из строки",
        разобрать_рубежи("noch=0.4,lag0=0.8")["noch"] == 0.4
        and разобрать_рубежи("noch=0.4")["den"] == РУБЕЖИ_ПО_УМОЛЧАНИЮ["den"])
    for мусор in ("noch", "неизвестный=1", "noch=слово"):
        ок = False
        try:
            разобрать_рубежи(мусор)
        except ValueError:
            ок = True
        chk(f"мусор в рубежах -- отказ, а не молчаливое значение: {мусор!r}", ок)
    плохих = [(и, ч) for и, ок, ч in проверки if not ок]
    for и, ок, ч in проверки:
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {и}" + ("" if ок else f" -> {ч}"))
    print(f"самопроверка сравнения мест: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


def main() -> int:
    п = argparse.ArgumentParser()
    п.add_argument("--self-test", action="store_true")
    п.add_argument("--vygruzka", default="",
                    help="выгрузка сделок полосы (json с полем «ряды»)")
    п.add_argument("--gruppa", default="vol_4vw")
    п.add_argument("--ot-utc", default="",
                    help="брать сделки не раньше этого времени, "
                         "например 2026-10-10T21:45:00Z")
    п.add_argument("--rubezhi", default="",
                    help="noch=0.384,den=0.550,lag0=0.784")
    п.add_argument("--out", default="")
    а = п.parse_args()
    if а.self_test:
        return самопроверка()
    if not а.vygruzka:
        print("СТОП: нужна --vygruzka")
        return 2
    try:
        рубежи = разобрать_рубежи(а.rubezhi)
    except ValueError as сбой:
        print(f"СТОП: {сбой}")
        return 2
    данные = json.loads(Path(а.vygruzka).read_text(encoding="utf-8"))
    ряды = данные.get("ряды") or данные.get("rows") or []
    строки = отобрать(ряды, группа=а.gruppa, от_utc=а.ot_utc.strip())
    из_ = {"vygruzka": а.vygruzka, "gruppa": а.gruppa,
            "ot_utc": а.ot_utc.strip() or None, "ryadov_v_fajle": len(ряды),
            "stroki": строки, **свод(строки, рубежи)}
    текст = json.dumps(из_, ensure_ascii=False, indent=1)
    print(текст)
    if а.out:
        Path(а.out).write_text(текст + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
