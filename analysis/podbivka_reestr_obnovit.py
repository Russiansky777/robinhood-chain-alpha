#!/usr/bin/env python3
"""Обновить реестр адресов из живой конфигурации Code-1 (data/sources_live.json). Офлайн.

ЗАЧЕМ (распорядок 06:30Z). Группы в data/podbivka/arhiv_adresa.json отставали: 04.10 в живой
конфигурации Code-1 уже стоят konveyer (BCagckXe, ALL1V7x5), cand2 с 7JVQMwRj, cand1_03
(6qudAN2k, DYAn4XpA), cand2_03 (DsqRyTUh), krug_001 (AFmiexHw, 5YRgrP3m) -- а в реестре были
прежние метки. Здесь ТОРГОВЫЕ метки берутся из их файла, а наши собственные пометки
(log_only, 543, 133, «снайперские источники», sniper_src и т.п.) остаются как были.

Файл Code-1 читается с его ветки заранее (git show ... > data/podbivka/code1_sources_live.json)
или из data/sources_live.json, если он уже на нашей ветке. Ничего не пишется, кроме
data/podbivka/arhiv_adresa.json и data/podbivka/istochniki_torguyuschie.json (список для
суточного прохода).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ТОРГОВЫЕ = ("leader", "batch5", "lane_s0", "cand1", "cand1_03", "cand1_05", "cand2", "cand2_03",
            "cand3", "konveyer", "krug_001", "sniper_src", "kandidaty")
НАШИ_ПОМЕТКИ = ("log_only", "543", "133", "снайперские источники", "sniper_src")


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--fayl", default="", help="путь к sources_live.json (по умолчанию ищется сам)")
    а = р.parse_args()
    кандидаты = [Path(а.fayl)] if а.fayl else [
        П / "code1_sources_live.json", КОРЕНЬ / "data" / "sources_live.json"]
    for ф in кандидаты:
        if ф.exists():
            живой = json.loads(ф.read_text(encoding="utf-8"))
            откуда = str(ф.relative_to(КОРЕНЬ))
            break
    else:
        print("sources_live.json не найден -- реестр не меняю", flush=True)
        return 1
    реестр_ф = П / "arhiv_adresa.json"
    д = json.loads(реестр_ф.read_text(encoding="utf-8"))
    адреса = д["адреса"]
    их: dict = {}
    for г, v in (живой.get("groups") or {}).items():
        if г not in ТОРГОВЫЕ:
            continue
        for a in (v.get("addresses") or []):
            их.setdefault(a, set()).add(г)
    изменено, добавлено = [], []
    for a, гр in их.items():
        было = set((адреса.get(a) or {}).get("группы") or [])
        наши = {x for x in было if x in НАШИ_ПОМЕТКИ}
        стало = sorted(наши | гр)
        if a not in адреса:
            адреса[a] = {"группы": стало}
            добавлено.append(a)
        elif sorted(было) != стало:
            адреса[a]["группы"] = стало
            изменено.append((a, sorted(было), стало))
    # У КОГО ТОРГОВАЯ МЕТКА ПРОПАЛА ИЗ ЖИВОЙ КОНФИГУРАЦИИ -- СНИМАЕМ ЕЁ, КРОМЕ sniper_src:
    # эта пометка наша (снайперы по деньгам), в живой конфигурации она тоже есть, но у других
    # адресов, и снимать её по их файлу значило бы терять свой же разбор.
    снимаемые = tuple(x for x in ТОРГОВЫЕ if x != "sniper_src")
    снято = []
    for a, v in адреса.items():
        было = set(v.get("группы") or [])
        лишние = {x for x in было if x in снимаемые} - (их.get(a) or set())
        if лишние:
            v["группы"] = sorted(было - лишние)
            снято.append((a, sorted(лишние)))
    д["адреса"] = адреса
    д["откуда"] = (д.get("откуда") or "") + f" | торговые группы из {откуда} " \
                                            f"({живой.get('generated_utc')}), сверено {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}"
    реестр_ф.write_text(json.dumps(д, ensure_ascii=False, indent=1), encoding="utf-8")
    торг = sorted(a for a, v in адреса.items()
                  if {x for x in (v.get("группы") or []) if x in ТОРГОВЫЕ})
    (П / "istochniki_torguyuschie.json").write_text(json.dumps(
        {"_зачем": "адреса всех торговых групп Code-1 (включая kandidaty, sniper_src, krug_001, "
                   "konveyer) -- список источников для суточного прохода архива",
         "откуда": откуда, "generated_utc": живой.get("generated_utc"),
         "адресов": len(торг), "адреса": торг}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"реестр: адресов {len(адреса)}, торговых {len(торг)}; добавлено {len(добавлено)}, "
          f"метки изменены у {len(изменено)}, снято у {len(снято)}", flush=True)
    for a, б, с in изменено[:12]:
        print(f"  {a[:8]}: {б} -> {с}", flush=True)
    for a, л in снято[:12]:
        print(f"  снято у {a[:8]}: {л}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
