#!/usr/bin/env python3
"""Правило 11 ТОЛЬКО ПО ЦЕПИ: сколько ушло, сколько вернулось, итог.

СЛОВО ВЛАДЕЛЬЦА 28.09: "Таблица Правила 11 -- только по цепи, без
несчитаемых: у закрытой позиции обе транзакции на цепи, итог = SOL ушло минус
SOL вернулось (комиссии, чаевые, рента включены)... Статус несчитаемый из учёта
убрать; вместо него -- продажа не найдена на цепи с тревогой в TG".

ЧТО СЧИТАЕТСЯ ДЕНЬГАМИ. Изменение НАТИВНОГО баланса НАШИХ счетов в самой
транзакции: кошелёк плюс его токеновые счета (их владелец виден в
meta.pre/postTokenBalances). Ничего не очищается: комиссия, чаевые, приоритет и
рента счетов входят в число, потому что это и есть потраченные деньги. Обёртка
SOL внутри нашего же кошелька не искажает итог -- лампорты уходят на НАШ счёт
WSOL, а он в списке наших.

ЗНАК. В таблице три числа: "ушло" (положительное), "вернулось" (положительное) и
"итог" = вернулось минус ушло, то есть ПРИБЫЛЬ со знаком плюс. Формулировку
владельца "ушло минус вернулось" привожу отдельной графой "расход_минус_возврат"
-- это то же число с обратным знаком, и путать их нельзя.

ЧЕГО ЗДЕСЬ НЕТ. Полей службы (pnl_counted_sol и прочих) в счёте нет вовсе:
таблица нужна как независимая проверка учёта, а не как его пересказ. Ни подписи,
ни отправки: только getTransaction и getSignatureStatuses.
"""
from __future__ import annotations

import argparse
import calendar
import glob
import gzip
import json
import os
import sys
import time
from pathlib import Path

ЛАМПОРТОВ_В_SOL = 1_000_000_000
ПАРТИЯ_СТАТУСОВ = 256
HELIUS = "https://mainnet.helius-rpc.com"


def урл() -> str:
    к = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    return f"{HELIUS}/?api-key={к}"


def зов(метод: str, параметры, *, таймаут: float = 60.0) -> dict:
    import urllib.request  # noqa: PLC0415

    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    try:
        зпр = urllib.request.Request(урл(), data=тело,
                                      headers={"content-type": "application/json"})
        with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
            д = json.loads(отв.read())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
    if "error" in д:
        return {"ok": False, "why_not": str(д["error"])[:200]}
    return {"ok": True, "result": д.get("result"), "why_not": None}


# --------------------------------------------------------- деньги по цепи

def наши_индексы(tx: dict, кошелёк: str) -> dict:
    """Индексы НАШИХ счетов: кошелёк и его токеновые счета."""
    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    сырые = сообщение.get("accountKeys") or []
    ключи = [к.get("pubkey") if isinstance(к, dict) else к for к in сырые]
    мета = (tx or {}).get("meta") or {}
    наши = set()
    почему = None
    if кошелёк in ключи:
        наши.add(ключи.index(кошелёк))
    else:
        почему = "кошелька нет среди статических ключей"
    for где in ("preTokenBalances", "postTokenBalances"):
        for з in (мета.get(где) or []):
            if з.get("owner") == кошелёк and isinstance(з.get("accountIndex"), int):
                наши.add(int(з["accountIndex"]))
    return {"индексы": sorted(наши), "ключи": ключи, "why_not": почему}


def дельта_наших(tx: dict, кошелёк: str) -> dict:
    """Изменение нативного баланса наших счетов в лампортах, без очистки."""
    из_ = {"ok": False, "lamports": None, "fee": None, "счетов": 0,
            "why_not": None}
    мета = (tx or {}).get("meta") or {}
    до = мета.get("preBalances") or []
    после = мета.get("postBalances") or []
    if not до or not после:
        из_["why_not"] = "в транзакции нет балансов до и после"
        return из_
    н = наши_индексы(tx, кошелёк)
    if not н["индексы"]:
        из_["why_not"] = н["why_not"] or "наших счетов в транзакции нет"
        return из_
    сумма = 0
    for и in н["индексы"]:
        if и < len(до) and и < len(после):
            сумма += int(после[и]) - int(до[и])
    из_.update(ok=True, lamports=сумма, fee=int(мета.get("fee") or 0),
                счетов=len(н["индексы"]),
                slot=(tx or {}).get("slot"),
                blockTime=(tx or {}).get("blockTime"),
                err=(мета.get("err") is not None))
    return из_


# --------------------------------------------------------- позиции и подписи

