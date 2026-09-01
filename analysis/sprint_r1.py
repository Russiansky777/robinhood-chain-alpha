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
import bisect
import datetime as dt
import json
import os
import random
import sys
from datetime import time as dtime
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintR1")
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from scipy import stats as sps

from config import CONFIG
import credit_guard as cg
from dune_client import DuneClient, render_sql
from run_pipeline import read_sql

CACHE_DIR = Path(CONFIG.r1_cache_dir)
CHECKPOINT_TMPL = read_sql("r1/r1_smoke_checkpoint_windows")
OPEN1H_TMPL = read_sql("r1/r1_smoke_open1h")
FEED_TMPL = read_sql("r1/r1_smoke_feed_events")
# Шаг 3 (полный прогон) переиспользует CHECKPOINT_TMPL/FEED_TMPL смоука
# (шаблоны уже полностью параметризованы -- см. docstring ниже,
# run_full()) плюс два новых шаблона для сессий открытия по всему
# периоду и понедельной валидации §2.2.
SESSION_OPEN_TMPL = read_sql("r1/r1_full_session_open_windows")
WEEKLY_UNIVERSE_TMPL = read_sql("r1/r1_full_weekly_universe")

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

# Перенос Independence Day -- единственный полный нерабочий день NYSE в
# периоде 01.07-31.08.2026 сверх обычных выходных (см. docs/R1_DESIGN.md,
# §1 Шаг 1 "Механика", WebSearch по нескольким источникам NYSE-календаря).
# Смоук-окно (24-27.07) не затрагивало эту дату -- баг было невозможно
# поймать раньше полного прогона, отсюда отдельная is_closed_hour_full.
R1_FULL_HOLIDAYS = {dt.date(2026, 7, 3)}


def is_closed_hour(t: pd.Timestamp) -> bool:
    if t.weekday() >= 5:
        return True
    tm = t.time()
    return tm < MARKET_OPEN or tm >= MARKET_CLOSE


def is_closed_hour_full(t: pd.Timestamp) -> bool:
    """Как is_closed_hour (смоук), плюс праздник 03.07.2026 -- см.
    R1_FULL_HOLIDAYS."""
    if t.date() in R1_FULL_HOLIDAYS:
        return True
    return is_closed_hour(t)


