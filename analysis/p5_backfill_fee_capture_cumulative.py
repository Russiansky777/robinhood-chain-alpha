#!/usr/bin/env python3
"""Одноразовый бэкфилл: дописать поля fee_capture_ratio_cumulative (и
входы для аудита) в ДВЕ уже собранные строки data/p5_fee_accrual.jsonl,
снятые ДО того, как p5_live_position_snapshot.py стал считать это поле
(владелец, 2026-09-04, п.4 задачи: "Пересчитать кумулятив по двум уже
собранным точкам и дописать поле в существующие строки jsonl, чтобы ряд
был однородным").

Источник для our_reserve_usd этих двух исторических точек -- РЕАЛЬНЫЕ
данные позиции (liquidity/ticks/цена), уже закоммиченные в git history
на момент каждого снятия (не переисполнение текущего состояния задним
числом): commit 8576798 (точка 1, 22:38:12Z) и 5fcb8f5 (точка 2,
22:54:10Z). Формула -- ТА ЖЕ raw-sqrt математика, что в самом снимке.

Обе точки сняты <1ч после открытия позиции (opened_at_utc=22:06:36Z) --
часовых OHLCV-свечей с timestamp>=position_open_ts на тот момент физически
ещё не существовало (первая возможная -- граница часа 23:00:00Z). По
явному указанию владельца (п.4): в этом случае pool_fees_usd_cum/
fee_capture_ratio_cumulative пишутся `null`, частичная свеча не
выдумывается. TVL пула по точке 2 РЕАЛЬНО был снят в своё время (сохранён
в data/p3_guard_cache/p5_live_snapshot_1000756_result.json той же
git-ревизии, fee_capture_detail.pool_reserve_usd) -- восстановлен оттуда,
не переснят заново. Для точки 1 TVL пула НЕ был снят в момент прогона
(тогдашний код ещё не сохранял его в это поле при отсутствии предыдущей
точки) -- честно null, не восстановим постфактум без искажения
"среднего по периоду".

Только чтение git history + перезапись data/p5_fee_accrual.jsonl.
Ончейн/Lighter/GT не трогает -- сеть не нужна.
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

ACCRUAL_LOG_PATH = Path("data/p5_fee_accrual.jsonl")
WETH_DECIMALS, USDG_DECIMALS = 18, 6

# (timestamp_utc в jsonl-строке, git-ревизия с реальным состоянием позиции на тот момент)
HISTORICAL_REVISIONS = {
    "2026-09-03T22:38:12Z": "8576798",
    "2026-09-03T22:54:10Z": "5fcb8f5",
}
POSITION_OPENED_AT_UTC = "2026-09-03T22:06:36Z"


def raw_sqrt_from_tick(tick: int) -> float:
    return (1.0001 ** tick) ** 0.5


def our_reserve_usd_from_revision(rev: str) -> tuple[float, float]:
    """Реальная стоимость LP-ноги (our_fees уже есть в самой jsonl-строке
    отдельно) на момент коммита `rev` -- та же raw-sqrt формула, что
    p5_live_position_snapshot.py::run() §4."""
    raw = subprocess.run(["git", "show", f"{rev}:data/p3_guard_cache/p5_live_snapshot_1000756_result.json"],
                          capture_output=True, text=True, check=True).stdout
    d = json.loads(raw)
    pos = d["nfpm_positions_raw"]
    pool_price_now = d["price_vs_range"]["pool_price_now_usd"]
    current_tick = d["price_vs_range"]["current_tick"]
    tick_lower, tick_upper, liquidity = pos["tick_lower"], pos["tick_upper"], pos["liquidity"]

    sqrt_pa_raw = raw_sqrt_from_tick(tick_lower)
    sqrt_pb_raw = raw_sqrt_from_tick(tick_upper)
    if sqrt_pa_raw > sqrt_pb_raw:
        sqrt_pa_raw, sqrt_pb_raw = sqrt_pb_raw, sqrt_pa_raw
    # sqrt_p_raw эквивалентен sqrt(1.0001**current_tick) по определению --
    # тот же приём, что price_from_tick()/raw_sqrt_from_tick() в основном
    # скрипте, не нужен сырой sqrtPriceX96 отдельно.
    sqrt_p_raw = raw_sqrt_from_tick(current_tick)
    sqrt_p_clamped = min(max(sqrt_p_raw, sqrt_pa_raw), sqrt_pb_raw)

    amount0_raw = max(liquidity * (1 / sqrt_p_clamped - 1 / sqrt_pb_raw), 0.0)
    amount1_raw = max(liquidity * (sqrt_p_clamped - sqrt_pa_raw), 0.0)
    amount0_eth = amount0_raw / 10 ** WETH_DECIMALS
    amount1_usdg = amount1_raw / 10 ** USDG_DECIMALS
    our_reserve_usd = amount0_eth * pool_price_now + amount1_usdg

    pool_reserve_now = d.get("fee_capture_detail", {}).get("pool_reserve_usd")  # None, если GT не снимался в тот прогон
    return our_reserve_usd, pool_reserve_now


def hours_covered(ts_utc: str) -> float:
    from datetime import datetime, timezone
    opened = datetime.strptime(POSITION_OPENED_AT_UTC, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    at = datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (at - opened).total_seconds() / 3600


def run() -> int:
    if not ACCRUAL_LOG_PATH.exists():
        print("[backfill] data/p5_fee_accrual.jsonl не найден -- нечего бэкфиллить.")
        return 1

    rows = [json.loads(line) for line in ACCRUAL_LOG_PATH.read_text().splitlines() if line.strip()]

    running_tvl_samples: list[float] = []  # running-window (не look-ahead) -- см. докстринг: только TVL, известный НА МОМЕНТ этой точки
    updated = 0
    for row in rows:
        if "fee_capture_ratio_cumulative" in row:
            # Уже посчитано текущей версией скрипта -- не трогаем (running_tvl
            # всё равно нужно продолжить накапливать для СЛЕДУЮЩИХ старых строк).
            if row.get("pool_reserve_in_usd") is not None:
                running_tvl_samples.append(float(row["pool_reserve_in_usd"]))
            continue

        ts = row["timestamp_utc"]
        rev = HISTORICAL_REVISIONS.get(ts)
        if rev is None:
            print(f"[backfill] ПРЕДУПРЕЖДЕНИЕ: нет известной ревизии для точки {ts} -- пропущена, не выдумываю.")
            continue

        our_reserve_usd, pool_reserve_now = our_reserve_usd_from_revision(rev)
        our_fees_usd_cum = row["fees0_eth"] * row["pool_price_usd"] + row["fees1_usdg"]
        our_yield_cum = (our_fees_usd_cum / our_reserve_usd) if our_reserve_usd else None
        hc = hours_covered(ts)

        if pool_reserve_now is not None:
            running_tvl_samples.append(float(pool_reserve_now))
        avg_pool_tvl_usd = (sum(running_tvl_samples) / len(running_tvl_samples)) if running_tvl_samples else None

        # Обе исторические точки сняты <1ч после открытия -- часовых свечей
        # с timestamp>=position_open_ts физически не существовало (первая
        # возможная граница часа -- 23:00:00Z, открытие было в 22:06:36Z).
        # Владелец, п.4: писать null, не выдумывать частичную свечу.
        n_hourly_candles = 0
        pool_volume_usd_sum_since_open = None
        pool_fees_usd_cum = None
        fee_capture_ratio_cumulative = None

        row.update({
            "pool_reserve_in_usd": pool_reserve_now,
            "our_fees_usd_cum": our_fees_usd_cum, "our_reserve_usd": our_reserve_usd,
            "pool_fees_usd_cum": pool_fees_usd_cum, "avg_pool_tvl_usd": avg_pool_tvl_usd,
            "hours_covered": hc, "n_hourly_candles": n_hourly_candles,
            "n_accrual_points": len(running_tvl_samples),
            "fee_capture_ratio_cumulative": fee_capture_ratio_cumulative,
        })
        # fee_capture_ratio -- старое имя поля (до переименования задачей
        # 2026-09-04) -- переносим в fee_capture_ratio_interval, если ещё
        # не сделано (совместимость со строками, записанными до ренейма).
        if "fee_capture_ratio" in row and "fee_capture_ratio_interval" not in row:
            row["fee_capture_ratio_interval"] = row.pop("fee_capture_ratio")

        print(f"[backfill] {ts}: our_reserve_usd={our_reserve_usd:.6f} our_yield_cum={our_yield_cum} "
              f"pool_reserve_in_usd={pool_reserve_now} avg_pool_tvl_usd={avg_pool_tvl_usd} "
              f"n_hourly_candles={n_hourly_candles} (>>> null, часовых свечей ещё не было) "
              f"fee_capture_ratio_cumulative={fee_capture_ratio_cumulative}")
        updated += 1

    ACCRUAL_LOG_PATH.write_text("\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows) + "\n")
    print(f"[backfill] обновлено строк: {updated}/{len(rows)}. Записано {ACCRUAL_LOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
