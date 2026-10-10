#!/usr/bin/env python3
"""Охват «создатель купил сам»: архив против живого журнала Code-1 на одних минтах.

Зачем. Живой журнал (подписка на PDA mint-authority программы pump.fun) за час
2026-10-09T17Z даёт 2 487 созданий, у 1 731 (69.6 %) создатель покупает в той же
транзакции. Сборщик запусков на архиве за сутки 07.10 видит 56 062 создания pump
(столько же, сколько журнал в пересчёте на сутки), но «создатель купил в створе»
ставит лишь у 889 из них -- 1.6 %. Разрыв в 44 раза не объясняется потерей
созданий: создания архив видит все. Значит причина в одном из двух -- либо строки
покупки создателя в потоке нет вовсе, либо она есть, но её адрес не совпадает с
адресом на строке создания.

Что делает. Берёт вырезку журнала (data/podbivka/sozdatel_zhurnal_vyrezka.json),
читает тот же час архива и по КАЖДОМУ минту журнала собирает все строки потока в
окрестности слота создания: действие, адреса (trader и txSigner раздельно),
poolId, суммы. Дальше сверяет по минту: есть ли строка создания; есть ли покупка в
блоке создания; чей адрес на строке создания; чей на покупке; совпадает ли хоть
один из них с создателем из журнала.

Ничего не считает кругами и не делает вердиктов -- только измерение охвата.
Только чтение архива. Выход: data/podbivka/sozdatel_ohvat.json.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_arhiv_den as AD      # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
ОКНО_СЛОТОВ = 2       # сколько слотов после створа держать строки минта

р_block = re.compile(r'"block":\s*(\d+)')
р_action = re.compile(r'"action":\s*"([a-zA-Z_]+)"')
р_mint = re.compile(r'"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_pool = re.compile(r'"poolId":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_tip = re.compile(r'"pool":\s*"([a-z0-9\-]+)"')
р_signer = re.compile(r'"txSigner":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_trader = re.compile(r'"trader":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_qam = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')
р_qmint = re.compile(r'"quoteMint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_sig = re.compile(r'"signature":\s*"([1-9A-HJ-NP-Za-km-z]{64,90})"')


def часы(с: str, сколько: int) -> list:
    т = time.strptime(с, "%Y-%m-%dT%H")
    н = int(time.mktime(т)) - time.timezone
    return [time.strftime("%Y/%m/%d/%H", time.gmtime(н + 3600 * k)) for k in range(сколько)]


def главное(а) -> int:  # noqa: PLR0912, PLR0915
    import requests  # noqa: PLC0415
    import zstandard  # noqa: PLC0415
    import io  # noqa: PLC0415

    вырезка = json.loads((П / "sozdatel_zhurnal_vyrezka.json").read_text())
    журнал = {з["mint"]: з for з in вырезка["записи"] if з.get("mint")}
    if not журнал:
        print("вырезка журнала пуста -- сверять нечего", flush=True)
        return 2
    print(f"минтов в журнале {len(журнал)}, с покупкой создателя "
          f"{sum(1 for з in журнал.values() if з['kupil'])}", flush=True)

    строки: dict = collections.defaultdict(list)   # mint -> [запись строки]
    счёт: collections.Counter = collections.Counter()

    def читать_час(поток) -> None:
        for стр in io.TextIOWrapper(
                zstandard.ZstdDecompressor().stream_reader(поток.raw),
                encoding="utf-8", errors="ignore"):
            счёт["строк"] += 1
            мм = р_mint.search(стр)
            if not мм:
                continue
            м = мм.group(1)
            з = журнал.get(м)
            if з is None:
                continue
            бм = р_block.search(стр)
            блок = int(бм.group(1)) if бм else None
            с0 = з.get("slot")
            if блок is not None and с0 and not (с0 - 1 <= блок <= с0 + ОКНО_СЛОТОВ):
                счёт["строка_минта_вне_окна"] += 1
                continue
            if len(строки[м]) >= 40:
                счёт["строк_минта_обрезано"] += 1
                continue
            ам = р_action.search(стр)
            тм = р_trader.search(стр)
            sм = р_signer.search(стр)
            qм = р_qam.search(стр)
            qmм = р_qmint.search(стр)
            тип = р_tip.search(стр)
            pм = р_pool.search(стр)
            sig = р_sig.search(стр)
            строки[м].append({
                "блок": блок, "действие": ам.group(1) if ам else "<нет>",
                "пул": тип.group(1) if тип else None,
                "poolId": pм.group(1) if pм else None,
                "trader": тм.group(1) if тм else None,
                "txSigner": sм.group(1) if sм else None,
                "sol": (float(qм.group(1)) if qм and qmм and qmм.group(1) == WSOL
                        and _число(qм.group(1)) else None),
                "подпись": sig.group(1) if sig else None})
            счёт["строк_минта"] += 1

    for ч in часы(а.s, а.chasov):
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        for попытка in range(3):
            try:
                о = AD.открыть_час(requests, url, ч)
            except Exception as exc:  # noqa: BLE001
                счёт[f"ошибка_{type(exc).__name__}"] += 1
                break
            with о as поток:
                код = getattr(поток, "status_code", 200)
                if код != 200:
                    счёт[f"http_{код}"] += 1
                    break
                try:
                    читать_час(поток)
                except Exception as exc:  # noqa: BLE001
                    счёт[f"обрыв_{type(exc).__name__}"] += 1
                    if попытка == 2:
                        счёт["обрыв_часов"] += 1
                    continue
            счёт["часов"] += 1
            break
        print(f"  {ч}: строк {счёт['строк']}, строк минтов {счёт['строк_минта']}",
              flush=True)

    # ---- сверка по минтам ------------------------------------------------------
    итог: collections.Counter = collections.Counter()
    по_журналу: dict = {True: collections.Counter(), False: collections.Counter()}
    примеры: list = []
    разбор: list = []
    for м, з in журнал.items():
        л = строки.get(м) or []
        к = по_журналу[з["kupil"]]
        итог["минтов"] += 1
        к["минтов"] += 1
        if not л:
            итог["минта_нет_в_архиве"] += 1
            к["минта_нет_в_архиве"] += 1
            continue
        итог["минт_найден"] += 1
        к["минт_найден"] += 1
        созд = [x for x in л if x["действие"] not in ("buy", "sell")]
        с0 = з.get("slot")
        покуп_створ = [x for x in л if x["действие"] == "buy" and x["блок"] == с0]
        покуп_любые = [x for x in л if x["действие"] == "buy"]
        адрес_созд = {x["trader"] for x in созд if x["trader"]} | \
                     {x["txSigner"] for x in созд if x["txSigner"]}
        адрес_пок = {x["trader"] for x in покуп_створ if x["trader"]} | \
                    {x["txSigner"] for x in покуп_створ if x["txSigner"]}
        if созд:
            итог["строка_создания_есть"] += 1
            к["строка_создания_есть"] += 1
            if з["sozdatel"] in адрес_созд:
                итог["адрес_создания_совпал"] += 1
                к["адрес_создания_совпал"] += 1
        if покуп_створ:
            итог["покупка_в_створе_есть"] += 1
            к["покупка_в_створе_есть"] += 1
            if з["sozdatel"] in адрес_пок:
                итог["покупка_создателя_видна"] += 1
                к["покупка_создателя_видна"] += 1
        if покуп_любые and not покуп_створ:
            итог["покупки_только_вне_створа"] += 1
            к["покупки_только_вне_створа"] += 1
        # одна подпись на создание и покупку -- признак связки create+dev buy
        подписи_созд = {x["подпись"] for x in созд if x["подпись"]}
        if покуп_створ and подписи_созд and any(
                x["подпись"] in подписи_созд for x in покуп_створ):
            итог["одна_подпись_с_созданием"] += 1
            к["одна_подпись_с_созданием"] += 1
        if з["kupil"] and созд and not покуп_створ and len(примеры) < 15:
            примеры.append({"минт": м, "журнал": з, "строки": л[:12]})
        if len(разбор) < 400:
            разбор.append({"минт": м, "купил_по_журналу": з["kupil"],
                           "sol_журнал": з["sol"], "создание_строк": len(созд),
                           "покупок_в_створе": len(покуп_створ),
                           "создатель_в_адресах_покупки": з["sozdatel"] in адрес_пок,
                           "создатель_в_адресах_создания": з["sozdatel"] in адрес_созд})

    т = {"что": "охват «создатель купил сам»: архив против живого журнала Code-1",
         "окно": {"с": а.s, "часов": а.chasov},
         "журнал": {"минтов": len(журнал),
                    "с_покупкой_создателя": sum(1 for з in журнал.values() if з["kupil"])},
         "счёт_потока": dict(счёт), "сверка": dict(итог),
         "по_журналу_купил": dict(по_журналу[True]),
         "по_журналу_не_купил": dict(по_журналу[False]),
         "примеры_купил_но_покупки_в_архиве_нет": примеры,
         "разбор": разбор}
    (П / f"sozdatel_ohvat{а.metka}.json").write_text(
        json.dumps(т, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in т.items()
                      if k in ("счёт_потока", "сверка", "по_журналу_купил",
                               "по_журналу_не_купил")}, ensure_ascii=False, indent=1),
          flush=True)
    return 0


def _число(s: str) -> bool:
    try:
        float(s)
    except ValueError:
        return False
    return True


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", required=True, help="начало окна, YYYY-MM-DDTHH")
    р.add_argument("--chasov", type=int, default=1)
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
