#!/usr/bin/env python3
"""Пункт 6: почему зависали продажи. По каждому зависшему токену --
есть ли пул в паре с SOL/USDC с ликвидностью > 1 SOL, или ликвидность
живёт только в паре с другим токеном; Token-2022 и комиссия за перевод.

Только чтение: DexScreener (публичный) + getAccountInfo по минту."""
from __future__ import annotations

import json, os, sys, time
from pathlib import Path
import requests

REPO = Path(__file__).resolve().parent.parent
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
WSOL = "So11111111111111111111111111111111111111112"
QUOTE_OK = {"SOL", "WSOL", "USDC", "USDT"}
OUT = REPO / "data" / "solana_hung_liquidity.json"


def rpc(method, params, key):
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    for a in range(4):
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=40)
        except Exception:  # noqa: BLE001
            time.sleep(2 * (a + 1)); continue
        if r.status_code == 429 or 500 <= r.status_code < 600:
            time.sleep(2 * (a + 1)); continue
        if not r.ok:
            return None
        b = r.json()
        return None if "error" in b else b.get("result")
    return None


def mint_info(mint, key):
    res = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}], key)
    val = (res or {}).get("value") or {}
    info = ((val.get("data") or {}).get("parsed") or {}).get("info", {})
    fee_bps = None
    for e in info.get("extensions") or []:
        if isinstance(e, dict) and e.get("extension") == "transferFeeConfig":
            fee_bps = ((e.get("state") or {}).get("newerTransferFee") or {}).get("transferFeeBasisPoints")
    return {"token2022": val.get("owner") == TOKEN_2022, "transfer_fee_bps": fee_bps,
            "decimals": info.get("decimals")}


def pairs(mint):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"ошибка": f"{type(exc).__name__}"}
    if r.status_code != 200:
        return {"ошибка": f"http={r.status_code}"}
    try:
        b = r.json()
    except ValueError:
        return {"ошибка": "не JSON"}
    out = []
    for p in (b.get("pairs") or []):
        out.append({"dex": p.get("dexId"),
                     "quote": ((p.get("quoteToken") or {}).get("symbol") or "").upper(),
                     "liq_usd": ((p.get("liquidity") or {}).get("usd")) or 0.0,
                     "vol24_usd": ((p.get("volume") or {}).get("h24")) or 0.0})
    out.sort(key=lambda x: -x["liq_usd"])
    return {"пары": out}


def sol_usd():
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{WSOL}", timeout=30)
        ps = [p for p in (r.json().get("pairs") or [])
              if ((p.get("quoteToken") or {}).get("symbol") or "").upper() in ("USDC", "USDT")]
        ps.sort(key=lambda p: -(((p.get("liquidity") or {}).get("usd")) or 0))
        return float(ps[0]["priceUsd"]) if ps else None
    except Exception:  # noqa: BLE001
        return None


def main():
    key = os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or ""
    if not key:
        raise SystemExit("нет HELIUS_API_KEY/HELIUS_API")
    trades = json.loads((REPO / "data" / "solana_trades_all.json").read_text())
    hung = [t for t in trades if t.get("is_hung")
            or (isinstance(t.get("held_seconds"), (int, float)) and t["held_seconds"] > 300)]
    seen, rows = set(), []
    px = sol_usd()
    print(f"[probe] курс SOL = {px} USD; зависших сделок {len(hung)}", flush=True)
    thr_usd = (px or 150.0) * 1.0     # порог "больше 1 SOL"
    for t in hung:
        m = t.get("mint")
        if not m or m in seen:
            continue
        seen.add(m)
        pr = pairs(m)
        mi = mint_info(m, key)
        good = [p for p in (pr.get("пары") or []) if p["quote"] in QUOTE_OK and p["liq_usd"] > thr_usd]
        best_any = (pr.get("пары") or [{}])[0] if pr.get("пары") else {}
        rows.append({
            "mint": m, "task": t.get("task_name"), "sol_in": t.get("sol_in"),
            "held_seconds": t.get("held_seconds"),
            "token2022": mi["token2022"], "transfer_fee_bps": mi["transfer_fee_bps"],
            "пар_всего": len(pr.get("пары") or []),
            "лучший_пул": {k: best_any.get(k) for k in ("dex", "quote", "liq_usd")} if best_any else None,
            "пулов_с_SOL_USDC_свыше_1SOL": len(good),
            "лучший_SOL_USDC_пул_usd": max([p["liq_usd"] for p in good], default=0.0),
            "ликвидность_только_в_чужой_паре": bool(pr.get("пары")) and not good,
            "ошибка": pr.get("ошибка"),
        })
        print(f"[probe] {m[:12]}… t2022={mi['token2022']} fee={mi['transfer_fee_bps']} "
              f"пар={len(pr.get('пары') or [])} годных={len(good)} лучший={best_any.get('quote')}"
              f"/{best_any.get('liq_usd')}", flush=True)
    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "sol_usd": px, "порог_usd": thr_usd, "строк": len(rows), "токены": rows}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    n_only_foreign = sum(1 for r in rows if r["ликвидность_только_в_чужой_паре"])
    n_t22 = sum(1 for r in rows if r["token2022"])
    print()
    print("=== ИТОГ ===")
    print(f"токенов: {len(rows)}")
    print(f"ликвидность ТОЛЬКО в паре с чужим токеном (не SOL/USDC): {n_only_foreign}")
    print(f"Token-2022: {n_t22}; из них с комиссией за перевод: "
          f"{sum(1 for r in rows if r['transfer_fee_bps'])}")
    print(f"есть пул SOL/USDC глубже 1 SOL: {sum(1 for r in rows if r['пулов_с_SOL_USDC_свыше_1SOL'])}")


if __name__ == "__main__":
    main()
