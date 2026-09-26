#!/usr/bin/env python3
"""Имя токена по минту -- для строки Telegram (формат владельца 26.09).

ЗАЧЕМ. В строке BUY владелец просит ИМЯ токена, а не адрес минта; нет имени --
стоит имя источника. Значит имя надо где-то взять, и взять ЧЕСТНО: из цепи.

ОТКУДА. Счёт метаданных Metaplex (Token Metadata program): его адрес -- PDA по
сеидам ("metadata", программа, минт), а в данных счёта после трёх адресов лежат
строки name / symbol / uri в формате Borsh (u32 длина, затем байты). Если счёта
нет (у токена метаданных может не быть вовсе) -- пробуется DAS getAsset того же
узла, и только потом возвращается None. Ничего не выдумывается: не нашли -- нет.

ГДЕ ЗОВЁТСЯ. На пути ДОКЛАДА, после посадки покупки по цепи, и только один раз
на минт: имена кэшируются в памяти. На горячем пути (решение -> отправка) этого
вызова нет и быть не может -- это сетевой запрос.
"""
from __future__ import annotations

import base64
import re
import threading

# Адрес программы Token Metadata -- дословно из документации Metaplex.
ПРОГРАММА_МЕТАДАННЫХ = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
# Смещение до строки name в счёте метаданных: key (1) + update_authority (32)
# + mint (32) = 65 байт, дальше Borsh-строка name.
СМЕЩЕНИЕ_ИМЕНИ = 65
ПРЕДЕЛ_ДЛИНЫ = 64

_КЭШ: dict = {}
_ЗАМОК = threading.Lock()


def _чисто(имя: str | None) -> str | None:
    """Имя из цепи -- чужие байты: режем нули, невидимки и длину.

    Пустое после чистки -- это отсутствие имени, а не пустая строка: иначе в
    сообщении стояло бы пустое место вместо имени источника.
    """
    if not имя:
        return None
    т = имя.replace("\x00", "").strip()
    т = re.sub(r"[\x00-\x1f\x7f]", "", т)
    т = т[:ПРЕДЕЛ_ДЛИНЫ].strip()
    return т or None


def адрес_метаданных(минт: str) -> str | None:
    """PDA счёта метаданных. Без solders -- None (и тогда работает только DAS)."""
    try:
        from solders.pubkey import Pubkey  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        прог = Pubkey.from_string(ПРОГРАММА_МЕТАДАННЫХ)
        адрес, _ = Pubkey.find_program_address(
            [b"metadata", bytes(прог), bytes(Pubkey.from_string(минт))], прог)
        return str(адрес)
    except Exception:  # noqa: BLE001
        return None


