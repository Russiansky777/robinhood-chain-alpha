#!/usr/bin/env python3
"""Владелец (2026-09-17), Fomo -- второй заход на приоритет 2: сопоставление
по ПОЗИЦИИ в списке Swap-логов (первая/последняя нога) дало 0 содержательных
совпадений из 1142 tx со свопами -- оказалось ненадёжным, потому что
исполнитель `0xb92fe925...` батчит свопы НЕСКОЛЬКИХ независимых
получателей в одной транзакции (см. `data/fomo_shared_contract_investigation_
result.json`: cleanupErc20s/multicall-паттерн).

Новый метод -- НЕ по позиции в списке, а по NET-FLOW БАЛАНСА конкретного
кошелька внутри транзакции: тот же принцип, что использовался для честного
детектора циклов на Arc (сведение входящих минус исходящих по каждому
токену внутри одной tx для конкретного адреса). Не нужно резолвить
Uniswap V4 pool_id -> currency (то, что съело весь бюджет прошлого
прогона) -- ERC-20 Transfer-логи сами по себе уже содержат from/to/value,
пул тут ни при чём.

Алгоритм на каждую кандидатную tx (тот же список 1720, что и в прошлом
раунде -- где кошелёк получил токен от исполнителя):
  1. Полный список Transfer-логов receipt (topic0 = keccak256(Transfer(
     address,address,uint256)), from/to -- индексированные topics[1]/[2],
     value -- data).
  2. Net-flow ПО ЭТОМУ КОШЕЛЬКУ по каждому токену = сумма(value, to=
     кошелёк) - сумма(value, from=кошелёк).
  3. Покупка = net>0 по какому-то токену X (не платёжный) И net<0 по
     каноническому USDG или WETH (по АДРЕСУ, не по symbol -- 23 токена
     самозаявляют "USDG").
  4. Цена входа = |net платёжного| / |net X| -- прямо из этих чисел,
     WETH переводится в USD через WETH/USDG референс-пул на том же
     блоке (тот же slot0()-метод, что везде в сессии).
  5. Батчинг: сколько РАЗНЫХ адресов получают НЕ-платёжный токен в этой
     же транзакции -- считается по факту, не предполагается.
  6. Если ни один канонический ERC-20-платёж не найден -- честно
     проверяем tx.value (нативный токен, приложенный к самой tx) как
     платёж; ВНУТРЕННИЕ нативные переводы НЕ проверяются (нужен
     debug_traceTransaction/trace_transaction, не гарантирован на
     публичном RPC этой цепи -- честно помечено как непроверенное, не
     выдумано)."""
from __future__ import annotations

import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import rpc_call_trading_path, topic0  # noqa: E402

OUT_PATH = Path("data/fomo_9wallets_balance_based_purchases_result.json")
IN_CSV = Path("data/fomo_9wallets_alchemy_raw_transfers.csv")

EXECUTOR = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
PAYMENT_TOKENS = {USDG, WETH}
USDG_DECIMALS = 6
WETH_DECIMALS = 18
WETH_USDG_POOL_V3 = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
SLOT0_SELECTOR = "0x3850c7bd"
DECIMALS_SELECTOR = "0x313ce567"
TRANSFER_TOPIC0 = topic0("Transfer(address,address,uint256)")

RPC = rpc_call_trading_path
TIME_BUDGET_S = 900.0


def get_decimals(token: str, cache: dict) -> int:
    token = token.lower()
    if token in cache:
        return cache[token]
    if token == USDG:
        cache[token] = USDG_DECIMALS
        return cache[token]
    if token == WETH:
        cache[token] = WETH_DECIMALS
        return cache[token]
    try:
        raw = RPC("eth_call", [{"to": token, "data": DECIMALS_SELECTOR}, "latest"])
        dec = int(raw, 16) if raw and raw != "0x" else 18
    except Exception:  # noqa: BLE001
        dec = 18
    cache[token] = dec
    return dec


def weth_usdg_price_at_block(block_num: int, cache: dict) -> float | None:
    if block_num in cache:
        return cache[block_num]
    try:
        raw = RPC("eth_call", [{"to": WETH_USDG_POOL_V3, "data": SLOT0_SELECTOR}, hex(block_num)])
        sqrt_price_x96 = int(raw[2:66], 16) if raw and raw != "0x" else None
    except Exception:  # noqa: BLE001
        sqrt_price_x96 = None
    if sqrt_price_x96 is None:
        cache[block_num] = None
        return None
    sqrt_p = sqrt_price_x96 / (2 ** 96)
    price = (sqrt_p * sqrt_p) * (10 ** (WETH_DECIMALS - USDG_DECIMALS))  # token0=WETH,token1=USDG
    cache[block_num] = price
    return price


