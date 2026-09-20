#!/usr/bin/env python3
"""Владелец: единый конвейер учёта сделок, заменяет все прошлые
промпты про статистику/леджер. RPC -- Helius, ключ ИЗ СЕКРЕТА HELIUS_API
в query-параметре (владелец подтвердил: секрет == рабочему ключу,
проверено HTTP 200 на getBalance) -- сам ключ в код не зашит.

A. DBot follow_orders (реальная форма ответа подтверждена в этой сессии
   ранее -- {"err":false,"res":[{...}]}, поля id/name/walletAddress/
   walletName/targetIds/targetNames -- см. data/dbot_tasks_config_raw.json)
   -- 0 кредитов, "источник намерений".
   follow_trades -- реальная форма ответа теперь подтверждена (первый
   полный прогон): подписи транзакции как отдельного поля НЕТ (как и
   предупреждала документация владельца, docs.dbotx.com/reference/
   copy-records), но она есть в links.etherscan -- Solscan-ссылка вида
   "https://solscan.io/tx/<подпись>" (имя поля общее/EVM-style, но URL
   реально ведёт на Solscan для сети solana). Источник (кого копировали)
   -- follow.wallet, прямо в записи, отдельный запрос с targetWallet не
   понадобился. Реальные поля: id, configId, configName, wallet,
   createAt (мс), timestamp (с, только у state=="done"), type (buy/sell),
   state (done/fail/...), errorCode/errorMessage/skipReason,
   send.info.contract + send.amount (raw), receive.info.contract +
   receive.amount (raw), dbotFeeRate/dbotFee, follow.wallet,
   follow.remark, links.etherscan. Первая сырая запись сохраняется
   отдельно в data/dbot_follow_trades_sample.json. Если подпись из
   links.etherscan не извлеклась -- запасная сшивка по (кошелёк, минт,
   тип buy/sell, |время цепочки - timestamp записи| <= 20с, ближайший по
   времени и по сумме) -- см. buy_match_method/sell_match_method
   ("signature"/"time_match") в каждой сделке.

B. Цепочка -- источник денег, баланс-метод (тот же принцип, что уже
   провалидирован в этой сессии multiple раз: preBalances/postBalances,
   preTokenBalances/postTokenBalances по owner=кошелёк, полный список
   ключей = static accountKeys + loadedAddresses). getTransaction --
   encoding=json (не jsonParsed, как просил владелец) -- значит подписантов
   получаем из message.header.numRequiredSignatures (первые N статических
   ключей), не из parsed-флагов.

C. Сшивка DBot<->цепочка по txHash=signature. Покупка/продажа одного
   минта в одной задаче определяется ЗНАКОМ дельты токена кошелька
   (не decode_tx/AMM-парсинг -- владелец явно просит баланс-метод здесь).

E. Контроль перед выдачей -- 3 пилотных сделки из
   data/solana_dbot_realized_ledger.json (SOL вход/выход), допуск 0.001."""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import time
from decimal import Decimal as D
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DBOT_HOST = "https://api-bot-v1.dbotx.com"
WSOL = "So11111111111111111111111111111111111111112"
DBOT_FEE_ADDRESS = "F7F8QYPCc3zDYNeAh4UUE2wJ7Mq1huid5MeMo7PPzsqB"
# Известные релеи чаевых -- те же, что уже установлены и использованы
# ранее в этой сессии (analysis/solana_fast_buyers_services.py).
KNOWN_RELAYS = {
    "DiTmWENJsHQdawVUUKnUXkconcpW4Jv52TnMWhkncF6t": "релей (наблюдался у AKBot)",
    "AsTRAEoyMofR3vUPpf9k68Gsfb6ymTZttEtsAbv8Bk4d": "релей (Astralane)",
    "astra9xWY93QyfG6yM8zwsKsRodscjQ2uU2HKNL5prk": "релей (Astralane)",
    "ste11p5x8tJ53H1NbNQsRBg1YNRd4GcVpxtDw8PBpmb": "релей (ste11)",
    "ste11eZvF5bebo6EVyGMJHV6LtPtx7e6TzNZtxPfXGy": "релей (ste11)",
    "LandX6RsjjnfaxfYAFTZX1TW9Cgfe9HFStj9nRDd5SK": "релей (LandX)",
}
RELAY_PREFIXES = ("astra", "AsTra", "ste11", "LandX")

FOLLOW_ORDERS_PATH = REPO_ROOT / "data" / "dbot_follow_orders_raw.json"
FOLLOW_TRADES_PATH = REPO_ROOT / "data" / "dbot_follow_trades_raw.json"
FOLLOW_TRADES_SAMPLE_PATH = REPO_ROOT / "data" / "dbot_follow_trades_sample.json"
CHAIN_CACHE_PATH = REPO_ROOT / "data" / "chain_tx_cache.json"
TRADES_ALL_PATH = REPO_ROOT / "data" / "solana_trades_all.json"
SOURCE_STATS_PATH = REPO_ROOT / "data" / "solana_source_stats.json"
TASK_STATS_PATH = REPO_ROOT / "data" / "solana_task_stats.json"
STATUS_PATH = REPO_ROOT / "data" / "ledger_status.json"
PILOT_LEDGER_PATH = REPO_ROOT / "data" / "solana_dbot_realized_ledger.json"

TIME_BUDGET_S = 50 * 60


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError):
            pass
    return default


def save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ---------- RPC: Helius, ключ из секрета в query ----------

def _rpc_url() -> str:
    key = os.environ.get("HELIUS_API", "")
    if not key:
        raise RuntimeError("HELIUS_API пуст в окружении")
    return f"https://mainnet.helius-rpc.com/?api-key={key}"


