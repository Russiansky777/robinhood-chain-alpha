#!/usr/bin/env python3
"""Подбивка: пары «наша сделка полосы — симулятор на том же событии» (сверка +13 %).

Позиции полосы из выгрузки Code-1 (data/podbivka/nashi_sdelki_host.json, коммит
b4542e3): side = lane, посадка S+0 (entry_slot = source_slot) и, для сравнения,
S+1. По каждой -- сделка источника, наша покупка и наша продажа по цепи.

ФАКТ: back/in − 1 из выгрузки (итог службы) и то же по цепи -- SOL в свопах
нашей покупки и продажи (WSOL-хранилища не подписантов, у кривой -- лампорты
кривой), без чаевых и платы.
СИМУЛЯТОР (основной, как в таблицах): вход S+0 -- состояние сразу после сделки
источника, трата = фактическая трата в свопе; выход +72 со вставкой нашей
покупки. Разложение расхождения:
  * вход: токены симулятора при S+0 / в фактическом месте посадки (состояние
    перед нашей покупкой в цепи) против фактических токенов;
  * слот выхода: симулятор с выходом в фактическом слоте продажи (состояние
    перед нашей продажей, наш след в нём уже есть) против выхода +72;
  * маршрут и доля продажи: продажа фактических токенов в состояние перед нашей
    продажей против фактического SOL продажи.
Выход: data/podbivka/pary_polosa.json, docs/podbivka_2026-09-27_pary.md.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_swap_build as SB  # noqa: E402
import podbivka as P  # noqa: E402
import podbivka_a3 as A3  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПУСТО = {"в_пул": 0, "tokens": 0}


def одна(уз: S.Узел, x: dict) -> dict:
    из_ = {к: x.get(к) for к in ("group_effective", "mint", "source", "source_sig", "source_slot", "entry_slot",
                                 "buy_sig", "sell_sig", "in_sol", "back_sol", "pnl_sol", "spend_sol")}
    из_["посадка"] = x["entry_slot"] - x["source_slot"]
    из_["факт_выгрузка_пп"] = round((x["back_sol"] / x["in_sol"] - 1) * 100, 3)
    tsrc, tb, ts = уз.tx(x["source_sig"]), уз.tx(x["buy_sig"]), уз.tx(x["sell_sig"])
    if not tsrc or not tb or not ts:
        return {**из_, "why_not": "узел не отдал одну из трёх сделок"}
    наш, минт, ист = x["wallet"], x["mint"], x["source"]
    s0 = tsrc["slot"]
    пул = C.identify_pool(tsrc, ист, минт)
    пул_наш = C.identify_pool(tb, наш, минт)
    из_.update(program=None, пул_тот_же=пул.get("pool_vault") == пул_наш.get("pool_vault"),
               слот_покупки=tb["slot"], слот_продажи=ts["slot"], удержание_слотов=ts["slot"] - tb["slot"])
    кривая = пул.get("pool_owner") if пул.get("quote_mint") == C.NATIVE_QUOTE else None
    вход = A3.своп_sol(tb, "buy", кривая)
    выход = A3.своп_sol(ts, "sell", кривая)
    т_факт = A3.наши_токены(tb, наш, минт)
    т_прод = -A3.наши_токены(ts, наш, минт)
    из_.update(факт_вход_лам=вход, факт_выход_лам=выход, факт_токенов=т_факт, продано=т_прод,
               факт_цепь_пп=round((выход / вход - 1) * 100, 3) if вход else None)
    if not вход:
        return {**из_, "why_not": "трата в свопе нашей покупки не найдена"}
    # режим и калибровка -- как в основном симуляторе, по сделке источника
    сим0 = P.наша_покупка_по_симулятору(tsrc, ист, минт, вход)
    из_["program"] = сим0.get("program")
    if not сим0.get("ok"):
        return {**из_, "why_not": f"S+0: {сим0.get('why_not')}"}
    режим = S.режим_из_метода(сим0.get("method"))
    f = сим0.get("fee_factor")
    if режим == "xyk" and сим0.get("program") == SB.LAUNCHLAB:
        режим = "launchlab"
        f = SB.launchlab_min_out(tsrc, S.РАЗМЕР_ЛАМ, 0.0).get("fee_rate")
    кривая_bps = (0, 0)
    if режим == "curve":
        ев = SB.pump_trade_event(tsrc, минт)
        if not ев:
            return {**из_, "why_not": "кривая: нет события источника"}
        кривая_bps = (int(ев["fee_bps"]), int(ев["creator_fee_bps"]))
    из_.update(режим=режим, f=f)
    if режим == "price":
        return {**из_, "why_not": "режим «цена свопа» -- без резервов, разложение не считается"}
    ст0 = S.состояние(tsrc, режим, пул, минт)
    if not ст0:
        return {**из_, "why_not": "состояние S+0 не читается"}
    налог = S.налог_минта(уз, минт)
    до = max(s0 + 72, ts["slot"])
    ист_п = S.история_пула(уз, пул["pool_vault"], x["source_sig"], s0,
                           опора=(S.подпись_после_слота(уз, до + 1) if уз.текущий == "helius" else None),
                           до_слота=до)
    if ист_п["why_not"] or ист_п["предел"]:
        return {**из_, "why_not": f"история пула: {ист_п['why_not'] or 'предел страниц'}"}
    сп = [з for з in ист_п["подписи"] if з["ok"]]
    ib = next((i for i, з in enumerate(сп) if з["signature"] == x["buy_sig"]), None)
    is_ = next((i for i, з in enumerate(сп) if з["signature"] == x["sell_sig"]), None)
    из_["в_истории"] = {"покупка": ib is not None, "продажа": is_ is not None}

    def ст(i):
        # ближайшее читаемое состояние не позже i-й сделки пула
        while i is not None and i >= 0:
            с = S.состояние(уз.tx(сп[i]["signature"]), режим, пул, минт)
            if с:
                return с
            i -= 1
        return ст0

    def ст_до_слота(слот):
        канд = [i for i, з in enumerate(сп) if з["slot"] <= слот]
        return ст(канд[-1]) if канд else ст0
    # доля продажи -- как в основном симуляторе (по продажам пула), для xyk
    g = f
    if режим == "xyk":
        доли = [S.доля_продажи(уз.tx(з["signature"]), пул, минт, f) for з in сп[:40]]
        доли = [d for d in доли if d]
        g = statistics.median(доли) if доли else f
        из_["g"], из_["g_продаж"] = round(g, 5) if g else None, len(доли)
    # вход
    пок0 = S.наша_покупка(режим, ст0, f=f, кривая_bps=кривая_bps, размер=вход)
    ст_пн = ст(ib - 1) if ib is not None else None
    пок_м = S.наша_покупка(режим, ст_пн, f=f, кривая_bps=кривая_bps, размер=вход) if ст_пн else {"ok": False}
    if not пок0.get("ok"):
        return {**из_, "why_not": f"покупка S+0: {пок0.get('why_not')}"}
    т0 = пок0["tokens"] - S.удержано(пок0["tokens"], налог)
    из_["вход_S0_к_факту_пп"] = round((т0 / т_факт - 1) * 100, 3) if т_факт else None
    if пок_м.get("ok"):
        тм = пок_м["tokens"] - S.удержано(пок_м["tokens"], налог)
        из_["вход_место_к_факту_пп"] = round((тм / т_факт - 1) * 100, 3) if т_факт else None

    def пп(лам):
        return round((лам / вход - 1) * 100, 3)
    # выход +72 со вставкой (модель таблиц)
    пр72 = S.наша_продажа(режим, ст_до_слота(s0 + 71), пок0, т0 - S.удержано(т0, налог), f=f,
                          кривая_bps=кривая_bps, g=g)
    из_["сим_S0_72_пп"] = пп(пр72["lamports"]) if пр72.get("ok") else None
    # выход в фактическом слоте продажи: состояние перед нашей продажей (наш след в нём есть)
    if is_ is not None:
        ст_пп = ст(is_ - 1)
        пр_ф = S.наша_продажа(режим, ст_пп, ПУСТО, т0 - S.удержано(т0, налог), f=f, кривая_bps=кривая_bps, g=g)
        из_["сим_S0_слот_продажи_пп"] = пп(пр_ф["lamports"]) if пр_ф.get("ok") else None
        пр_т = S.наша_продажа(режим, ст_пп, ПУСТО, т_прод - S.удержано(т_прод, налог), f=f,
                              кривая_bps=кривая_bps, g=g)
        if пр_т.get("ok") and выход:
            из_["продажа_факт_токенов_к_факту_пп"] = round((пр_т["lamports"] / выход - 1) * 100, 3)
    из_["расхождение_факт_минус_сим72_пп"] = (round(из_["факт_цепь_пп"] - из_["сим_S0_72_пп"], 3)
                                              if из_.get("сим_S0_72_пп") is not None and из_.get("факт_цепь_пп") is not None
                                              else None)
    return из_


def main() -> int:
    д = json.loads((КОРЕНЬ / "data" / "podbivka" / "nashi_sdelki_host.json").read_text(encoding="utf-8"))["sdelki"]
    поз = [x for x in д if x.get("side") == "lane" and x.get("entry_slot") is not None and x.get("source_slot") is not None
           and x["entry_slot"] - x["source_slot"] in (0, 1) and x.get("back_sol") is not None and x.get("in_sol")
           and x.get("buy_sig") and x.get("sell_sig") and x.get("source_sig")]
    уз = S.Узел()
    import calendar  # noqa: PLC0415
    import time  # noqa: PLC0415
    рез = []
    for x in поз:
        bt = calendar.timegm(time.strptime(x["ts_intent_utc"], "%Y-%m-%dT%H:%M:%SZ"))
        with уз.на(S.узел_по_времени(bt)):
            try:
                р = одна(уз, x)
            except Exception as exc:  # noqa: BLE001
                р = {"mint": x["mint"], "source_sig": x["source_sig"], "why_not": S.чисто(f"{type(exc).__name__}: {exc}")[:200]}
        уз._кэш.clear()  # noqa: SLF001
        рез.append(р)
        print(р.get("посадка"), (р.get("mint") or "")[:8], р.get("факт_цепь_пп"), р.get("сим_S0_72_пп"),
              р.get("сим_S0_слот_продажи_пп"), р.get("why_not"), flush=True)
    (КОРЕНЬ / "data" / "podbivka" / "pary_polosa.json").write_text(
        json.dumps({"позиций": len(поз), "ряды": рез, "расход": уз.расход()}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
