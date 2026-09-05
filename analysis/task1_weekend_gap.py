#!/usr/bin/env python3
"""Задача 1 (владелец, 2026-09-05, замена закрытому Query 3 -- 0
заявок 8-K в тёмном окне, docs/PROJECT_STATE.md): для каждых выходных с
2026-07-01 и каждого сток-токена с ликвидным пулом (реестр -- реальный,
195 токенов, `data/rwa_stock_token_registry.json`, из курируемой Dune-
таблицы `rwa_robinhood.balances`, тот же источник, что уже проверен в
Sprint R1):

  X -- движение токена в цепи пт 20:00 -> вс 19:55 ET (тёмное окно,
       та же дефиниция, что docs/R1_DESIGN.md/edgar_8k_fetch.py::
       is_dark_window, но здесь НЕ привязано к 8-K -- просто КАЖДЫЕ
       выходные).
  Y -- гэп акции пт закрытие (16:00 ET) -> пн открытие (9:30 ET),
       бесплатный дневной источник -- Stooq (`stooq.com/q/d/l/`, без
       ключа, дневные OHLC).
  Z -- движение токена вс 20:00 -> пн 9:30 ET (примыкающее к открытию
       окно, для проверки, продолжает ли позднее движение направление X
       или разворачивает).

Метод X/Z -- VWAP-брекеты 2ч у каждой границы окна (amount_usd/
token_qty из dex.trades, blockchain='robinhood') -- ТОТ ЖЕ стиль
чекпоинтов, что уже реально оплачен и проверен в Sprint R1
(`sql/r1/r1_full_session_open_windows.sql`,
`sql/r1/r1_full_checkpoint_windows.sql`), не единичная сделка --
устойчивее к тонкому объёму на границе окна.

Предрегистрация (владелец, ДО прогона): линия жива при |corr(X,Y)|<0.3,
N>=100 и Z систематически противоположен X; corr>0.7 -- закрываем;
промежуточное -- доложить, не решать. Фильтр тонких пулов: минимум 3
сделки В КАЖДОМ из 4 брекетов (x_start/x_end/z_start/z_end) -- выбран
ДО просмотра результатов, не подогнан под целевой N; число исключённых
по этому фильтру -- явно в отчёте.

Бюджет Dune -- новое пространство `task1_weekend_gap`, 250 кредитов
(владелец, 2026-09-05: "бюджет Dune -- из тех же 250"), внутри общего
биллинг-цикла (data/credits_spent.json billing_cycle, реальный остаток
цикла на 2026-09-01 -- 2500-2138.84-20=341.16, 250 умещается)."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task1_weekend_gap")
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402
from scipy import stats as sstats  # noqa: E402

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402
from run_pipeline import read_sql  # noqa: E402

BUDGET = 250.0
REGISTRY_PATH = Path("data/rwa_stock_token_registry.json")
CACHE_DIR = Path("data/task1_weekend_gap_cache")
OUT_PATH = Path("data/p3_guard_cache/task1_weekend_gap_result.json")
MIN_BRACKET_TRADES = 3  # предрегистрировано -- см. docstring
SMOKE_ONLY = os.environ.get("TASK1_SMOKE_ONLY", "1") == "1"  # по умолчанию смоук (1 выходные), полный прогон -- явный env


def real_fridays_since(start_date: str, now_utc: datetime) -> list[str]:
    """Реальные пятницы (00:00 UTC) от start_date до ПОСЛЕДНИХ ПОЛНОСТЬЮ
    завершённых выходных (нужен реальный понедельник 9:30 ET уже в
    прошлом) -- Python datetime, не по памяти."""
    d = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    # ближайшая пятница >= start_date
    while d.weekday() != 4:  # 4 = пятница
        d += timedelta(days=1)
    fridays = []
    while True:
        monday_930_et_utc = d + timedelta(days=3, hours=13, minutes=30)  # пн 9:30 ET = пн 13:30 UTC (EDT)
        if monday_930_et_utc >= now_utc:
            break
        fridays.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=7)
    return fridays


_STOOQ_DIAG_PRINTED = 0


def stooq_daily(symbol: str) -> pd.DataFrame | None:
    """Реальные дневные OHLC с Stooq (бесплатно, без ключа) --
    `stooq.com/q/d/l/?s=<symbol>.us&i=d`. Возвращает None, если тикер не
    найден (Stooq отвечает пустым/HTML телом вместо CSV на несуществующий
    символ -- НЕ выдумываем данные, честно пропускаем)."""
    try:
        r = requests.get("https://stooq.com/q/d/l/", params={"s": f"{symbol.lower()}.us", "i": "d"},
                          headers={"User-Agent": "Mozilla/5.0 (robinhood-chain-alpha-task1/1.0)"}, timeout=20)
    except Exception as e:  # noqa: BLE001
        print(f"    Stooq {symbol}: сетевая ошибка {e}")
        return None
    if r.status_code != 200 or "Date,Open" not in r.text[:200]:
        # Диагностика (владелец: не гадать) -- реальный статус и сырое
        # начало тела ответа для 2-3 первых неудач, чтобы увидеть точную
        # причину (BOM/HTML-заглушка/иной заголовок CSV/блок по UA) --
        # не на каждый тикер, иначе лог раздувается на все 38.
        global _STOOQ_DIAG_PRINTED
        if _STOOQ_DIAG_PRINTED < 3:
            print(f"    [STOOQ DIAG] {symbol}: status={r.status_code} content-type={r.headers.get('content-type')} "
                  f"body_repr={r.text[:200]!r}")
            _STOOQ_DIAG_PRINTED += 1
        return None
    from io import StringIO
    try:
        df = pd.read_csv(StringIO(r.text))
    except Exception:  # noqa: BLE001
        return None
    if "Date" not in df.columns or not len(df):
        return None
    df["Date"] = pd.to_datetime(df["Date"])
    return df


def friday_monday_gap(daily: pd.DataFrame, friday_date: str) -> dict:
    """Y = (Monday_Open / Friday_Close - 1) для КОНКРЕТНЫХ выходных --
    реальные даты по календарю (пятница = friday_date, понедельник =
    friday_date+3, с учётом реальных праздников NYSE -- если понедельник
    отсутствует в данных Stooq, значит был выходной/праздник, честно
    пропускаем эту пару выходных для этого тикера, не сдвигаем дату
    вручную/по памяти)."""
    fri = pd.Timestamp(friday_date)
    mon = fri + pd.Timedelta(days=3)
    row_fri = daily[daily["Date"] == fri]
    row_mon = daily[daily["Date"] == mon]
    if not len(row_fri) or not len(row_mon):
        return {"gap": None, "reason": "нет реальных дневных данных на пятницу или понедельник (праздник/выходной/тикер молодой)"}
    close_fri = float(row_fri.iloc[0]["Close"])
    open_mon = float(row_mon.iloc[0]["Open"])
    if close_fri <= 0:
        return {"gap": None, "reason": "close_fri<=0"}
    return {"gap": open_mon / close_fri - 1, "close_fri": close_fri, "open_mon": open_mon}


def run() -> int:
    credit_guard.ensure_namespace("task1_weekend_gap", BUDGET)

    registry = json.loads(REGISTRY_PATH.read_text())
    tokens = registry["tokens"]  # symbol -> {name, stock_token_address, evt_block_time}
    print(f"[task1] реестр: {len(tokens)} сток-токенов, источник: {registry['source']}")

    now_utc = datetime.now(timezone.utc)
    all_fridays = real_fridays_since("2026-07-01", now_utc)
    fridays = all_fridays[-1:] if SMOKE_ONLY else all_fridays
    print(f"[task1] реальных завершённых выходных с 2026-07-01: {len(all_fridays)} -- "
          f"{'СМОУК (последние 1)' if SMOKE_ONLY else 'ПОЛНЫЙ прогон'}: {fridays}")

    token_addrs = [t["stock_token_address"] for t in tokens.values()]
    addr_to_symbol = {t["stock_token_address"].lower(): sym for sym, t in tokens.items()}

    # Реальная проверка (2026-09-05, task1_dex_trades_columns_probe_result.json,
    # information_schema.columns): token_bought_address/token_sold_address в
    # dex.trades -- VARBINARY, не VARCHAR (первый прогон run 33964786227 упал
    # именно на этом: "Cannot find common type between varbinary and
    # varchar(42)" -- голые q_list()-литералы, как в sql/r1/*.sql, здесь не
    # годятся). from_hex() строит varbinary-литерал из hex-строки БЕЗ '0x'
    # префикса. НЕ трогаем sql/r1/*.sql задним числом -- их результат уже
    # реально получен и закоммичен ДО этой проверки; возможно, схема dex.trades
    # реально изменилась между прогоном R1 (01-04.09) и сейчас (05.09) -- это
    # находка о дрейфе схемы Dune, не повод переисполнять закрытый спринт.
    token_addrs_hex_list = ",".join(f"from_hex('{a[2:].lower()}')" for a in token_addrs)

    sql_template = read_sql("task1/task1_weekend_windows")
    friday_list_sql = ",".join(f"timestamp '{f} 00:00:00'" for f in fridays)
    trades_start = fridays[0] + " 00:00:00"
    trades_end_dt = datetime.strptime(fridays[-1], "%Y-%m-%d") + timedelta(days=3, hours=14)
    trades_end = trades_end_dt.strftime("%Y-%m-%d %H:%M:%S")
    sql = (sql_template
           .replace("{{weekend_friday_list}}", friday_list_sql)
           .replace("{{token_address_list}}", token_addrs_hex_list)
           .replace("{{trades_start}}", trades_start)
           .replace("{{trades_end}}", trades_end))

    print(f"[task1] окно сделок для Dune-запроса: {trades_start} .. {trades_end}, "
          f"{len(fridays)} выходных x {len(token_addrs)} токенов = {len(fridays)*len(token_addrs)} строк ожидается")

    client = DuneClient()
    qid = client.create_query("task1_weekend_windows", sql)
    # Оценка: тот же паттерн, что r1_full_session_open_windows (194
    # токена x ~9 сессий дал единицы-десятки кредитов на партицию,
    # здесь диапазон дат УЖЕ и брекетов вдвое больше на строку, но
    # смоук -- всего 1 выходные) -- консервативная оценка 8.0 для
    # смоука, санитарный порог >40 сработает сам, если это неверно.
    df = client.run_sql_cached(
        "task1_weekend_windows", sql, query_id=qid,
        estimated_credits=8.0 if SMOKE_ONLY else 60.0,
        expected_max_rows=len(fridays) * len(token_addrs) + 100, expected_columns=17,
    )
    if df is None or not len(df):
        print("[task1] Dune вернул пусто -- проверить SQL/токены/даты")
        return 1
    print(f"[task1] Dune: {len(df)} строк (токен x выходные)")

    df["symbol"] = df["token_address"].str.lower().map(addr_to_symbol)
    df["x_start_vwap"] = df["x_start_vol"] / df["x_start_qty"]
    df["x_end_vwap"] = df["x_end_vol"] / df["x_end_qty"]
    df["z_start_vwap"] = df["z_start_vol"] / df["z_start_qty"]
    df["z_end_vwap"] = df["z_end_vol"] / df["z_end_qty"]
    df["X"] = df["x_end_vwap"] / df["x_start_vwap"] - 1
    df["Z"] = df["z_end_vwap"] / df["z_start_vwap"] - 1

    n_before_thin_filter = len(df)
    thin_mask = (
        (df["x_start_n"].fillna(0) >= MIN_BRACKET_TRADES) & (df["x_end_n"].fillna(0) >= MIN_BRACKET_TRADES)
        & (df["z_start_n"].fillna(0) >= MIN_BRACKET_TRADES) & (df["z_end_n"].fillna(0) >= MIN_BRACKET_TRADES)
    )
    n_excluded_thin = int((~thin_mask).sum())
    df = df[thin_mask].copy()
    print(f"[task1] фильтр тонких пулов (мин {MIN_BRACKET_TRADES} сделок в каждом из 4 брекетов): "
          f"исключено {n_excluded_thin} из {n_before_thin_filter}, осталось {len(df)}")

    print(f"[task1] реальные дневные цены со Stooq для {df['symbol'].nunique()} тикеров...")
    daily_cache: dict[str, pd.DataFrame | None] = {}
    gaps = []
    for row in df.itertuples():
        sym = row.symbol
        if sym is None:
            gaps.append({"gap": None, "reason": "символ не сопоставлен из реестра"})
            continue
        if sym not in daily_cache:
            daily_cache[sym] = stooq_daily(sym)
            time.sleep(0.3)  # вежливый троттлинг, Stooq без документированного лимита, но публичный сервис
        daily = daily_cache[sym]
        if daily is None:
            gaps.append({"gap": None, "reason": "Stooq не вернул реальные дневные данные для этого тикера"})
            continue
        gaps.append(friday_monday_gap(daily, row.friday_utc[:10] if isinstance(row.friday_utc, str) else str(row.friday_utc)[:10]))

    df["Y"] = [g["gap"] for g in gaps]
    df["Y_reason_missing"] = [g.get("reason") for g in gaps]
    n_no_stooq = df["Y"].isna().sum()
    print(f"[task1] реальный Y (гэп Stooq) получен для {len(df)-n_no_stooq}/{len(df)}, "
          f"не найден для {n_no_stooq} (тикер молодой/не на Stooq/праздник)")
    if n_no_stooq:
        for sym, reason, cnt in df[df["Y"].isna()].groupby(["symbol", "Y_reason_missing"], dropna=False).size().reset_index(name="n").itertuples(index=False):
            print(f"    Y отсутствует: {sym} -- {reason} (n={cnt})")

    final = df.dropna(subset=["X", "Y", "Z"]).copy()
    n = len(final)
    print(f"[task1] финальная выборка (X, Y, Z все реальны): N={n}")

    # Диагностика (владелец: не гадать, честно доложить причину) --
    # какие символы прошли фильтр тонких пулов, но не получили Y, и
    # почему конкретно (символ не сопоставлен / Stooq не ответил /
    # реальных дневных данных на пятницу-понедельник не нашлось).
    missing_y_diag = (
        df[df["Y"].isna()][["symbol", "Y_reason_missing"]].drop_duplicates().to_dict("records")
        if "Y_reason_missing" in df.columns else []
    )

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "smoke_only": SMOKE_ONLY, "n_weekends_used": len(fridays), "n_weekends_total_available": len(all_fridays),
        "n_before_thin_filter": n_before_thin_filter, "n_excluded_thin_pools": n_excluded_thin,
        "min_bracket_trades_threshold": MIN_BRACKET_TRADES,
        "n_no_stooq_data": int(n_no_stooq), "n_final": n,
        "missing_y_diagnostic": missing_y_diag,
    }

    if n >= 3:
        slope, intercept, r, p, se = sstats.linregress(final["X"], final["Y"])
        corr_xy = final["X"].corr(final["Y"])
        corr_xz = final["X"].corr(final["Z"])
        sign_opposite_frac = float((np.sign(final["X"]) != np.sign(final["Z"])).mean())
        result.update({
            "regression_Y_on_X": {"slope": slope, "intercept": intercept, "r": r, "r_squared": r ** 2, "p_value": p, "stderr": se},
            "corr_X_Y": corr_xy, "corr_X_Z": corr_xz, "sign_opposite_fraction_X_Z": sign_opposite_frac,
        })
        print(f"[task1] РЕГРЕССИЯ Y~X: slope={slope:.4f} r={r:.4f} r²={r**2:.4f} p={p:.4f}")
        print(f"[task1] corr(X,Y)={corr_xy:.4f}  corr(X,Z)={corr_xz:.4f}  доля sign(X)!=sign(Z)={sign_opposite_frac:.2%}")

        if n < 100:
            verdict = f"НЕДОСТАТОЧНО ДАННЫХ (N={n}<100) -- доложить честно, не решать (по правилу владельца: N<требуемого -- без выводов)"
        elif abs(corr_xy) > 0.7:
            verdict = f"ЗАКРЫТЬ -- |corr(X,Y)|={abs(corr_xy):.3f} > 0.7"
        elif abs(corr_xy) < 0.3 and sign_opposite_frac > 0.5:
            verdict = (f"ЖИВА -- |corr(X,Y)|={abs(corr_xy):.3f} < 0.3, N={n}>=100, "
                       f"Z систематически противоположен X ({sign_opposite_frac:.1%} случаев)")
        else:
            verdict = f"ПРОМЕЖУТОЧНОЕ -- corr(X,Y)={corr_xy:.3f}, sign_opposite(X,Z)={sign_opposite_frac:.1%} -- доложить, не решать"
        result["verdict"] = verdict
        print(f"[task1] ВЕРДИКТ (по предрегистрации): {verdict}")
    else:
        result["verdict"] = f"N={n} слишком мало для регрессии/корреляции -- честно доложить"
        print(f"[task1] {result['verdict']}")

    final_out = final[["symbol", "friday_utc", "X", "Y", "Z", "x_start_n", "x_end_n", "z_start_n", "z_end_n"]].to_dict("records")
    result["rows"] = final_out

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[task1] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
