#!/usr/bin/env python3
"""Владелец, 2026-09-03 (дал реальный ALCHEMY_API_KEY, добавлен секретом
GH Actions): "Проверь фактический лимит запросов в секунду и
максимальный диапазон блоков на вызов — доложи цифрами... Если ключ не
проходит аутентификацию или упирается в лимит бесплатного тира —
остановись и доложи, не переключайся молча на публичный RPC."

НАРОЧНО не использует общий _post_with_fallback/_endpoints() (который
по конструкции молча уходит на следующий эндпоинт при 401/403/сетевой
ошибке) -- бьёт ПРЯМО в Alchemy URL, чтобы auth-провал был виден как
явный провал этого прогона, а не тихо замаскирован публичным RPC.

URL Alchemy для Robinhood Chain не подтверждён независимо (см.
докстринг _endpoints() в alchemy_fallback.py) -- пробуется несколько
правдоподобных вариантов ПОДРЯД, каждый явно записывается в результат
(сработавший/несработавший), выбор не скрыт.

Тесты:
  1. Auth -- eth_blockNumber на каждом URL-кандидате.
  2. Реальный rate limit -- серия eth_blockNumber с уменьшающимся
     интервалом (0.5с -> 0.05с), первая устойчивая 429/ошибка лимита
     фиксирует эмпирический потолок.
  3. Максимальный диапазон блоков/запрос -- eth_getLogs на СТОК-пуле
     NVDA (плотность известна из mm_pool_verify_result.json) с растущим
     диапазоном, пока не словим ошибку лимита диапазона/числа логов.

Только чтение, ключ используется ТОЛЬКО как HTTP-заголовок/URL для
живых вызовов в этом прогоне (не печатается, не пишется в файл как
текст -- см. _redact ниже), транзакций нет.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

from alchemy_fallback import UNISWAP_V3_SWAP_SIG, topic0  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/alchemy_key_probe_result.json")
NVDA_V3_POOL = "0xd4eb21209c4d6093f80b5b84f5c45cc093ea14a3"  # реальный, подтверждён mm_pool_verify_result.json
TOPIC0_V3_SWAP = topic0(UNISWAP_V3_SWAP_SIG)  # посчитан, не вписан вручную

CANDIDATE_HOSTS = [
    "robinhood-mainnet.g.alchemy.com",
    "robinhood.g.alchemy.com",
    "robinhood-chain-mainnet.g.alchemy.com",
    "robinhoodchain-mainnet.g.alchemy.com",
]


def _redact(msg: str, key: str) -> str:
    return msg.replace(key, "***REDACTED***") if key else msg


def _rpc(url: str, method: str, params: list, timeout: int = 20) -> dict:
    resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout)
    return {"status_code": resp.status_code, "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:300]}


def run() -> int:
    key = os.environ.get("ALCHEMY_API_KEY", "")
    if not key:
        result = {"error": "ALCHEMY_API_KEY не задан в окружении job'а -- секрет GH Actions отсутствует или не проброшен в workflow", "auth_ok": False}
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print("[alchemy_key_probe] СТОП: ALCHEMY_API_KEY пуст.")
        return 1

    t0 = time.time()
    print("=== Часть 1: auth -- перебор кандидатов хоста ===")
    working_url = None
    auth_attempts = []
    for host in CANDIDATE_HOSTS:
        url = f"https://{host}/v2/{key}"
        try:
            r = _rpc(url, "eth_blockNumber", [])
            ok = r["status_code"] == 200 and "result" in (r["body"] if isinstance(r["body"], dict) else {})
            auth_attempts.append({"host": host, "status_code": r["status_code"],
                                   "ok": ok, "body_sample": _redact(str(r["body"])[:200], key)})
            print(f"[alchemy_key_probe] {host}: status={r['status_code']} ok={ok}")
            if ok and working_url is None:
                working_url = url
                working_host = host
        except Exception as e:  # noqa: BLE001
            auth_attempts.append({"host": host, "error": _redact(str(e), key)})
            print(f"[alchemy_key_probe] {host}: исключение {_redact(str(e), key)}")

    if working_url is None:
        result = {
            "auth_ok": False,
            "auth_attempts": auth_attempts,
            "verdict": "НИ ОДИН URL-кандидат не прошёл аутентификацию -- ключ не подтверждён рабочим для Robinhood Chain, "
                       "ОСТАНОВЛЕНО по прямому указанию владельца (не переключаться молча на публичный RPC).",
            "requests_used_alchemy": len(auth_attempts), "runtime_s": time.time() - t0,
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"\n[alchemy_key_probe] ОСТАНОВЛЕНО: {result['verdict']}")
        return 1

    print(f"\n[alchemy_key_probe] Рабочий URL: хост={working_host} (сам ключ не печатается)")

    print("\n=== Часть 2: реальный rate limit (серия eth_blockNumber, уменьшающийся интервал) ===")
    intervals = [0.5, 0.25, 0.1, 0.05, 0.02, 0.0]
    rate_results = []
    n_calls_alchemy = len(auth_attempts)
    for interval in intervals:
        n_ok, n_429, n_err = 0, 0, 0
        burst_t0 = time.time()
        for _ in range(20):
            try:
                r = _rpc(working_url, "eth_blockNumber", [])
                n_calls_alchemy += 1
                if r["status_code"] == 200:
                    n_ok += 1
                elif r["status_code"] == 429:
                    n_429 += 1
                else:
                    n_err += 1
            except Exception:  # noqa: BLE001
                n_err += 1
            if interval > 0:
                time.sleep(interval)
        elapsed = time.time() - burst_t0
        achieved_rps = 20 / elapsed if elapsed > 0 else None
        rate_results.append({"target_interval_s": interval, "n_ok": n_ok, "n_429": n_429, "n_err": n_err,
                              "elapsed_s": elapsed, "achieved_req_per_s": achieved_rps})
        print(f"[alchemy_key_probe] interval={interval}s: ok={n_ok} 429={n_429} err={n_err} "
              f"достигнуто~{achieved_rps:.2f}req/s" if achieved_rps else f"[alchemy_key_probe] interval={interval}s: ok={n_ok} 429={n_429} err={n_err}")
        if n_429 > 5:
            print(f"[alchemy_key_probe] стабильный 429 на interval={interval}s -- останавливаю сужение")
            break

    print("\n=== Часть 3: максимальный диапазон блоков/запрос (eth_getLogs, реальный пул NVDA v3) ===")
    latest_r = _rpc(working_url, "eth_blockNumber", [])
    n_calls_alchemy += 1
    latest = int(latest_r["body"]["result"], 16)
    range_results = []
    for block_range in (2_000, 10_000, 50_000, 100_000, 500_000, 2_000_000, 5_000_000):
        from_block = max(1, latest - block_range)
        try:
            r = _rpc(working_url, "eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(latest),
                                                     "address": NVDA_V3_POOL, "topics": [TOPIC0_V3_SWAP]}], timeout=30)
            n_calls_alchemy += 1
            body = r["body"]
            if isinstance(body, dict) and "result" in body:
                range_results.append({"block_range": block_range, "status_code": r["status_code"],
                                       "n_logs_returned": len(body["result"]), "ok": True})
                print(f"[alchemy_key_probe] диапазон {block_range} блоков: OK, {len(body['result'])} логов")
            else:
                range_results.append({"block_range": block_range, "status_code": r["status_code"],
                                       "ok": False, "body_sample": _redact(str(body)[:300], key)})
                print(f"[alchemy_key_probe] диапазон {block_range} блоков: ОШИБКА -- {_redact(str(body)[:200], key)}")
                break
        except Exception as e:  # noqa: BLE001
            range_results.append({"block_range": block_range, "ok": False, "error": _redact(str(e), key)})
            print(f"[alchemy_key_probe] диапазон {block_range} блоков: исключение {_redact(str(e), key)}")
            break

    result = {
        "auth_ok": True, "working_host": working_host, "auth_attempts": auth_attempts,
        "rate_limit_probe": rate_results, "block_range_probe": range_results,
        "requests_used_alchemy": n_calls_alchemy, "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n[alchemy_key_probe] записано {OUT_PATH}, {n_calls_alchemy} запросов к Alchemy, {time.time()-t0:.0f}с")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
