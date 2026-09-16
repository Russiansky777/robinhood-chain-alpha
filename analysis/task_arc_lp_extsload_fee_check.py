#!/usr/bin/env python3
"""Владелец (2026-09-16), пункт А: наш rate limit был на eth_getLogs, НЕ
на eth_call (подтверждено диагностикой -- eth_blockNumber ходил без
единой ошибки, пока eth_getLogs получал -32005 раз за разом). Проверить,
читаются ли fee/hooks пула через eth_call -- если да, перепись ставок
не нуждается в getLogs вообще.

Формула extsload УЖЕ проверена и подтверждена в этой сессии на
Robinhood Chain (analysis/task5_v4_extsload_slot0_pointcheck.py,
сверено WebFetch реального src/libraries/StateLibrary.sol Uniswap/v4-
core, независимо перепроверено квотой V4Quoter, совпадение с точностью
до известного skim'а хука). Формула:

  stateSlot = keccak256(poolId ++ uint256(6))   # POOLS_SLOT=6 в PoolManager
  slot0 = extsload(stateSlot)                    # sqrtPriceX96|tick|protocolFee|lpFee
  liquidity = extsload(stateSlot + 3)            # LIQUIDITY_OFFSET=3

lpFee -- РЕАЛЬНЫЙ активный fee пула (для dynamic-fee пулов -- то, что
реально применяется прямо сейчас, не placeholder 0x800000 из
Initialize). ВАЖНО, честно: hooks НЕ хранится в Pool.State вообще (это
часть PoolKey, участвует только в вычислении poolId=keccak256(key), а
не в его storage) -- eth_call НЕ может отдать hooks по одному pool_id,
это фундаментальное свойство V4, не ограничение реализации. Для наших
уже известных 16 пулов hooks уже есть из локальных файлов -- не нужен."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOLS_SLOT = 6
LIQUIDITY_OFFSET = 3
REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def keccak256(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


_rpc_calls = [0]


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    _rpc_calls[0] += 1
    try:
        resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              headers={"Content-Type": "application/json"}, timeout=timeout)
        body = resp.json()
        body["_http_status"] = resp.status_code
        return body
    except Exception as exc:  # noqa: BLE001
        return {"error": {"message": f"{type(exc).__name__}: {exc}"}, "_http_status": None}


def extsload(slot_hex: str, block_hex: str = "latest") -> dict:
    selector = keccak256(b"extsload(bytes32)")[:4].hex()
    calldata = "0x" + selector + slot_hex[2:].rjust(64, "0")
    return rpc("eth_call", [{"to": POOL_MANAGER, "data": calldata}, block_hex])


def read_pool_state(pool_id: str, block_hex: str = "latest") -> dict:
    pool_id_bytes = bytes.fromhex(pool_id[2:])
    state_slot = keccak256(pool_id_bytes + POOLS_SLOT.to_bytes(32, "big"))
    state_slot_hex = "0x" + state_slot.hex()
    liq_slot_hex = "0x" + (int.from_bytes(state_slot, "big") + LIQUIDITY_OFFSET).to_bytes(32, "big").hex()

    r0 = extsload(state_slot_hex, block_hex)
    r1 = extsload(liq_slot_hex, block_hex)
    if "error" in r0 or "error" in r1:
        return {"pool_id": pool_id, "block": block_hex, "error": r0.get("error") or r1.get("error"),
                "http_status_slot0": r0.get("_http_status"), "http_status_liq": r1.get("_http_status")}

    slot0_raw = r0.get("result")
    liq_raw = r1.get("result")
    if not slot0_raw or slot0_raw == "0x" or int(slot0_raw, 16) == 0:
        return {"pool_id": pool_id, "block": block_hex, "note": "slot0 пуст (0x0) -- пул не инициализирован на этом блоке или неверный слот"}

    data = int(slot0_raw, 16)
    sqrt_price_x96 = data & ((1 << 160) - 1)
    tick_raw = (data >> 160) & ((1 << 24) - 1)
    tick = tick_raw - (1 << 24) if tick_raw >= (1 << 23) else tick_raw
    protocol_fee = (data >> 184) & ((1 << 24) - 1)
    lp_fee = (data >> 208) & ((1 << 24) - 1)
    liquidity = int(liq_raw, 16) & ((1 << 128) - 1) if liq_raw else None

    return {"pool_id": pool_id, "block": block_hex, "sqrt_price_x96": sqrt_price_x96, "tick": tick,
            "protocol_fee": protocol_fee, "lp_fee_REAL_ACTIVE": lp_fee, "liquidity": liquidity}


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # === Проверка: eth_call действительно не в rate limit? ===
    probe = rpc("eth_blockNumber", [])
    result["probe_blockNumber"] = {"http_status": probe.get("_http_status"), "has_error": "error" in probe}
    latest = int(probe.get("result", "0x0"), 16) if not probe.get("error") else None
    result["latest_block"] = latest

    # Один extsload-запрос как проба ПЕРЕД полным проходом
    top = json.loads(data_dir.joinpath("task_arc_top_pools_symbols_result.json").read_text())["top_pools_enriched"]
    probe_state = read_pool_state(top[0]["pool_id"], "latest")
    result["probe_extsload"] = probe_state
    if "error" in probe_state:
        err = probe_state["error"]
        if err and (err.get("code") == -32005 or "rate limit" in str(err.get("message", "")).lower()):
            result["STOPPED"] = "eth_call ТОЖЕ в rate limit -- гипотеза владельца не подтвердилась, останавливаемся"
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            data_dir.joinpath("task_arc_lp_extsload_fee_check_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            return

    # === Полный проход: все 16 пулов, зафиксированных в реестре, latest + init_block ===
    registry = json.loads(data_dir.joinpath("task_arc_recon_pools_result.json").read_text())
    reg_by_id = {p["pool_id"]: p for p in registry.get("initialize_events", {}).get("pools", [])}

    pool_states = []
    for p in top:
        reg = reg_by_id.get(p["pool_id"])
        if not reg:
            continue
        latest_state = read_pool_state(p["pool_id"], "latest")
        latest_state["pair"] = p.get("pair_symbols")
        latest_state["fee_initialize_pips"] = p.get("fee_initialize_pips")
        latest_state["is_dynamic_fee_flag"] = p.get("fee_initialize_pips") == 8388608
        pool_states.append(latest_state)

    result["n_pools_checked"] = len(pool_states)
    result["pool_states_via_extsload"] = pool_states

    # Сверка: для СТАТИЧНЫХ (не dynamic-fee) пулов lp_fee_REAL_ACTIVE ДОЛЖЕН
    # совпадать с fee_initialize_pips -- если нет, это реальная находка, не ошибка.
    mismatches = []
    for s in pool_states:
        if s.get("is_dynamic_fee_flag") or "lp_fee_REAL_ACTIVE" not in s:
            continue
        if s["lp_fee_REAL_ACTIVE"] != s["fee_initialize_pips"]:
            mismatches.append({"pool_id": s["pool_id"], "pair": s["pair"],
                                "fee_initialize": s["fee_initialize_pips"], "fee_active_now": s["lp_fee_REAL_ACTIVE"]})
    result["static_fee_mismatches_vs_initialize"] = mismatches
    result["conclusion_hooks_not_readable_via_eth_call"] = (
        "Подтверждено структурно (не предположение): hooks -- часть PoolKey, участвует только в "
        "вычислении poolId=keccak256(key) при создании пула, НЕ хранится в Pool.State -- eth_call "
        "к PoolManager не может отдать hooks по одному pool_id ни при каком выборе слота. Для fee "
        "(lpFee) это НЕ ограничение -- он хранится в Slot0 и читается, как показано выше."
    )

    result["total_rpc_calls"] = _rpc_calls[0]
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_lp_extsload_fee_check_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
