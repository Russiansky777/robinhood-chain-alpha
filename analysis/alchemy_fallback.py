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
from Crypto.Hash import keccak

from config import CONFIG

# Топики считаем из сигнатур событий, а не хардкодим — надёжнее.
UNISWAP_V3_SWAP_SIG = "Swap(address,address,int256,int256,uint160,uint128,int24)"
UNISWAP_V4_SWAP_SIG = "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
UNISWAP_V3_POOL_CREATED_SIG = "PoolCreated(address,address,uint24,int24,address)"
UNISWAP_V4_INITIALIZE_SIG = "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"


def topic0(signature: str) -> str:
    # pycryptodome (уже в requirements.txt для Sprint G1/R1, см.
    # analysis/g1_common.py, analysis/r1_common.py) -- не web3.py, чтобы
    # не тащить новую тяжёлую зависимость ради одной функции; исходно
    # здесь стоял Web3.keccak (заготовка ни разу не запускалась, web3
    # не был установлен и не был в requirements.txt -- баг, найден и
    # исправлен при подготовке P3-гарда, 2026-09-01).
    h = keccak.new(digest_bits=256)
    h.update(signature.encode())
    return "0x" + h.hexdigest()


def _rpc_url() -> str:
    if CONFIG.alchemy_rpc_url:
        return CONFIG.alchemy_rpc_url
    if CONFIG.alchemy_api_key:
        return f"https://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}"
    # Фолбэк без секрета -- публичный JSON-RPC прокси Blockscout (см.
    # config.py, blockscout_rpc_url). Найдено и подключено при
    # подготовке P3-гарда (2026-09-01): ALCHEMY_API_KEY в этом
    # репозитории оказался НЕ настроен как секрет GH Actions (первый
    # реальный прогон .github/workflows/run_p3_guard.yml упал именно на
    # этом -- см. docs/P3_GUARD.md), а задание прямо разрешало
    # "RPC/Blockscout" как источник.
    if CONFIG.blockscout_rpc_url:
        return CONFIG.blockscout_rpc_url
    raise RuntimeError(
        "Ни ALCHEMY_API_KEY/ALCHEMY_ROBINHOOD_RPC_URL, ни BLOCKSCOUT_RPC_URL "
        "не заданы. Заполните .env (см. .env.example)."
    )


# Blockscout eth-rpc прокси вернул 403 Forbidden без User-Agent (см.
# docs/P3_GUARD.md -- WAF/Cloudflare перед публичными block explorer'ами
# обычно блокирует запросы с дефолтным `python-requests/x.y` UA как
# похожие на неразмеченный скрейпинг-бот). Не обход защиты -- обычный
# заголовок легитимного клиента, тот же паттерн используют
# документированные публичные интеграции с Blockscout.
_HEADERS = {"User-Agent": "robinhood-chain-alpha-p3-guard/1.0"}


def _rpc_call(method: str, params: list) -> dict:
    """Единичный JSON-RPC вызов (не для eth_getLogs -- см. _chunked_get_logs
    для постраничной версии). Используется P3-гардом (analysis/
    p3_dislocation_guard.py) для eth_blockNumber/eth_getBlockByNumber/
    eth_getTransactionByHash -- лёгкие точечные вызовы, не диапазон блоков."""
    url = _rpc_url()
    resp = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=_HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"RPC {method} error ({url}): {body['error']}")
    return body["result"]


def get_block_number() -> int:
    return int(_rpc_call("eth_blockNumber", []), 16)


def get_block(block_number: int) -> dict:
    return _rpc_call("eth_getBlockByNumber", [hex(block_number), False])


def get_transaction(tx_hash: str) -> dict:
    return _rpc_call("eth_getTransactionByHash", [tx_hash])


def _chunked_get_logs(
    from_block: int,
    to_block: int,
    topics: list,
    chunk_size: int = 2000,
    address: str | list[str] | None = None,
) -> Iterator[dict]:
    """eth_getLogs постранично, чтобы не упереться в лимит провайдера на
    диапазон блоков за запрос. Возвращает сырые логи по одному.

    `topics` — либо плоский список topic0[,topic1,...] (как раньше,
    обратная совместимость), либо уже готовый список позиций топиков
    (каждая позиция — строка или список строк/None для OR/wildcard) --
    передаётся в payload как есть, если элемент сам является списком.
    `address` — опциональный фильтр по адресу контракта (одна строка
    или список строк), см. Sprint P3-гард (analysis/
    p3_dislocation_guard.py) — сужает диапазон без знания topic1/2
    заранее, дешевле для широких по времени, но узких по адресу сканов.
    """
    url = _rpc_url()

    def _get_range(lo: int, hi: int) -> Iterator[dict]:
        filter_obj: dict = {
            "fromBlock": hex(lo),
            "toBlock": hex(hi),
            "topics": topics,
        }
        if address is not None:
            filter_obj["address"] = address
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [filter_obj],
        }
        resp = requests.post(url, json=payload, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"eth_getLogs error ({url}): {body['error']}")
        result = body.get("result", [])
        # Blockscout's public eth-rpc proxy caps eth_getLogs at 1000
        # results/request (docs.blockscout.com/devs/apis/rpc/eth-rpc) --
        # найдено при подготовке P3-гарда (2026-09-01). Ровно 1000 --
        # подозрение на молчаливую обрезку (провайдер не поднимает
        # ошибку) -- бисекция диапазона блоков вместо тихой потери
        # логов (владелец: "никогда не выдумывай данные").
        if len(result) >= 1000 and hi > lo:
            mid = (lo + hi) // 2
            yield from _get_range(lo, mid)
            yield from _get_range(mid + 1, hi)
        else:
            yield from result

    block = from_block
    while block <= to_block:
        end = min(block + chunk_size - 1, to_block)
        yield from _get_range(block, end)
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
