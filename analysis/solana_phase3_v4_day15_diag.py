#!/usr/bin/env python3
"""Диагностика: день 09-15 Фазы 3 v4 упал (QUERY_STATE_FAILED), полное
тело ошибки не сохранилось (print в основном скрипте обрезает до 800
символов). execution_id уже известен и завершён (терминальное
состояние) -- повторный GET /execution/{id}/status ничего не
пересчитывает и не должен стоить кредитов (это чтение уже готового
результата, не новое исполнение)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_phase3_v4_day15_diag_result.json"
EXECUTION_ID = "01M2X66HHN0FA1Z27ECXE8AAT1"


def main() -> None:
    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    if key_name is None:
        OUT_PATH.write_text(json.dumps({"HONEST_ANSWER": "DUNE_JANA_API не живой."}, indent=2))
        return
    probe = DuneProbe(os.environ[key_name])
    st = probe.status(EXECUTION_ID)
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "execution_id": EXECUTION_ID, "status_response": st}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
