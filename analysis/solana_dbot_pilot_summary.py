#!/usr/bin/env python3
"""Владелец, 2026-09-18 (после разбора nub): упростить разбор сделок
пилота DBot -- декодер новых DEX-программ (engine.decode_tx) НЕ
расширяем, для сделки на десяток в день и сводки раз в несколько часов
хватает того, что уже сверено на nub:

  - наша сделка: дельта SOL-баланса кошелька пилота до/после (buy и sell
    транзакции по отдельности) -- работает для ЛЮБОГО маршрута, не
    требует декодирования AMM-нот;
  - издержки сделки: разница между "валовым" (по факту SOL, без чаевых
    и сетевой комиссии -- swap_output_sell/swap_principal_buy) и
    "чистым" (по факту дельты кошелёк-к-кошельку) -- без разложения по
    ногам маршрута;
  - лидер: его сделка по тому же минту непосредственно перед нашей,
    слот/разница в блоках, первый вход или докупка -- по ЕГО факту
    баланса (pre/post token balance), не по classify() (которая жёстко
    привязана к его адресу и всё равно не даёт больше, чем прямая
    проверка баланса);
  - отказы копирования: причина -- с дашборда DBot (владелец), движение
    цены после -- минутные свечи GeckoTerminal (публичный API, реальный
    источник, без декодирования маршрута);
  - время удержания: разница block_time между нашими buy и sell.

/automation/swap_orders подтверждённо пуст при реальных сделках пилота
(см. предыдущий проход) -- по прямому указанию владельца время на это
больше не тратим, вся история сделок пилота идёт с цепи (сканирование
подписей кошелька), не через DBot.

Только чтение -- Solana RPC (Alchemy/публичный) + DBot GET (только для
счётчиков задачи, для контекста) + GeckoTerminal GET. Никаких вызовов на
покупку/продажу."""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal as D
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dbot_pilot_summary.json"

DBOT_HOSTS = ["https://api-bot-v1.dbotx.com", "https://servapi.dbotx.com"]
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
PILOT_WALLET = "E1qAJBmrJDhBvm2sV8kfMXFAmgzHuSRNRosPEgMkKiWS"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
ENTRY_SIZE_THRESHOLD_USD = D("500")
LOOKBACK_HOURS = float(os.environ.get("PILOT_LOOKBACK_HOURS", "24"))
GECKO_BASE = "https://api.geckoterminal.com/api/v2"

# Известные отказы копирования (владелец сообщает с дашборда DBot -- API
# со списком отказов не существует, подтверждено ранее). Добавлять сюда
# по мере поступления новых.
KNOWN_FAILURES = [
    {"mint": "G44sp7eRAPnRGSsuQqCLSDELoiJLTbgpP5WDXNngDQfe", "event_time": 1789749341,
     "reason": "Copy Buy Failed -- Jupiter simulation 0x1771/6001 SlippageToleranceExceeded (дашборд, 16:35)"},
]

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED_SECRET]")
    return text


# ---------- DBot REST (только для счётчиков задачи -- контекст, не источник истории сделок) ----------

def dbot_get(path: str, api_key: str, params: dict | None = None) -> dict:
    if any(c in api_key for c in ("\n", "\r")):
        return {"exception": "DBOT_API_KEY содержит перевод строки -- похоже на многострочную метку, не сырой ключ."}
    header_variants = [{"x-api-key": api_key}, {"token": api_key}]
    last: dict = {}
    for host in DBOT_HOSTS:
        for headers in header_variants:
            try:
                resp = requests.get(f"{host}{path}", params=params or {}, headers=headers, timeout=30)
            except Exception as exc:  # noqa: BLE001
                last = {"exception": _scrub_all(f"{type(exc).__name__}: {exc}")}
                continue
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = {"non_json_body": _scrub_all(resp.text[:500])}
            last = {"http_status": resp.status_code, "body": body, "host": host, "auth_tried": list(headers.keys())}
            if resp.status_code == 200:
                return last
    return last


# ---------- Solana RPC (Alchemy с фолбэком на публичный, тот же паттерн, что в pilot_report.py) ----------

_working_endpoint: tuple[str, str] | None = None
_last_call_at = 0.0
REQUEST_INTERVAL_S = 0.15


