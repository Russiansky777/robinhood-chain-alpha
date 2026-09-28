#!/usr/bin/env python3
"""Подбивка: PumpApi Historical Replay и поток против наших данных -- сводка (офлайн).

Вход: data/podbivka/pumpapi/arhiv/*.json (выборки из часовых файлов архива; имя
файла -- час НАЧАЛА, проверено по timestamp событий), celi.json, potok_*.json,
m4 (резервы и счёт свопов после источника).
  1. покрытие: доля наших подписей (покупки 133 и 543 за 25.09, сделки полосы
     27.09) в архиве -- по программе пула и группе; только часы, которые скачаны;
  2. резервы: quoteInPool архива против резерва котировки m4 после той же сделки;
  3. свопы пула в слотах s0+1 и s0+2 (buy/sell по минту) против счёта m4;
  4. наши кошельки: покупки в архиве, которых нет в наших файлах, и наоборот;
  5. поток: событий в секунду, поля, задержка приёма к timestamp события.
"""
from __future__ import annotations

import calendar
import collections
import glob
import json
import statistics
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka" / "pumpapi"
PR = {"6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "кривая pump.fun",
      "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
      "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
      "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
      "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2",
      "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
      "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
      "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
      "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
      "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4"}


def main() -> int:
    ц = json.loads((П / "celi.json").read_text(encoding="utf-8"))["цели"]
    ev, по_минту, наши_ev, часы = {}, collections.defaultdict(list), [], []
    for f in sorted(glob.glob(str(П / "arhiv" / "*.json"))):
        д = json.loads(Path(f).read_text(encoding="utf-8"))
        if д.get("ошибка") or д.get("http") != 200:
            continue
        y, m, d, h = (int(x) for x in д["файл"].split("/"))
        часы.append(calendar.timegm((y, m, d, h, 0, 0)))
        for e in д["цели"]:
            ev[e["signature"]] = e
        for e in д["минты_m4"]:
            if e.get("mint"):
                по_минту[e["mint"]].append(e)
        наши_ev += д["наши_кошельки"]

    def в_часах(bt):
        return bt and any(ч <= bt < ч + 3600 for ч in часы)
    окно = {s: x for s, x in ц.items() if в_часах(x.get("blockTime"))}
    md = ["# Подбивка: PumpApi -- архив и поток против наших данных", "",
          f"Скачано часов архива: {len(часы)} (имя файла -- час начала; проверено по timestamp событий). "
          f"Наших подписей в этих часах: {len(окно)}, найдено в архиве: {sum(1 for s in окно if s in ev)}.", "",
          "## 1. Покрытие наших сделок", "", "| программа пула (наша) | найдено / всего | доля |", "|---|---|---|"]
    c, f = collections.Counter(), collections.Counter()
    for s, x in окно.items():
        к = PR.get(x.get("программа"), "прочие") if x.get("программа") else "не известна (без симулятора)"
        c[к] += 1
        f[к] += s in ev
    for к, n in c.most_common():
        md.append(f"| {к} | {f[к]} / {n} | {100 * f[к] / n:.0f}% |")
    g, gf = collections.Counter(), collections.Counter()
    for s, x in окно.items():
        g[x["группа"]] += 1
        gf[x["группа"]] += s in ev
    md += ["", "| группа | найдено / всего |", "|---|---|"] + [f"| {к} | {gf[к]} / {n} |" for к, n in g.items()]
    md += ["", "Найденные по pool / action архива: " + ", ".join(
        f"{к} {n}" for к, n in collections.Counter(f"{e.get('pool')}/{e.get('action')}" for s, e in ev.items()
                                                  if s in окно).most_common()), ""]
    # 2. резервы
    m4 = {}
    for fl in glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "postobr" / "m4_*.jsonl")):
        for l in open(fl, encoding="utf-8"):
            r = json.loads(l)
            if r["signature"] in окно and not r.get("why_not"):
                m4[r["signature"]] = r
    отн, пары = [], 0
    for s, r in m4.items():
        e = ev.get(s)
        рез = (r.get("доп") or {}).get("резерв_s0")
        if not e or not рез or e.get("quoteInPool") is None:
            continue
        пары += 1
        отн.append((float(e["quoteInPool"]) * 1e9 / float(рез) - 1) * 100)
    md += ["## 2. Резерв котировки после сделки источника: архив (quoteInPool) против m4", ""]
    if отн:
        о = sorted(отн)
        md += [f"Пар: {пары}. Отклонение архива от m4, %: медиана {statistics.median(о):+.3f}, "
               f"p10 {о[int(0.1 * len(о))]:+.3f}, p90 {о[int(0.9 * len(о))]:+.3f}; |отклонение| < 0.1 %: "
               f"{sum(1 for x in о if abs(x) < 0.1)} из {len(о)}.", ""]
    else:
        md += ["Пар нет.", ""]
    # 3. свопы s0+1, s0+2
    совп, всего, разн = 0, 0, []
    for s, r in m4.items():
        x = окно[s]
        св = ((r.get("доп") or {}).get("путь_цены") or {}).get("свопов") or {}
        s0 = x.get("slot")
        if not s0 or x.get("mint") not in по_минту:
            continue
        for k, dk in (("s1", 1), ("s2", 2)):
            if k not in св:
                continue
            а = sum(1 for e in по_минту[x["mint"]] if e.get("block") == s0 + dk and e.get("action") in ("buy", "sell"))
            всего += 1
            совп += а == св[k]
            разн.append(а - св[k])
    md += ["## 3. Свопы пула в слотах s0+1 и s0+2: архив против m4 (наша история пула)", "",
           f"Сравнений: {всего}; совпало: {совп}" + (f" ({100 * совп / всего:.0f}%); разница архив − m4: медиана "
                                                   f"{statistics.median(разн):+.1f}, архив больше в {sum(1 for x in разн if x > 0)}, "
                                                   f"меньше в {sum(1 for x in разн if x < 0)}" if всего else "") + ".", ""]
    # 4. наши кошельки
    наши_адр = {x["address"] for x in json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki.json")
                                                 .read_text(encoding="utf-8"))["источники"]}
    наши_sig = {s for s, x in ц.items() if x["группа"] == "наши 133"}
    покупки_арх = {e["signature"]: e for e in наши_ev if e.get("action") == "buy" and e.get("txSigner") in наши_адр}
    нет_у_нас = [e for s, e in покупки_арх.items() if s not in наши_sig]
    md += ["## 4. Покупки наших 133 в архиве против наших файлов (те же часы)", "",
           f"Покупок наших 133 в архиве (txSigner -- наш, action buy): {len(покупки_арх)}; из них в наших файлах: "
           f"{len(покупки_арх) - len(нет_у_нас)}; нет у нас: {len(нет_у_нас)} (по pool: " + ", ".join(
               f"{к} {n}" for к, n in collections.Counter(e.get("pool") for e in нет_у_нас).most_common(6)) + ").",
           f"Наших покупок 133 в этих часах: {sum(1 for s, x in окно.items() if x['группа'] == 'наши 133')}; в архиве: "
           f"{sum(1 for s, x in окно.items() if x['группа'] == 'наши 133' and s in ev)}.", ""]
    # 5. поток
    for fl in sorted(glob.glob(str(П / "potok_*.json"))):
        д = json.loads(Path(fl).read_text(encoding="utf-8"))
        з = sorted(д.get("задержки_мс") or [])
        md += ["## 5. Поток wss://stream.pumpapi.io (раннер GitHub Actions, не Франкфурт)", "",
               f"{д['событий']} событий за {д['секунд']} с ({д['событий'] / д['секунд']:.0f}/с); ошибка: {д['ошибка']}. "
               "По pool / action: " + ", ".join(f"{к} {n}" for к, n in sorted(д["по_pool_action"].items(),
                                                                           key=lambda kv: -kv[1])[:10]) + ".",
               (f"Задержка приёма к timestamp события (каждое 50-е, {len(з)} шт.), мс: медиана {statistics.median(з):.0f}, "
                f"p10 {з[int(0.1 * len(з))]:.0f}, p90 {з[int(0.9 * len(з))]:.0f}. Часы раннера и сервера не сверены -- "
                "оценка." if з else ""),
               "Поля событий: " + ", ".join(sorted(д.get("поля") or {})) + ".", ""]
    (КОРЕНЬ / "docs" / "podbivka_2026-09-28_pumpapi.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