def rpc_call(method: str, params: list, retries: int = 20):
    backoff = 0.0
    for _ in range(retries):
        try:
            resp = requests.post(_rpc_url(), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
        except Exception:  # noqa: BLE001
            backoff = min(max(backoff * 2, 0.5), 30.0)
            time.sleep(backoff)
            continue
        if resp.status_code == 429:
            backoff = min(max(backoff * 2, 0.5), 30.0)
            time.sleep(backoff)
            continue
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"RPC error {method}: {body['error']}")
        backoff = max(backoff * 0.5, 0.0)
        return body.get("result")
    raise RuntimeError(f"{method} исчерпал попытки")


def rpc_batch(reqs: list[tuple[str, list]]) -> list:
    """Владелец: 'если батч-запросы отклоняются -- параллельные одиночные'."""
    if not reqs:
        return []
    body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(reqs)]
    try:
        resp = requests.post(_rpc_url(), json=body, timeout=60)
        if resp.ok:
            results = resp.json()
            if isinstance(results, list):
                by_id = {r.get("id"): r for r in results if isinstance(r, dict)}
                out, ok = [], True
                for i in range(len(reqs)):
                    r = by_id.get(i)
                    if r is None or "error" in r:
                        ok = False
                        break
                    out.append(r.get("result"))
                if ok and len(out) == len(reqs):
                    return out
    except Exception:  # noqa: BLE001
        pass
    out = [None] * len(reqs)
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(rpc_call, m, p): i for i, (m, p) in enumerate(reqs)}
        for fut in cf.as_completed(futs):
            i = futs[fut]
            try:
                out[i] = fut.result()
            except Exception:  # noqa: BLE001
                out[i] = None
    return out


def get_signatures_for_address(address: str, before: str | None = None, limit: int = 1000) -> list[dict]:
    opts: dict = {"limit": limit}
    if before:
        opts["before"] = before
    return rpc_call("getSignaturesForAddress", [address, opts]) or []


def get_balance_sol(address: str) -> float:
    lam = rpc_call("getBalance", [address])
    if isinstance(lam, dict):
        lam = lam.get("value")
    return (lam or 0) / 1e9


def get_token_holding(wallet: str, mint: str) -> float:
    res = rpc_call("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
    total = 0.0
    for acc in (res or {}).get("value") or []:
        try:
            ui = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
            total += float(ui or 0)
        except (KeyError, TypeError):
            continue
    return total


# ---------- DBot REST (0 кредитов) ----------

def dbot_get(path: str, params: dict, api_key: str):
    for _attempt in range(6):
        try:
            resp = requests.get(f"{DBOT_HOST}{path}", params=params, headers={"X-API-KEY": api_key}, timeout=30)
        except Exception:  # noqa: BLE001
            time.sleep(2)
            continue
        if resp.status_code == 429:
            time.sleep(3)
            continue
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"non_json_body": resp.text[:500]}
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


def fetch_follow_orders(api_key: str) -> tuple[list[dict], int | None]:
    status, body = dbot_get("/automation/follow_orders", {}, api_key)
    items = extract_items(body)
    tasks = []
    for row in items:
        task_id = row.get("id") or row.get("_id")
        target_ids = row.get("targetIds") or []
        target_names = row.get("targetNames") or []
        sources = [{"address": addr, "remark": (target_names[i] if i < len(target_names) else "") or None}
                   for i, addr in enumerate(target_ids)]
        tasks.append({"id": task_id, "name": row.get("name"), "wallet": row.get("walletAddress"),
                      "wallet_name": row.get("walletName"), "enabled": row.get("enabled"), "sources": sources})
    return tasks, status


def fetch_follow_trades_for_task(task_id: str, api_key: str, my_wallet: str | None = None) -> list[dict]:
    """Владелец: пагинация должна идти ДО пустой страницы, а не
    останавливаться раньше на "страница короче size" -- на живом,
    постоянно дописываемом наборе данных страница может отдать <size
    записей НЕ на самом деле дойдя до конца (сдвиг из-за параллельной
    записи новых сделок), и тогда следующая страница снова непустая --
    эмпирически подтверждено: 4 реальные сделки по источнику N_HtuY были
    пропущены именно так. myWallet -- дополнительный фильтр по кошельку
    задачи, если поддерживается API (сужает выборку, не должен вредить)."""
    out = []
    page = 1
    params = {"chain": "solana", "configId": task_id, "size": 20}
    if my_wallet:
        params["myWallet"] = my_wallet
    while True:
        status, body = dbot_get("/account/follow_trades", {**params, "page": page}, api_key)
        items = extract_items(body)
        if not items:
            break
        out.extend(items)
        page += 1
        if page > 500:
            break
    return out


def dbot_signature_from_record(r: dict) -> str | None:
    """Подписи как отдельного поля в follow_trades нет (подтверждено
    документацией и эмпирически). Она зашита в links.* как URL explorer'а --
    у links.etherscan это на деле Solscan-ссылка для solana."""
    links = r.get("links") or {}
    for key in ("etherscan", "dexscreener", "uniswap"):
        url = links.get(key)
        if url and "/tx/" in url:
            sig = url.rsplit("/tx/", 1)[-1].strip()
            if sig:
                return sig
    return None


