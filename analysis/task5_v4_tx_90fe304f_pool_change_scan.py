#!/usr/bin/env python3
"""Продолжение разбора 0x90fe304f... (кандидат 1, ПОДТВЕРЖДЁН как
межвенчурный арбитраж -- V3-стиля пул 0x654e4143... + V4-пул
0x0112f42d..., см. task5_v4_tx_90fe304f_full_logs_and_venue.py: сам
контрагент реально эмитировал Swap(address,address,int256,int256,
uint160,uint128,int24) -- это НЕ гипотеза, а декодированное событие).

ЧЕСТНАЯ ПОПРАВКА ЗНАКА (см. task5_v4_tx_f4c3fe75_pool_change_scan.py,
тот же вывод, теперь дважды независимо подтверждён): decoded amount0/
amount1 у V4 PoolManager.Swap имеют ЗНАК "положительно = пул ОТДАЛ
трейдеру", т.е. ПРОТИВОПОЛОЖНЫЙ прежнему допущению net_trader_flow в
full_fund_flow_check(). Реальный (буквальный ERC20 Transfer) размер и
поток этой сделки:
  V3-плечо (пул 0x654e4143...): арбитражёр заплатил 196 293 427 raw
    USDG, получил 2 081 078 990 030 975 552 raw token1 (0xdf0992e4...).
  V4-плечо (пул 0x0112f42d...): арбитражёр заплатил ТУ ЖЕ сумму
    token1, получил 197 690 813 raw USDG.
  Итог: чистая прибыль 1 397 386 raw USDG (0x5fc536...->профит-коллектор
  0x11854ce19d..., подтверждено переводом в первом результате).

Здесь: скан изменений ОБОИХ пулов за 100 предыдущих блоков (владелец,
кандидат 1: "за предыдущие 100 блоков и до его позиции внутри
собственного блока") -- V4-пул через Swap/ModifyLiquidity (полная
поддержка), V3-стиля пул -- ТОЛЬКО через его собственный Swap-топик
(тот же, что уже декодирован) -- ModifyLiquidity/Mint/Burn V3-пула НЕ
сканируется (честно объявленный пробел покрытия, не причина отбросить
пример)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _chunked_get_logs, topic0  # noqa: E402

TARGET_BLOCK = 62373271
TARGET_TX_INDEX = 5
LOOKBACK_BLOCKS = 100
FROM_BLOCK = TARGET_BLOCK - LOOKBACK_BLOCKS

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
V4_POOL_ID = "0x0112f42d2da7164a1e224ef55be60c10ef9607cabdadbc003123daa500750358"
V3_STYLE_POOL_ADDRESS = "0x654e4143e82a5824445ade0824351c2a9acd95a8"

SWAP_TOPIC0 = topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
MODIFY_LIQUIDITY_TOPIC0 = topic0("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")
V3_SWAP_TOPIC0 = topic0("Swap(address,address,int256,int256,uint160,uint128,int24)")


def is_before_competitor(block_number: int, tx_index: int) -> bool:
    if block_number < TARGET_BLOCK:
        return True
    if block_number == TARGET_BLOCK and tx_index < TARGET_TX_INDEX:
        return True
    return False


def to_signed(v: int, bits: int = 256) -> int:
    return v - (1 << bits) if v >= (1 << (bits - 1)) else v


def main() -> None:
    result: dict = {"target_block": TARGET_BLOCK, "target_tx_index": TARGET_TX_INDEX,
                     "lookback_blocks": LOOKBACK_BLOCKS, "from_block": FROM_BLOCK}

    v4_events = []
    for topic0_hex in (SWAP_TOPIC0, MODIFY_LIQUIDITY_TOPIC0):
        logs = list(_chunked_get_logs(FROM_BLOCK, TARGET_BLOCK, topics=[topic0_hex, V4_POOL_ID],
                                        address=POOL_MANAGER, chunk_size=2000))
        for log in logs:
            bn, txi = int(log["blockNumber"], 16), int(log["transactionIndex"], 16)
            v4_events.append({"kind": "Swap" if topic0_hex == SWAP_TOPIC0 else "ModifyLiquidity",
                               "block_number": bn, "tx_index": txi, "tx_hash": log["transactionHash"],
                               "log_index": int(log["logIndex"], 16), "blocks_before_competitor": TARGET_BLOCK - bn})
    v4_events = [e for e in v4_events if is_before_competitor(e["block_number"], e["tx_index"])]
    v4_events.sort(key=lambda e: (e["block_number"], e["tx_index"], e["log_index"]))
    result["v4_pool_id"] = V4_POOL_ID
    result["v4_n_events"] = len(v4_events)
    result["v4_nearest_10"] = v4_events[-10:]

    v3_logs = list(_chunked_get_logs(FROM_BLOCK, TARGET_BLOCK, topics=[V3_SWAP_TOPIC0],
                                       address=V3_STYLE_POOL_ADDRESS, chunk_size=2000))
    v3_events = []
    for log in v3_logs:
        bn, txi = int(log["blockNumber"], 16), int(log["transactionIndex"], 16)
        if not is_before_competitor(bn, txi):
            continue
        data = log["data"][2:]
        words = [data[i:i + 64] for i in range(0, len(data), 64)]
        v3_events.append({
            "kind": "V3StyleSwap", "block_number": bn, "tx_index": txi, "tx_hash": log["transactionHash"],
            "log_index": int(log["logIndex"], 16), "blocks_before_competitor": TARGET_BLOCK - bn,
            "amount0": to_signed(int(words[0], 16)), "amount1": to_signed(int(words[1], 16)),
            "sqrt_price_x96": int(words[2], 16), "tick": to_signed(int(words[4], 16), 24) if len(words) > 4 else None,
        })
    v3_events.sort(key=lambda e: (e["block_number"], e["tx_index"], e["log_index"]))
    result["v3_style_pool_address"] = V3_STYLE_POOL_ADDRESS
    result["v3_n_events"] = len(v3_events)
    result["v3_nearest_10"] = v3_events[-10:]
    result["v3_coverage_gap_note"] = ("сканирован ТОЛЬКО Swap-топик этого адреса -- ModifyLiquidity/Mint/Burn "
                                       "V3-стиля события здесь НЕ декодируются (нет готового парсера в этом "
                                       "проекте) -- если реальная причина была изменением ликвидности (Mint/Burn), "
                                       "а не свопом, она НЕ будет видна в v3_nearest_10 выше; это пробел покрытия, "
                                       "не основание отбрасывать пример")

    all_events = [{**e, "pool": "v4"} for e in v4_events] + [{**e, "pool": "v3_style"} for e in v3_events]
    all_events.sort(key=lambda e: (e["block_number"], e["tx_index"], e["log_index"]))
    result["single_nearest_candidate_overall"] = all_events[-1] if all_events else None
    result["all_events_chronological_tail_15"] = all_events[-15:]

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_tx_90fe304f_pool_change_scan_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[pool_change_scan] сохранено: {out_path}")


if __name__ == "__main__":
    main()
