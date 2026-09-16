#!/usr/bin/env python3
"""Проверка перед тем, как доверять числам из предыдущего раунда: 59 из
70 пулов, найденных generic-обходом в task_arc_closed_cycles_result.json,
показали hooks=address(0) -- подозрительно много. Возможно, обход
спутал разные типы объектов (например, "route leg" с полем hooks,
скопированным из кэша метаданных, но БЕЗ fee в том же объекте, и
отдельно "pool meta" с fee, но без hooks). Прежде чем сообщать числа --
посмотреть на РЕАЛЬНУЮ структуру файла, без предположений."""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT_CANDIDATES = [
    Path("/home/bot/robinhood-chain-alpha"),
    Path(__file__).parent.parent,
]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def main() -> None:
    root = find_repo_root()
    path = root.joinpath("data", "task_arc_closed_cycles_result.json")
    d = json.loads(path.read_text())

    result: dict = {"top_level_keys": list(d.keys())}

    windows = d.get("windows")
    if isinstance(windows, dict):
        result["windows_type"] = "dict"
        result["windows_keys"] = list(windows.keys())
        first_window_key = next(iter(windows), None)
        result["first_window_key"] = first_window_key
        if first_window_key:
            fw = windows[first_window_key]
            result["first_window_top_keys"] = list(fw.keys()) if isinstance(fw, dict) else str(type(fw))
    elif isinstance(windows, list):
        result["windows_type"] = "list"
        result["windows_len"] = len(windows)
        if windows:
            result["first_window_top_keys"] = list(windows[0].keys()) if isinstance(windows[0], dict) else str(type(windows[0]))

    # Найти любой объект с "pool_id" и вывести ПОЛНОСТЬЮ первые 5 разных форм
    samples = []
    seen_key_signatures = set()

    def walk(obj, path_str=""):
        if len(samples) >= 8:
            return
        if isinstance(obj, dict):
            if "pool_id" in obj:
                sig = tuple(sorted(obj.keys()))
                if sig not in seen_key_signatures:
                    seen_key_signatures.add(sig)
                    samples.append({"json_path": path_str, "keys": list(obj.keys()), "sample": obj})
            for k, v in obj.items():
                walk(v, f"{path_str}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj[:3]):  # не более 3 элементов на список, чтобы не взорваться
                walk(v, f"{path_str}[{i}]")

    walk(d)
    result["distinct_pool_id_object_shapes"] = samples

    # Также: pool_map_seed -- отдельная структура, может содержать
    # canonical currency0/1/fee/hooks на pool_id (это "seed"-кэш метаданных).
    pms = d.get("pool_map_seed")
    if isinstance(pms, dict):
        result["pool_map_seed_top_keys"] = list(pms.keys())
        for k in ("pools", "map", "data"):
            if k in pms and isinstance(pms[k], (list, dict)):
                sample_container = pms[k]
                if isinstance(sample_container, list) and sample_container:
                    result[f"pool_map_seed.{k}_sample"] = sample_container[0]
                elif isinstance(sample_container, dict):
                    first_k = next(iter(sample_container), None)
                    if first_k:
                        result[f"pool_map_seed.{k}_sample"] = {first_k: sample_container[first_k]}

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    root.joinpath("data", "task_arc_lp_census_inspect_cycles_schema_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
