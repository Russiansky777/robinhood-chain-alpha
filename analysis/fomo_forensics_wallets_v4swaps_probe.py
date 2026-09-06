#!/usr/bin/env python3
"""Форензика fomo, последний запрос перед парковкой -- владелец, потолок
15 кредитов. `uniswap_v4_robinhood.swaps`, фильтр taker OR maker IN
(шесть кошельков). Сначала 1 день (оценка стоимости), потом 30 дней,
если в пределах потолка.

Реальная колонки таблицы (уже проверено ранее,
fomo_forensics_uniswap_v4_schema_check_result.json): blockchain,
project, version, block_month, block_date, block_time, block_number,
token_bought_amount_raw, token_sold_amount_raw, token_bought_address,
token_sold_address, taker, maker, project_contract_address, tx_hash,
evt_index, sender, hooks, fee, liquidity, sqrtpricex96, tick,
call_trace_address. **НЕТ колонки `amount_usd`** -- эта таблица хранит
СЫРЫЕ (raw, до применения decimals и цены) объёмы, не долларовые.
Не выдумываем долларовый агрегат из того, чего нет -- считаем то, что
реально есть (число свопов, число уникальных токенов), и явно
отмечаем отсутствие $ для честного решения владельца."""
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

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_wallets_v4swaps_probe_result.json")
NAMESPACE_BUDGET = 350.0
HARD_CAP = 15.0  # владелец: потолок на этот последний запрос

WALLETS = {
    "unipcs": "0x0a6EBEd0155EDB4b21D92AD02897A626CD90119E",
    "ogle": "0x1Bcc5f67CD17e13770F199fA03bC043b0cde1143",
    "avast": "0xcc0C581613DFd4ACe7c8686668427236f8BD5cC5",
    "frogman": "0x14AA2A71dbb5eF87b81F92205E2699AA4aa65794",
    "DumbCrayonEater": "0x8f62a08537cede87d511aca6436274ab4ca080a3",
    "vee": "0xa0670863bd5cd0d60022bab2eed78e81e1a06bce",
}
WALLETS_LOWER_NO_0X = {name: a[2:].lower() for name, a in WALLETS.items()}


def spent_so_far(client: DuneClient, prefix: str) -> float:
    return sum(e.get("credits") or 0.0 for e in client.credit_ledger if str(e.get("name", "")).startswith(prefix))


def build_sql(addrs_case_sql: str, addrs_in_sql: str, interval_days: int) -> str:
    return f"""select {addrs_case_sql} as wallet,
    count(*) as n_swaps,
    count(distinct token_bought_address) as n_distinct_bought_tokens,
    count(distinct token_sold_address) as n_distinct_sold_tokens
from uniswap_v4_robinhood.swaps
where block_time >= now() - interval '{interval_days}' day
    and (lower(to_hex(taker)) in ({addrs_in_sql}) or lower(to_hex(maker)) in ({addrs_in_sql}))
group by {addrs_case_sql}
order by n_swaps desc"""


def run() -> int:
    ensure_namespace("fomo_forensics_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[v4swaps] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")
    print(f"[v4swaps] ВНИМАНИЕ: таблица uniswap_v4_robinhood.swaps не содержит amount_usd -- "
          "долларовый объём из неё честно НЕ считается, только число свопов/уникальных токенов.")

    client = DuneClient()
    addrs_in_sql = ", ".join(f"'{a}'" for a in WALLETS_LOWER_NO_0X.values())
    # CASE, определяющий, КАКОЙ из 6 адресов реально совпал (taker или maker) --
    # чтобы агрегировать "по кошельку", как просил владелец, без UNION ALL.
    case_lines = "\n        ".join(
        f"when lower(to_hex(taker)) = '{a}' or lower(to_hex(maker)) = '{a}' then '{name}'"
        for name, a in WALLETS_LOWER_NO_0X.items()
    )
    addrs_case_sql = f"case\n        {case_lines}\n        else 'unknown'\n    end"

    # --- Этап 1: 1 день, оценка стоимости ---
    sql_1d = build_sql(addrs_case_sql, addrs_in_sql, 1)
    print("[v4swaps] SQL (1 день):")
    print(sql_1d)
    qid1 = client.create_query("fomo_v4swaps_1day", sql_1d)
    df1 = client.run_sql_cached("fomo_v4swaps_1day", sql_1d, query_id=qid1,
                                 estimated_credits=5.0, expected_max_rows=10, expected_columns=4)
    cost1 = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == "fomo_v4swaps_1day"), None)
    rows1 = df1.to_dict("records") if df1 is not None else []
    print(f"[v4swaps] РЕАЛЬНО (1 день): стоимость={cost1}, строки={rows1}")

    out: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note_no_amount_usd": "uniswap_v4_robinhood.swaps не содержит amount_usd -- долларовый объём честно не посчитан",
        "step_1day": {"cost": cost1, "rows": rows1},
    }

    total_so_far = cost1 or 0
    if total_so_far >= HARD_CAP:
        print(f"[v4swaps] СТОП: уже {total_so_far:.2f} >= потолок {HARD_CAP} -- 30-дневный запрос не запускаем.")
        out["step_30day"] = None
        out["stopped_at_hard_cap"] = True
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    # --- Этап 2: 30 дней, только если укладываемся в потолок ---
    sql_30d = build_sql(addrs_case_sql, addrs_in_sql, 30)
    print("\n[v4swaps] SQL (30 дней):")
    print(sql_30d)
    qid2 = client.create_query("fomo_v4swaps_30day", sql_30d)
    df2 = client.run_sql_cached("fomo_v4swaps_30day", sql_30d, query_id=qid2,
                                 estimated_credits=10.0, expected_max_rows=10, expected_columns=4)
    cost2 = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == "fomo_v4swaps_30day"), None)
    rows2 = df2.to_dict("records") if df2 is not None else []
    print(f"[v4swaps] РЕАЛЬНО (30 дней): стоимость={cost2}, строки={rows2}")

    total_all = (cost1 or 0) + (cost2 or 0)
    print(f"\n[v4swaps] суммарная стоимость обоих запросов: {total_all:.2f} (потолок владельца: {HARD_CAP})")

    n_total_swaps = sum(r["n_swaps"] for r in rows2 if r["wallet"] != "unknown")
    print(f"[v4swaps] реальный итог: {n_total_swaps} свопов за 30 дней по всем 6 кошелькам (taker или maker)")
    decision = "источник -- продолжать по спецификации" if n_total_swaps > 0 else "0 -- парковать до 14.09 ОКОНЧАТЕЛЬНО"
    print(f"[v4swaps] решение по правилу владельца: {decision}")

    out["step_30day"] = {"cost": cost2, "rows": rows2}
    out["total_cost_both_steps"] = total_all
    out["n_total_swaps_30d"] = n_total_swaps
    out["decision"] = decision

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[v4swaps] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