def dbot_token_contract(r: dict) -> str | None:
    """Адрес НЕ-SOL токена сделки: buy -- получаемый токен, sell -- отдаваемый."""
    rtype = str(r.get("type") or "").lower()
    send_c = ((r.get("send") or {}).get("info") or {}).get("contract")
    recv_c = ((r.get("receive") or {}).get("info") or {}).get("contract")
    if rtype == "buy":
        return recv_c if recv_c and recv_c != WSOL else send_c
    if rtype == "sell":
        return send_c if send_c and send_c != WSOL else recv_c
    for c in (send_c, recv_c):
        if c and c != WSOL:
            return c
    return send_c or recv_c


def dbot_sol_amount(r: dict) -> float | None:
    """Сумма SOL-ноги сделки (send для buy, receive для sell) в SOL -- для
    сравнения по сумме при сшивке по времени (кандидатов несколько)."""
    rtype = str(r.get("type") or "").lower()
    try:
        if rtype == "buy":
            return float(((r.get("send") or {}).get("amount"))) / 1e9
        if rtype == "sell":
            return float(((r.get("receive") or {}).get("amount"))) / 1e9
    except (TypeError, ValueError):
        return None
    return None


def dbot_time_candidates(records: list[dict]) -> list[dict]:
    """Записи state=='done' без извлекаемой подписи -- кандидаты для запасной
    сшивки по времени (owner: если подписи нет -- сшивать по кошельку/минту/
    типу/времени +-20с)."""
    out = []
    for r in records:
        if str(r.get("state") or "").lower() != "done":
            continue
        if dbot_signature_from_record(r):
            continue
        if r.get("timestamp") is None or dbot_token_contract(r) is None:
            continue
        out.append(r)
    return out


def find_dbot_record(sig: str, block_time, mint: str, is_buy: bool, sol_amount,
                      dbot_by_sig: dict, time_candidates: list[dict], consumed_ids: set):
    """Сначала точная подпись; если её нет в DBot-записях -- запасная сшивка
    по (тот же минт, тот же тип buy/sell, |время цепочки - timestamp| <= 20с),
    при нескольких кандидатах -- ближайший по времени, затем по сумме SOL."""
    rec = dbot_by_sig.get(sig)
    if rec is not None:
        return rec, "signature"
    if block_time is None:
        return None, None
    want_type = "buy" if is_buy else "sell"
    candidates = []
    for r in time_candidates:
        rid = r.get("id") or r.get("_id")
        if rid is not None and rid in consumed_ids:
            continue
        if str(r.get("type") or "").lower() != want_type:
            continue
        if dbot_token_contract(r) != mint:
            continue
        dt = abs((r.get("timestamp") or 0) - block_time)
        if dt <= 20:
            candidates.append((dt, r))
    if not candidates:
        return None, None
    if sol_amount is not None:
        def amount_diff(item):
            ra = dbot_sol_amount(item[1])
            return abs((ra if ra is not None else 1e9) - sol_amount)
        candidates.sort(key=lambda item: (item[0], amount_diff(item)))
    else:
        candidates.sort(key=lambda item: item[0])
    best = candidates[0][1]
    rid = best.get("id") or best.get("_id")
    if rid is not None:
        consumed_ids.add(rid)
    return best, "time_match"


# ---------- Section B: цепочка (баланс-метод) ----------

def account_keys_and_signers(tx: dict) -> tuple[list[str], set[str]]:
    msg = tx["transaction"]["message"]
    static_keys = list(msg["accountKeys"])
    la = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys = static_keys + list(la.get("writable") or []) + list(la.get("readonly") or [])
    num_sig = msg.get("header", {}).get("numRequiredSignatures", 1)
    signers = set(static_keys[:num_sig])
    return keys, signers


def token_balance_map(tb_list, owner: str) -> dict:
    out: dict = {}
    for b in tb_list or []:
        if b.get("owner") != owner:
            continue
        amt = D(b["uiTokenAmount"]["amount"]) / D(10) ** b["uiTokenAmount"]["decimals"]
        out[b["mint"]] = out.get(b["mint"], D(0)) + amt
    return out


def classify_transfer_address(addr: str) -> str:
    if addr == DBOT_FEE_ADDRESS:
        return "комиссия DBot"
    if addr in KNOWN_RELAYS or any(addr.startswith(p) for p in RELAY_PREFIXES):
        return "чаевые"
    return "прочее"


def parse_tx_for_wallet(sig: str, tx: dict, wallet: str) -> dict:
    meta = tx.get("meta") or {}
    keys, signers = account_keys_and_signers(tx)
    if wallet not in keys:
        return {"signature": sig, "slot": tx.get("slot"), "blockTime": tx.get("blockTime"),
                "err": "wallet_not_in_keys"}
    idx = keys.index(wallet)
    pre_bal, post_bal = meta.get("preBalances") or [], meta.get("postBalances") or []
    pre_sol = pre_bal[idx] if idx < len(pre_bal) else None
    post_sol = post_bal[idx] if idx < len(post_bal) else None
    sol_delta = float((D(post_sol) - D(pre_sol)) / D(10 ** 9)) if pre_sol is not None and post_sol is not None else None

    pre_tb = token_balance_map(meta.get("preTokenBalances"), wallet)
    post_tb = token_balance_map(meta.get("postTokenBalances"), wallet)
    token_deltas = {}
    for m in set(pre_tb) | set(post_tb):
        d = post_tb.get(m, D(0)) - pre_tb.get(m, D(0))
        if d != 0:
            token_deltas[m] = float(d)
    wsol_delta = token_deltas.pop(WSOL, 0.0)

    transfers = []
    for i, key in enumerate(keys):
        if key in signers or key == wallet or i >= len(pre_bal) or i >= len(post_bal):
            continue
        delta = (post_bal[i] - pre_bal[i]) / 1e9
        if delta > 0:
            transfers.append({"recipient": key, "amount_sol": round(delta, 9), "tag": classify_transfer_address(key)})

    # Контрагент входящего SOL (для п.5 -- "внешние потоки"/пополнения):
    # если кошелёк получил SOL, ищем среди подписантов ЭТОЙ транзакции (не
    # самого кошелька) того, чей баланс упал сильнее всего -- это и есть
    # реальный отправитель/плательщик перевода.
    counterparty_in = None
    if sol_delta is not None and sol_delta > 0:
        best = None
        for i, key in enumerate(keys):
            if key == wallet or key not in signers or i >= len(pre_bal) or i >= len(post_bal):
                continue
            d = (post_bal[i] - pre_bal[i]) / 1e9
            if d < 0 and (best is None or d < best[1]):
                best = (key, d)
        if best:
            counterparty_in = best[0]

    return {
        "signature": sig, "slot": tx.get("slot"), "blockTime": tx.get("blockTime"), "err": meta.get("err"),
        "is_signer": wallet in signers,
        "sol_delta_native": sol_delta, "wsol_delta": float(wsol_delta), "token_deltas": token_deltas,
        "fee_sol": (meta.get("fee") or 0) / 1e9, "sol_transfers": transfers, "counterparty_in": counterparty_in,
    }


