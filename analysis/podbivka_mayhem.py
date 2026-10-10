#!/usr/bin/env python3
"""Mayhem: кто торгует на этих кривых и предсказуема ли цена после его сделок.

Зачем. Mayhem -- 31 % запусков кривой pump.fun (189 590 из 605 880 за 17 суток). Правило
цены у них другое: произведение резервов k = x*y не держится, и модель постоянного
произведения на них неверна (медиана модель/факт 0.8504). Флаг is_mayhem_mode лежит в счёте
кривой, но RPC на каждый запуск не поставить, поэтому порода опознаётся по архиву -- по
ДРЕЙФУ k. На истине из 127 минтов с прочитанным флагом дрейф есть у 105 из 109 Mayhem и у
0 из 18 не-Mayhem.

Что делает. Один проход по окну архива: регистрирует запуски кривой pump.fun, следит за
дрейфом k у каждого (как только дрейф превысил предел -- запуск помечен Mayhem), и по
Mayhem-запускам собирает ВСЕ свопы: кошелёк, сторона, размер, сдвиг слота от створа,
состояние пула. Дальше складывает по кошелькам -- на сколько РАЗНЫХ Mayhem-минтов кошелёк
пришёл, сколько купил и продал, какими размерами, на каких сдвигах. Агент площадки должен
быть виден так же, как был виден агент BOOST: кошелёк, который приходит во МНОГИЕ запуски
подряд. Имя не задаётся -- оно находится по частоте.

Контроль обязателен: те же кошельки считаются и на НЕ-Mayhem запусках того же окна. Без
этого Mayhem-агента не отличить от обычного снайпера, который ходит во все запуски.

Предсказуемость цены: по сделкам верхних кошельков считается путь цены пула после сделки на
горизонтах 5 / 10 / 20 / 40 / 90 / 160 / 230 слотов -- медиана отношения цены и доля выше
нуля, покупки и продажи раздельно. Цена -- x/y состояния пула; для Mayhem это НЕ цена
исполнения (правило другое), поэтому число читается как «куда шёл пул», а не как «сколько
мы бы заработали».

Только чтение архива. Выход: data/podbivka/mayhem.json.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_arhiv_den as AD      # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
K0_КРИВОЙ = 32.19                 # x0*y0/1e9 канонического старта 30.0 / 1 073 000 000
ДРЕЙФ_ПРЕДЕЛ = 1e-4
ОКНО_СЛОТОВ = 240                 # сколько слотов после створа следим за запуском
ГОРИЗОНТЫ = (5, 10, 20, 40, 90, 160, 230)
ВЕРХ_КОШЕЛЬКОВ = 40
СОБЫТИЙ_НА_ЗАПУСК = 400           # предел на ряд, чтобы память не росла

р_block = re.compile(r'"block":\s*(\d+)')
р_action = re.compile(r'"action":\s*"([a-zA-Z_]+)"')
р_pool = re.compile(r'"poolId":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_tip = re.compile(r'"pool":\s*"([a-z0-9\-]+)"')
р_signer = re.compile(r'"txSigner":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_trader = re.compile(r'"trader":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_mint = re.compile(r'"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_qmint = re.compile(r'"quoteMint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_qam = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')
р_q = re.compile(r'"(quoteInPool|tokensInPool|vQuoteInBondingCurve|vTokensInBondingCurve)":'
                 r'\s*"?([0-9.eE+-]+)')


def часы(с: str, сколько: int) -> list:
    т = time.strptime(с, "%Y-%m-%dT%H")
    н = int(time.mktime(т)) - time.timezone
    return [time.strftime("%Y/%m/%d/%H", time.gmtime(н + 3600 * k)) for k in range(сколько)]


def состояние(стр: str) -> tuple | None:
    поля = dict(р_q.findall(стр))
    x = поля.get("vQuoteInBondingCurve") or поля.get("quoteInPool")
    y = поля.get("vTokensInBondingCurve") or поля.get("tokensInPool")
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        return None
    return (x, y) if x > 0 and y > 0 else None


def число(м):
    if not м:
        return None
    try:
        return float(м.group(1))
    except ValueError:
        return None


def путь_цены(ряд: list) -> dict:
    """Отношение цены на горизонте к цене сделки, по каждой сделке ряда."""
    из_: dict = {}
    for i, e in enumerate(ряд):
        if not e["цена"]:
            continue
        for h in ГОРИЗОНТЫ:
            поздние = [z for z in ряд[i:] if z["блок"] >= e["блок"] + h and z["цена"]]
            if not поздние:
                continue
            из_.setdefault(h, []).append(поздние[0]["цена"] / e["цена"] - 1)
    return из_


def главное(а) -> int:  # noqa: PLR0912, PLR0915
    import io            # noqa: PLC0415
    import requests      # noqa: PLC0415
    import zstandard     # noqa: PLC0415

    счёт: collections.Counter = collections.Counter()
    живые: dict = {}          # poolId -> запуск
    готовые: list = []
    последний_блок = 0

    def закрыть(до_блока: int) -> None:
        for pid in [k for k, v in живые.items() if v["блок"] + ОКНО_СЛОТОВ < до_блока]:
            з = живые.pop(pid)
            счёт["mayhem_запусков" if з["mayhem"] else "нормальных_запусков"] += 1
            готовые.append(з)

    def читать_час(поток) -> None:
        nonlocal последний_блок
        for стр in io.TextIOWrapper(
                zstandard.ZstdDecompressor().stream_reader(поток.raw),
                encoding="utf-8", errors="ignore"):
            счёт["строк"] += 1
            бм = р_block.search(стр)
            if not бм:
                continue
            блок = int(бм.group(1))
            if блок > последний_блок:
                последний_блок = блок
                if счёт["строк"] % 20000 == 0:
                    закрыть(блок)
            пм = р_pool.search(стр)
            if not пм:
                continue
            pid = пм.group(1)
            ам = р_action.search(стр)
            д = ам.group(1) if ам else None
            сост = состояние(стр)
            з = живые.get(pid)
            if з is None:
                if д in ("buy", "sell") or not сост:
                    continue
                тип = р_tip.search(стр)
                if (тип.group(1) if тип else None) != "pump":
                    continue
                qm = р_qmint.search(стр)
                if (qm.group(1) if qm else None) != WSOL:
                    счёт["створ_не_SOL"] += 1
                    continue
                k0 = сост[0] * сост[1] / 1e9
                if abs(k0 / K0_КРИВОЙ - 1) > 1e-3:
                    счёт["створ_не_канонический"] += 1
                    continue
                мм = р_mint.search(стр)
                счёт["створов"] += 1
                живые[pid] = {"poolId": pid, "минт": мм.group(1) if мм else None,
                              "блок": блок, "k0": k0, "mayhem": False,
                              "дрейф_макс": 0.0, "ряд": []}
                continue
            if блок > з["блок"] + ОКНО_СЛОТОВ:
                з2 = живые.pop(pid)
                счёт["mayhem_запусков" if з2["mayhem"] else "нормальных_запусков"] += 1
                готовые.append(з2)
                continue
            if not сост:
                continue
            k = сост[0] * сост[1] / 1e9
            д_ = abs(k / з["k0"] - 1)
            if д_ > з["дрейф_макс"]:
                з["дрейф_макс"] = д_
            if д_ > ДРЕЙФ_ПРЕДЕЛ:
                з["mayhem"] = True
            if д not in ("buy", "sell"):
                continue
            if len(з["ряд"]) >= СОБЫТИЙ_НА_ЗАПУСК:
                счёт["ряд_обрезан"] += 1
                continue
            тм = р_trader.search(стр)
            sм = р_signer.search(стр)
            кто = (тм.group(1) if тм else None) or (sм.group(1) if sм else None)
            qm = р_qmint.search(стр)
            sol = (число(р_qam.search(стр))
                   if (qm.group(1) if qm else None) == WSOL else None)
            з["ряд"].append({"блок": блок, "сдвиг": блок - з["блок"], "кто": кто,
                             "сторона": д, "sol": sol,
                             "цена": сост[0] / сост[1] if сост[1] else None})

    часы_прочитаны = []
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
                    счёт["часов_нет_в_архиве"] += 1
                    break
                try:
                    читать_час(поток)
                except Exception as exc:  # noqa: BLE001
                    счёт[f"обрыв_{type(exc).__name__}"] += 1
                    if попытка == 2:
                        счёт["обрыв_часов"] += 1
                    continue
            счёт["часов"] += 1
            часы_прочитаны.append(ч)
            break
        print(f"  {ч}: строк {счёт['строк']}, створов {счёт['створов']}, "
              f"mayhem {счёт['mayhem_запусков']}", flush=True)
    закрыть(последний_блок + ОКНО_СЛОТОВ + 1)

    # ---- кошельки на Mayhem и на нормальных, рядом ------------------------------
    def пусто() -> dict:
        return {"минтов": set(), "покупок": 0, "продаж": 0, "sol_покупок": [],
                "sol_продаж": [], "сдвиги": [], "первых_в_створе": 0}

    по_кош: dict = {"mayhem": collections.defaultdict(пусто),
                    "нормальные": collections.defaultdict(пусто)}
    for з in готовые:
        г = "mayhem" if з["mayhem"] else "нормальные"
        for e in з["ряд"]:
            if not e["кто"]:
                continue
            к = по_кош[г][e["кто"]]
            к["минтов"].add(з["минт"] or з["poolId"])
            к["сдвиги"].append(e["сдвиг"])
            if e["сторона"] == "buy":
                к["покупок"] += 1
                if e["sol"] is not None:
                    к["sol_покупок"].append(e["sol"])
                if e["сдвиг"] == 0:
                    к["первых_в_створе"] += 1
            else:
                к["продаж"] += 1
                if e["sol"] is not None:
                    к["sol_продаж"].append(e["sol"])

    def кванты(v: list) -> dict:
        if not v:
            return {"n": 0}
        v = sorted(v)
        return {"n": len(v), "p10": round(v[len(v) // 10], 5),
                "медиана": round(statistics.median(v), 5),
                "p90": round(v[9 * len(v) // 10], 5)}

    mh_запусков = счёт["mayhem_запусков"] or 1
    норм_запусков = счёт["нормальных_запусков"] or 1
    карточки = []
    for адр, к in sorted(по_кош["mayhem"].items(), key=lambda kv: -len(kv[1]["минтов"]))[:ВЕРХ_КОШЕЛЬКОВ]:
        н = по_кош["нормальные"].get(адр)
        карточки.append({
            "адрес": адр,
            "mayhem_минтов": len(к["минтов"]),
            "доля_mayhem_запусков": round(len(к["минтов"]) / mh_запусков, 4),
            "нормальных_минтов": len(н["минтов"]) if н else 0,
            "доля_нормальных_запусков": (round(len(н["минтов"]) / норм_запусков, 4)
                                         if н else 0.0),
            "покупок": к["покупок"], "продаж": к["продаж"],
            "в_створе_покупок": к["первых_в_створе"],
            "sol_покупок": кванты(к["sol_покупок"]),
            "sol_продаж": кванты(к["sol_продаж"]),
            "сдвиг_слотов": кванты(к["сдвиги"])})

    # ---- путь цены после сделок верхних кошельков -------------------------------
    верх = [x["адрес"] for x in карточки[:а.verh_puti]]
    путь: dict = {а_: {"buy": collections.defaultdict(list),
                       "sell": collections.defaultdict(list)} for а_ in верх}
    for з in готовые:
        if not з["mayhem"]:
            continue
        ряд = з["ряд"]
        цены = [(e["блок"], e["цена"]) for e in ряд if e["цена"]]
        if not цены:
            continue
        for i, e in enumerate(ряд):
            if e["кто"] not in путь or not e["цена"]:
                continue
            for h in ГОРИЗОНТЫ:
                поздние = [c for б, c in цены if б >= e["блок"] + h]
                if поздние:
                    путь[e["кто"]][e["сторона"]][h].append(поздние[0] / e["цена"] - 1)

    путь_свод = {}
    for а_, стороны in путь.items():
        с_ = {}
        for сторона, по_h in стороны.items():
            с_[сторона] = {str(h): {"n": len(v),
                                    "медиана_пп": round(100 * statistics.median(v), 3),
                                    "выше_нуля_проц": round(
                                        100 * sum(1 for x in v if x > 0) / len(v), 1)}
                           for h, v in sorted(по_h.items()) if v}
        путь_свод[а_] = с_

    тело = {"что": "Mayhem: кто торгует на этих кривых и предсказуема ли цена после",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "окно": {"с": а.s, "часов": а.chasov},
            "часы_прочитаны": часы_прочитаны,
            "порода": {"признак": f"дрейф k больше {ДРЕЙФ_ПРЕДЕЛ} на окне "
                                  f"{ОКНО_СЛОТОВ} слотов",
                       "истина": "на 127 минтах с флагом по цепи дрейф есть у 105 из 109 "
                                 "Mayhem и у 0 из 18 не-Mayhem"},
            "счёт": dict(счёт),
            "кошельков_на_mayhem": len(по_кош["mayhem"]),
            "кошельков_на_нормальных": len(по_кош["нормальные"]),
            "верхние_кошельки": карточки,
            "путь_цены_после_сделок": путь_свод,
            "как_читать_путь": "цена -- x/y состояния пула. У Mayhem правило цены другое, "
                               "и это НЕ цена исполнения: число говорит, куда шёл пул, а "
                               "не сколько мы бы заработали"}
    ф = П / f"mayhem{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    print(json.dumps({k: v for k, v in тело.items()
                      if k not in ("верхние_кошельки", "путь_цены_после_сделок")},
                     ensure_ascii=False, indent=1), flush=True)
    print("\nверхние кошельки на Mayhem:", flush=True)
    for x in карточки[:15]:
        print(f"  {x['адрес'][:12]} mayhem-минтов {x['mayhem_минтов']:>5} "
              f"({x['доля_mayhem_запусков']:.1%}) нормальных {x['нормальных_минтов']:>5} "
              f"({x['доля_нормальных_запусков']:.1%}) покупок {x['покупок']:>5} "
              f"продаж {x['продаж']:>5} в створе {x['в_створе_покупок']:>5} "
              f"sol медиана {x['sol_покупок'].get('медиана')}", flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", required=True, help="начало окна архива, YYYY-MM-DDTHH")
    р.add_argument("--chasov", type=int, default=3)
    р.add_argument("--verh-puti", type=int, default=8,
                   help="по скольким верхним кошелькам считать путь цены")
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
