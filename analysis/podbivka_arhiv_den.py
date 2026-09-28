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
  * цели (--celi, подписи): их события пишутся целиком -- для сверок.
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
ВЫХОДЫ = (6, 12, 24, 36, 72, 150)
БИЛЕТЫ = (0.3, 0.5)
ИЗДЕРЖКИ = 0.002
КЛЮЧИ = ("signature", "action", "pool", "poolId", "mint", "quoteMint", "txSigner", "tokenAmount", "quoteAmount",
         "tokensInPool", "quoteInPool", "vTokensInBondingCurve", "vQuoteInBondingCurve", "poolFeeRate",
         "priorityFee", "block", "timestamp")

р_sig = re.compile(r'"signature":\s*"([1-9A-HJ-NP-Za-km-z]{64,90})"')
р_pool = re.compile(r'"poolId":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_signer = re.compile(r'"txSigner":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
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
    """Ряд -- события пула в порядке файла, первым -- событие сигнала (или раньше)."""
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
        for a in БИЛЕТЫ:
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
                res[f"{имя}|{a}|{H}"] = round((out - a - ИЗДЕРЖКИ) / a * 100, 3)
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
    продавцы = [{"кто": e.get("txSigner"), "q": e.get("quoteAmount"), "t": e.get("tokenAmount"), "slot": e.get("block")}
                for e in после if e.get("action") == "sell"]
    вверх_пп, вниз_пп = (пик / p0 - 1) * 100, (дно / пик - 1) * 100
    return {"p0": p0, "пик_слот": ep.get("block"), "вверх_пп": round(вверх_пп, 2), "от_пика_пп": round(вниз_пп, 2),
            "паттерн": вверх_пп >= 20 and вниз_пп <= -25, "продавцы_после_пика": продавцы,
            "событий_до_30": len(пос)}


def прогон(день: str, часы: list, porog: float, окно: int, celi: set, метка: str,
           доп: set | None = None, porog_доп: float | None = None) -> Path:
    import requests  # noqa: PLC0415
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"], check=True)
    import zstandard  # noqa: PLC0415
    адр = dict(json.loads((КОРЕНЬ / "data" / "podbivka" / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"])
    доп = доп or set()
    for a in доп:
        адр.setdefault(a, {"группы": ["доп"]})
    наши_события, сигналы, цели_события = [], [], []
    активные: dict = {}          # poolId -> до_слота
    ряды: dict = {}              # poolId -> [события]
    счёт = {"строк": 0, "файлов": 0, "ошибки": []}
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
                    в_активе = pid in активные
                    sn = р_signer.search(стр)
                    трейдеры = set(р_trader.findall(стр))
                    if sn:
                        трейдеры.add(sn.group(1))
                    наши = [t for t in трейдеры if t in адр]
                    сг = р_sig.search(стр)
                    цель = bool(сг and сг.group(1) in celi)
                    if not (в_активе or наши or цель):
                        continue
                    try:
                        e_full = json.loads(стр)
                    except ValueError:
                        continue
                    e = {k: e_full.get(k) for k in КЛЮЧИ}
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
                        первая = (пост is not None and ток > 0 and abs(float(пост) - ток) <= 0.01 * ток)
                        q = e.get("quoteMint")
                        sol = кв if q == WSOL else (кв / курс((e.get("timestamp") or 0) / 1000) if q in USD and курс((e.get("timestamp") or 0) / 1000) else None)
                        наши_события.append({"trader": t, "signature": e["signature"], "action": e["action"],
                                             "pool": e.get("pool"), "poolId": pid, "mint": e.get("mint"),
                                             "quoteMint": q, "quote": кв, "tokens": ток, "sol_экв": sol,
                                             "первая": первая, "block": e.get("block"), "timestamp": e.get("timestamp"),
                                             "priorityFee": e.get("priorityFee")})
                        порог_t = porog_доп if (t in доп and porog_доп is not None) else porog
                        if (e["action"] == "buy" and первая and sol is not None and sol >= порог_t and q == WSOL
                                and (e.get("pool") in XYK or e.get("pool") in КРИВЫЕ) and pid):
                            сигналы.append({"trader": t, "signature": e["signature"], "pool": e.get("pool"),
                                            "poolId": pid, "mint": e.get("mint"), "block": e.get("block"),
                                            "sol": sol, "timestamp": e.get("timestamp")})
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
        ряд = ряды.get(с["poolId"]) or []
        с["событий_пула_в_окне"] = sum(1 for e in ряд if с["block"] <= (e.get("block") or 0) <= с["block"] + окно)
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
    р.add_argument("--porog-dop", type=float, default=None, help="порог сигнала для доп. адресов, SOL")
    а = р.parse_args()
    t0 = calendar.timegm(time.strptime(а.s, "%Y-%m-%dT%H"))
    часы = [time.strftime("%Y/%m/%d/%H", time.gmtime(t0 + 3600 * k)) for k in range(а.chasov + 1)]  # +1 час хвоста окна
    celi = set(json.loads(Path(а.celi).read_text(encoding="utf-8"))) if а.celi else set()
    доп = set(json.loads(Path(а.dop_adresa).read_text(encoding="utf-8"))) if а.dop_adresa else set()
    out = прогон(а.s, часы, а.porog, а.okno, celi, а.metka, доп, а.porog_dop)
    import podbivka_run as R  # noqa: PLC0415
    R.записано(out)
    R.пуш(f"Podbivka-2: arhiv den {а.metka} [automated]", [str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
