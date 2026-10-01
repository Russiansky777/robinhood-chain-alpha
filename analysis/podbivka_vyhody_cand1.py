#!/usr/bin/env python3
"""Встречные выходы для cand1: +108 против «выйти, когда он продаёт» против стопа −30 %. Офлайн, по архиву.

Вход:
  * data/podbivka/sliv_vhod_2026-10-01.json -- 73 сделки полосы 30.09 06Z → 01.10 06Z (выгрузка Code-1, Правило 13);
  * data/podbivka/arhiv_den/vyhody_2026-09-30T06.json.gz -- тот же суточный архив с ДЛИННЫМ окном (6200 слотов) по
    пулам наших сделок (цели -- подписи покупки, продажи и сделки источника).

Счёт. База у всех трёх выходов одна: модель архива от покупки ИСТОЧНИКА, вход «конец слота» (S0_дно), билет -- наш
фактический, издержки 0.002 SOL на круг, Pump AMM -- v6. Разница только в горизонте H:
  * «+108» -- H = 108 (удержание полосы);
  * «он продаёт» -- H до первой продажи источника в ряду пула после его покупки (трейдер ряда = источник);
  * «стоп −30 %» -- H до первого события ВНУТРИ удержания (слоты до s0+107), где цена пула упала на 30 % и ниже
    от цены входа; если такого падения не было -- стоп не сработал, выход тот же +108 (отмечено столбцом).
Рядом -- факт по цепи из выгрузки (итог_po_cepi_sol в п.п. билета), чтобы видеть, где модель и факт расходятся.
Выход: docs/podbivka_2026-10-02_vyhody_cand1.md, data/podbivka/vyhody_cand1_2026-10-01.json.
"""
from __future__ import annotations

import gzip
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_arhiv_den as A  # noqa: E402

КОРЕНЬ = A.КОРЕНЬ
П = КОРЕНЬ / "data" / "podbivka"
СТОП = 0.70


def цена(e: dict):
    с = A.ст(e)
    return (с[0] / с[1]) if (с and с[1]) else None


def разбор(с: dict, цели: dict, ряды: dict) -> dict:
    из_ = {"cid": с["cid"], "utc": с["utc"], "группа": с["group"], "источник": с["source"], "минт": с["mint"],
           "билет": с.get("билет_sol") or с.get("size_sol"), "s_plus": с.get("s_plus"),
           "факт_пп": (с["итог_po_cepi_sol"] / (с.get("билет_sol") or с["size_sol"]) * 100)
           if с.get("итог_po_cepi_sol") is not None else None}
    e_ист = цели.get(с["source_sig"])
    if not e_ист or e_ист.get("action") != "buy" or not e_ист.get("poolId"):
        return {**из_, "why_not": "сделки источника нет в архиве как покупки"}
    ряд = ряды.get(e_ист["poolId"]) or []
    i0 = next((i for i, e in enumerate(ряд) if e.get("signature") == с["source_sig"]), None)
    if i0 is None:
        return {**из_, "why_not": "покупка источника не найдена в ряду пула"}
    s0 = e_ист["block"]
    из_.update(пул=e_ист.get("pool"), s0=s0, событий_после=len(ряд) - i0 - 1,
               слотов_в_ряду=(ряд[-1].get("block") or s0) - s0)
    # H до первой продажи источника
    h_прод = None
    for e in ряд[i0 + 1:]:
        if e.get("action") == "sell" and e.get("трейдер") == с["source"]:
            h_прод = (e.get("block") or s0) - s0 + 1
            break
    # H до стопа −30 % от цены входа (вход -- последнее событие слота s0)
    вх = [e for e in ряд[i0:] if (e.get("block") or 0) <= s0 and цена(e)]
    p0 = цена(вх[-1]) if вх else цена(e_ист)
    h_стоп, стоп_сработал = None, False
    if p0:
        for e in ряд[i0 + 1:]:
            if (e.get("block") or 0) > s0 + 107:      # стоп смотрим только внутри удержания до +108
                break
            p = цена(e)
            if p and p / p0 <= СТОП:
                h_стоп = max(1, (e.get("block") or s0) - s0 + 1)
                стоп_сработал = True
                break
    если_нет = 108
    h_стоп = h_стоп if стоп_сработал else если_нет
    H = sorted({108, h_прод or 108, h_стоп})
    A.БИЛЕТЫ, A.ВЫХОДЫ = (из_["билет"],), tuple(h for h in H if h >= 1)
    м = A.модель(e_ист, ряд[i0:])
    пп = м.get("пп") or {}
    def взять(h):
        return пп.get(f"S0_дно|{из_['билет']}|{h}")
    из_.update(модель_why_not=м.get("why_not"), модель_pump_amm=м.get("модель_pump_amm"),
               h_продажа_источника=h_прод, h_стоп=h_стоп if стоп_сработал else None, стоп_сработал=стоп_сработал,
               пп_108=взять(108), пп_он_продаёт=взять(h_прод) if h_прод else None,
               пп_стоп=взять(h_стоп), p0=p0)
    return из_


