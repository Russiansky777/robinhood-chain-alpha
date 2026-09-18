#!/usr/bin/env python3
"""Владелец, 2026-09-18: первый прогон разведки (solana_dune_probe.py)
упал на create_query с HTTP 403 "Your account is read-only" -- ключ
DUNE_API_KEY (старый аккаунт). Владелец напомнил: есть ВТОРОЙ, более
новый аккаунт с кредитами (secrets.DUNE_API_KEY_MOZILA, уже
использовался в analysis/dune_mozila_recon.py, там же его отдельный
леджер data/credits_spent_mozila.json, namespace funding_mozila).

Эта проверка (владелец, п.1-2 задания): для КАЖДОГO известного в
секретах Dune-ключа -- один дешёвый запрос ("SELECT 1"), классификация
результата (работает / read-only / невалиден), БЕЗ печати самих
ключей. Если находится рабочий ключ -- запускаем ту же разведку схемы
(solana_dune_probe.run_probe), что и раньше, на этом ключе.

Единственные два Dune-секрета, реально используемые где-либо в этом
репозитории (проверено grep по .github/workflows/*.yml и analysis/*.py,
не гадание): DUNE_API_KEY, DUNE_API_KEY_MOZILA. Других имён
(DUNE_EXPLORER_API и т.п.) в коде/workflow нет -- если такой секрет
всё же существует в GH Actions, но нигде не читается кодом, эта
проверка его не увидит (честно фиксируем это ограничение в выводе).

Каждый ключ проверяется на СВОЁМ отдельном леджере (старый аккаунт --
data/credits_spent.json/namespace solana_dune_probe; Mozila --
data/credits_spent_mozila.json/namespace solana_dune_probe) -- бюджеты
двух аккаунтов не смешиваются, как и во всём остальном проекте."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

OUT_PATH = Path("data/solana_dune_key_check_result.json")

# (имя секрета, файл леджера для этого аккаунта, namespace в нём)
KEY_CANDIDATES = [
    ("DUNE_API_KEY", "data/credits_spent.json", "solana_dune_probe"),
    ("DUNE_API_KEY_MOZILA", "data/credits_spent_mozila.json", "solana_dune_probe"),
]


def _ensure_namespace(ledger_file: str, ns: str, budget: float) -> None:
    path = Path(ledger_file)
    if not path.exists():
        raise RuntimeError(f"Леджер {ledger_file} не существует -- аккаунт ещё ни разу не использовался, "
                            "нет billing_cycle/external_truth для честной проверки бюджета.")
    state = json.loads(path.read_text())
    if ns not in state:
        state[ns] = {"budget_remaining_at_init": budget, "spent": 0.0}
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def check_one_key(secret_name: str, ledger_file: str, namespace: str) -> dict:
    api_key = os.environ.get(secret_name, "")
    if not api_key:
        return {"secret_name": secret_name, "status": "not_set_in_env"}

    _ensure_namespace(ledger_file, namespace, budget=5.0)

    # credit_guard.CREDITS_FILE читается из env ТОЛЬКО при импорте модуля
    # (module-level Path(...)) -- поэтому переключаем на уже
    # ИМПОРТИРОВАННЫЙ модуль напрямую (namespace() читает env заново на
    # каждый вызов, с этим полем всё проще).
    os.environ["CREDIT_GUARD_NAMESPACE"] = namespace
    import credit_guard
    credit_guard.CREDITS_FILE = Path(ledger_file)
    # dune_client.py импортирует ФУНКЦИИ credit_guard (не константы) --
    # они читают credit_guard.CREDITS_FILE из его собственного module-
    # namespace на каждый вызов, так что переприсваивание выше уже
    # действует без перезагрузки dune_client.
    from dune_client import DuneClient, DuneCreditsExhausted, DuneRateLimited

    client = DuneClient(api_key=api_key, cache_dir=f"analysis/output/cache_{secret_name.lower()}")
    try:
        df = client.run_sql_cached(
            name=f"key_check_{secret_name.lower()}", sql="SELECT 1 AS probe",
            estimated_credits=0.5, expected_max_rows=10, expected_columns=1,
        )
        return {
            "secret_name": secret_name, "status": "working",
            "n_rows": len(df) if df is not None else None,
            "sample": df.to_dict("records") if df is not None else None,
        }
    except RuntimeError as exc:
        msg = str(exc)
        if "403" in msg and "read-only" in msg.lower():
            return {"secret_name": secret_name, "status": "read_only", "detail": msg[:300]}
        if "401" in msg or "403" in msg:
            return {"secret_name": secret_name, "status": "invalid_or_forbidden", "detail": msg[:300]}
        return {"secret_name": secret_name, "status": "error", "detail": msg[:300]}
    except (DuneCreditsExhausted, DuneRateLimited) as exc:
        return {"secret_name": secret_name, "status": "credits_exhausted_or_rate_limited", "detail": str(exc)[:300]}


def main() -> None:
    out: dict = {"key_checks": []}
    working: tuple[str, str, str] | None = None
    for secret_name, ledger_file, namespace in KEY_CANDIDATES:
        result = check_one_key(secret_name, ledger_file, namespace)
        out["key_checks"].append(result)
        print(f"[dune_key_check] {secret_name}: {result['status']}"
              + (f" -- {result['detail']}" if result.get("detail") else ""))
        if result["status"] == "working" and working is None:
            working = (secret_name, ledger_file, namespace)

    out["other_dune_secret_names_referenced_in_repo"] = (
        "Только DUNE_API_KEY и DUNE_API_KEY_MOZILA где-либо читаются кодом/workflow "
        "(проверено grep, не по памяти) -- если существует ещё один секрет с другим "
        "именем, эта проверка его не видит."
    )

    if working is None:
        out["HONEST_ANSWER"] = (
            "Ни один известный Dune-ключ не может выполнять запросы (см. key_checks выше) "
            "-- Dune закрыт для этой задачи, остаёмся на цепи."
        )
        print("[dune_key_check] " + out["HONEST_ANSWER"])
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return

    secret_name, ledger_file, namespace = working
    print(f"[dune_key_check] Рабочий ключ найден: {secret_name}. Продолжаю разведку схемы на нём.")

    # Тот же ключ/леджер/namespace, что уже настроены check_one_key --
    # credit_guard.CREDITS_FILE и CREDIT_GUARD_NAMESPACE уже указывают
    # на правильный аккаунт после последней успешной проверки выше.
    import credit_guard
    credit_guard.CREDITS_FILE = Path(ledger_file)
    os.environ["CREDIT_GUARD_NAMESPACE"] = namespace
    state = json.loads(Path(ledger_file).read_text())
    if state[namespace]["budget_remaining_at_init"] < 20.0:
        state[namespace]["budget_remaining_at_init"] = 20.0
        Path(ledger_file).write_text(json.dumps(state, indent=2, ensure_ascii=False))
        print(f"[dune_key_check] Бюджет '{namespace}' в {ledger_file} поднят до 20.0 "
              "(для основной разведки схемы после успешной проверки ключа).")

    from dune_client import DuneClient

    from solana_dune_probe import run_probe, _finish

    client = DuneClient(api_key=os.environ[secret_name], cache_dir=f"analysis/output/cache_{secret_name.lower()}")
    probe_out = run_probe(client)
    probe_out["used_secret_name"] = secret_name
    out["probe_result"] = probe_out
    _finish(probe_out)  # пишет data/solana_dune_probe_result.json отдельно, как раньше

    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[dune_key_check] Записано {OUT_PATH}")


if __name__ == "__main__":
    main()
