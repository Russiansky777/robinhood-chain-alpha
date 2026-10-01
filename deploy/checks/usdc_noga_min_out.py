#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ЧИСЛО ДЛЯ ШТАБА: MIN_OUT ВТОРОЙ НОГИ -- боевой котировщик против цены события.

ЗАЧЕМ ЭТОТ ЗАМЕР. Тень USDC-ноги считает min_out по ЦЕНЕ СОБЫТИЯ ИСТОЧНИКА: его
выход, делённый на его вход. Это СРЕДНЯЯ цена ЕГО сделки, а наша покупка идёт из
состояния ПОСЛЕ неё -- то есть по худшей цене. Значит цена события выход
ЗАВЫШАЕТ, и min_out от неё -- это оплаченные чаевые и приоритет за откат. Слово
владельца 01.10: в бою min_out берётся у котировщика типа, с учётом нашего объёма.
Здесь мерится, насколько они расходятся и где цена события дала бы откат.

ЧТО СЧИТАЕТСЯ -- ПО КАЖДОМУ ТИПУ, и ни одно число не выводится из другого:
  1. ФАКТ ПО ЦЕПИ -- сколько USDC источник отдал и сколько токена получил
     (движение хранилищ в его же сделке). Это не модель, это цепь.
  2. ЦЕНА СОБЫТИЯ на НАШЕМ размере -- ожидание и min_out тени.
  3. КОТИРОВЩИК ТИПА на НАШЕМ размере -- ожидание и min_out боя:
       CLMM      c2_swap_build.min_out_from_reserves -> clmm_min_out: L и цена
                 ПОСЛЕ сделки из события SwapEvent в логах, ставка комиссии из
                 data/clmm_konfigi.json. Нуль чтений -- то же число, которое
                 подготовить() выдаёт с чтениями, но на состоянии той же сделки.
       DAMM v2   min_out_from_reserves -> damm2_min_out: событие и резервы после.
       DLMM      c2_dlmm_bez_chteniy.котировка: нуль чтений (слово владельца).
       Whirlpool котировщика без чтений нет -- в этом замере по нему отказ по
                 имени, а не выведенное из соседей число.
  4. ЗАВЫШЕНИЕ = ожидание по цене события / ожидание котировщика - 1.
  5. ОТКАТ -- сколько сигналов, где min_out ПО ЦЕНЕ СОБЫТИЯ выше ожидания
     котировщика: столько покупок полоса отправила бы на промах. Считается на
     нескольких проскальзываниях, потому что запас группы и есть то, чем
     завышение закрывается.

НАШ РАЗМЕР берётся так же, как в бою: билет SOL -> USDC по цене статичного шаблона
первой ноги (data/nogi_shablony.json) с запасом первого шага. Своей цены USDC
замер не заводит.

Запуск: python3 deploy/checks/usdc_noga_min_out.py [--obrazcy <путь>] [--sol 0.01]
        [--proskalzyvaniya 0.05,0.10,0.35] [--out <файл>]
Выход 2 -- файла образцов нет (молчаливый пропуск считается провалом).
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[2]
ПО_УМОЛЧАНИЮ = КОРЕНЬ / "data" / "podbivka" / "usdc_noga_dlya_code3.json"
ЛАМПОРТОВ_В_SOL = 1_000_000_000


def _модули(путь: str | None = None):
    sys.path.insert(0, str(Path(путь) if путь else КОРЕНЬ / "analysis"))
    import bloom_nogi_shablony as NSH  # noqa: PLC0415
    import c2_common as C  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    import c2_usdc_noga as U  # noqa: PLC0415
    return C, B, U, NSH


