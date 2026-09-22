#!/usr/bin/env python3
"""Владелец, РАЗОВАЯ РУЧНАЯ ПРОДАЖА одной позиции. РЕАЛЬНЫЕ ДЕНЬГИ.

Вызов ровно тот же, что у сторожа (dbot_sold_position_guard.sell_100_percent):
POST /automation/swap_orders_with_multi_wallets, type="sell", -- отличаются
только sellPercent (0.95, при неудаче 0.90), maxSlippage 0.4, retries 3.

ПРЕДОХРАНИТЕЛИ. Продажа не отправляется без --confirm-live-sell И без
точного совпадения кошелька и минта с аргументами. walletId не
хардкодится -- берётся живьём из /automation/follow_orders по адресу
кошелька (как в сторожe).

СТОРОЖ НЕ ОСТАНАВЛИВАЕТСЯ (прямое указание владельца). Поэтому скрипт
ДО и ПОСЛЕ смотрит, есть ли эта позиция в expired-списке DBot: если
сторож продаст её сам, баланс упадёт не от нашего вызова, и выдавать
это за результат своей продажи нельзя.

ПРОВЕРКА БАЛАНСА -- НЕ ОДНИМ СПОСОБОМ. getTokenAccountsByOwner с
фильтром по минту исторически видит не все токен-программы, а у
владельца в списке дел стоит отдельная проверка Token-2022. Поэтому
баланс читается тремя путями (по минту, по программе Token, по
программе Token-2022) и печатаются все три: если они разойдутся --
это и будет ответ про Token-2022, а не молчаливый ноль.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

DBOT_HOST = "https://api-bot-v1.dbotx.com"
SOLANA_CHAIN = "solana"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
WSOL = "So11111111111111111111111111111111111111112"

_SECRETS: list[str] = []


def scrub(t: str) -> str:
    for s in _SECRETS:
        if s:
            t = t.replace(s, "[REDACTED]")
    return t


def log(msg: str) -> None:
    print(f"[sell] {scrub(str(msg))}", flush=True)


def env(*names: str) -> tuple[str, str]:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            if any(c in v for c in ("\n", "\r")):
                raise RuntimeError(f"{n} содержит перевод строки -- не похоже на сырой ключ")
            _SECRETS.append(v)
            return v, n
    raise RuntimeError("не задано ни одно из: " + ", ".join(names))


# ---------- DBot ----------

def dbot_get(path: str, params: dict, key: str) -> tuple[int | None, dict]:
    for attempt in range(6):
        try:
            r = requests.get(f"{DBOT_HOST}{path}", params=params, headers={"X-API-KEY": key}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            log(f"GET {path} попытка {attempt+1}/6: {type(exc).__name__}: {exc}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"non_json_body": scrub(r.text[:500])}
    return None, {}


def dbot_post(path: str, body: dict, key: str) -> tuple[int | None, dict]:
    for attempt in range(3):
        try:
            r = requests.post(f"{DBOT_HOST}{path}", json=body, headers={"X-API-KEY": key}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            log(f"POST {path} попытка {attempt+1}/3: {type(exc).__name__}: {exc}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"non_json_body": scrub(r.text[:500])}
    return None, {}


def extract_items(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("res", "data", "results", "list", "items"):
            v = body.get(k)
            if isinstance(v, list):
                return v
    return []


def find_wallet_id(wallet: str, key: str) -> tuple[str | None, dict]:
    st, body = dbot_get("/automation/follow_orders", {}, key)
    if st != 200:
        raise RuntimeError(f"follow_orders вернул http={st} -- walletId взять неоткуда")
    for row in extract_items(body):
        if row.get("walletAddress") == wallet:
            return row.get("walletId"), {"task_id": row.get("id") or row.get("_id"),
                                          "task_name": row.get("name"),
                                          "enabled": row.get("enabled")}
    return None, {}


def expired_has(wallet: str, mint: str, key: str, max_pages: int = 5) -> dict:
    """Есть ли эта позиция в expired-списке (её может продать сторож)."""
    found, pages, ok = [], 0, True
    page = 0
    while page < max_pages:
        # Путь ровно тот же, что в сторожe. На первом прогоне я взял
        # /automation/copy_tpsl_tasks -- он отдал не 200, и проверка
        # молча вернула "0 страниц прочитано, список неполный".
        st, body = dbot_get("/automation/pnl_orders_from_follow_order",
                             {"chain": SOLANA_CHAIN, "state": "expired", "size": 20, "page": page}, key)
        if st != 200:
            ok = False
            break
        items = extract_items(body)
        pages += 1
        if not items:
            break
        for rec in items:
            if rec.get("walletAddress") == wallet and (rec.get("tokenInfo") or {}).get("contract") == mint:
                found.append({"id": rec.get("id") or rec.get("_id"), "state": rec.get("state")})
        page += 1
    return {"в_expired": len(found), "записи": found[:3], "страниц_прочитано": pages, "список_полный": ok}


# ---------- цепочка ----------

def rpc(method: str, params: list, key: str) -> dict | None:
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    for attempt in range(5):
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=45)
        except Exception as exc:  # noqa: BLE001
            log(f"rpc {method} попытка {attempt+1}/5: {type(exc).__name__}: {exc}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429 or 500 <= r.status_code < 600:
            time.sleep(2 * (attempt + 1))
            continue
        if not r.ok:
            log(f"rpc {method} http={r.status_code}: {scrub(r.text[:200])}")
            return None
        b = r.json()
        if "error" in b:
            log(f"rpc {method} ошибка: {scrub(str(b['error'])[:200])}")
            return None
        return b.get("result")
    return None


def _sum_accounts(res, mint: str | None) -> tuple[float, int]:
    total, n = 0.0, 0
    for acc in (res or {}).get("value") or []:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            if mint is not None and info.get("mint") != mint:
                continue
            total += float(info["tokenAmount"]["uiAmount"] or 0)
            n += 1
        except (KeyError, TypeError):
            continue
    return total, n


def balance_three_ways(wallet: str, mint: str, key: str) -> dict:
    """Три независимых чтения одного и того же баланса."""
    by_mint = rpc("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}], key)
    by_tok = rpc("getTokenAccountsByOwner", [wallet, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}], key)
    by_t22 = rpc("getTokenAccountsByOwner", [wallet, {"programId": TOKEN_2022_PROGRAM}, {"encoding": "jsonParsed"}], key)
    m_bal, m_n = _sum_accounts(by_mint, None)
    t_bal, t_n = _sum_accounts(by_tok, mint)
    z_bal, z_n = _sum_accounts(by_t22, mint)
    vals = [m_bal, t_bal, z_bal]
    return {
        "по_минту": m_bal, "счетов_по_минту": m_n,
        "по_программе_Token": t_bal, "счетов_Token": t_n,
        "по_программе_Token2022": z_bal, "счетов_Token2022": z_n,
        "расхождение": (max(vals) - min(vals)) > 1e-12,
        "итог": max(vals),
    }


def mint_program(mint: str, key: str) -> dict:
    """Программа минта и, главное, СТАВКА комиссии за перевод.

    Реальная причина отказа свопа (errorMessage ордера DBot) --
    ExceededSlippage в Meteora cp-amm: комиссия за перевод съедает часть
    токена при заходе в пул, выход выходит меньше расчётного, защита
    пула отбивает сделку. Без ставки комиссии нельзя сказать, хватит ли
    поднять проскальзывание или через этот пул продать вообще нельзя."""
    res = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}], key)
    owner = ((res or {}).get("value") or {}).get("owner")
    parsed = (((res or {}).get("value") or {}).get("data") or {})
    info = (parsed.get("parsed") or {}).get("info", {}) if isinstance(parsed, dict) else {}
    exts = info.get("extensions") or []
    names, fee = [], None
    for e in exts:
        if not isinstance(e, dict):
            continue
        names.append(e.get("extension"))
        if e.get("extension") == "transferFeeConfig":
            st = e.get("state") or {}
            newer = st.get("newerTransferFee") or {}
            older = st.get("olderTransferFee") or {}
            fee = {"newer_bps": newer.get("transferFeeBasisPoints"),
                    "newer_max": newer.get("maximumFee"),
                    "older_bps": older.get("transferFeeBasisPoints"),
                    "older_max": older.get("maximumFee"),
                    "withheld": st.get("withheldAmount")}
            bps = newer.get("transferFeeBasisPoints")
            if isinstance(bps, int):
                fee["в_процентах"] = round(bps / 100, 4)
    return {"owner_program": owner,
             "это_Token2022": owner == TOKEN_2022_PROGRAM,
             "расширения": [n for n in names if n],
             "комиссия_за_перевод": fee,
             "decimals": info.get("decimals"),
             "supply": info.get("supply")}


def raw_balance(wallet: str, mint: str, key: str) -> tuple[int, int | None]:
    """Сырой (целочисленный) баланс и decimals -- для котировки Jupiter,
    где сумма задаётся в минимальных единицах."""
    res = rpc("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}], key)
    total, dec = 0, None
    for acc in (res or {}).get("value") or []:
        try:
            ta = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += int(ta["amount"])
            dec = ta.get("decimals", dec)
        except (KeyError, TypeError, ValueError):
            continue
    return total, dec


def jupiter_quote(mint: str, amount_raw: int, slippage_bps: int) -> dict:
    """Котировка Jupiter НА ЧТЕНИЕ: есть ли вообще маршрут для этого
    Token-2022 с комиссией за перевод и какой ценой.

    Ключ кошелька для котировки не нужен -- подпись требуется только для
    самой отправки, а её сделать нечем: приватный ключ у DBot.
    Пробуем оба известных хоста Jupiter и честно пишем, который ответил."""
    out: dict = {"amount_raw": amount_raw, "slippage_bps": slippage_bps}
    hosts = [("lite-api", "https://lite-api.jup.ag/swap/v1/quote"),
             ("quote-api-v6", "https://quote-api.jup.ag/v6/quote")]
    for name, url in hosts:
        params = {"inputMint": mint, "outputMint": WSOL, "amount": str(amount_raw),
                  "slippageBps": str(slippage_bps), "restrictIntermediateTokens": "false"}
        try:
            r = requests.get(url, params=params, timeout=30)
        except Exception as exc:  # noqa: BLE001
            out[name] = f"сеть: {type(exc).__name__}: {exc}"
            continue
        if r.status_code != 200:
            out[name] = f"http={r.status_code}: {scrub(r.text[:300])}"
            continue
        try:
            b = r.json()
        except ValueError:
            out[name] = "не JSON"
            continue
        routes = []
        for rp in (b.get("routePlan") or []):
            si = rp.get("swapInfo") or {}
            routes.append({"label": si.get("label"), "amm": si.get("ammKey"),
                            "in": si.get("inAmount"), "out": si.get("outAmount"),
                            "percent": rp.get("percent")})
        out[name] = {"outAmount": b.get("outAmount"),
                      "outAmount_SOL": (int(b["outAmount"]) / 1e9) if b.get("outAmount") else None,
                      "priceImpactPct": b.get("priceImpactPct"),
                      "маршрут": routes,
                      "ошибка": b.get("error") or b.get("errorCode")}
    return out


def find_sell_tx(wallet: str, mint: str, since_ts: int, key: str) -> dict | None:
    """Ищем НАШУ продажу: свежая транзакция кошелька, где баланс минта
    уменьшился. Подпись не выдумываем -- если не нашли, вернём None."""
    sigs = rpc("getSignaturesForAddress", [wallet, {"limit": 25}], key) or []
    cands = [s["signature"] for s in sigs
             if s.get("blockTime") and s["blockTime"] >= since_ts - 5 and s.get("err") is None]
    for sig in cands[::-1]:
        tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}], key)
        if not tx:
            continue
        meta = tx.get("meta") or {}
        pre = post = 0.0
        for b in meta.get("preTokenBalances") or []:
            if b.get("owner") == wallet and b.get("mint") == mint:
                pre += float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        for b in meta.get("postTokenBalances") or []:
            if b.get("owner") == wallet and b.get("mint") == mint:
                post += float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        if post >= pre:
            continue
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
        wsol = 0.0
        for b in meta.get("preTokenBalances") or []:
            if b.get("owner") == wallet and b.get("mint") == WSOL:
                wsol -= float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        for b in meta.get("postTokenBalances") or []:
            if b.get("owner") == wallet and b.get("mint") == WSOL:
                wsol += float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        return {"signature": sig, "slot": tx.get("slot"), "block_time": tx.get("blockTime"),
                 "solscan": f"https://solscan.io/tx/{sig}",
                 "токенов_продано": round(pre - post, 9),
                 "SOL_нативный_дельта": round(native, 9) if native is not None else None,
                 "WSOL_дельта": round(wsol, 9),
                 "SOL_получено_итого": round((native or 0) + wsol, 9),
                 "комиссия_SOL": round((meta.get("fee") or 0) / 1e9, 9)}
    return None


def order_status(ids: list[str], key: str) -> list[dict]:
    """GET /automation/swap_orders?ids=... -- состояние ордера и ПРИЧИНА
    отказа. Эндпоинт из документации DBot (docs.dbotx.com/reference/
    get-swap-order-info): state = init/processing/done/fail/expired,
    плюс swapHash, errorCode, errorMessage. Без него мы гадали, почему
    принятый ордер не доходит до цепочки."""
    if not ids:
        return []
    st, body = dbot_get("/automation/swap_orders", {"ids": ",".join(ids)}, key)
    if st != 200:
        return [{"id": i, "ошибка_запроса": f"http={st}", "сырое": scrub(str(body)[:300])} for i in ids]
    out = []
    for r in extract_items(body):
        out.append({"id": r.get("id"), "state": r.get("state"),
                     "swapHash": r.get("swapHash"), "errorCode": r.get("errorCode"),
                     "errorMessage": r.get("errorMessage"),
                     "tradeType": r.get("tradeType"), "txPriceUsd": r.get("txPriceUsd")})
    if not out:
        out = [{"id": i, "ошибка_запроса": "ответ без распознанного списка",
                 "сырое": scrub(json.dumps(body, ensure_ascii=False, default=str)[:400])} for i in ids]
    return out


def wait_order(ids: list[str], key: str, timeout_s: int = 45) -> list[dict]:
    """Ждём терминального состояния ордера, а не фиксированную паузу."""
    deadline = time.time() + timeout_s
    last: list[dict] = []
    while True:
        last = order_status(ids, key)
        states = {r.get("state") for r in last}
        if not (states & {"init", "processing"}) or time.time() > deadline:
            return last
        time.sleep(3)


def parse_sell_response(status: int | None, body: dict) -> tuple[bool, str]:
    if status != 200:
        return False, f"http={status}"
    if not isinstance(body, dict):
        return False, f"неожиданная форма ответа: {str(body)[:300]}"
    err = body.get("err")
    msg = next((str(body[k]) for k in ("msg", "message", "errorMessage", "error", "description") if body.get(k)), None)
    if err is True:
        return False, msg or "err=true без текста"
    if err is False:
        return True, msg or "ok"
    return False, msg or f"поля err нет, успех не подтверждён: {str(body)[:300]}"


def sell(mint: str, wallet_id: str, percent: float, slippage: float, retries: int,
         key: str) -> tuple[int | None, dict, bool, str]:
    body = {"chain": SOLANA_CHAIN, "pair": mint, "walletIdList": [wallet_id], "type": "sell",
            "sellPercent": percent, "maxSlippage": slippage, "retries": retries}
    log(f"ОТПРАВЛЯЮ ПРОДАЖУ: {json.dumps({**body, 'walletIdList': ['<walletId>']}, ensure_ascii=False)}")
    st, resp = dbot_post("/automation/swap_orders_with_multi_wallets", body, key)
    ok, msg = parse_sell_response(st, resp)
    log(f"ответ DBot: http={st} принято={ok} сообщение={msg}")
    log(f"сырое тело ответа: {scrub(json.dumps(resp, ensure_ascii=False, default=str)[:1500])}")
    return st, resp, ok, msg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--mint", required=True)
    ap.add_argument("--percents", default="0.95,0.90",
                    help="доли продажи; можно пары доля:проскальзывание, напр. 1.0:0.5,0.25:0.9")
    ap.add_argument("--jupiter-quote", default="",
                    help="через запятую slippageBps: спросить котировку Jupiter и выйти, напр. 300,1000,5000")
    ap.add_argument("--probe-ids", default="",
                    help="через запятую: прочитать состояние этих ордеров и выйти")
    ap.add_argument("--max-slippage", type=float, default=0.4)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--wait-s", type=int, default=25)
    ap.add_argument("--confirm-live-sell", action="store_true")
    ap.add_argument("--since-ts", type=int, default=0,
                    help="искать сделки по минту начиная с этой метки времени (для проверки постфактум)")
    args = ap.parse_args()

    dbot_key, dbot_name = env("DBOT_API_KEY")
    hel_key, hel_name = env("HELIUS_API_KEY", "HELIUS_API")
    log(f"ключи: DBot из {dbot_name}, Helius из {hel_name}")
    log(f"кошелёк={args.wallet} минт={args.mint}")

    report: dict = {"кошелёк": args.wallet, "минт": args.mint,
                     "начато_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "шаги": []}

    wallet_id, task = find_wallet_id(args.wallet, dbot_key)
    if not wallet_id:
        raise RuntimeError(f"в follow_orders нет задачи с walletAddress={args.wallet} -- walletId не выдумываю")
    _SECRETS.append(wallet_id)
    log(f"walletId найден живьём из follow_orders; задача: {json.dumps(task, ensure_ascii=False)}")
    report["задача"] = task

    # ПОРЯДОК ВАЖЕН: между решением продать и самим вызовом не должно
    # быть медленной диагностики -- на первом реальном прогоне обход
    # expired-списка задержал отправку. Сначала баланс (без него продавать
    # нельзя) и продажа, вся остальная диагностика -- после.
    prog0 = mint_program(args.mint, hel_key)
    report["программа_минта"] = prog0
    log(f"программа минта: {json.dumps(prog0, ensure_ascii=False)}")

    bal0 = balance_three_ways(args.wallet, args.mint, hel_key)
    log(f"БАЛАНС ДО: {json.dumps(bal0, ensure_ascii=False)}")
    report["баланс_до"] = bal0

    if bal0["итог"] <= 0:
        report["итог"] = "баланс уже нулевой -- продавать нечего, вызов НЕ отправлен"
        log(report["итог"])
        print("\n=== РЕЗУЛЬТАТ ===\n" + json.dumps(report, ensure_ascii=False, indent=2))
        return

    if args.jupiter_quote:
        amt, dec = raw_balance(args.wallet, args.mint, hel_key)
        report["сырой_баланс"] = {"amount": amt, "decimals": dec}
        log(f"сырой баланс для котировки: {amt} (decimals={dec})")
        qs = []
        for bps in [int(x) for x in args.jupiter_quote.split(",") if x.strip()]:
            q = jupiter_quote(args.mint, amt, bps)
            qs.append(q)
            log(f"Jupiter slippageBps={bps}: {json.dumps(q, ensure_ascii=False)[:900]}")
        report["котировки_jupiter"] = qs

    if args.probe_ids:
        ids = [x.strip() for x in args.probe_ids.split(",") if x.strip()]
        st_rows = order_status(ids, dbot_key)
        report["состояние_ордеров"] = st_rows
        for r in st_rows:
            log(f"ордер {r.get('id')}: state={r.get('state')} errorCode={r.get('errorCode')} "
                f"errorMessage={r.get('errorMessage')} swapHash={r.get('swapHash')}")

    if args.since_ts:
        report["сделки_с_момента"] = find_sell_tx(args.wallet, args.mint, args.since_ts, hel_key)
        report["expired_сейчас"] = expired_has(args.wallet, args.mint, dbot_key)
        log(f"сделки по минту с {args.since_ts}: {json.dumps(report['сделки_с_момента'], ensure_ascii=False)}")
        log(f"expired сейчас: {json.dumps(report['expired_сейчас'], ensure_ascii=False)}")

    if not args.confirm_live_sell:
        report["итог"] = "НАБЛЮДЕНИЕ: --confirm-live-sell не передан, продажа НЕ отправлена"
        log(report["итог"])
        print("\n=== РЕЗУЛЬТАТ ===\n" + json.dumps(report, ensure_ascii=False, indent=2))
        return

    prog = prog0
    exp_before = None
    prev = bal0["итог"]
    ladder = []
    for item in args.percents.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            a, b = item.split(":", 1)
            ladder.append((float(a), float(b)))
        else:
            ladder.append((float(item), args.max_slippage))
    for percent, slip in ladder:
        t0 = int(time.time())
        st, resp, ok, msg = sell(args.mint, wallet_id, percent, slip, args.retries, dbot_key)
        ids = ((resp.get("res") or {}).get("ids") or []) if isinstance(resp, dict) else []
        order_rows = wait_order(ids, dbot_key, timeout_s=max(args.wait_s, 45)) if ids else []
        for r in order_rows:
            log(f"ордер {r.get('id')}: state={r.get('state')} errorCode={r.get('errorCode')} "
                f"errorMessage={r.get('errorMessage')} swapHash={r.get('swapHash')}")
        time.sleep(5)
        bal = balance_three_ways(args.wallet, args.mint, hel_key)
        tx = find_sell_tx(args.wallet, args.mint, t0, hel_key)
        if prog is None:
            prog = mint_program(args.mint, hel_key)
            report["программа_минта"] = prog
            log(f"программа минта: {json.dumps(prog, ensure_ascii=False)}")
        exp_now = expired_has(args.wallet, args.mint, dbot_key)
        if exp_before is None:
            exp_before = exp_now
            report["expired_после_первой_попытки"] = exp_now
        step = {"sellPercent": percent, "maxSlippage": slip, "http": st,
                 "принято_DBot": ok, "сообщение": msg, "состояние_ордера": order_rows,
                 "сырой_ответ": json.loads(scrub(json.dumps(resp, ensure_ascii=False, default=str)))
                 if isinstance(resp, dict) else str(resp),
                 "баланс_после": bal, "баланс_упал": bal["итог"] < prev - 1e-12,
                 "транзакция": tx, "expired_после": exp_now}
        report["шаги"].append(step)
        log(f"БАЛАНС ПОСЛЕ {percent}: {json.dumps(bal, ensure_ascii=False)}")
        log(f"транзакция продажи: {json.dumps(tx, ensure_ascii=False)}")
        if step["баланс_упал"]:
            report["итог"] = f"продано на sellPercent={percent}"
            break
        log(f"баланс НЕ уменьшился ({prev} -> {bal['итог']}) -- иду к следующей ступени лестницы")
        prev = bal["итог"]
    else:
        report["итог"] = "ни одна ступень лестницы не уменьшила баланс"

    report["закончено_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print("\n=== РЕЗУЛЬТАТ ===\n" + json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
