#!/usr/bin/env python3
"""Задача 5, диагностика трёх кандидатов из
task5_v4_pilot2b_candidate_select.py, у которых ВСЕ плечи (по
сенсор-фильтрованному набору из скана) поддерживаются нашим
исполнителем, НО full_fund_flow_check честно вернул "НЕ доказанный
замкнутый цикл": печатаем ПОЛНЫЙ разбор (все Swap-логи ИЗ РЕЦЕПТА,
независимо от sender -- НЕ только те, что нашёл сенсор-фильтрованный
скан по sender==арбитражник) -- чтобы честно увидеть, состоит ли
реальная транзакция ИМЕННО из тех пулов, что мы предположили, или из
БОЛЬШЕГО/ДРУГОГО набора (что и объясняет 'не одна валюта осталась
ненулевой')."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import os  # noqa: E402
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from task5_v4_item3_in_window_control_trade import full_fund_flow_check  # noqa: E402

TX_HASHES = [
    "0xef1757f0b4235b110b4aa820ccfdf2d1d8a01ba8a94d4b09cf296f4804e30174",
    "0x692250d64e007d31e6611ee9d6a96285d9c63207203b52264f53c88d6550d009",
    "0x4e0f926bb9aef190428483f640a70ccac0ae5b670f67d229fd318127cfd4ebe6",
]


def main() -> None:
    out = {}
    for tx_hash in TX_HASHES:
        ff = full_fund_flow_check(tx_hash)
        out[tx_hash] = ff
        print(f"=== {tx_hash} ===")
        print(f"  n_legs_in_full_receipt (все Swap-логи, любой sender): {len(ff.get('legs', []))}")
        for leg in ff.get("legs", []):
            print(f"    pool_id={leg.get('pool_id')} currency0={leg.get('currency0')} "
                  f"currency1={leg.get('currency1')} amount0={leg.get('amount0')} amount1={leg.get('amount1')}")
        print(f"  net_trader_flow_by_token_raw: {ff.get('net_trader_flow_by_token_raw')}")
        print(f"  nonzero_net_flow_tokens: {ff.get('nonzero_net_flow_tokens')}")
        print(f"  other_token_spend_by_executor: {ff.get('other_token_spend_by_executor')}")
        print(f"  positive_net_transfer_addresses: {ff.get('positive_net_transfer_addresses')}")
        print(f"  tx_from: {ff.get('tx_from')}  tx_to: {ff.get('tx_to')}")
        print()

    out_path = Path("/home/bot/robinhood-chain-alpha/data/task5_v4_pilot2b_fund_flow_detail_result.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, default=str, ensure_ascii=False))
    print(f"[fund_flow_detail] сохранено: {out_path}")


if __name__ == "__main__":
    main()
