#!/usr/bin/env python3
"""Задача 5, владелец 2026-09-11, пункт 2: "Регион секвенсера -- бесплатно,
сегодня." Реальный публичный фид секвенсера Robinhood Chain (Arbitrum
Orbit, Nitro broadcaster protocol) -- НЕ выдуман, найден через WebSearch +
WebFetch реального README:
  https://github.com/chainstacklabs/robinhood-chain-sequencer-feed
Секвенсер-фид: wss://feed.mainnet.chain.robinhood.com
RPC: https://rpc.mainnet.chain.robinhood.com
(Официальная страница docs.robinhood.com/chain/connecting заблокирована
сетевым прокси этой сессии -- не смог свериться напрямую, но endpoint
подтверждён независимым реальным инструментом с открытым кодом.)

Метод (пассивное измерение, БЕЗ отправки транзакций -- только измерение,
как и просил владелец с самого начала Задачи 5):

1. RTT -- медиана TCP+TLS connect до обоих хостов (N=10 попыток).
2. "Feed lead time" -- слушаем WS фид `duration` секунд, для каждого
   сообщения фида парсим `sequenceNumber` (по документации инструмента
   выше -- "carries a sequence number matching the block number", то
   есть 1 сообщение = 1 L2-блок на этой цепи) и время получения.
   Параллельно опрашиваем `eth_blockNumber` через публичный RPC каждые
   ~150 мс, фиксируя момент, когда номер блока становится видимым через
   обычный RPC. lead_time = (момент видимости через RPC) - (момент
   получения того же номера блока через фид) -- ПОЛОЖИТЕЛЬНОЕ значение
   означает, что подписчик фида узнаёт о блоке РАНЬШЕ, чем обычный
   RPC-поллер.

ЧЕСТНАЯ ОГОВОРКА (важно, не скрывать): это измеряет только ОДНОСТОРОННЮЮ
задержку "секвенсер -> наблюдатель". Чтобы реально попасть в блок N или
N+1, нужен ЕЩЁ обратный путь "наблюдатель -> секвенсер" (отправка
транзакции), который здесь НЕ измеряется (это уже отправка транзакций,
не пассивное измерение -- отдельный, более дорогой и рискованный шаг).
lead_time -- необходимое, но не достаточное условие."""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import ssl
import statistics
import time
from urllib.parse import urlparse

import requests

FEED_URL = "wss://feed.mainnet.chain.robinhood.com"
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"


