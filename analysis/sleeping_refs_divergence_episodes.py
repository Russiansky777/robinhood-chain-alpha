#!/usr/bin/env python3
"""«Спящие референсы» → расхождение площадок, п.2-3 владельца
(2026-09-06, дословно):

2. "Время жизни. Для каждого случая |D| > суммы round-trip на $5000:
   сколько часов до схождения к половине и к нулю. Медиана, p90. Это
   оборачиваемость капитала."
3. "Знак. Систематически ли одна площадка дороже другой (тогда часть D
   -- базис, а не сделка), или знак симметричен. По тикерам отдельно."

Реюз реального часового D-ряда из `sleeping_refs_hourly_divergence.py`
(`data/sleeping_refs_cache/hourly_divergence_full.csv`) -- ЧИСТО
ЛОКАЛЬНО, 0 сети/кредитов, новых данных не требуется.

Метод "эпизода" (владелец не задавал алгоритм, решение явное и
задокументированное): эпизод начинается на "восходящем фронте" --
первый час, когда |D| ВПЕРВЫЕ становится > порога (round-trip $5000)
ПОСЛЕ часа, когда было <= порога (не считаем каждый час подряд внутри
уже открытого эпизода отдельным случаем -- иначе искусственно раздули
бы число "случаев" одним затяжным эпизодом). "Схождение к нулю" --
смена знака D относительно знака на входе, ИЛИ |D| падает ниже 0.005%
(на порядок ниже типичного round-trip) -- любое из двух, что наступит
раньше."""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

RAW_PATH = Path("data/sleeping_refs_cache/hourly_divergence_full.csv")
OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_divergence_episodes_result.json")
ZERO_EPS_PCT = 0.005  # см. докстринг -- на порядок ниже типичного round-trip


def find_episodes(g: pd.DataFrame) -> list[dict]:
    """g -- ОДИН тикер, отсортирован по времени, реальный (не
    синтетический) часовой ряд -- реальные разрывы во времени (дыры в
    данных) НЕ считаем отдельно, идём по индексу реальных строк как
    есть (шаг между соседними строками может быть >1ч на редких
    реальных пропусках -- честно, не интерполируем)."""
    g = g.reset_index(drop=True)
    threshold = g["round_trip_sum_pct_5000"].iloc[0]
    above = g["abs_D_pct"].to_numpy() > threshold
    D = g["D"].to_numpy()
    absD = g["abs_D_pct"].to_numpy()
    t_ms = g["t"].to_numpy()

    episodes = []
    i = 1
    n = len(g)
    while i < n:
        if above[i] and not above[i - 1]:  # восходящий фронт
            entry_absD = absD[i]
            entry_sign = np.sign(D[i])
            half_target = entry_absD / 2.0
            hours_to_half = None
            hours_to_zero = None
            j = i + 1
            while j < n:
                dt_hours = (t_ms[j] - t_ms[i]) / 3_600_000.0
                if hours_to_half is None and absD[j] <= half_target:
                    hours_to_half = dt_hours
                if hours_to_zero is None and (np.sign(D[j]) != entry_sign or absD[j] < ZERO_EPS_PCT):
                    hours_to_zero = dt_hours
                if hours_to_half is not None and hours_to_zero is not None:
                    break
                j += 1
            episodes.append({
                "entry_idx": i, "entry_abs_D_pct": float(entry_absD), "entry_sign": int(entry_sign),
                "hours_to_half": hours_to_half, "hours_to_zero": hours_to_zero,
                "converged_within_data": hours_to_zero is not None,
            })
        i += 1
    return episodes


