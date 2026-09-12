#!/usr/bin/env python3
"""Задача 5, живой бот -- предрасчёт маршрутов между двумя пулами одной
пары ЗАРАНЕЕ. Владелец, 2026-09-11/12: "пары и маршруты предрассчитаны";
2026-09-13, п.4 (третий VPS, Огайо): явный отдельный модуль для этого,
с поправкой на цель "блок 0" (см. `TARGET_BLOCK_DISTANCE` в
task5_bot_config.py).

Строит СТАТИЧЕСКУЮ часть параметров `executeCycle()`
(`contracts/ClosedCycleExecutorV3.sol::CycleParams`) для каждой пары
пулов, известной `PoolRegistry`, -- адреса, порядок token0/token1,
4-байтовый селектор функции -- ОДИН РАЗ при регистрации пула/пары, не
в горячем пути. `fill_cycle_params()` затем заполняет ЦЕНОЗАВИСИМЫЕ поля
(`amountSpecifiedA`/`minProfit`/`sqrtPriceLimit*`) из данных, уже лежащих
в `PoolRegistry` (обновляется из фида) -- ни одного сетевого вызова,
тот же принцип горячего пути, что `task5_bot_detector.py`.

ЧЕСТНО, важно не путать: этот модуль строит ПАРАМЕТРЫ будущего вызова
`executeCycle()` -- НЕ ABI-кодирует финальный calldata транзакции, НЕ
подписывает и НЕ отправляет ничего. Само ABI-кодирование calldata,
подпись и `eth_sendRawTransaction` -- предмет `task5_bot_executor.py::
_build_sign_send` (по-прежнему `NotImplementedError`, спецификация --
`docs/TASK5_BOT_EXECUTOR_SPEC.md`).

ЧЕСТНАЯ ОГОВОРКА про `fill_cycle_params()`: направление/размер здесь --
первое приближение (та же осторожность, что `expected_capture_usd` в
`task5_bot_detector.py` -- либо эвристика на liquidity, помечена как
таковая). Контракт САМ защищает капитал независимо от точности этой
оценки: `executeCycle()` откатывается целиком (только газ неудачной
попытки), если баланс `exitToken` не вырос минимум на `minProfit` --
ошибка в предрасчёте здесь стоит газа одной попытки, не инвентаря."""
from __future__ import annotations

from dataclasses import dataclass

from Crypto.Hash import keccak

from task5_bot_pool_state import PoolRegistry, V3PoolState

# executeCycle((address,address,bool,int256,address,uint256,uint160,uint160))
# -- сигнатура из contracts/ClosedCycleExecutorV3.sol::CycleParams, тот же
# способ вычисления селектора (keccak-256, первые 4 байта), что topic0
# событий в task5_bot_pool_state.py/sc1_v2_recon.py.
_EXECUTE_CYCLE_SIGNATURE = (
    "executeCycle((address,address,bool,int256,address,uint256,uint160,uint160))"
)


