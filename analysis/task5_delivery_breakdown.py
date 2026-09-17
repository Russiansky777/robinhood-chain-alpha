#!/usr/bin/env python3
"""Задача 5, живой бот -- разложить уже измеренные 237мс тёплой доставки
(`task5_delivery_latency_n50.py`) на сеть / TLS / HTTP-накладные / саму
обработку транзакции секвенсером -- чтобы понять, сколько реально даст
переезд бота в тот же регион AWS (us-east-2, где резолвится
sequencer.mainnet.chain.robinhood.com -- см. docs/PROJECT_STATE.md).

Владелец (заказчик), 2026-09-17, дословно (сокращённо): "237мс -- это
сеть плюс обработка на их стороне, пропорция неизвестна. Если задержка
в основном сетевая -- переезд даёт почти всё; если серверная -- почти
ничего. Разложить: (1) чистый TCP-RTT (SYN->SYN-ACK, без прикладной
обработки), (2) пустой/невалидный HTTP-запрос (без валидации
транзакции) -- разница с (1) = накладные HTTP/TLS, (3) полная
eth_sendRawTransaction (уже есть, 237мс) -- разница с (2) = обработка
транзакции. Плюс traceroute/mtr -- через какие города идёт маршрут."

Честная методологическая поправка (важно, отличается от буквальной
формулировки задания): предыдущий замер (`task5_delivery_latency_n50.py`)
показал, что 237мс -- это ТЁПЛОЕ (переиспользованное) соединение, TCP+TLS
там НЕ происходят на каждый запрос (только на первом, отдельно измерены
как 102.4мс+95.4мс). Поэтому здесь даются ДВЕ параллельные раскладки:
(a) "холодная лестница" буквально по заданию -- TCP-RTT / TCP+TLS /
TCP+TLS+невалидный-метод / TCP+TLS+реальная-tx (все с НОВЫМ соединением
на каждую попытку, честно показывает разовую цену подключения);
(b) "тёплая раскладка" -- операционно более релевantна для бота с
постоянным соединением: TCP+TLS оплачиваются ОДИН раз при старте, дальше
сравнивается тёплый невалидный-метод (сетевой RTT + минимальная
обработка ошибки) против уже известного тёплого реального eth_sendRaw-
Transaction (237.3мс) -- разница = обработка ИМЕННО транзакции
(RLP-декод, revert-логика, nonce/balance-проверка).

Реальных транзакций НЕ отправляется -- только TCP/TLS-хендшейки и
заведомо невалидные JSON-RPC методы (сервер отвечает "method not found"
без разбора тела transaction)."""
from __future__ import annotations

import json
import socket
import ssl
import statistics
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import SEQUENCER_SUBMIT_URL_MAINNET

_URL = urllib.parse.urlparse(SEQUENCER_SUBMIT_URL_MAINNET)
HOST = _URL.hostname
PORT = _URL.port or 443
PATH = _URL.path or "/"

N_TCP_ONLY = 25
N_TCP_TLS = 25
N_COLD_INVALID = 10
N_WARM_INVALID = 25
CONNECT_TIMEOUT_S = 10.0
INTERVAL_BETWEEN_ATTEMPTS_S = 0.3  # честно не подряд -- избегаем нагрузки/rate-limit, не требование владельца буквально, но тот же принцип, что в n50-замере


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    sv = sorted(values)
    return {
        "n": len(values), "median_ms": statistics.median(values),
        "p90_ms": sv[max(0, int(round(0.9 * (len(sv) - 1))))],
        "min_ms": min(values), "max_ms": max(values), "mean_ms": statistics.fmean(values),
    }


def resolve_ip() -> str:
    return socket.getaddrinfo(HOST, PORT, proto=socket.IPPROTO_TCP)[0][4][0]


def measure_tcp_handshake_only(ip: str, n: int) -> list[float]:
    """Голый TCP connect() -- SYN/SYN-ACK/ACK, БЕЗ TLS, БЕЗ HTTP. Это и
    есть 'чистая сеть' в терминах задания -- ближе всего к физическому
    RTT до хоста, без какой-либо прикладной обработки."""
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            sock = socket.create_connection((ip, PORT), timeout=CONNECT_TIMEOUT_S)
            t1 = time.perf_counter()
            sock.close()
            times.append((t1 - t0) * 1000.0)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(INTERVAL_BETWEEN_ATTEMPTS_S)
    return times


def measure_tcp_tls_handshake(ip: str, n: int) -> tuple[list[float], list[float], list[float]]:
    """TCP connect() + TLS handshake, каждый раз НОВОЕ соединение --
    возвращает (tcp_only_ms, tls_only_ms, total_ms) по каждой попытке."""
    ctx = ssl.create_default_context()
    tcp_times, tls_times, total_times = [], [], []
    for _ in range(n):
        try:
            t0 = time.perf_counter()
            sock = socket.create_connection((ip, PORT), timeout=CONNECT_TIMEOUT_S)
            t1 = time.perf_counter()
            ssock = ctx.wrap_socket(sock, server_hostname=HOST)
            t2 = time.perf_counter()
            ssock.close()
            tcp_times.append((t1 - t0) * 1000.0)
            tls_times.append((t2 - t1) * 1000.0)
            total_times.append((t2 - t0) * 1000.0)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(INTERVAL_BETWEEN_ATTEMPTS_S)
    return tcp_times, tls_times, total_times


