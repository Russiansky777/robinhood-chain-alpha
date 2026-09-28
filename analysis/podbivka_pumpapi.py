#!/usr/bin/env python3
"""Подбивка: PumpApi (pumpapi.io) как источник данных -- только чтение, без ключей.

Шаг --docs: скачать документацию https://pumpapi.io/llms-full.txt (и llms.txt) в
data/podbivka/pumpapi/ -- из контейнера сессии домен закрыт политикой сети,
поэтому -- облаком. Дальнейшие шаги (архив, поток) -- по прочтении документации.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПАПКА = КОРЕНЬ / "data" / "podbivka" / "pumpapi"


def docs() -> list:
    import requests  # noqa: PLC0415
    ПАПКА.mkdir(parents=True, exist_ok=True)
    из_ = []
    for url, имя in (("https://pumpapi.io/llms-full.txt", "llms-full.txt"), ("https://pumpapi.io/llms.txt", "llms.txt")):
        try:
            о = requests.get(url, timeout=60, headers={"User-Agent": "podbivka-readonly/1.0"})
            p = ПАПКА / имя
            p.write_bytes(о.content)
            из_.append(p)
            print(f"{url}: http {о.status_code}, {len(о.content)} байт")
        except Exception as exc:  # noqa: BLE001
            print(f"{url}: {type(exc).__name__}")
    (ПАПКА / "docs_meta.json").write_text(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                      "файлы": [p.name for p in из_]}, ensure_ascii=False),
                                          encoding="utf-8")
    return из_ + [ПАПКА / "docs_meta.json"]


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--docs", action="store_true")
    а = р.parse_args()
    import podbivka_run as R  # noqa: PLC0415
    файлы = docs() if а.docs else []
    for p in файлы:
        R.записано(p)
    if файлы:
        R.пуш("Podbivka-2: pumpapi docs [automated]", [str(p) for p in файлы])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
