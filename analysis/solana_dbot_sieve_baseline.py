#!/usr/bin/env python3
"""Владелец, 2026-09-18: запущена ВТОРАЯ copy-trade задача DBot -- сито на
~10 кошельков, отдельный кошелёк, вход фикс 0.1 SOL, Copy Buy Range
минимум 2 SOL, Total Buy Times 2. Снять СЕЙЧАС (до первых сделок)
стартовое состояние -- нулевую точку для всей бухгалтерии сита:
ID задачи, адрес её кошелька, баланс SOL, список отслеживаемых
кошельков (с именами, если DBot их вообще отдаёт -- честно, не
выдумывать), полный конфиг задачи, метка времени UTC.

Хост/заголовок/рабочий эндпоинт конфигурации задач -- те же, что уже
подтверждены реальными вызовами в этой сессии (solana_dbot_tasks_config_diff.py):
https://api-bot-v1.dbotx.com, x-api-key, GET /automation/follow_orders.

Только чтение -- GET-запросы + один getBalance по RPC. Никаких изменений
настроек задачи."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "dbot_sieve_baseline.json"

DBOT_HOST = "https://api-bot-v1.dbotx.com"
PILOT_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
EXPECTED_TARGET_COUNT_RANGE = (5, 20)  # "сито на 10 кошельков" -- честный диапазон, не жёстко 10

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED_SECRET]")
    return text


def dbot_get(path: str, api_key: str, params: dict | None = None) -> dict:
    try:
        resp = requests.get(f"{DBOT_HOST}{path}", headers={"x-api-key": api_key, "Accept": "application/json"},
                             params=params or {}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"exception": _scrub_all(f"{type(exc).__name__}: {exc}")}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"non_json_body": _scrub_all(resp.text[:500])}
    return {"http_status": resp.status_code, "body": body}


def find_name_fields(task: dict) -> dict:
    """Честный поиск ЛЮБОГО поля, которое может содержать имена
    отслеживаемых кошельков (remark/name/note/nick/label и т.п.) -- не
    гадаем формат заранее, DBot ранее не документировал это в наших
    вызовах."""
    hits = {}
    for k, v in task.items():
        lk = k.lower()
        if any(w in lk for w in ("remark", "name", "note", "nick", "label", "alias", "tag")):
            hits[k] = v
    return hits


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    api_key = os.environ.get("DBOT_API_KEY", "")
    if not api_key:
        out["HONEST_ANSWER"] = "DBOT_API_KEY пуст -- снять базу не могу."
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print("[sieve_baseline] " + out["HONEST_ANSWER"], flush=True)
        return
    if not any(c in api_key for c in ("\n", "\r")):
        _ACTIVE_SECRETS.append(api_key)

    r = dbot_get("/automation/follow_orders", api_key, params={"chain": "solana"})
    body = r.get("body")
    if r.get("http_status") != 200 or not isinstance(body, dict) or not body.get("res"):
        out["HONEST_ANSWER"] = f"не удалось получить список задач: http={r.get('http_status')} body={_scrub_all(json.dumps(body, default=str)[:500])}"
        OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
        print("[sieve_baseline] " + out["HONEST_ANSWER"], flush=True)
        return

    tasks = body.get("res") or []
    out["n_tasks_total"] = len(tasks)
    print(f"[sieve_baseline] задач всего: {len(tasks)}", flush=True)

    pilot_task = None
    non_pilot_tasks = []
    for t in tasks:
        target_ids = t.get("targetIds") or []
        if target_ids == [PILOT_WALLET]:
            pilot_task = t
        else:
            non_pilot_tasks.append(t)

    out["pilot_task_found"] = pilot_task is not None
    out["n_non_pilot_tasks"] = len(non_pilot_tasks)

    # Честно выбираем кандидата на "сито": среди НЕ-пилотных задач --
    # ближайшую по числу targetIds к ожидаемому диапазону; если ровно
    # одна не-пилотная задача есть вообще -- берём её независимо от
    # диапазона (не гадаем лишний раз, когда выбора и так нет).
    sieve_task = None
    if len(non_pilot_tasks) == 1:
        sieve_task = non_pilot_tasks[0]
    else:
        lo, hi = EXPECTED_TARGET_COUNT_RANGE
        in_range = [t for t in non_pilot_tasks if lo <= len(t.get("targetIds") or []) <= hi]
        if len(in_range) == 1:
            sieve_task = in_range[0]

    out["all_non_pilot_tasks_summary"] = [
        {"id": t.get("id"), "name": t.get("name"), "enabled": t.get("enabled"),
         "n_targetIds": len(t.get("targetIds") or []), "walletAddress": t.get("walletAddress")}
        for t in non_pilot_tasks
    ]

    if sieve_task is None:
        out["HONEST_ANSWER"] = (
            f"не смог однозначно выбрать задачу сита среди {len(non_pilot_tasks)} не-пилотных -- "
            f"см. all_non_pilot_tasks_summary, выбери руками по id."
        )
        OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
        print("[sieve_baseline] " + out["HONEST_ANSWER"], flush=True)
        return

    target_ids = sieve_task.get("targetIds") or []
    out["sieve_task_id"] = sieve_task.get("id")
    out["sieve_task_wallet_address"] = sieve_task.get("walletAddress")
    out["sieve_task_wallet_id"] = sieve_task.get("walletId")
    out["n_tracked_wallets"] = len(target_ids)
    out["tracked_wallets_raw"] = target_ids

    name_fields = find_name_fields(sieve_task)
    out["name_fields_found_in_task_config"] = name_fields
    if not name_fields:
        out["names_honest_note"] = (
            "в конфиге задачи (follow_orders) НЕТ поля с именами отслеживаемых "
            "кошельков (проверены remark/name/note/nick/label/alias/tag) -- "
            "targetIds это просто список адресов, без имён. Если имена нужны, "
            "их надо брать из другого источника (напр. таблица с сита Fomo "
            "этой же сессии) и сопоставлять по адресу вручную."
        )
        print("[sieve_baseline] " + out["names_honest_note"], flush=True)

    # Баланс SOL кошелька задачи -- один честный RPC-вызов, без кэша
    # (баланс меняется).
    wallet_addr = sieve_task.get("walletAddress")
    if wallet_addr:
        try:
            bal = fp.rpc_call("getBalance", [wallet_addr], use_cache=False)
            lamports = bal.get("value") if isinstance(bal, dict) else None
            out["sieve_wallet_sol_balance"] = lamports / 1e9 if isinstance(lamports, (int, float)) else None
            out["sieve_wallet_sol_balance_raw_lamports"] = lamports
        except RuntimeError as exc:
            out["sieve_wallet_sol_balance_error"] = str(exc)[:300]
    else:
        out["sieve_wallet_sol_balance_error"] = "у задачи нет walletAddress в конфиге"

    out["sieve_task_full_config"] = sieve_task
    out["pilot_task_full_config_for_reference"] = pilot_task

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
    print(f"[sieve_baseline] записано в {OUT_PATH}: task_id={out.get('sieve_task_id')} "
          f"wallet={out.get('sieve_task_wallet_address')} n_tracked={out.get('n_tracked_wallets')} "
          f"balance_sol={out.get('sieve_wallet_sol_balance')}", flush=True)


if __name__ == "__main__":
    main()
