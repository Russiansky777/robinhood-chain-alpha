#!/usr/bin/env python3
"""Задача 5, живой бот -- честный замер ДОСТАВКИ до секвенсера на n=30-50,
не n=1. Владелец (заказчик), 2026-09-17 (дословно, сокращённо):

"Вывод 'блок+1 недостижим' стоит на ОДНОМ замере доставки (356мс, n=1).
При n=1 нельзя отличить настоящую медиану от разового выброса: холодное
TCP-соединение, первый TLS-хендшейк, случайный edge Cloudflare -- любое
из этого даёт сотни мс однократно и десятки при переиспользовании
соединения. Замерить доставку нормально, 30-50 отправок, невалидные
транзакции (неверный nonce), не исполняются, денег не стоят."

Требования владельца, все реализованы буквально:
1. Одно keep-alive HTTP-соединение (не новое на каждую отправку).
   Первая отправка -- отдельно (холодный старт).
2. Время от начала отправки до ответа узла, РАЗБИТОЕ на DNS / TCP-connect
   / TLS-handshake / собственно запрос (TTFB после отправки).
3. Интервал между отправками 1-2с (не подряд).
4. Медиана, p50, p90, минимум, максимум + отдельно первая отправка.

Метод инструментовки (честно, без "requests"/urllib3, которые не дают
прямого доступа к DNS/TCP/TLS-разбивке по одному запросу): подкласс
`http.client.HTTPSConnection` с переопределённым `connect()` -- ТОТ ЖЕ
код, что и в стандартной библиотеке (`_create_connection` затем
`_context.wrap_socket`), только с таймерами вокруг каждого шага. ОДИН
экземпляр соединения используется для ВСЕХ отправок серии -- если
сервер реально держит keep-alive, `connect()` вызывается только один
раз (на первой отправке), и `tcp_connect_ms`/`tls_handshake_ms` будут
`None` у всех последующих (это и есть признак реального переиспользования
соединения, не совпадение с TTFB). Если соединение всё же обрывается
сервером между отправками -- ловим `http.client` ошибку, помечаем
`reconnected: true` у этой попытки и открываем новое (стандартный
клиентский путь для keep-alive, не баг).

Альтернативный (не-Cloudflare) эндпоинт -- УЖЕ проверен и УЖЕ
используется. `sequencer.mainnet.chain.robinhood.com` (см.
docs/PROJECT_STATE.md, "Путь записи транзакции -- РЕАЛЬНАЯ, важная
находка: отдельный sequencer-эндпоинт СУЩЕСТВУЕТ, не за Cloudflare")
реально резолвится в AWS EC2 us-east-2 (AS16509 Amazon.com, Columbus
OH), НЕ Cloudflare (публичный RPC/фид -- AS13335 Cloudflare, anycast) --
это ТОТ ЖЕ эндпоинт, что уже используется здесь и в предыдущем прогоне
(`task5_full_path_latency_probe.py`), не Cloudflare-фронт. Других
Robinhood-эндпоинтов приёма транзакций в открытых источниках не найдено
(WebSearch, тот же прошлый раунд: docs/Alchemy/Chainstack/dwellir --
никто не предлагает отдельный путь мимо этого). Этот скрипт
дополнительно и ДЁШЕВО подтверждает ASN/ip резолва host в СВОЁМ выводе
(`endpoint_asn_check`), не полагаясь только на паспорт."""
from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import statistics
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests
from eth_account import Account
from eth_utils import to_checksum_address

from task5_bot_config import CHAIN_ID_MAINNET, SEQUENCER_SUBMIT_URL_MAINNET, WETH_USDG_POOL

N_SENDS = int(os.environ.get("DELIVERY_LATENCY_N", "40"))  # в диапазоне 30-50, запрошенном владельцем
INTERVAL_S_MIN = 1.0
INTERVAL_S_MAX = 2.0
REQUEST_TIMEOUT_S = 15.0

_URL = urllib.parse.urlparse(SEQUENCER_SUBMIT_URL_MAINNET)
HOST = _URL.hostname
PORT = _URL.port or (443 if _URL.scheme == "https" else 80)
PATH = _URL.path or "/"


