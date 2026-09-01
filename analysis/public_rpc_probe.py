"""Проба публичного RPC Robinhood Chain (владелец, 2026-09-01, разовая
проверка, тот же маркер PROBE_REQUEST, что и Blockscout-проба выше --
P3 не тронут):

  https://rpc.mainnet.chain.robinhood.com

Замерить БЕЗ ключа:
  1. eth_blockNumber -- проходит ли вообще.
  2. eth_getLogs по ОДНОМУ пулу топ-20 кластера 0x0eaced..., узкий
     диапазон 1000 блоков (якорь -- реальный блок запуска этого пула,
     чтобы диапазон был содержательным, не произвольным).
  3. Максимальный принимаемый диапазон блоков -- прогрессия размеров
     от того же стартового блока, до первой ошибки.
  4. Rate-limit -- N быстрых подряд eth_blockNumber, коды ответов и
     тайминги.

Доложить цифрами в data/p3_guard_cache/public_rpc_probe_result.json.
Если базовый вызов (п.1) уже режет -- дальше не пробовать, доложить
код ошибки (по инструкции владельца).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from alchemy_fallback import UNISWAP_V3_SWAP_SIG, topic0  # noqa: E402 -- переиспользуем, не хардкодим topic0 заново (найдена и исправлена опечатка при подготовке)

BASE_URL = "https://rpc.mainnet.chain.robinhood.com"
HEADERS = {"User-Agent": "robinhood-chain-alpha-public-rpc-probe/1.0", "Content-Type": "application/json"}
OUT_PATH = Path("data/p3_guard_cache/public_rpc_probe_result.json")

# Первый пул топ-20 кластера 0x0eaced... (docs/SC1_NOTE.md, analysis/
# sc1_wash_slice.py::TOP20_TOKEN_POOL) -- токен 0x329e6cfb..., пул
# 0xf5f84476..., реальный блок его запуска (из уже оплаченного
# sc1_august_launches_decoded.csv) -- используется как якорь диапазона,
# чтобы тест был содержательным (события вокруг запуска), не произвольным.
POOL_ADDRESS = "0xf5f8447620c1ab53108db068de8a895d7ba096ba"
POOL_LAUNCH_BLOCK = 33436269
UNISWAP_V3_SWAP_TOPIC0 = topic0(UNISWAP_V3_SWAP_SIG)  # вычислено, не хардкожено

RANGE_CANDIDATES = [1000, 2000, 5000, 10000, 20000, 50000]
RATE_LIMIT_N_CALLS = 20


def _rpc_call(method: str, params: list) -> tuple[int, dict | None, float, str | None]:
    """Возвращает (http_status, json_body_или_None, latency_s, ошибка_или_None)."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    t0 = time.monotonic()
    try:
        resp = requests.post(BASE_URL, json=payload, headers=HEADERS, timeout=30)
        latency = time.monotonic() - t0
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body, latency, None
    except requests.exceptions.RequestException as e:
        return -1, None, time.monotonic() - t0, str(e)


