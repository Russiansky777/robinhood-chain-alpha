#!/usr/bin/env python3
"""Владелец, 2026-09-19: сверка "32 против 11" (Task 3 этого сообщения).

Найдена РЕАЛЬНАЯ причина: data/solana_29_candidates_flow_7d.json содержит
ОТДЕЛЬНУЮ секцию leader_control (лидер как контроль, 221 первый вход за
7 суток) с first_entries_per_day=31.571 -- ЭТО и есть "32/сутки" из
памяти владельца, реальное число, не выдумка. НО: SOL-эквивалент для
leader_control НИКОГДА не считался -- solana_29_candidates_usdc_to_sol.py
конвертировал только секцию wallets (29 кандидатов), leader_control
пропущен, поэтому его n_first_entries_ge_4_3_sol_per_day=0 (сырой
непереведённый порог, ложный ноль, не факт про лидера).

Этот скрипт: (1) досчитывает SOL-эквивалент для leader_control ТЕМ ЖЕ
методом (пул SOL/USDC, минутные свечи GeckoTerminal, линейная
интерполяция) -- честно новый вызов (не Dune/Alchemy, только Gecko,
без ограничений по стоимости, установленных для Dune в этом сообщении);
(2) строит почасовую (UTC) гистограмму первых входов лидера >=4.3
SOL-экв. за 18-19 сентября рядом с гистограммой 5 сигналов
solana_pilot_signal_capture_rate_v2.py (уже посчитаны локально, без
новых вызовов) -- чтобы увидеть, до или после старта пилота (16:37Z)
была основная активность."""
from __future__ import annotations

import bisect
import calendar
import json
import time
from decimal import Decimal as D
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_leader_signal_reconciliation.json"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
SOL_USDC_POOL = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"
SOL_THRESHOLD = D("4.3")


def gecko_get(path: str, params: dict) -> dict:
    resp = requests.get(f"{GECKO_BASE}{path}", params=params, timeout=30, headers={"Accept": "application/json"})
    return {"http_status": resp.status_code, "body": resp.json() if resp.ok else None}


_candles_cache: list[list] | None = None


