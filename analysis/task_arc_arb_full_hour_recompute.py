#!/usr/bin/env python3
"""Владелец (2026-09-17), арбитраж хвост 2: 73% многоплечевых tx были
отброшены детектором циклов, потому что их pool_id не резолвился в
currency0/currency1 (нужен был ОТДЕЛЬНЫЙ скан Initialize в ТОМ ЖЕ узком
окне -- если пул создан раньше окна, резолва не было). Ключевая
экономия: НЕ НУЖНО резолвить currency0/currency1 вообще, если проверять
цикл через РЕАЛЬНЫЕ settled Transfer-логи (тот же метод, что уже
доказан и использован в task_arc_arb_profit_recipients.py) -- реальная
прибыль арбитражника видна напрямую по чистому приросту USDC на каком-то
адресе внутри транзакции, без знания промежуточных токенов пути вообще.

Метод:
  1) ОДНА проба eth_getLogs (маленький диапазон) -- если лимит ещё
     активен, СТОП сразу, ничего дороже не пробуем.
  2) Если проба прошла -- скан Swap-событий за один свежий час (адаптивный
     chunk + терпеливый ретрай -- та же дисциплина, что везде в этой
     сессии), группировка по tx_hash, фильтр на ≥2 ног (многоплечевые).
  3) Для КАЖДОЙ многоплечевой tx -- ОДИН eth_getTransactionReceipt
     (не eth_getLogs, не подпадает под лимит, уже подтверждено 267/267
     раз без единой ошибки в прошлом раунде) -- реальные Transfer-логи
     (USDC ERC20 + нативная обёртка), зеркальные дедуплицированы
     (arc_units.merge_mirrored_transfers). НЕ нужно резолвить pool_id
     в currency0/currency1 -- цикл подтверждается напрямую по чистому
     приросту USDC на каком-то адресе (не PoolManager, не хук-комиссия).

ЧЕСТНАЯ ОГОВОРКА МЕТОДА: это НЕ строгая топологическая проверка цикла
(leg[i].token_out==leg[i+1].token_in), а прямое наблюдение результата --
имеет то же обоснование, что и task_arc_arb_profit_recipients.py
(реальный settled Transfer -- это ФАКТ, а не модель), но теоретически
может засчитать как "цикл" многоплечевую tx, которая на самом деле не
замкнутый цикл, а просто удачный многошаговый маршрут. Компромисс явно
назван, не скрыт."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

sys.path.insert(0, str(Path(__file__).parent))
from arc_units import USDC_ERC20, USDC_NATIVE_WRAP, to_human, merge_mirrored_transfers  # noqa: E402

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
PROFIT_RECEIVER_HOOK = "0x47e7936ae9891e61c5123db720593c05de7120cc"
BLOCK_TIME_S = 0.506
WINDOW_HOURS = 1
MAX_SWAP_SCAN_CALLS = 30
REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
MIN_CALL_INTERVAL_S = 0.1
_last_call_ts = [0.0]
_rpc_calls = [0]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
TRANSFER_TOPIC0 = keccak_topic0("Transfer(address,address,uint256)")


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    wait = MIN_CALL_INTERVAL_S - (time.time() - _last_call_ts[0])
    if wait > 0:
        time.sleep(wait)
    _last_call_ts[0] = time.time()
    _rpc_calls[0] += 1
    try:
        resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              headers={"Content-Type": "application/json"}, timeout=timeout)
        body = resp.json()
        body["_http_status"] = resp.status_code
        return body
    except Exception as exc:  # noqa: BLE001
        return {"error": {"message": f"{type(exc).__name__}: {exc}"}, "_http_status": None}


def is_rate_limited(body: dict) -> bool:
    err = body.get("error")
    return bool(err) and (err.get("code") == -32005 or "rate limit" in str(err.get("message", "")).lower()
                           or body.get("_http_status") == 429)


def scan_swap_tx_hashes(from_block: int, to_block: int, max_calls: int) -> tuple[dict, dict]:
    """Только tx_hash + log_index (минимум данных) -- группируем по tx для
    поиска многоплечевых, полные детали свопа не нужны (Transfer-логи из
    receipt дают всё, что нужно для проверки прибыли)."""
    by_tx: dict[str, int] = {}
    block = from_block
    chunk = 5000
    calls = 0
    stopped_by_rate_limit = False
    while block <= to_block and calls < max_calls:
        end = min(block + chunk - 1, to_block)
        body = None
        for _ in range(2):
            body = rpc("eth_getLogs", [{"fromBlock": hex(block), "toBlock": hex(end),
                                         "address": POOL_MANAGER, "topics": [SWAP_TOPIC0]}])
            calls += 1
            if not is_rate_limited(body):
                break
            time.sleep(20)
        if is_rate_limited(body):
            stopped_by_rate_limit = True
            break
        if "error" in body:
            msg = str(body["error"].get("message", "")).lower()
            if "too large" in msg or "max results" in msg or body["error"].get("code") in (-32012, -32602):
                chunk = max(500, chunk // 2)
                continue
            break
        for log in body.get("result", []):
            h = log["transactionHash"]
            by_tx[h] = by_tx.get(h, 0) + 1
        block = end + 1
    return by_tx, {"n_calls": calls, "stopped_by_rate_limit": stopped_by_rate_limit, "complete": block > to_block}


def decode_transfers_from_receipt(receipt: dict) -> list[dict]:
    out = []
    for log in receipt.get("logs", []):
        if log.get("topics") and log["topics"][0].lower() == TRANSFER_TOPIC0.lower():
            token = log["address"].lower()
            if token not in (USDC_ERC20.lower(), USDC_NATIVE_WRAP.lower()):
                continue
            if len(log["topics"]) < 3:
                continue
            frm = "0x" + log["topics"][1][-40:]
            to = "0x" + log["topics"][2][-40:]
            value = int(log["data"], 16) if log.get("data") and log["data"] != "0x" else 0
            out.append({"token": token, "from": frm.lower(), "to": to.lower(), "value": value})
    return out


def net_usdc_per_address(transfers: list[dict]) -> dict:
    net: dict = {}
    for t in transfers:
        human = to_human(t["token"], t["value"])
        if human is None:
            continue
        net[t["from"]] = net.get(t["from"], 0.0) - human
        net[t["to"]] = net.get(t["to"], 0.0) + human
    return net


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # === Проба: eth_getLogs (маленький диапазон), СТОП сразу если лимит ===
    latest_body = rpc("eth_blockNumber", [])
    latest = int(latest_body.get("result", "0x0"), 16) if not latest_body.get("error") else None
    result["latest_block"] = latest
    if latest is None:
        result["STOPPED"] = "не удалось получить latest_block"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        data_dir.joinpath("task_arc_arb_full_hour_recompute_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return

    probe_from = latest - 200
    probe = rpc("eth_getLogs", [{"fromBlock": hex(probe_from), "toBlock": hex(latest),
                                  "address": POOL_MANAGER, "topics": [SWAP_TOPIC0]}])
    result["probe_eth_getLogs"] = {"http_status": probe.get("_http_status"), "is_rate_limited": is_rate_limited(probe),
                                    "n_results": len(probe.get("result", [])) if "result" in probe else None}
    if is_rate_limited(probe):
        result["STOPPED"] = (
            "eth_getLogs ВСЁ ЕЩЁ в rate limit -- проба на 200 последних блоков откатилась с -32005. "
            "Хвост 2 (расширение покрытия) НЕ выполним этим методом сейчас -- pool_id для многоплечевых "
            "tx всё равно берётся из Swap-логов, которые нужно СНАЧАЛА найти диапазонным сканом "
            "(eth_getLogs), даже если дальнейшее резолвление через receipt свободно. Нужен либо "
            "рабочий альтернативный RPC (Chainstack, см. task_arc_alt_rpc_research_result.json), "
            "либо повторная попытка позже, когда пульсирующий лимит откроется."
        )
        result["total_rpc_calls"] = _rpc_calls[0]
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_arb_full_hour_recompute_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # === Скан свежего часа ===
    window_blocks = int(WINDOW_HOURS * 3600 / BLOCK_TIME_S)
    from_block = latest - window_blocks
    by_tx_swap_count, scan_meta = scan_swap_tx_hashes(from_block, latest, MAX_SWAP_SCAN_CALLS)
    result["swap_scan_meta"] = scan_meta
    result["window"] = {"from_block": from_block, "to_block": latest, "n_blocks": window_blocks}

    multi_leg_tx_hashes = [h for h, n in by_tx_swap_count.items() if n >= 2]
    result["n_swap_events_found"] = sum(by_tx_swap_count.values())
    result["n_distinct_tx_with_swaps"] = len(by_tx_swap_count)
    result["n_multi_leg_tx_candidates"] = len(multi_leg_tx_hashes)

    if scan_meta["stopped_by_rate_limit"] and not multi_leg_tx_hashes:
        result["STOPPED"] = "скан упёрся в rate limit до накопления кандидатов -- см. swap_scan_meta"
        result["total_rpc_calls"] = _rpc_calls[0]
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_arb_full_hour_recompute_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # === Для каждой многоплечевой tx -- receipt, Transfer-логи, реальный net USDC ===
    profit_by_recipient: dict = {}
    n_cycles_found = 0
    per_tx_rows = []
    for tx_hash in multi_leg_tx_hashes:
        recv = rpc("eth_getTransactionReceipt", [tx_hash])
        receipt = recv.get("result")
        if not receipt:
            continue
        transfers = merge_mirrored_transfers(decode_transfers_from_receipt(receipt))
        net = net_usdc_per_address(transfers)
        candidates = {a: v for a, v in net.items()
                      if a not in (POOL_MANAGER.lower(), PROFIT_RECEIVER_HOOK.lower()) and v > 0.001}
        if not candidates:
            continue
        top_recipient = max(candidates, key=candidates.get)
        profit = candidates[top_recipient]
        n_cycles_found += 1
        profit_by_recipient[top_recipient] = profit_by_recipient.get(top_recipient, 0.0) + profit
        per_tx_rows.append({"tx_hash": tx_hash, "n_swap_legs": by_tx_swap_count[tx_hash],
                             "recipient": top_recipient, "profit_usdc": profit})

    result["n_cycles_found_this_method"] = n_cycles_found
    profit_sorted = dict(sorted(profit_by_recipient.items(), key=lambda kv: -kv[1]))
    result["profit_by_recipient_usdc"] = profit_sorted
    result["n_distinct_recipients"] = len(profit_sorted)
    total_profit = sum(profit_sorted.values())
    result["total_profit_usdc_this_hour"] = total_profit
    result["largest_recipient_profit_usdc"] = next(iter(profit_sorted.values()), 0.0)

    result["method_caveat"] = (
        "Это НЕ строгая топологическая проверка цикла (не проверяем, что путь замыкается на "
        "стартовый токен через token_in/token_out) -- прямое наблюдение реального settled Transfer "
        "(тот же метод, что в task_arc_arb_profit_recipients.py). Может включать многошаговые "
        "прибыльные маршруты, не являющиеся строгим циклом в топологическом смысле, но реально "
        "принёсшие USDC-прибыль какому-то адресу за счёт цепочки свопов через PoolManager."
    )
    result["summary_line"] = (
        f"За последний час: ${total_profit:.2f} суммарно, крупнейший получатель "
        f"${result['largest_recipient_profit_usdc']:.2f}, {result['n_distinct_recipients']} игроков "
        f"(из {n_cycles_found} многоплечевых транзакций с реальной прибылью)."
    )

    result["total_rpc_calls"] = _rpc_calls[0]
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_arb_full_hour_recompute_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
