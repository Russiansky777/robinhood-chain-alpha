#!/usr/bin/env python3
"""Место в блоке ОТНОСИТЕЛЬНО ИСТОЧНИКА и толпа между нами. Только чтение.

Вопрос владельца буквально: сколько толпы мы обгоняем. Один слот с
источником (S+0) сам по себе ничего не значит -- порядок внутри блока
решает валидатор, и на всех трёх наших S+0 мы стояли ПОЗЖЕ источника.
Поэтому здесь считаются три числа по фактам цепи:

  1. наш индекс минус индекс источника (при S+0 -- в одном блоке);
  2. сколько ЧУЖИХ покупок того же минта прошло между источником и нами:
     в блоке источника после него и в нашем блоке до нас;
  3. то же для кошелька DBot -- он копирует ту же сделку, и его место в
     блоке это прямая мерка "кто быстрее" без всяких оговорок.

Чужая покупка -- транзакция, где остаток этого минта ВЫРОС у владельца,
который не источник, не мы и не DBot. Считается по preTokenBalances и
postTokenBalances из getBlock с transactionDetails="accounts": полные
транзакции блока для этого не нужны.

getBlock по цене дороже обычного вызова, поэтому замер идёт по позициям
списком, а не в горячем пути.
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

# Кошельки задач DBot: из снимка конфига (walletAddress), не из догадок.
КОШЕЛЬКИ_DBOT_ПО_УМОЛЧАНИЮ = {
    "BATCH-3": "BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu",
    "BATCH-5": "5Y8h877swoTzTdc8in9hU3SvXXVv1q9p19Y85tAsdqBv",
}


def блок_со_счетами(helius, слот: int) -> dict:
    """Блок с подписями и балансами. transactionDetails="accounts".

    Уровень "signatures" даёт только порядок, а нам нужно ещё и понять,
    какие из транзакций -- покупки того же минта. Уровень "full" для этого
    излишен: в "accounts" уже есть pre/postTokenBalances.
    """
    if not isinstance(слот, int):
        return {"known": False, "why_not": "слот не задан"}
    try:
        блок = helius.call("getBlock", [слот, {
            "encoding": "json", "transactionDetails": "accounts",
            "rewards": False, "maxSupportedTransactionVersion": BD.ПОТОЛОК_ВЕРСИИ_TX}])
    except Exception as exc:  # noqa: BLE001
        return {"known": False,
                "why_not": f"getBlock не отдался: {type(exc).__name__}: {str(exc)[:120]}"}
    tx = (блок or {}).get("transactions")
    if not tx:
        return {"known": False, "why_not": "в ответе getBlock нет транзакций"}
    return {"known": True, "slot": слот, "transactions": tx, "total": len(tx)}


def _подпись(t: dict) -> str | None:
    подписи = ((t or {}).get("transaction") or {}).get("signatures") or []
    return подписи[0] if подписи else None


def индекс_подписи(блок: dict, подпись: str) -> int | None:
    if not (блок.get("known") and подпись):
        return None
    for i, t in enumerate(блок["transactions"]):
        if _подпись(t) == подпись:
            return i
    return None


def _вырос_минт(t: dict, минт: str) -> set:
    """Владельцы, у которых остаток этого минта ВЫРОС в этой транзакции."""
    meta = (t or {}).get("meta") or {}
    if meta.get("err") is not None:
        return set()

    def карта(записи):
        out = {}
        for b in записи or []:
            if not isinstance(b, dict) or b.get("mint") != минт:
                continue
            сырое = (b.get("uiTokenAmount") or {}).get("amount")
            try:
                out[(b.get("owner"), b.get("accountIndex"))] = int(сырое)
            except (TypeError, ValueError):
                continue
        return out

    до, после = карта(meta.get("preTokenBalances")), карта(meta.get("postTokenBalances"))
    выросли = set()
    for ключ, сколько in после.items():
        if сколько > до.get(ключ, 0):
            выросли.add(ключ[0])
    return {в for в in выросли if в}


def покупки_минта(блок: dict, минт: str, *, с_индекса: int, до_индекса: int | None,
                   свои: tuple) -> dict:
    """Чужие покупки минта в интервале индексов (с_индекса, до_индекса).

    Границы ИСКЛЮЧИТЕЛЬНЫЕ: сам источник и сами мы в толпу не входят.
    до_индекса=None -- до конца блока.
    """
    if not блок.get("known"):
        return {"known": False, "why_not": блок.get("why_not")}
    свои_мн = {s for s in свои if s}
    конец = блок["total"] if до_индекса is None else до_индекса
    счёт, кто = 0, []
    for i in range(max(0, с_индекса + 1), min(конец, блок["total"])):
        владельцы = _вырос_минт(блок["transactions"][i], минт)
        чужие = владельцы - свои_мн
        if чужие:
            счёт += 1
            if len(кто) < 10:
                кто.append({"index": i, "signature": _подпись(блок["transactions"][i]),
                             "owners": sorted(чужие)[:3]})
    return {"known": True, "count": счёт, "from_index": с_индекса,
             "to_index": (конец if до_индекса is not None else блок["total"]),
             "examples": кто}


def кто_купил_минт(блок: dict, минт: str, кошелёк: str) -> dict:
    """Индекс транзакции, в которой этот кошелёк купил минт."""
    if not блок.get("known") or not кошелёк:
        return {"known": False, "why_not": блок.get("why_not") or "кошелёк не задан"}
    for i, t in enumerate(блок["transactions"]):
        if кошелёк in _вырос_минт(t, минт):
            return {"known": True, "index": i, "total": блок["total"],
                     "share": round(i / блок["total"], 4),
                     "signature": _подпись(t)}
    return {"known": False, "total": блок["total"],
             "why_not": "покупки этого минта этим кошельком в блоке нет"}


def покупатели_минта(helius, минт: str, слоты: list, *,
                      известные: dict | None = None) -> dict:
    """Кто покупал этот минт в этих слотах: индекс, кошелёк, чей он.

    Нужно, когда известна только покупка DBot: источника в его записи нет,
    а по цепи он находится сам -- это кошелёк, купивший тот же минт в том же
    или предыдущем слоте. Заодно это и есть замер толпы на живом событии.
    """
    известные = известные or {}
    строки = []
    прочитано = 0
    for слот in слоты:
        б = блок_со_счетами(helius, слот)
        if not б.get("known"):
            строки.append({"slot": слот, "known": False, "why_not": б.get("why_not")})
            continue
        прочитано += 1
        for i, t in enumerate(б["transactions"]):
            владельцы = _вырос_минт(t, минт)
            for вл in sorted(владельцы):
                строки.append({"slot": слот, "index": i, "total": б["total"],
                                "share": round(i / б["total"], 4),
                                "owner": вл, "whose": известные.get(вл),
                                "signature": _подпись(t)})
    return {"mint": минт, "blocks_read": прочитано, "rows": строки,
             "buyers": len({r.get("owner") for r in строки if r.get("owner")})}


def место_относительно_источника(helius, *, минт: str, слот_источника: int,
                                  подпись_источника: str, слот_наш: int | None,
                                  подпись_наша: str | None,
                                  кошелёк_наш: str | None = None,
                                  кошелёк_dbot: str | None = None) -> dict:
    """Полный замер по одной сделке. Блоки читаются по одному разу."""
    итог = {"mint": минт, "source_slot": слот_источника, "our_slot": слот_наш,
             "slot_delta": ((слот_наш - слот_источника)
                             if isinstance(слот_наш, int)
                             and isinstance(слот_источника, int) else None)}
    блоки = {}
    for слот in {x for x in (слот_источника, слот_наш) if isinstance(x, int)}:
        блоки[слот] = блок_со_счетами(helius, слот)
    би = блоки.get(слот_источника) or {"known": False, "why_not": "блок источника не читан"}
    итог["source_index"] = индекс_подписи(би, подпись_источника)
    итог["source_block_total"] = би.get("total")
    итог["blocks_read"] = len(блоки)
    if не_знаем := (не_знаем_почему(би)):
        итог["source_why_not"] = не_знаем

    бн = блоки.get(слот_наш) if isinstance(слот_наш, int) else None
    итог["our_index"] = индекс_подписи(бн, подпись_наша) if бн else None
    итог["our_block_total"] = (бн or {}).get("total")
    if бн and not бн.get("known"):
        итог["our_why_not"] = бн.get("why_not")

    if (итог["slot_delta"] == 0 and итог["source_index"] is not None
            and итог["our_index"] is not None):
        итог["index_delta_same_block"] = итог["our_index"] - итог["source_index"]
        итог["ahead_of_source"] = итог["index_delta_same_block"] < 0

    # Толпа между источником и нами.
    #
    # ВАЖНО про S+2 и дальше: между блоком источника и нашим есть ПРОМЕЖУТОЧНЫЕ
    # блоки, и покупки в них -- тоже те, кто нас обогнал. Пока они не
    # считались, замер по нашей же первой сделке показал "толпа 0" там, где
    # ручной разбор нашёл три чужих покупки в промежуточном слоте. Число,
    # которое молча не видит целый блок, хуже отсутствующего.
    свои = (кошелёк_наш, кошелёк_dbot, подпись_источника)
    if итог["source_index"] is not None:
        if итог["slot_delta"] == 0 and итог["our_index"] is not None:
            итог["crowd_between"] = покупки_минта(
                би, минт, с_индекса=итог["source_index"],
                до_индекса=итог["our_index"], свои=свои)
        elif бн is not None and итог["our_index"] is not None:
            хвост = покупки_минта(би, минт, с_индекса=итог["source_index"],
                                   до_индекса=None, свои=свои)
            голова = покупки_минта(бн, минт, с_индекса=-1,
                                    до_индекса=итог["our_index"], свои=свои)
            середина = []
            все_знаем = bool(хвост.get("known") and голова.get("known"))
            for слот in range(слот_источника + 1, слот_наш):
                б = блок_со_счетами(helius, слот)
                итог["blocks_read"] = итог.get("blocks_read", 0) + 1
                if not б.get("known"):
                    все_знаем = False
                    середина.append({"slot": слот, "known": False,
                                      "why_not": б.get("why_not")})
                    continue
                ч = покупки_минта(б, минт, с_индекса=-1, до_индекса=None, свои=свои)
                ч["slot"] = слот
                середина.append(ч)
            итог["crowd_between"] = {
                "known": все_знаем,
                "count": ((хвост.get("count") or 0) + (голова.get("count") or 0)
                           + sum(ч.get("count") or 0 for ч in середина)),
                "tail_of_source_block": хвост,
                "middle_blocks": середина,
                "head_of_our_block": голова}
    if кошелёк_dbot:
        для_dbot = {}
        for имя, б in (("source_block", би), ("our_block", бн)):
            if б is not None:
                для_dbot[имя] = кто_купил_минт(б, минт, кошелёк_dbot)
        итог["dbot"] = для_dbot
        нашли = [v for v in для_dbot.values() if v.get("known")]
        if нашли and итог["source_index"] is not None:
            d = нашли[0]
            итог["dbot_index_delta_same_block"] = (
                d["index"] - итог["source_index"]
                if для_dbot.get("source_block", {}).get("known") else None)
    return итог


def не_знаем_почему(блок: dict) -> str | None:
    return None if блок.get("known") else (блок.get("why_not") or "неизвестно")


# ------------------------------------------------------------- самопроверка

def self_test() -> None:
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    МИНТ = "МИНТ"

    def tx(подпись, *, вырос=(), упал=(), ошибка=None):
        pre, post = [], []
        for i, вл in enumerate(вырос):
            pre.append({"owner": вл, "mint": МИНТ, "accountIndex": i,
                        "uiTokenAmount": {"amount": "0"}})
            post.append({"owner": вл, "mint": МИНТ, "accountIndex": i,
                         "uiTokenAmount": {"amount": "100"}})
        for i, вл in enumerate(упал, start=50):
            pre.append({"owner": вл, "mint": МИНТ, "accountIndex": i,
                        "uiTokenAmount": {"amount": "100"}})
            post.append({"owner": вл, "mint": МИНТ, "accountIndex": i,
                         "uiTokenAmount": {"amount": "0"}})
        return {"transaction": {"signatures": [подпись]},
                "meta": {"err": ошибка, "preTokenBalances": pre,
                         "postTokenBalances": post}}

    блок_и = {"known": True, "slot": 100, "total": 6, "transactions": [
        tx("A", вырос=("ЧУЖОЙ1",)),
        tx("ИСТОЧНИК", вырос=("SRC",)),
        tx("B", вырос=("ЧУЖОЙ2",)),
        tx("C", вырос=("ЧУЖОЙ3",), ошибка={"InstructionError": [0, "X"]}),
        tx("DBOT", вырос=("DBOTW",)),
        tx("НАША", вырос=("МЫ",)),
    ]}

    chk("индекс источника найден", индекс_подписи(блок_и, "ИСТОЧНИК") == 1,
        индекс_подписи(блок_и, "ИСТОЧНИК"))
    chk("чужой подписи нет -- None", индекс_подписи(блок_и, "НЕТУ") is None)

    т = покупки_минта(блок_и, МИНТ, с_индекса=1, до_индекса=5,
                       свои=("МЫ", "DBOTW", "SRC"))
    chk("толпа между источником и нами -- одна чужая покупка",
        т["count"] == 1 and т["examples"][0]["signature"] == "B", т)
    chk("неудачная транзакция в толпу не идёт",
        all(x["signature"] != "C" for x in т["examples"]), т["examples"])
    chk("сам источник и мы в толпу не входят",
        all(x["signature"] not in ("ИСТОЧНИК", "НАША") for x in т["examples"]), т)

    d = кто_купил_минт(блок_и, МИНТ, "DBOTW")
    chk("покупка DBot в блоке найдена по кошельку", d["known"] and d["index"] == 4, d)
    chk("и доля посчитана", d["share"] == round(4 / 6, 4), d)
    chk("кошелька без покупки в блоке нет",
        кто_купил_минт(блок_и, МИНТ, "НИКТО")["known"] is False)

    # Продажа (остаток упал) покупкой не считается
    блок_прод = {"known": True, "slot": 100, "total": 2, "transactions": [
        tx("ИСТОЧНИК", вырос=("SRC",)), tx("ПРОДАЖА", упал=("ЧУЖОЙ",))]}
    chk("продажа чужого в толпу не идёт",
        покупки_минта(блок_прод, МИНТ, с_индекса=0, до_индекса=None,
                       свои=("SRC",))["count"] == 0)

    class HeliusБлоки:
        def __init__(self, блоки):
            self.блоки = блоки
            self.вызовы = []

        def call(self, метод, параметры):
            assert метод == "getBlock"
            self.вызовы.append(параметры[0])
            б = self.блоки.get(параметры[0])
            if б is None:
                raise RuntimeError("нет блока")
            return {"transactions": б["transactions"]}

    # S+0: мы в том же блоке, но ПОЗЖЕ источника
    h = HeliusБлоки({100: блок_и})
    r = место_относительно_источника(
        h, минт=МИНТ, слот_источника=100, подпись_источника="ИСТОЧНИК",
        слот_наш=100, подпись_наша="НАША", кошелёк_наш="МЫ", кошелёк_dbot="DBOTW")
    chk("S+0: разница индексов посчитана",
        r["index_delta_same_block"] == 4 and r["ahead_of_source"] is False, r)
    chk("S+0: толпа между источником и нами -- 1", r["crowd_between"]["count"] == 1,
        r["crowd_between"])
    chk("место DBot в блоке источника найдено",
        r["dbot"]["source_block"]["index"] == 4, r["dbot"])
    chk("разница индексов DBot к источнику посчитана",
        r["dbot_index_delta_same_block"] == 3, r.get("dbot_index_delta_same_block"))
    chk("один блок -- один вызов getBlock", h.вызовы == [100], h.вызовы)

    # S+1: хвост блока источника плюс голова нашего
    блок_наш = {"known": True, "slot": 101, "total": 4, "transactions": [
        tx("X", вырос=("ЧУЖОЙ4",)), tx("НАША2", вырос=("МЫ",)),
        tx("Y", вырос=("ЧУЖОЙ5",)), tx("Z", вырос=("ЧУЖОЙ6",))]}
    h2 = HeliusБлоки({100: блок_и, 101: блок_наш})
    r2 = место_относительно_источника(
        h2, минт=МИНТ, слот_источника=100, подпись_источника="ИСТОЧНИК",
        слот_наш=101, подпись_наша="НАША2", кошелёк_наш="МЫ", кошелёк_dbot="DBOTW")
    chk("S+1: разницы индексов в одном блоке нет",
        "index_delta_same_block" not in r2, r2.get("index_delta_same_block"))
    chk("S+1: толпа = хвост блока источника + голова нашего (1 + 1)",
        r2["crowd_between"]["count"] == 2, r2["crowd_between"])
    chk("S+1: прочитано два блока", r2["blocks_read"] == 2, r2["blocks_read"])

    # S+2: между блоками источника и нашим есть ПРОМЕЖУТОЧНЫЙ блок, и покупки
    # в нём -- тоже те, кто нас обогнал. Пока он не считался, замер показывал
    # "толпа 0" там, где ручной разбор нашёл три чужих покупки.
    блок_середина = {"known": True, "slot": 101, "total": 3, "transactions": [
        tx("M1", вырос=("ЧУЖОЙ7",)), tx("M2", вырос=("ЧУЖОЙ8",)),
        tx("M3", вырос=("ЧУЖОЙ9",))]}
    блок_наш2 = {"known": True, "slot": 102, "total": 2, "transactions": [
        tx("K", вырос=("ЧУЖОЙ10",)), tx("НАША3", вырос=("МЫ",))]}
    h4 = HeliusБлоки({100: блок_и, 101: блок_середина, 102: блок_наш2})
    r4 = место_относительно_источника(
        h4, минт=МИНТ, слот_источника=100, подпись_источника="ИСТОЧНИК",
        слот_наш=102, подпись_наша="НАША3", кошелёк_наш="МЫ", кошелёк_dbot="DBOTW")
    # хвост блока источника (1) + промежуточный блок целиком (3) + голова
    # нашего блока (1) = 5
    chk("S+2: считаются хвост, весь промежуточный блок и голова нашего",
        r4["crowd_between"]["count"] == 5
        and r4["crowd_between"]["tail_of_source_block"]["count"] == 1
        and r4["crowd_between"]["middle_blocks"][0]["count"] == 3
        and r4["crowd_between"]["head_of_our_block"]["count"] == 1,
        r4["crowd_between"]["count"])
    chk("S+2: промежуточные блоки видны отдельным списком",
        len(r4["crowd_between"]["middle_blocks"]) == 1
        and r4["crowd_between"]["middle_blocks"][0]["slot"] == 101,
        r4["crowd_between"].get("middle_blocks"))
    chk("S+2: прочитано три блока", r4["blocks_read"] == 3, r4["blocks_read"])

    # Непрочитанный промежуточный блок делает счёт НЕ полным, и это видно
    h5 = HeliusБлоки({100: блок_и, 102: блок_наш2})
    r5 = место_относительно_источника(
        h5, минт=МИНТ, слот_источника=100, подпись_источника="ИСТОЧНИК",
        слот_наш=102, подпись_наша="НАША3", кошелёк_наш="МЫ")
    chk("промежуточный блок не прочитан -- счёт помечен неполным",
        r5["crowd_between"]["known"] is False, r5["crowd_between"])

    # Узел не отдал блок -- это сказано, а не посчитано нулём
    h3 = HeliusБлоки({})
    r3 = место_относительно_источника(
        h3, минт=МИНТ, слот_источника=100, подпись_источника="ИСТОЧНИК",
        слот_наш=None, подпись_наша=None)
    chk("узел не отдал блок -- причина названа",
        r3.get("source_why_not") and r3.get("source_index") is None, r3)
    chk("и толпа не считается нулём", "crowd_between" not in r3, r3.get("crowd_between"))

    # Кто покупал минт в слотах: источник находится по цепи, когда в записи
    # DBot его нет.
    h4 = HeliusБлоки({100: блок_и, 101: {"known": True, "slot": 101, "total": 1,
                                          "transactions": [tx("ДРУГАЯ", вырос=("ЕЩЁ",))]}})
    п = покупатели_минта(h4, МИНТ, [100, 101],
                          известные={"DBOTW": "BATCH-3"})
    покупатели = {r["owner"] for r in п["rows"] if r.get("owner")}
    chk("покупатели минта найдены по всем слотам",
        покупатели == {"ЧУЖОЙ1", "SRC", "ЧУЖОЙ2", "DBOTW", "МЫ", "ЕЩЁ"}, покупатели)
    chk("кошелёк DBot помечен своей задачей",
        any(r.get("whose") == "BATCH-3" for r in п["rows"]), п["rows"][:3])
    chk("неудачная транзакция в покупатели не попала",
        all(r.get("signature") != "C" for r in п["rows"]))
    chk("блоков прочитано два", п["blocks_read"] == 2, п["blocks_read"])
    п_нет = покупатели_минта(HeliusБлоки({}), МИНТ, [100])
    chk("нет блока -- строка с причиной, а не тишина",
        п_нет["rows"] and п_нет["rows"][0].get("known") is False, п_нет)

    print(f"самопроверка места в блоке: {всего[1]}/{всего[0]}"
          f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    if всего[1] != всего[0]:
        raise SystemExit(1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--state-dir", default=None)
    p.add_argument("--out", default="data/bloom_block_position.json")
    p.add_argument("--limit", type=int, default=6,
                   help="сколько последних НАСТОЯЩИХ позиций замерить")
    p.add_argument("--mint-buyers", default=None,
                   help="режим разбора: кто покупал этот минт в указанных слотах")
    p.add_argument("--slots", default=None,
                   help="слоты через запятую или диапазон A-B (для --mint-buyers)")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return 0

    helius = BD.Helius(служба="bloom_block_position")

    if a.mint_buyers:
        слоты = []
        for часть in (a.slots or "").split(","):
            часть = часть.strip()
            if not часть:
                continue
            if "-" in часть:
                с, по = часть.split("-", 1)
                слоты.extend(range(int(с), int(по) + 1))
            else:
                слоты.append(int(часть))
        if not слоты:
            print("нужны --slots")
            return 2
        итог = покупатели_минта(helius, a.mint_buyers, слоты,
                                 известные={v: k for k, v in
                                             КОШЕЛЬКИ_DBOT_ПО_УМОЛЧАНИЮ.items()})
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        ST.atomic_write_json(Path(a.out), {
            "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mint_buyers": итог})
        print(json.dumps(итог, ensure_ascii=False, indent=1)[:6000])
        return 0

    state = ST.ExecState(base=Path(a.state_dir)) if a.state_dir else ST.ExecState()
    свои = state.positions()
    # Упавшие по цепи покупки в замер места в блоке НЕ идут: у транзакции,
    # которая не села, "место относительно источника" -- не число, а
    # видимость. 24.09 упавшая покупка на 0.2 SOL имела слот и попала бы
    # сюда как обычная посадка.
    все_свои = [p for p in свои.values() if ST.is_real_mode(p.get("mode"))]
    упавшие = [p for p in все_свои if p.get("chain_ok") is False]
    позиции = [p for p in все_свои if p.get("chain_ok") is not False]
    if упавшие:
        print(f"упавших по цепи покупок не берём в замер: {len(упавшие)}")
    позиции.sort(key=lambda p: float(p.get("ts_intent") or 0))
    вых = []
    for поз in позиции[-a.limit:]:
        подписи = поз.get("signatures") or []
        задача = поз.get("source_task")
        вых.append({
            "client_order_id": поз.get("client_order_id"),
            "mint": поз.get("mint"),
            "source_task": задача,
            "measure": место_относительно_источника(
                helius, минт=поз.get("mint"),
                слот_источника=поз.get("source_slot"),
                подпись_источника=поз.get("source_sig"),
                слот_наш=поз.get("our_slot"),
                подпись_наша=(подписи[0] if подписи else None),
                кошелёк_наш=поз.get("wallet"),
                кошелёк_dbot=КОШЕЛЬКИ_DBOT_ПО_УМОЛЧАНИЮ.get(задача or "")),
        })
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    ST.atomic_write_json(Path(a.out), {"built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             "rows": вых})
    print(json.dumps(вых, ensure_ascii=False, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
