#!/usr/bin/env python3
"""Задача 5, владелец 2026-09-11, пункт 3: "Кросс-таб -- локально, параллельно,
ноль кредитов."

Чистая Python-обработка уже скачанных данных из
data/p3_guard_cache/task5_operators_result.json (реальный прогон
task5_operators.py, стоимость 5.02 кредита, УЖЕ потрачена ранее -- здесь
НИКАКИХ обращений к Dune нет, только локальный разбор JSON).

Цель: показать, насколько три независимых сигнала кластеризации
(funder-match, hourly-cosine>=0.98, code-match) СОГЛАСУЮТСЯ друг с другом
для пар адресов, объединённых в одного "оператора" через union-find.
Пара адресов может попасть в один оператор транзитивно (через цепочку
других адресов), даже если у САМОЙ этой пары нет прямого сигнала --
это тоже важно показать явно, а не скрывать.

code_match_groups в реальных данных пуст (все 50 адресов -- EOA, что уже
зафиксировано в PROJECT_STATE.md), поэтому кросс-таб фактически сводится к
funder x hourly, но код написан общим, на случай будущих данных с
контрактами."""
from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path

IN_PATH = Path("data/p3_guard_cache/task5_operators_result.json")
OUT_PATH = Path("data/p3_guard_cache/task5_operators_crosstab_result.json")


def pairs_from_groups(groups: dict[str, list[str]]) -> set[frozenset[str]]:
    """Из {ключ: [члены]} строим все НЕУПОРЯДОЧЕННЫЕ пары членов внутри
    одной группы (сам ключ, если это тоже адрес-фандер/адрес с тем же кодом,
    не добавляется как отдельный член -- это внешний признак, не участник)."""
    out: set[frozenset[str]] = set()
    for members in groups.values():
        for a, b in combinations(sorted(set(members)), 2):
            out.add(frozenset((a, b)))
    return out


def run() -> int:
    data = json.loads(IN_PATH.read_text())

    code_pairs = pairs_from_groups(data.get("code_match_groups", {}) or {})
    funder_pairs = pairs_from_groups(data.get("funder_match_groups", {}) or {})
    hourly_pairs = {
        frozenset((row["a"], row["b"]))
        for row in data.get("hourly_high_similarity_pairs", []) or []
    }

    operators = data.get("operators", [])
    # реальный список member-полей у операторов (после union-find слияния)
    operator_member_lists = []
    for op in operators:
        members = op.get("members") or op.get("addresses") or []
        if len(members) > 1:
            operator_member_lists.append(sorted(set(members)))

    # Все пары адресов, оказавшихся в ОДНОМ операторе (после транзитивного
    # слияния union-find) -- это надмножество прямых code/funder/hourly пар.
    merged_pairs: set[frozenset[str]] = set()
    for members in operator_member_lists:
        for a, b in combinations(members, 2):
            merged_pairs.add(frozenset((a, b)))

    rows = []
    for pair in sorted(merged_pairs, key=lambda p: tuple(sorted(p))):
        a, b = tuple(sorted(pair))
        has_code = pair in code_pairs
        has_funder = pair in funder_pairs
        has_hourly = pair in hourly_pairs
        n_signals = sum([has_code, has_funder, has_hourly])
        rows.append(
            {
                "a": a,
                "b": b,
                "code_match": has_code,
                "funder_match": has_funder,
                "hourly_match": has_hourly,
                "n_direct_signals": n_signals,
                "transitive_only": n_signals == 0,
            }
        )

    n_total_merged_pairs = len(merged_pairs)
    n_direct_code = sum(1 for r in rows if r["code_match"])
    n_direct_funder = sum(1 for r in rows if r["funder_match"])
    n_direct_hourly = sum(1 for r in rows if r["hourly_match"])
    n_both_funder_and_hourly = sum(1 for r in rows if r["funder_match"] and r["hourly_match"])
    n_transitive_only = sum(1 for r in rows if r["transitive_only"])

    # Пары, у которых есть прямой сигнал (funder ИЛИ hourly ИЛИ code), но
    # НЕ оба сразу из {funder, hourly} -- т.е. согласие только одного метода.
    n_funder_only = sum(1 for r in rows if r["funder_match"] and not r["hourly_match"] and not r["code_match"])
    n_hourly_only = sum(1 for r in rows if r["hourly_match"] and not r["funder_match"] and not r["code_match"])

    summary = {
        "n_operators_with_multiple_members": len(operator_member_lists),
        "n_total_merged_pairs": n_total_merged_pairs,
        "n_pairs_with_direct_code_signal": n_direct_code,
        "n_pairs_with_direct_funder_signal": n_direct_funder,
        "n_pairs_with_direct_hourly_signal": n_direct_hourly,
        "n_pairs_funder_and_hourly_both": n_both_funder_and_hourly,
        "n_pairs_funder_only_no_hourly_no_code": n_funder_only,
        "n_pairs_hourly_only_no_funder_no_code": n_hourly_only,
        "n_pairs_transitive_only_no_direct_signal": n_transitive_only,
        "note": (
            "code_match_groups пуст в реальных данных (все 50 адресов -- EOA, "
            "см. PROJECT_STATE.md), поэтому code-сигнал везде False. "
            "transitive_only=True означает: пара оказалась в одном операторе "
            "через цепочку (A связан с B по funder, B связан с C по hourly) "
            "БЕЗ прямого funder/hourly/code совпадения между A и C -- это "
            "не ошибка, а ожидаемое поведение union-find, но его нужно "
            "явно отличать от прямого совпадения при интерпретации силы "
            "кластеризации."
        ),
    }

    out = {
        "generated_at_utc": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
        "source_file": str(IN_PATH),
        "summary": summary,
        "pairs": rows,
    }

    print("[task5_operators_crosstab] источник:", IN_PATH)
    print(f"[task5_operators_crosstab] операторов с >1 членом: {summary['n_operators_with_multiple_members']}")
    print(f"[task5_operators_crosstab] всего смерженных пар (после union-find): {n_total_merged_pairs}")
    print(f"[task5_operators_crosstab]   прямой funder-сигнал: {n_direct_funder}")
    print(f"[task5_operators_crosstab]   прямой hourly-сигнал (cosine>=0.98): {n_direct_hourly}")
    print(f"[task5_operators_crosstab]   оба сигнала сразу (funder И hourly): {n_both_funder_and_hourly}")
    print(f"[task5_operators_crosstab]   только funder (без hourly/code): {n_funder_only}")
    print(f"[task5_operators_crosstab]   только hourly (без funder/code): {n_hourly_only}")
    print(f"[task5_operators_crosstab]   ТОЛЬКО транзитивно, без прямого сигнала между этой парой: {n_transitive_only}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"[task5_operators_crosstab] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
