#!/usr/bin/env python3
"""Диагностика сторожа: признак жизни и СЫРЫЕ строки позиций.

Зачем отдельный файл, а не heredoc в шаге прогона. Вложенный heredoc
внутри "run: |" уже ломался: закрывающая метка идёт с отступом и
командная оболочка её не узнаёт. Файл в дереве ещё и проверяется
самотестом.

Зачем сама диагностика. Пустой seller.jsonl НИЧЕГО не объясняет: сторож
пишет туда только попытки продажи, UNSOLD и запрет рубильником, а все
ветки ожидания молчат. Без признака жизни и строк позиций "журнал пуст"
нельзя отличить от "сторож не видел позиции" и от "остаток не читается".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def собрать(база: Path) -> dict:
    вых: dict = {"state_dir": str(база), "heartbeat": None,
                 "heartbeat_why_not": None, "positions_rows": [],
                 "positions_why_not": None, "seller_rows": []}
    # Признак жизни ДЕТЕКТОРА тоже нужен рядом: без него "решения нет"
    # нельзя отличить от "сигнал не приходил" -- в нём счётчик увиденных
    # сигналов и обрывов подписки.
    dh = база / "detector_status.json"
    try:
        вых["detector_status"] = json.loads(dh.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        вых["detector_status_why_not"] = f"{type(exc).__name__}: {exc}"
    hb = база / "seller_heartbeat.json"
    try:
        вых["heartbeat"] = json.loads(hb.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        вых["heartbeat_why_not"] = f"{type(exc).__name__}: {exc}"
    for ключ, имя in (("positions_rows", "positions.jsonl"),
                      ("seller_rows", "seller.jsonl")):
        путь = база / имя
        if not путь.exists():
            if ключ == "positions_rows":
                вых["positions_why_not"] = f"{имя}: файла нет"
            continue
        try:
            текст = путь.read_text(encoding="utf-8")
        except OSError as exc:
            if ключ == "positions_rows":
                вых["positions_why_not"] = f"{type(exc).__name__}: {exc}"
            continue
        for line in текст.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                вых[ключ].append(json.loads(line))
            except ValueError:
                вых[ключ].append({"нечитаемая_строка": line[:200]})
    return вых


def self_test() -> None:
    import tempfile  # noqa: PLC0415
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    база = Path(tempfile.mkdtemp())
    пусто = собрать(база)
    chk("пустой каталог: признак жизни не прочитан, и это сказано",
        пусто["heartbeat"] is None and пусто["heartbeat_why_not"])
    chk("и про отсутствие позиций сказано отдельно",
        пусто["positions_why_not"] and "файла нет" in пусто["positions_why_not"])

    (база / "seller_heartbeat.json").write_text(
        json.dumps({"mode": "live", "positions_in_cycle": 1}), encoding="utf-8")
    (база / "positions.jsonl").write_text(
        json.dumps({"client_order_id": "c1", "state": "intent"}) + "\n"
        + "{битая\n"
        + json.dumps({"client_order_id": "c1", "state": "sent"}) + "\n",
        encoding="utf-8")
    d = собрать(база)
    chk("признак жизни прочитан", (d["heartbeat"] or {}).get("positions_in_cycle") == 1)
    chk("строки позиций все, включая битую", len(d["positions_rows"]) == 3)
    chk("битая строка помечена, а не выброшена молча",
        any("нечитаемая_строка" in r for r in d["positions_rows"]))
    chk("журнал сторожа отсутствует -- пустой список, без падения",
        d["seller_rows"] == [])

    (база / "seller.jsonl").write_text(json.dumps({"action": "продажа отправлена"}) + "\n",
                                       encoding="utf-8")
    chk("журнал сторожа читается, когда есть",
        len(собрать(база)["seller_rows"]) == 1)

    плохо = [c for c in checks if not c[1]]
    for n, ok, got in checks:
        print(f"{'OK ' if ok else 'НЕТ'} {n}{(' -- ' + str(got)) if got and not ok else ''}")
    print(f"итого {len(checks) - len(плохо)}/{len(checks)}")
    sys.exit(1 if плохо else 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default="/tmp/bloom_state")
    ap.add_argument("--out", default="data/bloom_seller_diag.json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return
    d = собрать(Path(a.state_dir))
    текст = json.dumps(d, ensure_ascii=False, indent=1)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(текст, encoding="utf-8")
    print(текст)


if __name__ == "__main__":
    main()
