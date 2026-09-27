#!/usr/bin/env python3
"""Состояние кошельков перед переездом полосы. Только чтение цепи.

ЗАЧЕМ. Владелец 27.09 (переезд полосы на новый кошелёк): шаги 2-4 -- продать
остатки, закрыть все токен-счета, перевести полномочия nonce, перевести SOL.
Каждый из них -- деньги, и делать их надо по числам, а не на память. Здесь
собираются числа: баланс обоих кошельков, все токен-счета старого (с остатком и
рентой), состояние счёта nonce (владелец, рента, сохранённый хеш).

Ни одной отправки и ни одной подписи. Ключ здесь не читается вовсе.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ЛАМПОРТОВ_В_SOL = 1_000_000_000
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
WSOL = "So11111111111111111111111111111111111111112"
СИСТЕМНАЯ = "11111111111111111111111111111111"


def узел() -> str:
    ключ = (os.environ.get("HELIUS_API") or os.environ.get("HELIUS_API_KEY") or "").strip()
    if not ключ:
        raise SystemExit("СБОЙ: HELIUS_API не задан")
    return f"https://mainnet.helius-rpc.com/?api-key={ключ}"


def rpc(метод: str, параметры: list, *, повторов: int = 4):
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                       "params": параметры}).encode()
    задержка = 0.4
    последняя = None
    for _ in range(повторов):
        req = urllib.request.Request(узел(), data=тело,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                о = json.loads(r.read().decode())
            if "error" in о:
                последняя = str(о["error"])[:200]
            else:
                return о.get("result")
        except Exception as exc:  # noqa: BLE001
            последняя = f"{type(exc).__name__}"
        time.sleep(задержка)
        задержка *= 2
    raise RuntimeError(f"{метод} не ответил: {последняя}")


def баланс(адрес: str) -> dict:
    о = rpc("getBalance", [адрес]) or {}
    лам = о.get("value") if isinstance(о, dict) else о
    return {"лампорты": лам, "SOL": (лам / ЛАМПОРТОВ_В_SOL) if лам is not None else None}


def токен_счета(владелец: str) -> dict:
    строки = []
    for программа in (TOKEN, TOKEN22):
        о = rpc("getTokenAccountsByOwner",
                [владелец, {"programId": программа},
                 {"encoding": "jsonParsed"}]) or {}
        for з in (о.get("value") or []):
            инфо = (((з.get("account") or {}).get("data") or {})
                    .get("parsed") or {}).get("info") or {}
            сумма = (инфо.get("tokenAmount") or {})
            строки.append({
                "счёт": з.get("pubkey"),
                "программа": программа,
                "минт": инфо.get("mint"),
                "сырой_остаток": сумма.get("amount"),
                "остаток": сумма.get("uiAmount"),
                "рента_лампорты": (з.get("account") or {}).get("lamports"),
                "wsol": инфо.get("mint") == WSOL})
    пустых = [с for с in строки if str(с["сырой_остаток"] or "0") == "0"]
    с_остатком = [с for с in строки if str(с["сырой_остаток"] or "0") != "0"]
    рента = sum(int(с["рента_лампорты"] or 0) for с in строки)
    return {"всего": len(строки), "пустых": len(пустых),
            "с_остатком": len(с_остатком),
            "рента_всего_лампорты": рента,
            "рента_всего_SOL": рента / ЛАМПОРТОВ_В_SOL,
            "счета": строки}


def счёт_nonce(адрес: str) -> dict:
    """Владелец, рента и сохранённый хеш счёта nonce. По цепи, а не из env."""
    из_ = {"адрес": адрес, "ok": False, "почему": None}
    if not адрес or len(адрес) < 32:
        из_["почему"] = (f"адрес счёта nonce задан не полностью ({адрес!r}) -- "
                          "по огрызку читать нельзя, нужен полный адрес")
        return из_
    о = rpc("getAccountInfo", [адрес, {"encoding": "jsonParsed"}]) or {}
    зн = о.get("value")
    if not зн:
        из_["почему"] = "счёта нет в цепи"
        return из_
    разбор = ((зн.get("data") or {}).get("parsed") or {}).get("info") or {}
    из_.update(ok=True, владелец_программа=зн.get("owner"),
               рента_лампорты=зн.get("lamports"),
               рента_SOL=(зн.get("lamports") or 0) / ЛАМПОРТОВ_В_SOL,
               полномочия=разбор.get("authority"),
               хеш=разбор.get("blockhash"),
               тип=((зн.get("data") or {}).get("parsed") or {}).get("type"))
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--out", required=True, help="файл отчёта: дополняется")
    р.add_argument("--staryy", required=True)
    р.add_argument("--nonce", default=None)
    а = р.parse_args()

    try:
        with open(а.out, encoding="utf-8") as ф:
            итог = json.load(ф)
    except Exception:  # noqa: BLE001
        итог = {}
    новый = итог.get("адрес")
    итог["снято_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    итог["старый_кошелёк"] = а.staryy

    if новый:
        итог["новый_баланс"] = баланс(новый)
        итог["новый_токен_счета"] = токен_счета(новый)
        # Есть ли у нового адреса вообще история: пустой и не существующий в
        # цепи адрес -- это нормально для свежего кошелька, и сказать это надо
        # словами, чтобы владелец не искал ошибку там, где её нет.
        подписи = rpc("getSignaturesForAddress", [новый, {"limit": 3}]) or []
        итог["новый_подписей_последних"] = len(подписи)
    else:
        итог["новый_почему"] = "адреса нового кошелька в отчёте нет -- читать нечего"

    итог["старый_баланс"] = баланс(а.staryy)
    итог["старый_токен_счета"] = токен_счета(а.staryy)
    if а.nonce:
        итог["nonce"] = счёт_nonce(а.nonce)

    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False, indent=1)
    кратко = {
        "новый_адрес": итог.get("адрес"),
        "новый_SOL": (итог.get("новый_баланс") or {}).get("SOL"),
        "новый_токен_счетов": (итог.get("новый_токен_счета") or {}).get("всего"),
        "старый_SOL": (итог.get("старый_баланс") or {}).get("SOL"),
        "старый_токен_счетов": (итог.get("старый_токен_счета") or {}).get("всего"),
        "старый_с_остатком": (итог.get("старый_токен_счета") or {}).get("с_остатком"),
        "старый_рента_SOL": (итог.get("старый_токен_счета") or {}).get("рента_всего_SOL"),
        "nonce": {k: v for k, v in (итог.get("nonce") or {}).items()
                   if k in ("адрес", "ok", "почему", "полномочия", "рента_SOL")}}
    print(json.dumps(кратко, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
