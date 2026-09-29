#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DLMM без денег: восстановить инструкцию живой покупки БАЙТ В БАЙТ и симулировать.

Слово владельца 29.09 ночью (п.3): "Проверка без денег: восстановление байт в
байт по 10 живым покупкам источников в DLMM за 28.09 (они в журнале),
simulateTransaction без подписи по трём из них -- err=None, CU. Отчёт: совпало
N из 10, CU p50/max, что не покрыто".

КАК ПРОВЕРЯЕМ. По каждой живой покупке источника в DLMM:
  1. берём шаблон инструкции из ЕГО транзакции (extract_template);
  2. пересобираем инструкцию С ЕГО ЖЕ аргументами (keep_source_ix=True) и
     сравниваем с его инструкцией -- данные байт в байт и список счетов;
  3. собираем НАШУ покупку тем же путём, что полоса, и (для первых трёх)
     просим у узла simulateTransaction без подписи: ждём err=None и CU.

Своих денег прогон не двигает и ничего не подписывает: симуляция идёт с
sigVerify=false и replaceRecentBlockhash=true.
"""

from __future__ import annotations

import argparse
import base64
import glob
import gzip
import json
import os
import sys
import time

HELIUS = "https://mainnet.helius-rpc.com"
DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"


def урл() -> str:
    к = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    return f"{HELIUS}/?api-key={к}"


def зов(метод: str, параметры, *, таймаут: float = 30.0) -> dict:
    import urllib.request  # noqa: PLC0415

    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    зпр = urllib.request.Request(урл(), data=тело,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
            о = json.loads(отв.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
    if "error" in о:
        return {"ok": False, "why_not": f"RPC: {str(о['error'])[:200]}"}
    return {"ok": True, "result": о.get("result")}


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


def подписи_dlmm(state_dir: str, *, сколько: int) -> list:
    """Подписи сделок источников, где виден пул DLMM. Журнал читается потоком."""
    найдено, видели = [], set()
    пути = sorted(glob.glob(os.path.join(state_dir, "decisions*.jsonl*")),
                   reverse=True)
    for путь in пути:
        for зап in строки(путь):
            строка = json.dumps(зап, ensure_ascii=False)
            if DLMM not in строка:
                continue
            п = зап.get("signature") or зап.get("source_sig")
            if not п or п in видели:
                continue
            видели.add(п)
            найдено.append({"подпись": п, "минт": зап.get("mint"),
                             "источник": зап.get("source"),
                             "stage": зап.get("stage")})
            if len(найдено) >= сколько:
                return найдено
    return найдено


def процентиль(ряд, доля):
    ч = sorted(x for x in ряд if isinstance(x, (int, float)))
    if not ч:
        return None
    м = min(len(ч) - 1, max(0, int(round(доля * (len(ч) - 1)))))
    return ч[м]


def разбор(подпись: str, *, кошелёк: str, лампорты: int, проскальзывание: float,
            симулировать: bool) -> dict:
    из_ = {"подпись": подпись, "байт_в_байт": None, "why_not": None}
    import c2_swap_build as B  # noqa: PLC0415

    о = зов("getTransaction", [подпись, {"encoding": "jsonParsed",
                                          "maxSupportedTransactionVersion": 1,
                                          "commitment": "finalized"}])
    if not о.get("ok") or not о.get("result"):
        из_["why_not"] = f"транзакция не прочиталась: {о.get('why_not')}"
        return из_
    tx = о["result"]
    # ХРАНИЛИЩЕ ПУЛА -- из самой транзакции: ищем инструкцию DLMM и берём её
    # счёт хранилища через mints_and_vaults по шаблону с любым счётом пула.
    инстр = [ix for ix in B.all_instructions(tx) if ix.get("programId") == DLMM]
    if not инстр:
        из_["why_not"] = "инструкции DLMM в транзакции нет"
        return из_
    счета = инстр[0]["accounts"]
    tpl = None
    for кандидат in счета:
        т = B.extract_template(tx, DLMM, кандидат)
        if т.get("ok"):
            tpl = т
            break
    if tpl is None:
        из_["why_not"] = "шаблон инструкции DLMM не восстановился"
        return из_
    из_["пул"] = (tpl.get("accounts") or [None])[0]
    из_["ix"] = tpl.get("ix")
    mv = B.mints_and_vaults(tpl, tx)
    if not mv:
        из_["why_not"] = "минты и хранилища не восстановились"
        return из_
    из_["база"] = mv.get("base_mint")
    из_["котировка"] = mv.get("quote_mint")

    # 1. БАЙТ В БАЙТ: его же аргументы, его же счета.
    try:
        а0, а1 = tpl["args"][0], tpl["args"][1]
    except Exception:  # noqa: BLE001
        а0 = а1 = None
    if а0 is None:
        из_["why_not"] = "аргументы инструкции источника не разобрались"
        return из_
    try:
        свой = B.swap_instruction(tpl, tx, tpl["accounts"][tpl.get("user_index", 0)]
                                   if tpl.get("user_index") is not None else кошелёк,
                                   а0, а1, keep_source_ix=True)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"пересборка упала: {type(exc).__name__}: {str(exc)[:140]}"
        return из_
    свои_байты = bytes(свой.data)
    его_байты = base64.b64decode(инстр[0]["data_b64"]) if инстр[0].get("data_b64") \
        else bytes(tpl["data"])
    из_["байт_в_байт"] = (свои_байты == его_байты)
    из_["длина_данных"] = len(его_байты)
    if not из_["байт_в_байт"]:
        из_["данные_его"] = его_байты.hex()[:80]
        из_["данные_наши"] = свои_байты.hex()[:80]

    # 1б. ТОЧНАЯ КОТИРОВКА и ЦЕНА ЕЁ ЧТЕНИЙ (слово владельца 29.09, п.2 к
    # докладу): два getMultipleAccounts на горячем пути -- сколько это стоит.
    try:
        import c2_dlmm_tochnaya_kotirovka as K  # noqa: PLC0415

        к = K.котировка(пул=из_["пул"], минт_базы=mv.get("base_mint"),
                         минт_котировки=mv.get("quote_mint"),
                         лампорты=лампорты, проскальзывание=проскальзывание,
                         rpc_call=lambda м, п_: зов(м, п_, таймаут=10.0))
        из_["котировка"] = {к_: к.get(к_) for к_ in
                             ("ok", "why_not", "expected_out", "min_out", "корзин",
                               "массивов", "чтений", "мс_чтение1", "мс_чтение2",
                               "мс_чтений_всего", "bin_step", "swap_for_y")}
    except Exception as exc:  # noqa: BLE001
        из_["котировка"] = {"ok": False,
                             "why_not": f"{type(exc).__name__}: {str(exc)[:140]}"}

    # 2. НАША сборка и (по просьбе) симуляция.
    сб = B.build_buy(tpl, tx, user=кошелёк, payer=кошелёк, amount_in=лампорты,
                      min_out=1, cu_price_micro=100_000, tip=None)
    из_["наша_сборка_ok"] = bool(сб) is not False
    if симулировать:
        tx64 = сб if isinstance(сб, str) else (сб or {}).get("tx_base64")
        if not tx64:
            из_["симуляция"] = {"ok": False, "why_not": "нашей транзакции нет"}
        else:
            с = зов("simulateTransaction",
                     [tx64, {"sigVerify": False, "replaceRecentBlockhash": True,
                              "encoding": "base64", "commitment": "processed"}])
            зн = ((с.get("result") or {}).get("value") or {}) if с.get("ok") else {}
            из_["симуляция"] = {"ok": с.get("ok"), "err": зн.get("err"),
                                 "cu": зн.get("unitsConsumed"),
                                 "why_not": с.get("why_not"),
                                 "логи_хвост": (зн.get("logs") or [])[-4:]}
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--kowelek", default="", help="наш кошелёк (пусто -- из окружения)")
    р.add_argument("--skolko", type=int, default=10)
    р.add_argument("--simulyacij", type=int, default=3)
    р.add_argument("--lamportov", type=int, default=10_000_000)
    р.add_argument("--proskalzyvanie", type=float, default=0.2)
    р.add_argument("--out", default="")
    а = р.parse_args()

    for путь in (os.environ.get("BLOOM_CODE_DIR") or "", "/home/bot/bloom_executor",
                  os.path.dirname(os.path.abspath(__file__))):
        if путь and os.path.isdir(путь) and путь not in sys.path:
            sys.path.insert(0, путь)

    кошелёк = (а.kowelek or os.environ.get("OWN_SEND_WALLET")
                or os.environ.get("BLOOM_LANE_WALLET") or "")
    if not кошелёк:
        print("СБОЙ: кошелёк не задан")
        return 2

    найдено = подписи_dlmm(а.state_dir, сколько=а.skolko)
    итог = {"кошелёк": кошелёк, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "найдено_подписей": len(найдено), "строки": []}
    print(f"НАЙДЕНО СДЕЛОК DLMM В ЖУРНАЛЕ: {len(найдено)}")
    симулировано = 0
    for н in найдено:
        симулировать = симулировано < а.simulyacij
        р_ = разбор(н["подпись"], кошелёк=кошелёк, лампорты=а.lamportov,
                     проскальзывание=а.proskalzyvanie, симулировать=симулировать)
        if симулировать and р_.get("симуляция") is not None:
            симулировано += 1
        р_.update(минт=н.get("минт"), источник=н.get("источник"))
        итог["строки"].append(р_)
        print(f"  {н['подпись'][:16]} байт_в_байт={р_.get('байт_в_байт')} "
              f"ix={р_.get('ix')} пул={str(р_.get('пул'))[:8]} "
              f"why_not={р_.get('why_not')}")
        к_ = р_.get("котировка") or {}
        if к_:
            print(f"      котировка: ok={к_.get('ok')} корзин={к_.get('корзин')} "
                  f"мс1={к_.get('мс_чтение1')} мс2={к_.get('мс_чтение2')} "
                  f"{к_.get('why_not') or ''}")
        с = р_.get("симуляция")
        if с:
            print(f"      симуляция: ok={с.get('ok')} err={с.get('err')} "
                  f"CU={с.get('cu')} {с.get('why_not') or ''}")

    совпало = sum(1 for с in итог["строки"] if с.get("байт_в_байт") is True)
    cu = [с["симуляция"]["cu"] for с in итог["строки"]
           if (с.get("симуляция") or {}).get("cu")]
    итог["совпало"] = совпало
    итог["из"] = len(итог["строки"])
    итог["cu_p50"] = процентиль(cu, 0.5)
    итог["cu_max"] = max(cu) if cu else None
    ч1 = [(с.get("котировка") or {}).get("мс_чтение1") for с in итог["строки"]]
    ч2 = [(с.get("котировка") or {}).get("мс_чтение2") for с in итог["строки"]]
    вс = [(с.get("котировка") or {}).get("мс_чтений_всего") for с in итог["строки"]]
    итог["время_чтений_мс"] = {
        "чтение1": {"p50": процентиль(ч1, 0.5),
                     "max": max([x for x in ч1 if isinstance(x, (int, float))] or [0]) or None},
        "чтение2": {"p50": процентиль(ч2, 0.5),
                     "max": max([x for x in ч2 if isinstance(x, (int, float))] or [0]) or None},
        "оба": {"p50": процентиль(вс, 0.5),
                 "max": max([x for x in вс if isinstance(x, (int, float))] or [0]) or None},
    }
    котировок_ок = sum(1 for с in итог["строки"]
                        if (с.get("котировка") or {}).get("ok") is True)
    итог["котировок_ок"] = котировок_ок
    п50 = итог["время_чтений_мс"]["оба"]["p50"]
    if isinstance(п50, (int, float)) and п50 > 30:
        итог["предложение_без_правки"] = (
            "Два чтения дороже 30 мс на горячем пути. Убрать можно ВТОРОЕ: "
            "подписаться заранее на счёта массивов корзин отслеживаемых пулов "
            "(тот же механизм, каким уже держится кэш шагов корзин), и тогда на "
            "пути сделки останется одно чтение пула -- или ни одного, если "
            "подписка держит и сам пул. Правку не делаю: это решение владельца "
            "про расход кредитов и про то, какие пулы держать в подписке.")
    итог["не_покрыто"] = [
        "активация пула (activation_point у Permission / CustomizablePermissionless)",
        "комиссия хоста (host fee) в котировке не моделируется",
        "налог Token-2022 по одному bps, без смены по эпохам",
        "сделки между нашим чтением пула и нашей покупкой",
    ]
    print(f"ИТОГ: совпало байт в байт {совпало} из {len(итог['строки'])}; "
          f"CU p50 {итог['cu_p50']}, max {итог['cu_max']}")
    вч = итог["время_чтений_мс"]
    print(f"КОТИРОВКА: посчиталась у {итог['котировок_ок']} из {len(итог['строки'])}")
    print(f"ВРЕМЯ ЧТЕНИЙ, мс: чтение1 p50 {вч['чтение1']['p50']} max {вч['чтение1']['max']}"
          f" | чтение2 p50 {вч['чтение2']['p50']} max {вч['чтение2']['max']}"
          f" | оба p50 {вч['оба']['p50']} max {вч['оба']['max']}")
    if итог.get("предложение_без_правки"):
        print("ПРЕДЛОЖЕНИЕ (без правки):", итог["предложение_без_правки"])
    for н in итог["не_покрыто"]:
        print("  не покрыто:", н)
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
        print("записано:", а.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
