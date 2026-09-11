#!/usr/bin/env python3
"""Задача 5, владелец 2026-09-11, пункты 2 (частично) и 3:

Пункт 3: доля откатов (revert) для топ-20 исполнителей за 2026-09-10, по
`robinhood.transactions` (реальные колонки: `success` boolean, подтверждено
разведкой схемы), в разрезе исполнитель x контракт-получатель (`to`) --
владелец: "на тот же контракт-получатель".

Пункт 2 (частично, реальные текущие данные): `robinhood.transactions`
реально содержит колонки L2- и L1-компонент газа (`gas_used`,
`gas_price`, `effective_gas_price`, `l1_gas_used`, `l1_fee`,
`l1_fee_scalar`) -- это ДЕЙСТВУЮЩИЕ на цепи поля (Dune нормализует их
единообразно для L2-роллапов), не выдумка. НЕТ официально объявленного
Robinhood тарифа на период ПОСЛЕ 29.09 (проверено WebSearch, реальный
результат: "Robinhood has not made specific announcements about the
exact gas fee structure that will be in place after the subsidy
expires") -- честно нечего пересчитывать "по объявлению", которого не
существует. НО реально известно (WebSearch, с источниками): (а) с
~7-8.08.2026 субсидия покрывает только транзакции ДЕШЕВЛЕ $0.50 --
всё, что дороже, УЖЕ платится по реальному тарифу СЕЙЧАС, до 29.09; (б)
средняя комиссия на цепи ~$0.40/tx (отчёты 03-05.09.2026). Здесь
собираются РЕАЛЬНЫЕ текущие значения gas_used/gas_price/l1_fee для
топ-20 исполнителей 09-10 -- лучшая доступная замена отсутствующему
объявлению."""
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

OUT_PATH = Path("data/p3_guard_cache/task5_gas_reverts_top20_result.json")
NAMESPACE_BUDGET = 1650.0

TARGET_DAY = "2026-09-10"

# Реальный топ-20 по прибыли за 09-10 (task5_arb_top_executors_oneday_v4_9edca3a5c64e4abd.csv,
# та же реальная выгрузка, что уже использована в измерении 2 -- операторы).
TOP20_EXECUTORS = [
    "824ABDA7E8BFCF47C543761C50BC7BF6756DEDA9",
    "D83E60D273FD0CB3B05C2611DB940FECB7B6CA64",
    "DA8EF690D9C9B6C7DB1A5F95943C838309306B03",
    "FDE88016A65B2371F2C6E4F698A0C79598E46435",
    "FE68082A448F17A3D1700957B9F63176CECD334E",
    "C27A2B4BD5B374C3CB3C711E17901628824F8258",
    "B4864F7F85EB2FDBD58B4293A2B9BD684FB13336",
    "7660E7E9E674EFDF5CE56B14593DCB7482FB95FF",
    "4A5CCF5AC7CFA7B752E5FE0B019245A9AB3A6331",
    "E3DCD9730081A5521F434303BB86D2613786CA83",
    "71B20F298B6B90424853D6AD178BCDD969DABF0F",
    "9FB5031CCB57E7850262C6BF805F0C5C84125620",
    "968A950EC7A3FA0487AC1F9B541B98A4CE22C3E5",
    "7FE3F1051D64E324230FB540C5F50F549D569C96",
    "E5B033407AD25FD1C12151650B0F2AE4830FCD68",
    "B0BFB9FD9166067360AE25754CC3314258E4F335",
    "6F0AB6D37A0CA891BF61BFA139CAEDB70A036230",
    "49E700C831863E67457E5706FF2F5697AE420FA8",
    "A808F09FC880312C4ED229F15D8D41EEC4C0D72D",
    "4DB7080FEBC684E96FBBD367F248173E3DE9E003",
]


def build_sql() -> str:
    addr_list = ", ".join(f"from_hex('{a}')" for a in TOP20_EXECUTORS)
    return f"""
select
    to_hex(t."from") as executor,
    to_hex(t."to") as to_contract,
    t.success as success,
    count(*) as n_tx,
    avg(t.gas_used) as avg_gas_used,
    avg(cast(t.gas_used as double) * coalesce(cast(t.effective_gas_price as double), cast(t.gas_price as double))) as avg_l2_fee_wei,
    avg(t.l1_gas_used) as avg_l1_gas_used,
    avg(cast(t.l1_fee as double)) as avg_l1_fee_wei,
    avg(t.l1_fee_scalar) as avg_l1_fee_scalar
from robinhood.transactions t
where t.block_date = date '{TARGET_DAY}'
  and t."from" in ({addr_list})
group by to_hex(t."from"), to_hex(t."to"), t.success
order by executor, n_tx desc
"""


