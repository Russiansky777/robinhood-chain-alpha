#!/usr/bin/env python3
"""Задание владельца, п.2: адресация USDC в V4-пулах на Arc -- входные
данные для решения, переносить ли ClosedCycleExecutorV4 (писался под
нативный ETH). Только чтение."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
USDC_ERC20 = "0x3600000000000000000000000000000000000000"
NATIVE_ZERO = "0x0000000000000000000000000000000000000000"


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


INIT_TOPIC0 = keccak_topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    for attempt in range(5):
        try:
            resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                  headers={"Content-Type": "application/json"}, timeout=timeout)
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            if attempt == 4:
                return {"error": {"message": str(exc)}}
            time.sleep(2 * (attempt + 1))
            continue
        err = body.get("error")
        if err and "rate limit" in str(err.get("message", "")).lower():
            if attempt == 4:
                return body
            time.sleep(2 * (attempt + 1))
            continue
        return body
    return {"error": {"message": "unreachable"}}


def topic_to_addr(t: str) -> str:
    return "0x" + t[-40:]


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rpc": RPC}

    latest = int(rpc("eth_blockNumber", [])["result"], 16)
    from_block = max(0, latest - int(3600 / 0.506))  # последний час, тот же блок-тайм, что в п.0
    logs_body = rpc("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(latest),
                                      "address": POOL_MANAGER, "topics": [INIT_TOPIC0]}])
    # Если чанк не влезает -- сузим окно вдвое один раз (честно, без слепого повтора).
    if "error" in logs_body:
        from_block = max(0, latest - int(1800 / 0.506))
        logs_body = rpc("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(latest),
                                          "address": POOL_MANAGER, "topics": [INIT_TOPIC0]}])
    logs = logs_body.get("result", []) if "error" not in logs_body else []
    result["initialize_scan_window"] = {"from_block": from_block, "to_block": latest,
                                         "n_found": len(logs), "error": logs_body.get("error")}

    pools = []
    for log in logs:
        c0 = topic_to_addr(log["topics"][2])
        c1 = topic_to_addr(log["topics"][3])
        pools.append({"pool_id": log["topics"][1], "currency0": c0, "currency1": c1,
                       "block_number": int(log["blockNumber"], 16), "tx_hash": log["transactionHash"]})

    def classify(pool):
        c0, c1 = pool["currency0"].lower(), pool["currency1"].lower()
        has_zero = NATIVE_ZERO in (c0, c1)
        has_usdc_erc20 = USDC_ERC20.lower() in (c0, c1)
        if has_zero:
            return "address(0)_native"
        if has_usdc_erc20:
            return "erc20_0x3600"
        return "neither"

    counts = Counter(classify(p) for p in pools)
    result["all_pools_in_window"] = {"n_total": len(pools), "by_usdc_addressing": dict(counts)}

    # Топ-30 по числу свопов за то же окно -- дороже (Swap-скан), делаем
    # компактно: считаем свопы на пулах из уже найденного набора Initialize
    # только если бюджет позволяет (честный потолок).
    swap_logs_body = rpc("eth_getLogs", [{"fromBlock": hex(max(0, latest - 300)), "toBlock": hex(latest),
                                           "address": POOL_MANAGER, "topics": [SWAP_TOPIC0]}])
    pool_swap_counts: Counter = Counter()
    if "error" not in swap_logs_body:
        for log in swap_logs_body.get("result", []):
            pool_swap_counts[log["topics"][1]] += 1
    result["swap_count_sample_window_blocks"] = 300
    top30_ids = [pid for pid, _ in pool_swap_counts.most_common(30)]
    pool_by_id = {p["pool_id"]: p for p in pools}
    top30_classified = Counter()
    n_top30_found_in_init_map = 0
    for pid in top30_ids:
        p = pool_by_id.get(pid)
        if p:
            n_top30_found_in_init_map += 1
            top30_classified[classify(p)] += 1
    result["top30_by_swap_count"] = {
        "n_top30_pools_considered": len(top30_ids),
        "n_found_in_this_windows_initialize_map": n_top30_found_in_init_map,
        "n_not_found_note": "пул мог быть создан раньше окна Initialize-скана -- не в этой карте, не значит 'не адресует USDC'",
        "by_usdc_addressing": dict(top30_classified),
    }

    # Прямые вызовы: eth_getCode/decimals/symbol/balanceOf(PoolManager) на
    # 0x3600..., и eth_getBalance(PoolManager) -- нативный баланс.
    code_body = rpc("eth_getCode", [USDC_ERC20, "latest"])
    decimals_body = rpc("eth_call", [{"to": USDC_ERC20, "data": "0x313ce567"}, "latest"])
    symbol_body = rpc("eth_call", [{"to": USDC_ERC20, "data": "0x95d89b41"}, "latest"])
    pm_padded = POOL_MANAGER[2:].rjust(64, "0")
    balanceof_data = "0x70a08231" + pm_padded
    balanceof_body = rpc("eth_call", [{"to": USDC_ERC20, "data": balanceof_data}, "latest"])
    native_balance_body = rpc("eth_getBalance", [POOL_MANAGER, "latest"])

    def dec_str(hexdata):
        if not hexdata or hexdata == "0x":
            return None
        raw = bytes.fromhex(hexdata[2:])
        if len(raw) < 64:
            return None
        length = int.from_bytes(raw[32:64], "big")
        try:
            return raw[64:64 + length].decode("utf-8")
        except Exception:
            return None

    result["usdc_erc20_direct_checks"] = {
        "address": USDC_ERC20,
        "code_len_bytes": (len(code_body["result"]) - 2) // 2 if code_body.get("result") else 0,
        "decimals": int(decimals_body["result"], 16) if decimals_body.get("result") not in (None, "0x") else None,
        "symbol": dec_str(symbol_body.get("result")),
        "pool_manager_erc20_balance_raw": int(balanceof_body["result"], 16) if balanceof_body.get("result") not in (None, "0x") else None,
        "pool_manager_native_balance_wei": int(native_balance_body["result"], 16) if native_balance_body.get("result") else None,
    }

    pm_erc20_bal = result["usdc_erc20_direct_checks"]["pool_manager_erc20_balance_raw"] or 0
    pm_native_bal = result["usdc_erc20_direct_checks"]["pool_manager_native_balance_wei"] or 0

    # Один реальный своп на address(0)-пуле (если такой найден) -- проверить,
    # несёт ли tx поле value>0 (нативная адресация подтверждена бы этим).
    zero_addr_pool = next((p for p in pools if NATIVE_ZERO in (p["currency0"].lower(), p["currency1"].lower())), None)
    native_value_check = None
    if zero_addr_pool:
        one_swap = rpc("eth_getLogs", [{"fromBlock": hex(zero_addr_pool["block_number"]), "toBlock": hex(latest),
                                         "address": POOL_MANAGER, "topics": [SWAP_TOPIC0, zero_addr_pool["pool_id"]]}])
        logs2 = one_swap.get("result", []) if "error" not in one_swap else []
        if logs2:
            txh = logs2[0]["transactionHash"]
            tx = rpc("eth_getTransactionByHash", [txh]).get("result") or {}
            native_value_check = {"pool_id": zero_addr_pool["pool_id"], "tx_hash": txh,
                                   "tx_value_wei": int(tx.get("value", "0x0"), 16) if tx.get("value") else 0}
    result["native_value_check_on_zero_addr_pool"] = native_value_check or "не найден ни один address(0)-пул со свопом в измеренном окне"

    addressing_summary = ("address(0) (нативная)" if counts.get("address(0)_native", 0) > counts.get("erc20_0x3600", 0)
                           else "ERC20 0x3600..." if counts.get("erc20_0x3600", 0) > 0
                           else "ни то, ни другое в измеренном окне")
    result["one_line_summary"] = (
        f"V4 на Arc адресует USDC как {addressing_summary} "
        f"({counts.get('address(0)_native', 0)} address(0)-пулов, {counts.get('erc20_0x3600', 0)} ERC20-0x3600-пулов, "
        f"{counts.get('neither', 0)} ни то ни другое из {len(pools)} за окно); "
        f"PoolManager держит нативных {pm_native_bal / 1e18:.6f} и ERC-20 0x3600 {pm_erc20_bal / 1e6:.6f}."
    )

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    Path(__file__).parent.parent.joinpath("data", "task_arc_usdc_addressing_check_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
