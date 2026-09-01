#!/usr/bin/env python3
"""Юнит-тест на analysis/credit_guard.py -- блокирующее условие владельца
перед Sprint G1 Шагом 2 (см. docs/G1_DESIGN.md, "Механика детекции" /
docs/RESULTS.md, секция G1). Не pytest (в requirements.txt его нет) --
голый скрипт: assert + явный ненулевой exit при провале, не warning.

Проверяет:
1. Превышение бюджета текущего пространства (sprintG1, лимит 300) даёт
   BudgetGuardStop (SystemExit), а не проходит с предупреждением.
2. Превышение внешней границы биллинг-цикла (2450) даёт BudgetGuardStop,
   ДАЖЕ когда у активного пространства есть запас -- т.е. проверка
   реально считает цикл как initialized_spent + СУММА ВСЕХ пространств,
   а не только текущего (регрессия ровно того бага, который был найден
   и исправлен при обобщении гарда под Sprint G1 -- до фикса новый
   спринт видел бы заниженный расход цикла).
3. billing_cycle инициализирован реальными числами человека (не
   выведенными из логов CI): initialized_spent=1394.0, reset_at
   ="2026-09-14" -- обе константы должны существовать И быть теми
   самыми (сверяется с реальным data/credits_spent.json, не только с
   дефолтом _init_state()).
4. Операция в пределах обоих лимитов НЕ бросает исключение (гард не
   ложно-срабатывает).
5. Нулевые (упавшие до биллинга) попытки пишутся в леджер с оценкой и
   причиной падения, не только с фактом=0 (см. record_execution).

Использование: python analysis/test_credit_guard.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import credit_guard as cg

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def with_temp_state(state: dict):
    """Контекст: подменяет CREDITS_FILE на временный файл с заданным
    состоянием, восстанавливает оригинал на выходе."""
    class _Ctx:
        def __enter__(self):
            self._orig = cg.CREDITS_FILE
            self._tmpdir = tempfile.mkdtemp()
            cg.CREDITS_FILE = Path(self._tmpdir) / "credits_spent.json"
            cg.CREDITS_FILE.write_text(json.dumps(state))
            return cg.CREDITS_FILE

        def __exit__(self, *exc):
            cg.CREDITS_FILE = self._orig

    return _Ctx()


def test_namespace_budget_breach_hard_stops() -> None:
    """Требование 1: попытка операции сверх капа sprintG1 (300) даёт
    ненулевой exit, не warning."""
    state = {
        "billing_cycle": {"external_limit": 2450.0, "initialized_spent": 1394.0, "reset_at": "2026-09-14"},
        "sprintG1": {"budget_remaining_at_init": 300.0, "spent": 295.0},
        "entries": [],
    }
    orig_ns = os.environ.get(cg.NAMESPACE_ENV)
    os.environ[cg.NAMESPACE_ENV] = "sprintG1"
    try:
        with with_temp_state(state):
            exc_obj = None
            try:
                # spent(295) + estimate(10) = 305 > namespace budget 300.
                cg.check_before_execute("over_namespace_cap", 10.0)
            except SystemExit as e:
                exc_obj = e
            check("namespace-cap breach raises SystemExit (not a warning)", exc_obj is not None)
            check(
                "namespace-cap breach exit code is non-zero",
                exc_obj is not None and exc_obj.code not in (None, 0),
                f"code={getattr(exc_obj, 'code', None)!r}",
            )
            check(
                "namespace-cap breach raises the specific BudgetGuardStop type, not a bare SystemExit",
                isinstance(exc_obj, cg.BudgetGuardStop),
            )
    finally:
        if orig_ns is None:
            os.environ.pop(cg.NAMESPACE_ENV, None)
        else:
            os.environ[cg.NAMESPACE_ENV] = orig_ns


def test_cycle_breach_sums_all_namespaces() -> None:
    """Требование 1+регрессия: превышение ВНЕШНЕЙ границы цикла (2450)
    должно останавливать операцию, даже если у активного пространства
    (sprintG1) есть запас по своему собственному лимиту -- т.е. проверка
    обязана суммировать ВСЕ пространства (sprint15 + sprintG1 + ...) поверх
    initialized_spent, а не только активное. До фикса (найден и исправлен
    в этой же сессии при обобщении гарда под Sprint G1) эта проверка
    считала бы только sprintG1.spent и пропустила бы операцию, реально
    пробивающую границу аккаунта."""
    state = {
        "billing_cycle": {"external_limit": 2450.0, "initialized_spent": 1394.0, "reset_at": "2026-09-14"},
        # sprint15 давно потратил много; sprintG1 -- почти ничего, но с
        # учётом sprint15 сумма цикла уже близко к границе.
        "sprint15": {"budget_remaining_at_init": 1200.0, "spent": 1050.0},
        "sprintG1": {"budget_remaining_at_init": 300.0, "spent": 5.0},
        "entries": [],
    }
    # cycle_spent_so_far = 1394 + 1050 + 5 = 2449. Остаток границы = 1.
    # Оценка операции 5.0 (в пределах sprintG1's 300-лимита с огромным
    # запасом) должна упасть по ЦИКЛОВОЙ границе: 2449 + 5 = 2454 > 2450.
    orig_ns = os.environ.get(cg.NAMESPACE_ENV)
    os.environ[cg.NAMESPACE_ENV] = "sprintG1"
    try:
        with with_temp_state(state):
            raised = False
            code = None
            try:
                cg.check_before_execute("over_cycle_boundary", 5.0)
            except SystemExit as e:
                raised = True
                code = e.code
            check(
                "cycle-boundary breach raises SystemExit even with namespace room to spare",
                raised,
            )
            check("cycle-boundary breach exit code is non-zero", code not in (None, 0), f"code={code!r}")
    finally:
        if orig_ns is None:
            os.environ.pop(cg.NAMESPACE_ENV, None)
        else:
            os.environ[cg.NAMESPACE_ENV] = orig_ns

    # Обратный случай -- доказывает, что это именно СУММА всех пространств,
    # а не игнорирование других пространств: та же арифметика, но с одним
    # пространством (без sprint15) -- та же оценка ДОЛЖНА пройти.
    state_single_ns = {
        "billing_cycle": {"external_limit": 2450.0, "initialized_spent": 1394.0, "reset_at": "2026-09-14"},
        "sprintG1": {"budget_remaining_at_init": 300.0, "spent": 5.0},
        "entries": [],
    }
    os.environ[cg.NAMESPACE_ENV] = "sprintG1"
    try:
        with with_temp_state(state_single_ns):
            raised = False
            try:
                # 1394 + 5(sprintG1) + 5(estimate) = 1404, далеко от 2450.
                cg.check_before_execute("under_cycle_boundary_single_ns", 5.0)
            except SystemExit:
                raised = True
            check(
                "same estimate passes when other namespaces are absent (proves it's a real sum, not a hardcoded fail)",
                not raised,
            )
    finally:
        if orig_ns is None:
            os.environ.pop(cg.NAMESPACE_ENV, None)
        else:
            os.environ[cg.NAMESPACE_ENV] = orig_ns


def test_within_limits_does_not_raise() -> None:
    """Требование 4: операция в пределах обоих лимитов не должна ложно
    останавливать пайплайн."""
    state = {
        "billing_cycle": {"external_limit": 2450.0, "initialized_spent": 1394.0, "reset_at": "2026-09-14"},
        "sprintG1": {"budget_remaining_at_init": 300.0, "spent": 18.62},
        "entries": [],
    }
    orig_ns = os.environ.get(cg.NAMESPACE_ENV)
    os.environ[cg.NAMESPACE_ENV] = "sprintG1"
    try:
        with with_temp_state(state):
            raised = False
            try:
                cg.check_before_execute("well_within_limits", 5.0)
            except SystemExit:
                raised = True
            check("in-budget operation does not raise", not raised)
    finally:
        if orig_ns is None:
            os.environ.pop(cg.NAMESPACE_ENV, None)
        else:
            os.environ[cg.NAMESPACE_ENV] = orig_ns


def test_real_billing_cycle_initialized_with_real_numbers() -> None:
    """Требование: цикловой потолок инициализирован РЕАЛЬНЫМИ числами
    (человеком, с dune.com/settings/billing), а не выведен из сумм
    пространств -- сверяется с фактическим data/credits_spent.json на
    диске (не только с дефолтом _init_state())."""
    real_file = Path("data/credits_spent.json")
    check("data/credits_spent.json exists", real_file.exists())
    if not real_file.exists():
        return
    real_state = json.loads(real_file.read_text())
    bc = real_state.get("billing_cycle", {})
    check(
        "billing_cycle.initialized_spent is the real human-confirmed 1394.0",
        bc.get("initialized_spent") == 1394.0,
        f"got {bc.get('initialized_spent')!r}",
    )
    check(
        "billing_cycle.initialized_by documents a human source, not CI logs",
        "человек" in str(bc.get("initialized_by", "")),
    )
    check(
        "billing_cycle.reset_at is the documented 2026-09-14 (PROJECT_STATE.md rule 7)",
        bc.get("reset_at") == "2026-09-14",
        f"got {bc.get('reset_at')!r}",
    )
    check(
        "_init_state() default also carries reset_at (survives a fresh/missing file)",
        cg._init_state()["billing_cycle"].get("reset_at") == "2026-09-14",
    )


def test_failed_attempts_carry_estimate_and_reason() -> None:
    """Требование (Дополнительно): нулевые попытки должны попадать в
    леджер как операция -> оценка -> факт=0 -> причина падения."""
    state = {
        "billing_cycle": {"external_limit": 2450.0, "initialized_spent": 1394.0, "reset_at": "2026-09-14"},
        "sprintG1": {"budget_remaining_at_init": 300.0, "spent": 0.0},
        "entries": [],
    }
    orig_ns = os.environ.get(cg.NAMESPACE_ENV)
    os.environ[cg.NAMESPACE_ENV] = "sprintG1"
    try:
        with with_temp_state(state) as tmp_file:
            cg.record_execution(
                "some_probe [FAILED]", None, "exec_123",
                estimated_credits=7.5, failure_reason="Column 'x' cannot be resolved at line 3",
            )
            saved = json.loads(tmp_file.read_text())
            entry = saved["entries"][-1]
            check("failed-attempt entry has credits=0.0", entry["credits"] == 0.0)
            check("failed-attempt entry preserves the pre-execute estimate", entry.get("estimated_credits") == 7.5)
            check(
                "failed-attempt entry preserves the failure reason",
                "cannot be resolved" in str(entry.get("failure_reason", "")),
            )
    finally:
        if orig_ns is None:
            os.environ.pop(cg.NAMESPACE_ENV, None)
        else:
            os.environ[cg.NAMESPACE_ENV] = orig_ns


def main() -> int:
    # Тест не должен трогать реальный git -- _git_commit коммитит
    # CREDITS_FILE, который здесь подменён на временный путь вне
    # репозитория; сама функция уже отказоустойчива (перехватывает
    # исключение и печатает предупреждение), но не молчит на stderr,
    # поэтому подменяем её на no-op на время теста.
    orig_git_commit = cg._git_commit
    cg._git_commit = lambda message: None
    try:
        test_namespace_budget_breach_hard_stops()
        test_cycle_breach_sums_all_namespaces()
        test_within_limits_does_not_raise()
        test_real_billing_cycle_initialized_with_real_numbers()
        test_failed_attempts_carry_estimate_and_reason()
    finally:
        cg._git_commit = orig_git_commit

    print()
    if FAILURES:
        print(f"[test_credit_guard] {len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("[test_credit_guard] Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
