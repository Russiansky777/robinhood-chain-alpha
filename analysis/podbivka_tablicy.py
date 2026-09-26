#!/usr/bin/env python3
"""Подбивка: таблицы п.2-п.5 и отчёт docs/podbivka_2026-09-26.md.

Читает только готовые файлы по кошелькам (data/podbivka/<задача>/<адрес>.json)
и сводки контроля и 1б -- сеть не нужна, пересобирается сколько угодно раз по
мере того, как досчитываются пакеты. Ничего не выбрасывается:
  * покупка без числа симулятора -- в отказах с причиной, доля рядом;
  * «без свопов до горизонта» -- выход по текущим резервам, доля отдельной колонкой;
  * кошелёк, который не разобрался, -- списком с причиной.

Статистика ячейки: n, медиана, среднее, доля в плюс, среднее без лучшей
(n < 30) или без трёх лучших (n >= 30). Метка по S+1/72 -- по СРЕДНЕМУ:
n >= 20 и >= +2 % -- «на размер»; 0..+2 % -- «дорогой»; <= 0 -- «вылет»;
n < 20 -- «мало покупок».
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПОРОГИ = ("2-5", "5+")
ВХОДЫ = ("S0", "S1", "S2")
ГОРИЗОНТЫ = (12, 25, 50, 72, 112, 150)
ВЫХОДЫ_ТАБЛИЦ = (72, 150)
ЯРЛЫКИ_ПУЛОВ = КОРЕНЬ / "data" / "solana_buyer_200" / "prior" / "current" / "buyer_100" / "dex_labels.json"


def чп(сим: dict | None, e: str, H: int):
    if not сим or сим.get("why_not"):
        return None
    т = (сим.get("чистый_пп") or {}).get(e) or {}
    v = т.get(str(H), т.get(H))
    return v


def стат(xs: list) -> dict:
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if not n:
        return {"n": 0}
    s = sorted(xs)
    без = s[:-1] if n < 30 else s[:-3]
    return {"n": n, "мед": round(statistics.median(s), 2), "ср": round(sum(s) / n, 2),
            "плюс": round(sum(1 for x in s if x > 0) / n, 3),
            "ср_без": round(sum(без) / len(без), 2) if без else None}


def метка(с: dict) -> str:
    if not с.get("n"):
        return "нет данных"
    if с["n"] < 20:
        return "мало покупок"
    if с["ср"] >= 2.0:
        return "на размер"
    if с["ср"] > 0:
        return "дорогой"
    return "вылет"


def доля(флаги: list):
    ф = [x for x in флаги if x is not None]
    return round(sum(1 for x in ф if x) / len(ф), 3) if ф else None


def строка_порога(покупки: list) -> dict:
    """Колонки одной строки (один порог) по списку покупок (с полем sim)."""
    ok = [п for п in покупки if п.get("sim") and not п["sim"].get("why_not")]
    из_ = {"покупок": len(покупки), "сим_ok": len(ok),
           "отказов": len(покупки) - len(ok)}
    for H in ВЫХОДЫ_ТАБЛИЦ:
        for e in ВХОДЫ:
            из_[f"{e}_{H}"] = стат([чп(п["sim"], e, H) for п in ok])
        из_[f"без_свопов_{H}"] = доля([(п["sim"].get("без_свопов") or {}).get(str(H),
                                        (п["sim"].get("без_свопов") or {}).get(H)) for п in ok])
    из_["продал_в_окне"] = доля([п.get("продал_в_окне") for п in покупки
                                  if п.get("окно_продажи_полное", True)])
    толпы = [п["sim"].get("толпа_s0_2") for п in ok if п["sim"].get("толпа_s0_2") is not None]
    из_["толпа_мед"] = statistics.median(толпы) if толпы else None
    кот: dict = {}
    for п in покупки:
        к = п.get("котировка_пула") or "нет данных"
        кот[к] = кот.get(к, 0) + 1
    из_["котировка"] = кот
    из_["метка"] = метка(из_["S1_72"])
    return из_


def каталог_задачи(имя: str) -> Path:
    п = Path(имя)
    return п if п.is_absolute() else КОРЕНЬ / "data" / "podbivka" / имя


# ---- пост-фактум курс и порог (слово владельца 26.09: без сети, по сохранённым суммам)

_РЯД: list = []


def курс_детектора(bt) -> float | None:
    """USD за SOL из data/podbivka/kurs_sol_usd.json -- ближайший снимок не дальше 12 ч."""
    import calendar
    import time
    if not _РЯД:
        п = КОРЕНЬ / "data" / "podbivka" / "kurs_sol_usd.json"
        if п.exists():
            for x in json.loads(п.read_text(encoding="utf-8"))["ряд"]:
                _РЯД.append((calendar.timegm(time.strptime(x["utc"], "%Y-%m-%dT%H:%M:%SZ")), x["usd_sol"]))
    if not _РЯД or not bt:
        return None
    т, к = min(_РЯД, key=lambda x: abs(x[0] - bt))
    return к if abs(т - bt) <= 12 * 3600 else None


ДОСЧИТАТЬ: list = []
ОТБРОШЕНО_ПОРОГОМ: list = []


def пересчёт_порога(покупки: list, адрес: str) -> list:
    """Порог 2 SOL-экв заново: трата SOL + трата USD / курс детектора (или сохранённый).

    Ниже 2 -- отбросить; выше и без числа симулятора (в прогоне не взята) --
    в список «досчитать» для второго прохода.
    """
    из_ = []
    for п in покупки:
        usd = float(п.get("трата_usd") or 0)
        курс = курс_детектора(п.get("blockTime")) if usd > 0 else None
        if usd > 0 and курс is None:
            курс = п.get("курс")
        sol = float(п.get("трата_sol") or 0) + (usd / курс if usd > 0 and курс else 0.0)
        if usd > 0 and not курс:
            ДОСЧИТАТЬ.append({"address": адрес, "signature": п.get("signature"),
                              "почему": "трата в стейблах, курса нет ни в ряду, ни в файле"})
            continue
        порог = "5+" if sol >= 5 else "2-5" if sol >= 2 else None
        if порог is None:
            ОТБРОШЕНО_ПОРОГОМ.append({"address": адрес, "signature": п.get("signature"), "sol_экв": round(sol, 4)})
            continue
        if not п.get("sim"):
            ДОСЧИТАТЬ.append({"address": адрес, "signature": п.get("signature"), "sol_экв": round(sol, 4),
                              "почему": "выше порога после пересчёта, в прогоне не просчитана"})
        из_.append({**п, "sol_экв": round(sol, 4), "порог": порог})
    return из_


def читать(каталог: Path) -> list:
    return [json.loads(Path(p).read_text(encoding="utf-8"))
            for p in sorted(glob.glob(str(каталог / "*.json"))) if not Path(p).name.startswith("_")]


ГРАНЬ_SHYFT = 1790208000  # 24.09 00:00Z: «ретро» наших -- как у чужих, окно Shyft


def в_окне_7(п: dict, до_ts: int) -> bool:
    """«Ретро»: первые покупки в окне Shyft (с 24.09), как у чужих в проходе (а)."""
    return (п.get("blockTime") or 0) >= max(до_ts - 7 * 86400, ГРАНЬ_SHYFT)


def до_20(покупки: list) -> list:
    """Не больше 20 самых свежих на порог -- как у чужих."""
    из_ = []
    for пор in ПОРОГИ:
        из_ += sorted([п for п in покупки if п.get("порог") == пор],
                      key=lambda п: -(п.get("blockTime") or 0))[:20]
    return из_


def не_отдано(кошельки: list, покупки: list) -> str:
    """Что не отдал узел: транзакции окна при скане и сделки источника у покупок."""
    подп = sum((к.get("скан") or {}).get("подписей") or 0 for к in кошельки)
    нет = sum((к.get("скан") or {}).get("не_отдал") or 0 for к in кошельки)
    пк = [п for п in покупки if п.get("sim")]
    нет_пк = sum(1 for п in пк if "узел не отдал" in (п["sim"].get("why_not") or "")
                 or "история пула не собрана: узел" in (п["sim"].get("why_not") or ""))
    return (f"транзакций окна не отдано {нет} из {подп} ({(нет / подп if подп else 0):.1%}); "
            f"покупок без сделки источника от узла {нет_пк} из {len(пк)} ({(нет_пк / len(пк) if пк else 0):.1%})")


def доли_узлов(покупки: list) -> str:
    сч: dict = {}
    for п in покупки:
        у = (п.get("sim") or {}).get("узел") or "нет числа"
        сч[у] = сч.get(у, 0) + 1
    n = sum(сч.values())
    return ", ".join(f"{у} {v / n:.0%} ({v})" for у, v in sorted(сч.items())) if n else "—"


# ------------------------------------------------------------ корзины п.4

def корзина(п: dict, имя: str, ярлыки: dict):
    сим = п.get("sim") or {}
    if имя == "сумма покупки, SOL":
        x = п.get("sol_экв")
        return None if x is None else ("2-5" if x < 5 else "5-8" if x < 8 else "8-15" if x < 15 else "15+")
    if имя == "налог токена":
        b = сим.get("налог_bps")
        return None if b is None else ("нет" if b == 0 else "до 1 %" if b <= 100 else
                                       "1-5 %" if b <= 500 else "больше 5 %")
    if имя == "тип пула":
        прог = сим.get("program")
        if not прог:
            return None
        return ярлыки.get(прог, прог[:8]) + (" (цена свопа)" if сим.get("режим") == "price" else "")
    if имя == "ликвидность на входе, SOL":
        x = сим.get("ликвидность_sol")
        return None if x is None else ("<10" if x < 10 else "10-50" if x < 50 else
                                       "50-200" if x < 200 else "200+")
    if имя == "возраст токена":
        в = п.get("возраст") or {}
        м = в.get("минут")
        if м is None:
            return "не определён"
        if в.get("старше_суток") or м >= 1440:
            return "старше суток"
        if not в.get("точно"):
            return "не определён (предел)" if м < 60 else ("1-24 ч" if м >= 60 else "не определён")
        return "<10 мин" if м < 10 else "10-60 мин" if м < 60 else "1-24 ч"
    if имя == "толпа s0..s0+2":
        т = сим.get("толпа_s0_2")
        return None if т is None else ("0" if т == 0 else "1-2" if т <= 2 else "3-5" if т <= 5 else "6+")
    if имя == "источник продал в окне":
        return "да" if п.get("продал_в_окне") else "нет"
    return None


КОРЗИНЫ = ("сумма покупки, SOL", "налог токена", "тип пула", "ликвидность на входе, SOL",
           "возраст токена", "толпа s0..s0+2", "источник продал в окне")


# ------------------------------------------------------------ вывод

def ячейка(с: dict) -> str:
    if not с or not с.get("n"):
        return "—"
    return f"{с['мед']:+.1f} / {с['ср']:+.1f} / {с['плюс']:.0%} / {с['ср_без']:+.1f}" if с.get("ср_без") is not None \
        else f"{с['мед']:+.1f} / {с['ср']:+.1f} / {с['плюс']:.0%} / —"


def таблица_порогов(заголовок: str, строки: list) -> list:
    """строки: [(имя, порог, колонки)]."""
    out = [f"### {заголовок}", "",
           "Ячейка: медиана / среднее / доля в плюс / среднее без лучшей (n<30) или без трёх (n≥30), п.п. на 0.5 SOL.", "",
           "| источник | порог | n (сим) | котировка пула | S+0/72 | S+1/72 | S+2/72 | S+0/150 | S+1/150 | S+2/150 | без свопов 72 / 150 | продал в окне | толпа мед | метка |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for имя, порог, к in строки:
        def д(x):
            return "—" if x is None else f"{x:.0%}"
        кот = " ".join(f"{кк} {vv}" for кк, vv in sorted(к.get("котировка", {}).items())) or "—"
        out.append(f"| {имя} | {порог} | {к['покупок']} ({к['сим_ok']}) | {кот} | {ячейка(к['S0_72'])} | {ячейка(к['S1_72'])} | "
                   f"{ячейка(к['S2_72'])} | {ячейка(к['S0_150'])} | {ячейка(к['S1_150'])} | {ячейка(к['S2_150'])} | "
                   f"{д(к['без_свопов_72'])} / {д(к['без_свопов_150'])} | {д(к['продал_в_окне'])} | "
                   f"{к['толпа_мед'] if к['толпа_мед'] is not None else '—'} | {к['метка']} |")
    return out + [""]


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--nashi", default="nashi")
    р.add_argument("--chuzhie", default="koshelki")
    р.add_argument("--proba", default="koshelki_proba")
    р.add_argument("--do-utc", required=True)
    р.add_argument("--out-json", default=str(КОРЕНЬ / "data" / "podbivka" / "tablicy.json"))
    р.add_argument("--out-md", default=str(КОРЕНЬ / "docs" / "podbivka_2026-09-26_tablicy.md"))
    а = р.parse_args()
    import calendar
    import time
    до_ts = calendar.timegm(time.strptime(а.do_utc, "%Y-%m-%dT%H:%M:%SZ"))
    ярлыки = json.loads(ЯРЛЫКИ_ПУЛОВ.read_text(encoding="utf-8")) if ЯРЛЫКИ_ПУЛОВ.exists() else {}
    ярлыки.setdefault("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "pump.fun кривая")
    md: list = []
    итог: dict = {}
    сырьё_п4: list = []

    # ---- п.2
    наши = читать(каталог_задачи(а.nashi))
    ист_группы = {}
    путь_ист = КОРЕНЬ / "data" / "podbivka" / "istochniki.json"
    if путь_ист.exists():
        for з in json.loads(путь_ист.read_text(encoding="utf-8"))["источники"]:
            ист_группы[з["address"]] = з["группа_п5"]
    строки_п2, не_разобр_п2, контроль = [], [], {"DBot_сделок": 0, "DBot_net_sol": 0.0}
    все_п2: list = []
    for к in наши:
        адрес = (к.get("строка") or {}).get("address")
        if к.get("why_not") or (к.get("скан") or {}).get("why_not"):
            не_разобр_п2.append({"address": адрес, "причина": к.get("why_not") or к["скан"]["why_not"]})
        б = до_20([п for п in пересчёт_порога(к.get("покупки") or [], адрес) if в_окне_7(п, до_ts)])
        сырьё_п4.extend(б)
        все_п2.extend(б + [{"sim": x.get("sim")} for x in к.get("факт") or []])
        for пор in ПОРОГИ:
            строки_п2.append((f"{адрес[:8]} (б)", пор, строка_порога([п for п in б if п["порог"] == пор])))
        ф = к.get("факт") or []
        фп = [{"sim": x.get("sim"), "порог": None} for x in ф]
        кол = строка_порога(фп)
        sol = sum(float(x.get("факт_net_sol") or 0) for x in ф if x.get("факт_net_sol") is not None)
        кол["факт_net_sol"] = round(sol, 6)
        кол["факт_сделок"] = sum(1 for x in ф if x.get("факт_net_sol") is not None)
        строки_п2.append((f"{адрес[:8]} (а) факт {кол['факт_сделок']} сд., {sol:+.3f} SOL", "все", кол))
        for x in ф:
            if x.get("канал") == "DBot" and x.get("факт_net_sol") is not None:
                контроль["DBot_сделок"] += 1
                контроль["DBot_net_sol"] += float(x["факт_net_sol"])
    итог["п2"] = {"строк": len(строки_п2), "не_разобрались": не_разобр_п2, "контроль_факта": контроль}
    if строки_п2:
        md += таблица_порогов("п.2 Наши источники: (а) факт -- наши сделки DBot/Bloom/полосы с 18.09, (б) ретро -- первые покупки в окне Shyft, до 20 на порог",
                               строки_п2)
        md += [f"Узлы по строкам п.2: {доли_узлов(все_п2)}",
               f"Не отдано узлом (п.2): {не_отдано(наши, все_п2)}", ""]

    # ---- п.3
    чужие = читать(каталог_задачи(а.chuzhie)) + читать(каталог_задачи(а.proba))
    строки_п3, не_разобр_п3, минуты = [], [], []
    for к in чужие:
        адрес = (к.get("строка") or {}).get("address")
        if к.get("why_not") or (к.get("скан") or {}).get("why_not"):
            не_разобр_п3.append({"address": адрес, "причина": к.get("why_not") or к["скан"]["why_not"]})
            continue
        if к.get("минут") is not None:
            минуты.append(к["минут"])
        б = пересчёт_порога(к.get("покупки") or [], адрес)
        сырьё_п4.extend(б)
        все = строка_порога(б)
        for пор in ПОРОГИ:
            строки_п3.append((адрес, пор, строка_порога([п for п in б if п["порог"] == пор]), все))
    строки_п3.sort(key=lambda r: -(r[3]["S1_72"].get("ср") if r[3]["S1_72"].get("n") else -1e9))
    итог["п3"] = {"кошельков": len(чужие), "не_разобрались": не_разобр_п3,
                  "минут_на_кошелёк_мед": round(statistics.median(минуты), 2) if минуты else None}
    if строки_п3:
        md += таблица_порогов("п.3 Чужие кошельки: первые покупки, до 20 на порог (сортировка по среднему S+1/72 кошелька)",
                               [(f"{a[:8]}", п, к) for a, п, к, _ in строки_п3])
        пп3 = [п for к in чужие for п in (к.get('покупки') or [])]
        md += [f"Узлы по строкам п.3: {доли_узлов([п for п in пп3 if п.get('порог')])}",
               f"Не отдано узлом (п.3): {не_отдано(чужие, пп3)}", ""]

    # ---- п.4
    md += ["### п.4 Корзины фильтров (все покупки п.2б и п.3), сигнал S+1/72", "",
           "n -- покупок в корзине всего (с числом симулятора); статистика -- по тем, где число есть.", "",
           "| признак | корзина | n | медиана | среднее | доля в плюс |", "|---|---|---|---|---|---|"]
    итог["п4"] = {}
    for имя in КОРЗИНЫ:
        по: dict = {}
        for п in сырьё_п4:
            к = корзина(п, имя, ярлыки)
            if к is None:
                к = "нет данных"
            по.setdefault(к, []).append(чп(п.get("sim"), "S1", 72))
        итог["п4"][имя] = {к: {**стат(v), "всего": len(v)} for к, v in по.items()}
        for к, с in sorted(итог["п4"][имя].items(), key=lambda kv: str(kv[0])):
            md.append(f"| {имя} | {к} | {с['всего']} ({с.get('n', 0)}) | "
                      f"{('%+.1f' % с['мед']) if с.get('n') else '—'} | {('%+.1f' % с['ср']) if с.get('n') else '—'} | "
                      f"{('%.0f%%' % (100 * с['плюс'])) if с.get('n') else '—'} |")
    md += ["", f"Узлы по строкам п.4: {доли_узлов(сырьё_п4)}",
           f"Не отдано узлом (п.4): {не_отдано(наши + чужие, сырьё_п4)}", ""]

    # ---- п.5
    md += ["### п.5 Горизонты выхода: наши источники (первые покупки 7 дней), вход S+1, 0.5 SOL", "",
           "Ячейка: среднее / медиана, п.п. (n)", "",
           "| группа | " + " | ".join(f"s0+{H}" for H in ГОРИЗОНТЫ) + " |",
           "|---|" + "---|" * len(ГОРИЗОНТЫ)]
    итог["п5"] = {}
    for группа in ("speed_only", "BATCH-3-5", "остальные"):
        покупки = [п for к in наши for п in (к.get("покупки") or [])
                   if п.get("порог") and в_окне_7(п, до_ts)
                   and ист_группы.get((к.get("строка") or {}).get("address")) == группа]
        ряд = {H: стат([чп(п.get("sim"), "S1", H) for п in покупки]) for H in ГОРИЗОНТЫ}
        итог["п5"][группа] = ряд
        md.append(f"| {группа} | " + " | ".join(
            (f"{ряд[H]['ср']:+.1f} / {ряд[H]['мед']:+.1f} ({ряд[H]['n']})" if ряд[H].get("n") else "—")
            for H in ГОРИЗОНТЫ) + " |")
    md.append("")
    итог["досчитать"] = ДОСЧИТАТЬ
    итог["отброшено_порогом_после_пересчёта"] = len(ОТБРОШЕНО_ПОРОГОМ)
    md += ["### Досчитать во втором проходе", "",
           f"Выше 2 SOL-экв после пересчёта курса, но без числа симулятора: {len(ДОСЧИТАТЬ)}; "
           f"отброшено как ниже порога после пересчёта: {len(ОТБРОШЕНО_ПОРОГОМ)}.", ""]
    md += ["### Не разобрались", "",
           *[f"- {x['address']}: {x['причина']}" for x in не_разобр_п2 + не_разобр_п3], ""]
    Path(а.out_json).write_text(json.dumps(итог, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    Path(а.out_md).write_text("\n".join(md), encoding="utf-8")
    print(f"таблицы: п.2 строк {len(строки_п2)}, п.3 кошельков {len(чужие)}, "
          f"п.4 покупок {len(сырьё_п4)}, не разобрались {len(не_разобр_п2) + len(не_разобр_п3)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
