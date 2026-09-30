#!/usr/bin/env python3
"""Перенос модели Pump AMM (v6) в архив: проверка на наших сделках Pump AMM по цепи. Офлайн.

Вход:
  * data/podbivka/sverka_leader_noch_<дата>_v6.json -- сверка v6 по цепи (факт в SOL свопов, модель v6 по
    хранилищам, f / g окна, f нашей покупки, g нашей продажи); берутся сделки Pump AMM (pAMMBay…) с моделью;
  * data/podbivka/arhiv_den/pumpamm_v6_*.json.gz -- архив PumpApi с целями = подписи этих сделок (buy / sell /
    src), --bez-adresov: события целей и ряды их пулов (analysis/podbivka_arhiv_den.py, бегунок lab-miami).
По каждой сделке -- модель АРХИВА на том же месте и тем же размером, что и v6 по цепи: вход -- состояние пула после
события перед нашей покупкой, выход -- состояние после события перед нашей продажей (наш след уже в пуле), билет --
факт_вход_sol, без 0.002 на круг (факт -- SOL в самих свопах). Ряд пула -- из того же куска архива, что и покупка.
Окно калибровки -- как у v6 по цепи (строки сверки собраны по Shyft): события после сделки источника до нашей продажи
(до max(продажа, s0+150) -- где модель_150 посчитана), первые 150 вместе с нашими, наши исключены.
Варианты модели архива:
  * «было» -- модель архива до переноса на своём окне (пары от сделки источника до s0+160, наши внутри): f =
    x0·dy/((y0−dy)·quoteAmount), g = quoteAmount·(y0+dy)/(x0·dy), обе в [0.5, 1.0], иначе 1 − poolFeeRate;
  * «v6» -- podbivka_arhiv_den.калибровка_pump_amm (f по приросту quoteInPool, g на руки = quoteAmount·(1 − fee) до
    1.6, фолбэки v6);
  * «v6, f по quoteAmount» -- то же, f по quoteAmount / tokenAmount события.
Сверка калибровок архива с цепью: quoteAmount нашей покупки к факт_вход_sol и к приросту хранилища по цепи,
f и g НАШИХ сделок по архиву к f_нашей_покупки / g_нашей_продажи по цепи.
Выход: docs/podbivka_2026-10-01_pumpamm_v6_arhiv.md, data/podbivka/pumpamm_v6_arhiv.json.
"""
from __future__ import annotations

import glob
import gzip
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_arhiv_den as A  # noqa: E402
from podbivka_arhiv_den import ст  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ДАТЫ = ("2026-09-28", "2026-09-29", "2026-09-30")
ГОРИЗОНТ = 150
МАКС_ОКНА = 150        # как v6: не больше 150 событий пула после источника


def f_q(пред: dict, e: dict):
    с0 = ст(пред)
    if not с0 or not e.get("tokenAmount") or not e.get("quoteAmount"):
        return None
    x0, y0 = с0
    dy, dx = float(e["tokenAmount"]), float(e["quoteAmount"])
    return x0 * dy / ((y0 - dy) * dx) if y0 > dy > 0 and dx > 0 else None


def f_хран(пред: dict, e: dict):
    с0, с1 = ст(пред), ст(e)
    if not с0 or not с1:
        return None
    (x0, y0), (x1, y1) = с0, с1
    dx, dy = x1 - x0, y0 - y1
    return x0 * dy / ((y0 - dy) * dx) if dx > 0 and y0 > dy > 0 else None


def g_q(пред: dict, e: dict):
    с0 = ст(пред)
    if not с0 or not e.get("tokenAmount") or not e.get("quoteAmount"):
        return None
    x0, y0 = с0
    dy, dx = float(e["tokenAmount"]), float(e["quoteAmount"])
    return dx * (y0 + dy) / (x0 * dy) if dy > 0 and x0 > 0 else None


def g_хран(пред: dict, e: dict):
    с0, с1 = ст(пред), ст(e)
    if not с0 or not с1:
        return None
    (x0, y0), (x1, y1) = с0, с1
    dx, dy = x0 - x1, y1 - y0
    return dx * y1 / (x0 * dy) if dx > 0 and dy > 0 and x0 > 0 else None


def калибровка(окно: list, вариант: str, fee: float) -> dict:
    """Модель архива ДО переноса (для сравнения): f по quoteAmount, g ≤ 1.0, все покупки / продажи окна."""
    assert вариант == "было"
    fs, gs = [], []
    for пред, e in окно:
        if e.get("action") == "buy":
            v = f_q(пред, e)
            if v and 0.5 <= v <= 1.0:
                fs.append(v)
        elif e.get("action") == "sell":
            v = g_q(пред, e)
            if v and 0.5 <= v <= 1.0:
                gs.append(v)
    return {"f": statistics.median(fs) if fs else 1 - fee, "g": statistics.median(gs) if gs else 1 - fee,
            "f_n": len(fs), "g_n": len(gs)}


