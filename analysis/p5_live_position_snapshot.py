#!/usr/bin/env python3
"""P5 LIVE -- первая реальная точка доходности живой позиции (tokenId
1000756) + первая точка почасового ряда комиссий (владелец, 2026-09-03,
две смежные задачи одним прогоном: "снять первую реальную точку
доходности" и "снимать [комиссии] раз в час... Минимум 24 точки до
запуска демона").

ТОЛЬКО ЧТЕНИЕ. Ни decreaseLiquidity, ни collect(), ни Lighter-ордера НЕ
отправляются -- ни одной подписанной транзакции, ни одного приватного
ключа не требуется.

Несобранные комиссии -- НЕ через `positions(tokenId).tokensOwed0/1`
(владелец, дословно: "они обновляются только после poke и покажут
нули"). Подтверждено реальным источником (Uniswap/v3-periphery
NonfungiblePositionManager.sol::collect): `tokensOwed` в сторадже
обновляется только явным mint/increaseLiquidity/decreaseLiquidity/
collect на САМОЙ позиции -- если с момента открытия не было ни одного
такого вызова, `positions()` покажет 0 даже при реально накопленных
комиссиях. `collect()` же ПЕРЕД возвратом суммы сам вызывает
`pool.burn(tickLower, tickUpper, 0)` (нулевой burn -- официальный
паттерн "poke" из того же контракта) и пересчитывает feeGrowthInside
СВЕЖИМ значением -- поэтому `eth_call` (staticcall-эквивалент: нода
выполняет транзакцию локально и возвращает результат, ничего не
подписывается и не публикуется в mempool) на `collect()` с
`amount0Max=amount1Max=type(uint128).max` даёт РЕАЛЬНУЮ актуальную
сумму несобранных комиссий на этот блок, без реальной отправки.

Пишет два файла:
  - data/p3_guard_cache/p5_live_snapshot_1000756_result.json -- полный
    отчёт этого прогона (цена/диапазон, хедж на Lighter, дельта-чек,
    экономика).
  - data/p5_fee_accrual.jsonl -- ОДНА строка добавляется КАЖДЫЙ прогон
    (append-only, схема владельца): timestamp, block, fees0, fees1,
    pool_price, in_range. Предназначен для почасового запуска (см.
    .github/workflows/run_p5_live_position_snapshot.yml) -- минимум 24
    строки нужны до включения демона ребаланса.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_abi import decode as abi_decode, encode as abi_encode  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402

from alchemy_fallback import _rpc_call, get_block_number, topic0  # noqa: E402
import p5_live_precheck as pc  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/p5_live_snapshot_1000756_result.json")
ACCRUAL_LOG_PATH = Path("data/p5_fee_accrual.jsonl")
STATE_PATH = Path("data/p5_live_position_state.json")

TOKEN_ID = 1000756
NFPM = "0x73991a25c818bf1f1128deaab1492d45638de0d3"
WALLET = pc.WALLET
WETH_DECIMALS, USDG_DECIMALS = pc.WETH_DECIMALS, pc.USDG_DECIMALS
MAX_UINT128 = 2 ** 128 - 1
RANGE_PCT = pc.RANGE_PCT  # 0.10 -- тот же параметр, что использовался при открытии

COLLECT_SIG = "collect((uint256,address,uint128,uint128))"


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def collect_static_call(token_id: int, recipient: str) -> tuple[int, int]:
    """eth_call (НЕ транзакция, ничего не подписывается и не
    отправляется) на NonfungiblePositionManager.collect() -- см.
    докстринг модуля почему это даёт РЕАЛЬНУЮ актуальную сумму, а не
    устаревший tokensOwed. from=WALLET нужен только чтобы пройти
    onlyAuthorizedForToken на стороне симуляции ноды."""
    selector = bytes.fromhex(_selector(COLLECT_SIG)[2:])
    data = selector + abi_encode(
        ["(uint256,address,uint128,uint128)"],
        [(token_id, to_checksum_address(recipient), MAX_UINT128, MAX_UINT128)],
    )
    raw = _rpc_call("eth_call", [{
        "to": to_checksum_address(NFPM), "from": to_checksum_address(WALLET),
        "data": "0x" + data.hex(),
    }, "latest"])
    amount0, amount1 = abi_decode(["uint256", "uint256"], bytes.fromhex(raw[2:]))
    return amount0, amount1


def price_from_tick(tick: int) -> float:
    """Та же decimals-поправка, что pc.price_from_sqrt() -- 1.0001**tick
    ЭКВИВАЛЕНТНО (sqrtPriceX96/2**96)**2 по определению Uniswap v3."""
    return (1.0001 ** tick) * (10 ** (WETH_DECIMALS - USDG_DECIMALS))


def raw_sqrt_from_tick(tick: int) -> float:
    """СЫРОЙ (без decimals-поправки) sqrt(1.0001**tick) -- нужен для
    amount0/1 формулы в raw wei/raw-unit, ТА ЖЕ математика, что
    p5_live_close.py::close_position() (см. её докстринг про
    зафиксированный баг 78 млрд ETH у human-adjusted версии в
    p5_live_precheck.py::v3_amounts -- здесь сознательно НЕ переиспользуется)."""
    return (1.0001 ** tick) ** 0.5


def run() -> int:
    t0 = time.time()
    now_utc = datetime.now(timezone.utc)

    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    if not state:
        print("[snapshot] ПРЕДУПРЕЖДЕНИЕ: data/p5_live_position_state.json не найден -- "
              "экономика (часы/APR) не будет посчитана, только фактическое чтение.")

    # === 1. Свежая NFT-позиция + callStatic collect (несобранные комиссии) ===
    print("=== 1. NFPM.positions(1000756) + callStatic collect() (только чтение) ===")
    pos = pc.nfpm_position(TOKEN_ID, NFPM)
    if not pos.get("found"):
        result = {"generated_at_utc": now_utc.isoformat(), "abort_reason": "nfpm_position() не нашла tokenId 1000756."}
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(f"[snapshot] {result['abort_reason']}")
        return 1
    print(f"[snapshot] positions(): liquidity={pos['liquidity']} tokensOwed0={pos['tokens_owed0']} "
          f"tokensOwed1={pos['tokens_owed1']} tick_lower={pos['tick_lower']} tick_upper={pos['tick_upper']}")

    collect_amount0_raw, collect_amount1_raw = collect_static_call(TOKEN_ID, WALLET)
    fees0_eth = collect_amount0_raw / 10 ** WETH_DECIMALS
    fees1_usdg = collect_amount1_raw / 10 ** USDG_DECIMALS
    print(f"[snapshot] callStatic collect() -> НЕСОБРАННЫЕ КОМИССИИ: {fees0_eth:.10f} ETH + {fees1_usdg:.6f} USDG "
          f"(tokensOwed0/1 в positions() выше -- заведомо устаревшие/нулевые, не факт)")

    # === 2. Свежая цена пула против диапазона ===
    print("\n=== 2. Цена пула против диапазона ===")
    pool = pc.read_pool_state()
    pool_price_now = pc.price_from_sqrt(pool["sqrt_price_x96"])
    current_tick = pool["tick"]
    range_lower = price_from_tick(pos["tick_lower"])
    range_upper = price_from_tick(pos["tick_upper"])
    in_range = pos["tick_lower"] <= current_tick < pos["tick_upper"]
    pos_pct_in_range = (pool_price_now - range_lower) / (range_upper - range_lower) * 100 if range_upper != range_lower else None
    pct_drop_to_lower = (pool_price_now - range_lower) / pool_price_now * 100
    pct_rise_to_upper = (range_upper - pool_price_now) / pool_price_now * 100
    nearest_edge = "lower" if pct_drop_to_lower <= pct_rise_to_upper else "upper"

    entry_price = state.get("pool_price_usd_entry")
    pct_of_10pct_path_to_nearest_edge = None
    move_from_entry_pct = None
    if entry_price:
        move_from_entry_pct = (pool_price_now - entry_price) / entry_price * 100
        edge_price = range_lower if move_from_entry_pct < 0 else range_upper
        denom = abs(edge_price - entry_price)
        pct_of_10pct_path_to_nearest_edge = (abs(pool_price_now - entry_price) / denom * 100) if denom else None

    print(f"[snapshot] pool_price_now=${pool_price_now:.4f} диапазон=[${range_lower:.2f}, ${range_upper:.2f}] "
          f"in_range={in_range} позиция_в_диапазоне={pos_pct_in_range:.2f}% ближайшая_граница={nearest_edge}")
    if move_from_entry_pct is not None:
        print(f"[snapshot] движение от входа (${entry_price:.4f}): {move_from_entry_pct:+.4f}%, "
              f"пройдено {pct_of_10pct_path_to_nearest_edge:.2f}% пути ±10% до {nearest_edge}-границы")

    # === 3. Lighter -- текущее состояние хеджа ===
    print("\n=== 3. Lighter, аккаунт 22012 -- текущее состояние хеджа (только чтение, публичный API) ===")
    account_full = pc.lighter_account_full()
    eth_pos = None
    if account_full:
        eth_pos = next((p for p in account_full.get("positions", []) if str(p.get("symbol", "")).upper() == "ETH"), None)
    eth_market = pc.lighter_eth_perp()
    lighter_mark_price_now = float(eth_market["mark_price"]) if eth_market else None
    real_leverage = pc.real_eth_leverage(account_full)

    lighter_hedge_now: dict = {"found": eth_pos is not None}
    if eth_pos:
        real_hedge_size_eth = abs(float(eth_pos.get("position", 0)))
        liq_price = float(eth_pos["liquidation_price"]) if eth_pos.get("liquidation_price") not in (None, "") else None
        unrealized_pnl = float(eth_pos.get("unrealized_pnl", 0))
        avg_entry_price = float(eth_pos.get("avg_entry_price", 0))
        dist_to_liq_pct_now = ((liq_price / lighter_mark_price_now - 1) * 100) if (liq_price and lighter_mark_price_now) else None
        collateral_usd = float(account_full.get("collateral", 0))
        available_usd = float(account_full.get("available_balance", 0))
        free_margin_pct_now = (available_usd / collateral_usd * 100) if collateral_usd else None

        # Кросс-проверка формулой §5 паспорта (P0/size ФИКСИРОВАНЫ при
        # входе позиции, collateral -- ТЕКУЩИЙ, cross-margin пулит по
        # всему аккаунту) -- сверка с реальным полем liquidation_price,
        # не замена ему.
        mmf_formula_check = None
        if eth_market and eth_market.get("maintenance_margin_fraction") is not None:
            mmf = float(eth_market["maintenance_margin_fraction"]) / 10000
            p0 = avg_entry_price
            p_liq_formula = (collateral_usd + real_hedge_size_eth * p0) / (real_hedge_size_eth * (1 + mmf)) if real_hedge_size_eth else None
            mmf_formula_check = {"mmf": mmf, "p_liq_formula": p_liq_formula,
                                  "p_liq_real": liq_price, "diff_abs": (p_liq_formula - liq_price) if (p_liq_formula and liq_price) else None}

        lighter_hedge_now.update({
            "symbol": eth_pos.get("symbol"), "sign": eth_pos.get("sign"),
            "position_size_eth": real_hedge_size_eth, "avg_entry_price_usd": avg_entry_price,
            "unrealized_pnl_usd": unrealized_pnl, "liquidation_price_usd": liq_price,
            "lighter_mark_price_now_usd": lighter_mark_price_now,
            "distance_to_liquidation_pct_from_current_price": dist_to_liq_pct_now,
            "collateral_usd": collateral_usd, "available_balance_usd": available_usd,
            "free_margin_pct_now": free_margin_pct_now,
            "current_leverage": real_leverage,
            "liquidation_formula_cross_check": mmf_formula_check,
        })
        print(f"[snapshot] Lighter ETH-позиция: size={real_hedge_size_eth} avg_entry=${avg_entry_price} "
              f"unrealized_pnl=${unrealized_pnl} liquidation_price=${liq_price} mark_now=${lighter_mark_price_now}")
        print(f"[snapshot] расстояние до ликвидации СЕЙЧАС (от текущей mark price): {dist_to_liq_pct_now}%")
        print(f"[snapshot] margin: collateral=${collateral_usd} available=${available_usd} свободно={free_margin_pct_now}%")
    else:
        print("[snapshot] ETH-позиция на Lighter НЕ найдена -- голая LP-экспозиция, если это неожиданно, см. флаг ниже.")

    # === 4. Дельта: формула против реального размера хеджа ===
    print("\n=== 4. Дельта -- amount_ETH(P) по РЕАЛЬНОЙ ончейн-ликвидности против реального шорта ===")
    sqrt_p_raw = pool["sqrt_price_x96"] / (2 ** 96)
    sqrt_pa_raw = raw_sqrt_from_tick(pos["tick_lower"])
    sqrt_pb_raw = raw_sqrt_from_tick(pos["tick_upper"])
    if sqrt_pa_raw > sqrt_pb_raw:
        sqrt_pa_raw, sqrt_pb_raw = sqrt_pb_raw, sqrt_pa_raw
    sqrt_p_clamped = min(max(sqrt_p_raw, sqrt_pa_raw), sqrt_pb_raw)
    amount0_required_raw = max(pos["liquidity"] * (1 / sqrt_p_clamped - 1 / sqrt_pb_raw), 0.0)
    amount0_eth_required_now = amount0_required_raw / 10 ** WETH_DECIMALS

    delta_check: dict = {"amount_eth_required_now_formula": amount0_eth_required_now}
    if eth_pos:
        real_hedge_size_eth = lighter_hedge_now["position_size_eth"]
        net_delta_eth_now = amount0_eth_required_now - real_hedge_size_eth
        net_delta_usd_now = net_delta_eth_now * pool_price_now
        drift_pct_of_hedge = (net_delta_eth_now / real_hedge_size_eth * 100) if real_hedge_size_eth else None
        hours_since_open_for_drift = None
        if state.get("opened_at_utc"):
            opened_at = datetime.fromisoformat(state["opened_at_utc"].replace("Z", "+00:00"))
            hours_since_open_for_drift = (now_utc - opened_at).total_seconds() / 3600
        drift_pct_per_hour = (drift_pct_of_hedge / hours_since_open_for_drift) if (drift_pct_of_hedge is not None and hours_since_open_for_drift) else None
        delta_check.update({
            "real_hedge_size_eth": real_hedge_size_eth,
            "net_delta_eth_now": net_delta_eth_now, "net_delta_usd_now": net_delta_usd_now,
            "drift_pct_of_hedge_size": drift_pct_of_hedge,
            "hours_since_open": hours_since_open_for_drift,
            "drift_pct_of_hedge_size_per_hour": drift_pct_per_hour,
        })
        print(f"[snapshot] требуется по формуле сейчас: {amount0_eth_required_now:.8f} ETH, реальный шорт: {real_hedge_size_eth:.8f} ETH")
        print(f"[snapshot] рассинхрон: {net_delta_eth_now:+.8f} ETH (${net_delta_usd_now:+.4f}), "
              f"{drift_pct_of_hedge:+.4f}% от размера хеджа" + (f", {drift_pct_per_hour:+.4f}%/час" if drift_pct_per_hour is not None else ""))
    else:
        print("[snapshot] нет реальной Lighter-позиции для сравнения -- дельта-чек неполный.")

    # === 5. Экономика: часы, комиссии/час, годовые против kill-порога ===
    print("\n=== 5. Экономика на реальных данных ===")
    economics: dict = {}
    fees_usd_unclaimed = fees0_eth * pool_price_now + fees1_usdg
    economics["fees_earned_usd_unclaimed_now"] = fees_usd_unclaimed
    if state.get("opened_at_utc") and state.get("capital_at_risk_usd_entry"):
        opened_at = datetime.fromisoformat(state["opened_at_utc"].replace("Z", "+00:00"))
        hours_elapsed = (now_utc - opened_at).total_seconds() / 3600
        capital_at_risk = state["capital_at_risk_usd_entry"]
        gas_spent_so_far_usd = state.get("total_gas_spent_usd_est", 0.0)  # только вход -- выход ещё не происходил, позиция не закрыта
        net_usd_so_far = fees_usd_unclaimed - gas_spent_so_far_usd
        fees_per_hour_usd = fees_usd_unclaimed / hours_elapsed if hours_elapsed > 0 else None
        annualized_pct_fees_gross = (fees_per_hour_usd * 24 * 365 / capital_at_risk * 100) if fees_per_hour_usd is not None else None
        annualized_pct_net_of_gas = ((net_usd_so_far / hours_elapsed) * 24 * 365 / capital_at_risk * 100) if hours_elapsed > 0 else None
        kill_threshold_pct = state.get("kill_threshold_annual", 0.30) * 100
        economics.update({
            "hours_elapsed": hours_elapsed, "capital_at_risk_usd_entry": capital_at_risk,
            "gas_spent_so_far_usd": gas_spent_so_far_usd, "net_usd_so_far": net_usd_so_far,
            "fees_per_hour_usd": fees_per_hour_usd,
            "annualized_pct_fees_only_gross": annualized_pct_fees_gross,
            "annualized_pct_net_of_gas_spent_so_far": annualized_pct_net_of_gas,
            "kill_threshold_annual_pct": kill_threshold_pct,
            "passes_kill_threshold_gross": (annualized_pct_fees_gross >= kill_threshold_pct) if annualized_pct_fees_gross is not None else None,
            "passes_kill_threshold_net_of_gas": (annualized_pct_net_of_gas >= kill_threshold_pct) if annualized_pct_net_of_gas is not None else None,
            "CAUTION": ("Экстраполяция в годовые с выборки в считанные часы -- статистически ненадёжна "
                        "(один выброс/затишье комиссий даёт кратные искажения APR). Не решение по kill-порогу, "
                        "только первая точка ряда -- см. data/p5_fee_accrual.jsonl, нужно минимум 24 часовых точки."),
        })
        print(f"[snapshot] прожито {hours_elapsed:.4f}ч, несобранные комиссии=${fees_usd_unclaimed:.6f}, "
              f"${fees_per_hour_usd:.6f}/час" if fees_per_hour_usd else "")
        print(f"[snapshot] годовых (комиссии брутто)={annualized_pct_fees_gross:.4f}% "
              f"годовых (за вычетом уже потраченного газа входа ${gas_spent_so_far_usd})={annualized_pct_net_of_gas:.4f}% "
              f"против kill-порога {kill_threshold_pct}%")
    else:
        print("[snapshot] нет data/p5_live_position_state.json с opened_at_utc/capital_at_risk_usd_entry -- экономика не посчитана.")

    # === Запись почасового ряда (append-only) ===
    block_now = get_block_number()
    accrual_entry = {
        "timestamp_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "block": block_now, "token_id": TOKEN_ID,
        "fees0_eth": fees0_eth, "fees1_usdg": fees1_usdg,
        "pool_price_usd": pool_price_now, "in_range": in_range,
    }
    ACCRUAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ACCRUAL_LOG_PATH.open("a") as f:
        f.write(json.dumps(accrual_entry, default=str, ensure_ascii=False) + "\n")
    print(f"\n[snapshot] добавлена строка в {ACCRUAL_LOG_PATH}: {accrual_entry}")

    result = {
        "generated_at_utc": now_utc.isoformat(), "token_id": TOKEN_ID, "block": block_now,
        "nfpm_positions_raw": pos,
        "uncollected_fees_callStatic": {"fees0_eth": fees0_eth, "fees1_usdg": fees1_usdg, "fees_usd": fees_usd_unclaimed},
        "price_vs_range": {
            "pool_price_now_usd": pool_price_now, "range_lower_usd": range_lower, "range_upper_usd": range_upper,
            "current_tick": current_tick, "in_range": in_range, "position_pct_in_range": pos_pct_in_range,
            "pct_drop_to_lower_edge": pct_drop_to_lower, "pct_rise_to_upper_edge": pct_rise_to_upper,
            "nearest_edge": nearest_edge, "move_from_entry_pct": move_from_entry_pct,
            "pct_of_10pct_path_to_nearest_edge": pct_of_10pct_path_to_nearest_edge,
        },
        "lighter_hedge_now": lighter_hedge_now,
        "delta_check": delta_check,
        "economics": economics,
        "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[snapshot] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