def строки(путь: str):
    открыть = gzip.open if путь.endswith(".gz") else open
    try:
        ф = открыть(путь, "rt", encoding="utf-8")
    except Exception:  # noqa: BLE001
        return
    with ф:
        for строка in ф:
            строка = строка.strip()
            if not строка:
                continue
            try:
                yield json.loads(строка)
            except ValueError:
                continue


def позиции_полосы(state_dir: str, с_ts: float | None,
                    по_ts: float | None = None) -> dict:
    по_cid: dict = {}
    for путь in sorted(glob.glob(str(Path(state_dir) / "positions.jsonl*"))):
        for з in строки(путь):
            cid = з.get("client_order_id")
            if not cid:
                continue
            в = по_cid.setdefault(cid, {})
            for к, зн in з.items():
                if зн is not None:
                    в[к] = зн
    из_: dict = {}
    for cid, п in по_cid.items():
        if not п.get("lane"):
            continue
        т = п.get("ts_sent") or п.get("ts_intent")
        if not isinstance(т, (int, float)):
            continue
        if с_ts and float(т) < float(с_ts):
            continue
        if по_ts and float(т) > float(по_ts):
            continue
        из_[cid] = п
    return из_


def подписи_покупки(п: dict) -> list:
    ряд = [п.get("lane_landed_signature"), п.get("lane_signature"),
            п.get("signature_accepted")]
    for поле in ("lane_signatures", "lane_pool_candidates", "lane_variants"):
        зн = п.get(поле)
        if isinstance(зн, dict):
            ряд += list(зн.values())
        elif isinstance(зн, list):
            ряд += list(зн)
    из_, видели = [], set()
    for с in ряд:
        if isinstance(с, str) and с and с not in видели:
            видели.add(с)
            из_.append(с)
    return из_


def подписи_продажи(п: dict) -> list:
    ряд = [п.get("last_sell_reported"), п.get("jup_signature")]
    зн = п.get("last_sell_signatures")
    if isinstance(зн, list):
        ряд += list(зн)
    из_, видели = [], set()
    for с in ряд:
        if isinstance(с, str) and с and с not in видели:
            видели.add(с)
            из_.append(с)
    return из_


def севшие(подписи: list) -> dict:
    """Какие из подписей есть на цепи: одним getSignatureStatuses на партию."""
    из_: dict = {}
    ряд = [с for с in подписи if с]
    for н in range(0, len(ряд), ПАРТИЯ_СТАТУСОВ):
        часть = ряд[н:н + ПАРТИЯ_СТАТУСОВ]
        о = зов("getSignatureStatuses",
                 [часть, {"searchTransactionHistory": True}])
        if not о["ok"]:
            continue
        for с, зн in zip(часть, ((о["result"] or {}).get("value") or [])):
            if isinstance(зн, dict):
                из_[с] = {"slot": зн.get("slot"), "err": зн.get("err")}
    return из_


def транзакция(подпись: str) -> dict | None:
    о = зов("getTransaction", [подпись, {"encoding": "jsonParsed",
                                          "maxSupportedTransactionVersion": 1,
                                          "commitment": "confirmed"}])
    return о.get("result") if о.get("ok") else None


СЛУЖЕБНЫЕ_ПРОГРАММЫ = {
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
}


def программы_сделки(tx: dict | None) -> list:
    """Программы пула в транзакции: чем именно покупали.

    Поле pool_program в записи бывает пустым (у части сделок его не пишут),
    а вопрос "каким строителем" отвечается по самой транзакции: берём все
    программы инструкций, кроме системных и токеновых.
    """
    сооб = ((tx or {}).get("transaction") or {}).get("message") or {}
    все = list(сооб.get("instructions") or [])
    for г in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        все += list(г.get("instructions") or [])
    из_ = []
    for и in все:
        пид = и.get("programId") if isinstance(и, dict) else None
        if not пид or пид in СЛУЖЕБНЫЕ_ПРОГРАММЫ or пид in из_:
            continue
        из_.append(пид)
    return из_


def имена_инструкций(tx: dict | None) -> list:
    """Имена инструкций из журнала программ: чем именно покупали.

    Anchor печатает строку "Program log: Instruction: <Имя>". Берём их по
    порядку и без повторов подряд -- это ответ на вопрос, каким сборщиком
    ушла покупка.
    """
    из_ = []
    for стр in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        текст = str(стр)
        если = "Program log: Instruction: "
        if текст.startswith(если):
            имя = текст[len(если):][:60]
            if not из_ or из_[-1] != имя:
                из_.append(имя)
    return из_


def хвост_журнала(tx: dict | None, сколько: int = 10) -> list:
    """Последние строки журнала программ -- слова самой цепи, без правок."""
    журнал = ((tx or {}).get("meta") or {}).get("logMessages") or []
    return [str(с)[:200] for с in журнал[-сколько:]]


