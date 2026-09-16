#!/usr/bin/env python3
"""Диагностика: два прогона task_arc_lp_fee_census.py подряд зависли за
пределами 30-минутного job timeout GH Actions, ни один не дал ни строки
результата. Прежде чем гадать -- измерить РЕАЛЬНОЕ время каждой
операции по отдельности (чтение локального реестра, разные формы
eth_getLogs, простой eth_blockNumber), чтобы найти, что именно висит."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
LOCAL_REGISTRY_PATHS = [
    Path("/home/bot/robinhood-chain-alpha/data/task_arc_recon_pools_result.json"),
    Path(__file__).parent.parent.joinpath("data", "task_arc_recon_pools_result.json"),
]


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


INIT_TOPIC0 = keccak_topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")


def timed_rpc(method: str, params: list, timeout: int = 15) -> dict:
    t0 = time.time()
    try:
        resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              headers={"Content-Type": "application/json"}, timeout=timeout)
        elapsed = time.time() - t0
        body = resp.json()
        n_results = len(body.get("result", [])) if isinstance(body.get("result"), list) else None
        return {"elapsed_s": elapsed, "http_status": resp.status_code,
                "has_error": "error" in body, "error": body.get("error"), "n_results": n_results}
    except Exception as exc:  # noqa: BLE001
        return {"elapsed_s": time.time() - t0, "exception": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # 1. Чтение локального реестра
    t0 = time.time()
    registry = None
    used_path = None
    for p in LOCAL_REGISTRY_PATHS:
        if p.exists():
            raw = p.read_text()
            registry = json.loads(raw)
            used_path = str(p)
            break
    result["step1_read_local_registry"] = {
        "elapsed_s": time.time() - t0, "path": used_path,
        "found": registry is not None,
        "n_pools": len(registry.get("initialize_events", {}).get("pools", [])) if registry else None,
        "file_size_bytes": len(raw) if registry else None,
    }
    reg_pools = registry.get("initialize_events", {}).get("pools", []) if registry else []

    # 2. 5x eth_blockNumber -- голая сетевая латентность
    bn_timings = []
    latest = None
    for _ in range(5):
        r = timed_rpc("eth_blockNumber", [])
        bn_timings.append(r["elapsed_s"])
        if not r.get("has_error") and not r.get("exception"):
            latest = int(requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}).json()["result"], 16)
    result["step2_blockNumber_5x"] = {"timings_s": bn_timings}

    latest = latest or 21230000

    # 3. Один eth_getLogs -- Initialize, 5000 блоков (как шаг 1 census)
    if reg_pools:
        min_init = min(p["block_number"] for p in reg_pools)
    else:
        min_init = latest - 100000
    r3 = timed_rpc("eth_getLogs", [{"fromBlock": hex(min_init), "toBlock": hex(min_init + 5000),
                                     "address": POOL_MANAGER, "topics": [INIT_TOPIC0]}], timeout=25)
    result["step3_getLogs_initialize_5000blocks"] = r3

    # 4. Один eth_getLogs -- Swap, КОНКРЕТНЫЙ pool_id, широкий диапазон (как count_swaps_for_pool)
    if reg_pools:
        sample_pool_id = reg_pools[0]["pool_id"]
        sample_init_block = reg_pools[0]["block_number"]
        window_end = min(latest, sample_init_block + int(12 * 3600 / 0.506))
        r4 = timed_rpc("eth_getLogs", [{"fromBlock": hex(sample_init_block), "toBlock": hex(window_end),
                                         "address": POOL_MANAGER, "topics": [SWAP_TOPIC0, sample_pool_id]}], timeout=25)
        r4["pool_id"] = sample_pool_id
        r4["range_blocks"] = window_end - sample_init_block
    else:
        r4 = {"skipped": "нет пулов в реестре"}
    result["step4_getLogs_swap_specific_pool_wide_range"] = r4

    # 5. Тот же тест, но диапазон поменьше (20000 блоков от конца окна назад) -- сравнить
    if reg_pools:
        r5 = timed_rpc("eth_getLogs", [{"fromBlock": hex(max(0, latest - 20000)), "toBlock": hex(latest),
                                         "address": POOL_MANAGER, "topics": [SWAP_TOPIC0, sample_pool_id]}], timeout=25)
        result["step5_getLogs_swap_specific_pool_20000blocks_recent"] = r5

    result["latest_block_used"] = latest
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    Path(__file__).parent.parent.joinpath("data", "task_arc_lp_census_diag_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
