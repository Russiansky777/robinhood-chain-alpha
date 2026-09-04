#!/usr/bin/env python3
"""Скринер пулов -- wash_share для 2 РЕАЛЬНЫХ (не 3 -- см. находку
2026-09-04: DefiLlama задваивает Aerodrome Slipstream под метками
'aerodrome-slipstream' И 'uniswap-v3', оба -- ОДИН физический адрес
0x7aea2e8a...) прошедших порог пулов на Base:
  - USDC-CBBTC: 0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb (выброс, ratio=31.57)
  - WETH-CBBTC: 0x7aea2e8a3843516afa07293a10ac8e49906dabd1 (погранично, ratio~2.0)

Реальные схемы Dune для Base (найдено analysis/dune_base_schema_discovery.py,
2026-09-04): uniswap_v3_base, aerodrome_slipstream_base. Тот же метод, что
дал 0x65050a на Robinhood Chain (analysis/dune_pool_volume_query1.py) --
топ-sender/recipient по объёму, доля топ-3, признак самоторговли."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "funding_mozila_block2")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace, load_state
from dune_client import DuneClient

OUT_PATH = Path("data/p3_guard_cache/dune_base_wash_share_result.json")

POOLS = {
    "USDC-CBBTC": "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb",
    "WETH-CBBTC": "0x7aea2e8a3843516afa07293a10ac8e49906dabd1",
}
CANDIDATE_SCHEMAS = ["uniswap_v3_base", "aerodrome_slipstream_base"]


def q(client: DuneClient, name: str, sql: str, estimated_credits: float,
      expected_max_rows: int = 100, expected_columns: int = 10) -> dict:
    try:
        df = client.run_sql_cached(name=name, sql=sql, estimated_credits=estimated_credits,
                                    expected_max_rows=expected_max_rows, expected_columns=expected_columns)
        rows = df.to_dict(orient="records") if df is not None else None
        print(f"[wash_share] {name}: {len(df) if df is not None else 0} строк")
        if df is not None and len(df):
            print(df.to_string())
        return {"rows": rows, "n_rows": len(df) if df is not None else 0}
    except SystemExit as exc:
        print(f"[wash_share] {name} остановлен гвардом: {exc}")
        return {"stopped": True, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        print(f"[wash_share] {name} УПАЛ: {exc}")
        return {"failed": True, "reason": str(exc)[:2000]}


def run() -> int:
    ensure_namespace("funding_mozila_block2", 250.0)
    client = DuneClient()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": {}}

    # Первая попытка (угаданное имя clpool_evt_swap по аналогии с
    # Robinhood Chain) реально упала -- "Table ... does not exist"
    # (0 кредитов, не оплачено). Реальные имена таблиц НЕ угадываем
    # второй раз -- широкая разведка по обеим схемам сначала.
    print("=== Разведка реальных имён evt_swap-таблиц в uniswap_v3_base и aerodrome_slipstream_base ===")
    discovery_sql = """
    SELECT table_schema, table_name
    FROM information_schema.tables
    WHERE table_schema IN ('uniswap_v3_base', 'aerodrome_slipstream_base')
      AND LOWER(table_name) LIKE '%evt_swap%'
    LIMIT 100
    """
    result["steps"]["table_discovery"] = q(client, "wash_table_discovery", discovery_sql, 3.0, expected_max_rows=100, expected_columns=2)

    discovered = (result["steps"]["table_discovery"] or {}).get("rows") or []
    by_schema: dict[str, str] = {}
    for row in discovered:
        by_schema.setdefault(row["table_schema"], row["table_name"])
    print(f"[wash_share] найдены реальные таблицы: {by_schema}")

    if not by_schema:
        print("[wash_share] ни одна из двух схем не дала evt_swap таблицу -- останавливаюсь, не гадаю дальше.")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1

    for pool_name, addr in POOLS.items():
        branches = [
            f"SELECT '{schema}' AS schema_name, count(*) AS n FROM {schema}.{table} WHERE contract_address = {addr}"
            for schema, table in by_schema.items()
        ]
        sql = "\nUNION ALL ".join(branches) + "\nLIMIT 100"
        print(f"\n=== Идентификация схемы для {pool_name} ({addr}) -- UNION ALL COUNT по реальным таблицам ===")
        result["steps"][f"identify_schema_{pool_name}"] = q(client, f"wash_identify_{pool_name}", sql, 5.0, expected_max_rows=100, expected_columns=2)

    # Для каждого пула -- берём схему, которая реально дала n>0 в
    # UNION ALL COUNT выше (та же дизамбигуация, что для Robinhood Chain
    # -- контракт может числиться в нескольких схемах-форках одновременно,
    # но реальные строки для НАШЕГО адреса есть только в одной).
    for pool_name, addr in POOLS.items():
        step = result["steps"].get(f"identify_schema_{pool_name}") or {}
        rows = step.get("rows") or []
        real_schema = None
        for row in rows:
            if row.get("n", 0) and row["n"] > 0:
                real_schema = row["schema_name"]
                break
        if real_schema is None:
            print(f"[wash_share] {pool_name}: ни одна схема не дала строк для {addr} -- пропускаю содержательный запрос")
            continue
        table = by_schema[real_schema]
        print(f"\n=== Содержательный wash_share для {pool_name} (схема {real_schema}.{table}) ===")

        window_filter = f"contract_address = {addr} AND evt_block_time >= NOW() - INTERVAL '7' DAY"
        by_sender_sql = f"""
        SELECT sender, COUNT(*) AS n_swaps, SUM(ABS(amount1)) AS volume_raw_units
        FROM {real_schema}.{table}
        WHERE {window_filter}
        GROUP BY sender ORDER BY volume_raw_units DESC LIMIT 20
        """
        result["steps"][f"by_sender_7d_{pool_name}"] = q(client, f"wash_by_sender_{pool_name}", by_sender_sql, 5.0, expected_max_rows=20, expected_columns=3)

        by_recipient_sql = f"""
        SELECT recipient, COUNT(*) AS n_swaps, SUM(ABS(amount1)) AS volume_raw_units
        FROM {real_schema}.{table}
        WHERE {window_filter}
        GROUP BY recipient ORDER BY volume_raw_units DESC LIMIT 20
        """
        result["steps"][f"by_recipient_7d_{pool_name}"] = q(client, f"wash_by_recipient_{pool_name}", by_recipient_sql, 5.0, expected_max_rows=20, expected_columns=3)

        totals_sql = f"""
        SELECT COUNT(*) AS n_swaps, SUM(ABS(amount1)) AS total_volume_raw_units,
               COUNT(DISTINCT sender) AS n_distinct_sender, COUNT(DISTINCT recipient) AS n_distinct_recipient
        FROM {real_schema}.{table}
        WHERE {window_filter}
        """
        result["steps"][f"totals_7d_{pool_name}"] = q(client, f"wash_totals_{pool_name}", totals_sql, 3.0, expected_max_rows=1, expected_columns=4)

        # Признак самоторговли -- та же эвристика, что нашла 0x65050a:
        # адрес одновременно топ-1 по sender И по recipient, доля топ-3
        # по объёму. Считается в Python ниже из уже полученных данных.
        sender_rows = (result["steps"][f"by_sender_7d_{pool_name}"] or {}).get("rows") or []
        recipient_rows = (result["steps"][f"by_recipient_7d_{pool_name}"] or {}).get("rows") or []
        totals_rows = (result["steps"][f"totals_7d_{pool_name}"] or {}).get("rows") or []
        wash_check = {"note": "недостаточно данных"}
        if sender_rows and recipient_rows and totals_rows:
            total_vol = totals_rows[0]["total_volume_raw_units"]
            top3_sender_vol = sum(r["volume_raw_units"] for r in sender_rows[:3])
            top_sender_addr = sender_rows[0]["sender"]
            top_recipient_addr = recipient_rows[0]["recipient"]
            wash_check = {
                "top3_sender_share_of_volume": (top3_sender_vol / total_vol) if total_vol else None,
                "top_sender_is_also_top_recipient": (top_sender_addr == top_recipient_addr),
                "top_sender_address": top_sender_addr, "top_recipient_address": top_recipient_addr,
            }
        result[f"wash_check_{pool_name}"] = wash_check
        print(f"[wash_share] {pool_name} wash_check: {wash_check}")

    state = load_state()
    print(f"\n=== Остаток funding_mozila_block2: {250.0 - state['funding_mozila_block2']['spent']:.2f} из 250 ===")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
