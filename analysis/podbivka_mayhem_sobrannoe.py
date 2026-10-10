#!/usr/bin/env python3
"""Mayhem на УЖЕ ПРОЧИТАННЫХ сутках: кто приходит в створ и как ведёт себя цена.

Зачем так. Полный разбор «как торгует агент площадки» требует всех свопов на
Mayhem-кривых -- это отдельный проход архива, а архив сейчас занят чтением 61 суток.
Поэтому здесь берётся то, что уже лежит в собранных файлах запусков (data/podbivka/zapuski
zap_*): по каждому запуску есть порода (по дрейфу k), адреса покупателей в блоке створа (до
восьми), волна покупок на 5/15/30/60 секундах, путь цены по горизонтам и первая продажа.

Что меряется:
  * доля Mayhem среди запусков кривой и как она держится по суткам;
  * кошельки створа: на сколько РАЗНЫХ Mayhem-запусков пришёл кошелёк против того же числа
    на нормальных запусках. Агент площадки должен быть виден как тот, кто ходит почти во
    все Mayhem и почти не ходит в нормальные -- так же, как в своё время нашёлся агент
    BOOST;
  * волна и путь цены у Mayhem против нормальных: есть ли окно, где цена после створа
    ведёт себя предсказуемо.

Чего здесь НЕТ: сторон сделок агента (покупка или продажа), его размеров и сдвигов слотов
по каждой сделке -- в собранных файлах этого нет, нужен проход архива
(analysis/podbivka_mayhem.py уже написан под это).

Только чтение собранных файлов. Выход: data/podbivka/mayhem_sobrannoe.json.
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
K0 = 32.19
ДРЕЙФ_ПРЕДЕЛ = 1e-4
ГОРИЗОНТЫ = ("20", "40", "60", "90", "120", "160", "230")
СОБЫТИЙ_ТИХИЙ = 3


def порода(з: dict) -> str:
    вх = (з.get("вход") or {}).get("створ_дно")
    if not вх:
        return "без_створа"
    k0 = float(вх[0]) * float(вх[1]) / 1e9
    if k0 <= 0:
        return "без_створа"
    д = 0.0
    for h in ГОРИЗОНТЫ:
        в = (з.get("выход") or {}).get(h)
        if в and float(в[0]) > 0 and float(в[1]) > 0:
            д = max(д, abs(float(в[0]) * float(в[1]) / 1e9 / k0 - 1))
    if д > ДРЕЙФ_ПРЕДЕЛ:
        return "mayhem"
    if (з.get("событий") or 0) <= СОБЫТИЙ_ТИХИЙ:
        return "неразличимо"
    return "нормальная"


def путь(з: dict) -> dict:
    вх = (з.get("вход") or {}).get("створ_дно")
    если = {}
    if not вх:
        return если
    ц0 = float(вх[0]) / float(вх[1])
    for h in ГОРИЗОНТЫ:
        в = (з.get("выход") or {}).get(h)
        if в and float(в[1]) > 0:
            если[h] = (float(в[0]) / float(в[1])) / ц0 - 1
    return если


def кванты(v: list, зн: int = 4) -> dict:
    if not v:
        return {"n": 0}
    v = sorted(v)

    def q(p):
        return round(v[min(len(v) - 1, int(p * len(v)))], зн)
    return {"n": len(v), "p10": q(0.10), "медиана": round(statistics.median(v), зн),
            "p90": q(0.90), "среднее": round(statistics.fmean(v), зн)}


def главное(а) -> int:  # noqa: PLR0912, PLR0915
    файлы = sorted(glob.glob(str(П / "zapuski" / "zap_*.json.gz")))
    if not файлы:
        print("нет файлов zap_* -- считать нечего", flush=True)
        return 2
    по_суткам: dict = collections.defaultdict(collections.Counter)
    створ: dict = {"mayhem": collections.Counter(), "нормальная": collections.Counter()}
    минтов: dict = {"mayhem": 0, "нормальная": 0}
    волна: dict = {"mayhem": collections.defaultdict(list),
                   "нормальная": collections.defaultdict(list)}
    путь_св: dict = {"mayhem": collections.defaultdict(list),
                     "нормальная": collections.defaultdict(list)}
    продажа: dict = {"mayhem": [], "нормальная": []}
    for ф in файлы:
        д = json.loads(gzip.open(ф, "rt", encoding="utf-8").read())
        сут = Path(ф).stem.split("_", 1)[-1].split("T")[0]
        for з in д.get("ряды") or []:
            if з.get("правило") != "создание:create" or з.get("пул") != "pump":
                continue
            if з.get("quoteMint") != WSOL:
                continue
            г = порода(з)
            по_суткам[сут][г] += 1
            if г not in ("mayhem", "нормальная"):
                continue
            минтов[г] += 1
            for к in ((з.get("в_блоке_создания") or {}).get("кто") or []):
                створ[г][к] += 1
            в = з.get("волна") or {}
            for окно, зн in в.items():
                if isinstance(зн, dict) and зн.get("покупок") is not None:
                    волна[г][окно].append(зн["покупок"])
            for h, р in путь(з).items():
                путь_св[г][h].append(р)
            пп = з.get("первая_продажа_слотов")
            if пп is not None:
                продажа[г].append(пп)
    # кошельки: доля запусков породы, куда кошелёк пришёл в створ
    карточки = []
    for к, n in створ["mayhem"].most_common(а.verh):
        нн = створ["нормальная"].get(к, 0)
        карточки.append({
            "адрес": к, "mayhem_запусков": n,
            "доля_mayhem": round(n / max(1, минтов["mayhem"]), 5),
            "нормальных_запусков": нн,
            "доля_нормальных": round(нн / max(1, минтов["нормальная"]), 5),
            "перевес_к_mayhem": round((n / max(1, минтов["mayhem"]))
                                      / max(1e-9, нн / max(1, минтов["нормальная"])), 2)
            if нн else None})
    тело = {"что": "Mayhem по собранным суткам: створ, волна, путь цены",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "файлов": len(файлы),
            "порода_признак": f"дрейф k больше {ДРЕЙФ_ПРЕДЕЛ} по горизонтам",
            "запусков_по_породе": {г: минтов[г] for г in минтов},
            "доля_mayhem": round(минтов["mayhem"]
                                 / max(1, минтов["mayhem"] + минтов["нормальная"]), 4),
            "по_суткам": {с: dict(v) for с, v in sorted(по_суткам.items())},
            "кошельков_в_створе": {г: len(створ[г]) for г in створ},
            "верхние_кошельки_створа": карточки,
            "волна_покупок": {г: {о: кванты(v, 1) for о, v in sorted(волна[г].items())}
                              for г in волна},
            "путь_цены": {г: {h: кванты([100 * x for x in v], 2)
                              for h, v in sorted(путь_св[г].items(), key=lambda kv: int(kv[0]))}
                          for г in путь_св},
            "первая_продажа_слотов": {г: кванты(продажа[г], 1) for г in продажа},
            "чего_нет": "сторон сделок агента, размеров и сдвигов слотов по каждой сделке "
                        "в собранных файлах нет -- нужен проход архива "
                        "(analysis/podbivka_mayhem.py)"}
    ф = П / f"mayhem_sobrannoe{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    print(json.dumps({k: v for k, v in тело.items()
                      if k not in ("верхние_кошельки_створа", "по_суткам")},
                     ensure_ascii=False, indent=1), flush=True)
    print(f"\n{'адрес':46}{'mayhem':>8}{'доля':>9}{'норм':>7}{'доля':>9}{'перевес':>9}",
          flush=True)
    for x in карточки[:15]:
        print(f"{x['адрес']:46}{x['mayhem_запусков']:>8}{x['доля_mayhem']:>9}"
              f"{x['нормальных_запусков']:>7}{x['доля_нормальных']:>9}"
              f"{str(x['перевес_к_mayhem']):>9}", flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--verh", type=int, default=60)
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
