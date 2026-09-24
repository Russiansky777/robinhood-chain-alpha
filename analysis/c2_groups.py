#!/usr/bin/env python3
"""C2: разбивка сделок задачи A по группам -- тип пула, котировка, капитализация.

Группы:
* тип пула: «кривая/лаунчпад» (Raydium Launchlab, Pump.fun, Meteora DBC,
  Moonit, Boop) против «AMM» (всё остальное: Pump AMM, CPMM, DAMM v2,
  DLMM, CLMM, Whirlpool, AMM v4) -- по pool_program из задачи A;
* котировка пула: SOL (WSOL или натив кривой) против другого токена;
* капитализация в момент покупки источника, USD: < 50k, 50-200k, > 200k.
  Цена токена -- средняя цена сделки источника в SOL-экв (его трата по
  методу проекта / полученные им токены, по его транзакции), в USD -- по
  курсу SOL/USD с цепи (RateBook). Предложение -- ТЕКУЩЕЕ предложение минта
  (getMultipleAccounts): предложение на момент покупки по цепи дёшево не
  получить; у сожжённых/допечатанных токенов это погрешность.
Метрики: n, медиана growth_after_30s (только пулы, где остатки -- резервы),
медиана growth_30s (все), доля падений > 30 % к 30 с (рост <= 0.7).
Служба c2_groups.
"""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402

SERVICE = "c2_groups"
CURVES = {"LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium Launchlab",
          "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun",
          "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
          "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG": "Moonit",
          "boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4": "Boop"}
CAP_BINS = ((0, 50_000, "<50k"), (50_000, 200_000, "50-200k"), (200_000, float("inf"), ">200k"))


def stats(rows: list) -> dict:
    ga = [r["growth_after_30s"] for r in rows if r.get("growth_after_30s") is not None]
    g = [r["growth_30s"] for r in rows if r.get("growth_30s") is not None]
    return {"n": len(rows), "n_after": len(ga), "growth_after_30s_median": round(C.median(ga), 4) if ga else None,
            "crash_after_share": round(sum(x <= 0.7 for x in ga) / len(ga), 3) if ga else None,
            "n_growth": len(g), "growth_30s_median": round(C.median(g), 4) if g else None,
            "crash_30s_share": round(sum(x <= 0.7 for x in g) / len(g), 3) if g else None,
            "x2_30s_share": round(sum(x >= 2 for x in g) / len(g), 3) if g else None}


def group_table(trades: list) -> dict:
    def pool_type(t):
        p = t.get("pool_program")
        return None if not p else ("curve" if p in CURVES else "amm")

    def quote(t):
        q = t.get("quote_mint")
        return None if not q else ("SOL" if q in (C.WSOL, C.NATIVE_QUOTE) else "other")

    def cap(t):
        c = t.get("mcap_usd")
        if c is None:
            return None
        return next(lbl for lo, hi, lbl in CAP_BINS if lo <= c < hi)
    out = {}
    for name, fn in (("pool_type", pool_type), ("quote", quote), ("mcap", cap)):
        groups: dict = {}
        for t in trades:
            groups.setdefault(fn(t) or "нет данных", []).append(t)
        out[name] = {k: stats(v) for k, v in sorted(groups.items())}
    return out


