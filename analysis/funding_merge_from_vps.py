#!/usr/bin/env python3
"""Слияние накопленного на NL VPS ряда `data/funding_spread.jsonl` (сбор
идёт автономным cron'ом на самом хосте, минута :21, см. docs/HANDOFF.md)
в git-версию файла. Тот же паттерн, что analysis/p5_merge_accrual_from_vps.py,
но ключ дедупа -- (timestamp_utc, symbol), не просто timestamp_utc: каждый
цикл сборщика пишет 13 строк (по числу пар) с ОДНИМ и тем же timestamp_utc.

НЕ генерирует новых строк и НЕ запускает funding_spread_hourly_snapshot.py --
только объединяет то, что реально уже накопил cron на VPS.

Использование: python funding_merge_from_vps.py <путь к файлу, забранному с VPS>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SPREAD_LOG_PATH = Path("data/funding_spread.jsonl")


def load_rows(path: Path) -> dict:
    if not path.exists():
        return {}
    rows: dict = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        key = (row["timestamp_utc"], row["symbol"])
        rows[key] = row
    return rows


def run(vps_file: str) -> int:
    existing = load_rows(SPREAD_LOG_PATH)
    fetched = load_rows(Path(vps_file))

    if not fetched:
        print("[merge] с VPS пришёл пустой/нечитаемый файл -- ничего не меняю, не коммичу.")
        return 1

    before = len(existing)
    merged = {**existing, **fetched}  # на пересечении ключей -- версия с VPS
    rows_sorted = sorted(merged.values(), key=lambda r: (r["timestamp_utc"], r["symbol"]))

    SPREAD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPREAD_LOG_PATH.write_text(
        "\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows_sorted) + "\n"
    )

    n_cycles = len({r["timestamp_utc"] for r in rows_sorted})
    print(f"[merge] было в git: {before}, забрано с VPS: {len(fetched)}, итог: {len(rows_sorted)} строк "
          f"(новых добавлено: {len(rows_sorted) - before}), уникальных циклов сбора: {n_cycles}.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: funding_merge_from_vps.py <vps_spread_file>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run(sys.argv[1]))
