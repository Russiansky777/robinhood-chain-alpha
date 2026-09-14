#!/usr/bin/env python3
"""Задача 5, контрольный пример конкурента, сессия run34788791689_1
("окно последнего пилота"). Второй шаг после определения границ окна
(task5_v4_pilot2b_block_range.py: блоки 62334833..62346576) и точечного
скана Swap-логов конкурента в этом окне (task5_v4_competitor_trade_scan.py,
уже отдельно запущен и сохранён в
/home/bot/robinhood-chain-alpha/data/task5_v4_competitor_trade_scan_result.json:
79 Swap-логов, 32 многоходовые (>=2 плеч) транзакции).

Этот скрипт (read-only, точечные запросы к уже найденным кандидатам --
НЕ новый широкий скан):
  1) читает уже сохранённый результат скана;
  2) группирует 32 кандидата по НАБОРУ pool_id (frozenset) -- находит,
     где конкурент ПОВТОРЯЛ один и тот же маршрут (владелец: "по
     возможности выбери маршрут с повторными сделками");
  3) для каждого пула в топ-комбинациях -- Initialize-событие (currency0/
     currency1/fee/tick_spacing/hooks), через уже существующий кэширующий
     `_cached_fetch_initialize` (task5_v4_item3_in_window_control_trade.py);
  4) строит RouteLeg (zero_for_one = amount0 > 0 пула -- ТА ЖЕ формула,
     что _build_route_from_fund_flow_legs в item3-скрипте) и route_id
     (_route_id_for_legs, task5_v4_route_registry.py -- ДЕТЕРМИНИРОВАННО,
     та же функция, что использует сам реестр);
  5) проверяет "маршрут поддерживается нашим исполнителем" (task5_v4_
     pool_state_cache.CONFIRMED_HOOK_MODELS + hookless, ТА ЖЕ проверка,
     что cheap_filter_route) для ВСЕХ комбинаций, не только для топ-1;
  6) для представителя каждой поддерживаемой комбинации -- ПОЛНАЯ
     проверка движения средств (full_fund_flow_check, item3-скрипт) --
     Swap-лог сам по себе НЕ доказывает прибыль трейдера;
  7) для маршрутов, прошедших (5) И (6) -- проверка присутствия route_id
     в ФИНАЛЬНОМ снимке реестра (load_registry_state) -- необходимое, но
     НЕ достаточное условие "был в реестре ДО события" (нет истории
     добавления, см. докстринг ниже)."""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import os  # noqa: E402
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from task5_v4_item3_in_window_control_trade import (  # noqa: E402
    _cached_fetch_initialize, full_fund_flow_check,
)
from task5_v4_route_registry import RouteLeg, _route_id_for_legs, load_registry_state  # noqa: E402
from task5_v4_pool_state_cache import CONFIRMED_HOOK_MODELS  # noqa: E402

NATIVE_HOOKS = "0x0000000000000000000000000000000000000000"
SCAN_RESULT_PATH = Path("/home/bot/robinhood-chain-alpha/data/task5_v4_competitor_trade_scan_result.json")
REGISTRY_STATE_PATH = "/home/bot/data/task5_v4_route_registry_state.json"


def leg_supported(pool_id_hex: str, zero_for_one: bool, hooks: str) -> tuple[bool, str]:
    if hooks.lower() == NATIVE_HOOKS:
        return True, "hookless"
    model = CONFIRMED_HOOK_MODELS.get((pool_id_hex.lower(), zero_for_one))
    if model is not None:
        return True, f"confirmed_hook_model({model.get('post_swap_output_skim_fraction')})"
    return False, "хук без подтверждённой модели именно для этого пула/направления"


