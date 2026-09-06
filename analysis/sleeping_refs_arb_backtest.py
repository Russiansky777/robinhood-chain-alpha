#!/usr/bin/env python3
"""«Спящие референсы» → расхождение площадок, п.4-5 владельца
(2026-09-06, дословно):

4. "Экономика. Ожидаемая прибыль на сделку при пороге входа = сумма
   round-trip x 1.5 и 2.0, на $5000 и на $20000 (глубину стакана обеих
   взять текущую). Число сделок в месяц, суммарный P&L, доля убыточных."
5. "Фандинг. Позиция держится часами-днями, значит фандинг на обеих
   ногах входит в P&L. У нас есть история обеих площадок -- учесть."

Предрегистрация (дословно): "линия жива при >=20 сделок в месяц с
ожидаемой прибылью выше издержек и суммарной доходностью >=30% годовых
на задействованный капитал с учётом фандинга и обеих марж."

ЧЕСТНОЕ ОГРАНИЧЕНИЕ МЕТОДА (не гадаем, не скрываем): round-trip издержки
-- ТЕКУЩИЙ (сейчас) снимок глубины стакана (владелец сам сказал "текущую"),
применяется РЕТРОАКТИВНО ко всему периоду 05.07-текущий момент. Если
ликвидность раньше была тоньше/глубже, реальный исторический P&L был бы
другим -- это раскрытое ограничение метода, не придуманные данные.

Метод входа/выхода (владелец не задавал алгоритм, решение здесь явное):
вход -- "восходящий фронт" |D| выше входного порога (round-trip x
множитель), НЕ было открытой позиции по этому тикеру. Выход -- ПЕРВОЕ
из: |D| <= round-trip (издержка полностью съедена, закрываем) ИЛИ смена
знака D (полный разворот/перехлёст) ИЛИ 336ч (14 дней) -- принудительное
закрытие, помечено отдельно "forced_exit", не участвует в P&L как
"успешная" сделка молча.

Капитал/доходность: капитал по тикеру = 2×notional (обе ноги, БЕЗ
плеча -- консервативно, явно), капитал перевыпускается между сделками
ПОЛНОСТЬЮ (последовательные сделки по одному тикеру, не параллельные) --
годовая доходность по тикеру = (сумма net P&L% сделок этого тикера) x
(365/дней_периода). Портфель = равновзвешенное среднее по 23 тикерам
(равный капитал на тикер) -- НЕ предполагаем леверидж/маржирование
сверх этого явного допущения."""
from __future__ import annotations

import json
import time
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

RAW_D_PATH = Path("data/sleeping_refs_cache/hourly_divergence_full.csv")
FUNDING_PATH = Path("data/sleeping_refs_cache/sleeping_refs_funding_history_full.csv")
DEPTH_20K_PATH = Path("data/p3_guard_cache/sleeping_refs_orderbook_depth_20k_result.json")
OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_arb_backtest_result.json")

NOTIONALS = (5000.0, 20000.0)
MULTIPLIERS = (1.5, 2.0)
MAX_HOLDING_HOURS = 336.0  # 14 дней -- см. докстринг
ANNUALIZATION_DAYS = 365.0


def load_round_trip_sums() -> dict:
    depth = json.loads(DEPTH_20K_PATH.read_text())["assets"]
    out: dict = {}
    for sym, venues in depth.items():
        out[sym] = {}
        for notional in NOTIONALS:
            key = f"round_trip_cost_pct_{int(notional)}"
            l_rt = venues.get("lighter", {}).get(key)
            x_rt = venues.get("xyz", {}).get(key)
            out[sym][notional] = (l_rt + x_rt) if (l_rt is not None and x_rt is not None) else None
    return out


