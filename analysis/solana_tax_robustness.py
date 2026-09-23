#!/usr/bin/env python3
"""Устойчивость плюса: держится он на всей выборке или на считанных сделках.

Три проверки, все без сети:
  1. Выбивание крайних. Результат по цепи и доля в плюс после исключения
     1, 3 и 5 ЛУЧШИХ сделок -- и, для симметрии, после исключения 3
     ХУДШИХ. Если плюс держится на паре сделок, он рассыплется на первом
     же шаге.
  2. Бутстрэп доверительного интервала среднего результата на сделку:
     10 000 выборок с возвращением, интервал 95%. Нижняя граница ниже
     нуля означает, что средний плюс от нуля статистически не отличим.
  3. То же по отдельной задаче.

Сид бутстрэпа фиксирован: тот же вход обязан давать тот же интервал,
иначе цифру нельзя проверить.

Источник строк -- выгрузка аудита комиссии: в ней лежат те же сделки,
по которым построены налоговые группы. Дата берётся из учёта по подписи
покупки.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_PATH = REPO_ROOT / "data" / "solana_transfer_fee_audit.json"
TRADES_PATH = REPO_ROOT / "data" / "solana_trades_all.json"
OUT_PATH = REPO_ROOT / "data" / "solana_tax_robustness.json"

PILOT_TASK = "pointfarmcap"
SEED = 20260923
N_BOOT = 10_000


def группа_а(r: dict) -> bool:
    return not r.get("цель_таксируемая") and not r.get("промежуточные_таксируемые")


def цепь(r: dict) -> float:
    return float(r.get("net_sol") or 0.0)


def свод(rows: list) -> dict:
    if not rows:
        return {"сделок": 0, "результат_по_цепи_sol": 0.0, "доля_в_плюс_по_цепи": None,
                "среднее_на_сделку_sol": None}
    плюс = sum(1 for r in rows if цепь(r) > 0)
    итог = sum(цепь(r) for r in rows)
    return {"сделок": len(rows),
            "результат_по_цепи_sol": round(итог, 6),
            "доля_в_плюс_по_цепи": round(плюс / len(rows), 4),
            "среднее_на_сделку_sol": round(итог / len(rows), 6)}


def без_лучших(rows: list, n: int) -> list:
    return sorted(rows, key=цепь, reverse=True)[n:]


def без_худших(rows: list, n: int) -> list:
    return sorted(rows, key=цепь)[n:]


def выбивание(rows: list, шаги=(1, 3, 5), худших=3) -> dict:
    out = {"как_есть": свод(rows)}
    for n in шаги:
        out[f"без_{n}_лучших"] = свод(без_лучших(rows, n))
    out[f"без_{худших}_худших"] = свод(без_худших(rows, худших))
    return out


def бутстрэп(rows: list, n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """Интервал 95% для СРЕДНЕГО результата на сделку."""
    vals = [цепь(r) for r in rows]
    if len(vals) < 2:
        return {"почему": "меньше двух сделок -- интервал не строится"}
    rnd = random.Random(seed)
    средние = []
    n = len(vals)
    for _ in range(n_boot):
        средние.append(sum(vals[rnd.randrange(n)] for _ in range(n)) / n)
    средние.sort()
    lo = средние[int(0.025 * n_boot)]
    hi = средние[int(0.975 * n_boot) - 1]
    факт = sum(vals) / n
    return {"сделок": n, "среднее_на_сделку_sol": round(факт, 6),
            "интервал_95_низ_sol": round(lo, 6), "интервал_95_верх_sol": round(hi, 6),
            "выборок": n_boot, "сид": seed,
            "ноль_внутри_интервала": bool(lo <= 0 <= hi),
            "итог_по_выборке_sol": round(факт * n, 6),
            "интервал_95_итога_низ_sol": round(lo * n, 6),
            "интервал_95_итога_верх_sol": round(hi * n, 6)}


def топ(rows: list, n: int, даты: dict) -> list:
    out = []
    for r in sorted(rows, key=цепь, reverse=True)[:n]:
        bt = даты.get(r.get("подпись_покупки"))
        out.append({
            "дата_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(bt)) if bt else None),
            "задача": r.get("задача"), "источник": r.get("источник"),
            "минт": r.get("минт"), "вложено_sol": r.get("sol_in"),
            "результат_по_цепи_sol": round(цепь(r), 6),
            "сигнал_pct": r.get("gross_pct")})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--n-boot", type=int, default=N_BOOT)
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    if not AUDIT_PATH.exists():
        raise SystemExit(f"нет {AUDIT_PATH}")
    rows = (json.loads(AUDIT_PATH.read_text()).get("строки") or [])
    if not rows:
        raise SystemExit("в выгрузке аудита нет строк")
    даты = {}
    if TRADES_PATH.exists():
        for t in json.loads(TRADES_PATH.read_text()):
            if t.get("buy_signature"):
                даты[t["buy_signature"]] = t.get("buy_block_time")

    а = [r for r in rows if группа_а(r)]
    а_без_пилота = [r for r in а if r.get("задача") != PILOT_TASK]
    b5 = [r for r in rows if r.get("задача") == "BATCH-5"]

    out = {
        "источник": str(AUDIT_PATH.relative_to(REPO_ROOT)),
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Результат по цепи -- sol_out минус sol_in; доля в плюс считается по нему же.",
            "Бутстрэп даёт интервал для СРЕДНЕГО на сделку при данной выборке; "
            "он не предсказывает будущее и не учитывает, что сделки могли быть "
            "связаны между собой (один токен, одно окно).",
            f"Сид бутстрэпа фиксирован ({SEED}): тот же вход даёт тот же интервал.",
            "Выбивание лучших -- не оценка, а проверка: если плюс держится на паре "
            "сделок, он исчезнет уже на первом шаге.",
        ],
        "группа_а": {"выбивание": выбивание(а), "бутстрэп": бутстрэп(а, args.n_boot),
                      "три_лучшие": топ(а, 3, даты)},
        "группа_а_без_пилота": {"выбивание": выбивание(а_без_пилота),
                                 "бутстрэп": бутстрэп(а_без_пилота, args.n_boot),
                                 "три_лучшие": топ(а_без_пилота, 3, даты)},
        "все_сделки": {"выбивание": выбивание(rows), "бутстрэп": бутстрэп(rows, args.n_boot),
                        "три_лучшие": топ(rows, 3, даты)},
        "BATCH-5": {"выбивание": выбивание(b5), "бутстрэп": бутстрэп(b5, args.n_boot),
                     "три_лучшие": топ(b5, 3, даты)},
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2)[:8000])
    print(f"\nвыгрузка -> {OUT_PATH}")


def self_test() -> None:
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    def R(net, задача="T", цель=False, промеж=False, sig=None):
        return {"net_sol": net, "задача": задача, "цель_таксируемая": цель,
                "промежуточные_таксируемые": (["X"] if промеж else []),
                "подпись_покупки": sig, "источник": "S", "минт": "M", "sol_in": 1.0,
                "gross_pct": net * 100}

    rows = [R(1.0), R(0.5), R(0.2), R(-0.1), R(-0.2), R(-0.3)]
    s = свод(rows)
    chk("итог по цепи", abs(s["результат_по_цепи_sol"] - 1.1) < 1e-9, str(s))
    chk("доля в плюс -- половина", s["доля_в_плюс_по_цепи"] == 0.5)
    chk("без лучшей итог падает",
        abs(свод(без_лучших(rows, 1))["результат_по_цепи_sol"] - 0.1) < 1e-9,
        str(свод(без_лучших(rows, 1))))
    chk("без трёх лучших уходит в минус",
        свод(без_лучших(rows, 3))["результат_по_цепи_sol"] < 0)
    chk("без трёх худших итог растёт",
        abs(свод(без_худших(rows, 3))["результат_по_цепи_sol"] - 1.7) < 1e-9)
    chk("выбивание считает все шаги",
        set(выбивание(rows)) == {"как_есть", "без_1_лучших", "без_3_лучших",
                                  "без_5_лучших", "без_3_худших"})

    b = бутстрэп(rows, n_boot=2000)
    chk("бутстрэп даёт среднее", abs(b["среднее_на_сделку_sol"] - 1.1 / 6) < 1e-6, str(b))
    chk("низ интервала не выше верха", b["интервал_95_низ_sol"] <= b["интервал_95_верх_sol"])
    chk("интервал повторяем при том же сиде",
        бутстрэп(rows, n_boot=2000)["интервал_95_низ_sol"] == b["интервал_95_низ_sol"])
    chk("другой сид -- другой интервал (иначе он не случайный)",
        бутстрэп(rows, n_boot=2000, seed=SEED + 1)["интервал_95_низ_sol"]
        != b["интервал_95_низ_sol"])
    chk("ноль внутри интервала помечен", isinstance(b["ноль_внутри_интервала"], bool))
    одна = бутстрэп([R(1.0)], n_boot=10)
    chk("одна сделка -- честный отказ, а не интервал нулевой ширины",
        "меньше двух" in (одна.get("почему") or ""), str(одна))

    узкий = бутстрэп([R(0.1) for _ in range(50)], n_boot=2000)
    chk("одинаковые значения дают нулевую ширину интервала",
        abs(узкий["интервал_95_верх_sol"] - узкий["интервал_95_низ_sol"]) < 1e-12)
    chk("и ноль в такой интервал не попадает", узкий["ноль_внутри_интервала"] is False)

    t = топ([R(1.0, sig="A"), R(0.5, sig="B")], 2, {"A": 1790000000})
    chk("лучшая идёт первой", t[0]["результат_по_цепи_sol"] == 1.0)
    chk("дата подставлена по подписи", t[0]["дата_utc"] is not None)
    chk("без даты в учёте -- None, а не выдуманное", t[1]["дата_utc"] is None)

    chk("группа а -- без комиссии и без промежуточного", группа_а(R(0.1)) is True)
    chk("таксируемый в группу а не идёт", группа_а(R(0.1, цель=True)) is False)
    chk("с промежуточным тоже не идёт", группа_а(R(0.1, промеж=True)) is False)
    chk("пустой набор не роняет свод", свод([])["сделок"] == 0)

    bad = 0
    for n, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка устойчивости: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    main()
