#!/usr/bin/env python3
"""Владелец, 2026-09-17: Solana разовая проверка, этап B.

ЧЕСТНАЯ НАХОДКА этапа A (`task_solana_wallet_first_buy_result.json`,
реальный прогон): 2 из 3 "покупок" -- это НЕ своп кошелька, а перевод
токена ИЗ стороннего аккаунта (`53AtUKnQG8TKkuPWxbv4ZC6ExVPbpPMTudK2RxHdhH8e`,
похоже на пул горячего кошелька торгового бота/OTC-исполнителя) НА
свежесозданный ATA этого кошелька -- своп исполнялся не самим кошельком.
Обе передачи показывают РОВНО 3.00% удержания в пути (12140.104108 ->
11775.900984 получено = -3.00000%; 47218.728977 -> 45802.167107 = -2.99999%)
-- это Token-2022 TransferFeeConfig (программа `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb`
подтверждена в обеих транзакциях), НЕ комиссия AMM-пула -- применяется к
ЛЮБОМУ переводу этого минта, включая обе стороны свопа в пуле. Это
СВЕРХ любой собственной комиссии AMM.

Этот этап:
  1. Подтверждает 3% mint-level fee количественно (из уже собранных данных
     этапа A, без новых сетевых вызовов).
  2. Реальная комиссия AMM-пула -- Raydium `api-v3.raydium.io/pools/info/ids`
     по адресу реального самого ликвидного пула (`LeZ2DH1y7bqzAghYbXqKB6fNxoBKqPSPihv3EmkLBRv`,
     dex_id=raydium, TVL $50,341, объём/сутки $101,089 -- на порядок
     ликвиднее остальных 4 найденных пулов, реальная торговая площадка).
  3. Цена после первой покупки -- РЕАЛЬНЫЕ сделки в этом пуле (не OHLCV-
     свечи -- для +5с/+15с/+30с свечи физически не дают разрешения),
     находятся через `getSignaturesForAddress(pool_id)` + дельты
     preTokenBalances/postTokenBalances (тот же метод, что в этапе A --
     DEX-агностичный, не требует декодирования конкретного формата
     инструкций). ЧЕСТНО: T0 = блоктайм ПЕРВОГО обнаруженного роста
     баланса кошелька (2026-09-16T22:00:37Z) -- если это перевод, а не
     прямой своп, отмечаем это в выводе, но используем как единственную
     реально доступную точку отсчёта. Горизонты, чьё целевое время ещё
     не наступило к моменту прогона, честно помечаются "ещё не наступил"
     -- НЕ экстраполируются.
  4. Волна -- число ДРУГИХ (не наш кошелёк) реальных сделок в этом же
     пуле за 1/5/30 минут до и после T0, и время до первой чужой сделки
     после T0 -- из того же набора реальных транзакций пула.

Результат: `data/task_solana_price_horizons_and_wave_result.json`."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from task_solana_wallet_first_buy import (  # noqa: E402
    rpc_call, WALLET, TOKEN_MINT, KNOWN_DEX_PROGRAMS,
)

FIRST_BUY_RESULT = Path("data/task_solana_wallet_first_buy_result.json")
OUT_PATH = Path("data/task_solana_price_horizons_and_wave_result.json")

POOL_ADDRESS = "LeZ2DH1y7bqzAghYbXqKB6fNxoBKqPSPihv3EmkLBRv"  # реальный, из GeckoTerminal (этап A), самый ликвидный
POOL_DEX_ID = "raydium"
# Реальный адрес котируемого минта (GLDx), подтверждён официальным Raydium
# API (mintA.address) -- НЕ угадан по имени. Используется как ОБЯЗАТЕЛЬНЫЙ
# фильтр контрагентного минта в extract_trade (см. ниже, честный фикс
# реального бага: без него разные несвязанные минты из одной сложной
# multi-hop транзакции суммировались в одну "цену", дав абсурдный
# результат вроде +166 триллионов% на реальном прогоне).
QUOTE_MINT = "Xsv9hRk1z5ystj9MhnA7Lq4vjSsLwzL2nxrwmwtD3re"

HORIZONS_S = [5, 15, 30, 60, 180, 300, 900, 3600, 21600, 86400]  # +5с..+24ч

TIME_BUDGET_S = 900.0
REQUEST_PAGE_LIMIT = 1000


def get_signatures_for_address(address: str, before: str | None = None, limit: int = REQUEST_PAGE_LIMIT) -> list[dict]:
    params: dict = {"limit": limit}
    if before:
        params["before"] = before
    return rpc_call("getSignaturesForAddress", [address, params]) or []


def get_transaction(sig: str) -> dict | None:
    # maxSupportedTransactionVersion=1: реальный прогон упал с RPC -32015 на
    # versioned-транзакции (address lookup table) в этом высоконагруженном
    # пуле ($101k/24h объёма) -- версии 0 недостаточно.
    return rpc_call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])


def raydium_pool_fee(pool_id: str) -> dict:
    try:
        resp = requests.get("https://api-v3.raydium.io/pools/info/ids", params={"ids": pool_id}, timeout=25)
        if resp.status_code != 200:
            return {"status": resp.status_code, "error": resp.text[:300]}
        body = resp.json()
        data = (body.get("data") or [None])[0]
        if not data:
            return {"status": 200, "error": "пул не найден в ответе Raydium API"}
        return {
            "status": 200, "raw_type": data.get("type"),
            "fee_rate": data.get("feeRate"), "config_trade_fee_rate": (data.get("config") or {}).get("tradeFeeRate"),
            "raw": data,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def extract_trade(tx: dict, token_mint: str) -> dict | None:
    """DEX-агностичное извлечение реальной цены сделки из ЛЮБОЙ транзакции,
    затронувшей пул -- сумма дельт token_mint против суммы дельт ИМЕННО
    QUOTE_MINT (реальный котируемый минт этого пула, GLDx, подтверждён
    Raydium API), участвовавшего в той же транзакции.

    ЧЕСТНЫЙ ФИКС РЕАЛЬНОГО БАГА (первый прогон узкого окна, реальные
    данные): раньше суммировались дельты ЛЮБОГО минта, отличного от
    token_mint -- в busy-блоке многие транзакции представляют собой
    multi-hop маршруты, трогающие НЕСКОЛЬКО несвязанных токенов в одной
    транзакции; суммирование их дельт как будто это одна валюта дало
    абсурдную "цену" (+166 триллионов% в одном горизонте). Теперь строго
    требуем QUOTE_MINT -- если его нет в транзакции или есть, но
    token_net около нуля, это НЕ своп нашей пары, честно пропускаем."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    pre_tb, post_tb = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []

    def by_idx(rows):
        return {r["accountIndex"]: r for r in rows}

    pre_by_idx, post_by_idx = by_idx(pre_tb), by_idx(post_tb)
    all_idx = set(pre_by_idx) | set(post_by_idx)
    token_net, quote_net = 0.0, 0.0
    signer = None
    account_keys = [k.get("pubkey") if isinstance(k, dict) else k
                    for k in tx.get("transaction", {}).get("message", {}).get("accountKeys", [])]
    if account_keys:
        signer = account_keys[0]
    for idx in all_idx:
        pre, post = pre_by_idx.get(idx), post_by_idx.get(idx)
        mint = (post or pre).get("mint")
        pre_amt = float(pre["uiTokenAmount"]["uiAmount"]) if pre and pre["uiTokenAmount"]["uiAmount"] is not None else 0.0
        post_amt = float(post["uiTokenAmount"]["uiAmount"]) if post and post["uiTokenAmount"]["uiAmount"] is not None else 0.0
        delta = post_amt - pre_amt
        if mint == token_mint:
            token_net += delta
        elif mint == QUOTE_MINT:
            quote_net += delta
    other_mint = QUOTE_MINT
    other_net = quote_net
    if abs(token_net) < 1e-9 or abs(other_net) < 1e-12:
        return None  # не своп нашей пары token/QUOTE_MINT в этой транзакции
    price = abs(other_net) / abs(token_net)
    return {
        "signature": tx.get("transaction", {}).get("signatures", [None])[0],
        "slot": tx.get("slot"), "block_time_unix": tx.get("blockTime"),
        "signer": signer, "token_net_delta": token_net, "other_mint": other_mint,
        "other_net_delta": other_net, "price_other_per_token": price,
        "is_our_wallet": signer == WALLET,
    }


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "pool_address": POOL_ADDRESS, "pool_dex_id": POOL_DEX_ID}
    t0 = time.time()

    # --- Часть 1: подтверждённая 3% mint-level комиссия (из уже собранных данных этапа A) ---
    if FIRST_BUY_RESULT.exists():
        stage_a = json.loads(FIRST_BUY_RESULT.read_text())
        transfer_fee_samples = []
        for p in stage_a.get("purchases", []):
            diag = p.get("UNRESOLVED_PAYMENT_DIAGNOSTIC")
            if not diag:
                continue
            pre_map = {r["accountIndex"]: r for r in diag["pre_token_balances"]}
            post_map = {r["accountIndex"]: r for r in diag["post_token_balances"]}
            for idx, pre in pre_map.items():
                if pre.get("mint") != TOKEN_MINT or pre.get("owner") == WALLET:
                    continue
                pre_amt = pre["uiTokenAmount"]["uiAmount"] or 0.0
                post = post_map.get(idx)
                post_amt = (post["uiTokenAmount"]["uiAmount"] if post and post["uiTokenAmount"]["uiAmount"] is not None else 0.0)
                sent_out = pre_amt - post_amt
                received = p["token_amount_received"]
                if sent_out > 0:
                    fee_pct = (sent_out - received) / sent_out * 100
                    transfer_fee_samples.append({"signature": p["signature"], "sent_from_source": sent_out,
                                                  "received_by_wallet": received, "implied_fee_pct": fee_pct})
        out["mint_transfer_fee_samples"] = transfer_fee_samples
        if transfer_fee_samples:
            avg_fee = sum(s["implied_fee_pct"] for s in transfer_fee_samples) / len(transfer_fee_samples)
            out["mint_transfer_fee_pct_measured"] = round(avg_fee, 4)
            out["MINT_FEE_HONEST_FINDING"] = (
                f"Token-2022 mint-level transfer fee ИЗМЕРЕН на {len(transfer_fee_samples)} реальных переводах: "
                f"~{avg_fee:.2f}% -- это СВЕРХ любой комиссии AMM-пула, применяется к ЛЮБОМУ перемещению токена "
                "(в т.ч. обе ноги свопа внутри пула). Круглый билет (купить+продать) стоит минимум ~2x эту цифру "
                "только на mint-fee, до учёта AMM-комиссии и проскальзывания."
            )
    else:
        out["mint_transfer_fee_note"] = "Результат этапа A не найден -- пропускаем эту сверку"

    # --- Часть 2: реальная комиссия AMM-пула (Raydium API) ---
    out["amm_pool_fee"] = raydium_pool_fee(POOL_ADDRESS)

    # --- Часть 3: собрать реальную историю сделок пула ---
    print("[stageB] собираем подписи пула...")
    sigs, before = [], None
    while time.time() - t0 < TIME_BUDGET_S * 0.5:
        batch = get_signatures_for_address(POOL_ADDRESS, before=before)
        if not batch:
            break
        sigs.extend(batch)
        before = batch[-1]["signature"]
        if len(batch) < REQUEST_PAGE_LIMIT:
            break
        oldest_bt = batch[-1].get("blockTime")
        # честная ранняя остановка -- как только ушли на сутки раньше T0, дальше не нужно
        first_buy_time = None
        if FIRST_BUY_RESULT.exists():
            fb = json.loads(FIRST_BUY_RESULT.read_text()).get("first_purchase", {})
            first_buy_time = fb.get("block_time_unix")
        if oldest_bt and first_buy_time and oldest_bt < first_buy_time - 86400:
            break
    out["n_pool_signatures_fetched"] = len(sigs)
    print(f"[stageB] {len(sigs)} подписей пула получено")

    trades = []
    n_decode_attempted, n_decode_errors = 0, 0
    for s in sigs:
        if time.time() - t0 > TIME_BUDGET_S:
            out.setdefault("budget_warnings", []).append("бюджет исчерпан при декодировании сделок пула")
            break
        if s.get("err") is not None:
            continue
        n_decode_attempted += 1
        try:
            tx = get_transaction(s["signature"])
            if tx is None:
                continue
            trade = extract_trade(tx, TOKEN_MINT)
            if trade:
                trades.append(trade)
        except Exception as exc:  # noqa: BLE001
            # ЧЕСТНАЯ НАХОДКА (реальный прогон упал целиком на ОДНОЙ versioned-
            # транзакции, -32015): один плохой сигнатура не должен стоить
            # ВСЕХ остальных decoded trades -- пропускаем и продолжаем,
            # честно считая, сколько раз это случилось.
            n_decode_errors += 1
            print(f"[stageB] пропуск {s['signature']}: {type(exc).__name__}: {exc}")
            continue
        if n_decode_attempted % 20 == 0:
            out["trades_checkpoint_count"] = len(trades)
            OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    out.pop("trades_checkpoint_count", None)
    out["n_decode_errors"] = n_decode_errors
    trades.sort(key=lambda t: t["block_time_unix"] or 0)
    out["n_trades_decoded"] = len(trades)
    out["n_decode_attempted"] = n_decode_attempted
    print(f"[stageB] реальных сделок декодировано: {len(trades)} из {n_decode_attempted} попыток")

    # --- Часть 4: цена по горизонтам от T0 ---
    first_buy = json.loads(FIRST_BUY_RESULT.read_text()).get("first_purchase") if FIRST_BUY_RESULT.exists() else None
    if not first_buy or not first_buy.get("block_time_unix") or not trades:
        out["HORIZONS_NOTE"] = "Нет T0 или нет декодированных сделок -- таблица горизонтов не построена, не выдумываем"
    else:
        t0_time = first_buy["block_time_unix"]
        out["T0_note"] = ("T0 = блоктайм первого обнаруженного роста баланса кошелька (перевод, не прямой своп "
                           "этим кошельком -- см. этап A) -- единственная реально доступная точка отсчёта")
        out["T0_unix"] = t0_time
        entry_trade = min(trades, key=lambda t: abs((t["block_time_unix"] or 0) - t0_time))
        out["entry_reference_trade"] = entry_trade
        entry_price = entry_trade["price_other_per_token"]
        now_unix = time.time()
        horizons_out = []
        for h in HORIZONS_S:
            target = t0_time + h
            if target > now_unix:
                horizons_out.append({"horizon_s": h, "status": "ещё не наступил к моменту прогона"})
                continue
            nearest = min(trades, key=lambda t: abs((t["block_time_unix"] or 0) - target))
            gap_s = abs((nearest["block_time_unix"] or 0) - target)
            pct_change = ((nearest["price_other_per_token"] - entry_price) / entry_price * 100) if entry_price else None
            horizons_out.append({
                "horizon_s": h, "target_time_unix": target, "nearest_real_trade_time_unix": nearest["block_time_unix"],
                "gap_to_target_s": gap_s, "slot": nearest["slot"], "price": nearest["price_other_per_token"],
                "pct_change_from_entry": pct_change, "signature": nearest["signature"],
            })
        out["price_horizons"] = horizons_out

        # --- Часть 5: волна ---
        wave_windows_s = [60, 300, 1800]
        wave_out = {}
        for w in wave_windows_s:
            before_others = sum(1 for t in trades if not t["is_our_wallet"] and t0_time - w <= (t["block_time_unix"] or 0) < t0_time)
            after_others = sum(1 for t in trades if not t["is_our_wallet"] and t0_time < (t["block_time_unix"] or 0) <= t0_time + w)
            wave_out[f"window_{w}s"] = {"n_other_trades_before": before_others, "n_other_trades_after": after_others}
        others_after = sorted([t for t in trades if not t["is_our_wallet"] and (t["block_time_unix"] or 0) > t0_time],
                               key=lambda t: t["block_time_unix"])
        wave_out["seconds_to_first_other_trade_after"] = (
            (others_after[0]["block_time_unix"] - t0_time) if others_after else None
        )
        out["wave_detection"] = wave_out

    out["total_runtime_s"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[stageB] записано {OUT_PATH}")


if __name__ == "__main__":
    main()
