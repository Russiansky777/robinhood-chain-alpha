#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ТОЧНАЯ котировка Meteora DLMM для полосы -- ЗА ФЛАГОМ, по умолчанию выключена.

ЗАЧЕМ. Цена у DLMM живёт в корзинах, и у каждой своя. Наш нынешний минимум
выхода (c2_swap_build.dlmm_min_out) считает по ОДНОЙ корзине -- по шагу из
кэша: это годится, пока сделка не выходит за корзину, и врёт, когда выходит.
Модуль Code-2 (analysis/podbivka_dlmm_quote.py, перенос quote.rs Meteora)
считает переход по корзинам и массивам точно. Здесь он подключён к сети:
читаем пул и нужные массивы корзин и отдаём минимум выхода в том же виде, в
каком его ждёт сборщик.

ЧТО ЭТО СТОИТ НА ГОРЯЧЕМ ПУТИ. Два запроса getMultipleAccounts (пул с
расширением, затем пул с массивами -- чтобы пул и корзины были из одного
слота). Поэтому путь ЗА ФЛАГОМ BLOOM_DLMM_QUOTE и по умолчанию выключен:
включать его можно только после замера задержки на живых сигналах.

Своих денег модуль не двигает и подписей не делает: только чтение и счёт.
"""

from __future__ import annotations

import os
import time

WSOL = "So11111111111111111111111111111111111111112"
ЗАПАС_МАССИВОВ = int(os.environ.get("BLOOM_DLMM_ARRAYS", "3") or 3)

# РОВНО ЭТИ УСЛОВИЯ САМОПРОВЕРКИ ТРЕБУЮТ solders (вывод PDA расширения пула) и
# пропускаются там, где его нет -- на облачном бегунке. На хосте он есть, и там
# проходят все. Сравнение байт в байт и simulateTransaction в этот список НЕ
# входят и входить не могут: они живут в другом прогоне
# (deploy/checks/dlmm_vosstanovlenie.py), который целиком идёт на хосте.
ПРОПУСКАЕМЫЕ_БЕЗ_SOLDERS = (
    "узел молчит -- отказ без минимума",
    "счёта пула нет -- отказ, минимум не выдумывается",
)


def включена() -> bool:
    """Флаг точной котировки. По умолчанию ВЫКЛЮЧЕНА."""
    зн = (os.environ.get("BLOOM_DLMM_QUOTE") or "").strip()
    return зн == "1"


def _модуль():
    import podbivka_dlmm_quote as Q  # noqa: PLC0415

    return Q


def _счета(rpc_call, адреса: list, *, таймаут: float = 3.0) -> dict:
    """getMultipleAccounts одним запросом: адрес -> сырые байты (или None)."""
    из_ = {"ok": False, "данные": {}, "why_not": None, "slot": None, "мс": None}
    т0 = time.perf_counter()
    try:
        о = rpc_call("getMultipleAccounts",
                      [адреса, {"encoding": "base64", "commitment": "confirmed"}])
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        из_["мс"] = round((time.perf_counter() - т0) * 1000.0, 2)
        return из_
    из_["мс"] = round((time.perf_counter() - т0) * 1000.0, 2)
    о = о if isinstance(о, dict) else {}
    рез = о.get("result") if "result" in о else о
    if not isinstance(рез, dict):
        из_["why_not"] = f"ответ узла не разобран: {str(о)[:120]}"
        return из_
    из_["slot"] = ((рез.get("context") or {}).get("slot")
                    if isinstance(рез.get("context"), dict) else None)
    значения = рез.get("value")
    if not isinstance(значения, list) or len(значения) != len(адреса):
        из_["why_not"] = "узел вернул не столько счетов, сколько спрашивали"
        return из_
    import base64  # noqa: PLC0415

    for адрес, зн in zip(адреса, значения):
        if not зн:
            из_["данные"][адрес] = None
            continue
        д = (зн.get("data") or [None, None])[0]
        try:
            из_["данные"][адрес] = base64.b64decode(д) if д else None
        except Exception:  # noqa: BLE001
            из_["данные"][адрес] = None
    из_["ok"] = True
    return из_


def котировка(*, пул: str, минт_базы: str, минт_котировки: str, лампорты: int,
               проскальзывание: float, rpc_call, сейчас: float | None = None) -> dict:
    """Минимум выхода по DLMM из живого состояния пула и корзин.

    Возвращает тот же вид, что min_out_from_reserves: {ok, expected_out,
    min_out, ...} плюс сколько корзин пройдено и сколько чтений стоило.
    """
    из_ = {"ok": False, "why_not": None, "чтений": 0, "путь": "точная котировка DLMM"}
    if лампорты <= 0:
        из_["why_not"] = "сумма входа не положительна"
        return из_
    try:
        Q = _модуль()
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"модуль котировки DLMM не загружен: {type(exc).__name__}"
        return из_

    ч1 = _счета(rpc_call, [пул, Q.адрес_расширения(пул)])
    из_["чтений"] += 1
    из_["мс_чтение1"] = ч1.get("мс")
    if not ч1["ok"]:
        из_["why_not"] = f"пул не прочитан: {ч1['why_not']}"
        return из_
    данные_пула = ч1["данные"].get(пул)
    if not данные_пула:
        из_["why_not"] = "счёта пула нет на цепи"
        return из_
    try:
        lb = Q.разобрать_пул(данные_пула)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"пул не разобрался: {type(exc).__name__}: {str(exc)[:120]}"
        return из_
    ext = None
    данные_ext = ч1["данные"].get(Q.адрес_расширения(пул))
    if данные_ext:
        try:
            ext = Q.разобрать_расширение(данные_ext)
        except Exception:  # noqa: BLE001
            ext = None

    # КУДА ИДЁТ СВОП. Платим котировкой (WSOL или иной минт котировки) и
    # получаем базу: swap_for_y значит "отдаём X, получаем Y".
    if lb.get("token_x_mint") == минт_котировки:
        swap_for_y = True
    elif lb.get("token_y_mint") == минт_котировки:
        swap_for_y = False
    else:
        из_["why_not"] = ("минт котировки не совпал ни с одной стороной пула: "
                           f"{минт_котировки[:8]} против "
                           f"{str(lb.get('token_x_mint'))[:8]}/"
                           f"{str(lb.get('token_y_mint'))[:8]}")
        return из_
    из_["swap_for_y"] = swap_for_y
    из_["bin_step"] = lb.get("bin_step")
    из_["active_id"] = lb.get("active_id")

    try:
        индексы = Q.нужные_массивы(lb, swap_for_y, ЗАПАС_МАССИВОВ, ext)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"массивы корзин не выбрались: {type(exc).__name__}"
        return из_
    if not индексы:
        из_["why_not"] = "в пуле не нашлось ни одного массива корзин"
        return из_
    адреса = [Q.адрес_массива(пул, и) for и in индексы]
    ч2 = _счета(rpc_call, [пул] + адреса)
    из_["чтений"] += 1
    из_["мс_чтение2"] = ч2.get("мс")
    из_["мс_чтений_всего"] = round(float(из_.get("мс_чтение1") or 0.0)
                                    + float(ч2.get("мс") or 0.0), 2)
    if not ч2["ok"]:
        из_["why_not"] = f"корзины не прочитаны: {ч2['why_not']}"
        return из_
    # Пул перечитан ВМЕСТЕ с корзинами: состояние из одного слота.
    свежий = ч2["данные"].get(пул)
    if свежий:
        try:
            lb = Q.разобрать_пул(свежий)
        except Exception:  # noqa: BLE001
            pass
    массивы = {}
    не_прочитаны = []
    for и, адрес in zip(индексы, адреса):
        д = ч2["данные"].get(адрес)
        if not д:
            не_прочитаны.append(и)
            continue
        try:
            ba = Q.разобрать_массив(д)
        except Exception:  # noqa: BLE001
            не_прочитаны.append(и)
            continue
        массивы[ba["index"]] = ba
    из_["массивов"] = len(массивы)
    из_["массивы_не_прочитаны"] = не_прочитаны
    if not массивы:
        из_["why_not"] = f"ни один массив корзин не прочитан: {не_прочитаны}"
        return из_

    ts = int(сейчас if сейчас is not None else time.time())
    try:
        q = Q.котировка_точный_вход(lb, пул, int(лампорты), swap_for_y, массивы, ts,
                                     ext)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"котировка не посчиталась: {type(exc).__name__}: {str(exc)[:140]}"
        return из_
    ожидаем = int(q.get("amount_out") or 0)
    if ожидаем <= 0:
        из_["why_not"] = "котировка дала ноль на выходе"
        return из_
    мин = int(ожидаем * (1.0 - float(проскальзывание)))
    if мин <= 0:
        из_["why_not"] = "минимум выхода не положителен при этом проскальзывании"
        return из_
    из_.update(ok=True, expected_out=ожидаем, min_out=мин,
                fee=q.get("fee"), protocol_fee=q.get("protocol_fee"),
                корзин=q.get("корзин"), slot=ч2.get("slot"))
    return из_


def self_test() -> int:
    """Без сети: подставляем узел, который отдаёт заранее собранные байты."""
    пройдено = провалено = 0

    def chk(что, ок, факт=None):
        nonlocal пройдено, провалено
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {что}"
              + (f" -> {факт!r}" if факт is not None and not ок else ""))
        пройдено += bool(ок)
        провалено += (not ок)

    # --- флаг по умолчанию выключен
    было = os.environ.pop("BLOOM_DLMM_QUOTE", None)
    chk("по умолчанию точная котировка ВЫКЛЮЧЕНА", включена() is False)
    os.environ["BLOOM_DLMM_QUOTE"] = "1"
    chk("флаг 1 включает", включена() is True)
    os.environ["BLOOM_DLMM_QUOTE"] = "0"
    chk("флаг 0 выключает", включена() is False)
    if было is None:
        os.environ.pop("BLOOM_DLMM_QUOTE", None)
    else:
        os.environ["BLOOM_DLMM_QUOTE"] = было

    # --- СУММА НЕ ПОЛОЖИТЕЛЬНА: solders тут не нужен вовсе (отказ раньше любых
    # адресов), поэтому условие стоит ДО оговорки и не пропускается никогда.
    def _молчит(метод, параметры):
        raise OSError("сети нет")

    р2 = котировка(пул="11111111111111111111111111111111", минт_базы="A" * 32,
                    минт_котировки=WSOL, лампорты=0, проскальзывание=0.2,
                    rpc_call=_молчит)
    chk("нулевой вход -- отказ", р2["ok"] is False
        and "не положительна" in (р2["why_not"] or ""))

    # --- ДАЛЬШЕ НУЖЕН solders (вывод адресов PDA). На бегунке его нет, на хосте
    # есть: без него эти условия ПРОПУСКАЮТСЯ и об этом говорится прямо, а не
    # выдаются за пройденные.
    try:
        import solders.pubkey  # noqa: F401,PLC0415
    except Exception:  # noqa: BLE001
        for имя in ПРОПУСКАЕМЫЕ_БЕЗ_SOLDERS:
            print(f"  [проп ] {имя}: на этой машине нет solders")
        print(f"самопроверка точной котировки DLMM: {пройдено}/{пройдено + провалено}"
              " пройдено (часть пропущена -- нет solders)")
        return 0 if провалено == 0 else 1

    # --- узел молчит: честный отказ, а не выдуманный минимум
    def мёртвый(метод, параметры):
        raise OSError("сети нет")

    р = котировка(пул="11111111111111111111111111111111", минт_базы="A" * 32,
                   минт_котировки=WSOL, лампорты=10_000_000,
                   проскальзывание=0.2, rpc_call=мёртвый)
    chk("узел молчит -- отказ без минимума",
        р["ok"] is False and "не прочитан" in (р["why_not"] or "")
        and "min_out" not in р, р)

    # --- пула нет на цепи
    def пусто(метод, параметры):
        return {"result": {"context": {"slot": 1}, "value": [None, None]}}

    р3 = котировка(пул="11111111111111111111111111111111", минт_базы="A" * 32,
                    минт_котировки=WSOL, лампорты=10_000_000,
                    проскальзывание=0.2, rpc_call=пусто)
    chk("счёта пула нет -- отказ, минимум не выдумывается",
        р3["ok"] is False and "нет на цепи" in (р3["why_not"] or ""), р3)

    print(f"самопроверка точной котировки DLMM: {пройдено}/{пройдено + провалено} пройдено")
    return 0 if провалено == 0 else 1


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(self_test() if "--self-test" in sys.argv else 0)
