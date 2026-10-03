#!/usr/bin/env python3
"""Закрыть ПУСТЫЕ токен-счета и вернуть запертую в них ренту.

ЗАЧЕМ. По цепи в 320 незакрытых счетах двух кошельков заперто 0.714136 SOL, и
0.488980 из них заперла сама ночь 25->26.09 (замер: у каждого счёта спрошена
дата рождения, data/token_accounts_age_*.json). Покупка счёт создаёт, продажа
его не закрывает -- деньги не потеряны, они лежат. Вернуть их можно только
закрытием счёта, а это ЗАПИСЬ В ЦЕПЬ, поэтому здесь всё построено так, чтобы
ошибка стоила ноль:

* ПО УМОЛЧАНИЮ НИЧЕГО НЕ ОТПРАВЛЯЕТСЯ. Отправка только при
  CLOSE_ACCOUNTS_LIVE=1; без флага прогон печатает план и суммы;
* ЗАКРЫВАЕМ ТОЛЬКО ПУСТЫЕ, и ТОЛЬКО НЕ-WSOL. Раньше счёт WSOL считался
  исключением (остаток -- завёрнутый SOL, вернулся бы владельцу), но слово
  владельца 03.10 вечером: WSOL не трогать вовсе -- полоса создаёт его на
  каждой покупке;
* СЧЁТ МОЛОДОЙ СДЕЛКИ НЕ ТРОГАЕМ (слово владельца 03.10): минты позиций,
  тронутых за окно max(hold_slots) * 0.4 с + 600 с запаса, из плана
  выбрасываются. Журнал позиций не прочитался -- прогон останавливается, а не
  считает все счета старыми;
* ПОЛУЧАТЕЛЬ РЕНТЫ -- ТОЛЬКО САМ КОШЕЛЁК. Адреса получателя нет ни в
  аргументах, ни в окружении: он равен владельцу счёта по построению;
* ЧУЖОЙ СЧЁТ НЕ ЗАКРЫВАЕТСЯ: владелец счёта обязан совпасть с нашим
  кошельком, а публичный ключ -- с ним же (проверка перед подписью);
* СВЕЖЕСТЬ ЧТЕНИЯ. Остаток перечитывается прямо перед сборкой пакета; чтение
  старше 30 с не годится. Счёт, у которого остаток изменился, из пакета
  выпадает;
* ПРИ ТОРГОВЛЕ НЕ РАБОТАЕМ. Полоса на каждой покупке создаёт счёт WSOL, и
  закрывать счета вслепую посреди сделки нельзя: нужен либо стоящий рубильник
  KILL, либо явное CLOSE_ACCOUNTS_WHILE_TRADING=1.

Ключ -- только из окружения (EXEC_WALLET_KEY / BLOOM_WALLET_KEY для
исполнителя, OWN_SEND_WALLET_KEY для полосы -- ровно те имена, что уже стоят в
окружении службы на хосте). В журнал, вывод и Telegram он не
попадает ни при какой ошибке: наружу идёт только имя класса исключения.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_exec_state as ST  # noqa: E402

WSOL = "So11111111111111111111111111111111111111112"
ТОКЕН = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ТОКЕН22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ПРОГРАММЫ_ТОКЕНА = (ТОКЕН, ТОКЕН22)
ЗАКРЫТЬ = 9                      # CloseAccount у обеих программ токена
СРОК_СВЕЖЕСТИ_S = 30.0
# СЛОВО ВЛАДЕЛЬЦА 03.10 ВЕЧЕРОМ (п.2б): "Не трогать: WSOL, счета открытых
# позиций, счета моложе максимального удержания". Счёт WSOL раньше считался
# годным (остаток -- завёрнутый SOL, он вернулся бы владельцу), но полоса
# создаёт его на КАЖДОЙ покупке: закрывать его уборщиком нельзя вовсе, иначе
# суточный таймер однажды попадёт в миг между созданием счёта и покупкой.
НЕ_ЗАКРЫВАТЬ_МИНТЫ = (WSOL,)
# Счёт моложе максимального удержания -- это счёт сделки, которая ещё может
# быть жива. Отсекаем по минтам позиций, тронутых за это окно: число берётся
# из политик групп (hold_slots), а не из памяти, и к нему прибавляется запас
# на отказ продавца (give_up_after_s = 600 с).
ЗАПАС_НА_ПРОДАВЦА_S = 600.0
ВОЗРАСТ_ПО_УМОЛЧАНИЮ_S = 1800.0
В_ПАКЕТЕ = 12
ПРЕДЕЛ_РАЗМЕРА_TX = 1232
РЕНТА_ОБЫЧНОГО = 0.00203928      # реальную ренту берём из цепи, это только
РЕНТА_ЛАМПОРТОВ = 2_039_280      # ожидание для сверки


def _узел() -> str:
    """Адрес узла: ключ Helius из окружения, как во всех прогонах репозитория.
    Своего адреса в коде нет -- только сборка из ключа."""
    ключ = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    if not ключ:
        raise RuntimeError("ключа узла нет в окружении (HELIUS_API_KEY)")
    return f"https://mainnet.helius-rpc.com/?api-key={ключ}"


def _вызов_узла(метод: str, парам: list, *, срок: float = 25.0):
    """Один вызов JSON-RPC. Отдельно от solana_crowd_scan намеренно: на хосте
    службы того модуля нет, а прогон обязан работать именно там -- ключ кошелька
    полосы живёт только в окружении службы."""
    import requests  # noqa: PLC0415
    от = requests.post(_узел(), json={"jsonrpc": "2.0", "id": 1, "method": метод,
                                      "params": парам}, timeout=срок)
    от.raise_for_status()
    тело = от.json()
    if "error" in тело:
        raise RuntimeError(f"узел ответил ошибкой: {str(тело['error'])[:160]}")
    return тело.get("result")


def живой_режим() -> bool:
    return (os.environ.get("CLOSE_ACCOUNTS_LIVE") or "0").strip() == "1"


def _политики_групп() -> dict:
    """{имя: политика} из файла групп. Файл не прочитан -- исключение наружу."""
    import bloom_source_groups as SG  # noqa: PLC0415

    д = SG.загрузить(заново=True) or {}
    if д.get("why_not"):
        raise RuntimeError(str(д["why_not"])[:160])
    return д.get("политики") or {}


def окно_без_покупок(состояние=None, политики=None) -> tuple:
    """(можно, почему). Окно, в котором купить не может НИКТО.

    ЗАЧЕМ ТРЕТИЙ ПУТЬ (слово владельца 29.09). Общий рубильник закрывает и
    Bloom, и полосу сразу -- потому он и стоял в условии первым. Но ставить его
    ради возврата ренты владелец запретил прямо ("общий KILL ради 0.003 SOL не
    трогать"), и флаг CLOSE_ACCOUNTS_WHILE_TRADING запретил тоже. Третий путь
    не ослабляет условие: он требует РОВНО того же, чего требует рубильник, но
    по отдельности и каждое -- фактом:
      * рубильник ПОЛОСЫ стоит -- своей отправки не будет;
      * открытых позиций нет -- ни одна сделка не в воздухе;
      * ни одна группа не торгует через Bloom -- площадка не купит тоже.
    Незнание любого из трёх -- запрет, а не исключение.
    """
    try:
        сост = состояние if состояние is not None else ST.ExecState()
        стоит_полоса, почему_полоса = сост.lane_kill_active()
    except Exception as exc:  # noqa: BLE001
        return False, (f"рубильник полосы не проверить ({type(exc).__name__}) -- "
                        "закрывать счета нельзя")
    if not стоит_полоса:
        return False, ("рубильник KILL не стоит и полоса не остановлена: полоса "
                        "создаёт счёт WSOL на каждой покупке, закрывать счета "
                        "посреди торговли нельзя")
    try:
        открытых = len(сост.open_positions())
    except Exception as exc:  # noqa: BLE001
        return False, (f"открытые позиции не прочитать ({type(exc).__name__}) -- "
                        "закрывать счета нельзя")
    if открытых:
        return False, (f"открытых позиций {открытых}: сделка в воздухе, закрывать "
                        "её счёт нельзя")
    try:
        пол = политики if политики is not None else _политики_групп()
    except Exception as exc:  # noqa: BLE001
        return False, (f"политики групп не прочитать ({type(exc).__name__}: "
                        f"{str(exc)[:120]}) -- закрывать счета нельзя")
    торгуют = sorted(и for и, з in (пол or {}).items() if (з or {}).get("bloom_trades"))
    if торгуют:
        return False, ("через Bloom торгуют группы " + ", ".join(торгуют)
                        + ": площадка может купить в любой миг, закрывать счета "
                          "нельзя")
    return True, ("полоса остановлена, открытых позиций нет, через Bloom не "
                   "торгует ни одна группа: " + (почему_полоса or "")[:120])


def можно_сейчас(состояние=None, политики=None) -> tuple:
    """Закрывать счета можно при стоящем рубильнике, в окне без покупок или по
    явному флагу.

    Полоса создаёт счёт WSOL на каждой покупке. Закрыть его в момент, когда
    сделка в воздухе, -- это сломать сделку, а не вернуть ренту.
    """
    if (os.environ.get("CLOSE_ACCOUNTS_WHILE_TRADING") or "0").strip() == "1":
        return True, "разрешено явным флагом CLOSE_ACCOUNTS_WHILE_TRADING=1"
    try:
        стоит = ST.kill_file().exists()
    except Exception as exc:  # noqa: BLE001
        return False, f"рубильник не проверить ({type(exc).__name__})"
    if стоит:
        return True, "рубильник KILL стоит -- торговли нет"
    return окно_без_покупок(состояние, политики)


def минты_моложе(предел_с: float, состояние=None) -> tuple:
    """(множество минтов, почему_не). Минты позиций, тронутых за последние
    предел_с секунд.

    НЕЗНАНИЕ -- ЗАПРЕТ, а не пустое множество: вернуть пустой набор при
    нечитаемом журнале значило бы объявить все счета старыми и закрыть счёт
    живой сделки. Поэтому вторым значением идёт причина, и вызывающий обязан
    остановиться, а не продолжить с пустым набором.
    """
    try:
        сост = состояние if состояние is not None else ST.ExecState()
        поз = сост.positions()
    except Exception as exc:  # noqa: BLE001
        return set(), (f"журнал позиций не прочитать ({type(exc).__name__}: "
                        f"{str(exc)[:120]}) -- молодые счета не отсечь")
    порог = time.time() - float(предел_с)
    минты = set()
    for з in (поз or {}).values():
        if not isinstance(з, dict):
            continue
        т = з.get("ts_update") or з.get("ts_intent") or 0.0
        try:
            т = float(т)
        except (TypeError, ValueError):
            т = 0.0
        if т >= порог and з.get("mint"):
            минты.add(str(з["mint"]))
    return минты, None


def предел_молодости_s(политики=None) -> tuple:
    """(секунды, откуда). Максимальное удержание по политикам групп плюс запас
    на продавца. Политики не прочитались -- берём умолчание и говорим это."""
    try:
        пол = политики if политики is not None else _политики_групп()
    except Exception as exc:  # noqa: BLE001
        return ВОЗРАСТ_ПО_УМОЛЧАНИЮ_S, (f"политики групп не прочитать "
                                         f"({type(exc).__name__}) -- умолчание")
    слотов = [float(з.get("hold_slots") or 0.0) for з in (пол or {}).values()
               if isinstance(з, dict)]
    макс = max(слотов) if слотов else 0.0
    if макс <= 0:
        return ВОЗРАСТ_ПО_УМОЛЧАНИЮ_S, "hold_slots нигде не задан -- умолчание"
    # Длина слота берётся с запасом (0.4 с -- константа кода, живой замер
    # обычно 0.26): запас в сторону осторожности, а не оптимизма.
    секунд = макс * 0.4 + ЗАПАС_НА_ПРОДАВЦА_S
    return max(секунд, 60.0), f"max(hold_slots)={макс:.0f} * 0.4 с + запас 600 с"


def подходит_для_закрытия(счёт: dict, кошелёк: str, *,
                           молодые: set | None = None) -> tuple:
    """(годен, причина_отказа). Решение только по свежему чтению из цепи.

    молодые -- минты позиций, тронутых за окно максимального удержания; их
    счета не трогаем (слово владельца 03.10). None -- проверка уже сделана
    выше, в плане: так зовёт сборщик инструкции, которому план уже отобрал
    счета.
    """
    if not isinstance(счёт, dict):
        return False, "счёт не разобран"
    if счёт.get("program") not in ПРОГРАММЫ_ТОКЕНА:
        return False, f"программа счёта не токеновая: {счёт.get('program')}"
    if счёт.get("owner") != кошелёк:
        return False, f"владелец счёта {счёт.get('owner')}, а не наш кошелёк"
    сырое = str(счёт.get("amount_raw") if счёт.get("amount_raw") is not None else "")
    if not сырое.isdigit():
        return False, f"остаток не прочитан: {счёт.get('amount_raw')!r}"
    if str(счёт.get("mint") or "") in НЕ_ЗАКРЫВАТЬ_МИНТЫ:
        return False, ("счёт WSOL -- слово владельца 03.10: не трогать, полоса "
                        "создаёт его на каждой покупке")
    if молодые and str(счёт.get("mint") or "") in молодые:
        return False, ("минт тронут позицией моложе максимального удержания -- "
                        "счёт сделки, которая может быть жива")
    if счёт.get("is_native"):
        # Завёрнутый SOL -- это SOL, он вернётся владельцу вместе с рентой.
        # Сюда доходят только НЕ-WSOL нативные счета: WSOL отсечён выше.
        return True, None
    if int(сырое) != 0:
        return False, f"остаток не ноль ({сырое}) -- закрывать нельзя, токен потеряется"
    return True, None


def вернётся_лампортов(счёт: dict) -> int:
    """Сколько лампортов вернёт закрытие: рента счёта плюс завёрнутый SOL."""
    рента = int(счёт.get("lamports") or 0)
    # lamports счёта -- это и есть всё, что на нём лежит (рента, а у WSOL
    # рента плюс завёрнутое). Ничего не складываем дважды.
    return рента


def инструкция_закрытия(счёт: dict, кошелёк: str):
    from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    годен, почему = подходит_для_закрытия(счёт, кошелёк)
    if not годен:
        raise ValueError(f"счёт не годен к закрытию: {почему}")
    к = Pubkey.from_string(кошелёк)
    return Instruction(
        Pubkey.from_string(счёт["program"]), bytes([ЗАКРЫТЬ]),
        # ПОЛУЧАТЕЛЬ -- САМ КОШЕЛЁК. Второй метой стоит он же, и другого
        # адреса здесь взять негде: аргумента получателя у функции нет.
        [AccountMeta(Pubkey.from_string(счёт["account"]), False, True),
         AccountMeta(к, False, True),
         AccountMeta(к, True, True)])


def пакеты(счета: list, кошелёк: str, *, в_пакете: int = В_ПАКЕТЕ,
            молодые: set | None = None) -> dict:
    """План: годные счета по пакетам, негодные -- с причинами."""
    годные, отказы = [], []
    for с in счета:
        ок, почему = подходит_для_закрытия(с, кошелёк, молодые=молодые)
        (годные if ок else отказы).append(с if ок else {**с, "why_not": почему})
    группы = [годные[и:и + в_пакете] for и in range(0, len(годные), в_пакете)]
    return {"пакетов": len(группы), "счетов_годных": len(годные),
            "счетов_отказано": len(отказы),
            "вернётся_лампортов": sum(вернётся_лампортов(с) for с in годные),
            "группы": группы, "отказы": отказы}


def собрать_пакет(группа: list, кошелёк: str, *, cu_units: int = 200_000,
                  cu_price_micro: int = 0) -> dict:
    """Неподписанная транзакция закрытия пакета. Ни подписи, ни отправки."""
    from solders.hash import Hash  # noqa: PLC0415
    from solders.message import MessageV0  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    from solders.signature import Signature  # noqa: PLC0415
    from solders.transaction import VersionedTransaction  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    ixs = [B.cu_limit(cu_units)]
    if cu_price_micro:
        ixs.append(B.cu_price(cu_price_micro))
    ixs += [инструкция_закрытия(с, кошелёк) for с in группа]
    msg = MessageV0.try_compile(Pubkey.from_string(кошелёк), ixs, [], Hash.default())
    vtx = VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures)
    сырое = bytes(vtx)
    return {"tx_base64": base64.b64encode(сырое).decode(), "size": len(сырое),
            "счетов": len(группа), "n_instructions": len(ixs),
            "вернётся_лампортов": sum(вернётся_лампортов(с) for с in группа)}


def прочитать_счета(rpc, адреса: list, кошелёк: str) -> dict:
    """Свежее чтение: остаток, владелец, программа, лампорты на счёте."""
    из_ = {"utc": time.time(), "счета": [], "why_not": None}
    for и in range(0, len(адреса), 100):
        часть = адреса[и:и + 100]
        try:
            от = rpc("getMultipleAccounts", [часть, {"encoding": "jsonParsed",
                                                     "commitment": "confirmed"}]) or {}
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:100]}"
            return из_
        for адрес, зн in zip(часть, (от.get("value") or [])):
            if not зн:
                из_["счета"].append({"account": адрес, "why_not": "счёта в цепи нет"})
                continue
            инфо = (((зн.get("data") or {}).get("parsed") or {}).get("info") or {})
            сумма = (инфо.get("tokenAmount") or {})
            из_["счета"].append({
                "account": адрес, "program": зн.get("owner"),
                "lamports": зн.get("lamports"), "mint": инфо.get("mint"),
                "owner": инфо.get("owner"), "is_native": bool(инфо.get("isNative")),
                "amount_raw": сумма.get("amount"), "decimals": сумма.get("decimals")})
    return из_


def свежо(чтение: dict, *, срок: float = СРОК_СВЕЖЕСТИ_S, сейчас: float | None = None) -> bool:
    сейчас = time.time() if сейчас is None else сейчас
    return bool(чтение) and (сейчас - float(чтение.get("utc") or 0)) <= срок


def ключ_кошелька(кошелёк: str) -> dict:
    """Секрет ТОГО кошелька, чьи счета закрываем. Сам секрет наружу не идёт."""
    import bloom_jupiter_sell as J  # noqa: PLC0415
    имена = (("OWN_SEND_WALLET_KEY",) if кошелёк != ST.EXECUTOR_WALLET
             else ("EXEC_WALLET_KEY", "BLOOM_WALLET_KEY"))
    секрет = next(((os.environ.get(и) or "").strip() for и in имена
                   if (os.environ.get(и) or "").strip()), "")
    if not секрет:
        return {"ok": False, "why_not": f"ключа кошелька нет в окружении ({' или '.join(имена)})"}
    п = J.публичный_ключ(секрет)
    if not п.get("ok"):
        return {"ok": False, "why_not": п.get("why_not") or "ключ не разобрался"}
    if п["pubkey"] != кошелёк:
        return {"ok": False, "why_not": f"ключ принадлежит {п['pubkey']}, а счета кошелька "
                                        f"{кошелёк} -- подписывать нельзя"}
    return {"ok": True, "секрет": секрет}


def закрыть_живьём(rpc, кошелёк: str, группы: list, *, секрет: str,
                   ждать_s: float = 30.0, пауза_s: float = 2.0) -> dict:
    """Закрыть пакеты по цепи. Без CLOSE_ACCOUNTS_LIVE=1 сети не касается вовсе.

    Каждый пакет: свежее перечитывание счетов -> отбор годных -> сборка ->
    подпись -> отправка -> подтверждение -> баланс до и после. Первый же сбой
    останавливает прогон: неизвестный результат хуже недоделанной работы.
    """
    из_ = {"ok": False, "why_not": None, "пакетов": 0, "закрыто_счетов": 0,
           "вернулось_лампортов": 0, "шаги": []}
    if not живой_режим():
        из_["why_not"] = "живой режим выключен (CLOSE_ACCOUNTS_LIVE не равен 1)"
        return из_
    можно, почему = можно_сейчас()
    if not можно:
        из_["why_not"] = почему
        return из_
    try:
        from solders.hash import Hash  # noqa: PLC0415
        from solders.message import MessageV0  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415
        from dbot_rescue import load_rescue_keypair  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"модули подписи не загружены: {type(exc).__name__}"
        return из_
    try:
        kp = load_rescue_keypair(секрет)
    except Exception as exc:  # noqa: BLE001
        # Текст исключения наружу НЕ идёт: в него может попасть сам секрет.
        из_["why_not"] = f"ключ не разобрался ({type(exc).__name__})"
        return из_
    if str(kp.pubkey()) != кошелёк:
        из_["why_not"] = "публичный ключ не совпал с кошельком -- закрытие отменено"
        return из_

    def баланс() -> int:
        от = rpc("getBalance", [кошелёк, {"commitment": "confirmed"}]) or {}
        return int((от.get("value") if isinstance(от, dict) else от) or 0)

    for номер, группа in enumerate(группы, 1):
        шаг = {"пакет": номер, "просили": len(группа), "закрыто": 0, "подпись": None,
               "баланс_до": None, "баланс_после": None, "дельта_лампортов": None,
               "ожидали_лампортов": None, "why_not": None}
        try:
            чтение = прочитать_счета(rpc, [с["account"] for с in группа], кошелёк)
            if чтение.get("why_not") or not свежо(чтение):
                шаг["why_not"] = чтение.get("why_not") or "чтение счетов устарело"
                из_["шаги"].append(шаг)
                из_["why_not"] = шаг["why_not"]
                return из_
            годные = [с for с in чтение["счета"] if подходит_для_закрытия(с, кошелёк)[0]]
            шаг["закрыто"] = len(годные)
            шаг["ожидали_лампортов"] = sum(вернётся_лампортов(с) for с in годные)
            if not годные:
                шаг["why_not"] = "после свежего чтения годных счетов не осталось"
                из_["шаги"].append(шаг)
                continue
            шаг["баланс_до"] = баланс()
            собрано = собрать_пакет(годные, кошелёк)
            bh = ((rpc("getLatestBlockhash", [{"commitment": "finalized"}]) or {})
                  .get("value") or {}).get("blockhash")
            if not bh:
                шаг["why_not"] = "узел не дал blockhash"
                из_["шаги"].append(шаг)
                из_["why_not"] = шаг["why_not"]
                return из_
            сырое = base64.b64decode(собрано["tx_base64"])
            tx = VersionedTransaction.from_bytes(сырое)
            msg = tx.message
            новое = MessageV0(msg.header, msg.account_keys, Hash.from_string(bh),
                              msg.instructions, msg.address_table_lookups)
            подписанная = VersionedTransaction(новое, [kp])
            подпись = rpc("sendTransaction", [
                base64.b64encode(bytes(подписанная)).decode(),
                {"encoding": "base64", "skipPreflight": False, "maxRetries": 3,
                 "preflightCommitment": "confirmed"}])
            шаг["подпись"] = подпись
            срок = time.time() + ждать_s
            села = False
            while time.time() < срок:
                time.sleep(пауза_s)
                ст = ((rpc("getSignatureStatuses", [[подпись], {"searchTransactionHistory": True}])
                       or {}).get("value") or [None])[0]
                if ст and (ст.get("confirmationStatus") in ("confirmed", "finalized")
                           or ст.get("slot")):
                    села = ст.get("err") is None
                    шаг["ошибка_цепи"] = ст.get("err")
                    break
            шаг["баланс_после"] = баланс()
            шаг["дельта_лампортов"] = шаг["баланс_после"] - шаг["баланс_до"]
            if not села:
                шаг["why_not"] = "транзакция не села или села с ошибкой"
                из_["шаги"].append(шаг)
                из_["why_not"] = шаг["why_not"]
                return из_
            из_["пакетов"] += 1
            из_["закрыто_счетов"] += len(годные)
            из_["вернулось_лампортов"] += шаг["дельта_лампортов"]
        except Exception as exc:  # noqa: BLE001
            шаг["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            из_["шаги"].append(шаг)
            из_["why_not"] = шаг["why_not"]
            return из_
        из_["шаги"].append(шаг)
    из_["ok"] = из_["why_not"] is None
    return из_


def счета_кошелька(rpc, кошелёк: str) -> dict:
    """Токен-счета кошелька прямо из цепи: по одному вызову на программу токена.

    Файл со списком не нужен -- так свежесть обеспечена построением, а не
    надеждой на прошлый прогон.
    """
    из_ = {"utc": time.time(), "счета": [], "why_not": None}
    for программа in ПРОГРАММЫ_ТОКЕНА:
        try:
            от = rpc("getTokenAccountsByOwner", [кошелёк, {"programId": программа},
                                                 {"encoding": "jsonParsed",
                                                  "commitment": "confirmed"}]) or {}
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:100]}"
            return из_
        for зн in (от.get("value") or []):
            счёт = зн.get("pubkey")
            данные = (((зн.get("account") or {}).get("data") or {}).get("parsed") or {})
            инфо = данные.get("info") or {}
            сумма = инфо.get("tokenAmount") or {}
            из_["счета"].append({
                "account": счёт, "program": (зн.get("account") or {}).get("owner") or программа,
                "lamports": (зн.get("account") or {}).get("lamports"),
                "mint": инфо.get("mint"), "owner": инфо.get("owner"),
                "is_native": bool(инфо.get("isNative")),
                "amount_raw": сумма.get("amount"), "decimals": сумма.get("decimals")})
    return из_


def self_test() -> int:
    проверки = []

    def chk(имя, ок, что=""):
        проверки.append((имя, bool(ок), что))

    кош = ST.EXECUTOR_WALLET
    пустой = {"account": "11111111111111111111111111111112", "program": ТОКЕН,
              "lamports": РЕНТА_ЛАМПОРТОВ, "mint": "M", "owner": кош,
              "is_native": False, "amount_raw": "0"}
    с_остатком = dict(пустой, amount_raw="12345")
    всол = {"account": "11111111111111111111111111111113", "program": ТОКЕН,
            "lamports": 4_078_560, "mint": WSOL, "owner": кош,
            "is_native": True, "amount_raw": "1488440"}
    чужой = dict(пустой, owner="CHUZHOY1111111111111111111111111111111111111")

    chk("пустой счёт годен", подходит_для_закрытия(пустой, кош)[0] is True)
    ок_ост, почему_ост = подходит_для_закрытия(с_остатком, кош)
    chk("счёт с остатком НЕ закрывается, и причина про потерю токена",
        ок_ост is False and "токен потеряется" in (почему_ост or ""), почему_ост)
    ок_в, почему_в = подходит_для_закрытия(всол, кош)
    chk("счёт WSOL НЕ закрывается вовсе (слово владельца 03.10) и причина названа",
        ок_в is False and "WSOL" in (почему_в or "") and "03.10" in (почему_в or ""),
        почему_в)
    ок_м, почему_м = подходит_для_закрытия(пустой, кош, молодые={"M"})
    chk("счёт молодого минта не закрывается, и причина про живую сделку",
        ок_м is False and "моложе максимального удержания" in (почему_м or ""),
        почему_м)
    chk("тот же счёт при пустом наборе молодых -- годен",
        подходит_для_закрытия(пустой, кош, молодые=set())[0] is True)
    class СостМинты:
        """Журнал позиций для проверки окна молодости: без каталога и цепи."""

        def __init__(self, ряды):
            self._ряды = ряды

        def positions(self):
            return self._ряды

        def падать(self):  # noqa: D102
            raise RuntimeError

    сейчас_ = time.time()
    молодые_, почему_мол = минты_моложе(100.0, СостМинты({
        "c1": {"mint": "SVEZHIY", "ts_update": сейчас_ - 10.0},
        "c2": {"mint": "STARYY", "ts_update": сейчас_ - 5000.0},
        "c3": {"mint": None, "ts_update": сейчас_},
        "c4": {"mint": "BEZ_VREMENI"}}))
    chk("окно молодости берёт только свежие минты и не падает на мусоре",
        почему_мол is None and молодые_ == {"SVEZHIY"}, {"минты": sorted(молодые_),
                                                         "почему": почему_мол})

    class СостПадает:
        def positions(self):
            raise OSError("журнал недоступен")

    _пусто, почему_пад = минты_моложе(100.0, СостПадает())
    chk("нечитаемый журнал позиций -- причина, а не пустой набор",
        почему_пад is not None and "молодые счета не отсечь" in почему_пад, почему_пад)
    сек, откуда = предел_молодости_s({"g1": {"hold_slots": 108}, "g2": {"hold_slots": 36}})
    chk("окно молодости из политик: max(hold_slots)=108 -> 108*0.4+600 = 643.2 с",
        abs(сек - 643.2) < 1e-9 and "108" in откуда, {"сек": сек, "откуда": откуда})
    сек0, откуда0 = предел_молодости_s({"g1": {}})
    chk("hold_slots нигде нет -- умолчание 1800 с и это сказано",
        сек0 == ВОЗРАСТ_ПО_УМОЛЧАНИЮ_S and "умолчание" in откуда0, откуда0)

    # СУТОЧНЫЙ ЖУРНАЛ УБОРЩИКА И ОДНА СТРОКА ВЛАДЕЛЬЦУ (слово владельца 03.10,
    # п.2в: "сумма -- строкой в суточный TG").
    import tempfile as _tf  # noqa: PLC0415
    _кат = _tf.mkdtemp()
    _ж = str(Path(_кат) / "u.json")
    _а = дописать_в_журнал(_ж, {"закрыто_счетов": 12, "вернулось_sol": 0.024})
    _б = дописать_в_журнал(_ж, {"закрыто_счетов": 5, "вернулось_sol": 0.010})
    chk("суточный журнал уборщика складывает счета и SOL за те же сутки",
        _а["ok"] and _б["ok"] and _б["закрыто_за_сутки"] == 17
        and abs(_б["за_сутки_sol"] - 0.034) < 1e-9, _б)
    _послано = []

    def _послать(т):
        _послано.append(т)
        return {"ok": True}

    _с1 = послать_итог_суток(_ж, послать=_послать)
    _с2 = послать_итог_суток(_ж, послать=_послать)
    chk("строка суток уходит ОДИН раз и несёт оба числа",
        _с1["ok"] and _с1["уже_послана"] is False and _с2["уже_послана"] is True
        and len(_послано) == 1 and "17" in _послано[0] and "0.034" in _послано[0],
        _послано)
    _с3 = послать_итог_суток(_ж, "1999-01-01", послать=_послать)
    chk("за сутки без уборки строки нет, и это не сбой",
        _с3["ok"] is False and "не закрывал" in (_с3["why_not"] or ""), _с3["why_not"])

    def _падает(т):
        return {"ok": False, "why_not": "сеть"}

    _ж2 = str(Path(_кат) / "u2.json")
    дописать_в_журнал(_ж2, {"закрыто_счетов": 1, "вернулось_sol": 0.002})
    _с4 = послать_итог_суток(_ж2, послать=_падает)
    _с5 = послать_итог_суток(_ж2, послать=_послать)
    chk("Telegram не принял -- признак НЕ ставится, строка уйдёт в следующий раз",
        _с4["ok"] is False and _с5["ok"] is True and _с5["уже_послана"] is False,
        {"первая": _с4["why_not"], "вторая": _с5})

    ок_ч, почему_ч = подходит_для_закрытия(чужой, кош)
    chk("чужой счёт не закрывается", ок_ч is False and "владелец" in (почему_ч or ""), почему_ч)
    chk("нечитаемый остаток -- отказ, а не ноль по умолчанию",
        подходит_для_закрытия(dict(пустой, amount_raw=None), кош)[0] is False)
    chk("не токеновая программа -- отказ",
        подходит_для_закрытия(dict(пустой, program="XXX"), кош)[0] is False)

    # Получатель ренты -- только сам кошелёк, и подписант тоже он.
    ix = инструкция_закрытия(пустой, кош)
    адреса = [str(m.pubkey) for m in ix.accounts]
    chk("в инструкции закрытия получатель и подписант -- наш кошелёк, и никого больше",
        адреса == [пустой["account"], кош, кош] and bytes(ix.data) == bytes([ЗАКРЫТЬ])
        and [m.is_signer for m in ix.accounts] == [False, False, True], адреса)
    try:
        инструкция_закрытия(с_остатком, кош)
        chk("инструкция на счёт с остатком не собирается", False)
    except ValueError:
        chk("инструкция на счёт с остатком не собирается", True)

    # План: годные, отказанные и сумма возврата.
    план = пакеты([пустой, с_остатком, всол, чужой], кош, в_пакете=2)
    # ЧИСЛА ИЗМЕНИЛИСЬ СО СЛОВОМ ВЛАДЕЛЬЦА 03.10: WSOL больше не годен, и
    # возврат считается без его завёрнутого SOL.
    chk("план: годен 1, отказано 3 (остаток, WSOL, чужой), возврат -- только рента",
        план["счетов_годных"] == 1 and план["счетов_отказано"] == 3
        and план["вернётся_лампортов"] == РЕНТА_ЛАМПОРТОВ
        and план["пакетов"] == 1, {к: план[к] for к in ("пакетов", "счетов_годных",
                                                         "счетов_отказано",
                                                         "вернётся_лампортов")})
    # Пакет на 15 счетов влезает в предел сети.
    много = [dict(пустой, account=str(__import__("solders").pubkey.Pubkey.default()))] * 0
    try:
        from solders.keypair import Keypair  # noqa: PLC0415
        много = [dict(пустой, account=str(Keypair().pubkey())) for _ in range(15)]
        соб = собрать_пакет(много, кош)
        chk(f"пакет из 15 закрытий: {соб['size']} байт при пределе {ПРЕДЕЛ_РАЗМЕРА_TX}",
            соб["size"] <= ПРЕДЕЛ_РАЗМЕРА_TX and соб["счетов"] == 15, соб["size"])
    except Exception as exc:  # noqa: BLE001
        chk("пакет из 15 закрытий собирается", False, f"{type(exc).__name__}: {exc}")

    # Живой режим по умолчанию выключен; отправки в модуле нет вовсе.
    было = os.environ.pop("CLOSE_ACCOUNTS_LIVE", None)
    try:
        chk("без флага живой режим выключен", живой_режим() is False)
        os.environ["CLOSE_ACCOUNTS_LIVE"] = "1"
        chk("флаг включает живой режим", живой_режим() is True)
    finally:
        os.environ.pop("CLOSE_ACCOUNTS_LIVE", None)
        if было is not None:
            os.environ["CLOSE_ACCOUNTS_LIVE"] = было
    текст = Path(__file__).read_text(encoding="utf-8")
    # Отправка ровно одна и ровно в одном месте. Считаем вызовы, а не слова:
    # в самопроверке имя метода тоже встречается.
    рабочая_часть = текст.split("def self_test", 1)[0]
    chk("отправка в рабочей части ровно одна -- в закрыть_живьём",
        рабочая_часть.count("sendTransaction") == 1,
        рабочая_часть.count("sendTransaction"))

    # БЕЗ ФЛАГА СЕТИ НЕ КАСАЕМСЯ ВОВСЕ: rpc, который на любой вызов кидает.
    def rpc_запрещён(метод, парам):
        raise AssertionError(f"сети касаться нельзя, а позвали {метод}")

    было_live = os.environ.pop("CLOSE_ACCOUNTS_LIVE", None)
    try:
        без_флага = закрыть_живьём(rpc_запрещён, кош, [[пустой]], секрет="не ключ")
        chk("без CLOSE_ACCOUNTS_LIVE=1 отказ ДО любого вызова сети",
            без_флага["ok"] is False and "живой режим выключен" in (без_флага["why_not"] or ""),
            без_флага["why_not"])
        os.environ["CLOSE_ACCOUNTS_LIVE"] = "1"
        было_тр = os.environ.pop("CLOSE_ACCOUNTS_WHILE_TRADING", None)
        было_kf = os.environ.get("BLOOM_KILL_FILE")
        проба = Path(os.environ.get("TMPDIR") or "/tmp") / "close_accounts_kill_probe2"
        try:
            os.environ["BLOOM_KILL_FILE"] = str(проба)
            проба.unlink(missing_ok=True)
            без_kill = закрыть_живьём(rpc_запрещён, кош, [[пустой]], секрет="не ключ")
            chk("с флагом, но без рубильника -- отказ ДО сети",
                без_kill["ok"] is False and "KILL" in (без_kill["why_not"] or ""),
                без_kill["why_not"])
            проба.write_text("kill")
            чужой_ключ = закрыть_живьём(rpc_запрещён, кош, [[пустой]], секрет="не ключ")
            chk("плохой ключ -- отказ ДО сети, и текста исключения наружу нет",
                чужой_ключ["ok"] is False
                and "ключ не разобрался" in (чужой_ключ["why_not"] or "")
                and "не ключ" not in (чужой_ключ["why_not"] or ""),
                чужой_ключ["why_not"])
        finally:
            проба.unlink(missing_ok=True)
            if было_kf is None:
                os.environ.pop("BLOOM_KILL_FILE", None)
            else:
                os.environ["BLOOM_KILL_FILE"] = было_kf
            if было_тр is not None:
                os.environ["CLOSE_ACCOUNTS_WHILE_TRADING"] = было_тр
    finally:
        os.environ.pop("CLOSE_ACCOUNTS_LIVE", None)
        if было_live is not None:
            os.environ["CLOSE_ACCOUNTS_LIVE"] = было_live

    # СПИСОК СЧЕТОВ ИЗ ЦЕПИ: что разобрали, то и решаем закрывать. Ошибка
    # разбора здесь означала бы закрытие не того счёта, поэтому проверка есть.
    ответы = {
        ТОКЕН: {"value": [
            {"pubkey": "11111111111111111111111111111112",
             "account": {"owner": ТОКЕН, "lamports": РЕНТА_ЛАМПОРТОВ,
                         "data": {"parsed": {"info": {
                             "mint": "M", "owner": кош, "isNative": False,
                             "tokenAmount": {"amount": "0", "decimals": 6}}}}}},
            {"pubkey": "11111111111111111111111111111113",
             "account": {"owner": ТОКЕН, "lamports": 4_078_560,
                         "data": {"parsed": {"info": {
                             "mint": WSOL, "owner": кош, "isNative": True,
                             "tokenAmount": {"amount": "1488440", "decimals": 9}}}}}},
            {"pubkey": "11111111111111111111111111111114",
             "account": {"owner": ТОКЕН, "lamports": РЕНТА_ЛАМПОРТОВ,
                         "data": {"parsed": {"info": {
                             "mint": "M2", "owner": кош, "isNative": False,
                             "tokenAmount": {"amount": "777", "decimals": 6}}}}}}]},
        ТОКЕН22: {"value": []},
    }

    def rpc_список(метод, парам):
        assert метод == "getTokenAccountsByOwner", метод
        return ответы[парам[1]["programId"]]

    сп = счета_кошелька(rpc_список, кош)
    план_сп = пакеты(сп["счета"], кош, в_пакете=10)
    chk("список счетов из цепи разобран: три счёта, годен ОДИН "
        "(второй WSOL, третий с остатком)",
        len(сп["счета"]) == 3 and план_сп["счетов_годных"] == 1
        and план_сп["счетов_отказано"] == 2
        and план_сп["вернётся_лампортов"] == РЕНТА_ЛАМПОРТОВ,
        {"счетов": len(сп["счета"]), **{к: план_сп[к] for к in ("счетов_годных", "счетов_отказано")}})
    chk("в отказах списка названы и WSOL, и остаток",
        any("WSOL" in (о.get("why_not") or "") for о in план_сп["отказы"])
        and any("токен потеряется" in (о.get("why_not") or "") for о in план_сп["отказы"]),
        [о.get("why_not") for о in план_сп["отказы"]])
    chk("у счёта из цепи взяты владелец, минт, признак нативности и остаток",
        сп["счета"][1]["owner"] == кош and сп["счета"][1]["mint"] == WSOL
        and сп["счета"][1]["is_native"] is True and сп["счета"][1]["amount_raw"] == "1488440",
        сп["счета"][1])

    # Свежесть чтения: старое чтение не годится.
    chk("чтение старше 30 с не свежее",
        свежо({"utc": 1000.0}, сейчас=1031.0) is False and свежо({"utc": 1000.0}, сейчас=1029.0) is True)

    # Рубильник: без него и без явного флага закрывать нельзя.
    class СостЗаглушка:
        """Состояние службы для проверок запрета: без каталога и без цепи."""

        def __init__(self, полоса=(True, "полоса остановлена: окно"), позиций=0,
                      падать=None):
            self._полоса, self._позиций, self._падать = полоса, позиций, падать

        def lane_kill_active(self):
            if self._падать == "полоса":
                raise OSError("состояние не читается")
            return self._полоса

        def open_positions(self):
            if self._падать == "позиции":
                raise OSError("журнал позиций не читается")
            return [{"cid": f"c{и}"} for и in range(self._позиций)]

    было_фл = os.environ.pop("CLOSE_ACCOUNTS_WHILE_TRADING", None)
    было_kill = os.environ.get("BLOOM_KILL_FILE")
    tmp = Path(os.environ.get("TMPDIR") or "/tmp") / "close_accounts_kill_probe"
    try:
        os.environ["BLOOM_KILL_FILE"] = str(tmp)
        tmp.unlink(missing_ok=True)
        # Состояние подставляется заглушкой: проверка запрета не должна зависеть
        # от того, есть ли на этой машине каталог состояния службы.
        нельзя, почему_нельзя = можно_сейчас(СостЗаглушка(полоса=(False, "")),
                                              {"lane_s0": {"bloom_trades": False}})
        chk("без рубильника и без флага закрывать нельзя",
            нельзя is False and "KILL" in (почему_нельзя or ""), почему_нельзя)
        tmp.write_text("kill")
        chk("при стоящем рубильнике можно", можно_сейчас()[0] is True)
        tmp.unlink(missing_ok=True)

        # ТРЕТИЙ ПУТЬ: окно без покупок. Проверяется по отдельности каждое из
        # трёх условий -- и что незнание любого из них запрещает закрытие.
        мирно = {"lane_s0": {"bloom_trades": False}, "batch5": {"bloom_trades": False}}
        можно3, почему3 = можно_сейчас(СостЗаглушка(), мирно)
        chk("окно без покупок разрешает закрытие без общего рубильника",
            можно3 is True and "полоса остановлена" in (почему3 or ""), почему3)
        chk("полоса не остановлена -- запрет",
            можно_сейчас(СостЗаглушка(полоса=(False, "")), мирно)[0] is False)
        chk("открытая позиция -- запрет",
            можно_сейчас(СостЗаглушка(позиций=1), мирно)[0] is False)
        chk("группа торгует через Bloom -- запрет",
            можно_сейчас(СостЗаглушка(),
                          {**мирно, "leader": {"bloom_trades": True}})[0] is False)
        chk("рубильник полосы не прочитать -- запрет",
            можно_сейчас(СостЗаглушка(падать="полоса"), мирно)[0] is False)
        chk("позиции не прочитать -- запрет",
            можно_сейчас(СостЗаглушка(падать="позиции"), мирно)[0] is False)
        # Политики групп не прочитаны -- тоже запрет, а не "значит, никто не
        # торгует". Подменяется ровно то место, которое их читает.
        было_пол = globals()["_политики_групп"]

        def _падающие():
            raise RuntimeError("файла групп нет")

        globals()["_политики_групп"] = _падающие
        try:
            нельзя_пол, почему_пол = можно_сейчас(СостЗаглушка())
        finally:
            globals()["_политики_групп"] = было_пол
        chk("политики групп не прочитать -- запрет",
            нельзя_пол is False and "политики групп" in (почему_пол or ""), почему_пол)

        os.environ["CLOSE_ACCOUNTS_WHILE_TRADING"] = "1"
        chk("явный флаг разрешает и без рубильника", можно_сейчас()[0] is True)
    finally:
        tmp.unlink(missing_ok=True)
        os.environ.pop("CLOSE_ACCOUNTS_WHILE_TRADING", None)
        if было_фл is not None:
            os.environ["CLOSE_ACCOUNTS_WHILE_TRADING"] = было_фл
        if было_kill is None:
            os.environ.pop("BLOOM_KILL_FILE", None)
        else:
            os.environ["BLOOM_KILL_FILE"] = было_kill

    плохих = 0
    for имя, ок, что in проверки:
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}" + (f"  -> {что}" if not ок and что != "" else ""))
        плохих += (not ок)
    print(f"самопроверка закрытия счетов: {len(проверки) - плохих}/{len(проверки)} пройдено")
    return 0 if плохих == 0 else 1


ЖУРНАЛ_УБОРЩИКА = "uborshchik_sutki.json"


def _день_полосы(ts: float | None = None) -> str:
    """Сутки полосы -- те же, что у учёта: граница 22:00Z (Europe/Madrid)."""
    try:
        # day_key отдаёт ПАРУ (дата, пояс_недоступен): берём дату, иначе ключом
        # суток стала бы строка вида "('2026-10-03', False)".
        return str(ST.day_key(ts if ts is not None else time.time())[0])
    except Exception:  # noqa: BLE001
        # Незнание границы суток не повод терять число: кладём по UTC и
        # говорим это самим ключом.
        return time.strftime("utc-%Y-%m-%d", time.gmtime(ts or time.time()))


def дописать_в_журнал(путь: str, факт: dict, *, день: str | None = None) -> dict:
    """Сложить возврат ренты за сутки. Падение здесь не отменяет возврата:
    деньги уже на кошельке, поэтому причина идёт в ответ, а не в исключение."""
    из_ = {"ok": False, "why_not": None, "день": день or _день_полосы(),
            "за_сутки_sol": None, "закрыто_за_сутки": None}
    try:
        ф = Path(путь)
        д = {}
        if ф.exists():
            д = json.loads(ф.read_text(encoding="utf-8")) or {}
        дни = д.setdefault("дни", {})
        з = дни.setdefault(из_["день"], {"закрыто": 0, "вернулось_sol": 0.0,
                                          "прогонов": 0, "строка_послана": False})
        з["закрыто"] = int(з.get("закрыто") or 0) + int(факт.get("закрыто_счетов") or 0)
        з["вернулось_sol"] = round(float(з.get("вернулось_sol") or 0.0)
                                    + float(факт.get("вернулось_sol") or 0.0), 9)
        з["прогонов"] = int(з.get("прогонов") or 0) + 1
        з["обновлено_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        д["обновлено_utc"] = з["обновлено_utc"]
        ф.parent.mkdir(parents=True, exist_ok=True)
        врем = ф.with_suffix(ф.suffix + ".tmp")
        врем.write_text(json.dumps(д, ensure_ascii=False, indent=1), encoding="utf-8")
        врем.replace(ф)
        из_.update(ok=True, за_сутки_sol=з["вернулось_sol"],
                    закрыто_за_сутки=з["закрыто"])
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return из_


def строка_суток(день: str, з: dict) -> str:
    """Одна строка владельцу: сколько ренты вернули за сутки."""
    return (f"уборщик ренты за {день}: закрыто счетов {int(з.get('закрыто') or 0)}, "
            f"вернулось {float(з.get('вернулось_sol') or 0.0):.9f} SOL, "
            f"прогонов {int(з.get('прогонов') or 0)}")


def послать_итог_суток(путь: str, день: str | None = None, *,
                        послать=None) -> dict:
    """Строка в Telegram раз в сутки. Дважды за те же сутки не посылаем:
    признак строка_послана лежит в том же файле, что и сумма."""
    из_ = {"ok": False, "why_not": None, "текст": None, "уже_послана": False}
    д_ = день or _день_полосы()
    try:
        ф = Path(путь)
        if not ф.exists():
            из_["why_not"] = f"журнала уборщика нет: {путь}"
            return из_
        д = json.loads(ф.read_text(encoding="utf-8")) or {}
        з = (д.get("дни") or {}).get(д_)
        if not з:
            из_["why_not"] = f"за {д_} уборщик не закрывал ничего"
            return из_
        if з.get("строка_послана"):
            из_.update(ok=True, уже_послана=True, текст=строка_суток(д_, з))
            return из_
        текст = строка_суток(д_, з)
        из_["текст"] = текст
        if послать is None:
            import bloom_notify as NT  # noqa: PLC0415
            послать = NT.Notifier(в_фоне=False).отправить
        ответ = послать(текст) or {}
        if not ответ.get("ok"):
            из_["why_not"] = f"Telegram не принял: {str(ответ.get('why_not'))[:120]}"
            return из_
        з["строка_послана"] = True
        врем = ф.with_suffix(ф.suffix + ".tmp")
        врем.write_text(json.dumps(д, ensure_ascii=False, indent=1), encoding="utf-8")
        врем.replace(ф)
        из_["ok"] = True
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--accounts", default="",
                   help="файл со списком счетов (пусто -- спросить цепь: так свежесть "
                        "обеспечена построением, а не надеждой на прошлый прогон)")
    р.add_argument("--wallet", default="", help="кошелёк (пусто -- из файла или исполнитель)")
    р.add_argument("--batch", type=int, default=В_ПАКЕТЕ)
    р.add_argument("--max-accounts", type=int, default=0, help="0 -- все годные")
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "close_accounts_plan.json"))
    р.add_argument("--result", default=str(КОРЕНЬ / "data" / "close_accounts_result.json"))
    р.add_argument("--max-batches", type=int, default=1,
                   help="сколько пакетов закрывать за прогон (1 -- по умолчанию, осторожно)")
    р.add_argument("--first-one", action="store_true",
                   help="первый прогон: закрыть РОВНО ОДИН счёт и сверить возврат")
    р.add_argument("--zhurnal", default="",
                   help="файл суточного итога уборщика (пусто -- рядом с --result)")
    р.add_argument("--tg-itog", default="",
                   help="послать строку за сутки и выйти: 'segodnja' или ключ суток")
    р.add_argument("--min-age-s", type=float, default=0.0,
                   help="окно молодости в секундах (0 -- считать по политикам групп: "
                        "max(hold_slots) * 0.4 с + запас 600 с)")
    а = р.parse_args()
    if а.self_test:
        return self_test()

    путь_журнала = а.zhurnal or str(Path(а.result).parent / ЖУРНАЛ_УБОРЩИКА)
    if а.tg_itog:
        д_ = None if а.tg_itog.strip().lower() in ("segodnja", "сегодня", "yes") else а.tg_itog
        итог = послать_итог_суток(путь_журнала, д_)
        print(json.dumps(итог, ensure_ascii=False))
        # СТРОКИ НЕТ -- ЭТО НЕ СБОЙ ТАЙМЕРА: за сутки могло не открыться ни
        # одного окна, и закрывать было нечего. Падать на этом значило бы
        # учить не читать настоящие падения.
        return 0

    кошелёк = а.wallet
    адреса = None
    if а.accounts:
        д = json.loads(Path(а.accounts).read_text(encoding="utf-8"))
        кошелёк = кошелёк or д.get("wallet") or ""
        адреса = [с["account"] for с in (д.get("empty") or []) + (д.get("with_balance") or [])]
    кошелёк = кошелёк or ST.EXECUTOR_WALLET
    if not кошелёк:
        print("СТОП: кошелёк не задан", file=sys.stderr)
        return 2

    вызовов = [0]

    def rpc(метод: str, парам: list):
        вызовов[0] += 1
        return _вызов_узла(метод, парам)

    print(f"кошелёк {кошелёк}; список счетов: "
          + ("из файла" if адреса is not None else "из цепи"))
    if адреса is None:
        найдено = счета_кошелька(rpc, кошелёк)
        if найдено.get("why_not"):
            print(f"СТОП: счета не прочитаны: {найдено['why_not']}", file=sys.stderr)
            return 2
        адреса = [с["account"] for с in найдено["счета"]]
    if а.max_accounts:
        адреса = адреса[:а.max_accounts]
    print(f"счетов к проверке {len(адреса)}")

    чтение = прочитать_счета(rpc, адреса, кошелёк)
    if чтение.get("why_not"):
        print(f"СТОП: счета не прочитаны: {чтение['why_not']}", file=sys.stderr)
        return 2
    # МОЛОДЫЕ СЧЕТА -- СЛОВО ВЛАДЕЛЬЦА 03.10: счёт сделки, которая ещё может
    # быть жива, не трогаем. Незнание окна -- остановка, а не пустой набор.
    предел_с, откуда_предел = ((а.min_age_s, "вход --min-age-s")
                                if а.min_age_s > 0 else предел_молодости_s())
    молодые, почему_молодые = минты_моложе(предел_с)
    if почему_молодые:
        print(f"СТОП: {почему_молодые}", file=sys.stderr)
        return 2
    план = пакеты(чтение["счета"], кошелёк, в_пакете=(1 if а.first_one else а.batch),
                   молодые=молодые)
    можно, почему_можно = можно_сейчас()
    итог = {
        "кошелёк": кошелёк, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "живой_режим": живой_режим(), "можно_сейчас": можно, "почему": почему_можно,
        "счетов_проверено": len(чтение["счета"]),
        "окно_молодости_s": round(float(предел_с), 1),
        "окно_молодости_откуда": откуда_предел,
        "минтов_молодых": len(молодые),
        "wsol_не_закрываем": True,
        "счетов_годных": план["счетов_годных"], "счетов_отказано": план["счетов_отказано"],
        "вернётся_sol": round(план["вернётся_лампортов"] / 1_000_000_000, 9),
        "пакетов": план["пакетов"],
        "размеры_пакетов": [собрать_пакет(г, кошелёк)["size"] for г in план["группы"]],
        "отказы": [{"account": о["account"], "why_not": о.get("why_not")} for о in план["отказы"]],
    }
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({к: v for к, v in итог.items() if к != "отказы"}, ensure_ascii=False, indent=1))
    if not живой_режим():
        print("ЖИВОГО РЕЖИМА НЕТ (CLOSE_ACCOUNTS_LIVE не равен 1): ничего не отправлено, "
              "это только план.")
        return 0
    if not можно:
        print(f"СТОП: {почему_можно}", file=sys.stderr)
        return 3
    к = ключ_кошелька(кошелёк)
    if not к.get("ok"):
        print(f"СТОП: {к.get('why_not')}", file=sys.stderr)
        return 4
    группы = план["группы"][:max(1, а.max_batches)] if not а.first_one else план["группы"][:1]
    факт = закрыть_живьём(rpc, кошелёк, группы, секрет=к["секрет"])
    факт["ожидали_sol"] = round(sum(вернётся_лампортов(с) for г in группы for с in г)
                                / 1_000_000_000, 9)
    факт["вернулось_sol"] = round(факт["вернулось_лампортов"] / 1_000_000_000, 9)
    # НАШИ ПОДПИСИ -- В ЖУРНАЛ СЛУЖЕБНЫХ, ЧТОБЫ СВЕРКА НЕ СЧИТАЛА ИХ ЧУЖИМИ.
    # Закрытие счетов -- не сделка, поэтому в записях позиций его нет, и сверка
    # при старте (bloom_reconcile) объявляла нашу же подпись "чужой свежей
    # активностью на кошельке": именно так вышло 29.09 в 18:42:50Z с подписью
    # r4THZ8F6... (12 счетов проверено, 9 закрыто, вернулось 0.02381504 SOL).
    # На стенде такой блокер смертелен, а регулярно врущий блокер учит не читать
    # настоящий.
    #
    # ЗАПИСЬ НЕ ДОЛЖНА РОНЯТЬ ПРОГОН: деньги уже вернулись на кошелёк, и падение
    # на ведении журнала не отменит и не откатит этого. Причина -- в отчёт.
    факт["sluzhebnye_podpisi_zapisany"] = 0
    факт["sluzhebnye_podpisi_why_not"] = None
    try:
        import bloom_exec_state as _ST  # noqa: PLC0415
        сост = _ST.ExecState()
        for шаг in (факт.get("шаги") or []):
            if сост.zapisat_sluzhebnuju_podpis(
                    шаг.get("подпись"), кто="close_empty_token_accounts",
                    почему=(f"закрытие пустых токен-счетов кошелька {кошелёк}, "
                            f"закрыто {шаг.get('закрыто')}")):
                факт["sluzhebnye_podpisi_zapisany"] += 1
    except Exception as exc:  # noqa: BLE001
        факт["sluzhebnye_podpisi_why_not"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    факт["sutki"] = дописать_в_журнал(путь_журнала, факт)
    Path(а.result).write_text(json.dumps(факт, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(факт, ensure_ascii=False, indent=1))
    return 0 if факт.get("ok") else 5


if __name__ == "__main__":
    raise SystemExit(main())
