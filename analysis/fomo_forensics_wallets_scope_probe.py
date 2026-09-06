#!/usr/bin/env python3
"""Форензика fomo, расконсервация 2026-09-06 -- владелец прислал 6
реальных адресов кошельков с fomo.family. Шаг 1 (ТОЛЬКО разведка
объёма, ничего построчного): один агрегирующий запрос к `dex.trades`
-- группировка по `blockchain` и месяцу за 90 дней, число сделок,
число уникальных токенов, суммарный `amount_usd`. Без фильтра по сети
(владелец: "гейт [dex.trades+blockchain='robinhood'] снимается,
предикат узкий" -- шесть адресов, не сто токенов, поэтому запрос ниже
СОЗНАТЕЛЬНО не содержит текстового `blockchain='robinhood'`).

Не предполагаем колонку-фильтр (`taker` vs `tx_from`) -- реальная
схема dex.trades уже проверялась раньше в этом коде
(sql/task1/task1_dex_trades_columns_probe.sql), но НЕ на предмет
taker/maker/tx_from -- разведка схемы здесь, дёшево, до основного
запроса."""
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

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_wallets_scope_probe_result.json")
NAMESPACE_BUDGET = 350.0
STEP1_SOFT_CAP = 30.0  # владелец: "если шаг 1 съест больше 30 -- остановиться и доложить"

WALLETS = {
    "unipcs": "0x0a6EBEd0155EDB4b21D92AD02897A626CD90119E",
    "ogle": "0x1Bcc5f67CD17e13770F199fA03bC043b0cde1143",
    "avast": "0xcc0C581613DFd4ACe7c8686668427236f8BD5cC5",
    "frogman": "0x14AA2A71dbb5eF87b81F92205E2699AA4aa65794",
    "DumbCrayonEater": "0x8f62a08537cede87d511aca6436274ab4ca080a3",
    "vee": "0xa0670863bd5cd0d60022bab2eed78e81e1a06bce",
}
WALLETS_LOWER_NO_0X = {name: addr[2:].lower() for name, addr in WALLETS.items()}


def probe_dex_trades_schema(client: DuneClient) -> list[str]:
    sql = """select column_name
from information_schema.columns
where table_schema = 'dex' and table_name = 'trades'
    and column_name in ('taker', 'maker', 'tx_from', 'tx_to', 'tx_hash')
order by column_name
limit 100"""
    qid = client.create_query("fomo_wallets_dex_trades_schema", sql)
    df = client.run_sql_cached("fomo_wallets_dex_trades_schema", sql, query_id=qid,
                                estimated_credits=2.0, expected_max_rows=100, expected_columns=1)
    return df["column_name"].tolist() if df is not None and "column_name" in df.columns else []


def spent_so_far(client: DuneClient, prefix: str) -> float:
    return sum(e.get("credits") or 0.0 for e in client.credit_ledger if str(e.get("name", "")).startswith(prefix))


