#!/usr/bin/env python3
"""Арбитраж между пулами одного минта по архиву PumpApi (задача владельца 09.10, п.3б).

Зачем отдельный проход. Сборщик запусков (`podbivka_zapuski.py`) видит только пулы,
СОЗДАННЫЕ в окне, и первые 230 слотов их жизни. Вопрос владельца шире: минты, у которых
одновременно живут два и более пула -- в том числе давно созданных. Для этого нужен проход,
который держит последнее состояние каждого пула и сравнивает цены в момент события.

Как считается. На каждом событии с состоянием пула:
  * обновляется последнее состояние этого пула и его счётчик событий;
  * если у минта известен ещё хотя бы один пул, чьё состояние свежее `--svezhest` слотов,
    считается полный круг арбитража: покупка на билет в одном пуле, продажа всего купленного
    в другом, обе наценки, оба тарифа и издержки круга. Считаются оба направления.
  * для каждой пары пулов ведётся длина серии: сколько подряд идущих слотов разница держится
    выгодной. Это и есть ответ на «не меньше 2 слотов».

Что в счёт не идёт и почему:
  * кривые (pump, launchpad) -- слово владельца: завершившая кривая больше не торгует,
    значит пары «кривая + пул» нет. Пока кривая не завершена, другого пула у минта нет;
  * пулы сосредоточенной ликвидности (DLMM, CLMM, Whirlpool) -- формула постоянного
    произведения на полных резервах для них неверна;
  * пулы беднее `--rezerv` SOL и с меньше чем `--sobytiy` событиями -- это не рынок, а
    пылевая засевка переезда (в потоке переезд размечен дважды: настоящий пул около 85 SOL
    и засевка на 1e-5 SOL);
  * котировка не SOL -- курс на момент события не собран.

Только чтение. Выход: data/podbivka/arbitrazh/<метка>.json.gz и сводка в
data/podbivka/arbitrazh_svod.json.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import io
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
# Полоса годности подогнанной наценки. Прежняя 0.5..1.0 пропускала мусор: у pump-amm
# подгонка давала 0.77-0.81 вместо структурной 1.0 (тариф там берётся с выхода), и это
# съедало 20-24 п.п. на круге. Настоящая наценка не может быть ниже 1 - тарифа - запас, а
# тарифы пулов тут не больше 5 %, поэтому ниже 0.9 значение просто неверно.
ГОДНО_НАЦЕНКЕ = (0.9, 1.0)
# Тариф больше этого -- не дробь (у DAMM v2 встречается 0.5 и даже 1.188): как дробь он
# даёт отрицательную выручку на продаже. Такие пулы в счёт не идут.
ТАРИФ_ПРЕДЕЛ = 0.3
# Подгонка годна только по СОСЕДНИМ событиям: иначе предыдущее состояние старое, и
# формула занижает наценку.
СЛОТОВ_ДЛЯ_ПОДГОНКИ = 2
ИЗДЕРЖКИ = 0.002015
К_G = 0.9908
XYK = {"raydium-cpmm", "meteora-damm-v1", "pump-amm"}
КРИВЫЕ = {"pump", "raydium-launchpad", "meteora-launchpad", "meteora-dbc"}
НЕ_СЧИТАЕМ = {"meteora-dlmm", "raydium-clmm", "orca-whirlpool"}
ПРЕДЕЛ_ПУЛОВ = 400_000          # дальше вытесняем самые старые по последнему событию

р_pool = re.compile(r'"poolId":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_tip = re.compile(r'"pool":\s*"([a-z0-9\-]+)"')
р_block = re.compile(r'"block":\s*(\d+)')
р_mint = re.compile(r'"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_qmint = re.compile(r'"quoteMint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_action = re.compile(r'"action":\s*"([a-zA-Z_]+)"')
р_fee = re.compile(r'"poolFeeRate":\s*"?([0-9.eE+-]+)')
р_tam = re.compile(r'"tokenAmount":\s*"?([0-9.eE+-]+)')
р_qam = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')
р_q = re.compile(r'"(quoteInPool|tokensInPool|vQuoteInBondingCurve|vTokensInBondingCurve)":\s*"?([0-9.eE+-]+)')


def число(м) -> float | None:
    if not м:
        return None
    try:
        return float(м.group(1))
    except ValueError:
        return None


def состояние(стр: str) -> tuple | None:
    поля = dict(р_q.findall(стр))
    x = поля.get("vQuoteInBondingCurve") or поля.get("quoteInPool")
    y = поля.get("vTokensInBondingCurve") or поля.get("tokensInPool")
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        return None
    return (x, y) if x > 0 and y > 0 else None


def часы(с: str, сколько: int) -> list:
    т = time.strptime(с, "%Y-%m-%dT%H")
    н = int(time.mktime(т)) - time.timezone
    return [time.strftime("%Y/%m/%d/%H", time.gmtime(н + 3600 * k)) for k in range(сколько)]


def куплено(с, пул: str, f, fee: float, a: float) -> float | None:
    x, y = с
    if пул in XYK:
        if not f:
            return None
        т = y * f * a / (x + f * a)
    else:
        net = a / (1 + (fee or 0))
        т = y * net / (x + net)
    return т if т > 0 else None


def продано(с, пул: str, g, fee: float, т: float) -> float | None:
    x, y = с
    if пул in XYK:
        if not g:
            return None
        return x * g * т / (y + т)
    return x * т / (y + т) * (1 - (fee or 0)) * К_G


def наценки(п: dict) -> None:
    """f и g пула -- медианы подогнанных значений; считаются при сравнении, не на каждом
    событии (строк в сутки 70 млн, медиана на каждой обошлась бы дороже самого прохода)."""
    if п.get("свежо"):
        return
    т_ = п.get("тариф")
    # у pump-amm тариф берётся с выхода: f = 1, g = 1 - тариф (замерено на цепи, медиана f
    # ровно 1.00000 по 3884 пулам суток 24.09). Запас 1 - тариф для f занижал покупку.
    запас_f = 1.0 if п.get("тип") == "pump-amm" else (1 - т_ if т_ is not None else None)
    п["f"] = statistics.median(п["fs"]) if п["fs"] else запас_f
    п["g"] = statistics.median(п["gs"]) if п["gs"] else (1 - т_ if т_ is not None else None)
    п["свежо"] = True


def main() -> int:  # noqa: PLR0912, PLR0915
    import subprocess  # noqa: PLC0415
    import requests  # noqa: PLC0415
    try:
        import zstandard  # noqa: PLC0415
    except ModuleNotFoundError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"], check=True)
        import zstandard  # noqa: PLC0415
    import podbivka_arhiv_den as AD  # noqa: PLC0415
    import podbivka_run as R  # noqa: PLC0415

    р_ = argparse.ArgumentParser()
    р_.add_argument("--s", required=True, help="начало окна, YYYY-MM-DDTHH")
    р_.add_argument("--chasov", type=int, default=24)
    р_.add_argument("--bilety", default="0.1,0.3,1.0")
    р_.add_argument("--svezhest", type=int, default=4,
                    help="состояние второго пула не старше столько слотов")
    р_.add_argument("--rezerv", type=float, default=1.0, help="минимальный резерв пула, SOL")
    р_.add_argument("--sobytiy", type=int, default=10, help="минимум событий у пула")
    р_.add_argument("--metka", default="")
    а = р_.parse_args()
    билеты = [float(x) for x in а.bilety.split(",") if x.strip()]
    метка = а.metka or f"arb_{а.s}"

    пулы: dict = {}            # poolId -> данные пула
    по_минту: dict = collections.defaultdict(set)
    серии: dict = {}           # (poolA, poolB, билет) -> [последний_блок, длина]
    счёт: collections.Counter = collections.Counter()
    итоги: dict = {б: {"круги": 0, "в_плюсе": 0, "сумма": 0.0, "пп": [],
                       "серии": collections.Counter(), "минты": set()} for б in билеты}
    верхние: list = []
    первый_блок = последний_блок = None

    def читать_час(поток) -> None:
        """Тело часа отдельной функцией -- чтобы обрыв потока ловился и час повторялся.

        Поток часа рвётся (urllib3 IncompleteRead): без повтора из-за одного часа
        теряются целые сутки. Что не дочиталось после трёх попыток, честно считается
        в «обрыв_часов».
        """
        nonlocal первый_блок, последний_блок

        for стр in io.TextIOWrapper(
                zstandard.ZstdDecompressor().stream_reader(поток.raw),
                encoding="utf-8", errors="ignore"):
            счёт["строк"] += 1
            пм = р_pool.search(стр)
            бм = р_block.search(стр)
            if not (пм and бм):
                continue
            pid, блок = пм.group(1), int(бм.group(1))
            if первый_блок is None:
                первый_блок = блок
            последний_блок = блок
            сост = состояние(стр)
            if not сост:
                continue
            п = пулы.get(pid)
            if п is None:
                тм = р_tip.search(стр)
                тип = тм.group(1) if тм else None
                if тип in КРИВЫЕ or тип in НЕ_СЧИТАЕМ or тип is None:
                    счёт["пул_мимо_типа"] += 1
                    пулы[pid] = {"мимо": True, "блок": блок}
                    continue
                qm = р_qmint.search(стр)
                if not qm or qm.group(1) != WSOL:
                    счёт["пул_не_sol"] += 1
                    пулы[pid] = {"мимо": True, "блок": блок}
                    continue
                мм = р_mint.search(стр)
                п = {"мимо": False, "тип": тип, "минт": мм.group(1) if мм else None,
                     "сост": сост, "блок": блок, "событий": 1, "тариф": число(р_fee.search(стр)),
                     "fs": [], "gs": [], "f": None, "g": None, "свежо": False}
                пулы[pid] = п
                if п["минт"]:
                    по_минту[п["минт"]].add(pid)
                счёт["пулов"] += 1
                if len(пулы) > ПРЕДЕЛ_ПУЛОВ:
                    стар = sorted(пулы.items(), key=lambda kv: kv[1]["блок"])[:50_000]
                    for k, v in стар:
                        if not v.get("мимо") and v.get("минт"):
                            по_минту[v["минт"]].discard(k)
                        пулы.pop(k, None)
                    счёт["вытеснено"] += len(стар)
                continue
            if п.get("мимо"):
                п["блок"] = блок
                continue
            # подгонка f и g по паре «предыдущее состояние -> этот своп»
            if п["тип"] in XYK and (len(п["fs"]) < 40 or len(п["gs"]) < 40):
                д_ = р_action.search(стр)
                dy, dx = число(р_tam.search(стр)), число(р_qam.search(стр))
                x0, y0 = п["сост"]
                if д_ and dy and dx:
                    if д_.group(1) == "buy" and y0 > dy > 0 and dx > 0:
                        v = x0 * dy / ((y0 - dy) * dx)
                        if ГОДНО_НАЦЕНКЕ[0] <= v <= ГОДНО_НАЦЕНКЕ[1]:
                            п["fs"].append(v)
                            п["свежо"] = False
                    elif д_.group(1) == "sell" and dy > 0 and x0 > 0:
                        v = dx * (y0 + dy) / (x0 * dy)
                        if ГОДНО_НАЦЕНКЕ[0] <= v <= ГОДНО_НАЦЕНКЕ[1]:
                            п["gs"].append(v)
                            п["свежо"] = False
            if п["тариф"] is None:
                п["тариф"] = число(р_fee.search(стр))
            п["сост"], п["блок"] = сост, блок
            п["событий"] += 1
            # второй пул того же минта
            if not п["минт"] or len(по_минту.get(п["минт"]) or ()) < 2:
                continue
            if п["событий"] < а.sobytiy or сост[0] < а.rezerv:
                continue
            for pid2 in по_минту[п["минт"]]:
                if pid2 == pid:
                    continue
                п2 = пулы.get(pid2)
                if (not п2 or п2.get("мимо") or п2["событий"] < а.sobytiy
                        or п2["сост"][0] < а.rezerv
                        or блок - п2["блок"] > а.svezhest):
                    continue
                счёт["сравнений"] += 1
                for б in билеты:
                    лучший = None
                    наценки(п)
                    наценки(п2)
                    for A, B, ka, kb in ((п, п2, pid, pid2), (п2, п, pid2, pid)):
                        т = куплено(A["сост"], A["тип"], A["f"], A["тариф"], б)
                        if not т:
                            continue
                        out = продано(B["сост"], B["тип"], B["g"], B["тариф"], т)
                        if not out:
                            continue
                        итог = out - б - ИЗДЕРЖКИ
                        if лучший is None or итог > лучший[0]:
                            лучший = (итог, ka, kb, A["тип"], B["тип"])
                    if лучший is None:
                        continue
                    итог, ka, kb, та, тб = лучший
                    и = итоги[б]
                    и["круги"] += 1
                    и["пп"].append(100 * итог / б)
                    ключ = (min(ka, kb), max(ka, kb), б)
                    пред = серии.get(ключ)
                    if итог > 0:
                        и["в_плюсе"] += 1
                        и["сумма"] += итог
                        и["минты"].add(п["минт"])
                        if пред and блок - пред[0] <= 1:
                            пред[0], пред[1] = блок, пред[1] + 1
                        else:
                            серии[ключ] = [блок, 1]
                        if len(верхние) < 4000:
                            верхние.append({"минт": п["минт"], "блок": блок, "билет": б,
                                            "итог_sol": round(итог, 6),
                                            "пулы": [та, тб]})
                    elif пред:
                        и["серии"][min(пред[1], 20)] += 1
                        серии.pop(ключ, None)

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
                    if попытка < 2:
                        time.sleep(5 * (попытка + 1))
                        continue
                    счёт["обрыв_часов"] += 1
                    print(f"  час {ч}: поток рвался трижды, идём дальше", flush=True)
                    break
                счёт["часов"] += 1
                break

    for ключ, v in серии.items():
        итоги[ключ[2]]["серии"][min(v[1], 20)] += 1

    сут = max(1, счёт["часов"]) / 24
    свод = {}
    for б, и in итоги.items():
        пп = sorted(и["пп"])
        дв = sum(n for k, n in и["серии"].items() if k >= 2)
        свод[str(б)] = {
            "сравнений": и["круги"], "в_плюсе": и["в_плюсе"],
            "доля_в_плюсе": round(100 * и["в_плюсе"] / max(1, и["круги"]), 3),
            "медиана_пп": round(statistics.median(пп), 3) if пп else None,
            "p95_пп": round(пп[int(0.95 * (len(пп) - 1))], 3) if пп else None,
            "лучший_пп": round(пп[-1], 3) if пп else None,
            "итог_sol": round(и["сумма"], 6), "sol_в_сутки": round(и["сумма"] / сут, 6),
            "минтов_с_выгодой": len(и["минты"]),
            "серий_всего": sum(и["серии"].values()), "серий_2_и_больше_слотов": дв,
            "серии_по_длине": dict(sorted(и["серии"].items()))}
    верхние.sort(key=lambda x: -x["итог_sol"])
    тело = {"что": "арбитраж между пулами одного минта", "окно": {"с": а.s, "часов": а.chasov},
            "параметры": {"билеты": билеты, "свежесть_слотов": а.svezhest,
                          "резерв_мин_sol": а.rezerv, "событий_мин": а.sobytiy,
                          "издержки_sol": ИЗДЕРЖКИ, "к_g": К_G},
            "счёт": dict(счёт), "блоки": {"первый": первый_блок, "последний": последний_блок},
            "минтов_с_2_пулами": sum(1 for v in по_минту.values() if len(v) >= 2),
            "по_билетам": свод, "верхние": верхние[:50]}
    пап = П / "arbitrazh"
    пап.mkdir(parents=True, exist_ok=True)
    вых = пап / f"{метка}.json.gz"
    with gzip.open(вых, "wt", encoding="utf-8") as ф:
        json.dump(тело, ф, ensure_ascii=False)
    R.записано(вых)
    св = П / "arbitrazh_svod.json"
    было = json.loads(св.read_text(encoding="utf-8")) if св.exists() else {}
    было[метка] = {k: v for k, v in тело.items() if k != "верхние"}
    св.write_text(json.dumps(было, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(св)
    for б, v in свод.items():
        print(f"билет {б}: сравнений {v['сравнений']}, в плюсе {v['доля_в_плюсе']} %, "
              f"{v['sol_в_сутки']:+.4f} SOL/сут, минтов {v['минтов_с_выгодой']}, "
              f"серий 2+ слота {v['серий_2_и_больше_слотов']}", flush=True)
    print(f"пулов {счёт['пулов']}, минтов с 2+ пулами {тело['минтов_с_2_пулами']}, "
          f"сравнений {счёт['сравнений']}, строк {счёт['строк']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
