#!/usr/bin/env python3
"""Задача 5, предфильтр -- пункты 2/4 (продолжение): ТОЛЬКО извлечение
уже сохранённых полей из УЖЕ существующих файлов ../data/ (найдены
предыдущим read-only прогоном task5_v4_hook_coverage_and_control_
recovery.py) -- ничего не реконструирует, ничего не запрашивает по
сети. task5_v4_item3_in_window_control_trade_result.json -- 3.6MB,
целиком печатать нельзя (обрежется построчным лимитом лога CI),
поэтому берём ТОЛЬКО нужные под-поля."""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
TARGET_TX = "0x878fb998474230338a47a710d2c25689987a836074729ae61e112dcccca7cd1b"


def main() -> None:
    result: dict = {}

    p = DATA_DIR / "task5_v4_hook_route_audit_result.json"
    result["hook_route_audit_full"] = json.loads(p.read_text()) if p.exists() else {"error": "не найден"}

    p2 = DATA_DIR / "task5_v4_item3_round11_competitor_size_result.json"
    if p2.exists():
        d = json.loads(p2.read_text())
        result["round11_competitor_size_relevant_fields"] = {
            k: d.get(k) for k in (
                "target_tx_hash", "target_block", "competitor_leg0_eth_input_from_swap_event",
                "real_gas_cost_wei", "our_bot_at_competitor_size", "revert_after_competitor_size_ok",
                "our_bot_at_0_02_eth", "revert_after_0_02_eth_ok", "competitor_profit_breakdown",
                "grid_size_hypothesis_confirmed", "grid_size_hypothesis_conclusion",
            )
        }
    else:
        result["round11_competitor_size_relevant_fields"] = {"error": "не найден"}

    p3 = DATA_DIR / "task5_v4_item3_in_window_control_trade_result.json"
    if p3.exists():
        d = json.loads(p3.read_text())
        result["in_window_control_trade_relevant_fields"] = {
            "chosen_control_trade": d.get("chosen_control_trade"),
            "n_fully_valid_closed_cycle_candidates": d.get("n_fully_valid_closed_cycle_candidates"),
            "ok": d.get("ok"),
            "conclusion": d.get("conclusion"),
        }
        target_row = None
        for row in d.get("fund_flow_checks", []):
            if str(row.get("tx_hash", "")).lower() == TARGET_TX.lower():
                target_row = row
                break
        result["fund_flow_check_for_target_tx"] = target_row if target_row is not None else \
            {"error": f"{TARGET_TX} не найден среди fund_flow_checks (проверено {len(d.get('fund_flow_checks', []))} строк)"}
    else:
        result["in_window_control_trade_relevant_fields"] = {"error": "не найден"}
        result["fund_flow_check_for_target_tx"] = {"error": "исходный файл не найден"}

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = DATA_DIR / "task5_v4_hook_model_extract_from_saved_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
