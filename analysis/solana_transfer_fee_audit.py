#!/usr/bin/env python3
"""Комиссия на перевод (Token-2022) и маршруты через промежуточные токены.

Что установлено на живой сделке SANTA кошелька TEST1: цена в пуле выросла,
а сделка дала -13.4%, потому что с КАЖДОГО перевода маршрута удерживалось
3%. Шесть переводов -- 0.97^6 = -16.7%. Здесь это проверяется по всем
сделкам, а не на одном примере.

Части:
  1. Минты: программа токена, расширения, ставка комиссии на перевод.
     Читается getAccountInfo самого минта -- ставка берётся из цепочки,
     а не из головы.
  2. Разложение пар (SANTA у TEST1, PURPLE у BATCH-6): суммарная комиссия
     на перевод по всем ногам маршрута и число таксируемых переводов
     каждого токена.
  3. Свод по закрытым сделкам: группа "маршрут через таксируемый
     промежуточный токен", список промежуточных токенов с числом сделок и
     суммой по цепи, доля таких маршрутов по источникам, пилот отдельно.
  4. Вывод: кого касается сильнее и сколько SOL ушло на комиссии.

Удержанное считается по балансам: у токена с комиссией на перевод сумма
изменений балансов всех счетов НЕ сходится в ноль -- недостача и есть
удержанное. Число переводов считается по разобранным инструкциям.

Только чтение цепочки.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT_PATH = REPO_ROOT / "data" / "solana_transfer_fee_audit.json"
CACHE_PATH = REPO_ROOT / "data" / "solana_transfer_fee_audit_cache.json"
TRADES_PATH = REPO_ROOT / "data" / "solana_trades_all.json"

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
BASE = (WSOL, USDC, USDT)

TOKEN_CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

PILOT_TASK = "pointfarmcap"
SANTA = "3c7mmVSyEH8jfZXgxvpLsETtko1Y16DyRJ5XYB4snhGt"
PURPLE = "MYQZzuiiRyy38hY8X7KQN8XyE67y2GYWs63cqjDjgLs"
TEST1 = "E1qAJBmrJDhBvm2sV8kfMXFAmgzHuSRNRosPEgMkKiWS"
BATCH6 = "DE5yR9S8qBkh8n4rYVvc6iGDF3rqrEPZX2aHBsDWJ79n"


# ---------------------------------------------------------------- минты

def mint_info(rpc, mint: str) -> dict:
    """Программа токена, расширения и ставка комиссии на перевод -- из цепочки."""
    out = {"минт": mint}
    try:
        r = rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    except RuntimeError as exc:
        out["почему"] = f"getAccountInfo не отдался: {str(exc)[:120]}"
        return out
    val = (r or {}).get("value") or {}
    owner = val.get("owner")
    out["программа_токена_id"] = owner
    out["программа_токена"] = ("SPL Token (классический)" if owner == TOKEN_CLASSIC else
                                "Token-2022" if owner == TOKEN_2022 else
                                f"другая ({owner})" if owner else "не определена")
    info = (((val.get("data") or {}).get("parsed") or {}).get("info") or {})
    out["десятичных"] = info.get("decimals")
    exts = info.get("extensions") or []
    out["расширения"] = [e.get("extension") for e in exts if isinstance(e, dict)]
    out["ставка_комиссии_bps"] = None
    for e in exts:
        if not isinstance(e, dict) or e.get("extension") != "transferFeeConfig":
            continue
        st = e.get("state") or {}
        newer = st.get("newerTransferFee") or {}
        older = st.get("olderTransferFee") or {}
        out["ставка_комиссии_bps"] = newer.get("transferFeeBasisPoints")
        out["ставка_комиссии_прежняя_bps"] = older.get("transferFeeBasisPoints")
        out["потолок_комиссии"] = newer.get("maximumFee")
        out["эпоха_ставки"] = newer.get("epoch")
    out["таксируемый"] = bool(out.get("ставка_комиссии_bps"))
    if out["ставка_комиссии_bps"] is None and out["программа_токена_id"] == TOKEN_2022:
        out["оговорка"] = "Token-2022 без расширения комиссии на перевод"
    return out


# --------------------------------------------------------------- маршрут

def _bal(entries) -> list:
    out = []
    for b in entries or []:
        out.append((b.get("accountIndex"), b.get("owner"), b.get("mint"),
                     float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)))
    return out


def deltas_by_mint(tx: dict) -> dict:
    """Сумма изменений балансов по каждому минту и карта счёт->минт.

    У обычного токена сумма равна нулю. У токена с комиссией на перевод
    она ОТРИЦАТЕЛЬНА ровно на удержанное: комиссия оседает отдельным
    полем счёта и в балансе не видна.
    """
    meta = (tx or {}).get("meta") or {}
    pre = {(i, m): a for i, o, m, a in _bal(meta.get("preTokenBalances"))}
    post = {(i, m): a for i, o, m, a in _bal(meta.get("postTokenBalances"))}
    acc_mint = {}
    for i, o, m, a in _bal(meta.get("preTokenBalances")) + _bal(meta.get("postTokenBalances")):
        acc_mint[i] = m
    сумма = defaultdict(float)
    for k in set(pre) | set(post):
        i, m = k
        сумма[m] += post.get(k, 0.0) - pre.get(k, 0.0)
    return dict(сумма), acc_mint


def transfers_by_mint(tx: dict, acc_mint: dict) -> dict:
    """Сколько переводов каждого минта было в транзакции."""
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    keys_raw = msg.get("accountKeys") or []
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys_raw]
    by_addr = {keys[i]: m for i, m in acc_mint.items() if isinstance(i, int) and i < len(keys)}
    groups = [msg.get("instructions") or []]
    for g in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        groups.append(g.get("instructions") or [])
    counts = defaultdict(int)
    for instrs in groups:
        for ins in instrs:
            parsed = ins.get("parsed")
            if not isinstance(parsed, dict):
                continue
            if parsed.get("type") not in ("transfer", "transferChecked", "transferCheckedWithFee"):
                continue
            info = parsed.get("info") or {}
            m = info.get("mint")
            if not m:
                m = by_addr.get(info.get("source")) or by_addr.get(info.get("destination"))
            if m:
                counts[m] += 1
    return dict(counts)


def _native_delta(tx: dict, owner: str) -> float:
    """Изменение нативного баланса счёта, без комиссии сети у подписанта."""
    meta = (tx or {}).get("meta") or {}
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    raw = msg.get("accountKeys") or []
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in raw]
    if owner not in keys:
        return 0.0
    i = keys.index(owner)
    pb, po = meta.get("preBalances") or [], meta.get("postBalances") or []
    if i >= len(pb) or i >= len(po):
        return 0.0
    d = (po[i] - pb[i]) / 1e9
    if i == 0:
        d += (meta.get("fee") or 0) / 1e9
    return d


def rate_in_sol(tx: dict, mint: str) -> float | None:
    """Курс минта в SOL ПО ЭТОЙ ЖЕ транзакции. Нет ноги -- честно None.

    Две ветки, и вторая обязательна: у половины сделок кошелёк платит
    НАТИВНЫМ SOL, а не WSOL-токеном, и поиск только по токеновым балансам
    оставлял без курса 182 сделки из 367.
    """
    if mint == WSOL:
        return 1.0
    meta = (tx or {}).get("meta") or {}
    pre = {(o, m): a for i, o, m, a in _bal(meta.get("preTokenBalances"))}
    post = {(o, m): a for i, o, m, a in _bal(meta.get("postTokenBalances"))}
    owners = {o for (o, m) in set(pre) | set(post)}
    for o in owners:
        dm = post.get((o, mint), 0.0) - pre.get((o, mint), 0.0)
        dw = post.get((o, WSOL), 0.0) - pre.get((o, WSOL), 0.0)
        if dm and dw and (dm > 0) != (dw > 0):
            return abs(dw) / abs(dm)
    # Нативная ветка: тот, у кого минт и SOL двигаются навстречу.
    best = None
    for o in owners:
        dm = post.get((o, mint), 0.0) - pre.get((o, mint), 0.0)
        if not dm:
            continue
        dn = _native_delta(tx, o)
        if dn and (dm > 0) != (dn > 0) and (best is None or abs(dm) > best[0]):
            best = (abs(dm), abs(dn) / abs(dm))
    return best[1] if best else None


def route_of(tx: dict, target: str) -> dict:
    """Маршрут одной транзакции: какие токены прошли, сколько переводов
    каждого, сколько удержано и во что это обошлось в SOL."""
    сумма, acc_mint = deltas_by_mint(tx)
    переводов = transfers_by_mint(tx, acc_mint)
    токены = {}
    промежуточные = []
    удержано_sol = 0.0
    без_курса = []
    for m, d in сумма.items():
        уд = -d if d < 0 else 0.0
        курс = rate_in_sol(tx, m)
        строка = {"переводов": переводов.get(m, 0), "недостача": round(-d, 10) if d < 0 else 0.0,
                   "курс_в_sol": курс,
                   "удержано_sol": (уд * курс) if (уд and курс) else (0.0 if not уд else None)}
        if уд and not курс:
            без_курса.append(m)
        токены[m] = строка
        if строка["удержано_sol"]:
            удержано_sol += строка["удержано_sol"]
        if m != target and m not in BASE:
            промежуточные.append(m)
    return {"токены": токены, "промежуточные": sorted(set(промежуточные)),
             # Сумма ДО фильтра по таксируемости -- сюда попадает и WSOL,
             # у которого баланс не сходится из-за обёртки, а не из-за
             # комиссии. Как результат её брать нельзя, и она так названа.
             "недостача_всех_минтов_sol_до_фильтра": round(удержано_sol, 9),
             "минты_без_курса": без_курса}


# ------------------------------------------------------------ самопроверка

def self_test() -> None:
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    M, I = "MINT", "INTER"

    def B(idx, owner, mint, amt):
        return {"accountIndex": idx, "owner": owner, "mint": mint,
                "uiTokenAmount": {"uiAmount": amt}}

    def TR(mint, src, dst):
        return {"parsed": {"type": "transferChecked",
                            "info": {"mint": mint, "source": src, "destination": dst}},
                 "programId": TOKEN_2022}

    tx = {"meta": {
        "preTokenBalances": [B(1, "POOL", M, 1000.0), B(2, "US", M, 0.0),
                              B(3, "POOL2", I, 500.0), B(4, "US", I, 0.0),
                              B(5, "POOL2", WSOL, 10.0)],
        "postTokenBalances": [B(1, "POOL", M, 900.0), B(2, "US", M, 97.0),
                               B(3, "POOL2", I, 400.0), B(4, "US", I, 97.0),
                               B(5, "POOL2", WSOL, 11.0)],
        "innerInstructions": [{"instructions": [TR(M, "a", "b"), TR(I, "c", "d"), TR(I, "d", "e")]}]},
        "transaction": {"message": {"accountKeys": [], "instructions": []}}}
    r = route_of(tx, M)
    chk("недостача целевого токена = комиссия перевода",
        abs(r["токены"][M]["недостача"] - 3.0) < 1e-9, str(r["токены"][M]))
    chk("переводы считаются поимённо", r["токены"][I]["переводов"] == 2,
        str(r["токены"][I]["переводов"]))
    chk("промежуточный токен опознан", r["промежуточные"] == [I], str(r["промежуточные"]))
    chk("WSOL промежуточным не считается", WSOL not in r["промежуточные"])
    chk("курс промежуточного взят из этой же транзакции",
        abs((r["токены"][I]["курс_в_sol"] or 0) - 0.01) < 1e-9, str(r["токены"][I]["курс_в_sol"]))
    chk("удержанное переведено в SOL",
        abs(r["токены"][I]["удержано_sol"] - 3.0 * 0.01) < 1e-9, str(r["токены"][I]))
    chk("минт без недостачи ничего не стоит", r["токены"][WSOL]["недостача"] == 0.0)

    # Обычный токен: сумма сходится в ноль, удержанного нет
    tx2 = {"meta": {
        "preTokenBalances": [B(1, "POOL", M, 1000.0), B(2, "US", M, 0.0)],
        "postTokenBalances": [B(1, "POOL", M, 900.0), B(2, "US", M, 100.0)],
        "innerInstructions": []},
        "transaction": {"message": {"accountKeys": [], "instructions": []}}}
    r2 = route_of(tx2, M)
    chk("у обычного токена удержанного нет", r2["токены"][M]["недостача"] == 0.0)
    chk("и в SOL ноль, а не пусто", r2["недостача_всех_минтов_sol_до_фильтра"] == 0.0)

    # Нет курса -- честное None, а не ноль
    tx3 = {"meta": {
        "preTokenBalances": [B(1, "POOL", M, 1000.0), B(2, "US", M, 0.0)],
        "postTokenBalances": [B(1, "POOL", M, 900.0), B(2, "US", M, 97.0)],
        "innerInstructions": []},
        "transaction": {"message": {"accountKeys": [], "instructions": []}}}
    r3 = route_of(tx3, M)
    chk("удержано есть, а курса нет -- None, не ноль",
        r3["токены"][M]["удержано_sol"] is None, str(r3["токены"][M]))
    chk("и минт назван в списке без курса", r3["минты_без_курса"] == [M])

    # Разбор минта
    fake = {"value": {"owner": TOKEN_2022, "data": {"parsed": {"info": {
        "decimals": 6, "extensions": [
            {"extension": "transferFeeConfig", "state": {
                "newerTransferFee": {"transferFeeBasisPoints": 300, "maximumFee": 18446744073709551615,
                                      "epoch": 900},
                "olderTransferFee": {"transferFeeBasisPoints": 300}}}]}}}}}

    class FakeRpc:
        def call(self, *a, **k):
            return fake

    mi = mint_info(FakeRpc(), "X")
    chk("программа токена опознана", mi["программа_токена"] == "Token-2022", mi["программа_токена"])
    chk("ставка комиссии прочитана", mi["ставка_комиссии_bps"] == 300)
    chk("и токен помечен таксируемым", mi["таксируемый"] is True)
    chk("расширения перечислены", mi["расширения"] == ["transferFeeConfig"])

    class DeadRpc:
        def call(self, *a, **k):
            raise RuntimeError("узел молчит")

    chk("минт не прочитался -- честная причина, а не пустая ставка",
        "не отдался" in mint_info(DeadRpc(), "X").get("почему", ""))

    # Курс по нативному SOL: кошелёк платит не WSOL-токеном, а прямо SOL.
    tx4 = {"meta": {"preTokenBalances": [B(1, "US", M, 0.0)],
                     "postTokenBalances": [B(1, "US", M, 100.0)],
                     "preBalances": [3_000_000_000], "postBalances": [2_000_000_000], "fee": 0},
            "transaction": {"message": {"accountKeys": [{"pubkey": "US"}], "instructions": []}}}
    chk("курс берётся и по нативному SOL", abs((rate_in_sol(tx4, M) or 0) - 0.01) < 1e-12,
        str(rate_in_sol(tx4, M)))

    bad = 0
    for n, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка учёта комиссии на перевод: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


# ------------------------------------------------------------------ прогон

CACHE_VERSION = 2   # курс минта теперь ищется и по нативному SOL


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            d = json.loads(CACHE_PATH.read_text())
        except (ValueError, OSError):
            return {}
        if d.get("_версия") == CACHE_VERSION:
            return d
        print(f"[кэш] версия {d.get('_версия')} устарела -- маршруты пересчитываются заново")
    return {"_версия": CACHE_VERSION}


def save_cache(cache: dict) -> None:
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False))
    tmp.replace(CACHE_PATH)


def closed_trades() -> list:
    t = json.loads(TRADES_PATH.read_text())
    return [x for x in t if x.get("buy_signature") and x.get("sell_signature")
            and x.get("bot") != "tradewiz"]


def find_pair_trades(rpc, wallet: str, mint: str, limit: int = 300) -> list:
    """Сделки кошелька по минту -- по его же подписям, а не по учёту:
    свежие сделки в учёт попадают с задержкой."""
    try:
        sigs = rpc.call("getSignaturesForAddress", [wallet, {"limit": limit}]) or []
    except RuntimeError as exc:
        return [{"почему": f"getSignaturesForAddress не отдался: {str(exc)[:120]}"}]
    ok = [s["signature"] for s in sigs if not s.get("err")]
    out = []
    for i in range(0, len(ok), 50):
        txs = rpc.transactions(ok[i:i + 50])
        for sig, tx in txs.items():
            if not tx:
                continue
            сумма, _ = deltas_by_mint(tx)
            if mint in сумма:
                out.append({"подпись": sig, "слот": tx.get("slot"),
                             "время_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                          time.gmtime(tx["blockTime"]))
                                            if tx.get("blockTime") else None),
                             "маршрут": route_of(tx, mint)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--min-interval-s", type=float, default=0.05)
    ap.add_argument("--limit", type=int, default=0, help="ограничить число сделок (отладка)")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return

    from solana_crowd_scan import Rpc, helius_key  # noqa: PLC0415
    key, _ = helius_key()
    rpc = Rpc(key, min_interval_s=args.min_interval_s, workers=1, service="разбор_пилота")

    rep = {"собрано_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ЧЕСТНЫЕ_ОГОВОРКИ": [
                "Ставка комиссии на перевод читается из самого минта в цепочке, "
                "а не берётся из наблюдаемой недостачи.",
                "Удержанное считается по балансам: у токена с комиссией сумма изменений "
                "всех счетов отрицательна ровно на удержанное.",
                "Перевод удержанного в SOL идёт по курсу ИЗ ТОЙ ЖЕ транзакции. Нет ноги "
                "с SOL -- курс не подставляется, минт попадает в список без курса.",
                "Только чтение цепочки.",
            ]}

    # 2. Разбор двух пар по реальным сделкам кошельков.
    пары = {}
    for имя, wallet, mint in (("SANTA у TEST1", TEST1, SANTA), ("PURPLE у BATCH-6", BATCH6, PURPLE)):
        пары[имя] = {"кошелёк": wallet, "минт": mint,
                      "сделки": find_pair_trades(rpc, wallet, mint)}
        print(f"[пары] {имя}: найдено {len(пары[имя]['сделки'])} транзакций")
    rep["пары"] = пары

    # 3. Свод по закрытым сделкам.
    trades = closed_trades()
    if args.limit:
        trades = trades[:args.limit]
    cache = load_cache()
    sigs = []
    cache.setdefault("_версия", CACHE_VERSION)
    for t in trades:
        for s in (t["buy_signature"], t["sell_signature"]):
            if s not in cache:
                sigs.append(s)
    print(f"[свод] закрытых сделок {len(trades)}, транзакций к добору {len(sigs)}")
    for i in range(0, len(sigs), 50):
        txs = rpc.transactions(sigs[i:i + 50])
        for sig, tx in txs.items():
            cache[sig] = {"нет": True} if not tx else {"маршрут": route_of(tx, "")}
        if i % 200 == 0:
            save_cache(cache)
            print(f"  {min(i + 50, len(sigs))}/{len(sigs)}")
    save_cache(cache)

    # Сначала узнаём, КАКИЕ минты вообще таксируемые. Без этого недостача
    # баланса приписывалась комиссии даже у классических SPL-токенов, где
    # комиссии на перевод не бывает: у них баланс не сходится по другой
    # причине -- закрытие временных счетов, чеканка, сжигание.
    все_минты = set()
    for t in trades:
        for s_ in (t["buy_signature"], t["sell_signature"]):
            r = (cache.get(s_) or {}).get("маршрут") if isinstance(cache.get(s_), dict) else None
            if r:
                все_минты |= set(r["токены"])
    все_минты -= set(BASE)
    минты = {}
    print(f"[минты] читаю {len(все_минты)} минтов")
    for i, m in enumerate(sorted(все_минты)):
        минты[m] = mint_info(rpc, m)
        if i % 100 == 0:
            print(f"  {i}/{len(все_минты)}")
    for m in (SANTA, PURPLE):
        if m not in минты:
            минты[m] = mint_info(rpc, m)
    for d in пары.values():
        for сд in d["сделки"]:
            for m in ((сд.get("маршрут") or {}).get("токены") or {}):
                if m not in минты and m not in BASE:
                    минты[m] = mint_info(rpc, m)
    таксируемые = {m for m, v in минты.items() if v.get("таксируемый")}
    rep["минты"] = минты
    rep["таксируемых_минтов"] = len(таксируемые)

    промеж = defaultdict(lambda: {"сделок": 0, "сумма_по_цепи_sol": 0.0, "удержано_sol": 0.0,
                                   "переводов": 0, "без_курса": 0})
    по_задачам = defaultdict(lambda: {"сделок": 0, "через_таксируемый_промежуточный": 0,
                                       "удержано_sol": 0.0, "сумма_по_цепи_sol": 0.0,
                                       "без_курса": 0, "sol_вложено": 0.0})
    всего = {"сделок": 0, "через_таксируемый_промежуточный": 0, "удержано_sol": 0.0,
              "без_курса": 0, "переводов_таксируемых": 0,
              "сделок_с_таксируемой_целью": 0}
    строки = []
    for t in trades:
        rb = (cache.get(t["buy_signature"]) or {}).get("маршрут")
        rs = (cache.get(t["sell_signature"]) or {}).get("маршрут")
        if not rb or not rs:
            continue
        mint = t["mint"]
        уд = 0.0
        нет_курса = False
        по_минтам = defaultdict(lambda: {"переводов": 0, "удержано": 0.0, "удержано_sol": 0.0})
        for r in (rb, rs):
            for m, v in r["токены"].items():
                if m not in таксируемые:
                    continue
                по_минтам[m]["переводов"] += v["переводов"]
                по_минтам[m]["удержано"] += v["недостача"]
                if v["недостача"]:
                    if v["удержано_sol"] is None:
                        нет_курса = True
                    else:
                        по_минтам[m]["удержано_sol"] += v["удержано_sol"]
                        уд += v["удержано_sol"]
        межд = sorted(m for m in по_минтам if m != mint)
        цепь = (t.get("sol_in") or 0.0) + (t.get("sol_out") or 0.0)
        задача = t.get("task_name") or "без задачи"
        g = по_задачам[задача]
        g["сделок"] += 1
        g["удержано_sol"] += уд
        g["без_курса"] += int(нет_курса)
        g["sol_вложено"] += (t.get("sol_in") or 0.0)
        всего["сделок"] += 1
        всего["удержано_sol"] += уд
        всего["без_курса"] += int(нет_курса)
        всего["сделок_с_таксируемой_целью"] += int(mint in таксируемые)
        всего["переводов_таксируемых"] += sum(v["переводов"] for v in по_минтам.values())
        if межд:
            g["через_таксируемый_промежуточный"] += 1
            g["сумма_по_цепи_sol"] += цепь
            всего["через_таксируемый_промежуточный"] += 1
            for m in межд:
                промеж[m]["сделок"] += 1
                промеж[m]["сумма_по_цепи_sol"] += цепь
                промеж[m]["удержано_sol"] += по_минтам[m]["удержано_sol"]
                промеж[m]["переводов"] += по_минтам[m]["переводов"]
        строки.append({"задача": задача, "минт": mint,
                        "цель_таксируемая": mint in таксируемые,
                        "промежуточные_таксируемые": межд,
                        "переводов_по_минтам": {m: v["переводов"] for m, v in по_минтам.items()},
                        "удержано_sol": round(уд, 9), "sol_in": t.get("sol_in"),
                        "gross_pct": t.get("gross_pct")})

    rep["свод"] = {
        "закрытых_сделок": всего["сделок"],
        "сделок_где_сам_токен_таксируемый": всего["сделок_с_таксируемой_целью"],
        "сделок_через_таксируемый_промежуточный": всего["через_таксируемый_промежуточный"],
        "доля_через_таксируемый_промежуточный": (
            round(всего["через_таксируемый_промежуточный"] / всего["сделок"], 4)
            if всего["сделок"] else None),
        "таксируемых_переводов_всего": всего["переводов_таксируемых"],
        "удержано_на_переводах_всего_sol": round(всего["удержано_sol"], 6),
        "сделок_где_курс_не_нашёлся": всего["без_курса"],
        "оговорка_к_сумме": ("комиссия считается ТОЛЬКО по минтам, у которых в цепочке "
                              "есть расширение комиссии на перевод; у остальных недостача "
                              "баланса вызвана закрытием временных счетов, чеканкой или "
                              "сжиганием и комиссией не является"),
    }
    rep["промежуточные_токены"] = {
        m: {**v, "сумма_по_цепи_sol": round(v["сумма_по_цепи_sol"], 6),
            "удержано_sol": round(v["удержано_sol"], 6),
            "ставка_комиссии_bps": (минты.get(m) or {}).get("ставка_комиссии_bps"),
            "программа_токена": (минты.get(m) or {}).get("программа_токена")}
        for m, v in sorted(промеж.items(), key=lambda kv: -kv[1]["сделок"])}
    rep["по_задачам"] = {
        k: {**v, "удержано_sol": round(v["удержано_sol"], 6),
            "сумма_по_цепи_sol": round(v["сумма_по_цепи_sol"], 6),
            "sol_вложено": round(v["sol_вложено"], 6),
            "доля_через_таксируемый": (round(v["через_таксируемый_промежуточный"] / v["сделок"], 4)
                                        if v["сделок"] else None),
            "удержано_к_вложенному": (round(v["удержано_sol"] / v["sol_вложено"], 4)
                                       if v["sol_вложено"] else None)}
        for k, v in sorted(по_задачам.items(), key=lambda kv: -kv[1]["сделок"])}
    # Разложение двух пар: комиссия ТОЛЬКО по таксируемым минтам, с числом
    # переводов каждого. Это и есть ответ на "суммарная комиссия по всем
    # ногам маршрута".
    for имя, d in пары.items():
        итог = {"комиссия_всего_sol": 0.0, "по_токенам": {}, "переводов_всего": 0}
        for сд in d["сделки"]:
            for m, v in ((сд.get("маршрут") or {}).get("токены") or {}).items():
                if m not in таксируемые:
                    continue
                g = итог["по_токенам"].setdefault(m, {
                    "переводов": 0, "удержано_токена": 0.0, "удержано_sol": 0.0,
                    "ставка_комиссии_bps": (минты.get(m) or {}).get("ставка_комиссии_bps")})
                g["переводов"] += v["переводов"]
                g["удержано_токена"] += v["недостача"]
                if v["удержано_sol"]:
                    g["удержано_sol"] += v["удержано_sol"]
                    итог["комиссия_всего_sol"] += v["удержано_sol"]
                итог["переводов_всего"] += v["переводов"]
        итог["комиссия_всего_sol"] = round(итог["комиссия_всего_sol"], 9)
        d["комиссия_на_перевод"] = итог
    rep["пилот_отдельно"] = rep["по_задачам"].get(PILOT_TASK, {"почему": "сделок пилота нет"})
    rep["строки"] = строки

    OUT_PATH.write_text(json.dumps(rep, ensure_ascii=False, indent=2))
    краткое = {k: v for k, v in rep.items() if k not in ("строки", "минты")}
    краткое["пары"] = {k: {"кошелёк": v["кошелёк"], "минт": v["минт"],
                            "транзакций": len(v["сделки"]),
                            "комиссия_на_перевод": v.get("комиссия_на_перевод")}
                        for k, v in пары.items()}
    print(json.dumps(краткое, ensure_ascii=False, indent=2)[:7000])
    print(f"\nвыгрузка -> {OUT_PATH}")


if __name__ == "__main__":
    main()
