#!/usr/bin/env python3
"""Владелец, 2026-09-18 (обновлено): ДВЕ copy-trade задачи копитрейдинга
поверх боевого пилота -- BATCH-1 и BATCH-2, по 10 кошельков каждая,
отдельные кошельки, по 3 SOL. Настройки одинаковые: вход фикс 0.1 SOL,
Copy Buy Range минимум 2 SOL, Total Buy Times 2, Only Buy Once выкл,
Skip Added Tokens вкл, slippage 35%, priority fee 0.005, TP/SL Expiry
0.008ч=28.8с, автопродажа. Пилот (mu746r8f05f9ic) -- контроль, отдельно.

Снимаем нулевую точку ПО ОБЕИМ задачам разом: id, имя (как есть в поле
name -- BATCH-1/BATCH-2, не переименовываем), адрес кошелька, баланс
SOL, список отслеживаемых адресов с ремарками (используем ремарки DBot
как есть, чтобы совпадали при расширении списка), полный конфиг,
метка времени UTC.

Хост/заголовок/эндпоинт -- те же, что уже подтверждены реальными
вызовами в этой сессии: https://api-bot-v1.dbotx.com, x-api-key,
GET /automation/follow_orders.

Только чтение -- GET-запросы + по одному getBalance на кошелёк. Никаких
изменений настроек задач."""
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
    гадаем формат заранее."""
    hits = {}
    for k, v in task.items():
        lk = k.lower()
        if any(w in lk for w in ("remark", "name", "note", "nick", "label", "alias", "tag")):
            hits[k] = v
    return hits


def wallet_balance_sol(addr: str | None) -> tuple[float | None, int | None, str | None]:
    if not addr:
        return None, None, "у задачи нет walletAddress в конфиге"
    try:
        bal = fp.rpc_call("getBalance", [addr], use_cache=False)
        lamports = bal.get("value") if isinstance(bal, dict) else None
        return (lamports / 1e9 if isinstance(lamports, (int, float)) else None), lamports, None
    except RuntimeError as exc:
        return None, None, str(exc)[:300]


def snapshot_batch(task: dict) -> dict:
    target_ids = task.get("targetIds") or []
    target_names = task.get("targetNames") or []
    tracked = [{"address": a, "remark": (target_names[i] if i < len(target_names) else None)}
               for i, a in enumerate(target_ids)]
    wallet_addr = task.get("walletAddress")
    bal_sol, bal_lamports, bal_err = wallet_balance_sol(wallet_addr)
    return {
        "task_id": task.get("id"),
        "task_name": task.get("name"),
        "enabled": task.get("enabled"),
        "wallet_address": wallet_addr,
        "wallet_id": task.get("walletId"),
        "wallet_sol_balance": bal_sol,
        "wallet_sol_balance_raw_lamports": bal_lamports,
        "wallet_sol_balance_error": bal_err,
        "n_tracked_wallets": len(target_ids),
        "tracked_wallets": tracked,
        "name_fields_found_in_task_config": find_name_fields(task),
        "full_config": task,
    }


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
    out["pilot_task_full_config_for_reference"] = pilot_task
    out["n_non_pilot_tasks"] = len(non_pilot_tasks)

    batches: dict[str, dict] = {}
    unclassified: list[dict] = []
    for t in non_pilot_tasks:
        name = t.get("name")
        if name:
            batches[name] = snapshot_batch(t)
        else:
            unclassified.append(t)
        print(f"[sieve_baseline] задача '{name}' (id={t.get('id')}): "
              f"{len(t.get('targetIds') or [])} отслеживаемых, кошелёк={t.get('walletAddress')}", flush=True)

    out["batches"] = batches
    if unclassified:
        out["unclassified_non_pilot_tasks_no_name"] = unclassified
        print(f"[sieve_baseline] ВНИМАНИЕ: {len(unclassified)} не-пилотных задач без поля name -- "
              "не смог классифицировать как BATCH-N, см. unclassified_non_pilot_tasks_no_name", flush=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
    print(f"[sieve_baseline] записано в {OUT_PATH}: батчи={list(batches.keys())}", flush=True)


if __name__ == "__main__":
    main()