def шаблон_ноги(C, NSH, U) -> dict:
    """Статичный шаблон первой ноги SOL -> USDC из репозитория. Цена -- образца."""
    п = Path(C.DATA) / "nogi_shablony.json"
    if not п.exists():
        return {"ok": False, "why_not": "data/nogi_shablony.json нет"}
    обр = (json.loads(п.read_text(encoding="utf-8")).get("shablony") or {}).get(U.USDC)
    if not обр:
        return {"ok": False, "why_not": "в шаблонах ног нет USDC"}
    зап = NSH.запись_из_образца(U.USDC, обр)
    if not isinstance(зап, dict):
        return {"ok": False, "why_not": f"образец не стал шаблоном: {зап}"}
    return {"ok": True, "запись": зап}


def факт_по_цepi(C, tx: dict, tpl: dict, база: str, котировка: str) -> dict:
    """Сколько источник отдал котировки и получил базы -- по его же балансам."""
    строки = {r["account"]: r for r in C.token_rows(tx).values()}
    счета = set(tpl.get("accounts") or [])
    отдал = sum(r["post"] - r["pre"] for a, r in строки.items()
                if a in счета and r.get("mint") == котировка and r["post"] > r["pre"])
    получил = sum(r["pre"] - r["post"] for a, r in строки.items()
                  if a in счета and r.get("mint") == база and r["pre"] > r["post"])
    if отдал <= 0 or получил <= 0:
        return {"ok": False, "why_not": "по хранилищам это не покупка базы за котировку"}
    return {"ok": True, "отдал_usdc": int(отдал), "получил_token": int(получил)}


def налог_perevoda(C, tx: dict, ст: dict):
    """(bps, максимум) налога перевода базы -- замером по этой же сделке.

    У Token-2022 налог берётся с КАЖДОГО перевода, и котировщик без ставки честно
    отказывается (у 5 из 8 образцов DLMM база именно на Token-2022). Ставка здесь
    не читается со счёта минта: пул отдал X, покупатель получил Y, и (X-Y)/X -- это
    она. Классический SPL Token налога не имеет -- там ноль без вопросов.
    """
    if ст.get("base_program") != "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb":
        return (0, None)
    строки = {r["account"]: r for r in C.token_rows(tx).values()}
    счета = set(ст["tpl"].get("accounts") or [])
    отдано = sum(r["pre"] - r["post"] for a, r in строки.items()
                 if a in счета and r.get("mint") == ст["base_mint"] and r["pre"] > r["post"])
    получено = sum(r["post"] - r["pre"] for a, r in строки.items()
                   if a in счета and r.get("mint") == ст["base_mint"] and r["post"] > r["pre"])
    if отдано <= 0 or получено <= 0 or получено > отдано:
        return None
    bps = int(round((отдано - получено) * 10_000 / отдано))
    return (bps, None)


