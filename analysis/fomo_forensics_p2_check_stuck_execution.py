#!/usr/bin/env python3
"""Форензика fomo, п.2: run 34002333499 (аккаунт Mozila) был убит GH
Actions по таймауту джоба (20 мин) ПОКА execution_id=01M1T353SCDK1QPEQXH7SYG4R9
(этап 1a, LIMIT 100 -- ожидался как дешёвая, быстрая проверка синтаксиса)
всё ещё висел в поллинге -- ни успех, ни провал не были записаны в леджер,
Python-обработчик TimeoutError (который сам пишет запись "ТАЙМАУТ
поллинга") тоже не сработал, потому что процесс убили извне, а не
собственный 1800s-таймаут поллинга. Реальный статус/стоимость этого
execution_id неизвестны -- проверяем напрямую, не гадаем и не считаем
0 кредитов по умолчанию."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "fomo_forensics_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from dune_client import DuneClient  # noqa: E402
from credit_guard import record_execution, load_state, CREDITS_FILE  # noqa: E402

STUCK_EXECUTION_ID = "01M1T353SCDK1QPEQXH7SYG4R9"
OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_p2_check_stuck_execution_result.json")


def run() -> int:
    client = DuneClient()
    status = client._get(f"/execution/{STUCK_EXECUTION_ID}/status")
    print(json.dumps(status, indent=2, ensure_ascii=False, default=str))

    state_ns = load_state().get("fomo_forensics_mozila", {})
    already_recorded = any(
        e.get("execution_id") == STUCK_EXECUTION_ID for e in load_state().get("entries", [])
    )
    print(f"[check] уже записано в леджере ранее: {already_recorded}")

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "execution_id": STUCK_EXECUTION_ID,
        "status": status,
        "already_recorded_in_ledger": already_recorded,
    }

    state = status.get("state")
    cost = status.get("execution_cost_credits")
    if not already_recorded and state in ("QUERY_STATE_COMPLETED", "QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
        record_execution(
            f"fomo_forensics_p2_sample_trades_dryrun100 [восстановлено после kill джоба GH Actions, реальный статус={state}]",
            cost, STUCK_EXECUTION_ID,
        )
        print(f"[check] записано в леджер: state={state}, cost={cost}")
        out["recorded_now"] = True
    elif state in ("QUERY_STATE_EXECUTING", "QUERY_STATE_PENDING"):
        print(f"[check] ВНИМАНИЕ: execution всё ещё {state} на стороне Dune -- НЕ завершилось само по себе даже сейчас, "
              "продолжает потенциально тратить время (кредиты по факту неизвестны до завершения).")
        out["recorded_now"] = False
    else:
        out["recorded_now"] = False

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[check] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
