#!/usr/bin/env python3
"""Владелец, ночное задание п.3: цена скорости конкурента, только по
уже известным подписям, без сканов.

data/solana_fast_buyers_priority2_result.json УЖЕ содержит (прошлая
сессия, локальный пересчёт без новых RPC): проверку внешнего
утверждения по подписи DT8/CC (чаевые LandX 1.800007105 SOL -- совпало
точно) и медианы (приоритет+чаевые) в SOL и % от размера сделки по
позициям 1/2/3 на всех 42 быстрых покупках. Этого узла отчёта -- честно
переиспользуем, НЕ пересчитываем заново.

Единственное, чего не было раньше и что владелец просит явно сейчас --
CU price И CU used (использованные, не только запрошенный лимит) для
самой сигнатуры DT8. Это требует ОДНОГО свежего getTransaction (не
скан, ровно та же уже известная подпись) -- meta.computeUnitsConsumed
даёт факт использованных CU, ComputeBudget-инструкция даёt CU price/
запрошенный лимит (уже извлекалось extract_compute_budget)."""
from __future__ import annotations

import json
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_batch_fee_change_first_trade import extract_compute_budget  # noqa: E402
from solana_entry_log import tx_signers, WSOL  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_point3_speed_cost.json"
PRIORITY2_PATH = REPO_ROOT / "data" / "solana_fast_buyers_priority2_result.json"

DT8_SIG = "65WA3e1VhP8Ynsyg1Jv7onsvDciZSFB7zNzwvpHXQ56gzg4KgqteQr57qKX8q8hTBehyW6mkx2E8sKN66oHQ1Q6t"
CLAIMED_TIP_SOL = 1.800007105


def account_keys(tx: dict) -> list[str]:
    la = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys = tx["transaction"]["message"]["accountKeys"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    return keys + list(la.get("writable") or []) + list(la.get("readonly") or [])


def non_swap_sol_transfers(tx: dict, signer: str) -> list[dict]:
    """SOL-переводы получателям, у которых баланс вырос, а сам он -- не
    подписант (эвристика 'не своп, а перевод/чаевые', та же, что
    использовалась раньше этой сессией для приоритетного анализа)."""
    meta = tx.get("meta") or {}
    keys = account_keys(tx)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    signers = set(tx_signers(tx))
    out = []
    for i, key in enumerate(keys):
        if key in signers or i >= len(pre) or i >= len(post):
            continue
        delta = (post[i] - pre[i]) / 1e9
        if delta > 0:
            out.append({"recipient": key, "amount_sol": round(delta, 9)})
    return out


def main() -> None:
    out: dict = {"signature": DT8_SIG}

    tx = fp.get_transaction(DT8_SIG)
    if tx is None:
        out["HONEST_ANSWER"] = "getTransaction вернул null для DT8_SIG"
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        print("[point3] " + out["HONEST_ANSWER"], flush=True)
        return

    meta = tx.get("meta") or {}
    signers = tx_signers(tx)
    signer = signers[0] if signers else None
    fee_sol = (meta.get("fee") or 0) / 1e9
    cb = extract_compute_budget(tx)
    cu_used = meta.get("computeUnitsConsumed")
    cu_price = cb.get("compute_unit_price_microlamports")
    cu_limit_requested = cb.get("compute_unit_limit")
    priority_fee_sol_by_used = (D(cu_price or 0) * D(cu_used or 0) / D(10**6) / D(10**9)) if (cu_price and cu_used) else None

    transfers = non_swap_sol_transfers(tx, signer) if signer else []
    tip_rows = [t for t in transfers if abs(t["amount_sol"] - CLAIMED_TIP_SOL) < 1e-6]

    out.update({
        "signer": signer, "network_fee_sol": fee_sol,
        "cu_price_microlamports": cu_price, "cu_limit_requested": cu_limit_requested,
        "cu_used": cu_used,
        "priority_fee_sol_computed_from_cu_used": float(priority_fee_sol_by_used) if priority_fee_sol_by_used is not None else None,
        "HONEST_NOTE_priority_fee": (
            "computeUnitsConsumed отсутствует в RPC-ответе -- CU used не восстановить, "
            "приоритетная комиссия по факту не считается (только по CU_LIMIT см. отдельно)"
            if cu_used is None else None),
        "non_swap_sol_transfers": transfers,
        "claimed_tip_sol": CLAIMED_TIP_SOL,
        "tip_found_exact_match": len(tip_rows) > 0,
        "tip_rows_matched": tip_rows,
    })

    if PRIORITY2_PATH.exists():
        prior = json.loads(PRIORITY2_PATH.read_text())
        out["cached_42_fast_buyers_analysis"] = {
            "source": "data/solana_fast_buyers_priority2_result.json (прошлая сессия, локальный пересчёт, без новых RPC)",
            "dt8_akbot_external_claim_check": prior.get("dt8_akbot_external_claim_check"),
            "position_table_priority_plus_tips": prior.get("position_table"),
            "n_rows": prior.get("n_rows"),
        }
    else:
        out["HONEST_NOTE_42_fast_buyers"] = "data/solana_fast_buyers_priority2_result.json не найден -- медианы по 42 покупкам недоступны"

    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[point3] fee={fee_sol} cu_price={cu_price} cu_used={cu_used} "
          f"non_swap_transfers={len(transfers)} tip_match={out['tip_found_exact_match']}", flush=True)
    fp._git_commit_progress("point3_speed_cost", [OUT_PATH])


if __name__ == "__main__":
    main()
