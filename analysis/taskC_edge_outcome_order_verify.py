#!/usr/bin/env python3
"""Задача C -- проверка порядка исходов (outcomes vs clobTokenIds) на
Polymarket ПЕРЕД тем, как верить в найденный "спред" (только чтение).

РЕАЛЬНЫЙ РИСК: taskC_edge_live_now.py брал clobTokenIds[0] как "Yes"
для той же стороны, что Kalshi yes_k_ask, БЕЗ проверки, что
clobTokenIds[0] реально соответствует ТОМУ ЖЕ бойцу/исходу, что и
Kalshi "team_names[0]". Если порядок не совпадает -- посчитанный
"спред" сравнивает Yes одного бойца с No/Yes другого, что даёт
огромные фиктивные числа (найдено: 0.32, 0.63, 0.74 -- подозрительно
большие для реального кросс-платформенного арба). Печатаем СЫРЫЕ
outcomes + clobTokenIds для каждой из 3 "арбитражных" находок, чтобы
подтвердить или опровергнуть реальным полем `outcomes`, кто есть кто."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from taskC_sports_matcher import fetch_polymarket_bulk, normalize  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/taskC_edge_outcome_order_verify_result.json")
LIVE_RESULT_PATH = Path("data/p3_guard_cache/taskC_edge_live_now_result.json")


def run() -> int:
    live = json.loads(LIVE_RESULT_PATH.read_text())
    arb_entries = [r for r in live["results"] if r.get("spread") and r["spread"]["is_arb"]]
    print(f"[verify] проверяем {len(arb_entries)} найденных 'арбитражных' пар")

    pm_markets = fetch_polymarket_bulk()
    print(f"[verify] реальных Polymarket-рынков загружено: {len(pm_markets)}")

    out = {"checked": []}
    for entry in arb_entries:
        q_norm = normalize(entry["polymarket_question"])
        pm = next((m for m in pm_markets if normalize(m.get("question", "")) == q_norm), None)
        record = {"kalshi_event": entry["kalshi_event"], "kalshi_ticker": entry.get("kalshi_ticker"),
                   "polymarket_question": entry["polymarket_question"], "found": pm is not None}
        if pm:
            record["outcomes"] = pm.get("outcomes")
            record["outcomePrices"] = pm.get("outcomePrices")
            record["clobTokenIds"] = pm.get("clobTokenIds")
        out["checked"].append(record)
        print(json.dumps(record, indent=2, ensure_ascii=False))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[verify] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
