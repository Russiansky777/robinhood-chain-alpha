#!/usr/bin/env python3
"""Покрытие архивом PumpApi и путь цены после первого появления токена в списке.

Описательно: ни одного правила не подбирает, порогов не ищет, вердиктов не выносит.

Вход -- заказ от podbivka_trending_proverka.py (минт или пул -> метка первого появления).
Проход читает часы https://replay.pumpapi.io/Y/M/D/HH.jsonl.zst потоком (в облаке -- без
кэша; на lab-miami -- через PODB_ARHIV_KESH) и по каждому заказанному адресу собирает:

  * есть ли он в архиве вообще (и сколько событий) -- это и есть покрытие;
  * состояние пула на момент появления: последнее событие НЕ ПОЗЖЕ метки -- цена и
    ликвидность (quoteInPool для SOL-пулов, иначе помечается котировка);
  * цену на +1 / +5 / +15 минут: ближайшее событие не позже метки+шаг (если после метки
    событий не было вовсе -- None, так и пишется);
  * котировку пула и тип пула, чтобы не складывать SOL-пулы с долларовыми.

Строки отбираются дешёвым поиском mint/poolId регуляркой, json.loads -- только для
попавших в заказ (иначе час стоил бы минут процессора на разбор всего).

Выход: data/podbivka/trending_arhiv<метка>.json.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_arhiv_den as А  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ШАГИ = (60, 300, 900)
р_mint = re.compile(r'"mint":"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_pool = re.compile(r'"poolId":"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_ts = re.compile(r'"timestamp":(\d+)')


def цена_и_ликв(е: dict) -> tuple:
    """(цена котировки за токен, ликвидность в котировке) по состоянию ПОСЛЕ события."""
    q = е.get("quoteInPool")
    t = е.get("tokensInPool")
    if q is None or t is None:
        q, t = е.get("vQuoteInBondingCurve"), е.get("vTokensInBondingCurve")
    try:
        q, t = float(q), float(t)
    except (TypeError, ValueError):
        return (None, None)
    if q <= 0 or t <= 0:
        return (None, None)
    return (q / t, q)


def главное(а) -> int:
    import requests  # noqa: PLC0415
    import zstandard  # noqa: PLC0415

    зак = json.loads(Path(а.zakaz).read_text(encoding="utf-8"))["минты"]
    минты = {к for к, v in зак.items() if v.get("вид", "минт") == "минт"}
    пулы = {к for к, v in зак.items() if v.get("вид") == "пул"}
    метка_мс = {к: int(time.mktime(time.strptime(v["utc"], "%Y-%m-%dT%H:%M:%SZ")) * 1000)
                for к, v in зак.items()}
    состояние: dict = {к: {"событий": 0, "до": None, "шаги": {}, "котировка": None,
                           "пул": None, "poolId": None, "первое_событие_мс": None,
                           "последнее_событие_мс": None} for к in зак}

    т0 = time.mktime(time.strptime(а.s, "%Y-%m-%dT%H"))
    часы = [time.strftime("%Y/%m/%d/%H", time.gmtime(т0 + 3600 * k))
            for k in range(а.chasov)]
    счёт = {"строк": 0, "разобрано": 0, "часов": 0, "ошибки": []}
    нач = time.time()
    for ч in часы:
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        try:
            with А.открыть_час(requests, url, ч) as о:
                if о.status_code != 200:
                    счёт["ошибки"].append(f"{ч}: http {о.status_code}")
                    continue
                счёт["часов"] += 1
                дек = zstandard.ZstdDecompressor().stream_reader(о.raw)
                for стр in io.TextIOWrapper(дек, encoding="utf-8", errors="replace"):
                    счёт["строк"] += 1
                    ключ = None
                    м = р_mint.search(стр)
                    if м and м.group(1) in минты:
                        ключ = м.group(1)
                    else:
                        p = р_pool.search(стр)
                        if p and p.group(1) in пулы:
                            ключ = p.group(1)
                    if ключ is None:
                        continue
                    счёт["разобрано"] += 1
                    try:
                        е = json.loads(стр)
                    except ValueError:
                        continue
                    ts = е.get("timestamp")
                    if ts is None:
                        мт = р_ts.search(стр)
                        ts = int(мт.group(1)) if мт else None
                    if ts is None:
                        continue
                    с = состояние[ключ]
                    с["событий"] += 1
                    с["котировка"] = с["котировка"] or е.get("quoteMint")
                    с["пул"] = с["пул"] or е.get("pool")
                    с["poolId"] = с["poolId"] or е.get("poolId")
                    с["первое_событие_мс"] = min(с["первое_событие_мс"] or ts, ts)
                    с["последнее_событие_мс"] = max(с["последнее_событие_мс"] or ts, ts)
                    ц, ликв = цена_и_ликв(е)
                    if ц is None:
                        continue
                    т = метка_мс[ключ]
                    if ts <= т:
                        пред = с["до"]
                        if not пред or ts >= пред["мс"]:
                            с["до"] = {"мс": ts, "цена": ц, "ликвидность": ликв,
                                       "от_метки_с": round((ts - т) / 1000, 1)}
                    for шаг in ШАГИ:
                        if ts <= т + шаг * 1000:
                            б = с["шаги"].get(str(шаг))
                            if not б or ts >= б["мс"]:
                                с["шаги"][str(шаг)] = {"мс": ts, "цена": ц,
                                                       "ликвидность": ликв}
        except Exception as e:  # noqa: BLE001
            счёт["ошибки"].append(f"{ч}: {type(e).__name__}: {str(e)[:120]}")
        print(f"  {ч}: строк {счёт['строк']}, разобрано {счёт['разобрано']}, "
              f"минут {round((time.time() - нач) / 60, 1)}", flush=True)

    есть = [к for к, с in состояние.items() if с["событий"]]
    тело = {"что": "покрытие архивом и путь цены после первого появления (описательно)",
            "описательно": "правил не подбирает, вердиктов не выносит",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "окно_часов": [часы[0], часы[-1]], "часов": len(часы), "счёт": счёт,
            "заказано": len(зак), "найдено_в_архиве": len(есть),
            "ряды": {к: {**состояние[к], "заказ": зак[к]} for к in есть}}
    ф = П / f"trending_arhiv{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False), encoding="utf-8")
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    print(f"заказано {len(зак)}, найдено {len(есть)}; записано {ф}", flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", required=True, help="первый час окна, YYYY-MM-DDTHH (UTC)")
    р.add_argument("--chasov", type=int, default=4)
    р.add_argument("--zakaz", default=str(П / "trending_zakaz_arhiva.json"))
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
