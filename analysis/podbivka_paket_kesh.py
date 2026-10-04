#!/usr/bin/env python3
"""Кэш сигналов архива для пакета владельца 04.10: один тяжёлый проход -- много офлайн-пунктов.

Зачем. Суточные файлы прохода (data/podbivka/arhiv_den/*.json.gz) -- 3.6 ГБ; каждый пункт
пакета заново их читать не должен. Здесь один проход складывает ПЛОСКИЕ ряды сигналов с
полями, которых хватает на размер билета, горизонты выхода, отсечки входа и видимость:
состояния пула на входах и выходах (по ним любой билет и любой горизонт считаются офлайн
формулой `podbivka_bilet.пп_по_состояниям`), удержание, рисунок цены, «перед нами», резерв.

Дедупликация по подписи: окна суточных файлов перекрываются (cand2_*, zakem2_*, paket_* и
den_* за одни и те же сутки). Приоритет файла -- по порядку ШАБЛОНЫ: кто раньше в списке,
того и оставляем (paket_* считаны расширенной моделью: выходы 30/60/300 и вход S2).

Выход: $PODB_PAKET_KESH/signaly.jsonl.gz (по строке на сигнал) и
data/podbivka/paket_kesh_svod.json (счёт: файлов, сигналов, дублей, суток, источников).
Только чтение.
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import os
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_cand2 as C2  # noqa: E402

КОРЕНЬ = C2.КОРЕНЬ
П = C2.П
# Кэш большой (сотни МБ) и восстанавливается одной командой, поэтому в репозиторий он
# не кладётся: путь задаётся PODB_PAKET_KESH (черновик сессии), сводка -- в data/podbivka.
ПАПКА = Path(os.environ.get("PODB_PAKET_KESH") or (П / "paket"))
СВОДКА = П / "paket_kesh_svod.json"
# порядок -- приоритет при дедупликации подписи
ШАБЛОНЫ = ("paket_*.json.gz", "konv_*.json.gz", "den_*.json.gz", "cand2_*.json.gz",
           "zakem2_*.json.gz", "zakem_*.json.gz", "vne*.json.gz", "pyg10_*.json.gz")


def кратко(x, знаков: int = 9):
    return round(float(x), знаков) if isinstance(x, (int, float)) else x


def ряд(с: dict, файл: str) -> dict:
    м = с.get("модель") or {}
    сост = м.get("состояния") or {}
    уд = с.get("удержание") or {}
    рис = с.get("рисунок") or {}
    перед = с.get("перед") or []
    return {
        "подпись": с.get("signature"), "ист": с.get("trader"), "пул": с.get("pool"),
        "poolId": с.get("poolId"), "минт": с.get("mint"), "блок": с.get("block"),
        "ts": с.get("timestamp"), "sol": кратко(с.get("sol"), 6),
        "quoteMint": с.get("quoteMint"), "курс_q": кратко(с.get("курс_q"), 12),
        "первая": с.get("первая"), "по_окну": с.get("по_окну"),
        "подписант": с.get("подписант"),
        "резерв_sol": кратко(с.get("резерв_sol"), 6),
        "вирт_резерв_sol": кратко(с.get("вирт_резерв_sol"), 6),
        "событий_пула_в_окне": с.get("событий_пула_в_окне"),
        "свопов": с.get("свопов"),
        "рост_до_150": кратко(с.get("рост_до_150"), 4),
        "рост_до_750": кратко(с.get("рост_до_750"), 4),
        # для правила билета и горизонтов -- вся модель, кроме готовых п.п.
        "f": кратко(м.get("f"), 9), "g": кратко(м.get("g"), 9), "fee": кратко(м.get("fee"), 9),
        "f_n": м.get("f_n"), "g_n": м.get("g_n"),
        "модель_pump_amm": м.get("модель_pump_amm"), "why_not": м.get("why_not"),
        "вход": сост.get("вход") or {}, "выход": сост.get("выход") or {},
        "масштаб": сост.get("масштаб"),
        # удержание и рисунок: выход «вслед за источником», хвост от пика, скам
        "первая_продажа_слотов": уд.get("первая_продажа_слотов"),
        "первая_продажа_05_слотов": уд.get("первая_продажа_05_слотов"),
        "пик_слотов": уд.get("пик_слотов"), "пик_пп": кратко(уд.get("пик_пп"), 3),
        "секунд_до": уд.get("секунд_до"),
        "рис_вверх_пп": кратко(рис.get("вверх_пп"), 3),
        "рис_от_пика_пп": кратко(рис.get("от_пика_пп"), 3),
        "рис_пик_слот": рис.get("пик_слот"), "рис_событий_до_30": рис.get("событий_до_30"),
        "перед_n": len(перед),
        "перед": [{"кто": p.get("кто"), "слотов": p.get("слотов"), "sol": кратко(p.get("sol"), 6)}
                  for p in перед[:10]],
        "файл": Path(файл).name,
    }


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--shablony", default=",".join(ШАБЛОНЫ))
    р.add_argument("--s", default="", help="только сутки не раньше этой даты (YYYY-MM-DD)")
    а = р.parse_args()
    ПАПКА.mkdir(parents=True, exist_ok=True)

    файлы: list = []
    for ш in а.shablony.split(","):
        файлы += sorted(glob.glob(str(П / "arhiv_den" / ш.strip())))
    видели: set = set()
    счёт = {"файлов": 0, "сигналов": 0, "записано": 0, "дублей": 0, "рано": 0}
    по_файлам: dict = collections.Counter()
    сутки: collections.Counter = collections.Counter()
    источники: collections.Counter = collections.Counter()
    out = ПАПКА / "signaly.jsonl.gz"
    with gzip.open(out, "wt", encoding="utf-8") as ф:
        for f in файлы:
            try:
                д = json.loads(gzip.open(f).read())
            except Exception as exc:  # noqa: BLE001
                счёт.setdefault("ошибки", []).append(f"{Path(f).name}: {type(exc).__name__}")
                continue
            счёт["файлов"] += 1
            for с in д.get("сигналы") or []:
                счёт["сигналов"] += 1
                п = с.get("signature")
                if п in видели:
                    счёт["дублей"] += 1
                    continue
                д_ = time.strftime("%Y-%m-%d", time.gmtime((с.get("timestamp") or 0) / 1000))
                if а.s and д_ < а.s:
                    счёт["рано"] += 1
                    continue
                видели.add(п)
                ф.write(json.dumps(ряд(с, f), ensure_ascii=False) + "\n")
                счёт["записано"] += 1
                по_файлам[Path(f).name] += 1
                сутки[д_] += 1
                источники[с.get("trader")] += 1
    свод = {"что": "кэш сигналов архива для пакета 04.10", "файл": out.name, "счёт": счёт,
            "суток": len(сутки), "по_суткам": dict(sorted(сутки.items())),
            "источников": len(источники), "по_файлам": dict(по_файлам.most_common()),
            "верх_источников": источники.most_common(20)}
    СВОДКА.write_text(json.dumps(свод, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    R.записано(out)
    R.записано(СВОДКА)
    print(f"кэш: файлов {счёт['файлов']}, сигналов {счёт['сигналов']}, записано "
          f"{счёт['записано']}, дублей {счёт['дублей']}, суток {len(сутки)}, "
          f"источников {len(источники)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
