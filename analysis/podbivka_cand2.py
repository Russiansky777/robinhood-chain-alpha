#!/usr/bin/env python3
"""Кандидаты cand2: то же правило кандидатов на новом окне. Офлайн, по готовому архиву cand2_*. Только чтение.

Правило -- без изменений (analysis/podbivka_kandidaty_vne.py, оттуда же берутся ячейки и счёт): наш вход «конец
слота» за источником, билеты 0.1 и 0.3, выходы +72 и +108 (главный +108 -- удержание групп), котировка WSOL,
кривая pump.fun / LaunchLab / CPMM / DAMM v1 плюс Pump AMM по модели v6, п.п. чистыми (0.002 SOL на круг).
  * отбор: n ≥ 20, среднее ≥ +2 п.п., медиана > 0 -- окно 24.09 17:00Z → 01.10 17:00Z (файлы cand2_*T16, у каждого
    первый «разгонный» час отброшен);
  * вне выборки: n ≥ 5, среднее > 0 и медиана > 0 -- окно 01.10 17:00Z → сколько есть в архиве (граница печатается);
  * куст -- пары адресов, у которых общих (минт, слот) не меньше половины сигналов: один источник, считать одним;
  * «никто %» -- доля ячеек, где в слотах s0+1 и s0+2 ни одного свопа пула (поле «свопов» архива). Это НЕ та же мера,
    что «никто ≥ 0.5 SOL за 15 с» на странице источников: та считается только для --istochniki, здесь её нет.

Кого считаем: 543 и живые группы (lane_s0 / batch5 / leader / cand1), 38 источников снайперов, 6 адресов USDC,
493 кошелька GMGN (data/podbivka/gmgn_2026-10-01_21Z.csv; buys7d = 0 -- не считаем) и кошельки Fomo (сигналы, где
первый подписант -- сервер Fomo: поле «подписант» архива, флаг --podpisant).

Стадии: --otbor (счёт по архиву -> data/podbivka/cand2_otbor.json), --svod (страница
docs/podbivka_2026-10-02_kandidaty_cand2.md). Налоговые маршруты -- отдельной стадией --nalog по прошедшим
(Helius, как в podbivka_kandidaty_vne.py --nalog); без неё столбец «налог» -- «не проверен».
"""
from __future__ import annotations

import argparse
import calendar
import collections
import csv
import glob
import gzip
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_kandidaty_vne as K  # noqa: E402

КОРЕНЬ = K.КОРЕНЬ
П = K.П
ОТБОР_С = calendar.timegm(time.strptime("2026-09-24T17", "%Y-%m-%dT%H"))
ОТБОР_ДО = calendar.timegm(time.strptime("2026-10-01T17", "%Y-%m-%dT%H"))
N_ОТБОР, ПП_ОТБОР = 20, 2.0
N_ВНЕ = 5
ЖИВЫЕ = ("lane_s0", "batch5", "leader", "cand1")
ГРУППЫ_СЧЁТА = ("543", *ЖИВЫЕ, "снайперские источники", "gmgn", "кандидат")
FOMO = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"


def ячейка(с: dict) -> bool:
    """Ячейка правила: обычные типы пулов или Pump AMM v6; главный выход +108 посчитан."""
    return (K.в_ячейке(с) or K.в_ячейке_amm(с)) and K.пп(с, "108") is not None


def никто(с: dict) -> bool:
    св = с.get("свопов") or {}
    return (св.get("s1") or 0) + (св.get("s2") or 0) == 0


def gmgn() -> dict:
    п = П / "gmgn_2026-10-01_21Z.csv"
    из_ = {}
    if not п.exists():
        return из_
    for r in csv.DictReader(п.open(encoding="utf-8")):
        a = (r.get("walletAddress") or "").strip()
        if not a:
            continue
        def ч(k, по=0.0):
            try:
                return float(r.get(k) or 0)
            except ValueError:
                return по
        из_[a] = {"followCount": int(ч("followCount")), "тип": r.get("traderTypeTegi") or "",
                  "buys7d": int(ч("buys7d")), "winRate7d": ч("winRate7d"), "pnl7d": ч("pnl7d"),
                  "теги": r.get("tags") or ""}
    return из_


def копилка() -> dict:
    return {"сигналов": 0, "ячеек": 0, "никто": 0, "pump_amm": 0, "не_sol": 0,
            "72": [], "108": [], "72_01": [], "108_01": [], "минт_слот": set(), "подписант": 0}