def main() -> None:
    data = json.loads(SCAN_RESULT_PATH.read_text())
    full = data["multi_leg_candidates_full"]
    print(f"[candidate_select] окно {data['from_block']}..{data['to_block']}, "
          f"{data['n_swap_logs']} Swap-логов, {len(full)} многоходовых кандидатов")

    combo_txs: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for c in full:
        pids = tuple(sorted(leg["pool_id"] for leg in c["legs"]))
        combo_txs[pids].append(c)

    combos_by_repeat = sorted(combo_txs.items(), key=lambda kv: -len(kv[1]))
    print(f"[candidate_select] {len(combos_by_repeat)} уникальных наборов пулов; "
          f"повторов (по числу tx на набор): {[len(v) for _, v in combos_by_repeat]}")

    report: dict = {"window": {"from_block": data["from_block"], "to_block": data["to_block"]},
                     "n_unique_pool_combos": len(combos_by_repeat), "combos": []}

    for pids, txs in combos_by_repeat:
        combo_entry: dict = {"pool_ids": list(pids), "n_repeat_txs": len(txs),
                              "tx_hashes_and_blocks": [(t["tx_hash"], t["block"]) for t in txs]}
        # Initialize для каждого пула -- ЗАРАНЕЕ (кэшируется по pool_id, см. _cached_fetch_initialize)
        inits = {}
        init_ok = True
        for pid in pids:
            init = _cached_fetch_initialize(pid, data["to_block"])
            if init is None:
                init_ok = False
                combo_entry["error"] = f"Initialize не найден для пула {pid} -- маршрут не построен"
                break
            inits[pid] = init
        if not init_ok:
            report["combos"].append(combo_entry)
            continue

        # Направление -- по ПЕРВОЙ (репрезентативной) tx набора (тот же
        # набор пулов в цикле почти всегда торгуется в одном и том же
        # направлении конкурентом -- проверяем явно ниже, честно).
        rep = txs[0]
        legs_by_pool = {leg["pool_id"]: leg for leg in rep["legs"]}
        route_legs = []
        leg_supports = []
        for pid in pids:
            leg = legs_by_pool[pid]
            zero_for_one = leg["amount0"] > 0
            init = inits[pid]
            supported, why = leg_supported(pid, zero_for_one, init["hooks"])
            leg_supports.append({"pool_id": pid, "zero_for_one": zero_for_one, "hooks": init["hooks"],
                                  "currency0": init["currency0"], "currency1": init["currency1"],
                                  "fee": init["fee"], "supported": supported, "why": why})
            route_legs.append(RouteLeg(init["currency0"], init["currency1"], init["fee"],
                                        init["tick_spacing"], init["hooks"], zero_for_one))
        combo_entry["leg_supports"] = leg_supports
        all_supported = all(l["supported"] for l in leg_supports)
        combo_entry["all_legs_supported_by_our_executor"] = all_supported

        # Порядок плеч в цикле -- в порядке logIndex представительной tx
        # (это и есть реальный исполненный порядок конкурента); route_id
        # строится ИМЕННО из этого порядка (та же функция, что реестр).
        route_id = _route_id_for_legs(route_legs)
        combo_entry["route_id_candidate"] = route_id
        combo_entry["exit_token_start"] = route_legs[0].input_currency
        combo_entry["exit_token_end"] = route_legs[-1].output_currency
        combo_entry["is_closed_leg_sequence"] = (
            route_legs[0].input_currency.lower() == route_legs[-1].output_currency.lower()
        )

        if all_supported:
            # ПОЛНАЯ проверка движения средств -- ДЛЯ ВСЕХ повторов этого
            # набора пулов (не только представителя) -- честно, сколько
            # из них реально замкнутый прибыльный цикл.
            fund_flow_results = []
            for t in txs:
                ff = full_fund_flow_check(t["tx_hash"])
                fund_flow_results.append({
                    "tx_hash": t["tx_hash"], "block": t["block"],
                    "fully_valid_closed_cycle": ff.get("fully_valid_closed_cycle"),
                    "base_token": ff.get("base_token"),
                    "profit_raw": ff.get("nonzero_net_flow_tokens", {}).get(ff.get("base_token"))
                                   if ff.get("base_token") else None,
                    "verdict": ff.get("verdict"),
                })
            combo_entry["fund_flow_checks"] = fund_flow_results
            combo_entry["n_confirmed_profitable"] = sum(
                1 for r in fund_flow_results if r["fully_valid_closed_cycle"])

        report["combos"].append(combo_entry)

    # Реестр -- ФИНАЛЬНЫЙ снимок (после сессии) -- необходимое, НЕ
    # достаточное условие "был в реестре ДО события" (честно, нет истории
    # добавления в сохранённых данных).
    resumed = load_registry_state(REGISTRY_STATE_PATH)
    if resumed is not None:
        registry, cursor_block = resumed
        report["final_registry_snapshot"] = {
            "state_path": REGISTRY_STATE_PATH, "cursor_block": cursor_block,
            "n_routes_in_final_snapshot": len(registry.routes),
        }
        for combo_entry in report["combos"]:
            rid = combo_entry.get("route_id_candidate")
            if rid:
                combo_entry["route_id_present_in_final_registry_snapshot"] = rid in registry.routes
    else:
        report["final_registry_snapshot"] = {"error": f"не удалось загрузить {REGISTRY_STATE_PATH}"}

    print(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    out_path = Path("/home/bot/robinhood-chain-alpha/data/task5_v4_pilot2b_candidate_select_result.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    print(f"[candidate_select] сохранено: {out_path}")


if __name__ == "__main__":
    main()
