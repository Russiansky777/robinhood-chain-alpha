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
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import (
    ASSUMED_REVERT_RATE,
    CHAIN_ID_MAINNET,
    CHAIN_ID_TESTNET,
    ENTRY_THRESHOLD_USD,
    EXECUTOR_CONTRACT_ADDRESS_MAINNET,
    EXECUTOR_CONTRACT_ADDRESS_TESTNET,
    PATH_A_KNOWN_ROUTER_ADDRESSES_FROM_HISTOGRAM,
    PATH_A_MIN_PRICE_IMPACT_FRACTION,
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

from task5_bot_detector import check_all_pairs_price_divergence, check_router_triggered_opportunity
from task5_bot_executor import Executor
from task5_bot_feed_client import FeedMessage, SequencerFeedClient, decode_l2_message
from task5_bot_pool_state import apply_swap_price_update_from_calldata, bootstrap_registry_from_rpc, refresh_pool_price
from task5_bot_route_precompute import RoutePrecomputeTable
from task5_bot_router_decode import (
    KNOWN_SELF_TRADE_ADDRESSES,
    KNOWN_SWAP_SELECTORS,
    decode_calldata,
    decode_pool_swap_calldata,
)
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
    ap.add_argument("--latency-sample-n", type=int, default=30,
                     help="Владелец, 2026-09-12: 'один длинный коннект... внутри него первым делом -- замер "
                          "задержки чтения по первым 20-30 сообщениям' -- ВНУТРИ ЭТОГО ЖЕ подключения, не "
                          "отдельным соединением. Для первых N сообщений считает (t_wall получения -- "
                          "sequencer_timestamp из заголовка), пишет data/task5_feed_latency_measurement.json.")
    ap.add_argument("--latency-out", type=str, default="data/task5_feed_latency_measurement.json",
                     help="Куда писать результат замера задержки чтения (см. --latency-sample-n).")
    ap.add_argument("--feed-url", type=str, default="",
                     help="Владелец, 2026-09-12: явный оверрайд URL фида -- например, "
                          "ws://127.0.0.1:9642 (собственный feed relay, см. scripts/deploy_feed_relay.sh) "
                          "вместо публичного wss://feed.mainnet.chain.robinhood.com. Пусто -- обычное "
                          "поведение по умолчанию (публичный mainnet/testnet URL). Для loopback-адресов "
                          "cooldown-гард НЕ применяется (см. task5_bot_feed_client.py::_is_loopback_feed_url).")
    args = ap.parse_args()

    dry_run = is_dry_run(args.confirm_mainnet)
    rpc_url = RPC_URL_TESTNET if args.testnet else RPC_URL_MAINNET
    feed_url = args.feed_url or (SEQUENCER_FEED_URL_TESTNET if args.testnet else SEQUENCER_FEED_URL_MAINNET)
    chain_id = CHAIN_ID_TESTNET if args.testnet else CHAIN_ID_MAINNET
    # --contract-address явно передан -- приоритет; иначе -- запасное значение
    # из конфига (заполняется владельцем после реального деплоя, см.
    # contracts/build/deploy_params.json и EXECUTOR_CONTRACT_ADDRESS_MAINNET/
    # _TESTNET в task5_bot_config.py) -- не забыть/не потерять адрес между запусками.
    contract_address = args.contract_address or (
        (EXECUTOR_CONTRACT_ADDRESS_TESTNET if args.testnet else EXECUTOR_CONTRACT_ADDRESS_MAINNET) or ""
    )

    print(f"[task5_bot] режим: {'DRY-RUN (ничего не отправляется)' if dry_run else 'LIVE (--confirm-mainnet)'}")
    print(f"[task5_bot] сеть: {'testnet' if args.testnet else 'mainnet'} (chain_id={chain_id})")
    print(f"[task5_bot] порог входа: ${ENTRY_THRESHOLD_USD}/попытка, допущение по откатам: {ASSUMED_REVERT_RATE:.0%}")

    # Владелец, 2026-09-12: "пока ключа [провайдера] нет -- цены из фида, bootstrap
    # через RPC только для пулов БЕЗ активности." Реальная находка бэктеста
    # (2026-09-12): eager-оценка ВСЕХ ~1140 пулов упирается в CU/s free tier
    # Alchemy (571/1140 в лучшем случае); target-оценка ТОЛЬКО реально
    # затронутых пулов дала 12/12 (100%). lazy_pricing=True -- тот же принцип
    # для живого бота: bootstrap делает ТОЛЬКО дешёвый PoolCreated-скан (один
    # eth_getLogs), цены заполняются по факту первого касания в горячем пути
    # (см. on_feed_message ниже -- один точечный RPC-запрос на пул при первом
    # касании, дальше -- пересчёт из calldata, без сети).
    print("[task5_bot] bootstrap: сканирование PoolCreated через RPC (единственный сетевой вызов ДО горячего пути, "
          "БЕЗ eager-оценки цен -- см. lazy_pricing)...")
    registry = bootstrap_registry_from_rpc(rpc_url=rpc_url, lazy_pricing=True)
    print(f"[task5_bot] найдено пулов: {len(registry.by_address)}")

    # Предрасчёт маршрутов -- ОДИН РАЗ здесь, до listen() (владелец,
    # 2026-09-13, п.4) -- см. task5_bot_route_precompute.py. exit_token=WETH --
    # та же заглушка, что exit_token в on_feed_message ниже (реальный выбор
    # exit_token зависит от направления цикла, не решено для общего случая).
    route_table = RoutePrecomputeTable()
    n_routes = route_table.build_for_registry(registry, exit_token=WETH)
    print(f"[task5_bot] предрасчитано маршрутов (poolA/poolB, оба порядка): {n_routes}")

    telemetry = TelemetryLog()
    executor = Executor(confirm_mainnet=args.confirm_mainnet, contract_address=contract_address,
                         telemetry=telemetry, chain_id=chain_id, registry=registry, route_table=route_table)

    # Последний известный на фиде номер блока -- нужен ТОЛЬКО для будущего
    # block_before_send в телеметрии (см. docs/TASK5_WRITEPATH_CLEAN_SPEC.md);
    # sequenceNumber == номер L2-блока на этой цепи (та же оговорка про
    # источник, что в спецификации). Список из одного элемента -- простейший
    # изменяемый холдер для замыкания on_feed_message ниже, не разделяемое
    # состояние между потоками (весь бот -- один asyncio-луп).
    last_seen_block_number: list[int | None] = [None]
    last_refresh_wall: dict[str, float] = {}  # pool_address.lower() -> time.time() последнего RPC-рефреша
    latency_samples: list[dict] = []  # владелец, 2026-09-12: замер задержки чтения по первым N сообщениям
    # ЭТОГО ЖЕ подключения (не отдельным соединением) -- см. --latency-sample-n
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
        # Владелец, 2026-09-12: замер задержки чтения ВНУТРИ этого же
        # подключения (не отдельным соединением) -- по первым N сообщениям.
        # ЧЕСТНАЯ ОГОВОРКА: sequencer_timestamp -- ЦЕЛЫЕ unix-секунды (не мс),
        # это ограничивает точность замера до ~1с -- реальная суб-секундная
        # задержка чтения этим полем не разрешима, честно фиксируем то, что
        # есть, не обманываем себя мнимой точностью.
        if len(latency_samples) < args.latency_sample_n and msg.sequencer_timestamp is not None:
            latency_s = msg.t_wall - msg.sequencer_timestamp
            latency_samples.append({
                "sequence_number": msg.sequence_number,
                "t_wall": msg.t_wall,
                "sequencer_timestamp": msg.sequencer_timestamp,
                "latency_s": latency_s,
            })
            print(f"[task5_bot][latency] seq={msg.sequence_number} "
                  f"latency_s={latency_s:.3f} (n={len(latency_samples)}/{args.latency_sample_n})")

        last_seen_block_number[0] = msg.sequence_number
        decoded = decode_l2_message(msg.raw_l2_msg_hex) if msg.raw_l2_msg_hex else []

        touched_pairs: set[tuple[str, str]] = set()
        now = time.time()
        for entry in decoded:
            to_addr = entry.get("to")
            if not to_addr:
                continue
            to_addr_l = to_addr.lower()

            # Путь А, минимальный (владелец, 2026-09-12, 'добавка'): calldata
            # роутера, ДО исполнения -- проверяется НЕЗАВИСИМО от того, известен
            # ли нам сам `to_addr` как ПУЛ (роутер почти никогда не совпадает с
            # адресом пула -- это другой контракт). Триггер -- по СЕЛЕКТОРУ
            # calldata (`KNOWN_SWAP_SELECTORS`), не по конкретному адресу
            # роутера -- реальная гистограмма (`task5_bot_router_histogram.py`,
            # захват через relay 2026-09-12) нашла минимум 3 разных адреса,
            # использующих ОДИН и тот же стандартный интерфейс (SwapRouter02/
            # UniversalRouter) -- см. `PATH_A_KNOWN_ROUTER_ADDRESSES_FROM_HISTOGRAM`
            # в конфиге (только для читаемости логов, не источник истины).
            data_hex = entry.get("data") or "0x"
            selector = ("0x" + data_hex[2:10]) if len(data_hex) >= 10 else None
            if selector in KNOWN_SWAP_SELECTORS and to_addr_l not in KNOWN_SELF_TRADE_ADDRESSES:
                for intent in decode_calldata(to_addr, data_hex):
                    if None in (intent.token_in, intent.token_out, intent.fee, intent.amount_in):
                        continue  # честно: этот хоп/команда декодирована не полностью -- не гадаем (см. router_decode.py)
                    touched_pool = registry.find_pool_by_tokens_fee(intent.token_in, intent.token_out, intent.fee)
                    if touched_pool is None:
                        continue  # пул вне нашей вселенной -- не наш случай

                    # Владелец, 2026-09-12: "пока ключа [провайдера] нет -- цены из
                    # фида. Для пулов, в которых декодер видел своп, цена восстанавливается
                    # из amountIn/направления. Bootstrap через RPC -- только для пулов
                    # БЕЗ активности." Реализовано буквально: RPC -- ТОЛЬКО на первое
                    # касание пула (нет базовой цены вообще), дальше -- пересчёт из
                    # calldata, без сети (см. task5_bot_pool_state.py::apply_swap_
                    # price_update_from_calldata, реальные формулы Uniswap V3, in-tick).
                    if touched_pool.sqrt_price_x96 is None:
                        last_ts = last_refresh_wall.get(touched_pool.address.lower(), 0.0)
                        if now - last_ts >= POOL_REFRESH_COOLDOWN_S:
                            if refresh_pool_price(touched_pool, rpc_url=rpc_url, record_block_number=msg.sequence_number):
                                last_refresh_wall[touched_pool.address.lower()] = now
                                n_refreshes_done[0] += 1
                                touched_pairs.add(registry._pair_key(touched_pool.token0, touched_pool.token1))
                    else:
                        zero_for_one_router = intent.token_in.lower() == touched_pool.token0.lower()
                        if apply_swap_price_update_from_calldata(touched_pool, zero_for_one_router, intent.amount_in,
                                                                  block_number=msg.sequence_number):
                            touched_pairs.add(registry._pair_key(touched_pool.token0, touched_pool.token1))

                    router_opp = check_router_triggered_opportunity(
                        registry, touched_pool, intent.token_in, intent.token_out, intent.amount_in,
                        trigger_sequence_number=msg.sequence_number,
                        min_price_impact_fraction=PATH_A_MIN_PRICE_IMPACT_FRACTION,
                        assumed_gas_cost_usd=ASSUMED_GAS_COST_USD_PLACEHOLDER,
                        exit_token=WETH,  # та же заглушка, что и divergence-путь ниже
                        router_to=to_addr,
                        router_function_label=KNOWN_SWAP_SELECTORS.get(selector),
                    )
                    if router_opp is not None:
                        amount_str = (f"{router_opp.touched_amount_in_human:.6f}"
                                      if router_opp.touched_amount_in_human is not None else "?")
                        usd_str = (f"${router_opp.touched_amount_in_usd_approx:.2f}"
                                   if router_opp.touched_amount_in_usd_approx is not None else "не оценено")
                        print(f"[task5_bot][Путь А] триггер по сдвигу цены: "
                              f"router={router_opp.router_to} ({router_opp.router_function_label}) "
                              f"pool={router_opp.touched_pool} zeroForOne={router_opp.touched_zero_for_one} "
                              f"сдвиг~{router_opp.price_impact_fraction_approx:.4%} "
                              f"amount_in~{amount_str} (~{usd_str})")
                        executor.handle_opportunity(router_opp, size_usd=router_opp.expected_capture_usd * args.size_fraction)

            pool = registry.by_address.get(to_addr_l)
            if pool is None:
                continue  # цель -- не известный нам пул (роутер, другой контракт и т.п.) -- честно пропускаем
                # для ЭТОЙ, direct-touch ветки (роутер-ветка выше уже обработана независимо)
            n_touches_seen[0] += 1

            if pool.sqrt_price_x96 is None:
                # Первое касание этого пула вообще -- нужен ОДИН точечный RPC-запрос
                # для базовой цены (см. комментарий в роутер-ветке выше -- тот же принцип).
                last_ts = last_refresh_wall.get(pool.address.lower(), 0.0)
                if now - last_ts < POOL_REFRESH_COOLDOWN_S:
                    continue
                if refresh_pool_price(pool, rpc_url=rpc_url, record_block_number=msg.sequence_number):
                    last_refresh_wall[pool.address.lower()] = now
                    n_refreshes_done[0] += 1
                    touched_pairs.add(registry._pair_key(pool.token0, pool.token1))
                continue

            # Базовая цена уже есть -- прямой вызов пула (`to==pool`, реальный паттерн
            # `0x65050a9b...` из паспорта) декодируется НАПРЯМУЮ (swap(address,bool,
            # int256,uint160,bytes)), без RPC (владелец, 2026-09-12, п.2).
            decoded_swap = decode_pool_swap_calldata(data_hex)
            if decoded_swap is not None:
                zero_for_one_direct, amount_specified = decoded_swap
                if apply_swap_price_update_from_calldata(pool, zero_for_one_direct, amount_specified,
                                                          block_number=msg.sequence_number):
                    touched_pairs.add(registry._pair_key(pool.token0, pool.token1))

        # Владелец, 2026-09-12 ('правильный триггер', после разбора известного
        # ответа/групп 1-2-3): "между двумя пулами одной пары цены разошлись
        # больше порога -> стреляем, независимо от того, что разрыв создало."
        # Заменяет прежний хардкод "только WETH/USDG" -- generic по ЛЮБОЙ
        # паре, у которой в реестре >=2 пула и хотя бы одна из них была
        # реально затронута этим сообщением (touched_pairs, не сканируем
        # ВСЕ пары на каждое сообщение фида без необходимости).
        if not touched_pairs:
            return

        for pair_opp in check_all_pairs_price_divergence(
            registry, trigger_sequence_number=msg.sequence_number,
        ):
            print(f"[task5_bot][попарный разрыв] pool_a(cheap)={pair_opp.pool_a} pool_b(expensive)={pair_opp.pool_b} "
                  f"разрыв={pair_opp.rel_divergence_fraction:.4%} комиссия(round-trip)={pair_opp.combined_fee_fraction:.4%}")
            executor.handle_opportunity(pair_opp, size_usd=pair_opp.expected_capture_usd * args.size_fraction)

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

    # Владелец, 2026-09-12: замер задержки чтения -- ВНУТРИ того же
    # подключения, что и весь остальной dry-run (см. on_feed_message выше).
    # Если фид вообще не пустил (403/обрыв до первого сообщения) --
    # latency_samples пуст, честно фиксируем это, а не молчим и не
    # подставляем выдуманные числа.
    latencies = [s["latency_s"] for s in latency_samples]
    latency_summary = {
        "n_samples": len(latency_samples),
        "requested_n": args.latency_sample_n,
        "feed_let_us_in": client.diag.get("n_messages_total", 0) > 0,
        "last_disconnect_error": client.diag.get("last_disconnect_error"),
        # Владелец, 2026-09-12: полные заголовки/тело последнего 403 (если
        # он был) -- см. SequencerFeedClient.listen()/InvalidStatus handling
        # в task5_bot_feed_client.py. None, если этого поля не было
        # (не было 403 именно с этим типом исключения) -- не подставляем.
        "last_invalid_status_code": client.diag.get("last_invalid_status_code"),
        "last_invalid_status_headers": client.diag.get("last_invalid_status_headers"),
        "last_invalid_status_body": client.diag.get("last_invalid_status_body"),
        "samples": latency_samples,
        "min_latency_s": min(latencies) if latencies else None,
        "max_latency_s": max(latencies) if latencies else None,
        "mean_latency_s": (sum(latencies) / len(latencies)) if latencies else None,
        "note": "sequencer_timestamp -- целые unix-секунды (не мс), точность замера ограничена ~1с; "
                "локальное время (t_wall) должно быть NTP-синхронизировано на хосте для честного "
                "результата -- проверяется отдельно (timedatectl/chronyc), не этим скриптом.",
    }
    if not latency_samples:
        latency_summary["note"] = (
            f"0 сэмплов -- фид НЕ пустил ни одного сообщения с sequencer_timestamp за это подключение "
            f"(диагностика: {client.diag}). Честно: подключение либо не удалось (403/обрыв), либо "
            f"закрылось до первого сообщения -- НЕ повторяем попытку в рамках этого запуска."
        )
    print(f"[task5_bot] замер задержки чтения: n={latency_summary['n_samples']}/{args.latency_sample_n}, "
          f"min={latency_summary['min_latency_s']}, mean={latency_summary['mean_latency_s']}, "
          f"max={latency_summary['max_latency_s']}")
    latency_out_path = args.latency_out
    os.makedirs(os.path.dirname(latency_out_path) or ".", exist_ok=True)
    with open(latency_out_path, "w") as f:
        json.dump(latency_summary, f, indent=2)
        f.write("\n")
    print(f"[task5_bot] результат замера задержки записан в {latency_out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