def parse_transfer_logs(receipt: dict) -> list[dict]:
    out = []
    for lg in receipt.get("logs", []):
        topics = lg.get("topics") or []
        if not topics or topics[0].lower() != TRANSFER_TOPIC0.lower():
            continue
        if len(topics) < 3:
            continue
        token = (lg.get("address") or "").lower()
        frm = "0x" + topics[1][-40:]
        to = "0x" + topics[2][-40:]
        try:
            value = int(lg["data"], 16)
        except (ValueError, TypeError):
            continue
        out.append({"token": token, "from": frm.lower(), "to": to.lower(), "value_raw": value})
    return out


def run() -> int:
    if not IN_CSV.exists():
        print(f"[balance_purchases] СТОП: {IN_CSV} не найден")
        return 1
    with IN_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    candidates = [r for r in rows if r["direction"] == "in" and (r["from_addr"] or "").lower() == EXECUTOR]
    by_tx: dict[str, list] = defaultdict(list)
    for r in candidates:
        by_tx[r["tx_hash"]].append(r)
    print(f"[balance_purchases] {len(candidates)} кандидатных erc20-переводов, {len(by_tx)} уникальных tx")

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "n_candidate_tx": len(by_tx), "executor": EXECUTOR,
                 "canonical_payment_tokens": sorted(PAYMENT_TOKENS)}

    start_ts = time.time()
    dec_cache: dict = {}
    weth_price_cache: dict = {}
    n_receipts_fetched = 0
    n_rpc_errors = 0
    budget_exhausted = False
    batching_distribution: dict[int, int] = defaultdict(int)  # n_distinct_recipients -> count of tx
    n_tx_with_erc20_payment = 0
    n_tx_no_erc20_payment_checked_native = 0
    n_tx_native_value_nonzero = 0

    confirmed_purchases: list[dict] = []

    tx_items = list(by_tx.items())
    for i, (tx_hash, wallet_rows) in enumerate(tx_items):
        if time.time() - start_ts > TIME_BUDGET_S:
            budget_exhausted = True
            out["budget_exhausted_at_tx_index"] = i
            break
        try:
            receipt = RPC("eth_getTransactionReceipt", [tx_hash])
        except Exception:  # noqa: BLE001
            n_rpc_errors += 1
            continue
        n_receipts_fetched += 1
        if not receipt:
            n_rpc_errors += 1
            continue

        transfers = parse_transfer_logs(receipt)
        block_num = int(receipt["blockNumber"], 16)

        # Батчинг: сколько РАЗНЫХ адресов получают НЕ-платёжный токен в этой tx.
        non_payment_recipients = {t["to"] for t in transfers if t["token"] not in PAYMENT_TOKENS}
        batching_distribution[len(non_payment_recipients)] += 1

        for wr in wallet_rows:
            wallet_addr = wr["wallet_address"].lower()
            net: dict[str, int] = defaultdict(int)
            for t in transfers:
                if t["to"] == wallet_addr:
                    net[t["token"]] += t["value_raw"]
                if t["from"] == wallet_addr:
                    net[t["token"]] -= t["value_raw"]

            payment_token = None
            payment_net_raw = 0
            for pt in PAYMENT_TOKENS:
                if net.get(pt, 0) < 0:
                    payment_token, payment_net_raw = pt, net[pt]
                    break

            purchased_token = None
            purchased_net_raw = 0
            for tok, v in net.items():
                if tok in PAYMENT_TOKENS:
                    continue
                if v > 0:
                    purchased_token, purchased_net_raw = tok, v
                    break

            if payment_token is None:
                n_tx_no_erc20_payment_checked_native += 1
                try:
                    tx = RPC("eth_getTransactionByHash", [tx_hash])
                    native_value = int(tx.get("value", "0x0"), 16) if tx else 0
                except Exception:  # noqa: BLE001
                    native_value = 0
                if native_value > 0:
                    n_tx_native_value_nonzero += 1
                    # Честно: считаем нативный платёж только если это ЕДИНСТВЕННЫЙ источник --
                    # внутренние нативные переводы НЕ проверяются (нужен debug_traceTransaction).
                    if purchased_token is not None:
                        payment_token, payment_net_raw = "native", -native_value
                if payment_token is None:
                    continue
            else:
                n_tx_with_erc20_payment += 1

            if purchased_token is None:
                continue

            paid_dec = WETH_DECIMALS if payment_token == "native" else get_decimals(payment_token, dec_cache)
            paid_human = abs(payment_net_raw) / (10 ** paid_dec)
            recv_dec = get_decimals(purchased_token, dec_cache)
            recv_human = purchased_net_raw / (10 ** recv_dec)
            if recv_human == 0:
                continue

            entry_price_usd = None
            price_note = None
            if payment_token == USDG:
                entry_price_usd = paid_human / recv_human
            elif payment_token in (WETH, "native"):
                weth_usd = weth_usdg_price_at_block(block_num, weth_price_cache)
                if weth_usd is not None:
                    entry_price_usd = (paid_human * weth_usd) / recv_human
                else:
                    price_note = "WETH/USDG на этом блоке не получен"
            else:
                price_note = "неканонический платёжный токен"

            confirmed_purchases.append({
                "wallet_name": wr["wallet_name"], "wallet_address": wallet_addr, "tx_hash": tx_hash,
                "block": block_num, "block_time_utc": wr.get("block_time_utc"),
                "payment_token": payment_token, "paid_amount_human": paid_human,
                "purchased_token": purchased_token, "purchased_symbol": wr.get("asset_symbol"),
                "purchased_amount_human": recv_human, "entry_price_usd_per_token": entry_price_usd,
                "price_note": price_note, "n_distinct_non_payment_recipients_in_tx": len(non_payment_recipients),
            })

        if (i + 1) % 200 == 0:
            print(f"[balance_purchases] прогресс: {i + 1}/{len(tx_items)} tx, "
                  f"{len(confirmed_purchases)} покупок пока")

    out["n_receipts_fetched"] = n_receipts_fetched
    out["n_rpc_errors"] = n_rpc_errors
    out["budget_exhausted"] = budget_exhausted
    out["n_tx_with_erc20_payment"] = n_tx_with_erc20_payment
    out["n_tx_no_erc20_payment_checked_native"] = n_tx_no_erc20_payment_checked_native
    out["n_tx_native_value_nonzero"] = n_tx_native_value_nonzero
    out["batching_distribution_n_distinct_recipients_to_n_tx"] = {str(k): v for k, v in sorted(batching_distribution.items())}
    n_tx_multi_recipient = sum(v for k, v in batching_distribution.items() if k > 1)
    out["n_tx_with_more_than_1_distinct_recipient"] = n_tx_multi_recipient
    out["batching_confirmed"] = n_tx_multi_recipient > 0

    out["n_confirmed_purchases_total"] = len(confirmed_purchases)
    per_wallet_purchases: dict[str, int] = defaultdict(int)
    for p in confirmed_purchases:
        per_wallet_purchases[p["wallet_name"]] += 1
    out["n_confirmed_purchases_by_wallet"] = dict(per_wallet_purchases)

    by_wallet_token: dict[tuple, list] = defaultdict(list)
    for p in confirmed_purchases:
        by_wallet_token[(p["wallet_name"], p["purchased_token"])].append(p)
    first_entries = []
    for (wallet, tok), plist in by_wallet_token.items():
        plist.sort(key=lambda x: x["block"])
        first_entries.append(plist[0])
    first_entries.sort(key=lambda x: x["block"])
    per_wallet_first_entries: dict[str, int] = defaultdict(int)
    for fe in first_entries:
        per_wallet_first_entries[fe["wallet_name"]] += 1
    out["n_first_entries_total"] = len(first_entries)
    out["n_first_entries_by_wallet"] = dict(per_wallet_first_entries)
    out["first_entries"] = first_entries
    out["all_confirmed_purchases"] = confirmed_purchases
    out["n_first_entries_with_usd_entry_price"] = sum(1 for fe in first_entries if fe["entry_price_usd_per_token"] is not None)

    print(f"\n[balance_purchases] ИТОГ: {out['n_confirmed_purchases_total']} подтверждённых покупок, "
          f"{out['n_first_entries_total']} первых входов")
    print(f"[balance_purchases] батчинг: {n_tx_multi_recipient} из {n_receipts_fetched} tx имеют >1 получателя "
          f"не-платёжного токена -- batching_confirmed={out['batching_confirmed']}")
    print(f"[balance_purchases] по кошелькам (покупки): {dict(per_wallet_purchases)}")
    print(f"[balance_purchases] по кошелькам (первые входы): {dict(per_wallet_first_entries)}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[balance_purchases] Результат: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
