#!/usr/bin/env python3
"""Извлечение уже сохранённого результата трассировки 0x356680b7
(task5_v4_item4_insufficientfunds_trace.py, седьмой/восьмой раунд) --
read-only, файл уже существует на Ohio, новый реплей не запускается."""
import json
from pathlib import Path

PATH = Path(__file__).parent.parent / "data" / "task5_v4_item4_insufficientfunds_trace_result.json"

if not PATH.exists():
    print(json.dumps({"exists": False}))
else:
    d = json.loads(PATH.read_text())
    out = {
        "exists": True,
        "route_id": d.get("route_id"),
        "selector_candidate_name_note": d.get("selector_candidate_name_note"),
        "fork_block": d.get("fork_block"),
        "amount_in": d.get("amount_in"),
        "amount_in_source": d.get("amount_in_source"),
        "revert_reproduced_on_fork": d.get("revert_reproduced_on_fork"),
        "estimate_gas_error_on_fork": d.get("estimate_gas_error_on_fork"),
        "debug_trace_call_ok": (d.get("debug_trace_call_on_local_fork") or {}).get("ok"),
        "first_erroring_call_via_debug_trace_call": (d.get("debug_trace_call_on_local_fork") or {}).get("first_erroring_call"),
        "debug_trace_transaction_on_local_send": d.get("debug_trace_transaction_on_local_send"),
        "ok": d.get("ok"),
        "error": d.get("error"),
    }
    print(json.dumps(out, indent=2, default=str, ensure_ascii=False))
