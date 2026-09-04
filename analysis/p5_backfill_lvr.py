#!/usr/bin/env python3
"""Одноразовый бэкфилл: дописать LVR-блок (задача владельца, 2026-09-04
"считать по каждой уже собранной точке ретроактивно, ряд однородный") в
уже собранные строки data/p5_fee_accrual.jsonl, снятые ДО того, как
p5_live_position_snapshot.py стал считать LVR/margin-reconciliation.

Источник для L/тиков/цены/unrealized_pnl каждой исторической точки --
РЕАЛЬНЫЕ данные, уже закоммиченные в git history на момент каждого
снятия (data/p3_guard_cache/p5_live_snapshot_1000756_result.json той же
ревизии) -- НЕ переисполнено текущим состоянием задним числом. Формулы
-- ТЕ ЖЕ, что в самом скрипте (lp_value_usd, sigma_realized_annualized_
from_series, static-hedge бенчмарк с базисным членом) -- см. докстринг
p5_live_position_snapshot.py для полного вывода.

ЧЕСТНО НЕ БЭКФИЛЛИТСЯ (владелец, "не выдумывать данные"): realized_pnl/
total_funding_paid_out/cross_initial_margin_requirement -- эти поля НЕ
читались до обновления кода (2026-09-04), поэтому:
  - combined_pnl_ex_fees для СТАРЫХ точек включает только unrealized_pnl
    (без funding/realized) -- честно помечено полем `funding_included=false`.
  - Сверка свободной маржи (Δcollateral на реальных полях) для СТАРЫХ
    точек НЕВОЗМОЖНА -- margin_recon_residual_usd=null с явной причиной,
    не 0 (0 выглядело бы как "проверено и сошлось", это не так).

Только чтение git history + перезапись data/p5_fee_accrual.jsonl.
Ончейн/Lighter/GT не трогает -- сеть не нужна.
"""
from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ACCRUAL_LOG_PATH = Path("data/p5_fee_accrual.jsonl")
WETH_DECIMALS, USDG_DECIMALS = 18, 6
L_HUMAN_DIVISOR = 10 ** ((WETH_DECIMALS + USDG_DECIMALS) // 2)
LP_OPEN_PRICE_USD = 2509.904741515298  # data/p5_live_position_state.json::pool_price_usd_entry, фиксировано при входе

# (timestamp_utc в jsonl-строке, git-ревизия с реальным полным отчётом на тот момент)
HISTORICAL_REVISIONS = {
    "2026-09-03T22:38:12Z": "8576798",
    "2026-09-03T22:54:10Z": "5fcb8f5",
    "2026-09-04T01:48:49Z": "c98742c",
    "2026-09-04T02:44:32Z": "ccea6fa",
    "2026-09-04T03:44:18Z": "205ab66",
    "2026-09-04T04:45:40Z": "7f0c53d",
    "2026-09-04T05:44:44Z": "d960084",
    "2026-09-04T06:45:23Z": "cc2c681",
    "2026-09-04T07:44:16Z": "1dcfe95",
    "2026-09-04T08:44:42Z": "2d30833",
    "2026-09-04T09:44:54Z": "f66567f",
    "2026-09-04T10:44:01Z": "ecc6dae",
}


def price_from_tick(tick: int) -> float:
    return (1.0001 ** tick) * (10 ** (WETH_DECIMALS - USDG_DECIMALS))


def lp_value_usd(L_human: float, tick_lower: int, tick_upper: int, P_usd: float) -> float:
    Pa, Pb = price_from_tick(tick_lower), price_from_tick(tick_upper)
    if Pa > Pb:
        Pa, Pb = Pb, Pa
    P_clamped = min(max(P_usd, Pa), Pb)
    x = max(L_human * (1 / math.sqrt(P_clamped) - 1 / math.sqrt(Pb)), 0.0)
    y = max(L_human * (math.sqrt(P_clamped) - math.sqrt(Pa)), 0.0)
    return x * P_usd + y


def sigma_realized_annualized_up_to(price_series: list[tuple[datetime, float]]) -> dict:
    """ТА ЖЕ логика, что p5_live_position_snapshot.py::sigma_realized_
    annualized_from_series -- running (не look-ahead): принимает только
    точки ДО И ВКЛЮЧАЯ текущую, не весь файл целиком."""
    pts = sorted(price_series, key=lambda p: p[0])
    if len(pts) < 2:
        return {"sigma_realized_annualized": None, "n_returns": 0}
    log_returns, total_seconds = [], 0.0
    for i in range(1, len(pts)):
        t0, p0 = pts[i - 1]
        t1, p1 = pts[i]
        dt_s = (t1 - t0).total_seconds()
        if dt_s <= 0 or p0 <= 0 or p1 <= 0:
            continue
        log_returns.append(math.log(p1 / p0))
        total_seconds += dt_s
    if not log_returns or total_seconds <= 0:
        return {"sigma_realized_annualized": None, "n_returns": 0}
    qv = sum(r ** 2 for r in log_returns)
    years = total_seconds / (365.25 * 24 * 3600)
    return {"sigma_realized_annualized": math.sqrt(qv / years), "n_returns": len(log_returns)}


def historical_position_snapshot(rev: str) -> dict:
    raw = subprocess.run(["git", "show", f"{rev}:data/p3_guard_cache/p5_live_snapshot_1000756_result.json"],
                          capture_output=True, text=True, check=True).stdout
    return json.loads(raw)


def run() -> int:
    if not ACCRUAL_LOG_PATH.exists():
        print("[backfill_lvr] data/p5_fee_accrual.jsonl не найден -- нечего бэкфиллить.")
        return 1

    rows = [json.loads(line) for line in ACCRUAL_LOG_PATH.read_text().splitlines() if line.strip()]

    price_series_so_far: list[tuple[datetime, float]] = []
    updated = 0
    for row in rows:
        ts_dt = datetime.strptime(row["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        price_series_so_far.append((ts_dt, row["pool_price_usd"]))

        if "lp_pnl_ex_fees_usd" in row and row.get("lp_pnl_ex_fees_usd") is not None:
            continue  # уже посчитано текущей версией скрипта -- не трогаем

        ts = row["timestamp_utc"]
        rev = HISTORICAL_REVISIONS.get(ts)
        if rev is None:
            print(f"[backfill_lvr] ПРЕДУПРЕЖДЕНИЕ: нет известной ревизии для точки {ts} -- пропущена, не выдумываю.")
            continue

        d = historical_position_snapshot(rev)
        pos = d["nfpm_positions_raw"]
        pvr = d["price_vs_range"]
        lh = d.get("lighter_hedge_now", {})

        L_human = pos["liquidity"] / L_HUMAN_DIVISOR
        pool_price_now = pvr["pool_price_now_usd"]
        avg_entry_price = lh.get("avg_entry_price_usd")
        real_hedge_size_eth = lh.get("position_size_eth")
        unrealized_pnl = lh.get("unrealized_pnl_usd")

        V_open = lp_value_usd(L_human, pos["tick_lower"], pos["tick_upper"], LP_OPEN_PRICE_USD)
        V_now = lp_value_usd(L_human, pos["tick_lower"], pos["tick_upper"], pool_price_now)
        lp_pnl_ex_fees_usd = V_now - V_open

        # funding/realized_pnl НЕ читались на момент этой точки -- честно
        # только unrealized (см. докстринг модуля).
        combined_pnl_ex_fees_usd = (lp_pnl_ex_fees_usd + unrealized_pnl) if unrealized_pnl is not None else None

        gamma_term_usd = -(L_human / (4 * LP_OPEN_PRICE_USD ** 1.5)) * ((pool_price_now - LP_OPEN_PRICE_USD) ** 2)
        basis_term_usd = (real_hedge_size_eth * (avg_entry_price - LP_OPEN_PRICE_USD)
                           if (real_hedge_size_eth is not None and avg_entry_price is not None) else None)
        static_hedge_benchmark_usd = (gamma_term_usd + basis_term_usd) if basis_term_usd is not None else None
        static_hedge_deviation_usd = (combined_pnl_ex_fees_usd - static_hedge_benchmark_usd
                                       if (combined_pnl_ex_fees_usd is not None and static_hedge_benchmark_usd is not None) else None)

        sigma_info = sigma_realized_annualized_up_to(price_series_so_far)
        sigma_realized = sigma_info.get("sigma_realized_annualized")
        hours_covered = row.get("hours_covered")
        delta_t_years = (hours_covered / (365.25 * 24)) if hours_covered else None
        continuous_lvr_theoretical_usd = (
            (sigma_realized ** 2) * L_human * math.sqrt(pool_price_now) / 4 * delta_t_years
        ) if (sigma_realized is not None and delta_t_years is not None) else None
        our_fees_usd_cum = row.get("our_fees_usd_cum")
        fee_lvr_ratio = (our_fees_usd_cum / continuous_lvr_theoretical_usd
                          if (our_fees_usd_cum is not None and continuous_lvr_theoretical_usd) else None)

        row.update({
            "L_human": L_human,
            "lp_pnl_ex_fees_usd": lp_pnl_ex_fees_usd,
            "combined_pnl_ex_fees_usd": combined_pnl_ex_fees_usd,
            "combined_pnl_ex_fees_funding_included": False,  # честно: funding не читался на этой точке
            "static_hedge_benchmark_usd": static_hedge_benchmark_usd,
            "static_hedge_benchmark_gamma_term_usd": gamma_term_usd,
            "static_hedge_benchmark_basis_term_usd": basis_term_usd,
            "static_hedge_deviation_usd": static_hedge_deviation_usd,
            "sigma_realized_annualized": sigma_realized,
            "sigma_realized_n_returns": sigma_info.get("n_returns"),
            "continuous_lvr_theoretical_usd": continuous_lvr_theoretical_usd,
            "fee_lvr_ratio": fee_lvr_ratio,
            # -- поля для сверки маржи -- НЕ читались на момент этой точки, честно null --
            "hedge_unrealized_pnl_usd": unrealized_pnl,
            "hedge_realized_pnl_usd": None,
            "hedge_funding_paid_out_usd": None,
            "cross_initial_margin_requirement_usd": None,
            "margin_recon_residual_usd": None,
            "margin_recon_note": "realized_pnl/funding/cross_imr не читались до обновления кода 2026-09-04 -- сверка для этой точки невозможна, не выдумана.",
        })
        print(f"[backfill_lvr] {ts}: L_human={L_human:.6f} lp_pnl_ex_fees={lp_pnl_ex_fees_usd:+.6f} "
              f"combined(no funding)={combined_pnl_ex_fees_usd} static_bm={static_hedge_benchmark_usd} "
              f"deviation={static_hedge_deviation_usd} sigma_realized={sigma_realized} "
              f"lvr_theoretical={continuous_lvr_theoretical_usd} fee_lvr_ratio={fee_lvr_ratio}")
        updated += 1

    ACCRUAL_LOG_PATH.write_text("\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows) + "\n")
    print(f"[backfill_lvr] обновлено строк: {updated}/{len(rows)}. Записано {ACCRUAL_LOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