def tcp_tls_connect_rtt_ms(host: str, port: int = 443, n: int = 10, timeout: float = 8.0) -> dict:
    samples = []
    errors = []
    ctx = ssl.create_default_context()
    for _ in range(n):
        try:
            t0 = time.monotonic()
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host):
                    pass
            samples.append((time.monotonic() - t0) * 1000.0)
        except Exception as exc:  # честно фиксируем реальную ошибку, не тихо пропускаем
            errors.append(str(exc))
    if not samples:
        return {"n_ok": 0, "n_error": len(errors), "errors_sample": errors[:3]}
    return {
        "n_ok": len(samples),
        "n_error": len(errors),
        "median_ms": statistics.median(samples),
        "p10_ms": sorted(samples)[max(0, len(samples) // 10 - 1)] if len(samples) >= 10 else min(samples),
        "p90_ms": sorted(samples)[min(len(samples) - 1, int(len(samples) * 0.9))],
        "min_ms": min(samples),
        "max_ms": max(samples),
    }


async def poll_rpc_block_number(duration_s: float, interval_s: float, out: list) -> None:
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        t_wall = time.time()
        try:
            resp = await asyncio.to_thread(
                requests.post, RPC_URL, json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
                timeout=5,
            )
            data = resp.json()
            bn_hex = data.get("result")
            bn = int(bn_hex, 16) if bn_hex else None
        except Exception as exc:
            bn = None
            out.append({"t_wall": t_wall, "block_number": None, "error": str(exc)})
            await asyncio.sleep(interval_s)
            continue
        out.append({"t_wall": t_wall, "block_number": bn})
        await asyncio.sleep(interval_s)


async def listen_feed(duration_s: float, out: list, diag: dict) -> None:
    try:
        import websockets
    except ImportError:
        diag["error"] = "websockets library not installed"
        return
    deadline = time.monotonic() + duration_s
    try:
        async with websockets.connect(FEED_URL, open_timeout=10, close_timeout=5) as ws:
            diag["connected"] = True
            diag["connect_time_wall"] = time.time()
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                t_wall = time.time()
                diag["n_messages_total"] = diag.get("n_messages_total", 0) + 1
                try:
                    payload = json.loads(raw)
                except Exception:
                    diag["n_messages_unparsed"] = diag.get("n_messages_unparsed", 0) + 1
                    continue
                msgs = payload.get("messages") if isinstance(payload, dict) else None
                if not msgs:
                    # диагностика формата -- фиксируем реальные ключи один раз, не гадаем
                    if "sample_payload_keys" not in diag and isinstance(payload, dict):
                        diag["sample_payload_keys"] = sorted(payload.keys())
                    continue
                for m in msgs:
                    seq = m.get("sequenceNumber")
                    if seq is not None:
                        out.append({"t_wall": t_wall, "sequence_number": seq})
    except Exception as exc:
        diag["error"] = str(exc)


async def run_async(duration_s: float, poll_interval_s: float) -> dict:
    rpc_samples: list = []
    feed_samples: list = []
    feed_diag: dict = {}
    await asyncio.gather(
        poll_rpc_block_number(duration_s + 5, poll_interval_s, rpc_samples),
        listen_feed(duration_s, feed_samples, feed_diag),
    )
    return {"rpc_samples": rpc_samples, "feed_samples": feed_samples, "feed_diag": feed_diag}


def analyze(rpc_samples: list, feed_samples: list) -> dict:
    rpc_ok = [r for r in rpc_samples if r.get("block_number") is not None]
    if len(rpc_ok) < 2:
        return {"error": "недостаточно успешных опросов RPC для анализа", "n_rpc_ok": len(rpc_ok)}

    # момент, когда высота блока N впервые стала видна через RPC
    first_visible: dict[int, float] = {}
    for r in rpc_ok:
        bn = r["block_number"]
        if bn not in first_visible or r["t_wall"] < first_visible[bn]:
            first_visible[bn] = r["t_wall"]

    heights_sorted = sorted(first_visible.keys())
    block_intervals = [
        first_visible[heights_sorted[i + 1]] - first_visible[heights_sorted[i]]
        for i in range(len(heights_sorted) - 1)
        if heights_sorted[i + 1] == heights_sorted[i] + 1
    ]

    lead_times = []
    for f in feed_samples:
        seq = f["sequence_number"]
        if seq in first_visible:
            lead_times.append(first_visible[seq] - f["t_wall"])

    out = {
        "n_rpc_polls_ok": len(rpc_ok),
        "n_distinct_heights_observed": len(heights_sorted),
        "block_height_range": [heights_sorted[0], heights_sorted[-1]] if heights_sorted else None,
        "median_block_interval_s": statistics.median(block_intervals) if block_intervals else None,
        "n_feed_messages_with_seq": len(feed_samples),
        "n_feed_messages_matched_to_rpc_height": len(lead_times),
    }
    if lead_times:
        out["median_feed_lead_time_s"] = statistics.median(lead_times)
        out["p10_feed_lead_time_s"] = sorted(lead_times)[max(0, len(lead_times) // 10 - 1)]
        out["p90_feed_lead_time_s"] = sorted(lead_times)[min(len(lead_times) - 1, int(len(lead_times) * 0.9))]
        out["n_positive_lead"] = sum(1 for x in lead_times if x > 0)
        out["share_positive_lead"] = out["n_positive_lead"] / len(lead_times)
        if out.get("median_block_interval_s"):
            out["lead_time_as_share_of_block_interval"] = out["median_feed_lead_time_s"] / out["median_block_interval_s"]
    return out


def run(duration_s: float, poll_interval_s: float) -> dict:
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "feed_url": FEED_URL,
        "rpc_url": RPC_URL,
        "duration_s": duration_s,
        "poll_interval_s": poll_interval_s,
    }

    feed_host = urlparse(FEED_URL).hostname
    rpc_host = urlparse(RPC_URL).hostname
    result["rtt_feed_host_ms"] = tcp_tls_connect_rtt_ms(feed_host)
    result["rtt_rpc_host_ms"] = tcp_tls_connect_rtt_ms(rpc_host)

    async_out = asyncio.run(run_async(duration_s, poll_interval_s))
    result["feed_diag"] = async_out["feed_diag"]
    result["analysis"] = analyze(async_out["rpc_samples"], async_out["feed_samples"])
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=45.0)
    ap.add_argument("--interval", type=float, default=0.15)
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    res = run(args.duration, args.interval)
    text = json.dumps(res, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
