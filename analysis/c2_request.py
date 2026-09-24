#!/usr/bin/env python3
"""C2: параметры прогона -> строки NAME=value для $GITHUB_ENV.

Источник параметров: при workflow_dispatch -- переменные IN_* (входы
формы, переданные через env, а не подстановкой в shell); при push --
файл-заявка data/c2_requests/run.json. Каждое значение проверяется по
белому списку символов: в $GITHUB_ENV не попадёт ничего, кроме того, что
можно безопасно подставить в командную строку. Имена -- только ASCII.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REQ = Path(__file__).resolve().parent.parent / "data" / "c2_requests" / "run.json"
TASKS = ("crowd", "sandwich", "both", "selftest", "probe")
ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SIG = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")
UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DEFAULTS = {"task": "both", "days": "7", "only": "", "limit_sources": "0", "workers": "4",
            "time_budget_s": "15000", "crowd_cap": "1500",
            "sigs": "", "mint": "", "from_utc": "", "to_utc": ""}


def validate(raw: dict) -> dict:
    v = {k: str(raw.get(k, d) if raw.get(k) not in (None, "") else d).strip()
         for k, d in DEFAULTS.items()}
    if v["task"] not in TASKS:
        raise ValueError(f"task: одно из {TASKS}")
    days = float(v["days"])
    if not 0 < days <= 14:
        raise ValueError("days: (0, 14]")
    only = [a.strip() for a in v["only"].split(",") if a.strip()]
    if any(not ADDR.match(a) for a in only):
        raise ValueError("only: только base58-адреса через запятую")
    for k, lo, hi in (("limit_sources", 0, 100), ("workers", 1, 8),
                      ("time_budget_s", 60, 19000), ("crowd_cap", 10, 20000)):
        n = int(v[k])
        if not lo <= n <= hi:
            raise ValueError(f"{k}: [{lo}, {hi}]")
        v[k] = str(n)
    v["days"] = str(days)
    v["only"] = ",".join(only)
    sigs = [x.strip() for x in v["sigs"].split(",") if x.strip()]
    if any(not SIG.match(x) for x in sigs) or len(sigs) > 10:
        raise ValueError("sigs: до 10 подписей base58 через запятую")
    v["sigs"] = ",".join(sigs)
    if v["mint"] and not ADDR.match(v["mint"]):
        raise ValueError("mint: base58-адрес")
    for k in ("from_utc", "to_utc"):
        if v[k] and not UTC.match(v[k]):
            raise ValueError(f"{k}: формат 2026-09-24T14:10:00Z")
    return v


def env_lines(v: dict) -> list:
    return [f"C2_{k.upper()}={val}" for k, val in v.items()]


def main(argv: list) -> int:
    if "--self-test" in argv:
        return self_test()
    event = argv[argv.index("--event") + 1] if "--event" in argv else ""
    if event == "workflow_dispatch":
        raw = {k: os.environ.get(f"IN_{k.upper()}", "") for k in DEFAULTS}
    else:
        raw = json.loads(REQ.read_text(encoding="utf-8")) if REQ.exists() else {}
    try:
        v = validate(raw)
    except (ValueError, TypeError) as exc:
        print(f"заявка отклонена: {exc}", file=sys.stderr)
        return 1
    for line in env_lines(v):
        print(line)
    return 0


def self_test() -> int:
    checks = []
    v = validate({})
    checks.append(("по умолчанию both / 7 суток", v["task"] == "both" and v["days"] == "7.0"))
    for bad in ({"task": "rm -rf"}, {"days": "30"}, {"only": "abc; echo x"}, {"workers": "99"}):
        try:
            validate(bad)
            checks.append((f"отклонено {bad}", False))
        except (ValueError, TypeError):
            checks.append((f"отклонено {list(bad)[0]}", True))
    try:
        validate({"task": "probe", "sigs": "abc;rm"})
        checks.append(("отклонена кривая подпись", False))
    except ValueError:
        checks.append(("отклонена кривая подпись", True))
    vp = validate({"task": "probe", "sigs": "5" * 88, "mint": "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit",
                   "from_utc": "2026-09-24T14:10:00Z", "to_utc": "2026-09-24T14:12:00Z"})
    checks.append(("заявка probe принята", vp["task"] == "probe" and vp["from_utc"].endswith("Z")))
    v = validate({"task": "crowd", "only": "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"})
    checks.append(("адрес в only принят", v["only"].startswith("Beqv6")))
    checks.append(("имена переменных -- ASCII",
                   all(re.match(r"^[A-Z_][A-Z0-9_]*=", ln) for ln in env_lines(v))))
    bad_n = 0
    for name, ok in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}")
        bad_n += (not ok)
    print(f"самопроверка c2_request: {len(checks) - bad_n}/{len(checks)} пройдено")
    return 0 if bad_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
