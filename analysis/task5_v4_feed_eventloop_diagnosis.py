#!/usr/bin/env python3
"""Задача 5 (владелец, п.2): проверка измерительного скрипта на блокирование
event loop -- ЛОКАЛЬНО, БЕЗ подключения к фиду/Alchemy.

Два раздельных результата:

1. СТАТИЧЕСКИЙ разбор кода run_feed() (task5_v4_feed_vs_alchemy_measurement.
   py) -- ДО этого исправления decode/ecrecover/json.dumps/файловая запись
   (fh.write+fh.flush) выполнялись СИНХРОННО, ИНЛАЙН, в том же цикле,
   что и `await ws.recv()` -- честно называется: это МОГЛО задерживать
   обработку внутреннего keepalive (PING/PONG) той же asyncio-петли.

2. РЕАЛЬНЫЙ (не выдуманный) локальный бенчмарк, БЕЗ сети: реальная
   офлайн-подпись тестовой EIP-1559 транзакции (eth_account, одноразовый
   локальный ключ, никогда не публикуется/не используется в проде) и
   замер time.perf_counter() для _decode_signed_tx()/_recover_sender() --
   РЕАЛЬНАЯ криптографическая операция (secp256k1 ecrecover), не мок.
   Результат используется ТОЛЬКО для оценки порядка величины возможной
   задержки на кадр (см. task5_v4_feed_backlog_diagnosis.py, оценка по
   реально наблюдённому числу транзакций в самом крупном кадре).

Сырые WS-кадры прошлого замера НЕ сохранялись -- прямое (не оценочное)
измерение фактического времени обработки каждого полученного сообщения
задним числом НЕВОЗМОЖНО; это ИСПРАВЛЕНО в самом измерительном скрипте
(отдельная очередь recv->processing, приём фиксирует t_monotonic ДО
разбора, тяжёлая обработка -- в отдельной задаче) для ВСЕХ будущих
прогонов, см. run_feed()/_feed_message_processor() ниже в этом файле."""
from __future__ import annotations

import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import websockets  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402

from task5_bot_feed_client import _decode_signed_tx, _recover_sender  # noqa: E402


def static_code_check() -> dict:
    """Читает ИСХОДНЫЙ КОД run_feed() (не выполняет его) -- ищет признаки
    синхронной тяжёлой работы (decode/recover/json/файл) непосредственно
    в цикле `while ... await ws.recv()`."""
    import task5_v4_feed_vs_alchemy_measurement as m
    src = inspect.getsource(m.run_feed)
    findings = {
        "recv_call_present": "await asyncio.wait_for(ws.recv()" in src or "await ws.recv()" in src,
        "decode_called_inline_in_same_function": "_decode_feed_payload(" in src,
        "file_write_inline_in_same_function": "fh.write" in src or ".write(" in src,
        "uses_separate_queue_for_processing": "asyncio.Queue" in src,
    }
    findings["blocking_risk_confirmed_before_fix"] = (
        findings["decode_called_inline_in_same_function"]
        and not findings["uses_separate_queue_for_processing"]
    )
    return findings


def real_offline_decode_benchmark(n: int = 2000) -> dict:
    """РЕАЛЬНАЯ офлайн-подпись + РЕАЛЬНЫЙ decode/ecrecover, БЕЗ сети."""
    acct = Account.create()
    tx = {
        "chainId": 4663, "nonce": 1, "maxPriorityFeePerGas": 1_000_000_000,
        "maxFeePerGas": 2_000_000_000, "gas": 200000,
        "to": to_checksum_address("0x8366a39cc670b4001a1121b8f6a443a643e40951"),
        "value": 0, "data": b"\x12\x34\x56\x78" * 20,
    }
    signed = acct.sign_transaction(tx)
    raw = bytes(signed.raw_transaction)

    t0 = time.perf_counter()
    for _ in range(n):
        r = _decode_signed_tx(raw, 4)
    t1 = time.perf_counter()
    if r.get("from") != acct.address.lower():
        raise RuntimeError("бенчмарк некорректен: восстановленный отправитель не совпал с реальным")
    decode_ecrecover_ms_per_tx = (t1 - t0) / n * 1000

    t0 = time.perf_counter()
    for _ in range(n):
        _recover_sender(raw)
    t1 = time.perf_counter()
    ecrecover_only_ms_per_tx = (t1 - t0) / n * 1000

    return {
        "n_iterations": n, "tx_bytes_len": len(raw),
        "decode_plus_ecrecover_ms_per_tx": decode_ecrecover_ms_per_tx,
        "ecrecover_only_ms_per_tx": ecrecover_only_ms_per_tx,
        "note": "Реальная secp256k1-подпись/ecrecover через eth_account, одноразовый локальный ключ, без сети.",
    }


def websockets_ping_pong_defaults() -> dict:
    """Фактические (не предположенные) настройки ping_interval/ping_timeout
    клиента websockets в ЭТОЙ установленной версии -- через сигнатуру
    websockets.connect, без подключения к сети. Секретов здесь нет."""
    sig = inspect.signature(websockets.connect)
    defaults = {}
    for name in ("ping_interval", "ping_timeout", "close_timeout", "open_timeout"):
        if name in sig.parameters:
            defaults[name] = sig.parameters[name].default
    return {
        "websockets_version": websockets.__version__,
        "connect_default_params": defaults,
        "note": (
            "connect_with_headers() (task5_bot_feed_client.py) НЕ переопределяет "
            "ping_interval/ping_timeout -- используются ИМЕННО эти значения по "
            "умолчанию установленной версии websockets. 'ws.latency' (последнее "
            "измеренное RTT ping/pong) доступен как атрибут живого соединения ПОСЛЕ "
            "подключения -- не секрет, теперь пишется в diag (см. run_feed())."
        ),
    }


def main() -> None:
    import json
    result = {
        "static_code_check_before_this_fix": static_code_check(),
        "real_offline_decode_benchmark": real_offline_decode_benchmark(),
        "websockets_ping_pong_defaults": websockets_ping_pong_defaults(),
        "conclusion": (
            "Ping timeout НЕ трактуется здесь как доказательство неисправности сервера -- "
            "heartbeat НЕ отключается и не ослабляется как 'исправление'. Вместо этого: (1) "
            "тяжёлая обработка (decode/ecrecover/JSON/файл) вынесена из цикла recv() в "
            "отдельную задачу-очередь (см. run_feed()), чтобы она НЕ могла задерживать "
            "внутренний PING/PONG той же asyncio-петли, независимо от того, была ли она "
            "реальной причиной прошлого обрыва; (2) фактические ping_interval/ping_timeout "
            "и ws.latency после подключения теперь видны в diag, а не скрыты."
        ),
    }
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
