#!/usr/bin/env python3
"""Снайперы по деньгам: сито по неделе 21–28.09 в архиве PumpApi. Только чтение, без Helius.

Слово владельца 02.10: признак не по слоту, а по деньгам. Пулы -- кривая pump.fun, LaunchLab, Pump AMM, CPMM;
котировка WSOL.
  1) кошельки с ≥ 50 покупками за неделю и медианным билетом ≥ 1 SOL-экв.;
  2) по каждому -- итог по минтам, ЗАКРЫТЫМ в окне (продано ≥ 99 % купленных токенов), оборот, удержание до первой
     продажи, доля продаж частями;
  3) верхние 50 по итогу -- карточки; «ведущий» -- кошелёк, покупавший тот же минт за ≤ 3 слота до него и сделавший
     это ≥ 5 раз за неделю.

Проход один, но технически двухфазный -- иначе нельзя: фильтр «≥ 50 покупок за неделю» известен только после
полного обхода, а держать разбор по (кошелёк, минт) для всех кошельков рынка в памяти нельзя (за два часа архива
их 188 тысяч). Часы оба раза читаются с локального диска бегунка (PODB_ARHIV_KESH), второй раз не качаются.
  --faza1 --den: по кошельку за сутки -- покупок, сумма SOL, билеты (кошельки с < 3 покупками за сутки отброшены:
     при < 3 в сутки 50 за неделю не собрать) -> snaipery/dengi_f1_<день>.json.gz;
  --sito: свести семь суток, оставить ≥ 50 покупок и медиану билета ≥ 1 SOL -> snaipery/dengi_sito.json;
  --faza2 --den: только по кошелькам сита -- по (кошелёк, минт) вошло/вышло SOL и токенов, слоты первой покупки и
     первой продажи, число продаж, приоритет (поле архива); плюс ведущие (покупка того же минта в слотах s0−3…s0−1,
     и в том же слоте раньше в файле) -> snaipery/dengi_f2_<день>.json.gz;
  --svod: страница docs/podbivka_2026-10-02_snaipery_po_dengam.md.
Чаевых и комиссии сети в архиве НЕТ (есть только priorityFee): итог «после издержек» на этой странице -- за вычетом
приоритета архива; чаевые по цепи -- отдельной стадией по верхним 50 (analysis/podbivka_snaipery_chaevye.py).
"""
from __future__ import annotations

import argparse
import calendar
import collections
import glob
import gzip
import io
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_arhiv_den as A  # noqa: E402

КОРЕНЬ = A.КОРЕНЬ
П = КОРЕНЬ / "data" / "podbivka" / "snaipery"
ТИПЫ = ("pump", "raydium-launchpad", "pump-amm", "raydium-cpmm")
НЕДЕЛЯ = ("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27")
МИН_ПОКУПОК, МИН_БИЛЕТ, МИН_В_СУТКИ = 50, 1.0, 3
ВЕДУЩИЙ_СЛОТОВ, ВЕДУЩИЙ_РАЗ = 3, 5
ЗАКРЫТ = 0.99
ВЕРХ = 50

