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


def отправить(tx_base64: str, *, состояние=None, ключ_операции: str | None = None,
               минт: str | None = None, лампорты: int | None = None,
               источник_подпись: str | None = None,
               источник_слот: int | None = None,
               адрес: str | None = None, таймаут: float = 5.0,
               отправитель=None, сейчас: float | None = None,
               без_учёта: bool = False) -> dict:
    """Одна отправка в Helius Sender. Метод и параметры -- из документации.

    Sender не тратит кредиты тарифа и берёт плату чаевыми в SOL; предел 50
    транзакций в секунду. skipPreflight=true и maxRetries=0 стоят так же,
    как в примере документации: предполётная проверка стоит задержки, а
    повторы делаем сами, зная своё состояние.

    Без BLOOM_OWN_SEND_LIVE=1 не уходит ничего. Это не настройка скорости,
    а рубильник живых денег.
    """
    из_ = {"ok": False, "sent": False, "why_not": None, "http": None,
            "result": None, "send_ms": None, "cid": None}
    if not живьём():
        из_["why_not"] = "BLOOM_OWN_SEND_LIVE не равен 1 -- отправка выключена"
        return из_
    # ВСЁ, ЧТО РЕШАЕТ ПРО ДЕНЬГИ, -- ПОД ОДНИМ ЗАМКОМ И ДО СЕТИ: ключ
    # операции, рубильник, пределы полосы и резервирующая запись позиции.
    # Раньше пределы проверял вызывающий, а замок держал только ключ
    # операции; два сигнала в двух потоках оба видели "открытых 0" и оба
    # отправляли -- предел "одна открытая" не работал именно тогда, когда он
    # нужен. Теперь позиция ложится на диск ЗДЕСЬ, внутри замка, и второй
    # поток видит её как открытую.
    with _ЗАМОК:
        if ключ_операции:
            прежнее = _ОТПРАВЛЕНО.get(ключ_операции)
            if прежнее is not None:
                из_["why_not"] = (f"по ключу операции {ключ_операции} уже отправляли: "
                                   f"{прежнее.get('result') or прежнее.get('why_not')}")
                из_["duplicate"] = True
                return из_
        if not без_учёта:
            if состояние is None:
                из_["why_not"] = ("состояние не передано -- ни рубильник, ни пределы "
                                   "полосы проверить нечем, отправка отменена")
                return из_
            убит, почему_kill = состояние.kill_active()
            if убит:
                из_["why_not"] = f"рубильник: {почему_kill}"
                из_["kill"] = True
                return из_
            try:
                позиции = состояние.positions()
            except Exception as exc:  # noqa: BLE001
                из_["why_not"] = (f"позиции не прочитаны ({type(exc).__name__}) -- "
                                   "пределы полосы не проверить")
                return из_
            гейт = можно_отправлять(позиции, сейчас=сейчас)
            if not гейт.get("ok"):
                из_["why_not"] = f"предел полосы: {гейт.get('why_not')}"
                из_["lane_state"] = гейт.get("state")
                return из_
            if not минт or not isinstance(лампорты, int) or лампорты <= 0:
                из_["why_not"] = ("для записи позиции полосы нужны минт и размер "
                                   f"(минт {минт!r}, лампорты {лампорты!r})")
                return из_
            if лампорты > потолок_лампортов():
                из_["why_not"] = (f"размер {лампорты} лампортов выше потолка полосы "
                                   f"{потолок_лампортов()} -- отправка отменена")
                return из_
            # РЕЗЕРВИРУЮЩАЯ ЗАПИСЬ. Позиция с state=intent и меткой полосы --
            # это и есть бронь: её видит и второй поток, и сторож, и она
            # остаётся на диске, если процесс упадёт между отправкой и ответом.
            cid = f"lane-{ключ_операции or int(time.time() * 1000)}"
            try:
                состояние.write_intent(
                    client_order_id=cid, mint=минт, source_sig=источник_подпись or "",
                    source_slot=источник_слот, sol_in=лампорты / ЛАМПОРТОВ_В_SOL,
                    pool=None, program=None, taxed=None, tax_bps=None,
                    mode=ST.MODE_LIVE, sell_after_s=СРОК_ПРОДАЖИ_S, lane=МЕТКА)
            except Exception as exc:  # noqa: BLE001
                из_["why_not"] = (f"позиция полосы не записана ({type(exc).__name__}) "
                                   "-- без записи отправлять нельзя: сторож о ней не "
                                   "узнает")
                return из_
            из_["cid"] = cid
        if ключ_операции:
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
        # ОТКАЗ ОПРЕДЕЛЁННЫЙ: узел разобрал транзакцию и отверг её -- в цепь
        # она не попала. Только на таком отказе бронь можно закрывать. Все
        # прочие неудачи (сеть, не-JSON, 200 без подписи) неопределённы:
        # транзакция могла уйти, и закрывать бронь по ним нельзя.
        из_["definite_refusal"] = True
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

# Срок продажи позиции полосы. Слово владельца: сторожем через 28.8 с, как у
# боевых покупок -- сравнение честно только при одинаковом горизонте.
СРОК_ПРОДАЖИ_S = ST.env_float("BLOOM_OWN_SEND_SELL_AFTER_S", 28.8)

ЛИМИТ_ОТКРЫТЫХ = 1
ЛИМИТ_В_СУТКИ = 20
СТОП_ПОДРЯД_УПАВШИХ = 3
СТОП_УБЫТОК_SOL = 0.1
МЕТКА = ST.МЕТКА_ПОЛОСЫ        # одно написание на весь репозиторий


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


# --------------------------------------------- сколько купила полоса

