#!/usr/bin/env python3
"""Лидер по цепи: все его подписи с заданного времени -- число и программы. Только чтение.

Зачем: архив PumpApi держит не все типы пулов, поэтому «в архиве сделок нет» не равно «сделок не было».
Этот счёт идёт по цепи: getSignaturesForAddress постранично назад до --s, затем сами транзакции пакетами
(getTransaction, maxSupportedTransactionVersion по правилу подбивки) -- и по каждой считаются программы
верхнего уровня и вложенных инструкций, упавшие отдельно.

Выход: data/podbivka/lider_po_cepi_<адрес[:8]>_<с>.json + строка в журнал прогона.
"""
from __future__ import annotations

import argparse
import calendar
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ИМЕНА = {"6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "кривая pump.fun",
         "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
         "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
         "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
         "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
         "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
         "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
         "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora DAMM v1",
         "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2",
         "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
         "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
         "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6",
         "JUPV1on9oBqZkHcbGvAwrbeYiVv5sqWdTDWRXNuWnJT": "Jupiter",
         "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "Token",
         "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022",
         "ComputeBudget111111111111111111111111111111": "ComputeBudget",
         "11111111111111111111111111111111": "System",
         "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "AssociatedToken"}
ПУЛОВЫЕ = {k for k, v in ИМЕНА.items() if v not in ("Token", "Token-2022", "ComputeBudget", "System",
                                                     "AssociatedToken")}


def программы(т: dict) -> set:
    """Программы верхнего уровня и вложенных инструкций транзакции."""
    из_ = set()
    с = (т or {}).get("transaction") or {}
    сообщ = с.get("message") or {}
    ключи = сообщ.get("accountKeys") or []
    плоские = [k if isinstance(k, str) else (k or {}).get("pubkey") for k in ключи]
    for и in сообщ.get("instructions") or []:
        if isinstance(и, dict):
            if и.get("programId"):
                из_.add(и["programId"])
            elif и.get("programIdIndex") is not None and и["programIdIndex"] < len(плоские):
                из_.add(плоские[и["programIdIndex"]])
    for гр in ((т.get("meta") or {}).get("innerInstructions") or []):
        for и in гр.get("instructions") or []:
            if isinstance(и, dict):
                if и.get("programId"):
                    из_.add(и["programId"])
                elif и.get("programIdIndex") is not None and и["programIdIndex"] < len(плоские):
                    из_.add(плоские[и["programIdIndex"]])
    return {x for x in из_ if x}


def разбор_упоминания(т: dict, адрес: str) -> dict:
    """Чем транзакция привязана к адресу: подписант / владелец токен-счёта / только упомянут.

    Архив PumpApi привязывает событие к кошельку только по txSigner или breakdown[].trader, поэтому
    транзакции, где адрес лишь упомянут (доля создателя минта, получатель чаевых, счёт в маршруте),
    в архиве вообще не появляются. Здесь это и считается по цепи.
    """
    с = (т or {}).get("transaction") or {}
    сообщ = с.get("message") or {}
    ключи = сообщ.get("accountKeys") or []
    плоские = [k if isinstance(k, str) else (k or {}).get("pubkey") for k in ключи]
    подписант = any(isinstance(k, dict) and k.get("signer") and k.get("pubkey") == адрес for k in ключи)
    м = (т or {}).get("meta") or {}
    пре = {(b.get("accountIndex"), b.get("mint")): b for b in (м.get("preTokenBalances") or [])}
    пост = {(b.get("accountIndex"), b.get("mint")): b for b in (м.get("postTokenBalances") or [])}
    владелец = получил = False
    for кл in set(пре) | set(пост):
        a, b = пре.get(кл), пост.get(кл)
        if ((b or a or {}).get("owner")) != адрес:
            continue
        владелец = True
        до = float(((a or {}).get("uiTokenAmount") or {}).get("uiAmountString") or 0)
        после = float(((b or {}).get("uiTokenAmount") or {}).get("uiAmountString") or 0)
        получил = получил or после > до
    лам, пре_л, пост_л = 0, м.get("preBalances") or [], м.get("postBalances") or []
    for и, p in enumerate(плоские):
        if p == адрес and и < len(пре_л) and и < len(пост_л):
            лам += пост_л[и] - пре_л[и]
    return {"в_ключах": адрес in плоские, "подписант": подписант, "владелец_токенсчёта": владелец,
            "получил_токен": получил, "sol": round(лам / 1e9, 9)}


def цепь_адреса(уз, адрес: str, с_ts: int, до_ts, страниц_макс: int, разбор: bool = False) -> dict:
    """Подписи адреса в окне и программы по ним (пакетное чтение транзакций)."""
    подписи, до, страниц = [], None, 0
    while страниц < страниц_макс:
        стр = уз.подписи(адрес, до=до, limit=1000)
        страниц += 1
        if not стр:
            break
        подписи += [з for з in стр if (з.get("blockTime") or 0) >= с_ts
                    and (до_ts is None or (з.get("blockTime") or 0) < до_ts)]
        if (стр[-1].get("blockTime") or 0) < с_ts or len(стр) < 1000:
            break
        до = стр[-1]["signature"]
    удачных = [з for з in подписи if з.get("err") is None]
    txs = уз.пакет([з["signature"] for з in удачных],
                   {з["signature"]: з.get("blockTime") for з in удачных}) if удачных else {}
    пул = collections.Counter()
    с_пулом, не_прочитано = 0, 0
    р = {"подписант": 0, "упомянут": 0, "упомянут_владелец_получившего": 0, "упомянут_владелец_без_прихода": 0,
         "только_упомянут": 0, "только_упомянут_с_sol": 0, "только_упомянут_sol": 0.0,
         "подписант_без_пула": 0, "только_упомянут_без_пула": 0, "примеры": []}
    for з in удачных:
        т = txs.get(з["signature"]) or {}
        if not т:
            не_прочитано += 1
            continue
        пр = программы(т) & ПУЛОВЫЕ
        if пр:
            с_пулом += 1
            for x in пр:
                пул[x] += 1
        if not разбор:
            continue
        к = разбор_упоминания(т, адрес)
        если_пул = bool(пр)
        if к["подписант"]:
            р["подписант" if если_пул else "подписант_без_пула"] += 1
            continue
        if not если_пул:
            р["только_упомянут_без_пула"] += 1 if not к["владелец_токенсчёта"] else 0
            continue
        р["упомянут"] += 1
        if к["получил_токен"]:
            р["упомянут_владелец_получившего"] += 1
        elif к["владелец_токенсчёта"]:
            р["упомянут_владелец_без_прихода"] += 1
        else:
            р["только_упомянут"] += 1
            р["только_упомянут_sol"] = round(р["только_упомянут_sol"] + к["sol"], 9)
            if к["sol"] > 0:
                р["только_упомянут_с_sol"] += 1
            if len(р["примеры"]) < 3:
                р["примеры"].append({"signature": з["signature"], "sol": к["sol"],
                                     "программы": sorted(ИМЕНА.get(x) or x[:8] for x in пр)})
    врем = [з.get("blockTime") or 0 for з in подписи if з.get("blockTime")]
    из_ = {"подписей": len(подписи), "упавших": len(подписи) - len(удачных), "не_прочитано": не_прочитано,
           "с_пулом": с_пулом, "пулы": {(ИМЕНА.get(k) or k[:8]): n for k, n in пул.most_common()},
           "последняя_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(врем))) if врем else None}
    if разбор:
        из_["разбор"] = р
    return из_


def по_группам(а, с_ts: int, до_ts) -> int:
    """Таблица «архив N / цепь N» по адресам живых групп."""
    import gzip  # noqa: PLC0415
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    реестр = json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    гр = {x for x in а.gruppy.split(",") if x}
    цель = sorted(a for a, v in реестр.items() if set((v or {}).get("группы") or []) & гр)
    арх = {}
    if а.arhiv:
        д = json.loads(gzip.decompress((П / "arhiv_den" / f"{а.arhiv}.json.gz").read_bytes()))
        for e in д.get("наши_события") or []:
            if e.get("action") == "buy":
                арх[e["trader"]] = арх.get(e["trader"], 0) + 1
        del д
    уз = S.Узел()
    строки = []
    with уз.на("helius"):
        for a in цель:
            r = цепь_адреса(уз, a, с_ts, до_ts, а.stranic, разбор=bool(а.razbor))
            r.update(адрес=a, группы=[x for x in ((реестр.get(a) or {}).get("группы") or []) if x != "log_only"],
                     архив_покупок=арх.get(a, 0))
            строки.append(r)
            хвост = ""
            if r.get("разбор"):
                p = r["разбор"]
                хвост = (f"; подписант {p['подписант']}, упомянут {p['упомянут']} (владелец получившего "
                         f"{p['упомянут_владелец_получившего']}, только упомянут {p['только_упомянут']}, "
                         f"из них с приходом SOL {p['только_упомянут_с_sol']} на {p['только_упомянут_sol']:+.6f})")
            print(f"{a[:8]} {','.join(r['группы'])}: цепь подписей {r['подписей']} (с пулом {r['с_пулом']}), "
                  f"архив покупок {r['архив_покупок']}, пулы {r['пулы']}{хвост}", flush=True)
    out = П / (f"cep_razbor_{а.s[:10]}.json" if а.razbor else f"cep_vs_arhiv_{а.s[:10]}.json")
    out.write_text(json.dumps({"с_utc": а.s, "до_utc": а.do or "сейчас", "архив": а.arhiv,
                               "группы": sorted(гр), "адресов": len(цель), "строки": строки,
                               "расход": уз.расход()}, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    тер = [r for r in строки if r["с_пулом"] and not r["архив_покупок"]]
    print(f"адресов {len(строки)}; теряем целиком (цепь с пулом > 0, архив 0): {len(тер)}", flush=True)
    return 0


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--adres", default="", help="один адрес")
    р.add_argument("--gruppy", default="", help="через запятую: группы реестра (lane_s0,batch5,cand1,cand2,leader)")
    р.add_argument("--s", required=True, help="с какого UTC: YYYY-MM-DDTHH:MM:SSZ")
    р.add_argument("--do", default="", help="до какого UTC (по умолчанию -- до сейчас)")
    р.add_argument("--arhiv", default="", help="метка суточного файла архива для сравнения (den_2026-10-01T06)")
    р.add_argument("--stranic", type=int, default=40, help="предел страниц по 1000 подписей")
    р.add_argument("--razbor", action="store_true",
                   help="с --gruppy: по каждой пуловой транзакции -- подписант / владелец токен-счёта / только упомянут")
    а = р.parse_args()
    с_ts = calendar.timegm(time.strptime(а.s, "%Y-%m-%dT%H:%M:%SZ"))
    до_ts = calendar.timegm(time.strptime(а.do, "%Y-%m-%dT%H:%M:%SZ")) if а.do else None
    if а.gruppy:
        return по_группам(а, с_ts, до_ts)
    if not а.adres:
        raise SystemExit("нужен --adres или --gruppy")
    уз = S.Узел()
    подписи, до, страниц = [], None, 0
    with уз.на("helius"):
        while страниц < а.stranic:
            стр = уз.подписи(а.adres, до=до, limit=1000)
            страниц += 1
            if not стр:
                break
            подписи += [з for з in стр if (з.get("blockTime") or 0) >= с_ts
                        and (до_ts is None or (з.get("blockTime") or 0) < до_ts)]
            if (стр[-1].get("blockTime") or 0) < с_ts or len(стр) < 1000:
                break
            до = стр[-1]["signature"]
        упавших = sum(1 for з in подписи if з.get("err") is not None)
        удачных = [з for з in подписи if з.get("err") is None]
        txs = уз.пакет([з["signature"] for з in удачных],
                       {з["signature"]: з.get("blockTime") for з in удачных}) if удачных else {}
    счёт_прог = collections.Counter()
    пуловые = collections.Counter()
    не_прочитано = 0
    for з in удачных:
        т = txs.get(з["signature"])
        if not т:
            не_прочитано += 1
            continue
        пр = программы(т)
        for x in пр:
            счёт_прог[x] += 1
        for x in пр & ПУЛОВЫЕ:
            пуловые[x] += 1
    врем = [з.get("blockTime") or 0 for з in подписи if з.get("blockTime")]
    из_ = {"адрес": а.adres, "с_utc": а.s, "снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "страниц": страниц, "подписей": len(подписи), "упавших": упавших, "не_прочитано": не_прочитано,
           "первая_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(min(врем))) if врем else None,
           "последняя_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(врем))) if врем else None,
           "программы": [{"адрес": k, "имя": ИМЕНА.get(k), "транзакций": n} for k, n in счёт_прог.most_common()],
           "пуловые_программы": [{"адрес": k, "имя": ИМЕНА.get(k), "транзакций": n} for k, n in пуловые.most_common()],
           "подписи": [{"signature": з["signature"], "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(з["blockTime"]))
                        if з.get("blockTime") else None, "err": з.get("err") is not None,
                        "программы": sorted(программы(txs.get(з["signature"]) or {}) & ПУЛОВЫЕ)} for з in подписи],
           "расход": уз.расход()}
    out = П / f"lider_po_cepi_{а.adres[:8]}_{а.s[:13].replace(':', '')}.json"
    out.write_text(json.dumps(из_, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    print(f"{а.adres[:8]} с {а.s}: подписей {len(подписи)} (упавших {упавших}, не прочитано {не_прочитано}), "
          f"первая {из_['первая_utc']}, последняя {из_['последняя_utc']}; пуловые программы: "
          + (", ".join(f"{(ИМЕНА.get(x['адрес']) or x['адрес'][:8])} {x['транзакций']}" for x in из_["пуловые_программы"]) or "нет")
          + "; все программы: "
          + ", ".join(f"{(ИМЕНА.get(x['адрес']) or x['адрес'][:8])} {x['транзакций']}" for x in из_["программы"][:12]),
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
