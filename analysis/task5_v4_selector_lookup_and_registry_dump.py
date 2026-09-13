#!/usr/bin/env python3
"""Задача 5, живой пилот, шестой раунд -- два маленьких дополнительных
чтения (дёшево, чтобы не гонять заново весь task5_v4_control_and_revert_
deep_dive.py, чей вывод частично обрезался в логе CI на части 2):

1. Публичные базы сигнатур (4byte.directory, openchain.xyz) для
   селектора 0x356680b7 -- он НЕ совпал ни с одной из ~32 проверенных
   локально сигнатур v4-core/нашего контракта (см. коммит с этим
   расследованием); эти запросы идут НЕ к RPC цепи, а к обычным
   публичным HTTP API -- честная попытка, реальный ответ, не гадаем.
2. КОМПАКТНЫЙ (без indent, чтобы не обрезался построчный лимит лога CI)
   дамп registry.routes[...] для ОСТАВШИХСЯ route_id, не попавших в
   предыдущий (обрезанный) вывод."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import requests  # noqa: E402
from task5_v4_route_registry import load_registry_state  # noqa: E402

SELECTOR = "0x356680b7"
REGISTRY_STATE_FILE = "/home/bot/data/task5_v4_route_registry_state.json"
REMAINING_ROUTE_IDS = [
    "route_e0ed68abz0_c009a2dbz1",
    "route_32306acaz0_f1897bd1z0_659e64f0z1",
    "route_06b97324z0_f4527156z1_aa94ace2z0",
    "route_06b97324z0_30daa45fz1_7caf58c3z0",
    "route_44e106bbz0_0c61eea1z0_24107d15z1",
    "route_778a632fz1_f6064451z0_06b97324z1",
    "route_6eb75d4ez1_f6064451z0_06b97324z1",
]


def main() -> None:
    result = {}
    try:
        r = requests.get("https://www.4byte.directory/api/v1/signatures/",
                          params={"hex_signature": SELECTOR}, timeout=20)
        result["4byte"] = {"status": r.status_code, "body": r.json() if r.ok else r.text[:500]}
    except Exception as exc:  # noqa: BLE001
        result["4byte"] = {"error": str(exc)}
    try:
        r = requests.get("https://api.openchain.xyz/signature-database/v1/lookup",
                          params={"function": SELECTOR, "filter": "true"}, timeout=20)
        result["openchain"] = {"status": r.status_code, "body": r.json() if r.ok else r.text[:500]}
    except Exception as exc:  # noqa: BLE001
        result["openchain"] = {"error": str(exc)}

    loaded = load_registry_state(REGISTRY_STATE_FILE)
    routes_found = {}
    if loaded is not None:
        registry, _cursor = loaded
        for rid in REMAINING_ROUTE_IDS:
            route = registry.routes.get(rid)
            if route is None:
                routes_found[rid] = None
                continue
            routes_found[rid] = {
                "exit_token": route.exit_token, "label": route.label, "source": route.source,
                "legs": [{"currency0": leg.currency0, "currency1": leg.currency1, "fee": leg.fee,
                           "tick_spacing": leg.tick_spacing, "hooks": leg.hooks,
                           "zero_for_one": leg.zero_for_one, "pool_id": leg.pool_id_hex}
                          for leg in route.legs],
            }
    else:
        routes_found = {"error": "registry не загрузился"}
    result["routes_found_remaining"] = routes_found

    # КОМПАКТНО (separators без пробелов, БЕЗ indent) -- построчный
    # лимит вывода CI обрезал предыдущий indent=2 дамп на части 2.
    print(json.dumps(result, default=str, ensure_ascii=False, separators=(",", ":")))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_selector_lookup_and_registry_dump_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