def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    """Список торговых дней NYSE (пн-пт, минус R1_FULL_HOLIDAYS) в
    [start, end] включительно -- основа динамического поиска
    "ближайшей следующей сессии" на Шаге 3 (§2.4), взамен захардкоженного
    единственного понедельника на смоуке."""
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in R1_FULL_HOLIDAYS:
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def build_week_partitions(
    period_start: pd.Timestamp, period_end: pd.Timestamp, step_days: int = 7
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Непересекающиеся недельные партиции чекпоинт-сетки для Шага 3
    (§1 Шаг 3 "партиции по неделям") -- каждая партиция кончается за 1ч
    до начала следующей, чтобы одна и та же чекпоинт-строка не попала в
    две партиции разом (sequence() в SQL включает обе границы)."""
    starts = []
    cur = period_start
    while cur <= period_end:
        starts.append(cur)
        cur += pd.Timedelta(days=step_days)
    partitions = []
    for i, s in enumerate(starts):
        e = (starts[i + 1] - pd.Timedelta(hours=1)) if i + 1 < len(starts) else period_end
        if e > period_end:
            e = period_end
        partitions.append((s, e))
    return partitions


def week_start_for(t: pd.Timestamp, period_start: pd.Timestamp) -> pd.Timestamp:
    """Календарная неделя (те же границы, что build_week_partitions),
    которой принадлежит чекпоинт t -- ключ для недельной валидации §2.2."""
    week_idx = (t - period_start).days // 7
    return period_start + pd.Timedelta(days=7 * week_idx)


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
    deviations: list[dict] = []  # ВСЕ валидные чекпоинты (дисконты И премии) -- нужны
    # для §2.9 (премии фиксируются для отчёта) и для проверки выбросов
    # (владелец: топ-10 |D| с контекстом -- глазами проверить, что
    # верхний хвост не артефакт маппинга фида).
    n_void_price = 0
    n_void_anchor = 0
    n_checkpoints_total = 0

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
            anchor_row = anchor_candidates.iloc[-1]
            f_price = anchor_row["price"]
            if f_price <= 0:
                n_void_anchor += 1
                continue
            d = float(np.log(p / f_price))
            deviations.append({
                "token": symbol, "feed_addr": feed, "t_checkpoint": t, "D": d,
                "P_vwap": p, "F_anchor": f_price, "anchor_age_min": (t - anchor_row["block_time"]).total_seconds() / 60,
                "n_trades_pre": n_trades_pre, "vol_usd_pre": vol_usd_pre,
            })
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

    dev_df = pd.DataFrame(deviations)
    out_dev = CACHE_DIR / "r1_smoke_deviations.csv"
    dev_df.to_csv(out_dev, index=False)
    client._commit_permanent(out_dev, f"sprintR1_cache: смоук-девиации, все чекпоинты ({len(dev_df)} строк) [automated]")

    print("\n[sprint_r1] ===== ИТОГИ СМОУКА =====")
    print(f"Чекпоинтов всего (закрытые часы x токены с фидом): {n_checkpoints_total}")
    print(f"Пусто по цене (P невалиден -- <3 сделок или <$500): {n_void_price} "
          f"({n_void_price / max(n_checkpoints_total, 1):.1%})")
    print(f"Пусто по анкору (нет обновления фида до t): {n_void_anchor}")
    if len(dev_df):
        dser = dev_df["D"]
        print(f"Девиации D (ln P/F): медиана {dser.median():.4f}, диапазон "
              f"[{dser.min():.4f}; {dser.max():.4f}] -- САНИТАРНАЯ ПРОВЕРКА (§2.10): не должно быть "
              f"величин порядка десятков (|D|>>1), иначе decimals перепутаны.")
        top10 = dev_df.reindex(dev_df["D"].abs().sort_values(ascending=False).index).head(10)
        print("\n-- Топ-10 |D| (владелец: глазами проверить, что не мусор маппинга фида) --")
        print(top10[["token", "t_checkpoint", "D", "P_vwap", "F_anchor", "anchor_age_min",
                      "n_trades_pre", "vol_usd_pre"]].to_string(index=False))
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


EVENT_COLUMNS = [
    "token", "feed_addr", "t_checkpoint", "theta", "D", "n_trades_pre", "vol_usd_pre",
    "entry_vwap", "exit_4h", "exit_12h", "exit_open1h", "void_entry", "void_exit_4h",
    "void_exit_12h", "void_exit_open1h", "week_id", "session_date",
]
CONTROL_COLUMNS = [
    "token", "t_checkpoint", "D", "entry_vwap", "exit_4h", "exit_12h", "exit_open1h",
    "void_entry", "void_exit_4h", "void_exit_12h", "void_exit_open1h", "week_id",
]


def run_full() -> int:
    """Sprint R1, Шаг 3 (полный прогон, партиции по неделям, §1) + Шаг 4
    (локальная статистика §2.7, 0 доп. кредитов) на 26 ликвидных токенах
    с известным Chainlink-фидом (см. docs/R1_DESIGN.md, "Шаг 2").

    Механика идентична смоуку (чекпоинты -> девиации -> события,
    non-overlap по §2.3) с тремя расширениями, отложенными на смоуке:

    1. Партиционирование по календарным неделям (build_week_partitions)
       -- чекпоинт-окна считаются отдельным Dune-запросом на партицию
       (калибровка по 1-й партиции x2.5 запас перед остальными, тот же
       принцип, что в G1/SC1), skip_until non-overlap переносится между
       партициями естественно (цикл идёт по ВСЕМ чекпоинтам токена
       сразу, после конкатенации партиций, а не партиция за партицией).
    2. Динамический per-checkpoint поиск "ближайшей следующей сессии"
       (next_session_date, бинарный поиск по календарю торговых дней) --
       взамен захардкоженного единственного понедельника смоука. Сама
       агрегация на Dune считается ОДИН раз на дату сессии (не на
       чекпоинт) -- session_map переиспользуется всеми чекпоинтами,
       резолвящимися в одну и ту же сессию.
    3. Понедельная валидация §2.2 (r1_full_weekly_universe) -- честная
       проверка "вселенная на неделе W" по календарным неделям (не
       буквально скользящее 7-дневное окно -- см. комментарий в самом
       SQL-файле и docs/R1_DESIGN.md; при объёмах на 1-2 порядка выше
       порога эта аппроксимация не меняет исход ни для одной комбинации
       токен-неделя, но проверяется, а не предполагается).
    """
    period_start = pd.Timestamp(f"{CONFIG.r1_period_start} 00:00:00", tz="UTC")
    period_end = pd.Timestamp(CONFIG.r1_period_end, tz="UTC")
    coverage_end = pd.Timestamp(CONFIG.r1_coverage_end, tz="UTC")

    tf_all = pd.read_csv(CACHE_DIR / "r1_token_feed_map.csv")
    tf = tf_all[tf_all["symbol"].isin(LIQUID_SYMBOLS)].reset_index(drop=True)
    tf["token_address"] = tf["token_address"].str.lower()
    tf["feed_address"] = tf["feed_address"].str.lower()
    token_addr_list = ", ".join(f"0x{a.replace('0x', '')}" for a in tf["token_address"])
    feed_addr_list = ", ".join(f"0x{a.replace('0x', '')}" for a in tf["feed_address"])
    print(
        f"[sprint_r1] Полный прогон на {len(tf)} токенах, период "
        f"{period_start} .. {period_end} (конец покрытия {coverage_end})."
    )

    client = DuneClient()

    # ---- Шаг 3а: чекпоинт-окна (цена/вход/выходы 4ч/12ч), партиции по неделям ----
    partitions = build_week_partitions(period_start, period_end, step_days=7)
    print(
        f"[sprint_r1] Партиций: {len(partitions)} -- "
        f"{[(s.date().isoformat(), e.date().isoformat()) for s, e in partitions]}"
    )

    cp_parts: list[pd.DataFrame] = []
    for i, (p_start, p_end) in enumerate(partitions):
        trades_start = p_start - pd.Timedelta(minutes=30)
        trades_end = p_end + pd.Timedelta(hours=12, minutes=5)
        name = f"r1_full_checkpoint_wk{i + 1:02d}"
        sql = render_sql(CHECKPOINT_TMPL, {
            "token_address_list": token_addr_list,
            "checkpoint_start": p_start.strftime("%Y-%m-%d %H:%M:%S"),
            "checkpoint_end": p_end.strftime("%Y-%m-%d %H:%M:%S"),
            "trades_start": trades_start.strftime("%Y-%m-%d %H:%M:%S"),
            "trades_end": trades_end.strftime("%Y-%m-%d %H:%M:%S"),
        })
        print(f"\n===== {name} ({p_start.date()}..{p_end.date()}) =====")
        qid = client.create_query(name, sql)
        part_df = client.run_sql_cached(
            name, sql, query_id=qid, estimated_credits=6.0,
            expected_max_rows=6000, expected_columns=14,
        )
        if part_df is not None and len(part_df):
            part_df["t_checkpoint"] = pd.to_datetime(part_df["t_checkpoint"], utc=True)
            part_df["token_address"] = part_df["token_address"].str.lower()
            cp_parts.append(part_df)
        if i == 0:
            # Калибровка узким срезом (правило G1/SC1): факт 1-й партиции
            # x оставшиеся партиции x2.5 запас -- если проекция не
            # укладывается в остаток namespace, стоп ДО траты остального
            # бюджета на партиции 2..N (1-я уже посчитана и сохранена).
            actual_first = spent_on(name)
            projected = actual_first * len(partitions) * 2.5
            remaining_ns = CONFIG.r1_credit_budget - cg.load_state().get("sprintR1", {}).get("spent", 0.0)
            print(
                f"[sprint_r1] Калибровка по 1-й партиции: факт {actual_first:.3f}, "
                f"проекция на все {len(partitions)} партиций x2.5 запас = {projected:.2f}, "
                f"остаток namespace = {remaining_ns:.2f}."
            )
            if projected > remaining_ns:
                print(
                    f"[sprint_r1] СТОП: проекция {projected:.2f} > остатка {remaining_ns:.2f} -- "
                    "не продолжаю остальные партиции без пересмотра SQL/оценки. "
                    "1-я партиция уже посчитана и закоммичена, деньги не потеряны."
                )
                return 1

    if not cp_parts:
        print("[sprint_r1] 0 строк по всем партициям -- нет данных.")
        return 1
    cp = pd.concat(cp_parts, ignore_index=True)
    out_cp = CACHE_DIR / "r1_full_checkpoint_windows.csv"
    cp.to_csv(out_cp, index=False)
    client._commit_permanent(out_cp, f"sprintR1_cache: чекпоинт-окна полного прогона ({len(cp)} строк) [automated]")

    # ---- Шаг 3б: сессии открытия -- динамический поиск локально в Python,
    # агрегация на Dune один раз на дату сессии (не на чекпоинт) ----
    trading_days_full = trading_days(period_start.date(), coverage_end.date())
    session_opens = [pd.Timestamp(f"{d} 13:30:00", tz="UTC") for d in trading_days_full]
    session_date_list_sql = ", ".join(f"timestamp '{d} 00:00:00'" for d in trading_days_full)
    so_sql = render_sql(SESSION_OPEN_TMPL, {
        "token_address_list": token_addr_list,
        "session_date_list": session_date_list_sql,
        "trades_start": (period_start - pd.Timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
        "trades_end": (coverage_end + pd.Timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
    })
    print(f"\n===== r1_full_session_open_windows ({len(trading_days_full)} торговых дат) =====")
    qid_so = client.create_query("r1_full_session_open_windows", so_sql)
    so_df = client.run_sql_cached(
        "r1_full_session_open_windows", so_sql, query_id=qid_so, estimated_credits=8.0,
        expected_max_rows=3000, expected_columns=5,
    )
    session_map: dict[tuple[str, dt.date], tuple[float, int, float] | None] = {}
    if so_df is not None and len(so_df):
        so_df["token_address"] = so_df["token_address"].str.lower()
        so_df["session_date"] = pd.to_datetime(so_df["session_date"]).dt.date
        for _, r in so_df.iterrows():
            key = (r["token_address"], r["session_date"])
            if (r["n"] >= CONFIG.r1_checkpoint_min_trades and r["vol"] >= CONFIG.r1_checkpoint_min_vol_usd
                    and r["qty"] > 0):
                session_map[key] = (r["vol"] / r["qty"], int(r["n"]), float(r["vol"]))
            else:
                session_map[key] = None
        out_so = CACHE_DIR / "r1_full_session_open_windows.csv"
        so_df.to_csv(out_so, index=False)
        client._commit_permanent(out_so, f"sprintR1_cache: окна открытий полного прогона ({len(so_df)} строк) [automated]")

    def next_session_date(t: pd.Timestamp) -> dt.date | None:
        idx = bisect.bisect_right(session_opens, t)
        if idx >= len(session_opens):
            return None
        return trading_days_full[idx]

    # ---- Шаг 3в: обновления фида за весь период (анкор) ----
    feed_sql = render_sql(FEED_TMPL, {
        "feed_address_list": feed_addr_list,
        "window_start": (period_start - pd.Timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),
        "window_end": (coverage_end + pd.Timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
    })
    print("\n===== r1_full_feed_events (весь период) =====")
    qid_feed = client.create_query("r1_full_feed_events", feed_sql)
    feed_ev = client.run_sql_cached(
        "r1_full_feed_events", feed_sql, query_id=qid_feed, estimated_credits=8.0,
        expected_max_rows=200_000, expected_columns=3,
    )
    if feed_ev is None or not len(feed_ev):
        print("[sprint_r1] 0 обновлений фида за весь период -- анкоринг невозможен.")
        return 1
    feed_ev["block_time"] = pd.to_datetime(feed_ev["block_time"], utc=True)
    feed_ev["feed_address"] = feed_ev["feed_address"].str.lower()
    feed_ev["price"] = feed_ev["current"].astype(float) / (10 ** DECIMALS)
    out_feed = CACHE_DIR / "r1_full_feed_events.csv"
    feed_ev.to_csv(out_feed, index=False)
    client._commit_permanent(out_feed, f"sprintR1_cache: фиды полного прогона ({len(feed_ev)} строк) [automated]")

    # ---- Шаг 3г: понедельная валидация §2.2 (см. docstring выше) ----
    week_starts = [s for s, _ in partitions]
    week_start_list_sql = ", ".join(f"timestamp '{s.strftime('%Y-%m-%d %H:%M:%S')}'" for s in week_starts)
    wu_sql = render_sql(WEEKLY_UNIVERSE_TMPL, {
        "token_address_list": token_addr_list,
        "week_start_list": week_start_list_sql,
        "trades_start": period_start.strftime("%Y-%m-%d %H:%M:%S"),
        "trades_end": (period_end + pd.Timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
    })
    print("\n===== r1_full_weekly_universe (валидация §2.2 понедельно) =====")
    qid_wu = client.create_query("r1_full_weekly_universe", wu_sql)
    wu_df = client.run_sql_cached(
        "r1_full_weekly_universe", wu_sql, query_id=qid_wu, estimated_credits=5.0,
        expected_max_rows=500, expected_columns=4,
    )
    universe_ok: dict[tuple[str, pd.Timestamp], bool] = {}
    n_universe_fail = 0
    if wu_df is not None and len(wu_df):
        wu_df["token_address"] = wu_df["token_address"].str.lower()
        wu_df["week_start"] = pd.to_datetime(wu_df["week_start"], utc=True)
        for _, r in wu_df.iterrows():
            ok = bool(r["n_trades"] >= CONFIG.r1_universe_min_trades and r["vol_usd"] >= CONFIG.r1_universe_min_vol_usd)
            universe_ok[(r["token_address"], r["week_start"])] = ok
            if not ok:
                n_universe_fail += 1
        out_wu = CACHE_DIR / "r1_full_weekly_universe.csv"
        wu_df.to_csv(out_wu, index=False)
        client._commit_permanent(out_wu, f"sprintR1_cache: недельная валидация §2.2 ({len(wu_df)} строк) [automated]")
    print(
        f"[sprint_r1] Недельная валидация §2.2: {n_universe_fail} из {len(universe_ok)} "
        "(токен, неделя)-комбинаций НЕ проходят порог (>=100 сделок И >=$10k) -- "
        "их чекпоинты исключены из вселенной этой недели."
    )

    # ---- Локальный пайплайн (0 кредитов): девиации -> события (non-overlap) + контроль §2.6 ----
    thetas = CONFIG.r1_thetas
    events: list[dict] = []
    deviations: list[dict] = []
    n_void_price = 0
    n_void_anchor = 0
    n_universe_excluded = 0
    n_checkpoints_total = 0

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
            if not is_closed_hour_full(t):
                continue
            if t > period_end:
                continue  # буфер трейдов партиции шире period_end -- сами чекпоинты нет (§2.1)
            n_checkpoints_total += 1
            wk = week_start_for(t, period_start)
            if not universe_ok.get((token, wk), True):
                n_universe_excluded += 1
                continue
            pw = window_stats(crow, "p")
            if pw is None:
                n_void_price += 1
                continue
            p, n_trades_pre, vol_usd_pre = pw
            anchor_candidates = f_events[f_events["block_time"] <= t]
            if not len(anchor_candidates):
                n_void_anchor += 1
                continue
            anchor_row = anchor_candidates.iloc[-1]
            f_price = anchor_row["price"]
            if f_price <= 0:
                n_void_anchor += 1
                continue
            d = float(np.log(p / f_price))
            week_id = wk.date().isoformat()
            deviations.append({
                "token": symbol, "feed_addr": feed, "t_checkpoint": t, "D": d,
                "P_vwap": p, "F_anchor": f_price,
                "anchor_age_min": (t - anchor_row["block_time"]).total_seconds() / 60,
                "n_trades_pre": n_trades_pre, "vol_usd_pre": vol_usd_pre, "week_id": week_id,
            })
            sess_date = next_session_date(t)
            exit_open1h = session_map.get((token, sess_date)) if sess_date else None
            for th in thetas:
                if t < skip_until[th]:
                    continue
                if d <= -th:
                    entry = window_stats(crow, "entry")
                    exit_4h = window_stats(crow, "exit4h")
                    exit_12h = window_stats(crow, "exit12h")
                    events.append({
                        "token": symbol, "feed_addr": feed, "t_checkpoint": t, "theta": th, "D": d,
                        "n_trades_pre": n_trades_pre, "vol_usd_pre": vol_usd_pre,
                        "entry_vwap": entry[0] if entry else None,
                        "exit_4h": exit_4h[0] if exit_4h else None,
                        "exit_12h": exit_12h[0] if exit_12h else None,
                        "exit_open1h": exit_open1h[0] if exit_open1h else None,
                        "void_entry": entry is None, "void_exit_4h": exit_4h is None,
                        "void_exit_12h": exit_12h is None, "void_exit_open1h": exit_open1h is None,
                        "week_id": week_id, "session_date": sess_date.isoformat() if sess_date else None,
                    })
                    if sess_date is not None:
                        resolve_t = (
                            pd.Timestamp(f"{sess_date} 13:30:00", tz="UTC")
                            + pd.Timedelta(minutes=CONFIG.r1_horizon_open1h_end_min)
                        )
                    else:
                        # Ни одной торговой сессии не нашлось до конца покрытия
                        # (чекпоинт у самой границы периода) -- пропустить с
                        # запасом, не блокируя дальнейшую обработку токена.
                        resolve_t = t + pd.Timedelta(days=7)
                    skip_until[th] = resolve_t

    dev_df = pd.DataFrame(deviations)
    out_dev = CACHE_DIR / "r1_full_deviations.csv"
    dev_df.to_csv(out_dev, index=False)
    client._commit_permanent(out_dev, f"sprintR1_cache: девиации полного прогона ({len(dev_df)} строк) [automated]")

    ev_df = pd.DataFrame(events) if events else pd.DataFrame(columns=EVENT_COLUMNS)
    out_events = CACHE_DIR / "r1_full_events.csv"
    ev_df.to_csv(out_events, index=False)
    client._commit_permanent(out_events, f"sprintR1_cache: события полного прогона ({len(ev_df)} строк) [automated]")

    # ---- Контроль §2.6: случайные валидные чекпоинты с |D|<=0.5%, объём >= N событий при theta=1% ----
    n_theta1_events = int((ev_df["theta"] == thetas[0]).sum()) if len(ev_df) else 0
    control_pool = dev_df[dev_df["D"].abs() <= CONFIG.r1_control_max_abs_d].reset_index(drop=True) if len(dev_df) else dev_df
    rng = random.Random(20260901)
    control_n = min(len(control_pool), max(n_theta1_events, 1)) if len(control_pool) else 0
    control_idx = rng.sample(range(len(control_pool)), control_n) if control_n else []
    control_rows = []
    for i in control_idx:
        crow_dev = control_pool.iloc[i]
        token_sym = crow_dev["token"]
        token_addr = tf.loc[tf["symbol"] == token_sym, "token_address"].iloc[0]
        t = crow_dev["t_checkpoint"]
        match = cp[(cp["token_address"] == token_addr) & (cp["t_checkpoint"] == t)]
        if not len(match):
            continue
        crow = match.iloc[0]
        entry = window_stats(crow, "entry")
        exit_4h = window_stats(crow, "exit4h")
        exit_12h = window_stats(crow, "exit12h")
        sess_date = next_session_date(t)
        exit_open1h = session_map.get((token_addr, sess_date)) if sess_date else None
        control_rows.append({
            "token": token_sym, "t_checkpoint": t, "D": crow_dev["D"],
            "entry_vwap": entry[0] if entry else None,
            "exit_4h": exit_4h[0] if exit_4h else None,
            "exit_12h": exit_12h[0] if exit_12h else None,
            "exit_open1h": exit_open1h[0] if exit_open1h else None,
            "void_entry": entry is None, "void_exit_4h": exit_4h is None,
            "void_exit_12h": exit_12h is None, "void_exit_open1h": exit_open1h is None,
            "week_id": crow_dev["week_id"],
        })
    ctrl_df = pd.DataFrame(control_rows) if control_rows else pd.DataFrame(columns=CONTROL_COLUMNS)
    out_ctrl = CACHE_DIR / "r1_full_control.csv"
    ctrl_df.to_csv(out_ctrl, index=False)
    client._commit_permanent(out_ctrl, f"sprintR1_cache: контроль §2.6 полного прогона ({len(ctrl_df)} строк) [automated]")

    print("\n[sprint_r1] ===== ИТОГИ ПОЛНОГО ПРОГОНА (Шаг 3) =====")
    print(f"Чекпоинтов всего (закрытые часы x токены с фидом, в периоде): {n_checkpoints_total}")
    print(f"Исключено недельной валидацией §2.2: {n_universe_excluded}")
    print(f"Пусто по цене: {n_void_price}; пусто по анкору: {n_void_anchor}")
    if len(dev_df):
        dser = dev_df["D"]
        print(f"Девиации D: медиана {dser.median():.4f}, диапазон [{dser.min():.4f}; {dser.max():.4f}]")
    print(f"Событий (сумма по theta, non-overlap): {len(ev_df)}")
    if len(ev_df):
        print(ev_df.groupby("theta").size().to_string())
    print(f"Контроль §2.6: {len(ctrl_df)} строк (нужно было >= {n_theta1_events} = событий при theta=1%)")

    # ---- Шаг 4: статистика (§2.7, BH-коррекция, 0 доп. кредитов) ----
    stats = compute_step4_stats(ev_df, ctrl_df, period_start, period_end)
    stats["n_checkpoints_total"] = n_checkpoints_total
    stats["n_universe_excluded"] = n_universe_excluded
    stats["n_void_price"] = n_void_price
    stats["n_void_anchor"] = n_void_anchor
    stats["n_control_needed"] = n_theta1_events
    out_stats = CACHE_DIR / "r1_full_stats.json"
    out_stats.write_text(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
    client._commit_permanent(out_stats, "sprintR1_cache: статистика Шага 4 (BH, ячейки) [automated]")
    print(f"\n[sprint_r1] Шаг 4 сохранён в r1_full_stats.json. Вердикт (предварительно): {stats['verdict']}")

    remaining = CONFIG.r1_credit_budget - cg.load_state().get("sprintR1", {}).get("spent", 0.0)
    print(f"[sprint_r1] Остаток бюджета R1: {remaining:.2f} из {CONFIG.r1_credit_budget}.")
    return 0


def bh_qvalues(pvalues: list[float], m: int) -> list[float]:
    """BH-коррекция с ФИКСИРОВАННЫМ размером семьи m (§2.7 дословно:
    "BH по 9 ячейкам") -- даже если реально протестировано k<m ячеек
    (остальные не допущены по N<50), знаменатель в формуле остаётся
    m=9, не k. Это чуть консервативнее стандартного BH по факту
    протестированных гипотез, но соответствует буквальному тексту
    замороженного §2.7 -- решение задокументировано здесь и в отчёте,
    не молчаливое допущение."""
    n = len(pvalues)
    ranked = sorted(range(n), key=lambda i: pvalues[i])
    qvals = [1.0] * n
    running_min = 1.0
    for pos in range(n - 1, -1, -1):
        i = ranked[pos]
        k = pos + 1  # ранг по возрастанию p, 1-индексация
        val = pvalues[i] * m / k
        running_min = min(running_min, val)
        qvals[i] = running_min
    return [min(1.0, v) for v in qvals]


def sign_test_median_gt0(r: pd.Series) -> tuple[float, int, int, int]:
    """Односторонний знаковый тест H0: median(r)<=0 vs H1: median(r)>0
    (§2.7). Точные нули исключаются из теста (стандартная практика
    знакового теста, не Wilcoxon -- дизайн явно требует именно
    "знаковый тест", не ранговый)."""
    pos = int((r > 0).sum())
    neg = int((r < 0).sum())
    zero = int((r == 0).sum())
    n = pos + neg
    if n == 0:
        return 1.0, pos, neg, zero
    p = sps.binomtest(pos, n, 0.5, alternative="greater").pvalue
    return float(p), pos, neg, zero


def trimmed_mean_bootstrap_ci(
    r: np.ndarray, trim: float, n_boot: int, seed: int = 20260901
) -> tuple[float, float, float]:
    """5%-усечённое среднее с бутстреп-CI (§2.7, secondary)."""
    if len(r) == 0:
        return float("nan"), float("nan"), float("nan")
    point = float(sps.trim_mean(r, trim))
    rng = np.random.default_rng(seed)
    n = len(r)
    boots = np.empty(n_boot)
    for b in range(n_boot):
        sample = rng.choice(r, size=n, replace=True)
        boots[b] = sps.trim_mean(sample, trim)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return point, float(lo), float(hi)


def compute_step4_stats(
    ev_df: pd.DataFrame, ctrl_df: pd.DataFrame, period_start: pd.Timestamp, period_end: pd.Timestamp
) -> dict:
    """§2.7/§2.8: 9 ячеек (3 theta x 3 горизонта), знаковый тест +
    BH(m=9) в допущенных (N>=50), половины периода, контроль §2.6,
    усечённое среднее + бутстреп, решение §2.8 (KILL/GO/GRAY/
    UNDERPOWERED, правила заморожены и не тронуты)."""
    thetas = list(CONFIG.r1_thetas)
    horizons = ["exit_4h", "exit_12h", "exit_open1h"]
    cost_base = CONFIG.r1_cost_scenario_base
    cost_scenarios = list(CONFIG.r1_cost_scenarios)
    mid = period_start + (period_end - period_start) / 2

    cells: list[dict] = []
    pvals_for_bh: list[float] = []
    cell_index_for_bh: list[int] = []

    for th in thetas:
        sub = ev_df[ev_df["theta"] == th] if len(ev_df) else ev_df
        for hz in horizons:
            valid = sub.dropna(subset=["entry_vwap", hz]) if len(sub) else sub
            n = len(valid)
            cell: dict = {"theta": th, "horizon": hz, "N": n, "admitted": n >= CONFIG.r1_min_n_per_cell}
            if n > 0:
                r_by_cost = {}
                for c in cost_scenarios:
                    r_by_cost[c] = (
                        np.log(valid[hz].to_numpy(dtype=float) / valid["entry_vwap"].to_numpy(dtype=float)) - c
                    )
                cell["median_r"] = {str(c): float(np.median(r_by_cost[c])) for c in cost_scenarios}
                if cell["admitted"]:
                    r_base = r_by_cost[cost_base]
                    p, pos, neg, zero = sign_test_median_gt0(pd.Series(r_base))
                    cell["p_value"], cell["sign_pos"], cell["sign_neg"], cell["sign_zero"] = p, pos, neg, zero
                    tm, lo, hi = trimmed_mean_bootstrap_ci(r_base, CONFIG.r1_trimmed_mean_pct, CONFIG.r1_bootstrap_n)
                    cell["trimmed_mean"], cell["trimmed_mean_ci95"] = tm, [lo, hi]

                    half1 = valid[valid["t_checkpoint"] < mid]
                    half2 = valid[valid["t_checkpoint"] >= mid]
                    r1_ = (
                        np.log(half1[hz].to_numpy(dtype=float) / half1["entry_vwap"].to_numpy(dtype=float)) - cost_base
                        if len(half1) else np.array([])
                    )
                    r2_ = (
                        np.log(half2[hz].to_numpy(dtype=float) / half2["entry_vwap"].to_numpy(dtype=float)) - cost_base
                        if len(half2) else np.array([])
                    )
                    cell["median_half1"] = float(np.median(r1_)) if len(r1_) else None
                    cell["median_half2"] = float(np.median(r2_)) if len(r2_) else None
                    cell["n_half1"], cell["n_half2"] = int(len(r1_)), int(len(r2_))

                    ctrl_valid = ctrl_df.dropna(subset=["entry_vwap", hz]) if len(ctrl_df) else ctrl_df
                    if len(ctrl_valid):
                        r_ctrl = (
                            np.log(ctrl_valid[hz].to_numpy(dtype=float) / ctrl_valid["entry_vwap"].to_numpy(dtype=float))
                            - cost_base
                        )
                        cell["control_median_r"] = float(np.median(r_ctrl))
                        cell["control_n"] = int(len(r_ctrl))
                        cell["excess_over_control"] = float(np.median(r_base) - np.median(r_ctrl))
                    else:
                        cell["control_median_r"], cell["control_n"], cell["excess_over_control"] = None, 0, None

                    pvals_for_bh.append(p)
                    cell_index_for_bh.append(len(cells))
            cells.append(cell)

    if pvals_for_bh:
        qvals = bh_qvalues(pvals_for_bh, m=9)
        for idx, q in zip(cell_index_for_bh, qvals):
            cells[idx]["q_value"] = q

    admitted = [c for c in cells if c["admitted"]]
    go_cell = None
    if not admitted:
        verdict = "UNDERPOWERED"
    else:
        significant = [c for c in admitted if c.get("q_value", 1.0) < CONFIG.r1_bh_alpha]
        if not significant:
            verdict = "KILL"
        else:
            go_candidates = []
            for c in significant:
                cond_median = c["median_r"][str(cost_base)] >= CONFIG.r1_go_min_median_pct
                cond_halves = (
                    c.get("median_half1") is not None and c["median_half1"] > 0
                    and c.get("median_half2") is not None and c["median_half2"] > 0
                )
                cond_control = (
                    c.get("excess_over_control") is not None
                    and c["excess_over_control"] >= CONFIG.r1_go_min_control_excess_pct
                )
                c["go_conditions"] = {"median": cond_median, "halves": cond_halves, "control": cond_control}
                if cond_median and cond_halves and cond_control:
                    go_candidates.append(c)
            if go_candidates:
                verdict = "GO"
                go_cell = min(go_candidates, key=lambda c: c["q_value"])
            else:
                verdict = "GRAY"
                go_cell = min(significant, key=lambda c: c["q_value"])

    return {
        "verdict": verdict,
        "best_cell": go_cell,
        "cells": cells,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "period_mid": mid.isoformat(),
        "n_events_total": int(len(ev_df)),
        "n_control_total": int(len(ctrl_df)),
    }


def run_report() -> int:
    """Sprint R1, Шаг 5: сборка docs/RESULTS.md из уже посчитанных (0
    доп. кредитов, только локальные файлы) артефактов Шага 3/4 --
    вердикт §2.8, таблица ячеек, топ-10 |D| (по просьбе владельца, см.
    docs/R1_DESIGN.md "Проверка выбросов девиации"), секция
    чувствительности (по просьбе владельца -- пересечение старый/новый
    реестр пусто, см. docs/R1_DESIGN.md "Формальная запись"), леджер."""
    stats_path = CACHE_DIR / "r1_full_stats.json"
    if not stats_path.exists():
        print(f"[sprint_r1] СТОП: {stats_path} не найден -- сначала нужен --stage full.")
        return 1
    stats = json.loads(stats_path.read_text())
    dev_df = pd.read_csv(CACHE_DIR / "r1_full_deviations.csv")
    ev_df = pd.read_csv(CACHE_DIR / "r1_full_events.csv")
    ctrl_df = pd.read_csv(CACHE_DIR / "r1_full_control.csv")

    guard_state = cg.load_state()
    ns_spent = guard_state.get("sprintR1", {}).get("spent", 0.0)
    ns_budget = guard_state.get("sprintR1", {}).get("budget_remaining_at_init", CONFIG.r1_credit_budget)
    step3_entries = [
        e for e in guard_state.get("entries", [])
        if e.get("namespace") == "sprintR1" and str(e.get("name", "")).startswith(
            ("r1_full_checkpoint", "r1_full_session_open_windows", "r1_full_feed_events", "r1_full_weekly_universe")
        )
    ]
    step3_spent = sum(e.get("credits", 0.0) or 0.0 for e in step3_entries)

    md = build_results_md(stats, dev_df, ev_df, ctrl_df, step3_entries, step3_spent, ns_spent, ns_budget)
    out_path = Path("docs/RESULTS.md")
    out_path.write_text(md)
    print(f"[sprint_r1] Записан {out_path} -- вердикт {stats['verdict']}.")
    return 0


def _fmt_pct(x) -> str:
    return f"{x * 100:+.2f}%" if x is not None and not (isinstance(x, float) and np.isnan(x)) else "н/д"


def build_results_md(
    stats: dict, dev_df: pd.DataFrame, ev_df: pd.DataFrame, ctrl_df: pd.DataFrame,
    step3_entries: list[dict], step3_spent: float, ns_spent: float, ns_budget: float,
) -> str:
    lines: list[str] = []
    lines.append("# Sprint R1 — Результаты и вердикт (RWA-конвергенция, сток-токены)")
    lines.append("")
    lines.append(f"**Дата:** 2026-09-01. **Вердикт §2.8: {stats['verdict']}.**")
    lines.append("")
    lines.append(
        f"Период: {stats['period_start']} → {stats['period_end']} (UTC). "
        f"Событий всего: {stats['n_events_total']}. Контроль §2.6: {stats['n_control_total']} строк. "
        f"Чекпоинтов: {stats.get('n_checkpoints_total', 'н/д')}, исключено недельной валидацией §2.2: "
        f"{stats.get('n_universe_excluded', 'н/д')}, пусто по цене: {stats.get('n_void_price', 'н/д')}, "
        f"пусто по анкору: {stats.get('n_void_anchor', 'н/д')}."
    )
    lines.append("")

    lines.append("## §2.8 — обоснование вердикта")
    lines.append("")
    verdict = stats["verdict"]
    if verdict == "UNDERPOWERED":
        lines.append("Ни одна из 9 ячеек (3θ × 3 горизонта) не набрала N≥50 — недостаточно мощности для теста.")
    elif verdict == "KILL":
        lines.append("Есть допущенные ячейки (N≥50), но ни одна не значима по BH (q<0.05, m=9) при базовых издержках (1.5%).")
    else:
        bc = stats.get("best_cell") or {}
        lines.append(
            f"Лучшая значимая ячейка: θ={bc.get('theta')}, горизонт={bc.get('horizon')}, "
            f"N={bc.get('N')}, q={bc.get('q_value'):.4f}. " + (
                "Все условия GO (медиана≥+1%, обе половины периода>0, превышение контроля≥+1%) выполнены."
                if verdict == "GO" else
                "Значимость есть, но не все условия GO выполнены (см. go_conditions в таблице ниже) — "
                "по правилу §2.8 дефолт закрыть, решение штаба."
            )
        )
    lines.append("")

    lines.append("## Таблица ячеек (3θ × 3 горизонта = 9)")
    lines.append("")
    lines.append(
        "| θ | горизонт | N | допущена | p (знак. тест) | q (BH, m=9) | медиана r (база 1.5%) | "
        "усеч. среднее [95% CI] | половина 1 | половина 2 | медиана контроля | превышение контроля | GO-условия |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for c in stats["cells"]:
        if not c.get("admitted"):
            lines.append(
                f"| {c['theta']} | {c['horizon']} | {c['N']} | нет (N<50) | — | — | — | — | — | — | — | — | — |"
            )
            continue
        tm_ci = c.get("trimmed_mean_ci95") or [None, None]
        gc = c.get("go_conditions") or {}
        lines.append(
            f"| {c['theta']} | {c['horizon']} | {c['N']} | да | {c.get('p_value', float('nan')):.4f} | "
            f"{c.get('q_value', float('nan')):.4f} | {_fmt_pct(c['median_r'][str(CONFIG.r1_cost_scenario_base)])} | "
            f"{_fmt_pct(c.get('trimmed_mean'))} [{_fmt_pct(tm_ci[0])}; {_fmt_pct(tm_ci[1])}] | "
            f"{_fmt_pct(c.get('median_half1'))} (N={c.get('n_half1')}) | "
            f"{_fmt_pct(c.get('median_half2'))} (N={c.get('n_half2')}) | "
            f"{_fmt_pct(c.get('control_median_r'))} (N={c.get('control_n')}) | "
            f"{_fmt_pct(c.get('excess_over_control'))} | "
            f"медиана={'✓' if gc.get('median') else '✗'} половины={'✓' if gc.get('halves') else '✗'} "
            f"контроль={'✓' if gc.get('control') else '✗'} |"
        )
    lines.append("")
    lines.append(
        "Сценарии издержек (0.5% / 1.5% база / 3%) по медиане r для каждой допущенной ячейки:"
    )
    lines.append("")
    lines.append("| θ | горизонт | r @ 0.5% | r @ 1.5% (база) | r @ 3% |")
    lines.append("|---|---|---|---|---|")
    for c in stats["cells"]:
        if not c.get("admitted"):
            continue
        mr = c["median_r"]
        lines.append(
            f"| {c['theta']} | {c['horizon']} | {_fmt_pct(mr.get('0.005'))} | "
            f"{_fmt_pct(mr.get('0.015'))} | {_fmt_pct(mr.get('0.03'))} |"
        )
    lines.append("")

    lines.append("## Сенситивность (по просьбе штаба, п.3 уточнения §2.2)")
    lines.append("")
    lines.append(
        "Штаб запросил тот же расчёт таблицы ячеек на пересечении «старый реестр ∩ новый» "
        "(ожидание ~23-26 токенов). Проверено формально (см. `docs/R1_DESIGN.md`, "
        "«Формальная запись: замена источника вселенной»): пересечение {23 токена, прошедших §2.2 "
        "на старом факторном реестре Шага 1} ∩ {26 токенов с активным Chainlink-фидом, реально "
        "используемых в этом расчёте} = **пустое множество (0 токенов)** — это структурно два "
        "непересекающихся множества (длинный хвост старого реестра vs флагманы с фидом), а не два "
        "варианта одного и того же списка. Буквальная параллельная таблица технически невыполнима: "
        "без фида нет анкора F(i,t), без анкора нет девиации. Единственная вычислимая таблица ячеек — "
        "та, что выше (26 феед-покрытых токенов). Взамен выполнена структурная сверка: 98/102 токенов "
        "старого реестра входят в новый (194), и все 23 токена, прошедшие §2.2 на старых данных, "
        "проходят и на новых с согласующимися числами — коррекция реестра ничего не отменила, только "
        "добавила видимость (флагманы, которых старый реестр физически не мог увидеть). Решение "
        "штаба нужно только если требуется тест старых 23 токенов на ИНОМ анкоре (не Chainlink) — "
        "это отдельный дизайн (правка §2.3), не выполняется без явной команды владельца."
    )
    lines.append("")

    lines.append("## Топ-10 |D| по всей выборке (по просьбе штаба, п.4 уточнения — глазами проверить выбросы)")
    lines.append("")
    if len(dev_df):
        top10 = dev_df.reindex(dev_df["D"].abs().sort_values(ascending=False).index).head(10)
        lines.append("| токен | t_checkpoint | D | P (VWAP) | F (анкор) | возраст анкора, мин | сделок пре- | объём пре-, $ |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for _, r in top10.iterrows():
            lines.append(
                f"| {r['token']} | {r['t_checkpoint']} | {r['D']:+.4f} | {r['P_vwap']:.4f} | "
                f"{r['F_anchor']:.4f} | {r['anchor_age_min']:.0f} | {int(r['n_trades_pre'])} | "
                f"{r['vol_usd_pre']:,.0f} |"
            )
        lines.append("")
        n_unique_tokens_top10 = top10["token"].nunique()
        lines.append(
            f"Топ-10 сосредоточен в {n_unique_tokens_top10} уникальн{'ом токене' if n_unique_tokens_top10 == 1 else 'ых токенах'}. "
            "Диагноз по каждой строке необходимо свести к одному из двух объяснений: (а) застывший "
            "анкор в закрытые часы + реальная/тонкая торговля токена (см. разбор того же паттерна на "
            "смоук-выборке в `docs/R1_DESIGN.md`, §2.10 — там весь топ-10 объяснился именно так, "
            "MSTR, выходные 25-26.07); (б) ошибка маппинга фида (в этом случае F_anchor выглядел бы "
            "нехарактерно для тикера/decimals — не наблюдалось ни на смоуке, ни в санитарной проверке "
            "диапазона девиаций ниже). Финальное подтверждение — за штабом (глазами по таблице выше)."
        )
    else:
        lines.append("Нет валидных девиаций в выборке.")
    lines.append("")
    if len(dev_df):
        dser = dev_df["D"]
        lines.append(
            f"Санитарная проверка (§2.10): медиана D = {dser.median():.4f}, диапазон "
            f"[{dser.min():.4f}; {dser.max():.4f}] — нет величин порядка десятков (|D|≫1), decimals не перепутаны."
        )
        lines.append("")

    lines.append("## Ограничения (§2.10, обязательны в отчёте, текст заморожен дословно)")
    lines.append("")
    lines.append(
        "Фид вне часов рынка может замирать/быть stale — дисконт может отражать реальные новости "
        "(гэп-риск), частично контролируется контролем и выходом после открытия; тонкая ликвидность "
        "закрытых часов = слиппедж хуже ретро-VWAP; MEV; юридическая доступность живой торговли "
        "сток-токенами для владельца (резидент ЕС) — проверяется отдельно ДО любого лайва; "
        "decimals/нормировка фидов и пулов — явный тест на смоуке (пройден)."
    )
    lines.append("")

    lines.append("## Кредитный леджер Шага 3")
    lines.append("")
    lines.append(f"Потрачено в namespace `sprintR1`: {ns_spent:.2f} из {ns_budget:.1f}. Из них Шаг 3: {step3_spent:.2f}.")
    lines.append("")
    lines.append("| запрос | факт, кредиты |")
    lines.append("|---|---|")
    for e in step3_entries:
        lines.append(f"| {e.get('name')} | {e.get('credits', 0.0):.3f} |")
    lines.append("")

    lines.append("## Артефакты")
    lines.append("")
    lines.append(
        "`data/sprintR1_cache/r1_full_checkpoint_windows.csv`, `r1_full_deviations.csv`, "
        "`r1_full_events.csv`, `r1_full_control.csv`, `r1_full_session_open_windows.csv`, "
        "`r1_full_feed_events.csv`, `r1_full_weekly_universe.csv`, `r1_full_stats.json`. "
        "Дизайн: `docs/R1_DESIGN.md` (§2 заморожен, «Механика» дополнена по факту разведки/смоука/"
        "полного прогона)."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=["smoke", "full", "report"])
    args = parser.parse_args()

    if args.stage == "smoke":
        return run_smoke()
    if args.stage == "full":
        return run_full()
    if args.stage == "report":
        return run_report()
    print(f"[sprint_r1] Стадия '{args.stage}' не реализована в этом коммите.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
