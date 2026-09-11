#!/usr/bin/env python3
"""Задача 5, живой бот -- главная точка входа.

usage:
    # dry-run (по умолчанию, ничего не отправляет, только лог "вот здесь бы вошёл"):
    python analysis/task5_bot_run.py --testnet

    # реальная отправка -- НЕ РЕАЛИЗОВАНА в этой версии (см. task5_bot_executor.py),
    # --confirm-mainnet сейчас только готовит подписанта и падает с NotImplementedError
    # при первой реальной попытке -- намеренно, до отдельного явного разрешения владельца
    # и деплоя контракта (см. план по неделям, PROJECT_STATE.md).
    python analysis/task5_bot_run.py --confirm-mainnet

Владелец, 2026-09-11/12, требования к горячему пути: "состояние
отслеживаемых пулов -- в памяти, обновляется из фида, без RPC-запросов
в горячем пути" + "подпись и отправка -- сразу после детекции, без
промежуточных проверок через сеть".

**ЧЕСТНОЕ ОБНОВЛЕНИЕ, 2026-09-13 (владелец, п.3 -- реальные детекции):**
первое требование ("ноль RPC в горячем пути") сейчас НАРУШЕНО осознанно
и явно, не тихо -- фид секвенсера отдаёт RAW-транзакции ДО исполнения
(подтверждено реальной разведкой формы сообщений,
`task5_bot_feed_structure_probe.py`), не постсвоповую цену; без полного
симулятора математики Uniswap V3 (не написан в этой сессии) единственный
способ узнать РЕАЛЬНУЮ (не устаревшую) цену после того, как пул реально
затронут -- точечный `eth_call slot0()/liquidity()` именно для этого
пула (`refresh_pool_price()` в `task5_bot_pool_state.py`), с кулдауном.
Это диагностический компромисс для dry-run -- см. докстринг
`refresh_pool_price()` для полного разбора и того, что нужно вместо
него для настоящего низколатентного горячего пути. Второе требование
("подпись сразу после детекции") по-прежнему соблюдено -- executor всё
ещё `NotImplementedError`, ничего не подписывается и не отправляется."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import (
    ASSUMED_REVERT_RATE,
    CHAIN_ID_MAINNET,
    CHAIN_ID_TESTNET,
    ENTRY_THRESHOLD_USD,
    RPC_URL_MAINNET,
    RPC_URL_TESTNET,
    SEQUENCER_FEED_URL_MAINNET,
    SEQUENCER_FEED_URL_TESTNET,
    USDG,
    USDG_DECIMALS,
    WETH,
    WETH_DECIMALS,
    is_dry_run,
)
import time

from task5_bot_detector import check_pair_for_divergence
from task5_bot_executor import Executor
from task5_bot_feed_client import FeedMessage, SequencerFeedClient, decode_l2_message
from task5_bot_pool_state import bootstrap_registry_from_rpc, refresh_pool_price
from task5_bot_route_precompute import RoutePrecomputeTable
from task5_bot_telemetry import TelemetryLog

# Владелец, 2026-09-13, п.3: минимальный кулдаун между RPC-рефрешами ОДНОГО
# и того же пула -- реальная разведка (task5_bot_feed_structure_probe.py)
# показала блоки с ДЕСЯТКАМИ транзакций каждый; без кулдауна один и тот же
# горячий пул мог бы триггерить рефреш на каждой транзакции в блоке.
POOL_REFRESH_COOLDOWN_S = 1.0

# Оценка стоимости газа одной попытки -- честно, ЗАГЛУШКА до реального
# наблюдения (владелец: тест -- после 29.09, на платном газе; до этого
# момента реального тарифа для подстановки сюда просто не существует,
# см. PROJECT_STATE.md, WebSearch про отсутствие официального объявления).
ASSUMED_GAS_COST_USD_PLACEHOLDER = 0.30  # ~2.4x текущей субсидированной L2-комиссии ($0.125) -- консервативный
# ориентир до реальных пост-29.09 данных, ПЕРЕСЧИТЫВАЕТСЯ автоматически из телеметрии в течение теста


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--confirm-mainnet", action="store_true",
                     help="БЕЗ этого флага -- всегда dry-run, ничего не отправляется.")
    ap.add_argument("--testnet", action="store_true", help="Использовать testnet (chain id 46630) вместо mainnet.")
    ap.add_argument("--contract-address", type=str, default="",
                     help="Адрес задеплоенного ClosedCycleExecutorV3 (нужен только для --confirm-mainnet).")
    ap.add_argument("--size-fraction", type=float, default=1.0,
                     help="Доля от целевого размера позиции -- владелец: неделя 1 теста = 0.5.")
    ap.add_argument("--duration-seconds", type=float, default=None,
                     help="Владелец, 2026-09-13: ограниченный по времени dry-run смоук-тест ПЕРЕД реальными "
                          "деньгами -- слушает фид ровно это число секунд, затем аккуратно завершается и "
                          "печатает диагностику (по умолчанию -- бесконечно, как раньше, для реального прод-режима).")
    args = ap.parse_args()

    dry_run = is_dry_run(args.confirm_mainnet)
    rpc_url = RPC_URL_TESTNET if args.testnet else RPC_URL_MAINNET
    feed_url = SEQUENCER_FEED_URL_TESTNET if args.testnet else SEQUENCER_FEED_URL_MAINNET
    chain_id = CHAIN_ID_TESTNET if args.testnet else CHAIN_ID_MAINNET

    print(f"[task5_bot] режим: {'DRY-RUN (ничего не отправляется)' if dry_run else 'LIVE (--confirm-mainnet)'}")
    print(f"[task5_bot] сеть: {'testnet' if args.testnet else 'mainnet'} (chain_id={chain_id})")
    print(f"[task5_bot] порог входа: ${ENTRY_THRESHOLD_USD}/попытка, допущение по откатам: {ASSUMED_REVERT_RATE:.0%}")

    print("[task5_bot] bootstrap: сканирование PoolCreated через RPC (единственный сетевой вызов ДО горячего пути)...")
    registry = bootstrap_registry_from_rpc(rpc_url=rpc_url)
    print(f"[task5_bot] найдено пулов: {len(registry.by_address)}")

    # Предрасчёт маршрутов -- ОДИН РАЗ здесь, до listen() (владелец,
    # 2026-09-13, п.4) -- см. task5_bot_route_precompute.py. exit_token=WETH --
    # та же заглушка, что exit_token в on_feed_message ниже (реальный выбор
    # exit_token зависит от направления цикла, не решено для общего случая).
    route_table = RoutePrecomputeTable()
    n_routes = route_table.build_for_registry(registry, exit_token=WETH)
    print(f"[task5_bot] предрасчитано маршрутов (poolA/poolB, оба порядка): {n_routes}")

    telemetry = TelemetryLog()
    executor = Executor(confirm_mainnet=args.confirm_mainnet, contract_address=args.contract_address,
                         telemetry=telemetry, chain_id=chain_id, registry=registry, route_table=route_table)

    # Последний известный на фиде номер блока -- нужен ТОЛЬКО для будущего
    # block_before_send в телеметрии (см. docs/TASK5_WRITEPATH_CLEAN_SPEC.md);
    # sequenceNumber == номер L2-блока на этой цепи (та же оговорка про
    # источник, что в спецификации). Список из одного элемента -- простейший
    # изменяемый холдер для замыкания on_feed_message ниже, не разделяемое
    # состояние между потоками (весь бот -- один asyncio-луп).
    last_seen_block_number: list[int | None] = [None]
    last_refresh_wall: dict[str, float] = {}  # pool_address.lower() -> time.time() последнего RPC-рефреша
    n_touches_seen = [0]
    n_refreshes_done = [0]

    def on_feed_message(msg: FeedMessage) -> None:
        """Владелец, 2026-09-13: реальная разведка формы сообщений фида
        (`task5_bot_feed_structure_probe.py`) нашла реальную причину, по
        которой детекция раньше молчала (0/2286 декодировано) -- l2Msg
        base64, не hex, и это batch МНОГИХ транзакций, не одна. Декодер
        (`decode_l2_message`) исправлен, реально проверен на пойманных
        сэмплах. ЧЕСТНЫЙ КОМПРОМИСС (не тихая замена архитектуры,
        см. `refresh_pool_price()`): фид даёт RAW-намерение ДО исполнения,
        не постсвоповую цену -- полного симулятора математики V3 в этой
        сессии нет, поэтому для пулов, ЗАТРОНУТЫХ напрямую (`to` ==
        адрес пула -- реальный паттерн активных ботов, см. паспорт про
        `0x65050a9b...`), делается ТОЧЕЧНЫЙ (не на каждый пул, не на
        каждое сообщение) RPC-рефреш `slot0()`/`liquidity()` -- это уже
        НЕ "ноль RPC в горячем пути", нарушение явное и залогированное,
        не скрытое."""
        last_seen_block_number[0] = msg.sequence_number
        decoded = decode_l2_message(msg.raw_l2_msg_hex) if msg.raw_l2_msg_hex else []

        touched_pairs: set[tuple[str, str]] = set()
        now = time.time()
        for entry in decoded:
            to_addr = entry.get("to")
            if not to_addr:
                continue
            pool = registry.by_address.get(to_addr.lower())
            if pool is None:
                continue  # цель -- не известный нам пул (роутер, другой контракт и т.п.) -- честно пропускаем,
                # не пытаемся угадать внутренний своп через роутер без декодирования его calldata
            n_touches_seen[0] += 1
            last_ts = last_refresh_wall.get(pool.address.lower(), 0.0)
            if now - last_ts < POOL_REFRESH_COOLDOWN_S:
                continue  # кулдаун -- этот же пул уже рефрешился недавно в этом же блоке
            if refresh_pool_price(pool, rpc_url=rpc_url, record_block_number=msg.sequence_number):
                last_refresh_wall[pool.address.lower()] = now
                n_refreshes_done[0] += 1
                touched_pairs.add(registry._pair_key(pool.token0, pool.token1))

        # Детекция -- ТОЛЬКО для пары WETH/USDG (та же заглушка, что раньше:
        # decimals/exit_token завязаны конкретно на эту пару, обобщение на
        # произвольные пары -- отдельная задача, не сделана здесь).
        weth_usdg_key = registry._pair_key(WETH, USDG)
        if weth_usdg_key not in touched_pairs:
            return

        opp = check_pair_for_divergence(
            registry, WETH, USDG, WETH_DECIMALS, USDG_DECIMALS,
            trigger_sequence_number=msg.sequence_number,
            assumed_gas_cost_usd=ASSUMED_GAS_COST_USD_PLACEHOLDER,
            exit_token=WETH,  # ЗАГЛУШКА: реальный exit_token зависит от направления цикла
            # (какой токен на самом деле "выходит" из net-flow) -- та же логика, что
            # closed_cycle_exit в Dune-запросах этой сессии, здесь пока не воспроизведена
            # для WETH/USDG пары в общем виде (нужна проверка первой недели/дня).
        )
        if opp is not None:
            executor.handle_opportunity(opp, size_usd=opp.expected_capture_usd * args.size_fraction)

    client = SequencerFeedClient(feed_url)
    print(f"[task5_bot] подключение к фиду: {feed_url}")
    if args.duration_seconds is not None:
        print(f"[task5_bot] СМОУК-ТЕСТ: ограничено {args.duration_seconds}с (владелец, 2026-09-13, "
              f"первый безопасный dry-run перед реальными деньгами)")
    try:
        if args.duration_seconds is not None:
            try:
                asyncio.run(asyncio.wait_for(client.listen(on_feed_message), timeout=args.duration_seconds))
            except asyncio.TimeoutError:
                pass  # штатное завершение смоук-теста по времени, не ошибка
        else:
            asyncio.run(client.listen(on_feed_message))
    except KeyboardInterrupt:
        pass
    print(f"[task5_bot] диагностика фида: {client.diag}")
    print(f"[task5_bot] последний известный номер блока с фида: {last_seen_block_number[0]}")
    print(f"[task5_bot] касаний известных пулов: {n_touches_seen[0]}, реальных RPC-рефрешей цены: {n_refreshes_done[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