р_act = re.compile(r'"action":\s*"(buy|sell)"')
р_qamt = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')
р_tamt = re.compile(r'"tokenAmount":\s*"?([0-9.eE+-]+)')
р_pooltype = re.compile(r'"pool":\s*"([a-z0-9-]+)"')
р_trader = re.compile(r'"trader":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_prio = re.compile(r'"priorityFee":\s*"?([0-9.eE+-]+)')
р_ts = re.compile(r'"timestamp":\s*"?(\d+)')


def ч(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def часы_суток(д: str, хвост: int = 1) -> list:
    t0 = calendar.timegm(time.strptime(д, "%Y-%m-%d"))
    return [time.strftime("%Y/%m/%d/%H", time.gmtime(t0 + 3600 * k)) for k in range(24 + хвост)], t0


def поток(д: str, хвост: int, дело) -> dict:
    """Обход часов суток: на каждую строку -- дело(поля). Возвращает счёт."""
    import requests  # noqa: PLC0415
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"], check=True)
    import zstandard  # noqa: PLC0415
    часы, t0 = часы_суток(д, хвост)
    лево, право = t0 * 1000, (t0 + 86400) * 1000
    счёт = {"строк": 0, "файлов": 0, "событий": 0, "ошибки": []}
    for чс in часы:
        url = f"https://replay.pumpapi.io/{чс}.jsonl.zst"
        прочитано = 0
        for попытка in range(4):
            try:
                with A.открыть_час(requests, url, чс) as о:
                    if о.status_code != 200:
                        счёт["ошибки"].append(f"{чс}: http {о.status_code}")
                        break
                    if попытка == 0:
                        счёт["файлов"] += 1
                    n = 0
                    for стр in io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(о.raw), encoding="utf-8",
                                                errors="replace"):
                        n += 1
                        if n <= прочитано:
                            continue
                        прочитано = n
                        счёт["строк"] += 1
                        if A.WSOL not in стр:
                            continue
                        ам = р_act.search(стр)
                        тм = р_pooltype.search(стр)
                        if not (ам and тм and тм.group(1) in ТИПЫ):
                            continue
                        qм = A.р_qmint.search(стр)
                        бм = A.р_block.search(стр)
                        пм = A.р_pool.search(стр)
                        if not (qм and qм.group(1) == A.WSOL and бм and пм):
                            continue
                        тр = р_trader.search(стр)
                        сн = A.р_signer.search(стр)
                        кто = тр.group(1) if тр else (сн.group(1) if сн else None)
                        if not кто:
                            continue
                        тсм = р_ts.search(стр)
                        тс = int(тсм.group(1)) if тсм else 0
                        счёт["событий"] += 1
                        дело({"кто": кто, "действие": ам.group(1), "тип": тм.group(1), "pid": пм.group(1),
                              "блок": int(бм.group(1)), "минт": (A.р_mint.search(стр) or [None, None])[1],
                              "q": ч((р_qamt.search(стр) or [None, None])[1]),
                              "t": ч((р_tamt.search(стр) or [None, None])[1]),
                              "prio": ч((р_prio.search(стр) or [None, None])[1]) or 0.0,
                              "в_сутках": лево <= тс < право})
                    break
            except (OSError, RuntimeError, ValueError) as exc:
                if попытка == 3:
                    счёт["ошибки"].append(f"{чс}: {type(exc).__name__} {str(exc)[:120]}")
                else:
                    time.sleep(3 * (попытка + 1))
    return счёт


def фаза1(д: str) -> int:
    import podbivka_run as R  # noqa: PLC0415
    по: dict = {}

    def дело(e):
        if e["действие"] != "buy" or not e["в_сутках"]:
            return
        з = по.get(e["кто"])
        if з is None:
            з = по[e["кто"]] = [0, 0.0, []]
        з[0] += 1
        з[1] += e["q"] or 0.0
        if len(з[2]) < 400:
            з[2].append(round(e["q"] or 0.0, 4))
    счёт = поток(д, 0, дело)
    ряды = {w: {"покупок": з[0], "sol": round(з[1], 3), "билеты": з[2]}
            for w, з in по.items() if з[0] >= МИН_В_СУТКИ}
    П.mkdir(parents=True, exist_ok=True)
    out = П / f"dengi_f1_{д}.json.gz"
    out.write_bytes(gzip.compress(json.dumps({"день": д, "счёт": счёт, "порог_в_сутки": МИН_В_СУТКИ,
                                              "кошельков_всего": len(по), "кошельков": ряды},
                                             ensure_ascii=False).encode()))
    R.записано(out)
    print(out.name, "строк", счёт["строк"], "событий", счёт["событий"], "кошельков всего", len(по),
          "в файле", len(ряды), "ошибки", счёт["ошибки"], flush=True)
    return 0


def сито() -> int:
    import podbivka_run as R  # noqa: PLC0415
    файлы = sorted(glob.glob(str(П / "dengi_f1_*.json.gz")))
    куч: dict = {}
    дни = []
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        дни.append({"день": д["день"], "строк": д["счёт"]["строк"], "ошибки": д["счёт"]["ошибки"],
                    "кошельков_всего": д["кошельков_всего"]})
        for w, v in д["кошельков"].items():
            з = куч.setdefault(w, [0, 0.0, []])
            з[0] += v["покупок"]
            з[1] += v["sol"]
            з[2] += v["билеты"]
        del д
    отбор = {}
    for w, з in куч.items():
        if з[0] < МИН_ПОКУПОК or not з[2]:
            continue
        мед = statistics.median(з[2])
        if мед >= МИН_БИЛЕТ:
            отбор[w] = {"покупок": з[0], "sol_покупок": round(з[1], 3), "билет_медиана": round(мед, 4),
                        "билетов_в_выборке": len(з[2])}
    out = П / "dengi_sito.json"
    out.write_text(json.dumps({"дни": дни, "порог": {"покупок": МИН_ПОКУПОК, "билет_медиана": МИН_БИЛЕТ},
                               "кошельков_просмотрено": len(куч), "кошельков": отбор},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    print("сито:", len(отбор), "кошельков из", len(куч), flush=True)
    return 0


def фаза2(д: str) -> int:
    import podbivka_run as R  # noqa: PLC0415
    цель = set(json.loads((П / "dengi_sito.json").read_text(encoding="utf-8"))["кошельков"])
    по: dict = {}                 # (кошелёк, минт) -> запись
    ведущие: dict = {}            # кошелёк -> Counter(ведущий)
    за_ведущим: dict = {}         # кошелёк -> [покупок с кем-то впереди, всего покупок]
    хвосты: dict = {}             # pid -> deque[(слот, кошелёк)]

    счётчик = [0]

    def дело(e):
        pid, блок, кто = e["pid"], e["блок"], e["кто"]
        счётчик[0] += 1
        if not (счётчик[0] % 5_000_000):
            for p_ in [p_ for p_, dq in хвосты.items() if not dq or dq[-1][0] < блок - ВЕДУЩИЙ_СЛОТОВ]:
                del хвосты[p_]
        д_ = хвосты.setdefault(pid, collections.deque())
        if e["действие"] == "buy":
            while д_ and д_[0][0] < блок - ВЕДУЩИЙ_СЛОТОВ:
                д_.popleft()
            if кто in цель and e["в_сутках"]:
                впереди = [w for s, w in д_ if w != кто]
                сч = за_ведущим.setdefault(кто, [0, 0])
                сч[1] += 1
                if впереди:
                    сч[0] += 1
                    ведущие.setdefault(кто, collections.Counter()).update(set(впереди))
            д_.append((блок, кто))
        if кто not in цель or not e["в_сутках"]:
            return
        кл = f"{кто}|{e['минт']}"
        з = по.get(кл)
        if з is None:
            з = по[кл] = {"тип": e["тип"], "sol_в": 0.0, "sol_из": 0.0, "ток_в": 0.0, "ток_из": 0.0,
                          "покупок": 0, "продаж": 0, "s_покупки": None, "s_продажи": None, "prio": 0.0}
        if e["действие"] == "buy":
            з["sol_в"] += e["q"] or 0.0
            з["ток_в"] += e["t"] or 0.0
            з["покупок"] += 1
            з["prio"] += e["prio"] or 0.0
            з["s_покупки"] = блок if з["s_покупки"] is None else min(з["s_покупки"], блок)
        else:
            з["sol_из"] += e["q"] or 0.0
            з["ток_из"] += e["t"] or 0.0
            з["продаж"] += 1
            з["prio"] += e["prio"] or 0.0
            з["s_продажи"] = блок if з["s_продажи"] is None else min(з["s_продажи"], блок)
    счёт = поток(д, 1, дело)
    out = П / f"dengi_f2_{д}.json.gz"
    out.write_bytes(gzip.compress(json.dumps({
        "день": д, "счёт": счёт, "кошельков_сита": len(цель), "пар": len(по),
        "пары": {к: {kk: (round(vv, 6) if isinstance(vv, float) else vv) for kk, vv in v.items()} for к, v in по.items()},
        "ведущие": {w: dict(c.most_common(50)) for w, c in ведущие.items()},
        "за_ведущим": за_ведущим}, ensure_ascii=False).encode()))
    R.записано(out)
    print(out.name, "строк", счёт["строк"], "пар", len(по), "ошибки", счёт["ошибки"], flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--faza1", action="store_true")
    р.add_argument("--faza2", action="store_true")
    р.add_argument("--sito", action="store_true")
    р.add_argument("--den", default="")
    а = р.parse_args()
    if а.sito:
        return сито()
    if not а.den:
        р.error("нужен --den YYYY-MM-DD")
    return фаза1(а.den) if а.faza1 else фаза2(а.den) if а.faza2 else р.error("нужна фаза")


if __name__ == "__main__":
    raise SystemExit(main())
