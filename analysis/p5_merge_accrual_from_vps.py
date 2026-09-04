#!/usr/bin/env python3
"""Слияние накопленного на NL VPS ряда `data/p5_fee_accrual.jsonl` (сбор
идёт автономным cron'ом на самом хосте, см. docs/HANDOFF.md, 2026-09-04)
в git-версию файла.

НЕ генерирует новых строк и НЕ запускает p5_live_position_snapshot.py --
только объединяет то, что реально уже накопил cron на VPS, с тем, что
уже закоммичено в git, дедуп по `timestamp_utc` (ключ реального момента
снятия), сортировка по времени. При совпадении timestamp содержимое с
VPS считается источником истины (git-версия могла быть снята другим
путём раньше). Забирает ВСЕ строки, накопленные с прошлого визита, а не
только последнюю.

Использование: python p5_merge_accrual_from_vps.py <путь к файлу, забранному с VPS>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ACCRUAL_LOG_PATH = Path("data/p5_fee_accrual.jsonl")


def load_rows(path: Path) -> dict:
    if not path.exists():
        return {}
    rows: dict = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        rows[row["timestamp_utc"]] = row
    return rows


def run(vps_file: str) -> int:
    existing = load_rows(ACCRUAL_LOG_PATH)
    fetched = load_rows(Path(vps_file))

    if not fetched:
        print("[merge] с VPS пришёл пустой/нечитаемый файл -- ничего не меняю, не коммичу.")
        return 1

    before = len(existing)
    merged = {**existing, **fetched}  # на пересечении ключей -- версия с VPS
    rows_sorted = sorted(merged.values(), key=lambda r: r["timestamp_utc"])

    ACCRUAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACCRUAL_LOG_PATH.write_text(
        "\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows_sorted) + "\n"
    )

    print(f"[merge] было в git: {before}, забрано с VPS: {len(fetched)}, итог: {len(rows_sorted)} "
          f"(новых добавлено: {len(rows_sorted) - before}).")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: p5_merge_accrual_from_vps.py <vps_accrual_file>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run(sys.argv[1]))
