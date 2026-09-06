#!/usr/bin/env python3
"""Общая (без сети, без Dune) библиотека для метрик «спящих референсов»
(владелец, 2026-09-06): загрузка часовых свечей (Lighter/HL xyz),
поиск реальной "as-of"-цены на границе окна, список реальных
завершённых выходных, корреляции/доля обратных знаков, кластерный
перестановочный тест (sign-flip по тикеру), сравнение |Z1| с round-trip.

Метод "as-of"-цены (владелец не задавал явно -- решение здесь, честно
задокументировано): для произвольной границы окна (не всегда кратной
часу -- напр. 19:55, 9:30) берём CLOSE последней реальной свечи с
t <= граница (backward as-of, тот же принцип, что VWAP-брекеты в
`task1_weekend_gap.py`, только без трейд-уровня данных -- часовая
свеча вместо VWAP-брекета). Если реальной свечи в пределах
`max_lag_hours` до границы нет -- честно None, не интерполируем.

Кластерный перестановочный тест -- метод здесь выбран явно (владелец
сказал только "перестановочный тест по кластерам", не задал алгоритм):
sign-flip (Rademacher) на уровне кластера = тикер -- для каждой
перестановки для КАЖДОГО тикера целиком флипаем знак его Z-строк с
вероятностью 0.5, пересчитываем pooled corr(X,Z), эмпirical p-value =
доля |perm_corr| >= |набл. corr|. Стандартный метод для кластерных
данных (wild cluster bootstrap, Cameron/Gelbach/Miller), применим
здесь напрямую к перестановочному тесту."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from task1_weekend_gap import real_fridays_since, yfinance_daily, friday_monday_gap  # noqa: E402

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def load_hourly_csv(path: Path) -> pd.DataFrame:
    """Реальные часовые свечи (t в мс-эпохе UTC, o/h/l/c) -- общий
    формат обоих источников (Lighter markprice_1h_*.csv, HL
    hl_xyz_markprice_1h_*.csv)."""
    df = pd.read_csv(path)
    df["t"] = pd.to_numeric(df["t"], errors="coerce")
    df = df.dropna(subset=["t", "c"]).sort_values("t").reset_index(drop=True)
    df["dt_utc"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    return df


def price_asof(df: pd.DataFrame, target_utc: pd.Timestamp, max_lag_hours: float = 3.0) -> float | None:
    """CLOSE последней реальной свечи с t <= target_utc, реальный лаг
    <= max_lag_hours. None -- честно, если данных нет (до начала
    истории тикера, дыра, слишком старый лаг)."""
    eligible = df[df["dt_utc"] <= target_utc]
    if not len(eligible):
        return None
    row = eligible.iloc[-1]
    lag_hours = (target_utc - row["dt_utc"]).total_seconds() / 3600.0
    if lag_hours > max_lag_hours:
        return None
    return float(row["c"])


def et_to_utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> pd.Timestamp:
    dt_et = datetime(year, month, day, hour, minute, tzinfo=ET)
    return pd.Timestamp(dt_et.astimezone(UTC))


def weekend_dates_from_friday(friday_str: str) -> tuple[int, int, int]:
    d = datetime.strptime(friday_str, "%Y-%m-%d")
    return d.year, d.month, d.day


def stock_class_windows(friday_str: str) -> dict:
    """Класс «отдельные акции» (владелец, 2026-09-06): X = пт 20:00 ->
    вс 19:55 ET, Z1 = вс 20:00 -> 21:00 ET, Z2 = 21:00 -> пн 9:30 ET."""
    y, m, d = weekend_dates_from_friday(friday_str)
    fri = datetime(y, m, d)
    sun = fri + timedelta(days=2)
    mon = fri + timedelta(days=3)
    return {
        "x_start": et_to_utc(fri.year, fri.month, fri.day, 20, 0),
        "x_end": et_to_utc(sun.year, sun.month, sun.day, 19, 55),
        "z1_start": et_to_utc(sun.year, sun.month, sun.day, 20, 0),
        "z1_end": et_to_utc(sun.year, sun.month, sun.day, 21, 0),
        "z2_start": et_to_utc(sun.year, sun.month, sun.day, 21, 0),
        "z2_end": et_to_utc(mon.year, mon.month, mon.day, 9, 30),
    }


def macro_class_windows(friday_str: str) -> dict:
    """Класс «индексы/товары/форекс» (владелец, 2026-09-06): X = пт
    17:00 -> вс 17:55 ET, Z1 = вс 18:00 -> 19:00 ET. Владелец НЕ задал
    Z2 для этого класса (реальная причина: CME/COMEX-фьючерсы
    возобновляют почти непрерывную торговлю с 18:00 ET вс и дальше не
    "спят" до следующей пятницы -- второго тёмного под-окна физически
    нет, в отличие от акций, где NYSE открывается только в пн 9:30) --
    Z2 здесь отсутствует по конструкции, не забыто."""
    y, m, d = weekend_dates_from_friday(friday_str)
    fri = datetime(y, m, d)
    sun = fri + timedelta(days=2)
    return {
        "x_start": et_to_utc(fri.year, fri.month, fri.day, 17, 0),
        "x_end": et_to_utc(sun.year, sun.month, sun.day, 17, 55),
        "z1_start": et_to_utc(sun.year, sun.month, sun.day, 18, 0),
        "z1_end": et_to_utc(sun.year, sun.month, sun.day, 19, 0),
    }


def sign_opposite_fraction(x: pd.Series, z: pd.Series) -> float:
    return float((np.sign(x) != np.sign(z)).mean())


def cluster_sign_flip_test(x: np.ndarray, z: np.ndarray, clusters: np.ndarray,
                            n_perm: int = 5000, seed: int = 42) -> dict:
    """Кластерный перестановочный тест (sign-flip/Rademacher по
    кластеру=тикер) для corr(x,z). См. докстринг модуля."""
    x = np.asarray(x, dtype=float)
    z = np.asarray(z, dtype=float)
    clusters = np.asarray(clusters)
    if len(x) < 3 or np.std(x) == 0 or np.std(z) == 0:
        return {"observed_corr": None, "p_value": None, "n_perm": n_perm, "reason": "N<3 или нулевая дисперсия"}
    observed = float(np.corrcoef(x, z)[0, 1])
    uniq = np.unique(clusters)
    rng = np.random.default_rng(seed)
    null = np.empty(n_perm)
    idx_by_cluster = {c: np.where(clusters == c)[0] for c in uniq}
    for i in range(n_perm):
        flips = rng.choice([-1.0, 1.0], size=len(uniq))
        z_perm = z.copy()
        for c, f in zip(uniq, flips):
            z_perm[idx_by_cluster[c]] *= f
        sd = np.std(z_perm)
        null[i] = float(np.corrcoef(x, z_perm)[0, 1]) if sd > 0 else 0.0
    p = float((np.abs(null) >= abs(observed)).mean())
    return {"observed_corr": observed, "p_value": p, "n_perm": n_perm, "n_clusters": int(len(uniq))}


def round_trip_comparison(abs_z: pd.Series, round_trip_pct_500: float | None,
                           round_trip_pct_5000: float | None) -> dict:
    """Доля наблюдений, где |Z1| (в %) превышает реальный round-trip на
    номинал -- предрегистрационный критерий владельца."""
    out: dict = {}
    for label, rt in (("500", round_trip_pct_500), ("5000", round_trip_pct_5000)):
        if rt is None or pd.isna(rt):
            out[f"frac_abs_z_gt_roundtrip_{label}"] = None
            continue
        out[f"frac_abs_z_gt_roundtrip_{label}"] = float((abs_z * 100 > rt).mean())
    return out


def weekend_breakdown(df: pd.DataFrame, x_col: str, z_col: str, friday_col: str = "friday") -> list[dict]:
    """Разбивка по каждым выходным отдельно (владелец: "разбивка по
    выходным отдельно") -- не агрегат, реальные X/Z по каждой строке
    выходных (усреднённые по тикерам на эти выходные, для читаемости)."""
    out = []
    for friday, g in df.groupby(friday_col):
        out.append({
            "friday": friday, "n_tickers": int(len(g)),
            "mean_X": float(g[x_col].mean()), "mean_Z": float(g[z_col].mean()),
            "sign_opposite_fraction": sign_opposite_fraction(g[x_col], g[z_col]),
        })
    return sorted(out, key=lambda r: r["friday"])


__all__ = [
    "ET", "UTC", "load_hourly_csv", "price_asof", "et_to_utc", "stock_class_windows",
    "macro_class_windows", "sign_opposite_fraction", "cluster_sign_flip_test",
    "round_trip_comparison", "weekend_breakdown", "real_fridays_since", "yfinance_daily",
    "friday_monday_gap",
]
