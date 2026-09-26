#!/usr/bin/env python3
"""Дневные счётчики полосы и разбор одной сделки площадки -- потоком, по журналу.

ЗАЧЕМ (пункты 3 и 4 дневного плана владельца 26.09):
  4) цепочка чисел по полосе с 11:33Z: сигналов -> отказ на сборке (по
     причинам) -> отправлено -> село -> с местом по цепи. В таблице полосы
     видно 14 покупок, а суточные счётчики показывают 82 попытки -- нужна вся
     цепочка, чтобы понять, где числа расходятся;
  3) по одной сделке площадки: возраст сигнала при решении В СЛОТАХ (slot_lag),
     возраст сети в секундах и время ответа Bloom. Если возраст при решении был
     больше порога STALE -- фильтр не сработал, и это отдельная беда.

Журнал в память не читается: идём построчно, держим только счётчики.
Только чтение. Ничего не меняет.
"""
import argparse
import gzip
import json
import os
import sys
import time

# Порог фильтра устаревших сигналов -- тот же, что у детектора
# (BLOOM_STALE_SLOTS, по умолчанию 3).
ПОРОГ_STALE = int(os.environ.get("BLOOM_STALE_SLOTS", "3"))


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
        for ln in ф:
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


def файлы(каталог: str, имя: str) -> list:
    из_ = [os.path.join(каталог, имя)]
    из_ += sorted(os.path.join(каталог, ф) for ф in os.listdir(каталог)
                  if ф.startswith(имя + ".") and ф.endswith(".gz"))
    return [п for п in из_ if os.path.exists(п)]


def в_секунды(s: str) -> float:
    return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone


def группа_причины(почему: str) -> str:
    п = (почему or "").lower()
    if "тип пула вне полосы" in п or "тип пула не покрыт" in п:
        return "тип пула вне полосы"
    if "котировка" in п:
        return "котировка не SOL"
    if "разновидность инструкции кривой" in п:
        return "кривая: разновидность не покрыта"
    if "шаблон" in п:
        return "шаблон пула не собран"
    if "минимум" in п:
        return "минимум не выдаётся"
    if "предел полосы" in п or "gate" in п:
        return "гейт полосы (предел открытых, потолок, стоп)"
    if "рубильник" in п or "kill" in п:
        return "рубильник"
    return (почему or "без причины")[:70]


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default=os.environ.get("BLOOM_STATE_DIR")
                   or "/home/bot/bloom_executor_live_data")
    р.add_argument("--since-utc", required=True)
    р.add_argument("--sdelka-utc", default="",
                   help="время сделки площадки для разбора (пункт 3)")
    р.add_argument("--okno-s", type=float, default=90.0,
                   help="сколько секунд вокруг --sdelka-utc смотреть")
    а = р.parse_args()
    порог = в_секунды(а.since_utc)
    цель = в_секунды(а.sdelka_utc) if а.sdelka_utc else None

    полоса = {"сигналов_дошло": 0, "по_стадиям": {}, "отказов_на_сборке": 0,
              "причины": {}, "отправлено": 0}
    площадка = {"решений_buy": 0, "по_кодам": {}}
    разбор = []
    for путь in файлы(а.state_dir, "decisions.jsonl"):
        for з in строки(путь):
            т = з.get("t_decide_ts") or з.get("t_recv_ts")
            if not т:
                # у записей полосы своего времени нет -- берём ts_utc
                у = з.get("ts_utc")
                т = в_секунды(у) if isinstance(у, str) and у.endswith("Z") else None
            if т is None or float(т) < порог:
                continue
            # ЗАПИСЬ ПОЛОСЫ узнаётся по своему полю времени сборки.
            if з.get("own_send_total_ms") is not None:
                полоса["сигналов_дошло"] += 1
                ст = з.get("stage") or "?"
                полоса["по_стадиям"][ст] = полоса["по_стадиям"].get(ст, 0) + 1
                if ст == "sent" or з.get("sent"):
                    полоса["отправлено"] += 1
                elif з.get("ok") is False or з.get("why_not"):
                    полоса["отказов_на_сборке"] += 1
                    г = группа_причины(з.get("why_not"))
                    полоса["причины"][г] = полоса["причины"].get(г, 0) + 1
                continue
            if з.get("action") == "buy":
                площадка["решений_buy"] += 1
                к = з.get("code") or "?"
                площадка["по_кодам"][к] = площадка["по_кодам"].get(к, 0) + 1
            if цель is not None and abs(float(т) - цель) <= а.okno_s:
                if з.get("action") == "buy" or з.get("stage") == "exec_result":
                    разбор.append({
                        "utc": з.get("t_decide_utc") or з.get("t_recv_utc")
                               or з.get("ts_utc")
                               or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(т))),
                        "stage": з.get("stage"), "code": з.get("code"),
                        "mint": (з.get("mint") or "")[:10],
                        "source_task": з.get("source_task"),
                        "slot_lag": з.get("slot_lag"),
                        "source_slot": з.get("source_slot"),
                        "net_slot_at_decision": з.get("net_slot_at_decision"),
                        "net_slot_age_s": з.get("net_slot_age_s"),
                        "decide_latency_ms": з.get("decide_latency_ms"),
                        "exec_code": з.get("exec_code"),
                        "bloom_ms": (з.get("bloom_ms") or з.get("api_ms")
                                     or з.get("request_ms")),
                        "sell_address_kind": з.get("sell_address_kind"),
                        "exec": {к: v for к, v in (з.get("exec") or {}).items()
                                 if к in ("exec_code", "bloom_ms", "api_ms",
                                           "signature", "client_order_id",
                                           "http_ms", "elapsed_ms")},
                    })

    позиции = {"полосы_всего": 0, "село_по_цепи": 0, "с_местом_по_цепи": 0,
               "не_село": 0, "без_признака": 0}
    по_cid = {}
    for путь in файлы(а.state_dir, "positions.jsonl"):
        for з in строки(путь):
            cid = з.get("client_order_id")
            if not cid:
                continue
            по_cid.setdefault(cid, {}).update({к: v for к, v in з.items() if v is not None})
    for cid, п in по_cid.items():
        if not п.get("lane"):
            continue
        if float(п.get("ts_intent") or 0) < порог:
            continue
        позиции["полосы_всего"] += 1
        if п.get("chain_ok") is True:
            позиции["село_по_цепи"] += 1
        elif п.get("chain_ok") is False:
            позиции["не_село"] += 1
        else:
            позиции["без_признака"] += 1
        if п.get("block_index") is not None or п.get("lane_block_index") is not None:
            позиции["с_местом_по_цепи"] += 1

    итог = {"с": а.since_utc, "полоса_по_журналу_решений": полоса,
            "позиции_полосы": позиции, "площадка": площадка,
            "порог_stale_слотов": ПОРОГ_STALE}
    print(json.dumps(итог, ensure_ascii=False, indent=1))
    if цель is not None:
        print("--- разбор сделки площадки ---")
        for з in разбор:
            print(json.dumps(з, ensure_ascii=False))
        плохие = [з for з in разбор if isinstance(з.get("slot_lag"), int)
                  and з["slot_lag"] > ПОРОГ_STALE and з.get("code") == "BUY"]
        if плохие:
            print(f"ВНИМАНИЕ: покупка при отставании {плохие[0]['slot_lag']} слотов "
                  f"при пороге {ПОРОГ_STALE} -- фильтр STALE не сработал")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
