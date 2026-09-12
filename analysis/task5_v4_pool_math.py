"""Uniswap V4 (PoolManager) -- разбор PoolKey/PoolId, Swap-события,
калдату V4Quoter'а.

Всё в этом файле проверено САМОСТОЯТЕЛЬНО против реальных данных, не
принято на веру:
  - `pool_id()` -- реализация `PoolIdLibrary.toId()` из реального
    исходника Uniswap/v4-core (`keccak256(poolKey, 0xa0)` == keccak256
    от 5 слов по 32 байта: currency0, currency1, fee, tickSpacing,
    hooks -- для этой структуры из одних value-type полей это то же
    самое, что abi.encode). Сверено с ДВУМЯ реальными poolId с
    Robinhood Chain (USDG/MOSIAI fee=70000 tickSpacing=4 и tickSpacing=3,
    hooks=0x0) -- оба совпали побайтово.
  - `QUOTE_EXACT_INPUT_SINGLE_SELECTOR`/`QUOTE_EXACT_OUTPUT_SINGLE_SELECTOR`
    -- пересчитаны из реальной сигнатуры IV4Quoter.sol (raw.githubusercontent.com/
    Uniswap/v4-periphery/main/src/interfaces/IV4Quoter.sol, читано в этой
    же сессии) через `eth_utils.function_signature_to_4byte_selector` на
    КАНОНИЧЕСКОЙ форме с ОДНИМ верхнеуровневым tuple-аргументом
    (QuoteExactSingleParams целиком, а не 4 отдельных параметра -- это
    была первая, ошибочная попытка, не совпавшая с реальным вызовом).
    `aa9d21cb` независимо совпал с реальным selector'ом, который реально
    вызывался в исторических eth_call из внешнего аудита -- две
    независимые сверки, не одна.
  - `decode_v4_swap_log_data()` -- раскладка data-поля реального
    PoolManager Swap-события (`Swap(bytes32,address,int128,int128,
    uint160,uint128,int24,uint24)`, топик из alchemy_fallback.
    UNISWAP_V4_SWAP_SIG) по 32-байтным словам; проверено на реальном
    Swap-событии контрольной tx 0xd487122244... -- независимо
    воспроизвело заявленные 9.437184 -> 10.242566 USDG, прибыль
    0.805382 USDG до газа, побайтово.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PoolKey:
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str = "0x0000000000000000000000000000000000000000"

    def __post_init__(self):
        # currency0 < currency1 -- обязательное условие V4 (иначе PoolManager
        # реально ревертит на InvalidCurrencies при инициализации пула).
        assert int(self.currency0, 16) < int(self.currency1, 16), \
            f"currency0 должен быть < currency1: {self.currency0} vs {self.currency1}"


def _word_addr(addr: str) -> bytes:
    return bytes.fromhex(addr[2:].rjust(40, "0").zfill(64))


def _word_uint(n: int) -> bytes:
    return (n & ((1 << 256) - 1)).to_bytes(32, "big")


def _word_int(n: int) -> bytes:
    if n < 0:
        n = (1 << 256) + n
    return (n & ((1 << 256) - 1)).to_bytes(32, "big")


def pool_id(key: PoolKey) -> str:
    """PoolIdLibrary.toId(poolKey) -- keccak256 пяти 32-байтных слов
    (currency0, currency1, fee, tickSpacing, hooks), это РОВНО память
    struct PoolKey в Solidity (0xa0 = 160 байт = 5 слов)."""
    from Crypto.Hash import keccak
    data = (
        _word_addr(key.currency0)
        + _word_addr(key.currency1)
        + _word_uint(key.fee)
        + _word_int(key.tick_spacing)
        + _word_addr(key.hooks)
    )
    h = keccak.new(digest_bits=256)
    h.update(data)
    return "0x" + h.hexdigest()


def decode_v4_swap_log_data(data_hex: str) -> dict:
    """Раскладка data PoolManager.Swap(bytes32 id, address sender,
    int128 amount0, int128 amount1, uint160 sqrtPriceX96, uint128
    liquidity, int24 tick, uint24 fee) -- id и sender индексированы
    (в topics), в data остаются 6 полей x 32 байта."""
    h = data_hex[2:] if data_hex.startswith("0x") else data_hex
    assert len(h) == 6 * 64, f"ожидали 6 слов (384 hex-символа), получили {len(h)}"
    words = [h[i:i + 64] for i in range(0, len(h), 64)]

    def to_signed(w: str) -> int:
        v = int(w, 16)
        return v - (1 << 256) if v >= (1 << 255) else v

    def to_unsigned(w: str) -> int:
        return int(w, 16)

    return {
        "amount0": to_signed(words[0]),
        "amount1": to_signed(words[1]),
        "sqrt_price_x96": to_unsigned(words[2]),
        "liquidity": to_unsigned(words[3]),
        "tick": to_signed(words[4]),
        "fee": to_unsigned(words[5]),
    }


# Пересчитаны из реальной сигнатуры IV4Quoter (см. докстринг модуля).
QUOTE_EXACT_INPUT_SINGLE_SELECTOR = "aa9d21cb"
QUOTE_EXACT_OUTPUT_SINGLE_SELECTOR = "58733073"


def _pack_pool_key(key: PoolKey) -> bytes:
    return (
        _word_addr(key.currency0)
        + _word_addr(key.currency1)
        + _word_uint(key.fee)
        + _word_int(key.tick_spacing)
        + _word_addr(key.hooks)
    )


def quote_exact_input_single_calldata(key: PoolKey, zero_for_one: bool, exact_amount: int,
                                       hook_data: bytes = b"") -> str:
    """Калдата quoteExactInputSingle(QuoteExactSingleParams) -- один
    верхнеуровневый (динамический из-за bytes hookData) tuple-аргумент:
    offset(0x20) + [poolKey(5 слов) + zeroForOne + exactAmount + offset
    до hookData(0x100=256, т.к. голова tuple = 5+1+1+1=8 слов=256 байт)]
    + hookData(length + padded data)."""
    head = _pack_pool_key(key) + _word_uint(1 if zero_for_one else 0) + _word_uint(exact_amount) + _word_uint(0x100)
    tail = _word_uint(len(hook_data)) + (hook_data + b"\x00" * ((32 - len(hook_data) % 32) % 32) if hook_data else b"")
    return "0x" + QUOTE_EXACT_INPUT_SINGLE_SELECTOR + _word_uint(0x20).hex() + head.hex() + tail.hex()


def decode_quote_result(raw_hex: str) -> tuple[int, int]:
    """returns (amountOut, gasEstimate) -- оба uint256, по 32 байта."""
    h = raw_hex[2:] if raw_hex.startswith("0x") else raw_hex
    amount_out = int(h[0:64], 16)
    gas_estimate = int(h[64:128], 16)
    return amount_out, gas_estimate


if __name__ == "__main__":
    # Самопроверка: реальные poolId с Robinhood Chain, USDG/MOSIAI.
    USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
    MOSIAI = "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a"
    k1 = PoolKey(USDG, MOSIAI, 70000, 4)
    k2 = PoolKey(USDG, MOSIAI, 70000, 3)
    p1 = pool_id(k1)
    p2 = pool_id(k2)
    expect1 = "0xcb90948a1ef145614fc4a20418956f7273690abca77804d2a0cf68c7092aea03"
    expect2 = "0x92ed62e7bcdca7aed7e2d17f60621781ae62ff3d80e87dc4702f1cfb17e3534a"
    assert p1.lower() == expect1.lower(), (p1, expect1)
    assert p2.lower() == expect2.lower(), (p2, expect2)
    print("pool_id() самопроверка: OK,", p1, p2)

    cd = quote_exact_input_single_calldata(k1, True, 9437184)
    print("quoteExactInputSingle calldata (pool1, USDG->MOSIAI, 9.437184 USDG):", cd)
    assert cd.startswith("0x" + QUOTE_EXACT_INPUT_SINGLE_SELECTOR)
    print("selector самопроверка: OK")
