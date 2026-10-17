#!/usr/bin/env python3
"""Выборка токенов для проверки «первое упоминание в X против пути цены».

Берётся по суточным производным запусков, какие уже лежат в ветке
(data/podbivka/zapuski/zap_<сутки>.json.gz -- все запуски, окно 230 слотов от создания;
dolgo_<сутки>.json.gz -- те, что дошли до пула, окно 1121 слот от переезда). Сырые часы
архива для этого не нужны.

Две группы, как заказано:
  * «дошли до пула» -- строки dolgo (создание:createPool / создание:migrate);
  * «не дошли» -- строки zap с правилом создание:create, которых нет в первой группе и у
    которых оборот первой минуты (волна 60с, SOL) >= порога; случайная выборка того же
    размера, зерно задано.

Что кладётся на токен: минт, сутки, тип пула, метка создания (ts, мс), слот и рост пика
ВНУТРИ ОКНА производной, первая продажа (слотов), волна 5/15/30/60 с, оборот, создатель.
Окна у групп разные (230 против 1121 слота) -- это написано в выходе, чтобы сравнение
«пост раньше пика» читалось с этой оговоркой, а не как равное.

Только чтение. Выход: data/podbivka/x_vyborka<метка>.json и, по флагу, срез на пилот.
"""
from __future__ import annotations

import argparse
import gzip
import json
import collections
import random
import statistics
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ЗАП = П / "zapuski"
ПОЛЯ = ("минт", "пул", "poolId", "блок", "ts", "создатель", "правило", "quoteMint",
        "событий", "продаж", "первая_продажа_слотов", "пик", "волна")


def оборот_sol(р: dict) -> float:
    в = (р.get("волна") or {}).get("60с") or {}
    return float(в.get("sol") or 0.0)


