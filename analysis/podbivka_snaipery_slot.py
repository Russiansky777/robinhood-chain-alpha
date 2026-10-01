#!/usr/bin/env python3
"""Снайперы по слоту: кто садится в слоте чужой покупки. Один проход по суткам архива PumpApi, только чтение.

ПРИЗНАК ОТВЕРГНУТ (проба 01.10, 2 часа 30.09): «сел в слоте чужой покупки» -- это весь рынок, не снайперы:
508 373 события за 2 часа, 102 253 кошелька хотя бы раз, 33 753 кошелька уже с ≥ 3 за два часа. Признак по деньгам --
analysis/podbivka_snaipery_dengi.py (слово владельца 02.10). Файл оставлен ради проб и чисел пробы.

Слово владельца 01.10 02:14 (блок в HEDGE_must_check.md -- этого файла в репозитории нет, работаю по тексту задания):
  (а) 54uaRuJEc9BHY7uVMXtcf9JWcYDtB75hUeFJWeCxEkBE -- копирует ли он вообще: доля покупок, где перед ним только
      первая покупка пула (дев), доля с нашим источником перед ним; как продаёт (части, толпа за ним); итог по
      корзинам позиции в пуле 1–9 / 10–50 / 50+;
  (б) похожие: кошельки, которые ≥ 3 раз в сутки садятся в слоте ЧУЖОЙ покупки того же минта -- карточка на каждого.

--den YYYY-MM-DD (бегунок lab-miami, часы с локального диска): один проход по часам суток (+1 час хвоста окна).
Разбор строки -- регулярками по сырой строке, без json.loads (в сутках ~69 млн строк); сначала отсев по подстроке
WSOL: считаются только события с котировкой WSOL.
  * по каждому пулу в потоке: число покупок до, кошельки покупок текущего слота, кошелёк первой покупки, виден ли
    пул «с нуля» (у кривой pump.fun виртуальный резерв до первой покупки ≈ 30 SOL);
  * «сел в слоте чужой покупки» -- покупка кошелька W в пуле P в слоте s, где в этом же слоте РАНЬШЕ в файле уже
    была покупка другого кошелька; «ведущий» -- автор той покупки;
  * фокусные кошельки (--fokus): каждая покупка -- позиция в пуле, предшественники в слоте, толпа +1…+12, модель
    нашего входа за ним (конец его слота, билет 0.3, выходы +72 / +108, модель архива, Pump AMM v6) и его
    собственные продажи этого минта в сутках;
  * карточки: кошельки, чей счётчик «сел в чужом слоте» дошёл до 3; их покупки и продажи пишутся С МОМЕНТА третьего
    совпадения -- одного прохода на большее не хватает, это оговорено на странице.
Выход: data/podbivka/snaipery/snaipery_<день>.json.gz.

Чего в архиве нет и что здесь НЕ считается: чаевые, комиссии и приоритет (их нет в событиях PumpApi -- только цепь),
регионы лидеров слотов, отправители. Эти части -- отдельным прогоном по цепи.
"""
from __future__ import annotations

import argparse
import calendar
import collections
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
ФОКУС = ("54uaRuJEc9BHY7uVMXtcf9JWcYDtB75hUeFJWeCxEkBE",)
ОКНО = 160                  # слотов на модель нашего входа за фокусным кошельком
ТОЛПА_СЛОТОВ = 12
ПОРОГ_СИДЕЛ = 3             # «≥ 3 раз в сутки сел в слоте чужой покупки»
ВЫХОДЫ = (72, 108)
КРИВАЯ_НАЧАЛО = 31.0        # вирт. резерв кривой pump.fun до первой покупки ≈ 30 SOL
ЗАБЫТЬ_СЛОТОВ = 20000       # пул без событий столько слотов (~2.3 ч) выбрасывается из памяти
НАШИ_ГРУППЫ = ("lane_s0", "batch5", "cand1", "leader", "sniper_src", "снайперские источники", "133")

р_action = re.compile(r'"action":\s*"(buy|sell)"')
р_trader = re.compile(r'"trader":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_qamt = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')
р_pooltype = re.compile(r'"pool":\s*"([a-z0-9-]+)"')
р_ts = re.compile(r'"timestamp":\s*"?(\d+)')
р_vq = re.compile(r'"vQuoteInBondingCurve":\s*"?([0-9.eE+-]+)')