def rpc_endpoints() -> list[tuple[str, str]]:
    out = []
    key = os.environ.get("ALCHEMY_API_KEY", "")
    if key:
        out.append(("alchemy_solana", f"https://solana-mainnet.g.alchemy.com/v2/{key}"))
    out.append(("public_solana", PUBLIC_RPC))
    return out


def rpc_call(method: str, params: list, max_retries: int = 6) -> dict | None:
    global _working_endpoint, _last_call_at
    candidates = [_working_endpoint] if _working_endpoint else rpc_endpoints()
    last_exc = None
    for name, url in candidates:
        for attempt in range(max_retries):
            wait = REQUEST_INTERVAL_S - (time.monotonic() - _last_call_at)
            if wait > 0:
                time.sleep(wait)
            _last_call_at = time.monotonic()
            try:
                resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if resp.status_code in (401, 403):
                last_exc = RuntimeError(f"{name}: HTTP {resp.status_code}")
                break
            if not resp.ok:
                last_exc = RuntimeError(f"{name}: HTTP {resp.status_code} -- {resp.text[:200]}")
                time.sleep(2 * (attempt + 1))
                continue
            body = resp.json()
            if "error" in body:
                err = body["error"]
                if err.get("code") == 429 or "rate" in str(err.get("message", "")).lower():
                    time.sleep(3 * (attempt + 1))
                    continue
                raise RuntimeError(f"{name}: RPC error {err}")
            if _working_endpoint is None:
                _working_endpoint = (name, url)
                print(f"[pilot_summary] рабочий RPC-эндпоинт: {name}", flush=True)
            return body.get("result")
    raise RuntimeError(f"Все RPC-эндпоинты отказали: {last_exc}")


def get_signatures_for_address(address: str, before: str | None = None, limit: int = 1000) -> list[dict]:
    params = {"limit": limit}
    if before:
        params["before"] = before
    return rpc_call("getSignaturesForAddress", [address, params]) or []


def get_transaction(sig: str) -> dict | None:
    return rpc_call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])


# ---------- Балансовые дельты -- метод сверен на сделке nub (см. solana_dbot_trade1_analysis.py) ----------

def wallet_mint_deltas(tx: dict, wallet: str) -> dict:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return {"increased": [], "decreased": [], "deltas": {}}
    pre_tb, post_tb = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []

    def bals(rows):
        a: dict[str, D] = {}
        for b in rows:
            if b.get("owner") == wallet:
                amt = b["uiTokenAmount"]
                a[b["mint"]] = a.get(b["mint"], D(0)) + D(amt["amount"]) / D(10) ** amt["decimals"]
        return a

    pre, post = bals(pre_tb), bals(post_tb)
    delta = {k: post.get(k, D(0)) - pre.get(k, D(0)) for k in set(pre) | set(post)}
    return {
        "increased": [k for k, v in delta.items() if v > 0],
        "decreased": [k for k, v in delta.items() if v < 0],
        "deltas": {k: str(v) for k, v in delta.items()},
        "pre_balances": {k: str(v) for k, v in pre.items()},
    }


def wallet_sol_delta(tx: dict, wallet: str) -> dict | None:
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    idx = None
    for i, k in enumerate(keys):
        pk = k.get("pubkey") if isinstance(k, dict) else k
        if pk == wallet:
            idx = i
            break
    if idx is None:
        return None
    meta = tx.get("meta") or {}
    pre_list, post_list = meta.get("preBalances") or [], meta.get("postBalances") or []
    if idx >= len(pre_list) or idx >= len(post_list):
        return None
    pre, post = pre_list[idx], post_list[idx]
    return {"pre_lamports": pre, "post_lamports": post, "delta_lamports": post - pre, "delta_sol": (post - pre) / 1e9}


