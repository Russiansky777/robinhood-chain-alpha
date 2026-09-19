#!/usr/bin/env python3
"""Владелец: приоритет 1, шаг 2 -- LEADER_WALLET сошёлся (METHOD_VALIDATED=True,
data/solana_batch5_leader_validation_result.json), теперь Brez, без
дополнительного разрешения. Тот же 72ч-скан (scan_wallet), ожидание
~12 покупок >=2 SOL за 72ч (T29)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_batch5_rpc_check import scan_wallet  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_batch5_brez_validation_result.json"
BREZ = "Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB"
EXPECTED_GE2SOL_PER_72H = 12.0


def main() -> None:
    scan = scan_wallet(BREZ)
    summary = {k: v for k, v in scan.items() if k != "entries"}
    n_per_day = round(scan["n_first_entries_ge_2sol"] / (scan["coverage_hours_actual"] / 24), 2) if scan["coverage_hours_actual"] else None
    n_expected_over_actual_coverage = round(EXPECTED_GE2SOL_PER_72H * scan["coverage_hours_actual"] / 72, 2) if scan["coverage_hours_actual"] else None
    order_of_magnitude_ok = (scan["n_first_entries_ge_2sol"] > 0 and n_expected_over_actual_coverage and
                              n_expected_over_actual_coverage * 0.3 <= scan["n_first_entries_ge_2sol"] <= n_expected_over_actual_coverage * 3)
    result = {
        "wallet": BREZ, "scan_72h_summary": summary,
        "n_ge_2sol_per_day_actual": n_per_day, "expected_ge2sol_per_72h_T29": EXPECTED_GE2SOL_PER_72H,
        "expected_ge2sol_scaled_to_actual_coverage": n_expected_over_actual_coverage,
        "order_of_magnitude_ok": order_of_magnitude_ok,
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[brez_validation] первых входов={scan['n_first_entries_72h']} >=2SOL={scan['n_first_entries_ge_2sol']} "
          f">=4.3SOL={scan['n_first_entries_ge_4_3sol']} медиана={scan['median_spend_sol_equiv']} "
          f"coverage={scan['coverage_status']} ({scan['coverage_hours_actual']}ч) "
          f"order_of_magnitude_ok={order_of_magnitude_ok}", flush=True)


if __name__ == "__main__":
    main()