def _send_invalid_method_over_socket(ssock: ssl.SSLSocket) -> tuple[bool, dict | None]:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_thisMethodDoesNotExist987", "params": []}).encode()
    req = (
        f"POST {PATH} HTTP/1.1\r\n"
        f"Host: {HOST}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
    ).encode() + payload
    ssock.sendall(req)
    ssock.settimeout(CONNECT_TIMEOUT_S)
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = ssock.recv(4096)
        if not chunk:
            return False, None
        resp += chunk
    header_part, _, body_part = resp.partition(b"\r\n\r\n")
    header_text = header_part.decode(errors="replace")
    content_length = None
    for line in header_text.split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip().lower() == "content-length":
                content_length = int(v.strip())
    body = body_part
    if content_length is not None:
        while len(body) < content_length:
            chunk = ssock.recv(4096)
            if not chunk:
                break
            body += chunk
    try:
        return True, json.loads(body.decode(errors="replace"))
    except Exception:  # noqa: BLE001
        return True, None


def measure_cold_invalid_method(ip: str, n: int) -> list[float]:
    """Каждая попытка -- НОВОЕ TCP+TLS соединение + один невалидный
    JSON-RPC метод. Сервер отвечает 'method not found' без разбора тела
    транзакции -- изолирует TCP+TLS+минимальный HTTP от cost'а именно
    RLP-декодирования/валидации транзакции."""
    ctx = ssl.create_default_context()
    times = []
    for _ in range(n):
        try:
            t0 = time.perf_counter()
            sock = socket.create_connection((ip, PORT), timeout=CONNECT_TIMEOUT_S)
            ssock = ctx.wrap_socket(sock, server_hostname=HOST)
            ok, _ = _send_invalid_method_over_socket(ssock)
            t1 = time.perf_counter()
            ssock.close()
            if ok:
                times.append((t1 - t0) * 1000.0)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(INTERVAL_BETWEEN_ATTEMPTS_S)
    return times


