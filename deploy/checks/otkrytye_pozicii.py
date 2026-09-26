#!/usr/bin/env python3
"""Какие позиции считаются ОТКРЫТЫМИ прямо сейчас. Только чтение.

Гейт деплоя сторожа спрашивает ExecState().open_positions() и отказывается
перезапускать службу, пока их больше нуля. Этот разбор печатает, ЧТО именно
считается открытым: минт, состояние, возраст, чья позиция и что известно о
продаже. Ничего не меняет и ничего не продаёт.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.environ.get("PYTHONPATH", "") or ".")

import bloom_exec_state as ST  # noqa: E402


def main() -> int:
    с = ST.ExecState()
    поз = с.open_positions()
    сейчас = time.time()
    из_ = []
    for p in поз:
        итог, расход = (None, None)
        try:
            итог, расход = ST.итог_позиции(p)
        except Exception as exc:  # noqa: BLE001
            итог = f"счёт не вышел: {type(exc).__name__}"
        из_.append({
            "client_order_id": p.get("client_order_id"),
            "mint": p.get("mint"),
            "state": p.get("state"),
            "lane": p.get("lane"),
            "mode": p.get("mode"),
            "sol_in": p.get("sol_in"),
            "qty": p.get("qty") or p.get("amount_raw"),
            "ts_intent_utc": p.get("ts_intent_utc"),
            "возраст_часов": (round((сейчас - float(p.get("ts_intent") or 0)) / 3600, 2)
                              if p.get("ts_intent") else None),
            "sell_attempts": p.get("sell_attempts"),
            "last_sell_outcome": (p.get("last_sell_outcome") or {}).get("code")
                                 if isinstance(p.get("last_sell_outcome"), dict) else p.get("last_sell_outcome"),
            "chain_ok": p.get("chain_ok"),
            "result_uncountable": p.get("result_uncountable"),
            "итог_по_формуле": итог,
            "расход": расход,
            "why_not": p.get("why_not") or p.get("closed_why_not"),
        })
    print(json.dumps({"открытых": len(из_), "позиции": из_}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
