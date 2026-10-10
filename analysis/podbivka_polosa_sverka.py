#!/usr/bin/env python3
"""Модель против живых сделок полосы: поправки по ТИПУ ПУЛА, а не одна на всё.

Зачем. Поправки k_f 1.0099 и k_g 1.0127 измерены на 44 живых сделках ОДНОЙ группы
(vol_4vw), все на кривой pump.fun и все с продажей через Jupiter. Переносить их на все
площадки нельзя. Здесь то же сравнение делается на ВСЕХ закрытых сделках полосы за окно,
по группам, по типам пулов и по пути продажи.

Чего в данных НЕТ, и об этом сказано прямо. Поля `tokenov_polucheno_raw`,
`vhod_sol_po_cepi`, `vyhod_sol_po_cepi` и `put_prodazhi` живая выгрузка заполняет ТОЛЬКО
с 2026-10-09 (версия pravilo14v): за 27.09--08.10 они пусты у всех 1 710 сделок. Поэтому
разложить поправку на две ноги (токены на входе и SOL на выходе) и на путь продажи можно
лишь на 44 сделках 09--10.10. На всём окне сравнивается КРУГ целиком: модельный итог против
`итог_po_cepi_sol` (сумма изменений наших счетов по двум подписям) -- то есть поправка
выходит общая, k_f·k_g, а не две раздельные.

Тип пула живая строка за то окно тоже не несёт: там заполнен только `pool_reserve_kind`
(реальный / виртуальный). Площадка берётся из АРХИВА -- живая сделка сшивается с сигналом
кэша по подписи источника (`source_sig` против `подпись`), и у сигнала есть и площадка, и
состояния пула, по которым считается круг. Новой прокачки архива для этого не нужно.

Удержание берётся фактическое (`hold_slots_fact`), а горизонт выхода -- ближайший из сетки
кэша, и какой именно взят, печатается рядом.

Только чтение. Выход: data/podbivka/polosa_sverka.json.
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_bilet as B           # noqa: E402
import podbivka_paket_obshee as O    # noqa: E402
import podbivka_zapuski_svod as Z    # noqa: E402

КОРЕНЬ = O.КОРЕНЬ
П = O.П
ВЕТКА_C1 = "origin/claude/nifty-sagan-r0polg"
ГОРИЗОНТЫ = (6, 12, 24, 30, 36, 60, 72, 108, 150, 300)
ПОЛЯ_КАЛИБРОВКИ = ("tokenov_polucheno_raw", "vhod_sol_po_cepi", "vyhod_sol_po_cepi",
                   "put_prodazhi", "итог_po_cepi_sol", "pool_program")


def живые(с: str, до: str) -> list:
    """Закрытые сделки полосы за окно, дедуплицированные по cid."""
    имена = subprocess.run(["git", "ls-tree", "-r", "--name-only", ВЕТКА_C1],
                           capture_output=True, text=True, check=True).stdout.split()
    файлы = sorted(f for f in имена if f.startswith("data/sdelki_polosy_"))
    по_cid: dict = {}

    def балл(y: dict) -> int:
        return sum(1 for p in ПОЛЯ_КАЛИБРОВКИ if y.get(p) is not None)

    for ф in файлы:
        т = subprocess.run(["git", "show", f"{ВЕТКА_C1}:{ф}"],
                           capture_output=True, text=True).stdout
        try:
            д = json.loads(т)
        except ValueError:
            continue
        for x in д.get("ряды") or []:
            к = x.get("cid") or x.get("source_sig")
            if not к:
                continue
            п = по_cid.get(к)
            if п is None or балл(x) > балл(п):
                по_cid[к] = x
    из_ = [x for x in по_cid.values()
           if x.get("utc") and с <= x["utc"][:19] <= до
           and (x.get("state") or "closed") == "closed"]
    return sorted(из_, key=lambda x: x["utc"])


def сигналы_по_подписи(нужны: set) -> dict:
    """Сигналы кэша, чьи подписи просят живые сделки."""
    из_: dict = {}
    for р in O.сигналы(фильтр=lambda r: r.get("подпись") in нужны):
        из_[р["подпись"]] = р
    return из_


def горизонт(дер: float | None) -> int:
    """Ближайший горизонт сетки кэша к фактическому удержанию."""
    if not дер:
        return 36
    return min(ГОРИЗОНТЫ, key=lambda h: abs(h - дер))


def кванты(v: list, знаков: int = 5) -> dict:
    if not v:
        return {"n": 0}
    v = sorted(v)

    def q(p):
        return round(v[min(len(v) - 1, int(p * len(v)))], знаков)
    return {"n": len(v), "p10": q(0.10), "p25": q(0.25),
            "медиана": round(statistics.median(v), знаков), "p75": q(0.75),
            "p90": q(0.90), "среднее": round(statistics.fmean(v), знаков)}


def главное(а) -> int:  # noqa: PLR0912, PLR0915
    print(f"живые сделки полосы {а.s} .. {а.do}", flush=True)
    сд = живые(а.s, а.do)
    print(f"  закрытых сделок {len(сд)}", flush=True)
    if not сд:
        print("сделок в окне нет", flush=True)
        return 2
    нужны = {x["source_sig"] for x in сд if x.get("source_sig")}
    сиг = сигналы_по_подписи(нужны)
    print(f"  сшито с сигналами кэша {len(сиг)} из {len(нужны)}", flush=True)

    кал = (json.loads((П / "kalibrovka.json").read_text(encoding="utf-8"))
           .get("калибровка") or {})
    изд_общ = float(кал.get("издержки_медиана_sol") or 0.002)

    пары: list = []
    отк: collections.Counter = collections.Counter()
    for x in сд:
        п_ = сиг.get(x.get("source_sig"))
        if п_ is None:
            отк["сигнала_в_кэше_нет"] += 1
            continue
        б = x.get("bilet_sol") or x.get("size_sol")
        if not б:
            отк["в_живой_строке_нет_билета"] += 1
            continue
        H = горизонт(x.get("hold_slots_fact") or x.get("hold_slots_plan"))
        с_ = O.как_сигнал(п_)
        изд = изд_общ
        чай, при = x.get("чаевые_sol"), x.get("приоритет_sol")
        if чай is not None or при is not None:
            изд = (чай or 0) + (при or 0)
            отк["издержки_из_живой_строки"] += 1
        # модель БЕЗ поправок: k_f = k_g = 1, чтобы поправка и вышла из сравнения
        итог, вид = None, None
        сост = (с_.get("модель") or {}).get("состояния") or {}
        вх = (сост.get("вход") or {}).get("S0_дно")
        вых = (сост.get("выход") or {}).get(str(H))
        вид = Z.вид_состояния(вх, вых) if (вх and вых) else "пусто"
        кр = (B.сделка_по_состояниям(с_, б, "S0_дно", H, издержки=изд, к_f=1.0, к_g=1.0)
              if вид == "годно" else None)
        if кр is None:
            отк[f"круг_не_посчитан|{вид}"] += 1
            continue
        факт = x.get("итог_po_cepi_sol")
        если_нет = x.get("итог_sol")
        # Правило входа на ПРОВЕРКУ. Поправка k по выручке объясняет лишь часть разрыва
        # (0.989 на выходе даёт 0.003 SOL из 0.012), значит дело ещё и в том, какое
        # состояние модель берёт за вход: она покупает по цене слота САМОГО источника,
        # а живая полоса садится позже и хуже. Кэш держит S0_дно, S1 и S2 -- считаем круг
        # на каждом и смотрим, какое ближе к факту.
        по_местам = {}
        for м_ in ("S0_дно", "S1", "S2"):
            в_ = (сост.get("вход") or {}).get(м_)
            if not в_:
                continue
            вид_ = Z.вид_состояния(в_, вых) if вых else "пусто"
            if вид_ != "годно":
                continue
            к_ = B.сделка_по_состояниям(с_, б, м_, H, издержки=изд, к_f=1.0, к_g=1.0)
            if к_:
                по_местам[м_] = round(к_["итог_sol"], 8)
        пары.append({
            "utc": x.get("utc"), "группа": x.get("group"), "минт": x.get("mint"),
            "площадка": п_.get("пул"), "резерв_вид": x.get("pool_reserve_kind"),
            "путь_продажи": x.get("put_prodazhi"), "билет": б,
            "удержание_факт": x.get("hold_slots_fact"), "горизонт": H,
            "издержки": round(изд, 8),
            "модель_sol": round(кр["итог_sol"], 8),
            "модель_токенов": кр["токенов"], "модель_out": round(кр["out"], 8),
            "факт_sol": факт if факт is not None else если_нет,
            "факт_по_цепи": факт is not None,
            "токенов_факт": (x.get("tokenov_polucheno_raw") or 0) / 1e6 or None,
            "вход_sol_факт": x.get("vhod_sol_po_cepi"),
            "выход_sol_факт": x.get("vyhod_sol_po_cepi"),
            "по_местам_входа": по_местам,
            "вид": вид})
    print(f"  посчитано пар {len(пары)}; отказы {dict(отк)}", flush=True)

    def срез(ряды: list) -> dict:
        м = [x["модель_sol"] for x in ряды if x["факт_sol"] is not None]
        ф = [x["факт_sol"] for x in ряды if x["факт_sol"] is not None]
        раз = [x["модель_sol"] - x["факт_sol"] for x in ряды if x["факт_sol"] is not None]
        # Поправка круга: насколько модель надо УМНОЖИТЬ на выходе, чтобы итоги сошлись.
        # Считается по выручке: (факт + билет + издержки) / (модель_out).
        к_круга = []
        for x in ряды:
            if x["факт_sol"] is None or not x["модель_out"]:
                continue
            факт_out = x["факт_sol"] + x["билет"] + x["издержки"]
            if факт_out > 0:
                к_круга.append(факт_out / x["модель_out"])
        # Поноговые поправки -- только там, где живая выгрузка даёт факт по ногам
        к_f = [x["токенов_факт"] / x["модель_токенов"] for x in ряды
               if x.get("токенов_факт") and x.get("модель_токенов")]
        к_g = [x["выход_sol_факт"] / x["модель_out"] for x in ряды
               if x.get("выход_sol_факт") and x.get("модель_out")]
        места = {}
        for м_ in ("S0_дно", "S1", "S2"):
            z = [(x["по_местам_входа"][м_], x["факт_sol"]) for x in ряды
                 if x["факт_sol"] is not None and м_ in (x.get("по_местам_входа") or {})]
            if z:
                мод = [a for a, _ in z]
                фк = [b for _, b in z]
                места[м_] = {"n": len(z),
                             "модель_среднее": round(statistics.fmean(мод), 6),
                             "факт_среднее": round(statistics.fmean(фк), 6),
                             "разница_среднее": round(
                                 statistics.fmean([a - b for a, b in z]), 6),
                             "разница_медиана": round(
                                 statistics.median([a - b for a, b in z]), 6)}
        из_ = {"сделок": len(ряды), "с_фактом": len(ф), "по_месту_входа": места,
               "модель_сумма": round(sum(м), 5) if м else 0.0,
               "факт_сумма": round(sum(ф), 5) if ф else 0.0,
               "модель_среднее": round(statistics.fmean(м), 6) if м else None,
               "факт_среднее": round(statistics.fmean(ф), 6) if ф else None,
               "разница_среднее": round(statistics.fmean(раз), 6) if раз else None,
               "разница_медиана": round(statistics.median(раз), 6) if раз else None,
               "модель_в_плюсе": sum(1 for y in м if y > 0),
               "факт_в_плюсе": sum(1 for y in ф if y > 0),
               "k_круга": кванты(к_круга), "k_f_по_ногам": кванты(к_f),
               "k_g_по_ногам": кванты(к_g)}
        return из_

    по_площадке = collections.defaultdict(list)
    по_группе = collections.defaultdict(list)
    по_пути = collections.defaultdict(list)
    по_резерву = collections.defaultdict(list)
    for x in пары:
        по_площадке[x["площадка"] or "<нет>"].append(x)
        по_группе[x["группа"] or "<нет>"].append(x)
        по_пути[x["путь_продажи"] or "<не назван>"].append(x)
        по_резерву[x["резерв_вид"] or "<нет>"].append(x)

    тело = {"что": "модель против живых сделок полосы: поправки по типу пула и пути продажи",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "окно": {"с": а.s, "до": а.do},
            "чего_нет_в_данных":
                "поля tokenov_polucheno_raw, vhod_sol_po_cepi, vyhod_sol_po_cepi и "
                "put_prodazhi живая выгрузка заполняет только с 2026-10-09 (pravilo14v); "
                "за 27.09--08.10 они пусты у всех сделок, поэтому поправка на этом окне "
                "выходит ОБЩАЯ по кругу (k_f*k_g), а не две раздельные, и путь продажи "
                "там не назван",
            "сделок_живых": len(сд), "сшито_с_кэшем": len(сиг),
            "пар": len(пары), "отказы": dict(отк),
            "всё": срез(пары),
            "по_площадке": {k: срез(v) for k, v in sorted(
                по_площадке.items(), key=lambda kv: -len(kv[1]))},
            "по_группе": {k: срез(v) for k, v in sorted(
                по_группе.items(), key=lambda kv: -len(kv[1]))},
            "по_пути_продажи": {k: срез(v) for k, v in sorted(
                по_пути.items(), key=lambda kv: -len(kv[1]))},
            "по_виду_резерва": {k: срез(v) for k, v in sorted(
                по_резерву.items(), key=lambda kv: -len(kv[1]))},
            "пары": пары[:600]}
    ф = П / f"polosa_sverka{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    print(json.dumps({k: v for k, v in тело.items() if k != "пары"},
                     ensure_ascii=False, indent=1)[:4000], flush=True)
    print(f"\n{'площадка':20}{'сделок':>8}{'модель ср':>12}{'факт ср':>11}"
          f"{'разница':>10}{'k круга мед':>13}", flush=True)
    for k, v in тело["по_площадке"].items():
        if not v.get("с_фактом"):
            continue
        print(f"{k:20}{v['сделок']:>8}{v['модель_среднее']:>+12.5f}"
              f"{v['факт_среднее']:>+11.5f}{v['разница_среднее']:>+10.5f}"
              f"{v['k_круга'].get('медиана', 0):>13.4f}", flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", default="2026-09-27T00:00:00", help="начало окна, UTC")
    р.add_argument("--do", default="2026-10-08T23:59:59", help="конец окна, UTC")
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
