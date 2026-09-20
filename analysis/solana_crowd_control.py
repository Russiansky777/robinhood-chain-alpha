#!/usr/bin/env python3
"""Владелец, ночное задание п.2, контроль ДО 90 кошельков.

Эталон (Dune, из промпта владельца): лидер Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit,
его покупки >=15 SOL за 72ч -- медианный рост цены к +30с ~+17%.
Brez Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB, покупки >=2 SOL --
~+11%. Критерий (владелец): совпадение по ЗНАКУ и ПОРЯДКУ ВЕЛИЧИНЫ --
не точное число. Не более 2 попыток чинить, потом стоп с сырыми
данными по одной покупке."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_crowd_common import analyze_wallet  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_crowd_control_result.json"

LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
BREZ = "Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB"
EXPECTED = {LEADER_WALLET: {"name": "LEADER", "min_sol": 15.0, "expected_pct": 17.0},
            BREZ: {"name": "Brez", "min_sol": 2.0, "expected_pct": 11.0}}


def same_sign_same_order(got: float, expected: float) -> bool:
    if got == 0 or expected == 0:
        return False
    if (got > 0) != (expected > 0):
        return False
    ratio = abs(got) / abs(expected)
    return 0.3 <= ratio <= 3.0  # порядок величины, не точное число


def main() -> None:
    result = {"expected": EXPECTED, "checks": {}}
    all_ok = True
    for addr, spec in EXPECTED.items():
        print(f"[crowd_control] считаю {spec['name']} ({addr[:10]}..)...", flush=True)
        r = analyze_wallet(addr, spec["name"], min_sol=spec["min_sol"])
        got = r.get("median_growth_pct_30s")
        ok = got is not None and same_sign_same_order(got, spec["expected_pct"])
        all_ok = all_ok and ok
        result["checks"][addr] = {**r, "expected_pct": spec["expected_pct"], "match_sign_and_order": ok}
        print(f"[crowd_control] {spec['name']}: n={r['n_purchases_analyzed']} "
              f"unresolved={r['n_unresolved']} empty={r['n_empty']} "
              f"медиана_роста={got} (ожидание~{spec['expected_pct']}%) match={ok}", flush=True)
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        fp._git_commit_progress("crowd_control", [OUT_PATH])

    result["DECISION"] = "go" if all_ok else "no-go"
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    fp._git_commit_progress("crowd_control_final", [OUT_PATH])
    print(f"[crowd_control] ИТОГ: {result['DECISION']}", flush=True)


if __name__ == "__main__":
    main()
