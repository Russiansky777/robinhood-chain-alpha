#!/usr/bin/env python3
"""Консолидация активов -- НЕЗАВИСИМАЯ верификация результата
execute_base.py (ТОЛЬКО ЧТЕНИЕ, 0 транзакций).

РЕАЛЬНАЯ НАХОДКА: execute_base.py (run 33976972830, оба свопа реально
SUCCESS on-chain) напечатал "финальный баланс WETH" = 0.014648849538673675
-- ЭТО БИТ-В-БИТ равно ТОЛЬКО симулированному output cbBTC-ноги
(14648849538673675 raw), хотя USDC-нога тоже реально прошла (tx
0x92c5541056508a14de7730a92e7af8007bc5bc93a4a3d839524bd747cdc3cd60,
её собственная симуляция дала ещё +17801561015032567 raw). Похоже на
лаг RPC-реплики (balanceOf сразу после последней записи попал на
отстающую реплику) -- тот же класс проблемы, что уже упоминается в
шапке execute_base.py про gas-estimate retries.

Эта верификация НЕ доверяет balanceOf сразу после tx. Вместо этого:
1. Читает ПОЛНЫЕ квитанции (eth_getTransactionReceipt) обоих реальных
   swap-tx и декодирует Transfer(address,address,uint256) events WETH
   с to=WALLET -- это даёт ТОЧНУЮ сумму, которая реально пришла на
   кошелёк по каждой транзакции, независимо от реплика-лага.
2. Отдельно, с задержкой и повтором, читает ТЕКУЩИЙ (по состоянию на
   сейчас) баланс WETH/USDC/cbBTC/ETH на Base -- если он всё ещё не
   совпадает с суммой Transfer-логов, значит лаг ещё не рассосался,
   и мы явно об этом пишем, не выдавая расчёт за факт."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
BASE_RPC = "https://mainnet.base.org"
WETH = "0x4200000000000000000000000000000000000006"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH_DECIMALS, CBBTC_DECIMALS, USDC_DECIMALS = 18, 8, 6


def _event_topic(sig: str) -> str:
    """Реальный keccak топика события, вычисляется, не хардкодится --
    тот же принцип, что _selector() для функций везде в проекте."""
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()


TRANSFER_TOPIC = _event_topic("Transfer(address,address,uint256)")
BALANCE_OF_SELECTOR = "0x70a08231"

RESULT_PATH = Path("data/p3_guard_cache/asset_consolidation_execute_base_result.json")
OUT_PATH = Path("data/p3_guard_cache/asset_consolidation_base_verify_result.json")

RETRY_DELAYS = [2.0, 4.0, 8.0, 16.0]


def rpc(method: str, params: list):
    last_exc = None
    for delay in [0.0] + RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
            if r.status_code == 429:
                last_exc = RuntimeError("429 Too Many Requests")
                continue
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise RuntimeError(f"{body['error']} (метод={method})")
            return body["result"]
        except requests.exceptions.HTTPError as e:
            last_exc = e
    raise RuntimeError(f"RPC {method} не удался: {last_exc}")


def erc20_balance(token: str) -> int:
    padded = WALLET[2:].lower().rjust(64, "0")
    return int(rpc("eth_call", [{"to": token, "data": BALANCE_OF_SELECTOR + padded}, "latest"]), 16)


def decode_weth_transfers_to_wallet(receipt: dict) -> int:
    """Сумма Transfer(WETH) событий с to=WALLET из РЕАЛЬНОЙ квитанции."""
    total = 0
    wallet_topic = "0x" + WALLET[2:].lower().rjust(64, "0")
    for log in receipt.get("logs", []):
        if log["address"].lower() != WETH.lower():
            continue
        topics = log["topics"]
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC.lower():
            continue
        if topics[2].lower() != wallet_topic:
            continue
        total += int(log["data"], 16)
    return total


def run() -> int:
    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    result = json.loads(RESULT_PATH.read_text())
    swap_txs = [t for t in result["txs"] if t["label"].endswith("_swap") and t["status"] == "success"]
    out["swap_txs_checked"] = [t["tx_hash"] for t in swap_txs]

    total_weth_from_receipts = 0
    per_tx = {}
    for t in swap_txs:
        receipt = rpc("eth_getTransactionReceipt", [t["tx_hash"]])
        amount = decode_weth_transfers_to_wallet(receipt)
        per_tx[t["label"]] = amount / 10 ** WETH_DECIMALS
        total_weth_from_receipts += amount
        print(f"[verify] {t['label']} ({t['tx_hash']}): WETH Transfer(to=WALLET) из квитанции = {amount / 10**WETH_DECIMALS}")
        time.sleep(0.5)

    out["weth_per_tx_from_receipt_logs"] = per_tx
    out["weth_total_from_receipt_logs"] = total_weth_from_receipts / 10 ** WETH_DECIMALS
    print(f"[verify] СУММА по квитанциям (независимо от реплика-лага): {out['weth_total_from_receipt_logs']} WETH")

    # Отдельно, с задержкой -- живой текущий баланс, для сверки, что
    # реплика-лаг рассосался.
    time.sleep(3.0)
    weth_now = erc20_balance(WETH)
    cbbtc_now = erc20_balance(CBBTC)
    usdc_now = erc20_balance(USDC)
    out["balances_now"] = {
        "weth": weth_now / 10 ** WETH_DECIMALS,
        "cbbtc": cbbtc_now / 10 ** CBBTC_DECIMALS,
        "usdc": usdc_now / 10 ** USDC_DECIMALS,
    }
    print(f"[verify] ТЕКУЩИЙ (после задержки) баланс WETH: {out['balances_now']['weth']}, "
          f"cbBTC: {out['balances_now']['cbbtc']}, USDC: {out['balances_now']['usdc']}")

    out["matches"] = abs(out["balances_now"]["weth"] - out["weth_total_from_receipt_logs"]) < 1e-9
    if not out["matches"]:
        print(f"[verify] ВНИМАНИЕ: текущий баланс ({out['balances_now']['weth']}) НЕ совпадает с суммой квитанций "
              f"({out['weth_total_from_receipt_logs']}) -- лаг ещё не рассосался или есть иная причина, не выдаём "
              f"расчёт за факт, нужна повторная проверка.")
    else:
        print("[verify] Совпадает -- реплика-лаг рассосался, реальный текущий баланс подтверждён.")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[verify] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
