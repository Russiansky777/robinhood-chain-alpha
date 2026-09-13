#!/usr/bin/env python3
"""Точечный поиск ОДНОЙ конкретной tx (указанной владельцем) по ВСЕМУ
уже сохранённому fund_flow_checks (не только среди 120 с одним ненулевым
токеном -- по любому числу ненулевых). Печатает её ПОЛНУЮ запись как
есть плюс исправленный (без унарного минуса) пересчёт net-flow по её же
legs. НИКАКИХ новых RPC-вызовов -- чистое чтение уже сохранного файла."""
import json
import sys
from pathlib import Path

RESULT_FILE = Path(__file__).parent.parent / "data" / "task5_v4_item3_in_window_control_trade_result.json"
TARGET_TX = "0xde38133b2cdf7f3b6307225fce0fad6c0914796a48290dcb5083fe51abdbdf71"


def recompute_corrected_net_flow(legs):
    net = {}
    incomplete = False
    for leg in legs:
        if leg.get("currency0") is None:
            incomplete = True
            continue
        c0, c1 = leg["currency0"], leg["currency1"]
        net[c0] = net.get(c0, 0) + leg["amount0"]
        net[c1] = net.get(c1, 0) + leg["amount1"]
    return net, incomplete


def main() -> None:
    d = json.loads(RESULT_FILE.read_text())
    ffc = d.get("fund_flow_checks") or []
    row = next((r for r in ffc if r.get("tx_hash", "").lower() == TARGET_TX.lower()), None)
    if row is None:
        print(json.dumps({
            "found": False,
            "note": f"tx {TARGET_TX} НЕ найдена среди {len(ffc)} fund_flow_checks этого прогона -- "
                     "либо она вне окна пилота (блок до 61631734 или после 61934900), либо не была "
                     "распознана как многоходовый кандидат сканом (task5_v4_competitor_trade_scan.py), "
                     "либо это другая транзакция того же адреса. Честно фиксируем, не подгоняем.",
        }, indent=2, ensure_ascii=False))
        return

    legs = row.get("legs") or []
    corrected_net, incomplete = recompute_corrected_net_flow(legs)
    nz_corrected = {t: v for t, v in corrected_net.items() if v != 0}

    print(json.dumps({
        "found": True,
        "full_row_as_saved": row,
        "corrected_net_flow_all_tokens": corrected_net,
        "corrected_nonzero_tokens": nz_corrected,
        "incomplete_leg_decode": incomplete,
    }, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
