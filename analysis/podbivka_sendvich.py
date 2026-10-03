#!/usr/bin/env python3
"""Сэндвичи на наших покупках: тот же подписант покупает перед нами и продаёт после в том же блоке.

Вопрос владельца 03.10 (до подъёма билета): по нашим сделкам из выгрузки Code-1 -- была ли наша покупка в
сэндвиче, сколько SOL с нас сняли, разбивка по билету 0.1 / 0.3 / 0.5.

Как считается. По каждой сделке читается её блок (`getBlock`, transactionDetails=accounts: счета с признаком
подписанта, pre/postTokenBalances с владельцами, порядок в блоке). В блоке по нашему минту считается дельта
токена по владельцам каждой транзакции; сторона пула -- владелец, который встречается в большинстве таких
транзакций. Сэндвич: один и тот же подписант купил этот минт ДО нас и продал ПОСЛЕ нас в том же блоке
(«прямо перед/после» -- соседние по индексу; «в блоке» -- где угодно до и после).
Сколько сняли с нас: по состоянию пула перед его покупкой считается, сколько токенов мы получили бы без
его вклинивания. Коэффициент f пула берётся из НАШЕЙ же сделки (из фактически полученных токенов и состояния
пула перед нами) -- поэтому комиссия пула не влияет на отношение. Потеря -- в п.п. билета и в SOL.
Выход: data/podbivka/sendvich.json, docs/podbivka_2026-10-03_sendvichi.md.
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
ВЕРСИЯ_TX = 1
НАШИ = {"4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x", "21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w"}


def сделки() -> list:
    из_ = []
    for f in sorted((П / "sdelki").glob("sdelki_polosy_*.json")):
        for r in json.loads(f.read_text(encoding="utf-8")).get("ряды") or []:
            if r.get("buy_sig") and r.get("landed_slot") and r.get("mint"):
                r["_файл"] = f.name
                из_.append(r)
    return из_


def подписант(тх: dict) -> str | None:
    кл = ((тх.get("transaction") or {}).get("accountKeys") or [])
    for k in кл:
        if isinstance(k, dict) and k.get("signer"):
            return k.get("pubkey")
    return (кл[0].get("pubkey") if кл and isinstance(кл[0], dict) else None)


def лампорты(тх: dict) -> dict:
    """Дельта лампортов по адресам счётов -- нужна там, где котировка нативная (кривая pump.fun)."""
    кл = ((тх.get("transaction") or {}).get("accountKeys") or [])
    плоские = [k.get("pubkey") if isinstance(k, dict) else k for k in кл]
    м = тх.get("meta") or {}
    пре, пост = м.get("preBalances") or [], м.get("postBalances") or []
    из_ = {}
    for и, p in enumerate(плоские):
        if p and и < len(пре) and и < len(пост):
            из_[p] = из_.get(p, 0) + (пост[и] - пре[и]) / 1e9
    return из_


def дельты(тх: dict, минт: str) -> tuple[dict, dict]:
    м = тх.get("meta") or {}
    пре = {(b.get("accountIndex"), b.get("mint")): b for b in (м.get("preTokenBalances") or [])}
    пост = {(b.get("accountIndex"), b.get("mint")): b for b in (м.get("postTokenBalances") or [])}
    ток: dict = {}
    квот: dict = {}
    сост: dict = {}
    for кл in set(пре) | set(пост):
        a, b = пре.get(кл), пост.get(кл)
        вл = (b or a) .get("owner")
        до = float(((a or {}).get("uiTokenAmount") or {}).get("uiAmountString") or 0)
        после = float(((b or {}).get("uiTokenAmount") or {}).get("uiAmountString") or 0)
        if кл[1] == минт:
            ток[вл] = round(ток.get(вл, 0.0) + (после - до), 9)
            сост.setdefault(вл, {})["ток_до"] = сост.get(вл, {}).get("ток_до", 0.0) + до
        elif кл[1] == WSOL:
            квот[вл] = round(квот.get(вл, 0.0) + (после - до), 9)
            сост.setdefault(вл, {})["квот_до"] = сост.get(вл, {}).get("квот_до", 0.0) + до
    return {"ток": ток, "квот": квот, "состояние_до": сост}, м


def блок_разбор(тр: list, минт: str) -> list:
    """По транзакциям блока -- дельты нашего минта, подписант и состояние пула перед транзакцией."""
    ряды = []
    for и, тх in enumerate(тр):
        д, м = дельты(тх, минт)
        if not д["ток"]:
            ряды.append(None)
            continue
        подписи = (тх.get("transaction") or {}).get("signatures") or []
        ряды.append({"и": и, "подпись": подписи[0] if подписи else None, "подписант": подписант(тх),
                     "ток": д["ток"], "квот": д["квот"], "состояние_до": д["состояние_до"],
                     "лам": лампорты(тх), "err": (м.get("err") is not None)})
    return ряды


def сторона_пула(ряды: list) -> str | None:
    счёт: collections.Counter = collections.Counter()
    всего = 0
    for r in ряды:
        if not r:
            continue
        всего += 1
        for вл in r["ток"]:
            счёт[вл] += 1
    if not счёт or not всего:
        return None
    вл, n = счёт.most_common(1)[0]
    return вл if n / всего >= 0.5 else None


def нога_sol(r: dict, пул: str | None) -> float | None:
    """SOL, вошедший в пул (покупка) или вышедший (продажа): у кривой -- лампорты пула, у AMM -- WSOL пула."""
    if not пул:
        return None
    л = (r.get("лам") or {}).get(пул)
    if л:
        return л
    к = (r.get("квот") or {}).get(пул)
    return к if к else None


def сделка_пула(r: dict, пул: str | None) -> dict | None:
    """Чья покупка/продажа и на сколько SOL: токены -- не пула, SOL -- по ноге пула."""
    ток = {вл: d for вл, d in (r.get("ток") or {}).items() if вл != пул and abs(d) > 0}
    if not ток:
        return None
    кто = max(ток, key=lambda вл: abs(ток[вл]))
    д = ток[кто]
    sol = нога_sol(r, пул)
    if sol is None:
        return None
    return {"кто": кто, "токенов": д, "sol": abs(sol), "покупка": д > 0,
            "цена": (abs(sol) / d if d > 0 else None)}


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--predel", type=int, default=0, help="сколько сделок считать (0 -- все)")
    р.add_argument("--okno", type=int, default=10, help="сэндвич: в пределах N мест блока до и после нас")
    а = р.parse_args()
    сд = сделки()
    if а.predel:
        сд = сд[:а.predel]
    уз = S.Узел()
    из_, счёт = [], {"сделок": len(сд), "блоков": 0, "не_отдано": 0, "нашей_не_найдено": 0, "без_пула": 0}
    with уз.на("helius"):
        for r in сд:
            сл = r["landed_slot"]
            б = уз.вызов("getBlock", [сл, {"transactionDetails": "accounts", "rewards": False,
                                           "maxSupportedTransactionVersion": ВЕРСИЯ_TX}], срок=120.0)
            if not б:
                счёт["не_отдано"] += 1
                continue
            счёт["блоков"] += 1
            тр = б.get("transactions") or []
            ряды = блок_разбор(тр, r["mint"])
            наш = next((x for x in ряды if x and x["подпись"] == r["buy_sig"]), None)
            if not наш:
                счёт["нашей_не_найдено"] += 1
                из_.append({"cid": r["cid"], "why_not": "нашей покупки нет в блоке по подписи",
                            "слот": сл, "билет": r.get("size_sol")})
                continue
            пул = сторона_пула(ряды)
            if not пул:
                счёт["без_пула"] += 1
            наш_кош = next((вл for вл, d in наш["ток"].items() if d > 0 and вл != пул), None)
            наши_токены = наш["ток"].get(наш_кош or "", 0.0)
            # состояние пула перед нашей покупкой
            x1 = (наш["состояние_до"].get(пул) or {}).get("квот_до") if пул else None
            y1 = (наш["состояние_до"].get(пул) or {}).get("ток_до") if пул else None
            билет = r.get("sol_in") or r.get("size_sol")
            f_эфф = None
            if x1 and y1 and билет and наши_токены and y1 > наши_токены:
                f_эфф = наши_токены * x1 / (билет * (y1 - наши_токены))
            # наша цена: SOL по ноге пула на наши токены
            наша_нога = сделка_пула(наш, пул)
            наша_цена = (наша_нога or {}).get("цена")
            # сэндвич: тот же подписант купил ДО нас и продал ПОСЛЕ нас, тот же пул, в окне ОКНО по индексу
            до = [x for x in ряды[:наш["и"]] if x and not x["err"] and наш["и"] - x["и"] <= а.okno]
            после = [x for x in ряды[наш["и"] + 1:] if x and not x["err"] and x["и"] - наш["и"] <= а.okno]
            сэнд = []
            for a_ in до:
                s = a_["подписант"]
                if not s or s in НАШИ or s == наш["подписант"]:
                    continue
                сд_а = сделка_пула(a_, пул)
                if not (сд_а and сд_а["покупка"] and сд_а["кто"] == s):
                    continue
                for b_ in после:
                    if b_["подписант"] != s:
                        continue
                    сд_б = сделка_пула(b_, пул)
                    if not (сд_б and not сд_б["покупка"]):
                        continue
                    надбавка = (round((наша_цена / сд_а["цена"] - 1) * 100, 3)
                                if (наша_цена and сд_а.get("цена")) else None)
                    сэнд.append({"подписант": s, "индекс_до": a_["и"], "индекс_после": b_["и"],
                                 "мест_до_нас": наш["и"] - a_["и"], "мест_после_нас": b_["и"] - наш["и"],
                                 "соседний_до": наш["и"] - a_["и"] == 1,
                                 "соседний_после": b_["и"] - наш["и"] == 1,
                                 "его_sol_вход": round(сд_а["sol"], 6), "его_sol_выход": round(сд_б["sol"], 6),
                                 "его_итог_sol": round(сд_б["sol"] - сд_а["sol"], 6),
                                 "его_токенов_вход": сд_а["токенов"], "его_токенов_выход": сд_б["токенов"],
                                 "наша_цена": наша_цена, "его_цена": сд_а.get("цена"),
                                 "наша_надбавка_пп": надбавка,
                                 "наша_потеря_sol": (round(билет * надбавка / 100 / (1 + надбавка / 100), 6)
                                                     if (надбавка and билет) else None)})
            из_.append({"cid": r["cid"], "utc": r.get("utc"), "группа": r.get("group"),
                        "билет": r.get("size_sol"), "sol_in": билет, "слот": сл, "минт": r["mint"],
                        "наш_индекс": наш["и"], "в_блоке": len(тр), "пул": пул, "наш_кошелёк": наш_кош,
                        "наши_токены": наши_токены, "f_эфф": round(f_эфф, 6) if f_эфф else None,
                        "наша_цена": наша_цена, "наш_sol_в_пул": (наша_нога or {}).get("sol"),
                        "окно_мест": а.okno,
                        "покупок_до_нас": sum(1 for x in до if any(d > 0 for вл, d in x["ток"].items() if вл != пул)),
                        "сэндвичи": сэнд, "итог_сделки_sol": r.get("итог_po_cepi_sol")})
            del б, тр, ряды
    дт = П / "sendvich.json"
    дт.write_text(json.dumps({"счёт": счёт, "сделки": из_, "расход": уз.расход()},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(дт)
    с_сэнд = [x for x in из_ if x.get("сэндвичи")]
    print(f"{дт.name}: сделок {len(из_)}, блоков прочитано {счёт['блоков']}, в сэндвиче "
          f"{len(с_сэнд)}, нашей покупки не нашлось {счёт['нашей_не_найдено']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
