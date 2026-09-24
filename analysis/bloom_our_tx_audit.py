#!/usr/bin/env python3
"""Наши транзакции по цепи: села или упала, с каким кодом и что двинулось.

Зачем. 24.09 Telegram доложил "покупка SENT ... S+0 по цепи" и через минуту
"продажа продана", а по цепи покупка УПАЛА: токена не было, продавать было
нечего. Значит, ни строке, ни отчётам верить нельзя, пока они не сверены с
цепью. Этот разбор ходит ровно в цепь и отвечает по фактам:

  * meta.err -- села транзакция или упала, и с какой ошибкой;
  * строки журнала программ, где видно причину (проскальзывание, нехватка
    средств, счёт);
  * движение НАШИХ балансов: сколько ушло нативного SOL и какие токены
    пришли или ушли -- по preTokenBalances/postTokenBalances нашего адреса;
  * слот и время посадки.

Только чтение. Ни одного ордера. Ключ берётся из HELIUS_API_KEY и никуда
не печатается.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402

LAMPORT = 1_000_000_000
WSOL = "So11111111111111111111111111111111111111112"
# Строки журнала программ, в которых обычно и лежит причина отказа.
СЛОВА_ОШИБКИ = ("failed", "error", "insufficient", "slippage", "exceeded",
                 "custom program error", "panicked", "0x")


def utc(ts) -> str | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def наш_нативный_дельта(tx: dict, кошелёк: str) -> float | None:
    """Изменение нативного SOL нашего адреса, в SOL."""
    мета = tx.get("meta") or {}
    ключи = (((tx.get("transaction") or {}).get("message") or {})
              .get("accountKeys") or [])
    индекс = None
    for i, k in enumerate(ключи):
        адрес = k.get("pubkey") if isinstance(k, dict) else k
        if адрес == кошелёк:
            индекс = i
            break
    if индекс is None:
        return None
    до = мета.get("preBalances") or []
    после = мета.get("postBalances") or []
    if индекс >= len(до) or индекс >= len(после):
        return None
    return (после[индекс] - до[индекс]) / LAMPORT


def наши_токены(tx: dict, кошелёк: str) -> dict:
    """Изменение токенных балансов нашего адреса: минт -> дельта в единицах."""
    мета = tx.get("meta") or {}
    из_: dict = {}
    for где, знак in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in (мета.get(где) or []):
            if not isinstance(b, dict) or b.get("owner") != кошелёк:
                continue
            минт = b.get("mint")
            сумма = ((b.get("uiTokenAmount") or {}).get("uiAmount"))
            if минт is None or not isinstance(сумма, (int, float)):
                continue
            из_[минт] = из_.get(минт, 0.0) + знак * float(сумма)
    return {м: round(v, 9) for м, v in из_.items() if abs(v) > 1e-12}


def строки_ошибки(tx: dict, сколько: int = 12) -> list:
    """Строки журнала программ, похожие на причину отказа."""
    журнал = (tx.get("meta") or {}).get("logMessages") or []
    важные = [с for с in журнал
               if any(сл in с.lower() for сл in СЛОВА_ОШИБКИ)]
    return (важные or журнал[-сколько:])[:сколько]


def разобрать_нашу(tx: dict, подпись: str, кошелёк: str) -> dict:
    """Одна наша транзакция: села или нет, что двинулось, причина отказа."""
    мета = tx.get("meta") or {}
    ошибка = мета.get("err")
    токены = наши_токены(tx, кошелёк)
    пришли = {м: v for м, v in токены.items() if v > 0 and м != WSOL}
    ушли = {м: v for м, v in токены.items() if v < 0 and м != WSOL}
    натив = наш_нативный_дельта(tx, кошелёк)
    вид = "прочее"
    if пришли and (натив is None or натив < 0):
        вид = "покупка"
    elif ушли:
        вид = "продажа"
    return {"signature": подпись, "slot": tx.get("slot"),
             "block_utc": utc(tx.get("blockTime")),
             "ok": ошибка is None, "err": ошибка,
             "fee_sol": round((мета.get("fee") or 0) / LAMPORT, 9),
             "native_delta_sol": натив,
             "tokens_in": пришли, "tokens_out": ушли,
             "kind": вид,
             "logs": strings_ok(строки_ошибки(tx)) if ошибка is not None else []}


def strings_ok(строки: list) -> list:
    """Обрезка строк журнала: длинные base64-хвосты в докладе не нужны."""
    return [с[:220] for с in строки]


def собрать(helius, кошелёк: str, с_utc: str | None, предел: int) -> list:
    """Подписи нашего адреса и разбор каждой. Только чтение."""
    подписи = helius.call("getSignaturesForAddress",
                           [кошелёк, {"limit": max(1, min(1000, предел))}])
    из_ = []
    for з in (подписи or []):
        если_время = utc(з.get("blockTime"))
        if с_utc and если_время and если_время < с_utc:
            continue
        подпись = з.get("signature")
        if not подпись:
            continue
        tx = None
        try:
            tx = helius.транзакция(подпись)
        except Exception as exc:  # noqa: BLE001
            из_.append({"signature": подпись, "why_not":
                         f"{type(exc).__name__}: {str(exc)[:120]}",
                         "err_from_list": з.get("err"),
                         "block_utc": если_время, "slot": з.get("slot")})
            continue
        if not tx:
            из_.append({"signature": подпись, "why_not": "узел не отдал транзакцию",
                         "err_from_list": з.get("err"),
                         "block_utc": если_время, "slot": з.get("slot")})
            continue
        из_.append(разобрать_нашу(tx, подпись, кошелёк))
    из_.sort(key=lambda з: (з.get("block_utc") or "", з.get("slot") or 0))
    return из_


def пары_покупка_продажа(записи: list) -> list:
    """Пары "покупка -> продажа" по минту: секунды по цепи между ними.

    Пара считается только по СЕВШИМ транзакциям: упавшая покупка позиции не
    открывает, и время до продажи по ней -- не число, а выдумка.
    """
    открытые: dict = {}
    пары = []
    for з in записи:
        if not з.get("ok"):
            continue
        for м in (з.get("tokens_in") or {}):
            открытые.setdefault(м, []).append(з)
        for м in (з.get("tokens_out") or {}):
            очередь = открытые.get(м) or []
            if not очередь:
                continue
            покупка = очередь.pop(0)
            t1 = покупка.get("block_utc")
            t2 = з.get("block_utc")
            секунды = None
            if t1 and t2:
                секунды = (datetime.strptime(t2, "%Y-%m-%dT%H:%M:%SZ")
                            - datetime.strptime(t1, "%Y-%m-%dT%H:%M:%SZ")
                            ).total_seconds()
            пары.append({"mint": м, "buy_sig": покупка["signature"],
                          "sell_sig": з["signature"],
                          "buy_utc": t1, "sell_utc": t2,
                          "buy_slot": покупка.get("slot"), "sell_slot": з.get("slot"),
                          "seconds": секунды,
                          "slots": ((з.get("slot") or 0) - (покупка.get("slot") or 0))
                          if (з.get("slot") and покупка.get("slot")) else None,
                          "sol_in": покупка.get("native_delta_sol"),
                          "sol_out": з.get("native_delta_sol")})
    return пары


def self_test() -> None:
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    КОШ = "НАШ"

    def TX(err, натив_до, натив_после, токены_до, токены_после, слот=100,
            время=1790000000, журнал=None):
        return {"slot": слот, "blockTime": время,
                 "transaction": {"message": {"accountKeys": [
                     {"pubkey": КОШ}, {"pubkey": "ЧУЖОЙ"}]}},
                 "meta": {"err": err, "fee": 5000,
                           "preBalances": [натив_до, 0],
                           "postBalances": [натив_после, 0],
                           "preTokenBalances": токены_до,
                           "postTokenBalances": токены_после,
                           "logMessages": журнал or []}}

    def Б(минт, сумма, владелец=КОШ):
        return {"mint": минт, "owner": владелец,
                 "uiTokenAmount": {"uiAmount": сумма}}

    купля = TX(None, 2 * LAMPORT, int(1.8 * LAMPORT), [], [Б("M1", 1000.0)])
    р = разобрать_нашу(купля, "S_КУП", КОШ)
    chk("севшая покупка опознана", р["ok"] and р["kind"] == "покупка", р)
    chk("нативная дельта посчитана",
        abs(р["native_delta_sol"] + 0.2) < 1e-9, р["native_delta_sol"])
    chk("пришедший токен назван", р["tokens_in"] == {"M1": 1000.0}, р["tokens_in"])

    упала = TX({"InstructionError": [3, {"Custom": 6001}]},
                2 * LAMPORT, int(1.99993 * LAMPORT), [], [],
                журнал=["Program log: Error: slippage tolerance exceeded",
                         "Program XYZ failed: custom program error: 0x1771"])
    у = разобрать_нашу(упала, "S_УПАЛ", КОШ)
    chk("упавшая транзакция помечена ok=False", у["ok"] is False, у)
    chk("код ошибки сохранён целиком",
        у["err"] == {"InstructionError": [3, {"Custom": 6001}]}, у["err"])
    chk("строка про проскальзывание попала в журнал разбора",
        any("slippage" in с for с in у["logs"]), у["logs"])
    chk("у упавшей покупки токенов нет", у["tokens_in"] == {}, у)

    продажа = TX(None, int(1.8 * LAMPORT), int(1.95 * LAMPORT),
                  [Б("M1", 1000.0)], [], слот=140, время=1790000068)
    п = разобрать_нашу(продажа, "S_ПРОД", КОШ)
    chk("продажа опознана по ушедшему токену", п["kind"] == "продажа", п)

    пары = пары_покупка_продажа([р, у, п])
    chk("пара покупка-продажа одна", len(пары) == 1, пары)
    chk("секунды по цепи посчитаны", пары[0]["seconds"] == 68.0, пары[0])
    chk("слотов между покупкой и продажей", пары[0]["slots"] == 40, пары[0])

    # Упавшая покупка пару НЕ открывает: иначе в таблицу попадёт сделка,
    # которой не было.
    купля2 = TX({"InstructionError": [3, {"Custom": 6001}]},
                 2 * LAMPORT, int(1.99 * LAMPORT), [], [Б("M2", 5.0)])
    р2 = разобрать_нашу(купля2, "S_УПАЛ2", КОШ)
    продажа2 = TX(None, 0, 0, [Б("M2", 5.0)], [], слот=200, время=1790000200)
    п2 = разобрать_нашу(продажа2, "S_ПРОД2", КОШ)
    chk("упавшая покупка пары не создаёт",
        пары_покупка_продажа([р2, п2]) == [], пары_покупка_продажа([р2, п2]))

    # Чужие токенные балансы в той же транзакции -- не наши.
    чужой = TX(None, 0, 0, [], [Б("M3", 7.0, владелец="ЧУЖОЙ")])
    chk("чужой токенный баланс не считается нашим",
        разобрать_нашу(чужой, "S_ЧУЖ", КОШ)["tokens_in"] == {}, чужой)

    print(f"самопроверка разбора наших транзакций: {всего[1]}/{всего[0]}"
           f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    if всего[1] != всего[0]:
        raise SystemExit(1)


def человеку(записи: list, пары: list) -> str:
    строки = [f"наших транзакций разобрано: {len(записи)}"]
    упавших = [з for з in записи if з.get("ok") is False]
    строки.append(f"из них УПАЛО по цепи: {len(упавших)}")
    for з in записи:
        if "why_not" in з:
            строки.append(f"  {з['block_utc']} {з['signature'][:12]} -- "
                           f"не разобрано: {з['why_not']}")
            continue
        итог = "села" if з["ok"] else f"УПАЛА {json.dumps(з['err'], ensure_ascii=False)}"
        строки.append(
            f"  {з['block_utc']} слот {з['slot']} {з['signature'][:12]} "
            f"{з['kind']}: {итог}, комиссия {з['fee_sol']:.6f}, "
            f"нативно {з['native_delta_sol']}, пришло {з['tokens_in']}, "
            f"ушло {з['tokens_out']}")
        for с in (з.get("logs") or [])[:4]:
            строки.append(f"      {с}")
    строки.append(f"пар покупка-продажа (только по севшим): {len(пары)}")
    for п in пары:
        строки.append(
            f"  минт {п['mint'][:10]}: {п['buy_utc']} -> {п['sell_utc']}, "
            f"{п['seconds']} с, {п['slots']} слотов, вход {п['sol_in']}, "
            f"выход {п['sol_out']}")
    return "\n".join(строки)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--wallet", default="")
    p.add_argument("--since", default="", help="UTC вида 2026-09-24T00:00:00Z")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--out", default="")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return 0
    кошелёк = a.wallet or getattr(BD.ST, "EXECUTOR_WALLET", "") or ""
    if not кошелёк:
        print("не задан кошелёк: --wallet", file=sys.stderr)
        return 2
    helius = BD.Helius(служба="bloom_our_tx_audit")
    записи = собрать(helius, кошелёк, a.since or None, a.limit)
    пары = пары_покупка_продажа(записи)
    print(f"кошелёк: {кошелёк}")
    print(человеку(записи, пары))
    if a.out:
        Path(a.out).write_text(json.dumps(
            {"built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "wallet": кошелёк, "since": a.since, "records": записи,
             "pairs": пары}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"записано: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