def строка(сигнал: dict, *, модули, запись, лампорты: float, проскальзывания: list) -> dict:
    C, B, U, _NSH = модули
    из_ = {"signature": сигнал.get("signature"), "тип": сигнал.get("тип"),
            "пул": сигнал.get("пул"), "почему": None}
    tx = сигнал.get("tx")
    if not isinstance(tx, dict) or "meta" not in tx:
        из_["почему"] = "в образце нет транзакции целиком"
        return из_
    try:
        tx = U.tx_s_adresami(tx)
    except Exception as exc:  # noqa: BLE001
        из_["почему"] = f"сделка не перевелась в адреса: {type(exc).__name__}"
        return из_
    ст = U.storona(tx, programma=сигнал.get("программа_пула"), pul=сигнал.get("пул"))
    if not ст.get("ok"):
        из_["почему"] = ст.get("why_not")
        return из_
    из_["развёрнут"] = ст.get("razvernut")
    # НАШ РАЗМЕР -- билет по цене первой ноги, той же арифметикой, что в бою.
    с = U.summy_nog(lamporty=int(лампорты), price_sol=запись["price_sol_obrazca"],
                    q_dec=запись["q_dec"],
                    quote_program=(запись.get("mv") or {}).get("base_program"))
    if not с.get("ok"):
        из_["почему"] = с.get("why_not")
        return из_
    из_["usdc_nashi"] = с["leg2_to_pool"]
    # НАЛОГ ПЕРЕВОДА БАЗЫ -- ПО ЦЕПИ, а не догадкой: пул отдал столько, покупатель
    # получил столько, разница и есть налог этого перевода. Нужен котировщикам,
    # которые при Token-2022 без ставки честно отказываются.
    налог_выхода = налог_perevoda(C, tx, ст)
    из_["налог_выхода_bps"] = налог_выхода[0] if налог_выхода else None
    # ШАБЛОН ДЛЯ КИРПИЧНОЙ ЗАМЕНЫ -- СВОЙ. c2_swap_build читает роли по своей
    # таблице (DYN/SPECS), и шаблон строителя типа ей не годится: у CLMM swap (v1)
    # минтов в инструкции нет вовсе, и mints_and_vaults честно отказывает. Хранилище
    # берётся тем же C.identify_pool, которым его находит полоса.
    кирпичный = None
    п_пул = C.identify_pool(tx, сигнал.get("источник") or "",
                            сигнал.get("минт_токена") or "")
    из_["почему_pul"] = п_пул.get("why_not")
    if п_пул.get("ok"):
        кирпичный = B.extract_template(tx, сигнал.get("программа_пула"),
                                       п_пул["pool_vault"])
    ф = факт_по_цepi(C, tx, ст["tpl"], ст["base_mint"], U.USDC)
    из_["факт"] = ф if ф.get("ok") else None
    из_["почему_факт"] = ф.get("why_not")
    # 1) цена события на нашем размере
    ц = U.cena_sobytiya(tx, ст["tpl"], base_mint=ст["base_mint"])
    из_["цена_события"] = {}
    for пр in проскальзывания:
        м = U.min_out_po_sobytiyu(ц, int(с["leg2_to_pool"]), пр)
        из_["цена_события"][str(пр)] = {"ok": м.get("ok"), "min_out": м.get("min_out"),
                                         "expected_out": м.get("expected_out"),
                                         "why_not": м.get("why_not")}
    # 2) котировщик типа на нашем размере. Узла здесь нет, поэтому у типов, чей
    # боевой котировщик читает состояние (CLMM, Whirlpool -- подготовить()), берётся
    # ЗАМЕНА БЕЗ ЧТЕНИЙ: c2_swap_build.min_out_from_reserves. У CLMM это
    # clmm_min_out -- ликвидность L и цена ПОСЛЕ сделки из события SwapEvent в
    # логах плюс ставка конфига из data/clmm_konfigi.json, то есть та же кривая,
    # по которой считает подготовить(), но на состоянии этой же сделки. Чем
    # посчитано -- стоит в колонке «котировщик», и подменять одно другим молча
    # нельзя: у Whirlpool замены нет вовсе, и там честный отказ.
    из_["котировщик"] = {}
    бn = U.KOTIROVSHCHIKI.get(сигнал.get("программа_пула") or "")
    for пр in проскальзывания:
        if бn == U.SPOSOB_STROITEL:
            if кирпичный is None or not кирпичный.get("ok"):
                из_["котировщик"][str(пр)] = {
                    "ok": False, "min_out": None, "expected_out": None, "chtenij": 0,
                    "put": "min_out_from_reserves (замена подготовить(): без узла)",
                    "why_not": f"шаблон кирпичей не снялся: {(кирпичный or {}).get('why_not')}"}
                continue
            мо = B.min_out_from_reserves(кирпичный, tx, int(с["leg2_to_pool"]), пр)
            мо = мо if isinstance(мо, dict) else {}
            из_["котировщик"][str(пр)] = {
                "ok": bool(мо.get("ok")), "min_out": мо.get("min_out"),
                "expected_out": мо.get("expected_out"), "chtenij": 0,
                "put": "min_out_from_reserves (замена подготовить(): без узла)",
                "why_not": мо.get("why_not")}
            continue
        к = U.min_out_boj(ст, tx, amount_in=int(с["leg2_to_pool"]), proskalzyvanie=пр,
                          rpc_call=None, pul=сигнал.get("пул"),
                          mint_bazy=ст["base_mint"], nalog_vyhoda=налог_выхода)
        из_["котировщик"][str(пр)] = {"ok": к.get("ok"), "min_out": к.get("min_out"),
                                       "expected_out": к.get("expected_out"),
                                       "put": к.get("put"), "chtenij": к.get("chtenij"),
                                       "why_not": к.get("why_not")}
    # 3) СВЕРКА С ЦЕПЬЮ: котировщику даётся ВХОД ИСТОЧНИКА, и его ожидание
    # сравнивается с тем, что источник РЕАЛЬНО получил. На нашем размере факта не
    # существует -- сделки не было, -- а здесь он есть, и это единственная честная
    # сверка модели с цепью. Отклонение вниз -- это и есть цена того, что
    # котировщик считает по состоянию ПОСЛЕ сделки источника: на его объёме.
    из_["на_входе_istochnika"] = None
    if ф.get("ok"):
        вх = int(ф["отдал_usdc"])
        if бn == U.SPOSOB_STROITEL:
            мо = (B.min_out_from_reserves(кирпичный, tx, вх, 0.0)
                  if (кирпичный or {}).get("ok") else {})
        else:
            к2 = U.min_out_boj(ст, tx, amount_in=вх, proskalzyvanie=0.0,
                               rpc_call=None, pul=сигнал.get("пул"),
                               mint_bazy=ст["base_mint"], nalog_vyhoda=налог_выхода)
            мо = {"ok": к2.get("ok"), "expected_out": к2.get("expected_out"),
                  "why_not": к2.get("why_not")}
        мо = мо if isinstance(мо, dict) else {}
        из_["на_входе_istochnika"] = {
            "ok": bool(мо.get("ok")), "expected_out": мо.get("expected_out"),
            "fakt": ф["получил_token"], "why_not": мо.get("why_not"),
            "otklonenie": ((мо["expected_out"] / ф["получил_token"] - 1.0)
                           if мо.get("ok") and ф["получил_token"] else None)}
    return из_