def ряд(р: dict, сутки: str, группа: str, окно: int) -> dict:
    из_ = {к: р.get(к) for к in ПОЛЯ}
    из_.update({"сутки": сутки, "группа": группа, "окно_слотов": окно,
                "оборот_60с_sol": оборот_sol(р),
                "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime((р.get("ts") or 0) / 1000))})
    return из_


def главное(а) -> int:
    дни = [x for x in а.dni.split(",") if x.strip()]
    рнд = random.Random(а.semya)
    дошли: dict = {}
    не_дошли: dict = {}
    счёт = {"dolgo_строк": 0, "zap_строк": 0, "кривых_создано": 0,
            "кривых_с_оборотом": 0}
    for д in дни:
        ф_д = ЗАП / f"dolgo_{д}.json.gz"
        ф_z = ЗАП / f"zap_{д}.json.gz"
        if not (ф_д.exists() and ф_z.exists()):
            raise SystemExit(f"нет производных за {д}: {ф_д.name} / {ф_z.name}")
        дол = json.load(gzip.open(ф_д, "rt", encoding="utf-8"))
        счёт["dolgo_строк"] += len(дол["ряды"])
        окно_д = дол.get("слотов")
        for р in дол["ряды"]:
            м = р.get("минт")
            if м and м not in дошли:
                дошли[м] = ряд(р, д, "дошли_до_пула", окно_д)
        зап = json.load(gzip.open(ф_z, "rt", encoding="utf-8"))
        счёт["zap_строк"] += len(зап["ряды"])
        окно_z = зап.get("слотов")
        for р in зап["ряды"]:
            if р.get("правило") != "создание:create":
                continue
            счёт["кривых_создано"] += 1
            м = р.get("минт")
            if not м or м in дошли or м in не_дошли:
                continue
            if оборот_sol(р) < а.porog_oborota:
                continue
            счёт["кривых_с_оборотом"] += 1
            не_дошли[м] = ряд(р, д, "не_doshli", окно_z)

    if а.verh_po_oborotu:
        # ВЕРХНИЕ ПО ОБОРОТУ, без отбора по исходу: отбор идёт по величине, замеренной в
        # МОМЕНТ запуска (SOL, пришедшие в первую минуту жизни пула), а не по тому, чем
        # дело кончилось. Это единственная оборотная величина, какая есть в суточных
        # производных без нового прохода по архиву, и она измерена до исхода -- значит
        # отбор не подглядывает вперёд. Группы не разделяются: берётся верх по суткам
        # среди ВСЕХ запусков этих суток.
        все = list(дошли.values()) + list(не_дошли.values())
        по_суткам: dict = collections.defaultdict(list)
        for р in все:
            по_суткам[р["сутки"]].append(р)
        верх = []
        на_сутки = max(1, а.verh_po_oborotu // max(1, len(дни)))
        for д in дни:
            сп = sorted(по_суткам.get(д, []), key=lambda x: -(x["оборот_60с_sol"] or 0))
            верх += сп[:на_сутки]
        тело = {
            "что": "выборка токенов для X: верхние по обороту первой минуты, по суткам",
            "как_отобрано": "верх по SOL первой минуты (волна 60 с) среди ВСЕХ запусков "
                            "суток; отбор по величине в момент запуска, не по исходу",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "дни": дни, "на_сутки": на_сутки, "зерно": а.semya,
            "порог_оборота_sol": а.porog_oborota,
            "всего_запусков_рассмотрено": len(все),
            "в_выборке": len(верх),
            "состав_по_группам": dict(collections.Counter(р["группа"] for р in верх)),
            "оборот_в_выборке_sol": {
                "минимум": round(min((р["оборот_60с_sol"] or 0) for р in верх), 3),
                "медиана": round(statistics.median(
                    [(р["оборот_60с_sol"] or 0) for р in верх]), 3),
                "максимум": round(max((р["оборот_60с_sol"] or 0) for р in верх), 3)},
            "счёт": счёт, "ряды": верх,
        }
        ф = П / f"x_vyborka_top{а.verh_po_oborotu}{а.metka}.json"
        ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"верхние по обороту: {len(верх)} токенов ({на_сутки} на сутки), "
              f"оборот первой минуты от {тело['оборот_в_выборке_sol']['минимум']} до "
              f"{тело['оборот_в_выборке_sol']['максимум']} SOL, медиана "
              f"{тело['оборот_в_выборке_sol']['медиана']}", flush=True)
        print(f"состав: {тело['состав_по_группам']}", flush=True)
        print(f"записано {ф}", flush=True)
        try:
            import sys  # noqa: PLC0415
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import podbivka_run as R  # noqa: PLC0415
            R.записано(ф)
        except Exception:  # noqa: BLE001, S110
            pass
        return 0

    а_сп = sorted(дошли.values(), key=lambda x: x["ts"] or 0)
    б_сп = sorted(не_дошли.values(), key=lambda x: x["ts"] or 0)
    # Размер групп равный: сколько есть в меньшей, но не больше --predel
    размер = min(len(а_сп), len(б_сп), а.predel)
    выб_а = рнд.sample(а_сп, размер)
    выб_б = рнд.sample(б_сп, размер)
    тело = {
        "что": "выборка токенов для проверки первого упоминания в X",
        "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "дни": дни, "зерно": а.semya, "порог_оборота_sol": а.porog_oborota,
        "окна": {"дошли_до_пула": (а_сп[0]["окно_слотов"] if а_сп else None),
                 "не_doshli": (б_сп[0]["окно_слотов"] if б_сп else None),
                 "оговорка": "пик считается ВНУТРИ окна производной; окна у групп разные, "
                             "настоящий пик может быть позже -- значит доля «пост раньше "
                             "пика» здесь занижена, а не завышена"},
        "всего_найдено": {"дошли_до_пула": len(а_сп), "не_doshli": len(б_сп)},
        "в_выборке": {"дошли_до_пула": размер, "не_doshli": размер},
        "счёт": счёт,
        "ряды": выб_а + выб_б,
    }
    ф = П / f"x_vyborka{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    пути = [ф]
    if а.pilot:
        н = а.pilot // 2
        пилот = dict(тело)
        пилот["что"] += f" -- пилот {а.pilot}"
        пилот["ряды"] = рнд.sample(выб_а, н) + рнд.sample(выб_б, а.pilot - н)
        пилот["в_выборке"] = {"дошли_до_пула": н, "не_doshli": а.pilot - н}
        фп = П / f"x_vyborka_pilot{а.pilot}{а.metka}.json"
        фп.write_text(json.dumps(пилот, ensure_ascii=False, indent=1), encoding="utf-8")
        пути.append(фп)
    print(f"дни {дни}: дошли до пула {len(а_сп)}, не дошли с оборотом >= "
          f"{а.porog_oborota} SOL {len(б_сп)}; в выборке по {размер}", flush=True)
    print(f"счёт: {счёт}", flush=True)
    for p in пути:
        print(f"записано {p}", flush=True)
    try:
        import sys  # noqa: PLC0415
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import podbivka_run as R  # noqa: PLC0415
        for p in пути:
            R.записано(p)
    except Exception:  # noqa: BLE001, S110
        pass
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--dni", default="2026-09-21T06,2026-09-22T06,2026-09-23T06")
    р.add_argument("--porog-oborota", type=float, default=1.0, help="SOL за первую минуту")
    р.add_argument("--predel", type=int, default=150, help="не больше N на группу")
    р.add_argument("--pilot", type=int, default=50, help="размер пилотного среза (0 -- не делать)")
    р.add_argument("--verh-po-oborotu", type=int, default=0,
                   help="N верхних по обороту первой минуты, поровну на сутки")
    р.add_argument("--semya", type=int, default=20261017)
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
