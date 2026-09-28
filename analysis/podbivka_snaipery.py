#!/usr/bin/env python3
"""Подбивка: снайперы (кошельки, опередившие нас в сделке) -- сбор по цепи.
Только чтение. Сводка -- podbivka_snaipery_svod.py (офлайн).

По кошельку за последние --chasov часов:
  * все его сделки с токенами: покупки и продажи (рост / убыль минта у кошелька;
    WSOL / USDC / USDT -- котировка, не токен);
  * SOL-экв. сделки: изменение лампортов кошелька + WSOL + стейблы по курсу,
    без комиссии сети и без чаевых (они отдельно);
  * комиссия (meta.fee), приоритет = fee − 5000 × подписей; чаевые (оценка) --
    ВНЕШНИЕ (не вложенные) переводы SOL системной программой от кошелька на чужой
    адрес, кроме своего WSOL-счёта; адреса получателей пишутся;
  * индекс сделки в блоке (getBlock, transactionDetails "signatures");
  * источники покупки: кто купил тот же минт в слотах s0−3 .. s0 ДО снайпера
    (подписи минта до его сделки), их слот и индекс в блоке.
Узел -- по времени сделки (Shyft / Helius, правило подбивки).
Выход: data/podbivka/snaipery/<кошелёк>.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
КОТИРОВКИ = {C.WSOL, C.USDC, C.USDT}
SYSTEM = "11111111111111111111111111111111"
НАЗАД_СЛОТОВ = 3


def курс_usd(bt) -> float | None:
    import podbivka_tablicy as T  # noqa: PLC0415
    return T.курс_детектора(bt)


def чаевые(tx: dict, кош: str) -> list:
    """Внешние системные переводы SOL от кошелька на чужой адрес (оценка чаевых)."""
    свои = {r["account"] for r in C.token_rows(tx).values() if r["owner"] == кош}
    из_ = []
    for ix in ((((tx or {}).get("transaction") or {}).get("message") or {}).get("instructions") or []):
        п = (ix or {}).get("parsed") if isinstance(ix, dict) else None
        if not isinstance(п, dict) or ix.get("programId") != SYSTEM or п.get("type") != "transfer":
            continue
        инф = п.get("info") or {}
        if инф.get("source") == кош and инф.get("destination") not in свои and инф.get("destination") != кош:
            из_.append({"куда": инф.get("destination"), "lamports": int(инф.get("lamports") or 0)})
    return из_


def разбор(tx: dict, кош: str) -> list:
    """Сделки кошелька в tx: [{mint, сторона, токенов, dec, sol_экв, ...}]."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err") is not None:
        return []
    ряды = C.token_rows(tx).values()
    дельта: dict = {}
    dec: dict = {}
    for r in ряды:
        if r["owner"] == кош:
            дельта[r["mint"]] = дельта.get(r["mint"], 0) + (r["post"] - r["pre"])
            dec[r["mint"]] = r.get("dec")
    токены = {m: d for m, d in дельта.items() if m not in КОТИРОВКИ and d != 0}
    if not токены:
        return []
    ld = C.lamport_delta(tx, кош) or 0
    подп = ((tx.get("transaction") or {}).get("signatures") or [])
    плательщик = (C.account_keys(tx) or [None])[0] == кош
    fee = int(meta.get("fee") or 0) if плательщик else 0
    ч = чаевые(tx, кош)
    чл = sum(x["lamports"] for x in ч)
    # SOL-экв. сделки без комиссии сети и чаевых; знак: + пришло кошельку
    sol = D(ld + fee + чл) / D(10**9)
    sol += D(дельта.get(C.WSOL, 0)) / D(10**9)
    usd = D(дельта.get(C.USDC, 0) + дельта.get(C.USDT, 0)) / D(10**6)
    курс = курс_usd(tx.get("blockTime")) if usd != 0 else None
    из_ = []
    for m, d in токены.items():
        из_.append({"mint": m, "сторона": "buy" if d > 0 else "sell", "токенов": abs(d), "dec": dec.get(m),
                    "sol": float(sol), "usd": float(usd), "курс": курс,
                    "sol_экв": float(sol + (usd / D(str(курс)) if курс and usd else 0)),
                    "usd_без_курса": bool(usd and not курс),
                    "многоминтовая": len(токены) > 1})
    for x in из_:
        x.update(fee=int(meta.get("fee") or 0), приоритет=int(meta.get("fee") or 0) - 5000 * len(подп),
                 чаевые=ч, чаевые_lamports=чл, плательщик=плательщик)
    return из_


