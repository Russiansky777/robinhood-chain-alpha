#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""АВАРИЯ 28.09: покупки полосы есть, продаж по таймеру нет.

Что отвечает этот прогон (вопросы владельца 28.09 вечером, только чтение):
  п.2 -- по каждой позиции с 21:00Z: время покупки, токен, источник, группа,
         время ПЛАНОВОЙ продажи (ts_sent + sell_after_s), время фактического
         закрытия, кто закрыл и чем, ушло/вернулось по записи;
  п.3 и п.6 -- ПОЛНАЯ лента записей журнала позиций по каждому cid в порядке
         появления: видно, кто и когда правил позицию, в каком состоянии она
         была, дошло ли дело до симуляции своей продажи (own_sell_sim_*) и до
         Jupiter (jup_*), и какой текст причины записан;
  п.7 -- какие позиции были ОТКРЫТЫ (state в intent/bought/selling/unsold) на
         момент гейта деплоя, по тому же правилу, каким считает служба.

Журнал позиций дописывается частями: одна строка -- одна правка. Поэтому лента
правок и есть история, а склеенная позиция -- только её итог.
"""

from __future__ import annotations

import argparse
import calendar
import glob
import gzip
import json
import os
import time

# Ровно как в bloom_exec_state: позиция "в воздухе" -- только эти состояния.
СОСТОЯНИЯ_ОТКРЫТЫХ = ("intent", "bought", "selling", "unsold")
# Поля, которые в ленте интересны всегда: остальные прячем, чтобы строка читалась.
ВАЖНЫЕ = (
    "state", "closed_reason", "uncountable_why", "uncountable_shared_mint",
    "lane_group", "mint", "source", "source_slot", "sell_after_s",
    "ts_intent", "ts_sent", "ts_accepted", "ts_closed", "ts_jup_attempt",
    "lane_signature", "lane_landed_signature", "lane_not_landed",
    "lane_bought_raw", "chain_ok", "cu_limit", "program", "pool_program",
    "own_sell_sim_ok", "own_sell_sim_why_not", "own_sell_sim_units",
    "own_sell_sim_n", "sell_signature", "sell_sent_slot", "sell_address_kind",
    "jup_attempts", "jup_why_not", "two_step_sell", "two_step_sell_why_not",
    "closed_sol_net", "sol_in", "failure_reviewed", "doklad_upala_sent",
)


def разобрать_время(строка: str):
    if not строка:
        return None
    т = строка.strip().replace("Z", "")
    for вид in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return float(calendar.timegm(time.strptime(т, вид)))
        except ValueError:
            continue
    return None


def utc(т) -> str | None:
    if not isinstance(т, (int, float)) or т <= 0:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(т)))


def строки(путь: str):
    открыть = gzip.open if путь.endswith(".gz") else open
    try:
        ф = открыть(путь, "rt", encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return
    with ф:
        for стр in ф:
            стр = стр.strip()
            if not стр:
                continue
            try:
                yield json.loads(стр)
            except ValueError:
                continue


def правки(state_dir: str) -> dict:
    """cid -> список правок в порядке появления, каждая с номером строки."""
    из_: dict = {}
    for путь in sorted(glob.glob(os.path.join(state_dir, "positions*.jsonl*"))):
        н = 0
        for зап in строки(путь):
            н += 1
            cid = зап.get("client_order_id") or зап.get("cid")
            if not cid:
                continue
            из_.setdefault(cid, []).append((os.path.basename(путь), н, зап))
    return из_


def склеить(лента) -> dict:
    поз: dict = {}
    for _, _, зап in лента:
        поз.update({к: v for к, v in зап.items() if v is not None})
    return поз


def кто_закрыл(поз: dict) -> str:
    """По тексту причины -- какая именно ветка закрыла позицию."""
    п = str(поз.get("closed_reason") or "")
    if поз.get("lane_not_landed"):
        return "детектор, ветка «не села» (закрыл без продажи)"
    if "продавать нечего" in п:
        return "сторож, ветка «продавать нечего» (остаток прочитан нулём)"
    if "крошки" in п:
        return "сторож, ветка крошки"
    if поз.get("sell_signature") or поз.get("sell_address_kind"):
        return f"сторож продал ({поз.get('sell_address_kind') or 'jupiter'})"
    if поз.get("chain_ok") is False and поз.get("doklad_upala_sent"):
        return "детектор, ветка «села с ошибкой»"
    if поз.get("state") in СОСТОЯНИЯ_ОТКРЫТЫХ:
        return "никто: позиция ещё открыта"
    return f"не названо: {п[:80]}" if п else "не названо: причины в записи нет"


def главное(state_dir: str, *, с: float, лента_для: int) -> dict:
    все = правки(state_dir)
    из_ = {"позиций_в_окне": 0, "таблица": [], "ленты": {}, "открытые_сейчас": []}
    отобранные = []
    for cid, лента in все.items():
        поз = склеить(лента)
        if (поз.get("lane") or "") != "own_send":
            continue
        т = поз.get("ts_intent")
        if not isinstance(т, (int, float)) or float(т) < с:
            continue
        отобранные.append((float(т), cid, лента, поз))
    отобранные.sort()
    из_["позиций_в_окне"] = len(отобранные)

    for т, cid, лента, поз in отобранные:
        задержка = поз.get("sell_after_s")
        отправлено = поз.get("ts_sent")
        план = ((float(отправлено) + float(задержка))
                 if isinstance(отправлено, (int, float))
                 and isinstance(задержка, (int, float)) else None)
        закрыто = поз.get("ts_closed")
        опоздание = ((float(закрыто) - план) if план is not None
                      and isinstance(закрыто, (int, float)) else None)
        из_["таблица"].append({
            "cid": cid, "минт": поз.get("mint"), "источник": поз.get("source"),
            "группа": поз.get("lane_group"), "строитель": поз.get("program"),
            "куплено_utc": utc(отправлено or т),
            "слот_источника": поз.get("source_slot"),
            "держать_с": задержка,
            "план_продажи_utc": utc(план),
            "закрыто_utc": utc(закрыто),
            "опоздание_с": (round(опоздание, 1) if опоздание is not None else None),
            "состояние": поз.get("state"),
            "кто_закрыл": кто_закрыл(поз),
            "причина": str(поз.get("closed_reason") or "")[:160],
            "не_села": поз.get("lane_not_landed"),
            "цепь_ок": поз.get("chain_ok"),
            "куплено_raw": поз.get("lane_bought_raw"),
            "подпись_покупки": (поз.get("lane_landed_signature")
                                 or поз.get("lane_signature")),
            "подпись_продажи": поз.get("sell_signature"),
            "sol_in": поз.get("sol_in"),
            "закрыто_sol_net": поз.get("closed_sol_net"),
            "симуляция_ok": поз.get("own_sell_sim_ok"),
            "симуляция_почему": поз.get("own_sell_sim_why_not"),
            "симуляция_n": поз.get("own_sell_sim_n"),
            "попыток_jupiter": поз.get("jup_attempts"),
            "jupiter_почему": поз.get("jup_why_not"),
        })
        if поз.get("state") in СОСТОЯНИЯ_ОТКРЫТЫХ:
            из_["открытые_сейчас"].append({"cid": cid, "минт": поз.get("mint"),
                                            "состояние": поз.get("state")})

    # ЛЕНТЫ -- по последним N позициям окна: каждая правка отдельной строкой.
    for _, cid, лента, _ in отобранные[-лента_для:]:
        шаги = []
        for файл, н, зап in лента:
            шаг = {"файл": файл, "строка": н}
            for к in ВАЖНЫЕ:
                if к in зап and зап[к] is not None:
                    шаг[к] = зап[к]
            for к in ("ts_intent", "ts_sent", "ts_closed", "ts_accepted",
                       "ts_jup_attempt"):
                if isinstance(зап.get(к), (int, float)):
                    шаг[к + "_utc"] = utc(зап[к])
            шаги.append(шаг)
        из_["ленты"][cid] = шаги
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--s", default="2026-09-28T21:00:00Z")
    р.add_argument("--lent", type=int, default=8,
                    help="по скольким последним позициям печатать полную ленту")
    р.add_argument("--out", default="")
    а = р.parse_args()
    с = разобрать_время(а.s) or 0.0
    итог = главное(а.state_dir, с=с, лента_для=а.lent)
    итог["с"] = а.s
    итог["по"] = utc(time.time())
    итог["журналы_в_каталоге"] = sorted(
        os.path.basename(x) for x in glob.glob(os.path.join(а.state_dir, "*.jsonl*")))

    print(f"ПОЗИЦИЙ ПОЛОСЫ С {а.s}: {итог['позиций_в_окне']}")
    print("журналы:", ", ".join(итог["журналы_в_каталоге"]))
    for з in итог["таблица"]:
        print(f"{з['куплено_utc']} {(з['минт'] or '')[:8]:9s} "
              f"{(з['источник'] or '?')[:8]:9s} {(з['группа'] or '?'):11s} "
              f"план {з['план_продажи_utc']} закрыто {з['закрыто_utc']} "
              f"опоздание {з['опоздание_с']} с | {з['кто_закрыл']}")
        if з["причина"]:
            print(f"      причина: {з['причина']}")
        print(f"      симуляция: ok={з['симуляция_ok']} n={з['симуляция_n']} "
              f"почему={з['симуляция_почему']} | jupiter попыток="
              f"{з['попыток_jupiter']} почему={з['jupiter_почему']}")
    print(f"ОТКРЫТЫХ СЕЙЧАС (по правилу службы): {len(итог['открытые_сейчас'])} "
          f"{json.dumps(итог['открытые_сейчас'], ensure_ascii=False)[:300]}")
    for cid, шаги in итог["ленты"].items():
        print(f"--- лента {cid} ({len(шаги)} правок)")
        for ш in шаги:
            print("   " + json.dumps(ш, ensure_ascii=False)[:400])
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
        print("записано:", а.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
