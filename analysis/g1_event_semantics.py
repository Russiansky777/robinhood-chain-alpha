#!/usr/bin/env python3
"""Sprint G1 -- владелец, 2026-09-01: проверка семантики события после
аномалии масштаба (266 221 "градуаций" за 6 недель). Один агрегатный
запрос (см. sql/g1/g1_event_semantics.sql): счётчики TokenLaunched vs
TokenDeployed + распределение задержки launched_at - deployed_at на
токен. Наружу -- ОДНА строка (агрегат), не построчная выгрузка.

Использование: python analysis/g1_event_semantics.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintG1")
sys.path.insert(0, str(Path(__file__).parent))

from dune_client import DuneClient
from run_pipeline import read_sql


def main() -> int:
    client = DuneClient()
    sql = read_sql("g1/g1_event_semantics")
    print("\n===== g1_event_semantics (оценка 15.0) =====")
    qid = client.create_query("g1_event_semantics", sql)
    df = client.run_sql_cached(
        "g1_event_semantics", sql, query_id=qid, estimated_credits=15.0,
        expected_max_rows=5, expected_columns=10,
    )
    if df is None or len(df) == 0:
        print("[g1_event_semantics] СТОП: пустой результат -- неожиданно для агрегатного запроса.")
        return 1
    row = df.iloc[0]
    print(df.to_string())

    n_launched = int(row["n_launched_tokens"])
    n_deployed = int(row["n_deployed_tokens"])
    n_both = int(row["n_both"])
    median_delay = row["median_delay_s"]

    print(f"\n[g1_event_semantics] n_launched_tokens={n_launched}, n_deployed_tokens={n_deployed}, "
          f"n_both={n_both}, n_launched_no_deploy_seen={int(row['n_launched_no_deploy_seen'])}")
    print(f"[g1_event_semantics] Задержка deploy->launch (сек): "
          f"min={row['min_delay_s']}, p10={row['p10_delay_s']}, median={median_delay}, "
          f"p90={row['p90_delay_s']}, max={row['max_delay_s']} (n={int(row['n_delay_samples'])})")

    if n_deployed < n_launched:
        print(
            "\n[g1_event_semantics] ПОДОЗРЕНИЕ ПОДТВЕРЖДЕНО: TokenDeployed < TokenLaunched -- "
            "topic0, вероятно, перепутаны (деплой должен предшествовать/покрывать запуск). "
            "НЕ продолжаю дальше -- нужен разбор вручную."
        )
        return 1

    if median_delay is not None and float(median_delay) < 60:
        print(
            f"\n[g1_event_semantics] ПОДТВЕРЖДЕНО: медианная задержка {median_delay} с -- "
            "мгновенные запуски, не настоящие бондинг-кривые. Это меняет интерпретацию "
            "объёма (266K), не обязательно детекцию события."
        )
    else:
        print(f"\n[g1_event_semantics] Медианная задержка {median_delay} с -- похоже на "
              "настоящие бондинг-кривые, не мгновенный спам.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