class TimedHTTPSConnection(http.client.HTTPSConnection):
    """Тот же connect(), что в стандартной библиотеке (см. cpython
    http/client.py HTTPSConnection.connect), только с таймерами вокруг
    TCP-connect и TLS-handshake по отдельности -- НЕ переизобретаем
    протокол, просто измеряем существующие шаги."""

    last_tcp_connect_ms: float | None = None
    last_tls_handshake_ms: float | None = None
    n_connects: int = 0

    def connect(self) -> None:  # noqa: D102 -- совпадает по контракту с базовым классом
        t_tcp0 = time.perf_counter()
        self.sock = self._create_connection((self.host, self.port), self.timeout, self.source_address)
        t_tcp1 = time.perf_counter()
        if getattr(self, "_tunnel_host", None):
            self._tunnel()
        t_tls0 = time.perf_counter()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        t_tls1 = time.perf_counter()
        self.last_tcp_connect_ms = (t_tcp1 - t_tcp0) * 1000.0
        self.last_tls_handshake_ms = (t_tls1 - t_tls0) * 1000.0
        self.n_connects += 1


def dns_resolve_once(host: str) -> dict:
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, PORT, proto=socket.IPPROTO_TCP)
        t1 = time.perf_counter()
        ips = sorted({info[4][0] for info in infos})
        return {"dns_ms": (t1 - t0) * 1000.0, "resolved_ips": ips, "error": None}
    except Exception as exc:  # noqa: BLE001
        t1 = time.perf_counter()
        return {"dns_ms": (t1 - t0) * 1000.0, "resolved_ips": [], "error": str(exc)}


def endpoint_asn_check(ips: list[str]) -> dict:
    """Дёшево подтвердить ASN резолва (см. докстринг модуля) -- публичный
    ipinfo.io/<ip>/json без ключа, лимит бесплатного тира достаточен для
    1-2 IP. Не критично для основного замера -- ошибка здесь не должна
    ронять прогон."""
    out = []
    for ip in ips[:2]:
        try:
            resp = requests.get(f"https://ipinfo.io/{ip}/json", timeout=8.0)
            body = resp.json() if resp.status_code == 200 else {"http_status": resp.status_code}
        except Exception as exc:  # noqa: BLE001
            body = {"error": str(exc)}
        out.append({"ip": ip, "info": body})
    return {"checked": out}


def build_invalid_tx() -> tuple[str, bytes]:
    """Свежий одноразовый EOA (Account.create(), НЕ PRIVATE_KEY_NOX/
    PRIVATE_KEY_TASK5_BOT) на КАЖДУЮ отправку -- избегает дублирования
    tx_hash/nonce между попытками, тот же приём, что в предыдущем
    прогоне. nonce=10**9 гарантированно невалиден, 0 реальных средств
    (кошелёк никогда не фондирован)."""
    acct = Account.create()
    tx = {
        "to": to_checksum_address(WETH_USDG_POOL), "value": 0, "gas": 21000, "gasPrice": 1_000_000_000,
        "nonce": 10**9, "chainId": CHAIN_ID_MAINNET, "data": "0x",
    }
    signed = Account.sign_transaction(tx, acct.key)
    raw_hex = signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex
    return acct.address, raw_hex


