#!/usr/bin/env python3
"""Широкая выборка сделок кривой: разные тарифы и Token-2022 -- к калибровке п.1.

Зачем. Модель кривой уже сверена с цепью точно, но на 22 718 сделках ВСЕГО 33 минтов, и
тариф там один на все -- 0.0125. Если у части кривых тариф протокола или тариф создателя
другой, модель с единым тарифом на них соврёт. Этот проход читает заданные часы архива и
берёт случайную выборку сделок кривой ПО МНОГИМ минтам -- с состоянием, суммами и тарифом,
чтобы модель/факт считалась не на 33 минтах, а на тысячах.

Выборка -- резервуарная: поток читается один раз, память не растёт.

Только чтение архива. Выход: data/podbivka/krivaya_obrazcy_<метка>.json.gz.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import io
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_zapuski as ZP   # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
КРИВЫЕ = {"pump", "raydium-launchpad", "meteora-launchpad"}


def main() -> int:
    import subprocess          # noqa: PLC0415

    import requests            # noqa: PLC0415
    try:
        import zstandard       # noqa: PLC0415
    except ModuleNotFoundError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"],
                       check=True)
        import zstandard       # noqa: PLC0415
    import podbivka_arhiv_den as AD   # noqa: PLC0415
    import podbivka_run as R          # noqa: PLC0415

    р_ = argparse.ArgumentParser()
    р_.add_argument("--s", required=True, help="начало окна, YYYY-MM-DDTHH")
    р_.add_argument("--chasov", type=int, default=2)
    р_.add_argument("--skolko", type=int, default=20000, help="размер выборки")
    р_.add_argument("--seed", type=int, default=20261009)
    р_.add_argument("--metka", default="")
    а = р_.parse_args()
    рнд = random.Random(а.seed)
    выборка: list = []
    видел = 0
    счёт: collections.Counter = collections.Counter()

    def читать_час(поток) -> None:
        nonlocal видел
        for стр in io.TextIOWrapper(
                zstandard.ZstdDecompressor().stream_reader(поток.raw),
                encoding="utf-8", errors="ignore"):
            счёт["строк"] += 1
            ам = ZP.р_action.search(стр)
            if not ам or ам.group(1) not in ("buy", "sell"):
                continue
            тм = ZP.р_tip.search(стр)
            if not тм or тм.group(1) not in КРИВЫЕ:
                continue
            qm = ZP.р_qmint.search(стр)
            if not qm or qm.group(1) != ZP.WSOL:
                continue
            сост = ZP.состояние(стр)
            ток = ZP.число(ZP.р_tam.search(стр))
            кв = ZP.число(ZP.р_qam.search(стр))
            if not (сост and ток and кв):
                continue
            мм = ZP.р_mint.search(стр)
            ряд = {"действие": ам.group(1), "пул": тм.group(1),
                   "минт": мм.group(1) if мм else None,
                   "x": сост[0], "y": сост[1], "ток": ток, "кв": кв,
                   "тариф": ZP.число(ZP.р_fee.search(стр))}
            бм = ZP.р_block.search(стр)
            if бм:
                ряд["блок"] = int(бм.group(1))
            пм = ZP.р_sig.search(стр)
            if пм:
                ряд["подпись"] = пм.group(1)
            видел += 1
            счёт[f"сделок:{ряд['пул']}"] += 1
            # резервуар: каждая сделка попадает с вероятностью skolko/видел
            if len(выборка) < а.skolko:
                выборка.append(ряд)
            else:
                j = рнд.randrange(видел)
                if j < а.skolko:
                    выборка[j] = ряд

    for ч in ZP.часы(а.s, а.chasov):
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        for попытка in range(3):
            try:
                о = AD.открыть_час(requests, url, ч)
            except Exception as exc:     # noqa: BLE001
                счёт[f"ошибка_{type(exc).__name__}"] += 1
                break
            with о as поток:
                код = getattr(поток, "status_code", 200)
                if код != 200:
                    счёт[f"http_{код}"] += 1
                    break
                try:
                    читать_час(поток)
                except Exception as exc:    # noqa: BLE001
                    счёт[f"обрыв_{type(exc).__name__}"] += 1
                    if попытка < 2:
                        continue
                    счёт["обрыв_часов"] += 1
            break
        счёт["часов"] += 1
        print(f"  {ч}: строк {счёт['строк']}, сделок кривых {видел}, "
              f"в выборке {len(выборка)}", flush=True)
    тело = {"что": "широкая выборка сделок кривой для калибровки",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "параметры": vars(а), "счёт": dict(счёт), "сделок_видено": видел,
            "минтов_в_выборке": len({r.get("минт") for r in выборка}),
            "тарифы": dict(collections.Counter(
                round(r["тариф"], 6) if r.get("тариф") is not None else None
                for r in выборка)),
            "выборка": выборка}
    путь = П / f"krivaya_obrazcy{('_' + а.metka) if а.metka else ''}.json.gz"
    with gzip.open(путь, "wt", encoding="utf-8") as ф:
        json.dump(тело, ф, ensure_ascii=False)
    R.записано(путь)
    print(f"готово: {путь.name}, минтов {тело['минтов_в_выборке']}, "
          f"тарифы {json.dumps(тело['тарифы'], ensure_ascii=False)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
