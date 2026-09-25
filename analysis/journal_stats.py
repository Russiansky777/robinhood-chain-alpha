#!/usr/bin/env python3
"""Счёт по журналам: откуда пришёл сигнал, почему нет количества, что с продажей.

Только чтение копий журналов (decisions.jsonl, positions.jsonl). Ни одного
обращения к сети, ни одной записи на хост.

ТРИ ВОПРОСА ВЛАДЕЛЬЦА 25.09 (вечер).

1. ТРАНЗАКЦИИ ВЕРСИИ 1 / getTransaction. "Какая доля сигналов за сегодня пришла
   через getTransaction, а не из сообщения; причина". В журнале это поле
   parsed_from: PARSE_VIA_MSG -- разбор из самого сообщения подписки,
   PARSE_VIA_RPC -- сообщение транзакцию не принесло и её пришлось добирать
   вызовом getTransaction (это +1-2 слота). Отдельно считается код
   "не достали" -- когда и getTransaction не отдал.

2. КОЛИЧЕСТВО ПОЛОСЫ. Сколько покупок полосы осталось без количества
   (lane_bought_raw) и что написано причиной (lane_bought_why_not, сколько
   попыток). Это тот же корень, что и п. 1: наша собственная транзакция не
   отдаётся узлом сразу после отправки.

3. ПРОДАЖА -- второй конец круга. По закрытым сделкам: от срока продажи
   (ts_sent + sell_after_s) до посадки продажи по цепи, маршрут, комиссия
   продажи из last_sell_outcome. Чего в журналах НЕТ, того здесь нет:
   проскальзывание против средней цены пула требует котировки на момент
   отправки, и она в журнале лежит только для пути Jupiter (jup_floor).
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

МЕТКА_ПОЛОСЫ = "own_send"
ИЗ_СООБЩЕНИЯ = "PARSE_VIA_MSG"
ЧЕРЕЗ_RPC = "PARSE_VIA_RPC"


def поток(путь: str):
    """Строки журнала ПО ОДНОЙ. Файл решений на хосте -- 635 МБ и 439 тысяч
    строк; read_text на нём съел память бегунка, и прогон убило OOM прямо на
    боевом хосте. Журналы читаются только потоком и только так."""
    п = Path(путь)
    if not п.exists():
        return
    with п.open(encoding="utf-8", errors="replace") as ф:
        for с in ф:
            с = с.strip()
            if not с.startswith("{"):
                continue
            try:
                yield json.loads(с)
            except ValueError:
                continue


def строки(путь: str) -> list:
    """Список -- только для маленьких журналов (позиции). Для решений
    пользоваться поток()."""
    return list(поток(путь))


def позиции(путь: str) -> dict:
    из_: dict = {}
    for з in поток(путь):
        cid = з.get("client_order_id")
        if not cid:
            continue
        из_.setdefault(cid, {}).update({к: v for к, v in з.items() if v is not None})
    return из_


def _в_окне(з: dict, с_utc: str) -> bool:
    когда = str(з.get("ts_utc") or з.get("ts_intent_utc") or "")
    return (not с_utc) or (bool(когда) and когда >= с_utc)


def откуда_сигналы(решения, с_utc: str = "") -> dict:
    """П. 1: доля разбора через getTransaction против разбора из сообщения."""
    свод = {"rows": 0, "msg": 0, "rpc": 0, "not_fetched": 0,
             "by_hour": {}, "by_source_rpc": {}, "by_group_rpc": {},
             "examples_rpc": []}
    for з in решения:
        if not _в_окне(з, с_utc):
            continue
        откуда = з.get("parsed_from")
        код = з.get("code")
        час = str(з.get("ts_utc") or "")[:13]
        if код == "NOT_FETCHED" or (код and "НЕ_ДОСТАЛИ" in str(код)):
            свод["not_fetched"] += 1
        if откуда not in (ИЗ_СООБЩЕНИЯ, ЧЕРЕЗ_RPC):
            continue
        свод["rows"] += 1
        ч = свод["by_hour"].setdefault(час, {"msg": 0, "rpc": 0})
        if откуда == ИЗ_СООБЩЕНИЯ:
            свод["msg"] += 1
            ч["msg"] += 1
        else:
            свод["rpc"] += 1
            ч["rpc"] += 1
            и = з.get("source") or "?"
            свод["by_source_rpc"][и] = свод["by_source_rpc"].get(и, 0) + 1
            г = з.get("source_group") or з.get("group") or "?"
            свод["by_group_rpc"][г] = свод["by_group_rpc"].get(г, 0) + 1
            if len(свод["examples_rpc"]) < 10:
                свод["examples_rpc"].append(
                    {"ts_utc": з.get("ts_utc"), "signature": з.get("signature"),
                     "source": и, "slot": з.get("slot"), "via": з.get("via")})
    всего = свод["rows"]
    свод["share_rpc"] = (round(свод["rpc"] / всего, 4) if всего else None)
    свод["share_msg"] = (round(свод["msg"] / всего, 4) if всего else None)
    return свод


def количество_полосы(поз: dict, с_utc: str = "") -> dict:
    """П. 2: у скольких покупок полосы нет количества и почему."""
    свод = {"lane_rows": 0, "with_amount": 0, "without_amount": 0,
             "why": {}, "tries": [], "rows": []}
    for cid, п in поз.items():
        if п.get("lane") != МЕТКА_ПОЛОСЫ or not _в_окне(п, с_utc):
            continue
        свод["lane_rows"] += 1
        кол = п.get("lane_bought_raw")
        есть = isinstance(кол, int) and кол > 0
        свод["with_amount" if есть else "without_amount"] += 1
        if not есть:
            почему = str(п.get("lane_bought_why_not") or "причина не записана")
            свод["why"][почему] = свод["why"].get(почему, 0) + 1
            попыток = п.get("lane_bought_tries")
            if isinstance(попыток, int):
                свод["tries"].append(попыток)
            свод["rows"].append({"cid": cid, "utc": п.get("ts_intent_utc"),
                                  "mint": (п.get("mint") or "")[:10],
                                  "group": п.get("lane_group"),
                                  "why_not": почему[:80],
                                  "tries": попыток,
                                  "state": п.get("state")})
    свод["share_without"] = (round(свод["without_amount"] / свод["lane_rows"], 4)
                              if свод["lane_rows"] else None)
    return свод


def продажи(поз: dict, с_utc: str = "") -> dict:
    """П. 3: второй конец круга по закрытым сделкам.

    От срока продажи до посадки: срок -- ts_sent (или ts_accepted, или
    ts_intent) плюс sell_after_s; посадка -- слот из last_sell_outcome, а во
    времени -- ts_closed, потому что слот в секунды честно не переводится без
    времени блока. Пишем обе величины и не смешиваем их.
    """
    свод = {"rows": [], "by_route": {}, "fees_sol": [], "delay_s": []}
    for cid, п in поз.items():
        if not _в_окне(п, с_utc):
            continue
        исход = п.get("last_sell_outcome") or {}
        if not (исход.get("known") and исход.get("ok")):
            continue
        основа = п.get("ts_sent") or п.get("ts_accepted") or п.get("ts_intent")
        срок = (float(основа) + float(п.get("sell_after_s") or 0.0)) if основа else None
        закрыта = п.get("ts_closed")
        попытка = п.get("ts_jup_attempt") or п.get("ts_last_sell_attempt")
        задержка = ((float(попытка) - срок) if (срок and попытка) else None)
        маршрут = (п.get("sell_address_kind") or п.get("jup_api_used")
                    or ("jupiter" if п.get("jup_attempts") else "bloom"))
        свод["by_route"][маршрут] = свод["by_route"].get(маршрут, 0) + 1
        плата = исход.get("fee_sol")
        if isinstance(плата, (int, float)):
            свод["fees_sol"].append(float(плата))
        if задержка is not None:
            свод["delay_s"].append(задержка)
        пол = п.get("jup_floor") or {}
        свод["rows"].append({
            "cid": cid, "utc": п.get("ts_intent_utc"),
            "lane": п.get("lane") or "bloom", "mint": (п.get("mint") or "")[:10],
            "route": маршрут, "attempts": п.get("jup_attempts") or п.get("sell_attempts"),
            "sell_slot": исход.get("slot"),
            "from_due_to_attempt_s": (round(задержка, 1) if задержка is not None else None),
            "closed_utc": (п.get("ts_closed_utc") or
                            (закрыта and __import__("time").strftime(
                                "%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime(float(закрыта))))),
            "fee_sol": плата,
            "sol_delta_net": исход.get("sol_delta_net"),
            "sol_delta": исход.get("sol_delta"),
            "quote_share_of_entry_pct": пол.get("quote_share_of_entry_pct"),
            "out_amount": пол.get("out_amount"),
            "uncountable": п.get("result_uncountable"),
        })
    свод["rows"].sort(key=lambda з: str(з["utc"]))
    for имя, значения in (("delay_s", свод["delay_s"]), ("fees_sol", свод["fees_sol"])):
        if значения:
            свод[f"{имя}_median"] = round(statistics.median(значения), 6)
            свод[f"{имя}_mean"] = round(statistics.fmean(значения), 6)
    return свод


def доклад(о: dict) -> str:
    и, к, п = о["signals"], о["lane_amount"], о["sells"]
    т = []
    т.append("## 1. Откуда пришёл сигнал: сообщение подписки или getTransaction\n")
    if not и["rows"]:
        т.append("Строк с полем parsed_from в окне нет: считать нечего.\n")
    else:
        т.append(f"Разобрано сигналов: {и['rows']}. Из сообщения подписки "
                 f"{и['msg']} ({(и['share_msg'] or 0) * 100:.1f} %), добором "
                 f"через getTransaction {и['rpc']} "
                 f"({(и['share_rpc'] or 0) * 100:.1f} %). "
                 f"Не достали вовсе: {и['not_fetched']}.\n")
        if и["by_source_rpc"]:
            топ = sorted(и["by_source_rpc"].items(), key=lambda x: -x[1])[:10]
            т.append("\nДобор через getTransaction по источникам: "
                     + ", ".join(f"{a[:6]}… {n}" for a, n in топ) + "\n")
        if и["by_hour"]:
            т.append("\n| час UTC | из сообщения | добором |\n|---|---|---|")
            for час in sorted(и["by_hour"]):
                ч = и["by_hour"][час]
                т.append(f"| {час} | {ч['msg']} | {ч['rpc']} |")
            т.append("")
    т.append("\n## 2. Количество, купленное полосой\n")
    т.append(f"Покупок полосы в окне: {к['lane_rows']}; с количеством "
             f"{к['with_amount']}, без количества {к['without_amount']}"
             + (f" ({(к['share_without'] or 0) * 100:.0f} %)"
                if к["share_without"] is not None else "") + ".\n")
    for почему, сколько in sorted(к["why"].items(), key=lambda x: -x[1]):
        т.append(f"* {сколько}× {почему}")
    if к["tries"]:
        т.append(f"\nПопыток добора количества: медиана "
                 f"{statistics.median(к['tries'])}, максимум {max(к['tries'])}.")
    т.append("\n## 3. Продажа: от срока до попытки, маршрут, комиссия\n")
    if not п["rows"]:
        т.append("Подтверждённых продаж в окне нет.\n")
    else:
        т.append(f"Подтверждённых продаж: {len(п['rows'])}. Маршруты: "
                 + ", ".join(f"{к_}={v}" for к_, v in sorted(п["by_route"].items()))
                 + ".\n")
        if "delay_s_median" in п:
            т.append(f"От срока продажи до попытки: медиана "
                     f"{п['delay_s_median']} с, среднее {п['delay_s_mean']} с.\n")
        if "fees_sol_median" in п:
            т.append(f"Комиссия продажи: медиана {п['fees_sol_median']} SOL, "
                     f"среднее {п['fees_sol_mean']} SOL.\n")
        т.append("\n| время покупки | чья | минт | маршрут | попыток | слот продажи | "
                 "от срока до попытки, с | комиссия SOL | итог SOL | котировка к входу, % |")
        т.append("|---|---|---|---|---|---|---|---|---|---|")
        for з in п["rows"]:
            def ч(x):
                return "—" if x is None else str(x)
            т.append(f"| {ч(з['utc'])} | {ч(з['lane'])} | {ч(з['mint'])} | "
                     f"{ч(з['route'])} | {ч(з['attempts'])} | {ч(з['sell_slot'])} | "
                     f"{ч(з['from_due_to_attempt_s'])} | {ч(з['fee_sol'])} | "
                     f"{ч(з['sol_delta_net'])} | {ч(з['quote_share_of_entry_pct'])} |")
        т.append("")
        т.append("\nЧего в этой таблице НЕТ и почему: проскальзывание против средней "
                 "цены пула в момент отправки в журнале не лежит -- там есть только "
                 "котировка Jupiter на момент подписи (столбец «котировка к входу»). "
                 "Сравнение со своей сборкой в пул считается отдельным прогоном по "
                 "цепи, а не по журналу.")
    return "\n".join(т)


def main() -> int:
    import argparse

    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--decisions", default="/tmp/night_state/decisions.jsonl")
    р.add_argument("--positions", default="/tmp/night_state/positions.jsonl")
    р.add_argument("--since", default="", help="окно по UTC, например 2026-09-25T00:00")
    р.add_argument("--out-md", default=None)
    р.add_argument("--out-json", default=None)
    а = р.parse_args()
    поз = позиции(а.positions)
    сигналы = откуда_сигналы(поток(а.decisions), а.since)
    о = {"since": а.since,
          "decisions_rows": сигналы.get("rows"), "positions": len(поз),
          "signals": сигналы,
          "lane_amount": количество_полосы(поз, а.since),
          "sells": продажи(поз, а.since)}
    т = доклад(о)
    print(f"строк решений с parsed_from: {сигналы.get('rows')}, "
           f"позиций: {len(поз)}")
    print(т)
    if а.out_md:
        Path(а.out_md).write_text(
            f"# Журналы: сигналы, количество полосы, продажа "
            f"(окно с {а.since or 'начала журнала'})\n\n" + т + "\n",
            encoding="utf-8")
        print(f"записано: {а.out_md}")
    if а.out_json:
        Path(а.out_json).write_text(json.dumps(о, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
        print(f"записано: {а.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
