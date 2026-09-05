#!/usr/bin/env python3
"""Задача (владелец, 2026-09-05, "дешевле и важнее" Задачи 2): полный
исторический бэкфилл спреда фандинга Lighter x Hyperliquid по 13 парам
из `data/funding_pairs.json`, с 2026-07-05 (реальная earliest точка
Lighter BTC, `funding_historical_probe_result.json`) по сейчас.

Реальные подтверждённые параметры (analysis/funding_historical_probe.py,
данные с 2026-09-05, только чтение, публичные API, без Dune):
  - Lighter `/api/v1/fundings`: пагинация НАЗАД, `count_back=750`,
    `resolution=1h` -- ТОТ ЖЕ метод, что `p4_lighter_markets.py::
    fetch_funding_history` (уже реально работает на другом Lighter-
    деплое, здесь -- api.rh.lighter.xyz, тот же хост, что хедж P5).
    `rate` -- уже ПРОЦЕНТ за час (4 независимых доказательства,
    funding_spread_hourly_snapshot.py).
  - Hyperliquid `fundingHistory` (POST /info {"type":"fundingHistory",
    "coin","startTime","endTime"}): реальный лимит страницы -- 500
    записей за вызов (подтверждено: запрос на 1596ч вернул ровно 500) --
    пагинация ВПЕРЁД, следующий `startTime` = `time` последней записи + 1мс.
    `fundingRate` -- ДОЛЯ (fraction) за час (тот же формат/единица, что
    `ctx["funding"]` в metaAndAssetCtxs, ×100 для сопоставимости с Lighter).
  - `predictedFundings` (POST /info {"type":"predictedFundings"}) --
    ТОЛЬКО как разовый снимок ТЕКУЩЕГО момента (нет исторического
    параметра у этого эндпоинта) -- сверка с Binance/Bybit ("HlPerp"
    внутри НЕ считается независимой проверкой, владелец, 2026-09-05 --
    это тот же показатель HL, представленный дважды, не независимый
    источник).

Единицы (владелец, дословно): "Lighter... проценты за час, подтверждено"
"Hyperliquid... доля за час, подтверждено" -- используются КАК ЕСТЬ.
Аннуализация -- ×8760 (24×365), тот же коэффициент, что уже
устоялся в проекте (docs/P4_RECON.md, 3.504%/год = 0.0004%/час×8760)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

PAIRS_PATH = Path("data/funding_pairs.json")
OUT_PATH = Path("data/p3_guard_cache/funding_historical_backfill_result.json")
RAW_CACHE_DIR = Path("data/funding_historical_cache")

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
HYPERLIQUID_API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-funding-backfill/1.0"}

BACKFILL_START_UTC = "2026-07-05T00:00:00Z"  # реальная earliest точка Lighter BTC (см. докстринг)
ANNUALIZATION_HOURS = 24 * 365  # 8760, тот же коэффициент, что уже устоялся в проекте (3.504%/год)
BREAKEVEN_BPS_PER_8H = 1.3  # владелец: "безубыточность из чужих оценок"
BREAKEVEN_HOURLY_PCT = (BREAKEVEN_BPS_PER_8H / 10_000) / 8 * 100  # bps -> доля -> за 8ч -> за час -> %
KILL_ANNUAL_PCT = 30.0
KILL_HOURLY_PCT = KILL_ANNUAL_PCT / ANNUALIZATION_HOURS
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 3.0


def _retry_get(url: str, params: dict) -> requests.Response:
    last_exc = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"HTTP {r.status_code}")
            return r
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            time.sleep(RETRY_BACKOFF_S * (2 ** attempt))
    raise last_exc


def _retry_post(url: str, json_body: dict) -> requests.Response:
    last_exc = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            r = requests.post(url, json=json_body, headers=HEADERS, timeout=30)
            if r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"HTTP {r.status_code}")
            return r
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            time.sleep(RETRY_BACKOFF_S * (2 ** attempt))
    raise last_exc


def fetch_lighter_history(market_id: int, since_unix: int) -> list[dict]:
    """Пагинация НАЗАД (count_back=750), реальный метод из
    p4_lighter_markets.py::fetch_funding_history, адаптирован на
    api.rh.lighter.xyz. Останавливается на коротком/пустом ответе ИЛИ
    при достижении since_unix -- не гадаем более раннюю границу."""
    now = int(time.time())
    records: list[dict] = []
    end_ts = now
    seen: set[int] = set()
    for _ in range(200):  # генеральный предохранитель, реальных данных с 05.07 в разы меньше
        r = _retry_get(f"{LIGHTER_API_BASE}/api/v1/fundings", {
            "market_id": market_id, "resolution": "1h",
            "start_timestamp": since_unix, "end_timestamp": end_ts, "count_back": 750,
        })
        if r.status_code != 200:
            break
        page = r.json().get("fundings", [])
        if not page:
            break
        new_page = [x for x in page if x.get("timestamp") not in seen]
        if not new_page:
            break
        records.extend(new_page)
        seen.update(x["timestamp"] for x in new_page)
        oldest = min(x["timestamp"] for x in new_page)
        if len(new_page) < 750 or oldest <= since_unix:
            break
        end_ts = oldest - 1
    return records


def fetch_hyperliquid_history(coin: str, since_ms: int) -> list[dict]:
    """Пагинация ВПЕРЁД (реальный лимит страницы 500, подтверждено
    2026-09-05, funding_historical_probe_result.json), следующий
    startTime = time последней записи страницы + 1мс."""
    now_ms = int(time.time() * 1000)
    records: list[dict] = []
    start_ms = since_ms
    for _ in range(200):
        r = _retry_post(f"{HYPERLIQUID_API_BASE}/info", {"type": "fundingHistory", "coin": coin, "startTime": start_ms, "endTime": now_ms})
        if r.status_code != 200:
            break
        page = r.json()
        if not isinstance(page, list) or not page:
            break
        records.extend(page)
        last_time = max(x["time"] for x in page)
        if len(page) < 500 or last_time >= now_ms:
            break
        start_ms = last_time + 1
    return records


def process_pair(pair: dict, since_unix: int, since_ms: int) -> pd.DataFrame:
    symbol = pair["symbol"]
    print(f"=== {symbol} ({pair['cohort']}) ===")
    l_records = fetch_lighter_history(pair["lighter_market_id"], since_unix)
    print(f"  Lighter: {len(l_records)} реальных часовых записей")
    h_records = fetch_hyperliquid_history(pair["hyperliquid_raw_symbol"], since_ms)
    print(f"  Hyperliquid: {len(h_records)} реальных часовых записей")

    l_df = pd.DataFrame(l_records)
    if len(l_df):
        l_df["hour"] = pd.to_datetime(l_df["timestamp"], unit="s", utc=True).dt.floor("h")
        l_df = l_df.rename(columns={"rate": "lighter_rate_pct"})[["hour", "lighter_rate_pct"]]
        l_df = l_df.groupby("hour", as_index=False).last()  # на случай дублей внутри часа

    h_df = pd.DataFrame(h_records)
    if len(h_df):
        h_df["hour"] = pd.to_datetime(h_df["time"], unit="ms", utc=True).dt.floor("h")
        h_df["hl_rate_pct"] = h_df["fundingRate"].astype(float) * 100
        h_df = h_df.groupby("hour", as_index=False)["hl_rate_pct"].last()

    if not len(l_df) or not len(h_df):
        return pd.DataFrame(columns=["hour", "symbol", "cohort", "lighter_rate_pct", "hl_rate_pct", "spread_pct"])

    merged = l_df.merge(h_df, on="hour", how="inner")
    merged["symbol"] = symbol
    merged["cohort"] = pair["cohort"]
    merged["spread_pct"] = merged["lighter_rate_pct"] - merged["hl_rate_pct"]
    print(f"  выровнено по часу: {len(merged)} общих часов")
    return merged


def sign_run_stats(spread: pd.Series) -> dict:
    s = np.sign(spread.values)
    s = s[s != 0]  # исключаем ровно 0 (крайне маловероятно, но честно)
    if len(s) < 2:
        return {"n_sign_changes": 0, "median_run_length_hours": None, "n_runs": 0}
    changes = int((s[1:] != s[:-1]).sum())
    run_lengths = []
    cur = 1
    for i in range(1, len(s)):
        if s[i] == s[i - 1]:
            cur += 1
        else:
            run_lengths.append(cur)
            cur = 1
    run_lengths.append(cur)
    return {"n_sign_changes": changes, "median_run_length_hours": float(np.median(run_lengths)), "n_runs": len(run_lengths)}


def cohort_stats(df: pd.DataFrame) -> dict:
    if not len(df):
        return {"n_hours": 0}
    abs_spread_annual = (df["spread_pct"].abs() * ANNUALIZATION_HOURS)
    frac_above_breakeven = float((df["spread_pct"].abs() > BREAKEVEN_HOURLY_PCT).mean())
    frac_above_kill = float((df["spread_pct"].abs() > KILL_HOURLY_PCT).mean())
    # sign-run статистика -- ПО КАЖДОЙ ПАРЕ ОТДЕЛЬНО (смены знака имеют смысл
    # только внутри одного непрерывного ряда), затем объединяем длины серий.
    all_runs: list[float] = []
    total_changes = 0
    for sym, g in df.sort_values("hour").groupby("symbol"):
        st = sign_run_stats(g["spread_pct"])
        total_changes += st["n_sign_changes"]
        if st["median_run_length_hours"] is not None:
            all_runs.extend([st["median_run_length_hours"]] * st["n_runs"])
    return {
        "n_hours": int(len(df)), "n_pairs": int(df["symbol"].nunique()),
        "median_abs_spread_annual_pct": float(abs_spread_annual.median()),
        "p25_abs_spread_annual_pct": float(abs_spread_annual.quantile(0.25)),
        "p75_abs_spread_annual_pct": float(abs_spread_annual.quantile(0.75)),
        "p90_abs_spread_annual_pct": float(abs_spread_annual.quantile(0.90)),
        "frac_hours_above_breakeven_1_3bps_8h": frac_above_breakeven,
        "frac_hours_above_kill_30pct_year": frac_above_kill,
        "total_sign_changes_all_pairs": total_changes,
        "median_run_length_hours_pooled": float(np.median(all_runs)) if all_runs else None,
    }


def weekly_stability(df: pd.DataFrame) -> list[dict]:
    if not len(df):
        return []
    df = df.copy()
    df["week"] = df["hour"].dt.to_period("W").apply(lambda p: p.start_time.strftime("%Y-%m-%d"))
    rows = []
    for week, g in sorted(df.groupby("week")):
        abs_annual = g["spread_pct"].abs() * ANNUALIZATION_HOURS
        rows.append({"week_start": week, "n_hours": int(len(g)), "median_abs_spread_annual_pct": float(abs_annual.median())})
    return rows


def check_predicted_fundings(all_symbols: list[str]) -> dict:
    """Разовый ТЕКУЩИЙ снимок (нет исторического параметра у этого
    эндпоинта) -- сверка нашего расчёта HL с Binance/Bybit. HL-запись
    ВНУТРИ predictedFundings НЕ считается независимой проверкой
    (владелец, 2026-09-05 -- тот же показатель, представленный дважды)."""
    r = _retry_post(f"{HYPERLIQUID_API_BASE}/info", {"type": "predictedFundings"})
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    data = {entry[0]: dict(entry[1]) for entry in r.json() if isinstance(entry, list) and len(entry) == 2}
    out = {}
    for sym in all_symbols:
        entry = data.get(sym)
        if entry is None:
            out[sym] = {"error": "нет в снимке predictedFundings"}
            continue
        venues = {}
        for venue in ("BinPerp", "BybitPerp"):
            v = entry.get(venue)
            if v is None:
                continue
            rate = float(v["fundingRate"])
            interval_h = float(v["fundingIntervalHours"])
            venues[venue] = {"rate_raw": rate, "interval_hours": interval_h,
                              "annualized_pct": rate * (ANNUALIZATION_HOURS / interval_h) * 100}
        out[sym] = {"venues": venues}
    return out


def run() -> int:
    pairs = json.loads(PAIRS_PATH.read_text())["pairs"]
    since_unix = int(pd.Timestamp(BACKFILL_START_UTC).timestamp())
    since_ms = since_unix * 1000

    all_rows = []
    for pair in pairs:
        df = process_pair(pair, since_unix, since_ms)
        all_rows.append(df)
    full = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()

    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if len(full):
        full.to_csv(RAW_CACHE_DIR / "funding_spread_hourly_full.csv", index=False)

    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "backfill_start_utc": BACKFILL_START_UTC, "n_pairs": len(pairs),
              "breakeven_hourly_pct_threshold": BREAKEVEN_HOURLY_PCT, "kill_hourly_pct_threshold": KILL_HOURLY_PCT}

    for cohort in ("primary", "exploratory"):
        sub = full[full["cohort"] == cohort] if len(full) else full
        stats = cohort_stats(sub)
        stats["weekly"] = weekly_stability(sub)
        result[cohort] = stats
        print(f"\n=== Когорта {cohort} ===")
        print(json.dumps({k: v for k, v in stats.items() if k != "weekly"}, indent=2, ensure_ascii=False))

    # Предрегистрация (владелец, дословно): primary -- медиана >=30%/год
    # после издержек при доле прибыльного времени >=50%; exploratory --
    # вдвое выше (медиана >=60%/год, тот же порог доли прибыльного времени).
    def verdict(stats: dict, median_threshold: float) -> str:
        if stats.get("n_hours", 0) == 0:
            return "НЕТ ДАННЫХ"
        med = stats["median_abs_spread_annual_pct"]
        frac = stats["frac_hours_above_breakeven_1_3bps_8h"]
        alive = med >= median_threshold and frac >= 0.5
        return f"{'ЖИВА' if alive else 'НЕ ПРОХОДИТ'} (медиана={med:.2f}%/год, порог={median_threshold}%/год, доля_прибыльного_времени={frac:.1%})"

    result["primary"]["verdict"] = verdict(result["primary"], 30.0)
    result["exploratory"]["verdict"] = verdict(result["exploratory"], 60.0)
    print(f"\n[funding_backfill] primary: {result['primary']['verdict']}")
    print(f"[funding_backfill] exploratory: {result['exploratory']['verdict']}")

    all_symbols = [p["symbol"] for p in pairs]
    print("\n=== Сверка с predictedFundings (снимок СЕЙЧАС, Binance/Bybit -- не HL) ===")
    result["predicted_fundings_crosscheck"] = check_predicted_fundings(all_symbols)
    for sym, v in result["predicted_fundings_crosscheck"].items():
        print(f"  {sym}: {v}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[funding_backfill] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