def _selector(signature: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(signature.encode())
    return "0x" + h.hexdigest()[:8]


EXECUTE_CYCLE_SELECTOR = _selector(_EXECUTE_CYCLE_SIGNATURE)

# Слиппедж-запас на sqrtPriceLimit -- владелец пока не задавал точное
# число; 50 бпс -- консервативный старт (та же порядок величины, что
# ASSUMED_REVERT_RATE-эра калибровки в детекторе), ПЕРЕСЧИТЫВАЕТСЯ по
# факту первой недели dry-run, как и остальные заглушки этого модуля.
DEFAULT_SLIPPAGE_BPS = 50

# Владелец, 2026-09-12 (код-ревью контракта): TickMath-константы Uniswap V3
# (contracts/ClosedCycleExecutorV3.sol не переопределяет проверку "SPL" --
# она в самом пуле). ВАЖНО: `sqrtPriceLimitX96 = 0` -- НЕ "без лимита" --
# пул V3 требует zeroForOne ? (MIN_SQRT_RATIO < limit < currentPrice)
# : (currentPrice < limit < MAX_SQRT_RATIO); 0 не проходит НИ ОДНУ из
# половин этой проверки (0 < MIN_SQRT_RATIO всегда) -- своп с limit=0
# гарантированно откатится с "SPL" на самом первом pool.swap(), то есть
# каждая попытка executeCycle() билась бы о это ДО того, как вообще
# дошла бы до реальной логики цикла. Прежняя заглушка "0 = без лимита"
# была неверна для реального протокола -- заменено на реальный расчёт.
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342


@dataclass
class RouteSkeleton:
    """Статическая, ценонезависимая часть маршрута между двумя пулами
    одной пары -- строится один раз, переиспользуется на каждой
    детекции этой пары."""

    pool_a_address: str
    pool_b_address: str
    pool_a_token0: str
    pool_a_token1: str
    pool_b_token0: str
    pool_b_token1: str
    exit_token: str
    execute_cycle_selector: str = EXECUTE_CYCLE_SELECTOR


class RoutePrecomputeTable:
    """address(cheap_pool), address(expensive_pool) -> RouteSkeleton,
    построено заранее для ОБОИХ порядков (любой из двух пулов пары может
    оказаться "дешёвым" в момент детекции -- направление меняется с
    ценой, набор пулов -- нет)."""

    def __init__(self) -> None:
        self._by_pool_pair: dict[tuple[str, str], RouteSkeleton] = {}

    @staticmethod
    def _key(pool_a: str, pool_b: str) -> tuple[str, str]:
        return (pool_a.lower(), pool_b.lower())

    def build_for_registry(self, registry: PoolRegistry, exit_token: str) -> int:
        """Строит скелеты для ВСЕХ пар с >=2 пулами, известных реестру,
        в ОБОИХ направлениях poolA/poolB -- вызывается один раз при
        bootstrap (после bootstrap_registry_from_rpc), не в горячем
        пути. Возвращает число построенных скелетов (для лога/диагностики,
        не для решений)."""
        seen_pairs: set[tuple[str, str]] = set()
        n_built = 0
        for pool in registry.by_address.values():
            pair_key = registry._pair_key(pool.token0, pool.token1)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            pools = registry.pools_for_pair(pool.token0, pool.token1)
            for i, pa in enumerate(pools):
                for pb in pools:
                    if pa.address.lower() == pb.address.lower():
                        continue
                    self._by_pool_pair[self._key(pa.address, pb.address)] = RouteSkeleton(
                        pool_a_address=pa.address,
                        pool_b_address=pb.address,
                        pool_a_token0=pa.token0,
                        pool_a_token1=pa.token1,
                        pool_b_token0=pb.token0,
                        pool_b_token1=pb.token1,
                        exit_token=exit_token,
                    )
                    n_built += 1
        return n_built

    def get(self, pool_a: str, pool_b: str) -> RouteSkeleton | None:
        return self._by_pool_pair.get(self._key(pool_a, pool_b))


def _sqrt_price_limit(sqrt_price_x96: int, zero_for_one: bool, slippage_bps: int) -> int:
    """Реальный (не заглушка) sqrtPriceLimitX96 для `IUniswapV3Pool.swap()`:
    zeroForOne=true двигает цену ВНИЗ (лимит должен быть СТРОГО меньше
    текущей sqrtPriceX96 и строго больше MIN_SQRT_RATIO); zeroForOne=false
    двигает цену ВВЕРХ (лимит строго больше текущей и строго меньше
    MAX_SQRT_RATIO) -- см. `MIN_SQRT_RATIO`/`MAX_SQRT_RATIO` выше. Считаем
    лимит как sqrtPrice * sqrt(1 ± slippage), затем зажимаем внутрь
    допустимого полуинтервала -- если бы отдали 0 или "сырое" значение без
    зажима, своп в pool A/B откатился бы на входной проверке пула ("SPL"),
    ДО того, как дошёл бы до логики цикла в контракте."""
    frac = slippage_bps / 10_000.0
    if zero_for_one:
        limit = int(sqrt_price_x96 * ((1.0 - frac) ** 0.5))
        limit = min(limit, sqrt_price_x96 - 1)
        limit = max(limit, MIN_SQRT_RATIO + 1)
    else:
        limit = int(sqrt_price_x96 * ((1.0 + frac) ** 0.5))
        limit = max(limit, sqrt_price_x96 + 1)
        limit = min(limit, MAX_SQRT_RATIO - 1)
    return limit


def fill_cycle_params(
    skeleton: RouteSkeleton,
    cheap_pool: V3PoolState,
    expensive_pool: V3PoolState,
    notional_wei: int,
    expected_capture_after_gas_and_reverts_usd: float,
    price_usd_per_exit_token: float,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> dict:
    """Заполняет ЦЕНОЗАВИСИМЫЕ поля `CycleParams` из данных, уже лежащих
    в памяти (skeleton + текущее состояние обоих пулов) -- ни одного
    сетевого вызова. `poolA` = дешёвый пул (занимаем токен здесь,
    покупая его дёшево), `poolB` = дорогой пул (продаём там же дороже,
    закрывая долг перед poolA) -- это и есть сам арбитраж: купить
    дёшево/продать дорого в одном атомарном цикле.

    Возвращает dict-представление `CycleParams` (не ABI-кодированный
    calldata -- это задача `task5_bot_executor.py`, здесь только
    значения полей структуры)."""
    zero_for_one_a = cheap_pool.token0.lower() == skeleton.pool_a_token0.lower() and (
        skeleton.exit_token.lower() != cheap_pool.token0.lower()
    )
    # ЧЕСТНО: точный знак zeroForOneA/amountSpecifiedA зависит от того,
    # какой токен пары -- exit_token, и от текущего tick обоих пулов --
    # это первое приближение (см. докстринг модуля), не откалибровано
    # против реального контракта на testnet. Контракт откатит цикл целиком
    # при ошибке направления (amount0/amount1 в V3 swap() имеют строгий
    # знак, неверная комбинация либо ничего не даст занять, либо не
    # закроется в pool B) -- потеря ограничена газом попытки, не инвентарём.
    min_profit_usd = expected_capture_after_gas_and_reverts_usd * (1 - slippage_bps / 10_000)
    min_profit_wei = int(max(min_profit_usd, 0.0) / max(price_usd_per_exit_token, 1e-9) * 1e18) \
        if skeleton.exit_token.lower() != "usdg" else int(max(min_profit_usd, 0.0) * 1e6)

    if cheap_pool.sqrt_price_x96 is None or expensive_pool.sqrt_price_x96 is None:
        raise ValueError(
            "fill_cycle_params: sqrt_price_x96 не заполнен -- вызывающий код (executor) "
            "должен вызывать это только после успешной check_pair_for_divergence "
            "(которая сама требует непустой sqrt_price_x96 для обеих сторон)"
        )
    zero_for_one_b = not zero_for_one_a
    # poolB = expensive_pool (см. докстринг выше: poolA -- дешёвый, где занимаем,
    # poolB -- дорогой, где закрываем встречным свопом).
    sqrt_price_limit_a = _sqrt_price_limit(cheap_pool.sqrt_price_x96, zero_for_one_a, slippage_bps)
    sqrt_price_limit_b = _sqrt_price_limit(expensive_pool.sqrt_price_x96, zero_for_one_b, slippage_bps)

    return {
        "poolA": cheap_pool.address,
        "poolB": expensive_pool.address,
        "zeroForOneA": zero_for_one_a,
        "amountSpecifiedA": -int(notional_wei),  # отрицательное = exact output заимствования (см. .sol)
        "exitToken": skeleton.exit_token,
        "minProfit": min_profit_wei,
        "sqrtPriceLimitA": sqrt_price_limit_a,
        "sqrtPriceLimitB": sqrt_price_limit_b,
        "execute_cycle_selector": skeleton.execute_cycle_selector,
    }
