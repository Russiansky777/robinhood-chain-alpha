#!/usr/bin/env python3
"""Подбивка: п.5 (336 «история > 150 страниц» -- из блоков, m5) и п.13 (корзины
п.4 при входе S+0 и S+1, выход +72), офлайн.

п.5  сколько из 336 получили число S+1/72 (S+1/150); таблица первого прохода
     (404 кошелька, S+1/72 по всем покупкам выше порога) до и после добавки.
п.13 корзины п.4 по покупкам 404 кошельков (с пересчётом порога), S+0/72 и
     S+1/72: n, среднее, усечённое без ceil(5 % n) лучших, медиана, в плюс.
Пылевая цена S+0 в режиме «цена свопа» (> 10 000 п.п., ошибка 16) исключается.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_m4_svod as M  # noqa: E402
import podbivka_tablicy as T  # noqa: E402

КОРЕНЬ = T.КОРЕНЬ


def чп(сим, e, H):
    v = T.чп(сим, e, H)
    if v is not None and v > M.ПЫЛЬ_ПП and (сим or {}).get("режим") == "price":
        return None
    return v


def строка(с: dict) -> str:
    if not с.get("n"):
        return "0 | — | — | — | —"
    return (f"{с['n']} | {M.ф(с['ср'])} | {M.ф(с['ус'])} | {M.ф(с['мед'])} | "
            f"{100 * с['плюс']:.0f}%")


def main() -> int:
    ярлыки = json.loads(T.ЯРЛЫКИ_ПУЛОВ.read_text(encoding="utf-8")) if T.ЯРЛЫКИ_ПУЛОВ.exists() else {}
    ярлыки.setdefault("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "pump.fun кривая")
    все = []
    # Проба -- подмножество koshelki (те же 10 кошельков, ранний код): не читается.
    for к in T.читать(T.каталог_задачи("koshelki")):
        адрес = (к.get("строка") or {}).get("address")
        if адрес and not к.get("why_not") and not (к.get("скан") or {}).get("why_not"):
            все += T.пересчёт_порога(к.get("покупки") or [], адрес)
    md = ["# Подбивка: п.5 (из блоков) и п.13 (корзины при S+0 и S+1)", ""]

    # п.5
    m5 = {}
    for l in (КОРЕНЬ / "data" / "podbivka" / "postobr" / "m5_0_end.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(l)
        m5[r["signature"]] = r
    с72 = sum(1 for r in m5.values() if r.get("S1_72") is not None)
    с150 = sum(1 for r in m5.values() if r.get("S1_150") is not None)
    по_режиму: dict = {}
    for r in m5.values():
        x = по_режиму.setdefault(r.get("режим") or "отказ", [0, 0])
        x[0] += 1
        x[1] += r.get("S1_72") is not None
    до = [чп(п.get("sim"), "S1", 72) for п in все]
    после = list(до)
    доб = 0
    for i, п in enumerate(все):
        r = m5.get(п.get("signature"))
        if после[i] is None and r and r.get("S1_72") is not None:
            после[i] = r["S1_72"]
            доб += 1
    а, б = M.стат(до), M.стат(после)
    md += ["## п.5 История пула > 150 страниц -- состояния из блоков", "",
           f"Из 336: число S+1/72 -- {с72}, S+1/150 -- {с150}. По режиму (всего / с числом): "
           + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(по_режиму.items()))
           + ". Режим «цена свопа» из блоков не считается (нужна цена свопа, а не резервы). "
           "Доля продажи в m5 = f покупки (продажи пула не смотрелись).", "",
           "| таблица первого прохода, S+1/72 | n | среднее | усечённое | медиана | в плюс |",
           "|---|---|---|---|---|---|",
           f"| до добавки | {строка(а)} |", f"| после добавки ({доб} покупок) | {строка(б)} |",
           f"| только добавленные | {строка(M.стат([r['S1_72'] for r in m5.values() if r.get('S1_72') is not None]))} |",
           ""]

    # п.13
    md += ["## п.13 Корзины п.4 при входе S+0 и S+1, выход +72", "",
           "| признак | корзина | S+0: n | ср | ус. | мед | в плюс | S+1: n | ср | ус. | мед | в плюс |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for имя in T.КОРЗИНЫ:
        по: dict = {}
        for п in все:
            к = T.корзина(п, имя, ярлыки)
            по.setdefault("нет данных" if к is None else к, []).append(п)
        for к, пп in sorted(по.items(), key=lambda kv: str(kv[0])):
            с0 = M.стат([чп(п.get("sim"), "S0", 72) for п in пп])
            с1 = M.стат([чп(п.get("sim"), "S1", 72) for п in пп])
            if not с0.get("n") and not с1.get("n"):
                continue
            md.append(f"| {имя} | {к} | {строка(с0)} | {строка(с1)} |")
    md.append("")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-27_p5_p13.md").write_text("\n".join(md), encoding="utf-8")
    print(f"п.5: {с72}/336 с числом, добавлено {доб}; п.13 готов")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
