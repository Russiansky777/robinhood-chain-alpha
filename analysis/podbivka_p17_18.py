#!/usr/bin/env python3
"""Подбивка: п.17 и п.18 (владелец 27.09) -- офлайн, по сохранённым числам симулятора.

п.17  S+0/72 и S+0/150 по источникам: n, среднее, усечённое без верхних 5 %
      (отбрасывается ceil(5 % n) лучших), доля в плюс. Наши 133 (ретро, как в п.2б)
      и 14 кандидатов.
п.18  «S+0 или ничего»: входы S+1 оставляются, если цена входа S+1 <= цена
      входа S+0 × 1.03 (× 1.05). Цена входа восстанавливается из самих чисел
      симулятора: при одном выходе H выручка пропорциональна числу токенов,
      т.е. обратна цене входа того же размера:
          P1/P0 = (100 + пп_S0_H + c) / (100 + пп_S1_H + c),  c = 0.4 п.п. (0.002 SOL).
      Приближение (нелинейность продажи в пул); точный путь цены -- в m4.
      Вход S+0 по определению = цена сразу после источника: отсечённых 0.
Группы: «не продал» (покупки без продажи в окне, 404 кошелька), толпа > 10
(медиана толпы s0..s0+2 кошелька > 10, n >= 5), 14 кандидатов.
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_tablicy as T  # noqa: E402

КОРЕНЬ = T.КОРЕНЬ
C_ПП = 0.4


ПЫЛЬ_ПП = 10_000  # > 100×: режим «цена свопа» с пылевой ценой S+0 (ошибка 16)
ПЫЛЬ: list = []


def чп(сим: dict | None, e: str, H: int):
    v = T.чп(сим, e, H)
    if v is not None and v > ПЫЛЬ_ПП and (сим or {}).get("режим") == "price":
        ПЫЛЬ.append(сим.get("signature"))
        return None
    return v


def стат(xs: list) -> dict:
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return {"n": 0}
    отбр = math.ceil(0.05 * n)
    ус = xs[:n - отбр] if n - отбр > 0 else []
    return {"n": n, "ср": round(sum(xs) / n, 2), "ус": round(sum(ус) / len(ус), 2) if ус else None,
            "мед": round(statistics.median(xs), 2), "плюс": round(sum(1 for x in xs if x > 0) / n, 3)}


def я(с: dict) -> str:
    if not с.get("n"):
        return "— | — | — | —"
    ус = f"{с['ус']:+.2f}" if с.get("ус") is not None else "—"
    return f"{с['n']} | {с['ср']:+.2f} | {ус} | {100 * с['плюс']:.0f}%"


def отношение_цены(сим: dict, H: int = 72):
    """P(S+1)/P(S+0) при одинаковом билете -- из выручки на одном выходе."""
    if (сим.get("вход_откуда") or {}).get("S1") == "S+0":
        return 1.0
    for h in (H, 150, 112, 50):
        a, b = чп(сим, "S0", h), чп(сим, "S1", h)
        if a is not None and b is not None and 100 + b + C_ПП > 0:
            return (100 + a + C_ПП) / (100 + b + C_ПП)
    return None


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--do-utc-nashi", default="2026-09-26T19:00:00Z")
    р.add_argument("--out-md", default=str(КОРЕНЬ / "docs" / "podbivka_2026-09-27_p17_18.md"))
    р.add_argument("--out-json", default=str(КОРЕНЬ / "data" / "podbivka" / "p17_18.json"))
    а = р.parse_args()
    до_ts = calendar.timegm(time.strptime(а.do_utc_nashi, "%Y-%m-%dT%H:%M:%SZ"))
    md: list = ["# Подбивка: п.17 (S+0 по источникам) и п.18 («S+0 или ничего»)", "",
                "Офлайн, по числам симулятора в файлах кошельков; 0.5 SOL; п.п. чистыми. "
                "Усечённое -- без ceil(5 % n) лучших покупок.", ""]
    итог: dict = {}

    # ---- чужие (404) и кандидаты
    чужие: dict = {}
    for к in T.читать(T.каталог_задачи("koshelki")) + T.читать(T.каталог_задачи("koshelki_proba")):
        адрес = (к.get("строка") or {}).get("address")
        if адрес and not к.get("why_not") and not (к.get("скан") or {}).get("why_not"):
            чужие[адрес] = T.пересчёт_порога(к.get("покупки") or [], адрес)
    канд = [x["address"] for x in json.loads((КОРЕНЬ / "data" / "podbivka" / "candidates_2026-09-27.json")
                                             .read_text(encoding="utf-8"))["верхние"]]
    толпа_кош = []
    for адрес, пп in чужие.items():
        т = [п["sim"]["толпа_s0_2"] for п in пп if п.get("sim") and п["sim"].get("толпа_s0_2") is not None
             and not п["sim"].get("why_not")]
        if len(т) >= 5 and statistics.median(т) > 10:
            толпа_кош.append(адрес)

    # ---- наши (ретро, как п.2б)
    наши: dict = {}
    for к in T.читать(T.каталог_задачи("nashi")):
        адрес = (к.get("строка") or {}).get("address")
        if not адрес:
            continue
        # Все первые покупки 7 дней выше порога (как в п.8: 43 источника с n >= 5).
        наши[адрес] = T.пересчёт_порога(к.get("покупки") or [], адрес)
    группы_ист = {з["address"]: з["группа_п5"] for з in json.loads(
        (КОРЕНЬ / "data" / "podbivka" / "istochniki.json").read_text(encoding="utf-8"))["источники"]}

    # ---- п.17
    def строки_ист(словарь: dict, адреса: list) -> list:
        из_ = []
        for адр in адреса:
            пп = словарь.get(адр) or []
            с72 = стат([чп(п.get("sim"), "S0", 72) for п in пп])
            с150 = стат([чп(п.get("sim"), "S0", 150) for п in пп])
            с1 = стат([чп(п.get("sim"), "S1", 72) for п in пп])
            из_.append({"address": адр, "S0_72": с72, "S0_150": с150, "S1_72": с1,
                        "группа": группы_ист.get(адр), "покупок": len(пп)})
        return из_

    шапка = ("| источник | группа | n | S+0/72 ср | ус. 5 % | в плюс | S+0/150 ср | ус. 5 % | в плюс | S+1/72 ср (для сверки) |",
             "|---|---|---|---|---|---|---|---|---|---|")

    def таблица(заг: str, строки: list) -> list:
        т = [f"### {заг}", "", *шапка]
        for x in sorted(строки, key=lambda x: -(x["S0_72"].get("ср") or -1e9) if x["S0_72"].get("n") else 1e9):
            а72, а150 = x["S0_72"], x["S0_150"]
            т.append(f"| {x['address'][:8]} | {x['группа'] or '—'} | {а72.get('n', 0)} | "
                     + " | ".join(я(а72).split(" | ")[1:]) + " | "
                     + " | ".join(я(а150).split(" | ")[1:]) + " | "
                     + (f"{x['S1_72']['ср']:+.2f}" if x["S1_72"].get("n") else "—") + " |")
        return т + [""]

    ст_наши = строки_ист(наши, sorted(наши))
    n5 = [x for x in ст_наши if x["S0_72"].get("n", 0) >= 5]
    md += таблица(f"п.17а Наши источники с n >= 5 (S+0/72): {len(n5)} из {len(наши)} файлов", n5)
    md += таблица(f"п.17б Наши источники, все с файлом ({len(ст_наши)}; источников в списке 133)", ст_наши)
    ст_канд = строки_ист(чужие, канд)
    md += таблица("п.17в 14 кандидатов (все первые покупки, оба порога)", ст_канд)
    итог["п17"] = {"наши": ст_наши, "кандидаты": ст_канд, "наших_файлов": len(наши), "наших_n5": len(n5)}

    # ---- п.18
    def покупки_группы(имя: str) -> list:
        if имя == "не продал":
            return [п for пп in чужие.values() for п in пп if п.get("продал_в_окне") is False]
        if имя == "толпа > 10":
            return [п for а_ in толпа_кош for п in чужие[а_]]
        return [п for а_ in канд for п in чужие.get(а_, [])]

    md += ["### п.18 «S+0 или ничего»: входы S+1 с ценой входа <= цена S+0 × k, выход +72", "",
           "Цена S+1 к S+0 -- из выручки симулятора на одном выходе (приближение, см. шапку скрипта). "
           "S+0 по определению вход по цене сразу после источника: отсечённых 0, строка -- для сравнения.", "",
           "| группа | вход | порог | всего с числом | оставлено n | доля отсечённых | среднее | усечённое 5 % | в плюс |",
           "|---|---|---|---|---|---|---|---|---|"]
    итог["п18"] = {}
    for имя in ("не продал", "толпа > 10", "14 кандидатов"):
        пп = [п for п in покупки_группы(имя) if п.get("sim") and not п["sim"].get("why_not")]
        с0 = стат([чп(п["sim"], "S0", 72) for п in пп])
        md.append(f"| {имя} | S+0 | — | {с0.get('n', 0)} | {с0.get('n', 0)} | 0% | "
                  + " | ".join(я(с0).split(" | ")[1:]) + " |")
        итог["п18"][имя] = {"S0": с0}
        с_числом = [п for п in пп if чп(п["sim"], "S1", 72) is not None]
        отн = {п["signature"]: отношение_цены(п["sim"]) for п in с_числом}
        без_отн = sum(1 for v in отн.values() if v is None)
        сall = стат([чп(п["sim"], "S1", 72) for п in с_числом])
        md.append(f"| {имя} | S+1 все | — | {len(с_числом)} | {сall.get('n', 0)} | 0% | "
                  + " | ".join(я(сall).split(" | ")[1:]) + " |")
        итог["п18"][имя]["S1_все"] = сall
        for k in (1.03, 1.05):
            ост = [п for п in с_числом if отн[п["signature"]] is not None and отн[п["signature"]] <= k]
            с = стат([чп(п["sim"], "S1", 72) for п in ост])
            доля_отс = 1 - len(ост) / len(с_числом) if с_числом else 0
            md.append(f"| {имя} | S+1 | ×{k:.2f} | {len(с_числом)} | {len(ост)} | {100 * доля_отс:.0f}% | "
                      + " | ".join(я(с).split(" | ")[1:]) + " |")
            итог["п18"][имя][f"S1_{k}"] = {**с, "отсечено": round(доля_отс, 3), "без_цены": без_отн}
        отн_v = sorted(v for v in отн.values() if v is not None)
        if отн_v:
            итог["п18"][имя]["P1_P0_медиана"] = round(statistics.median(отн_v), 4)
    md += ["", f"Кошельков «толпа > 10» (медиана толпы s0..s0+2 > 10, n >= 5): {len(толпа_кош)}.", ""]
    for имя, v in итог["п18"].items():
        if v.get("P1_P0_медиана"):
            md.append(f"- {имя}: медиана цены входа S+1 к S+0 = ×{v['P1_P0_медиана']}")
    md.append("")
    Path(а.out_md).write_text("\n".join(md), encoding="utf-8")
    Path(а.out_json).write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    md.append(f"Исключено как пылевая цена S+0 (режим «цена свопа», > {ПЫЛЬ_ПП} п.п.): {len(set(ПЫЛЬ))} покупок.")
    Path(а.out_md).write_text("\n".join(md), encoding="utf-8")
    print(f"п.17: наших файлов {len(наши)}, n>=5 {len(n5)}; кандидатов {len(ст_канд)}; толпа>10 кошельков {len(толпа_кош)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
