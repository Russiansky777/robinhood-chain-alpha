"""Персистентный бюджетный гард поверх Dune API (см. docs/COST_POSTMORTEM.md).

data/credits_spent.json -- единственный источник правды о том, сколько
потрачено в этом биллинг-цикле и сколько из бюджета Sprint 1.5 (150
кредитов на ОСТАТОК спринта, не на прогон) уже израсходовано.
Коммитится в git СРАЗУ после каждого execute с известной фактической
стоимостью -- не в конце прогона -- чтобы переживать краш/таймаут
посреди работы (см. пост-мортем: run #11 запустил ~100-кредитный
запрос и потерял его из виду при таймауте поллинга; run #8 аналогично
потерял стоимость уже завершившегося запроса при падении на скачивании
результатов -- ни то ни другое не попало бы в файл при коммите только
в конце прогона).

Dune API v1 не даёт предварительную оценку стоимости запроса перед
execute(). Проверка "потрачено + оценка <= лимит" поэтому использует
ПЕССИМИСТИЧНУЮ оценку на основе реальных исторических стоимостей (см.
вызывающий код) -- если она не передана явно, используется наихудшая
реально замеренная стоимость одного запроса в проекте (полномесячный
скан dex.trades, run #13: 102.8 кредита) с запасом.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

CREDITS_FILE = Path("data/credits_spent.json")

DEFAULT_ESTIMATE = 110.0  # см. docstring: наихудший реально замеренный запрос + запас


class BudgetGuardStop(SystemExit):
    """Жёсткий стоп -- гард сработал ДО execute. Не перехватывать и не
    ретраить в вызывающем коде; сообщение уже содержит полный отчёт."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_state() -> dict:
    return {
        "billing_cycle": {
            "external_limit": 2450.0,
            "initialized_spent": 1394.0,
            "initialized_at": _now(),
            "initialized_by": (
                "человек, факт с dune.com/settings/billing на 2026-08-31 -- "
                "НЕ выведено из логов CI, см. docs/COST_POSTMORTEM.md"
            ),
        },
        "sprint15": {
            "budget_remaining_at_init": 150.0,
            "spent": 0.0,
        },
        "entries": [],
    }


def load_state() -> dict:
    if CREDITS_FILE.exists():
        try:
            return json.loads(CREDITS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return _init_state()


def _save(state: dict) -> None:
    CREDITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDITS_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _git_commit(message: str) -> None:
    """Коммитит data/credits_spent.json немедленно, не дожидаясь
    финального шага workflow -- см. docstring модуля."""
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            check=False,
        )
        subprocess.run(["git", "add", str(CREDITS_FILE)], check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
        if diff.returncode == 0:
            return  # нечего коммитить
        subprocess.run(["git", "commit", "-m", message], check=False)
        subprocess.run(["git", "push"], check=False)
    except Exception as exc:  # никогда не роняем пайплайн из-за коммита гарда
        print(f"[credit_guard] ПРЕДУПРЕЖДЕНИЕ: не удалось закоммитить {CREDITS_FILE}: {exc}")


def check_before_execute(name: str, estimated_credits: float | None = None) -> None:
    """Вызывается ПЕРЕД КАЖДЫМ execute(). Жёсткий exit с полным докладом
    при нарушении лимита -- см. docs/README.md, Sprint 1.5, п.2."""
    estimate = estimated_credits if estimated_credits is not None else DEFAULT_ESTIMATE
    state = load_state()
    sprint15_spent = state["sprint15"]["spent"]
    sprint15_budget = state["sprint15"]["budget_remaining_at_init"]
    cycle_spent_so_far = state["billing_cycle"]["initialized_spent"] + sprint15_spent
    cycle_limit = state["billing_cycle"]["external_limit"]

    projected_sprint15 = sprint15_spent + estimate
    projected_cycle = cycle_spent_so_far + estimate

    if projected_sprint15 > sprint15_budget:
        print(
            f"[credit_guard] СТОП: запрос '{name}' (оценка {estimate:.1f} кредитов) "
            f"превысил бы бюджет Sprint 1.5 на остаток: потрачено "
            f"{sprint15_spent:.2f} + оценка {estimate:.1f} = {projected_sprint15:.2f} "
            f"> лимит {sprint15_budget:.1f}.\nНичего не исполнено. Файл: {CREDITS_FILE}."
        )
        raise BudgetGuardStop(1)
    if projected_cycle > cycle_limit:
        print(
            f"[credit_guard] СТОП: запрос '{name}' (оценка {estimate:.1f} кредитов) "
            f"превысил бы внешнюю границу биллинг-цикла: потрачено в цикле "
            f"{cycle_spent_so_far:.2f} + оценка {estimate:.1f} = {projected_cycle:.2f} "
            f"> граница {cycle_limit:.1f}.\nНичего не исполнено. Файл: {CREDITS_FILE}."
        )
        raise BudgetGuardStop(1)
    print(
        f"[credit_guard] OK: '{name}' оценка {estimate:.1f}; после -- Sprint1.5 "
        f"{projected_sprint15:.2f}/{sprint15_budget:.1f}, цикл "
        f"{projected_cycle:.2f}/{cycle_limit:.1f}."
    )


def record_execution(name: str, actual_credits: float | None, execution_id: str | None = None) -> None:
    """Вызывается СРАЗУ после того, как стала известна (или точно
    неизвестна -- см. таймаут) фактическая стоимость. Коммитит немедленно."""
    credits = float(actual_credits) if actual_credits is not None else 0.0
    state = load_state()
    state["sprint15"]["spent"] = round(state["sprint15"]["spent"] + credits, 6)
    state["entries"].append(
        {
            "name": name,
            "execution_id": execution_id,
            "credits": credits,
            "credits_known": actual_credits is not None,
            "at": _now(),
        }
    )
    _save(state)
    tag = f"{credits:.3f}" if actual_credits is not None else "НЕИЗВЕСТНО (см. entries)"
    _git_commit(f"credits_spent.json: +{tag} за '{name}' [automated guard]")
