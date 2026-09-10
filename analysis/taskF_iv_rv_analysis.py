#!/usr/bin/env python3
"""Задача F -- расчёт после Шага 0 (ликвидность подтверждена) и Шага 1
(near-ATM сделки Derive собраны, источник RV подтверждён). БЕСПЛАТНЫЙ
скрипт (все источники -- публичные API без авторизации): Kraken OHLC
(RV), Derive/Lyra get_funding_rate_history (фандинг хеджа), реальные
поля комиссий из уже собранных данных get_instruments (Шаг 0b).

Гипотеза (владелец): IV систематически выше RV -- продавец опциона
получает премию за риск волатильности. Предрегистрация (владелец):
линия жива, если медианная (IV-RV) после ВСЕХ издержек >= 15% годовых
(в п.п. годовой волатильности, т.к. IV/RV уже в годовом выражении) при
доле положительных периодов >= 60%, и максимальная просадка на
хвостовых событиях не превышает 3x среднемесячного дохода.

Реальная формула комиссии Derive (Шаг 0b -- поля из живого
/public/get_instruments; независимо подтверждена численным примером из
официальной документации docs.derive.xyz через WebSearch, т.к. прямой
доступ к docs.derive.xyz заблокирован сетевой политикой этого раннера):
    fee = min(base_fee + taker_fee_rate * qty * spot,
              mark_price_fee_rate_cap * premium * qty)
Реальные значения полей на спот-опционах (Шаг 0b): base_fee=0.5 (USDC,
флэт за ордер), taker_fee_rate=0.0003, mark_price_fee_rate_cap=0.125.

Bid-ask спред НЕ измерен напрямую (Derive public API отдаёт только
сделки, не стакан/котировки, даже исторически) -- оценка методом Ролла
(Roll 1984): spread = 2*sqrt(max(0, -cov(dP_t, dP_{t-1}))) по
последовательным сделкам ОДНОГО инструмента, честно помечено как
ОЦЕНКА, не измерение."""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskF-iv-rv-analysis/1.0"}
LYRA_BASE = "https://api.lyra.finance"
KRAKEN_BASE = "https://api.kraken.com/0/public"

IN_TRADES_PATH = Path("data/p3_guard_cache/taskF_derive_near_atm_trades.json")
OUT_PATH = Path("data/p3_guard_cache/taskF_iv_rv_result.json")

KRAKEN_PAIR = {"BTC": "XBTUSD", "ETH": "ETHUSD"}
LYRA_PERP = {"BTC": "BTC-PERP", "ETH": "ETH-PERP"}

RISK_FREE_RATE = 0.0  # владелец не задавал -- крипто-опционы традиционно считаются с r=0 (Deribit/Lyra конвенция)
ANNUALIZE_HOURS = math.sqrt(24 * 365)

# Реальные значения полей комиссии Derive на спот-опционах (Шаг 0b, live /public/get_instruments)
DERIVE_BASE_FEE_USDC = 0.5
DERIVE_TAKER_FEE_RATE = 0.0003
DERIVE_FEE_CAP_PCT_OF_PREMIUM = 0.125

PREREG_MEDIAN_NET_EDGE_ANNUAL_PP = 15.0  # п.п. годовой волатильности
PREREG_FRAC_POSITIVE = 0.60
PREREG_MAX_DRAWDOWN_MULT_OF_AVG_MONTHLY = 3.0


