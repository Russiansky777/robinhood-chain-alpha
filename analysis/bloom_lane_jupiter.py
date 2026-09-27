#!/usr/bin/env python3
"""Покупка ПОЛОСЫ через Jupiter, когда котировка пула не в SOL.

ЗАЧЕМ. Прямой сборщик полосы берёт только одношаговые пулы с котировкой SOL:
на всём остальном он честно отказывает строкой "котировка пула не SOL -- полоса
только одношаговая". Сигналов лидера с котировкой USDC это лишает нас целиком.
Слово владельца 27.09 (вне очереди): "Путь через Jupiter (как у сторожа на
продаже, тот же клиент, тёплое соединение): котировка SOL -> токен по минту с
exactIn, затем swap-транзакция Jupiter, подпись нашим ключом, отправка нашим
пулом отправителей (тем же, что у прямой сборки). Минимум выхода -- от
котировки Jupiter с проскальзыванием группы."

ПОЧЕМУ ТРАНЗАКЦИЮ СОБИРАЕМ МЫ, А НЕ БЕРЁМ ГОТОВУЮ. Отправка идёт нашим пулом
отправителей, а каждый сервис пула смотрит ТОЛЬКО свои чаевые и ниже своего
минимума молча отбрасывает транзакцию. В готовой транзакции Jupiter наших
чаевых нет -- её бы просто никто не повёз. Поэтому от Jupiter берутся
ИНСТРУКЦИИ (/swap/v1/swap-instructions), а транзакцию собираем сами: наш
приоритет, наши чаевые каждому сервису, наш blockhash или долговечный nonce --
ровно как в прямой сборке.

ЧТО ИЗМЕРЕНО НА ЖИВОМ API С NL-ХОСТА 27.09 (data/jup_buy_probe.json), а не
взято из памяти:
  * /swap/v1/quote с swapMode=ExactIn отдаёт inAmount, outAmount и
    otherAmountThreshold -- последнее и есть минимум выхода при покупке;
  * /swap/v1/swap-instructions отдаёт computeBudgetInstructions (2),
    setupInstructions (4), swapInstruction (программа JUP6Lkb..., 20 счетов),
    cleanupInstruction (закрытие WSOL) и addressLookupTableAddresses (1);
  * котировка по тёплому соединению 15.96 мс (медиана 8 замеров), по новому
    43.49 мс -- то есть тёплая сессия экономит около 28 мс на сигнал.

КЛИЕНТ ОДИН. Сессия с пулом соединений живёт в dbot_rescue (тот же клиент, что
у сторожа на продаже), и второй копии здесь нет: два клиента однажды разойдутся
по имени поля порога, а поле порога -- это наш минимум выхода.

Ни одной отправки в этом модуле. Он даёт котировку, инструкции и СОБРАННУЮ
неподписанную транзакцию; подпись и отправка остаются в bloom_own_send, где
стоят рубильник, пределы и бронь.
"""
from __future__ import annotations

import base64
import os
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

WSOL = "So11111111111111111111111111111111111111112"
ЛАМПОРТОВ_В_SOL = 1_000_000_000
# Программа маршрутизатора Jupiter. Проверяется у полученной инструкции свопа:
# чужая программа в денежном пути -- отказ, а не "наверное, они поменяли".
ПРОГРАММА_JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
# Базы Jupiter. lite-api ключа не требует, api.jup.ag читает x-api-key. Порядок
# -- как в замере 27.09: по тёплому соединению обе около 16 мс, но lite-api не
# зависит от ключа, поэтому она первая.
БАЗЫ_ПО_УМОЛЧАНИЮ = ("https://lite-api.jup.ag/swap/v1",
                      "https://api.jup.ag/swap/v1")
ENV_БАЗА = "BLOOM_JUP_SWAP_BASE"
# Сколько ждём котировку и инструкции. Это горячий путь: лучше отказаться от
# сигнала, чем стоять. Значения -- отдельными переменными, чтобы их можно было
# ужать, не правя код.
ТАЙМАУТ_КОТИРОВКИ_S = float(os.environ.get("BLOOM_JUP_QUOTE_TIMEOUT_S", "1.5") or 1.5)
ТАЙМАУТ_ИНСТРУКЦИЙ_S = float(os.environ.get("BLOOM_JUP_IX_TIMEOUT_S", "2.5") or 2.5)


