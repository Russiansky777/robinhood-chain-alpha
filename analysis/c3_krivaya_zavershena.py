#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ЗАВЕРШЁННАЯ КРИВАЯ PUMP.FUN: один признак, одно чтение, честный отказ.

ЗАЧЕМ. 04.10 позиция D6eyhD2oMTc3HvzHMPwDWC1fHimZbQTED1i8kaS5pump (вход 0.5
SOL, кривая pump.fun) провисела непроданной полтора часа. Правило 4 берёт
ЛУЧШУЮ из двух котировок: Jupiter давал 11.0 %, а НАШ ПУЛ ПО РЕЗЕРВАМ --
63.4 % гарантированных. Лучшая из двух -- пул, и это НЕПРАВДА: кривая была
ЗАВЕРШЕНА (ликвидность уехала в PumpSwap), а остатки на её счетах всё ещё
считались резервами и давали красивое число, которого рынок не даёт.

ЧТО ЗДЕСЬ ЕСТЬ. Разбор счёта кривой (BondingCurve) и ОДИН ответ: можно ли
котировать эту кривую по резервам. Завершена -- «пути через кривую нет»,
маршрут через пул PumpSwap после миграции (его знает Jupiter). Ни сети, ни
ключей, ни подписи: байты счёта приходят снаружи.

ПОЧЕМУ ДВА ПРИЗНАКА, А НЕ ОДИН. Признак `complete` -- один байт в счёте, и
если смещение в раскладке однажды окажется не тем, отказ молча перестанет
срабатывать -- то есть вернётся ровно та дыра, из-за которой модуль и написан.
Поэтому рядом стоит ВТОРОЙ признак, не зависящий от смещения байта:
`real_token_reserves == 0` -- после миграции на кривой НЕ ОСТАЁТСЯ токенов.
Признаки обязаны совпасть; разошлись -- это тоже отказ, по имени.

РАСКЛАДКА СЧЁТА -- ПО ПУБЛИЧНОМУ IDL pump.fun (github.com/pump-fun/
pump-public-docs, idl/pump.json, программа 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M
5uBEwF6P), тип аккаунта BondingCurve:

    дискриминатор 8, virtual_token_reserves 8, virtual_sol_reserves 8,
    real_token_reserves 8, real_sol_reserves 8, token_total_supply 8,
    complete 1, creator 32                       -- всего 49 + 32 = 81 байт

ДИСКРИМИНАТОР НЕ ВЗЯТ ИЗ ПАМЯТИ, А ПОСЧИТАН: Anchor кладёт в начало счёта
первые 8 байт sha256("account:<Имя>"). Для BondingCurve это 17b7f83760d8ac60,
и модуль считает его САМ при импорте -- если имя типа однажды поменяют, число
изменится вместе с ним, а не останется неверной константой.

ЧЕГО ЗДЕСЬ НЕТ. Модуль НЕ ищет пул PumpSwap после миграции и не строит
маршрут: это отдельная работа (и Jupiter её уже делает). Его дело -- не дать
котировать мёртвую кривую живыми цифрами.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent

ПРОГРАММА_КРИВОЙ = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
ИМЯ_СЧЁТА = "BondingCurve"
ДИСКРИМИНАТОР = hashlib.sha256(f"account:{ИМЯ_СЧЁТА}".encode()).digest()[:8]
# Обязательная часть -- до `complete` включительно. `creator` пришёл позже и
# есть не у всех счетов, поэтому он НЕОБЯЗАТЕЛЕН: требовать его значило бы
# отказывать на старых кривых.
ДЛИНА_ОБЯЗАТЕЛЬНОЙ = 8 + 8 * 5 + 1
ДЛИНА_С_СОЗДАТЕЛЕМ = ДЛИНА_ОБЯЗАТЕЛЬНОЙ + 32

WHY_НЕ_ТОТ_СЧЁТ = "это не счёт кривой pump.fun"
WHY_КОРОТКО = "данных счёта меньше обязательной части"
WHY_ЗАВЕРШЕНА = "кривая завершена -- пути через неё нет, маршрут через PumpSwap"
WHY_РАСХОЖДЕНИЕ = ("признаки завершения расходятся: complete и "
                    "real_token_reserves говорят разное")
