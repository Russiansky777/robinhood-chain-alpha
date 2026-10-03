#!/usr/bin/env python3
"""Поиск похожих на стабильных: вселенная архива, ведущие 200, профиль и «за кем идут наши». Офлайн.

Слово владельца 03.10. Правило отбора -- кандидатское плюс стабильность:
n ≥ 20 первых покупок источника от 2 SOL-экв, среднее ≥ +2 п.п., медиана > 0, «толпа есть» (доля ячеек со
свопом пула в s0+1 / s0+2 ≥ 0.5) И стабильность (`podbivka_stabilnost`): медиана > 0 в обеих половинах окна
и не меньше 60 % суток в плюсе. Кусты -- одним адресом. Последние сутки окна -- вне выборки отдельно.

Разделы (каждый считается, когда легли его файлы архива; состояние -- data/podbivka/poisk_svod.json):
  --zakem        за кем идут наши: по полю «перед» сигналов наших источников (zakem_*T16, --pered 10);
  --vselennaya   сито всей вселенной (vsel_*T16) -> короткий список для шага 2;
  --ved200       ведущие верхних 200 копировщиков через правило со стабильностью (ved200_*T16);
  --profil       профиль 6qudAN2k / DYAn4XpA и кто из прошедших к нему ближе.
Страница -- docs/podbivka_2026-10-03_poisk_stabilnyh.md, строится из состояния каждый раз.
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

import podbivka_cand2 as C2  # noqa: E402
import podbivka_porog_razmera as PR  # noqa: E402
import podbivka_stabilnost as ST  # noqa: E402

КОРЕНЬ = PR.КОРЕНЬ
П = PR.П
N_МИН, СР_МИН, ТОЛПА_МИН = 20, 2.0, 0.5
ПОВТОР_МИН = 5
СОСТ = П / "poisk_svod.json"
СТРАНИЦА = КОРЕНЬ / "docs" / "podbivka_2026-10-03_poisk_stabilnyh.md"


def состояние() -> dict:
    return json.loads(СОСТ.read_text(encoding="utf-8")) if СОСТ.exists() else {}


def файлы(шаблон: str) -> list:
    return sorted(x for x in glob.glob(str(П / "arhiv_den" / шаблон)) if "_vne_" not in Path(x).name)


def имена_наших() -> dict:
    реестр = json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    return {a: ", ".join(x for x in (v or {}).get("группы") or [] if x not in ("543", "133"))
            for a, v in реестр.items()}


def закем() -> dict:
    """Кто покупал тот же пул за 1…10 слотов до наших источников -- и кто делает это повторно."""
    ф = файлы("zakem_*T16.json.gz")
    если = {"файлов": len(ф), "сигналов": 0, "сигналов_с_перед": 0, "ошибки": []}
    if not ф:
        return {**если, "why_not": "файлов zakem_*T16 ещё нет"}
    перед: dict = {}
    по_нашим: dict = {}
    видел = set()
    for f in ф:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        если["ошибки"] += [f"{Path(f).name}: {x}" for x in (д.get("счёт") or {}).get("ошибки") or []]
        for с in д.get("сигналы") or []:
            if not PR.окно(с, f) or с["signature"] in видел:
                continue
            видел.add(с["signature"])
            если["сигналов"] += 1
            сп = с.get("перед") or []
            if сп:
                если["сигналов_с_перед"] += 1
            наш = с["trader"]
            н = по_нашим.setdefault(наш, {"сигналов": 0, "с_перед": 0, "перед": collections.Counter()})
            н["сигналов"] += 1
            н["с_перед"] += 1 if сп else 0
            for x in сп:
                кто = x.get("кто")
                if not кто or кто == наш:
                    continue
                p = перед.setdefault(кто, {"раз": 0, "наши": collections.Counter(), "слотов": [], "sol": []})
                p["раз"] += 1
                p["наши"][наш] += 1
                p["слотов"].append(x.get("слотов"))
                if x.get("sol") is not None:
                    p["sol"].append(x["sol"])
                н["перед"][кто] += 1
        del д
    ряды = {}
    for a, p in перед.items():
        ряды[a] = {"раз": p["раз"], "наших_источников": len(p["наши"]),
                   "медиана_слотов": round(statistics.median(p["слотов"]), 1) if p["слотов"] else None,
                   "медиана_sol": round(statistics.median(p["sol"]), 4) if p["sol"] else None,
                   "наши": p["наши"].most_common(5)}
    повтор = sorted((a for a, v in ряды.items() if v["раз"] >= ПОВТОР_МИН), key=lambda a: -ряды[a]["раз"])
    (П / "zakem_povtor_adresa.json").write_text(json.dumps(
        {"что": f"адреса, стоящие перед нашими источниками не меньше {ПОВТОР_МИН} раз (поле «перед», s0-10…s0-1)",
         "откуда": "data/podbivka/arhiv_den/zakem_*T16.json.gz (--pered 10), 55 наших источников",
         "адреса": повтор[:400]}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {**если, "адресов_перед": len(ряды), "повторяющихся": len(повтор),
            "ряды": {a: ряды[a] for a in повтор[:200]},
            "по_нашим": {a: {"сигналов": v["сигналов"], "с_перед": v["с_перед"],
                             "перед": v["перед"].most_common(5)} for a, v in по_нашим.items()}}


def по_правилу(шаблон: str, ключ_билета: str = "108") -> dict:
    """Правило кандидатов плюс стабильность по суточным файлам одного прогона."""
    ф = файлы(шаблон)
    если = {"файлов": len(ф), "сигналов": 0, "ошибки": []}
    if not ф:
        return {**если, "why_not": f"файлов {шаблон} ещё нет"}
    по: dict = {}
    видел = set()
    for f in ф:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        если["ошибки"] += [f"{Path(f).name}: {x}" for x in (д.get("счёт") or {}).get("ошибки") or []]
        for с in д.get("сигналы") or []:
            if not PR.окно(с, f) or с["signature"] in видел:
                continue
            видел.add(с["signature"])
            если["сигналов"] += 1
            к = по.setdefault(с["trader"], {"сигналов": 0, "ячеек": 0, "никто": 0, "пп": [], "минт_слот": set()})
            к["сигналов"] += 1
            if C2.ячейка(с):
                v = C2.K.пп(с, ключ_билета)
                к["ячеек"] += 1
                к["никто"] += 1 if C2.никто(с) else 0
                к["пп"].append((PR.K.ts(с), v))
                к["минт_слот"].add((с.get("mint"), с.get("block")))
        del д
    ряды = {}
    for a, к in по.items():
        зн = [v for _, v in к["пп"]]
        s = PR.стат(зн)
        толпа = round(1 - к["никто"] / к["ячеек"], 3) if к["ячеек"] else None
        ст = ST.стабильность(к["пп"])
        прошёл = bool((s.get("n") or 0) >= N_МИН and (s.get("среднее") or -99) >= СР_МИН
                      and (s.get("медиана") or -99) > 0 and (толпа or 0) >= ТОЛПА_МИН and ст["стабилен"])
        ряды[a] = {"сигналов": к["сигналов"], "ячеек": к["ячеек"], "толпа": толпа, "итог": s,
                   "стабильность": ст, "прошёл": прошёл, "минт_слот": len(к["минт_слот"])}
    прошли = [a for a, v in ряды.items() if v["прошёл"]]
    # кусты: общих (минт, слот) не меньше половины сигналов -- оставляем того, у кого больше n
    пары, убрать = [], set()
    мс = {a: {(m, b) for (m, b) in по[a]["минт_слот"]} for a in прошли}
    for i, a in enumerate(прошли):
        for b in прошли[i + 1:]:
            общ = len(мс[a] & мс[b])
            if общ and 2 * общ >= min(len(мс[a]), len(мс[b])):
                пары.append([a, b, общ])
                худ = min((a, b), key=lambda x: (ряды[x]["итог"].get("n") or 0))
                убрать.add(худ)
    список = sorted((a for a in прошли if a not in убрать),
                    key=lambda a: -(ряды[a]["итог"].get("медиана") or -99))
    return {**если, "адресов": len(ряды), "прошли": len(прошли), "кустов": len(пары),
            "убрано_кустом": len(убрать), "список": список,
            "ряды": {a: ряды[a] for a in (список + [x for x in прошли if x in убрать])[:300]}}


def вселенная() -> dict:
    ф = файлы("vsel_*T16.json.gz")
    если = {"файлов": len(ф)}
    if not ф:
        return {**если, "why_not": "файлов vsel_*T16 ещё нет"}
    счёт: dict = collections.Counter()
    сутки = 0
    всего = 0
    for f in ф:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        сутки += 1
        всего = max(всего, д.get("кошельков_всего") or 0)
        for a, n in (д.get("вселенная") or {}).items():
            счёт[a] += n
        del д
    короткий = [a for a, n in счёт.items() if n >= N_МИН]
    короткий.sort(key=lambda a: -счёт[a])
    (П / "vselennaya_korotkiy.json").write_text(json.dumps(
        {"что": f"короткий список вселенной: не меньше {N_МИН} первых покупок от 2 SOL за окно",
         "откуда": "data/podbivka/arhiv_den/vsel_*T16.json.gz (режим --vselennaya)",
         "кошельков": len(короткий), "адреса": короткий}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {**если, "суток": сутки, "кошельков_в_сутках_макс": всего, "кошельков_в_сите": len(счёт),
            "короткий_список": len(короткий), "верх": [[a, счёт[a]] for a in короткий[:30]]}


def профиль() -> dict:
    """Профиль 6qudAN2k / DYAn4XpA по событиям: покупок в сутки, размер, типы, удержание, доля продаж."""
    ф = файлы("bilet_*T16.json.gz") or файлы("kust_*T16.json.gz")
    если = {"файлов": len(ф)}
    if not ф:
        return {**если, "why_not": "файлов bilet_*T16 / kust_*T16 ещё нет"}
    цель = set(json.loads((П / "bilet_adresa.json").read_text(encoding="utf-8"))["адреса"])
    соб: dict = {a: [] for a in цель}
    видел = set()
    for f in ф:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        for e in д.get("наши_события") or []:
            кл = (e.get("signature"), e.get("trader"), e.get("action"))
            if e.get("trader") not in цель or кл in видел or not PR.окно(e, f):
                continue
            видел.add(кл)
            соб[e["trader"]].append(e)
        del д
    из_ = {}
    for a, сп in соб.items():
        пок = [e for e in сп if e.get("action") == "buy"]
        прод = [e for e in сп if e.get("action") == "sell"]
        разм = [e.get("sol_экв") for e in пок if e.get("sol_экв")]
        дни = collections.Counter(ST.сутки((e.get("timestamp") or 0) / 1000) for e in пок)
        держ = []
        по_минту: dict = {}
        for e in сп:
            по_минту.setdefault(e.get("mint"), []).append(e)
        for m, сп_м in по_минту.items():
            пк = [x for x in сп_м if x.get("action") == "buy"]
            пр = [x for x in сп_м if x.get("action") == "sell"]
            if пк and пр:
                держ.append((пр[-1].get("block") or 0) - (пк[0].get("block") or 0))
        из_[a] = {"событий": len(сп), "покупок": len(пок), "продаж": len(прод),
                  "суток": len(дни), "покупок_в_сутки": round(len(пок) / len(дни), 1) if дни else None,
                  "размер_медиана": round(statistics.median(разм), 4) if разм else None,
                  "типы": dict(collections.Counter(e.get("pool") for e in пок).most_common()),
                  "удержание_медиана_слотов": round(statistics.median(держ)) if держ else None,
                  "доля_продаж": round(len(прод) / len(пок), 2) if пок else None}
    return {**если, "профиль": из_}


def страница(с: dict) -> None:
    def ош(d: dict) -> str:
        return f"ошибок чтения часов {len(d.get('ошибки') or [])}" if d else ""
    им = имена_наших()
    md = ["# Подбивка: поиск похожих на стабильных (окно 21.09 17Z – 01.10 17Z)", "",
          "Правило отбора -- кандидатское **плюс стабильность**: n ≥ 20 первых покупок источника от 2 SOL-экв, "
          f"среднее ≥ +{СР_МИН:g} п.п. и медиана > 0 при нашем входе «конец слота» на билете 0.3 с выходом "
          f"+108, «толпа есть» ≥ {ТОЛПА_МИН:g}, и медиана > 0 в **обеих половинах окна** при не меньше "
          f"{ST.ДОЛЯ_СУТОК:.0%} суток в плюсе. Кусты -- одним адресом. Ничего не рекомендуется.", ""]

    з = с.get("закем") or {}
    md += ["## За кем идут наши", ""]
    if з.get("why_not"):
        md += [f"Прогон ещё не закончен: {з['why_not']}.", ""]
    else:
        md += [f"По {з.get('файлов')} суткам: сигналов наших источников {з.get('сигналов')}, из них с "
               f"покупателями того же пула в слотах s0−10…s0−1 -- **{з.get('сигналов_с_перед')}** "
               f"({100 * (з.get('сигналов_с_перед') or 0) / max(1, з.get('сигналов') or 1):.0f} %); "
               f"разных адресов перед нами {з.get('адресов_перед')}, из них стоят перед нами не меньше "
               f"{ПОВТОР_МИН} раз -- **{з.get('повторяющихся')}**. {ош(з)}.", "",
               "| адрес | раз перед нами | наших источников | медиана опережения, слотов | медиана размера, SOL | "
               "перед кем чаще всего |", "|---|---|---|---|---|---|"]
        for a, v in list((з.get("ряды") or {}).items())[:40]:
            чаще = ", ".join(f"{b[:8]}" + (f" ({им.get(b)})" if им.get(b) else "") + f" {n}"
                             for b, n in (v.get("наши") or [])[:3])
            md.append(f"| `{a}` | {v['раз']} | {v['наших_источников']} | {v['медиана_слотов']} | "
                      f"{v['медиана_sol']} | {чаще} |")
        md += ["", "### По нашим источникам: кто стоит перед ними", "",
               "| наш источник | группы | сигналов | из них с кем-то впереди | чаще всего впереди |",
               "|---|---|---|---|---|"]
        for a, v in sorted((з.get("по_нашим") or {}).items(), key=lambda kv: -kv[1]["сигналов"])[:30]:
            чаще = ", ".join(f"`{b[:8]}` {n}" for b, n in (v.get("перед") or [])[:3]) or "—"
            md.append(f"| `{a[:8]}` | {им.get(a, '—')} | {v['сигналов']} | {v['с_перед']} | {чаще} |")
        md.append("")

    for имя, заг in (("повторяющиеся", "Повторяющиеся впереди нас -- через правило со стабильностью"),
                     ("вселенная2", "Вся вселенная архива -- через правило со стабильностью"),
                     ("ved200", "Ведущие верхних 200 копировщиков -- через правило со стабильностью")):
        д = с.get(имя) or {}
        md += [f"## {заг}", ""]
        if not д or д.get("why_not"):
            md += [f"Прогон ещё не закончен: {(д or {}).get('why_not', 'файлов нет')}.", ""]
            continue
        md += [f"Адресов со сигналами {д.get('адресов')}, правило со стабильностью проходят "
               f"**{д.get('прошли')}**; кустов {д.get('кустов')}, убрано как второй адрес куста "
               f"{д.get('убрано_кустом')}; в списке **{len(д.get('список') or [])}**. {ош(д)}.", "",
               "| адрес | n | среднее, п.п. | медиана, п.п. | в плюсе | толпа | медиана 1-й / 2-й половины | "
               "суток в плюсе | сигналов |", "|---|---|---|---|---|---|---|---|---|"]
        for a in (д.get("список") or [])[:60]:
            v = д["ряды"][a]
            s, ст = v["итог"], v["стабильность"]
            md.append(f"| `{a}` | {s.get('n')} | {s.get('среднее'):+.2f} | {s.get('медиана'):+.2f} | "
                      f"{s.get('в_плюсе'):.0%} | {v['толпа']} | {ст['медиана_1']:+.2f} / "
                      f"{ст['медиана_2']:+.2f} | {ст['суток_в_плюсе']} / {ст['суток']} | {v['сигналов']} |")
        md.append("")

    в = с.get("вселенная") or {}
    md += ["## Сито вселенной (шаг 1)", ""]
    md += ([f"Прогон ещё не закончен: {в['why_not']}.", ""] if в.get("why_not") else
           [f"По {в.get('суток')} суткам: кошельков с первыми покупками от 2 SOL в сите "
            f"{в.get('кошельков_в_сите')}, из них с не меньше {N_МИН} такими покупками за окно -- "
            f"**{в.get('короткий_список')}** (список -- `data/podbivka/vselennaya_korotkiy.json`, "
            "по нему идёт шаг 2: правило со стабильностью).", ""])

    п = (с.get("профиль") or {}).get("профиль") or {}
    пр_ф = (с.get("профиль") or {}).get("файлов")
    md += ["## Профиль стабильных (6qudAN2k / DYAn4XpA)", "",
           *([f"Считано по {пр_ф} суточным файлам прохода -- это ещё не всё окно, числа предварительные.", ""]
             if (пр_ф or 0) and (пр_ф or 0) < 11 else [])]
    if not п:
        md += [f"{(с.get('профиль') or {}).get('why_not', 'файлов нет')}.", ""]
    else:
        md += ["| адрес | покупок | продаж | суток | покупок в сутки | размер, медиана SOL | "
               "удержание, медиана слотов | доля продаж | типы пулов |", "|---|---|---|---|---|---|---|---|---|"]
        for a, v in п.items():
            md.append(f"| `{a[:8]}` | {v['покупок']} | {v['продаж']} | {v['суток']} | "
                      f"{v['покупок_в_сутки']} | {v['размер_медиана']} | {v['удержание_медиана_слотов']} | "
                      f"{v['доля_продаж']} | " + ", ".join(f"{k} {n}" for k, n in list(v['типы'].items())[:5])
                      + " |")
        md.append("")
    СТРАНИЦА.write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--zakem", action="store_true")
    р.add_argument("--vselennaya", action="store_true")
    р.add_argument("--vselennaya2", action="store_true", help="правило по короткому списку вселенной (vsel2_*)")
    р.add_argument("--ved200", action="store_true")
    р.add_argument("--povtor", action="store_true", help="правило по повторяющимся впереди нас (zakem2_*)")
    р.add_argument("--profil", action="store_true")
    а = р.parse_args()
    с = состояние()
    if а.zakem:
        с["закем"] = закем()
    if а.vselennaya:
        с["вселенная"] = вселенная()
    if а.vselennaya2:
        с["вселенная2"] = по_правилу("vsel2_*T16.json.gz")
    if а.ved200:
        с["ved200"] = по_правилу("ved200_*T16.json.gz")
    if а.povtor:
        с["повторяющиеся"] = по_правилу("zakem2_*T16.json.gz")
    if а.profil:
        с["профиль"] = профиль()
    СОСТ.write_text(json.dumps(с, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    страница(с)
    R.записано(СОСТ)
    R.записано(СТРАНИЦА)
    print(СТРАНИЦА.name, "разделов:", ", ".join(k for k in с), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
