#!/usr/bin/env python3
"""Образцы продаж токен→SOL для Code-3: по 6 подписей на тип пула, полные тела транзакций. Только чтение.

Слово владельца 02.10: по 6 подписей продаж токен→SOL за 30.09–01.10 для кривой pump.fun, LaunchLab, DAMM v1,
DAMM v2 и CPMM; полные тела -- getTransaction в двух видах (encoding jsonParsed и base64 «как есть»),
maxSupportedTransactionVersion 0. Выход -- data/samples/prodazhi/<тип>.json (ветка claude/podbivka).

Подписи берутся из суточного архива PumpApi data/podbivka/arhiv_den/den_2026-09-30T06.json.gz (30.09 06Z → 01.10 06Z):
события наших адресов с action=sell и котировкой WSOL. По каждому типу берутся продажи РАЗНЫХ кошельков и РАЗНЫХ
минтов (сначала самые крупные по SOL), читается больше шести -- в файл попадают первые шесть, что прочитались обоими
видами; непрочитанные остаются в разделе «не_вышло» с причиной.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ВЫХОД = КОРЕНЬ / "data" / "samples" / "prodazhi"
WSOL = "So11111111111111111111111111111111111111112"
АРХИВ = "den_2026-09-30T06"
НУЖНО = 6
ПРЕДЕЛ_КАНД = 200       # читаем кандидатов с запасом: транзакции версии 1 при maxSupportedTransactionVersion 0
                        # не отдаются узлом, а их много (у LaunchLab из 18 первых кандидатов годных было 3)
ТИПЫ = {"pump": ("krivaya_pump_fun", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"),
        "raydium-launchpad": ("launchlab", "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"),
        "meteora-damm-v1": ("damm_v1", "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB"),
        "meteora-damm-v2": ("damm_v2", "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"),
        "raydium-cpmm": ("cpmm", "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C")}


def отобрать(события: list, тип: str) -> list:
    """Продажи этого типа: разные кошельки и разные минты, крупные сперва; больше чем нужно -- на отсев при чтении."""
    сс = [e for e in события if e.get("action") == "sell" and e.get("quoteMint") == WSOL and e.get("pool") == тип
          and e.get("signature") and (e.get("sol_экв") or 0) > 0]
    сс.sort(key=lambda e: -(e.get("sol_экв") or 0))
    из_, кош, минты = [], set(), set()
    for e in сс:
        if e.get("trader") in кош or e.get("mint") in минты:
            continue
        кош.add(e.get("trader"))
        минты.add(e.get("mint"))
        из_.append(e)
        if len(из_) >= ПРЕДЕЛ_КАНД:
            break
    if len(из_) < ПРЕДЕЛ_КАНД:                # мало разных кошельков -- добираем любыми, но разными подписями
        видел = {e["signature"] for e in из_}
        for e in сс:
            if e["signature"] not in видел:
                из_.append(e)
                видел.add(e["signature"])
            if len(из_) >= ПРЕДЕЛ_КАНД:
                break
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--tipy", default="", help="через запятую: только эти выходные имена (krivaya_pump_fun, launchlab, damm_v1, damm_v2, cpmm)")
    а = р.parse_args()
    только = {x for x in а.tipy.split(",") if x}
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    д = json.loads(gzip.decompress((П / "arhiv_den" / f"{АРХИВ}.json.gz").read_bytes()))
    события = д["наши_события"]
    уз = S.Узел()
    ВЫХОД.mkdir(parents=True, exist_ok=True)
    итоги = {}
    with уз.на("helius"):
        for тип, (имя, программа) in ТИПЫ.items():
            if только and имя not in только:
                continue
            канд = отобрать(события, тип)
            готово, не_вышло = [], []
            for e in канд:
                if len(готово) >= НУЖНО:
                    break
                тела = {}
                почему = None
                for вид in ("jsonParsed", "base64"):
                    try:
                        т = уз.вызов("getTransaction", [e["signature"], {"encoding": вид, "commitment": "confirmed",
                                                                         "maxSupportedTransactionVersion": 0}], срок=60.0)
                        if not т:
                            почему = f"{вид}: узел вернул пусто"
                            break
                        тела[вид] = т
                    except RuntimeError as exc:
                        почему = f"{вид}: {str(exc)[:200]}"
                        break
                if почему or len(тела) != 2:
                    не_вышло.append({"signature": e["signature"], "почему": почему or "прочитан не полностью"})
                    continue
                есть_программа = программа in json.dumps(тела["jsonParsed"], ensure_ascii=False)
                готово.append({"signature": e["signature"], "тип_пула_архива": тип, "программа_пула": программа,
                               "программа_в_транзакции": есть_программа,
                               "кошелёк": e.get("trader"), "mint": e.get("mint"), "poolId": e.get("poolId"),
                               "slot": e.get("block"), "timestamp_ms": e.get("timestamp"),
                               "токенов_продано": e.get("tokens"), "sol_получено_оценка_архива": e.get("sol_экв"),
                               "tx_jsonParsed": тела["jsonParsed"], "tx_base64": тела["base64"]})
            out = ВЫХОД / f"{имя}.json"
            out.write_text(json.dumps({
                "что": f"продажи токен→SOL на {имя} ({тип}), по {НУЖНО} образцов: полные тела getTransaction "
                       "в двух видах -- encoding jsonParsed и base64 (как есть), maxSupportedTransactionVersion 0",
                "программа_пула": программа, "тип_пула_архива": тип,
                "откуда_подписи": f"архив PumpApi data/podbivka/arhiv_den/{АРХИВ}.json.gz "
                                  "(30.09 06:00Z → 01.10 06:00Z), события наших адресов action=sell, котировка WSOL",
                "образцов": len(готово), "не_вышло": не_вышло,
                "программа_найдена_в_телах": sum(1 for x in готово if x["программа_в_транзакции"]),
                "образцы": готово},
                ensure_ascii=False, indent=1), encoding="utf-8")
            R.записано(out)
            итоги[имя] = {"образцов": len(готово), "не_вышло": len(не_вышло), "кандидатов": len(канд)}
            print(имя, итоги[имя], flush=True)
    print(json.dumps({"итоги": итоги, "расход": уз.расход()}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
