#!/usr/bin/env python3
"""Задача D (переоткрыта, 2026-09-10) -- ДОРОГОЙ шаг, реальные деньги
($30/мес план, ~10800 из 20000 кредитов на этот прогон). Выгружает
исторические котировки Pinnacle (h2h, регион eu) через The Odds API за
30 дней по NFL/NBA/MLB, 12 снимков/день (каждые 2 часа) -- бюджет
подтверждён отдельным прогоном (taskD_odds_credit_budget.py).

Реальная стоимость ОДНОГО вызова измерена эмпирически: 10 кредитов
(regions=eu x markets=h2h x 10), НЕ по документации (заблокирована
сетевой политикой). НЕ содержит логики сопоставления с Polymarket и
расчёта расхождений -- это ОТДЕЛЬНЫЙ бесплатный скрипт
(taskD_pinnacle_polymarket_analysis.py), чтобы баг в анализе не требовал
повторной траты кредитов.

Владелец, 2026-09-10 (переформулированная предрегистрация): критерий
экономический (размер и живучесть расхождения), не микроструктурный --
но сам ФЕТЧ снимков не знает о критерии, просто сохраняет реальные
котировки на явно заданных временных точках, checkpoint'ится по ходу
(реальные деньги потрачены -- нельзя потерять прогресс при обрыве, тот
же урок, что и ретро-сторожок).

Ключ ТОЛЬКО из переменной окружения THE_ODDS_API -- нигде не печатается
и не пишется в результат."""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskD-pinnacle-fetch/1.0"}
BASE = "https://api.the-odds-api.com/v4"
API_KEY = os.environ.get("THE_ODDS_API", "")

OUT_PATH = Path("data/p3_guard_cache/taskD_pinnacle_snapshots.json")
DIAG_PATH = Path("data/p3_guard_cache/taskD_pinnacle_fetch_diagnostics.json")

# Реальный подтверждённый план владельца (2026-09-10): 30 дней, 12
# снимков/день (каждые 2ч), 3 спорта -- 10800 из 20000 кредитов (54%).
# Переопределяемо через env для дешёвого smoke-теста перед полным прогоном.
WINDOW_DAYS = int(os.environ.get("TASKD_WINDOW_DAYS", "30"))
SNAPSHOTS_PER_DAY = int(os.environ.get("TASKD_SNAPSHOTS_PER_DAY", "12"))
SPORTS = os.environ.get("TASKD_SPORTS", "americanfootball_nfl,basketball_nba,baseball_mlb").split(",")
CHECKPOINT_EVERY_N_CALLS = 30

REGIONS = "eu"  # подтверждено эмпирически: Pinnacle реально возвращается в этом регионе
MARKETS = "h2h"


def git_checkpoint(message: str) -> None:
    """Тот же паттерн pull-rebase-retry, что уже исправлен для
    ретро-сторожка и credit_guard в этой сессии -- реальные потраченные
    кредиты не должны теряться при обрыве джоба."""
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
            print(f"[taskD_fetch] push чекпоинта отклонён, попытка {attempt + 1}/5 -- pull --rebase и повтор")
            subprocess.run(["git", "pull", "--rebase"], check=False)
            time.sleep(3)
        print("[taskD_fetch] чекпоинт НЕ запушился после 5 попыток")
    except Exception as exc:  # noqa: BLE001
        print(f"[taskD_fetch] чекпоинт-коммит не удался (не критично, продолжаем): {exc}")


def no_vig_probs(outcomes: list[dict]) -> dict[str, float] | None:
    """Пропорциональный метод снятия вига -- стандартный, реальный:
    implied_i = 1/price_i, no_vig_i = implied_i / sum(implied)."""
    try:
        implied = {o["name"]: 1.0 / float(o["price"]) for o in outcomes if o.get("price")}
        total = sum(implied.values())
        if total <= 0:
            return None
        return {name: v / total for name, v in implied.items()}
    except (KeyError, ValueError, ZeroDivisionError, TypeError):
        return None