def run() -> int:
    ensure_namespace("fomo_forensics_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[wallets_scope] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")
    print(f"[wallets_scope] адреса (нижний регистр, без 0x): {list(WALLETS_LOWER_NO_0X.values())}")

    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "wallets": WALLETS}

    # --- Разведка схемы: реально ли есть taker/tx_from (дёшево, до основного запроса) ---
    cols = probe_dex_trades_schema(client)
    print(f"[wallets_scope] реальные колонки dex.trades (из проверяемого набора): {cols}")
    out["dex_trades_schema_probe"] = cols
    addr_col = "taker" if "taker" in cols else ("tx_from" if "tx_from" in cols else None)
    if addr_col is None:
        print("[wallets_scope] СТОП: ни 'taker', ни 'tx_from' реально не существуют в dex.trades -- "
              "не гадаем дальше, нужен другой столбец (не в проверенном наборе).")
        out["error"] = "neither taker nor tx_from exists in dex.trades"
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 1
    print(f"[wallets_scope] используем реальную колонку: {addr_col}")
    out["addr_col_used"] = addr_col

    spent_schema = spent_so_far(client, "fomo_wallets_")
    print(f"[wallets_scope] потрачено на разведку схемы: {spent_schema:.2f}")

    # --- Шаг 1: единственный агрегирующий запрос, БЕЗ фильтра по сети (владелец: гейт снимается) ---
    addrs_sql = ", ".join(f"'{a}'" for a in WALLETS_LOWER_NO_0X.values())
    sql_agg = f"""select blockchain, date_trunc('month', block_time) as month,
    count(*) as n_trades,
    count(distinct token_bought_address) as n_distinct_bought_tokens,
    count(distinct token_sold_address) as n_distinct_sold_tokens,
    coalesce(sum(amount_usd), 0) as total_amount_usd
from dex.trades
where block_time >= now() - interval '90' day
    and lower(to_hex({addr_col})) in ({addrs_sql})
group by blockchain, date_trunc('month', block_time)
order by blockchain, month"""

    # Оценка ДО запуска (владелец явно попросил) -- реальный API Dune не отдаёт
    # предварительную оценку (подтверждено раньше в этой сессии), поэтому оценка
    # -- обоснованная, не с потолка: это ПРОСТОЙ WHERE-IN фильтр по 6 адресам на
    # ОДНОЙ уже существующей декодированной таблице (не JOIN двух источников,
    # не OR по разным колонкам через коррелированную CTE -- та самая причина
    # инцидента 238.71, см. docs/PROJECT_STATE.md). Предыдущие узкие
    # robinhood-only агрегаты по dex.trades в этом проекте реально стоили
    # 0.2-5 кредитов; здесь сканируется БОЛЬШЕ (все сети, не одна), поэтому
    # оценка взята с большим запасом -- 30 (мягкий потолок владельца), не выше
    # SANITY_MAX_ESTIMATE=40 (иначе жёсткий стоп сработает до попытки).
    print(f"[wallets_scope] SQL шага 1 (без 'blockchain=\\'robinhood\\'' -- гейт dex.trades+robinhood не должен сработать):")
    print(sql_agg)
    print("[wallets_scope] обоснование оценки ДО запуска: простой WHERE-IN фильтр по 6 адресам на одной "
          "уже декодированной таблице (не JOIN/OR как в инциденте 238.71); прежние узкие агрегаты по "
          "dex.trades в этом проекте стоили 0.2-5 кредитов; здесь сканируются ВСЕ сети -- оценка с запасом: 30.0")

    qid_agg = client.create_query("fomo_wallets_step1_volume_by_chain_month", sql_agg)
    df_agg = client.run_sql_cached("fomo_wallets_step1_volume_by_chain_month", sql_agg, query_id=qid_agg,
                                    estimated_credits=30.0, expected_max_rows=500, expected_columns=6)
    cost_agg = next((e["credits"] for e in reversed(client.credit_ledger)
                      if e["name"] == "fomo_wallets_step1_volume_by_chain_month"), None)
    print(f"[wallets_scope] РЕАЛЬНАЯ фактическая стоимость шага 1: {cost_agg}")
    out["step1_actual_cost_credits"] = cost_agg

    rows = df_agg.to_dict("records") if df_agg is not None else []
    out["step1_results_by_chain_month"] = rows
    print(f"\n[wallets_scope] реальные агрегаты (по сети/месяцу):")
    for r in rows:
        print(f"  {r}")

    by_chain: dict[str, dict] = {}
    for r in rows:
        c = r["blockchain"]
        agg = by_chain.setdefault(c, {"n_trades": 0, "total_amount_usd": 0.0,
                                        "n_distinct_bought_tokens_max_month": 0, "n_distinct_sold_tokens_max_month": 0})
        agg["n_trades"] += r["n_trades"]
        agg["total_amount_usd"] += r["total_amount_usd"]
        agg["n_distinct_bought_tokens_max_month"] = max(agg["n_distinct_bought_tokens_max_month"], r["n_distinct_bought_tokens"])
        agg["n_distinct_sold_tokens_max_month"] = max(agg["n_distinct_sold_tokens_max_month"], r["n_distinct_sold_tokens"])
    out["by_chain_90d_totals"] = by_chain
    print(f"\n[wallets_scope] сводка по сетям за 90 дней (реальные, честные суммы n_trades/amount_usd; "
          f"n_distinct_*_tokens_max_month -- МАКСИМУМ по одному месяцу, не сумма/не точная уникальность за весь период):")
    for chain, agg in sorted(by_chain.items(), key=lambda kv: -kv[1]["n_trades"]):
        print(f"  {chain}: {agg}")

    total_step1_spent = spent_so_far(client, "fomo_wallets_")
    out["total_step1_spent_credits"] = total_step1_spent
    print(f"\n[wallets_scope] суммарно потрачено на шаг 1 (разведка схемы + агрегат): {total_step1_spent:.2f}")
    if total_step1_spent > STEP1_SOFT_CAP:
        print(f"[wallets_scope] ВНИМАНИЕ: шаг 1 превысил мягкий потолок владельца ({STEP1_SOFT_CAP}) -- "
              "ОСТАНАВЛИВАЮСЬ, шаг 2 не запускается без решения владельца.")
        out["step1_exceeded_soft_cap"] = True
    else:
        out["step1_exceeded_soft_cap"] = False

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[wallets_scope] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