def базы() -> tuple:
    своя = (os.environ.get(ENV_БАЗА) or "").strip()
    return (своя,) if своя else БАЗЫ_ПО_УМОЛЧАНИЮ


def _сессия():
    """Тёплая сессия Jupiter -- ОДНА на процесс, из dbot_rescue."""
    from dbot_rescue import сессия  # noqa: PLC0415

    return сессия()


def _заголовки(база: str) -> dict:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    ключ = (os.environ.get("JUPITER_API_KEY") or "").strip()
    if ключ and "lite-api" not in база:
        h["x-api-key"] = ключ
    return h


def проскальзывание_в_bps(доля: float) -> int:
    """0.35 -> 3500. Одна формула на весь модуль: два перевода однажды разойдутся."""
    д = max(0.0, min(1.0, float(доля)))
    return int(round(д * 10_000))


# ------------------------------------------------------------------ котировка

def котировка(минт: str, лампорты: int, *, проскальзывание: float = 0.35,
               таймаут: float | None = None) -> dict:
    """Котировка SOL -> минт с exactIn. Только чтение, подпись не нужна.

    Возвращает ответ Jupiter целиком (он нужен для /swap-instructions) плюс
    разобранные числа и время. Ошибка -- словами и с именем базы: "не дал
    котировку" без имени базы уже стоило прогона.
    """
    из_ = {"ok": False, "why_not": None, "quote_ms": None, "база": None}
    if not минт or минт == WSOL:
        из_["why_not"] = f"минт для покупки негодный: {минт!r}"
        return из_
    if not isinstance(лампорты, int) or лампорты <= 0:
        из_["why_not"] = f"размер не положительное целое: {лампорты!r}"
        return из_
    bps = проскальзывание_в_bps(проскальзывание)
    отказы = []
    t0 = time.perf_counter()
    for база in базы():
        пар = {"inputMint": WSOL, "outputMint": минт, "amount": str(лампорты),
               "slippageBps": str(bps), "swapMode": "ExactIn"}
        try:
            r = _сессия().get(база + "/quote", params=пар,
                               headers=_заголовки(база),
                               timeout=(таймаут or ТАЙМАУТ_КОТИРОВКИ_S))
        except Exception as exc:  # noqa: BLE001
            отказы.append(f"{база}: сеть {type(exc).__name__}")
            continue
        if r.status_code != 200:
            отказы.append(f"{база}: http={r.status_code}")
            continue
        try:
            т = r.json()
        except ValueError:
            отказы.append(f"{база}: не JSON")
            continue
        если_ошибка = т.get("error") or t_код(т)
        if если_ошибка:
            отказы.append(f"{база}: {если_ошибка}")
            continue
        из_["quote_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        из_["база"] = база
        из_["quote"] = т
        из_.update(ok=True,
                   in_amount=_цел(т.get("inAmount")),
                   out_amount=_цел(т.get("outAmount")),
                   min_out=_цел(т.get("otherAmountThreshold")),
                   swap_mode=т.get("swapMode"),
                   slippage_bps=_цел(т.get("slippageBps")),
                   context_slot=_цел(т.get("contextSlot")),
                   price_impact_pct=т.get("priceImpactPct"),
                   route=" -> ".join(
                       str((rp.get("swapInfo") or {}).get("label"))
                       for rp in (т.get("routePlan") or [])) or None,
                   route_steps=len(т.get("routePlan") or []))
        return из_
    из_["quote_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    из_["why_not"] = "котировку не дал ни один хост Jupiter: " + "; ".join(отказы)
    return из_


def t_код(т: dict):
    return т.get("errorCode") or т.get("errorMessage")


def _цел(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def проверить_котировку(к: dict, *, лампорты: int,
                         проскальзывание: float) -> dict:
    """Можно ли по этой котировке собирать покупку. Денежная проверка.

    Три условия, и каждое -- про деньги:
      1. режим ExactIn: иначе otherAmountThreshold означает не минимум выхода;
      2. вход котировки РОВНО наш: если Jupiter посчитал другую сумму, мы
         потратим не то, что решили;
      3. минимум выхода положителен и не ниже котировки с нашим
         проскальзыванием: ноль или заниженный порог -- это покупка по любой
         цене.
    Неизвестное число -- отказ: в сторону денег неизвестность не трактуется.
    """
    из_ = {"ok": False, "why_not": None, "checks": {}}
    if (к.get("swap_mode") or "ExactIn") != "ExactIn":
        из_["why_not"] = (f"swapMode={к.get('swap_mode')}: порог означает не "
                           "минимум выхода")
        return из_
    вход, выход, порог = к.get("in_amount"), к.get("out_amount"), к.get("min_out")
    из_["checks"].update(in_amount=вход, out_amount=выход, min_out=порог,
                         lamports=лампорты)
    if вход is None or выход is None or порог is None:
        из_["why_not"] = ("в котировке нет inAmount/outAmount/"
                           "otherAmountThreshold -- собирать нечего")
        return из_
    if вход != int(лампорты):
        из_["why_not"] = (f"котировка на {вход} лампортов, а решили тратить "
                           f"{лампорты} -- расхождение входа")
        return из_
    if порог <= 0:
        из_["why_not"] = f"минимум выхода не положителен: {порог}"
        return из_
    нужный = int(выход * (1.0 - max(0.0, min(1.0, float(проскальзывание)))))
    из_["checks"]["floor_needed"] = нужный
    if порог < нужный:
        из_["why_not"] = (f"минимум выхода {порог} ниже пола {нужный} "
                           f"(котировка {выход} с проскальзыванием "
                           f"{проскальзывание_в_bps(проскальзывание)} bps)")
        return из_
    из_["ok"] = True
    return из_


# ------------------------------------------------------------------ инструкции

def инструкции(ответ_котировки: dict, кошелёк: str, *,
                таймаут: float | None = None, база: str | None = None) -> dict:
    """Инструкции свопа от Jupiter. Транзакцию из них собираем мы.

    wrapAndUnwrapSol=True: вход у нас нативный SOL, и обёртку делает Jupiter
    своими setup-инструкциями -- в прямой сборке это делает наш сборщик, здесь
    делает он. useSharedAccounts=False: общие счета Jupiter экономят место, но
    на свежих минтах именно они чаще всего и отказывают.
    dynamicComputeUnitLimit=False: предел вычислений ставим свой, как в прямой
    сборке, иначе приоритет считался бы от чужого числа.
    """
    из_ = {"ok": False, "why_not": None, "ix_ms": None, "база": None}
    if not кошелёк:
        из_["why_not"] = "кошелёк не передан -- инструкции просить не на кого"
        return из_
    тело = {"userPublicKey": кошелёк, "quoteResponse": ответ_котировки,
            "wrapAndUnwrapSol": True, "useSharedAccounts": False,
            "dynamicComputeUnitLimit": False,
            "skipUserAccountsRpcCalls": False}
    отказы = []
    t0 = time.perf_counter()
    for б in ((база,) if база else базы()):
        try:
            r = _сессия().post(б + "/swap-instructions", json=тело,
                                headers=_заголовки(б),
                                timeout=(таймаут or ТАЙМАУТ_ИНСТРУКЦИЙ_S))
        except Exception as exc:  # noqa: BLE001
            отказы.append(f"{б}: сеть {type(exc).__name__}")
            continue
        if r.status_code != 200:
            отказы.append(f"{б}: http={r.status_code}")
            continue
        try:
            т = r.json()
        except ValueError:
            отказы.append(f"{б}: не JSON")
            continue
        if т.get("error") or т.get("simulationError"):
            отказы.append(f"{б}: {т.get('error') or т.get('simulationError')}")
            continue
        if not т.get("swapInstruction"):
            отказы.append(f"{б}: в ответе нет swapInstruction")
            continue
        из_.update(ok=True, база=б, ответ=т,
                   ix_ms=round((time.perf_counter() - t0) * 1000, 2),
                   таблицы=list(т.get("addressLookupTableAddresses") or []),
                   cu_limit_jupiter=_цел(т.get("computeUnitLimit")),
                   setup_n=len(т.get("setupInstructions") or []),
                   cleanup_есть=bool(т.get("cleanupInstruction")))
        return из_
    из_["ix_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    из_["why_not"] = "инструкций не дал ни один хост Jupiter: " + "; ".join(отказы)
    return из_


# ------------------------------------------------- таблицы адресов (с кешем)

_ТАБЛИЦЫ: dict = {}
_ЗАМОК_ТАБЛИЦ = threading.Lock()


def таблицы_адресов(rpc_call, адреса: list) -> dict:
    """AddressLookupTableAccount по адресам. Кеш на процесс.

    Таблицы Jupiter меняются редко, а каждый круг до узла -- это миллисекунды
    на горячем пути. Поэтому загруженная таблица остаётся в памяти; чего нет в
    кеше, спрашивается ОДНИМ getMultipleAccounts, а не по одной.
    """
    из_ = {"ok": False, "why_not": None, "accounts": [], "из_кеша": 0,
            "спрошено": 0, "lut_ms": None}
    нужно = [а for а in (адреса or []) if а]
    if not нужно:
        из_.update(ok=True)
        return из_
    t0 = time.perf_counter()
    with _ЗАМОК_ТАБЛИЦ:
        нет = [а for а in нужно if а not in _ТАБЛИЦЫ]
        из_["из_кеша"] = len(нужно) - len(нет)
    if нет:
        if rpc_call is None:
            из_["why_not"] = ("таблиц адресов нет в кеше, а узел не передан: "
                              + ", ".join(а[:10] for а in нет))
            return из_
        из_["спрошено"] = len(нет)
        try:
            отв = rpc_call("getMultipleAccounts",
                           [нет, {"encoding": "jsonParsed"}]) or {}
            значения = отв.get("value") or []
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = f"таблицы адресов не прочитаны: {type(exc).__name__}"
            return из_
        try:
            from solders.address_lookup_table_account import (  # noqa: PLC0415
                AddressLookupTableAccount)
            from solders.pubkey import Pubkey  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = f"solders не загружен: {type(exc).__name__}"
            return из_
        for адрес, зн in zip(нет, значения):
            адр_список = ((((зн or {}).get("data") or {}).get("parsed") or {})
                          .get("info") or {}).get("addresses") or []
            if not адр_список:
                из_["why_not"] = (f"таблица {адрес[:10]} пустая или не разобралась "
                                  "-- собирать нельзя, счета сдвинутся")
                return из_
            with _ЗАМОК_ТАБЛИЦ:
                _ТАБЛИЦЫ[адрес] = AddressLookupTableAccount(
                    Pubkey.from_string(адрес),
                    [Pubkey.from_string(а) for а in адр_список])
    with _ЗАМОК_ТАБЛИЦ:
        из_["accounts"] = [_ТАБЛИЦЫ[а] for а in нужно if а in _ТАБЛИЦЫ]
    из_["lut_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    из_["ok"] = len(из_["accounts"]) == len(нужно)
    if not из_["ok"]:
        из_["why_not"] = "не все таблицы адресов загружены"
    return из_


def забыть_таблицы() -> None:
    """Сброс кеша таблиц -- для самопроверки и для разбора после сбоя."""
    with _ЗАМОК_ТАБЛИЦ:
        _ТАБЛИЦЫ.clear()


# ------------------------------------------------------------------ сборка

def _инструкция(з: dict):
    """JSON-инструкция Jupiter -> solders.Instruction."""
    from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415

    счета = [AccountMeta(Pubkey.from_string(с["pubkey"]),
                          bool(с.get("isSigner")), bool(с.get("isWritable")))
             for с in (з.get("accounts") or [])]
    return Instruction(Pubkey.from_string(з["programId"]),
                        base64.b64decode(з.get("data") or ""), счета)


def _в_байтах(сырое: bytes, число, размер: int) -> bool:
    try:
        return int(число).to_bytes(размер, "little") in сырое
    except (TypeError, ValueError, OverflowError):
        return False


def что_в_байтах(данные: bytes, *, порог, котировка_out, slippage_bps) -> dict:
    """Что из заявленного реально лежит в данных инструкции свопа.

    Та же честность, что у продажи: маршрутизатор Jupiter кодирует котировку и
    проскальзывание, а минимум выхода считает на цепи как котировка*(1-slip).
    Поэтому самого порога в байтах может не быть, и это не признак подлога --
    но сказать надо ровно то, что нашлось, и ничего не выдавать за полный
    разбор маршрута.
    """
    из_ = {"threshold_found": _в_байтах(данные, порог, 8),
            "quote_found": _в_байтах(данные, котировка_out, 8),
            "slippage_found": any(_в_байтах(данные, slippage_bps, n)
                                   for n in (2, 4))}
    if из_["threshold_found"]:
        из_["note"] = "минимум выхода лежит в данных инструкции прямым числом"
    elif из_["quote_found"] and из_["slippage_found"]:
        из_["note"] = ("минимума прямым числом нет, но в данных есть котировка и "
                        "наше проскальзывание: на цепи минимум считается из них")
    else:
        из_["note"] = ("ни минимума, ни пары котировка+проскальзывание в данных "
                        "не нашлось: проверка согласованности НЕ пройдена")
    return из_


def собрать(*, ответ_инструкций: dict, котировка_разбор: dict, наш_кошелёк: str,
             лампорты: int, таблицы: list, cu_units: int = 400_000,
             cu_price_micro: int = 0, чаевые: list | None = None,
             нонс: tuple | None = None,
             белый_список_чаевых=None) -> dict:
    """Наша транзакция из инструкций Jupiter. Неподписанная.

    Порядок инструкций: [advance_nonce] -> предел вычислений -> цена вычислений
    -> setup Jupiter (обёртка SOL, создание счетов) -> своп Jupiter -> cleanup
    Jupiter -> чаевые. Compute budget от Jupiter НЕ берётся: приоритет наш, и
    считать его от чужого числа нельзя.

    Чаевые -- ПОСЛЕДНИМИ и только на адреса из белого списка: получатель не из
    списка это утечка, и решается она отказом, а не предупреждением.
    """
    из_ = {"ok": False, "why_not": None, "build_ms": None}
    t0 = time.perf_counter()
    if not isinstance(лампорты, int) or лампорты <= 0:
        из_["why_not"] = f"размер не положительное целое: {лампорты!r}"
        return из_
    if not наш_кошелёк:
        из_["why_not"] = "кошелёк сборки не задан"
        return из_
    т = ответ_инструкций or {}
    своп = т.get("swapInstruction")
    if not своп:
        из_["why_not"] = "в ответе нет swapInstruction -- собирать нечего"
        return из_
    if своп.get("programId") != ПРОГРАММА_JUPITER:
        из_["why_not"] = (f"инструкция свопа не от Jupiter: {своп.get('programId')} "
                           f"вместо {ПРОГРАММА_JUPITER}")
        return из_
    try:
        from solders.hash import Hash  # noqa: PLC0415
        from solders.message import MessageV0  # noqa: PLC0415
        from solders.pubkey import Pubkey  # noqa: PLC0415
        from solders.signature import Signature  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415

        import c2_swap_build as B  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"модули сборки не загружены: {type(exc).__name__}"
        return из_

    ixs = []
    if нонс:
        ixs.append(B.advance_nonce(str(нонс[0]), наш_кошелёк))
    ixs.append(B.cu_limit(int(cu_units)))
    ixs.append(B.cu_price(int(cu_price_micro)))
    try:
        for з in (т.get("setupInstructions") or []):
            ixs.append(_инструкция(з))
        ixs.append(_инструкция(своп))
        if т.get("cleanupInstruction"):
            ixs.append(_инструкция(т["cleanupInstruction"]))
        for з in (т.get("otherInstructions") or []):
            ixs.append(_инструкция(з))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = (f"инструкция Jupiter не разобралась: "
                           f"{type(exc).__name__}: {str(exc)[:120]}")
        return из_

    сумма_чаевых = 0
    if чаевые:
        for адрес_ч, лам_ч in чаевые:
            if not адрес_ч or int(лам_ч) <= 0:
                из_["why_not"] = "в списке чаевых пустой адрес или ноль -- отказ"
                return из_
            if белый_список_чаевых is not None and адрес_ч not in белый_список_чаевых:
                из_["why_not"] = ("адрес чаевых не из списка счетов Sender -- "
                                   "сборка отменена")
                return из_
            ixs.append(B.sol_transfer(наш_кошелёк, адрес_ч, int(лам_ч)))
            сумма_чаевых += int(лам_ч)

    try:
        msg = MessageV0.try_compile(Pubkey.from_string(наш_кошелёк), ixs,
                                    list(таблицы or []), Hash.default())
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = (f"сообщение не скомпилировалось: {type(exc).__name__}: "
                           f"{str(exc)[:120]}")
        return из_
    n_подписей = msg.header.num_required_signatures
    if n_подписей != 1:
        # Наш кошелёк один, и подписать он может только за себя. Требование
        # второй подписи означает, что в транзакции чужой подписант -- такую
        # сеть не примет, а приоритет и чаевые уже были бы потрачены.
        из_["why_not"] = (f"транзакция требует {n_подписей} подписей, а у нас "
                           "один кошелёк -- отправлять нельзя")
        return из_
    if str(msg.account_keys[0]) != наш_кошелёк:
        из_["why_not"] = (f"плательщик в сообщении {str(msg.account_keys[0])[:12]}, "
                           f"а должен быть наш {наш_кошелёк[:12]}")
        return из_
    vtx = VersionedTransaction.populate(msg, [Signature.default()] * n_подписей)
    сырое = bytes(vtx)
    из_.update(ok=True, tx_base64=base64.b64encode(сырое).decode(),
               size=len(сырое), n_instructions=len(ixs),
               tips_total_lamports=сумма_чаевых,
               min_out=котировка_разбор.get("min_out"),
               expected_out=котировка_разбор.get("out_amount"),
               in_tx=что_в_байтах(base64.b64decode(своп.get("data") or ""),
                                   порог=котировка_разбор.get("min_out"),
                                   котировка_out=котировка_разбор.get("out_amount"),
                                   slippage_bps=котировка_разбор.get("slippage_bps")),
               build_ms=round((time.perf_counter() - t0) * 1000, 3))
    return из_


# ------------------------------------------------------------------ самопроверка

def self_test() -> int:
    """Самопроверка ДЕНЕЖНОГО ПУТИ: ключ не трогаем, сеть не трогаем.

    Проверяется то, от чего зависят деньги: сумма входа, минимум выхода,
    получатели чаевых, плательщик и число подписей, чужая программа свопа.
    """
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя}" + (f" -- {факт}" if факт is not None else ""))

    # --- перевод проскальзывания
    chk("0.35 -> 3500 bps", проскальзывание_в_bps(0.35) == 3500,
        проскальзывание_в_bps(0.35))
    chk("проскальзывание выше единицы в bps не разгоняется",
        проскальзывание_в_bps(9.9) == 10_000, проскальзывание_в_bps(9.9))

    # --- ПРОВЕРКА КОТИРОВКИ. Числа взяты из живого ответа 27.09
    # (data/jup_buy_probe.json): вход 10000000, выход 1238608, порог 805096
    # при проскальзывании 3500 bps.
    живая = {"swap_mode": "ExactIn", "in_amount": 10_000_000,
             "out_amount": 1_238_608, "min_out": 805_096, "slippage_bps": 3500}
    п = проверить_котировку(живая, лампорты=10_000_000, проскальзывание=0.35)
    chk("живая котировка 27.09 проходит проверку пола", п["ok"] is True, п)
    chk("пол посчитан от котировки, а не взят на слово",
        п["checks"]["floor_needed"] == int(1_238_608 * 0.65),
        п["checks"]["floor_needed"])
    chk("вход не тот -- отказ (потратили бы не то, что решили)",
        проверить_котировку(живая, лампорты=20_000_000,
                             проскальзывание=0.35)["ok"] is False, "")
    chk("и в причине названы оба числа входа",
        "20000000" in (проверить_котировку(живая, лампорты=20_000_000,
                                            проскальзывание=0.35)["why_not"] or ""),
        "")
    низкий = dict(живая, min_out=1)
    chk("порог ниже пола -- отказ",
        проверить_котировку(низкий, лампорты=10_000_000,
                             проскальзывание=0.35)["ok"] is False, "")
    chk("порог ноль -- отказ, а не покупка по любой цене",
        проверить_котировку(dict(живая, min_out=0), лампорты=10_000_000,
                             проскальзывание=0.35)["ok"] is False, "")
    chk("ExactOut -- отказ: порог значит не минимум выхода",
        проверить_котировку(dict(живая, swap_mode="ExactOut"),
                             лампорты=10_000_000,
                             проскальзывание=0.35)["ok"] is False, "")
    chk("нет числа в котировке -- отказ, а не ноль",
        проверить_котировку(dict(живая, min_out=None), лампорты=10_000_000,
                             проскальзывание=0.35)["ok"] is False, "")
    chk("проскальзывание шире -- тот же порог проходит",
        проверить_котировку(низкий, лампорты=10_000_000,
                             проскальзывание=1.0)["ok"] is True, "")

    # --- СБОРКА. Инструкции подставные, но структура ровно как у живого
    # ответа: setup + swap (программа Jupiter) + cleanup.
    КОШ = "11111111111111111111111111111112"
    ЧАЕВЫЕ_ОК = "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"
    ЧУЖОЙ = "9999999999999999999999999999999999999999999"
    СИСТЕМА = "11111111111111111111111111111111"

    def ix(программа, данные=b"\x00", счета=()):
        return {"programId": программа,
                "data": base64.b64encode(данные).decode(),
                "accounts": [{"pubkey": с, "isSigner": False, "isWritable": True}
                             for с in счета]}

    данные_свопа = (b"\xe5\x17\xcb\x97\x7a\xe3\xad\x2a"
                    + struct.pack("<Q", 1_238_608)
                    + struct.pack("<H", 3500))
    ответ = {"setupInstructions": [ix(СИСТЕМА, b"\x01")],
             "swapInstruction": ix(ПРОГРАММА_JUPITER, данные_свопа, (КОШ,)),
             "cleanupInstruction": ix("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                                       b"\x09", (КОШ,)),
             "otherInstructions": [],
             "addressLookupTableAddresses": []}
    сб = собрать(ответ_инструкций=ответ, котировка_разбор=живая,
                  наш_кошелёк=КОШ, лампорты=10_000_000, таблицы=[],
                  cu_price_micro=2500,
                  чаевые=[(ЧАЕВЫЕ_ОК, 1_000_000)],
                  белый_список_чаевых={ЧАЕВЫЕ_ОК})
    chk("транзакция собралась и она непустая",
        сб["ok"] is True and сб.get("size", 0) > 100, сб.get("why_not"))
    chk("плательщик -- наш кошелёк, подпись одна",
        сб["ok"] is True, сб.get("why_not"))
    chk("минимум выхода из котировки попал в отчёт сборки",
        сб.get("min_out") == 805_096, сб.get("min_out"))
    chk("сумма чаевых посчитана",
        сб.get("tips_total_lamports") == 1_000_000, сб.get("tips_total_lamports"))
    chk("в данных инструкции свопа найдены котировка и проскальзывание",
        сб["in_tx"]["quote_found"] is True
        and сб["in_tx"]["slippage_found"] is True, сб.get("in_tx"))
    chk("compute budget берётся НАШ: в сборке ровно две наши инструкции сверх Jupiter",
        сб.get("n_instructions") == 2 + 1 + 1 + 1 + 1, сб.get("n_instructions"))

    # ЧУЖОЙ ПОЛУЧАТЕЛЬ ЧАЕВЫХ -- ОТКАЗ.
    сб_ч = собрать(ответ_инструкций=ответ, котировка_разбор=живая,
                    наш_кошелёк=КОШ, лампорты=10_000_000, таблицы=[],
                    чаевые=[(ЧУЖОЙ, 1_000_000)],
                    белый_список_чаевых={ЧАЕВЫЕ_ОК})
    chk("чаевые не из белого списка -- отказ сборки",
        сб_ч["ok"] is False and "не из списка" in (сб_ч["why_not"] or ""),
        сб_ч.get("why_not"))
    chk("ноль в чаевых -- отказ",
        собрать(ответ_инструкций=ответ, котировка_разбор=живая, наш_кошелёк=КОШ,
                 лампорты=10_000_000, таблицы=[], чаевые=[(ЧАЕВЫЕ_ОК, 0)],
                 белый_список_чаевых={ЧАЕВЫЕ_ОК})["ok"] is False, "")

    # ЧУЖАЯ ПРОГРАММА СВОПА -- ОТКАЗ.
    ответ_чужой = dict(ответ, swapInstruction=ix(СИСТЕМА, данные_свопа, (КОШ,)))
    сб_п = собрать(ответ_инструкций=ответ_чужой, котировка_разбор=живая,
                    наш_кошелёк=КОШ, лампорты=10_000_000, таблицы=[])
    chk("инструкция свопа не от Jupiter -- отказ",
        сб_п["ok"] is False and "не от Jupiter" in (сб_п["why_not"] or ""),
        сб_п.get("why_not"))
    chk("нет swapInstruction -- отказ словами",
        собрать(ответ_инструкций={}, котировка_разбор=живая, наш_кошелёк=КОШ,
                 лампорты=10_000_000, таблицы=[])["ok"] is False, "")
    chk("размер не положителен -- отказ до всякой сборки",
        собрать(ответ_инструкций=ответ, котировка_разбор=живая, наш_кошелёк=КОШ,
                 лампорты=0, таблицы=[])["ok"] is False, "")

    # ЧУЖОЙ ПОДПИСАНТ В ИНСТРУКЦИИ -- ОТКАЗ (вторая подпись).
    ответ_подп = dict(ответ, setupInstructions=[{
        "programId": СИСТЕМА, "data": base64.b64encode(b"\x01").decode(),
        "accounts": [{"pubkey": ЧУЖОЙ, "isSigner": True, "isWritable": True}]}])
    сб_подп = собрать(ответ_инструкций=ответ_подп, котировка_разбор=живая,
                       наш_кошелёк=КОШ, лампорты=10_000_000, таблицы=[])
    chk("чужой подписант в инструкциях -- отказ, а не отправка",
        сб_подп["ok"] is False and "подписей" in (сб_подп["why_not"] or ""),
        сб_подп.get("why_not"))

    # --- ТАБЛИЦЫ АДРЕСОВ: без узла и без кеша -- отказ, а не пустой список.
    забыть_таблицы()
    т_нет = таблицы_адресов(None, ["RWqRxLuWzhdqMttMmQQgTrGscqFrGNcy9RLzJYgj2Ui"])
    chk("таблицы нет ни в кеше, ни узла -- отказ словами",
        т_нет["ok"] is False and "узел не передан" in (т_нет["why_not"] or ""),
        т_нет.get("why_not"))
    chk("пустой список таблиц -- это не отказ",
        таблицы_адресов(None, [])["ok"] is True, "")

    def узел_с_таблицей(метод, параметры):
        assert метод == "getMultipleAccounts"
        return {"value": [{"data": {"parsed": {"info": {
            "addresses": ["11111111111111111111111111111111",
                          "So11111111111111111111111111111111111111112"]}}}}]}

    т_да = таблицы_адресов(узел_с_таблицей,
                            ["RWqRxLuWzhdqMttMmQQgTrGscqFrGNcy9RLzJYgj2Ui"])
    chk("таблица читается с узла и её адреса разобраны",
        т_да["ok"] is True and len(т_да["accounts"]) == 1
        and т_да["спрошено"] == 1, т_да.get("why_not"))
    т_кеш = таблицы_адресов(None, ["RWqRxLuWzhdqMttMmQQgTrGscqFrGNcy9RLzJYgj2Ui"])
    chk("второй раз таблица берётся из кеша, без узла",
        т_кеш["ok"] is True and т_кеш["из_кеша"] == 1
        and т_кеш["спрошено"] == 0, т_кеш)

    def узел_пустой(метод, параметры):
        return {"value": [None]}

    забыть_таблицы()
    chk("узел отдал пустую таблицу -- отказ: счета в сообщении сдвинулись бы",
        таблицы_адресов(узел_пустой, ["RWqRxLuWzhdqMttMmQQgTrGscqFrGNcy9RLzJYgj2Ui"]
                         )["ok"] is False, "")

    # --- КОТИРОВКА БЕЗ СЕТИ: негодный минт и размер отсекаются до запроса.
    chk("WSOL за WSOL не котируется",
        котировка(WSOL, 10_000_000)["ok"] is False, "")
    chk("нулевой размер не котируется",
        котировка("МИНТ", 0)["ok"] is False, "")

    # --- В РАБОЧЕЙ ЧАСТИ МОДУЛЯ НЕТ НИ ОДНОЙ ОТПРАВКИ.
    рабочая = Path(__file__).read_text(encoding="utf-8").split("def self_test")[0]
    chk("в модуле нет sendTransaction -- отправка живёт в bloom_own_send",
        "sendTransaction" not in рабочая, "")
    chk("в модуле нет подписи -- ключа он не видит вовсе",
        "sign_versioned" not in рабочая and "Keypair" not in рабочая, "")

    print(f"самопроверка маршрута Jupiter: {всего[1]}/{всего[0]} пройдено")
    return 0 if всего[1] == всего[0] else 1


if __name__ == "__main__":
    sys.exit(self_test())