class Блоки:
    def __init__(self, уз):
        self.уз, self.кэш = уз, {}

    def индекс(self, слот: int, подпись: str):
        if слот not in self.кэш:
            try:
                б = self.уз.вызов("getBlock", [слот, {"transactionDetails": "signatures", "rewards": False,
                                                      "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
                                  срок=40.0)
                self.кэш[слот] = (б or {}).get("signatures") or []
            except RuntimeError:
                self.кэш[слот] = None
        с = self.кэш[слот]
        if not с:
            return None, None
        try:
            return с.index(подпись), len(с)
        except ValueError:
            return None, len(с)


def источники(уз, бл: Блоки, кош: str, mint: str, подпись: str, s0: int, i0) -> list:
    """Кто купил mint в слотах s0−3 .. s0 до снайпера."""
    try:
        стр = уз.подписи(mint, до=подпись, limit=200)
    except RuntimeError as exc:
        return [{"why_not": S.чисто(str(exc))[:120]}]
    канд = [з for з in стр if з.get("err") is None and s0 - НАЗАД_СЛОТОВ <= (з.get("slot") or 0) <= s0]
    if not канд:
        return []
    txs = уз.пакет([з["signature"] for з in канд])
    из_ = []
    for з in канд:
        т = txs.get(з["signature"])
        for w in C.mint_buyers(т, mint) if т else {}:
            if w == кош:
                continue
            слот = з.get("slot")
            инд, _ = бл.индекс(слот, з["signature"])
            if слот == s0 and инд is not None and i0 is not None and инд > i0:
                continue          # после снайпера в том же слоте -- не источник
            из_.append({"адрес": w, "подпись": з["signature"], "слот": слот, "индекс": инд,
                        "слотов_до": s0 - слот,
                        "позиций_до": (i0 - инд) if (слот == s0 and инд is not None and i0 is not None) else None})
    return из_


def кошелёк(уз, кош: str, часов: float) -> dict:
    до_ts = time.time() - часов * 3600
    подписи, до = [], None
    while True:
        with уз.на("shyft" if часов <= 48 else "helius"):
            стр = уз.подписи(кош, до=до, limit=1000)
        if not стр:
            break
        for з in стр:
            if (з.get("blockTime") or 0) >= до_ts:
                подписи.append(з)
        if len(стр) < 1000 or (стр[-1].get("blockTime") or 0) < до_ts:
            break
        до = стр[-1]["signature"]
    подписи = [з for з in подписи if з.get("err") is None]
    подписи.reverse()
    бл = Блоки(уз)
    сделки = []
    for и in range(0, len(подписи), 100):
        кусок = подписи[и:и + 100]
        txs = уз.пакет([з["signature"] for з in кусок], {з["signature"]: з.get("blockTime") for з in кусок})
        for з in кусок:
            т = txs.get(з["signature"])
            if not т:
                continue
            for с in разбор(т, кош):
                with уз.на(S.узел_по_времени(з.get("blockTime"))):
                    инд, всего = бл.индекс(т["slot"], з["signature"])
                    с.update(signature=з["signature"], slot=т["slot"], blockTime=т.get("blockTime"),
                             индекс=инд, в_блоке=всего)
                    if с["сторона"] == "buy":
                        пул = C.identify_pool(т, кош, с["mint"])
                        try:
                            import c2_pool_programs as PP  # noqa: PLC0415
                            с["программа"] = PP.pool_program(т, пул.get("pool_vault"), PP.labels()).get("pool_program") \
                                if пул.get("pool_vault") else None
                        except Exception:  # noqa: BLE001
                            с["программа"] = None
                        с["котировка_пула"] = пул.get("quote_mint")
                        с["источники"] = источники(уз, бл, кош, с["mint"], з["signature"], т["slot"], инд)
                сделки.append(с)
        бл.кэш = {k: v for k, v in list(бл.кэш.items())[-200:]}
    return {"кошелёк": кош, "часов": часов, "с_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(до_ts)),
            "подписей": len(подписи), "сделок": len(сделки), "сделки": сделки, "расход": уз.расход()}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--koshelki", required=True, help="через запятую")
    р.add_argument("--chasov", type=float, default=24)
    р.add_argument("--push", action="store_true")
    а = р.parse_args()
    import podbivka_run as R  # noqa: PLC0415
    уз = S.Узел()
    out_dir = КОРЕНЬ / "data" / "podbivka" / "snaipery"
    out_dir.mkdir(parents=True, exist_ok=True)
    for кош in а.koshelki.split(","):
        рез = кошелёк(уз, кош.strip(), а.chasov)
        p = out_dir / f"{кош.strip()}_{int(а.chasov)}ч.json"
        p.write_text(json.dumps(рез, ensure_ascii=False, default=str), encoding="utf-8")
        R.записано(p)
        print(f"{кош[:8]}: подписей {рез['подписей']}, сделок {рез['сделок']}")
        if а.push:
            R.пуш(f"Podbivka-2: snaipery {кош[:8]} {int(а.chasov)}h [automated]", [str(p)])
    print(json.dumps(уз.расход(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
