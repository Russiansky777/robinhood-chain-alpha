#!/usr/bin/env python3
"""GALAXY-пример: реальное состояние (sqrtPriceX96/tick/liquidity/fee,
через extsload -- та же, уже верифицированная формула, что и в основном
коде) обоих пулов на трёх доступных контрольных точках:
  62371949 -- до предполагаемой причины (0xfaffa3d3..., блок 62371950)
  62371950 -- после причины / непосредственно перед арбитражем (0xf4c3fe75...,
             блок 62371951) -- ОДНО И ТО ЖЕ состояние для обеих целей,
             т.к. между причиной и арбитражем НЕТ других событий ни в
             одном из двух пулов (уже подтверждено сканом за 200 блоков:
             0xdabec00b -- 0 событий вообще, 0x335b6a9d -- ровно 1, это
             сама причина) -- честно указано явно, не подменяется молча.
  62371951 -- после арбитража.

Человеко-читаемая цена: GALAXY = 18 decimals, USDG = 6 decimals.
raw_ratio = (sqrtPriceX96/2**96)**2 (currency1-в-currency0, т.е. USDG за
1 raw-единицу GALAXY currency0); human_price_usdg_per_galaxy = raw_ratio
* 10**(18-6) (перевод raw-отношения в цену за 1 целый GALAXY в USDG)."""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal, getcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call  # noqa: E402
from task5_v4_pool_state_cache import extsload_pool_state  # noqa: E402

getcontext().prec = 60

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOL1 = "0x335b6a9d35d99442aa8e67f817f0d07f1970f17898c099ac20e5ab0977fd4135"  # currency0=GALAXY, currency1=USDG, fee 5%
POOL2 = "0xdabec00bfedb7d75a654f6a2e8a5281a7ecd7e1eef4e4a6ecc4265b7cac4eab9"  # currency0=GALAXY, currency1=USDG, fee 10%
GALAXY_DECIMALS = 18
USDG_DECIMALS = 6

CHECKPOINTS = {
    "before_cause_62371949": 62371949,
    "after_cause_and_before_arb_62371950": 62371950,
    "after_arb_62371951": 62371951,
}


def human_price(sqrt_price_x96: int) -> str:
    raw_ratio = (Decimal(sqrt_price_x96) / Decimal(2) ** 96) ** 2
    price_usdg_per_galaxy = raw_ratio * (Decimal(10) ** (GALAXY_DECIMALS - USDG_DECIMALS))
    return str(price_usdg_per_galaxy)


def main() -> None:
    result: dict = {"pools": {"pool1_fee5pct": POOL1, "pool2_fee10pct": POOL2}, "checkpoints": {}}

    for cp_label, block in CHECKPOINTS.items():
        block_hex = hex(block)
        cp: dict = {"block": block}
        for pool_label, pool_id in (("pool1_fee5pct", POOL1), ("pool2_fee10pct", POOL2)):
            state = extsload_pool_state(pool_id, block_hex, _rpc_call, POOL_MANAGER)
            state["human_price_usdg_per_galaxy"] = human_price(state["sqrt_price_x96"])
            cp[pool_label] = state
        result["checkpoints"][cp_label] = cp

    # Дельта тика между чекпоинтами -- относительно tick_spacing каждого пула
    # (500 у pool1, 1000 у pool2) -- признак, пересекла ли цена несколько
    # инициализированных интервалов (НЕ доказательство пересечения КОНКРЕТНОГО
    # инициализированного тика -- для этого нужен отдельный запрос
    # tickBitmap/tick info, здесь не делается -- честно не заявляется больше,
    # чем показано).
    p1_before = result["checkpoints"]["before_cause_62371949"]["pool1_fee5pct"]["tick"]
    p1_after = result["checkpoints"]["after_cause_and_before_arb_62371950"]["pool1_fee5pct"]["tick"]
    result["pool1_tick_delta_cause"] = p1_after - p1_before
    result["pool1_tick_delta_cause_in_spacings"] = (p1_after - p1_before) / 500

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_galaxy_pool_states_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[galaxy_pool_states] сохранено: {out_path}")


if __name__ == "__main__":
    main()
