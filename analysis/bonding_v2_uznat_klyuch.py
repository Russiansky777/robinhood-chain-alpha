#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Спросить у программы кривой правильный связанный накопитель. Без отправки.

ПОЧЕМУ ТАК. У 27-счётной покупки кривой (BuyExactQuoteInV2) место 21 --
associated_user_volume_accumulator. Семена его вывести нечем: IDL программы на
цепи не выложен, а перебор строк по трём фактам цепи (кошелёк+котировка ->
ключ) не дал ни одного совпадения. Зато программа САМА печатает ожидаемый
адрес в ошибке ConstraintSeeds. Значит порядок такой:

  1. собрать НАШУ покупку из живой сделки источника, поставив на место 21 его
     ключ (заведомо чужой) -- только в симуляцию, без подписи и отправки;
  2. прочитать из логов "Right:" -- это наш правильный ключ;
  3. записать его в состояние службы (файл связанных накопителей);
  4. собрать заново с правильным ключом и снова симулировать: ошибки
     ConstraintSeeds по этому счёту больше быть не должно. Если появилась
     другая ошибка -- назвать её как есть.

Ключ кошелька не нужен: simulateTransaction идёт с sigVerify=false.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent
if str(КОРЕНЬ) not in sys.path:
    sys.path.insert(0, str(КОРЕНЬ))

import c2_swap_build as SB  # noqa: E402

HELIUS = "https://mainnet.helius-rpc.com"


def урл() -> str:
    к = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    return f"{HELIUS}/?api-key={к}"


def зов(метод: str, параметры, *, таймаут: float = 60.0) -> dict:
    import urllib.request  # noqa: PLC0415

    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    try:
        зпр = urllib.request.Request(урл(), data=тело,
                                      headers={"content-type": "application/json"})
        with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
            д = json.loads(отв.read())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
    if "error" in д:
        return {"ok": False, "why_not": str(д["error"])[:200]}
    return {"ok": True, "result": д.get("result"), "why_not": None}


def симулировать(tx_base64: str) -> dict:
    о = зов("simulateTransaction",
             [tx_base64, {"sigVerify": False, "replaceRecentBlockhash": True,
                           "encoding": "base64", "commitment": "processed"}])
    if not о["ok"]:
        return {"ok": False, "why_not": о["why_not"], "логи": []}
    зн = (о["result"] or {}).get("value") or {}
    return {"ok": зн.get("err") is None, "err": зн.get("err"),
             "units": зн.get("unitsConsumed"), "логи": зн.get("logs") or []}


def транзакция(подпись: str) -> dict | None:
    о = зов("getTransaction", [подпись, {"encoding": "jsonParsed",
                                          "maxSupportedTransactionVersion": 1,
                                          "commitment": "confirmed"}])
    return о.get("result") if о.get("ok") else None


def хранилище_кривой(tx: dict) -> str | None:
    """Хранилище минта в сделке кривой -- по нему извлекается шаблон."""
    for и in (((tx or {}).get("transaction") or {}).get("message") or {}).get("instructions") or []:
        if и.get("programId") == SB.BONDING and isinstance(и.get("accounts"), list):
            сч = и["accounts"]
            # В 27-счётной раскладке базовое хранилище кривой стоит на 11 месте
            # (проверено раскладкой по цепи), в 18-счётной -- на 4.
            if len(сч) == 27:
                return сч[11]
            if len(сч) == 18:
                return сч[4]
    for г in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        for и in г.get("instructions") or []:
            if и.get("programId") == SB.BONDING and isinstance(и.get("accounts"), list):
                сч = и["accounts"]
                if len(сч) == 27:
                    return сч[11]
    return None


