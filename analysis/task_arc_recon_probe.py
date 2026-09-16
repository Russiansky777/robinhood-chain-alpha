#!/usr/bin/env python3
"""Новая задача владельца ("ДЛЯ CODE, задание 1, Arc") -- read-only
разведка Arc Mainnet (Circle), запущенного сегодня (16.09). НИКАКИХ
транзакций, только чтение. Ohio -- единственное место в этом проекте
с реальным исходящим доступом в интернет (песочница сессии блокирует
произвольные хосты, проверено -- 403 CONNECT).

ЧЕСТНАЯ ОГОВОРКА: базовые факты (chain_id=5042, USDC-газ, EVM/Reth,
Malachite-консенсус, дата запуска) взяты из веб-поиска (см. отчёт
владельцу) -- это АГРЕГИРОВАННЫЕ вторичные источники (новостные сайты),
НЕ официальная документация Circle (docs.arc.io заблокирован для
прямого fetch из песочницы сессии). Реальный RPC/chain_id здесь
ПРОВЕРЯЕТСЯ напрямую, а не берётся на веру -- особенно учитывая, что
один из источников прямо предупредил про фальшивые "официальные" RPC
в неделю запуска нового чейна."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

CANDIDATE_RPCS = [
    "https://rpc.arc.io",
    "https://rpc.mainnet.arc.io",
    "https://rpc.arc.network",
    "https://arc-mainnet.rpc.io",
]
EXPECTED_CHAIN_ID_HEX = hex(5042)  # 0x13b2 -- из веб-поиска, ПРОВЕРЯЕМ, не считаем данностью

# Адрес, упомянутый в веб-поиске как "Uniswap v4 PoolManager на Arc" --
# ПОДОЗРИТЕЛЬНО совпадает byte-in-byte с PoolManager на Robinhood Chain
# в этом же проекте. Может быть (а) реальным совпадением из-за
# детерминированного CREATE2-деплоя Uniswap (официальная практика для
# v4 на многих чейнах), либо (б) галлюцинацией поисковой сводки. НЕ
# считаем истиной -- проверяем eth_getCode напрямую.
SUSPECT_POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"


def try_rpc(url: str) -> dict:
    row = {"url": url}
    try:
        resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
                              headers={"Content-Type": "application/json"}, timeout=10)
        row["http_status"] = resp.status_code
        try:
            row["body"] = resp.json()
        except Exception:
            row["body_raw_text"] = resp.text[:500]
    except Exception as exc:  # noqa: BLE001
        row["exception"] = f"{type(exc).__name__}: {exc}"
    return row


def main() -> None:
    result = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rpc_probes": []}
    working_url = None
    for url in CANDIDATE_RPCS:
        row = try_rpc(url)
        result["rpc_probes"].append(row)
        body = row.get("body") or {}
        if isinstance(body, dict) and body.get("result"):
            row["matches_expected_chain_id"] = body["result"].lower() == EXPECTED_CHAIN_ID_HEX.lower()
            if row["matches_expected_chain_id"] and working_url is None:
                working_url = url

    result["working_rpc_found"] = working_url

    if working_url:
        def rpc(method, params):
            r = requests.post(working_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                               headers={"Content-Type": "application/json"}, timeout=15)
            return r.json()

        try:
            latest_hex = rpc("eth_blockNumber", [])["result"]
            latest = int(latest_hex, 16)
            block_a = rpc("eth_getBlockByNumber", [hex(latest), False])["result"]
            time.sleep(2)
            latest_hex2 = rpc("eth_blockNumber", [])["result"]
            latest2 = int(latest_hex2, 16)
            block_b = rpc("eth_getBlockByNumber", [hex(latest2), False])["result"] if latest2 != latest else None
            result["chain_state"] = {
                "latest_block_1": latest, "latest_block_1_timestamp": int(block_a["timestamp"], 16),
                "latest_block_2_after_2s_sleep": latest2,
                "blocks_advanced_in_2s": latest2 - latest,
                "block_2_timestamp": int(block_b["timestamp"], 16) if block_b else None,
            }
        except Exception as exc:  # noqa: BLE001
            result["chain_state_error"] = str(exc)

        try:
            code = rpc("eth_getCode", [SUSPECT_POOL_MANAGER, "latest"])["result"]
            result["suspect_pool_manager_check"] = {
                "address": SUSPECT_POOL_MANAGER, "code_len_bytes": (len(code) - 2) // 2 if code else 0,
                "has_code": bool(code) and code != "0x",
            }
        except Exception as exc:  # noqa: BLE001
            result["suspect_pool_manager_check_error"] = str(exc)
    else:
        result["note"] = ("Ни один из угаданных RPC-URL не ответил корректным chain_id=5042 -- "
                           "реальный официальный mainnet RPC Circle НЕ подтверждён этим прогоном. "
                           "НЕ пытаемся угадывать дальше вслепую -- см. предупреждение веб-поиска "
                           "про фальшивые 'официальные' RPC в неделю запуска.")

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    Path(__file__).parent.parent.joinpath("data", "task_arc_recon_probe_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
