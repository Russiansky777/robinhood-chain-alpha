#!/usr/bin/env python3
"""Задача 5 (владелец, измерение реакции конкурента, кандидат
0xf4c3fe75... -- ПОДТВЕРЖДЁН как замкнутый 2-пуловый арбитраж по
реальным ERC20 Transfer, см. ниже честную сверку с Swap-log-дельтами):
"найди изменения всех участвующих пулов за 200 предыдущих блоков и до
позиции конкурента в его блоке. Покажи ближайшие изменения-кандидаты и
расстояние до конкурента -- пока без объявления доказанной причины."

ВАЖНАЯ ЧЕСТНАЯ ПОПРАВКА (обнаружена при разборе ЭТОЙ транзакции):
Swap-log-производный net_trader_flow из full_fund_flow_check()
(task5_v4_item3_in_window_control_trade.py, соглашение "amount>0 у
currency <=> трейдер ОТДАЛ") даёт ЗНАК, противоречащий буквальным
ERC20 Transfer этой же tx: PoolManager реально ПЕРЕДАЛ арбитражеру
6 684 990 raw USDG и 82 242 725 888 898 389 615 349 raw TOK, а
арбитражер реально ЗАПЛАТИЛ НАЗАД 1 769 472 raw USDG и ТОЧНО ТУ ЖЕ
сумму TOK -- т.е. чистый нетто по USDG у арбитражера = +4 915 518 raw
(ПРИБЫЛЬ), а не -4 915 518 (убыток), как показал net_trader_flow.
Здесь и далее используется ТОЛЬКО буквальный ERC20-перевод (не
интерпретация знака Swap-события) -- см. "amount_in_start_token_raw"/
"amount_out_end_token_raw" ниже, посчитанные из
all_transfers_in_receipt, не из decoded Swap amount0/amount1.

read-only, не трогает контракт/Sender/очереди/бюджет, не запускает
LIVE."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _chunked_get_logs, topic0  # noqa: E402

TARGET_BLOCK = 62371951
TARGET_TX_INDEX = 3
LOOKBACK_BLOCKS = 200
FROM_BLOCK = TARGET_BLOCK - LOOKBACK_BLOCKS

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOL_IDS = [
    "0x335b6a9d35d99442aa8e67f817f0d07f1970f17898c099ac20e5ab0977fd4135",
    "0xdabec00bfedb7d75a654f6a2e8a5281a7ecd7e1eef4e4a6ecc4265b7cac4eab9",
]
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
TOK = "0x34031e1c8b4e6bc8f9e7d2cd88ca6aad08772f29"

SWAP_TOPIC0 = topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
MODIFY_LIQUIDITY_TOPIC0 = topic0("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")

# Буквальные ERC20-переводы этой конкретной tx (уже установлены в первом
# результате, task5_v4_tx_f4c3fe75_first_result.py) -- НЕ decoded Swap
# amount0/amount1 (см. докстринг про честную поправку знака выше).
REAL_START_AMOUNT_USDG_RAW = 1_769_472   # арбитражер -> PoolManager (плечо 1: USDG -> TOK)
REAL_END_AMOUNT_USDG_RAW = 6_684_990     # PoolManager -> арбитражер (плечо 2: TOK -> USDG)
REAL_NET_PROFIT_USDG_RAW_BEFORE_GAS = REAL_END_AMOUNT_USDG_RAW - REAL_START_AMOUNT_USDG_RAW


def is_before_competitor(block_number: int, tx_index: int) -> bool:
    if block_number < TARGET_BLOCK:
        return True
    if block_number == TARGET_BLOCK and tx_index < TARGET_TX_INDEX:
        return True
    return False


def main() -> None:
    result: dict = {
        "target_block": TARGET_BLOCK, "target_tx_index": TARGET_TX_INDEX,
        "lookback_blocks": LOOKBACK_BLOCKS, "from_block": FROM_BLOCK,
        "real_start_amount_usdg_raw": REAL_START_AMOUNT_USDG_RAW,
        "real_end_amount_usdg_raw": REAL_END_AMOUNT_USDG_RAW,
        "real_net_profit_usdg_raw_before_gas": REAL_NET_PROFIT_USDG_RAW_BEFORE_GAS,
        "real_net_profit_usdg_before_gas": REAL_NET_PROFIT_USDG_RAW_BEFORE_GAS / 1e6,
    }

    events_by_pool: dict[str, list[dict]] = {}
    for pool_id in POOL_IDS:
        events = []
        for topic0_hex in (SWAP_TOPIC0, MODIFY_LIQUIDITY_TOPIC0):
            logs = list(_chunked_get_logs(FROM_BLOCK, TARGET_BLOCK, topics=[topic0_hex, pool_id],
                                            address=POOL_MANAGER, chunk_size=2000))
            for log in logs:
                bn = int(log["blockNumber"], 16)
                txi = int(log["transactionIndex"], 16)
                events.append({
                    "kind": "Swap" if topic0_hex == SWAP_TOPIC0 else "ModifyLiquidity",
                    "block_number": bn, "tx_index": txi, "tx_hash": log["transactionHash"],
                    "log_index": int(log["logIndex"], 16),
                    "blocks_before_competitor": TARGET_BLOCK - bn,
                })
        events = [e for e in events if is_before_competitor(e["block_number"], e["tx_index"])]
        events.sort(key=lambda e: (e["block_number"], e["tx_index"], e["log_index"]))
        events_by_pool[pool_id] = events

    result["n_events_by_pool"] = {pid: len(evs) for pid, evs in events_by_pool.items()}
    result["nearest_candidates_by_pool"] = {
        pid: evs[-10:] for pid, evs in events_by_pool.items()  # 10 БЛИЖАЙШИХ к конкуренту (уже отсортировано по возрастанию -> хвост)
    }
    # Единственное ближайшее событие по ОБОИМ пулам вместе (самый вероятный кандидат-причина)
    all_events = [
        {**e, "pool_id": pid} for pid, evs in events_by_pool.items() for e in evs
    ]
    all_events.sort(key=lambda e: (e["block_number"], e["tx_index"], e["log_index"]))
    result["single_nearest_candidate_overall"] = all_events[-1] if all_events else None
    result["all_events_chronological_tail_15"] = all_events[-15:]

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_tx_f4c3fe75_pool_change_scan_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[pool_change_scan] сохранено: {out_path}")


if __name__ == "__main__":
    main()
