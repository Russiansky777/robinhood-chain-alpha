#!/usr/bin/env python3
"""Владелец, 2026-09-19: смена ключа Dune -- DUNE_JANA_API (Plus, остаток
1480 кредитов на момент смены), DUNE_EXPLORER_API больше не используем.
Дешёвая проверка ПЕРЕД любой платной работой: create_query (0 кредитов
по докстрингу solana_dune_explorer_check.py) + полный цикл
create->execute->poll->results на "SELECT 1 AS x" (без FROM -- не
сканирует ни одну таблицу, ожидаемо ~0 кредитов), чтобы честно
подтвердить, что и создание, и ИСПОЛНЕНИЕ запросов через API проходят,
не только создание."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_jana_key_check.json"


def main() -> None:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    discovery = step0_discover_keys()
    result["step0_key_discovery"] = discovery
    key_name = pick_working_key(discovery)
    result["key_name_used"] = key_name
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_JANA_API не живой (create_query упал) -- см. step0_key_discovery.liveness."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[jana_key_check] " + result["HONEST_ANSWER"], flush=True)
        return

    probe = DuneProbe(os.environ[key_name])
    r = probe.run_sql_sync("jana_key_full_roundtrip_select1", "SELECT 1 AS x", timeout_s=60)
    result["roundtrip_step"] = r
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    ok = r.get("status") == "ok" and r.get("rows") == [{"x": 1}]
    cost = (r.get("status_meta") or {}).get("execution_cost_credits")
    result["VERDICT"] = "DUNE_JANA_API рабочий, create/execute/results проходят" if ok else "ПРОБЛЕМА -- см. roundtrip_step"
    result["cost_credits_this_check"] = cost
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[jana_key_check] {result['VERDICT']} (стоимость этой проверки: {cost} кредитов)", flush=True)


if __name__ == "__main__":
    main()
