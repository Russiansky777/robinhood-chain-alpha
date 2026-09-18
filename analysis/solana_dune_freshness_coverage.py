#!/usr/bin/env python3
"""Владелец, 2026-09-19: перед вердиктом по Dune -- проверить свежесть
таблиц и покрытие по возрасту данных, прежде чем объявлять decoder
негодным. Три честные проверки, вердикт `solana_dune_explorer_check.py`
НЕ переписывается, пока эти три не сделаны:

1. `max(block_time), count(*)` для dex_solana.trades и pumpdotfun_solana.trades
   -- насколько таблицы отстают от текущего момента.
2. Покрытие 300 реальных покупок лидера ПО ДНЯМ ВОЗРАСТА (день -1..-14 от
   текущего момента) -- честно только те дни, что реально покрыты нашим
   датасетом (окно ~5.75-7 суток назад, не выдумываем день -14, если его
   физически нет в наших 300).
3. Только если старые данные ТОЖЕ дают ~0 -- распределение покупок по
   DEX-программам из нашего датасета (уже локально, без Dune) для
   сопоставления со списком project, который Dune реально показал в
   калибровке (`data/solana_dune_explorer_check.json`)."""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, LEADER_WALLET, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_freshness_coverage.json"

DEX_TABLES = ["dex_solana.trades", "pumpdotfun_solana.trades"]
AGE_BUCKETS_DAYS = [1, 2, 3, 5, 7, 14]  # честно -- бакеты, для которых реально проверяем покрытие


def step_a1_freshness(probe: DuneProbe) -> dict:
    out = {}
    for tbl in DEX_TABLES:
        sql = f"SELECT max(block_time) AS max_bt, count(*) AS n FROM {tbl}"
        r = probe.run_sql_sync(f"freshness_{tbl.replace('.', '_')}", sql, timeout_s=300)
        row = (r.get("rows") or [{}])[0] if r.get("status") == "ok" else {}
        out[tbl] = {"status": r.get("status"), "max_block_time": row.get("max_bt"),
                    "row_count": row.get("n"), "cost_credits": (r.get("status_meta") or {}).get("execution_cost_credits")}
        print(f"[dune_fresh] {tbl}: {out[tbl]}", flush=True)
    return out


def step_a2_coverage_by_age(probe: DuneProbe) -> dict:
    sel_path = REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json"
    rows = json.loads(sel_path.read_text())
    our_sigs_by_time = [(r["time"], r["signature"]) for r in rows]
    now = int(time.time())
    earliest_our_time = min(t for t, _ in our_sigs_by_time)
    days_covered_by_us = round((now - earliest_our_time) / 86400, 2)

    lo_time = earliest_our_time - 60
    hi_time = max(t for t, _ in our_sigs_by_time) + 60
    sql = (f"SELECT tx_id, block_time FROM dex_solana.trades WHERE trader_id = '{LEADER_WALLET}' "
           f"AND block_time >= from_unixtime({lo_time}) AND block_time <= from_unixtime({hi_time})")
    r = probe.run_sql_sync("coverage_full_range_for_bucketing", sql, timeout_s=300)
    out = {"days_actually_covered_by_our_dataset": days_covered_by_us,
           "honest_note": "бакеты возраста дальше нашего реального окна (earliest_our_time) не проверяются -- "
                           "там просто нет наших покупок для сравнения, не выдумываем цифру",
           "dune_step": {k: v for k, v in r.items() if k != "rows"}}
    if r.get("status") != "ok":
        return out
    dune_tx_ids = {row["tx_id"] for row in r["rows"]}
    out["dune_n_rows_full_range"] = len(r["rows"])

    buckets = []
    for age_days in AGE_BUCKETS_DAYS:
        bucket_hi = now - (age_days - 1) * 86400
        bucket_lo = now - age_days * 86400
        our_in_bucket = [sig for t, sig in our_sigs_by_time if bucket_lo <= t < bucket_hi]
        if not our_in_bucket:
            buckets.append({"age_days": age_days, "n_our_purchases": 0,
                             "note": "нет наших покупок в этом дне -- вне реального окна датасета"})
            continue
        found = sum(1 for sig in our_in_bucket if sig in dune_tx_ids)
        buckets.append({"age_days": age_days, "n_our_purchases": len(our_in_bucket),
                         "n_found_in_dune": found, "coverage": round(found / len(our_in_bucket), 4)})
        print(f"[dune_fresh] возраст день-{age_days}: наших={len(our_in_bucket)} "
              f"найдено_в_dune={found} покрытие={buckets[-1].get('coverage')}", flush=True)
    out["coverage_by_age_bucket"] = buckets
    return out


def step_a3_program_distribution() -> dict:
    """Локально, без Dune -- распределение программ из нашего датасета,
    для сопоставления с project-значениями, которые Dune реально вернул
    в калибровке (см. data/solana_dune_explorer_check.json)."""
    sel_path = REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json"
    rows = json.loads(sel_path.read_text())
    KNOWN_INFRA = {
        "ComputeBudget111111111111111111111111111111", "11111111111111111111111111111111",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    }
    prog_counter = Counter()
    for r in rows:
        for p in r.get("programs", []):
            if p not in KNOWN_INFRA:
                prog_counter[p] += 1

    dune_check_path = REPO_ROOT / "data" / "solana_dune_explorer_check.json"
    dune_projects_seen = set()
    if dune_check_path.exists():
        d = json.loads(dune_check_path.read_text())
        for row in (d.get("step2_calibration", {}).get("dune_step", {}).get("rows") or []):
            if row.get("project"):
                dune_projects_seen.add(row["project"])

    return {
        "our_dex_program_distribution_excl_infra": dict(prog_counter.most_common(30)),
        "n_distinct_dex_programs": len(prog_counter),
        "dune_project_labels_seen_in_calibration_sample": sorted(dune_projects_seen),
        "honest_note": "Dune возвращает project-ЛЕЙБЛЫ (не program ID напрямую) -- без исходников decoder'а "
                        "нельзя гарантировать точное 1:1 соответствие конкретного program ID конкретному project; "
                        "сопоставление ниже -- по силе присутствия/отсутствия известных лейблов, не строгая проверка.",
    }


def main() -> None:
    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой -- дальше идти некуда."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[dune_fresh] " + result["HONEST_ANSWER"], flush=True)
        return
    probe = DuneProbe(os.environ[key_name])

    result["step_a1_freshness"] = step_a1_freshness(probe)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    result["step_a2_coverage_by_age"] = step_a2_coverage_by_age(probe)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    cov = result["step_a2_coverage_by_age"].get("coverage_by_age_bucket") or []
    still_zero = all((b.get("coverage") or 0) < 0.05 for b in cov if "coverage" in b)
    result["still_near_zero_on_old_data"] = still_zero
    if still_zero:
        result["step_a3_program_distribution"] = step_a3_program_distribution()

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[dune_fresh] прогон завершён, still_near_zero_on_old_data={still_zero}", flush=True)


if __name__ == "__main__":
    main()
