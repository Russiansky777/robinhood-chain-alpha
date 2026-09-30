#!/usr/bin/env python3
"""Подбивка на бегунке lab-miami: все запуски маркеров data/podbivka/zapusk/miami_*.json -- одним заданием,
параллельными процессами (по суткам архива), часы PumpApi -- с локального диска (PODB_ARHIV_KESH).

Маркер -- как zadacha_*.json: {"skript", "zapuski": [[args]...], "helius_rps_na_paket", "zachem"} и по желанию
"parallelno" (процессов одновременно; по умолчанию min(запусков, 5) -- ядер 6, одно остаётся бегунку).
Ключи Helius / Shyft отдаются процессу, только если helius_rps_na_paket > 0 (архиву PumpApi они не нужны).
Предел Helius -- суммарно: helius_rps_na_paket × одновременных процессов ≤ 6 (слово владельца 30.09) -- проверяется до старта.
После прогона -- чистка кэша до PODB_KESH_GB (по умолчанию 80) по давности чтения.
Выход: код 1, если хоть один запуск упал; файлы, записанные запусками, -- в PODB_MANIFEST (коммитит шаг workflow).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
КЛЮЧИ = ("HELIUS_API_KEY", "HELIUS_API_KEY2", "SHYFT_API_KEY")
ПОТОЛОК_HELIUS = 6.0          # запросов/с суммарно на все процессы Code-2 (слово владельца 30.09)


def запуски(маркеры: list) -> list:
    из_ = []
    for путь in маркеры:
        м = json.loads(Path(путь).read_text(encoding="utf-8"))
        rps = float(м.get("helius_rps_na_paket") or 0)
        пар = int(м.get("parallelno") or min(len(м["zapuski"]), 5))
        if rps * пар > ПОТОЛОК_HELIUS:
            raise SystemExit(f"{путь}: Helius {rps} × {пар} процессов > {ПОТОЛОК_HELIUS:g} запросов/с суммарно")
        for н, арг in enumerate(м["zapuski"]):
            из_.append({"имя": f"{Path(путь).stem}_{н}", "skript": м["skript"], "args": [str(x) for x in арг],
                        "rps": rps, "пар": пар})
    return из_


def один(з: dict, логи: Path) -> tuple:
    окр = dict(os.environ)
    if з["rps"] > 0:
        окр["PODB_HELIUS_RPS"] = str(з["rps"])
    else:
        for к in КЛЮЧИ:
            окр.pop(к, None)
    лог = логи / f"{з['имя']}.log"
    т0 = time.time()
    with open(лог, "w", encoding="utf-8") as ф:
        код = subprocess.run([sys.executable, str(КОРЕНЬ / "analysis" / з["skript"]), *з["args"]], cwd=КОРЕНЬ,
                             env=окр, stdout=ф, stderr=subprocess.STDOUT).returncode
    return з["имя"], код, round((time.time() - т0) / 60, 1), лог


def чистка(кэш: Path, предел_гб: float) -> None:
    файлы = sorted((p for p in кэш.rglob("*.jsonl.zst")), key=lambda p: p.stat().st_mtime)
    всего = sum(p.stat().st_size for p in файлы)
    убрано = 0
    while файлы and всего > предел_гб * 1e9:
        p = файлы.pop(0)
        всего -= p.stat().st_size
        p.unlink()
        убрано += 1
    print(f"кэш: {len(файлы)} часов, {всего / 1e9:.1f} ГБ (предел {предел_гб} ГБ), убрано {убрано}", flush=True)


def main() -> int:
    маркеры = [m for m in sys.argv[1:] if m.endswith(".json") and Path(m).exists()]
    зз = запуски(маркеры)
    if not зз:
        print("маркеров miami_*.json в последнем коммите нет")
        return 0
    логи = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "podb_logi"
    логи.mkdir(parents=True, exist_ok=True)
    пар = min(з["пар"] for з in зз)
    print(f"запусков {len(зз)}, одновременно {пар}: " + "; ".join(f"{з['имя']}: {з['skript']} {shlex.join(з['args'])}" for з in зз), flush=True)
    упало = 0
    with cf.ThreadPoolExecutor(max_workers=пар) as пул:
        for имя, код, мин, лог in (ф.result() for ф in cf.as_completed([пул.submit(один, з, логи) for з in зз])):
            упало += код != 0
            print(f"=== {имя}: код {код}, {мин} мин; хвост журнала:", flush=True)
            print("".join(open(лог, encoding="utf-8", errors="replace").readlines()[-15:]), flush=True)
    if os.environ.get("PODB_ARHIV_KESH"):
        чистка(Path(os.environ["PODB_ARHIV_KESH"]), float(os.environ.get("PODB_KESH_GB") or 80))
    print(f"итог: запусков {len(зз)}, упало {упало}", flush=True)
    return 1 if упало else 0


if __name__ == "__main__":
    raise SystemExit(main())
