#!/usr/bin/env python3
"""Arc Mainnet -- суженное задание владельца: НЕ характеризовать все
24567 найденных пулов, а найти "живые" за последний час реальных
торгов, и день-ноль арбитражную активность тем же методом ("баланс
инициатора"), что финальный аудит на Robinhood Chain -- НЕ net-flow
формула, а прямой разбор Transfer-логов на tx.from.

Все RPC-вызовы -- к https://rpc.mainnet.arc.io (единственный подтверждённый
рабочий mainnet RPC, см. task_arc_recon_probe.py). Только чтение.
Честные самоограничения на число вызовов в каждой фазе -- если реальных
данных больше, чем бюджет вызовов, это явно помечается, а не выдаётся
как полное покрытие."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
DATA_DIR = Path(__file__).parent.parent.joinpath("data")

# Подтверждён отдельным скриптом (task_arc_currency0_identity.py) -- этот
# скрипт ЧИТАЕТ уже сохранённый результат, не предполагает заново.
IDENTITY_RESULT_PATH = DATA_DIR / "task_arc_currency0_identity_result.json"
PREV_POOL_MAP_PATH = DATA_DIR / "task_arc_recon_pools_result.json"

BLOCK_TIME_S = 0.5  # подтверждено измерением (4 блока за 2с), не предположение
HOUR_BLOCKS = int(3600 / BLOCK_TIME_S)  # 7200

MAX_CHUNK_RETRIES = 3
CHUNK_RETRY_PAUSE_S = 3
TOP_N = 30
PER_POOL_TX_FROM_CAP = 40
MAX_ARB_RECEIPT_LOOKUPS = 500


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


INIT_TOPIC0 = keccak_topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
TRANSFER_TOPIC0 = keccak_topic0("Transfer(address,address,uint256)")

_rpc_call_count = 0


def rpc(method: str, params: list, timeout: int = 25) -> dict:
    """Реальный прогон (пункт 2, tx.from) показал баг: eth_getTransactionByHash
    вызывался БЕЗ повтора на rate limit (в отличие от лог-сканера), и весь
    бюджет в 400 вызовов ушёл в -32005, тихо трактуемый как "from не
    найден" -- 0 разрешённых трейдеров на всех пулах топа. Централизуем
    честный повтор с паузой на rate limit ЗДЕСЬ, для всех вызовов сразу."""
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
    if v >= 2 ** 255:
        v -= 2 ** 256
    return v


def to_addr(w: bytes) -> str:
    return "0x" + w[-20:].hex()


def topic_to_addr(topic_hex: str) -> str:
    return "0x" + topic_hex[-40:]


import re

_RETRY_RANGE_RE = re.compile(r"retry with the range (\d+)-(\d+)")
MIN_ADAPTIVE_CHUNK = 8


def chunked_get_logs_with_retry(address: str, topic0: str, from_block: int, to_block: int,
                                 chunk: int, max_calls: int, extra_topics: list | None = None) -> tuple[list, list, dict]:
    """Реальный первый прогон показал: ограничение провайдера на Swap-топик
    -- это НЕ размер диапазона (как для Initialize, где 5000 работал), а
    число результатов ("query exceeds max results 2000") -- на Arc Day 0
    плотность свопов настолько высока, что RPC сам подсказывает безопасный
    под-диапазон в тексте ошибки ("retry with the range X-Y"). Вместо
    слепого повтора ТОГО ЖЕ диапазона (бесполезно для детерминированной
    ошибки размера) -- адаптивно следуем подсказке/делим пополам, и это
    же становится новым рабочим шагом для последующих диапазонов."""
    out: list = []
    unresolved: list = []
    block = from_block
    n_calls = 0
    topics = [topic0] + (extra_topics or [])
    current_chunk = chunk
    stats = {"initial_chunk": chunk, "min_chunk_seen": chunk, "n_adaptive_shrinks": 0}

    def try_range(lo: int, hi: int) -> dict:
        nonlocal n_calls
        n_calls += 1
        return rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi),
                                     "address": address, "topics": topics}])

    while block <= to_block and n_calls < max_calls:
        end = min(block + current_chunk - 1, to_block)
        lo, hi = block, end
        resolved = False
        for _ in range(20):  # честный потолок адаптивных сужений на один диапазон
            if n_calls >= max_calls:
                break
            body = try_range(lo, hi)
            if "error" not in body:
                out.extend(body.get("result", []))
                resolved = True
                block = hi + 1
                if hi < end:
                    # диапазон сузился -- следующий шаг продолжает с той же
                    # (уменьшенной) шириной, не возвращаясь к исходной.
                    current_chunk = hi - lo + 1
                break
            err = body["error"]
            if err.get("code") == -32005 or "rate limit" in err.get("message", "").lower():
                # Rate-limit -- НЕ ошибка размера диапазона. Сужение чанка
                # тут бесполезно (прошлый прогон впустую жёг вызовы именно
                # так) -- честная пауза и повтор ТОГО ЖЕ диапазона, как
                # прямо просил владелец.
                time.sleep(CHUNK_RETRY_PAUSE_S)
                continue
            msg = err.get("message", "")
            m = _RETRY_RANGE_RE.search(msg)
            if m:
                # Не доверяем hint_lo слепо (держим свой lo неизменным, чтобы
                # не пропустить блоки, если провайдер вдруг предложит
                # диапазон, начинающийся позже нашего) -- используем только
                # верхнюю границу подсказки, с защитой от "подсказка не
                # уменьшила диапазон" (тогда делим пополам сами).
                hint_hi = int(m.group(2))
                new_hi = min(hi, hint_hi)
                if new_hi >= hi:
                    new_hi = lo + max(MIN_ADAPTIVE_CHUNK, (hi - lo + 1) // 2) - 1
                hi = max(new_hi, lo)
            else:
                width = hi - lo + 1
                if width <= MIN_ADAPTIVE_CHUNK:
                    unresolved.append({"from": lo, "to": hi, "last_error": body["error"],
                                        "reason": "min_adaptive_chunk_reached"})
                    block = hi + 1
                    resolved = True  # сдвигаемся дальше, честно пометив как unresolved
                    break
                hi = lo + max(MIN_ADAPTIVE_CHUNK, width // 2) - 1
            stats["n_adaptive_shrinks"] += 1
            stats["min_chunk_seen"] = min(stats["min_chunk_seen"], hi - lo + 1)
        if not resolved:
            unresolved.append({"from": lo, "to": hi, "reason": "n_calls budget exhausted mid-range"})
            block = hi + 1
    stats["final_chunk"] = current_chunk
    stats["n_calls_used"] = n_calls
    stats["scan_incomplete"] = block <= to_block
    if stats["scan_incomplete"]:
        stats["first_unscanned_block"] = block
    return out, unresolved, stats


def decode_initialize_log(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {
        "pool_id": log["topics"][1],
        "currency0": topic_to_addr(log["topics"][2]),
        "currency1": topic_to_addr(log["topics"][3]),
        "fee": int.from_bytes(word(data, 0)[-3:], "big"),
        "tick_spacing": to_int_signed(word(data, 1)),
        "hooks": to_addr(word(data, 2)),
        "sqrt_price_x96": int.from_bytes(word(data, 3), "big"),
        "tick": to_int_signed(word(data, 4)),
        "block_number": int(log["blockNumber"], 16),
        "tx_hash": log["transactionHash"],
    }


def decode_swap_log(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {
        "pool_id": log["topics"][1],
        "sender": topic_to_addr(log["topics"][2]),
        "amount0": to_int_signed(word(data, 0)),
        "amount1": to_int_signed(word(data, 1)),
        "sqrt_price_x96": int.from_bytes(word(data, 2), "big"),
        "liquidity": int.from_bytes(word(data, 3), "big"),
        "tick": to_int_signed(word(data, 4)),
        "fee": int.from_bytes(word(data, 5)[-3:], "big"),
        "block_number": int(log["blockNumber"], 16),
        "log_index": int(log["logIndex"], 16),
        "tx_hash": log["transactionHash"],
    }


def decode_transfer_log(log: dict) -> dict | None:
    if len(log.get("topics", [])) < 3:
        return None
    return {
        "token": log["address"],
        "from": topic_to_addr(log["topics"][1]),
        "to": topic_to_addr(log["topics"][2]),
        "value": int(log["data"], 16) if log.get("data") and log["data"] != "0x" else 0,
        "tx_hash": log["transactionHash"],
    }


def get_tx_receipt(tx_hash: str) -> dict:
    return rpc("eth_getTransactionReceipt", [tx_hash])


def get_tx_from(tx_hash: str, cache: dict) -> str | None:
    if tx_hash in cache:
        return cache[tx_hash]
    body = rpc("eth_getTransactionByHash", [tx_hash])
    frm = (body.get("result") or {}).get("from")
    cache[tx_hash] = frm
    return frm


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rpc": RPC}

    # 0. Идентичность USDC -- ЧИТАЕМ уже сохранённый результат отдельного
    # eth_call-скрипта, не предполагаем заново.
    usdc_addr = None
    usdc_decimals = None
    if IDENTITY_RESULT_PATH.exists():
        idres = json.loads(IDENTITY_RESULT_PATH.read_text())
        dom = idres.get("currency0_dominant", {})
        if idres.get("conclusion", {}).get("is_confirmed_usdc"):
            usdc_addr = dom.get("address")
            usdc_decimals = dom.get("decimals", {}).get("decoded")
    result["usdc_identity_confirmed"] = usdc_addr is not None
    result["usdc_address"] = usdc_addr
    result["usdc_decimals"] = usdc_decimals
    if usdc_addr is None:
        result["usdc_identity_warning"] = (
            "task_arc_currency0_identity_result.json не найден или не подтвердил USDC -- "
            "объём в USDC НЕ считается для пулов без независимо подтверждённой стороны, "
            "чтобы не выдумывать курс."
        )

    latest = int(rpc("eth_blockNumber", [])["result"], 16)
    from_block = max(0, latest - HOUR_BLOCKS)
    result["window"] = {"from_block": from_block, "to_block": latest, "window_blocks": latest - from_block}

    # 1. Swap-события за последний час. Прошлый прогон показал: чанк=5000
    # (лимит для Initialize) НЕ годится для Swap -- реальная ошибка
    # "query exceeds max results 2000" на плотности Day-0 Arc, с реальной
    # подсказкой RPC "retry with the range X-Y" (~70 блоков). Стартуем с
    # умеренного чанка -- дальше адаптивно подстраивается сам.
    swap_logs, swap_unresolved, swap_scan_stats = chunked_get_logs_with_retry(
        POOL_MANAGER, SWAP_TOPIC0, from_block, latest, chunk=200, max_calls=350)
    swaps = [decode_swap_log(l) for l in swap_logs]
    result["swap_scan"] = {"n_raw_logs": len(swap_logs), "n_decoded": len(swaps),
                            "unresolved_chunks_after_retry": swap_unresolved,
                            "adaptive_chunk_stats": swap_scan_stats}

    # 2. Группировка по pool_id -- объём, число свопов, цена начало/конец.
    by_pool: dict[str, list] = defaultdict(list)
    for s in swaps:
        by_pool[s["pool_id"]].append(s)
    for pid in by_pool:
        by_pool[pid].sort(key=lambda s: (s["block_number"], s["log_index"]))

    # 3. Свежие Initialize-события за то же часовое окно (для пулов,
    # созданных именно в этот час) -- декодируем fee/tickSpacing/hooks,
    # которых не было в прошлом (более широком) прогоне.
    init_logs, init_unresolved, init_scan_stats = chunked_get_logs_with_retry(
        POOL_MANAGER, INIT_TOPIC0, from_block, latest, chunk=5000, max_calls=40)
    fresh_inits = {d["pool_id"]: d for d in (decode_initialize_log(l) for l in init_logs)}
    result["initialize_rescan"] = {"n_found": len(fresh_inits), "unresolved_chunks_after_retry": init_unresolved,
                                    "adaptive_chunk_stats": init_scan_stats}

    # 4. Карта pool_id -> block_number из прошлого 6ч-скана (для пулов
    # постарше часа, но всё ещё торгуемых) -- нужна только чтобы точечно,
    # одним вызовом на блок, забрать их Initialize-лог и узнать fee/hooks.
    prev_block_by_pool: dict[str, int] = {}
    if PREV_POOL_MAP_PATH.exists():
        prev = json.loads(PREV_POOL_MAP_PATH.read_text())
        for p in prev.get("initialize_events", {}).get("pools", []):
            prev_block_by_pool[p["pool_id"]] = p["block_number"]

    def get_pool_meta(pid: str, lookups_budget: list) -> dict | None:
        if pid in fresh_inits:
            return fresh_inits[pid]
        blk = prev_block_by_pool.get(pid)
        if blk is None or lookups_budget[0] <= 0:
            return None
        body = rpc("eth_getLogs", [{"fromBlock": hex(blk), "toBlock": hex(blk),
                                     "address": POOL_MANAGER, "topics": [INIT_TOPIC0, pid]}])
        lookups_budget[0] -= 1
        logs = body.get("result", []) if "error" not in body else []
        if not logs:
            return None
        meta = decode_initialize_log(logs[0])
        fresh_inits[pid] = meta  # кэш, чтобы не спрашивать дважды
        return meta

    # 5. Считаем объём -- только для пулов с подтверждённой USDC-ногой.
    pool_agg = []
    meta_lookup_budget = [60]
    for pid, evs in by_pool.items():
        meta = get_pool_meta(pid, meta_lookup_budget)
        volume_usdc = None
        usdc_side = None
        if usdc_addr and meta:
            c0, c1 = meta["currency0"].lower(), meta["currency1"].lower()
            if c0 == usdc_addr.lower():
                usdc_side = "currency0"
                volume_usdc = sum(abs(s["amount0"]) for s in evs) / (10 ** usdc_decimals)
            elif c1 == usdc_addr.lower():
                usdc_side = "currency1"
                volume_usdc = sum(abs(s["amount1"]) for s in evs) / (10 ** usdc_decimals)

        first_p, last_p = evs[0]["sqrt_price_x96"], evs[-1]["sqrt_price_x96"]
        price_change_frac = (last_p - first_p) / first_p if first_p else None

        last_ev = evs[-1]
        sqrt_p = last_ev["sqrt_price_x96"] / (2 ** 96) if last_ev["sqrt_price_x96"] else None
        reserve_usdc_side_est = None
        if usdc_side and sqrt_p:
            if usdc_side == "currency0":
                reserve_raw = last_ev["liquidity"] / sqrt_p if sqrt_p else None
            else:
                reserve_raw = last_ev["liquidity"] * sqrt_p
            reserve_usdc_side_est = reserve_raw / (10 ** usdc_decimals) if reserve_raw is not None else None

        pool_agg.append({
            "pool_id": pid,
            "currency0": meta["currency0"] if meta else None,
            "currency1": meta["currency1"] if meta else None,
            "fee_initialize": meta["fee"] if meta else None,
            "fee_actual_last_swap": last_ev["fee"],
            "hooks": meta["hooks"] if meta else None,
            "meta_resolved": meta is not None,
            "n_swaps": len(evs),
            "n_distinct_senders_msgsender": len({s["sender"] for s in evs}),
            "usdc_side": usdc_side,
            "volume_usdc": volume_usdc,
            "price_change_frac_sqrtP": price_change_frac,
            "tvl_usdc_side_estimate": reserve_usdc_side_est,
            "sample_tx_hashes": list({s["tx_hash"] for s in evs})[:50],
        })

    ranked = sorted([p for p in pool_agg if p["volume_usdc"] is not None],
                     key=lambda p: p["volume_usdc"], reverse=True)
    unranked_no_usdc_leg = len(pool_agg) - len(ranked)
    top = ranked[:TOP_N]

    # 6. Реальные tx.from для пулов из топа -- дедуп по tx_hash. Первый
    # прогон с общим бюджетом на все 30 пулов честно сломался: пул #1 один
    # забирал весь бюджет (1500+ хешей), остальным 0 -- заменено на
    # СПРАВЕДЛИВЫЙ потолок НА ПУЛ, чтобы каждый из топ-30 получил реальную
    # (пусть частичную) оценку, а не только первый по объёму.
    tx_from_cache: dict[str, str | None] = {}
    total_lookups_used = 0
    for p in top:
        pid = p["pool_id"]
        tx_hashes = list({s["tx_hash"] for s in by_pool[pid]})
        resolved_from = set()
        n_sampled = 0
        for h in tx_hashes:
            if n_sampled >= PER_POOL_TX_FROM_CAP:
                break
            frm = get_tx_from(h, tx_from_cache)
            n_sampled += 1
            total_lookups_used += 1
            if frm:
                resolved_from.add(frm.lower())
        capped = n_sampled < len(tx_hashes)
        p["n_unique_tx_from_resolved"] = len(resolved_from)
        p["n_unique_tx_hashes_total"] = len(tx_hashes)
        p["n_tx_hashes_sampled_for_from"] = n_sampled
        p["tx_from_capped"] = capped
        # "Похоже на накрутку" -- ТОЛЬКО если реально видели весь набор
        # tx_hash (не капнуто) ИЛИ выборка была достаточно большой (>=20),
        # чтобы <=3 уникальных не было артефактом маленькой выборки.
        p["is_likely_wash"] = len(resolved_from) <= 3 and (not capped or n_sampled >= 20)

    result["pools_hourly"] = {
        "n_pools_with_swaps": len(pool_agg),
        "n_ranked_by_usdc_volume": len(ranked),
        "n_excluded_no_confirmed_usdc_leg": unranked_no_usdc_leg,
        "top_pools": top,
        "tx_from_lookups_used": total_lookups_used,
        "per_pool_tx_from_cap": PER_POOL_TX_FROM_CAP,
    }

    # 7. День-ноль арбитраж: транзакции с ≥2 свопами в РАЗНЫХ пулах.
    tx_pools: dict[str, set] = defaultdict(set)
    tx_swap_count: dict[str, int] = defaultdict(int)
    for s in swaps:
        tx_pools[s["tx_hash"]].add(s["pool_id"])
        tx_swap_count[s["tx_hash"]] += 1
    multi_pool_txs = [h for h, pools in tx_pools.items() if len(pools) >= 2]

    # Равномерная выборка по всему часу (по порядку появления в свопах,
    # который примерно хронологический) -- НЕ только первые N по времени,
    # иначе оценка смещена в начало окна.
    if len(multi_pool_txs) > MAX_ARB_RECEIPT_LOOKUPS:
        stride = len(multi_pool_txs) / MAX_ARB_RECEIPT_LOOKUPS
        sample_idx = sorted({int(i * stride) for i in range(MAX_ARB_RECEIPT_LOOKUPS)})
        sampled_txs = [multi_pool_txs[i] for i in sample_idx]
    else:
        sampled_txs = multi_pool_txs

    arb_candidates = []
    receipts_used = 0
    for h in sampled_txs:
        if receipts_used >= MAX_ARB_RECEIPT_LOOKUPS:
            break
        rec = get_tx_receipt(h)
        receipts_used += 1
        r = rec.get("result")
        if not r:
            continue
        initiator = r.get("from", "").lower()
        transfers = [decode_transfer_log(l) for l in r.get("logs", []) if l.get("topics", [None])[0] == TRANSFER_TOPIC0]
        transfers = [t for t in transfers if t]
        net: dict[str, int] = defaultdict(int)
        for t in transfers:
            if t["from"].lower() == initiator:
                net[t["token"].lower()] -= t["value"]
            if t["to"].lower() == initiator:
                net[t["token"].lower()] += t["value"]
        positive_tokens = {tok: v for tok, v in net.items() if v > 0}
        # Владелец: "замкнутые циклы с ПЛЮСОМ В ОДНОМ токене" -- ровно один
        # положительный токен, остальные <=0 (не любой набор с хотя бы одним).
        is_closed_profit_loop = len(positive_tokens) == 1 and len(net) >= 2 and all(
            v <= 0 for tok, v in net.items() if tok not in positive_tokens
        )
        arb_candidates.append({
            "tx_hash": h, "initiator_from": initiator, "n_pools_touched": len(tx_pools[h]),
            "n_swaps_in_tx": tx_swap_count[h], "net_balance_by_token": net,
            "is_closed_profit_loop": is_closed_profit_loop,
        })

    closed_loops = [a for a in arb_candidates if a["is_closed_profit_loop"]]
    result["arb_day_zero"] = {
        "n_multi_pool_txs_found": len(multi_pool_txs),
        "n_receipts_examined": receipts_used,
        "n_receipts_capped": len(multi_pool_txs) - receipts_used if len(multi_pool_txs) > receipts_used else 0,
        "n_closed_profit_loops": len(closed_loops),
        "closed_loop_executors": sorted({a["initiator_from"] for a in closed_loops}),
        "closed_loops_sample": closed_loops[:20],
        "window_is_zero_if_n_closed_loops_is_0": len(closed_loops) == 0,
    }

    # 8. aka.fun-хук -- наиболее частый hooks-адрес среди пулов топа (или
    # среди всех meta-resolved пулов, если топ маловат). Ищем реальный
    # fee-recipient через Transfer-логи реальных свопов на этом хуке.
    hook_counts: dict[str, int] = defaultdict(int)
    for p in pool_agg:
        if p["hooks"] and p["hooks"] != "0x0000000000000000000000000000000000000000":
            hook_counts[p["hooks"]] += 1
    dominant_hook = max(hook_counts, key=hook_counts.get) if hook_counts else None

    hook_info: dict = {"address": dominant_hook, "n_pools_using_it_in_window": hook_counts.get(dominant_hook, 0)}
    if dominant_hook:
        code = rpc("eth_getCode", [dominant_hook, "latest"])["result"]
        hook_info["code_len_bytes"] = (len(code) - 2) // 2 if code else 0
        addr_int = int(dominant_hook, 16)
        flag_bits = {
            "BEFORE_SWAP": 6, "AFTER_SWAP": 7,
            "BEFORE_SWAP_RETURNS_DELTA": 10, "AFTER_SWAP_RETURNS_DELTA": 11,
        }
        hook_info["permission_flags"] = {name: bool((addr_int >> bit) & 1) for name, bit in flag_bits.items()}

        sample_pools_with_hook = [p for p in pool_agg if p["hooks"] == dominant_hook][:5]
        fee_recipient_candidates: dict[str, int] = defaultdict(int)
        base_fee_vs_actual = []
        for p in sample_pools_with_hook:
            for h in p["sample_tx_hashes"][:3]:
                rec = get_tx_receipt(h)
                r = rec.get("result")
                if not r:
                    continue
                pool_participants = {POOL_MANAGER.lower(), (r.get("from") or "").lower()}
                for l in r.get("logs", []):
                    if l.get("topics", [None])[0] != TRANSFER_TOPIC0:
                        continue
                    t = decode_transfer_log(l)
                    if t and t["to"].lower() not in pool_participants:
                        fee_recipient_candidates[t["to"].lower()] += 1
            base_fee_vs_actual.append({
                "pool_id": p["pool_id"], "fee_initialize": p["fee_initialize"],
                "fee_actual_last_swap": p["fee_actual_last_swap"],
            })
        hook_info["fee_recipient_candidates_by_recurrence"] = dict(
            sorted(fee_recipient_candidates.items(), key=lambda kv: kv[1], reverse=True)[:5]
        )
        hook_info["base_fee_vs_actual_swap_fee_sample"] = base_fee_vs_actual
        hook_info["note"] = (
            "fee_recipient_candidates -- адреса-получатели Transfer, не являющиеся PoolManager "
            "или initiator'ом, повторяющиеся в реальных receipts свопов на этом хуке. Не окончательный "
            "вывод без ручной проверки контракта-получателя (может быть ещё один внутренний контракт хука)."
        )

    result["aka_fun_hook_hypothesis"] = hook_info

    # 9. Fee/LVR для топ-10 (формула LVR = sigma^2/8 из pool_screener_sigma_lvr.py,
    # здесь sigma оценивается как |изменение цены за час| -- честная
    # почасовая аппроксимация, не годовая волатильность).
    fee_lvr = []
    for p in top[:10]:
        if p["volume_usdc"] is None or p["fee_initialize"] is None:
            continue
        fee_frac = p["fee_initialize"] / 1_000_000  # V4 fee -- units из 1e6 (pips)
        fee_revenue_usdc = p["volume_usdc"] * fee_frac
        sigma_hourly = abs(p["price_change_frac_sqrtP"]) if p["price_change_frac_sqrtP"] is not None else None
        lvr_frac_of_tvl_hourly = (sigma_hourly ** 2) / 8 if sigma_hourly is not None else None
        lvr_usdc = (lvr_frac_of_tvl_hourly * p["tvl_usdc_side_estimate"]
                    if lvr_frac_of_tvl_hourly is not None and p["tvl_usdc_side_estimate"] else None)
        ratio = (fee_revenue_usdc / lvr_usdc) if lvr_usdc else None
        fee_lvr.append({
            "pool_id": p["pool_id"], "fee_tier_pips": p["fee_initialize"],
            "volume_usdc_1h": p["volume_usdc"], "fee_revenue_usdc_1h": fee_revenue_usdc,
            "price_change_frac_1h": p["price_change_frac_sqrtP"],
            "lvr_frac_of_tvl_1h_formula_sigma2_over_8": lvr_frac_of_tvl_hourly,
            "tvl_usdc_side_estimate": p["tvl_usdc_side_estimate"],
            "lvr_usdc_1h_estimate": lvr_usdc,
            "fee_over_lvr_ratio": ratio,
        })
    result["fee_lvr_top10"] = fee_lvr

    result["total_rpc_calls_used"] = _rpc_call_count

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    DATA_DIR.joinpath("task_arc_hour_zero_audit_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
