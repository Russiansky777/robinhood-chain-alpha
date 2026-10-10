#!/usr/bin/env python3
"""Грубое сито вселенной: кто вне реестра покупает достаточно часто, чтобы его проверять.

Зачем. Стандарт выборки требует на окне проверки не меньше ста сделок на источник, а
поправка на перебор -- порога p < 0.05/N, где N -- число проверенных кошельков. В одних
сутках сито `--vselennaya 20` отбирает около 1 750 кошельков; на 78 сутках пул выходит
такой, что порог уходит ниже 1e-5, и пройти его сможет лишь кошелёк с почти всеми сутками
в плюсе. Поэтому прицельный проход по стандарту надо ставить не на всех, а на тех, у кого
хватит и ЧАСТОТЫ, и ПОСТОЯНСТВА: сделок в сутки достаточно для n >= 100 на окне из четырёх
суток, и сутки не одиночные.

Что делает. Складывает поля `вселенная` из всех суточных файлов (их пишет
podbivka_arhiv_den.py с --vselennaya N) по кошельку: сумму первых покупок, число суток, в
которых он появлялся, медиану покупок в сутки. Выбрасывает адреса реестра -- они и так
проверяются. Дальше режет по двум порогам: покупок в сутки не меньше --v-sutki (по
умолчанию 25, чтобы на окне из четырёх суток набралось сто) и суток не меньше --sutok.

В выходе печатается, каким станет порог поправки на перебор при получившемся N вместе с
адресами реестра -- чтобы решение «сколько кошельков брать» принималось числом, а не на
глаз.

Только чтение собранных файлов. Выход: data/podbivka/vselennaya_sito.json.
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

import podbivka_paket_obshee as O    # noqa: E402

КОРЕНЬ = O.КОРЕНЬ
П = O.П


def реестр_адреса() -> set:
    try:
        return set(O.реестр())
    except Exception:  # noqa: BLE001
        return set()


def главное(а) -> int:
    файлы = sorted(glob.glob(str(П / "arhiv_den" / f"{а.prefiks}*.json.gz")))
    if not файлы:
        print(f"нет файлов {а.prefiks}* -- сито считать нечего", flush=True)
        return 2
    по_кош: dict = collections.defaultdict(list)   # адрес -> [покупок в сутки]
    сутки: list = []
    без_сита = 0
    for ф in файлы:
        try:
            д = json.loads(gzip.open(ф, "rt", encoding="utf-8").read())
        except (OSError, ValueError):
            continue
        вс = д.get("вселенная")
        if not вс:
            без_сита += 1
            continue
        сутки.append(д.get("день") or Path(ф).stem)
        for а_, n in вс.items():
            по_кош[а_].append(n)
    print(f"файлов {len(файлы)}, с ситом {len(сутки)}, без сита {без_сита}, "
          f"кошельков всего {len(по_кош)}", flush=True)
    if not сутки:
        print("ни в одном файле нет поля «вселенная» -- нужен прогон с --vselennaya",
              flush=True)
        return 2

    рее = реестр_адреса()
    карточки = []
    for а_, сп in по_кош.items():
        карточки.append({"адрес": а_, "в_реестре": а_ in рее,
                         "покупок_всего": sum(сп), "суток": len(сп),
                         "покупок_в_сутки_медиана": round(statistics.median(сп), 1),
                         "покупок_в_сутки_макс": max(сп)})
    вне = [x for x in карточки if not x["в_реестре"]]
    прошли = [x for x in вне
              if x["покупок_в_сутки_медиана"] >= а.v_sutki and x["суток"] >= а.sutok]
    прошли.sort(key=lambda x: -x["покупок_всего"])
    N = len(прошли) + len(рее)
    тело = {"что": "грубое сито вселенной: кого стоит проверять стандартом",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "файлов": len(файлы), "суток_с_ситом": len(сутки),
            "сутки": сутки,
            "пороги": {"покупок_в_сутки_медиана_мин": а.v_sutki,
                       "суток_мин": а.sutok,
                       "зачем": "на окне проверки из четырёх суток нужно не меньше ста "
                                "сделок, значит в сутки не меньше двадцати пяти"},
            "кошельков_в_сите": len(по_кош),
            "из_них_в_реестре": sum(1 for x in карточки if x["в_реестре"]),
            "вне_реестра": len(вне), "прошли_грубое_сито": len(прошли),
            "N_для_поправки": N,
            "порог_p_при_этом_N": (0.05 / N) if N else None,
            "сколько_суток_в_плюсе_нужно":
                "при N = %d порог 0.05/N = %.2e; знаковый критерий даёт такой p только "
                "при почти всех сутках в плюсе -- смотреть таблицу в самом пересчёте"
                % (N, 0.05 / N) if N else None,
            "кандидаты": прошли[:а.verh]}
    ф = П / f"vselennaya_sito{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    цели = П / f"vselennaya_celi{а.metka}.json"
    цели.write_text(json.dumps(
        {"что": "адреса вне реестра на прицельный проход по стандарту",
         "пороги": тело["пороги"], "суток_с_ситом": len(сутки),
         "адреса": [x["адрес"] for x in прошли[:а.verh]]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
        R.записано(цели)
    except Exception:  # noqa: BLE001, S110
        pass
    print(json.dumps({k: v for k, v in тело.items()
                      if k not in ("кандидаты", "сутки")},
                     ensure_ascii=False, indent=1), flush=True)
    print(f"\n{'адрес':46}{'покупок':>9}{'суток':>7}{'в сутки мед':>13}{'макс':>7}",
          flush=True)
    for x in прошли[:20]:
        print(f"{x['адрес']:46}{x['покупок_всего']:>9}{x['суток']:>7}"
              f"{x['покупок_в_сутки_медиана']:>13}{x['покупок_в_сутки_макс']:>7}",
              flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--prefiks", default="paket_",
                   help="префикс суточных файлов, в которых лежит поле «вселенная»")
    р.add_argument("--v-sutki", type=float, default=25.0,
                   help="минимум первых покупок в сутки (медиана по суткам)")
    р.add_argument("--sutok", type=int, default=10, help="минимум суток присутствия")
    р.add_argument("--verh", type=int, default=400, help="сколько кандидатов печатать")
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
