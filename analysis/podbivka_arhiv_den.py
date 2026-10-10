#!/usr/bin/env python3
"""Подбивка: сутки архива PumpApi (Historical Replay) -- покупки наших адресов и
модель режима 1 на рядах событий пула. Только чтение, без ключей и RPC.

Один проход по часовым файлам https://replay.pumpapi.io/Y/M/D/HH.jsonl.zst (имя --
час НАЧАЛА) в порядке времени:
  * события наших адресов (data/podbivka/arhiv_adresa.json; txSigner или трейдер
    в breakdown) -- все покупки и продажи, кратко;
  * сигнал -- первая покупка (баланс токена после = купленному, допуск 1 %) от
    --porog SOL-экв. в SOL-пуле поддержанного типа; с сигнала пул «активен»
    --okno слотов: все его события пишутся в ряд;
  * цели (--celi, подписи): их события пишутся целиком -- для сверок;
  * рост перед покупкой: у каждого события наших адресов и сигнала -- цена пула перед
    ним к наименьшей цене пула за 750 и 150 слотов до него (история цены всех пулов,
    скользящая; цена -- по полям резервов события);
  * --dop-adresa -- ещё адреса (события и сигналы по --porog); --istochniki -- адреса
    с порогом сигнала --porog-dop; у каждого сигнала -- рисунок (рост к s0+30,
    падение от пика к s0+75, продавцы после пика).
Модель режима 1 на ряду пула (всё в SOL и токенах, UI-единицы):
  * состояние после события: x*y=k (pump-amm, raydium-cpmm, meteora-damm-v1) --
    quoteInPool / tokensInPool; кривая (pump, raydium-launchpad) -- виртуальные
    vQuoteInBondingCurve / vTokensInBondingCurve;
  * доля траты f и доля продажи g (x*y=k) -- медианы по ВСЕМ покупкам / продажам
    пула в окне (состояние до -- предыдущее событие ряда); нет -- 1 − poolFeeRate;
    Pump AMM (v6, 01.10) -- калибровка_pump_amm: f к полной трате, g на руки (quoteAmount·(1 − fee)) до 1.6,
    окно s0…s0+150 (≤ 150 событий), нет продаж -- g 0.985; наша покупка на выходе -- X + билет;
    кривая -- комиссия poolFeeRate сверху при покупке и из выручки при продаже;
  * входы: S0 -- сразу после сделки источника; S0_дно -- после последнего события
    слота s0 (порядок внутри слота -- порядок файла, timestamp мс; оценка);
    S1 -- после последнего события слота s0+1;
  * выход +H -- состояние после последнего события со слотом <= s0+H−1, наша
    покупка вставлена (потолок); билеты --bilety (0.3 и 0.5 SOL; pyg9 -- ещё 1 и 3), минус 0.002 SOL на круг;
  * резерв пула после события (quoteInPool / vQuoteInBondingCurve и в SOL-экв.) -- у событий наших адресов,
    сигналов и покупок источников (pyg9);
  * налог Token-2022 не учтён (флаг);
  * модель сигнала считается в момент, когда окно его пула закрылось (ряд после этого отпускается -- иначе ряды
    всех сигналов суток живут до конца прогона: на 997 адресах прогон убит ядром, код -9, 01.10). Следствие,
    проверенное A/B на часе 30.09 18Z против суточного файла (784 общих сигнала): f, g, fee и ВСЕ п.п. совпадают
    до знака, в том числе входы N16 / N50; перестали заполняться только три справочных поля, которым нужны данные
    ЗА пределами окна и которые прежде заполнялись случайно -- если пул дожил из-за чужого сигнала:
    «N16_слотов» / «N50_слотов» (через сколько слотов 16-я и 50-я покупка после сигнала, 23 и 92 случая из 784)
    и «удержание.секунд_до[150]» (28 из 784; прежние значения вида 1358 и 4991 с -- это пул без событий 20–80 мин,
    то есть мера всё равно негодная).
Выход: data/podbivka/arhiv_den/<метка>.json.
"""
from __future__ import annotations

import argparse
import collections
import calendar
import contextlib
import io
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
WSOL = "So11111111111111111111111111111111111111112"
USD = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
XYK = {"pump-amm", "raydium-cpmm", "meteora-damm-v1"}
КРИВЫЕ = {"pump", "raydium-launchpad"}
ВЫХОДЫ = (6, 12, 24, 30, 36, 60, 72, 108, 150, 300)   # 108 -- удержание живой полосы
# lane_s0; 30, 60 и 300 добавлены 04.10 под пакет владельца (сравнение горизонтов выхода).
БИЛЕТЫ = (0.3, 0.5)
ОКНО_ВСЕМ = False       # --okno-vsem: правило окна докупки для всех наших адресов
БЕЗ_АДРЕСОВ = False     # --bez-adresov: только цели (--celi) и их ряды -- без событий и сигналов наших адресов
БЕЗ_СОБЫТИЙ = False     # --bez-sobytij: не писать "наши_события" (правилу кандидатов нужны только сигналы)
ПОДПИСАНТ = ""          # --podpisant: адрес первого подписанта (сервер бота). Покупки, где txSigner -- он, считаются
                        # сигналами для КОШЕЛЬКОВ из breakdown (их события в "наши_события" не пишутся -- только сигналы)
ИЗДЕРЖКИ = 0.002
КЛЮЧИ = ("signature", "action", "pool", "poolId", "mint", "quoteMint", "txSigner", "tokenAmount", "quoteAmount",
         "tokensInPool", "quoteInPool", "vTokensInBondingCurve", "vQuoteInBondingCurve", "poolFeeRate",
         "priorityFee", "block", "timestamp")

