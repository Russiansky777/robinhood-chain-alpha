#!/usr/bin/env python3
"""P5 -- dry-run ЗАКРЫТИЯ (владелец, 2026-09-04): "закрыть ETH-шорт на
Lighter -> collect комиссий и burn LP 1000756 -> финальный PnL по всем
ногам с газом". ТОЛЬКО ЧТЕНИЕ -- ни одна функция здесь не подписывает
и не отправляет ничего (ни на Base, ни на Lighter). Реальное закрытие
-- отдельный явный шаг, только после "да" владельца (p5_live_close.py
--confirm-mainnet для LP-ноги, p5_live_flatten_lighter.py для хеджа).

Источники чисел (всё реально прочитано, ничего не предположено):
  - LP-нога 1000756: read_position() (positions(uint256), тот же вызов,
    что p5_live_close.py) для liquidity/tickLower/tickUpper; ФАКТИЧЕСКИЕ
    накопленные комиссии -- collect_static_call() (eth_call-симуляция
    NonfungiblePositionManager.collect(), НЕ транзакция, ничего не
    меняет ончейн) из p5_live_position_snapshot.py -- этот вызов
    реально пересчитывает feeGrowthInside к текущему блоку (NFPM.collect()
    делает pool.burn(tickLower,tickUpper,0) перед transfer), поэтому даёт
    АКТУАЛЬНУЮ сумму, а не устаревший tokensOwed0/1.
  - Ожидаемый принципал при decreaseLiquidity(вся liquidity) -- та же
    формула (sqrtP зажатый в [sqrtA,sqrtB]), что p5_live_close.py.
  - IL относительно факта открытия -- известный депозит из
    data/p3_guard_cache/p5_live_step1_result.json (реальные amount0/1
    при открытии, тот же tokenId).
  - Газ на decreaseLiquidity+collect -- eth_estimateGas (РЕАЛЬНАЯ оценка,
    ничего не отправляется) + eth_gasPrice, тот же паттерн/буфер collect
    (+250_000), что p5_live_close.py.
  - ETH-шорт на Lighter -- GET /api/v1/account (публичный, без подписи):
    unrealized_pnl, avg_entry_price, realized_pnl, total_funding_paid_out,
    liquidation_price -- те же поля, что уже пишет p5_live_position_snapshot.py
    в data/p5_fee_accrual.jsonl. taker_fee -- реальное поле рынка ETH
    (orderBookDetails), не предположено.
  - "Что освободится на Lighter" -- available_balance ПОСЛЕ закрытия
    оценивается как collateral_now + unrealized_pnl_usd − closing_fee_usd
    (при условии, что после закрытия на аккаунте не остаётся других
    позиций -- проверяется явно по факту, не предполагается).

Ничего не пишет в data/p5_fee_accrual.jsonl (тот ряд -- собственность
защищённого cron'а, здесь только читается для контекста, не изменяется).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import p5_live_precheck as pc  # noqa: E402
from p5_live_close import read_position  # noqa: E402 -- реальное чтение positions(tokenId), read-only
from p5_live_position_snapshot import collect_static_call, TOKEN_ID, NFPM  # noqa: E402 -- eth_call-симуляция collect(), read-only
from p5_live_step1 import eth_estimate_gas, eth_gas_price, WETH_DECIMALS, USDG_DECIMALS  # noqa: E402 -- только оценки газа, ничего не отправляется

OUT_PATH = Path("data/p3_guard_cache/p5_close_dry_run_result.json")
STEP1_RESULT_PATH = Path("data/p3_guard_cache/p5_live_step1_result.json")
ACCRUAL_LOG_PATH = Path("data/p5_fee_accrual.jsonl")

DECREASE_SIG_GAS_PROBE_EXTRA = 250_000  # тот же буфер на collect, что p5_live_close.py (его оценка ДО decreaseLiquidity ненадёжна)


def build_calldata_decrease(token_id: int, liquidity: int, amount0_min: int, amount1_min: int, deadline: int) -> bytes:
    from eth_abi import encode as abi_encode
    from alchemy_fallback import topic0
    selector = bytes.fromhex(topic0("decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))")[2:10])
    return selector + abi_encode(["(uint256,uint128,uint256,uint256,uint256)"], [(token_id, liquidity, amount0_min, amount1_min, deadline)])


def read_last_accrual_entry() -> dict | None:
    if not ACCRUAL_LOG_PATH.exists():
        return None
    last_line = None
    with ACCRUAL_LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                last_line = line
    return json.loads(last_line) if last_line else None


def lp_close_dry_run() -> dict:
    print("=== LP-нога: реальное чтение позиции 1000756 (read-only) ===")
    pos = read_position(TOKEN_ID)
    pool = pc.read_pool_state()
    pool_price = pc.price_from_sqrt(pool["sqrt_price_x96"])
    tick_lower, tick_upper = pos["tickLower"], pos["tickUpper"]

    sqrt_p_raw = pool["sqrt_price_x96"] / (2 ** 96)
    sqrt_pa_raw = (1.0001 ** tick_lower) ** 0.5
    sqrt_pb_raw = (1.0001 ** tick_upper) ** 0.5
    if sqrt_pa_raw > sqrt_pb_raw:
        sqrt_pa_raw, sqrt_pb_raw = sqrt_pb_raw, sqrt_pa_raw
    sqrt_p_clamped = min(max(sqrt_p_raw, sqrt_pa_raw), sqrt_pb_raw)
    principal_amount0_raw = max(pos["liquidity"] * (1 / sqrt_p_clamped - 1 / sqrt_pb_raw), 0.0)
    principal_amount1_raw = max(pos["liquidity"] * (sqrt_p_clamped - sqrt_pa_raw), 0.0)
    principal0_eth = principal_amount0_raw / 10 ** WETH_DECIMALS
    principal1_usdg = principal_amount1_raw / 10 ** USDG_DECIMALS

    print("=== LP-нога: реальные накопленные комиссии (eth_call-симуляция collect(), не транзакция) ===")
    fees0_raw, fees1_raw = collect_static_call(TOKEN_ID, pc.WALLET)
    fees0_eth = fees0_raw / 10 ** WETH_DECIMALS
    fees1_usdg = fees1_raw / 10 ** USDG_DECIMALS
    fees_usd = fees0_eth * pool_price + fees1_usdg

    step1 = json.loads(STEP1_RESULT_PATH.read_text()) if STEP1_RESULT_PATH.exists() else {}
    lp_open = step1.get("lp_position", {})
    known_deposit0 = lp_open.get("amount0_eth_actual") if lp_open.get("token_id") == TOKEN_ID else None
    known_deposit1 = lp_open.get("amount1_usdg_actual") if lp_open.get("token_id") == TOKEN_ID else None
    il_usd = None
    if known_deposit0 is not None and known_deposit1 is not None:
        il0 = principal0_eth - known_deposit0
        il1 = principal1_usdg - known_deposit1
        il_usd = il0 * pool_price + il1

    deadline = int(time.time()) + 600
    amount0_min = int(principal_amount0_raw * 0.95)
    amount1_min = int(principal_amount1_raw * 0.95)
    gas_price = eth_gas_price()
    gas_decrease = eth_estimate_gas(NFPM, build_calldata_decrease(TOKEN_ID, pos["liquidity"], amount0_min, amount1_min, deadline))
    gas_total_est = gas_decrease + DECREASE_SIG_GAS_PROBE_EXTRA
    gas_cost_usd = gas_total_est * gas_price / 1e18 * pool_price

    last_accrual = read_last_accrual_entry()

    return {
        "token_id": TOKEN_ID, "onchain_position": pos,
        "pool_price_usd_now": pool_price, "pool_tick_now": pool["tick"],
        "principal_if_decreased_now": {"amount0_eth": principal0_eth, "amount1_usdg": principal1_usdg,
                                        "principal_usd": principal0_eth * pool_price + principal1_usdg},
        "known_deposit_at_open": {"amount0_eth": known_deposit0, "amount1_usdg": known_deposit1},
        "impermanent_loss_vs_deposit_usd": il_usd,
        "fees_collectible_now_real": {"fees0_eth": fees0_eth, "fees1_usdg": fees1_usdg, "fees_usd": fees_usd,
                                       "method": "eth_call-симуляция collect() -- реальная, актуальная к текущему блоку сумма"},
        "gas_projection": {"gas_units_decrease_estimated": gas_decrease, "gas_units_collect_buffer": DECREASE_SIG_GAS_PROBE_EXTRA,
                            "gas_price_wei": gas_price, "gas_cost_usd_new_close_txs": gas_cost_usd},
        "already_spent_gas_usd_cumulative_sunk": (last_accrual or {}).get("gas_spent_cumulative_usd"),
        "last_accrual_point_context": {
            "timestamp_utc": (last_accrual or {}).get("timestamp_utc"),
            "fee_capture_ratio_cumulative": (last_accrual or {}).get("fee_capture_ratio_cumulative"),
            "our_fees_usd_cum_last_hourly": (last_accrual or {}).get("our_fees_usd_cum"),
        } if last_accrual else None,
    }


def hedge_close_dry_run() -> dict:
    print("\n=== Хедж-нога: реальное чтение позиции ETH на Lighter (публичный GET, без подписи) ===")
    account_full = pc.lighter_account_full()
    eth_market = pc.lighter_eth_perp()
    positions = account_full.get("positions", []) if account_full else []
    eth_pos = next((p for p in positions if str(p.get("symbol", "")).upper() == "ETH"), None)
    other_open_positions = [p for p in positions if str(p.get("symbol", "")).upper() != "ETH" and abs(float(p.get("position", 0))) > 1e-9]

    if account_full is None or eth_pos is None:
        return {"abort_reason": "не удалось прочитать ETH-позицию с аккаунта 22012 -- нечего закрывать в dry-run."}

    mark_price_now = float(eth_market["mark_price"]) if eth_market else None
    taker_fee = float(eth_market.get("taker_fee", "0")) if eth_market else 0.0
    position_size_eth = float(eth_pos.get("position", 0))
    unrealized_pnl_usd = float(eth_pos.get("unrealized_pnl", 0))
    avg_entry_price = float(eth_pos.get("avg_entry_price", 0)) if eth_pos.get("avg_entry_price") not in (None, "") else None
    realized_pnl_usd = float(eth_pos["realized_pnl"]) if eth_pos.get("realized_pnl") not in (None, "") else 0.0
    total_funding_paid_out_usd = (float(eth_pos["total_funding_paid_out"])
                                   if eth_pos.get("total_funding_paid_out") not in (None, "") else 0.0)
    liquidation_price_usd = eth_pos.get("liquidation_price")

    collateral_now_usd = float(account_full.get("collateral", 0))
    available_balance_now_usd = float(account_full.get("available_balance", 0))
    cross_imr_now_usd = float(account_full.get("cross_initial_margin_requirement", 0))

    closing_notional_usd = abs(position_size_eth) * (mark_price_now or 0)
    closing_fee_usd = closing_notional_usd * taker_fee

    realized_if_closed_now_usd = unrealized_pnl_usd - closing_fee_usd  # реализуется сейчас; realized_pnl_usd/funding уже сидят в collateral_now

    collateral_after_close_usd = collateral_now_usd + unrealized_pnl_usd - closing_fee_usd
    # доступный баланс после закрытия = collateral, ЕСЛИ на аккаунте не осталось других позиций
    # (проверено по факту -- other_open_positions -- не предположено)
    if other_open_positions:
        available_balance_after_close_usd = None
        freed_note = ("НА АККАУНТЕ ЕСТЬ ДРУГИЕ ОТКРЫТЫЕ ПОЗИЦИИ КРОМЕ ETH -- "
                       "available_balance после закрытия НЕ равен collateral_after_close, не считаю без доп. данных.")
    else:
        available_balance_after_close_usd = collateral_after_close_usd
        freed_note = "На аккаунте нет других открытых позиций (проверено по факту) -- available_balance после закрытия = collateral_after_close."
    freed_on_lighter_usd = (available_balance_after_close_usd - available_balance_now_usd) if available_balance_after_close_usd is not None else None

    return {
        "eth_position_now": {"symbol": eth_pos.get("symbol"), "sign": eth_pos.get("sign"),
                              "position_size_eth": position_size_eth, "avg_entry_price_usd": avg_entry_price,
                              "mark_price_now_usd": mark_price_now, "unrealized_pnl_usd": unrealized_pnl_usd,
                              "realized_pnl_usd_already": realized_pnl_usd,
                              "total_funding_paid_out_usd_already": total_funding_paid_out_usd,
                              "liquidation_price_usd": liquidation_price_usd},
        "other_open_positions_besides_eth": other_open_positions,
        "closing_order_projection": {"taker_fee_real": taker_fee, "closing_notional_usd": closing_notional_usd,
                                      "closing_fee_usd": closing_fee_usd, "reduce_only": True,
                                      "note": "reduce_only рыночный ордер на текущий реальный размер (тот же паттерн, что p5_live_flatten_lighter.py) -- НЕ отправлен, только оценка."},
        "realized_pnl_if_closed_now_usd": realized_if_closed_now_usd,
        "account_now": {"collateral_usd": collateral_now_usd, "available_balance_usd": available_balance_now_usd,
                         "cross_initial_margin_requirement_usd": cross_imr_now_usd},
        "account_projection_after_close": {"collateral_usd": collateral_after_close_usd,
                                            "available_balance_usd": available_balance_after_close_usd,
                                            "note": freed_note},
        "freed_on_lighter_usd": freed_on_lighter_usd,
    }


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "note": "DRY-RUN, ничего не отправлено (ни на Base, ни на Lighter) -- только чтение и симуляция eth_call.",
                     "order_owner_specified": "1) закрыть ETH-шорт на Lighter -> 2) collect комиссий и burn LP 1000756 -> 3) финальный PnL по всем ногам с газом"}

    result["step1_hedge_close"] = hedge_close_dry_run()
    result["step2_lp_close"] = lp_close_dry_run()

    hedge = result["step1_hedge_close"]
    lp = result["step2_lp_close"]
    if "abort_reason" not in hedge:
        fees_usd = lp["fees_collectible_now_real"]["fees_usd"]
        il_usd = lp["impermanent_loss_vs_deposit_usd"] or 0.0
        hedge_pnl_usd = hedge["realized_pnl_if_closed_now_usd"]
        new_gas_usd = lp["gas_projection"]["gas_cost_usd_new_close_txs"] + hedge["closing_order_projection"]["closing_fee_usd"]
        net_pnl_usd = fees_usd + il_usd + hedge_pnl_usd - new_gas_usd
        result["step3_final_pnl_all_legs"] = {
            "fees_earned_usd": fees_usd, "impermanent_loss_usd": il_usd, "hedge_pnl_usd": hedge_pnl_usd,
            "new_closing_gas_and_fees_usd": new_gas_usd,
            "already_sunk_gas_usd_cumulative": lp.get("already_spent_gas_usd_cumulative_sunk"),
            "net_pnl_if_closed_now_usd": net_pnl_usd,
            "freed_on_lighter_usd": hedge.get("freed_on_lighter_usd"),
        }
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
