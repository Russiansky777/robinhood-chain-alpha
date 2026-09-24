#!/usr/bin/env python3
"""Полоса СВОЕЙ отправки: сборка, подпись, отправка через Helius Sender.

Слово владельца 24.09: на первой настоящей паре BxdfWdGUUt1L Bloom ответил
за 65.9 мс, а наша транзакция появилась в потоке через 410 мс ПОСЛЕ его
ответа, и в том же блоке DBot стоял 191-м, а мы 975-м. Узкое место -- путь
Bloom до блока. Полоса проверяет, помогает ли своя отправка, и делает это
маленьким размером рядом с обычной покупкой, не трогая её.

ЧЕГО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ. Он не решает, покупать ли: решение принимает
детектор. Он не задерживает Bloom: вызывается в своём потоке. Он не
отправляет ничего, пока BLOOM_OWN_SEND_LIVE не равен 1, и не подписывает
ничем, кроме ключа, публичный ключ которого СОВПАЛ с кошельком исполнителя
-- проверка стоит перед КАЖДОЙ подписью, а не один раз при старте.

Источники чисел (не по памяти, страницы забраны прогоном и лежат в
data/docs/): адреса Helius Sender по регионам, тариф Sender Max с
минимальным типом 0.001 SOL и список из десяти tip-аккаунтов взяты со
страниц https://www.helius.dev/docs/sending-transactions/sender и
.../sender-max, снятых 2026-09-24T19:17Z.
"""
from __future__ import annotations

import base64
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_exec_state as ST  # noqa: E402

# ------------------------------------------------------------------ источник

# Адреса Sender по регионам. Для службы на хосте документация
# рекомендует HTTP-адрес ближайшего региона; наш хост в Нидерландах.
SENDER_HOSTS = {
    "ams": "http://ams-sender.helius-rpc.com/fast",
    "fra": "http://fra-sender.helius-rpc.com/fast",
    "lon": "http://lon-sender.helius-rpc.com/fast",
    "ewr": "http://ewr-sender.helius-rpc.com/fast",
    "slc": "http://slc-sender.helius-rpc.com/fast",
    "sg": "http://sg-sender.helius-rpc.com/fast",
    "tyo": "http://tyo-sender.helius-rpc.com/fast",
}

# Десять tip-аккаунтов Sender Max, дословно со страницы документации.
TIP_ACCOUNTS = (
    "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
    "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
    "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta",
    "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
    "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
    "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
    "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF",
    "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
    "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey",
    "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or",
)

# Минимум типа для приоритетного буфера Sender Max -- 0.001 SOL. Меньше
# принимается, но идёт по остаточному пути, а полоса меряет именно быстрый.
TIP_MIN_SOL = 0.001

ЛАМПОРТОВ_В_SOL = 1_000_000_000

# ЖЁСТКИЙ ПОТОЛОК РАЗМЕРА. Полоса -- замер, а не торговля, и размер у неё
# крошечный по слову владельца. Потолок стоит в коде, а не только в env:
# опечатка в переменной окружения не должна превращать замер в покупку на
# весь баланс. Выше потолка -- отказ, а не обрезка: молча купить не столько,
# сколько просили, хуже, чем не купить вовсе.
ПОТОЛОК_РАЗМЕРА_SOL = 0.05


# ------------------------------------------------------------------ настройки

def включена() -> bool:
    """Полоса вообще собрана? Сборка и симуляция идут и без этого флага."""
    return ST.env_int("BLOOM_OWN_SEND", 0) == 1


def живьём() -> bool:
    """ОТПРАВКА в сеть. Без этого не уходит ни одна транзакция полосы."""
    return ST.env_int("BLOOM_OWN_SEND_LIVE", 0) == 1


def адрес_сендера() -> str:
    регион = (os.environ.get("BLOOM_SENDER_REGION") or "ams").strip().lower()
    return SENDER_HOSTS.get(регион, SENDER_HOSTS["ams"])


def размер_sol() -> float:
    return ST.env_float("BLOOM_OWN_SEND_SOL", 0.01)


def чаевые_sol() -> float:
    """Чаевые Sender. Ниже документированного минимума не опускаемся.

    Меньший тип документация принимает, но он идёт мимо приоритетного
    буфера -- полоса тогда мерила бы не тот путь, ради которого заведена.
    """
    з = ST.env_float("BLOOM_OWN_SEND_TIP_SOL", TIP_MIN_SOL)
    return max(з, TIP_MIN_SOL)


def приоритет_sol() -> float:
    return ST.env_float("BLOOM_OWN_SEND_PRIORITY_SOL", 0.001)


def выбрать_чаевые(семя: str | None = None) -> str:
    """Адрес для чаевых. Разный от сделки к сделке.

    Документация даёт десять адресов не для красоты: один и тот же счёт на
    каждой отправке -- лишняя точка соперничества за запись.
    """
    if not семя:
        return random.choice(TIP_ACCOUNTS)
    return TIP_ACCOUNTS[sum(bytearray(семя.encode("utf-8"))) % len(TIP_ACCOUNTS)]


