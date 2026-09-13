#!/usr/bin/env python3
"""Печатает ПОЛНУЮ уже сохранённую запись (fund_flow_checks) для
конкретного отобранного кандидата -- 0x878fb998... (блок 61636697),
первого из 120 (знак исправлен), прошедшего критерии владельца. Только
чтение уже сохранённого файла, ни одного нового RPC-вызова."""
import json
from pathlib import Path

RESULT_FILE = Path(__file__).parent.parent / "data" / "task5_v4_item3_in_window_control_trade_result.json"
TARGET_TX = "0x878fb998474230338a47a710d2c25689987a836074729ae61e112dcccca7cd1b"


def main() -> None:
    d = json.loads(RESULT_FILE.read_text())
    ffc = d.get("fund_flow_checks") or []
    row = next((r for r in ffc if r.get("tx_hash", "").lower() == TARGET_TX.lower()), None)
    print(json.dumps(row, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
