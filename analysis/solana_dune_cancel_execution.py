#!/usr/bin/env python3
"""Владелец, 2026-09-19: правило отмены -- при остановке платного
Dune-прогона (напр. cancel_workflow_run в GitHub Actions) СНАЧАЛА
отменять исполнение на стороне Dune через API, а не только
останавливать раннер (инцидент с днём 09-18: execute() почти наверняка
уже ушёл, а execution_id было негде взять -- отменить было нечем).

Источник execution_id -- data/p3_guard_cache/DUNE_LAST_EXECUTION.json,
который run_sql_sync (solana_dune_explorer_check.py) теперь пишет
СРАЗУ после успешного execute(), до начала опроса статуса; этот файл
коммитится шагом "Commit results" (if: always()) даже если раннер убит
посреди опроса -- т.е. переживает cancel_workflow_run."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, LAST_EXECUTION_MARKER_PATH, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_cancel_execution_result.json"


def main() -> None:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if not LAST_EXECUTION_MARKER_PATH.exists():
        result["HONEST_ANSWER"] = f"Маркер {LAST_EXECUTION_MARKER_PATH} не найден -- отменять нечего (или execute() ещё не был вызван к моменту остановки)."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[dune_cancel] " + result["HONEST_ANSWER"], flush=True)
        return

    marker = json.loads(LAST_EXECUTION_MARKER_PATH.read_text())
    result["marker"] = marker
    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_JANA_API не живой -- отменить не могу."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    probe = DuneProbe(os.environ[key_name])
    exec_id = marker["execution_id"]
    st = probe.status(exec_id)
    result["status_before_cancel"] = st
    cur_state = ((st.get("body") or {}).get("state"))
    if cur_state in ("QUERY_STATE_COMPLETED", "QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
        result["HONEST_ANSWER"] = (f"Исполнение {exec_id} уже в терминальном состоянии ({cur_state}) -- "
                                    f"отменять поздно, стоимость (если была) уже начислена.")
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[dune_cancel] " + result["HONEST_ANSWER"], flush=True)
        return

    cr = probe.cancel(exec_id)
    result["cancel_response"] = cr
    result["HONEST_ANSWER"] = f"Отправлена отмена исполнения {exec_id} (query_id={marker.get('query_id')}, name={marker.get('name')}), http_status={cr.get('http_status')}."
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print("[dune_cancel] " + result["HONEST_ANSWER"], flush=True)


if __name__ == "__main__":
    main()