def _genesis_key(wallet: str) -> str:
    return f"__genesis_synced__:{wallet}"


def _oldest_cached_signature(cache: dict, wallet: str) -> str | None:
    rows = [v for v in cache.values() if isinstance(v, dict) and v.get("_wallet") == wallet and v.get("slot") is not None]
    if not rows:
        return None
    return min(rows, key=lambda v: v["slot"])["signature"]


def sync_wallet_chain(wallet: str, cache: dict, deadline: float) -> int:
    """Найдено при проверке выдачи: у нескольких кошельков (BATCH-1..4)
    первая закэшированная транзакция -- уже покупка, без транзакции
    пополнения перед ней -- значит первоначальный обход истории не дошёл
    до реального начала (бюджет времени кончился раньше), а старая логика
    ('встретили уже известную подпись -- считаем, что дальше в глубину всё
    известно') после этого НАВСЕГДА замораживала разрыв между этим местом
    и настоящим началом истории кошелька, потому что каждый следующий
    запуск снова упирался в тот же самый кэш на первой же странице.
    Теперь: проход 1 -- свежая активность с конца; проход 2, если геном
    ещё не подтверждён явным маркером -- докапываем назад от самой старой
    уже известной подписи, а не останавливаемся на первом совпадении."""
    todo = []

    before = None
    while time.monotonic() < deadline:
        page = get_signatures_for_address(wallet, before=before, limit=1000)
        if not page:
            cache[_genesis_key(wallet)] = {"_genesis_marker": True}
            break
        new_in_page = [h for h in page if h["signature"] not in cache]
        todo.extend(new_in_page)
        before = page[-1]["signature"]
        if len(new_in_page) < len(page):
            break  # дальше в глубину -- уже известная область (с прошлого раза)
        if len(page) < 1000:
            cache[_genesis_key(wallet)] = {"_genesis_marker": True}
            break

    if not cache.get(_genesis_key(wallet)):
        before = _oldest_cached_signature(cache, wallet)
        while before and time.monotonic() < deadline:
            page = get_signatures_for_address(wallet, before=before, limit=1000)
            if not page:
                cache[_genesis_key(wallet)] = {"_genesis_marker": True}
                break
            todo.extend(h for h in page if h["signature"] not in cache)
            before = page[-1]["signature"]
            if len(page) < 1000:
                cache[_genesis_key(wallet)] = {"_genesis_marker": True}
                break

    todo.reverse()
    n_new = 0
    chunk_size = 20
    for start in range(0, len(todo), chunk_size):
        if time.monotonic() > deadline:
            break
        chunk = [h for h in todo[start:start + chunk_size] if h["signature"] not in cache]
        if not chunk:
            continue
        ok_chunk = [h for h in chunk if h.get("err") is None]
        for h in chunk:
            if h.get("err") is not None:
                cache[h["signature"]] = {"signature": h["signature"], "slot": h.get("slot"),
                                          "blockTime": h.get("blockTime"), "err": h.get("err"), "_wallet": wallet}
        if ok_chunk:
            reqs = [("getTransaction", [h["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 0}]) for h in ok_chunk]
            results = rpc_batch(reqs)
            for h, tx in zip(ok_chunk, results):
                if tx is None:
                    continue
                parsed = parse_tx_for_wallet(h["signature"], tx, wallet)
                parsed["_wallet"] = wallet
                cache[h["signature"]] = parsed
                n_new += 1
    return n_new


def fetch_missing_tx(sig: str, wallet: str, cache: dict) -> None:
    if sig in cache:
        return
    tx = rpc_call("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0}])
    if tx is None:
        cache[sig] = {"signature": sig, "err": "getTransaction_null", "_wallet": wallet}
        return
    parsed = parse_tx_for_wallet(sig, tx, wallet)
    parsed["_wallet"] = wallet
    cache[sig] = parsed


# ---------- Section C+D: сшивка, сделки, статистика ----------

def classify_swap_leg(v: dict):
    """Владелец, п.1: сделка = ТОЛЬКО своп против SOL/WSOL -- покупка
    (SOL/WSOL ушли, токен пришёл) или продажа (токен ушёл, SOL/WSOL
    пришли). "SOL/WSOL ушли/пришли" -- совместная нога sol_delta_native +
    wsol_delta (своп может расплачиваться уже обёрнутым WSOL без изменения
    нативного баланса). Любая другая комбинация знаков (голая передача
    токена, побочная пыль без встречной SOL-ноги и т.п.) -- не сделка.
    Возвращает (минт, знак_дельта_токена, sol_leg) или None."""
    deltas = v.get("token_deltas") or {}
    if not deltas:
        return None
    sol_leg = (v.get("sol_delta_native") or 0) + (v.get("wsol_delta") or 0)
    if sol_leg < 0:
        candidates = {m: d for m, d in deltas.items() if d > 0}
    elif sol_leg > 0:
        candidates = {m: d for m, d in deltas.items() if d < 0}
    else:
        return None
    if not candidates:
        return None
    mint, delta = max(candidates.items(), key=lambda kv: abs(kv[1]))
    return mint, delta, sol_leg


def classify_external_flow(v: dict):
    """Владелец, доп. к п.5 (все кошельки задач): "внешний поток" -- нет
    свопа (нет изменения НЕ-WSOL токенов) и нет перевода на адрес комиссии
    DBot (это отдельная, уже учтённая статья). Входящий SOL/WSOL --
    пополнение, исходящий -- вывод. Возвращает (kind, amount_sol>0,
    контрагент|None) или None, если это не внешний поток."""
    if v.get("token_deltas"):
        return None
    sol_leg = (v.get("sol_delta_native") or 0) + (v.get("wsol_delta") or 0)
    if abs(sol_leg) < 1e-9:
        return None
    transfers = v.get("sol_transfers") or []
    if any(tr["recipient"] == DBOT_FEE_ADDRESS for tr in transfers):
        return None
    if sol_leg > 0:
        return "topup", sol_leg, v.get("counterparty_in")
    top = max(transfers, key=lambda tr: tr["amount_sol"]) if transfers else None
    return "withdrawal", -sol_leg, (top["recipient"] if top else None)


def build_trades_for_task(task: dict, records: list[dict], chain_cache: dict) -> list[dict]:
    wallet = task["wallet"]
    # is_signer=True обязательно: иначе в "сделки" попадают транзакции, которые
    # кошелёк вообще не инициировал (пассивная пыль/чужая транзакция,
    # затрагивающая его токен-аккаунт -- эмпирически найдено: несколько таких
    # tx с token_deltas>0 и sol_delta_native==0 давали фантомные "незакрытые
    # позиции" и путали сопоставление buy/sell для реальных сделок).
    wallet_tx = [v for v in chain_cache.values()
                 if v.get("_wallet") == wallet and not v.get("err") and v.get("is_signer")]
    by_mint: dict[str, list] = {}
    for v in wallet_tx:
        leg = classify_swap_leg(v)
        if leg is None:
            continue
        mint, delta, sol_leg = leg
        by_mint.setdefault(mint, []).append((v, delta, sol_leg))
    for mint in by_mint:
        by_mint[mint].sort(key=lambda t: (t[0].get("slot") or 0))

    dbot_by_sig: dict[str, dict] = {}
    for r in records:
        sig = dbot_signature_from_record(r)
        if sig:
            dbot_by_sig[sig] = r
    time_candidates = dbot_time_candidates(records)
    consumed_ids: set = set()

    def resolve_source(record: dict | None):
        if not record:
            return None
        follow = record.get("follow") or {}
        src_addr = follow.get("wallet")
        if not src_addr:
            return None
        known = next((s for s in task["sources"] if s["address"] == src_addr), None)
        return known or {"address": src_addr, "remark": follow.get("remark")}

    trades = []
    matched_sigs: set = set()
    matched_record_ids: set = set()
    for mint, txs in by_mint.items():
        buys = [t for t in txs if t[1] > 0]
        sells = [t for t in txs if t[1] < 0]
        sells_remaining = list(sells)
        for buy_v, buy_amt, buy_sol_leg in buys:
            matched_sigs.add(buy_v["signature"])
            sell_v, sell_sol_leg = None, None
            for i, (sv, sd, sl) in enumerate(sells_remaining):
                if (sv.get("slot") or 0) > (buy_v.get("slot") or 0):
                    sell_v, sell_sol_leg = sv, sl
                    del sells_remaining[i]
                    break

            sol_in = round(-buy_sol_leg, 9)
            # Владелец, п.2: источник -- ТОЛЬКО из записи DBot со state=done
            # (signature из links, источник -- follow.wallet). Нет такой
            # записи -> источник "неизвестен", но сделка всё равно "закрыта"
            # (продажа DBot никогда не бывает state=done в этих данных --
            # копирующий продавал сам, это не повод считать сделку неучтённой).
            buy_record, buy_method = find_dbot_record(
                buy_v["signature"], buy_v.get("blockTime"), mint, True, sol_in,
                dbot_by_sig, time_candidates, consumed_ids)
            if buy_record is not None:
                rid = buy_record.get("id") or buy_record.get("_id")
                if rid is not None:
                    matched_record_ids.add(rid)

            sell_record, sell_method = None, None
            sol_out = None
            if sell_v:
                matched_sigs.add(sell_v["signature"])
                sol_out = round(sell_sol_leg, 9)
                sell_record, sell_method = find_dbot_record(
                    sell_v["signature"], sell_v.get("blockTime"), mint, False, sol_out,
                    dbot_by_sig, time_candidates, consumed_ids)
                if sell_record is not None:
                    rid = sell_record.get("id") or sell_record.get("_id")
                    if rid is not None:
                        matched_record_ids.add(rid)

            source = resolve_source(buy_record)

            # Владелец: брутто -- НЕ из транзакции (в ней чаевые/приоритетные
            # сборы той же сделки смешаны со свопом, надёжно не разделить без
            # decode_tx -- уже проверено и не подтвердилось на примерах).
            # Брутто = то, что сама DBot записала как отправленное/полученное
            # (send.amount у buy-записи, receive.amount у sell-записи);
            # нетто (sol_in/sol_out выше) остаётся дельтой кошелька по цепочке
            # -- это и есть проверенное на пилоте значение, не трогаем.
            gross_sol_in = dbot_sol_amount(buy_record) if buy_record else None
            gross_sol_out = dbot_sol_amount(sell_record) if sell_record else None

            net_sol = round(buy_sol_leg + (sell_sol_leg or 0), 9)
            gross_pct = round((sol_out / sol_in - 1) * 100, 4) if (sell_v and sol_in) else None

            # Владелец, п.3: закрыта -- покупка+продажа обе найдены в цепочке
            # (независимо от того, есть ли запись DBot для продажи -- её и
            # не будет, автопродажи DBot тут не проходили); незакрыта --
            # токен ещё на балансе; "ручная продажа" -- токена на балансе
            # уже нет, но продажи в истории кошелька мы не нашли (передан/
            # продан вне зоны видимости этого метода).
            if sell_v:
                status = "закрыта"
            else:
                holding = get_token_holding(wallet, mint)
                status = "незакрыта" if holding > 0 else "ручная продажа"

            trades.append({
                "task_id": task["id"], "task_name": task.get("name"), "wallet": wallet,
                "wallet_name": task.get("wallet_name"),
                "source_address": source["address"] if source else None,
                "source_remark": source["remark"] if source else None,
                "mint": mint,
                "buy_signature": buy_v["signature"], "buy_block_time": buy_v.get("blockTime"),
                "buy_match_method": buy_method,
                "sell_signature": sell_v["signature"] if sell_v else None,
                "sell_block_time": sell_v.get("blockTime") if sell_v else None,
                "sell_match_method": sell_method,
                "sol_in": sol_in, "sol_out": sol_out, "gross_pct": gross_pct, "net_sol": net_sol,
                "gross_sol_in": gross_sol_in, "gross_sol_in_source": "dbot_record" if buy_record else None,
                "gross_sol_out": gross_sol_out, "gross_sol_out_source": "dbot_record" if sell_record else None,
                "status": status,
                "dbot_fail_reason": None,
            })

    for r in records:
        rid = r.get("id") or r.get("_id")
        sig = dbot_signature_from_record(r)
        state = str(r.get("state") or "").lower()
        if sig and sig in matched_sigs:
            continue
        if rid is not None and rid in matched_record_ids:
            continue
        if state == "done":
            continue  # реально прошла, но не нашлась в chain-кэше в этом проходе -- честно не учитываем
        # Владелец, п.1: fail/skip/expired -- НЕ сделки, только счётчик
        # срывов ИСТОЧНИКА с причиной -- источник берём прямо из записи
        # (follow.wallet), сшивка по signature/времени тут не нужна: сама
        # запись уже точно говорит, кого пытались скопировать.
        source = resolve_source(r)
        trades.append({
            "task_id": task["id"], "task_name": task.get("name"), "wallet": wallet,
            "wallet_name": task.get("wallet_name"),
            "source_address": source["address"] if source else None,
            "source_remark": source["remark"] if source else None,
            "mint": dbot_token_contract(r),
            "buy_signature": sig, "status": "срыв",
            "dbot_fail_reason": r.get("errorMessage") or r.get("skipReason") or r.get("errorCode"),
        })
    return trades


def build_source_stats(trades_all: list[dict]) -> list[dict]:
    by_key: dict = {}
    for t in trades_all:
        if not t.get("source_address"):
            continue
        key = (t["task_id"], t["source_address"])
        by_key.setdefault(key, {"task_id": t["task_id"], "task_name": t.get("task_name"),
                                 "source_address": t["source_address"], "source_remark": t.get("source_remark"),
                                 "trades": []})
        by_key[key]["trades"].append(t)
    out = []
    for (task_id, addr), row in by_key.items():
        trades = row["trades"]
        closed = [t for t in trades if t["status"] == "закрыта" and t.get("gross_pct") is not None]
        n_profit = sum(1 for t in closed if t["gross_pct"] > 0)
        # Брутто (владелец, п.1) -- строго из записи DBot, не из транзакции;
        # доступно только когда есть done-запись на покупку (продажа DBot
        # тут никогда не завершается -- gross_sol_out практически всегда
        # неизвестен, честно не считаем его как ноль).
        gross_sum = sum(t["gross_sol_in"] for t in trades if t.get("gross_sol_in"))
        net_sum = sum(t["net_sol"] for t in trades if t.get("net_sol") is not None)
        best_pct = max((t["gross_pct"] for t in closed), default=None)
        fails = [t for t in trades if t["status"].startswith("срыв")]
        last_bt = max((t.get("buy_block_time") or 0 for t in trades), default=0)
        out.append({
            "task_id": task_id, "task_name": row["task_name"], "source_address": addr,
            "source_remark": row["source_remark"], "n_trades": len(trades), "n_profit": n_profit,
            "gross_sol_sum": round(gross_sum, 6), "net_sol_sum": round(net_sum, 6),
            "best_trade_pct": best_pct, "n_fails": len(fails),
            "fail_reasons": [f.get("dbot_fail_reason") for f in fails][:10],
            "last_trade_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_bt)) if last_bt else None,
        })
    out.sort(key=lambda r: r["net_sol_sum"], reverse=True)
    return out


def build_task_stats(tasks: list[dict], trades_all: list[dict], chain_cache: dict) -> list[dict]:
    out = []
    for task in tasks:
        wallet = task["wallet"]
        task_trades = [t for t in trades_all if t["task_id"] == task["id"]]
        n_trades = len(task_trades)
        net_sum = sum(t["net_sol"] for t in task_trades if t.get("net_sol") is not None)
        n_open = sum(1 for t in task_trades if t["status"] == "незакрыта")

        wallet_tx = [v for v in chain_cache.values() if isinstance(v, dict) and v.get("_wallet") == wallet and not v.get("err")]
        dbot_fee_total = sum(tr["amount_sol"] for v in wallet_tx for tr in (v.get("sol_transfers") or [])
                              if tr["tag"] == "комиссия DBot")

        # Владелец, доп. к п.5: "внешние потоки" (не своп, не комиссия DBot)
        # -- входящие пополнения и исходящие выводы, с подписью/адресом/
        # суммой каждого, чтобы можно было явно проверить, куда/откуда шли
        # деньги, а не просто верить агрегату.
        # Владелец, п.3: пополнение без опознанного контрагента-плательщика --
        # ОТДЕЛЬНАЯ строка "неопознанное поступление", в topups_sol/сверку не
        # входит, пока владелец не подтвердит его происхождение (эмпирически
        # именно так BATCH-4 сходится в ноль: без него сверка точная).
        topups, withdrawals, unidentified_deposits = [], [], []
        for v in wallet_tx:
            flow = classify_external_flow(v)
            if flow is None:
                continue
            kind, amount, counterparty = flow
            entry = {"signature": v.get("signature"), "counterparty": counterparty, "amount_sol": round(amount, 9)}
            if kind == "topup" and counterparty is None:
                unidentified_deposits.append(entry)
            elif kind == "topup":
                topups.append(entry)
            else:
                withdrawals.append(entry)
        topups_sol = sum(e["amount_sol"] for e in topups)
        withdrawals_sol = sum(e["amount_sol"] for e in withdrawals)
        unidentified_deposits_sol = sum(e["amount_sol"] for e in unidentified_deposits)

        try:
            current_balance = get_balance_sol(wallet)
        except Exception:  # noqa: BLE001
            current_balance = None

        # Сверка: баланс - пополнения(опознанные) + выводы == нетто сделок - комиссия DBot.
        # Неопознанные поступления сознательно НЕ входят в формулу.
        recon_left = (current_balance - topups_sol + withdrawals_sol) if current_balance is not None else None
        recon_right = net_sum - dbot_fee_total
        diff = abs(recon_left - recon_right) if recon_left is not None else None

        out.append({
            "task_id": task["id"], "task_name": task.get("name"), "wallet": wallet,
            "wallet_name": task.get("wallet_name"), "n_trades": n_trades, "net_sol_sum": round(net_sum, 6),
            "dbot_fee_total_sol": round(dbot_fee_total, 6), "n_open": n_open,
            "current_balance_sol": round(current_balance, 6) if current_balance is not None else None,
            "topups_sol": round(topups_sol, 6), "topups": topups,
            "withdrawals_sol": round(withdrawals_sol, 6), "withdrawals": withdrawals,
            "unidentified_deposits_sol": round(unidentified_deposits_sol, 6),
            "unidentified_deposits": unidentified_deposits,
            "reconciliation_left_balance_minus_topups_plus_withdrawals": round(recon_left, 6) if recon_left is not None else None,
            "reconciliation_right_net_minus_fee": round(recon_right, 6),
            "reconciliation_diff_sol": round(diff, 6) if diff is not None else None,
            "reconciliation_flag": bool(diff is not None and diff > 0.01),
        })

    # Владелец: если адрес пополнения/вывода повторяется у нескольких РАЗНЫХ
    # кошельков задач -- это, скорее всего, общий кошелёк владельца, а не
    # случайный сторонний адрес; помечаем каждую такую запись явно.
    addr_wallets: dict[str, set] = {}
    for row in out:
        for e in row["topups"] + row["withdrawals"]:
            if e["counterparty"]:
                addr_wallets.setdefault(e["counterparty"], set()).add(row["wallet"])
    owner_addrs = {a for a, ws in addr_wallets.items() if len(ws) >= 2}
    for row in out:
        for e in row["topups"] + row["withdrawals"]:
            e["note"] = "кошелёк владельца" if e["counterparty"] in owner_addrs else None
    return out


# ---------- E: контроль перед выдачей ----------

def validate_against_pilot(trades_all: list[dict]) -> dict:
    if not PILOT_LEDGER_PATH.exists():
        return {"skipped": True, "reason": "нет data/solana_dbot_realized_ledger.json"}
    pilot = json.loads(PILOT_LEDGER_PATH.read_text())
    pilot_trades = [t for t in pilot.get("trades", []) if t.get("label") == "pilot"][:3]
    by_buy_sig = {t["buy_signature"]: t for t in trades_all}
    checks = []
    all_ok = True
    for pt in pilot_trades:
        new = by_buy_sig.get(pt["buy_signature"])
        if new is None:
            all_ok = False
            checks.append({"buy_signature": pt["buy_signature"], "found": False})
            continue
        d_in = abs((new.get("sol_in") or 0) - pt["total_sol_out_buy"])
        d_out = abs((new.get("sol_out") or 0) - pt["total_sol_in_sell"]) if new.get("sol_out") is not None else None
        ok = d_in < 0.001 and (d_out is not None and d_out < 0.001)
        all_ok = all_ok and ok
        checks.append({"buy_signature": pt["buy_signature"], "found": True,
                        "expected_sol_in": pt["total_sol_out_buy"], "got_sol_in": new.get("sol_in"), "diff_in": d_in,
                        "expected_sol_out": pt["total_sol_in_sell"], "got_sol_out": new.get("sol_out"), "diff_out": d_out,
                        "ok": ok})
    return {"all_ok": all_ok, "n_checked": len(checks), "checks": checks}


# ---------- main ----------

def main() -> None:
    started = time.monotonic()
    deadline = started + TIME_BUDGET_S
    status = {"updated_utc": now_utc(), "n_records_dbot": 0, "n_tx_chain": 0, "n_trades": 0,
              "n_open": 0, "n_unmatched": 0, "last_error": None}

    api_key = os.environ.get("DBOT_API_KEY", "")
    if not api_key:
        status["last_error"] = "DBOT_API_KEY пуст в окружении -- секция A невозможна"
        save_json(STATUS_PATH, status)
        print("[ledger] " + status["last_error"], flush=True)
        return

    print("[ledger] A: follow_orders...", flush=True)
    tasks, http_status = fetch_follow_orders(api_key)
    save_json(FOLLOW_ORDERS_PATH, {"fetched_utc": now_utc(), "http_status": http_status, "tasks": tasks})
    print(f"[ledger] задач найдено: {len(tasks)}", flush=True)

    follow_trades_cache = load_json(FOLLOW_TRADES_PATH, {})
    follow_trades_by_task: dict[str, list] = {}
    n_records_total = 0
    for task in tasks:
        tid = task["id"]
        print(f"[ledger] A: follow_trades для задачи {tid} ({task.get('name')})...", flush=True)
        records = fetch_follow_trades_for_task(tid, api_key, my_wallet=task.get("wallet"))
        follow_trades_by_task[tid] = records
        if records and not FOLLOW_TRADES_SAMPLE_PATH.exists():
            save_json(FOLLOW_TRADES_SAMPLE_PATH, records[0])
            print(f"[ledger] первая сырая запись follow_trades сохранена в {FOLLOW_TRADES_SAMPLE_PATH.name}", flush=True)
        for r in records:
            rid = r.get("id") or r.get("_id") or f"{tid}:{len(follow_trades_cache)}"
            follow_trades_cache[str(rid)] = {"task_id": tid, "record": r}
        n_records_total += len(records)
    save_json(FOLLOW_TRADES_PATH, follow_trades_cache)
    status["n_records_dbot"] = n_records_total
    print(f"[ledger] всего DBot-записей: {n_records_total}", flush=True)

    print("[ledger] B: синхронизация цепочки по кошелькам задач...", flush=True)
    chain_cache = load_json(CHAIN_CACHE_PATH, {})
    for task in tasks:
        if time.monotonic() > deadline:
            print("[ledger] B: бюджет времени исчерпан, продолжим со следующего запуска", flush=True)
            break
        n_new = sync_wallet_chain(task["wallet"], chain_cache, deadline)
        print(f"[ledger] {task.get('name')} ({task['wallet'][:10]}..): +{n_new} новых транзакций, "
              f"всего в кэше {sum(1 for v in chain_cache.values() if v.get('_wallet') == task['wallet'])}", flush=True)
        save_json(CHAIN_CACHE_PATH, chain_cache)
    status["n_tx_chain"] = len(chain_cache)

    print("[ledger] C: дотягиваю done-записи DBot без транзакции в кэше...", flush=True)
    for task in tasks:
        for r in follow_trades_by_task.get(task["id"], []):
            state = str(r.get("state") or "").lower()
            h = dbot_signature_from_record(r)
            if state == "done" and h and h not in chain_cache:
                fetch_missing_tx(h, task["wallet"], chain_cache)
    save_json(CHAIN_CACHE_PATH, chain_cache)
    status["n_tx_chain"] = len(chain_cache)

    print("[ledger] C+D: строю сделки...", flush=True)
    trades_all = []
    for task in tasks:
        trades_all.extend(build_trades_for_task(task, follow_trades_by_task.get(task["id"], []), chain_cache))
    save_json(TRADES_ALL_PATH, trades_all)
    status["n_trades"] = len(trades_all)
    status["n_open"] = sum(1 for t in trades_all if t["status"] == "незакрыта")
    status["n_unmatched"] = sum(1 for t in trades_all if t["status"].startswith("срыв"))

    print("[ledger] E: контроль против пилотных сделок...", flush=True)
    validation = validate_against_pilot(trades_all)
    if not validation.get("skipped") and not validation.get("all_ok"):
        status["last_error"] = "КОНТРОЛЬ НЕ ПРОШЁЛ: " + json.dumps(validation, ensure_ascii=False, default=str)[:2000]
        save_json(STATUS_PATH, status)
        print("[ledger] СТОП: " + status["last_error"], flush=True)
        return

    print("[ledger] D: статистика по источникам и задачам...", flush=True)
    source_stats = build_source_stats(trades_all)
    save_json(SOURCE_STATS_PATH, source_stats)
    task_stats = build_task_stats(tasks, trades_all, chain_cache)
    save_json(TASK_STATS_PATH, task_stats)

    status["validation"] = validation
    save_json(STATUS_PATH, status)
    print(f"[ledger] ГОТОВО: сделок={status['n_trades']} открыто={status['n_open']} "
          f"срывов={status['n_unmatched']} контроль_ок={validation.get('all_ok')}", flush=True)


if __name__ == "__main__":
    main()