р_sig = re.compile(r'"signature":\s*"([1-9A-HJ-NP-Za-km-z]{64,90})"')
р_pool = re.compile(r'"poolId":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_signer = re.compile(r'"txSigner":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_block = re.compile(r'"block":\s*(\d+)')
р_q = re.compile(r'"(quoteInPool|tokensInPool|vQuoteInBondingCurve|vTokensInBondingCurve)":\s*"?([0-9.eE+-]+)')
НАЗАД = 750            # слотов истории цены пула до события (≈ 5 мин) -- рост перед покупкой
р_qmint = re.compile(r'"quoteMint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_mint = re.compile(r'"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_trader = re.compile(r'"trader":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_action = re.compile(r'"action":\s*"(buy|sell)"')
р_qam = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')


def резерв(e: dict, курс_q: float | None) -> dict:
    """Резерв котировки пула ПОСЛЕ события (поля архива): quoteInPool -- настоящий (AMM; у кривой -- если есть),
    vQuoteInBondingCurve -- виртуальный кривой; и в SOL-экв. по курсу котировки события (SOL -- 1)."""
    def ч(k):
        try:
            return float(e.get(k)) if e.get(k) not in (None, "") else None
        except (TypeError, ValueError):
            return None
    x, vx = ч("quoteInPool"), ч("vQuoteInBondingCurve")
    к = 1.0 if e.get("quoteMint") == WSOL else курс_q
    return {"резерв_q": x, "вирт_резерв_q": vx, "резерв_sol": round(x * к, 4) if (x is not None and к) else None,
            "вирт_резерв_sol": round(vx * к, 4) if (vx is not None and к) else None}


def курс(ts):
    import podbivka_tablicy as T  # noqa: PLC0415
    return T.курс_детектора(ts)


def ст(e: dict):
    if e.get("pool") in КРИВЫЕ:
        x, y = e.get("vQuoteInBondingCurve"), e.get("vTokensInBondingCurve")
    else:
        x, y = e.get("quoteInPool"), e.get("tokensInPool")
    if not x or not y:
        return None
    return float(x), float(y)


PUMP_AMM_V6 = True      # модель Pump AMM v6 (сверка по цепи 28–30.09): f по хранилищам, g до 1.6, окно как у v6
V6_ГОРИЗОНТ, V6_МАКС_СОБЫТИЙ, V6_ДО, V6_G_ВЕРХ, V6_G_БЕЗ_ПРОДАЖ = 150, 150, 30, 1.6, 0.985


def калибровка_pump_amm(пары: list, fee: float, f_по: str = "хранилищам", f_источника: float | None = None) -> dict:
    """Pump AMM, v6 (analysis/podbivka_sverka_leader.py): пары (предыдущее событие ряда, событие) окна.
    f -- x0·dy / ((y0 − dy)·dx): «хранилищам» -- dx прирост quoteInPool, dy убыль tokensInPool к предыдущему событию;
    «quoteAmount» -- dx = quoteAmount, dy = tokenAmount. В архиве quoteInPool растёт на ВЕСЬ quoteAmount покупки (с
    комиссиями протокола и создателя; ревью 01.10: медиана Δ/quoteAmount 1.000), поэтому оба дают f к полной трате
    -- то, что нужно для билета «сколько отдали». До 30 прямых покупок, [0.5, 1.0].
    g -- на руки продавцу: quoteAmount·(1 − poolFeeRate)·(y0 + dy)/(x0·dy), dy = tokenAmount (quoteAmount продажи в
    архиве -- ДО комиссии пула), [0.5, 1.6]; продажи копятся, пока и покупок, и продаж не наберётся по 30 (как v6).
    Нет покупок -- f сделки источника (f_источника), иначе 1 − fee; нет продаж -- 0.985 (v6: G_БЕЗ_ПРОДАЖ)."""
    fs, gs = [], []
    for пред, e in пары:
        if len(fs) >= V6_ДО and len(gs) >= V6_ДО:
            break
        с0 = ст(пред)
        if not с0:
            continue
        x0, y0 = с0
        if e.get("action") == "buy" and len(fs) < V6_ДО:
            v = f_пары(пред, e, f_по)
            if v is not None and 0.5 <= v <= 1.0:
                fs.append(v)
        elif e.get("action") == "sell":
            # quoteAmount продажи в архиве -- ДО комиссии пула: на руки = quoteAmount·(1 − poolFeeRate)
            # (наши 9 продаж 28.09: quoteAmount / полученное по цепи = 1/(1 − poolFeeRate) × 1.001)
            dy = float(e.get("tokenAmount") or 0)
            dx = float(e.get("quoteAmount") or 0) * (1 - float(e.get("poolFeeRate") or 0))
            if dy > 0 and dx > 0 and x0 > 0:
                v = dx * (y0 + dy) / (x0 * dy)
                if 0.5 <= v <= V6_G_ВЕРХ:
                    gs.append(v)
    f_зап = f_источника if (f_источника is not None and 0.5 <= f_источника <= 1.0) else 1 - fee
    return {"f": statistics.median(fs) if fs else f_зап, "g": statistics.median(gs) if gs else V6_G_БЕЗ_ПРОДАЖ,
            "f_n": len(fs), "g_n": len(gs)}


def f_пары(пред: dict, e: dict, f_по: str = "хранилищам"):
    """f одной покупки к состоянию после предыдущего события ряда (см. калибровка_pump_amm)."""
    с0 = ст(пред)
    if not с0:
        return None
    x0, y0 = с0
    if f_по == "хранилищам":
        с1 = ст(e)
        if not с1:
            return None
        dx, dy = с1[0] - x0, y0 - с1[1]
    else:
        dx, dy = float(e.get("quoteAmount") or 0), float(e.get("tokenAmount") or 0)
    return x0 * dy / ((y0 - dy) * dx) if dx > 0 and y0 > dy > 0 else None


def модель(сигнал: dict, ряд: list) -> dict:
    """Ряд -- события пула в порядке файла, первым -- событие сигнала (или раньше).
    Котировка не SOL: билет и издержки в единицах котировки по курсу сигнала
    (сигнал["курс_q"] -- SOL за единицу котировки); курс на выходе = на входе (флаг)."""
    масштаб = 1.0 / сигнал["курс_q"] if сигнал.get("курс_q") else 1.0
    пул = сигнал["pool"]
    s0 = сигнал["block"]
    i0 = next((i for i, e in enumerate(ряд) if e["signature"] == сигнал["signature"]), None)
    if i0 is None:
        return {"why_not": "сигнал не найден в ряду"}
    f_ист = f_пары(ряд[i0 - 1], ряд[i0]) if (i0 > 0 and пул == "pump-amm") else None
    ряд = ряд[i0:]
    fee = float(ряд[0].get("poolFeeRate") or 0)
    из_ = {"f": None, "g": None, "fee": fee}
    if пул == "pump-amm" and PUMP_AMM_V6:
        # окно v6: события после сигнала до s0+150, не больше 150 (в этих пределах ряд непрерывен -- пул под
        # наблюдением с сигнала; дальше ряд мог прерваться и снова начаться с другого сигнала)
        окно = [e for e in ряд[1:] if (e.get("block") or 0) <= s0 + V6_ГОРИЗОНТ][:V6_МАКС_СОБЫТИЙ]
        из_.update(калибровка_pump_amm(list(zip([ряд[0]] + окно, окно)), fee, f_источника=f_ист))
        из_["модель_pump_amm"] = "v6"
    elif пул in XYK:
        fs, gs = [], []
        for пред, e in zip(ряд, ряд[1:]):
            с0 = ст(пред)
            if not с0 or not e.get("tokenAmount") or not e.get("quoteAmount"):
                continue
            x0, y0 = с0
            dy, dx = float(e["tokenAmount"]), float(e["quoteAmount"])
            if e["action"] == "buy" and y0 > dy > 0 and dx > 0:
                v = x0 * dy / ((y0 - dy) * dx)
                if 0.5 <= v <= 1.0:
                    fs.append(v)
            elif e["action"] == "sell" and dy > 0 and x0 > 0:
                v = dx * (y0 + dy) / (x0 * dy)
                if 0.5 <= v <= 1.0:
                    gs.append(v)
        из_["f"] = statistics.median(fs) if fs else 1 - fee
        из_["g"] = statistics.median(gs) if gs else 1 - fee
        из_["f_n"], из_["g_n"] = len(fs), len(gs)
    # точки входа
    def последнее(слот_до):
        канд = [e for e in ряд if e["block"] <= слот_до and ст(e)]
        return канд[-1] if канд else None
    # S2 добавлена 04.10 (задание владельца про допуск по цене): у полосы по журналу
    # вход бывает и в s0+2, а состояний этого слота в прошлых проходах не было.
    входы = {"S0": ряд[0], "S0_дно": последнее(s0), "S1": последнее(s0 + 1),
             "S2": последнее(s0 + 2)}
    # «наше место»: после N покупок того же пула после сигнала (N -- медиана 16 и p80 50 из места в блоке, 29.09)
    пок_после = [e for e in ряд[1:] if e.get("action") == "buy" and ст(e)]
    for n in (16, 50):
        if len(пок_после) >= n:
            входы[f"N{n}"] = пок_после[n - 1]
            из_[f"N{n}_слотов"] = (пок_после[n - 1].get("block") or s0) - s0
    res = {}
    # состояния пула на входе и на выходах: по ним ЛЮБОЙ билет и сдвиг цены считаются офлайн,
    # без нового прохода архива (вопрос владельца 03.10 про билеты 1 и 3 и «сколько съедает сдвиг»)
    состояния: dict = {"вход": {}, "выход": {}, "масштаб": масштаб}
    # ВЫХОДЫ ПО СОБЫТИЮ (добавлено 04.10 под пакет владельца, п. Б.5): состояние пула на слоте
    # пика цены, первой ЧУЖОЙ продажи и первой продажи САМОГО источника. Без них выход «вслед за
    # продажей источника», хвост от пика и фиксация половины офлайн не считаются вовсе.
    _п0 = ст(ряд[0])
    if _п0:
        _цена0 = _п0[0] / _п0[1]
        _пик_e, _пик_ц = None, _цена0
        _чужая_e = _своя_e = None
        for e in ряд[1:]:
            b = e.get("block") or 0
            if b > s0 + max(ВЫХОДЫ):
                break
            с_ = ст(e)
            if с_ and с_[1] > 0 and с_[0] / с_[1] > _пик_ц:
                _пик_ц, _пик_e = с_[0] / с_[1], e
            if e.get("action") == "sell":
                кто = e.get("трейдер") or e.get("txSigner")
                if кто == сигнал["trader"]:
                    if _своя_e is None:
                        _своя_e = e
                elif _чужая_e is None:
                    _чужая_e = e
        for имя_, e_ in (("пик", _пик_e), ("чужая_продажа", _чужая_e),
                         ("продажа_источника", _своя_e)):
            с_ = ст(e_) if e_ else None
            if с_:
                состояния["выход"][имя_] = [с_[0], с_[1], e_.get("block")]
    for имя, e_вх in входы.items():
        с_вх = ст(e_вх) if e_вх else None
        if not с_вх:
            continue
        x, y = с_вх
        состояния["вход"][имя] = [x, y, e_вх.get("block")]
        for a_sol in БИЛЕТЫ:
            a = a_sol * масштаб
            if пул in XYK:
                т = y * из_["f"] * a / (x + из_["f"] * a)
                вст_x = a if (пул == "pump-amm" and PUMP_AMM_V6) else из_["f"] * a   # архив: quoteInPool растёт на весь quoteAmount
            else:
                net = a / (1 + fee)
                т = y * net / (x + net)
                вст_x = net
            if т <= 0:
                continue
            for H in ВЫХОДЫ:
                if (e_вх.get("block") or s0) > s0 + H - 1:
                    continue                 # вход позже выхода (N-й покупки не было до s0+H)
                e_вых = последнее(s0 + H - 1)
                с_вых = ст(e_вых) if e_вых else None
                if not с_вых:
                    continue
                состояния["выход"][str(H)] = [с_вых[0], с_вых[1], e_вых.get("block")]
                X, Y = с_вых[0] + вст_x, с_вых[1] - т
                if Y <= 0:
                    continue
                if пул in XYK:
                    out = X * из_["g"] * т / (Y + т)
                else:
                    out = X * т / (Y + т) * (1 - fee)
                res[f"{имя}|{a_sol}|{H}"] = round((out - a - ИЗДЕРЖКИ * масштаб) / a * 100, 3)
    из_["пп"] = res
    из_["состояния"] = состояния
    из_["наценка_S1_пп"] = None
    return из_


def рисунок(сигнал: dict, ряд: list, вверх: int = 30, вниз: int = 75) -> dict:
    """«≥ +20 % к s0+30 и ≥ −25 % от пика к s0+75»: p0 -- цена после события сигнала;
    пик -- наибольшая цена после событий слотов (s0, s0+вверх]; падение -- наименьшая
    цена после пика до s0+вниз включительно, к пику. Продавцы -- продажи после пика
    до s0+вниз (txSigner, quoteAmount, tokenAmount)."""
    s0 = сигнал["block"]
    i0 = next((i for i, e in enumerate(ряд) if e["signature"] == сигнал["signature"]), None)
    if i0 is None or not ст(ряд[i0]):
        return {"why_not": "сигнал не найден в ряду"}
    x, y = ст(ряд[i0])
    p0 = x / y
    пос = [(i, e) for i, e in enumerate(ряд) if i > i0 and s0 < (e.get("block") or 0) <= s0 + вверх and ст(e)]
    if not пос:
        return {"p0": p0, "вверх_пп": 0.0, "паттерн": False, "событий_до_30": 0}
    ip, ep = max(пос, key=lambda t: ст(t[1])[0] / ст(t[1])[1])
    пик = ст(ep)[0] / ст(ep)[1]
    после = [e for i, e in enumerate(ряд) if i > ip and (e.get("block") or 0) <= s0 + вниз and ст(e)]
    дно = min((ст(e)[0] / ст(e)[1] for e in после), default=пик)
    продавцы = [{"кто": e.get("трейдер") or e.get("txSigner"), "q": e.get("quoteAmount"), "t": e.get("tokenAmount"), "slot": e.get("block")}
                for e in после if e.get("action") == "sell"]
    вверх_пп, вниз_пп = (пик / p0 - 1) * 100, (дно / пик - 1) * 100
    return {"p0": p0, "пик_слот": ep.get("block"), "вверх_пп": round(вверх_пп, 2), "от_пика_пп": round(вниз_пп, 2),
            "паттерн": вверх_пп >= 20 and вниз_пп <= -25, "продавцы_после_пика": продавцы,
            "событий_до_30": len(пос)}


def удержание(покупка: dict, ряд: list, окно: int = 150) -> dict:
    """После покупки (сигнала или покупки источника): через сколько слотов первая чужая продажа
    от 0.5 SOL-экв. (кошелёк -- по трейдеру) и первая чужая продажа любого размера; слот и
    величина пика цены пула в s0..s0+окно (к цене после покупки)."""
    i0 = next((i for i, e in enumerate(ряд) if e["signature"] == покупка["signature"]), None)
    if i0 is None or not ст(ряд[i0]):
        return {"why_not": "покупка не найдена в ряду"}
    s0 = покупка["block"]
    курс_q = покупка.get("курс_q") or 1.0
    x0, y0 = ст(ряд[i0])
    p0 = x0 / y0
    пр05 = пр_любая = None
    пик, пик_слот = p0, s0
    t0 = ряд[i0].get("timestamp") or 0
    секунд: dict = {}           # секунд от покупки до первого события пула на слоте ≥ s0+h (метки архива)
    for e in ряд[i0 + 1:]:
        b = e.get("block") or 0
        for h in (72, 108, 150):
            if h not in секунд and b >= s0 + h and e.get("timestamp") and t0:
                секунд[h] = round((e["timestamp"] - t0) / 1000, 2)
        if b > s0 + окно:
            break
        с_ = ст(e)
        if с_ and с_[0] / с_[1] > пик:
            пик, пик_слот = с_[0] / с_[1], b
        if e.get("action") == "sell" and (e.get("трейдер") or e.get("txSigner")) != покупка["trader"]:
            if пр_любая is None:
                пр_любая = b - s0
            if пр05 is None and float(e.get("quoteAmount") or 0) * курс_q >= 0.5:
                пр05 = b - s0
    return {"первая_продажа_05_слотов": пр05, "первая_продажа_слотов": пр_любая,
            "пик_слотов": пик_слот - s0, "пик_пп": round((пик / p0 - 1) * 100, 2),
            "секунд_до": {str(h): v for h, v in секунд.items()}}


class _ЛокальныйЧас:
    status_code = 200

    def __init__(self, путь: Path):
        os.utime(путь)                 # отметка «читали» -- по ней чистка кэша убирает давно не нужные часы
        self.raw = open(путь, "rb")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.raw.close()


def открыть_час(requests, url: str, ч: str):
    """Без PODB_ARHIV_KESH -- поток прямо с replay.pumpapi.io (облако). С ним (бегунок lab-miami) -- час сперва целиком
    на локальный диск (атомарно: .part → замена), дальше читается с диска; повторные прогоны качать уже не будут."""
    кэш = os.environ.get("PODB_ARHIV_KESH")
    if not кэш:
        return requests.get(url, stream=True, timeout=120)
    путь = Path(кэш) / f"{ч}.jsonl.zst"
    if not путь.exists():
        путь.parent.mkdir(parents=True, exist_ok=True)
        часть = путь.with_name(f"{путь.name}.{os.getpid()}.part")
        with requests.get(url, stream=True, timeout=120) as о:
            if о.status_code != 200:
                return contextlib.nullcontext(о)
            длина = int(о.headers.get("Content-Length") or 0)
            with open(часть, "wb") as ф:
                for кусок in о.iter_content(1 << 20):
                    ф.write(кусок)
        if длина and часть.stat().st_size != длина:
            часть.unlink(missing_ok=True)
            raise IOError(f"час скачан не целиком: {длина} байт заявлено")
        os.replace(часть, путь)
    return _ЛокальныйЧас(путь)


def посчитать(с: dict, ряд: list, окно: int) -> dict:
    """Все поля сигнала, которым нужен ряд пула: модель, рисунок, удержание, события окна, свопы s0+1 / s0+2."""
    с["модель"] = модель(с, ряд)
    с["рисунок"] = рисунок(с, ряд)
    с["удержание"] = удержание(с, ряд)
    с["событий_пула_в_окне"] = sum(1 for e in ряд if с["block"] <= (e.get("block") or 0) <= с["block"] + окно)
    с["свопов"] = {f"s{k}": sum(1 for e in ряд if e.get("block") == с["block"] + k and e.get("action") in ("buy", "sell"))
                   for k in (1, 2)}
    return с


р_адрес = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def адреса_файла(путь: str) -> set:
    """Адреса из --dop-adresa: список, или словарь с ключом «адреса», или словарь адрес -> что угодно.
    Пустой разбор -- отказ сразу: молчаливый ноль здесь стоит целого прогона (02.10: 73 млн строк впустую)."""
    д = json.loads(Path(путь).read_text(encoding="utf-8"))
    канд = д if isinstance(д, list) else (д.get("адреса") if isinstance(д.get("адреса"), list) else list(д))
    из_ = {x for x in канд if isinstance(x, str) and р_адрес.match(x)}
    if not из_:
        raise SystemExit(f"STOP: в {путь} не нашлось ни одного адреса (ключи: {list(д)[:5] if isinstance(д, dict) else len(д)})")
    print(f"--dop-adresa {путь}: адресов {len(из_)}", flush=True)
    return из_


ВСЕЛЕННАЯ = 0          # --vselennaya N: сито по всей вселенной архива, порог первых покупок -- N штук


def прогон(день: str, часы: list, porog: float, окно: int, celi: set, метка: str,
           доп: set | None = None, porog_доп: float | None = None, ист: set | None = None,
           минты: set | None = None, не_sol: bool = False, окно_докупки: int | None = None,
           перед_слотов: int = 0) -> Path:
    import requests  # noqa: PLC0415
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"], check=True)
    import zstandard  # noqa: PLC0415
    адр = {} if БЕЗ_АДРЕСОВ else dict(json.loads((КОРЕНЬ / "data" / "podbivka" / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"])
    доп = доп or set()
    ист = ист or set()

    def вселенная_учесть(стр: str, блок, трейдеры: set, sn) -> None:
        """Сито вселенной: первые покупки ЛЮБОГО кошелька от porog SOL в котировке WSOL.

        Вынесено из тела цикла, чтобы учёт вселенной НЕ отменял сбор сигналов: раньше
        здесь стояли `continue`, и суточный файл с --vselennaya выходил без модели.
        """
        ам_в = р_action.search(стр)
        if not (ам_в and ам_в.group(1) == "buy" and блок):
            return
        qм_в = р_qmint.search(стр)
        if not (qм_в and qм_в.group(1) == WSOL):
            return
        км_в = р_qam.search(стр)
        try:
            кв_в = float(км_в.group(1)) if км_в else 0.0
        except ValueError:
            кв_в = 0.0
        кто_в = sorted(трейдеры)[0] if трейдеры else (sn.group(1) if sn else None)
        if доп and кто_в in доп:
            # корзины размера ВСЕХ покупок -- признак «много мелких + редкие крупные»
            к_в = ("<0.1" if кв_в < 0.1 else "0.1-0.3" if кв_в < 0.3 else
                   "0.3-1" if кв_в < 1 else "1-2" if кв_в < 2 else ">=2")
            кз = вс_корзины.setdefault(кто_в, {})
            кз[к_в] = кз.get(к_в, 0) + 1
        if кв_в < porog:
            return
        мм_в = р_mint.search(стр)
        if not (мм_в and кто_в):
            return
        кл_в = (кто_в, мм_в.group(1))
        пред_в = вс_посл.get(кл_в)
        if пред_в is None or блок - пред_в > (окно_докупки or 1800):
            вс_счёт[кто_в] = вс_счёт.get(кто_в, 0) + 1
        вс_посл[кл_в] = блок
        if len(вс_посл) > 1_500_000:        # память: дальше окна правило не смотрит
            гр_в = блок - (окно_докупки or 1800)
            for к_ in [k for k, v in вс_посл.items() if v < гр_в]:
                del вс_посл[к_]

    # «кто покупал тот же пул за 1…N слотов до нас»: кольцо последних покупок ВСЕХ пулов.
    # Дёшево: предфильтр и так достаёт poolId, block и трейдеров из строки; держим только N слотов.
    недавние: collections.deque = collections.deque()
    # сито вселенной (--vselennaya): первые покупки ЛЮБОГО кошелька от porog SOL (котировка WSOL),
    # правило окна докупки -- то же. Модель не считается, события не пишутся.
    вс_счёт: dict = {}
    вс_посл: dict = {}
    вс_корзины: dict = {}      # только для адресов --dop-adresa: корзины размера всех покупок
    for a in доп | ист:
        адр.setdefault(a, {"группы": ["доп"]})
    наши_события, сигналы, цели_события, покупки_ист = [], [], [], []
    активные: dict = {}          # poolId -> до_слота
    ряды: dict = {}              # poolId -> [события]
    сиг_пула: dict = {}          # poolId -> [сигналы этого пула, ещё не посчитанные]
    держать: set = set()         # poolId, чьи ряды нужны до конца прогона (цели, --istochniki)

    def закрыть_пул(pid_: str) -> None:
        """Окно пула кончилось: посчитать модель его сигналов и отпустить ряд. Иначе ряды всех сигналов
        суток живут до конца прогона -- на 997 адресах это десятки гигабайт (прогон 01.10 убит ядром, код -9)."""
        сп = сиг_пула.pop(pid_, None)
        ряд_ = ряды.get(pid_) or []
        if сп:
            for с_ in сп:
                посчитать(с_, ряд_, окно)
        if pid_ not in держать:
            # оставляем ОДНО последнее событие: ряд пула и прежде прерывался между окнами, и у следующего сигнала
            # предыдущим событием было ровно это (pump-amm берёт из него f источника) -- счёт не меняется
            ряды[pid_] = [ряд_[-1]] if ряд_ else ряды.pop(pid_, None) or []
    счёт = {"строк": 0, "файлов": 0, "ошибки": []}
    история_цены: dict = {}      # poolId -> deque[(слот, цена)] за НАЗАД слотов
    ноги: dict = {}              # (подпись, трейдер, минт) -> токенов куплено в транзакции (все ноги)
    # правило Code-1 (bloom_detector.py, слово владельца 27.09): докупка -- тот же источник и тот же минт в пределах
    # окна слотов от его предыдущей покупки; позже -- новый сигнал. Для --istochniki при --okno-dokupki.
    последняя_пок: dict = {}     # (трейдер, минт) -> слот последней покупки
    цена_sol: dict = {}          # минт -> SOL за единицу (последнее событие SOL-пула этого минта)
    минты = минты or set()
    ленты_минтов: dict = {}      # минт -> все события (--minty)
    счёт_ист = 0
    for ч in часы:
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        прочитано = 0            # строк часа уже обработано: после обрыва связи час качается заново, они пропускаются
        for попытка in range(4):
            try:
                with открыть_час(requests, url, ч) as о:
                    if о.status_code != 200:
                        счёт["ошибки"].append(f"{ч}: http {о.status_code}")
                        break
                    if попытка == 0:
                        счёт["файлов"] += 1
                    n_стр = 0
                    for стр in io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(о.raw), encoding="utf-8",
                                                errors="replace"):
                        n_стр += 1
                        if n_стр <= прочитано:
                            continue
                        прочитано = n_стр
                        счёт["строк"] += 1
                        пм = р_pool.search(стр)
                        pid = пм.group(1) if пм else None
                        бм = р_block.search(стр)
                        блок = int(бм.group(1)) if бм else None
                        цена_до = None
                        if pid and блок:
                            поля = dict(р_q.findall(стр))
                            x_ = поля.get("vQuoteInBondingCurve") or поля.get("quoteInPool")
                            y_ = поля.get("vTokensInBondingCurve") or поля.get("tokensInPool")
                            try:
                                цена = float(x_) / float(y_) if x_ and y_ and float(y_) > 0 else None
                            except ValueError:
                                цена = None
                            qм = р_qmint.search(стр) if (цена and не_sol) else None
                            if qм and qм.group(1) == WSOL:
                                мм_ = р_mint.search(стр)
                                if мм_:
                                    цена_sol[мм_.group(1)] = цена
                            if цена:
                                дк = история_цены.setdefault(pid, collections.deque())
                                рост = None
                                if дк:
                                    пр_ц = дк[-1][1]
                                    мин750 = min(c for b, c in дк)
                                    мин150 = min((c for b, c in дк if b >= блок - 150), default=пр_ц)
                                    рост = (round((пр_ц / мин750 - 1) * 100, 2), round((пр_ц / мин150 - 1) * 100, 2))
                                дк.append((блок, цена))
                                while дк and дк[0][0] < блок - НАЗАД:
                                    дк.popleft()
                                цена_до = рост
                            else:
                                цена_до = None
                            счёт_ист += 1
                            if счёт_ист % 2_000_000 == 0:
                                for k in [k for k, v in история_цены.items() if not v or v[-1][0] < блок - НАЗАД]:
                                    del история_цены[k]
                        в_активе = pid in активные
                        sn = р_signer.search(стр)
                        трейдеры = set(р_trader.findall(стр))
                        if sn:
                            трейдеры.add(sn.group(1))
                        наши = [t for t in трейдеры if t in адр]
                        только_сигнал: set = set()
                        if ПОДПИСАНТ and sn and sn.group(1) == ПОДПИСАНТ:
                            доп_п = [t for t in трейдеры if t != ПОДПИСАНТ and t not in адр]
                            только_сигнал = set(доп_п or ([ПОДПИСАНТ] if not наши else []))
                            наши = наши + sorted(только_сигнал)
                        сг = р_sig.search(стр)
                        цель = bool(сг and сг.group(1) in celi)
                        if ВСЕЛЕННАЯ:
                            # Сито вселенной больше НЕ отменяет сбор сигналов. Прежде этот
                            # блок кончался `continue`, а запись файла -- ранним `return`, и
                            # с --vselennaya суточный файл выходил вообще без `сигналы`:
                            # прогон глубины 10.10 был заказан с этим флагом и дал бы сито
                            # вместо модели. Теперь учёт вселенной -- отдельная функция без
                            # continue, и строка идёт дальше по обычному пути.
                            вселенная_учесть(стр, блок, трейдеры, sn)
                        if перед_слотов and pid and блок:
                            ам = р_action.search(стр)
                            if ам and ам.group(1) == "buy":
                                км = р_qam.search(стр)
                                try:
                                    кв_п = float(км.group(1)) if км else 0.0
                                except ValueError:
                                    кв_п = 0.0
                                qм_п = р_qmint.search(стр)
                                кто_п = sorted(трейдеры)[0] if трейдеры else (sn.group(1) if sn else None)
                                недавние.append((блок, pid, кто_п, кв_п, qм_п.group(1) if qм_п else None))
                            while недавние and недавние[0][0] < блок - перед_слотов:
                                недавние.popleft()
                        мм = р_mint.search(стр) if минты else None
                        по_минту = bool(мм and мм.group(1) in минты)
                        if not (в_активе or наши or цель or по_минту):
                            continue
                        try:
                            e_full = json.loads(стр)
                        except ValueError:
                            continue
                        e = {k: e_full.get(k) for k in КЛЮЧИ}
                        _бд = [b.get("trader") for b in e_full.get("breakdown") or [] if isinstance(b, dict) and b.get("trader")]
                        e["трейдер"] = _бд[0] if _бд else e.get("txSigner")      # кошелёк -- по трейдеру, не по плательщику
                        if по_минту:
                            ленты_минтов.setdefault(мм.group(1), []).append(
                                {**e, "трейдеры": [b.get("trader") for b in e_full.get("breakdown") or [] if isinstance(b, dict)],
                                 "рост_до_750": цена_до[0] if цена_до else None})
                        if цель:
                            цели_события.append({**e, "breakdown": e_full.get("breakdown")})
                            if pid:
                                держать.add(pid)
                                if pid not in активные:          # ряд пула цели -- тоже в окне
                                    ряды.setdefault(pid, []).append(e)
                                    активные[pid] = (e.get("block") or 0) + окно
                                    в_активе = False
                        if в_активе:
                            if (e.get("block") or 0) > активные[pid]:
                                активные.pop(pid, None)
                                закрыть_пул(pid)
                            else:
                                ряды[pid].append(e)
                        if not наши or e.get("action") not in ("buy", "sell"):
                            continue
                        по_трейдеру = {b.get("trader"): b for b in e_full.get("breakdown") or [] if isinstance(b, dict)}
                        for t in наши:
                            b = по_трейдеру.get(t) or {}
                            ток = float(b.get("tokenAmount") or e.get("tokenAmount") or 0)
                            кв = float(b.get("quoteAmount") or e.get("quoteAmount") or 0)
                            пост = ((e_full.get("postBalances") or {}).get(t) or {}).get(e.get("mint"))
                            # первая покупка: баланс после = сумме купленного этим трейдером этого минта во всех
                            # ногах транзакции (покупка одной транзакцией в двух пулах -- тоже первая)
                            ключ_ноги = (e["signature"], t, e.get("mint"))
                            if e["action"] == "buy":
                                ноги[ключ_ноги] = ноги.get(ключ_ноги, 0.0) + ток
                            всего_в_tx = ноги.get(ключ_ноги, ток)
                            первая = (пост is not None and ток > 0 and abs(float(пост) - всего_в_tx) <= 0.01 * всего_в_tx)
                            if len(ноги) > 20000:
                                ноги.clear()
                            q = e.get("quoteMint")
                            курс_q = None
                            if q == WSOL:
                                sol = кв
                            elif q in USD and курс((e.get("timestamp") or 0) / 1000):
                                курс_q = 1.0 / курс((e.get("timestamp") or 0) / 1000)
                                sol = кв * курс_q
                            elif не_sol and цена_sol.get(q):
                                курс_q = цена_sol[q]
                                sol = кв * курс_q
                            else:
                                sol = None
                            if t not in только_сигнал and not БЕЗ_СОБЫТИЙ:
                                наши_события.append({"trader": t, "signature": e["signature"], "action": e["action"],
                                                     "pool": e.get("pool"), "poolId": pid, "mint": e.get("mint"),
                                                     "quoteMint": q, "quote": кв, "tokens": ток, "sol_экв": sol,
                                                     "первая": первая, "block": e.get("block"),
                                                     "timestamp": e.get("timestamp"), "priorityFee": e.get("priorityFee"),
                                                     "рост_до_750": цена_до[0] if цена_до else None,
                                                     "рост_до_150": цена_до[1] if цена_до else None,
                                                     "курс_q": курс_q, **резерв(e, курс_q)})
                            if t in ист and e["action"] == "buy" and q == WSOL and pid:
                                покупки_ист.append({"trader": t, "signature": e["signature"], "poolId": pid, "pool": e.get("pool"),
                                                    "mint": e.get("mint"), "block": e.get("block"), "timestamp": e.get("timestamp"),
                                                    "sol": sol, "первая": первая,
                                                    "рост_до_750": цена_до[0] if цена_до else None,
                                                    "рост_до_150": цена_до[1] if цена_до else None, **резерв(e, 1.0)})
                                держать.add(pid)
                                if pid not in активные:
                                    ряды.setdefault(pid, [])
                                    if not ряды[pid] or ряды[pid][-1]["signature"] != e["signature"]:
                                        ряды[pid].append(e)
                                активные[pid] = max(активные.get(pid, 0), (e.get("block") or 0) + окно)
                            порог_t = porog_доп if (t in ист and porog_доп is not None) else porog
                            по_окну = None
                            if окно_докупки and (t in ист or ОКНО_ВСЕМ) and e["action"] == "buy":
                                кл = (t, e.get("mint"))
                                пред = последняя_пок.get(кл)
                                по_окну = пред is None or (e.get("block") or 0) - пред > окно_докупки
                                последняя_пок[кл] = e.get("block") or 0
                                if len(последняя_пок) > 800_000:      # память: дальше окна правило не смотрит
                                    порог_сл = (e.get("block") or 0) - окно_докупки
                                    for кл_ in [k for k, v in последняя_пок.items() if v < порог_сл]:
                                        del последняя_пок[кл_]
                            новый = по_окну if по_окну is not None else первая
                            if (e["action"] == "buy" and новый and sol is not None and sol >= порог_t and (q == WSOL or (не_sol and курс_q))
                                    and (e.get("pool") in XYK or e.get("pool") in КРИВЫЕ) and pid):
                                перед_нами = None
                                if перед_слотов:
                                    s0_ = e.get("block") or 0
                                    перед_нами = [{"кто": к_, "слотов": s0_ - бл_,
                                                   "sol": round(кв_, 6) if qm_ == WSOL else None,
                                                   "quote": None if qm_ == WSOL else round(кв_, 6),
                                                   "quoteMint": qm_}
                                                  for бл_, pid_, к_, кв_, qm_ in недавние
                                                  if pid_ == pid and 1 <= s0_ - бл_ <= перед_слотов][:30]
                                с_нов = {"trader": t, "signature": e["signature"], "pool": e.get("pool"),
                                                "перед": перед_нами,
                                                "подписант": ПОДПИСАНТ if t in только_сигнал else None,
                                                "первая": первая, "по_окну": по_окну,
                                                "poolId": pid, "mint": e.get("mint"), "block": e.get("block"),
                                                "sol": sol, "timestamp": e.get("timestamp"),
                                                "quoteMint": q, "курс_q": курс_q,
                                                "рост_до_750": цена_до[0] if цена_до else None,
                                                "рост_до_150": цена_до[1] if цена_до else None, **резерв(e, курс_q)}
                                сигналы.append(с_нов)
                                сиг_пула.setdefault(pid, []).append(с_нов)
                                if pid not in активные:
                                    ряды.setdefault(pid, [])
                                    if not ряды[pid] or ряды[pid][-1]["signature"] != e["signature"]:
                                        ряды[pid].append(e)
                                активные[pid] = max(активные.get(pid, 0), (e.get("block") or 0) + окно)
                break
            except Exception as exc:  # noqa: BLE001
                if os.environ.get("PODB_ARHIV_KESH"):          # битый час на диске -- прочь, следующая попытка скачает заново
                    (Path(os.environ["PODB_ARHIV_KESH"]) / f"{ч}.jsonl.zst").unlink(missing_ok=True)
                if попытка == 3:
                    счёт["ошибки"].append(f"{ч}: {type(exc).__name__}: {str(exc)[:100]}")
                else:
                    счёт.setdefault("докачки", []).append(f"{ч}: после {прочитано} строк: {type(exc).__name__}")
                    time.sleep(5 * (попытка + 1))
        print(f"{ч}: строк всего {счёт['строк']}, наших событий {len(наши_события)}, сигналов {len(сигналы)}, "
              f"активных пулов {len(активные)}", flush=True)
    for pid_ in list(сиг_пула):
        закрыть_пул(pid_)
    # покупки --istochniki: кто купил следом (≥ 0.5 SOL, другие кошельки) в +15 с, в его слоте и в +3 слота
    for п in покупки_ист:
        ряд = ряды.get(п["poolId"]) or []
        i0 = next((i for i, e in enumerate(ряд) if e["signature"] == п["signature"]), None)
        if i0 is None:
            п["следом"] = None
            continue
        следом = []
        for e in ряд[i0 + 1:]:
            if (e.get("timestamp") or 0) > (п["timestamp"] or 0) + 15000:
                break
            if e.get("action") == "buy" and e.get("трейдер") != п["trader"] and e.get("quoteMint") == WSOL:
                следом.append({"кто": e.get("трейдер"), "q": float(e.get("quoteAmount") or 0), "slot": e.get("block"),
                               "мс": (e.get("timestamp") or 0) - (п["timestamp"] or 0)})
        п["следом"] = следом
        п["следом_05_15с"] = sum(1 for x in следом if x["q"] >= 0.5)
        п["удержание"] = удержание(п, ряд)
        # продажи в его слоте после его покупки (порядок файла внутри слота -- по timestamp, оценка)
        прод_сл = [e for e in ряд[i0 + 1:] if e.get("block") == п["block"] and e.get("action") == "sell"
                   and e.get("трейдер") != п["trader"]]
        п["продаж_в_слоте"] = len(прод_сл)
        п["продажи_в_слоте_sol"] = round(sum(float(e.get("quoteAmount") or 0) for e in прод_сл), 4)
    ряды_целей = {e["poolId"]: ряды.get(e["poolId"]) for e in цели_события if e.get("poolId")}
    import gzip  # noqa: PLC0415
    out = КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / f"{метка}.json.gz"   # > 100 МБ несжатым -- предел GitHub
    out.parent.mkdir(parents=True, exist_ok=True)
    вс_доп: dict = {}
    if ВСЕЛЕННАЯ:
        отобр = {k: v for k, v in вс_счёт.items() if v >= ВСЕЛЕННАЯ}
        вс_доп = {"вселенная_мин": ВСЕЛЕННАЯ, "кошельков_всего": len(вс_счёт),
                  "кошельков_отобрано": len(отобр), "вселенная": отобр,
                  "корзины": вс_корзины}
    out.write_bytes(gzip.compress(json.dumps({"день": день, "часы": часы, "порог_sol": porog, "окно_слотов": окно, "перед_слотов": перед_слотов, "счёт": счёт,
                               "наши_события": наши_события, "сигналы": сигналы, "цели": цели_события,
                               "покупки_ист": покупки_ист, "ленты_минтов": ленты_минтов,
                               "ряды_целей": ряды_целей, **вс_доп},
                              ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6))
    return out


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", required=True, help="начало: YYYY-MM-DDTHH (UTC, час начала файла)")
    р.add_argument("--chasov", type=int, default=24)
    р.add_argument("--porog", type=float, default=2.0)
    р.add_argument("--okno", type=int, default=160)
    р.add_argument("--celi", default="", help="json со списком подписей-целей")
    р.add_argument("--metka", required=True)
    р.add_argument("--dop-adresa", default="", help="json: список доп. адресов (события и сигналы)")
    р.add_argument("--ne-sol", action="store_true", help="сигналы и в пулах с котировкой не SOL (курс -- по SOL-пулам архива)")
    р.add_argument("--minty", default="", help="через запятую: минты, все события которых пишутся целиком")
    р.add_argument("--istochniki", default="", help="через запятую: адреса с отдельным порогом сигнала --porog-dop")
    р.add_argument("--porog-dop", type=float, default=None, help="порог сигнала для --istochniki, SOL")
    р.add_argument("--vselennaya", type=int, default=0,
                   help="сито по всей вселенной архива: писать кошельки с не меньше N первых покупок от --porog "
                        "(модель и события не считаются)")
    р.add_argument("--pered", type=int, default=0,
                   help="писать в сигнал, кто покупал тот же пул за 1…N слотов до нас (0 -- не писать)")
    р.add_argument("--bilety", default="0.3,0.5", help="билеты модели, SOL, через запятую (pyg9: 0.3,0.5,1,3)")
    р.add_argument("--okno-vsem", action="store_true",
                   help="правило Code-1 (--okno-dokupki) для ВСЕХ наших адресов, порог -- --porog (не только --istochniki)")
    р.add_argument("--bez-adresov", action="store_true", help="только цели (--celi) и ряды их пулов, без наших адресов")
    р.add_argument("--bez-sobytij", action="store_true", help="не писать наши_события (нужны только сигналы с моделью)")
    р.add_argument("--podpisant", default="", help="адрес первого подписанта (сервер бота): покупки с этим txSigner -- сигналы для кошельков breakdown")
    р.add_argument("--okno-dokupki", type=int, default=None,
                   help="для --istochniki сигнал по правилу Code-1: нет покупки того же минта в предыдущие N слотов (1800)")
    а = р.parse_args()
    global БИЛЕТЫ, ОКНО_ВСЕМ, БЕЗ_АДРЕСОВ, ПОДПИСАНТ, БЕЗ_СОБЫТИЙ  # noqa: PLW0603
    БИЛЕТЫ = tuple(float(x) for x in а.bilety.split(",") if x.strip())
    ОКНО_ВСЕМ = а.okno_vsem
    БЕЗ_АДРЕСОВ = а.bez_adresov
    ПОДПИСАНТ = а.podpisant
    БЕЗ_СОБЫТИЙ = а.bez_sobytij
    global ВСЕЛЕННАЯ                 # noqa: PLW0603
    ВСЕЛЕННАЯ = а.vselennaya
    t0 = calendar.timegm(time.strptime(а.s, "%Y-%m-%dT%H"))
    часы = [time.strftime("%Y/%m/%d/%H", time.gmtime(t0 + 3600 * k)) for k in range(а.chasov + 1)]  # +1 час хвоста окна
    celi = set(json.loads(Path(а.celi).read_text(encoding="utf-8"))) if а.celi else set()
    доп = адреса_файла(а.dop_adresa) if а.dop_adresa else set()
    ист = {x for x in а.istochniki.split(",") if x}
    out = прогон(а.s, часы, а.porog, а.okno, celi, а.metka, доп, а.porog_dop, ист,
                 {x for x in а.minty.split(",") if x}, а.ne_sol, а.okno_dokupki, а.pered)
    import podbivka_run as R  # noqa: PLC0415
    R.записано(out)
    R.пуш(f"Podbivka-2: arhiv den {а.metka} [automated]", [str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
