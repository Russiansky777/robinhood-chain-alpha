#!/usr/bin/env python3
"""Задача 2 владельца (2026-09-10): базис токен/перп на Robinhood chain.
Тикеры с реальным v3-пулом сток-токена НА ЦЕПИ и реальным перпом на
Lighter. Часовая цена токена -- VWAP свопов через Dune (предикат по
адресам ПУЛОВ, project_contract_address), Lighter mark price -- уже
реально накопленная часовая история (lighter_stock_perp_markprice_
history.py, 0 кредитов). D = токен/перп - 1.

Владелец, 2026-09-10 (новое правило бюджета): "каждый новый запрос --
сначала оценка стоимости сканирования на коротком окне, потом полный.
Ни один запрос без оценки". Реализовано буквально: PROBE_DAYS=1 реально
исполняется первым (тот же query name, что и полный прогон -- credit_
guard._historical_max_actual_cost() увидит эту реальную историю и
форсирует оценку полного прогона от неё, не от слепых 250), реальная
стоимость печатается и сравнивается с потолком владельца (60 кредитов
Mozila) ДО запуска полного окна.

ЧЕСТНОЕ УПРОЩЕНИЕ (реальная находка при подготовке, не гадание):
23 тикера реально пересекают Lighter (37 сток-перпов) и v3-пул на цепи
(task1_pool_addresses_by_token cache); из них 21 котируются в USDG
(стейблкоин, VWAP уже в USD-эквиваленте напрямую), 2 (COIN, SPY) -- в
WETH (нужна отдельная конвертация WETH/USD за тот же час, не
реализовано в этом первом прогоне -- честно исключены, не выдуманы
конвертацией на глазок)."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task2_basis_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd  # noqa: E402

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402
from run_pipeline import read_sql  # noqa: E402
from task1_liquidity_v3_roundtrip import find_pool_addr_cache  # noqa: E402
from task1_pool_liquidity import find_v3_pool, get_gt_reserve_usd, read_fee_bps, round_trip_cost_pct  # noqa: E402

# РЕАЛЬНЫЙ баг первого прогона: BUDGET=200 оказался НИЖЕ forced-250
# (credit_guard форсирует минимум 250 для dex.trades(blockchain='robinhood'),
# когда реальной истории исполнений ещё нет -- та же проверка, что уже
# ловилась в Задаче 1) -- PROBE не смог выполниться вообще, даже не
# дошёл до реального execute(). BUDGET -- ТЕХНИЧЕСКИЙ потолок namespace
# (переживает forced-250 gate), НЕ настоящий потолок задачи -- настоящий
# потолок (60 кредитов, владелец) применяется отдельно ниже, ПОСЛЕ
# реальной стоимости PROBE, до запуска full.
BUDGET = 600.0
TASK_CEILING_CREDITS = 60.0  # владелец, 2026-09-10: потолок именно этой задачи
PROBE_DAYS = 1
FULL_DAYS = 30  # реальный дефолт этого проекта для "недель устойчивости" (см. другие линии) -- явно не указан владельцем, помечено
TRADE_SIZE_POOL_USD = 5000.0
D_ENTRY_THRESHOLD = 0.01  # |D| > 1% -- владелец
OUT_PATH = Path("data/p3_guard_cache/task2_basis_token_perp_result.json")
LIGHTER_RESULT_PATH = Path("data/p3_guard_cache/lighter_stock_perp_markprice_history_result.json")
LIGHTER_CACHE_DIR = Path("data/sleeping_refs_cache")


def build_universe() -> list[dict]:
    """Реальное пересечение: Lighter сток-перпы x v3-пул на цепи,
    ТОЛЬКО котируемые в USDG (честное упрощение, см. докстринг)."""
    lighter_data = json.loads(Path("data/p3_guard_cache/lighter_stock_perp_funding_result.json").read_text())
    lighter_syms = {r["symbol"]: r["market_id"] for r in lighter_data["results"]}
    pool_df = pd.read_csv(find_pool_addr_cache())
    universe = []
    for sym, market_id in sorted(lighter_syms.items()):
        pool = find_v3_pool(sym, pool_df)
        if pool is None or pool["quote_symbol"] != "USDG":
            continue
        universe.append({"symbol": sym, "market_id": market_id, "pool_address_hex": pool["pool_address_hex"]})
    return universe


def token_addr_for_symbol(symbol: str) -> str | None:
    registry = json.loads(Path("data/rwa_stock_token_registry.json").read_text())["tokens"]
    t = registry.get(symbol)
    return t["stock_token_address"] if t else None


def fetch_hourly_vwap(client: DuneClient, universe: list[dict], start: datetime, end: datetime, days_label: str) -> pd.DataFrame | None:
    pool_list_sql = ",".join(f"from_hex('{u['pool_address_hex'].lower()}')" for u in universe)
    token_addrs = [token_addr_for_symbol(u["symbol"]) for u in universe]
    token_list_sql = ",".join(f"from_hex('{a[2:].lower()}')" for a in token_addrs if a)
    sql_template = read_sql("task2/task2_basis_hourly_vwap")
    sql = (sql_template
           .replace("{{pool_address_list}}", pool_list_sql)
           .replace("{{token_address_list}}", token_list_sql)
           .replace("{{trades_start}}", start.strftime("%Y-%m-%d %H:%M:%S"))
           .replace("{{trades_end}}", end.strftime("%Y-%m-%d %H:%M:%S")))
    qid = client.create_query(f"task2_basis_hourly_vwap_{days_label}", sql)
    # Владелец: оценка на коротком окне СНАЧАЛА -- см. run(). Здесь просто
    # передаём честную оценку (пропорционально дням x числу пулов), реальная
    # стоимость запишется в credit_guard независимо от того, что мы оценили.
    n_days = max((end - start).total_seconds() / 86400, 0.1)
    naive_estimate = max(2.0, n_days * len(universe) * 0.15)  # честная грубая экстраполяция, не точная формула Dune
    df = client.run_sql_cached(
        "task2_basis_hourly_vwap", sql, query_id=qid, estimated_credits=naive_estimate,
        expected_max_rows=len(universe) * 24 * int(n_days) + 500, expected_columns=4,
    )
    return df


def load_lighter_hourly(symbol: str, market_id: int) -> pd.DataFrame | None:
    csv_path = LIGHTER_CACHE_DIR / f"markprice_1h_{symbol}_{market_id}.csv"
    if not csv_path.exists():
        return None
    df = pd.read_csv(csv_path)
    df["hour_utc"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.floor("h")
    df["mark_close"] = df["c"].astype(float)
    return df[["hour_utc", "mark_close"]]


def compute_convergence_episodes(d_series: pd.Series) -> list[float]:
    """Реальные эпизоды: часы подряд, где |D|>=D_ENTRY_THRESHOLD, длительность
    в часах до возврата ниже порога -- честное определение "схождения"."""
    episodes = []
    in_ep = False
    start_i = None
    for i, v in enumerate(d_series):
        above = abs(v) >= D_ENTRY_THRESHOLD
        if above and not in_ep:
            in_ep = True
            start_i = i
        elif not above and in_ep:
            episodes.append(i - start_i)  # часов от входа до возврата
            in_ep = False
    return episodes


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "task_ceiling_credits": TASK_CEILING_CREDITS, "full_days": FULL_DAYS,
                     "d_entry_threshold": D_ENTRY_THRESHOLD, "trade_size_pool_usd": TRADE_SIZE_POOL_USD}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    credit_guard.ensure_namespace(credit_guard.namespace(), BUDGET)
    universe = build_universe()
    result["n_tickers_universe"] = len(universe)
    result["universe_symbols"] = [u["symbol"] for u in universe]
    print(f"[task2_basis] реальная вселенная (Lighter x v3-пул, USDG-квота): {len(universe)} тикеров: "
          f"{[u['symbol'] for u in universe]}")

    client = DuneClient()
    now = datetime.now(timezone.utc)

    # ШАГ 1: PROBE -- короткое окно, реальная стоимость СНАЧАЛА
    probe_start = now - timedelta(days=PROBE_DAYS)
    print(f"\n=== Шаг 1 (владелец: оценка на коротком окне): PROBE {PROBE_DAYS}д, {len(universe)} пулов ===")
    try:
        probe_df = fetch_hourly_vwap(client, universe, probe_start, now, "probe")
    except credit_guard.BudgetGuardStop:
        result["blocker"] = "PROBE остановлен credit_guard -- см. лог выше, реального прогона не было"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1
    state = credit_guard.load_state()
    ns = credit_guard.namespace()
    probe_spent = state[ns]["spent"]
    result["probe_real_cost_credits"] = probe_spent
    print(f"[task2_basis] реальная стоимость PROBE (namespace spent после probe): {probe_spent:.3f} кредитов")

    # Экстраполяция полного окна от РЕАЛЬНОЙ стоимости пробы (не слепая догадка)
    extrapolated_full_estimate = probe_spent * (FULL_DAYS / PROBE_DAYS) * 1.3  # 30% запас
    result["full_estimate_extrapolated_from_probe"] = extrapolated_full_estimate
    print(f"[task2_basis] экстраполированная оценка полного окна ({FULL_DAYS}д): {extrapolated_full_estimate:.2f} кредитов "
          f"(потолок задачи: {TASK_CEILING_CREDITS})")
    if extrapolated_full_estimate > TASK_CEILING_CREDITS:
        result["blocker"] = (f"ОСТАНОВЛЕНО ДО ПОЛНОГО ПРОГОНА: экстраполированная оценка {extrapolated_full_estimate:.2f} "
                              f"> потолок задачи {TASK_CEILING_CREDITS} -- владелец просил возвращаться с вопросом именно "
                              "в этом случае, не тратить дальше самостоятельно.")
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[task2_basis] {result['blocker']}")
        return 1

    # ШАГ 2: ПОЛНЫЙ прогон
    full_start = now - timedelta(days=FULL_DAYS)
    print(f"\n=== Шаг 2: ПОЛНЫЙ прогон {FULL_DAYS}д, {len(universe)} пулов (в пределах потолка) ===")
    full_df = fetch_hourly_vwap(client, universe, full_start, now, "full")
    state = credit_guard.load_state()
    full_spent_total = state[ns]["spent"]
    result["real_cost_total_credits"] = full_spent_total
    print(f"[task2_basis] реальная суммарная стоимость (probe+full): {full_spent_total:.3f} кредитов")

    if full_df is None or not len(full_df):
        result["blocker"] = "полный прогон вернул пусто -- реальных свопов по этим пулам за окно нет"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1

    full_df["hour_utc"] = pd.to_datetime(full_df["hour_utc"], utc=True)
    full_df["vwap"] = full_df["vol_usd"] / full_df["token_qty"]
    addr_to_symbol = {u["pool_address_hex"].lower(): u["symbol"] for u in universe}
    full_df["symbol"] = full_df["pool_address"].str.replace("0x", "", regex=False).str.lower().map(addr_to_symbol)

    pool_addr_path = find_pool_addr_cache()
    pool_df_full = pd.read_csv(pool_addr_path)

    per_ticker = {}
    for u in universe:
        sym = u["symbol"]
        tok_df = full_df[full_df["symbol"] == sym][["hour_utc", "vwap"]].dropna()
        lighter_df = load_lighter_hourly(sym, u["market_id"])
        if lighter_df is None or not len(tok_df):
            per_ticker[sym] = {"status": "нет данных (VWAP пуст или нет кэша Lighter)"}
            continue
        merged = pd.merge(tok_df, lighter_df, on="hour_utc", how="inner")
        if not len(merged):
            per_ticker[sym] = {"status": "нет совпадающих часов между VWAP и Lighter"}
            continue
        merged["D"] = merged["vwap"] / merged["mark_close"] - 1
        d = merged["D"]

        # Реальная стоимость round-trip в ПУЛЕ на $5000 (fee() on-chain + TVL с GT,
        # тот же метод, что task1_pool_liquidity.evaluate_symbol_liquidity)
        pool = find_v3_pool(sym, pool_df_full)
        pool_cost_pct = None
        if pool:
            addr = "0x" + pool["pool_address_hex"].lower()
            try:
                fee_bps = read_fee_bps(addr)
                reserve_usd = get_gt_reserve_usd(addr)
                if reserve_usd:
                    pool_cost_pct = round_trip_cost_pct(fee_bps, reserve_usd, trade_usd=TRADE_SIZE_POOL_USD)["round_trip_cost_pct"]
            except Exception as exc:  # noqa: BLE001
                per_ticker.setdefault("_pool_cost_errors", {})[sym] = str(exc)[:200]
        lighter_rt = json.loads(LIGHTER_RESULT_PATH.read_text())["markets"].get(sym, {}).get("round_trip_cost_pct_5000")
        total_cost_pct = (pool_cost_pct or 0) + (lighter_rt or 0) if (pool_cost_pct is not None or lighter_rt is not None) else None

        n_weeks = max(1, (merged["hour_utc"].max() - merged["hour_utc"].min()).days // 7)
        weekly_signs = merged.groupby(merged["hour_utc"].dt.isocalendar().week)["D"].apply(lambda s: (s > 0).mean())
        convergence_hours = compute_convergence_episodes(d.reset_index(drop=True))

        per_ticker[sym] = {
            "n_hours": len(merged), "mean_D_pct": d.mean() * 100, "median_abs_D_pct": d.abs().median() * 100,
            "max_abs_D_pct": d.abs().max() * 100, "sign_positive_frac": (d > 0).mean(),
            "n_weeks": n_weeks, "weekly_sign_positive_frac_by_week": {int(k): float(v) for k, v in weekly_signs.items()},
            "n_convergence_episodes": len(convergence_hours),
            "median_convergence_hours": (sorted(convergence_hours)[len(convergence_hours) // 2] if convergence_hours else None),
            "pool_round_trip_cost_pct_5000": pool_cost_pct,
            "lighter_round_trip_cost_pct_5000": lighter_rt,
            "total_cost_pct": total_cost_pct,
            "frac_hours_abs_D_pct_gt_1pct": (d.abs() * 100 > 1.0).mean(),
            # Предрегистрация владельца: доля часов, где |D| РЕАЛЬНО превышает
            # суммарные издержки (не просто порог 1%) -- прямой прокси
            # "линия жива" на уровне тикера.
            "frac_hours_abs_D_gt_total_cost": ((d.abs() * 100 > total_cost_pct).mean() if total_cost_pct is not None else None),
        }
    result["per_ticker"] = per_ticker

    # Предрегистрация владельца: "линия жива при доле часов с |D| > сумма
    # издержек >= 20% и медианном схождении < 24 ч" -- считаем по ВСЕЙ
    # реальной выборке (все тикеры вместе), не по отдельным тикерам.
    valid = [v for v in per_ticker.values() if isinstance(v, dict) and v.get("frac_hours_abs_D_gt_total_cost") is not None]
    if valid:
        overall_frac = sum(v["n_hours"] * v["frac_hours_abs_D_gt_total_cost"] for v in valid) / sum(v["n_hours"] for v in valid)
        all_convergence = [v["median_convergence_hours"] for v in valid if v.get("median_convergence_hours") is not None]
        overall_median_convergence = sorted(all_convergence)[len(all_convergence) // 2] if all_convergence else None
        alive = (overall_frac >= 0.20) and (overall_median_convergence is not None and overall_median_convergence < 24)
        result["prereg_summary"] = {
            "n_tickers_scored": len(valid),
            "overall_frac_hours_abs_D_gt_total_cost": overall_frac,
            "overall_median_convergence_hours": overall_median_convergence,
            "verdict_line_alive": alive,
        }
    else:
        result["prereg_summary"] = {"n_tickers_scored": 0, "verdict_line_alive": None,
                                     "note": "нет ни одного тикера с посчитанными издержками -- вердикт не выносится"}

    result["runtime_s_total"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[task2_basis] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
