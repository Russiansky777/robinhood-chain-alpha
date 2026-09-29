#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""СУТОЧНЫЙ СЧЁТ ПОЛОСЫ до позиции: чем именно набран убыток и что на цепи.

Слово владельца 29.09 (п.А): стоп 00:49:48Z "суточный убыток -2.189326375 при
пороге -1.5" -- подтвердить, что это перезапуск сторожа, а не новая сделка, и
разложить число по сделкам рядом с потерей по цепи.

Считает ТОЙ ЖЕ формулой, что и стоп (bloom_exec_state.итог_позиции), позиция за
позицией, и рядом ставит цепные поля записи. Плюс последние подписи кошелька
полосы с цепи: видно, была ли после остановки хоть одна сделка.

Только чтение: журнал позиций потоком, getSignaturesForAddress, getTransaction.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
import time

HELIUS = "https://mainnet.helius-rpc.com"
ПУЛЫ = ("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
         "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
         "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
         "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
         "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",
         "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")


def зов(метод, параметры, *, таймаут=25.0):
    import urllib.request  # noqa: PLC0415

    к = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    зпр = urllib.request.Request(f"{HELIUS}/?api-key={к}", data=тело,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
            о = json.loads(отв.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:140]}"}
    if "error" in о:
        return {"ok": False, "why_not": f"RPC: {str(о['error'])[:160]}"}
    return {"ok": True, "result": о.get("result")}


