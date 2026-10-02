#!/usr/bin/env python3
"""Куст DYAn4XpA / HT5EVAzf / Gf2wYM2k: кто входит быстрее, есть ли уникальные, один ли это трейдер. Офлайн.

Вход: data/podbivka/arhiv_den/kust_*T16.json.gz (7 суток, порог сигнала 0.01, события включены),
data/podbivka/kust_mesta.json (места в блоке по общим минт+слот -- отдельный проход по цепи, если готов).
Выход: docs/podbivka_2026-10-02_kust_DYAn.md, data/podbivka/kust_svod.json и, на первом проходе,
data/podbivka/kust_slots.json -- заявка на места в блоке.

Три вопроса владельца: (а) общие минт+слот -- кто раньше по месту в блоке, одна транзакция или разные,
разница индексов, доля случаев, где первым каждый; (б) уникальные сигналы каждого (минт без двух других в
окне 1800 слотов) -- n и результат при нашем входе (конец слота, билет 0.3, выход +108); (в) похожи ли на
«один трейдер -- разные кошельки» (размеры, интервалы, программы, приоритет) -- числом.
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

import podbivka_porog_razmera as PR  # noqa: E402

КОРЕНЬ = PR.КОРЕНЬ
П = PR.П
БИЛЕТ = 0.3
ОКНО_1800 = 1800
ПОРОГ_ЖИВОЙ = 2.0        # порог размера покупки источника откатан владельцем до 2.0


def имя(a: str) -> str:
    return a[:8]


def собрать() -> tuple[dict, dict, dict]:
    """События (покупки и продажи) и сигналы трёх адресов по суточным файлам."""
    файлы = sorted(glob.glob(str(П / "arhiv_den" / "kust_*T16.json.gz")))
    события, сигналы = [], []
    счёт = {"файлов": 0, "событий": 0, "сигналов": 0, "ошибки": []}
    видел_с, видел_е = set(), set()
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        счёт["файлов"] += 1
        счёт["ошибки"] += [f"{Path(f).name}: {x}" for x in (д.get("счёт") or {}).get("ошибки") or []]
        for e in д.get("наши_события") or []:
            кл = (e.get("signature"), e.get("trader"), e.get("action"))
            if кл in видел_е or not PR.окно(e, f):
                continue
            видел_е.add(кл)
            события.append(e)
        for с in д.get("сигналы") or []:
            кл = (с.get("signature"), с.get("trader"))
            if кл in видел_с or not PR.окно(с, f):
                continue
            видел_с.add(кл)
            сигналы.append(с)
        del д
    счёт["событий"], счёт["сигналов"] = len(события), len(сигналы)
    return события, сигналы, счёт


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    адреса = json.loads((П / "kust_dyan_adresa.json").read_text(encoding="utf-8"))["адреса"]
    события, сигналы, счёт = собрать()
    if not события:
        print("нет файлов kust_*T16 -- проход архива не закончен", flush=True)
        return 1
    покупки = [e for e in события if e.get("action") == "buy"]
    продажи = [e for e in события if e.get("action") == "sell"]

    # (а) общие минт+слот
    по_ключу: dict = {}
    for e in покупки:
        по_ключу.setdefault((e.get("mint"), e.get("block")), []).append(e)
    общие = {к: v for к, v in по_ключу.items() if len({x["trader"] for x in v}) >= 2}
    места = {}
    мп = П / "kust_mesta.json"
    if мп.exists():
        места = json.loads(мп.read_text(encoding="utf-8")).get("слоты") or {}
    заявка: dict = {}
    for (м, сл), v in общие.items():
        заявка.setdefault(str(сл), [])
        for e in v:
            if e["signature"] not in заявка[str(сл)]:
                заявка[str(сл)].append(e["signature"])

    разбор_а, первым = [], collections.Counter()
    одна_тр = 0
    без_мест = 0
    for (м, сл), v in sorted(общие.items(), key=lambda kv: kv[0][1]):
        подписи = {e["trader"]: e["signature"] for e in v}
        одна = len(set(подписи.values())) == 1 and len(подписи) >= 2
        одна_тр += 1 if одна else 0
        место_сл = (места.get(str(сл)) or {}).get("места") or {}
        мест = {t: место_сл.get(s) for t, s in подписи.items()}
        есть = {t: x for t, x in мест.items() if x is not None}
        кто_первый = min(есть, key=lambda t: есть[t]) if есть and not одна else None
        if одна:
            первым["одна транзакция"] += 1
        elif кто_первый:
            первым[кто_первый] += 1
        else:
            без_мест += 1
        разбор_а.append({"минт": м, "слот": сл, "участники": sorted(подписи),
                         "одна_транзакция": одна, "места": мест,
                         "разница": (max(есть.values()) - min(есть.values())) if len(есть) >= 2 else None,
                         "первый": кто_первый, "в_блоке": (места.get(str(сл)) or {}).get("всего"),
                         "размеры": {e["trader"]: round(e.get("sol_экв") or 0, 4) for e in v},
                         "подписи": подписи})

    # (б) уникальные сигналы: минт, которого нет у двух других в окне 1800 слотов
    покупки_по_минту: dict = {}
    for e in покупки:
        покупки_по_минту.setdefault(e.get("mint"), []).append((e.get("block") or 0, e["trader"]))
    уник = {a: {"все": [], "от2": []} for a in адреса}
    for с in сигналы:
        a, м, сл = с.get("trader"), с.get("mint"), с.get("block") or 0
        чужие = [b for (bl, t) in покупки_по_минту.get(м) or [] if t != a and abs(bl - сл) <= ОКНО_1800
                 for b in [t]]
        if чужие:
            continue
        if not PR.в_ячейке(с, БИЛЕТ):
            continue
        v = PR.пп(с, БИЛЕТ)
        уник[a]["все"].append(v)
        if (с.get("sol") or 0) >= ПОРОГ_ЖИВОЙ:
            уник[a]["от2"].append(v)

    # (в) один трейдер или разные
    в: dict = {}
    for a in адреса:
        пок = [e for e in покупки if e["trader"] == a]
        прод = [e for e in продажи if e["trader"] == a]
        разм = [e.get("sol_экв") or 0 for e in пок if (e.get("sol_экв") or 0) > 0]
        прио = [e.get("priorityFee") for e in пок if e.get("priorityFee") is not None]
        блоки = sorted(e.get("block") or 0 for e in пок)
        промеж = [b - a_ for a_, b in zip(блоки, блоки[1:]) if b > a_]
        в[a] = {"покупок": len(пок), "продаж": len(прод),
                "минтов": len({e.get("mint") for e in пок}),
                "размер_медиана": round(statistics.median(разм), 4) if разм else None,
                "размер_среднее": round(statistics.mean(разм), 4) if разм else None,
                "размер_макс": round(max(разм), 4) if разм else None,
                "приоритет_медиана": round(statistics.median(прио), 9) if прио else None,
                "пулы": dict(collections.Counter(e.get("pool") for e in пок).most_common()),
                "котировки": dict(collections.Counter(("SOL" if e.get("quoteMint") == PR.K.WSOL else "не SOL")
                                                      for e in пок).most_common()),
                "промежуток_медиана_слотов": round(statistics.median(промеж)) if промеж else None,
                "в_общих_минт_слот": sum(1 for x in разбор_а if a in x["участники"]),
                "своих_минтов_без_других": len({с.get("mint") for с in сигналы if с.get("trader") == a
                                                and not [1 for (bl, t) in покупки_по_минту.get(с.get("mint")) or []
                                                         if t != a and abs(bl - (с.get("block") or 0)) <= ОКНО_1800]})}
    пары = {}
    for i, a in enumerate(адреса):
        for b in адреса[i + 1:]:
            ма = {e.get("mint") for e in покупки if e["trader"] == a}
            мб = {e.get("mint") for e in покупки if e["trader"] == b}
            общ = ма & мб
            # медианный разрыв по общим минтам: ближайшая покупка b к покупке a
            разрывы = []
            for e in (x for x in покупки if x["trader"] == a and x.get("mint") in общ):
                бл = [x.get("block") or 0 for x in покупки if x["trader"] == b and x.get("mint") == e.get("mint")]
                if бл:
                    разрывы.append(min(abs((e.get("block") or 0) - x) for x in бл))
            пары[f"{имя(a)} / {имя(b)}"] = {
                "общих_минтов": len(общ), "минтов_a": len(ма), "минтов_b": len(мб),
                "жаккар": round(len(общ) / len(ма | мб), 3) if (ма | мб) else None,
                "разрыв_медиана_слотов": round(statistics.median(разрывы)) if разрывы else None,
                "разрыв_0_слотов": sum(1 for x in разрывы if x == 0),
                "случаев": len(разрывы)}

    def клетка(v: list) -> str:
        s = PR.стат(v)
        if not s.get("n"):
            return "— | — | — | —"
        if s["n"] < 20:
            return f"n={s['n']} (мало) | — | — | —"
        return f"{s['n']} | {s['среднее']:+.2f} | {s['медиана']:+.2f} | {s['в_плюсе']:.0%}"

    md = ["# Подбивка: куст DYAn4XpA / HT5EVAzf / Gf2wYM2k -- кто входит быстрее и есть ли уникальные", "",
          f"7 суток архива (окно 24.09 17:00Z → 01.10 17:00Z, файлы `kust_*T16`, порог сигнала 0.01 SOL): "
          f"событий трёх адресов {счёт['событий']} (покупок {len(покупки)}, продаж {len(продажи)}), сигналов "
          f"{счёт['сигналов']}, ошибок чтения часов {len(счёт['ошибки'])}. Места в блоке -- отдельный проход по "
          f"цепи (`getBlock`), в архиве их нет"
          + (f": слотов с местами {len(места)}." if места else " -- ещё не собраны, столбцы мест пустые.")
          + " Ничего не рекомендуется.", "",
          "## а. Общие минт + слот: кто раньше внутри блока", "",
          f"Случаев, где тот же минт в том же слоте купили хотя бы двое из трёх: **{len(общие)}**; из них одна "
          f"и та же транзакция на двоих -- **{одна_тр}**"
          + (f", без мест в блоке (блок не отдан) -- {без_мест}." if без_мест else "."), ""]
    if общие:
        md += ["| слот | минт | кто | размеры, SOL-экв | одна транзакция | места в блоке | разница | первый |",
               "|---|---|---|---|---|---|---|---|"]
        for x in разбор_а[:60]:
            md.append(f"| {x['слот']} | `{(x['минт'] or '')[:8]}` | "
                      + ", ".join(имя(t) for t in x["участники"]) + " | "
                      + ", ".join(f"{имя(t)} {v:g}" for t, v in x["размеры"].items()) + " | "
                      + ("да" if x["одна_транзакция"] else "нет") + " | "
                      + ", ".join(f"{имя(t)} {v if v is not None else '—'}" for t, v in x["места"].items())
                      + f" | {x['разница'] if x['разница'] is not None else '—'} | "
                      + (имя(x["первый"]) if x["первый"] else ("одна транзакция" if x["одна_транзакция"] else "—"))
                      + " |")
        if len(разбор_а) > 60:
            md.append(f"| … ещё {len(разбор_а) - 60} случаев в `data/podbivka/kust_svod.json` | | | | | | | |")
        md += ["", "**Доля случаев, где первым каждый:** "
               + ", ".join(f"{(имя(k) if len(k) > 20 else k)} -- {n} ({n / len(общие):.0%})"
                           for k, n in первым.most_common()) + ".", ""]
    md += ["## б. Уникальные сигналы каждого (минт без двух других в окне 1800 слотов)", "",
           f"Результат при нашем входе «конец слота» за ним, билет {БИЛЕТ:g}, выход +108, п.п. чистыми. Первая "
           f"строка -- по живому правилу (покупка источника от {ПОРОГ_ЖИВОЙ:g} SOL-экв), вторая -- по всем "
           "размерам, какие есть в проходе.", "",
           "| адрес | от 2 SOL: n | среднее | медиана | в плюсе | все размеры: n | среднее | медиана | в плюсе |",
           "|---|---|---|---|---|---|---|---|---|"]
    for a in адреса:
        md.append(f"| `{a}` | " + клетка(уник[a]["от2"]) + " | " + клетка(уник[a]["все"]) + " |")
    md += ["", "## в. Похожи ли на «один трейдер -- разные кошельки»", "",
           "| адрес | покупок / продаж | минтов | размер: медиана / среднее / макс | приоритет, медиана SOL | "
           "промежуток между своими покупками, медиана слотов | в общих минт+слот | своих минтов без двух других |",
           "|---|---|---|---|---|---|---|---|"]
    for a in адреса:
        v = в[a]
        md.append(f"| `{a}` | {v['покупок']} / {v['продаж']} | {v['минтов']} | "
                  f"{v['размер_медиана']} / {v['размер_среднее']} / {v['размер_макс']} | "
                  f"{v['приоритет_медиана']} | {v['промежуток_медиана_слотов']} | {v['в_общих_минт_слот']} | "
                  f"{v['своих_минтов_без_других']} |")
    md += ["", "| адрес | пулы покупок | котировки |", "|---|---|---|"]
    for a in адреса:
        md.append(f"| `{a}` | " + ", ".join(f"{k} {n}" for k, n in в[a]["пулы"].items()) + " | "
                  + ", ".join(f"{k} {n}" for k, n in в[a]["котировки"].items()) + " |")
    md += ["", "| пара | общих минтов | жаккар | разрыв по общему минту, медиана слотов | из них в том же слоте |",
           "|---|---|---|---|---|"]
    for k, v in пары.items():
        md.append(f"| {k} | {v['общих_минтов']} (из {v['минтов_a']} и {v['минтов_b']}) | {v['жаккар']} | "
                  f"{v['разрыв_медиана_слотов']} | {v['разрыв_0_слотов']} из {v['случаев']} |")
    md.append("")
    out = КОРЕНЬ / "docs" / "podbivka_2026-10-02_kust_DYAn.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(out)
    св = П / "kust_svod.json"
    св.write_text(json.dumps({"окно": ["2026-09-24T17:00Z", "2026-10-01T17:00Z"], "счёт": счёт,
                              "общих_минт_слот": len(общие), "одна_транзакция": одна_тр,
                              "первым": dict(первым), "случаи": разбор_а,
                              "уникальные": {a: {"от2": PR.стат(уник[a]["от2"]), "все": PR.стат(уник[a]["все"])}
                                             for a in адреса},
                              "адреса": в, "пары": пары}, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(св)
    if not места:
        зв = П / "kust_slots.json"
        зв.write_text(json.dumps({"что": "заявка на места в блоке по общим минт+слот куста",
                                  "слоты": заявка}, ensure_ascii=False, indent=1), encoding="utf-8")
        R.записано(зв)
        print(f"мест в блоке нет: заявка на {len(заявка)} слотов в {зв.name}", flush=True)
    print(out.name, "общих минт+слот", len(общие), "одна транзакция", одна_тр, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
