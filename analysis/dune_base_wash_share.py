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

    for pool_name, addr in POOLS.items():
        print(f"\n=== Идентификация схемы для {pool_name} ({addr}) -- UNION ALL COUNT по кандидатам ===")
        sql = f"""
        SELECT 'uniswap_v3_base' AS schema, count(*) AS n FROM uniswap_v3_base.uniswapv3pool_evt_swap WHERE contract_address = {addr}
        UNION ALL SELECT 'aerodrome_slipstream_base', count(*) FROM aerodrome_slipstream_base.clpool_evt_swap WHERE contract_address = {addr}
        LIMIT 100
        """
        result["steps"][f"identify_schema_{pool_name}"] = q(client, f"wash_identify_{pool_name}", sql, 5.0, expected_max_rows=100, expected_columns=2)

    state = load_state()
    print(f"\n=== Остаток funding_mozila_block2: {250.0 - state['funding_mozila_block2']['spent']:.2f} из 250 ===")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
