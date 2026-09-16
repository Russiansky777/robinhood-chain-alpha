#!/usr/bin/env python3
"""Владелец: старый замер прибыли ($70/час) был неверен -- считали остаток
на исполнителе, а прибыль реально уходит получателю в той же tx. ОДИН
узкий вопрос, без часовых сканов:

1) разобрать tx 0xcc9a...ec1d -- цикл или снайп нового токена;
2) 20 последних Swap этого же исполнителя -- на что реагировал (тот же
   блок / +1 / 2+), позиция в блоке;
3) для 10 его реальных tx (из реального txlist обозревателя, если он
   даёт список, не выдумываем reverted-список вручную) -- что стоит
   прямо перед ним в блоке;
4) приток на кошелёк-получатель за 24ч -- сумма по токенам + число tx.

Все запросы -- целевые (по конкретному адресу/pool_id/tx), НЕ полный
скан логов за час/сутки без фильтра."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
EXPLORER_API = "https://arc-scan.org/api"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
TARGET_TX = "0xcc9a463b72ffbc0050d53ddbe7aaa8193ca0f94fdd34b800bf143298a6c2ec1d"
PROFIT_RECEIVER = "0x47e7936ae9891e61c5123db720593c05de7120cc"
BLOCK_TIME_S = 0.506

DATA_DIR = Path(__file__).parent.parent.joinpath("data")


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


INIT_TOPIC0 = keccak_topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
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


def word(data_bytes: bytes, i: int) -> bytes:
    return data_bytes[i * 32:(i + 1) * 32]


def to_int_signed(w: bytes) -> int:
    v = int.from_bytes(w, "big")
    return v - 2 ** 256 if v >= 2 ** 255 else v


def decode_swap(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {"pool_id": log["topics"][1], "sender": topic_to_addr(log["topics"][2]),
            "amount0": to_int_signed(word(data, 0)), "amount1": to_int_signed(word(data, 1)),
            "block_number": int(log["blockNumber"], 16), "tx_hash": log["transactionHash"],
            "log_index": int(log["logIndex"], 16),
            "transactionIndex": int(log["transactionIndex"], 16)}


def decode_transfer(log: dict) -> dict | None:
    if len(log.get("topics", [])) < 3:
        return None
    return {"token": log["address"].lower(), "from": topic_to_addr(log["topics"][1]).lower(),
            "to": topic_to_addr(log["topics"][2]).lower(),
            "value": int(log["data"], 16) if log.get("data") and log["data"] != "0x" else 0}


def get_logs(params: dict) -> tuple[list, dict | None]:
    body = rpc("eth_getLogs", [params])
    if "error" in body:
        return [], body["error"]
    return body.get("result", []), None


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rpc": RPC}

    # === 1. Разбор целевой транзакции ===
    receipt = rpc("eth_getTransactionReceipt", [TARGET_TX]).get("result")
    tx = rpc("eth_getTransactionByHash", [TARGET_TX]).get("result") or {}
    swap_logs = [l for l in (receipt or {}).get("logs", []) if l.get("topics", [None])[0] == SWAP_TOPIC0]
    init_logs = [l for l in (receipt or {}).get("logs", []) if l.get("topics", [None])[0] == INIT_TOPIC0]
    transfer_logs = [t for t in (decode_transfer(l) for l in (receipt or {}).get("logs", [])
                                  if l.get("topics", [None])[0] == TRANSFER_TOPIC0) if t]
    legs = [decode_swap(l) for l in swap_logs]
    executor = legs[0]["sender"] if legs else None
    tx_from = (receipt or {}).get("from")

    to_receiver = [t for t in transfer_logs if t["to"] == PROFIT_RECEIVER.lower()]
    profit_by_token = defaultdict(int)
    for t in to_receiver:
        profit_by_token[t["token"]] += t["value"]

    # Топология: замкнутый цикл, если >=2 плеча и знак amount0/amount1
    # даёт путь, возвращающийся к тому же токену (используем реальные
    # Transfer, не событийные величины -- см. прошлый прогон, хук искажает
    # событийную величину до ~75%). Простая проверка: если >=2 Swap-лога
    # в receipt -- вероятный цикл; если ровно 1 -- одиночная покупка
    # (снайп, если это первая активность на только что созданном пуле).
    is_snipe_candidate = len(legs) == 1
    pool_created_in_same_tx = len(init_logs) > 0

    # Реальная проверка цикла -- по net-flow трейдера на РЕАЛЬНЫХ Transfer
    # (не по знакам событий Swap, которые хук может исказить по величине,
    # см. task_arc_sign_convention_check_result.json), а не по числу плеч.
    trader_addrs = {a.lower() for a in (executor, tx_from) if a}
    trader_net = defaultdict(int)
    trader_vol = defaultdict(int)
    for t in transfer_logs:
        if t["from"] in trader_addrs:
            trader_net[t["token"]] -= t["value"]
            trader_vol[t["token"]] += t["value"]
        if t["to"] in trader_addrs:
            trader_net[t["token"]] += t["value"]
            trader_vol[t["token"]] += t["value"]
    positive_tokens = {tok: v for tok, v in trader_net.items() if v > 0}
    near_zero_tokens = {tok: v for tok, v in trader_net.items()
                         if tok not in positive_tokens and trader_vol.get(tok, 0) > 0 and abs(v) <= 1e-9 * trader_vol[tok]}
    negative_tokens = {tok: v for tok, v in trader_net.items() if v < 0 and tok not in near_zero_tokens}

    if is_snipe_candidate:
        cycle_verdict = ("СНАЙП нового токена (1 плечо, пул создан в этой же tx)" if pool_created_in_same_tx
                          else "СНАЙП уже существующего пула (1 плечо, пул создан раньше)")
    elif len(positive_tokens) == 1 and len(near_zero_tokens) >= 1:
        cycle_verdict = (f"ЗАМКНУТЫЙ ЦИКЛ, подтверждено реальными Transfer: ровно один токен в плюсе у трейдера "
                          f"({list(positive_tokens.keys())[0]}), {len(near_zero_tokens)} промежуточных ~0")
    elif len(positive_tokens) == 1 and not near_zero_tokens and negative_tokens:
        cycle_verdict = (f"НЕ чистый цикл -- один токен в плюсе ({list(positive_tokens.keys())[0]}), но нет "
                          f"промежуточного токена с net~0 (трейдер реально потратил {list(negative_tokens.keys())} "
                          f"без возврата) -- похоже на прямую покупку через {len(legs)} плеч, не на арбитраж")
    else:
        cycle_verdict = f"неоднозначно по реальным Transfer -- {len(legs)} плеч, positive={positive_tokens}, negative={negative_tokens}"

    result["part1_target_tx"] = {
        "tx_hash": TARGET_TX, "block_number": int(tx.get("blockNumber", "0x0"), 16) if tx.get("blockNumber") else None,
        "status": (receipt or {}).get("status"), "n_swap_legs": len(legs), "n_initialize_in_same_tx": len(init_logs),
        "executor_sender": executor, "tx_from": tx_from,
        "pools_touched": sorted({l["pool_id"] for l in legs}),
        "profit_to_receiver_raw_by_token": dict(profit_by_token),
        "n_transfers_to_receiver_in_this_tx": len(to_receiver),
        "trader_net_by_token_real_transfers": dict(trader_net),
        "classification": cycle_verdict,
        "all_transfer_logs_raw": transfer_logs,
    }

    if executor is None:
        result["fatal"] = "Не удалось декодировать ни одного Swap в целевой tx -- receipt/tx не найдены или другая структура."
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        DATA_DIR.joinpath("task_arc_single_tx_and_bot_pattern_result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # === 2. Последние 20 Swap этого исполнителя (targeted, не hour-scan) ===
    latest = int(rpc("eth_blockNumber", [])["result"], 16)
    found = []
    to_block = latest
    chunk = 20000
    n_calls_p2 = 0
    MAX_CALLS_P2 = 60
    while len(found) < 20 and to_block > 0 and n_calls_p2 < MAX_CALLS_P2:
        from_block = max(0, to_block - chunk)
        logs, err = get_logs({"fromBlock": hex(from_block), "toBlock": hex(to_block),
                               "address": POOL_MANAGER, "topics": [SWAP_TOPIC0, None, addr_topic(executor)]})
        n_calls_p2 += 1
        if err:
            code = err.get("code")
            if code == -32012 or "too large" in str(err.get("message", "")).lower():
                chunk = max(1000, chunk // 2)
                continue
            if code == -32602 or "max results" in str(err.get("message", "")).lower():
                chunk = max(200, chunk // 4)
                continue
            break
        found.extend(decode_swap(l) for l in logs)
        to_block = from_block - 1
    found.sort(key=lambda s: (s["block_number"], s["transactionIndex"]), reverse=True)
    last20 = found[:20]

    trigger_rows = []
    n_calls_p2b = 0
    for s in last20:
        pool_id = s["pool_id"]
        bot_block = s["block_number"]
        trigger = None
        for lo, hi in ((max(0, bot_block - 3), bot_block), (max(0, bot_block - 50), max(0, bot_block - 4))):
            if lo > hi:
                continue
            swap_evts, _ = get_logs({"fromBlock": hex(lo), "toBlock": hex(hi), "address": POOL_MANAGER,
                                      "topics": [SWAP_TOPIC0, pool_id]})
            n_calls_p2b += 1
            init_evts, _ = get_logs({"fromBlock": hex(lo), "toBlock": hex(hi), "address": POOL_MANAGER,
                                      "topics": [INIT_TOPIC0, pool_id]})
            n_calls_p2b += 1
            candidates = []
            for l in swap_evts:
                d = decode_swap(l)
                if d["tx_hash"] == s["tx_hash"]:
                    continue
                if d["block_number"] == bot_block and d["transactionIndex"] >= s["transactionIndex"]:
                    continue  # не раньше нашей tx в том же блоке
                candidates.append(("human_or_bot_swap", d["block_number"], d["transactionIndex"], d["sender"], d["tx_hash"]))
            for l in init_evts:
                blk = int(l["blockNumber"], 16)
                txidx = int(l["transactionIndex"], 16)
                if blk == bot_block and txidx >= s["transactionIndex"]:
                    continue
                candidates.append(("initialize", blk, txidx, None, l["transactionHash"]))
            if candidates:
                candidates.sort(key=lambda c: (c[1], c[2]), reverse=True)
                trigger = candidates[0]
                break
        delta = (bot_block - trigger[1]) if trigger else None
        trigger_rows.append({
            "bot_tx_hash": s["tx_hash"], "bot_block": bot_block, "bot_transactionIndex": s["transactionIndex"],
            "pool_id": pool_id,
            "trigger_type": trigger[0] if trigger else "не найден в пределах 53 блоков назад",
            "trigger_block": trigger[1] if trigger else None, "trigger_transactionIndex": trigger[2] if trigger else None,
            "trigger_tx_hash": trigger[4] if trigger else None,
            "block_delta": delta,
        })

    delta_buckets = defaultdict(int)
    for r in trigger_rows:
        if r["block_delta"] is None:
            delta_buckets["не_найден"] += 1
        elif r["block_delta"] == 0:
            delta_buckets["0_тот_же_блок"] += 1
        elif r["block_delta"] == 1:
            delta_buckets["+1"] += 1
        else:
            delta_buckets["2+"] += 1

    result["part2_last20_pattern"] = {
        "executor": executor, "n_found_total_backward_scan": len(found), "n_calls_backward_scan": n_calls_p2,
        "n_calls_trigger_lookup": n_calls_p2b, "n_analyzed": len(last20),
        "block_delta_distribution": dict(delta_buckets), "rows": trigger_rows,
    }

    # === 3. 10 реальных tx исполнителя из explorer API (не выдумываем reverted-список) ===
    part3 = {"explorer_api_tried": f"{EXPLORER_API}?module=account&action=txlist&address={executor}"}
    try:
        r = requests.get(EXPLORER_API, params={"module": "account", "action": "txlist", "address": executor,
                                                 "sort": "desc"}, timeout=15)
        part3["http_status"] = r.status_code
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") or r.text.strip().startswith("{") else None
        part3["api_status_field"] = body.get("status") if body else None
        txlist = body.get("result") if body and isinstance(body.get("result"), list) else None
        part3["api_usable"] = txlist is not None
        if txlist is not None:
            reverted = [t for t in txlist if str(t.get("isError", "0")) == "1"][:10]
            part3["n_reverted_found"] = len(reverted)
            rows3 = []
            n_bots_ahead = 0
            block_cache = {}
            for rt in reverted:
                blk_num = int(rt["blockNumber"])
                tx_idx = int(rt.get("transactionIndex", "-1"))
                if blk_num not in block_cache:
                    block_cache[blk_num] = rpc("eth_getBlockByNumber", [hex(blk_num), True]).get("result")
                blk = block_cache[blk_num]
                preceding = None
                if blk and tx_idx > 0:
                    txs_in_block = blk.get("transactions", [])
                    if tx_idx - 1 < len(txs_in_block):
                        preceding = txs_in_block[tx_idx - 1]
                row = {"reverted_tx": rt.get("hash"), "block": blk_num, "transactionIndex": tx_idx}
                if preceding:
                    prec_hash = preceding["hash"]
                    prec_receipt = rpc("eth_getTransactionReceipt", [prec_hash]).get("result") or {}
                    prec_swaps = [l for l in prec_receipt.get("logs", []) if l.get("topics", [None])[0] == SWAP_TOPIC0]
                    is_bot = len(prec_swaps) >= 2
                    if is_bot:
                        n_bots_ahead += 1
                    row.update({"preceding_tx": prec_hash, "preceding_to": preceding.get("to"),
                                "preceding_n_swap_legs": len(prec_swaps),
                                "classification": "другой арбитражный бот (>=2 плеча)" if is_bot
                                                   else ("обычный своп через роутер (1 плечо)" if len(prec_swaps) == 1
                                                         else "не своп (0 Swap-логов)")})
                else:
                    row["classification"] = "нет предыдущей tx в блоке (transactionIndex=0) или блок не получен"
                rows3.append(row)
            part3["rows"] = rows3
            part3["summary_line"] = f"обгоняют боты {n_bots_ahead} из {len(rows3)}, цена ушла от людей {sum(1 for r in rows3 if r.get('classification','').startswith('обычный своп')) } из {len(rows3)}"
        else:
            part3["note"] = "API не вернул пригодный список (не JSON с result-list) -- реальный список reverted tx НЕ построен. Цена альтернативы: обратный посблочный скан без индексатора неизвестного объёма (могут понадобиться тысячи eth_getBlockByNumber, т.к. нет способа искать по address без индексатора/логов, а reverted tx логов не оставляют)."
    except Exception as exc:  # noqa: BLE001
        part3["exception"] = f"{type(exc).__name__}: {exc}"
        part3["note"] = "Обозреватель API недоступен из этого запроса -- реальный список reverted tx НЕ построен, см. цену альтернативы выше."
    result["part3_reverted_pattern"] = part3

    # === 4. Приток на кошелёк-получатель за 24ч (targeted address-filtered log scan) ===
    window_blocks = int(24 * 3600 / BLOCK_TIME_S)
    from_block_24h = max(0, latest - window_blocks)
    inflow_by_token = defaultdict(int)
    inflow_tx_hashes = set()
    n_calls_p4 = 0
    unresolved_p4 = []
    block = from_block_24h
    chunk4 = 5000
    MAX_CALLS_P4 = 80
    while block <= latest and n_calls_p4 < MAX_CALLS_P4:
        end = min(block + chunk4 - 1, latest)
        logs, err = get_logs({"fromBlock": hex(block), "toBlock": hex(end),
                               "topics": [TRANSFER_TOPIC0, None, addr_topic(PROFIT_RECEIVER)]})
        n_calls_p4 += 1
        if err:
            code = err.get("code")
            if code in (-32012, -32602) or "too large" in str(err.get("message", "")).lower() or "max results" in str(err.get("message", "")).lower():
                if chunk4 <= 200:
                    unresolved_p4.append({"from": block, "to": end, "error": err})
                    block = end + 1
                    continue
                chunk4 = max(200, chunk4 // 3)
                continue
            unresolved_p4.append({"from": block, "to": end, "error": err})
            block = end + 1
            continue
        for l in logs:
            t = decode_transfer(l)
            if t:
                inflow_by_token[t["token"]] += t["value"]
                inflow_tx_hashes.add(l["transactionHash"])
        block = end + 1

    result["part4_receiver_24h_inflow"] = {
        "receiver": PROFIT_RECEIVER, "window_blocks": window_blocks,
        "from_block": from_block_24h, "to_block": latest,
        "n_calls_used": n_calls_p4, "unresolved_ranges": unresolved_p4,
        "sum_raw_by_token": dict(inflow_by_token), "n_incoming_transfer_txs": len(inflow_tx_hashes),
        "note": "Только ERC20/обёрнутые Transfer-логи (включая native-USDC-обёртку 0xffff...fe). "
                "Прямые нативные переводы (без Transfer-события) этим методом не видны -- если нужны, "
                "цена: обозреватель API 'txlist' по адресу получателя за 24ч (то же, что в п.3).",
    }

    result["total_rpc_calls_used"] = _rpc_calls
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    DATA_DIR.joinpath("task_arc_single_tx_and_bot_pattern_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
