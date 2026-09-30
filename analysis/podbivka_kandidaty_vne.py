#!/usr/bin/env python3
"""Кандидаты вне выборки: отбор по окну pyg7, проверка на 28.09 17Z → 30.09 06Z. Только чтение.

Архив -- podbivka_arhiv_den.py с --okno-vsem --okno-dokupki 1800: правило сигнала Code-1 (нет покупки того же минта в
предыдущие 1800 слотов) для ВСЕХ наших адресов, порог 2 SOL-экв. (pyg10_* -- окно pyg7, сутки со «разгонным» часом
16Z, который отбрасывается; vne10_2026-09-28T16 -- вне выборки). Сигнал в ячейках -- котировка WSOL, пул с моделью
архива (кривая pump.fun, LaunchLab, CPMM, DAMM v1), без why_not модели; Pump AMM -- только число (модель не для вывода).
Модель режима 1: вход «конец слота» (S0_дно), билет 0.3 (и 0.1 для справки), выходы +72 / +108, п.п. чистыми.

Стадии:
  --otbor   pyg10: по каждому из 543 и 15 кандидатов 27.09 -- n, среднее, медиана, в плюсе; отбор «верх 543»:
            n ≥ 20, среднее +72 ≥ +2 п.п. и медиана +72 > 0 (порог владельца 30.09: плюс на 3–5 сделках хвоста уже стрелял 23.09) -> data/podbivka/kandidaty_vne_otbor.json
  --vne     vne10: сигналы отобранных и кандидатов вне выборки -> data/podbivka/kandidaty_vne_signaly.json
  --nalog   (облако, Helius) транзакции этих сигналов и налог маршрута модулем Code-1 bloom_route_tax.py (копия
            data/podbivka/code1_snapshot/bloom_route_tax.py, ветка claude/nifty-sagan-r0polg 76d2a986) ->
            data/podbivka/kandidaty_vne_nalog.json
  --svod    страница docs/podbivka_2026-09-30_kandidaty_vne_vyborki.md
"""
from __future__ import annotations

import argparse
import calendar
import csv
import glob
import gzip
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
ДЛЯ_ВЫВОДА = {"pump", "raydium-launchpad", "raydium-cpmm", "meteora-damm-v1"}
ВНЕ_С = calendar.timegm(time.strptime("2026-09-28T17", "%Y-%m-%dT%H"))
ВНЕ_ДО = calendar.timegm(time.strptime("2026-09-30T06", "%Y-%m-%dT%H"))
N_МИН, ПОРОГ_ПП = 20, 2.0
КЛЮЧИ = {"72": "S0_дно|0.3|72", "108": "S0_дно|0.3|108", "72_01": "S0_дно|0.1|72", "108_01": "S0_дно|0.1|108"}


def ts(с: dict) -> float:
    return (с.get("timestamp") or 0) / 1000


def пп(с: dict, k: str):
    return ((с.get("модель") or {}).get("пп") or {}).get(КЛЮЧИ[k])


def в_ячейке(с: dict) -> bool:
    return (с.get("по_окну") is True and с.get("quoteMint") == WSOL and с.get("pool") in ДЛЯ_ВЫВОДА
            and not (с.get("модель") or {}).get("why_not") and пп(с, "72") is not None)


def стат(v: list) -> dict:
    v = [x for x in v if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "среднее": round(statistics.mean(v), 2), "медиана": round(statistics.median(v), 2),
            "в_плюсе": round(sum(1 for x in v if x > 0) / len(v), 3)}


def адреса() -> tuple:
    адр = json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    г543 = {a for a, v in адр.items() if "543" in ((v or {}).get("группы") or [])}
    канд = [r["address"] for r in csv.DictReader(open(П / "candidates_2026-09-27.csv", encoding="utf-8"))]
    return г543, канд, адр


def сигналы(файлы: list, окно) -> list:
    из_, видел = [], set()
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        for с in д.get("сигналы") or []:
            if с["signature"] in видел or not окно(с, f):
                continue
            видел.add(с["signature"])
            из_.append(с)
    return из_


def окно_pyg10(с: dict, f: str) -> bool:
    d0 = calendar.timegm(time.strptime(Path(f).name.split("_")[1][:13], "%Y-%m-%dT%H")) + 3600   # разгонный час прочь
    return d0 <= ts(с) < d0 + 86400


