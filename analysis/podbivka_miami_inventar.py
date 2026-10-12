#!/usr/bin/env python3
"""Что лежит на бегунке lab-miami: кэш часов PumpApi, место на диске, venv.

Зачем. Для бэкапа лаборатории нужно знать, что есть ТОЛЬКО на lab-miami и сколько это
занимает: кэш часов архива ($PODB_ARHIV_KESH) живёт на диске бегунка и в ветку не кладётся
(часы по 0.47 ГБ -- в git им места нет). Из контейнера этого не видно: журналы прогонов
отдаются с хоста, который закрыт сетевой политикой. Поэтому опись снимает сам бегунок.

Только чтение: ни одного файла не меняет и не удаляет. Ключей не просит.
Выход: data/podbivka/miami_inventar.json.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from collections import defaultdict
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"


def главное() -> int:
    кэш = Path(os.environ.get("PODB_ARHIV_KESH") or (Path.home() / "arhiv_pumpapi"))
    по_суткам: dict = defaultdict(lambda: {"часов": 0, "байт": 0})
    всего = 0
    часов = 0
    самый_старый = самый_новый = None
    if кэш.is_dir():
        for ф in кэш.rglob("*.jsonl.zst"):
            с = ф.stat()
            # путь вида <кэш>/2026/10/08/13.jsonl.zst либо 2026-10-08T13.jsonl.zst
            ч = ф.relative_to(кэш).as_posix()
            сут = ч.rsplit("/", 1)[0] if "/" in ч else ф.stem[:10]
            по_суткам[сут]["часов"] += 1
            по_суткам[сут]["байт"] += с.st_size
            всего += с.st_size
            часов += 1
            самый_старый = min(самый_старый or с.st_mtime, с.st_mtime)
            самый_новый = max(самый_новый or с.st_mtime, с.st_mtime)

    диск = shutil.disk_usage(str(Path.home()))
    venv = Path.home() / "pod_venv"
    венв_байт = sum(p.stat().st_size for p in venv.rglob("*") if p.is_file()) if venv.is_dir() else 0

    прочее = {}
    for имя in ("arhiv_pumpapi", "pod_venv", "actions-runner", "_work"):
        п = Path.home() / имя
        if п.exists():
            try:
                прочее[имя] = int(subprocess.run(["du", "-sb", str(п)], capture_output=True,
                                                 text=True, timeout=600).stdout.split()[0])
            except Exception as e:  # noqa: BLE001
                прочее[имя] = f"{type(e).__name__}"

    тело = {
        "что": "опись бегунка lab-miami: кэш часов PumpApi, место, venv",
        "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "машина": {"hostname": socket.gethostname(), "ядер": os.cpu_count(),
                   "дом": str(Path.home())},
        "диск_гб": {"всего": round(диск.total / 1e9, 1), "занято": round(диск.used / 1e9, 1),
                    "свободно": round(диск.free / 1e9, 1)},
        "кэш": {"путь": str(кэш), "есть": кэш.is_dir(), "часов": часов,
                "гб": round(всего / 1e9, 2),
                "предел_гб": os.environ.get("PODB_KESH_GB"),
                "самый_старый_файл": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(самый_старый))
                                      if самый_старый else None),
                "самый_новый_файл": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(самый_новый))
                                     if самый_новый else None),
                "по_суткам": {к: {"часов": v["часов"], "гб": round(v["байт"] / 1e9, 2)}
                              for к, v in sorted(по_суткам.items())}},
        "venv_гб": round(венв_байт / 1e9, 2),
        "каталоги_дома_байт": прочее,
    }
    ф = П / "miami_inventar.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import sys  # noqa: PLC0415
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    print(json.dumps(тело, ensure_ascii=False, indent=1), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(главное())
