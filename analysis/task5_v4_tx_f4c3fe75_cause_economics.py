#!/usr/bin/env python3
"""Экономика предполагаемой причины для 0xf4c3fe75... (владелец: "проверь
наиболее вероятную причину: экономика того же цикла на размере
конкурента до/после изменения. Разделяй прибыль до газа и после
газа.").

Единственный кандидат-причина за 200 блоков до позиции конкурента (см.
task5_v4_tx_f4c3fe75_pool_change_scan.py): Swap в пуле 0x335b6a9d...
(плечо 1, TOK/USDG, 5%), блок 62371950, tx_index 6, ровно за 1 блок до
конкурента (62371951). Пул 0xdabec00b... (плечо 2, 10%) НЕ менялся ни
разу за все 200 блоков -- статичен.

Проверка: тот же цикл (leg1: USDG->TOK на 0x335b6a9d..., leg2:
TOK->USDG на 0xdabec00b...), тот же реальный размер конкурента
(1 769 472 raw USDG, буквальный ERC20-перевод, НЕ decoded Swap-знак --
см. докстринг pool_change_scan.py про инвертированный знак V4 Swap-
события) -- на блоке ДО кандидата-причины (62371949) и блоке ПОСЛЕ
(62371950, тот же блок, где сама причина уже учтена)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import task5_v4_hotpath as hp  # noqa: E402
from task5_v4_route_registry import RouteCycle, RouteLeg, USDG  # noqa: E402

TOK = "0x34031e1c8b4e6bc8f9e7d2cd88ca6aad08772f29"
POOL1 = "0x335b6a9d35d99442aa8e67f817f0d07f1970f17898c099ac20e5ab0977fd4135"  # currency0=TOK, currency1=USDG, fee 50000
POOL2 = "0xdabec00bfedb7d75a654f6a2e8a5281a7ecd7e1eef4e4a6ecc4265b7cac4eab9"  # currency0=TOK, currency1=USDG, fee 100000
NATIVE = "0x0000000000000000000000000000000000000000"

CANDIDATE_CAUSE_BLOCK = 62371950  # блок, где произошёл единственный кандидат-причина (сам Swap уже внутри)
BLOCK_BEFORE_CAUSE = 62371949
COMPETITOR_BLOCK = 62371951
COMPETITOR_TX_INDEX = 3
ACTUAL_SIZE_USDG_RAW = 1_769_472  # реальный вход конкурента (буквальный ERC20-перевод leg1)


def build_route() -> RouteCycle:
    leg1 = RouteLeg(currency0=TOK, currency1=USDG, fee=50000, tick_spacing=500, hooks=NATIVE, zero_for_one=False)
    leg2 = RouteLeg(currency0=TOK, currency1=USDG, fee=100000, tick_spacing=1000, hooks=NATIVE, zero_for_one=True)
    assert leg1.pool_id_hex.lower() == POOL1.lower(), (leg1.pool_id_hex, POOL1)
    assert leg2.pool_id_hex.lower() == POOL2.lower(), (leg2.pool_id_hex, POOL2)
    return RouteCycle(route_id="route_f4c3fe75_manual", legs=(leg1, leg2), exit_token=USDG,
                       label="0x34031e->0x5fc536(pool1,5%)->0x34031e->0x5fc536(pool2,10%)", source="manual")


def main() -> None:
    route = build_route()
    result: dict = {
        "route_id": route.route_id, "actual_size_usdg_raw": ACTUAL_SIZE_USDG_RAW,
        "block_before_cause": BLOCK_BEFORE_CAUSE, "candidate_cause_block": CANDIDATE_CAUSE_BLOCK,
        "competitor_block": COMPETITOR_BLOCK, "competitor_tx_index": COMPETITOR_TX_INDEX,
        "note": ("profit_raw здесь -- ПРИБЫЛЬ ДО ГАЗА в raw USDG (6 decimals), из quote_route_at_size "
                 "(реальный V4Quoter eth_call, НЕ decoded Swap-знак)."),
    }

    for label, block in (("before_cause", BLOCK_BEFORE_CAUSE), ("after_cause_same_block", CANDIDATE_CAUSE_BLOCK),
                          ("competitor_block", COMPETITOR_BLOCK)):
        try:
            q = hp.quote_route_at_size(route, ACTUAL_SIZE_USDG_RAW, block)
            result[label] = {"block": block, **q}
            if q.get("ok"):
                result[label]["profit_before_gas_usdg"] = q["profit_raw"] / 1e6
        except Exception as exc:  # noqa: BLE001
            result[label] = {"block": block, "ok": False, "error": str(exc)}

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_tx_f4c3fe75_cause_economics_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[cause_economics] сохранено: {out_path}")


if __name__ == "__main__":
    main()
