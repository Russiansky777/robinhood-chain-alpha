#!/usr/bin/env python3
"""Задача 5, быстрый предварительный отбор -- локальные точечные
проверки (без сети) РЕАЛЬНОГО кода (task5_v4_pool_state_cache.py +
HotPath._admit_touched_route/_mark_hook_review/poll_once), в том же
стиле, что round3-8 pointchecks: РЕАЛЬНЫЕ классы, застабленные RPC-
вызовы, не переписанная копия логики."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", "http://stub-not-used")

import task5_v4_hotpath as hp  # noqa: E402
import task5_v4_route_registry as rr  # noqa: E402
from task5_v4_pool_state_cache import PoolStateCache, cheap_filter_route, CONFIRMED_HOOK_MODELS  # noqa: E402
from task5_v4_pilot_accounting import AttemptTable, PilotBudget, ReasonLog  # noqa: E402

RESULTS = []


def _record(name, desc, ok, detail=""):
    RESULTS.append({"name": name, "desc": desc, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {desc}\n        {detail}")


def _make_hotpath(tmp_path):
    registry = rr.RouteRegistry()
    budget = PilotBudget(state_path=str(tmp_path / "budget.json"))
    attempt_table = AttemptTable(str(tmp_path / "attempts.jsonl"))
    reason_log = ReasonLog(str(tmp_path / "reasons.jsonl"))
    priority_hint = hp._RpcPriorityHint()
    hotpath = hp.HotPath(registry, "0x0000000000000000000000000000000000000009", "0x0",
                          budget, attempt_table, reason_log, priority_hint, sender=None, dry_run=True)
    return hotpath, registry, reason_log


def check1_hookless_favorable_admitted(tmp_path):
    hotpath, registry, _ = _make_hotpath(tmp_path)
    key_a = rr.PoolKey(rr.USDG, rr.MOSIAI, 3000, 60, rr.NATIVE)  # 0.3% fee, hookless
    pid = rr.pool_id(key_a)
    route = rr.RouteCycle("route_fav", (rr.RouteLeg(key_a.currency0, key_a.currency1, key_a.fee,
                                                     key_a.tick_spacing, key_a.hooks, True),),
                          rr.USDG, "fav-route", "discovered")
    registry.add_route(route)
    registry.set_liveness(route.route_id, {"live": True})
    # sqrtPriceX96 такой, что raw_price сильно > 1 (заведомо "расхождение" на этом фильтре)
    sqrt_price = int((2.0 ** 0.5) * (2 ** 96))
    hotpath._pool_cache.apply_swap_batch([{
        "pool_id": pid, "block_number": 100, "log_index": 0,
        "sqrt_price_x96": sqrt_price, "liquidity": 10 ** 20, "tick": 1000, "fee": 3000,
    }])
    hotpath._admit_touched_route(route, 100, time.monotonic())
    item = hotpath._queue.pop_one(timeout_s=0.1)
    ok = item is not None and item[0] == route.route_id
    _record("HP.1", "hookless-маршрут с выгодной кэшированной ценой -- в ГЛАВНУЮ очередь",
            ok, f"main queue item={item}")


def check2_hookless_unfavorable_dropped_silently(tmp_path):
    hotpath, registry, reason_log = _make_hotpath(tmp_path)
    key_a = rr.PoolKey(rr.USDG, rr.MOSIAI, 3000, 60, rr.NATIVE)
    pid = rr.pool_id(key_a)
    route = rr.RouteCycle("route_bad", (rr.RouteLeg(key_a.currency0, key_a.currency1, key_a.fee,
                                                     key_a.tick_spacing, key_a.hooks, True),),
                          rr.USDG, "bad-route", "discovered")
    registry.add_route(route)
    registry.set_liveness(route.route_id, {"live": True})
    sqrt_price = int((0.5 ** 0.5) * (2 ** 96))  # raw_price < 1 -- после fee тем более < 1
    hotpath._pool_cache.apply_swap_batch([{
        "pool_id": pid, "block_number": 100, "log_index": 0,
        "sqrt_price_x96": sqrt_price, "liquidity": 10 ** 20, "tick": -1000, "fee": 3000,
    }])
    hotpath._admit_touched_route(route, 100, time.monotonic())
    item = hotpath._queue.pop_one(timeout_s=0.1)
    hook_item = hotpath._pop_hook_review_one()
    n_reason_lines = sum(1 for _ in open(reason_log.path)) if os.path.exists(reason_log.path) else 0
    ok = item is None and hook_item is None and n_reason_lines == 0
    _record("HP.2", "hookless-маршрут с невыгодной ценой -- НЕ в очередь, НЕ логируется (не посчитан)",
            ok, f"main={item}, hook_review={hook_item}, reason_log_lines={n_reason_lines}")


def check3_not_yet_initialized_dropped_silently_not_queued(tmp_path):
    hotpath, registry, _ = _make_hotpath(tmp_path)
    key_a = rr.PoolKey(rr.USDG, rr.MOSIAI, 3000, 60, rr.NATIVE)
    route = rr.RouteCycle("route_uninit", (rr.RouteLeg(key_a.currency0, key_a.currency1, key_a.fee,
                                                        key_a.tick_spacing, key_a.hooks, True),),
                          rr.USDG, "uninit-route", "discovered")
    registry.add_route(route)
    registry.set_liveness(route.route_id, {"live": True})
    # НЕ применяем apply_swap_batch -- кэш для этого пула пуст
    hotpath._admit_touched_route(route, 100, time.monotonic())
    item = hotpath._queue.pop_one(timeout_s=0.1)
    hook_item = hotpath._pop_hook_review_one()
    ok = item is None and hook_item is None
    _record("HP.3", "маршрут допускается к фильтру ТОЛЬКО после инициализации всех его пулов "
                     "(ещё не инициализирован -- тихо не подан никуда)",
            ok, f"main={item}, hook_review={hook_item}")


def check4_unconfirmed_hook_goes_to_review_queue_not_main(tmp_path):
    hotpath, registry, _ = _make_hotpath(tmp_path)
    unconfirmed_hook = "0x1e29254ad5a9108dbf77411bdeb76a3aed7ca8c0"  # реальный адрес из реестра, НЕ в CONFIRMED_HOOK_MODELS
    key_a = rr.PoolKey(rr.USDG, rr.MOSIAI, 3000, 60, unconfirmed_hook)
    pid = rr.pool_id(key_a)
    route = rr.RouteCycle("route_hook_unconf", (rr.RouteLeg(key_a.currency0, key_a.currency1, key_a.fee,
                                                             key_a.tick_spacing, key_a.hooks, True),),
                          rr.USDG, "hook-unconf-route", "discovered")
    registry.add_route(route)
    registry.set_liveness(route.route_id, {"live": True})
    hotpath._pool_cache.apply_swap_batch([{
        "pool_id": pid, "block_number": 100, "log_index": 0,
        "sqrt_price_x96": int((2.0 ** 0.5) * (2 ** 96)), "liquidity": 10 ** 20, "tick": 1000, "fee": 3000,
    }])
    hotpath._admit_touched_route(route, 100, time.monotonic())
    main_item = hotpath._queue.pop_one(timeout_s=0.1)
    hook_item = hotpath._pop_hook_review_one()
    ok = main_item is None and hook_item is not None and hook_item[0] == route.route_id
    _record("HP.4", "хук без подтверждённой модели -- В ОГРАНИЧЕННУЮ очередь пересмотра, НЕ в главную",
            ok, f"main={main_item}, hook_review={hook_item}")


def check5_not_live_route_goes_to_bounded_queue_not_main(tmp_path):
    hotpath, registry, _ = _make_hotpath(tmp_path)
    key_a = rr.PoolKey(rr.USDG, rr.MOSIAI, 3000, 60, rr.NATIVE)
    route = rr.RouteCycle("route_notlive", (rr.RouteLeg(key_a.currency0, key_a.currency1, key_a.fee,
                                                         key_a.tick_spacing, key_a.hooks, True),),
                          rr.USDG, "notlive-route", "discovered")
    registry.add_route(route)
    registry.set_liveness(route.route_id, {"live": False})  # явно НЕ живой (по умолчанию is_live()==True!)
    hotpath._admit_touched_route(route, 100, time.monotonic())
    main_item = hotpath._queue.pop_one(timeout_s=0.1)
    liveness_item = hotpath._pop_liveness_recheck_one()
    ok = main_item is None and liveness_item is not None and liveness_item[0] == route.route_id
    _record("HP.5", "не-живой маршрут НЕ занимает главную очередь -- уходит в ограниченную очередь "
                     "пересмотра живучести",
            ok, f"main={main_item}, liveness_recheck={liveness_item}")


def check5b_route_returns_to_normal_admission_after_liveness_restored(tmp_path):
    hotpath, registry, _ = _make_hotpath(tmp_path)
    key_a = rr.PoolKey(rr.USDG, rr.MOSIAI, 3000, 60, rr.NATIVE)
    pid = rr.pool_id(key_a)
    route = rr.RouteCycle("route_recovers", (rr.RouteLeg(key_a.currency0, key_a.currency1, key_a.fee,
                                                          key_a.tick_spacing, key_a.hooks, True),),
                          rr.USDG, "recovers-route", "discovered")
    registry.add_route(route)
    registry.set_liveness(route.route_id, {"live": False})
    hotpath._admit_touched_route(route, 100, time.monotonic())
    still_not_live_main = hotpath._queue.pop_one(timeout_s=0.1)
    hotpath._pop_liveness_recheck_one()  # осушаем очередь пересмотра, как это сделал бы evaluator_loop

    # Живучесть "восстановилась" (как это реально делает evaluator_loop после check_route_liveness)
    registry.set_liveness(route.route_id, {"live": True})
    sqrt_price = int((2.0 ** 0.5) * (2 ** 96))
    hotpath._pool_cache.apply_swap_batch([{
        "pool_id": pid, "block_number": 101, "log_index": 0,
        "sqrt_price_x96": sqrt_price, "liquidity": 10 ** 20, "tick": 1000, "fee": 3000,
    }])
    hotpath._admit_touched_route(route, 101, time.monotonic())
    main_item_after = hotpath._queue.pop_one(timeout_s=0.1)
    liveness_item_after = hotpath._pop_liveness_recheck_one()
    ok = (still_not_live_main is None and main_item_after is not None
          and main_item_after[0] == route.route_id and liveness_item_after is None)
    _record("HP.5b", "после восстановления живучести следующее касание идёт через обычный дешёвый "
                      "фильтр (в главную очередь), не в очередь пересмотра",
            ok, f"main_before={still_not_live_main}, main_after={main_item_after}, "
                f"liveness_after={liveness_item_after}")


def check6_confirmed_hook_model_matches_registry_key(tmp_path):
    # Сверяем, что ДВА подтверждённых ключа (ETH/MOSIAI 2.00% + ETH/0x35a79120 2.90%,
    # оба хук 0xe5e70264..., РАЗНЫЕ пул/направление/ставка) реально в словаре.
    key1 = ("0xf6562daa10e734d41846562b5f418f7833849643bf6c937eeeb0147b8ea94c2f", True)
    key2 = ("0xfaf0d4093602eb2d7f80ce7ba50cffaeece9c5d546b6df363107779e5ff553aa", False)
    ok = (key1 in CONFIRMED_HOOK_MODELS and CONFIRMED_HOOK_MODELS[key1]["post_swap_output_skim_fraction"] == 0.02
          and key2 in CONFIRMED_HOOK_MODELS and CONFIRMED_HOOK_MODELS[key2]["post_swap_output_skim_fraction"] == 0.029)
    _record("HP.6", "две подтверждённые модели хука 0xe5e70264...: ETH/MOSIAI 2.00%, ETH/0x35a79120 2.90%",
            ok, f"CONFIRMED_HOOK_MODELS keys={list(CONFIRMED_HOOK_MODELS.keys())}")


def main():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for i, fn in enumerate([
            check1_hookless_favorable_admitted, check2_hookless_unfavorable_dropped_silently,
            check3_not_yet_initialized_dropped_silently_not_queued,
            check4_unconfirmed_hook_goes_to_review_queue_not_main,
            check5_not_live_route_goes_to_bounded_queue_not_main,
            check5b_route_returns_to_normal_admission_after_liveness_restored,
        ]):
            sub = Path(tmp) / f"case{i}"
            sub.mkdir(parents=True, exist_ok=True)
            fn(sub)
    check6_confirmed_hook_model_matches_registry_key(None)

    n_ok = sum(1 for r in RESULTS if r["ok"])
    print(f"\n=== ИТОГ: {n_ok}/{len(RESULTS)} проверок пройдено ===")
    import json
    out = Path(__file__).parent.parent / "data" / "task5_v4_hook_prefilter_pointchecks_result.json"
    out.write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False))
    print(f"результат записан в {out}")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
