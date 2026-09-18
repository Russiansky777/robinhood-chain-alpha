#!/usr/bin/env python3
"""Владелец, 2026-09-18: интерфейс DBot не сохраняет 3 поля sellSettings
копитрейдинг-задачи (stopEarnPercent/stopLossPercent/pnlOrderExpireDelta)
-- владелец пробовал через UI, реального изменения не произошло (см.
data/solana_dbot_pilot_report.json: updateAt сдвинулся, но эти 3 числа
остались ровно прежними). Владелец явно попросил попробовать через API
напрямую.

ЭТО ЗАПИСЬ В ЖИВУЮ ТОРГОВУЮ КОНФИГУРАЦИЮ (не чтение) -- отдельный
скрипт от solana_dbot_pilot_report.py именно поэтому, не смешиваем.
Правим ТОЛЬКО 3 явно названных поля, оставляя buySettings и остальные
поля sellSettings как есть. Endpoint обновления НЕ подтверждён чтением
документации (docs.dbotx.com недоступен из песочницы) -- пробуем
несколько правдоподобных REST-путей по порядку, останавливаясь на
первом успешном (200), и ОБЯЗАТЕЛЬНО перечитываем задачу после КАЖДОЙ
попытки с http=200, чтобы честно увидеть, что реально изменилось --
не считаем 200 успехом без подтверждения читкой.

Целевые значения (владелец): stopEarnPercent=10000, stopLossPercent=null,
pnlOrderExpireDelta=29 (секунды -- подтверждено: 28800 в текущей
конфигурации = 8 часов, значит поле в секундах, не в часах, несмотря на
подпись в интерфейсе)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dbot_fix_sell_config_result.json"
DBOT_BASE = "https://api-bot-v1.dbotx.com"

TARGET_CHANGES = {
    "stopEarnPercent": 10000,
    "stopLossPercent": None,
    "pnlOrderExpireDelta": 29,
}
FIELDS_MUST_STAY = ["mode", "pnlOrderExpireExecute", "pnlOrderExpireExecuteSellAll"]


def dbot_call(method: str, path: str, api_key: str, json_body: dict | None = None, params: dict | None = None) -> dict:
    if any(c in api_key for c in ("\n", "\r")):
        raise RuntimeError("DBOT_API_KEY содержит перевод строки -- не отправляю как есть в заголовок.")
    try:
        resp = requests.request(method, f"{DBOT_BASE}{path}", headers={"x-api-key": api_key},
                                 json=json_body, params=params or {}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"exception": f"{type(exc).__name__}: {exc}"}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"non_json_body": resp.text[:1000]}
    return {"http_status": resp.status_code, "body": body}


def count_tasks(api_key: str) -> int | None:
    r = dbot_call("GET", "/automation/follow_orders", api_key, params={"chain": "solana"})
    if r.get("http_status") != 200:
        return None
    return len((r.get("body") or {}).get("res") or [])


def get_task(api_key: str) -> dict | None:
    r = dbot_call("GET", "/automation/follow_orders", api_key, params={"chain": "solana"})
    if r.get("http_status") != 200:
        return None
    res = (r.get("body") or {}).get("res") or []
    if not res:
        return None
    return res[0]


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    api_key = os.environ.get("DBOT_API_KEY", "")
    if not api_key:
        out["HONEST_ANSWER"] = "DBOT_API_KEY пуст."
        _finish(out)
        return

    before = get_task(api_key)
    if not before:
        out["HONEST_ANSWER"] = "Не удалось прочитать текущую задачу (0 задач или ошибка GET) -- правку не пробую."
        _finish(out)
        return
    out["before_sellSettings"] = before["sellSettings"]
    task_id, wallet_id = before["id"], before["walletId"]
    n_tasks_before = count_tasks(api_key)
    out["n_tasks_before"] = n_tasks_before
    print(f"[dbot_fix] Текущие sellSettings ДО правки: {before['sellSettings']} "
          f"(задач у пользователя: {n_tasks_before})", flush=True)

    new_sell_settings = dict(before["sellSettings"])
    new_sell_settings.update(TARGET_CHANGES)

    # Пробуем несколько правдоподобных путей обновления по очереди --
    # ни один не подтверждён документацией, честно логируем каждую
    # попытку целиком. Останавливаемся на первом http=200 И подтверждаем
    # реальное изменение повторным чтением -- не доверяем http=200 самому
    # по себе (DBot мог молча принять запрос, не применив поля -- см.
    # ровно этот же симптом с UI).
    attempts = []
    # НЕ пробуем голый POST на /automation/follow_orders (список/создание) --
    # по конвенции REST и формулировке доков ("используется чтобы получить
    # ВСЕ задачи пользователя" для GET) это похоже на эндпоинт СОЗДАНИЯ, не
    # обновления; отправка туда полного объекта существующей задачи рискует
    # создать ВТОРУЮ дублирующую живую задачу копитрейдинга вместо правки
    # первой -- удвоенное исполнение на каждый сигнал лидера. Пробуем ТОЛЬКО
    # пути, где id явно адресует конкретный существующий ресурс.
    candidates = [
        ("PUT", f"/automation/follow_orders/{task_id}", {"sellSettings": new_sell_settings}),
        ("POST", f"/automation/follow_orders/{task_id}", {"sellSettings": new_sell_settings}),
        ("PATCH", f"/automation/follow_orders/{task_id}", {"sellSettings": new_sell_settings}),
        ("POST", "/automation/follow_orders/update", {"id": task_id, "sellSettings": new_sell_settings}),
        ("POST", "/automation/follow_orders/edit", {"id": task_id, "sellSettings": new_sell_settings}),
    ]

    success = False
    for method, path, body in candidates:
        r = dbot_call(method, path, api_key, json_body=body)
        attempt_log = {"method": method, "path": path, "body_keys": list(body.keys()),
                        "http_status": r.get("http_status"),
                        "response_body_preview": json.dumps(r.get("body"), default=str)[:500],
                        "exception": r.get("exception")}
        print(f"[dbot_fix] Попытка {method} {path}: http={r.get('http_status')}", flush=True)
        if r.get("http_status") == 200:
            # Проверяем НЕМЕДЛЕННО -- не доверяем 200 без подтверждения.
            # В ПЕРВУЮ очередь: не создалась ли ВТОРАЯ задача (см.
            # докстринг -- главный риск POST на путь, похожий на "создание").
            n_tasks_after = count_tasks(api_key)
            attempt_log["n_tasks_after_this_attempt"] = n_tasks_after
            if n_tasks_after is not None and n_tasks_before is not None and n_tasks_after > n_tasks_before:
                attempt_log["DUPLICATE_TASK_CREATED"] = True
                attempts.append(attempt_log)
                out["CRITICAL_HONEST_ANSWER"] = (
                    f"СТОП: попытка {method} {path} подняла число задач копитрейдинга с "
                    f"{n_tasks_before} до {n_tasks_after} -- похоже, создалась дублирующая живая "
                    "задача вместо правки существующей. Дальше не пробую, ничего не удаляю "
                    "автоматически (удаление -- тоже запись, отдельное решение владельца). "
                    "Нужно вручную проверить и удалить дубликат через интерфейс DBot."
                )
                print("[dbot_fix] " + out["CRITICAL_HONEST_ANSWER"], flush=True)
                out["attempts"] = attempts
                out["success"] = False
                _finish(out)
                return
            after = get_task(api_key)
            attempt_log["confirmed_by_reread"] = after["sellSettings"] if after else None
            matches = after and all(after["sellSettings"].get(k) == v for k, v in TARGET_CHANGES.items())
            attempt_log["target_changes_actually_applied"] = bool(matches)
            attempts.append(attempt_log)
            if matches:
                success = True
                out["after_sellSettings"] = after["sellSettings"]
                print(f"[dbot_fix] ПОДТВЕРЖДЕНО повторным чтением: {after['sellSettings']}", flush=True)
                break
            print("[dbot_fix] http=200, но повторное чтение НЕ показывает нужных изменений -- "
                  "пробую следующий путь, не считаю это успехом.", flush=True)
        else:
            attempts.append(attempt_log)

    out["attempts"] = attempts
    out["success"] = success

    if not success:
        final = get_task(api_key)
        out["final_sellSettings_unchanged_check"] = final["sellSettings"] if final else None
        out["HONEST_ANSWER"] = (
            "НИ ОДНА из опробованных попыток обновления не привела к подтверждённому изменению "
            "stopEarnPercent/stopLossPercent/pnlOrderExpireDelta (см. attempts -- полные HTTP-статусы "
            "и тела ответов). Либо API этой задачи в принципе не поддерживает редактирование через "
            "опробованные пути, либо (как и в интерфейсе) поле pnlOrderExpireDelta=29 не проходит "
            "валидацию (возможен минимум, например 3600с/1ч). Конфигурация НЕ изменена. "
            "Рекомендация: возвращаемся к выходу по цене, как предложил владелец."
        )
        print("[dbot_fix] " + out["HONEST_ANSWER"], flush=True)
    else:
        # Проверяем, что ничего ЛИШНЕГО не поменялось -- покупка/остальные
        # поля продажи должны остаться как были.
        diffs_elsewhere = {}
        for k in FIELDS_MUST_STAY:
            if before["sellSettings"].get(k) != out["after_sellSettings"].get(k):
                diffs_elsewhere[k] = {"before": before["sellSettings"].get(k), "after": out["after_sellSettings"].get(k)}
        out["unexpected_side_effects_in_kept_fields"] = diffs_elsewhere or "нет -- остальные поля не тронуты"
        print(f"[dbot_fix] Побочные изменения в полях, которые НЕ должны были трогаться: {diffs_elsewhere or 'нет'}", flush=True)

    _finish(out)


def _finish(out: dict) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[dbot_fix] Записано {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
