#!/usr/bin/env python3
"""Задача 5 -- проверка гипотезы владельца (2026-09-11): формула
net-flow по WETH/USDG слепа к остальным токенам -- в транзакции с
8-13+ плечами часть свопов может идти по несвязанным токенам, и
формула видит односторонний приток WETH без соответствующего оттока.
Честная арбитражная транзакция должна обнулять net-flow по ВСЕМ
токенам, кроме одного (входного/выходного актива).

Точечный lookup (НЕ скан диапазона дат) по ОДНОМУ конкретному tx_hash:
  - 0xf342eb74...d73f8 (B8F305F27CCC..., 22 плеча, 9 пулов, $885k)
    -- главный кандидат гипотезы владельца.

ВАЖНО (реальная, только что обнаруженная ошибка в оценке стоимости,
исправлено 2026-09-11): предыдущая версия скрипта пыталась сначала
найти tx_hash 2-легового выброса ($116k) через SELECT со ссылкой на
уже материализованный query_8673700, посчитав это "почти бесплатным
чтением кэша". Это оказалось НЕВЕРНО -- ссылка на query_<id> в новом
запросе на Dune ПЕРЕИСПОЛНЯЕТ всю логику исходного запроса заново, а
не читает готовый результат. Реальная стоимость составила 25.86
кредита вместо оценённых 1.0, что вызвало срабатывание
check_overrun_after_execute() (actual > 2x estimate AND actual >= 25.0)
и остановило скрипт ДО того, как он дошёл до настоящего point-lookup
по сырым таблицам. Контрольная 2-леговая проверка убрана полностью
(инструкция владельца: "кредиты не тратить сверх точечного SELECT по
уже оплаченному query_id" -- сам этот дозапрос НЕ был точечным дешёвым
чтением, а был вторым таким же дорогим переисполнением). Остаётся
только настоящий point-lookup по сырым базовым таблицам
(uniswap_v3_robinhood.uniswapv3pool_evt_swap /
uniswap_v4_robinhood.swaps), отфильтрованный точным tx_hash -- это
честный узкий WHERE на базовой таблице, а не ре-исполнение сложного
CTE-запроса, и должен быть действительно дёшев."""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task5_active_arb_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task5_active_arb_diag_full_legs_result.json")
CANONICAL_FACTORY = "1f7d7550b1b028f7571e69a784071f0205fd2efa"
# ПРИМЕЧАНИЕ: query_id больше НЕ используется в этом скрипте -- ссылка
# на query_<id> переисполняет всю логику заново (реальная стоимость
# 25.86 кредита вместо ожидаемых ~1, подтверждено 2026-09-11). Ниже --
# только point-lookup по сырым таблицам, отфильтрованный точным tx_hash.

# Реальные tx_hash из предыдущей диагностики (task5_active_arb_diag_single_tx_result.json)
TARGET_TXS = {
    "B8F305F27CCC_22legs_885k": "f342eb747f97793c8a561cb628f84c13acb76215f083c148dcc3f77e221d73f8",
}


def build_sql(tx_hash_hex: str) -> str:
    return f"""
select 'v3' as version, to_hex(s.contract_address) as pool, to_hex(cp.token0) as token0, to_hex(cp.token1) as token1,
       cast(s.amount0 as varchar) as amount0, cast(s.amount1 as varchar) as amount1,
       cast(null as varchar) as token_bought, cast(null as varchar) as token_sold,
       cast(null as varchar) as amount_bought, cast(null as varchar) as amount_sold,
       s.evt_index as leg_index
from uniswap_v3_robinhood.uniswapv3pool_evt_swap s
left join uniswap_v3_robinhood.uniswapv3factory_evt_poolcreated cp on cp.pool = s.contract_address and cp.contract_address = from_hex('{CANONICAL_FACTORY}')
where s.evt_tx_hash = from_hex('{tx_hash_hex}')
union all
select 'v4' as version, concat(to_hex(w.token_bought_address), '-', to_hex(w.token_sold_address)) as pool,
       cast(null as varchar) as token0, cast(null as varchar) as token1,
       cast(null as varchar) as amount0, cast(null as varchar) as amount1,
       to_hex(w.token_bought_address) as token_bought, to_hex(w.token_sold_address) as token_sold,
       cast(w.token_bought_amount_raw as varchar) as amount_bought, cast(w.token_sold_amount_raw as varchar) as amount_sold,
       w.evt_index as leg_index
from uniswap_v4_robinhood.swaps w
where w.tx_hash = from_hex('{tx_hash_hex}')
order by leg_index
"""


def run() -> int:
    ensure_namespace("task5_active_arb_mozila", 400.0)
    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "transactions": {}}

    for label, tx_hash in TARGET_TXS.items():
        print(f"\n=== {label} (tx=0x{tx_hash}) ===")
        sql = build_sql(tx_hash)
        qid = client.create_query(f"task5_diag_fulllegs_{label}"[:100], sql)
        df = client.run_sql_cached(f"task5_diag_fulllegs_{label}"[:100], sql, query_id=qid,
                                    estimated_credits=3.0, expected_max_rows=100, expected_columns=11)
        rows = df.to_dict("records") if df is not None else []
        print(f"  найдено {len(rows)} плечей")
        for r in rows:
            print(f"   {r}")

        # Считаем net-flow по КАЖДОМУ токену отдельно (raw units, без decimals -- честно, не гадаем decimals).
        net_flow: dict[str, float] = defaultdict(float)
        for r in rows:
            if r.get("version") == "v3":
                t0, t1 = r.get("token0"), r.get("token1")
                a0, a1 = r.get("amount0"), r.get("amount1")
                if t0 and a0 is not None:
                    net_flow[t0] += -float(a0)  # с точки зрения трейдера -- минус дельты пула
                if t1 and a1 is not None:
                    net_flow[t1] += -float(a1)
            elif r.get("version") == "v4":
                tb, ts = r.get("token_bought"), r.get("token_sold")
                ab, as_ = r.get("amount_bought"), r.get("amount_sold")
                if tb and ab is not None:
                    net_flow[tb] += float(ab)
                if ts and as_ is not None:
                    net_flow[ts] += -float(as_)

        out["transactions"][label] = {
            "tx_hash": f"0x{tx_hash}", "n_legs_found": len(rows), "raw_legs": rows,
            "net_flow_by_token_raw_units": dict(net_flow),
        }
        print(f"  net-flow по токенам (raw units, знак с точки зрения трейдера):")
        for tok, flow in net_flow.items():
            print(f"    {tok}: {flow:,.0f}")

    total_cost = sum(e.get("credits") or 0.0 for e in client.credit_ledger)
    out["total_cost_credits"] = total_cost
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[diag_full_legs] итого: {total_cost:.4f}, записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
