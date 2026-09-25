#!/usr/bin/env python3
"""З1: КТО СТОЯЛ ПЕРЕД НАМИ. Цена места в блоке -- только цепь, без денег.

Вопрос владельца 25.09 буквально: наше отставание (700 мс до посадки, слот
S+1) -- это ПЛАТА (очередь к горячему пулу) или ПУТЬ И СКОРОСТЬ. Плата и
путь различаются не рассуждением, а фактом: если перед нами в нашем же
блоке стоят транзакции, заплатившие МЕНЬШЕ нас, то место покупается не
деньгами -- нас обогнали скоростью. Если они платили больше -- обогнали
платой, и тогда у места есть цена, которую можно посчитать.

ЧТО СЧИТАЕТСЯ НА ОДНУ НАШУ ПОКУПКУ (два getBlock, два кредита):

  * все транзакции ТОГО ЖЕ ПУЛА в слоте источника (S+0) и в нашем слоте --
    место в блоке, плата и сторона сделки;
  * а) сколько транзакций ПЕРЕД нами в нашем слоте платили МЕНЬШЕ нас;
  * б) сколько платили больше;
  * в) в слоте источника ПОСЛЕ него -- были ли транзакции дешевле нашей
    (значит, в S+0 можно было попасть без переплаты);
  * г) какой платой садились в S+0 сразу за источником: минимум, 25-й
    процентиль, медиана, 75-й процентиль.

ЧТО ТАКОЕ "ПЛАТА" ЗДЕСЬ. Плата = приоритет + чаевые, в лампортах:

  * приоритет = meta.fee минус базовый тариф 5000 лампортов за подпись.
    Это ФАКТИЧЕСКИ уплаченное, а не заявленное: заявленную цену единицы CU
    мы тоже читаем (инструкция ComputeBudget SetComputeUnitPrice), но
    сравнивать транзакции между собой honest только по уплаченному;
  * чаевые = лампорты, пришедшие на счёт чаевых системным переводом внутри
    этой же транзакции. Счёт чаевых опознаётся двумя способами: по списку
    из репозитория (docs/helius_sender_max -- те же адреса принимает и
    Jito) и ПО ЧАСТОТЕ в этом же блоке (адрес, которому платят пять и
    более разных плательщиков). Имена ускорителей не придумываются: у
    неопознанного адреса в отчёте стоит сам адрес, а не догадка.

МИКРОЛАМПОРТЫ НА CU. Заявленная цена берётся из инструкции; уплаченная на
израсходованную единицу считается отдельным полем. Путать их нельзя: сеть
списывает цену за ЗАПРОШЕННЫЙ предел CU, а израсходовано обычно меньше,
поэтому "уплачено на израсходованную единицу" всегда не ниже заявленной.

ЧЕГО ЗДЕСЬ НЕТ. Ни одной отправки: модуль только читает. Он не отвечает на
вторую половину вопроса (путь и скорость) -- это делают З2 (контроль пути
пустой транзакцией) и З3 (лестница платы), и у них своя цена в SOL.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_followers_growth as CF  # noqa: E402

# Тариф сети за подпись. Он же базовая часть meta.fee: всё, что выше --
# приоритет. Значение сетевое, не наше: 5000 лампортов за подпись.
ТАРИФ_ПОДПИСИ_ЛАМПОРТЫ = 5000
ЛАМПОРТОВ_В_SOL = 1_000_000_000
# Уровень версий транзакций. Узел отказывает на целом блоке, если в нём
# есть транзакция версии выше заявленной: первый прогон 25.09 получил
# "Transaction version (1) is not supported" и не разобрал ни одного блока.
# Значение то же, что у детектора (BLOOM_MAX_TX_VERSION, по умолчанию 1).
ПОТОЛОК_ВЕРСИИ_TX = int((os.environ.get("BLOOM_MAX_TX_VERSION") or "1").strip() or 1)
# Порог опознания счёта чаевых ПО ЧАСТОТЕ: столько разных плательщиков
# должно заплатить одному адресу в одном блоке. Тот же приём и тот же порог,
# что в c2_followers_growth (TIP_MIN_WALLETS).
ПОРОГ_ЧАЕВЫХ_ПЛАТЕЛЬЩИКОВ = 5


def _tip_список() -> tuple:
    """Известные счета чаевых из репозитория. Отдельной функцией, чтобы
    модуль грузился и там, где полосы нет вовсе."""
    try:
        import bloom_own_send as OS  # noqa: PLC0415
        return tuple(OS.TIP_ACCOUNTS)
    except Exception:  # noqa: BLE001
        return ()


def метка_чаевых(адрес: str, *, известные: tuple = (), по_частоте: set | None = None):
    """Чей это счёт чаевых. Придумывать названия ускорителей нельзя: адрес
    без метки честнее метки без источника."""
    if адрес in tuple(известные or ()):
        return "tip Helius Sender / Jito (список репозитория)"
    if по_частоте and адрес in по_частоте:
        return f"счёт чаевых по частоте (>= {ПОРОГ_ЧАЕВЫХ_ПЛАТЕЛЬЩИКОВ} плательщиков в блоке)"
    return None


# --------------------------------------------------------------- один блок

def _ключи(t: dict) -> list:
    сообщение = ((t.get("transaction") or {}).get("message") or {})
    из_ = []
    for к in (сообщение.get("accountKeys") or []):
        из_.append(к.get("pubkey") if isinstance(к, dict) else к)
    return [к for к in из_ if к]


def _подписанты(t: dict) -> set:
    сообщение = ((t.get("transaction") or {}).get("message") or {})
    ключи = сообщение.get("accountKeys") or []
    свои = {к.get("pubkey") for к in ключи
            if isinstance(к, dict) and к.get("signer") and к.get("pubkey")}
    if свои:
        return свои
    # encoding=json: флагов нет, но подписантов ровно столько, сколько
    # подписей, и они всегда в начале списка.
    подписей = len((t.get("transaction") or {}).get("signatures") or [])
    плоские = _ключи(t)
    return set(плоские[:подписей]) if подписей else set()


def _подпись(t: dict) -> str | None:
    подписи = (t.get("transaction") or {}).get("signatures") or []
    return подписи[0] if подписи else None


def сторона(t: dict, минт: str) -> str | None:
    """Покупка это или продажа -- по знаку дельты минта у ПОДПИСАНТА.

    Владелец пула тоже меняет остаток, и притом в противоположную сторону,
    поэтому смотреть надо именно на подписанта: он и есть тот, кто торгует.
    Ни одного подписанта с движением минта -- значит транзакция трогала пул
    не сделкой (сбор платы, создание счёта), и сторона неизвестна: None, а
    не "покупка".
    """
    мета = t.get("meta") or {}
    подписанты = _подписанты(t)
    дельты: dict = {}
    for где, знак in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for б in (мета.get(где) or []):
            if not isinstance(б, dict) or б.get("mint") != минт or not б.get("owner"):
                continue
            try:
                сырое = int((б.get("uiTokenAmount") or {}).get("amount") or 0)
            except (TypeError, ValueError):
                continue
            дельты[б["owner"]] = дельты.get(б["owner"], 0) + знак * сырое
    свои = {в: д for в, д in дельты.items() if в in подписанты and д}
    if not свои:
        return None
    крупнейший = max(свои.items(), key=lambda п: abs(п[1]))
    return "покупка" if крупнейший[1] > 0 else "продажа"


def сторона_по_торговцу(торг: dict) -> str | None:
    """Сторона сделки по найденному торговцу -- включая тех, кто держит токен
    на счёте своей программы. Без этого 267 из 313 транзакций остались бы без
    стороны только потому, что подписант не владелец счёта."""
    д = (торг or {}).get("delta")
    if not д:
        return None
    return "покупка" if д > 0 else "продажа"


def торговец(t: dict, минт: str, *, адреса_пула: set | None = None) -> dict:
    """Кто здесь торгует и что у него было ДО этой транзакции.

    Торговец -- подписант с наибольшим по модулю движением минта. Его остаток
    ДО сделки отвечает на вопрос владельца "реакция на источник или
    независимая толпа": нулевой (или отсутствующий) остаток до покупки значит,
    что кошелёк купил этот токен ВПЕРВЫЕ -- то есть приехал на событие. Уже
    ненулевой остаток значит, что он этим токеном торгует и до нас.

    Ни одного вызова сети: всё берётся из pre/postTokenBalances блока.
    """
    мета = t.get("meta") or {}
    подписанты = _подписанты(t)
    было: dict = {}
    стало: dict = {}
    for где, куда in (("preTokenBalances", было), ("postTokenBalances", стало)):
        for б in (мета.get(где) or []):
            if not isinstance(б, dict) or б.get("mint") != минт or not б.get("owner"):
                continue
            try:
                сырое = int((б.get("uiTokenAmount") or {}).get("amount") or 0)
            except (TypeError, ValueError):
                continue
            куда[б["owner"]] = куда.get(б["owner"], 0) + сырое
    дельты = {в: стало.get(в, 0) - было.get(в, 0)
              for в in set(было) | set(стало)}
    свои = {в: д for в, д in дельты.items() if в in подписанты and д}
    откуда = "подписант"
    if not свои:
        # ПОДПИСАНТ НЕ ВСЕГДА ВЛАДЕЛЕЦ СЧЁТА. Бот держит токен на счёте,
        # принадлежащем ЕГО ПРОГРАММЕ (PDA), и тогда подписант минт не двигает
        # вовсе. Первый прогон 25.09 отнёс так 267 из 313 транзакций в
        # "неясно" -- то есть почти всё. Берём наибольшее движение среди
        # владельцев, кроме самого пула, и честно помечаем, откуда взяли.
        пул = set(адреса_пула or ())
        чужие = {в: д for в, д in дельты.items() if д and в not in пул}
        if not чужие:
            return {"wallet": None, "delta": None, "held_before": None,
                    "first_buy": None, "trader_from": None,
                    "why_not": "минт в этой транзакции не двигался ни у кого, кроме пула"}
        свои, откуда = чужие, "владелец счёта (не подписант)"
    кош = max(свои.items(), key=lambda п: abs(п[1]))[0]
    до = было.get(кош, 0)
    return {"wallet": кош, "delta": свои[кош], "held_before": до,
            # ПЕРВАЯ ПОКУПКА -- только когда это покупка: у продажи остаток до
            # неё по определению был, и "первой покупкой" её называть нельзя.
            "first_buy": (свои[кош] > 0 and до == 0),
            "trader_from": откуда, "why_not": None}


def долговременный_nonce(t: dict) -> bool:
    """Идёт ли транзакция с долговременным nonce.

    Признак однозначный: ПЕРВАЯ инструкция -- AdvanceNonceAccount системной
    программы (в jsonParsed это parsed.type == "advanceNonce", без парсера --
    код 4 в данных). Такой транзакции не нужен свежий blockhash: её можно
    подписать заранее и держать наготове -- ровно то, что делает тот, кто
    хочет уехать в первом же блоке.
    """
    инструкции = (((t.get("transaction") or {}).get("message") or {})
                  .get("instructions") or [])
    if not инструкции:
        return False
    первая = инструкции[0] or {}
    разобрано = (первая.get("parsed") or {})
    if isinstance(разобрано, dict) and разобрано.get("type") in (
            "advanceNonce", "advanceNonceAccount"):
        return True
    if первая.get("programId") == "11111111111111111111111111111111" and первая.get("data"):
        try:
            сырое = CF.b58decode(первая["data"])
        except (ValueError, IndexError):
            return False
        return len(сырое) >= 4 and сырое[:4] == b"\x04\x00\x00\x00"
    return False


def чаевые_блока_по_индексу(транзакции: list, *, чаевые_адреса: set) -> dict:
    """Индекс транзакции -> множество счетов чаевых, которым она платила.

    Нужно для признака бандла: бандл едет подряд и платит одному и тому же
    получателю чаевых, поэтому сосед по индексу с тем же адресом чаевых --
    это единственный признак бандла, который виден из блока. Признак
    вероятностный, и назван он именно так.
    """
    из_: dict = {}
    for и, t in enumerate(транзакции):
        куда = set()
        for _, получатель, лам in CF.sol_transfers(t):
            if получатель in чаевые_адреса and лам > 0:
                куда.add(получатель)
        if куда:
            из_[и] = куда
    return из_


def похоже_на_бандл(по_индексу: dict, индекс: int) -> bool:
    """Сосед по индексу платит тому же счёту чаевых -- похоже на бандл."""
    свои = по_индексу.get(индекс)
    if not свои:
        return False
    for рядом in (индекс - 1, индекс + 1):
        if свои & (по_индексу.get(рядом) or set()):
            return True
    return False


def плата_транзакции(t: dict, *, чаевые_адреса: set) -> dict:
    """Приоритет, чаевые и их сумма -- в лампортах, по фактам транзакции."""
    cb = CF.compute_budget(t)
    приоритет = cb.get("priority_lamports")
    чаевые: dict = {}
    for откуда, куда, лам in CF.sol_transfers(t):
        if куда in чаевые_адреса and лам > 0:
            чаевые[куда] = чаевые.get(куда, 0) + int(лам)
    сумма_чаевых = sum(чаевые.values())
    плата = None if приоритет is None else int(приоритет) + сумма_чаевых
    израсходовано = cb.get("cu_consumed")
    уплачено_за_cu = None
    if приоритет is not None and израсходовано:
        уплачено_за_cu = int(int(приоритет) * 1_000_000 // int(израсходовано))
    return {"priority_lamports": приоритет, "tips_lamports": сумма_чаевых,
            "tips": чаевые, "pay_lamports": плата,
            "pay_sol": (None if плата is None else плата / ЛАМПОРТОВ_В_SOL),
            "cu_price_micro_declared": cb.get("cu_price_micro"),
            "cu_limit": cb.get("cu_limit"), "cu_consumed": израсходовано,
            "paid_micro_per_cu_consumed": уплачено_за_cu}


def адреса_чаевых_блока(транзакции: list, *, известные: tuple) -> dict:
    """Счета чаевых этого блока: известные плюс опознанные по частоте.

    По частоте -- потому что список ускорителей в репозитории неполон, а
    выдумывать имена нельзя. Адрес, которому в одном блоке платят пять и
    более разных плательщиков, ведёт себя как счёт чаевых, и это факт
    выборки, а не название.
    """
    получатели: dict = {}
    for t in транзакции:
        плательщик = (_ключи(t) or [None])[0]
        if not плательщик:
            continue
        for _, куда, лам in CF.sol_transfers(t):
            if куда and лам > 0 and куда != плательщик:
                получатели.setdefault(куда, set()).add(плательщик)
    по_частоте = {а for а, п in получатели.items()
                  if len(п) >= ПОРОГ_ЧАЕВЫХ_ПЛАТЕЛЬЩИКОВ}
    return {"known": set(известные), "by_frequency": по_частоте,
            "all": set(известные) | по_частоте,
            "payers_per_address": {а: len(п) for а, п in получатели.items()}}


def блок(rpc_call, слот: int, *, детали: str = "full") -> dict:
    """Один getBlock -- один кредит. Молчание узла НЕ превращается в пустой
    блок: пустой блок и неизвестный блок это разные ответы."""
    if not isinstance(слот, int) or слот <= 0:
        return {"known": False, "why_not": f"слот не задан: {слот!r}"}
    опции = {"encoding": "jsonParsed", "transactionDetails": детали,
             "rewards": False, "maxSupportedTransactionVersion": ПОТОЛОК_ВЕРСИИ_TX}
    try:
        б = rpc_call("getBlock", [слот, опции])
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "slot": слот,
                "why_not": f"getBlock не отдался: {type(exc).__name__}: {str(exc)[:160]}"}
    tx = (б or {}).get("transactions")
    if tx is None:
        return {"known": False, "slot": слот,
                "why_not": "в ответе getBlock нет транзакций"}
    return {"known": True, "slot": слот, "transactions": tx, "total": len(tx),
            "block_time": (б or {}).get("blockTime")}


def очередь_пула(б: dict, *, минт: str, адреса_пула: set,
                  известные_чаевые: tuple = ()) -> dict:
    """Все транзакции блока, которые трогали ЭТОТ пул -- строками с платой.

    Порядок сохраняется: индекс в блоке и есть место в очереди, и именно он
    отвечает на вопрос "кто стоял перед нами".
    """
    if not б.get("known"):
        return {"known": False, "why_not": б.get("why_not"), "rows": []}
    транзакции = б.get("transactions") or []
    чаевые = адреса_чаевых_блока(транзакции, известные=известные_чаевые)
    по_индексу = чаевые_блока_по_индексу(транзакции, чаевые_адреса=чаевые["all"])
    строки = []
    for и, t in enumerate(транзакции):
        ключи = set(_ключи(t))
        if адреса_пула and not (ключи & адреса_пула):
            continue
        мета = t.get("meta") or {}
        торг = торговец(t, минт, адреса_пула=адреса_пула)
        строка = {"index": и, "signature": _подпись(t),
                  "slot": б.get("slot"), "total_in_block": len(транзакции),
                  "failed": мета.get("err") is not None,
                  "side": (сторона(t, минт) or сторона_по_торговцу(торг)),
                  "trader_from": торг.get("trader_from"),
                  "payer": (_ключи(t) or [None])[0],
                  "trader": торг.get("wallet"),
                  "held_before": торг.get("held_before"),
                  "first_buy_of_mint": торг.get("first_buy"),
                  "durable_nonce": долговременный_nonce(t),
                  "bundle_like": похоже_на_бандл(по_индексу, и)}
        строка.update(плата_транзакции(t, чаевые_адреса=чаевые["all"]))
        строка["tip_labels"] = {а: метка_чаевых(а, известные=известные_чаевые,
                                                по_частоте=чаевые["by_frequency"])
                                for а in (строка.get("tips") or {})}
        строки.append(строка)
    return {"known": True, "slot": б.get("slot"), "rows": строки,
            "total_in_block": len(транзакции),
            "tip_addresses_by_frequency": sorted(чаевые["by_frequency"]),
            "n_pool_tx": len(строки)}


# ------------------------------------------------- пул из нашей же покупки

def адреса_пула_из_покупки(tx: dict, *, минт: str, наш_кошелёк: str) -> dict:
    """Адреса пула, выведенные ИЗ НАШЕЙ ЖЕ транзакции, без лишних вызовов.

    Хранилище пула -- счёт этого минта, владелец которого не мы. Его адрес
    (accountKeys[accountIndex]) и его владелец вместе и есть "этот пул": по
    любому из них транзакции пула находятся в блоке.
    """
    из_ = {"ok": False, "vaults": set(), "owners": set(), "why_not": None}
    мета = (tx or {}).get("meta") or {}
    ключи = _ключи(tx or {})
    if not ключи:
        из_["why_not"] = "в транзакции нет списка счетов"
        return из_
    for где in ("preTokenBalances", "postTokenBalances"):
        for б in (мета.get(где) or []):
            if not isinstance(б, dict) or б.get("mint") != минт:
                continue
            владелец = б.get("owner")
            if not владелец or владелец == наш_кошелёк:
                continue
            и = б.get("accountIndex")
            if isinstance(и, int) and 0 <= и < len(ключи):
                из_["vaults"].add(ключи[и])
            из_["owners"].add(владелец)
    if not (из_["vaults"] or из_["owners"]):
        из_["why_not"] = "чужих счетов этого минта в транзакции нет -- пул не выведен"
        return из_
    из_["ok"] = True
    return из_


# ------------------------------------------------------- итог по покупке

def _процентиль(числа: list, доля: float):
    """Процентиль по ближайшему рангу -- без интерполяции.

    Интерполяция придумала бы плату, которой никто не платил. Здесь нужна
    именно та сумма, с которой КТО-ТО сел: по ней и назначается ставка.
    """
    ряд = sorted(х for х in числа if х is not None)
    if not ряд:
        return None
    к = max(0, min(len(ряд) - 1, int(round(доля * (len(ряд) - 1)))))
    return ряд[к]


def свод_плат(строки: list) -> dict:
    платы = [с.get("pay_lamports") for с in строки
             if с.get("pay_lamports") is not None and not с.get("failed")]
    if not платы:
        return {"n": 0, "min": None, "p25": None, "median": None, "p75": None,
                "max": None}
    return {"n": len(платы), "min": min(платы), "p25": _процентиль(платы, 0.25),
            "median": int(statistics.median(платы)), "p75": _процентиль(платы, 0.75),
            "max": max(платы)}


def итог_по_покупке(*, наша_подпись: str, наш_слот: int, слот_источника: int,
                     подпись_источника: str, очередь_наша: dict,
                     очередь_источника: dict) -> dict:
    """а/б/в/г по одной нашей покупке. Числа, а не мнение.

    Если нашей подписи в нашем же блоке нет -- это не ноль обогнавших, это
    "неизвестно": молчание узла не имеет права выглядеть как хороший
    результат.
    """
    из_ = {"our_signature": наша_подпись, "our_slot": наш_слот,
           "source_slot": слот_источника, "source_signature": подпись_источника,
           "slots_behind": (None if not (isinstance(наш_слот, int)
                                          and isinstance(слот_источника, int))
                            else наш_слот - слот_источника),
           "known": False, "why_not": None}
    строки_н = (очередь_наша or {}).get("rows") or []
    наша = next((с for с in строки_н if с.get("signature") == наша_подпись), None)
    if наша is None:
        из_["why_not"] = ((очередь_наша or {}).get("why_not")
                           or "нашей подписи нет среди транзакций пула в нашем блоке")
        return из_
    наша_плата = наша.get("pay_lamports")
    if наша_плата is None:
        из_["why_not"] = "нашу плату не посчитать: в транзакции нет meta.fee"
        return из_
    из_.update(known=True, our_index=наша.get("index"),
               our_total_in_block=наша.get("total_in_block"),
               our_pay_lamports=наша_плата, our_pay_sol=наша.get("pay_sol"),
               our_priority_lamports=наша.get("priority_lamports"),
               our_tips_lamports=наша.get("tips_lamports"),
               our_cu_price_micro_declared=наша.get("cu_price_micro_declared"),
               our_paid_micro_per_cu=наша.get("paid_micro_per_cu_consumed"),
               # ЧЕМ ДОСТАВЛЕНА НАША: получатели чаевых с метками. У покупки
               # Bloom их обычно нет вовсе -- его processor_tip идёт на его же
               # сборщик и места в блоке не покупает.
               our_tips=наша.get("tips"), our_tip_labels=наша.get("tip_labels"),
               our_durable_nonce=наша.get("durable_nonce"),
               our_bundle_like=наша.get("bundle_like"))

    # а) и б) -- ПЕРЕД НАМИ В НАШЕМ БЛОКЕ. Упавшие не считаются: место в
    # очереди они заняли, но обогнать нас в сделке не могли.
    перед = [с for с in строки_н
             if с.get("index") is not None and с["index"] < наша["index"]
             and not с.get("failed") and с.get("pay_lamports") is not None]
    дешевле = [с for с in перед if с["pay_lamports"] < наша_плата]
    дороже = [с for с in перед if с["pay_lamports"] > наша_плата]
    равно = [с for с in перед if с["pay_lamports"] == наша_плата]
    из_.update(ahead_total=len(перед), ahead_cheaper=len(дешевле),
               ahead_dearer=len(дороже), ahead_equal=len(равно),
               ahead_cheaper_signatures=[с.get("signature") for с in дешевле[:10]],
               overtaken_by_speed=bool(дешевле), overtaken_by_pay=bool(дороже))

    # в) и г) -- СЛОТ ИСТОЧНИКА ПОСЛЕ НЕГО. Это и есть место, за которое
    # идёт спор: попасть в S+0 сразу за источником.
    строки_и = (очередь_источника or {}).get("rows") or []
    источник = next((с for с in строки_и
                     if с.get("signature") == подпись_источника), None)
    if источник is None:
        из_["source_in_block_why_not"] = (
            (очередь_источника or {}).get("why_not")
            or "подписи источника нет среди транзакций пула в его блоке")
        return из_
    из_["source_index"] = источник.get("index")
    из_["source_block_total"] = источник.get("total_in_block")
    # ДОЛЯ ИСТОЧНИКА ВНУТРИ ЕГО БЛОКА (вопрос владельца 25.09, пункт 2а).
    # Индекс сам по себе несравним между блоками: в одном 950 транзакций, в
    # другом 1500. Доля -- оценка МОМЕНТА внутри слота: 0.1 значит источник
    # сел в начале слота и у нас оставалось почти всё окно, 0.9 -- что окна
    # уже не было.
    if (isinstance(источник.get("index"), int)
            and источник.get("total_in_block")):
        из_["source_share"] = round(
            источник["index"] / источник["total_in_block"], 4)
    за_источником = [с for с in строки_и
                     if с.get("index") is not None
                     and с["index"] > источник["index"]
                     and not с.get("failed")
                     and с.get("pay_lamports") is not None]
    дешевле_нас = [с for с in за_источником if с["pay_lamports"] < наша_плата]
    из_.update(s0_after_source=len(за_источником),
               s0_after_source_cheaper_than_us=len(дешевле_нас),
               s0_reachable_without_overpay=bool(дешевле_нас),
               s0_pay=свод_плат(за_источником),
               # ВСЕ строки, а не двадцать: именно по ним считается, кто эти
               # севшие дешевле нас (вопрос владельца от 25.09).
               s0_rows=[{к: с.get(к) for к in
                          ("index", "signature", "side", "pay_lamports",
                           "pay_sol", "tips_lamports", "tips", "tip_labels",
                           "cu_price_micro_declared", "paid_micro_per_cu_consumed",
                           "trader", "trader_from", "payer", "held_before",
                           "first_buy_of_mint",
                           "durable_nonce", "bundle_like")}
                        for с in за_источником])
    return из_


def сводка(итоги: list) -> dict:
    """Доли а/б/в по всем покупкам и цена места по фактам, а не по догадке."""
    известные = [и for и in итоги if и.get("known")]
    with_s0 = [и for и in известные if и.get("s0_pay")]
    def доля(отбор):
        return (None if not известные
                else round(len([и for и in известные if отбор(и)]) / len(известные), 4))
    медианы_s0 = [и["s0_pay"]["median"] for и in with_s0
                  if (и.get("s0_pay") or {}).get("median") is not None]
    p25_s0 = [и["s0_pay"]["p25"] for и in with_s0
              if (и.get("s0_pay") or {}).get("p25") is not None]
    p75_s0 = [и["s0_pay"]["p75"] for и in with_s0
              if (и.get("s0_pay") or {}).get("p75") is not None]
    наши = [и["our_pay_lamports"] for и in известные
            if и.get("our_pay_lamports") is not None]
    из_ = {"n_trades": len(итоги), "n_known": len(известные),
           "n_unknown": len(итоги) - len(известные),
           "share_overtaken_by_speed": доля(lambda и: и.get("overtaken_by_speed")),
           "share_overtaken_by_pay": доля(lambda и: и.get("overtaken_by_pay")),
           "share_s0_reachable_without_overpay":
               доля(lambda и: и.get("s0_reachable_without_overpay")),
           "our_pay_lamports_median": (int(statistics.median(наши)) if наши else None),
           "s0_pay_median_of_medians": (int(statistics.median(медианы_s0))
                                         if медианы_s0 else None),
           "s0_pay_p25_median": (int(statistics.median(p25_s0)) if p25_s0 else None),
           "s0_pay_p75_median": (int(statistics.median(p75_s0)) if p75_s0 else None),
           "n_with_s0": len(with_s0)}
    if из_["n_known"]:
        из_["ahead_cheaper_total"] = sum(и.get("ahead_cheaper") or 0 for и in известные)
        из_["ahead_dearer_total"] = sum(и.get("ahead_dearer") or 0 for и in известные)
    return из_


def вердикт(с: dict) -> str:
    """Одной строкой: ПЛАТА / ПУТЬ / ОБА -- строго по долям, без оговорок.

    Правило простое и объявлено заранее: кто стоял перед нами дешевле нас,
    тот обогнал скоростью; кто дороже -- платой. Обе доли большие -- значит
    и то и другое, и тогда числа важнее слова.
    """
    скорость = с.get("share_overtaken_by_speed")
    плата = с.get("share_overtaken_by_pay")
    if скорость is None or плата is None:
        return "НЕИЗВЕСТНО: ни одной покупки с известным местом в блоке"
    if скорость >= 0.5 and плата >= 0.5:
        return (f"ОБА: обогнали скоростью в {скорость:.0%} покупок, "
                f"платой в {плата:.0%}")
    if скорость >= 0.5:
        return (f"ПУТЬ: в {скорость:.0%} покупок перед нами стояли транзакции, "
                f"заплатившие МЕНЬШЕ нас")
    if плата >= 0.5:
        return (f"ПЛАТА: в {плата:.0%} покупок перед нами платили больше нас, "
                f"дешевле нас перед нами почти никого ({скорость:.0%})")
    return (f"НИ ТО НИ ДРУГОЕ ЯВНО: скоростью {скорость:.0%}, платой {плата:.0%} "
            "-- решают З2 и З3")


# --------------------------------------------------- цена места и окупаемость

def окупаемость(*, плата_sol: float, выигрыш_доля: float,
                 размеры_sol: tuple = (0.01, 0.2, 1.0, 3.0, 5.0)) -> dict:
    """С какого объёма место в S+0 окупается.

    выигрыш_доля -- НАСКОЛЬКО ДЕШЕВЛЕ вход в S+0 против нашего S+1, долей
    цены (0.01 = на процент дешевле). Величина замерная: её даёт сравнение
    наших входов с ценой сразу за источником, и подставлять её "на глаз"
    нельзя -- поэтому она аргумент, а не константа внутри.
    """
    строки = []
    for р in размеры_sol:
        выигрыш = float(р) * float(выигрыш_доля)
        строки.append({"size_sol": р, "gain_sol": round(выигрыш, 6),
                       "extra_pay_sol": round(float(плата_sol), 6),
                       "net_sol": round(выигрыш - float(плата_sol), 6),
                       "pays_off": выигрыш > float(плата_sol)})
    порог = (None if выигрыш_доля <= 0
             else round(float(плата_sol) / float(выигрыш_доля), 4))
    return {"rows": строки, "breakeven_size_sol": порог,
            "pay_sol": float(плата_sol), "gain_fraction": float(выигрыш_доля)}


# ------------------------------------------------------------------ прогон

def разобрать_покупку(rpc_call, *, минт: str, наша_подпись: str, наш_слот: int,
                       подпись_источника: str, слот_источника: int,
                       наш_кошелёк: str, tx_нашей: dict | None = None,
                       известные_чаевые: tuple | None = None,
                       кэш_блоков: dict | None = None) -> dict:
    """Одна покупка целиком: пул из нашей транзакции, два блока, итог а-г.

    Цена: 1 getTransaction (если транзакция не передана) + 2 getBlock, и
    второй getBlock не тратится, когда S+0 и наш слот -- один и тот же.
    """
    известные_чаевые = (_tip_список() if известные_чаевые is None
                        else известные_чаевые)
    кэш_блоков = кэш_блоков if кэш_блоков is not None else {}
    из_ = {"mint": минт, "our_signature": наша_подпись, "credits": 0}
    if tx_нашей is None:
        try:
            tx_нашей = rpc_call("getTransaction", [
                наша_подпись, {"encoding": "jsonParsed",
                               "maxSupportedTransactionVersion": ПОТОЛОК_ВЕРСИИ_TX}])
            из_["credits"] += 1
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = (f"нашу транзакцию не прочитать: "
                              f"{type(exc).__name__}: {str(exc)[:160]}")
            return из_
    пул = адреса_пула_из_покупки(tx_нашей or {}, минт=минт, наш_кошелёк=наш_кошелёк)
    if not пул.get("ok"):
        из_["why_not"] = f"пул не выведен: {пул.get('why_not')}"
        return из_
    адреса = set(пул["vaults"]) | set(пул["owners"])
    из_["pool_addresses"] = sorted(адреса)
    if не_задан(наш_слот) or не_задан(слот_источника):
        из_["why_not"] = (f"слоты не заданы (наш {наш_слот!r}, "
                          f"источника {слот_источника!r})")
        return из_

    def очередь(слот):
        if слот not in кэш_блоков:
            б = блок(rpc_call, слот)
            из_["credits"] += 1
            кэш_блоков[слот] = очередь_пула(б, минт=минт, адреса_пула=адреса,
                                             известные_чаевые=известные_чаевые)
        return кэш_блоков[слот]

    оч_наша = очередь(наш_слот)
    оч_ист = оч_наша if наш_слот == слот_источника else очередь(слот_источника)
    итог = итог_по_покупке(наша_подпись=наша_подпись, наш_слот=наш_слот,
                            слот_источника=слот_источника,
                            подпись_источника=подпись_источника,
                            очередь_наша=оч_наша, очередь_источника=оч_ист)
    из_.update(итог)
    из_["our_queue"] = {"n_pool_tx": оч_наша.get("n_pool_tx"),
                        "total_in_block": оч_наша.get("total_in_block")}
    из_["source_queue"] = {"n_pool_tx": оч_ист.get("n_pool_tx"),
                           "total_in_block": оч_ист.get("total_in_block")}
    из_["rows_our_slot"] = [{к: с.get(к) for к in
                             ("index", "signature", "side", "pay_lamports",
                              "pay_sol", "priority_lamports", "tips_lamports",
                              "cu_price_micro_declared",
                              "paid_micro_per_cu_consumed", "failed")}
                            for с in ((оч_наша.get("rows") or [])[:40])]
    return из_


def не_задан(слот) -> bool:
    return not isinstance(слот, int) or слот <= 0


def стоимость_прогона(n_покупок: int, *, транзакция_известна: bool = False) -> dict:
    """Явная цена прогона в кредитах: считать до запуска, а не после."""
    за_покупку = 2 + (0 if транзакция_известна else 1)
    return {"per_trade_credits": за_покупку,
            "total_credits": n_покупок * за_покупку,
            "what": "2 x getBlock (S+0 и наш слот) + 1 x getTransaction нашей покупки"}


def покупки_из_позиций(позиции: dict, *, с_даты_ts: float | None = None,
                        режим_боевой=None) -> list:
    """Наши покупки из позиций: и Bloom, и полоса, одним списком.

    Берётся только то, у чего есть ПОДПИСЬ НАШЕЙ транзакции и слот: без них
    места в блоке не бывает. Подпись полосы лежит в своём поле
    (lane_signature), у Bloom -- в своём (our_tx_signature / signature).
    """
    из_ = []
    for cid, п in (позиции or {}).items():
        # ИМЕНА ПОЛЕЙ -- ТЕ ЖЕ, ЧТО У ДОГОНА МЕСТА В БЛОКЕ в детекторе.
        # У покупки Bloom подпись приходит списком от площадки (signatures),
        # у полосы лежит своим полем: своя отправка через площадку не идёт.
        подписи = п.get("signatures") or []
        подпись = (подписи[0] if подписи else
                    (п.get("lane_signature") or п.get("lane_signature_local")))
        слот = п.get("our_slot") or п.get("own_tx_seen_slot")
        ts = п.get("ts_intent") or п.get("ts_sent")
        if с_даты_ts and ts and float(ts) < float(с_даты_ts):
            continue
        если_нет = []
        # СУХИЕ И СТЕНДОВЫЕ ПОЗИЦИИ В РАЗБОР НЕ ИДУТ: у них нет транзакции в
        # цепи вовсе, и считать их "покупками без места в блоке" значило бы
        # разбавить доли а/б/в тишиной.
        if режим_боевой is not None and not режим_боевой(п.get("mode")):
            если_нет.append(f"режим не боевой: {п.get('mode')!r}")
        if not подпись:
            если_нет.append("нет подписи нашей транзакции")
        if не_задан(слот):
            если_нет.append("нет слота нашей транзакции")
        if not п.get("source_sig"):
            если_нет.append("нет подписи источника")
        if не_задан(п.get("source_slot")):
            если_нет.append("нет слота источника")
        из_.append({"client_order_id": cid, "mint": п.get("mint"),
                    "lane": п.get("lane"), "our_signature": подпись,
                    "our_slot": слот, "source_signature": п.get("source_sig"),
                    "source_slot": п.get("source_slot"),
                    "ts_intent": ts, "skip_why_not": "; ".join(если_нет)})
    из_.sort(key=lambda р: float(р.get("ts_intent") or 0))
    return из_


# ------------------------------- момент источника внутри слота (пункт 2а)

# ДЛИТЕЛЬНОСТЬ СЛОТА. Число сетевое, не наше: цель Solana -- 400 мс на слот.
# Здесь оно нужно ровно для одного -- перевести "долю слота" в миллисекунды,
# и потому вынесено отдельной константой, а не вписано в формулу.
ДЛИТЕЛЬНОСТЬ_СЛОТА_МС = 400.0


def свод_доли_источника(итоги: list) -> dict:
    """Где внутри своего блока сел источник -- и куда сели мы (пункт 2а).

    Вопрос владельца: если S+0 случается только при МАЛОЙ доле источника,
    значит исход задаёт остаток слота, а не плата. Тогда у нашего постоянного
    расхода (увидеть + собрать + довезти) появляется числовая цель: он должен
    укладываться в то, что остаётся от слота.

    Доля = место источника в блоке, делённое на число транзакций в блоке. Это
    ОЦЕНКА момента, а не время: транзакции внутри слота идут не равномерно, и
    доля 0.5 не означает ровно половину слота. Так и написано в примечании.
    """
    строки = []
    for и in итоги:
        if not и.get("known") or и.get("source_share") is None:
            continue
        чаевые = и.get("our_tips") or {}
        метки = и.get("our_tip_labels") or {}
        строки.append({
            "trade": и.get("client_order_id"), "mint": и.get("mint"),
            "lane": bool(и.get("lane")),
            "source_share": и.get("source_share"),
            "source_index": и.get("source_index"),
            "source_block_total": и.get("source_block_total"),
            "slots_behind": и.get("slots_behind"),
            "our_index": и.get("our_index"),
            "our_total_in_block": и.get("our_total_in_block"),
            "our_pay_sol": и.get("our_pay_sol"),
            "accelerator": ([f"{метки.get(а) or а}" for а in чаевые]
                            or ["без чаевых на опознанные счета"]),
        })
    строки.sort(key=lambda р: р["source_share"])
    из_ = {"n": len(строки), "rows": строки,
           "slot_ms": ДЛИТЕЛЬНОСТЬ_СЛОТА_МС,
           "note": ("доля -- это место источника в блоке, делённое на число "
                     "транзакций блока: оценка момента внутри слота, а не "
                     "время. Транзакции внутри слота идут неравномерно")}
    if not строки:
        из_["why_not"] = "ни у одной покупки нет места источника в его блоке"
        return из_
    s0 = [р for р in строки if р.get("slots_behind") == 0]
    поздние = [р for р in строки if (р.get("slots_behind") or 0) >= 1]
    доли_s0 = sorted(р["source_share"] for р in s0)
    доли_позже = sorted(р["source_share"] for р in поздние)
    из_.update(
        n_s0=len(s0), n_later=len(поздние),
        s0_share_max=(доли_s0[-1] if доли_s0 else None),
        s0_share_median=(доли_s0[len(доли_s0) // 2] if доли_s0 else None),
        later_share_min=(доли_позже[0] if доли_позже else None),
        later_share_median=(доли_позже[len(доли_позже) // 2] if доли_позже else None))
    # ГРАНИЦА: наибольшая доля, при которой мы ещё попадали в S+0. Выше неё
    # S+0 у нас не случалось ни разу -- это и есть "остаток слота кончился".
    из_["s0_never_above_share"] = (
        None if not доли_s0 else
        (доли_s0[-1] if not доли_позже or доли_позже[0] > доли_s0[-1]
         else доли_s0[-1]))
    return из_


def нужный_расход(доля: float = 0.7, *, слот_мс: float = ДЛИТЕЛЬНОСТЬ_СЛОТА_МС,
                   запас_мс: float = 0.0) -> dict:
    """Во сколько миллисекунд должен уложиться наш постоянный расход, чтобы
    успеть в S+0 при заданной доле источника.

    Считается прямо: если источник сел на доле p, от слота остаётся (1 - p),
    то есть (1 - p) * длительность слота. Наш расход -- увидеть, собрать,
    довезти -- должен уложиться в этот остаток, да ещё и до отсечки лидера,
    поэтому запас вычитается явно и называется числом, а не подразумевается.
    """
    остаток = max(0.0, (1.0 - float(доля))) * float(слот_мс)
    return {"share": доля, "slot_ms": слот_мс,
            "remaining_ms": round(остаток, 2),
            "budget_ms": round(max(0.0, остаток - float(запас_мс)), 2),
            "reserve_ms": запас_мс,
            "note": ("остаток слота после посадки источника; наш постоянный "
                      "расход (увидеть + собрать + довезти) должен быть меньше "
                      "этого числа, иначе S+0 недостижим")}


def таблица_доли(с: dict, *, наш_расход_мс: float | None = None) -> str:
    """Таблица 2а: доля источника -> наш слот и чем доставлена наша."""
    if not с or not с.get("n"):
        причина = (с or {}).get("why_not") or "нет данных"
        return f"\n## Момент источника внутри слота\n\n{причина}\n"
    строки = ["", "## Момент источника внутри слота (пункт 2а)", "",
              f"Разобрано {с['n']} покупок. S+0: {с.get('n_s0')}, "
              f"позже: {с.get('n_later')}.", "",
              "| доля источника | место источника | наш слот | наше место | "
              "наша плата, SOL | чем доставлена наша |",
              "|---|---|---|---|---|---|"]
    for р in с["rows"]:
        строки.append(
            f"| {р['source_share']} | {р['source_index']}/{р['source_block_total']} | "
            f"S+{р['slots_behind']} | {р['our_index']}/{р['our_total_in_block']} | "
            f"{р['our_pay_sol']} | {', '.join(р['accelerator'])} |")
    строки += ["", "### Что из этого следует", "",
               f"* доля источника у покупок, где мы попали в S+0: медиана "
               f"{с.get('s0_share_median')}, наибольшая {с.get('s0_share_max')}",
               f"* доля источника у покупок, где мы опоздали на слот и больше: "
               f"медиана {с.get('later_share_median')}, наименьшая "
               f"{с.get('later_share_min')}",
               "", f"_{с.get('note')}_"]
    цели = [нужный_расход(д, слот_мс=с.get("slot_ms") or ДЛИТЕЛЬНОСТЬ_СЛОТА_МС)
            for д in (0.5, 0.7, 0.9)]
    строки += ["", "### Во сколько надо уложиться", "",
               "| доля источника | остаток слота, мс | наш замер (отправка -> видно), мс | успеваем |",
               "|---|---|---|---|"]
    for ц in цели:
        успеваем = ("—" if наш_расход_мс is None
                    else ("да" if наш_расход_мс < ц["remaining_ms"] else "нет"))
        строки.append(f"| {ц['share']} | {ц['remaining_ms']} | "
                      f"{'—' if наш_расход_мс is None else наш_расход_мс} | {успеваем} |")
    строки += ["", f"_Длительность слота взята {с.get('slot_ms')} мс (цель сети). "
               f"{цели[0]['note']}_"]
    return "\n".join(строки) + "\n"


# ------------------------------------------- кто садится в S+0 за источником

def свод_соседей(итоги: list, *, наша_плата_по_сделке: bool = True) -> dict:
    """Кто эти транзакции, что садятся в S+0 за источником ДЕШЕВЛЕ нас.

    Три вопроса владельца от 25.09 и ответы на них ровно по цепи:

      а) РЕАКЦИЯ ИЛИ ТОЛПА. Реакция -- кошелёк купил этот токен впервые
         (остаток до сделки ноль): он приехал на событие. Толпа -- остаток
         был, то есть токеном он торгует и без нас. Ни одного лишнего
         вызова: остаток до сделки лежит в той же meta блока.
      б) ЧЕМ ДОСТАВЛЕНЫ. Получатели чаевых по частоте (с меткой только там,
         где источник известен -- имена ускорителей не придумываются),
         долговременный nonce, признак бандла (сосед по индексу платит тому
         же счёту чаевых).
      в) КТО РЕГУЛЯРНО. Кошельки, попавшие в S+0 за источником на нескольких
         НАШИХ сделках: число попаданий, доля от наших сделок и их плата.

    Считается ТОЛЬКО по тем строкам, что дешевле нашей платы на той же
    сделке: вопрос был именно про них.
    """
    строки = []
    for и in итоги:
        if not и.get("known"):
            continue
        наша = и.get("our_pay_lamports")
        for с in (и.get("s0_rows") or []):
            плата = с.get("pay_lamports")
            if плата is None or с.get("failed"):
                continue
            if наша_плата_по_сделке and наша is not None and плата >= наша:
                continue
            строки.append({**с, "trade": и.get("client_order_id"),
                            "mint": и.get("mint"),
                            "source_signature": и.get("source_signature")})
    из_: dict = {"n_rows": len(строки), "n_trades": len(
        {с["trade"] for с in строки if с.get("trade")})}
    if not строки:
        из_["why_not"] = "дешевле нас в S+0 никого не нашлось"
        return из_

    # --- а) реакция или толпа
    реакция = [с for с in строки if с.get("first_buy_of_mint") is True]
    толпа = [с for с in строки if с.get("first_buy_of_mint") is False]
    неясно = [с for с in строки if с.get("first_buy_of_mint") is None]
    из_["who"] = {
        "reaction_first_buy": len(реакция),
        "crowd_held_before": len(толпа),
        "unknown": len(неясно),
        "share_reaction": round(len(реакция) / len(строки), 4),
        "share_crowd": round(len(толпа) / len(строки), 4),
        "note": ("реакция -- остаток минта до сделки ноль (купил впервые); "
                  "толпа -- остаток был; неясно -- подписант минт не двигал "
                  "(сбор платы, маршрут через посредника)")}
    покупки = [с for с in строки if с.get("side") == "покупка"]
    продажи = [с for с in строки if с.get("side") == "продажа"]
    из_["who"].update(buys=len(покупки), sells=len(продажи),
                      side_unknown=len(строки) - len(покупки) - len(продажи))

    # --- б) чем доставлены
    по_получателю: dict = {}
    метки: dict = {}
    без_чаевых = 0
    for с in строки:
        чае = с.get("tips") or {}
        if not чае:
            без_чаевых += 1
            continue
        for адрес, лам in чае.items():
            з = по_получателю.setdefault(адрес, {"n": 0, "lamports": 0})
            з["n"] += 1
            з["lamports"] += int(лам or 0)
            метка = (с.get("tip_labels") or {}).get(адрес)
            if метка:
                метки[адрес] = метка
    доставка = sorted(
        ({"tip_account": а, "label": метки.get(а),
          "n": з["n"], "share": round(з["n"] / len(строки), 4),
          "lamports_total": з["lamports"],
          "lamports_median_per_tx": int(з["lamports"] / з["n"])}
         for а, з in по_получателю.items()),
        key=lambda р: -р["n"])
    из_["delivery"] = {
        "tip_accounts": доставка,
        "no_tip": без_чаевых,
        "share_no_tip": round(без_чаевых / len(строки), 4),
        "durable_nonce": sum(1 for с in строки if с.get("durable_nonce")),
        "bundle_like": sum(1 for с in строки if с.get("bundle_like")),
        "note": ("метка ставится только там, где источник адреса известен; "
                  "неопознанный адрес так и остаётся адресом. Бандл -- признак "
                  "вероятностный: сосед по индексу платит тому же счёту чаевых")}

    # --- в) кто регулярно
    по_кошельку: dict = {}
    сделок_всего = len({и.get("client_order_id") for и in итоги if и.get("known")})
    for с in строки:
        кош = с.get("trader") or с.get("payer")
        if not кош:
            continue
        з = по_кошельку.setdefault(кош, {"hits": 0, "trades": set(), "pays": [],
                                          "first_buys": 0, "nonce": 0,
                                          "bundle": 0, "tips": {}})
        з["hits"] += 1
        if с.get("trade"):
            з["trades"].add(с["trade"])
        if с.get("pay_lamports") is not None:
            з["pays"].append(с["pay_lamports"])
        if с.get("first_buy_of_mint") is True:
            з["first_buys"] += 1
        if с.get("durable_nonce"):
            з["nonce"] += 1
        if с.get("bundle_like"):
            з["bundle"] += 1
        for а in (с.get("tips") or {}):
            з["tips"][а] = з["tips"].get(а, 0) + 1
    регулярные = []
    for кош, з in по_кошельку.items():
        платы = sorted(з["pays"])
        регулярные.append({
            "wallet": кош, "hits": з["hits"], "trades": len(з["trades"]),
            "share_of_our_trades": (round(len(з["trades"]) / сделок_всего, 4)
                                     if сделок_всего else None),
            "pay_median_lamports": (платы[len(платы) // 2] if платы else None),
            "pay_min_lamports": (платы[0] if платы else None),
            "first_buys": з["first_buys"], "durable_nonce": з["nonce"],
            "bundle_like": з["bundle"],
            "tip_accounts": sorted(з["tips"], key=lambda а: -з["tips"][а])[:3]})
    регулярные.sort(key=lambda р: (-р["trades"], -р["hits"]))
    из_["wallets"] = {
        "n_unique": len(регулярные),
        # ПОРОГ ВЛАДЕЛЬЦА: три и более раза за неделю. Наше окно -- ровно те
        # сделки, что разобраны, и оно названо числом, а не словом "неделя".
        "regular_threshold_trades": 3,
        "regular": [р for р in регулярные if р["trades"] >= 3],
        "top": регулярные[:25],
        "our_trades_in_window": сделок_всего}
    return из_


def таблица_соседей(с: dict) -> str:
    """Ответ на "кто эти 313" таблицами. Пусто -- так и сказано словами."""
    if not с or not с.get("n_rows"):
        причина = (с or {}).get("why_not") or "нет данных"
        return f"\n## Кто садится в S+0 дешевле нас\n\n{причина}\n"
    кто = с.get("who") or {}
    дост = с.get("delivery") or {}
    кош = с.get("wallets") or {}
    строки = ["", "## Кто садится в S+0 за источником ДЕШЕВЛЕ нас", "",
              f"Разобрано {с['n_rows']} транзакций на {с['n_trades']} наших сделках.",
              "", "### а) реакция на источник или независимая толпа", "",
              "| что | сколько | доля |", "|---|---|---|",
              f"| реакция (купил минт ВПЕРВЫЕ) | {кто.get('reaction_first_buy')} | "
              f"{кто.get('share_reaction')} |",
              f"| толпа (остаток минта уже был) | {кто.get('crowd_held_before')} | "
              f"{кто.get('share_crowd')} |",
              f"| неясно (подписант минт не двигал) | {кто.get('unknown')} | — |",
              f"| из них покупки / продажи | {кто.get('buys')} / {кто.get('sells')} | — |",
              "", f"_{кто.get('note')}_", "",
              "### б) чем доставлены", "",
              "| счёт чаевых | метка | транзакций | доля | лампортов на транзакцию |",
              "|---|---|---|---|---|"]
    for р in (дост.get("tip_accounts") or [])[:15]:
        строки.append(f"| `{р['tip_account']}` | {р.get('label') or '— (адрес без метки)'} | "
                      f"{р['n']} | {р['share']} | {р['lamports_median_per_tx']} |")
    строки += [f"| без чаевых вовсе | — | {дост.get('no_tip')} | "
               f"{дост.get('share_no_tip')} | 0 |", "",
               f"* долговременный nonce: {дост.get('durable_nonce')} из {с['n_rows']}",
               f"* похоже на бандл (сосед платит тому же счёту): {дост.get('bundle_like')}",
               "", f"_{дост.get('note')}_", "",
               "### в) кто попадает в S+0 регулярно", "",
               f"Уникальных кошельков: {кош.get('n_unique')}. Порог регулярности: "
               f"{кош.get('regular_threshold_trades')} и более НАШИХ сделок из "
               f"{кош.get('our_trades_in_window')}.", "",
               "| кошелёк | наших сделок | попаданий | доля | плата, медиана | "
               "первых покупок | nonce | бандл | счета чаевых |",
               "|---|---|---|---|---|---|---|---|---|"]
    ряд = (кош.get("regular") or самые_частые(кош))
    for р in ряд[:25]:
        строки.append(f"| `{р['wallet']}` | {р['trades']} | {р['hits']} | "
                      f"{р['share_of_our_trades']} | {р['pay_median_lamports']} | "
                      f"{р['first_buys']} | {р['durable_nonce']} | {р['bundle_like']} | "
                      + ", ".join(f"`{а[:8]}`" for а in (р.get('tip_accounts') or []))
                      + " |")
    if not (кош.get("regular") or []):
        строки.append("")
        строки.append("_Регулярных (>= 3 наших сделок) нет: показаны самые частые._")
    return "\n".join(строки) + "\n"


def самые_частые(кош: dict) -> list:
    """Если регулярных нет, показываем самых частых -- но говорим об этом."""
    return кош.get("top") or []

# ------------------------------------------------------------ самопроверка

def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    import struct as _struct  # noqa: PLC0415

    def цена_ix(микро: int) -> dict:
        return {"programId": CF.CB_PROGRAM, "program": "unknown",
                "data": CF.b58encode(b"\x03" + _struct.pack("<Q", микро))}

    def предел_ix(единиц: int) -> dict:
        return {"programId": CF.CB_PROGRAM, "program": "unknown",
                "data": CF.b58encode(b"\x02" + _struct.pack("<I", единиц))}

    def перевод_ix(откуда: str, куда: str, лампорты: int) -> dict:
        return {"program": "system", "programId": "11111111111111111111111111111111",
                "parsed": {"type": "transfer",
                           "info": {"source": откуда, "destination": куда,
                                     "lamports": лампорты}}}

    def бал(индекс: int, владелец: str, минт: str, сырое: int) -> dict:
        return {"accountIndex": индекс, "owner": владелец, "mint": минт,
                "uiTokenAmount": {"amount": str(сырое), "decimals": 6,
                                   "uiAmount": сырое / 1e6}}

    def тх(*, подпись: str, платящий: str, ключи: tuple = (), fee: int = 5000,
           cu: int = 100_000, цена: int | None = None, предел: int | None = None,
           чаевые: tuple = (), pre: tuple = (), post: tuple = (),
           упала: bool = False, подписей: int = 1) -> dict:
        инструкции = []
        if предел is not None:
            инструкции.append(предел_ix(предел))
        if цена is not None:
            инструкции.append(цена_ix(цена))
        for куда, лам in чаевые:
            инструкции.append(перевод_ix(платящий, куда, лам))
        список = [{"pubkey": платящий, "signer": True, "writable": True}]
        список += [{"pubkey": к, "signer": False, "writable": True} for к in ключи]
        return {"transaction": {"signatures": [подпись] * подписей,
                                 "message": {"accountKeys": список,
                                              "instructions": инструкции}},
                "meta": {"err": ({"InstructionError": [0, "X"]} if упала else None),
                          "fee": fee, "computeUnitsConsumed": cu,
                          "preTokenBalances": list(pre),
                          "postTokenBalances": list(post),
                          "innerInstructions": []}}

    МИНТ = "МИНТ"
    ПУЛ = "ПУЛХРАН"
    TIP = "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"  # из TIP_ACCOUNTS

    # --- ПЛАТА. Приоритет -- это fee минус базовый тариф за КАЖДУЮ подпись.
    п = плата_транзакции(
        тх(подпись="A", платящий="ПЛАТ", fee=105_000, cu=50_000, цена=2_000,
            предел=200_000, чаевые=((TIP, 1_000_000),)),
        чаевые_адреса={TIP})
    chk("приоритет = fee минус 5000 за подпись",
        п["priority_lamports"] == 100_000, п)
    chk("чаевые сложены и названы адресом",
        п["tips_lamports"] == 1_000_000 and п["tips"] == {TIP: 1_000_000}, п)
    chk("плата -- приоритет ПЛЮС чаевые, и в SOL тоже",
        п["pay_lamports"] == 1_100_000 and abs(п["pay_sol"] - 0.0011) < 1e-12, п)
    chk("заявленная цена CU прочитана из инструкции, предел тоже",
        п["cu_price_micro_declared"] == 2_000 and п["cu_limit"] == 200_000, п)
    chk("уплаченное на израсходованную единицу считается отдельно",
        п["paid_micro_per_cu_consumed"] == 2_000_000, п)
    п2 = плата_транзакции(тх(подпись="A2", платящий="ПЛАТ", fee=10_000,
                              подписей=2, цена=None), чаевые_адреса={TIP})
    chk("две подписи -- базовый тариф вдвое, приоритет ноль",
        п2["priority_lamports"] == 0 and п2["pay_lamports"] == 0, п2)
    п3 = плата_транзакции(
        тх(подпись="A3", платящий="ПЛАТ", fee=25_000,
            чаевые=(("ЧУЖОЙ_АДРЕС", 500_000),)), чаевые_адреса={TIP})
    chk("перевод НЕ на счёт чаевых чаевыми не считается",
        п3["tips_lamports"] == 0 and п3["pay_lamports"] == 20_000, п3)

    # --- СТОРОНА СДЕЛКИ. Смотрим на подписанта, а не на пул: у пула дельта
    # всегда противоположная, и по ней сторона выходила бы наизнанку.
    покупка = тх(подпись="B", платящий="ТОРГ", fee=5000,
                  pre=(бал(3, "ПУЛВЛАД", МИНТ, 1_000_000),),
                  post=(бал(3, "ПУЛВЛАД", МИНТ, 900_000),
                        бал(7, "ТОРГ", МИНТ, 100_000)))
    chk("сторона покупки -- по росту минта у подписанта",
        сторона(покупка, МИНТ) == "покупка", сторона(покупка, МИНТ))
    продажа = тх(подпись="C", платящий="ТОРГ", fee=5000,
                  pre=(бал(7, "ТОРГ", МИНТ, 100_000),),
                  post=(бал(7, "ТОРГ", МИНТ, 0),
                        бал(3, "ПУЛВЛАД", МИНТ, 1_100_000)))
    chk("сторона продажи -- по падению минта у подписанта",
        сторона(продажа, МИНТ) == "продажа", сторона(продажа, МИНТ))
    без_сделки = тх(подпись="D", платящий="КТО", fee=5000,
                     post=(бал(3, "ПУЛВЛАД", МИНТ, 1_000_000),))
    chk("движение только у пула -- сторона НЕИЗВЕСТНА, а не покупка",
        сторона(без_сделки, МИНТ) is None, сторона(без_сделки, МИНТ))

    # ТОРГОВЕЦ НА СЧЁТЕ СВОЕЙ ПРОГРАММЫ. Подписант минт не двигает, а сделка
    # есть: 25.09 первый прогон отнёс так 267 из 313 транзакций в "неясно".
    через_pda = тх(подпись="P", платящий="ПОДПИСАНТ", fee=5000,
                    pre=(бал(4, "ПУЛВЛАД", МИНТ, 1_000_000),),
                    post=(бал(4, "ПУЛВЛАД", МИНТ, 700_000),
                          бал(9, "PDA_БОТА", МИНТ, 300_000)))
    торг = торговец(через_pda, МИНТ, адреса_пула={"ПУЛВЛАД"})
    chk("торговец найден на счёте программы, а не потерян",
        торг["wallet"] == "PDA_БОТА" and торг["delta"] == 300_000
        and торг["first_buy"] is True
        and "не подписант" in (торг["trader_from"] or ""), торг)
    chk("сторона такой сделки тоже известна",
        сторона_по_торговцу(торг) == "покупка"
        and сторона_по_торговцу({"delta": -5}) == "продажа", "")
    chk("пул торговцем не считается: без него ответ честно пустой",
        торговец({"meta": {"preTokenBalances": [бал(4, "ПУЛВЛАД", МИНТ, 10)],
                            "postTokenBalances": [бал(4, "ПУЛВЛАД", МИНТ, 20)]},
                   "transaction": {"signatures": ["z"], "message": {
                       "accountKeys": [{"pubkey": "ПОДПИСАНТ", "signer": True}],
                       "instructions": []}}},
                  МИНТ, адреса_пула={"ПУЛВЛАД"})["wallet"] is None, "")

    # --- СЧЕТА ЧАЕВЫХ ПО ЧАСТОТЕ. Пять разных плательщиков одному адресу.
    много = [тх(подпись=f"F{и}", платящий=f"П{и}", fee=5000,
                 чаевые=(("ЧАСТЫЙ", 1000), ("РЕДКИЙ", 1000) if и == 0 else ("ЧАСТЫЙ", 1)))
             for и in range(5)]
    ч = адреса_чаевых_блока(много, известные=(TIP,))
    chk("адрес от пяти разных плательщиков опознан как чаевые",
        "ЧАСТЫЙ" in ч["by_frequency"], ч["by_frequency"])
    chk("адрес от одного плательщика -- НЕ чаевые",
        "РЕДКИЙ" not in ч["by_frequency"], ч["by_frequency"])
    chk("известный список чаевых остаётся в списке всегда",
        TIP in ч["all"], sorted(ч["all"]))
    chk("метка неопознанного адреса не выдумывается",
        метка_чаевых("НЕЗНАКОМЫЙ", известные=(TIP,), по_частоте=set()) is None
        and "Helius" in (метка_чаевых(TIP, известные=(TIP,)) or ""), "")

    # --- ОЧЕРЕДЬ ПУЛА. Фильтр по адресам пула, порядок сохранён.
    блок_наш = {"known": True, "slot": 100, "total": 4, "transactions": [
        тх(подпись="ЧУЖОЙ_ПУЛ", платящий="X", ключи=("ДРУГОЙ_ПУЛ",), fee=999_000),
        тх(подпись="ДЕШЕВЛЕ", платящий="Y", ключи=(ПУЛ,), fee=15_000,
            pre=(), post=(бал(7, "Y", МИНТ, 5),)),
        тх(подпись="НАША", платящий="МЫ", ключи=(ПУЛ,), fee=25_000,
            чаевые=((TIP, 1_000_000),), post=(бал(7, "МЫ", МИНТ, 5),)),
        тх(подпись="ПОЗЖЕ", платящий="Z", ключи=(ПУЛ,), fee=99_000),
    ]}
    оч = очередь_пула(блок_наш, минт=МИНТ, адреса_пула={ПУЛ}, известные_чаевые=(TIP,))
    chk("в очередь пула попали только транзакции ЭТОГО пула",
        [с["signature"] for с in оч["rows"]] == ["ДЕШЕВЛЕ", "НАША", "ПОЗЖЕ"], оч["rows"])
    chk("место в блоке -- это индекс в блоке, а не номер в отборе",
        [с["index"] for с in оч["rows"]] == [1, 2, 3], оч["rows"])
    chk("чаевые нашей транзакции опознаны меткой",
        "Helius" in (list(оч["rows"][1]["tip_labels"].values()) or [""])[0],
        оч["rows"][1]["tip_labels"])

    # --- ИТОГ ПО ПОКУПКЕ. Главные числа: кто перед нами и чем обогнал.
    блок_ист = {"known": True, "slot": 99, "total": 5, "transactions": [
        тх(подпись="ИСТОЧНИК", платящий="SRC", ключи=(ПУЛ,), fee=10_000),
        тх(подпись="S0_ДЕШЁВАЯ", платящий="A", ключи=(ПУЛ,), fee=12_000),
        тх(подпись="S0_СРЕДНЯЯ", платящий="B", ключи=(ПУЛ,), fee=20_000),
        тх(подпись="S0_ДОРОГАЯ", платящий="C", ключи=(ПУЛ,), fee=1_200_000),
        тх(подпись="S0_УПАЛА", платящий="D", ключи=(ПУЛ,), fee=5_000_000, упала=True),
    ]}
    оч_ист = очередь_пула(блок_ист, минт=МИНТ, адреса_пула={ПУЛ},
                           известные_чаевые=(TIP,))
    ит = итог_по_покупке(наша_подпись="НАША", наш_слот=100, слот_источника=99,
                          подпись_источника="ИСТОЧНИК", очередь_наша=оч,
                          очередь_источника=оч_ист)
    chk("наша плата = приоритет 20 000 + чаевые 1 000 000",
        ит["our_pay_lamports"] == 1_020_000, ит.get("our_pay_lamports"))
    chk("перед нами в нашем блоке один дешевле нас -- обогнали СКОРОСТЬЮ",
        ит["ahead_cheaper"] == 1 and ит["overtaken_by_speed"] is True
        and ит["ahead_dearer"] == 0, ит)
    chk("в S+0 после источника три севших (упавшая не считается)",
        ит["s0_after_source"] == 3, ит.get("s0_rows"))
    chk("в S+0 были дешевле нас -- значит туда можно было без переплаты",
        ит["s0_after_source_cheaper_than_us"] == 2
        and ит["s0_reachable_without_overpay"] is True, ит)
    chk("плата севших в S+0: минимум, 25-й процентиль, медиана без выдумки",
        ит["s0_pay"] == {"n": 3, "min": 7_000, "p25": 7_000, "median": 15_000,
                         "p75": 1_195_000, "max": 1_195_000}, ит["s0_pay"])
    chk("отставание в слотах посчитано",
        ит["slots_behind"] == 1 and ит["known"] is True, ит.get("slots_behind"))

    # ОБРАТНЫЙ СЛУЧАЙ: перед нами дороже нас -- обогнали платой.
    блок_платой = {"known": True, "slot": 100, "total": 2, "transactions": [
        тх(подпись="ДОРОЖЕ", платящий="Y", ключи=(ПУЛ,), fee=25_000,
            чаевые=((TIP, 5_000_000),)),
        тх(подпись="НАША", платящий="МЫ", ключи=(ПУЛ,), fee=25_000,
            чаевые=((TIP, 1_000_000),)),
    ]}
    ит2 = итог_по_покупке(
        наша_подпись="НАША", наш_слот=100, слот_источника=99,
        подпись_источника="ИСТОЧНИК",
        очередь_наша=очередь_пула(блок_платой, минт=МИНТ, адреса_пула={ПУЛ},
                                   известные_чаевые=(TIP,)),
        очередь_источника=оч_ист)
    chk("перед нами дороже нас -- обогнали ПЛАТОЙ, а не скоростью",
        ит2["overtaken_by_pay"] is True and ит2["overtaken_by_speed"] is False, ит2)

    # МОЛЧАНИЕ НЕ НОЛЬ: нашей подписи в блоке нет -- это "неизвестно".
    нет_нас = итог_по_покупке(наша_подпись="НЕТ_ТАКОЙ", наш_слот=100,
                               слот_источника=99, подпись_источника="ИСТОЧНИК",
                               очередь_наша=оч, очередь_источника=оч_ист)
    chk("нет нашей подписи в блоке -- known ложно и причина словами",
        нет_нас["known"] is False and нет_нас["why_not"]
        and "ahead_cheaper" not in нет_нас, нет_нас)
    нет_ист = итог_по_покупке(наша_подпись="НАША", наш_слот=100,
                               слот_источника=99, подпись_источника="НЕТ_ИСТОЧНИКА",
                               очередь_наша=оч, очередь_источника=оч_ист)
    chk("нет подписи источника -- а/б есть, в/г честно отсутствуют",
        нет_ист["known"] is True and нет_ист["ahead_cheaper"] == 1
        and "s0_pay" not in нет_ист and нет_ист["source_in_block_why_not"],
        нет_ист)

    # --- ПУЛ ИЗ НАШЕЙ ЖЕ ПОКУПКИ, без лишних вызовов.
    наша_tx = тх(подпись="НАША", платящий="МЫ", ключи=("ХРАН_ПУЛА",), fee=25_000,
                  post=(бал(0, "МЫ", МИНТ, 5), бал(1, "ПУЛВЛАД", МИНТ, 900_000)))
    пул = адреса_пула_из_покупки(наша_tx, минт=МИНТ, наш_кошелёк="МЫ")
    chk("хранилище пула и его владелец выведены из нашей транзакции",
        пул["ok"] and "ХРАН_ПУЛА" in пул["vaults"] and "ПУЛВЛАД" in пул["owners"],
        пул)
    только_мы = тх(подпись="Х", платящий="МЫ", fee=5000,
                    post=(бал(0, "МЫ", МИНТ, 5),))
    chk("своих счетов мало -- пул НЕ выведен, и это сказано словами",
        адреса_пула_из_покупки(только_мы, минт=МИНТ, наш_кошелёк="МЫ")["ok"] is False,
        адреса_пула_из_покупки(только_мы, минт=МИНТ, наш_кошелёк="МЫ"))

    # --- СВОДКА И ВЕРДИКТ.
    с = сводка([ит, ит2, нет_нас])
    chk("сводка считает доли только по известным покупкам",
        с["n_trades"] == 3 and с["n_known"] == 2 and с["n_unknown"] == 1
        and с["share_overtaken_by_speed"] == 0.5
        and с["share_overtaken_by_pay"] == 0.5, с)
    chk("вердикт на равных долях называет ОБА с числами",
        вердикт(с).startswith("ОБА"), вердикт(с))
    chk("вердикт ПУТЬ -- когда обогнали скоростью",
        вердикт({"share_overtaken_by_speed": 0.9,
                  "share_overtaken_by_pay": 0.1}).startswith("ПУТЬ"), "")
    chk("вердикт ПЛАТА -- когда перед нами платили больше",
        вердикт({"share_overtaken_by_speed": 0.1,
                  "share_overtaken_by_pay": 0.8}).startswith("ПЛАТА"), "")
    chk("без известных покупок вердикт -- НЕИЗВЕСТНО, а не вывод",
        вердикт({"share_overtaken_by_speed": None,
                  "share_overtaken_by_pay": None}).startswith("НЕИЗВЕСТНО"), "")

    # --- ОКУПАЕМОСТЬ МЕСТА. Порог -- плата, делённая на выигрыш доли.
    ок = окупаемость(плата_sol=0.002, выигрыш_доля=0.01,
                      размеры_sol=(0.01, 0.2, 1.0, 3.0))
    chk("на 0.2 SOL место не окупается, на 1 SOL -- окупается",
        ок["rows"][1]["pays_off"] is False and ок["rows"][2]["pays_off"] is True,
        ок["rows"])
    chk("порог окупаемости посчитан, а не назван на глаз",
        ок["breakeven_size_sol"] == 0.2, ок["breakeven_size_sol"])
    chk("нулевой выигрыш -- порога нет вовсе, а не ноль",
        окупаемость(плата_sol=0.002, выигрыш_доля=0.0)["breakeven_size_sol"] is None,
        "")

    # --- МОМЕНТ ИСТОЧНИКА ВНУТРИ СЛОТА (пункт 2а владельца 25.09).
    доли = свод_доли_источника([
        {"known": True, "client_order_id": "d1", "mint": "M1",
         "source_share": 0.1, "source_index": 100, "source_block_total": 1000,
         "slots_behind": 0, "our_index": 400, "our_total_in_block": 1000,
         "our_pay_sol": 0.000995, "our_tips": {}},
        {"known": True, "client_order_id": "d2", "mint": "M2",
         "source_share": 0.8, "source_index": 800, "source_block_total": 1000,
         "slots_behind": 1, "our_index": 90, "our_total_in_block": 1100,
         "our_pay_sol": 0.002, "our_tips": {TIP: 1_000_000},
         "our_tip_labels": {TIP: "tip Helius Sender / Jito (список репозитория)"}},
        {"known": True, "client_order_id": "d3", "mint": "M3",
         "source_share": 0.9, "source_index": 900, "source_block_total": 1000,
         "slots_behind": 2, "our_index": 50, "our_total_in_block": 1000,
         "our_pay_sol": 0.000995, "our_tips": {}},
        {"known": False, "client_order_id": "d4"},
    ])
    chk("доли считаются только там, где место источника известно",
        доли["n"] == 3 and доли["n_s0"] == 1 and доли["n_later"] == 2, доли)
    chk("строки идут по возрастанию доли -- так видно границу S+0",
        [р["source_share"] for р in доли["rows"]] == [0.1, 0.8, 0.9], доли["rows"])
    chk("наибольшая доля с попаданием в S+0 и наименьшая с опозданием названы",
        доли["s0_share_max"] == 0.1 and доли["later_share_min"] == 0.8, доли)
    chk("ускоритель нашей покупки назван, а пустые чаевые -- не выдуманы",
        доли["rows"][0]["accelerator"] == ["без чаевых на опознанные счета"]
        and "Helius" in доли["rows"][1]["accelerator"][0], доли["rows"][:2])
    ну = нужный_расход(0.7, слот_мс=400.0)
    chk("остаток слота при доле 0.7 -- 120 мс, и это считано, а не названо",
        ну["remaining_ms"] == 120.0 and ну["budget_ms"] == 120.0, ну)
    chk("запас вычитается явно",
        нужный_расход(0.7, слот_мс=400.0, запас_мс=20.0)["budget_ms"] == 100.0, "")
    chk("доля 1.0 не даёт отрицательного остатка",
        нужный_расход(1.2)["remaining_ms"] == 0.0, "")
    тд = таблица_доли(доли, наш_расход_мс=700.3)
    chk("таблица 2а печатает долю, слот и вывод про расход",
        all(к in тд for к in ("доля источника", "S+0", "остаток слота",
                              "наш замер")) and "нет |" in тд, тд[:200])
    chk("без замера расхода в таблице прочерк, а не подставленное число",
        "| — | — |" in таблица_доли(доли) or "—" in таблица_доли(доли),
        таблица_доли(доли)[-300:])

    # --- КТО САДИТСЯ В S+0 ДЕШЕВЛЕ НАС (вопрос владельца 25.09).
    def строка_s0(*, подпись, плата, первая=None, nonce=False, бандл=False,
                   кошелёк="W", чае=None, сторона_="покупка", упала=False):
        return {"signature": подпись, "pay_lamports": плата, "failed": упала,
                "first_buy_of_mint": первая, "durable_nonce": nonce,
                "bundle_like": бандл, "trader": кошелёк, "payer": кошелёк,
                "side": сторона_, "tips": dict(чае or {}),
                "tip_labels": {а: ("tip Helius Sender / Jito (список репозитория)"
                                    if а == TIP else None) for а in (чае or {})}}

    сделки_с = [
        {"known": True, "client_order_id": "t1", "mint": "M1",
         "our_pay_lamports": 1_000_000, "source_signature": "S1", "s0_rows": [
             строка_s0(подпись="a1", плата=1_000, первая=True, кошелёк="БЫСТРЫЙ",
                        чае={TIP: 1_000}),
             строка_s0(подпись="a2", плата=2_000, первая=False, кошелёк="ТОЛПА1"),
             строка_s0(подпись="a3", плата=5_000_000, первая=True, кошелёк="ДОРОГОЙ"),
             строка_s0(подпись="a4", плата=100, упала=True, кошелёк="УПАЛА"),
         ]},
        {"known": True, "client_order_id": "t2", "mint": "M2",
         "our_pay_lamports": 1_000_000, "source_signature": "S2", "s0_rows": [
             строка_s0(подпись="b1", плата=1_500, первая=True, кошелёк="БЫСТРЫЙ",
                        nonce=True, бандл=True, чае={TIP: 2_000}),
             строка_s0(подпись="b2", плата=900, первая=None, кошелёк="НЕЯСНО",
                        сторона_=None),
         ]},
        {"known": True, "client_order_id": "t3", "mint": "M3",
         "our_pay_lamports": 1_000_000, "source_signature": "S3", "s0_rows": [
             строка_s0(подпись="c1", плата=1_200, первая=True, кошелёк="БЫСТРЫЙ",
                        чае={TIP: 1_500}),
         ]},
        {"known": False, "client_order_id": "t4", "why_not": "нет места"},
    ]
    сс = свод_соседей(сделки_с)
    chk("в разбор идут только те, кто ДЕШЕВЛЕ нас, и только севшие",
        сс["n_rows"] == 5 and сс["n_trades"] == 3, сс)
    chk("реакция и толпа разделены по остатку минта ДО сделки",
        сс["who"]["reaction_first_buy"] == 3
        and сс["who"]["crowd_held_before"] == 1
        and сс["who"]["unknown"] == 1
        and сс["who"]["share_reaction"] == 0.6, сс["who"])
    chk("получатели чаевых сведены по частоте и с меткой там, где она известна",
        сс["delivery"]["tip_accounts"][0]["tip_account"] == TIP
        and сс["delivery"]["tip_accounts"][0]["n"] == 3
        and "Helius" in (сс["delivery"]["tip_accounts"][0]["label"] or ""),
        сс["delivery"]["tip_accounts"])
    chk("без чаевых считается отдельно, а не приписывается кому-то",
        сс["delivery"]["no_tip"] == 2, сс["delivery"])
    chk("долговременный nonce и признак бандла считаются",
        сс["delivery"]["durable_nonce"] == 1
        and сс["delivery"]["bundle_like"] == 1, сс["delivery"])
    chk("регулярный кошелёк найден по числу НАШИХ сделок, а не попаданий",
        [р["wallet"] for р in сс["wallets"]["regular"]] == ["БЫСТРЫЙ"]
        and сс["wallets"]["regular"][0]["trades"] == 3
        and сс["wallets"]["regular"][0]["share_of_our_trades"] == 1.0,
        сс["wallets"]["regular"])
    chk("у регулярного кошелька видна его плата и способ доставки",
        сс["wallets"]["regular"][0]["pay_median_lamports"] == 1_200
        and сс["wallets"]["regular"][0]["durable_nonce"] == 1
        and сс["wallets"]["regular"][0]["tip_accounts"] == [TIP],
        сс["wallets"]["regular"][0])
    chk("таблица соседей печатается и называет обе части ответа",
        all(к in таблица_соседей(сс) for к in
            ("реакция", "толпа", "счёт чаевых", "долговременный nonce",
             "регулярно")), таблица_соседей(сс)[:200])
    chk("пустой свод говорит словами, а не пустой таблицей",
        "нет данных" in таблица_соседей({}) or "никого" in таблица_соседей(
            свод_соседей([])), таблица_соседей(свод_соседей([])))

    # --- ЦЕНА ПРОГОНА В КРЕДИТАХ.
    chk("цена прогона названа до запуска: 3 кредита на покупку",
        стоимость_прогона(10)["total_credits"] == 30
        and стоимость_прогона(10, транзакция_известна=True)["total_credits"] == 20,
        стоимость_прогона(10))

    # --- ПОКУПКИ ИЗ ПОЗИЦИЙ: причина пропуска названа, а не молчание.
    поз = {"c1": {"mint": "M1", "lane": "own_send", "lane_signature": "L1",
                   "own_tx_seen_slot": 10, "source_sig": "S1", "source_slot": 9,
                   "ts_intent": 100.0},
            "c2": {"mint": "M2", "signatures": ["B1"], "our_slot": 20,
                   "source_sig": "S2", "source_slot": 19, "ts_intent": 200.0},
            "c3": {"mint": "M3", "source_sig": "S3", "source_slot": 30,
                   "ts_intent": 300.0}}
    список = покупки_из_позиций(поз)
    chk("сухая позиция в разбор не идёт и причина названа",
        "режим не боевой" in покупки_из_позиций(
            {"d1": {"mint": "M", "signatures": ["X"], "our_slot": 1,
                     "source_sig": "S", "source_slot": 1, "mode": "dry"}},
            режим_боевой=lambda м: м == "live")[0]["skip_why_not"], "")
    chk("в список попадают и полоса, и Bloom, по времени",
        [р["client_order_id"] for р in список] == ["c1", "c2", "c3"], список)
    chk("у покупки без подписи и слота названа причина, а не тишина",
        "нет подписи" in список[2]["skip_why_not"]
        and not список[0]["skip_why_not"], список[2])
    chk("отбор по дате отсекает старое",
        [р["client_order_id"] for р in покупки_из_позиций(поз, с_даты_ts=150.0)]
        == ["c2", "c3"], "")

    # --- ТОЛЬКО ЧТЕНИЕ. В модуле не должно быть ни одной отправки.
    исходник = Path(__file__).read_text(encoding="utf-8")
    # Имя метода собирается из кусков нарочно: написанное целиком, оно
    # само попало бы в исходник и проверка всегда была бы ложной.
    отправка = "send" + "Transaction"
    chk("в модуле нет отправки транзакций вовсе",
        отправка not in исходник, "")
    chk("молчание узла даёт known=False, а не пустой блок",
        блок(lambda м, п: (_ for _ in ()).throw(TimeoutError("узел молчит")),
              100)["known"] is False, "")
    chk("блок без транзакций -- тоже не тишина, а known=False с причиной",
        блок(lambda м, п: {}, 100)["why_not"], блок(lambda м, п: {}, 100))

    плохих = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'нет '}] {имя}"
              + ("" if ок else f" -- {факт}"))
    print(f"самопроверка цены места: {len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--state-dir", default="")
    p.add_argument("--since", default="", help="ISO-дата: покупки не раньше неё")
    p.add_argument("--max-trades", type=int, default=40)
    p.add_argument("--credit-limit", type=int, default=400)
    p.add_argument("--our-constant-ms", type=float, default=0.0,
                   help="замеренный постоянный расход полосы (увидеть + собрать "
                        "+ довезти), мс -- для таблицы 'во сколько надо уложиться'")
    p.add_argument("--gain-fraction", type=float, default=0.0,
                   help="замеренный выигрыш входа в S+0 долей цены (для таблицы "
                        "окупаемости). Ноль -- таблица не строится: выдумывать "
                        "выигрыш нельзя")
    p.add_argument("--out", default="data/queue_price.json")
    p.add_argument("--out-md", default="docs/queue_price.md")
    a = p.parse_args()
    if a.self_test:
        return self_test()

    import bloom_exec_state as ST  # noqa: PLC0415
    from night_leader_stonkfun import BudgetedRpc  # noqa: PLC0415
    import c2_common as C2m  # noqa: PLC0415

    состояние = (ST.ExecState(base=Path(a.state_dir)) if a.state_dir
                 else ST.ExecState())
    с_даты = None
    if a.since:
        с_даты = time.mktime(time.strptime(a.since, "%Y-%m-%d")) if len(a.since) == 10 \
            else float(a.since)
    покупки = покупки_из_позиций(состояние.positions(), с_даты_ts=с_даты,
                                  режим_боевой=ST.is_real_mode)
    причины: dict = {}
    for р in покупки:
        if р["skip_why_not"]:
            причины[р["skip_why_not"]] = причины.get(р["skip_why_not"], 0) + 1
    for почему, сколько in sorted(причины.items(), key=lambda п: -п[1]):
        print(f"  пропущено {сколько}: {почему}")
    годные = [р for р in покупки if not р["skip_why_not"]][:a.max_trades]
    цена = стоимость_прогона(len(годные))
    print(f"покупок всего {len(покупки)}, годных для разбора {len(годные)}, "
          f"цена прогона {цена['total_credits']} кредитов")
    rpc = BudgetedRpc(C2m.C2Rpc(service="c2_queue_price"), a.credit_limit)
    итоги = []
    кэш: dict = {}
    for р in годные:
        try:
            и = разобрать_покупку(
                rpc, минт=р["mint"], наша_подпись=р["our_signature"],
                наш_слот=р["our_slot"], подпись_источника=р["source_signature"],
                слот_источника=р["source_slot"],
                наш_кошелёк=(состояние.positions().get(р["client_order_id"], {})
                              .get("lane") and _кошелёк_полосы() or ST.EXECUTOR_WALLET),
                кэш_блоков=кэш)
        except Exception as exc:  # noqa: BLE001
            и = {"our_signature": р["our_signature"],
                 "why_not": f"{type(exc).__name__}: {str(exc)[:200]}"}
        и["client_order_id"] = р["client_order_id"]
        и["lane"] = р.get("lane")
        итоги.append(и)
        print(f"  {р['client_order_id']}: "
              + (и.get("why_not") or
                 f"место {и.get('our_index')}/{и.get('our_total_in_block')}, "
                 f"перед нами дешевле {и.get('ahead_cheaper')}, дороже "
                 f"{и.get('ahead_dearer')}, плата {и.get('our_pay_sol')} SOL"))
    с = сводка(итоги)
    соседи = свод_соседей(итоги)
    итог = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trades": итоги, "summary": с, "verdict_z1": вердикт(с),
            "s0_riders": соседи,
            "source_share": свод_доли_источника(итоги),
            "our_constant_ms": (a.our_constant_ms if a.our_constant_ms > 0 else None),
            "credits_used": getattr(rpc, "used", None),
            "cost_plan": цена}
    if a.gain_fraction > 0 and с.get("s0_pay_median_of_medians"):
        итог["payoff"] = окупаемость(
            плата_sol=(с["s0_pay_median_of_medians"] / ЛАМПОРТОВ_В_SOL),
            выигрыш_доля=a.gain_fraction)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(итог, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    Path(a.out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out_md).write_text(в_таблицу(итог), encoding="utf-8")
    print(итог["verdict_z1"])
    print(f"записано: {a.out} и {a.out_md}")
    return 0


def _кошелёк_полосы() -> str:
    try:
        import bloom_own_send as OS  # noqa: PLC0415
        return OS.кошелёк_полосы()
    except Exception:  # noqa: BLE001
        return ""


def в_таблицу(итог: dict) -> str:
    """Отчёт таблицей: одна строка на покупку, потом сводка и вердикт."""
    строки = ["# З1: кто стоял перед нами (цена места)", "",
              f"Сформировано {итог.get('generated_utc')}. "
              f"Кредитов потрачено: {итог.get('credits_used')}.", "",
              "| сделка | минт | место/всего | S-S0 | наша плата, SOL | "
              "перед нами дешевле | дороже | в S+0 дешевле нас | "
              "медиана платы S+0, SOL |",
              "|---|---|---|---|---|---|---|---|---|"]
    for т in (итог.get("trades") or []):
        if not т.get("known"):
            строки.append(f"| {т.get('client_order_id')} | {т.get('mint')} | "
                          f"— | — | — | — | — | — | {т.get('why_not')} |")
            continue
        медиана = (т.get("s0_pay") or {}).get("median")
        строки.append(
            f"| {т.get('client_order_id')} | {т.get('mint')} | "
            f"{т.get('our_index')}/{т.get('our_total_in_block')} | "
            f"{т.get('slots_behind')} | {т.get('our_pay_sol')} | "
            f"{т.get('ahead_cheaper')} | {т.get('ahead_dearer')} | "
            f"{т.get('s0_after_source_cheaper_than_us')} | "
            f"{'—' if медиана is None else round(медиана / ЛАМПОРТОВ_В_SOL, 6)} |")
    с = итог.get("summary") or {}
    строки += ["", "## Сводка", "",
               f"* покупок разобрано: {с.get('n_known')} из {с.get('n_trades')} "
               f"(без места в блоке: {с.get('n_unknown')})",
               f"* обогнали СКОРОСТЬЮ (перед нами платили меньше): "
               f"{с.get('share_overtaken_by_speed')}",
               f"* обогнали ПЛАТОЙ (перед нами платили больше): "
               f"{с.get('share_overtaken_by_pay')}",
               f"* в S+0 можно было попасть без переплаты: "
               f"{с.get('share_s0_reachable_without_overpay')}",
               f"* наша плата, медиана: {с.get('our_pay_lamports_median')} лампортов",
               f"* плата севших в S+0 за источником: медиана медиан "
               f"{с.get('s0_pay_median_of_medians')}, 25-й процентиль "
               f"{с.get('s0_pay_p25_median')}, 75-й {с.get('s0_pay_p75_median')}",
               "", f"**{итог.get('verdict_z1')}**"]
    п = итог.get("payoff")
    if п:
        строки += ["", "## Цена места против выигрыша по цене", "",
                   f"Выигрыш входа в S+0 взят замером: {п.get('gain_fraction')} "
                   f"долей цены. Доплата за место: {п.get('pay_sol')} SOL.", "",
                   "| объём сделки, SOL | выигрыш, SOL | доплата, SOL | итог, SOL | окупается |",
                   "|---|---|---|---|---|"]
        for р in п.get("rows") or []:
            строки.append(f"| {р['size_sol']} | {р['gain_sol']} | "
                          f"{р['extra_pay_sol']} | {р['net_sol']} | "
                          f"{'да' if р['pays_off'] else 'нет'} |")
        строки += ["", f"Окупается начиная с {п.get('breakeven_size_sol')} SOL на сделку."]
    # НАШ ПОСТОЯННЫЙ РАСХОД -- замерный: медиана "от отправки до появления в
    # подписке" по полосе, если она в данных есть. Не замерили -- прочерк, а
    # не подставленное число.
    расход = итог.get("our_constant_ms")
    хвост = (таблица_доли(итог.get("source_share") or {}, наш_расход_мс=расход)
             + таблица_соседей(итог.get("s0_riders") or {}))
    return "\n".join(строки) + "\n" + хвост


if __name__ == "__main__":
    raise SystemExit(main())
