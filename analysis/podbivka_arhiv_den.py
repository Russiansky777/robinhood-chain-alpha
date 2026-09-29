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
    кривая -- комиссия poolFeeRate сверху при покупке и из выручки при продаже;
  * входы: S0 -- сразу после сделки источника; S0_дно -- после последнего события
    слота s0 (порядок внутри слота -- порядок файла, timestamp мс; оценка);
    S1 -- после последнего события слота s0+1;
  * выход +H -- состояние после последнего события со слотом <= s0+H−1, наша
    покупка вставлена (потолок); билеты 0.3 и 0.5 SOL, минус 0.002 SOL на круг;
  * налог Token-2022 не учтён (флаг).
Выход: data/podbivka/arhiv_den/<метка>.json.
"""
from __future__ import annotations

import argparse
import collections
import calendar
import io
import json
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
ВЫХОДЫ = (6, 12, 24, 36, 72, 108, 150)   # 108 -- удержание живой полосы lane_s0
БИЛЕТЫ = (0.3, 0.5)
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
    ряд = ряд[i0:]
    fee = float(ряд[0].get("poolFeeRate") or 0)
    из_ = {"f": None, "g": None, "fee": fee}
    if пул in XYK:
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
    входы = {"S0": ряд[0], "S0_дно": последнее(s0), "S1": последнее(s0 + 1)}
    res = {}
    for имя, e_вх in входы.items():
        с_вх = ст(e_вх) if e_вх else None
        if not с_вх:
            continue
        x, y = с_вх
        for a_sol in БИЛЕТЫ:
            a = a_sol * масштаб
            if пул in XYK:
                т = y * из_["f"] * a / (x + из_["f"] * a)
                вст_x = из_["f"] * a
            else:
                net = a / (1 + fee)
                т = y * net / (x + net)
                вст_x = net
            if т <= 0:
                continue
            for H in ВЫХОДЫ:
                e_вых = последнее(s0 + H - 1)
                с_вых = ст(e_вых) if e_вых else None
                if not с_вых:
                    continue
                X, Y = с_вых[0] + вст_x, с_вых[1] - т
                if Y <= 0:
                    continue
                if пул in XYK:
                    out = X * из_["g"] * т / (Y + т)
                else:
                    out = X * т / (Y + т) * (1 - fee)
                res[f"{имя}|{a_sol}|{H}"] = round((out - a - ИЗДЕРЖКИ * масштаб) / a * 100, 3)
    из_["пп"] = res
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


def прогон(день: str, часы: list, porog: float, окно: int, celi: set, метка: str,
           доп: set | None = None, porog_доп: float | None = None, ист: set | None = None,
           минты: set | None = None, не_sol: bool = False, окно_докупки: int | None = None) -> Path:
    import requests  # noqa: PLC0415
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"], check=True)
    import zstandard  # noqa: PLC0415
    адр = dict(json.loads((КОРЕНЬ / "data" / "podbivka" / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"])
    доп = доп or set()
    ист = ист or set()
    for a in доп | ист:
        адр.setdefault(a, {"группы": ["доп"]})
    наши_события, сигналы, цели_события, покупки_ист = [], [], [], []
    активные: dict = {}          # poolId -> до_слота
    ряды: dict = {}              # poolId -> [события]
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
        try:
            with requests.get(url, stream=True, timeout=120) as о:
                if о.status_code != 200:
                    счёт["ошибки"].append(f"{ч}: http {о.status_code}")
                    continue
                счёт["файлов"] += 1
                for стр in io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(о.raw), encoding="utf-8",
                                            errors="replace"):
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
                    сг = р_sig.search(стр)
                    цель = bool(сг and сг.group(1) in celi)
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
                        if pid and pid not in активные:          # ряд пула цели -- тоже в окне
                            ряды.setdefault(pid, []).append(e)
                            активные[pid] = (e.get("block") or 0) + окно
                            в_активе = False
                    if в_активе:
                        if (e.get("block") or 0) > активные[pid]:
                            активные.pop(pid, None)
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
                        наши_события.append({"trader": t, "signature": e["signature"], "action": e["action"],
                                             "pool": e.get("pool"), "poolId": pid, "mint": e.get("mint"),
                                             "quoteMint": q, "quote": кв, "tokens": ток, "sol_экв": sol,
                                             "первая": первая, "block": e.get("block"), "timestamp": e.get("timestamp"),
                                             "priorityFee": e.get("priorityFee"),
                                             "рост_до_750": цена_до[0] if цена_до else None,
                                             "рост_до_150": цена_до[1] if цена_до else None})
                        if t in ист and e["action"] == "buy" and q == WSOL and pid:
                            покупки_ист.append({"trader": t, "signature": e["signature"], "poolId": pid, "pool": e.get("pool"),
                                                "mint": e.get("mint"), "block": e.get("block"), "timestamp": e.get("timestamp"),
                                                "sol": sol, "первая": первая,
                                                "рост_до_750": цена_до[0] if цена_до else None,
                                                "рост_до_150": цена_до[1] if цена_до else None})
                            if pid not in активные:
                                ряды.setdefault(pid, [])
                                if not ряды[pid] or ряды[pid][-1]["signature"] != e["signature"]:
                                    ряды[pid].append(e)
                            активные[pid] = max(активные.get(pid, 0), (e.get("block") or 0) + окно)
                        порог_t = porog_доп if (t in ист and porog_доп is not None) else porog
                        по_окну = None
                        if окно_докупки and t in ист and e["action"] == "buy":
                            кл = (t, e.get("mint"))
                            пред = последняя_пок.get(кл)
                            по_окну = пред is None or (e.get("block") or 0) - пред > окно_докупки
                            последняя_пок[кл] = e.get("block") or 0
                        новый = по_окну if по_окну is not None else первая
                        if (e["action"] == "buy" and новый and sol is not None and sol >= порог_t and (q == WSOL or (не_sol and курс_q))
                                and (e.get("pool") in XYK or e.get("pool") in КРИВЫЕ) and pid):
                            сигналы.append({"trader": t, "signature": e["signature"], "pool": e.get("pool"),
                                            "первая": первая, "по_окну": по_окну,
                                            "poolId": pid, "mint": e.get("mint"), "block": e.get("block"),
                                            "sol": sol, "timestamp": e.get("timestamp"),
                                            "quoteMint": q, "курс_q": курс_q,
                                            "рост_до_750": цена_до[0] if цена_до else None,
                                            "рост_до_150": цена_до[1] if цена_до else None})
                            if pid not in активные:
                                ряды.setdefault(pid, [])
                                if not ряды[pid] or ряды[pid][-1]["signature"] != e["signature"]:
                                    ряды[pid].append(e)
                            активные[pid] = max(активные.get(pid, 0), (e.get("block") or 0) + окно)
        except Exception as exc:  # noqa: BLE001
            счёт["ошибки"].append(f"{ч}: {type(exc).__name__}: {str(exc)[:100]}")
        print(f"{ч}: строк всего {счёт['строк']}, наших событий {len(наши_события)}, сигналов {len(сигналы)}, "
              f"активных пулов {len(активные)}", flush=True)
    for с in сигналы:
        с["модель"] = модель(с, ряды.get(с["poolId"]) or [])
        с["рисунок"] = рисунок(с, ряды.get(с["poolId"]) or [])
        с["удержание"] = удержание(с, ряды.get(с["poolId"]) or [])
        ряд = ряды.get(с["poolId"]) or []
        с["событий_пула_в_окне"] = sum(1 for e in ряд if с["block"] <= (e.get("block") or 0) <= с["block"] + окно)
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
    # слоты s0+1 / s0+2: число свопов (для сверки с историей пула)
    for с in сигналы:
        ряд = ряды.get(с["poolId"]) or []
        с["свопов"] = {f"s{k}": sum(1 for e in ряд if e.get("block") == с["block"] + k and e.get("action") in ("buy", "sell"))
                       for k in (1, 2)}
    ряды_целей = {e["poolId"]: ряды.get(e["poolId"]) for e in цели_события if e.get("poolId")}
    import gzip  # noqa: PLC0415
    out = КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / f"{метка}.json.gz"   # > 100 МБ несжатым -- предел GitHub
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(gzip.compress(json.dumps({"день": день, "часы": часы, "порог_sol": porog, "окно_слотов": окно, "счёт": счёт,
                               "наши_события": наши_события, "сигналы": сигналы, "цели": цели_события,
                               "покупки_ист": покупки_ист, "ленты_минтов": ленты_минтов,
                               "ряды_целей": ряды_целей},
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
    р.add_argument("--okno-dokupki", type=int, default=None,
                   help="для --istochniki сигнал по правилу Code-1: нет покупки того же минта в предыдущие N слотов (1800)")
    а = р.parse_args()
    t0 = calendar.timegm(time.strptime(а.s, "%Y-%m-%dT%H"))
    часы = [time.strftime("%Y/%m/%d/%H", time.gmtime(t0 + 3600 * k)) for k in range(а.chasov + 1)]  # +1 час хвоста окна
    celi = set(json.loads(Path(а.celi).read_text(encoding="utf-8"))) if а.celi else set()
    доп = set(json.loads(Path(а.dop_adresa).read_text(encoding="utf-8"))) if а.dop_adresa else set()
    ист = {x for x in а.istochniki.split(",") if x}
    out = прогон(а.s, часы, а.porog, а.okno, celi, а.metka, доп, а.porog_dop, ист,
                 {x for x in а.minty.split(",") if x}, а.ne_sol, а.okno_dokupki)
    import podbivka_run as R  # noqa: PLC0415
    R.записано(out)
    R.пуш(f"Podbivka-2: arhiv den {а.metka} [automated]", [str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
