#!/usr/bin/env python3
"""II.8 -- ДОБОР НАЛОГОВОГО СТАТУСА МИНТОВ. Только чтение цепи.

ЗАЧЕМ. В прогоне A3 (data/a2_a1a3.json) из 346 сигналов посчитано 132, а 214
помечены missing. Разбор причин по строкам A1:
  * 107 -- "налоговый статус минта неизвестен (нет в transfer_fee_audit)";
  *  96 -- "нет spot_after" (тип пула не даёт спот по остаткам хранилищ);
  *  11 -- "нет growth_after_30s".
Первая причина -- не свойство данных, а пробел справочника: аудит налога
собирался 23.09 по нашим сделкам и знает 338 минтов, а в кэше толпы их 273, из
которых 141 в аудит не попал (вместе с котировочными -- 173).

ЧТО ДЕЛАЕТ. Читает из цепи сам минт (getAccountInfo, encoding jsonParsed) и
берёт ставку комиссии на перевод из расширения transferFeeConfig -- ровно тем
же кодом, что и аудит 23.09 (analysis/solana_transfer_fee_audit.mint_info),
чтобы числа были одной природы. Ничего не достраивает: минт, который цепь не
отдала, так и остаётся неизвестным, и это записано словами.

ЧЕГО НЕ ДЕЛАЕТ. Не трогает data/solana_transfer_fee_audit.json: тот файл --
измерение 23.09, и переписывать его добором нельзя. Добор ложится отдельным
файлом, а расчёт A3 склеивает их, отдавая приоритет исходному аудиту.

Кредиты: getAccountInfo -- самый дешёвый вызов; 173 минта это меньше тысячи
кредитов при ночном бюджете 150 000.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

WSOL = "So11111111111111111111111111111111111111112"


def нужные_минты(кэш: dict, аудит: dict) -> list:
    """Минты и котировочные минты из кэша толпы, которых нет в аудите.

    Котировочный минт нужен потому, что налог считается по ОБОИМ концам сделки
    (a2_common.налог_сделки): неизвестный котировочный минт делает сигнал
    непосчитанным так же, как неизвестный основной. WSOL не проверяем -- он
    классический SPL и налога не имеет по определению.
    """
    из_ = set()
    for и in (кэш or {}).get("per_source") or []:
        for с in и.get("trades") or []:
            for м in (с.get("mint"), с.get("quote_mint")):
                if м and м != WSOL and м not in аудит:
                    из_.add(м)
    return sorted(из_)


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--crowd", default=str(КОРЕНЬ / "data" / "a2_crowd_metric_2026-09-24.json"))
    р.add_argument("--audit", default=str(КОРЕНЬ / "data" / "solana_transfer_fee_audit.json"))
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "a2_mint_tax_fill.json"))
    р.add_argument("--limit", type=int, default=0, help="0 -- все")
    а = р.parse_args()

    кэш = json.loads(Path(а.crowd).read_text(encoding="utf-8"))
    аудит = (json.loads(Path(а.audit).read_text(encoding="utf-8")) or {}).get("минты") or {}
    нужны = нужные_минты(кэш, аудит)
    if а.limit:
        нужны = нужны[:а.limit]
    print(f"минтов в аудите: {len(аудит)}; добрать: {len(нужны)}")
    if not нужны:
        print("добирать нечего")
        return 0

    from solana_crowd_scan import Rpc, helius_key  # noqa: PLC0415
    from solana_transfer_fee_audit import mint_info  # noqa: PLC0415

    ключ, имя = helius_key()
    print(f"ключ Helius из {имя} (значение не печатается)")
    rpc = Rpc(ключ, service="a2_mint_tax_fill")

    начало = time.time()
    собрано: dict = {}
    не_вышло: list = []
    for и, м in enumerate(нужны, 1):
        з = mint_info(rpc, м)
        if з.get("почему") or з.get("программа_токена_id") is None:
            не_вышло.append({"минт": м, "почему": з.get("почему") or "цепь не отдала минт"})
        собрано[м] = з
        if и % 25 == 0 or и == len(нужны):
            print(f"  {и}/{len(нужны)}, вызовов {rpc.calls}, {time.time() - начало:.0f} с")

    таксируемых = sum(1 for з in собрано.values() if з.get("таксируемый"))
    t2022 = sum(1 for з in собрано.values()
                if "Token-2022" in str(з.get("программа_токена") or ""))
    итог = {
        "собрано_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "источник": "getAccountInfo encoding=jsonParsed, ставка из расширения transferFeeConfig",
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Это ДОБОР к аудиту 23.09, а не его замена: при склейке приоритет у исходного аудита.",
            "Минт, который цепь не отдала, остаётся неизвестным и в расчёт не входит -- это НЕ ноль.",
            "Ставка читается из самого минта на МОМЕНТ ДОБОРА. Если эмитент менял ставку после "
            "сделки, к сделке относится прежняя, и такой случай виден по полю "
            "ставка_комиссии_прежняя_bps.",
        ],
        "минтов": len(собрано),
        "таксируемых": таксируемых,
        "token_2022": t2022,
        "не_вышло": не_вышло,
        "вызовов_rpc": rpc.calls,
        "секунд": round(time.time() - начало, 1),
        "минты": собрано,
    }
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"добрано минтов: {len(собрано)}; таксируемых: {таксируемых}; "
          f"Token-2022: {t2022}; не вышло: {len(не_вышло)}; вызовов {rpc.calls}")
    for з in не_вышло[:10]:
        print(f"  не вышло: {з['минт']} -- {з['почему']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