def run() -> int:
    if not RAW_PATH.exists():
        raise SystemExit(f"[episodes] нет {RAW_PATH} -- сначала sleeping_refs_hourly_divergence.py")
    df = pd.read_csv(RAW_PATH)
    df = df.sort_values(["symbol", "t"])
    print(f"[episodes] реальных часовых наблюдений: {len(df)}, тикеров: {df['symbol'].nunique()}")

    all_episodes: list[dict] = []
    sign_stats: dict = {}
    for sym, g in df.groupby("symbol"):
        eps = find_episodes(g)
        for e in eps:
            e["symbol"] = sym
        all_episodes.extend(eps)
        print(f"    {sym}: {len(eps)} реальных эпизодов (порог=${5000}, round_trip_sum={g['round_trip_sum_pct_5000'].iloc[0]:.4f}%)")

        # П.3 -- знак: систематически ли Lighter дороже/дешевле xyz
        mean_D = float(g["D"].mean())
        frac_positive = float((g["D"] > 0).mean())  # D>0 значит Lighter дороже xyz
        sign_stats[sym] = {
            "n": int(len(g)), "mean_D_pct": mean_D * 100, "median_D_pct": float(g["D"].median()) * 100,
            "frac_lighter_more_expensive": frac_positive,
        }

    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "zero_eps_pct": ZERO_EPS_PCT, "n_episodes_total": len(all_episodes),
    }

    # П.2 -- время жизни (только реально сошедшиеся в пределах данных -- честно отдельно те, что не сошлись)
    conv = [e for e in all_episodes if e["hours_to_zero"] is not None]
    not_conv = [e for e in all_episodes if e["hours_to_zero"] is None]
    half_conv = [e for e in all_episodes if e["hours_to_half"] is not None]
    print(f"\n[episodes] всего эпизодов: {len(all_episodes)}, сошлись к нулю в пределах данных: {len(conv)}, "
          f"НЕ сошлись (данные кончились раньше): {len(not_conv)}")

    if half_conv:
        hrs_half = np.array([e["hours_to_half"] for e in half_conv])
        result["hours_to_half"] = {"n": len(hrs_half), "median": float(np.median(hrs_half)), "p90": float(np.quantile(hrs_half, 0.90))}
        print(f"[episodes] часов до схождения к ПОЛОВИНЕ: медиана={result['hours_to_half']['median']:.2f}, p90={result['hours_to_half']['p90']:.2f} (N={len(hrs_half)})")
    if conv:
        hrs_zero = np.array([e["hours_to_zero"] for e in conv])
        result["hours_to_zero"] = {"n": len(hrs_zero), "median": float(np.median(hrs_zero)), "p90": float(np.quantile(hrs_zero, 0.90))}
        print(f"[episodes] часов до схождения к НУЛЮ: медиана={result['hours_to_zero']['median']:.2f}, p90={result['hours_to_zero']['p90']:.2f} (N={len(hrs_zero)})")
    result["frac_episodes_not_converged_within_data"] = float(len(not_conv) / len(all_episodes)) if all_episodes else None

    # По тикеру
    per_symbol_episodes: dict = {}
    for sym, g in pd.DataFrame(all_episodes).groupby("symbol") if all_episodes else []:
        conv_g = g[g["hours_to_zero"].notna()]
        per_symbol_episodes[sym] = {
            "n_episodes": int(len(g)),
            "median_hours_to_zero": float(conv_g["hours_to_zero"].median()) if len(conv_g) else None,
            "n_not_converged": int(g["hours_to_zero"].isna().sum()),
        }
    result["per_symbol_episodes"] = per_symbol_episodes

    # П.3 -- знак
    result["sign_stats_by_symbol"] = sign_stats
    pooled_mean_D = float(df["D"].mean())
    pooled_frac_positive = float((df["D"] > 0).mean())
    result["sign_stats_pooled"] = {"mean_D_pct": pooled_mean_D * 100, "frac_lighter_more_expensive": pooled_frac_positive}
    print(f"\n[episodes] П.3 знак -- pooled: mean(D)={pooled_mean_D*100:.4f}% "
          f"(положительно = Lighter дороже xyz), доля часов Lighter>xyz = {pooled_frac_positive:.1%}")
    print("[episodes] по тикеру:")
    for sym, s in sorted(sign_stats.items(), key=lambda kv: abs(kv[1]["mean_D_pct"]), reverse=True):
        bias = "Lighter систематически ДОРОЖЕ" if s["frac_lighter_more_expensive"] > 0.7 else (
            "Lighter систематически ДЕШЕВЛЕ" if s["frac_lighter_more_expensive"] < 0.3 else "знак СИММЕТРИЧЕН")
        print(f"    {sym}: mean(D)={s['mean_D_pct']:.4f}%, доля Lighter>xyz={s['frac_lighter_more_expensive']:.1%} -- {bias}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[episodes] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
