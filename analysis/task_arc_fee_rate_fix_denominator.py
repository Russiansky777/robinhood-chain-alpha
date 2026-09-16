#!/usr/bin/env python3
"""Владелец нашёл реальную ошибку в analysis/task_arc_fee_rate_verify.py:
знаменатель ratio_to_receiver брался как total_usdc_out_of_pool_manager --
верно только для ПРОДАЖ токена за USDC (USDC выходит из пула). Для ПОКУПОК
токена за USDC знаменатель структурно вырождается в сам числитель: USDC
входит в пул одним переводом (полный объём сделки), а выходит из пула
ТОЛЬКО комиссия -- поэтому 14/20 ложно получили ratio=1.0.

Доказательство (устно от владельца, проверено здесь на кэше): деление
amount_to_receiver на 0.02475 давало круглые объёмы сделок (25, 50, 5, 400
USDC и т.д.) для части ratio=1.0 строк.

Исправление НЕ требует новых RPC-вызовов: нужные Transfer-логи (кто/куда/
сколько USDC) уже сохранены в data/task_arc_fee_rate_verify_result.json
по каждой из 20 строк (поле all_usdc_transfers_deduped, уже прошедшее
дедуп зеркальных native/ERC20 логов). Правильный знаменатель -- реальный
ВАЛОВЫЙ объём сделки в USDC независимо от направления:
    denom = max(sum(USDC в PoolManager), sum(USDC из PoolManager))
Для продажи (USDC из пула) это не меняет старое значение. Для покупки
(USDC в пул) это впервые захватывает истинный объём вместо одной комиссии."""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent.joinpath("data")
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
PROFIT_RECEIVER = "0x47e7936ae9891e61c5123db720593c05de7120cc"


def main() -> None:
    src = json.loads(DATA_DIR.joinpath("task_arc_fee_rate_verify_result.json").read_text())
    rows_old = src["fee_rate_sample_20_spread"]["rows"]

    rows_fixed = []
    for r in rows_old:
        transfers = r.get("all_usdc_transfers_deduped", [])
        total_in = sum(t["usdc_equiv"] for t in transfers if t["to"] == POOL_MANAGER.lower())
        total_out = sum(t["usdc_equiv"] for t in transfers if t["from"] == POOL_MANAGER.lower())
        to_recv = r.get("amount_to_receiver_usdc") or 0.0
        denom = max(total_in, total_out)
        ratio_fixed = (to_recv / denom) if denom else None
        rows_fixed.append({
            "tx_hash": r["tx_hash"], "block_number": r.get("block_number"),
            "amount_to_receiver_usdc": to_recv,
            "total_usdc_into_pool_manager": total_in,
            "total_usdc_out_of_pool_manager_OLD_DENOM": total_out,
            "denom_FIXED_max_in_out": denom,
            "ratio_to_receiver_OLD_WRONG": r.get("ratio_to_receiver"),
            "ratio_to_receiver_FIXED": ratio_fixed,
            "direction_inferred": "buy (USDC->pool)" if total_in > total_out else "sell (pool->USDC)",
        })

    def near(r, target, eps=0.0005):
        return r is not None and abs(r - target) <= eps

    ratios = [r["ratio_to_receiver_FIXED"] for r in rows_fixed if r["ratio_to_receiver_FIXED"] is not None]
    n_at_2_5pct = sum(1 for r in ratios if near(r, 0.025))
    n_at_1pct = sum(1 for r in ratios if near(r, 0.01))
    other = [round(r, 5) for r in ratios if not near(r, 0.025) and not near(r, 0.01)]

    result = {
        "note": "Пересчёт БЕЗ новых RPC-вызовов -- те же 20 сэмплов, Transfer-логи уже были сохранены в task_arc_fee_rate_verify_result.json.",
        "source": "data/task_arc_fee_rate_verify_result.json",
        "bug_confirmed": "старый знаменатель (только USDC-выход из PoolManager) давал ratio=1.0 на всех 'покупках', где реальный валовый объём сделки заходил В пул, а из пула выходила только комиссия.",
        "rows": rows_fixed,
        "n_sampled": len(rows_fixed),
        "n_at_2_5_percent": n_at_2_5pct,
        "n_at_1_percent": n_at_1pct,
        "other_ratio_values": sorted(set(other)),
        "summary_line": f"ставка 2.5% в {n_at_2_5pct} из {len(ratios)} после фикса, 1% в {n_at_1pct} из {len(ratios)}, иные значения: {sorted(set(other))}",
    }

    if n_at_2_5pct == len(ratios):
        daily_inflow_usd = 94608.50949651619
        result["platform_daily_volume_estimate"] = {
            "assumption": "ставка ПОСТОЯННА и равна 2.5% на всей выборке 20/20 после фикса",
            "estimated_daily_platform_volume_usd": daily_inflow_usd / 0.025,
        }
    else:
        daily_inflow_usd = 94608.50949651619
        result["platform_daily_volume_estimate"] = {
            "note": "ставка НЕ единая -- как минимум два кластера (2.5% и 1%), оценка одним числом была бы недостоверной",
            "bound_if_all_at_2_5_percent": daily_inflow_usd / 0.025,
            "bound_if_all_at_1_percent": daily_inflow_usd / 0.01,
            "share_2_5_percent_in_sample": n_at_2_5pct / len(ratios) if ratios else None,
            "share_1_percent_in_sample": n_at_1pct / len(ratios) if ratios else None,
        }

    print(json.dumps(result, indent=2, ensure_ascii=False))
    DATA_DIR.joinpath("task_arc_fee_rate_fix_denominator_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
