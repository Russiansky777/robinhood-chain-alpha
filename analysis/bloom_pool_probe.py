#!/usr/bin/env python3
"""Пулы из транзакции и проверка кандидатов по цепи. ТОЛЬКО ЧТЕНИЕ.

Зачем. Владелец требует покупать по ID пула источника, а сторожу --
продавать по ID пула нашей покупки. Значит ID пула надо доставать из
транзакции, и доставать так, чтобы на горячем пути не появилось ни одного
лишнего запроса в сеть.

Правило, которым берётся пул, и почему именно оно. Пул держит обе стороны
пары на СВОИХ счетах, поэтому в pre/postTokenBalances транзакции у этих
счетов стоит владелец -- и у концентрированной ликвидности (Raydium CLMM,
Orca Whirlpool, Meteora DLMM/DAMM) владельцем счёта-хранилища выступает
сам адрес пула. Значит кандидат в пул -- это владелец, у которого в одной
транзакции есть хранилища ДВУХ разных минтов, один из которых наш токен.
Ни одного индекса счёта в инструкции не зашито: индексы у каждой
программы свои и меняются с версией, а владелец хранилища -- факт из
данных.

Где правило НЕ работает, и это сказано прямо, а не спрятано. У Raydium
AMM v4 и Raydium CPMM владельцем хранилищ выступает ОБЩИЙ для всех пулов
служебный адрес (authority PDA), а не пул. Такой адрес выглядит как
кандидат, но пулом не является, и отправлять его в Bloom нельзя.
Отличить его от пула по одной транзакции невозможно -- поэтому есть
проверка по цепи (getMultipleAccounts: владелец счёта и длина данных) и
список проверенных исключений с доказательством: у служебного адреса
владелец -- системная программа и нулевая длина данных, у пула владелец
-- программа DEX и данные есть.

Самотест проверяет чистый разбор без сети. Ключи не печатаются.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

import bloom_detector as BD  # noqa: E402

WSOL = "So11111111111111111111111111111111111111112"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
HELIUS_RPC = "https://mainnet.helius-rpc.com"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"

# Проверенные по цепи служебные адреса: владельцы хранилищ, но НЕ пулы.
# Пополняется только по факту проверки (см. --check), с доказательством.
ИСКЛЮЧЕНИЯ_ПУЛОВ_ФАЙЛ = Path(__file__).resolve().parent.parent / "data" / "bloom_pool_exclusions.json"


def исключения() -> dict:
    """Служебные адреса, проверенные по цепи. Нечитаемый файл -- пусто."""
    try:
        d = json.loads(ИСКЛЮЧЕНИЯ_ПУЛОВ_ФАЙЛ.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _хранилища(tx: dict) -> dict:
    """Владелец счёта -> минты, которые он держит в этой транзакции."""
    из_владельца: dict = {}
    meta = (tx or {}).get("meta") or {}
    for где in ("preTokenBalances", "postTokenBalances"):
        for b in meta.get(где) or []:
            if not isinstance(b, dict):
                continue
            вл, м = b.get("owner"), b.get("mint")
            if вл and м:
                из_владельца.setdefault(вл, set()).add(м)
    return из_владельца


def _счета_инструкций_dex(tx: dict) -> dict:
    """Адрес счёта -> программы DEX, в чьих инструкциях он встретился."""
    вых: dict = {}
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    meta = (tx or {}).get("meta") or {}
    пачки = [msg.get("instructions") or []]
    for гр in meta.get("innerInstructions") or []:
        пачки.append((гр or {}).get("instructions") or [])
    for пачка in пачки:
        for ins in пачка:
            if not isinstance(ins, dict):
                continue
            имя = BD.ПРОГРАММЫ_DEX.get(ins.get("programId"))
            if not имя:
                continue
            for acc in ins.get("accounts") or []:
                if isinstance(acc, str):
                    вых.setdefault(acc, [])
                    if имя not in вых[acc]:
                        вых[acc].append(имя)
    return вых


def кандидаты_пулов(tx: dict, *, минт: str, кошелёк: str | None = None,
                     исключить: dict | None = None) -> dict:
    """Кандидаты в пул из одной транзакции. БЕЗ СЕТИ.

    Возвращает всех кандидатов с доказательствами и отдельно лучший выбор
    для пары токен/WSOL. Если выбора нет -- сказано, почему нет.
    """
    исключить = исключить if исключить is not None else исключения()
    хран = _хранилища(tx)
    в_dex = _счета_инструкций_dex(tx)
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    ключи = BD._баланс_ключи(tx)
    плательщик = ключи[0] if ключи else None

    кандидаты = []
    for вл, минты in sorted(хран.items()):
        if len(минты) < 2 or минт not in минты:
            continue
        причина_нет = None
        if вл == кошелёк or вл == плательщик:
            причина_нет = "это кошелёк сделки, а не пул"
        elif вл in исключить:
            причина_нет = f"служебный адрес, проверен по цепи: {исключить[вл].get('why', '')}"
        elif вл not in в_dex:
            причина_нет = "адрес не встречается в счетах инструкций DEX"
        кандидаты.append({
            "address": вл,
            "mints": sorted(минты),
            "with_wsol": WSOL in минты,
            "dex_programs": в_dex.get(вл, []),
            "vault_mints_count": len(минты),
            "rejected": причина_нет,
        })

    годные = [k for k in кандидаты if not k["rejected"] and k["with_wsol"]]
    выбор = годные[0]["address"] if len(годные) == 1 else None
    почему_нет = None
    if выбор is None:
        if not кандидаты:
            почему_нет = "в транзакции нет владельца с хранилищами двух минтов, один из которых наш"
        elif not годные:
            почему_нет = "ни один кандидат не парный к WSOL или все отклонены"
        else:
            почему_нет = f"кандидатов с WSOL больше одного ({len(годные)}) -- какой пул наш, неясно"
    return {"mint": минт, "pool_wsol": выбор, "why_not": почему_нет,
             "candidates": кандидаты,
             "payer": плательщик,
             "dex_programs": BD.программы_dex(tx)}


# --------------------------------------------------------------- сеть

def rpc(метод: str, параметры: list, *, таймаут: float = 20.0) -> dict:
    key = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    адреса = ([f"{HELIUS_RPC}/?api-key={key}"] if key else []) + [PUBLIC_RPC]
    последняя = "адресов узла нет"
    for url in адреса:
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": метод,
                                          "params": параметры}, timeout=таймаут)
        except requests.RequestException as exc:
            последняя = f"{type(exc).__name__}: {exc}"
            continue
        if r.status_code != 200:
            последняя = f"HTTP {r.status_code}: {r.text[:200]}"
            continue
        try:
            тело = r.json()
        except ValueError:
            последняя = "ответ узла не json"
            continue
        if "error" in тело:
            последняя = f"RPC error: {str(тело['error'])[:300]}"
            continue
        return {"ok": True, "result": тело.get("result")}
    return {"ok": False, "why_not": последняя}


def проверить_кандидатов(адреса: list) -> dict:
    """Владелец счёта и длина данных -- по цепи. Пул или служебный адрес."""
    if not адреса:
        return {"ok": True, "accounts": {}}
    r = rpc("getMultipleAccounts", [адреса, {"encoding": "base64"}])
    if not r.get("ok"):
        return {"ok": False, "why_not": r.get("why_not")}
    вых = {}
    значения = ((r.get("result") or {}).get("value") or [])
    for адрес, знач in zip(адреса, значения):
        if not знач:
            вых[адрес] = {"exists": False, "verdict": "счёта нет"}
            continue
        владелец = знач.get("owner")
        данные = (знач.get("data") or ["", ""])[0]
        длина = знач.get("space")
        if длина is None:
            import base64  # noqa: PLC0415
            try:
                длина = len(base64.b64decode(данные))
            except Exception:  # noqa: BLE001
                длина = None
        имя = BD.ПРОГРАММЫ_DEX.get(владелец)
        вых[адрес] = {
            "exists": True, "owner_program": владелец, "owner_name": имя,
            "data_len": длина,
            "verdict": ("пул: владелец -- программа DEX, данные есть"
                        if имя and (длина or 0) > 0 else
                        ("служебный адрес: владелец -- системная программа, данных нет"
                         if владелец == SYSTEM_PROGRAM and not (длина or 0) else
                         "не пул: владелец не программа DEX")),
        }
    return {"ok": True, "accounts": вых}


def проба_остатка(кошелёк: str, минт: str) -> dict:
    """Три способа спросить остаток. Нужен ФАКТ, какой из них работает."""
    из = {}
    варианты = {
        "mint+programId": [кошелёк, {"mint": минт,
                                      "programId": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"},
                            {"encoding": "jsonParsed"}],
        "mint": [кошелёк, {"mint": минт}, {"encoding": "jsonParsed"}],
        "programId_2022": [кошелёк,
                            {"programId": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"},
                            {"encoding": "jsonParsed"}],
    }
    for имя, параметры in варианты.items():
        r = rpc("getTokenAccountsByOwner", параметры)
        if not r.get("ok"):
            из[имя] = {"ok": False, "why_not": r.get("why_not")}
            continue
        значения = ((r.get("result") or {}).get("value") or [])
        счета = []
        for it in значения:
            info = ((((it.get("account") or {}).get("data") or {}).get("parsed") or {})
                    .get("info") or {})
            amt = info.get("tokenAmount") or {}
            if имя == "programId_2022" and info.get("mint") != минт:
                continue
            счета.append({"pubkey": it.get("pubkey"), "mint": info.get("mint"),
                           "amount": amt.get("amount"), "ui": amt.get("uiAmount")})
        из[имя] = {"ok": True, "accounts": счета}
    return из


def self_test() -> int:
    checks = []

    def chk(имя, ок, факт=""):
        checks.append((имя, bool(ок), факт))

    ТОКЕН = "TOK"
    ПУЛ = "POOL1"
    СЛУЖ = "AUTH1"
    КОШ = "WALLET"

    def tx(*, владельцы, dex_accounts, программа="CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"):
        балансы = []
        i = 0
        for вл, минты in владельцы.items():
            for м in минты:
                i += 1
                балансы.append({"accountIndex": i, "mint": м, "owner": вл,
                                 "uiTokenAmount": {"amount": "1", "decimals": 6,
                                                    "uiAmount": 1.0}})
        return {"transaction": {"message": {
                    "accountKeys": [{"pubkey": КОШ}, {"pubkey": ПУЛ}, {"pubkey": СЛУЖ}],
                    "instructions": [{"programId": программа, "accounts": dex_accounts,
                                       "data": "x"}]}},
                "meta": {"preTokenBalances": балансы, "postTokenBalances": балансы,
                          "innerInstructions": []}}

    d = кандидаты_пулов(tx(владельцы={ПУЛ: [ТОКЕН, WSOL], КОШ: [ТОКЕН]},
                            dex_accounts=[ПУЛ, "VAULT_A", "VAULT_B"]),
                         минт=ТОКЕН, кошелёк=КОШ, исключить={})
    chk("пул концентрированной ликвидности взят как владелец хранилищ",
        d["pool_wsol"] == ПУЛ, d)
    chk("кошелёк сделки кандидатом не считается",
        all(k["address"] != КОШ or k["rejected"] for k in d["candidates"]))

    d2 = кандидаты_пулов(tx(владельцы={СЛУЖ: [ТОКЕН, WSOL]}, dex_accounts=[СЛУЖ]),
                          минт=ТОКЕН, кошелёк=КОШ,
                          исключить={СЛУЖ: {"why": "проверен по цепи: системная программа"}})
    chk("проверенный служебный адрес за пул не выдаётся", d2["pool_wsol"] is None, d2)
    chk("и причина названа", "служебный" in (d2["why_not"] or "")
        or any("служебный" in (k["rejected"] or "") for k in d2["candidates"]), d2)

    d3 = кандидаты_пулов(tx(владельцы={ПУЛ: [ТОКЕН, "USDC_M"]}, dex_accounts=[ПУЛ]),
                          минт=ТОКЕН, кошелёк=КОШ, исключить={})
    chk("пул только к USDC не выдаётся за пул к WSOL", d3["pool_wsol"] is None, d3)
    chk("и это видно в кандидате", d3["candidates"][0]["with_wsol"] is False)

    d4 = кандидаты_пулов(tx(владельцы={ПУЛ: [ТОКЕН, WSOL], "POOL2": [ТОКЕН, WSOL]},
                             dex_accounts=[ПУЛ, "POOL2"]), минт=ТОКЕН, кошелёк=КОШ,
                          исключить={})
    chk("два пула к WSOL -- выбор не делается наугад", d4["pool_wsol"] is None, d4)
    chk("и сказано, что кандидатов больше одного", "больше одного" in (d4["why_not"] or ""))

    d5 = кандидаты_пулов(tx(владельцы={ПУЛ: [ТОКЕН, WSOL]}, dex_accounts=["ДРУГОЙ"]),
                          минт=ТОКЕН, кошелёк=КОШ, исключить={})
    chk("владелец, которого нет в счетах инструкций DEX, отклонён",
        d5["pool_wsol"] is None and d5["candidates"][0]["rejected"], d5)

    d6 = кандидаты_пулов({"meta": {}, "transaction": {"message": {}}}, минт=ТОКЕН,
                          кошелёк=КОШ, исключить={})
    chk("пустая транзакция: пула нет и причина названа",
        d6["pool_wsol"] is None and "нет владельца" in (d6["why_not"] or ""), d6)

    src = Path(__file__).read_text(encoding="utf-8")
    тело = src.split("def self_test")[0]
    chk("ни одного зашитого индекса счёта в инструкции",
        "accounts\"][0]" not in тело and "accounts'][0]" not in тело)
    chk("чистый разбор сети не касается",
        "requests" not in кандидаты_пулов.__code__.co_names)

    плохо = [c for c in checks if not c[1]]
    for имя, ок, факт in checks:
        print(f"{'OK ' if ок else 'НЕТ'} {имя}"
              f"{(' -- ' + json.dumps(факт, ensure_ascii=False)[:300]) if факт and not ок else ''}")
    print(f"итого {len(checks) - len(плохо)}/{len(checks)}")
    return 1 if плохо else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sig", action="append", default=[],
                     help="подпись транзакции: достать кандидатов в пул")
    ap.add_argument("--mint", default="", help="минт токена сделки")
    ap.add_argument("--wallet", default="", help="кошелёк сделки (исключается из кандидатов)")
    ap.add_argument("--balance-probe", nargs=2, metavar=("WALLET", "MINT"),
                     help="три способа спросить остаток токена -- какой работает")
    ap.add_argument("--out", default="data/bloom_pool_probe.json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    вых = {"built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "signatures": {}, "balance_probe": None}

    if a.balance_probe:
        вых["balance_probe"] = {"wallet": a.balance_probe[0], "mint": a.balance_probe[1],
                                 "variants": проба_остатка(*a.balance_probe)}

    for подпись in a.sig:
        r = rpc("getTransaction", [подпись, {"encoding": "jsonParsed",
                                              "maxSupportedTransactionVersion":
                                                  BD.ПОТОЛОК_ВЕРСИИ_TX,
                                              "commitment": "confirmed"}])
        if not r.get("ok") or not r.get("result"):
            вых["signatures"][подпись] = {"ok": False,
                                           "why_not": r.get("why_not") or "транзакции нет"}
            continue
        tx = r["result"]
        минт = a.mint or ""
        разбор = кандидаты_пулов(tx, минт=минт, кошелёк=a.wallet or None)
        адреса = [k["address"] for k in разбор["candidates"]]
        проверка = проверить_кандидатов(адреса)
        вых["signatures"][подпись] = {"ok": True, "slot": tx.get("slot"),
                                       "err": (tx.get("meta") or {}).get("err"),
                                       "parse": разбор, "on_chain": проверка}

    текст = json.dumps(вых, ensure_ascii=False, indent=1)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(текст, encoding="utf-8")
    print(текст)
    return 0


if __name__ == "__main__":
    sys.exit(main())
