#!/usr/bin/env python3
"""Форензика fomo -- 0 совпадений кошельков даже за 7 дней (при 4.96М
логов контракта/день) означает: либо адрес трейдера не лежит в
topic1/topic2/topic3 события ВООБЩЕ (тогда наш поиск структурно не
может найти совпадение, не важно, сколько дней брать), либо трейдеры
взаимодействуют через промежуточный роутер, и в логах ЭТОГО контракта
их адреса просто нет. Проверяем структурно, не гадаем: сырой сэмпл
(LIMIT 20, БЕЗ фильтра по кошелькам) доминирующего topic0
(0x40e9cecb9f... -- 4 335 764 из 4 962 343 логов/день, 87%) --
смотрим реальные topic1/topic2/topic3/data этого события."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "fomo_forensics_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_pool_dominant_event_sample_result.json")
NAMESPACE_BUDGET = 350.0

POOL_CONTRACT_NO_0X = "8366a39cc670b4001a1121b8f6a443a643e40951"
DOMINANT_TOPIC0 = "40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"


def run() -> int:
    ensure_namespace("fomo_forensics_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[dominant_sample] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()
    sql = f"""select lower(to_hex(tx_hash)) as tx_hash, block_number, block_time,
    lower(to_hex(topic1)) as topic1, lower(to_hex(topic2)) as topic2, lower(to_hex(topic3)) as topic3,
    lower(to_hex(data)) as data
from robinhood.logs
where lower(to_hex(contract_address)) = '{POOL_CONTRACT_NO_0X}'
    and lower(to_hex(topic0)) = '{DOMINANT_TOPIC0}'
    and block_time >= now() - interval '1' day
order by block_time desc
limit 20"""
    qid = client.create_query("fomo_pool_dominant_event_sample", sql)
    df = client.run_sql_cached("fomo_pool_dominant_event_sample", sql, query_id=qid,
                                estimated_credits=10.0, expected_max_rows=20, expected_columns=7)
    cost = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == "fomo_pool_dominant_event_sample"), None)
    rows = df.to_dict("records") if df is not None else []
    print(f"[dominant_sample] стоимость: {cost}, строк: {len(rows)}")
    for r in rows[:5]:
        print(f"  {r}")

    # Структурный анализ: сколько indexed-топиков реально присутствует,
    # длина data, похожи ли topic1/2/3 на адреса (последние 40 hex-символов
    # ненулевые при 24 нулевых первых) или на что-то другое (напр. числа).
    def looks_like_address(hex_val: str | float) -> bool:
        if not isinstance(hex_val, str) or len(hex_val) != 64:
            return False
        prefix, suffix = hex_val[:24], hex_val[24:]
        return prefix == "0" * 24 and suffix != "0" * 40

    analysis = []
    for r in rows:
        analysis.append({
            "tx_hash": r["tx_hash"],
            "topic1_looks_like_address": looks_like_address(r.get("topic1")),
            "topic2_looks_like_address": looks_like_address(r.get("topic2")),
            "topic3_looks_like_address": looks_like_address(r.get("topic3")),
            "data_len_bytes": (len((r.get("data") or "").removeprefix("0x")) // 2) if r.get("data") else 0,
        })

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "topic0": DOMINANT_TOPIC0, "cost": cost, "n_rows": len(rows),
        "sample_rows": rows, "structural_analysis": analysis,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[dominant_sample] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