def один_проход(подпись: str, *, кошелёк: str, трата_лампорты: int,
                 файл: str) -> dict:
    из_ = {"подпись": подпись, "кошелёк": кошелёк, "файл": файл}
    tx = транзакция(подпись)
    if not tx:
        return {**из_, "ok": False, "why_not": "транзакции нет на цепи"}
    хран = хранилище_кривой(tx)
    if not хран:
        return {**из_, "ok": False, "why_not": "инструкции кривой в транзакции нет"}
    шаблон = SB.extract_template(tx, SB.BONDING, хран)
    if not шаблон.get("ok"):
        return {**из_, "ok": False, "why_not": шаблон.get("why_not")}
    спец = SB.spec_of(шаблон)
    ас = спец.get("assoc_uva")
    if not ас:
        return {**из_, "ok": False,
                 "why_not": f"у разновидности {шаблон['ix']} связанного накопителя нет"}
    место, место_котировки = ас
    котировка = шаблон["accounts"][место_котировки]
    чужой = шаблон["accounts"][место]
    из_.update({"разновидность": шаблон["ix"], "котировка": котировка,
                 "чужой_ключ": чужой, "место": место})

    # ШАГ 1: затравка чужим ключом -- только чтобы программа назвала свой.
    SB.assoc_uva_запомнить(кошелёк, котировка, чужой, путь=файл)
    SB.assoc_uva_забыть()
    os.environ["BLOOM_ASSOC_UVA_FILE"] = файл
    сборка = SB.build_buy(шаблон, tx, user=кошелёк, payer=кошелёк,
                           amount_in=трата_лампорты, min_out=1,
                           cu_price_micro=10_000, tip=None)
    сим1 = симулировать(сборка["tx_base64"])
    ключ = SB.assoc_uva_из_логов(сим1["логи"])
    из_["первая_симуляция"] = {"ok": сим1["ok"], "err": сим1.get("err"),
                                "units": сим1.get("units"),
                                "логи_хвост": сим1["логи"][-8:]}
    if not ключ:
        return {**из_, "ok": False,
                 "why_not": "программа не назвала ожидаемый накопитель: "
                            f"err={сим1.get('err')}"}
    из_["ключ_от_программы"] = ключ

    # ШАГ 2: записать названный ключ и проверить второй симуляцией.
    SB.assoc_uva_запомнить(кошелёк, котировка, ключ, путь=файл)
    SB.assoc_uva_забыть()
    сборка2 = SB.build_buy(шаблон, tx, user=кошелёк, payer=кошелёк,
                            amount_in=трата_лампорты, min_out=1,
                            cu_price_micro=10_000, tip=None)
    сим2 = симулировать(сборка2["tx_base64"])
    остался = SB.assoc_uva_из_логов(сим2["логи"])
    из_["вторая_симуляция"] = {"ok": сим2["ok"], "err": сим2.get("err"),
                                "units": сим2.get("units"),
                                "логи_хвост": сим2["логи"][-8:],
                                "ошибка_накопителя_осталась": bool(остался)}
    из_["ok"] = bool(остался is None)
    из_["why_not"] = None if остался is None else "ошибка по накопителю осталась"
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--podpis", action="append", default=[],
                    help="подпись живой покупки кривой V2 (можно несколько)")
    р.add_argument("--koshelek", required=True, help="наш кошелёк полосы")
    р.add_argument("--sol", type=float, default=0.01, help="размер для симуляции")
    р.add_argument("--fajl", default=SB.ASSOC_UVA_ПО_УМОЛЧАНИЮ,
                    help="файл связанных накопителей в состоянии службы")
    р.add_argument("--out", default="")
    а = р.parse_args()

    трата = int(round(а.sol * 1_000_000_000))
    итог = {"файл": а.fajl, "проходы": []}
    for подпись in а.podpis:
        р_ = один_проход(подпись, кошелёк=а.koshelek, трата_лампорты=трата,
                          файл=а.fajl)
        итог["проходы"].append(р_)
        print(f"\n{подпись[:16]} {р_.get('разновидность')} котировка "
              f"{р_.get('котировка')}")
        print(f"  чужой ключ (из сделки источника): {р_.get('чужой_ключ')}")
        print(f"  программа назвала:               {р_.get('ключ_от_программы')}")
        пс = р_.get("первая_симуляция") or {}
        вс = р_.get("вторая_симуляция") or {}
        print(f"  первая симуляция: ok={пс.get('ok')} err={пс.get('err')}")
        print(f"  вторая симуляция: ok={вс.get('ok')} err={вс.get('err')} "
              f"ошибка_накопителя_осталась={вс.get('ошибка_накопителя_осталась')}")
        if not р_.get("ok"):
            print(f"  ОТКАЗ: {р_.get('why_not')}")
        for л in (вс.get("логи_хвост") or []):
            print(f"    {л}")
    ключи = SB.assoc_uva_все()
    итог["состояние"] = ключи
    print("\nв состоянии теперь:", json.dumps(ключи, ensure_ascii=False)[:400])
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
        print("записано:", а.out)
    return 0 if all(п.get("ok") for п in итог["проходы"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