def main() -> int:
    key = C.RC.helius_key()[0]
    if not key:
        print("СТОП: ключ Helius не задан", file=sys.stderr)
        return 2
    rpc = C.C2Rpc(SERVICE, key=key)
    src = sorted(C.DATA.glob("crowd_metric_2*.json"))[-1]
    d = json.loads(src.read_text(encoding="utf-8"))
    trades = [dict(t, source=ps["address"], remark=ps.get("remark"))
              for ps in d["per_source"] for t in ps.get("trades") or [] if t.get("signature")]
    got = rpc.get_txs([t["signature"] for t in trades])
    mints = sorted({t["mint"] for t in trades if t.get("mint")})
    supply = {}
    for i in range(0, len(mints), 100):
        part = mints[i:i + 100]
        vals = (rpc.call("getMultipleAccounts", [part, {"encoding": "jsonParsed"}]) or {}).get("value") or []
        for m, v in zip(part, vals):
            info = ((((v or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
            if info.get("supply") is not None and info.get("decimals") is not None:
                supply[m] = D(info["supply"]) / D(10) ** int(info["decimals"])
    book = C.RateBook(rpc)
    for t in trades:
        tx = got.get(t["signature"])
        t["mcap_usd"] = None
        if tx is None:
            t["why_no_mcap"] = "узел не отдал транзакцию"
            continue
        tok = C.owner_mint_delta(tx, t["mint"]).get(t["source"])
        if not tok or tok <= 0:
            t["why_no_mcap"] = "источник не получил токен на свой кошелёк"
            continue
        rate = D(str(t["rate_usd_per_sol"])) if t.get("rate_usd_per_sol") else None
        if rate is None:
            r, _ = book.rate_for(tx)
            rate = r
        if rate is None or t["mint"] not in supply:
            t["why_no_mcap"] = "нет курса SOL/USD" if rate is None else "нет предложения минта"
            continue
        price_usd = D(str(t["spend_sol_equiv"])) * rate / tok
        t["price_usd"] = float(price_usd)
        t["mcap_usd"] = float(price_usd * supply[t["mint"]])
    tables = group_table(trades)
    out = {"generated_utc": C.utc(time.time()), "source_file": src.name, "n_trades": len(trades),
           "n_with_mcap": sum(1 for t in trades if t.get("mcap_usd") is not None),
           "groups": tables,
           "cross_pool_quote": {f"{pt}|{q}": stats([t for t in trades
                                                    if ((t.get("pool_program") in CURVES) == (pt == "curve"))
                                                    and t.get("pool_program")
                                                    and ((t.get("quote_mint") in (C.WSOL, C.NATIVE_QUOTE)) == (q == "SOL"))
                                                    and t.get("quote_mint")])
                                for pt in ("curve", "amm") for q in ("SOL", "other")},
           "trades": [{k: t.get(k) for k in ("remark", "signature", "mint", "pool_program", "quote_mint",
                                            "spend_sol_equiv", "price_usd", "mcap_usd", "why_no_mcap",
                                            "growth_30s", "growth_after_30s")} for t in trades],
           "rate_stats": book.stats, "credits_this_run": rpc.stats.get("кредитов"),
           "c2_usage": C.c2_usage_report()}
    (C.DATA / f"c2_groups_{C.today_utc()}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1,
                                                                        default=str), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("n_trades", "n_with_mcap", "groups", "cross_pool_quote")},
                     ensure_ascii=False, indent=1))
    print(f"кредитов {rpc.stats.get('кредитов')}; C2 сегодня {C.c2_spent_today()}")
    return 0


def self_test() -> int:
    tr = [{"pool_program": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj", "quote_mint": C.WSOL, "mcap_usd": 30_000,
           "growth_30s": 0.5, "growth_after_30s": None},
          {"pool_program": "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C", "quote_mint": "X", "mcap_usd": 300_000,
           "growth_30s": 1.5, "growth_after_30s": 1.4},
          {"pool_program": None, "quote_mint": None, "mcap_usd": None, "growth_30s": None}]
    g = group_table(tr)
    ok = (g["pool_type"]["curve"]["crash_30s_share"] == 1.0 and g["pool_type"]["amm"]["n"] == 1
          and g["mcap"][">200k"]["n"] == 1 and g["quote"]["нет данных"]["n"] == 1)
    print(f"  [{'ok  ' if ok else 'СБОЙ'}] группы и доля падений считаются, «нет данных» отдельной группой")
    print(f"самопроверка c2_groups: {int(ok)}/1 пройдено")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else main())