# ---------- Black-Scholes ----------
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
        return intrinsic
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_delta(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    if T <= 0 or sigma <= 0:
        return 1.0 if (is_call and S > K) else (0.0 if is_call else (-1.0 if S < K else 0.0))
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0


def implied_vol_bisection(price: float, S: float, K: float, T: float, r: float, is_call: bool,
                           lo: float = 0.01, hi: float = 5.0, tol: float = 1e-5, max_iter: int = 80) -> float | None:
    intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
    if price < intrinsic - 1e-6 or price <= 0:
        return None  # цена вне ноарбитражных границ -- не решается BS, честно None, не гадаем
    price_hi = bs_price(S, K, T, r, hi, is_call)
    if price > price_hi:
        return None  # цена выше цены при 500% вол -- за пределами разумного диапазона решения
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        p_mid = bs_price(S, K, T, r, mid, is_call)
        if abs(p_mid - price) < tol:
            return mid
        if p_mid < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def bs_vega_per_1pct(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    h = 0.001
    p_up = bs_price(S, K, T, r, sigma + h, is_call)
    p_dn = bs_price(S, K, T, r, max(sigma - h, 1e-4), is_call)
    vega_raw = (p_up - p_dn) / (2 * h)  # $ за 1.0 (100 п.п.) сигмы
    return vega_raw / 100.0  # $ за 1 п.п. годовой волатильности


# ---------- Kraken RV source ----------
def fetch_kraken_ohlc_full(pair: str, since_ts: int) -> list[tuple[int, float]]:
    """Пагинация вперёд по времени через реальный курсор `last`,
    подтверждено Шагом 1a (реальный 200, error:[])."""
    out: list[tuple[int, float]] = []
    cur_since = since_ts
    for _ in range(30):  # предохранитель -- Kraken отдаёт ~720 свечей/вызов, 30 вызовов с запасом покрывают весь диапазон
        try:
            r = requests.get(f"{KRAKEN_BASE}/OHLC", params={"pair": pair, "interval": 60, "since": cur_since},
                              headers=HEADERS, timeout=20)
        except requests.exceptions.RequestException:
            break
        if r.status_code != 200:
            break
        body = r.json()
        if body.get("error"):
            break
        result = body.get("result", {})
        last = result.pop("last", None)
        rows = next(iter(result.values()), [])
        if not rows:
            break
        for row in rows:
            out.append((int(row[0]), float(row[4])))  # (unix_ts, close)
        if last is None or last <= cur_since:
            break
        cur_since = last
        if last * 1000 >= int(time.time() * 1000) - 3600_000:
            break  # дошли до текущего часа
        time.sleep(0.5)
    out.sort(key=lambda x: x[0])
    return out


# ---------- Derive funding rate history (Шаг 1c, лимит 30 дней/вызов -- реальное ограничение API) ----------
def fetch_funding_rate_history(instrument_name: str, window_days: int = 90) -> list[dict]:
    out = []
    now = datetime.now(timezone.utc)
    n_windows = math.ceil(window_days / 29)  # реальный лимит API -- не более 30 дней на вызов, запас в 1 день
    for i in range(n_windows):
        end_dt = now - timedelta(days=29 * i)
        start_dt = end_dt - timedelta(days=29)
        try:
            r = requests.post(f"{LYRA_BASE}/public/get_funding_rate_history", json={
                "instrument_name": instrument_name,
                "start_timestamp": int(start_dt.timestamp() * 1000),
                "end_timestamp": int(end_dt.timestamp() * 1000),
            }, headers={**HEADERS, "Content-Type": "application/json"}, timeout=20)
        except requests.exceptions.RequestException:
            continue
        if r.status_code != 200:
            continue
        body = r.json().get("result", {})
        events = body.get("funding_rate_history") if isinstance(body, dict) else body
        if isinstance(events, list):
            out.extend(events)
        time.sleep(0.3)
    return out


def roll_spread_estimate(prices: list[float]) -> float | None:
    """Roll (1984): spread = 2*sqrt(max(0,-cov(dP_t,dP_{t-1}))). Реальная
    оценка эффективного спреда по последовательным сделкам ОДНОГО
    инструмента -- НЕ измерение стакана (Derive его не отдаёт даже
    исторически), честно помечено как оценка."""
    if len(prices) < 5:
        return None
    diffs = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    pairs = list(zip(diffs[:-1], diffs[1:]))
    n = len(pairs)
    if n < 3:
        return None
    mean_a = sum(p[0] for p in pairs) / n
    mean_b = sum(p[1] for p in pairs) / n
    cov = sum((p[0] - mean_a) * (p[1] - mean_b) for p in pairs) / n
    if cov >= 0:
        return None  # положительная автоковариация -- модель Ролла не применима, честно None
    return 2 * math.sqrt(-cov)


def run() -> int:
    if not IN_TRADES_PATH.exists():
        print(f"[taskF_analysis] {IN_TRADES_PATH} не найден.")
        return 1
    data = json.loads(IN_TRADES_PATH.read_text())
    trades_by_currency = data.get("near_atm_trades", {})

    now = datetime.now(timezone.utc)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "by_currency": {}}

    for currency, trades in trades_by_currency.items():
        print(f"[taskF_analysis] {currency}: {len(trades)} near-ATM сделок, загружаю RV-источник (Kraken)...")
        pair = KRAKEN_PAIR.get(currency)
        if not pair:
            continue
        earliest_trade_ts = min(t["timestamp"] for t in trades) / 1000
        since_ts = int(earliest_trade_ts) - 3600
        ohlc = fetch_kraken_ohlc_full(pair, since_ts)
        print(f"[taskF_analysis] {currency}: Kraken OHLC получено {len(ohlc)} часовых точек, "
              f"{datetime.fromtimestamp(ohlc[0][0], tz=timezone.utc) if ohlc else '?'} -> "
              f"{datetime.fromtimestamp(ohlc[-1][0], tz=timezone.utc) if ohlc else '?'}")
        ohlc_ts = [p[0] for p in ohlc]
        ohlc_px = [p[1] for p in ohlc]

        print(f"[taskF_analysis] {currency}: реальная история фандинга ({LYRA_PERP[currency]}, 90д, окна по 29д)...")
        funding_events = fetch_funding_rate_history(LYRA_PERP[currency], 90)
        funding_rates = []
        for ev in funding_events:
            try:
                funding_rates.append(float(ev.get("funding_rate")))
            except (TypeError, ValueError):
                pass
        avg_funding_rate_per_event = sum(funding_rates) / len(funding_rates) if funding_rates else None
        print(f"[taskF_analysis] {currency}: {len(funding_events)} событий фандинга, "
              f"средняя ставка/событие={avg_funding_rate_per_event}")

        # Roll-оценка спреда -- по инструментам с достаточным числом сделок
        by_instrument: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for t in trades:
            by_instrument[t["instrument_name"]].append((t["timestamp"], float(t["trade_price"])))
        spread_estimates = []
        for instr, pts in by_instrument.items():
            pts.sort(key=lambda x: x[0])
            prices = [p[1] for p in pts]
            est = roll_spread_estimate(prices)
            if est is not None and prices:
                spread_estimates.append(est / (sum(prices) / len(prices)))  # относительный спред (доля премии)
        median_rel_spread = sorted(spread_estimates)[len(spread_estimates) // 2] if spread_estimates else None
        print(f"[taskF_analysis] {currency}: Roll-оценка относительного спреда -- "
              f"{len(spread_estimates)} инструментов с оценкой, медиана={median_rel_spread}")

        def realized_vol_forward(t_ms: int, tenor_days: float) -> float | None:
            t_s = t_ms / 1000
            end_s = t_s + tenor_days * 86400
            if end_s > ohlc_ts[-1] if ohlc_ts else True:
                return None  # окно ещё не наступило целиком -- не считаем, честно пропускаем
            import bisect
            i0 = bisect.bisect_left(ohlc_ts, t_s)
            i1 = bisect.bisect_right(ohlc_ts, end_s)
            window_px = ohlc_px[i0:i1]
            if len(window_px) < 8:
                return None
            log_rets = [math.log(window_px[i] / window_px[i - 1]) for i in range(1, len(window_px))
                        if window_px[i - 1] > 0 and window_px[i] > 0]
            if len(log_rets) < 5:
                return None
            mean = sum(log_rets) / len(log_rets)
            var = sum((x - mean) ** 2 for x in log_rets) / (len(log_rets) - 1)
            return math.sqrt(var) * ANNUALIZE_HOURS

        observations = []
        n_skipped_future = 0
        n_skipped_iv_fail = 0
        for t in trades:
            S, K = float(t["index_price"]), float(t["strike"])
            T_years = t["days_to_expiry_at_trade"] / 365.0
            is_call = t["option_type"] == "C"
            price = float(t["trade_price"])
            iv = implied_vol_bisection(price, S, K, T_years, RISK_FREE_RATE, is_call)
            if iv is None:
                n_skipped_iv_fail += 1
                continue
            rv = realized_vol_forward(t["timestamp"], t["days_to_expiry_at_trade"])
            if rv is None:
                n_skipped_future += 1
                continue
            vega_1pct = bs_vega_per_1pct(S, K, T_years, RISK_FREE_RATE, iv, is_call)
            if vega_1pct <= 0:
                continue
            delta = bs_delta(S, K, T_years, RISK_FREE_RATE, iv, is_call)

            # --- реальные издержки, $ за 1 контракт, round-trip ---
            fee_open = min(DERIVE_BASE_FEE_USDC + DERIVE_TAKER_FEE_RATE * S, DERIVE_FEE_CAP_PCT_OF_PREMIUM * price)
            fee_close = fee_open  # приближение -- цена закрытия неизвестна без второй реальной сделки, честно помечено
            spread_cost_usd = (median_rel_spread * price) if median_rel_spread else 0.0
            hedge_notional = abs(delta) * S
            n_rebalances = max(1, math.ceil(t["days_to_expiry_at_trade"]))  # владелец не задал частоту -- документированное допущение: 1 ребаланс/день
            perp_taker_fee_rate = 0.0003  # владелец не задавал; используем ту же реальную ставку тейкера, что и у опционов Derive (единая тарифная линейка биржи, отдельного perp-поля в наших живых данных нет)
            perp_fee_cost_usd = perp_taker_fee_rate * hedge_notional * (n_rebalances + 2)  # + открытие/закрытие хеджа
            funding_cost_usd = (abs(avg_funding_rate_per_event) * hedge_notional * (t["days_to_expiry_at_trade"] / (8 / 24))
                                 if avg_funding_rate_per_event is not None else 0.0)  # фандинг Derive -- события раз в 8ч (стандарт биржи)
            total_cost_usd = fee_open + fee_close + spread_cost_usd + perp_fee_cost_usd + funding_cost_usd
            cost_vol_points = total_cost_usd / vega_1pct

            raw_edge_pp = (iv - rv) * 100
            net_edge_pp = raw_edge_pp - cost_vol_points
            pnl_usd_approx = net_edge_pp * vega_1pct  # линеаризация через Vega -- приближение, не полная симуляция хеджа

            observations.append({
                "instrument_name": t["instrument_name"], "timestamp": t["timestamp"],
                "iv": iv, "rv": rv, "raw_edge_pp": raw_edge_pp, "cost_vol_points": cost_vol_points,
                "net_edge_pp": net_edge_pp, "pnl_usd_approx": pnl_usd_approx,
                "total_cost_usd": total_cost_usd, "vega_1pct": vega_1pct,
            })

        n = len(observations)
        cur_result = {
            "n_near_atm_trades": len(trades), "n_iv_solve_failed": n_skipped_iv_fail,
            "n_future_unresolved_rv": n_skipped_future, "n_usable_observations": n,
            "kraken_ohlc_points": len(ohlc), "n_funding_events": len(funding_events),
            "avg_funding_rate_per_event": avg_funding_rate_per_event,
            "median_relative_spread_estimate": median_rel_spread,
            "n_instruments_with_spread_estimate": len(spread_estimates),
        }
        if n >= 5:
            edges = sorted(o["net_edge_pp"] for o in observations)
            median_net_edge = edges[n // 2]
            frac_positive = sum(1 for e in edges if e > 0) / n
            raw_edges = sorted(o["raw_edge_pp"] for o in observations)
            median_raw_edge = raw_edges[n // 2]

            # недельная устойчивость
            by_week: dict[str, list[float]] = defaultdict(list)
            for o in observations:
                wk = datetime.fromtimestamp(o["timestamp"] / 1000, tz=timezone.utc).strftime("%G-W%V")
                by_week[wk].append(o["net_edge_pp"])
            weekly_medians = {wk: sorted(v)[len(v) // 2] for wk, v in by_week.items()}
            n_weeks_positive = sum(1 for v in weekly_medians.values() if v > 0)

            # просадка на хвостовых событиях: топ-5 однодневных |log-return| БА в окне сделок
            daily_px: dict[str, float] = {}
            for ts, px in ohlc:
                day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                daily_px[day] = px  # последняя цена часа этого дня -- приближение close-of-day
            days_sorted = sorted(daily_px.keys())
            daily_rets = [(days_sorted[i], math.log(daily_px[days_sorted[i]] / daily_px[days_sorted[i - 1]]))
                          for i in range(1, len(days_sorted)) if daily_px[days_sorted[i - 1]] > 0]
            tail_days = sorted(daily_rets, key=lambda x: -abs(x[1]))[:5]
            pnl_by_day: dict[str, float] = defaultdict(float)
            for o in observations:
                day = datetime.fromtimestamp(o["timestamp"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                pnl_by_day[day] += o["pnl_usd_approx"]
            tail_day_pnls = [pnl_by_day.get(d, 0.0) for d, _ in tail_days]
            worst_tail_day_pnl = min(tail_day_pnls) if tail_day_pnls else None
            all_days = sorted(pnl_by_day.keys())
            n_months = max(1, len(all_days) / 30.0)
            avg_monthly_pnl = sum(pnl_by_day.values()) / n_months

            prereg_median_ok = median_net_edge >= PREREG_MEDIAN_NET_EDGE_ANNUAL_PP
            prereg_frac_ok = frac_positive >= PREREG_FRAC_POSITIVE
            prereg_drawdown_ok = (worst_tail_day_pnl is None or avg_monthly_pnl <= 0 or
                                   worst_tail_day_pnl >= -PREREG_MAX_DRAWDOWN_MULT_OF_AVG_MONTHLY * abs(avg_monthly_pnl))

            cur_result.update({
                "median_raw_edge_pp": median_raw_edge, "median_net_edge_pp": median_net_edge,
                "frac_positive_net_edge": frac_positive,
                "n_weeks_total": len(weekly_medians), "n_weeks_positive": n_weeks_positive,
                "weekly_medians_pp": weekly_medians,
                "tail_days_sample": tail_days, "worst_tail_day_pnl_usd_approx": worst_tail_day_pnl,
                "avg_monthly_pnl_usd_approx": avg_monthly_pnl,
                "prereg_median_ok": prereg_median_ok, "prereg_frac_positive_ok": prereg_frac_ok,
                "prereg_drawdown_ok": prereg_drawdown_ok,
                "preregistration_met": bool(prereg_median_ok and prereg_frac_ok and prereg_drawdown_ok),
            })
        else:
            cur_result["blocker"] = f"только {n} пригодных наблюдений (после IV/RV-фильтров) -- недостаточно для вывода"

        result["by_currency"][currency] = cur_result
        print(f"[taskF_analysis] {currency}: usable={n}, median_net_edge_pp={cur_result.get('median_net_edge_pp')}, "
              f"frac_positive={cur_result.get('frac_positive_net_edge')}, "
              f"preregistration_met={cur_result.get('preregistration_met')}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[taskF_analysis] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
