#!/usr/bin/env python3
"""Перепись по fee/hooks ПЕРЕД LVR (владелец, 2026-09-16): дешёвая
проверка -- есть ли вообще смысл считать LVR на 300 пулах, или линия LP
на Arc закрыта фактом (fee_pips=0 почти everywhere, как на 2 уже
проверенных пулах).

РЕАЛЬНАЯ находка при подготовке: data/task_arc_recon_pools_result.json
(24567 пулов) НЕ содержит fee/hooks -- исходный скан (analysis/
task_arc_recon_pools.py) decode'ил только topics (pool_id/currency0/
currency1/block_number/tx_hash), поле "data" Initialize-события (fee,
tickSpacing, hooks, sqrtPriceX96, tick) НЕ читалось и НЕ сохранялось.
Посылка задания ("fee и hooks читаются прямо из Initialize [уже
сохранённого]") была неверна -- нужен свежий скан. Он всё равно дешёвый:
Initialize -- редкое событие (24567 за ~38198 блоков = 0.64/блок), тот
же скрипт нашёл рабочий chunk=5000 для ЭТОГО топика (см. summary) -- те
же ~9-15 вызовов, просто в этот раз декодируем "data" тоже.

Свопы -- ДРУГОЕ дело: 141357 Swap-логов ЗА ОДИН ЧАС (task_arc_hour_zero_
audit_result.json) -- полный скан по ВСЕМ 24567 пулам с рождения обошёлся
бы в сотни вызовов и сотни МБ трафика. Вместо этого: (1) точный подсчёт
свопов ТОЛЬКО для пулов с fee_pips>0 (таргетированный запрос по
конкретному pool_id -- дёшево, т.к. это малое подмножество, если гипотеза
"почти все fee=0" верна); (2) СЛУЧАЙНАЯ выборка N пулов из ВСЕХ 24567 для
оценки доли "живых" (>=50 свопов) по всей популяции -- честно помечено
как оценка по выборке, не полный подсчёт."""
from __future__ import annotations

import json
import random
import time
from collections import Counter
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
PROFIT_RECEIVER_HOOK = "0x47e7936ae9891e61c5123db720593c05de7120cc"
DATA_DIR = Path(__file__).parent.parent.joinpath("data")
LOCAL_REGISTRY_PATHS = [
    Path("/home/bot/robinhood-chain-alpha/data/task_arc_recon_pools_result.json"),
    DATA_DIR.joinpath("task_arc_recon_pools_result.json"),
]
ALIVE_THRESHOLD_SWAPS = 50
RANDOM_SAMPLE_SIZE = 300
RANDOM_SEED = 20260916

_rpc_calls = [0]


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


INIT_TOPIC0 = keccak_topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")


def rpc(method: str, params: list, timeout: int = 30) -> dict:
    _rpc_calls[0] += 1
    for attempt in range(5):
        try:
            resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                  headers={"Content-Type": "application/json"}, timeout=timeout)
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            if attempt == 4:
                return {"error": {"message": f"{type(exc).__name__}: {exc}"}}
            time.sleep(2 * (attempt + 1))
            continue
        err = body.get("error")
        if err and "rate limit" in str(err.get("message", "")).lower():
            if attempt == 4:
                return body
            time.sleep(2 * (attempt + 1))
            continue
        return body
    return {"error": {"message": "unreachable"}}


def word(data_bytes: bytes, i: int) -> bytes:
    return data_bytes[i * 32:(i + 1) * 32]


