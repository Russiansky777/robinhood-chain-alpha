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
                     "err": (м.get("err") is not None)})
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


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--predel", type=int, default=0, help="сколько сделок считать (0 -- все)")
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
            # сэндвич: тот же подписант купил до нас и продал после
            до = [x for x in ряды[:наш["и"]] if x and not x["err"]]
            после = [x for x in ряды[наш["и"] + 1:] if x and not x["err"]]
            сэнд = []
            for a_ in до:
                s = a_["подписант"]
                if not s or s in НАШИ or s == наш["подписант"]:
                    continue
                купил = sum(d for вл, d in a_["ток"].items() if вл != пул and d > 0)
                if купил <= 0:
                    continue
                for b_ in после:
                    if b_["подписант"] != s:
                        continue
                    продал = sum(d for вл, d in b_["ток"].items() if вл != пул and d < 0)
                    if продал >= 0:
                        continue
                    сол_купил = -sum(d for вл, d in a_["квот"].items() if вл != пул and d < 0)
                    сол_продал = sum(d for вл, d in b_["квот"].items() if вл != пул and d > 0)
                    x0 = (a_["состояние_до"].get(пул) or {}).get("квот_до") if пул else None
                    y0 = (a_["состояние_до"].get(пул) or {}).get("ток_до") if пул else None
                    ид = (y0 * f_эфф * билет / (x0 + f_эфф * билет)
                          if (x0 and y0 and f_эфф and билет) else None)
                    потеря_пп = (round((наши_токены / ид - 1) * 100, 3) if (ид and наши_токены) else None)
                    сэнд.append({"подписант": s, "индекс_до": a_["и"], "индекс_после": b_["и"],
                                 "соседний_до": наш["и"] - a_["и"] == 1,
                                 "соседний_после": b_["и"] - наш["и"] == 1,
                                 "его_sol_вход": round(сол_купил, 6), "его_sol_выход": round(сол_продал, 6),
                                 "его_итог_sol": round(сол_продал - сол_купил, 6),
                                 "наша_потеря_пп": потеря_пп,
                                 "наша_потеря_sol": (round(билет * потеря_пп / 100, 6)
                                                     if потеря_пп is not None else None)})
            из_.append({"cid": r["cid"], "utc": r.get("utc"), "группа": r.get("group"),
                        "билет": r.get("size_sol"), "sol_in": билет, "слот": сл, "минт": r["mint"],
                        "наш_индекс": наш["и"], "в_блоке": len(тр), "пул": пул, "наш_кошелёк": наш_кош,
                        "наши_токены": наши_токены, "f_эфф": round(f_эфф, 6) if f_эфф else None,
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
