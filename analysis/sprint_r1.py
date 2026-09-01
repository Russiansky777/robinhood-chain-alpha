#!/usr/bin/env python3
"""Sprint R1, Шаг 2: смоук-тест полного пайплайна (чекпоинты -> девиации
-> события -> входы/выходы -> агрегаты) на одном срединном уикенде
(25-26.07.2026) для 26 ликвидных токенов с известным активным
Chainlink-фидом -- пересечение §2.2-прохождения (переигранный гейт,
194-токенная авторитетная вселенная) и наличия фида, см.
docs/R1_DESIGN.md, "Шаг 2". Проверки по заданию: разумность девиаций
(не сотни % из-за decimals!), доля пустых entry/exit-окон, факт vs
калибровка.

Использование: python analysis/sprint_r1.py --stage smoke
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import time as dtime
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintR1")
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from config import CONFIG
import credit_guard as cg
from dune_client import DuneClient, render_sql
from run_pipeline import read_sql

CACHE_DIR = Path(CONFIG.r1_cache_dir)
TRADES_TMPL = read_sql("r1/r1_smoke_trades")
FEED_TMPL = read_sql("r1/r1_smoke_feed_events")
CALIBRATION_SCALE_FACTOR = 2.5
SANITY_MAX_ESTIMATE = 40.0

# Смоук: один срединный уикенд периода (01.07-28.08.2026) -- 25-26.07.2026
# (суббота-воскресенье), окно с буфером для анкоринга чекпоинтов и
# горизонта "открытие+1ч" следующей сессии (понедельник 27.07).
SMOKE_WINDOW_START = "2026-07-24 00:00:00"
SMOKE_WINDOW_END = "2026-07-27 20:00:00"
NEXT_SESSION_OPEN = pd.Timestamp("2026-07-27 13:30:00", tz="UTC")
NEXT_SESSION_OPEN1H_END = pd.Timestamp("2026-07-27 15:00:00", tz="UTC")

DECIMALS = 8  # эмпирически подтверждено для ВСЕХ 31 фида (Шаг 2, run #17/18)

# 26 из 31 фид-токенов -- пересечение с §2.2-прохождением на
# авторитетной вселенной (docs/R1_DESIGN.md, "Шаг 2").
LIQUID_SYMBOLS = {
    "NVDA", "SPY", "GME", "AAPL", "MSTR", "TSLA", "USO", "GOOGL", "MU", "MSFT",
    "QQQ", "INTC", "COIN", "SNDK", "PLTR", "META", "SLV", "AMZN", "CRCL", "AMD",
    "USAR", "DELL", "SGOV", "ORCL", "CRWV", "RKLB",
}

MARKET_OPEN = dtime(13, 30)
MARKET_CLOSE = dtime(20, 0)


def is_closed_hour(t: pd.Timestamp) -> bool:
    if t.weekday() >= 5:
        return True
    tm = t.time()
    return tm < MARKET_OPEN or tm >= MARKET_CLOSE


def vwap_window(trades: pd.DataFrame, token: str, start: pd.Timestamp, end: pd.Timestamp):
    sub = trades[(trades["token_address"] == token) & (trades["block_time"] > start) & (trades["block_time"] <= end)]
    n = len(sub)
    if n == 0:
        return None
    vol = sub["amount_usd"].sum()
    qty = sub["token_qty"].sum()
    if n < CONFIG.r1_checkpoint_min_trades or vol < CONFIG.r1_checkpoint_min_vol_usd or qty <= 0:
        return None
    return vol / qty, n, vol


def spent_on(name: str) -> float:
    state = cg.load_state()
    total = 0.0
    for e in state.get("entries", []):
        if e.get("name") == name and e.get("namespace") == "sprintR1":
            total += e.get("credits", 0.0)
    return total


def run_smoke() -> int:
    tf_all = pd.read_csv(CACHE_DIR / "r1_token_feed_map.csv")
    tf = tf_all[tf_all["symbol"].isin(LIQUID_SYMBOLS)].reset_index(drop=True)
    print(f"[sprint_r1] Смоук на {len(tf)} токенах: {sorted(tf['symbol'].tolist())}")

    tf["token_address"] = tf["token_address"].str.lower()
    tf["feed_address"] = tf["feed_address"].str.lower()
    token_addr_list = ", ".join(f"0x{a.replace('0x', '')}" for a in tf["token_address"])
    feed_addr_list = ", ".join(f"0x{a.replace('0x', '')}" for a in tf["feed_address"])

    client = DuneClient()

    # ---- Калибровка: 1 день (25.07) перед полным окном (наследовано из G1/SC1) ----
    calib_sql = render_sql(TRADES_TMPL, {
        "token_address_list": token_addr_list,
        "window_start": "2026-07-25 00:00:00", "window_end": "2026-07-26 00:00:00",
    })
    print("\n===== r1_smoke_trades_calib (1 день, оценка 10.0) =====")
    qidc = client.create_query("r1_smoke_trades_calib", calib_sql)
    dfc = client.run_sql_cached(
        "r1_smoke_trades_calib", calib_sql, query_id=qidc, estimated_credits=10.0,
        expected_max_rows=200_000, expected_columns=4,
    )
    calib_actual = spent_on("r1_smoke_trades_calib")
    n_days = 4  # 24.07 00:00 -> 27.07 20:00 ~3.83 дня, округляем вверх
    proj_full = calib_actual * n_days * CALIBRATION_SCALE_FACTOR
    print(f"[sprint_r1] Калибровка: {len(dfc) if dfc is not None else 0} сделок за 1 день, "
          f"факт {calib_actual:.3f} кредита. Проекция на {n_days} дня x{CALIBRATION_SCALE_FACTOR}: "
          f"{proj_full:.2f}.")

    if proj_full > SANITY_MAX_ESTIMATE:
        print(f"[sprint_r1] СТОП: проекция {proj_full:.2f} > {SANITY_MAX_ESTIMATE} -- партиционирование "
              f"не реализовано для смоука (узкое окно, не ожидалось для 26 токенов за 4 дня).")
        return 1

    full_sql = render_sql(TRADES_TMPL, {
        "token_address_list": token_addr_list,
        "window_start": SMOKE_WINDOW_START, "window_end": SMOKE_WINDOW_END,
    })
    print(f"\n===== r1_smoke_trades_full (весь уикенд+буфер, оценка {max(proj_full, 5.0):.2f}) =====")
    qidf = client.create_query("r1_smoke_trades_full", full_sql)
    trades = client.run_sql_cached(
        "r1_smoke_trades_full", full_sql, query_id=qidf, estimated_credits=max(proj_full, 5.0),
        expected_max_rows=500_000, expected_columns=4,
    )
    if trades is None or not len(trades):
        print("[sprint_r1] 0 сделок в окне смоука -- нет данных для чекпоинтов.")
        return 1
    trades["block_time"] = pd.to_datetime(trades["block_time"], utc=True)
    trades["token_address"] = trades["token_address"].str.lower()
    out_trades = CACHE_DIR / "r1_smoke_trades.csv"
    trades.to_csv(out_trades, index=False)
    client._commit_permanent(out_trades, f"sprintR1_cache: смоук-сделки ({len(trades)} строк) [automated]")

    # ---- Фиды (метаданные-масштаб, дешёво) ----
    feed_sql = render_sql(FEED_TMPL, {
        "feed_address_list": feed_addr_list,
        "window_start": SMOKE_WINDOW_START, "window_end": SMOKE_WINDOW_END,
    })
    print("\n===== r1_smoke_feed_events (оценка 5.0) =====")
    qide = client.create_query("r1_smoke_feed_events", feed_sql)
    feed_ev = client.run_sql_cached(
        "r1_smoke_feed_events", feed_sql, query_id=qide, estimated_credits=5.0,
        expected_max_rows=20_000, expected_columns=3,
    )
    if feed_ev is None or not len(feed_ev):
        print("[sprint_r1] 0 обновлений фида в окне -- анкоринг невозможен.")
        return 1
    feed_ev["block_time"] = pd.to_datetime(feed_ev["block_time"], utc=True)
    feed_ev["feed_address"] = feed_ev["feed_address"].str.lower()
    feed_ev["price"] = feed_ev["current"].astype(float) / (10 ** DECIMALS)
    out_feed = CACHE_DIR / "r1_smoke_feed_events.csv"
    feed_ev.to_csv(out_feed, index=False)
    client._commit_permanent(out_feed, f"sprintR1_cache: смоук-фиды ({len(feed_ev)} строк) [automated]")

    # ---- Локальный пайплайн (0 кредитов): чекпоинты -> девиации -> события -> вход/выход ----
    checkpoint_hours = pd.date_range(SMOKE_WINDOW_START, SMOKE_WINDOW_END, freq="h", tz="UTC")
    closed_checkpoints = [t for t in checkpoint_hours if is_closed_hour(t)]
    print(f"\n[sprint_r1] Чекпоинтов в сетке закрытых часов: {len(closed_checkpoints)}")

    thetas = CONFIG.r1_thetas
    events: list[dict] = []
    n_void_price = 0
    n_void_anchor = 0
    n_checkpoints_total = 0
    deviations_sample: list[float] = []
    price_window = pd.Timedelta(minutes=CONFIG.r1_checkpoint_price_window_min)
    entry_window = pd.Timedelta(minutes=CONFIG.r1_entry_window_min)

    for _, row in tf.iterrows():
        token = row["token_address"]
        feed = row["feed_address"]
        symbol = row["symbol"]
        f_events = feed_ev[feed_ev["feed_address"] == feed].sort_values("block_time")
        if not len(f_events):
            continue
        skip_until = {th: pd.Timestamp.min.tz_localize("UTC") for th in thetas}
        for t in closed_checkpoints:
            n_checkpoints_total += 1
            pw = vwap_window(trades, token, t - price_window, t)
            if pw is None:
                n_void_price += 1
                continue
            p, n_trades_pre, vol_usd_pre = pw
            anchor_candidates = f_events[f_events["block_time"] <= t]
            if not len(anchor_candidates):
                n_void_anchor += 1
                continue
            f_price = anchor_candidates.iloc[-1]["price"]
            if f_price <= 0:
                n_void_anchor += 1
                continue
            d = float(np.log(p / f_price))
            deviations_sample.append(d)
            for th in thetas:
                if t < skip_until[th]:
                    continue
                if d <= -th:
                    entry = vwap_window(trades, token, t, t + entry_window)
                    exit_4h = vwap_window(trades, token, t + pd.Timedelta(hours=4) - price_window,
                                           t + pd.Timedelta(hours=4))
                    exit_12h = vwap_window(trades, token, t + pd.Timedelta(hours=12) - price_window,
                                            t + pd.Timedelta(hours=12))
                    exit_open1h = vwap_window(
                        trades, token,
                        NEXT_SESSION_OPEN + pd.Timedelta(minutes=CONFIG.r1_horizon_open1h_start_min),
                        NEXT_SESSION_OPEN + pd.Timedelta(minutes=CONFIG.r1_horizon_open1h_end_min),
                    )
                    events.append({
                        "token": symbol, "feed_addr": feed, "t_checkpoint": t, "theta": th, "D": d,
                        "n_trades_pre": n_trades_pre, "vol_usd_pre": vol_usd_pre,
                        "entry_vwap": entry[0] if entry else None,
                        "exit_4h": exit_4h[0] if exit_4h else None,
                        "exit_12h": exit_12h[0] if exit_12h else None,
                        "exit_open1h": exit_open1h[0] if exit_open1h else None,
                        "void_entry": entry is None, "void_exit_4h": exit_4h is None,
                        "void_exit_12h": exit_12h is None, "void_exit_open1h": exit_open1h is None,
                    })
                    skip_until[th] = NEXT_SESSION_OPEN1H_END

    ev_df = pd.DataFrame(events)
    out_events = CACHE_DIR / "r1_smoke_events.csv"
    ev_df.to_csv(out_events, index=False)
    client._commit_permanent(out_events, f"sprintR1_cache: смоук-события ({len(ev_df)} строк) [automated]")

    print("\n[sprint_r1] ===== ИТОГИ СМОУКА =====")
    print(f"Чекпоинтов всего (сетка x токены с фидом): {n_checkpoints_total}")
    print(f"Пусто по цене (P невалиден -- <3 сделок или <$500): {n_void_price} "
          f"({n_void_price / max(n_checkpoints_total, 1):.1%})")
    print(f"Пусто по анкору (нет обновления фида до t): {n_void_anchor}")
    if deviations_sample:
        dser = pd.Series(deviations_sample)
        print(f"Девиации D (ln P/F): медиана {dser.median():.4f}, диапазон "
              f"[{dser.min():.4f}; {dser.max():.4f}] -- САНИТАРНАЯ ПРОВЕРКА (§2.10): не должно быть "
              f"величин порядка десятков (|D|>>1), иначе decimals перепутаны.")
    print(f"Событий (сумма по всем theta, с учётом non-overlap): {len(ev_df)}")
    if len(ev_df):
        print(ev_df.groupby("theta").size().to_string())
        print(f"Доля пустых entry: {ev_df['void_entry'].mean():.1%}")
        print(f"Доля пустых exit_open1h: {ev_df['void_exit_open1h'].mean():.1%}")
        print(f"Доля пустых exit_4h: {ev_df['void_exit_4h'].mean():.1%}")
        print(f"Доля пустых exit_12h: {ev_df['void_exit_12h'].mean():.1%}")

    print(f"\n[sprint_r1] Записано: {out_trades}, {out_feed}, {out_events}")
    remaining = 100.0 - cg.load_state().get("sprintR1", {}).get("spent", 0.0)
    print(f"[sprint_r1] Остаток бюджета R1 (примерно): {remaining:.2f} из 100.0.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=["smoke", "full", "report"])
    args = parser.parse_args()

    if args.stage == "smoke":
        return run_smoke()
    print(f"[sprint_r1] Стадия '{args.stage}' не реализована в этом коммите.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
