#!/usr/bin/env python3
"""Проба на убийство: зарабатывает ли поставщик ликвидности на свежих пулах
мемкоинов на обороте больше, чем теряет на цене. ТОЛЬКО РАСЧЁТ.

Ни одной отправки, ни одной подписи, ни одного вызова узла, ни строки в
денежном пути. Читаются СУТОЧНЫЕ ФАЙЛЫ АРХИВА Code-2 и ЕГО ЖЕ модули
(podbivka_arhiv_den, podbivka_hvosty) -- своей читалки архива здесь нет.

ЧТО АРХИВ ДАЁТ, А ЧЕГО НЕ ДАЁТ -- СКАЗАНО ЧИСЛОМ, А НЕ ОБОЙДЕНО.
У каждого сигнала есть `модель.состояния`: резервы пула (котировка в SOL,
токены, слот) на входах S0/S0_дно/S1/N16/N50 и на выходах 6…300 слотов. Этого
достаточно для ТОЧНОГО счёта LP: доход от оборота и потеря на цене целиком
определяются двумя снимками резервов (см. ФОРМУЛЫ). Но окно архива --
`окно_слотов` = 160 слотов, и при измеренных 0.220 с/слот это около 35 секунд.
Горизонты задания -- 10 мин / 1 ч / 6 ч / 24 ч -- это 2700 / 16000 / 98000 /
390000 слотов, то есть в 17--2400 раз дальше окна. ИХ В АРХИВЕ НЕТ, и считать
их нечем: ряды событий в суточных файлах пустые (`ленты_минтов` = 0 у всех
префиксов). Поэтому прогон считает горизонты, КОТОРЫЕ ЕСТЬ, и отказывается от
заказанных по имени -- а не подставляет вместо них ближайшие.

ФОРМУЛЫ (точные, для пула x*y=k, комиссия LP остаётся в пуле).
Вклад двусторонний: X SOL + токенов на X SOL по цене входа p0. Тогда
«ликвидность» вклада l0 = X/sqrt(p0), и стоимость позиции в SOL при цене p
равна 2*l0*sqrt(p). Комиссия LP растит k, то есть l: l1 = l0*sqrt(k1/k0).
При r = p1/p0 и K = k1/k0:
    стоимость позиции на выходе = 2X * sqrt(K*r)
    стоимость того же набора без пула (держать) = X * (1 + r)
    чистая непостоянная потеря (при K=1) = -X * (sqrt(r) - 1)^2   -- всегда <= 0
    доход от оборота = 2X * sqrt(r) * (sqrt(K) - 1)
Числитель и знаменатель берутся ИЗ ДВУХ СНИМКОВ РЕЗЕРВОВ, без допущений о
том, сколько было сделок.

ЧЕГО ЭТИ ФОРМУЛЫ НЕ РАЗДЕЛЯЮТ. Рост k даёт не только комиссия: вклад или
выход ЧУЖОЙ ликвидности меняет резервы, не будучи сделкой. В архиве их не
видно (п.4 задания), поэтому `K` -- это ВЕРХНЯЯ ОЦЕНКА дохода от оборота, и
прогон печатает рядом подразумеваемый оборот, чтобы перебор был виден.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- Code-2
# ЕГО МОДУЛИ, А НЕ СВОЯ КОПИЯ. Путь приходит снаружи: файлы живут на ветке
# claude/podbivka, и копировать их в свою ветку нельзя -- разойдутся.
КОД_CODE2 = os.environ.get("C3_LP_KOD_CODE2") or ""
WSOL = "So11111111111111111111111111111111111111112"

# ИЗМЕРЕНО ПО СОБЫТИЯМ САМОЙ ПРОГРАММЫ PumpSwap (BuyEvent.lp_fee_basis_points),
# 40 живых событий из образцов репозитория; разбор проверен ТОЖДЕСТВОМ
# программы (lp_fee = база * bps / 10000 и
# quote_amount_in_with_lp_fee = quote_amount_in + lp_fee), сошлось на всех 40.
#   lp_fee_basis_points      = 20 у 31 события, 25 у 3, 2 у 6
#   protocol_fee_basis_points = 5 у 34, 93 у 6
#   coin_creator_fee_bps      = 0…300, настраивается создателем
# ПОСТАВЩИКУ ИДЁТ ТОЛЬКО lp_fee, и она ОСТАЁТСЯ В ПУЛЕ (отсюда рост k).
# Полная ставка, которую платит торговец (poolFeeRate у PumpApi), в этих же
# пулах 100 б.п. -- то есть LP получает ПЯТУЮ ЧАСТЬ собранной комиссии.
LP_BPS_ИЗМЕРЕНО = 20
LP_BPS_РАСПРЕДЕЛЕНИЕ = {20: 31, 25: 3, 2: 6}
ПРОТОКОЛ_BPS_ИЗМЕРЕНО = 5
# ЗА ВХОД И ВЫХОД ЛИКВИДНОСТИ ПРОГРАММА НЕ БЕРЁТ НИЧЕГО: у инструкций
# deposit и withdraw в закреплённом IDL (data/idl/pump_amm.json) НЕТ ни одного
# счёта комиссии -- проверено перебором имён счетов.
КОМИССИЯ_ВКЛАДА_BPS = 0
# ИЗДЕРЖКИ КРУГА -- ЧИСЛО CODE-2 (podbivka_arhiv_den.ИЗДЕРЖКИ = 0.002 SOL),
# его же замер на живых кругах. Здесь круг ДЛИННЕЕ: вход ликвидности и выход
# -- две транзакции, плюс две на покупку и продажу токеновой ноги.
ИЗДЕРЖКИ_ТРАНЗАКЦИИ = 0.002 / 2      # на одну транзакцию
ТРАНЗАКЦИЙ_В_КРУГЕ = 4               # купить ногу, вложить, снять, продать ногу
# РЕНТА СЧЕТОВ -- ВОЗВРАТНАЯ, и поэтому в итог не ставится, но называется:
# два счёта токена и счёт LP-токена, по 0.00203928 SOL (ATA SPL), возвращается
# при закрытии. В итог не входит; названа, чтобы её не искали.
РЕНТА_СЧЁТА = 0.00203928
СЧЕТОВ_ПОД_РЕНТУ = 3

# ГОРИЗОНТЫ, КОТОРЫЕ ЕСТЬ В АРХИВЕ (слоты). Больше 160 быть не может:
# окно_слотов = 160 у всех суточных файлов всех префиксов.
ГОРИЗОНТЫ_АРХИВА = (6, 12, 24, 30, 36, 60, 72, 108, 150, 300)
ОКНО_СЛОТОВ = 160
# ИЗМЕРЕНО: удержание.секунд_до[150] / 150 по суткам архива.
СЕКУНД_НА_СЛОТ = 0.220
# ГОРИЗОНТЫ ЗАДАНИЯ -- в слотах при измеренной длине слота.
ЗАКАЗАНЫ = {"10 мин": 600, "1 ч": 3600, "6 ч": 21600, "24 ч": 86400}

РАЗМЕРЫ = (1.0, 5.0, 20.0)

WHY_НЕТ_ОКНА = ("горизонт дальше окна архива -- считать нечем, и ближайший "
                "вместо него не подставляется")
WHY_НЕТ_СОСТОЯНИЙ = "у сигнала нет снимка резервов на этом месте"


class НетДанных(Exception):
    """Данных нет. Отказ по имени, а не ноль вместо числа."""


# ---------------------------------------------------------------- расчёт

def lp_itog(*, y0: float, x0: float, y1: float, x1: float, razmer: float,
            fee_torgovli: float) -> dict:
    """ИТОГ ПОЗИЦИИ LP В SOL -- точно, по двум снимкам резервов.

    y -- котировка (SOL) в пуле, x -- токены. `razmer` -- SOL на ОДНУ сторону
    (полный вклад стоит 2*razmer SOL). `fee_torgovli` -- полная доля, которую
    платит торговец в этом пуле (poolFeeRate архива): по ней считаются наши
    ДВЕ сделки с токеновой ногой, которых не избежать -- токен для вклада надо
    купить, а на выходе продать.
    """
    if min(y0, x0, y1, x1) <= 0:
        raise НетДанных(WHY_НЕТ_СОСТОЯНИЙ)
    p0, p1 = y0 / x0, y1 / x1
    k0, k1 = x0 * y0, x1 * y1
    r, K = p1 / p0, k1 / k0
    X = float(razmer)
    вклад = 2 * X
    позиция = 2 * X * math.sqrt(K * r)
    держать = X * (1 + r)
    потеря_цены = -X * (math.sqrt(r) - 1) ** 2
    доход_оборота = 2 * X * math.sqrt(r) * (math.sqrt(K) - 1)
    # ПОДРАЗУМЕВАЕМЫЙ ОБОРОТ: рост k при ставке LP. Нужен, чтобы перебор был
    # виден: если он больше пула в разы, то k вырос не от комиссии.
    доля_lp = LP_BPS_ИЗМЕРЕНО / 10_000
    оборот = ((math.sqrt(K) - 1) * 2 * y0 / доля_lp) if K > 1 else 0.0
    # НАШИ ДВЕ СДЕЛКИ С ТОКЕНОВОЙ НОГОЙ: комиссия пула и СВОЙ СДВИГ ЦЕНЫ.
    # Сдвиг считается по x*y=k на РЕЗЕРВАХ АРХИВА: покупка на X SOL в пуле с
    # котировкой y даёт цену хуже спота ровно на X/(y+X).
    комиссия_ноги = fee_torgovli * X * (1 + math.sqrt(K * r))
    сдвиг_входа = X * X / (y0 + X)
    стоимость_ноги_на_выходе = X * math.sqrt(K * r)
    сдвиг_выхода = (стоимость_ноги_на_выходе ** 2 / (y1 + стоимость_ноги_на_выходе)
                    if y1 > 0 else 0.0)
    сеть = ИЗДЕРЖКИ_ТРАНЗАКЦИИ * ТРАНЗАКЦИЙ_В_КРУГЕ
    расходы = комиссия_ноги + сдвиг_входа + сдвиг_выхода + сеть
    итог = позиция - вклад - расходы
    return {"p0": p0, "p1": p1, "r": r, "K": K,
            "vklad": вклад, "pozicija": позиция, "derzhat": держать,
            "protiv_derzhat": позиция - держать,
            "poterja_ceny": потеря_цены, "dohod_oborota": доход_оборота,
            "oborot_podrazumevaemyj": оборот,
            "rashody": расходы, "komissija_nogi": комиссия_ноги,
            "sdvig_vhoda": сдвиг_входа, "sdvig_vyhoda": сдвиг_выхода,
            "set": сеть, "itog": итог,
            "dolja_pula": X / (y0 + X)}


def gorizonty_est() -> dict:
    """Какие горизонты архив ДАЁТ, и чего из заказанного в нём НЕТ."""
    есть = {h: round(h * СЕКУНД_НА_СЛОТ, 1) for h in ГОРИЗОНТЫ_АРХИВА
            if h <= ОКНО_СЛОТОВ}
    нет = {имя: сек for имя, сек in ЗАКАЗАНЫ.items()
           if сек / СЕКУНД_НА_СЛОТ > ОКНО_СЛОТОВ}
    return {"est_slotov": sorted(есть), "est_sekund": есть,
            "okno_slotov": ОКНО_СЛОТОВ,
            "okno_sekund": round(ОКНО_СЛОТОВ * СЕКУНД_НА_СЛОТ, 1),
            "zakazano_net": {и: {"sekund": с,
                                 "slotov": round(с / СЕКУНД_НА_СЛОТ),
                                 "vo_skolko_raz_dalshe_okna":
                                     round(с / СЕКУНД_НА_СЛОТ / ОКНО_СЛОТОВ, 1)}
                             for и, с in нет.items()}}


# ---------------------------------------------------------------- архив

def suточные_файлы(kat: Path, prefiks: str = "konv") -> list:
    return sorted(kat.glob(f"{prefiks}_*.json.gz"))


def chitat_sutki(put: Path, *, tip_pula: str = "pump-amm") -> dict:
    """ОДНИ СУТКИ: сигналы нужного типа пула со снимками резервов.

    Читается ГОТОВЫЙ суточный файл Code-2 как есть. Разбор состояний --
    его же раскладкой (`модель.состояния`), ставка комиссии -- его же полем
    `модель.fee` (poolFeeRate архива).
    """
    with gzip.open(put, "rt", encoding="utf-8") as ф:
        д = json.load(ф)
    счёт = д.get("счёт") or {}
    из_ = {"den": д.get("день"), "okno_slotov": д.get("окно_слотов"),
           "porog_sol": д.get("порог_sol"),
           "chasov": len(д.get("часы") or []),
           "fajlov": счёт.get("файлов"), "strok": счёт.get("строк"),
           "oshibok_chasov": len(счёт.get("ошибки") or []),
           "signalov_vsego": len(д.get("сигналы") or []),
           "tipy_pulov": {}, "pozicii": []}
    for с in д.get("сигналы") or []:
        из_["tipy_pulov"][с.get("pool")] = из_["tipy_pulov"].get(с.get("pool"), 0) + 1
    for с in д.get("сигналы") or []:
        if с.get("pool") != tip_pula or с.get("quoteMint") != WSOL:
            continue
        сост = ((с.get("модель") or {}).get("состояния") or {})
        вход = (сост.get("вход") or {}).get("S1")
        if not вход:
            continue
        выходы = сост.get("выход") or {}
        поз = {"poolId": с.get("poolId"), "mint": с.get("mint"),
               "slot": с.get("block"), "sol_signala": с.get("sol"),
               "fee": (с.get("модель") or {}).get("fee"),
               "sobytij_v_okne": с.get("событий_пула_в_окне"),
               "svopov_s1": (с.get("свопов") or {}).get("s1"),
               "svopov_s2": (с.get("свопов") or {}).get("s2"),
               "vhod": вход, "vyhody": {}}
        for h in ГОРИЗОНТЫ_АРХИВА:
            в = выходы.get(str(h))
            if в and в[2] > вход[2]:
                поз["vyhody"][h] = в
        if поз["vyhody"]:
            из_["pozicii"].append(поз)
    return из_


def posчитать_sutki(sutki: dict, *, razmer: float, gorizont: int) -> dict:
    """ВСЕ ПОЗИЦИИ ОДНИХ СУТОК на одном горизонте и одном размере."""
    if gorizont > ОКНО_СЛОТОВ:
        raise НетДанных(f"{WHY_НЕТ_ОКНА}: {gorizont} слотов против окна "
                        f"{ОКНО_СЛОТОВ}")
    итоги, против, доли, обороты, пропущено = [], [], [], [], 0
    for п in sutki["pozicii"]:
        в = п["vyhody"].get(gorizont)
        if not в:
            пропущено += 1
            continue
        y0, x0 = float(п["vhod"][0]), float(п["vhod"][1])
        y1, x1 = float(в[0]), float(в[1])
        фи = п.get("fee")
        if фи is None:
            пропущено += 1
            continue
        try:
            р = lp_itog(y0=y0, x0=x0, y1=y1, x1=x1, razmer=razmer,
                        fee_torgovli=float(фи))
        except НетДанных:
            пропущено += 1
            continue
        итоги.append(р["itog"])
        против.append(р["protiv_derzhat"])
        доли.append(р["dolja_pula"])
        обороты.append(р["oborot_podrazumevaemyj"])
    return {"den": sutki["den"], "gorizont": gorizont, "razmer": razmer,
            "pozicij": len(итоги), "propushcheno": пропущено,
            "itogi": итоги, "protiv_derzhat": против,
            "dolja_pula_mediana": (statistics.median(доли) if доли else None),
            "oborot_mediana": (statistics.median(обороты) if обороты else None),
            "v_pljuse": (sum(1 for и in итоги if и > 0) / len(итоги)
                         if итоги else None)}


def di_po_sutkam(po_sutkam: list) -> dict:
    """ДОВЕРИТЕЛЬНЫЙ ИНТЕРВАЛ ПО СУТКАМ -- по суточным средним, а не по
    позициям: позиции внутри суток не независимы (один рынок, одни и те же
    крупные игроки), и интервал по ним был бы уже, чем правда."""
    ср = [с for с in po_sutkam if с is not None]
    n = len(ср)
    if n < 2:
        return {"n": n, "sr": (ср[0] if n else None), "di95": None,
                "why_not": "суток меньше двух -- интервал не считается"}
    m = statistics.fmean(ср)
    sd = statistics.stdev(ср)
    # t для 95 % при n-1 степенях свободы -- таблица на нужные n, без
    # сторонних библиотек. Больше 10 суток здесь не бывает.
    T = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571, 7: 2.447,
         8: 2.365, 9: 2.306, 10: 2.262}
    t = T.get(n, 2.0)
    пол = t * sd / math.sqrt(n)
    return {"n": n, "sr": m, "sd": sd, "di95": [m - пол, m + пол],
            "polushirina": пол}


# ------------------------------------------------- п.2: порог по обороту

ГОРИЗОНТ_ФИЛЬТРА = 24          # слотов (~5.3 с) -- «оборот первых секунд»
ГОРИЗОНТ_ОЦЕНКИ = 150          # слотов (~33 с) -- на нём считаем итог
ПОРОГИ_N = (0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0)


def oborot_pozicii(p: dict, gorizont: int) -> float | None:
    """ПОДРАЗУМЕВАЕМЫЙ ОБОРОТ позиции к горизонту -- из роста k при ставке LP."""
    в = p["vyhody"].get(gorizont)
    if not в:
        return None
    y0, x0 = float(p["vhod"][0]), float(p["vhod"][1])
    y1, x1 = float(в[0]), float(в[1])
    if min(y0, x0, y1, x1) <= 0:
        return None
    K = (x1 * y1) / (x0 * y0)
    if K <= 1:
        return 0.0
    return (math.sqrt(K) - 1) * 2 * y0 / (LP_BPS_ИЗМЕРЕНО / 10_000)


def porog_podobrat(sutki_kalibr: list, *, razmer: float) -> dict:
    """ПОДБОР N НА КАЛИБРОВОЧНЫХ СУТКАХ. Выбирается порог с лучшим СРЕДНИМ
    БЕЗ ВЕРХНИХ 5 % (усечённым) -- не по максимуму и не по медиане: максимум
    ловит один хвост, а усечённое среднее и есть то, что просили в итоге."""
    строки = []
    for N in ПОРОГИ_N:
        итоги = []
        for с in sutki_kalibr:
            for п in с["pozicii"]:
                об = oborot_pozicii(п, ГОРИЗОНТ_ФИЛЬТРА)
                if об is None or об < N:
                    continue
                в = п["vyhody"].get(ГОРИЗОНТ_ОЦЕНКИ)
                фи = п.get("fee")
                if not в or фи is None:
                    continue
                try:
                    р = lp_itog(y0=float(п["vhod"][0]), x0=float(п["vhod"][1]),
                                y1=float(в[0]), x1=float(в[1]),
                                razmer=razmer, fee_torgovli=float(фи))
                except НетДанных:
                    continue
                итоги.append(р["itog"])
        строки.append({"N": N, "pozicij": len(итоги),
                       "stat": _стат(итоги),
                       "v_pljuse": (sum(1 for и in итоги if и > 0) / len(итоги)
                                    if итоги else None)})
    годные = [с for с in строки if (с["stat"].get("n") or 0) >= 100]
    лучший = max(годные, key=lambda с: с["stat"]["ус"]) if годные else None
    return {"stroki": строки, "luchshij": лучший,
            "why_not": (None if лучший else
                        "ни у одного порога не набралось 100 позиций")}


def porog_proverit(sutki_proverki: list, *, N: float, razmer: float) -> dict:
    """ПРОВЕРКА ПОДОБРАННОГО N НА ОТЛОЖЕННЫХ СУТКАХ -- тех, что в подборе не
    участвовали. Иначе порог подогнан под свой же шум."""
    по_суткам, все_итоги = [], []
    for с in sutki_proverki:
        итоги = []
        for п in с["pozicii"]:
            об = oborot_pozicii(п, ГОРИЗОНТ_ФИЛЬТРА)
            if об is None or об < N:
                continue
            в = п["vyhody"].get(ГОРИЗОНТ_ОЦЕНКИ)
            фи = п.get("fee")
            if not в or фи is None:
                continue
            try:
                р = lp_itog(y0=float(п["vhod"][0]), x0=float(п["vhod"][1]),
                            y1=float(в[0]), x1=float(в[1]), razmer=razmer,
                            fee_torgovli=float(фи))
            except НетДанных:
                continue
            итоги.append(р["itog"])
        все_итоги += итоги
        по_суткам.append({"den": с["den"], "pozicij": len(итоги),
                          "sr": (statistics.fmean(итоги) if итоги else None),
                          "us": _стат(итоги).get("ус")})
    return {"N": N, "po_sutkam": по_суткам, "pozicij": len(все_итоги),
            "stat": _стат(все_итоги),
            "v_pljuse": (sum(1 for и in все_итоги if и > 0) / len(все_итоги)
                         if все_итоги else None),
            "di_po_sutkam": di_po_sutkam([с["sr"] for с in по_суткам])}



# ОТБОР, НЕ ВЫВЕДЕННЫЙ ИЗ k -- ЧТОБЫ НЕ СУДИТЬ ПО ТОМУ ЖЕ, ЧЕМ ОТБИРАЕМ.
# Порог по подразумеваемому обороту считается ИЗ РОСТА k, и итог позиции --
# тоже из роста k. Значит пул, где чужой поставщик долил ликвидность, пройдёт
# отбор и покажет прибыль ПО ОДНОЙ И ТОЙ ЖЕ причине, а не потому, что
# заработал на обороте. Это замкнутый круг, и он закрывается вторым отбором,
# независимым от k: ЧИСЛО СОБЫТИЙ ПУЛА В ОКНЕ (поле архива
# `событий_пула_в_окне`, счётчик событий, а не резервов). Если оба отбора
# дают один ответ -- ответ не артефакт.
ПОРОГИ_СОБЫТИЙ = (0, 50, 100, 200, 300, 500, 800)


def otbor_po_sobytijam(p: dict, porog: int) -> bool:
    return (p.get("sobytij_v_okne") or 0) >= porog


def otbor_po_oborotu(p: dict, porog: float) -> bool:
    об = oborot_pozicii(p, ГОРИЗОНТ_ФИЛЬТРА)
    return об is not None and об >= porog


def itogi_otbora(sutki: list, *, razmer: float, gorizont: int, otbor) -> list:
    """Итоги позиций, прошедших отбор, на одном горизонте и размере."""
    из_ = []
    for с in sutki:
        for п in с["pozicii"]:
            if not otbor(п):
                continue
            в = п["vyhody"].get(gorizont)
            фи = п.get("fee")
            if not в or фи is None:
                continue
            try:
                из_.append(lp_itog(y0=float(п["vhod"][0]), x0=float(п["vhod"][1]),
                                   y1=float(в[0]), x1=float(в[1]),
                                   razmer=razmer, fee_torgovli=float(фи)))
            except НетДанных:
                continue
    return из_


def svod_po_sutkam(sutki: list, *, razmer: float, gorizont: int, otbor) -> dict:
    """ПО СУТКАМ: на позицию, в сутки, доля в плюсе, ДИ по суткам."""
    строки = []
    for с in sutki:
        р = itogi_otbora([с], razmer=razmer, gorizont=gorizont, otbor=otbor)
        если = [х["itog"] for х in р]
        if not если:
            continue
        строки.append({
            "den": с["den"], "pozicij": len(если),
            "sr": statistics.fmean(если), "us": _стат(если)["ус"],
            "med": statistics.median(если),
            "v_pljuse": sum(1 for х in если if х > 0) / len(если),
            "za_sutki": statistics.fmean(если) * len(если),
            "dohod_oborota_med": statistics.median([х["dohod_oborota"] for х in р]),
            "rashody_med": statistics.median([х["rashody"] for х in р])})
    if not строки:
        return {"stroki": [], "why_not": "ни одной позиции не прошло отбор"}
    return {"stroki": строки,
            "pozicij_v_sutki": statistics.fmean(с["pozicij"] for с in строки),
            "na_poziciju_sr": statistics.fmean(с["sr"] for с in строки),
            "na_poziciju_us": statistics.fmean(с["us"] for с in строки),
            "na_poziciju_med": statistics.fmean(с["med"] for с in строки),
            "v_pljuse": statistics.fmean(с["v_pljuse"] for с in строки),
            "za_sutki": statistics.fmean(с["za_sutki"] for с in строки),
            "di_za_sutki": di_po_sutkam([с["za_sutki"] for с in строки]),
            "di_na_poziciju": di_po_sutkam([с["sr"] for с in строки]),
            "why_not": None}


def otbit_krug(svod: dict, *, gorizont: int) -> dict:
    """ЗА СКОЛЬКО ОБОРОТ ОТБИВАЕТ КРУГ, если его скорость не меняется.

    Расходы круга платятся ОДИН РАЗ, а доход идёт со временем. Значит вопрос
    «платит ли LP» сводится к одному числу: сколько надо держать, чтобы доход
    дошёл до расходов. Допущение названо прямо: скорость оборота ТА ЖЕ, что в
    первые секунды. Архив её дальше окна не показывает, поэтому это НЕ
    предсказание, а порог, который следующий замер должен проверить.
    """
    стр = svod.get("stroki") or []
    if not стр:
        return {"why_not": svod.get("why_not") or "нет позиций"}
    д = statistics.fmean(с["dohod_oborota_med"] for с in стр)
    р = statistics.fmean(с["rashody_med"] for с in стр)
    сек_гор = gorizont * СЕКУНД_НА_СЛОТ
    if д <= 0:
        return {"rashody": р, "dohod_za_gorizont": д,
                "why_not": "доход от оборота не положителен -- отбивать нечем"}
    сек = сек_гор * р / д
    return {"rashody": р, "dohod_za_gorizont": д, "gorizont_sekund": сек_гор,
            "nuzhno_sekund": сек, "nuzhno_minut": сек / 60,
            "nuzhno_chasov": сек / 3600,
            "dopushchenije": ("скорость оборота та же, что в первые "
                              f"{сек_гор:.0f} с -- архив дальше не показывает")}

# ------------------------------------------------- п.3: другие площадки

def drugije_ploshchadki(sutki_vse: list) -> dict:
    """МЕТЕОРА И ПРОЧИЕ: есть ли их сделки с резервами в архиве. Нет -- так и
    сказано числом, а не додумано."""
    свод: dict = {}
    for с in sutki_vse:
        for тип, n in (с.get("tipy_pulov") or {}).items():
            свод[тип] = свод.get(тип, 0) + n
    искали = ("meteora-damm-v1", "meteora-damm-v2", "meteora-dlmm")
    return {"tipy_v_arhive": dict(sorted(свод.items(), key=lambda кв: -кв[1])),
            "iskali": {и: свод.get(и, 0) for и in искали},
            "why_not": ("ни одного сигнала Meteora в архиве за взятые сутки"
                        if not any(свод.get(и) for и in искали) else None)}


# ------------------------------------------------- статистика Code-2

def _стат(xs) -> dict:
    """СТАТИСТИКА -- ЕГО ФУНКЦИЕЙ (podbivka_hvosty.стат): n, среднее,
    усечённое без верхних 5 %, медиана, p95, максимум. Своей копии нет."""
    H = _hvosty()
    return H.стат(xs)


_КЭШ: dict = {}


def _hvosty():
    if "H" in _КЭШ:
        return _КЭШ["H"]
    if not КОД_CODE2:
        raise НетДанных(
            "путь к модулям Code-2 не задан: нужен C3_LP_KOD_CODE2 "
            "(ветка claude/podbivka, analysis/podbivka_hvosty.py). Своей "
            "копии его статистики здесь нет и не будет")
    if КОД_CODE2 not in sys.path:
        sys.path.insert(0, КОД_CODE2)
    import podbivka_hvosty as H  # noqa: PLC0415
    _КЭШ["H"] = H
    return H


def ст_code2(e: dict):
    """Резервы события -- ЕГО же функцией podbivka_arhiv_den.ст."""
    if "A" not in _КЭШ:
        if not КОД_CODE2:
            raise НетДанных("путь к модулям Code-2 не задан")
        if КОД_CODE2 not in sys.path:
            sys.path.insert(0, КОД_CODE2)
        import podbivka_arhiv_den as A  # noqa: PLC0415
        _КЭШ["A"] = A
    return _КЭШ["A"].ст(e)


# ============================================================ РЯД СДЕЛОК
# ЗАЧЕМ ОТДЕЛЬНЫЙ СЧЁТ ПО РЯДУ, А НЕ ПО ДВУМ СНИМКАМ.
# По двум снимкам доход от оборота выводится из роста k. Это оказалось
# НЕНАДЁЖНЫМ СЧЁТЧИКОМ, и это измерено: на 16 481 позиции суточного архива
# k ПАДАЕТ в 6 193 случаях (37.6 %), хотя сделка растить k обязана -- комиссия
# LP остаётся в пуле. Падения не мелкие: медиана 0.39 %, p90 4.6 %, p99 31 %,
# максимум 85.7 %; больше 1 % падают 1 946 окон, больше 10 % -- 286, больше
# 50 % -- 16. Значит в резервах сидит ещё что-то, кроме сделок: снятие чужой
# ликвидности и сбор накопленных комиссий (Pool.protocol_fees и
# Pool.creator_fees лежат в том же счёте пула). ПОЭТОМУ ПО РЯДУ СДЕЛОК ОБОРОТ
# СУММИРУЕТСЯ НАПРЯМУЮ (quoteAmount каждой сделки), а k не используется вовсе.
#
# ВХОД -- РЯД СОБЫТИЙ ОДНОГО ПУЛА В ТОМ ВИДЕ, В КАКОМ ИХ ОТДАЁТ ЧИТАЛКА
# CODE-2 (podbivka_arhiv_den.КЛЮЧИ): action, quoteAmount, tokenAmount,
# quoteInPool, tokensInPool, poolFeeRate, block, timestamp. Своего формата не
# придумываю.

ГОРИЗОНТЫ_СЕКУНД = (600, 3600, 21600, 86400)   # 10 мин, 1 ч, 6 ч, 24 ч
ПОРОГ_ОКНО_СЕКУНД = 600                        # «события первых 10 минут»

# BOOST -- НЕ ОБОРОТ. Выкуп за счёт BOOST двигает цену и резервы, но это не
# сделка торговца, и комиссии LP с него нет. Дискриминаторы посчитаны по
# sha256('global:имя') и sha256('event:Имя'), как и везде:
#   boost_buy_and_burn   инструкция 694406af000723a2
#   BoostBuyAndBurnEvent событие    3f451c16305cc2b9
# Снятие и вклад ликвидности -- тоже не оборот:
#   deposit  f223c68952e1f2b6 / DepositEvent  78f83d531f8e6b90
#   withdraw b712469c946da122 / WithdrawEvent 1609851aa02c47c0
НЕ_ОБОРОТ_ДЕЙСТВИЯ = frozenset({
    "boost_buy_and_burn", "boost", "init_boost",
    "deposit", "withdraw", "add_liquidity", "remove_liquidity"})
ОБОРОТ_ДЕЙСТВИЯ = frozenset({"buy", "sell"})


def _rez(e: dict) -> tuple | None:
    """Резервы события -- через функцию Code-2, если путь к нему задан."""
    try:
        return ст_code2(e)
    except НетДанных:
        x, y = e.get("quoteInPool"), e.get("tokensInPool")
        if not x or not y:
            return None
        return float(x), float(y)


def _учесть_вывод(действие: str, y_до: float, y_после: float,
                  прошло: float, вывод: dict) -> None:
    """ВЫХОД ЛИКВИДНОСТИ: сколько, когда впервые, какую долю глубины забрали.

    `withdraw` в ленте и падение резервов -- ДВА РАЗНЫХ ПУТИ одного события,
    и они считаются по отдельности: по какому пути увидено, столько и стоит
    доверия. Если лента несёт действие -- верим ей; если нет, остаётся только
    падение резервов, и об этом в отчёте сказано числом.
    """
    if y_после >= y_до or y_до <= 0:
        if "deposit" in действие or "add_liquidity" in действие:
            вывод["vklad_chuzhoj"] += 1
        return
    забрали = 1 - y_после / y_до
    вывод["sobytij"] += 1
    вывод["dolja_glubiny"] += забрали
    if вывод["sekund_do_pervogo"] is None:
        вывод["sekund_do_pervogo"] = прошло
    if "rezerv" in действие:
        вывод["po_rezervam"] += 1
    else:
        вывод["po_dejstviju"] += 1


def lp_po_rjadu(rjad: list, *, razmer: float, gorizont_sekund: int,
                lp_bps: int = LP_BPS_ИЗМЕРЕНО,
                fee_torgovli: float | None = None) -> dict:
    """ИТОГ ПОЗИЦИИ LP ПО РЯДУ СДЕЛОК ПУЛА. Вход -- первое событие ряда.

    Оборот суммируется ИЗ СДЕЛОК, а не из роста k. BOOST и движения
    ликвидности в оборот НЕ идут и считаются отдельно. Наша доля
    пересчитывается на каждом движении чужой ликвидности -- в этом и смысл
    ряда: по двум снимкам такое движение неотличимо от оборота.
    """
    события = [е for е in rjad if _rez(е)]
    if len(события) < 2:
        raise НетДанных("в ряду меньше двух событий с резервами")
    события.sort(key=lambda е: (е.get("block") or 0, е.get("timestamp") or 0))
    первое = события[0]
    # МЕТКА ВРЕМЕНИ ОБЯЗАТЕЛЬНА, И ПРОВЕРЯЕТСЯ НА None, А НЕ НА ЛОЖЬ.
    # Здесь стояло `or 0`, и у события с меткой 0 горизонт отключался
    # ЦЕЛИКОМ: все сделки попадали в любой горизонт, 10 минут и сутки давали
    # один и тот же оборот. На живых данных метки в миллисекундах и не нули,
    # поэтому молча сошло бы за правду -- ровно то, чего быть не должно.
    т0 = первое.get("timestamp")
    if т0 is None:
        raise НетДанных("у первого события ряда нет метки времени -- горизонт "
                        "в секундах считать нечем")
    р0 = _rez(первое)
    y0, x0 = р0[0], р0[1]
    if min(y0, x0) <= 0:
        raise НетДанных(WHY_НЕТ_СОСТОЯНИЙ)
    фи = (float(fee_torgovli) if fee_torgovli is not None
          else float(первое.get("poolFeeRate") or 0.0))
    X = float(razmer)
    p0 = y0 / x0
    # НАША ДОЛЯ -- ПО КОТИРОВОЧНОЙ СТОРОНЕ, как и в счёте по двум снимкам.
    доля = X / (y0 + X)
    оборот = доход = 0.0
    без_метки = за_горизонтом = 0
    буст_объём = буст_событий = 0.0
    ликв_событий = 0
    ликв_сдвиг_доли = 0.0
    # ВЫХОД ЛИКВИДНОСТИ -- ОТДЕЛЬНОЙ СТРОКОЙ: сколько раз, когда впервые,
    # какую долю глубины забрали и каким путём это увидено.
    вывод = {"sobytij": 0, "sekund_do_pervogo": None, "dolja_glubiny": 0.0,
             "po_dejstviju": 0, "po_rezervam": 0, "vklad_chuzhoj": 0}
    y, x = y0, x0
    последнее = первое
    событий_в_окне_порога = 0
    for е in события[1:]:
        т = е.get("timestamp")
        if т is None:
            # Событие без метки времени в горизонт отнести нельзя: пропускаем
            # и считаем, а не приписываем его к началу ряда.
            без_метки += 1
            continue
        прошло = (т - т0) / 1000.0
        if прошло <= ПОРОГ_ОКНО_СЕКУНД:
            событий_в_окне_порога += 1
        if прошло > gorizont_sekund:
            # ПРОПУСК, А НЕ ВЫХОД ИЗ ЦИКЛА. Здесь стоял break, и он молча
            # обрезал ряд, если метки времени не строго по блокам: первая же
            # сделка за горизонтом прекращала счёт, и всё, что лежало за ней
            # по порядку блоков, но В горизонте, терялось -- вместе с
            # движениями чужой ликвидности. На живых данных метка растёт
            # вместе с блоком, и разницы не было бы; но данных, на которых
            # это можно проверить, у меня нет, поэтому порядок не
            # предполагается.
            за_горизонтом += 1
            continue
        р = _rez(е)
        yн, xн = р[0], р[1]
        действие = str(е.get("action") or "").lower()
        q = abs(float(е.get("quoteAmount") or 0.0))
        if действие in ОБОРОТ_ДЕЙСТВИЯ:
            оборот += q
            доход += (lp_bps / 10_000) * q * доля
        elif действие in НЕ_ОБОРОТ_ДЕЙСТВИЯ:
            if "boost" in действие:
                буст_объём += q
                буст_событий += 1
            else:
                # ЧУЖАЯ ЛИКВИДНОСТЬ: наша доля размывается или растёт.
                # Цена при этом не меняется, меняется глубина.
                if yн > 0 and y > 0:
                    было = доля
                    наша_котировка = доля * y
                    доля = (наша_котировка / yн) if yн > 0 else доля
                    доля = min(max(доля, 0.0), 1.0)
                    ликв_событий += 1
                    ликв_сдвиг_доли += доля - было
                    _учесть_вывод(действие, y, yн, прошло, вывод)
        # ВТОРОЙ ПУТЬ ОБНАРУЖЕНИЯ -- ПО РЕЗЕРВАМ, И ОН ИДЁТ ОТДЕЛЬНОЙ
        # ПРОВЕРКОЙ, А НЕ ВЕТКОЙ `elif`. Здесь стоял elif после ветки
        # оборота, и для действий buy/sell он был НЕДОСТИЖИМ: первая ветка
        # уже сработала. То есть второй путь не работал вовсе, а проверка
        # это поймала.
        # Лента PumpApi может не нести отдельного действия withdraw -- тогда
        # вывод виден только так: ОБА резерва падают пропорционально, глубина
        # ушла, а цена осталась. Сделка так выглядеть не может: она двигает
        # цену. Порог 2 % взят выше шума округления резервов.
        if (действие not in НЕ_ОБОРОТ_ДЕЙСТВИЯ and y > 0 and x > 0
                and yн < y and xн < x and yн > 0 and xн > 0):
            дол_y, дол_x = 1 - yн / y, 1 - xн / x
            цена_стоит = abs((yн / xн) / (y / x) - 1) < 0.02
            if цена_стоит and min(дол_y, дол_x) > 0.02:
                было = доля
                доля = min(max(доля * y / yн, 0.0), 1.0)
                ликв_событий += 1
                ликв_сдвиг_доли += доля - было
                _учесть_вывод("withdraw_po_rezervam", y, yн, прошло, вывод)
        y, x = yн, xн
        последнее = е
    if последнее is первое:
        raise НетДанных("на горизонте не нашлось ни одного события после входа")
    p1 = y / x
    r = p1 / p0
    # СТОИМОСТЬ ПОЗИЦИИ: наша доля конечных резервов плюс накопленный доход.
    # Доход считается отдельно, а не через рост k, поэтому из резервов его
    # вычитать не надо -- он в них УЖЕ лежит, но и доля его уже учитывает.
    позиция = 2 * X * math.sqrt(r)
    вклад = 2 * X
    потеря_цены = -X * (math.sqrt(r) - 1) ** 2
    # КРУГ ВХОДА-ВЫХОДА -- как и в счёте по снимкам.
    комиссия_ноги = фи * X * (1 + math.sqrt(r))
    сдвиг_входа = X * X / (y0 + X)
    нога_на_выходе = X * math.sqrt(r)
    сдвиг_выхода = нога_на_выходе ** 2 / (y + нога_на_выходе) if y > 0 else 0.0
    сеть = ИЗДЕРЖКИ_ТРАНЗАКЦИИ * ТРАНЗАКЦИЙ_В_КРУГЕ
    расходы = комиссия_ноги + сдвиг_входа + сдвиг_выхода + сеть
    итог = позиция + доход - вклад - расходы
    return {"sobytij": len(события), "gorizont_sekund": gorizont_sekund,
            "p0": p0, "p1": p1, "r": r,
            "oborot": оборот, "dohod": доход,
            "boost_objom": буст_объём, "boost_sobytij": буст_событий,
            "likv_sobytij": ликв_событий, "likv_sdvig_doli": ликв_сдвиг_доли,
            "vyhod_likvidnosti": вывод,
            "dolja_pula_vhod": X / (y0 + X), "dolja_pula_vyhod": доля,
            "pozicija": позиция, "vklad": вклад,
            "poterja_ceny": потеря_цены,
            "protiv_derzhat": позиция + доход - X * (1 + r),
            "komissija_nogi": комиссия_ноги, "sdvig_vhoda": сдвиг_входа,
            "sdvig_vyhoda": сдвиг_выхода, "set": сеть, "rashody": расходы,
            "itog": итог,
            "sobytij_pervyh_10_min": событий_в_окне_порога,
            "sobytij_bez_metki": без_метки,
            "sobytij_za_gorizontom": за_горизонтом}


def rjady_iz_fajla(put: Path) -> dict:
    """РЯДЫ ПУЛОВ ИЗ ФАЙЛА ЧИТАЛКИ CODE-2: {poolId: [события]}.

    Ждём ровно то, что его читалка умеет писать: либо {"ряды": {...}}, либо
    {"ленты_минтов": {...}}, либо плоский список событий с полем poolId.
    Другого формата не придумываю: придёт иное -- отказ по имени.
    """
    открыть = gzip.open if str(put).endswith(".gz") else open
    with открыть(put, "rt", encoding="utf-8") as ф:
        д = json.load(ф)
    if isinstance(д, dict):
        for ключ in ("ряды", "ряды_целей", "ленты_минтов", "rjady"):
            если = д.get(ключ)
            if isinstance(если, dict) and если:
                return {к: list(в) for к, в in если.items()}
        если = д.get("события") or д.get("sobytija")
        if isinstance(если, list) and если:
            д = если
    if isinstance(д, list):
        из_: dict = {}
        for е in д:
            к = е.get("poolId") or е.get("pool_id")
            if к:
                из_.setdefault(к, []).append(е)
        if из_:
            return из_
    raise НетДанных(
        "в файле не нашлось рядов пулов: ждал ключ 'ряды' / 'ленты_минтов' "
        "или плоский список событий с poolId -- того, что пишет читалка "
        "Code-2. Своего формата не придумываю")


def _svod_vyhoda(stroki: list) -> dict:
    """ВЫХОД ЛИКВИДНОСТИ ПО ВЫБОРКЕ: доля пулов, когда, сколько."""
    с_выводом = [с for с in stroki if (с["vyhod_likvidnosti"]["sobytij"] or 0)]
    если = [с["vyhod_likvidnosti"] for с in с_выводом]
    когда = [в["sekund_do_pervogo"] for в in если
             if в["sekund_do_pervogo"] is not None]
    сколько = [в["dolja_glubiny"] for в in если]
    return {
        "pulov_vsego": len(stroki), "pulov_s_vyhodom": len(с_выводом),
        "dolja_pulov": (len(с_выводом) / len(stroki)) if stroki else None,
        "sekund_do_pervogo_med": (statistics.median(когда) if когда else None),
        "minut_do_pervogo_med": (statistics.median(когда) / 60
                                 if когда else None),
        "dolja_glubiny_med": (statistics.median(сколько) if сколько else None),
        "dolja_glubiny_p90": (sorted(сколько)[int(0.9 * (len(сколько) - 1))]
                              if сколько else None),
        "uvideno_po_dejstviju": sum(в["po_dejstviju"] for в in если),
        "uvideno_po_rezervam": sum(в["po_rezervam"] for в in если),
        "vkladov_chuzhih": sum(в["vklad_chuzhoj"] for в in
                               (с["vyhod_likvidnosti"] for с in stroki))}


# РАЗРЕЗ НА ПОЛОВИНЫ СУТОК. Артефакт Code-2 -- 48 часов 08--09.10, то есть
# входы (переезды) лежат в ОДНИХ сутках 10-08. При разрезе по дате вторая
# половина вышла бы ПУСТОЙ: подбор порога остался бы без проверки, а ДИ по
# суткам не посчитался бы вовсе («суток меньше двух»). Поэтому единица
# разреза -- ПОЛОВИНА СУТОК, 12 часов: 10-08/00--11 и 10-08/12--23. Названо
# это так, как есть: не «сутки», а половина, и в отчёте стоит именно она.
ЧАСОВ_В_ПОЛОВИНЕ = 12


def sutki_rjada(rjad: list, *, chasov: int = ЧАСОВ_В_ПОЛОВИНЕ) -> str:
    """ОТРЕЗОК позиции -- по метке времени ПЕРВОГО события ряда (UTC).

    `chasov` = 24 даёт сутки, 12 -- половину суток. Метка берётся у первого
    события, то есть у ВХОДА: позиция относится к тому отрезку, когда её
    открыли, а не когда закрыли.
    """
    for е in rjad:
        т = е.get("timestamp")
        if т is not None:
            сек = т / 1000.0
            if chasov >= 24:
                return time.strftime("%Y-%m-%d", time.gmtime(сек))
            н = int(сек // (3600 * chasov)) * 3600 * chasov
            ч = time.gmtime(н)
            return (f"{time.strftime('%Y-%m-%d', ч)}"
                    f"/{ч.tm_hour:02d}-{(ч.tm_hour + chasov - 1) % 24:02d}")
    return "?"


def progon_rjady(puti: list, *, razmery: tuple = (1.0, 5.0, 20.0),
                 chasov_otrezka: int = ЧАСОВ_В_ПОЛОВИНЕ) -> dict:
    """ВЕСЬ РАСЧЁТ ПО РЯДАМ: горизонты x размеры, отбор, подбор и проверка.

    Отбор -- по числу событий ПЕРВЫХ 10 МИНУТ (п.3 задания). Подбор порога на
    ПЕРВОЙ ПОЛОВИНЕ суток, проверка на второй: иначе порог подогнан под свой
    же шум.
    """
    ряды: dict = {}
    for п in puti:
        for пул, р in rjady_iz_fajla(Path(п)).items():
            ряды.setdefault(пул, []).extend(р)
    if not ряды:
        raise НетДанных("ни одного ряда пула не прочитано")
    по_суткам: dict = {}
    for пул, р in ряды.items():
        по_суткам.setdefault(sutki_rjada(р, chasov=chasov_otrezka),
                             []).append((пул, р))
    сутки = sorted(по_суткам)
    if len(сутки) < 2:
        raise НетДанных(
            f"отрезков всего {len(сутки)} ({сутки}) -- подбор порога и "
            "проверку на отложенном не разделить. Уменьшите --chasov-otrezka "
            "или дайте больше часов архива")
    полов = max(1, len(сутки) // 2)
    калибр, проверка = сутки[:полов], сутки[полов:]
    из_ = {"fajlov": [str(п) for п in puti], "pulov": len(ряды),
           "chasov_v_otrezke": chasov_otrezka,
           "otrezkov": сутки, "kalibr": калибр, "proverka": проверка,
           "po_gorizontam": {}, "porog": {}}

    def счёт(дни, X, H, порог):
        строки, пропущено = [], {"malo_sobytij": 0, "net_dannyh": 0}
        for д in дни:
            for _пул, р in по_суткам[д]:
                try:
                    из__ = lp_po_rjadu(р, razmer=X, gorizont_sekund=H)
                except НетДанных:
                    пропущено["net_dannyh"] += 1
                    continue
                if из__["sobytij_pervyh_10_min"] < порог:
                    пропущено["malo_sobytij"] += 1
                    continue
                из__["den"] = д
                строки.append(из__)
        return строки, пропущено

    for X in razmery:
        for H in ГОРИЗОНТЫ_СЕКУНД:
            стр, проп = счёт(сутки, X, H, 0)
            если = [с["itog"] for с in стр]
            if not если:
                из_["po_gorizontam"][f"{X:g}SOL|{H}"] = {
                    "why_not": "ни одной позиции", "propushcheno": проп}
                continue
            ср_дни = {}
            for с in стр:
                ср_дни.setdefault(с["den"], []).append(с["itog"])
            из_["po_gorizontam"][f"{X:g}SOL|{H}"] = {
                "razmer": X, "gorizont_sekund": H, "pozicij": len(если),
                "stat_itog": _стат(если),
                "stat_protiv_derzhat": _стат([с["protiv_derzhat"] for с in стр]),
                "dohod_med": statistics.median([с["dohod"] for с in стр]),
                "oborot_med": statistics.median([с["oborot"] for с in стр]),
                "rashody_med": statistics.median([с["rashody"] for с in стр]),
                "boost_objom_med": statistics.median(
                    [с["boost_objom"] for с in стр]),
                "pulov_s_boost": sum(1 for с in стр if с["boost_sobytij"]),
                "pulov_s_dvizhenijem_likvidnosti": sum(
                    1 for с in стр if с["likv_sobytij"]),
                "vyhod_likvidnosti": _svod_vyhoda(стр),
                "dolja_vhod_med": statistics.median(
                    [с["dolja_pula_vhod"] for с in стр]),
                "dolja_vyhod_med": statistics.median(
                    [с["dolja_pula_vyhod"] for с in стр]),
                "v_pljuse": sum(1 for х in если if х > 0) / len(если),
                "propushcheno": проп,
                "di_po_sutkam": di_po_sutkam(
                    [statistics.fmean(в) for в in ср_дни.values()]),
                "za_sutki": statistics.fmean(
                    [statistics.fmean(в) * len(в) for в in ср_дни.values()])}
    for X in razmery:
        подбор = []
        for порог in (0, 10, 25, 50, 100, 200, 400):
            стр, _ = счёт(калибр, X, 86400, порог)
            если = [с["itog"] for с in стр]
            подбор.append({"porog_sobytij": порог, "pozicij": len(если),
                           "stat": _стат(если),
                           "v_pljuse": (sum(1 for х in если if х > 0) / len(если)
                                        if если else None)})
        годные = [с for с in подбор if (с["stat"].get("n") or 0) >= 30]
        лучший = max(годные, key=lambda с: с["stat"]["ус"]) if годные else None
        рез = None
        if лучший:
            стр, проп = счёт(проверка, X, 86400, лучший["porog_sobytij"])
            если = [с["itog"] for с in стр]
            по_дням: dict = {}
            for с in стр:
                по_дням.setdefault(с["den"], []).append(с["itog"])
            рез = {"porog_sobytij": лучший["porog_sobytij"],
                   "pozicij": len(если), "propushcheno": проп,
                   "stat": _стат(если) if если else {"n": 0},
                   "v_pljuse": (sum(1 for х in если if х > 0) / len(если)
                                if если else None),
                   "pozicij_v_sutki": (len(если) / len(по_дням)
                                       if по_дням else None),
                   "za_sutki": (statistics.fmean(
                       [statistics.fmean(в) * len(в) for в in по_дням.values()])
                       if по_дням else None),
                   "di_za_sutki": di_po_sutkam(
                       [statistics.fmean(в) * len(в) for в in по_дням.values()])}
        из_["porog"][f"{X:g}SOL"] = {"podbor": подбор, "luchshij": лучший,
                                     "proverka": рез}
    return из_


# ------------------------------------------------- самопроверка

# ЧИСЛО ОБЪЯВЛЕНО. Меньше -- проверку убрали и никто не заметил; больше --
# добавили и не сказали. И то и другое здесь ПРОВАЛ.
ZHDEM_PROVEROK = 41


def self_test() -> int:  # noqa: C901, PLR0915
    было, плохо = 0, 0
    упавшие: list = []

    def chk(имя, условие, подробность=None):
        nonlocal было, плохо
        было += 1
        if условие:
            print(f"  ok   {имя}")
        else:
            плохо += 1
            упавшие.append(имя)
            print(f" ПЛОХО {имя} -- {подробность}")

    # ----- 1. ФОРМУЛЫ: проверяются тождествами, а не на глаз
    ровно = lp_itog(y0=100, x0=1000, y1=100, x1=1000, razmer=1,
                    fee_torgovli=0.0)
    chk("ЦЕНА НЕ УЕХАЛА И ОБОРОТА НЕ БЫЛО: позиция стоит ровно столько, "
        "сколько вложено, доход от оборота и потеря на цене -- ноль",
        abs(ровно["pozicija"] - ровно["vklad"]) < 1e-9
        and abs(ровно["dohod_oborota"]) < 1e-9
        and abs(ровно["poterja_ceny"]) < 1e-9, ровно)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: при нулевом обороте итог УБЫТОЧЕН ровно на "
        "расходы круга -- сеть и свой сдвиг цены, без всякой магии",
        ровно["itog"] < 0
        and abs(ровно["itog"] + ровно["rashody"]) < 1e-9, ровно)
    # Непостоянная потеря: цена вдвое, оборота нет.
    вдвое = lp_itog(y0=100, x0=1000, y1=200, x1=500, razmer=1,
                    fee_torgovli=0.0)
    ждём_потерю = -1 * (math.sqrt(4.0) - 1) ** 2
    chk(f"НЕПОСТОЯННАЯ ПОТЕРЯ СЧИТАЕТСЯ ТОЧНО: цена вчетверо (r={вдвое['r']:.0f}) "
        f"даёт -(sqrt(r)-1)^2 = {ждём_потерю:.3f} на SOL стороны",
        abs(вдвое["poterja_ceny"] - ждём_потерю) < 1e-9
        and abs(вдвое["K"] - 1) < 1e-12, вдвое)
    chk("ПОТЕРЯ НА ЦЕНЕ ВСЕГДА НЕ ПОЛОЖИТЕЛЬНА -- на 200 значениях r от 0.01 "
        "до 100 ни одного исключения",
        all(lp_itog(y0=100, x0=1000, y1=100 * math.sqrt(rr),
                    x1=1000 / math.sqrt(rr), razmer=1,
                    fee_torgovli=0.0)["poterja_ceny"] <= 1e-12
            for rr in [0.01 * (1.1 ** и) for и in range(200)]))
    # Доход от оборота: k вырос, цена на месте.
    обор = lp_itog(y0=100, x0=1000, y1=100.5, x1=1005, razmer=1,
                   fee_torgovli=0.0)
    chk("ДОХОД ОТ ОБОРОТА: k вырос на 1 %, цена на месте -- доход равен "
        f"2*(sqrt(K)-1) = {2 * (math.sqrt(1.01005 ** 2) - 1):.5f} на сторону, "
        "и он ПОЛОЖИТЕЛЕН",
        обор["dohod_oborota"] > 0 and abs(обор["r"] - 1) < 1e-12
        and abs(обор["dohod_oborota"] - 2 * (math.sqrt(обор["K"]) - 1)) < 1e-9,
        обор)
    chk("ПОЗИЦИЯ = ДЕРЖАТЬ + ПОТЕРЯ ЦЕНЫ + ДОХОД ОБОРОТА -- тождество сходится "
        "на случайных r и K, а не только в круглых числах",
        all(abs((лп := lp_itog(y0=100, x0=1000, y1=100 * a, x1=1000 * b,
                               razmer=3, fee_torgovli=0.0))["pozicija"]
                - (лп["derzhat"] + лп["poterja_ceny"] + лп["dohod_oborota"]))
            < 1e-7
            for a, b in ((1.3, 0.9), (0.7, 1.4), (2.0, 1.1), (0.5, 0.95))))

    # ----- 2. ЧТО АРХИВ НЕ ДАЁТ -- ОТКАЗ ПО ИМЕНИ
    г = gorizonty_est()
    chk(f"ОКНО АРХИВА НАЗВАНО ЧИСЛОМ: {г['okno_slotov']} слотов = "
        f"{г['okno_sekund']} с при измеренных {СЕКУНД_НА_СЛОТ} с/слот",
        г["okno_slotov"] == 160 and 30 < г["okno_sekund"] < 40, г)
    chk("ВСЕ ЧЕТЫРЕ ЗАКАЗАННЫХ ГОРИЗОНТА (10 мин / 1 ч / 6 ч / 24 ч) В АРХИВЕ "
        "ОТСУТСТВУЮТ, и у каждого сказано, во сколько раз он дальше окна: "
        + ", ".join(f"{и} в {в['vo_skolko_raz_dalshe_okna']} раза"
                    for и, в in г["zakazano_net"].items()),
        set(г["zakazano_net"]) == set(ЗАКАЗАНЫ)
        and all(в["vo_skolko_raz_dalshe_okna"] > 15
                for в in г["zakazano_net"].values()), г["zakazano_net"])
    упало = None
    try:
        posчитать_sutki({"den": "x", "pozicii": []}, razmer=1, gorizont=3600)
    except НетДанных as сбой:
        упало = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: горизонт за окном -- ОТКАЗ по имени, а не "
        "подстановка ближайшего и не ноль",
        упало and WHY_НЕТ_ОКНА in упало, упало)
    упало2 = None
    try:
        lp_itog(y0=0, x0=1000, y1=1, x1=1, razmer=1, fee_torgovli=0.0)
    except НетДанных as сбой:
        упало2 = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: нет снимка резервов -- отказ, а не счёт по нулям",
        упало2 and WHY_НЕТ_СОСТОЯНИЙ in упало2, упало2)

    # ----- 3. СТАВКИ -- ИЗ ПРОГРАММЫ, А НЕ ИЗ ПАМЯТИ
    chk(f"ДОЛЯ ПОСТАВЩИКА -- {LP_BPS_ИЗМЕРЕНО} б.п., и это ИЗМЕРЕНО по "
        f"BuyEvent.lp_fee_basis_points самой программы: {LP_BPS_РАСПРЕДЕЛЕНИЕ} "
        "на 40 событиях, разбор проверен тождеством lp_fee = база*bps/10000",
        LP_BPS_ИЗМЕРЕНО == 20
        and sum(LP_BPS_РАСПРЕДЕЛЕНИЕ.values()) == 40
        and max(LP_BPS_РАСПРЕДЕЛЕНИЕ, key=LP_BPS_РАСПРЕДЕЛЕНИЕ.get) == 20)
    chk("ЗА ВХОД И ВЫХОД ЛИКВИДНОСТИ ПРОГРАММА НЕ БЕРЁТ НИЧЕГО: у deposit и "
        "withdraw в закреплённом IDL ни одного счёта комиссии",
        КОМИССИЯ_ВКЛАДА_BPS == 0 and _net_fee_scheta_v_idl())
    chk("ПОЛНАЯ СТАВКА ТОРГОВЦА БЕРЁТСЯ ИЗ АРХИВА ПОЛЕМ fee, а не из "
        f"{LP_BPS_ИЗМЕРЕНО} б.п.: поставщику идёт ПЯТАЯ ЧАСТЬ от 100 б.п., и "
        "спутать их значит завысить доход впятеро",
        abs(lp_itog(y0=100, x0=1000, y1=100, x1=1000, razmer=1,
                    fee_torgovli=0.01)["komissija_nogi"] - 0.02) < 1e-9)

    # ----- 4. ДОЛЯ ПУЛА И РАЗМЕР
    доли = {X: lp_itog(y0=330, x0=6.1e7, y1=330, x1=6.1e7, razmer=X,
                       fee_torgovli=0.01)["dolja_pula"] for X in РАЗМЕРЫ}
    chk("ДОЛЯ ПУЛА РАСТЁТ С РАЗМЕРОМ И НАЗВАНА ЧИСЛОМ: на пуле 330 SOL "
        + ", ".join(f"{X:g} SOL -> {д * 100:.2f} %" for X, д in доли.items()),
        доли[1.0] < доли[5.0] < доли[20.0] and доли[20.0] < 0.07, доли)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: свой сдвиг цены РАСТЁТ КВАДРАТИЧНО по размеру -- "
        "вчетверо больший вклад даёт вчетверо больший сдвиг на SOL, и "
        "поэтому 20 SOL не просто в 20 раз хуже 1 SOL",
        (lp_itog(y0=330, x0=6.1e7, y1=330, x1=6.1e7, razmer=20,
                 fee_torgovli=0.0)["sdvig_vhoda"] / 20)
        > 15 * (lp_itog(y0=330, x0=6.1e7, y1=330, x1=6.1e7, razmer=1,
                        fee_torgovli=0.0)["sdvig_vhoda"] / 1))

    # ----- 5. ДОВЕРИТЕЛЬНЫЙ ИНТЕРВАЛ -- ПО СУТКАМ
    ди = di_po_sutkam([1.0, 1.2, 0.8, 1.1, 0.9, 1.0, 1.05])
    chk("ДИ СЧИТАЕТСЯ ПО СУТОЧНЫМ СРЕДНИМ (7 суток, t=2.447), а не по "
        "позициям: внутри суток позиции не независимы",
        ди["n"] == 7 and ди["di95"][0] < ди["sr"] < ди["di95"][1]
        and abs(ди["polushirina"] - 2.447 * ди["sd"] / math.sqrt(7)) < 1e-9, ди)
    ди1 = di_po_sutkam([1.0])
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: одни сутки -- интервал НЕ считается, и сказано "
        "почему, а не нарисован нулевой",
        ди1["di95"] is None and "меньше двух" in ди1["why_not"], ди1)

    # ----- 6. СТАТИСТИКА -- ЕГО ФУНКЦИЕЙ
    try:
        с = _стат([1, 2, 3, 4, 5, 100])
        есть_его = с.get("ус") == 3.0 and с.get("n") == 6
    except НетДанных as сбой:
        с, есть_его = str(сбой), False
    chk("СРЕДНЕЕ БЕЗ ВЕРХНИХ 5 % СЧИТАЕТСЯ ЕГО ФУНКЦИЕЙ "
        "(podbivka_hvosty.стат), а не моей копией: на [1..5,100] усечённое 3.0",
        есть_его, с)
    КОД = КОД_CODE2
    try:
        globals()["КОД_CODE2"] = ""
        _КЭШ.pop("H", None)
        пусто = None
        try:
            _стат([1, 2])
        except НетДанных as сбой:
            пусто = str(сбой)
    finally:
        globals()["КОД_CODE2"] = КОД
        _КЭШ.pop("H", None)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: без пути к модулям Code-2 -- ОТКАЗ по имени, а "
        "не своя копия его статистики втихую",
        пусто and "Code-2" in пусто, пусто)

    # ----- 7. ДВА ОТБОРА, И ВТОРОЙ НЕ ИЗ k
    поз = {"sobytij_v_okne": 600, "vhod": [100, 1000, 10],
           "vyhody": {24: [101, 1000, 34], 150: [102, 1000, 160]}, "fee": 0.01}
    chk("ОТБОР ПО ЧИСЛУ СОБЫТИЙ НЕ ЗАВИСИТ ОТ k: он смотрит счётчик событий "
        "архива, а не резервы -- иначе отбирали бы и судили одним и тем же, и "
        "чужой долив ликвидности прошёл бы за заработок",
        otbor_po_sobytijam(поз, 500) is True
        and otbor_po_sobytijam(поз, 800) is False
        and otbor_po_sobytijam({"sobytij_v_okne": None}, 1) is False)
    chk("ОТБОР ПО ОБОРОТУ ВЫВЕДЕН ИЗ k -- и это сказано прямо: на пуле, где k "
        "не вырос, подразумеваемый оборот ноль, и порог выше нуля его не "
        "пропускает",
        otbor_po_oborotu({"vhod": [100, 1000, 10],
                          "vyhody": {24: [100, 1000, 34]}}, 1.0) is False
        and otbor_po_oborotu(поз, 1.0) is True)
    отб = otbit_krug({"stroki": [{"dohod_oborota_med": 0.001,
                                  "rashody_med": 0.042}]}, gorizont=150)
    chk("ОТБИВКА КРУГА СЧИТАЕТСЯ И ДОПУЩЕНИЕ НАЗВАНО: расходы платятся РАЗ, "
        f"доход идёт со временем -- при 0.001 SOL за 33 с и расходах 0.042 "
        f"нужно {отб['nuzhno_sekund']:.0f} с = {отб['nuzhno_minut']:.1f} мин, "
        f"и рядом стоит, что скорость "
        "оборота принята постоянной",
        abs(отб["nuzhno_sekund"] - 33.0 * 0.042 / 0.001) < 1e-6
        and "архив дальше не показывает" in отб["dopushchenije"], отб)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: доход от оборота не положителен -- отбивка НЕ "
        "считается, и сказано почему, а не нарисовано «ноль минут»",
        otbit_krug({"stroki": [{"dohod_oborota_med": 0.0,
                                "rashody_med": 1.0}]},
                   gorizont=150).get("nuzhno_minut") is None)

    # ----- 8. СЧЁТ ПО РЯДУ СДЕЛОК: проверяется рядами с известным ответом
    def сд(действие, q, y, x, t, blok):
        return {"action": действие, "quoteAmount": q, "quoteInPool": y,
                "tokensInPool": x, "poolFeeRate": 0.0, "timestamp": t,
                "block": blok, "pool": "pump-amm"}

    # Ряд, где цена не двигается, а оборот есть: доход обязан быть ровно
    # 20 б.п. * оборот * наша доля, и ни копейкой больше.
    ряд = [сд("buy", 0, 100.0, 1000.0, 0, 1)]
    ряд += [сд("buy", 10.0, 100.0, 1000.0, 1000 * и, 1 + и) for и in range(1, 11)]
    р = lp_po_rjadu(ряд, razmer=1.0, gorizont_sekund=86400)
    доля = 1.0 / 101.0
    ждём_доход = 0.0020 * 100.0 * доля
    chk(f"ДОХОД ПО РЯДУ = 20 б.п. * ОБОРОТ * НАША ДОЛЯ, и ничего больше: "
        f"оборот {р['oborot']:.0f} SOL, доля {доля * 100:.3f} %, доход "
        f"{р['dohod']:.6f} = {ждём_доход:.6f}",
        abs(р["oborot"] - 100.0) < 1e-9
        and abs(р["dohod"] - ждём_доход) < 1e-12, р)
    chk("ОБОРОТ СУММИРУЕТСЯ ИЗ СДЕЛОК, А НЕ ИЗ РОСТА k -- на этом ряду k не "
        "меняется вовсе, и прежний счёт показал бы доход НОЛЬ",
        abs(р["r"] - 1.0) < 1e-12 and р["dohod"] > 0)
    # BOOST в оборот не идёт.
    ряд_б = list(ряд) + [сд("boost_buy_and_burn", 500.0, 100.0, 1000.0,
                            11000, 12)]
    рб = lp_po_rjadu(ряд_б, razmer=1.0, gorizont_sekund=86400)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: ВЫКУП BOOST -- НЕ ОБОРОТ: 500 SOL выкупа оборот "
        f"не изменили ({рб['oborot']:.0f} SOL, как и было), а посчитаны "
        f"отдельно ({рб['boost_objom']:.0f} SOL, {рб['boost_sobytij']:.0f} "
        "событие)",
        abs(рб["oborot"] - р["oborot"]) < 1e-9
        and abs(рб["boost_objom"] - 500.0) < 1e-9
        and рб["boost_sobytij"] == 1, рб)
    # ЧУЖОЙ ВКЛАД РАЗМЫВАЕТ НАШУ ДОЛЮ.
    ряд_л = [сд("buy", 0, 100.0, 1000.0, 0, 1),
             сд("deposit", 0, 300.0, 3000.0, 1000, 2),
             сд("buy", 10.0, 300.0, 3000.0, 2000, 3)]
    рл = lp_po_rjadu(ряд_л, razmer=1.0, gorizont_sekund=86400)
    chk("ЧУЖОЙ ВКЛАД РАЗМЫВАЕТ НАШУ ДОЛЮ, и по ряду это ВИДНО: пул втрое "
        f"глубже -- доля с {рл['dolja_pula_vhod'] * 100:.3f} % до "
        f"{рл['dolja_pula_vyhod'] * 100:.3f} %. По двум снимкам такое "
        "движение неотличимо от оборота",
        рл["likv_sobytij"] == 1
        and рл["dolja_pula_vyhod"] < рл["dolja_pula_vhod"] / 2.5, рл)
    упало4 = None
    try:
        lp_po_rjadu([сд("buy", 0, 100.0, 1000.0, 0, 1)], razmer=1.0,
                    gorizont_sekund=86400)
    except НетДанных as сбой:
        упало4 = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: в ряду одно событие -- отказ по имени, а не "
        "итог из воздуха",
        упало4 and "меньше двух" in упало4, упало4)
    упало5 = None
    try:
        rjady_iz_fajla(КОРЕНЬ / "data" / "idl" / "pump_amm.json")
    except НетДанных as сбой:
        упало5 = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: файл не того формата -- отказ с перечислением "
        "ожидаемых ключей читалки Code-2, а не разбор наугад",
        упало5 and "Своего формата не придумываю" in упало5, упало5)
    chk("ГОРИЗОНТЫ РЯДА -- РОВНО ЗАКАЗАННЫЕ ЧЕТЫРЕ: 10 мин, 1 ч, 6 ч, 24 ч "
        "в секундах, и порог считается по первым 10 минутам",
        ГОРИЗОНТЫ_СЕКУНД == (600, 3600, 21600, 86400)
        and ПОРОГ_ОКНО_СЕКУНД == 600)
    # РЯД НА СУТКИ: сделки на 5-й минуте, 15-й минуте и 2-м часе.
    ряд_сут = [сд("buy", 0, 100.0, 1000.0, 0, 1),
               сд("buy", 10.0, 100.0, 1000.0, 300_000, 2),
               сд("buy", 10.0, 100.0, 1000.0, 900_000, 3),
               сд("buy", 10.0, 100.0, 1000.0, 7_200_000, 4)]
    р10 = lp_po_rjadu(ряд_сут, razmer=1.0, gorizont_sekund=600)
    р1ч = lp_po_rjadu(ряд_сут, razmer=1.0, gorizont_sekund=3600)
    р24 = lp_po_rjadu(ряд_сут, razmer=1.0, gorizont_sekund=86400)
    chk("ГОРИЗОНТ РЕЖЕТ РЯД ПО ВРЕМЕНИ, А НЕ ПО ЧИСЛУ СОБЫТИЙ: сделки на 5-й "
        f"минуте, 15-й и 2-м часе дают оборот {р10['oborot']:.0f} / "
        f"{р1ч['oborot']:.0f} / {р24['oborot']:.0f} SOL на 10 мин / 1 ч / 24 ч",
        abs(р10["oborot"] - 10.0) < 1e-9 and abs(р1ч["oborot"] - 20.0) < 1e-9
        and abs(р24["oborot"] - 30.0) < 1e-9, (р10, р1ч, р24))
    # ----- 9. ВЫХОД ЛИКВИДНОСТИ -- ОТДЕЛЬНОЙ СТРОКОЙ
    ряд_в = [сд("buy", 0, 100.0, 1000.0, 0, 1),
             сд("buy", 5.0, 105.0, 960.0, 60_000, 2),
             сд("withdraw", 0, 52.5, 480.0, 600_000, 3),
             сд("buy", 5.0, 57.5, 440.0, 900_000, 4)]
    рв = lp_po_rjadu(ряд_в, razmer=1.0, gorizont_sekund=86400)
    в = рв["vyhod_likvidnosti"]
    chk("ВЫХОД ЛИКВИДНОСТИ НАЗВАН ТРЕМЯ ЧИСЛАМИ -- сколько раз, КОГДА впервые "
        f"и СКОЛЬКО глубины забрали: {в['sobytij']} раз, через "
        f"{в['sekund_do_pervogo']:.0f} с, забрали {в['dolja_glubiny'] * 100:.0f} % "
        "котировочной стороны",
        в["sobytij"] == 1 and abs(в["sekund_do_pervogo"] - 600) < 1e-6
        and 0.45 < в["dolja_glubiny"] < 0.55 and в["po_dejstviju"] == 1, рв)
    chk("ПОСЛЕ ВЫХОДА ЧУЖОЙ ЛИКВИДНОСТИ НАША ДОЛЯ РАСТЁТ -- пул стал вдвое "
        f"мельче, доля с {рв['dolja_pula_vhod'] * 100:.2f} % до "
        f"{рв['dolja_pula_vyhod'] * 100:.2f} %",
        рв["dolja_pula_vyhod"] > рв["dolja_pula_vhod"] * 1.8, рв)
    # ВТОРОЙ ПУТЬ: в ленте НЕТ действия withdraw вовсе -- только резервы.
    ряд_р = [сд("buy", 0, 100.0, 1000.0, 0, 1),
             сд("buy", 5.0, 105.0, 960.0, 60_000, 2),
             сд("buy", 0.0, 52.5, 480.0, 600_000, 3)]
    рр = lp_po_rjadu(ряд_р, razmer=1.0, gorizont_sekund=86400)
    вр = рр["vyhod_likvidnosti"]
    chk("ВТОРОЙ ПУТЬ: ЕСЛИ В ЛЕНТЕ НЕТ ДЕЙСТВИЯ withdraw ВОВСЕ, вывод виден "
        "по резервам -- оба упали пропорционально, цена стоит. Сделка так "
        "выглядеть не может: она двигает цену и растит k",
        вр["sobytij"] == 1 and вр["po_rezervam"] == 1
        and вр["po_dejstviju"] == 0, рр)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: обычная сделка, двигающая цену, за вывод НЕ "
        "считается -- иначе половина оборота пошла бы в выход ликвидности",
        lp_po_rjadu([сд("buy", 0, 100.0, 1000.0, 0, 1),
                     сд("buy", 20.0, 120.0, 840.0, 60_000, 2)],
                    razmer=1.0,
                    gorizont_sekund=86400)["vyhod_likvidnosti"]["sobytij"] == 0)
    chk("СВОД ПО ВЫБОРКЕ СЧИТАЕТ ДОЛЮ ПУЛОВ С ВЫХОДОМ, А НЕ ЧИСЛО СОБЫТИЙ: "
        "на двух пулах, из которых вывод был у одного, доля 50 %",
        abs(_svod_vyhoda([рв, lp_po_rjadu(ряд, razmer=1.0,
                                          gorizont_sekund=86400)]
                         )["dolja_pulov"] - 0.5) < 1e-9)

    # ----- 10. РАЗРЕЗ НА ПОЛОВИНЫ СУТОК (артефакт -- одни сутки входов)
    т_утро = 1791417600000      # 2026-10-08T00:00:00Z в миллисекундах
    т_вечер = т_утро + 13 * 3600_000
    о_у = sutki_rjada([сд("buy", 0, 100.0, 1000.0, т_утро, 1)])
    о_в = sutki_rjada([сд("buy", 0, 100.0, 1000.0, т_вечер, 1)])
    chk(f"РАЗРЕЗ НА ПОЛОВИНЫ СУТОК: вход в 00Z даёт отрезок {о_у}, вход в 13Z "
        f"-- {о_в}. Иначе у артефакта (48 ч, входы в ОДНИХ сутках 10-08) "
        "вторая половина вышла бы пустой: подбор остался бы без проверки, а "
        "ДИ по суткам не посчитался бы вовсе",
        о_у != о_в and о_у.endswith("/00-11") and о_в.endswith("/12-23"),
        (о_у, о_в))
    chk("при chasov=24 разрез остаётся суточным -- прежнее поведение не "
        f"сломано: {sutki_rjada([сд('buy', 0, 100.0, 1000.0, т_вечер, 1)], chasov=24)}",
        sutki_rjada([сд("buy", 0, 100.0, 1000.0, т_вечер, 1)],
                    chasov=24) == "2026-10-08")
    упало6 = None
    try:
        progon_rjady([], razmery=(1.0,))
    except НетДанных as сбой:
        упало6 = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: рядов нет вовсе -- отказ по имени",
        упало6 and "ни одного ряда" in упало6, упало6)

    # МЕТКИ НЕ ПО ПОРЯДКУ БЛОКОВ: ряд НЕ должен обрезаться.
    ряд_кривой = [сд("buy", 0, 100.0, 1000.0, 0, 1),
                  сд("buy", 10.0, 100.0, 1000.0, 50_000_000, 2),
                  сд("buy", 10.0, 100.0, 1000.0, 300_000, 3)]
    ркр = lp_po_rjadu(ряд_кривой, razmer=1.0, gorizont_sekund=3600)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: метки времени НЕ по порядку блоков -- сделка за "
        "горизонтом НЕ обрезает ряд: оборот 10 SOL (вторая сделка, что в "
        f"горизонте), за горизонтом посчитано {ркр['sobytij_za_gorizontom']}",
        abs(ркр["oborot"] - 10.0) < 1e-9
        and ркр["sobytij_za_gorizontom"] == 1, ркр)
    chk("СОБЫТИЯ ПЕРВЫХ 10 МИНУТ СЧИТАЮТСЯ ОТДЕЛЬНО ОТ ГОРИЗОНТА -- это то, "
        "по чему идёт отбор: на суточном горизонте их всё равно одно",
        р24["sobytij_pervyh_10_min"] == 1
        and р10["sobytij_pervyh_10_min"] == 1, (р24, р10))

    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    for и in упавшие:
        print(f"УПАЛИ: {и}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным")
        плохо += 1
    return плохо


def _net_fee_scheta_v_idl() -> bool:
    """У deposit и withdraw в ЗАКРЕПЛЁННОМ IDL нет счетов комиссии."""
    п = КОРЕНЬ / "data" / "idl" / "pump_amm.json"
    if not п.exists():
        return False
    д = json.loads(п.read_text(encoding="utf-8"))
    for их in д.get("instructions") or []:
        if их["name"] in ("deposit", "withdraw"):
            if any("fee" in а["name"].lower() for а in их.get("accounts") or []):
                return False
    return True


# ------------------------------------------------- командная строка

def progon(kat: Path, *, prefiks: str, sutok: int, razmery: tuple) -> dict:
    """ВЕСЬ РАСЧЁТ: сутки -> горизонты -> размеры -> порог -> площадки."""
    файлы = suточные_файлы(kat, prefiks)[-sutok:]
    if not файлы:
        raise НетДанных(f"суточных файлов {prefiks}_*.json.gz в {kat} нет")
    сутки = [chitat_sutki(п) for п in файлы]
    из_ = {"fajlov": [п.name for п in файлы],
           "sutki": [{к: с[к] for к in ("den", "okno_slotov", "porog_sol",
                                        "chasov", "fajlov", "strok",
                                        "oshibok_chasov", "signalov_vsego")}
                     | {"pozicij_pump_amm_wsol": len(с["pozicii"])}
                     for с in сутки],
           "gorizonty": gorizonty_est(),
           "ploshchadki": drugije_ploshchadki(сутки),
           "po_gorizontam": {}, "porog": {}}
    for X in razmery:
        for h in из_["gorizonty"]["est_slotov"]:
            по_суткам = [posчитать_sutki(с, razmer=X, gorizont=h) for с in сутки]
            все = [и for с in по_суткам for и in с["itogi"]]
            против = [и for с in по_суткам for и in с["protiv_derzhat"]]
            ср_по_суткам = [(statistics.fmean(с["itogi"]) if с["itogi"] else None)
                            for с in по_суткам]
            из_["po_gorizontam"][f"{X:g}SOL|{h}"] = {
                "razmer": X, "gorizont_slotov": h,
                "gorizont_sekund": round(h * СЕКУНД_НА_СЛОТ, 1),
                "pozicij": len(все), "stat_itog": _стат(все),
                "stat_protiv_derzhat": _стат(против),
                "v_pljuse": (sum(1 for и in все if и > 0) / len(все)
                             if все else None),
                "dolja_pula_mediana": statistics.median(
                    [с["dolja_pula_mediana"] for с in по_суткам
                     if с["dolja_pula_mediana"] is not None] or [0]),
                "oborot_mediana": statistics.median(
                    [с["oborot_mediana"] for с in по_суткам
                     if с["oborot_mediana"] is not None] or [0]),
                "di_po_sutkam": di_po_sutkam(ср_по_суткам),
                "po_sutkam": [{"den": с["den"], "pozicij": с["pozicij"],
                               "sr": м, "v_pljuse": с["v_pljuse"]}
                              for с, м in zip(по_суткам, ср_по_суткам,
                                              strict=False)]}
    # П.2: ПОРОГ. Подбор на первых 4 сутках, проверка на остальных 3.
    делёж = 4
    for X in razmery:
        под = porog_podobrat(сутки[:делёж], razmer=X)
        N = (под["luchshij"] or {}).get("N")
        из_["porog"][f"{X:g}SOL"] = {
            "kalibr_sutok": делёж, "proverka_sutok": len(сутки) - делёж,
            "podbor": под,
            "proverka": (porog_proverit(сутки[делёж:], N=N, razmer=X)
                         if N is not None else None),
            "bez_poroga_na_proverke": porog_proverit(сутки[делёж:], N=0.0,
                                                     razmer=X)}
    return из_


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--arhiv", default=None,
                   help="каталог с суточными файлами Code-2 (konv_*.json.gz)")
    p.add_argument("--prefiks", default="konv")
    p.add_argument("--sutok", type=int, default=7)
    p.add_argument("--rjady", default=None,
                   help="файл(ы) читалки Code-2 с рядами сделок пулов на 24 ч, "
                        "через запятую: тогда считаются горизонты 10 мин/1 ч/"
                        "6 ч/24 ч, а не окно суточного файла")
    p.add_argument("--chasov-otrezka", type=int,
                   default=ЧАСОВ_В_ПОЛОВИНЕ,
                   help="единица разреза для подбора и проверки, часов "
                        f"(по умолчанию {ЧАСОВ_В_ПОЛОВИНЕ} -- половина суток: "
                        "входы артефакта лежат в одних сутках)")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return 1 if self_test() else 0
    if a.rjady:
        итог = progon_rjady([п for п in a.rjady.split(",") if п],
                            razmery=РАЗМЕРЫ,
                            chasov_otrezka=a.chasov_otrezka)
        print(json.dumps(итог, ensure_ascii=False, indent=1, default=str))
        return 0
    if not a.arhiv:
        p.print_help()
        print("\nНУЖЕН --arhiv: суточные файлы лежат на ветке claude/podbivka "
              "(data/podbivka/arhiv_den). Свою читалку архива здесь не пишу -- "
              "берутся ГОТОВЫЕ файлы Code-2 и его же модули (C3_LP_KOD_CODE2).")
        return 1
    итог = progon(Path(a.arhiv), prefiks=a.prefiks, sutok=a.sutok,
                  razmery=РАЗМЕРЫ)
    print(json.dumps(итог, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

