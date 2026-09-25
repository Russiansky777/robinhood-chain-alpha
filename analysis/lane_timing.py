#!/usr/bin/env python3
"""I.7 -- ВРЕМЯ НАШЕЙ ЛИНИИ В РЕЖИМЕ NONCE. Только чтение журнала позиций.

Вопрос владельца: сколько проходит от решения до отправки первого варианта
(сборка шести + подпись), медиана и p90; и если подпись одного варианта
стоит около 18 мс -- почему, ведь ed25519 подписывает за доли миллисекунды.

Что в журнале ЕСТЬ на каждую покупку полосы:
  * signal_recv_ts   -- когда мы получили сигнал источника (это и есть "решение":
                        между ним и сборкой нет ничего, кроме разбора и гейтов);
  * lane_build_ms    -- сборка ОБЫЧНОГО варианта;
  * lane_ts_sent_buy -- отметка ровно перед отправкой (после сборки и подписи
                        обычного варианта И после сборки+подписи всех вариантов
                        на nonce);
  * lane_send_ms     -- сама отправка (сеть до сервисов).

Чего в журнале НЕТ: времени подписи (sign_ms остаётся во внутреннем словаре и
в позицию не пишется) и времени сборки вариантов на nonce по отдельности.
Поэтому здесь считается ОСТАТОК:

    остаток = (lane_ts_sent_buy - signal_recv_ts) * 1000 - lane_build_ms

В остатке лежат: гейты, разбор, подпись обычного варианта и сборка+подпись
шести вариантов на nonce (они делаются последовательно, в одном потоке).
Остаток -- это и есть то, что можно ускорить, и на что смотрит правка.
"""

from __future__ import annotations

import argparse
import gzip
import json
import statistics
from pathlib import Path

МЕТКА = "own_send"


def открыть(путь: Path):
    if str(путь).endswith(".gz"):
        return gzip.open(путь, "rt", encoding="utf-8", errors="replace")
    return путь.open(encoding="utf-8", errors="replace")


def позиции(пути: list) -> dict:
    """Свёрнутые позиции из нескольких файлов журнала (включая ротированные).

    Строки журнала дописываются, последняя правит поля -- ровно так же, как это
    делает lane_table: одна реализация смысла, разные входы.
    """
    из_: dict = {}
    for путь in пути:
        п = Path(путь)
        if not п.exists():
            continue
        with открыть(п) as ф:
            for строка in ф:
                строка = строка.strip()
                if not строка.startswith("{"):
                    continue
                try:
                    з = json.loads(строка)
                except ValueError:
                    continue
                cid = з.get("client_order_id")
                if not cid:
                    continue
                из_.setdefault(cid, {}).update(
                    {к: v for к, v in з.items() if v is not None})
    return из_


def ряды(поз: dict, *, с_utc: str = "") -> list:
    из_ = []
    for cid, п in поз.items():
        if п.get("lane") != МЕТКА:
            continue
        когда = str(п.get("ts_intent_utc") or "")
        if с_utc and когда and когда < с_utc:
            continue
        сигнал = п.get("signal_recv_ts")
        отправка = п.get("lane_ts_sent_buy")
        if not сигнал or not отправка:
            continue
        до_отправки = (float(отправка) - float(сигнал)) * 1000.0
        сборка = float(п.get("lane_build_ms") or 0.0)
        кандидатов = len(п.get("lane_pool_candidates") or [])
        из_.append({
            "cid": cid, "utc": когда, "группа": п.get("lane_group"),
            "режим": п.get("lane_pool_mode"),
            "до_отправки_мс": round(до_отправки, 2),
            "сборка_мс": round(сборка, 3),
            "остаток_мс": round(до_отправки - сборка, 2),
            "отправка_мс": п.get("lane_send_ms"),
            "вариантов": кандидатов,
            "на_вариант_мс": (round((до_отправки - сборка) / кандидатов, 2)
                               if кандидатов else None),
            "довёз": п.get("lane_pool_winner"),
        })
    return sorted(из_, key=lambda з: з["utc"])


def p(значения: list, доля: float):
    if not значения:
        return None
    з = sorted(значения)
    и = min(len(з) - 1, int(round(доля * (len(з) - 1))))
    return round(з[и], 2)


def сводка(ряд: list) -> dict:
    def столбец(имя):
        зн = [з[имя] for з in ряд if з.get(имя) is not None]
        if not зн:
            return None
        return {"n": len(зн), "медиана": round(statistics.median(зн), 2),
                 "p90": p(зн, 0.9), "мин": round(min(зн), 2),
                 "макс": round(max(зн), 2)}

    по_режимам = {}
    for з in ряд:
        по_режимам.setdefault(з["режим"] or "без режима", []).append(з)
    return {
        "покупок": len(ряд),
        "до_отправки_мс": столбец("до_отправки_мс"),
        "сборка_мс": столбец("сборка_мс"),
        "остаток_мс": столбец("остаток_мс"),
        "отправка_мс": столбец("отправка_мс"),
        "на_вариант_мс": столбец("на_вариант_мс"),
        "вариантов_медиана": (statistics.median([з["вариантов"] for з in ряд])
                               if ряд else None),
        "по_режимам": {к: len(v) for к, v in по_режимам.items()},
    }


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--positions", nargs="+", required=True,
                   help="файлы журнала позиций, можно .gz")
    р.add_argument("--since", default="")
    р.add_argument("--out", default="data/lane_timing.json")
    р.add_argument("--rows", type=int, default=25)
    а = р.parse_args()

    поз = позиции(а.positions)
    ряд = ряды(поз, с_utc=а.since)
    св = сводка(ряд)
    Path(а.out).parent.mkdir(parents=True, exist_ok=True)
    Path(а.out).write_text(
        json.dumps({"since": а.since, "сводка": св, "ряды": ряд},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"позиций в журналах: {len(поз)}; покупок полосы со временем: {len(ряд)}")
    print(json.dumps(св, ensure_ascii=False, indent=1))
    print()
    print("| время | группа | режим | решение→отправка, мс | сборка, мс | "
          "остаток, мс | вариантов | остаток/вариант, мс | отправка, мс |")
    print("|---|---|---|---|---|---|---|---|---|")
    for з in ряд[-а.rows:]:
        print(f"| {з['utc'][11:19]} | {з['группа']} | {з['режим']} | "
              f"{з['до_отправки_мс']} | {з['сборка_мс']} | {з['остаток_мс']} | "
              f"{з['вариантов']} | {з['на_вариант_мс']} | {з['отправка_мс']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
