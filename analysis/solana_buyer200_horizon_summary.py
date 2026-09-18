#!/usr/bin/env python3
"""Владелец, 2026-09-18: сводная таблица брутто-движения по горизонтам
5/15/30/60/180/300/900с/1ч/6ч/24ч, отдельно первые входы (zero_balance)
и докупки, без вычета издержек (наложим отдельно). Плюс медиана
максимальной просадки внутри каждого горизонта.

База для "движения" -- РЕАЛЬНАЯ цена исполнения нашей покупки
(entry_usdc = usdc_spent/tokens_received, уже точно посчитана в
selected_300.json) -- ЭТО фактическая цена входа, не котировка +5с
(тот отдельный приём "движение от +5с" в docstring
solana_buyer200_fast_price.py относится к сверке с чужим методом на
Шаге 1/2, не к этой экономической сводке: вопрос "что если держать
дольше" по смыслу сравнивает С МОМЕНТОМ ВХОДА, иначе горизонт 5с был
бы тривиальным нулём для всех сделок).

Источники, оба уже реальные, ничего не досчитывается заново:
- data/solana_buyer_200/step_extended_result.json -- ончейн, 5/15/30/60/
  180/300с (метод: последняя сделка в пуле до момента t).
- data/solana_buyer_200/long_horizons_result.json -- GeckoTerminal,
  900с/1ч/6ч/24ч (см. solana_buyer200_long_horizons.py).

Просадка внутри горизонта -- ДВА РАЗНЫХ метода, честно не смешиваем:
- короткие горизонты (<=300с): только 6 дискретных точек на горизонт --
  просадка = МИНИМУМ движения СРЕДИ ТЕХ ТОЧЕК, что <= данного горизонта.
  Это грубое приближение снизу (могли быть более глубокие проседания
  МЕЖДУ точками, которые мы не увидим) -- честно помечено drawdown_method.
- длинные горизонты (900с/1ч/6ч/24ч): минутные свечи GeckoTerminal,
  минимум low среди свечей от входа до горизонта -- гораздо точнее."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, median

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = REPO_ROOT / "data" / "solana_buyer_200"
OUT_PATH = OUT_ROOT / "horizon_summary.json"

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


def gross_move_at(sig: str, sec: int, entry_usdc: float, short: dict, long_: dict) -> float | None:
    if sec in SHORT_HORIZONS:
        e = short.get((sig, sec))
        if not e or e.get("mine_status") != "ok" or e.get("mine_price") is None:
            return None
        return float(e["mine_price"]) / entry_usdc - 1
    e = long_.get((sig, sec))
    if not e or e.get("status") != "ok" or e.get("price_usd") is None:
        return None
    return float(e["price_usd"]) / entry_usdc - 1


def drawdown_at(sig: str, sec: int, entry_usdc: float, short: dict, long_: dict) -> tuple[float | None, str]:
    if sec in SHORT_HORIZONS:
        pts = [gross_move_at(sig, s, entry_usdc, short, long_) for s in SHORT_HORIZONS if s <= sec]
        pts = [p for p in pts if p is not None]
        if not pts:
            return None, "discrete_points_proxy"
        return min(min(pts), 0.0), "discrete_points_proxy"
    e = long_.get((sig, sec))
    if not e or e.get("min_low_usd_from_entry") is None:
        return None, "continuous_candle_low"
    return float(e["min_low_usd_from_entry"]) / entry_usdc - 1, "continuous_candle_low"


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def main() -> None:
    selected = load_selected()
    short = load_short()
    long_ = load_long()

    n_long_done = len({sig for (sig, _s) in long_.keys()})
    n_short_done = len({sig for (sig, _s), e in short.items() if e.get("mine_status") == "ok"})
    print(f"[horizon_summary] покупок всего={len(selected)}, "
          f"с готовыми короткими горизонтами (>=1 ok)={n_short_done}, "
          f"с готовыми длинными горизонтами={n_long_done}", flush=True)

    out = {"generated_at_utc": None, "n_purchases_total": len(selected),
           "n_short_horizons_available_for": n_short_done, "n_long_horizons_available_for": n_long_done,
           "note_baseline": "движение считается ОТ РЕАЛЬНОЙ цены исполнения покупки (entry_usdc), не от котировки +5с",
           "table": []}
    import time
    out["generated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    print(f"{'горизонт':>8} {'группа':>12} {'n':>5} {'медиана%':>10} {'среднее%':>10} "
          f"{'доля<0':>8} {'мед.просадка%':>14} {'метод просадки':>22}")
    print("-" * 95)

    for sec in ALL_HORIZONS:
        for group_name, want_zero in (("первый вход", True), ("докупка", False)):
            moves = []
            drawdowns = []
            dd_method = None
            for sig, row in selected.items():
                if bool(row.get("zero_balance")) != want_zero:
                    continue
                entry_usdc = float(row["entry_usdc"])
                mv = gross_move_at(sig, sec, entry_usdc, short, long_)
                if mv is not None:
                    moves.append(mv)
                dd, dd_method = drawdown_at(sig, sec, entry_usdc, short, long_)
                if dd is not None:
                    drawdowns.append(dd)
            n = len(moves)
            row_out = {
                "horizon_seconds": sec, "horizon_label": LABELS[sec], "group": group_name,
                "n": n,
                "median_pct": round(median(moves) * 100, 3) if moves else None,
                "mean_pct": round(mean(moves) * 100, 3) if moves else None,
                "share_negative": round(sum(1 for m in moves if m < 0) / n, 3) if n else None,
                "median_max_drawdown_pct": round(median(drawdowns) * 100, 3) if drawdowns else None,
                "drawdown_method": dd_method,
                "p25_pct": round(pct(moves, 0.25) * 100, 3) if moves else None,
                "p75_pct": round(pct(moves, 0.75) * 100, 3) if moves else None,
            }
            out["table"].append(row_out)
            print(f"{LABELS[sec]:>8} {group_name:>12} {n:>5} "
                  f"{('%.2f' % row_out['median_pct']) if row_out['median_pct'] is not None else '--':>10} "
                  f"{('%.2f' % row_out['mean_pct']) if row_out['mean_pct'] is not None else '--':>10} "
                  f"{('%.2f' % (row_out['share_negative']*100)) if row_out['share_negative'] is not None else '--':>7}% "
                  f"{('%.2f' % row_out['median_max_drawdown_pct']) if row_out['median_max_drawdown_pct'] is not None else '--':>13} "
                  f"{dd_method:>22}")

    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\n[horizon_summary] записано в {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
