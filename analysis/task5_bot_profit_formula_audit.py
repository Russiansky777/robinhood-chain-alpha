#!/usr/bin/env python3
"""Задача 5 -- ПРОВЕРКА СУЩЕСТВОВАНИЯ ПРЕДМЕТА (владелец, 2026-09-12,
"СТОП всей работе по боту"). Владелец вручную разобрал первую строку
известного ответа (`0x7AF12717...`, "прибыль $87.07") в обозревателе:
это НЕ арбитраж, а обычный обмен пользователя (CLAWNCH -> NVDA -> USDG
-> WETH через роутер-агрегатор) -- вход в 300 895 CLAWNCH наша формула
НЕ УВИДЕЛА (первое плечо не дало v3 Swap-лог), поэтому выход 0.0354 WETH
был засчитан как ЧИСТАЯ прибыль, хотя реально это была ОПЛАТА обмена.

ЕДИНСТВЕННАЯ ЗАДАЧА ЭТОГО СКРИПТА (ничего не чинить, не оптимизировать,
не запускать бота): взять 20 транзакций с наибольшей "прибылью" из
дневного датасета (`task5_diag_single_tx_7fe99563c7dc0ca2.csv` -- РЕАЛЬНЫЙ
top-200 по всему дню 2026-09-10, 01:32-23:24 UTC, а не 29-минутный срез)
и для каждой -- разобрать ВСЕ логи `Transfer` (ERC-20, любой протокол --
v2/v3/v4/агрегатор, метод архитектурно-агностичен, не завязан на
конкретный Swap-топик) плюс попытаться увидеть внутренние ETH-переводы
через `debug_traceTransaction` (callTracer) -- ЧЕСТНО, если нода не
поддерживает debug_* методы (частый случай для публичных/free-tier RPC),
явно фиксируется, не подменяется тишиной.

Вердикт по инициатору (`tx.from`, тот же адрес, что "executor" в
остальных измерениях этой сессии): "замкнутый цикл" -- если С адреса
инициатора НЕ УШЁЛ ни один токен (весь входной капитал был занят/
маршрутизирован ВНУТРИ транзакции, никогда не касаясь кошелька), только
пришла прибыль; "обмен" -- если с адреса реально ушёл токен A и пришёл
токен B (обычный своп, не арбитраж)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import RPC_URL_MAINNET
from task5_bot_pool_state import _rpc_call_with_provider_fallback

TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DUST_RAW_THRESHOLD = 1000  # честная защита от округления/пыли на уровне raw-единиц, не $ (токены разных decimals)

# Владелец: "20 транзакций с наибольшей 'прибылью' из дневного датасета" --
# РЕАЛЬНЫЙ top-20 из task5_diag_single_tx_7fe99563c7dc0ca2.csv (весь день
# 2026-09-10, не 29-минутный срез, который использовался в известном ответе
# выше по разговору) -- источник: столбец profit_usd, отсортирован по убыванию.
TOP20_HIGHEST_PROFIT = [
    {"tx_hash": "0xf342eb747f97793c8a561cb628f84c13acb76215f083c148dcc3f77e221d73f8", "block_number": 59793678, "profit_usd": 885502.10, "n_legs": 22, "n_pools": 9},
    {"tx_hash": "0x426082893a59be64e5bdc8eb153e71c951dac454f59445148ce513c975bbabaa", "block_number": 59013625, "profit_usd": 268258.46, "n_legs": 4, "n_pools": 4},
    {"tx_hash": "0x46f142aaa642f469a9bc552ebecbcb8baf141eb9d2c02207d0dd137b374f6296", "block_number": 59013184, "profit_usd": 256463.68, "n_legs": 8, "n_pools": 7},
    {"tx_hash": "0x97f4806989f32b3d2a0014e314931aff90089a01da6d72d7230318ba7f6ed5e7", "block_number": 59013853, "profit_usd": 252407.65, "n_legs": 3, "n_pools": 3},
    {"tx_hash": "0xe65d7e2ba31a4434db89c667f9c3e4cdb2a3f439deda019d67220c58edbe4b40", "block_number": 59013433, "profit_usd": 186637.67, "n_legs": 5, "n_pools": 4},
    {"tx_hash": "0xcfa1226d5ebe46b43c22ef9075e71a4e685bd91bed2889293cd0664502227985", "block_number": 59405137, "profit_usd": 19209.39, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0xa5f9d2b18eec0b4412e8c3f7e4a7c2b12fd4e637ef00de322239874c36c74c6d", "block_number": 59600849, "profit_usd": 19150.97, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0xe3a892c894963c8fd93f290b9af75f78a3c7492c30ece7985d8ea79643e7c88d", "block_number": 59406853, "profit_usd": 18531.85, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0x182e446150892f7b65cde45b192aaf45488a48d01cb64e3bd0eadb1129e2da9d", "block_number": 59404259, "profit_usd": 17862.51, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0x09b349dd463391c7b1ca37429d1649ad4ff01a5541375f2ab41c3f70fa44d30d", "block_number": 59643222, "profit_usd": 17733.81, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0x7f966b3c71545e4726f2361107a1d2b16294a5f0830cbb002aaaf966d4c848be", "block_number": 59407858, "profit_usd": 13794.18, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0x6af4b7422b073126c1f1a0f8dc6edba4d65137ab0d0aef7fd7441c01bf7239e4", "block_number": 59475382, "profit_usd": 13651.06, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0xfecae362a91dd90ac81150deeb323eaa4f2195dfa297b0c8492e7ccfd386581c", "block_number": 59252236, "profit_usd": 13276.19, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0xb2a713b1b1a34d319a89fe236844074426c2fdede936d5d694fac4998f052c45", "block_number": 59600180, "profit_usd": 13042.26, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0x35c37cbaa8dd4757a4191842ed191a5764a0d63e5328db475db116dca95e4b09", "block_number": 59602045, "profit_usd": 12151.01, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0xf0641c750fc5a600047f2fc5db703afed4d9488cb0d556c49bb9223c6dfcd4bc", "block_number": 59404384, "profit_usd": 11852.60, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0x32dbe510452c638f135a6f6f4535eba7695867c45a2d171b7d13db3a1c746e3e", "block_number": 59319640, "profit_usd": 11448.98, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0x91f47618399904b059146eace6fb529a945d394ee8680e7f42ceeddee4eeded8", "block_number": 59351836, "profit_usd": 11277.13, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0x7cda784dc06f7ee4b6b9c139f28b6796e672b807acbec0bb54af68b08acb0aa7", "block_number": 59404194, "profit_usd": 11256.06, "n_legs": 2, "n_pools": 2},
    {"tx_hash": "0x1f1287b2c0c07cbf905c0d77d5c10424d3444e1441145218f9d8da4fe8af5242", "block_number": 59258916, "profit_usd": 10444.83, "n_legs": 2, "n_pools": 2},
]


def _rpc(method, params):
    return _rpc_call_with_provider_fallback(method, params, RPC_URL_MAINNET, timeout=25.0)


def _topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:]


def _walk_trace_for_value_transfers(node: dict, initiator: str, out: list[dict]) -> None:
    """Владелец: 'внутренние переводы ETH/WETH' -- обходит дерево вызовов
    callTracer (если нода поддерживает debug_traceTransaction), собирает
    любой шаг, где нативный ETH (`value`) реально двигался К инициатору
    или ОТ него (не считая саму верхнюю tx -- та уже учтена отдельно)."""
    if not isinstance(node, dict):
        return
    value_hex = node.get("value")
    if value_hex and value_hex not in ("0x0", "0x"):
        frm = (node.get("from") or "").lower()
        to = (node.get("to") or "").lower()
        if frm == initiator or to == initiator:
            out.append({"from": frm, "to": to, "value_wei": int(value_hex, 16), "type": node.get("type")})
    for child in node.get("calls") or []:
        _walk_trace_for_value_transfers(child, initiator, out)


def audit_one(entry: dict) -> dict:
    tx_hash = entry["tx_hash"]
    row: dict = {**entry}

    try:
        tx_obj = _rpc("eth_getTransactionByHash", [tx_hash])
    except Exception as exc:
        row["error"] = f"eth_getTransactionByHash: {exc}"
        return row
    if not tx_obj:
        row["error"] = "транзакция не найдена (eth_getTransactionByHash вернул None)"
        return row
    initiator = (tx_obj.get("from") or "").lower()
    tx_value_wei = int(tx_obj.get("value") or "0x0", 16)
    row["initiator"] = initiator
    row["tx_native_value_wei"] = tx_value_wei

    try:
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
    except Exception as exc:
        row["error"] = f"eth_getTransactionReceipt: {exc}"
        return row
    if not receipt:
        row["error"] = "рецепт не найден"
        return row

    initiator_out: dict[str, int] = {}
    initiator_in: dict[str, int] = {}
    n_transfer_logs = 0
    distinct_tokens_touched = set()
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if not topics or topics[0].lower() != TRANSFER_TOPIC0 or len(topics) < 3:
            continue
        n_transfer_logs += 1
        token = (log.get("address") or "").lower()
        frm = _topic_to_addr(topics[1])
        to = _topic_to_addr(topics[2])
        value = int(log.get("data") or "0x0", 16)
        distinct_tokens_touched.add(token)
        if frm == initiator:
            initiator_out[token] = initiator_out.get(token, 0) + value
        if to == initiator:
            initiator_in[token] = initiator_in.get(token, 0) + value

    # чистый (net) поток по каждому токену, реально задевшему инициатора
    net_out = {t: initiator_out.get(t, 0) - initiator_in.get(t, 0)
               for t in set(initiator_out) | set(initiator_in)
               if initiator_out.get(t, 0) - initiator_in.get(t, 0) > DUST_RAW_THRESHOLD}
    net_in = {t: initiator_in.get(t, 0) - initiator_out.get(t, 0)
              for t in set(initiator_out) | set(initiator_in)
              if initiator_in.get(t, 0) - initiator_out.get(t, 0) > DUST_RAW_THRESHOLD}

    row["n_transfer_logs_total"] = n_transfer_logs
    row["n_distinct_tokens_in_receipt"] = len(distinct_tokens_touched)
    row["tokens_left_initiator_raw"] = net_out
    row["tokens_arrived_to_initiator_raw"] = net_in

    if tx_value_wei > DUST_RAW_THRESHOLD:
        row["native_eth_sent_with_tx_wei"] = tx_value_wei

    # честная попытка internal-трейса -- многие публичные/free-tier RPC не
    # поддерживают debug_*, тогда просто фиксируем это, не гадаем.
    internal_transfers = []
    trace_error = None
    try:
        trace = _rpc("debug_traceTransaction", [tx_hash, {"tracer": "callTracer"}])
        if trace:
            _walk_trace_for_value_transfers(trace, initiator, internal_transfers)
    except Exception as exc:
        trace_error = str(exc)
    row["internal_native_eth_transfers_touching_initiator"] = internal_transfers
    row["debug_trace_error"] = trace_error

    # --- вердикт ---
    has_out = bool(net_out) or tx_value_wei > DUST_RAW_THRESHOLD
    has_in = bool(net_in) or any(t["to"] == initiator for t in internal_transfers)
    if not has_out and has_in:
        row["verdict"] = "ЗАМКНУТЫЙ ЦИКЛ (с адреса инициатора ничего не ушло, только пришла прибыль)"
    elif has_out and has_in:
        row["verdict"] = "ОБМЕН (с адреса ушёл токен, пришёл другой -- не арбитраж)"
    elif has_out and not has_in:
        row["verdict"] = "ЧЕСТНО СТРАННО: с адреса ушло, ничего не пришло -- не наш формат прибыли"
    else:
        row["verdict"] = "НЕИЗВЕСТНО (ни входа, ни выхода не обнаружено на уровне tx.from)"
    return row


if __name__ == "__main__":
    rows = []
    for entry in TOP20_HIGHEST_PROFIT:
        print(f"[profit_audit] {entry['tx_hash']} (заявленная прибыль ${entry['profit_usd']:,.2f})...")
        row = audit_one(entry)
        rows.append(row)
        print(f"    -> вердикт: {row.get('verdict', row.get('error'))}")

    n_closed_cycle = sum(1 for r in rows if r.get("verdict", "").startswith("ЗАМКНУТЫЙ"))
    n_exchange = sum(1 for r in rows if r.get("verdict", "").startswith("ОБМЕН"))
    n_other = len(rows) - n_closed_cycle - n_exchange
    usd_misclassified_as_profit = sum(r["profit_usd"] for r in rows if r.get("verdict", "").startswith("ОБМЕН"))

    result = {
        "n_total": len(rows),
        "n_closed_cycle": n_closed_cycle,
        "n_exchange_misclassified": n_exchange,
        "n_other_or_error": n_other,
        "usd_total_misclassified_as_profit_top20": round(usd_misclassified_as_profit, 2),
        "rows": rows,
    }
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path("data/task5_bot_profit_formula_audit_result.json").write_text(text)
    print(f"\n[profit_audit] ИТОГ: {n_closed_cycle}/{len(rows)} реальных замкнутых циклов, "
          f"{n_exchange}/{len(rows)} обменов ошибочно засчитаны как прибыль (${usd_misclassified_as_profit:,.2f}), "
          f"{n_other} прочее/ошибка")
