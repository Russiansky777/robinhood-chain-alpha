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
import statistics
import sys
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
    per_message: list[dict] = []
    decode_diag: Counter = Counter()
    n_messages_total_seen = 0
    prev_t_wall: float | None = None
    detect_wait_samples: list[float] = []

    dummy_tx_to = to_checksum_address(WETH_USDG_POOL)

    def on_message(msg: FeedMessage) -> None:
        nonlocal prev_t_wall, n_messages_total_seen
        n_messages_total_seen += 1
        t0 = msg.t_wall
        if prev_t_wall is not None:
            detect_wait_samples.append((t0 - prev_t_wall) * 1000.0)
        prev_t_wall = t0

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

        if len(per_message) < max_raw_samples_kept:
            per_message.append({
                "sequence_number": msg.sequence_number,
                "n_subentries_decoded": len(decoded),
                "n_calc_checked": calc_stats["n_checked"],
                "n_calc_recomputed": calc_stats["n_recomputed"],
                "decode_ms": (t1 - t0) * 1000.0,
                "calc_ms": (t2 - t1) * 1000.0,
                "sign_ms": (t3 - t2) * 1000.0,
                "processing_total_ms": (t3 - t0) * 1000.0,
            })
        else:
            # Владелец: "собери статистику по десяткам-сотням сообщений" -- полный
            # список сырых сэмплов ограничен (--max-raw-samples-kept) ради размера
            # вывода/лога, НО статистика (median/p90) ниже считается по ВСЕМ
            # реально обработанным сообщениям, не только по сохранённым сырым --
            # см. all_decode_ms/all_calc_ms/all_sign_ms.
            pass
        _all_decode_ms.append((t1 - t0) * 1000.0)
        _all_calc_ms.append((t2 - t1) * 1000.0)
        _all_sign_ms.append((t3 - t2) * 1000.0)
        _all_processing_total_ms.append((t3 - t0) * 1000.0)

    _all_decode_ms: list[float] = []
    _all_calc_ms: list[float] = []
    _all_sign_ms: list[float] = []
    _all_processing_total_ms: list[float] = []

    client = SequencerFeedClient(SEQUENCER_FEED_URL_MAINNET)
    print(f"[latency_probe] подключение к {SEQUENCER_FEED_URL_MAINNET}, слушаем {duration_s:.0f}с "
          f"(ОДНО подключение -- cooldown-гард task5_bot_feed_client.py)...", file=sys.stderr)
    try:
        await asyncio.wait_for(client.listen(on_message), timeout=duration_s)
    except asyncio.TimeoutError:
        pass

    def stats(samples: list[float]) -> dict | None:
        if not samples:
            return None
        sorted_s = sorted(samples)
        return {
            "n": len(samples),
            "median_ms": statistics.median(samples),
            "p90_ms": (statistics.quantiles(samples, n=10)[8] if len(samples) >= 10 else max(samples)),
            "min_ms": min(samples),
            "max_ms": max(samples),
            "mean_ms": statistics.mean(samples),
        }

    full_total_with_detect = [d + p for d, p in zip(detect_wait_samples, _all_processing_total_ms[1:])] \
        if len(detect_wait_samples) == len(_all_processing_total_ms) - 1 else None

    return {
        "n_messages_total_seen": n_messages_total_seen,
        "feed_diag": client.diag,
        "decode_diag": dict(decode_diag),
        "test_wallet_address": test_account.address,
        "test_wallet_note": "свежесгенерированный одноразовый ключ, НЕ PRIVATE_KEY_NOX/PRIVATE_KEY_TASK5_BOT, "
                             "без фондирования, ни разу не использован для реальной отправки.",
        "stage_stats": {
            "detect_wait_ms": stats(detect_wait_samples),
            "decode_ms": stats(_all_decode_ms),
            "calc_ms": stats(_all_calc_ms),
            "sign_ms": stats(_all_sign_ms),
            "processing_total_ms_decode_calc_sign": stats(_all_processing_total_ms),
            "full_total_with_detect_wait_ms": stats(full_total_with_detect) if full_total_with_detect else None,
        },
        "raw_samples_kept": per_message,
        "n_raw_samples_kept": len(per_message),
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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=180.0,
                     help="Сколько секунд слушать фид ЭТИМ единственным подключением.")
    ap.add_argument("--max-raw-samples-kept", type=int, default=60,
                     help="Сколько сырых по-сообщенческих сэмплов сохранить в вывод целиком (для беглой "
                          "проверки) -- статистика (median/p90) считается по ВСЕМ сообщениям, не только этим.")
    ap.add_argument("--out", type=str, default=None)
    ap.add_argument("--skip-invalid-nonce-submit", action="store_true",
                     help="Пропустить п.4 (реальную отправку невалидной транзакции) -- для повторных "
                          "прогонов, где доставка до секвенсера уже измерена и не нужно слать снова.")
    args = ap.parse_args()

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

    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text)


if __name__ == "__main__":
    main()
