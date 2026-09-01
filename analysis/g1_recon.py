#!/usr/bin/env python3
"""Sprint G1, Шаг 1: разведка (лимит этапа <=20 кредитов, см.
docs/G1_DESIGN.md). НЕ меняет §2 (заморожен) -- только собирает факты
для подраздела "Механика детекции" и грубую оценку N_total (точный счёт
-- задача Шага 3, полностью бюджетированного отдельно).

ВАЖНО про стоимость: свежее сканирование dex.trades БЕЗ фильтра по
узкому окну дат стоит как 02_swaps_raw_july в Sprint 1 (~100-125
кредитов за месяц, см. docs/COST_POSTMORTEM.md) -- на порядок больше
бюджета этого шага. Поэтому:
  - Июльская часть разведки идёт ЧЕРЕЗ query_02_swaps_raw_july (уже
    материализован в Sprint 1, query_id из data/query_ids_recovered.json,
    require_cached=True) -- сам скан бесплатен, платится только
    агрегация поверх него.
  - Для проверки покрытия за пределами июля -- УЗКОЕ (заведомо
    ограниченное) окно последних ~10 дней, не весь август целиком.
  - Полный посуточный счёт за ВЕСЬ период G1 -- задача Шага 3
    (партиционирование по неделям, отдельный бюджет ≤200).

Использование: python analysis/g1_recon.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintG1")
sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG
from dune_client import DuneClient, render_sql
from run_pipeline import read_sql, q_ts

PROBE_SCHEMAS = """
select table_schema, count(*) as n_tables
from information_schema.tables
where table_schema like '%robinhood%'
group by 1
order by 1
"""

# Через query_02 (уже материализован, скан бесплатен) -- разбивка по
# project/version за июль: покажет ВСЕ протоколы, активные на чейне, не
# только uniswap -- нужно, чтобы не пропустить собственный AMM
# pons.family, если он тегируется иначе.
PROBE_PROJECTS_JULY_TMPL = """
select project, version, count(*) as n_swaps,
    count(distinct pool_address) as n_pools,
    min(block_time) as first_seen, max(block_time) as last_seen
from query_02_swaps_raw_july
group by 1, 2
order by n_swaps desc
limit 50
"""

# Через query_02 -- "рождения" пулов (min block_time на pool_address) по
# дням, ТОЛЬКО uniswap v3/v4 (рабочая гипотеза механики детекции: на
# молодом чейне появление нового Uniswap-подобного пула == миграция
# ликвидности после градуации bonding curve). Если распределение по дням
# и число пулов неправдоподобны для темпа лаунчпада -- гипотеза неверна,
# нужен другой сигнал (см. вывод и распредление project/version выше).
PROBE_POOL_BIRTHS_JULY_TMPL = """
with swaps as (
    select pool_address, block_time
    from query_02_swaps_raw_july
    where project = 'uniswap' and version in ('3', '4')
),
births as (
    select pool_address, min(block_time) as pool_birth_time
    from swaps
    group by 1
)
select date_trunc('day', pool_birth_time) as day, count(*) as n_new_pools
from births
group by 1
order by 1
"""

# ОДИН день (не окно!) -- 2026-08-30, тот же принцип, что смоук-тест
# Sprint 1 (один день стоил ~13-14 кредитов на полный скан dex.trades с
# фильтром по чейну и дате -- стоимость масштабируется примерно линейно
# по числу дней, см. docs/COST_POSTMORTEM.md, так что окно даже в 10
# дней стоило бы ~130-140, далеко за бюджетом этого шага). Дата выбрана
# как уже известная содержательная точка (см. docs/PROJECT_STATE.md:
# "~51% объёма сети на pons.family на 30.08.2026") -- не наугад. Даёт
# (а) подтверждение, что покрытие доходит почти до текущей даты
# симуляции, (б) одну точку недавнего темпа для грубой экстраполяции
# N_total. Точная дата конца периода фиксируется отдельно, по
# max(block_time) в этом же результате -- не по исходам цен.
PROBE_RECENT_DAY = """
with swaps as (
    select project_contract_address as pool_address, block_time
    from dex.trades
    where blockchain = 'robinhood'
        and project = 'uniswap' and version in ('3', '4')
        and block_time >= timestamp '2026-08-30 00:00:00'
        and block_time <  timestamp '2026-08-31 00:00:00'
),
births as (
    select pool_address, min(block_time) as pool_birth_time
    from swaps
    group by 1
)
select count(*) as n_new_pools_this_day,
    (select max(block_time) from swaps) as coverage_probe_max_block_time
from births
"""


def main() -> int:
    client = DuneClient()

    print("== Pre-flight: query_id для 02_swaps_raw_july (без нового execute) ==")
    query_ids = {
        "02_swaps_raw_july": client.create_query(
            "02_swaps_raw_july",
            render_sql(read_sql("02_swaps_raw_july"), {"start_date": q_ts(CONFIG.train_start), "end_date": q_ts(CONFIG.train_end)}),
            require_cached=True,
        )
    }
    from run_pipeline import substitute_query_refs

    def run(name: str, sql_template: str, est: float, max_rows: int, max_cols: int):
        sql = substitute_query_refs(sql_template, query_ids)
        print(f"\n===== {name} (оценка {est:.1f}) =====")
        qid = client.create_query(name, sql)
        df = client.run_sql_cached(
            name, sql, query_id=qid, estimated_credits=est,
            expected_max_rows=max_rows, expected_columns=max_cols,
        )
        print(df.to_string(max_rows=300) if df is not None else "(no rows)")
        return df

    run("g1_schemas_like_robinhood_distinct", PROBE_SCHEMAS, 6.0, 100, 2)
    run("g1_dex_trades_projects_july", PROBE_PROJECTS_JULY_TMPL, 2.0, 50, 6)
    run("g1_pool_births_daily_july", PROBE_POOL_BIRTHS_JULY_TMPL, 2.0, 40, 2)
    run("g1_recent_day_coverage_probe", PROBE_RECENT_DAY, 15.0, 2, 2)

    print("\n[g1_recon] Готово. См. вывод выше -- обновите docs/G1_DESIGN.md, "
          "\"Механика детекции\", по фактам, и зафиксируйте g1_period_end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
