#!/usr/bin/env python3
"""Подбивка: глубокие пулы под крупный билет -- кто из 655 адресов покупает в пулах с резервом ≥ 200 SOL. Офлайн.

Данные: архив PumpApi, прогон pyg9 (analysis/podbivka_arhiv_den.py с резервом пула и билетами 1 и 3 SOL):
  * 7 суток 21.09 17Z → 28.09 17Z (те же, что pyg8) и 18.09 15Z → 21.09 17Z (период ленты DBot до архива);
  * резерв -- котировка пула ПОСЛЕ события по полям архива (quoteInPool; у кривой -- настоящий quoteInPool, если
    архив его даёт, виртуальный не считается), в SOL-экв. по курсу котировки события; резерв ДО покупки =
    после − потраченное (SOL-экв.). Глубокий пул -- резерв до покупки ≥ --porog-rezerva (200) SOL.
Лента DBot (data/dbot_follow_trades_raw.json, 18.09–26.09, 67 источников): покупка источника сопоставляется с
событием архива по кошельку, минту и времени (|Δ| ≤ 120 с); у сопоставленной -- пул и резерв из архива.
Итог по архиву: сигналы модели режима 1 (первая покупка от 2 SOL; для 34 источников Code-1 -- правило окна 1800
слотов), вход «конец слота» (S0_дно), билеты 1 и 3 SOL, выходы +72 и +108; ячейка: среднее / медиана / в плюсе, п.п.
чистыми (минус 0.002 SOL на круг). Модель есть для кривой, LaunchLab, Pump AMM, CPMM, DAMM v1; DLMM, DAMM v2,
CLMM -- без модели. Выход: docs/podbivka_2026-09-29_glubokie_puly.md.
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import statistics
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
WSOL = "So11111111111111111111111111111111111111112"
USD = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
ИМЯ_ПУЛА = {"pump": "кривая pump.fun", "raydium-launchpad": "LaunchLab", "pump-amm": "Pump AMM", "raydium-cpmm": "CPMM",
            "meteora-damm-v1": "DAMM v1", "meteora-damm-v2": "DAMM v2", "meteora-dlmm": "DLMM", "raydium-clmm": "CLMM",
            "raydium-amm": "Raydium AMM v4", "meteora-dbc": "DBC"}
С_МОДЕЛЬЮ = {"pump", "raydium-launchpad", "pump-amm", "raydium-cpmm", "meteora-damm-v1"}


def котировка(q: str | None) -> str:
    return "SOL" if q == WSOL else USD.get(q or "", (q or "?")[:6])


def резерв_до(e: dict) -> float | None:
    """Резерв котировки до события, SOL-экв.: после − потраченное (покупка) / + полученное (продажа)."""
    r = e.get("резерв_sol")
    if r is None:
        return None
    s = e.get("sol_экв") if "sol_экв" in e else e.get("sol")
    if s is None:
        return None
    return r - s if e.get("action", "buy") == "buy" else r + s


def ячейка(v: list) -> str:
    v = [x for x in v if x is not None]
    if not v:
        return "—"
    return f"{statistics.mean(v):+.1f} / {statistics.median(v):+.1f} / {100 * sum(1 for x in v if x > 0) / len(v):.0f}% (n {len(v)})"


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--prefiks", default="pyg9")
    р.add_argument("--porog-rezerva", type=float, default=200.0)
    р.add_argument("--dopusk-s", type=float, default=120.0, help="окно сопоставления ленты DBot с архивом, секунд")
    а = р.parse_args()
    адреса = dict(json.loads((КОРЕНЬ / "data" / "podbivka" / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"])
    файлы = sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / f"{а.prefiks}_*.json.gz")))
    события, сигналы, ошибки, видел, видел_с = [], [], [], set(), set()
    часы_всего = []
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        часы_всего += д.get("часы") or []
        ошибки += [e.split(":")[0] for e in (д.get("счёт") or {}).get("ошибки") or []]
        for e in д.get("наши_события") or []:
            к = (e["signature"], e["trader"], e.get("poolId"))
            if к not in видел:
                видел.add(к)
                события.append(e)
        for с in д.get("сигналы") or []:
            if с["signature"] not in видел_с:
                видел_с.add(с["signature"])
                сигналы.append(с)
    покупки = [e for e in события if e.get("action") == "buy"]
    с_резервом = [e for e in покупки if e.get("резерв_sol") is not None]
    глуб = [e for e in с_резервом if (резерв_до(e) or 0) >= а.porog_rezerva]
    без_резерва = collections.Counter(e.get("pool") for e in покупки if e.get("резерв_sol") is None)
    # лента DBot -> архив
    лента = json.loads((КОРЕНЬ / "data" / "dbot_follow_trades_raw.json").read_text(encoding="utf-8"))
    по_ключу: dict = collections.defaultdict(list)
    for e in покупки:
        по_ключу[(e["trader"], e.get("mint"))].append(e)
    dbot = {"покупок": 0, "сопоставлено": 0, "глубоких": 0, "до_архива": 0}
    dbot_глуб: dict = collections.Counter()
    t_арх0 = min((e.get("timestamp") or 0) for e in покупки) / 1000 if покупки else 0
    for з in лента.values():
        r = з.get("record") or {}
        if r.get("type") != "buy":
            continue
        fw = (r.get("follow") or {}).get("wallet")
        mint = ((r.get("receive") or {}).get("info") or {}).get("contract")
        t = (r.get("createAt") or 0) / 1000
        dbot["покупок"] += 1
        if t < t_арх0:
            dbot["до_архива"] += 1
        канд = [e for e in по_ключу.get((fw, mint), []) if abs((e.get("timestamp") or 0) / 1000 - t) <= а.dopusk_s]
        if not канд:
            continue
        dbot["сопоставлено"] += 1
        e = min(канд, key=lambda e: abs((e.get("timestamp") or 0) / 1000 - t))
        if (резерв_до(e) or 0) >= а.porog_rezerva:
            dbot["глубоких"] += 1
            dbot_глуб[fw] += 1
    # сигналы с моделью в глубоких пулах
    сиг_глуб = [с for с in сигналы if (резерв_до(с) or 0) >= а.porog_rezerva and not (с.get("модель") or {}).get("why_not")]
    по_ист: dict = collections.defaultdict(list)
    for e in глуб:
        по_ист[e["trader"]].append(e)
    сиг_по_ист: dict = collections.defaultdict(list)
    for с in сиг_глуб:
        сиг_по_ист[с["trader"]].append(с)
    пп = lambda с, k: ((с.get("модель") or {}).get("пп") or {}).get(k)  # noqa: E731
    ряды = []
    for t, ee in по_ист.items():
        сс = сиг_по_ист.get(t, [])
        гр = ",".join(г for г in (адреса.get(t) or {}).get("группы", []) if г != "log_only") or "log_only"
        ряды.append({"t": t, "гр": гр, "n": len(ee), "dbot": dbot_глуб.get(t, 0),
                     "пулы": collections.Counter(ИМЯ_ПУЛА.get(e.get("pool"), e.get("pool")) for e in ee),
                     "кот": collections.Counter(котировка(e.get("quoteMint")) for e in ee),
                     "мед": statistics.median([e["sol_экв"] for e in ee if e.get("sol_экв") is not None] or [0]),
                     "рез": statistics.median([резерв_до(e) for e in ee]),
                     "сиг": len(сс), "яч": {(б, h): ячейка([пп(с, f"S0_дно|{б}|{h}") for с in сс])
                                             for б in (1.0, 3.0) for h in (72, 108)}})
    ряды.sort(key=lambda r: -r["n"])
    def кр(c: collections.Counter) -> str:
        return ", ".join(f"{k} {v}" for k, v in c.most_common(3)) + (" …" if len(c) > 3 else "")
    дни = sorted({ч[:10] for ч in часы_всего})
    md = ["# Подбивка: глубокие пулы под крупный билет -- кто из 655 адресов покупает в пулах с резервом ≥ "
          f"{а.porog_rezerva:g} SOL", "",
          f"Архив PumpApi, прогон {а.prefiks}: файлов суток {len(файлы)} ({дни[0] if дни else '—'} … {дни[-1] if дни else '—'}, "
          f"начала часов файлов); недочитанные часы: {', '.join(sorted(set(ошибки))) or 'нет'}. Покупок 655 адресов "
          f"{len(покупки)}, с резервом в архиве {len(с_резервом)}; глубоких (резерв до покупки ≥ {а.porog_rezerva:g} SOL-экв.) "
          f"{len(глуб)} у {len(по_ист)} адресов. Без резерва в архиве (по типу пула): "
          f"{', '.join(f'{ИМЯ_ПУЛА.get(k, k)} {v}' for k, v in без_резерва.most_common()) or 'нет'} -- они не классифицированы.",
          "",
          f"Лента DBot (18.09–26.09, 67 источников): покупок {dbot['покупок']}, из них до начала архива {dbot['до_архива']}; "
          f"сопоставлено с архивом по кошельку, минту и времени (±{а.dopusk_s:g} с) {dbot['сопоставлено']}, "
          f"из них в глубоких пулах {dbot['глубоких']} (столбец «DBot»).", "",
          "Итог -- модель режима 1 по сигналам источника в глубоких пулах (первая покупка от 2 SOL; для 34 источников "
          "Code-1 -- правило окна 1800 слотов), вход «конец слота», билет 1 и 3 SOL; ячейка: среднее / медиана / в плюсе, "
          "п.п. чистыми. Модели нет для DLMM, DAMM v2, CLMM -- там «—». Ничего не рекомендуется.", "",
          "| источник | группы | покупок в глубоких | DBot | тип пула | котировка | медиана покупки, SOL | медиана резерва, SOL | "
          "сигналов с моделью | 1 SOL +72 | 1 SOL +108 | 3 SOL +72 | 3 SOL +108 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in ряды:
        md.append(f"| `{r['t'][:8]}` | {r['гр']} | {r['n']} | {r['dbot']} | {кр(r['пулы'])} | {кр(r['кот'])} | {r['мед']:.2f} | "
                  f"{r['рез']:.0f} | {r['сиг']} | {r['яч'][(1.0, 72)]} | {r['яч'][(1.0, 108)]} | {r['яч'][(3.0, 72)]} | "
                  f"{r['яч'][(3.0, 108)]} |")
    вс = [с for с in сиг_глуб]
    md += [f"| **все** | | {len(глуб)} | {dbot['глубоких']} | {кр(collections.Counter(ИМЯ_ПУЛА.get(e.get('pool'), e.get('pool')) for e in глуб))} | "
           f"{кр(collections.Counter(котировка(e.get('quoteMint')) for e in глуб))} | "
           f"{statistics.median([e['sol_экв'] for e in глуб if e.get('sol_экв') is not None] or [0]):.2f} | "
           f"{statistics.median([резерв_до(e) for e in глуб] or [0]):.0f} | {len(вс)} | " +
           " | ".join(ячейка([пп(с, f"S0_дно|{б}|{h}") for с in вс]) for б in (1.0, 3.0) for h in (72, 108)) + " |", ""]
    (КОРЕНЬ / "docs" / "podbivka_2026-09-29_glubokie_puly.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