def run() -> int:
    ns = credit_guard.namespace()
    credit_guard.ensure_namespace(ns, NAMESPACE_BUDGET)
    remaining = credit_guard.remaining_cycle_budget(credit_guard.load_state())
    print(f"[gas_reverts_top20] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target_day": TARGET_DAY,
        "top20_executors": TOP20_EXECUTORS,
    }

    sql = build_sql()
    qid = client.create_query("task5_gas_reverts_top20_v1", sql)
    # Дешевле измерений 1/3 -- без self-join, прямая агрегация по
    # robinhood.transactions с узким предикатом на 20 адресов.
    df = client.run_sql_cached("task5_gas_reverts_top20_v1", sql, query_id=qid,
                                estimated_credits=40.0, expected_max_rows=250, expected_columns=9)
    rows = df.to_dict("records") if df is not None else []
    result["rows"] = rows
    print(f"[gas_reverts_top20] строк: {len(rows)}")

    def is_missing(v: object) -> bool:
        return v is None or v != v

    by_executor: dict[str, dict] = {}
    for r in rows:
        ex = r["executor"]
        b = by_executor.setdefault(ex, {"n_success": 0, "n_revert": 0, "by_contract": {}})
        n = r["n_tx"]
        if r["success"]:
            b["n_success"] += n
        else:
            b["n_revert"] += n
        c = b["by_contract"].setdefault(r["to_contract"], {"n_success": 0, "n_revert": 0})
        if r["success"]:
            c["n_success"] += n
        else:
            c["n_revert"] += n

    for ex, b in by_executor.items():
        total = b["n_success"] + b["n_revert"]
        b["revert_rate"] = (b["n_revert"] / total) if total else None

    total_success = sum(b["n_success"] for b in by_executor.values())
    total_revert = sum(b["n_revert"] for b in by_executor.values())
    total_all = total_success + total_revert

    real_fee_rows = [r for r in rows if not is_missing(r.get("avg_l2_fee_wei"))]
    avg_l2_fee_wei_overall = (
        sum(r["avg_l2_fee_wei"] * r["n_tx"] for r in real_fee_rows) / sum(r["n_tx"] for r in real_fee_rows)
        if real_fee_rows else None
    )
    real_l1_rows = [r for r in rows if not is_missing(r.get("avg_l1_fee_wei"))]
    avg_l1_fee_wei_overall = (
        sum(r["avg_l1_fee_wei"] * r["n_tx"] for r in real_l1_rows) / sum(r["n_tx"] for r in real_l1_rows)
        if real_l1_rows else None
    )

    result["by_executor"] = by_executor
    result["overall_revert_rate_top20"] = (total_revert / total_all) if total_all else None
    result["overall_n_success"] = total_success
    result["overall_n_revert"] = total_revert
    result["avg_l2_fee_wei_overall_top20"] = avg_l2_fee_wei_overall
    result["avg_l1_fee_wei_overall_top20"] = avg_l1_fee_wei_overall
    result["note_l1_l2_all_missing"] = (
        "Если avg_l2_fee_wei/avg_l1_fee_wei везде null -- значит колонки "
        "gas_price/effective_gas_price/l1_fee в РЕАЛЬНОЙ активной схеме "
        "robinhood.transactions называются/типизированы иначе, чем в "
        "information_schema пробе -- честно доложить, не считать нулём."
    )

    print(f"[gas_reverts_top20] общий revert rate топ-20 09-10: {result['overall_revert_rate_top20']}")
    print(f"[gas_reverts_top20] средняя L2-комиссия (wei): {avg_l2_fee_wei_overall}")
    print(f"[gas_reverts_top20] средняя L1-комиссия (wei): {avg_l1_fee_wei_overall}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    result["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[gas_reverts_top20] итого: {total_cost:.4f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
