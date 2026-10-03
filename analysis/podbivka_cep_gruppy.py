#!/usr/bin/env python3
"""Покупки источников торгующих групп ПО ЦЕПИ за окно: сколько прошло чужой подписью.

ЗАЧЕМ (задание владельца 03.10, п.3). У лидера 362 покупки из 1297 прошли подписью
посредника -- архив PumpApi такие события не видит вовсе (он привязывает событие к
подписанту). Вопрос ко всем торгующим источникам: сколько у каждого ПЕРВЫХ покупок от
2 SOL-экв по цепи за 72 ч и сколько из них под ЧУЖОЙ подписью. Code-1 добавляет к этому,
сколько из них увидел детектор полосы.

Покупатель -- владелец токен-счёта, на который пришёл токен (тем же сбором, что у лидера:
podbivka_lider_cep.сбор). Своих формул здесь нет.
Выход: docs/podbivka_<дата>_cep_gruppy.md, data/podbivka/cep_gruppy_<дата>.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_lider_cep as LC  # noqa: E402

КОРЕНЬ = LC.КОРЕНЬ
П = LC.П
ТОРГУЮЩИЕ = ("lane_s0", "batch5", "cand1", "cand1_03", "cand1_05", "cand2", "cand3", "leader", "konveyer")
ПОРОГ = 2.0


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--chasov", type=int, default=72)
    р.add_argument("--stranic", type=int, default=12)
    р.add_argument("--metka", default=time.strftime("%Y-%m-%d", time.gmtime()))
    р.add_argument("--gruppy", default=",".join(ТОРГУЮЩИЕ))
    а = р.parse_args()
    гр = {x for x in а.gruppy.split(",") if x}
    адр = json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    цель = sorted(a for a, v in адр.items() if set((v or {}).get("группы") or []) & гр)
    с_ts = time.time() - а.chasov * 3600
    с_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(с_ts))
    ряды = {}
    for i, a in enumerate(цель, 1):
        под = types.SimpleNamespace(adres=a, out=f"cep_gruppy_syroe_{a[:8]}.json", s=с_utc, do="",
                                    stranic=а.stranic)
        try:
            д = LC.сбор(под)
        except Exception as exc:  # noqa: BLE001
            ряды[a] = {"группы": (адр.get(a) or {}).get("группы") or [], "ошибка": f"{type(exc).__name__}: {exc}"[:200]}
            print(f"[{i}/{len(цель)}] {a[:8]}: сбор упал -- {type(exc).__name__}", flush=True)
            continue
        пок = д["покупки"]
        первые = [x for x in пок if x["вид"] == "первая"]
        от2 = [x for x in первые if (x.get("sol_экв") or 0) >= ПОРОГ]
        чужой = [x for x in от2 if not x["подписант"]]
        без_размера = [x for x in первые if x.get("sol_экв") is None]
        ряды[a] = {"группы": (адр.get(a) or {}).get("группы") or [],
                   "подписей": д["подписей"], "покупок": len(пок), "первых": len(первые),
                   "первых_от_2": len(от2), "из_них_чужой_подписью": len(чужой),
                   "первых_без_размера": len(без_размера),
                   "сбоев": д.get("сбоев") or 0, "счёт": д.get("счёт") or {}}
        print(f"[{i}/{len(цель)}] {a[:8]}: покупок {len(пок)}, первых от 2 SOL {len(от2)}, "
              f"из них чужой подписью {len(чужой)}", flush=True)
    дт = {"окно_часов": а.chasov, "с_utc": с_utc, "порог_sol": ПОРОГ,
          "группы": sorted(гр), "адресов": len(цель), "ряды": ряды}
    out_j = П / f"cep_gruppy_{а.metka}.json"
    out_j.write_text(json.dumps(дт, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out_j)
    всего = sum(v.get("первых_от_2") or 0 for v in ряды.values())
    чуж = sum(v.get("из_них_чужой_подписью") or 0 for v in ряды.values())
    md = [f"# Источники торгующих групп по цепи: первые покупки от 2 SOL и чужая подпись ({а.metka})", "",
          f"Окно {а.chasov} ч (с {с_utc}), адресов {len(цель)}. Покупатель -- владелец токен-счёта, "
          f"на который пришёл токен; «чужая подпись» -- транзакцию подписал не он (архив PumpApi "
          f"такие события не видит). Всего первых покупок от {ПОРОГ:g} SOL-экв **{всего}**, из них "
          f"чужой подписью **{чуж}**. Столбец детектора заполняет Code-1. Ничего не рекомендуется.", "",
          "| источник | группы | подписей | покупок | первых | первых ≥ 2 SOL | из них чужой подписью | "
          "первых без размера | детектор (Code-1) |", "|---|---|---|---|---|---|---|---|---|"]
    for a, v in sorted(ряды.items(), key=lambda kv: -(kv[1].get("первых_от_2") or 0)):
        if v.get("ошибка"):
            md.append(f"| `{a[:8]}` | {', '.join(v['группы'])} | — | — | — | — | — | — | "
                      f"сбор упал: {v['ошибка'][:60]} |")
            continue
        md.append(f"| `{a[:8]}` | {', '.join(v['группы'])} | {v['подписей']} | {v['покупок']} | "
                  f"{v['первых']} | **{v['первых_от_2']}** | {v['из_них_чужой_подписью']} | "
                  f"{v['первых_без_размера']} | |")
    md += [""]
    out_m = КОРЕНЬ / "docs" / f"podbivka_{а.metka}_cep_gruppy.md"
    out_m.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(out_m)
    print(f"{out_m.name}: адресов {len(цель)}, первых от 2 SOL {всего}, чужой подписью {чуж}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
