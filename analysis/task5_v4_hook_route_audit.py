#!/usr/bin/env python3
"""Задача 5, стадия 2 (продолжение): маршрут ETH -> MOSIAI -> USDG -> ETH
через хук-содержащий V4-пул ETH/MOSIAI. Владелец явно потребовал:
"изучи комиссии/поведение/лимиты хука, прежде чем полагаться на этот
маршрут" -- этот скрипт делает это ПЕРЕД тем, как считать маршрут
прибыльным по одной лишь величине `fee` в PoolKey.

Хук: 0xe5e702641ea86f4ae6cc3cdaed2b886f976be044 (только на пуле
ETH/MOSIAI; USDG/MOSIAI и ETH/USDG в этом маршруте -- hookless).

Реально проверено (без сети, чистая арифметика над адресом -- V4 кодирует
права хука в младших 14 битах его адреса, см. Uniswap/v4-core
Hooks.sol, сверено через WebFetch реального исходника в этой сессии):
  BEFORE_INITIALIZE_FLAG (1<<13): ДА
  AFTER_SWAP_FLAG        (1<<6):  ДА
  AFTER_SWAP_RETURNS_DELTA_FLAG (1<<2): ДА
  BEFORE_SWAP_FLAG: НЕТ (хук не может отменить своп/переопределить fee
  ДО него -- нет lpFeeOverride)
  все остальные флаги: НЕТ

Значит: этот хук НЕ трогает своп до его исполнения, но ПОСЛЕ свопа
имеет право забрать/добавить дополнительную дельту баланса (сверх
обычной комиссии пула) -- реальный, а не декларативный экономический
риск для маршрута. Оценивается здесь эмпирически по реальным
Transfer-логам исторических tx, а не только по факту наличия флага.

Метод:
  1. Реальные Initialize-логи PoolManager для всех трёх poolId маршрута
     -- получаем настоящий PoolKey (currency0/1, fee, tickSpacing,
     hooks) вместо предположений, сверяем PoolKey->PoolId сами.
  2. eth_getCode хука -- подтверждаем, что это реальный контракт
     (не EOA, не самоуничтожившийся).
  3. Полный разбор Transfer- и V4 Swap-логов ДВУХ контрольных tx
     маршрута -- ищем прямые переводы токена ОТ PoolManager ИЛИ пула
     К АДРЕСУ ХУКА (эмпирический признак AFTER_SWAP-скима, уже замечен
     в другой -- не по этому маршруту -- транзакции этого проекта,
     0x2264176a..., где хук получил ~1% от объёма напрямую от
     PoolManager)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import (  # noqa: E402
    UNISWAP_V4_INITIALIZE_SIG, _chunked_get_logs, _rpc_call, topic0,
)

from task5_v4_pool_math import PoolKey, decode_v4_swap_log_data, pool_id  # noqa: E402

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
HOOK_ETH_MOSIAI = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

POOL_ID_ETH_MOSIAI = "0xf6562daa10e734d41846562b5f418f7833849643bf6c937eeeb0147b8ea94c2f"
POOL_ID_USDG_MOSIAI = "0x784df9b627194fffd79fa5475edc4bb67c458ac7e8b7890f55eb6a522d4a442c"
POOL_ID_ETH_USDG = "0x24107d152f14a76d292123265ae3f3c71f863fc2f4ef7ba49d64e78d28ea379e"

CONTROL_TXS = [
    "0x658c2ab808cd0fa30f5dc14a5ea0e6259fe11714b842efcb5721c3acd320274f",
    "0x9976a38fec4f4d2be228118da68b1276a26e7f33927ea606d11348d31e2ed98c",
]

# Uniswap/v4-core src/libraries/Hooks.sol -- сверено WebFetch в этой сессии.
HOOK_FLAGS = {
    "BEFORE_INITIALIZE_FLAG": 1 << 13,
    "AFTER_INITIALIZE_FLAG": 1 << 12,
    "BEFORE_ADD_LIQUIDITY_FLAG": 1 << 11,
    "AFTER_ADD_LIQUIDITY_FLAG": 1 << 10,
    "BEFORE_REMOVE_LIQUIDITY_FLAG": 1 << 9,
    "AFTER_REMOVE_LIQUIDITY_FLAG": 1 << 8,
    "BEFORE_SWAP_FLAG": 1 << 7,
    "AFTER_SWAP_FLAG": 1 << 6,
    "BEFORE_DONATE_FLAG": 1 << 5,
    "AFTER_DONATE_FLAG": 1 << 4,
    "BEFORE_SWAP_RETURNS_DELTA_FLAG": 1 << 3,
    "AFTER_SWAP_RETURNS_DELTA_FLAG": 1 << 2,
    "AFTER_ADD_LIQUIDITY_RETURNS_DELTA_FLAG": 1 << 1,
    "AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA_FLAG": 1 << 0,
}


def decode_hook_permissions(hook_addr: str) -> dict:
    addr_int = int(hook_addr, 16)
    return {name: bool(addr_int & bit) for name, bit in HOOK_FLAGS.items()}


def fetch_initialize_event(pool_id_hex: str, to_block: int) -> dict | None:
    """Ищем Initialize(id, currency0, currency1, fee, tickSpacing,
    hooks, sqrtPriceX96, tick) для данного poolId -- узкий фильтр
    (адрес PoolManager + topic1=poolId), диапазон от 0 до to_block,
    самобисектящийся при отказе провайдера на широкий диапазон."""
    logs = list(_chunked_get_logs(
        0, to_block,
        topics=[topic0(UNISWAP_V4_INITIALIZE_SIG), pool_id_hex],
        chunk_size=to_block + 1,
        address=POOL_MANAGER,
    ))
    if not logs:
        return None
    log = logs[0]
    currency0 = "0x" + log["topics"][2][-40:]
    currency1 = "0x" + log["topics"][3][-40:]
    data = log["data"][2:]
    words = [data[i:i + 64] for i in range(0, len(data), 64)]
    fee = int(words[0], 16)
    tick_spacing = int(words[1], 16)
    tick_spacing = tick_spacing - (1 << 256) if tick_spacing >= (1 << 255) else tick_spacing
    hooks = "0x" + words[2][-40:]
    sqrt_price_x96 = int(words[3], 16)
    tick = int(words[4], 16)
    tick = tick - (1 << 256) if tick >= (1 << 255) else tick
    return {
        "pool_id": pool_id_hex, "currency0": currency0, "currency1": currency1,
        "fee": fee, "tick_spacing": tick_spacing, "hooks": hooks,
        "init_sqrt_price_x96": sqrt_price_x96, "init_tick": tick,
        "block_number": log.get("blockNumber"),
    }


def audit_tx(tx_hash: str) -> dict:
    receipt = _rpc_call("eth_getTransactionReceipt", [tx_hash])
    tx_obj = _rpc_call("eth_getTransactionByHash", [tx_hash])
    if receipt is None or tx_obj is None:
        return {"tx_hash": tx_hash, "error": "receipt/tx не найдены"}

    v4_topic0 = topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
    swaps, transfers, hook_direct_transfers = [], [], []
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if not topics:
            continue
        if topics[0].lower() == v4_topic0.lower():
            pid = topics[1]
            decoded = decode_v4_swap_log_data(log["data"])
            swaps.append({"pool_id": pid, **decoded})
        elif topics[0].lower() == TRANSFER_TOPIC0.lower() and len(topics) >= 3:
            token = log.get("address")
            frm = "0x" + topics[1][-40:]
            to = "0x" + topics[2][-40:]
            data_hex = log.get("data") or "0x0"
            value = int(data_hex, 16) if data_hex not in ("0x", "") else 0
            row = {"token": token, "from": frm, "to": to, "value_raw": value}
            transfers.append(row)
            if to.lower() == HOOK_ETH_MOSIAI.lower() or frm.lower() == HOOK_ETH_MOSIAI.lower():
                hook_direct_transfers.append(row)

    return {
        "tx_hash": tx_hash,
        "block_number": receipt.get("blockNumber"),
        "status": "success" if receipt.get("status") == "0x1" else "revert",
        "tx_value_wei": tx_obj.get("value"),
        "gas_used": int(receipt["gasUsed"], 16),
        "n_v4_swaps": len(swaps),
        "swaps": swaps,
        "n_transfers": len(transfers),
        "transfers": transfers,
        "hook_direct_transfers": hook_direct_transfers,
    }


def main() -> None:
    result: dict = {}

    print("[v4_hook_route_audit] шаг 1: права хука из его адреса (Hooks.sol флаги)")
    perms = decode_hook_permissions(HOOK_ETH_MOSIAI)
    result["hook_permissions"] = perms
    print(json.dumps(perms, indent=2))

    print("[v4_hook_route_audit] шаг 2: eth_getCode хука -- подтверждаем реальный контракт")
    try:
        code = _rpc_call("eth_getCode", [HOOK_ETH_MOSIAI, "latest"])
        code_len = (len(code) - 2) // 2 if code and code != "0x" else 0
        result["hook_code_len_bytes"] = code_len
        print(f"hook eth_getCode: {code_len} байт")
    except Exception as exc:  # noqa: BLE001
        result["hook_code_len_bytes"] = {"error": str(exc)}
        print(f"ошибка eth_getCode: {exc}", file=sys.stderr)

    print("[v4_hook_route_audit] шаг 3: реальные Initialize-события трёх пулов маршрута")
    try:
        latest = int(_rpc_call("eth_blockNumber", []), 16)
    except Exception as exc:  # noqa: BLE001
        print(f"не удалось получить latest block: {exc}", file=sys.stderr)
        latest = 61300000  # честная деградация -- заведомо позже всех контрольных tx

    pool_infos = {}
    for label, pid in [("ETH_MOSIAI", POOL_ID_ETH_MOSIAI), ("USDG_MOSIAI", POOL_ID_USDG_MOSIAI),
                        ("ETH_USDG", POOL_ID_ETH_USDG)]:
        try:
            info = fetch_initialize_event(pid, latest)
            pool_infos[label] = info
            if info:
                key = PoolKey(info["currency0"], info["currency1"], info["fee"], info["tick_spacing"], info["hooks"])
                computed = pool_id(key)
                info["poolkey_to_poolid_match"] = computed.lower() == pid.lower()
                print(f"{label}: {json.dumps(info, indent=2, default=str)}")
            else:
                print(f"{label}: Initialize-событие НЕ найдено (poolId={pid})", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            pool_infos[label] = {"error": str(exc)}
            print(f"{label}: ошибка получения Initialize: {exc}", file=sys.stderr)
    result["pool_infos"] = pool_infos

    print("[v4_hook_route_audit] шаг 4: разбор двух контрольных tx маршрута -- Swap+Transfer логи, прямые переводы на хук")
    tx_audits = []
    for tx_hash in CONTROL_TXS:
        try:
            audit = audit_tx(tx_hash)
        except Exception as exc:  # noqa: BLE001
            audit = {"tx_hash": tx_hash, "error": str(exc)}
        tx_audits.append(audit)
        print(json.dumps(audit, indent=2, default=str))
    result["control_tx_audits"] = tx_audits

    out_path = Path(__file__).parent.parent / "data" / "task5_v4_hook_route_audit_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"[v4_hook_route_audit] сохранено: {out_path}")


if __name__ == "__main__":
    main()
