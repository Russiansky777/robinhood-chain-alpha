#!/usr/bin/env python3
"""Таблица масштаба: сигнал по источнику и по билету, минимальный выгодный билет, держится ли. Офлайн.

Вход:
  * выгрузки сделок Code-1 (копии в data/podbivka/sdelki/): живой итог по цепи на каждую сделку;
  * суточные файлы архива -- сигналы наших источников: zakem_*T16 и bilet_*T16 (есть состояния пула,
    поэтому любой билет считается офлайн), porog14_*T16 и cand2_*T16 (билеты только те, что были в прогоне).
Правило: покупка источника от 2 SOL-экв, наш вход «конец слота», выход +108, п.п. чистыми (0.002 SOL на круг),
котировка WSOL, Pump AMM по модели v6. «Держится» -- медиана > 0 в обеих половинах окна и не меньше 60 %
суток в плюсе при n ≥ 20; «неясно» -- n < 20; иначе «не держится».
Выход: docs/podbivka_2026-10-03_masshtab.md, data/podbivka/masshtab_svod.json.
"""
from __future__ import annotations

import collections
import glob
import gzip
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_bilet as B  # noqa: E402
import podbivka_cand2 as C2  # noqa: E402
import podbivka_porog_razmera as PR  # noqa: E402
import podbivka_stabilnost as ST  # noqa: E402

КОРЕНЬ = PR.КОРЕНЬ
П = PR.П
СЕТКА = (0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 3.0, 5.0)
ШАБЛОНЫ = ("zakem_*T16.json.gz", "bilet_*T16.json.gz", "porog14_*T16.json.gz", "cand2_*T16.json.gz",
            "cand3_*T16.json.gz", "blast3_*T16.json.gz", "kust_*T16.json.gz")
N_МИН = 20


def сделки() -> list:
    из_ = []
    for f in sorted((П / "sdelki").glob("sdelki_polosy_*.json")):
        д = json.loads(f.read_text(encoding="utf-8"))
        for r in д.get("ряды") or []:
            r["_файл"] = f.name
            из_.append(r)
    return из_


