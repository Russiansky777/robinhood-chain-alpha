#!/usr/bin/env python3
"""Читает УЖЕ сохранённый (прогоном 34767602644, коммит 9ed37e5) файл
data/task5_v4_item3_in_window_control_trade_result.json на Ohio и печатает
ТОЛЬКО сжатую сводку ключевых полей -- НИКАКИХ новых RPC-вызовов, никакого
форка, никакого пересчёта. Нужно только потому, что полный JSON (601
кандидат) слишком большой для лога GitHub Actions/контекста -- это НЕ
повторный запуск item 3, просто безопасное чтение уже готового файла."""
import json
from pathlib import Path

RESULT_FILE = Path(__file__).parent.parent / "data" / "task5_v4_item3_in_window_control_trade_result.json"


def main() -> None:
    if not RESULT_FILE.exists():
        print(json.dumps({"ok": False, "error": f"файл не найден: {RESULT_FILE}"}, ensure_ascii=False))
        return
    d = json.loads(RESULT_FILE.read_text())

    summary = {
        "ok": d.get("ok"),
        "pilot_start_block_lookup": d.get("pilot_start_block_lookup"),
        "pilot_end_block_lookup": d.get("pilot_end_block_lookup"),
        "restart_evidence_note": (d.get("restart_evidence") or {}).get("note"),
        "n_candidates_in_window": d.get("n_candidates_in_window"),
        "n_fully_valid_closed_cycle_candidates": d.get("n_fully_valid_closed_cycle_candidates"),
        "chosen_control_trade": d.get("chosen_control_trade"),
        "fork_reconstruction_present": "fork_reconstruction" in d,
        "candidate_reconstruction_attempts": d.get("candidate_reconstruction_attempts"),
        "own_log_cross_reference": d.get("own_log_cross_reference"),
        "conclusion": d.get("conclusion"),
    }
    # Сколько кандидатов реально дали КАКОЙ-то ненулевой набор токенов
    # (для честной прозрачности -- почему 0 из n прошли строгий гейт):
    ffc = d.get("fund_flow_checks") or []
    reasons_histogram: dict[str, int] = {}
    for row in ffc:
        v = row.get("nonzero_net_flow_tokens")
        if v is None:
            key = row.get("verdict", row.get("error", "нет verdict/error"))[:80]
        else:
            key = f"{len(v)} ненулевых токенов"
        reasons_histogram[key] = reasons_histogram.get(key, 0) + 1
    summary["n_fund_flow_checks_total"] = len(ffc)
    summary["nonzero_token_count_histogram"] = reasons_histogram

    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