def tips_and_fee(tx: dict) -> dict:
    """Чаевые Astralane (System Program transfer ~0.00495/0.00995 SOL,
    top-level ИЛИ inner instruction) + сетевая комиссия. Без декодера
    AMM-нот -- этой сделке он не нужен (см. докстринг)."""
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    inner = (tx.get("meta") or {}).get("innerInstructions") or []
    tips = []
    for i in instrs:
        parsed = i.get("parsed") or {}
        if parsed.get("type") == "transfer" and i.get("program") == "system":
            info = parsed.get("info", {})
            lamports = info.get("lamports")
            if lamports and (abs(lamports - 4_950_000) < 50_000 or abs(lamports - 9_950_000) < 50_000):
                tips.append({"destination": info.get("destination"), "lamports": lamports, "sol": lamports / 1e9})
    for grp in inner:
        for ix in grp.get("instructions", []):
            parsed = ix.get("parsed") or {}
            if parsed.get("type") == "transfer" and ix.get("program") == "system":
                info = parsed.get("info", {})
                lamports = info.get("lamports")
                if lamports and (abs(lamports - 4_950_000) < 50_000 or abs(lamports - 9_950_000) < 50_000):
                    tips.append({"destination": info.get("destination"), "lamports": lamports, "sol": lamports / 1e9})
    meta = tx.get("meta") or {}
    return {"tips_found": tips, "tip_sol": sum(t["sol"] for t in tips), "network_fee_sol": meta.get("fee", 0) / 1e9}


# ---------- Сканирование сделок пилота (любой минт, без хардкода) ----------

def scan_pilot_trades(cutoff_time: int) -> list[dict]:
    events, before = [], None
    for _ in range(30):
        batch = get_signatures_for_address(PILOT_WALLET, before=before)
        if not batch:
            break
        stop = False
        for s in batch:
            bt = s.get("blockTime")
            if bt is None:
                continue
            if bt < cutoff_time:
                stop = True
                break
            if s.get("err") is not None:
                continue
            tx = get_transaction(s["signature"])
            if tx is None:
                continue
            deltas = wallet_mint_deltas(tx, PILOT_WALLET)
            for mint in deltas["increased"]:
                if mint in (USDC_MINT, SOL_MINT):
                    continue
                events.append({"signature": s["signature"], "block_time": bt, "slot": tx.get("slot"),
                                "mint": mint, "direction": "buy", "tx": tx})
            for mint in deltas["decreased"]:
                if mint in (USDC_MINT, SOL_MINT):
                    continue
                events.append({"signature": s["signature"], "block_time": bt, "slot": tx.get("slot"),
                                "mint": mint, "direction": "sell", "tx": tx})
        if stop:
            break
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break
    return events


def pair_trades(events: list[dict]) -> list[dict]:
    """FIFO-спаривание buy->sell по минту (позиции пилота короткие --
    таймер продажи ~29с -- наложений почти не бывает)."""
    by_mint: dict[str, list[dict]] = {}
    for e in events:
        by_mint.setdefault(e["mint"], []).append(e)
    pairs = []
    for mint, evs in by_mint.items():
        evs.sort(key=lambda e: e["block_time"])
        buys = [e for e in evs if e["direction"] == "buy"]
        sells = [e for e in evs if e["direction"] == "sell"]
        si = 0
        for b in buys:
            while si < len(sells) and sells[si]["block_time"] < b["block_time"]:
                si += 1
            if si < len(sells):
                pairs.append({"mint": mint, "buy": b, "sell": sells[si]})
                si += 1
            else:
                pairs.append({"mint": mint, "buy": b, "sell": None})
    return pairs


def trade_metrics(pair: dict) -> dict:
    mint = pair["mint"]
    b, s = pair["buy"], pair["sell"]
    b_delta = wallet_sol_delta(b["tx"], PILOT_WALLET)
    b_tips = tips_and_fee(b["tx"])
    tokens_bought = float(D(wallet_mint_deltas(b["tx"], PILOT_WALLET)["deltas"].get(mint, "0")))
    result: dict = {
        "mint": mint, "buy_signature": b["signature"], "buy_block_time": b["block_time"], "buy_slot": b["slot"],
        "tokens_bought": tokens_bought,
        "total_sol_out_buy": -b_delta["delta_sol"] if b_delta else None,
        "tip_buy_sol": b_tips["tip_sol"], "network_fee_buy_sol": b_tips["network_fee_sol"],
    }
    if s is None:
        result["status"] = "не продано в окне -- позиция открыта или sell вне LOOKBACK_HOURS"
        return result
    s_delta = wallet_sol_delta(s["tx"], PILOT_WALLET)
    s_tips = tips_and_fee(s["tx"])
    tokens_sold = -float(D(wallet_mint_deltas(s["tx"], PILOT_WALLET)["deltas"].get(mint, "0")))
    total_sol_in_sell = s_delta["delta_sol"] if s_delta else None
    result.update({
        "sell_signature": s["signature"], "sell_block_time": s["block_time"], "sell_slot": s["slot"],
        "tokens_sold": tokens_sold, "hold_seconds": s["block_time"] - b["block_time"],
        "total_sol_in_sell": total_sol_in_sell,
        "tip_sell_sol": s_tips["tip_sol"], "network_fee_sell_sol": s_tips["network_fee_sol"],
    })
    if result["total_sol_out_buy"] and total_sol_in_sell is not None:
        swap_principal_buy = result["total_sol_out_buy"] - result["network_fee_buy_sol"] - result["tip_buy_sol"]
        swap_output_sell = total_sol_in_sell + result["network_fee_sell_sol"] + result["tip_sell_sol"]
        gross_pct = (swap_output_sell / swap_principal_buy - 1) * 100 if swap_principal_buy else None
        net_pct = (total_sol_in_sell / result["total_sol_out_buy"] - 1) * 100
        result["gross_pct"] = gross_pct
        result["net_pct"] = net_pct
        result["cost_pct"] = (gross_pct - net_pct) if gross_pct is not None else None
        result["status"] = "ok"
    else:
        result["status"] = "дельта SOL-баланса недоступна"
    return result


