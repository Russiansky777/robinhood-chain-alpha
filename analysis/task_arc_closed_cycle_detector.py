#!/usr/bin/env python3
"""Задание владельца: НАСТОЯЩИЕ замкнутые циклы на Arc Day 0, не обычные
покупки (старый детектор путал одно с другим -- "один токен в плюсе"
ловит любую сквозную покупку). Предрегистрированное определение (см.
задание, п.1), сверено на реальных данных, не на памяти/документации:

  1) все Swap одной tx, по logIndex, >=2 плеч
  2) token_in(leg0) == token_out(leg_last) -- цикл-токен
  3) net-flow каждого ПРОМЕЖУТОЧНОГО токена ~ 0 (допуск 1e-9 от оборота)
  4) net-flow цикл-токена > 0
  5) прибыль после газа (USDC-native на Arc), с ценой только из плеча
     той же tx, если цикл-токен не USDC

Знак amount0/amount1: подтверждён на реальной tx (см.
task_arc_sign_convention_check_result.json) -- amount>0 = токен ПРИШЁЛ
В ПУЛ (token_in трейдера), amount<0 = токен УШЁЛ ИЗ ПУЛА (token_out).
НО: тот же реальный прогон показал, что величина amount0/1 из события
может НЕ совпадать с реально settled Transfer (хук/AFTER_SWAP_RETURNS_
DELTA) -- поэтому топология (кто/что/куда) берётся из знаков события
(дёшево, без доп. RPC), а РЕАЛЬНАЯ прибыль и net-flow -- из настоящих
Transfer-логов receipt (дорого, только для короткого списка кандидатов,
прошедших дешёвый топологический фильтр).

Двойная адресация USDC на Arc (подтверждено ранее и ниже, п.2 задания):
ERC20 0x3600...0000 (decimals=6) и "native"-представление того же
доллара по адресу-обёртке 0xfff...ffe (decimals=18, отношение 1e12
между ними подтверждено на реальных данных -- см. task_arc_hour_zero_
audit closed_loops_sample). Обе адресации трактуются как USD."""
from __future__ import annotations

import re
import time
from collections import defaultdict
from pathlib import Path
import json

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
DATA_DIR = Path(__file__).parent.parent.joinpath("data")

USDC_ERC20 = "0x3600000000000000000000000000000000000000"
USDC_NATIVE_WRAP = "0xfffffffffffffffffffffffffffffffffffffffe"  # подтверждено task_arc_hour_zero_audit
USDC_ERC20_DECIMALS = 6
USDC_NATIVE_DECIMALS = 18
AKA_FUN_HOOK = "0x20eead6db6b3d0a4491e9073119dd0ebff166acc"  # см. task_arc_hour_zero_audit_result.json

BLOCK_TIME_S = 0.506  # реально измерено task_arc_rpc_limits_probe_result.json
HOUR_BLOCKS = int(3600 / BLOCK_TIME_S)

# Реальное окно старого (ошибочного) детектора -- для прямого сравнения
# "старые попадания -> настоящие циклы".
OLD_DETECTOR_WINDOW = {"from_block": 21173786, "to_block": 21180986}

MAX_SCAN_CALLS_PER_WINDOW = 400
MAX_RECEIPT_LOOKUPS_TOTAL = 300
MIN_ADAPTIVE_CHUNK = 8
CHUNK_RETRY_PAUSE_S = 3

_RETRY_RANGE_RE = re.compile(r"retry with the range (\d+)-(\d+)")
_rpc_call_count = 0


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


INIT_TOPIC0 = keccak_topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
TRANSFER_TOPIC0 = keccak_topic0("Transfer(address,address,uint256)")


def rpc(method: str, params: list, timeout: int = 25) -> dict:
    global _rpc_call_count
    for attempt in range(5):
        _rpc_call_count += 1
        try:
            resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                  headers={"Content-Type": "application/json"}, timeout=timeout)
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            if attempt == 4:
                return {"error": {"code": None, "message": f"{type(exc).__name__}: {exc}"}}
            time.sleep(2 * (attempt + 1))
            continue
        err = body.get("error")
        if err and (err.get("code") == -32005 or "rate limit" in str(err.get("message", "")).lower()):
            if attempt == 4:
                return body
            time.sleep(2 * (attempt + 1))
            continue
        return body
    return {"error": {"code": None, "message": "unreachable"}}


