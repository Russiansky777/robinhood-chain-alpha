"""Фолбэк-путь получения данных, если Dune free-tier кредиты (2500/мес)
исчерпаны или покрытие Robinhood Chain на Dune ещё не готово.

Тянет `Swap`-логи Uniswap v3 (Pool) и v4 (PoolManager) напрямую через
Alchemy `eth_getLogs` за диапазон блоков, без прохода через Dune вообще.

Статус: интерактивная сессия по-прежнему не имеет сети до этого домена
(egress-прокси блокирует всё, кроме github.com — см. docs/DATA_ACCESS.md),
но с GH Actions runner'а публичный `PUBLIC_RPC_URL` (без ключа)
реально работает — подтверждено прогоном 2026-09-01 (docs/P3_GUARD.md,
"Проба публичного RPC..."). Alchemy/Blockscout остаются
фолбэком — см. `_endpoints()`. Компромиссы относительно SQL-пути:

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

import time
from typing import Callable, Iterator

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


_BASE_HEADERS = {"User-Agent": "robinhood-chain-alpha-p3-guard/1.0"}
_BACKOFF_BASE_S = 2.0


def _endpoints() -> list[tuple[str, dict]]:
    """Упорядоченный список (base_url, доп.заголовки) -- первый в
    приоритете, следующие -- ФОЛБЭК.

    ПУБЛИЧНЫЙ RPC (CONFIG.public_rpc_url) -- ПЕРВЫЙ с 2026-09-01
    (владелец, дозапрос): реальный прогон GH Actions (run 33570102743,
    см. docs/P3_GUARD.md, "Проба публичного RPC...") подтвердил --
    eth_blockNumber и eth_getLogs (диапазоны 1000/2000 блоков) проходят
    БЕЗ 403 и без ключа; троттлинг (429) начинается при быстрых
    последовательных вызовах (~3 запроса/с по факту). Раньше (до этого
    дозапроса) единственными вариантами были платные ключи -- Alchemy
    и Blockscout PRO API (см. историю ниже) -- теперь они ФОЛБЭК,
    используются только если публичный RPC вернул стойкую (не 429)
    ошибку на всех попытках."""
    endpoints: list[tuple[str, dict]] = []
    if CONFIG.public_rpc_url:
        endpoints.append((CONFIG.public_rpc_url, {}))
    if CONFIG.alchemy_rpc_url:
        endpoints.append((CONFIG.alchemy_rpc_url, {}))
    elif CONFIG.alchemy_api_key:
        endpoints.append((f"https://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}", {}))
    if CONFIG.blockscout_api_key:
        # ВАЖНО (найдено при подготовке P3-гарда, 2026-09-01, см.
        # docs/P3_GUARD.md): прямой безключевой eth-rpc-прокси
        # Blockscout для ЭТОГО чейна вернул 403 -- обслуживается через
        # платный "PRO API" гейтвей (api.blockscout.com/4663/json-rpc),
        # требующий Bearer-токен даже на бесплатном тире.
        endpoints.append((CONFIG.blockscout_rpc_url, {"Authorization": f"Bearer {CONFIG.blockscout_api_key}"}))
    if not endpoints:
        # Практически недостижимо -- CONFIG.public_rpc_url всегда имеет
        # дефолт -- но если владелец явно очистил PUBLIC_RPC_URL="" И
        # ключи не заданы, явная ошибка лучше тихого сбоя.
        raise RuntimeError(
            "Ни PUBLIC_RPC_URL, ни ALCHEMY_API_KEY/ALCHEMY_ROBINHOOD_RPC_URL, "
            "ни BLOCKSCOUT_API_KEY не заданы -- нет ни одного RPC-эндпоинта. "
            "Заполните .env (см. .env.example) или добавьте секрет GH Actions."
        )
    return endpoints


def _rpc_url() -> str:
    """Текущий ПЕРВЫЙ (приоритетный) эндпоинт -- для кода, которому
    нужен один URL без встроенного фолбэка/ретрая (напр. диагностика).
    Основной путь запросов (_rpc_call/_chunked_get_logs) сам перебирает
    весь список _endpoints(), не полагается только на эту функцию."""
    return _endpoints()[0][0]


def _auth_headers() -> dict:
    """Заголовки ПЕРВОГО (приоритетного) эндпоинта -- см. оговорку
    _rpc_url() выше про встроенный фолбэк основного пути."""
    _, extra = _endpoints()[0]
    return {**_BASE_HEADERS, **extra}


# НАЙДЕНО 2026-09-02 (run 33576972863): даже 10 попыток/capped-20с
# backoff (~130с суммарно) НЕ хватило -- 429 держался устойчиво
# несколько минут подряд. Вероятная причина (не подтверждено
# источником, честная гипотеза): GH Actions runner'ы делят пулы
# исходящих IP между МНОГИМИ независимыми job'ами разных пользователей
# одновременно -- если у провайдера рейт-лимит по IP, а не по клиенту,
# наш собственный самотроттлинг не защищает от чужого трафика с того
# же IP. Единственная надёжная защита -- ждать ДОЛЬШЕ, не больше
# попыток за то же время. 429 переведён на БЮДЖЕТ ПО ВРЕМЕНИ (см.
# `_RATE_LIMIT_WAIT_BUDGET_S`), не по числу попыток.
_RATE_LIMIT_WAIT_BUDGET_S = 900.0  # 15 минут суммарного ожидания на 429 для ОДНОГО запроса, прежде чем сдаться на этом эндпоинте
_BACKOFF_CAP_S = 25.0
_MIN_REQUEST_INTERVAL_S = 0.5  # ~2 req/s -- ещё консервативнее прежних 0.35с/~2.9 req/с, см. оговорку выше про общий IP-пул
_last_request_at = 0.0


def _throttle() -> None:
    """Минимальный интервал между запросами К ЛЮБОМУ эндпоинту --
    найдено 2026-09-02 (run 33575241260): реактивного ретрая на 429
    недостаточно при устойчивой нагрузке (тысячи eth_getLogs подряд) --
    после нескольких retry с backoff всё равно исчерпывается лимит
    попыток и весь прогон падает. Проактивный самотроттлинг снижает
    частоту 429 в принципе, а не только реагирует на них постфактум."""
    global _last_request_at
    wait = _last_request_at + _MIN_REQUEST_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _post_with_fallback(payload: dict) -> dict:
    """POST payload по списку `_endpoints()` в порядке приоритета.

    На 429 (rate-limit) И на прочие транзиентные JSON-RPC ошибки (см.
    `_TRANSIENT_ERROR_MARKERS` -- эвристика по коду -32000/-32603 +
    маркерам сообщения) -- ОДИНАКОВЫЙ путь: retry с экспоненциальным
    backoff (капнутым `_BACKOFF_CAP_S`) НА ТОМ ЖЕ эндпоинте, по БЮДЖЕТУ
    ВРЕМЕНИ `_RATE_LIMIT_WAIT_BUDGET_S` (не по числу попыток -- см. её
    докстринг: фиксированное число попыток эмпирически не хватало ни
    разу за три разных инцидента одного и того же спринта) прежде чем
    перейти к следующему эндпоинту -- обе категории одинаково
    транзиентны, не повод сразу тратить платный ключ. На 401/403 --
    сразу переход к следующему эндпоинту (без ретрая на этом же -- не
    транзиентно). Возвращает распарсенный JSON-body первого успешного
    ответа (включая JSON-RPC-level `"error"` в теле, если он НЕ
    транзиентный -- это не транспортная проблема, вызывающий код сам
    решает, что с ней делать).
    Кидает RuntimeError, если ВСЕ эндпоинты исчерпаны."""
    last_err: Exception | str | None = None
    for url, extra_headers in _endpoints():
        headers = {**_BASE_HEADERS, **extra_headers}
        attempt = 0
        rate_limit_deadline: float | None = None
        while True:
            _throttle()
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
            except requests.exceptions.RequestException as e:
                last_err = f"{url}: сетевая ошибка {e}"
                break  # не ретраим сетевые ошибки на этом URL -- следующий эндпоинт
            if resp.status_code == 429:
                now = time.monotonic()
                if rate_limit_deadline is None:
                    rate_limit_deadline = now + _RATE_LIMIT_WAIT_BUDGET_S
                if now < rate_limit_deadline:
                    time.sleep(min(_BACKOFF_BASE_S * (2 ** attempt), _BACKOFF_CAP_S))
                    attempt += 1
                    continue
                last_err = f"{url}: 429 устойчиво в течение {_RATE_LIMIT_WAIT_BUDGET_S:.0f}с -- сдаюсь на этом эндпоинте"
                break
            if resp.status_code in (401, 403):
                last_err = f"{url}: {resp.status_code} {resp.text[:300]!r}"
                break
            try:
                resp.raise_for_status()
                body = resp.json()
            except requests.exceptions.HTTPError as e:
                last_err = f"{url}: {e}"
                break
            except ValueError:
                last_err = f"{url}: не-JSON ответ {resp.status_code} {resp.text[:300]!r}"
                break
            # НАЙДЕНО 2026-09-01 (реальный прогон wash-slice, run
            # 33571808939): сам публичный RPC-гейтвей — это прокси
            # перед внутренним нодом; при сбое ЭТОГО внутреннего дайла
            # он возвращает 200 OK с JSON-RPC `"error"` вида
            # `{'code': -32000, 'message': 'Post "http://10.x.x.x:8547/rpc":
            # dial tcp ...: i/o timeout'}` -- транспортно это выглядит
            # как успех (raise_for_status() не сработал бы), но по сути
            # та же транзиентная проблема, что и 429. Раньше это сразу
            # каскадом улетало в RuntimeError вызывающему коду и роняло
            # весь прогон (36 минут работы потеряно на одном таком сбое)
            # -- теперь ретраится здесь же, тем же бюджетом времени, что
            # 429 (см. докстринг выше).
            err = body.get("error") if isinstance(body, dict) else None
            if err is not None and _looks_transient(err):
                # НАЙДЕНО 2026-09-02 (run 33608868849): фиксированные 3
                # попытки здесь оказались тем же недостаточным бюджетом,
                # что раньше был у 429 -- 'log query timed out' на CONTROL
                # рухнул без единого шанса на восстановление после
                # рестарта соединения с апстримом. Тот же бюджет по
                # времени, что 429 (см. `_RATE_LIMIT_WAIT_BUDGET_S`) --
                # обе категории транзиентны одинаково, отдельного
                # меньшего бюджета для этой ветки больше нет.
                now = time.monotonic()
                if rate_limit_deadline is None:
                    rate_limit_deadline = now + _RATE_LIMIT_WAIT_BUDGET_S
                if now < rate_limit_deadline:
                    time.sleep(min(_BACKOFF_BASE_S * (2 ** attempt), _BACKOFF_CAP_S))
                    attempt += 1
                    continue
                last_err = f"{url}: транзиентная RPC-ошибка устойчиво в течение {_RATE_LIMIT_WAIT_BUDGET_S:.0f}с: {err}"
                break
            return body
    raise RuntimeError(f"Все RPC-эндпоинты исчерпаны. Последняя ошибка: {last_err}")


_TRANSIENT_ERROR_MARKERS = (
    # НАЙДЕНО 2026-09-02 (run 33608868849, третий по счёту РАЗНЫЙ текст
    # транзиентной ошибки за один спринт): узкий список конкретных фраз
    # -- вечная игра в "поймай следующую формулировку". CONTROL упал на
    # {'code': -32000, 'message': 'log query timed out'} -- "timed out"
    # (с пробелом) не совпадало ни с одним из прежних маркеров ("i/o
    # timeout" и т.п.), ошибка ушла в RuntimeError без единой попытки
    # ретрая. Список расширен generic-словами -- для eth_getLogs ЛЮБАЯ
    # JSON-RPC error (-32000/-32603) по сути ВСЕГДА транзиентна (нет
    # "легитимного revert" у чтения логов, в отличие от eth_call) --
    # но код используется и для eth_call (launcher), где revert может
    # быть содержательным, поэтому остаёмся в рамках эвристики по
    # словам, а не "любая -32000/-32603 = ретраить" целиком.
    "i/o timeout", "dial tcp", "connection reset", "connection refused",
    "eof", "context deadline exceeded", "no such host", "timeout awaiting",
    "temporarily unavailable", "upstream", "bad gateway", "gateway timeout",
    "internal error", "timed out", "timeout", "rate limit", "too many requests",
    "resource exhausted", "unavailable", "try again", "overloaded", "busy",
    "query timed", "request timed",
)


def _looks_transient(err: dict) -> bool:
    """Эвристика (не документированный контракт провайдера -- честно
    приблизительная): код -32000/-32603 ("generic"/"internal error" в
    JSON-RPC) с сообщением, указывающим на сетевую/upstream-проблему
    самого гейтвея, а не на содержательную ошибку запроса (некорректные
    параметры, неизвестный метод и т.п., которые ретраить бессмысленно
    -- они повторятся идентично)."""
    code = err.get("code")
    if code not in (-32000, -32603):
        return False
    msg = str(err.get("message", "")).lower()
    return any(marker in msg for marker in _TRANSIENT_ERROR_MARKERS)


def _rpc_call(method: str, params: list) -> dict:
    """Единичный JSON-RPC вызов (не для eth_getLogs -- см. _chunked_get_logs
    для постраничной версии). Используется P3-гардом (analysis/
    p3_dislocation_guard.py) для eth_blockNumber/eth_getBlockByNumber/
    eth_getTransactionByHash -- лёгкие точечные вызовы, не диапазон блоков."""
    body = _post_with_fallback({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in body:
        raise RuntimeError(f"RPC {method} error: {body['error']}")
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
    on_call: Callable[[int, int, int], None] | None = None,
) -> Iterator[dict]:
    """eth_getLogs постранично, чтобы не упереться в лимит провайдера на
    диапазон блоков за запрос. Возвращает сырые логи по одному. Каждый
    реальный HTTP-вызов идёт через `_post_with_fallback` -- ретрай на
    429 + переход на фолбэк-эндпоинт при стойкой ошибке (см. выше).

    `topics` — либо плоский список topic0[,topic1,...] (как раньше,
    обратная совместимость), либо уже готовый список позиций топиков
    (каждая позиция — строка или список строк/None для OR/wildcard) --
    передаётся в payload как есть, если элемент сам является списком.
    `address` — опциональный фильтр по адресу контракта (одна строка
    или список строк), см. Sprint P3-гард (analysis/
    p3_dislocation_guard.py) — сужает диапазон без знания topic1/2
    заранее, дешевле для широких по времени, но узких по адресу сканов.
    `on_call(lo, hi, n_results)` — опциональный колбэк, вызывается
    после КАЖДОГО реального HTTP-вызова (для верификации оценок
    стоимости постфактум, см. analysis/sc1_wash_slice.py).
    """

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
        body = _post_with_fallback(payload)
        if "error" in body:
            raise RuntimeError(f"eth_getLogs error [{lo};{hi}]: {body['error']}")
        result = body.get("result", [])
        if on_call is not None:
            on_call(lo, hi, len(result))
        # Blockscout's public eth-rpc proxy caps eth_getLogs at 1000
        # results/request (docs.blockscout.com/devs/apis/rpc/eth-rpc) --
        # найдено при подготовке P3-гарда (2026-09-01). Ровно 1000 --
        # подозрение на молчаливую обрезку (провайдер не поднимает
        # ошибку) -- бисекция диапазона блоков вместо тихой потери
        # логов (владелец: "никогда не выдумывай данные"). Публичный
        # RPC (теперь основной эндпоинт) такого капа не документирует,
        # но проверка безвредна и на нём -- оставлена как есть.
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