def simulate_ticker(g: pd.DataFrame, funding: pd.DataFrame, round_trip_sum: float, multiplier: float) -> list[dict]:
    """g -- часовой D-ряд ОДНОГО тикера, отсортирован по времени.
    funding -- часовые ставки фандинга ОДНОГО тикера (может быть пустым)."""
    if round_trip_sum is None:
        return []
    g = g.reset_index(drop=True)
    entry_threshold = round_trip_sum * multiplier
    above = g["abs_D_pct"].to_numpy() > entry_threshold
    D = g["D"].to_numpy()
    absD = g["abs_D_pct"].to_numpy()
    t_ms = g["t"].to_numpy()
    p_l = g["price_lighter"].to_numpy()
    p_x = g["price_xyz"].to_numpy()

    fmap_l = dict(zip(funding["hour_ms"], funding["lighter_rate_pct"])) if len(funding) else {}
    fmap_x = dict(zip(funding["hour_ms"], funding["xyz_rate_pct"])) if len(funding) else {}

    trades = []
    i = 1
    n = len(g)
    while i < n:
        if above[i] and not above[i - 1]:
            entry_sign = np.sign(D[i])
            p_l0, p_x0, t0 = p_l[i], p_x[i], t_ms[i]
            exit_j = None
            exit_reason = None
            j = i + 1
            while j < n:
                hours_held = (t_ms[j] - t0) / 3_600_000.0
                if absD[j] <= round_trip_sum:
                    exit_j, exit_reason = j, "cost_fully_eaten"
                    break
                if np.sign(D[j]) != entry_sign:
                    exit_j, exit_reason = j, "sign_flip"
                    break
                if hours_held >= MAX_HOLDING_HOURS:
                    exit_j, exit_reason = j, "forced_exit_14d"
                    break
                j += 1
            if exit_j is None:
                i += 1
                continue  # ни одно условие выхода не наступило до конца данных -- честно НЕ считаем незакрытой сделкой

            p_l1, p_x1, t1 = p_l[exit_j], p_x[exit_j], t_ms[exit_j]
            hours_held = (t1 - t0) / 3_600_000.0
            if entry_sign > 0:  # Lighter дороже -> шорт Lighter, лонг xyz
                gross_pnl_pct = (p_x1 / p_x0 - 1) - (p_l1 / p_l0 - 1)
            else:  # Lighter дешевле -> лонг Lighter, шорт xyz
                gross_pnl_pct = (p_l1 / p_l0 - 1) - (p_x1 / p_x0 - 1)
            gross_pnl_pct *= 100  # доля -> %

            # Фандинг за период удержания (реальная история, часовые ставки; отсутствующие часы -- 0, честно посчитано отдельно ниже)
            hours_in_range = [h for h in range(int(t0), int(t1), 3_600_000)]
            funding_pnl_pct = 0.0
            n_funding_hours_found = 0
            for h in hours_in_range:
                fl = fmap_l.get(h)
                fx = fmap_x.get(h)
                if fl is not None:
                    funding_pnl_pct += (fl if entry_sign > 0 else -fl)  # шорт lighter получает +fl, лонг lighter платит -fl
                    n_funding_hours_found += 1
                if fx is not None:
                    funding_pnl_pct += (-fx if entry_sign > 0 else fx)  # лонг xyz платит -fx, шорт xyz получает +fx
                    n_funding_hours_found += 1

            net_pnl_pct = gross_pnl_pct - round_trip_sum + funding_pnl_pct
            trades.append({
                "entry_t": int(t0), "exit_t": int(t1), "hours_held": hours_held, "exit_reason": exit_reason,
                "entry_sign": int(entry_sign), "gross_pnl_pct": gross_pnl_pct, "cost_pct": round_trip_sum,
                "funding_pnl_pct": funding_pnl_pct, "net_pnl_pct": net_pnl_pct,
                "n_funding_hours_found": n_funding_hours_found,
            })
            i = exit_j + 1  # не открываем новую позицию, пока не закрыта текущая
        else:
            i += 1
    return trades


