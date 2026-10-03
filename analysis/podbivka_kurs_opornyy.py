#!/usr/bin/env python3
"""Часовой курс SOL/USD по ОПОРНОМУ пулу SOL/USDC. Только чтение цепи.

ЗАЧЕМ (задание владельца 03.10, п.2). Стейбл переводится в SOL-экв как 1 USD по курсу
опорного пула того же часа. Сохранённый ряд курса детектора (data/podbivka/kurs_sol_usd.json)
кончается 26.09 17:11Z, а `podbivka_tablicy.курс_детектора` отдаёт число только не дальше 12 ч
от снимка -- поэтому в проходах архива с 27.09 ветка «USD-котировка» не включалась, и USDC-сделки
получали курс, выведенный из пар архива: медиана 0.0267 SOL за USDC против истинных ~0.0082,
то есть завышение в 3.2 раза. Здесь ряд достраивается по цепи.

Опорные пулы -- те же, что у c2_common.RateBook: REF_SOL_USDC_POOL и хранилище второго пула.
Курс из каждой сделки считает c2_common.rate_from_tx (своя формула не пишется).
Выход: data/podbivka/kurs_sol_usd_opornyy.json {"ряд": [{utc, usd_sol, n, откуда}], "счёт": ...}.
"""
from __future__ import annotations

import argparse
import calendar
import collections
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"


def utc(ts) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--s", required=True, help="с какого UTC: YYYY-MM-DDTHH:MM:SSZ")
    р.add_argument("--stranic", type=int, default=120, help="предел страниц подписей на пул")
    р.add_argument("--na-stranicu", type=int, default=3, help="сколько сделок читать на страницу")
    а = р.parse_args()
    с_ts = calendar.timegm(time.strptime(а.s, "%Y-%m-%dT%H:%M:%SZ"))
    уз = S.Узел()
    по_часу: dict = collections.defaultdict(list)
    счёт = collections.Counter()
    сбои: list = []
    with уз.на("helius"):
        for пул in (C.REF_SOL_USDC_POOL, C.REF2_SOL_USDC_WSOL_VAULT):
            до, страниц = None, 0
            while страниц < а.stranic:
                try:
                    стр = уз.подписи(пул, до=до, limit=1000)
                except Exception as exc:  # noqa: BLE001
                    сбои.append(f"{пул[:6]} страница {страниц}: {type(exc).__name__}")
                    break
                страниц += 1
                if not стр:
                    break
                счёт["подписей"] += len(стр)
                удачные = [з for з in стр if з.get("err") is None and з.get("blockTime")]
                # по часу: берём не больше --na-stranicu сделок на страницу, равномерно
                шаг = max(1, len(удачные) // max(1, а.na_stranicu))
                проба = удачные[::шаг][:а.na_stranicu]
                нужны = [з["signature"] for з in проба
                         if (з.get("blockTime") or 0) >= с_ts
                         and len(по_часу[(з["blockTime"] // 3600)]) < 3]
                if нужны:
                    пак = уз.пакет(нужны, {з["signature"]: з.get("blockTime") for з in проба})
                    for з in проба:
                        т = пак.get(з["signature"])
                        if not т:
                            счёт["не_прочитано"] += 1
                            continue
                        к = C.rate_from_tx(т)
                        if к is None:
                            счёт["без_курса"] += 1
                            continue
                        по_часу[з["blockTime"] // 3600].append(float(к))
                        счёт["курсов"] += 1
                посл = удачные[-1] if удачные else None
                if посл is None or (посл.get("blockTime") or 0) < с_ts or len(стр) < 1000:
                    break
                до = стр[-1]["signature"]
            счёт[f"страниц_{пул[:6]}"] = страниц
    ряд = [{"utc": utc(ч * 3600), "usd_sol": round(statistics.median(v), 6), "n": len(v),
            "откуда": "опорный пул SOL/USDC (c2_common.rate_from_tx), медиана часа"}
           for ч, v in sorted(по_часу.items()) if v]
    out = П / "kurs_sol_usd_opornyy.json"
    out.write_text(json.dumps({"_зачем": "часовой курс SOL/USD по опорному пулу SOL/USDC: стейбл -> SOL-экв",
                               "с_utc": а.s, "часов": len(ряд), "счёт": dict(счёт), "сбои": сбои[:20],
                               "ряд": ряд}, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    if ряд:
        print(f"{out.name}: часов {len(ряд)}, с {ряд[0]['utc']} по {ряд[-1]['utc']}, "
              f"курс {ряд[0]['usd_sol']} -> {ряд[-1]['usd_sol']}, "
              f"сделок с курсом {счёт['курсов']}, без курса {счёт['без_курса']}", flush=True)
    else:
        print(f"{out.name}: часов 0 -- курса не получено; счёт {dict(счёт)}; сбои {сбои[:3]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
