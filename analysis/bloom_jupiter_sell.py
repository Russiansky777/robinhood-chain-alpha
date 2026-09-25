#!/usr/bin/env python3
"""Продажа позиции через Jupiter Ultra с ПОЛОМ по выходу. Правило 4.

Зачем этот путь вообще появился. Bloom по токену п. 1 стенда трижды подряд
повёл продажу через один и тот же пул Meteora DAMM v2 без ликвидности в
диапазоне и трижды получил "assertion failed: liquidity > 0": авто-ордер и
две попытки сторожа. Маршрут выбирает Bloom, и повлиять на выбор нельзя --
значит нужен второй путь выхода, где маршрут выбирает Jupiter.

Правило 4 владельца, дословно и в коде:

  1. Минимальный выход в транзакции >= 70 % котировки. Делается ДВУМЯ
     независимыми способами, потому что одного мало:
       * slippageBps задаётся в запросе /order явно (по документации Ultra
         это параметр запроса; 0 означает "Ultra выберет сама"). Для пола
         70 % это 3000 bps;
       * ответ /order отдаёт otherAmountThreshold -- при swapMode=ExactIn
         это минимальный выход ВЫДАННОЙ транзакции. Перед подписью он
         сверяется с 70 % от outAmount, и если ниже -- транзакция НЕ
         подписывается. То есть пол не только просится, но и проверяется.
  2. Котировка меньше 30 % от входа -- не продавать: это не продажа, а
     раздача. Позиция помечается UNSOLD, владельцу уходит строка.

Оговорка, которую нельзя прятать: otherAmountThreshold -- это заявленный
порог ответа API, а не результат разбора байтов инструкции. Дополнительно
проверяется, встречается ли это число в данных инструкций выданной
транзакции (little-endian u64); результат проверки честно пишется в журнал
и не выдаётся за полный разбор маршрута. Фактический результат всё равно
сверяется по цепи после отправки.

Ключ. Подпись требует приватного ключа кошелька исполнителя -- переменная
окружения BLOOM_WALLET_KEY (в репозиторий не кладётся, в логи не печатается).
Ключа нет -- путь через Jupiter недоступен, и это честный отказ с причиной,
а не тихое бездействие.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

WSOL = "So11111111111111111111111111111111111111112"
ПОЛ_ПРОЦЕНТОВ = float(os.environ.get("BLOOM_JUP_FLOOR_PCT", "70") or 70)
МИН_ДОЛЯ_ОТ_ВХОДА = float(os.environ.get("BLOOM_JUP_MIN_QUOTE_PCT", "30") or 30)


def пол_bps(процент: float = ПОЛ_ПРОЦЕНТОВ) -> int:
    """Пол 70 % -> допустимое проскальзывание 30 % -> 3000 bps."""
    доля = max(0.0, min(100.0, float(процент)))
    return int(round((100.0 - доля) * 100))


def _в_байтах(сырое: bytes, число: int, размер: int) -> bool:
    try:
        return int(число).to_bytes(размер, "little") in сырое
    except (TypeError, ValueError, OverflowError):
        return False


def что_в_байтах(tx_b64: str, *, порог: int | None, котировка: int | None,
                  slippage_bps: int | None) -> dict:
    """Что из заявленного реально лежит в байтах выданной транзакции.

    Зачем три числа, а не одно. Маршрутизатор Jupiter (metis) кодирует в
    инструкции КОТИРОВКУ и ПРОСКАЛЬЗЫВАНИЕ, а минимум выхода считает на
    цепи как котировка * (1 - slippage). Поэтому самого порога в байтах
    может не быть -- и это не признак подлога: если в байтах есть котировка
    и наше проскальзывание 3000 bps, то пол 70 % обеспечен теми же байтами,
    которые мы подписываем. Проверка сообщает, что именно нашла, и ничего
    не выдаёт за полный разбор маршрута.
    """
    try:
        сырое = base64.b64decode(tx_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "why_not": f"база64 не разобралась: {type(exc).__name__}"}
    if not сырое:
        return {"known": False, "why_not": "транзакция пустая"}
    вых = {"known": True, "bytes_len": len(сырое),
            "threshold_found": (_в_байтах(сырое, порог, 8) if порог is not None else None),
            "quote_found": (_в_байтах(сырое, котировка, 8)
                             if котировка is not None else None),
            "slippage_found": (any(_в_байтах(сырое, slippage_bps, n) for n in (2, 4))
                                if slippage_bps is not None else None)}
    if вых["threshold_found"]:
        вых["note"] = "минимум выхода лежит в байтах транзакции прямым числом"
    elif вых["quote_found"] and вых["slippage_found"]:
        вых["note"] = ("минимума прямым числом нет, но в байтах есть котировка и "
                        "наше проскальзывание: на цепи минимум считается из них, "
                        "то есть пол обеспечен подписываемыми байтами")
    else:
        вых["note"] = ("ни минимума, ни пары котировка+проскальзывание в байтах не "
                        "нашлось: проверка согласованности НЕ пройдена, полагаться "
                        "можно только на заявленный порог ответа API")
    return вых


def порог_в_байтах(tx_b64: str, порог: int) -> dict:
    """Встречается ли заявленный порог в данных транзакции как u64 LE.

    Это НЕ разбор маршрута: это проверка согласованности заявленного порога
    с байтами, которые мы собираемся подписать. Не нашли -- так и сказано.
    """
    try:
        # validate=True обязательно: без него b64decode молча выбрасывает
        # чужие символы, и мусор вместо транзакции превращается в пустые
        # байты, то есть в "порога нет" вместо честного "не разобралось".
        сырое = base64.b64decode(tx_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "why_not": f"база64 не разобралась: {type(exc).__name__}"}
    if not сырое:
        return {"known": False, "why_not": "транзакция пустая"}
    try:
        нужно = int(порог).to_bytes(8, "little")
    except (TypeError, ValueError, OverflowError):
        return {"known": False, "why_not": "порог не целое число"}
    return {"known": True, "found": нужно in сырое,
             "note": ("порог найден в байтах транзакции"
                      if нужно in сырое else
                      "порога нет в байтах: маршрут может кодировать его иначе "
                      "(например RFQ) -- проверка согласованности не пройдена, "
                      "но заявленный порог всё равно проверен по 70 %")}


def проверить_пол(order: dict, *, вход_sol: float | None = None,
                   пол_процентов: float = ПОЛ_ПРОЦЕНТОВ,
                   мин_доля_от_входа: float = МИН_ДОЛЯ_ОТ_ВХОДА) -> dict:
    """Можно ли подписывать этот ордер. Ничего не подписывает.

    Возвращает ok=True только когда выполнены ОБА условия правила 4.
    Любое неизвестное число -- это отказ: неизвестность в сторону денег не
    трактуется.
    """
    итог: dict = {"ok": False, "why_not": None, "checks": {}}
    try:
        out = int(order.get("outAmount"))
    except (TypeError, ValueError):
        итог["why_not"] = "в ответе Ultra нет outAmount -- подписывать нечего"
        return итог
    try:
        порог = int(order.get("otherAmountThreshold"))
    except (TypeError, ValueError):
        итог["why_not"] = ("в ответе Ultra нет otherAmountThreshold -- минимум "
                            "выхода неизвестен, подписывать нельзя")
        return итог
    режим = (order.get("swapMode") or "ExactIn")
    if str(режим) != "ExactIn":
        итог["why_not"] = f"swapMode={режим}: порог означает не минимум выхода"
        return итог

    нужный = int(out * (float(пол_процентов) / 100.0))
    итог["checks"]["out_amount"] = out
    итог["checks"]["threshold"] = порог
    итог["checks"]["floor_needed"] = нужный
    итог["checks"]["floor_pct"] = пол_процентов
    итог["checks"]["slippage_bps"] = order.get("slippageBps")
    итог["checks"]["router"] = order.get("router")
    if order.get("transaction"):
        try:
            слиппедж = int(order.get("slippageBps"))
        except (TypeError, ValueError):
            слиппедж = None
        итог["checks"]["in_tx"] = что_в_байтах(order["transaction"], порог=порог,
                                                котировка=out, slippage_bps=слиппедж)

    if порог < нужный:
        итог["why_not"] = (f"минимум выхода {порог} ниже пола {нужный} "
                            f"({пол_процентов:.0f} % от котировки {out})")
        return итог

    # Второе условие: котировка против того, что мы вложили.
    if вход_sol is not None:
        try:
            вход_raw = int(round(float(вход_sol) * 1e9))
        except (TypeError, ValueError):
            вход_raw = None
        if вход_raw:
            доля = 100.0 * out / вход_raw
            итог["checks"]["quote_share_of_entry_pct"] = round(доля, 2)
            итог["checks"]["entry_lamports"] = вход_raw
            if доля < float(мин_доля_от_входа):
                итог["why_not"] = (f"котировка {доля:.1f} % от входа ниже "
                                    f"{мин_доля_от_входа:.0f} % -- это не продажа, "
                                    "а раздача: UNSOLD и доклад владельцу")
                итог["unsold"] = True
                return итог

    итог["ok"] = True
    return итог


# Имена переменной с ключом. Первое -- как назвал владелец в секрете;
# второе осталось от первой версии и читается, чтобы имя не разошлось
# между секретом, env на хосте и кодом: такое расхождение уже стоило
# прогона.
ИМЕНА_КЛЮЧА = ("EXEC_WALLET_KEY", "BLOOM_WALLET_KEY")


def ключ_сырой() -> str:
    for имя in ИМЕНА_КЛЮЧА:
        v = (os.environ.get(имя) or "").strip()
        if v:
            return v
    return ""


def ключ_есть() -> tuple:
    if not ключ_сырой():
        return False, ("ключа кошелька нет в окружении ("
                        + " или ".join(ИМЕНА_КЛЮЧА) + ") -- продажа через "
                        "Jupiter невозможна, остаётся путь Bloom")
    return True, ""


def публичный_ключ(секрет: str | None = None) -> dict:
    """Публичный ключ из секрета. Сам секрет не возвращается и не печатается."""
    сырое = секрет if секрет is not None else ключ_сырой()
    if not сырое:
        return {"ok": False, "why_not": "ключа нет"}
    try:
        from dbot_rescue import load_rescue_keypair  # noqa: PLC0415
        kp = load_rescue_keypair(сырое)
        return {"ok": True, "pubkey": str(kp.pubkey())}
    except Exception as exc:  # noqa: BLE001
        # Текст исключения не печатаем: в него может попасть сам секрет.
        return {"ok": False, "why_not": f"ключ не разобрался ({type(exc).__name__})"}


def ключ_от_нашего_кошелька(ожидаемый: str, секрет: str | None = None) -> dict:
    """Тот ли это кошелёк. Несовпадение -- ЗАПРЕТ, а не предупреждение.

    Цена ошибки прямая: чужой ключ подпишет чужой кошелёк, и продажа уйдёт
    не с нашей позиции. Поэтому проверка обязательна перед подписью, а не
    один раз при деплое.
    """
    п = публичный_ключ(секрет)
    if not п.get("ok"):
        return {"ok": False, "why_not": п.get("why_not")}
    совпал = п["pubkey"] == ожидаемый
    return {"ok": совпал, "pubkey": п["pubkey"], "expected": ожидаемый,
             "why_not": (None if совпал else
                          f"ключ принадлежит {п['pubkey']}, а позиция на "
                          f"{ожидаемый} -- подписывать нельзя")}


def план_путей() -> dict:
    """Какими путями пойдёт продажа и почему. Ничего не делает и не зовёт сеть.

    Правило выбора живёт ЗДЕСЬ и только здесь: и продажа, и признак жизни
    сторожа спрашивают его одной функцией. Два места с одним правилом
    однажды разойдутся, и разойдутся они в деньгах.
    """
    try:
        from dbot_rescue import jup_v2_key_present  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"plan": ["ultra"],
                "why": f"общий клиент не загружен ({type(exc).__name__})"}
    путь = (os.environ.get("BLOOM_JUP_API") or "").strip().lower()
    есть_ключ, почему_нет_ключа = jup_v2_key_present()
    if путь == "ultra":
        return {"plan": ["ultra"], "why": "выбран вход BLOOM_JUP_API=ultra"}
    if путь in ("v2", "swap-v2", "swapv2"):
        return {"plan": ["swap-v2", "ultra"],
                "why": "выбран вход BLOOM_JUP_API=v2, Ultra запасной"}
    if есть_ключ:
        return {"plan": ["swap-v2", "ultra"], "why": "ключ V2 есть, Ultra запасной"}
    return {"plan": ["ultra"], "why": почему_нет_ключа or "ключа V2 нет"}


def продать(*, mint: str, amount_raw: int, taker: str, вход_sol: float | None,
             живьём: bool, ордер_фн=None, исполнить_фн=None,
             подписать_фн=None, секрет: str | None = None,
             мин_доля_от_входа: float | None = None) -> dict:
    """Один проход продажи через Ultra. Функции подменяются в самопроверке.

    Порядок намеренно такой: ордер -> ПРОВЕРКА ПОЛА -> подпись -> отправка.
    Подпись после проверки, а не до: подписанную транзакцию уже поздно
    отзывать.

    мин_доля_от_входа переопределяет правило "котировка ниже 30 % от входа --
    не продавать". Решение владельца 25.09 (вечер): позиции ПОЛОСЫ (свой
    кошелёк, размер не больше 0.05 SOL) продаются по таймеру при ЛЮБОЙ
    котировке -- там 0.05 SOL, и держать их в UNSOLD дороже, чем продать
    дешево. Для покупок Bloom правило 30 % остаётся, поэтому переопределение
    приходит снаружи и только там, где владелец его разрешил, -- а не меняется
    порогом на весь репозиторий.
    """
    шаги: list = []
    итог = {"ok": False, "steps": шаги, "mint": mint, "amount_raw": amount_raw}

    пути: list = []
    if ордер_фн is None or исполнить_фн is None:
        # Клиент один на весь репозиторий -- в dbot_rescue. Второй копии быть
        # не должно: расхождение двух клиентов по полю порога стоило бы денег.
        #
        # Путь по умолчанию -- Swap V2 (Ultra по документации больше не
        # развивают, а lite-api уходит). Ultra -- ЗАПАСНОЙ, и запасной он не
        # на словах: раньше путь выбирался ОДИН раз до сети, и 401 по ключу,
        # 429 или 5xx от api.jup.ag означали не переход на Ultra, а потерянный
        # выход и позицию в UNSOLD. Теперь при отказе на ЗАКАЗЕ пробуется
        # второй путь. Заказ -- единственное безопасное место для отката:
        # ничего не подписано и ничего не отправлено. После подписи и отправки
        # откат запрещён -- транзакция могла уйти в сеть.
        from dbot_rescue import (  # noqa: PLC0415
            swap_v2_execute, swap_v2_order, ultra_execute, ultra_order)
        # Правило выбора -- в план_путей(), одно на весь модуль.
        план = план_путей()
        почему_путь = план["why"]
        ФУНКЦИИ = {"swap-v2": (swap_v2_order, swap_v2_execute),
                    "ultra": (ultra_order, ultra_execute)}
        пути = [(имя, *ФУНКЦИИ[имя]) for имя in план["plan"] if имя in ФУНКЦИИ]
        итог["api_plan"] = [имя for имя, _, _ in пути]
        # api -- ОСНОВНОЙ путь плана. Это имя читают уже написанные разборы,
        # и менять его смысл нельзя; какой путь дал заказ на самом деле,
        # говорит api_used.
        итог["api"] = пути[0][0]
        шаги.append({"step": "api", "ok": True, "api": пути[0][0],
                      "plan": итог["api_plan"], "why_not": почему_путь})

    # Подпись нужна ТОЛЬКО для живой отправки: при живьём=False (проверка
    # котировки и пола) ни ключ, ни solders не требуются вовсе, и падать на
    # их отсутствии этот путь не имеет права.
    if подписать_фн is None and живьём:
        # СЕКРЕТ МОЖЕТ БЫТЬ СВОЙ. У полосы отдельный кошелёк (решение владельца
        # 25.09), и продавать её позицию надо ЕГО ключом. Ключ окружения
        # остаётся путём по умолчанию для позиций Bloom.
        if секрет is not None and not str(секрет).strip():
            итог["why_not"] = "передан пустой секрет -- подписывать нельзя"
            шаги.append({"step": "key", "ok": False, "why_not": итог["why_not"]})
            return итог
        if секрет is None:
            есть, почему = ключ_есть()
            if not есть:
                итог["why_not"] = почему
                шаги.append({"step": "key", "ok": False, "why_not": почему})
                return итог
        свой = ключ_от_нашего_кошелька(taker, секрет)
        шаги.append({"step": "key", "ok": bool(свой.get("ok")),
                      "pubkey": свой.get("pubkey"), "why_not": свой.get("why_not")})
        if not свой.get("ok"):
            итог["why_not"] = свой.get("why_not")
            return итог
        from dbot_rescue import (  # noqa: PLC0415
            load_rescue_keypair, sign_versioned_b64)

        def подписать_фн(tx_b64):  # noqa: E306
            return sign_versioned_b64(
                tx_b64, load_rescue_keypair(секрет if секрет is not None
                                             else ключ_сырой()))

    if пути:
        order = None
        отказы: list = []
        пробовали: list = []
        for имя, о_фн, и_фн in пути:
            пробовали.append(имя)
            order = о_фн(mint, int(amount_raw), taker, slippage_bps=пол_bps())
            шаги.append({"step": "order", "api": имя,
                          "ok": not order.get("ошибка"),
                          "why_not": order.get("ошибка"),
                          "out_amount": order.get("outAmount"),
                          "threshold": order.get("otherAmountThreshold"),
                          "router": order.get("router")})
            if not order.get("ошибка"):
                итог["api_used"] = имя
                исполнить_фн = и_фн
                break
            отказы.append(f"{имя}: {order['ошибка']}")
        else:
            # Ни один путь не дал заказ. Имя пути в причине обязательно:
            # прежний текст всегда говорил "Ultra", каким бы путь ни был, и
            # по журналу было не понять, что вообще пробовали.
            итог["why_not"] = "заказ не дал ни один путь: " + "; ".join(отказы)
            итог["api_tried"] = list(пробовали)
            return итог
        итог["api_tried"] = list(пробовали)
        if итог["api_used"] != итог["api"]:
            шаги.append({"step": "api_fallback", "ok": True,
                          "api": итог["api_used"],
                          "why_not": "; ".join(отказы)})
    else:
        order = ордер_фн(mint, int(amount_raw), taker, slippage_bps=пол_bps())
        шаги.append({"step": "order", "ok": not order.get("ошибка"),
                      "why_not": order.get("ошибка"),
                      "out_amount": order.get("outAmount"),
                      "threshold": order.get("otherAmountThreshold"),
                      "router": order.get("router")})
        if order.get("ошибка"):
            итог["why_not"] = f"заказ не вышел: {order['ошибка']}"
            return итог

    проверка = проверить_пол(
        order, вход_sol=вход_sol,
        **({} if мин_доля_от_входа is None
            else {"мин_доля_от_входа": float(мин_доля_от_входа)}))
    шаги.append({"step": "floor", **проверка})
    итог["floor"] = проверка
    if not проверка.get("ok"):
        итог["why_not"] = проверка.get("why_not")
        итог["unsold"] = bool(проверка.get("unsold"))
        return итог

    if not живьём:
        итог.update(ok=True, dry_run=True,
                     reason="проверка пройдена, отправка выключена (не живьём)")
        шаги.append({"step": "execute", "skipped": True, "why_not": "не живьём"})
        return итог

    try:
        подписанная = подписать_фн(order["transaction"])
    except Exception as exc:  # noqa: BLE001
        итог["why_not"] = f"подпись не удалась: {type(exc).__name__}"
        шаги.append({"step": "sign", "ok": False, "why_not": итог["why_not"]})
        return итог
    шаги.append({"step": "sign", "ok": True})

    ответ = исполнить_фн(подписанная, order["requestId"])
    шаги.append({"step": "execute", "ok": not ответ.get("ошибка"),
                  "status": ответ.get("status"), "signature": ответ.get("signature"),
                  "why_not": ответ.get("ошибка")})
    итог.update(ok=not ответ.get("ошибка"), signature=ответ.get("signature"),
                 status=ответ.get("status"), why_not=ответ.get("ошибка"))
    return итог


def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    chk("пол 70 % -- это 3000 bps", пол_bps(70) == 3000, пол_bps(70))
    chk("пол 100 % -- это 0 bps", пол_bps(100) == 0, пол_bps(100))

    ордер = {"outAmount": "1000000", "otherAmountThreshold": "700000",
              "swapMode": "ExactIn", "slippageBps": 3000, "router": "jupiterz"}
    r = проверить_пол(ордер, вход_sol=0.001)
    chk("порог ровно 70 % проходит", r["ok"] is True, r)
    chk("и в проверке видно оба числа",
        r["checks"]["threshold"] == 700000 and r["checks"]["floor_needed"] == 700000, r)

    r2 = проверить_пол({**ордер, "otherAmountThreshold": "699999"}, вход_sol=0.001)
    chk("порог на единицу ниже пола НЕ проходит", r2["ok"] is False, r2)
    chk("и причина -- числами", "ниже пола" in r2["why_not"], r2["why_not"])

    r3 = проверить_пол({k: v for k, v in ордер.items() if k != "otherAmountThreshold"},
                        вход_sol=0.001)
    chk("нет порога -- отказ, а не подпись наудачу",
        r3["ok"] is False and "otherAmountThreshold" in r3["why_not"], r3)
    r4 = проверить_пол({k: v for k, v in ордер.items() if k != "outAmount"})
    chk("нет котировки -- отказ", r4["ok"] is False and "outAmount" in r4["why_not"], r4)
    r5 = проверить_пол({**ордер, "swapMode": "ExactOut"}, вход_sol=0.001)
    chk("ExactOut не принимается: порог там означает другое", r5["ok"] is False, r5)

    # правило "котировка меньше 30 % от входа -- не продавать"
    r6 = проверить_пол({"outAmount": "200000", "otherAmountThreshold": "140000",
                         "swapMode": "ExactIn"}, вход_sol=0.001)
    chk("котировка 20 % от входа -- не продаём",
        r6["ok"] is False and r6.get("unsold") is True, r6)
    chk("и доля названа числом", r6["checks"]["quote_share_of_entry_pct"] == 20.0, r6)
    r7 = проверить_пол({"outAmount": "400000", "otherAmountThreshold": "280000",
                         "swapMode": "ExactIn"}, вход_sol=0.001)
    chk("котировка 40 % от входа -- продаём", r7["ok"] is True, r7)
    r8 = проверить_пол({"outAmount": "200000", "otherAmountThreshold": "140000",
                         "swapMode": "ExactIn"}, вход_sol=None)
    chk("вход неизвестен -- правило 30 % не применяется, пол работает",
        r8["ok"] is True and "quote_share_of_entry_pct" not in r8["checks"], r8)

    # что лежит в байтах транзакции
    tx_кот = base64.b64encode(b"\x09" + (895562).to_bytes(8, "little")
                               + (3000).to_bytes(2, "little") + b"\x07").decode()
    вб = что_в_байтах(tx_кот, порог=626893, котировка=895562, slippage_bps=3000)
    chk("порога прямым числом нет, но котировка и проскальзывание найдены",
        вб["threshold_found"] is False and вб["quote_found"] is True
        and вб["slippage_found"] is True and "пол обеспечен" in вб["note"], вб)
    tx_пор = base64.b64encode(b"\x09" + (626893).to_bytes(8, "little")).decode()
    вб2 = что_в_байтах(tx_пор, порог=626893, котировка=895562, slippage_bps=3000)
    chk("порог прямым числом опознан",
        вб2["threshold_found"] is True and "прямым числом" in вб2["note"], вб2)
    вб3 = что_в_байтах(base64.b64encode(b"\x01\x02\x03\x04").decode(),
                        порог=626893, котировка=895562, slippage_bps=3000)
    chk("ничего не нашлось -- сказано прямо, без успокоения",
        вб3["threshold_found"] is False and "НЕ пройдена" in вб3["note"], вб3)

    # порог в байтах транзакции
    tx = base64.b64encode(b"\x01\x02" + (700000).to_bytes(8, "little") + b"\x03").decode()
    п = порог_в_байтах(tx, 700000)
    chk("порог найден в байтах", п["known"] and п["found"] is True, п)
    п2 = порог_в_байтах(tx, 123456)
    chk("чужого порога в байтах нет, и это сказано",
        п2["known"] and п2["found"] is False and "RFQ" in п2["note"], п2)
    chk("битая база64 -- честный отказ проверки",
        порог_в_байтах("!!!", 1)["known"] is False)

    # полный проход без сети
    посл = {}

    def ордер_ок(mint, amount, taker, slippage_bps=None):
        посл["slippage_bps"] = slippage_bps
        return {"transaction": tx, "requestId": "R1", "outAmount": "1000000",
                 "otherAmountThreshold": "700000", "swapMode": "ExactIn",
                 "slippageBps": slippage_bps, "router": "metis"}

    def исполнить_ок(signed, request_id):
        посл["request_id"] = request_id
        посл["signed"] = signed
        return {"status": "Success", "signature": "ПОДПИСЬ_JUP"}

    r9 = продать(mint="M", amount_raw=92278326, taker="W", вход_sol=0.001, живьём=True,
                  ордер_фн=ордер_ок, исполнить_фн=исполнить_ок,
                  подписать_фн=lambda t: "ПОДПИСАННАЯ")
    chk("живой проход дошёл до отправки", r9["ok"] is True and r9["signature"] == "ПОДПИСЬ_JUP", r9)
    chk("проскальзывание в запросе -- ровно пол", посл["slippage_bps"] == 3000, посл)
    chk("requestId ушёл в execute", посл["request_id"] == "R1", посл)

    подписей = []

    def подпись_считаем(t):
        подписей.append(t)
        return "ПОДПИСАННАЯ"

    r10 = продать(mint="M", amount_raw=1, taker="W", вход_sol=0.001, живьём=True,
                   ордер_фн=lambda *a, **k: {"transaction": tx, "requestId": "R",
                                              "outAmount": "1000000",
                                              "otherAmountThreshold": "1",
                                              "swapMode": "ExactIn"},
                   исполнить_фн=исполнить_ок, подписать_фн=подпись_считаем)
    chk("ордер ниже пола НЕ подписывается вообще",
        r10["ok"] is False and подписей == [], (r10.get("why_not"), подписей))

    r11 = продать(mint="M", amount_raw=1, taker="W", вход_sol=0.001, живьём=False,
                   ордер_фн=ордер_ок, исполнить_фн=исполнить_ок,
                   подписать_фн=подпись_считаем)
    chk("не живьём -- проверка есть, отправки нет",
        r11["ok"] is True and r11.get("dry_run") is True
        and len(подписей) == 0, r11)

    r12 = продать(mint="M", amount_raw=1, taker="W", вход_sol=0.001, живьём=True,
                   ордер_фн=lambda *a, **k: {"ошибка": "http=429"},
                   исполнить_фн=исполнить_ок, подписать_фн=подпись_считаем)
    chk("Ultra отказала -- честный отказ без подписи",
        r12["ok"] is False and "429" in r12["why_not"] and len(подписей) == 0, r12)

    # Сверка публичного ключа. Пара создаётся здесь же и к деньгам отношения
    # не имеет: проверяется механика, а не чей-то настоящий ключ.
    try:
        from solders.keypair import Keypair  # noqa: PLC0415
        свежая = Keypair()
        секрет = str(свежая)
        мой = str(свежая.pubkey())
        chk("публичный ключ из секрета выводится", публичный_ключ(секрет)["pubkey"] == мой,
            публичный_ключ(секрет))
        chk("свой кошелёк опознан", ключ_от_нашего_кошелька(мой, секрет)["ok"] is True)
        чужой = ключ_от_нашего_кошелька("4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N",
                                          секрет)
        chk("чужой кошелёк -- запрет подписи",
            чужой["ok"] is False and "подписывать нельзя" in чужой["why_not"], чужой)
        chk("мусор вместо ключа -- отказ без печати секрета",
            публичный_ключ("НЕ_КЛЮЧ")["ok"] is False
            and "НЕ_КЛЮЧ" not in публичный_ключ("НЕ_КЛЮЧ")["why_not"],
            публичный_ключ("НЕ_КЛЮЧ"))

        # живой проход с чужим ключом не должен дойти до подписи
        подписей2 = []
        было_имя = os.environ.get("EXEC_WALLET_KEY")
        os.environ["EXEC_WALLET_KEY"] = секрет
        try:
            r_чужой = продать(mint="M", amount_raw=1,
                               taker="4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N",
                               вход_sol=0.001, живьём=True, ордер_фн=ордер_ок,
                               исполнить_фн=исполнить_ок)
            chk("живьём с ключом ЧУЖОГО кошелька -- отказ до подписи",
                r_чужой["ok"] is False and "подписывать нельзя" in r_чужой["why_not"],
                r_чужой.get("why_not"))
            chk("и имя EXEC_WALLET_KEY читается",
                ключ_есть()[0] is True, ключ_есть())
        finally:
            if было_имя is None:
                os.environ.pop("EXEC_WALLET_KEY", None)
            else:
                os.environ["EXEC_WALLET_KEY"] = было_имя
        del подписей2
    except ImportError:
        chk("solders доступен для сверки ключа", False, "solders не установлен")

    было = os.environ.pop("BLOOM_WALLET_KEY", None)
    было_exec = os.environ.pop("EXEC_WALLET_KEY", None)
    try:
        есть, почему = ключ_есть()
        chk("без ключа путь через Jupiter недоступен и причина названа",
            есть is False and "BLOOM_WALLET_KEY" in почему, почему)
        r13 = продать(mint="M", amount_raw=1, taker="W", вход_sol=0.001, живьём=True,
                       ордер_фн=ордер_ок, исполнить_фн=исполнить_ок)
        chk("живьём без ключа -- отказ на шаге ключа, ордер не подписан",
            r13["ok"] is False and "BLOOM_WALLET_KEY" in r13["why_not"], r13)
        r14 = продать(mint="M", amount_raw=1, taker="W", вход_sol=0.001, живьём=False,
                       ордер_фн=ордер_ок, исполнить_фн=исполнить_ок)
        chk("а проверка котировки без ключа проходит: ключ ей не нужен",
            r14["ok"] is True and r14.get("dry_run") is True, r14)
    finally:
        if было is not None:
            os.environ["BLOOM_WALLET_KEY"] = было
        if было_exec is not None:
            os.environ["EXEC_WALLET_KEY"] = было_exec

    # Имена в общем клиенте проверяются ЗДЕСЬ, а не в бою: первый живой
    # прогон упал именно на несуществующем имени load_keypair, потому что
    # ленивый импорт внутри ветки самотестом не покрывался.
    import dbot_rescue as DR  # noqa: PLC0415
    for имя in ("ultra_order", "ultra_execute", "sign_versioned_b64",
                 "load_rescue_keypair"):
        chk(f"в общем клиенте есть {имя}", hasattr(DR, имя), имя)
    chk("ultra_order принимает slippage_bps",
        "slippage_bps" in DR.ultra_order.__code__.co_varnames,
        DR.ultra_order.__code__.co_varnames)

    src = Path(__file__).read_text(encoding="utf-8")
    # --- выбор пути API: V2 по умолчанию при ключе, Ultra как запасной
    было_ключ = os.environ.get("JUPITER_API_KEY")
    было_путь = os.environ.get("BLOOM_JUP_API")
    try:
        os.environ.pop("JUPITER_API_KEY", None)
        os.environ.pop("BLOOM_JUP_API", None)
        r_у = продать(mint="М", amount_raw=100, taker="Т", вход_sol=None,
                       живьём=False)
        chk("без ключа V2 путь -- Ultra", r_у.get("api") == "ultra", r_у.get("api"))
        os.environ["JUPITER_API_KEY"] = "КЛЮЧ"
        r_в = продать(mint="М", amount_raw=100, taker="Т", вход_sol=None,
                       живьём=False)
        chk("с ключом путь по умолчанию -- Swap V2", r_в.get("api") == "swap-v2",
            r_в.get("api"))
        os.environ["BLOOM_JUP_API"] = "ultra"
        r_н = продать(mint="М", amount_raw=100, taker="Т", вход_sol=None,
                       живьём=False)
        chk("вход BLOOM_JUP_API=ultra сильнее наличия ключа",
            r_н.get("api") == "ultra", r_н.get("api"))
        chk("и выбор пути виден в шагах записи",
            any(ш.get("step") == "api" for ш in (r_н.get("steps") or [])), r_н.get("steps"))

        # План и сама продажа обязаны говорить одно: правило одно.
        for окружение, ожидание in (({}, ["ultra"]),
                                     ({"JUPITER_API_KEY": "К"}, ["swap-v2", "ultra"]),
                                     ({"JUPITER_API_KEY": "К", "BLOOM_JUP_API": "ultra"},
                                      ["ultra"]),
                                     ({"BLOOM_JUP_API": "v2"}, ["swap-v2", "ultra"])):
            os.environ.pop("JUPITER_API_KEY", None)
            os.environ.pop("BLOOM_JUP_API", None)
            os.environ.update(окружение)
            п = план_путей()
            r_п = продать(mint="М", amount_raw=100, taker="Т", вход_sol=None,
                           живьём=False)
            chk(f"план и продажа согласны при {окружение or 'пустом окружении'}",
                п["plan"] == ожидание and r_п.get("api_plan") == ожидание,
                (п["plan"], r_п.get("api_plan")))

        # ---- ULTRA -- ЗАПАСНОЙ НА ДЕЛЕ, А НЕ НА СЛОВАХ (владелец 25.09) ----
        # Прежде путь выбирался ОДИН раз до сети: 401 по ключу, 429 или 5xx
        # от api.jup.ag означали не переход на Ultra, а потерянный выход и
        # позицию в UNSOLD. Проверяем именно откат -- и его границу.
        os.environ.pop("BLOOM_JUP_API", None)
        os.environ["JUPITER_API_KEY"] = "КЛЮЧ"
        import dbot_rescue as DR2  # noqa: PLC0415
        было_v2, было_ultra = DR2.swap_v2_order, DR2.ultra_order
        было_v2_и, было_ultra_и = DR2.swap_v2_execute, DR2.ultra_execute
        звали: list = []
        try:
            def v2_падает(mint, amount_raw, taker, **kw):
                звали.append("v2")
                return {"ошибка": "http=401 unauthorized"}

            def ultra_даёт(mint, amount_raw, taker, **kw):
                звали.append("ultra")
                return {"transaction": "AAA", "requestId": "r1",
                         "outAmount": "1000", "otherAmountThreshold": "900",
                         "swapMode": "ExactIn", "router": "jupiterz"}

            DR2.swap_v2_order = v2_падает
            DR2.ultra_order = ultra_даёт
            r_о = продать(mint="М", amount_raw=100, taker="Т", вход_sol=None,
                           живьём=False)
            chk("V2 отказал на заказе -- заказ взят у Ultra",
                r_о.get("ok") is True and r_о.get("api") == "swap-v2"
                and r_о.get("api_used") == "ultra"
                and r_о.get("api_tried") == ["swap-v2", "ultra"],
                {к: r_о.get(к) for к in ("ok", "api", "api_used", "api_tried")})
            chk("порядок путей соблюдён: сначала V2, потом Ultra",
                звали == ["v2", "ultra"], звали)
            chk("откат виден отдельным шагом с причиной",
                any(ш.get("step") == "api_fallback" and "401" in (ш.get("why_not") or "")
                    for ш in (r_о.get("steps") or [])), r_о.get("steps"))

            звали.clear()
            DR2.ultra_order = lambda mint, amount_raw, taker, **kw: (
                звали.append("ultra") or {"ошибка": "http=500"})
            r_оба = продать(mint="М", amount_raw=100, taker="Т", вход_sol=None,
                             живьём=False)
            chk("отказали оба пути -- причина называет ОБА, а не 'Ultra'",
                r_оба.get("ok") is False and "swap-v2" in r_оба["why_not"]
                and "ultra" in r_оба["why_not"] and "401" in r_оба["why_not"],
                r_оба.get("why_not"))
            chk("и попытки перечислены", r_оба.get("api_tried") == ["swap-v2", "ultra"],
                r_оба.get("api_tried"))

            # ГРАНИЦА ОТКАТА. После подписи и отправки откат ЗАПРЕЩЁН:
            # транзакция могла уйти в сеть, и второй путь означал бы вторую
            # продажу. Проверяем, что на отказе ОТПРАВКИ второй путь не зовётся.
            звали.clear()
            отправок: list = []
            DR2.swap_v2_order = lambda mint, amount_raw, taker, **kw: (
                звали.append("v2") or {"transaction": "AAA", "requestId": "r1",
                                        "outAmount": "1000",
                                        "otherAmountThreshold": "900",
                                        "swapMode": "ExactIn"})
            DR2.ultra_order = lambda mint, amount_raw, taker, **kw: (
                звали.append("ultra") or {"transaction": "BBB", "requestId": "r2",
                                           "outAmount": "1000",
                                           "otherAmountThreshold": "900",
                                           "swapMode": "ExactIn"})

            def v2_отправка_падает(signed, request_id, **kw):
                отправок.append(("v2", request_id))
                return {"ошибка": "http=503 на отправке"}

            def ultra_отправка(signed, request_id, **kw):
                отправок.append(("ultra", request_id))
                return {"signature": "ПОДПИСЬ", "status": "Success"}

            DR2.swap_v2_execute = v2_отправка_падает
            DR2.ultra_execute = ultra_отправка
            r_отпр = продать(mint="М", amount_raw=100, taker="Т", вход_sol=None,
                              живьём=True, подписать_фн=lambda tx: "ПОДПИСАННАЯ")
            chk("отказ ОТПРАВКИ второй путь не зовёт -- иначе это вторая продажа",
                r_отпр.get("ok") is False and отправок == [("v2", "r1")]
                and звали == ["v2"], (отправок, звали, r_отпр.get("why_not")))
        finally:
            DR2.swap_v2_order, DR2.ultra_order = было_v2, было_ultra
            DR2.swap_v2_execute, DR2.ultra_execute = было_v2_и, было_ultra_и
    finally:
        os.environ.pop("JUPITER_API_KEY", None)
        os.environ.pop("BLOOM_JUP_API", None)
        if было_ключ is not None:
            os.environ["JUPITER_API_KEY"] = было_ключ
        if было_путь is not None:
            os.environ["BLOOM_JUP_API"] = было_путь

    # ОТДЕЛЬНЫЙ СЕКРЕТ (кошелёк полосы). Проверка та же, что у ключа
    # окружения: несовпадение адреса -- запрет, а не предупреждение.
    было_имя = os.environ.get("EXEC_WALLET_KEY")
    try:
        os.environ.pop("EXEC_WALLET_KEY", None)
        пустой = продать(mint="M", amount_raw=1, taker="W", вход_sol=0.001,
                          живьём=True, секрет="   ")
        chk("пустой переданный секрет -- отказ до всякой сети",
            пустой["ok"] is False and "пустой секрет" in (пустой["why_not"] or ""),
            пустой.get("why_not"))
        чужой = продать(mint="M", amount_raw=1, taker="ЧУЖОЙ_КОШЕЛЁК",
                         вход_sol=0.001, живьём=True, секрет="[1,2,3]")
        chk("переданный секрет тоже сверяется с taker",
            чужой["ok"] is False
            and any(ш.get("step") == "key" and not ш.get("ok")
                    for ш in (чужой.get("steps") or [])),
            чужой.get("why_not"))
    finally:
        if было_имя is not None:
            os.environ["EXEC_WALLET_KEY"] = было_имя

    тело = src.split("def self_test")[0]
    chk("секрет полосы доходит до подписи, а не подменяется ключом окружения",
        "секрет if секрет is not None" in тело)
    chk("секрет ключа в текст ошибок не подставляется",
        "type(exc).__name__" in тело.split("def публичный_ключ")[1].split("def ")[0])
    chk("подпись строго после проверки пола",
        тело.index("проверить_пол(order") < тело.index("подписать_фн(order"))

    плохо = [c for c in проверки if not c[1]]
    for имя, ок, факт in проверки:
        print(f"{'OK ' if ок else 'НЕТ'} {имя}"
              f"{(' -- ' + json.dumps(факт, ensure_ascii=False, default=str)[:300]) if факт and not ок else ''}")
    print(f"самопроверка продажи через Jupiter: {len(проверки) - len(плохо)}/{len(проверки)}")
    return 1 if плохо else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--quote-only", nargs=3, metavar=("MINT", "AMOUNT_RAW", "TAKER"),
                     help="ордер и проверка пола БЕЗ подписи и отправки")
    ap.add_argument("--entry-sol", type=float, default=None)
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if a.quote_only:
        м, сумма, кто = a.quote_only
        print(json.dumps(продать(mint=м, amount_raw=int(сумма), taker=кто,
                                  вход_sol=a.entry_sol, живьём=False),
                          ensure_ascii=False, indent=1))
    else:
        print(json.dumps({"floor_pct": ПОЛ_ПРОЦЕНТОВ, "floor_bps": пол_bps(),
                           "min_quote_share_pct": МИН_ДОЛЯ_ОТ_ВХОДА,
                           "key": ключ_есть()[1] or "ключ есть"}, ensure_ascii=False))