def причина_ошибки(tx: dict | None) -> str | None:
    """Почему покупка ничего не купила -- словами самой цепи.

    Берём err как есть и последнюю строку журнала программ, где сказано про
    ошибку. Ничего не додумываем: если цепь молчит, возвращаем None.
    """
    мета = (tx or {}).get("meta") or {}
    куски = []
    err = мета.get("err")
    if err is not None:
        куски.append(json.dumps(err, ensure_ascii=False)[:120]
                      if not isinstance(err, str) else err[:120])
    журнал = мета.get("logMessages") or []
    строка = None
    for стр in журнал:
        низ = str(стр).lower()
        if ("error" in низ or "failed" in низ or "panicked" in низ
                or "slippage" in низ or "exceeded" in низ):
            строка = str(стр)
    if строка is None and журнал:
        строка = str(журнал[-1])
    if строка:
        куски.append(строка[:160])
    return " | ".join(куски) or None


def счёт_минта(кошелёк: str, минт: str) -> str | None:
    """ATA нашего кошелька под этот минт -- через тот же модуль, что и сборка."""
    try:
        import c2_swap_build as SB  # noqa: PLC0415

        return SB.ata(кошелёк, минт, SB.TOKEN_PROGRAM)
    except Exception:  # noqa: BLE001
        return None


def продажа_по_цепи(кошелёк: str, минт: str, *, предел: int = 20) -> dict:
    """Найти продажу по цепи, когда подписи в записи нет или она не села.

    ПОЧЕМУ ПО ИСТОРИИ ТОКЕН-СЧЁТА, А НЕ КОШЕЛЬКА. У кошелька сотни подписей в
    сутки, и перебирать их -- сотни вызовов на позицию. У ATA под конкретный
    минт их единицы: покупка, продажа, закрытие. Ищем транзакцию, в которой
    остаток НАШЕГО минта уменьшился -- она и есть продажа.
    """
    из_ = {"ok": False, "подпись": None, "why_not": None, "осмотрено": 0}
    ата = счёт_минта(кошелёк, минт)
    if not ата:
        из_["why_not"] = "ATA минта не вывелся (нет модуля сборки)"
        return из_
    из_["ata"] = ата
    о = зов("getSignaturesForAddress", [ата, {"limit": int(предел)}])
    if not о["ok"]:
        из_["why_not"] = f"история счёта не прочиталась: {о['why_not']}"
        return из_
    for зап in (о["result"] or []):
        подпись = (зап or {}).get("signature")
        if not подпись:
            continue
        из_["осмотрено"] += 1
        tx = транзакция(подпись)
        if not tx:
            continue
        мета = (tx or {}).get("meta") or {}

        def остаток(где):
            for з in (мета.get(где) or []):
                if з.get("owner") == кошелёк and з.get("mint") == минт:
                    try:
                        return int((з.get("uiTokenAmount") or {}).get("amount"))
                    except (TypeError, ValueError):
                        return None
            return None

        до, после = остаток("preTokenBalances"), остаток("postTokenBalances")
        if до is None:
            continue
        if после is None or int(после) < int(до):
            из_.update(ok=True, подпись=подпись, было=до, стало=после)
            return из_
    из_["why_not"] = (f"в {из_['осмотрено']} подписях счёта минта уменьшения "
                       "остатка не нашлось")
    return из_


def кошелёк_позиции(п: dict, по_умолчанию: str = "") -> str:
    for поле in ("wallet", "lane_wallet", "own_send_wallet"):
        зн = п.get(поле)
        if isinstance(зн, str) and зн:
            return зн
    return по_умолчанию


# --------------------------------------------------------- таблица

