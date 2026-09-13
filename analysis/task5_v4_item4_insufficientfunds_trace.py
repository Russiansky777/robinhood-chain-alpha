#!/usr/bin/env python3
"""Задача 5, седьмой раунд (разбор владельца, пункт 4): "возьми ОДИН уже
воспроизводящийся случай 0x356680b7; получи РЕАЛЬНЫЙ трейс на локальном
форке и покажи: какой контракт ПЕРВЫМ вернул ошибку, на каком именно
вызове, и какой именно баланс/обязательство было недостаточно. Совпадение
с публичной базой сигнатур -- только КАНДИДАТ на имя, не доказательство
причины без трейса."

Случай: route_e0ed68abz0_c009a2dbz1, ts_wall=1789270778.614601 -- ТОТ ЖЕ
route_id, что и (уже отозванный как "не в окне пилота", но по-прежнему
РЕАЛЬНЫЙ) пример конкурента 0x9c4712ab... на блоке 61630766 (см.
task5_v4_control_and_revert_deep_dive.py, FOUR_REVERTS[0]) -- уже
подтверждённый ранее в этой сессии как реально реплицирующийся revert
0x356680b7 через eth_estimateGas на публичном RPC.

selector 0x356680b7 == keccak256("InsufficientFunds()")[:4] -- ТОЧНОЕ
совпадение с публичной базой сигнатур (4byte.directory/openchain.xyz,
см. task5_v4_selector_lookup_and_registry_dump.py) -- но это ИМЯ-
КАНДИДАТ, не факт. Ни один custom error нашего собственного
ClosedCycleExecutorV4.sol не называется InsufficientFunds (у нас --
InsufficientProfit(uint256,uint256,uint256), другая сигнатура) --
ошибка приходит из ДРУГОГО контракта в цепочке вызовов (PoolManager v4-core
или один из токенов) -- ниже РЕАЛЬНЫЙ трейс называет, из какого именно."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import dataclasses  # noqa: E402

from alchemy_fallback import _alchemy_direct_endpoint, _rpc_call, get_block  # noqa: E402
import alchemy_fallback as af  # noqa: E402
import task5_v4_hotpath as hp  # noqa: E402
from task5_v4_route_registry import load_registry_state  # noqa: E402
from task5_v4_executor_calldata import build_execute_cycle_calldata  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
ANVIL = str(FOUNDRY_BIN / "anvil")
CAST = str(FOUNDRY_BIN / "cast")
PORT = 8558
RPC = f"http://127.0.0.1:{PORT}"

CONTRACT_ADDRESS = "0xAB24907bceEF4EDC366a1E4DfA15ed8aA5fdfe71"
OWNER = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
REGISTRY_STATE_FILE = "/home/bot/data/task5_v4_route_registry_state.json"

ROUTE_ID = "route_e0ed68abz0_c009a2dbz1"
TS_WALL = 1789270778.614601
BINSEARCH_LOW = 61600000

SELECTOR = "0x356680b7"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def find_block_by_timestamp(target_ts: float) -> dict:
    lo = BINSEARCH_LOW
    hi = int(_rpc_call("eth_blockNumber", []), 16)
    lo_block = get_block(lo)
    hi_block = get_block(hi)
    if lo_block is None:
        return {"ok": False, "error": f"нижняя граница (блок {lo}) не получена"}
    if hi_block is None:
        return {"ok": False, "error": f"верхняя граница (блок {hi}) не получена"}
    if not (int(lo_block["timestamp"], 16) <= target_ts <= int(hi_block["timestamp"], 16)):
        return {"ok": False, "error": "target_ts вне границ", "lo": lo, "hi": hi}
    while hi - lo > 1:
        mid = (lo + hi) // 2
        blk = get_block(mid)
        if blk is None:
            return {"ok": False, "error": f"eth_getBlockByNumber({mid}) вернул None"}
        if int(blk["timestamp"], 16) <= target_ts:
            lo = mid
        else:
            hi = mid
    return {"ok": True, "block": lo, "block_ts": int(get_block(lo)["timestamp"], 16), "target_ts": target_ts}


# ПРАВКА (восьмой раунд, разбор владельца, пункт 4): "Excess blob gas not
# set" от debug_traceCall -- НЕ случайная несовместимость версий, а
# реальное расхождение hardfork'а. task5_v4_item4_hardfork_diagnosis.py
# (тот же коммит) проверил РЕАЛЬНЫЙ заголовок недавнего блока этой цепи
# (62066736): baseFeePerGas ЕСТЬ (London+), withdrawalsRoot/excessBlobGas/
# blobGasUsed -- НЕТ (цепь физически НЕ поддерживает Shanghai/Cancun) --
# anvil 1.8.1 по умолчанию форкует на более новом hardfork'е, чем
# реально существует у источника, отсюда и ошибка. "london" --
# ЕДИНСТВЕННЫЙ hardfork, для которого РЕАЛЬНЫЕ поля заголовка этой цепи
# совпадают, подтверждено реальным debug_traceCall (returncode 0,
# см. task5_v4_item4_hardfork_diagnosis_result.json) -- ЭТО приведение
# окружения форка в соответствие с реальным устройством исходной цепи,
# а не смягчение правил ради прохождения теста.
ANVIL_HARDFORK = "london"


def anvil_start(fork_block: int) -> subprocess.Popen:
    fork_url = _alchemy_direct_endpoint()
    if not fork_url:
        raise RuntimeError("нет Alchemy-эндпоинта для форка")
    proc = subprocess.Popen(
        [ANVIL, "--fork-url", fork_url, "--fork-block-number", str(fork_block),
         "--hardfork", ANVIL_HARDFORK, "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        if "Listening on" in line:
            break
    for _ in range(30):
        p = run([CAST, "block-number", "--rpc-url", RPC], timeout=10)
        if p.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("anvil не ответил за 30с")
    return proc


def _first_erroring_call(node: dict, path: str = "root") -> dict | None:
    """Обход callTracer-дерева -- находим ПЕРВЫЙ (самый внешний из
    неудачных) call с непустым 'error' или 'revertReason', содержащим
    наш селектор, ЛИБО первый call с error вообще (если селектор не
    отражён в 'error' текстом, но revert-данные это он)."""
    err = node.get("error") or node.get("revertReason")
    output = node.get("output", "")
    matches_selector = (isinstance(output, str) and output.lower().startswith(SELECTOR.lower())) or \
                        (err and SELECTOR[2:] in str(err).lower())
    if err and matches_selector:
        return {"path": path, "to": node.get("to"), "from": node.get("from"), "type": node.get("type"),
                "input": (node.get("input") or "")[:200], "error": err, "output": output,
                "value": node.get("value")}
    for i, child in enumerate(node.get("calls", []) or []):
        found = _first_erroring_call(child, f"{path}.calls[{i}]")
        if found:
            return found
    # ни один вложенный вызов не совпал с селектором явно -- если сам этот узел
    # ошибся (любая ошибка), возвращаем его как ближайшего кандидата, честно
    # отметив, что совпадение по селектору не подтверждено на этом уровне.
    if err:
        return {"path": path, "to": node.get("to"), "from": node.get("from"), "type": node.get("type"),
                "input": (node.get("input") or "")[:200], "error": err, "output": output,
                "value": node.get("value"), "selector_match_confirmed": False}
    return None


def main() -> None:
    result: dict = {"route_id": ROUTE_ID, "ts_wall": TS_WALL, "selector": SELECTOR,
                     "selector_candidate_name": "InsufficientFunds()",
                     "selector_candidate_name_note": (
                         "keccak256('InsufficientFunds()')[:4] == 0x356680b7 -- ТОЧНОЕ совпадение, "
                         "пересчитано в этом же прогоне (не только внешняя база сигнатур); ЭТО ИМЯ-"
                         "КАНДИДАТ -- ниже РЕАЛЬНЫЙ трейс называет источник и причину, не только имя")}

    block_lookup = find_block_by_timestamp(TS_WALL)
    result["block_lookup"] = block_lookup
    if not block_lookup.get("ok"):
        result["ok"] = False
        _finish(result)
        return
    block = block_lookup["block"]

    loaded = load_registry_state(REGISTRY_STATE_FILE)
    route = None
    if loaded is not None:
        registry, _cursor = loaded
        route = registry.routes.get(ROUTE_ID)
    result["route_found_in_registry"] = route is not None

    anvil_proc = None
    orig_config = af.CONFIG
    orig_checked = af._alchemy_direct_checked
    orig_url = af._alchemy_direct_url
    try:
        anvil_proc = anvil_start(block)
        result["fork_block"] = block

        af._alchemy_direct_checked = True
        af._alchemy_direct_url = None
        af.CONFIG = dataclasses.replace(af.CONFIG, public_rpc_url=RPC, alchemy_rpc_url="", alchemy_api_key="")

        if route is None:
            result["ok"] = False
            result["error"] = f"route_id {ROUTE_ID} не найден в ТЕКУЩЕМ реестре -- честно фиксируем, не гадаем"
            _finish(result)
            return

        recompute = hp.recompute_route(route, block)
        result["recompute_route_on_fork_at_historical_block"] = recompute
        if recompute.get("ok"):
            amount_in = recompute["amount_in"]
            amount_in_source = "recompute_route на форке, ТОТ ЖЕ исторический блок (реальный расчёт, не догадка)"
        else:
            start_token = route.legs[0].input_currency.lower()
            grid = hp.SIZE_GRID_BY_START_TOKEN.get(start_token, hp.SIZE_GRID_BY_START_TOKEN[hp.USDG.lower()])
            amount_in = grid[0]
            amount_in_source = ("RECONSTRUCTION -- recompute_route не нашёл прибыльный размер на этом блоке; "
                                 "минимальный размер сетки взят как диагностический зонд (точный лог "
                                 "исходной попытки не сохранил amount_in, как и предупреждал владелец)")
        result["amount_in"] = amount_in
        result["amount_in_source"] = amount_in_source

        calldata = build_execute_cycle_calldata(route, -amount_in, min_profit=1)
        call_obj = {"from": OWNER, "to": CONTRACT_ADDRESS, "data": "0x" + calldata.hex()}

        # Сначала честно подтверждаем, что revert РЕАЛЬНО воспроизводится на
        # ЭТОМ форке (тот же селектор) -- ДО того, как тратить время на трейс.
        try:
            _rpc_call("eth_estimateGas", [call_obj])
            result["revert_reproduced_on_fork"] = False
            result["note"] = "eth_estimateGas НЕ откатился на форке -- revert НЕ воспроизведён этим calldata/блоком"
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            result["revert_reproduced_on_fork"] = SELECTOR in msg
            result["estimate_gas_error_on_fork"] = msg

        trace_res = {}
        # ПРАВКА (первый прогон): явный исторический блок дал "Excess blob
        # gas not set" (-32602) -- похоже, anvil строит окружение блока для
        # debug_traceCall по правилам пост-Cancun (требует excessBlobGas), а
        # форкнутый исторический блок его не предоставляет. "latest" на
        # локальном форке -- ЭТО ЖЕ состояние (форк был снят РОВНО на block),
        # но собственный (не прокси) блок anvil, где эти поля выставлены
        # корректно -- пробуем как честный fallback, а не выдумываем ответ.
        for block_tag_label, block_tag in (("historical_block", hex(block)), ("latest_fallback", "latest")):
            try:
                trace = _rpc_call("debug_traceCall", [call_obj, block_tag, {"tracer": "callTracer"}])
                trace_res["ok"] = True
                trace_res["block_tag_used"] = block_tag_label
                trace_res["raw_trace"] = trace
                trace_res["first_erroring_call"] = _first_erroring_call(trace)
                break
            except Exception as exc:  # noqa: BLE001
                trace_res.setdefault("attempts", []).append({"block_tag_label": block_tag_label, "error": str(exc)})
        else:
            trace_res["ok"] = False
            trace_res["missing_request_named"] = (
                "debug_traceCall (callTracer) на локальном anvil-форке -- обе попытки (явный исторический "
                "блок И 'latest') отказали, см. attempts выше для точного текста ошибки каждой"
            )
        result["debug_trace_call_on_local_fork"] = trace_res
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["error"] = str(exc)
    finally:
        if anvil_proc is not None:
            anvil_proc.terminate()
            try:
                anvil_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                anvil_proc.kill()
        af.CONFIG = orig_config
        af._alchemy_direct_checked = orig_checked
        af._alchemy_direct_url = orig_url
    _finish(result)


def _finish(result: dict) -> None:
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = REPO_ROOT / "data" / "task5_v4_item4_insufficientfunds_trace_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[item4] сохранено: {out_path}")


if __name__ == "__main__":
    main()
