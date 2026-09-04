#!/usr/bin/env python3
"""P5 -- signature plot волатильности + распределение по скользящим
окнам + связь комиссий с волатильностью (владелец, 2026-09-04, задача
на 52-дневной истории GT -- "сети из сессии нет, гнать через GH
Actions").

Переиспользует `p5_gt_pool_history.py` (`fetch_ohlcv_paginated`,
`log_returns`, `annualized_sigma`, `find_working_network_slug`) --
та же сеть/пул/ретрай-на-429/currency=token&token=quote (цена ETH в
USDG, не через шумный USD-курс USDG). НЕ дублирует уже собранные
15м/часовые данные `p5_gt_pool_history_result.json` -- перекачивает
заново под остальные таймфреймы (свежий снимок честнее устаревшего
кэша, стоимость по времени приемлема при текущем 52-63-дневном охвате).

1. Signature plot: реализованная аннуализированная sigma на 15m/1h/4h/
   12h/1d за весь доступный период + variance ratio VR(h)=sigma^2(h)/
   sigma^2(15m). VR<1 по мере роста h => возврат к среднему (mean-
   reversion), статический хедж дешевле, чем даёт LVR-формула
   (построенная на GBM, VR=1 всегда). VR>=1 => тренд/случайное
   блуждание, LVR-оценка честная или консервативная.
2. Распределение sigma на скользящих окнах 24ч/72ч (сэмплирование 1h,
   по ВСЕМУ доступному периоду) -- медиана/p25/p75/p90 + доля окон с
   sigma ниже двух порогов (41.1%, 53.6% -- владелец, из своего расчёта
   fee_yield-2.53*sigma^2>=0.30 при fee_yield=72.73% брутто).
3. Связь почасового объёма пула и |почасового log-return| (корреляция +
   OLS-регрессия), затем НАШ fee_yield(sigma) через эту регрессию:
   sigma -> E[|r|_час]=sigma*sqrt(1/8760)*sqrt(2/pi) -> объём(регрессия)
   -> комиссия пула -> доля пула (fee_capture_ratio, медиана НАШЕГО
   реального ряда) -> годовых. Порог по sigma для kill (30%) и
   breakeven (0%) ищется численно (scipy.optimize.brentq) с РЕАЛЬНЫМ
   коэффициентом LVR c=L_human*sqrt(P)/(4*our_reserve_usd) (не просто
   принят готовый 2.53 владельца -- пересчитан из текущих реальных
   данных data/p5_fee_accrual.jsonl, сверен с 2.53 отдельно).
4. Проверка (не отдельный сетевой вызов): sigma_realized в
   p5_live_position_snapshot.py уже взвешена по РЕАЛЬНОМУ Δt каждого
   шага (QV=sum(r_i^2), делится на СУММУРНОЕ elapsed-время в годах --
   не на число шагов) -- корректно для неравномерных интервалов
   (доказательство в докстринге функции), баг не найден, код не
   менялся.

Только чтение (HTTP GET на GeckoTerminal + локальный git-чекаут
data/p5_fee_accrual.jsonl). Ончейн/Lighter не трогает.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
from scipy.optimize import brentq  # noqa: E402

import p5_gt_pool_history as gth  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/p5_gt_signature_plot_result.json")
ACCRUAL_LOG_PATH = Path("data/p5_fee_accrual.jsonl")
POOL_FEE_FRACTION = 0.0001

TIMEFRAMES = [
    # (label, timeframe, aggregate, periods_per_year, target_candles, max_pages)
    ("15m", "minute", 15, 4 * 24 * 365.25, 9000, 10),
    ("1h", "hour", 1, 24 * 365.25, 2000, 4),
    ("4h", "hour", 4, 6 * 365.25, 500, 2),
    ("12h", "hour", 12, 2 * 365.25, 200, 1),
    ("1d", "day", 1, 365.25, 100, 1),
]
SIGMA_THRESHOLD_KILL = 0.411   # владелец: fee_yield(72.73%)-2.53*sigma^2>=0.30 => sigma<=41.1%
SIGMA_THRESHOLD_BREAKEVEN = 0.536  # тот же расчёт при пороге 0 (просто не в минусе)


def read_local_accrual_series() -> list[dict]:
    if not ACCRUAL_LOG_PATH.exists():
        return []
    return [json.loads(line) for line in ACCRUAL_LOG_PATH.read_text().splitlines() if line.strip()]


def rolling_sigma_distribution(closes: list[float], window_candles: int, periods_per_year: float) -> dict:
    """sigma аннуализированная на КАЖДОМ скользящем окне длиной window_candles
    (шаг 1 свеча) -- эмпирическое распределение, не единственная точечная
    оценка за весь период."""
    n = len(closes)
    if n <= window_candles:
        return {"n_windows": 0}
    sigmas = []
    for start in range(0, n - window_candles):
        window = closes[start:start + window_candles + 1]
        rets = gth.log_returns(window)
        s = gth.annualized_sigma(rets, periods_per_year)
        if s is not None:
            sigmas.append(s)
    if not sigmas:
        return {"n_windows": 0}
    sigmas.sort()

    def pct(p: float) -> float:
        idx = min(len(sigmas) - 1, int(round(p * (len(sigmas) - 1))))
        return sigmas[idx]

    return {
        "n_windows": len(sigmas), "median": statistics.median(sigmas),
        "p25": pct(0.25), "p75": pct(0.75), "p90": pct(0.90),
        "min": sigmas[0], "max": sigmas[-1],
        "frac_below_kill_threshold": sum(1 for s in sigmas if s < SIGMA_THRESHOLD_KILL) / len(sigmas),
        "frac_below_breakeven_threshold": sum(1 for s in sigmas if s < SIGMA_THRESHOLD_BREAKEVEN) / len(sigmas),
    }


def run() -> int:
    t0 = time.time()

    print("=== 0. Сеть (сверка, тот же слэг, что уже подтверждён) ===")
    network, probes = gth.find_working_network_slug()
    if network is None:
        result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "abort_reason": "Ни один слэг сети не дал 200 OK.", "network_probes": probes}
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(f"[sig_plot] СТОП: {result['abort_reason']}")
        return 1
    print(f"[sig_plot] сеть: {network}")

    print("\n=== 1. Signature plot: sigma на 15m/1h/4h/12h/1d ===")
    ohlcv_by_tf: dict[str, list[list]] = {}
    signature_plot: dict[str, dict] = {}
    for label, timeframe, aggregate, periods_per_year, target_candles, max_pages in TIMEFRAMES:
        rows = gth.fetch_ohlcv_paginated(network, timeframe, aggregate, target_candles=target_candles, max_pages=max_pages)
        ohlcv_by_tf[label] = rows
        closes = [float(r[4]) for r in rows]
        rets = gth.log_returns(closes)
        sigma = gth.annualized_sigma(rets, periods_per_year)
        signature_plot[label] = {
            "n_candles": len(rows), "n_returns": len(rets), "periods_per_year": periods_per_year,
            "sigma_annualized": sigma,
            "first_ts": rows[0][0] if rows else None, "last_ts": rows[-1][0] if rows else None,
        }
        print(f"[sig_plot] {label}: {len(rows)} свечей, sigma_annualized={sigma}")

    sigma_15m = signature_plot["15m"]["sigma_annualized"]
    variance_ratios = {}
    for label in ("1h", "4h", "12h", "1d"):
        s = signature_plot[label]["sigma_annualized"]
        variance_ratios[label] = (s ** 2 / sigma_15m ** 2) if (s is not None and sigma_15m) else None
    print(f"[sig_plot] variance ratios (относительно 15m): {variance_ratios}")
    # Честная эвристика, не подгонка под ожидаемый ответ: сравниваем VR на
    # самом длинном таймфрейме с 1.0 и с VR на самом коротком следующем шаге --
    # монотонность по всем точкам не требуется, реальные ряды шумные.
    vr_values = [v for v in variance_ratios.values() if v is not None]
    if len(vr_values) >= 2:
        last_vr = vr_values[-1]
        if last_vr < 0.9:
            vr_trend = f"падает (VR на самом длинном таймфрейме={last_vr:.3f} < 1) -- признак mean-reversion, статический хедж дешевле LVR-формулы"
        elif last_vr > 1.1:
            vr_trend = f"растёт (VR на самом длинном таймфрейме={last_vr:.3f} > 1) -- признак тренда, хуже базового GBM-сценария"
        else:
            vr_trend = f"около 1 (VR на самом длинном таймфрейме={last_vr:.3f}) -- случайное блуждание, LVR-оценка честная"
    else:
        vr_trend = None
    print(f"[sig_plot] тренд VR: {vr_trend}")

    print("\n=== 2. Распределение sigma на скользящих окнах 24ч/72ч (сэмплирование 1h) ===")
    closes_1h = [float(r[4]) for r in ohlcv_by_tf["1h"]]
    rolling_24h = rolling_sigma_distribution(closes_1h, 24, 24 * 365.25)
    rolling_72h = rolling_sigma_distribution(closes_1h, 72, 24 * 365.25)
    print(f"[sig_plot] rolling 24h: {rolling_24h}")
    print(f"[sig_plot] rolling 72h: {rolling_72h}")

    print("\n=== 3. Комиссии vs волатильность: регрессия объём~|log-return| (часовые свечи) ===")
    hourly_rows = ohlcv_by_tf["1h"]
    hourly_closes = [float(r[4]) for r in hourly_rows]
    hourly_volumes = [float(r[5]) for r in hourly_rows]
    abs_returns, vols_for_returns = [], []
    for i in range(1, len(hourly_closes)):
        if hourly_closes[i - 1] > 0 and hourly_closes[i] > 0:
            abs_returns.append(abs(math.log(hourly_closes[i] / hourly_closes[i - 1])))
            vols_for_returns.append(hourly_volumes[i])
    n_obs = len(abs_returns)
    correlation, beta, alpha, r_squared = None, None, None, None
    if n_obs >= 3:
        x = np.array(abs_returns)
        y = np.array(vols_for_returns)
        correlation = float(np.corrcoef(x, y)[0, 1])
        beta, alpha = np.polyfit(x, y, 1)
        beta, alpha = float(beta), float(alpha)
        y_pred = alpha + beta * x
        ss_res = float(np.sum((y - y_pred) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else None
    regression = {"n_obs": n_obs, "correlation": correlation, "alpha_intercept_usd": alpha,
                  "beta_slope_usd_per_absreturn": beta, "r_squared": r_squared}
    print(f"[sig_plot] регрессия объём~|r|: {regression}")

    # --- Реальный коэффициент LVR-в-%-от-reserve (не готовый 2.53 --
    # пересчитан из последней реальной точки data/p5_fee_accrual.jsonl) ---
    accrual = read_local_accrual_series()
    last = accrual[-1] if accrual else {}
    L_human = last.get("L_human")
    pool_price_usd = last.get("pool_price_usd")
    our_reserve_usd = last.get("our_reserve_usd")
    avg_pool_tvl_usd = last.get("avg_pool_tvl_usd") or (
        statistics.mean([r["pool_reserve_in_usd"] for r in accrual if r.get("pool_reserve_in_usd") is not None])
        if any(r.get("pool_reserve_in_usd") is not None for r in accrual) else None
    )
    fee_capture_samples = [r["fee_capture_ratio_cumulative"] for r in accrual if r.get("fee_capture_ratio_cumulative") is not None]
    fee_capture_repr = statistics.median(fee_capture_samples) if fee_capture_samples else None

    lvr_coefficient_c = (L_human * math.sqrt(pool_price_usd) / (4 * our_reserve_usd)
                          if (L_human and pool_price_usd and our_reserve_usd) else None)
    print(f"[sig_plot] LVR-коэффициент c (реальный, из последней точки ряда) = {lvr_coefficient_c} "
          f"(владелец использовал 2.53 -- {'близко' if lvr_coefficient_c and abs(lvr_coefficient_c-2.53)<0.15 else 'см. разницу'})")

    def fee_yield_annual(sigma: float) -> float | None:
        if alpha is None or beta is None or avg_pool_tvl_usd is None or fee_capture_repr is None:
            return None
        e_abs_r_hourly = sigma * math.sqrt(1 / 8760) * math.sqrt(2 / math.pi)
        predicted_hourly_volume = max(alpha + beta * e_abs_r_hourly, 0.0)
        predicted_hourly_pool_fee = predicted_hourly_volume * POOL_FEE_FRACTION
        predicted_pool_yield_hourly = predicted_hourly_pool_fee / avg_pool_tvl_usd
        our_yield_hourly = predicted_pool_yield_hourly * fee_capture_repr
        return our_yield_hourly * 8760

    def net_of_lvr(sigma: float, threshold: float) -> float | None:
        fy = fee_yield_annual(sigma)
        if fy is None or lvr_coefficient_c is None:
            return None
        return fy - lvr_coefficient_c * sigma ** 2 - threshold

    fee_yield_threshold_solve: dict = {
        "assumptions": {
            "lvr_coefficient_c_real": lvr_coefficient_c, "lvr_coefficient_c_owner_estimate": 2.53,
            "fee_capture_ratio_representative_median": fee_capture_repr,
            "avg_pool_tvl_usd": avg_pool_tvl_usd, "regression": regression,
            "note": "fee_yield(sigma) -- НАША годовая доходность по комиссиям, выведенная из регрессии "
                    "объём~|log-return| часовых свечей пула + медианный fee_capture_ratio нашего ряда. "
                    "Экстраполяция регрессии за пределы наблюдённого диапазона |r| -- умозрительна, не факт.",
        },
    }
    for label, threshold in (("kill_30pct", 0.30), ("breakeven_0pct", 0.0)):
        if fee_yield_annual(0.01) is None:
            fee_yield_threshold_solve[label] = {"sigma_threshold": None, "note": "регрессия/коэффициенты недоступны."}
            continue
        lo, hi = 0.01, 3.0
        f_lo, f_hi = net_of_lvr(lo, threshold), net_of_lvr(hi, threshold)
        if f_lo is None or f_hi is None or f_lo * f_hi > 0:
            fee_yield_threshold_solve[label] = {
                "sigma_threshold": None,
                "f_at_sigma_0.01": f_lo, "f_at_sigma_3.0": f_hi,
                "note": ("порога НЕТ в диапазоне sigma∈[1%,300%] -- " +
                         ("неравенство ВСЕГДА выполняется (устойчивее, чем ожидалось)" if (f_lo is not None and f_lo > 0) else
                          "неравенство НИКОГДА не выполняется в этом диапазоне")),
            }
        else:
            sigma_thr = brentq(lambda s: net_of_lvr(s, threshold), lo, hi, xtol=1e-6)
            fee_yield_threshold_solve[label] = {"sigma_threshold": sigma_thr,
                                                  "fee_yield_at_threshold": fee_yield_annual(sigma_thr)}
        print(f"[sig_plot] {label}: {fee_yield_threshold_solve[label]}")

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "network": network,
        "signature_plot": signature_plot, "variance_ratios": variance_ratios, "vr_trend_hint": vr_trend,
        "rolling_sigma_24h": rolling_24h, "rolling_sigma_72h": rolling_72h,
        "sigma_thresholds_used": {"kill": SIGMA_THRESHOLD_KILL, "breakeven": SIGMA_THRESHOLD_BREAKEVEN},
        "volume_return_regression": regression,
        "fee_yield_vs_lvr_threshold_solve": fee_yield_threshold_solve,
        "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[sig_plot] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
