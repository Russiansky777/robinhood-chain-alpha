#!/usr/bin/env python3
"""Задача 5, восьмой раунд (разбор владельца, пункт 4): "проверь настройки
hardfork и версию локального Anvil применительно к исходной сети;
устрани несовместимость Excess blob gas not set, не меняя произвольно
правила исполнения ради прохождения теста".

Единственный НЕ-произвольный способ выбрать правильный --hardfork для
anvil -- посмотреть, какие поля РЕАЛЬНО присутствуют в РЕАЛЬНОМ заголовке
блока этой цепи (chain_id 4663): наличие/отсутствие excessBlobGas/
blobGasUsed (EIP-4844, Cancun+), withdrawalsRoot (Shanghai+),
baseFeePerGas (EIP-1559, London+). Если РЕАЛЬНЫЙ блок этой цепи НЕ несёт
excessBlobGas -- цепь физически не поддерживает EIP-4844, и anvil,
форкающий её на любом hardfork Cancun+, будет ТРЕБОВАТЬ поле, которого
у форкнутого источника никогда не было -- это и есть источник "Excess
blob gas not set", а не случайная несовместимость версий. Выбор
--hardfork здесь -- ПРИВЕДЕНИЕ окружения форка в соответствие с РЕАЛЬНЫМ
устройством исходной цепи, а не смягчение правил ради прохождения теста
(она делает форк ТОЧНЕЕ отражающим реальность, а не наоборот)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _alchemy_direct_endpoint, _rpc_call  # noqa: E402

FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
ANVIL = str(FOUNDRY_BIN / "anvil")
CAST = str(FOUNDRY_BIN / "cast")
PORT = 8559
RPC = f"http://127.0.0.1:{PORT}"

# Кандидаты, от новых к старым -- пробуем ПОСЛЕ того, как реальные поля
# заголовка блока скажут, на чём именно остановиться (не перебор вслепую).
HARDFORK_CANDIDATES_NEWEST_FIRST = ["cancun", "shanghai", "paris", "london"]


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def main() -> None:
    result: dict = {}

    # --- Часть A: версия anvil ---
    v = run([ANVIL, "--version"], timeout=15)
    result["anvil_version"] = v.stdout.strip() or v.stderr.strip()

    # --- Часть B: РЕАЛЬНЫЙ заголовок недавнего блока этой цепи ---
    latest_num = int(_rpc_call("eth_blockNumber", []), 16)
    block = _rpc_call("eth_getBlockByNumber", [hex(latest_num), False])
    result["sample_block_number"] = latest_num
    result["sample_block_fields_present"] = {
        "baseFeePerGas": "baseFeePerGas" in block,
        "withdrawalsRoot": "withdrawalsRoot" in block,
        "excessBlobGas": "excessBlobGas" in block,
        "blobGasUsed": "blobGasUsed" in block,
        "parentBeaconBlockRoot": "parentBeaconBlockRoot" in block,
    }
    result["sample_block_raw_relevant"] = {
        k: block.get(k) for k in ("baseFeePerGas", "withdrawalsRoot", "excessBlobGas",
                                    "blobGasUsed", "parentBeaconBlockRoot")
    }
    has_blob_fields = result["sample_block_fields_present"]["excessBlobGas"]
    has_withdrawals = result["sample_block_fields_present"]["withdrawalsRoot"]
    has_base_fee = result["sample_block_fields_present"]["baseFeePerGas"]
    if has_blob_fields:
        inferred = "cancun"
    elif has_withdrawals:
        inferred = "shanghai"
    elif has_base_fee:
        inferred = "london"
    else:
        inferred = "paris"
    result["inferred_correct_hardfork_from_real_chain_fields"] = inferred
    result["inference_reasoning"] = (
        f"excessBlobGas присутствует={has_blob_fields}, withdrawalsRoot присутствует={has_withdrawals}, "
        f"baseFeePerGas присутствует={has_base_fee} -- РЕАЛЬНАЯ цепь на блоке {latest_num} "
        f"{'поддерживает' if has_blob_fields else 'НЕ поддерживает'} EIP-4844 (blob-транзакции); "
        f"вывод: '{inferred}' -- САМЫЙ НОВЫЙ hardfork, чьи поля реально присутствуют в заголовке, "
        f"а не первый, который 'просто работает'."
    )

    fork_url = _alchemy_direct_endpoint()
    if not fork_url:
        result["ok"] = False
        result["error"] = "нет Alchemy-эндпоинта для форка"
        _finish(result)
        return

    # --- Часть C: пробуем anvil С ПРАВИЛЬНЫМ (сделанным по реальным данным,
    # не наугад) --hardfork, ТОЛЬКО его -- не перебираем остальные вслепую,
    # если он сработал; иначе -- честно фиксируем, какие ещё пробовали. ---
    attempts = []
    for hf in [inferred] + [h for h in HARDFORK_CANDIDATES_NEWEST_FIRST if h != inferred]:
        anvil_proc = None
        attempt: dict = {"hardfork": hf}
        try:
            anvil_proc = subprocess.Popen(
                [ANVIL, "--fork-url", fork_url, "--fork-block-number", str(latest_num - 5),
                 "--hardfork", hf, "--port", str(PORT)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            banner = []
            deadline = time.monotonic() + 25
            started_ok = False
            while time.monotonic() < deadline:
                line = anvil_proc.stdout.readline()
                if not line:
                    if anvil_proc.poll() is not None:
                        break
                    continue
                banner.append(line.rstrip("\n"))
                if "Listening on" in line:
                    started_ok = True
                    break
                if "error" in line.lower() and "unknown hardfork" in line.lower():
                    break
            attempt["anvil_started"] = started_ok
            attempt["anvil_banner_tail"] = banner[-5:]
            if not started_ok:
                attempt["result"] = "anvil не запустился с этим --hardfork (см. banner_tail)"
                attempts.append(attempt)
                continue

            ready = False
            for _ in range(20):
                p = run([CAST, "block-number", "--rpc-url", RPC], timeout=10)
                if p.returncode == 0:
                    ready = True
                    break
                time.sleep(1)
            if not ready:
                attempt["result"] = "anvil запустился, но не ответил на cast block-number"
                attempts.append(attempt)
                continue

            call_obj = json.dumps({"from": "0x0000000000000000000000000000000000000000",
                                     "to": "0x0000000000000000000000000000000000000000", "data": "0x"})
            trace_proc = run([CAST, "rpc", "debug_traceCall", call_obj, "latest",
                               json.dumps({"tracer": "callTracer"}), "--rpc-url", RPC], timeout=20)
            attempt["debug_trace_call_returncode"] = trace_proc.returncode
            attempt["debug_trace_call_stdout_head"] = trace_proc.stdout.strip()[:500]
            attempt["debug_trace_call_stderr"] = trace_proc.stderr.strip()[:500]
            attempt["excess_blob_gas_error"] = "Excess blob gas not set" in trace_proc.stderr
            attempt["result"] = ("debug_traceCall прошёл" if trace_proc.returncode == 0 else
                                   "debug_traceCall НЕ прошёл (см. stderr)")
        except Exception as exc:  # noqa: BLE001
            attempt["result"] = f"исключение: {exc}"
        finally:
            if anvil_proc is not None:
                anvil_proc.terminate()
                try:
                    anvil_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    anvil_proc.kill()
        attempts.append(attempt)
        if attempt.get("debug_trace_call_returncode") == 0:
            break  # нашли рабочий hardfork -- дальше не перебираем

    result["hardfork_attempts"] = attempts
    working = [a for a in attempts if a.get("debug_trace_call_returncode") == 0]
    result["working_hardfork"] = working[0]["hardfork"] if working else None
    result["ok"] = bool(working)
    _finish(result)


def _finish(result: dict) -> None:
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_item4_hardfork_diagnosis_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[item4_hardfork] сохранено: {out_path}")


if __name__ == "__main__":
    main()
