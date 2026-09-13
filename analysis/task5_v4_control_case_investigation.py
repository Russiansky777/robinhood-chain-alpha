#!/usr/bin/env python3
"""Задача 5, живой пилот, шестой раунд -- "почему пилот не отправил ни
одной сделки": контрольная сделка конкурента, поддержка quoteExactInput
(многоходовая котировка), реальный замер RPC (задержка/пропускная
способность/блоктайм), поддержка исторического block-параметра у
eth_estimateGas, и разбор 7+4 конкретных пропусков из no-send журнала.

Все вызовы -- ЧТЕНИЕ (eth_call/eth_getLogs/eth_estimateGas без
подписи/отправки). Ничего не меняет на цепи, ничего не тратит. Только
через уже существующий, доверенный RPC-путь этого проекта
(alchemy_fallback.py) -- без новых доменов.

Запускается на Ohio (только там есть сетевой доступ к RPC этой цепи).
Каждая часть -- в своём try/except, честно падает в JSON-результат при
ошибке, не глотает молча и не останавливает остальные части."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import requests  # noqa: E402

from alchemy_fallback import (  # noqa: E402
    _alchemy_direct_endpoint, _BASE_HEADERS, _chunked_get_logs, _rpc_call, topic0,
    UNISWAP_V4_SWAP_SIG,
)
from task5_v4_pool_math import (  # noqa: E402
    PoolKey, decode_quote_result, decode_v4_swap_log_data, pool_id,
    quote_exact_input_single_calldata, quote_exact_input_multihop_calldata,
    QUOTE_EXACT_INPUT_SELECTOR,
)

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
NATIVE = "0x0000000000000000000000000000000000000000"
KNOWN_ARBITRAGEUR = "0x1b357e7acd2a32aebfa2de286c9e8e617d39a251"
BENEFICIARY = "0x11854ce19dcd63a7eccaa12ded5aa991c94c79c6"
OUR_CONTRACT = "0xAB24907bceEF4EDC366a1E4DfA15ed8aA5fdfe71"
V4_SWAP_TOPIC0 = topic0(UNISWAP_V4_SWAP_SIG)

PILOT_START_UTC = "2026-09-13T03:19:20Z"
PILOT_END_UTC = "2026-09-13T11:51:03Z"

RESULT: dict = {}
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _save() -> None:
    (DATA_DIR / "task5_v4_control_case_investigation_result.json").write_text(
        json.dumps(RESULT, indent=2, default=str, ensure_ascii=False))


def _direct_call(url: str, method: str, params: list, timeout: float = 20.0) -> tuple[float, dict]:
    """Прямой POST на конкретный эндпоинт (в обход fallback-порядка/
    троттлинга) -- для ЧЕСТНОГО замера латентности КОНКРЕТНОГО
    провайдера, без самотроттлинга _post_with_fallback между
    измерениями (иначе меряли бы троттлинг, а не сеть)."""
    t0 = time.monotonic()
    resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                          headers=_BASE_HEADERS, timeout=timeout)
    dt = time.monotonic() - t0
    return dt, resp.json()


# ---------- ЧАСТЬ A: замер RPC (латентность, устойчивая пропускная способность) ----------

def part_a_rpc_benchmark() -> dict:
    from config import CONFIG
    out: dict = {}
    public_url = CONFIG.public_rpc_url
    alchemy_url = _alchemy_direct_endpoint()
    out["public_rpc_url_host"] = public_url.split("//")[-1].split("/")[0] if public_url else None
    out["alchemy_configured"] = bool(alchemy_url)

    def bench(url: str, label: str, n: int = 15) -> dict:
        lat = []
        errors = 0
        t_start = time.monotonic()
        for _ in range(n):
            try:
                dt, body = _direct_call(url, "eth_blockNumber", [])
                if "error" in body:
                    errors += 1
                else:
                    lat.append(dt)
            except Exception as exc:  # noqa: BLE001
                errors += 1
        wall = time.monotonic() - t_start
        lat.sort()
        return {
            "label": label, "n_requests": n, "n_errors": errors,
            "wall_s": round(wall, 3), "achieved_req_per_s": round((n - errors) / wall, 2) if wall > 0 else None,
            "latency_min_ms": round(lat[0] * 1000, 1) if lat else None,
            "latency_median_ms": round(lat[len(lat) // 2] * 1000, 1) if lat else None,
            "latency_max_ms": round(lat[-1] * 1000, 1) if lat else None,
        }

    if public_url:
        print("[speed_audit] бенчмарк публичного RPC (без искусственной паузы между запросами)...")
        out["public_rpc_burst"] = bench(public_url, "public_rpc_no_delay")
    if alchemy_url:
        print("[speed_audit] бенчмарк Alchemy напрямую (без искусственной паузы между запросами)...")
        out["alchemy_burst"] = bench(alchemy_url, "alchemy_no_delay")

    # eth_call (реальная котировка) -- не только eth_blockNumber, разные
    # методы могут иметь разную стоимость на стороне нода.
    quoter_calldata = quote_exact_input_single_calldata(
        PoolKey(USDG, "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a", 70000, 4), True, 1_000_000)
    try:
        latest = int(_rpc_call("eth_blockNumber", []), 16)
        out["latest_block_at_benchmark"] = latest
        call_params = [{"to": V4_QUOTER, "data": quoter_calldata}, hex(latest)]

        def bench_call(url: str, label: str, n: int = 10) -> dict:
            lat = []
            errors = 0
            for _ in range(n):
                try:
                    dt, body = _direct_call(url, "eth_call", call_params)
                    if "error" in body and "NotEnoughLiquidity" not in str(body.get("error")):
                        errors += 1
                    else:
                        lat.append(dt)
                except Exception:  # noqa: BLE001
                    errors += 1
            lat.sort()
            return {"label": label, "n": n, "n_errors": errors,
                    "latency_median_ms": round(lat[len(lat) // 2] * 1000, 1) if lat else None,
                    "latency_min_ms": round(lat[0] * 1000, 1) if lat else None}

        if public_url:
            out["public_rpc_eth_call"] = bench_call(public_url, "public_rpc_eth_call")
        if alchemy_url:
            out["alchemy_eth_call"] = bench_call(alchemy_url, "alchemy_eth_call")
    except Exception as exc:  # noqa: BLE001
        out["eth_call_benchmark_error"] = str(exc)

    return out


# ---------- ЧАСТЬ B: реальный блоктайм (не предположение) ----------

def part_b_block_time() -> dict:
    out: dict = {}
    try:
        latest = int(_rpc_call("eth_blockNumber", []), 16)
        blocks = []
        for offset in (0, 200, 400, 600, 800, 1000):
            bn = latest - offset
            blk = _rpc_call("eth_getBlockByNumber", [hex(bn), False])
            blocks.append({"block": bn, "timestamp": int(blk["timestamp"], 16)})
        blocks.sort(key=lambda b: b["block"])
        diffs = []
        for i in range(1, len(blocks)):
            dt_blocks = blocks[i]["block"] - blocks[i - 1]["block"]
            dt_time = blocks[i]["timestamp"] - blocks[i - 1]["timestamp"]
            diffs.append({"from_block": blocks[i - 1]["block"], "to_block": blocks[i]["block"],
                           "n_blocks": dt_blocks, "seconds": dt_time,
                           "avg_block_time_s": round(dt_time / dt_blocks, 4) if dt_blocks else None})
        out["samples"] = blocks
        out["intervals"] = diffs
        avg_times = [d["avg_block_time_s"] for d in diffs if d["avg_block_time_s"]]
        out["overall_avg_block_time_s"] = round(sum(avg_times) / len(avg_times), 4) if avg_times else None
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


# ---------- ЧАСТЬ C: quoteExactInput (многоходовая котировка) ----------

def part_c_multihop_quoter() -> dict:
    out: dict = {}
    MOSIAI = "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a"
    HOOKS_NONE = "0x0000000000000000000000000000000000000000"
    pool_a = PoolKey(USDG, MOSIAI, 70000, 4)
    pool_b = PoolKey(USDG, MOSIAI, 70000, 3)
    try:
        latest = int(_rpc_call("eth_blockNumber", []), 16)
        out["block"] = latest
        amount_in = 1_000_000
        # Последовательно (текущий hot path).
        cd1 = quote_exact_input_single_calldata(pool_a, True, amount_in)
        raw1 = _rpc_call("eth_call", [{"to": V4_QUOTER, "data": cd1}, hex(latest)])
        mosiai_out, _ = decode_quote_result(raw1)
        cd2 = quote_exact_input_single_calldata(pool_b, False, mosiai_out)
        raw2 = _rpc_call("eth_call", [{"to": V4_QUOTER, "data": cd2}, hex(latest)])
        usdg_out_sequential, _ = decode_quote_result(raw2)
        out["sequential_two_calls"] = {"mosiai_out": mosiai_out, "usdg_out": usdg_out_sequential,
                                        "n_eth_call": 2}

        # Один вызов quoteExactInput (весь маршрут).
        cd_multi = quote_exact_input_multihop_calldata(
            USDG, [(MOSIAI, 70000, 4, HOOKS_NONE), (USDG, 70000, 3, HOOKS_NONE)], amount_in)
        out["multihop_calldata_selector"] = cd_multi[:10]
        t0 = time.monotonic()
        raw_multi = _rpc_call("eth_call", [{"to": V4_QUOTER, "data": cd_multi}, hex(latest)])
        dt_multi = time.monotonic() - t0
        usdg_out_multi, gas_estimate_multi = decode_quote_result(raw_multi)
        out["multihop_one_call"] = {"usdg_out": usdg_out_multi, "gas_estimate": gas_estimate_multi,
                                     "n_eth_call": 1, "latency_s": round(dt_multi, 3)}
        out["matches_sequential"] = usdg_out_multi == usdg_out_sequential
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = str(exc)
    return out


# ---------- ЧАСТЬ D: eth_estimateGas на историческом блоке -- поддерживается ли? ----------

def part_d_historical_estimate_gas() -> dict:
    """Проверяет: честно ли эндпоинт учитывает второй (блочный) параметр
    eth_estimateGas, или молча всегда считает на latest/pending
    (JSON-RPC формально допускает block-параметр, но многие ноды его
    игнорируют для estimateGas -- нужно ПРОВЕРИТЬ, не предполагать)."""
    out: dict = {}
    try:
        latest = int(_rpc_call("eth_blockNumber", []), 16)
        old_block = latest - 50
        call = {"from": "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75", "to": V4_QUOTER,
                 "data": quote_exact_input_single_calldata(
                     PoolKey(USDG, "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a", 70000, 4), True, 1_000_000)}
        # eth_estimateGas С явным блочным тегом.
        try:
            g_latest = int(_rpc_call("eth_estimateGas", [call, "latest"]), 16)
            out["estimate_gas_latest_ok"] = True
            out["gas_at_latest"] = g_latest
        except Exception as exc:  # noqa: BLE001
            out["estimate_gas_latest_ok"] = False
            out["estimate_gas_latest_error"] = str(exc)
        try:
            g_old = int(_rpc_call("eth_estimateGas", [call, hex(old_block)]), 16)
            out["estimate_gas_historical_ok"] = True
            out["gas_at_old_block"] = g_old
            out["old_block"] = old_block
        except Exception as exc:  # noqa: BLE001
            out["estimate_gas_historical_ok"] = False
            out["estimate_gas_historical_error"] = str(exc)
        # Без блочного параметра вообще (как делает estimate_gas() в
        # task5_v4_hotpath.py СЕЙЧАС -- см. её докстринг: без
        # block_identifier -- отсюда честная проверка, на что это
        # реально резолвится по умолчанию).
        try:
            g_default = int(_rpc_call("eth_estimateGas", [call]), 16)
            out["estimate_gas_no_block_param_ok"] = True
            out["gas_no_block_param"] = g_default
            out["matches_latest"] = g_default == out.get("gas_at_latest")
        except Exception as exc:  # noqa: BLE001
            out["estimate_gas_no_block_param_ok"] = False
            out["estimate_gas_no_block_param_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def main() -> None:
    print("[speed_audit] === ЧАСТЬ A: RPC-бенчмарк ===")
    RESULT["part_a_rpc_benchmark"] = part_a_rpc_benchmark()
    print(json.dumps(RESULT["part_a_rpc_benchmark"], indent=2, ensure_ascii=False))
    _save()

    print("[speed_audit] === ЧАСТЬ B: реальный блоктайм ===")
    RESULT["part_b_block_time"] = part_b_block_time()
    print(json.dumps(RESULT["part_b_block_time"], indent=2, ensure_ascii=False))
    _save()

    print("[speed_audit] === ЧАСТЬ C: quoteExactInput (многоходовая котировка) ===")
    RESULT["part_c_multihop_quoter"] = part_c_multihop_quoter()
    print(json.dumps(RESULT["part_c_multihop_quoter"], indent=2, ensure_ascii=False))
    _save()

    print("[speed_audit] === ЧАСТЬ D: eth_estimateGas на историческом блоке ===")
    RESULT["part_d_historical_estimate_gas"] = part_d_historical_estimate_gas()
    print(json.dumps(RESULT["part_d_historical_estimate_gas"], indent=2, ensure_ascii=False))
    _save()

    print("[speed_audit] готово, результат в data/task5_v4_control_case_investigation_result.json")


if __name__ == "__main__":
    main()
