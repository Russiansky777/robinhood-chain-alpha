#!/usr/bin/env python3
"""Глубокая разведка Arc Mainnet -- ПРОДОЛЖЕНИЕ task_arc_recon_probe.py.
RPC теперь РЕАЛЬНО подтверждён (https://rpc.mainnet.arc.io, chain_id
0x13b2=5042, живой -- 4 блока за 2с в прошлом прогоне). Только чтение,
никаких транзакций."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

RPC = "https://rpc.mainnet.arc.io"

# Те же адреса, что на Robinhood Chain (в этом же проекте) -- реальная
# гипотеза: Uniswap деплоит v4 через детерминированный CREATE2 на многих
# чейнах одним и тем же адресом. Уже подтверждено для PoolManager
# (task_arc_recon_probe.py) -- проверяем остальные компоненты того же
# стека тем же методом (eth_getCode), не гадаем.
KNOWN_ROBINHOOD_ADDRESSES = {
    "PoolManager_v4": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
    "V4Quoter": "0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
}

def keccak_topic0(sig: str) -> str:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


INITIALIZE_SIG = "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
SWAP_SIG = "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                          headers={"Content-Type": "application/json"}, timeout=timeout)
    return resp.json()


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rpc": RPC}

    init_topic0 = keccak_topic0(INITIALIZE_SIG)
    swap_topic0 = keccak_topic0(SWAP_SIG)
    result["computed_topics"] = {"initialize": init_topic0, "swap_v4": swap_topic0}

    # 1. Код на известных Robinhood-адресах -- реально ли весь стек
    # совпадает, или только PoolManager.
    result["known_address_code_check"] = {}
    for name, addr in KNOWN_ROBINHOOD_ADDRESSES.items():
        try:
            code = rpc("eth_getCode", [addr, "latest"])["result"]
            result["known_address_code_check"][name] = {
                "address": addr, "code_len_bytes": (len(code) - 2) // 2 if code else 0,
                "has_code": bool(code) and code != "0x",
            }
        except Exception as exc:  # noqa: BLE001
            result["known_address_code_check"][name] = {"address": addr, "error": str(exc)}

    # 2. Реальный latest block + честная проверка глубины: пробуем блок
    # "0x0" (генезис) -- если чейн реально мигрировал состояние с
    # существовавшей ранее сети, genesis НЕ будет block 0 по времени
    # "сегодня".
    try:
        latest = int(rpc("eth_blockNumber", [])["result"], 16)
        genesis = rpc("eth_getBlockByNumber", ["0x0", False])["result"]
        result["genesis_block"] = {
            "number": int(genesis["number"], 16), "timestamp_unix": int(genesis["timestamp"], 16),
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(genesis["timestamp"], 16))),
        }
        latest_block = rpc("eth_getBlockByNumber", [hex(latest), False])["result"]
        result["latest_block"] = {
            "number": latest, "timestamp_unix": int(latest_block["timestamp"], 16),
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(latest_block["timestamp"], 16))),
        }
    except Exception as exc:  # noqa: BLE001
        result["genesis_block_error"] = str(exc)

    # 3. Реальные Initialize-события V4 PoolManager за ПОСЛЕДНИЕ N
    # блоков (узкий, дешёвый диапазон -- честная, не широкая разведка).
    try:
        latest = int(rpc("eth_blockNumber", [])["result"], 16)
        from_block = max(0, latest - 50000)  # ~50000 блоков * 0.5с ~ 7ч -- разумное "за последние часы"
        logs = rpc("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(latest),
                                     "address": KNOWN_ROBINHOOD_ADDRESSES["PoolManager_v4"],
                                     "topics": [init_topic0]}])
        if "error" in logs:
            result["recent_initialize_events_error"] = logs["error"]
        else:
            evs = logs.get("result", [])
            result["recent_initialize_events"] = {
                "from_block": from_block, "to_block": latest, "n_found": len(evs),
                "sample": evs[:10],
            }
    except Exception as exc:  # noqa: BLE001
        result["recent_initialize_events_error"] = str(exc)

    # 4. Реальные V4 Swap-события за то же окно -- признак реальной
    # DEX-активности (не только деплой контракта).
    try:
        logs2 = rpc("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(latest),
                                      "address": KNOWN_ROBINHOOD_ADDRESSES["PoolManager_v4"],
                                      "topics": [swap_topic0]}])
        if "error" in logs2:
            result["recent_swap_events_error"] = logs2["error"]
        else:
            evs2 = logs2.get("result", [])
            distinct_pools = sorted({e["topics"][1] for e in evs2 if len(e.get("topics", [])) > 1})
            result["recent_v4_swap_events"] = {
                "from_block": from_block, "to_block": latest, "n_swap_logs": len(evs2),
                "n_distinct_pools": len(distinct_pools), "distinct_pool_ids_sample": distinct_pools[:20],
            }
    except Exception as exc:  # noqa: BLE001
        result["recent_swap_events_error"] = str(exc)

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    Path(__file__).parent.parent.joinpath("data", "task_arc_recon_deep_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
