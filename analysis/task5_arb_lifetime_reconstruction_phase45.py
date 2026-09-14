#!/usr/bin/env python3
"""Продолжение task5_arb_lifetime_reconstruction.py -- фазы 4-5, ПОСЛЕ
честного результата фазы 2: архивный eth_call/debug_traceCall на этом
RPC-провайдере НЕДОСТУПЕН ни на B-1, ни на B-100 (реальная ошибка
'-32000 metadata is not found' на ЛЮБОМ историческом блоке) --
следовательно ТОЧНАЯ пересимуляция маршрута (V4Quoter/V3-математика) на
произвольном историческом блоке НЕВОЗМОЖНА физически, не только дорого.
Это НЕ пересчитывается заново здесь -- используется как установленный
факт предыдущего прогона (data/task5_arb_lifetime_reconstruction_result.json).

ЧЕСТНАЯ ЗАМЕНА (не выдаём приближённую котировку за исполнимую
прибыль): вместо "прибыль на каждом из 100 блоков" (невозможно без
архивного state) используем то, что РЕАЛЬНО доступно без state --
eth_getLogs по уже смайненным блокам работает независимо от архивной
глубины state (логи -- часть receipt, хранятся постоянно). Строим
ТАЙМЛАЙН РЕАЛЬНЫХ событий (Swap/ModifyLiquidity/любое другое) на все 3
пула цикла в [B-100, B] -- если состояние ВСЕХ трёх пулов НЕ менялось
от блока X до B-1, то РЕАЛЬНО исполненная в B сделка (уже известный,
не смоделированный результат) дала бы ТОТ ЖЕ результат на любом блоке
[X+1, B-1] -- это логический вывод из неизменности состояния, а НЕ
новая симуляция. Явная оговорка: хук (AFTER_SWAP_RETURNS_DELTA=true,
подтверждено фазой 1) может иметь ВНУТРЕННЕЕ состояние вне трёх пулов
(неизвестное, исходник хука не читан) -- эта проверка НЕ покрывает
такую зависимость, честно помечаем как непроверенную."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import alchemy_fallback as _af  # noqa: E402

# --- бюджет: ПРОДОЛЖЕНИЕ единого бюджета задачи (фаза 1-3 уже
# потратила 12 RPC-вызовов и 7 секунд -- реальные числа из
# data/task5_arb_lifetime_reconstruction_result.json этой же сессии).
PRIOR_CALLS_USED = 12
PRIOR_ELAPSED_S = 7
RPC_BUDGET = 300
TIME_BUDGET_S = 45 * 60
START_WALL = time.time() - PRIOR_ELAPSED_S
CALL_LOG: list[dict] = [{"phase": "1-3", "note": "уже потрачено в предыдущем прогоне"}] * 0
N_CALLS_THIS_RUN = 0


class BudgetExceeded(Exception):
    pass


_orig_rpc_call = _af._rpc_call


def _counted_rpc_call(method, params):
    global N_CALLS_THIS_RUN
    total_used = PRIOR_CALLS_USED + N_CALLS_THIS_RUN
    if total_used >= RPC_BUDGET:
        raise BudgetExceeded(f"RPC_BUDGET={RPC_BUDGET} исчерпан (уже {total_used}) перед {method}")
    elapsed = time.time() - START_WALL
    if elapsed > TIME_BUDGET_S:
        raise BudgetExceeded(f"TIME_BUDGET={TIME_BUDGET_S}с исчерпан ({elapsed:.0f}с) перед {method}")
    N_CALLS_THIS_RUN += 1
    t0 = time.time()
    try:
        result = _orig_rpc_call(method, params)
        CALL_LOG.append({"method": method, "params_summary": str(params)[:150], "dt": time.time() - t0, "ok": True})
        return result
    except Exception as exc:  # noqa: BLE001
        CALL_LOG.append({"method": method, "params_summary": str(params)[:150], "dt": time.time() - t0,
                          "ok": False, "error": str(exc)})
        raise


_af._rpc_call = _counted_rpc_call

from alchemy_fallback import _chunked_get_logs, topic0  # noqa: E402

TX_HASH = "0xcc630e7d525909dd32453101baf311588813279edb5a97e0d386ea9cc4c2ede3"
B = 62_790_344
FROM_BLOCK = B - 100
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
V3_POOL = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
POOL1_ID = "0x935c1401e40a4eeec0e722f1841cc4be9d7a523cf90cdc52900f9e343304f0fe"
POOL2_ID = "0x0086a03ec05c3115a0bb90e0d00e84b6228ceb518769f205b4d52a3d432a8fa2"
EXECUTOR = "0xed4728d89bd4ef81177d8f5448f9bd1bae4e23e3"

MODIFY_LIQUIDITY_TOPIC0 = topic0("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")
SWAP_V4_TOPIC0 = topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
DONATE_TOPIC0 = topic0("Donate(bytes32,address,uint256,uint256)")

RESULT: dict = {
    "tx_hash": TX_HASH, "block_B": B, "from_block": FROM_BLOCK,
    "prior_phase_calls_used": PRIOR_CALLS_USED,
    "phase4_state_change_timeline": {},
    "phase5_origin_search": {},
    "method_note": (
        "Архивный eth_call/debug_traceCall НЕДОСТУПЕН на этом RPC-провайдере "
        "(установлено в фазе 2, реальная ошибка 'metadata is not found' на "
        "любом историческом блоке, включая B-1) -- точная пересимуляция "
        "маршрута на произвольном блоке физически невозможна. Вместо неё: "
        "таймлайн РЕАЛЬНЫХ eth_getLogs событий на все 3 пула (логи не требуют "
        "архивного state) -- если состояние не менялось от блока X до B-1, "
        "УЖЕ ИЗВЕСТНЫЙ реальный результат B применим и к этому диапазону "
        "БЕЗ новой симуляции. НЕ покрывает возможную внутреннюю зависимость "
        "хука от состояния вне этих 3 пулов (исходник хука не читан)."
    ),
    "stopped_early": None,
}
OUT_PATH = Path(__file__).parent.parent / "data" / "task5_arb_lifetime_reconstruction_phase45_result.json"


def _save():
    RESULT["n_rpc_calls_this_run"] = N_CALLS_THIS_RUN
    RESULT["n_rpc_calls_total"] = PRIOR_CALLS_USED + N_CALLS_THIS_RUN
    RESULT["elapsed_s_total"] = time.time() - START_WALL
    RESULT["call_log"] = CALL_LOG
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(RESULT, indent=2, ensure_ascii=False, default=str))


def phase4_timeline() -> None:
    out = RESULT["phase4_state_change_timeline"]

    # pool1: ЛЮБОЕ событие PoolManager с topics[1]==pool1_id (Swap ИЛИ
    # ModifyLiquidity ИЛИ Donate -- все три индексируют poolId в topics[1]
    # в реальной v4-core схеме событий).
    try:
        logs1 = list(_chunked_get_logs(FROM_BLOCK, B, topics=[None, POOL1_ID], address=POOL_MANAGER,
                                        chunk_size=B - FROM_BLOCK + 1))
        out["pool1_events_in_window"] = [
            {"block": int(l["blockNumber"], 16), "tx_hash": l["transactionHash"],
             "log_index": l["logIndex"], "topic0": l["topics"][0]} for l in logs1
        ]
    except Exception as exc:  # noqa: BLE001
        out["pool1_events_error"] = str(exc)

    try:
        logs2 = list(_chunked_get_logs(FROM_BLOCK, B, topics=[None, POOL2_ID], address=POOL_MANAGER,
                                        chunk_size=B - FROM_BLOCK + 1))
        out["pool2_events_in_window"] = [
            {"block": int(l["blockNumber"], 16), "tx_hash": l["transactionHash"],
             "log_index": l["logIndex"], "topic0": l["topics"][0]} for l in logs2
        ]
    except Exception as exc:  # noqa: BLE001
        out["pool2_events_error"] = str(exc)

    # V3 pool -- ВСЕ его собственные события (Swap/Mint/Burn/Collect/Flash).
    try:
        logs3 = list(_chunked_get_logs(FROM_BLOCK, B, topics=[], address=V3_POOL,
                                        chunk_size=B - FROM_BLOCK + 1))
        out["v3_pool_events_in_window"] = [
            {"block": int(l["blockNumber"], 16), "tx_hash": l["transactionHash"],
             "log_index": l["logIndex"], "topic0": (l["topics"][0] if l["topics"] else None)} for l in logs3
        ]
    except Exception as exc:  # noqa: BLE001
        out["v3_pool_events_error"] = str(exc)

    # Хук -- ВСЕ его события (не только на наших пулах) -- честная
    # проверка "занят ли хук чем-то ещё, что могло бы означать
    # независимое от наших 3 пулов внутреннее состояние".
    try:
        logs_hook = list(_chunked_get_logs(FROM_BLOCK, B, topics=[], address=HOOK,
                                            chunk_size=B - FROM_BLOCK + 1))
        out["hook_events_in_window_total"] = len(logs_hook)
        out["hook_events_sample"] = [
            {"block": int(l["blockNumber"], 16), "tx_hash": l["transactionHash"],
             "topics": l["topics"]} for l in logs_hook[-10:]
        ]
    except Exception as exc:  # noqa: BLE001
        out["hook_events_error"] = str(exc)

    # Итоговый вывод: последний блок ДО B, где хоть один из 3 пулов
    # реально менялся (Swap/ModifyLiquidity/Donate).
    all_events = []
    for key in ("pool1_events_in_window", "pool2_events_in_window", "v3_pool_events_in_window"):
        all_events.extend(out.get(key, []))
    events_before_b = [e for e in all_events if e["block"] < B]
    if events_before_b:
        last_change_block = max(e["block"] for e in events_before_b)
        out["last_state_changing_event_before_B"] = last_change_block
        out["state_constant_window"] = [last_change_block + 1, B - 1]
        out["reached_left_boundary_B_minus_100"] = last_change_block < FROM_BLOCK
    else:
        out["last_state_changing_event_before_B"] = None
        out["state_constant_window"] = [FROM_BLOCK, B - 1]
        out["reached_left_boundary_B_minus_100"] = True
        out["note"] = ("Ни один из 3 пулов не менялся ВООБЩЕ во всём окне [B-100, B-1] -- "
                        "достигнута ЛЕВАЯ ГРАНИЦА разрешённого окна, точный момент "
                        "возникновения ВНЕ этого окна, не выдумываем его.")

    _save()


def phase5_origin_search() -> None:
    out = RESULT["phase5_origin_search"]

    # Блок B: полный список транзакций + какие из них (кроме уже
    # известного исполнителя) реально касаются наших 3 пулов --
    # честная перепроверка (не переиспользуем слепо прошлый вывод).
    try:
        block_b = _af._rpc_call("eth_getBlockByNumber", [hex(B), True])
        txs_b = block_b.get("transactions", [])
        out["block_B_tx_count"] = len(txs_b)
        out["block_B_tx_order"] = [
            {"tx_index": int(t.get("transactionIndex", "0x0"), 16), "hash": t.get("hash"),
             "from": t.get("from"), "to": t.get("to")} for t in txs_b
        ]
    except Exception as exc:  # noqa: BLE001
        out["block_B_error"] = str(exc)

    # Если last_state_changing_event_before_B попадает НЕ в блок B --
    # это блок A. Если попадает в САМ блок B (индекс < 4, до нашей tx) --
    # значит возможность могла возникнуть ВНУТРИ блока B.
    last_change = RESULT["phase4_state_change_timeline"].get("last_state_changing_event_before_B")
    if last_change is not None:
        out["candidate_block_A"] = last_change
        if last_change == B:
            out["origin_inside_block_B"] = True
            # найти, какая именно из tx блока B (с индексом < 4) это
            all_events = []
            for key in ("pool1_events_in_window", "pool2_events_in_window", "v3_pool_events_in_window"):
                all_events.extend(RESULT["phase4_state_change_timeline"].get(key, []))
            trigger_candidates = [e for e in all_events if e["block"] == B]
            out["trigger_candidates_in_block_B"] = trigger_candidates
        else:
            out["origin_inside_block_B"] = False
            # получить порядок tx в блоке A, чтобы найти конкретный триггер
            try:
                block_a = _af._rpc_call("eth_getBlockByNumber", [hex(last_change), True])
                txs_a = block_a.get("transactions", [])
                out["block_A_tx_count"] = len(txs_a)
                all_events = []
                for key in ("pool1_events_in_window", "pool2_events_in_window", "v3_pool_events_in_window"):
                    all_events.extend(RESULT["phase4_state_change_timeline"].get(key, []))
                trigger_candidates = [e for e in all_events if e["block"] == last_change]
                out["trigger_candidates_in_block_A"] = trigger_candidates
                out["block_A_tx_order"] = [
                    {"tx_index": int(t.get("transactionIndex", "0x0"), 16), "hash": t.get("hash"),
                     "from": t.get("from"), "to": t.get("to")} for t in txs_a
                ]
            except Exception as exc:  # noqa: BLE001
                out["block_A_error"] = str(exc)
    else:
        out["candidate_block_A"] = None
        out["note"] = "Пулы не менялись во всём окне -- см. reached_left_boundary_B_minus_100"

    _save()


def main() -> None:
    try:
        phase4_timeline()
        phase5_origin_search()
    except BudgetExceeded as exc:
        RESULT["stopped_early"] = str(exc)
        _save()
    _save()
    print(json.dumps(RESULT, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
