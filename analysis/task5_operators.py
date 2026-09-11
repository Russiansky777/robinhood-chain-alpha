#!/usr/bin/env python3
"""Задача 5, измерение 2 (владелец, 2026-09-11) -- операторы: топ-50
исполнителей дня -- байткод (EOA/контракт, совпадения), первый входящий
перевод (общий источник фондирования), совпадение часовой активности.
Свернуть в операторов, пересчитать "позиции 2-10" по операторам.

Реальные таблицы (найдены в task5_operators_schema_probe, дёшево,
4.98 кредита): robinhood.contracts / robinhood.creation_traces
(address, code, from=деплойер) для байткода; robinhood.transactions
для первого входящего перевода. Цепь короткая (131 день, генезис
2026-04-30 -- проверено task5_operators_tx_table_probe, 0.54 кредита)
-- полный обзор истории безопасен, JOIN (не OR-цепочка) на 50 адресов.

Дизайн: три УЗКИХ SQL-подзапроса (контракт-инфо, первый перевод,
часовая активность) по конкретным 50 адресам -- каждый дешёвый point-
lookup, НЕ полное пересканирование дня/истории с широким фильтром.
Кластеризация в "операторов" (общий байткод ИЛИ общий источник
фондирования ИЛИ высокая корреляция часовой активности) делается в
Python на уже скачанном малом результате (50 строк) -- не требует
дополнительных Dune-запросов."""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task5_active_arb_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_operators_result.json")

# Реальный топ-50 исполнителей дня (task5_arb_top_executors_oneday_v4_9edca3a5c64e4abd.csv,
# сортировка total_profit_usd desc) -- захардкожено, уже реально получено и оплачено.
TOP_EXECUTORS_CSV = Path("data/task5_active_arb_mozila_cache/task5_arb_top_executors_oneday_v4_9edca3a5c64e4abd.csv")


def load_top50() -> list[dict]:
    import csv
    rows = []
    with TOP_EXECUTORS_CSV.open() as f:
        for row in csv.DictReader(f):
            rows.append({"executor": row["executor"], "total_profit_usd": float(row["total_profit_usd"]),
                         "n_profitable_txs": int(row["n_profitable_txs"])})
    rows.sort(key=lambda r: -r["total_profit_usd"])
    return rows[:50]


def build_contract_info_sql(addrs: list[str]) -> str:
    values = ", ".join(f"(from_hex('{a}'))" for a in addrs)
    return f"""
with target_addrs as (select addr from (values {values}) as t(addr)),
contract_info as (
    select c.address, to_hex(c."from") as deployer, c.name, to_hex(md5(c.code)) as code_hash, length(c.code) as code_len
    from robinhood.contracts c
    inner join target_addrs t on t.addr = c.address
)
select to_hex(ta.addr) as executor, (ci.address is not null) as is_contract,
       ci.deployer, ci.name, ci.code_hash, ci.code_len
from target_addrs ta
left join contract_info ci on ci.address = ta.addr
"""


def build_first_incoming_sql(addrs: list[str]) -> str:
    values = ", ".join(f"(from_hex('{a}'))" for a in addrs)
    return f"""
with target_addrs as (select addr from (values {values}) as t(addr)),
first_incoming as (
    select tx."to" as addr, min_by(tx."from", tx.block_time) as funding_source, min(tx.block_time) as first_seen
    from robinhood.transactions tx
    inner join target_addrs t on t.addr = tx."to"
    group by tx."to"
)
select to_hex(ta.addr) as executor, to_hex(fi.funding_source) as funding_source, cast(fi.first_seen as varchar) as first_seen
from target_addrs ta
left join first_incoming fi on fi.addr = ta.addr
"""


def build_hourly_activity_sql(addrs: list[str], day_start: str, day_end: str) -> str:
    values_hex = ", ".join(f"'{a}'" for a in addrs)
    return f"""
with v3_acts as (
    select to_hex(s.evt_tx_from) as executor, hour(s.evt_block_time) as hour_utc, count(*) as n
    from uniswap_v3_robinhood.uniswapv3pool_evt_swap s
    where s.evt_block_time >= timestamp '{day_start}' and s.evt_block_time < timestamp '{day_end}'
      and to_hex(s.evt_tx_from) in ({values_hex})
    group by to_hex(s.evt_tx_from), hour(s.evt_block_time)
),
v4_acts as (
    select to_hex(w.sender) as executor, hour(w.block_time) as hour_utc, count(*) as n
    from uniswap_v4_robinhood.swaps w
    where w.block_time >= timestamp '{day_start}' and w.block_time < timestamp '{day_end}'
      and to_hex(w.sender) in ({values_hex})
    group by to_hex(w.sender), hour(w.block_time)
),
combined as (
    select * from v3_acts
    union all
    select * from v4_acts
)
select executor, hour_utc, sum(n) as n_swaps
from combined
group by executor, hour_utc
order by executor, hour_utc
"""


