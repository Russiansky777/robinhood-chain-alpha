#!/usr/bin/env python3
"""Владелец (2026-09-16): предыдущий вывод "приз арбитража невелик"
отозван -- заработок арбитражников НЕ измерен ни разу. 267 подтверждённых
циклов (data/task_arc_closed_cycles_summary.json, УЖЕ в git, реальные
tx_hash/profit_usdc_net/senders/tx_from по каждому) дают "senders"
(4 адреса-исполнителя из событий Swap) и "tx_from" (кто платил газ) --
это РАЗНЫЕ множества (4 уникальных sender против 27 уникальных tx_from),
значит нельзя предполагать, кто реально получает прибыль -- нужно
смотреть реальные Transfer-логи транзакции.

Метод: eth_getTransactionReceipt -- ДРУГОЙ RPC-метод, чем eth_getLogs
(точечный лукап по одному tx_hash, не скан диапазона блоков) -- уже
использовался в этой сессии (task_arc_lp_hook_router_reconcile.py) без
проблем с лимитом. Проба -- ОДИН вызов ПЕРЕД полным проходом, как везде
в этой линии.

Для каждой из 267 транзакций: реальные Transfer-логи по обеим формам
USDC (ERC20 0x3600.../нативная обёртка 0xfff...ffe), зеркальные пары
схлопнуты (arc_units.merge_mirrored_transfers -- тот же модуль, что и
везде в этой сессии, не новая логика). Считаем чистое изменение USDC по
КАЖДОМУ адресу внутри транзакции -- эмпирически, не предполагая заранее,
что прибыль идёт "sender"-у или "tx_from"-у. Хук-комиссия площадки
(0x47e7936ae9891e61c5123db720593c05de7120cc) исключена из кандидатов на
"получателя прибыли" -- это уже установленный факт (реконсиляция
task_arc_lp_hook_router_reconcile.py), не предположение.

Суточный приток на найденного получателя -- через eth_call
balanceOf(addr) на 2 блоках (~сутки назад и latest), тот же класс
вызова, что extsload (НЕ eth_getLogs, не должен быть в rate limit).
Отдельно: сколько из дельты баланса объясняется уже найденными
арбитражными циклами за эти же сутки (из уже посчитанных
profit_usdc_net), и сколько -- всё остальное (честно, может быть
отрицательным, если адрес тоже тратит)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from arc_units import USDC_ERC20, USDC_NATIVE_WRAP, to_human, merge_mirrored_transfers  # noqa: E402

RPC = "https://rpc.mainnet.arc.io"
PROFIT_RECEIVER_HOOK = "0x47e7936ae9891e61c5123db720593c05de7120cc"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
BLOCK_TIME_S = 0.506
BLOCKS_PER_DAY = round(86400 / BLOCK_TIME_S)
TRANSFER_TOPIC0 = None  # вычисляется ниже

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
MIN_CALL_INTERVAL_S = 0.15
_last_call_ts = [0.0]
_rpc_calls = [0]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def keccak_topic0(sig: str) -> str:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


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


def decode_transfers_from_receipt(receipt: dict, transfer_topic0: str) -> list[dict]:
    out = []
    for log in receipt.get("logs", []):
        if log.get("topics") and log["topics"][0].lower() == transfer_topic0.lower():
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


def erc20_balance_of(token: str, addr: str, block_hex: str) -> int | None:
    selector = "70a08231"  # balanceOf(address)
    calldata = "0x" + selector + addr[2:].rjust(64, "0")
    body = rpc("eth_call", [{"to": token, "data": calldata}, block_hex])
    res = body.get("result")
    if not res or res == "0x" or "error" in body:
        return None
    return int(res, 16)


def main() -> None:
    global TRANSFER_TOPIC0
    TRANSFER_TOPIC0 = keccak_topic0("Transfer(address,address,uint256)")

    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    cycles = json.loads(data_dir.joinpath("task_arc_closed_cycles_summary.json").read_text())["all_verified_cycles_compact"]
    result["n_cycles_loaded"] = len(cycles)

    # === Проба: eth_getTransactionReceipt -- другой метод, чем eth_getLogs ===
    probe_tx = cycles[0]["tx_hash"]
    probe = rpc("eth_getTransactionReceipt", [probe_tx])
    result["probe"] = {"http_status": probe.get("_http_status"), "has_error": "error" in probe,
                        "has_result": bool(probe.get("result"))}
    if is_rate_limited(probe):
        result["STOPPED"] = "eth_getTransactionReceipt ТОЖЕ в rate limit -- гипотеза не подтвердилась, останавливаемся"
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_arb_profit_recipients_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return
    if not probe.get("result"):
        result["STOPPED"] = f"проба вернула пустой результат для {probe_tx} -- проверить схему/сеть перед полным проходом"
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_arb_profit_recipients_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # === Полный проход: все 267 циклов ===
    per_cycle = []
    for c in cycles:
        recv = rpc("eth_getTransactionReceipt", [c["tx_hash"]])
        if is_rate_limited(recv):
            result["stopped_mid_scan_by_rate_limit"] = c["tx_hash"]
            break
        receipt = recv.get("result")
        if not receipt:
            per_cycle.append({**c, "error": "receipt not found"})
            continue
        transfers = decode_transfers_from_receipt(receipt, TRANSFER_TOPIC0)
        transfers = merge_mirrored_transfers(transfers)
        net = net_usdc_per_address(transfers)
        # Исключаем PoolManager (внутренний расчётный узел, не получатель прибыли) и хук-комиссию
        candidates = {a: v for a, v in net.items()
                      if a not in (POOL_MANAGER.lower(), PROFIT_RECEIVER_HOOK.lower())}
        top_recipient = max(candidates, key=candidates.get) if candidates else None
        per_cycle.append({
            "tx_hash": c["tx_hash"], "block_number": c["block_number"], "profit_usdc_net_from_cycle_detector": c["profit_usdc_net"],
            "senders": c["senders"], "tx_from": c["tx_from"],
            "empirical_top_net_recipient": top_recipient,
            "empirical_top_net_recipient_amount_usdc": candidates.get(top_recipient) if top_recipient else None,
            "net_to_sender_addr": sum(net.get(s, 0.0) for s in c["senders"]),
            "net_to_tx_from": net.get(c["tx_from"], 0.0),
            "net_to_hook_fee": net.get(PROFIT_RECEIVER_HOOK.lower(), 0.0),
            "all_nonzero_net_addresses": {a: v for a, v in net.items() if abs(v) > 1e-9},
        })

    result["per_cycle_recipient_analysis"] = per_cycle
    result["total_rpc_calls_step1"] = _rpc_calls[0]

    # === Агрегация: кто реально получает прибыль -- sender, tx_from, или третий адрес? ===
    n_sender_matches = sum(1 for p in per_cycle if "error" not in p and p["empirical_top_net_recipient"] in [s.lower() for s in p["senders"]])
    n_tx_from_matches = sum(1 for p in per_cycle if "error" not in p and p["empirical_top_net_recipient"] == p["tx_from"].lower())
    n_other = sum(1 for p in per_cycle if "error" not in p and p["empirical_top_net_recipient"] not in
                  [s.lower() for s in p["senders"]] + [p["tx_from"].lower()])
    result["recipient_pattern_summary"] = {
        "n_cycles_analyzed": sum(1 for p in per_cycle if "error" not in p),
        "n_where_recipient_is_a_sender_address": n_sender_matches,
        "n_where_recipient_is_tx_from": n_tx_from_matches,
        "n_where_recipient_is_OTHER_address": n_other,
    }

    # === Агрегация прибыли по РЕАЛЬНОМУ (эмпирическому) получателю ===
    profit_by_real_recipient: dict = {}
    for p in per_cycle:
        if "error" in p or not p["empirical_top_net_recipient"]:
            continue
        r = p["empirical_top_net_recipient"]
        profit_by_real_recipient[r] = profit_by_real_recipient.get(r, 0.0) + (p["empirical_top_net_recipient_amount_usdc"] or 0.0)
    result["profit_by_real_recipient_usdc_267_cycles"] = dict(
        sorted(profit_by_real_recipient.items(), key=lambda kv: -kv[1])
    )

    # === Шаг 2: суточный приток на каждого найденного получателя (top по прибыли) ===
    latest_body = rpc("eth_blockNumber", [])
    latest = int(latest_body.get("result", "0x0"), 16) if not latest_body.get("error") else None
    result["latest_block"] = latest
    day_ago_block = max(0, latest - BLOCKS_PER_DAY) if latest else None

    daily_inflow = {}
    top_recipients = list(dict(sorted(profit_by_real_recipient.items(), key=lambda kv: -kv[1])[:6]).keys())
    for addr in top_recipients:
        bal_start = erc20_balance_of(USDC_ERC20, addr, hex(day_ago_block)) if day_ago_block else None
        bal_end = erc20_balance_of(USDC_ERC20, addr, hex(latest)) if latest else None
        native_bal_start = erc20_balance_of(USDC_NATIVE_WRAP, addr, hex(day_ago_block)) if day_ago_block else None
        native_bal_end = erc20_balance_of(USDC_NATIVE_WRAP, addr, hex(latest)) if latest else None
        erc20_delta = (to_human(USDC_ERC20, bal_end) - to_human(USDC_ERC20, bal_start)) if (bal_start is not None and bal_end is not None) else None
        native_delta = (to_human(USDC_NATIVE_WRAP, native_bal_end) - to_human(USDC_NATIVE_WRAP, native_bal_start)) if (native_bal_start is not None and native_bal_end is not None) else None
        total_delta = (erc20_delta or 0.0) + (native_delta or 0.0) if (erc20_delta is not None or native_delta is not None) else None
        cycle_attributed = sum(p["empirical_top_net_recipient_amount_usdc"] or 0.0 for p in per_cycle
                                if "error" not in p and p["empirical_top_net_recipient"] == addr
                                and day_ago_block is not None and p["block_number"] >= day_ago_block)
        daily_inflow[addr] = {
            "erc20_usdc_balance_delta_24h": erc20_delta,
            "native_usdc_balance_delta_24h": native_delta,
            "total_usdc_balance_delta_24h": total_delta,
            "cycle_attributed_profit_last_24h_usdc": cycle_attributed,
            "everything_else_usdc": (total_delta - cycle_attributed) if total_delta is not None else None,
        }
    result["daily_inflow_top_recipients"] = daily_inflow
    result["day_ago_block"] = day_ago_block

    result["total_rpc_calls"] = _rpc_calls[0]
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_arb_profit_recipients_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