def measure_warm_invalid_method(ip: str, n: int) -> list[float]:
    """ОДНО TCP+TLS соединение, переиспользуемое на все `n` запросов
    (тот же принцип, что `task5_delivery_latency_n50.py`) -- первая
    попытка (холодная, включает handshake) исключена из статистики,
    как и там."""
    ctx = ssl.create_default_context()
    sock = socket.create_connection((ip, PORT), timeout=CONNECT_TIMEOUT_S)
    ssock = ctx.wrap_socket(sock, server_hostname=HOST)
    times = []
    for i in range(n):
        t0 = time.perf_counter()
        try:
            ok, _ = _send_invalid_method_over_socket(ssock)
            t1 = time.perf_counter()
            if ok:
                times.append((t1 - t0) * 1000.0)
        except Exception:  # noqa: BLE001
            break
        time.sleep(INTERVAL_BETWEEN_ATTEMPTS_S)
    ssock.close()
    return times


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    ip = resolve_ip()
    print(f"[delivery_breakdown] host={HOST} ip={ip} port={PORT}", file=sys.stderr)

    print(f"[delivery_breakdown] 1/4: TCP-only handshake x{N_TCP_ONLY}...", file=sys.stderr)
    tcp_only = measure_tcp_handshake_only(ip, N_TCP_ONLY)

    print(f"[delivery_breakdown] 2/4: TCP+TLS handshake x{N_TCP_TLS}...", file=sys.stderr)
    tcp_component, tls_component, tcp_tls_total = measure_tcp_tls_handshake(ip, N_TCP_TLS)

    print(f"[delivery_breakdown] 3/4: холодный невалидный-метод x{N_COLD_INVALID}...", file=sys.stderr)
    cold_invalid = measure_cold_invalid_method(ip, N_COLD_INVALID)

    print(f"[delivery_breakdown] 4/4: тёплый невалидный-метод x{N_WARM_INVALID} (1 соединение)...", file=sys.stderr)
    warm_invalid = measure_warm_invalid_method(ip, N_WARM_INVALID)

    # Реальные ранее измеренные числа (task5_delivery_latency_n50.py, n=40,
    # data/task5_delivery_latency_n50_result.json) -- НЕ повторяем реальную
    # отправку eth_sendRawTransaction заново (экономия сети/времени,
    # владелец: "не выдумывай данные" == "не дублируй, если уже честно
    # измерено"), просто читаем готовый результат как референс.
    n50_path = Path(__file__).resolve().parent.parent / "data" / "task5_delivery_latency_n50_result.json"
    n50_data = json.loads(n50_path.read_text()) if n50_path.exists() else None
    warm_real_tx_median_ms = n50_data["stats_warm_reused_connection_only"]["median_ms"] if n50_data else None
    cold_real_tx_first_ms = n50_data["first_attempt"]["round_trip_ms"] if n50_data else None
    cold_real_tx_tcp_ms = n50_data["first_attempt"].get("tcp_connect_ms") if n50_data else None
    cold_real_tx_tls_ms = n50_data["first_attempt"].get("tls_handshake_ms") if n50_data else None

    tcp_only_stats = summarize(tcp_only)
    tcp_tls_total_stats = summarize(tcp_tls_total)
    cold_invalid_stats = summarize(cold_invalid)
    warm_invalid_stats = summarize(warm_invalid)

    network_only_ms = tcp_only_stats.get("median_ms")
    tls_overhead_ms = (tcp_tls_total_stats.get("median_ms") - network_only_ms) if (network_only_ms is not None and tcp_tls_total_stats.get("median_ms") is not None) else None
    cold_http_overhead_ms = (cold_invalid_stats.get("median_ms") - tcp_tls_total_stats.get("median_ms")) if (cold_invalid_stats.get("median_ms") is not None and tcp_tls_total_stats.get("median_ms") is not None) else None
    warm_http_baseline_ms = warm_invalid_stats.get("median_ms")
    warm_tx_processing_ms = (warm_real_tx_median_ms - warm_http_baseline_ms) if (warm_real_tx_median_ms is not None and warm_http_baseline_ms is not None) else None

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": SEQUENCER_SUBMIT_URL_MAINNET,
        "resolved_ip_used": ip,
        "raw_measurements": {
            "tcp_only_handshake_ms": tcp_only_stats,
            "tcp_component_of_tls_run_ms": summarize(tcp_component),
            "tls_component_ms": summarize(tls_component),
            "tcp_plus_tls_total_ms": tcp_tls_total_stats,
            "cold_invalid_method_round_trip_ms": cold_invalid_stats,
            "warm_invalid_method_round_trip_ms": warm_invalid_stats,
        },
        "reference_from_task5_delivery_latency_n50": {
            "source_file": str(n50_path),
            "warm_real_tx_median_ms": warm_real_tx_median_ms,
            "cold_real_tx_first_round_trip_ms": cold_real_tx_first_ms,
            "cold_real_tx_tcp_connect_ms": cold_real_tx_tcp_ms,
            "cold_real_tx_tls_handshake_ms": cold_real_tx_tls_ms,
        },
        "breakdown_cold_ladder": {
            "note": "Буквально по заданию -- каждая ступень НОВОЕ соединение. "
                    "Реальная цена этой лестницы платится ТОЛЬКО при установке нового "
                    "соединения (см. breakdown_warm_persistent для операционно релевантной картины).",
            "1_network_only_tcp_handshake_ms": network_only_ms,
            "2_tls_overhead_ms": tls_overhead_ms,
            "3_cold_http_overhead_over_tls_ms": cold_http_overhead_ms,
            "4_cold_real_tx_total_ms": cold_real_tx_first_ms,
        },
        "breakdown_warm_persistent_connection": {
            "note": "Операционно релевантно для бота, держащего ОДНО постоянное соединение "
                    "(TCP+TLS оплачиваются один раз при старте, не на каждую отправку).",
            "network_plus_minimal_http_ms": warm_http_baseline_ms,
            "tx_specific_processing_ms": warm_tx_processing_ms,
            "warm_real_tx_total_ms": warm_real_tx_median_ms,
        },
        "colocation_estimate": {
            "note": "Если бот стоит в том же регионе AWS, что sequencer.mainnet (us-east-2, "
                    "AS16509 -- см. docs/PROJECT_STATE.md) -- сетевая часть (network_only) "
                    "падает до ~1-2мс внутри-региона (типичное внутри-AZ/внутри-региона AWS "
                    "значение, НЕ измерено напрямую в этом прогоне -- честная оценка по общеизвестному "
                    "порядку величины, не реальный замер из другого региона AWS). Остальные "
                    "компоненты (TLS handshake, HTTP-накладные, обработка транзакции) НЕ зависят "
                    "от географии клиента -- остаются как есть.",
            "estimated_warm_total_ms_same_region": (
                (warm_tx_processing_ms + 2.0) if warm_tx_processing_ms is not None else None
            ),
            "estimated_reduction_ms": (
                (warm_real_tx_median_ms - (warm_tx_processing_ms + 2.0))
                if (warm_real_tx_median_ms is not None and warm_tx_processing_ms is not None) else None
            ),
        },
        "traceroute": None,  # заполняется отдельным шагом workflow (raw-сокет ICMP -- не доступен непривилегированному Python-процессу без root/CAP_NET_RAW)
    }

    out_path = Path(__file__).resolve().parent.parent / "data" / "task5_delivery_breakdown_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[delivery_breakdown] написано {out_path}", file=sys.stderr)
    print(json.dumps({
        "breakdown_cold_ladder": result["breakdown_cold_ladder"],
        "breakdown_warm_persistent_connection": result["breakdown_warm_persistent_connection"],
        "colocation_estimate": result["colocation_estimate"],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
