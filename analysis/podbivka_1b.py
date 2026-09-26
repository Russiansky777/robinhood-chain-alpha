#!/usr/bin/env python3
"""Подбивка, пункт 1б: сквозная сверка по лидеру и Brez против журнала DBot.

ЧТО ЭТО. Условие владельца, чтобы идти к таблицам: по двум источникам за
18-25.09 сойтись СПИСКАМИ (что источник купил по цепи против того, что стоит в
журнале DBot) и СЧИСЛАМИ (симулятор на фактическом слоте входа и фактическом
размере против факта по цепи, медиана расхождения не больше 3 п.п.).

ТРИ СПИСКА НА ВЫХОДЕ:
  совпало            -- покупка источника по цепи и наша запись под неё;
  источник_без_нас   -- источник купил, нашей записи нет (с причиной, а не «?»);
  мы_без_источника   -- наша запись есть, покупки источника под неё на цепи в
                        окне не нашлось (с причиной).

ЧЕСТНОСТЬ. Ничего не достраивается. Причина «не восстановить по данным» пишется
словами, а не прячется в ноль. Перебор цепи ограничен пределом вызовов, и если
предел уперся -- это написано в своде, а не выглядит как полный список.

Только чтение. Ни подписи, ни отправки, ключей кошельков здесь нет.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka as P  # noqa: E402

ЛАМПОРТОВ = 1_000_000_000
# Окно, в которое наша копия обязана попасть после сделки источника. DBot
# копирует секундами; три минуты -- запас на медленный ответ, дальше это уже
# другая сделка, а не копия этой.
ОКНО_КОПИИ_С = 180


def utc(ts) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts))) if ts else ""


def в_секунды(строка: str) -> float:
    return time.mktime(time.strptime(строка, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone


# ----------------------------------------------------------- цепь источника

def план_отдаёт_транзакции(уз: P.Узел, адрес: str) -> dict:
    """Проверка ПЕРВЫМ вызовом: отдаёт ли план getTransactionsForAddress.

    Требование владельца по заданию 2. Если метода нет -- идём длинным путём
    (getSignaturesForAddress + getTransaction) и говорим об этом в своде.
    """
    try:
        рез = уз.вызов("getTransactionsForAddress",
                       [адрес, {"limit": 1, "transactionDetails": "full",
                                "status": "succeeded",
                                "maxSupportedTransactionVersion": 0}])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:120]}"}
    return {"ok": isinstance(рез, (list, dict)), "why_not": None,
            "вид_ответа": type(рез).__name__}


def подписи_окна(уз: P.Узел, адрес: str, с_ts: float, до_ts: float, *,
                 предел_вызовов: int) -> dict:
    """Подписи адреса в окне -- страницами по 1000, от свежих к старым."""
    из_ = {"подписи": [], "страниц": 0, "предел_уперся": False, "why_not": None}
    до_подписи = None
    while True:
        if уз.вызовов >= предел_вызовов:
            из_["предел_уперся"] = True
            break
        парам = {"limit": 1000}
        if до_подписи:
            парам["before"] = до_подписи
        try:
            стр = уз.вызов("getSignaturesForAddress", [адрес, парам])
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            break
        из_["страниц"] += 1
        if not стр:
            break
        for з in стр:
            bt = з.get("blockTime") or 0
            if bt and bt < с_ts:
                return из_
            if bt and bt >= до_ts:
                continue
            if з.get("err"):
                continue  # статус succeeded: упавшая покупка не покупка
            из_["подписи"].append({"signature": з.get("signature"), "blockTime": bt,
                                    "slot": з.get("slot")})
        до_подписи = стр[-1].get("signature")
        if len(стр) < 1000:
            break
    return из_


def покупки_через_план(уз: P.Узел, адрес: str, с_ts: float, до_ts: float, *,
                       предел_вызовов: int) -> dict:
    """Покупки источника через getTransactionsForAddress: целые транзакции.

    ПОЧЕМУ ЭТО ВАЖНО. Длинный путь стоит один вызов НА КАЖДУЮ транзакцию
    адреса; у лидера их за восемь дней много, и предел вызовов упрётся раньше
    окна -- список получится неполным, а неполный список врёт про "источник
    купил, а мы нет". Этот метод отдаёт до 100 целых транзакций за вызов, то
    есть то же окно в сто раз дешевле. Требование владельца по заданию 2:
    проверять его доступность ПЕРВЫМ вызовом.
    """
    из_ = {"покупки": [], "смотрено": 0, "не_покупки": 0, "страниц": 0,
            "предел_уперся": False, "why_not": None}
    курсор = None
    while True:
        if уз.вызовов >= предел_вызовов:
            из_["предел_уперся"] = True
            break
        парам = {"limit": 100, "transactionDetails": "full", "status": "succeeded",
                 "maxSupportedTransactionVersion": 0}
        if курсор:
            парам["before"] = курсор
        try:
            рез = уз.вызов("getTransactionsForAddress", [адрес, парам], срок=40.0)
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            break
        строки = рез.get("transactions") if isinstance(рез, dict) else рез
        if not строки:
            break
        из_["страниц"] += 1
        последняя = None
        for tx in строки:
            подпись = (((tx.get("transaction") or {}).get("signatures") or [None])[0]
                       if isinstance(tx, dict) else None)
            последняя = подпись or последняя
            bt = tx.get("blockTime") or 0
            if bt and bt < с_ts:
                return из_
            if bt and bt >= до_ts:
                continue
            if (tx.get("meta") or {}).get("err"):
                continue
            из_["смотрено"] += 1
            факт = P.факт_покупки(tx, адрес)
            if not факт.get("tokens_raw") or not факт.get("sol_out") or факт["sol_out"] <= 0:
                из_["не_покупки"] += 1
                continue
            из_["покупки"].append({
                "signature": подпись, "slot": tx.get("slot"),
                "blockTime": bt, "mint": факт.get("mint"),
                "sol_out": факт.get("sol_out"), "tokens_raw": факт.get("tokens_raw")})
            # Транзакция уже в руках -- положим в кэш, чтобы симулятор не
            # спрашивал её второй раз.
            if подпись:
                уз._кэш[подпись] = tx  # noqa: SLF001
        if len(строки) < 100:
            break
        курсор = последняя
        if not курсор:
            break
    return из_


def покупки_источника(уз: P.Узел, адрес: str, подписи: list, *,
                      предел_вызовов: int) -> dict:
    """Из подписей окна оставить ПОКУПКИ: токен пришёл, SOL ушёл."""
    из_ = {"покупки": [], "смотрено": 0, "предел_уперся": False,
            "не_покупки": 0, "отказов": 0}
    for з in подписи:
        if уз.вызовов >= предел_вызовов:
            из_["предел_уперся"] = True
            break
        из_["смотрено"] += 1
        try:
            tx = уз.транзакция(з["signature"])
        except Exception:  # noqa: BLE001
            из_["отказов"] += 1
            continue
        if not tx:
            из_["отказов"] += 1
            continue
        факт = P.факт_покупки(tx, адрес)
        if not факт.get("tokens_raw") or not факт.get("sol_out") or факт["sol_out"] <= 0:
            из_["не_покупки"] += 1
            continue
        из_["покупки"].append({
            "signature": з["signature"], "slot": tx.get("slot") or з.get("slot"),
            "blockTime": tx.get("blockTime") or з.get("blockTime"),
            "mint": факт.get("mint"), "sol_out": факт.get("sol_out"),
            "tokens_raw": факт.get("tokens_raw"),
        })
    return из_


# ----------------------------------------------------------- журнал DBot

def записи_журнала(путь: Path, адреса: dict, с_ts: float, до_ts: float) -> list:
    д = json.loads(путь.read_text(encoding="utf-8"))
    из_ = []
    for т in д:
        ист = т.get("source_address") or ""
        if ист not in адреса:
            continue
        bt = т.get("buy_block_time") or 0
        if not (с_ts <= bt < до_ts):
            continue
        из_.append(т)
    из_.sort(key=lambda т: т.get("buy_block_time") or 0)
    return из_


def почему_не_скопировали(покупка: dict, записи_по_минту: dict, *,
                          порог_sol: float) -> str:
    """Причина словами. Где данных не хватает -- так и сказано."""
    if покупка["sol_out"] < порог_sol:
        return (f"покупка источника {покупка['sol_out']:.4f} SOL ниже порога задачи "
                f"{порог_sol} SOL-экв")
    прежние = записи_по_минту.get(покупка["mint"]) or []
    раньше = [з for з in прежние if (з.get("buy_block_time") or 0) < покупка["blockTime"]]
    if раньше:
        return (f"этот минт уже был куплен нами раньше ({utc(раньше[-1].get('buy_block_time'))}) "
                "-- повтор по тому же минту задача не берёт")
    return "не восстановить по данным журнала: решения детектора DBot нам не отдаёт"


# ----------------------------------------------------------- симулятор

def сверить_числа(пары: list, уз: P.Узел, *, предел_вызовов: int) -> dict:
    ряды, отказы = [], []
    for п in пары:
        if уз.вызовов >= предел_вызовов:
            отказы.append({"mint": п["источник"]["mint"], "why_not": "предел вызовов узла"})
            continue
        з = п["запись"]
        строка = {"mint": п["источник"]["mint"], "task": з.get("task_name"),
                   "источник": (з.get("source_address") or "")[:8],
                   "sol_in": з.get("sol_in"), "наш_utc": utc(з.get("buy_block_time")),
                   "source_sig": п["источник"]["signature"],
                   "our_sig": з.get("buy_signature")}
        if not строка["our_sig"]:
            строка["why_not"] = "в записи журнала нет подписи нашей покупки"
            отказы.append(строка)
            continue
        try:
            tx_ист = уз.транзакция(строка["source_sig"])
            tx_наша = уз.транзакция(строка["our_sig"])
        except Exception as exc:  # noqa: BLE001
            строка["why_not"] = f"узел: {type(exc).__name__}"
            отказы.append(строка)
            continue
        факт = P.факт_покупки(tx_наша, з.get("wallet") or "", строка["mint"])
        строка.update(факт_токенов=факт.get("tokens_raw"), факт_sol=факт.get("sol_out"),
                       наш_слот=факт.get("slot"), слотов_позже=(
                           (факт.get("slot") or 0) - (п["источник"].get("slot") or 0)))
        if not факт.get("tokens_raw"):
            строка["why_not"] = факт.get("why_not") or "токенов по факту нет"
            отказы.append(строка)
            continue
        # РАЗМЕР -- ФАКТИЧЕСКИЙ: сколько SOL реально ушло с кошелька на эту
        # покупку, а не сколько стояло в настройке задачи.
        лампорты = int(round(float(факт.get("sol_out") or 0) * ЛАМПОРТОВ))
        сим = P.наша_покупка_по_симулятору(tx_ист, з.get("source_address") or "",
                                           строка["mint"], лампорты)
        строка.update(сим_токенов=сим.get("tokens_raw"), сим_метод=сим.get("method"),
                       сосредоточенный=сим.get("concentrated"), кривая=сим.get("curve"),
                       программа=сим.get("program"))
        if not сим.get("ok") or not сим.get("tokens_raw"):
            строка["why_not"] = сим.get("why_not") or "симулятор не дал числа"
            отказы.append(строка)
            continue
        строка["расхождение_пп"] = round((факт["tokens_raw"] / сим["tokens_raw"] - 1.0) * 100.0, 3)
        ряды.append(строка)
    абс = sorted(abs(r["расхождение_пп"]) for r in ряды)

    def квантиль(доля):
        if not абс:
            return None
        и = min(len(абс) - 1, int(round(доля * (len(абс) - 1))))
        return round(абс[и], 3)

    причины: dict = {}
    for о in отказы:
        к = (о.get("why_not") or "без причины")[:90]
        причины[к] = причины.get(к, 0) + 1
    return {"сверено": len(ряды), "отказов": len(отказы),
            "медиана_пп": round(statistics.median(абс), 3) if абс else None,
            "p90_пп": квантиль(0.9), "макс_пп": абс[-1] if абс else None,
            "причины_отказов": причины, "ряды": ряды, "отказы": отказы}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--istochniki", required=True,
                   help="адрес=имя через запятую")
    р.add_argument("--s", default="2026-09-18T00:00:00Z")
    р.add_argument("--do", default="2026-09-26T00:00:00Z")
    р.add_argument("--ledger", default=str(КОРЕНЬ / "data" / "solana_trades_all.json"))
    р.add_argument("--porog-sol", type=float, default=2.0,
                   help="порог покупки источника в задачах DBot")
    р.add_argument("--predel-vyzovov", type=int, default=4000)
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "podbivka" / "p1b_sverka.json"))
    а = р.parse_args()

    адреса = {}
    for кус in а.istochniki.split(","):
        кус = кус.strip()
        if not кус:
            continue
        адрес, _, имя = кус.partition("=")
        адреса[адрес.strip()] = имя.strip() or адрес.strip()[:6]
    с_ts, до_ts = в_секунды(а.s), в_секунды(а.do)
    уз = P.Узел()
    начало = time.time()

    журнал = записи_журнала(Path(а.ledger), адреса, с_ts, до_ts)
    план = план_отдаёт_транзакции(уз, next(iter(адреса)))

    по_источнику = {}
    for адрес, имя in адреса.items():
        зап = [т for т in журнал if т.get("source_address") == адрес]
        по_минту: dict = {}
        for т in зап:
            по_минту.setdefault(т.get("mint"), []).append(т)
        if план.get("ok"):
            пк = покупки_через_план(уз, адрес, с_ts, до_ts,
                                     предел_вызовов=а.predel_vyzovov)
            сп = {"подписи": [], "страниц": пк["страниц"],
                  "предел_уперся": пк["предел_уперся"], "why_not": пк["why_not"],
                  "путь": "getTransactionsForAddress"}
        else:
            сп = подписи_окна(уз, адрес, с_ts, до_ts, предел_вызовов=а.predel_vyzovov)
            сп["путь"] = "getSignaturesForAddress + getTransaction"
            пк = покупки_источника(уз, адрес, сп["подписи"], предел_вызовов=а.predel_vyzovov)
        совпало, без_нас = [], []
        занятые = set()
        for п in пк["покупки"]:
            свои = [т for т in (по_минту.get(п["mint"]) or [])
                    if т.get("buy_signature") not in занятые
                    and 0 <= (т.get("buy_block_time") or 0) - (п["blockTime"] or 0) <= ОКНО_КОПИИ_С]
            if свои:
                з = min(свои, key=lambda т: (т.get("buy_block_time") or 0))
                занятые.add(з.get("buy_signature"))
                совпало.append({"источник": п, "запись": з})
            else:
                без_нас.append({**п, "utc": utc(п["blockTime"]),
                                 "почему": почему_не_скопировали(п, по_минту,
                                                                 порог_sol=а.porog_sol)})
        без_источника = []
        for т in зап:
            if т.get("buy_signature") in занятые:
                continue
            без_источника.append({
                "mint": т.get("mint"), "task": т.get("task_name"),
                "наш_utc": utc(т.get("buy_block_time")), "sol_in": т.get("sol_in"),
                "our_sig": т.get("buy_signature"),
                "почему": ("покупки источника по этому минту на цепи в окне нет: "
                            + ("перебор цепи уперся в предел вызовов"
                               if (сп["предел_уперся"] or пк["предел_уперся"])
                               else "источник указан журналом DBot (source_match_method="
                                    + str(т.get("source_match_method")) + "), "
                                    "своей покупки под нашу копию на цепи не нашлось")),
            })
        по_источнику[имя] = {
            "адрес": адрес,
            "записей_журнала": len(зап),
            "путь_по_цепи": сп.get("путь"),
            "подписей_в_окне": len(сп["подписи"]), "страниц_подписей": сп["страниц"],
            "транзакций_посмотрено": пк["смотрено"], "не_покупок": пк["не_покупки"],
            "покупок_источника": len(пк["покупки"]),
            "совпало": len(совпало), "источник_без_нас": len(без_нас),
            "мы_без_источника": len(без_источника),
            "предел_уперся": bool(сп["предел_уперся"] or пк["предел_уперся"]),
            "why_not_подписи": сп["why_not"],
            "список_источник_без_нас": без_нас,
            "список_мы_без_источника": без_источника,
            "_пары": совпало,
        }

    все_пары = [п for и in по_источнику.values() for п in и["_пары"]]
    числа = сверить_числа(все_пары, уз, предел_вызовов=а.predel_vyzovov)
    for и in по_источнику.values():
        и.pop("_пары", None)

    итог = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "окно": {"с": а.s, "до": а.do},
        "план_getTransactionsForAddress": план,
        "порог_задачи_sol": а.porog_sol,
        "по_источнику": по_источнику,
        "числа": {к: v for к, v in числа.items() if к not in ("ряды", "отказы")},
        "ряды_чисел": числа["ряды"],
        "отказы_чисел": числа["отказы"],
        "вызовов_узла": уз.вызовов, "ошибок_узла": уз.ошибок,
        "секунд": round(time.time() - начало, 1),
        "предел_вызовов": а.predel_vyzovov,
    }
    Path(а.out).parent.mkdir(parents=True, exist_ok=True)
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    кратко = {к: v for к, v in итог.items()
              if к not in ("ряды_чисел", "отказы_чисел", "по_источнику")}
    кратко["по_источнику"] = {и: {к: v for к, v in д.items()
                                   if not к.startswith("список_")}
                               for и, д in по_источнику.items()}
    print(json.dumps(кратко, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
