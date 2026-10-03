#!/usr/bin/env python3
"""Сколько сигналов в сутки даёт источник на своём пороге входа. Офлайн, по суточным den_*.

ЗАЧЕМ (задание владельца 03.10, п.4). У тех, кто «держится», живых сделок 1--8: подъём билета
опирается на архив, а не на свою торговлю. Это число отвечает, сколько сделок подъём вообще
может дать: сколько ПЕРВЫХ покупок источника в сутки проходит его порог входа.

Порог входа = билет группы (Правило 14, слово владельца 02.10): lane_s0 0.5, batch5 0.3,
cand1 0.1, cand1_03 0.3, cand1_05 0.5, cand2 0.1, cand3 0.1, leader 3, konveyer 0.1.
Источник в нескольких группах -- берётся наименьший порог (его сигналы видит та группа).

Берутся файлы data/podbivka/arhiv_den/den_*.json.gz: суточный проход идёт с --porog-dop 0 по
этим 55 адресам, поэтому в них видны ВСЕ первые покупки источника, а не только от 2 SOL.
Выход: docs/podbivka_<дата>_signalov_v_sutki.md, data/podbivka/signalov_v_sutki_<дата>.json.
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

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ПОРОГ = {"lane_s0": 0.5, "batch5": 0.3, "cand1": 0.1, "cand1_03": 0.3, "cand1_05": 0.5,
         "cand2": 0.1, "cand3": 0.1, "leader": 3.0, "konveyer": 0.1}


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--metka", default="2026-10-03")
    р.add_argument("--sdelok-do", type=int, default=8, help="верх живых сделок (кого показывать)")
    а = р.parse_args()
    св = json.loads((П / "masshtab_svod.json").read_text(encoding="utf-8"))["ряды"]
    цель = {a: v for a, v in св.items()
            if v["вердикт"] == "держится" and v["сделок"] <= а.sdelok_do}
    по: dict = {a: collections.defaultdict(list) for a in цель}
    файлы = sorted(glob.glob(str(П / "arhiv_den" / "den_*.json.gz")))
    for f in файлы:
        сут = Path(f).name.replace(".json.gz", "").split("_")[-1][:10]
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        for x in д.get("сигналы") or []:
            a = x.get("trader")
            if a in по:
                по[a][сут].append(float(x.get("sol") or 0))
        del д
    ряды = {}
    for a, v in цель.items():
        гр = [g for g in v["группы"] if g in ПОРОГ]
        порог = min((ПОРОГ[g] for g in гр), default=None)
        сут = по[a]
        на_пороге = ([sum(1 for s in сп if s >= порог) for сп in сут.values()]
                     if порог is not None else [])
        от2 = [sum(1 for s in сп if s >= 2.0) for сп in сут.values()]
        ряды[a] = {"группы": v["группы"], "группы_с_порогом": гр, "порог": порог,
                   "сделок_живых": v["сделок"], "суток_с_данными": len(сут),
                   "в_сутки_на_пороге": (round(statistics.median(на_пороге), 1) if на_пороге else None),
                   "в_сутки_на_пороге_среднее": (round(statistics.mean(на_пороге), 1) if на_пороге else None),
                   "в_сутки_от_2": (round(statistics.median(от2), 1) if от2 else None),
                   "мин_билет": v.get("мин_билет")}
    md = [f"# Сколько сигналов в сутки даёт источник на своём пороге входа ({а.metka})", "",
          f"Только те, кто «держится» в таблице масштаба и у кого живых сделок не больше "
          f"{а.sdelok_do}: подъём билета у них опирается на архив, и это число говорит, сколько "
          f"сделок подъём может дать. Суточные файлы `den_*` ({len(файлы)} суток, проход идёт с "
          f"`--porog-dop 0` по этим адресам -- видны все первые покупки, а не только от 2 SOL). "
          f"Порог входа -- билет группы (Правило 14); в нескольких группах -- наименьший порог. "
          f"Ничего не рекомендуется.", "",
          "| источник | группы | живых сделок | порог входа, SOL | сигналов в сутки на пороге: "
          "медиана (среднее) | в сутки ≥ 2 SOL | суток с данными | мин. выгодный билет |",
          "|---|---|---|---|---|---|---|---|"]
    for a, v in sorted(ряды.items(), key=lambda kv: -(kv[1]["сделок_живых"])):
        md.append(f"| `{a[:8]}` | {', '.join(v['группы']) or '—'} | {v['сделок_живых']} | "
                  + (f"{v['порог']:g}" if v["порог"] is not None else "— (нет группы)") + " | "
                  + (f"**{v['в_сутки_на_пороге']:.1f}** ({v['в_сутки_на_пороге_среднее']:.1f})"
                     if v["в_сутки_на_пороге"] is not None else "—") + " | "
                  + (f"{v['в_сутки_от_2']:.1f}" if v["в_сутки_от_2"] is not None else "—")
                  + f" | {v['суток_с_данными']} | {v['мин_билет'] or '—'} |")
    md += [""]
    out = КОРЕНЬ / "docs" / f"podbivka_{а.metka}_signalov_v_sutki.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(out)
    дт = П / f"signalov_v_sutki_{а.metka}.json"
    дт.write_text(json.dumps({"суток_файлов": len(файлы), "пороги": ПОРОГ, "ряды": ряды},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(дт)
    print(out.name, "источников", len(ряды), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
