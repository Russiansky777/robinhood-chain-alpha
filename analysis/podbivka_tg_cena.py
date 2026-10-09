#!/usr/bin/env python3
"""Путь цены для коллов TG-каналов по архиву PumpApi (задача владельца 09.10, п.2в-2г).

Что это. Сборщик `podbivka_tg_sbor.py` даёт посты каналов: время поста по секундам (UTC) и
найденные в тексте base58-минты. Здесь по каждому ПЕРВОМУ коллу минта в канале снимается
путь цены вокруг момента поста -- чтобы посчитать, что получилось бы, если входить за
каналом, а не за кошельком-источником.

Что пишется на каждый колл:
  * состояния пула для входа: момент поста и +1 / +2 / +5 секунд после него (наш вход --
    пост+2 с и пост+5 с, остальные для сверки сдвига);
  * состояния для выхода: +10 / +30 / +60 / +120 / +300 секунд;
  * чужие покупки ВПЕРЁД НАС: сколько покупок, какими кошельками и на сколько SOL прошло
    за 1, 2 и 3 секунды от поста -- это и есть ответ «сколько чужих покупок в первые 1-3 с»;
  * пик цены после поста и слот пика, первая продажа, тариф пула и подогнанные f и g
    (без них калиброванная модель круга не считает).

Окно часов берётся по самим постам: качаются только те часы архива, в которых есть коллы,
плюс следующий час -- хвост +300 секунд. Минты, которых в архиве не нашлось, печатаются
списком: по ним цена добирается по цепи отдельным проходом, а выдумывать их нельзя.

Только чтение. Выход: data/podbivka/tg/cena_<метка>.json.gz и сводка в
data/podbivka/tg/cena_svod.json.
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import io
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_tg_sbor as S      # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ПАПКА = П / "tg"
WSOL = "So11111111111111111111111111111111111111112"
ВХОДЫ_С = (0, 1, 2, 5)
ВЫХОДЫ_С = (10, 30, 60, 120, 300)
ВПЕРЁД_С = (1, 2, 3)
ХВОСТ_С = max(ВЫХОДЫ_С)
ДО_ПОСТА_С = 60                  # столько секунд до поста держим для цены «на момент колла»
XYK = {"raydium-cpmm", "meteora-damm-v1", "pump-amm"}

р_pool = re.compile(r'"poolId":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_tip = re.compile(r'"pool":\s*"([a-z0-9\-]+)"')
р_block = re.compile(r'"block":\s*(\d+)')
р_ts = re.compile(r'"timestamp":\s*(\d+)')
р_mint = re.compile(r'"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_qmint = re.compile(r'"quoteMint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_action = re.compile(r'"action":\s*"([a-zA-Z_]+)"')
р_fee = re.compile(r'"poolFeeRate":\s*"?([0-9.eE+-]+)')
р_tam = re.compile(r'"tokenAmount":\s*"?([0-9.eE+-]+)')
р_qam = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')
р_trader = re.compile(r'"trader":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_signer = re.compile(r'"txSigner":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
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


def коллы(папка: Path) -> list:
    """Первый колл каждого минта в каждом канале, по времени поста."""
    первые: dict = {}
    счёт_мимо: dict = {}
    всего = 0
    for п in sorted(glob.glob(str(папка / "posty_*.jsonl.gz"))):
        with gzip.open(п, "rt", encoding="utf-8") as ф:
            for стр in ф:
                try:
                    з = json.loads(стр)
                except json.JSONDecodeError:
                    continue
                if not з.get("utc") or not з.get("минты"):
                    continue
                try:
                    ts = int(time.mktime(time.strptime(
                        з["utc"][:19], "%Y-%m-%dT%H:%M:%S"))) - time.timezone
                except ValueError:
                    continue
                for м in з["минты"]:
                    # уже собранные файлы могли попасть под старую регулярку без границ:
                    # из адреса EVM выкусывался кусок из 32 символов алфавита base58.
                    # Проверяем здесь, чтобы не перекачивать историю заново.
                    if м == WSOL or not S.адрес_solana(м):
                        счёт_мимо[м[:8]] = счёт_мимо.get(м[:8], 0) + 1
                        continue
                    всего += 1
                    к = (з["канал"], м)
                    if к not in первые or ts < первые[к]["ts"]:
                        первые[к] = {"канал": з["канал"], "минт": м, "ts": ts,
                                     "utc": з["utc"], "id": з.get("id")}
    из_ = sorted(первые.values(), key=lambda x: x["ts"])
    print(f"коллов в постах {всего}, первых по паре канал+минт {len(из_)}; "
          f"не адресов Solana отброшено {sum(счёт_мимо.values())} "
          f"({len(счёт_мимо)} разных)", flush=True)
    return из_


def часы_под_коллы(кс: list) -> list:
    """Только те часы архива, где есть коллы, плюс следующий -- хвост +300 с."""
    нужны: set = set()
    for к in кс:
        ч = к["ts"] - к["ts"] % 3600
        нужны.add(ч)
        if (к["ts"] % 3600) < ДО_ПОСТА_С:
            нужны.add(ч - 3600)
        if (к["ts"] % 3600) + ХВОСТ_С >= 3600:
            нужны.add(ч + 3600)
    return [time.strftime("%Y/%m/%d/%H", time.gmtime(ч)) for ч in sorted(нужны)]


def наценки(п: dict) -> None:
    т_ = п.get("тариф")
    п["f"] = statistics.median(п["fs"]) if п["fs"] else (1 - т_ if т_ is not None else None)
    п["g"] = statistics.median(п["gs"]) if п["gs"] else (1 - т_ if т_ is not None else None)


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
    р_.add_argument("--s", default="", help="брать коллы не раньше этой даты, YYYY-MM-DD")
    р_.add_argument("--do", default="", help="и не позже этой")
    р_.add_argument("--metka", default="")
    р_.add_argument("--predel-chasov", type=int, default=0, help="0 -- все нужные часы")
    а = р_.parse_args()
    метка = а.metka or time.strftime("%Y-%m-%d", time.gmtime())

    кс = коллы(ПАПКА)
    if а.s:
        кс = [к for к in кс if к["utc"][:10] >= а.s]
    if а.do:
        кс = [к for к in кс if к["utc"][:10] <= а.do]
    if not кс:
        print("нет коллов в окне: сначала podbivka_tg_sbor.py --rezhim istoriya", flush=True)
        return 1
    по_минту: dict = collections.defaultdict(list)
    for к in кс:
        по_минту[к["минт"]].append(к)
    часы = часы_под_коллы(кс)
    if а.predel_chasov:
        часы = часы[:а.predel_chasov]
    print(f"коллов в окне {len(кс)}, минтов {len(по_минту)}, часов архива {len(часы)}",
          flush=True)

    ряды: dict = {}            # (канал, минт) -> запись
    пулы: dict = {}            # poolId -> тариф и подгонка
    счёт: collections.Counter = collections.Counter()

    for ч in часы:
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        try:
            о = AD.открыть_час(requests, url, ч)
        except Exception as exc:  # noqa: BLE001
            счёт[f"ошибка_{type(exc).__name__}"] += 1
            continue
        with о as поток:
            if getattr(поток, "status_code", 200) != 200:
                счёт[f"http_{getattr(поток, 'status_code', '?')}"] += 1
                continue
            счёт["часов"] += 1
            for стр in io.TextIOWrapper(
                    zstandard.ZstdDecompressor().stream_reader(поток.raw),
                    encoding="utf-8", errors="ignore"):
                счёт["строк"] += 1
                мм = р_mint.search(стр)
                if not мм or мм.group(1) not in по_минту:
                    continue
                минт = мм.group(1)
                qm = р_qmint.search(стр)
                if not qm or qm.group(1) != WSOL:
                    счёт["событие_не_sol"] += 1
                    continue
                тс = р_ts.search(стр)
                бм = р_block.search(стр)
                сост = состояние(стр)
                if not (тс and бм and сост):
                    continue
                ts = int(тс.group(1)) / 1000.0
                блок = int(бм.group(1))
                счёт["событий_минтов"] += 1
                pid_м = р_pool.search(стр)
                pid = pid_м.group(1) if pid_м else None
                д_ = р_action.search(стр)
                действие = д_.group(1) if д_ else None
                тм = р_tip.search(стр)
                тип = тм.group(1) if тм else None
                # тариф и подгонка f/g по паре «предыдущее состояние -> этот своп»
                if pid:
                    п = пулы.get(pid)
                    if п is None:
                        п = {"тип": тип, "тариф": число(р_fee.search(стр)),
                             "fs": [], "gs": [], "сост": сост}
                        пулы[pid] = п
                    else:
                        if п["тариф"] is None:
                            п["тариф"] = число(р_fee.search(стр))
                        if тип in XYK and (len(п["fs"]) < 40 or len(п["gs"]) < 40):
                            dy, dx = число(р_tam.search(стр)), число(р_qam.search(стр))
                            x0, y0 = п["сост"]
                            if dy and dx:
                                if действие == "buy" and y0 > dy > 0 and dx > 0:
                                    v = x0 * dy / ((y0 - dy) * dx)
                                    if 0.5 <= v <= 1.0:
                                        п["fs"].append(v)
                                elif действие == "sell" and dy > 0 and x0 > 0:
                                    v = dx * (y0 + dy) / (x0 * dy)
                                    if 0.5 <= v <= 1.0:
                                        п["gs"].append(v)
                        п["сост"] = сост
                кто = None
                т2 = р_trader.search(стр)
                с2 = р_signer.search(стр)
                кто = (т2.group(1) if т2 else None) or (с2.group(1) if с2 else None)
                sol = число(р_qam.search(стр))
                for к in по_минту[минт]:
                    если = ts - к["ts"]
                    # до поста берём минуту: «цена на момент поста» -- это последнее
                    # состояние ДО него, а не только событие в ту же секунду
                    if если < -ДО_ПОСТА_С or если > ХВОСТ_С:
                        continue
                    ключ = (к["канал"], минт)
                    з = ряды.get(ключ)
                    if з is None:
                        з = {"канал": к["канал"], "минт": минт, "utc": к["utc"],
                             "id": к["id"], "ts_поста": к["ts"], "пул": тип, "poolId": pid,
                             "до_поста": None, "вход": {}, "выход": {},
                             "вперёд": {f"{t}с": {
                                 "покупок": 0, "кошельков": set(), "sol": 0.0}
                                 for t in ВПЕРЁД_С},
                             "событий": 0, "пик": None, "первая_продажа_с": None,
                             "продаж": 0}
                        ряды[ключ] = з
                    # у минта может быть несколько пулов -- держимся того, в котором
                    # увидели его первым, иначе состояния смешаются
                    if pid and з["poolId"] and pid != з["poolId"]:
                        счёт["другой_пул_минта"] += 1
                        continue
                    з["событий"] += 1
                    if если < 0:
                        # пик считается только ПОСЛЕ поста, поэтому здесь только состояние
                        з["до_поста"] = [сост[0], сост[1], блок, round(если, 3)]
                        continue
                    # состояния: берётся ПОСЛЕДНЕЕ событие не позже порога
                    for t in ВХОДЫ_С:
                        if если <= t:
                            з["вход"][f"{t}с"] = [сост[0], сост[1], блок, round(если, 3)]
                    for t in ВЫХОДЫ_С:
                        if если <= t:
                            з["выход"][f"{t}с"] = [сост[0], сост[1], блок, round(если, 3)]
                    if действие == "buy":
                        for t in ВПЕРЁД_С:
                            if если <= t:
                                в = з["вперёд"][f"{t}с"]
                                в["покупок"] += 1
                                в["sol"] += sol or 0
                                if кто:
                                    в["кошельков"].add(кто)
                    if действие == "sell":
                        з["продаж"] += 1
                        if з["первая_продажа_с"] is None:
                            з["первая_продажа_с"] = round(если, 3)
                    ц = сост[0] / сост[1]
                    if з["пик"] is None or ц > з["пик"][0]:
                        з["пик"] = [ц, round(если, 3), блок]

    # досчёт и сериализация
    готовые = []
    for (канал, минт), з in ряды.items():
        п = пулы.get(з["poolId"]) or {}
        наценки(п)
        з["тариф"] = п.get("тариф")
        з["f"], з["g"] = п.get("f"), п.get("g")
        п0 = None
        if з.get("до_поста"):
            п0 = з["до_поста"][0] / з["до_поста"][1]
        for t in ВХОДЫ_С:
            if п0:
                break
            v = з["вход"].get(f"{t}с")
            if v:
                п0 = v[0] / v[1]
                break
        if з["пик"] and п0:
            з["пик"] = {"рост_пп": round(100 * (з["пик"][0] / п0 - 1), 2),
                        "через_с": з["пик"][1]}
        else:
            з["пик"] = None
        for t in ВПЕРЁД_С:
            в = з["вперёд"][f"{t}с"]
            в["кошельков"] = len(в["кошельков"])
            в["sol"] = round(в["sol"], 4)
        готовые.append(з)
    нашлись = {(x["канал"], x["минт"]) for x in готовые}
    нет_в_архиве = [{"канал": к["канал"], "минт": к["минт"], "utc": к["utc"]}
                    for к in кс if (к["канал"], к["минт"]) not in нашлись]

    тело = {"что": "путь цены вокруг коллов TG-каналов",
            "коллов_в_окне": len(кс), "с_ценой": len(готовые),
            "нет_в_архиве": len(нет_в_архиве), "счёт": dict(счёт),
            "окна": {"входы_с": list(ВХОДЫ_С), "выходы_с": list(ВЫХОДЫ_С),
                     "вперёд_с": list(ВПЕРЁД_С)},
            "ряды": готовые, "минты_без_цены": нет_в_архиве[:3000]}
    ПАПКА.mkdir(parents=True, exist_ok=True)
    вых = ПАПКА / f"cena_{метка}.json.gz"
    with gzip.open(вых, "wt", encoding="utf-8") as ф:
        json.dump(тело, ф, ensure_ascii=False)
    R.записано(вых)
    св = ПАПКА / "cena_svod.json"
    было = json.loads(св.read_text(encoding="utf-8")) if св.exists() else {}
    было[метка] = {k: v for k, v in тело.items() if k not in ("ряды", "минты_без_цены")}
    св.write_text(json.dumps(было, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(св)
    по_каналам = collections.Counter(x["канал"] for x in готовые)
    print(f"с ценой {len(готовые)} из {len(кс)}, без цены {len(нет_в_архиве)}; "
          f"часов {счёт['часов']}, строк {счёт['строк']}", flush=True)
    for к, n in по_каналам.most_common():
        print(f"  {к}: {n}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