def строки(путь):
    открыть = gzip.open if путь.endswith(".gz") else open
    try:
        ф = открыть(путь, "rt", encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return
    with ф:
        for с in ф:
            с = с.strip()
            if not с:
                continue
            try:
                yield json.loads(с)
            except ValueError:
                continue


def позиции(state_dir):
    видели = {}
    for п in sorted(glob.glob(os.path.join(state_dir, "positions*.jsonl*"))):
        for з in строки(п):
            cid = з.get("client_order_id") or з.get("cid")
            if not cid:
                continue
            б = видели.get(cid) or {}
            б.update({к: v for к, v in з.items() if v is not None})
            видели[cid] = б
    return видели


def utc(т):
    if not isinstance(т, (int, float)) or т <= 0:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(т)))


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--kowelek", default="")
    р.add_argument("--podpisej", type=int, default=12)
    р.add_argument("--out", default="")
    а = р.parse_args()

    for пт in (os.environ.get("BLOOM_CODE_DIR") or "", "/home/bot/bloom_executor",
                os.path.dirname(os.path.abspath(__file__))):
        if пт and os.path.isdir(пт) and пт not in sys.path:
            sys.path.insert(0, пт)
    import bloom_exec_state as ST  # noqa: PLC0415
    import bloom_own_send as OSW  # noqa: PLC0415

    кошелёк = (а.kowelek or os.environ.get("OWN_SEND_WALLET")
                or os.environ.get("BLOOM_LANE_WALLET") or "")
    состояние = ST.ExecState()
    с = OSW.состояние_полосы(состояние)
    сут = OSW.начало_суток()
    итог = {"порог_суток_sol": float(OSW.СТОП_ПОЛОСЫ_СУТКИ_SOL),
             "день": с.get("day"), "начало_суток_utc": utc(сут.get("ts")),
             "как_считаются_сутки": сут.get("how"),
             "pnl_all_groups_sol": с.get("pnl_all_groups_sol"),
             "pnl_sol": с.get("pnl_sol"), "today": с.get("today"),
             "uncountable_today": с.get("uncountable_today"),
             "no_result_today": с.get("no_result_today"),
             "spend_sol": с.get("spend_sol"), "кошелёк": кошелёк, "сделки": []}
    print(f"ПОРОГ СУТОК: -{итог['порог_суток_sol']} SOL | день {итог['день']} "
          f"(начало {итог['начало_суток_utc']}, {итог['как_считаются_сутки']})")
    print(f"СЧЁТ СЛУЖБЫ: pnl_all_groups {итог['pnl_all_groups_sol']} | "
          f"сделок {итог['today']} | несчитаемых {итог['uncountable_today']} | "
          f"без итога {итог['no_result_today']} | расход {итог['spend_sol']}")

    начало = float(сут.get("ts") or 0)
    все = позиции(а.state_dir)
    сумма_службы = 0.0
    сумма_цепи = 0.0
    for cid, п in sorted(все.items(), key=lambda кв: float(кв[1].get("ts_intent") or 0)):
        if (п.get("lane") or "") != "own_send":
            continue
        ти = п.get("ts_intent")
        if not isinstance(ти, (int, float)) or float(ти) < начало:
            continue
        итог_p, расход_p = ST.итог_позиции(п)
        натив = п.get("lane_buy_native_sol")
        возврат = п.get("closed_sol_net")
        цепь = None
        if натив is not None:
            цепь = round(float(натив) + float(возврат or 0.0), 9)
        засчитано = ("по цепи (есть возврат)" if возврат is not None
                      else ("полная потеря: закрыта без продажи"
                             if итог_p is not None else "итог неизвестен"))
        строка = {"cid": cid, "минт": п.get("mint"), "группа": п.get("lane_group"),
                   "куплено_utc": utc(п.get("ts_sent") or ти),
                   "вход_sol": п.get("sol_in"),
                   "натив_покупки_sol": натив, "возврат_sol": возврат,
                   "итог_службы_sol": (round(итог_p, 9) if итог_p is not None else None),
                   "расход_sol": round(расход_p, 9),
                   "итог_по_нативу_sol": цепь, "засчитано": засчитано,
                   "состояние": п.get("state"),
                   "причина": str(п.get("closed_reason") or "")[:90]}
        итог["сделки"].append(строка)
        if итог_p is not None:
            сумма_службы += float(итог_p)
        else:
            сумма_службы -= float(расход_p)
        if цепь is not None:
            сумма_цепи += float(цепь)
        print(f"  {строка['куплено_utc']} {str(строка['минт'])[:8]:9s} "
              f"{str(строка['группа']):11s} вход {строка['вход_sol']} "
              f"| служба {строка['итог_службы_sol']} | натив {натив} "
              f"возврат {возврат} -> цепь {цепь} | {засчитано}")
    итог["сумма_службы_sol"] = round(сумма_службы, 9)
    итог["сумма_по_нативу_sol"] = round(сумма_цепи, 9)
    print(f"СУММА ПО ФОРМУЛЕ СТОПА: {итог['сумма_службы_sol']} | "
          f"СУММА ПО НАТИВУ ЗАПИСЕЙ: {итог['сумма_по_нативу_sol']}")

    # --- ПОСЛЕДНИЕ ПОДПИСИ КОШЕЛЬКА: была ли сделка после остановки
    if кошелёк:
        о = зов("getSignaturesForAddress", [кошелёк, {"limit": int(а.podpisej)}])
        подписи = []
        if о.get("ok"):
            for з in (о.get("result") or []):
                п_ = з.get("signature")
                вид = "не смотрели"
                тx = зов("getTransaction", [п_, {"encoding": "jsonParsed",
                                                  "maxSupportedTransactionVersion": 1}])
                if тx.get("ok") and тx.get("result"):
                    текст = json.dumps(тx["result"], ensure_ascii=False)
                    пулы = [pp for pp in ПУЛЫ if pp in текст]
                    вид = ("СДЕЛКА через " + ",".join(x[:6] for x in пулы)) if пулы \
                        else ("закрытие счёта" if "CloseAccount" in текст
                               else "прочее")
                подписи.append({"подпись": п_, "utc": utc(з.get("blockTime")),
                                 "err": з.get("err"), "вид": вид})
        else:
            подписи = [{"why_not": о.get("why_not")}]
        итог["последние_подписи"] = подписи
        print("ПОСЛЕДНИЕ ПОДПИСИ КОШЕЛЬКА:")
        for з in подписи:
            print(f"  {з.get('utc')} {str(з.get('подпись'))[:16]} {з.get('вид')} "
                  f"err={з.get('err')} {з.get('why_not') or ''}")

    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
        print("записано:", а.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
