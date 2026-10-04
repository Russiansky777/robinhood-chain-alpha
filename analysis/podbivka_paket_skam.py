#!/usr/bin/env python3
"""Пакет 04.10, п. В.8: зависшие и слитые сделки с 25.09 -- чем они отличались ДО входа.

Только чтение. Две части:
  * офлайн: список сделок по журналу Code-1 (выгрузки в data/podbivka/sdelki и
    data/podbivka/mesto, склейка по cid). «Зависшая» -- closed_reason говорит, что продавать
    было нечего (остаток минта 0) или что продано вне службы; «слитая» -- итог сделки
    <= порога (--porog-sliva, по умолчанию -0.1 SOL). Поля журнала, которые известны ДО
    входа: налог токена (`nalog_tokena_bps`), налог маршрута, freeze authority и отозвана ли
    она, тип пула, резерв пула, размер покупки источника;
  * `--cep`: по цепи через Helius для каждого минта -- владелец mint authority и freeze
    authority, программа токена (Token-2022 или обычная), расширения Token-2022 (налог на
    перевод, transfer hook, permanent delegate, default state frozen), число знаков и
    предложение, и концентрация держателей (`getTokenLargestAccounts`: доля первого,
    первых 3 и первых 10 от предложения).

Отсечки оцениваются одинаково: сколько SOL спасла бы каждая (сумма итогов сделок, которые
она не пустила) и сколько ХОРОШИХ сделок (итог > 0) она срезала бы по тому же правилу на
всей выгрузке, с разбиением на подбор (ранние сутки) и проверку (последние).
Выход: data/podbivka/paket_skam.json и docs/podbivka_<метка>_skam.md.
Ничего не рекомендуется -- решает владелец.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_cand2 as C2  # noqa: E402
import podbivka_nedelya_bilet as N  # noqa: E402

КОРЕНЬ = C2.КОРЕНЬ
П = C2.П
С_25_09 = "2026-09-25T00:00:00Z"
ПРИЗНАКИ_ЗАВИСА = ("продавать нечего", "продано вне службы", "остаток минта 0")


def журнал() -> tuple[dict, list]:
    по_cid: dict = {}
    откуда: list = []
    файлы = [П / "sdelki" / "vse_s_2709.json"]
    файлы += [Path(f) for f in sorted(glob.glob(str(П / "sdelki" / "sdelki_polosy_*.json")))]
    файлы += [Path(f) for f in sorted(glob.glob(str(П / "mesto" / "vhod_*.json")))]
    for f in файлы:
        if not f.exists():
            continue
        try:
            д = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        ряды = д.get("ряды") if isinstance(д, dict) else д
        if not ряды:
            continue
        откуда.append({"файл": f.name, "рядов": len(ряды)})
        for r in ряды:
            if r.get("cid"):
                по_cid[r["cid"]] = r
    return по_cid, откуда


def вид(r: dict, итог: float | None, порог: float) -> str | None:
    пр = (r.get("closed_reason") or "").lower()
    if any(с in пр for с in ПРИЗНАКИ_ЗАВИСА):
        return "зависшая"
    if итог is not None and итог <= порог:
        return "слитая"
    return None


def по_цепи(минты: list, rps: float) -> dict:
    import podbivka_sim as S  # noqa: PLC0415
    уз = S.Узел()
    из_: dict = {}
    with уз.на("helius"):
        for м in минты:
            стр: dict = {"минт": м}
            try:
                от = уз.вызов("getAccountInfo", [м, {"encoding": "jsonParsed"}])
                инфо = (((от or {}).get("result") or {}).get("value") or {})
                стр["программа"] = инфо.get("owner")
                разб = (((инфо.get("data") or {}).get("parsed") or {}).get("info") or {})
                стр["mint_authority"] = разб.get("mintAuthority")
                стр["freeze_authority"] = разб.get("freezeAuthority")
                стр["знаков"] = разб.get("decimals")
                стр["предложение"] = разб.get("supply")
                стр["заморожен_по_умолчанию"] = (разб.get("defaultAccountState") == "frozen"
                                                 if разб.get("defaultAccountState") else None)
                расш = разб.get("extensions") or []
                стр["расширения"] = [(x.get("extension") if isinstance(x, dict) else str(x))
                                     for x in расш]
                for x in расш:
                    if not isinstance(x, dict):
                        continue
                    если = x.get("state") or {}
                    if x.get("extension") == "transferFeeConfig":
                        нов = (если.get("newerTransferFee") or {})
                        стр["налог_bps"] = нов.get("transferFeeBasisPoints")
                    if x.get("extension") == "transferHook":
                        стр["hook"] = если.get("programId")
                    if x.get("extension") == "permanentDelegate":
                        стр["permanent_delegate"] = если.get("delegate")
            except Exception as exc:  # noqa: BLE001
                стр["why_not_mint"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            try:
                от = уз.вызов("getTokenLargestAccounts", [м])
                сч = (((от or {}).get("result") or {}).get("value") or [])
                сум = [float((x.get("uiAmountString") or x.get("amount") or 0)) for x in сч]
                всего = float(стр.get("предложение") or 0) / (10 ** (стр.get("знаков") or 0) or 1)
                if всего > 0 and сум:
                    стр["доля_первого"] = round(100 * сум[0] / всего, 2)
                    стр["доля_первых_3"] = round(100 * sum(сум[:3]) / всего, 2)
                    стр["доля_первых_10"] = round(100 * sum(сум[:10]) / всего, 2)
                стр["счетов_в_ответе"] = len(сч)
            except Exception as exc:  # noqa: BLE001
                стр["why_not_держатели"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            из_[м] = стр
            time.sleep(max(1.0 / rps, 0.0))
    из_["_расход_узла"] = уз.сводка() if hasattr(уз, "сводка") else {}
    return из_


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--metka", default=time.strftime("%Y-%m-%d", time.gmtime()))
    р.add_argument("--porog-sliva", type=float, default=-0.1)
    р.add_argument("--cep", action="store_true", help="дочитать минты по цепи (Helius)")
    р.add_argument("--rps", type=float, default=2.0)
    а = р.parse_args()

    по_cid, откуда = журнал()
    плохие, все_сделки = [], []
    for r in по_cid.values():
        и, из_ = N.итог_сделки(r)
        if not r.get("utc") or r["utc"] < С_25_09:
            continue
        стр = {"cid": r.get("cid"), "utc": r.get("utc"), "сутки": N.сутки_мадрид(r),
               "группа": r.get("group"), "минт": r.get("mint"), "имя": r.get("token_name"),
               "итог_sol": и, "итог_откуда": из_, "билет_sol": r.get("sol_in"),
               "closed_reason": r.get("closed_reason"),
               "налог_токена_bps": r.get("nalog_tokena_bps"),
               "налог_маршрута_bps": r.get("nalog_marshruta_bps"),
               "freeze_authority": r.get("freeze_authority"),
               "freeze_отозвана": r.get("freeze_authority_otozvan"),
               "пул": r.get("pool") or r.get("pool_vid") or r.get("lane_pool"),
               "резерв_пула_sol": r.get("pool_reserve_sol"),
               "размер_источника_sol": r.get("size_sol")}
        все_сделки.append(стр)
        в = вид(r, и, а.porog_sliva)
        if в:
            плохие.append({**стр, "вид": в})

    плохие.sort(key=lambda x: (x["итог_sol"] if x["итог_sol"] is not None else 0))
    цеп = {}
    if а.cep and плохие:
        цеп = по_цепи(sorted({x["минт"] for x in плохие if x.get("минт")}), а.rps)

    # отсечки, которые видны по журналу ДО входа
    def оценить(усл, имя):
        не_пустим = [x for x in все_сделки if усл(x)]
        спасено = sum(-(x["итог_sol"] or 0) for x in не_пустим if (x["итог_sol"] or 0) < 0)
        потеряно = sum((x["итог_sol"] or 0) for x in не_пустим if (x["итог_sol"] or 0) > 0)
        return {"отсечка": имя, "сделок_не_пустим": len(не_пустим),
                "из_них_в_плюсе": sum(1 for x in не_пустим if (x["итог_sol"] or 0) > 0),
                "спасло_бы_sol": round(спасено, 6), "потеряло_бы_sol": round(потеряно, 6),
                "итог_sol_чистыми": round(спасено - потеряно, 6)}

    отсечки = [
        оценить(lambda x: (x.get("налог_токена_bps") or 0) > 0, "налог токена > 0"),
        оценить(lambda x: (x.get("налог_маршрута_bps") or 0) > 0, "налог маршрута > 0"),
        оценить(lambda x: x.get("freeze_authority") and not x.get("freeze_отозвана"),
                "freeze authority на месте"),
        оценить(lambda x: x.get("группа") == "sniper_src", "не торговать sniper_src"),
    ]
    if цеп:
        отсечки += [
            оценить(lambda x: (цеп.get(x.get("минт")) or {}).get("mint_authority"),
                    "mint authority не отозвана (по цепи)"),
            оценить(lambda x: (цеп.get(x.get("минт")) or {}).get("расширения"),
                    "есть расширения Token-2022 (по цепи)"),
            оценить(lambda x: ((цеп.get(x.get("минт")) or {}).get("доля_первого") or 0) >= 20,
                    "доля первого держателя ≥ 20 % (по цепи)"),
        ]

    итог = {"что": "В.8 зависшие и слитые с 25.09: чем отличались до входа",
            "журнал": откуда, "сделок_в_журнале_с_2509": len(все_сделки),
            "порог_слива_sol": а.porog_sliva,
            "плохих": len(плохие), "по_видам": dict(collections.Counter(x["вид"] for x in плохие)),
            "по_группам": dict(collections.Counter(x["группа"] for x in плохие)),
            "сумма_итогов_плохих_sol": round(sum(x["итог_sol"] or 0 for x in плохие), 6),
            "сделки": плохие, "по_цепи": цеп, "отсечки": отсечки}
    out = П / "paket_skam.json"
    out.write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)

    def ф(v, формат="+.4f"):
        return "—" if v is None else format(v, формат)

    md = [f"# Зависшие и слитые сделки с 25.09: чем отличались до входа ({а.metka})", "",
          f"Журнал Code-1 (склейка по `cid`): "
          + ", ".join(f"`{x['файл']}` {x['рядов']}" for x in откуда)
          + f". Сделок с 25.09 — {len(все_сделки)}. «Зависшая» — closed_reason говорит, что "
            f"продавать было нечего или продано вне службы; «слитая» — итог ≤ {а.porog_sliva} SOL. "
            f"Таких {len(плохие)} на сумму {ф(итог['сумма_итогов_плохих_sol'])} SOL: "
          + ", ".join(f"{к} {в}" for к, в in итог["по_видам"].items())
          + "; по группам: " + ", ".join(f"{к} {в}" for к, в in итог["по_группам"].items()) + ".", "",
          "| сделка | сутки | группа | токен | минт | билет | итог SOL | налог токена, bps "
          "| freeze authority | почему закрылась |", "|---|---|---|---|---|---|---|---|---|---|"]
    for x in плохие:
        md.append(f"| `{(x['cid'] or '')[-8:]}` | {x['сутки']} | {x['группа']} "
                  f"| {(x['имя'] or '—')[:18]} | `{(x['минт'] or '')[:8]}` | {x['билет_sol']} "
                  f"| {ф(x['итог_sol'])} | {x['налог_токена_bps'] if x['налог_токена_bps'] is not None else '—'} "
                  f"| {'есть' if x['freeze_authority'] else '—'}"
                  f"{' (отозвана)' if x['freeze_отозвана'] else ''} "
                  f"| {(x['closed_reason'] or '—')[:46]} |")
    if цеп:
        md += ["", "## По цепи (минты плохих сделок)", "",
               "| минт | программа токена | mint authority | freeze authority | расширения "
               "| налог, bps | доля первого | первых 3 | первых 10 |",
               "|---|---|---|---|---|---|---|---|---|"]
        for м, v in цеп.items():
            if м.startswith("_"):
                continue
            md.append(f"| `{м[:8]}` | {(v.get('программа') or '—')[:12]} "
                      f"| {'есть' if v.get('mint_authority') else '—'} "
                      f"| {'есть' if v.get('freeze_authority') else '—'} "
                      f"| {', '.join(v.get('расширения') or []) or '—'} "
                      f"| {v.get('налог_bps') if v.get('налог_bps') is not None else '—'} "
                      f"| {ф(v.get('доля_первого'), '.2f')} | {ф(v.get('доля_первых_3'), '.2f')} "
                      f"| {ф(v.get('доля_первых_10'), '.2f')} |")
    else:
        md += ["", "## По цепи", "",
               "Не читалось: прогон запускался без `--cep`. Полномочия mint/freeze, расширения "
               "Token-2022 и концентрация держателей по цепи здесь отсутствуют — это «нет "
               "числа», а не ноль.", ""]
    md += ["", "## Отсечки: сколько спасла бы каждая", "",
           "| отсечка | сделок не пустим | из них были в плюсе | спасло бы SOL "
           "| потеряло бы SOL | чистыми SOL |", "|---|---|---|---|---|---|"]
    for v in sorted(отсечки, key=lambda x: -x["итог_sol_чистыми"]):
        md.append(f"| {v['отсечка']} | {v['сделок_не_пустим']} | {v['из_них_в_плюсе']} "
                  f"| {ф(v['спасло_бы_sol'])} | {ф(v['потеряло_бы_sol'])} "
                  f"| {ф(v['итог_sol_чистыми'])} |")
    лучшая = max(отсечки, key=lambda x: x["итог_sol_чистыми"])
    md += ["", "## Вывод", "",
           f"Лучшая отсечка по журналу — **{лучшая['отсечка']}**: чистыми "
           f"{ф(лучшая['итог_sol_чистыми'])} SOL за всё окно "
           f"(спасла бы {ф(лучшая['спасло_бы_sol'])}, потеряла бы {ф(лучшая['потеряло_бы_sol'])} "
           f"на {лучшая['из_них_в_плюсе']} плюсовых сделках из "
           f"{лучшая['сделок_не_пустим']} непущенных).", "",
           "Ничего не меняется и не рекомендуется — решает владелец.", ""]
    стр = КОРЕНЬ / "docs" / f"podbivka_{а.metka}_skam.md"
    стр.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(стр)
    print(f"{стр.name}: плохих {len(плохие)} на {итог['сумма_итогов_плохих_sol']} SOL, "
          f"лучшая отсечка {лучшая['отсечка']} ({лучшая['итог_sol_чистыми']})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
