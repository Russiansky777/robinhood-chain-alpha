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

# Все SQL-тексты Sprint G1 живут как файлы в sql/g1/ (не только внутри
# Dune) -- требование владельца 2026-09-01. read_sql() уже умеет
# подпути (SQL_DIR / f"{name}.sql").
PROBE_SCHEMAS = read_sql("g1/g1_schemas_like_robinhood_distinct")
PROBE_PROJECTS_JULY_TMPL = read_sql("g1/g1_dex_trades_projects_july")
PROBE_POOL_BIRTHS_JULY_TMPL = read_sql("g1/g1_pool_births_daily_july")
PROBE_RECENT_DAY = read_sql("g1/g1_recent_day_coverage_probe")


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
