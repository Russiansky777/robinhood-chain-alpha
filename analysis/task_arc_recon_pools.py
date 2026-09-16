#!/usr/bin/env python3
"""Arc Mainnet -- поиск реальных пулов/активности за окно, соответствующее
"первому дню" (chain live с genesis 2026-05-12, но публичный mainnet
открыт СЕГОДНЯ, 2026-09-16 -- ищем активность именно за последние часы,
не всю историю). RPC режет eth_getLogs диапазон ("requested range too
large" на 50000 блоков в прошлом прогоне) -- самобисектящийся чанкинг,
начиная с малого шага, чтобы найти реальный лимит, а не гадать."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


INIT_TOPIC0 = keccak_topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")


def rpc(method: str, params: list, timeout: int = 25) -> dict:
    resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                          headers={"Content-Type": "application/json"}, timeout=timeout)
    return resp.json()


def find_max_chunk(address: str, topic0: str, latest: int) -> int:
    """Пробует убывающие размеры диапазона, пока eth_getLogs реально не
    ответит успехом -- реальный лимит провайдера, не предположение."""
    for size in (20000, 10000, 5000, 2000, 1000, 500, 200, 100, 50):
        lo = max(0, latest - size)
        body = rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(latest),
                                     "address": address, "topics": [topic0]}])
        if "error" not in body:
            return size
    return 50


def chunked_get_logs(address: str, topic0: str, from_block: int, to_block: int, chunk: int) -> list:
    out = []
    block = from_block
    n_calls = 0
    while block <= to_block and n_calls < 60:  # честный потолок вызовов на эту функцию
        end = min(block + chunk - 1, to_block)
        body = rpc("eth_getLogs", [{"fromBlock": hex(block), "toBlock": hex(end),
                                     "address": address, "topics": [topic0]}])
        n_calls += 1
        if "error" in body:
            out.append({"_chunk_error": body["error"], "from": block, "to": end})
        else:
            out.extend(body.get("result", []))
        block = end + 1
    return out


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rpc": RPC}
    latest = int(rpc("eth_blockNumber", [])["result"], 16)
    result["latest_block"] = latest

    init_chunk = find_max_chunk(POOL_MANAGER, INIT_TOPIC0, latest)
    result["max_working_chunk_for_initialize"] = init_chunk

    # окно "первого дня" -- последние 24ч публичного mainnet по факту
    # 0.5с/блок ~ 172800 блоков/сутки; честно ограничим до последних ~6ч
    # (~43200 блоков), чтобы уложиться в разумное число вызовов при
    # найденном реальном чанке.
    window_blocks = 43200
    from_block = max(0, latest - window_blocks)
    n_chunks_needed = -(-window_blocks // init_chunk)  # ceil
    result["window"] = {"from_block": from_block, "to_block": latest,
                         "window_blocks": window_blocks, "chunk_size": init_chunk,
                         "n_chunks_needed_estimate": n_chunks_needed}

    if n_chunks_needed > 55:
        # честно уменьшаем окно, чтобы не выйти за наш собственный
        # потолок вызовов (60) -- не выдаём частичный скан за полный.
        window_blocks = init_chunk * 50
        from_block = max(0, latest - window_blocks)
        result["window"]["adjusted_down_to_blocks"] = window_blocks
        result["window"]["reason"] = "исходное окно требовало больше вызовов, чем самоограничение скрипта"

    init_events = chunked_get_logs(POOL_MANAGER, INIT_TOPIC0, from_block, latest, init_chunk)
    real_init = [e for e in init_events if "_chunk_error" not in e]
    errors_init = [e for e in init_events if "_chunk_error" in e]
    result["initialize_events"] = {
        "n_found": len(real_init), "n_chunk_errors": len(errors_init),
        "pools": [{
            "pool_id": e["topics"][1], "currency0": "0x" + e["topics"][2][-40:],
            "currency1": "0x" + e["topics"][3][-40:], "block_number": int(e["blockNumber"], 16),
            "tx_hash": e["transactionHash"],
        } for e in real_init],
    }

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    Path(__file__).parent.parent.joinpath("data", "task_arc_recon_pools_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