# ---------- Лидер: сделка по тому же минту перед нашей, первый вход или докупка ----------

def leader_entry_before(mint: str, before_time: int) -> dict:
    """ATA лидера для этого минта -> история подписей ATA -> последняя
    покупка (рост баланса) на момент <= before_time. Честно возвращает
    status=no_token_account/no_purchase_found, если не нашли."""
    ata_result = rpc_call("getTokenAccountsByOwner", [LEADER_WALLET, {"mint": mint}, {"encoding": "jsonParsed"}])
    accounts = (ata_result or {}).get("value", [])
    if not accounts:
        return {"status": "no_token_account_for_leader"}
    for acc in accounts:
        ata = acc["pubkey"]
        sigs, before = [], None
        for _ in range(20):
            batch = rpc_call("getSignaturesForAddress", [ata, {"limit": 1000, "before": before}]) or []
            if not batch:
                break
            sigs.extend(batch)
            if batch[-1].get("blockTime") and batch[-1]["blockTime"] < before_time - 3600:
                break
            before = batch[-1]["signature"]
            if len(batch) < 1000:
                break
        candidates = [s for s in sigs if s.get("blockTime") and s["blockTime"] <= before_time and s.get("err") is None]
        candidates.sort(key=lambda s: s["blockTime"], reverse=True)
        for s in candidates[:5]:
            tx = get_transaction(s["signature"])
            if not tx:
                continue
            info = _extract_leader_buy(tx, mint)
            if info:
                info.update({"status": "ok", "signature": s["signature"], "slot": tx.get("slot"), "block_time": tx.get("blockTime")})
                return info
    return {"status": "no_purchase_found_in_recent_history"}


def _extract_leader_buy(tx: dict, mint: str) -> dict | None:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    deltas = wallet_mint_deltas(tx, LEADER_WALLET)
    if mint not in deltas["increased"]:
        return None
    pre_balances = deltas.get("pre_balances", {})
    zero_balance_before = D(pre_balances.get(mint, "0")) == 0
    paid_usdc = None
    usdc_delta = deltas["deltas"].get(USDC_MINT)
    if usdc_delta is not None and D(usdc_delta) < 0:
        paid_usdc = float(-D(usdc_delta))
    return {"tokens_received": float(D(deltas["deltas"][mint])), "paid_usdc": paid_usdc,
            "zero_balance_before": zero_balance_before}


# ---------- GeckoTerminal: движение цены после отказа (публичный API, без декодера маршрута) ----------

def gecko_get(path: str, params: dict | None = None) -> dict:
    try:
        r = requests.get(f"{GECKO_BASE}{path}", params=params or {}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return {"exception": f"{type(exc).__name__}: {exc}"}
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"non_json_body": r.text[:500]}
    return {"http_status": r.status_code, "body": body}


