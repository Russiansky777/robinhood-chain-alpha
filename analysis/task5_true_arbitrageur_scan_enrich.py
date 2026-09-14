#!/usr/bin/env python3
"""Владелец: "если найдётся подтверждённый прибыльный атомарный цикл,
сохрани txHash, полный маршрут, blockHash/number, transactionIndex,
доступные receipt/traces и сведения, необходимые для восстановления
состояния непосредственно перед транзакцией. Отметь, поддерживается ли
этот маршрут нашим исполнителем. Не считать состояние конца предыдущего
блока точным pre-state, если перед сделкой в её блоке были другие
изменяющие состояние транзакции."

ЭТО ЗАГОТОВКА для последующего benchmark на реальном рабочем эпизоде --
здесь НЕТ ни replay, ни benchmark, ни forka: только read-only сбор уже
перечисленных артефактов для транзакций, которые
task5_true_arbitrageur_scan.py УЖЕ пометил как qualifying (прошедшие
точный критерий владельца). Если qualifying_rows пуст -- скрипт честно
завершается, ничего не выдумывая.

Не запускается автоматически из самого скана -- отдельный,
последующий шаг, читает его результат."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import UNISWAP_V3_SWAP_SIG, UNISWAP_V4_SWAP_SIG, _rpc_call, topic0  # noqa: E402

RESULT_PATH = Path(__file__).parent.parent / "data" / "task5_true_arbitrageur_scan_result.json"
OUT_PATH = Path(__file__).parent.parent / "data" / "task5_true_arbitrageur_scan_enriched.json"

V4_SWAP_TOPIC0 = topic0(UNISWAP_V4_SWAP_SIG).lower()
V3_SWAP_TOPIC0 = topic0(UNISWAP_V3_SWAP_SIG).lower()
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"


def get_receipt(tx_hash: str) -> dict:
    body = _rpc_call("eth_getTransactionReceipt", [tx_hash])
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(f"eth_getTransactionReceipt error: {body['error']}")
    return body if isinstance(body, dict) else {}


def extract_route_legs(receipt: dict) -> list[dict]:
    """Полный маршрут -- последовательность Swap-событий (V4 PoolManager
    ИЛИ V3-style standalone пул) в порядке logIndex внутри рецепта.
    НЕ гадаем формат -- берём ровно то, что есть в реальных логах."""
    legs = []
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if not topics:
            continue
        t0 = topics[0].lower()
        addr = (log.get("address") or "").lower()
        if t0 == V4_SWAP_TOPIC0 and addr == POOL_MANAGER.lower():
            legs.append({
                "kind": "v4_pool_manager", "log_index": log.get("logIndex"),
                "pool_manager_address": log.get("address"),
                "pool_id": topics[1] if len(topics) > 1 else None,
            })
        elif t0 == V3_SWAP_TOPIC0:
            legs.append({
                "kind": "v3_standalone_pool", "log_index": log.get("logIndex"),
                "pool_address": log.get("address"),
            })
    legs.sort(key=lambda l: int(l["log_index"], 16) if isinstance(l["log_index"], str) else (l["log_index"] or 0))
    return legs


def route_supported_by_our_executor(legs: list[dict]) -> dict:
    """Наш build_execute_cycle_calldata/RouteLeg (task5_v4_executor_
    calldata.py) моделирует ИСКЛЮЧИТЕЛЬНО V4 PoolManager-циклы (проверено
    чтением кода -- ни одного упоминания V3 в этом файле). Standalone
    V3-пул в маршруте -- НЕ представим текущим исполнителем без
    отдельного адаптера, которого в проекте нет."""
    if not legs:
        return {"supported": None, "reason": "не удалось извлечь ни одной Swap-ноги из логов рецепта -- "
                                               "маршрут не восстановлен, поддержка не определена"}
    has_v3 = any(l["kind"] == "v3_standalone_pool" for l in legs)
    if has_v3:
        return {"supported": False,
                "reason": "маршрут включает как минимум один standalone V3-пул -- "
                          "task5_v4_executor_calldata.py (build_execute_cycle_calldata/RouteLeg) "
                          "моделирует только V4 PoolManager-циклы, адаптера для standalone V3 в проекте нет"}
    return {"supported": True,
            "reason": "все ноги маршрута -- V4 PoolManager Swap-события -- совместимо с "
                      "RouteLeg/build_execute_cycle_calldata, НО это НЕ доказывает, что конкретные "
                      "currency0/currency1/fee/tickSpacing/hooks каждой ноги уже зарегистрированы "
                      "или проходимы в текущем RouteRegistry -- требует отдельной проверки при реальном benchmark"}


def check_pre_state_accuracy(receipt: dict, route_legs: list[dict]) -> dict:
    """Владелец: "не считать состояние конца предыдущего блока точным
    pre-state, если перед сделкой в её блоке были другие изменяющие
    состояние транзакции" -- реально проверяем ВСЕ предыдущие tx этого
    же блока на предмет затрагивания ТЕХ ЖЕ пулов (по адресу лога +
    pool_id для V4)."""
    block_hash = receipt.get("blockHash")
    tx_index = int(receipt.get("transactionIndex", "0x0"), 16)
    result: dict = {"block_hash": block_hash, "block_number": receipt.get("blockNumber"),
                     "transaction_index": tx_index}
    if tx_index == 0:
        result["n_preceding_txs_in_block"] = 0
        result["end_of_previous_block_is_exact_pre_state"] = True
        result["pre_state_note"] = ("Первая транзакция своего блока (transactionIndex=0) -- состояние конца "
                                     "ПРЕДЫДУЩЕГО блока ЯВЛЯЕТСЯ точным pre-state для этой сделки.")
        return result

    block = _rpc_call("eth_getBlockByHash", [block_hash, True])
    if not isinstance(block, dict) or not block.get("transactions"):
        result["checked"] = False
        result["reason"] = "eth_getBlockByHash не вернул полный список транзакций -- предыдущие tx НЕ проверены, " \
                            "pre-state НЕ подтверждён (честно не гадаем)"
        result["end_of_previous_block_is_exact_pre_state"] = None
        return result

    all_txs = block["transactions"]
    preceding = [t for t in all_txs if int(t.get("transactionIndex", "0x0"), 16) < tx_index]
    result["n_preceding_txs_in_block"] = len(preceding)

    relevant_pool_ids = {l["pool_id"].lower() for l in route_legs if l["kind"] == "v4_pool_manager" and l.get("pool_id")}
    relevant_pool_addrs = {l["pool_address"].lower() for l in route_legs
                            if l["kind"] == "v3_standalone_pool" and l.get("pool_address")}

    touching = []
    errors = []
    for t in preceding:
        h = t.get("hash")
        try:
            r = get_receipt(h)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{h}: {type(exc).__name__}: {exc}")
            continue
        for log in r.get("logs", []):
            addr = (log.get("address") or "").lower()
            topics = log.get("topics") or []
            if addr == POOL_MANAGER.lower() and len(topics) > 1 and topics[1].lower() in relevant_pool_ids:
                touching.append(h)
                break
            if addr in relevant_pool_addrs:
                touching.append(h)
                break
    if errors:
        result["receipt_fetch_errors"] = errors
    result["preceding_txs_touching_same_pools"] = touching
    if touching:
        result["end_of_previous_block_is_exact_pre_state"] = False
        result["pre_state_note"] = (
            f"{len(touching)} из {len(preceding)} предыдущих транзакций ЭТОГО блока реально затронули те же "
            f"пулы -- состояние конца ПРЕДЫДУЩЕГО блока НЕ ЯВЛЯЕТСЯ точным pre-state для этой сделки; "
            f"нужен точечный replay/трейс до её индекса внутри этого же блока (НЕ выполнен здесь -- заготовка).")
    elif not errors:
        result["end_of_previous_block_is_exact_pre_state"] = True
        result["pre_state_note"] = (
            f"Проверены все {len(preceding)} предыдущих транзакций этого блока -- ни одна не затронула те же "
            f"пулы -- состояние конца ПРЕДЫДУЩЕГО блока можно использовать как точный pre-state для этой сделки.")
    else:
        result["end_of_previous_block_is_exact_pre_state"] = None
        result["pre_state_note"] = (
            f"{len(errors)} из {len(preceding)} предыдущих транзакций НЕ удалось проверить (ошибки RPC) -- "
            f"pre-state НЕ подтверждён полностью, честно не гадаем.")
    return result


def attempt_trace(tx_hash: str) -> dict:
    """Реальная попытка debug_traceTransaction -- честно фиксируем
    неудачу, если этот RPC-эндпоинт её не поддерживает (уже
    задокументированная в проекте ситуация для некоторых точек чейна)."""
    try:
        body = _rpc_call("debug_traceTransaction", [tx_hash, {"tracer": "callTracer"}])
        if isinstance(body, dict) and body.get("error"):
            return {"available": False, "error": str(body["error"]),
                     "note": "RPC вернул ошибку на debug_traceTransaction -- эндпоинт, вероятно, не поддерживает трейс."}
        return {"available": True, "trace": body}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": f"{type(exc).__name__}: {exc}",
                 "note": "debug_traceTransaction не удался -- реальная, не выдуманная попытка, эндпоинт скорее "
                         "всего не поддерживает этот метод на текущем RPC-провайдере."}


def enrich_one(row: dict) -> dict:
    tx_hash = row["tx_hash"]
    receipt = get_receipt(tx_hash)
    legs = extract_route_legs(receipt)
    return {
        "tx_hash": tx_hash,
        "block_hash": receipt.get("blockHash"),
        "block_number": receipt.get("blockNumber"),
        "transaction_index": receipt.get("transactionIndex"),
        "full_route_legs_in_log_order": legs,
        "route_supported_by_our_executor": route_supported_by_our_executor(legs),
        "raw_receipt": receipt,
        "trace_attempt": attempt_trace(tx_hash),
        "pre_state_accuracy_check": check_pre_state_accuracy(receipt, legs),
        "original_scan_row": row,
    }


def main() -> None:
    if not RESULT_PATH.exists():
        print(json.dumps({"error": f"{RESULT_PATH} не найден -- сначала запустить "
                                    f"task5_true_arbitrageur_scan.py (через _launch.py/_poll.py)"}))
        return
    scan_result = json.loads(RESULT_PATH.read_text())
    qualifying_rows = scan_result.get("qualifying_rows", [])
    if not qualifying_rows:
        out = {"n_qualifying_rows_from_scan": 0,
               "note": "Скан не нашёл ни одной транзакции, прошедшей точный критерий владельца -- "
                       "обогащать нечего, артефакты не создаются, ничего не выдумано."}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))
        return

    enriched = []
    for row in qualifying_rows:
        try:
            enriched.append(enrich_one(row))
        except Exception as exc:  # noqa: BLE001
            enriched.append({"tx_hash": row.get("tx_hash"), "enrichment_error": f"{type(exc).__name__}: {exc}"})

    out = {"n_qualifying_rows_from_scan": len(qualifying_rows), "enriched": enriched}
    text = json.dumps(out, indent=2, default=str, ensure_ascii=False)
    print(text)
    OUT_PATH.write_text(text)


if __name__ == "__main__":
    main()