def сделка_по_цепи(п: dict, *, кошелёк: str) -> dict:
    """Одна позиция: обе транзакции по цепи и деньги по ним."""
    из_ = {"cid": п.get("client_order_id"), "минт": п.get("mint"),
            "источник": п.get("source"), "имя_источника": п.get("source_name"),
            "группа": п.get("lane_group"), "кошелёк": кошелёк,
            "utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(float(п.get("ts_sent")
                                                      or п.get("ts_intent") or 0)))),
            # СТРОИТЕЛЬ -- ПОЛЕ program: именно его пишет отправка полосы и по
            # нему же служба считает "село на программе" (правило первой сделки
            # 0.01 на новом пуле). Прежде здесь стояло pool_program, которого в
            # записи нет вовсе, и столбец был пустым у всех сделок.
            "строитель": п.get("program") or п.get("pool_program"),
            "цепь_ок": п.get("chain_ok"),
            "подпись_покупки": None, "подпись_продажи": None,
            "ушло_sol": None, "вернулось_sol": None, "итог_sol": None,
            "удержание_слотов": None, "статус": None, "тревога": False}
    п_пок, п_прод = подписи_покупки(п), подписи_продажи(п)
    села = севшие(п_пок + п_прод)
    пок = next((с for с in п_пок if с in села), None)
    прод = next((с for с in п_прод if с in села), None)
    из_["подпись_покупки"], из_["подпись_продажи"] = пок, прод
    из_["подписей_покупки_в_записи"] = len(п_пок)
    if not пок:
        # РАЗНЫЕ ВЕЩИ. Если подписи покупки в записи нет вовсе -- покупка не
        # отправлялась (сторож отказал, гонка по минту), денег не двигалось и
        # тревожить владельца нечем. Если подпись есть, но её нет на цепи --
        # это настоящая дыра в учёте.
        из_.update(статус=("покупки не было: подписи в записи нет" if not п_пок
                            else "покупка не села: подпись в записи есть, "
                                 "на цепи её нет"),
                    тревога=bool(п_пок))
        return из_
    tx_пок = транзакция(пок)
    д_пок = дельта_наших(tx_пок or {}, кошелёк)
    if not д_пок["ok"]:
        из_.update(статус=f"покупка не считается: {д_пок['why_not']}", тревога=True)
        return из_
    из_["ушло_sol"] = round(-д_пок["lamports"] / ЛАМПОРТОВ_В_SOL, 9)
    из_["слот_покупки"] = д_пок.get("slot")
    из_["покупка_с_ошибкой"] = bool(д_пок.get("err"))
    из_["инструкции_покупки"] = имена_инструкций(tx_пок)
    из_["программы_покупки"] = программы_сделки(tx_пок)
    из_["комиссия_покупки_sol"] = round((д_пок.get("fee") or 0) / ЛАМПОРТОВ_В_SOL, 9)
    # ПОКУПКА, КОТОРАЯ НИЧЕГО НЕ КУПИЛА. Если с кошелька ушла ровно комиссия
    # (или транзакция села с ошибкой), токенов у нас нет, и продажи не будет
    # никогда: это не "продажа не найдена", а сгоревшая комиссия. Называем
    # прямо -- иначе такая сделка годами висит в тревогах.
    только_комиссия = (abs(-д_пок["lamports"] - (д_пок.get("fee") or 0)) <= 1)
    if д_пок.get("err") or только_комиссия:
        из_.update(статус=("покупка села с ошибкой -- заплатили только комиссию"
                            if д_пок.get("err") else
                            "покупка не купила: ушла ровно комиссия"),
                    итог_sol=round(д_пок["lamports"] / ЛАМПОРТОВ_В_SOL, 9),
                    расход_минус_возврат_sol=round(
                        -д_пок["lamports"] / ЛАМПОРТОВ_В_SOL, 9),
                    вернулось_sol=0.0, тревога=True,
                    причина_цепи=причина_ошибки(tx_пок),
                    журнал_хвост=хвост_журнала(tx_пок))
        return из_
    if not прод:
        # ПОИСК ПРОДАЖИ ПО ЦЕПИ. Подписи в записи может не быть вовсе (выход
        # сделал не наш сторож) или она могла не сесть. Таблица обязана быть
        # ТОЛЬКО ПО ЦЕПИ, поэтому ищем продажу по истории токен-счёта минта.
        найдена = продажа_по_цепи(кошелёк, п.get("mint") or "")
        из_["подписей_продажи_в_записи"] = len(п_прод)
        из_["поиск_продажи"] = {к: зн for к, зн in найдена.items()
                                 if к in ("ok", "why_not", "осмотрено", "ata")}
        if найдена.get("ok"):
            прод = найдена["подпись"]
            из_["подпись_продажи"] = прод
            из_["продажа_найдена_поиском"] = True
        else:
            из_.update(статус=("продажа не найдена на цепи"
                                + (" (в записи подписи продажи нет)"
                                    if not п_прод else
                                    " (подпись в записи есть, но не села)")),
                        тревога=True)
            return из_
    tx_прод = транзакция(прод)
    д_прод = дельта_наших(tx_прод or {}, кошелёк)
    if not д_прод["ok"]:
        из_.update(статус=f"продажа не считается: {д_прод['why_not']}",
                    тревога=True)
        return из_
    из_["вернулось_sol"] = round(д_прод["lamports"] / ЛАМПОРТОВ_В_SOL, 9)
    из_["слот_продажи"] = д_прод.get("slot")
    из_["комиссия_продажи_sol"] = round((д_прод.get("fee") or 0) / ЛАМПОРТОВ_В_SOL, 9)
    итог = (д_пок["lamports"] + д_прод["lamports"]) / ЛАМПОРТОВ_В_SOL
    из_["итог_sol"] = round(итог, 9)
    из_["расход_минус_возврат_sol"] = round(-итог, 9)
    if isinstance(д_пок.get("slot"), int) and isinstance(д_прод.get("slot"), int):
        из_["удержание_слотов"] = int(д_прод["slot"]) - int(д_пок["slot"])
    из_["статус"] = "по цепи"
    # СВЕРКА СО СЧЁТОМ СЛУЖБЫ (п.9 владельца): её же формула против цепи, и
    # разложение разницы по известным кускам. Необъяснённое остаётся числом, а
    # не словом: по нему видно, чего поля не видят вовсе (например, рента).
    из_["счёт_службы"] = счёт_службы(п)
    из_["разложение"] = разложить_разницу(из_, п)
    return из_


