#!/usr/bin/env python3
"""Подбивка A3: контроль симулятора на режиме 2 (пулы с котировкой USDC и др.)
на реальных сделках DBot: 8 сделок пилота (18–19.09, лидер) и 30 кругов BATCH-5
(25–27.09). Только чтение цепи.

Список сделок -- data/podbivka/a3_sdelki.json (офлайн из выгрузок Code-1).
По каждой сделке:
  ФАКТ (по цепи, без чаевых, приоритета и сбора DBot): SOL, вошедший в свопы
  покупки (увеличение WSOL-хранилищ пулов, не подписантов; у кривой -- лампорты
  кривой), и SOL, вышедший из свопов продажи. Ноги маршрута -- по хранилищам.
  СИМУЛЯТОР (как в таблицах шага 2 для не-SOL пулов):
    * состояние пула токена на входе S+2 -- после последней успешной сделки
      пула со слотом <= s0+2, стоящей в цепи ДО нашей покупки (наш след
      исключён); выход -- состояние прямо перед нашей продажей (наш след в нём
      есть, поэтому продаём без вставки);
    * доля траты f пула токена -- по хранилищам сделки источника:
      f = x0·dy / ((y0 − dy)·dx) (не по кошельку источника);
    * котировочный к SOL: цена из сделки источника (плечо к WSOL/USD, курс) на
      входе И на выходе, без проскальзывания в пуле q/SOL (флаги шага 2);
    * налог Token-2022 на токене и на котировочном.
  РАЗЛОЖЕНИЕ ошибки: токенов на входе (сим/факт), котировочного на выходе при
  фактически проданных токенах, курс ноги SOL→q на входе и q→SOL на выходе
  (шаг 2 против факта).
Выход: data/podbivka/a3_kontrol.json и docs/podbivka_2026-09-27_a3.md.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka as P  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
SOLы = (C.WSOL, C.NATIVE_QUOTE)


def строки(tx):
    return list(C.token_rows(tx).values())


def своп_sol(tx: dict, сторона: str, кривая: str | None = None) -> int:
    """Лампорты, вошедшие в свопы (buy) или вышедшие из них (sell): WSOL-счета
    не подписантов; у кривой pump.fun -- лампорты самой кривой."""
    sg = C.signers(tx)
    д = 0
    for r in строки(tx):
        if r["mint"] == C.WSOL and r["owner"] not in sg and r["account"]:
            x = int(r["post"]) - int(r["pre"])
            if сторона == "buy" and x > 0 or сторона == "sell" and x < 0:
                д += abs(x)
    if кривая:
        л = C.lamport_delta(tx, кривая)
        if л and (сторона == "buy" and л > 0 or сторона == "sell" and л < 0):
            д += abs(л)
    return д


def дельта_счёта(tx: dict, счёт: str):
    for r in строки(tx):
        if r["account"] == счёт:
            return int(r["pre"]), int(r["post"])
    return None


def наши_токены(tx: dict, кошелёк: str, минт: str) -> int:
    return sum(int(r["post"]) - int(r["pre"]) for r in строки(tx) if r["owner"] == кошелёк and r["mint"] == минт)


def f_по_хранилищам(tx: dict, пул: dict):
    """Доля траты, дошедшей до резерва: x0·dy / ((y0 − dy)·dx) по хранилищам."""
    кв, тв = дельта_счёта(tx, пул["quote_vault"]), дельта_счёта(tx, пул["pool_vault"])
    if not кв or not тв:
        return None
    x0, x1 = кв
    y0, y1 = тв
    dx, dy = x1 - x0, y0 - y1
    if dx <= 0 or dy <= 0 or y0 <= dy:
        return None
    return x0 * dy / ((y0 - dy) * dx)


def нога_q(tx: dict, q: str, сторона: str):
    """Курс ноги SOL<->q в нашей сделке: лампорты за сырую единицу q (по хранилищам
    пула q/SOL: там, где WSOL и q движутся навстречу в одной инструкции)."""
    sg = C.signers(tx)
    ряды = [r for r in строки(tx) if r["account"] and r["owner"] not in sg]
    for s in C.instruction_account_sets(tx):
        w = [r for r in ряды if r["mint"] == C.WSOL and r["account"] in s and r["post"] != r["pre"]]
        k = [r for r in ряды if r["mint"] == q and r["account"] in s and r["post"] != r["pre"]]
        for a in w:
            for b in k:
                da, db = int(a["post"]) - int(a["pre"]), int(b["post"]) - int(b["pre"])
                if (da > 0) != (db > 0) and (сторона == "buy") == (da > 0):
                    return abs(da) / abs(db), a["owner"]
    return None, None


def одна(уз: S.Узел, с: dict) -> dict:
    import podbivka_lider_kotirovka as LK  # noqa: PLC0415
    из_ = {к: с.get(к) for к in ("группа", "mint", "buy_sig", "источник")}
    tb = уз.tx(с["buy_sig"])
    if not tb:
        return {**из_, "why_not": "узел не отдал нашу покупку"}
    наш = с["наш_кошелёк"]
    # продажа: из выгрузки или первая наша продажа минта после покупки
    sell = с.get("sell_sig")
    if not sell:
        for з in reversed(уз.подписи(наш, limit=1000)):
            if (з.get("slot") or 0) <= tb["slot"] or з.get("err") is not None:
                continue
            т = уз.tx(з["signature"])
            if т and наши_токены(т, наш, с["mint"]) < 0:
                sell = з["signature"]
                break
    ts = уз.tx(sell) if sell else None
    if not ts:
        return {**из_, "why_not": "продажа не найдена"}
    из_["sell_sig"] = sell
    пул = C.identify_pool(tb, наш, с["mint"])
    q = пул.get("quote_mint")
    из_.update(quote_mint=q, pool_vault=пул.get("pool_vault"), slot_buy=tb["slot"], slot_sell=ts["slot"])
    if not пул.get("pool_vault") or not пул.get("quote_vault"):
        return {**из_, "why_not": "пул токена не опознан в нашей покупке"}
    кривая = пул.get("pool_owner") if q == C.NATIVE_QUOTE else None
    # источник: по истории хранилища пула до нашей покупки (before = наша
    # подпись -- она из истории этого адреса), первая сделка, где источник
    # подписант и получил токен.
    src = с.get("src_sig")
    if not src:
        сп0 = [з for з in уз.подписи(с["mint"], до=с["buy_sig"], limit=1000)
               if з.get("err") is None and (з.get("slot") or 0) >= tb["slot"] - 600]
        for i in range(0, len(сп0), 25):
            пачка = уз.пакет([з["signature"] for з in сп0[i:i + 25]])
            for з in сп0[i:i + 25]:
                т = пачка.get(з["signature"])
                if т and с["источник"] in C.signers(т) and наши_токены(т, с["источник"], с["mint"]) > 0:
                    src = з["signature"]
                    break
            if src:
                break
    tsrc = уз.tx(src) if src else None
    if not tsrc:
        return {**из_, "why_not": "сделка источника не найдена"}
    s0 = tsrc["slot"]
    из_.update(src_sig=src, s0=s0, сдвиг_нашей_покупки=tb["slot"] - s0)
    # ФАКТ
    вход = своп_sol(tb, "buy", кривая)
    выход = своп_sol(ts, "sell", кривая)
    т_факт = наши_токены(tb, наш, с["mint"])
    т_прод = -наши_токены(ts, наш, с["mint"])
    из_.update(факт_вход_лам=вход, факт_выход_лам=выход, факт_токенов=т_факт, продано_токенов=т_прод,
               факт_пп=round((выход - вход) / вход * 100, 3) if вход else None)
    кв_b, кв_s = дельта_счёта(tb, пул["quote_vault"]), дельта_счёта(ts, пул["quote_vault"])
    q_в_пул = (кв_b[1] - кв_b[0]) if кв_b else None
    q_из_пула = (кв_s[0] - кв_s[1]) if кв_s else None
    из_.update(факт_q_в_пул=q_в_пул, факт_q_из_пула=q_из_пула)
    # СИМУЛЯТОР (прогон 2, 27.09): курс котировочного -- по пулу q/SOL нашего
    # маршрута; доля продажи -- по настоящим продажам пула; SOL-пулы --
    # калибровка по кошельку источника (как в основном симуляторе).
    if q == C.NATIVE_QUOTE:
        return {**из_, "why_not": "кривая pump.fun (котировка SOL) -- вне режима 2, только факт"}
    из_["split"] = bool(пул.get("split"))
    пул_ист = C.identify_pool(tsrc, с["источник"], с["mint"])
    из_["пул_источника_тот_же"] = пул_ист.get("pool_vault") == пул["pool_vault"]
    f = None
    if q in SOLы:
        сим0 = P.наша_покупка_по_симулятору(tsrc, с["источник"], с["mint"], вход)
        f = сим0.get("fee_factor")
        из_["f_откуда"] = "кошелёк источника (основной симулятор)" if f else None
    elif из_["пул_источника_тот_же"]:
        f = f_по_хранилищам(tsrc, пул)
        из_["f_откуда"] = "хранилища, сделка источника" if f else None
    нал_т = S.налог_минта(уз, с["mint"])
    нал_q = S.налог_минта(уз, q) if q not in SOLы else {"bps": 0}
    из_.update(налог_токена_bps=нал_т.get("bps"), налог_q_bps=нал_q.get("bps"))
    ист = S.история_пула(уз, пул["pool_vault"], src, s0,
                         опора=(S.подпись_после_слота(уз, ts["slot"] + 1) if уз.текущий == "helius" else None),
                         до_слота=ts["slot"])
    if ист["why_not"] or ист["предел"]:
        return {**из_, "why_not": f"история пула: {ист['why_not'] or 'предел страниц'}"}
    сп = [з for з in ист["подписи"] if з["ok"]]
    ib = next((i for i, з in enumerate(сп) if з["signature"] == с["buy_sig"]), None)
    is_ = next((i for i, з in enumerate(сп) if з["signature"] == sell), None)
    if ib is None or is_ is None:
        return {**из_, "why_not": "наши сделки не найдены в истории пула"}
    if not f:
        for i in range(ib - 1, max(-1, ib - 30), -1):
            f = f_по_хранилищам(уз.tx(сп[i]["signature"]), пул)
            if f and 0.5 <= f <= 1.0:
                из_["f_откуда"] = "хранилища, ближайшая покупка в пуле до нас"
                break
            f = None
    из_["f"] = round(f, 5) if f else None
    if not f or not (0.5 <= f <= 1.0):
        return {**из_, "why_not": f"доля траты не калибруется ({f})"}
    # доля продажи -- по продажам пула между нашей покупкой и продажей
    доли = []
    for i in range(ib + 1, is_):
        т = уз.tx(сп[i]["signature"])
        кв, тв = дельта_счёта(т, пул["quote_vault"]), дельта_счёта(т, пул["pool_vault"])
        if кв and тв and тв[1] > тв[0] and кв[1] < кв[0] and кв[0] > 0:
            x0, dy, dx = кв[0], тв[1] - тв[0], кв[0] - кв[1]
            g_ = dx * (тв[0] + dy) / (x0 * dy)
            if 0.5 <= g_ <= 1.0:
                доли.append(g_)
        if len(доли) >= 6:
            break
    g = statistics.median(доли) if доли else f
    из_["g"], из_["g_откуда"] = round(g, 5), (f"продажи пула ({len(доли)})" if доли else "продаж нет: g = f")

    def ст_после(i):
        return S.состояние(уз.tx(сп[i]["signature"]), "xyk", пул, с["mint"]) if i is not None and i >= 0 else None
    до_нас = [i for i in range(ib) if сп[i]["slot"] <= s0 + 2]
    ст_вход = ст_после(до_нас[-1]) if до_нас else S.состояние(tsrc, "xyk", пул, с["mint"])
    ст_выход = ст_после(is_ - 1)
    if not ст_вход or not ст_выход:
        return {**из_, "why_not": "состояние пула не читается"}
    # нога SOL <-> q
    if q in SOLы:
        q_raw = вход
        нога = None
    else:
        нв, нв_ист, нп = пул_q(tb, q), пул_q(tsrc, q), пул_q(ts, q)
        if not нв or not нп:
            return {**из_, "why_not": "нога SOL<->q в нашей сделке не найдена"}
        if нв_ист and нв_ист["wsol_vault"] == нв["wsol_vault"]:
            W, Q, fl = нв_ист["W1"], нв_ист["Q1"], нв_ист["f"]
            из_["нога_откуда"] = "пул q/SOL после сделки источника, калибровка на ней"
        else:
            W, Q, fl = нв["W0"], нв["Q0"], нв["f"]
            из_["нога_откуда"] = "пул q/SOL перед нашей покупкой, калибровка на нашей ноге (флаг)"
        if not fl:
            return {**из_, "why_not": "нога SOL->q не калибруется"}
        q_raw = int(Q * fl * вход / (W + fl * вход))
        нога = (нп, fl)
        из_["нога_f"] = round(fl, 5)
        из_["q_вход_сим_к_факту_пп"] = round((q_raw / q_в_пул - 1) * 100, 3) if q_в_пул else None
    q_raw -= S.удержано(q_raw, нал_q)
    пок = S.наша_покупка("xyk", ст_вход, f=f, кривая_bps=(0, 0), размер=q_raw)
    if not пок.get("ok"):
        return {**из_, "why_not": f"покупка: {пок.get('why_not')}"}
    т_сим = пок["tokens"] - S.удержано(пок["tokens"], нал_т)
    из_["сим_токенов"] = т_сим
    из_["ошибка_токенов_пп"] = round((т_сим / т_факт - 1) * 100, 3) if т_факт else None
    пусто = {"в_пул": 0, "tokens": 0}

    def продажа(токенов):
        в_пул = токенов - S.удержано(токенов, нал_т)
        пр = S.наша_продажа("xyk", ст_выход, пусто, в_пул, f=f, кривая_bps=(0, 0), g=g)
        return пр["lamports"] if пр.get("ok") else None
    q_сим, q_ф = продажа(т_сим), продажа(т_прод)
    if q_сим is None:
        return {**из_, "why_not": "продажа не считается"}
    из_["ошибка_q_выхода_пп"] = round((q_ф / q_из_пула - 1) * 100, 3) if q_ф and q_из_пула else None
    q_сим -= S.удержано(q_сим, нал_q)
    if нога:
        нп, fl = нога
        выход_сим = int(нп["W0"] * fl * q_сим / (нп["Q0"] + fl * q_сим))
        if q_из_пула and выход:
            ф_курс = выход / q_из_пула
            м_курс = нп["W0"] * fl * q_из_пула / (нп["Q0"] + fl * q_из_пула) / q_из_пула
            из_["нога_выход_сим_к_факту_пп"] = round((м_курс / ф_курс - 1) * 100, 3)
    else:
        выход_сим = q_сим
    из_["сим_пп"] = round((выход_сим - вход) / вход * 100, 3)
    из_["ошибка_пп"] = round(из_["сим_пп"] - из_["факт_пп"], 3) if из_.get("факт_пп") is not None else None
    из_["флаги"] = ["нога q/SOL на выходе -- состояние перед нашей продажей, калибровка ноги входа"]
    return из_


def пул_q(tx: dict, q: str):
    """Пул q/SOL в сделке: WSOL- и q-хранилища не подписантов, движущиеся навстречу
    в одной инструкции; резервы до/после и калибровка f по хранилищам."""
    sg = C.signers(tx)
    ряды = [r for r in строки(tx) if r["account"] and r["owner"] not in sg]
    for s in C.instruction_account_sets(tx):
        w = [r for r in ряды if r["mint"] == C.WSOL and r["account"] in s and r["post"] != r["pre"]]
        k = [r for r in ряды if r["mint"] == q and r["account"] in s and r["post"] != r["pre"]]
        for a in w:
            for b in k:
                if a["owner"] != b["owner"]:
                    continue
                W0, W1, Q0, Q1 = int(a["pre"]), int(a["post"]), int(b["pre"]), int(b["post"])
                f = None
                if W1 > W0 and Q1 < Q0 and Q0 > (Q0 - Q1):        # SOL -> q (покупка q)
                    dW, dQ = W1 - W0, Q0 - Q1
                    f = W0 * dQ / ((Q0 - dQ) * dW)
                elif W1 < W0 and Q1 > Q0:                          # q -> SOL: доля по выходу
                    dQ, dW = Q1 - Q0, W0 - W1
                    f = dW * (W0 and (Q0 + dQ)) / (W0 * dQ) if W0 else None
                return {"wsol_vault": a["account"], "q_vault": b["account"], "W0": W0, "W1": W1,
                        "Q0": Q0, "Q1": Q1, "f": f if f and 0.5 <= f <= 1.0 else None}
    return None


def main() -> int:
    с = json.loads((КОРЕНЬ / "data" / "podbivka" / "a3_sdelki.json").read_text(encoding="utf-8"))["сделки"]
    уз = S.Узел()
    рез = []
    for x in с:
        with уз.на(S.узел_по_времени(x.get("buy_ts"))):
            try:
                р = одна(уз, x)
            except Exception as exc:  # noqa: BLE001
                р = {"группа": x["группа"], "mint": x["mint"], "why_not": S.чисто(f"{type(exc).__name__}: {exc}")[:200]}
        уз._кэш.clear()  # noqa: SLF001
        рез.append(р)
        print(р.get("группа"), (р.get("mint") or "")[:8], р.get("факт_пп"), р.get("сим_пп"), р.get("why_not"), flush=True)
    ош = [abs(р["ошибка_пп"]) for р in рез if р.get("ошибка_пп") is not None]
    свод = {"сделок": len(рез), "с_числом": len(ош),
            "медиана_модуля": round(statistics.median(ош), 3) if ош else None,
            "p90_модуля": round(sorted(ош)[min(len(ош) - 1, int(0.9 * len(ош)))], 3) if ош else None}
    (КОРЕНЬ / "data" / "podbivka" / "a3_kontrol.json").write_text(
        json.dumps({"свод": свод, "ряды": рез, "расход": уз.расход()}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("A3:", json.dumps(свод, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
