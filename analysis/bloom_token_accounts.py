#!/usr/bin/env python3
"""Токен-счета кошелька исполнителя: сколько их и сколько SOL в них заперто.

Вопрос владельца прямой: рента ATA -- возвращённая издержка или запертая.
Ответ даёт только цепь: getTokenAccountsByOwner отдаёт каждый живой счёт с
его lamports (это и есть рента, внесённая при открытии) и остатком токена.

  * счёт с НУЛЕВЫМ остатком -- рента заперта зря: продажа прошла, а счёт не
    закрыт. Эти лампорты вернутся только закрытием счёта (closeAccount);
  * счёт с НЕНУЛЕВЫМ остатком -- это пыль или позиция: рента работает, но
    остаток стоит посмотреть отдельно.

Только чтение. Ни одной подписи, ни одного закрытия счёта: закрытие -- это
транзакция кошелька, и делать её молча нельзя.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402
import bloom_exec_state as ST  # noqa: E402

ЛАМПОРТ = 10 ** 9
ПРОГРАММЫ = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
              "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


def счета(helius, кошелёк: str) -> dict:
    """Все токен-счета кошелька по обеим токен-программам."""
    строки, отказы = [], []
    for программа in ПРОГРАММЫ:
        try:
            r = helius.call("getTokenAccountsByOwner",
                             [кошелёк, {"programId": программа},
                              {"encoding": "jsonParsed"}])
        except Exception as exc:  # noqa: BLE001
            отказы.append({"program": программа,
                            "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"})
            continue
        for z in ((r or {}).get("value") or []):
            счёт = z.get("pubkey")
            данные = (((z.get("account") or {}).get("data") or {}).get("parsed")
                       or {}).get("info") or {}
            сумма = (данные.get("tokenAmount") or {})
            лампорты = int((z.get("account") or {}).get("lamports", 0) or 0)
            # СЧЁТ WSOL: его lamports -- это рента ПЛЮС завёрнутый SOL. Считать
            # их рентой целиком значит задвоить завёрнутые деньги: те же самые
            # лампорты приходят вторым разом нативным возвратом при закрытии
            # счёта. Признак isNative и rentExemptReserve узел отдаёт сам.
            свой = bool(данные.get("isNative"))
            запас = данные.get("rentExemptReserve")
            if свой:
                try:
                    рента_л = int(запас)
                except (TypeError, ValueError):
                    # Запаса в ответе нет -- завёрнутое берём из остатка счёта:
                    # у WSOL остаток и есть лампорты.
                    рента_л = max(0, лампорты - int(сумма.get("amount") or 0))
            else:
                рента_л = лампорты
            рента_л = max(0, min(рента_л, лампорты))
            строки.append({
                "account": счёт,
                "mint": данные.get("mint"),
                "program": программа,
                "amount_ui": сумма.get("uiAmount"),
                "amount_raw": сумма.get("amount"),
                "decimals": сумма.get("decimals"),
                "is_native": свой,
                "lamports": лампорты,
                "rent_sol": round(рента_л / ЛАМПОРТ, 9),
                "wrapped_sol": round((лампорты - рента_л) / ЛАМПОРТ, 9),
            })
    пустые = [x for x in строки if not float(x.get("amount_raw") or 0)]
    с_остатком = [x for x in строки if float(x.get("amount_raw") or 0)]
    return {
        "wallet": кошелёк,
        "accounts": строки,
        "count": len(строки),
        "empty_count": len(пустые),
        "with_balance_count": len(с_остатком),
        "rent_locked_sol": round(sum(x["rent_sol"] for x in строки), 9),
        "rent_locked_in_empty_sol": round(sum(x["rent_sol"] for x in пустые), 9),
        # Завёрнутый SOL считается ОТДЕЛЬНО от ренты и в неё не входит: он
        # вернётся нативно при закрытии счёта, и складывать его с рентой
        # значит посчитать одни и те же лампорты дважды.
        "wrapped_sol": round(sum(x["wrapped_sol"] for x in строки), 9),
        "wsol_accounts": [x for x in строки if x["is_native"]],
        "empty": пустые,
        "with_balance": с_остатком,
        "failures": отказы,
        "note": ("рента в ПУСТЫХ счетах заперта зря: продажа прошла, счёт не "
                  "закрыт. Вернуть её можно только закрытием счёта, и это "
                  "транзакция кошелька -- здесь она НЕ делается. У счёта WSOL "
                  "рента и завёрнутый SOL разделены: в rent_sol только рента, "
                  "завёрнутое в wrapped_sol"),
    }


def self_test() -> None:
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    def счёт(pk, минт, raw, lamports=2039280, свой=False, запас=None):
        инфо = {"mint": минт, "tokenAmount": {"amount": str(raw), "decimals": 6,
                                               "uiAmount": raw / 1e6}}
        if свой:
            инфо["isNative"] = True
            if запас is not None:
                инфо["rentExemptReserve"] = str(запас)
        return {"pubkey": pk, "account": {"lamports": lamports, "data": {"parsed": {
            "info": инфо}}}}

    class HeliusЗаглушка:
        def __init__(self, по_программам, падать=False):
            self.по_программам = по_программам
            self.падать = падать

        def call(self, метод, параметры):
            assert метод == "getTokenAccountsByOwner"
            if self.падать:
                raise RuntimeError("узел молчит")
            return {"value": self.по_программам.get(параметры[1]["programId"], [])}

    h = HeliusЗаглушка({ПРОГРАММЫ[0]: [счёт("A", "МИНТ1", 0), счёт("B", "МИНТ2", 12345)],
                         ПРОГРАММЫ[1]: [счёт("C", "МИНТ3", 0)]})
    r = счета(h, "КОШ")
    chk("счета обеих токен-программ прочитаны", r["count"] == 3, r["count"])
    chk("пустые и с остатком разделены",
        r["empty_count"] == 2 and r["with_balance_count"] == 1, r)
    chk("рента посчитана и отдельно по пустым",
        abs(r["rent_locked_sol"] - 0.00611784) < 1e-9
        and abs(r["rent_locked_in_empty_sol"] - 0.00407856) < 1e-9,
        (r["rent_locked_sol"], r["rent_locked_in_empty_sol"]))
    chk("пыль видна с минтом и остатком",
        r["with_balance"][0]["mint"] == "МИНТ2"
        and r["with_balance"][0]["amount_ui"] == 0.012345, r["with_balance"])

    # ЗАДВОЕНИЕ WSOL. Счёт WSOL на 0.1 SOL держит 0.102039280 лампортов: рента
    # плюс завёрнутое. Прежний код звал всё это рентой, и те же лампорты второй
    # раз приходили нативным возвратом при закрытии счёта.
    WSOL = "So11111111111111111111111111111111111111112"
    hw = HeliusЗаглушка({ПРОГРАММЫ[0]: [
        счёт("W", WSOL, 100_000_000, lamports=102_039_280, свой=True, запас=2_039_280),
        счёт("D", "МИНТ9", 0)]})
    rw = счета(hw, "КОШ")
    chk("у счёта WSOL рента без завёрнутого",
        abs(rw["accounts"][0]["rent_sol"] - 0.00203928) < 1e-12, rw["accounts"][0])
    chk("завёрнутое названо отдельно",
        abs(rw["accounts"][0]["wrapped_sol"] - 0.1) < 1e-12, rw["accounts"][0])
    chk("итог ренты не включает завёрнутое",
        abs(rw["rent_locked_sol"] - 0.00407856) < 1e-12
        and abs(rw["wrapped_sol"] - 0.1) < 1e-12,
        (rw["rent_locked_sol"], rw["wrapped_sol"]))
    chk("счёт WSOL c остатком не попал в пустые",
        rw["empty_count"] == 1 and rw["empty"][0]["account"] == "D", rw["empty"])
    # Без rentExemptReserve в ответе завёрнутое берётся из остатка счёта.
    hb = HeliusЗаглушка({ПРОГРАММЫ[0]: [
        счёт("W2", WSOL, 100_000_000, lamports=102_039_280, свой=True)]})
    rb = счета(hb, "КОШ")
    chk("без запаса ренты завёрнутое считается по остатку",
        abs(rb["accounts"][0]["rent_sol"] - 0.00203928) < 1e-12
        and abs(rb["accounts"][0]["wrapped_sol"] - 0.1) < 1e-12, rb["accounts"][0])
    # Обычный токен-счёт не трогаем: вся его рента остаётся рентой.
    chk("у обычного счёта завёрнутого нет",
        r["accounts"][0]["wrapped_sol"] == 0.0
        and abs(r["accounts"][0]["rent_sol"] - 0.00203928) < 1e-12, r["accounts"][0])

    r2 = счета(HeliusЗаглушка({}, падать=True), "КОШ")
    chk("отказ узла -- причина, а не пустота",
        r2["count"] == 0 and len(r2["failures"]) == 2
        and "RuntimeError" in r2["failures"][0]["why_not"], r2["failures"])

    print(f"самопроверка токен-счетов: {всего[1]}/{всего[0]}"
           f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    if всего[1] != всего[0]:
        raise SystemExit(1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--wallet", default=None)
    p.add_argument("--out", default="data/bloom_token_accounts.json")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return 0
    helius = BD.Helius(служба="bloom_token_accounts")
    кошелёк = a.wallet or ST.EXECUTOR_WALLET
    итог = счета(helius, кошелёк)
    итог["built_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    ST.atomic_write_json(Path(a.out), итог)
    print(json.dumps({k: v for k, v in итог.items()
                       if k not in ("accounts", "empty", "with_balance")},
                      ensure_ascii=False, indent=1))
    print("ПУСТЫЕ (рента заперта):")
    for x in итог["empty"][:20]:
        print(f"  {x['account']} минт {x['mint']} рента {x['rent_sol']}")
    print("С ОСТАТКОМ (пыль или позиция):")
    for x in итог["with_balance"][:20]:
        print(f"  {x['account']} минт {x['mint']} остаток {x['amount_ui']} "
               f"(raw {x['amount_raw']}) рента {x['rent_sol']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
