#!/usr/bin/env python3
"""Публичный адрес из секрета. Сам секрет наружу не выходит НИКОГДА.

ЗАЧЕМ. Владелец 27.09: секрет LIVE_PRIVATE -- приватный ключ нового кошелька
полосы; "вывести из LIVE_PRIVATE публичный адрес (внутри прогона), доложить его
и ждать подтверждения владельца, что это его новый кошелёк. До подтверждения
ничего не переводить".

ЧТО ЗДЕСЬ ЕСТЬ И ЧЕГО НЕТ. Есть: разбор секрета общим разборщиком репозитория
(dbot_rescue.load_rescue_keypair -- он понимает и base58, и массив байтов),
публичный адрес, длина и вид секрета словом. Нет: печати секрета, его хвоста,
любого его куска, и нет текста исключений разбора -- в текст исключения может
попасть сам секрет, поэтому наружу идёт только имя класса.

Ни одной отправки, ни одной подписи: ключ читается только чтобы получить из
него публичный адрес.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(КОРЕНЬ / "analysis"))


def вид_секрета(с: str) -> str:
    """Каким видом записан секрет. Ни одного знака самого секрета."""
    т = с.strip()
    if т.startswith("[") and т.endswith("]"):
        return "массив байтов JSON"
    if all(ч in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
           for ч in т):
        return "base58"
    return "неопознанный вид"


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--env", default="LIVE_PRIVATE",
                    help="имя переменной окружения с секретом")
    р.add_argument("--out", default=None)
    а = р.parse_args()

    сырое = (os.environ.get(а.env) or "").strip()
    итог = {"переменная": а.env, "ok": False, "адрес": None, "почему": None}
    if not сырое:
        итог["почему"] = f"{а.env} не задан в окружении -- выводить адрес не из чего"
        print(json.dumps(итог, ensure_ascii=False, indent=1))
        return 1
    итог["длина_секрета"] = len(сырое)
    итог["вид_секрета"] = вид_секрета(сырое)
    try:
        from dbot_rescue import load_rescue_keypair  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        итог["почему"] = f"разборщик ключа не загружен: {type(exc).__name__}"
        print(json.dumps(итог, ensure_ascii=False, indent=1))
        return 1
    try:
        kp = load_rescue_keypair(сырое)
        адрес = str(kp.pubkey())
    except Exception as exc:  # noqa: BLE001
        # ТЕКСТ ИСКЛЮЧЕНИЯ НЕ ПЕЧАТАЕМ: в него может попасть сам секрет.
        итог["почему"] = f"ключ не разобрался ({type(exc).__name__})"
        print(json.dumps(итог, ensure_ascii=False, indent=1))
        return 1
    # Проверка наизнанку: адрес обязан быть base58 длиной 32 байта. Если разбор
    # вернул что-то иное, докладывать такой "адрес" владельцу нельзя.
    if not (32 <= len(адрес) <= 44):
        итог["почему"] = f"полученный адрес не похож на адрес Solana (длина {len(адрес)})"
        print(json.dumps(итог, ensure_ascii=False, indent=1))
        return 1
    итог.update(ok=True, адрес=адрес)
    print(json.dumps(итог, ensure_ascii=False, indent=1))
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
