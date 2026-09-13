#!/usr/bin/env python3
"""Задача 5, живой пилот, шестой раунд -- пункты 1 (контрольная сделка
конкурента), 3 (разбор 4 откатов estimateGas + 7 блоков "позитив до
газа/негатив после газа") и 4 (где именно теряется возможность).

ВСЁ, что можно получить БЕЗ реконструкции -- получаем реальными
вызовами (реальный tx/receipt конкурента, реальный registry.routes[...],
реальный hp.recompute_route/estimate_gas НЕ переписаны -- импортируются
как есть). Всё, для чего исходных данных не хватает (точный размер и
calldata 4 отклонённых estimateGas-попыток -- лог их не сохранил),
явно помечено "RECONSTRUCTION" в самом результате, с указанием, какое
именно предположение сделано.

Контрольная сделка конкурента: 0x9c4712ab46e082aa662dd28514dcc0fd7e0199f3621bee95a739bfe5995443d3,
блок 61630766, маршрут route_e0ed68abz0_c009a2dbz1 -- ТОТ ЖЕ route_id,
который наш бот ПОЗЖЕ (ts_wall=1789270778, тот же пилот) пытался
отправить и получил estimateGas revert 0x356680b7. Найдена реальным
сканом task5_v4_competitor_trade_scan.py (Swap-логи PoolManager,
sender=арбитражник, окно всего пилота) -- НЕ выдумана."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import os  # noqa: E402
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call, get_block  # noqa: E402
import task5_v4_hotpath as hp  # noqa: E402
from task5_v4_route_registry import load_registry_state  # noqa: E402
from task5_v4_executor_calldata import build_execute_cycle_calldata  # noqa: E402
from task5_v4_hook_route_audit import fetch_initialize_event  # noqa: E402

REGISTRY_STATE_FILE = "/home/bot/data/task5_v4_route_registry_state.json"
CONTRACT_ADDRESS = "0xAB24907bceEF4EDC366a1E4DfA15ed8aA5fdfe71"
OWNER = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

CONTROL_TX = "0x9c4712ab46e082aa662dd28514dcc0fd7e0199f3621bee95a739bfe5995443d3"
CONTROL_BLOCK = 61630766
CONTROL_ROUTE_ID = "route_e0ed68abz0_c009a2dbz1"
CONTROL_POOL_IDS = [
    "0xe0ed68ab7762d80eb45cc43d07f10be342b72554c06fa59e6335706a067081eb",
    "0xc009a2dbb8eaecde7179a3aeb10869035c8efd80c11962c37fd144a321921475",
]

SEVEN_BLOCKS = [
    ("route_7499938cz0_30daa45fz1_7caf58c3z0", 61755138),
    ("route_32306acaz0_f1897bd1z0_659e64f0z1", 61784512),
    ("route_e0a66e49z0_7e9d3e8cz1", 61832214),
    ("route_06b97324z0_f4527156z1_aa94ace2z0", 61838017),
    ("route_7499938cz0_f4527156z1_aa94ace2z0", 61839831),
    ("route_06b97324z0_30daa45fz1_7caf58c3z0", 61899018),
    ("route_7499938cz0_30daa45fz1_7caf58c3z0", 61901748),
]

FOUR_REVERTS = [
    ("route_e0ed68abz0_c009a2dbz1", 1789270778.614601),
    ("route_44e106bbz0_0c61eea1z0_24107d15z1", 1789272547.8693779),
    ("route_778a632fz1_f6064451z0_06b97324z1", 1789284156.011543),
    ("route_6eb75d4ez1_f6064451z0_06b97324z1", 1789284295.3087983),
]

BINSEARCH_LOW = 61600000
BINSEARCH_HIGH = 61970000


def find_block_by_timestamp(target_ts: float) -> dict:
    """Реальный бинарный поиск по eth_getBlockByNumber(...)['timestamp']
    -- НЕ экстраполяция по среднему блок-тайму (владелец явно запретил
    "реконструировать историческую задержку доставки состояния из
    сегодняшнего RPC-тайминга"; это тот же принцип -- ищем РЕАЛЬНЫЙ
    блок, не считаем его по среднему)."""
    lo, hi = BINSEARCH_LOW, BINSEARCH_HIGH
    lo_block = get_block(lo)
    hi_block = get_block(hi)
    if lo_block is None or hi_block is None:
        return {"ok": False, "error": "не удалось получить границы бинарного поиска"}
    if not (int(lo_block["timestamp"], 16) <= target_ts <= int(hi_block["timestamp"], 16)):
        return {"ok": False, "error": "target_ts вне границ поиска", "lo_ts": int(lo_block["timestamp"], 16),
                 "hi_ts": int(hi_block["timestamp"], 16), "target_ts": target_ts}
    n_calls = 0
    while hi - lo > 1:
        mid = (lo + hi) // 2
        blk = get_block(mid)
        n_calls += 1
        if blk is None:
            return {"ok": False, "error": f"eth_getBlockByNumber({mid}) вернул None"}
        ts = int(blk["timestamp"], 16)
        if ts <= target_ts:
            lo = mid
        else:
            hi = mid
    return {"ok": True, "block": lo, "block_ts": int(get_block(lo)["timestamp"], 16),
            "target_ts": target_ts, "n_calls": n_calls}


def decode_receipt_transfers(receipt: dict, watch_address: str) -> dict:
    """ERC20 Transfer-логи, где watch_address -- from ИЛИ to (реальные
    движения актива инициатора/получателя, не Swap-дельты пулов)."""
    TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    watch = watch_address.lower()
    out_by_token: dict[str, int] = {}
    in_by_token: dict[str, int] = {}
    for log in receipt.get("logs", []):
        if not log["topics"] or log["topics"][0].lower() != TRANSFER_TOPIC0:
            continue
        if len(log["topics"]) < 3:
            continue
        frm = "0x" + log["topics"][1][-40:]
        to = "0x" + log["topics"][2][-40:]
        amount = int(log["data"], 16) if log["data"] not in ("0x", "") else 0
        token = log["address"].lower()
        if frm.lower() == watch:
            out_by_token[token] = out_by_token.get(token, 0) + amount
        if to.lower() == watch:
            in_by_token[token] = in_by_token.get(token, 0) + amount
    return {"out_by_token_raw": out_by_token, "in_by_token_raw": in_by_token}


def try_debug_trace(tx_hash: str) -> dict:
    try:
        trace = _rpc_call("debug_traceTransaction", [tx_hash, {"tracer": "callTracer"}])
        return {"ok": True, "trace": trace}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def try_debug_trace_call(call_obj: dict, block_tag) -> dict:
    try:
        trace = _rpc_call("debug_traceCall", [call_obj, hex(block_tag) if isinstance(block_tag, int) else block_tag,
                                               {"tracer": "callTracer"}])
        return {"ok": True, "trace": trace}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def part1_control_trade() -> dict:
    result: dict = {"tx_hash": CONTROL_TX, "block": CONTROL_BLOCK, "route_id": CONTROL_ROUTE_ID}
    try:
        tx = _rpc_call("eth_getTransactionByHash", [CONTROL_TX])
        receipt = _rpc_call("eth_getTransactionReceipt", [CONTROL_TX])
        if tx is None or receipt is None:
            result["error"] = "tx/receipt не найдены"
            return result
        result["tx_from"] = tx["from"]
        result["tx_to"] = tx.get("to")
        result["tx_index"] = int(tx["transactionIndex"], 16)
        result["gas_used"] = int(receipt["gasUsed"], 16)
        result["effective_gas_price"] = int(receipt.get("effectiveGasPrice", "0x0"), 16)
        result["status"] = int(receipt["status"], 16)
        result["n_logs_in_receipt"] = len(receipt["logs"])

        # реальные движения актива инициатора (tx.from) -- ERC20 Transfer, НЕ Swap-дельта пула
        result["initiator_transfers"] = decode_receipt_transfers(receipt, tx["from"])

        # позиция в блоке -- сколько транзакций ему предшествовало (нужно для "состояние
        # непосредственно перед", если tx_index > 0 -- честно фиксируем, replay -- отдельный шаг)
        block_full = _rpc_call("eth_getBlockByNumber", [hex(CONTROL_BLOCK), True])
        result["n_txs_in_block"] = len(block_full["transactions"]) if block_full else None
        result["preceding_tx_hashes"] = (
            [t["hash"] for t in block_full["transactions"][:result["tx_index"]]] if block_full else None
        )
        result["state_immediately_before_note"] = (
            "tx_index == 0 -- состояние конца блока N-1 ЯВЛЯЕТСЯ состоянием непосредственно перед "
            "(предшествующих транзакций в этом же блоке нет)"
            if result["tx_index"] == 0 else
            f"tx_index == {result['tx_index']} -- {result['tx_index']} транзакций ЭТОГО ЖЕ блока "
            f"предшествуют контрольной; конец блока N-1 НЕ равен состоянию непосредственно перед "
            f"(honestly помечено, replay preceding tx НЕ выполнен в этом скрипте)"
        )

        # блоки N-2..N+1 -- реальные таймстемпы (стартовое окно, не автоматическое доказательство)
        timestamps = {}
        for b in (CONTROL_BLOCK - 2, CONTROL_BLOCK - 1, CONTROL_BLOCK, CONTROL_BLOCK + 1):
            blk = get_block(b)
            timestamps[b] = int(blk["timestamp"], 16) if blk else None
        result["block_timestamps_n2_to_n1"] = timestamps

        # реальные PoolKey обоих плеч через Initialize-событие (не предполагаются)
        pool_keys = {}
        for pid in CONTROL_POOL_IDS:
            info = fetch_initialize_event(pid, CONTROL_BLOCK)
            pool_keys[pid] = info
        result["pool_keys"] = pool_keys

        # честная попытка callTracer -- если нода не поддерживает debug_*, фиксируем, не гадаем
        result["debug_trace"] = try_debug_trace(CONTROL_TX)

    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


def part2_registry_lookup() -> dict:
    result: dict = {"registry_state_file": REGISTRY_STATE_FILE}
    loaded = load_registry_state(REGISTRY_STATE_FILE)
    if loaded is None:
        result["ok"] = False
        result["error"] = "реестр не загрузился (файл отсутствует/повреждён) -- честно фиксируем"
        return result
    registry, cursor_block = loaded
    result["ok"] = True
    result["cursor_block"] = cursor_block
    result["n_routes_in_registry"] = len(registry.routes)
    all_route_ids = {r for r, _ in SEVEN_BLOCKS} | {r for r, _ in FOUR_REVERTS}
    found = {}
    for rid in all_route_ids:
        route = registry.routes.get(rid)
        if route is None:
            found[rid] = None
            continue
        found[rid] = {
            "exit_token": route.exit_token, "label": route.label, "source": route.source,
            "legs": [{"currency0": leg.currency0, "currency1": leg.currency1, "fee": leg.fee,
                       "tick_spacing": leg.tick_spacing, "hooks": leg.hooks, "zero_for_one": leg.zero_for_one,
                       "pool_id": leg.pool_id_hex} for leg in route.legs],
        }
    result["routes_found"] = found
    result["_registry_object"] = registry  # передаётся между функциями этого процесса, не сериализуется
    return result


def part3_seven_blocks(registry) -> list:
    results = []
    for route_id, block in SEVEN_BLOCKS:
        entry: dict = {"route_id": route_id, "block": block}
        try:
            route = registry.routes.get(route_id) if registry is not None else None
            if route is None:
                entry["error"] = "route_id не найден в ТЕКУЩЕМ реестре (возможно, устарел/вычищен с пилота)"
                results.append(entry)
                continue
            # РЕАЛЬНЫЙ hp.recompute_route -- та же функция, что использовал пилот, на ТОМ ЖЕ
            # историческом блоке (проверка архивной глубины публичного RPC -- честно, не гадаем)
            recompute = hp.recompute_route(route, block)
            entry["recompute_route_at_signal_block"] = recompute
            if recompute.get("ok"):
                amount_in = recompute["amount_in"]
                calldata = build_execute_cycle_calldata(route, -amount_in, min_profit=1)
                entry["reconstructed_calldata_note"] = (
                    "calldata ПОСТРОЕНА реальной build_execute_cycle_calldata из amount_in, реально "
                    "выбранного recompute_route на СИГНАЛЬНОМ блоке -- это ТА ЖЕ логика, что вызвал бы "
                    "реальный пилот; НЕ реконструкция (в отличие от части 4, где amount_in не логировался)"
                )
                gas_latest = hp.estimate_gas(CONTRACT_ADDRESS, calldata, OWNER)
                entry["gas_estimate_at_latest"] = gas_latest
                try:
                    gas_hist = _rpc_call("eth_estimateGas", [{
                        "from": OWNER, "to": CONTRACT_ADDRESS, "data": "0x" + calldata.hex(),
                    }, hex(block)])
                    entry["gas_estimate_at_signal_block"] = {"ok": True, "gas_estimate": int(gas_hist, 16)}
                except Exception as exc:  # noqa: BLE001
                    entry["gas_estimate_at_signal_block"] = {"ok": False, "error": str(exc)}
            results.append(entry)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
            results.append(entry)
    return results


def part4_four_reverts(registry) -> list:
    results = []
    for route_id, ts_wall in FOUR_REVERTS:
        entry: dict = {"route_id": route_id, "ts_wall": ts_wall}
        try:
            block_lookup = find_block_by_timestamp(ts_wall)
            entry["block_lookup"] = block_lookup
            if not block_lookup.get("ok"):
                results.append(entry)
                continue
            block = block_lookup["block"]
            route = registry.routes.get(route_id) if registry is not None else None
            if route is None:
                entry["error"] = "route_id не найден в ТЕКУЩЕМ реестре"
                results.append(entry)
                continue
            recompute = hp.recompute_route(route, block)
            entry["recompute_route_at_estimated_attempt_block"] = recompute
            entry["reconstruction_note"] = (
                "ТОЧНЫЙ amount_in/calldata реального неудавшегося estimateGas-вызова В ЛОГЕ НЕ "
                "СОХРАНЁН (владелец предупредил об этом заранее) -- ниже RECONSTRUCTION: если "
                "recompute_route на оценённом блоке нашёл прибыльный размер, используем ЕГО (это "
                "правдоподобная, но НЕ подтверждённая точным логом реконструкция); иначе -- берём "
                "МИНИМАЛЬНЫЙ размер сетки как диагностический зонд."
            )
            if recompute.get("ok"):
                amount_in = recompute["amount_in"]
                amount_in_source = "recompute_route (RECONSTRUCTION -- не точный лог)"
            else:
                start_token = route.legs[0].input_currency.lower()
                grid = hp.SIZE_GRID_BY_START_TOKEN.get(start_token, hp.SIZE_GRID_BY_START_TOKEN[hp.USDG.lower()])
                amount_in = grid[0]
                amount_in_source = "минимальный размер сетки (RECONSTRUCTION, recompute_route не дал прибыльного варианта)"
            entry["reconstructed_amount_in"] = amount_in
            entry["reconstructed_amount_in_source"] = amount_in_source
            calldata = build_execute_cycle_calldata(route, -amount_in, min_profit=1)

            for label, block_tag in (("latest", None), ("at_estimated_attempt_block", block)):
                try:
                    call_obj = {"from": OWNER, "to": CONTRACT_ADDRESS, "data": "0x" + calldata.hex()}
                    params = [call_obj] if block_tag is None else [call_obj, hex(block_tag)]
                    raw = _rpc_call("eth_estimateGas", params)
                    entry[f"gas_estimate_{label}"] = {"ok": True, "gas_estimate": int(raw, 16)}
                except Exception as exc:  # noqa: BLE001
                    entry[f"gas_estimate_{label}"] = {"ok": False, "error": str(exc)}
                    msg = str(exc)
                    if "356680b7" in msg:
                        entry[f"reverted_with_0x356680b7_{label}"] = True
                        trace = try_debug_trace_call(call_obj, block_tag if block_tag is not None else "latest")
                        entry[f"debug_trace_call_{label}"] = trace
            results.append(entry)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
            results.append(entry)
    return results


def main() -> None:
    result: dict = {}
    print("[deep_dive] === ЧАСТЬ 1: контрольная сделка конкурента ===")
    result["part1_control_trade"] = part1_control_trade()
    print(json.dumps(result["part1_control_trade"], indent=2, default=str, ensure_ascii=False)[:4000])

    print("[deep_dive] === ЧАСТЬ 2: реестр -- поиск route_id (7 блоков + 4 отката) ===")
    part2 = part2_registry_lookup()
    registry = part2.pop("_registry_object", None)
    result["part2_registry_lookup"] = part2
    print(json.dumps(part2, indent=2, default=str, ensure_ascii=False)[:4000])

    print("[deep_dive] === ЧАСТЬ 3: 7 блоков (позитив до газа / негатив после газа) ===")
    result["part3_seven_blocks"] = part3_seven_blocks(registry)
    print(json.dumps(result["part3_seven_blocks"], indent=2, default=str, ensure_ascii=False))

    print("[deep_dive] === ЧАСТЬ 4: 4 отката estimateGas (0x356680b7) ===")
    result["part4_four_reverts"] = part4_four_reverts(registry)
    print(json.dumps(result["part4_four_reverts"], indent=2, default=str, ensure_ascii=False))

    out_path = Path(__file__).parent.parent / "data" / "task5_v4_control_and_revert_deep_dive_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[deep_dive] сохранено: {out_path}")


if __name__ == "__main__":
    main()
