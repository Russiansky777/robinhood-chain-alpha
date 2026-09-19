#!/usr/bin/env python3
"""Владелец: приоритет 1 -- валидация метода СНАЧАЛА на одном адресе
(LEADER_WALLET), остальное не запускать, пока этот не сойдётся.

Часть 1: 8 известных покупок лидера (leader_signature из 8 логов трассы
пилота) -- classify_tx(LEADER_WALLET) должен дать first_entry=да,
сумма в пределах ±2% от независимого эталона:
  - 7 из 8 подтверждены в data/solana_phase2_events_v3.json (Dune-
    верифицированный spend_sol_equiv, метод "Источника B", уже
    подтверждён на 300/300 транзакциях);
  - jupcat -- честно ЧЕСТНО без независимого SOL-эталона (не входит в
    окно фазы 3, data/solana_dbot_realized_ledger.json даёт только
    ЦЕНУ за токен, не размер сделки лидера в SOL) -- метод всё равно
    прогоняется и результат показывается, но без строки "совпало да/нет".

Часть 2: 72ч скан ТОЛЬКО LEADER_WALLET (тот же classify_tx/scan_wallet,
что и остальной BATCH-5-конвейер) -- итог: всего первых покупок, >=2 SOL,
>=4.3 SOL, медиана. Ожидание (Фаза 3, 5 дней): ~17 входов>=2SOL/сутки."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_batch5_rpc_check import classify_tx, scan_wallet, LOOKBACK_HOURS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_batch5_leader_validation_result.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"

PILOT_LOGS = ["jupcat", "xzc5swuory", "egp1f5j9ld", "gtdzkaqvmz", "vaddewuhuy", "hgcxvs6kjh", "7runv1hjfc", "gpuxeqplff"]
TOLERANCE = 0.02


def load_ground_truth() -> dict[str, dict]:
    """sig -> {label, expected_sol (или None, если независимого эталона нет)}."""
    phase2 = json.loads((REPO_ROOT / "data/solana_phase2_events_v3.json").read_text())
    leader = phase2["wallet_roles"]["leader"][0]
    by_tx = {e["tx_id"]: e for e in phase2["events_by_wallet"][leader]}

    out = {}
    for label in PILOT_LOGS:
        p = REPO_ROOT / f"data/solana_entry_log_{label}.json"
        if not p.exists():
            continue
        sig = json.loads(p.read_text())["leader_signature"]
        e = by_tx.get(sig)
        out[sig] = {"label": label, "expected_sol": e.get("spend_sol_equiv") if e else None,
                    "expected_source": "phase2_events_v3 (Dune-верифицировано)" if e else "нет независимого эталона (вне окна фазы 3)"}
    return out


def main() -> None:
    ground_truth = load_ground_truth()
    print(f"[leader_validation] известных покупок: {len(ground_truth)}", flush=True)

    known_table = []
    for sig, gt in ground_truth.items():
        tx = fp.get_transaction(sig)
        if tx is None:
            known_table.append({**gt, "signature": sig, "HONEST_ANSWER": "getTransaction вернул null"})
            continue
        ev = classify_tx(tx, LEADER_WALLET)
        got = ev.get("spend_sol_equiv") if ev and ev.get("kind") == "first_entry" else None
        row = {"label": gt["label"], "signature": sig, "expected_sol": gt["expected_sol"],
               "expected_source": gt["expected_source"], "classify_kind": ev.get("kind") if ev else None,
               "got_sol": got}
        if gt["expected_sol"] is not None and got is not None:
            row["match_within_2pct"] = abs(got - gt["expected_sol"]) / gt["expected_sol"] < TOLERANCE
        else:
            row["match_within_2pct"] = None
        known_table.append(row)
        print(f"[leader_validation] {gt['label']}: expected={gt['expected_sol']} got={got} "
              f"match={row['match_within_2pct']}", flush=True)

    core = [r for r in known_table if r["expected_sol"] is not None]
    all_known_pass = all(r["match_within_2pct"] for r in core) if core else None

    print("[leader_validation] запускаю 72ч скан LEADER_WALLET...", flush=True)
    scan = scan_wallet(LEADER_WALLET)
    scan_summary = {k: v for k, v in scan.items() if k != "entries"}
    print(f"[leader_validation] 72ч: первых входов={scan['n_first_entries_72h']} "
          f">=2SOL={scan['n_first_entries_ge_2sol']} >=4.3SOL={scan['n_first_entries_ge_4_3sol']} "
          f"медиана={scan['median_spend_sol_equiv']} coverage={scan['coverage_status']}", flush=True)

    n_per_day_ge2sol = round(scan["n_first_entries_ge_2sol"] / (scan["coverage_hours_actual"] / 24), 2) if scan["coverage_hours_actual"] else None
    expected_per_day_phase3 = 17.0
    order_of_magnitude_ok = (n_per_day_ge2sol is not None and
                              expected_per_day_phase3 * 0.3 <= n_per_day_ge2sol <= expected_per_day_phase3 * 3)

    result = {
        "wallet": LEADER_WALLET, "known_purchases_table": known_table,
        "n_known_with_ground_truth": len(core), "all_known_pass_within_2pct": all_known_pass,
        "scan_72h_summary": scan_summary,
        "n_ge_2sol_per_day_actual": n_per_day_ge2sol, "n_ge_2sol_per_day_expected_phase3": expected_per_day_phase3,
        "order_of_magnitude_ok": order_of_magnitude_ok,
        "METHOD_VALIDATED": bool(all_known_pass) and bool(order_of_magnitude_ok),
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[leader_validation] ИТОГ: known_pass={all_known_pass} order_of_magnitude_ok={order_of_magnitude_ok} "
          f"METHOD_VALIDATED={result['METHOD_VALIDATED']}", flush=True)


if __name__ == "__main__":
    main()
