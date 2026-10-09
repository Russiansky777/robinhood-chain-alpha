#!/usr/bin/env python3
"""Пункт 2 задачи: можно ли продать в срезе createPool и кто двигает цену.

Четыре вопроса владельца и чем на каждый отвечается:

  а) токен -- Token или Token-2022, mint и freeze authority, расширения. Только по цепи:
     читается data/podbivka/pereezd_token_*.json (прогон podbivka_pereezd_token.py).
  б) создатель -- один кошелёк на много пулов? покупает ли сам? Сосредоточенность и
     покупка в блоке створа считаются по собранным суткам; откуда минт -- по цепи из того
     же файла; добавляет ли ликвидность -- по прицельному проходу окна.
  в) продажи не создателем -- по прицельному проходу окна
     (data/podbivka/pereezd_okno_*.json.gz, прогон podbivka_pereezd_okno.py).
  г) рост от свопов или от ликвидности -- по собранным суткам. Своп сохраняет
     произведение резервов (k = x*y растёт только на тариф), а изменение ликвидности
     двигает оба плеча в одну сторону и k вместе с ними. Поэтому k = x1*y1/(x0*y0)
     разделяет два случая без всякой сети.

Только чтение собранных файлов. Выход: docs/podbivka_pereezd_mehanizm.md и
data/podbivka/pereezd_mehanizm.json.
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
СЕГМЕНТЫ = {("pump-amm", "создание:migrate"): "а",
            ("meteora-damm-v2", "создание:migrate"): "б",
            ("pump-amm", "создание:createPool"): "в1",
            ("meteora-damm-v2", "создание:createPool"): "в2"}
ИМЕНА = {"а": "(а) переезд pump.fun", "б": "(б) переезд Meteora DBC",
         "в1": "(в) createPool pump-amm", "в2": "(в) createPool DAMM v2"}
ГОРИЗОНТЫ = ("224", "230", "448", "1121")


def кв(v: list, q: float):
    s = sorted(v)
    return s[max(0, min(len(s) - 1, int(q * (len(s) - 1))))] if s else None


def по_суткам() -> dict:
    """Сосредоточенность создателей и источник роста -- по всем собранным суткам."""
    тв: dict = collections.defaultdict(collections.Counter)
    k_от: dict = collections.defaultdict(lambda: collections.defaultdict(list))
    счёт: dict = collections.defaultdict(collections.Counter)
    for ф in sorted(glob.glob(str(П / "zapuski" / "dolgo_*.json.gz"))):
        with gzip.open(ф, "rt", encoding="utf-8") as о:
            тело = json.load(о)
        for x in тело.get("ряды") or []:
            с = СЕГМЕНТЫ.get((x.get("пул"), x.get("правило")))
            if not с or x.get("quoteMint") != WSOL:
                continue
            вх = (x.get("вход") or {}).get("створ_дно")
            if not вх or float(вх[0]) < 10.0:
                continue
            счёт[с]["пулов"] += 1
            тв[с][x.get("создатель")] += 1
            if (x.get("в_блоке_создания") or {}).get("создатель_купил"):
                счёт[с]["создатель_купил_в_створе"] += 1
            if (x.get("продаж") or 0) > 0:
                счёт[с]["есть_продажи_в_окне"] += 1
            соз = x.get("создатель")
            пк = x.get("покупатели") or []
            if any(к.get("кто") and к["кто"] != соз for к in пк):
                счёт[с]["есть_покупатель_не_создатель"] += 1
            if any(к.get("кто") == соз for к in пк):
                счёт[с]["создатель_среди_верхних_покупателей"] += 1
            x0, y0 = float(вх[0]), float(вх[1])
            for h in ГОРИЗОНТЫ:
                v = (x.get("выход") or {}).get(h)
                if not v:
                    continue
                x1, y1 = float(v[0]), float(v[1])
                k_от[с][h].append(0.0 if (x1 <= 0 or y1 <= 0) else (x1 * y1) / (x0 * y0))
    из_ = {}
    for с in СЕГМЕНТЫ.values():
        c = тв[с]
        всего = sum(c.values()) or 1
        верх = c.most_common(5)
        из_[с] = {
            "пулов": счёт[с]["пулов"],
            "создателей": len(c), "пулов_на_создателя": round(всего / max(1, len(c)), 2),
            "верхние_5_доля": round(100 * sum(v for _, v in верх) / всего, 1),
            "верхние": [{"кошелёк": k, "пулов": v, "доля": round(100 * v / всего, 1)}
                        for k, v in верх],
            "создателей_с_одним_пулом_доля": round(
                100 * sum(1 for v in c.values() if v == 1) / max(1, len(c)), 1),
            **{f"{имя}_доля": round(100 * счёт[с][имя] / max(1, счёт[с]["пулов"]), 1)
               for имя in ("создатель_купил_в_створе", "есть_продажи_в_окне",
                           "есть_покупатель_не_создатель",
                           "создатель_среди_верхних_покупателей")},
            "k": {h: {"медиана": round(statistics.median(v), 4),
                      "p10": round(кв(v, 0.10), 4), "p90": round(кв(v, 0.90), 4),
                      "закрыт_доля": round(100 * sum(1 for q in v if q == 0) / len(v), 1),
                      "ликвидность_ушла_доля": round(
                          100 * sum(1 for q in v if q < 0.5) / len(v), 1),
                      "n": len(v)}
                  for h, v in sorted(k_от[с].items()) if v},
        }
    return из_


def по_цепи() -> dict:
    """Счета минта и пула, расширения и исток -- из прогона podbivka_pereezd_token.py."""
    пути = sorted(glob.glob(str(П / "pereezd_token_*.json")))
    if not пути:
        return {}
    д = json.loads(Path(пути[-1]).read_text(encoding="utf-8"))
    ряды = д.get("ряды") or []
    по_сег: dict = collections.defaultdict(lambda: collections.Counter())
    for r in ряды:
        с = "в2" if r.get("пул") == "meteora-damm-v2" else "в1"
        м = r.get("минт_счёт") or {}
        c = по_сег[с]
        c["пулов"] += 1
        c[f"программа:{м.get('программа')}"] += 1
        c["mint_authority_есть"] += 1 if м.get("mint_authority") else 0
        c["freeze_authority_есть"] += 1 if м.get("freeze_authority") else 0
        if м.get("опасные"):
            c["опасные_расширения"] += 1
            for и in м["опасные"]:
                c[f"расширение:{и}"] += 1
        for пл in ((r.get("исток") or {}).get("создание_площадки") or []):
            c[f"исток:{пл}"] += 1
        if (r.get("исток") or {}).get("обрезано"):
            c["исток:обрезано"] += 1
    return {"файл": Path(пути[-1]).name, "свод_прогона": д.get("свод") or {},
            "по_сегментам": {k: dict(v) for k, v in по_сег.items()}}


def по_окну() -> dict:
    """Продажи не создателем, добавление ликвидности и партии выкупа -- из окна."""
    пути = sorted(glob.glob(str(П / "pereezd_okno_*.json.gz")))
    if not пути:
        return {}
    with gzip.open(пути[-1], "rt", encoding="utf-8") as о:
        д = json.load(о)
    из_: dict = collections.defaultdict(lambda: collections.Counter())
    партии: list = []
    for п in д.get("пулы") or []:
        соз = п.get("создатель")
        сег = ("в2" if п.get("пул") == "meteora-damm-v2" and
               п.get("правило") == "создание:createPool"
               else "в1" if п.get("правило") == "создание:createPool"
               else "а" if п.get("пул") == "pump-amm" else "б")
        c = из_[сег]
        c["пулов"] += 1
        соб = п.get("события") or []
        прод = [e for e in соб if e.get("действие") == "sell"]
        пок = [e for e in соб if e.get("действие") == "buy"]
        ликв = [e for e in соб if e.get("действие") in ("add", "remove", "deposit",
                                                        "withdraw", "addLiquidity",
                                                        "removeLiquidity")]
        c["событий"] += len(соб)
        c["есть_продажи"] += 1 if прод else 0
        c["есть_продажа_не_создателя"] += 1 if any(e.get("кто") != соз for e in прод) else 0
        c["создатель_продавал"] += 1 if any(e.get("кто") == соз for e in прод) else 0
        c["создатель_покупал"] += 1 if any(e.get("кто") == соз for e in пок) else 0
        c["есть_событие_ликвидности"] += 1 if ликв else 0
        c["создатель_менял_ликвидность"] += 1 if any(e.get("кто") == соз
                                                     for e in ликв) else 0
        c["продаж"] += len(прод)
        c["покупок"] += len(пок)
        c["продаж_не_создателя"] += sum(1 for e in прод if e.get("кто") != соз)
        # Партии выкупа: по каждому кошельку -- сколько покупок и в каких слотах.
        if сег == "а" and пок:
            по_кош: dict = collections.defaultdict(list)
            for e in пок:
                if e.get("кто"):
                    по_кош[e["кто"]].append((e["блок"] - (п.get("блок") or e["блок"]),
                                             e.get("кв") or 0.0))
            луч = max(по_кош.items(), key=lambda kv: len(kv[1]), default=None)
            if луч and len(луч[1]) >= 5:
                сл = sorted(s for s, _ in луч[1])
                шаги = [b - a for a, b in zip(сл, сл[1:])]
                партии.append({"poolId": п["poolId"], "кошелёк": луч[0],
                               "партий": len(сл), "первый_слот": сл[0],
                               "последний_слот": сл[-1],
                               "шаг_медиана": statistics.median(шаги) if шаги else None,
                               "sol": round(sum(v for _, v in луч[1]), 4),
                               "кошельков_в_пуле": len(по_кош)})
    св = {}
    if партии:
        св = {"пулов": len(партии),
              "партий_медиана": statistics.median([x["партий"] for x in партии]),
              "первый_слот_медиана": statistics.median([x["первый_слот"] for x in партии]),
              "последний_слот_медиана": statistics.median([x["последний_слот"]
                                                          for x in партии]),
              "шаг_слотов_медиана": statistics.median([x["шаг_медиана"] for x in партии
                                                       if x["шаг_медиана"] is not None]),
              "sol_медиана": round(statistics.median([x["sol"] for x in партии]), 3),
              "sol_среднее": round(statistics.fmean([x["sol"] for x in партии]), 3),
              "кошельков_разных": len({x["кошелёк"] for x in партии})}
    return {"файл": Path(пути[-1]).name, "счёт": д.get("счёт") or {},
            "часы": д.get("часы") or [], "по_сегментам": {k: dict(v) for k, v in из_.items()},
            "выкуп_по_цепи_архива": св, "партии": партии[:30]}


def доля(c: dict, имя: str, из_чего: str = "пулов"):
    всего = c.get(из_чего) or 0
    return None if not всего else round(100 * (c.get(имя) or 0) / всего, 1)


def страница(т: dict) -> None:
    ф = lambda v, f=".1f": "--" if v is None else format(v, f)       # noqa: E731
    л = ["# Переезд, пункт 2: можно ли продать в срезе createPool и кто двигает цену", "",
         "Срез createPool -- не переезд: на 35 прочитанных створах нет ни программы "
         "pump.fun, ни программы Meteora DBC. Значит ни выкупа BOOST, ни понятного "
         "источника роста там заранее нет, и прежде денег нужен механизм.", ""]
    с = т.get("по_суткам") or {}
    л += ["## б) Создатель пула: один кошелёк на много пулов?", "",
          "| сегмент | пулов | создателей | пулов на создателя | верхние 5 держат | у создателя один пул | создатель купил в створе | есть продажи в окне | есть покупатель не создатель |",
          "|---|---|---|---|---|---|---|---|---|"]
    for k in ("а", "б", "в1", "в2"):
        v = с.get(k)
        if not v:
            continue
        л.append(f"| {ИМЕНА[k]} | {v['пулов']} | {v['создателей']} | "
                 f"{v['пулов_на_создателя']} | {v['верхние_5_доля']} % | "
                 f"{v['создателей_с_одним_пулом_доля']} % | "
                 f"{v['создатель_купил_в_створе_доля']} % | "
                 f"{v['есть_продажи_в_окне_доля']} % | "
                 f"{v['есть_покупатель_не_создатель_доля']} % |")
    л += ["", "Верхние создатели по сегментам:", ""]
    for k in ("а", "б", "в1", "в2"):
        v = с.get(k)
        if not v:
            continue
        л.append(f"* {ИМЕНА[k]}: " + ", ".join(
            f"`{x['кошелёк'][:8]}...` {x['пулов']} пулов ({x['доля']} %)"
            for x in v["верхние"]))
    л += ["", "## г) Рост цены в окне: от свопов или от изменения ликвидности", "",
          "Своп сохраняет произведение резервов: k = x1*y1/(x0*y0) остаётся около единицы "
          "и растёт только на тариф. Добавление или вывод ликвидности двигают оба плеча в "
          "одну сторону, и k уходит вместе с ними. k около нуля -- пул опустошён, продать "
          "в нём нельзя ни по какой цене.", "",
          "| сегмент | удержание | k медиана | k p10 | k p90 | ликвидность ушла (k < 0.5) | пул закрыт (k = 0) | пулов |",
          "|---|---|---|---|---|---|---|---|"]
    for k in ("а", "б", "в1", "в2"):
        v = (с.get(k) or {}).get("k") or {}
        for h in ("224", "230", "448", "1121"):
            if h not in v:
                continue
            x = v[h]
            л.append(f"| {ИМЕНА[k]} | {h} сл | {x['медиана']} | {x['p10']} | {x['p90']} | "
                     f"{x['ликвидность_ушла_доля']} % | {x['закрыт_доля']} % | {x['n']} |")
    ц = т.get("по_цепи") or {}
    л += ["", "## а) Токен прошедшего среза по цепи", ""]
    if not ц:
        л += ["Прогон по цепи (`podbivka_pereezd_token.py`) ещё не лёг -- раздел пуст.", ""]
    else:
        л += [f"Файл `{ц['файл']}`.", "",
              "| сегмент | пулов | Token | Token-2022 | mint authority есть | freeze authority есть | опасные расширения |",
              "|---|---|---|---|---|---|---|"]
        for k in ("в1", "в2"):
            c = (ц.get("по_сегментам") or {}).get(k)
            if not c:
                continue
            л.append(f"| {ИМЕНА[k]} | {c.get('пулов')} | {c.get('программа:Token', 0)} | "
                     f"{c.get('программа:Token-2022', 0)} | "
                     f"{c.get('mint_authority_есть', 0)} | "
                     f"{c.get('freeze_authority_есть', 0)} | "
                     f"{c.get('опасные_расширения', 0)} |")
        л += ["", "Расширения и истоки по прогону: "
              f"`{json.dumps(ц.get('свод_прогона') or {}, ensure_ascii=False)}`", ""]
    о = т.get("по_окну") or {}
    л += ["## в) Продажи в окне: кто и успешно ли", ""]
    if not о:
        л += ["Прицельный проход окна (`podbivka_pereezd_okno.py`) ещё не лёг -- "
              "раздел пуст.", ""]
    else:
        л += [f"Файл `{о['файл']}`, часов архива {len(о.get('часы') or [])}.", "",
              "| сегмент | пулов | есть продажи | есть продажа НЕ создателя | создатель продавал | создатель покупал | менял ликвидность | продаж на пул | из них не создателя |",
              "|---|---|---|---|---|---|---|---|---|"]
        for k in ("а", "б", "в1", "в2"):
            c = (о.get("по_сегментам") or {}).get(k)
            if not c:
                continue
            л.append(f"| {ИМЕНА[k]} | {c['пулов']} | {ф(доля(c,'есть_продажи'))} % | "
                     f"{ф(доля(c,'есть_продажа_не_создателя'))} % | "
                     f"{ф(доля(c,'создатель_продавал'))} % | "
                     f"{ф(доля(c,'создатель_покупал'))} % | "
                     f"{ф(доля(c,'создатель_менял_ликвидность'))} % | "
                     f"{c['продаж'] / max(1, c['пулов']):.0f} | "
                     f"{100 * c['продаж_не_создателя'] / max(1, c['продаж']):.1f} % |")
        св = о.get("выкуп_по_цепи_архива") or {}
        if св:
            л += ["", "### к) Партии выкупа по полному окну (сегмент а)", "",
                  f"Пулов {св['пулов']}, кошельков разных {св['кошельков_разных']}; "
                  f"первая партия на слоте {св['первый_слот_медиана']}, последняя "
                  f"{св['последний_слот_медиана']}, партий {св['партий_медиана']}, шаг "
                  f"{св['шаг_слотов_медиана']} слотов, сумма {св['sol_медиана']} SOL "
                  f"медиана при {св['sol_среднее']} среднем (заявлено 17.6 SOL).", ""]
    (КОРЕНЬ / "docs" / "podbivka_pereezd_mehanizm.md").write_text("\n".join(л) + "\n",
                                                                  encoding="utf-8")


def main() -> int:
    argparse.ArgumentParser().parse_args()
    т = {"что": "пункт 2: механизм среза createPool", "по_суткам": по_суткам(),
         "по_цепи": по_цепи(), "по_окну": по_окну()}
    (П / "pereezd_mehanizm.json").write_text(json.dumps(т, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    страница(т)
    print("готово: docs/podbivka_pereezd_mehanizm.md", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
