#!/usr/bin/env python3
"""Живой файл групп прочитан ТЕМ ЖЕ модулем, что у службы: числа и хэш.

ЗАЧЕМ. Файл групп решает, чей сигнал покупается и каким размером. После замены
файла недостаточно увидеть "скопировано": надо прочитать его тем же кодом,
которым читает служба, и сверить числа с задуманными. Не сошлось -- прогон
обязан вернуть файл из копии, а не оставить бой в неизвестном состоянии.

Печатает JSON: хэш, mtime, счёт адресов по группам и политики торгующих групп.
Ни одной записи, ни одной отправки.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--fayl", default="", help="путь к файлу групп (пусто -- из окружения)")
    р.add_argument("--zhdem-hash", default="", help="ожидаемый хэш (12 знаков)")
    р.add_argument("--zhdem-torguyushchih", default="",
                   help="ожидаемые торгующие группы через запятую")
    а = р.parse_args()

    try:
        import bloom_source_groups as SG
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False,
                           "почему": f"модуль групп не загрузился: {type(exc).__name__}"},
                          ensure_ascii=False))
        return 1
    д = SG.загрузить(а.fayl or None, заново=True)
    свод = {"ok": д.get("why_not") is None, "file": д.get("file"),
             "hash": д.get("hash"), "mtime_utc": д.get("mtime_utc"),
             "почему": д.get("why_not"), "тревога": д.get("тревога")}
    счёт: dict = {}
    for _, г in (д.get("по_адресу") or {}).items():
        счёт[г] = счёт.get(г, 0) + 1
    свод["адресов_по_группам"] = счёт
    торгующие = sorted(и for и, п in (д.get("политики") or {}).items()
                       if п.get("lane_trades"))
    свод["торгуют_полосой"] = торгующие
    свод["торгуют_площадкой"] = sorted(и for и, п in (д.get("политики") or {}).items()
                                        if п.get("bloom_trades"))
    свод["политики_торгующих"] = {и: {к: (д["политики"][и] or {}).get(к)
                                       for к in ("lane_size", "min_target_sol",
                                                  "slippage", "slippage_thin_pool",
                                                  "thin_pool_below_sol",
                                                  "min_pool_sol_reserve",
                                                  "max_slots_from_source",
                                                  "skip_flippers", "allow_taxed_route",
                                                  "lane_open_max", "stop_loss_sol",
                                                  "hold_slots", "lane_pools")}
                                   for и in торгующие}
    беды = []
    if свод["почему"]:
        беды.append(свод["почему"])
    if а.zhdem_hash and свод.get("hash") != а.zhdem_hash:
        беды.append(f"хэш {свод.get('hash')} вместо ожидаемого {а.zhdem_hash}")
    if а.zhdem_torguyushchih:
        ждём = sorted(x.strip() for x in а.zhdem_torguyushchih.split(",") if x.strip())
        if торгующие != ждём:
            беды.append(f"торгуют полосой {торгующие} вместо {ждём}")
    if свод["торгуют_площадкой"]:
        беды.append(f"площадка торгует по группам {свод['торгуют_площадкой']}, "
                     "а по слову владельца не должна ни по одной")
    свод["беды"] = беды
    свод["ok"] = not беды
    print(json.dumps(свод, ensure_ascii=False, indent=1))
    return 0 if not беды else 1


if __name__ == "__main__":
    sys.exit(main())
