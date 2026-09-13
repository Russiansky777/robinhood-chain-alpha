#!/usr/bin/env python3
"""Задача 5, кэш состояния -- пункт 1 (нач. состояние): ОДНА точечная
проверка формулы слота StateLibrary.getSlot0/getLiquidity (сверена
WebFetch реального src/libraries/StateLibrary.sol Uniswap/v4-core в
этой сессии):

  stateSlot = keccak256(abi.encodePacked(poolId, uint256(6)))   # POOLS_SLOT=6
  slot0_word = extsload(stateSlot)                              # sqrtPriceX96|tick|protocolFee|lpFee
  liquidity_word = extsload(stateSlot + 3)                      # LIQUIDITY_OFFSET=3, нижние 128 бит

Проверяем: extsload на РЕАЛЬНОМ уже известном пуле (ETH/MOSIAI,
0xf6562daa...) НА ТОМ ЖЕ блоке, что и уже сохранённая (task5_v4_
hook_route_audit_result.json) РЕАЛЬНАЯ Swap-запись этого пула --
sqrtPriceX96/liquidity ДОЛЖНЫ совпасть побайтово (то же состояние,
два независимых способа его прочитать: событие И extsload). НЕ
предполагаем формулу верной -- either matches, or мы честно это
не используем."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call  # noqa: E402

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOL_ID_ETH_MOSIAI = "0xf6562daa10e734d41846562b5f418f7833849643bf6c937eeeb0147b8ea94c2f"
# Из уже сохранённого task5_v4_hook_route_audit_result.json, control_tx_audits[0]:
# tx 0x658c2ab8... block 0x3a694cb, swap на ETH/MOSIAI дал sqrt_price_x96/liquidity/tick ниже.
KNOWN_BLOCK_HEX = "0x3a694cb"
EXPECTED_SQRT_PRICE_X96 = 233449081997724268724434687613747
EXPECTED_LIQUIDITY = 29277002188455995779456
EXPECTED_TICK = 159775

POOLS_SLOT = 6
LIQUIDITY_OFFSET = 3


def keccak256(data: bytes) -> bytes:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def extsload(slot_hex: str, block_hex: str) -> str:
    # extsload(bytes32) selector = keccak256("extsload(bytes32)")[:4]
    selector = keccak256(b"extsload(bytes32)")[:4].hex()
    calldata = "0x" + selector + slot_hex[2:].rjust(64, "0")
    return _rpc_call("eth_call", [{"to": POOL_MANAGER, "data": calldata}, block_hex])


def main() -> None:
    result: dict = {}
    pool_id_bytes = bytes.fromhex(POOL_ID_ETH_MOSIAI[2:])
    state_slot = keccak256(pool_id_bytes + (POOLS_SLOT).to_bytes(32, "big"))
    state_slot_hex = "0x" + state_slot.hex()
    liquidity_slot_hex = "0x" + (int.from_bytes(state_slot, "big") + LIQUIDITY_OFFSET).to_bytes(32, "big").hex()

    result["state_slot"] = state_slot_hex
    result["liquidity_slot"] = liquidity_slot_hex

    slot0_raw = extsload(state_slot_hex, KNOWN_BLOCK_HEX)
    liq_raw = extsload(liquidity_slot_hex, KNOWN_BLOCK_HEX)
    result["slot0_raw"] = slot0_raw
    result["liquidity_raw"] = liq_raw

    data = int(slot0_raw, 16)
    sqrt_price_x96 = data & ((1 << 160) - 1)
    tick_raw = (data >> 160) & ((1 << 24) - 1)
    tick = tick_raw - (1 << 24) if tick_raw >= (1 << 23) else tick_raw
    protocol_fee = (data >> 184) & ((1 << 24) - 1)
    lp_fee = (data >> 208) & ((1 << 24) - 1)
    liquidity = int(liq_raw, 16) & ((1 << 128) - 1)

    result["decoded"] = {
        "sqrt_price_x96": sqrt_price_x96, "tick": tick, "protocol_fee": protocol_fee,
        "lp_fee": lp_fee, "liquidity": liquidity,
    }
    result["expected_from_saved_swap_event"] = {
        "sqrt_price_x96": EXPECTED_SQRT_PRICE_X96, "tick": EXPECTED_TICK, "liquidity": EXPECTED_LIQUIDITY,
    }
    result["match"] = {
        "sqrt_price_x96": sqrt_price_x96 == EXPECTED_SQRT_PRICE_X96,
        "tick": tick == EXPECTED_TICK,
        "liquidity": liquidity == EXPECTED_LIQUIDITY,
    }
    result["all_match"] = all(result["match"].values())

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_extsload_slot0_pointcheck_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