def купленное_raw(tx: dict, кошелёк: str, минт: str) -> dict:
    """Сколько СЫРЫХ единиц минта прибыло на наш адрес этой транзакцией.

    Это количество -- то самое, которое сторож потом продаёт. Считается
    ТОЛЬКО в сырых единицах (uiTokenAmount.amount): у токена с девятью
    знаками движение в единицах показывается в ui нулём, а пыль -- числом.
    Цена ошибки прямая: по ui продали бы не то количество, что купили.

    Своих счетов минта может быть несколько (ATA плюс созданный в этой же
    транзакции), поэтому суммируем по всем, где владелец -- наш кошелёк.
    Отсутствие счёта в preTokenBalances -- обычный случай: счёт создан
    здесь же, и до транзакции его не было вовсе.
    """
    из_ = {"ok": False, "raw": None, "why_not": None}
    if not isinstance(tx, dict):
        из_["why_not"] = "транзакции нет"
        return из_
    мета = tx.get("meta")
    if not isinstance(мета, dict):
        из_["why_not"] = "у транзакции нет meta -- количество не посчитать"
        return из_
    if мета.get("err") is not None:
        из_["why_not"] = f"транзакция упала: {json.dumps(мета.get('err'), ensure_ascii=False)[:120]}"
        из_["chain_ok"] = False
        return из_
    if not кошелёк or not минт:
        из_["why_not"] = f"нужны кошелёк и минт (кошелёк {кошелёк!r}, минт {минт!r})"
        return из_
    было: dict = {}
    стало: dict = {}
    for где, куда in (("preTokenBalances", было), ("postTokenBalances", стало)):
        for б in (мета.get(где) or []):
            if not isinstance(б, dict) or б.get("owner") != кошелёк:
                continue
            if б.get("mint") != минт:
                continue
            сырое = (б.get("uiTokenAmount") or {}).get("amount")
            try:
                куда[б.get("accountIndex")] = int(сырое)
            except (TypeError, ValueError):
                из_["why_not"] = f"количество не число: {сырое!r}"
                return из_
    if not стало:
        из_["why_not"] = "нашего счёта этого минта в postTokenBalances нет"
        return из_
    дельта = sum(стало.values()) - sum(было.get(и, 0) for и in стало)
    if дельта <= 0:
        из_["why_not"] = f"приход не положителен: {дельта}"
        из_["raw"] = дельта
        return из_
    из_.update(ok=True, raw=дельта, chain_ok=True)
    return из_


# ------------------------------------------------------------------ весь путь

# СРОК ГОДНОСТИ ТЁПЛОГО BLOCKHASH. Сеть держит хеш около 150 слотов (~60 с).
# Тёплый хеш обновляет пульс детектора; просроченным подписывать нельзя --
# транзакция уйдёт, сеть её отбросит, и замер выйдет ложным: "не доехало"
# вместо "доехало медленно". Держим запас вдвое.
СРОК_BLOCKHASH_S = 30.0


