#!/usr/bin/env python3
"""Задача 1, проверка 4 (владелец, 2026-09-05, дословно): "Out-of-sample
на эти выходные. В воскресенье в 19:55 ET -- прогноз знака Z по всем
тикерам с X, записать в файл с таймстемпом до 20:00. В понедельник
после 9:30 -- проверка. Предрегистрация: >=60% попаданий."

Выходные: пятница 2026-09-04 (ближайшие ПОЛНОСТЬЮ ещё не наступившие
на момент постановки задачи, 2026-09-05).

Правило прогноза (механическое, из уже найденного на исторических 9
выходных факта -- corr(X,Z)=-0.356, доля противоположных знаков 62.4%,
`docs/PROJECT_STATE.md`, полный прогон): **predicted_sign(Z) = -sign(X)**
-- ставка на разворот, тот же и единственный сигнал, который дала
Задача 1, никакой новой гипотезы не вводится.

Дополнение владельца, 2026-09-05 (к тому же триггеру 23:55 UTC):
- Фильтр |X| > 0.5 -- ИСКЛЮЧАТЬ, предрегистрировано (тот же порог, что
  поймал артефакт тонкой ликвидности AMC в проверке 2 -- исключается
  ДО прогноза, не постфактум).
- По каждому тикеру -- реальный адрес v3-пула, TVL (GT, текущий снимок,
  явно помечено), round-trip издержка на $500 (`task1_pool_liquidity.py`,
  тот же метод, что финал проверки 1), флаг "торгуемо" = |X| >
  round-trip (v4/не оценено -- флаг False, не гадать).
- В `verify()` -- hit_rate по ВСЕМ и ОТДЕЛЬНО по торгуемым, порог 60%
  проверяется именно на торгуемых.

Два режима, вызываются в РАЗНОЕ реальное время (см. запланированные
триггеры): `predict` -- воскресенье 19:55 ET (сразу после закрытия
окна X, до открытия окна Z); `verify` -- понедельник после 9:30 ET
(после закрытия окна Z). Оба используют ТОТ ЖЕ SQL-шаблон и тот же
универсум токенов/фильтр тонких пулов, что полный прогон
`task1_weekend_gap.py` -- НЕ новый метод, применение уже
откалиброванного пайплайна к ещё не наступившим данным."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task1_weekend_gap")
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402
from run_pipeline import read_sql  # noqa: E402
from task1_pool_liquidity import evaluate_symbol_liquidity  # noqa: E402
from task1_liquidity_v3_roundtrip import find_pool_addr_cache  # noqa: E402

BUDGET = 250.0
REGISTRY_PATH = Path("data/rwa_stock_token_registry.json")
FRIDAY = "2026-09-04"  # выходные, на которые ставится предрегистрированный прогноз
MIN_BRACKET_TRADES = 3  # тот же порог, что в task1_weekend_gap.py
ABS_X_OUTLIER_THRESHOLD = 0.5  # предрегистрировано владельцем 2026-09-05, тот же порог, что в проверке 2/3
PREDICT_PATH = Path("data/p3_guard_cache/task1_oos_check4_predict_result.json")
VERIFY_PATH = Path("data/p3_guard_cache/task1_oos_check4_verify_result.json")
HIT_RATE_THRESHOLD = 0.60  # предрегистрировано владельцем ДО прогноза -- проверяется на ТОРГУЕМЫХ (2026-09-05)


def fetch_weekend_df(trades_end_hours_after_friday: int) -> pd.DataFrame:
    """Тот же SQL-шаблон, что task1_weekend_gap.py, на ОДНИ выходные
    (FRIDAY). trades_end_hours_after_friday расширяет окно сканирования
    dex.trades -- на этапе predict Monday ещё не наступил, окно
    сканирования всё равно можно указать как для полного прогона (сам
    Dune просто не найдёт будущих сделок, вреда нет)."""
    registry = json.loads(REGISTRY_PATH.read_text())["tokens"]
    token_addrs = [t["stock_token_address"] for t in registry.values()]
    addr_to_symbol = {t["stock_token_address"].lower(): sym for sym, t in registry.items()}
    token_addrs_hex_list = ",".join(f"from_hex('{a[2:].lower()}')" for a in token_addrs)

    sql_template = read_sql("task1/task1_weekend_windows")
    friday_list_sql = f"timestamp '{FRIDAY} 00:00:00'"
    trades_start = f"{FRIDAY} 00:00:00"
    from datetime import datetime, timedelta
    trades_end_dt = datetime.strptime(FRIDAY, "%Y-%m-%d") + timedelta(hours=trades_end_hours_after_friday)
    trades_end = trades_end_dt.strftime("%Y-%m-%d %H:%M:%S")
    sql = (sql_template
           .replace("{{weekend_friday_list}}", friday_list_sql)
           .replace("{{token_address_list}}", token_addrs_hex_list)
           .replace("{{trades_start}}", trades_start)
           .replace("{{trades_end}}", trades_end))

    client = DuneClient()
    qid = client.create_query("task1_weekend_windows", sql)
    df = client.run_sql_cached(
        "task1_weekend_windows", sql, query_id=qid,
        estimated_credits=2.0,  # 1 выходные, тот же порядок, что смоук (0.62 факт) -- с запасом
        expected_max_rows=len(token_addrs) + 50, expected_columns=17,
    )
    if df is None or not len(df):
        raise SystemExit("[oos_check4] Dune вернул пусто")
    df["symbol"] = df["token_address"].str.lower().map(addr_to_symbol)
    df["x_start_vwap"] = df["x_start_vol"] / df["x_start_qty"]
    df["x_end_vwap"] = df["x_end_vol"] / df["x_end_qty"]
    df["z_start_vwap"] = df["z_start_vol"] / df["z_start_qty"]
    df["z_end_vwap"] = df["z_end_vol"] / df["z_end_qty"]
    df["X"] = df["x_end_vwap"] / df["x_start_vwap"] - 1
    df["Z"] = df["z_end_vwap"] / df["z_start_vwap"] - 1
    return df


def predict() -> int:
    credit_guard.ensure_namespace("task1_weekend_gap", BUDGET)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[oos_check4:predict] реальное время генерации (UTC): {now}")
    # Sunday 19:55 ET = friday+3d 00:00 UTC - 5min ровно, т.е. окно X
    # уже полностью реализовалось; окно сканирования расширяем до
    # понедельника с запасом -- будущие сделки просто ещё не существуют.
    df = fetch_weekend_df(trades_end_hours_after_friday=3 * 24 + 14)

    x_ok = df[(df["x_start_n"].fillna(0) >= MIN_BRACKET_TRADES) & (df["x_end_n"].fillna(0) >= MIN_BRACKET_TRADES)].copy()
    x_ok = x_ok.dropna(subset=["X", "symbol"])
    n_before_outlier_filter = len(x_ok)
    x_ok = x_ok[x_ok["X"].abs() <= ABS_X_OUTLIER_THRESHOLD].copy()
    n_excluded_outliers = n_before_outlier_filter - len(x_ok)
    print(f"[oos_check4:predict] фильтр |X|>{ABS_X_OUTLIER_THRESHOLD} (предрегистрировано): "
          f"исключено {n_excluded_outliers} из {n_before_outlier_filter}")
    x_ok["predicted_sign_Z"] = -np.sign(x_ok["X"])
    x_ok = x_ok[x_ok["predicted_sign_Z"] != 0]

    pool_addr_path = find_pool_addr_cache()
    pool_df = pd.read_csv(pool_addr_path)
    print(f"[oos_check4:predict] реальный кэш адресов пулов: {pool_addr_path}")

    predictions = []
    for _, row in x_ok.iterrows():
        sym = row["symbol"]
        liq = evaluate_symbol_liquidity(sym, pool_df)
        tradeable = False
        if liq.get("status") == "оценено" and liq.get("round_trip_cost_pct") is not None:
            tradeable = bool(abs(row["X"]) * 100 > liq["round_trip_cost_pct"])
        entry = {
            "symbol": sym, "X": row["X"], "predicted_sign_Z": row["predicted_sign_Z"],
            "pool_address": liq.get("pool_address"), "tvl_usd_now": liq.get("tvl_usd_now"),
            "round_trip_cost_pct": liq.get("round_trip_cost_pct"), "liquidity_status": liq.get("status"),
            "tradeable": tradeable,
        }
        predictions.append(entry)
        print(f"    {sym}: X={row['X']:.4f} pred_sign_Z={row['predicted_sign_Z']:+.0f} "
              f"{liq.get('status')} round_trip={liq.get('round_trip_cost_pct')} tradeable={tradeable}")

    n_tradeable = sum(1 for p in predictions if p["tradeable"])
    out = {
        "generated_at_utc": now, "friday": FRIDAY,
        "rule": "predicted_sign_Z = -sign(X) (предрегистрировано, из исторического corr(X,Z)=-0.356 на 9 выходных)",
        "abs_X_outlier_threshold": ABS_X_OUTLIER_THRESHOLD, "n_excluded_outliers": n_excluded_outliers,
        "hit_rate_threshold": HIT_RATE_THRESHOLD,
        "n_predictions": len(predictions), "n_tradeable": n_tradeable, "predictions": predictions,
    }
    PREDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREDICT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[oos_check4:predict] {len(predictions)} реальных прогнозов ({n_tradeable} торгуемых) "
          f"записано в {PREDICT_PATH} (timestamp {now})")
    return 0


def verify() -> int:
    if not PREDICT_PATH.exists():
        raise SystemExit(f"[oos_check4:verify] нет файла прогноза {PREDICT_PATH} -- predict() не выполнялся")
    pred = json.loads(PREDICT_PATH.read_text())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[oos_check4:verify] реальное время проверки (UTC): {now}, прогноз был записан {pred['generated_at_utc']}")

    df = fetch_weekend_df(trades_end_hours_after_friday=3 * 24 + 14)
    z_ok = df[(df["z_start_n"].fillna(0) >= MIN_BRACKET_TRADES) & (df["z_end_n"].fillna(0) >= MIN_BRACKET_TRADES)].copy()
    z_ok = z_ok.dropna(subset=["Z", "symbol"])
    realized_sign = {row["symbol"]: (1.0 if row["Z"] > 0 else (-1.0 if row["Z"] < 0 else 0.0)) for _, row in z_ok.iterrows()}
    realized_Z = {row["symbol"]: row["Z"] for _, row in z_ok.iterrows()}

    rows = []
    for p in pred["predictions"]:
        sym = p["symbol"]
        if sym not in realized_sign or realized_sign[sym] == 0:
            rows.append({**p, "realized_Z": None, "realized_sign_Z": None, "hit": None,
                         "reason_missing": "нет реального Z (не прошёл фильтр тонких пулов на понедельник или symbol не найден)"})
            continue
        hit = bool(p["predicted_sign_Z"] == realized_sign[sym])
        rows.append({**p, "realized_Z": realized_Z[sym], "realized_sign_Z": realized_sign[sym], "hit": hit})

    scored = [r for r in rows if r["hit"] is not None]
    n_hit = sum(1 for r in scored if r["hit"])
    hit_rate = (n_hit / len(scored)) if scored else None

    scored_tradeable = [r for r in scored if r.get("tradeable")]
    n_hit_tradeable = sum(1 for r in scored_tradeable if r["hit"])
    hit_rate_tradeable = (n_hit_tradeable / len(scored_tradeable)) if scored_tradeable else None

    # Предрегистрация владельца (2026-09-05): порог 60% проверяется на
    # ТОРГУЕМЫХ (|X| > round-trip), не на всех -- все посчитаны для
    # честного сравнения, но вердикт выносится по торгуемой подвыборке.
    verdict = None
    if hit_rate_tradeable is not None:
        verdict = (f"ПРОШЛА (hit_rate_tradeable={hit_rate_tradeable:.1%} >= {HIT_RATE_THRESHOLD:.0%}, N_tradeable={len(scored_tradeable)})"
                   if hit_rate_tradeable >= HIT_RATE_THRESHOLD
                   else f"НЕ ПРОШЛА (hit_rate_tradeable={hit_rate_tradeable:.1%} < {HIT_RATE_THRESHOLD:.0%}, N_tradeable={len(scored_tradeable)})")
    else:
        verdict = "НЕТ ТОРГУЕМЫХ НАБЛЮДЕНИЙ -- честно доложить, порог не проверить"

    out = {
        "generated_at_utc": now, "friday": FRIDAY, "predict_generated_at_utc": pred["generated_at_utc"],
        "hit_rate_threshold": HIT_RATE_THRESHOLD, "n_predictions": len(pred["predictions"]),
        "n_scored": len(scored), "n_hit": n_hit, "hit_rate": hit_rate,
        "n_scored_tradeable": len(scored_tradeable), "n_hit_tradeable": n_hit_tradeable, "hit_rate_tradeable": hit_rate_tradeable,
        "verdict": verdict,
        "rows": rows,
    }
    VERIFY_PATH.parent.mkdir(parents=True, exist_ok=True)
    VERIFY_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[oos_check4:verify] ВСЕ: N={len(scored)} hit={n_hit} hit_rate={hit_rate}")
    print(f"[oos_check4:verify] ТОРГУЕМЫЕ: N={len(scored_tradeable)} hit={n_hit_tradeable} hit_rate={hit_rate_tradeable}")
    print(f"[oos_check4:verify] ВЕРДИКТ: {verdict}")
    print(f"[oos_check4:verify] результат записан в {VERIFY_PATH}")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TASK1_OOS_MODE", "predict")
    if mode == "predict":
        raise SystemExit(predict())
    elif mode == "verify":
        raise SystemExit(verify())
    else:
        raise SystemExit(f"неизвестный режим: {mode} (ожидается predict|verify)")
