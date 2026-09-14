#!/usr/bin/env python3
"""Инспекция уже записанных JSONL этого замера (read-only, без сети,
можно перезапускать сколько угодно) -- фид оборвался почти сразу
(disconnect_error: MultipleValuesError: 'server-timing', ~5с жизни
соединения, реальный баг ЭТОГО измерительного скрипта -- dict() на
multi-value Headers, НЕ поведение сервера) -- нужно честно увидеть,
что вообще успело записаться до сбоя."""
from __future__ import annotations

import json
from pathlib import Path

OUT_DIR = Path(__file__).parent.parent / "data" / "task5_v4_feed_vs_alchemy_measurement"


def main() -> None:
    result: dict = {}
    for name in ("feed_messages.jsonl", "alchemy_messages.jsonl"):
        path = OUT_DIR / name
        if not path.exists():
            result[name] = {"exists": False}
            continue
        lines = path.read_text().splitlines()
        result[name] = {
            "exists": True, "n_lines": len(lines),
            "first_3": [json.loads(l) for l in lines[:3]] if lines else [],
            "last_3": [json.loads(l) for l in lines[-3:]] if lines else [],
        }
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