def run() -> int:
    ensure_namespace("task5_active_arb_mozila", 1000.0)
    client = DuneClient()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    top50 = load_top50()
    addrs = [r["executor"] for r in top50]
    result["n_addresses"] = len(addrs)

    now = datetime.now(timezone.utc)
    day_end_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_dt = day_end_dt - timedelta(days=1)
    day_start = day_start_dt.strftime("%Y-%m-%d %H:%M:%S")
    day_end = day_end_dt.strftime("%Y-%m-%d %H:%M:%S")

    print("=== A. Байткод / EOA-vs-контракт для топ-50 ===")
    sql_a = build_contract_info_sql(addrs)
    qid_a = client.create_query("task5_op_contract_info", sql_a)
    df_a = client.run_sql_cached("task5_op_contract_info", sql_a, query_id=qid_a,
                                  estimated_credits=20.0, expected_max_rows=60, expected_columns=6)
    contract_rows = df_a.to_dict("records") if df_a is not None else []
    result["contract_info"] = contract_rows
    print(f"  строк: {len(contract_rows)}")

    print("\n=== B. Первый входящий перевод (источник фондирования) для топ-50 ===")
    sql_b = build_first_incoming_sql(addrs)
    qid_b = client.create_query("task5_op_first_incoming", sql_b)
    df_b = client.run_sql_cached("task5_op_first_incoming", sql_b, query_id=qid_b,
                                  estimated_credits=30.0, expected_max_rows=60, expected_columns=3)
    funding_rows = df_b.to_dict("records") if df_b is not None else []
    result["funding_info"] = funding_rows
    print(f"  строк: {len(funding_rows)}")

    print("\n=== C. Часовая активность (тот же день) для топ-50 ===")
    sql_c = build_hourly_activity_sql(addrs, day_start, day_end)
    qid_c = client.create_query("task5_op_hourly_activity", sql_c)
    df_c = client.run_sql_cached("task5_op_hourly_activity", sql_c, query_id=qid_c,
                                  estimated_credits=30.0, expected_max_rows=1200, expected_columns=3)
    hourly_rows = df_c.to_dict("records") if df_c is not None else []
    result["hourly_activity_rows"] = len(hourly_rows)
    print(f"  строк: {len(hourly_rows)}")

    # ---- Python-кластеризация в операторов (без доп. трат Dune) ----
    by_code = defaultdict(list)
    for r in contract_rows:
        if r.get("code_hash"):
            by_code[r["code_hash"]].append(r["executor"])
    code_groups = {k: v for k, v in by_code.items() if len(v) > 1}

    by_funder = defaultdict(list)
    for r in funding_rows:
        if r.get("funding_source"):
            by_funder[r["funding_source"]].append(r["executor"])
    funder_groups = {k: v for k, v in by_funder.items() if len(v) > 1}

    # hourly vectors (24-dim) per executor -> cosine similarity clustering (простой union-find по порогу)
    vectors: dict[str, list[float]] = defaultdict(lambda: [0.0] * 24)
    for r in hourly_rows:
        vectors[r["executor"]][int(r["hour_utc"])] = float(r["n_swaps"])

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    HOURLY_SIM_THRESHOLD = 0.98  # высокий порог -- почти идентичный часовой отпечаток, не просто "оба активны днём"
    hourly_pairs = []
    execs_with_vec = list(vectors.keys())
    for i in range(len(execs_with_vec)):
        for j in range(i + 1, len(execs_with_vec)):
            a, b = execs_with_vec[i], execs_with_vec[j]
            sim = cosine(vectors[a], vectors[b])
            if sim >= HOURLY_SIM_THRESHOLD:
                hourly_pairs.append({"a": a, "b": b, "cosine_similarity": sim})

    # Union-find: объединяем в оператора по ЛЮБОМУ из трёх сигналов
    parent = {a: a for a in addrs}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for group in list(code_groups.values()) + list(funder_groups.values()):
        for a in group[1:]:
            union(group[0], a)
    for pair in hourly_pairs:
        union(pair["a"], pair["b"])

    operators: dict[str, list[str]] = defaultdict(list)
    for a in addrs:
        operators[find(a)].append(a)

    profit_by_exec = {r["executor"]: r["total_profit_usd"] for r in top50}
    operator_rows = []
    for root, members in operators.items():
        total_profit = sum(profit_by_exec.get(m, 0.0) for m in members)
        operator_rows.append({"root": root, "members": members, "n_members": len(members),
                               "total_profit_usd": total_profit})
    operator_rows.sort(key=lambda r: -r["total_profit_usd"])

    result["code_match_groups"] = code_groups
    result["funder_match_groups"] = funder_groups
    result["hourly_high_similarity_pairs"] = hourly_pairs
    result["hourly_sim_threshold"] = HOURLY_SIM_THRESHOLD
    result["operators"] = operator_rows
    result["n_raw_addresses"] = len(addrs)
    result["n_operators_after_clustering"] = len(operator_rows)

    if len(operator_rows) >= 2:
        pos_2_10 = operator_rows[1:10]
        result["operator_positions_2_10_sum_usd"] = sum(r["total_profit_usd"] for r in pos_2_10)
    else:
        result["operator_positions_2_10_sum_usd"] = 0.0

    print(f"\n[operators] адресов -> операторов: {len(addrs)} -> {len(operator_rows)}")
    print(f"[operators] группы по байткоду: {len(code_groups)}, по фондированию: {len(funder_groups)}, "
          f"пар по часовой активности (>= {HOURLY_SIM_THRESHOLD}): {len(hourly_pairs)}")
    print(f"[operators] позиции 2-10 по операторам: ${result['operator_positions_2_10_sum_usd']:,.2f}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    result["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[operators] итого: {total_cost:.4f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
