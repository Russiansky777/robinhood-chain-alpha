#!/usr/bin/env python3
"""Проверка 1 к переключению Y (Задача 1) на Chainlink-оракул (владелец,
2026-09-05): "Обновляется ли фид по выходным: таймстемпы AnswerUpdated
для 5 фидов за три последних выходных, пт 20:00 -> вс 20:00 ET. Число
обновлений в окне. Это не техническая проверка -- это факт о том, есть
ли у токена референс по выходным, и он войдёт в интерпретацию
результата."

5 фидов -- реальные, из уже провалидированной карты Sprint R1
(data/sprintR1_cache/r1_feed_token_map.csv), выбраны пересечением с
тикерами, которым реально не хватило Y от Stooq в смоуке Задачи 1
(missing_y_diagnostic, task1_weekend_gap_result.json): TSLA, AMZN,
GOOGL, SPY, QQQ.

`contract_address` в chainlink_robinhood.dualaggregator_evt_answerupdated
-- ПРЕВЕНТИВНО через from_hex() (владелец, правило #23, п.2: "ограничение
ещё актуально?" -- dex.trades только что реально оказался VARBINARY,
не VARCHAR, хотя старый код Sprint R1 предполагал обратное; decoded
event-таблицы на Dune тем же образом кодируют адреса -- не ждём
повторного падения, чтобы это подтвердить заново)."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task1_weekend_gap")
sys.path.insert(0, str(Path(__file__).parent))

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402
from run_pipeline import read_sql  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task1_chainlink_weekend_check_result.json")

FEEDS = {
    "TSLA": "0x7a6b81ba7fbcb90104d8c496158cf383cd7233b1",
    "AMZN": "0x93503dfc97157cdb8aadccaf70452621d598fdeb",
    "GOOGL": "0x11ed6d598ef565dda86fafe7e779303e7cc6b2bd",
    "SPY": "0x78bcb218fa04b9b3a278ebc865ed320bf8defbac",
    "QQQ": "0x25e996ce8b3529885d429241156e83e7b7744049",
}


def real_last_n_complete_fridays(n: int, now_utc: datetime) -> list[str]:
    d = now_utc
    while d.weekday() != 4:
        d -= timedelta(days=1)
    d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    fridays = []
    while len(fridays) < n:
        sunday_2000_et_utc = d + timedelta(days=3)  # вс 20:00 ET = пн 00:00 UTC = friday_utc+3d
        if sunday_2000_et_utc <= now_utc:
            fridays.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=7)
    return sorted(fridays)


def run() -> int:
    credit_guard.ensure_namespace("task1_weekend_gap", 250.0)
    now_utc = datetime.now(timezone.utc)
    fridays = real_last_n_complete_fridays(3, now_utc)
    print(f"[chainlink_check] 3 последних завершённых выходных: {fridays}")
    print(f"[chainlink_check] 5 фидов: {FEEDS}")

    feed_list_sql = ",".join(f"from_hex('{addr[2:].lower()}')" for addr in FEEDS.values())
    friday_list_sql = ",".join(f"timestamp '{f} 00:00:00'" for f in fridays)
    sql = (read_sql("task1/task1_chainlink_weekend_updates")
           .replace("{{feed_address_list}}", feed_list_sql)
           .replace("{{weekend_friday_list}}", friday_list_sql))

    client = DuneClient()
    qid = client.create_query("task1_chainlink_weekend_updates", sql)
    df = client.run_sql_cached("task1_chainlink_weekend_updates", sql, query_id=qid,
                                 estimated_credits=3.0, expected_max_rows=50, expected_columns=5)
    if df is None or not len(df):
        print("[chainlink_check] Dune вернул пусто")
        return 1

    addr_to_symbol = {v.lower(): k for k, v in FEEDS.items()}
    df["symbol"] = df["feed_address"].str.lower().map(addr_to_symbol)
    print(df.to_string(index=False))

    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "weekends_checked": fridays, "feeds": FEEDS,
              "rows": df.to_dict("records")}
    n_zero = int((df["n_updates_in_window"] == 0).sum())
    result["n_weekend_windows_with_zero_updates"] = n_zero
    result["n_total_windows"] = len(df)
    print(f"\n[chainlink_check] окон (фид x выходные) с НУЛЁМ обновлений: {n_zero} из {len(df)}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"[chainlink_check] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
