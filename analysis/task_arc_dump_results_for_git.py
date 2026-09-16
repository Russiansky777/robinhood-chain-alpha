#!/usr/bin/env python3
"""Локальные JSON-результаты предыдущих прогонов (extsload, bitquery oauth
probe, alt-rpc research) существуют только на VPS -- этот скрипт печатает
их содержимое с явными маркерами, чтобы забрать текст из лога job'а и
закоммитить в git (прямого доступа по SSH к VPS из среды сессии нет)."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
FILES = [
    "task_arc_lp_extsload_fee_check_result.json",
    "task_arc_bitquery_oauth_probe_result.json",
    "task_arc_alt_rpc_research_result.json",
]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def main() -> None:
    data_dir = find_repo_root().joinpath("data")
    for name in FILES:
        p = data_dir.joinpath(name)
        print(f"===BEGIN {name}===")
        if p.exists():
            print(p.read_text())
        else:
            print("===MISSING===")
        print(f"===END {name}===")


if __name__ == "__main__":
    main()
