#!/usr/bin/env python3
"""Насколько глубоко назад лежит архив PumpApi: сколько суток, место, время.

Зачем. Стандарт выборки владельца с 10.10 требует нормальной выборки, а у меня всего 18
суток (21.09 .. 08.10) и на скользящую проверку «подбор 7 -> проверка 4, сдвиг 4» это даёт
всего два окна. Прежде чем заказывать подъём 30--60 суток, надо ЗНАТЬ, есть ли они в архиве
и чего стоит их прочитать. Проход спрашивает у replay.pumpapi.io по одному часу на каждую
проверяемую дату (запросом с Range на первые байты, чтобы не качать час целиком), и по
ответу берёт код, Content-Length и Last-Modified. Дальше считает: сколько суток доступно,
сколько гигабайт в сутках и сколько времени займёт чтение при измеренной скорости.

Скорость измеряется честно: один час читается ЦЕЛИКОМ с замером секунд и байт.

Только чтение. Выход: data/podbivka/arhiv_glubina.json.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"


def даты(с: str, назад: list) -> list:
    т = time.strptime(с, "%Y-%m-%d")
    н = int(time.mktime(т)) - time.timezone
    return [time.strftime("%Y/%m/%d", time.gmtime(н - 86400 * d)) for d in назад]


def главное(а) -> int:
    import requests  # noqa: PLC0415

    назад = [int(x) for x in а.nazad.split(",") if x.strip()]
    ряды = []
    for д in даты(а.ot, назад):
        url = f"https://replay.pumpapi.io/{д}/{а.chas}.jsonl.zst"
        зап = {"дата": д, "url_хвост": f"{д}/{а.chas}"}
        try:
            о = requests.get(url, headers={"Range": "bytes=0-1023"}, timeout=60,
                             stream=True)
            зап["код"] = о.status_code
            зап["длина_заявлена"] = о.headers.get("Content-Range") or \
                о.headers.get("Content-Length")
            зап["изменён"] = о.headers.get("Last-Modified")
            о.close()
        except Exception as exc:  # noqa: BLE001
            зап["ошибка"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        ряды.append(зап)
        print(f"  {д}/{а.chas}: {зап.get('код') or зап.get('ошибка')} "
              f"{зап.get('длина_заявлена')}", flush=True)

    # честный замер скорости: один час целиком
    замер = {}
    if а.zamer:
        url = f"https://replay.pumpapi.io/{а.zamer}/{а.chas}.jsonl.zst"
        т0 = time.time()
        байт = 0
        try:
            with requests.get(url, stream=True, timeout=600) as о:
                замер["код"] = о.status_code
                if о.status_code == 200:
                    for кусок in о.iter_content(1 << 20):
                        байт += len(кусок)
        except Exception as exc:  # noqa: BLE001
            замер["ошибка"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        сек = time.time() - т0
        замер.update({"час": f"{а.zamer}/{а.chas}", "байт": байт,
                      "секунд": round(сек, 1),
                      "мбайт_в_секунду": round(байт / 1e6 / max(0.001, сек), 2)})
        print(f"  замер {замер}", flush=True)

    есть = [x for x in ряды if x.get("код") in (200, 206)]
    тело = {"что": "глубина архива PumpApi: какие сутки есть, сколько места и времени",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "от": а.ot, "час_пробы": а.chas, "назад_суток": назад,
            "ряды": ряды, "доступно_дат": len(есть),
            "самая_старая_доступная": (min(x["дата"] for x in есть) if есть else None),
            "замер_скорости": замер}
    if замер.get("байт") and замер.get("мбайт_в_секунду"):
        гб_час = замер["байт"] / 1e9
        тело["оценка"] = {
            "гб_на_час": round(гб_час, 3),
            "гб_на_сутки": round(гб_час * 24, 1),
            "минут_на_сутки_чтения": round(24 * замер["секунд"] / 60, 1),
            "гб_на_30_суток": round(гб_час * 24 * 30, 1),
            "часов_на_30_суток": round(24 * 30 * замер["секунд"] / 3600, 1),
            "гб_на_60_суток": round(гб_час * 24 * 60, 1),
            "часов_на_60_суток": round(24 * 60 * замер["секунд"] / 3600, 1),
            "примечание": "гигабайты -- это ПРОКАЧАТЬ, а не хранить: проход потоковый, "
                          "на диске остаются только суточные файлы сигналов (у 18 "
                          "собранных суток это 4.0 ГБ на 203 файла)"}
    ф = П / f"arhiv_glubina{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import sys  # noqa: PLC0415
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    print(json.dumps({k: v for k, v in тело.items() if k != "ряды"},
                     ensure_ascii=False, indent=1), flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--ot", default="2026-09-21", help="от какой даты считать назад")
    р.add_argument("--nazad", default="1,3,7,14,21,30,45,60,90,120,180",
                   help="на сколько суток назад пробовать, через запятую")
    р.add_argument("--chas", default="06")
    р.add_argument("--zamer", default="",
                   help="дата вида 2026/09/20 -- прочитать её час целиком и замерить "
                        "скорость и объём")
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
