#!/usr/bin/env python3
"""P5 -- устойчивость variance ratio (VR) + доп. порог sigma<37% (владелец,
2026-09-04, пп.4-5 задания поверх signature-plot). Отдельный скрипт (не
правит p5_gt_signature_plot.py) -- переиспользует те же фетчеры GT
(p5_gt_pool_history.py), но считает VR методологически последовательно:
h-периодные returns строятся СУММИРОВАНИЕМ q последовательных 15-минутных
log-return'ов (q = h/15m), а не берутся из независимо агрегированных GT
h-свечей -- это и есть классическое определение variance ratio (Lo-
MacKinlay), плюс даёт естественный способ бутстрапить: блочный ресэмплинг
15-минутного ряда автоматически ресэмплит h-периодные returns согласованно.

1. Доп. порог sigma<37% (владелец: "планка на полном капитале") --
   добавлен К уже посчитанным 41.1%/53.6% в rolling_sigma_24h/72h, той же
   методологией (окна 24ч/72ч на часовых свечах).
2. Блочный бутстрап VR(h) для h∈{1h,4h,12h,1d}: длина блока = 1 календарный
   день (96 пятнадцатиминуток) -- сохраняет короткую автокорреляцию внутри
   дня, которую ПРОСТОЙ iid-ресэмплинг разрушил бы (а именно
   автокорреляция и есть то, что измеряет VR -- iid-бутстрап систематически
   стягивал бы VR к 1). B=500 повторов, отчёт -- median/p5/p95.
3. Leave-one-day-out (не leave-one-return -- при тысячах 15-минуток влияние
   ОДНОГО return'а статистически инертно по конструкции; календарный день
   -- содержательная единица при ~52-63 днях истории). Для каждого дня:
   исключить его 15-минутные returns, пересчитать VR(h) по остатку --
   показывает, не тянет ли один "новостной" день весь тренд VR.

Только чтение (HTTP GET на GeckoTerminal). Ончейн/Lighter/P5-позицию не
трогает.
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402

import p5_gt_pool_history as gth  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/p5_gt_vr_robustness_result.json")

SIGMA_THRESHOLD_FULL_CAPITAL = 0.37  # владелец, 2026-09-04: "планка на полном капитале", в доп. к 41.1%/53.6%
SIGMA_THRESHOLD_KILL = 0.411
SIGMA_THRESHOLD_BREAKEVEN = 0.536

BARS_PER_DAY_15M = 4 * 24  # 96
HORIZONS = [("1h", 4), ("4h", 16), ("12h", 48), ("1d", 96)]  # (label, q = кол-во 15m баров в периоде)
N_BOOTSTRAP = 500
RNG_SEED = 20260904  # владелец не просил конкретный сид -- фиксирован для воспроизводимости отчёта


def rolling_sigma_distribution(closes: list[float], window_candles: int, periods_per_year: float) -> dict:
    """Та же логика, что p5_gt_signature_plot.py::rolling_sigma_distribution
    -- продублирована здесь (не импортирована), чтобы этот скрипт не зависел
    от файла, который не трогаем."""
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

    import statistics
    return {
        "n_windows": len(sigmas), "median": statistics.median(sigmas),
        "p25": pct(0.25), "p75": pct(0.75), "p90": pct(0.90),
        "min": sigmas[0], "max": sigmas[-1],
        "frac_below_kill_41_1pct": sum(1 for s in sigmas if s < SIGMA_THRESHOLD_KILL) / len(sigmas),
        "frac_below_breakeven_53_6pct": sum(1 for s in sigmas if s < SIGMA_THRESHOLD_BREAKEVEN) / len(sigmas),
        "frac_below_full_capital_37pct": sum(1 for s in sigmas if s < SIGMA_THRESHOLD_FULL_CAPITAL) / len(sigmas),
    }


def annualized_sigma_from_returns(rets: np.ndarray, periods_per_year: float) -> float | None:
    if len(rets) == 0:
        return None
    return float(np.std(rets, ddof=1) * np.sqrt(periods_per_year)) if len(rets) > 1 else None


def vr_point_estimates(rets_15m: np.ndarray) -> dict:
    """VR(h) = Var(h-периодного return, построенного суммированием q
    последовательных 15m returns) / (q * Var(15m return)) -- классическое
    определение (Lo-MacKinlay), нормировка на q делает VR=1 эталоном GBM
    (не аннуализация -- она сокращается в отношении, дана для читаемости)."""
    var_15m = float(np.var(rets_15m, ddof=1)) if len(rets_15m) > 1 else None
    out = {}
    for label, q in HORIZONS:
        n_full = len(rets_15m) // q
        if n_full < 2 or var_15m is None or var_15m == 0:
            out[label] = None
            continue
        h_rets = rets_15m[:n_full * q].reshape(n_full, q).sum(axis=1)
        var_h = float(np.var(h_rets, ddof=1))
        out[label] = var_h / (q * var_15m)
    return out


def block_bootstrap_vr(rets_15m: np.ndarray, rng: np.random.Generator) -> dict:
    n = len(rets_15m)
    n_blocks_needed = -(-n // BARS_PER_DAY_15M)  # ceil
    n_full_days = n // BARS_PER_DAY_15M
    if n_full_days < 5:
        return {"note": f"слишком мало полных дней ({n_full_days}) для блочного бутстрапа -- пропущено."}
    blocks = [rets_15m[i * BARS_PER_DAY_15M:(i + 1) * BARS_PER_DAY_15M] for i in range(n_full_days)]

    samples: dict[str, list[float]] = {label: [] for label, _ in HORIZONS}
    for _ in range(N_BOOTSTRAP):
        chosen = rng.integers(0, n_full_days, size=n_blocks_needed)
        boot_series = np.concatenate([blocks[i] for i in chosen])[:n]
        vr = vr_point_estimates(boot_series)
        for label, _ in HORIZONS:
            if vr[label] is not None:
                samples[label].append(vr[label])

    result = {}
    for label, _ in HORIZONS:
        vals = sorted(samples[label])
        if not vals:
            result[label] = None
            continue
        result[label] = {
            "n_boot": len(vals),
            "median": float(np.median(vals)),
            "p5": vals[max(0, int(0.05 * len(vals)) - 1)],
            "p95": vals[min(len(vals) - 1, int(0.95 * len(vals)))],
        }
    return result


def leave_one_day_out_vr(rows_15m: list[list], rng_unused=None) -> dict:
    """rows_15m -- сырые GT-строки [timestamp, open, high, low, close, volume].
    Группируем по календарному дню (UTC) метки времени ОТКРЫТИЯ свечи,
    исключаем каждый день по очереди, пересчитываем VR(h) по остатку
    (returns считаются на урезанном по порядку ряду close -- на стыке
    исключённого дня несколько return'ов неизбежно "смешивают" соседей,
    это принятое приближение, не искажающее вывод при десятках дней)."""
    closes = np.array([float(r[4]) for r in rows_15m])
    ts = [datetime.fromtimestamp(int(r[0]), tz=timezone.utc).date() for r in rows_15m]
    day_of_bar_return = ts[1:]  # returns[i] соответствует бару i+1 (close[i+1]/close[i])
    full_rets = np.diff(np.log(closes))

    day_to_indices: dict = defaultdict(list)
    for i, d in enumerate(day_of_bar_return):
        day_to_indices[d].append(i)

    full_vr = vr_point_estimates(full_rets)
    per_day_vr = {}
    for d, idxs in sorted(day_to_indices.items()):
        mask = np.ones(len(full_rets), dtype=bool)
        mask[idxs] = False
        rest = full_rets[mask]
        per_day_vr[str(d)] = {"n_excluded_returns": len(idxs), "vr": vr_point_estimates(rest)}

    # Насколько сильно один день двигает VR(1d) (самый чувствительный горизонт,
    # меньше всего returns на усреднение) -- явный диагностический вывод.
    vr_1d_values = [v["vr"]["1d"] for v in per_day_vr.values() if v["vr"]["1d"] is not None]
    spread_1d = (max(vr_1d_values) - min(vr_1d_values)) if vr_1d_values else None

    return {
        "full_sample_vr": full_vr,
        "n_days": len(day_to_indices),
        "per_day_vr_1d_spread_max_minus_min": spread_1d,
        "per_day": per_day_vr,
    }


def run() -> int:
    t0 = time.time()
    rng = np.random.default_rng(RNG_SEED)

    print("=== 0. Сеть ===")
    network, probes = gth.find_working_network_slug()
    if network is None:
        result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "abort_reason": "Ни один слэг сети не дал 200 OK.", "network_probes": probes}
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(f"[vr_robust] СТОП: {result['abort_reason']}")
        return 1
    print(f"[vr_robust] сеть: {network}")

    print("\n=== 1. Скачать 15m и 1h OHLCV (та же конфигурация, что signature-plot) ===")
    rows_15m = gth.fetch_ohlcv_paginated(network, "minute", 15, target_candles=9000, max_pages=10)
    rows_1h = gth.fetch_ohlcv_paginated(network, "hour", 1, target_candles=2000, max_pages=4)
    print(f"[vr_robust] 15m: {len(rows_15m)} свечей, 1h: {len(rows_1h)} свечей")

    print("\n=== 2. Доп. порог sigma<37% на скользящих окнах 24ч/72ч ===")
    closes_1h = [float(r[4]) for r in rows_1h]
    rolling_24h = rolling_sigma_distribution(closes_1h, 24, 24 * 365.25)
    rolling_72h = rolling_sigma_distribution(closes_1h, 72, 24 * 365.25)
    print(f"[vr_robust] rolling 24h: {rolling_24h}")
    print(f"[vr_robust] rolling 72h: {rolling_72h}")

    print("\n=== 3. VR(h) точечно (h-return = сумма q 15m returns, классическое определение) ===")
    closes_15m = np.array([float(r[4]) for r in rows_15m])
    rets_15m = np.diff(np.log(closes_15m))
    vr_point = vr_point_estimates(rets_15m)
    print(f"[vr_robust] VR точечно: {vr_point}")

    print(f"\n=== 4. Блочный бутстрап VR(h), блок=1 день, B={N_BOOTSTRAP} ===")
    vr_boot = block_bootstrap_vr(rets_15m, rng)
    print(f"[vr_robust] VR бутстрап (median/p5/p95): {vr_boot}")

    print("\n=== 5. Leave-one-day-out VR(h) ===")
    loo = leave_one_day_out_vr(rows_15m)
    print(f"[vr_robust] дней: {loo['n_days']}, разброс VR(1d) leave-one-out: "
          f"{loo['per_day_vr_1d_spread_max_minus_min']}")

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "network": network,
        "n_candles_15m": len(rows_15m), "n_candles_1h": len(rows_1h),
        "rolling_sigma_24h": rolling_24h, "rolling_sigma_72h": rolling_72h,
        "sigma_thresholds_used": {
            "kill_41_1pct": SIGMA_THRESHOLD_KILL, "breakeven_53_6pct": SIGMA_THRESHOLD_BREAKEVEN,
            "full_capital_37pct": SIGMA_THRESHOLD_FULL_CAPITAL,
        },
        "vr_point_estimate": vr_point,
        "vr_block_bootstrap": {"n_bootstrap": N_BOOTSTRAP, "block_len_bars_15m": BARS_PER_DAY_15M,
                                "rng_seed": RNG_SEED, "results": vr_boot},
        "vr_leave_one_day_out": loo,
        "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[vr_robust] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
