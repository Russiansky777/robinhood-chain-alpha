#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ЧИСЛО ДЛЯ ШТАБА: билет от резерва -- распределение резервов и какой билет вышел бы.

ЭТО ЗАМЕР, А НЕ ВРЕЗКА. Считает по выгрузке сделок полосы: у скольких наших сделок
резерв пула вообще есть, какой он, и какой билет дала бы
c3_bilet_ot_rezerva.bilet() при потолке 0.5 SOL и доле 1 % / 1.5 % / 2 % --
таблицей по ГРУППАМ и по ТИПАМ ПУЛОВ. Ни одной сетевой операции.

ГДЕ ЖИВЁТ РЕЗЕРВ. Полоса пишет его с 27.09 в РЕШЕНИЕ (bloom_own_send:
`из_["pool_reserve_sol"]`, `pool_reserve_kind`), а выгрузка сделок кладёт в ряд
запись ПОЗИЦИИ (`zapis`) плюс три поля из журнала решений (налог, комиссия пула,
строитель). Резерва среди них нет, поэтому замер честно говорит «у 0 из N рядов
резерва нет» и называет, что для этого нужно: одна строка в
deploy/checks/sdelki_polosy_vygruzka.py (из_решений), переносящая
`pool_reserve_sol`/`pool_reserve_kind` в ряд, -- после неё распределение считается
по нашим сделкам само.

