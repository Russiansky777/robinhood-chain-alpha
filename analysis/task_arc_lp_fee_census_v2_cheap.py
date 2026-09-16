#!/usr/bin/env python3
"""Владелец (2026-09-16): перепись дешевле полного скана. Ключевое
упрощение по сравнению с провалившимся task_arc_lp_fee_census.py --
"живые" пулы теперь берутся из УЖЕ ГОТОВЫХ локальных файлов (top_pools_
symbols + маршруты 267 циклов, см. task_arc_lp_census_from_local_files_
result.json), а НЕ через дорогой per-pool подсчёт свопов. Значит из
дорогого остаётся только ОДНА вещь: узнать fee/hooks по pool_id --
и это дёшево, если делать ОДНИМ широким сканом Initialize (полный
декод data, не только topics), а не точечно по каждому pool_id.

Диагностика (task_arc_lp_census_diag_result.json) уже подтвердила: один
чанк в 5000 блоков по топику Initialize -- ОДИН вызов eth_getLogs,
2839 результатов, 4с. Весь реестр (24567 пулов, ~38200 блоков) --
это ~8 таких чанков, не сотни точечных запросов. САМОЕ ОСТОРОЖНОЕ:
сначала проба на 1 маленьком чанке -- если сразу rate limit, останавливаемся
и ничего больше не шлём (уважаем паузу)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from collections import Counter

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
PROFIT_RECEIVER_HOOK = "0x47e7936ae9891e61c5123db720593c05de7120cc"
AKA_FUN_HOOK = "0x20eead6db6b3d0a4491e9073119dd0ebff166acc"
NATIVE_SENTINEL = "0x0000000000000000000000000000000000000000"

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
MIN_CALL_INTERVAL_S = 0.15  # ~6.7 вызовов/с -- очень осторожно после rate limit
_last_call_ts = [0.0]
_rpc_calls = [0]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


INIT_TOPIC0 = keccak_topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    wait = MIN_CALL_INTERVAL_S - (time.time() - _last_call_ts[0])
    if wait > 0:
        time.sleep(wait)
    _last_call_ts[0] = time.time()
    _rpc_calls[0] += 1
    try:
        resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              headers={"Content-Type": "application/json"}, timeout=timeout)
        body = resp.json()
        body["_http_status"] = resp.status_code
        return body
    except Exception as exc:  # noqa: BLE001
        return {"error": {"message": f"{type(exc).__name__}: {exc}"}, "_http_status": None}


def word(data_bytes: bytes, i: int) -> bytes:
    return data_bytes[i * 32:(i + 1) * 32]


def decode_initialize_full(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {
        "pool_id": log["topics"][1], "currency0": "0x" + log["topics"][2][-40:],
        "currency1": "0x" + log["topics"][3][-40:],
        "fee_pips": int.from_bytes(word(data, 0)[-3:], "big"),
        "hooks": ("0x" + word(data, 2)[-20:].hex()).lower(),
        "block_number": int(log["blockNumber"], 16),
    }


def is_rate_limited(body: dict) -> bool:
    err = body.get("error")
    return bool(err) and (err.get("code") == -32005 or "rate limit" in str(err.get("message", "")).lower()
                           or body.get("_http_status") == 429)


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    registry = json.loads(data_dir.joinpath("task_arc_recon_pools_result.json").read_text())
    reg_pools = registry.get("initialize_events", {}).get("pools", [])
    min_init = min(p["block_number"] for p in reg_pools)
    max_init = max(p["block_number"] for p in reg_pools)
    result["registry_n_pools"] = len(reg_pools)
    result["registry_block_range"] = {"min": min_init, "max": max_init}

    # === Осторожная проба: ОДИН маленький чанк (500 блоков) ===
    probe = rpc("eth_getLogs", [{"fromBlock": hex(min_init), "toBlock": hex(min_init + 500),
                                  "address": POOL_MANAGER, "topics": [INIT_TOPIC0]}])
    result["probe_small_chunk"] = {"http_status": probe.get("_http_status"), "has_error": "error" in probe,
                                    "error": probe.get("error"), "n_results": len(probe.get("result", []))}
    if is_rate_limited(probe):
        result["STOPPED"] = "rate limit ещё активен на первой же пробе (500 блоков) -- НЕ продолжаем, уважаем паузу. Только пункт 1 (локальные файлы) остаётся в силе."
        result["total_rpc_calls"] = _rpc_calls[0]
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_lp_fee_census_v2_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # === Проба прошла -- полный скан реестра, chunk=5000 (уже проверенный рабочий размер) ===
    all_logs = []
    from_block = min_init
    chunk = 5000
    calls = 0
    stopped_by_rate_limit = False
    while from_block <= max_init and calls < 20:
        to_block = min(from_block + chunk - 1, max_init)
        body = rpc("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(to_block),
                                     "address": POOL_MANAGER, "topics": [INIT_TOPIC0]}])
        calls += 1
        if is_rate_limited(body):
            stopped_by_rate_limit = True
            break
        if "error" in body:
            err = body["error"]
            msg = str(err.get("message", "")).lower()
            if "too large" in msg or "max results" in msg or err.get("code") in (-32012, -32602):
                chunk = max(500, chunk // 2)
                continue
            break
        all_logs.extend(body.get("result", []))
        from_block = to_block + 1

    result["full_rescan"] = {
        "n_calls": calls, "stopped_by_rate_limit": stopped_by_rate_limit,
        "scanned_up_to_block": from_block - 1, "target_max_block": max_init,
        "complete": from_block > max_init,
        "n_logs_found": len(all_logs),
    }

    pools_full = [decode_initialize_full(l) for l in all_logs]

    # === Полная перепись по fee/hooks на реально просканированном множестве ===
    fee_zero = [p for p in pools_full if p["fee_pips"] == 0]
    fee_nonzero = [p for p in pools_full if p["fee_pips"] > 0]
    result["fee_census_FULL_registry_scanned_portion"] = {
        "n_total_scanned": len(pools_full),
        "n_fee_zero": len(fee_zero), "pct_fee_zero": (len(fee_zero) / len(pools_full) * 100) if pools_full else None,
        "n_fee_nonzero": len(fee_nonzero), "pct_fee_nonzero": (len(fee_nonzero) / len(pools_full) * 100) if pools_full else None,
    }
    result["nonzero_fee_histogram_pips"] = dict(sorted(Counter(p["fee_pips"] for p in fee_nonzero).items()))

    hook_counts = Counter(p["hooks"] for p in pools_full)
    n_no_hook = hook_counts.get(NATIVE_SENTINEL, 0)
    real_hook_counts = Counter({h: n for h, n in hook_counts.items() if h != NATIVE_SENTINEL})
    top10_real_hooks = real_hook_counts.most_common(10)
    hook_breakdown = []
    for hook_addr, n in top10_real_hooks:
        fees_for_hook = Counter(p["fee_pips"] for p in pools_full if p["hooks"] == hook_addr)
        hook_breakdown.append({"hook": hook_addr, "n_pools": n, "fee_pips_used": dict(sorted(fees_for_hook.items()))})

    result["hooks_census_FULL_registry_scanned_portion"] = {
        "n_pools_no_hook_address_zero": n_no_hook,
        "pct_no_hook": (n_no_hook / len(pools_full) * 100) if pools_full else None,
        "n_unique_REAL_hook_addresses": len(real_hook_counts),
        "top10_real_hooks": hook_breakdown,
        "top3_real_hooks_share_of_pools_WITH_a_real_hook": (
            sum(n for _, n in top10_real_hooks[:3]) / sum(real_hook_counts.values()) * 100
        ) if real_hook_counts else None,
        "aka_fun_hook_n_pools": hook_counts.get(AKA_FUN_HOOK, 0),
        "profit_receiver_hook_n_pools": hook_counts.get(PROFIT_RECEIVER_HOOK.lower(), 0),
    }

    # === Пересечение с "живыми" пулами из локальных файлов (если тот файл уже есть) ===
    local_census_path = data_dir.joinpath("task_arc_lp_census_from_local_files_result.json")
    if local_census_path.exists():
        local = json.loads(local_census_path.read_text())
        live_pool_ids = {p["pool_id"] for p in local.get("all_live_pools", [])}
        fresh_by_id = {p["pool_id"]: p for p in pools_full}
        matched = [fresh_by_id[pid] for pid in live_pool_ids if pid in fresh_by_id]
        result["cross_check_with_live_pools_from_local_files"] = {
            "n_live_pools_from_local_files": len(live_pool_ids),
            "n_matched_in_fresh_scan": len(matched),
            "matched_fee_zero": sum(1 for p in matched if p["fee_pips"] == 0),
            "matched_fee_nonzero": sum(1 for p in matched if p["fee_pips"] > 0),
            "matched_no_hook": sum(1 for p in matched if p["hooks"] == NATIVE_SENTINEL),
        }

    result["total_rpc_calls"] = _rpc_calls[0]
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_lp_fee_census_v2_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
