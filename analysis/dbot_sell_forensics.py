#!/usr/bin/env python3
"""Разбор ОДНОЙ продажи по цепочке: по какой цене реально исполнилось,
какой была цена до сделки, сколько съела цена влияния, и существовал ли
более глубокий маршрут.

Только чтение. Нужен, чтобы ответить на вопрос владельца "интерфейс
показывает в 10 раз больше, а продали за 4 доллара -- каким образом".
Ответ считается из данных транзакции и соседних блоков, а не из общих
соображений."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_SEC: list[str] = []


def scrub(t: str) -> str:
    for s in _SEC:
        if s:
            t = t.replace(s, "[REDACTED]")
    return t


def log(m: str) -> None:
    print(f"[forensics] {scrub(str(m))}", flush=True)


def env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            _SEC.append(v)
            return v
    raise RuntimeError("нет ключа: " + ", ".join(names))


def rpc(method: str, params: list, key: str, url: str | None = None):
    u = url or f"https://mainnet.helius-rpc.com/?api-key={key}"
    for a in range(5):
        try:
            r = requests.post(u, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=60)
        except Exception as exc:  # noqa: BLE001
            log(f"rpc {method} {a+1}/5: {type(exc).__name__}")
            time.sleep(2 * (a + 1)); continue
        if r.status_code == 429 or 500 <= r.status_code < 600:
            time.sleep(2 * (a + 1)); continue
        if not r.ok:
            log(f"rpc {method} http={r.status_code}: {scrub(r.text[:200])}"); return None
        b = r.json()
        if "error" in b:
            log(f"rpc {method}: {scrub(str(b['error'])[:200])}"); return None
        return b.get("result")
    return None


def token_deltas(meta: dict) -> dict:
    """(owner, mint) -> (pre, post) в ui-единицах."""
    pre, post = {}, {}
    for b in meta.get("preTokenBalances") or []:
        pre[(b.get("owner"), b.get("mint"))] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    for b in meta.get("postTokenBalances") or []:
        post[(b.get("owner"), b.get("mint"))] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    out = {}
    for k in set(pre) | set(post):
        out[k] = (pre.get(k, 0.0), post.get(k, 0.0))
    return out


def analyse_tx(sig: str, wallet: str, mint: str, key: str) -> dict:
    tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}], key)
    if not tx:
        return {"ошибка": "транзакция не получена"}
    meta = tx.get("meta") or {}
    d = token_deltas(meta)
    res: dict = {"signature": sig, "slot": tx.get("slot"), "blockTime": tx.get("blockTime"),
                 "err": meta.get("err"), "fee_SOL": (meta.get("fee") or 0) / 1e9}

    # наша нога
    ours_tok = d.get((wallet, mint), (0.0, 0.0))
    res["наш_токен_до_после"] = ours_tok
    res["токенов_отдали"] = round(ours_tok[0] - ours_tok[1], 9)
    keys_raw = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys_raw]
    native = None
    if wallet in keys:
        i = keys.index(wallet)
        pb, qb = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pb) and i < len(qb):
            native = (qb[i] - pb[i]) / 1e9
            if i == 0:
                native += (meta.get("fee") or 0) / 1e9
    res["SOL_получили"] = round(native or 0, 9)

    # пул: тот, у кого токен ПРИБЫЛ, а SOL/WSOL убыл (зеркально нам)
    pools = []
    for (owner, m), (a, b) in d.items():
        if m == mint and owner and owner != wallet and (b - a) > 0:
            sol_leg = d.get((owner, WSOL))
            pools.append({"pool_owner": owner,
                          "токен_до": a, "токен_после": b, "токен_приход": round(b - a, 9),
                          "WSOL_до": sol_leg[0] if sol_leg else None,
                          "WSOL_после": sol_leg[1] if sol_leg else None})
    res["пулы"] = pools

    # цена исполнения
    if res["токенов_отдали"] > 0 and res["SOL_получили"]:
        res["цена_исполнения_SOL_за_токен"] = res["SOL_получили"] / res["токенов_отдали"]

    # глубина пула ДО сделки и теоретическая цена без влияния
    for p in pools:
        if p["WSOL_до"] and p["токен_до"]:
            spot = p["WSOL_до"] / p["токен_до"]
            p["цена_до_сделки_SOL_за_токен"] = spot
            p["резерв_SOL_до"] = p["WSOL_до"]
            p["резерв_токена_до"] = p["токен_до"]
            p["наш_объём_в_долях_резерва"] = (res["токенов_отдали"] / p["токен_до"]) if p["токен_до"] else None
            if res.get("цена_исполнения_SOL_за_токен"):
                p["цена_влияния_%"] = round(
                    (res["цена_исполнения_SOL_за_токен"] / spot - 1) * 100, 3)
            # сколько дал бы пул по формуле x*y=k без комиссий
            x, y = p["токен_до"], p["WSOL_до"]
            dx = res["токенов_отдали"]
            p["выход_по_формуле_xy_k_SOL"] = round(y - (x * y) / (x + dx), 9)
    return res


def recent_prices(mint: str, slot_from: int, slot_to: int, wallet: str, key: str) -> list[dict]:
    """Сделки этого минта в блоках ДО нашей -- рыночная отметка."""
    out = []
    for s in range(slot_from, slot_to + 1):
        blk = rpc("getBlock", [s, {"encoding": "jsonParsed", "transactionDetails": "accounts",
                                    "maxSupportedTransactionVersion": 1, "rewards": False}], key)
        if not blk:
            continue
        for t in blk.get("transactions") or []:
            meta = t.get("meta") or {}
            if meta.get("err") is not None:
                continue
            d = token_deltas(meta)
            movers = [(o, a, b) for (o, m), (a, b) in d.items() if m == mint and abs(b - a) > 0]
            if not movers:
                continue
            best = max(movers, key=lambda r: abs(r[2] - r[1]))
            owner, a, b = best
            sol = d.get((owner, WSOL))
            tok_delta = b - a
            sol_delta = (sol[1] - sol[0]) if sol else None
            if sol_delta and tok_delta:
                out.append({"slot": s, "owner": owner,
                            "токен": round(tok_delta, 6), "SOL": round(sol_delta, 9),
                            "цена_SOL_за_токен": abs(sol_delta / tok_delta)})
    return out


def jupiter_quote(mint: str, amount_raw: int, bps: int) -> dict:
    for name, url in (("lite-api", "https://lite-api.jup.ag/swap/v1/quote"),
                      ("quote-api-v6", "https://quote-api.jup.ag/v6/quote")):
        try:
            r = requests.get(url, params={"inputMint": mint, "outputMint": WSOL,
                                           "amount": str(amount_raw), "slippageBps": str(bps),
                                           "restrictIntermediateTokens": "false"}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            continue
        if r.status_code != 200:
            continue
        try:
            b = r.json()
        except ValueError:
            continue
        routes = [{"label": (rp.get("swapInfo") or {}).get("label"),
                   "in": (rp.get("swapInfo") or {}).get("inAmount"),
                   "out": (rp.get("swapInfo") or {}).get("outAmount")}
                  for rp in (b.get("routePlan") or [])]
        return {"хост": name, "outAmount": b.get("outAmount"),
                "outAmount_SOL": (int(b["outAmount"]) / 1e9) if b.get("outAmount") else None,
                "priceImpactPct": b.get("priceImpactPct"), "маршрут": routes}
    return {"ответа_нет": "оба хоста Jupiter не ответили"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--signature", required=True)
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--mint", required=True)
    ap.add_argument("--decimals", type=int, default=6)
    ap.add_argument("--prior-blocks", type=int, default=8)
    ap.add_argument("--quote-tokens", default="1000,10000,100000,1029680")
    args = ap.parse_args()

    key = env("HELIUS_API_KEY", "HELIUS_API")
    rep: dict = {}
    tx = analyse_tx(args.signature, args.wallet, args.mint, key)
    rep["наша_продажа"] = tx
    log("НАША ПРОДАЖА: " + json.dumps(tx, ensure_ascii=False, default=str))

    if tx.get("slot"):
        prior = recent_prices(args.mint, tx["slot"] - args.prior_blocks, tx["slot"] - 1, args.wallet, key)
        rep["сделки_до_нашей"] = prior
        log(f"сделок этого минта в {args.prior_blocks} блоках до нашей: {len(prior)}")
        for p in prior[-10:]:
            log("  " + json.dumps(p, ensure_ascii=False))

    quotes = []
    for t in [float(x) for x in args.quote_tokens.split(",") if x.strip()]:
        raw = int(t * (10 ** args.decimals))
        q = jupiter_quote(args.mint, raw, 5000)
        q["токенов"] = t
        if q.get("outAmount_SOL") and t:
            q["цена_SOL_за_токен"] = q["outAmount_SOL"] / t
        quotes.append(q)
        log(f"Jupiter на {t:g} токенов: {json.dumps(q, ensure_ascii=False)}")
    rep["котировки_jupiter_сейчас"] = quotes

    print("\n=== РЕЗУЛЬТАТ ===\n" + json.dumps(rep, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
