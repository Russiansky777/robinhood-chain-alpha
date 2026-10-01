#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USDC-нога: РАЗМЕР пакета против 1232 и CU против 250 000 -- таблицей по типам.

ЗАЧЕМ (утренний пакет владельца 01.10, п.2б): "в тени -- размер транзакции против
1232 и CU по simulateTransaction против 250 000 (BLOOM_LANE_CU_TWO_STEP), таблицей
по CLMM / DLMM / Whirlpool / DAMM v2. Влезает и CU хватает -- 0.01 на первом
USDC-сигнале; не влезает -- число и стоп, не чинить втихую."

ПОЧЕМУ ОТДЕЛЬНЫМ ПРОГОНОМ, А НЕ ВНУТРИ СЛУЖБЫ. Размер пакета знает только
СОБРАННАЯ транзакция, а тень её не собирает вовсе (решений она не принимает, и это
её собственная проверка). Поэтому замер идёт своим процессом на хосте: он ставит
BLOOM_USDC_NOGA=boj ТОЛЬКО СЕБЕ, собирает транзакцию по ЖИВЫМ сделкам источников
и НИЧЕГО НЕ ОТПРАВЛЯЕТ -- ни одной подписи в сеть. Служба при этом остаётся в
тени, и её env не трогается.

ЧТО СЧИТАЕТСЯ ЖИВЫМ. Сделки берутся из образцов Code-3
(data/c3_usdc_noga/obrazcy_usdc_noga.json) -- это снимки НАСТОЯЩИХ свопов с
котировкой USDC по типам пулов, с подписями. Синтетики здесь нет.

CU СПРАШИВАЕТСЯ У ЦЕПИ, А НЕ СЧИТАЕТСЯ. simulateTransaction с
replaceRecentBlockhash и sigVerify=false: число берётся из unitsConsumed. Нет
ответа -- так и говорим, а не подставляем предел.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
# СВОЙ КАТАЛОГ -- ПЕРВЫМ. На хосте рядом со скриптом лежат доставленные модули, и
# они обязаны перебить копии службы: иначе замер пойдёт по старому коду.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(КОРЕНЬ / "analysis"))

ПРЕДЕЛ_ПАКЕТА = 1232
ФАЙЛ_ALT = "usdc_noga_alt.json"
# ВТОРОЙ ПОРОГ CU -- ТОТ, КОТОРЫЙ НАЗВАЛ ВЛАДЕЛЕЦ (утренний пакет 01.10, п.2б:
# "CU против 250 000"). В КОДЕ ПОЛОСЫ ПРЕДЕЛ ДРУГОЙ: BLOOM_LANE_CU_TWO_STEP по
# умолчанию 800 000 с 28.09 ("лимит больше не стоит денег, а падение по лимиту
# стоит сделку"). Поэтому считаются ОБА: по живому пределу полосы -- что будет в
# бою, по 250 000 -- ответ на вопрос владельца. Подменить одно другим значило бы
# ответить не на тот вопрос.
ПОРОГ_CU_СЛОВО_ВЛАДЕЛЬЦА = 250_000