def decode_initialize_full(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {
        "pool_id": log["topics"][1], "currency0": "0x" + log["topics"][2][-40:],
        "currency1": "0x" + log["topics"][3][-40:],
        "fee_pips": int.from_bytes(word(data, 0)[-3:], "big"),
        "tick_spacing": int.from_bytes(word(data, 1)[-3:], "big", signed=True),
        "hooks": "0x" + word(data, 2)[-20:].hex(),
        "block_number": int(log["blockNumber"], 16), "tx_hash": log["transactionHash"],
    }


def chunked_get_logs_full_decode(address: str, topic0: str, from_block: int, to_block: int,
                                  chunk: int, max_calls: int) -> tuple[list, int, dict]:
    out = []
    block = from_block
    calls = 0
    min_chunk_seen = chunk
    n_shrinks = 0
    while block <= to_block and calls < max_calls:
        end = min(block + chunk - 1, to_block)
        body = rpc("eth_getLogs", [{"fromBlock": hex(block), "toBlock": hex(end),
                                     "address": address, "topics": [topic0]}])
        calls += 1
        if "error" in body:
            err = body["error"]
            msg = str(err.get("message", "")).lower()
            if err.get("code") in (-32012, -32602) or "too large" in msg or "max results" in msg:
                chunk = max(50, chunk // 2)
                min_chunk_seen = min(min_chunk_seen, chunk)
                n_shrinks += 1
                continue  # тот же block, меньший диапазон
            out.append({"_chunk_error": err, "from": block, "to": end})
            block = end + 1
            continue
        out.extend(body.get("result", []))
        block = end + 1
    return out, calls, {"min_chunk_seen": min_chunk_seen, "n_shrinks": n_shrinks,
                         "scan_incomplete": block <= to_block}


def count_swaps_for_pool(pool_id: str, from_block: int, to_block: int, max_calls: int = 15) -> dict:
    """Точечный подсчёт (не декод) числа Swap-логов КОНКРЕТНОГО пула --
    адаптивный чанк, останавливаемся рано, если явно перевалили порог
    ALIVE_THRESHOLD_SWAPS с большим запасом (не нужно знать точное число
    для очень активных пулов, достаточно 'точно >= порога')."""
    total = 0
    block = from_block
    chunk = min(20000, max(1, to_block - from_block + 1))
    calls = 0
    while block <= to_block and calls < max_calls:
        end = min(block + chunk - 1, to_block)
        body = rpc("eth_getLogs", [{"fromBlock": hex(block), "toBlock": hex(end),
                                     "address": POOL_MANAGER, "topics": [SWAP_TOPIC0, pool_id]}])
        calls += 1
        if "error" in body:
            err = body["error"]
            msg = str(err.get("message", "")).lower()
            if err.get("code") in (-32012, -32602) or "too large" in msg or "max results" in msg:
                chunk = max(500, chunk // 2)
                continue
            return {"pool_id": pool_id, "n_swaps": total, "calls": calls, "error": err, "complete": False}
        total += len(body.get("result", []))
        block = end + 1
        if total >= ALIVE_THRESHOLD_SWAPS * 5:
            # с большим запасом жив -- не тратим вызовы на точное число
            return {"pool_id": pool_id, "n_swaps": total, "calls": calls, "complete": False,
                     "note": f"остановлено рано -- уже >= {ALIVE_THRESHOLD_SWAPS*5}, точно жив"}
    return {"pool_id": pool_id, "n_swaps": total, "calls": calls, "complete": block > to_block}


def main() -> None:
    result: dict = {
        "probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scoping_note": (
            "Проверка 'живости' (>=50 свопов) ограничена окном [Initialize, Initialize+12ч] на пул, "
            "а не всей историей с рождения без ограничения -- потому что дальнейший LVR-анализ всё равно "
            "смотрит только на окна 1/3/6/12ч, более старая активность пула к этому заданию не относится. "
            "Для пулов младше 12ч на момент замера окно урезано до текущего latest_block (честно, не форсируем будущее)."
        ),
    }

    # === локальный реестр (для окна скана и как fallback currency0/1) ===
    registry = None
    for p in LOCAL_REGISTRY_PATHS:
        if p.exists():
            registry = json.loads(p.read_text())
            result["registry_source_path"] = str(p)
            break
    if registry is None:
        result["error"] = "локальный реестр task_arc_recon_pools_result.json не найден ни по одному из путей"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        DATA_DIR.joinpath("task_arc_lp_fee_census_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return

    reg_pools = registry.get("initialize_events", {}).get("pools", [])
    result["registry_n_pools"] = len(reg_pools)
    min_init_block = min(p["block_number"] for p in reg_pools)
    max_init_block = max(p["block_number"] for p in reg_pools)
    result["registry_block_range"] = {"min_init_block": min_init_block, "max_init_block": max_init_block}

    latest = int(rpc("eth_blockNumber", []).get("result", "0x0"), 16)
    result["latest_block_now"] = latest

    # === Шаг 1: свежий скан Initialize с ПОЛНЫМ декодом (fee, hooks) ===
    # Тот же диапазон, что дал 24567 пулов изначально (с запасом на старте
    # окна -- 2 чанка тогда ошиблись, пробуем закрыть тот пробел тоже).
    scan_from = min_init_block - 6000
    scan_to = latest
    init_logs, n_calls_init, init_stats = chunked_get_logs_full_decode(
        POOL_MANAGER, INIT_TOPIC0, scan_from, scan_to, chunk=5000, max_calls=60)
    pools_full = [decode_initialize_full(l) for l in init_logs if "_chunk_error" not in l]
    n_chunk_errors = sum(1 for l in init_logs if "_chunk_error" in l)
    result["step1_initialize_rescan"] = {
        "scan_from": scan_from, "scan_to": scan_to, "n_calls": n_calls_init,
        "n_chunk_errors": n_chunk_errors, "adaptive_stats": init_stats,
        "n_pools_found": len(pools_full),
        "vs_original_registry_count": result["registry_n_pools"],
    }

    fee_zero = [p for p in pools_full if p["fee_pips"] == 0]
    fee_nonzero = [p for p in pools_full if p["fee_pips"] > 0]
    result["step1_fee_census"] = {
        "n_total": len(pools_full),
        "n_fee_zero": len(fee_zero), "pct_fee_zero": len(fee_zero) / len(pools_full) * 100 if pools_full else None,
        "n_fee_nonzero": len(fee_nonzero), "pct_fee_nonzero": len(fee_nonzero) / len(pools_full) * 100 if pools_full else None,
    }

    fee_hist = Counter(p["fee_pips"] for p in fee_nonzero)
    result["step1_nonzero_fee_histogram_pips"] = dict(sorted(fee_hist.items()))

    hook_counts = Counter(p["hooks"] for p in pools_full)
    top10_hooks = hook_counts.most_common(10)
    hook_fee_breakdown = []
    for hook_addr, n in top10_hooks:
        fees_for_hook = Counter(p["fee_pips"] for p in pools_full if p["hooks"] == hook_addr)
        hook_fee_breakdown.append({"hook": hook_addr, "n_pools": n,
                                    "fee_pips_used": dict(sorted(fees_for_hook.items()))})
    result["step1_top10_hooks"] = hook_fee_breakdown

    receiver_hook_pools = [p for p in pools_full if p["hooks"].lower() == PROFIT_RECEIVER_HOOK.lower()]
    result["step1_profit_receiver_hook"] = {
        "hook": PROFIT_RECEIVER_HOOK, "n_pools": len(receiver_hook_pools),
        "pct_of_all_pools": len(receiver_hook_pools) / len(pools_full) * 100 if pools_full else None,
        "fee_pips_used": dict(sorted(Counter(p["fee_pips"] for p in receiver_hook_pools).items())),
    }

    # === Шаг 2а: точный подсчёт свопов ТОЛЬКО для fee>0 пулов (таргетированно) ===
    fee_nonzero_alive_check = []
    calls_budget_step2a = 800
    calls_used_step2a = 0
    for p in fee_nonzero:
        if calls_used_step2a >= calls_budget_step2a:
            fee_nonzero_alive_check.append({"pool_id": p["pool_id"], "skipped_budget": True})
            continue
        window_end = min(latest, p["block_number"] + int(12 * 3600 / 0.506))
        r = count_swaps_for_pool(p["pool_id"], p["block_number"], window_end)
        calls_used_step2a += r["calls"]
        r["fee_pips"] = p["fee_pips"]
        r["hooks"] = p["hooks"]
        r["init_block"] = p["block_number"]
        r["alive"] = r["n_swaps"] >= ALIVE_THRESHOLD_SWAPS
        fee_nonzero_alive_check.append(r)

    n_fee_nonzero_alive = sum(1 for r in fee_nonzero_alive_check if r.get("alive"))
    result["step2a_fee_nonzero_activity_exact"] = {
        "n_fee_nonzero_pools_checked": len(fee_nonzero_alive_check),
        "n_calls_used": calls_used_step2a,
        "n_alive_ge_50_swaps": n_fee_nonzero_alive,
        "qualifying_pools": [r for r in fee_nonzero_alive_check if r.get("alive")],
    }

    # === Шаг 2б: случайная выборка ВСЕХ пулов для оценки доли "живых" (>=50) ===
    random.seed(RANDOM_SEED)
    all_pool_ids_with_block = [(p["pool_id"], p["block_number"]) for p in pools_full]
    sample = random.sample(all_pool_ids_with_block, min(RANDOM_SAMPLE_SIZE, len(all_pool_ids_with_block)))
    sample_results = []
    calls_used_sample = 0
    for pool_id, init_block in sample:
        window_end = min(latest, init_block + int(12 * 3600 / 0.506))
        r = count_swaps_for_pool(pool_id, init_block, window_end, max_calls=8)
        calls_used_sample += r["calls"]
        r["alive"] = r["n_swaps"] >= ALIVE_THRESHOLD_SWAPS
        sample_results.append(r)

    n_alive_sample = sum(1 for r in sample_results if r["alive"])
    alive_rate_estimate = n_alive_sample / len(sample_results) if sample_results else None
    result["step2b_random_sample_alive_rate"] = {
        "sample_size": len(sample_results), "n_calls_used": calls_used_sample,
        "n_alive_ge_50_swaps_in_sample": n_alive_sample,
        "alive_rate_estimate": alive_rate_estimate,
        "estimated_total_alive_pools_in_registry": (alive_rate_estimate * len(pools_full)) if alive_rate_estimate is not None else None,
    }

    if alive_rate_estimate and n_fee_nonzero_alive is not None:
        est_total_alive = alive_rate_estimate * len(pools_full)
        result["step2_weighted_estimate"] = {
            "n_fee_nonzero_and_alive_EXACT": n_fee_nonzero_alive,
            "estimated_total_alive_pools_ALL_fee_tiers": est_total_alive,
            "estimated_pct_of_alive_pools_with_fee_gt_0": (n_fee_nonzero_alive / est_total_alive * 100) if est_total_alive else None,
            "caveat": "числитель (fee>0 И живой) -- ТОЧНЫЙ таргетированный подсчёт. Знаменатель (все живые) -- ОЦЕНКА по случайной выборке 300 пулов, не полный подсчёт (полный скан всех Swap-логов с рождения каждого из 24567 пулов стоил бы сотни вызовов и сотни МБ -- не сделан, честно не выдаётся за точный)."
        }

    # === Решение по предрегистрации ===
    if n_fee_nonzero_alive >= 20:
        decision = "4_PRODOLZHIT -- >=20 живых пулов с fee>0 найдено, продолжаем LVR ТОЛЬКО на них"
    else:
        decision = "3_NET -- меньше 20 живых пулов с fee>0, линия LP на Arc закрыта фактом, LVR не считаем"
    result["decision"] = decision
    result["decision_pool_count"] = n_fee_nonzero_alive

    result["total_rpc_calls"] = _rpc_calls[0]

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    DATA_DIR.joinpath("task_arc_lp_fee_census_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
