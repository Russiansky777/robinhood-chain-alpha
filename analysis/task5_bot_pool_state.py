#!/usr/bin/env python3
"""Задача 5, живой бот -- состояние отслеживаемых пулов В ПАМЯТИ,
обновляется из фида, БЕЗ RPC-запросов в горячем пути (владелец,
2026-09-11/12, требование 1). RPC используется ТОЛЬКО один раз при
старте (bootstrap): найти реальные канонические v3-пулы через
PoolCreated-события (0 кредитов Dune -- прямой eth_getLogs, тот же
класс метода, что forensics уже использовала в этом проекте) и прочитать
их начальный slot0 (sqrtPriceX96/tick/liquidity)."""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

import requests
from Crypto.Hash import keccak  # pycryptodome, уже зависимость проекта (topic0 для G1/этой сессии)

from task5_bot_config import (
    CANONICAL_V3_FACTORY,
    POOL_DISCOVERY_LOOKBACK_BLOCKS,
    RPC_URL_MAINNET,
    RPC_URL_PROVIDER_ENV_VAR,
)


def keccak_topic(signature: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(signature.encode())
    return "0x" + h.hexdigest()


# PoolCreated(address indexed token0, address indexed token1, uint24 indexed fee, int24 tickSpacing, address pool)
POOL_CREATED_TOPIC0 = keccak_topic("PoolCreated(address,address,uint24,int24,address)")
# Swap(address indexed sender, address indexed recipient, int256 amount0, int256 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick)
SWAP_TOPIC0 = keccak_topic("Swap(address,address,int256,int256,uint160,uint128,int24)")


@dataclass
class V3PoolState:
    address: str
    token0: str
    token1: str
    fee: int
    sqrt_price_x96: int | None = None
    tick: int | None = None
    liquidity: int | None = None
    last_update_wall: float = field(default_factory=time.time)
    last_update_block: int | None = None

    def implied_price_token1_per_token0(self) -> float | None:
        """Цена token1 за 1 token0, из sqrtPriceX96 (Q64.96) -- НЕ
        нормализована по decimals, вызывающий код должен учесть
        decimals обоих токенов сам (пары в этом проекте -- только
        WETH(18)/USDG(6), см. task5_bot_config)."""
        if self.sqrt_price_x96 is None:
            return None
        return (self.sqrt_price_x96 / (2**96)) ** 2

    def apply_swap(self, sqrt_price_x96: int, liquidity: int, tick: int, block_number: int | None) -> None:
        self.sqrt_price_x96 = sqrt_price_x96
        self.liquidity = liquidity
        self.tick = tick
        self.last_update_wall = time.time()
        self.last_update_block = block_number


_Q96 = 1 << 96


def apply_swap_price_update_from_calldata(pool: "V3PoolState", zero_for_one: bool, amount_in_raw: int,
                                           block_number: int | None = None) -> bool:
    """Владелец, 2026-09-12: "Пока ключа [провайдера] нет -- цены из фида.
    Для пулов, в которых декодер видел своп, цена после свопа
    восстанавливается из amountIn/направления и предыдущего состояния."
    Реальные, публичные формулы Uniswap V3 (`SqrtPriceMath.getNextSqrtPrice
    FromInput`, within-tick, БЕЗ пересечения границ тика):

        zeroForOne (token0 -> pool):  sqrtP' = L*sqrtP*Q96 / (L*Q96 + amountIn*sqrtP)
        !zeroForOne (token1 -> pool): sqrtP' = sqrtP + amountIn*Q96 / L

    `amountIn` -- ПОСЛЕ вычета комиссии пула (`pool.fee` в hundredths of a
    bip, тот же формат, что и остальной проект) -- комиссия остаётся LP,
    не двигает цену, это тоже реальная часть формулы, не отдельная догадка.

    ЧЕСТНАЯ ОГОВОРКА (владелец: "точность не нужна, контракт отсеивает
    сам" -- тот же принцип, что и везде в этом детекторе): формула -- ТОЛЬКО
    в пределах ТЕКУЩЕГО тика (постоянная виртуальная ликвидность `L`), БЕЗ
    симуляции пересечения границ тика (нужен полный тик-бай-тик проход по
    initialized tick'ам с их `liquidityNet` -- не сделано в этой сессии).
    Для свопа, требующего пересечь хотя бы одну границу тика, результат
    будет НЕТОЧНЫМ (обычно недооценивает итоговый сдвиг цены) -- приемлемо
    для триггера (владелец явно принял этот компромисс), НЕ для реального
    расчёта минимального профита (это уже делает контракт при исполнении).

    Возвращает False (не трогает `pool`), если нет базовой цены/ликвидности
    для обновления -- вызывающий код должен в этом случае сначала сделать
    ОДНОРАЗОВЫЙ RPC-запрос (`refresh_pool_price`), не гадать с нуля."""
    if pool.sqrt_price_x96 is None or not pool.liquidity:
        return False
    if amount_in_raw <= 0:
        return False
    fee_pips = pool.fee or 0  # hundredths of a bip, напр. 3000 = 0.3% = 3000/1e6
    amount_in_after_fee = amount_in_raw * (1_000_000 - fee_pips) // 1_000_000
    if amount_in_after_fee <= 0:
        return False

    liquidity = pool.liquidity
    sqrt_p = pool.sqrt_price_x96
    if zero_for_one:
        numerator = liquidity * sqrt_p * _Q96
        denominator = liquidity * _Q96 + amount_in_after_fee * sqrt_p
        if denominator <= 0:
            return False
        sqrt_p_new = numerator // denominator
    else:
        sqrt_p_new = sqrt_p + (amount_in_after_fee * _Q96) // liquidity

    if sqrt_p_new <= 0:
        return False
    pool.sqrt_price_x96 = sqrt_p_new
    pool.last_update_wall = time.time()
    pool.last_update_block = block_number
    return True


class PoolRegistry:
    """В памяти: address -> V3PoolState, плюс индекс по паре токенов для
    быстрого поиска "другие пулы этой же пары" при детекции расхождения."""

    def __init__(self) -> None:
        self.by_address: dict[str, V3PoolState] = {}
        self.by_pair: dict[tuple[str, str], list[str]] = {}

    def _pair_key(self, token0: str, token1: str) -> tuple[str, str]:
        a, b = token0.lower(), token1.lower()
        return (a, b) if a < b else (b, a)

    def register(self, pool: V3PoolState) -> None:
        self.by_address[pool.address.lower()] = pool
        key = self._pair_key(pool.token0, pool.token1)
        self.by_pair.setdefault(key, []).append(pool.address.lower())

    def pools_for_pair(self, token0: str, token1: str) -> list[V3PoolState]:
        key = self._pair_key(token0, token1)
        return [self.by_address[a] for a in self.by_pair.get(key, [])]

    def find_pool_by_tokens_fee(self, token_a: str, token_b: str, fee: int) -> V3PoolState | None:
        """Владелец, 2026-09-12 ('добавка', Путь А): маппинг (token0, token1,
        fee) -> pool из уже существующего реестра. Роутер-calldata (см.
        `task5_bot_router_decode.py`) даёт `tokenIn`/`tokenOut` НЕ обязательно
        в том же порядке, что `token0`/`token1` пула (порядок в пуле --
        канонический, по возрастанию адреса) -- сверяем оба токена как
        неупорядоченную пару + fee, не гадаем по порядку из calldata."""
        for pool in self.pools_for_pair(token_a, token_b):
            if pool.fee == fee:
                return pool
        return None

    def apply_swap_event(self, pool_address: str, sqrt_price_x96: int, liquidity: int, tick: int,
                          block_number: int | None) -> None:
        pool = self.by_address.get(pool_address.lower())
        if pool is None:
            return  # пул вне нашей вселенной -- игнорируем, не гадаем
        pool.apply_swap(sqrt_price_x96, liquidity, tick, block_number)


def _rpc_call(method: str, params: list, rpc_url: str = RPC_URL_MAINNET, timeout: float = 10.0) -> dict:
    resp = requests.post(rpc_url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
                          timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC {method} вернул ошибку: {data['error']}")
    return data["result"]


def _rpc_call_with_provider_fallback(method: str, params: list, fallback_rpc_url: str = RPC_URL_MAINNET,
                                      timeout: float = 10.0) -> dict:
    """Владелец, 2026-09-12: "публичный RPC не для bootstrap... провайдерский
    RPC (Alchemy/Chainstack) с fallback на публичный." `RPC_URL_PROVIDER`
    (см. task5_bot_config.py) -- полный URL, из /etc/bot/env, задаётся
    владельцем. Если не задан -- ведёт себя ровно как раньше (публичный
    напрямую). Если задан, но провайдер сам упал (сеть/429/что угодно) --
    честный fallback на публичный, не тихий отказ и не падение всего вызова."""
    provider_url = os.environ.get(RPC_URL_PROVIDER_ENV_VAR)
    if provider_url:
        try:
            return _rpc_call(method, params, provider_url, timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"[bootstrap] провайдерский RPC упал ({exc}) -- fallback на публичный {fallback_rpc_url}",
                  file=sys.stderr)
    return _rpc_call(method, params, fallback_rpc_url, timeout)


def _post_batch_with_provider_fallback(batch_body: list, fallback_rpc_url: str = RPC_URL_MAINNET,
                                        timeout: float = 20.0) -> list:
    """Та же логика provider-with-fallback, что `_rpc_call_with_provider_
    fallback`, но для СЫРОГО batched JSON-RPC POST (не через `_rpc_call`,
    см. `populate_initial_prices` ниже -- один HTTP POST = много eth_call)."""
    provider_url = os.environ.get(RPC_URL_PROVIDER_ENV_VAR)
    if provider_url:
        try:
            resp = requests.post(provider_url, json=batch_body, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[bootstrap] провайдерский RPC (batch) упал ({exc}) -- fallback на публичный {fallback_rpc_url}",
                  file=sys.stderr)
    resp = requests.post(fallback_rpc_url, json=batch_body, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def bootstrap_registry_from_rpc(rpc_url: str = RPC_URL_MAINNET,
                                 lookback_blocks: int = POOL_DISCOVERY_LOOKBACK_BLOCKS,
                                 populate_prices: bool = True, batch_size: int = 25,
                                 lazy_pricing: bool = False) -> PoolRegistry:
    """Единственное место, где бот делает RPC-запросы в НЕ-горячем пути
    (только при старте). Реальный, а не выдуманный метод: PoolCreated --
    стандартное indexed-событие Uniswap V3 Factory, topic0 вычислен через
    keccak (тот же метод, что sc1_v2_recon.py/G1 в этом проекте).

    Владелец, 2026-09-13, п.2: `populate_prices=True` (по умолчанию) --
    сразу после скана PoolCreated читает slot0()/liquidity() КАЖДОГО
    найденного пула батчами (см. populate_initial_prices() ниже) --
    заполняет реальные начальные цены ДО горячего пути, честный пробел
    прошлых раундов (ни один пул раньше не имел цены до первого реально
    декодированного свопа, а декодер свопов сам по себе ненадёжен --
    см. task5_bot_feed_client.py) -- закрыт здесь, независимо от
    состояния декодера.

    Владелец, 2026-09-12 ('пока ключа [провайдера] нет'): `lazy_pricing=True`
    ПОЛНОСТЬЮ пропускает этот эager-батч-проход (0 дополнительных RPC-вызовов
    сверх одного eth_getLogs) -- цены пулов заполняются ЛЕНИВО, по факту
    первого реального касания в горячем пути (см. `task5_bot_run.py`:
    первое касание пула -- один точечный `refresh_pool_price()`, дальше --
    пересчёт из calldata без сети, `task5_bot_price_from_calldata.py`).
    "Это сокращает нагрузку [на bootstrap] на порядок" -- честно: nагрузка
    переносится на горячий путь по чуть-чуть, не исчезает совсем, но
    ВСЕГДА ограничена реально ТРОНУТЫМИ пулами за время работы, а не всей
    вселенной ~1150+ сразу одним взрывом запросов."""
    latest_hex = _rpc_call_with_provider_fallback("eth_blockNumber", [], rpc_url)
    latest = int(latest_hex, 16)
    from_block = max(0, latest - lookback_blocks)

    logs = _rpc_call_with_provider_fallback("eth_getLogs", [{
        "fromBlock": hex(from_block),
        "toBlock": hex(latest),
        "address": CANONICAL_V3_FACTORY,
        "topics": [POOL_CREATED_TOPIC0],
    }], rpc_url)

    registry = PoolRegistry()
    # Владелец, 2026-09-12: реальная находка при разборе двух прогонов бэктеста --
    # `current_weth_usd_price()` стабильно возвращал None, а `n_priced_via_pool`
    # был 0 для ВСЕХ 233 совпавших пулов -- т.е. `populate_initial_prices()` ниже
    # массово не успевает заполнить цены (RPC 429 при большом батч-потоке, см.
    # PROJECT_STATE.md). `TASK5_KNOWN_PROFITABLE_POOLS` (наши единственные
    # известные WETH/USDG-пулы, они же самые ценные для бота) регистрируются
    # здесь ПЕРВЫМИ (до PoolCreated-скана), чтобы список пулов для батчей ниже
    # начинался именно с них -- если рейт-лимит и обрежет хвост батчей, наши
    # приоритетные пулы уже будут в САМОМ ПЕРВОМ, а не последнем батче.
    _merge_known_profitable_pools(registry)
    for log in logs:
        topics = log["topics"]
        token0 = "0x" + topics[1][-40:]
        token1 = "0x" + topics[2][-40:]
        fee = int(topics[3], 16)
        data = log["data"][2:]  # tickSpacing (int24, паддинг до 32 байт) + address pool (32 байта)
        pool_address = "0x" + data[-40:]
        if pool_address.lower() in registry.by_address:
            continue  # уже зарегистрирован выше (известный профитный пул) -- не перезаписываем правильный fee
        registry.register(V3PoolState(address=pool_address, token0=token0, token1=token1, fee=fee))

    if lazy_pricing:
        print(f"[bootstrap] lazy_pricing=True -- populate_initial_prices ПРОПУЩЕН для всех "
              f"{len(registry.by_address)} пулов, цены заполнятся по факту касания в горячем пути "
              f"(0 доп. RPC-вызовов сейчас)", file=sys.stderr)
    elif populate_prices:
        # block_number=latest (не None) -- ВАЖНО: check_pair_for_divergence меряет
        # divergence_age_blocks от last_update_block; None трактуется как "age=0",
        # что при первом же реальном сообщении сделало бы ЛЮБУЮ пару "слишком свежей"
        # (age=0 < MIN_DIVERGENCE_AGE_BLOCKS=1) и заблокировало детекцию НАВСЕГДА --
        # реальный баг, найденный при подготовке к живому 10-минутному прогону
        # (владелец, 2026-09-13, п.3), исправлен здесь, не оставлен молча.
        price_stats = populate_initial_prices(registry, rpc_url=rpc_url, batch_size=batch_size, block_number=latest)
        print(f"[bootstrap] populate_initial_prices: {price_stats['n_ok']}/{price_stats['n_pools']} успешно, "
              f"{price_stats['n_error']} ошибок, примеры: {price_stats['errors_sample']}", file=sys.stderr)
    return registry


# slot0()/liquidity() -- стандартные view-функции Uniswap V3 Pool, селекторы
# вычислены тем же способом (keccak-256, первые 4 байта), что POOL_CREATED_TOPIC0
# выше -- НЕ найдены подстрочным совпадением, реально посчитаны.
_SLOT0_SELECTOR = "0x" + keccak_topic("slot0()")[2:10]
_LIQUIDITY_SELECTOR = "0x" + keccak_topic("liquidity()")[2:10]


def _decode_signed_word(hex_word: str) -> int:
    """ABI всегда знак-расширяет signed-типы (int24 tick и т.п.) до полных
    32 байт -- стандартное двухкомплементное декодирование полного слова,
    не специфичное для конкретной битности исходного типа."""
    raw = int(hex_word, 16)
    return raw - (1 << 256) if raw >= (1 << 255) else raw


def populate_initial_prices(registry: PoolRegistry, rpc_url: str = RPC_URL_MAINNET,
                             batch_size: int = 25, timeout: float = 20.0,
                             block_number: int | None = None) -> dict:
    """Владелец, 2026-09-13, п.2: "slot0 при bootstrap -- для ВСЕХ пулов,
    батчами, чтобы не упереться в лимит." Один-единственный, разовый вызов
    ДО горячего пути (тот же принцип, что PoolCreated-скан выше) -- читает
    `slot0()` (sqrtPriceX96/tick) и `liquidity()` каждого пула через
    JSON-RPC batch-запросы (несколько `eth_call` в одном HTTP POST -- нода
    отвечает МАССИВОМ, не по одному round-trip'у на пул). `batch_size` --
    число ПУЛОВ на один HTTP POST (2 запроса/пул -- slot0+liquidity, то
    есть `2*batch_size` элементов в массиве) -- 25 пулов/50 запросов --
    консервативный размер, ниже типичного лимита провайдеров (100-1000).

    Честно: пулы, для которых `eth_call` вернул ошибку (несуществующий
    контракт слот, нестандартный ABI и т.п.) -- остаются с
    `sqrt_price_x96=None` (как и было до вызова), НЕ подставляется 0/угаданное
    значение -- такие пулы просто не участвуют в детекции расхождений
    (`_normalized_price` вернёт None), это уже штатно обрабатывается
    `check_pair_for_divergence`."""
    pools = list(registry.by_address.values())
    stats = {"n_pools": len(pools), "n_ok": 0, "n_error": 0, "errors_sample": []}

    for i in range(0, len(pools), batch_size):
        if i > 0:
            # Владелец, 2026-09-12: реальная находка -- два независимых прогона
            # bootstrap подряд оба вернули `sqrt_price_x96=None` практически для
            # ВСЕХ пулов (0/233 совпадений оценены), при том что eth_getLogs (один
            # запрос) отрабатывал нормально -- похоже на 429 внутри самого этого
            # батч-потока (~47 POST подряд без паузы), не только между отдельными
            # запусками. Небольшая пауза между батчами -- честная попытка снизить
            # частоту запросов, не гарантия (RPC не публикует точный лимит).
            time.sleep(0.25)
        chunk = pools[i:i + batch_size]
        batch_body = []
        for j, pool in enumerate(chunk):
            batch_body.append({"jsonrpc": "2.0", "id": f"{j}-slot0",
                                "method": "eth_call",
                                "params": [{"to": pool.address, "data": _SLOT0_SELECTOR}, "latest"]})
            batch_body.append({"jsonrpc": "2.0", "id": f"{j}-liquidity",
                                "method": "eth_call",
                                "params": [{"to": pool.address, "data": _LIQUIDITY_SELECTOR}, "latest"]})
        try:
            raw_results = _post_batch_with_provider_fallback(batch_body, fallback_rpc_url=rpc_url, timeout=timeout)
            by_id = {r.get("id"): r for r in raw_results}
        except Exception as exc:
            stats["n_error"] += len(chunk)
            if len(stats["errors_sample"]) < 3:
                stats["errors_sample"].append(f"batch HTTP error: {exc}")
            continue

        for j, pool in enumerate(chunk):
            slot0_r = by_id.get(f"{j}-slot0")
            liq_r = by_id.get(f"{j}-liquidity")
            if not slot0_r or "error" in slot0_r or not slot0_r.get("result") or slot0_r["result"] == "0x":
                stats["n_error"] += 1
                if len(stats["errors_sample"]) < 3:
                    stats["errors_sample"].append(f"{pool.address}: slot0 -> {slot0_r}")
                continue
            try:
                slot0_hex = slot0_r["result"][2:]
                sqrt_price_x96 = int(slot0_hex[0:64], 16)
                tick = _decode_signed_word(slot0_hex[64:128])
                liquidity = int(liq_r["result"], 16) if liq_r and liq_r.get("result") not in (None, "0x") else 0
            except Exception as exc:
                stats["n_error"] += 1
                if len(stats["errors_sample"]) < 3:
                    stats["errors_sample"].append(f"{pool.address}: decode error {exc}")
                continue
            pool.apply_swap(sqrt_price_x96=sqrt_price_x96, liquidity=liquidity, tick=tick, block_number=block_number)
            stats["n_ok"] += 1

    return stats


def refresh_pool_price(pool: V3PoolState, rpc_url: str = RPC_URL_MAINNET, record_block_number: int | None = None,
                        timeout: float = 5.0) -> bool:
    """Владелец, 2026-09-13, п.3 (повторный dry-run, реальные детекции):
    ЧЕСТНЫЙ, ОСОЗНАННЫЙ КОМПРОМИСС, не тихая замена архитектуры. Фид
    секвенсера отдаёт RAW подписанные транзакции ДО исполнения (сырое
    намерение), НЕ результат исполнения -- у него физически нет
    постсвоповых sqrtPriceX96/liquidity (это подтверждено реальной
    разведкой формы сообщений, см. `decode_l2_message`). Два пути дать
    боту реальную (не устаревшую) цену: (а) полный симулятор математики
    Uniswap V3 (tick-crossing, liquidityNet по тикам) -- отдельная,
    существенная задача, не сделана в этой сессии; (б) точечный RPC-запрос
    `slot0()`/`liquidity()` ИМЕННО для пула, который только что реально
    тронут (не всех пулов, не по расписанию) -- этот вариант.

    Это НАРУШАЕТ первоначальное требование "ноль RPC в горячем пути" --
    честно, не скрыто: вызывается СИНХРОННО из `on_feed_message()`,
    блокирует event loop на время запроса (десятки мс). Годится для
    ДИАГНОСТИКИ/dry-run (только логирование "здесь бы вошёл", ничего не
    отправляется) -- для реальной низколатентной торговли этот компромисс
    нужно будет заменить симулятором (а) или принять сознательно, не по
    умолчанию."""
    # ВСЕГДА "latest" для самого eth_call -- нужна РЕАЛЬНАЯ текущая цена,
    # `record_block_number` -- ОТДЕЛЬНО, только для bookkeeping в
    # `pool.last_update_block` (divergence_age_blocks в детекторе), не для
    # запроса истории (запрос по номеру только что вышедшего из секвенсера
    # блока рискует не найтись на индексирующей RPC-ноде из-за небольшого
    # лага индексации -- честно, не рискуем этим).
    try:
        slot0_hex = _rpc_call_with_provider_fallback(
            "eth_call", [{"to": pool.address, "data": _SLOT0_SELECTOR}, "latest"], rpc_url, timeout)
        liq_hex = _rpc_call_with_provider_fallback(
            "eth_call", [{"to": pool.address, "data": _LIQUIDITY_SELECTOR}, "latest"], rpc_url, timeout)
        if not slot0_hex or slot0_hex == "0x":
            return False
        body = slot0_hex[2:]
        sqrt_price_x96 = int(body[0:64], 16)
        tick = _decode_signed_word(body[64:128])
        liquidity = int(liq_hex, 16) if liq_hex and liq_hex != "0x" else 0
    except Exception:
        return False  # честно: не удалось обновить -- пул остаётся со старой ценой, не гадаем
    pool.apply_swap(sqrt_price_x96=sqrt_price_x96, liquidity=liquidity, tick=tick, block_number=record_block_number)
    return True


def _merge_known_profitable_pools(registry: PoolRegistry) -> None:
    """Владелец, 2026-09-12: "RPC-скан оставить как дополнение, не
    замену" -- список TASK5_KNOWN_PROFITABLE_POOLS (из
    `analysis/task5_pool_map.py`, реальные данные Dune) добавляется
    ПОВЕРХ RPC-скана, на случай если lookback RPC-скана не покрыл
    старый пул (пул мог быть создан раньше POOL_DISCOVERY_LOOKBACK_BLOCKS)."""
    from task5_bot_config import TASK5_KNOWN_PROFITABLE_POOLS

    for entry in TASK5_KNOWN_PROFITABLE_POOLS:
        addr = entry.get("pool_key") or entry.get("address")
        if not addr or addr.lower() in registry.by_address:
            continue
        token0, token1 = entry.get("token0"), entry.get("token1")
        if not token0 or not token1:
            continue
        registry.register(V3PoolState(address=addr, token0=token0, token1=token1, fee=entry.get("fee", 0)))


def save_registry_cache(registry: PoolRegistry, path: str) -> None:
    """Владелец, 2026-09-12: "429 -- тот же урок, что с фидом: bootstrap один
    раз, кешировать реестр на диск, не перезапрашивать RPC каждый прогон."
    ТОЛЬКО для повторных офлайн-анализов (`task5_bot_router_backtest.py`) --
    живой бот (`task5_bot_run.py`) этим кешем НЕ пользуется, всегда бутстрапит
    заново (кеш устаревает, живой торговле нужны свежие цены)."""
    import json as _json
    from pathlib import Path as _Path

    data = {
        "generated_at_wall": time.time(),
        "pools": [
            {
                "address": p.address, "token0": p.token0, "token1": p.token1, "fee": p.fee,
                "sqrt_price_x96": p.sqrt_price_x96, "tick": p.tick, "liquidity": p.liquidity,
                "last_update_block": p.last_update_block,
            }
            for p in registry.by_address.values()
        ],
    }
    _Path(path).parent.mkdir(parents=True, exist_ok=True)
    _Path(path).write_text(_json.dumps(data, indent=2))


def load_registry_cache(path: str) -> PoolRegistry:
    """Обратная операция к `save_registry_cache` -- ЧИСТО ОФЛАЙН, ни одного
    сетевого вызова. Честно падает, если файла нет (вызывающий код должен
    сам решить -- бутстрапить через RPC или сообщить об ошибке, не гадаем
    молча пустым реестром)."""
    import json as _json
    from pathlib import Path as _Path

    data = _json.loads(_Path(path).read_text())
    registry = PoolRegistry()
    for entry in data["pools"]:
        pool = V3PoolState(address=entry["address"], token0=entry["token0"], token1=entry["token1"],
                            fee=entry["fee"])
        pool.sqrt_price_x96 = entry.get("sqrt_price_x96")
        pool.tick = entry.get("tick")
        pool.liquidity = entry.get("liquidity")
        pool.last_update_block = entry.get("last_update_block")
        registry.register(pool)
    return registry