# ------------------------------- СВЕРКА С СОБСТВЕННЫМ СЧЁТОМ СЛУЖБЫ (п.9)
#
# Владелец 28.09: "разложение «учёт службы 1.086 против цепи 0.914» до лампорта
# по позициям -- таблицей". Счёт службы НЕ переписываем: зовём ЕЁ ЖЕ формулу
# (bloom_exec_state.итог_позиции), иначе сравнивались бы две наши арифметики, а
# не служба против цепи. Модуль службы есть только на хосте, поэтому импорт
# мягкий: без него строка сверки просто не заполняется.
def счёт_службы(поз: dict) -> dict:
    try:
        import bloom_exec_state as ST  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"модуля службы нет: {type(exc).__name__}"}
    try:
        итог, расход = ST.итог_позиции(поз)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"формула службы упала: {type(exc).__name__}"}
    return {"ok": True, "итог_sol": (None if итог is None else round(float(итог), 9)),
             "расход_sol": round(float(расход or 0.0), 9)}


def разложить_разницу(строка: dict, поз: dict) -> dict:
    """Разница «цепь минус служба» РОВНО по формуле службы, без домыслов.

    Служба, когда у неё есть нативная дельта покупки, считает так:
        итог = closed_sol_net + lane_buy_native_sol - lane_buy_fee_sol
    Цепь считает:
        итог = дельта_наших(покупка) + дельта_наших(продажа)
    Значит разница раскладывается ТОЧНО на три слагаемых:
        (дельта покупки - lane_buy_native_sol)
      + (дельта продажи - closed_sol_net)
      + lane_buy_fee_sol
    Остаток обязан быть нулём до лампорта; если он не ноль -- значит служба
    считала другой ветвью формулы, и это честно видно в поле "ветвь".
    """
    цепь = строка.get("итог_sol")
    сл = (строка.get("счёт_службы") or {}).get("итог_sol")
    из_ = {"разница_sol": None, "ветвь": None,
            "расхождение_покупки_sol": None, "расхождение_продажи_sol": None,
            "комиссия_покупки_в_формуле_sol": None, "остаток_sol": None,
            "возврат_службы_sol": None, "натив_покупки_службы_sol": None}
    if not isinstance(цепь, (int, float)) or not isinstance(сл, (int, float)):
        return из_
    из_["разница_sol"] = round(float(цепь) - float(сл), 9)
    натив = поз.get("lane_buy_native_sol")
    возврат = поз.get("closed_sol_net")
    if возврат is None:
        возврат = ((поз.get("last_sell_outcome") or {}).get("sol_delta_net"))
    ком_пок = поз.get("lane_buy_fee_sol")
    из_["возврат_службы_sol"] = (None if возврат is None
                                  else round(float(возврат), 9))
    из_["натив_покупки_службы_sol"] = (None if натив is None
                                        else round(float(натив), 9))
    if натив is None or возврат is None:
        из_["ветвь"] = "служба считала по полям (нативной дельты покупки нет)"
        return из_
    из_["ветвь"] = "натив покупки плюс возврат"
    д_пок = строка.get("ушло_sol")
    д_прод = строка.get("вернулось_sol")
    if not isinstance(д_пок, (int, float)) or not isinstance(д_прод, (int, float)):
        return из_
    рп = round(-float(д_пок) - float(натив), 9)
    рпр = round(float(д_прод) - float(возврат), 9)
    кф = round(float(ком_пок or 0.0), 9)
    из_.update(расхождение_покупки_sol=рп, расхождение_продажи_sol=рпр,
                комиссия_покупки_в_формуле_sol=кф,
                остаток_sol=round(из_["разница_sol"] - (рп + рпр + кф), 9))
    return из_


