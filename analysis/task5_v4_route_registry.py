#!/usr/bin/env python3
"""Задача 5, живой пилот -- реестр живых маршрутов (МЕДЛЕННЫЙ фон,
секунды-минуты). Владелец, 2026-09-12:

  "Медленная (фон, секунды-минуты): реестр живых маршрутов. Наполнить
  из пулов уже разобранных арбитражных сделок. Добавлять новые пулы из
  событий Initialize/ModifyLiquidity в фиде, связывая в циклы из 2-3
  обменов с началом и концом в USDG или ETH/WETH. Раз в минуту проверять
  котировку каждого маршрута; не котируется -- временно исключить,
  вернуть при изменении состояния его пулов."

Три части:
  1. Сид -- ДВА маршрута, уже реально проверенных в этой сессии
     (Этап 2, fork-симуляция): USDG<->MOSIAI (2 пула той же пары) и
     ETH->MOSIAI->USDG->ETH (3 пула, средний плечо с хуком). Не
     выдуманы -- те же PoolKey, что в task5_v4_fork_simulation.py /
     task5_v4_fork_simulation_hook_route.py.
  2. Обнаружение новых пулов -- реальные Initialize-события PoolManager
     (topic0 сверен и переиспользован из task5_v4_hook_route_audit.py
     этой же сессии), построение циклов 2-3 обменов начинающихся и
     заканчивающихся в USDG/NATIVE.
  3. Проверка живучести -- раз в минуту котируем КАЖДЫЙ маршрут
     небольшим каноническим размером через реальный V4Quoter; не
     котируется (revert -- NotEnoughLiquidity и т.п., см.
     data/task5_v4_diag_current_liquidity_result.json, реальный
     прецедент этой же сессии, где ИМЕННО эта проверка нашла пересохший
     пул) -- временно исключаем из живых, не удаляем из реестра
     (пере-проверяем каждую минуту, "вернуть при изменении состояния")."""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _chunked_get_logs, _rpc_call, topic0  # noqa: E402
from task5_v4_pool_math import PoolKey, pool_id  # noqa: E402
from task5_v4_quote_replay import quote_exact_input_single  # noqa: E402

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
NATIVE = "0x0000000000000000000000000000000000000000"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
MOSIAI = "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a"
HOOK_ETH_MOSIAI = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"

# Токены, которыми маршрут ОБЯЗАН начинаться и заканчиваться (владелец:
# "начало и конец в USDG или ETH/WETH"). NATIVE (0x0) -- нативный ETH в
# терминах V4-currency; на этой цепи WETH-обёртки отдельным ERC20 в
# известных маршрутах этой сессии не встречалось -- если понадобится,
# добавить сюда её реальный адрес после подтверждения.
START_TOKENS = {USDG, NATIVE}

INITIALIZE_SIG = "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
INITIALIZE_TOPIC0 = topic0(INITIALIZE_SIG)

# Канонический тестовый размер для проверки живучести -- НЕ размер
# сделки, только "котируется/не котируется" (см. докстринг). Разные
# единицы по стартовому токену.
CANONICAL_TEST_SIZE = {
    USDG: 1_000_000,       # 1 USDG (6 decimals)
    NATIVE: 10**16,        # 0.01 ETH
}


@dataclass(frozen=True)
class RouteLeg:
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str
    zero_for_one: bool  # направление ИМЕННО в этом плече этого маршрута

    @property
    def pool_key(self) -> PoolKey:
        return PoolKey(self.currency0, self.currency1, self.fee, self.tick_spacing, self.hooks)

    @property
    def pool_id_hex(self) -> str:
        return pool_id(self.pool_key)

    @property
    def input_currency(self) -> str:
        return self.currency0 if self.zero_for_one else self.currency1

    @property
    def output_currency(self) -> str:
        return self.currency1 if self.zero_for_one else self.currency0


@dataclass
class RouteCycle:
    route_id: str
    legs: tuple[RouteLeg, ...]
    exit_token: str
    label: str
    source: str  # "seed" | "discovered"

    def pool_ids(self) -> list[str]:
        return [leg.pool_id_hex for leg in self.legs]


def _route_id_for_legs(legs: list[RouteLeg]) -> str:
    return "route_" + "_".join(f"{leg.pool_id_hex[2:10]}{'z1' if leg.zero_for_one else 'z0'}" for leg in legs)


def seed_routes() -> list[RouteCycle]:
    """Два маршрута, реально проверенных в Этапе 2 этой сессии (fork-
    симуляция, реальная прибыль воспроизведена побайтово) -- не
    выдуманы, PoolKey идентичны task5_v4_fork_simulation*.py."""
    usdg_mosiai_legs = [
        RouteLeg(USDG, MOSIAI, 70000, 4, NATIVE, True),
        RouteLeg(USDG, MOSIAI, 70000, 3, NATIVE, False),
    ]
    eth_hook_legs = [
        RouteLeg(NATIVE, MOSIAI, 0, 200, HOOK_ETH_MOSIAI, True),
        RouteLeg(USDG, MOSIAI, 75000, 2, NATIVE, False),
        RouteLeg(NATIVE, USDG, 100, 1, NATIVE, False),
    ]
    return [
        RouteCycle(_route_id_for_legs(usdg_mosiai_legs), tuple(usdg_mosiai_legs), USDG,
                   "USDG<->MOSIAI (2 пула той же пары)", "seed"),
        RouteCycle(_route_id_for_legs(eth_hook_legs), tuple(eth_hook_legs), NATIVE,
                   "ETH->MOSIAI(хук)->USDG->ETH", "seed"),
    ]


