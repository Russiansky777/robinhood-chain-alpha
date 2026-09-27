#!/usr/bin/env python3
"""Разметка ПРОДАЖИ (токен -> котировка) по НАСТОЯЩИМ транзакциям.

ЗАЧЕМ. Зеркальная продажа двухшаговой покупки (п.3 плана владельца) должна
собираться из тех же пулов в обратную сторону. Переворот шаблона покупки
проверен только для DLMM (FLIP_OK в c2_shadow_build); у CLMM программа
перевёрнутый шаблон отвергла 9 раз из 9 (0x1787). Значит для CPMM и Pump AMM
раскладку продажи нельзя брать наугад -- её надо СНЯТЬ с живых продаж.

Этот модуль ничего не собирает и не отправляет: он читает образцы
data/c2_pool_samples/*.json, находит в них инструкции продажи и проверяет
гипотезы о раскладке счетов по движению балансов той же транзакции.
Измерительный код -- по правилу 8 без самопроверки денежного пути.
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_swap_build as B  # noqa: E402

# Имена инструкций, чьи дискриминаторы считаем и сверяем с наблюдёнными.
# Список -- для УЗНАВАНИЯ наблюдённых байтов, а не для выдумывания формата:
# в отчёт попадает только то имя, чей дискриминатор реально встретился.
ИМЕНА = ["buy", "sell", "swap", "swap2", "swap_v2",
         "buy_exact_in", "buy_exact_out", "sell_exact_in", "sell_exact_out",
         "buy_exact_quote_in", "sell_exact_base_in", "sell_exact_quote_out",
         "swap_base_input", "swap_base_output", "swap_exact_in", "swap_exact_out"]
ПО_ДИСКРИМИНАТОРУ = {B.disc(и).hex(): и for и in ИМЕНА}


def _строки(tx):
    return {r["account"]: r for r in C.token_rows(tx).values()}


def _инструкции(tx, программа):
    for ix in B.all_instructions(tx):
        if ix.get("programId") == программа:
            yield ix


def _образцы():
    """Все транзакции всех файлов образцов, по одному разу на подпись."""
    видели = set()
    for f in sorted(B.SAMPLES_DIR.glob("*.json")):
        try:
            лист = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(лист, dict):
            лист = [x for v in лист.values() if isinstance(v, list) for x in v]
        for x in лист:
            tx = x.get("tx") if isinstance(x, dict) else None
            if not tx:
                continue
            sg = C.first_signature(tx)
            if sg in видели:
                continue
            видели.add(sg)
            yield f.name, sg, tx


def cpmm(транзакции) -> dict:
    """CPMM: раскладка вход/выход. Гипотеза -- 4 наш вход, 5 наш выход,
    6 хранилище входа, 7 хранилище выхода, 8/9 программы токена входа/выхода,
    10/11 минты входа/выхода. Проверяем по балансам ТОЙ ЖЕ транзакции."""
    итог = {"всего": 0, "по_имени": collections.Counter(), "продаж": 0, "покупок": 0,
            "гипотеза_верна": 0, "гипотеза_неверна": 0, "не_проверить": 0,
            "примеры_продаж": [], "расхождения": []}
    for имя_файла, sg, tx in транзакции:
        for ix in _инструкции(tx, B.CPMM):
            data = B.b58decode(ix["data"])
            имя = ПО_ДИСКРИМИНАТОРУ.get(data[:8].hex(), data[:8].hex())
            итог["по_имени"][имя] += 1
            a = ix["accounts"]
            if len(a) != 13:
                continue
            итог["всего"] += 1
            r = _строки(tx)
            vin, vout = r.get(a[6]), r.get(a[7])
            if not vin or not vout:
                итог["не_проверить"] += 1
                continue
            ошибки = []
            if vin["mint"] != a[10]:
                ошибки.append(f"минт хранилища входа {vin['mint'][:8]} != счёт 10 {a[10][:8]}")
            if vout["mint"] != a[11]:
                ошибки.append(f"минт хранилища выхода {vout['mint'][:8]} != счёт 11 {a[11][:8]}")
            if vin["post"] - vin["pre"] <= 0:
                ошибки.append("в хранилище входа не пришло")
            if vout["post"] - vout["pre"] >= 0:
                ошибки.append("из хранилища выхода не ушло")
            if ошибки:
                итог["гипотеза_неверна"] += 1
                if len(итог["расхождения"]) < 5:
                    итог["расхождения"].append({"sig": sg, "имя": имя, "почему": ошибки})
                continue
            итог["гипотеза_верна"] += 1
            продажа = a[11] == C.WSOL and a[10] != C.WSOL
            покупка = a[10] == C.WSOL
            итог["продаж"] += int(продажа)
            итог["покупок"] += int(покупка)
            if продажа and len(итог["примеры_продаж"]) < 5:
                итог["примеры_продаж"].append({
                    "sig": sg, "файл": имя_файла, "имя": имя,
                    "вход_минт": a[10], "выход_минт": a[11],
                    "аргументы": list(__import__("struct").unpack("<QQ", data[8:24])),
                    "хвост": data[24:].hex(), "счетов": len(a)})
    return итог


def pump_amm(транзакции) -> dict:
    """Pump AMM: раскладка НЕ зависит от направления (счета названы база и
    котировка). Гипотеза -- 3 базовый минт, 4 минт котировки, 7 базовое
    хранилище, 8 хранилище котировки; направление задаёт ДИСКРИМИНАТОР."""
    итог = {"всего": 0, "по_имени": collections.Counter(), "продаж": 0, "покупок": 0,
            "гипотеза_верна": 0, "гипотеза_неверна": 0, "не_проверить": 0,
            "имя_продажи": collections.Counter(), "имя_покупки": collections.Counter(),
            "примеры_продаж": [], "расхождения": []}
    for имя_файла, sg, tx in транзакции:
        for ix in _инструкции(tx, B.PUMP_AMM):
            data = B.b58decode(ix["data"])
            имя = ПО_ДИСКРИМИНАТОРУ.get(data[:8].hex(), data[:8].hex())
            итог["по_имени"][имя] += 1
            a = ix["accounts"]
            if len(a) < 20:
                continue
            итог["всего"] += 1
            r = _строки(tx)
            bv, qv = r.get(a[7]), r.get(a[8])
            if not bv or not qv:
                итог["не_проверить"] += 1
                continue
            ошибки = []
            if bv["mint"] != a[3]:
                ошибки.append(f"минт базового хранилища {bv['mint'][:8]} != счёт 3 {a[3][:8]}")
            if qv["mint"] != a[4]:
                ошибки.append(f"минт хранилища котировки {qv['mint'][:8]} != счёт 4 {a[4][:8]}")
            db, dq = bv["post"] - bv["pre"], qv["post"] - qv["pre"]
            if db == 0 or dq == 0 or (db > 0) == (dq > 0):
                ошибки.append(f"движение хранилищ не своп: база {db}, котировка {dq}")
            if ошибки:
                итог["гипотеза_неверна"] += 1
                if len(итог["расхождения"]) < 5:
                    итог["расхождения"].append({"sig": sg, "имя": имя, "почему": ошибки})
                continue
            итог["гипотеза_верна"] += 1
            if db > 0:      # база пришла в пул -- это продажа базы
                итог["продаж"] += 1
                итог["имя_продажи"][f"{имя}|счетов={len(a)}|хвост={data[24:].hex()}"] += 1
                if len(итог["примеры_продаж"]) < 5:
                    итог["примеры_продаж"].append({
                        "sig": sg, "файл": имя_файла, "имя": имя,
                        "база": a[3], "котировка": a[4], "счетов": len(a),
                        "аргументы": list(__import__("struct").unpack("<QQ", data[8:24])),
                        "база_в_пул": db, "котировка_из_пула": dq,
                        "хвост": data[24:].hex()})
            else:
                итог["покупок"] += 1
                итог["имя_покупки"][f"{имя}|счетов={len(a)}|хвост={data[24:].hex()}"] += 1
    return итог


def main() -> int:
    тр = list(_образцы())
    print(f"образцов транзакций: {len(тр)}")
    for имя, функция in (("CPMM", cpmm), ("Pump AMM", pump_amm)):
        о = функция(тр)
        print(f"\n=== {имя}")
        for к, v in о.items():
            if isinstance(v, collections.Counter):
                print(f"  {к}: {dict(v.most_common(12))}")
            elif isinstance(v, list):
                for x in v:
                    print(f"  {к}: {json.dumps(x, ensure_ascii=False)}")
            else:
                print(f"  {к}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
