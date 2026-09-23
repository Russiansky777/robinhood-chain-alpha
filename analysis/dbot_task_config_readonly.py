#!/usr/bin/env python3
"""Конфигурация задач DBot -- ТОЛЬКО ЧТЕНИЕ.

Зачем. В снимке репозитория (data/dbot_follow_orders_raw.json) от задачи
остались лишь id/name/sources/wallet/enabled: остальное учёт выбрасывал при
разборе. Чтобы ответить, задан ли в задаче конкретный пул (поле pair) или
пул выбирает сам DBot, нужна полная конфигурация.

ГРАНИЦЫ, заданные владельцем и закреплённые в коде:
  * единственный сетевой вызов -- HTTP GET на /automation/follow_orders;
  * никаких POST/PUT/PATCH/DELETE: методы записи здесь физически не
    вызываются, и самотест это проверяет по исходнику;
  * тела запроса нет вообще -- GET отправляется без payload;
  * скрипт ничего не меняет в задачах и не трогает торговлю.

GET на список задач у DBot -- операция чтения: изменение задачи идёт
другим путём (POST на одиночный ресурс), и этот скрипт его не знает.

Ключ API нигде не печатается.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "dbot_task_config_readonly.json"

DBOT_HOST = "https://api-bot-v1.dbotx.com"
READ_PATH = "/automation/follow_orders"

# Поля, ради которых всё и затевается: чем задан пул и как выбирается.
INTERESTING = re.compile(
    r"pair|pool|dex|amm|route|slippage|priorit|tip|gas|amount|maxSlippage|minAmount",
    re.I)
# Поля, значения которых не печатаем никогда.
SECRET_LIKE = re.compile(r"key|secret|token|password|mnemonic|private", re.I)


def scrub(text: str, key: str) -> str:
    return text.replace(key, "<КЛЮЧ>") if key else text


def read_tasks(api_key: str, timeout: int = 30) -> tuple[int | None, dict]:
    """ЕДИНСТВЕННЫЙ сетевой вызов скрипта: GET, без тела."""
    resp = requests.get(f"{DBOT_HOST}{READ_PATH}",
                        headers={"X-API-KEY": api_key}, timeout=timeout)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {"не_json": resp.text[:500]}


def items_of(body) -> list:
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    for k in ("res", "data", "items", "list", "tasks"):
        v = body.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k2 in ("list", "items", "data"):
                if isinstance(v.get(k2), list):
                    return v[k2]
    return []


def describe(task: dict) -> dict:
    """Что за задача и чем в ней задан пул. Секретоподобные поля -- только
    факт наличия, без значения."""
    out = {"id": task.get("id") or task.get("_id"),
           "name": task.get("name"),
           "wallet": task.get("walletAddress") or task.get("wallet"),
           "enabled": task.get("enabled"),
           "все_поля": sorted(task.keys()),
           "поля_про_пул_и_исполнение": {},
           "скрытые_поля": []}
    for k, v in task.items():
        if SECRET_LIKE.search(k):
            out["скрытые_поля"].append(k)
            continue
        if INTERESTING.search(k):
            out["поля_про_пул_и_исполнение"][k] = v
    pair = task.get("pair") or task.get("pairAddress") or task.get("poolAddress")
    out["пул_задан_явно"] = bool(pair)
    out["пул"] = pair or None
    out["вывод"] = ("в задаче задан конкретный пул -- DBot не выбирает"
                    if pair else
                    "конкретный пул в задаче НЕ задан -- пул выбирает сам DBot")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return

    key = (os.environ.get("DBOT_API_KEY") or "").strip()
    if not key:
        raise SystemExit("DBOT_API_KEY пуст в окружении")
    print(f"[dbot] ТОЛЬКО ЧТЕНИЕ: GET {DBOT_HOST}{READ_PATH}, без тела запроса", flush=True)
    status, body = read_tasks(key)
    print(f"[dbot] код ответа: {status}", flush=True)

    tasks = items_of(body)
    out = {
        "запрошено_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "метод": "GET", "путь": READ_PATH, "тело_запроса": None,
        "код_ответа": status,
        "ГРАНИЦЫ": [
            "Единственный сетевой вызов -- GET на список задач.",
            "POST/PUT/PATCH/DELETE в этом скрипте не вызываются вообще.",
            "Ничего в задачах не меняется, торговля не трогается.",
            "Значения полей, похожих на ключи и секреты, не печатаются -- только имена.",
        ],
        "задач": len(tasks),
        "задачи": [describe(t) for t in tasks if isinstance(t, dict)],
    }
    if not tasks:
        out["почему_пусто"] = ("ответ не содержит списка задач; сырой ответ сохранён "
                               "обрезанным для разбора")
        out["сырой_ответ_обрезан"] = scrub(json.dumps(body, ensure_ascii=False)[:1500], key)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    for t in out["задачи"]:
        print(f"  {t['name']}: пул_задан_явно={t['пул_задан_явно']} "
              f"поля_про_пул={list(t['поля_про_пул_и_исполнение'])}", flush=True)
    print(json.dumps({"задач": out["задач"], "код": status}, ensure_ascii=False), flush=True)


def self_test() -> None:
    checks = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    # Границы доказываются по ИСХОДНИКУ, а не обещанием в комментарии.
    src = Path(__file__).read_text()
    body = src.split("def self_test()")[0]
    for bad in ("requests.post", "requests.put", "requests.patch", "requests.delete"):
        chk(f"в коде нет {bad}", bad not in body, bad)
    chk("сетевой вызов ровно один", body.count("requests.get") == 1,
        str(body.count("requests.get")))
    chk("GET уходит без тела", "json=" not in body.split("def read_tasks")[1].split("def ")[0])

    t = {"id": "T1", "name": "BATCH-5", "walletAddress": "W", "enabled": True,
         "pair": "POOL123", "maxSlippage": 0.2, "apiKey": "СЕКРЕТ", "targetIds": ["A"]}
    d = describe(t)
    chk("явно заданный пул виден", d["пул_задан_явно"] and d["пул"] == "POOL123", str(d["пул"]))
    chk("вывод сформулирован", "не выбирает" in d["вывод"])
    chk("поля про исполнение собраны", "maxSlippage" in d["поля_про_пул_и_исполнение"])
    chk("секретоподобное поле не раскрыто",
        "apiKey" in d["скрытые_поля"] and "apiKey" not in d["поля_про_пул_и_исполнение"])
    chk("значение секрета нигде не всплыло", "СЕКРЕТ" not in json.dumps(d, ensure_ascii=False))

    d2 = describe({"id": "T2", "name": "BATCH-6"})
    chk("без пула -- прямо сказано, что выбирает DBot",
        d2["пул_задан_явно"] is False and "выбирает сам DBot" in d2["вывод"])

    chk("список задач достаётся из res", items_of({"res": [{"id": 1}]}) == [{"id": 1}])
    chk("и из вложенного list", items_of({"res": {"list": [{"id": 2}]}}) == [{"id": 2}])
    chk("мусор не превращается в задачи", items_of({"err": True}) == [])
    chk("ключ вычищается", scrub("k=SEK", "SEK") == "k=<КЛЮЧ>")

    bad = 0
    for n, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка границ чтения: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    main()
