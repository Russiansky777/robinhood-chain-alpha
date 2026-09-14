#!/usr/bin/env python3
"""Печатает КОМПАКТНУЮ сводку из уже сохранённого result_*.json
(task5_v4_speed_benchmark.py) -- БЕЗ сети, БЕЗ повторного запуска
замера. Нужен отдельно, т.к. полный JSON с raw-трейсами слишком велик
для лога GitHub Actions (tail_lines обрезает до печати сводки)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

OUT_DIR_DEFAULT = Path(__file__).parent.parent / "data" / "task5_v4_speed_benchmark"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result-file", type=str, default=None, help="Если не указан -- берётся самый свежий result_*.json")
    ap.add_argument("--out-dir", type=str, default=str(OUT_DIR_DEFAULT))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    if args.result_file:
        path = Path(args.result_file)
    else:
        candidates = sorted(out_dir.glob("result_*.json"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            print(json.dumps({"error": f"нет result_*.json в {out_dir}"}))
            return
        path = candidates[-1]

    report = json.loads(path.read_text())
    summary = {
        "source_file": str(path),
        "test_account_address": report.get("test_account_address"),
        "repeats": report.get("repeats"),
        "variants": report.get("variants"),
        "routes": report.get("routes"),
        "parity_check_all_match_baseline": report.get("parity_check", {}).get("all_match_baseline"),
        "parity_check_per_variant_match": report.get("parity_check", {}).get("per_variant_match"),
    }

    direct = report.get("direct_probes", {})
    summary["direct_stats_by_variant"] = {v: direct[v]["stats"] for v in direct}

    full = report.get("full_attempt_probes", {})
    summary["full_attempt_stats_by_variant"] = {}
    for v in full:
        block = full[v]
        summary["full_attempt_stats_by_variant"][v] = {
            "n_reached_ready_to_send": block.get("n_reached_ready_to_send"),
            "n_returned_without_send": block.get("n_returned_without_send"),
            "n_exception": block.get("n_exception"),
            "stats": block.get("stats"),
            "stop_reasons_sample": [r.get("stop_reason") for r in block.get("raw", [])[:3]],
        }

    summary["idealized_limits_baseline"] = report.get("idealized_limits_baseline")
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