def discover_pools_from_initialize_events(from_block: int, to_block: int) -> dict[str, PoolKey]:
    """Реальные Initialize-события PoolManager -- currency0/currency1
    индексированы (topics[2]/topics[3]), fee/tickSpacing/hooks -- в
    data (см. task5_v4_hook_route_audit.py этой же сессии, тот же
    разбор). Возвращает {pool_id_hex: PoolKey}."""
    pools: dict[str, PoolKey] = {}
    logs = _chunked_get_logs(
        from_block, to_block, topics=[INITIALIZE_TOPIC0], address=POOL_MANAGER,
        chunk_size=max(1, to_block - from_block + 1),
    )
    for log in logs:
        pool_id_hex = log["topics"][1]
        currency0 = "0x" + log["topics"][2][-40:]
        currency1 = "0x" + log["topics"][3][-40:]
        data = log["data"][2:]
        words = [data[i:i + 64] for i in range(0, len(data), 64)]
        fee = int(words[0], 16)
        tick_spacing = int(words[1], 16)
        if tick_spacing >= (1 << 255):
            tick_spacing -= (1 << 256)
        hooks = "0x" + words[2][-40:]
        pools[pool_id_hex] = PoolKey(currency0, currency1, fee, tick_spacing, hooks)
    return pools


def build_cycles_from_pools(pools: dict[str, PoolKey]) -> list[RouteCycle]:
    """Строит циклы из 2 (та же пара, 2 разных пула) и 3 обменов
    (треугольник через разные пары), начинающиеся и заканчивающиеся в
    USDG/NATIVE (см. START_TOKENS). Граф маленький (десятки пулов) --
    прямой перебор, не нужен сложный поиск путей."""
    # adjacency[token] = список (other_token, pool_key) -- пул связывает currency0<->currency1
    adjacency: dict[str, list[tuple[str, PoolKey]]] = {}
    for key in pools.values():
        adjacency.setdefault(key.currency0, []).append((key.currency1, key))
        adjacency.setdefault(key.currency1, []).append((key.currency0, key))

    cycles: list[RouteCycle] = []
    seen_route_ids: set[str] = set()

    for start in START_TOKENS:
        for m1, key1 in adjacency.get(start, []):
            zero_for_one_1 = key1.currency0.lower() == start.lower()
            leg1 = RouteLeg(key1.currency0, key1.currency1, key1.fee, key1.tick_spacing, key1.hooks, zero_for_one_1)

            # --- 2-leg: та же пара (start, m1), другой пул ---
            for m1b, key2 in adjacency.get(m1, []):
                if m1b.lower() != start.lower():
                    continue
                if pool_id(key2) == pool_id(key1):
                    continue  # тот же пул -- не цикл, а ничего
                zero_for_one_2 = key2.currency0.lower() == m1.lower()
                leg2 = RouteLeg(key2.currency0, key2.currency1, key2.fee, key2.tick_spacing, key2.hooks, zero_for_one_2)
                legs = [leg1, leg2]
                rid = _route_id_for_legs(legs)
                if rid not in seen_route_ids:
                    seen_route_ids.add(rid)
                    cycles.append(RouteCycle(rid, tuple(legs), start,
                                              f"{start[:8]}<->{m1[:8]} (2 пула)", "discovered"))

            # --- 3-leg: треугольник start -> m1 -> m2 -> start ---
            for m2, key2 in adjacency.get(m1, []):
                if m2.lower() in (start.lower(), m1.lower()):
                    continue
                zero_for_one_2 = key2.currency0.lower() == m1.lower()
                leg2 = RouteLeg(key2.currency0, key2.currency1, key2.fee, key2.tick_spacing, key2.hooks, zero_for_one_2)
                for m2b, key3 in adjacency.get(m2, []):
                    if m2b.lower() != start.lower():
                        continue
                    ids = {pool_id(key1), pool_id(key2), pool_id(key3)}
                    if len(ids) != 3:
                        continue  # повторяющийся пул в треугольнике -- вырожденный случай
                    zero_for_one_3 = key3.currency0.lower() == m2.lower()
                    leg3 = RouteLeg(key3.currency0, key3.currency1, key3.fee, key3.tick_spacing, key3.hooks, zero_for_one_3)
                    legs = [leg1, leg2, leg3]
                    rid = _route_id_for_legs(legs)
                    if rid not in seen_route_ids:
                        seen_route_ids.add(rid)
                        cycles.append(RouteCycle(rid, tuple(legs), start,
                                                  f"{start[:8]}->{m1[:8]}->{m2[:8]}->{start[:8]}", "discovered"))
    return cycles


