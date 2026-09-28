#!/usr/bin/env python3
"""Подбивка: сверка живых сделок группы leader с моделью режима 2 (аналог A3 для
нашей полосы). Только чтение цепи.

Вход -- строки сделок: наш_кошелёк, источник, mint, buy_sig, [sell_sig],
[src_sig], группа, [buy_ts]. Источник строк:
  --vhod a3            -- data/podbivka/a3_sdelki.json (пилот DBot 18–19.09 и
                          круги BATCH-5 25–27.09) -- проверка заготовки;
  --vhod <путь.json>   -- позиции Code-1 (ключи wallet/source/mint/buy_sig/
                          sell_sig/source_sig; side = lane, группа leader).
По каждой сделке:
 1. место посадки в слоте лидера: «сразу за ним» (0–1 своп пула между его и
    нашей сделкой) / «середина» / «конец слота» (после нас в слоте s0 свопов
    пула нет) / «слот s0+k»; фактическая наценка -- наш котировочный, ушедший в
    пул / токены, отданные пулом, к спот-цене пула сразу после лидера;
 2. модель на том же месте (состояние пула прямо перед нашей покупкой в цепи),
    нашим фактическим размером (SOL в свопе покупки); выход -- (а) через 150
    слотов от s0, (б) в фактическом слоте нашей продажи (состояние перед ней);
 3. факт по цепи (SOL в свопах покупки и продажи, обе ноги через котировочный,
    все переводы и налоги -- как есть в цепи) против модели (б);
 4. сдвиг курса котировочного к SOL за удержание: исполненный курс ноги
    q↔SOL в нашей продаже к курсу в нашей покупке (плечи в самих сделках) и его
    вклад в расхождение (модель держит курс постоянным).
Модель -- A3 (прогон 6): f по хранилищам сделки лидера (< 0.95 -- по ближайшей
покупке пула до нас), g по продажам пула (нет -- 0.985), продажа x·g·t/(y+t),
налог котировочного на двух переводах в каждую сторону, налог токена на
получении и продаже; курс котировочного -- из сделки лидера.
Выход: data/podbivka/sverka_leader[_<метка>].json и docs/podbivka_sverka_leader[_<метка>].md.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka as P  # noqa: E402
import podbivka_a3 as A3  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
SOLы = (C.WSOL, C.NATIVE_QUOTE)
ГОРИЗОНТ = 150


def место(сп: list, ib: int, s0: int, слот_нашей: int) -> dict:
    между = ib                                   # успешные свопы пула после лидера и до нас
    if слот_нашей > s0:
        return {"место": f"слот s0+{слот_нашей - s0}", "свопов_между": между}
    после_в_слоте = sum(1 for з in сп[ib + 1:] if з["slot"] == s0)
    if между <= 1:
        м = "сразу за ним"
    elif после_в_слоте == 0:
        м = "конец слота"
    else:
        м = "середина"
    return {"место": м, "свопов_между": между, "свопов_после_в_слоте": после_в_слоте}


def одна(уз: S.Узел, с: dict) -> dict:
    import podbivka_lider_kotirovka as LK  # noqa: PLC0415
    из_ = {к: с.get(к) for к in ("группа", "mint", "buy_sig", "источник")}
    tb = уз.tx(с["buy_sig"])
    if not tb:
        return {**из_, "why_not": "узел не отдал нашу покупку"}
    наш = с.get("наш_кошелёк") or (C.account_keys(tb) or [None])[0]   # плательщик нашей покупки
    sell = с.get("sell_sig")
    if not sell:
        for з in reversed(уз.подписи(наш, limit=1000)):
            if (з.get("slot") or 0) <= tb["slot"] or з.get("err") is not None:
                continue
            т = уз.tx(з["signature"])
            if т and A3.наши_токены(т, наш, с["mint"]) < 0:
                sell = з["signature"]
                break
    ts = уз.tx(sell) if sell else None
    if not ts:
        return {**из_, "why_not": "продажа не найдена"}
    из_["sell_sig"] = sell
    пул = C.identify_pool(tb, наш, с["mint"])
    q = пул.get("quote_mint")
    из_.update(quote_mint=q, pool_vault=пул.get("pool_vault"), slot_buy=tb["slot"], slot_sell=ts["slot"],
               split=bool(пул.get("split")))
    if not пул.get("pool_vault") or not пул.get("quote_vault"):
        return {**из_, "why_not": "пул токена не опознан в нашей покупке"}
    if q == C.NATIVE_QUOTE:
        return кривая(уз, с, из_, tb, ts, наш, пул)
    import c2_pool_programs as PP  # noqa: PLC0415
    прог = PP.pool_program(tb, пул["pool_vault"], PP.labels()).get("pool_program")
    из_["program"] = прог
    if прог not in (S.SB.CPMM, S.SB.PUMP_AMM, "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"):
        return {**из_, "why_not": f"программа пула {прог}: вне x*y=k по хранилищам"}
    src = с.get("src_sig")
    if not src:
        сп0 = [з for з in уз.подписи(с["mint"], до=с["buy_sig"], limit=1000)
               if з.get("err") is None and (з.get("slot") or 0) >= tb["slot"] - 600]
        for i in range(0, len(сп0), 25):
            пачка = уз.пакет([з["signature"] for з in сп0[i:i + 25]])
            for з in сп0[i:i + 25]:
                т = пачка.get(з["signature"])
                if т and с["источник"] in C.signers(т) and A3.наши_токены(т, с["источник"], с["mint"]) > 0:
                    src = з["signature"]
                    break
            if src:
                break
    tsrc = уз.tx(src) if src else None
    if not tsrc:
        return {**из_, "why_not": "сделка лидера не найдена"}
    s0 = tsrc["slot"]
    из_.update(src_sig=src, s0=s0, сдвиг_слотов=tb["slot"] - s0)
    # ФАКТ
    вход, выход = A3.своп_sol(tb, "buy"), A3.своп_sol(ts, "sell")
    if q in SOLы:                                   # на руки: убыль хранилища минус комиссии получателям
        выход = max(0, выход - комиссии_инструкции(ts, пул))
    т_факт = A3.наши_токены(tb, наш, с["mint"])
    т_прод = -A3.наши_токены(ts, наш, с["mint"])
    кв_b, тв_b = A3.дельта_счёта(tb, пул["quote_vault"]), A3.дельта_счёта(tb, пул["pool_vault"])
    кв_s = A3.дельта_счёта(ts, пул["quote_vault"])
    q_в_пул = (кв_b[1] - кв_b[0]) if кв_b else None
    т_из_пула = (тв_b[0] - тв_b[1]) if тв_b else None
    q_из_пула = (кв_s[0] - кв_s[1]) if кв_s else None
    из_.update(факт_вход_sol=вход / 1e9 if вход else None, факт_выход_sol=выход / 1e9 if выход else None,
               факт_пп=round((выход - вход) / вход * 100, 3) if вход else None)
    if not вход or not q_в_пул or not т_из_пула:
        return {**из_, "why_not": "факт покупки по хранилищам не читается"}
    нал_т = S.налог_минта(уз, с["mint"])
    нал_q = S.налог_минта(уз, q) if q not in SOLы else {"bps": 0}
    for чей, нал in (("токена", нал_т), ("котировочного", нал_q)):
        if нал.get("why_not"):
            return {**из_, "why_not": f"налог {чей} не прочитан"}
    из_.update(налог_токена_bps=нал_т.get("bps"), налог_q_bps=нал_q.get("bps"))
    до = max(ts["slot"], s0 + ГОРИЗОНТ)
    ист = история(уз, пул, src, s0, до, из_.get("sell_sig") or с.get("sell_sig"), ts)
    if ист["why_not"] or ист["предел"]:
        return {**из_, "why_not": f"история пула: {ист['why_not'] or 'предел страниц'}"}
    сп = [з for з in ист["подписи"] if з["ok"]]
    ib = next((i for i, з in enumerate(сп) if з["signature"] == с["buy_sig"]), None)
    is_ = next((i for i, з in enumerate(сп) if з["signature"] == sell), None)
    if ib is None or is_ is None:
        return {**из_, "why_not": "наши сделки не найдены в истории пула"}
    ст0 = S.состояние(tsrc, "xyk", пул, с["mint"])
    if not ст0:
        return {**из_, "why_not": "состояние после лидера не читается"}
    кэш: dict = {}

    def txi(i):
        if i not in кэш:
            try:
                кэш[i] = уз.tx(сп[i]["signature"])
            except RuntimeError:
                кэш[i] = None
        return кэш[i]

    def ст(i):
        for j in range(i, max(-1, i - S.ШАГОВ_НАЗАД - 1), -1):
            с_ = S.состояние(txi(j), "xyk", пул, с["mint"])
            if с_:
                return с_
        return None
    # 1. место и фактическая наценка
    из_.update(место(сп, ib, s0, tb["slot"]))
    p0 = ст0["x"] / ст0["y"]
    из_["наценка_факт_пп"] = round((q_в_пул / т_из_пула / p0 - 1) * 100, 3)
    # калибровки
    f = f_все(tsrc, пул)
    из_["f_лидера"] = round(f, 5) if f else None
    из_["f_только_хранилище"] = round(A3.f_по_хранилищам(tsrc, пул) or 0, 5) or None
    if not f or not (0.5 <= f <= 1.0):
        f = None
        for i in range(ib - 1, max(-1, ib - 30), -1):
            ff = f_все(txi(i), пул)
            if ff and 0.5 <= ff <= 1.0:
                f = ff
                break
    if not f:
        return {**из_, "why_not": "доля траты не калибруется"}
    из_["f"] = round(f, 5)
    доли = []
    for i in range(0, len(сп)):                   # все продажи пула в окне (кроме нашей)
        if i in (is_, ib):
            continue
        т = txi(i)
        кв, тв = A3.дельта_счёта(т, пул["quote_vault"]), A3.дельта_счёта(т, пул["pool_vault"])
        if кв and тв and тв[1] > тв[0] and кв[1] < кв[0]:
            g_ = g_все(т, пул)
            if g_ and 0.5 <= g_ <= 1.05:
                доли.append(g_)
        if len(доли) >= 30:
            break
    g = statistics.median(доли) if доли else A3.G_БЕЗ_ПРОДАЖ
    из_["g"], из_["g_продаж"] = round(g, 5), len(доли)
    # курс котировочного: модель -- из сделки лидера; факт -- плечи наших сделок
    if q in SOLы:
        цена_q, r_in, r_out = 1.0, 1.0, 1.0
    else:
        цена_q, откуда = LK.цена_q_в_sol(tsrc, q, tsrc.get("blockTime"))
        if not цена_q:
            return {**из_, "why_not": f"курс котировочного: {откуда}"}
        r_in = LK.цена_q_в_sol(tb, q, tb.get("blockTime"))[0]
        r_out = LK.цена_q_в_sol(ts, q, ts.get("blockTime"))[0]
    из_.update(курс_q_модель=цена_q, курс_q_покупка=r_in, курс_q_продажа=r_out)
    # 2. модель на том же месте
    ст_вход = ст(ib - 1) if ib > 0 else ст0
    if not ст_вход:
        return {**из_, "why_not": "состояние перед нашей покупкой не читается"}
    q_raw = int(вход / цена_q)
    q_raw -= S.удержано(q_raw, нал_q)
    q_raw -= S.удержано(q_raw, нал_q)
    x, y = ст_вход["x"], ст_вход["y"]
    т_бр = int(y * f * q_raw / (x + f * q_raw))
    т_м = т_бр - S.удержано(т_бр, нал_т)
    в_пул = т_м - S.удержано(т_м, нал_т)
    из_["токены_модель_к_факту_пп"] = round((т_м / т_факт - 1) * 100, 3) if т_факт else None

    def в_sol(q_out):
        q_out -= S.удержано(q_out, нал_q)
        q_out -= S.удержано(q_out, нал_q)
        return int(q_out * цена_q)

    def продать(ст_, вставить):
        X, Y = ст_["x"], ст_["y"]
        if вставить:
            X, Y = X + q_в_пул, Y - т_из_пула
        return int(X * g * в_пул / (Y + в_пул)) if Y > 0 else None
    # (а) выход через 150 слотов от s0: состояние после последней сделки со слотом <= s0+149;
    # до нашей продажи наш след в нём уже есть, после -- вставляем нашу покупку обратно
    канд = [i for i, з in enumerate(сп) if з["slot"] <= s0 + ГОРИЗОНТ - 1]
    i150 = канд[-1] if канд else None
    ст150 = ст(i150) if i150 is not None else ст0
    q150 = продать(ст150, вставить=(i150 is None or i150 >= is_ or i150 < ib)) if ст150 else None
    из_["модель_150_пп"] = round((в_sol(q150) - вход) / вход * 100, 3) if q150 else None
    # (б) выход в фактическом слоте продажи: состояние перед нашей продажей
    ст_пп = ст(is_ - 1)
    qf = продать(ст_пп, вставить=False) if ст_пп else None
    if not qf:
        return {**из_, "why_not": "состояние перед нашей продажей не читается"}
    из_["модель_пп"] = round((в_sol(qf) - вход) / вход * 100, 3)
    if ст_пп and q in SOLы and выход:
        X, Y = ст_пп["x"], ст_пп["y"]
        из_["g_нашей_продажи"] = round(выход * (Y + в_пул) / (X * в_пул), 5)
    из_["расхождение_пп"] = round(из_["факт_пп"] - из_["модель_пп"], 3)
    # 4. сдвиг курса котировочного за удержание -- по хранилищам наших сделок:
    # SOL за единицу котировочного, дошедшую до пула токена (покупка), и SOL за
    # единицу, вышедшую из пула токена (продажа). Модель предполагает их
    # отношение = (1 − налог)^4 (курс постоянный, налог на четырёх переводах);
    # всё сверх -- сдвиг курса и проскальзывание нашей ноги q<->SOL.
    из_.update(факт_q_в_пул=q_в_пул, факт_q_из_пула=q_из_пула)
    if из_.get("split"):
        из_["курс_q_примечание"] = "маршрут дробился: котировочный в главном пуле -- часть траты, вклад курса не считается"
    elif q not in SOLы and q_из_пула and выход:
        t = (нал_q.get("bps") or 0) / 1e4
        курс_вход, курс_выход = вход / q_в_пул, выход / q_из_пула
        сдвиг = курс_выход / курс_вход / (1 - t) ** 4 - 1
        из_["курс_q_вход_к_модели"] = round(курс_вход / (цена_q / (1 - t) ** 2), 4)
        из_["курс_q_выход_к_модели"] = round(курс_выход / (цена_q * (1 - t) ** 2), 4)
        из_["сдвиг_курса_q_пп"] = round(сдвиг * 100, 3)
        из_["из_них_курс_пп"] = round((1 + из_["модель_пп"] / 100) * сдвиг * 100, 3)
        из_["расхождение_без_курса_пп"] = round(из_["расхождение_пп"] - из_["из_них_курс_пп"], 3)
    return из_


def комиссии_инструкции(tx: dict, пул: dict) -> int:
    """Сумма переводов котировки получателям комиссии в инструкциях пула (счета не
    подписантов, кроме хранилищ, с приростом; в тех наборах счетов инструкций,
    где есть оба хранилища пула). Pump AMM: комиссии протокола и создателя уходят
    из хранилища/от покупателя отдельными переводами (слово Code-1 28.09)."""
    if not tx:
        return 0
    sg = C.signers(tx)
    ряды = [r for r in C.token_rows(tx).values() if r["account"]]
    q = пул.get("quote_mint")
    q = C.WSOL if q in SOLы else q
    наборы = [s_ for s_ in C.instruction_account_sets(tx) if пул["pool_vault"] in s_ and пул["quote_vault"] in s_]
    if not наборы:
        return 0
    счета = set().union(*наборы)
    return sum(int(r["post"]) - int(r["pre"]) for r in ряды
               if r["account"] in счета and r["mint"] == q and r["owner"] not in sg
               and r["account"] not in (пул["quote_vault"], пул["pool_vault"]) and int(r["post"]) > int(r["pre"]))


def f_все(tx: dict, пул: dict):
    """f = x0·dy / ((y0 − dy)·(прирост хранилища + комиссии получателям))."""
    кв, тв = A3.дельта_счёта(tx, пул["quote_vault"]), A3.дельта_счёта(tx, пул["pool_vault"])
    if not кв or not тв:
        return None
    x0, x1 = кв
    y0, y1 = тв
    dx, dy = x1 - x0 + комиссии_инструкции(tx, пул), y0 - y1
    if dx <= 0 or dy <= 0 or y0 <= dy:
        return None
    return x0 * dy / ((y0 - dy) * dx)


def g_все(tx: dict, пул: dict):
    """g = (убыль хранилища − комиссии получателям)·(y0 + dy) / (x0·dy) -- на руки продавцу."""
    кв, тв = A3.дельта_счёта(tx, пул["quote_vault"]), A3.дельта_счёта(tx, пул["pool_vault"])
    if not кв or not тв:
        return None
    x0, x1 = кв
    y0, y1 = тв
    dy = y1 - y0
    на_руки = x0 - x1 - комиссии_инструкции(tx, пул)
    if dy <= 0 or на_руки <= 0 or x0 <= 0:
        return None
    return на_руки * (y0 + dy) / (x0 * dy)


def история(уз, пул: dict, src: str, s0: int, до: int, sell: str, ts: dict) -> dict:
    """История хранилища пула от сделки источника. Helius -- опора «любая подпись
    блока после окна» (узел находит её слот). Shyft такую опору не понимает (подпись
    должна принадлежать адресу): опора -- наша продажа (она проходит через
    хранилище), история до неё, сама продажа добавляется последней; окно +150
    тогда только до продажи."""
    if уз.текущий == "helius":
        return S.история_пула(уз, пул["pool_vault"], src, s0, опора=S.подпись_после_слота(уз, до + 1), до_слота=до)
    ист = S.история_пула(уз, пул["pool_vault"], src, s0, опора=sell, до_слота=ts["slot"])
    if not ист["why_not"] and not ист["предел"]:
        ист["подписи"].append({"signature": sell, "slot": ts["slot"], "ok": True})
    return ист


def кривая(уз, с: dict, из_: dict, tb: dict, ts: dict, наш: str, пул: dict) -> dict:
    """Сверка на кривой pump.fun (модель режима 1: состояние из события сделки,
    комиссии кривой -- из события сделки источника).
    Факт: вход -- лампорты, вошедшие в кривую в нашей покупке, плюс комиссии кривой
    (из события нашей покупки); выход -- лампорты, ушедшие из кривой в нашей
    продаже, минус комиссии кривой. Модель: покупка той же тратой в состоянии
    перед нашей покупкой, продажа наших фактических токенов в состоянии перед
    нашей продажей."""
    из_["program"] = S.SB.PUMP_CURVE if hasattr(S.SB, "PUMP_CURVE") else "pump.fun кривая"
    src = с.get("src_sig")
    tsrc = уз.tx(src) if src else None
    if not tsrc:
        return {**из_, "why_not": "сделка источника не найдена"}
    s0 = tsrc["slot"]
    из_.update(src_sig=src, s0=s0, сдвиг_слотов=tb["slot"] - s0)
    ев0 = S.SB.pump_trade_event(tsrc, с["mint"])
    ев_b = S.SB.pump_trade_event(tb, с["mint"])
    if not ев0 or not ев_b:
        return {**из_, "why_not": "кривая: нет события сделки источника или нашей покупки"}
    bps = (int(ев0["fee_bps"]), int(ев0["creator_fee_bps"]))
    вход = int(ев_b["sol_amount"]) + int(ев_b.get("fee") or 0) + int(ев_b.get("creator_fee") or 0)
    т_факт = A3.наши_токены(tb, наш, с["mint"])
    т_прод = -A3.наши_токены(ts, наш, с["mint"])
    из_кривой = -(C.lamport_delta(ts, пул["quote_vault"]) or 0)
    выход = из_кривой - (-(-из_кривой * bps[0] // 10_000)) - (-(-из_кривой * bps[1] // 10_000)) if из_кривой > 0 else 0
    из_.update(факт_вход_sol=вход / 1e9, факт_выход_sol=выход / 1e9 if выход else None,
               факт_пп=round((выход - вход) / вход * 100, 3) if вход and выход else None,
               продано_токенов_доля=round(т_прод / т_факт, 4) if т_факт else None)
    if not выход:
        return {**из_, "why_not": "кривая: выход нашей продажи не читается (миграция или другой пул)"}
    до = max(ts["slot"], s0 + ГОРИЗОНТ)
    ист = история(уз, пул, src, s0, до, из_.get("sell_sig") or с.get("sell_sig"), ts)
    if ист["why_not"] or ист["предел"]:
        return {**из_, "why_not": f"история пула: {ист['why_not'] or 'предел страниц'}"}
    сп = [з for з in ист["подписи"] if з["ok"]]
    ib = next((i for i, з in enumerate(сп) if з["signature"] == с["buy_sig"]), None)
    is_ = next((i for i, з in enumerate(сп) if з["signature"] == из_["sell_sig"]), None)
    if ib is None or is_ is None:
        return {**из_, "why_not": "наши сделки не найдены в истории пула"}
    из_.update(место(сп, ib, s0, tb["slot"]))

    def ст(i):
        for j in range(i, max(-1, i - S.ШАГОВ_НАЗАД - 1), -1):
            try:
                с_ = S.состояние(уз.tx(сп[j]["signature"]), "curve", пул, с["mint"])
            except RuntimeError:
                с_ = None
            if с_:
                return с_
        return None
    ст0 = S.состояние(tsrc, "curve", пул, с["mint"])
    ст_вход = ст(ib - 1) if ib > 0 else ст0
    ст_пп = ст(is_ - 1)
    if not ст0 or not ст_вход or not ст_пп:
        return {**из_, "why_not": "кривая: состояние не читается"}
    p0 = ст0["vs"] / ст0["vt"]
    из_["наценка_факт_пп"] = round((int(ев_b["sol_amount"]) / int(ев_b["token_amount"]) / p0 - 1) * 100, 3)
    пок = S.наша_покупка("curve", ст_вход, f=None, кривая_bps=bps, размер=вход)
    if not пок.get("ok"):
        return {**из_, "why_not": f"кривая: модель покупки -- {пок.get('why_not')}"}
    из_["токены_модель_к_факту_пп"] = round((пок["tokens"] / т_факт - 1) * 100, 3) if т_факт else None
    пр = S.наша_продажа("curve", ст_пп, {"в_пул": 0, "tokens": 0}, т_прод, f=None, кривая_bps=bps)
    if not пр.get("ok"):
        return {**из_, "why_not": f"кривая: модель продажи -- {пр.get('why_not')}"}
    из_["модель_пп"] = round((пр["lamports"] - вход) / вход * 100, 3)
    из_["расхождение_пп"] = round(из_["факт_пп"] - из_["модель_пп"], 3)
    канд = [i for i, з in enumerate(сп) if з["slot"] <= s0 + ГОРИЗОНТ - 1]
    ст150 = ст(канд[-1]) if канд else ст0
    if ст150:
        вст = {"в_пул": пок["в_пул"], "tokens": пок["tokens"]} if (канд and (канд[-1] < ib or канд[-1] >= is_)) else {"в_пул": 0, "tokens": 0}
        п150 = S.наша_продажа("curve", ст150, вст, пок["tokens"], f=None, кривая_bps=bps)
        if п150.get("ok"):
            из_["модель_150_пп"] = round((п150["lamports"] - вход) / вход * 100, 3)
    из_["quote_mint"] = "SOL (кривая)"
    return из_


def строки_входа(вход: str) -> list:
    if вход == "a3":
        д = json.loads((КОРЕНЬ / "data" / "podbivka" / "a3_sdelki.json").read_text(encoding="utf-8"))["сделки"]
        return д
    д = json.loads(Path(вход).read_text(encoding="utf-8"))
    if isinstance(д, dict):
        д = д.get("sdelki") or д.get("позиции") or []
    из_ = []
    имена: dict = {}
    гп = КОРЕНЬ / "data" / "podbivka" / "gruppy_code1.json"
    if гп.exists():
        for г in json.loads(гп.read_text(encoding="utf-8"))["groups"].values():
            for а, y in г["addresses"].items():
                имена[y.get("name") or а[:8]] = а
    for x in д:
        if "polya" in x:                       # отчёт Code-1 data/peresborka_sdelok.json
            п = x["polya"]
            п = {**п, "имя_источника": имена.get(п.get("имя_источника"), п.get("имя_источника"))}
            из_.append({"группа": п.get("группа"), "наш_кошелёк": None, "источник": п.get("имя_источника"),
                        "mint": п.get("минт"), "buy_sig": п.get("подпись_покупки"), "sell_sig": п.get("подпись_продажи"),
                        "src_sig": п.get("подпись_источника"), "buy_ts": None,   # в отчёте только ЧЧ:ММ:СС; сутки -- Shyft
                        "code1_итог_процент": п.get("итог_процент"), "code1_s_n": п.get("s_n")})
            continue
        из_.append({"группа": x.get("group_effective") or x.get("group") or "leader", "наш_кошелёк": x["wallet"],
                    "источник": x["source"], "mint": x["mint"], "buy_sig": x["buy_sig"], "sell_sig": x.get("sell_sig"),
                    "src_sig": x.get("source_sig"), "buy_ts": x.get("buy_ts")})
    return из_


def md_таблица(ряды: list) -> str:
    def ф(v, z="+.2f"):
        return "—" if v is None else format(v, z)
    с = [р for р in ряды if р.get("расхождение_пп") is not None]
    м = sorted(abs(р["расхождение_пп"]) for р in с)
    мк = sorted(abs(р["расхождение_без_курса_пп"]) for р in с if р.get("расхождение_без_курса_пп") is not None)
    строки = [f"Сделок: {len(ряды)}, с числом: {len(с)}. Медиана модуля расхождения факт − модель: "
              f"{statistics.median(м):.2f} п.п. (p90 {м[min(len(м) - 1, int(0.9 * len(м)))]:.2f}); "
              + (f"без вклада курса котировочного: {statistics.median(мк):.2f} п.п." if мк else "курс котировочного не участвует (SOL-пулы).")
              if с else "Сделок с числом нет.", "",
              "| сделка | группа | котировка | место (свопов между) | наценка факт, % | модель +150, п.п. | модель в слоте продажи | "
              "факт, п.п. | расхождение | из них курс q | без курса | причина без числа |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for р in ряды:
        строки.append(f"| {(р.get('buy_sig') or '')[:10]} | {р.get('группа')} | {(р.get('quote_mint') or '—')[:6]} | "
                      f"{р.get('место', '—')} ({р.get('свопов_между', '—')}) | {ф(р.get('наценка_факт_пп'))} | "
                      f"{ф(р.get('модель_150_пп'))} | {ф(р.get('модель_пп'))} | {ф(р.get('факт_пп'))} | "
                      f"{ф(р.get('расхождение_пп'))} | {ф(р.get('из_них_курс_пп'))} | {ф(р.get('расхождение_без_курса_пп'))} | "
                      f"{(р.get('why_not') or '')[:50]} |")
    return "\n".join(строки)


def пересобрать(метка: str) -> int:
    """Офлайн: пересчитать таблицу из сохранённого json (без узла)."""
    суф = f"_{метка}" if метка else ""
    п = КОРЕНЬ / "data" / "podbivka" / f"sverka_leader{суф}.json"
    д = json.loads(п.read_text(encoding="utf-8"))
    for р in д["ряды"]:
        if р.get("split") and р.get("из_них_курс_пп") is not None:
            for к in ("сдвиг_курса_q_пп", "из_них_курс_пп", "расхождение_без_курса_пп"):
                р.pop(к, None)
            р["курс_q_примечание"] = "маршрут дробился: вклад курса не считается"
    п.write_text(json.dumps(д, ensure_ascii=False, indent=1), encoding="utf-8")
    (КОРЕНЬ / "docs" / f"podbivka_sverka_leader{суф}.md").write_text(
        "# Сверка сделок полосы с моделью (режим 2 на x*y=k, режим 1 на кривой pump.fun)\n\n" + md_таблица(д["ряды"]) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--vhod", default="a3")
    р.add_argument("--metka", default="")
    а = р.parse_args()
    import calendar  # noqa: PLC0415
    import time  # noqa: PLC0415
    уз = S.Узел()
    рез = []
    for x in строки_входа(а.vhod):
        bt = x.get("buy_ts")
        if isinstance(bt, str):
            bt = calendar.timegm(time.strptime(bt[:19], "%Y-%m-%dT%H:%M:%S"))
        with уз.на(S.узел_по_времени(bt) if bt else "shyft"):
            try:
                рр = одна(уз, x)
            except Exception as exc:  # noqa: BLE001
                рр = {"группа": x.get("группа"), "buy_sig": x.get("buy_sig"),
                      "why_not": S.чисто(f"{type(exc).__name__}: {exc}")[:200]}
        уз._кэш.clear()  # noqa: SLF001
        if рр.get("s0") and рр.get("slot_sell") and рр["slot_sell"] < рр["s0"] + ГОРИЗОНТ - 1 \
                and (S.узел_по_времени(bt) if bt else "shyft") == "shyft":
            рр["модель_150_пп"] = None          # на Shyft история только до нашей продажи
        рез.append(рр)
        print(рр.get("группа"), (рр.get("buy_sig") or "")[:10], рр.get("место"), рр.get("факт_пп"), рр.get("модель_пп"),
              рр.get("why_not"), flush=True)
    суф = f"_{а.metka}" if а.metka else ""
    out = КОРЕНЬ / "data" / "podbivka" / f"sverka_leader{суф}.json"
    out.write_text(json.dumps({"ряды": рез, "расход": уз.расход()}, ensure_ascii=False, indent=1), encoding="utf-8")
    (КОРЕНЬ / "docs" / f"podbivka_sverka_leader{суф}.md").write_text(
        "# Сверка сделок полосы с моделью (режим 2 на x*y=k, режим 1 на кривой pump.fun)\n\n" + md_таблица(рез) + "\n", encoding="utf-8")
    import podbivka_run as R  # noqa: PLC0415
    R.записано(out)
    R.записано(КОРЕНЬ / "docs" / f"podbivka_sverka_leader{суф}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