def собрать(файлы: list, в_окне) -> tuple[dict, dict]:
    """По файлам архива -- копилки по адресам и счёт. Сигналы не держим в памяти."""
    по: dict = {}
    счёт = {"файлов": 0, "сигналов": 0, "в_окне": 0, "дублей": 0, "граница_ts": 0, "ошибки": []}
    видел: set = set()
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        счёт["файлов"] += 1
        счёт["ошибки"] += [f"{Path(f).name}: {x}" for x in (д.get("счёт") or {}).get("ошибки") or []]
        for с in д.get("сигналы") or []:
            счёт["сигналов"] += 1
            ts = K.ts(с)
            счёт["граница_ts"] = max(счёт["граница_ts"], ts)
            if not в_окне(с, f):
                continue
            if с["signature"] in видел:
                счёт["дублей"] += 1
                continue
            видел.add(с["signature"])
            счёт["в_окне"] += 1
            к = по.setdefault(с["trader"], копилка())
            к["сигналов"] += 1
            к["pump_amm"] += 1 if с.get("pool") == "pump-amm" else 0
            к["не_sol"] += 1 if с.get("quoteMint") != K.WSOL else 0
            к["подписант"] += 1 if с.get("подписант") else 0
            if ячейка(с):
                к["ячеек"] += 1
                к["никто"] += 1 if никто(с) else 0
                к["минт_слот"].add((с.get("mint"), с.get("block")))
                for кл in ("72", "108", "72_01", "108_01"):
                    v = K.пп(с, кл)
                    if v is not None:
                        к[кл].append(v)
        del д
    return по, счёт


def окно_отбора(с: dict, f: str) -> bool:
    д0 = calendar.timegm(time.strptime(Path(f).name.split("_")[1][:13], "%Y-%m-%dT%H")) + 3600
    return д0 <= K.ts(с) < д0 + 86400 and ОТБОР_С <= K.ts(с) < ОТБОР_ДО


def стат(v: list) -> dict:
    return K.стат(v)


