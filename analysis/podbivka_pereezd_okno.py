#!/usr/bin/env python3
"""Прицельный проход окна по заданным пулам: пункт 2 задачи и пункты и, к.

Зачем отдельный проход. Сводные файлы запусков держат по пулу только верхние десять
покупателей и состояния на нескольких горизонтах -- этого мало, чтобы сказать, КТО двигает
цену: покупал ли сам создатель, продавал ли кто-то кроме него успешно, менялась ли
ликвидность, какими партиями шёл выкуп. Поэтому по списку целей
(data/podbivka/pereezd_celi.json) читаются РОВНО те часы архива, где эти пулы родились, и
сохраняются ВСЕ события окна целиком.

Цели собираются сгущённо, из самых густых часов: 50 пулов из одного часа стоят два часа
архива вместо восьмидесяти.

Только чтение архива. Выход: data/podbivka/pereezd_okno_<метка>.json.gz.
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_arhiv_den as AD      # noqa: E402
import podbivka_zapuski as ZP        # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"


def цели(путь: Path, разделы: tuple = ()) -> dict:
    """Пулы из файла целей, какой бы ни была его вложенность.

    Файлы целей у переезда и у TG устроены по-разному (`пункт_и` / `срезы` и ещё уровень
    внутри), поэтому обход рекурсивный: берётся любой словарь, у которого есть `poolId`, а
    путь до него запоминается в «зачем». Так один прицельный проход годится и для переезда,
    и для коллов, и ничего не нужно подгонять под форму файла.
    """
    д = json.loads(путь.read_text(encoding="utf-8"))
    из_: dict = {}

    def обойти(узел, путь_: tuple) -> None:
        if isinstance(узел, dict):
            if узел.get("poolId"):
                pid = узел["poolId"]
                з = из_.setdefault(pid, {"poolId": pid, "зачем": []})
                з["зачем"].append("/".join(путь_))
                for k in ("минт", "блок", "ts", "час", "пул", "сутки", "резерв_sol",
                          "канал", "вид", "итог_sol", "ts_поста"):
                    if з.get(k) in (None, "") and узел.get(k) not in (None, ""):
                        з[k] = узел[k]
                return
            for k, v in узел.items():
                if путь_ == () and разделы and k not in разделы:
                    continue
                обойти(v, путь_ + (str(k),))
        elif isinstance(узел, list):
            for v in узел:
                обойти(v, путь_)

    обойти(д, ())
    return из_


def добрать_из_сводов(цел: dict) -> None:
    """Создатель, правило и верхние покупатели -- из уже собранных суток, без сети."""
    надо = {pid for pid, з in цел.items()
            if not з.get("блок") or not з.get("создатель")}
    if not надо:
        return
    for ф in sorted(glob.glob(str(П / "zapuski" / "dolgo_*.json.gz"))) + \
             sorted(glob.glob(str(П / "zapuski" / "zap_*.json.gz"))):
        if not надо:
            break
        with gzip.open(ф, "rt", encoding="utf-8") as о:
            тело = json.load(о)
        for x in тело.get("ряды") or []:
            pid = x.get("poolId")
            if pid not in надо:
                continue
            з = цел[pid]
            з.setdefault("создатель", x.get("создатель"))
            з.setdefault("правило", x.get("правило"))
            з.setdefault("покупатели", x.get("покупатели"))
            з.setdefault("покупателей_всего", x.get("покупателей_всего"))
            з["блок"] = з.get("блок") or x.get("блок")
            з["ts"] = з.get("ts") or x.get("ts")
            з["минт"] = з.get("минт") or x.get("минт")
            if not з.get("час") and з.get("ts"):
                з["час"] = time.strftime("%Y/%m/%d/%H", time.gmtime(з["ts"] / 1000.0))
            надо.discard(pid)


def часы_целей(цел: dict, слотов: int, сек_на_слот: float = 0.2677) -> list:
    """Час створа плюс следующий -- но следующий только там, где окно реально переползает.

    Окно 1121 слот -- это пять минут, и границу часа оно перелезает лишь у тех целей, что
    родились в последние пять минут часа. Прежде следующий час добавлялся всем, и на 95
    целях выходило 150 часов архива вместо 90: вдвое больше чтения впустую.
    """
    длина_с = слотов * сек_на_слот
    ч: set = set()
    for з in цел.values():
        if not з.get("час"):
            continue
        ч.add(з["час"])
        т = time.strptime(з["час"], "%Y/%m/%d/%H")
        н = int(time.mktime(т)) - time.timezone
        ts = з.get("ts") or ((з.get("ts_поста") or 0) * 1000)
        сек_в_часе = (ts / 1000.0 - н) if ts else 0.0
        if not ts or сек_в_часе + длина_с > 3600:
            ч.add(time.strftime("%Y/%m/%d/%H", time.gmtime(н + 3600)))
    return sorted(ч)


def проход(цел: dict, часы: list, слотов: int, счёт: collections.Counter) -> dict:
    """Читает ровно заданные часы и держит все события окна целевых пулов.

    Распаковка и повтор часа -- как в сборщике запусков: час рвётся на urllib3
    IncompleteRead, и без трёх попыток из-за одного часа терялись целые сутки.
    """
    import io          # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    import requests    # noqa: PLC0415
    try:
        import zstandard  # noqa: PLC0415
    except ModuleNotFoundError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"],
                       check=True)
        import zstandard  # noqa: PLC0415

    события: dict = collections.defaultdict(list)
    окна = {pid: (з["блок"], з["блок"] + слотов) for pid, з in цел.items() if з.get("блок")}
    ключи = set(окна)

    def читать_час(поток) -> None:
        for стр in io.TextIOWrapper(
                zstandard.ZstdDecompressor().stream_reader(поток.raw),
                encoding="utf-8", errors="ignore"):
            счёт["строк"] += 1
            пм = ZP.р_pool.search(стр)
            if not пм or пм.group(1) not in ключи:
                continue
            pid = пм.group(1)
            бм = ZP.р_block.search(стр)
            if not бм:
                continue
            б = int(бм.group(1))
            н, к = окна[pid]
            if б < н or б > к:
                continue
            события[pid].append(событие(стр, б))
            счёт["событий"] += 1

    for ч in часы:
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        for попытка in range(3):
            try:
                о = AD.открыть_час(requests, url, ч)
            except Exception as exc:          # noqa: BLE001
                счёт[f"ошибка_{type(exc).__name__}"] += 1
                break
            with о as поток:
                код = getattr(поток, "status_code", 200)
                if код != 200:
                    счёт[f"http_{код}"] += 1
                    break
                try:
                    читать_час(поток)
                except Exception as exc:      # noqa: BLE001
                    счёт[f"обрыв_{type(exc).__name__}"] += 1
                    if попытка < 2:
                        continue
            break
        else:
            счёт["обрыв_часов"] += 1
        счёт["часов"] += 1
        print(f"  {ч}: строк {счёт['строк']}, событий {счёт['событий']}", flush=True)
    return {pid: sorted(сп, key=lambda e: e["блок"]) for pid, сп in события.items()}


def событие(строка: str, блок: int) -> dict:
    дм = ZP.р_action.search(строка)
    тм = ZP.р_trader.search(строка)
    sм = ZP.р_signer.search(строка)
    qм = ZP.р_qam.search(строка)
    qmм = ZP.р_qmint.search(строка)
    сост = ZP.состояние(строка)
    е = {"блок": блок, "действие": дм.group(1) if дм else None,
         "кто": (тм.group(1) if тм else None) or (sм.group(1) if sм else None),
         "кв": ZP.число(qм), "квота": qmм.group(1) if qmм else None,
         "ток": ZP.число(ZP.р_tam.search(строка)),
         "тариф": ZP.число(ZP.р_fee.search(строка))}
    if сост:
        е["x"], е["y"] = сост
    тсм = ZP.р_ts.search(строка)
    if тсм:
        е["ts"] = int(тсм.group(1))
    пм = ZP.р_sig.search(строка)
    if пм:
        е["подпись"] = пм.group(1)
    return е


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--celi", default=str(П / "pereezd_celi.json"))
    р.add_argument("--slotov", type=int, default=1121)
    р.add_argument("--chasov-predel", type=int, default=12)
    р.add_argument("--razdely", default="",
                   help="разделы файла целей через запятую; пусто (по умолчанию) -- все. "
                        "Имена разделов у файлов разные (пункт_2/пункт_к у переезда, срезы "
                        "у TG), и неподходящий фильтр молча отбрасывал ВСЕ цели -- так один "
                        "проход на 87 часов прошёл впустую. Поэтому по умолчанию -- все.")
    р.add_argument("--metka", default="okno")
    а = р.parse_args()
    цел = цели(Path(а.celi),
               tuple(x.strip() for x in а.razdely.split(",") if x.strip()))
    добрать_из_сводов(цел)
    часы = часы_целей(цел, а.slotov)
    if len(часы) > а.chasov_predel:
        # Предел -- чтобы прицельный проход не превратился в полный: лишние часы и пулы
        # этих часов отбрасываются, и сколько отброшено -- сказано в счёте.
        оставить = set(часы[:а.chasov_predel])
        цел = {pid: з for pid, з in цел.items() if з.get("час") in оставить}
        часы = часы_целей(цел, а.slotov)
    if not цел or not часы:
        # Пустой проход -- это ошибка, а не результат: лучше упасть сразу, чем положить
        # пустой файл и выдать его за ответ.
        print(f"ОШИБКА: целей {len(цел)}, часов {len(часы)} -- нечего читать. "
              f"Проверь --celi и --razdely (разделы файла: "
              f"{sorted(json.loads(Path(а.celi).read_text(encoding='utf-8')))}).",
              flush=True)
        return 2
    print(f"целей {len(цел)}, часов {len(часы)}: {часы}", flush=True)
    счёт: collections.Counter = collections.Counter()
    соб = проход(цел, часы, а.slotov, счёт)
    из_ = {"что": "прицельный проход окна по целям переезда", "слотов": а.slotov,
           "часы": часы, "счёт": dict(счёт),
           "пулы": [{**з, "события": соб.get(pid) or []} for pid, з in sorted(цел.items())]}
    путь = П / f"pereezd_okno_{а.metka}.json.gz"
    with gzip.open(путь, "wt", encoding="utf-8") as о:
        json.dump(из_, о, ensure_ascii=False)
    print(f"готово: {путь} (пулов {len(цел)}, событий {счёт['событий']})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