def word(data_bytes: bytes, i: int) -> bytes:
    return data_bytes[i * 32:(i + 1) * 32]


def to_int_signed(w: bytes) -> int:
    v = int.from_bytes(w, "big")
    return v - 2 ** 256 if v >= 2 ** 255 else v


def to_addr(w: bytes) -> str:
    return "0x" + w[-20:].hex()


def topic_to_addr(t: str) -> str:
    return "0x" + t[-40:]


def chunked_get_logs(address: str, topic0: str, from_block: int, to_block: int, chunk: int,
                      max_calls: int) -> tuple[list, list, dict]:
    """Та же адаптивная схема, что в task_arc_hour_zero_audit.py (реально
    отработавшая версия): следует подсказке RPC при "max results", делит
    пополам при прочих ошибках размера, честно ждёт при rate limit
    (обрабатывается внутри rpc()), честный потолок вызовов."""
    out, unresolved = [], []
    block, n_calls, current_chunk = from_block, 0, chunk
    stats = {"initial_chunk": chunk, "n_adaptive_shrinks": 0}

    while block <= to_block and n_calls < max_calls:
        end = min(block + current_chunk - 1, to_block)
        lo, hi = block, end
        resolved = False
        for _ in range(20):
            if n_calls >= max_calls:
                break
            body = rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi),
                                         "address": address, "topics": [topic0]}])
            n_calls += 1
            if "error" not in body:
                out.extend(body.get("result", []))
                resolved = True
                block = hi + 1
                if hi < end:
                    current_chunk = hi - lo + 1
                break
            msg = body["error"].get("message", "")
            m = _RETRY_RANGE_RE.search(msg)
            if m:
                hint_hi = int(m.group(2))
                new_hi = min(hi, hint_hi)
                if new_hi >= hi:
                    new_hi = lo + max(MIN_ADAPTIVE_CHUNK, (hi - lo + 1) // 2) - 1
                hi = max(new_hi, lo)
            else:
                width = hi - lo + 1
                if width <= MIN_ADAPTIVE_CHUNK:
                    unresolved.append({"from": lo, "to": hi, "last_error": body["error"]})
                    block = hi + 1
                    resolved = True
                    break
                hi = lo + max(MIN_ADAPTIVE_CHUNK, width // 2) - 1
            stats["n_adaptive_shrinks"] += 1
        if not resolved:
            unresolved.append({"from": lo, "to": hi, "reason": "budget exhausted mid-range"})
            block = hi + 1
    stats["n_calls_used"] = n_calls
    stats["scan_incomplete"] = block <= to_block
    return out, unresolved, stats


def decode_initialize_log(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {
        "pool_id": log["topics"][1], "currency0": topic_to_addr(log["topics"][2]),
        "currency1": topic_to_addr(log["topics"][3]), "fee": int.from_bytes(word(data, 0)[-3:], "big"),
        "hooks": to_addr(word(data, 2)), "block_number": int(log["blockNumber"], 16),
    }


def decode_swap_log(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {
        "pool_id": log["topics"][1], "sender": topic_to_addr(log["topics"][2]),
        "amount0": to_int_signed(word(data, 0)), "amount1": to_int_signed(word(data, 1)),
        "liquidity": int.from_bytes(word(data, 3), "big"),
        "block_number": int(log["blockNumber"], 16), "log_index": int(log["logIndex"], 16),
        "tx_hash": log["transactionHash"],
    }


def decode_transfer_log(log: dict) -> dict | None:
    if len(log.get("topics", [])) < 3:
        return None
    return {"token": log["address"].lower(), "from": topic_to_addr(log["topics"][1]).lower(),
            "to": topic_to_addr(log["topics"][2]).lower(),
            "value": int(log["data"], 16) if log.get("data") and log["data"] != "0x" else 0}


def usdc_value(token: str, raw: int) -> float | None:
    t = token.lower()
    if t == USDC_ERC20.lower():
        return raw / (10 ** USDC_ERC20_DECIMALS)
    if t == USDC_NATIVE_WRAP.lower():
        return raw / (10 ** USDC_NATIVE_DECIMALS)
    return None


def build_pool_map(from_block: int, to_block: int) -> tuple[dict, dict]:
    init_logs, unresolved, stats = chunked_get_logs(POOL_MANAGER, INIT_TOPIC0, from_block, to_block,
                                                      chunk=5000, max_calls=40)
    pool_map = {}
    for log in init_logs:
        d = decode_initialize_log(log)
        pool_map[d["pool_id"]] = d
    return pool_map, {"n_found": len(pool_map), "unresolved": unresolved, "stats": stats}


def process_window(label: str, from_block: int, to_block: int, prior_pool_map: dict,
                    receipt_budget: list) -> dict:
    window_result: dict = {"label": label, "from_block": from_block, "to_block": to_block,
                            "window_blocks": to_block - from_block}

    pool_map, pool_map_meta = build_pool_map(from_block, to_block)
    # Пулы старше окна (созданы раньше) -- берём из предыдущего реального
    # реестра, если есть (без новых RPC).
    merged_pool_map = {**prior_pool_map, **pool_map}
    window_result["pool_map_scan"] = pool_map_meta

    swap_logs, swap_unresolved, swap_stats = chunked_get_logs(POOL_MANAGER, SWAP_TOPIC0, from_block, to_block,
                                                                chunk=200, max_calls=MAX_SCAN_CALLS_PER_WINDOW)
    swaps = [decode_swap_log(l) for l in swap_logs]
    window_result["swap_scan"] = {"n_found": len(swaps), "unresolved": swap_unresolved, "stats": swap_stats}

    by_tx: dict[str, list] = defaultdict(list)
    for s in swaps:
        by_tx[s["tx_hash"]].append(s)
    for h in by_tx:
        by_tx[h].sort(key=lambda s: s["log_index"])

    # --- Дешёвый топологический фильтр (без доп. RPC) ---
    n_multi_leg = 0
    n_unresolved_pool = 0
    n_ambiguous_leg = 0
    n_topology_candidates = 0
    candidates = []
    for tx_hash, legs in by_tx.items():
        if len(legs) < 2:
            continue
        n_multi_leg += 1
        leg_info = []
        ok = True
        for s in legs:
            meta = merged_pool_map.get(s["pool_id"])
            if meta is None:
                n_unresolved_pool += 1
                ok = False
                break
            c0, c1, a0, a1 = meta["currency0"], meta["currency1"], s["amount0"], s["amount1"]
            if (a0 > 0) == (a1 > 0):  # оба положительны/отрицательны/нулевые -- не обычный своп
                n_ambiguous_leg += 1
                ok = False
                break
            token_in, token_out = (c0, c1) if a0 > 0 else (c1, c0)
            amt_in, amt_out = (a0, -a1) if a0 > 0 else (a1, -a0)
            leg_info.append({"pool_id": s["pool_id"], "token_in": token_in, "token_out": token_out,
                              "amount_in_event": amt_in, "amount_out_event": amt_out,
                              "liquidity": s["liquidity"], "hooks": meta["hooks"],
                              "pool_init_block": meta["block_number"], "sender": s["sender"],
                              "block_number": s["block_number"]})
        if not ok:
            continue

        cycle_token = leg_info[0]["token_in"]
        if cycle_token.lower() != leg_info[-1]["token_out"].lower():
            continue  # не цикл -- сквозной маршрут (обычная покупка/продажа)

        # net-flow по событийным величинам (грубая проверка топологии,
        # финальная -- на реальных Transfer ниже для короткого списка).
        net_by_token: dict[str, int] = defaultdict(int)
        vol_by_token: dict[str, int] = defaultdict(int)
        for leg in leg_info:
            net_by_token[leg["token_in"].lower()] -= leg["amount_in_event"]
            net_by_token[leg["token_out"].lower()] += leg["amount_out_event"]
            vol_by_token[leg["token_in"].lower()] += leg["amount_in_event"]
            vol_by_token[leg["token_out"].lower()] += leg["amount_out_event"]

        intermediate_ok = True
        for tok, net in net_by_token.items():
            if tok == cycle_token.lower():
                continue
            vol = vol_by_token.get(tok, 0)
            if vol > 0 and abs(net) > 1e-9 * vol:
                intermediate_ok = False
                break
        if not intermediate_ok:
            continue
        if net_by_token.get(cycle_token.lower(), 0) <= 0:
            continue

        n_topology_candidates += 1
        candidates.append({"tx_hash": tx_hash, "legs": leg_info, "cycle_token_event": cycle_token,
                            "block_number": legs[0]["block_number"]})

    window_result["topology_filter"] = {
        "n_multi_leg_txs": n_multi_leg, "n_unresolved_pool_skipped": n_unresolved_pool,
        "n_ambiguous_leg_skipped": n_ambiguous_leg, "n_topology_candidates": n_topology_candidates,
    }

    # --- Верификация на РЕАЛЬНЫХ Transfer-логах receipt (дорого, только
    # для кандидатов, честный общий потолок вызовов на весь прогон) ---
    verified_cycles = []
    n_receipt_checked = 0
    n_receipt_capped = 0
    for cand in candidates:
        if receipt_budget[0] <= 0:
            n_receipt_capped += 1
            continue
        rec = rpc("eth_getTransactionReceipt", [cand["tx_hash"]])
        receipt_budget[0] -= 1
        n_receipt_checked += 1
        r = rec.get("result")
        if not r or r.get("status") != "0x1":
            continue  # реверт/не найдено -- не цикл

        transfers = [t for t in (decode_transfer_log(l) for l in r.get("logs", [])
                                  if l.get("topics", [None])[0] == TRANSFER_TOPIC0) if t]
        tx_from = (r.get("from") or "").lower()
        senders = {leg["sender"].lower() for leg in cand["legs"]}
        trader_addrs = senders | {tx_from}

        real_net: dict[str, int] = defaultdict(int)
        real_vol: dict[str, int] = defaultdict(int)
        for t in transfers:
            if t["token"] not in {leg["token_in"].lower() for leg in cand["legs"]} | \
                    {leg["token_out"].lower() for leg in cand["legs"]}:
                continue
            if t["from"] in trader_addrs:
                real_net[t["token"]] -= t["value"]
                real_vol[t["token"]] += t["value"]
            if t["to"] in trader_addrs:
                real_net[t["token"]] += t["value"]
                real_vol[t["token"]] += t["value"]

        cycle_token = cand["cycle_token_event"].lower()
        intermediate_ok_real = True
        for tok, net in real_net.items():
            if tok == cycle_token:
                continue
            vol = real_vol.get(tok, 0)
            if vol > 0 and abs(net) > 1e-9 * vol:
                intermediate_ok_real = False
                break
        real_cycle_net = real_net.get(cycle_token, 0)
        if not intermediate_ok_real or real_cycle_net <= 0:
            continue  # реальные Transfer не подтвердили цикл -- отбрасываем

        gas_used = int(r.get("gasUsed", "0x0"), 16)
        eff_gas_price = int(r.get("effectiveGasPrice", "0x0"), 16)
        gas_cost_native_wei = gas_used * eff_gas_price  # нативный газ = USDC, 18 decimals (см. модуль docstring)

        profit_usdc_gross = usdc_value(cycle_token, real_cycle_net)
        profit_usdc_net = None
        pricing_method = "cycle_token_is_usdc" if profit_usdc_gross is not None else None
        if profit_usdc_gross is None:
            # Цена только из плеча ЭТОЙ ЖЕ tx, где токен встречается против USDC.
            for leg in cand["legs"]:
                other = leg["token_out"] if leg["token_in"].lower() == cycle_token else (
                    leg["token_in"] if leg["token_out"].lower() == cycle_token else None)
                if other is None:
                    continue
                if usdc_value(other, 1) is not None:  # эта нога против USDC
                    usdc_amt = usdc_value(other, leg["amount_in_event"] if usdc_value(leg["token_in"], 1) is not None
                                           else leg["amount_out_event"])
                    token_amt = leg["amount_out_event"] if usdc_value(leg["token_in"], 1) is not None \
                        else leg["amount_in_event"]
                    if usdc_amt and token_amt:
                        implied_price = usdc_amt / (token_amt / 1e18)  # предполагаем 18 decimals по умолчанию
                        profit_usdc_gross = (real_cycle_net / 1e18) * implied_price
                        pricing_method = f"implied_from_same_tx_leg_pool_{leg['pool_id'][:10]}"
                        break

        gas_cost_usdc = gas_cost_native_wei / (10 ** USDC_NATIVE_DECIMALS)
        if profit_usdc_gross is not None:
            profit_usdc_net = profit_usdc_gross - gas_cost_usdc

        # Куда ушла прибыль: Transfer цикл-токена ИЗ sender'а НАРУЖУ (не в
        # PoolManager) в этой же tx.
        profit_destination = None
        for t in transfers:
            if t["token"] == cycle_token and t["from"] in trader_addrs and \
                    t["to"] not in trader_addrs and t["to"] != POOL_MANAGER.lower():
                profit_destination = t["to"]

        liquidity_zero_leg = any(leg["liquidity"] == 0 for leg in cand["legs"])
        aka_fun_leg = any(leg["hooks"].lower() == AKA_FUN_HOOK.lower() for leg in cand["legs"])
        pool_age_blocks = min((cand["block_number"] - leg["pool_init_block"]) for leg in cand["legs"]
                               if leg["pool_init_block"] is not None)

        verified_cycles.append({
            "tx_hash": cand["tx_hash"], "block_number": cand["block_number"], "n_legs": len(cand["legs"]),
            "cycle_token": cycle_token, "gross_profit_cycle_token_raw": real_cycle_net,
            "gas_used": gas_used, "effective_gas_price_wei": eff_gas_price,
            "gas_cost_usdc": gas_cost_usdc, "profit_usdc_gross": profit_usdc_gross,
            "profit_usdc_net": profit_usdc_net, "pricing_method": pricing_method,
            "senders": sorted(senders), "tx_from": tx_from, "profit_destination": profit_destination,
            "route": [{"pool_id": leg["pool_id"], "hooks": leg["hooks"], "liquidity": leg["liquidity"],
                       "token_in": leg["token_in"], "token_out": leg["token_out"]} for leg in cand["legs"]],
            "has_liquidity_zero_leg": liquidity_zero_leg, "uses_aka_fun_hook": aka_fun_leg,
            "pool_age_blocks_at_cycle": pool_age_blocks,
        })

    window_result["verification"] = {
        "n_candidates": len(candidates), "n_receipts_checked": n_receipt_checked,
        "n_receipts_capped": n_receipt_capped, "n_verified_real_cycles": len(verified_cycles),
    }
    window_result["verified_cycles"] = verified_cycles
    window_result["merged_pool_map_for_next_window"] = merged_pool_map
    return window_result


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rpc": RPC}
    receipt_budget = [MAX_RECEIPT_LOOKUPS_TOTAL]

    latest = int(rpc("eth_blockNumber", [])["result"], 16)
    windows = [("old_detector_window", OLD_DETECTOR_WINDOW["from_block"], OLD_DETECTOR_WINDOW["to_block"])]
    last_hour_from = max(0, latest - HOUR_BLOCKS)
    if last_hour_from != OLD_DETECTOR_WINDOW["from_block"]:
        windows.append(("last_full_hour_at_run", last_hour_from, latest))

    # Первый прогон показал реальный баг: подавляющее большинство
    # multi-leg tx трогают пулы, созданные РАНЬШЕ часового окна --
    # без сидирования старым реестром (уже есть на этом же VPS от
    # предыдущей сессии) 9234/11157 и 11412/15527 tx пропускались как
    # "pool не найден", 0 кандидатов -- недостоверно. Подмешиваем
    # data/task_arc_recon_pools_result.json (6ч-скан, 24567 пулов) как
    # стартовый реестр -- без доп. RPC-вызовов, только currency0/1
    # (hooks/fee для этих старых записей неизвестны, помечаем явно).
    pool_map: dict = {}
    seed_path = DATA_DIR / "task_arc_recon_pools_result.json"
    n_seeded = 0
    if seed_path.exists():
        seed = json.loads(seed_path.read_text())
        for p in seed.get("initialize_events", {}).get("pools", []):
            pool_map[p["pool_id"]] = {"pool_id": p["pool_id"], "currency0": p["currency0"],
                                       "currency1": p["currency1"], "fee": None, "hooks": "unknown",
                                       "block_number": p["block_number"]}
            n_seeded += 1
    result["pool_map_seed"] = {"path": str(seed_path), "found": seed_path.exists(), "n_seeded": n_seeded}

    window_results = []
    for label, fb, tb in windows:
        wr = process_window(label, fb, tb, pool_map, receipt_budget)
        pool_map = wr.pop("merged_pool_map_for_next_window")
        window_results.append(wr)

    result["windows"] = window_results
    result["total_rpc_calls_used"] = _rpc_call_count
    result["receipt_budget_remaining"] = receipt_budget[0]

    all_cycles = [c for wr in window_results for c in wr["verified_cycles"]]
    net_positive = [c for c in all_cycles if c["profit_usdc_net"] is not None and c["profit_usdc_net"] > 0]
    net_over_1usd = [c for c in net_positive if c["profit_usdc_net"] > 1]
    executor_counts: dict[str, int] = defaultdict(int)
    for c in all_cycles:
        for s in c["senders"]:
            executor_counts[s] += 1

    profits = sorted(c["profit_usdc_net"] for c in net_positive)
    def pctl(p):
        if not profits:
            return None
        idx = min(len(profits) - 1, int(len(profits) * p))
        return profits[idx]

    result["summary"] = {
        "n_cycles_total": len(all_cycles),
        "n_cycles_net_positive": len(net_positive),
        "n_cycles_net_over_1usd": len(net_over_1usd),
        "n_cycles_no_usd_pricing": sum(1 for c in all_cycles if c["profit_usdc_net"] is None),
        "n_unique_executors": len(executor_counts),
        "cycles_per_executor": dict(sorted(executor_counts.items(), key=lambda kv: kv[1], reverse=True)),
        "max_cycles_by_single_executor": max(executor_counts.values()) if executor_counts else 0,
        "profit_usdc_net_median": pctl(0.5), "profit_usdc_net_p90": pctl(0.9),
        "profit_usdc_net_max": profits[-1] if profits else None,
        "frac_cycles_via_aka_fun_hook": (sum(1 for c in all_cycles if c["uses_aka_fun_hook"]) / len(all_cycles)
                                          if all_cycles else None),
        "n_cycles_with_liquidity_zero_leg": sum(1 for c in all_cycles if c["has_liquidity_zero_leg"]),
    }

    # Предрегистрация (владелец, дословно из задания).
    max_exec = result["summary"]["max_cycles_by_single_executor"]
    n_pos = result["summary"]["n_cycles_net_positive"]
    total_all = result["summary"]["n_cycles_total"]
    if n_pos <= 5 and max_exec < 10:
        verdict = "OKNO"
    elif max_exec >= 20 or total_all >= 50:
        verdict = "GONKA"
    else:
        verdict = "NE_RESHENO"
    result["preregistered_verdict"] = verdict

    top_profitable = sorted(net_positive, key=lambda c: c["profit_usdc_net"], reverse=True)[:3]
    result["top_profitable_cycles_tx_hashes"] = [c["tx_hash"] for c in top_profitable]

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    DATA_DIR.joinpath("task_arc_closed_cycles_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
