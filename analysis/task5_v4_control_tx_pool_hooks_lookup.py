#!/usr/bin/env python3
"""Задача 5, предфильтр -- пункт 5 (продолжение): для двух пулов
контрольного примера tx 0x878fb998... (0x2e61865f..., 0xfaf0d409...),
чьи hooks НЕ входят в уже сохранённый task5_v4_hook_route_audit_result.json
(тот аудит -- про ДРУГОЙ маршрут ETH/MOSIAI/USDG/MOSIAI/ETH/USDG),
ищем PoolKey.hooks в УЖЕ загруженном (не пересозданном) реестре
маршрутов -- registry.known_pools заполняется при обычном discover
(Initialize-события), значит, если эти пулы вообще в реестре, hooks
уже там. НЕ делает нового RPC/reconstruction, если пулы уже известны;
только если совсем не найдены -- честно говорит об этом, RPC не
запускает (решение об этом -- отдельно, не в этом скрипте)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from task5_v4_route_registry import load_registry_state

REGISTRY_STATE_FILE = os.environ.get(
    "ROUTE_REGISTRY_STATE_FILE", "/home/bot/data/task5_v4_route_registry_state.json")

TARGET_POOL_IDS = [
    "0x24107d152f14a76d292123265ae3f3c71f863fc2f4ef7ba49d64e78d28ea379e",  # ETH/USDG, уже известен hookless
    "0x2e61865fcb733f98d816beb7c05ca692570b91aca4bdb9a85269bd02c2e2b09b",  # ? hooks
    "0xfaf0d4093602eb2d7f80ce7ba50cffaeece9c5d546b6df363107779e5ff553aa",  # ? hooks
]
NATIVE_HOOKS = "0x0000000000000000000000000000000000000000"


def main() -> None:
    result: dict = {}
    loaded = load_registry_state(REGISTRY_STATE_FILE)
    if loaded is None:
        result["error"] = f"{REGISTRY_STATE_FILE} не найден/не читается"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    registry, cursor_block = loaded
    result["cursor_block"] = cursor_block

    found = {}
    for pid in TARGET_POOL_IDS:
        key = registry.known_pools.get(pid)
        if key is None:
            # регистр индексируется точным регистром hex-строки -- пробуем case-insensitive на всякий случай
            match = next((k for p, k in registry.known_pools.items() if p.lower() == pid.lower()), None)
            key = match
        if key is None:
            found[pid] = {"in_known_pools": False}
        else:
            found[pid] = {
                "in_known_pools": True,
                "currency0": key.currency0, "currency1": key.currency1,
                "fee": key.fee, "tick_spacing": key.tick_spacing, "hooks": key.hooks,
                "is_hookless": key.hooks.lower() == NATIVE_HOOKS,
            }
    result["pool_hooks_lookup"] = found

    # Заодно: есть ли уже готовый RouteCycle с ровно этими тремя пулами (в любом порядке) в реестре.
    target_set = set(p.lower() for p in TARGET_POOL_IDS)
    matching_routes = []
    with registry._lock:
        for rid, route in registry.routes.items():
            if set(pid.lower() for pid in route.pool_ids()) == target_set:
                matching_routes.append({
                    "route_id": rid, "label": route.label, "source": route.source,
                    "is_live": registry.liveness.get(rid, {}).get("live"),
                    "legs_hooks": [leg.hooks for leg in route.legs],
                })
    result["matching_route_in_registry"] = matching_routes if matching_routes else \
        {"found": False, "note": "точное совпадение по набору из этих 3 pool_id в реестре не найдено"}

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_control_tx_pool_hooks_lookup_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
