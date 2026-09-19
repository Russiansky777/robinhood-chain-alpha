#!/usr/bin/env python3
"""Владелец, 2026-09-18: сводная таблица брутто-движения по горизонтам
5/15/30/60/180/300/900с/1ч/6ч/24ч, отдельно первые входы (zero_balance)
и докупки, без вычета издержек (наложим отдельно). Плюс медиана
максимальной просадки внутри каждого горизонта.

База для "движения" -- КОТИРОВКА +5с (mine_price при seconds=5), НЕ
фактическая цена исполнения покупки (entry_usdc). Изначально здесь было
наоборот (база = entry_usdc) с аргументом "иначе горизонт 5с
тривиальный ноль" -- ОШИБКА, найдена и исправлена эмпирически: по 37
первым входам с валидной точкой на 30с медиана движения (+5с -> +30с)
= 20.70% ТОЧНО совпадает с уже известным владельцу опорным числом
"медиана +20.70% на n=37" (см. переписку) -- а медиана от entry_usdc
до +30с даёт 104.66%, НЕ совпадает. Значит установленная в этом
конвейере (docstring solana_buyer200_fast_price.py: "движение считается
от котировки +5с") конвенция -- это база и для ЭТОЙ таблицы, не только
для сверки Шага 1/2. Горизонт 5с оттого тривиально нулевой для всех
сделок -- это ОЖИДАЕМО, не баг, оставлен в таблице с пояснением.

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


def baseline_price5(sig: str, short: dict) -> float | None:
    """Установленная в конвейере база -- котировка +5с (см. правку в
    docstring модуля: эмпирически подтверждена, воспроизводит опорное
    +20.70% на n=37)."""
    e = short.get((sig, 5))
    if not e or e.get("mine_status") != "ok" or e.get("mine_price") is None:
        return None
    return float(e["mine_price"])


def gross_move_at(sig: str, sec: int, base: float, short: dict, long_: dict) -> float | None:
    if sec in SHORT_HORIZONS:
        e = short.get((sig, sec))
        if not e or e.get("mine_status") != "ok" or e.get("mine_price") is None:
            return None
        return float(e["mine_price"]) / base - 1
    e = long_.get((sig, sec))
    if not e or e.get("status") != "ok" or e.get("price_usd") is None:
        return None
    return float(e["price_usd"]) / base - 1


def drawdown_at(sig: str, sec: int, base: float, short: dict, long_: dict) -> tuple[float | None, str]:
    if sec in SHORT_HORIZONS:
        pts = [gross_move_at(sig, s, base, short, long_) for s in SHORT_HORIZONS if s <= sec]
        pts = [p for p in pts if p is not None]
        if not pts:
            return None, "discrete_points_proxy"
        return min(min(pts), 0.0), "discrete_points_proxy"
    e = long_.get((sig, sec))
    if not e or e.get("min_low_usd_from_entry") is None:
        return None, "continuous_candle_low"
    return float(e["min_low_usd_from_entry"]) / base - 1, "continuous_candle_low"


def trimmed_mean(vals: list[float], trim_frac: float = 0.05) -> float | None:
    """Среднее с отсечением trim_frac с КАЖДОЙ стороны -- владелец,
    2026-09-19: рядом со средним, чтобы медиана и усечённое среднее
    вместе честно показывали, не тащат ли результат единичные иксы
    (аномальные средние на длинных горизонтах, см.
    long_horizons_reliability_warning)."""
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


def main() -> None:
    selected = load_selected()
    short = load_short()
    long_ = load_long()

    n_long_done = len({sig for (sig, _s) in long_.keys()})
    n_short_done = len({sig for (sig, _s), e in short.items() if e.get("mine_status") == "ok"})
    print(f"[horizon_summary] покупок всего={len(selected)}, "
          f"с готовыми короткими горизонтами (>=1 ok)={n_short_done}, "
          f"с готовыми длинными горизонтами={n_long_done}", flush=True)

    bases = {sig: baseline_price5(sig, short) for sig in selected}
    n_with_base = sum(1 for v in bases.values() if v is not None)

    out = {"generated_at_utc": None, "n_purchases_total": len(selected),
           "n_short_horizons_available_for": n_short_done, "n_long_horizons_available_for": n_long_done,
           "n_with_valid_5s_baseline": n_with_base,
           "note_baseline": "движение считается ОТ КОТИРОВКИ +5с (mine_price при seconds=5) -- "
                             "установленная в конвейере конвенция, эмпирически подтверждена: "
                             "воспроизводит опорное +20.70% на n=37 (см. docstring). Сделки без "
                             "валидной точки на +5с честно исключены (base=None), не обнулены.",
           "table": []}
    import time
    out["generated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    print(f"{'горизонт':>8} {'группа':>12} {'n':>5} {'медиана%':>10} {'среднее%':>10} {'усеч.среднее%':>13} "
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
                base = bases.get(sig)
                if base is None:
                    continue
                mv = gross_move_at(sig, sec, base, short, long_)
                if mv is not None:
                    moves.append(mv)
                dd, dd_method = drawdown_at(sig, sec, base, short, long_)
                if dd is not None:
                    drawdowns.append(dd)
            n = len(moves)
            row_out = {
                "horizon_seconds": sec, "horizon_label": LABELS[sec], "group": group_name,
                "n": n,
                "median_pct": round(median(moves) * 100, 3) if moves else None,
                "mean_pct": round(mean(moves) * 100, 3) if moves else None,
                "trimmed_mean_pct_5pct_each_side": round(trimmed_mean(moves) * 100, 3) if moves else None,
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
                  f"{('%.2f' % row_out['trimmed_mean_pct_5pct_each_side']) if row_out['trimmed_mean_pct_5pct_each_side'] is not None else '--':>12} "
                  f"{('%.2f' % (row_out['share_negative']*100)) if row_out['share_negative'] is not None else '--':>7}% "
                  f"{('%.2f' % row_out['median_max_drawdown_pct']) if row_out['median_max_drawdown_pct'] is not None else '--':>13} "
                  f"{dd_method:>22}")

    out["long_horizons_reliability_warning"] = (
        "900с/1ч/6ч/24ч: среднее (mean_pct) для части сделок аномально огромное "
        "(до сотен тысяч %) -- найдено при первом прогоне на реальных данных. "
        "Проверено на худшем случае (4JfcMioEjwsfR8Q..., пул DGUzdrnp...): и "
        "ончейн-нога маршрута, и GeckoTerminal используют РОВНО ТОТ ЖЕ адрес "
        "пула, маршрут честно завершается в USDC (не юнит-разъезд SOL/USD) -- "
        "но price_usd от GeckoTerminal на этом пуле даёт множитель ~218000x к "
        "цене +5с. Причина НЕ установлена (похоже на расхождение в декодировании "
        "конкретно pump.fun-подобных пулов между нашим ончейн-методом и "
        "GeckoTerminal, не проверено до конца). МЕДИАНА по длинным горизонтам "
        "выглядит разумно (10-60%), но доверять СРЕДНЕМУ и, возможно, части "
        "медианы для 900с-24ч пока нельзя. Короткие горизонты (5-300с) этой "
        "проблемы не имеют -- полностью ончейн, один метод, воспроизводят "
        "опорное +20.70% на n=37 точно."
    )
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\n[horizon_summary] записано в {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
