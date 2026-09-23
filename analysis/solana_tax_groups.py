#!/usr/bin/env python3
"""Результат по налоговым группам: кому и сколько стоит комиссия на перевод.

Четыре группы по двум признакам -- таксируем ли сам торгуемый токен и
идёт ли маршрут через таксируемый промежуточный:

  (а) токен без комиссии, маршрут прямой
  (б) токен таксируемый, маршрут прямой
  (в) через таксируемый промежуточный, токен без комиссии
  (г) и то и другое

По каждой группе: сделок, доля в плюс, медиана и среднее сигнала,
результат по цепи, налоги и результат БЕЗ налога. Пилот отдельно и
отдельно без пилота -- у пилота доля налоговых маршрутов в четыре раза
выше средней, и в общей куче он перекашивает картину.

Результат по цепи -- это sol_out минус sol_in, то есть что реально
осталось. Налог прибавляется обратно, и получается, сколько те же сделки
дали бы, будь токены без комиссии на перевод.

Сети не требует: работает по выгрузке аудита.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_PATH = REPO_ROOT / "data" / "solana_transfer_fee_audit.json"
OUT_PATH = REPO_ROOT / "data" / "solana_tax_groups.json"

PILOT_TASK = "pointfarmcap"
MIN_TRADES_SOURCE = 3


def группа(r: dict) -> str:
    цель = bool(r.get("цель_таксируемая"))
    промеж = bool(r.get("промежуточные_таксируемые"))
    if not цель and not промеж:
        return "а: токен без комиссии, маршрут прямой"
    if цель and not промеж:
        return "б: токен таксируемый, маршрут прямой"
    if not цель and промеж:
        return "в: через таксируемый промежуточный, токен без комиссии"
    return "г: и токен таксируемый, и маршрут через таксируемый"


def _med(vals):
    v = [x for x in vals if x is not None]
    return round(statistics.median(v), 4) if v else None


def _avg(vals):
    v = [x for x in vals if x is not None]
    return round(statistics.fmean(v), 4) if v else None


def свод(rows: list) -> dict:
    """Числа по набору сделок. Всё, что не считается, честно None."""
    сигналы = [r.get("gross_pct") for r in rows]
    плюсы = [r for r in rows if (r.get("gross_pct") or 0) > 0]
    цепь = sum((r.get("net_sol") or 0.0) for r in rows)
    налог = sum((r.get("удержано_sol") or 0.0) for r in rows)
    неполных = sum(1 for r in rows if r.get("налог_неполон_нет_курса"))
    return {
        "сделок": len(rows),
        "доля_в_плюс": round(len(плюсы) / len(rows), 4) if rows else None,
        "медиана_сигнала_pct": _med(сигналы),
        "среднее_сигнала_pct": _avg(сигналы),
        "вложено_sol": round(sum((r.get("sol_in") or 0.0) for r in rows), 6),
        "результат_по_цепи_sol": round(цепь, 6),
        "налогов_sol": round(налог, 6),
        "результат_без_налога_sol": round(цепь + налог, 6),
        "сделок_с_неполным_налогом": неполных,
    }


def по_группам(rows: list) -> dict:
    out = {}
    for r in rows:
        out.setdefault(группа(r), []).append(r)
    return {k: свод(v) for k, v in sorted(out.items())}


def по_задачам(rows: list) -> dict:
    out = {}
    for r in rows:
        out.setdefault(r.get("задача") or "без задачи", []).append(r)
    res = {}
    for k, v in sorted(out.items(), key=lambda kv: -len(kv[1])):
        такс = [r for r in v if r.get("цель_таксируемая")]
        чист = [r for r in v if not r.get("цель_таксируемая")]
        res[k] = {
            "сделок": len(v),
            "с_таксируемым_токеном": len(такс),
            "доля_таксируемых": round(len(такс) / len(v), 4) if v else None,
            "на_чистых": свод(чист),
            "на_таксируемых": свод(такс),
        }
    return res


def по_источникам(rows: list, минимум: int = MIN_TRADES_SOURCE) -> dict:
    out = {}
    for r in rows:
        out.setdefault(r.get("источник") or "источник не определён", []).append(r)
    res = {}
    for k, v in sorted(out.items(), key=lambda kv: -len(kv[1])):
        if len(v) < минимум:
            continue
        такс = [r for r in v if r.get("цель_таксируемая")]
        чист = [r for r in v if not r.get("цель_таксируемая")]
        res[k] = {
            "сделок": len(v),
            "метка": next((r.get("источник_метка") for r in v if r.get("источник_метка")), None),
            "задачи": sorted({r.get("задача") for r in v if r.get("задача")}),
            "доля_таксируемых": round(len(такс) / len(v), 4) if v else None,
            "на_чистых": свод(чист),
            "на_таксируемых": свод(такс),
        }
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(IN_PATH))
    ap.add_argument("--min-trades-source", type=int, default=MIN_TRADES_SOURCE)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    p = Path(args.inp)
    if not p.exists():
        raise SystemExit(f"нет {p} -- группировать нечего")
    d = json.loads(p.read_text())
    rows = d.get("строки") or []
    if not rows:
        raise SystemExit("в выгрузке аудита нет строк")
    if "net_sol" not in rows[0]:
        raise SystemExit("в строках нет net_sol -- нужна свежая выгрузка аудита")

    пилот = [r for r in rows if r.get("задача") == PILOT_TASK]
    без = [r for r in rows if r.get("задача") != PILOT_TASK]
    out = {
        "источник": str(p.relative_to(REPO_ROOT)),
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Результат по цепи -- это sol_out минус sol_in по каждой сделке, "
            "то есть что реально осталось; налог прибавляется обратно и даёт, "
            "сколько те же сделки дали бы без комиссии на перевод.",
            "Налог считается только по минтам, у которых расширение комиссии "
            "действительно есть в цепочке.",
            "У части сделок курс минта в SOL в их транзакциях не находится -- "
            "их налог неполон, и такие сделки посчитаны отдельной строкой.",
            "Сигнал -- это gross_pct из учёта, он НЕ равен результату по цепи: "
            "в цепь входят комиссия сети, чаевые и комиссия площадки.",
        ],
        "всего_сделок": len(rows),
        "группы_все": по_группам(rows),
        "итог_все": свод(rows),
        "группы_пилот": по_группам(пилот),
        "итог_пилот": свод(пилот),
        "группы_без_пилота": по_группам(без),
        "итог_без_пилота": свод(без),
        "по_задачам": по_задачам(rows),
        "по_источникам": по_источникам(rows, args.min_trades_source),
        "порог_источника": args.min_trades_source,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k != "по_источникам"},
                      ensure_ascii=False, indent=2)[:7000])
    print(f"\nвыгрузка -> {OUT_PATH}")


def self_test() -> None:
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    def R(цель, промеж, gross, net, sol_in, налог, задача="T", источник="S", неполон=False):
        return {"цель_таксируемая": цель, "промежуточные_таксируемые": (["X"] if промеж else []),
                "gross_pct": gross, "net_sol": net, "sol_in": sol_in, "удержано_sol": налог,
                "задача": задача, "источник": источник, "налог_неполон_нет_курса": неполон}

    chk("группа а", группа(R(False, False, 1, 1, 1, 0)).startswith("а"))
    chk("группа б", группа(R(True, False, 1, 1, 1, 0)).startswith("б"))
    chk("группа в", группа(R(False, True, 1, 1, 1, 0)).startswith("в"))
    chk("группа г", группа(R(True, True, 1, 1, 1, 0)).startswith("г"))

    rows = [R(False, False, 10.0, 0.1, 1.0, 0.0),
            R(False, False, -5.0, -0.05, 1.0, 0.0),
            R(True, True, -20.0, -0.2, 1.0, 0.15, задача=PILOT_TASK)]
    s = свод(rows)
    chk("сделок посчитано", s["сделок"] == 3)
    chk("доля в плюс -- одна из трёх", abs(s["доля_в_плюс"] - 1 / 3) < 1e-4, str(s["доля_в_плюс"]))
    chk("медиана сигнала", s["медиана_сигнала_pct"] == -5.0, str(s["медиана_сигнала_pct"]))
    chk("среднее сигнала", abs(s["среднее_сигнала_pct"] - (-5.0)) < 1e-9,
        str(s["среднее_сигнала_pct"]))
    chk("результат по цепи -- сумма net", abs(s["результат_по_цепи_sol"] - (-0.15)) < 1e-9,
        str(s["результат_по_цепи_sol"]))
    chk("налоги сложены", abs(s["налогов_sol"] - 0.15) < 1e-9)
    chk("без налога = результат + налог", abs(s["результат_без_налога_sol"] - 0.0) < 1e-9,
        str(s["результат_без_налога_sol"]))

    g = по_группам(rows)
    chk("групп ровно две", len(g) == 2, str(list(g)))
    chk("в чистой группе две сделки",
        g["а: токен без комиссии, маршрут прямой"]["сделок"] == 2)

    t = по_задачам(rows)
    chk("доля таксируемых по задаче T -- ноль", t["T"]["доля_таксируемых"] == 0.0)
    chk("у пилота доля таксируемых -- единица", t[PILOT_TASK]["доля_таксируемых"] == 1.0)
    chk("на таксируемых у чистой задачи сделок нет", t["T"]["на_таксируемых"]["сделок"] == 0)

    chk("источник ниже порога отсекается", по_источникам(rows, минимум=4) == {},
        str(list(по_источникам(rows, минимум=4))))
    chk("источник на пороге остаётся", по_источникам(rows, минимум=3)["S"]["сделок"] == 3)

    неполный = свод([R(True, False, -1.0, -0.01, 1.0, 0.0, неполон=True)])
    chk("сделка с неполным налогом отмечена", неполный["сделок_с_неполным_налогом"] == 1)
    chk("пустой набор не роняет свод", свод([])["сделок"] == 0)
    chk("и доля в плюс у пустого -- None, а не ноль", свод([])["доля_в_плюс"] is None)

    bad = 0
    for n, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка налоговых групп: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    main()