def check_route_liveness(route: RouteCycle, block_number: int) -> dict:
    """Котируем маршрут КАНОНИЧЕСКИМ (не боевым) размером через реальный
    V4Quoter -- живой, если ВСЕ плечи прошли без revert (не про
    прибыльность, только про исполнимость -- см. докстринг модуля)."""
    start_token = route.legs[0].input_currency
    amount = CANONICAL_TEST_SIZE.get(start_token.lower(), CANONICAL_TEST_SIZE[USDG])
    cur = amount
    try:
        for leg in route.legs:
            cur = quote_exact_input_single(leg.pool_key, leg.zero_for_one, cur, block_number)
        return {"live": True, "test_amount_in": amount, "test_amount_out": cur, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"live": False, "test_amount_in": amount, "test_amount_out": None, "error": str(exc)[:300]}


class RouteRegistry:
    """Держит маршруты + их статус живучести + обратный индекс
    pool_id -> маршруты (для горячего пути -- какие маршруты
    пересчитывать при свопе в данном пуле)."""

    def __init__(self) -> None:
        self.routes: dict[str, RouteCycle] = {}
        self.liveness: dict[str, dict] = {}
        self.pool_to_routes: dict[str, set[str]] = {}

    def add_route(self, route: RouteCycle) -> None:
        self.routes[route.route_id] = route
        for pid in route.pool_ids():
            self.pool_to_routes.setdefault(pid, set()).add(route.route_id)

    def add_routes(self, routes: list[RouteCycle]) -> None:
        for r in routes:
            self.add_route(r)

    def refresh_liveness_all(self, block_number: int) -> dict[str, dict]:
        results = {}
        for route_id, route in self.routes.items():
            res = check_route_liveness(route, block_number)
            res["checked_at_block"] = block_number
            res["checked_at_wall"] = time.time()
            self.liveness[route_id] = res
            results[route_id] = res
        return results

    def live_routes(self) -> list[RouteCycle]:
        return [self.routes[rid] for rid, st in self.liveness.items() if st.get("live")]

    def routes_touched_by_pool(self, pool_id_hex: str) -> list[RouteCycle]:
        return [self.routes[rid] for rid in self.pool_to_routes.get(pool_id_hex, set())]

    def to_summary(self) -> dict:
        return {
            "n_routes": len(self.routes),
            "n_live": sum(1 for st in self.liveness.values() if st.get("live")),
            "routes": [
                {"route_id": rid, "label": r.label, "source": r.source, "n_legs": len(r.legs),
                 "pool_ids": r.pool_ids(), "exit_token": r.exit_token,
                 "liveness": self.liveness.get(rid)}
                for rid, r in self.routes.items()
            ],
        }


def main() -> None:
    """Самопроверка/диагностика: сид + реальное обнаружение пулов из
    Initialize-событий (0..latest -- тот же паттерн, что уже реально
    работал в Этапе 2 для получения PoolKey хук-маршрута) + построение
    циклов + живучесть ВСЕХ маршрутов на latest блоке. Реальный RPC
    (eth_getLogs + Quoter), read-only, ничего не отправляет.

    Реальный повод сделать это ЗДЕСЬ, не только сид: оба сид-маршрута
    на момент написания честно оказались НЕ живы (пересохшая
    ликвидность, см. data/task5_v4_diag_current_liquidity_result.json)
    -- обнаружение новых пулов может найти РЕАЛЬНО исполнимые сейчас
    маршруты, а не полагаться только на исторически проверенные, но
    сейчас мёртвые."""
    latest = int(_rpc_call("eth_blockNumber", []), 16)
    registry = RouteRegistry()
    registry.add_routes(seed_routes())
    print(f"[route_registry] сид: {len(registry.routes)} маршрутов")

    print(f"[route_registry] сканирую Initialize-события PoolManager (0..{latest})...")
    try:
        pools = discover_pools_from_initialize_events(0, latest)
        print(f"[route_registry] найдено пулов: {len(pools)}")
        discovered_cycles = build_cycles_from_pools(pools)
        print(f"[route_registry] построено кандидатных циклов (2-3 обмена, начало/конец USDG/NATIVE): "
              f"{len(discovered_cycles)}")
        registry.add_routes(discovered_cycles)
    except Exception as exc:  # noqa: BLE001
        print(f"[route_registry] обнаружение пулов упало (честно, не молчим): {exc}", file=sys.stderr)

    print(f"[route_registry] проверяю живучесть всех {len(registry.routes)} маршрутов на блоке {latest}...")
    results = registry.refresh_liveness_all(latest)
    for rid, res in results.items():
        route = registry.routes[rid]
        print(f"  [{route.source}] {route.label}: live={res['live']} "
              f"({res.get('error') or ('amount_out=' + str(res['test_amount_out']))})")

    print(f"[route_registry] ИТОГ: живых {len(registry.live_routes())} из {len(registry.routes)}")

    out_path = Path(__file__).parent.parent / "data" / "task5_v4_route_registry_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(registry.to_summary(), indent=2, default=str))
    print(f"[route_registry] сохранено: {out_path}")


if __name__ == "__main__":
    main()