def fetch_one_snapshot(sport: str, date_iso: str) -> tuple[dict | None, dict]:
    diag = {"sport": sport, "requested_date": date_iso}
    try:
        r = requests.get(f"{BASE}/historical/sports/{sport}/odds", params={
            "apiKey": API_KEY, "regions": REGIONS, "markets": MARKETS,
            "bookmakers": "pinnacle", "date": date_iso, "oddsFormat": "decimal",
        }, headers=HEADERS, timeout=25)
        diag["status"] = r.status_code
        diag["x_requests_used"] = r.headers.get("x-requests-used")
        diag["x_requests_last"] = r.headers.get("x-requests-last")
        if r.status_code != 200:
            diag["body_snippet"] = r.text[:300]
            return None, diag
        body = r.json()
        actual_ts = body.get("timestamp")
        events_out = []
        for ev in body.get("data", []):
            bms = ev.get("bookmakers", [])
            pinnacle = next((b for b in bms if b.get("key") == "pinnacle"), None)
            if not pinnacle:
                continue
            h2h = next((m for m in pinnacle.get("markets", []) if m.get("key") == "h2h"), None)
            if not h2h:
                continue
            probs = no_vig_probs(h2h.get("outcomes", []))
            if not probs:
                continue
            events_out.append({
                "id": ev.get("id"), "sport_key": ev.get("sport_key"),
                "commence_time": ev.get("commence_time"),
                "home_team": ev.get("home_team"), "away_team": ev.get("away_team"),
                "pinnacle_last_update": h2h.get("last_update"),
                "outcomes_decimal": h2h.get("outcomes"),
                "no_vig_probs": probs,
            })
        return {"sport": sport, "requested_date": date_iso, "actual_timestamp": actual_ts,
                "n_events_with_pinnacle_h2h": len(events_out), "events": events_out}, diag
    except requests.exceptions.RequestException as exc:
        diag["exception"] = str(exc)[:300]
        return None, diag


def run() -> int:
    if not API_KEY:
        print("[taskD_fetch] THE_ODDS_API не найден в окружении -- останавливаюсь.")
        return 1

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=WINDOW_DAYS)
    step_hours = 24 / SNAPSHOTS_PER_DAY
    timestamps = []
    t = window_start
    while t <= now:
        timestamps.append(t)
        t += timedelta(hours=step_hours)

    print(f"[taskD_fetch] план: {len(SPORTS)} спортов x {len(timestamps)} снимков = "
          f"{len(SPORTS) * len(timestamps)} вызовов x 10 кредитов = {len(SPORTS) * len(timestamps) * 10} кредитов")

    snapshots = []
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text())
            snapshots = existing.get("snapshots", [])
            print(f"[taskD_fetch] найден существующий файл -- резюмируем с {len(snapshots)} уже сохранённых снимков")
        except (json.JSONDecodeError, KeyError):
            pass
    done_keys = {(s["sport"], s["requested_date"]) for s in snapshots}

    n_calls = 0
    n_fail = 0
    n_consecutive_fail = 0
    fail_samples = []
    last_diag = {}

    for sport in SPORTS:
        for ts in timestamps:
            date_iso = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
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
                    print(f"[taskD_fetch] СТОП: {n_consecutive_fail} подряд неудачных вызовов -- "
                          "возможна проблема с ключом/лимитом, не жгу кредиты вслепую дальше.")
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
                }, indent=2, ensure_ascii=False, default=str))
                git_checkpoint(f"Задача D: чекпоинт фетча Pinnacle, {n_calls} вызовов, {len(snapshots)} снимков [automated]")
                print(f"[taskD_fetch] чекпоинт: {n_calls} вызовов ({sport}, {date_iso}), "
                      f"remaining={diag.get('x_requests_used', '?')}, fail={n_fail}")
            time.sleep(0.15)
        if n_consecutive_fail >= 8:
            break

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({"snapshots": snapshots}, indent=2, ensure_ascii=False, default=str))
    diag_final = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_days": WINDOW_DAYS, "snapshots_per_day": SNAPSHOTS_PER_DAY, "sports": SPORTS,
        "n_calls_total": n_calls, "n_fail_total": n_fail, "n_snapshots_saved": len(snapshots),
        "fail_samples": fail_samples, "last_diag": last_diag,
    }
    DIAG_PATH.write_text(json.dumps(diag_final, indent=2, ensure_ascii=False, default=str))
    git_checkpoint(f"Задача D: финальный фетч Pinnacle, {n_calls} вызовов, {len(snapshots)} снимков [automated]")

    print(f"\n[taskD_fetch] ИТОГО: {n_calls} реальных вызовов, {n_fail} неудачных, "
          f"{len(snapshots)} снимков сохранено в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
