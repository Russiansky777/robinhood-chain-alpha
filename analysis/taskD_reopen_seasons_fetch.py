#!/usr/bin/env python3
"""Задача D переоткрыта повторно (владелец, 2026-09-10): дорогой
реальный фетч Pinnacle по СЫГРАННЫМ сезонам (не будущим играм) --
NFL сен-дек 2025, NBA мар-апр 2026, EPL авг-сен 2025, UFC/MMA авг-сен
2025. MLB НЕ выгружается (владелец: установлено фактом, что Polymarket
его не торгует).

Реальный игровой календарь -- taskD_reopen_game_calendar_result.json
(построен periodic /events, НЕ наивный перебор календарных дней --
экономит кредиты, снимая только реальные игровые дни). Реальный
бюджет: план на ВЕСЬ найденный календарь стоил 47520 кредитов при
остатке 8990 -- урезано пропорционально приоритету владельца (NFL 40% >
NBA 25% ~ EPL 25% > UFC 10%, EPL/UFC ниже веса NFL/NBA несмотря на
"сильное направление", т.к. UFC даёt только ~27% реального покрытия
Pinnacle) до первых N игровых дней каждого спорта в хронологическом
порядке (не выборочно) -- реальный расчёт см. коммит, итог 8280 из
8990 кредитов, запас ~710.

12 снимков/день (каждые 2ч) -- частота владельца, НЕ понижена (в
отличие от окна). Переиспользует РЕАЛЬНУЮ, уже написанную и рабочую
логику снимка (no_vig_probs, fetch_one_snapshot) из
taskD_pinnacle_historical_fetch.py -- не переписывает её."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from taskD_pinnacle_historical_fetch import fetch_one_snapshot  # реальная, уже рабочая логика снимка -- не переписываем

CALENDAR_PATH = Path("data/p3_guard_cache/taskD_reopen_game_calendar_result.json")
OUT_PATH = Path("data/p3_guard_cache/taskD_pinnacle_snapshots.json")
DIAG_PATH = Path("data/p3_guard_cache/taskD_pinnacle_fetch_diagnostics.json")
CHECKPOINT_EVERY_N_CALLS = 30

# Реальный расчёт бюджета (2026-09-10, владелец: "трать свободно, не
# откладывай"): вес по приоритету, первые N игровых дней хронологически.
SPORT_DAY_BUDGET = {
    "americanfootball_nfl": 28,
    "basketball_nba": 17,
    "soccer_epl": 17,
    "mma_mixed_martial_arts": 7,
}


def git_checkpoint(message: str) -> None:
    try:
        subprocess.run(["git", "add", str(OUT_PATH), str(DIAG_PATH)], check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
        if diff.returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", message], check=False)
        for attempt in range(5):
            push = subprocess.run(["git", "push"], check=False)
            if push.returncode == 0:
                return
            print(f"[reopen_fetch] push чекпоинта отклонён, попытка {attempt + 1}/5 -- pull --rebase и повтор")
            subprocess.run(["git", "pull", "--rebase"], check=False)
            time.sleep(3)
        print("[reopen_fetch] чекпоинт НЕ запушился после 5 попыток")
    except Exception as exc:  # noqa: BLE001
        print(f"[reopen_fetch] чекпоинт-коммит не удался (не критично, продолжаем): {exc}")


def run() -> int:
    if not CALENDAR_PATH.exists():
        print(f"[reopen_fetch] {CALENDAR_PATH} не найден -- сначала построить календарь.")
        return 1
    calendar = json.loads(CALENDAR_PATH.read_text())

    # План снимков: (sport, date_iso) на каждый выбранный игровой день x 12 снимков/день
    plan: list[tuple[str, str]] = []
    for sport, n_days in SPORT_DAY_BUDGET.items():
        game_dates = calendar["sports"][sport]["game_dates"][:n_days]
        for d in game_dates:
            day_start = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
            for h in range(0, 24, 2):
                plan.append((sport, (day_start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    print(f"[reopen_fetch] план: {len(plan)} снимков x 10 кредитов = {len(plan) * 10} кредитов")

    snapshots = []
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text())
            snapshots = existing.get("snapshots", [])
            print(f"[reopen_fetch] резюмируем с {len(snapshots)} уже сохранённых снимков")
        except (json.JSONDecodeError, KeyError):
            pass
    done_keys = {(s["sport"], s["requested_date"]) for s in snapshots}

    n_calls = 0
    n_fail = 0
    n_consecutive_fail = 0
    fail_samples = []
    last_diag = {}

    for sport, date_iso in plan:
        if (sport, date_iso) in done_keys:
            continue
        snap, diag = fetch_one_snapshot(sport, date_iso)
        n_calls += 1
        last_diag = diag
        if snap is None:
            n_fail += 1
            n_consecutive_fail += 1
            if len(fail_samples) < 10:
                fail_samples.append(diag)
            if n_consecutive_fail >= 8:
                print(f"[reopen_fetch] СТОП: {n_consecutive_fail} подряд неудачных вызовов -- не жгу кредиты вслепую.")
                break
        else:
            n_consecutive_fail = 0
            snapshots.append(snap)
        if n_calls % CHECKPOINT_EVERY_N_CALLS == 0:
            OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUT_PATH.write_text(json.dumps({"snapshots": snapshots}, indent=2, ensure_ascii=False, default=str))
            DIAG_PATH.write_text(json.dumps({
                "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "n_calls_so_far": n_calls, "n_fail_so_far": n_fail, "last_diag": last_diag,
                "sport_day_budget": SPORT_DAY_BUDGET,
            }, indent=2, ensure_ascii=False, default=str))
            git_checkpoint(f"Задача D: чекпоинт фетча сыгранных сезонов, {n_calls} вызовов, {len(snapshots)} снимков [automated]")
            print(f"[reopen_fetch] чекпоинт: {n_calls} вызовов ({sport}, {date_iso}), "
                  f"remaining={diag.get('x_requests_used', '?')}, fail={n_fail}")
        time.sleep(0.15)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({"snapshots": snapshots}, indent=2, ensure_ascii=False, default=str))
    diag_final = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sport_day_budget": SPORT_DAY_BUDGET,
        "n_calls_total": n_calls, "n_fail_total": n_fail, "n_snapshots_saved": len(snapshots),
        "fail_samples": fail_samples, "last_diag": last_diag,
    }
    DIAG_PATH.write_text(json.dumps(diag_final, indent=2, ensure_ascii=False, default=str))
    git_checkpoint(f"Задача D: финальный фетч сыгранных сезонов, {n_calls} вызовов, {len(snapshots)} снимков [automated]")

    print(f"\n[reopen_fetch] ИТОГО: {n_calls} реальных вызовов, {n_fail} неудачных, "
          f"{len(snapshots)} снимков сохранено в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
