#!/usr/bin/env python3
"""Задача 1, проверка 1 (владелец, 2026-09-05, "Ликвидность... TVL >
$200k на момент окна... стоимость round-trip на $500"), шаг 0 --
разведка ДО расчёта: что реально есть в Dune для реконструкции TVL/
резервов пула НА МОМЕНТ прошедших выходных (не гадаем, есть ли готовая
историческая таблица резервов, или придётся идти через ончейн-вызов на
историческом блоке / текущий GT-снимок как явно помеченное приближение).

Три вещи проверяем, все дёшево (метаданные + маленькие лимиты):
1. Полный список колонок dex.trades (не только те, что уже
   использовались в Задаче 1) -- есть ли адрес пула/проекта.
2. Список схем/таблиц Dune со словами pool/reserve/liquidity в имени,
   относящихся к robinhood chain -- есть ли готовая история резервов.
3. Если найдётся project_contract_address в dex.trades -- реальный
   пример нескольких строк для одного из тикеров Задачи 1, чтобы
   увидеть, есть ли там что-то полезное для TVL (например, отдельные
   таблицы `project.pool_id` reserves)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task1_liquidity_probe_result.json")


def run() -> int:
    client = DuneClient()
    out = {}

    sql1 = """select column_name, data_type, ordinal_position
from information_schema.columns
where table_schema='dex' and table_name='trades'
order by ordinal_position"""
    qid1 = client.create_query("task1_full_dex_trades_columns", sql1)
    df1 = client.run_sql_cached("task1_full_dex_trades_columns", sql1, query_id=qid1,
                                 estimated_credits=0.05, expected_max_rows=60, expected_columns=3)
    out["dex_trades_all_columns"] = df1.to_dict("records") if df1 is not None else None
    print("[probe] dex.trades колонки:", [r["column_name"] for r in out["dex_trades_all_columns"]] if df1 is not None else None)

    sql2 = """select table_schema, table_name
from information_schema.tables
where table_name ilike '%pool%' or table_name ilike '%reserve%' or table_name ilike '%liquidit%'
order by table_schema, table_name
limit 200"""
    qid2 = client.create_query("task1_pool_reserve_tables_search", sql2)
    df2 = client.run_sql_cached("task1_pool_reserve_tables_search", sql2, query_id=qid2,
                                 estimated_credits=0.5, expected_max_rows=250, expected_columns=2)
    out["pool_reserve_tables"] = df2.to_dict("records") if df2 is not None else None
    print(f"[probe] найдено таблиц с pool/reserve/liquidit в имени: {len(out['pool_reserve_tables']) if df2 is not None else 0}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[probe] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
