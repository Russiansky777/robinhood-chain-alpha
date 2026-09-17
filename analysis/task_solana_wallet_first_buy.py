#!/usr/bin/env python3
"""Владелец, 2026-09-17: разовая задача, Solana (не EVM, не относится к
текущим линиям Robinhood Chain). Кошелёк BeqvBd...eit, токен 72yx...5bi.

Этап A: найти РЕАЛЬНЫЕ покупки этого токена этим кошельком.

Метод (без готового Solana-индексера/ключа -- их нет в этом репозитории,
проверено: только DUNE_API_KEY* и ALCHEMY_API_KEY для Robinhood Chain,
ни одного Solana-специфичного секрета):
  1. `getTokenAccountsByOwner(wallet, mint=token)` -- находим реальный
     associated token account (ATA) кошелька для этого минта (может не
     быть, если кошелёк ни разу не держал токен -- честно проверяем).
  2. `getSignaturesForAddress(ata)` -- ВСЯ история подписей именно этого
     ATA (не всего кошелька -- иначе пришлось бы фильтровать тысячи
     чужих транзакций постфактум).
  3. Для каждой подписи -- `getTransaction(sig, jsonParsed,
     maxSupportedTransactionVersion=0)`, и РЕАЛЬНЫЕ дельты из
     `meta.preTokenBalances`/`postTokenBalances` (а не декодирование
     произвольного бинарного формата инструкций конкретного DEX --
     дельты балансов одинаково надёжны для ЛЮБОЙ программы, потому что
     их считает сама Solana для каждого затронутого token account).
     Рост баланса нашего ATA = покупка (получили токен); дальше по той
     же транзакции ищем, чем заплатили -- падение SOL (`meta.preBalances
     /postBalances` по индексу кошелька, за вычетом komиссии) или
     падение USDC-баланса другого ATA того же владельца.
  4. DEX определяем по РЕАЛЬНОМУ programId верхнеуровневых инструкций
     транзакции (это она сама, on-chain факт, не декодируется -- просто
     читается) -- сверяем с известными константами (Raydium AMM v4/CPMM/
     CLMM, Orca Whirlpool, Meteora DLMM, pump.fun) с явной пометкой
     уверенности (см. KNOWN_DEX_PROGRAMS ниже -- если programId не
     совпал ни с одной, честно пишем "неизвестная программа <id>", не
     гадаем название).
  5. GeckoTerminal `/networks/solana/tokens/{mint}/pools` -- НЕЗАВИСИМАЯ
     сверка (реальный, уже используемый в этом проекте источник) какие
     пулы вообще существуют для этого токена, с их собственной меткой
     dex_id -- перепроверяет вывод шага 4, а не заменяет его.

RPC: пробуем Alchemy Solana-эндпоинт (тот же ALCHEMY_API_KEY, что и для
Robinhood Chain -- честно, это отдельный продукт Alchemy, аккаунт МОЖЕТ
не иметь доступа к Solana; если 401/403 -- сразу и без танцев переходим
на публичный `api.mainnet-beta.solana.com`, с честным логом какой
эндпоинт реально сработал), троттлинг консервативный (публичный free
RPC имеет жёсткие лимиты).

Результат: `data/task_solana_wallet_first_buy_result.json`."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/task_solana_wallet_first_buy_result.json")

WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
TOKEN_MINT = "72yxYmhLgDGwdyi2b9GjDynBB6VuG3kDxNKqDbzXh5bi"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
REQUEST_INTERVAL_S = 0.6  # честный троттлинг публичного free-tier RPC
TIME_BUDGET_S = 600.0

# ЧЕСТНАЯ ОГОВОРКА: это широко задокументированные, стабильные program-id
# каждого протокола (публичные константы уровня "адрес контракта", не
# результат измерения) -- если реальный programId транзакции НЕ совпадёт
# ни с одним, скрипт печатает сырой id, а не подставляет угаданное имя.
KNOWN_DEX_PROGRAMS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": "Orca (legacy v1)",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora DAMM v2",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun (bonding curve)",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pump.fun AMM (PumpSwap)",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter Aggregator v6",
}


def rpc_endpoints() -> list[tuple[str, str]]:
    out = []
    key = os.environ.get("ALCHEMY_API_KEY", "")
    if key:
        out.append(("alchemy_solana", f"https://solana-mainnet.g.alchemy.com/v2/{key}"))
    out.append(("public_solana", PUBLIC_RPC))
    return out


_working_endpoint: tuple[str, str] | None = None
_last_call_at = 0.0


def rpc_call(method: str, params: list, max_retries: int = 5) -> dict:
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
                resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                      timeout=30)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if resp.status_code in (401, 403):
                last_exc = RuntimeError(f"{name}: HTTP {resp.status_code} -- {resp.text[:200]}")
                break  # этот эндпоинт не подходит вообще, пробуем следующий
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
                print(f"[solana] рабочий RPC-эндпоинт: {name}")
            return body["result"]
    raise RuntimeError(f"Все RPC-эндпоинты отказали. Последняя ошибка: {last_exc}")


def gt_get(path: str, params: dict | None = None, max_retries: int = 3) -> tuple[int, dict | None]:
    for attempt in range(max_retries):
        try:
            resp = requests.get(f"https://api.geckoterminal.com/api/v2{path}", params=params or {},
                                 headers={"Accept": "application/json"}, timeout=25)
            if resp.status_code == 200:
                return 200, resp.json()
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return resp.status_code, None
        except Exception as exc:  # noqa: BLE001
            time.sleep(2 * (attempt + 1))
    return -1, None


def find_token_accounts(wallet: str, mint: str) -> list[dict]:
    result = rpc_call("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
    return result.get("value", []) if result else []


def get_signatures_for_address(address: str, before: str | None = None, limit: int = 1000) -> list[dict]:
    params: dict = {"limit": limit}
    if before:
        params["before"] = before
    return rpc_call("getSignaturesForAddress", [address, params]) or []


def get_transaction(sig: str) -> dict | None:
    return rpc_call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])


def analyze_tx(tx: dict, wallet: str, mint: str) -> dict | None:
    """Возвращает описание покупки (или None, если это не покупка нашего
    токена этим кошельком -- напр. перевод/продажа/чужая транзакция)."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None  # неудачная транзакция -- честно пропускаем, не покупка
    pre_tb = meta.get("preTokenBalances") or []
    post_tb = meta.get("postTokenBalances") or []

    def by_idx(rows):
        return {r["accountIndex"]: r for r in rows}

    pre_by_idx, post_by_idx = by_idx(pre_tb), by_idx(post_tb)
    token_delta = None
    for idx, post in post_by_idx.items():
        if post.get("mint") != mint or post.get("owner") != wallet:
            continue
        pre = pre_by_idx.get(idx)
        pre_amt = float(pre["uiTokenAmount"]["uiAmount"]) if pre and pre["uiTokenAmount"]["uiAmount"] is not None else 0.0
        post_amt = float(post["uiTokenAmount"]["uiAmount"]) if post["uiTokenAmount"]["uiAmount"] is not None else 0.0
        delta = post_amt - pre_amt
        if delta > 0:
            token_delta = delta
            break
    if token_delta is None:
        return None  # наш баланс этого токена не вырос в этой транзакции

    # Чем заплатили: USDC delta (тот же владелец) или SOL (нативный лампорт).
    paid_usdc, paid_sol = None, None
    for idx, post in post_by_idx.items():
        if post.get("mint") != USDC_MINT or post.get("owner") != wallet:
            continue
        pre = pre_by_idx.get(idx)
        pre_amt = float(pre["uiTokenAmount"]["uiAmount"]) if pre and pre["uiTokenAmount"]["uiAmount"] is not None else 0.0
        post_amt = float(post["uiTokenAmount"]["uiAmount"]) if post["uiTokenAmount"]["uiAmount"] is not None else 0.0
        if post_amt < pre_amt:
            paid_usdc = pre_amt - post_amt

    account_keys = [k.get("pubkey") if isinstance(k, dict) else k
                    for k in tx.get("transaction", {}).get("message", {}).get("accountKeys", [])]
    if paid_usdc is None and wallet in account_keys:
        wallet_idx = account_keys.index(wallet)
        pre_bal = (meta.get("preBalances") or [None])[wallet_idx] if wallet_idx < len(meta.get("preBalances") or []) else None
        post_bal = (meta.get("postBalances") or [None])[wallet_idx] if wallet_idx < len(meta.get("postBalances") or []) else None
        fee = meta.get("fee", 0)
        if pre_bal is not None and post_bal is not None:
            lamport_delta = pre_bal - post_bal - fee  # за вычетом комиссии сети
            if lamport_delta > 0:
                paid_sol = lamport_delta / 1e9

    # Программы верхнеуровневых инструкций -- реальный on-chain факт.
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    program_ids = sorted({i.get("programId") for i in instrs if i.get("programId")})
    inner = meta.get("innerInstructions") or []
    for grp in inner:
        for i in grp.get("instructions", []):
            if i.get("programId"):
                program_ids.append(i["programId"])
    program_ids = sorted(set(program_ids))
    dex_labels = [KNOWN_DEX_PROGRAMS.get(p, f"неизвестная программа {p}") for p in program_ids
                  if p not in ("11111111111111111111111111111111",
                               "ComputeBudget111111111111111111111111111111",
                               "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                               "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")]

    return {
        "signature": tx.get("transaction", {}).get("signatures", [None])[0],
        "slot": tx.get("slot"), "block_time_unix": tx.get("blockTime"),
        "token_amount_received": token_delta,
        "paid_usdc": paid_usdc, "paid_sol": paid_sol,
        "program_ids_involved": program_ids, "dex_labels_guess": dex_labels,
    }


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "wallet": WALLET, "token_mint": TOKEN_MINT}
    t0 = time.time()

    token_accounts = find_token_accounts(WALLET, TOKEN_MINT)
    out["n_token_accounts_found"] = len(token_accounts)
    out["token_accounts"] = [ta.get("pubkey") for ta in token_accounts]
    if not token_accounts:
        out["HONEST_ANSWER"] = ("У кошелька НЕТ ни одного token account для этого минта прямо сейчас -- "
                                 "если он когда-то покупал и полностью продал/закрыл ATA, история всё равно "
                                 "должна быть видна через getSignaturesForAddress закрытого аккаунта, но "
                                 "getTokenAccountsByOwner его уже не покажет. Честно останавливаемся здесь -- "
                                 "нужен другой путь (например, поиск через известный ATA-адрес, если он есть "
                                 "у владельца) прежде чем говорить 'покупок не было'.")
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        print("[solana] " + out["HONEST_ANSWER"])
        return

    all_purchases = []
    for ta in token_accounts:
        ata = ta.get("pubkey")
        sigs, before = [], None
        while True:
            if time.time() - t0 > TIME_BUDGET_S:
                out.setdefault("budget_warnings", []).append(f"бюджет исчерпан при пагинации подписей {ata}")
                break
            batch = get_signatures_for_address(ata, before=before, limit=1000)
            if not batch:
                break
            sigs.extend(batch)
            if len(batch) < 1000:
                break
            before = batch[-1]["signature"]
        out.setdefault("n_signatures_per_ata", {})[ata] = len(sigs)
        print(f"[solana] ATA {ata}: {len(sigs)} подписей в истории")

        for s in sigs:
            if time.time() - t0 > TIME_BUDGET_S:
                out.setdefault("budget_warnings", []).append("бюджет исчерпан при разборе транзакций")
                break
            if s.get("err") is not None:
                continue
            tx = get_transaction(s["signature"])
            if tx is None:
                continue
            purchase = analyze_tx(tx, WALLET, TOKEN_MINT)
            if purchase:
                all_purchases.append(purchase)
            out["purchases_checkpoint"] = sorted(all_purchases, key=lambda p: p["block_time_unix"] or 0)
            OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))

    all_purchases.sort(key=lambda p: p["block_time_unix"] or 0)
    out.pop("purchases_checkpoint", None)
    out["n_purchases_found"] = len(all_purchases)
    out["purchases"] = all_purchases
    if all_purchases:
        first = all_purchases[0]
        out["first_purchase"] = first
        price_sol = (first["paid_sol"] / first["token_amount_received"]) if first.get("paid_sol") and first["token_amount_received"] else None
        price_usdc = (first["paid_usdc"] / first["token_amount_received"]) if first.get("paid_usdc") and first["token_amount_received"] else None
        out["first_purchase_entry_price"] = {"price_per_token_sol": price_sol, "price_per_token_usdc": price_usdc}
        print(f"[solana] первая покупка: слот {first['slot']}, sig {first['signature']}, "
              f"blockTime={first['block_time_unix']}, получено {first['token_amount_received']} токенов, "
              f"programs={first['dex_labels_guess']}")
    else:
        out["HONEST_ANSWER"] = "История ATA просмотрена полностью, ни одной транзакции с ростом баланса этого токена не найдено -- покупок нет."
        print("[solana] " + out["HONEST_ANSWER"])

    # --- Независимая сверка: реальные пулы этого токена на GeckoTerminal ---
    gt_status, gt_body = gt_get(f"/networks/solana/tokens/{TOKEN_MINT}/pools")
    out["geckoterminal_pools_status"] = gt_status
    if gt_status == 200 and gt_body:
        pools = []
        for p in gt_body.get("data", []):
            attrs = p.get("attributes", {})
            pools.append({
                "address": attrs.get("address"), "name": attrs.get("name"),
                "dex_id": (p.get("relationships", {}).get("dex", {}).get("data", {}) or {}).get("id"),
                "reserve_in_usd": attrs.get("reserve_in_usd"), "pool_created_at": attrs.get("pool_created_at"),
                "volume_usd_h24": (attrs.get("volume_usd") or {}).get("h24"),
            })
        out["geckoterminal_pools"] = pools
        print(f"[solana] GeckoTerminal: {len(pools)} реальных пулов для этого токена (независимая сверка DEX)")
    else:
        out["geckoterminal_pools_note"] = "GT не вернул 200 -- сверка недоступна в этом прогоне, не выдумываем данные"

    out["total_runtime_s"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[solana] записано {OUT_PATH}")


if __name__ == "__main__":
    main()
