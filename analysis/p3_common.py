"""P3-гард: общие декодеры/константы для событий Uniswap v3/v4 --
доля топ-3 адресов в кросс-версионных (v3<->v4) закрытиях дислокаций
топ-5 сток-токенов за последние 7 дней. Без Dune -- eth_getLogs через
Alchemy RPC (analysis/alchemy_fallback.py). Владелец, 2026-09-01: "один
дешёвый гард... если >80% -- P3 закрывается окончательно."

topic0 для всех событий -- посчитаны локально (Crypto.Hash.keccak) от
официальных сигнатур событий, проверенных дословно по первоисточнику
(GitHub Uniswap/v3-core IUniswapV3PoolEvents.sol, Uniswap/v4-core
IPoolManager.sol) -- см. воспроизведение ниже. Совпадают с уже
посчитанными в analysis/alchemy_fallback.py (та же сигнатура) -- этот
модуль реэкспортирует их оттуда, не дублирует хардкодом.

    from Crypto.Hash import keccak
    def topic0(sig: str) -> str:
        h = keccak.new(digest_bits=256)
        h.update(sig.encode())
        return "0x" + h.hexdigest()

    topic0("Swap(address,address,int256,int256,uint160,uint128,int24)")
    # -> V3 Swap
    topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
    # -> V4 Swap
    topic0("PoolCreated(address,address,uint24,int24,address)")
    # -> V3 PoolCreated (Factory)
    topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
    # -> V4 Initialize (PoolManager)
"""
from __future__ import annotations

from alchemy_fallback import (
    UNISWAP_V3_POOL_CREATED_SIG,
    UNISWAP_V3_SWAP_SIG,
    UNISWAP_V4_INITIALIZE_SIG,
    UNISWAP_V4_SWAP_SIG,
    topic0,
)
from g1_common import decode_address_word, decode_uint_word

TOPIC0_V3_SWAP = topic0(UNISWAP_V3_SWAP_SIG)
TOPIC0_V4_SWAP = topic0(UNISWAP_V4_SWAP_SIG)
TOPIC0_V3_POOL_CREATED = topic0(UNISWAP_V3_POOL_CREATED_SIG)
TOPIC0_V4_INITIALIZE = topic0(UNISWAP_V4_INITIALIZE_SIG)

# Кандидат-адрес Uniswap V3 Factory -- детерминированный CREATE2-адрес,
# одинаковый на десятках EVM-чейнов с официальным деплоем Uniswap Labs
# (Ethereum/Arbitrum/Optimism/Base/Polygon/...). НЕ подтверждён для
# Robinhood Chain напрямую (developers.uniswap.org и блокскан
# заблокированы egress-прокси интерактивной песочницы, см.
# docs/P3_GUARD.md) -- стадия discover проверяет его САМА (по коду
# контракта + по факту, что PoolCreated-логи с этого адреса реально
# существуют в узком срезе), а не полагается на это слепо. Если
# discover его не подтвердит -- фоллбэк на chain-wide скан без address
# (дороже, но не требует знания адреса заранее).
CANDIDATE_V3_FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"


def decode_pool_created(log: dict) -> dict:
    """PoolCreated(address indexed token0, address indexed token1,
    uint24 indexed fee, int24 tickSpacing, address pool). token0/token1/fee
    -- indexed (topics 1-3). tickSpacing (int24) + pool (address) -- в data,
    два 32-байтных слова."""
    topics = log["topics"]
    data = str(log["data"]).removeprefix("0x")
    tick_spacing_word = data[0:64]
    pool_word = data[64:128]
    # int24 в ABI-энкодинге `data` -- знако-расширено на все 256 бит
    # (не на 24), тот же приём, что decode_answer_updated (analysis/
    # r1_common.py) для int256: расширение сохраняет числовое значение,
    # поэтому проверяем старший бит полного 256-битного слова.
    tick_spacing_raw = int(tick_spacing_word, 16) if tick_spacing_word else 0
    if tick_spacing_raw >= 2 ** 255:
        tick_spacing_raw -= 2 ** 256
    return {
        "tx_hash": log["transactionHash"],
        "block_number": int(log["blockNumber"], 16),
        "factory_address": str(log["address"]).lower(),
        "token0": decode_address_word(topics[1]),
        "token1": decode_address_word(topics[2]),
        "fee": decode_uint_word(topics[3]),
        "tick_spacing": tick_spacing_raw,
        "pool": decode_address_word(pool_word) if pool_word else None,
    }


def decode_v4_initialize(log: dict) -> dict:
    """Initialize(bytes32 indexed id, address indexed currency0,
    address indexed currency1, uint24 fee, int24 tickSpacing,
    address hooks, uint160 sqrtPriceX96, int24 tick). id/currency0/
    currency1 -- indexed (topics 1-3, currency* уложены как address-слова).
    Остальное -- в data (не нужно для этого гарда, кроме диагностики)."""
    topics = log["topics"]
    return {
        "tx_hash": log["transactionHash"],
        "block_number": int(log["blockNumber"], 16),
        "pool_manager_address": str(log["address"]).lower(),
        "pool_id": str(topics[1]).lower(),
        "currency0": decode_address_word(topics[2]),
        "currency1": decode_address_word(topics[3]),
    }


def decode_v3_swap(log: dict) -> dict:
    """Swap(address indexed sender, address indexed recipient, ...).
    Для этого гарда важны только pool_address (log['address']) и
    tx_hash/block_number -- корреляция по транзакции, не по объёму."""
    return {
        "tx_hash": log["transactionHash"],
        "block_number": int(log["blockNumber"], 16),
        "log_index": int(log["logIndex"], 16),
        "pool_address": str(log["address"]).lower(),
    }


def decode_v4_swap(log: dict) -> dict:
    """Swap(bytes32 indexed id, address indexed sender, ...). id --
    topic1, поштучно связывает событие с конкретным v4-пулом (адрес
    контракта у всех v4-свопов один и тот же -- PoolManager-синглтон,
    см. docs/G1_DESIGN.md)."""
    topics = log["topics"]
    return {
        "tx_hash": log["transactionHash"],
        "block_number": int(log["blockNumber"], 16),
        "log_index": int(log["logIndex"], 16),
        "pool_manager_address": str(log["address"]).lower(),
        "pool_id": str(topics[1]).lower(),
    }