ВТОРОЙ ИСТОЧНИК (--iz-obrazcov): живые сделки источников, которые лежат в
репозитории (data/c2_pool_samples/*.json, data/c3_usdc_noga/obrazcy_usdc_noga.json).
По ним резерв считается тем же модулем -- остатком котировочного хранилища, а у
CLMM ёмкостью активного шага тика, -- и распределение выходит ПО ТИПАМ ПУЛОВ.
Это НЕ наши сделки, и так и подписано.

Запуск: python3 deploy/checks/bilet_ot_rezerva_chislo.py [--vygruzka <путь>]
        [--potolok 0.5] [--doli 0.01,0.015,0.02] [--iz-obrazcov] [--out <файл>]
Выход 2 -- файла нет; 3 -- резерва нет ни у одного ряда (молчаливый ноль -- провал).
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[2]
ВЫГРУЗКА_ПО_УМОЛЧАНИЮ = КОРЕНЬ / "data" / "sdelki_polosy_2026-10-01_pravilo13.json"
# Цена USDC в SOL -- из статичного шаблона первой ноги репозитория (образец 28.09),
# нужна только для SOL-эквивалента резервов пулов с котировкой USDC.
ФАЙЛ_ЦЕНЫ_USDC = КОРЕНЬ / "data" / "nogi_shablony.json"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ТИПЫ_ПУЛОВ = {
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "DAMM v2",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "AMM v4",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "DLMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "CLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Whirlpool",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "DBC",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Launchlab",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "кривая pump.fun",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "DAMM v1",
}


def _модули(путь: str | None = None):
    sys.path.insert(0, str(Path(путь) if путь else КОРЕНЬ / "analysis"))
    import c3_bilet_ot_rezerva as BR  # noqa: PLC0415
    return BR


def цена_usdc_v_sol() -> float | None:
    if not ФАЙЛ_ЦЕНЫ_USDC.exists():
        return None
    д = json.loads(ФАЙЛ_ЦЕНЫ_USDC.read_text(encoding="utf-8")).get("shablony") or {}
    з = (д.get(USDC) or {}).get("price_sol_v_sdelke")
    return float(з) if isinstance(з, (int, float)) else None


def резерв_ряда(ряд: dict, BR) -> dict:
    """Резерв ряда выгрузки: сам ряд, потом запись позиции внутри него."""
    р = BR.rezerv_iz_zapisi(ряд)
    if р.get("ok") or р.get("pole"):
        return р
    з = ряд.get("zapis")
    return BR.rezerv_iz_zapisi(з) if isinstance(з, dict) else р


def свод(строки: list, доли: list) -> dict:
    из_ = collections.OrderedDict()
    for с in строки:
        ключ = с["ключ"]
        д = из_.setdefault(ключ, {"сделок": 0, "резерв_есть": 0, "резервы": [],
                                   "билеты": {str(x): [] for x in доли},
                                   "ниже_потолка": {str(x): 0 for x in доли},
                                   "почему": collections.Counter()})
        д["сделок"] += 1
        if с.get("rezerv_sol") is None:
            д["почему"][(с.get("rezerv_why_not") or "резерва нет")[:58]] += 1
        else:
            д["резерв_есть"] += 1
            д["резервы"].append(с["rezerv_sol"])
        for x in доли:
            b = с["билеты"][str(x)]
            # МЕДИАНА БИЛЕТА -- ТОЛЬКО ПО РЯДАМ С РЕЗЕРВОМ. Иначе её задавали бы
            # ряды, где билет равен потолку просто потому, что резерва нет, и
            # число отвечало бы не на тот вопрос.
            if с.get("rezerv_sol") is not None:
                д["билеты"][str(x)].append(b["bilet_sol"])
            if b.get("ot_rezerva"):
                д["ниже_потолка"][str(x)] += 1
    return из_


def печать(заголовок: str, св: dict, доли: list, потолок: float) -> None:
    print(f"\n{заголовок} (потолок {потолок} SOL)")
    print("  ключ                 сделок  резерв есть  резерв SOL-экв "
          "(мин/медиана/макс)   доля: билетов ниже потолка из тех, где резерв "
          "есть, и медиана билета по ним")
    for ключ, д in св.items():
        р = д["резервы"]
        строка_р = (f"{min(р):.3f}/{statistics.median(р):.3f}/{max(р):.3f}"
                    if р else "--")
        части = []
        for x in доли:
            б = [z for z in д["билеты"][str(x)] if z is not None]
            мед = f"{statistics.median(б):.4f}" if б else "--"
            части.append(f"{x}: {д['ниже_потолка'][str(x)]}/{д['резерв_есть']} "
                         f"мед {мед}")
        print(f"  {ключ:<20} {д['сделок']:>6} {д['резерв_есть']:>12}  {строка_р:<28} "
              f"{' | '.join(части)}")
        for п, н in д["почему"].most_common(2):
            print(f"      нет резерва x{н}: {п}")


def по_выгрузке(путь: str, BR, доли: list, потолок: float) -> dict:
    д = json.loads(Path(путь).read_text(encoding="utf-8"))
    ряды = д.get("ряды") or []
    строки_групп, строки_типов = [], []
    for р in ряды:
        рез = резерв_ряда(р, BR)
        билеты = {}
        for x in доли:
            билеты[str(x)] = BR.bilet(потолок, рез.get("rezerv_sol"), x)
        тип = ТИПЫ_ПУЛОВ.get(р.get("stroitel") or "", (р.get("stroitel") or "?")[:12])
        общее = {"rezerv_sol": рез.get("rezerv_sol"),
                  "rezerv_why_not": рез.get("why_not"), "билеты": билеты}
        строки_групп.append(dict(общее, ключ=(р.get("group") or "без группы")))
        строки_типов.append(dict(общее, ключ=тип))
    return {"рядов": len(ряды), "окно": [д.get("с"), д.get("до")],
            "по_группам": свод(строки_групп, доли),
            "по_типам": свод(строки_типов, доли),
            "резерв_есть": sum(1 for с in строки_групп if с["rezerv_sol"] is not None)}


def по_образцам(BR, доли: list, потолок: float) -> dict:
    """Резервы живых сделок источников из репозитория -- по типам пулов."""
    цена = цена_usdc_v_sol()
    строки = []
    for прог, метка in ТИПЫ_ПУЛОВ.items():
        п = КОРЕНЬ / "data" / "c2_pool_samples" / f"{прог}.json"
        if not п.exists():
            continue
        for x in json.loads(п.read_text(encoding="utf-8")):
            tx = x.get("tx")
            if not isinstance(tx, dict):
                continue
            строки.append(одна_сделка(BR, tx, метка=метка, прог=прог,
                                       источник=x.get("source"), минт=x.get("mint"),
                                       пул=None, цена_usdc=цена, доли=доли,
                                       потолок=потолок))
    п_usdc = КОРЕНЬ / "data" / "c3_usdc_noga" / "obrazcy_usdc_noga.json"
    if п_usdc.exists():
        for r in json.loads(п_usdc.read_text(encoding="utf-8")).get("ряды") or []:
            метка = ТИПЫ_ПУЛОВ.get(r.get("program") or "", r.get("tip") or "?")
            строки.append(одна_сделка(BR, r.get("транзакция") or {}, метка=метка,
                                       прог=r.get("program"), источник=None,
                                       минт=None, пул=r.get("пул"), цена_usdc=цена,
                                       доли=доли, потолок=потолок))
    return {"строк": len(строки), "по_типам": свод(строки, доли),
            "цена_usdc_sol": цена}


def одна_сделка(BR, tx: dict, *, метка: str, прог: str | None, источник, минт,
                 пул, цена_usdc, доли: list, потолок: float) -> dict:
    """Резерв одной живой сделки источника: хранилище, а у CLMM -- шаг тика."""
    рез, почему = None, None
    if источник and минт:
        с = BR.rezerv_iz_sdelki(tx, istochnik=источник, mint=минт, programma=прог,
                                 ceny_kotirovok=({USDC: цена_usdc} if цена_usdc
                                                  else None))
        if с.get("ok") and с.get("vid") != BR.VID_VIRTUALNYJ:
            рез = с["rezerv_sol"]
        else:
            почему = с.get("why_not")
    if рез is None and прог == "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo" and пул:
        г = BR.rezerv_dlmm_nizhe_ne_budet(tx, pul=пул)
        почему = (f"DLMM: ёмкости корзины в событии нет; нижняя граница "
                   f"{round(г['granica_sol'], 4)} SOL по сделке источника"
                   if г.get("ok") else г.get("why_not"))
    if рез is None and пул and прог == "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK":
        к = BR.rezerv_clmm_iz_sdelki(tx, pul=пул, mint_kotirovki=USDC, q_dec=6,
                                      cena_sol_za_kotirovku=цена_usdc)
        if к.get("ok"):
            рез = к["rezerv_sol"]
        else:
            почему = к.get("why_not")
    if рез is None and почему is None:
        почему = "источник и минт образца не названы -- пул не опознать"
    return {"ключ": метка, "rezerv_sol": рез, "rezerv_why_not": почему,
             "билеты": {str(x): BR.bilet(потолок, рез, x) for x in доли}}


def main() -> int:
    п = argparse.ArgumentParser()
    п.add_argument("--vygruzka", default=str(ВЫГРУЗКА_ПО_УМОЛЧАНИЮ))
    п.add_argument("--moduli", default=None)
    п.add_argument("--potolok", type=float, default=0.5)
    п.add_argument("--doli", default="0.01,0.015,0.02")
    п.add_argument("--iz-obrazcov", action="store_true")
    п.add_argument("--out", default=None)
    а = п.parse_args()
    if not Path(а.vygruzka).exists():
        print(f"выгрузки нет: {а.vygruzka}")
        return 2
    BR = _модули(а.moduli)
    доли = [float(x) for x in а.doli.split(",") if x.strip()]
    в = по_выгрузке(а.vygruzka, BR, доли, а.potolok)
    print(f"выгрузка {Path(а.vygruzka).name}: рядов {в['рядов']}, окно "
          f"{в['окно'][0]}..{в['окно'][1]}, резерв есть у {в['резерв_есть']}")
    печать("НАШИ СДЕЛКИ ПО ГРУППАМ", в["по_группам"], доли, а.potolok)
    печать("НАШИ СДЕЛКИ ПО ТИПАМ ПУЛОВ", в["по_типам"], доли, а.potolok)
    о = None
    if а.iz_obrazcov:
        о = по_образцам(BR, доли, а.potolok)
        print(f"\nЖИВЫЕ СДЕЛКИ ИСТОЧНИКОВ ИЗ РЕПОЗИТОРИЯ -- это НЕ наши сделки "
              f"(строк {о['строк']}, цена USDC {о['цена_usdc_sol']} SOL)")
        печать("ОБРАЗЦЫ ПО ТИПАМ ПУЛОВ", о["по_типам"], доли, а.potolok)
    if а.out:
        def _чисто(св):
            return {к: {кк: (dict(вв) if isinstance(вв, collections.Counter) else вв)
                        for кк, вв in д.items()} for к, д in св.items()}
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump({"выгрузка": {"рядов": в["рядов"], "окно": в["окно"],
                                     "резерв_есть": в["резерв_есть"],
                                     "по_группам": _чисто(в["по_группам"]),
                                     "по_типам": _чисто(в["по_типам"])},
                        "образцы": ({"строк": о["строк"],
                                     "по_типам": _чисто(о["по_типам"])} if о else None)},
                       ф, ensure_ascii=False, indent=1, default=str)
        print(f"  отчёт: {а.out}")
    if в["резерв_есть"] == 0:
        print("\nРЕЗЕРВА НЕТ НИ У ОДНОГО РЯДА -- распределение по НАШИМ сделкам не\n"
              "считается. Полоса пишет резерв в журнал решений (pool_reserve_sol),\n"
              "а выгрузка переносит из него только налог, комиссию пула и строителя:\n"
              "нужна одна строка в из_решений (sdelki_polosy_vygruzka.py), и числа\n"
              "появятся сами. Молчаливый ноль считается провалом, поэтому выход 3.")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
