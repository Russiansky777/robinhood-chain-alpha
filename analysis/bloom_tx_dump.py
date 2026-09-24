#!/usr/bin/env python3
"""Сырой getTransaction в файл. ТОЛЬКО ЧТЕНИЕ.

Зачем. Разбор сигнала надо проверять на ТОЙ ЖЕ транзакции, на которой он
сломался, и проверять офлайн -- без сети и без службы. Для этого нужен
сырой ответ узла, а не чья-то выжимка: выжимка уже могла потерять то поле,
из-за которого всё и пошло не так (например loadedAddresses у версии 0).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sig", action="append", default=[], required=False)
    ap.add_argument("--out", default="data/bloom_tx_raw.json")
    a = ap.parse_args()
    if not a.sig:
        print("нужна хотя бы одна --sig")
        return 2
    h = BD.Helius(служба="")
    вых = {}
    for s in a.sig:
        try:
            tx = h.транзакция(s)
        except Exception as exc:  # noqa: BLE001
            вых[s] = {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:200]}"}
            continue
        вых[s] = ({"ok": True, "tx": tx} if tx else
                   {"ok": False, "why_not": "узел не отдал транзакцию"})
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(вых, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    for s, r in вых.items():
        print(f"{s[:16]}: ok={r.get('ok')} {r.get('why_not') or ''}")
        tx = r.get("tx") or {}
        msg = ((tx.get("transaction") or {}).get("message") or {})
        meta = tx.get("meta") or {}
        print(f"   версия={tx.get('version')} слот={tx.get('slot')} "
              f"статических ключей={len(msg.get('accountKeys') or [])} "
              f"loadedAddresses={'есть' if meta.get('loadedAddresses') else 'нет'} "
              f"lookups={len(msg.get('addressTableLookups') or [])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
