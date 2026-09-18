#!/usr/bin/env python3
"""Владелец, 2026-09-18: сравнить конфигурацию пилотной copy-trade задачи
(один кошелёк Beqv6dz...) с новой (40+ кошельков) через DBot API.

Хост/заголовок подтверждены реальными вызовами ранее в этой сессии:
https://api-bot-v1.dbotx.com, x-api-key. Эндпоинт списка задач тоже уже
подтверждён реальным вызовом ранее (GET /automation/follow_orders --
несмотря на название, отдаёт КОНФИГ задач, не построчные ордера) --
но владелец явно попросил честно перепробовать альтернативные
правдоподобные пути ПЕРЕД тем, как опираться на уже известный, поэтому
пробуем все, логируя код и превью.

Только чтение -- GET-запросы. Никаких изменений настроек."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_OUT_PATH = REPO_ROOT / "data" / "dbot_tasks_config_raw.json"
DIFF_OUT_PATH = REPO_ROOT / "data" / "dbot_tasks_config_diff.json"

DBOT_HOST = "https://api-bot-v1.dbotx.com"
PILOT_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"

CANDIDATE_TASK_ENDPOINTS = [
    "/automation/copy_trades",
    "/automation/copy_trade_tasks",
    "/automation/tasks",
    "/automation/follow_tasks",
    "/automation/strategies",
    "/automation/follow_orders",  # уже подтверждён реальным вызовом ранее в этой сессии
]

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED_SECRET]")
    return text


def dbot_get(path: str, api_key: str, params: dict | None = None) -> dict:
    if any(c in api_key for c in ("\n", "\r")):
        return {"exception": "ключ содержит перевод строки -- не отправляю."}
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


def flatten(d: dict, prefix: str = "") -> dict:
    """Плоский словарь path->value для честного дифа вложенных настроек
    (buySettings.*, sellSettings.* и т.д.)."""
    out = {}
    for k, v in (d or {}).items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, path))
        elif isinstance(v, list):
            out[path] = v  # списки не разворачиваем -- сравниваем целиком (напр. targetIds)
        else:
            out[path] = v
    return out


# Единицы полей -- ТОЛЬКО те, что реально подтверждены владельцем в этой
# сессии (см. диалог: "pnlOrderExpireDelta -- МИЛЛИСЕКУНДЫ... stopEarnPercent
# -- ДОЛЯ, т.е. +10000%..."). Для остального -- не гадаем, смотрим по факту
# значения и имени поля (суффикс UI = человеко-читаемые единицы, обычно SOL,
# подтверждено тем, что владелец сам задавал targetMinAmountUI=5 как "5 SOL").
KNOWN_UNITS = {
    "sellSettings.pnlOrderExpireDelta": lambda v: f"{v} мс = {v / 1000:.1f} с" if isinstance(v, (int, float)) else None,
    "buySettings.maxSlippage": lambda v: f"{v * 100:.0f}%" if isinstance(v, (int, float)) else None,
    "sellSettings.maxSlippage": lambda v: f"{v * 100:.0f}%" if isinstance(v, (int, float)) else None,
    "sellSettings.stopEarnPercent": lambda v: f"+{v * 100:.0f}%" if isinstance(v, (int, float)) else None,
    "sellSettings.stopLossPercent": lambda v: f"{(v - 1) * 100:.0f}%" if isinstance(v, (int, float)) and v < 1
    else (f"-{v * 100:.0f}%" if isinstance(v, (int, float)) else None),
}


def interpret(path: str, value) -> str | None:
    if path in KNOWN_UNITS:
        try:
            return KNOWN_UNITS[path](value)
        except Exception:  # noqa: BLE001
            return None
    if path.endswith("UI") and isinstance(value, (int, float)):
        return f"{value} (суффикс UI -- человеко-читаемые единицы, подтверждено владельцем ранее: это SOL, не лампорты)"
    return None


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    api_key = os.environ.get("DBOT_API_KEY", "")
    if not api_key:
        out["HONEST_ANSWER"] = "DBOT_API_KEY пуст."
        RAW_OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print("[dbot_tasks] " + out["HONEST_ANSWER"], flush=True)
        return
    if not any(c in api_key for c in ("\n", "\r")):
        _ACTIVE_SECRETS.append(api_key)

    # ---------- Шаг 1: честно перебираем кандидатов эндпоинта ----------
    out["endpoint_probes"] = {}
    tasks_body = None
    working_endpoint = None
    for path in CANDIDATE_TASK_ENDPOINTS:
        r = dbot_get(path, api_key, params={"chain": "solana"})
        preview = json.dumps(r.get("body"), default=str)[:500] if not r.get("exception") else r.get("exception")
        out["endpoint_probes"][path] = {"http_status": r.get("http_status"), "body_preview": _scrub_all(preview)}
        print(f"[dbot_tasks] {path} -> http={r.get('http_status')} preview={_scrub_all(preview)[:150]}", flush=True)
        if r.get("http_status") == 200 and isinstance(r.get("body"), dict) and r["body"].get("res"):
            if tasks_body is None:
                tasks_body = r["body"]
                working_endpoint = path
        time.sleep(0.3)

    if tasks_body is None:
        out["HONEST_ANSWER"] = "Ни один кандидат не вернул непустой список задач -- см. endpoint_probes. Официальные доки не выкачаны (сеть на dbotx.com из песочницы этой сессии заблокирована прокси, подтверждено ранее)."
        print("[dbot_tasks] " + out["HONEST_ANSWER"], flush=True)
        RAW_OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
        return

    out["working_endpoint"] = working_endpoint
    tasks = tasks_body.get("res") or []
    out["n_tasks_total"] = len(tasks)
    out["all_tasks_raw"] = tasks
    print(f"[dbot_tasks] рабочий эндпоинт: {working_endpoint}, задач: {len(tasks)}", flush=True)

    # ---------- Шаг 2: список кошельков задачи (баланс -- отдельный эндпоинт) ----------
    r_wallets = dbot_get("/automation/wallets", api_key)
    out["wallets_raw"] = r_wallets
    print(f"[dbot_tasks] /automation/wallets -> http={r_wallets.get('http_status')}", flush=True)

    RAW_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAW_OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
    print(f"[dbot_tasks] сырые данные записаны в {RAW_OUT_PATH}", flush=True)

    # ---------- Шаг 3: найти пилотную и новую задачу ----------
    pilot_task = None
    new_task = None
    for t in tasks:
        target_ids = t.get("targetIds") or []
        if target_ids == [PILOT_WALLET] or (len(target_ids) == 1 and target_ids[0] == PILOT_WALLET):
            pilot_task = t
        elif len(target_ids) > 1:
            new_task = t
    diff_out: dict = {"generated_at_utc": out["generated_at_utc"], "n_tasks_total": len(tasks)}
    diff_out["pilot_task_found"] = pilot_task is not None
    diff_out["new_task_found"] = new_task is not None
    if not pilot_task or not new_task:
        diff_out["HONEST_ANSWER"] = (
            f"pilot_task_found={pilot_task is not None}, new_task_found={new_task is not None} -- "
            f"не нашёл обе задачи по критерию (targetIds==[пилот] / len(targetIds)>1). Все задачи -- "
            f"в data/dbot_tasks_config_raw.json:all_tasks_raw, посмотри руками."
        )
        print("[dbot_tasks] " + diff_out["HONEST_ANSWER"], flush=True)
        DIFF_OUT_PATH.write_text(_scrub_all(json.dumps(diff_out, ensure_ascii=False, indent=2, default=str)))
        return

    # ---------- Шаг 4: детали новой задачи ----------
    new_target_ids = new_task.get("targetIds") or []
    diff_out["new_task_details"] = {
        "id": new_task.get("id"),
        "n_copy_wallets_actual": len(new_target_ids),
        "copy_wallets_full_list": new_target_ids,
        "enabled": new_task.get("enabled"),
        "name": new_task.get("name"),
        "walletId": new_task.get("walletId"),
        "walletAddress": new_task.get("walletAddress"),
        "simulation_related_fields": {k: v for k, v in new_task.items()
                                       if any(w in k.lower() for w in ("sim", "mode", "test", "paper", "dry"))},
    }
    print(f"[dbot_tasks] новая задача: {len(new_target_ids)} адресов в targetIds, "
          f"simulation_related_fields={diff_out['new_task_details']['simulation_related_fields']}", flush=True)

    # Баланс кошелька задачи -- из /automation/wallets, если там есть.
    wallets_list = (r_wallets.get("body") or {}).get("res") or [] if isinstance(r_wallets.get("body"), dict) else []
    matching_wallet = next((w for w in wallets_list if w.get("id") == new_task.get("walletId")
                             or w.get("address") == new_task.get("walletAddress")), None)
    diff_out["new_task_wallet_info"] = matching_wallet or {"HONEST_ANSWER": "не нашёл кошелёк задачи в /automation/wallets по walletId/address"}

    # ---------- Шаг 5: диф настроек ----------
    flat_pilot = flatten(pilot_task)
    flat_new = flatten(new_task)
    all_keys = set(flat_pilot) | set(flat_new)
    diffs = []
    only_pilot = []
    only_new = []
    for k in sorted(all_keys):
        in_pilot, in_new = k in flat_pilot, k in flat_new
        if in_pilot and not in_new:
            only_pilot.append({"field": k, "pilot_value": flat_pilot[k]})
            continue
        if in_new and not in_pilot:
            only_new.append({"field": k, "new_value": flat_new[k]})
            continue
        if flat_pilot[k] != flat_new[k]:
            diffs.append({
                "field": k,
                "pilot_raw": flat_pilot[k], "pilot_human": interpret(k, flat_pilot[k]),
                "new_raw": flat_new[k], "new_human": interpret(k, flat_new[k]),
            })
    diff_out["fields_that_differ"] = diffs
    diff_out["fields_only_in_pilot"] = only_pilot
    diff_out["fields_only_in_new"] = only_new
    print(f"[dbot_tasks] различий: {len(diffs)}, только у пилота: {len(only_pilot)}, только у новой: {len(only_new)}", flush=True)

    # ---------- Шаг 6: активность за 24ч по новой задаче ----------
    now = int(time.time())
    day_ago = now - 86400
    activity = {}
    for path in ["/automation/follow_orders", "/automation/swap_orders"]:
        # Пробуем несколько правдоподобных наборов параметров честно -- не знаем заранее,
        # какой фильтр по времени/кошельку API реально принимает.
        for params in [
            {"chain": "solana", "walletId": new_task.get("walletId")},
            {"chain": "solana", "walletAddress": new_task.get("walletAddress")},
            {"chain": "solana", "startTime": day_ago, "endTime": now},
        ]:
            r = dbot_get(path, api_key, params=params)
            body = r.get("body")
            items = body.get("res") if isinstance(body, dict) else None
            key = f"{path}?{'&'.join(f'{k}={v}' for k, v in params.items() if v is not None)}"
            activity[key] = {"http_status": r.get("http_status"),
                              "n_items": len(items) if isinstance(items, list) else None,
                              "body_preview": _scrub_all(json.dumps(body, default=str)[:400])}
            print(f"[dbot_tasks] 24ч активность {key} -> http={r.get('http_status')} n={activity[key]['n_items']}", flush=True)
            time.sleep(0.3)
    diff_out["last_24h_activity_probes"] = activity

    # Честная сводка по первому непустом наборе (если такой есть).
    non_empty = next((v for v in activity.values() if v.get("n_items")), None)
    if non_empty:
        diff_out["last_24h_summary"] = f"найдены записи -- см. last_24h_activity_probes для деталей (успех/неуспех нужно разбирать по полю status внутри записей)."
    else:
        diff_out["last_24h_summary"] = "ноль -- ни один из опробованных наборов параметров не вернул записей за 24ч по новой задаче."

    DIFF_OUT_PATH.write_text(_scrub_all(json.dumps(diff_out, ensure_ascii=False, indent=2, default=str)))
    print(f"[dbot_tasks] диф записан в {DIFF_OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
