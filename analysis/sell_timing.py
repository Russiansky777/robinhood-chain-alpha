#!/usr/bin/env python3
"""II.9 -- ПРОДАЖА ПО ЗАКРЫТЫМ СДЕЛКАМ. Только чтение журнала позиций.

Вопрос владельца: по закрытым сделкам с 24.09 -- время от таймера до посадки,
проскальзывание, комиссии, выгода своей сборки.

ЧТО ЕСТЬ В ЖУРНАЛЕ НА КАЖДУЮ ЗАКРЫТУЮ ПОЗИЦИЮ:
  * ts_intent и sell_after_s -- когда таймер должен был сработать;
  * ts_last_sell_attempt -- когда продажа реально ушла;
  * ts_closed -- когда мы УВИДЕЛИ закрытие (не время блока, а наше);
  * last_sell_outcome -- итог по цепи: slot, sol_delta, sol_delta_net, fee_sol;
  * jup_floor.out_amount -- котировка, под которую подписывались;
  * sell_address_kind -- чья сборка продажи (jupiter или своя).

ЧЕГО НЕТ: времени блока продажи. Поэтому "попытка -> посадка" считается до
МОМЕНТА, КОГДА МЫ УВИДЕЛИ закрытие, и так и подписано; это верхняя граница, а
не время сети.

ПРОСКАЛЬЗЫВАНИЕ считается как (факт - котировка) / котировка. Котировка в
лампортах SOL лежит в jup_floor.out_amount. Сделки, где котировка меньше
порога доли от входа (по умолчанию 1 %), в проскальзывание НЕ идут: там токен
обесценился, и возврат -- это в основном возврат аренды закрытых счетов, а не
цена. Такие строки считаются отдельно и названы словами.
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
from pathlib import Path

ЛАМПОРТОВ_В_SOL = 1_000_000_000.0


def открыть(путь: Path):
    if str(путь).endswith(".gz"):
        return gzip.open(путь, "rt", encoding="utf-8", errors="replace")
    return путь.open(encoding="utf-8", errors="replace")


def позиции(пути: list) -> dict:
    """Свёрнутые позиции: строки журнала дописываются, последняя правит поля."""
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


def ряды(поз: dict, *, с_utc: str = "", доля_котировки_мин: float = 1.0) -> list:
    из_ = []
    for cid, п in поз.items():
        когда = str(п.get("ts_intent_utc") or "")
        if с_utc and когда and когда < с_utc:
            continue
        исход = п.get("last_sell_outcome") or {}
        if not исход.get("ok"):
            continue
        намерение = п.get("ts_intent")
        срок = п.get("sell_after_s")
        попытка = п.get("ts_last_sell_attempt")
        закрыто = п.get("ts_closed")
        таймер_мс = None
        if намерение and срок is not None and попытка:
            таймер_мс = (float(попытка) - float(намерение) - float(срок)) * 1000.0
        видно_мс = None
        if попытка and закрыто:
            видно_мс = (float(закрыто) - float(попытка)) * 1000.0
        пол = п.get("jup_floor") or {}
        котировка = пол.get("out_amount")
        доля = пол.get("quote_share_of_entry_pct")
        факт = исход.get("sol_delta")
        проскальзывание = None
        обесценился = (доля is not None and float(доля) < доля_котировки_мин)
        if котировка and факт and not обесценился:
            к_sol = float(котировка) / ЛАМПОРТОВ_В_SOL
            проскальзывание = (float(факт) - к_sol) / к_sol * 100.0
        из_.append({
            "cid": cid,
            "utc": когда,
            "чья": "полоса" if п.get("lane") == "own_send" else "bloom",
            "группа": п.get("lane_group") or п.get("source_task") or "",
            "сборка": п.get("sell_address_kind") or "не записана",
            "вход_sol": п.get("sol_in"),
            "таймер_мс": round(таймер_мс, 1) if таймер_мс is not None else None,
            "до_видно_мс": round(видно_мс, 1) if видно_мс is not None else None,
            "котировка_sol": (round(float(котировка) / ЛАМПОРТОВ_В_SOL, 9)
                               if котировка else None),
            "факт_sol": факт,
            "чисто_sol": исход.get("sol_delta_net"),
            "комиссия_sol": исход.get("fee_sol"),
            "проскальзывание_проц": (round(проскальзывание, 3)
                                      if проскальзывание is not None else None),
            "обесценился": обесценился,
            "доля_котировки_от_входа_проц": доля,
            "попыток_jup": п.get("jup_attempts"),
            "slot_продажи": исход.get("slot"),
        })
    return sorted(из_, key=lambda з: з["utc"])


def столбец(ряд: list, имя: str) -> dict | None:
    зн = [з[имя] for з in ряд if з.get(имя) is not None]
    if not зн:
        return None
    з = sorted(зн)
    и = min(len(з) - 1, int(round(0.9 * (len(з) - 1))))
    return {"n": len(зн), "медиана": round(statistics.median(зн), 4),
            "p90": round(з[и], 4), "мин": round(min(зн), 4),
            "макс": round(max(зн), 4)}


def свод(ряд: list) -> dict:
    из_ = {"продаж": len(ряд),
           "таймер_мс": столбец(ряд, "таймер_мс"),
           "до_видно_мс": столбец(ряд, "до_видно_мс"),
           "проскальзывание_проц": столбец(ряд, "проскальзывание_проц"),
           "комиссия_sol": столбец(ряд, "комиссия_sol"),
           "обесценившихся": sum(1 for з in ряд if з["обесценился"])}
    по_сборке: dict = {}
    for з in ряд:
        по_сборке.setdefault(з["сборка"], []).append(з)
    из_["по_сборке"] = {
        к: {"продаж": len(v),
            "проскальзывание_проц": столбец(v, "проскальзывание_проц"),
            "комиссия_sol": столбец(v, "комиссия_sol"),
            "таймер_мс": столбец(v, "таймер_мс")}
        for к, v in по_сборке.items()}
    по_чьей: dict = {}
    for з in ряд:
        по_чьей.setdefault(з["чья"], []).append(з)
    из_["по_исполнителю"] = {
        к: {"продаж": len(v),
            "проскальзывание_проц": столбец(v, "проскальзывание_проц"),
            "таймер_мс": столбец(v, "таймер_мс")}
        for к, v in по_чьей.items()}
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--positions", nargs="+", required=True)
    р.add_argument("--since", default="")
    р.add_argument("--quote-min-pct", type=float, default=1.0)
    р.add_argument("--out", default="data/sell_timing.json")
    р.add_argument("--rows", type=int, default=20)
    а = р.parse_args()

    поз = позиции(а.positions)
    ряд = ряды(поз, с_utc=а.since, доля_котировки_мин=а.quote_min_pct)
    с = свод(ряд)
    Path(а.out).parent.mkdir(parents=True, exist_ok=True)
    Path(а.out).write_text(
        json.dumps({"since": а.since, "сводка": с, "ряды": ряд},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"позиций в журналах: {len(поз)}; продаж с подтверждением по цепи: {len(ряд)}")
    print(json.dumps(с, ensure_ascii=False, indent=1))
    print()
    print("| время | чья | сборка | вход SOL | таймер, мс | до подтверждения, мс | "
          "котировка SOL | факт SOL | проскальзывание, % | комиссия SOL |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for з in ряд[-а.rows:]:
        print(f"| {з['utc'][11:19]} | {з['чья']} | {з['сборка']} | {з['вход_sol']} | "
              f"{з['таймер_мс']} | {з['до_видно_мс']} | {з['котировка_sol']} | "
              f"{з['факт_sol']} | "
              f"{'обесценился' if з['обесценился'] else з['проскальзывание_проц']} | "
              f"{з['комиссия_sol']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
