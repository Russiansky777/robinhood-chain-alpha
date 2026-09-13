#!/usr/bin/env python3
"""Задача 5, кэш состояния -- пункт 1 (нач. состояние): ОДНА точечная
проверка формулы слота StateLibrary.getSlot0/getLiquidity (сверена
WebFetch реального src/libraries/StateLibrary.sol Uniswap/v4-core в
этой сессии):

  stateSlot = keccak256(abi.encodePacked(poolId, uint256(6)))   # POOLS_SLOT=6
  slot0_word = extsload(stateSlot)                              # sqrtPriceX96|tick|protocolFee|lpFee
  liquidity_word = extsload(stateSlot + 3)                      # LIQUIDITY_OFFSET=3, нижние 128 бит

Проверяем на РЕАЛЬНОМ уже известном пуле (ETH/MOSIAI, 0xf6562daa...).
Первая попытка -- сверить extsload с УЖЕ СОХРАНЁННЫМ старым блоком
реальной Swap-записи (task5_v4_hook_route_audit_result.json) провалилась
честно ("metadata is not found" -- провайдер не хранит state настолько
старого блока, это НЕ ошибка формулы). Поэтому здесь -- независимая
проверка на "latest": извлечённые sqrtPriceX96/liquidity должны дать
цену, СОГЛАСОВАННУЮ с независимым методом (крошечная реальная
котировка через уже проверенный V4Quoter на том же блоке/пуле/
направлении) -- два независимых способа увидеть одно и то же состояние
должны совпасть с точностью до комиссии/округления на малом размере."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call  # noqa: E402
from task5_v4_pool_math import PoolKey, quote_exact_input_single_calldata, decode_quote_result  # noqa: E402

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
POOL_ID_ETH_MOSIAI = "0xf6562daa10e734d41846562b5f418f7833849643bf6c937eeeb0147b8ea94c2f"
KEY_ETH_MOSIAI = PoolKey(
    "0x0000000000000000000000000000000000000000", "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a",
    0, 200, "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044")
TINY_AMOUNT_IN = 10 ** 12  # 0.000001 ETH -- достаточно мало, чтобы влияние liquidity-кривой было пренебрежимо

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
    latest_hex = _rpc_call("eth_blockNumber", [])
    result["latest_block"] = latest_hex

    pool_id_bytes = bytes.fromhex(POOL_ID_ETH_MOSIAI[2:])
    state_slot = keccak256(pool_id_bytes + (POOLS_SLOT).to_bytes(32, "big"))
    state_slot_hex = "0x" + state_slot.hex()
    liquidity_slot_hex = "0x" + (int.from_bytes(state_slot, "big") + LIQUIDITY_OFFSET).to_bytes(32, "big").hex()
    result["state_slot"] = state_slot_hex
    result["liquidity_slot"] = liquidity_slot_hex

    slot0_raw = extsload(state_slot_hex, latest_hex)
    liq_raw = extsload(liquidity_slot_hex, latest_hex)
    result["slot0_raw"] = slot0_raw
    result["liquidity_raw"] = liq_raw

    data = int(slot0_raw, 16)
    sqrt_price_x96 = data & ((1 << 160) - 1)
    tick_raw = (data >> 160) & ((1 << 24) - 1)
    tick = tick_raw - (1 << 24) if tick_raw >= (1 << 23) else tick_raw
    protocol_fee = (data >> 184) & ((1 << 24) - 1)
    lp_fee = (data >> 208) & ((1 << 24) - 1)
    liquidity = int(liq_raw, 16) & ((1 << 128) - 1)

    result["decoded_via_extsload"] = {
        "sqrt_price_x96": sqrt_price_x96, "tick": tick, "protocol_fee": protocol_fee,
        "lp_fee": lp_fee, "liquidity": liquidity,
    }
    # Цена token1-за-token0 из sqrtPriceX96 (Q96): price = (sqrtPriceX96 / 2^96)^2
    price_from_extsload = (sqrt_price_x96 / (2 ** 96)) ** 2

    # Независимая проверка -- крошечная РЕАЛЬНАЯ котировка через уже проверенный V4Quoter,
    # тот же пул/направление/блок. На малом размере amountOut/amountIn ~= текущая цена
    # (минус пул-fee=0 у этого пула -- см. hook_route_audit_result.json pool_infos.ETH_MOSIAI.fee=0).
    calldata = quote_exact_input_single_calldata(KEY_ETH_MOSIAI, True, TINY_AMOUNT_IN)
    quote_raw = _rpc_call("eth_call", [{"to": V4_QUOTER, "data": calldata}, latest_hex])
    amount_out, gas_estimate = decode_quote_result(quote_raw)
    price_from_quote = amount_out / TINY_AMOUNT_IN

    result["independent_quote_check"] = {
        "amount_in": TINY_AMOUNT_IN, "amount_out": amount_out, "gas_estimate": gas_estimate,
        "implied_price_token1_per_token0": price_from_quote,
    }
    result["price_from_extsload_sqrt_price_x96"] = price_from_extsload
    rel_diff = abs(price_from_quote - price_from_extsload) / price_from_extsload if price_from_extsload else None
    result["relative_difference"] = rel_diff
    # ВАЖНО: этот пул -- ETH/MOSIAI, тот самый с хуком 0xe5e70264..., у которого уже
    # эмпирически подтверждён ~2% пост-своп ским НА ВЫХОДЕ (см. task5_v4_hook_route_audit_
    # result.json) -- quoteExactInputSingle идёт через РЕАЛЬНЫЙ PoolManager.unlock, значит
    # amountOut уже включает этот ским, а sqrtPriceX96 из extsload -- чистое AMM-состояние
    # ДО скима. Ожидаем расхождение ~2% (не 0%) -- это подтверждало бы ОБА независимых
    # источника данных, а не противоречие. Порог -- 3% (2% ским + small price impact/rounding
    # на крошечном размере), не 0.5%.
    result["match_within_3pct_accounting_for_known_hook_skim"] = (rel_diff is not None and rel_diff < 0.03)

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_extsload_slot0_pointcheck_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
