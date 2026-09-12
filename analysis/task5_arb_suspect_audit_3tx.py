#!/usr/bin/env python3
"""Задача 5 -- три конкретные подозрительные на арбитраж транзакции,
исполнитель `0x1b357e7acd2a32aebfa2de286c9e8e617d39a251`. Тот же метод,
что и в `task5_bot_profit_formula_audit_contract_balance.py`: баланс
КОНТРАКТА (не EOA-инициатора) по всем ERC-20 Transfer-логам транзакции
(любой протокол, не завязано на v3 Swap) -- пришло/ушло по каждому
токену, плюс реальный газ (gasUsed * effectiveGasPrice, статус success/
revert), плюс eth_getCode (длина байткода) и активность контракта за
последний час (Blockscout API, число tx / доля success/revert -- ЧЕСТНО,
если Blockscout недоступен (как в прошлом запуске -- 403 на все запросы),
явно фиксируется, не подменяется догадкой)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import PATH_A_KNOWN_ROUTER_ADDRESSES_FROM_HISTOGRAM, RPC_URL_MAINNET
from task5_bot_pool_state import _rpc_call_with_provider_fallback
from task5_bot_router_decode import KNOWN_SELF_TRADE_ADDRESSES

TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DUST_RAW_THRESHOLD = 1000
BLOCKSCOUT_API = "https://robinhoodchain.blockscout.com/api/v2"

TX_HASHES = [
    "0x2264176a23d8ced039d386829c709ce3b3ec335427582179fb7aaedcff4de245",
    "0xd487122244e6c89a0c7b91d48111720575294a6cfeceedfaa3cf105b1fb996d0",
    "0xa1733a2816fe30fbd3de9c8fedbcbe43f54c3dfdbe274bf838eba1f66a2c2236",
]
EXECUTOR_CONTRACT = "0x1b357e7acd2a32aebfa2de286c9e8e617d39a251"

WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH_DECIMALS, USDG_DECIMALS = 18, 6


def _rpc(method, params):
    return _rpc_call_with_provider_fallback(method, params, RPC_URL_MAINNET, timeout=25.0)


def _topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:]


def _human(token_addr: str, raw: int) -> str:
    a = token_addr.lower()
    if a == WETH:
        return f"{raw / 10 ** WETH_DECIMALS:.8f} WETH"
    if a == USDG:
        return f"{raw / 10 ** USDG_DECIMALS:.6f} USDG"
    return f"{raw} raw ({token_addr}, decimals неизвестны)"


def blockscout_get(path: str, params: dict | None = None) -> dict:
    try:
        resp = requests.get(f"{BLOCKSCOUT_API}{path}", params=params, timeout=15,
                             headers={"User-Agent": "robinhood-chain-alpha-audit/1.0"})
        if resp.status_code == 200:
            return {"ok": True, "data": resp.json()}
        return {"ok": False, "http_status": resp.status_code}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def audit_tx(tx_hash: str) -> dict:
    row: dict = {"tx_hash": tx_hash}
    try:
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
        tx_obj = _rpc("eth_getTransactionByHash", [tx_hash])
    except Exception as exc:
        row["error"] = str(exc)
        return row
    if not receipt or not tx_obj:
        row["error"] = "рецепт или tx не найдены"
        return row

    contract_addr = (receipt.get("to") or "").lower()
    row["contract_to"] = contract_addr
    row["block_number"] = int(receipt["blockNumber"], 16)
    row["status"] = "success" if receipt.get("status") == "0x1" else "REVERT"

    gas_used = int(receipt["gasUsed"], 16)
    # effectiveGasPrice -- реальное поле пост-EIP-1559 receipt; честный fallback на tx.gasPrice,
    # если нода его не вернула (некоторые legacy-ответы).
    gas_price_hex = receipt.get("effectiveGasPrice") or tx_obj.get("gasPrice") or "0x0"
    gas_price_wei = int(gas_price_hex, 16)
    gas_cost_wei = gas_used * gas_price_wei
    row["gas_used"] = gas_used
    row["gas_price_wei"] = gas_price_wei
    row["gas_cost_eth"] = gas_cost_wei / 1e18

    contract_in: dict[str, int] = {}
    contract_out: dict[str, int] = {}
    all_transfers = []
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if not topics or topics[0].lower() != TRANSFER_TOPIC0 or len(topics) < 3:
            continue
        token = (log.get("address") or "").lower()
        frm = _topic_to_addr(topics[1])
        to = _topic_to_addr(topics[2])
        value = int(log.get("data") or "0x0", 16)
        all_transfers.append({"token": token, "from": frm, "to": to, "value_raw": value})
        if frm == contract_addr:
            contract_out[token] = contract_out.get(token, 0) + value
        if to == contract_addr:
            contract_in[token] = contract_in.get(token, 0) + value
    row["n_transfer_logs"] = len(all_transfers)
    row["all_transfers"] = all_transfers

    net = {t: contract_in.get(t, 0) - contract_out.get(t, 0) for t in set(contract_in) | set(contract_out)}
    positive = {t: v for t, v in net.items() if v > DUST_RAW_THRESHOLD}
    negative = {t: v for t, v in net.items() if v < -DUST_RAW_THRESHOLD}
    row["contract_net_positive"] = {t: {"raw": v, "human": _human(t, v)} for t, v in positive.items()}
    row["contract_net_negative"] = {t: {"raw": -v, "human": _human(t, -v)} for t, v in negative.items()}

    if row["status"] == "REVERT":
        row["verdict"] = "REVERT -- транзакция откатилась, никакого движения токенов не произошло"
    elif len(positive) == 1 and len(negative) == 0:
        row["verdict"] = "ЗАМКНУТЫЙ ЦИКЛ (один токен net-положителен на контракте, остальные обнулились)"
        gain_token, gain_info = next(iter(positive.items()))
        if gain_token == WETH:
            gross_eth = gain_info["raw"] / 1e18
            row["gross_profit_eth"] = gross_eth
            row["net_profit_after_gas_eth"] = gross_eth - row["gas_cost_eth"]
        else:
            row["gross_profit_eth"] = None
            row["net_profit_after_gas_eth"] = None
            row["note_profit"] = f"прибыль в {gain_token}, не WETH -- $ значение не переведено (курс не запрошен)"
    elif positive and negative:
        row["verdict"] = "ОБМЕН (токен ушёл с контракта, другой пришёл -- не арбитраж)"
    elif not positive and not negative:
        row["verdict"] = "НЕИЗВЕСТНО (баланс контракта по Transfer-логам не изменился)"
    else:
        row["verdict"] = f"ЧЕСТНО НЕОДНОЗНАЧНО (positive={len(positive)}, negative={len(negative)})"

    try:
        code = _rpc("eth_getCode", [contract_addr, "latest"])
        row["contract_code_len_bytes"] = (len(code) - 2) // 2 if code and code != "0x" else 0
    except Exception as exc:
        row["contract_code_len_bytes"] = None
        row["eth_getcode_error"] = str(exc)
    row["is_known_router_from_histogram"] = contract_addr in PATH_A_KNOWN_ROUTER_ADDRESSES_FROM_HISTOGRAM
    row["is_known_self_trade_address"] = contract_addr in KNOWN_SELF_TRADE_ADDRESSES
    return row


def measure_contract_activity_last_hour(contract_addr: str) -> dict:
    """Реальная попытка через публичный Blockscout API (уже используется
    в проекте, без ключа) -- листинг транзакций контракта, сортировка по
    убыванию времени, суммируем, пока не пересечём час назад. ЧЕСТНО:
    если API вернул не-200 (как в прошлом запуске -- 403 на каждый вызов),
    явно фиксируем отказ, не подставляем оценку вместо факта."""
    now = int(time.time())
    one_hour_ago = now - 3600
    n_total = 0
    n_success = 0
    n_revert = 0
    cursor_params: dict = {}
    pages_fetched = 0
    stopped_reason = None
    for _ in range(30):  # честный потолок страниц, не бесконечный цикл
        resp = blockscout_get(f"/addresses/{contract_addr}/transactions", params=cursor_params or None)
        if not resp.get("ok"):
            stopped_reason = f"blockscout error: {resp}"
            break
        pages_fetched += 1
        items = resp["data"].get("items", [])
        if not items:
            stopped_reason = "пустая страница -- конец истории"
            break
        reached_window_end = False
        for item in items:
            ts_str = item.get("timestamp")
            try:
                item_ts = int(time.mktime(time.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S"))) if ts_str else None
            except Exception:
                item_ts = None
            if item_ts is not None and item_ts < one_hour_ago:
                reached_window_end = True
                break
            n_total += 1
            status = item.get("status")
            if status == "ok":
                n_success += 1
            else:
                n_revert += 1
        if reached_window_end:
            stopped_reason = "пересекли час назад"
            break
        next_params = (resp["data"].get("next_page_params") or {})
        if not next_params:
            stopped_reason = "нет next_page_params -- конец истории"
            break
        cursor_params = next_params
    return {
        "method": "blockscout /addresses/{addr}/transactions, постранично, пока не пересечём 1 час назад",
        "n_tx_last_hour": n_total, "n_success": n_success, "n_revert": n_revert,
        "success_share": (n_success / n_total) if n_total else None,
        "pages_fetched": pages_fetched, "stopped_reason": stopped_reason,
    }


if __name__ == "__main__":
    rows = []
    for h in TX_HASHES:
        print(f"[3tx_audit] {h}...")
        row = audit_tx(h)
        rows.append(row)
        print(f"    -> contract={row.get('contract_to')} status={row.get('status')} "
              f"verdict={row.get('verdict', row.get('error'))} gas_cost_eth={row.get('gas_cost_eth')} "
              f"net_profit_after_gas_eth={row.get('net_profit_after_gas_eth')}")

    print(f"\n[3tx_audit] активность контракта {EXECUTOR_CONTRACT} за последний час...")
    activity = measure_contract_activity_last_hour(EXECUTOR_CONTRACT)
    print(json.dumps(activity, indent=2, ensure_ascii=False))

    try:
        code = _rpc("eth_getCode", [EXECUTOR_CONTRACT, "latest"])
        contract_code_len = (len(code) - 2) // 2 if code and code != "0x" else 0
    except Exception as exc:
        contract_code_len = None
        print(f"[3tx_audit] eth_getCode ошибка: {exc}", file=sys.stderr)

    result = {
        "executor_contract": EXECUTOR_CONTRACT, "executor_contract_code_len_bytes": contract_code_len,
        "is_known_router_from_histogram": EXECUTOR_CONTRACT in PATH_A_KNOWN_ROUTER_ADDRESSES_FROM_HISTOGRAM,
        "is_known_self_trade_address": EXECUTOR_CONTRACT in KNOWN_SELF_TRADE_ADDRESSES,
        "activity_last_hour": activity,
        "rows": rows,
    }
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path("data/task5_arb_suspect_audit_3tx_result.json").write_text(text)
    print("\n[3tx_audit] записано в data/task5_arb_suspect_audit_3tx_result.json")
