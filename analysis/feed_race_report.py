#!/usr/bin/env python3
"""Доклад по гонке потоков на ПОЛНОЙ выборке -- из журнала зонда.

Зачем отдельно от зонда. В самом зонде сводка считается по окну памяти
(Гонка выселяет подписи старше окна), и это правильно для признака жизни:
служба не должна расти в памяти сутками. Но владелец просит доклад по всей
выборке за час -- тысячи пар, p99 и обрывы. Полная выборка лежит в журнале
feed_race.jsonl: каждая пришедшая подпись со своим каналом и меткой времени.
Поэтому доклад считается ЗДЕСЬ, по записанному, а не по памяти службы.

Три вещи, которые легко посчитать неправильно:

  * МЕТКА ВРЕМЕНИ -- time.monotonic() процесса. При перезапуске службы она
    начинает отсчёт заново, а журнал продолжается тем же файлом. Сравнивать
    метки из разных запусков нельзя: получится разница часов, а не каналов.
    Поэтому записи режутся на отрезки по откату времени назад;
  * ГРУППА. У gRPC в записи стоит имя фильтра (src0, boost0), у Helius WS --
    адрес подписки. Группа берётся из того, что есть, и подпись, увиденная
    обоими, наследует группу от любого канала, который её знает;
  * ОБРЫВЫ. Строки с полем error и thread_died -- это не сообщения, и в
    статистику дельт они не идут, но именно они отвечают на вопрос про
    падения беты.

Совместная гонка. Вопрос владельца: что дала бы подписка "Helius + лучший
Shyft, кто первый" против одного Helius. Считается честно: выигрыш по
каждой подписи = max(0, t_helius - t_shyft), плюс ОТДЕЛЬНО число подписей,
которых Helius не видел вовсе -- их одиночный Helius не увидел бы никогда,
и в медиану выигрыша они не входят.

Только чтение. Ни одного сетевого вызова, ни одного ордера.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

КАНАЛ_WS = "helius_ws"
КАНАЛ_GRPC = "grpc"
КАНАЛ_GRPC2 = "grpc2"
КАНАЛ_RABBIT = "rabbit_ams"
КАНАЛ_RABBIT2 = "rabbit_fra"
ГРУППА_НАША = "источники"
ГРУППА_РАЗГОН = "разгонные"
# Откат метки времени назад больше этого -- признак нового запуска службы.
ОТКАТ_НОВОГО_ЗАПУСКА_S = 1.0


def _процентиль(значения: list, доля: float) -> float:
    """Процентиль по отсортированному списку с линейной интерполяцией."""
    if not значения:
        return 0.0
    с = sorted(значения)
    if len(с) == 1:
        return float(с[0])
    место = доля * (len(с) - 1)
    низ = int(место)
    верх = min(низ + 1, len(с) - 1)
    вес = место - низ
    return float(с[низ] * (1 - вес) + с[верх] * вес)


def числа(дельты: list) -> dict:
    """Числа по списку дельт в мс. Плюс -- первый канал пришёл раньше."""
    if not дельты:
        return {"pairs": 0}
    return {"pairs": len(дельты),
             "first_share": round(sum(1 for d in дельты if d > 0) / len(дельты), 4),
             "median_ms": round(statistics.median(дельты), 2),
             "p10_ms": round(_процентиль(дельты, 0.10), 2),
             "p90_ms": round(_процентиль(дельты, 0.90), 2),
             "p95_ms": round(_процентиль(дельты, 0.95), 2),
             "p99_ms": round(_процентиль(дельты, 0.99), 2),
             "mean_ms": round(sum(дельты) / len(дельты), 2)}


def группа_записи(з: dict, разгонные: set) -> str | None:
    """Группа по записи: у gRPC -- имя фильтра, у WS -- адрес подписки."""
    имя = з.get("filter")
    if isinstance(имя, str) and имя:
        if имя.startswith("boost"):
            return ГРУППА_РАЗГОН
        if имя.startswith("src"):
            return ГРУППА_НАША
    адрес = з.get("address")
    if isinstance(адрес, str) and адрес:
        return ГРУППА_РАЗГОН if адрес in разгонные else ГРУППА_НАША
    return None


def читать(пути: list) -> list:
    """Строки журнала как список словарей. Битые строки считаются отдельно."""
    записи = []
    битых = 0
    for путь in пути:
        with open(путь, encoding="utf-8", errors="replace") as f:
            for строка in f:
                строка = строка.strip()
                if not строка:
                    continue
                try:
                    записи.append(json.loads(строка))
                except ValueError:
                    битых += 1
    if битых:
        print(f"битых строк журнала пропущено: {битых}", file=sys.stderr)
    return записи


def отрезки(записи: list) -> list:
    """Записи, порезанные на запуски службы по откату монотонных часов.

    Внутри отрезка метки сравнимы, между отрезками -- нет.
    """
    из_: list = []
    текущий: list = []
    максимум = None
    for з in записи:
        t = з.get("t")
        if not isinstance(t, (int, float)):
            текущий.append(з)
            continue
        if максимум is not None and t < максимум - ОТКАТ_НОВОГО_ЗАПУСКА_S:
            из_.append(текущий)
            текущий = []
            максимум = None
        максимум = t if максимум is None else max(максимум, t)
        текущий.append(з)
    if текущий:
        из_.append(текущий)
    return из_


def события_отрезка(записи: list, разгонные: set) -> dict:
    """подпись -> {"t": {канал: самая ранняя метка}, "группа": ...}."""
    из_: dict = {}
    for з in записи:
        подпись = з.get("signature")
        канал = з.get("channel")
        t = з.get("t")
        if not подпись or not канал or not isinstance(t, (int, float)):
            continue
        е = из_.setdefault(подпись, {"t": {}, "группа": None, "повторов": 0})
        if канал in е["t"]:
            # Повтор той же подписи по тому же каналу -- не пара, а дубль.
            е["повторов"] += 1
            е["t"][канал] = min(е["t"][канал], t)
        else:
            е["t"][канал] = t
        г = группа_записи(з, разгонные)
        if г and not е["группа"]:
            е["группа"] = г
    return из_


def дельты(события: dict, a: str, b: str, группа: str | None = None) -> list:
    """Дельты в мс между каналами a и b. Плюс -- a пришёл раньше b."""
    из_ = []
    for е in события.values():
        if группа is not None and е["группа"] != группа:
            continue
        if a in е["t"] and b in е["t"]:
            из_.append((е["t"][b] - е["t"][a]) * 1000.0)
    return из_


def обрывы(записи: list) -> dict:
    """Обрывы и смерти потоков по каналам -- из тех же строк журнала."""
    из_: dict = {}
    for з in записи:
        канал = з.get("channel")
        if not канал:
            continue
        текст = з.get("error") or з.get("thread_died")
        if not текст:
            continue
        к = из_.setdefault(канал, {"count": 0, "last": "", "kinds": {}})
        к["count"] += 1
        к["last"] = str(текст)[:200]
        вид = str(текст).split(":")[0][:60]
        к["kinds"][вид] = к["kinds"].get(вид, 0) + 1
    return из_


def совместная_гонка(события: dict, база: str, помощник: str,
                      группа: str | None = None) -> dict:
    """Что дала бы подписка "база + помощник, кто первый" против одной базы.

    Выигрыш по подписи -- max(0, t_база - t_помощник) в мс: на столько
    раньше мы увидели бы сделку. Подписи, которых база не видела вовсе,
    в медиану не входят: там выигрыш не число, а сам факт, и он считается
    отдельно.
    """
    выигрыши = []
    только_помощник = 0
    только_база = 0
    обе = 0
    for е in события.values():
        if группа is not None and е["группа"] != группа:
            continue
        есть_б = база in е["t"]
        есть_п = помощник in е["t"]
        if есть_б and есть_п:
            обе += 1
            выигрыши.append(max(0.0, (е["t"][база] - е["t"][помощник]) * 1000.0))
        elif есть_п:
            только_помощник += 1
        elif есть_б:
            только_база += 1
    из_ = {"base": база, "helper": помощник, "both": обе,
            "only_helper_saw": только_помощник, "only_base_saw": только_база}
    if выигрыши:
        ненулевые = [в for в in выигрыши if в > 0]
        из_.update({
            "gain_median_ms": round(statistics.median(выигрыши), 2),
            "gain_p90_ms": round(_процентиль(выигрыши, 0.90), 2),
            "gain_p95_ms": round(_процентиль(выигрыши, 0.95), 2),
            "gain_p99_ms": round(_процентиль(выигрыши, 0.99), 2),
            "gain_mean_ms": round(sum(выигрыши) / len(выигрыши), 2),
            "helper_first_share": round(len(ненулевые) / len(выигрыши), 4)})
    return из_


def лучший_помощник(события: dict, база: str, кандидаты: list) -> str | None:
    """Кандидат с наибольшим средним выигрышем -- он и есть "лучший Shyft"."""
    лучший, лучшее = None, None
    for к in кандидаты:
        с = совместная_гонка(события, база, к)
        если_есть = с.get("gain_mean_ms")
        if если_есть is None:
            continue
        if лучшее is None or если_есть > лучшее:
            лучший, лучшее = к, если_есть
    return лучший


def доклад(записи: list, разгонные: set, каналы: list) -> dict:
    """Полный доклад по журналу: отрезки, пары, группы, обрывы, гонка."""
    части = отрезки(записи)
    все_события: dict = {}
    for i, часть in enumerate(части):
        for подпись, е in события_отрезка(часть, разгонные).items():
            # Ключ с номером отрезка: одна подпись в двух запусках -- это
            # два независимых наблюдения, а не одна пара.
            все_события[(i, подпись)] = е
    из_: dict = {"segments": len(части), "records": len(записи),
                  "events": len(все_события),
                  "messages": {}, "pairwise": {}, "by_group": {},
                  "disconnects": обрывы(записи)}
    for к in каналы:
        из_["messages"][к] = sum(1 for е in все_события.values() if к in е["t"])
    for i, a in enumerate(каналы):
        for b in каналы[i + 1:]:
            д = дельты(все_события, a, b)
            if д:
                из_["pairwise"][f"{a}_vs_{b}"] = числа(д)
    for г in (ГРУППА_НАША, ГРУППА_РАЗГОН):
        часть: dict = {}
        for i, a in enumerate(каналы):
            for b in каналы[i + 1:]:
                д = дельты(все_события, a, b, группа=г)
                if д:
                    часть[f"{a}_vs_{b}"] = числа(д)
        часть["messages"] = {к: sum(1 for е in все_события.values()
                                     if к in е["t"] and е["группа"] == г)
                              for к in каналы}
        из_["by_group"][г] = часть
    # Цена раннего срабатывания: транзакция пришла по каналу, а по Helius WS
    # её не было вовсе -- значит, она упала или не вошла в блок. Для потока
    # из шредов (RabbitStream) это главный вопрос: он показывает сделку ДО
    # исполнения, и часть его сделок исполнением не станет.
    из_["not_seen_by_ws"] = {}
    for к in каналы:
        if к == КАНАЛ_WS:
            continue
        всего_к = sum(1 for е in все_события.values() if к in е["t"])
        без_ws = sum(1 for е in все_события.values()
                      if к in е["t"] and КАНАЛ_WS not in е["t"])
        из_["not_seen_by_ws"][к] = {
            "messages": всего_к, "not_in_ws": без_ws,
            "share": (round(без_ws / всего_к, 4) if всего_к else None)}
    помощники = [к for к in каналы if к != КАНАЛ_WS]
    лучший = лучший_помощник(все_события, КАНАЛ_WS, помощники)
    из_["combined"] = {
        "best_helper": лучший,
        "all": {к: совместная_гонка(все_события, КАНАЛ_WS, к) for к in помощники},
        "best_by_group": ({г: совместная_гонка(все_события, КАНАЛ_WS, лучший,
                                                группа=г)
                            for г in (ГРУППА_НАША, ГРУППА_РАЗГОН)}
                           if лучший else {})}
    return из_


def self_test() -> None:
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    chk("процентиль на одном значении не падает", _процентиль([5.0], 0.99) == 5.0)
    # 0..99, линейная интерполяция: 0.99 * (100 - 1) = 98.01. Ровно 99 дал бы
    # только "взять последний элемент", а это уже максимум, а не p99.
    chk("p99 берёт верх выборки с интерполяцией",
        abs(_процентиль([float(i) for i in range(100)], 0.99) - 98.01) < 0.01,
        _процентиль([float(i) for i in range(100)], 0.99))

    # Отрезки: откат монотонных часов назад -- это новый запуск службы.
    з = [{"t": 10.0, "channel": "grpc", "signature": "A"},
         {"t": 11.0, "channel": "grpc", "signature": "B"},
         {"t": 0.5, "channel": "grpc", "signature": "C"}]
    chk("откат времени режет журнал на запуски", len(отрезки(з)) == 2,
        [len(x) for x in отрезки(з)])
    chk("ровный журнал -- один отрезок",
        len(отрезки(з[:2])) == 1, отрезки(з[:2]))

    # Одна подпись в двух запусках парой НЕ становится.
    два = [{"t": 10.0, "channel": КАНАЛ_WS, "signature": "X", "address": "И1"},
           {"t": 0.2, "channel": КАНАЛ_GRPC, "signature": "X", "filter": "src0"}]
    д2 = доклад(два, разгонные=set(), каналы=[КАНАЛ_WS, КАНАЛ_GRPC])
    chk("подпись из разных запусков не даёт пару",
        д2["segments"] == 2 and not д2["pairwise"], д2["pairwise"])

    # Пары, знак и группы.
    ж = [{"t": 1.000, "channel": КАНАЛ_WS, "signature": "S1", "address": "ИСТ"},
         {"t": 1.010, "channel": КАНАЛ_GRPC, "signature": "S1", "filter": "src0"},
         {"t": 2.000, "channel": КАНАЛ_GRPC, "signature": "S2", "filter": "boost0"},
         {"t": 2.030, "channel": КАНАЛ_WS, "signature": "S2", "address": "РАЗГ"},
         {"t": 3.000, "channel": КАНАЛ_GRPC, "signature": "S3", "filter": "boost0"}]
    д = доклад(ж, разгонные={"РАЗГ"}, каналы=[КАНАЛ_WS, КАНАЛ_GRPC])
    п = д["pairwise"]["helius_ws_vs_grpc"]
    chk("две пары найдены", п["pairs"] == 2, п)
    chk("знак: +10 мс значит WS раньше на 10 мс",
        abs(sorted(дельты({к: v for к, v in
                            события_отрезка(ж, {"РАЗГ"}).items()},
                           КАНАЛ_WS, КАНАЛ_GRPC))[1] - 10.0) < 0.01,
        дельты(события_отрезка(ж, {"РАЗГ"}), КАНАЛ_WS, КАНАЛ_GRPC))
    chk("доля 'первый' считается по знаку", п["first_share"] == 0.5, п)
    chk("группа источников отделена от разгонных",
        д["by_group"][ГРУППА_НАША]["helius_ws_vs_grpc"]["pairs"] == 1
        and д["by_group"][ГРУППА_РАЗГОН]["helius_ws_vs_grpc"]["pairs"] == 1,
        д["by_group"])
    chk("сообщения считаются по каналам",
        д["messages"][КАНАЛ_GRPC] == 3 and д["messages"][КАНАЛ_WS] == 2,
        д["messages"])

    # Совместная гонка: выигрыш только там, где помощник был раньше.
    с = совместная_гонка(события_отрезка(ж, {"РАЗГ"}), КАНАЛ_WS, КАНАЛ_GRPC)
    chk("выигрыш считается только вперёд: 0 и 30 мс",
        с["both"] == 2 and abs(с["gain_median_ms"] - 15.0) < 0.01, с)
    chk("подпись, которую база не видела, считается отдельно",
        с["only_helper_saw"] == 1 and с["only_base_saw"] == 0, с)
    chk("доля 'помощник был первым' -- половина", с["helper_first_share"] == 0.5, с)

    # Лучший помощник -- тот, кто даёт больший средний выигрыш.
    ж2 = ж + [{"t": 1.005, "channel": КАНАЛ_GRPC2, "signature": "S1",
                "filter": "src0"},
               {"t": 2.025, "channel": КАНАЛ_GRPC2, "signature": "S2",
                "filter": "boost0"}]
    д3 = доклад(ж2, разгонные={"РАЗГ"},
                 каналы=[КАНАЛ_WS, КАНАЛ_GRPC, КАНАЛ_GRPC2])
    chk("лучшим выбран канал с большим средним выигрышем",
        д3["combined"]["best_helper"] == КАНАЛ_GRPC,
        {к: v.get("gain_mean_ms") for к, v in д3["combined"]["all"].items()})

    # Обрывы.
    ж3 = ж + [{"channel": КАНАЛ_GRPC2, "error": "RpcError: UNAVAILABLE"},
               {"channel": КАНАЛ_GRPC2, "error": "RpcError: UNAVAILABLE"},
               {"channel": КАНАЛ_WS, "thread_died": "TypeError: x"}]
    о = обрывы(ж3)
    chk("обрывы считаются по каналам и видам",
        о[КАНАЛ_GRPC2]["count"] == 2 and о[КАНАЛ_WS]["count"] == 1
        and о[КАНАЛ_GRPC2]["kinds"]["RpcError"] == 2, о)
    chk("обрывы в дельты не попадают",
        доклад(ж3, разгонные={"РАЗГ"},
                каналы=[КАНАЛ_WS, КАНАЛ_GRPC])["pairwise"]
        ["helius_ws_vs_grpc"]["pairs"] == 2,
        доклад(ж3, разгонные=set(), каналы=[КАНАЛ_WS, КАНАЛ_GRPC])["pairwise"])

    # Повтор той же подписи по тому же каналу -- дубль, а не вторая метка.
    ж4 = [{"t": 1.0, "channel": КАНАЛ_GRPC, "signature": "D", "filter": "src0"},
          {"t": 1.5, "channel": КАНАЛ_GRPC, "signature": "D", "filter": "src0"},
          {"t": 1.2, "channel": КАНАЛ_WS, "signature": "D", "address": "ИСТ"}]
    е4 = события_отрезка(ж4, set())
    chk("дубль не создаёт второй канал и берёт раннюю метку",
        е4["D"]["повторов"] == 1 and abs(е4["D"]["t"][КАНАЛ_GRPC] - 1.0) < 1e-9,
        е4["D"])

    # Доля "не дошло до Helius": цена раннего срабатывания потока из шредов.
    ж5 = [{"t": 1.0, "channel": КАНАЛ_RABBIT, "signature": "R1", "filter": "src0"},
          {"t": 1.01, "channel": КАНАЛ_WS, "signature": "R1", "address": "ИСТ"},
          {"t": 2.0, "channel": КАНАЛ_RABBIT, "signature": "R2", "filter": "src0"},
          {"t": 3.0, "channel": КАНАЛ_RABBIT, "signature": "R3", "filter": "src0"},
          {"t": 9.0, "channel": КАНАЛ_WS, "signature": "ХВОСТ", "address": "ИСТ"}]
    д5 = доклад(ж5, разгонные=set(), каналы=[КАНАЛ_WS, КАНАЛ_RABBIT])
    chk("доля не дошедших до Helius посчитана",
        д5["not_seen_by_ws"][КАНАЛ_RABBIT]["not_in_ws"] == 2
        and abs(д5["not_seen_by_ws"][КАНАЛ_RABBIT]["share"] - 0.6667) < 0.001,
        д5["not_seen_by_ws"])
    chk("сам Helius в эту долю не считается",
        КАНАЛ_WS not in д5["not_seen_by_ws"], д5["not_seen_by_ws"])

    print(f"самопроверка доклада по гонке: {всего[1]}/{всего[0]}"
           f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    if всего[1] != всего[0]:
        raise SystemExit(1)


def человеку(д: dict) -> str:
    """Доклад строками -- то, что уходит владельцу."""
    строки = [f"записей журнала {д['records']}, запусков службы {д['segments']}, "
               f"подписей {д['events']}",
               "сообщения по каналам: "
               + ", ".join(f"{к} {n}" for к, n in sorted(д["messages"].items()))]
    def блок(заголовок, пары):
        строки.append(заголовок)
        for имя, ч in sorted(пары.items()):
            if имя == "messages" or not isinstance(ч, dict) or "pairs" not in ч:
                continue
            a = имя.split("_vs_")[0]
            строки.append(
                f"  {имя}: пар {ч['pairs']}, первым {a} в "
                f"{ч['first_share'] * 100:.2f} %, медиана {ч['median_ms']} мс, "
                f"p10 {ч['p10_ms']}, p90 {ч['p90_ms']}, p95 {ч['p95_ms']}, "
                f"p99 {ч['p99_ms']}")
    блок("все подписи:", д["pairwise"])
    for г, часть in д["by_group"].items():
        блок(f"группа {г}:", часть)
    к = д.get("combined") or {}
    if к.get("best_helper"):
        строки.append(f"совместная гонка: Helius + {к['best_helper']}")
        for имя, с in sorted((к.get("all") or {}).items()):
            if "gain_median_ms" in с:
                строки.append(
                    f"  + {имя}: общих подписей {с['both']}, помощник первым в "
                    f"{с['helper_first_share'] * 100:.2f} %, выигрыш медиана "
                    f"{с['gain_median_ms']} мс, p90 {с['gain_p90_ms']}, "
                    f"p95 {с['gain_p95_ms']}; видел один помощник: "
                    f"{с['only_helper_saw']}, видел один Helius: "
                    f"{с['only_base_saw']}")
    нв = д.get("not_seen_by_ws") or {}
    if нв:
        строки.append("не дошло до Helius WS (упало или не вошло в блок):")
        for к, ч in sorted(нв.items()):
            доля = ("?" if ч["share"] is None else f"{ч['share'] * 100:.2f} %")
            строки.append(f"  {к}: {ч['not_in_ws']} из {ч['messages']} ({доля})")
    if д.get("disconnects"):
        строки.append("обрывы: " + ", ".join(
            f"{к} {v['count']} ({v['last'][:60]})"
            for к, v in sorted(д["disconnects"].items())))
    else:
        строки.append("обрывов в журнале нет")
    return "\n".join(строки)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--journal", action="append", default=[],
                    help="файл feed_race.jsonl (можно несколько раз)")
    p.add_argument("--boosters", default="",
                    help="разгонные адреса через запятую (для группы у WS)")
    p.add_argument("--channels",
                    default=f"{КАНАЛ_WS},{КАНАЛ_GRPC},{КАНАЛ_GRPC2},"
                             f"{КАНАЛ_RABBIT},{КАНАЛ_RABBIT2}")
    p.add_argument("--out", default="")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return 0
    if not a.journal:
        print("нечего разбирать: задайте --journal", file=sys.stderr)
        return 2
    записи = читать(a.journal)
    каналы = [x.strip() for x in a.channels.split(",") if x.strip()]
    разгонные = {x.strip() for x in a.boosters.split(",") if x.strip()}
    д = доклад(записи, разгонные, каналы)
    print(человеку(д))
    if a.out:
        Path(a.out).write_text(json.dumps(д, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        print(f"числа записаны: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
