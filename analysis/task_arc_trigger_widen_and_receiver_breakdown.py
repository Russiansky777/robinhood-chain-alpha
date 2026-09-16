#!/usr/bin/env python3
"""Два узких хвоста прошлого прогона (task_arc_single_tx_and_bot_pattern.py):

1) 5 из 20 tx исполнителя 0x608750874f... не нашли триггер в 53 блоках
   назад -- расширить до 500 блоков, и если не нашлось Swap, посмотреть
   ModifyLiquidity/Initialize того же пула (не только Swap).
2) Кошелёк-получатель 0x47e79...20cc -- 200 последних входящих Transfer:
   сколько от PoolManager напрямую (арбитраж), сколько от прочих
   контрактов/EOA. eth_getCode на топ-10 отправителей.

Всё целевое (по конкретным pool_id/адресам), не часовые сканы."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
PROFIT_RECEIVER = "0x47e7936ae9891e61c5123db720593c05de7120cc"
DATA_DIR = Path(__file__).parent.parent.joinpath("data")

# 5 не-найденных из прошлого прогона (data/task_arc_single_tx_and_bot_pattern_result.json)
UNRESOLVED = [
    {"bot_tx_hash": "0x984ed264a8ec2b68a55040d5187612151349e71973bedc4bf444274ad5b7095c",
     "bot_block": 21212654, "pool_id": "0xd9133187331e20885fd1b4ff2f18b892f94793ddaf808b1db6d4ef29ca58b943"},
    {"bot_tx_hash": "0x44412ed7331649b07f51cd572e8d48ec195528e7daf88d7e2145e304d19ddfdf",
     "bot_block": 21212574, "pool_id": "0x6537c4d251a1d6084b674445c08315583763d91a9a93ecdfdd5c176c98213ac5"},
    {"bot_tx_hash": "0xafc79b78df08a4847b8f823617f0eceb868163920c639cf33fea0d7e7eb61c80",
     "bot_block": 21212490, "pool_id": "0xeb0fd02fb8044d5514fb6e165ee134fd547eff0378bb33b76f4b81d8b03bd1ae"},
    {"bot_tx_hash": "0xe1c900996d6fb806221cc497245a77da45529339074102ce656c0e5a4e220788",
     "bot_block": 21212427, "pool_id": "0xb6e2e17f0d9a2f49713d685cd72268b8c7d61b37bfa255dee728efa4a80725ed"},
    {"bot_tx_hash": "0xa0dc2d2e2a6d5d3a6000773490d6319fb61e11eefc3ead23e87c02b5bf36341c",
     "bot_block": 21212398, "pool_id": "0x4cd60ec63898be4e2ae7922f66d4bfaca7fd9e1d131338cb7e87e248d366e82c"},
]


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
INIT_TOPIC0 = keccak_topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
MODIFY_LIQ_TOPIC0 = keccak_topic0("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")
TRANSFER_TOPIC0 = keccak_topic0("Transfer(address,address,uint256)")

_rpc_calls = 0


def rpc(method: str, params: list, timeout: int = 25) -> dict:
    global _rpc_calls
    for attempt in range(5):
        _rpc_calls += 1
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
        if err and (err.get("code") == -32005 or "rate limit" in str(err.get("message", "")).lower()):
            if attempt == 4:
                return body
            time.sleep(2 * (attempt + 1))
            continue
        return body
    return {"error": {"message": "unreachable"}}


def addr_topic(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def topic_to_addr(t: str) -> str:
    return "0x" + t[-40:]


def get_logs_adaptive(params_base: dict, from_block: int, to_block: int) -> tuple[list, list]:
    """Простой адаптивный чанкер под конкретный pool_id-фильтр (низкая
    плотность на один пул ожидается, но на всякий случай -- сужение при
    ошибке размера/результатов, честный потолок 15 вызовов на диапазон."""
    out, unresolved = [], []
    block = from_block
    chunk = to_block - from_block + 1
    n_calls = 0
    while block <= to_block and n_calls < 15:
        end = min(block + chunk - 1, to_block)
        body = rpc("eth_getLogs", [{**params_base, "fromBlock": hex(block), "toBlock": hex(end)}])
        n_calls += 1
        if "error" in body:
            if chunk <= 20:
                unresolved.append({"from": block, "to": end, "error": body["error"]})
                block = end + 1
                continue
            chunk = max(20, chunk // 4)
            continue
        out.extend(body.get("result", []))
        block = end + 1
    return out, unresolved


def decode_transfer(log: dict) -> dict | None:
    if len(log.get("topics", [])) < 3:
        return None
    return {"token": log["address"].lower(), "from": topic_to_addr(log["topics"][1]).lower(),
            "to": topic_to_addr(log["topics"][2]).lower(),
            "value": int(log["data"], 16) if log.get("data") and log["data"] != "0x" else 0,
            "block": int(log["blockNumber"], 16), "tx_hash": log["transactionHash"]}


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rpc": RPC}

    # === Вопрос 1: расширить поиск триггера до 500 блоков + ModifyLiquidity ===
    q1_rows = []
    for u in UNRESOLVED:
        pool_id = u["pool_id"]
        bot_block = u["bot_block"]
        lo, hi = max(0, bot_block - 500), max(0, bot_block - 4)
        swap_logs, _ = get_logs_adaptive({"address": POOL_MANAGER, "topics": [SWAP_TOPIC0, pool_id]}, lo, hi)
        init_logs, _ = get_logs_adaptive({"address": POOL_MANAGER, "topics": [INIT_TOPIC0, pool_id]}, lo, hi)
        modliq_logs, _ = get_logs_adaptive({"address": POOL_MANAGER, "topics": [MODIFY_LIQ_TOPIC0, pool_id]}, lo, hi)

        candidates = []
        for l in swap_logs:
            if l["transactionHash"] == u["bot_tx_hash"]:
                continue
            candidates.append(("swap", int(l["blockNumber"], 16), int(l["transactionIndex"], 16), l["transactionHash"]))
        for l in init_logs:
            candidates.append(("initialize", int(l["blockNumber"], 16), int(l["transactionIndex"], 16), l["transactionHash"]))
        for l in modliq_logs:
            candidates.append(("modify_liquidity", int(l["blockNumber"], 16), int(l["transactionIndex"], 16), l["transactionHash"]))

        candidates.sort(key=lambda c: (c[1], c[2]), reverse=True)
        best = candidates[0] if candidates else None
        q1_rows.append({
            "bot_tx_hash": u["bot_tx_hash"], "bot_block": bot_block, "pool_id": pool_id,
            "n_swap_found_500back": len(swap_logs), "n_initialize_found_500back": len(init_logs),
            "n_modify_liquidity_found_500back": len(modliq_logs),
            "trigger_type": best[0] if best else "не найдено ни одного события (Swap/Initialize/ModifyLiquidity) в 500 блоках назад",
            "trigger_block": best[1] if best else None, "block_delta": (bot_block - best[1]) if best else None,
            "trigger_tx_hash": best[3] if best else None,
        })

    n_found_further = sum(1 for r in q1_rows if r["trigger_block"] is not None)
    result["question1_widen_to_500"] = {
        "n_resolved_of_5": n_found_further,
        "summary_line": (f"триггер найден дальше 53 блоков у {n_found_further} из 5"
                          if n_found_further > 0 else
                          "триггера нет ни у одного из 5 в пределах 500 блоков и 3 типов событий — похоже, это не прямая реакция на локальное событие пула"),
        "rows": q1_rows,
    }

    # === Вопрос 2: 200 последних входящих Transfer на получателя ===
    latest = int(rpc("eth_blockNumber", [])["result"], 16)
    found = []
    to_block = latest
    chunk = 20000
    n_calls_scan = 0
    MAX_CALLS = 60
    while len(found) < 200 and to_block > 0 and n_calls_scan < MAX_CALLS:
        from_block = max(0, to_block - chunk)
        body = rpc("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(to_block),
                                     "topics": [TRANSFER_TOPIC0, None, addr_topic(PROFIT_RECEIVER)]}])
        n_calls_scan += 1
        if "error" in body:
            err = body["error"]
            code = err.get("code")
            if code in (-32012, -32602) or "too large" in str(err.get("message", "")).lower() or "max results" in str(err.get("message", "")).lower():
                chunk = max(500, chunk // 3)
                continue
            break
        logs = body.get("result", [])
        found.extend(t for t in (decode_transfer(l) for l in logs) if t)
        to_block = from_block - 1

    found.sort(key=lambda t: t["block"], reverse=True)
    last200 = found[:200]

    from_pool_manager = [t for t in last200 if t["from"] == POOL_MANAGER.lower()]
    from_other = [t for t in last200 if t["from"] != POOL_MANAGER.lower()]

    sum_by_source_token: dict = defaultdict(lambda: defaultdict(int))
    for t in from_pool_manager:
        sum_by_source_token["from_pool_manager"][t["token"]] += t["value"]
    for t in from_other:
        sum_by_source_token["from_other"][t["token"]] += t["value"]

    sender_counts = defaultdict(int)
    sender_volume_native = defaultdict(int)
    NATIVE = "0xfffffffffffffffffffffffffffffffffffffffe"
    for t in from_other:
        sender_counts[t["from"]] += 1
        if t["token"] == NATIVE:
            sender_volume_native[t["from"]] += t["value"]

    top10_by_volume = sorted(sender_volume_native.items(), key=lambda kv: kv[1], reverse=True)[:10]
    top10_addrs = [a for a, _ in top10_by_volume]
    if len(top10_addrs) < 10:
        for a, _ in sorted(sender_counts.items(), key=lambda kv: kv[1], reverse=True):
            if a not in top10_addrs:
                top10_addrs.append(a)
            if len(top10_addrs) >= 10:
                break

    KNOWN_EXECUTORS = {
        "0x608750874fdcbcc21f4f9610af48e1babacf10fe": "исполнитель из детектора циклов (225/час)",
        "0xe1eee09af59990bdff17582467f5ddbef66c4bbd": "исполнитель из детектора циклов (37 всего)",
        "0x147cd06a76c37959c25c7d1f763aa01e7992facf": "исполнитель из детектора циклов (4)",
        "0xbe3705802aa0a1b857da82cece8d7948cbd0e8f4": "исполнитель из детектора циклов (1)",
    }
    top10_info = []
    for addr in top10_addrs:
        code = rpc("eth_getCode", [addr, "latest"]).get("result")
        top10_info.append({
            "address": addr, "n_transfers_in_200": sender_counts.get(addr, 0),
            "native_wrap_volume_raw": sender_volume_native.get(addr, 0),
            "is_contract": bool(code) and code != "0x",
            "code_len_bytes": (len(code) - 2) // 2 if code else 0,
            "known_as": KNOWN_EXECUTORS.get(addr, "неизвестно -- не входит в найденных ранее исполнителей"),
        })

    total_native_in_200 = sum(t["value"] for t in last200 if t["token"] == NATIVE)
    pm_native_in_200 = sum(t["value"] for t in from_pool_manager if t["token"] == NATIVE)
    frac_from_pm = (pm_native_in_200 / total_native_in_200) if total_native_in_200 else None

    result["question2_receiver_breakdown"] = {
        "receiver": PROFIT_RECEIVER, "n_calls_backward_scan": n_calls_scan,
        "n_found_total_backward_scan": len(found), "n_analyzed": len(last200),
        "n_from_pool_manager_direct": len(from_pool_manager), "n_from_other": len(from_other),
        "sum_raw_from_pool_manager_by_token": dict(sum_by_source_token["from_pool_manager"]),
        "sum_raw_from_other_by_token": dict(sum_by_source_token["from_other"]),
        "frac_native_wrap_value_from_pool_manager_in_200": frac_from_pm,
        "top10_other_senders": top10_info,
        "summary_line": f"из 200 входящих: от PoolManager {len(from_pool_manager)} (сумма {pm_native_in_200/1e18:.4f} native-wrap), прочее {len(from_other)}",
        "address_assessment": (
            "агрегатор/касса НЕСКОЛЬКИХ ботов" if len({i['address'] for i in top10_info if i['is_contract']}) >= 3
            else "касса одного бота или узкой группы" if len({i['address'] for i in top10_info if i['is_contract']}) <= 2
            else "неоднозначно"
        ),
    }

    result["total_rpc_calls_used"] = _rpc_calls
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    DATA_DIR.joinpath("task_arc_trigger_widen_and_receiver_breakdown_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