def цена_единицы_cu(приоритет_лампорты: int, cu_units: int) -> int:
    """Микролампорты за единицу CU, чтобы приоритет вышел заданной суммой."""
    if cu_units <= 0:
        return 0
    return int(приоритет_лампорты * 1_000_000 // cu_units)


# ------------------------------------------------------------------ сборка

ПУЛЫ_ПОЛОСЫ = ("PUMP_AMM", "CPMM")     # имена констант в c2_swap_build


def _модули():
    """Модули тени подгружаются лениво: без них полоса просто выключена."""
    import c2_common as C  # noqa: PLC0415
    import c2_pool_programs as PP  # noqa: PLC0415
    import c2_shadow_build as SB  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    return C, PP, SB, B


def тип_пула_подходит(программа: str) -> bool:
    """Полоса берёт только одношаговые пулы с котировкой SOL.

    Слово владельца: Pump AMM или Raydium CPMM. Сосредоточенная ликвидность
    (DLMM, CLMM, DAMM v2) сюда не идёт -- там минимум по резервам не
    выдаётся, и отправлять вслепую нельзя.
    """
    try:
        _, _, _, B = _модули()
    except Exception:  # noqa: BLE001
        return False
    return программа in {getattr(B, имя) for имя in ПУЛЫ_ПОЛОСЫ}


def потолок_лампортов() -> int:
    return int(round(ПОТОЛОК_РАЗМЕРА_SOL * ЛАМПОРТОВ_В_SOL))


def собрать(*, tx_источника: dict, источник: str, минт: str, наш_кошелёк: str,
             лампорты: int, проскальзывание: float = 0.35,
             cu_units: int = 400_000, приоритет_лампорты: int = 1_000_000,
             чаевые_лампорты: int = 1_000_000, семя: str | None = None) -> dict:
    """Наша покупка одной транзакцией: своп + приоритет + чаевые Sender.

    Возвращает НЕПОДПИСАННУЮ транзакцию. Ни подписи, ни отправки здесь нет
    намеренно: собрать -- дёшево и безопасно, а подпись это уже деньги.
    """
    из_ = {"ok": False, "why_not": None, "pool_program": None, "min_out": None,
            "expected_out": None, "tip_account": None, "build_ms": None}
    t0 = time.perf_counter()
    # ПОТОЛОК И КОШЕЛЁК -- ДО всякой работы. Проверять их после сборки
    # бессмысленно: собранную транзакцию уже можно подписать.
    if наш_кошелёк != ST.EXECUTOR_WALLET:
        из_["why_not"] = ("сборка не на кошелёк исполнителя: "
                           f"{str(наш_кошелёк)[:12]} вместо {ST.EXECUTOR_WALLET[:12]}")
        return из_
    if not isinstance(лампорты, int) or лампорты <= 0:
        из_["why_not"] = f"размер не положительное целое: {лампорты!r}"
        return из_
    if лампорты > потолок_лампортов():
        из_["why_not"] = (f"размер {лампорты / ЛАМПОРТОВ_В_SOL} SOL выше потолка "
                           f"полосы {ПОТОЛОК_РАЗМЕРА_SOL} SOL")
        return из_
    try:
        C, PP, SB, B = _модули()
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"модули сборки не загружены: {type(exc).__name__}"
        return из_
    try:
        пул = C.identify_pool(tx_источника, источник, минт)
        if not пул.get("ok"):
            из_["why_not"] = f"пул: {пул.get('why_not')}"
            return из_
        прог = PP.pool_program(tx_источника, пул["pool_vault"], SB._labels())["pool_program"]
        из_["pool_program"] = прог
        if прог not in {getattr(B, имя) for имя in ПУЛЫ_ПОЛОСЫ}:
            из_["why_not"] = f"тип пула вне полосы: {прог}"
            return из_
        tpl = B.extract_template(tx_источника, прог, пул["pool_vault"])
        if not tpl.get("ok"):
            из_["why_not"] = f"шаблон: {tpl.get('why_not')}"
            return из_
        mv = B.mints_and_vaults(tpl, tx_источника)
        if not mv or mv.get("quote_mint") != C.WSOL:
            из_["why_not"] = "котировка пула не SOL -- полоса только одношаговая"
            return из_
        мо = B.min_out_from_reserves(tpl, tx_источника, лампорты, проскальзывание)
        if not мо.get("ok"):
            # Без минимума отправлять нельзя: это покупка по любой цене.
            из_["why_not"] = f"минимум не выдаётся: {мо.get('why_not')}"
            return из_
        # Флага ok мало: значение минимума тоже обязано быть положительным.
        # min_out=0 -- это и есть "куплю по любой цене", только под видом
        # посчитанного числа.
        if not isinstance(мо.get("min_out"), int) or мо["min_out"] <= 0:
            из_["why_not"] = f"минимум не положителен: {мо.get('min_out')!r}"
            return из_
        чаевые = выбрать_чаевые(семя)
        собрано = B.build_buy(
            tpl, tx_источника, user=наш_кошелёк, payer=наш_кошелёк,
            amount_in=лампорты, min_out=мо["min_out"], cu_units=cu_units,
            cu_price_micro=цена_единицы_cu(приоритет_лампорты, cu_units),
            tip=(чаевые, чаевые_лампорты), wrap_sol=True)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"сборка: {type(exc).__name__}: {str(exc)[:160]}"
        return из_
    из_.update(ok=True, min_out=мо["min_out"], expected_out=мо.get("expected_out"),
               tip_account=чаевые, tx_base64=собрано["tx_base64"],
               size=собрано["size"], quote_mint=собрано.get("quote_mint"),
               build_ms=round((time.perf_counter() - t0) * 1000, 3))
    return из_


# ------------------------------------------------------------------ подпись

def подписать(tx_base64: str, *, blockhash: str, ожидаемый_кошелёк: str,
               секрет: str | None = None) -> dict:
    """Подставить настоящий blockhash и подписать нашим ключом.

    Сборщик компилирует сообщение с пустым blockhash -- подписывать такое
    бессмысленно, сеть его не примет. Здесь сообщение пересобирается с
    настоящим blockhash и только потом подписывается.

    Публичный ключ сверяется с кошельком позиции ПЕРЕД подписью и на каждой
    сделке. Чужой ключ подписал бы чужой кошелёк: это не предупреждение, а
    запрет.
    """
    из_ = {"ok": False, "why_not": None, "signature": None}
    try:
        import bloom_jupiter_sell as J  # noqa: PLC0415
        from solders.hash import Hash  # noqa: PLC0415
        from solders.message import MessageV0  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"модули подписи не загружены: {type(exc).__name__}"
        return из_
    # ПУСТОЙ BLOCKHASH -- ОТКАЗ. Сборщик компилирует сообщение с нулевым
    # хешем, и он же -- законный Hash.default(). Подписать такую транзакцию
    # можно, отправить тоже, а сеть её не примет: деньги на чаевые и
    # приоритет при этом уже потрачены. Проверка стоит ДО подписи.
    НУЛЕВОЙ = "1" * 32
    if not blockhash or str(blockhash) == НУЛЕВОЙ or set(str(blockhash)) == {"1"}:
        из_["why_not"] = "blockhash пустой -- такую транзакцию сеть не примет"
        return из_
    свой = J.ключ_от_нашего_кошелька(ожидаемый_кошелёк, секрет)
    if not свой.get("ok"):
        из_["why_not"] = свой.get("why_not") or "ключ не проверен"
        return из_
    try:
        сырой = base64.b64decode(tx_base64)
        tx = VersionedTransaction.from_bytes(сырой)
        msg = tx.message
        новое = MessageV0(msg.header, msg.account_keys, Hash.from_string(blockhash),
                           msg.instructions, msg.address_table_lookups)
        # Разбор ключа -- ОДИН на весь репозиторий, из dbot_rescue: он
        # понимает и base58, и массив байтов. Второй разборщик здесь значил
        # бы два разных понимания одного секрета, а это деньги.
        from dbot_rescue import load_rescue_keypair  # noqa: PLC0415
        # Разбор ключа -- в СВОЁМ try, и текст его исключения наружу не
        # уходит: в сообщение об ошибке разбора может попасть сам секрет.
        # Наружу идёт только имя класса исключения.
        try:
            kp = load_rescue_keypair(секрет if секрет is not None else J.ключ_сырой())
        except Exception as exc_ключ:  # noqa: BLE001
            из_["why_not"] = f"ключ не разобрался ({type(exc_ключ).__name__})"
            return из_
        if str(kp.pubkey()) != ожидаемый_кошелёк:
            из_["why_not"] = "публичный ключ не совпал с кошельком -- подпись отменена"
            return из_
        подписанная = VersionedTransaction(новое, [kp])
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"подпись: {type(exc).__name__}: {str(exc)[:160]}"
        return из_
    из_.update(ok=True, signature=str(подписанная.signatures[0]),
               tx_base64=base64.b64encode(bytes(подписанная)).decode())
    return из_


# ------------------------------------------------------------------ отправка

# ОТПРАВЛЕННЫЕ КЛЮЧИ ОПЕРАЦИЙ. Одна операция -- одна отправка, и точка.
# Повторная сборка по тому же сигналу даёт ДРУГУЮ транзакцию с другой
# подписью, то есть вторую настоящую покупку. Защита здесь, у самой отправки,
# а не у вызывающего: вызывающих может стать больше.
_ОТПРАВЛЕНО: dict = {}
_ЗАМОК = threading.Lock()


def забыть_отправленные() -> None:
    """Только для самопроверки: очистить память отправленных ключей."""
    with _ЗАМОК:
        _ОТПРАВЛЕНО.clear()


def уже_отправляли(ключ: str) -> dict | None:
    with _ЗАМОК:
        з = _ОТПРАВЛЕНО.get(ключ)
    return dict(з) if з else None


def _запомнить_отказ(ключ: str | None, почему: str) -> None:
    """Неудачная отправка тоже занимает ключ операции.

    Повторять её вслепую нельзя: транзакция могла уйти в сеть и не дойти до
    нас ответом. Снимать замок вправе только человек, разобравшись.
    """
    if not ключ:
        return
    with _ЗАМОК:
        _ОТПРАВЛЕНО[ключ] = {"result": None, "why_not": почему}


def отправить(tx_base64: str, *, ключ_операции: str | None = None,
               адрес: str | None = None, таймаут: float = 5.0,
               отправитель=None) -> dict:
    """Одна отправка в Helius Sender. Метод и параметры -- из документации.

    Sender не тратит кредиты тарифа и берёт плату чаевыми в SOL; предел 50
    транзакций в секунду. skipPreflight=true и maxRetries=0 стоят так же,
    как в примере документации: предполётная проверка стоит задержки, а
    повторы делаем сами, зная своё состояние.

    Без BLOOM_OWN_SEND_LIVE=1 не уходит ничего. Это не настройка скорости,
    а рубильник живых денег.
    """
    из_ = {"ok": False, "sent": False, "why_not": None, "http": None,
            "result": None, "send_ms": None}
    if not живьём():
        из_["why_not"] = "BLOOM_OWN_SEND_LIVE не равен 1 -- отправка выключена"
        return из_
    # Ключ операции резервируется ДО сети и под замком: два потока, вошедшие
    # одновременно, не должны оба увидеть "ещё не отправляли".
    if ключ_операции:
        with _ЗАМОК:
            прежнее = _ОТПРАВЛЕНО.get(ключ_операции)
            if прежнее is not None:
                из_["why_not"] = (f"по ключу операции {ключ_операции} уже отправляли: "
                                   f"{прежнее.get('result') or прежнее.get('why_not')}")
                из_["duplicate"] = True
                return из_
            _ОТПРАВЛЕНО[ключ_операции] = {"result": None, "why_not": "отправка идёт"}
    тело = {"jsonrpc": "2.0", "id": str(int(time.time() * 1000)),
             "method": "sendTransaction",
             "params": [tx_base64, {"encoding": "base64", "skipPreflight": True,
                                     "maxRetries": 0}]}
    t0 = time.perf_counter()
    try:
        if отправитель is None:
            import requests  # noqa: PLC0415

            def отправитель(url, данные, таймаут_):  # noqa: E306
                r = requests.post(url, json=данные, timeout=таймаут_,
                                  headers={"Content-Type": "application/json"})
                return r.status_code, r.text
        код, текст = отправитель(адрес or адрес_сендера(), тело, таймаут)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"сеть: {type(exc).__name__}: {str(exc)[:160]}"
        из_["send_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        _запомнить_отказ(ключ_операции, из_["why_not"])
        return из_
    из_["send_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    из_["http"] = код
    try:
        ответ = json.loads(текст)
    except ValueError:
        из_["why_not"] = f"ответ не JSON: {текст[:160]}"
        return из_
    if isinstance(ответ, dict) and ответ.get("error"):
        из_["why_not"] = f"Sender отказал: {json.dumps(ответ['error'], ensure_ascii=False)[:200]}"
        return из_
    # УСПЕХ -- ЭТО HTTP 200 И ПОДПИСЬ В ОТВЕТЕ. Без подписи мы не знаем, что
    # ушло, и считать такую отправку удачной значит потом искать в цепи то,
    # чего, возможно, нет.
    подпись = (ответ or {}).get("result") if isinstance(ответ, dict) else None
    if код != 200:
        из_["why_not"] = f"Sender ответил кодом {код}, а не 200"
        return из_
    if not isinstance(подпись, str) or not подпись:
        из_["why_not"] = "Sender не вернул подпись -- отправка не подтверждена"
        return из_
    из_.update(ok=True, sent=True, result=подпись)
    if ключ_операции:
        with _ЗАМОК:
            _ОТПРАВЛЕНО[ключ_операции] = {"result": подпись, "why_not": None}
    return из_


# ------------------------------------------------------------------ пределы полосы

ЛИМИТ_ОТКРЫТЫХ = 1
ЛИМИТ_В_СУТКИ = 20
СТОП_ПОДРЯД_УПАВШИХ = 3
СТОП_УБЫТОК_SOL = 0.1
МЕТКА = "own_send"


def состояние_полосы(позиции: dict, *, сейчас: float | None = None) -> dict:
    """Что полоса сделала за сутки и можно ли ей ещё.

    Считается по ПОЗИЦИЯМ с меткой полосы, а не по журналу: журнал говорит о
    намерениях, а пределы владельца -- про сделки. Упавшей считается покупка,
    которая по цепи не села (chain_ok=false), а не та, что ушла в минус:
    три убыточных подряд -- это рынок, три упавших -- это мы.
    """
    сейчас = сейчас if сейчас is not None else time.time()
    сутки_назад = сейчас - 86400.0
    свои = [p for p in (позиции or {}).values()
            if (p or {}).get("lane") == МЕТКА]
    за_сутки = [p for p in свои if float(p.get("ts_intent") or 0) >= сутки_назад]
    открытых = [p for p in свои
                if p.get("state") not in (ST.STATE_CLOSED, "unsold", "closed")]
    по_времени = sorted(за_сутки, key=lambda p: float(p.get("ts_intent") or 0))
    подряд = 0
    for p in reversed(по_времени):
        if p.get("chain_ok") is False:
            подряд += 1
        elif p.get("chain_ok") is True:
            break
    итог_sol = 0.0
    for p in за_сутки:
        вх = p.get("sol_in")
        наз = (p.get("closed_sol_net")
               if p.get("closed_sol_net") is not None
               else (p.get("last_sell_outcome") or {}).get("sol_delta_net"))
        if вх and наз is not None:
            итог_sol += float(наз) - float(вх)
    return {"open": len(открытых), "today": len(за_сутки),
            "failed_in_row": подряд, "pnl_sol": round(итог_sol, 9),
            "total": len(свои)}


def можно_отправлять(позиции: dict, *, kill: bool = False,
                      сейчас: float | None = None) -> dict:
    """Пропускать ли ещё одну сделку полосы. Отказ называется словами."""
    с = состояние_полосы(позиции, сейчас=сейчас)
    почему = None
    if kill:
        почему = "рубильник KILL включён"
    elif not включена():
        почему = "полоса выключена (BLOOM_OWN_SEND не равен 1)"
    elif с["open"] >= ЛИМИТ_ОТКРЫТЫХ:
        почему = f"открытых позиций полосы {с['open']} при пределе {ЛИМИТ_ОТКРЫТЫХ}"
    elif с["today"] >= ЛИМИТ_В_СУТКИ:
        почему = f"за сутки уже {с['today']} сделок при пределе {ЛИМИТ_В_СУТКИ}"
    elif с["failed_in_row"] >= СТОП_ПОДРЯД_УПАВШИХ:
        почему = (f"{с['failed_in_row']} упавших подряд -- полоса остановлена "
                   "до разбора")
    elif с["pnl_sol"] <= -СТОП_УБЫТОК_SOL:
        почему = (f"убыток полосы за сутки {с['pnl_sol']} SOL при пороге "
                   f"-{СТОП_УБЫТОК_SOL}")
    return {"ok": почему is None, "why_not": почему, "state": с}


# ------------------------------------------------------------------ самопроверка

def self_test() -> int:
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    # --- числа из документации, а не из головы
    chk("tip-аккаунтов ровно десять, как на странице Sender Max",
        len(TIP_ACCOUNTS) == 10 and len(set(TIP_ACCOUNTS)) == 10, len(TIP_ACCOUNTS))
    алфавит = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    chk("все tip-адреса -- base58 нужной длины",
        all(32 <= len(a) <= 44 and set(a) <= алфавит for a in TIP_ACCOUNTS),
        [a for a in TIP_ACCOUNTS if not (32 <= len(a) <= 44)])
    chk("минимальный тип -- 0.001 SOL, как в тарифе Sender Max",
        TIP_MIN_SOL == 0.001, TIP_MIN_SOL)
    chk("адрес Амстердама -- тот, что в документации",
        SENDER_HOSTS["ams"] == "http://ams-sender.helius-rpc.com/fast",
        SENDER_HOSTS["ams"])

    # --- чаевые ниже минимума не опускаются
    было = os.environ.get("BLOOM_OWN_SEND_TIP_SOL")
    os.environ["BLOOM_OWN_SEND_TIP_SOL"] = "0.000005"
    chk("чаевые ниже документированного минимума поднимаются до него",
        чаевые_sol() == TIP_MIN_SOL, чаевые_sol())
    if было is None:
        os.environ.pop("BLOOM_OWN_SEND_TIP_SOL", None)
    else:
        os.environ["BLOOM_OWN_SEND_TIP_SOL"] = было

    # --- приоритет: заданная сумма, а не наугад взятая цена единицы
    chk("цена единицы CU даёт заданный приоритет",
        цена_единицы_cu(1_000_000, 400_000) == 2_500_000,
        цена_единицы_cu(1_000_000, 400_000))
    chk("нулевой лимит CU не делит на ноль",
        цена_единицы_cu(1_000_000, 0) == 0, цена_единицы_cu(1_000_000, 0))

    # --- адрес чаевых разный, но по одному семени повторяем
    а1 = выбрать_чаевые("ПОДПИСЬ_ОДИН")
    chk("по одному семени адрес чаевых один и тот же",
        а1 == выбрать_чаевые("ПОДПИСЬ_ОДИН") and а1 in TIP_ACCOUNTS, а1)
    разные = {выбрать_чаевые(f"С{i}") for i in range(40)}
    chk("по разным семенам адреса расходятся", len(разные) > 1, разные)

    # --- ОТПРАВКА БЕЗ РУБИЛЬНИКА НЕ УХОДИТ
    было_live = os.environ.get("BLOOM_OWN_SEND_LIVE")
    os.environ.pop("BLOOM_OWN_SEND_LIVE", None)
    ушло: list = []

    def перехват(url, данные, таймаут):
        ушло.append((url, данные))
        return 200, json.dumps({"jsonrpc": "2.0", "result": "ПОДПИСЬ"})

    р = отправить("AAA", отправитель=перехват)
    chk("без BLOOM_OWN_SEND_LIVE=1 не уходит ничего",
        р["sent"] is False and not ушло and "не равен 1" in (р["why_not"] or ""), р)

    os.environ["BLOOM_OWN_SEND_LIVE"] = "1"
    р2 = отправить("AAA", адрес="http://пример/fast", отправитель=перехват)
    chk("с рубильником отправка уходит и подпись возвращается",
        р2["ok"] and р2["result"] == "ПОДПИСЬ" and len(ушло) == 1, р2)
    url, тело = ушло[0]
    chk("метод и параметры -- ровно из документации Sender",
        тело["method"] == "sendTransaction"
        and тело["params"][0] == "AAA"
        and тело["params"][1] == {"encoding": "base64", "skipPreflight": True,
                                   "maxRetries": 0}, тело)
    ушло.clear()

    # --- ПОВТОРНАЯ ОТПРАВКА ПО ТОМУ ЖЕ КЛЮЧУ ОПЕРАЦИИ. Вторая сборка даёт
    # ДРУГУЮ транзакцию с другой подписью, то есть вторую настоящую покупку.
    забыть_отправленные()
    ушло.clear()
    р_1 = отправить("AAA", ключ_операции="ОП1", отправитель=перехват)
    р_2 = отправить("BBB", ключ_операции="ОП1", отправитель=перехват)
    chk("первая отправка по ключу операции уходит",
        р_1["ok"] and len(ушло) == 1, (р_1, len(ушло)))
    chk("вторая по ТОМУ ЖЕ ключу в сеть не уходит вовсе",
        р_2["ok"] is False and р_2.get("duplicate") is True and len(ушло) == 1,
        (р_2, len(ушло)))
    р_3 = отправить("CCC", ключ_операции="ОП2", отправитель=перехват)
    chk("другой ключ операции не заблокирован",
        р_3["ok"] and len(ушло) == 2, (р_3, len(ушло)))

    # Неудачная отправка тоже занимает ключ: транзакция могла уйти, а ответ
    # не дойти. Повторять её вслепую нельзя.
    забыть_отправленные()
    ушло.clear()

    def падает_сеть(url, данные, таймаут):
        ушло.append((url, данные))
        raise TimeoutError("сеть молчит")

    н1 = отправить("AAA", ключ_операции="ОП3", отправитель=падает_сеть)
    н2 = отправить("AAA", ключ_операции="ОП3", отправитель=падает_сеть)
    chk("после сетевой неудачи повтор по тому же ключу запрещён",
        н1["ok"] is False and н2.get("duplicate") is True and len(ушло) == 1,
        (н1["why_not"], н2["why_not"], len(ушло)))
    забыть_отправленные()
    ушло.clear()

    # --- УСПЕХ ТОЛЬКО ПРИ HTTP 200 И ПОДПИСИ В ОТВЕТЕ.
    def код_500(url, данные, таймаут):
        return 500, json.dumps({"jsonrpc": "2.0", "result": "ПОДПИСЬ"})

    п500 = отправить("AAA", отправитель=код_500)
    chk("код 500 -- не успех, даже если в теле есть подпись",
        п500["ok"] is False and "кодом 500" in (п500["why_not"] or ""), п500)

    def без_подписи(url, данные, таймаут):
        return 200, json.dumps({"jsonrpc": "2.0", "id": "1"})

    пбп = отправить("AAA", отправитель=без_подписи)
    chk("200 без подписи -- не успех, искать в цепи нечего",
        пбп["ok"] is False and "не вернул подпись" in (пбп["why_not"] or ""), пбп)

    def отказ(url, данные, таймаут):
        return 200, json.dumps({"jsonrpc": "2.0",
                                 "error": {"code": -32002, "message": "нет"}})

    р3 = отправить("AAA", отправитель=отказ)
    chk("отказ Sender -- не успех и назван словами",
        р3["ok"] is False and "Sender отказал" in (р3["why_not"] or ""), р3)

    def падает(url, данные, таймаут):
        raise TimeoutError("сеть молчит")

    р4 = отправить("AAA", отправитель=падает)
    chk("падение сети -- отказ с причиной, а не исключение наружу",
        р4["ok"] is False and "TimeoutError" in (р4["why_not"] or ""), р4)
    if было_live is None:
        os.environ.pop("BLOOM_OWN_SEND_LIVE", None)
    else:
        os.environ["BLOOM_OWN_SEND_LIVE"] = было_live

    # --- ПОТОЛОК РАЗМЕРА И КОШЕЛЁК. Проверяются ДО сборки: собранную
    # транзакцию уже можно подписать.
    выше = собрать(tx_источника={"meta": {}}, источник="SRC", минт="M",
                    наш_кошелёк=ST.EXECUTOR_WALLET,
                    лампорты=потолок_лампортов() + 1)
    chk("размер выше потолка полосы -- отказ, а не обрезка",
        выше["ok"] is False and "выше потолка" in (выше["why_not"] or ""), выше)
    чужой = собрать(tx_источника={"meta": {}}, источник="SRC", минт="M",
                     наш_кошелёк="ЧУЖОЙ_КОШЕЛЁК", лампорты=10_000_000)
    chk("сборка не на кошелёк исполнителя -- отказ",
        чужой["ok"] is False and "кошелёк исполнителя" in (чужой["why_not"] or ""),
        чужой)
    ноль = собрать(tx_источника={"meta": {}}, источник="SRC", минт="M",
                    наш_кошелёк=ST.EXECUTOR_WALLET, лампорты=0)
    chk("нулевой размер -- отказ", ноль["ok"] is False, ноль)

    # --- МИНИМУМ РАВНЫЙ НУЛЮ НЕ ПРОХОДИТ. Флага ok мало: ноль -- это и есть
    # "куплю по любой цене", только под видом посчитанного числа.
    class МодулиСНулевымМинимумом:
        class B:
            PUMP_AMM = "PUMP"
            CPMM = "CPMM"

            @staticmethod
            def extract_template(*a, **kw):
                return {"ok": True, "program": "PUMP"}

            @staticmethod
            def mints_and_vaults(*a, **kw):
                return {"quote_mint": "So11111111111111111111111111111111111111112"}

            @staticmethod
            def min_out_from_reserves(*a, **kw):
                return {"ok": True, "min_out": 0, "expected_out": 0}

            @staticmethod
            def build_buy(*a, **kw):
                raise AssertionError("до сборки дойти не должно")

        class C:
            WSOL = "So11111111111111111111111111111111111111112"

            @staticmethod
            def identify_pool(*a, **kw):
                return {"ok": True, "pool_vault": "ХРАН", "quote_mint": C_WSOL}

        class PP:
            @staticmethod
            def pool_program(*a, **kw):
                return {"pool_program": "PUMP"}

        class SB:
            @staticmethod
            def _labels():
                return {}

    C_WSOL = "So11111111111111111111111111111111111111112"
    было_модули = globals()["_модули"]
    globals()["_модули"] = lambda: (МодулиСНулевымМинимумом.C,
                                     МодулиСНулевымМинимумом.PP,
                                     МодулиСНулевымМинимумом.SB,
                                     МодулиСНулевымМинимумом.B)
    try:
        м0 = собрать(tx_источника={"meta": {}}, источник="SRC", минт="M",
                      наш_кошелёк=ST.EXECUTOR_WALLET, лампорты=10_000_000)
        chk("минимум ноль -- отказ, покупки по любой цене не будет",
            м0["ok"] is False and "минимум не положителен" in (м0["why_not"] or ""),
            м0)
    finally:
        globals()["_модули"] = было_модули

    # --- ПУСТОЙ BLOCKHASH. Подписать можно, отправить можно, сеть не примет,
    # а чаевые и приоритет уже потрачены.
    пусто_bh = подписать("AAA", blockhash="1" * 32,
                          ожидаемый_кошелёк=ST.EXECUTOR_WALLET, секрет="не ключ")
    chk("пустой blockhash -- отказ ДО проверки ключа",
        пусто_bh["ok"] is False and "blockhash пустой" in (пусто_bh["why_not"] or ""),
        пусто_bh)
    нет_bh = подписать("AAA", blockhash="",
                        ожидаемый_кошелёк=ST.EXECUTOR_WALLET, секрет="не ключ")
    chk("отсутствующий blockhash -- тоже отказ", нет_bh["ok"] is False, нет_bh)

    # --- ТЕКСТ ИСКЛЮЧЕНИЯ РАЗБОРА КЛЮЧА НАРУЖУ НЕ ИДЁТ: в него может попасть
    # сам секрет.
    плохой = подписать("AAA", blockhash="11111111111111111111111111111112",
                        ожидаемый_кошелёк=ST.EXECUTOR_WALLET,
                        секрет="СЕКРЕТНАЯ_СТРОКА_КОТОРОЙ_ТУТ_БЫТЬ_НЕ_ДОЛЖНО")
    chk("секрет не утекает в причину отказа",
        плохой["ok"] is False
        and "СЕКРЕТНАЯ_СТРОКА" not in (плохой["why_not"] or ""), плохой["why_not"])

    # --- подпись чужим ключом запрещена
    п = подписать("AAA", blockhash="11111111111111111111111111111111",
                   ожидаемый_кошелёк="ЧУЖОЙ_КОШЕЛЁК", секрет="не ключ")
    chk("чужой или негодный ключ -- подписи нет",
        п["ok"] is False and п["why_not"], п)

    # --- пределы полосы
    def поз(cid, *, ts, состояние="closed", цепь=True, вход=0.01, назад=None,
             метка=МЕТКА):
        p = {"client_order_id": cid, "lane": метка, "ts_intent": ts,
             "state": состояние, "chain_ok": цепь, "sol_in": вход}
        if назад is not None:
            p["closed_sol_net"] = назад
        return p

    сейчас = 1_790_000_000.0
    пусто = можно_отправлять({}, сейчас=сейчас)
    chk("выключенная полоса не пропускает",
        пусто["ok"] is False and "выключена" in пусто["why_not"], пусто)
    было_вкл = os.environ.get("BLOOM_OWN_SEND")
    os.environ["BLOOM_OWN_SEND"] = "1"
    chk("включённая и пустая полоса пропускает",
        можно_отправлять({}, сейчас=сейчас)["ok"], можно_отправлять({}, сейчас=сейчас))
    chk("KILL останавливает полосу",
        можно_отправлять({}, kill=True, сейчас=сейчас)["ok"] is False,
        можно_отправлять({}, kill=True, сейчас=сейчас))
    открытая = {"a": поз("a", ts=сейчас - 10, состояние="bought")}
    chk("одна открытая позиция полосы закрывает вход второй",
        можно_отправлять(открытая, сейчас=сейчас)["ok"] is False
        and "открытых" in можно_отправлять(открытая, сейчас=сейчас)["why_not"],
        можно_отправлять(открытая, сейчас=сейчас))
    сутки = {str(i): поз(str(i), ts=сейчас - 100 - i, назад=0.01)
             for i in range(ЛИМИТ_В_СУТКИ)}
    chk("предел сделок за сутки соблюдается",
        можно_отправлять(сутки, сейчас=сейчас)["ok"] is False
        and "за сутки" in можно_отправлять(сутки, сейчас=сейчас)["why_not"],
        можно_отправлять(сутки, сейчас=сейчас)["why_not"])
    вчера = {str(i): поз(str(i), ts=сейчас - 90000 - i, назад=0.01)
             for i in range(ЛИМИТ_В_СУТКИ)}
    chk("вчерашние сделки в сегодняшний предел не идут",
        можно_отправлять(вчера, сейчас=сейчас)["ok"], можно_отправлять(вчера, сейчас=сейчас))
    упавшие = {str(i): поз(str(i), ts=сейчас - 300 + i, цепь=False)
               for i in range(СТОП_ПОДРЯД_УПАВШИХ)}
    chk("три упавших подряд останавливают полосу",
        можно_отправлять(упавшие, сейчас=сейчас)["ok"] is False
        and "подряд" in можно_отправлять(упавшие, сейчас=сейчас)["why_not"],
        можно_отправлять(упавшие, сейчас=сейчас)["why_not"])
    с_разрывом = dict(упавшие)
    с_разрывом["ок"] = поз("ок", ts=сейчас - 100, цепь=True, назад=0.01)
    chk("севшая покупка обрывает серию упавших",
        можно_отправлять(с_разрывом, сейчас=сейчас)["ok"],
        можно_отправлять(с_разрывом, сейчас=сейчас))
    убыток = {"u1": поз("u1", ts=сейчас - 200, вход=0.2, назад=0.05)}
    chk("убыток полосы за сутки останавливает её",
        можно_отправлять(убыток, сейчас=сейчас)["ok"] is False
        and "убыток" in можно_отправлять(убыток, сейчас=сейчас)["why_not"],
        можно_отправлять(убыток, сейчас=сейчас)["why_not"])
    чужие = {"b": поз("b", ts=сейчас - 10, состояние="bought", метка=None)}
    chk("позиции ОБЫЧНОЙ полосы пределам этой не мешают",
        можно_отправлять(чужие, сейчас=сейчас)["ok"],
        можно_отправлять(чужие, сейчас=сейчас))
    if было_вкл is None:
        os.environ.pop("BLOOM_OWN_SEND", None)
    else:
        os.environ["BLOOM_OWN_SEND"] = было_вкл

    # --- сборка отказывается там, где отправлять вслепую нельзя
    пусто_сб = собрать(tx_источника={}, источник="SRC", минт="M",
                        наш_кошелёк=ST.EXECUTOR_WALLET, лампорты=10_000_000)
    chk("на пустой транзакции сборка отказывает словами, а не падает",
        пусто_сб["ok"] is False and пусто_сб["why_not"], пусто_сб)

    # --- В МОДУЛЕ НЕТ ВТОРОГО ПУТИ ОТПРАВКИ. Считаем только рабочую часть,
    # до самопроверки: в самой самопроверке имя метода встречается в
    # ожиданиях, и это не второй путь.
    текст = Path(__file__).read_text(encoding="utf-8")
    рабочая = текст.split("def self_test")[0]
    chk("sendTransaction в рабочей части ровно один -- в отправить()",
        рабочая.count('"sendTransaction"') == 1, рабочая.count('"sendTransaction"'))
    chk("в рабочей части нет второго адреса отправки помимо реестра регионов",
        рабочая.count("helius-rpc.com/fast") == len(SENDER_HOSTS),
        рабочая.count("helius-rpc.com/fast"))

    print(f"самопроверка полосы своей отправки: {всего[1]}/{всего[0]} пройдено")
    return 0 if всего[1] == всего[0] else 1


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else 0)
