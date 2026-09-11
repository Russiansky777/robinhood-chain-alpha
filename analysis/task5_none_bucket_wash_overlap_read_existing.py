#!/usr/bin/env python3
"""Задача 5, владелец 2026-09-11 (доп. подтверждение): "Превышение 3.54
кредита сверх лимита -- подтверждено владельцем, читай."

`task5_none_bucket_wash_overlap.py` реально выполнился (execute стоил
279.695 против оценки 250, execution_id `01M28K0TVE99F4P11DWY262EHT`),
но `credit_guard` корректно отказался платить за чтение результата --
цикл Mozila на тот момент был реально превышен (2003.54 > 2000.0).
Владелец явно подтвердил перерасход и разрешил читать; лимит цикла
поднят в `data/credits_spent_mozila.json` (2000.0 -> 2030.0, см. запись
в billing_cycle.initialized_by) -- ЭТО ФИКСАЦИЯ УЖЕ СЛУЧИВШЕГОСЯ ФАКТА
(деньги потрачены независимо от того, читаем мы результат или нет), не
новое разрешение тратить дальше.

Этот скрипт НЕ пересчитывает и НЕ исполняет запрос заново -- только
`fetch_existing(execution_id)` (см. dune_client.py: GET .../status
бесплатно, GET .../results платно по объёму данных, но результат тут
всего ~15-20 агрегированных строк -- копейки). Вся аналитика (доля
'none' от накрутки/high-freq, сравнение с 0/1/2/3+, вердикт >50%) --
идентична оригинальному run() из task5_none_bucket_wash_overlap.py,
скопирована сюда, чтобы не тянуть побочные эффекты (namespace budget
check, повторный create_query) из основного скрипта."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task5_active_arb_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_none_bucket_wash_overlap_result.json")
EXECUTION_ID = "01M28K0TVE99F4P11DWY262EHT"


def run() -> int:
    ns = credit_guard.namespace()
    remaining = credit_guard.remaining_cycle_budget(credit_guard.load_state())
    print(f"[none_wash_overlap_read] остаток общего цикла Dune (Mozila) после владельческого поднятия лимита: {remaining:.2f}")

    client = DuneClient()
    df, status, stats = client.fetch_existing(
        EXECUTION_ID, name="task5_none_wash_overlap_v1_reread",
        expected_max_rows=20, expected_columns=5,
    )
    rows = df.to_dict("records") if df is not None else []
    print(f"[none_wash_overlap_read] строк: {len(rows)}")
    for r in rows:
        print(f"  {r}")

    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "execution_id": EXECUTION_ID,
        "note": (
            "Прочитано из УЖЕ ОПЛАЧЕННОГО execute (279.695 кредитов, реально "
            "потрачено в предыдущем прогоне) -- этот скрипт платит только за "
            "само чтение результата (объём мал, копейки), не за пересчёт."
        ),
        "distribution": rows,
    }

    none_rows = [r for r in rows if r.get("distance_bucket") == "none"]

    summary = {}
    for bucket in ("0", "1", "2", "3+", "none"):
        brows = [r for r in rows if r.get("distance_bucket") == bucket]
        total_usd = sum(r["total_profit_usd"] for r in brows)
        wash_usd = sum(r["total_profit_usd"] for r in brows if r.get("category") == "known_wash")
        hf_usd = sum(r["total_profit_usd"] for r in brows if r.get("category") == "high_freq_other")
        total_tx = sum(r["n_txs"] for r in brows)
        wash_tx = sum(r["n_txs"] for r in brows if r.get("category") == "known_wash")
        hf_tx = sum(r["n_txs"] for r in brows if r.get("category") == "high_freq_other")
        summary[bucket] = {
            "total_usd": total_usd,
            "known_wash_usd": wash_usd,
            "known_wash_share_usd": (wash_usd / total_usd) if total_usd else None,
            "high_freq_other_usd": hf_usd,
            "high_freq_other_share_usd": (hf_usd / total_usd) if total_usd else None,
            "combined_wash_plus_highfreq_share_usd": ((wash_usd + hf_usd) / total_usd) if total_usd else None,
            "total_tx": total_tx,
            "known_wash_tx": wash_tx,
            "known_wash_share_tx": (wash_tx / total_tx) if total_tx else None,
            "high_freq_other_tx": hf_tx,
            "high_freq_other_share_tx": (hf_tx / total_tx) if total_tx else None,
        }
    result["summary_by_bucket"] = summary

    none_total_usd = sum(r["total_profit_usd"] for r in none_rows)
    none_wash_usd = sum(r["total_profit_usd"] for r in none_rows if r.get("category") == "known_wash")
    none_highfreq_usd = sum(r["total_profit_usd"] for r in none_rows if r.get("category") == "high_freq_other")
    result["none_bucket_wash_share_usd"] = (none_wash_usd / none_total_usd) if none_total_usd else None
    result["none_bucket_combined_share_usd"] = (
        (none_wash_usd + none_highfreq_usd) / none_total_usd if none_total_usd else None
    )
    result["verdict_gt_50pct"] = bool(
        none_total_usd and (none_wash_usd + none_highfreq_usd) / none_total_usd > 0.5
    )

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"[none_wash_overlap_read] ВЕРДИКТ (>50% 'none' -- внутренние циклы накрутки): {result['verdict_gt_50pct']}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    result["total_cost_credits_this_read"] = total_cost
    result["total_cost_credits_including_original_execute"] = total_cost + 279.695
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[none_wash_overlap_read] итого за это чтение: {total_cost:.4f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