def sol_usd_price_at(t: int) -> float | None:
    global _candles_cache
    if _candles_cache is None:
        r = gecko_get(f"/networks/solana/pools/{SOL_USDC_POOL}/ohlcv/minute",
                      {"aggregate": 1, "before_timestamp": t + 8 * 24 * 3600, "limit": 1000, "currency": "usd"})
        rows = (((r.get("body") or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        _candles_cache = sorted(rows, key=lambda c: c[0])
    rows = _candles_cache
    if not rows:
        return None
    times = [c[0] + 60 for c in rows]
    idx = bisect.bisect_right(times, t)
    if idx == 0:
        return rows[0][4]
    if idx >= len(rows):
        return rows[-1][4]
    t0, c0 = times[idx - 1], D(str(rows[idx - 1][4]))
    t1, c1 = times[idx], D(str(rows[idx][4]))
    if t1 == t0:
        return float(c0)
    frac = D(t - t0) / D(t1 - t0)
    return float(c0 + (c1 - c0) * frac)


def hour_bucket(bt: int) -> str:
    return time.strftime("%Y-%m-%dT%H:00Z", time.gmtime(bt))


def main() -> None:
    flow = json.loads((REPO_ROOT / "data" / "solana_29_candidates_flow_7d.json").read_text())
    lc = flow["leader_control"]
    events = lc["first_entry_events"]

    n_converted = 0
    ge_events = []
    for e in events:
        if e.get("quote_amount") is None:
            continue
        sol_usd = sol_usd_price_at(e["block_time"])
        if not sol_usd:
            continue
        sol_equiv = e["quote_amount"] / sol_usd
        e["sol_equivalent"] = sol_equiv
        n_converted += 1
        if sol_equiv >= float(SOL_THRESHOLD):
            ge_events.append(e)

    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gap_found": (
            "leader_control в solana_29_candidates_flow_7d.json НИКОГДА не проходил конвертацию USDC->SOL "
            "(solana_29_candidates_usdc_to_sol.py обрабатывал только секцию wallets/29 кандидатов) -- "
            "n_first_entries_ge_4_3_sol_per_day=0 у лидера был ложным нулём (непереведённый порог), не фактом."
        ),
        "leader_control_days_covered": lc["days_covered"],
        "leader_control_n_first_entries_total": lc["n_first_entries"],
        "leader_control_first_entries_per_day_ALL_SIZES": lc["first_entries_per_day"],
        "n_events_converted_to_sol_equiv": n_converted,
        "n_ge_4_3_sol_equiv": len(ge_events),
        "corrected_ge_4_3_per_day": round(len(ge_events) / lc["days_covered"], 3) if lc["days_covered"] else None,
    }
    print(f"[reconcile] лидер: {lc['n_first_entries']} первых входов всех размеров за {lc['days_covered']}сут "
          f"({lc['first_entries_per_day']}/сутки) -- ИСПРАВЛЕНО >=4.3 SOL-экв: {len(ge_events)} "
          f"({result['corrected_ge_4_3_per_day']}/сутки)", flush=True)

    # --- Почасовая гистограмма: candidates-flow (исправленный >=4.3) ---
    hist_flow: dict[str, int] = {}
    for e in ge_events:
        h = hour_bucket(e["block_time"])
        hist_flow[h] = hist_flow.get(h, 0) + 1

    # --- Почасовая гистограмма: pilot_signal_capture_rate_v2 (5 канонических сигналов) ---
    cap_path = REPO_ROOT / "data" / "solana_pilot_signal_capture_rate_v2.json"
    hist_capture: dict[str, int] = {}
    pilot_start_hour = None
    if cap_path.exists():
        cap = json.loads(cap_path.read_text())
        pilot_start_hour = hour_bucket(cap["pilot_start_time"])
        all_capture_events = (
            [{"block_time": m["block_time"]} for m in cap.get("matched_with_classify_explanation", [])] +
            [{"block_time": calendar.timegm(time.strptime(m["block_time_utc"], "%Y-%m-%dT%H:%M:%SZ"))}
             for m in cap.get("missed_signals_full_detail", [])]
        )
        for e in all_capture_events:
            h = hour_bucket(int(e["block_time"]))
            hist_capture[h] = hist_capture.get(h, 0) + 1

    all_hours = sorted(set(hist_flow) | set(hist_capture))
    table = [{"hour_utc": h, "leader_control_ge_4_3": hist_flow.get(h, 0),
              "pilot_capture_v2_ge_4_3": hist_capture.get(h, 0)} for h in all_hours]
    result["hourly_table"] = table
    result["pilot_start_hour_utc"] = pilot_start_hour

    n_before_pilot_start = sum(1 for e in ge_events if pilot_start_hour and hour_bucket(e["block_time"]) < pilot_start_hour)
    n_after_pilot_start = len(ge_events) - n_before_pilot_start
    result["n_leader_ge_4_3_before_pilot_start"] = n_before_pilot_start
    result["n_leader_ge_4_3_after_pilot_start"] = n_after_pilot_start

    print(f"[reconcile] старт пилота: {pilot_start_hour}", flush=True)
    print(f"[reconcile] лидерских >=4.3 сигналов ДО старта пилота: {n_before_pilot_start}, ПОСЛЕ: {n_after_pilot_start}", flush=True)
    for row in table:
        print(f"  {row['hour_utc']}  leader_control={row['leader_control_ge_4_3']:>2}  "
              f"pilot_capture={row['pilot_capture_v2_ge_4_3']:>2}", flush=True)

    if n_after_pilot_start and result.get("corrected_ge_4_3_per_day"):
        window_capture_per_day = 24 / max((time.time() - cap["pilot_start_time"]) / 3600, 0.01) * n_after_pilot_start if cap_path.exists() else None
        result["reconciliation_note"] = (
            f"Из {len(ge_events)} исправленных сигналов лидера (>=4.3 SOL-экв, 7 суток) {n_before_pilot_start} "
            f"пришлись на время ДО старта пилота, {n_after_pilot_start} -- ПОСЛЕ. Если n_after_pilot_start мало "
            "относительно общего 7-суточного темпа -- расхождение объясняется ВРЕМЕННЫМ ОКНОМ (пилот запущен в "
            "относительно тихий период), а не ошибкой одного из скриптов. Если n_after_pilot_start, "
            "пересчитанный в темп/сутки, всё равно сильно ближе к 32, чем к 11.7 -- тогда скрипт захвата "
            "пилота (solana_pilot_signal_capture_rate_v2.py) занижает, и это указывает на ошибку в НЁМ, не в "
            "candidates-flow."
        )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[reconcile] записано в {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