def свод(строки: list, проскальзывания: list) -> dict:
    по_типам = collections.OrderedDict()
    for с in строки:
        т = с.get("тип") or "прочее"
        д = по_типам.setdefault(т, {"сигналов": 0, "сторона": 0, "факт": 0,
                                     "котировщик": 0, "путь": set(), "чтений": set(),
                                     "сверка_с_цепью": [], "сверка_строк": 0,
                                     "завышение": [], "откатов": {str(п): 0 for п in проскальзывания},
                                     "почему": collections.Counter()})
        д["сигналов"] += 1
        if с.get("почему"):
            д["почему"][с["почему"][:58]] += 1
            continue
        д["сторона"] += 1
        if с.get("факт"):
            д["факт"] += 1
        к0 = (с.get("котировщик") or {}).get(str(проскальзывания[0])) or {}
        ц0 = (с.get("цена_события") or {}).get(str(проскальзывания[0])) or {}
        if к0.get("put"):
            д["путь"].add(str(к0["put"]))
        if not к0.get("ok"):
            д["почему"][str(к0.get("why_not"))[:58]] += 1
            continue
        д["котировщик"] += 1
        д["чтений"].add(к0.get("chtenij"))
        if ц0.get("ok") and к0.get("expected_out"):
            д["завышение"].append(ц0["expected_out"] / к0["expected_out"] - 1.0)
        св = с.get("на_входе_istochnika") or {}
        if св.get("otklonenie") is not None:
            д["сверка_с_цепью"].append(св["otklonenie"])
            д["сверка_строк"] += 1
        for п in проскальзывания:
            ц = (с["цена_события"] or {}).get(str(п)) or {}
            к = (с["котировщик"] or {}).get(str(п)) or {}
            if ц.get("ok") and к.get("ok") and ц["min_out"] > к["expected_out"]:
                д["откатов"][str(п)] += 1
    return по_типам


