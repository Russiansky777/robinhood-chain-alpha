#!/usr/bin/env python3
"""Правило кандидатов по заданным адресам -- офлайн, по ВСЕМ готовым суткам архива.

ЗАЧЕМ. Адрес мог попасть в прогон архива под любым семейством файлов (den_*, cand2_*, pyg*,
porog14_*, zakem_* и т.д.). Здесь берутся ВСЕ суточные файлы, сигналы склеиваются по подписи,
отбираются первые покупки от 2 SOL-экв и считается ровно то же правило, что в cand2 / cand3:
наш вход «конец слота» (S0_дно) за ним, билеты 0.1 и 0.3, выход +108, котировка WSOL,
кривая pump.fun / LaunchLab / CPMM / DAMM v1 плюс Pump AMM по модели v6, п.п. чистыми
(0.002 SOL на круг); n >= 20, среднее >= +2 п.п., медиана > 0, «толпа есть» (доля ячеек со
свопом пула в s0+1 / s0+2 >= 0.5) И стабильность (медиана > 0 в обеих половинах окна и не
меньше 60 % суток в плюсе) -- слово владельца 03.10.

Счёт ячеек и п.п. -- кодом podbivka_cand2 / podbivka_kandidaty_vne, без своей копии формул.
Вывод: строка на адрес в stdout и docs/podbivka_<дата>_pravilo_adres.md.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import statistics
import sys
import time
import calendar
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_cand2 as C2  # noqa: E402
import podbivka_kandidaty_vne as K  # noqa: E402
import podbivka_stabilnost as ST  # noqa: E402

КОРЕНЬ = C2.КОРЕНЬ
П = C2.П
N_МИН, СР_МИН, ТОЛПА_МИН = 20, 2.0, 0.5
ПОРОГ_SOL = 2.0


def окно_файла(с: dict, f: str) -> bool:
    """Сутки файла, первый «разгонный» час отброшен (как в окне отбора cand2)."""
    хвост = Path(f).name.replace(".json.gz", "").split("_")[-1]
    try:
        д0 = calendar.timegm(time.strptime(хвост[:13], "%Y-%m-%dT%H")) + 3600
    except ValueError:
        return False
    return д0 <= K.ts(с) < д0 + 86400


def собрать(файлы: list, цель: set) -> tuple[dict, dict]:
    """Копилки по целевым адресам: ячейки с временем (для стабильности) и толпа."""
    по = {a: {"сигналов": 0, "ячеек": 0, "никто": 0, "pump_amm": 0, "не_sol": 0,
              "108": [], "108_01": [], "файлы": set(), "первых": 0} for a in цель}
    счёт = {"файлов": 0, "сигналов": 0, "наших": 0, "дублей": 0, "ошибки": []}
    видел: set = set()
    for f in файлы:
        try:
            д = json.loads(gzip.decompress(Path(f).read_bytes()))
        except Exception as exc:  # noqa: BLE001
            счёт["ошибки"].append(f"{Path(f).name}: {type(exc).__name__}")
            continue
        счёт["файлов"] += 1
        for с in д.get("сигналы") or []:
            счёт["сигналов"] += 1
            if с.get("trader") not in по or not окно_файла(с, f):
                continue
            if с["signature"] in видел:
                счёт["дублей"] += 1
                continue
            видел.add(с["signature"])
            счёт["наших"] += 1
            к = по[с["trader"]]
            к["сигналов"] += 1
            if (с.get("sol") or 0) < ПОРОГ_SOL:
                continue
            к["первых"] += 1
            к["pump_amm"] += 1 if с.get("pool") == "pump-amm" else 0
            к["не_sol"] += 1 if с.get("quoteMint") != K.WSOL else 0
            if not C2.ячейка(с):
                continue
            к["ячеек"] += 1
            к["никто"] += 1 if C2.никто(с) else 0
            к["файлы"].add(Path(f).name.replace(".json.gz", ""))
            ts = K.ts(с)
            for кл in ("108", "108_01"):
                v = K.пп(с, кл)
                if v is not None:
                    к[кл].append((ts, v))
        del д
    return по, счёт


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--adresa", required=True, help="через запятую")
    р.add_argument("--metka", default="2026-10-03")
    а = р.parse_args()
    цель = {x for x in а.adresa.split(",") if x}
    файлы = sorted(glob.glob(str(П / "arhiv_den" / "*.json.gz")))
    по, счёт = собрать(файлы, цель)
    адр = json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    строки, ряды = [], {}
    for a in sorted(цель, key=lambda x: -(по[x]["ячеек"])):
        к = по[a]
        рез = {"группы": (адр.get(a) or {}).get("группы") or [], "сигналов": к["сигналов"],
               "первых_от_2": к["первых"], "ячеек": к["ячеек"],
               "толпа": round(1 - к["никто"] / к["ячеек"], 3) if к["ячеек"] else None,
               "pump_amm": к["pump_amm"], "не_sol": к["не_sol"],
               "суток_файлов": len(к["файлы"])}
        for кл, имя in (("108", "0.3"), ("108_01", "0.1")):
            зн = [v for _, v in к[кл]]
            s = K.стат(зн)
            ст = ST.стабильность(к[кл])
            прошёл = bool((s.get("n") or 0) >= N_МИН and (s.get("среднее") or -99) >= СР_МИН
                          and (s.get("медиана") or -99) > 0
                          and (рез["толпа"] or 0) >= ТОЛПА_МИН and ст["стабилен"])
            рез[имя] = {"стат": s, "стабильность": ст, "прошёл": прошёл}
        ряды[a] = рез
        for имя in ("0.3", "0.1"):
            v = рез[имя]
            s, ст = v["стат"], v["стабильность"]
            строки.append(
                f"`{a[:8]}` билет {имя}: n {s.get('n', 0)}, среднее "
                + (f"{s['среднее']:+.2f}" if s.get("n") else "—")
                + ", медиана " + (f"{s['медиана']:+.2f}" if s.get("n") else "—")
                + f", в плюсе " + (f"{s['в_плюсе']:.0%}" if s.get("n") else "—")
                + f", толпа {рез['толпа'] if рез['толпа'] is not None else '—'}"
                + f", половины {ст['медиана_1']} / {ст['медиана_2']}"
                + f", суток в плюсе {ст['суток_в_плюсе']} / {ст['суток']}"
                + f" -> {'ПРОШЁЛ' if v['прошёл'] else 'не прошёл'}")
    md = [f"# Правило кандидатов по адресам ({а.metka})", "",
          f"Файлов суток прочитано {счёт['файлов']}, сигналов в них {счёт['сигналов']}, наших сигналов в окнах "
          f"{счёт['наших']}, дублей подписи между семействами файлов {счёт['дублей']}, ошибок чтения "
          f"{len(счёт['ошибки'])}.", "",
          "Правило: вход «конец слота» за ним, выход +108, котировка WSOL, кривая pump.fun / LaunchLab / CPMM / "
          f"DAMM v1 плюс Pump AMM v6, п.п. чистыми; n ≥ {N_МИН}, среднее ≥ +{СР_МИН:g} п.п., медиана > 0, "
          f"толпа ≥ {ТОЛПА_МИН:g}, стабильность -- медиана > 0 в обеих половинах окна и ≥ 60 % суток в плюсе. "
          f"Первые покупки от {ПОРОГ_SOL:g} SOL-экв. Ничего не рекомендуется.", "",
          "| адрес | группы | сигналов | первых ≥2 SOL | ячеек | толпа | билет | n | среднее | медиана | "
          "в плюсе | медиана 1-й пол. | медиана 2-й пол. | суток в плюсе | правило |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for a, рез in ряды.items():
        for имя in ("0.3", "0.1"):
            v = рез[имя]
            s, ст = v["стат"], v["стабильность"]
            md.append(f"| `{a[:8]}` | {', '.join(рез['группы']) or '—'} | {рез['сигналов']} | "
                      f"{рез['первых_от_2']} | {рез['ячеек']} | "
                      + (f"{рез['толпа']:.2f}" if рез["толпа"] is not None else "—")
                      + f" | {имя} | {s.get('n', 0)} | "
                      + (f"{s['среднее']:+.2f}" if s.get("n") else "—") + " | "
                      + (f"{s['медиана']:+.2f}" if s.get("n") else "—") + " | "
                      + (f"{s['в_плюсе']:.0%}" if s.get("n") else "—") + " | "
                      + (f"{ст['медиана_1']:+.2f}" if ст["медиана_1"] is not None else "—") + " | "
                      + (f"{ст['медиана_2']:+.2f}" if ст["медиана_2"] is not None else "—") + " | "
                      + (f"{ст['суток_в_плюсе']} / {ст['суток']}" if ст["суток"] else "—")
                      + f" | **{'прошёл' if v['прошёл'] else 'не прошёл'}** |")
    out = КОРЕНЬ / "docs" / f"podbivka_{а.metka}_pravilo_adres.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(out)
    дт = П / f"pravilo_adres_{а.metka}.json"
    дт.write_text(json.dumps({"счёт": счёт, "ряды": ряды}, ensure_ascii=False, indent=1, default=str),
                  encoding="utf-8")
    R.записано(дт)
    for с in строки:
        print(с, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
