#!/usr/bin/env python3
"""Владелец, 2026-09-19: таблица горизонтов v2 -- база = ЦЕНА ИСПОЛНЕНИЯ
ЛИДЕРА (entry_usdc из selected_300.json, реальная цена его сделки, уже
посчитана в основном конвейере, ничего не досчитываем), а не котировка
+5с.

Причина смены базы -- Task C этой же сессии
(solana_dbot_entry_price_calibration.py): на реальных исполненных копиях
(пилот+BATCH-1, n=4) наша фактическая цена входа гораздо БЛИЖЕ к цене
лидера (медиана +26.1%), чем к котировке +5с (медиана +52.7% от цены
лидера) -- т.е. база "+5с" ЗАНИЖАЕТ реальную доходность практически
вдвое. Старая колонка "+5с" оставлена в этой же таблице для
преемственности, помечена deprecated/superseded.

Модельная оценка НАШЕЙ ожидаемой доходности на горизонте:
  model_estimate = (1 + growth_from_leader_price) / (1 + markup) - 1
где growth_from_leader_price -- медиана роста (эта таблица, база=лидер),
markup -- медиана наценки при входе (из data/solana_dbot_realized_ledger.json,
Task 2, РЕАЛЬНЫЕ исполненные копии, n=3 -- МАЛАЯ ВЫБОРКА, чувствительность
показана отдельно на p25/p75 наценки).

Валидация модели: для реальных закрытых сделок (та же ведомость) --
сравниваем model_estimate (на ближайшем горизонте к их hold_seconds) с
их РЕАЛЬНЫМ net_pct. Расхождение -- прямой ответ на вопрос "можно ли
верить этой таблице"."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, median

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = REPO_ROOT / "data" / "solana_buyer_200"
OUT_PATH = OUT_ROOT / "horizon_summary_v2.json"
LEDGER_PATH = REPO_ROOT / "data" / "solana_dbot_realized_ledger.json"

SHORT_HORIZONS = [5, 15, 30, 60, 180, 300]
LONG_HORIZONS = [900, 3600, 21600, 86400]
ALL_HORIZONS = SHORT_HORIZONS + LONG_HORIZONS
LABELS = {5: "5с", 15: "15с", 30: "30с", 60: "60с", 180: "180с", 300: "300с",
          900: "900с", 3600: "1ч", 21600: "6ч", 86400: "24ч"}


def load_selected() -> dict[str, dict]:
    rows = json.loads((OUT_ROOT / "selected_300.json").read_text())
    return {r["signature"]: r for r in rows}


def load_short() -> dict[tuple[str, int], dict]:
    p = OUT_ROOT / "step_extended_result.json"
    if not p.exists():
        return {}
    rows = json.loads(p.read_text())
    return {(r["signature"], r["seconds"]): r for r in rows if "seconds" in r}


def load_long() -> dict[tuple[str, int], dict]:
    p = OUT_ROOT / "long_horizons_result.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    out = {}
    for sig, by_sec in raw.items():
        for sec_str, v in by_sec.items():
            out[(sig, int(sec_str))] = v
    return out


def baseline_leader(sig: str, selected: dict) -> float | None:
    """НОВАЯ ПЕРВИЧНАЯ база -- entry_usdc (цена исполнения СДЕЛКИ ЛИДЕРА,
    usdc_spent/tokens_received), уже посчитана в selected_300.json."""
    row = selected.get(sig)
    if not row or row.get("entry_usdc") is None:
        return None
    try:
        return float(row["entry_usdc"])
    except (TypeError, ValueError):
        return None


def baseline_price5_deprecated(sig: str, short: dict) -> float | None:
    """УСТАРЕВШАЯ база -- котировка +5с. Оставлена только для
    преемственности (см. docstring solana_buyer200_horizon_summary.py) --
    НЕ использовать для новых выводов, занижает реальную доходность
    (см. docstring этого модуля)."""
    e = short.get((sig, 5))
    if not e or e.get("mine_status") != "ok" or e.get("mine_price") is None:
        return None
    return float(e["mine_price"])


def price_at(sig: str, sec: int, short: dict, long_: dict) -> float | None:
    if sec in SHORT_HORIZONS:
        e = short.get((sig, sec))
        if not e or e.get("mine_status") != "ok" or e.get("mine_price") is None:
            return None
        return float(e["mine_price"])
    e = long_.get((sig, sec))
    if not e or e.get("status") != "ok" or e.get("price_usd") is None:
        return None
    return float(e["price_usd"])


def trimmed_mean(vals: list[float], trim_frac: float = 0.05) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    k = int(n * trim_frac)
    core = s[k: n - k] if n - 2 * k > 0 else s
    return sum(core) / len(core)


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def load_markup_stats() -> dict:
    if not LEDGER_PATH.exists():
        return {"n": 0, "median_pct": None, "p25_pct": None, "p75_pct": None, "values": []}
    ledger = json.loads(LEDGER_PATH.read_text())
    vals = [t["markup_at_entry_pct"] for t in ledger.get("trades", []) if "markup_at_entry_pct" in t]
    if not vals:
        return {"n": 0, "median_pct": None, "p25_pct": None, "p75_pct": None, "values": []}
    return {"n": len(vals), "median_pct": median(vals), "p25_pct": pct(vals, 0.25), "p75_pct": pct(vals, 0.75),
            "values": vals}


def nearest_horizon(hold_seconds: int) -> int:
    return min(SHORT_HORIZONS, key=lambda h: abs(h - hold_seconds))


def main() -> None:
    selected = load_selected()
    short = load_short()
    long_ = load_long()

    leader_bases = {sig: baseline_leader(sig, selected) for sig in selected}
    old_bases = {sig: baseline_price5_deprecated(sig, short) for sig in selected}
    n_with_leader_base = sum(1 for v in leader_bases.values() if v is not None)
    n_with_old_base = sum(1 for v in old_bases.values() if v is not None)

    markup_stats = load_markup_stats()

    out = {"n_purchases_total": len(selected),
           "n_with_valid_leader_baseline": n_with_leader_base,
           "n_with_valid_5s_baseline_deprecated": n_with_old_base,
           "markup_stats_from_realized_ledger": markup_stats,
           "note_baseline_change": (
               "База сменена с котировки '+5с' на entry_usdc (реальная цена исполнения сделки лидера) -- "
               "см. Task C калибровку (solana_dbot_entry_price_calibration.py): наша реальная копия ближе "
               "к цене лидера (медиана +26.1%), чем к +5с (медиана +52.7%) -- старая база занижала доходность. "
               "Колонки growth_from_5s_DEPRECATED оставлены для преемственности."
           ),
           "note_markup_small_sample": (
               f"Наценка для модельной оценки взята из data/solana_dbot_realized_ledger.json: n={markup_stats['n']} "
               "РЕАЛЬНЫХ исполненных копий (не смоделировано) -- ЭТО МАЛАЯ ВЫБОРКА, оценка p25/p75 показана "
               "отдельно как чувствительность, не как точный интервал."
           ),
           "table": []}
    import time
    out["generated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    print(f"markup (наценка при входе, n={markup_stats['n']}): "
          f"медиана={markup_stats['median_pct']}, p25={markup_stats['p25_pct']}, p75={markup_stats['p75_pct']}")
    print(f"{'горизонт':>8} {'группа':>12} {'n':>5} {'медиана%лидер':>14} {'усеч.сред%лидер':>16} "
          f"{'доля<0':>7} {'медиана%+5с(deprecated)':>24} {'модель_est%(медиана наценки)':>28}")
    print("-" * 130)

    for sec in ALL_HORIZONS:
        for group_name, want_zero in (("первый вход", True), ("докупка", False)):
            moves_leader, moves_old = [], []
            for sig, row in selected.items():
                if bool(row.get("zero_balance")) != want_zero:
                    continue
                p_t = price_at(sig, sec, short, long_)
                if p_t is None:
                    continue
                lb = leader_bases.get(sig)
                if lb is not None:
                    moves_leader.append(p_t / lb - 1)
                ob = old_bases.get(sig)
                if ob is not None:
                    moves_old.append(p_t / ob - 1)

            n = len(moves_leader)
            median_leader = median(moves_leader) if moves_leader else None
            row_out = {
                "horizon_seconds": sec, "horizon_label": LABELS[sec], "group": group_name,
                "n_leader_base": n,
                "median_growth_from_leader_price_pct": round(median_leader * 100, 3) if median_leader is not None else None,
                "mean_growth_from_leader_price_pct": round(mean(moves_leader) * 100, 3) if moves_leader else None,
                "trimmed_mean_growth_from_leader_price_pct": round(trimmed_mean(moves_leader) * 100, 3) if moves_leader else None,
                "share_negative_leader_base": round(sum(1 for m in moves_leader if m < 0) / n, 3) if n else None,
                "p25_growth_from_leader_price_pct": round(pct(moves_leader, 0.25) * 100, 3) if moves_leader else None,
                "p75_growth_from_leader_price_pct": round(pct(moves_leader, 0.75) * 100, 3) if moves_leader else None,
                "n_5s_base_DEPRECATED": len(moves_old),
                "median_growth_from_5s_DEPRECATED_pct": round(median(moves_old) * 100, 3) if moves_old else None,
            }

            if group_name == "первый вход" and median_leader is not None and markup_stats["median_pct"] is not None:
                def model_est(markup_pct):
                    return (1 + median_leader) / (1 + markup_pct / 100) - 1
                row_out["model_our_estimated_return_pct_at_median_markup"] = round(model_est(markup_stats["median_pct"]) * 100, 3)
                row_out["model_our_estimated_return_pct_at_p25_markup"] = round(model_est(markup_stats["p25_pct"]) * 100, 3) if markup_stats["p25_pct"] is not None else None
                row_out["model_our_estimated_return_pct_at_p75_markup"] = round(model_est(markup_stats["p75_pct"]) * 100, 3) if markup_stats["p75_pct"] is not None else None

            out["table"].append(row_out)
            model_str = f"{row_out.get('model_our_estimated_return_pct_at_median_markup', '')}" if group_name == "первый вход" else ""
            print(f"{LABELS[sec]:>8} {group_name:>12} {n:>5} "
                  f"{('%.2f' % row_out['median_growth_from_leader_price_pct']) if row_out['median_growth_from_leader_price_pct'] is not None else '--':>14} "
                  f"{('%.2f' % row_out['trimmed_mean_growth_from_leader_price_pct']) if row_out['trimmed_mean_growth_from_leader_price_pct'] is not None else '--':>16} "
                  f"{('%.2f' % (row_out['share_negative_leader_base']*100)) if row_out['share_negative_leader_base'] is not None else '--':>6}% "
                  f"{('%.2f' % row_out['median_growth_from_5s_DEPRECATED_pct']) if row_out['median_growth_from_5s_DEPRECATED_pct'] is not None else '--':>23} "
                  f"{model_str:>28}")

    # --- Валидация модели против РЕАЛЬНЫХ закрытых сделок ---
    validation = []
    if LEDGER_PATH.exists():
        ledger = json.loads(LEDGER_PATH.read_text())
        table_by_key = {(r["horizon_seconds"], r["group"]): r for r in out["table"]}
        for t in ledger.get("trades", []):
            if t.get("status") != "ok" or "hold_seconds" not in t or "net_pct" not in t:
                continue
            h = nearest_horizon(t["hold_seconds"])
            model_row = table_by_key.get((h, "первый вход"))
            model_est = model_row.get("model_our_estimated_return_pct_at_median_markup") if model_row else None
            if model_est is None:
                continue
            validation.append({
                "label": t.get("label"), "mint": t.get("mint"), "hold_seconds": t["hold_seconds"],
                "matched_horizon": h, "model_estimate_pct": model_est, "real_net_pct": round(t["net_pct"], 3),
                "discrepancy_pct_points": round(t["net_pct"] - model_est, 3),
            })
    out["model_validation_vs_real_trades"] = validation
    if validation:
        discs = [abs(v["discrepancy_pct_points"]) for v in validation]
        out["model_validation_summary"] = {
            "n": len(validation), "median_abs_discrepancy_pct_points": round(median(discs), 3),
            "note": "Расхождение (реальный net_pct - модельная оценка) на КАЖДОЙ реальной сделке -- "
                    "главный индикатор, можно ли доверять этой таблице как прогнозу. Малое n -- не "
                    "статистическая гарантия, только прямая проверка на всех доступных реальных сделках."
        }
    else:
        out["model_validation_summary"] = {"n": 0, "note": "нет пересечения реальных закрытых сделок с рассчитанными горизонтами -- не проверено"}

    out["long_horizons_reliability_warning"] = (
        "900с/1ч/6ч/24ч: среднее/усечённое среднее для части сделок аномально огромное (сотни тысяч %) -- "
        "тот же диагностированный в Task D эффект (solana_buyer200_horizon_anomaly_diag.py): реальные "
        "экстремальные (400-1300x) движения на низколиквидных пулах, подтверждено реальной текущей "
        "ликвидностью GeckoTerminal ($27k-57k) -- не деление на почти ноль, не артефакт расчёта. МЕДИАНА "
        "устойчива к этому и её можно использовать; СРЕДНЕМУ/усечённому среднему на 900с-24ч не доверять."
    )
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\n[horizon_v2] валидация модели: {out['model_validation_summary']}")
    print(f"[horizon_v2] записано в {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
