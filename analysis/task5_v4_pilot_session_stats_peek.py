#!/usr/bin/env python3
"""ЧИСТО ЧИТАЮЩИЙ вспомогательный скрипт (продолжение task5_v4_pilot_session_stats.py,
без нового скана/пилота/RPC): показывает РЕАЛЬНЫЙ текст detail нескольких
записей reason=='нет прибыльного цикла' этой сессии, не совпавших ни с
одним из 3 текстовых шаблонов classify_bucket() -- чтобы честно уточнить
эвристику по факту, а не по непрочитанному целиком коду recompute_route().
Читает уже сохранённый срез сессии (session_only_no_send_log.jsonl),
новых файлов не создаёт."""
import json
from pathlib import Path

PATH = Path("/home/bot/data/task5_v4_pilot_session_only_no_send_log.jsonl")


def main() -> None:
    result: dict = {}
    if not PATH.exists():
        result["ok"] = False
        result["error"] = f"файл-срез сессии не найден: {PATH} (сначала нужен task5_v4_pilot_session_stats.py)"
        print(json.dumps(result, ensure_ascii=False))
        return

    rows = [json.loads(l) for l in PATH.read_text().splitlines() if l.strip()]
    markers = ("профит(до газа)", "после газа", "устарел", "сдвинулся", "уже не подходит")
    cycle_rows = [r for r in rows if r.get("reason") == "нет прибыльного цикла"]
    unmatched = [r for r in cycle_rows if not any(m in (r.get("detail") or "") for m in markers)]

    result["n_reason_nо_profitable_cycle_total"] = len(cycle_rows)
    result["n_unmatched_by_current_heuristic"] = len(unmatched)
    result["sample_unmatched_detail_texts"] = [r.get("detail") for r in unmatched[:10]]
    result["sample_unmatched_full_rows"] = unmatched[:5]
    result["ok"] = True
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
