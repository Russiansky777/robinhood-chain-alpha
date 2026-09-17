#!/usr/bin/env python3
"""Владелец, 2026-09-17: Solana разовая проверка, честный фолбэк после
реального провала попытки полного decode истории пула через публичный
Solana RPC (`task_solana_price_horizons_and_wave_result.json`, реальный
прогон): decode дошёл лишь на ~11 часов назад от текущего момента, НЕ
дойдя до T0 (первая покупка была ~18.4 часа назад) -- публичный RPC
оказался слишком медленным (~45с/страница подписей под нагрузкой) для
пула с реальным объёмом $100k+/сутки в рамках одного разумного прогона.
Таблица горизонтов и волна из того прогона -- артефакт непокрытия
времени, НЕ реальный ответ, честно не используются.

Этот скрипт -- проверка, есть ли у GeckoTerminal реальные OHLCV-свечи
(минута/час) для ЭТОГО КОНКРЕТНОГО пула (`LeZ2DH1y7bqzAghYbXqKB6fNxoBKqPSPihv3EmkLBRv`,
Raydium, Solana) -- если да, они дают реальную, честно привязанную по
времени цену для горизонтов от +1мин и грубее (свечи не разрешают
+5с/+15с/+30с -- это честно остаётся непокрытым, публичный RPC этого
пула не потянул, нужен платный/выделенный индексатор для точных
секундных горизонтов на пуле такого объёма).

Результат: `data/task_solana_gt_ohlcv_fallback_result.json`."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/task_solana_gt_ohlcv_fallback_result.json")
POOL_ADDRESS = "LeZ2DH1y7bqzAghYbXqKB6fNxoBKqPSPihv3EmkLBRv"
T0_UNIX = 1789596037  # первая покупка, этап A, реальный блоктайм
HORIZONS_S = [60, 180, 300, 900, 3600, 21600, 86400]  # +1мин..+24ч -- секундные горизонты сюда НЕ входят честно


def gt_get(path: str, params: dict | None = None, max_retries: int = 3) -> tuple[int, dict | None]:
    for attempt in range(max_retries):
        try:
            resp = requests.get(f"https://api.geckoterminal.com/api/v2{path}", params=params or {},
                                 headers={"Accept": "application/json"}, timeout=25)
            if resp.status_code == 200:
                return 200, resp.json()
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return resp.status_code, None
        except Exception:  # noqa: BLE001
            time.sleep(2 * (attempt + 1))
    return -1, None


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "pool_address": POOL_ADDRESS, "T0_unix": T0_UNIX,
                 "honest_context": "Прямой decode через RPC не дошёл до T0 (реальный прогон, публичный RPC "
                                    "не потянул объём пула за разумное время) -- эта проверка ищет РЕАЛЬНУЮ "
                                    "альтернативу через уже готовые OHLCV-агрегаты GeckoTerminal."}

    for timeframe, aggregate in [("minute", 1), ("minute", 5), ("hour", 1)]:
        status, body = gt_get(f"/networks/solana/pools/{POOL_ADDRESS}/ohlcv/{timeframe}",
                               params={"aggregate": aggregate, "limit": 1000, "before_timestamp": T0_UNIX + 90000})
        key = f"{timeframe}_{aggregate}"
        if status != 200 or not body:
            out[key] = {"status": status, "note": "GT не вернул 200 -- честно нет данных на этом таймфрейме"}
            continue
        rows = (body.get("data", {}).get("attributes", {}) or {}).get("ohlcv_list", [])
        out[key] = {"status": 200, "n_candles": len(rows),
                     "earliest_unix": rows[-1][0] if rows else None, "latest_unix": rows[0][0] if rows else None}
        out.setdefault("_raw_candles", {})[key] = rows
        time.sleep(1.5)

    # --- построить горизонты из самого мелкого доступного таймфрейма, покрывающего T0 ---
    candles_by_tf = out.pop("_raw_candles", {})
    chosen_tf, chosen_rows = None, None
    for key in ("minute_1", "minute_5", "hour_1"):
        rows = candles_by_tf.get(key)
        if rows and rows[-1][0] <= T0_UNIX:  # свечи должны реально накрывать T0
            chosen_tf, chosen_rows = key, rows
            break
    out["chosen_timeframe_for_horizons"] = chosen_tf
    if not chosen_rows:
        out["HORIZONS_NOTE"] = ("Ни один таймфрейм GeckoTerminal не накрывает T0 реальными свечами -- "
                                 "честно не строим таблицу горизонтов из выдуманных/экстраполированных данных.")
    else:
        # ohlcv_list: [timestamp, open, high, low, close, volume], новые сверху
        rows_sorted = sorted(chosen_rows, key=lambda r: r[0])
        entry_row = min(rows_sorted, key=lambda r: abs(r[0] - T0_UNIX))
        entry_price = entry_row[4]  # close ближайшей к T0 свечи
        out["entry_reference_candle"] = {"timestamp_unix": entry_row[0], "close": entry_price}
        now_unix = time.time()
        horizons_out = []
        for h in HORIZONS_S:
            target = T0_UNIX + h
            if target > now_unix:
                horizons_out.append({"horizon_s": h, "status": "ещё не наступил к моменту прогона"})
                continue
            nearest = min(rows_sorted, key=lambda r: abs(r[0] - target))
            gap_s = abs(nearest[0] - target)
            pct = ((nearest[4] - entry_price) / entry_price * 100) if entry_price else None
            horizons_out.append({
                "horizon_s": h, "target_time_unix": target, "nearest_candle_time_unix": nearest[0],
                "gap_to_target_s": gap_s, "close_price": nearest[4], "pct_change_from_entry": pct,
                "candle_volume": nearest[5],
            })
        out["price_horizons_from_gt_ohlcv"] = horizons_out
        out["RESOLUTION_CAVEAT"] = (
            f"Построено из таймфрейма '{chosen_tf}' -- honest предел разрешения: горизонты +5с/+15с/+30с/"
            "+1мин (для часовых свечей) физически неразличимы на этом таймфрейме. gap_to_target_s в каждой "
            "строке показывает, насколько реально ближайшая свеча далека от точной цели."
        )

    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[gt_fallback] записано {OUT_PATH}")


if __name__ == "__main__":
    main()