def наша_таблица(каталог: str | None) -> dict:
    """Адрес НАШЕЙ таблицы адресов из файла состояния службы."""
    из_ = {"ok": False, "адрес": None, "адресов": None, "why_not": None}
    if not каталог:
        из_["why_not"] = "каталог состояния не задан"
        return из_
    п = Path(каталог) / ФАЙЛ_ALT
    if not п.exists():
        из_["why_not"] = f"файла таблицы нет: {п}"
        return из_
    try:
        д = json.loads(п.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"файл таблицы не разобран: {type(exc).__name__}"
        return из_
    а = д.get("адрес")
    if not а:
        из_["why_not"] = "в файле таблицы нет адреса"
        return из_
    из_.update(ok=True, адрес=а, адресов=д.get("адресов"))
    return из_


def образцы(путь: str | None, *, UN) -> dict:
    """Живые образцы: из НАЗВАННОГО файла плюс DAMM v2 из образцов пулов службы.

    ПУТЬ НУЖЕН ЯВНО. `c2_usdc_noga._obrazcy` ищет файл в data РЯДОМ С КОДОМ
    СЛУЖБЫ (c2_common.DATA = ../data от модуля), а на хосте это /home/bot/data --
    то есть каталог службы, а не прогона. Из-за этого первый прогон разобрал НОЛЬ
    сделок и честно сказал «не знаю»: файла там просто не было. Писать образцы в
    каталог службы ради замера нельзя, поэтому путь передаётся входом.

    DAMM v2 всё равно берётся через модуль: его ряды лежат в образцах пулов самой
    службы (data/c2_pool_samples), и дублировать их в прогон незачем.
    """
    из_ = {"ok": False, "why_not": None, "ряды": [], "из_файла": 0,
            "damm2_из_службы": 0, "почему_службы": None}
    ряды = []
    if путь:
        п = Path(путь)
        if not п.exists():
            из_["why_not"] = f"файла образцов нет: {п}"
            return из_
        try:
            д = json.loads(п.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = f"файл образцов не разобран: {type(exc).__name__}"
            return из_
        ряды = [dict(р) for р in (д.get("ряды") or [])]
        из_["из_файла"] = len(ряды)
    сл = {}
    try:
        сл = UN._obrazcy()
    except Exception as exc:  # noqa: BLE001
        из_["почему_службы"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    if isinstance(сл, dict):
        из_["почему_службы"] = из_["почему_службы"] or сл.get("why_not")
        свои = {(р.get("сделка") or "") + "|" + (р.get("пул") or "") for р in ряды}
        for р in (сл.get("ryady") or []):
            ключ = (р.get("сделка") or "") + "|" + (р.get("пул") or "")
            if ключ in свои:
                continue
            ряды.append(dict(р))
            if р.get("tip") == "DAMM v2":
                из_["damm2_из_службы"] += 1
    if not ряды:
        из_["why_not"] = (из_["why_not"] or из_["почему_службы"]
                           or "живых образцов не нашлось ни в файле, ни у службы")
        return из_
    из_.update(ok=True, ряды=ряды)
    return из_


def cu_по_цепи(rpc_call, tx_base64: str) -> dict:
    """CU у ЦЕПИ: simulateTransaction. Нет ответа -- так и говорим."""
    из_ = {"ok": False, "cu": None, "why_not": None, "ошибка_симуляции": None}
    try:
        о = rpc_call("simulateTransaction",
                      [tx_base64, {"encoding": "base64", "sigVerify": False,
                                    "replaceRecentBlockhash": True,
                                    "commitment": "confirmed"}])
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"симуляция не ответила: {type(exc).__name__}: {str(exc)[:120]}"
        return из_
    зн = (о or {}).get("value") or {}
    из_["ошибка_симуляции"] = зн.get("err")
    ед = зн.get("unitsConsumed")
    if not isinstance(ед, int):
        из_["why_not"] = (f"узел не назвал unitsConsumed"
                           + (f"; ошибка симуляции {зн.get('err')}"
                               if зн.get("err") else ""))
        return из_
    из_.update(ok=True, cu=int(ед))
    return из_


def один(ряд: dict, *, UN, кэш_ног, наш: str, предел_cu: int, таблица: str | None,
          rpc_call, проскальзывание: float) -> dict:
    """Собрать USDC-ногу по одной живой сделке и измерить размер и CU."""
    из_ = {"тип": ряд.get("tip"), "сделка": ряд.get("сделка"),
            "пул": ряд.get("пул"), "ok": False, "why_not": None,
            "размер": None, "влез": None, "cu": None, "cu_хватило": None,
            "таблиц": None, "наша_таблица_взята": None,
            "leg1_min_out": None, "leg2_amount_in": None, "min_out": None}
    tx = ряд.get("транзакция")
    if not isinstance(tx, dict):
        из_["why_not"] = "в образце нет транзакции"
        return из_
    ст = UN.storona(tx, programma=ряд.get("program"), pul=ряд.get("пул"))
    if not ст.get("ok"):
        из_["why_not"] = f"сторона не выведена: {ст.get('why_not')}"
        return из_
    минт = ст.get("base_mint")
    из_["минт"] = минт
    с = UN.sobrat(tx_istochnika=tx, istochnik=ряд.get("пул") or "",
                   mint=минт or "", nash_koshelek=наш,
                   lamporty=10_000_000, kesh_nog=кэш_ног,
                   proskalzyvanie=проскальзывание, cu_units=int(предел_cu),
                   rpc_call=rpc_call,
                   nashi_tablicy=([таблица] if таблица else None))
    с = с if isinstance(с, dict) else {}
    из_.update(таблиц=с.get("lut_tables"),
                наша_таблица_взята=bool(с.get("nashi_tablicy_vzjaty")),
                leg1_min_out=с.get("leg1_min_out"),
                leg2_amount_in=с.get("leg2_amount_in"),
                min_out=с.get("min_out"))
    if not с.get("ok") or not с.get("tx_base64"):
        из_["why_not"] = с.get("why_not") or "сборка не дала транзакции"
        return из_
    из_["размер"] = с.get("size")
    из_["влез"] = bool(isinstance(из_["размер"], int)
                        and из_["размер"] <= ПРЕДЕЛ_ПАКЕТА)
    ц = cu_по_цепи(rpc_call, с["tx_base64"])
    из_["cu"] = ц.get("cu")
    из_["cu_почему_нет"] = ц.get("why_not")
    из_["ошибка_симуляции"] = ц.get("ошибка_симуляции")
    из_["cu_хватило"] = (None if ц.get("cu") is None
                          else bool(ц["cu"] <= int(предел_cu)))
    из_["ok"] = True
    return из_


def свод(строки: list, *, предел_cu: int,
          порог_владельца: int = ПОРОГ_CU_СЛОВО_ВЛАДЕЛЬЦА) -> dict:
    """Таблица по типам: размер против 1232 и CU против предела."""
    по_типам: dict = {}
    for с in строки:
        т = по_типам.setdefault(с.get("тип") or "?", {
            "сделок": 0, "собралось": 0, "отказов": 0, "влезло": 0,
            "не_влезло": 0, "размеры": [], "cu": [], "cu_хватило": 0,
            "cu_не_хватило": 0, "cu_не_известен": 0, "причины": {}})
        т["сделок"] += 1
        if not с.get("ok"):
            т["отказов"] += 1
            п = str(с.get("why_not") or "без причины")[:90]
            т["причины"][п] = т["причины"].get(п, 0) + 1
            continue
        т["собралось"] += 1
        if isinstance(с.get("размер"), int):
            т["размеры"].append(с["размер"])
            т["влезло" if с.get("влез") else "не_влезло"] += 1
        if isinstance(с.get("cu"), int):
            т["cu"].append(с["cu"])
            т["cu_хватило" if с.get("cu_хватило") else "cu_не_хватило"] += 1
        else:
            т["cu_не_известен"] += 1
    for т in по_типам.values():
        р = т.pop("размеры")
        c = т.pop("cu")
        т["размер_мин"] = min(р) if р else None
        т["размер_медиана"] = int(statistics.median(р)) if р else None
        т["размер_макс"] = max(р) if р else None
        т["cu_мин"] = min(c) if c else None
        т["cu_медиана"] = int(statistics.median(c)) if c else None
        т["cu_макс"] = max(c) if c else None
    # ПО ПОРОГУ ВЛАДЕЛЬЦА -- ОТДЕЛЬНЫЙ СЧЁТ, по тем же живым числам CU.
    по_владельцу = {"хватило": 0, "не_хватило": 0, "не_известен": 0}
    for с in строки:
        if not с.get("ok"):
            continue
        cu = с.get("cu")
        if not isinstance(cu, int):
            по_владельцу["не_известен"] += 1
        else:
            по_владельцу["хватило" if cu <= int(порог_владельца)
                          else "не_хватило"] += 1
    всего_влезло = sum(т["влезло"] for т in по_типам.values())
    всего_нет = sum(т["не_влезло"] for т in по_типам.values())
    cu_нет = sum(т["cu_не_хватило"] for т in по_типам.values())
    cu_неизв = sum(т["cu_не_известен"] for т in по_типам.values())
    # ВЕРДИКТ ОДНИМ СЛОВОМ, И НЕЗНАНИЕ -- НЕ "ДА". Нет CU хоть у одной сделки --
    # ответ "не знаю", а не "хватает": включать боевой режим по незнанию нельзя.
    if всего_нет:
        вердикт = f"НЕ ВЛЕЗАЕТ: {всего_нет} сделок больше {ПРЕДЕЛ_ПАКЕТА} байт"
    elif not всего_влезло:
        вердикт = "НЕ ЗНАЮ: ни одна сделка не собралась"
    elif cu_нет:
        вердикт = f"CU НЕ ХВАТАЕТ: {cu_нет} сделок выше предела {предел_cu}"
    elif cu_неизв:
        вердикт = f"НЕ ЗНАЮ: CU не известен у {cu_неизв} сделок"
    else:
        вердикт = (f"ВЛЕЗАЕТ И CU ХВАТАЕТ: {всего_влезло} сделок, "
                    f"предел пакета {ПРЕДЕЛ_ПАКЕТА}, предел CU {предел_cu}")
    return {"по_типам": по_типам, "предел_пакета": ПРЕДЕЛ_ПАКЕТА,
             "предел_cu": предел_cu, "влезло": всего_влезло,
             "не_влезло": всего_нет, "cu_не_хватило": cu_нет,
             "cu_не_известен": cu_неизв, "вердикт": вердикт,
             "порог_cu_слово_владельца": int(порог_владельца),
             "по_порогу_владельца": по_владельцу}


def self_test() -> int:
    всего = сбоев = 0

    def chk(что, ок, факт=None):
        nonlocal всего, сбоев
        всего += 1
        сбоев += (not ок)
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {что}"
              + (f" -> {факт!r}" if факт is not None and not ок else ""))

    chk("предел пакета -- 1232, как в сети", ПРЕДЕЛ_ПАКЕТА == 1232)
    # ИМЕНА, КОТОРЫМИ ПОЛЬЗУЕТСЯ main(), -- ПРОВЕРЯЮТСЯ ЗДЕСЬ. Два прогона уже
    # упали на хосте именно на именах (bloom_api.Helius, c2_swap_build.LegCache):
    # проверка без сети ловит это до любого прогона.
    try:
        import bloom_nogi_shablony as NSп  # noqa: PLC0415
        import bloom_own_send as OSп  # noqa: PLC0415
        import c2_shadow_build as SHп  # noqa: PLC0415
        import solana_rpc_client as RPCп  # noqa: PLC0415

        chk("кэш ног -- c2_shadow_build.LegCache, и у него есть get, luts, load_luts",
            hasattr(SHп, "LegCache")
            and all(hasattr(SHп.LegCache, и) for и in ("get", "load_luts")),
            [и for и in dir(SHп) if "Cache" in и])
        ст_п, _п = NSп.загрузить(путь=str(КОРЕНЬ / "data" / "nogi_shablony.json"))
        chk("статичные записи ноги умеют вливаться в кэш (метод влить)",
            callable(getattr(ст_п, "влить", None))
            and callable(getattr(ст_п, "обновить", None)))
        chk("клиент узла -- solana_rpc_client.SolanaRpc с методом call",
            hasattr(RPCп, "SolanaRpc") and callable(
                getattr(RPCп.SolanaRpc, "call", None)))
        chk("предел CU и кошелёк полосы спрашиваются у bloom_own_send",
            callable(getattr(OSп, "предел_cu", None))
            and callable(getattr(OSп, "кошелёк_полосы", None)))
    except Exception as exc:  # noqa: BLE001
        chk("модули, которыми пользуется замер, загружаются", False,
            f"{type(exc).__name__}: {str(exc)[:140]}")
    # ВЕРДИКТ: НЕЗНАНИЕ НЕ ПРЕВРАЩАЕТСЯ В "ДА" -- это и есть защита от тихой зелени.
    с1 = свод([{"тип": "CLMM", "ok": True, "размер": 1100, "влез": True,
                 "cu": 120_000, "cu_хватило": True}], предел_cu=250_000)
    chk("влезло и CU хватает -- вердикт говорит это словами",
        с1["вердикт"].startswith("ВЛЕЗАЕТ И CU ХВАТАЕТ") and с1["влезло"] == 1, с1)
    с2 = свод([{"тип": "CLMM", "ok": True, "размер": 1300, "влез": False,
                 "cu": 120_000, "cu_хватило": True}], предел_cu=250_000)
    chk("не влезло -- вердикт НЕ ВЛЕЗАЕТ, и число названо",
        с2["вердикт"].startswith("НЕ ВЛЕЗАЕТ") and с2["не_влезло"] == 1, с2)
    с3 = свод([{"тип": "DLMM", "ok": True, "размер": 1100, "влез": True,
                 "cu": None, "cu_хватило": None}], предел_cu=250_000)
    chk("CU не известен -- вердикт НЕ ЗНАЮ, а не «хватает»",
        с3["вердикт"].startswith("НЕ ЗНАЮ") and с3["cu_не_известен"] == 1, с3)
    с4 = свод([{"тип": "DLMM", "ok": True, "размер": 1100, "влез": True,
                 "cu": 300_000, "cu_хватило": False}], предел_cu=250_000)
    chk("CU выше предела -- вердикт CU НЕ ХВАТАЕТ",
        с4["вердикт"].startswith("CU НЕ ХВАТАЕТ"), с4)
    # ДВА ПОРОГА СРАЗУ: живой предел полосы и 250 000 слова владельца. 300 000
    # проходит по 800 000 и НЕ проходит по 250 000 -- оба числа названы.
    с4б = свод([{"тип": "DLMM", "ok": True, "размер": 1100, "влез": True,
                  "cu": 300_000, "cu_хватило": True}], предел_cu=800_000)
    chk("CU 300 000: по живому пределу 800 000 хватает, по 250 000 владельца -- нет",
        с4б["вердикт"].startswith("ВЛЕЗАЕТ И CU ХВАТАЕТ")
        and с4б["по_порогу_владельца"]["не_хватило"] == 1
        and с4б["порог_cu_слово_владельца"] == 250_000, с4б)
    с5 = свод([{"тип": "DBC", "ok": False, "why_not": "своп источника в другую сторону"}],
               предел_cu=250_000)
    chk("ни одна не собралась -- вердикт НЕ ЗНАЮ, причина отказа посчитана",
        с5["вердикт"].startswith("НЕ ЗНАЮ")
        and с5["по_типам"]["DBC"]["отказов"] == 1
        and с5["по_типам"]["DBC"]["причины"], с5)
    с6 = свод([{"тип": "CLMM", "ok": True, "размер": 1000, "влез": True,
                 "cu": 100_000, "cu_хватило": True},
                {"тип": "CLMM", "ok": True, "размер": 1200, "влез": True,
                 "cu": 200_000, "cu_хватило": True}], предел_cu=250_000)
    т = с6["по_типам"]["CLMM"]
    chk("по типу считаются мин, медиана и макс -- и размера, и CU",
        (т["размер_мин"], т["размер_медиана"], т["размер_макс"]) == (1000, 1100, 1200)
        and (т["cu_мин"], т["cu_макс"]) == (100_000, 200_000), т)
    # CU БЕРЁТСЯ ИЗ ОТВЕТА ЦЕПИ, А НЕ ИЗ ПРЕДЕЛА.
    ц = cu_по_цепи(lambda *_а: {"value": {"unitsConsumed": 137_000, "err": None}}, "X")
    chk("CU читается из unitsConsumed", ц["ok"] and ц["cu"] == 137_000, ц)
    ц2 = cu_по_цепи(lambda *_а: {"value": {"err": "InstructionError"}}, "X")
    chk("нет unitsConsumed -- отказ словами и ошибка симуляции названа",
        ц2["ok"] is False and ц2["cu"] is None
        and "InstructionError" in (ц2["why_not"] or ""), ц2)
    чт = наша_таблица("")
    chk("без каталога -- отказ словами, а не пустая таблица",
        чт["ok"] is False and чт["why_not"], чт)
    # ЖИВЫЕ ОБРАЗЦЫ НА МЕСТЕ И С ПОДПИСЯМИ.
    try:
        import c2_usdc_noga as UN  # noqa: PLC0415

        о = UN._obrazcy()
        ряды = о.get("ryady") or []
        # ПОДПИСЬ ЕСТЬ У СДЕЛОК ИСТОЧНИКОВ; у образцов DAMM v2 её нет вовсе --
        # они снимки ПУЛА, а не чужой сделки, и это названо полем "источник".
        # Требовать подпись от всех значило бы объявить живые образцы кривыми.
        со_сделкой = [р for р in ряды if not р.get("источник")]
        из_пула = [р for р in ряды if р.get("источник")]
        chk(f"живых образцов {len(ряды)}: сделок источников {len(со_сделкой)} "
            f"(у всех подпись) и снимков пула {len(из_пула)} (у них адрес пула)",
            о.get("ok") and len(ряды) >= 30 and len(со_сделкой) >= 30
            and all(len(str(р.get('сделка') or '')) > 40 for р in со_сделкой)
            and all(р.get("пул") for р in из_пула),
            (о.get("why_not"), len(ряды), len(со_сделкой), len(из_пула)))
        об_ф = образцы(str(КОРЕНЬ / "data" / "c3_usdc_noga"
                            / "obrazcy_usdc_noga.json"), UN=UN)
        chk(f"образцы читаются из НАЗВАННОГО файла: {об_ф.get('из_файла')} рядов",
            об_ф["ok"] and об_ф["из_файла"] >= 30, об_ф)
        об_н = образцы(str(КОРЕНЬ / "нет-такого.json"), UN=UN)
        chk("названного файла нет -- отказ словами, а не тихий ноль рядов",
            об_н["ok"] is False and "нет" in (об_н["why_not"] or ""), об_н)
        chk("у сборки USDC-ноги есть вход для НАШЕЙ таблицы адресов",
            "nashi_tablicy" in UN.sobrat.__code__.co_varnames,
            UN.sobrat.__code__.co_varnames)
    except Exception as exc:  # noqa: BLE001
        chk("модуль USDC-ноги и его образцы читаются", False,
            f"{type(exc).__name__}: {str(exc)[:120]}")
    print(f"самопроверка замера размера и CU: {всего - сбоев}/{всего} пройдено")
    return 1 if сбоев else 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--state-dir", default="")
    р.add_argument("--koshelek", default="")
    р.add_argument("--predel-cu", type=int, default=0,
                    help="предел CU (0 -- взять из BLOOM_LANE_CU_TWO_STEP полосы)")
    р.add_argument("--tipov", default="", help="только эти типы, через запятую")
    р.add_argument("--predel", type=int, default=0, help="не больше N сделок")
    р.add_argument("--obrazcy", default="",
                    help="путь к obrazcy_usdc_noga.json (иначе только то, что "
                          "найдёт сама служба в своём каталоге data)")
    р.add_argument("--out", default="")
    а = р.parse_args()
    if а.self_test:
        return self_test()
    # БОЕВОЙ РЕЖИМ -- ТОЛЬКО СЕБЕ, в своём процессе. env службы не трогается, и
    # ни одна подпись в сеть не уходит: собираем и симулируем, не отправляем.
    os.environ["BLOOM_USDC_NOGA"] = "boj"
    os.environ.pop("BLOOM_USDC_NOGA_GROUPS", None)
    import bloom_nogi_shablony as NS  # noqa: PLC0415
    import bloom_own_send as OS  # noqa: PLC0415
    # КЭШ НОГ ЖИВЁТ В c2_shadow_build, а не в c2_swap_build: перепутал -- и прогон
    # упал на AttributeError уже на хосте (06:42Z). Имена, которыми пользуется
    # main(), теперь проверяются самопроверкой.
    import c2_shadow_build as SH  # noqa: PLC0415
    import c2_usdc_noga as UN  # noqa: PLC0415
    import solana_rpc_client as RPC  # noqa: PLC0415

    наш = а.koshelek or OS.кошелёк_полосы()
    предел_cu = а.predel_cu or OS.предел_cu("two_step")
    клиент = RPC.SolanaRpc(service="usdc_noga_razmer_cu")
    ответ: dict = {"кошелёк": наш, "предел_cu": предел_cu,
                    "предел_пакета": ПРЕДЕЛ_ПАКЕТА}
    # ОТКУДА ВЗЯЛСЯ МОДУЛЬ -- В ОТЧЁТ. Прогон 06:47Z упал на том, что sobrat() не
    # знал входа nashi_tablicy: взялась копия с хоста, а не доставленная. Молча
    # измерить не тем модулем -- значит измерить не то, что поедет в бой.
    ответ["модуль_usdc_nogi"] = getattr(UN, "__file__", None)
    ответ["вход_nashi_tablicy_есть"] = (
        "nashi_tablicy" in UN.sobrat.__code__.co_varnames)
    if not ответ["вход_nashi_tablicy_есть"]:
        print(f"ОТКАЗ: модуль USDC-ноги взят из {ответ['модуль_usdc_nogi']} -- у его "
              "sobrat() нет входа nashi_tablicy, то есть НАША таблица в сборку не "
              "попадёт, и замер сказал бы «не влезает» по своей же причине")
        if а.out:
            Path(а.out).write_text(json.dumps(ответ, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        return 2
    чт = наша_таблица(а.state_dir)
    ответ["наша_таблица"] = чт
    if not чт.get("ok"):
        print(f"ОТКАЗ: нашей таблицы адресов нет -- {чт.get('why_not')}")
        print("без неё замер сказал бы «не влезает» по причине, которую мы сами "
              "и создали; сначала создать таблицу (run_vps_usdc_noga_alt_nl.yml)")
        if а.out:
            Path(а.out).write_text(json.dumps(ответ, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        return 2
    # КЭШ НОГ -- ЖИВОЙ, С ЦЕНОЙ ПО ОСТАТКАМ: без цены сборка откажет, и замер
    # сказал бы «не влезает» там, где дело в отсутствии котировки.
    статичные, почему_ф = NS.загрузить()
    ответ["кэш_ног_почему"] = почему_ф
    try:
        обн = статичные.обновить(клиент.call)
        ответ["кэш_ног_обновление"] = обн if isinstance(обн, dict) else str(обн)
    except Exception as exc:  # noqa: BLE001
        ответ["кэш_ног_обновление"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    # КЭШ ТОТ ЖЕ, ЧТО У СЛУЖБЫ: LegCache плюс влив статичных записей ноги. Своим
    # словарём тут не обойтись -- сборка спрашивает у кэша get(q), luts и
    # load_luts, и подделка дала бы "влезает" там, где в бою таблиц не нашлось.
    кэш = SH.LegCache({}, клиент.call, allow_polling=True)
    ответ["влито_записей_ноги"] = статичные.влить(кэш)
    об = образцы(а.obrazcy or None, UN=UN)
    ответ["образцы"] = {к: об.get(к) for к in
                         ("ok", "why_not", "из_файла", "damm2_из_службы",
                          "почему_службы")}
    ряды = об.get("ряды") or []
    if not ряды:
        print(f"ОТКАЗ: живых образцов нет -- {об.get('why_not')}")
        if а.out:
            Path(а.out).write_text(json.dumps(ответ, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        return 2
    только = {т.strip() for т in (а.tipov or "").split(",") if т.strip()}
    if только:
        ряды = [р for р in ряды if р.get("tip") in только]
    if а.predel:
        ряды = ряды[:а.predel]
    строки = []
    for ряд in ряды:
        try:
            строки.append(один(ряд, UN=UN, кэш_ног=кэш, наш=наш,
                                предел_cu=предел_cu, таблица=чт["адрес"],
                                rpc_call=клиент.call, проскальзывание=0.35))
        except Exception as exc:  # noqa: BLE001
            строки.append({"тип": ряд.get("tip"), "сделка": ряд.get("сделка"),
                            "ok": False,
                            "why_not": f"упало: {type(exc).__name__}: {str(exc)[:140]}"})
    ответ["строки"] = строки
    ответ["свод"] = свод(строки, предел_cu=предел_cu)
    print(f"кошелёк {наш} · наша таблица {чт['адрес']} ({чт.get('адресов')} адресов)")
    print(f"предел пакета {ПРЕДЕЛ_ПАКЕТА} · предел CU {предел_cu}")
    print()
    for тип, т in sorted(ответ["свод"]["по_типам"].items()):
        print(f"{тип}: сделок {т['сделок']} · собралось {т['собралось']} · "
              f"отказов {т['отказов']}")
        print(f"   размер мин/медиана/макс {т['размер_мин']}/"
              f"{т['размер_медиана']}/{т['размер_макс']} · влезло {т['влезло']} · "
              f"НЕ влезло {т['не_влезло']}")
        print(f"   CU мин/медиана/макс {т['cu_мин']}/{т['cu_медиана']}/"
              f"{т['cu_макс']} · хватило {т['cu_хватило']} · не хватило "
              f"{т['cu_не_хватило']} · не известен {т['cu_не_известен']}")
        for п, н in sorted(т["причины"].items(), key=lambda x: -x[1]):
            print(f"   отказ x{н}: {п}")
    print()
    пв = ответ["свод"]["по_порогу_владельца"]
    print(f"по порогу владельца {ответ['свод']['порог_cu_слово_владельца']} CU: "
          f"хватило {пв['хватило']} · не хватило {пв['не_хватило']} · "
          f"не известен {пв['не_известен']}")
    print(f"ВЕРДИКТ (по живому пределу полосы {предел_cu}): "
          f"{ответ['свод']['вердикт']}")
    if а.out:
        Path(а.out).write_text(json.dumps(ответ, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
