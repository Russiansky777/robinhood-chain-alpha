#!/usr/bin/env python3
"""Подбивка: первые покупки лидера (окно 1б, 125) и BATCH-5 -- по программе пула
и по котировке, офлайн.

Лидер: 125 первых покупок от 2 SOL-экв окна 1б (data/podbivka/lider_kotirovka.json:
котировка пула по сделке; программа -- из чисел симулятора в файлах кошельков).
BATCH-5: 9 названных источников задачи (кроме лидера), первые покупки выше
порога в их окне (data/podbivka/nashi). Символы котировочных -- из выгрузки DBot
(GP = HTmQz7My…, RuneScape Gold).
"""
from __future__ import annotations

import collections
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_tablicy as T  # noqa: E402

КОРЕНЬ = T.КОРЕНЬ
GP = "HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = ("So11111111111111111111111111111111111111112", "native_sol")
КРИВАЯ = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
ЛИДЕР = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"


def класс_программы(прог, ярлыки) -> str:
    if not прог:
        return "не опознан"
    if прог == КРИВАЯ:
        return "кривая pump.fun"
    м = (ярлыки.get(прог) or прог).lower()
    if "pump" in м and "amm" in м:
        return "Pump AMM"
    if "clmm" in м:
        return "Raydium CLMM"
    if "raydium cp" in м or "cpmm" in м:
        return "Raydium CPMM"
    if "launchlab" in м:
        return "Raydium LaunchLab"
    if м.strip() == "raydium" or "amm v4" in м or "ammv4" in м:
        return "Raydium AMM v4"
    if "meteora" in м or "dynamic bonding" in м or "dlmm" in м or "damm" in м:
        return "Meteora"
    return "прочее: " + (ярлыки.get(прог) or прог[:8])


def класс_котировки(q) -> str:
    if q in SOL:
        return "SOL"
    if q == USDC:
        return "USDC"
    if q == GP:
        return "GP"
    return "другое" if q else "не опознан"


def таблица(заг: str, ряды: list) -> list:
    n = len(ряды)
    out = [f"### {заг} (всего {n})", "", "| программа пула | число | доля |", "|---|---|---|"]
    for к, v in collections.Counter(р[0] for р in ряды).most_common():
        out.append(f"| {к} | {v} | {100 * v / n:.1f}% |")
    out += ["", "| котировка | число | доля |", "|---|---|---|"]
    for к, v in collections.Counter(р[1] for р in ряды).most_common():
        out.append(f"| {к} | {v} | {100 * v / n:.1f}% |")
    out += ["", "| программа × котировка | число |", "|---|---|"]
    for к, v in collections.Counter(р for р in ряды).most_common():
        out.append(f"| {к[0]} / {к[1]} | {v} |")
    return out + [""]


def main() -> int:
    ярлыки = json.loads(T.ЯРЛЫКИ_ПУЛОВ.read_text(encoding="utf-8")) if T.ЯРЛЫКИ_ПУЛОВ.exists() else {}
    lk = json.loads((КОРЕНЬ / "data" / "podbivka" / "lider_kotirovka.json").read_text(encoding="utf-8"))
    прог: dict = {}
    for f in glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "*" / "*.json")):
        if "/_" in f or "postobr" in f or "zapusk" in f:
            continue
        try:
            к = json.loads(Path(f).read_text(encoding="utf-8"))
        except ValueError:
            continue
        for п in (к.get("покупки") or []) if isinstance(к, dict) else []:
            с = п.get("sim") or {}
            if с.get("program"):
                прог[п.get("signature")] = с["program"]
    for р in (lk.get("шаг2") or {}).get("ряды") or []:
        if р.get("program"):
            прог.setdefault(р["sig"], р["program"])
    лидер = [(класс_программы(прог.get(р["sig"]), ярлыки), класс_котировки(р.get("quote_mint"))) for р in lk["ряды"]]
    имена = json.loads((КОРЕНЬ / "data" / "podbivka" / "imena_istochnikov.json").read_text(encoding="utf-8"))["имена"]
    б5 = [a for a in имена if a != ЛИДЕР and "Brez" not in имена[a]]
    ряды_б5, по_кош = [], {}
    for a in б5:
        p = КОРЕНЬ / "data" / "podbivka" / "nashi" / f"{a}.json"
        if not p.exists():
            по_кош[имена[a]] = "нет файла"
            continue
        к = json.loads(p.read_text(encoding="utf-8"))
        пп = T.пересчёт_порога(к.get("покупки") or [], a)
        по_кош[имена[a]] = len(пп)
        for п in пп:
            с = п.get("sim") or {}
            ряды_б5.append((класс_программы(с.get("program"), ярлыки), класс_котировки(с.get("quote_mint"))))
    md = ["# Подбивка: программа пула и котировка первых покупок -- лидер и BATCH-5", "",
          "GP -- `HTmQz7My…` (символ из выгрузки DBot). SOL -- WSOL и натив кривой.", ""]
    md += таблица("Лидер pointfarmcap, окно 1б 18–25.09, первые покупки от 2 SOL-экв", лидер)
    md += таблица("BATCH-5 вместе (9 источников без лидера), первые покупки выше порога", ряды_б5)
    md += ["Покупок по источникам BATCH-5: " + ", ".join(f"{k} {v}" for k, v in по_кош.items()), ""]
    (КОРЕНЬ / "docs" / "podbivka_2026-09-27_programmy.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