def run() -> int:
    if not RAW_D_PATH.exists():
        raise SystemExit(f"[backtest] нет {RAW_D_PATH}")
    d_df = pd.read_csv(RAW_D_PATH)
    round_trip_sums = load_round_trip_sums()

    funding_df = pd.DataFrame()
    if FUNDING_PATH.exists():
        funding_df = pd.read_csv(FUNDING_PATH)
        funding_df["hour_ms"] = pd.to_datetime(funding_df["hour"], utc=True).astype("int64") // 1_000_000
    else:
        print("[backtest] !!! нет истории фандинга -- считаем funding_pnl_pct=0 везде, честно помечено")

    days_span = (d_df["t"].max() - d_df["t"].min()) / 86_400_000.0
    print(f"[backtest] реальный диапазон бэктеста: {days_span:.1f} дней")

    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "days_span": days_span, "has_funding_history": len(funding_df) > 0, "scenarios": {}}

    for notional, multiplier in product(NOTIONALS, MULTIPLIERS):
        scenario_key = f"notional_{int(notional)}_mult_{multiplier}"
        print(f"\n=== Сценарий: notional=${notional:.0f}, порог=x{multiplier} ===")
        per_ticker_results: dict = {}
        all_trades_pooled: list[dict] = []
        for sym, g in d_df.groupby("symbol"):
            rt_sum = round_trip_sums.get(sym, {}).get(notional)
            fund_g = funding_df[funding_df["symbol"] == sym] if len(funding_df) else pd.DataFrame()
            trades = simulate_ticker(g.sort_values("t"), fund_g, rt_sum, multiplier)
            if not trades:
                per_ticker_results[sym] = {"n_trades": 0, "round_trip_sum_pct": rt_sum}
                continue
            tdf = pd.DataFrame(trades)
            annualized_return_pct = float(tdf["net_pnl_pct"].sum()) * (ANNUALIZATION_DAYS / days_span)
            per_ticker_results[sym] = {
                "n_trades": len(tdf), "round_trip_sum_pct": rt_sum,
                "mean_net_pnl_pct": float(tdf["net_pnl_pct"].mean()),
                "median_hours_held": float(tdf["hours_held"].median()),
                "frac_losing": float((tdf["net_pnl_pct"] < 0).mean()),
                "sum_net_pnl_pct": float(tdf["net_pnl_pct"].sum()),
                "annualized_return_pct_on_capital": annualized_return_pct,
                "n_forced_exit_14d": int((tdf["exit_reason"] == "forced_exit_14d").sum()),
            }
            all_trades_pooled.extend(trades)

        pooled = pd.DataFrame(all_trades_pooled)
        n_tickers_with_trades = sum(1 for v in per_ticker_results.values() if v["n_trades"] > 0)
        scenario: dict = {
            "n_trades_total": len(pooled), "n_tickers_with_trades": n_tickers_with_trades,
            "trades_per_month": len(pooled) / (days_span / 30.0) if len(pooled) else 0.0,
        }
        if len(pooled):
            scenario.update({
                "mean_net_pnl_pct_per_trade": float(pooled["net_pnl_pct"].mean()),
                "median_net_pnl_pct_per_trade": float(pooled["net_pnl_pct"].median()),
                "frac_losing_trades": float((pooled["net_pnl_pct"] < 0).mean()),
                "median_hours_held": float(pooled["hours_held"].median()),
                "p90_hours_held": float(pooled["hours_held"].quantile(0.90)),
                "n_forced_exit_14d": int((pooled["exit_reason"] == "forced_exit_14d").sum()),
                "mean_funding_pnl_pct_per_trade": float(pooled["funding_pnl_pct"].mean()),
                "mean_gross_pnl_pct_per_trade": float(pooled["gross_pnl_pct"].mean()),
                # Портфель: равновзвешенное среднее годовой доходности по тикерам, где были сделки (равный капитал на тикер)
                "portfolio_annualized_return_pct": float(np.mean([
                    v["annualized_return_pct_on_capital"] for v in per_ticker_results.values() if v["n_trades"] > 0
                ])) if n_tickers_with_trades else None,
            })
            alive = (scenario["trades_per_month"] >= 20 and scenario["mean_net_pnl_pct_per_trade"] > 0
                     and scenario.get("portfolio_annualized_return_pct") is not None
                     and scenario["portfolio_annualized_return_pct"] >= 30.0)
            scenario["preregistered_verdict"] = (
                f"{'ЖИВА' if alive else 'НЕ подтверждено по предрегистрации'}: "
                f"сделок/мес={scenario['trades_per_month']:.1f}(>=20?), "
                f"ожид.прибыль/сделку={scenario['mean_net_pnl_pct_per_trade']:.4f}%(>0?), "
                f"доходность/год={scenario.get('portfolio_annualized_return_pct')}(>=30%?)"
            )
            print(f"  сделок всего={scenario['n_trades_total']}, сделок/мес={scenario['trades_per_month']:.1f}, "
                  f"среднее net P&L/сделку={scenario['mean_net_pnl_pct_per_trade']:.4f}%, "
                  f"доля убыточных={scenario['frac_losing_trades']:.1%}, "
                  f"доходность/год (портфель)={scenario.get('portfolio_annualized_return_pct')}")
            print(f"  {scenario['preregistered_verdict']}")
        else:
            scenario["note"] = "0 реальных сделок в этом сценарии"
            print("  0 реальных сделок")

        scenario["per_ticker"] = per_ticker_results
        result["scenarios"][scenario_key] = scenario

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[backtest] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
