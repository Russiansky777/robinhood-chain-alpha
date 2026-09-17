#!/usr/bin/env python3
"""Задача 5, живой бот -- ПЕРВАЯ живая проверка полного пути реакции на
живом фиде секвенсера Robinhood Chain, с NL VPS (45.59.170.76).

Владелец (заказчик), 2026-09-17, дословно (сокращённо): "14 мс до
секвенсера -- это RTT сети, не полный путь принятия решения. На Arc
Mainnet весь путь реально мерили (обнаружение блока -> чтение состояния
-> расчёт -> подпись) -- медиана ~500 мс, из них большая часть уходила
на обнаружение нового блока (см. `analysis/task_arc_arb_our_share.py`,
результат в docs/PROJECT_STATE.md: медиана 499.76мс/p90 759.97мс). Для
Robinhood Chain (блок ~120мс) полный путь НИ РАЗУ не мерили -- измерить
его: детекция, декодирование, расчёт по памяти, подпись; отдельно --
доставка ЗАВЕДОМО НЕВАЛИДНОЙ (плохой nonce) транзакции до секвенсера."

Метод -- ОДНО подключение к живому фиду (cooldown-гард
`task5_bot_feed_client.py`, "не более одного подключения за 30 минут",
реально проверено ПЕРЕД этим прогоном -- `task5_feed_cooldown_check.py`
на этом же хосте показал `seconds_until_allowed=0.0`, состояние файла
`null`, то есть это подключение -- первое с этого хоста в текущем окне):

1. **Bootstrap** (ВНЕ горячего пути, единственный блок сетевых вызовов
   ДО подключения к фиду): регистрируются ТОЛЬКО известные, реально
   торгуемые пулы (`TASK5_KNOWN_PROFITABLE_POOLS`, `task5_bot_config.py`)
   -- их slot0()/liquidity() читаются РЕАЛЬНО (`populate_initial_prices`,
   тот же код, что использует живой бот `task5_bot_run.py`), чтобы
   расчётная стадия ниже работала на РЕАЛЬНЫХ, не пустых состояниях.
2. **Одно** подключение к `wss://feed.mainnet.chain.robinhood.com`
   (`SequencerFeedClient`, тот же класс, что в живом боте) на
   `--duration-seconds` секунд.
3. На КАЖДОЕ сообщение фида (`FeedMessage.t_wall` = t0, момент получения
   сырого сообщения по сокету):
     - **decode_ms** (t1-t0): `decode_l2_message()` -- РЕАЛЬНЫЙ декодер
       живого бота (base64 l2Msg, Nitro batch type 3 -> под-сообщения
       type 4 signedTx -> RLP), НЕ переписан для этого замера.
     - **calc_ms** (t2-t1): для каждого декодированного под-сообщения --
       попытка пересчитать цену пула ПО СОСТОЯНИЮ В ПАМЯТИ (прямой вызов
       известного пула -- `decode_pool_swap_calldata` + `apply_swap_
       price_update_from_calldata`; ЛИБО calldata известного роутера --
       `decode_calldata` + поиск пула по (token_in, token_out, fee) в
       уже забутстрапленном реестре) -- ТЕ ЖЕ реальные формулы Uniswap V3
       (in-tick), что в `task5_bot_pool_state.py`/`task5_bot_run.py`.
       НИ ОДНОГО сетевого вызова в этом шаге -- честно проверено (весь
       путь -- чистый Python над уже загруженным `PoolRegistry`).
     - **sign_ms** (t3-t2): `Account.sign_transaction` ОДНОЙ представительной
       транзакции свежесгенерированным (`Account.create()`) тестовым
       EOA-ключом -- НЕ `PRIVATE_KEY_NOX`/`PRIVATE_KEY_TASK5_BOT`, без
       фондирования, без сети.
   Отдельно, МЕЖДУ сообщениями (не часть цепочки одного сообщения) --
   **detect_wait_ms**: интервал между `t_wall` этого и предыдущего
   сообщения фида (= по факту это наблюдаемый интервал появления новых
   L2-блоков на фиде, "обнаружение" в терминах задачи -- НЕ управляемая
   нами задержка, а сам темп цепи).
4. ОТДЕЛЬНО, РОВНО ОДИН РАЗ за весь прогон (владелец: "не чаще одного
   раза за прогон"): подписываем и отправляем ОДНУ транзакцию с
   ЗАВЕДОМО НЕВЕРНЫМ nonce (10**9 -- гарантированно не пройдёт
   nonce-проверку, гарантированно НЕ исполнится, 0 реальных средств) на
   `SEQUENCER_SUBMIT_URL_MAINNET` (write-only приёмник секвенсера, тот
   же эндпоинт, что использует `task5_bot_sender.py` для реальной
   отправки) -- меряем round-trip до ЛЮБОГО ответа узла (ack ИЛИ явная
   ошибка, обе стороны одинаково подтверждают, что запрос дошёл и
   узел ответил). Фоллбэк на публичный RPC -- ТОЛЬКО при сетевой
   ошибке самого запроса к sequencer-эндпоинту (не при обычном
   JSON-RPC error в теле ответа -- это уже означает "дошло").

ЧЕСТНАЯ ОГОВОРКА, ГЛАВНАЯ ЦЕЛЬ ЭТОГО ПРОГОНА (владелец: "если формат
живых данных фида не совпадёт с тем, что заложено в декодере -- это
нужно сообщить прямо и отдельно, не пытаться подогнать код под
реальность без объяснения"): `decode_diag` ниже -- ПОЛНЫЙ, честный
счётчик исходов декодирования (`decoded_ok`/`decode_error`/
`unhandled_msg_type_N`/`unrecognized_first_byte`/`no_l2msg_field`) по
факту этого прогона. Если `decoded_ok` мал или 0 относительно
`n_messages_total` -- это САМОСТОЯТЕЛЬНАЯ находка/блокер, не шум."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests
from eth_account import Account
from eth_utils import to_checksum_address

from task5_bot_config import (
    CHAIN_ID_MAINNET,
    RPC_URL_MAINNET,
    SEQUENCER_FEED_URL_MAINNET,
    SEQUENCER_SUBMIT_URL_MAINNET,
    WETH_USDG_POOL,
)
from task5_bot_feed_client import FeedMessage, SequencerFeedClient, decode_l2_message
from task5_bot_pool_state import (
    PoolRegistry,
    _merge_known_profitable_pools,
    apply_swap_price_update_from_calldata,
    populate_initial_prices,
)
from task5_bot_router_decode import (
    KNOWN_SELF_TRADE_ADDRESSES,
    KNOWN_SWAP_SELECTORS,
    decode_calldata,
    decode_pool_swap_calldata,
)


# --- Жёсткий watchdog (владелец: неизвестно заранее, как поведёт себя декодер на
# реальном живом объёме -- одно ОЧЕНЬ большое сообщение фида (документированный
# реальный случай -- 3.57МБ в task5_bot_feed_client.py) может содержать тысячи
# под-транзакций, и `decode_l2_message` делает РЕАЛЬНЫЙ ecrecover (криптография,
# не бесплатно) для КАЖДОЙ -- если это окажется на порядки медленнее, чем блок
# в 120мс, это САМОСТОЯТЕЛЬНАЯ находка, а не повод менять существующий декодер.
# `asyncio.wait_for` НЕ может прервать чисто синхронный (без await внутри)
# Python-код на середине -- если один вызов `on_message` окажется аномально
# долгим, единственный надёжный способ не зависнуть НАВСЕГДА -- отдельный ОС-поток
# с `threading.Timer`, который может сработать независимо от того, чем занят
# главный поток (GIL переключается между потоками между байткод-инструкциями,
# в отличие от кооперативных await-точек asyncio). Пишет ЧАСТИЧНЫЙ результат
# (всё, что реально успело накопиться к этому моменту) -- честно, не молчит.
_LIVE_STATE: dict = {
    "n_messages_total_seen": 0,
    "decode_diag": Counter(),
    "all_decode_ms": [],
    "all_calc_ms": [],
    "all_sign_ms": [],
    "all_processing_total_ms": [],
    "detect_wait_ms": [],
    "raw_samples_kept": [],
    "slowest_message": None,
    "test_wallet_address": None,
}


def _stats(samples: list[float]) -> dict | None:
    if not samples:
        return None
    return {
        "n": len(samples),
        "median_ms": statistics.median(samples),
        "p90_ms": (statistics.quantiles(samples, n=10)[8] if len(samples) >= 10 else max(samples)),
        "min_ms": min(samples),
        "max_ms": max(samples),
        "mean_ms": statistics.mean(samples),
    }


def _snapshot_live_state_as_stage_stats() -> dict:
    detect = _LIVE_STATE["detect_wait_ms"]
    proc = _LIVE_STATE["all_processing_total_ms"]
    full_total = [d + p for d, p in zip(detect, proc[1:])] if len(detect) == len(proc) - 1 and detect else None
    return {
        "detect_wait_ms": _stats(detect),
        "decode_ms": _stats(_LIVE_STATE["all_decode_ms"]),
        "calc_ms": _stats(_LIVE_STATE["all_calc_ms"]),
        "sign_ms": _stats(_LIVE_STATE["all_sign_ms"]),
        "processing_total_ms_decode_calc_sign": _stats(proc),
        "full_total_with_detect_wait_ms": _stats(full_total) if full_total else None,
    }


def _watchdog_fire(started_monotonic: float) -> None:
    elapsed_s = time.monotonic() - started_monotonic
    print(f"\n[latency_probe][WATCHDOG] жёсткий таймаут ({elapsed_s:.1f}с реального времени) -- "
          f"принудительное завершение процесса, печатаем ЧАСТИЧНЫЙ результат как есть, "
          f"НЕ выдумываем недостающее.", file=sys.stderr)
    partial = {
        "HARD_WATCHDOG_TRIGGERED": True,
        "watchdog_note": "Главный поток не вернул управление до жёсткого дедлайна (--duration + запас) -- "
                          "скорее всего один или несколько сообщений фида декодировались/считались АНОМАЛЬНО "
                          "долго (см. slowest_message ниже, если успел записаться) -- это САМОСТОЯТЕЛЬНАЯ "
                          "находка, не сбой замера.",
        "elapsed_wall_s": elapsed_s,
        "n_messages_total_seen": _LIVE_STATE["n_messages_total_seen"],
        "decode_diag": dict(_LIVE_STATE["decode_diag"]),
        "slowest_message": _LIVE_STATE["slowest_message"],
        "test_wallet_address": _LIVE_STATE["test_wallet_address"],
        "stage_stats_partial": _snapshot_live_state_as_stage_stats(),
        "raw_samples_kept_partial": _LIVE_STATE["raw_samples_kept"],
    }
    try:
        text = json.dumps(partial, indent=2, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001 -- даже это должно быть честным, не должно само по себе падать без следа
        text = json.dumps({"HARD_WATCHDOG_TRIGGERED": True, "dump_error": str(exc)})
    print(text)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)  # noqa: SLF001 -- намеренно: единственный надёжный способ гарантированно завершиться


def install_hard_watchdog(hard_deadline_s: float) -> threading.Timer:
    started = time.monotonic()
    timer = threading.Timer(hard_deadline_s, _watchdog_fire, args=(started,))
    timer.daemon = True
    timer.start()
    return timer


def bootstrap_known_pools_registry() -> tuple[PoolRegistry, dict]:
    """Единственный блок сетевых вызовов ДО подключения к фиду -- см.
    докстринг модуля, п.1. Не полный `bootstrap_registry_from_rpc`
    (который сканирует ВСЮ вселенную ~1150+ пулов через eth_getLogs +
    батч-eth_call -- ненужная нагрузка/время для замера латентности) --
    только уже известные, реально прибыльные пулы этой сессии."""
    registry = PoolRegistry()
    _merge_known_profitable_pools(registry)
    stats = populate_initial_prices(registry, rpc_url=RPC_URL_MAINNET, batch_size=8)
    return registry, stats


def do_calc_step(registry: PoolRegistry, decoded_entries: list[dict]) -> dict:
    """Реальный расчёт по состоянию В ПАМЯТИ -- см. докстринг модуля, п.3.
    Возвращает диагностику (сколько под-сообщений реально пересчитано),
    не триггерит исполнение -- чистый замер стоимости арифметики
    хот-пути на реальных декодированных данных этого сообщения."""
    n_checked = 0
    n_recomputed = 0
    for entry in decoded_entries:
        to_addr = entry.get("to")
        if not to_addr or "decode_error" in entry or "unhandled_msg_type" in entry:
            continue
        n_checked += 1
        to_addr_l = to_addr.lower()
        data_hex = entry.get("data") or "0x"
        selector = ("0x" + data_hex[2:10]) if len(data_hex) >= 10 else None

        pool = registry.by_address.get(to_addr_l)
        if pool is not None:
            if pool.sqrt_price_x96 is not None:
                decoded_swap = decode_pool_swap_calldata(data_hex)
                if decoded_swap is not None:
                    zero_for_one, amount_specified = decoded_swap
                    if apply_swap_price_update_from_calldata(pool, zero_for_one, amount_specified, block_number=None):
                        n_recomputed += 1
            continue

        if selector in KNOWN_SWAP_SELECTORS and to_addr_l not in KNOWN_SELF_TRADE_ADDRESSES:
            for intent in decode_calldata(to_addr, data_hex):
                if None in (intent.token_in, intent.token_out, intent.fee, intent.amount_in):
                    continue
                touched_pool = registry.find_pool_by_tokens_fee(intent.token_in, intent.token_out, intent.fee)
                if touched_pool is None or touched_pool.sqrt_price_x96 is None:
                    continue
                zero_for_one_router = intent.token_in.lower() == touched_pool.token0.lower()
                if apply_swap_price_update_from_calldata(touched_pool, zero_for_one_router, intent.amount_in,
                                                          block_number=None):
                    n_recomputed += 1
    return {"n_checked": n_checked, "n_recomputed": n_recomputed}


async def run_probe(duration_s: float, registry: PoolRegistry, max_raw_samples_kept: int) -> dict:
    test_account = Account.create()
    _LIVE_STATE["test_wallet_address"] = test_account.address
    prev_t_wall: list[float | None] = [None]
    dummy_tx_to = to_checksum_address(WETH_USDG_POOL)
    decode_diag: Counter = _LIVE_STATE["decode_diag"]

    def on_message(msg: FeedMessage) -> None:
        _LIVE_STATE["n_messages_total_seen"] += 1
        t0 = msg.t_wall
        if prev_t_wall[0] is not None:
            _LIVE_STATE["detect_wait_ms"].append((t0 - prev_t_wall[0]) * 1000.0)
        prev_t_wall[0] = t0

        if msg.raw_l2_msg_hex is None:
            decode_diag["no_l2msg_field"] += 1
            return
        try:
            decoded = decode_l2_message(msg.raw_l2_msg_hex)
        except Exception as exc:  # noqa: BLE001 -- честно фиксируем, не роняем весь прогон на одном сообщении
            decode_diag["decode_l2_message_raised"] += 1
            decode_diag[f"decode_l2_message_raised_detail_{type(exc).__name__}"] += 1
            return
        t1 = time.time()
        if not decoded:
            decode_diag["decode_empty_list"] += 1
        for entry in decoded:
            if "unhandled_msg_type" in entry:
                decode_diag[f"unhandled_msg_type_{entry['unhandled_msg_type']}"] += 1
            elif "decode_error" in entry:
                decode_diag["decode_error"] += 1
            elif "unrecognized_first_byte" in entry:
                decode_diag["unrecognized_first_byte"] += 1
            else:
                decode_diag["decoded_ok"] += 1

        calc_stats = do_calc_step(registry, decoded)
        t2 = time.time()

        tx = {"to": dummy_tx_to, "value": 0, "gas": 300000, "gasPrice": 1_000_000_000,
              "nonce": 0, "chainId": CHAIN_ID_MAINNET, "data": "0x"}
        Account.sign_transaction(tx, test_account.key)
        t3 = time.time()

        decode_ms, calc_ms, sign_ms = (t1 - t0) * 1000.0, (t2 - t1) * 1000.0, (t3 - t2) * 1000.0
        processing_total_ms = (t3 - t0) * 1000.0
        sample = {
            "sequence_number": msg.sequence_number,
            "n_subentries_decoded": len(decoded),
            "n_calc_checked": calc_stats["n_checked"],
            "n_calc_recomputed": calc_stats["n_recomputed"],
            "decode_ms": decode_ms,
            "calc_ms": calc_ms,
            "sign_ms": sign_ms,
            "processing_total_ms": processing_total_ms,
        }
        if len(_LIVE_STATE["raw_samples_kept"]) < max_raw_samples_kept:
            _LIVE_STATE["raw_samples_kept"].append(sample)
        # Владелец: "собери статистику по десяткам-сотням сообщений" -- полный список
        # СЫРЫХ сэмплов ограничен (--max-raw-samples-kept) ради размера вывода/лога,
        # НО статистика (median/p90) ниже считается по ВСЕМ реально обработанным
        # сообщениям через all_*_ms, не только по сохранённым сырым.
        current_slowest = _LIVE_STATE["slowest_message"]
        if current_slowest is None or processing_total_ms > current_slowest["processing_total_ms"]:
            _LIVE_STATE["slowest_message"] = sample
        _LIVE_STATE["all_decode_ms"].append(decode_ms)
        _LIVE_STATE["all_calc_ms"].append(calc_ms)
        _LIVE_STATE["all_sign_ms"].append(sign_ms)
        _LIVE_STATE["all_processing_total_ms"].append(processing_total_ms)

    client = SequencerFeedClient(SEQUENCER_FEED_URL_MAINNET)
    print(f"[latency_probe] подключение к {SEQUENCER_FEED_URL_MAINNET}, слушаем {duration_s:.0f}с "
          f"(ОДНО подключение -- cooldown-гард task5_bot_feed_client.py)...", file=sys.stderr)
    try:
        await asyncio.wait_for(client.listen(on_message), timeout=duration_s)
    except asyncio.TimeoutError:
        pass

    return {
        "n_messages_total_seen": _LIVE_STATE["n_messages_total_seen"],
        "feed_diag": client.diag,
        "decode_diag": dict(decode_diag),
        "test_wallet_address": test_account.address,
        "test_wallet_note": "свежесгенерированный одноразовый ключ, НЕ PRIVATE_KEY_NOX/PRIVATE_KEY_TASK5_BOT, "
                             "без фондирования, ни разу не использован для реальной отправки.",
        "stage_stats": _snapshot_live_state_as_stage_stats(),
        "slowest_message": _LIVE_STATE["slowest_message"],
        "raw_samples_kept": _LIVE_STATE["raw_samples_kept"],
        "n_raw_samples_kept": len(_LIVE_STATE["raw_samples_kept"]),
        "n_raw_samples_kept_cap": max_raw_samples_kept,
    }


def measure_invalid_nonce_submit_once() -> dict:
    """См. докстринг модуля, п.4 -- РОВНО одна отправка, гарантированно
    невалидный nonce, 0 реальных средств. Основной путь -- прямой
    write-only приёмник секвенсера; публичный RPC -- ТОЛЬКО фоллбэк при
    сетевой ошибке самого запроса (не при JSON-RPC error в ответе)."""
    test_account = Account.create()
    tx = {
        "to": to_checksum_address(WETH_USDG_POOL), "value": 0, "gas": 21000, "gasPrice": 1_000_000_000,
        "nonce": 10**9, "chainId": CHAIN_ID_MAINNET, "data": "0x",
    }
    signed = Account.sign_transaction(tx, test_account.key)
    raw_hex = signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex

    result: dict = {
        "test_wallet_address": test_account.address,
        "note": "nonce=10**9 -- гарантированно невалиден, транзакция НЕ будет исполнена, 0 реальных средств "
                "потрачено (тестовый кошелёк не фондирован). Измеряет ТОЛЬКО время до ответа узла на "
                "eth_sendRawTransaction, не время реального включения в блок.",
    }
    t0 = time.time()
    endpoint_used = "sequencer_submit"
    try:
        resp = requests.post(SEQUENCER_SUBMIT_URL_MAINNET,
                              json={"jsonrpc": "2.0", "method": "eth_sendRawTransaction", "params": [raw_hex], "id": 1},
                              timeout=15.0)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 -- честный фоллбэк на публичный RPC, см. докстринг
        result["sequencer_submit_network_error"] = str(exc)
        endpoint_used = "public_rpc_fallback"
        try:
            resp = requests.post(RPC_URL_MAINNET,
                                  json={"jsonrpc": "2.0", "method": "eth_sendRawTransaction", "params": [raw_hex], "id": 1},
                                  timeout=15.0)
            body = resp.json()
        except Exception as exc2:  # noqa: BLE001
            t1 = time.time()
            result.update({"endpoint_used": endpoint_used, "round_trip_ms": (t1 - t0) * 1000.0,
                            "public_rpc_fallback_network_error": str(exc2), "response": None})
            return result
    t1 = time.time()
    result.update({
        "endpoint_used": endpoint_used,
        "round_trip_ms": (t1 - t0) * 1000.0,
        "response": body,
    })
    return result


def main() -> None:
    # Владелец: печать должна доходить до лога СРАЗУ, не только при выходе
    # процесса -- критично для watchdog-сценария (os._exit() ниже НЕ делает
    # обычную interpreter-очистку/флаш буферов stdio).
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001 -- на некоторых окружениях reconfigure недоступен, не критично
        pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=90.0,
                     help="Сколько секунд слушать фид ЭТИМ единственным подключением.")
    ap.add_argument("--max-raw-samples-kept", type=int, default=60,
                     help="Сколько сырых по-сообщенческих сэмплов сохранить в вывод целиком (для беглой "
                          "проверки) -- статистика (median/p90) считается по ВСЕМ сообщениям, не только этим.")
    ap.add_argument("--out", type=str, default=None)
    ap.add_argument("--skip-invalid-nonce-submit", action="store_true",
                     help="Пропустить п.4 (реальную отправку невалидной транзакции) -- для повторных "
                          "прогонов, где доставка до секвенсера уже измерена и не нужно слать снова.")
    ap.add_argument("--watchdog-margin-s", type=float, default=300.0,
                     help="Жёсткий запас СВЕРХ --duration, на случай, если ОДНО сообщение фида (реальный "
                          "документированный случай -- батч 3.57МБ) декодируется/считается аномально долго "
                          "(ecrecover на каждую под-транзакцию) -- см. install_hard_watchdog().")
    args = ap.parse_args()

    watchdog_timer = install_hard_watchdog(args.duration + args.watchdog_margin_s + 60.0)  # +60с на bootstrap/submit
    print(f"[latency_probe] жёсткий watchdog установлен на {args.duration + args.watchdog_margin_s + 60.0:.0f}с "
          f"с этого момента (--duration={args.duration:.0f}с + --watchdog-margin-s={args.watchdog_margin_s:.0f}с "
          f"+ 60с на bootstrap/submit)", file=sys.stderr)

    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    print("[latency_probe] bootstrap: реальные slot0()/liquidity() известных пулов (ЕДИНСТВЕННЫЙ блок RPC "
          "ДО подключения к фиду)...", file=sys.stderr)
    registry, bootstrap_stats = bootstrap_known_pools_registry()
    print(f"[latency_probe] bootstrap: {bootstrap_stats['n_ok']}/{bootstrap_stats['n_pools']} пулов с реальной "
          f"ценой, {bootstrap_stats['n_error']} ошибок", file=sys.stderr)
    result["bootstrap_stats"] = bootstrap_stats

    result["full_path_probe"] = asyncio.run(run_probe(args.duration, registry, args.max_raw_samples_kept))

    if args.skip_invalid_nonce_submit:
        result["invalid_nonce_submit"] = {"skipped": True}
    else:
        print("[latency_probe] п.4: ОДНА заведомо-невалидная (nonce=10**9) транзакция -> "
              f"{SEQUENCER_SUBMIT_URL_MAINNET} ...", file=sys.stderr)
        result["invalid_nonce_submit"] = measure_invalid_nonce_submit_once()

    watchdog_timer.cancel()
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text)


if __name__ == "__main__":
    main()
