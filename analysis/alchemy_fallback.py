"""Фолбэк-путь получения данных, если Dune free-tier кредиты (2500/мес)
исчерпаны или покрытие Robinhood Chain на Dune ещё не готово.

Тянет `Swap`-логи Uniswap v3 (Pool) и v4 (PoolManager) напрямую через
Alchemy `eth_getLogs` за диапазон блоков, без прохода через Dune вообще.

Статус: заготовка (не выполнялась — нет сети до *.alchemy.com из этой
среды, см. docs/DATA_ACCESS.md). Компромиссы относительно SQL-пути:

- Не даёт готовый amount_usd — цены нужно джойнить отдельно (напр. через
  Alchemy Prices API или CoinGecko по timestamp блока), здесь оставлен
  TODO.
- Нужно заранее знать адреса пулов (или сначала выгрести
  `PoolCreated`/`Initialize` события фабрики — тем же eth_getLogs).
- eth_getLogs на большинстве RPC ограничен диапазоном блоков за один
  запрос (обычно 2k-10k блоков/запрос) — при ~100мс блоктайме Robinhood
  Chain это ОЧЕНЬ много запросов на месяц (порядка 300k блоков/день).
  Планируйте пагинацию и параллелизацию заранее (см. `_chunked_get_logs`).
"""
from __future__ import annotations

from typing import Iterator

import requests
from web3 import Web3

from config import CONFIG

# Топики считаем из сигнатур событий, а не хардкодим — надёжнее.
UNISWAP_V3_SWAP_SIG = "Swap(address,address,int256,int256,uint160,uint128,int24)"
UNISWAP_V4_SWAP_SIG = "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
UNISWAP_V3_POOL_CREATED_SIG = "PoolCreated(address,address,uint24,int24,address)"
UNISWAP_V4_INITIALIZE_SIG = "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"


def topic0(signature: str) -> str:
    return Web3.keccak(text=signature).hex()


def _rpc_url() -> str:
    if CONFIG.alchemy_rpc_url:
        return CONFIG.alchemy_rpc_url
    if CONFIG.alchemy_api_key:
        return f"https://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}"
    raise RuntimeError(
        "ALCHEMY_API_KEY / ALCHEMY_ROBINHOOD_RPC_URL не заданы. "
        "Заполните .env (см. .env.example)."
    )


def _chunked_get_logs(
    from_block: int, to_block: int, topics: list[str], chunk_size: int = 2000
) -> Iterator[dict]:
    """eth_getLogs постранично, чтобы не упереться в лимит провайдера на
    диапазон блоков за запрос. Возвращает сырые логи по одному.
    """
    url = _rpc_url()
    block = from_block
    while block <= to_block:
        end = min(block + chunk_size - 1, to_block)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [
                {
                    "fromBlock": hex(block),
                    "toBlock": hex(end),
                    "topics": [topics],
                }
            ],
        }
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"Alchemy eth_getLogs error: {body['error']}")
        yield from body.get("result", [])
        block = end + 1


def fetch_v3_swap_logs(from_block: int, to_block: int) -> list[dict]:
    return list(_chunked_get_logs(from_block, to_block, [topic0(UNISWAP_V3_SWAP_SIG)]))


def fetch_v4_swap_logs(from_block: int, to_block: int) -> list[dict]:
    return list(_chunked_get_logs(from_block, to_block, [topic0(UNISWAP_V4_SWAP_SIG)]))


def fetch_pool_creation_logs(from_block: int, to_block: int) -> dict[str, list[dict]]:
    return {
        "v3_pool_created": list(
            _chunked_get_logs(from_block, to_block, [topic0(UNISWAP_V3_POOL_CREATED_SIG)])
        ),
        "v4_initialize": list(
            _chunked_get_logs(from_block, to_block, [topic0(UNISWAP_V4_INITIALIZE_SIG)])
        ),
    }


# TODO (при реальном переключении на этот фолбэк):
# 1. decode_v3_swap_log / decode_v4_swap_log — ABI-декодирование через
#    web3.codec (топики известны, нужен только decode data по типам сигнатуры).
# 2. Резолв block_number -> block_time батчем через eth_getBlockByNumber
#    (или Alchemy `alchemy_getBlockRange`, если доступен на чейне).
# 3. USD-цены: Alchemy Prices API по (token_address, timestamp) либо
#    привязка к стейблкоин-ноге сделки (если одна из сторон свопа —
#    USDC/USDT, amount_usd = amount этой ноги напрямую, без оракула).
# 4. Дальше — тот же weighted-average-cost PnL леджер, что и в
#    sql/03_wallet_agg_july.sql, но на pandas DataFrame вместо SQL.
if __name__ == "__main__":
    raise SystemExit(
        "Заготовка, не для прямого запуска. См. docstring и TODO выше. "
        "Используйте после того, как определите точные block ranges "
        "июля/августа 2026 и адреса пулов."
    )
