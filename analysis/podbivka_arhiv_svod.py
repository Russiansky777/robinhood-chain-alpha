#!/usr/bin/env python3
"""Подбивка: сводка суток архива PumpApi (data/podbivka/arhiv_den/<метка>.json). Офлайн.

 A. Лог по кошелькам (замена живого лога): сигналов (покупок), первых покупок от
    2 SOL-экв., из них в модели (SOL-пул поддержанного типа), модель S+0 сразу за
    ним, 0.5 SOL, +12 и +72.
 B. Режим 1 по типу пула: вход S0 / S0_дно / S1 × выход +6/+12/+24/+36/+72/+150 ×
    билет 0.3 / 0.5; Pump AMM -- отдельно при f < 0.95 (нестандартная доля траты).
 C. Сделки полосы (цели): модель на том же месте по ряду пула из архива против
    факта (суммы наших свопов в архиве); g по всем продажам пула в окне против g по
    умолчанию (1 − poolFeeRate).
 D. Сверка с m4 (Helius) по подписям: S0 +6/+12/+72, конец слота +72, свопы s0+1/s0+2.
Ячейка: n | среднее | усеч. | медиана | p95 | макс | доля 5 % лучших.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from podbivka_arhiv_den import XYK, ст  # noqa: E402
from podbivka_hvosty import КОРЕНЬ, шапка, стат, я  # noqa: E402

ТИП = {"pump": "кривая pump.fun", "pump-amm": "Pump AMM", "raydium-launchpad": "LaunchLab",
       "raydium-cpmm": "Raydium CPMM", "meteora-damm-v1": "Meteora DAMM v1"}


def пп(с, вход="S0", a=0.5, H=72):
    return ((с.get("модель") or {}).get("пп") or {}).get(f"{вход}|{a}|{H}")


def раздел_a(д, имена) -> list:
    по = collections.defaultdict(lambda: {"покупок": 0, "продаж": 0, "первых2": 0})
    for e in д["наши_события"]:
        x = по[e["trader"]]
        x["покупок" if e["action"] == "buy" else "продаж"] += 1
        if e["action"] == "buy" and e.get("первая") and (e.get("sol_экв") or 0) >= д["порог_sol"]:
            x["первых2"] += 1
    сиг = collections.defaultdict(list)
    for с in д["сигналы"]:
        сиг[с["trader"]].append(с)
    md = ["## A. Лог по кошелькам (архив PumpApi)", "",
          f"Покупок наших адресов в архиве: {sum(x['покупок'] for x in по.values())} у {len(по)} адресов; первых от "
          f"{д['порог_sol']:g} SOL-экв.: {sum(x['первых2'] for x in по.values())}; из них в модели (SOL-пул pump / "
          f"pump-amm / raydium-cpmm / raydium-launchpad / meteora-damm-v1): {len(д['сигналы'])}.", "",
          "| кошелёк | группы | покупок | продаж | первых от 2 SOL | в модели | S+0 +12 (0.5): ср / мед / в плюс | S+0 +72 (0.5): ср / мед / в плюс |",
          "|---|---|---|---|---|---|---|---|"]

    def кр(xs):
        xs = [x for x in xs if x is not None]
        if not xs:
            return "—"
        return f"{sum(xs) / len(xs):+.1f} / {statistics.median(xs):+.1f} / {100 * sum(1 for x in xs if x > 0) / len(xs):.0f}%"
    for а, x in sorted(по.items(), key=lambda kv: -len(сиг.get(kv[0], []))):
        if not x["первых2"] and not сиг.get(а):
            continue
        н = имена.get(а) or {}
        md.append(f"| {(н.get('имя') or '')} {а[:8]} | {', '.join(н.get('группы') or [])} | {x['покупок']} | {x['продаж']} | "
                  f"{x['первых2']} | {len(сиг.get(а, []))} | {кр([пп(с, H=12) for с in сиг.get(а, [])])} | "
                  f"{кр([пп(с, H=72) for с in сиг.get(а, [])])} |")
    return md + [""]


def раздел_b(д) -> list:
    сиг = [с for с in д["сигналы"] if (с.get("модель") or {}).get("пп")]
    md = ["## B. Режим 1 по типу пула (архив)", "",
          "f и g -- медианы по всем покупкам / продажам пула в окне 160 слотов; нет -- 1 − poolFeeRate. Потолок.", ""]
    md += шапка(["пул", "вход", "билет", "выход", "x"])
    группы = [(т, [с for с in сиг if с["pool"] == п]) for п, т in ТИП.items()]
    amm = [с for с in сиг if с["pool"] == "pump-amm"]
    группы.insert(2, ("Pump AMM, f < 0.95", [с for с in amm if (с["модель"].get("f") or 1) < 0.95]))
    группы.insert(3, ("Pump AMM, f ≥ 0.95", [с for с in amm if (с["модель"].get("f") or 1) >= 0.95]))
    for имя, рр in группы:
        if not рр:
            continue
        for вход in ("S0", "S0_дно", "S1"):
            for a in (0.5, 0.3):
                for H in (6, 12, 24, 36, 72, 150):
                    if a == 0.3 and H not in (12, 72):
                        continue
                    md.append(f"| {имя} | {вход} | {a} | +{H} | {я(стат([пп(с, вход, a, H) for с in рр]))} |")
    return md + [""]


def раздел_c(д) -> list:
    п = КОРЕНЬ / "data" / "podbivka" / "sverka_noch_vhod_raw.json"
    if not п.exists() or not д.get("ряды_целей"):
        return []
    сделки = [x["polya"] for x in json.loads(п.read_text(encoding="utf-8"))["sdelki"]]
    цели = {e["signature"]: e for e in д["цели"]}
    md = ["## C. Сделки полосы: модель по ряду пула из архива против факта", "",
          "Вход -- состояние после события, предшествующего нашей покупке в ряду пула; выход -- после события, "
          "предшествующего нашей продаже; трата -- наша фактическая (quoteAmount покупки). Факт -- quoteAmount продажи к "
          "quoteAmount покупки (своп, без комиссии сети и чаевых).", "",
          "| покупка | пул | факт, п.п. | модель, g по окну | g окна (n) | модель, g = 1 − fee | f окна (n) | Code-1 итог, % |",
          "|---|---|---|---|---|---|---|---|"]
    расх_окно, расх_деф = [], []
    for с in сделки:
        b, s = цели.get(с.get("подпись_покупки")), цели.get(с.get("подпись_продажи"))
        if not b or not s:
            md.append(f"| {(с.get('подпись_покупки') or '')[:8]} | — | — | нет в архиве | | | | {с.get('итог_процент') or 0:+.1f} |")
            continue
        ряд = д["ряды_целей"].get(b.get("poolId")) or []
        ib = next((i for i, e in enumerate(ряд) if e["signature"] == b["signature"]), None)
        is_ = next((i for i, e in enumerate(ряд) if e["signature"] == s["signature"]), None)
        факт = (float(s["quoteAmount"]) / float(b["quoteAmount"]) - 1) * 100 if b.get("quoteAmount") else None
        if ib is None or is_ is None or ib == 0:
            md.append(f"| {b['signature'][:8]} | {b.get('pool')} | {факт:+.1f} | ряд неполон | | | | {с.get('итог_процент') or 0:+.1f} |")
            continue
        fee = float(b.get("poolFeeRate") or 0)
        fs, gs = [], []
        for пред, e in zip(ряд, ряд[1:]):
            с0 = ст(пред)
            if not с0 or not e.get("tokenAmount") or not e.get("quoteAmount") or e["signature"] in (b["signature"], s["signature"]):
                continue
            x0, y0 = с0
            dy, dx = float(e["tokenAmount"]), float(e["quoteAmount"])
            if e["action"] == "buy" and y0 > dy > 0:
                v = x0 * dy / ((y0 - dy) * dx)
                if 0.5 <= v <= 1.0:
                    fs.append(v)
            elif e["action"] == "sell" and x0 > 0:
                v = dx * (y0 + dy) / (x0 * dy)
                if 0.5 <= v <= 1.0:
                    gs.append(v)
        f = statistics.median(fs) if fs else 1 - fee
        g = statistics.median(gs) if gs else 1 - fee
        x, y = ст(ряд[ib - 1])
        a = float(b["quoteAmount"])
        кривая = b.get("pool") not in XYK
        if кривая:
            net = a / (1 + fee)
            т = y * net / (x + net)
        else:
            т = y * f * a / (x + f * a)
        X, Y = ст(ряд[is_ - 1])
        рез = {}
        for имя, gg in (("окно", g), ("деф", 1 - fee)):
            out = X * т / (Y + т) * (1 - fee) if кривая else X * gg * т / (Y + т)
            рез[имя] = (out / a - 1) * 100
        if факт is not None:
            расх_окно.append(факт - рез["окно"])
            расх_деф.append(факт - рез["деф"])
        md.append(f"| {b['signature'][:8]} | {b.get('pool')} | {факт:+.1f} | {рез['окно']:+.1f} | {g:.3f} ({len(gs)}) | "
                  f"{рез['деф']:+.1f} | {f:.3f} ({len(fs)}) | {с.get('итог_процент') or 0:+.1f} |")
    if расх_окно:
        md += ["", f"Медиана модуля расхождения факт − модель: g по окну {statistics.median(abs(x) for x in расх_окно):.2f} п.п., "
               f"g = 1 − fee {statistics.median(abs(x) for x in расх_деф):.2f} п.п. (n {len(расх_окно)}); средний знак "
               f"{statistics.mean(расх_окно):+.2f} / {statistics.mean(расх_деф):+.2f}."]
    return md + [""]


def раздел_d(д) -> list:
    m4 = {}
    for f in glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "postobr" / "m4_*.jsonl")):
        for l in open(f, encoding="utf-8"):
            r = json.loads(l)
            if not r.get("why_not"):
                m4[r["signature"]] = r
    пары = [(с, m4[с["signature"]]) for с in д["сигналы"] if с["signature"] in m4]
    if not пары:
        return []
    md = ["## D. Сверка с m4 (Helius, наш симулятор режима 1) по тем же подписям", "",
          f"Пар: {len(пары)}. Разница архив − m4, п.п. (0.5 SOL, потолок): медиана / p10 / p90 / |разница| < 1 п.п.", "",
          "| величина | n | медиана | p10 | p90 | < 1 п.п. |", "|---|---|---|---|---|---|"]

    def ряд_(имя, fa, fm):
        р = [fa(с) - fm(r) for с, r in пары if fa(с) is not None and fm(r) is not None]
        if not р:
            md.append(f"| {имя} | 0 | — | — | — | — |")
            return
        р.sort()
        md.append(f"| {имя} | {len(р)} | {statistics.median(р):+.2f} | {р[int(0.1 * len(р))]:+.2f} | "
                  f"{р[int(0.9 * len(р))]:+.2f} | {sum(1 for x in р if abs(x) < 1)} |")
    for H in (6, 12, 72, 150):
        ряд_(f"S0 +{H}", lambda с, H=H: пп(с, "S0", 0.5, H), lambda r, H=H: ((r.get("чистый_пп") or {}).get("S0") or {}).get(str(H)))
    ряд_("конец слота +72", lambda с: пп(с, "S0_дно", 0.5, 72),
         lambda r: ((r.get("доп") or {}).get("S0_конец_слота") or {}).get("72"))
    св_а, св_м, не = 0, 0, []
    for с, r in пары:
        св = ((r.get("доп") or {}).get("путь_цены") or {}).get("свопов") or {}
        for k in ("s1", "s2"):
            if k in св:
                if с["свопов"].get(k) == св[k]:
                    св_а += 1
                else:
                    не.append((с["signature"], k, с["свопов"].get(k), св[k]))
                св_м += 1
    md += ["", f"Свопы в слотах s0+1 / s0+2 (по poolId в архиве; m4 -- история хранилища): совпало {св_а} из {св_м}; "
           f"архив меньше в {sum(1 for *_, a, m in не if a < m)}, больше в {sum(1 for *_, a, m in не if a > m)}.", ""]
    (КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / "svopy_rashozhdenie.json").write_text(
        json.dumps([{"signature": s, "слот": k, "архив": a, "m4": m} for s, k, a, m in не], ensure_ascii=False), encoding="utf-8")
    return md + [""]


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--metka", required=True)
    а = р.parse_args()
    import gzip  # noqa: PLC0415
    п = КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / f"{а.metka}.json"
    д = json.loads(gzip.decompress(Path(f"{п}.gz").read_bytes()) if Path(f"{п}.gz").exists() else п.read_text(encoding="utf-8"))
    имена = json.loads((КОРЕНЬ / "data" / "podbivka" / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    md = [f"# Подбивка: архив PumpApi, сутки с {д['день']} -- лог и режим 1", "",
          f"Файлов: {д['счёт']['файлов']} из {len(д['часы'])}, строк {д['счёт']['строк']}; ошибки: {д['счёт']['ошибки'] or 'нет'}. "
          "Порядок внутри слота -- порядок файла (timestamp мс), оценка. Налог Token-2022 не учтён. 0.5 SOL -- главный "
          "билет, минус 0.002 SOL на круг.", ""]
    md += раздел_a(д, имена) + раздел_b(д) + раздел_c(д) + раздел_d(д)
    (КОРЕНЬ / "docs" / f"podbivka_arhiv_{а.metka}.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md[:40]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