def отбор() -> int:
    import podbivka_run as R  # noqa: PLC0415
    отб_файлы = sorted(glob.glob(str(П / "arhiv_den" / "cand2_2026-*T16.json.gz")))
    вне_файлы = sorted(glob.glob(str(П / "arhiv_den" / "cand2_vne_*.json.gz")))
    if not отб_файлы:
        print("нет файлов cand2_*T16 -- прогон архива не закончен", flush=True)
        return 1
    по_отб, счёт_отб = собрать(отб_файлы, окно_отбора)
    по_вне, счёт_вне = собрать(вне_файлы, lambda с, f: K.ts(с) >= ОТБОР_ДО)
    адр = json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    g = gmgn()
    ряды = {}
    for a in set(по_отб) | set(по_вне):
        о, в = по_отб.get(a) or копилка(), по_вне.get(a) or копилка()
        группы = (адр.get(a) or {}).get("группы") or []
        если_fomo = (о["подписант"] + в["подписант"]) > 0 and not (set(группы) & set(ГРУППЫ_СЧЁТА))
        ряды[a] = {"группы": группы, "fomo": если_fomo,
                   "gmgn": g.get(a), "отбор": {к: стат(о[к]) for к in ("72", "108", "72_01", "108_01")},
                   "отбор_сигналов": о["сигналов"], "отбор_ячеек": о["ячеек"], "отбор_никто": о["никто"],
                   "отбор_pump_amm": о["pump_amm"], "отбор_не_sol": о["не_sol"],
                   "вне": {к: стат(в[к]) for к in ("72", "108", "72_01", "108_01")},
                   "вне_сигналов": в["сигналов"], "вне_ячеек": в["ячеек"], "вне_никто": в["никто"]}
        s108 = ряды[a]["отбор"]["108"]
        ряды[a]["прошёл_отбор"] = bool(s108.get("n", 0) >= N_ОТБОР and s108.get("среднее", -9) >= ПП_ОТБОР
                                       and s108.get("медиана", -9) > 0)
        v108 = ряды[a]["вне"]["108"]
        ряды[a]["прошёл_вне"] = bool(v108.get("n", 0) >= N_ВНЕ and v108.get("среднее", -9) > 0
                                     and v108.get("медиана", -9) > 0)
    # кусты: пары с общим (минт, слот) не меньше половины сигналов (окно отбора)
    мс = {a: (по_отб[a]["минт_слот"]) for a in по_отб if по_отб[a]["минт_слот"]}
    интерес = [a for a in мс if len(мс[a]) >= 5]
    пары = []
    for i, a in enumerate(интерес):
        for b in интерес[i + 1:]:
            общ = len(мс[a] & мс[b])
            if общ and 2 * общ >= min(len(мс[a]), len(мс[b])):
                пары.append({"a": a, "b": b, "общих": общ, "n_a": len(мс[a]), "n_b": len(мс[b])})
    out = П / "cand2_otbor.json"
    out.write_text(json.dumps({
        "окно_отбора": ["2026-09-24T17:00Z", "2026-10-01T17:00Z"],
        "окно_вне": ["2026-10-01T17:00Z", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(счёт_вне["граница_ts"]))
                     if счёт_вне["граница_ts"] else None],
        "порог_отбора": {"n": N_ОТБОР, "среднее_108": ПП_ОТБОР, "медиана_108": "> 0"},
        "порог_вне": {"n": N_ВНЕ, "среднее_108": "> 0", "медиана_108": "> 0"},
        "файлы_отбора": [Path(f).name for f in отб_файлы], "файлы_вне": [Path(f).name for f in вне_файлы],
        "счёт_отбора": счёт_отб, "счёт_вне": счёт_вне, "кусты": пары,
        "адресов": len(ряды), "ряды": ряды}, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    print("адресов с сигналами", len(ряды), "прошли отбор",
          sum(1 for v in ряды.values() if v["прошёл_отбор"]), "прошли и вне",
          sum(1 for v in ряды.values() if v["прошёл_отбор"] and v["прошёл_вне"]), flush=True)
    return 0


def ф(s: dict) -> str:
    return "—" if not s.get("n") else f"{s['n']} / {s['среднее']:+.1f} / {s['медиана']:+.1f} / {100 * s['в_плюсе']:.0f}%"


def главный(a: str, r: dict, куст: dict) -> str:
    г = ", ".join(r["группы"]) or ("Fomo" if r["fomo"] else "")
    gm = r.get("gmgn") or {}
    никто_ = f"{100 * r['отбор_никто'] / r['отбор_ячеек']:.0f}%" if r["отбор_ячеек"] else "—"
    в_списке = "да" if (r["прошёл_отбор"] and r["прошёл_вне"]) else ("отбор" if r["прошёл_отбор"] else "нет")
    к = куст.get(a)
    return (f"| `{a[:8]}` | {г}{' + куст ' + к[:8] if к else ''} | {r['отбор_сигналов']} | {r['отбор_ячеек']} | "
            f"{ф(r['отбор']['108'])} | {ф(r['отбор']['72'])} | {ф(r['отбор']['108_01'])} | {никто_} | "
            f"{ф(r['вне']['108'])} | {в_списке} | "
            f"{gm.get('followCount', '') if gm else ''} | {(gm.get('тип') or '') if gm else ''} |")


def свод() -> int:
    import podbivka_run as R  # noqa: PLC0415
    д = json.loads((П / "cand2_otbor.json").read_text(encoding="utf-8"))
    ряды = д["ряды"]
    старое = json.loads((П / "kandidaty_vne_otbor.json").read_text(encoding="utf-8"))
    было = {a: (старое["по_адресам"].get(a) or {}) for a in старое["по_адресам"]}
    верх_старый = set(старое["верх_543"])
    g = gmgn()
    снайп = {a for a, v in json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"].items()
             if "снайперские источники" in ((v or {}).get("группы") or [])}
    usdc = ["BMgsHTvcasRVtuevHJh8t6Vf5dmcWkDLAx6gSAQ3dsYm", "GZi5tmvZePF3rqWyGJf3wtNaninFKbCWanYjAr736xU3",
            "7txcAXw9t7jRiW93tt6mbJ7cDA5EUSiVSRRfPyzH1DMa", "2L17Sw85Lh2Ca7ejEzWqyJsZ3yS4DxQhYr52X5H5xRns",
            "5VRgqb2qbVqaWVGsM2k1b2bnPJk7up2xYbn4ziEjFgNt", "CzU8MaRcwvwUoNkwJFLbvtFWJugcEXAhDDQqNFE4ybb7"]
    куст = {}
    for п in д["кусты"]:
        куст.setdefault(п["a"], п["b"])
        куст.setdefault(п["b"], п["a"])
    загол = ("| адрес | группы | сигналов | ячеек | отбор +108 | отбор +72 | отбор +108 (0.1) | никто % | "
             "вне +108 | в списке | followCount | тип GMGN |")
    линия = "|---|---|---|---|---|---|---|---|---|---|---|---|"

    def таблица(адреса: list, пусто: str) -> list:
        стр = [главный(a, ряды[a], куст) for a in адреса if a in ряды]
        return [загол, линия] + стр if стр else [пусто]

    гр = lambda a, *g_: bool(set((ряды.get(a) or {}).get("группы") or []) & set(g_))  # noqa: E731
    а_адр = sorted((a for a in ряды if гр(a, "543", *ЖИВЫЕ)),
                   key=lambda a: -(ряды[a]["отбор"]["108"].get("среднее") or -99))
    б_адр = sorted((a for a in ряды if a in снайп), key=lambda a: -(ряды[a]["отбор"]["108"].get("среднее") or -99))
    г_адр = sorted((a for a in ряды if (ряды[a].get("gmgn") or {}).get("buys7d", 0) > 0),
                   key=lambda a: -(ряды[a]["отбор"]["108"].get("среднее") or -99))
    фомо = sorted((a for a in ряды if ряды[a]["fomo"]), key=lambda a: -(ряды[a]["отбор"]["108"].get("медиана") or -99))
    прошли = [a for a in ряды if ряды[a]["прошёл_отбор"] and ряды[a]["прошёл_вне"]]
    только_отбор = [a for a in ряды if ряды[a]["прошёл_отбор"] and not ряды[a]["прошёл_вне"]]

    св = д["счёт_вне"]
    гр_вне = (д["окно_вне"][1] or "нет данных")
    часов_вне = None
    if д["окно_вне"][1]:
        часов_вне = (calendar.timegm(time.strptime(д["окно_вне"][1], "%Y-%m-%dT%H:%M:%SZ")) - ОТБОР_ДО) / 3600
    md = ["# Кандидаты cand2: то же правило на окне 24.09 17Z → 01.10 17Z, проверка вне выборки с 01.10 17Z", "",
          "Правило кандидатов без изменений (счёт и ячейки -- кодом analysis/podbivka_kandidaty_vne.py): вход "
          "«конец слота» за источником, билеты 0.1 и 0.3, выходы +72 и +108 (главный +108), котировка WSOL, "
          "кривая pump.fun / LaunchLab / CPMM / DAMM v1 плюс Pump AMM по модели v6, п.п. чистыми (0.002 SOL на "
          f"круг). Отбор: n ≥ {N_ОТБОР}, среднее ≥ +{ПП_ОТБОР:.0f} п.п., медиана > 0. Вне выборки: n ≥ {N_ВНЕ}, "
          "среднее и медиана > 0.", "",
          f"Архив: {len(д['файлы_отбора'])} суток отбора ({', '.join(д['файлы_отбора'])}) и "
          f"{len(д['файлы_вне'])} файл вне выборки. Сигналов в окне отбора: {д['счёт_отбора']['в_окне']}, "
          f"вне выборки: {св['в_окне']}. Адресов с сигналами: {д['адресов']}.", "",
          f"**Граница окна вне выборки: {гр_вне}** -- это "
          + (f"{часов_вне:.1f} ч" if часов_вне is not None else "нет данных") +
          " от 01.10 17:00Z: меньше суток, как и ожидалось; числа вне выборки на таком окне опорой быть не могут, "
          "они приведены как есть.", "",
          "Столбцы: «сигналов» -- всего по правилу окна (1800 слотов, порог 2 SOL-экв.); «ячеек» -- из них с "
          "посчитанной моделью и котировкой WSOL; ячейка таблицы -- n / среднее / медиана / доля в плюсе, п.п. "
          "«никто %» -- доля ячеек, где в слотах s0+1 и s0+2 ни одного свопа пула (не та же мера, что «никто "
          "≥ 0.5 SOL за 15 с» на странице источников -- её в этом проходе нет). «в списке»: «да» -- прошёл и "
          "отбор, и вне выборки; «отбор» -- прошёл только отбор. Налоговые маршруты в этом проходе НЕ проверены "
          "(это отдельный прогон по цепи), поэтому в счёт вошли все ячейки. Ничего не рекомендуется.", ""]

    md += ["## (а) 543 и живые группы (lane_s0 / batch5 / leader / cand1)", ""] + таблица(а_адр, "— нет сигналов")
    # ротация против окна 21–28.09
    сейчас = {a for a in а_адр if ряды[a]["прошёл_отбор"]}
    тогда = верх_старый
    md += ["", "### Ротация против окна 21.09 17Z – 28.09 17Z (файл kandidaty_vne_otbor.json)", "",
           "Старый отбор считался по +72 (порог тот же), новый -- по +108; это надо держать в уме.", "",
           "Прошёл сейчас, не проходил тогда:", ""]
    md += [f"- `{a}`" + (f" -- тогда +72: {ф(было.get(a, {}).get('72') or {})}" if было.get(a) else " -- тогда сигналов не было")
           for a in sorted(сейчас - тогда)] or ["- никто"]
    md += ["", "Проходил тогда, не проходит сейчас:", ""]
    md += [f"- `{a}` -- сейчас +108: {ф((ряды.get(a) or {}).get('отбор', {}).get('108') or {})}"
           for a in sorted(тогда - сейчас)] or ["- никто"]
    cand1 = [a for a in ряды if "cand1" in (ряды[a]["группы"] or [])]
    md += ["", "Группа `cand1` на новом окне:", ""]
    md += [f"- `{a}` -- отбор +108: {ф(ряды[a]['отбор']['108'])}, вне: {ф(ряды[a]['вне']['108'])}, "
           f"{'проходит' if ряды[a]['прошёл_отбор'] else 'НЕ проходит отбор'}" for a in sorted(cand1)] or ["- нет сигналов"]

    md += ["", "## (б) Источники снайперов (38 адресов: 37 со страницы 28.09 + 7g5CJ754)", ""] + таблица(б_адр, "— нет сигналов")
    девять = ["387FRwow6MKDhSwuMULhKjqeXuxoBY4BbbaSntdk9LaH", "BCrTEXmWutwPz8qv6w1S5gDbaLnSLpXKM5kSGVWyyfxu",
              "DAEdBmTPEKM6xkwfzC3d411QUe6coKpkND6UURa4CvHC", "DrJ6SnDXkEsPeGdmSs93v5rwWumv5QMvAGSZjAyWSd5o",
              "JEBy7VuMsCqZDdprhmUNjB1MHTmt5dUFDeMXytbhTLdR", "4nBNtRX6Q3NAhtcaXfV5R2okHr2Cd6iur7EDQvNedujV",
              "EgYa22bNrcQGRJny4rGJz8eJe5CSFKtXEqB9qPQQGN9p", "DyXg3Xp6BoMq5K6Nmdb7hEzPMpkRWH2aXHHZhUgN1gd6",
              "Bra5EH8vqm5bmGupdEHWcwNepetuvSuW2z4excQR5yNs"]
    md += ["", "### Девять источников снайперов из лога (все девять в 543)", "",
           "| адрес | отбор +108 (новое окно) | вне +108 | окно 21–28.09, +72 | в списке |", "|---|---|---|---|---|"]
    for a in девять:
        r = ряды.get(a)
        md.append(f"| `{a}` | {ф((r or {}).get('отбор', {}).get('108') or {})} | {ф((r or {}).get('вне', {}).get('108') or {})} | "
                  f"{ф(было.get(a, {}).get('72') or {})} | "
                  f"{'да' if r and r['прошёл_отбор'] and r['прошёл_вне'] else ('отбор' if r and r['прошёл_отбор'] else 'нет')} |")

    md += ["", "## (в) Режим 2 на USDC-пулах (CLMM / DLMM / DAMM v2 / Whirlpool)", "",
           "**Нет данных.** Архив PumpApi держит события этих пулов (на них и считались 22 USDC-сигнала за 28–30.09), "
           "но модели входа для них в подбивке нет: сигнал в архиве строится только для кривых pump.fun / LaunchLab "
           "и x*y=k (CPMM / DAMM v1 / Pump AMM), поэтому ни +108, ни билет по этим шести адресам не считаются. "
           "Модель USDC-ноги Code-3 (`c2_usdc_noga`) -- боевой сборщик второй ноги (min_out от резервов), а не "
           "обратный счёт по архиву. Нового конвейера не строю (Правило 10.4). Адреса для протокола: "
           + ", ".join(f"`{a}`" for a in usdc) + ".", ""]

    md += ["## (г) Список GMGN (493 кошелька, buys7d > 0)", "",
           f"Файл data/gmgn/gmgn_2026-10-01_21Z.csv ветки claude/stroiteli (копия data/podbivka/gmgn_2026-10-01_21Z.csv). "
           f"У 179 из 493 buys7d = 0 -- они не считаются. Уже в 543 или живых группах: "
           f"{sum(1 for a in g if (ряды.get(a) or {}).get('группы') and set(ряды[a]['группы']) & {'543', *ЖИВЫЕ})} "
           "(в таблице (а) они же, второй раз не считаются -- в этой таблице помечены группами).", ""] + таблица(г_адр, "— нет сигналов")

    md += ["", "## Fomo (первый подписант -- сервер Fomo)", "",
           f"Архив ХРАНИТ подписанта транзакции (поле `txSigner`), поэтому счёт возможен: прогон шёл с "
           f"`--podpisant {FOMO}`, покупки с этим первым подписантом дают сигналы для кошельков из `breakdown`. "
           f"Кошельков Fomo с сигналами (и не входящих в наши списки): {len(фомо)}; с n ≥ {N_ОТБОР} ячеек: "
           f"{sum(1 for a in фомо if (ряды[a]['отбор']['108'].get('n') or 0) >= N_ОТБОР)}.", ""]
    фомо30 = [a for a in sorted(фомо, key=lambda a: -(ряды[a]['отбор']['108'].get('медиана') or -99))
              if (ряды[a]['отбор']['108'].get('n') or 0) >= N_ОТБОР][:30]
    md += таблица(фомо30, "— никто не дошёл до порога n")
    канд_фомо, кп = set(), П / "code1_snapshot" / "fomo_leaderboard_candidates.json"
    if кп.exists():
        кд = json.loads(кп.read_text(encoding="utf-8"))
        for ряд in (кд.get("final_table_sorted_by_followers") or []) + (кд.get("union_candidates") or []):
            а_ = (ряд or {}).get("solana_address")
            if а_:
                канд_фомо.add(а_)
    md += ["", f"Совпадения верхних 30 с data/fomo_leaderboard_candidates.json: "
           + (", ".join(f"`{a}`" for a in фомо30 if a in канд_фомо) or
              (f"нет (в лидерборде {len(канд_фомо)} адресов)" if канд_фомо
               else "файла лидерборда нет -- сверка не делалась")) + ".", ""]

    md += ["## Кусты (один источник, считать одним)", ""]
    md += [f"- `{п['a'][:8]}` и `{п['b'][:8]}`: общих (минт, слот) {п['общих']} из {п['n_a']} / {п['n_b']}"
           + (" -- оба прошли" if п["a"] in прошли and п["b"] in прошли else "") for п in д["кусты"]] or ["- нет"]

    md += ["", "## Прошли правило полностью (отбор и вне выборки), полными адресами", ""]
    md += [f"- `{a}` -- {', '.join(ряды[a]['группы']) or ('Fomo' if ряды[a]['fomo'] else 'GMGN')}; "
           f"отбор +108 {ф(ряды[a]['отбор']['108'])}, вне {ф(ряды[a]['вне']['108'])}"
           for a in sorted(прошли, key=lambda a: -(ряды[a]["отбор"]["108"].get("среднее") or -99))] or ["- никто"]
    md += ["", f"Прошли только отбор (вне выборки окна не хватило или числа не те): {len(только_отбор)}.", ""]
    md += [f"- `{a}` -- отбор +108 {ф(ряды[a]['отбор']['108'])}, вне {ф(ряды[a]['вне']['108'])}"
           for a in sorted(только_отбор, key=lambda a: -(ряды[a]["отбор"]["108"].get("среднее") or -99))][:60]
    md += ["", "Решение о группе -- владельца.", ""]
    out = КОРЕНЬ / "docs" / "podbivka_2026-10-02_kandidaty_cand2.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(out)
    print(out.name, "прошли", len(прошли), "только отбор", len(только_отбор), flush=True)
    return 0


if __name__ == "__main__":
    р = argparse.ArgumentParser()
    г = р.add_mutually_exclusive_group(required=True)
    г.add_argument("--otbor", action="store_true")
    г.add_argument("--svod", action="store_true")
    а = р.parse_args()
    raise SystemExit(отбор() if а.otbor else свод())
