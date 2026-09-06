#!/usr/bin/env python3
"""Форензика fomo, 2026-09-06 -- владелец: повторить шаг 1 по `tx_from`,
не по `taker`. Ожидание владельца: объём вырастет на порядки, потому
что свопы через роутер записывают кошелёк в tx_from (реальный
инициатор транзакции), а не в taker (может быть адресом роутера,
как показал структурный анализ пула 0x8366a39c... на предыдущем шаге).
Те же агрегаты: group by blockchain/месяц, 90 дней, только агрегаты,
БЕЗ фильтра по сети (гейт dex.trades+blockchain='robinhood' не должен
сработать -- текста 'robinhood' в WHERE нет)."""
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

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_wallets_step1_txfrom_result.json")
NAMESPACE_BUDGET = 350.0

WALLETS = {
    "unipcs": "0x0a6EBEd0155EDB4b21D92AD02897A626CD90119E",
    "ogle": "0x1Bcc5f67CD17e13770F199fA03bC043b0cde1143",
    "avast": "0xcc0C581613DFd4ACe7c8686668427236f8BD5cC5",
    "frogman": "0x14AA2A71dbb5eF87b81F92205E2699AA4aa65794",
    "DumbCrayonEater": "0x8f62a08537cede87d511aca6436274ab4ca080a3",
    "vee": "0xa0670863bd5cd0d60022bab2eed78e81e1a06bce",
}
WALLETS_LOWER_NO_0X = [a[2:].lower() for a in WALLETS.values()]


def spent_so_far(client: DuneClient, prefix: str) -> float:
    return sum(e.get("credits") or 0.0 for e in client.credit_ledger if str(e.get("name", "")).startswith(prefix))


def run() -> int:
    ensure_namespace("fomo_forensics_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[wallets_txfrom] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()
    addrs_sql = ", ".join(f"'{a}'" for a in WALLETS_LOWER_NO_0X)

    sql_agg = f"""select blockchain, date_trunc('month', block_time) as month,
    count(*) as n_trades,
    count(distinct token_bought_address) as n_distinct_bought_tokens,
    count(distinct token_sold_address) as n_distinct_sold_tokens,
    coalesce(sum(amount_usd), 0) as total_amount_usd
from dex.trades
where block_time >= now() - interval '90' day
    and lower(to_hex(tx_from)) in ({addrs_sql})
group by blockchain, date_trunc('month', block_time)
order by blockchain, month"""

    print("[wallets_txfrom] SQL (фильтр по tx_from, не taker):")
    print(sql_agg)
    print("[wallets_txfrom] обоснование оценки до запуска: та же структура запроса, что уже реально "
          "стоила 14.81 кредита с фильтром по taker (fomo_wallets_step1_volume_by_chain_month) -- "
          "оценка владельца ~15, беру ту же цифру.")

    qid = client.create_query("fomo_wallets_step1_txfrom_volume_by_chain_month", sql_agg)
    df = client.run_sql_cached("fomo_wallets_step1_txfrom_volume_by_chain_month", sql_agg, query_id=qid,
                                estimated_credits=15.0, expected_max_rows=500, expected_columns=6)
    cost = next((e["credits"] for e in reversed(client.credit_ledger)
                 if e["name"] == "fomo_wallets_step1_txfrom_volume_by_chain_month"), None)
    print(f"[wallets_txfrom] РЕАЛЬНАЯ фактическая стоимость: {cost}")

    rows = df.to_dict("records") if df is not None else []
    print(f"\n[wallets_txfrom] реальные агрегаты (по сети/месяцу):")
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
    print(f"\n[wallets_txfrom] сводка по сетям за 90 дней (реальные суммы; n_distinct_*_tokens_max_month -- "
          f"максимум по одному месяцу, не точная уникальность за весь период):")
    for chain, agg in sorted(by_chain.items(), key=lambda kv: -kv[1]["n_trades"]):
        print(f"  {chain}: {agg}")

    grand_total_usd = sum(a["total_amount_usd"] for a in by_chain.values())
    grand_total_trades = sum(a["n_trades"] for a in by_chain.values())
    print(f"\n[wallets_txfrom] ИТОГО по всем сетям за 90 дней: {grand_total_trades} сделок, ${grand_total_usd:,.2f}")

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "addr_col_used": "tx_from",
        "actual_cost_credits": cost,
        "results_by_chain_month": rows,
        "by_chain_90d_totals": by_chain,
        "grand_total_trades_90d": grand_total_trades,
        "grand_total_amount_usd_90d": grand_total_usd,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[wallets_txfrom] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
