#!/usr/bin/env python3
"""Разбор транзакций лидера до инструкций: кто посредник и чем привязать событие к его кошельку. Только чтение.

Зачем (02.10): по цепи у лидера Beqv6dzT 76 подписей, а в суточном архиве PumpApi его событий ноль. Архив
привязывает событие к кошельку по txSigner или breakdown[].trader; если торговля идёт через посредника, кошелёк
в этих полях не стоит. Здесь по каждой подписи печатается:
  * подписанты, плательщик, программы верхнего уровня и вложенных инструкций (по порядку, с именами известных);
  * pre/post балансы токенов с ВЛАДЕЛЬЦЕМ счёта -- видно, у кого на самом деле менялся токен и SOL;
  * изменение лампортов по счетам, где владелец -- наш адрес;
  * вывод: стоит ли адрес в подписантах, и стоит ли он владельцем в pre/postTokenBalances.
Выход: data/podbivka/lider_tx/<подпись[:12]>.json (полное тело) и data/podbivka/lider_tx/razbor.json (сводка).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ПТ = П / "lider_tx"


def имена() -> dict:
    import podbivka_lider_po_cepi as L  # noqa: PLC0415
    return L.ИМЕНА


def инструкции(т: dict, имя: dict) -> dict:
    с = (т or {}).get("transaction") or {}
    сообщ = с.get("message") or {}
    ключи = [k if isinstance(k, str) else (k or {}).get("pubkey") for k in (сообщ.get("accountKeys") or [])]

    def прог(и: dict):
        if и.get("programId"):
            return и["programId"]
        к = и.get("programIdIndex")
        return ключи[к] if (к is not None and к < len(ключи)) else None

    верх = []
    for n, и in enumerate(сообщ.get("instructions") or []):
        p = прог(и) if isinstance(и, dict) else None
        верх.append({"n": n, "программа": p, "имя": имя.get(p), "тип": (и or {}).get("parsed", {}).get("type")
                     if isinstance((и or {}).get("parsed"), dict) else None,
                     "счётов": len((и or {}).get("accounts") or [])})
    вложенные = []
    for гр in ((т.get("meta") or {}).get("innerInstructions") or []):
        for и in гр.get("instructions") or []:
            p = прог(и) if isinstance(и, dict) else None
            вложенные.append({"над": гр.get("index"), "программа": p, "имя": имя.get(p)})
    return {"ключей": len(ключи), "подписантов": (сообщ.get("header") or {}).get("numRequiredSignatures"),
            "плательщик": ключи[0] if ключи else None, "верхние": верх, "вложенные": вложенные}


def балансы(т: dict, адрес: str) -> dict:
    м = (т or {}).get("meta") or {}
    пре = {(b.get("accountIndex"), b.get("mint")): b for b in (м.get("preTokenBalances") or [])}
    пост = {(b.get("accountIndex"), b.get("mint")): b for b in (м.get("postTokenBalances") or [])}
    строки = []
    for к in sorted(set(пре) | set(пост), key=lambda x: (x[0] or 0)):
        a, b = пре.get(к), пост.get(к)
        вл = (b or a or {}).get("owner")
        до = float(((a or {}).get("uiTokenAmount") or {}).get("uiAmountString") or 0)
        после = float(((b or {}).get("uiTokenAmount") or {}).get("uiAmountString") or 0)
        строки.append({"индекс": к[0], "mint": к[1], "владелец": вл, "до": до, "после": после,
                       "дельта": round(после - до, 9), "наш": вл == адрес})
    с = (т or {}).get("transaction") or {}
    ключи = [k if isinstance(k, str) else (k or {}).get("pubkey") for k in ((с.get("message") or {}).get("accountKeys") or [])]
    лам = []
    for и, k in enumerate(ключи):
        до, после = (м.get("preBalances") or [None] * len(ключи))[и], (м.get("postBalances") or [None] * len(ключи))[и]
        if до is not None and после is not None and до != после:
            лам.append({"счёт": k, "дельта_sol": round((после - до) / 1e9, 9), "наш": k == адрес})
    return {"токены": строки, "лампорты": лам,
            "наш_владелец_токенсчёта": sorted({x["mint"] for x in строки if x["наш"]}),
            "наш_в_лампортах": any(x["наш"] for x in лам)}


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--adres", required=True)
    р.add_argument("--podpisi", default="lider_tx_podpisi.json", help="json со списком подписей в data/podbivka")
    а = р.parse_args()
    сп = json.loads((П / а.podpisi).read_text(encoding="utf-8"))
    имя = имена()
    уз = S.Узел()
    ПТ.mkdir(parents=True, exist_ok=True)
    разбор = []
    with уз.на("helius"):
        for п in сп:
            т = уз.вызов("getTransaction", [п, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                "maxSupportedTransactionVersion": 0}], срок=60.0)
            сырое = уз.вызов("getTransaction", [п, {"encoding": "base64", "commitment": "confirmed",
                                                    "maxSupportedTransactionVersion": 0}], срок=60.0)
            if not т:
                разбор.append({"signature": п, "why_not": "узел не отдал транзакцию"})
                continue
            (ПТ / f"{п[:12]}.json").write_text(json.dumps({"signature": п, "jsonParsed": т, "base64": сырое},
                                                          ensure_ascii=False, indent=1), encoding="utf-8")
            R.записано(ПТ / f"{п[:12]}.json")
            и = инструкции(т, имя)
            б = балансы(т, а.adres)
            подписанты = [k.get("pubkey") for k in ((т.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
                          if isinstance(k, dict) and k.get("signer")]
            разбор.append({"signature": п, "slot": т.get("slot"), "blockTime": т.get("blockTime"),
                           "подписанты": подписанты, "наш_подписант": а.adres in подписанты,
                           "плательщик": и["плательщик"], "верхние": и["верхние"], "вложенные_программы":
                           sorted({(x["имя"] or (x["программа"] or "")[:8]) for x in и["вложенные"]}),
                           "наш_владелец_токенсчёта": б["наш_владелец_токенсчёта"],
                           "наш_в_лампортах": б["наш_в_лампортах"],
                           "токены_наши": [x for x in б["токены"] if x["наш"]],
                           "лампорты_наши": [x for x in б["лампорты"] if x["наш"]],
                           "токены_всего_счётов": len(б["токены"]), "владельцы": sorted({x["владелец"] for x in б["токены"] if x["владелец"]})})
    out = ПТ / "razbor.json"
    out.write_text(json.dumps({"адрес": а.adres, "разбор": разбор, "расход": уз.расход()},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    for x in разбор:
        print(f"{x['signature'][:12]}: наш подписант {x.get('наш_подписант')}, владелец токен-счёта по минтам "
              f"{x.get('наш_владелец_токенсчёта')}, в лампортах {x.get('наш_в_лампортах')}, "
              f"верхние {[ (y['имя'] or (y['программа'] or '')[:8]) for y in x.get('верхние') or [] ]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