def свод(ряды: list) -> dict:
    считанные = [р for р in ряды if р.get("итог_sol") is not None]
    по_источникам: dict = {}
    for р in считанные:
        ключ = р.get("источник") or р.get("имя_источника") or "источника нет"
        с_ = по_источникам.setdefault(ключ, {
            "адрес": р.get("источник"), "имя": р.get("имя_источника"),
            "n": 0, "сумма_sol": 0.0, "лучшая_sol": None, "в_плюсе": 0,
            "группы": {}})
        с_["n"] += 1
        с_["сумма_sol"] = round(с_["сумма_sol"] + float(р["итог_sol"]), 9)
        if с_["лучшая_sol"] is None or float(р["итог_sol"]) > с_["лучшая_sol"]:
            с_["лучшая_sol"] = round(float(р["итог_sol"]), 9)
        if float(р["итог_sol"]) > 0:
            с_["в_плюсе"] += 1
        гр = р.get("группа") or "?"
        с_["группы"][гр] = int(с_["группы"].get(гр, 0)) + 1
    for _, с_ in по_источникам.items():
        с_["доля_в_плюсе"] = (round(с_["в_плюсе"] / с_["n"], 4) if с_["n"] else None)
    тревоги = [р for р in ряды if р.get("тревога")]
    # СВЕРКА СО СЛУЖБОЙ (п.9): суммы по её формуле, по цепи и разница с
    # разложением. Считаются только строки, где известны ОБА числа -- иначе
    # разница была бы суммой разных множеств позиций.
    пара = [р for р in ряды
             if isinstance(р.get("итог_sol"), (int, float))
             and isinstance((р.get("счёт_службы") or {}).get("итог_sol"),
                             (int, float))]
    сверка = {
        "позиций": len(пара),
        "сумма_службы_sol": round(sum(float(р["счёт_службы"]["итог_sol"])
                                       for р in пара), 9),
        "сумма_цепи_sol": round(sum(float(р["итог_sol"]) for р in пара), 9),
    }
    сверка["разница_sol"] = round(сверка["сумма_цепи_sol"]
                                   - сверка["сумма_службы_sol"], 9)
    for поле in ("расхождение_покупки_sol", "расхождение_продажи_sol",
                  "комиссия_покупки_в_формуле_sol", "остаток_sol"):
        сверка[поле] = round(sum(float((р.get("разложение") or {}).get(поле) or 0.0)
                                  for р in пара), 9)
    return {"сделок": len(ряды), "посчитано_по_цепи": len(считанные),
             "сумма_sol": round(sum(float(р["итог_sol"]) for р in считанные), 9),
             "тревог": len(тревоги), "сверка_со_службой": сверка,
             "по_источникам": dict(sorted(по_источникам.items(),
                                           key=lambda т: -т[1]["сумма_sol"])),
             "тревоги": [{"cid": р["cid"], "минт": р.get("минт"),
                           "статус": р.get("статус"),
                           "подпись_покупки": р.get("подпись_покупки"),
                           "подпись_продажи": р.get("подпись_продажи")}
                          for р in тревоги]}


