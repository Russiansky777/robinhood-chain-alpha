#!/usr/bin/env python3
"""C2: ключей в выгрузках и логах быть не должно. Проверка перед коммитом.

Два слоя, как у Code-1 в шаге «Токенов в отчёте быть не должно», но без
heredoc внутри run: |:
1. шаблоны: `api-key=<значение>`, `Bearer <значение>`, `X-API-KEY: <значение>`;
2. БУКВАЛЬНЫЕ значения секретов из окружения (HELIUS_API, HELIUS_API_KEY,
   DBOT_API_KEY): если строка секрета встретилась в файле -- провал.
   Значения не печатаются никогда -- только имя файла и имя шаблона.

Код возврата 1 -- утечка найдена, коммит делать нельзя.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PATTERNS = (
    ("api-key", re.compile(r"api-key=[A-Za-z0-9\-_]{8,}", re.I)),
    ("bearer", re.compile(r"bearer\s+[A-Za-z0-9\-_.=]{12,}", re.I)),
    ("x-api-key", re.compile(r"x-api-key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9\-_]{8,}", re.I)),
)
SECRET_ENVS = ("HELIUS_API", "HELIUS_API_KEY", "DBOT_API_KEY")


def scan_text(text: str, secrets: list) -> list:
    hits = [name for name, rx in PATTERNS if rx.search(text)]
    hits += [f"значение секрета #{i + 1}" for i, s in enumerate(secrets) if s and s in text]
    return hits


def files_of(paths: list) -> list:
    out = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            out += [x for x in pp.rglob("*") if x.is_file()]
        elif pp.is_file():
            out.append(pp)
    return out


def main(argv: list) -> int:
    if "--self-test" in argv:
        return self_test()
    secrets = [(os.environ.get(n) or "").strip() for n in SECRET_ENVS]
    secrets = [s for s in secrets if len(s) >= 8]
    bad = []
    files = files_of([a for a in argv if not a.startswith("--")])
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        h = scan_text(text, secrets)
        if h:
            bad.append((str(f), h))
    print(f"проверено файлов: {len(files)}, секретов для сверки: {len(secrets)}")
    if bad:
        for f, h in bad:
            print(f"УТЕЧКА: {f}: {', '.join(h)}")
        return 1
    print("ключей в выгрузках нет")
    return 0


def self_test() -> int:
    checks = [
        ("url с ключом ловится", scan_text("https://x/?api-key=abcd1234efgh", []) == ["api-key"]),
        ("Bearer ловится", "bearer" in scan_text("Authorization: Bearer abcdefghijklmnop", [])),
        ("X-API-KEY ловится", "x-api-key" in scan_text('{"X-API-KEY": "abcd1234efgh"}', [])),
        ("буквальное значение секрета ловится",
         scan_text("строка SECRETVALUE123 внутри", ["SECRETVALUE123"]) == ["значение секрета #1"]),
        ("заглушка <КЛЮЧ> не ловится", scan_text("?api-key=<КЛЮЧ>", []) == []),
        ("обычный текст чист", scan_text("кредитов за прогон 1234, api-key не задан", []) == []),
        ("подпись транзакции не принимается за ключ",
         scan_text("5YWg3g588LtzkDFKAbvK4cefS8KujAN5S9aN3irq6KCmHpfJgWuYj1ywAX4TbXejRmBbT9mur2dYdACD1LXGvTjf",
                   []) == []),
    ]
    bad = 0
    for name, ok in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}")
        bad += (not ok)
    print(f"самопроверка c2_leak_scan: {len(checks) - bad}/{len(checks)} пройдено")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