def архив(цель: set) -> tuple[dict, dict]:
    по: dict = {a: {"ячеек": 0, "сигналов": 0, "пп": {}, "сост": [], "файлы": set(), "ts": []} for a in цель}
    счёт = {"файлов": 0, "сигналов": 0, "дублей": 0, "ошибки": []}
    видел = set()
    for шаб in ШАБЛОНЫ:
        for f in sorted(x for x in glob.glob(str(П / "arhiv_den" / шаб)) if "_vne_" not in Path(x).name):
            д = json.loads(gzip.decompress(Path(f).read_bytes()))
            счёт["файлов"] += 1
            счёт["ошибки"] += [f"{Path(f).name}: {x}" for x in (д.get("счёт") or {}).get("ошибки") or []]
            for с in д.get("сигналы") or []:
                if с.get("trader") not in по or not PR.окно(с, f):
                    continue
                счёт["сигналов"] += 1
                if с["signature"] in видел:
                    счёт["дублей"] += 1
                    continue
                видел.add(с["signature"])
                if (с.get("sol") or 0) < 2.0:
                    continue
                к = по[с["trader"]]
                к["сигналов"] += 1
                if not C2.ячейка(с):
                    continue
                к["ячеек"] += 1
                к["файлы"].add(Path(f).name.split("_")[0])
                ts = PR.K.ts(с)
                к["ts"].append(ts)
                for б in (0.1, 0.3, 0.5, 1.0, 3.0):
                    v = B.пп(с, б)
                    if v is not None:
                        к["пп"].setdefault(б, []).append((ts, v))
                if ((с.get("модель") or {}).get("состояния") or {}).get("вход"):
                    к["сост"].append(с)
            del д
    return по, счёт


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    сд = сделки()
    живые = [r for r in сд if r.get("итог_po_cepi_sol") is not None and r.get("sol_in")]
    источники = {r["source"] for r in сд if r.get("source")}
    по, счёт = архив(источники)
    реестр = json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]

    ряды = {}
    for a in источники:
        сд_а = [r for r in живые if r.get("source") == a]
        пп_ж = [100 * r["итог_po_cepi_sol"] / r["sol_in"] for r in сд_а]
        к = по[a]
        по_билету = {}
        for б, сп in к["пп"].items():
            s = PR.стат([v for _, v in сп])
            по_билету[f"{б:g}"] = s
        сетка = {}
        for б in СЕТКА:
            зн = [B.пп_по_состояниям(с, б) for с in к["сост"]]
            зн = [v for v in зн if v is not None]
            if зн:
                сетка[f"{б:g}"] = {"n": len(зн), "медиана": round(statistics.median(зн), 2),
                                   "среднее": round(statistics.mean(зн), 2)}
        мин = next((б for б in СЕТКА if (сетка.get(f"{б:g}") or {}).get("медиана", -99) > 0), None)
        осн = к["пп"].get(0.3) or к["пп"].get(0.1) or []
        ст = ST.стабильность(осн)
        n = len(осн)
        вердикт = ("неясно" if n < N_МИН else ("держится" if ст["стабилен"] else "не держится"))
        ряды[a] = {"группы": [x for x in ((реестр.get(a) or {}).get("группы") or []) if x not in ("543", "133")],
                   "сделок": len(сд_а), "билеты_живые": sorted({r.get("size_sol") for r in сд_а}),
                   "живой_итог": PR.стат(пп_ж),
                   "живой_медиана_sol": round(statistics.median([r["итог_po_cepi_sol"] for r in сд_а]), 4) if сд_а else None,
                   "архив_сигналов": к["сигналов"], "архив_ячеек": к["ячеек"],
                   "файлы": sorted(к["файлы"]), "с_состояниями": len(к["сост"]),
                   "по_билету": по_билету, "сетка": сетка, "мин_билет": мин,
                   "стабильность": ст, "вердикт": вердикт}

    поряд = sorted(ряды, key=lambda a: -ряды[a]["сделок"])
    пп_всё = [100 * r["итог_po_cepi_sol"] / r["sol_in"] for r in живые]
    по_билету_живые: dict = {}
    for r in живые:
        по_билету_живые.setdefault(r.get("size_sol"), []).append(100 * r["итог_po_cepi_sol"] / r["sol_in"])

    md = ["# Подбивка: таблица масштаба -- сигнал по источнику и по билету, минимальный выгодный билет", "",
          f"Живые сделки -- выгрузки Code-1 ({', '.join(sorted({r['_файл'] for r in сд}))}, копии в "
          f"`data/podbivka/sdelki/`): всего {len(сд)} сделок, с итогом по цепи {len(живые)}. Архив -- сигналы "
          f"тех же источников по файлам {', '.join(ШАБЛОНЫ)}: суток {счёт['файлов']}, сигналов источников "
          f"{счёт['сигналов']}, дублей {счёт['дублей']}, ошибок чтения часов {len(счёт['ошибки'])}.", "",
          "Живой итог сделки -- `итог_po_cepi_sol` выгрузки (сумма изменений всех наших счетов по двум "
          "подписям), в п.п. к `sol_in`: там уже и чаевые, и приоритет, и комиссия. Архивный сигнал -- модель "
          "архива: вход «конец слота» за источником, выход +108, п.п. чистыми (0.002 SOL на круг). "
          "«Минимальный выгодный билет» -- наименьший билет сетки "
          + ", ".join(f"{б:g}" for б in СЕТКА) + ", у которого медиана архивного сигнала выше нуля (считается "
          "по состояниям пула, формула сверена с моделью до нуля). Ничего не рекомендуется.", "",
          "## Итог по всем сделкам выгрузки", "",
          f"- Сделок с итогом по цепи **{len(живые)}**, медиана **{statistics.median(пп_всё):+.2f} п.п.**, "
          f"среднее {statistics.mean(пп_всё):+.2f}, в плюсе {sum(1 for v in пп_всё if v > 0)} из {len(пп_всё)}.", ""]
    md += ["| билет живой | сделок | медиана, п.п. | среднее, п.п. | в плюсе |", "|---|---|---|---|---|"]
    for б, v in sorted(по_билету_живые.items(), key=lambda kv: (kv[0] or 0)):
        md.append(f"| {б} | {len(v)} | {statistics.median(v):+.2f} | {statistics.mean(v):+.2f} | "
                  f"{sum(1 for x in v if x > 0)} / {len(v)} |")
    md += ["", "## По источникам", "",
           "| источник | группы | сделок (билеты) | живой итог: медиана / среднее / в плюсе | архив: ячеек | "
           "сигнал 0.3 | сигнал 0.5 | сигнал 1 | сигнал 3 | мин. выгодный билет | держится |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in поряд:
        v = ряды[a]
        ж = v["живой_итог"]
        def кл(б: str) -> str:
            s = v["сетка"].get(б) or v["по_билету"].get(б)
            if not s:
                return "—"
            return (f"{s['медиана']:+.2f}" if (s.get("n") or 0) >= N_МИН else f"n={s.get('n')}")
        md.append(f"| `{a[:8]}` | {', '.join(v['группы']) or '—'} | {v['сделок']} "
                  f"({', '.join(f'{x:g}' for x in v['билеты_живые'] if x is not None)}) | "
                  + (f"{ж['медиана']:+.2f} / {ж['среднее']:+.2f} / {ж['в_плюсе']:.0%}" if ж.get("n") else "—")
                  + f" | {v['архив_ячеек']} | " + " | ".join(кл(б) for б in ("0.3", "0.5", "1", "3"))
                  + f" | {v['мин_билет'] if v['мин_билет'] else '—'} | **{v['вердикт']}** |")
    md += ["", "### Стабильность по источникам (основной билет)", "",
           "| источник | n | медиана 1-й половины | медиана 2-й половины | суток в плюсе | вердикт |",
           "|---|---|---|---|---|---|"]
    for a in поряд:
        v, ст = ряды[a], ряды[a]["стабильность"]
        md.append(f"| `{a[:8]}` | {ст['n']} | "
                  + (f"{ст['медиана_1']:+.2f}" if ст["медиана_1"] is not None else "—") + " | "
                  + (f"{ст['медиана_2']:+.2f}" if ст["медиана_2"] is not None else "—") + " | "
                  + (f"{ст['суток_в_плюсе']} / {ст['суток']}" if ст["суток"] else "—")
                  + f" | {v['вердикт']} |")
    нет_сост = [a for a in поряд if not ряды[a]["с_состояниями"]]
    md += ["", f"Источников без состояний пула в файлах (минимальный билет и сетка по ним не посчитаны): "
           f"**{len(нет_сост)}** из {len(поряд)}"
           + (" -- " + ", ".join(f"`{a[:8]}`" for a in нет_сост[:15]) if нет_сост else "")
           + ". По ним столбцы сигнала стоят только там, где билет был в самом прогоне "
             "(0.1 / 0.3 / 0.5). Как закончится проход `zakem` (11 суток, все 55 источников, состояния) -- "
             "пересчитаю всю таблицу по сетке.", ""]
    out = КОРЕНЬ / "docs" / "podbivka_2026-10-03_masshtab.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(out)
    дт = П / "masshtab_svod.json"
    дт.write_text(json.dumps({"счёт": счёт, "сделок": len(сд), "с_итогом": len(живые),
                              "ряды": {a: {k: v for k, v in ряды[a].items()} for a in поряд}},
                             ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    R.записано(дт)
    держ = sum(1 for a in поряд if ряды[a]["вердикт"] == "держится")
    print(out.name, "источников", len(поряд), "держится", держ,
          "не держится", sum(1 for a in поряд if ряды[a]["вердикт"] == "не держится"),
          "неясно", sum(1 for a in поряд if ряды[a]["вердикт"] == "неясно"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
