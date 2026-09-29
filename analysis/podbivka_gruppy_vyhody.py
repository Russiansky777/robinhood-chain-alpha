#!/usr/bin/env python3
"""Подбивка: ранние выходы / билет 0.3 / конец слота по группам Code-1 -- архив PumpApi (pyg6_*). Офлайн.

Группы и их живые настройки -- data/vps_health_nl.txt ветки Code-1 (29.09 01:14Z, только чтение):
leader 0.5 SOL / порог 3 / держим 150; batch5 и lane_s0 0.3 / 2 / 108; sniper_src 0.3 / 2 / 12 (lane_trades false).
Адреса групп -- data/podbivka/gruppy_code1.json, снайперы -- data/podbivka/istochniki_code1_kandidaty.json.
Сигнал -- первая покупка источника от порога группы, котировка SOL. Модель режима 1 архива: входы
S0 (сразу за ним), S0_дно (конец слота), S1; выходы +6…+150 слотов; билеты 0.3 и 0.5.
Пулы: кривая pump.fun и LaunchLab -- для вывода; Pump AMM -- не для вывода (лишний остаток E, архив видит
не все свопы). Выход: docs/podbivka_2026-09-29_gruppy_vyhody.md.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import statistics
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
WSOL = "So11111111111111111111111111111111111111112"
ЖИВЫЕ = {"leader": (0.5, 3.0, 150), "batch5": (0.3, 2.0, 108), "lane_s0": (0.3, 2.0, 108), "sniper_src": (0.3, 2.0, 12)}
ВЫХОДЫ = (6, 12, 24, 36, 72, 108, 150)
ВХОДЫ = (("S0", "сразу за ним"), ("S0_дно", "конец слота"), ("S1", "S+1"))
ПУЛЫ = (("pump", "кривая pump.fun"), ("raydium-launchpad", "LaunchLab"), ("pump-amm", "Pump AMM (не для вывода)"))


def ячейка(v: list) -> str:
    v = [x for x in v if x is not None]
    if not v:
        return "—"
    return f"{statistics.mean(v):+.1f} / {statistics.median(v):+.1f} / {100 * sum(1 for x in v if x > 0) / len(v):.0f}%"


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--prefiks", default="pyg6")
    а = р.parse_args()
    гр = json.loads((КОРЕНЬ / "data" / "podbivka" / "gruppy_code1.json").read_text(encoding="utf-8"))["groups"]
    кошельки = {г: set((гр.get(г) or {}).get("addresses") or {}) for г in ("leader", "batch5", "lane_s0")}
    кошельки["sniper_src"] = set(json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki_code1_kandidaty.json")
                                            .read_text(encoding="utf-8")).get("снайперы") or [])
    файлы = sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / f"{а.prefiks}_*.json.gz")))
    сиг, видел, ошибки = [], set(), []
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        ошибки += [e.split(":")[0] for e in (д.get("счёт") or {}).get("ошибки") or []]
        for с in д.get("сигналы") or []:
            if с["signature"] in видел or (с.get("модель") or {}).get("why_not") or с.get("quoteMint") != WSOL:
                continue
            видел.add(с["signature"])
            сиг.append(с)
    пп = lambda с, k: (с["модель"].get("пп") or {}).get(k)  # noqa: E731
    дни = sorted({Path(f).name.split("_")[1][:10] for f in файлы})
    md = ["# Подбивка: ранние выходы / билет / конец слота по группам Code-1 -- архив PumpApi", "",
          f"Прогон {а.prefiks}: суток {len(файлы)} (начала суток: {', '.join(дни)}); недочитанные часы: "
          f"{', '.join(sorted(set(ошибки))) or 'нет'}. Сигнал -- первая покупка источника от порога группы, котировка SOL "
          "(«первая» -- по сумме ног транзакции). Модель режима 1 архива, п.п. чистыми. Ячейка: среднее / медиана / в плюсе. "
          "Живые настройки групп -- data/vps_health_nl.txt ветки Code-1 (29.09 01:14Z); столбец живого удержания -- жирным. "
          "Секунды: +72 ≈ 19 с, +108 ≈ 29 с, +150 ≈ 41 с (метки архива, docs/podbivka_2026-09-29_lane_s0.md). "
          "Ничего не рекомендуется.", ""]
    for г, (билет, порог, держим) in ЖИВЫЕ.items():
        сс_г = [с for с in сиг if с["trader"] in кошельки[г] and (с.get("sol") or 0) >= порог]
        md += [f"## {г}: живой билет {билет}, порог {порог:g} SOL, держим {держим} слотов; сигналов {len(сс_г)}", ""]
        for p, имя in ПУЛЫ:
            сс = [с for с in сс_г if с["pool"] == p]
            if not сс:
                continue
            md += [f"### {имя}: {len(сс)} сигналов, кошельков {len({с['trader'] for с in сс})}", "",
                   "| вход | билет | " + " | ".join((f"**+{h}**" if h == держим else f"+{h}") for h in ВЫХОДЫ) + " |",
                   "|---|---|" + "---|" * len(ВЫХОДЫ)]
            for вход, ви in ВХОДЫ:
                for б in (0.3, 0.5):
                    md.append(f"| {ви} | {б}{' (живой)' if б == билет else ''} | " +
                              " | ".join(ячейка([пп(с, f'{вход}|{б}|{h}') for с in сс]) for h in ВЫХОДЫ) + " |")
            md.append("")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-29_gruppy_vyhody.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