def run() -> int:
    result: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "base_url": BASE_URL}

    # 1. eth_blockNumber
    status, body, latency, err = _rpc_call("eth_blockNumber", [])
    print(f"[public_rpc_probe] eth_blockNumber: status={status} latency={latency:.3f}s body={body} err={err}")
    result["eth_blockNumber"] = {"status": status, "body": body, "latency_s": round(latency, 3), "error": err}
    if status != 200 or not body or "result" not in body:
        print("[public_rpc_probe] СТОП: базовый вызов не прошёл -- дальше не пробую, см. код ошибки выше.")
        result["stopped_at"] = "eth_blockNumber"
        _write(result)
        return 0
    current_block = int(body["result"], 16)
    print(f"[public_rpc_probe] текущий блок сети: {current_block}")
    result["current_block"] = current_block

    # 2. eth_getLogs, один пул, узкий диапазон 1000 блоков (якорь -- блок запуска)
    from_block, to_block = POOL_LAUNCH_BLOCK, POOL_LAUNCH_BLOCK + 999
    status, body, latency, err = _rpc_call("eth_getLogs", [{
        "fromBlock": hex(from_block), "toBlock": hex(to_block),
        "address": POOL_ADDRESS, "topics": [UNISWAP_V3_SWAP_TOPIC0],
    }])
    n_logs = len(body["result"]) if (status == 200 and body and "result" in body) else None
    print(f"[public_rpc_probe] eth_getLogs 1000 блоков [{from_block};{to_block}] pool={POOL_ADDRESS}: "
          f"status={status} latency={latency:.3f}s n_logs={n_logs} body_error={(body or {}).get('error')} err={err}")
    result["eth_getLogs_1000_blocks"] = {
        "status": status, "latency_s": round(latency, 3), "n_logs": n_logs,
        "body_error": (body or {}).get("error"), "error": err,
    }
    if status != 200 or not body or "result" not in body:
        print("[public_rpc_probe] СТОП: eth_getLogs (базовый узкий диапазон) не прошёл -- дальше не пробую.")
        result["stopped_at"] = "eth_getLogs_1000_blocks"
        _write(result)
        return 0

    # 3. Максимальный принимаемый диапазон -- прогрессия от того же старта, до первой ошибки
    range_probe = []
    max_ok_range = None
    for size in RANGE_CANDIDATES:
        fb, tb = POOL_LAUNCH_BLOCK, POOL_LAUNCH_BLOCK + size - 1
        status, body, latency, err = _rpc_call("eth_getLogs", [{
            "fromBlock": hex(fb), "toBlock": hex(tb),
            "address": POOL_ADDRESS, "topics": [UNISWAP_V3_SWAP_TOPIC0],
        }])
        ok = status == 200 and body and "result" in body
        n_logs = len(body["result"]) if ok else None
        body_error = (body or {}).get("error")
        print(f"[public_rpc_probe] диапазон {size} блоков: status={status} ok={ok} "
              f"n_logs={n_logs} body_error={body_error} err={err}")
        range_probe.append({
            "range_size": size, "status": status, "ok": bool(ok), "n_logs": n_logs,
            "body_error": body_error, "error": err, "latency_s": round(latency, 3),
        })
        if ok:
            max_ok_range = size
        else:
            print(f"[public_rpc_probe] диапазон {size} блоков -- первая ошибка, останавливаю прогрессию.")
            break
    result["range_probe"] = range_probe
    result["max_ok_range_tried"] = max_ok_range
    print(f"[public_rpc_probe] максимальный ПРОВЕРЕННЫЙ рабочий диапазон из кандидатов {RANGE_CANDIDATES}: "
          f"{max_ok_range} (могло быть выше -- проверялись только эти размеры)")

    # 4. Rate-limit: N быстрых подряд eth_blockNumber
    rate_probe = []
    t_start = time.monotonic()
    for i in range(RATE_LIMIT_N_CALLS):
        status, body, latency, err = _rpc_call("eth_blockNumber", [])
        rate_probe.append({"i": i, "status": status, "latency_s": round(latency, 3), "error": err})
        if status not in (200, -1):
            print(f"[public_rpc_probe] rate-limit: вызов {i} status={status} (не 200) -- возможный троттлинг")
    t_total = time.monotonic() - t_start
    n_ok = sum(1 for r in rate_probe if r["status"] == 200)
    n_throttled = sum(1 for r in rate_probe if r["status"] not in (200, -1))
    print(f"[public_rpc_probe] rate-limit проба: {n_ok}/{RATE_LIMIT_N_CALLS} успешно, "
          f"{n_throttled} с не-200 статусом, {t_total:.2f}с всего "
          f"(~{RATE_LIMIT_N_CALLS/t_total:.1f} запросов/с фактически без явного троттлинга)")
    result["rate_limit_probe"] = {
        "n_calls": RATE_LIMIT_N_CALLS, "n_ok": n_ok, "n_throttled": n_throttled,
        "total_time_s": round(t_total, 3), "calls": rate_probe,
    }

    _write(result)
    return 0


def _write(result: dict) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str))
    print(f"[public_rpc_probe] записано {OUT_PATH}")


if __name__ == "__main__":
    raise SystemExit(run())
