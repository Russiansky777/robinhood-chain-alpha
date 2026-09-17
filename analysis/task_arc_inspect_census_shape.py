#!/usr/bin/env python3
"""Разовый инспектор: реальная схема task_arc_lp_census_from_local_files_result.json
на VPS -- Шаг 0 предыдущего прогона не нашёл ожидаемых полей, нужна
РЕАЛЬНАЯ форма файла, не гадать снова."""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def describe(obj, prefix="", depth=0, max_depth=3):
    if depth > max_depth:
        return
    if isinstance(obj, dict):
        print(f"{prefix}dict keys: {list(obj.keys())}")
        for k, v in list(obj.items())[:5]:
            describe(v, prefix + f"  [{k}] ", depth + 1, max_depth)
    elif isinstance(obj, list):
        print(f"{prefix}list len={len(obj)}")
        if obj:
            describe(obj[0], prefix + "  [0] ", depth + 1, max_depth)
    else:
        print(f"{prefix}{type(obj).__name__}: {str(obj)[:80]}")


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    p = data_dir.joinpath("task_arc_lp_census_from_local_files_result.json")
    obj = json.loads(p.read_text())
    for key in ("top_hooks_in_sample", "n_unique_hooks_in_sample", "top3_hooks_share_of_sample", "profit_receiver_hook_in_sample"):
        print(f"=== {key} ===")
        print(json.dumps(obj.get(key), indent=2, ensure_ascii=False, default=str)[:3000])
        print()


if __name__ == "__main__":
    main()
