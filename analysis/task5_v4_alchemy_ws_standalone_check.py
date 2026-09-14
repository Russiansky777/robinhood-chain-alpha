#!/usr/bin/env python3
"""Задача 5 (владелец, п.3): отдельная, ограниченная диагностика Alchemy
WebSocket -- БЕЗ подключения к официальному фиду. Тот же URL/конфигурация,
что уже отработала 600с в первой (сбойной только по фиду) попытке этого
замера. Один ограниченный заход: handshake -> подтверждение logs-подписки
(с фильтром Swap V4 PoolManager) -> получение хотя бы одного реального
Swap-лога, с ограничением по времени (не бесконечные retries). Отказ
подписки newPendingTransactions сохраняется ОТДЕЛЬНО и НЕ ломает основную
проверку logs -- это разные, независимые факты."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# РЕАЛЬНЫЙ баг, найденный этим же прогоном: без этой строки (та же, что в
# task5_v4_feed_vs_alchemy_measurement.py) CONFIG.alchemy_rpc_url пуст на
# Ohio -- ALCHEMY_ROBINHOOD_RPC_URL там не задан напрямую, только
# RPC_URL_PROVIDER. ДОЛЖНА идти ДО импорта config (CONFIG читает env при
# инициализации модуля).
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import websockets  # noqa: E402

from alchemy_fallback import UNISWAP_V4_SWAP_SIG, topic0  # noqa: E402
from config import CONFIG  # noqa: E402

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
V4_SWAP_TOPIC0 = topic0(UNISWAP_V4_SWAP_SIG)
# Ограниченное окно ожидания РЕАЛЬНОГО Swap-лога после успешной подписки --
# НЕ бесконечное, но достаточное, чтобы поймать хотя бы один реальный своп
# на активном пуле (реально наблюдалось МНОГО Swap-активности в первой
# 600с-попытке -- 0 логов за пару минут было бы уже нетипично).
MAX_WAIT_FOR_REAL_SWAP_LOG_S = 90.0


def _redact_ws_url(url: str) -> str:
    if not url:
        return url
    parts = url.rsplit("/", 1)
    if len(parts) == 2 and len(parts[1]) > 8:
        return parts[0] + "/<redacted>"
    return url


def _alchemy_ws_url() -> str | None:
    """ТОЧНО ТА ЖЕ логика, что task5_v4_feed_vs_alchemy_measurement.py::
    _alchemy_ws_url() -- намеренно не импортируется оттуда, чтобы этот
    диагностический скрипт был независим и не тянул модуль, который
    требует cooldown-гард фида при импорте побочных эффектов."""
    if CONFIG.alchemy_rpc_url:
        return CONFIG.alchemy_rpc_url.replace("https://", "wss://", 1)
    if CONFIG.alchemy_api_key:
        return f"wss://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}"
    return None


async def _subscribe(ws, name: str, params: dict | None, diag: dict, key: str) -> str | None:
    req = {"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": [name] + ([params] if params else [])}
    diag[f"{key}_request"] = req
    await ws.send(json.dumps(req))
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
    except asyncio.TimeoutError:
        diag[f"{key}_response"] = None
        diag[f"{key}_outcome"] = "no_response_received_timeout"
        return None
    try:
        body = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        diag[f"{key}_response"] = {"_parse_error": str(exc)}
        diag[f"{key}_outcome"] = "no_response_received_unparseable"
        return None
    diag[f"{key}_response"] = body
    if isinstance(body, dict) and "error" in body:
        diag[f"{key}_outcome"] = "subscription_rejected"
        return None
    if isinstance(body, dict) and body.get("result"):
        diag[f"{key}_outcome"] = "subscribed_ok"
        return body.get("result")
    diag[f"{key}_outcome"] = "unexpected_response_shape"
    return None


async def run() -> dict:
    diag: dict = {"stage": "not_started"}
    url = _alchemy_ws_url()
    if not url:
        diag["stage"] = "no_url_configured"
        diag["error"] = "нет ALCHEMY_API_KEY/ALCHEMY_ROBINHOOD_RPC_URL"
        return diag
    diag["url_redacted"] = _redact_ws_url(url)
    diag["url_construction_matches_measurement_script"] = True  # та же функция скопирована буквально
    diag["swap_topic0_used"] = V4_SWAP_TOPIC0

    diag["stage"] = "connecting"
    try:
        async with websockets.connect(url, open_timeout=15, close_timeout=5) as ws:
            diag["connected"] = True
            diag["connected_at_monotonic"] = time.monotonic()

            diag["stage"] = "subscribing_logs"
            sub_logs = await _subscribe(
                ws, "logs", {"address": POOL_MANAGER, "topics": [V4_SWAP_TOPIC0]}, diag, "swap_log_sub")
            if not sub_logs:
                diag["stage"] = "logs_subscription_failed"
                return diag

            # newPendingTransactions -- ОТДЕЛЬНО, её отказ НЕ ломает logs-проверку.
            diag["stage"] = "subscribing_pending_tx_separately"
            await _subscribe(ws, "newPendingTransactions", None, diag, "pending_tx_sub")

            diag["stage"] = "waiting_for_real_swap_log"
            deadline = time.monotonic() + MAX_WAIT_FOR_REAL_SWAP_LOG_S
            n_pending_seen = 0
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                t_mono = time.monotonic()
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                params = payload.get("params") or {}
                sub_id = params.get("subscription")
                result = params.get("result")
                if sub_id == sub_logs and isinstance(result, dict):
                    diag["stage"] = "real_swap_log_received"
                    diag["first_real_swap_log_at_monotonic"] = t_mono
                    diag["first_real_swap_log_seconds_after_subscribe"] = (
                        t_mono - diag["connected_at_monotonic"])
                    diag["first_real_swap_log_sample"] = {
                        "tx_hash": result.get("transactionHash"), "block_hash": result.get("blockHash"),
                        "block_number": result.get("blockNumber"), "log_index": result.get("logIndex"),
                        "topic0": (result.get("topics") or [None])[0],
                    }
                    return diag
                if sub_id == diag.get("pending_tx_sub_response", {}).get("result") and isinstance(result, str):
                    n_pending_seen += 1
            diag["n_pending_tx_events_seen_while_waiting"] = n_pending_seen
            diag["stage"] = "timeout_waiting_for_real_swap_log"
            return diag
    except websockets.exceptions.InvalidStatus as exc:
        diag["stage"] = "handshake_rejected"
        diag["status_code"] = exc.response.status_code
        return diag
    except Exception as exc:  # noqa: BLE001
        diag["stage"] = f"failed_at_{diag['stage']}"
        diag["error"] = f"{type(exc).__name__}: {exc}"
        return diag


def main() -> None:
    diag = asyncio.run(run())
    print(json.dumps(diag, indent=2, default=str, ensure_ascii=False))
    out = Path(__file__).parent.parent / "data" / "task5_v4_alchemy_ws_standalone_check_result.json"
    out.write_text(json.dumps(diag, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
