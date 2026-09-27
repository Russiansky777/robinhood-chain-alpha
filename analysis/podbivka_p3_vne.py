#!/usr/bin/env python3
"""Подбивка: п.3 -- дни вне окна отбора (до 24.09 00:00Z) по второму проходу
(data/podbivka/koshelki_7d: верхние 100 и 14 кандидатов, 7 дней), офлайн.

По кошельку: первые покупки выше порога (пересчёт курса), разделённые на «вне
окна» (раньше 24.09) и «в окне»; S+0/72 и S+1/72 -- n, среднее, усечённое без
ceil(5 % n) лучших, медиана, доля в плюс. Ограничение прохода: до 20 самых
свежих покупок на порог, поэтому у активных кошельков дни вне окна урезаны.
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_m4_svod as M  # noqa: E402
import podbivka_p5_p13 as P13  # noqa: E402
import podbivka_tablicy as T  # noqa: E402

КОРЕНЬ = T.КОРЕНЬ
ГРАНЬ_ОТБОРА = 1790208000  # 24.09 00:00Z


def main() -> int:
    канд = [x["address"] for x in json.loads((КОРЕНЬ / "data" / "podbivka" / "candidates_2026-09-27.json")
                                             .read_text(encoding="utf-8"))["верхние"]]
    кош: dict = {}
    for f in sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "koshelki_7d" / "*.json"))):
        if Path(f).name.startswith("_"):
            continue
        к = json.loads(Path(f).read_text(encoding="utf-8"))
        а = (к.get("строка") or {}).get("address")
        if а and not к.get("why_not") and not (к.get("скан") or {}).get("why_not"):
            кош[а] = T.пересчёт_порога(к.get("покупки") or [], а)

    def части(пп):
        return ([п for п in пп if (п.get("blockTime") or 0) < ГРАНЬ_ОТБОРА],
                [п for п in пп if (п.get("blockTime") or 0) >= ГРАНЬ_ОТБОРА])

    def стр(пп, e):
        return P13.строка(M.стат([P13.чп(п.get("sim"), e, 72) for п in пп]))

    md = ["# Подбивка: п.3 -- дни вне окна отбора (второй проход, 7 дней)", "",
          f"Кошельков с файлом: {len(кош)} из 100. Вне окна -- первые покупки раньше 24.09 00:00Z. "
          "До 20 самых свежих покупок на порог: у активных кошельков дни вне окна урезаны. "
          "Ячейка: n | среднее | усечённое | медиана | в плюс, выход +72.", "",
          "## Сводно", "",
          "| группа | часть | S+0: n | ср | ус. | мед | в плюс | S+1: n | ср | ус. | мед | в плюс |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for имя, адреса in (("верхние 100", list(кош)), (f"кандидаты ({len(канд)})", [a for a in канд if a in кош])):
        вне = [п for a in адреса for п in части(кош[a])[0]]
        вок = [п for a in адреса for п in части(кош[a])[1]]
        md.append(f"| {имя} | вне окна | {стр(вне, 'S0')} | {стр(вне, 'S1')} |")
        md.append(f"| {имя} | в окне | {стр(вок, 'S0')} | {стр(вок, 'S1')} |")
    md += ["", "## Кандидаты по одному", "",
           "| кандидат | часть | S+0: n | ср | ус. | мед | в плюс | S+1: n | ср | ус. | мед | в плюс |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in канд:
        if a not in кош:
            md.append(f"| {a[:8]} | нет файла второго прохода | | | | | | | | | | |")
            continue
        вне, вок = части(кош[a])
        md.append(f"| {a[:8]} | вне окна | {стр(вне, 'S0')} | {стр(вне, 'S1')} |")
        md.append(f"| {a[:8]} | в окне | {стр(вок, 'S0')} | {стр(вок, 'S1')} |")
    md.append("")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-27_p3_vne.md").write_text("\n".join(md), encoding="utf-8")
    print(f"п.3 вне окна: кошельков {len(кош)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