def send_one(conn: TimedHTTPSConnection, req_id: int) -> dict:
    address, raw_hex = build_invalid_tx()
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": "eth_sendRawTransaction", "params": [raw_hex], "id": req_id}
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
        "Connection": "keep-alive",
    }

    was_connected_before = conn.sock is not None
    reconnected = False
    t_send0 = time.perf_counter()
    try:
        conn.request("POST", PATH, body=payload, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        t_done = time.perf_counter()
        status = resp.status
        try:
            body_json = json.loads(body.decode(errors="replace"))
        except Exception:  # noqa: BLE001
            body_json = None
        result = {
            "req_id": req_id,
            "test_wallet_address": address,
            "round_trip_ms": (t_done - t_send0) * 1000.0,
            "http_status": status,
            "response_json": body_json,
            "response_raw_head": body[:300].decode(errors="replace") if body_json is None else None,
        }
    except Exception as exc:  # noqa: BLE001 -- соединение оборвано сервером/сетью -- честно переоткрываем
        t_fail = time.perf_counter()
        reconnected = True
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        conn.sock = None
        t_send0b = time.perf_counter()
        try:
            conn.request("POST", PATH, body=payload, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            t_done = time.perf_counter()
            status = resp.status
            try:
                body_json = json.loads(body.decode(errors="replace"))
            except Exception:  # noqa: BLE001
                body_json = None
            result = {
                "req_id": req_id,
                "test_wallet_address": address,
                "round_trip_ms": (t_done - t_send0) * 1000.0,  # честно включает время сбоя+переоткрытия
                "round_trip_ms_after_reconnect_only": (t_done - t_send0b) * 1000.0,
                "http_status": status,
                "response_json": body_json,
                "response_raw_head": body[:300].decode(errors="replace") if body_json is None else None,
                "first_attempt_error": str(exc),
                "first_attempt_failed_after_ms": (t_fail - t_send0) * 1000.0,
            }
        except Exception as exc2:  # noqa: BLE001
            t_done = time.perf_counter()
            result = {
                "req_id": req_id,
                "test_wallet_address": address,
                "round_trip_ms": None,
                "error": f"первая попытка: {exc}; повтор после переоткрытия: {exc2}",
            }

    result["was_reused_connection"] = was_connected_before and not reconnected
    result["reconnected"] = reconnected
    result["tcp_connect_ms"] = conn.last_tcp_connect_ms if (not was_connected_before or reconnected) else None
    result["tls_handshake_ms"] = conn.last_tls_handshake_ms if (not was_connected_before or reconnected) else None
    result["n_connects_so_far"] = conn.n_connects
    return result


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    sorted_vals = sorted(values)
    return {
        "n": len(values),
        "median_ms": statistics.median(values),
        "p50_ms": statistics.median(values),
        "p90_ms": sorted_vals[max(0, int(round(0.9 * (len(sorted_vals) - 1))))],
        "min_ms": min(values),
        "max_ms": max(values),
        "mean_ms": statistics.fmean(values),
    }


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    print(f"[delivery_latency] host={HOST} port={PORT} path={PATH} n_sends={N_SENDS}", file=sys.stderr)

    dns_info = dns_resolve_once(HOST)
    print(f"[delivery_latency] DNS: {dns_info}", file=sys.stderr)
    asn_info = endpoint_asn_check(dns_info.get("resolved_ips", []))
    print(f"[delivery_latency] ASN check: {asn_info}", file=sys.stderr)

    ctx = ssl.create_default_context()
    conn = TimedHTTPSConnection(HOST, PORT, timeout=REQUEST_TIMEOUT_S, context=ctx)

    attempts: list[dict] = []
    for i in range(N_SENDS):
        req_id = i + 1
        res = send_one(conn, req_id)
        attempts.append(res)
        tag = "COLD" if req_id == 1 else ("RECONNECT" if res.get("reconnected") else "warm")
        print(
            f"[delivery_latency] #{req_id}/{N_SENDS} [{tag}] round_trip_ms="
            f"{res.get('round_trip_ms')} status={res.get('http_status')} "
            f"tcp={res.get('tcp_connect_ms')} tls={res.get('tls_handshake_ms')}",
            file=sys.stderr,
        )
        if req_id < N_SENDS:
            sleep_s = INTERVAL_S_MIN + (INTERVAL_S_MAX - INTERVAL_S_MIN) * ((i * 0.61803398875) % 1.0)
            time.sleep(sleep_s)

    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass

    ok_attempts = [a for a in attempts if a.get("round_trip_ms") is not None]
    first_attempt = attempts[0] if attempts else None
    warm_only = [a for a in ok_attempts[1:] if a.get("was_reused_connection")]
    reconnect_events = [a for a in attempts if a.get("reconnected")]

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": SEQUENCER_SUBMIT_URL_MAINNET,
        "endpoint_note": "Уже НЕ Cloudflare -- см. докстринг модуля и docs/PROJECT_STATE.md "
                          "('sequencer.mainnet.chain.robinhood.com' -- AWS EC2 us-east-2, AS16509, "
                          "подтверждено ранее; другой альтернативы в открытых источниках не найдено).",
        "dns_resolution": dns_info,
        "endpoint_asn_check": asn_info,
        "n_sends_requested": N_SENDS,
        "n_sends_completed": len(attempts),
        "n_ok": len(ok_attempts),
        "n_errors": len(attempts) - len(ok_attempts),
        "n_reconnect_events_after_first": len(reconnect_events) - (1 if reconnect_events and reconnect_events[0]["req_id"] == 1 else 0),
        "first_attempt": first_attempt,
        "stats_all_ok": summarize([a["round_trip_ms"] for a in ok_attempts]),
        "stats_excluding_first": summarize([a["round_trip_ms"] for a in ok_attempts[1:]]),
        "stats_warm_reused_connection_only": summarize([a["round_trip_ms"] for a in warm_only]),
        "n_warm_reused_connection_only": len(warm_only),
        "all_attempts": attempts,
    }

    out_path = Path(__file__).resolve().parent.parent / "data" / "task5_delivery_latency_n50_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[delivery_latency] написано {out_path}", file=sys.stderr)
    print(json.dumps({
        "stats_all_ok": result["stats_all_ok"],
        "stats_excluding_first": result["stats_excluding_first"],
        "stats_warm_reused_connection_only": result["stats_warm_reused_connection_only"],
        "n_warm_reused_connection_only": result["n_warm_reused_connection_only"],
        "n_reconnect_events_after_first": result["n_reconnect_events_after_first"],
        "first_attempt_round_trip_ms": (first_attempt or {}).get("round_trip_ms"),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