def ч(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def наши_источники() -> set:
    адр = json.loads((КОРЕНЬ / "data" / "podbivka" / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    return {a for a, v in адр.items() if set((v or {}).get("группы") or []) & set(НАШИ_ГРУППЫ)}


def модель_за(с: dict, ряд: list) -> dict:
    """Модель нашего входа «конец слота» за покупкой с (билет 0.3): п.п. по выходам, толпа за ним."""
    i0 = next((i for i, e in enumerate(ряд) if e.get("signature") == с["signature"]), None)
    if i0 is None:
        return {"why_not": "покупка не найдена в ряду"}
    окно = [e for e in ряд[i0:] if (e.get("block") or 0) <= с["block"] + ОКНО]
    м = A.модель(с, окно)
    пп = м.get("пп") or {}
    return {"пп": {str(h): пп.get(f"S0_дно|0.3|{h}") for h in ВЫХОДЫ}, "why_not": м.get("why_not"),
            "модель_pump_amm": м.get("модель_pump_amm"), "событий_окна": len(окно),
            "толпа": sum(1 for e in окно if e.get("action") == "buy"
                         and с["block"] < (e.get("block") or 0) <= с["block"] + ТОЛПА_СЛОТОВ
                         and e.get("трейдер") != с["trader"])}


def день(д: str, фокус: set, часов: int = 25) -> Path:
    import requests  # noqa: PLC0415
    import podbivka_run as R  # noqa: PLC0415
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"], check=True)
    import zstandard  # noqa: PLC0415
    A.БИЛЕТЫ, A.ВЫХОДЫ = (0.3,), ВЫХОДЫ
    ист = наши_источники()
    t0 = calendar.timegm(time.strptime(д, "%Y-%m-%d"))
    лево, право = t0 * 1000, (t0 + 86400) * 1000
    часы = [time.strftime("%Y/%m/%d/%H", time.gmtime(t0 + 3600 * k)) for k in range(часов)]

    пулы: dict = {}                      # pid -> [покупок до, слот, кошельки слота, с_нуля, тип, первый, последний слот]
    сидел = collections.Counter()
    ведущие: dict = {}
    карточки: dict = {}
    пок_все = collections.Counter()
    сол_все = collections.Counter()
    ф_ряды: dict = {}
    ф_ждут: dict = {}
    ф_покупки: list = []
    ф_продажи: list = []
    счёт = {"строк": 0, "файлов": 0, "ошибки": [], "событий_wsol": 0, "покупок_wsol": 0, "сидел_событий": 0,
            "пулов_забыто": 0, "действия": collections.Counter()}
    посл_слот = [0]

    def забыть() -> None:
        порог = посл_слот[0] - ЗАБЫТЬ_СЛОТОВ
        if порог <= 0:
            return
        мёртвые = [pid for pid, v in пулы.items() if v[6] < порог and pid not in ф_ждут]
        for pid in мёртвые:
            пулы.pop(pid, None)
        счёт["пулов_забыто"] += len(мёртвые)

    def закрыть_ф(pid: str, до_слота: int | None) -> None:
        ост = []
        for с in ф_ждут.get(pid) or []:
            if до_слота is None or до_слота > с["block"] + ОКНО:
                с["модель"] = модель_за(с, ф_ряды.get(pid) or [])
                ф_покупки.append(с)
            else:
                ост.append(с)
        if ост:
            ф_ждут[pid] = ост
            мин = min(x["block"] for x in ост)
            ряд = ф_ряды.get(pid) or []
            k = 0
            while k < len(ряд) and (ряд[k].get("block") or 0) < мин:
                k += 1
            if k:
                del ряд[:k]
        else:
            ф_ждут.pop(pid, None)
            ф_ряды.pop(pid, None)

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
                    n_стр = 0
                    for стр in io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(о.raw), encoding="utf-8",
                                                errors="replace"):
                        n_стр += 1
                        if n_стр <= прочитано:
                            continue
                        прочитано = n_стр
                        счёт["строк"] += 1
                        if not (счёт["строк"] % 5_000_000):
                            забыть()
                        if A.WSOL not in стр:
                            continue
                        ам = р_action.search(стр)
                        пм = A.р_pool.search(стр)
                        бм = A.р_block.search(стр)
                        qм = A.р_qmint.search(стр)
                        if not (ам and пм and бм and qм and qм.group(1) == A.WSOL):
                            continue
                        действие, pid, блок = ам.group(1), пм.group(1), int(бм.group(1))
                        счёт["событий_wsol"] += 1
                        счёт["действия"][действие] += 1
                        посл_слот[0] = max(посл_слот[0], блок)
                        тм = р_trader.search(стр)
                        см = A.р_signer.search(стр)
                        кто = тм.group(1) if тм else (см.group(1) if см else None)
                        тип = (р_pooltype.search(стр) or [None, None])[1]
                        минт = (A.р_mint.search(стр) or [None, None])[1]
                        qa = ч((р_qamt.search(стр) or [None, None])[1])
                        тсм = р_ts.search(стр)
                        тс = int(тсм.group(1)) if тсм else 0
                        в_сутках = лево <= тс < право
                        фокусный = кто in фокус and в_сутках

                        # ряд событий пулов, где фокусный кошелёк уже купил -- на модель нашего входа за ним
                        разобрано = None
                        if pid in ф_ждут:
                            закрыть_ф(pid, блок)
                            if pid in ф_ждут:
                                try:
                                    разобрано = {k: v for k, v in json.loads(стр).items() if k in A.КЛЮЧИ}
                                    разобрано["трейдер"] = кто
                                    if isinstance(разобрано.get("block"), int):
                                        ф_ряды.setdefault(pid, []).append(разобрано)
                                except ValueError:
                                    pass

                        if действие == "sell":
                            if в_сутках and кто in карточки:
                                к = карточки[кто]
                                к["продаж"] += 1
                                к["sol_продаж"] += qa or 0.0
                                к["минты_продано"].add(минт)
                                б = к["минты"].get(минт)
                                if б is not None:
                                    к["удержание"].append(блок - б)
                                    к["частей"][минт] = к["частей"].get(минт, 0) + 1
                            if фокусный:
                                ф_продажи.append({"signature": (A.р_sig.search(стр) or [None, None])[1], "poolId": pid,
                                                  "mint": минт, "block": блок, "sol": qa, "тип": тип})
                            continue

                        п = пулы.get(pid)
                        if п is None:
                            вм = р_vq.search(стр)
                            вирт = ч(вм.group(1)) if вм else None
                            с_нуля = ((вирт - (qa or 0)) <= КРИВАЯ_НАЧАЛО) if (тип == "pump" and вирт is not None) else None
                            п = пулы[pid] = [0, блок, [], с_нуля, тип, кто, блок]
                        п[6] = блок
                        if п[1] != блок:
                            п[1], п[2] = блок, []
                        чужие = [w for w in п[2] if w and w != кто]
                        сел = bool(чужие)
                        позиция = п[0] + 1
                        if в_сутках and кто:
                            пок_все[кто] += 1
                            сол_все[кто] += qa or 0.0
                            if сел:
                                сидел[кто] += 1
                                счёт["сидел_событий"] += 1
                                ведущие.setdefault(кто, collections.Counter())[чужие[-1]] += 1
                                if сидел[кто] >= ПОРОГ_СИДЕЛ and кто not in карточки:
                                    карточки[кто] = {"покупок": 0, "sol_покупок": 0.0, "билеты": [],
                                                     "типы": collections.Counter(), "сел": 0, "продаж": 0,
                                                     "sol_продаж": 0.0, "удержание": [], "минты": {},
                                                     "минты_продано": set(), "частей": {}, "позиции": []}
                            к = карточки.get(кто)
                            if к is not None:
                                к["покупок"] += 1
                                к["sol_покупок"] += qa or 0.0
                                к["билеты"].append(qa or 0.0)
                                к["типы"][тип] += 1
                                к["позиции"].append(позиция)
                                к["сел"] += 1 if сел else 0
                                к["минты"].setdefault(минт, блок)
                        if фокусный:
                            с = {"signature": (A.р_sig.search(стр) or [None, None])[1], "poolId": pid, "pool": тип,
                                 "mint": минт, "trader": кто, "block": блок, "timestamp": тс, "sol": qa,
                                 "позиция": позиция, "предшественники_в_слоте": чужие[-3:], "сел_в_слоте": сел,
                                 "ведущий": чужие[-1] if чужие else None, "первый_в_пуле": п[5],
                                 "наш_источник_перед": bool(set(чужие) & ист) or (п[5] in ист),
                                 "пул_с_нуля": п[3], "quoteMint": A.WSOL}
                            if разобрано is None:
                                try:
                                    разобрано = {k: v for k, v in json.loads(стр).items() if k in A.КЛЮЧИ}
                                    разобрано["трейдер"] = кто
                                except ValueError:
                                    разобрано = None
                            if разобрано is None:
                                ф_покупки.append({**с, "модель": {"why_not": "строка не разобрана"}})
                            else:
                                с.update({k: разобрано.get(k) for k in A.КЛЮЧИ if k not in с})
                                if pid not in ф_ждут:
                                    ф_ряды[pid] = [разобрано]
                                ф_ждут.setdefault(pid, []).append(с)
                        п[0] += 1
                        п[2].append(кто)
                        счёт["покупок_wsol"] += 1
                    break
            except (OSError, RuntimeError, ValueError) as exc:
                if попытка == 3:
                    счёт["ошибки"].append(f"{чс}: {type(exc).__name__} {str(exc)[:120]}")
                else:
                    time.sleep(3 * (попытка + 1))
    for pid in list(ф_ждут):
        закрыть_ф(pid, None)

    карты = {}
    for w, к in карточки.items():
        if сидел[w] < ПОРОГ_СИДЕЛ:
            continue
        части = list(к["частей"].values())
        карты[w] = {"сел_в_чужом_слоте": сидел[w], "покупок_всего_в_сутках": пок_все[w],
                    "sol_покупок_всего": round(сол_все[w], 3),
                    "покупок_после_порога": к["покупок"], "sol_покупок_после_порога": round(к["sol_покупок"], 3),
                    "билет_медиана": round(statistics.median(к["билеты"]), 4) if к["билеты"] else None,
                    "доля_того_же_слота": round(к["сел"] / к["покупок"], 3) if к["покупок"] else None,
                    "позиция_медиана": statistics.median(к["позиции"]) if к["позиции"] else None,
                    "типы": dict(к["типы"]), "продаж": к["продаж"], "sol_продаж": round(к["sol_продаж"], 3),
                    "минтов_куплено": len(к["минты"]), "минтов_продано": len(к["минты_продано"]),
                    "частей_на_минт_медиана": statistics.median(части) if части else None,
                    "удержание_слотов_медиана": statistics.median(к["удержание"]) if к["удержание"] else None,
                    "итог_sol_после_порога": round(к["sol_продаж"] - к["sol_покупок"], 3),
                    "ведущие": dict(sorted((ведущие.get(w) or {}).items(), key=lambda kv: -kv[1])[:5])}
    П.mkdir(parents=True, exist_ok=True)
    out = П / (f"snaipery_{д}.json.gz" if часов >= 25 else f"snaipery_proba_{д}_{часов}ch.json.gz")
    out.write_bytes(gzip.compress(json.dumps({
        "день": д, "часы": [часы[0], часы[-1]] if часы else [], "фокус": sorted(фокус), "порог_сидел": ПОРОГ_СИДЕЛ,
        "окно_модели": ОКНО, "выходы": list(ВЫХОДЫ), "наши_источники_n": len(ист),
        "счёт": {**счёт, "действия": dict(счёт["действия"]), "кошельков_с_покупками": len(пок_все),
                 "кошельков_сидели_1+": len(сидел), "кошельков_сидели_порог": len(карты)},
        "фокус_покупки": ф_покупки, "фокус_продажи": ф_продажи, "карточки": карты,
    }, ensure_ascii=False).encode()))
    R.записано(out)
    print(out.name, "строк", счёт["строк"], "покупок wsol", счёт["покупок_wsol"], "фокус покупок", len(ф_покупки),
          "карточек", len(карты), "ошибки", счёт["ошибки"], flush=True)
    return out


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--den", default="")
    р.add_argument("--fokus", default=",".join(ФОКУС))
    р.add_argument("--chasov", type=int, default=25, help="часов суток (проба -- меньше 25)")
    а = р.parse_args()
    if not а.den:
        р.error("нужен --den YYYY-MM-DD")
    день(а.den, {x for x in а.fokus.split(",") if x}, а.chasov)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