def отбор() -> int:
    г543, канд, _ = адреса()
    файлы = sorted(glob.glob(str(П / "arhiv_den" / "pyg10_*.json.gz")))
    сс = сигналы(файлы, окно_pyg10)
    по: dict = {}
    for с in сс:
        if с["trader"] in г543 or с["trader"] in канд:
            по.setdefault(с["trader"], []).append(с)
    ряды = {}
    for a, L in по.items():
        яч = [с for с in L if в_ячейке(с)]
        ряды[a] = {"всего_сигналов": len(L), "pump_amm": sum(1 for с in L if с.get("pool") == "pump-amm"),
                   "не_sol": sum(1 for с in L if с.get("quoteMint") != WSOL),
                   **{k: стат([пп(с, k) for с in яч]) for k in КЛЮЧИ}}
    верх = sorted((a for a in г543 if (ряды.get(a) or {}).get("72", {}).get("n", 0) >= N_МИН
                   and ряды[a]["72"]["среднее"] >= ПОРОГ_ПП and ряды[a]["72"]["медиана"] > 0), key=lambda a: -ряды[a]["72"]["среднее"])
    (П / "kandidaty_vne_otbor.json").write_text(json.dumps({
        "архив": [Path(f).name for f in файлы], "сигналов_всего": len(сс), "порог": {"n": N_МИН, "среднее_72_пп": ПОРОГ_ПП, "медиана_72_пп": "> 0"},
        "верх_543": верх, "кандидаты_27_09": канд, "по_адресам": ряды}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"файлов {len(файлы)}, сигналов {len(сс)}, у 543 с сигналами {sum(1 for a in по if a in г543)}, верх 543: {len(верх)}")
    return 0


def вне() -> int:
    о = json.loads((П / "kandidaty_vne_otbor.json").read_text(encoding="utf-8"))
    цель = set(о["верх_543"]) | set(о["кандидаты_27_09"])
    сс = сигналы([str(П / "arhiv_den" / "vne10_2026-09-28T16.json.gz")], lambda с, f: ВНЕ_С <= ts(с) < ВНЕ_ДО)
    наши = [с for с in сс if с["trader"] in цель]
    (П / "kandidaty_vne_signaly.json").write_text(json.dumps({"окно": ["2026-09-28T17:00Z", "2026-09-30T06:00Z"],
                                                              "сигналы": наши}, ensure_ascii=False), encoding="utf-8")
    print(f"сигналов вне выборки {len(сс)}, у отобранных и кандидатов {len(наши)}, в ячейках {sum(1 for с in наши if в_ячейке(с))}")
    return 0


def налог() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    сп = importlib.util.spec_from_file_location("bloom_route_tax_code1", П / "code1_snapshot" / "bloom_route_tax.py")
    RT = importlib.util.module_from_spec(сп)
    сп.loader.exec_module(RT)
    сс = json.loads((П / "kandidaty_vne_signaly.json").read_text(encoding="utf-8"))["сигналы"]
    нужные = [с for с in сс if в_ячейке(с) or с.get("pool") == "pump-amm"]
    уз = S.Узел()
    из_ = {}
    with уз.на("helius"):
        def налог_фн(м):
            н = S.налог_минта(уз, м)
            return {"fee_bps": н["bps"] if not н.get("why_not") else None}
        подписи = [с["signature"] for с in нужные]
        txs = {}
        for и in range(0, len(подписи), 50):
            txs.update(уз.пакет(подписи[и:и + 50]))
        for с in нужные:
            т = txs.get(с["signature"])
            р = RT.налог_маршрута(т, с["mint"], налог_фн, откуда="source") if т else {"why_not": "транзакция не прочитана"}
            из_[с["signature"]] = {к: р.get(к) for к in ("token_fee_bps", "route_transfer_fee_bps", "transfers_of_token",
                                                          "taxed_intermediates", "route_tax_lower_bound", "mints_unknown", "why_not")}
    out = П / "kandidaty_vne_nalog.json"
    out.write_text(json.dumps({"сигналов": len(нужные), "налог": из_, "расход": уз.расход()}, ensure_ascii=False), encoding="utf-8")
    R.записано(out)
    print("налог маршрута:", len(из_), flush=True)
    return 0


def налоговый(н: dict | None) -> bool | None:
    if not н or н.get("why_not") and н.get("route_transfer_fee_bps") is None:
        return None
    return (н.get("route_transfer_fee_bps") or 0) > 0 or (н.get("token_fee_bps") or 0) > 0


def свод() -> int:
    г543, канд, адр = адреса()
    о = json.loads((П / "kandidaty_vne_otbor.json").read_text(encoding="utf-8"))
    сс = json.loads((П / "kandidaty_vne_signaly.json").read_text(encoding="utf-8"))["сигналы"]
    нп = П / "kandidaty_vne_nalog.json"
    нал = json.loads(нп.read_text(encoding="utf-8"))["налог"] if нп.exists() else {}
    цели = list(dict.fromkeys(о["верх_543"] + о["кандидаты_27_09"]))
    ф = lambda s_: "—" if not s_.get("n") else f"{s_['n']} / {s_['среднее']:+.1f} / {s_['медиана']:+.1f} / {100 * s_['в_плюсе']:.0f}%"  # noqa: E731
    ряды, список = [], []
    for a in цели:
        L = [с for с in сс if с["trader"] == a]
        яч = [с for с in L if в_ячейке(с)]
        чистые = [с for с in яч if налоговый(нал.get(с["signature"])) is False]
        налог_ = [с for с in яч if налоговый(нал.get(с["signature"])) is True]
        неизв = [с for с in яч if налоговый(нал.get(с["signature"])) is None]
        вне72, вне108 = стат([пп(с, "72") for с in чистые]), стат([пп(с, "108") for с in чистые])
        вне72_01 = стат([пп(с, "72_01") for с in чистые])
        нал72 = стат([пп(с, "72") for с in налог_])
        вн = (о["по_адресам"].get(a) or {})
        кто = ("кандидат 27.09" if a in канд else "") + (" + " if a in канд and a in о["верх_543"] else "") + \
              ("верх 543" if a in о["верх_543"] else "")
        прошёл = a in о["верх_543"] and вне72.get("n", 0) >= 5 and вне72["среднее"] > 0
        if прошёл:
            список.append(a)
        ряды.append(f"| `{a[:8]}` | {(адр.get(a) or {}).get('имя') or ''} | {кто} | {ф(вн.get('72') or {})} | {ф(вне72)} | {ф(вне108)} | "
                    f"{вне72_01.get('среднее', '—') if вне72_01.get('n') else '—'} | {ф(нал72)} | {len(неизв)} | "
                    f"{sum(1 for с in L if с.get('pool') == 'pump-amm')} | {'да' if прошёл else 'нет'} |")
    md = ["# Кандидаты вне выборки: отбор по окну pyg7, проверка 28.09 17Z → 30.09 06Z", "",
          "Архив PumpApi с правилом сигнала Code-1 для всех наших адресов (нет покупки того же минта в предыдущие 1800 "
          "слотов; analysis/podbivka_arhiv_den.py --okno-vsem), порог 2 SOL-экв. Отбор -- pyg10 (окно pyg7: 21.09 17Z → "
          f"28.09 17Z): из 543 -- n ≥ {N_МИН}, среднее ≥ +{ПОРОГ_ПП:.0f} п.п. на сделку и медиана > 0 (вход «конец слота», "
          "билет 0.3, выход +72); плюс 15 кандидатов 27.09 (data/podbivka/candidates_2026-09-27.csv) без отбора. Вне выборки -- "
          "vne10: 28.09 17:00Z → 30.09 06:00Z. Ячейки -- котировка WSOL, кривая pump.fun / LaunchLab / CPMM / DAMM v1; "
          "Pump AMM -- только число. Налоговый маршрут -- модулем Code-1 bloom_route_tax.py по транзакции источника "
          "(сумма bps Token-2022 по передачам > 0 или налог самого токена), отдельно. Ячейка: n / среднее / медиана / "
          "в плюсе, п.п. чистыми. Ничего не рекомендуется; решение о группе -- владельца.", "",
          f"Отобрано из 543 по pyg10: {len(о['верх_543'])}. В предварительный список `cand1` ниже -- отобранные, у которых "
          "вне выборки (без налоговых маршрутов) n ≥ 5 и среднее +72 > 0: **" + str(len(список)) + "**.", "",
          "| адрес | имя | откуда | pyg10 +72 (отбор) | вне: +72 | вне: +108 | вне: +72 при 0.1, среднее | вне: налоговые +72 | "
          "налог не прочитан | Pump AMM | в списке |", "|---|---|---|---|---|---|---|---|---|---|---|"] + ряды
    md += ["", "## Список для `cand1` (0.1 SOL)", ""] + ([f"- `{a}`" for a in список] or ["— никто не прошёл"])
    (КОРЕНЬ / "docs" / "podbivka_2026-09-30_kandidaty_vne_vyborki.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[:8]))
    return 0


if __name__ == "__main__":
    р = argparse.ArgumentParser()
    г = р.add_mutually_exclusive_group(required=True)
    for ф_ in ("otbor", "vne", "nalog", "svod"):
        г.add_argument(f"--{ф_}", action="store_true")
    а = р.parse_args()
    raise SystemExit(отбор() if а.otbor else вне() if а.vne else налог() if а.nalog else свод())
