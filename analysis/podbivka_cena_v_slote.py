#!/usr/bin/env python3
"""Где внутри слота источника мы садимся: цена по факту против цены модели.

Зачем. Вердикт забора набором решает не доходность и не доля попадания, а ЦЕНА внутри
слота источника: на конце его слота (S0_дно) набор не проходит, на середине наплыва --
проходит. Значит нужно замерить, какую цену мы получаем на деле.

Как. У живой сделки известны вход в SOL (ликвидная часть, без чаевых и платы) и
полученные токены -- то есть фактическая средняя цена покупки. Состояние пула сразу за
сделкой источника (S0) и в конце его слота (S0_дно) известны из архива. Внутри слота
цена идёт по инварианту x*y, поэтому по факту решается обратная задача: при какой доле
наплыва слота q мы бы получили ровно столько токенов.

    токенов = y*net/(x+net),  y = x0*y0/x,  net = вход/(1+fee)
    =>  x^2 + net*x - x0*y0*net/токенов = 0
    =>  x = (-net + sqrt(net^2 + 4*x0*y0*net/токенов))/2
    =>  q = (x - x0)/(x_дно - x0)

q = 0 -- мы сели сразу за источником, q = 1 -- в конце его слота, q > 1 -- хуже конца
слота (следующий слот). Для сделок с лагом 1 и больше q > 1 и ожидается -- там цена уже
не из слота источника; эти сделки считаются отдельной корзиной.

Сверка идёт по подписи сделки источника (source_sig живой выгрузки против `подпись`
кэша сигналов), при промахе -- по (минт, источник, слот источника).

Только чтение: живая выгрузка Code-1 и кэш сигналов архива. Выход:
data/podbivka/cena_v_slote.json.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_paket_obshee as O  # noqa: E402

П = O.П
ДЕСЯТИЧНЫХ = 6          # pump.fun mint: 6 знаков; иное -- флагом --desyatichnyh


def решить_q(вход_sol: float, токенов: float, s0: list, sд: list,
             fee: float) -> tuple:
    """(q, цена_факт, цена_S0, цена_S0_дно) -- где в наплыве слота мы оказались."""
    x0, y0 = float(s0[0]), float(s0[1])
    xд = float(sд[0])
    if min(x0, y0, вход_sol, токенов) <= 0:
        return None, None, None, None
    net = вход_sol / (1.0 + (fee or 0.0))
    д = net * net + 4.0 * x0 * y0 * net / токенов
    if д <= 0:
        return None, None, None, None
    x = (-net + math.sqrt(д)) / 2.0
    ц_факт = вход_sol / токенов
    ц_s0 = (x0 + net) / y0
    ц_дно = (xд + net) / (x0 * y0 / xд) if xд > 0 else None
    q = (x - x0) / (xд - x0) if abs(xд - x0) > 1e-12 else None
    return q, ц_факт, ц_s0, ц_дно


def главное(а) -> int:
    кэш: dict = {}
    по_троице: dict = {}
    for р in O.сигналы():
        п = р.get("подпись")
        if п:
            кэш[п] = р
        т = (р.get("минт"), р.get("ист"), р.get("блок"))
        if all(т):
            по_троице[т] = р
    д = json.loads(Path(а.vhod).read_text(encoding="utf-8"))
    сделки = д.get("сделки") if isinstance(д, dict) else д
    счёт: collections.Counter = collections.Counter()
    ряды: list = []
    for с in сделки:
        счёт["сделок"] += 1
        if а.gruppa and с.get("group") != а.gruppa:
            continue
        if not с.get("chain_ok"):
            счёт["нет_цепи"] += 1
            continue
        вх = с.get("vhod_sol_likvid_po_cepi") or с.get("vhod_sol_po_cepi")
        ток = с.get("tokenov_polucheno_raw")
        if not (вх and ток):
            счёт["нет_входа_или_токенов"] += 1
            continue
        р = кэш.get(с.get("source_sig")) or по_троице.get(
            (с.get("mint"), с.get("source"), с.get("source_slot")))
        if not р:
            счёт["сигнал_не_найден"] += 1
            continue
        сост = р.get("вход") or {}
        s0, sд = сост.get("S0"), сост.get("S0_дно")
        if not (s0 and sд):
            счёт["нет_состояний"] += 1
            continue
        мсш = float(р.get("масштаб") or 1.0)
        if мсш != 1.0:
            счёт["масштаб_не_1"] += 1
            continue
        токенов = float(ток) / (10.0 ** а.desyatichnyh)
        q, ц_факт, ц_s0, ц_дно = решить_q(float(вх), токенов, s0, sд,
                                          float(р.get("fee") or 0.0))
        if q is None:
            счёт["q_не_решается"] += 1
            continue
        счёт["годных"] += 1
        x0, y0, xд = float(s0[0]), float(s0[1]), float(sд[0])
        net = float(вх) / (1.0 + float(р.get("fee") or 0.0))
        мод = {}
        for кв in (0.0, 0.5, 0.7, 0.784, 1.0):
            x = x0 + кв * (xд - x0)
            y = x0 * y0 / x if x > 0 else None
            мод[str(кв)] = (round(токенов / (y * net / (x + net)), 4)
                            if y and x + net > 0 else None)
        лаг = ((с.get("our_slot") or 0) - (с.get("source_slot") or 0)
               if с.get("our_slot") and с.get("source_slot") else None)
        ои, ии = с.get("our_block_index"), с.get("source_block_index")
        между = (ои - ии - 1 if лаг == 0 and isinstance(ои, int)
                 and isinstance(ии, int) and ои > ии else None)
        ряды.append({
            "utc": с.get("utc"), "минт": с.get("mint"), "лаг": лаг,
            "доля_блока_факт": с.get("our_block_share"),
            "индекс_в_блоке": ои,
            "индекс_источника": ии,
            "транзакций_между": между,
            "транзакций_в_блоке": с.get("our_block_total"),
            "q_замеренное": round(q, 4),
            "наплыв_слота_пп": round((xд / x0 - 1.0) * 100, 2) if x0 else None,
            "цена_факт": ц_факт, "цена_S0": ц_s0, "цена_S0_дно": ц_дно,
            "факт_к_модели_по_q": мод,
            "итог_sol": с.get("итог_sol"), "билет": с.get("size_sol"),
        })
    по_лагу: dict = collections.defaultdict(list)
    for r in ряды:
        по_лагу["лаг 0" if r["лаг"] == 0 else
                ("лаг 1" if r["лаг"] == 1 else "лаг 2+")].append(r)
    свод = {}
    for имя, сп in sorted(по_лагу.items()):
        qs = [r["q_замеренное"] for r in сп]
        нап = [r["наплыв_слота_пп"] for r in сп if r["наплыв_слота_пп"] is not None]
        меж = [r["транзакций_между"] for r in сп
               if r.get("транзакций_между") is not None]
        свод[имя] = {
            "n": len(сп),
            "транзакций_между_медиана": statistics.median(меж) if меж else None,
            "транзакций_между_n": len(меж),
            "q_медиана": round(statistics.median(qs), 3),
            "q_p10": round(sorted(qs)[int(0.1 * len(qs))], 3),
            "q_p90": round(sorted(qs)[min(len(qs) - 1, int(0.9 * len(qs)))], 3),
            "q_меньше_05_доля": round(sum(1 for q in qs if q < 0.5) / len(qs), 3),
            "наплыв_слота_пп_медиана": round(statistics.median(нап), 2) if нап else None,
            "факт_к_модели_медиана": {
                кв: round(statistics.median(
                    [r["факт_к_модели_по_q"][кв] for r in сп
                     if r["факт_к_модели_по_q"].get(кв)]), 4)
                for кв in ("0.0", "0.5", "0.7", "0.784", "1.0")
                if any(r["факт_к_модели_по_q"].get(кв) for r in сп)},
        }
    прошлое = {}
    if а.sravnit:
        пр = Path(а.sravnit)
        if пр.exists():
            прошлое = (json.loads(пр.read_text(encoding="utf-8"))
                       .get("по_лагу") or {})
        else:
            счёт["прошлого_файла_нет"] += 1
    сдвиг = {}
    for имя, с_ in свод.items():
        б = прошлое.get(имя) or {}
        if б.get("q_медиана") is not None:
            сдвиг[имя] = {"было": б["q_медиана"], "стало": с_["q_медиана"],
                          "n_было": б.get("n"), "n_стало": с_["n"],
                          "сдвинулось_на": round(с_["q_медиана"] - б["q_медиана"], 3),
                          "между_было": б.get("транзакций_между_медиана"),
                          "между_стало": с_.get("транзакций_между_медиана")}
    тело = {"что": "где внутри слота источника мы садимся: факт против модели",
            "сдвиг_против_прошлого": сдвиг,
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "вход": str(а.vhod), "группа": а.gruppa or "все",
            "счёт": dict(счёт), "по_лагу": свод, "сделки": ряды}
    вых = П / f"cena_v_slote{а.metka}.json"
    вых.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    import podbivka_run as R  # noqa: PLC0415
    R.записано(вых)
    л0 = свод.get("лаг 0") or {}
    с0 = сдвиг.get("лаг 0")
    if с0 and abs(с0["сдвинулось_на"]) >= а.porog_sdviga:
        print(f"q СДВИНУЛОСЬ с {с0['было']} до {с0['стало']} "
              f"(лаг 0, n было {с0['n_было']}, стало {с0['n_стало']}; "
              f"транзакций между источником и нами: было {с0['между_было']}, "
              f"стало {с0['между_стало']})", flush=True)
    elif с0:
        print(f"q НЕ сдвинулось: было {с0['было']}, стало {с0['стало']} "
              f"(лаг 0, n было {с0['n_было']}, стало {с0['n_стало']})", flush=True)
    elif л0:
        print(f"q при лаге 0: {л0.get('q_медиана')} (n {л0.get('n')}), "
              "сравнивать не с чем -- прошлый прогон не передан", flush=True)
    print(f"счёт: {dict(счёт)}", flush=True)
    for имя, с_ in свод.items():
        print(f"{имя}: n {с_['n']}, q медиана {с_['q_медиана']} "
              f"(p10 {с_['q_p10']}, p90 {с_['q_p90']}), q<0.5 у "
              f"{с_['q_меньше_05_доля']:.0%}, наплыв слота {с_['наплыв_слота_пп_медиана']} пп, "
              f"факт/модель {с_['факт_к_модели_медиана']}", flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--vhod", default=str(П / "4vw_zhivye_celi.json"),
                   help="живая выгрузка сделок Code-1")
    р.add_argument("--gruppa", default="", help="только эта группа (например vol_4vw)")
    р.add_argument("--desyatichnyh", type=int, default=ДЕСЯТИЧНЫХ)
    р.add_argument("--sravnit", default="",
                   help="прошлый cena_v_slote*.json: насколько сдвинулось q")
    р.add_argument("--porog-sdviga", type=float, default=0.05,
                   help="сдвиг q меньше этого считается «не сдвинулось»")
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