def провести(*, tx_источника: dict, источник: str, минт: str, состояние,
              blockhash: str | None = None, blockhash_ts: float | None = None,
              лампорты: int | None = None, проскальзывание: float = 0.35,
              ключ_операции: str | None = None,
              источник_подпись: str | None = None,
              источник_слот: int | None = None,
              отправитель=None, rpc_call=None, секрет: str | None = None,
              сейчас: float | None = None) -> dict:
    """Весь путь полосы на один сигнал: сборка -> (симуляция | подпись и отправка).

    Одна точка входа нарочно. У полосы три шага, и каждый из них умеет
    отказать; если их вызывать по отдельности из детектора, порядок и
    причины отказа расползутся по вызывающим, а это деньги. Здесь же и
    стадия отказа называется словом: по ней видно, где полоса стоит.

    БЕЗ BLOOM_OWN_SEND_LIVE=1 доходит только до симуляции: ни подписи, ни
    отправки. Пределы и рубильник проверяются дважды -- дёшево здесь (чтобы
    не собирать зря) и обязательно внутри отправить() под замком, где
    делается бронь.
    """
    сейчас = сейчас if сейчас is not None else time.time()
    из_ = {"stage": "off", "ok": False, "sent": False, "dry": None,
            "why_not": None, "lane": МЕТКА, "mint": минт,
            "source_sig": источник_подпись, "cid": None, "signature": None}
    if not включена():
        из_["why_not"] = "полоса выключена (BLOOM_OWN_SEND не равен 1)"
        return из_
    лампорты = (лампорты if isinstance(лампорты, int)
                else int(round(размер_sol() * ЛАМПОРТОВ_В_SOL)))
    из_["lamports"] = лампорты
    из_["size_sol"] = лампорты / ЛАМПОРТОВ_В_SOL

    # ПРЕДВАРИТЕЛЬНЫЙ ГЕЙТ. Не заменяет тот, что внутри отправить(): он там
    # под замком и с бронью. Здесь только чтобы не тратить сеть на сборку,
    # когда полоса всё равно закрыта.
    из_["stage"] = "gate"
    if состояние is None:
        из_["why_not"] = "состояние не передано -- пределы полосы проверить нечем"
        return из_
    убит, почему_kill = состояние.kill_active()
    if убит:
        из_["why_not"] = f"рубильник: {почему_kill}"
        из_["kill"] = True
        return из_
    try:
        гейт = можно_отправлять(состояние.positions(), сейчас=сейчас)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"позиции не прочитаны ({type(exc).__name__})"
        return из_
    из_["lane_state"] = гейт.get("state")
    if not гейт.get("ok"):
        из_["why_not"] = f"предел полосы: {гейт.get('why_not')}"
        return из_

    # СБОРКА. Дёшево и безопасно: ни ключа, ни подписи, ни отправки.
    из_["stage"] = "build"
    сб = собрать(tx_источника=tx_источника or {}, источник=источник or "",
                 минт=минт or "", наш_кошелёк=ST.EXECUTOR_WALLET,
                 лампорты=лампорты, проскальзывание=проскальзывание,
                 семя=источник_подпись or ключ_операции)
    из_.update(pool_program=сб.get("pool_program"), min_out=сб.get("min_out"),
               expected_out=сб.get("expected_out"), build_ms=сб.get("build_ms"),
               size=сб.get("size"), tip_account=сб.get("tip_account"))
    if not сб.get("ok"):
        из_["why_not"] = сб.get("why_not")
        return из_

    # СИМУЛЯЦИЯ -- ПЕРЕД КАЖДОЙ ОТПРАВКОЙ, а не только в нежилом режиме.
    # Слово владельца 25.09: "сборка -> симуляция -> отправка, только если
    # симуляция прошла; иначе SKIP_SIM_FAIL с причиной". Цена этого решения
    # честная: один круг до узла (около 50-150 мс) прибавляется к НАШЕЙ
    # стороне, и в паре с Bloom мы этим временем платим за то, чтобы не
    # отправлять покупку, которая упадёт. Симуляция идёт по НЕПОДПИСАННОЙ
    # транзакции: sigVerify=false, blockhash подставляет узел.
    из_["stage"] = "sim"
    if rpc_call is None:
        # БЕЗ СИМУЛЯЦИИ НЕ ОТПРАВЛЯЕМ. Это не осторожность, а правило
        # владельца: отправка без симуляции запрещена в обоих режимах.
        из_["why_not"] = "симулировать нечем (узел не передан) -- отправка отменена"
        из_["code"] = "SKIP_SIM_FAIL"
        return из_
    t_сим = time.perf_counter()
    try:
        _, _, SB, _ = _модули()
        знач = (rpc_call("simulateTransaction",
                        [сб["tx_base64"], SB.SIM_OPTS]) or {}).get("value") or {}
        из_.update(SB.classify_sim(знач))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"симуляция: {type(exc).__name__}: {str(exc)[:160]}"
        из_["code"] = "SKIP_SIM_FAIL"
        из_["sim_ms"] = round((time.perf_counter() - t_сим) * 1000, 2)
        return из_
    из_["sim_ms"] = round((time.perf_counter() - t_сим) * 1000, 2)
    прошла = из_.get("sim_verdict") == "would_pass"
    if not прошла:
        из_["why_not"] = f"симуляция не прошла: {из_.get('sim_verdict')}"
        из_["code"] = "SKIP_SIM_FAIL"
        return из_
    if not живьём():
        # Нежилой режим: дальше подписи нет и денег нет.
        из_.update(stage="dry", dry=True, ok=True)
        return из_

    # ПОДПИСЬ. Тёплый blockhash обязан быть свежим: просроченным подписывать
    # бессмысленно, сеть такую транзакцию отбросит.
    из_["stage"] = "blockhash"
    if not blockhash:
        из_["why_not"] = "тёплого blockhash нет -- подписывать нечем"
        return из_
    возраст = (сейчас - float(blockhash_ts)) if blockhash_ts else None
    из_["blockhash_age_s"] = round(возраст, 2) if возраст is not None else None
    if возраст is not None and возраст > СРОК_BLOCKHASH_S:
        из_["why_not"] = (f"тёплый blockhash старше {СРОК_BLOCKHASH_S} с "
                          f"(возраст {из_['blockhash_age_s']} с) -- сеть отбросит")
        return из_
    из_["stage"] = "sign"
    t_подпись = time.perf_counter()
    пд = подписать(сб["tx_base64"], blockhash=blockhash,
                   ожидаемый_кошелёк=ST.EXECUTOR_WALLET, секрет=секрет)
    из_["sign_ms"] = round((time.perf_counter() - t_подпись) * 1000, 2)
    if not пд.get("ok"):
        из_["why_not"] = пд.get("why_not")
        return из_
    из_["signature"] = пд.get("signature")

    # ОТПРАВКА. Пределы, рубильник и бронь -- внутри, под замком.
    из_["stage"] = "send"
    о = отправить(пд["tx_base64"], состояние=состояние,
                  ключ_операции=ключ_операции, минт=минт, лампорты=лампорты,
                  источник_подпись=источник_подпись, источник_слот=источник_слот,
                  отправитель=отправитель, сейчас=сейчас)
    из_.update(sent=bool(о.get("sent")), http=о.get("http"),
               send_ms=о.get("send_ms"), cid=о.get("cid"),
               duplicate=о.get("duplicate"))
    if о.get("lane_state") is not None:
        из_["lane_state"] = о.get("lane_state")
    if о.get("ok"):
        из_.update(stage="sent", ok=True, dry=False)
        # Подпись из ответа Sender и наша сходятся по построению: мы её сами
        # и считали. Если вдруг нет -- это не успех, а расхождение, и в
        # позиции должны лежать обе.
        из_["signature_sender"] = о.get("result")
        try:
            состояние.update_position(
                о.get("cid"), state="bought", lane=МЕТКА,
                lane_signature=о.get("result") or пд.get("signature"),
                lane_signature_local=пд.get("signature"),
                ts_sent=сейчас, ts_accepted=time.time(),
                pool=None, program=сб.get("pool_program"),
                lane_min_out=сб.get("min_out"),
                lane_expected_out=сб.get("expected_out"),
                lane_send_ms=о.get("send_ms"), lane_build_ms=сб.get("build_ms"),
                lane_tip_account=сб.get("tip_account"))
        except Exception as exc:  # noqa: BLE001
            # Отправка уже состоялась: молчать нельзя, но и "не ok" ставить
            # поздно -- деньги ушли. Причина идёт в отчёт отдельным полем.
            из_["position_update_why_not"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return из_

    из_["why_not"] = о.get("why_not")
    if о.get("kill"):
        из_["kill"] = True
    # БРОНЬ ПОСЛЕ НЕУДАЧИ. Закрываем её ТОЛЬКО на определённом отказе узла:
    # он разобрал транзакцию и отверг её, в цепи её нет. Любая другая
    # неудача (сеть молчит, ответ не JSON, 200 без подписи) неопределённа --
    # транзакция могла уйти, и бронь обязана остаться открытой, иначе сторож
    # не узнает о токене, который у нас на руках.
    if о.get("cid"):
        try:
            if о.get("definite_refusal"):
                состояние.update_position(
                    о["cid"], state=ST.STATE_CLOSED, lane=МЕТКА,
                    close_reason="sender_refused", chain_ok=False,
                    why_not=str(о.get("why_not"))[:300])
                из_["reservation"] = "closed"
            else:
                состояние.update_position(
                    о["cid"], lane=МЕТКА, lane_send_ambiguous=True,
                    lane_signature_local=пд.get("signature"),
                    why_not=str(о.get("why_not"))[:300])
                из_["reservation"] = "open_ambiguous"
        except Exception as exc:  # noqa: BLE001
            из_["position_update_why_not"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return из_


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
    р2 = отправить("AAA", адрес="http://пример/fast", отправитель=перехват,
                    без_учёта=True)
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
    р_1 = отправить("AAA", ключ_операции="ОП1", отправитель=перехват, без_учёта=True)
    р_2 = отправить("BBB", ключ_операции="ОП1", отправитель=перехват, без_учёта=True)
    chk("первая отправка по ключу операции уходит",
        р_1["ok"] and len(ушло) == 1, (р_1, len(ушло)))
    chk("вторая по ТОМУ ЖЕ ключу в сеть не уходит вовсе",
        р_2["ok"] is False and р_2.get("duplicate") is True and len(ушло) == 1,
        (р_2, len(ушло)))
    р_3 = отправить("CCC", ключ_операции="ОП2", отправитель=перехват, без_учёта=True)
    chk("другой ключ операции не заблокирован",
        р_3["ok"] and len(ушло) == 2, (р_3, len(ушло)))

    # Неудачная отправка тоже занимает ключ: транзакция могла уйти, а ответ
    # не дойти. Повторять её вслепую нельзя.
    забыть_отправленные()
    ушло.clear()

    def падает_сеть(url, данные, таймаут):
        ушло.append((url, данные))
        raise TimeoutError("сеть молчит")

    н1 = отправить("AAA", ключ_операции="ОП3", отправитель=падает_сеть,
                    без_учёта=True)
    н2 = отправить("AAA", ключ_операции="ОП3", отправитель=падает_сеть,
                    без_учёта=True)
    chk("после сетевой неудачи повтор по тому же ключу запрещён",
        н1["ok"] is False and н2.get("duplicate") is True and len(ушло) == 1,
        (н1["why_not"], н2["why_not"], len(ушло)))
    забыть_отправленные()
    ушло.clear()

    # --- УСПЕХ ТОЛЬКО ПРИ HTTP 200 И ПОДПИСИ В ОТВЕТЕ.
    def код_500(url, данные, таймаут):
        return 500, json.dumps({"jsonrpc": "2.0", "result": "ПОДПИСЬ"})

    п500 = отправить("AAA", отправитель=код_500, без_учёта=True)
    chk("код 500 -- не успех, даже если в теле есть подпись",
        п500["ok"] is False and "кодом 500" in (п500["why_not"] or ""), п500)

    def без_подписи(url, данные, таймаут):
        return 200, json.dumps({"jsonrpc": "2.0", "id": "1"})

    пбп = отправить("AAA", отправитель=без_подписи, без_учёта=True)
    chk("200 без подписи -- не успех, искать в цепи нечего",
        пбп["ok"] is False and "не вернул подпись" in (пбп["why_not"] or ""), пбп)

    def отказ(url, данные, таймаут):
        return 200, json.dumps({"jsonrpc": "2.0",
                                 "error": {"code": -32002, "message": "нет"}})

    р3 = отправить("AAA", отправитель=отказ, без_учёта=True)
    chk("отказ Sender -- не успех и назван словами",
        р3["ok"] is False and "Sender отказал" in (р3["why_not"] or ""), р3)

    def падает(url, данные, таймаут):
        raise TimeoutError("сеть молчит")

    р4 = отправить("AAA", отправитель=падает, без_учёта=True)
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

    # --- ПОЛУЧАТЕЛИ ДЕНЕГ. Слово владельца: пул, наши счета и чаевые ТОЛЬКО
    # на счета Sender из data/docs/. Проверяем оба конца: что список адресов
    # совпадает со снятой страницей документации (а не выдуман), и что сборка
    # передаёт в покупку именно наш кошелёк и адрес из этого списка.
    страница = Path(__file__).resolve().parent.parent / "data" / "docs" / "helius_sender_max.md"
    if страница.exists():
        текст_стр = страница.read_text(encoding="utf-8", errors="replace")
        нет_в_странице = [а for а in TIP_ACCOUNTS if а not in текст_стр]
        chk("все десять tip-адресов есть в снятой странице Sender Max",
            not нет_в_странице, нет_в_странице)
    else:
        chk("страница Sender Max на месте (без неё адреса нечем сверить)",
            False, str(страница))
    семена = [f"ПОДПИСЬ_{и}" for и in range(300)]
    chk("чаевые уходят ТОЛЬКО на адреса из этого списка",
        all(выбрать_чаевые(с) in TIP_ACCOUNTS for с in семена), "")

    class МодулиСЗахватом:
        собрано: dict = {}

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
                return {"ok": True, "min_out": 777_000, "expected_out": 1_000_000}

            @staticmethod
            def build_buy(tpl, tx, **kw):
                МодулиСЗахватом.собрано = dict(kw)
                return {"tx_base64": "СОБРАНО", "size": 700,
                        "quote_mint": "So11111111111111111111111111111111111111112"}

        class C:
            WSOL = "So11111111111111111111111111111111111111112"

            @staticmethod
            def identify_pool(*a, **kw):
                return {"ok": True, "pool_vault": "ХРАНИЛИЩЕ_ПУЛА",
                        "quote_mint": "So11111111111111111111111111111111111111112"}

        class PP:
            @staticmethod
            def pool_program(*a, **kw):
                return {"pool_program": "PUMP"}

        class SB:
            @staticmethod
            def _labels():
                return {}

    было_модули2 = globals()["_модули"]
    globals()["_модули"] = lambda: (МодулиСЗахватом.C, МодулиСЗахватом.PP,
                                     МодулиСЗахватом.SB, МодулиСЗахватом.B)
    try:
        сб_п = собрать(tx_источника={"meta": {}}, источник="SRC", минт="МИНТ",
                        наш_кошелёк=ST.EXECUTOR_WALLET, лампорты=10_000_000,
                        семя="ПОДПИСЬ_ПОЛУЧАТЕЛЕЙ")
        зхв = МодулиСЗахватом.собрано
        chk("покупка собирается НА НАШ кошелёк и платит с него же",
            сб_п["ok"] and зхв.get("user") == ST.EXECUTOR_WALLET
            and зхв.get("payer") == ST.EXECUTOR_WALLET, зхв)
        chk("сумма и минимум уходят в сборку ровно те, что посчитаны",
            зхв.get("amount_in") == 10_000_000 and зхв.get("min_out") == 777_000
            and сб_п["min_out"] == 777_000, (зхв.get("amount_in"), зхв.get("min_out")))
        chk("чаевые -- на адрес Sender из списка и на документированную сумму",
            isinstance(зхв.get("tip"), tuple) and зхв["tip"][0] in TIP_ACCOUNTS
            and зхв["tip"][1] >= int(TIP_MIN_SOL * ЛАМПОРТОВ_В_SOL)
            and сб_п["tip_account"] == зхв["tip"][0], зхв.get("tip"))
    finally:
        globals()["_модули"] = было_модули2

    # --- СКОЛЬКО КУПИЛА ПОЛОСА. Ровно это количество сторож потом продаёт:
    # ошибка здесь -- это проданное не своё или недопроданное своё.
    def бал(индекс, владелец, минт_, сырое, знаков=6):
        return {"accountIndex": индекс, "owner": владелец, "mint": минт_,
                "uiTokenAmount": {"amount": str(сырое), "decimals": знаков,
                                   "uiAmount": сырое / (10 ** знаков)}}

    НАШ = ST.EXECUTOR_WALLET
    создан = {"meta": {"err": None, "preTokenBalances": [],
                        "postTokenBalances": [бал(5, НАШ, "М", 1_234_567)]}}
    chk("счёт создан этой же транзакцией: приход -- весь остаток",
        купленное_raw(создан, НАШ, "М") == {"ok": True, "raw": 1_234_567,
                                             "why_not": None, "chain_ok": True},
        купленное_raw(создан, НАШ, "М"))
    добавка = {"meta": {"err": None,
                         "preTokenBalances": [бал(5, НАШ, "М", 1_000)],
                         "postTokenBalances": [бал(5, НАШ, "М", 3_500)]}}
    chk("счёт был: приход -- разница, а не весь остаток",
        купленное_raw(добавка, НАШ, "М")["raw"] == 2_500,
        купленное_raw(добавка, НАШ, "М"))
    чужой_тоже = {"meta": {"err": None,
                            "preTokenBalances": [бал(9, "ЧУЖОЙ", "М", 10)],
                            "postTokenBalances": [бал(5, НАШ, "М", 700),
                                                  бал(9, "ЧУЖОЙ", "М", 10_000)]}}
    chk("чужие счета того же минта в наш приход не идут",
        купленное_raw(чужой_тоже, НАШ, "М")["raw"] == 700,
        купленное_raw(чужой_тоже, НАШ, "М"))
    два_счёта = {"meta": {"err": None, "preTokenBalances": [бал(5, НАШ, "М", 100)],
                           "postTokenBalances": [бал(5, НАШ, "М", 400),
                                                 бал(7, НАШ, "М", 50)]}}
    chk("своих счетов минта может быть несколько -- складываем",
        купленное_raw(два_счёта, НАШ, "М")["raw"] == 350,
        купленное_raw(два_счёта, НАШ, "М"))
    другой_минт = {"meta": {"err": None, "preTokenBalances": [],
                             "postTokenBalances": [бал(5, НАШ, "ДРУГОЙ", 999)]}}
    chk("минт не тот -- количества нет, а не ноль молчком",
        купленное_raw(другой_минт, НАШ, "М")["ok"] is False
        and купленное_raw(другой_минт, НАШ, "М")["raw"] is None,
        купленное_raw(другой_минт, НАШ, "М"))
    упала = {"meta": {"err": {"InstructionError": [2, {"Custom": 6001}]},
                       "postTokenBalances": [бал(5, НАШ, "М", 1)]}}
    chk("упавшая транзакция: количества нет и chain_ok False",
        купленное_raw(упала, НАШ, "М")["ok"] is False
        and купленное_raw(упала, НАШ, "М")["chain_ok"] is False,
        купленное_raw(упала, НАШ, "М"))
    chk("без meta -- отказ словами, а не ноль",
        купленное_raw({}, НАШ, "М")["ok"] is False
        and "meta" in купленное_raw({}, НАШ, "М")["why_not"],
        купленное_raw({}, НАШ, "М"))
    убыло = {"meta": {"err": None, "preTokenBalances": [бал(5, НАШ, "М", 900)],
                       "postTokenBalances": [бал(5, НАШ, "М", 100)]}}
    chk("остаток уменьшился -- это не покупка, приход не выдаём за неё",
        купленное_raw(убыло, НАШ, "М")["ok"] is False
        and купленное_raw(убыло, НАШ, "М")["raw"] == -800,
        купленное_raw(убыло, НАШ, "М"))
    # СЫРЫЕ, А НЕ ui: у девяти знаков ui округляется в ноль, и по нему
    # продавать было бы нечего.
    девять = {"meta": {"err": None, "preTokenBalances": [],
                        "postTokenBalances": [{"accountIndex": 5, "owner": НАШ,
                                               "mint": "М",
                                               "uiTokenAmount": {"amount": "7",
                                                                 "decimals": 9,
                                                                 "uiAmount": 0.0}}]}}
    chk("считаем сырые единицы, а не ui (в ui это был бы ноль)",
        купленное_raw(девять, НАШ, "М")["raw"] == 7,
        купленное_raw(девять, НАШ, "М"))

    # --- ПРЕДЕЛЫ, РУБИЛЬНИК И БРОНЬ ВНУТРИ отправить(). Пределы, проверенные
    # чистой функцией, не стоят ничего, если гейт не подключён к деньгам:
    # раньше их звал вызывающий, а замок держал только ключ операции, и два
    # сигнала в двух потоках оба видели "открытых ноль".
    import tempfile as _tmp  # noqa: PLC0415

    было_вкл2 = os.environ.get("BLOOM_OWN_SEND")
    было_live2 = os.environ.get("BLOOM_OWN_SEND_LIVE")
    os.environ["BLOOM_OWN_SEND"] = "1"
    os.environ["BLOOM_OWN_SEND_LIVE"] = "1"
    МИНТ_П = "МинтПолосы"
    ЛАМП_П = 10_000_000        # 0.01 SOL -- размер полосы по слову владельца
    try:
        with _tmp.TemporaryDirectory() as врем:
            врем = Path(врем)

            def состояние_для(имя):
                return ST.ExecState(base=врем / имя,
                                    kill=врем / имя / "РУБИЛЬНИКА_НЕТ")

            ушли: list = []

            def сендер(url, данные, таймаут):
                ушли.append(данные)
                return 200, json.dumps({"jsonrpc": "2.0",
                                        "result": "ПОДПИСЬ_ПОЛОСЫ"})

            # 1. БЕЗ СОСТОЯНИЯ -- отказ: ни рубильник, ни пределы проверить нечем.
            забыть_отправленные()
            ушли.clear()
            бс = отправить("AAA", ключ_операции="БС", минт=МИНТ_П,
                           лампорты=ЛАМП_П, отправитель=сендер)
            chk("без состояния отправка отменяется, в сеть не идёт ничего",
                бс["ok"] is False and not ушли
                and "состояние не передано" in (бс["why_not"] or ""), бс)

            # 2. РУБИЛЬНИК. Файл есть -- ни отправки, ни брони.
            забыть_отправленные()
            ушли.clear()
            ск = состояние_для("kill")
            ск.kill_tg_path.write_text("владелец остановил", encoding="utf-8")
            рк = отправить("AAA", состояние=ск, ключ_операции="КИЛЛ", минт=МИНТ_П,
                           лампорты=ЛАМП_П, отправитель=сендер)
            chk("KILL останавливает отправку полосы до сети",
                рк["ok"] is False and рк.get("kill") is True and not ушли, рк)
            chk("при KILL позиция полосы не пишется вовсе",
                ск.lane_positions() == [], ск.lane_positions())

            # 3. ЧЕСТНЫЙ ПУТЬ: бронь на диске ДО сетевого вызова.
            забыть_отправленные()
            ушли.clear()
            сд = состояние_для("ok")
            видно_в_сети: list = []

            def сендер_смотрит(url, данные, таймаут):
                # Мы внутри отправки: позиция обязана быть на диске УЖЕ.
                видно_в_сети.append(len(сд.lane_positions()))
                return сендер(url, данные, таймаут)

            ро = отправить("AAA", состояние=сд, ключ_операции="ОК1", минт=МИНТ_П,
                           лампорты=ЛАМП_П, источник_подпись="ИСТ",
                           источник_слот=777, отправитель=сендер_смотрит)
            chk("отправка полосы с состоянием уходит и даёт подпись",
                ро["ok"] and ро["result"] == "ПОДПИСЬ_ПОЛОСЫ" and len(ушли) == 1, ро)
            chk("позиция полосы лежит на диске ДО сетевого вызова",
                видно_в_сети == [1], видно_в_сети)
            поз_п = (сд.lane_positions() or [{}])[0]
            chk("в позиции полосы метка, минт, размер, режим и срок продажи",
                поз_п.get("lane") == МЕТКА and поз_п.get("mint") == МИНТ_П
                and abs(float(поз_п.get("sol_in") or 0)
                        - ЛАМП_П / ЛАМПОРТОВ_В_SOL) < 1e-12
                and поз_п.get("mode") == ST.MODE_LIVE
                and abs(float(поз_п.get("sell_after_s") or 0)
                        - СРОК_ПРОДАЖИ_S) < 1e-9
                and поз_п.get("state") == "intent"
                and поз_п.get("source_slot") == 777, поз_п)
            chk("cid брони возвращён вызывающему -- иначе ответ сети некуда класть",
                ро.get("cid") == "lane-ОК1", ро.get("cid"))

            # 4. ВТОРАЯ отправка отказана: бронь уже считается открытой.
            р2п = отправить("BBB", состояние=сд, ключ_операции="ОК2",
                            минт="ДругойМинт", лампорты=ЛАМП_П, отправитель=сендер)
            chk("вторая сделка полосы при одной открытой в сеть не идёт",
                р2п["ok"] is False and len(ушли) == 1
                and "предел полосы" in (р2п["why_not"] or "")
                and "открытых" in (р2п["why_not"] or ""), р2п)
            chk("отказ по пределу брони не создаёт",
                len(сд.lane_positions()) == 1, len(сд.lane_positions()))

            # 5. РАЗМЕР И МИНТ. Без минта сторож не найдёт, что продавать; выше
            # потолка полоса перестаёт быть замером.
            забыть_отправленные()
            ушли.clear()
            сн = состояние_для("net")
            бм = отправить("AAA", состояние=сн, ключ_операции="БМ", минт=None,
                           лампорты=ЛАМП_П, отправитель=сендер)
            бл = отправить("AAA", состояние=сн, ключ_операции="БЛ", минт=МИНТ_П,
                           лампорты=0, отправитель=сендер)
            вп = отправить("AAA", состояние=сн, ключ_операции="ВП", минт=МИНТ_П,
                           лампорты=потолок_лампортов() + 1, отправитель=сендер)
            chk("без минта, без размера и выше потолка -- отказ до сети",
                all(р["ok"] is False for р in (бм, бл, вп)) and not ушли
                and "выше потолка" in (вп["why_not"] or ""),
                (бм["why_not"], бл["why_not"], вп["why_not"]))
            chk("ни один такой отказ брони не оставил",
                сн.lane_positions() == [], сн.lane_positions())

            # 6. СЕТЬ УПАЛА ПОСЛЕ БРОНИ -- бронь остаётся: транзакция могла
            # уйти, и сторож обязан о ней знать.
            def падает_после(url, данные, таймаут):
                ушли.append(данные)
                raise TimeoutError("сеть молчит")

            забыть_отправленные()
            ушли.clear()
            сп = состояние_для("fail")
            рф = отправить("AAA", состояние=сп, ключ_операции="ПАД", минт=МИНТ_П,
                           лампорты=ЛАМП_П, отправитель=падает_после)
            chk("сетевая неудача: отказ словами, бронь на диске, ключ занят",
                рф["ok"] is False and len(сп.lane_positions()) == 1
                and (уже_отправляли("ПАД") or {}).get("why_not"),
                (рф["why_not"], len(сп.lane_positions())))

            # 7. ГОНКА ДВУХ ПОТОКОВ. Предел "одна открытая" обязан работать
            # именно тогда, когда сигналов два: это и есть цена ошибки.
            забыть_отправленные()
            ушли.clear()
            сг = состояние_для("race")
            старт = threading.Event()
            итоги: list = []

            def гонщик(н):
                старт.wait(5)
                итоги.append(отправить("AAA", состояние=сг, ключ_операции=f"Г{н}",
                                       минт=f"М{н}", лампорты=ЛАМП_П,
                                       отправитель=сендер))

            потоки = [threading.Thread(target=гонщик, args=(и,)) for и in (1, 2)]
            for т in потоки:
                т.start()
            старт.set()
            for т in потоки:
                т.join(10)
            chk("в гонке двух потоков уходит ровно одна отправка полосы",
                len(ушли) == 1 and sum(1 for р in итоги if р.get("ok")) == 1
                and len(сг.lane_positions()) == 1,
                (len(ушли), [р.get("why_not") for р in итоги]))

            # 8. СУТОЧНЫЙ ПРЕДЕЛ считается по позициям с диска, а не только
            # чистой функцией на выдуманном словаре.
            забыть_отправленные()
            ушли.clear()
            сс = состояние_для("day")
            for и in range(ЛИМИТ_В_СУТКИ):
                сс.write_intent(client_order_id=f"d{и}", mint=f"DM{и}",
                                source_sig="S", source_slot=и, sol_in=0.01,
                                pool=None, program=None, taxed=None, tax_bps=None,
                                mode=ST.MODE_LIVE, sell_after_s=СРОК_ПРОДАЖИ_S,
                                lane=МЕТКА)
                сс.update_position(f"d{и}", state=ST.STATE_CLOSED, chain_ok=True,
                                   closed_sol_net=0.01)
            рс = отправить("AAA", состояние=сс, ключ_операции="СУТ", минт=МИНТ_П,
                           лампорты=ЛАМП_П, отправитель=сендер)
            chk("суточный предел полосы отказывает по позициям с диска",
                рс["ok"] is False and not ушли
                and "за сутки" in (рс["why_not"] or ""), рс)
    finally:
        забыть_отправленные()
        for имя_пер, знач in (("BLOOM_OWN_SEND", было_вкл2),
                              ("BLOOM_OWN_SEND_LIVE", было_live2)):
            if знач is None:
                os.environ.pop(имя_пер, None)
            else:
                os.environ[имя_пер] = знач

    # --- ВЕСЬ ПУТЬ: провести(). Порядок шагов и есть защита денег: сборка
    # без ключа, симуляция вместо отправки без рубильника, подпись только на
    # свежем хеше, отправка только через гейт с бронью. Сборка и подпись тут
    # подменены заглушками: настоящие проверены выше, а ключа на машине
    # самопроверки нет и быть не должно.
    было_вкл3 = os.environ.get("BLOOM_OWN_SEND")
    было_live3 = os.environ.get("BLOOM_OWN_SEND_LIVE")
    было_собрать = globals()["собрать"]
    было_подписать = globals()["подписать"]
    try:
        with _tmp.TemporaryDirectory() as врем3:
            врем3 = Path(врем3)

            def сост3(имя):
                return ST.ExecState(base=врем3 / имя, kill=врем3 / имя / "НЕТ")

            шаги: list = []
            отказ_сборки = {"да": False}

            def сборка_заглушка(**кв):
                шаги.append("собрать")
                if отказ_сборки["да"]:
                    return {"ok": False, "why_not": "тип пула вне полосы: WHIRL",
                            "pool_program": "WHIRL"}
                return {"ok": True, "tx_base64": "СОБРАНО", "pool_program": "PUMP",
                        "min_out": 12345, "expected_out": 23456, "build_ms": 1.0,
                        "size": 700, "tip_account": TIP_ACCOUNTS[0]}

            def подпись_заглушка(tx, **кв):
                шаги.append("подписать")
                return {"ok": True, "signature": "НАША_ПОДПИСЬ",
                        "tx_base64": "ПОДПИСАНО"}

            globals()["собрать"] = сборка_заглушка
            globals()["подписать"] = подпись_заглушка
            ушло3: list = []

            def сендер3(url, данные, таймаут):
                ушло3.append(данные)
                return 200, json.dumps({"jsonrpc": "2.0", "result": "ПОДПИСЬ_СЕТИ"})

            def узел_ок(метод, параметры):
                шаги.append(метод)
                return {"value": {"err": None, "logs": ["Program log: ok"],
                                  "unitsConsumed": 90_000}}

            def провести_на(с, **кв):
                return провести(tx_источника={"meta": {}}, источник="ИСТОЧНИК",
                                минт=МИНТ_П, состояние=с, отправитель=сендер3,
                                rpc_call=узел_ок, **кв)

            # 1. Выключенная полоса не доходит даже до сборки.
            os.environ.pop("BLOOM_OWN_SEND", None)
            os.environ.pop("BLOOM_OWN_SEND_LIVE", None)
            шаги.clear()
            п_выкл = провести_на(сост3("off"), ключ_операции="П1")
            chk("выключенная полоса: стадия off, ни сборки, ни сети",
                п_выкл["stage"] == "off" and not шаги and not ушло3, п_выкл)

            # 2. БЕЗ ЖИВОГО РУБИЛЬНИКА -- только сборка и симуляция.
            os.environ["BLOOM_OWN_SEND"] = "1"
            шаги.clear()
            сс3 = сост3("dry")
            п_сух = провести_на(сс3, ключ_операции="П2")
            chk("без BLOOM_OWN_SEND_LIVE путь кончается симуляцией",
                п_сух["stage"] == "dry" and п_сух["dry"] is True
                and п_сух["ok"] is True
                and п_сух.get("sim_verdict") == "would_pass"
                and "подписать" not in шаги and not ушло3, п_сух)
            chk("в сухом режиме позиция полосы не пишется",
                сс3.lane_positions() == [], сс3.lane_positions())

            # 3. РУБИЛЬНИК -- до сборки: тратить сеть на сборку уже нельзя.
            шаги.clear()
            ск3 = сост3("kill3")
            ск3.kill_tg_path.write_text("стоп", encoding="utf-8")
            п_килл = провести_на(ск3, ключ_операции="П3")
            chk("KILL останавливает путь на гейте, до сборки",
                п_килл["stage"] == "gate" and п_килл.get("kill") is True
                and not шаги, п_килл)

            # 4. Закрытый предел -- тоже до сборки.
            шаги.clear()
            сп3 = сост3("lim3")
            сп3.write_intent(client_order_id="занято", mint="ЧТО-ТО", source_sig="S",
                             source_slot=1, sol_in=0.01, pool=None, program=None,
                             taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                             sell_after_s=СРОК_ПРОДАЖИ_S, lane=МЕТКА)
            п_пред = провести_на(сп3, ключ_операции="П4")
            chk("закрытый предел полосы останавливает путь до сборки",
                п_пред["stage"] == "gate" and not шаги
                and "предел полосы" in (п_пред["why_not"] or ""), п_пред)

            # 5. Чужой тип пула: сборка отказала -- ни подписи, ни позиции.
            отказ_сборки["да"] = True
            шаги.clear()
            сб3 = сост3("nopool")
            п_пул = провести_на(сб3, ключ_операции="П5")
            chk("чужой тип пула: отказ на сборке, подписи нет",
                п_пул["stage"] == "build" and "вне полосы" in (п_пул["why_not"] or "")
                and "подписать" not in шаги and сб3.lane_positions() == [], п_пул)
            отказ_сборки["да"] = False

            # 6. ЖИВОЙ РЕЖИМ БЕЗ ХЕША И С ПРОСРОЧЕННЫМ ХЕШЕМ -- подписи нет.
            os.environ["BLOOM_OWN_SEND_LIVE"] = "1"
            шаги.clear()
            сх3 = сост3("bh")
            п_нет_bh = провести_на(сх3, ключ_операции="П6")
            п_стар = провести_на(сх3, ключ_операции="П7", blockhash="ХЕШ",
                                 blockhash_ts=time.time() - СРОК_BLOCKHASH_S - 5)
            chk("без тёплого хеша и с просроченным хешем подписи нет и сети нет",
                п_нет_bh["stage"] == "blockhash" and п_стар["stage"] == "blockhash"
                and "старше" in (п_стар["why_not"] or "")
                and "подписать" not in шаги and not ушло3, (п_нет_bh, п_стар))
            chk("отказ по хешу брони не оставляет",
                сх3.lane_positions() == [], сх3.lane_positions())

            # 7. ЧЕСТНЫЙ ЖИВОЙ ПУТЬ: одна отправка, позиция bought с подписью.
            забыть_отправленные()
            шаги.clear()
            ушло3.clear()
            сж3 = сост3("live")
            п_жив = провести_на(сж3, ключ_операции="П8", blockhash="ХЕШ",
                                blockhash_ts=time.time(), источник_подпись="ИСТ8",
                                источник_слот=999)
            п_жив_поз = (сж3.lane_positions() or [{}])[0]
            chk("живой путь: подпись, одна отправка, позиция bought с меткой",
                п_жив["ok"] and п_жив["stage"] == "sent" and len(ушло3) == 1
                and п_жив_поз.get("state") == "bought"
                and п_жив_поз.get("lane") == МЕТКА
                and п_жив_поз.get("lane_signature") == "ПОДПИСЬ_СЕТИ"
                and п_жив_поз.get("lane_min_out") == 12345
                and п_жив_поз.get("mint") == МИНТ_П
                and п_жив["cid"] == "lane-П8", (п_жив, п_жив_поз))

            # 8. ОПРЕДЕЛЁННЫЙ ОТКАЗ УЗЛА -- бронь закрывается: в цепи её нет.
            забыть_отправленные()
            ушло3.clear()
            со3 = сост3("refused")

            def сендер_отказ(url, данные, таймаут):
                ушло3.append(данные)
                return 200, json.dumps({"jsonrpc": "2.0",
                                        "error": {"code": -32002,
                                                  "message": "blockhash not found"}})

            п_отк = провести(tx_источника={"meta": {}}, источник="ИСТОЧНИК",
                             минт=МИНТ_П, состояние=со3, отправитель=сендер_отказ,
                             rpc_call=узел_ок, ключ_операции="П9",
                             blockhash="ХЕШ", blockhash_ts=time.time())
            поз_отк = (со3.lane_positions() or [{}])[0]
            chk("определённый отказ узла: бронь закрыта, не висит на стороже",
                п_отк["ok"] is False and п_отк.get("reservation") == "closed"
                and поз_отк.get("state") == ST.STATE_CLOSED
                and поз_отк.get("chain_ok") is False, (п_отк, поз_отк))

            # 9. НЕОПРЕДЕЛЁННАЯ НЕУДАЧА -- бронь ОСТАЁТСЯ: транзакция могла
            # уйти, и сторож обязан о ней знать.
            забыть_отправленные()
            ушло3.clear()
            сн3 = сост3("ambig")

            def сендер_молчит(url, данные, таймаут):
                ушло3.append(данные)
                raise TimeoutError("сеть молчит")

            п_нео = провести(tx_источника={"meta": {}}, источник="ИСТОЧНИК",
                             минт=МИНТ_П, состояние=сн3, отправитель=сендер_молчит,
                             rpc_call=узел_ок, ключ_операции="П10",
                             blockhash="ХЕШ", blockhash_ts=time.time())
            поз_нео = (сн3.lane_positions() or [{}])[0]
            chk("неопределённая неудача: бронь открыта и помечена",
                п_нео["ok"] is False and п_нео.get("reservation") == "open_ambiguous"
                and поз_нео.get("state") == "intent"
                and поз_нео.get("lane_send_ambiguous") is True
                and поз_нео.get("lane_signature_local") == "НАША_ПОДПИСЬ",
                (п_нео, поз_нео))
            # 11. СИМУЛЯЦИЯ -- ГЕЙТ ПЕРЕД ОТПРАВКОЙ (слово владельца 25.09).
            # Упала симуляция -- денег нет: ни подписи, ни отправки, ни брони.
            забыть_отправленные()
            шаги.clear()
            ушло3.clear()
            сим = сост3("simfail")

            def узел_падение(метод, параметры):
                шаги.append(метод)
                return {"value": {"err": {"InstructionError": [3, {"Custom": 6001}]},
                                  "logs": ["Program log: slippage exceeded"],
                                  "unitsConsumed": 40_000}}

            п_сим = провести(tx_источника={"meta": {}}, источник="ИСТОЧНИК",
                             минт=МИНТ_П, состояние=сим, отправитель=сендер3,
                             rpc_call=узел_падение, ключ_операции="П11",
                             blockhash="ХЕШ", blockhash_ts=time.time())
            chk("симуляция не прошла: SKIP_SIM_FAIL, ни подписи, ни отправки, ни брони",
                п_сим["ok"] is False and п_сим["stage"] == "sim"
                and п_сим.get("code") == "SKIP_SIM_FAIL"
                and "подписать" not in шаги and not ушло3
                and сим.lane_positions() == [], (п_сим, шаги))

            # 12. НЕТ УЗЛА -- НЕТ СИМУЛЯЦИИ -- НЕТ ОТПРАВКИ.
            забыть_отправленные()
            шаги.clear()
            без_узла = сост3("nonode")
            п_бу = провести(tx_источника={"meta": {}}, источник="ИСТОЧНИК",
                            минт=МИНТ_П, состояние=без_узла, отправитель=сендер3,
                            rpc_call=None, ключ_операции="П12",
                            blockhash="ХЕШ", blockhash_ts=time.time())
            chk("без узла симуляции нет, значит и отправки нет",
                п_бу["ok"] is False and п_бу.get("code") == "SKIP_SIM_FAIL"
                and "подписать" not in шаги and not ушло3, п_бу)

            # 13. УПАЛ САМ ВЫЗОВ СИМУЛЯЦИИ -- тоже SKIP_SIM_FAIL, а не отправка.
            забыть_отправленные()
            шаги.clear()
            сбой = сост3("simcrash")

            def узел_рвётся(метод, параметры):
                шаги.append(метод)
                raise TimeoutError("узел молчит")

            п_сб = провести(tx_источника={"meta": {}}, источник="ИСТОЧНИК",
                            минт=МИНТ_П, состояние=сбой, отправитель=сендер3,
                            rpc_call=узел_рвётся, ключ_операции="П13",
                            blockhash="ХЕШ", blockhash_ts=time.time())
            chk("обрыв узла на симуляции -- отказ, а не отправка вслепую",
                п_сб["ok"] is False and п_сб.get("code") == "SKIP_SIM_FAIL"
                and "TimeoutError" in (п_сб["why_not"] or "")
                and not ушло3, п_сб)

            # 14. СИМУЛЯЦИЯ ИДЁТ ДО ПОДПИСИ, а не после: ключ не трогаем,
            # пока не знаем, что покупка вообще проходит.
            забыть_отправленные()
            шаги.clear()
            ушло3.clear()
            порядок = сост3("order")
            п_пор = провести(tx_источника={"meta": {}}, источник="ИСТОЧНИК",
                             минт=МИНТ_П, состояние=порядок, отправитель=сендер3,
                             rpc_call=узел_ок, ключ_операции="П14",
                             blockhash="ХЕШ", blockhash_ts=time.time())
            chk("порядок шагов: сборка, симуляция, подпись, отправка",
                п_пор["ok"] and шаги == ["собрать", "simulateTransaction", "подписать"]
                and len(ушло3) == 1 and п_пор.get("sim_ms") is not None,
                (шаги, п_пор.get("sim_ms")))
    finally:
        globals()["собрать"] = было_собрать
        globals()["подписать"] = было_подписать
        забыть_отправленные()
        for имя_пер, знач in (("BLOOM_OWN_SEND", было_вкл3),
                              ("BLOOM_OWN_SEND_LIVE", было_live3)):
            if знач is None:
                os.environ.pop(имя_пер, None)
            else:
                os.environ[имя_пер] = знач

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
