#!/usr/bin/env python3
"""Голова очереди по архиву: три среза по длине слота (250 мс -> 200 мс -> Votor).

Зачем. Слот сократили до 200 мс 9.10, 21.10 включили Alpenglow/Votor (эпоха 1064). Вопрос
владельца -- изменилась ли СТРУКТУРА головы очереди: где в слоте стоит покупка источника,
сколько покупок того же минта успевает пройти до него, какой наплыв в его слоте и в
следующем, сколько впереди независимых плательщиков, какая доля объёма у верхних десяти и
через сколько слотов источник продаёт. Торговых выводов тут нет -- только структура.

Методика взята от data/golova_ocheredi.json (сентябрь, по цепи через Helius), но считается
по архиву PumpApi, и два отличия названы прямо:

  * ИНДЕКС В СЛОТЕ. В архиве индекса транзакции в блоке нет вовсе. Порядок внутри слота
    берётся как порядок событий в часовом файле, а доля считается от числа событий архива
    в этом слоте, а НЕ от размера блока: 707 из 935 транзакций блока и «пятый из сорока
    событий архива» -- разные величины, и смешивать их нельзя. Насколько порядок архива
    совпадает с порядком в блоке, проверяется отдельным режимом --sverka-golova на тех
    слотах, где у Code-1 есть истина по цепи.
  * ПЛАТЕЛЬЩИК. В архиве есть txSigner (подписант), а не плательщик. Для источников,
    которые подписывают сами за себя, это одно и то же; покупки через посредника под
    чужой подписью сюда не попадут.

Наплыв пересчитывается ВО ВРЕМЯ: на слот и на секунду, а длина слота меряется по самому
архиву (медиана разницы timestamp между соседними блоками), а не берётся на слово.

Только чтение. Выход: data/podbivka/golova_srez<метка>.json.
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import re
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_arhiv_den as А  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
ВЕРХНИХ = 10
р_signer = re.compile(r'"txSigner":"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_action = re.compile(r'"action":"(\w+)"')


def медиана(в: list):
    return round(st.median(в), 4) if в else None


def доля(к: int, n: int):
    return round(к / n, 4) if n else None


def главное(а) -> int:
    import requests  # noqa: PLC0415
    import zstandard  # noqa: PLC0415

    источники = {x.strip() for x in Path(а.istochniki).read_text(
        encoding="utf-8").split() if x.strip()}
    т0 = time.mktime(time.strptime(а.s, "%Y-%m-%dT%H"))
    часы = [time.strftime("%Y/%m/%d/%H", time.gmtime(т0 + 3600 * k))
            for k in range(а.chasov)]

    # по слоту: события архива (для индекса), по (минт, слот): покупки (для головы)
    покупки_мс: dict = collections.defaultdict(list)   # (минт, блок) -> [(поз, ts, signer, sol)]
    событий_в_слоте: collections.Counter = collections.Counter()
    блок_ts: dict = {}
    сигналы = []            # покупки источников
    продажи: dict = collections.defaultdict(list)      # (signer, минт) -> [блок]
    счёт = collections.Counter()
    поз_в_слоте: collections.Counter = collections.Counter()
    нач = time.time()
    for ч in часы:
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        try:
            with А.открыть_час(requests, url, ч) as о:
                if о.status_code != 200:
                    счёт[f"час_нет_{о.status_code}"] += 1
                    continue
                счёт["часов"] += 1
                дек = zstandard.ZstdDecompressor().stream_reader(о.raw)
                for стр in io.TextIOWrapper(дек, encoding="utf-8", errors="replace"):
                    счёт["строк"] += 1
                    ам = р_action.search(стр)
                    if not ам or ам.group(1) not in ("buy", "sell"):
                        continue
                    try:
                        е = json.loads(стр)
                    except ValueError:
                        continue
                    блок = е.get("block")
                    минт = е.get("mint")
                    signer = е.get("txSigner")
                    if not (блок and минт and signer):
                        continue
                    ts = е.get("timestamp")
                    if isinstance(ts, (int, float)):
                        блок_ts.setdefault(блок, int(ts))
                    поз_в_слоте[блок] += 1
                    поз = поз_в_слоте[блок]
                    событий_в_слоте[блок] += 1
                    if ам.group(1) == "sell":
                        if signer in источники:
                            продажи[(signer, минт)].append(блок)
                        continue
                    if е.get("quoteMint") != WSOL:
                        счёт["покупка_не_sol"] += 1
                        continue
                    sol = е.get("quoteAmount")
                    try:
                        sol = float(sol)
                    except (TypeError, ValueError):
                        sol = None
                    покупки_мс[(минт, блок)].append((поз, ts, signer, sol))
                    if signer in источники:
                        сигналы.append({"минт": минт, "блок": блок, "ts": ts,
                                        "источник": signer, "sol": sol, "поз": поз})
        except Exception as e:  # noqa: BLE001
            счёт["ошибка_часа"] += 1
            print(f"  {ч}: {type(e).__name__}: {str(e)[:100]}", flush=True)
        print(f"  {ч}: строк {счёт['строк']}, сигналов {len(сигналы)}, "
              f"минут {round((time.time() - нач) / 60, 1)}", flush=True)

    # длина слота по самому архиву: медиана разницы ts между соседними блоками
    блоки = sorted(блок_ts)
    шаги = [блок_ts[b] - блок_ts[a] for a, b in zip(блоки, блоки[1:])
            if 0 < блок_ts[b] - блок_ts[a] < 5000 and b - a == 1]
    слот_мс = медиана(шаги)

    ряды = []
    for с in сигналы:
        минт, блок = с["минт"], с["блок"]
        в_слоте = sorted(покупки_мс.get((минт, блок)) or [])
        до = [x for x in в_слоте if x[0] < с["поз"]]
        пред3 = []
        for d in (1, 2, 3):
            пред3 += покупки_мс.get((минт, блок - d)) or []
        наплыв = sum(x[3] or 0 for x in в_слоте)
        наплыв_след = sum(x[3] or 0 for x in (покупки_мс.get((минт, блок + 1)) or []))
        вокруг = в_слоте + (покупки_мс.get((минт, блок + 1)) or [])
        по_плат: dict = collections.defaultdict(float)
        for _п, _т, sg, sl in вокруг:
            по_плат[sg] += sl or 0
        всего_об = sum(по_плат.values())
        верх = sorted(по_плат.values(), reverse=True)[:ВЕРХНИХ]
        прод = sorted(x for x in (продажи.get((с["источник"], минт)) or [])
                      if x >= блок)
        ряды.append({
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((с["ts"] or 0) / 1000)),
            "минт": минт, "блок": блок, "источник": с["источник"],
            "покупка_sol": с["sol"],
            "индекс_в_слоте_архива": с["поз"],
            "событий_архива_в_слоте": событий_в_слоте.get(блок),
            "доля_слота_архива": доля(с["поз"], событий_в_слоте.get(блок) or 0),
            "покупок_до_в_слоте": len(до),
            "плательщиков_до_в_слоте": len({x[2] for x in до}),
            "покупок_до_в_3_слотах": len(пред3),
            "плательщиков_до_в_3_слотах": len({x[2] for x in пред3}),
            "первый_в_слоте": len(до) == 0,
            "наплыв_слота_sol": round(наплыв, 6),
            "наплыв_следующего_sol": round(наплыв_след, 6),
            "покупок_в_слоте": len(в_слоте),
            "плательщиков_вокруг": len(по_плат),
            "доля_объёма_верхних10": доля(round(sum(верх), 6), round(всего_об, 6))
            if всего_об else None,
            "первая_продажа_слотов": (прод[0] - блок) if прод else None,
        })

    def сч(поле, где=None):
        v = [р[поле] for р in ряды if р.get(поле) is not None
             and (где is None or где(р))]
        return v

    свод = {
        "n_покупок_источников": len(ряды),
        "источников_разных": len({р["источник"] for р in ряды}),
        "слот_мс_по_архиву": слот_мс,
        "слот_мс_заявлено": а.slot_ms,
        "индекс_в_слоте_архива_медиана": медиана(сч("индекс_в_слоте_архива")),
        "событий_архива_в_слоте_медиана": медиана(сч("событий_архива_в_слоте")),
        "доля_слота_архива_медиана": медиана(сч("доля_слота_архива")),
        "покупок_до_в_слоте_медиана": медиана(сч("покупок_до_в_слоте")),
        "покупок_до_в_слоте_среднее": (round(st.fmean(сч("покупок_до_в_слоте")), 3)
                                       if сч("покупок_до_в_слоте") else None),
        "плательщиков_до_в_слоте_медиана": медиана(сч("плательщиков_до_в_слоте")),
        "покупок_до_в_3_слотах_медиана": медиана(сч("покупок_до_в_3_слотах")),
        "доля_первых_в_слоте": доля(sum(1 for р in ряды if р["первый_в_слоте"]), len(ряды)),
        "наплыв_слота_sol_медиана": медиана(сч("наплыв_слота_sol")),
        "наплыв_следующего_sol_медиана": медиана(сч("наплыв_следующего_sol")),
        "покупок_в_слоте_медиана": медиана(сч("покупок_в_слоте")),
        "доля_объёма_верхних10_медиана": медиана(сч("доля_объёма_верхних10")),
        "плательщиков_вокруг_медиана": медиана(сч("плательщиков_вокруг")),
        "первая_продажа_слотов_медиана": медиана(сч("первая_продажа_слотов")),
        "с_продажей": len(сч("первая_продажа_слотов")),
    }
    # ВО ВРЕМЯ, а не на слот: тот же наплыв и то же число покупок на секунду
    if слот_мс:
        к = 1000.0 / слот_мс
        свод["наплыв_слота_sol_в_секунду"] = (
            round(свод["наплыв_слота_sol_медиана"] * к, 6)
            if свод["наплыв_слота_sol_медиана"] is not None else None)
        свод["покупок_в_слоте_в_секунду"] = (
            round(свод["покупок_в_слоте_медиана"] * к, 4)
            if свод["покупок_в_слоте_медиана"] is not None else None)
        свод["покупок_до_в_слоте_в_секунду"] = (
            round(свод["покупок_до_в_слоте_среднее"] * к, 4)
            if свод["покупок_до_в_слоте_среднее"] is not None else None)
        свод["первая_продажа_мс_медиана"] = (
            round(свод["первая_продажа_слотов_медиана"] * слот_мс, 1)
            if свод["первая_продажа_слотов_медиана"] is not None else None)

    тело = {"что": "структура головы очереди по архиву: один срез",
            "срез": а.metka.lstrip("_"), "сутки_с": а.s, "часов_заказано": а.chasov,
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "источников_в_наборе": len(источники),
            "оговорки": [
                "индекс в слоте -- порядок событий АРХИВА, доля -- от событий архива в "
                "слоте, а не от размера блока: индекса транзакции в блоке архив не даёт",
                "плательщик в архиве -- это txSigner (подписант); покупки через "
                "посредника под чужой подписью в счёт не попадают",
                "длина слота измерена по самому архиву, рядом с заявленной",
            ],
            "счёт": dict(счёт), "свод": свод, "ряды": ряды}
    ф = П / f"golova_srez{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(свод, ensure_ascii=False, indent=1), flush=True)
    print(f"записано {ф}", flush=True)
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", required=True, help="первый час среза, YYYY-MM-DDTHH (UTC)")
    р.add_argument("--chasov", type=int, default=24)
    р.add_argument("--istochniki", default=str(П / "nabor_istochniki.txt"))
    р.add_argument("--slot-ms", type=float, default=0.0, help="заявленная длина слота")
    р.add_argument("--metka", required=True)
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