# ЭТА МЕТКА -- ЧУЖАЯ, И ЭТО НАРОЧНО. bloom_vtoroe_mnenie.отказ_по_типу_пула
# считает отказ ПОСТОЯННЫМ (повтор не поможет) ровно по этим словам. Завершённая
# кривая -- такой же постоянный отказ: она не «оживёт» через минуту.
МЕТКА_ПОСТОЯННОГО_ОТКАЗА = "своим строителем его не котировать"


class ОшибкаКривой(Exception):
    pass


def _b58(b: bytes) -> str:
    """base58 без внешних библиотек: solders на лаборатории может не быть."""
    алфавит = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    ноль = len(b) - len(b.lstrip(b"\x00"))
    н = int.from_bytes(b, "big")
    из_ = ""
    while н:
        н, ост = divmod(н, 58)
        из_ = алфавит[ост] + из_
    return "1" * ноль + из_


def razbor_schjota(dannye) -> dict:
    """Счёт кривой из байтов (bytes или base64-строка) -- полями и числами."""
    из_ = {"ok": False, "why_not": None, "complete": None,
            "virtual_token_reserves": None, "virtual_sol_reserves": None,
            "real_token_reserves": None, "real_sol_reserves": None,
            "token_total_supply": None, "creator": None, "bajt": 0}
    b = dannye
    if isinstance(b, str):
        try:
            b = base64.b64decode(b, validate=False)
        except (ValueError, TypeError) as сбой:
            из_["why_not"] = f"данные счёта не base64: {type(сбой).__name__}"
            return из_
    if not isinstance(b, (bytes, bytearray)):
        из_["why_not"] = f"данные счёта не байты, а {type(b).__name__}"
        return из_
    b = bytes(b)
    из_["bajt"] = len(b)
    if len(b) < ДЛИНА_ОБЯЗАТЕЛЬНОЙ:
        из_["why_not"] = f"{WHY_КОРОТКО}: {len(b)} из {ДЛИНА_ОБЯЗАТЕЛЬНОЙ}"
        return из_
    if b[:8] != ДИСКРИМИНАТОР:
        из_["why_not"] = (f"{WHY_НЕ_ТОТ_СЧЁТ}: дискриминатор {b[:8].hex()}, "
                          f"ждали {ДИСКРИМИНАТОР.hex()}")
        return из_
    (вт, вс, рт, рс, всего) = struct.unpack_from("<QQQQQ", b, 8)
    флаг = b[48]
    if флаг not in (0, 1):
        из_["why_not"] = f"complete не 0 и не 1, а {флаг} -- раскладка не та"
        return из_
    из_.update(ok=True, virtual_token_reserves=вт, virtual_sol_reserves=вс,
               real_token_reserves=рт, real_sol_reserves=рс,
               token_total_supply=всего, complete=bool(флаг))
    if len(b) >= ДЛИНА_С_СОЗДАТЕЛЕМ:
        из_["creator"] = _b58(b[49:81])
    return из_


def mozhno_kotirovat(dannye) -> dict:
    """МОЖНО ЛИ КОТИРОВАТЬ ЭТУ КРИВУЮ ПО РЕЗЕРВАМ. Один ответ, одно чтение.

    Отдаёт `ok=False` и `why_not` словами, когда котировать нельзя. Отказ
    завершённой кривой несёт МЕТКУ_ПОСТОЯННОГО_ОТКАЗА: повтор через минуту её
    не оживит, и сторож продавца обязан это видеть.
    """
    р = razbor_schjota(dannye)
    из_ = {"ok": False, "why_not": р.get("why_not"), "zavershena": None,
            "razbor": р, "postojannyj": False}
    if not р["ok"]:
        return из_
    по_флагу = bool(р["complete"])
    по_ostatku = int(р["real_token_reserves"]) == 0
    из_["zavershena"] = по_флагу
    if по_флагу != по_ostatku:
        # ДВА ПРИЗНАКА РАЗОШЛИСЬ -- ЭТО НЕ ПОВОД ВЫБРАТЬ ПОУДОБНЕЕ. Либо
        # раскладка сдвинулась, либо счёт в необычном состоянии; в обоих
        # случаях котировать по его резервам нельзя.
        из_.update(why_not=(f"{WHY_РАСХОЖДЕНИЕ}: complete={по_флагу}, "
                             f"real_token_reserves={р['real_token_reserves']}"),
                   postojannyj=False)
        return из_
    if по_флагу:
        из_.update(why_not=f"{WHY_ЗАВЕРШЕНА} ({МЕТКА_ПОСТОЯННОГО_ОТКАЗА})",
                   postojannyj=True)
        return из_
    if int(р["virtual_token_reserves"]) <= 0 or int(р["virtual_sol_reserves"]) <= 0:
        из_["why_not"] = ("виртуальные резервы кривой нулевые -- котировать "
                          "нечем")
        return из_
    из_["ok"] = True
    return из_


