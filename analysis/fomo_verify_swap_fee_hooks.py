#!/usr/bin/env python3
"""Владелец (2026-09-17): проверить, что комиссия 11% (fee=110000 из
Initialize) реально применяется на свопе, а не переопределяется хуком
на beforeSwap. Дёшево, напрямую по цепи:
  1. Локально (без RPC): у скольких из пулов с fee=110000 вообще есть
     хук (поле hooks в Initialize != 0x0), и может ли он по битам
     адреса влиять на своп (BEFORE_SWAP_FLAG / BEFORE_SWAP_RETURNS_
     DELTA_FLAG) -- уже посчитано локально ИЗ уже закоммиченного
     data/fomo_resolved_pool_keys_cache.json: 0 из 22 пулов имеют хук
     вообще (hooks=0x0 у всех) -- это уже структурно исключает
     переопределение (V4 core не вызывает хук на своп, если хука нет).
  2. Здесь -- прямая проверка per-swap поля fee из САМОГО события
     Swap (PoolManager.Swap(...,uint24 fee) -- последнее слово data)
     для 10 реальных транзакций входа лидеров через пулы с fee=110000.
     Если хука нет, поле fee в событии ОБЯЗАНО совпадать со статической
     комиссией пула -- это не гипотеза, а прямое следствие кода V4
     core (Pool.swap() использует self.slot0.lpFee, обновляемый хуком
     ТОЛЬКО если пул создан с LPFeeLibrary.DYNAMIC_FEE_FLAG и хук имеет
     соответствующий permission-бит -- ни то, ни другое здесь не
     выполняется). Проверка -- подтверждение, не единственный источник
     истины.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import rpc_call_trading_path, topic0  # noqa: E402
from task5_v4_pool_math import decode_v4_swap_log_data  # noqa: E402
from task5_v4_hook_route_audit import decode_hook_permissions  # noqa: E402

IN_HORIZON_PATH = Path("data/fomo_first_entries_horizon_economics_result.json")
POOL_KEYS_CACHE_PATH = Path("data/fomo_resolved_pool_keys_cache.json")
OUT_PATH = Path("data/fomo_fee_verification_result.json")

RPC = rpc_call_trading_path
SWAP_TOPIC0 = topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
N_SAMPLES = 10


def main() -> None:
    horizon = json.loads(IN_HORIZON_PATH.read_text())
    pool_keys = json.loads(POOL_KEYS_CACHE_PATH.read_text())

    tx_to_pool = {e["tx_hash"]: e["pool_id"] for e in horizon["entries"] if e.get("pool_found")}

    fee_pools = {pid: info for pid, info in pool_keys.items() if info.get("fee") == 110000}
    hook_report = []
    n_hook_set = 0
    n_beforeswap_capable = 0
    for pid, info in fee_pools.items():
        hooks = info.get("hooks", "0x" + "00" * 20)
        is_zero = int(hooks, 16) == 0
        perms = None if is_zero else decode_hook_permissions(hooks)
        can_override = bool(perms and (perms["BEFORE_SWAP_FLAG"] or perms["BEFORE_SWAP_RETURNS_DELTA_FLAG"]))
        if not is_zero:
            n_hook_set += 1
        if can_override:
            n_beforeswap_capable += 1
        hook_report.append({"pool_id": pid, "hooks": hooks, "hook_is_zero": is_zero,
                             "hook_permissions": perms, "can_override_swap_fee": can_override})

    candidates = [(tx, pid) for tx, pid in tx_to_pool.items() if pid in fee_pools]
    sample = candidates[:N_SAMPLES]

    swap_checks = []
    n_match = 0
    n_checked = 0
    for tx_hash, pool_id in sample:
        try:
            receipt = RPC("eth_getTransactionReceipt", [tx_hash])
        except Exception as exc:  # noqa: BLE001
            swap_checks.append({"tx_hash": tx_hash, "pool_id": pool_id, "error": str(exc)})
            continue
        if not receipt:
            swap_checks.append({"tx_hash": tx_hash, "pool_id": pool_id, "error": "no receipt"})
            continue
        matched_log = None
        for log in receipt.get("logs", []):
            topics = log.get("topics") or []
            if topics and topics[0].lower() == SWAP_TOPIC0.lower() and len(topics) >= 2 \
                    and topics[1].lower() == pool_id.lower():
                matched_log = log
                break
        if matched_log is None:
            swap_checks.append({"tx_hash": tx_hash, "pool_id": pool_id, "error": "no matching Swap log in receipt"})
            continue
        decoded = decode_v4_swap_log_data(matched_log["data"])
        n_checked += 1
        matches = decoded["fee"] == 110000
        if matches:
            n_match += 1
        swap_checks.append({
            "tx_hash": tx_hash, "pool_id": pool_id,
            "declared_fee_pips_from_initialize": 110000,
            "actual_fee_pips_from_swap_event": decoded["fee"],
            "matches": matches,
        })

    out = {
        "n_pools_with_fee_110000": len(fee_pools),
        "n_pools_with_any_hook": n_hook_set,
        "n_pools_hook_can_override_swap_fee": n_beforeswap_capable,
        "hook_report": hook_report,
        "n_swap_events_checked": n_checked,
        "n_swap_events_matching_declared_fee": n_match,
        "swap_checks": swap_checks,
        "answer": (f"фактическая комиссия на свопе {n_match} из {n_checked} пулов = 11%"
                   + (f", у остальных {n_checked - n_match} -- отличается (см. swap_checks)"
                      if n_checked > n_match else ", у остальных -- нет остальных (все совпали)")),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print("[fee_verification] " + out["answer"])
    print(f"[fee_verification] хуков среди {len(fee_pools)} пулов с fee=110000: {n_hook_set} ненулевых, "
          f"{n_beforeswap_capable} с правом влиять на своп")


if __name__ == "__main__":
    main()
