#!/usr/bin/env python3
"""Консолидация активов -- Robinhood Chain НЕ использует канонический
Uniswap Labs SwapRouter02 (реально подтверждено: P5-пул.factory() =
`0x1f7d7550b1b028f7571e69a784071f0205fd2efa`, НЕ стандартный CREATE2-
Factory, тот же кастомный форк, что уже задокументирован в §7
паспорта для P3). Реальный роутер неизвестен -- находим его из
РЕАЛЬНОЙ истории сделок через P5-пул (dex.trades.tx_to), не гадаем по
аналогии с другими сетями."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/asset_consolidation_rh_router_recon_result.json")
P5_POOL = "52e65b17fb6e5ba00ed806f37afcd2daa50271ca"


def run() -> int:
    client = DuneClient()
    sql = f"""select to_hex(tx_to) as tx_to, project, version, count(*) as n_trades, max(block_time) as last_trade
from dex.trades
where blockchain = 'robinhood' and to_hex(project_contract_address) = '{P5_POOL}'
group by to_hex(tx_to), project, version
order by n_trades desc
limit 20"""
    qid = client.create_query("asset_consolidation_rh_router_recon", sql)
    df = client.run_sql_cached("asset_consolidation_rh_router_recon", sql, query_id=qid,
                                estimated_credits=3.0, expected_max_rows=25, expected_columns=5)
    out = {"rows": df.to_dict("records") if df is not None else None}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[rh_router_recon] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
