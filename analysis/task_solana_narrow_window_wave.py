#!/usr/bin/env python3
"""Владелец, 2026-09-17: Solana разовая проверка, доделка волны -- узкое
окно ±5 минут вокруг РЕАЛЬНОГО свопа (покупка 3, единственный прямой своп
этим кошельком), а НЕ откат всей истории пула от "сейчас" (это уже
провалилось честно в прошлом прогоне -- публичный RPC не тянет объём
пула на 18-часовом окне).

Якорь: signature="2NkPm8GfVw2FYBHrbLbhUrwJGmGECYh4t89oMdCnEsnAXGK8qu4BoTfpNVKEHZLWgBfGYCpZ2oUCBFLW35m8XCNS",
slot=447644977, blockTime=1789600539 (2026-09-16T23:15:39Z) -- взято из
уже подтверждённого реального результата этапа A, не пересчитано.

Метод:
  - ДО покупки (0..300с назад): `getSignaturesForAddress(pool,
    before=<сама сигнатура покупки>)` -- начинает ИМЕННО с позиции нашей
    транзакции в истории пула, никакого отката от "сейчас" не требуется,
    дёшево (несколько страниц на 5 минут активного пула).
  - ПОСЛЕ покупки (0..300с вперёд): `getSignaturesForAddress` умеет идти
    только назад от известной точки -- "заглянуть вперёд" от старой
    сигнатуры им нельзя. Вместо этого -- `getBlocks(start_slot, end_slot)`
    (реальный список произведённых слотов в диапазоне, один дешёвый
    вызов) + `getBlock(slot, transactionDetails="accounts")` на каждый
    реальный слот в узком диапазоне [anchor_slot, anchor_slot+N] --
     обычный, документированный способ доступа к диапазону слотов по
    номеру (в отличие от истории адреса, слоты адресуются напрямую).
    `transactionDetails="accounts"` даёт account keys + pre/postToken-
    Balances без полных данных инструкций -- тот же дельта-метод, что
    и раньше, но легче тела ответа.
  - N (число слотов на 300с) -- ИЗМЕРЕНО из двух уже подтверждённых
    реальных точек (покупка 1 и покупка 3 этапа A), НЕ константа с
    другой сети: (447644977-447630803)/(1789600539-1789596037) = слот/с,
    отсюда 300с -> N слотов.
  - Честный бюджет времени и явный отчёт, если 5 минут не покрылись --
    по прямому указанию владельца: "если и пять минут не вытягиваются --
    сказать прямо, задачу закрываем", БЕЗ повторных попыток колотить
    публичный RPC.

Результат: `data/task_solana_narrow_window_wave_result.json`."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task_solana_wallet_first_buy import WALLET, TOKEN_MINT, rpc_call  # noqa: E402
from task_solana_price_horizons_and_wave import POOL_ADDRESS, extract_trade  # noqa: E402

OUT_PATH = Path("data/task_solana_narrow_window_wave_result.json")

ANCHOR_SIGNATURE = "2NkPm8GfVw2FYBHrbLbhUrwJGmGECYh4t89oMdCnEsnAXGK8qu4BoTfpNVKEHZLWgBfGYCpZ2oUCBFLW35m8XCNS"
ANCHOR_SLOT = 447644977
ANCHOR_TIME = 1789600539
# Измерено из покупок 1 и 3 этапа A (реальные слот/время), не константа с Robinhood Chain/Arc.
MEASURED_SLOT_TIME_S = (447644977 - 447630803) / (1789600539 - 1789596037)

WINDOW_S = 300  # ±5 минут
HORIZONS_S = [30, 60, 180, 300]

TIME_BUDGET_BEFORE_S = 180.0
TIME_BUDGET_AFTER_S = 480.0  # честно больше -- getBlock тяжелее по данным, чем getSignaturesForAddress


def get_signatures_for_address(address: str, before: str | None = None, until: str | None = None,
                                limit: int = 1000) -> list[dict]:
    params: dict = {"limit": limit}
    if before:
        params["before"] = before
    if until:
        params["until"] = until
    return rpc_call("getSignaturesForAddress", [address, params]) or []


def get_transaction(sig: str) -> dict | None:
    return rpc_call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])


def get_blocks(start_slot: int, end_slot: int) -> list[int]:
    return rpc_call("getBlocks", [start_slot, end_slot]) or []


def get_block_accounts(slot: int) -> dict | None:
    return rpc_call("getBlock", [slot, {"encoding": "jsonParsed", "transactionDetails": "accounts",
                                         "maxSupportedTransactionVersion": 1, "rewards": False}])


def collect_before(t0: float) -> tuple[list[dict], dict]:
    """ДО покупки: дёшево, начинаем ИМЕННО с позиции якоря."""
    trades = []
    sigs, before = [], ANCHOR_SIGNATURE
    n_pages = 0
    while time.time() - t0 < TIME_BUDGET_BEFORE_S:
        batch = get_signatures_for_address(POOL_ADDRESS, before=before)
        n_pages += 1
        if not batch:
            break
        sigs.extend(batch)
        oldest = batch[-1]
        before = oldest["signature"]
        if (oldest.get("blockTime") or 0) < ANCHOR_TIME - WINDOW_S:
            break
        if len(batch) < 1000:
            break
    in_window = [s for s in sigs if s.get("blockTime") and ANCHOR_TIME - WINDOW_S <= s["blockTime"] < ANCHOR_TIME
                 and s.get("err") is None]
    n_decoded, n_errors = 0, 0
    for s in in_window:
        if time.time() - t0 > TIME_BUDGET_BEFORE_S:
            break
        try:
            tx = get_transaction(s["signature"])
            if tx is None:
                continue
            n_decoded += 1
            trade = extract_trade(tx, TOKEN_MINT)
            if trade:
                trades.append(trade)
        except Exception as exc:  # noqa: BLE001
            n_errors += 1
            print(f"[narrow] before: пропуск {s['signature']}: {type(exc).__name__}: {exc}")
    meta = {"n_pages": n_pages, "n_signatures_in_window": len(in_window), "n_decoded": n_decoded, "n_errors": n_errors,
            "earliest_signature_blocktime_seen": sigs[-1].get("blockTime") if sigs else None,
            "coverage_seconds_before": (ANCHOR_TIME - sigs[-1]["blockTime"]) if sigs and sigs[-1].get("blockTime") else 0}
    return trades, meta


def collect_after(t0: float) -> tuple[list[dict], dict]:
    """ПОСЛЕ покупки: getSignaturesForAddress не умеет идти вперёд от
    старой точки -- используем прямой доступ по номеру слота (getBlocks/
    getBlock), честный диапазон [ANCHOR_SLOT, ANCHOR_SLOT + N]."""
    n_slots_needed = int(WINDOW_S / MEASURED_SLOT_TIME_S) + 50  # запас
    end_slot = ANCHOR_SLOT + n_slots_needed
    real_slots = get_blocks(ANCHOR_SLOT, end_slot)
    print(f"[narrow] after: реальных слотов в диапазоне [{ANCHOR_SLOT}, {end_slot}]: {len(real_slots)}")

    trades = []
    n_blocks_scanned, n_block_errors = 0, 0
    max_time_covered = ANCHOR_TIME
    first_block_shape_sample = None
    n_txs_seen_total = 0
    for slot in real_slots:
        if time.time() - t0 > TIME_BUDGET_AFTER_S:
            break
        try:
            block = get_block_accounts(slot)
        except Exception as exc:  # noqa: BLE001
            n_block_errors += 1
            print(f"[narrow] after: пропуск слота {slot}: {type(exc).__name__}: {exc}")
            continue
        if block is None:
            continue
        n_blocks_scanned += 1
        block_txs = block.get("transactions", [])
        n_txs_seen_total += len(block_txs)
        if first_block_shape_sample is None and block_txs:
            # ЧЕСТНАЯ ДИАГНОСТИКА: точная форма getBlock(transactionDetails=
            # "accounts") не проверена вживую заранее -- если POOL_ADDRESS
            # ни разу не найдётся ниже, этот сэмпл покажет, была ли причина
            # в реальном отсутствии сделок или в неверно угаданной форме
            # ответа (ключи не там, где ожидалось).
            first_block_shape_sample = {"slot": slot, "keys_top": sorted(block.keys()),
                                         "sample_tx_keys": sorted(block_txs[0].keys()),
                                         "sample_tx": block_txs[0]}
        block_time = block.get("blockTime")
        if block_time:
            max_time_covered = max(max_time_covered, block_time)
        for tx in block.get("transactions", []):
            meta = tx.get("meta") or {}
            txn = tx.get("transaction", {}) or {}
            # ЧЕСТНАЯ ОГОВОРКА: точная форма ответа getBlock с
            # transactionDetails="accounts" не проверена вживую заранее
            # (сетевой доступ к Solana из песочницы заблокирован) --
            # пробуем оба правдоподобных пути (accountKeys либо под
            # message, либо прямо в transaction), не падаем молча, если
            # ни один не сработал (просто не найдём POOL_ADDRESS и
            # пропустим транзакцию, ничего не выдумывая).
            account_keys = (txn.get("message") or {}).get("accountKeys") or txn.get("accountKeys") or []
            keys_flat = [k.get("pubkey") if isinstance(k, dict) else k for k in account_keys]
            if POOL_ADDRESS not in keys_flat:
                continue
            fake_tx = {
                "slot": slot, "blockTime": block_time,
                "transaction": {"signatures": tx.get("transaction", {}).get("signatures", [None]),
                                 "message": {"accountKeys": account_keys}},
                "meta": meta,
            }
            trade = extract_trade(fake_tx, TOKEN_MINT)
            if trade:
                trades.append(trade)
        if block_time and block_time - ANCHOR_TIME >= WINDOW_S:
            break
    coverage_s = max_time_covered - ANCHOR_TIME
    meta_out = {"n_real_slots_in_range": len(real_slots), "n_blocks_scanned": n_blocks_scanned,
                "n_block_errors": n_block_errors, "coverage_seconds_after": coverage_s,
                "target_coverage_seconds": WINDOW_S, "n_txs_seen_total": n_txs_seen_total,
                "n_pool_trades_found": len(trades)}
    if len(trades) == 0 and n_txs_seen_total > 0:
        meta_out["ZERO_TRADES_DIAGNOSTIC"] = (
            "0 сделок пула найдено при {} просканированных транзакциях -- это может быть реальным "
            "(в узком окне после покупки просто не было других свопов) ИЛИ ошибкой формата ответа "
            "getBlock(transactionDetails='accounts'), не проверенного вживую заранее. Смотри "
            "first_block_shape_sample ниже, чтобы отличить одно от другого.".format(n_txs_seen_total)
        )
    meta_out["first_block_shape_sample"] = first_block_shape_sample
    return trades, meta_out


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "anchor_signature": ANCHOR_SIGNATURE, "anchor_slot": ANCHOR_SLOT, "anchor_time_unix": ANCHOR_TIME,
                 "measured_slot_time_s": MEASURED_SLOT_TIME_S,
                 "measurement_note": "измерено из покупок 1 и 3 этапа A (реальные слот/время), не константа с другой сети"}
    t0 = time.time()

    print("[narrow] собираем ДО покупки (before=якорь, дёшево)...")
    before_trades, before_meta = collect_before(t0)
    out["before_collection"] = before_meta
    print(f"[narrow] ДО: {len(before_trades)} сделок декодировано, покрытие {before_meta['coverage_seconds_before']:.0f}с из {WINDOW_S}с")

    print("[narrow] собираем ПОСЛЕ покупки (getBlocks/getBlock по номеру слота)...")
    after_trades, after_meta = collect_after(t0)
    out["after_collection"] = after_meta
    print(f"[narrow] ПОСЛЕ: {len(after_trades)} сделок декодировано, покрытие {after_meta['coverage_seconds_after']:.0f}с из {WINDOW_S}с")

    if after_meta["coverage_seconds_after"] < WINDOW_S * 0.8 or before_meta["coverage_seconds_before"] < WINDOW_S * 0.8:
        out["HONEST_COVERAGE_WARNING"] = (
            f"Окно НЕ покрыто полностью на бюджете: до={before_meta['coverage_seconds_before']:.0f}с/"
            f"{WINDOW_S}с, после={after_meta['coverage_seconds_after']:.0f}с/{WINDOW_S}с -- "
            "дальнейшие числа честно ограничены этим покрытием, не выдумываем недостающее."
        )
        print("[narrow] " + out["HONEST_COVERAGE_WARNING"])

    all_trades = before_trades + after_trades
    all_trades.sort(key=lambda t: t["block_time_unix"] or 0)
    out["n_trades_total"] = len(all_trades)
    out["all_trades"] = all_trades

    others = [t for t in all_trades if not t["is_our_wallet"]]
    unique_addresses = sorted({t["signer"] for t in others if t.get("signer")})
    out["n_unique_other_addresses"] = len(unique_addresses)
    out["unique_other_addresses"] = unique_addresses

    wave = {}
    for w in [30, 60, 180, 300]:
        n_before = sum(1 for t in others if ANCHOR_TIME - w <= (t["block_time_unix"] or 0) < ANCHOR_TIME)
        n_after = sum(1 for t in others if ANCHOR_TIME < (t["block_time_unix"] or 0) <= ANCHOR_TIME + w)
        wave[f"window_{w}s"] = {"n_other_trades_before": n_before, "n_other_trades_after": n_after}
    others_after_sorted = sorted([t for t in others if (t["block_time_unix"] or 0) > ANCHOR_TIME],
                                  key=lambda t: t["block_time_unix"])
    if others_after_sorted:
        first = others_after_sorted[0]
        wave["seconds_to_first_other_trade_after"] = first["block_time_unix"] - ANCHOR_TIME
        wave["slot_of_first_other_trade_after"] = first["slot"]
        wave["signature_of_first_other_trade_after"] = first["signature"]
    else:
        wave["seconds_to_first_other_trade_after"] = None
    out["wave_detection"] = wave

    price_horizons = []
    for h in HORIZONS_S:
        target = ANCHOR_TIME + h
        candidates = [t for t in all_trades if abs((t["block_time_unix"] or 0) - target) <= 60]
        if not candidates:
            price_horizons.append({"horizon_s": h, "status": "нет реальной сделки в пределах 60с от цели в собранном окне"})
            continue
        nearest = min(candidates, key=lambda t: abs((t["block_time_unix"] or 0) - target))
        anchor_trade = min(all_trades, key=lambda t: abs((t["block_time_unix"] or 0) - ANCHOR_TIME)) if all_trades else None
        pct = None
        if anchor_trade and anchor_trade["price_other_per_token"]:
            pct = (nearest["price_other_per_token"] - anchor_trade["price_other_per_token"]) / anchor_trade["price_other_per_token"] * 100
        price_horizons.append({
            "horizon_s": h, "target_time_unix": target, "nearest_trade_time_unix": nearest["block_time_unix"],
            "gap_to_target_s": abs((nearest["block_time_unix"] or 0) - target), "slot": nearest["slot"],
            "price": nearest["price_other_per_token"], "pct_change_from_entry": pct, "signature": nearest["signature"],
        })
    out["price_horizons_from_real_swaps"] = price_horizons

    out["total_runtime_s"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[narrow] записано {OUT_PATH}")


if __name__ == "__main__":
    main()