def св(v: list) -> str:
    v = [x for x in v if x is not None]
    if not v:
        return "| 0 | — | — | — |"
    return (f"| {len(v)} | {statistics.median(v):+.1f} | {statistics.fmean(v):+.1f} | "
            f"{sum(1 for x in v if x > 0) / len(v) * 100:.0f} % |")


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    сд = json.loads((П / "sliv_vhod_2026-10-01.json").read_text(encoding="utf-8"))
    арх = json.loads(gzip.decompress((П / "arhiv_den" / "vyhody_2026-09-30T06.json.gz").read_bytes()))
    цели = {e["signature"]: e for e in арх["цели"] if e.get("signature")}
    ряды = арх["ряды_целей"]
    ряды_все = [разбор(с, цели, ряды) for с in сд["сделки"]]
    ок = [r for r in ряды_все if not r.get("why_not") and not r.get("модель_why_not") and r.get("пп_108") is not None]
    cand = [r for r in ок if r["группа"] == "cand1"]
    q = [r for r in cand if r["источник"].startswith("6qudAN2k")]

    md = ["# Подбивка: встречные выходы cand1 -- +108 против «он продаёт» против стопа −30 % (30.09 06Z – 01.10 06Z)", "",
          f"Сделок в выгрузке: {len(ряды_все)}; в счёт вошли {len(ок)} (из них cand1 {len(cand)}, источник "
          f"`6qudAN2k` {len(q)}). Архив: `vyhody_2026-09-30T06` -- окно 6200 слотов по пулам наших сделок, "
          f"строк {арх['счёт']['строк']}, ошибок {len(арх['счёт']['ошибки'])}.", "",
          "База одна у всех трёх выходов: модель архива от покупки источника, вход «конец слота», билет -- наш "
          "фактический, 0.002 SOL издержек на круг, Pump AMM по модели v6. Разница только в горизонте. Стоп "
          "считается от цены входа и только внутри удержания (слоты до s0+107): «стоп −30 %» -- это то же удержание "
          "+108, но с досрочным выходом на падении; где падения не было, столбец повторяет +108.", ""]

    md += ["## A. Три выхода", "", "| набор | выход | n | медиана, п.п. | среднее, п.п. | в плюсе |", "|---|---|---|---|---|---|"]
    for имя, набор in (("cand1", cand), ("из них `6qudAN2k`", q), ("все группы", ок)):
        md.append(f"| **{имя}** | +108 {св([r['пп_108'] for r in набор])}")
        md.append(f"|  | он продаёт {св([r['пп_он_продаёт'] for r in набор])}")
        md.append(f"|  | стоп −30 % {св([r['пп_стоп'] for r in набор])}")
        md.append(f"|  | факт по цепи (+108) {св([r['факт_пп'] for r in набор])}")
    сраб = sum(1 for r in ок if r["стоп_сработал"])
    есть_прод = sum(1 for r in ок if r["h_продажа_источника"])
    md += ["", f"Стоп −30 % сработал в {сраб} сделках из {len(ок)}. Продажа источника нашлась в окне у "
           f"{есть_прод} из {len(ок)}; у остальных «он продаёт» не считается (в окне 6200 слотов он не продал).", ""]

    md += ["## Б. По сделкам cand1", "",
           "| время | источник | минт | билет | S+ | +108, п.п. | он продаёт: H / п.п. | стоп: H / п.п. | факт, п.п. |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(cand, key=lambda r: r["utc"]):
        оп = f"{r['h_продажа_источника']} / {r['пп_он_продаёт']:+.1f}" if r["пп_он_продаёт"] is not None else "— / —"
        ст_ = f"{r['h_стоп']} / {r['пп_стоп']:+.1f}" if r["стоп_сработал"] and r["пп_стоп"] is not None else "не сработал"
        md.append(f"| {r['utc'][5:16]} | `{r['источник'][:8]}` | `{r['минт'][:8]}` | {r['билет']} | {r['s_plus']} | "
                  f"{r['пп_108']:+.1f} | {оп} | {ст_} | " + (f"{r['факт_пп']:+.1f} |" if r["факт_пп"] is not None else "— |"))

    нет = [r for r in ряды_все if r.get("why_not") or r.get("модель_why_not")]
    md += ["", "## В. Не вошли в счёт", "", f"Сделок: {len(нет)}.", "", "| время | группа | источник | почему |", "|---|---|---|---|"]
    for r in sorted(нет, key=lambda r: r["utc"]):
        md.append(f"| {r['utc'][5:16]} | {r['группа']} | `{r['источник'][:8]}` | "
                  f"{r.get('why_not') or r.get('модель_why_not')} |")
    md.append("")

    out = КОРЕНЬ / "docs" / "podbivka_2026-10-02_vyhody_cand1.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(out)
    дт = П / "vyhody_cand1_2026-10-01.json"
    дт.write_text(json.dumps({"что": "встречные выходы: +108 / продажа источника / стоп −30 %", "ряды": ряды_все},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(дт)
    print(out.name, "в счёт", len(ок), "cand1", len(cand), "6qud", len(q), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
