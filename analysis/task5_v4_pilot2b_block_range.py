#!/usr/bin/env python3
"""Задача 5, контрольный пример конкурента, сессия run34788791689_1
("окно последнего пилота" по постановке владельца, pilot_started_at=
2026-09-13T23:08:00.864Z, ~1200.47с). Первый обязательный шаг перед
любым точечным запросом к цепи (по инструкции владельца): точные
границы блоков ЭТОЙ сессии -- НЕ по среднему блок-тайму, а напрямую из
уже сохранённого полного журнала

/home/bot/robinhood-chain-alpha/data/task5_v4_pilot_session2b_only_no_send_log.jsonl

(238182 строк). Берём min/max по `signal_block` для ВСЕХ строк, где
оно есть; если строка `signal_block` не содержит -- используем
`quote_block`, если он есть (обе величины -- блоки, на которых бот
реально смотрел на цепь в рамках этой сессии, так что их объединённый
диапазон -- честная верхняя/нижняя граница окна активности сессии).
Read-only, только чтение уже существующего файла на этой же машине
(Ohio) -- НИКАКИХ новых RPC-запросов в этом скрипте."""
from __future__ import annotations

import json
from pathlib import Path

LOG_PATH = Path("/home/bot/robinhood-chain-alpha/data/task5_v4_pilot_session2b_only_no_send_log.jsonl")


def main() -> None:
    n_lines = 0
    n_bad_json = 0
    n_with_signal_block = 0
    n_with_quote_block_only = 0
    n_without_any_block = 0
    min_block = None
    max_block = None
    min_signal_block = None
    max_signal_block = None
    min_quote_block = None
    max_quote_block = None
    reason_counts: dict[str, int] = {}

    with LOG_PATH.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            n_lines += 1
            try:
                rec = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                n_bad_json += 1
                continue

            sb = rec.get("signal_block")
            qb = rec.get("quote_block")
            reason = rec.get("reason")
            if reason is not None:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

            if isinstance(qb, (int, float)):
                qb = int(qb)
                min_quote_block = qb if min_quote_block is None else min(min_quote_block, qb)
                max_quote_block = qb if max_quote_block is None else max(max_quote_block, qb)

            if isinstance(sb, (int, float)):
                sb = int(sb)
                n_with_signal_block += 1
                min_signal_block = sb if min_signal_block is None else min(min_signal_block, sb)
                max_signal_block = sb if max_signal_block is None else max(max_signal_block, sb)
                used_block = sb
            elif isinstance(qb, (int, float)):
                n_with_quote_block_only += 1
                used_block = qb
            else:
                n_without_any_block += 1
                used_block = None

            if used_block is not None:
                min_block = used_block if min_block is None else min(min_block, used_block)
                max_block = used_block if max_block is None else max(max_block, used_block)

    result = {
        "log_path": str(LOG_PATH),
        "n_lines": n_lines,
        "n_bad_json": n_bad_json,
        "n_with_signal_block": n_with_signal_block,
        "n_with_quote_block_only_fallback": n_with_quote_block_only,
        "n_without_any_block": n_without_any_block,
        "min_signal_block": min_signal_block,
        "max_signal_block": max_signal_block,
        "min_quote_block": min_quote_block,
        "max_quote_block": max_quote_block,
        "combined_min_block": min_block,
        "combined_max_block": max_block,
        "combined_block_span": (max_block - min_block) if (min_block is not None and max_block is not None) else None,
        "reason_counts_top20": dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])[:20]),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))

    out_path = Path("/home/bot/robinhood-chain-alpha/data/task5_v4_pilot2b_block_range_result.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"[block_range] сохранено: {out_path}")


if __name__ == "__main__":
    main()
