#!/usr/bin/env python3
"""«Спящие референсы», метрики п.2 владельца (2026-09-06, дословно):
класс «индексы/товары/форекс» -- реальные активы HIP-3 xyz (SP500,
JP225, NIFTY, GOLD, SILVER, COPPER, EUR, GBP, JPY, секторные ETF
SMH/SOXL/XBI/XLE). Окна: X = пт 17:00 -> вс 17:55 ET, Z1 = вс 18:00 ->
19:00 ET (Z2 не определён владельцем для этого класса -- см.
`sleeping_refs_metrics_lib.macro_class_windows` докстринг: реальная
причина -- CME/COMEX почти непрерывны после вс 18:00, второго тёмного
под-окна нет).

Y -- реальные тикеры (владелец, дословно + honest lookup там, где не
назван явно):
  SP500 -> ES=F (дано), GOLD -> GC=F (дано), SILVER -> SI=F (дано),
  EUR -> EURUSD=X (дано); COPPER -> HG=F (реальный COMEX-фьючерс,
  стандартный yfinance-тикер); GBP -> GBPUSD=X, JPY -> USDJPY=X
  (направление сверено с реальным xyz mid_price -- xyz:GBP~1.35,
  xyz:JPY~156 -- совпадает с котировкой GBPUSD/USDJPY, не JPYUSD);
  JP225/NIFTY -- реальных общепринятых CME-тикеров с уверенностью НЕ
  знаем заранее, пробуем несколько РЕАЛЬНЫХ кандидатов эмпирически
  (см. `CANDIDATE_TICKERS`) и берём первый, вернувший реальные непустые
  данные -- честно фиксируем, какой сработал, не гадаем один вариант.
  Секторные ETF (SMH/SOXL/XBI/XLE) -- у CME/COMEX НЕТ прямого фьючерса
  на них (общеизвестный факт, не проверяется отдельно) -- используем
  СОБСТВЕННЫЙ тикер ETF и МЕТОД АКЦИЙ (дневной yfinance, закрытие пт
  РЕГУЛЯРНОЙ сессии -> открытие пн) как честный fallback -- ЯВНО
  помечено в результате как `y_method: "daily_etf_fallback"`, не
  подгоняется под окно класса 2 молча.

Фьючерсы/форекс -- часовой (`interval="60m"`) yfinance intraday,
as-of-цена на границе (тот же backward as-of принцип, что
`sleeping_refs_metrics_lib.price_asof`, здесь -- по колонке `dt_utc`
интрадей-фрейма). Ноль кредитов Dune."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from sleeping_refs_metrics_lib import (  # noqa: E402
    load_hourly_csv, price_asof, macro_class_windows, sign_opposite_fraction,
    cluster_sign_flip_test, round_trip_comparison, weekend_breakdown,
    real_fridays_since, yfinance_daily, friday_monday_gap,
)

OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_macro_metrics_result.json")
CACHE_DIR = Path("data/sleeping_refs_cache")
XYZ_MAIN_RESULT_PATH = Path("data/p3_guard_cache/hyperliquid_hip3_xyz_markprice_history_result.json")
XYZ_EXTRA_RESULT_PATH = Path("data/p3_guard_cache/hyperliquid_hip3_xyz_extra_assets_history_result.json")

# ticker -> (yfinance-кандидаты Y, "futures_fx" | "etf_daily")
FUTURES_FX_TICKERS = {
    "SP500": (["ES=F"], "futures_fx"),
    "GOLD": (["GC=F"], "futures_fx"),
    "SILVER": (["SI=F"], "futures_fx"),
    "COPPER": (["HG=F"], "futures_fx"),
    "EUR": (["EURUSD=X"], "futures_fx"),
    "GBP": (["GBPUSD=X"], "futures_fx"),
    "JPY": (["USDJPY=X"], "futures_fx"),
    "JP225": (["NKD=F", "^N225", "NIY=F"], "futures_fx"),
    "NIFTY": (["^NSEI", "NIFTY_50.NS"], "futures_fx"),
}
ETF_TICKERS = {"SMH": "SMH", "SOXL": "SOXL", "XBI": "XBI", "XLE": "XLE"}


def build_universe() -> dict:
    xyz_main = json.loads(XYZ_MAIN_RESULT_PATH.read_text())["assets"]
    xyz_extra = json.loads(XYZ_EXTRA_RESULT_PATH.read_text())["assets"] if XYZ_EXTRA_RESULT_PATH.exists() else {}
    combined = {**xyz_main, **xyz_extra}

    universe: dict = {}
    for ticker in list(FUTURES_FX_TICKERS) + list(ETF_TICKERS):
        m = combined.get(ticker)
        if m is None:
            print(f"[macro_metrics] !!! {ticker}: реально нет данных xyz (не был получен) -- пропуск")
            continue
        csv_path = CACHE_DIR / f"hl_xyz_markprice_1h_{ticker}.csv"
        if not csv_path.exists():
            print(f"[macro_metrics] !!! {ticker}: CSV свечей не найден ({csv_path}) -- пропуск")
            continue
        universe[ticker] = {
            "csv": csv_path, "round_trip_pct_500": m.get("round_trip_cost_pct_500"),
            "round_trip_pct_5000": m.get("round_trip_cost_pct_5000"),
        }
    print(f"[macro_metrics] реальный универсум ({len(universe)}): {sorted(universe)}")
    return universe


def yfinance_intraday(symbol: str, start_date: str, end_date: str) -> tuple[pd.DataFrame | None, str | None]:
    """Реальные часовые OHLC intraday с yfinance. Возвращает (df, tz_used)
    -- df с колонками dt_utc/Open/Close, УЖЕ сконвертированными в UTC
    (абсолютное время, безопасно вне зависимости от исходной tz
    ответа)."""
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(start=start_date, end=end_date, interval="60m")
    except Exception as e:  # noqa: BLE001
        print(f"    yfinance intraday {symbol}: сетевая/библиотечная ошибка {e}")
        return None, None
    if hist is None or not len(hist):
        return None, None
    hist = hist.reset_index()
    time_col = "Datetime" if "Datetime" in hist.columns else hist.columns[0]
    tz_repr = str(hist[time_col].dt.tz) if hasattr(hist[time_col].dt, "tz") else "naive(!)"
    hist["dt_utc"] = pd.to_datetime(hist[time_col], utc=True)
    return hist[["dt_utc", "Open", "Close"]], tz_repr


def intraday_price_asof(df: pd.DataFrame, target_utc: pd.Timestamp, max_lag_hours: float = 72.0) -> float | None:
    """Реальный, эмпирически найденный факт (`sleeping_refs_yfinance_
    intraday_diag.py`, GH Actions run 34035843308): у ES=F (и, судя по
    структуре рынка, у всех CME/COMEX/форекс-тикеров) РЕАЛЬНЫЙ разрыв в
    часовых данных Yahoo между пт 16:00 ET (последний реальный бар) и
    вс 18:00 ET (первый реальный бар после выходных) -- т.е. РОВНО
    столько, сколько и должно быть по структуре рынка (CME закрыт).
    Дефолт 3.0ч, унаследованный от `price_asof` (наши СОБСТВЕННЫЕ 24/7
    свечи xyz, где разрывов в принципе нет), был ОШИБОЧНО мал здесь --
    граница X_end (вс 17:55 ET) специально стоит ВНУТРИ этого реального
    разрыва (последний доступный принт -- пятничный), лаг там реально
    ~50ч, не 3ч. 72ч -- реальный запас с несколькими часами сверху
    факта, не подогнано впритык."""
    eligible = df[df["dt_utc"] <= target_utc]
    if not len(eligible):
        return None
    row = eligible.iloc[-1]
    lag_hours = (target_utc - row["dt_utc"]).total_seconds() / 3600.0
    if lag_hours > max_lag_hours:
        return None
    return float(row["Close"])


def intraday_price_first_after(df: pd.DataFrame, target_utc: pd.Timestamp, max_lead_hours: float = 6.0) -> float | None:
    """РЕАЛЬНЫЙ фикс второго бага, найденного по факту (Y=0.00000
    буквально для КАЖДОЙ строки GOLD/SP500/... после первого фикса):
    `intraday_price_asof` на границе x_end (вс 17:55 ET) backward-asof
    резолвится в ТОТ ЖЕ пятничный принт, что и x_start (вс 17:55 ET
    находится ВНУТРИ реального разрыва биржи -- рынок физически ещё не
    открылся) -- разность тождественно 0, это НЕ измерение, а
    тавтология. Реальный экономический смысл Y здесь -- гэп РЕАЛЬНОГО
    рынка через ЕГО собственное закрытие: цена ПЕРЕД закрытием (Fri, тот
    же `intraday_price_asof` на x_start -- корректно, лаг ~1ч, реально
    проверено) -> цена ПРИ реальном возобновлении торгов (первый
    реальный принт >= границы, forward-поиск, НЕ backward-asof).
    Используется как цель вс 18:00 ET (`z1_start` -- то же самое время,
    что и реальное открытие CME Globex по спецификации владельца)."""
    eligible = df[df["dt_utc"] >= target_utc]
    if not len(eligible):
        return None
    row = eligible.iloc[0]
    lead_hours = (row["dt_utc"] - target_utc).total_seconds() / 3600.0
    if lead_hours > max_lead_hours:
        return None
    return float(row["Open"])


def resolve_y_source(ticker: str, start_date: str, end_date: str) -> dict:
    """Реальный выбор рабочего Y-тикера/метода -- пробуем кандидатов,
    честно фиксируем, какой реально сработал."""
    if ticker in ETF_TICKERS:
        daily = yfinance_daily(ETF_TICKERS[ticker], start_date, end_date)
        return {"y_method": "daily_etf_fallback", "y_ticker_used": ETF_TICKERS[ticker],
                "daily": daily, "intraday": None, "tz": None}

    candidates, _ = FUTURES_FX_TICKERS[ticker]
    for cand in candidates:
        intraday, tz_repr = yfinance_intraday(cand, start_date, end_date)
        time.sleep(0.3)
        if intraday is not None and len(intraday) > 10:
            print(f"    {ticker}: реальный рабочий Y-тикер = {cand} ({len(intraday)} часовых баров, tz={tz_repr})")
            return {"y_method": "futures_fx_intraday", "y_ticker_used": cand, "daily": None,
                    "intraday": intraday, "tz": tz_repr}
        print(f"    {ticker}: кандидат {cand} НЕ дал реальных данных -- пробуем следующий" if len(candidates) > 1
              else f"    {ticker}: кандидат {cand} НЕ дал реальных данных")
    return {"y_method": "no_real_y_found", "y_ticker_used": None, "daily": None, "intraday": None, "tz": None}


def compute_rows(universe: dict, fridays: list[str]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    drop_reasons: dict[str, int] = {}

    def bump(reason: str) -> None:
        drop_reasons[reason] = drop_reasons.get(reason, 0) + 1

    y_start = fridays[0]
    y_end = (datetime.strptime(fridays[-1], "%Y-%m-%d") + timedelta(days=4)).strftime("%Y-%m-%d")

    for ticker, meta in universe.items():
        candles = load_hourly_csv(meta["csv"])
        y_src = resolve_y_source(ticker, y_start, y_end)
        if y_src["y_method"] == "no_real_y_found":
            bump(f"{ticker}: реального Y-тикера не нашлось ни у одного кандидата")
            continue

        for friday in fridays:
            w = macro_class_windows(friday)
            x_start_p = price_asof(candles, w["x_start"])
            x_end_p = price_asof(candles, w["x_end"])
            z1_start_p = price_asof(candles, w["z1_start"])
            z1_end_p = price_asof(candles, w["z1_end"])
            if None in (x_start_p, x_end_p, z1_start_p, z1_end_p):
                bump(f"{ticker}: нет реальных свечей xyz в пределах допуска для {friday}")
                continue

            if y_src["y_method"] == "daily_etf_fallback":
                if y_src["daily"] is None:
                    bump(f"{ticker}: yfinance daily (ETF) недоступен")
                    continue
                y = friday_monday_gap(y_src["daily"], friday)
                if y.get("gap") is None:
                    bump(f"{ticker}: Y (ETF daily) недоступен для {friday} -- {y.get('reason')}")
                    continue
                y_val = y["gap"]
            else:
                y_before = intraday_price_asof(y_src["intraday"], w["x_start"])
                y_after = intraday_price_first_after(y_src["intraday"], w["z1_start"])
                if y_before is None or y_after is None or y_before <= 0:
                    bump(f"{ticker}: реального Y (intraday) нет в пределах допуска для {friday}")
                    continue
                y_val = y_after / y_before - 1

            rows.append({
                "symbol": ticker, "friday": friday,
                "X": x_end_p / x_start_p - 1, "Z1": z1_end_p / z1_start_p - 1, "Y": y_val,
                "y_method": y_src["y_method"], "y_ticker_used": y_src["y_ticker_used"],
                "round_trip_pct_500": meta.get("round_trip_pct_500"),
                "round_trip_pct_5000": meta.get("round_trip_pct_5000"),
            })
    return rows, drop_reasons


def compute_metrics(df: pd.DataFrame) -> dict:
    result: dict = {"n": len(df)}
    if len(df) < 3:
        result["note"] = f"N={len(df)} слишком мал для корреляций -- честно доложить, не считать"
        return result

    result["corr_X_Y"] = float(df["X"].corr(df["Y"]))
    result["corr_X_Z1"] = float(df["X"].corr(df["Z1"]))
    result["sign_opposite_fraction_X_Y"] = sign_opposite_fraction(df["X"], df["Y"])
    result["sign_opposite_fraction_X_Z1"] = sign_opposite_fraction(df["X"], df["Z1"])
    result["weekend_breakdown_X_Z1"] = weekend_breakdown(df, "X", "Z1")

    clusters = df["symbol"].to_numpy()
    result["cluster_permutation_test"] = {
        "X_Y": cluster_sign_flip_test(df["X"].to_numpy(), df["Y"].to_numpy(), clusters),
        "X_Z1": cluster_sign_flip_test(df["X"].to_numpy(), df["Z1"].to_numpy(), clusters),
    }

    rt500 = df.groupby("symbol")["round_trip_pct_500"].first()
    rt5000 = df.groupby("symbol")["round_trip_pct_5000"].first()
    per_ticker_rt: dict[str, dict] = {}
    for sym, g in df.groupby("symbol"):
        per_ticker_rt[sym] = round_trip_comparison(g["Z1"].abs(), rt500.get(sym), rt5000.get(sym))
    result["round_trip_comparison_by_ticker"] = per_ticker_rt
    valid_5000 = [v["frac_abs_z_gt_roundtrip_5000"] for v in per_ticker_rt.values() if v["frac_abs_z_gt_roundtrip_5000"] is not None]
    result["mean_frac_abs_z1_gt_roundtrip_5000"] = float(np.mean(valid_5000)) if valid_5000 else None

    corr_xy = result["corr_X_Y"]
    n = result["n"]
    frac5000 = result["mean_frac_abs_z1_gt_roundtrip_5000"]
    alive = (abs(corr_xy) < 0.3 and result["corr_X_Z1"] < -0.3 and n >= 30
             and frac5000 is not None and frac5000 > 0.5)
    result["preregistered_verdict"] = (
        f"{'ЖИВА' if alive else 'НЕ подтверждено по предрегистрации'}: "
        f"|corr(X,Y)|={abs(corr_xy):.3f}(<0.3?), corr(X,Z1)={result['corr_X_Z1']:.3f}(<-0.3?), "
        f"N={n}(>=30?), frac|Z1|>rt5000={frac5000}(>0.5?) -- "
        f"ПРЕДУПРЕЖДЕНИЕ: для этого класса Z2 не определён (см. докстринг), N считается по X/Z1/Y, "
        f"не путать с классом акций"
    )
    return result


def run() -> int:
    universe = build_universe()
    if not universe:
        print("[macro_metrics] реальный универсум пуст -- нечего считать")
        return 1

    now_utc = datetime.now(timezone.utc)
    all_fridays = real_fridays_since("2026-07-01", now_utc)
    print(f"[macro_metrics] реальных завершённых выходных с 2026-07-01: {len(all_fridays)} -- {all_fridays}")

    rows, drops = compute_rows(universe, all_fridays)
    print(f"\n[macro_metrics] реальных строк (тикер x выходные) с полными X/Z1/Y: {len(rows)}")
    for reason, cnt in drops.items():
        print(f"    пропущено: {reason} (n={cnt})")

    df = pd.DataFrame(rows)
    metrics = compute_metrics(df)
    print("\n=== ИТОГО (все активы класса пула, объединённая корреляция) ===")
    for k in ("corr_X_Y", "corr_X_Z1", "sign_opposite_fraction_X_Y", "sign_opposite_fraction_X_Z1",
              "mean_frac_abs_z1_gt_roundtrip_5000", "preregistered_verdict"):
        if k in metrics:
            print(f"  {k}: {metrics[k]}")

    # Отдельно по каждому тикеру (владелец просил метрики "по каждому активу")
    per_asset: dict[str, dict] = {}
    for sym, g in df.groupby("symbol") if len(df) else []:
        per_asset[sym] = compute_metrics(g)
        print(f"\n--- {sym} (N={len(g)}) ---")
        for k in ("corr_X_Y", "corr_X_Z1", "sign_opposite_fraction_X_Y", "sign_opposite_fraction_X_Z1"):
            if k in per_asset[sym]:
                print(f"  {k}: {per_asset[sym][k]}")

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "weekends_considered": all_fridays, "universe": sorted(universe),
        "drop_reasons": drops, "pooled_metrics": metrics, "per_asset_metrics": per_asset,
        "rows": rows,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[macro_metrics] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
