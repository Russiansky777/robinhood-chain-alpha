#!/usr/bin/env python3
"""BLAST 02.10 15:45Z: кто сделал движение в пуле PumpSwap. Только чтение цепи.

Вопрос владельца: наша покупка 15:45:16Z, основной рост по графику 15:45:12Z -- кто двигал цену и не он ли
тот, кого копируют. Здесь собирается голая цепь: все транзакции пула в окне, по слотам и по месту в блоке,
с подписантом, направлением (покупка / продажа), размером в SOL и владельцем получившего токен-счёт
(postTokenBalances[].owner -- привязка по балансам, а не по txSigner).

Выход: data/podbivka/blast_15-45.json -- строки по транзакциям, разбор источника и нашей покупки.
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def ключи(т: dict) -> list:
    с = (т or {}).get("transaction") or {}
    сообщ = с.get("message") or {}
    return [k if isinstance(k, str) else (k or {}).get("pubkey") for k in (сообщ.get("accountKeys") or [])]


def подписанты(т: dict) -> list:
    с = (т or {}).get("transaction") or {}
    сообщ = с.get("message") or {}
    из_ = [k.get("pubkey") for k in (сообщ.get("accountKeys") or []) if isinstance(k, dict) and k.get("signer")]
    if из_:
        return из_
    кл = ключи(т)
    n = (сообщ.get("header") or {}).get("numRequiredSignatures") or 1
    return кл[:n]


def дельты(т: dict, минт: str) -> tuple[dict, dict]:
    """{владелец: дельта токена} и {владелец: дельта WSOL} по pre/postTokenBalances."""
    м = (т or {}).get("meta") or {}
    пре = {(b.get("accountIndex"), b.get("mint")): b for b in (м.get("preTokenBalances") or [])}
    пост = {(b.get("accountIndex"), b.get("mint")): b for b in (м.get("postTokenBalances") or [])}
    ток: dict = {}
    квот: dict = {}
    for кл in set(пре) | set(пост):
        a, b = пре.get(кл), пост.get(кл)
        вл = (b or a or {}).get("owner")
        до = float(((a or {}).get("uiTokenAmount") or {}).get("uiAmountString") or 0)
        после = float(((b or {}).get("uiTokenAmount") or {}).get("uiAmountString") or 0)
        д = после - до
        if кл[1] == минт:
            ток[вл] = round(ток.get(вл, 0.0) + д, 9)
        elif кл[1] == WSOL:
            квот[вл] = round(квот.get(вл, 0.0) + д, 9)
    return ток, квот


def разбор(т: dict, минт: str, пул: str) -> dict:
    """Строка по транзакции. Сторона пула определяется потом, по частоте владельца (сторона_пула())."""
    ток, квот = дельты(т, минт)
    м = (т or {}).get("meta") or {}
    прог = set()
    сообщ = ((т or {}).get("transaction") or {}).get("message") or {}
    кл = ключи(т)
    for и in (сообщ.get("instructions") or []):
        p = и.get("programId") or (кл[и["programIdIndex"]] if и.get("programIdIndex") is not None
                                   and и["programIdIndex"] < len(кл) else None)
        if p:
            прог.add(p)
    for гр in (м.get("innerInstructions") or []):
        for и in гр.get("instructions") or []:
            p = и.get("programId") or (кл[и["programIdIndex"]] if и.get("programIdIndex") is not None
                                       and и["programIdIndex"] < len(кл) else None)
            if p:
                прог.add(p)
    подп = подписанты(т)
    return {"слот": т.get("slot"), "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(т.get("blockTime") or 0)),
            "подписант": подп[0] if подп else None, "подписантов": len(подп),
            "владельцы_токена": {k: v for k, v in ток.items() if k and abs(v) > 0},
            "wsol_по_владельцам": {k: v for k, v in квот.items() if k and abs(v) > 0},
            "pump_amm": PUMP_AMM in прог, "программ": len(прог),
            "err": ((т or {}).get("meta") or {}).get("err") is not None,
            "комиссия_sol": round(((т or {}).get("meta") or {}).get("fee") or 0) / 1e9}


def сторона_пула(строки: list, пул: str) -> tuple[str | None, float]:
    """Владелец хранилищ пула -- тот, кто стоит почти в каждом свопе пула (и обычно равен адресу пула)."""
    счёт: dict = {}
    всего = 0
    for r in строки:
        вл = r.get("владельцы_токена") or {}
        if not вл:
            continue
        всего += 1
        for k in вл:
            счёт[k] = счёт.get(k, 0) + 1
    if пул in счёт and всего and счёт[пул] / всего >= 0.5:
        return пул, round(счёт[пул] / всего, 3)
    if not счёт or not всего:
        return None, 0.0
    k = max(счёт, key=lambda x: счёт[x])
    return (k, round(счёт[k] / всего, 3)) if счёт[k] / всего >= 0.5 else (None, 0.0)


def дополнить(r: dict, вл_пула: str | None) -> dict:
    """Направление, размер в SOL и владелец получившего токен-счёт -- по дельтам относительно стороны пула."""
    ток = r.get("владельцы_токена") or {}
    квот = r.get("wsol_по_владельцам") or {}
    пул_ток = ток.get(вл_пула, 0.0) if вл_пула else 0.0
    пул_sol = квот.get(вл_пула, 0.0) if вл_пула else 0.0
    торговцы = {k: v for k, v in ток.items() if k != вл_пула}
    напр = "покупка" if пул_ток < 0 else ("продажа" if пул_ток > 0 else None)
    sol = abs(пул_sol)
    if напр is None and торговцы:                     # пул не опознан: судим по стороне торговца
        д = max(торговцы.values(), key=abs)
        напр = "покупка" if д > 0 else "продажа"
        sol = max((abs(v) for k, v in квот.items() if k != вл_пула), default=0.0)
    r["направление"] = напр
    r["sol"] = round(sol, 9)
    r["токенов"] = round(abs(пул_ток) or max((abs(v) for v in торговцы.values()), default=0.0), 6)
    r["получил_токен"] = max(торговцы, key=lambda k: торговцы[k]) if торговцы and max(торговцы.values()) > 0 else None
    r["отдал_токен"] = min(торговцы, key=lambda k: торговцы[k]) if торговцы and min(торговцы.values()) < 0 else None
    r["торговцы"] = торговцы
    return r


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--pul", required=True)
    р.add_argument("--mint", required=True)
    р.add_argument("--s", required=True, help="с какого UTC: YYYY-MM-DDTHH:MM:SSZ")
    р.add_argument("--do", required=True)
    р.add_argument("--krupnye-s", default="", help="окно крупных покупок, с")
    р.add_argument("--krupnye-do", default="")
    р.add_argument("--min-sol", type=float, default=1.0)
    р.add_argument("--podpisi", default="", help="через запятую: подписи-ориентиры (наша покупка, источник)")
    р.add_argument("--prefiksy", default="", help="через запятую: префиксы подписей-ориентиров (ист 7JVQ...)")
    р.add_argument("--metka", default="blast_15-45")
    р.add_argument("--stranic", type=int, default=80, help="предел страниц по 1000 подписей пула")
    а = р.parse_args()
    т0 = calendar.timegm(time.strptime(а.s, "%Y-%m-%dT%H:%M:%SZ"))
    т1 = calendar.timegm(time.strptime(а.do, "%Y-%m-%dT%H:%M:%SZ"))
    кр = ((calendar.timegm(time.strptime(а.krupnye_s, "%Y-%m-%dT%H:%M:%SZ")),
           calendar.timegm(time.strptime(а.krupnye_do, "%Y-%m-%dT%H:%M:%SZ")))
          if а.krupnye_s and а.krupnye_do else None)
    уз = S.Узел()
    строки, блоки = [], {}
    with уз.на("helius"):
        # подписи пула от конца окна назад
        подписи, до, страниц = [], None, 0
        while страниц < а.stranic:
            стр = уз.подписи(а.pul, до=до, limit=1000)
            страниц += 1
            if not стр:
                break
            подписи += стр
            if (стр[-1].get("blockTime") or 0) < т0 or len(стр) < 1000:
                break
            до = стр[-1]["signature"]
        самая_старая = min((з.get("blockTime") or 0) for з in подписи) if подписи else 0
        дошли = bool(подписи) and самая_старая < т0
        в_окне = [з for з in подписи if т0 <= (з.get("blockTime") or 0) <= т1]
        ориентиры = [x for x in а.podpisi.split(",") if x]
        префиксы = [x for x in а.prefiksy.split(",") if x]
        нужные = {з["signature"] for з in в_окне} | set(ориентиры)
        txs = уз.пакет(sorted(нужные), {з["signature"]: з.get("blockTime") for з in в_окне})
        for з in в_окне:
            т = txs.get(з["signature"])
            if not т:
                строки.append({"signature": з["signature"], "why_not": "узел не отдал транзакцию",
                               "слот": з.get("slot")})
                continue
            r = разбор(т, а.mint, а.pul)
            r["signature"] = з["signature"]
            строки.append(r)
        # место в блоке: подписи блоков окна по порядку
        слоты = sorted({r.get("слот") for r in строки if r.get("слот")})
        for сл in слоты:
            б = уз.вызов("getBlock", [сл, {"transactionDetails": "signatures", "rewards": False,
                                           "maxSupportedTransactionVersion": 0}], срок=60.0)
            сп = (б or {}).get("signatures") or []
            блоки[сл] = {"всего": len(сп), "места": {s: и for и, s in enumerate(сп)}}
        вл_пула, доля_пула = сторона_пула(строки, а.pul)
        for r in строки:
            дополнить(r, вл_пула)
        for r in строки:
            б = блоки.get(r.get("слот")) or {}
            r["место_в_блоке"] = (б.get("места") or {}).get(r.get("signature"))
            r["транзакций_в_блоке"] = б.get("всего")
        # ориентиры: наша покупка и источник (по полной подписи или по префиксу из окна)
        ор = {}
        for п in ориентиры:
            т = txs.get(п)
            ор[п] = (дополнить(разбор(т, а.mint, а.pul), вл_пула) | {"signature": п}) if т else {"signature": п, "why_not": "нет тела"}
        for пр in префиксы:
            наш = [r for r in строки if r["signature"].startswith(пр)]
            ор[пр] = наш[0] if наш else {"префикс": пр, "why_not": "в окне не найдено"}
    строки.sort(key=lambda r: (r.get("слот") or 0, r.get("место_в_блоке") if r.get("место_в_блоке") is not None else 10**9))
    крупные = [r for r in строки if r.get("направление") == "покупка" and (r.get("sol") or 0) >= а.min_sol
               and (not кр or (кр[0] <= calendar.timegm(time.strptime(r["utc"], "%Y-%m-%dT%H:%M:%SZ")) <= кр[1]))]
    из_ = {"пул": а.pul, "минт": а.mint, "владелец_хранилищ_пула": вл_пула, "доля_свопов_с_ним": доля_пула,
           "страниц_подписей": страниц, "подписей_просмотрено": len(подписи),
           "самая_старая_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(самая_старая)) if самая_старая else None,
           "дошли_до_окна": дошли,
           "why_not": None if дошли else f"предел страниц {а.stranic}: до начала окна не дочитали", "окно": [а.s, а.do], "крупные_окно": [а.krupnye_s, а.krupnye_do],
           "мин_sol": а.min_sol, "транзакций": len(строки), "ориентиры": ор,
           "по_слотам": {str(сл): {"транзакций_пула": sum(1 for r in строки if r.get("слот") == сл),
                                   "покупок": sum(1 for r in строки if r.get("слот") == сл and r.get("направление") == "покупка"),
                                   "продаж": sum(1 for r in строки if r.get("слот") == сл and r.get("направление") == "продажа"),
                                   "sol_покупок": round(sum((r.get("sol") or 0) for r in строки
                                                            if r.get("слот") == сл and r.get("направление") == "покупка"), 6),
                                   "транзакций_в_блоке": (блоки.get(сл) or {}).get("всего")}
                         for сл in sorted({r.get("слот") for r in строки if r.get("слот")})},
           "крупные_покупки": крупные, "строки": строки, "расход": уз.расход()}
    out = П / f"{а.metka}.json"
    out.write_text(json.dumps(из_, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    print(f"{out.name}: страниц подписей {страниц}, просмотрено {len(подписи)}, самая старая "
          f"{из_['самая_старая_utc']}, дошли до окна {дошли}", flush=True)
    print(f"{out.name}: транзакций пула в окне {len(строки)}, крупных покупок (≥ {а.min_sol:g} SOL) "
          f"{len(крупные)}, слотов {len(из_['по_слотам'])}", flush=True)
    for r in крупные:
        print(f"  {r['utc']} слот {r['слот']} место {r['место_в_блоке']}/{r['транзакций_в_блоке']} "
              f"{r['sol']:.3f} SOL подписант {r['подписант']} получил {r['получил_токен']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
