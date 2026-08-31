"""Персистентный бюджетный гард поверх Dune API (см. docs/COST_POSTMORTEM.md).

data/credits_spent.json -- единственный источник правды о том, сколько
потрачено в этом биллинг-цикле и сколько из бюджета Sprint 1.5 (лимит
живёт в самом файле, `sprint15.budget_remaining_at_init` -- поднят с
150 до 210 после инцидента с 03c, см. docs/COST_POSTMORTEM.md,
ревизия 3) уже израсходовано.
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

# ---------- Санитарная проверка запроса ДО execute (ревизия 3 гарда) ----------
#
# 03c_cap_summary (ревизия 2) стоил 144.00 кредита вместо оценённых 8 --
# SQL был написан как UNION ALL четырёх независимых SELECT, каждый из
# которых заново ссылается на цепочку CTE, включающую полное сканирование
# сырых свопов (query_02_swaps_raw_july) -- судя по всему, движок Dune не
# делит вычисление общих CTE между ветками UNION ALL, а пересчитывает его
# в каждой ветке. См. docs/COST_POSTMORTEM.md, ревизия 3. Эта проверка --
# защита от ПОВТОРЕНИЯ именно этого паттерна, а не общая линтинг SQL.
SANITY_MAX_ESTIMATE = 40.0
HEAVY_SOURCE_MARKERS = ("query_02_swaps_raw_july", "dex.trades")


def check_sql_sanity(name: str, sql: str, estimated_credits: float) -> None:
    """Вызывается ПЕРЕД любым execute(), НЕЗАВИСИМО от остатка бюджета --
    жёсткий стоп с докладом при срабатывании. Оценка печатается ВСЕГДА,
    даже если проверка проходит."""
    print(f"[credit_guard] Оценка перед execute '{name}': {estimated_credits:.1f} кредитов.")
    if estimated_credits > SANITY_MAX_ESTIMATE:
        print(
            f"[credit_guard] СТОП (санитарная проверка): оценка '{name}' = "
            f"{estimated_credits:.1f} > {SANITY_MAX_ESTIMATE} -- жёсткий стоп ДО исполнения, "
            "независимо от остатка лимита. Пересмотрите SQL или оценку перед повторной попыткой."
        )
        raise BudgetGuardStop(1)
    lower = sql.lower()
    has_union_all = "union all" in lower
    has_heavy_source = any(marker.lower() in lower for marker in HEAVY_SOURCE_MARKERS)
    if has_union_all and has_heavy_source:
        print(
            f"[credit_guard] СТОП (санитарная проверка): '{name}' содержит UNION ALL И ссылку "
            "на тяжёлый источник (сырые свопы) -- риск многократного пересчёта одной и той же "
            "CTE-цепочки в каждой ветке UNION, тот же паттерн, что дал 144 кредита вместо 8 в "
            "03c_cap_summary ревизии 2 (см. docs/COST_POSTMORTEM.md). Перепишите одним проходом "
            "(CASE/filter в одном SELECT) перед исполнением. Ничего не заплачено."
        )
        raise BudgetGuardStop(1)


def check_overrun_after_execute(name: str, estimated_credits: float, actual_credits: float | None) -> None:
    """Пост-хок проверка: если факт > вдвое оценки -- немедленный стоп, не
    дожидаясь исчерпания лимита (см. п.4 задания пользователя). Деньги уже
    потрачены (Dune не даёт pre-execution оценку) -- это останавливает
    ДАЛЬНЕЙШИЕ шаги, а не отменяет уже случившееся списание."""
    if actual_credits is None:
        return
    if actual_credits > 2 * estimated_credits:
        print(
            f"[credit_guard] СТОП: '{name}' стоил по факту {actual_credits:.2f}, что больше чем "
            f"вдвое превышает оценку {estimated_credits:.1f} -- немедленная остановка, не дожидаясь "
            "исчерпания лимита. Деньги за этот шаг уже потрачены; дальнейшие шаги остановлены."
        )
        raise BudgetGuardStop(1)

# ---------- Result Read billing (ревизия 2 гарда, см. docs/COST_POSTMORTEM.md) ----------
#
# Биллинг-страница пользователя вскрыла вторую дыру: чтение результата
# через /execution/{id}/results биллится ОТДЕЛЬНО от execute() и
# зависит от объёма данных ("датапоинты" = строки × колонки), а не
# только от execute()'s execution_cost_credits. Пример: чтение полного
# результата 03_wallet_agg_july (1,210,160 строк × 5 колонок =
# 6,050,800 датапоинтов) стоило 163.98 кредита -- за само ЧТЕНИЕ, не
# исполнение. Точной публичной формулы не нашлось (docs.dune.com
# заблокирован сетевым прокси отсюда); ставка ниже калибрована по этому
# подтверждённому пользователем факту (163.98 / 6,050,800) с запасом
# на неточность калибровки под другие формы данных.
READ_COST_PER_DATAPOINT = 163.98 / 6_050_800  # ~2.71e-5, подтверждено пользователем для 03
READ_COST_PER_DATAPOINT_SAFETY_MARGIN = 1.5    # запас поверх калибровки


def estimate_read_credits(row_count: int, column_count: int) -> float:
    """Оценка стоимости ЧТЕНИЯ результата (не execute) по объёму данных.
    Используется ПЕРЕД каждым вызовом /execution/{id}/results -- см.
    check_before_read. Architectural-принцип «сырые данные не покидают
    Dune» (docs/README.md, Sprint 1.5 ревизия 2) означает: в этом
    проекте после редизайна такие чтения всегда малы (тысячи строк
    максимум) -- эта функция в первую очередь защита от регрессии."""
    datapoints = max(row_count, 0) * max(column_count, 1)
    return datapoints * READ_COST_PER_DATAPOINT * READ_COST_PER_DATAPOINT_SAFETY_MARGIN


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


def _check_before_operation(op_kind: str, name: str, estimate: float) -> None:
    state = load_state()
    sprint15_spent = state["sprint15"]["spent"]
    sprint15_budget = state["sprint15"]["budget_remaining_at_init"]
    cycle_spent_so_far = state["billing_cycle"]["initialized_spent"] + sprint15_spent
    cycle_limit = state["billing_cycle"]["external_limit"]

    projected_sprint15 = sprint15_spent + estimate
    projected_cycle = cycle_spent_so_far + estimate

    if projected_sprint15 > sprint15_budget:
        print(
            f"[credit_guard] СТОП: {op_kind} '{name}' (оценка {estimate:.1f} кредитов) "
            f"превысил бы бюджет Sprint 1.5 на остаток: потрачено "
            f"{sprint15_spent:.2f} + оценка {estimate:.1f} = {projected_sprint15:.2f} "
            f"> лимит {sprint15_budget:.1f}.\nНичего не исполнено/не прочитано. Файл: {CREDITS_FILE}."
        )
        raise BudgetGuardStop(1)
    if projected_cycle > cycle_limit:
        print(
            f"[credit_guard] СТОП: {op_kind} '{name}' (оценка {estimate:.1f} кредитов) "
            f"превысил бы внешнюю границу биллинг-цикла: потрачено в цикле "
            f"{cycle_spent_so_far:.2f} + оценка {estimate:.1f} = {projected_cycle:.2f} "
            f"> граница {cycle_limit:.1f}.\nНичего не исполнено/не прочитано. Файл: {CREDITS_FILE}."
        )
        raise BudgetGuardStop(1)
    print(
        f"[credit_guard] OK: {op_kind} '{name}' оценка {estimate:.1f}; после -- Sprint1.5 "
        f"{projected_sprint15:.2f}/{sprint15_budget:.1f}, цикл "
        f"{projected_cycle:.2f}/{cycle_limit:.1f}."
    )


def check_before_execute(name: str, estimated_credits: float | None = None) -> None:
    """Вызывается ПЕРЕД КАЖДЫМ execute(). Жёсткий exit с полным докладом
    при нарушении лимита -- см. docs/README.md, Sprint 1.5, п.2."""
    estimate = estimated_credits if estimated_credits is not None else DEFAULT_ESTIMATE
    _check_before_operation("execute", name, estimate)


def check_before_read(name: str, row_count: int, column_count: int) -> float:
    """Вызывается ПЕРЕД КАЖДЫМ чтением результата (/execution/.../results).
    Ревизия 2 гарда -- execute() был не единственной платной операцией,
    см. docs/COST_POSTMORTEM.md. Возвращает использованную оценку (для
    записи в record_read)."""
    estimate = estimate_read_credits(row_count, column_count)
    _check_before_operation("чтение результата", name, estimate)
    return estimate


def _record(op_kind: str, name: str, credits: float, credits_known: bool, execution_id: str | None) -> None:
    state = load_state()
    state["sprint15"]["spent"] = round(state["sprint15"]["spent"] + credits, 6)
    state["entries"].append(
        {
            "op": op_kind,
            "name": name,
            "execution_id": execution_id,
            "credits": credits,
            "credits_known": credits_known,
            "at": _now(),
        }
    )
    _save(state)
    tag = f"{credits:.3f}" if credits_known else f"~{credits:.3f} (ОЦЕНКА, не подтверждено Dune)"
    _git_commit(f"credits_spent.json: +{tag} за {op_kind} '{name}' [automated guard]")


def record_execution(name: str, actual_credits: float | None, execution_id: str | None = None) -> None:
    """Вызывается СРАЗУ после того, как стала известна (или точно
    неизвестна -- см. таймаут) фактическая стоимость execute(). Коммитит
    немедленно."""
    credits = float(actual_credits) if actual_credits is not None else 0.0
    _record("execute", name, credits, actual_credits is not None, execution_id)


def record_read(name: str, estimated_credits: float, row_count: int, column_count: int, execution_id: str | None = None) -> None:
    """Вызывается СРАЗУ после чтения результата. Dune не отдаёт точную
    стоимость чтения через API (в отличие от execute()) -- пишем
    ОЦЕНКУ (estimate_read_credits по фактическому row/column count,
    обычно точнее предварительной, т.к. row_count здесь уже реальный,
    не ожидаемый) и явно помечаем её как неподтверждённую."""
    actual_estimate = estimate_read_credits(row_count, column_count)
    _record("чтение результата", name, actual_estimate, False, execution_id)
