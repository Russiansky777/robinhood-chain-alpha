#!/usr/bin/env python3
"""Владелец: 'новый скан не запускать'. Скан УЖЕ реально завершился
(elapsed_seconds~619с, задолго до собственных бюджетов) и напечатал
полный result JSON в stdout ДО того, как упал на реальном баге --
Path("data/...") в task5_true_arbitrageur_scan.py:332 -- относительный
путь, который резолвится от текущей рабочей директории (analysis/ в
процессе-лаунчере), а не от корня репозитория, поэтому файл не был
сохранён. Это ЧИСТОЕ восстановление уже полученного результата из уже
записанного лога (task5_true_arbitrageur_scan_launch.log) -- НИ ОДНОГО
нового RPC-вызова, никакого нового скана."""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
LOG_PATH = DATA_DIR / "task5_true_arbitrageur_scan_launch.log"
OUT_PATH = DATA_DIR / "task5_true_arbitrageur_scan_result.json"


def main() -> None:
    lines = LOG_PATH.read_text().splitlines()
    # JSON верхнего уровня начинается со строки "{" и заканчивается строкой
    # "}" непосредственно ПЕРЕД "Traceback (most recent call last):"
    # (порядок в скрипте: print(text) -- ПОТОМ write_text, которая упала).
    try:
        tb_idx = next(i for i, l in enumerate(lines) if l.startswith("Traceback (most recent call last):"))
    except StopIteration:
        print(json.dumps({"error": "в логе не найдено 'Traceback' -- возможно, скрипт не падал/ещё не завершился"}))
        return
    # Ищем последнюю строку "}" ПЕРЕД traceback -- конец JSON-блока.
    end_idx = None
    for i in range(tb_idx - 1, -1, -1):
        if lines[i].strip() == "}":
            end_idx = i
            break
    if end_idx is None:
        print(json.dumps({"error": "не найден конец JSON-блока перед traceback"}))
        return
    # Ищем НАЧАЛО этого JSON-блока -- первая строка "{" в самом начале
    # уровня отступа 0, идя назад от end_idx, до строки, которая тоже "{"
    # на нулевом отступе (json.dumps(indent=2) -- верхний "{" не имеет
    # ведущих пробелов).
    start_idx = None
    for i in range(end_idx, -1, -1):
        if lines[i] == "{":
            start_idx = i
            break
    if start_idx is None:
        print(json.dumps({"error": "не найдено начало JSON-блока (строка '{' без отступа)"}))
        return

    json_text = "\n".join(lines[start_idx:end_idx + 1])
    try:
        result = json.loads(json_text)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"извлечённый текст не парсится как JSON: {exc}",
                            "start_idx": start_idx, "end_idx": end_idx, "n_lines_extracted": end_idx - start_idx + 1}))
        return

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    summary = {
        "recovered": True,
        "saved_to": str(OUT_PATH),
        "window_from_block": result.get("window_from_block"),
        "window_to_block_actually_covered": result.get("window_to_block_actually_covered"),
        "scan_timed_out": result.get("scan_timed_out"),
        "n_tx_with_any_swap": result.get("n_tx_with_any_swap"),
        "n_multi_pool_tx": result.get("n_multi_pool_tx"),
        "n_unique_sender_groups": result.get("n_unique_sender_groups"),
        "n_sender_groups_checked": result.get("n_sender_groups_checked"),
        "n_sender_groups_qualifying": result.get("n_sender_groups_qualifying"),
        "n_qualifying_transactions_total": result.get("n_qualifying_transactions_total"),
        "by_contract": result.get("by_contract"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "qualifying_rows": result.get("qualifying_rows"),
    }
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
