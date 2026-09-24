#!/usr/bin/env python3
"""C2: коммит выгрузок из прогона в СВОЮ ветку (ту, на которой идёт прогон).

Только пути внутри data/. Перед коммитом -- c2_leak_scan по тем же путям:
нашлась утечка -- коммита нет, код 1. Пуш с повтором и rebase на случай,
если в ветку за это время что-то пришло.

Ветка берётся из GITHUB_REF_NAME; в ветку Code-1 (claude/nifty-sagan-r0polg)
этот помощник не пушит никогда -- проверяется явно.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_BRANCHES = ("claude/nifty-sagan-r0polg", "claude/robinhood-copytrading-hypothesis-awjj0g",
                      "main", "master")


def run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, check=check, text=True, capture_output=True)


def main(argv: list) -> int:
    if "--self-test" in argv:
        return self_test()
    msg = argv[argv.index("-m") + 1]
    pats = [a for a in argv if not a.startswith("-") and a != msg]
    branch = os.environ.get("GITHUB_REF_NAME", "")
    if not branch or branch in FORBIDDEN_BRANCHES:
        print(f"СТОП: пуш в ветку «{branch}» C2 не делает")
        return 1
    files = sorted({f for p in pats for f in glob.glob(str(ROOT / p), recursive=True)})
    files = [f for f in files if Path(f).resolve().is_relative_to((ROOT / "data").resolve())]
    if not files:
        print("нечего коммитить: файлов по шаблонам нет")
        return 0
    leak = subprocess.run([sys.executable, str(ROOT / "analysis" / "c2_leak_scan.py"), *files],
                          cwd=ROOT, text=True, capture_output=True)
    print(leak.stdout.strip())
    if leak.returncode != 0:
        print("СТОП: в выгрузках найден ключ -- коммит не делается")
        return 1
    run("git", "config", "user.name", "github-actions[bot]")
    run("git", "config", "user.email", "github-actions[bot]@users.noreply.github.com")
    run("git", "add", "--", *files)
    if run("git", "diff", "--cached", "--quiet", check=False).returncode == 0:
        print("изменений нет")
        return 0
    run("git", "commit", "-m", msg)
    for i in range(5):
        r = run("git", "push", "origin", f"HEAD:{branch}", check=False)
        if r.returncode == 0:
            print(f"запушено в {branch}")
            return 0
        print(f"пуш не прошёл (попытка {i + 1}): {r.stderr.strip()[:200]}")
        run("git", "pull", "--rebase", "origin", branch, check=False)
        time.sleep(3 * (i + 1))
    return 1


def self_test() -> int:
    ok = all(b not in ("claude/c2-hedge-analysis",) for b in FORBIDDEN_BRANCHES)
    ok2 = "claude/nifty-sagan-r0polg" in FORBIDDEN_BRANCHES
    print(f"  [{'ok  ' if ok else 'СБОЙ'}] своя ветка не в запрете")
    print(f"  [{'ok  ' if ok2 else 'СБОЙ'}] ветка Code-1 в запрете")
    print(f"самопроверка c2_commit: {int(ok) + int(ok2)}/2 пройдено")
    return 0 if ok and ok2 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
