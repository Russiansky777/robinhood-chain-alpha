#!/usr/bin/env python3
"""Задача 5 -- финальная проверка: существует ли на Robinhood Chain хоть
ОДИН настоящий атомарный арбитражник, по правильному критерию (не по
сломанной формуле прибыли этой сессии):

  "У контракта-исполнителя (receipt.to) по итогу транзакции РОВНО ОДИН
  токен в плюсе, все остальные РОВНО в ноль, И на входе с адреса
  инициатора (tx.from) НИЧЕГО НЕ УШЛО (ни ERC-20, ни нативный ETH)."

Роутеры/агрегаторы (как 0x1b357e7a... из предыдущего разбора) отсекаются
автоматически -- у них ВСЁ обнуляется, ни одного плюса. Обмены
отсекаются -- там на входе токен A, на выходе B (либо с EOA, либо
контракт не netto-нулевой по >1 токену).

МЕТОД (Alchemy для точечных запросов, публичный RPC -- для ДИАПАЗОННОГО
eth_getLogs, см. честную оговорку ниже, реально задокументированную в
этом же проекте, analysis/alchemy_fallback.py):

1. Реальное окно 1 час недавних блоков (бинарный поиск по timestamp).
2. `fetch_v3_swap_logs`/`fetch_v4_swap_logs` (analysis/alchemy_fallback.py,
   уже протестированный, самобисектящийся eth_getLogs) -- собрать
   (tx_hash -> множество затронутых пулов, v3-адрес или v4-poolId) БЕЗ
   единого point-запроса, только из уже полученных логов.
3. Оставить tx с >=2 различных пулов -- "многопуловые" кандидаты.
4. ЧЕСТНАЯ ОПТИМИЗАЦИЯ ОБЪЁМА (реальная находка предыдущих разборов --
   многопуловых tx в этой сети могут быть ДЕСЯТКИ ТЫСЯЧ в час, буквальная
   проверка receipt каждой физически не уложится ни в один разумный
   бюджет времени): группируем кандидатов по `sender` СВОП-СОБЫТИЯ (уже
   есть в topics, без доп. RPC) -- это почти всегда тот же контракт, что
   инициирует все ноги внутри одной tx. Проверяем критерий на ОДНОЙ
   репрезентативной tx каждого уникального sender'а; только для
   sender'ов, прошедших эту пробу, досматриваем ВСЕ их tx окна полностью
   (расход RPC пропорционален числу РЕАЛЬНЫХ операторов, не числу сырых
   транзакций)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ВАЖНО: должно случиться ДО импорта alchemy_fallback/config -- Config --
# frozen dataclass, читает env ОДИН РАЗ при импорте. RPC_URL_PROVIDER уже
# реально настроен в /etc/bot/env (полный URL Alchemy с ключом) под ДРУГИМ
# именем, чем ожидает alchemy_fallback.py (ALCHEMY_ROBINHOOD_RPC_URL) --
# честно пробрасываем одно в другое, а не тихо теряем ключ.
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import json  # noqa: E402
import requests  # noqa: E402

from alchemy_fallback import (  # noqa: E402
    _alchemy_direct_endpoint, _BASE_HEADERS, _post_with_fallback, _rpc_call,
    fetch_v3_swap_logs, fetch_v4_swap_logs, get_block, get_block_number, get_transaction_fast,
)

TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH_USDG_POOL = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
WETH_DECIMALS, USDG_DECIMALS = 18, 6
DUST_RAW_THRESHOLD = 1000


# ЧЕСТНАЯ КОРРЕКТИРОВКА после первого реального прогона (падение на баге
# 'data':'0x', но реальные тайминги ДО падения сохранены): 15000 блоков
# (42% часового окна) заняли 559с сбора логов -- полный час на этой
# плотности потребует ~1300-1400с. Бюджеты подняты под РЕАЛЬНО измеренную
# плотность (v3=62239 + v4=102185 логов на 15000 блоков), не угаданы.
TIME_BUDGET_LOG_SCAN_S = 1500.0  # честный бюджет на этап 2 (сбор Swap-логов) -- покрывает полный час на измеренной плотности
TIME_BUDGET_TOTAL_S = 2100.0  # общий честный бюджет всего скрипта -- оставляет ~10 минут на проверку sender-групп


def _get_transaction_receipt_fast(tx_hash: str) -> dict:
    """Симметрично get_transaction_fast() -- целенаправленно через
    Alchemy для точечного (не диапазонного) вызова, фолбэк на публичный
    RPC при любой ошибке."""
    url = _alchemy_direct_endpoint()
    if url:
        try:
            resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt",
                                             "params": [tx_hash]}, headers=_BASE_HEADERS, timeout=20)
            if resp.status_code == 200:
                body = resp.json()
                if "result" in body and body["result"] is not None:
                    return body["result"]
        except Exception:  # noqa: BLE001
            pass
    body = _post_with_fallback({"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_hash]})
    if "error" in body:
        raise RuntimeError(f"eth_getTransactionReceipt error: {body['error']}")
    return body["result"]


def _topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:]


def find_block_n_seconds_ago(latest_block: int, seconds_ago: float) -> int:
    target_ts = time.time() - seconds_ago
    lo, hi = 0, latest_block
    latest_blk = get_block(latest_block)
    if int(latest_blk["timestamp"], 16) <= target_ts:
        return latest_block
    while lo < hi:
        mid = (lo + hi + 1) // 2
        blk = get_block(mid)
        if blk is None:
            hi = mid - 1
            continue
        ts = int(blk["timestamp"], 16)
        if ts <= target_ts:
            lo = mid
        else:
            hi = mid - 1
    return lo


def current_weth_usdg_price() -> float | None:
    """Реальная ТЕКУЩАЯ цена -- slot0() пула P5 (0x52e65b17...) прямо
    сейчас, не переиспользуем старую константу из более раннего разговора."""
    selector = "0x3850c7bd"  # slot0() -- тот же селектор, что везде в проекте (keccak-проверен ранее)
    try:
        result = _rpc_call("eth_call", [{"to": WETH_USDG_POOL, "data": selector}, "latest"])
        sqrt_price_x96 = int(result[2:66], 16)
        raw_ratio = (sqrt_price_x96 / (2 ** 96)) ** 2
        return raw_ratio * (10 ** (WETH_DECIMALS - USDG_DECIMALS))
    except Exception as exc:
        print(f"[true_arb_scan] не удалось получить текущую цену WETH/USDG: {exc}", file=sys.stderr)
        return None


def human_value(token: str, raw: int) -> str:
    t = token.lower()
    if t == WETH:
        return f"{raw / 10 ** WETH_DECIMALS:.8f} WETH"
    if t == USDG:
        return f"{raw / 10 ** USDG_DECIMALS:.6f} USDG"
    return f"{raw} raw ({token}, decimals неизвестны)"


def check_tx_against_criterion(tx_hash: str, eth_price_usdg: float | None) -> dict:
    """Полная проверка ОДНОЙ транзакции по точному критерию владельца.
    Возвращает {"qualifies": bool, ...детали...}."""
    try:
        receipt = _get_transaction_receipt_fast(tx_hash)
        tx_obj = get_transaction_fast(tx_hash)
    except Exception as exc:
        return {"tx_hash": tx_hash, "qualifies": False, "error": str(exc)}
    if not receipt or not tx_obj:
        return {"tx_hash": tx_hash, "qualifies": False, "error": "receipt/tx не найдены"}

    contract = (receipt.get("to") or "").lower()
    initiator = (tx_obj.get("from") or "").lower()
    tx_value_wei = int(tx_obj.get("value") or "0x0", 16)

    contract_net: dict[str, int] = {}
    initiator_out: dict[str, int] = {}
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if not topics or topics[0].lower() != TRANSFER_TOPIC0 or len(topics) < 3:
            continue
        token = (log.get("address") or "").lower()
        frm = _topic_to_addr(topics[1])
        to = _topic_to_addr(topics[2])
        data_hex = log.get("data") or "0x0"
        value = int(data_hex, 16) if data_hex not in ("0x", "") else 0
        if frm == contract:
            contract_net[token] = contract_net.get(token, 0) - value
        if to == contract:
            contract_net[token] = contract_net.get(token, 0) + value
        if frm == initiator:
            initiator_out[token] = initiator_out.get(token, 0) + value

    positive = {t: v for t, v in contract_net.items() if v > DUST_RAW_THRESHOLD}
    negative = {t: v for t, v in contract_net.items() if v < -DUST_RAW_THRESHOLD}
    initiator_spent = {t: v for t, v in initiator_out.items() if v > DUST_RAW_THRESHOLD}

    qualifies = (len(positive) == 1 and len(negative) == 0
                 and not initiator_spent and tx_value_wei <= DUST_RAW_THRESHOLD
                 and receipt.get("status") == "0x1")

    row = {
        "tx_hash": tx_hash, "contract": contract, "initiator": initiator,
        "status": "success" if receipt.get("status") == "0x1" else "revert",
        "qualifies": qualifies,
        "contract_net_positive": {t: human_value(t, v) for t, v in positive.items()},
        "contract_net_negative": {t: human_value(t, v) for t, v in negative.items()},
        "initiator_spent": {t: human_value(t, v) for t, v in initiator_spent.items()},
        "tx_native_value_wei": tx_value_wei,
    }
    if qualifies:
        gain_token, gain_raw = next(iter(positive.items()))
        gas_used = int(receipt["gasUsed"], 16)
        gas_price = int(receipt.get("effectiveGasPrice") or tx_obj.get("gasPrice") or "0x0", 16)
        gas_cost_eth = gas_used * gas_price / 1e18
        row["gas_cost_eth"] = gas_cost_eth
        if gain_token == WETH:
            gain_eth = gain_raw / 1e18
            row["gain_usd"] = gain_eth * eth_price_usdg if eth_price_usdg else None
            row["gas_cost_usd"] = gas_cost_eth * eth_price_usdg if eth_price_usdg else None
            row["net_after_gas_usd"] = (row["gain_usd"] - row["gas_cost_usd"]
                                         if row["gain_usd"] is not None else None)
        elif gain_token == USDG:
            gain_usd = gain_raw / 1e6
            row["gain_usd"] = gain_usd
            row["gas_cost_usd"] = gas_cost_eth * eth_price_usdg if eth_price_usdg else None
            row["net_after_gas_usd"] = (gain_usd - row["gas_cost_usd"]
                                         if row["gas_cost_usd"] is not None else None)
        else:
            row["gain_usd"] = None
            row["gain_raw_note"] = f"прибыль в {gain_token} -- decimals/курс неизвестны, $ не считаем"
    return row


def main() -> None:
    start_wall = time.time()
    latest = get_block_number()
    from_block = find_block_n_seconds_ago(latest, 3600.0)
    print(f"[true_arb_scan] окно: блоки {from_block}..{latest} ({latest - from_block} блоков), "
          f"latest={latest}")

    eth_price = current_weth_usdg_price()
    print(f"[true_arb_scan] реальная текущая цена WETH/USDG: {eth_price}")

    tx_pools: dict[str, set[str]] = {}
    tx_senders: dict[str, set[str]] = {}
    CHUNK = 2000  # честно уменьшен с 5000 -- реже промахиваемся мимо TIME_BUDGET_LOG_SCAN_S на одном большом чанке
    block = from_block
    last_covered = from_block - 1
    scan_timed_out = False
    n_v3_logs = 0
    n_v4_logs = 0
    while block <= latest:
        if time.time() - start_wall > TIME_BUDGET_LOG_SCAN_S:
            scan_timed_out = True
            break
        end = min(block + CHUNK - 1, latest)
        try:
            v3_logs = fetch_v3_swap_logs(block, end)
        except Exception as exc:
            print(f"[true_arb_scan] v3 log fetch error [{block};{end}]: {exc}", file=sys.stderr)
            v3_logs = []
        for log in v3_logs:
            txh = log["transactionHash"]
            tx_pools.setdefault(txh, set()).add("v3:" + log["address"].lower())
            tx_senders.setdefault(txh, set()).add(_topic_to_addr(log["topics"][1]))
        n_v3_logs += len(v3_logs)
        try:
            v4_logs = fetch_v4_swap_logs(block, end)
        except Exception as exc:
            print(f"[true_arb_scan] v4 log fetch error [{block};{end}]: {exc}", file=sys.stderr)
            v4_logs = []
        for log in v4_logs:
            txh = log["transactionHash"]
            tx_pools.setdefault(txh, set()).add("v4:" + log["topics"][1].lower())
            tx_senders.setdefault(txh, set()).add(_topic_to_addr(log["topics"][2]))
        n_v4_logs += len(v4_logs)
        last_covered = end
        block = end + 1
        print(f"[true_arb_scan] покрыто до блока {last_covered}, v3_logs={n_v3_logs} v4_logs={n_v4_logs} "
              f"tx_учтено={len(tx_pools)} прошло_{time.time()-start_wall:.0f}с", file=sys.stderr)

    multi_pool_tx = {txh: pools for txh, pools in tx_pools.items() if len(pools) >= 2}
    print(f"[true_arb_scan] РЕАЛЬНОЕ покрытие: блоки {from_block}..{last_covered} "
          f"({'ПОЛНОЕ окно' if not scan_timed_out and last_covered >= latest else 'ЧАСТИЧНОЕ -- бюджет времени исчерпан'}), "
          f"всего tx с Swap-событием: {len(tx_pools)}, многопуловых (>=2 пула): {len(multi_pool_tx)}")

    # группировка по sender -- реальная, из уже полученных логов, без доп. RPC
    sender_to_txs: dict[str, list[str]] = {}
    for txh, pools in multi_pool_tx.items():
        senders = tx_senders.get(txh, set())
        key = next(iter(senders)) if len(senders) == 1 else f"MIXED:{'|'.join(sorted(senders))}"
        sender_to_txs.setdefault(key, []).append(txh)
    print(f"[true_arb_scan] уникальных sender-групп среди многопуловых tx: {len(sender_to_txs)}")

    qualifying_senders: dict[str, dict] = {}
    n_checked_sample = 0
    for sender, txs in sender_to_txs.items():
        if time.time() - start_wall > TIME_BUDGET_TOTAL_S:
            print("[true_arb_scan] ОБЩИЙ бюджет времени исчерпан -- останавливаю проверку sender-групп", file=sys.stderr)
            break
        sample_tx = txs[0]
        n_checked_sample += 1
        sample_result = check_tx_against_criterion(sample_tx, eth_price)
        if sample_result.get("qualifies"):
            qualifying_senders[sender] = {"sample": sample_result, "all_txs_in_window": txs}

    print(f"[true_arb_scan] проверено {n_checked_sample} sender-групп по образцу, "
          f"из них прошли проверку: {len(qualifying_senders)}")

    # для прошедших sender'ов -- досмотреть ВСЕ их tx в окне
    final_rows = []
    for sender, info in qualifying_senders.items():
        for txh in info["all_txs_in_window"]:
            if txh == info["sample"]["tx_hash"]:
                row = info["sample"]
            else:
                row = check_tx_against_criterion(txh, eth_price)
            if row.get("qualifies"):
                final_rows.append(row)

    by_contract: dict[str, dict] = {}
    for row in final_rows:
        c = row["contract"]
        agg = by_contract.setdefault(c, {"n_qualifying_tx": 0, "total_net_after_gas_usd": 0.0,
                                          "total_gain_usd": 0.0, "any_usd_unknown": False})
        agg["n_qualifying_tx"] += 1
        if row.get("net_after_gas_usd") is not None:
            agg["total_net_after_gas_usd"] += row["net_after_gas_usd"]
            agg["total_gain_usd"] += row["gain_usd"]
        else:
            agg["any_usd_unknown"] = True

    result = {
        "window_from_block": from_block, "window_to_block_requested": latest,
        "window_to_block_actually_covered": last_covered,
        "scan_timed_out": scan_timed_out,
        "n_tx_with_any_swap": len(tx_pools), "n_multi_pool_tx": len(multi_pool_tx),
        "n_unique_sender_groups": len(sender_to_txs), "n_sender_groups_checked": n_checked_sample,
        "n_sender_groups_qualifying": len(qualifying_senders),
        "current_weth_usdg_price": eth_price,
        "n_qualifying_transactions_total": len(final_rows),
        "qualifying_rows": final_rows,
        "by_contract": by_contract,
        "elapsed_seconds": time.time() - start_wall,
    }
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path("data/task5_true_arbitrageur_scan_result.json").write_text(text)
    print(f"\n[true_arb_scan] ИТОГ: {len(final_rows)} транзакций прошли точный критерий "
          f"настоящего атомарного арбитража, {len(by_contract)} уникальных контрактов")


if __name__ == "__main__":
    main()