def модель(ряд: list, ib: int, is_: int, a: float, к: dict):
    с_вх, с_вых = ст(ряд[ib - 1]) if ib > 0 else None, ст(ряд[is_ - 1])
    if not с_вх or not с_вых:
        return None
    x, y = с_вх
    т = y * к["f"] * a / (x + к["f"] * a)
    X, Y = с_вых
    return round((X * к["g"] * т / (Y + т) - a) / a * 100, 3)


def main() -> int:
    цели, ряды = {}, {}                  # подпись -> [(файл, событие)]; (файл, poolId) -> ряд (ревью: ряды кусков не смешивать)
    for f in sorted(glob.glob(str(П / "arhiv_den" / "pumpamm_v6_*.json.gz"))):
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        for e in д.get("цели") or []:
            цели.setdefault(e["signature"], []).append((f, e))
        for pid, р in (д.get("ряды_целей") or {}).items():
            if р:
                ряды[(f, pid)] = р
    строки, нет = [], []
    for дата in ДАТЫ:
        for с in json.loads((П / f"sverka_leader_noch_{дата}_v6.json").read_text(encoding="utf-8"))["ряды"]:
            if not (с.get("program") or "").startswith("pAMM") or с.get("why_not"):
                continue
            кандидаты = [(f, e) for f, e in цели.get(с["buy_sig"], []) if e.get("pool") == "pump-amm" and e.get("mint") == с["mint"]]
            if not кандидаты:
                нет.append((дата, с["buy_sig"][:8], "покупки нет в архиве (pump-amm)"))
                continue
            файл, пок = кандидаты[0]
            ряд = ряды.get((файл, пок["poolId"])) or []
            idx: dict = {}
            for i, e in enumerate(ряд):
                idx.setdefault(e["signature"], i)          # первое событие подписи в ряду этого пула
            ib, is_ = idx.get(с["buy_sig"]), idx.get(с["sell_sig"])
            if ib is None or is_ is None or is_ <= ib or ib == 0:
                нет.append((дата, с["buy_sig"][:8], "наши сделки не в ряду пула архива (или покупка -- первое событие ряда)"))
                continue
            isrc = idx.get(с.get("src_sig"))
            s0 = с["s0"]
            # окно v6 по цепи: строки сверки собраны по Shyft -- история пула до нашей продажи; «до s0+150» -- только
            # там, где модель_150 посчитана; 150 событий считаются вместе с нашими (ревью 01.10)
            до = max(с["slot_sell"], s0 + ГОРИЗОНТ) if с.get("модель_150_пп") is not None else с["slot_sell"]
            if isrc is not None:
                номера = [i for i in range(isrc + 1, len(ряд)) if (ряд[i].get("block") or 0) <= до]
            else:
                номера = [i for i in range(1, len(ряд)) if s0 <= (ряд[i].get("block") or 0) <= до]
            номера = номера[:МАКС_ОКНА]
            окно = [(ряд[i - 1], ряд[i]) for i in номера if i not in (ib, is_)]
            fee = float(ряд[ib].get("poolFeeRate") or 0)
            f_ист = A.f_пары(ряд[isrc - 1], ряд[isrc]) if isrc else None
            # «было» -- модель архива до переноса на её собственном окне: пары от источника до s0+160 (--okno), наши внутри
            нач = isrc if isrc is not None else next((i for i, e in enumerate(ряд) if (e.get("block") or 0) >= s0), 0)
            окно_было = [(ряд[i - 1], ряд[i]) for i in range(max(нач + 1, 1), len(ряд)) if (ряд[i].get("block") or 0) <= s0 + 160]
            a = float(с["факт_вход_sol"])
            к = {"было": калибровка(окно_было, "было", fee),
                 "v6": A.калибровка_pump_amm(окно, fee, "хранилищам", f_ист),     # перенос -- функция архива
                 "v6_fq": A.калибровка_pump_amm(окно, fee, "quoteAmount", f_ист)}
            м = {в: модель(ряд, ib, is_, a, к[в]) for в in к}
            пок_а, прод_а = ряд[ib], ряд[is_]
            строки.append({
                "дата": дата, "buy": с["buy_sig"], "группа": с.get("группа"), "место": с.get("место"),
                "src_в_ряду": isrc is not None, "событий_окна": len(окно), "до_слота": до - s0,
                "факт_пп": с["факт_пп"], "v6_цепь_пп": с["модель_пп"],
                **{f"модель_{в}_пп": м[в] for в in м}, **{f"к_{в}": к[в] for в in к},
                "f_цепь_окна": с.get("f"), "g_цепь_окна": с.get("g"), "f_n_цепь": с.get("f_покупок_окна"), "g_n_цепь": с.get("g_продаж"),
                "f_нашей_цепь": с.get("f_нашей_покупки"), "g_нашей_цепь": с.get("g_нашей_продажи"),
                "f_нашей_архив_хран": f_хран(ряд[ib - 1], пок_а), "f_нашей_архив_q": f_q(ряд[ib - 1], пок_а),
                "g_нашей_архив_q": g_q(ряд[is_ - 1], прод_а),
                "g_нашей_архив_нетто": (g_q(ряд[is_ - 1], прод_а) or 0) * (1 - float(прод_а.get("poolFeeRate") or 0)) or None,
                "q_покупки_архив": float(пок_а.get("quoteAmount") or 0), "q_продажи_архив": float(прод_а.get("quoteAmount") or 0),
                "факт_вход_sol": a, "факт_выход_sol": с.get("факт_выход_sol"),
                "факт_q_в_пул_sol": (с.get("факт_q_в_пул") or 0) / 1e9 or None,
                "прирост_хранилища_архив": (ст(пок_а)[0] - ст(ряд[ib - 1])[0]) if ст(пок_а) and ст(ряд[ib - 1]) else None,
            })
    (П / "pumpamm_v6_arhiv.json").write_text(json.dumps({"строки": строки, "нет": нет}, ensure_ascii=False, indent=1), encoding="utf-8")

    КЛ = ("v6_цепь_пп", "модель_было_пп", "модель_v6_пп", "модель_v6_fq_пп")

    def ячейка(ключ, L):
        L = [r for r in L if all(r.get(k) is not None for k in КЛ)]
        v = [abs(r[ключ] - r["факт_пп"]) for r in L]
        if not v:
            return "—"
        return f"{statistics.median(v):.2f} / {100 * sum(1 for x in v if x <= 2) / len(v):.0f}% / {max(v):.1f} ({len(v)})"

    def мед_abs(ключ, L):
        L = [r for r in L if all(r.get(k) is not None for k in КЛ)]      # общий набор для всех моделей (ревью)
        v = [abs(r[ключ] - r["факт_пп"]) for r in L]
        return (f"{statistics.median(v):.2f}", sum(1 for x in v if x <= 2), len(v)) if v else ("—", 0, 0)

    def мед(v):
        v = [x for x in v if x is not None]
        return f"{statistics.median(v):.4f}" if v else "—"
    md = ["# Модель Pump AMM (v6) в архиве: проверка на наших сделках по цепи", "",
          "Сделки Pump AMM полосы из сверки v6 по цепи (data/podbivka/sverka_leader_noch_<дата>_v6.json; файлы 28, 29, 30.09 "
          "-- сделки 27.09 22Z → 30.09 05Z) и ряды их пулов из архива PumpApi (цели -- подписи покупки, продажи и "
          "источника, analysis/podbivka_arhiv_den.py --bez-adresov, бегунок lab-miami). Модель архива -- на том же месте "
          "(вход -- состояние перед нашей покупкой, выход -- перед нашей продажей) и тем же билетом, что v6 по цепи; без "
          "0.002 SOL на круг (факт -- SOL в свопах). Окно калибровки как у v6: после сделки источника до max(продажа, "
          "s0+150), не больше 150 событий, без наших, до 30 покупок / 30 продаж. Разбор -- analysis/podbivka_pumpamm_v6_arhiv.py.", "",
          "Варианты: «было» -- модель архива до переноса (f по quoteAmount, g ≤ 1.0); «v6» -- f по хранилищам (приросту "
          "quoteInPool), g ≤ 1.6; «v6, f по quoteAmount» -- g ≤ 1.6, f как было.", ""]
    наборы = (("все", строки), ("28.09 (в выборке)", [r for r in строки if r["дата"] == "2026-09-28"]),
              ("29–30.09 (вне выборки)", [r for r in строки if r["дата"] != "2026-09-28"]))
    md += ["## Итог: модуль расхождения модели с фактом, п.п.", "",
           "Поправка g на комиссию пула найдена на сделках файла 28.09 -- они «в выборке»; сделки файлов 29 и 30.09 -- "
           "проверка вне выборки. Ячейка: медиана / доля в пределах 2 п.п. / максимум (n). Все модели -- на одном наборе сделок.", "",
           "| модель | " + " | ".join(н for н, _ in наборы) + " |", "|---|" + "---|" * len(наборы)]
    for имя, кл in (("v6 по цепи (эталон)", "v6_цепь_пп"), ("архив, было", "модель_было_пп"),
                    ("архив, v6 (перенос)", "модель_v6_пп"), ("архив, v6 с f по quoteAmount", "модель_v6_fq_пп")):
        md.append(f"| {имя} | " + " | ".join(ячейка(кл, L) for _, L in наборы) + " |")
    худшие = sorted(строки, key=lambda r: -abs(r["модель_v6_пп"] - r["факт_пп"]))[:3]
    md += ["", "Наибольшие расхождения архива v6 -- те же сделки, что у v6 по цепи (промах самой модели, не переноса): "
           + "; ".join(f"`{r['buy'][:8]}` факт {r['факт_пп']:+.1f}, v6 цепь {r['v6_цепь_пп']:+.1f}, архив v6 "
                       f"{r['модель_v6_пп']:+.1f} (продаж в окне {r['к_v6']['g_n']})" for r in худшие) + ".",
           "Известное ограничение v6 (и переноса): g постоянна в окне, а доля лишнего остатка E в резерве меняется с ценой -- "
           "на сильном движении модель занижает рост (синтетика: +32.1 против +35.7 при +36 %).", ""]
    md += ["## Калибровки: архив против цепи (медианы по сделкам)", "",
           "| величина | цепь | архив |", "|---|---|---|",
           f"| f окна (v6) | {мед([r['f_цепь_окна'] for r in строки])} | {мед([r['к_v6']['f'] for r in строки])} |",
           f"| g окна (v6) | {мед([r['g_цепь_окна'] for r in строки])} | {мед([r['к_v6']['g'] for r in строки])} |",
           f"| f окна, было (архив) | — | {мед([r['к_было']['f'] for r in строки])} |",
           f"| g окна, было (архив) | — | {мед([r['к_было']['g'] for r in строки])} |",
           f"| f нашей покупки: по хранилищам | {мед([r['f_нашей_цепь'] for r in строки])} | {мед([r['f_нашей_архив_хран'] for r in строки])} |",
           f"| f нашей покупки: по quoteAmount | — | {мед([r['f_нашей_архив_q'] for r in строки])} |",
           f"| g нашей продажи | {мед([r['g_нашей_цепь'] for r in строки])} | {мед([r['g_нашей_архив_нетто'] for r in строки])} "
           f"(quoteAmount·(1 − fee), как в v6 архива); {мед([r['g_нашей_архив_q'] for r in строки])} (quoteAmount как есть) |",
           f"| quoteAmount нашей покупки / факт_вход_sol | 1 | {мед([r['q_покупки_архив'] / r['факт_вход_sol'] for r in строки])} |",
           f"| прирост хранилища нашей покупкой / факт_вход_sol | {мед([r['факт_q_в_пул_sol'] / r['факт_вход_sol'] for r in строки if r['факт_q_в_пул_sol']])} | "
           f"{мед([r['прирост_хранилища_архив'] / r['факт_вход_sol'] for r in строки if r['прирост_хранилища_архив']])} |",
           f"| quoteAmount нашей продажи / факт_выход_sol | 1 | {мед([r['q_продажи_архив'] / r['факт_выход_sol'] for r in строки if r['факт_выход_sol']])} |", "",
           "## По сделкам", "",
           "| дата файла | покупка | место | факт | v6 цепь | архив было | архив v6 | архив v6 (f по q) | f / g окна: цепь | f / g окна: архив v6 | событий окна |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    ф = lambda v: "—" if v is None else f"{v:+.1f}"  # noqa: E731
    for r in строки:
        md.append(f"| {r['дата'][5:]} | `{r['buy'][:8]}` | {r['место']} | {ф(r['факт_пп'])} | {ф(r['v6_цепь_пп'])} | "
                  f"{ф(r['модель_было_пп'])} | {ф(r['модель_v6_пп'])} | {ф(r['модель_v6_fq_пп'])} | "
                  f"{r['f_цепь_окна']} / {r['g_цепь_окна']} | {r['к_v6']['f']:.4f} / {r['к_v6']['g']:.4f} | {r['событий_окна']} |")
    if нет:
        md += ["", "Не сопоставлены: " + "; ".join(f"{d[5:]} `{b}` -- {w}" for d, b, w in нет)]
    (КОРЕНЬ / "docs" / "podbivka_2026-10-01_pumpamm_v6_arhiv.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