def имя_из_данных(данные: bytes) -> str | None:
    """Borsh-строка name со смещения 65. Короткие данные -- None, без догадок."""
    if not данные or len(данные) < СМЕЩЕНИЕ_ИМЕНИ + 4:
        return None
    длина = int.from_bytes(данные[СМЕЩЕНИЕ_ИМЕНИ:СМЕЩЕНИЕ_ИМЕНИ + 4], "little")
    if длина <= 0 or длина > 200:
        return None
    конец = СМЕЩЕНИЕ_ИМЕНИ + 4 + длина
    if конец > len(данные):
        return None
    try:
        return _чисто(данные[СМЕЩЕНИЕ_ИМЕНИ + 4:конец].decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def имя(минт: str | None, rpc_call) -> str | None:
    """Имя токена или None. Результат (в том числе "нет имени") кэшируется:
    второй запрос по тому же минту сети не касается."""
    if not минт or rpc_call is None:
        return None
    with _ЗАМОК:
        if минт in _КЭШ:
            return _КЭШ[минт]
    найдено = None
    адрес = адрес_метаданных(минт)
    if адрес:
        try:
            от = rpc_call("getAccountInfo",
                          [адрес, {"encoding": "base64", "commitment": "confirmed"}])
            значение = (от or {}).get("value") if isinstance(от, dict) else None
            данные = ((значение or {}).get("data") or [None])[0]
            if данные:
                найдено = имя_из_данных(base64.b64decode(данные))
        except Exception:  # noqa: BLE001
            найдено = None
    if not найдено:
        # DAS того же узла: у части токенов метаданных Metaplex нет, но узел
        # знает имя из расширения токена. Отказ DAS -- не ошибка сделки.
        try:
            от = rpc_call("getAsset", {"id": минт})
            мета = (((от or {}).get("content") or {}).get("metadata") or {})
            найдено = _чисто(мета.get("name"))
        except Exception:  # noqa: BLE001
            найдено = None
    with _ЗАМОК:
        _КЭШ[минт] = найдено
    return найдено


def сброс_кэша() -> None:
    """Только для самопроверки: боевой путь кэш не чистит."""
    with _ЗАМОК:
        _КЭШ.clear()


def self_test() -> int:
    пройдено = провалено = 0

    def chk(что, ок, факт=""):
        nonlocal пройдено, провалено
        if ок:
            пройдено += 1
            print(f"  [ok  ] {что}")
        else:
            провалено += 1
            print(f"  [ПРОВАЛ] {что}: {факт}")

    # Разбор Borsh: имя "MOON" со смещения 65.
    данные = bytes(СМЕЩЕНИЕ_ИМЕНИ) + (4).to_bytes(4, "little") + b"MOON" + bytes(8)
    chk("имя читается со смещения 65", имя_из_данных(данные) == "MOON",
        имя_из_данных(данные))
    chk("короткие данные -- None, а не пустая строка",
        имя_из_данных(b"\x00" * 10) is None)
    chk("нули и невидимки вычищены",
        имя_из_данных(bytes(СМЕЩЕНИЕ_ИМЕНИ) + (8).to_bytes(4, "little")
                       + b"AB\x00\x01CD\x00\x00") == "ABCD",
        имя_из_данных(bytes(СМЕЩЕНИЕ_ИМЕНИ) + (8).to_bytes(4, "little")
                       + b"AB\x00\x01CD\x00\x00"))
    chk("длина режется до предела",
        len(_чисто("Я" * 200) or "") == ПРЕДЕЛ_ДЛИНЫ)
    # PDA -- настоящий адрес, а не выдумка: он воспроизводим и не равен минту.
    минт = "So11111111111111111111111111111111111111112"
    а = адрес_метаданных(минт)
    chk("PDA считается и отличается от минта", а is None or а != минт, а)
    chk("PDA воспроизводим", а == адрес_метаданных(минт), а)

    # Сеть подменяется: проверяется кэш и порядок путей.
    сброс_кэша()
    вызовы = []

    def узел(метод, параметры=None, **кв):
        вызовы.append(метод)
        if метод == "getAccountInfo":
            д = base64.b64encode(bytes(СМЕЩЕНИЕ_ИМЕНИ)
                                  + (3).to_bytes(4, "little") + b"DOG").decode()
            return {"value": {"data": [д, "base64"]}}
        raise RuntimeError("не должно понадобиться")

    chk("имя берётся из счёта метаданных", имя(минт, узел) == "DOG")
    chk("второй раз сеть не трогается", имя(минт, узел) == "DOG"
        and вызовы.count("getAccountInfo") == 1, вызовы)

    сброс_кэша()
    вызовы2 = []

    def узел2(метод, параметры=None, **кв):
        вызовы2.append(метод)
        if метод == "getAccountInfo":
            return {"value": None}
        if метод == "getAsset":
            return {"content": {"metadata": {"name": "CAT "}}}
        raise RuntimeError("неизвестный метод")

    chk("нет счёта метаданных -- пробуется DAS", имя(минт, узел2) == "CAT",
        вызовы2)
    сброс_кэша()

    def узел3(метод, параметры=None, **кв):
        raise RuntimeError("узел молчит")

    chk("узел молчит -- None, и это кэшируется",
        имя(минт, узел3) is None and имя(минт, узел3) is None)
    chk("без минта и без узла -- None",
        имя(None, узел) is None and имя(минт, None) is None)
    print(f"самопроверка имени токена: {пройдено}/{пройдено + провалено} пройдено")
    return 0 if провалено == 0 else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(self_test() if "--self-test" in sys.argv else 0)
