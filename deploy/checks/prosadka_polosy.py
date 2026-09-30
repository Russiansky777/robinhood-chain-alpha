#!/usr/bin/env python3
"""Куда ушла просадка полосы: по подписям, с разбором каждого списания.

ЗАЧЕМ. Владелец 27.09, пункт 5: "Просадка полосы 0.016 за ночь без сделок: что
именно списано (nonce, рента, иное), сколько в сутки; если периодическая
прокрутка nonce -- перевести на прокрутку только после использования".

ЧТО СЧИТАЕТСЯ. Все транзакции кошелька за окно, и по каждой -- нативная дельта
кошелька и ПРИЧИНА списания по инструкциям, а не по догадке:
  nonce      -- в транзакции есть AdvanceNonceAccount (код 4 системной
                программы) и она ничего больше не делает: это прокрутка;
  рента      -- создан счёт (пришли лампорты на счёт, которого не было);
  чаевые     -- перевод на адрес из реестра отправителей;
  тариф      -- meta.fee, он есть у каждой транзакции;
  покупка/продажа -- есть инструкция DEX (по списку программ пулов);
  иное       -- всё, что не опознано; такие подписи перечисляются поштучно,
                чтобы "иное" не стало ковром, под который всё заметается.

Итог -- сколько списано всего, сколько по каждой причине, сколько прокруток
nonce и сколько они стоят в сутки при текущем ритме.

Только чтение. Ни одной отправки.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(КОРЕНЬ / "analysis"))

ЛАМПОРТОВ_В_SOL = 1_000_000_000
СИСТЕМНАЯ = "11111111111111111111111111111111"
# Программы пулов: если инструкция одной из них есть, это сделка, а не накладные.
ПРОГРАММЫ_ПУЛОВ = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",      # Pump AMM
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",       # Pump.fun кривая
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",      # Raydium CPMM
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",      # Raydium AMM v4
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",      # Raydium CLMM
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",       # Meteora DAMM v2
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",       # Meteora DBC
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",       # Meteora DLMM
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",       # Jupiter
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",       # Orca Whirlpool
}


def узел() -> str:
    к = (os.environ.get("HELIUS_API_KEY2")
         or os.environ.get("HELIUS_API")
         or os.environ.get("HELIUS_API_KEY") or "").strip()
    if not к:
        raise SystemExit("СБОЙ: HELIUS_API не задан")
    return f"https://mainnet.helius-rpc.com/?api-key={к}"


def rpc(метод: str, параметры: list, *, повторов: int = 4):
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                       "params": параметры}).encode()
    пауза, последняя = 0.4, None
    for _ in range(повторов):
        req = urllib.request.Request(узел(), data=тело,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                о = json.loads(r.read().decode())
            if "error" in о:
                последняя = str(о["error"])[:200]
            else:
                return о.get("result")
        except Exception as exc:  # noqa: BLE001
            последняя = type(exc).__name__
        time.sleep(пауза)
        пауза *= 2
    raise RuntimeError(f"{метод} не ответил: {последняя}")


def счета_чаевых() -> dict:
    из_ = {}
    try:
        д = json.loads((КОРЕНЬ / "data" / "senders.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return из_
    for имя, тело in (д.get("senders") or {}).items():
        for а in (тело.get("tip_accounts") or []):
            из_[а] = имя
    return из_


def ключи(tx: dict) -> list:
    к = ((tx or {}).get("transaction") or {}).get("message", {}).get("accountKeys", [])
    return [(x.get("pubkey") if isinstance(x, dict) else x) for x in к]


def все_инструкции(tx: dict) -> list:
    мета = (tx or {}).get("meta") or {}
    сообщ = ((tx or {}).get("transaction") or {}).get("message") or {}
    из_ = list(сообщ.get("instructions") or [])
    for гр in (мета.get("innerInstructions") or []):
        из_.extend(гр.get("instructions") or [])
    return из_


def прокрутка_нонса(tx: dict) -> bool:
    """Есть ли AdvanceNonceAccount. jsonParsed называет его advanceNonce."""
    for и in все_инструкции(tx):
        р = и.get("parsed")
        if isinstance(р, dict) and str(р.get("type", "")).lower().startswith("advancenonce"):
            return True
        # Не разобранный вид: системная программа и данные с кодом 4.
        if и.get("programId") == СИСТЕМНАЯ and not р:
            д = и.get("data") or ""
            if д in ("6Nkq6ti", "6Nkq6tj"):  # base58 от 04000000 (встречается)
                return True
    return False


def есть_пул(tx: dict) -> str | None:
    for и in все_инструкции(tx):
        п = и.get("programId")
        if п in ПРОГРАММЫ_ПУЛОВ:
            return п
    return None


def разобрать(tx: dict, кошелёк: str, метки: dict) -> dict:
    мета = (tx or {}).get("meta") or {}
    кл = ключи(tx)
    до, после = мета.get("preBalances") or [], мета.get("postBalances") or []
    из_ = {"тариф": (мета.get("fee") or 0) / ЛАМПОРТОВ_В_SOL,
            "дельта_кошелька": None, "причина": None, "получатели": [],
            "ошибка": мета.get("err")}
    if кошелёк in кл:
        и = кл.index(кошелёк)
        if и < len(до) and и < len(после):
            из_["дельта_кошелька"] = (после[и] - до[и]) / ЛАМПОРТОВ_В_SOL
    # Кому пришло, и создавался ли счёт (до было 0, а счёта раньше не было).
    созданные = 0
    for и, адрес in enumerate(кл):
        if адрес == кошелёк or и >= len(до) or и >= len(после):
            continue
        прирост = после[и] - до[и]
        if прирост > 0:
            из_["получатели"].append({"адрес": адрес,
                                       "sol": прирост / ЛАМПОРТОВ_В_SOL,
                                       "чаевые": метки.get(адрес),
                                       "создан": до[и] == 0})
            if до[и] == 0:
                созданные += 1
    пул = есть_пул(tx)
    if пул:
        из_["причина"] = "сделка"
        из_["пул"] = пул
    elif прокрутка_нонса(tx):
        из_["причина"] = "прокрутка nonce"
    elif созданные:
        из_["причина"] = "рента созданного счёта"
    elif any(п.get("чаевые") for п in из_["получатели"]):
        из_["причина"] = "чаевые"
    elif not из_["получатели"]:
        из_["причина"] = "только тариф"
    else:
        из_["причина"] = "иное"
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--koshelek", required=True)
    р.add_argument("--s", required=True, help="начало окна UTC, ISO")
    р.add_argument("--po", required=True, help="конец окна UTC, ISO")
    р.add_argument("--predel", type=int, default=400, help="сколько подписей брать")
    р.add_argument("--out", default=None)
    а = р.parse_args()

    import calendar  # noqa: PLC0415

    def в_эпоху(с: str) -> int:
        с = с.replace("Z", "").replace("T", " ")
        return calendar.timegm(time.strptime(с[:19], "%Y-%m-%d %H:%M:%S"))

    от, до_ = в_эпоху(а.s), в_эпоху(а.po)
    итог = {"кошелёк": а.koshelek, "окно": [а.s, а.po],
            "снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    метки = счета_чаевых()
    подписи, курсор = [], None
    while len(подписи) < а.predel:
        пар = {"limit": min(1000, а.predel - len(подписи) + 200)}
        if курсор:
            пар["before"] = курсор
        пачка = rpc("getSignaturesForAddress", [а.koshelek, пар]) or []
        if not пачка:
            break
        for з in пачка:
            вр = з.get("blockTime")
            if вр is None:
                continue
            if вр > до_:
                continue
            if вр < от:
                пачка = []
                break
            подписи.append(з)
        курсор = (пачка[-1].get("signature") if пачка else None)
        if not курсор:
            break
    итог["подписей_в_окне"] = len(подписи)

    по_причинам: dict = {}
    строки = []
    иное = []
    for з in подписи:
        tx = rpc("getTransaction", [з["signature"],
                                     {"encoding": "jsonParsed",
                                      "maxSupportedTransactionVersion": 1}])
        if not tx:
            иное.append({"подпись": з["signature"], "почему": "узел не отдал транзакцию"})
            continue
        р_ = разобрать(tx, а.koshelek, метки)
        д = р_.get("дельта_кошелька")
        причина = р_["причина"]
        с = по_причинам.setdefault(причина, {"n": 0, "sol": 0.0, "тариф_sol": 0.0})
        с["n"] += 1
        с["sol"] += (д or 0.0)
        с["тариф_sol"] += р_["тариф"]
        строки.append({"подпись": з["signature"], "utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(з.get("blockTime") or 0)),
            "причина": причина, "дельта_sol": д, "тариф_sol": р_["тариф"],
            "получателей": len(р_["получатели"]),
            "ошибка": bool(р_.get("ошибка"))})
        if причина == "иное":
            иное.append({"подпись": з["signature"], "дельта_sol": д,
                          "получатели": р_["получатели"][:5]})

    итог["по_причинам"] = {к: {"n": v["n"], "sol": round(v["sol"], 9),
                                "тариф_sol": round(v["тариф_sol"], 9)}
                            for к, v in sorted(по_причинам.items(),
                                                key=lambda кв: кв[1]["sol"])}
    итог["всего_sol"] = round(sum(v["sol"] for v in по_причинам.values()), 9)
    часов = max(1e-9, (до_ - от) / 3600.0)
    итог["часов_в_окне"] = round(часов, 3)
    итог["в_сутки_sol"] = round(итог["всего_sol"] / часов * 24.0, 9)
    н = по_причинам.get("прокрутка nonce")
    if н:
        итог["прокрутка_nonce"] = {
            "раз": н["n"], "sol": round(н["sol"], 9),
            "в_сутки_sol": round(н["sol"] / часов * 24.0, 9),
            "среднее_на_прокрутку_sol": round(н["sol"] / н["n"], 9),
            "раз_в_сутки": round(н["n"] / часов * 24.0, 2)}
    итог["иное"] = иное[:20]
    итог["строки"] = строки

    кратко = {к: итог[к] for к in ("кошелёк", "окно", "подписей_в_окне",
                                    "по_причинам", "всего_sol", "часов_в_окне",
                                    "в_сутки_sol") if к in итог}
    if "прокрутка_nonce" in итог:
        кратко["прокрутка_nonce"] = итог["прокрутка_nonce"]
    кратко["иного_подписей"] = len(иное)
    print(json.dumps(кратко, ensure_ascii=False, indent=1))
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
