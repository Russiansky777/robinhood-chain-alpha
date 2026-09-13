#!/usr/bin/env python3
"""Задача 5, продолжение замера скорости (владелец: "старые 37.79 с и
новые 0.20 с относятся к разным операциям" -- 37.79 с из журнала пилота
это медиана calc_duration_s ПОЛНОГО _evaluate_and_maybe_send (включая
recompute_route -- полный перебор сетки -- ПЛЮС estimate_gas/gasPrice/
согласование minProfit), а 0.20 с из прошлого замера -- ОДНА цепочка
котировок ОДНОГО размера. Эта правка меряет ПОЛНЫЙ recompute_route()
(вся сетка) целиком, старым и новым RPC-путём, НА ОДНОМ И ТОМ ЖЕ
зафиксированном блоке -- чисто читающий, ничего не пишет/не отправляет,
новых LIVE/широких сканов/скана реестра не запускает."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import requests  # noqa: E402

import task5_v4_hotpath as hp  # noqa: E402
import task5_v4_quote_replay as qr  # noqa: E402
from alchemy_fallback import _rpc_call  # noqa: E402
from task5_v4_route_registry import RouteCycle, RouteLeg  # noqa: E402
from task5_v4_revert_decode import decode_v4_revert_detail  # noqa: E402

REGISTRY_STATE_FILE = Path("/home/bot/data/task5_v4_route_registry_state.json")
NATIVE = "0x0000000000000000000000000000000000000000"


class _Instrumentation:
    """Тот же приём, что в task5_v4_rpc_speed_measurement.py: монки-патч
    requests.post/time.sleep на время `with`, считает РЕАЛЬНЫЕ HTTP-
    вызовы и РЕАЛЬНЫЕ паузы, поведение не меняет."""

    def __init__(self) -> None:
        self.n_http_calls = 0
        self.http_time_s = 0.0
        self.n_sleeps = 0
        self.sleep_time_s = 0.0
        self._orig_post = requests.post
        self._orig_sleep = time.sleep

    def __enter__(self) -> "_Instrumentation":
        def wrapped_post(*args, **kwargs):
            t0 = time.monotonic()
            try:
                return self._orig_post(*args, **kwargs)
            finally:
                self.n_http_calls += 1
                self.http_time_s += time.monotonic() - t0
        def wrapped_sleep(seconds):
            self.n_sleeps += 1
            self.sleep_time_s += seconds
            self._orig_sleep(seconds)
        requests.post = wrapped_post
        time.sleep = wrapped_sleep
        return self

    def __exit__(self, *exc) -> None:
        requests.post = self._orig_post
        time.sleep = self._orig_sleep

    def as_dict(self) -> dict:
        return {"n_http_calls": self.n_http_calls, "http_time_s": round(self.http_time_s, 4),
                "n_sleep_calls": self.n_sleeps, "sleep_time_s": round(self.sleep_time_s, 4)}


def _route_from_legs(rid: str, legs_json: list[dict]) -> RouteCycle:
    legs = tuple(RouteLeg(l["currency0"], l["currency1"], l["fee"], l["tick_spacing"], l["hooks"], l["zero_for_one"])
                 for l in legs_json)
    return RouteCycle(rid, legs, legs[0].input_currency, "full_recompute_measurement", "discovered")


def _candidate_hooked_3leg_routes(registry: dict) -> list[tuple[str, list[dict]]]:
    out = []
    for rid, r in registry.get("routes", {}).items():
        legs = r.get("legs", [])
        if len(legs) == 3 and any(l["hooks"].lower() != NATIVE for l in legs):
            out.append((rid, legs))
    return out


def _probe_smallest_size_ok(route: RouteCycle, block_number: int) -> bool:
    """Один быстрый пробный quote на САМОМ МАЛЕНЬКОМ размере сетки (не
    полный перебор) -- чтобы честно найти маршрут, где хотя бы один
    размер РЕАЛЬНО котируется на этом блоке (владелец: "не называй
    быстрое завершение на первом revert скоростью полного успешного
    перебора" -- для этого нужен маршрут, где полный перебор ДЕЙСТВИТЕЛЬНО
    происходит, не обрывается на первом же размере)."""
    start_token = route.legs[0].input_currency.lower()
    grid = hp.SIZE_GRID_BY_START_TOKEN.get(start_token, hp.SIZE_GRID_BY_START_TOKEN[hp.USDG.lower()])
    res = hp.quote_route_at_size(route, grid[0], block_number)
    return res["ok"]


def _run_full_recompute(route: RouteCycle, block_number: int, use_new_path: bool) -> dict:
    orig_quote_single = hp.quote_exact_input_single
    if not use_new_path:
        # ПРАВКА: принудительно ВОСПРОИЗВОДИМ дефолтный (старый, ДО этой
        # правки) путь -- quote_exact_input_single БЕЗ инъекции rpc_call
        # (внутри неё это == прежний _rpc_call, публичный-RPC-первый,
        # 0.5с троттлинг, ОБЩИЙ с фоном). Реальная функция та же самая
        # (qr.quote_exact_input_single), математика/calldata НЕ меняются --
        # меняется ТОЛЬКО то, какая RPC-функция уходит внутрь.
        def old_path_quote_single(pool_key, zero_for_one, amount_in, blk, rpc_call=None):  # noqa: ARG001
            return qr.quote_exact_input_single(pool_key, zero_for_one, amount_in, blk)
        hp.quote_exact_input_single = old_path_quote_single

    hp._reset_rpc_call_count()
    grid = hp.SIZE_GRID_BY_START_TOKEN.get(route.legs[0].input_currency.lower(),
                                            hp.SIZE_GRID_BY_START_TOKEN[hp.USDG.lower()])
    n_sizes_in_grid = len(grid)
    try:
        with _Instrumentation() as instr:
            t0 = time.monotonic()
            result = hp.recompute_route(route, block_number)
            total_s = time.monotonic() - t0
    finally:
        hp.quote_exact_input_single = orig_quote_single

    internal_rpc_counter = hp._read_rpc_call_count()
    n_sizes_tried = None
    if result.get("ok"):
        n_sizes_tried = grid.index(result["amount_in"]) + 1 if result["amount_in"] in grid else None
    out = {"result": result, "n_sizes_in_full_grid": n_sizes_in_grid, "n_sizes_tried_estimate": n_sizes_tried,
           "total_duration_s": round(total_s, 4),
           "internal_rpc_call_counter_hotpath_wrapper_only": internal_rpc_counter}
    out.update(instr.as_dict())
    return out


def main() -> None:
    result: dict = {}
    registry = json.loads(REGISTRY_STATE_FILE.read_text())
    candidates = _candidate_hooked_3leg_routes(registry)
    result["n_hooked_3leg_candidates_in_registry"] = len(candidates)

    latest = int(_rpc_call("eth_blockNumber", []), 16)
    result["fixed_block_used"] = latest
    result["note_on_block"] = ("Один зафиксированный latest, ОДИН РАЗ полученный ДО обоих прогонов -- "
                                "используется ОДИНАКОВО и для старого, и для нового пути (не два "
                                "независимых latest).")

    chosen = None
    probe_log = []
    for rid, legs_json in candidates:
        route = _route_from_legs(rid, legs_json)
        try:
            ok = _probe_smallest_size_ok(route, latest)
        except Exception as exc:  # noqa: BLE001
            ok = False
            probe_log.append({"route_id": rid, "probe_error": str(exc)[:200]})
            continue
        probe_log.append({"route_id": rid, "smallest_size_quote_ok": ok})
        if ok:
            chosen = (rid, legs_json)
            break
    result["probe_log_smallest_size"] = probe_log

    only_reverts_available = chosen is None
    if chosen is None:
        # Честное ограничение (владелец, пункт 4): ни один кандидат не
        # дал успешную котировку даже на самом маленьком размере сетки на
        # этом блоке -- берём ПЕРВОГО кандидата для сравнения "как быстро
        # ловится подтверждённый revert", НЕ выдаём это за замер полного
        # успешного перебора.
        chosen = candidates[0] if candidates else None
    result["measurement_limited_to_reverts_only"] = only_reverts_available

    if chosen is None:
        result["ok"] = False
        result["error"] = "в реестре нет ни одного 3-плечевого маршрута с хуком"
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return

    rid, legs_json = chosen
    route = _route_from_legs(rid, legs_json)
    result["chosen_route_id"] = rid
    result["chosen_route_legs_hooks"] = [l["hooks"] for l in legs_json]

    old = _run_full_recompute(route, latest, use_new_path=False)
    new = _run_full_recompute(route, latest, use_new_path=True)

    result["old"] = old
    result["new"] = new

    old_res, new_res = old["result"], new["result"]
    if old_res.get("ok") and new_res.get("ok"):
        results_match = (old_res["amount_in"] == new_res["amount_in"]
                          and old_res["amount_out"] == new_res["amount_out"])
    elif not old_res.get("ok") and not new_res.get("ok"):
        results_match = (old_res.get("reason") == new_res.get("reason")
                          and decode_v4_revert_detail(old_res.get("detail", ""))["outer_selector"]
                          == decode_v4_revert_detail(new_res.get("detail", ""))["outer_selector"])
    else:
        results_match = False
    result["results_match"] = results_match

    result["ok"] = True
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