def kotirovka_po_krivoj(dannye, prodajom_raw: int, *, komissija_bps: int = 100
                        ) -> dict:
    """Сколько SOL даст кривая за столько-то токенов. Чистая арифметика.

    Формула кривой -- та же x*y=k по ВИРТУАЛЬНЫМ резервам, что и у покупки
    (c2_swap_build.bonding_min_out считает её в другую сторону). Нужна она
    здесь для одного: показать числом, ЧТО ИМЕННО вернул бы старый путь на
    завершённой кривой, -- и потому считается только после mozhno_kotirovat.
    """
    из_ = {"ok": False, "why_not": None, "lamports": 0}
    м = mozhno_kotirovat(dannye)
    if not м["ok"]:
        из_["why_not"] = м["why_not"]
        из_["postojannyj"] = м.get("postojannyj")
        return из_
    р = м["razbor"]
    вт, вс = int(р["virtual_token_reserves"]), int(р["virtual_sol_reserves"])
    к = int(prodajom_raw)
    if к <= 0:
        из_["why_not"] = "продавать нечего"
        return из_
    # ФОРМУЛА -- ТА, ЧТО У САМОЙ ПРОГРАММЫ, И ЭТО ПРОВЕРЕНО ЖИВЫМИ СДЕЛКАМИ:
    # выход = (резерв_противоположный * вход) // (резерв_свой + вход).
    # На пяти живых покупках кривой из data/c2_pool_samples она даёт РОВНО то
    # число, что записано в событии сделки (5 из 5, без единицы разницы).
    # Запись «резерв − k/(резерв+вход)» арифметически та же, но округляется в
    # другую сторону и расходилась с цепью на единицу -- на деньгах берём ту,
    # что совпала.
    вышло = (вс * к) // (вт + к)
    из_.update(ok=True, lamports=max(0, вышло * (10_000 - int(komissija_bps))
                                     // 10_000),
               bez_komissii=max(0, вышло))
    return из_


def sobrat_schjot(*, virtual_token_reserves: int, virtual_sol_reserves: int,
                  real_token_reserves: int, real_sol_reserves: int,
                  token_total_supply: int, complete: bool,
                  creator: bytes | None = None) -> bytes:
    """Счёт кривой БАЙТАМИ -- для самопроверки и для сверки с живым счётом."""
    b = bytearray(ДИСКРИМИНАТОР)
    b += struct.pack("<QQQQQ", virtual_token_reserves, virtual_sol_reserves,
                     real_token_reserves, real_sol_reserves, token_total_supply)
    b += bytes([1 if complete else 0])
    if creator is not None:
        b += bytes(creator)[:32].ljust(32, b"\x00")
    return bytes(b)


# ------------------------------------------------------------- самопроверка

# ЧИСЛО ПРОВЕРОК ОБЪЯВЛЕНО: меньше -- значит пропущено молча.
ZHDEM_PROVEROK = 14
ФАЙЛ_ОБРАЗЦОВ = "c2_pool_samples/6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P.json"
# Живые покупки кривой, на которых сверена формула: 5 событий из 6 образцов
# (шестое -- кривая v2 с котировкой не в SOL, там sol_amount нулевой).
ЖДЁМ_ЖИВЫХ_СОБЫТИЙ = 5
СОБЫТИЕ_ДИСК = bytes.fromhex("bddb7fd34ee661ee")
АНКОР_ДИСК = bytes.fromhex("e445a52e51cb9a1d")


def sobytija_krivoj(tx: dict) -> list:
    """Тела событий сделки кривой из логов транзакции. Только чтение."""
    из_ = []
    for строка in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        if "Program data: " not in строка:
            continue
        try:
            b = base64.b64decode(строка.split("Program data: ", 1)[1])
        except (ValueError, TypeError):
            continue
        if b[:8] == АНКОР_ДИСК:
            b = b[8:]
        if b[:8] != СОБЫТИЕ_ДИСК:
            continue
        из_.append(b[8:])
    return из_


def chisla_sobytija(telo: bytes) -> dict:
    """sol_amount, token_amount, is_buy и виртуальные резервы ПОСЛЕ сделки."""
    м = 32                                    # mint
    sol_amount, token_amount = struct.unpack_from("<QQ", telo, м)
    м += 16
    is_buy = telo[м]
    м += 1 + 32 + 8                           # is_buy, user, timestamp
    вс, вт, рс, рт = struct.unpack_from("<QQQQ", telo, м)
    return {"sol_amount": sol_amount, "token_amount": token_amount,
            "is_buy": bool(is_buy), "virtual_sol_reserves": вс,
            "virtual_token_reserves": вт, "real_sol_reserves": рс,
            "real_token_reserves": рт}


def self_test() -> int:  # noqa: C901, PLR0915
    было, плохо = 0, 0
    упавшие: list = []

    def chk(имя, усл, факт=None):
        nonlocal было, плохо
        было += 1
        if усл:
            print(f"  ok   {имя}")
        else:
            плохо += 1
            упавшие.append(имя)
            print(f" ПЛОХО {имя} -- {факт!r}")

    print("c3_krivaya_zavershena: самопроверка")
    # --------------------------------- 1. ДИСКРИМИНАТОР СЧИТАН, А НЕ ВСПОМНЕН
    chk("дискриминатор счёта кривой считается по схеме Anchor "
        "(sha256 'account:BondingCurve'), а не лежит выдуманной константой",
        ДИСКРИМИНАТОР == hashlib.sha256(b"account:BondingCurve").digest()[:8]
        and ДИСКРИМИНАТОР.hex() == "17b7f83760d8ac60", ДИСКРИМИНАТОР.hex())

    ЖИВАЯ = dict(virtual_token_reserves=876_876_413_383_234,
                 virtual_sol_reserves=36_709_848_235,
                 real_token_reserves=596_976_413_383_234,
                 real_sol_reserves=6_709_848_235,
                 token_total_supply=1_000_000_000_000_000)
    b_живая = sobrat_schjot(complete=False, **ЖИВАЯ)
    b_мертвая = sobrat_schjot(complete=True, **{**ЖИВАЯ,
                                                "real_token_reserves": 0})
    # --------------------------------- 2. РАЗБОР И ОТКАЗЫ
    р_ж = razbor_schjota(b_живая)
    chk("живой счёт кривой разбирается полями: пять чисел и признак",
        р_ж["ok"] and р_ж["complete"] is False
        and р_ж["virtual_sol_reserves"] == ЖИВАЯ["virtual_sol_reserves"]
        and р_ж["token_total_supply"] == ЖИВАЯ["token_total_supply"], р_ж)
    chk("счёт с чужим дискриминатором -- отказ ПО ИМЕНИ, а не разбор мусора",
        (lambda р: not р["ok"] and WHY_НЕ_ТОТ_СЧЁТ in (р["why_not"] or ""))(
            razbor_schjota(b"\\x00" * 60)), None)
    chk("короткие данные -- отказ по имени, а не исключение из глубины",
        (lambda р: not р["ok"] and WHY_КОРОТКО in (р["why_not"] or ""))(
            razbor_schjota(ДИСКРИМИНАТОР + b"\\x00" * 8)), None)
    chk("base64 принимается наравне с байтами: узел отдаёт счёт именно так",
        razbor_schjota(base64.b64encode(b_живая).decode())["ok"], None)
    chk("создатель читается, когда он в счёте есть, и не требуется, когда нет",
        razbor_schjota(b_живая)["creator"] is None
        and len(razbor_schjota(sobrat_schjot(complete=False, creator=b"\\x01" * 32,
                                             **ЖИВАЯ))["creator"] or "") >= 32,
        None)
    # --------------------------------- 3. ГЛАВНОЕ: ЗАВЕРШЁННАЯ НЕ КОТИРУЕТСЯ
    м_ж = mozhno_kotirovat(b_живая)
    м_м = mozhno_kotirovat(b_мертвая)
    chk("живую кривую котировать МОЖНО", м_ж["ok"] and м_ж["zavershena"] is False,
        м_ж["why_not"])
    chk("ЗАВЕРШЁННУЮ КРИВУЮ КОТИРОВАТЬ НЕЛЬЗЯ -- это и есть дыра 04.10: "
        "Jupiter давал 11.0 %, а резервы мёртвой кривой -- 63.4 %",
        not м_м["ok"] and м_м["zavershena"] is True
        and WHY_ЗАВЕРШЕНА in (м_м["why_not"] or ""), м_м)
    chk("отказ завершённой кривой ПОСТОЯННЫЙ и несёт метку, по которой сторож "
        "продавца не станет повторять: повтор её не оживит",
        м_м["postojannyj"] is True
        and МЕТКА_ПОСТОЯННОГО_ОТКАЗА in (м_м["why_not"] or ""), м_м["why_not"])
    # ДОКАЗАННЫЙ КРАСНЫЙ: без признака те же байты дают КРАСИВОЕ ЧИСЛО.
    к_мертвая = kotirovka_po_krivoj(b_мертвая, 10 ** 12)
    к_живая = kotirovka_po_krivoj(b_живая, 10 ** 12)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: на тех же резервах живая кривая даёт число, а "
        "завершённая -- отказ; раньше оба случая давали число",
        к_живая["ok"] and к_живая["lamports"] > 0
        and not к_мертвая["ok"] and к_мертвая.get("postojannyj") is True,
        (к_живая.get("lamports"), к_мертвая.get("why_not")))
    # --------------------------------- 4. ДВА ПРИЗНАКА ОБЯЗАНЫ СОВПАСТЬ
    b_спор = sobrat_schjot(complete=True, **ЖИВАЯ)          # complete, но токены есть
    м_с = mozhno_kotirovat(b_спор)
    chk("признаки разошлись (complete=1, а токены на кривой есть) -- отказ по "
        "имени, а не выбор того, что удобнее",
        not м_с["ok"] and WHY_РАСХОЖДЕНИЕ in (м_с["why_not"] or ""), м_с["why_not"])
    b_спор2 = sobrat_schjot(complete=False, **{**ЖИВАЯ, "real_token_reserves": 0})
    chk("и в другую сторону: токенов нет, а признак не стоит -- тоже отказ",
        not mozhno_kotirovat(b_спор2)["ok"], None)
    # --------------------------------- 5. ФОРМУЛА СВЕРЕНА С ЖИВЫМИ СДЕЛКАМИ
    п_обр = КОРЕНЬ / "data" / ФАЙЛ_ОБРАЗЦОВ
    сошлось = всего = 0
    if п_обр.exists():
        for x in json.loads(п_обр.read_text(encoding="utf-8")):
            tx = x.get("tx") if isinstance(x, dict) else None
            if not isinstance(tx, dict):
                continue
            for тело in sobytija_krivoj(tx):
                ч = chisla_sobytija(тело)
                if not ч["virtual_sol_reserves"] or not ч["sol_amount"]:
                    continue        # кривая v2 с котировкой не в SOL
                всего += 1
                до_с = ч["virtual_sol_reserves"] - ч["sol_amount"]
                до_т = ч["virtual_token_reserves"] + ч["token_amount"]
                b = sobrat_schjot(virtual_token_reserves=до_т,
                                  virtual_sol_reserves=до_с,
                                  real_token_reserves=до_т, real_sol_reserves=до_с,
                                  token_total_supply=10 ** 15, complete=False)
                р = razbor_schjota(b)
                вышло = (р["virtual_token_reserves"] * ч["sol_amount"]
                         // (р["virtual_sol_reserves"] + ч["sol_amount"]))
                сошлось += (вышло == ч["token_amount"])
    chk(f"формула кривой сверена с ЖИВЫМИ сделками: {ЖДЁМ_ЖИВЫХ_СОБЫТИЙ} "
        "покупок из образцов репозитория, выход совпал с событием БАЙТ В БАЙТ",
        всего == ЖДЁМ_ЖИВЫХ_СОБЫТИЙ and сошлось == всего, (сошлось, всего))
    chk("продажа на живых резервах даёт положительный выход и меньше, чем "
        "без комиссии",
        (lambda к: к["ok"] and 0 < к["lamports"] < к["bez_komissii"])(
            kotirovka_po_krivoj(b_живая, 10 ** 12)), None)

    if упавшие:
        print("УПАЛИ: " + "; ".join(упавшие))
    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным")
        return 1
    return 1 if плохо else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--schjot", default=None,
                   help="данные счёта кривой в base64 (из getAccountInfo)")
    p.add_argument("--prodat", type=int, default=0,
                   help="сколько сырых токенов котировать по этой кривой")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if a.schjot:
        из_ = mozhno_kotirovat(a.schjot)
        if a.prodat and из_.get("ok"):
            из_["kotirovka"] = kotirovka_po_krivoj(a.schjot, a.prodat)
        print(json.dumps(из_, ensure_ascii=False, indent=1))
        return 0 if из_.get("ok") else 2
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