def gecko_find_pool(mint: str) -> str | None:
    r = gecko_get(f"/networks/solana/tokens/{mint}/pools")
    if r.get("http_status") != 200:
        return None
    data = (r.get("body") or {}).get("data") or []
    if not data:
        return None

    def liq(p):
        try:
            return float((p.get("attributes") or {}).get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            return 0.0

    data.sort(key=liq, reverse=True)
    return ((data[0].get("attributes") or {}).get("address"))


def gecko_price_after(mint: str, event_time: int) -> dict:
    pool = gecko_find_pool(mint)
    if not pool:
        return {"status": "no_pool_found_on_geckoterminal", "mint": mint}
    r = gecko_get(f"/networks/solana/pools/{pool}/ohlcv/minute",
                  params={"before_timestamp": event_time + 20 * 60, "limit": 40, "currency": "usd"})
    if r.get("http_status") != 200:
        return {"status": "gecko_ohlcv_http_error", "mint": mint, "pool": pool, "raw": r}
    candles = (((r.get("body") or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    if not candles:
        return {"status": "no_candles", "mint": mint, "pool": pool}
    candles = sorted(candles, key=lambda c: c[0])
    at_or_before = [c for c in candles if c[0] <= event_time]
    after = [c for c in candles if c[0] > event_time]
    if not at_or_before and not after:
        return {"status": "no_candles_near_event", "mint": mint, "pool": pool}
    ref_close = at_or_before[-1][4] if at_or_before else after[0][1]
    path = [{"ts": c[0], "minutes_after": round((c[0] - event_time) / 60, 1),
             "open": c[1], "high": c[2], "low": c[3], "close": c[4]} for c in after[:20]]
    result = {"status": "ok", "mint": mint, "pool": pool, "event_time": event_time, "ref_close_at_event": ref_close,
              "candles_after": path}
    if ref_close and path:
        result["max_close_pct_vs_event"] = max((c["close"] / ref_close - 1) * 100 for c in path)
        result["min_close_pct_vs_event"] = min((c["close"] / ref_close - 1) * 100 for c in path)
    return result


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "lookback_hours": LOOKBACK_HOURS}
    api_key = os.environ.get("DBOT_API_KEY", "")
    if api_key and not any(c in api_key for c in ("\n", "\r")):
        _ACTIVE_SECRETS.append(api_key)

    # Счётчики задачи -- только для контекста (сверка n сделок), НЕ источник истории.
    if api_key:
        r = dbot_get("/automation/follow_orders", api_key, params={"chain": "solana"})
        res = (r.get("body") or {}).get("res") or []
        if res:
            task = res[0]
            out["dbot_task_counters"] = {k: task.get(k) for k in
                                          ("buyTimes", "sellTimes", "boughtUsd", "soldUsd", "pnlOrderCount", "updateAt")}
        print(f"[pilot_summary] dbot_task_counters={out.get('dbot_task_counters')}", flush=True)

    cutoff = int(time.time()) - int(LOOKBACK_HOURS * 3600)
    events = scan_pilot_trades(cutoff)
    pairs = pair_trades(events)
    print(f"[pilot_summary] событий={len(events)} пар(buy[+sell])={len(pairs)} за последние {LOOKBACK_HOURS}ч", flush=True)

    trades = []
    for pair in pairs:
        m = trade_metrics(pair)
        leader = leader_entry_before(pair["mint"], pair["buy"]["block_time"])
        m["leader"] = leader
        if leader.get("status") == "ok":
            m["leader_slot_delta"] = (pair["buy"]["slot"] or 0) - (leader.get("slot") or 0)
            m["leader_seconds_delta"] = pair["buy"]["block_time"] - (leader.get("block_time") or 0)
            m["leader_entry_ge_500usd"] = (leader.get("paid_usdc") or 0) >= float(ENTRY_SIZE_THRESHOLD_USD)
        trades.append(m)
        print(f"[pilot_summary]   {pair['mint'][:8]}.. status={m['status']} "
              f"gross={m.get('gross_pct')} net={m.get('net_pct')} leader={leader.get('status')}", flush=True)

    out["trades"] = trades
    out["trades_leader_ge_500usd"] = [t for t in trades if t.get("leader_entry_ge_500usd") is True]
    out["trades_leader_lt_500usd"] = [t for t in trades if t.get("leader_entry_ge_500usd") is False]

    out["known_failures_aftermath"] = []
    for f in KNOWN_FAILURES:
        aftermath = gecko_price_after(f["mint"], f["event_time"])
        aftermath["reason"] = f["reason"]
        out["known_failures_aftermath"].append(aftermath)
        print(f"[pilot_summary] отказ {f['mint'][:8]}..: {aftermath.get('status')} "
              f"max={aftermath.get('max_close_pct_vs_event')} min={aftermath.get('min_close_pct_vs_event')}", flush=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
    print(f"[pilot_summary] Записано {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
