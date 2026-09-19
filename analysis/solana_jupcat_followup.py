#!/usr/bin/env python3
"""Владелец: доп. вопросы по хронологии JUPCAT (data/solana_jupcat_timeline_result.json),
только RPC.

1. Среди подписантов ВСЕХ 33 промежуточных транзакций -- сколько раз
   встречается Fomo Co-signer AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51
   (проверяем ВСЕХ подписантов транзакции, не только fee payer -- прошлый
   скрипт брал только первого), и сколько из НИХ -- покупка минта, сколько
   продажа (по direction того же decode_tx-события, что и раньше).
2. Наша позиция внутри слота 448427775 -- порядок транзакций внутри блока
   строго соответствует порядку в getBlock(...).signatures (Solana
   выполняет транзакции блока в этом порядке); кто из числа
   пул-транзакций в ЭТОМ ЖЕ слоте оказался РАНЬШЕ нас, и их
   compute_unit_price (микролампорт/CU) -- честная попытка увидеть,
   "переплатили" ли они за место впереди."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_buyer200_fast_price import PRIOR_ROOT  # noqa: E402
from solana_batch_fee_change_first_trade import extract_compute_budget  # noqa: E402

sys.path.insert(0, str(PRIOR_ROOT))
import engine  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_PATH = REPO_ROOT / "data" / "solana_jupcat_timeline_result.json"
OUT_PATH = REPO_ROOT / "data" / "solana_jupcat_followup_result.json"
MINT = "AaEhFTX4naHSWSXz9TVe5QgLbtSLT8ZqYJGZzDDcoroh"
FOMO_COSIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
PILOT_BUY_SIG = "2u7xoQkBTAzGK5ksA9NyYgF2sANgKeFwZ7bfYzT2Y3L8h3Z7sANPj32LLrjhSgTcvtAbH7rj3jRW83xGLnvQaPGQ"


def all_signers(tx: dict) -> list[str]:
    keys = tx["transaction"]["message"]["accountKeys"]
    return [k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")]


def mint_direction(tx: dict) -> str | None:
    for e in engine.decode_tx(tx):
        if MINT not in (e.get("m0"), e.get("m1")):
            continue
        ev = e.get("event") or {}
        if e.get("kind") == "cp" and "input_mint" in ev:
            if ev["input_mint"] == MINT:
                return "продажа минта"
            if ev["output_mint"] == MINT:
                return "покупка минта"
        return "направление не определено для этого вида пула (kind=%s)" % e.get("kind")
    return None


def main() -> None:
    src = json.loads(IN_PATH.read_text())
    result: dict = {"mint": MINT, "fomo_cosigner": FOMO_COSIGNER, "pool": src["pool"],
                     "pilot_slot": src["pilot_tx_slot"]}

    # --- 1. Fomo co-signer среди 33 промежуточных ---
    per_tx = []
    n_with_cosigner = 0
    n_cosigner_buy = 0
    n_cosigner_sell = 0
    for r in src["intermediate_trades"]:
        tx = fp.get_transaction(r["signature"])
        if tx is None:
            per_tx.append({"signature": r["signature"], "status": "getTransaction_null"})
            continue
        signers = all_signers(tx)
        has_cosigner = FOMO_COSIGNER in signers
        direction = mint_direction(tx)
        if has_cosigner:
            n_with_cosigner += 1
            if direction == "покупка минта":
                n_cosigner_buy += 1
            elif direction == "продажа минта":
                n_cosigner_sell += 1
        per_tx.append({"signature": r["signature"], "slot": r["slot"], "all_signers": signers,
                        "has_fomo_cosigner": has_cosigner, "direction": direction})
    result["intermediate_with_cosigner_check"] = per_tx
    result["n_intermediate_total"] = len(per_tx)
    result["n_intermediate_with_fomo_cosigner"] = n_with_cosigner
    result["n_cosigner_buys"] = n_cosigner_buy
    result["n_cosigner_sells"] = n_cosigner_sell

    # --- 2. Позиция пилота внутри слота 448427775 ---
    slot = src["pilot_tx_slot"]
    block = fp.get_block_signatures(slot)
    if block is None or "signatures" not in block:
        result["HONEST_NOTE_BLOCK"] = f"getBlock({slot}) не вернул подписи -- позицию в блоке определить не могу."
    else:
        sigs_in_order = block["signatures"]
        try:
            pilot_idx = sigs_in_order.index(PILOT_BUY_SIG)
        except ValueError:
            result["HONEST_NOTE_BLOCK"] = f"Подпись пилота не найдена в списке getBlock({slot}) -- странно, проверить вручную."
            pilot_idx = None
        if pilot_idx is not None:
            result["pilot_index_in_block"] = pilot_idx
            result["n_transactions_in_block"] = len(sigs_in_order)
            # какие из наших "промежуточных" (в этом же слоте, на этом пуле) сигнатур стоят РАНЬШЕ пилота
            same_slot_pool_sigs = {r["signature"] for r in src["intermediate_trades"] if r["slot"] == slot}
            ahead = []
            for sig in sigs_in_order[:pilot_idx]:
                if sig not in same_slot_pool_sigs:
                    continue
                tx = fp.get_transaction(sig)
                cb = extract_compute_budget(tx) if tx else {"compute_unit_limit": None, "compute_unit_price_microlamports": None}
                ahead.append({"signature": sig, "index_in_block": sigs_in_order.index(sig),
                               "compute_unit_limit": cb["compute_unit_limit"],
                               "compute_unit_price_microlamports": cb["compute_unit_price_microlamports"]})
            result["pool_trades_ahead_of_pilot_in_same_slot"] = ahead
            pilot_tx = fp.get_transaction(PILOT_BUY_SIG)
            pilot_cb = extract_compute_budget(pilot_tx) if pilot_tx else {}
            result["pilot_compute_unit_price_microlamports"] = pilot_cb.get("compute_unit_price_microlamports")
            result["pilot_compute_unit_limit"] = pilot_cb.get("compute_unit_limit")

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[jupcat_followup] cosigner: {n_with_cosigner}/{len(per_tx)} (buy={n_cosigner_buy}, sell={n_cosigner_sell}); "
          f"pilot_index_in_block={result.get('pilot_index_in_block')}, "
          f"ahead_on_same_pool={len(result.get('pool_trades_ahead_of_pilot_in_same_slot', []))}", flush=True)


if __name__ == "__main__":
    main()
