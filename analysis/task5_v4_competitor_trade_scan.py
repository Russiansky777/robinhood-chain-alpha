#!/usr/bin/env python3
"""Задача 5, живой пилот, шестой раунд -- контрольная реальная сделка
конкурента (0x1b357e7acd2a32aebfa2de286c9e8e617d39a251) внутри окна
пилота, на маршруте, поддерживаемом нашим V4-исполнителем (чистый V4,
2-3 плеча, старт/конец в USDG или нативном ETH).

Метод: реальные Swap-события PoolManager с sender==арбитражник
(topics[2], индексирован -- Swap эмитится ТОЛЬКО при успешном свопе, не
при revert), сгруппированные по transactionHash (>=2 логов в одной tx --
многоходовый атомарный цикл). Для каждой группы -- сеть валют по
свопам; если начальная/конечная валюта совпадает и это USDG/NATIVE --
кандидат в контрольные примеры. ЧТЕНИЕ (eth_getLogs/eth_call), никакой
подписи/отправки.

Диапазон блоков и стартовое окно -- параметры командной строки (узкое
окно быстрее; расширяется явным повторным запуском, не автоматически
"на всякий случай")."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import os  # noqa: E402
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _chunked_get_logs, _rpc_call, get_transaction_fast, topic0  # noqa: E402
from task5_v4_pool_math import decode_v4_swap_log_data  # noqa: E402

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
KNOWN_ARBITRAGEUR = "0x1b357e7acd2a32aebfa2de286c9e8e617d39a251"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
NATIVE = "0x0000000000000000000000000000000000000000"
SWAP_TOPIC0 = topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")


def scan(from_block: int, to_block: int) -> dict:
    result: dict = {"from_block": from_block, "to_block": to_block}
    sender_topic = "0x" + KNOWN_ARBITRAGEUR[2:].rjust(64, "0").lower()
    print(f"[competitor_scan] eth_getLogs sender=арбитражник, блоки {from_block}..{to_block} "
          f"({to_block - from_block} блоков)...")
    logs = _chunked_get_logs(from_block, to_block, topics=[SWAP_TOPIC0, None, sender_topic],
                              address=POOL_MANAGER, chunk_size=2000)
    result["n_swap_logs"] = len(logs)
    print(f"[competitor_scan] найдено {len(logs)} Swap-логов арбитражника в окне")

    by_tx: dict[str, list[dict]] = defaultdict(list)
    for log in logs:
        by_tx[log["transactionHash"]].append(log)

    multi_leg_candidates = []
    for tx_hash, tx_logs in by_tx.items():
        if len(tx_logs) < 2:
            continue
        tx_logs_sorted = sorted(tx_logs, key=lambda l: int(l["logIndex"], 16))
        legs = []
        for log in tx_logs_sorted:
            decoded = decode_v4_swap_log_data(log["data"])
            legs.append({"pool_id": log["topics"][1], "block": int(log["blockNumber"], 16),
                          "log_index": int(log["logIndex"], 16), **decoded})
        multi_leg_candidates.append({"tx_hash": tx_hash, "block": legs[0]["block"], "n_legs": len(legs), "legs": legs})

    multi_leg_candidates.sort(key=lambda c: c["block"])
    result["n_multi_leg_txs"] = len(multi_leg_candidates)
    print(f"[competitor_scan] из них {len(multi_leg_candidates)} многоходовых (>=2 плеч) транзакций")
    result["multi_leg_candidates_summary"] = [
        {"tx_hash": c["tx_hash"], "block": c["block"], "n_legs": c["n_legs"]} for c in multi_leg_candidates
    ]
    result["multi_leg_candidates_full"] = multi_leg_candidates[:50]  # полные данные -- первые 50 (диагностика)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-block", type=int, required=True)
    ap.add_argument("--to-block", type=int, required=True)
    args = ap.parse_args()

    result = scan(args.from_block, args.to_block)
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_competitor_trade_scan_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[competitor_scan] сохранено: {out_path}")


if __name__ == "__main__":
    main()