def разобрать_время(текст: str):
    if not текст:
        return None
    try:
        return float(calendar.timegm(time.strptime(текст, "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--s", dest="s", default="2026-09-28T00:21:00Z")
    р.add_argument("--po", dest="po", default="")
    р.add_argument("--istochnik", default="",
                    help="только этот источник (адрес или имя)")
    р.add_argument("--out", default="")
    а = р.parse_args()
    с_ts, по_ts = разобрать_время(а.s), разобрать_время(а.po)
    поз = позиции_полосы(а.state_dir, с_ts, по_ts)
    по_умолчанию = (os.environ.get("OWN_SEND_WALLET")
                     or os.environ.get("BLOOM_LANE_WALLET") or "")
    ряды = []
    for cid, п in sorted(поз.items(),
                          key=lambda т: float(т[1].get("ts_sent")
                                               or т[1].get("ts_intent") or 0)):
        if а.istochnik and а.istochnik not in (п.get("source"),
                                                п.get("source_name")):
            continue
        ряды.append(сделка_по_цепи(п, кошелёк=кошелёк_позиции(п, по_умолчанию)))
    итог = {"с": а.s, "по": а.po or None, "источник": а.istochnik or None,
             "свод": свод(ряды), "сделки": ряды}
    текст = json.dumps(итог, ensure_ascii=False, indent=1)
    if а.out:
        Path(а.out).write_text(текст, encoding="utf-8")
    краткое = {"с": итог["с"], "свод": итог["свод"]}
    print(json.dumps(краткое, ensure_ascii=False, indent=1)[:6000])
    return 0


def self_test() -> int:
    """Арифметика денег и выбор подписей. Сети здесь нет."""
    пройдено = провалено = 0

    def chk(что, ок, факт=""):
        nonlocal пройдено, провалено
        if ок:
            пройдено += 1
            print(f"  [ok  ] {что}")
        else:
            провалено += 1
            print(f"  [ПРОВАЛ] {что}: {факт}")

    НАШ = "КОШЕЛЁК"
    # Покупка: с кошелька ушло 0.3 SOL, ещё 0.002 осело на созданном НАШЕМ
    # токен-счёте (рента), комиссия 0.001005 внутри дельты кошелька.
    tx_пок = {"slot": 1000, "blockTime": 100,
               "transaction": {"message": {"accountKeys": [
                   {"pubkey": НАШ}, {"pubkey": "ТОКЕНСЧЁТ"}, {"pubkey": "ПУЛ"}]}},
               "meta": {"fee": 1_005_000, "err": None,
                         "preBalances": [1_000_000_000, 0, 5_000_000_000],
                         "postBalances": [698_995_000, 2_000_000, 5_300_000_000],
                         "preTokenBalances": [],
                         "postTokenBalances": [
                             {"accountIndex": 1, "owner": НАШ, "mint": "МИНТ"}]}}
    д = дельта_наших(tx_пок, НАШ)
    chk("покупка: дельта наших счетов -- кошелёк ПЛЮС наш токен-счёт",
        д["ok"] and д["lamports"] == (698_995_000 - 1_000_000_000) + 2_000_000
        and д["счетов"] == 2, д)
    # Продажа: вернулось 0.34 на кошелёк, токен-счёт закрыт (рента вернулась).
    tx_прод = {"slot": 1108, "blockTime": 130,
                "transaction": {"message": {"accountKeys": [
                    {"pubkey": НАШ}, {"pubkey": "ТОКЕНСЧЁТ"}, {"pubkey": "ПУЛ"}]}},
                "meta": {"fee": 1_005_000, "err": None,
                          "preBalances": [698_995_000, 2_000_000, 5_300_000_000],
                          "postBalances": [1_040_990_000, 0, 4_960_000_000],
                          "preTokenBalances": [
                              {"accountIndex": 1, "owner": НАШ, "mint": "МИНТ"}],
                          "postTokenBalances": []}}
    д2 = дельта_наших(tx_прод, НАШ)
    chk("продажа: вернувшаяся рента закрытого счёта тоже в дельте",
        д2["ok"] and д2["lamports"] == (1_040_990_000 - 698_995_000) - 2_000_000,
        д2)
    chk("комиссия НЕ вычищается: она и есть потраченные деньги",
        д["fee"] == 1_005_000 and д2["fee"] == 1_005_000)
    chk("чужого счёта в наших нет",
        наши_индексы(tx_пок, НАШ)["индексы"] == [0, 1],
        наши_индексы(tx_пок, НАШ)["индексы"])
    chk("кошелька нет в транзакции -- отказ словами",
        дельта_наших(tx_пок, "ЧУЖОЙ")["ok"] is False)

    # Подписи: берутся все варианты, без повторов и пустых.
    п = {"lane_landed_signature": "П1", "lane_signature": "П1",
          "lane_pool_candidates": {"a": "П2", "b": None},
          "last_sell_signatures": ["С1", "С2"], "last_sell_reported": "С2"}
    chk("подписи покупки -- без повторов и пустых",
        подписи_покупки(п) == ["П1", "П2"], подписи_покупки(п))
    chk("подписи продажи -- доложенная первой, потом остальные",
        подписи_продажи(п) == ["С2", "С1"], подписи_продажи(п))

    # Свод: сумма, лучшая, доля в плюсе, тревоги.
    ряды = [{"cid": "1", "итог_sol": 0.05, "источник": "И1", "группа": "g"},
             {"cid": "2", "итог_sol": -0.02, "источник": "И1", "группа": "g"},
             {"cid": "3", "итог_sol": None, "источник": "И2", "группа": "g",
               "статус": "продажа не найдена на цепи", "тревога": True}]
    с = свод(ряды)
    chk("свод: посчитано 2 из 3, сумма 0.03, тревог 1",
        с["посчитано_по_цепи"] == 2 and abs(с["сумма_sol"] - 0.03) < 1e-9
        and с["тревог"] == 1, с)
    chk("по источнику: лучшая 0.05, доля в плюсе 0.5",
        с["по_источникам"]["И1"]["лучшая_sol"] == 0.05
        and с["по_источникам"]["И1"]["доля_в_плюсе"] == 0.5,
        с["по_источникам"]["И1"])
    chk("источник без счёта в таблицу сумм не попадает",
        "И2" not in с["по_источникам"], с["по_источникам"].keys())
    # ПОКУПКА, КОТОРАЯ НИЧЕГО НЕ КУПИЛА: ушла ровно комиссия. Это не "продажа
    # не найдена", а сгоревшая комиссия, и в сумму она обязана входить.
    tx_пусто = {"slot": 5, "transaction": {"message": {"accountKeys": [
                    {"pubkey": НАШ}]}},
                 "meta": {"fee": 1_005_000, "err": None,
                           "preBalances": [1_000_000_000],
                           "postBalances": [998_995_000],
                           "preTokenBalances": [], "postTokenBalances": []}}
    д3 = дельта_наших(tx_пусто, НАШ)
    chk("ушла ровно комиссия -- дельта равна комиссии со знаком минус",
        д3["lamports"] == -1_005_000 and д3["fee"] == 1_005_000, д3)
    # ПРИЧИНА СЛОВАМИ ЦЕПИ: err как есть плюс последняя строка про ошибку.
    # РАЗЛОЖЕНИЕ РАЗНИЦЫ: числа подобраны так, что тождество проверяется в уме.
    # Цепь: ушло 0.302005, вернулось 0.310000. Служба: возврат 0.309, натив
    # покупки -0.301, комиссия покупки 0.001005.
    стр_р = {"итог_sol": round(0.310000 - 0.302005, 9),
              "ушло_sol": 0.302005, "вернулось_sol": 0.310000,
              "счёт_службы": {"ok": True,
                               "итог_sol": round(0.309 - 0.301 - 0.001005, 9)}}
    р_ = разложить_разницу(стр_р, {"lane_buy_native_sol": -0.301,
                                    "closed_sol_net": 0.309,
                                    "lane_buy_fee_sol": 0.001005})
    chk("разложение разницы сходится до лампорта",
        abs(р_["расхождение_покупки_sol"] + 0.001005) < 1e-9
        and abs(р_["расхождение_продажи_sol"] - 0.001) < 1e-9
        and abs(р_["комиссия_покупки_в_формуле_sol"] - 0.001005) < 1e-9
        and abs(р_["остаток_sol"]) < 1e-9, р_)
    chk("без числа службы разложение молчит, а не выдумывает ноль",
        разложить_разницу({"итог_sol": -0.01, "счёт_службы": {}},
                           {})["разница_sol"] is None)
    chk("ветвь названа, когда служба считала по полям",
        разложить_разницу({"итог_sol": -0.01,
                            "счёт_службы": {"итог_sol": -0.009}},
                           {})["ветвь"].startswith("служба считала по полям"))
    chk("программы сделки -- без системных и токеновых",
        программы_сделки({"transaction": {"message": {"instructions": [
            {"programId": "ComputeBudget111111111111111111111111111111"},
            {"programId": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"},
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}]}}})
        == ["pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"])
    chk("имена инструкций читаются из журнала без повторов подряд",
        имена_инструкций({"meta": {"logMessages": [
            "Program log: Instruction: BuyExactQuoteInV2",
            "Program log: Instruction: BuyExactQuoteInV2",
            "Program log: Instruction: CloseAccount"]}})
        == ["BuyExactQuoteInV2", "CloseAccount"])
    chk("хвост журнала -- последние строки как есть",
        хвост_журнала({"meta": {"logMessages": ["a", "b", "c"]}}, 2) == ["b", "c"])
    пусто = сделка_по_цепи({"cid": "x", "mint": "M"}, кошелёк=НАШ)
    chk("покупки не было: подписи в записи нет -- не тревога",
        пусто["статус"] == "покупки не было: подписи в записи нет"
        and пусто["тревога"] is False, пусто)
    chk("причина берётся из err и журнала программ",
        причина_ошибки({"meta": {"err": {"InstructionError": [3, {"Custom": 6002}]},
                                  "logMessages": ["Program log: start",
                                                  "Program log: Error: slippage"]}})
        == '{"InstructionError": [3, {"Custom": 6002}]} | Program log: Error: slippage',
        причина_ошибки({"meta": {"err": {"InstructionError": [3, {"Custom": 6002}]},
                                  "logMessages": ["Program log: Error: slippage"]}}))
    chk("без ошибки и без журнала причины нет",
        причина_ошибки({"meta": {"err": None, "logMessages": []}}) is None)
    chk("окно читается как UTC",
        разобрать_время("2026-09-28T00:21:00Z") == 1790554860.0)
    print(f"самопроверка Правила 11 по цепи: {пройдено}/{пройдено + провалено} пройдено")
    return 1 if провалено else 0


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else main())
