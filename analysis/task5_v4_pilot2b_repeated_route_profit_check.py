#!/usr/bin/env python3
"""Задача 5, честная проверка прибыльности ПОВТОРЯЮЩИХСЯ (но НЕ
поддерживаемых нашим исполнителем -- хук без подтверждённой модели)
маршрутов конкурента внутри окна пилота -- нужно для отчёта владельцу
("какие маршруты/пулы БЫЛИ у конкурента, но НЕ поддерживаются моделью").
Все 3 fully-supported кандидата (task5_v4_pilot2b_candidate_select.py)
оказались НЕ прибыльными (task5_v4_pilot2b_fund_flow_detail.py) --
проверяем ЗДЕСЬ, была ли реальная прибыль хотя бы у повторяющихся
хук-маршрутов (которые МЫ не можем поддержать), чтобы честно описать
разрыв: "конкурент реально зарабатывал, но на непокрытом хуке", а не
"конкурент вообще не зарабатывал в этом окне"."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import os  # noqa: E402
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from task5_v4_item3_in_window_control_trade import full_fund_flow_check  # noqa: E402

# 7x-повторяющийся набор пулов (0x324323f0.../0x7605ec72...) -- 2 представителя
# (первый и последний по блоку из 7).
# 4x-повторяющийся набор пулов (0xb14ad1fa.../0xcc9f479b...) -- 2 представителя.
TX_HASHES = [
    "0xd1d0aa9208c7344bc17364d6242465b5e4cf9fb33403774e15e648daa0dd2b4e",  # 62335643, 7x-набор, первый
    "0xf1b35e8d4bc2d9065c24719affd18d516ca61d0da0423f8bae0f8a19c0f6d58d",  # 62336111, 7x-набор, последний
    "0x77b461bbb19e69cbf9ad296b7d5b7bc901af812fd539087aa4e2e9162477a756",  # 62339139, 4x-набор, первый
    "0xff0fac2328d276943220d5a5c88b1c9b351394fd43a0ff531d028e8bd1541b6a",  # 62344922, 4x-набор, последний
]


def main() -> None:
    out = {}
    for tx_hash in TX_HASHES:
        ff = full_fund_flow_check(tx_hash)
        out[tx_hash] = ff
        print(f"=== {tx_hash} (блок {ff.get('legs', [{}])[0].get('block', '?') if ff.get('legs') else '?'}) ===")
        for leg in ff.get("legs", []):
            print(f"    pool_id={leg.get('pool_id')} currency0={leg.get('currency0')} "
                  f"currency1={leg.get('currency1')} amount0={leg.get('amount0')} amount1={leg.get('amount1')}")
        print(f"  fully_valid_closed_cycle: {ff.get('fully_valid_closed_cycle')}")
        print(f"  base_token: {ff.get('base_token')}  nonzero_net_flow_tokens: {ff.get('nonzero_net_flow_tokens')}")
        print(f"  verdict: {ff.get('verdict')}")
        print()

    out_path = Path("/home/bot/robinhood-chain-alpha/data/task5_v4_pilot2b_repeated_route_profit_check_result.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, default=str, ensure_ascii=False))
    print(f"[repeated_route_profit_check] сохранено: {out_path}")


if __name__ == "__main__":
    main()