def печать(св: dict, проскальзывания: list) -> None:
    print("MIN_OUT второй ноги: цена события против котировщика типа "
          f"(наш размер; проскальзывания {', '.join(str(п) for п in проскальзывания)})")
    print("  тип          сигналов  сторона  факт  котировщик  завышение цены события "
          "(медиана/макс)  откатов по цене события")
    for т, д in св.items():
        зав = д["завышение"]
        мед = f"{statistics.median(зав) * 100:+.2f}%" if зав else "--"
        мак = f"{max(зав) * 100:+.2f}%" if зав else "--"
        отк = " ".join(f"{п}:{д['откатов'][str(п)]}" for п in проскальзывания)
        print(f"  {т:<12} {д['сигналов']:>8} {д['сторона']:>8} {д['факт']:>5} "
              f"{д['котировщик']:>11}   {мед:>8} / {мак:>8}            {отк}")
        if д["путь"]:
            print(f"      котировщик: {', '.join(sorted(д['путь']))} "
                  f"(чтений {sorted(x for x in д['чтений'] if x is not None)})")
        св = д["сверка_с_цепью"]
        if св:
            print(f"      сверка с цепью (вход источника, ожидание/факт-1): строк "
                  f"{д['сверка_строк']}, медиана {statistics.median(св) * 100:+.2f}%, "
                  f"худшее вверх {max(св) * 100:+.2f}%, худшее вниз "
                  f"{min(св) * 100:+.2f}%")
        for п, н in д["почему"].most_common(3):
            print(f"      отказ x{н}: {п}")


def сверка_s_faktom(строки: list, проскальзывания: list) -> dict:
    """Наш min_out против ФАКТИЧЕСКОГО выхода источника -- на ЕГО размере.

    На нашем размере фактического выхода не существует: сделки не было. Поэтому
    сверка с цепью делается там, где факт есть: котировщику даётся ВХОД
    ИСТОЧНИКА, и его ожидание сравнивается с тем, что источник получил. Отклонение
    и есть цена того, что котировщик считает по состоянию ПОСЛЕ сделки.
    """
    return {"что": "см. --sverka", "строк": len(строки)}


def main() -> int:  # noqa: C901
    п = argparse.ArgumentParser()
    п.add_argument("--obrazcy", default=str(ПО_УМОЛЧАНИЮ))
    п.add_argument("--moduli", default=None)
    п.add_argument("--sol", type=float, default=0.01)
    п.add_argument("--proskalzyvaniya", default="0.05,0.10,0.35")
    п.add_argument("--out", default=None)
    а = п.parse_args()
    if not Path(а.obrazcy).exists():
        print(f"образцов нет: {а.obrazcy}")
        return 2
    модули = _модули(а.moduli)
    C, B, U, NSH = модули
    B.загрузить_ставки_clmm()
    ш = шаблон_ноги(C, NSH, U)
    if not ш.get("ok"):
        print(f"шаблона первой ноги нет: {ш.get('why_not')}")
        return 2
    прс = [float(x) for x in а.proskalzyvaniya.split(",") if x.strip()]
    лампорты = int(round(а.sol * ЛАМПОРТОВ_В_SOL))
    with open(а.obrazcy, encoding="utf-8") as ф:
        д = json.load(ф)
    строки = [строка(с, модули=модули, запись=ш["запись"], лампорты=лампорты,
                      проскальзывания=прс)
              for с in (д.get("сигналы") or [])]
    св = свод(строки, прс)
    print(f"размер покупки {а.sol} SOL -> {строки[0].get('usdc_nashi')} USDC "
          f"(билет по цене статичного шаблона первой ноги)" if строки else "образцов нет")
    печать(св, прс)
    if а.out:
        готово = {т: {к: (sorted(v) if isinstance(v, set) else v)
                       for к, v in д_.items() if к != "почему"}
                   | {"почему": dict(д_["почему"])} for т, д_ in св.items()}
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump({"свод": готово, "строки": строки}, ф, ensure_ascii=False, indent=1)
        print(f"  отчёт: {а.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
