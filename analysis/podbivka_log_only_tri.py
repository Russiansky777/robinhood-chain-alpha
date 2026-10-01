#!/usr/bin/env python3
"""Три сильнейших log_only из архива через правило кандидатов. Офлайн, по готовому архиву vne11.

Кошельки 4vw54BmA / ardinRsN / CyaE1Vxv (верх лога суток 30.09 06Z). Правило кандидатов -- как в
analysis/podbivka_kandidaty_vne.py: отбор по n ≥ 20, среднее ≥ +2 п.п. и медиана > 0; здесь те же порог и ячейка
применяются к ВНЕ выборки (28.09 17Z → 30.09 06Z, архив vne11_* с моделью Pump AMM v6), вход «конец слота»
(S0_дно), билет 0.3, выход +108, п.п. чистыми. Pump AMM входит в счёт (v6).
Выход: docs/podbivka_2026-10-01_log_only_tri.md.
"""
from __future__ import annotations

import glob
import gzip
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_kandidaty_vne as K  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ТРИ = ("4vw54BmA", "ardinRsN", "CyaE1Vxv")


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    адр = json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    цель = {a: v for a, v in адр.items() if a[:8] in ТРИ}
    файлы = sorted(glob.glob(str(П / "arhiv_den" / "vne11_*.json.gz")))
    сс, видел = [], set()
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        for с in д.get("сигналы") or []:
            if с["signature"] in видел or not (K.ВНЕ_С <= K.ts(с) < K.ВНЕ_ДО) or с["trader"] not in цель:
                continue
            видел.add(с["signature"])
            сс.append(с)
    md = ["# Подбивка: три сильнейших log_only через правило кандидатов (вне выборки 28.09 17Z – 30.09 06Z)", "",
          f"Архив: {', '.join(Path(f).name for f in файлы)} (модель Pump AMM v6). Вход «конец слота» (S0_дно), "
          "билет 0.3, выход +108, п.п. чистыми. Ячейка -- котировка WSOL, правило окна pyg7, модель посчитана; "
          "Pump AMM входит в счёт. Порог правила кандидатов: n ≥ 20, среднее ≥ +2 п.п., медиана > 0.", "",
          "| кошелёк | группы | сигналов | в ячейке | n | среднее, п.п. | медиана, п.п. | в плюсе | порог |",
          "|---|---|---|---|---|---|---|---|---|"]
    строки = []
    for a in sorted(цель, key=lambda a: ТРИ.index(a[:8])):
        L = [с for с in сс if с["trader"] == a]
        яч = [с for с in L if K.в_ячейке(с) or K.в_ячейке_amm(с)]
        v = [K.пп(с, "108") for с in яч]
        v = [x for x in v if x is not None]
        if v:
            ср, мед = statistics.mean(v), statistics.median(v)
            прошёл = len(v) >= K.N_МИН and ср >= K.ПОРОГ_ПП and мед > 0
            пл = sum(1 for x in v if x > 0) / len(v) * 100
            md.append(f"| `{a[:8]}` | {', '.join((цель[a] or {}).get('группы') or [])} | {len(L)} | {len(яч)} | "
                      f"{len(v)} | {ср:+.2f} | {мед:+.2f} | {пл:.0f} % | "
                      f"{'прошёл' if прошёл else 'НЕ прошёл'} |")
            строки.append({"адрес": a, "сигналов": len(L), "в_ячейке": len(яч), "n": len(v), "среднее": round(ср, 2),
                           "медиана": round(мед, 2), "в_плюсе": round(пл, 1), "прошёл": прошёл})
        else:
            md.append(f"| `{a[:8]}` | {', '.join((цель[a] or {}).get('группы') or [])} | {len(L)} | {len(яч)} | 0 | — | — | — | нет ячеек |")
            строки.append({"адрес": a, "сигналов": len(L), "в_ячейке": len(яч), "n": 0, "прошёл": None})
    md += ["", "Строкой:", ""]
    for с in строки:
        if с["n"]:
            md.append(f"* `{с['адрес'][:8]}` -- {'проходит' if с['прошёл'] else 'НЕ проходит'} правило кандидатов: "
                      f"n {с['n']}, среднее {с['среднее']:+.2f} п.п., медиана {с['медиана']:+.2f} п.п., "
                      f"в плюсе {с['в_плюсе']:.0f} %.")
        else:
            md.append(f"* `{с['адрес'][:8]}` -- ячеек нет, правило не применимо.")
    md += ["", "Оговорка: это итог нашего входа за кошельком (конец его слота, билет 0.3), а не итог самого кошелька. "
           "Сильные числа лога суток 30.09 -- это его собственные входы по режиму 1, другая мера.", ""]
    out = КОРЕНЬ / "docs" / "podbivka_2026-10-01_log_only_tri.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(out)
    print(out.name, json.dumps(строки, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
