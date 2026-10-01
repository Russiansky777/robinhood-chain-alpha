#!/usr/bin/env python3
"""Р («решили») по часам из записей позиций: медиана, p90, n. Только чтение.

ЗАЧЕМ (слово владельца 01.10, п.1). По TG Р выросло с 10-22 мс ночью до 37 в
08:07, 96 в 09:39, 117 в 14:52 и 144 в 15:21. Нужна таблица по часам с 22:00Z
30.09, до и после перезапуска, и разложение по подшагам на 5 последних сделках
против 5 ночных.

ОТКУДА ЧИСЛА. Р НЕ СЧИТАЕТСЯ ЗДЕСЬ: берётся поле путь_решили у того же
переводчика записи, что печатает BUY и часовую (bloom_doklad.поля). Второй счёт
одного числа расходится молча -- именно это правило уже стоило репозиторию
суточной сверки. Подшаги берутся полем podshagi_ms, которое пишет сама полоса
монотонными часами.

ЧЕГО ЗДЕСЬ НЕТ. Сети, подписей, отправок, записи в журналы службы. Это
измеритель.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
for _п in (КОРЕНЬ / "analysis", Path("/home/bot/bloom_executor")):
    if _п.exists() and str(_п) not in sys.path:
        sys.path.insert(0, str(_п))

ПЕРЕЗАПУСК_ПО_УМОЛЧАНИЮ = "2026-10-01T10:14:25Z"


def _ts(текст: str) -> float:
    """'2026-10-01T10:14:25Z' -> unix. Своего разбора времени не пишем."""
    import datetime as d
    т = str(текст).strip().replace("Z", "+00:00")
    return d.datetime.fromisoformat(т).timestamp()


def _чч(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%HZ", time.gmtime(float(ts)))


def p90(значения: list) -> float | None:
    """p90 по ближайшему рангу. Одно значение -- оно же и p90, не None."""
    if not значения:
        return None
    з = sorted(значения)
    и = max(0, min(len(з) - 1, int(round(0.9 * (len(з) - 1)))))
    return з[и]


def позиции_полосы(путь: str | None) -> list:
    """Записи позиций полосы. Файл не прочитан -- исключение наружу."""
    import bloom_exec_state as ST
    if путь:
        строки = []
        with open(путь, encoding="utf-8") as ф:
            for с in ф:
                с = с.strip()
                if not с:
                    continue
                try:
                    строки.append(json.loads(с))
                except Exception:  # noqa: BLE001
                    continue
        # Журнал -- ЛЕНТА ПРАВОК: последняя запись по cid и есть позиция.
        слито: dict = {}
        for з in строки:
            cid = з.get("client_order_id") or з.get("cid")
            if not cid:
                continue
            слито.setdefault(cid, {}).update(з)
        все = list(слито.values())
    else:
        ст = ST.ExecState()
        все = list((ст.positions() or {}).values())
    метка = getattr(ST, "МЕТКА_ПОЛОСЫ", "own_send")
    return [p for p in все if (p or {}).get("lane") == метка]


def р_записи(поз: dict) -> float | None:
    """Р одной записи -- ПОЛЕМ путь_решили переводчика, не своим счётом."""
    import bloom_doklad as DK
    try:
        п = DK.поля(поз) or {}
    except Exception:  # noqa: BLE001
        return None
    з = п.get("путь_решили")
    return float(з) if isinstance(з, (int, float)) else None


def по_часам(позиции: list, *, с_ts: float, перезапуск_ts: float) -> dict:
    """Таблица по часам плюс две сводки: до и после перезапуска."""
    часы: dict = {}
    до: list = []
    после: list = []
    без_р = 0
    for p in позиции:
        т = p.get("ts_intent")
        if not isinstance(т, (int, float)) or float(т) < с_ts:
            continue
        р = р_записи(p)
        if р is None:
            без_р += 1
            continue
        часы.setdefault(_чч(т), []).append(р)
        (после if float(т) >= перезапуск_ts else до).append(р)

    def свод(зн: list) -> dict:
        return {"n": len(зн),
                 "медиана": round(statistics.median(зн), 1) if зн else None,
                 "p90": round(p90(зн), 1) if зн else None}

    return {"по_часам": {ч: свод(зн) for ч, зн in sorted(часы.items())},
             "до_перезапуска": свод(до), "после_перезапуска": свод(после),
             "записей_без_Р": без_р,
             "перезапуск_utc": _чч(перезапуск_ts),
             "почему_без_Р": ("у записи нет signal_recv_ts или времени отправки "
                               "-- Р не из чего посчитать" if без_р else None)}


def метки(поз: dict) -> dict:
    """Разложение по МЕТКАМ ЗАПИСИ (ts_decision / ts_signed) -- слово владельца.

    podshagi_ms лежит в журнале РЕШЕНИЙ, а не в позиции, и на выгрузке позиций
    его нет вовсе. Зато в позиции есть метки, которые полоса пишет сама:

      приём → решение  = signal_recv_ts .. ts_decision  -- разбор, размер, ГЕЙТ
      решение → подпись = ts_decision .. ts_signed      -- сборка и симуляция
      подпись → отправка = ts_signed .. lane_ts_sent_buy

    ПЕРВЫЙ КУСОК И ЕСТЬ Р с точностью до подписи: именно в нём стоит гейт полосы.
    Чего нет -- то None, а не ноль: отсутствие метки это не "мгновенно".
    """
    def мс(а, б):
        if not isinstance(а, (int, float)) or not isinstance(б, (int, float)):
            return None
        з = (float(б) - float(а)) * 1000.0
        return round(з, 1) if з >= 0 else None

    приём = поз.get("signal_recv_ts")
    реш = поз.get("ts_decision")
    подп = поз.get("ts_signed")
    посл = поз.get("lane_ts_sent_buy") or поз.get("ts_sent")
    return {"приём_решение_мс": мс(приём, реш),
             "решение_подпись_мс": мс(реш, подп),
             "подпись_отправка_мс": мс(подп, посл)}


def по_меткам(позиции: list, *, сколько: int, ночь_до_ts: float) -> dict:
    """Метки у последних и у ночных сделок плюс медианы по каждому куску."""
    годные = [p for p in позиции if isinstance(p.get("ts_intent"), (int, float))]
    годные.sort(key=lambda p: float(p["ts_intent"]))
    ночные = [p for p in годные if float(p["ts_intent"]) < ночь_до_ts]

    def строки(набор):
        из_ = []
        for p in набор:
            из_.append({"cid": p.get("client_order_id"),
                         "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(float(p["ts_intent"]))),
                         "группа": p.get("lane_group"), "Р_мс": р_записи(p),
                         **метки(p)})
        return из_

    def медианы(набор):
        из_ = {}
        for ключ in ("приём_решение_мс", "решение_подпись_мс", "подпись_отправка_мс"):
            зн = [м[ключ] for м in (метки(p) for p in набор)
                  if м[ключ] is not None]
            из_[ключ] = round(statistics.median(зн), 1) if зн else None
            из_[ключ + "_n"] = len(зн)
        return из_

    посл = годные[-сколько:]
    ноч = ночные[-сколько:]
    return {"последние": строки(посл), "ночные": строки(ноч),
             "медианы_последних": медианы(посл), "медианы_ночных": медианы(ноч)}


def подшаги(позиции: list, *, сколько: int, ночь_до_ts: float) -> dict:
    """Подшаги у 5 последних сделок и у 5 ночных. Поле podshagi_ms, как есть."""
    с_подшагами = [p for p in позиции
                   if isinstance(p.get("podshagi_ms"), dict) and p["podshagi_ms"]
                   and isinstance(p.get("ts_intent"), (int, float))]
    с_подшагами.sort(key=lambda p: float(p["ts_intent"]))
    ночные = [p for p in с_подшагами if float(p["ts_intent"]) < ночь_до_ts]

    def строки(набор: list) -> list:
        из_ = []
        for p in набор:
            из_.append({"cid": p.get("client_order_id"),
                         "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(float(p["ts_intent"]))),
                         "группа": p.get("lane_group"),
                         "Р_мс": р_записи(p),
                         "подшаги_мс": p["podshagi_ms"]})
        return из_

    def медианы(набор: list) -> dict:
        имена: dict = {}
        for p in набор:
            for имя, мс in (p["podshagi_ms"] or {}).items():
                if isinstance(мс, (int, float)):
                    имена.setdefault(имя, []).append(float(мс))
        return {и: round(statistics.median(з), 2) for и, з in sorted(имена.items())}

    последние = с_подшагами[-сколько:]
    ночь = ночные[-сколько:]
    return {"последние": строки(последние), "ночные": строки(ночь),
             "медианы_последних": медианы(последние),
             "медианы_ночных": медианы(ночь),
             "записей_с_подшагами": len(с_подшагами),
             "почему_пусто": (None if с_подшагами else
                               "ни в одной записи нет podshagi_ms -- разложить "
                               "по подшагам нечем")}


def самопроверка() -> int:
    из_строя = []

    def chk(имя, усл, *лишнее):
        print(("ok  " if усл else "СБОЙ") + "  " + имя
               + ("" if усл else "  " + repr(лишнее)))
        if not усл:
            из_строя.append(имя)

    chk("время разбирается", abs(_ts("2026-10-01T10:14:25Z") - 1790849665.0) < 1.0,
        _ts("2026-10-01T10:14:25Z"))
    chk("p90 одного значения -- оно само", p90([5.0]) == 5.0)
    chk("p90 пустого -- None, а не ноль", p90([]) is None)
    # p90 по БЛИЖАЙШЕМУ РАНГУ: индекс round(0.9*(n-1)) = round(8.1) = 8, то есть
    # девятое из десяти значений. Ждать здесь максимум -- значит ждать p100.
    chk("p90 по ближайшему рангу: девятое из десяти, а не максимум",
        p90(list(range(1, 11))) == 9, p90(list(range(1, 11))))
    chk("p90 не равен максимуму на десяти значениях",
        p90(list(range(1, 11))) != max(range(1, 11)))

    # Р берётся ПОЛЕМ переводчика: ставим запись, у которой Р считается, и
    # вторую, у которой посчитать нельзя -- она обязана уйти в "без Р".
    осн = {"client_order_id": "p1", "lane": "own_send", "mint": "M1",
            "sol_in": 0.01, "ts_intent": 1000.0, "seen_lag_ms": 100.0,
            "signal_recv_ts": 1000.0, "lane_ts_sent_buy": 1000.144,
            "lane_build_ms": 7.0, "lane_sign_ms": 3.0,
            "podshagi_ms": {"разбор и размер": 1.0, "гейт полосы": 130.0,
                             "сборка": 7.0, "всего": 138.0}}
    chk("Р одной записи = путь_решили (144 после приёма минус 10 сборки)",
        р_записи(осн) == 134.0, р_записи(осн))
    нет_р = {"client_order_id": "p2", "lane": "own_send", "mint": "M2",
              "sol_in": 0.01, "ts_intent": 2000.0}
    chk("без времён Р -- None, а не ноль", р_записи(нет_р) is None, р_записи(нет_р))

    т = по_часам([осн, нет_р], с_ts=0.0, перезапуск_ts=1500.0)
    chk("час посчитан, запись без Р отделена",
        т["по_часам"]["1970-01-01T00Z"]["n"] == 1 and т["записей_без_Р"] == 1, т)
    chk("до и после перезапуска разделены по ts_intent",
        т["до_перезапуска"]["n"] == 1 and т["после_перезапуска"]["n"] == 0, т)
    chk("почему без Р названо словами", "signal_recv_ts" in (т["почему_без_Р"] or ""))

    п = подшаги([осн, нет_р], сколько=5, ночь_до_ts=1500.0)
    chk("подшаги взяты только у записи с podshagi_ms",
        п["записей_с_подшагами"] == 1 and len(п["последние"]) == 1, п)
    chk("медиана подшага посчитана по именам",
        п["медианы_последних"]["гейт полосы"] == 130.0, п["медианы_последних"])
    chk("ночные отобраны по времени", len(п["ночные"]) == 1)
    пусто = подшаги([нет_р], сколько=5, ночь_до_ts=1500.0)
    chk("нет подшагов нигде -- сказано словами, а не пустая таблица",
        "нечем" in (пусто["почему_пусто"] or ""), пусто["почему_пусто"])

    осн_м = dict(осн, ts_decision=1000.130, ts_signed=1000.137)
    м = метки(осн_м)
    chk("метки: приём→решение = 130 мс (там и стоит гейт)",
        м["приём_решение_мс"] == 130.0, м)
    chk("метки: решение→подпись = 7 мс", м["решение_подпись_мс"] == 7.0, м)
    chk("метки: подпись→отправка = 7 мс", м["подпись_отправка_мс"] == 7.0, м)
    chk("нет метки -- None, а не ноль",
        метки({"signal_recv_ts": 1.0})["приём_решение_мс"] is None)
    chk("отрицательная разность не превращается в число",
        метки({"signal_recv_ts": 2.0, "ts_decision": 1.0})["приём_решение_мс"] is None)
    пм = по_меткам([осн_м, нет_р], сколько=5, ночь_до_ts=1500.0)
    chk("медиана по метке посчитана только по тем, у кого метка есть",
        пм["медианы_последних"]["приём_решение_мс"] == 130.0
        and пм["медианы_последних"]["приём_решение_мс_n"] == 1,
        пм["медианы_последних"])

    # САМОЕ ГЛАВНОЕ: модуль обязан брать Р у переводчика службы, а не считать
    # сам. Если bloom_doklad.поля перестанет отдавать путь_решили, измеритель
    # должен молчать, а не выдумывать число.
    import bloom_doklad as DK
    chk("bloom_doklad.поля существует и отдаёт путь_решили",
        "путь_решили" in (DK.поля(осн) or {}))

    print(f"итог: самопроверка {'норма' if not из_строя else 'СБОЙ'}; "
           f"провалов {len(из_строя)}")
    return 1 if из_строя else 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", default="2026-09-30T22:00:00Z", help="с какого времени UTC")
    р.add_argument("--perezapusk", default=ПЕРЕЗАПУСК_ПО_УМОЛЧАНИЮ)
    р.add_argument("--noch-do", default="2026-10-01T06:00:00Z",
                    help="что считать ночью: записи раньше этого времени")
    р.add_argument("--skolko", type=int, default=5)
    р.add_argument("--pozicii", default="", help="файл positions.jsonl (пусто -- из состояния)")
    р.add_argument("--out", default=None)
    р.add_argument("--self-test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return самопроверка()
    поз = позиции_полосы(а.pozicii or None)
    свод_ = {"позиций_полосы": len(поз), "окно_с": а.s,
              "перезапуск": а.perezapusk, "ночь_до": а.noch_do}
    свод_.update(по_часам(поз, с_ts=_ts(а.s), перезапуск_ts=_ts(а.perezapusk)))
    свод_["подшаги"] = подшаги(поз, сколько=а.skolko, ночь_до_ts=_ts(а.noch_do))
    свод_["по_меткам"] = по_меткам(поз, сколько=а.skolko, ночь_до_ts=_ts(а.noch_do))
    print(json.dumps(свод_, ensure_ascii=False, indent=1))
    if а.out:
        Path(а.out).parent.mkdir(parents=True, exist_ok=True)
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(свод_, ф, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
