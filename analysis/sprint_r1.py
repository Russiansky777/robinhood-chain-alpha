#!/usr/bin/env python3
"""Sprint R1, Шаг 2: смоук-тест полного пайплайна (чекпоинты -> девиации
-> события -> входы/выходы -> агрегаты) на одном срединном уикенде
(25-26.07.2026) для 26 ликвидных токенов с известным активным
Chainlink-фидом -- пересечение §2.2-прохождения (переигранный гейт,
194-токенная авторитетная вселенная) и наличия фида, см.
docs/R1_DESIGN.md, "Шаг 2".

ИСПРАВЛЕНО после run #24: первая версия читала сырые сделки построчно
(116069 строк за 1 день, 18.9 кредита -- проекция на 4 дня 191.9 >> 40
санитарного лимита). Правило G1/SC1 ("агрегируй на стороне Dune, чтение
результатов биллится по объёму") было нарушено. Теперь ВСЕ VWAP-окна
(цена P, вход, выходы 4ч/12ч/open+1ч) считаются в самом SQL -- наружу
идут только ~2400 маленьких агрегированных строк.

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
CHECKPOINT_TMPL = read_sql("r1/r1_smoke_checkpoint_windows")
OPEN1H_TMPL = read_sql("r1/r1_smoke_open1h")
FEED_TMPL = read_sql("r1/r1_smoke_feed_events")

# Смоук: один срединный уикенд периода (01.07-28.08.2026) -- 25-26.07.2026
# (суббота-воскресенье). Чекпоинты -- вся сетка часов в этом диапазоне
# (закрытые часы отфильтровываются локально); буфер сделок расширен под
# P-окно (-30м) и самый длинный горизонт (+12ч).
CHECKPOINT_START = "2026-07-24 00:00:00"
CHECKPOINT_END = "2026-07-27 20:00:00"
TRADES_START = "2026-07-23 23:30:00"
TRADES_END = "2026-07-28 08:00:00"
NEXT_SESSION_OPEN = pd.Timestamp("2026-07-27 13:30:00", tz="UTC")
NEXT_SESSION_OPEN1H_START = "2026-07-27 14:00:00"
NEXT_SESSION_OPEN1H_END = "2026-07-27 15:00:00"

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


def window_stats(row, prefix: str):
    """vol/qty/n -> (vwap, n, vol) или None, если ниже порогов §2.3."""
    n = row.get(f"{prefix}_n")
    if n is None or pd.isna(n) or n < CONFIG.r1_checkpoint_min_trades:
        return None
    vol = row.get(f"{prefix}_vol")
    qty = row.get(f"{prefix}_qty")
    if vol is None or pd.isna(vol) or vol < CONFIG.r1_checkpoint_min_vol_usd:
        return None
    if qty is None or pd.isna(qty) or qty <= 0:
        return None
    return vol / qty, int(n), float(vol)


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

    # ---- Основной агрегат: чекпоинт-окна (цена/вход/выходы 4ч/12ч) на стороне Dune ----
    cp_sql = render_sql(CHECKPOINT_TMPL, {
        "token_address_list": token_addr_list,
        "checkpoint_start": CHECKPOINT_START, "checkpoint_end": CHECKPOINT_END,
        "trades_start": TRADES_START, "trades_end": TRADES_END,
    })
    print("\n===== r1_smoke_checkpoint_windows (оценка 15.0, агрегат) =====")
    qidc = client.create_query("r1_smoke_checkpoint_windows", cp_sql)
    cp = client.run_sql_cached(
        "r1_smoke_checkpoint_windows", cp_sql, query_id=qidc, estimated_credits=15.0,
        expected_max_rows=3000, expected_columns=14,
    )
    if cp is None or not len(cp):
        print("[sprint_r1] 0 строк чекпоинт-окон -- нет данных.")
        return 1
    cp["t_checkpoint"] = pd.to_datetime(cp["t_checkpoint"], utc=True)
    cp["token_address"] = cp["token_address"].str.lower()
    out_cp = CACHE_DIR / "r1_smoke_checkpoint_windows.csv"
    cp.to_csv(out_cp, index=False)
    client._commit_permanent(out_cp, f"sprintR1_cache: чекпоинт-окна смоука ({len(cp)} строк) [automated]")

    # ---- Открытие+1ч (фиксированное окно, 1 строка на токен) ----
    o1_sql = render_sql(OPEN1H_TMPL, {
        "token_address_list": token_addr_list,
        "open1h_start": NEXT_SESSION_OPEN1H_START, "open1h_end": NEXT_SESSION_OPEN1H_END,
    })
    print("\n===== r1_smoke_open1h (оценка 5.0) =====")
    qido = client.create_query("r1_smoke_open1h", o1_sql)
    o1 = client.run_sql_cached(
        "r1_smoke_open1h", o1_sql, query_id=qido, estimated_credits=5.0,
        expected_max_rows=30, expected_columns=4,
    )
    open1h_map: dict[str, tuple[float, int, float] | None] = {}
    if o1 is not None and len(o1):
        o1["token_address"] = o1["token_address"].str.lower()
        for _, r in o1.iterrows():
            if r["n"] >= CONFIG.r1_checkpoint_min_trades and r["vol"] >= CONFIG.r1_checkpoint_min_vol_usd and r["qty"] > 0:
                open1h_map[r["token_address"]] = (r["vol"] / r["qty"], int(r["n"]), float(r["vol"]))
        out_o1 = CACHE_DIR / "r1_smoke_open1h.csv"
        o1.to_csv(out_o1, index=False)
        client._commit_permanent(out_o1, f"sprintR1_cache: open+1h смоука ({len(o1)} строк) [automated]")

    # ---- Фиды (метаданные-масштаб, дешёво) ----
    feed_sql = render_sql(FEED_TMPL, {
        "feed_address_list": feed_addr_list,
        "window_start": TRADES_START, "window_end": TRADES_END,
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

    # ---- Локальный пайплайн (0 кредитов): девиации -> события (non-overlap) ----
    thetas = CONFIG.r1_thetas
    events: list[dict] = []
    n_void_price = 0
    n_void_anchor = 0
    n_checkpoints_total = 0
    deviations_sample: list[float] = []

    for _, row in tf.iterrows():
        token = row["token_address"]
        feed = row["feed_address"]
        symbol = row["symbol"]
        f_events = feed_ev[feed_ev["feed_address"] == feed].sort_values("block_time")
        token_cp = cp[cp["token_address"] == token].sort_values("t_checkpoint")
        if not len(f_events) or not len(token_cp):
            continue
        skip_until = {th: pd.Timestamp.min.tz_localize("UTC") for th in thetas}
        for _, crow in token_cp.iterrows():
            t = crow["t_checkpoint"]
            if not is_closed_hour(t):
                continue
            n_checkpoints_total += 1
            pw = window_stats(crow, "p")
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
                    entry = window_stats(crow, "entry")
                    exit_4h = window_stats(crow, "exit4h")
                    exit_12h = window_stats(crow, "exit12h")
                    exit_open1h = open1h_map.get(token)
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
                    skip_until[th] = NEXT_SESSION_OPEN + pd.Timedelta(minutes=CONFIG.r1_horizon_open1h_end_min)

    ev_df = pd.DataFrame(events)
    out_events = CACHE_DIR / "r1_smoke_events.csv"
    ev_df.to_csv(out_events, index=False)
    client._commit_permanent(out_events, f"sprintR1_cache: смоук-события ({len(ev_df)} строк) [automated]")

    print("\n[sprint_r1] ===== ИТОГИ СМОУКА =====")
    print(f"Чекпоинтов всего (закрытые часы x токены с фидом): {n_checkpoints_total}")
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

    print(f"\n[sprint_r1] Записано: {out_cp}, {out_feed}, {out_events}")
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
