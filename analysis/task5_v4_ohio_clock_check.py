#!/usr/bin/env python3
"""Задача 5 (владелец, п.1): проверка корректности системных часов Ohio --
БЕЗ подключения к официальному фиду. Обычный HTTPS POST к уже
используемому RPC-провайдеру (тот же путь, что и остальной анализ этого
проекта), сравнение локального time.time() с HTTP 'Date' заголовком
ответа. Секретов (ключ в URL) в вывод не попадает."""
from __future__ import annotations

import json
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

from config import CONFIG  # noqa: E402


def _redact(url: str) -> str:
    if not url:
        return url
    parts = url.rsplit("/", 1)
    if len(parts) == 2 and len(parts[1]) > 8:
        return parts[0] + "/<redacted>"
    return url


def main() -> None:
    url = CONFIG.alchemy_rpc_url or CONFIG.public_rpc_url
    result: dict = {"url_redacted": _redact(url)}
    if not url:
        result["error"] = "нет ни alchemy_rpc_url, ни public_rpc_url в CONFIG"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    try:
        t_before = time.time()
        resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                              timeout=10)
        t_after = time.time()
        date_header = resp.headers.get("Date")
        server_epoch = parsedate_to_datetime(date_header).timestamp() if date_header else None
        result.update({
            "ohio_time_before_request_unix": t_before,
            "ohio_time_after_request_unix": t_after,
            "round_trip_s": t_after - t_before,
            "http_date_header": date_header,
            "http_date_header_epoch": server_epoch,
            "ohio_clock_offset_vs_server_date_header_s": (
                (t_before - server_epoch) if server_epoch is not None else None),
            "caveat": (
                "Секундное разрешение Date-заголовка + половина RTT -- НЕ эталонная "
                "NTP-сверка, но достаточно, чтобы исключить ГРУБУЮ (десятки секунд и "
                "больше) рассинхронизацию часов Ohio. Положительное значение offset -- "
                "часы Ohio ВПЕРЕДИ сервера, отрицательное -- ПОЗАДИ."
            ),
        })
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
