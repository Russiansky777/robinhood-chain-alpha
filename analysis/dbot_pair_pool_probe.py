#!/usr/bin/env python3
"""ПРОБА: принимает ли DBot АДРЕС ПУЛА в поле pair.

Зачем. Спасение через утилизатор упирается в то, что distribute не
переносит Token-2022 (измерено: отказ 0x1f и с комиссией, и без). Но
свопы DBot с Token-2022 работают -- значит, если DBot примет в pair
адрес конкретного пула, зависшую позицию можно продать НЕ через прямую
пару mint->SOL, а через тот пул, который нашёл Jupiter. Это дешевле и
безопаснее утилизатора: ключи никуда не переезжают.

Документация DBot (docs.dbotx.com/reference/create-fast-swaps) говорит,
что pair -- это "address of the token or pair". Своими глазами я её не
читал (сеть на *.dbotx.com из моего контейнера заблокирована), поэтому
проба и нужна: сырой ответ сохраняется целиком.

ЧТО ДЕЛАЕТ:
  1. берёт кошельки живьём из /automation/follow_orders (белый список);
  2. читает остаток минта в цепи;
  3. спрашивает у Jupiter маршрут mint -> SOL на продаваемую долю и
     берёт ammKey первого шага и его outputMint;
  4. с --confirm отправляет продажу с pair = ammKey (НЕ минт);
  5. успех считает ТОЛЬКО по уменьшению баланса в цепи;
  6. сравнивает фактически полученный SOL с котировкой Jupiter.

ГРАНИЦЫ: maxSlippage жёстко <= 0.5 (тот же потолок, что в стороже);
без --confirm ничего не отправляется; доля продажи задаётся явно.
Ни одной покупки.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "dbot_pair_pool_probe.json"
DBOT_HOST = "https://api-bot-v1.dbotx.com"
CHAIN = "solana"
WSOL = "So11111111111111111111111111111111111111112"
MAX_SLIPPAGE_CEILING = 0.5

_SECRETS: list[str] = []


def scrub(t: str) -> str:
    for s in _SECRETS:
        if s and len(s) > 6:
            t = t.replace(s, "<секрет>")
    return t


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {scrub(m)}", flush=True)


def env(*names: str) -> tuple[str, str]:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            if "\n" in v or "\r" in v:
                raise RuntimeError(f"{n} содержит перевод строки -- не сырой ключ")
            _SECRETS.append(v)
            return v, n
    raise RuntimeError(f"нет переменных окружения: {', '.join(names)}")


def dbot_get(path: str, params: dict, key: str) -> tuple[int | None, dict]:
    for a in range(5):
        try:
            r = requests.get(f"{DBOT_HOST}{path}", params=params, headers={"X-API-KEY": key}, timeout=30)
        except Exception:  # noqa: BLE001
            time.sleep(2 * (a + 1)); continue
        if r.status_code == 429:
            time.sleep(3 * (a + 1)); continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"non_json": scrub(r.text[:400])}
    return None, {}


def dbot_post(path: str, body: dict, key: str) -> tuple[int | None, dict]:
    for a in range(3):
        try:
            r = requests.post(f"{DBOT_HOST}{path}", json=body, headers={"X-API-KEY": key}, timeout=40)
        except Exception:  # noqa: BLE001
            time.sleep(2 * (a + 1)); continue
        if r.status_code == 429:
            time.sleep(3 * (a + 1)); continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"non_json": scrub(r.text[:600])}
    return None, {}


def items(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("res", "data", "results", "list", "items"):
            if isinstance(body.get(k), list):
                return body[k]
    return []


def wallets(key: str) -> list[dict]:
    st, b = dbot_get("/automation/follow_orders", {}, key)
    if st != 200:
        raise RuntimeError(f"follow_orders http={st}")
    out, seen = [], set()
    for r in items(b):
        a, w = r.get("walletAddress"), r.get("walletId")
        if a and w and a not in seen:
            seen.add(a)
            out.append({"address": a, "walletId": w, "name": r.get("name") or a})
    if not out:
        raise RuntimeError("follow_orders вернул 0 задач")
    return out


def rpc(method: str, params: list, key: str):
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    last = None
    for a in range(5):
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=45)
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__; time.sleep(1.5 * (a + 1)); continue
        if r.status_code == 429 or 500 <= r.status_code < 600:
            last = f"http={r.status_code}"; time.sleep(2 * (a + 1)); continue
        if not r.ok:
            raise RuntimeError(f"{method}: http={r.status_code}")
        b = r.json()
        if "error" in b:
            raise RuntimeError(f"{method}: {json.dumps(b['error'])[:200]}")
        return b.get("result")
    raise RuntimeError(f"{method}: исчерпаны попытки ({last})")


def token_bal(addr: str, mint: str, key: str) -> tuple[int, int | None]:
    res = rpc("getTokenAccountsByOwner", [addr, {"mint": mint}, {"encoding": "jsonParsed"}], key)
    raw, dec = 0, None
    for acc in (res or {}).get("value") or []:
        try:
            ta = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            raw += int(ta["amount"]); dec = ta.get("decimals", dec)
        except (KeyError, TypeError, ValueError):
            continue
    return raw, dec


def sol_bal(addr: str, key: str) -> float:
    return ((rpc("getBalance", [addr], key) or {}).get("value") or 0) / 1e9


def jup_route(mint: str, amount_raw: int, bps: int = 3000) -> dict:
    """Котировка и МАРШРУТ. Нужен ammKey первого шага -- это и есть адрес
    пула, который мы отдадим DBot в pair."""
    if amount_raw <= 0:
        return {"ошибка": "нулевое количество"}
    errs = {}
    for url in ("https://lite-api.jup.ag/swap/v1/quote", "https://quote-api.jup.ag/v6/quote"):
        try:
            r = requests.get(url, params={"inputMint": mint, "outputMint": WSOL,
                                           "amount": str(amount_raw), "slippageBps": str(bps)}, timeout=25)
        except Exception as exc:  # noqa: BLE001
            errs[url] = type(exc).__name__; continue
        if r.status_code != 200:
            errs[url] = f"http={r.status_code}: {scrub(r.text[:200])}"; continue
        try:
            b = r.json()
        except ValueError:
            errs[url] = "не JSON"; continue
        if b.get("error") or b.get("errorCode"):
            errs[url] = str(b.get("error") or b.get("errorCode")); continue
        steps = []
        for rp in (b.get("routePlan") or []):
            si = rp.get("swapInfo") or {}
            steps.append({"label": si.get("label"), "ammKey": si.get("ammKey"),
                           "inputMint": si.get("inputMint"), "outputMint": si.get("outputMint"),
                           "inAmount": si.get("inAmount"), "outAmount": si.get("outAmount"),
                           "percent": rp.get("percent")})
        out = b.get("outAmount")
        return {"SOL": int(out) / 1e9 if out else None, "outAmount": out,
                "priceImpactPct": b.get("priceImpactPct"), "шагов": len(steps),
                "маршрут": steps, "хост": url}
    return {"ошибка": "ни один хост Jupiter не ответил", "подробности": errs}


def order_status(ids: list[str], key: str) -> list[dict]:
    if not ids:
        return []
    st, b = dbot_get("/automation/swap_orders", {"ids": ",".join(ids)}, key)
    if st != 200:
        return [{"id": i, "ошибка_запроса": f"http={st}"} for i in ids]
    out = [{"id": r.get("id"), "state": r.get("state"), "swapHash": r.get("swapHash"),
            "errorCode": r.get("errorCode"), "errorMessage": r.get("errorMessage")}
           for r in items(b)]
    return out or [{"id": i, "ошибка_запроса": "ответ без списка"} for i in ids]


def wait_order(ids: list[str], key: str, timeout_s: int = 60) -> list[dict]:
    deadline = time.time() + timeout_s
    last: list[dict] = []
    while True:
        last = order_status(ids, key)
        if not ({r.get("state") for r in last} & {"init", "processing"}) or time.time() > deadline:
            return last
        time.sleep(3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mint", required=True)
    ap.add_argument("--wallet", default="", help="пусто -- взять кошелёк задачи с наибольшим остатком")
    ap.add_argument("--sell-percent", type=float, default=0.1)
    ap.add_argument("--max-slippage", type=float, default=0.4)
    ap.add_argument("--confirm", action="store_true")
    args = ap.parse_args()

    slip = min(args.max_slippage, MAX_SLIPPAGE_CEILING)
    dbot_key, dn = env("DBOT_API_KEY", "DBOT_APIKEY")
    hel_key, hn = env("HELIUS_API_KEY", "HELIUS_API")
    log(f"ключи: DBot из {dn}, Helius из {hn}; режим={'БОЕВОЙ' if args.confirm else 'СУХОЙ'}")

    wl = wallets(dbot_key)
    for w in wl:
        _SECRETS.append(w["walletId"])
    holders = []
    for w in wl:
        raw, dec = token_bal(w["address"], args.mint, hel_key)
        if raw:
            holders.append({**w, "raw": raw, "decimals": dec})
    if args.wallet:
        src = next((h for h in holders if h["address"] == args.wallet), None)
        if src is None:
            raise RuntimeError(f"на {args.wallet} нет остатка {args.mint} (или он не кошелёк задачи)")
    else:
        if not holders:
            raise RuntimeError(f"ни на одном кошельке задач нет остатка {args.mint}")
        src = max(holders, key=lambda h: h["raw"])
    dec = src["decimals"] or 0
    sell_raw = int(src["raw"] * args.sell_percent)
    log(f"кошелёк {src['name']} {src['address']}: остаток raw={src['raw']} dec={dec}; "
        f"продаём долю {args.sell_percent} = {sell_raw} raw")

    route_full = jup_route(args.mint, src["raw"])
    route_part = jup_route(args.mint, sell_raw)
    rep: dict = {"начато_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "режим": "боевой" if args.confirm else "сухой прогон",
                 "минт": args.mint, "кошелёк": {"имя": src["name"], "адрес": src["address"]},
                 "остаток_raw": src["raw"], "decimals": dec,
                 "доля_продажи": args.sell_percent, "продаём_raw": sell_raw,
                 "maxSlippage": slip,
                 "маршрут_на_весь_остаток": route_full,
                 "маршрут_на_продаваемую_долю": route_part}
    log(f"маршрут Jupiter на долю: {json.dumps(route_part, ensure_ascii=False)[:700]}")

    steps = (route_part.get("маршрут") or [])
    if not steps:
        rep["итог"] = "Jupiter не дал маршрут -- адрес пула брать неоткуда"
        OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2))
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    amm = steps[0]["ammKey"]
    out_mint = steps[0]["outputMint"]
    rep["адрес_пула_первого_шага"] = amm
    rep["outputMint_первого_шага"] = out_mint
    rep["первый_шаг_сразу_в_SOL"] = (out_mint == WSOL)
    log(f"адрес пула первого шага: {amm} ({steps[0]['label']}), outputMint={out_mint}, "
        f"сразу в SOL={out_mint == WSOL}")

    if not args.confirm:
        rep["итог"] = "СУХОЙ ПРОГОН: продажа НЕ отправлена"
        rep["тело_которое_ушло_бы"] = {"chain": CHAIN, "pair": amm, "walletIdList": ["<walletId>"],
                                        "type": "sell", "sellPercent": args.sell_percent,
                                        "maxSlippage": slip, "retries": 3}
        OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2))
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    sol_before = sol_bal(src["address"], hel_key)
    tok_before = src["raw"]
    body = {"chain": CHAIN, "pair": amm, "walletIdList": [src["walletId"]], "type": "sell",
            "sellPercent": args.sell_percent, "maxSlippage": slip, "retries": 3}
    log(f"ОТПРАВЛЯЮ продажу с pair=АДРЕС ПУЛА: {json.dumps({**body, 'walletIdList': ['<walletId>']}, ensure_ascii=False)}")
    st, resp = dbot_post("/automation/swap_orders_with_multi_wallets", body, dbot_key)
    rep["http"] = st
    rep["ответ"] = json.loads(scrub(json.dumps(resp, ensure_ascii=False, default=str)))
    log(f"ответ DBot: http={st} {scrub(json.dumps(resp, ensure_ascii=False, default=str)[:700])}")

    ids = ((resp.get("res") or {}).get("ids") or []) if isinstance(resp, dict) else []
    rows = wait_order([str(i) for i in ids], dbot_key) if ids else []
    rep["состояние_ордера"] = rows
    for r in rows:
        log(f"ордер {r.get('id')}: state={r.get('state')} errorCode={r.get('errorCode')} "
            f"errorMessage={r.get('errorMessage')} tx={r.get('swapHash')}")

    time.sleep(8)
    tok_after, _ = token_bal(src["address"], args.mint, hel_key)
    sol_after = sol_bal(src["address"], hel_key)
    rep["баланс_токена"] = {"до_raw": tok_before, "после_raw": tok_after,
                             "продано_raw": tok_before - tok_after}
    rep["баланс_SOL"] = {"до": sol_before, "после": sol_after,
                          "дельта": round(sol_after - sol_before, 9)}
    rep["баланс_упал"] = tok_after < tok_before
    exp = route_part.get("SOL")
    if rep["баланс_упал"] and exp:
        got = sol_after - sol_before
        rep["доля_от_котировки"] = round(got / exp * 100, 2)
        rep["пояснение_доли"] = ("дельта SOL кошелька уже ЗА ВЫЧЕТОМ комиссии сети и чаевых DBot, "
                                  "поэтому доля занижена относительно чистой цены исполнения")
    rep["итог"] = ("DBot ПРИНЯЛ адрес пула в pair и продал" if rep["баланс_упал"]
                    else "баланс не уменьшился -- продажи через адрес пула НЕ произошло")
    log(rep["итог"])
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    print()
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    print(f"файл: {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
