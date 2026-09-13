#!/usr/bin/env python3
"""Задача 5, предфильтр (уточнение владельца перед реализацией),
пункты 1/2/4 -- ТОЛЬКО чтение, ничего не реконструирует и не шлёт:

1. По уже сохранённому реестру (`ROUTE_REGISTRY_STATE_FILE`, тот же
   файл, что читает боевой пилот) считает маршруты без хуков и с
   хуками; хук-маршруты группирует по МНОЖЕСТВУ различных адресов
   хуков, использованных в маршруте (не по одному хуку "маршрута"),
   чтобы явно не задваивать долю у маршрутов с несколькими разными
   хуками -- такой маршрут учитывается РОВНО ОДИН раз в "покрыто хотя
   бы одним из топ-N", даже если он попадает в несколько групп по
   отдельности.

2/4. Ищет уже СУЩЕСТВУЮЩИЕ файлы по контрольному примеру и разбору
   хука 0xe5e702641ea86f4ae6cc3cdaed2b886f976be044 в ../data/ (это
   рабочее дерево Ohio -- data/ НЕ трогается точечным checkout
   analysis/, значит файлы прошлых прогонов должны быть ЗДЕСЬ, если их
   не удаляли руками) -- НЕ создаёт новых реконструкций, только
   перечисляет и (если найдено) вытаскивает уже посчитанные поля."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_v4_route_registry import load_registry_state  # noqa: E402
from task5_v4_pool_math import PoolKey  # noqa: E402

NATIVE_HOOKS = "0x0000000000000000000000000000000000000000"
REGISTRY_STATE_FILE = os.environ.get(
    "ROUTE_REGISTRY_STATE_FILE", "/home/bot/data/task5_v4_route_registry_state.json")
DATA_DIR = Path(__file__).parent.parent / "data"

# Файлы, которые (по докстрингам соответствующих скриптов в analysis/)
# ДОЛЖНЫ были быть сохранены прошлыми прогонами -- ищем их здесь, а не
# гадаем/пересчитываем.
CANDIDATE_FILES = [
    "task5_v4_hook_route_audit_result.json",
    "task5_v4_item3_round10_reconstruct_result.json",
    "task5_v4_item3_round11_competitor_size_result.json",
    "task5_v4_item3_round12_gas_after_result.json",
    "task5_v4_item3_round12_grid_check_result.json",
    "task5_v4_item3_in_window_control_trade_result.json",
    "task5_v4_diag_current_liquidity_result.json",
    "task5_v4_hook_quote_check_result.json",
    "task5_v4_fork_simulation_hook_route_result.json",
]


def route_hook_set(route) -> frozenset[str]:
    return frozenset(leg.hooks.lower() for leg in route.legs if leg.hooks.lower() != NATIVE_HOOKS)


def main() -> None:
    result: dict = {}

    # --- пункт 1: покрытие по уже сохранённому реестру ---
    loaded = load_registry_state(REGISTRY_STATE_FILE)
    if loaded is None:
        result["registry_coverage"] = {"error": f"{REGISTRY_STATE_FILE} не найден/не читается -- "
                                                 "честно, без оценки на глаз"}
    else:
        registry, cursor_block = loaded
        with registry._lock:
            routes = list(registry.routes.values())
        n_total = len(routes)
        hookless = [r for r in routes if not route_hook_set(r)]
        hooked = [r for r in routes if route_hook_set(r)]

        from collections import Counter
        hook_addr_counts: Counter[str] = Counter()
        for r in hooked:
            for addr in route_hook_set(r):
                hook_addr_counts[addr] += 1  # маршрут с 2 разными хуками учтён в ОБЕИХ группах -- это группировка по хуку, не общая доля

        top5 = hook_addr_counts.most_common(5)
        result["registry_coverage"] = {
            "cursor_block": cursor_block,
            "n_routes_total": n_total,
            "n_routes_hookless_all_legs": len(hookless),
            "n_routes_with_at_least_one_hook": len(hooked),
            "n_distinct_hook_addresses_in_registry": len(hook_addr_counts),
            "top5_hook_addresses_by_n_routes_touching": [
                {"hook": addr, "n_routes_touching_this_hook": n,
                 "share_of_all_routes_pct": round(100.0 * n / n_total, 2) if n_total else None}
                for addr, n in top5
            ],
            "note_on_double_counting": (
                "top5 считает 'маршрут ЗАДЕЛ этот хук' -- один маршрут с 2 разными хуками попадёт "
                "в ОБЕ строки top5 (это group-by хуку, не партиция). n_routes_with_at_least_one_hook "
                "выше -- ЕДИНСТВЕННОЕ число, где такой маршрут учтён РОВНО один раз (множество route_id, "
                "не сумма по хукам)."
            ),
            "n_routes_with_multiple_distinct_hooks": sum(1 for r in hooked if len(route_hook_set(r)) > 1),
        }

    # --- пункты 2/4: что из старых результатов реально ещё на диске ---
    found = {}
    for fname in CANDIDATE_FILES:
        p = DATA_DIR / fname
        if not p.exists():
            found[fname] = {"exists": False}
            continue
        try:
            data = json.loads(p.read_text())
            found[fname] = {"exists": True, "size_bytes": p.stat().st_size,
                             "mtime": p.stat().st_mtime, "top_level_keys": list(data.keys())
                             if isinstance(data, dict) else f"(не dict, тип {type(data).__name__})"}
        except Exception as exc:  # noqa: BLE001
            found[fname] = {"exists": True, "size_bytes": p.stat().st_size, "read_error": str(exc)}
    result["existing_files_in_data_dir"] = found
    result["data_dir_listing_all"] = sorted(p.name for p in DATA_DIR.glob("*.json")) if DATA_DIR.exists() else \
        {"error": f"{DATA_DIR} не существует"}

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = DATA_DIR / "task5_v4_hook_coverage_and_control_recovery_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
