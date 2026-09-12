#!/usr/bin/env python3
"""Диагностика (НЕ трогает фид, cooldown не расходуется): смоук-тест
часа наблюдения (data/task5_v4_observation_hour_result.json,
2026-09-12) реально показал ПУСТЫЕ routes на ВСЕХ 6 проверках --
причина найдена честно, не предположена: RPC-ошибка

  {"code": 3, "message": "execution reverted",
   "data": "0x6190b2b0...7a5ed734<poolId>..."}

Селекторы пересчитаны и сверены (eth_utils.function_signature_to_4byte_selector):
  0x6190b2b0 == UnexpectedRevertBytes(bytes)  -- обёртка V4Quoter
    (реальный исходник, Uniswap/v4-periphery/src/libraries/QuoterRevert.sol,
    сверено WebFetch в этой сессии) для НЕ-ожидаемого (не QuoteSwap)
    revert-а внутри симуляции.
  0x7a5ed734 == NotEnoughLiquidity(bytes32)  -- реальная причина
    (полный перебор кандидатных сигнатур, побайтовое совпадение).

Значит: на РЕАЛЬНОМ ТЕКУЩЕМ состоянии (не на исторических блоках,
которые мы проверяли раньше в этой сессии) наша фиксированная сетка
размеров (1-30 USDG / 0.02-0.3 ETH) НЕ исполнима -- пулам не хватает
ликвидности. Этот скрипт находит РЕАЛЬНЫЙ текущий потолок размера
(бинарным спуском по гораздо меньшим величинам), чтобы понять,
пересохла ли ликвидность совсем или просто сдвинулась цена."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call  # noqa: E402
from task5_v4_pool_math import PoolKey  # noqa: E402
from task5_v4_quote_replay import quote_exact_input_single  # noqa: E402

NATIVE = "0x0000000000000000000000000000000000000000"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
MOSIAI = "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a"
HOOK_ETH_MOSIAI = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"

POOL_A_USDG_MOSIAI = PoolKey(USDG, MOSIAI, 70000, 4)
POOL_B_USDG_MOSIAI = PoolKey(USDG, MOSIAI, 70000, 3)
POOL_ETH_MOSIAI = PoolKey(NATIVE, MOSIAI, 0, 200, HOOK_ETH_MOSIAI)
POOL_USDG_MOSIAI_75000 = PoolKey(USDG, MOSIAI, 75000, 2)
POOL_ETH_USDG = PoolKey(NATIVE, USDG, 100, 1)

# Гораздо более мелкая сетка, чем в наблюдении -- от 1 raw-единицы.
SIZE_GRID_USDG_RAW = [1, 10, 100, 1_000, 10_000, 100_000, 1_000_000, 5_000_000, 10_000_000, 30_000_000]
SIZE_GRID_ETH_WEI = [10**i for i in range(9, 19)] + [int(0.05e18), int(0.1e18), int(0.3e18)]


def probe_leg(key: PoolKey, zero_for_one: bool, amount_in: int, block_number: int) -> dict:
    try:
        out = quote_exact_input_single(key, zero_for_one, amount_in, block_number)
        return {"amount_in": amount_in, "ok": True, "amount_out": out}
    except Exception as exc:  # noqa: BLE001
        return {"amount_in": amount_in, "ok": False, "error": str(exc)[:300]}


def main() -> None:
    latest = int(_rpc_call("eth_blockNumber", []), 16)
    result = {"block_number": latest, "usdg_route_leg_a": [], "eth_route_leg_a": []}

    print(f"[diag_liquidity] блок {latest}")
    print("[diag_liquidity] проба плеча A маршрута USDG/MOSIAI (пул tickSpacing=4)...")
    for amount in SIZE_GRID_USDG_RAW:
        row = probe_leg(POOL_A_USDG_MOSIAI, True, amount, latest)
        result["usdg_route_leg_a"].append(row)
        print(f"  in={amount} -> {row}")

    print("[diag_liquidity] проба плеча A маршрута ETH-хук (пул ETH/MOSIAI)...")
    for amount in SIZE_GRID_ETH_WEI:
        row = probe_leg(POOL_ETH_MOSIAI, True, amount, latest)
        result["eth_route_leg_a"].append(row)
        print(f"  in={amount} -> {row}")

    out_path = Path(__file__).parent.parent / "data" / "task5_v4_diag_current_liquidity_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"[diag_liquidity] сохранено: {out_path}")


if __name__ == "__main__":
    main()
