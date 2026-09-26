#!/usr/bin/env python3
"""Детектор v2: сигналы источников BATCH-5/BATCH-3 -> решение о покупке.

Что делает и чего НЕ делает
---------------------------
Слушает сделки кошельков-источников через вебсокет Helius, разбирает
транзакцию по балансам самого источника, воспроизводит фильтры DBot
(targetMinAmountUI, skipTargetIncreasePosition, maxBuyTimesPerToken,
buyExist) и добавляет наши тормоза из bloom_exec_state. Ничего не
покупает сам: выдаёт решение и пишет его в журнал. Покупку делает
bloom_executor через bloom_api -- единственное место, откуда может уйти
ордер.

Почему источник сделки берётся из подписки, а не угадывается
------------------------------------------------------------
На одном соединении держится по одной подписке на адрес (как в
solana_detect_probe): при accountInclude со всем списком уведомление не
говорит, какой адрес совпал. Тот же приём -- id запроса == порядковый
номер адреса -- позволяет связать подписку с источником.

Почему нельзя рассчитывать на полную транзакцию из вебсокета
------------------------------------------------------------
По факту работы зонда обнаружения (data/dbot_detect_probe_report.txt):
transactionSubscribe на atlas-хосте отдал 86 событий, logsSubscribe --
4784. То есть Enhanced Websockets у нас работает не всегда, и из
уведомления в общем случае есть только подпись и слот. Поэтому разбор
всегда идёт через getTransaction, а инлайновая транзакция из
transactionSubscribe используется только как ускорение, когда она есть.
Горизонт удержания 28.8 с, так что лишние 200-400 мс на getTransaction
роли не играют.

Чего детектор принципиально НЕ придумывает
------------------------------------------
* Адрес пула. Вывести его одинаково для Raydium/Meteora/Pump/Orca из
  разобранной транзакции нельзя без разбора раскладки счетов каждой
  программы. Bloom принимает минт, а не пул, поэтому поле pool
  остаётся null, а в журнал пишется список ВСТРЕЧЕННЫХ программ DEX --
  это факт из транзакции, а не догадка.
* Курс SOL. Источники платят в основном USDC (550 покупок из 589 у
  BATCH-5), а порог DBot задан в SOL-эквиваленте. Курс берётся из
  реального источника; если ни один не ответил -- сигнал уходит в
  журнал с кодом UNKNOWN_RATE и НЕ исполняется. Угаданный курс здесь
  дороже пропущенной сделки.

Запуск
------
  python3 analysis/bloom_detector.py --self-test      # без сети
  python3 analysis/bloom_detector.py --sig <подпись>  # разбор одной сделки
  python3 analysis/bloom_detector.py --serve          # служба
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_exec_state as ST  # noqa: E402

try:                                  # тень не обязана быть на хосте
    import c2_shadow_build as SB      # noqa: E402
    SHADOW_IMPORT_ERR = ""
except Exception as _тень_exc:        # noqa: BLE001
    # Причину прячем в переменную, а не глотаем: без неё "тени нет" на хосте
    # выглядит как решение, хотя это поломка. Она уходит в признак жизни и
    # печатается самопроверкой.
    SB = None
    SHADOW_IMPORT_ERR = f"{type(_тень_exc).__name__}: {_тень_exc}"

try:                                  # кривая цены -- для тени пропусков
    import bloom_price_curve as PC     # noqa: E402
    PRICE_CURVE_IMPORT_ERR = ""
except Exception as _кривая_exc:      # noqa: BLE001
    PC = None
    PRICE_CURVE_IMPORT_ERR = f"{type(_кривая_exc).__name__}: {_кривая_exc}"

try:                                  # налог по маршруту -- узкий фильтр владельца
    import bloom_route_tax as RT      # noqa: E402
    ROUTE_TAX_IMPORT_ERR = ""
except Exception as _налог_exc:       # noqa: BLE001
    # Модуля нет -- фильтр не работает, и это НЕ тихое событие: причина идёт в
    # признак жизни и в лог при старте. Покупки при этом идут как до фильтра,
    # то есть как всю прошлую неделю: молча останавливать торговлю из-за
    # отсутствующего файла хуже, чем молча не фильтровать, но знать об этом
    # владелец обязан.
    RT = None
    ROUTE_TAX_IMPORT_ERR = f"{type(_налог_exc).__name__}: {_налог_exc}"

try:                                  # полоса своей отправки -- тоже не обязана
    import bloom_own_send as OS        # noqa: E402
    OWN_SEND_IMPORT_ERR = ""
except Exception as _полоса_exc:      # noqa: BLE001
    # Так же, как с тенью: причина живёт в переменной и уходит в признак
    # жизни. "Полосы нет" без причины выглядит решением, а это поломка.
    OS = None
    OWN_SEND_IMPORT_ERR = f"{type(_полоса_exc).__name__}: {_полоса_exc}"


class ПодменаТени:
    """Подменяет глобальное SB на время проверки и возвращает как было.

    Отличать "имени нет" от "имя есть и равно None" обязательно: на хосте,
    где модуль тени не импортировался, SB СУЩЕСТВУЕТ и равно None. Ранняя
    версия хранила только значение, и восстановление стирало имя совсем --
    детектор падал на "SB is not None" NameError-ом (деплой 36038500119).
    """

    def __init__(self, чем):
        self.было_имя = "SB" in globals()
        self.было = globals().get("SB")
        globals()["SB"] = чем

    def вернуть(self):
        if self.было_имя:
            globals()["SB"] = self.было
        else:
            globals().pop("SB", None)

try:
    import bloom_telegram_cmd as TGC  # noqa: E402
except Exception:  # noqa: BLE001
    TGC = None

try:
    import bloom_executor as EXEC
except ImportError:  # pragma: no cover
    EXEC = None

try:
    import bloom_notify as NT
except ImportError:  # pragma: no cover
    NT = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

REPO_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("bloom_detector")

# ------------------------------------------------------------------ минты

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
СТАБИЛЬНЫЕ = (USDC, USDT)
КОТИРОВОЧНЫЕ = (WSOL, USDC, USDT)

TOKEN_CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,90}$")
LAMPORT = 10 ** 9

# Программы DEX -- только те, что реально встречались в наших разборах
# (solana_transfer_fee_audit / solana_pool_price_recompute). Список
# используется ТОЛЬКО для пометки в журнале, ни на одно решение он не
# влияет: dexFilter у задач DBot равен null, то есть биржа не фильтруется.
ПРОГРАММЫ_DEX = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora Pools",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "Jupiter v4",
}

# ------------------------------------------------------------------ коды

КОД_КУПИТЬ = "BUY"
КОД_НЕ_ПОКУПКА = "NOT_A_BUY"
КОД_МАЛО = "TARGET_AMOUNT_OUT_OF_RANGE"
# Отдельный код для входов ВПЛОТНУЮ к порогу. Смысл разделения: "вход 0.007
# при пороге 2" и "вход 1.95 при пороге 2" -- это разные события, и мешать
# их в одном коде значит терять из вида второе. Полоса задаётся долей от
# порога и по умолчанию 0.9: ниже -- обычный отказ, внутри -- THRESHOLD_EDGE.
# Код НЕ покупает: он только называет случай, чтобы его было видно в сверке.
КОД_У_ПОРОГА = "THRESHOLD_EDGE"
ПОЛОСА_У_ПОРОГА = ST.env_float("BLOOM_THRESHOLD_EDGE_PCT", 0.9)
КОД_ДОКУПКА = "SKIP_TARGET_INCREASE_POSITION"
КОД_УСТАРЕЛ = "STALE"
КОД_НЕТ_КУРСА = "UNKNOWN_RATE"
КОД_НЕЯСНО = "AMBIGUOUS_TX"
КОД_ОШИБКА_ЦЕПИ = "TX_FAILED"
КОД_НЕ_ДОСТАЛИ = "TX_NOT_FETCHED"
КОД_ПРОМЕЖУТОЧНЫЙ = "INTERMEDIATE_ROUTE"
# Токен пришёл, но источник за него ничего не отдал. Разобран по цепи
# 23.09: источник есть в accountKeys (индекс 28, не плательщик), таблиц
# адресов нет, индексы preBalances сходятся, нативная дельта -- РОВНЫЙ
# ноль. То есть это не сбой разбора и не неоднозначность: источник не
# покупал, а получил. DBot на тех же пяти сделках тоже ничего не купил
# (1681 запись follow, совпадений по источнику и минту в окне 180 с нет).
КОД_ПОЛУЧЕН_НЕ_КУПЛЕН = "TOKEN_RECEIVED_NOT_BOUGHT"
# Флаги -- не коды. Код один на решение, флагов может быть несколько, и
# они не меняют решения: покупка по минту при отсутствии пула остаётся
# покупкой, а расхождение маршрутов -- поводом для доклада, не для отказа.
КОД_РАЗБОР_УПАЛ = "HANDLER_CRASHED"
# Наша покупка ушла, но В ЦЕПИ УПАЛА (meta.err не null). Это не покупка:
# токена нет, позиции нет, ждать авто-ордер Bloom не от чего.
КОД_ПОКУПКА_УПАЛА = "BUY_FAILED_ON_CHAIN"
# Наша собственная транзакция, увиденная в подписке. Это НЕ сигнал: по ней
# ничего не решается и ничего не покупается. Она нужна ровно для одного --
# засечь, через сколько от решения наша покупка появляется в потоке
# processed, и тем самым измерить путь Bloom от ответа до включения.
КОД_НАША_ТРАНЗАКЦИЯ = "OWN_TX_SEEN"
# УЗКИЙ БОЕВОЙ ФИЛЬТР ПО НАЛОГУ МАРШРУТА (слово владельца 25.09). Режет и
# покупку Bloom, и полосу: налог берётся на каждой передаче, и маршрут через
# налоговую промежуточную монету съедает сделку целиком. Пример владельца --
# PICKAXE: WSOL->USDC->GLDx->GP->PICKAXE, GP передан дважды, -5.9 %.
КОД_НАЛОГ_МАРШРУТА = RT.КОД_ПРОПУСКА if RT is not None else "SKIP_TAXED_ROUTE"
ФЛАГ_НЕТ_ПУЛА_SOL = "NO_SOL_POOL_IN_TX"
ФЛАГ_МАРШРУТ_РАЗОШЁЛСЯ = "ROUTE_MISMATCH"
РАЗБОР_ИЗ_СООБЩЕНИЯ = "PARSE_VIA_MSG"
РАЗБОР_ЧЕРЕЗ_RPC = "PARSE_VIA_RPC"

# Потолок версии транзакции -- ОДИН для подписки и для getTransaction.
# Раньше подписка просила 0, а getTransaction 1, и эта асимметрия была
# названа в коде, но не убрана: на прогоне Части A узел прямо отвечал
# -32015 "Transaction version (1) is not supported" при потолке 0, то есть
# в блоках есть транзакции версии 1. Сообщение о такой транзакции
# приходило без meta, детектор падал на getTransaction -- а это только
# confirmed, то есть 1-2 слота и ~300 мс вместо 0 слотов и 0.3 мс.
# Держать два разных потолка нельзя: подписка обязана уметь разобрать всё,
# что умеет разобрать запасной путь, иначе запасной путь становится
# основным незаметно.
ПОТОЛОК_ВЕРСИИ_TX = ST.env_int("BLOOM_MAX_TX_VERSION", 1)

# Порог DBot: targetMinAmountUI = 2 (SOL-эквивалент). Верхней границы
# нет: targetMaxAmountUI = null в обеих задачах.
ПОРОГ_ВХОДА_SOL = ST.env_float("BLOOM_MIN_TARGET_SOL", 2.0)
МАКС_ОТСТАВАНИЕ_СЛОТОВ = ST.env_int("BLOOM_STALE_SLOTS", 3)
КУРС_TTL_S = ST.env_float("BLOOM_RATE_TTL_S", 60.0)

# Тестовый источник -- кошелёк владельца для контролируемого стенда.
# Порог у него СВОЙ и много ниже боевого: стенд ставится на маленькие
# покупки. Всё остальное -- фильтры как у BATCH-5.
def _тестовые_из_окружения() -> tuple:
    """Тестовых источников может быть несколько: у владельца их два --
    кошелёк в Fomo (там комиссию платит Fomo, а владелец второй подписант --
    это путь НАШИХ источников) и обычный кошелёк. Порядок не важен,
    повторы убираются, пустые строки отбрасываются.

    Читаются оба имени: BLOOM_TEST_SOURCES (список) и BLOOM_TEST_SOURCE
    (одиночный, как было). Старое имя оставлено, чтобы уже развёрнутый
    env не перестал работать молча.
    """
    сырое = ((os.environ.get("BLOOM_TEST_SOURCES") or "") + "," +
             (os.environ.get("BLOOM_TEST_SOURCE") or ""))
    видели, out = set(), []
    for часть in сырое.split(","):
        а = часть.strip()
        if а and а not in видели:
            видели.add(а)
            out.append(а)
    return tuple(out)


TEST_SOURCES = _тестовые_из_окружения()
# Оставлено для совместимости отчётов: первый из списка.
TEST_SOURCE = TEST_SOURCES[0] if TEST_SOURCES else ""
TEST_SOURCE_TASK = "TEST"
TEST_MIN_SOL = ST.env_float("BLOOM_TEST_MIN_SOL", 0.05)

# Версия транзакции в записи решения. Отсутствие поля -- это отсутствие, а
# не версия 0: путать их уже случалось, и именно на версиях строится
# проверка гипотезы о сообщениях без meta.
ВЕРСИЯ_НЕТ = "absent"


def версия_транзакции(tx: dict) -> object:
    """Версия транзакции из конверта -- как его отдаёт и подписка, и
    getTransaction."""
    if not isinstance(tx, dict):
        return ВЕРСИЯ_НЕТ
    if "version" in tx:
        return tx["version"]
    внутр = tx.get("transaction")
    if isinstance(внутр, dict) and "version" in внутр:
        return внутр["version"]
    return ВЕРСИЯ_НЕТ


# ------------------------------------------------------- разбор транзакции

def _баланс_ключи(tx: dict) -> list:
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    out = []
    for k in msg.get("accountKeys") or []:
        out.append(k.get("pubkey") if isinstance(k, dict) else k)
    return out


def балансы_кошелька(tx: dict, кошелёк: str) -> dict:
    """Изменения балансов ОДНОГО кошелька: нативный SOL и все токены.

    Нативная разница очищается от комиссии, если кошелёк -- плательщик:
    иначе своя же комиссия выглядит как трата на покупку.
    """
    meta = (tx or {}).get("meta") or {}
    ключи = _баланс_ключи(tx)
    out = {"native_delta_sol": 0.0, "by_mint": {}, "is_fee_payer": False}

    pre_n = meta.get("preBalances") or []
    post_n = meta.get("postBalances") or []
    if кошелёк in ключи:
        i = ключи.index(кошелёк)
        if i < len(pre_n) and i < len(post_n):
            d = int(post_n[i]) - int(pre_n[i])
            if i == 0:
                out["is_fee_payer"] = True
                d += int(meta.get("fee") or 0)
            out["native_delta_sol"] = d / LAMPORT

    def свод(записи, куда):
        for b in записи or []:
            if not isinstance(b, dict) or b.get("owner") != кошелёк:
                continue
            m = b.get("mint")
            if not m:
                continue
            ui = b.get("uiTokenAmount") or {}
            try:
                raw = int(ui.get("amount"))
            except (TypeError, ValueError):
                continue
            з = out["by_mint"].setdefault(
                m, {"pre_raw": 0, "post_raw": 0, "decimals": ui.get("decimals"),
                    "program": b.get("programId"), "in_pre": False})
            з[куда] = з[куда] + raw
            if куда == "pre_raw":
                з["in_pre"] = True
            if з.get("decimals") is None:
                з["decimals"] = ui.get("decimals")
            if not з.get("program"):
                з["program"] = b.get("programId")

    свод(meta.get("preTokenBalances"), "pre_raw")
    свод(meta.get("postTokenBalances"), "post_raw")

    for m, з in out["by_mint"].items():
        d = з["decimals"]
        з["delta_raw"] = з["post_raw"] - з["pre_raw"]
        з["delta_ui"] = (з["delta_raw"] / (10 ** d)) if isinstance(d, int) else None
    return out


def программы_dex(tx: dict) -> list:
    """Какие программы DEX встретились -- факт из транзакции."""
    найдено = []
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    meta = (tx or {}).get("meta") or {}
    пачки = [msg.get("instructions") or []]
    for гр in meta.get("innerInstructions") or []:
        пачки.append((гр or {}).get("instructions") or [])
    for пачка in пачки:
        for ins in пачка:
            if not isinstance(ins, dict):
                continue
            pid = ins.get("programId")
            if pid in ПРОГРАММЫ_DEX and ПРОГРАММЫ_DEX[pid] not in найдено:
                найдено.append(ПРОГРАММЫ_DEX[pid])
    return найдено


def программы_в_транзакции(tx: dict) -> list:
    """ВСЕ программы транзакции, не только DEX -- факт, а не толкование.

    Нужно там, где транзакция подписана нашим ключом, но в журнале её нет:
    список программ показывает, ЧТО именно произошло (перевод системной
    программой, свап через DEX, вызов программы площадки), и не требует
    гадать по сумме.
    """
    найдено = []
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    meta = (tx or {}).get("meta") or {}
    пачки = [msg.get("instructions") or []]
    for гр in meta.get("innerInstructions") or []:
        пачки.append((гр or {}).get("instructions") or [])
    for пачка in пачки:
        for ins in пачка:
            if not isinstance(ins, dict):
                continue
            pid = ins.get("programId")
            if pid and pid not in найдено:
                найдено.append(pid)
    return найдено


def _минты_источника(tx: dict, источник: str | None) -> tuple:
    """Минты, которые ДВИГАЛИСЬ на счетах самого источника, и все минты tx.

    Зачем разделение. Прежнее правило брало ВСЕ минты транзакции и объявляло
    лишние промежуточными. В 2026 агрегаторы кладут в одну транзакцию чужие
    ноги и раздачу комиссий, поэтому "ещё один минт в транзакции" -- это не
    "наш хоп". Проверено на двух живых сделках 24.09:

      * pump.fun AC1vvv...pump: минт 3NZ9JMVB... двигался ТОЛЬКО на чужих
        счетах (+0.0000028, +0.000092, -0.000096), у источника по нему
        ровно ноль движения -- покупка была прямой, а решение было
        INTERMEDIATE_ROUTE;
      * Jupiter NeonTj...: у источника изменился только NeonTj (+113.29),
        а 7tM6wU.../UPTx1d.../3RpEek... двигались на чужих счетах -- это
        чужая нога в той же транзакции, не наш хоп.

    Оговорка вслух: сквозной проход (пришло и ушло, итог ноль) на счете
    источника по балансам неотличим от нетронутого счёта. Поэтому он
    ловится отдельно -- по переводам transferChecked, где минт указан
    явно, -- и в записи видно, каким признаком найден промежуточный.
    """
    meta = (tx or {}).get("meta") or {}
    все_минты = set()
    до, после = {}, {}
    for где, куда in (("preTokenBalances", до), ("postTokenBalances", после)):
        for b in meta.get(где) or []:
            if not isinstance(b, dict) or not b.get("mint"):
                continue
            все_минты.add(b["mint"])
            if источник and b.get("owner") == источник:
                ключ = (b["mint"], b.get("accountIndex"))
                # RAW, а не uiAmount: у токена с 8-9 знаками движение в
                # единицах показывается нулём в ui, и наоборот -- пыль в ui
                # выглядит движением. Считать надо в том, в чём цепь считает.
                сырое = (b.get("uiTokenAmount") or {}).get("amount")
                try:
                    куда[ключ] = int(сырое or 0)
                except (TypeError, ValueError):
                    куда[ключ] = 0
    двигались = set()
    for ключ in set(до) | set(после):
        if после.get(ключ, до.get(ключ, 0)) != до.get(ключ, 0):
            двигались.add(ключ[0])

    # Минты, которые источник лишь ПОДПИСАЛ (он authority у счёта перевода),
    # но на его собственных счетах они не двигались. Это НЕ хоп маршрута:
    # проверено на живой сделке pump.fun 24.09 -- служебный токен
    # 3NZ9JMVB... раздавался крошками (278, 9246, 44, 44 при 8 знаках) на
    # четыре чужих счёта из счёта, где источник только подписант, а его
    # собственный счёт этого минта был создан пустым и остался нулём.
    # Поэтому признак идёт в запись как пометка, но решения НЕ меняет.
    подписанные = set()
    пачки = [((tx.get("transaction") or {}).get("message") or {}).get("instructions") or []]
    for гр in meta.get("innerInstructions") or []:
        пачки.append((гр or {}).get("instructions") or [])
    for пачка in пачки:
        for ins in пачка:
            if not isinstance(ins, dict):
                continue
            # parsed бывает СТРОКОЙ, а не объектом: у разобранной инструкции
            # memo разобранное значение -- сам текст заметки. 24.09 в
            # 14:10:50Z на этом упал боевой разбор сигнала источника
            # Beqv6dzT (AttributeError: 'str' object has no attribute 'get'),
            # и решение было потеряно целиком. Ровно эта же форма уже ломала
            # пересчёт цены пула, там её обошли -- а здесь нет.
            разбор = ins.get("parsed")
            if not isinstance(разбор, dict):
                continue
            if разбор.get("type") != "transferChecked":
                continue
            info = разбор.get("info")
            if not isinstance(info, dict):
                continue
            if info.get("mint") and info.get("authority") == источник:
                подписанные.add(info["mint"])
    return двигались, подписанные, все_минты


def маршрут_из_транзакции(tx: dict, *, минт_покупки: str | None,
                           трата_минт: str | None,
                           источник: str | None = None) -> dict:
    """Маршрут свопа источника: программы, хопы, промежуточные минты.

    Промежуточный минт -- это НЕ любой лишний минт транзакции, а минт,
    который двигался на счетах САМОГО ИСТОЧНИКА (или прошёл через них
    переводом с явным минтом), не котировочный и не купленный. Причина
    ровно такая -- см. _минты_источника: чужая нога в одной транзакции с
    нашей стоила двух ложных INTERMEDIATE_ROUTE на живом стенде.

    Два измерения хопов даются отдельно, потому что это РАЗНЫЕ вещи:
      * минтов_в_маршруте - 1 -- оценка по числу задействованных токенов;
      * вызовов_dex -- сколько раз вызваны известные программы DEX.
    Ни одно из них не выдаётся за "точное число хопов".

    Если источник не передан, правило работать не может: тогда
    промежуточных нет, и это честно помечено в поле intermediate_from.
    """
    двигались, подписанные, все_минты = _минты_источника(tx, источник)
    лишние = {минт_покупки or "", трата_минт or ""} | set(КОТИРОВОЧНЫЕ)
    промежуточные = sorted(двигались - лишние)
    откуда = ("нет источника: правило не применялось" if not источник else
               ("движение на счетах источника" if промежуточные
                else "промежуточных нет"))
    # Сквозной хоп с нулевым итогом (пришло и сразу ушло) по балансам не
    # виден -- и это сказано вслух, а не спрятано. От таксируемого токена
    # нас защищает не этот признак, а налог НАШЕГО минта (taxed/tax_bps) и
    # метка ROUTE_MISMATCH на нашем собственном маршруте.
    только_подпись = sorted(подписанные - двигались - лишние)
    минты = все_минты

    вызовов = 0
    meta = (tx or {}).get("meta") or {}
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    пачки = [msg.get("instructions") or []]
    for гр in meta.get("innerInstructions") or []:
        пачки.append((гр or {}).get("instructions") or [])
    for пачка in пачки:
        for ins in пачка:
            if isinstance(ins, dict) and ins.get("programId") in ПРОГРАММЫ_DEX:
                вызовов += 1

    в_маршруте = len(промежуточные) + 1 + (1 if трата_минт else 0)
    return {"programs": программы_dex(tx),
             "intermediate_mints": промежуточные,
             "mints_in_route": в_маршруте,
             "hops_by_mints": max(1, в_маршруте - 1),
             "dex_calls": вызовов,
             "via_intermediate": bool(промежуточные),
             "intermediate_from": откуда,
             # Минты, которые источник только подписал (раздача комиссий и
             # тому подобное). Видно в записи, на решение не влияет.
             "authorized_only_mints": только_подпись,
             # Минты чужих ног остаются в записи -- их видно, но решение
             # они не меняют: раньше именно они его и меняли.
             "other_tx_mints": sorted(минты - двигались - подписанные - лишние),
             "all_tx_mints": sorted(минты)}


# ------------------------------------------------------------------ пулы

# Служебные адреса: владельцы хранилищ пула, но НЕ пулы. Правило "пул --
# это владелец хранилищ обеих сторон пары" верно для концентрированной
# ликвидности (Raydium CLMM, Orca Whirlpool, Meteora DLMM/DAMM), но у
# Raydium AMM v4 и CPMM владельцем хранилищ выступает ОДИН служебный адрес
# на все пулы. Каждый адрес здесь проверен по цепи: getMultipleAccounts
# отдаёт владельцем системную программу и нулевую длину данных, тогда как у
# настоящего пула владелец -- программа DEX и данные есть. Доказательства и
# подписи, в которых адрес встретился -- в data/bloom_pool_exclusions.json,
# и самопроверка следит, чтобы файл и этот список не разошлись.
СЛУЖЕБНЫЕ_НЕ_ПУЛЫ = {
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL":
        "владелец хранилищ Raydium CPMM, но не пул: владелец счёта -- "
        "системная программа, длина данных 0",
}


def _хранилища(tx: dict) -> dict:
    """Владелец счёта -> минты, которые он держит в этой транзакции."""
    из_владельца: dict = {}
    meta = (tx or {}).get("meta") or {}
    for где in ("preTokenBalances", "postTokenBalances"):
        for b in meta.get(где) or []:
            if not isinstance(b, dict):
                continue
            вл, м = b.get("owner"), b.get("mint")
            if вл and м:
                из_владельца.setdefault(вл, set()).add(м)
    return из_владельца


def _счета_инструкций_dex(tx: dict) -> dict:
    """Адрес счёта -> программы DEX, в чьих инструкциях он встретился."""
    вых: dict = {}
    meta = (tx or {}).get("meta") or {}
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    пачки = [msg.get("instructions") or []]
    for гр in meta.get("innerInstructions") or []:
        пачки.append((гр or {}).get("instructions") or [])
    for пачка in пачки:
        for ins in пачка:
            if not isinstance(ins, dict):
                continue
            имя = ПРОГРАММЫ_DEX.get(ins.get("programId"))
            if not имя:
                continue
            for acc in ins.get("accounts") or []:
                if isinstance(acc, str):
                    вых.setdefault(acc, [])
                    if имя not in вых[acc]:
                        вых[acc].append(имя)
    return вых


def кандидаты_пулов(tx: dict, *, минт: str, кошелёк: str | None = None,
                     исключить: dict | None = None) -> dict:
    """ID пула пары токен/WSOL из транзакции. БЕЗ СЕТИ, на горячем пути.

    Правило и его цена ошибки. Пул держит обе стороны пары на своих счетах,
    поэтому в pre/postTokenBalances у этих счетов владельцем стоит сам пул
    (проверено по цепи на живой сделке: Meteora DLMM
    5N9DdF1w1Q6tae6Xy7DbSNxwbYraorNfGSo6ddtLQoGD, владелец счёта --
    программа DEX, данные 904 байта). Индексы счетов в инструкции НЕ
    зашиваются: у каждой программы они свои и меняются с версией.

    Три отказа, каждый честный, вместо догадки:
      * подписант или плательщик -- это кошелёк сделки, а не пул (кошелёк
        источника в одной транзакции держит и токен, и котировочный минт,
        то есть выглядит кандидатом ровно как пул);
      * адрес из проверенного списка служебных -- не пул;
      * кандидатов с WSOL больше одного -- какой наш, неясно, выбор наугад
        не делается.
    """
    исключить = СЛУЖЕБНЫЕ_НЕ_ПУЛЫ if исключить is None else исключить
    хран = _хранилища(tx)
    в_dex = _счета_инструкций_dex(tx)
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    ключи = _баланс_ключи(tx)
    плательщик = ключи[0] if ключи else None
    подписанты = {k.get("pubkey") for k in (msg.get("accountKeys") or [])
                  if isinstance(k, dict) and k.get("signer") and k.get("pubkey")}

    кандидаты = []
    for вл, минты in sorted(хран.items()):
        if len(минты) < 2 or минт not in минты:
            continue
        причина_нет = None
        if вл == кошелёк or вл == плательщик or вл in подписанты:
            причина_нет = "это кошелёк сделки, а не пул (подписант или плательщик)"
        elif вл in исключить:
            причина_нет = f"служебный адрес, проверен по цепи: {исключить[вл]}"
        elif вл not in в_dex:
            причина_нет = "адрес не встречается в счетах инструкций DEX"
        кандидаты.append({"address": вл, "mints": sorted(минты),
                           "with_wsol": WSOL in минты,
                           "dex_programs": в_dex.get(вл, []),
                           "vault_mints_count": len(минты),
                           "rejected": причина_нет})

    годные = [k for k in кандидаты if not k["rejected"] and k["with_wsol"]]
    выбор = годные[0]["address"] if len(годные) == 1 else None
    почему_нет = None
    if выбор is None:
        if not кандидаты:
            почему_нет = ("в транзакции нет владельца с хранилищами двух минтов, "
                           "один из которых наш")
        elif not годные:
            почему_нет = "ни один кандидат не парный к WSOL или все отклонены"
        else:
            почему_нет = (f"кандидатов с WSOL больше одного ({len(годные)}) -- "
                           "какой пул наш, неясно")
    return {"mint": минт, "pool_wsol": выбор, "why_not": почему_нет,
             "candidates": кандидаты, "payer": плательщик,
             "dex_programs": программы_dex(tx)}


def пул_и_прямизна(tx: dict, *, минт: str, кошелёк: str | None = None) -> dict:
    """Пул НАШЕЙ покупки и был ли маршрут одним пулом токен/WSOL.

    Сторожу нужно ровно это: продавать по ID пула можно только если покупка
    прошла одним пулом. На п. 1 стенда наша покупка шла двумя хопами
    (WSOL -> промежуточный -> токен), пула токен/WSOL в ней не было вовсе,
    и продавать по пулу было нечем.
    """
    марш = маршрут_из_транзакции(tx, минт_покупки=минт, трата_минт=WSOL,
                                  источник=кошелёк)
    пулы = кандидаты_пулов(tx, минт=минт, кошелёк=кошелёк)
    прямой = bool(пулы.get("pool_wsol")) and not марш.get("via_intermediate")
    return {"pool": пулы.get("pool_wsol"), "direct": прямой,
             "why_not": пулы.get("why_not"), "route": марш,
             "candidates": пулы.get("candidates")}


def рента_новых_счетов(tx: dict, кошелёк: str) -> float:
    """Аренда токен-счетов, СОЗДАННЫХ в этой транзакции для кошелька, в SOL.

    Зачем это отдельной величиной: при покупке за USDC источник всё равно
    тратит нативный SOL -- на комиссию и на аренду нового ATA под купленный
    токен. Если считать этот SOL тратой, покупка на 1000 USDC выглядит как
    вход 0.007 SOL и не проходит порог. Комиссию из нативной дельты уже
    вычитает балансы_кошелька; аренда считается здесь -- по факту, из роста
    лампортов у самих новых счетов, а не по табличному значению 0.00203928.

    Учитывается только УДЕРЖАННАЯ аренда: временный WSOL-счёт, закрытый в
    той же транзакции, к концу имеет ноль и в сумму не попадает.
    """
    meta = (tx or {}).get("meta") or {}
    pre_n = meta.get("preBalances") or []
    post_n = meta.get("postBalances") or []
    было = {b.get("accountIndex") for b in (meta.get("preTokenBalances") or [])
             if isinstance(b, dict) and b.get("owner") == кошелёк}
    рента = 0
    for b in meta.get("postTokenBalances") or []:
        if not isinstance(b, dict) or b.get("owner") != кошелёк:
            continue
        i = b.get("accountIndex")
        if i in было or not isinstance(i, int):
            continue
        if i < len(pre_n) and i < len(post_n):
            рента += max(0, int(post_n[i]) - int(pre_n[i]))
    return рента / LAMPORT


def сигнал_из_транзакции(tx: dict, источник: str, *, подпись: str,
                          слот: int | None = None) -> dict:
    """Чистый разбор: что именно сделал источник. Без сети и состояния."""
    meta = (tx or {}).get("meta") or {}
    сиг = {"signature": подпись, "source": источник,
            "slot": слот if slot_ok(слот) else (tx or {}).get("slot"),
            "kind": None, "mint": None, "spend": None, "spend_mint": None,
            "spend_ui": None, "first_entry": None, "dex_programs": программы_dex(tx),
            "token_program": None, "decide_reason": None}

    if meta.get("err") is not None:
        сиг["kind"] = "fail"
        сиг["decide_reason"] = "транзакция источника с ошибкой"
        return сиг

    б = балансы_кошелька(tx, источник)
    сиг["native_delta_sol"] = б["native_delta_sol"]

    вошли = [(m, з) for m, з in б["by_mint"].items()
              if m not in КОТИРОВОЧНЫЕ and (з["delta_raw"] or 0) > 0]
    вышли = [(m, з) for m, з in б["by_mint"].items()
              if m not in КОТИРОВОЧНЫЕ and (з["delta_raw"] or 0) < 0]

    # трата в котировочных: WSOL и нативный SOL считаются вместе
    sol_ушло = -min(0.0, б["native_delta_sol"])
    wsol = б["by_mint"].get(WSOL)
    if wsol and (wsol.get("delta_ui") or 0) < 0:
        sol_ушло += -wsol["delta_ui"]
    стабиль_ушло = {}
    for m in СТАБИЛЬНЫЕ:
        з = б["by_mint"].get(m)
        if з and (з.get("delta_ui") or 0) < 0:
            стабиль_ушло[m] = -з["delta_ui"]

    if len(вошли) > 1:
        сиг["kind"] = "ambiguous"
        сиг["decide_reason"] = (f"в транзакции выросло {len(вошли)} некотировочных "
                                   f"минтов -- какой из них покупка, из балансов не видно")
        return сиг

    if вошли:
        m, з = вошли[0]
        сиг["kind"] = "buy"
        сиг["mint"] = m
        сиг["first_entry"] = not з["in_pre"] or з["pre_raw"] == 0
        сиг["token_program"] = з.get("program")
        сиг["received_ui"] = з.get("delta_ui")
        # Нативный SOL при покупке за стейбл уходит на комиссию и аренду
        # нового ATA -- это не трата на вход. Комиссию вычли в
        # балансы_кошелька, аренду вычитаем здесь, и обе величины остаются в
        # записи: иначе "почему 0.007" не проверить.
        рента = рента_новых_счетов(tx, источник)
        sol_чистое = max(0.0, sol_ушло - рента)
        сиг["sol_out_gross"] = sol_ушло or None
        сиг["rent_new_accounts_sol"] = рента or None
        кандидаты = dict(стабиль_ушло)
        if sol_чистое > 0:
            кандидаты[WSOL] = sol_чистое
        сиг["spend_candidates"] = кандидаты or None
        if кандидаты:
            # Предварительный выбор -- без курса: одна нога берётся как есть,
            # из нескольких SOL-нога выбирается только если стейблов нет.
            # Окончательно ногу выбирает в_sol, по SOL-эквиваленту: ровно на
            # этом месте раньше терялись покупки за USDC.
            if WSOL in кандидаты and len(кандидаты) == 1:
                сиг["spend_mint"] = WSOL
                сиг["spend_ui"] = кандидаты[WSOL]
                сиг["spend"] = кандидаты[WSOL]      # уже в SOL
            else:
                m2 = max(стабиль_ушло, key=lambda k: стабиль_ушло[k])
                сиг["spend_mint"] = m2
                сиг["spend_ui"] = стабиль_ушло[m2]
                сиг["spend"] = None                  # нужен курс
        else:
            сиг["kind"] = "received"
            сиг["decide_reason"] = (
                "минт вырос, но источник не отдал ни SOL, ни стейблов "
                f"(нативная дельта {б['native_delta_sol']:+.9f} SOL): это "
                "получение токена, а не покупка -- копировать нечего")
        сиг["route"] = маршрут_из_транзакции(
            tx, минт_покупки=сиг["mint"], трата_минт=сиг.get("spend_mint"),
            источник=источник)
        # ID пула источника: чем покупать, если пул пары токен/WSOL в его
        # транзакции виден. Разбор чистый, без сети -- см. bloom_pool_probe.
        пулы = кандидаты_пулов(tx, минт=сиг["mint"], кошелёк=источник)
        сиг["source_pool"] = пулы.get("pool_wsol")
        сиг["source_pool_candidates"] = [
            {k: c[k] for k in ("address", "with_wsol", "dex_programs", "rejected")}
            for c in пулы.get("candidates") or []]
        if not сиг["source_pool"]:
            сиг["pool_why_not"] = пулы.get("why_not")
            сиг["flags"] = sorted(set(сиг.get("flags") or []) | {ФЛАГ_НЕТ_ПУЛА_SOL})
        return сиг

    if вышли:
        сиг["kind"] = "sell"
        сиг["mint"] = вышли[0][0]
        сиг["decide_reason"] = "продажа источника"
        return сиг

    сиг["kind"] = "other"
    сиг["decide_reason"] = "ни один некотировочный минт не изменился"
    return сиг


def slot_ok(s) -> bool:
    return isinstance(s, int) and s > 0


# ------------------------------------------------------------------ курс

class КурсSOL:
    """Курс SOL/USD из реального источника, с TTL и честным отказом.

    Два источника подряд: пул SOL/USDC на GeckoTerminal и котировка
    Jupiter (оба уже используются в этом репозитории). Если ни один не
    ответил -- курс None, и сигнал с тратой в стейблах НЕ исполняется.
    """

    GECKO = ("https://api.geckoterminal.com/api/v2/networks/solana/pools/"
              "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv")
    JUP = "https://lite-api.jup.ag/swap/v1/quote"

    def __init__(self, ttl_s: float = КУРС_TTL_S) -> None:
        self.ttl_s = ttl_s
        self.значение: float | None = None
        self.когда: float = 0.0
        self.источник: str | None = None
        self.отказы: list = []

    def свежий(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return self.значение is not None and (now - self.когда) < self.ttl_s

    def _gecko(self) -> float | None:
        r = requests.get(self.GECKO, timeout=8, headers={"Accept": "application/json"})
        if not r.ok:
            raise RuntimeError(f"gecko http {r.status_code}")
        a = (((r.json() or {}).get("data") or {}).get("attributes") or {})
        v = a.get("base_token_price_usd")
        return float(v) if v else None

    def _jup(self) -> float | None:
        r = requests.get(self.JUP, timeout=8, params={
            "inputMint": WSOL, "outputMint": USDC,
            "amount": str(LAMPORT), "slippageBps": 50})
        if not r.ok:
            raise RuntimeError(f"jup http {r.status_code}")
        j = r.json() or {}
        out = j.get("outAmount")
        return (int(out) / 10 ** 6) if out else None

    def получить(self, now: float | None = None) -> float | None:
        now = now if now is not None else time.time()
        if self.свежий(now):
            return self.значение
        if requests is None:
            self.отказы = [{"source": "нет requests"}]
            return None
        for имя, fn in (("geckoterminal", self._gecko), ("jupiter", self._jup)):
            try:
                v = fn()
            except Exception as exc:  # noqa: BLE001
                self.отказы.append({"source": имя, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
                continue
            if v and v > 0:
                self.значение, self.когда, self.источник = float(v), now, имя
                self.отказы = []
                return self.значение
        # Протухший курс лучше выдуманного, но и он не вечен: держим
        # его не дольше пяти TTL, дальше честное "курса нет".
        if self.значение is not None and (now - self.когда) < self.ttl_s * 5:
            return self.значение
        return None

    def для_решения(self, now: float | None = None) -> tuple[float | None, str, float | None]:
        """Курс для проверки порога: (значение, пояснение, возраст в секундах).

        Зачем отдельный метод. В горячем пути стояла проверка свежий(): курс
        старше одного TTL (60 с) считался отсутствующим, и покупка за стейбл
        уходила в UNKNOWN_RATE. На боевом сигнале 07:59:49Z так была потеряна
        покупка на 1000 USDC (примерно 8.7 SOL при пороге 2) -- фоновое
        обновление курса не успело, а решение принималось мгновенно.

        Отказ от протухшего курса тут строже, чем нужно: порог проверяется с
        запасом в разы, и курс минутной давности отвечает на вопрос "больше
        двух SOL или нет" так же верно, как секундный. Поэтому допускается
        курс не старше пяти TTL -- ровно та граница, что уже стоит в
        получить() -- и возраст пишется в запись решения, чтобы оговорка была
        видна, а не молчала. Старше -- по-прежнему честное "курса нет".
        """
        now = now if now is not None else time.time()
        if self.значение is None:
            return None, "курса SOL/USD нет вовсе", None
        возраст = now - self.когда
        if возраст < self.ttl_s:
            return self.значение, f"курс свежий ({возраст:.0f} с)", возраст
        if возраст < self.ttl_s * 5:
            return (self.значение,
                     f"курс протух на {возраст:.0f} с при TTL {self.ttl_s:.0f} -- "
                     "взят с оговоркой: порог проверяется с запасом в разы",
                     возраст)
        return None, (f"курс старше {self.ttl_s * 5:.0f} с "
                       f"(возраст {возраст:.0f} с) -- не годится"), возраст


def в_sol(сигнал: dict, курс_usd: float | None) -> tuple[float | None, str]:
    """Трата источника в SOL-эквиваленте. Возвращает (сумма, пояснение).

    Если в транзакции ушли И стейблы, И нативный SOL, нога выбирается по
    БОЛЬШЕМУ SOL-эквиваленту, а не по приоритету валюты. Так потерялись две
    настоящие покупки: источник платил 1000 и 2317 USDC, а тратой считался
    остаток нативного SOL (аренда нового ATA) -- 0.0067 и 0.00055 SOL. При
    пороге 2 SOL обе ушли в TARGET_AMOUNT_OUT_OF_RANGE, а DBot их купил.

    Побочная функция выбора: она же ставит spend_mint/spend_ui в сигнал,
    чтобы в журнале стояла та нога, по которой принято решение.
    """
    канд = сигнал.get("spend_candidates") or {}
    if len(канд) > 1:
        sol_нога = float(канд.get(WSOL) or 0.0)
        стейблы = {m: v for m, v in канд.items() if m in СТАБИЛЬНЫЕ}
        if not курс_usd or курс_usd <= 0:
            # Без курса большую ногу не назвать. Молча взять SOL-ногу нельзя:
            # именно так покупка за стейбл и превращается в "вход 0.007".
            return None, ("две ноги траты (SOL и стейбл), курса SOL/USD нет -- "
                           "какая нога больше, не определить")
        лучший_м = max(стейблы, key=lambda k: стейблы[k])
        стейбл_sol = float(стейблы[лучший_м]) / float(курс_usd)
        if стейбл_sol >= sol_нога:
            сигнал["spend_mint"] = лучший_м
            сигнал["spend_ui"] = стейблы[лучший_м]
            сигнал["spend"] = None
            return стейбл_sol, (f"две ноги: {sol_нога:.6f} SOL и "
                                 f"{стейблы[лучший_м]:.6f} стейбла = "
                                 f"{стейбл_sol:.6f} SOL по курсу "
                                 f"{курс_usd:.2f} USD/SOL -- взята большая")
        сигнал["spend_mint"] = WSOL
        сигнал["spend_ui"] = sol_нога
        сигнал["spend"] = sol_нога
        return sol_нога, (f"две ноги: {sol_нога:.6f} SOL и "
                           f"{стейбл_sol:.6f} SOL в стейбле -- взята большая")
    if сигнал.get("spend") is not None:
        return float(сигнал["spend"]), "трата в SOL/WSOL, курс не нужен"
    if сигнал.get("spend_mint") in СТАБИЛЬНЫЕ and сигнал.get("spend_ui"):
        if not курс_usd or курс_usd <= 0:
            return None, "курс SOL/USD недоступен"
        return float(сигнал["spend_ui"]) / float(курс_usd), f"по курсу {курс_usd:.2f} USD/SOL"
    return None, "трата не определена"


# --------------------------------------------------------------- решение

def фильтры_dbot(сигнал: dict, трата_sol: float | None, *,
                  порог_sol: float = ПОРОГ_ВХОДА_SOL) -> tuple[bool, str, str]:
    """Воспроизведение фильтров задач BATCH-5/BATCH-3.

    Порядок сознательно совпадает с тем, что видно в follow_trades:
    сначала «не покупка», потом размер, потом докупка.
    """
    т = сигнал.get("kind")
    if т == "sell":
        return False, КОД_НЕ_ПОКУПКА, ("продажа источника: sellSettings.mode=only_pnl, "
                                        "DBot копирует только покупки")
    if т != "buy":
        return False, (КОД_НЕЯСНО if т == "ambiguous" else
                        КОД_ПОЛУЧЕН_НЕ_КУПЛЕН if т == "received" else
                        КОД_ОШИБКА_ЦЕПИ if т == "fail" else КОД_НЕ_ПОКУПКА), \
            сигнал.get("decide_reason") or f"тип сигнала {т}"
    if трата_sol is None:
        # Отсутствие курса -- НАША слепота, а не фильтр задачи, и называть им
        # случай можно только если он что-то решает. Докупка уже имеющегося
        # токена не проходит вообще без курса: причина видна из транзакции.
        # Пока это не было учтено, пятнадцать докупок одного минта читались
        # как "курса нет", и разобрать по журналу, потеряли ли мы покупку,
        # было нельзя -- пришлось идти в сверку.
        if сигнал.get("first_entry") is False:
            return False, КОД_ДОКУПКА, ("источник докупает уже имеющийся токен "
                                          "(skipTargetIncreasePosition=true); курса SOL "
                                          "при этом не было, но он тут ничего не решает")
        return False, КОД_НЕТ_КУРСА, ("трата источника в стейблах, а курса SOL нет -- "
                                        "порог в SOL-эквиваленте не проверить")
    if трата_sol < порог_sol:
        доля = (трата_sol / порог_sol) if порог_sol else 0.0
        if доля >= ПОЛОСА_У_ПОРОГА:
            return False, КОД_У_ПОРОГА, (
                f"вход источника {трата_sol:.3f} SOL-эквивалента -- "
                f"{доля * 100:.1f} % от порога {порог_sol}: у самой границы, "
                "но ниже. Покупки нет; случай виден отдельной строкой сверки")
        return False, КОД_МАЛО, (f"вход источника {трата_sol:.3f} SOL-эквивалента "
                                   f"меньше targetMinAmountUI={порог_sol} "
                                   f"({доля * 100:.1f} % от порога)")
    if сигнал.get("first_entry") is False:
        return False, КОД_ДОКУПКА, "источник докупает уже имеющийся токен (skipTargetIncreasePosition=true)"
    if сигнал.get("first_entry") is None:
        return False, КОД_НЕЯСНО, "первый ли это вход источника -- из транзакции не видно"
    return True, КОД_КУПИТЬ, "фильтры задачи пройдены"


def решение(сигнал: dict, *, состояние, трата_sol: float | None,
             баланс_sol: float | None, текущий_слот: int | None = None,
             порог_sol: float = ПОРОГ_ВХОДА_SOL,
             макс_отставание: int = МАКС_ОТСТАВАНИЕ_СЛОТОВ) -> dict:
    """Итоговое решение: фильтры задачи + наши тормоза.

    Устаревший сигнал отсекается ДО тормозов: иначе он занял бы подпись
    в «уже видели» и исказил сверку.
    """
    строка = {"signature": сигнал.get("signature"), "source": сигнал.get("source"),
               "mint": сигнал.get("mint"), "slot": сигнал.get("slot"),
               "kind": сигнал.get("kind"), "spend_sol_eq": трата_sol,
               "spend_mint": сигнал.get("spend_mint"), "spend_ui": сигнал.get("spend_ui"),
               "first_entry": сигнал.get("first_entry"),
               "dex_programs": сигнал.get("dex_programs"),
               "token_program": сигнал.get("token_program"),
               "route": сигнал.get("route")}

    # ПЕРЕНОС РАЗБОРА В РЕШЕНИЕ. Исполнитель покупает по адресу
    # decision["source_pool"], а сюда это поле не копировалось вообще -- оно
    # оставалось только в сигнале. Поэтому ветка "покупать по ID пула
    # источника" не срабатывала НИ РАЗУ, сколько бы пулов разбор ни нашёл:
    # исполнитель каждый раз видел None и покупал по минту. Той же причиной
    # был искалечен разбор нашей покупки -- источник_пул приходил пустым, и
    # сравнивать наш маршрут было не с чем.
    #
    # Числа траты (ноги, аренда, брутто-SOL) идут в запись по той же
    # причине: без них вопрос "почему вход 0.007" не проверить по журналу.
    for поле in ("source_pool", "source_pool_candidates", "pool_why_not",
                  "flags", "spend_candidates", "sol_out_gross",
                  "rent_new_accounts_sol", "native_delta_sol", "received_ui"):
        значение = сигнал.get(поле)
        if значение not in (None, [], {}):
            строка[поле] = значение

    отставание = None
    if slot_ok(текущий_слот) and slot_ok(сигнал.get("slot")):
        отставание = текущий_слот - сигнал["slot"]
    строка["slot_lag"] = отставание
    if отставание is not None and отставание > макс_отставание:
        строка.update({"action": "skip", "code": КОД_УСТАРЕЛ,
                        "reason": (f"сигнал отстал на {отставание} слотов при пороге "
                                    f"{макс_отставание} -- цена уже не та")})
        return строка

    ок, код, причина = фильтры_dbot(сигнал, трата_sol, порог_sol=порог_sol)
    if not ок:
        строка.update({"action": "skip", "code": код, "reason": причина,
                        "filter": "задача DBot"})
        return строка

    # Маршрут через промежуточный токен -- НАШ отказ, не отказ DBot: у
    # задач dexFilter=null и такого фильтра нет. Поэтому помечается как
    # наш лимит с dbot_бы_купил, чтобы сверка не считала это расхождением.
    м = сигнал.get("route") or {}
    if м.get("via_intermediate"):
        строка.update({"action": "skip", "code": КОД_ПРОМЕЖУТОЧНЫЙ,
                        "reason": (f"источник купил через промежуточный токен "
                                    f"{', '.join(x[:10] for x in м['intermediate_mints'])}: "
                                    f"комиссия на перевод берётся на каждой ноге"),
                        "filter": "наш лимит", "dbot_бы_купил": True})
        return строка

    # НАЛОГ ПО МАРШРУТУ. Разбор кладёт в сигнал детектор (ему нужны
    # транзакция и узел), а здесь только решение -- чтобы функция осталась
    # чистой и проверяемой. Нет разбора -- нет и фильтра: молча считать
    # "налога нет" нельзя, поэтому в записи остаётся причина.
    нал = сигнал.get("route_tax") or {}
    фил = сигнал.get("route_tax_filter") or {}
    if нал:
        строка["route_transfer_fee_bps"] = нал.get("route_transfer_fee_bps")
        строка["token_fee_bps"] = нал.get("token_fee_bps")
        строка["route_transfers_of_token"] = нал.get("transfers_of_token")
        строка["route_taxed_intermediates"] = [
            з.get("mint") for з in (нал.get("taxed_intermediates") or [])]
        строка["route_tax_from"] = нал.get("route_from")
        if нал.get("why_not"):
            строка["route_tax_why_not"] = нал["why_not"]
    if сигнал.get("route_tax_ms") is not None:
        строка["route_tax_ms"] = сигнал["route_tax_ms"]
    if сигнал.get("route_tax_batch"):
        строка["route_tax_mints_asked"] = (сигнал["route_tax_batch"] or {}).get("asked")
    if фил:
        строка["would_skip_fee"] = bool(фил.get("would_skip_fee"))
        if фил.get("reason") and not фил.get("skip"):
            строка["route_tax_note"] = фил["reason"]
    if фил.get("skip"):
        строка.update({"action": "skip", "code": КОД_НАЛОГ_МАРШРУТА,
                        "reason": фил.get("reason"),
                        "filter": "наш лимит", "dbot_бы_купил": True})
        return строка

    можно, почему, код2 = состояние.can_open_detailed(
        mint=сигнал["mint"], source_sig=сигнал["signature"], balance_sol=баланс_sol)
    if not можно:
        строка.update({"action": "skip", "code": код2, "reason": почему,
                        "filter": "наш лимит",
                        "dbot_бы_купил": True})
        return строка

    строка.update({"action": "buy", "code": КОД_КУПИТЬ,
                    "reason": "фильтры задачи и наши лимиты пройдены"})
    return строка


# ------------------------------------------------------------------- RPC

def счётчик_кредитов(служба: str):
    """Учёт кредитов Helius по службе, по УЖЕ согласованной модели из
    solana_rpc_client -- свою придумывать нельзя.

    Пишется в каталог состояния, а не в дерево репозитория: на хосте
    ProtectHome=read-only, и repo/data службе недоступен. Оттуда осколок
    забирает проверка живучести.
    """
    try:
        from solana_rpc_client import CreditMeter  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log.warning("учёт кредитов недоступен (%s) -- расход не будет виден",
                    type(exc).__name__)
        return None
    try:
        каталог = ST.state_dir() / "helius_usage"
        каталог.mkdir(parents=True, exist_ok=True)
        return CreditMeter(служба, base=каталог)
    except Exception as exc:  # noqa: BLE001
        log.warning("счётчик кредитов не создан: %s: %s", type(exc).__name__, str(exc)[:160])
        return None


def сессия_с_пулом(соединений: int = 4, размер: int = 8):
    """Сессия с пулом: TCP и TLS платятся один раз, а не на каждый вызов.

    Замер с NL-хоста 24.09.2026 (curl 8.5, два ОДИНАКОВЫХ запроса в одном
    вызове; решающее поле num_connects -- 1 у первого, 0 у второго):

      Helius getSlot по новому соединению: tcp 2.3-2.7 мс, tls 20.0-23.9 мс,
        первый байт 43.8-52.6 мс;
      он же по уже открытому: tcp 0, tls 0, первый байт 9.9-18.5 мс.

    То есть каждый вызов без пула стоил лишних 28-38 мс, и это горячий путь:
    getTransaction делает до 12 попыток подряд, а налог минта, слот и баланс
    идут тем же способом. Сессия одна на объект Helius, а объект в службе
    создаётся один раз при старте.
    """
    if requests is None:
        return None
    try:
        s = requests.Session()
    except Exception:  # noqa: BLE001
        return None
    try:
        from requests.adapters import HTTPAdapter  # noqa: PLC0415
        адаптер = HTTPAdapter(pool_connections=соединений, pool_maxsize=размер,
                               max_retries=0)
        s.mount("https://", адаптер)
        s.mount("http://", адаптер)
    except Exception:  # noqa: BLE001
        pass
    return s


class Helius:
    def __init__(self, key: str | None = None, служба: str = "bloom_detector") -> None:
        self.key = key or os.environ.get("HELIUS_API_KEY") or ""
        self.url = f"https://mainnet.helius-rpc.com/?api-key={self.key}"
        self.sess = сессия_с_пулом()
        self.вызовов = 0
        self.по_методам: dict = {}
        self._кеш_минтов: dict = {}
        self.метр = счётчик_кредитов(служба) if служба else None
        self.учёт_пишется: bool | None = None if self.метр else False
        self.учёт_почему = "" if self.метр else "служба не названа -- учёт не ведётся"

    def _учесть(self, метод: str, байт: int = 0) -> None:
        self.по_методам[метод] = self.по_методам.get(метод, 0) + 1
        if self.метр is None:
            return
        try:
            from solana_rpc_client import CREDITS_BY_METHOD, CREDITS_DEFAULT  # noqa: PLC0415
            self.метр.add(CREDITS_BY_METHOD.get(метод, CREDITS_DEFAULT), bytes_in=байт)
            self.учёт_пишется = True
            self.учёт_почему = ""
        except Exception as exc:  # noqa: BLE001
            # Ронять торговлю учёт не должен, но и МОЛЧАТЬ он не должен:
            # ровно так расход детектора выглядел как 1 кредит при 190 в
            # памяти -- каталог учёта создал root на шаге --check-only, и
            # служба от bot не могла в него писать. Отказ теперь виден в
            # признаке жизни.
            self.учёт_пишется = False
            self.учёт_почему = f"{type(exc).__name__}: {str(exc)[:160]}"

    def учесть_вебсокет(self, байт: int) -> None:
        """Подписка тоже стоит кредитов: 2 за 0.1 МБ по тарифу."""
        if self.метр is None or байт <= 0:
            return
        try:
            from solana_rpc_client import CREDITS_PER_01MB_WS  # noqa: PLC0415
            # Округление ВВЕРХ и целочисленно. Недосчитанный расход -- это
            # порог бюджета, который не сработает; для сторожа расхода
            # ошибаться надо в сторону перерасхода, а не наоборот. Дробное
            # умножение здесь давало 3 кредита вместо 4 на 0.2 МБ.
            ПОРЦИЯ = 104858            # 0.1 МиБ с округлением вверх
            порций = -(-байт // ПОРЦИЯ)
            кредитов = порций * CREDITS_PER_01MB_WS
            if кредитов > 0:
                self.метр.add(кредитов, bytes_in=байт)
        except Exception:  # noqa: BLE001
            pass

    def call(self, метод: str, параметры: list, *, таймаут: float = 10.0):
        """Любая неудача наружу идёт как RuntimeError.

        Это не косметика. Все вызывающие ловят RuntimeError, а requests
        бросает свои исключения (ConnectionError, Timeout, ProxyError), и
        они проходили НАСКВОЗЬ: одна сетевая заминка в налог_минта уносила
        решение, ещё не записанное в журнал, а в слушателе всплывала как
        обрыв подписки с переподпиской. Сигнал терялся молча.
        """
        if requests is None:
            raise RuntimeError("нет requests")
        self.вызовов += 1
        try:
            клиент = self.sess if self.sess is not None else requests
            r = клиент.post(self.url, json={"jsonrpc": "2.0", "id": 1,
                                             "method": метод, "params": параметры},
                             timeout=таймаут)
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{метод}: {type(exc).__name__}: "
                                f"{str(exc)[:160]}") from exc
        self._учесть(метод, len(r.content or b""))
        if not r.ok:
            raise RuntimeError(f"{метод}: http {r.status_code}")
        # Разбор тела -- ТОЖЕ через RuntimeError. Докстрока обещает: любая
        # неудача наружу идёт как RuntimeError, и все вызывающие ловят только
        # его. Но r.json() стоял вне try: при HTTP 200 с телом, которое не
        # разбирается как JSON (заглушка прокси, обрезанный ответ), летел
        # ValueError -- мимо всех обработчиков. Путь транзакция() идёт на
        # КАЖДОЕ сообщение без meta и каждый сигнал logsSubscribe: одно
        # плохое тело вместо одной из двенадцати попыток уносило весь сигнал
        # в HANDLER_CRASHED.
        try:
            j = r.json()
        except ValueError as exc:
            raise RuntimeError(f"{метод}: тело ответа не JSON: "
                                f"{(r.text or '')[:120]}") from exc
        if not isinstance(j, dict):
            # Не словарь -- не ответ JSON-RPC. Молча вернуть None нельзя:
            # вызывающий примет это за "узел ещё не отдал" и будет ждать.
            raise RuntimeError(f"{метод}: ответ не объект JSON-RPC: "
                                f"{type(j).__name__}")
        if "error" in j:
            raise RuntimeError(f"{метод}: {str(j['error'])[:200]}")
        return j.get("result")

    def транзакция(self, подпись: str, *, попыток: int = 12,
                    пауза_s: float = 0.15) -> dict | None:
        """На processed транзакция появляется не мгновенно -- короткие
        частые повторы, а не одна попытка и не длинное ожидание."""
        последняя = None
        for _ in range(попыток):
            try:
                r = self.call("getTransaction", [подпись, {
                    "encoding": "jsonParsed", "commitment": "confirmed",
                    "maxSupportedTransactionVersion": ПОТОЛОК_ВЕРСИИ_TX}])
            except RuntimeError as exc:
                последняя = exc
                r = None
            if r:
                return r
            time.sleep(пауза_s)
        if последняя:
            log.debug("getTransaction %s… не отдался: %s", подпись[:10], последняя)
        return None

    def севшая_подпись(self, подписи: list) -> dict:
        """Какая из подписей варианта села -- ОДНИМ вызовом getSignatureStatuses.

        ЗАЧЕМ. Полоса шлёт шесть вариантов на одном nonce, и садится РОВНО
        ОДИН, а в позицию пишется тот, чей сервис первым сказал "принял". Это
        разные подписи: 25.09 18:09:41Z принял zeroslot (4UV1iMzo...), а села
        67Nt8E64... Догон количества спрашивал узел про принятую -- узел
        честно отвечал "нет такой", и пара становилась несчитаемой. За ночь
        так вышло у 61 пары из 63.

        Возвращает {"signature": ..., "slot": ..., "err": ...} по севшей или
        {"why_not": ...}. Шесть подписей -- один вызов, а не шесть.
        """
        из_: dict = {"signature": None, "slot": None, "err": None,
                     "why_not": None}
        чистые = [п for п in (подписи or []) if isinstance(п, str) and п]
        if not чистые:
            из_["why_not"] = "подписей нет"
            return из_
        try:
            о = self.call("getSignatureStatuses",
                          [чистые[:256], {"searchTransactionHistory": True}])
        except RuntimeError as exc:
            из_["why_not"] = f"getSignatureStatuses не отдался: {str(exc)[:120]}"
            return из_
        значения = (о or {}).get("value") or []
        for подпись, з in zip(чистые, значения):
            if not isinstance(з, dict):
                continue
            из_.update(signature=подпись, slot=з.get("slot"), err=з.get("err"))
            return из_
        из_["why_not"] = "ни одна из подписей в цепи не найдена"
        return из_

    def слот(self) -> int | None:
        try:
            s = self.call("getSlot", [{"commitment": "processed"}])
            return s if isinstance(s, int) else None
        except RuntimeError:
            return None

    def баланс_sol(self, адрес: str) -> float | None:
        try:
            r = self.call("getBalance", [адрес, {"commitment": "confirmed"}])
        except RuntimeError:
            return None
        v = (r or {}).get("value") if isinstance(r, dict) else r
        return (int(v) / LAMPORT) if isinstance(v, int) else None

    @staticmethod
    def _налог_из_счёта(минт: str, val: dict) -> dict:
        """Разбор ОДНОГО счёта минта. Один разбор на оба пути -- одиночный
        getAccountInfo и пакетный getMultipleAccounts: два разных понимания
        одного расширения означали бы два разных налога у одного токена."""
        out = {"mint": минт, "token_program": None, "fee_bps": None,
                "taxed": None}
        val = val or {}
        out["token_program"] = val.get("owner")
        данные = val.get("data")
        разбор = данные.get("parsed") if isinstance(данные, dict) else None
        info = разбор.get("info") if isinstance(разбор, dict) else None
        if not isinstance(info, dict):
            info = {}
        out["decimals"] = info.get("decimals")
        for e in info.get("extensions") or []:
            if isinstance(e, dict) and e.get("extension") == "transferFeeConfig":
                st = (e.get("state") or {})
                out["fee_bps"] = (st.get("newerTransferFee") or {}).get("transferFeeBasisPoints")
                out["max_fee"] = (st.get("newerTransferFee") or {}).get("maximumFee")
                out["fee_authority"] = st.get("transferFeeConfigAuthority")
                out["withdraw_authority"] = st.get("withdrawWithheldAuthority")
        out["taxed"] = bool(out.get("fee_bps"))
        return out

    def налоги_минтов(self, минты: list) -> dict:
        """Налоги СРАЗУ ПО НЕСКОЛЬКИМ минтам -- одним запросом.

        Зачем пакет: фильтр по налогу маршрута стоит ПЕРЕД покупкой, а
        getAccountInfo на каждый минт -- это круг до сети на каждый. Три минта
        в маршруте превратились бы в три круга и в треть секунды задержки
        перед покупкой; getMultipleAccounts берёт их за один круг. Кеш общий с
        налог_минта: спрашиваем только то, чего в нём нет.
        """
        итог = {"asked": 0, "got": 0, "why_not": None, "ms": None}
        новые = [м for м in (минты or []) if м and м not in self._кеш_минтов]
        if not новые:
            return итог
        итог["asked"] = len(новые)
        t0 = time.perf_counter()
        try:
            r = self.call("getMultipleAccounts",
                          [новые, {"encoding": "jsonParsed"}])
        except RuntimeError as exc:
            итог["why_not"] = str(exc)[:160]
            итог["ms"] = round((time.perf_counter() - t0) * 1000, 2)
            return итог
        значения = (r or {}).get("value") or []
        for минт, val in zip(новые, значения):
            # Счёт может не разобраться (адрес не минт) -- тогда кешируем то,
            # что вышло: ставка None, и потребитель увидит "налог не прочитан",
            # а не ноль.
            self._кеш_минтов[минт] = self._налог_из_счёта(минт, val or {})
            итог["got"] += 1
        итог["ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return итог

    def налог_минта(self, минт: str) -> dict:
        """Программа токена и ставка комиссии на перевод. Кеш на процесс:
        расширение у минта меняется эпохами, а не секундами."""
        if минт in self._кеш_минтов:
            return self._кеш_минтов[минт]
        out = {"mint": минт, "token_program": None, "fee_bps": None,
                "taxed": None}
        try:
            r = self.call("getAccountInfo", [минт, {"encoding": "jsonParsed"}])
        except RuntimeError as exc:
            out["why_not"] = str(exc)[:120]
            return out                      # НЕ кешируем неудачу
        val = (r or {}).get("value") or {}
        out["token_program"] = val.get("owner")
        # data при jsonParsed бывает СПИСКОМ ["<base64>", "base64"]: узел так
        # отвечает, когда разобрать счёт нечем (адрес оказался не минтом).
        # Конструкция (data or {}).get(...) на списке падает: непустой список
        # истинен, и "or {}" его не подменяет.
        данные = val.get("data")
        разбор = данные.get("parsed") if isinstance(данные, dict) else None
        info = разбор.get("info") if isinstance(разбор, dict) else None
        if not isinstance(info, dict):
            info = {}
        out["decimals"] = info.get("decimals")
        for e in info.get("extensions") or []:
            if isinstance(e, dict) and e.get("extension") == "transferFeeConfig":
                st = (e.get("state") or {})
                out["fee_bps"] = (st.get("newerTransferFee") or {}).get("transferFeeBasisPoints")
        out["taxed"] = bool(out.get("fee_bps"))
        self._кеш_минтов[минт] = out
        return out


# --------------------------------------------------------------- источники

def тело_ответа(конфиг: dict) -> dict:
    """Тело ответа DBot -- из снимка или из живого ответа.

    Снимки сохранены с КИРИЛЛИЧЕСКИМ ключом "тело", живой ответ несёт
    "body". Код искал только "body", получал None, откатывался на сам
    конфиг, не находил там "res" и честно возвращал пустой список -- то
    есть откат на снимок не работал ВООБЩЕ, и узнать об этом можно было
    только когда DBot откажет. Снимок -- собранные с боя данные, их
    переписывать нельзя, поэтому читаются оба имени.

    Это тот же класс ошибки, ради которого введено правило про ASCII:
    имя ключа разошлось, и код молча получил пустоту вместо данных.
    """
    к = конфиг or {}
    for имя in ("body", "тело"):
        т = к.get(имя)
        if isinstance(т, dict):
            return т
    return к if isinstance(к, dict) else {}


def источники_из_конфига(конфиг: dict, задачи: tuple) -> dict:
    """{адрес: имя задачи} из ответа GET /automation/follow_orders.

    Берётся targetIds -- это и есть адреса источников задачи (проверено
    на снимке data/final/.../konfig.json, где targetIds у BATCH-5 --
    девять base58-адресов, а targetNames -- их прозвища).
    """
    out = {}
    res = тело_ответа(конфиг).get("res") or []
    for t in res:
        if not isinstance(t, dict):
            continue
        имя = t.get("name")
        if имя not in задачи:
            continue
        if not t.get("enabled"):
            continue
        for a in t.get("targetIds") or []:
            if isinstance(a, str) and SIG_RE.match(a) is None and 32 <= len(a) <= 44:
                out[a] = имя
    return out


def загрузить_источники(путь: Path, задачи: tuple) -> dict:
    return источники_из_конфига(json.loads(путь.read_text(encoding="utf-8")), задачи)


DBOT_HOST = "https://api-bot-v1.dbotx.com"
DBOT_READ = "/automation/follow_orders"


def источники_живьём(ключ: str, задачи: tuple, *, таймаут: float = 20.0) -> dict:
    """Список источников прямо из DBot. ТОЛЬКО GET.

    Задачи владелец правит в DBot, а не в репозитории, поэтому зашитый
    снимок со временем разойдётся с боем -- и разойдётся молча, пропуская
    сигналы нового источника. Снимок остаётся откатом, и в лог пишется,
    какой из двух путей сработал.
    """
    if requests is None:
        raise RuntimeError("нет requests")
    r = requests.get(DBOT_HOST + DBOT_READ, timeout=таймаут,
                      headers={"X-API-KEY": ключ, "Accept": "application/json"},
                      params={"page": 0, "size": 100})
    if not r.ok:
        raise RuntimeError(f"DBot {DBOT_READ}: http {r.status_code}")
    body = r.json() or {}
    if body.get("err"):
        raise RuntimeError(f"DBot вернул ошибку: {str(body['err'])[:200]}")
    ист = источники_из_конфига({"res": body.get("res")}, задачи)
    if not ист:
        raise RuntimeError("DBot ответил, но нужных задач в ответе нет")
    return ист


def источники(задачи: tuple, снимок: Path) -> tuple[dict, str]:
    """(источники, откуда). Сначала DBot, затем снимок -- и это видно.

    Тестовый источник добавляется ВСЕГДА, если он задан в окружении: он не
    приходит из DBot и не должен зависеть от его доступности.
    """
    ключ = os.environ.get("DBOT_API_KEY") or ""
    ист, откуда = None, None
    if ключ:
        try:
            ист, откуда = источники_живьём(ключ, задачи), "DBot живьём"
        except Exception as exc:  # noqa: BLE001
            log.warning("живой список источников не получен (%s: %s) -- беру снимок",
                        type(exc).__name__, str(exc)[:200])
    else:
        log.warning("DBOT_API_KEY не задан -- беру снимок источников из репозитория")
    if ист is None:
        ист, откуда = загрузить_источники(снимок, задачи), f"снимок {снимок.name}"
    # ГРУППЫ ИСТОЧНИКОВ (сводный промпт владельца 25.09, пункты 1.1 и 1.2).
    # 38 кошельков реестра и источники скорости приходят НЕ из DBot: Bloom по
    # ним не торгует вовсе, задач в DBot для них нет, а слушать их надо --
    # полоса на них мерит слот, место и кто довёз. Имя задачи для них -- имя
    # группы: по нему дальше и решается, звать ли Bloom.
    try:
        import bloom_source_groups as SG  # noqa: PLC0415

        группы = SG.адреса_всех_групп()
    except Exception as exc:  # noqa: BLE001
        группы = {}
        log.warning("группы источников не прочитаны (%s) -- слушаю только DBot",
                    type(exc).__name__)
    if группы:
        ист = dict(ист)
        добавлено = 0
        for а, г in группы.items():
            # Адрес, который УЖЕ пришёл из DBot, остаётся за своей задачей:
            # там Bloom торгует, и переписать его группой значило бы молча
            # выключить торговлю по нему.
            if а not in ист:
                ист[а] = г
                добавлено += 1
        откуда = f"{откуда} + групп источников: {добавлено}"
    if TEST_SOURCES:
        ист = dict(ист)
        for а in TEST_SOURCES:
            ист[а] = TEST_SOURCE_TASK
        откуда = (f"{откуда} + тестовых источников из окружения: "
                  f"{len(TEST_SOURCES)}")
    return ист, откуда


# ---------------------------------------------------------------- вебсокет

def ws_url(key: str, atlas: bool) -> str:
    host = "atlas-mainnet.helius-rpc.com" if atlas else "mainnet.helius-rpc.com"
    return f"wss://{host}/?api-key={key}"


def подпись_и_слот(res: dict) -> tuple[str | None, int | None]:
    слот = res.get("slot")
    if слот is None:
        слот = (res.get("context") or {}).get("slot")
    sig = res.get("signature")
    if not sig:
        tx = res.get("transaction") or {}
        inner = tx.get("transaction") if isinstance(tx.get("transaction"), dict) else tx
        sigs = (inner or {}).get("signatures") or []
        sig = sigs[0] if sigs else None
    if not sig:
        sig = (res.get("value") or {}).get("signature")
    return (sig if isinstance(sig, str) and SIG_RE.match(sig) else None,
            слот if isinstance(слот, int) else None)


class Детектор:
    def __init__(self, *, источники: dict, состояние, helius: Helius,
                  курс: КурсSOL, режим: str = "dry", исполнитель=None) -> None:
        # Исполнитель вызывается В ЭТОМ ЖЕ ПРОЦЕССЕ, сразу после решения:
        # по замеру решение готово за 0.4 мс и в слоте источника, и отдавать
        # этот запас процессу-посреднику, вычитывающему журнал, нельзя.
        self.исполнитель = исполнитель
        self.исполнено = 0
        self.по_кодам_исполнителя: dict = {}
        self.по_кодам_теста: dict = {}
        self.источники = источники
        self.состояние = состояние
        self.helius = helius
        self.курс = курс
        self.режим = режим
        self.обработано = 0
        self.к_покупке = 0
        self.видели: set = set()
        self.последний_слот: int | None = None
        self.поколение = 0
        self.откуда_источники = "не задано"
        self.обрывов = 0
        # Падения разбора отдельным счётчиком: обрыв подписки и падение на
        # одном сообщении -- разные болезни, и лечатся по-разному.
        self.сбоев_разбора = 0
        # Сколько раз мы увидели в потоке СВОЮ транзакцию. Это замер, а не
        # торговля: сигналом наш кошелёк не становится никогда.
        self.наших_транзакций = 0
        # Тень: сколько собрано, сколько прошло бы симуляцию, сколько дорогих
        # по капитализации и сколько упало. Торговля от них не зависит.
        self.тень_включена = ST.env_int("BLOOM_SHADOW", 1) == 1 and SB is not None
        self.теней = 0
        self.теней_прошло = 0
        self.теней_дорогих = 0
        self.теней_упало = 0
        # СЕРИЯ УСПЕШНЫХ ТЕНЕЙ ПО ТИПУ ПУЛА. Условие владельца для полосы
        # своей отправки: тень по этому типу пула прошла симуляцию хотя бы
        # в пяти последних случаях ПОДРЯД. Серия рвётся первой же неудачей
        # по тому же типу -- иначе "пять из последних двадцати" выдавалось
        # бы за "пять подряд".
        self.тени_подряд: dict = {}
        # ПОЛОСА СВОЕЙ ОТПРАВКИ. Собирает и подписывает покупку сама и
        # отправляет её через Helius Sender рядом с покупкой Bloom. Включает
        # BLOOM_OWN_SEND=1; отправляет только при BLOOM_OWN_SEND_LIVE=1, и
        # этот рубильник проверяется В МОДУЛЕ, а не здесь: одна точка.
        self.полоса_включена = (OS is not None
                                 and ST.env_int("BLOOM_OWN_SEND", 0) == 1)
        # Проверка модуля места в блоке: считается один раз, см.
        # признак_места_в_блоке.
        self._место_модуль: dict | None = None
        self.полос_путей = 0
        # ЗАПУСКОВ и ПУТЕЙ -- разные числа, и разница между ними и есть ответ
        # на вопрос "почему полоса молчит". Путь считается ПОСЛЕ возврата
        # провести(), запуск -- в момент старта потока. Ночь 25.09: покупка
        # была, а путей ноль, и по одному счётчику нельзя было отличить
        # "полосу не позвали" от "позвали, и она не вернулась".
        self.полос_запусков = 0
        # Сколько раз Bloom не был позван потому, что группа источника только
        # для полосы. Это НЕ ошибка, но в признаке жизни оно должно быть видно
        # числом: иначе "Bloom молчит" не отличить от "Bloom отвалился".
        self.bloom_не_зван = 0
        # ТЁПЛЫЕ СОЕДИНЕНИЯ С ОТПРАВИТЕЛЯМИ: когда грели последний раз и что
        # вышло. Холодное соединение -- десятки миллисекунд на пути сделки.
        self.прогрев_отправителей_ts = 0.0
        self.прогрев_отправителей: dict = {}
        self.полос_отправлено = 0
        self.полос_по_стадиям: dict = {}
        self.полос_последняя: dict = {}
        self.полос_куплено_догнано = 0
        # ТЕНЬ СИМУЛЯЦИИ. Считается отдельно от путей: она больше не гейт, и
        # её отказы -- материал для разбора, а не остановка сделки. Если
        # отказов много, а сделки идут -- это разговор о min_out, не о полосе.
        # Веер контролей по сервисам (З2 + пул отправителей): свой счётчик,
        # чтобы "контроль пары виден" и "контроль через Jito виден" не
        # складывались в одно число.
        self.контролей_сервисов_видно = 0
        # ЗАМОК НА СЛОВАРЬ КОНТРОЛЕЙ. Его пишут ДВЕ ветки: узнавание подписи в
        # потоке (из потока разбора) и догон места в блоке (из пульса), и обе
        # пишут словарь ЦЕЛИКОМ. Без замка догон, прочитавший словарь до
        # появления нового сервиса, затёр бы его замер: деньги на этот контроль
        # уже потрачены, а строка пары вышла бы без него.
        self.замок_контролей = threading.Lock()
        self.полос_тень_сим = 0
        self.полос_тень_сим_отказов = 0
        # БАЛАНС КОШЕЛЬКА ПОЛОСЫ -- отдельный от баланса исполнителя (решение
        # владельца 25.09: "Учёт баланса отдельно по кошелькам"). Общий баланс
        # ничего не сказал бы о полосе: на кошельке исполнителя 0.2 SOL на
        # сделку Bloom, а у полосы свои 0.1 SOL.
        self.баланс_полосы_sol = None
        self.t_баланс_полосы = None
        # ТЁПЛЫЙ BLOCKHASH. getLatestBlockhash в горячем пути -- это круг до
        # сети, то есть ровно то, что полоса и меряет. Обновляет отдельные
        # часы (тёплый_хеш), а полоса берёт готовое значение и проверяет его
        # возраст сама.
        self.blockhash = None
        self.blockhash_ts = None
        self.blockhash_почему = "ещё не обновлялся"
        self.blockhash_обновлений = 0
        # КЭШ ШАБЛОНОВ ПЕРВОГО ШАГА (SOL -> Q) для двухшаговой тени.
        # Сеть при создании не зовётся ни разу: шаблоны приходят из той же
        # подписки, что и сигналы, а ingest их только разбирает. Опрос узла
        # выключен -- слово владельца, и в модуле он под отдельным флагом.
        self.кэш_ног = None
        self.кэш_ног_адреса: dict = {}      # хранилище Q -> котировка Q
        self.кэш_ног_почему = "выключен входом" if not self.тень_включена else ""
        self.ног_скормлено = 0
        self.ног_без_транзакции = 0
        self.ног_упало = 0
        self.ног_прогрето = None            # сколько таблиц адресов загружено при старте
        self.ног_прогрев_почему = "ещё не было"
        self.ног_догружено = 0
        self.chain_ok_догнано = 0
        self.ног_догрузка_почему = ""
        self.ног_первые: list = []          # первые события -- на сверку с ingest_stats
        # ЦЕНА ЗАМЕРА ОТДЕЛЬНОЙ СТРОКОЙ. Замер 24.09, первые две минуты
        # работы: четыре котировочных пула дали 421 уведомление и около
        # 17.8 МБ -- это ~535 МБ и ~10 000 кредитов в час, против ~390
        # кредитов в час на всех двадцати источниках вместе. Смешивать их с
        # расходом на торговлю нельзя: источники -- это сделки, а это замер,
        # и при перерасходе выключать надо именно замер.
        self.ног_байт = 0
        self.ног_кредитов = 0
        # ЗАМЕР ДВУХШАГОВОЙ ТЕНИ СНЯТ (решение владельца 25.09 вечером):
        # "двухшаговую тень снять (кэш ног выключить, бюджет 0) -- 2 сигнала из
        # 41 977 за сутки, замер не нужен". Снят В КОДЕ, а не в окружении:
        # деплой идёт с keep_env=yes, env не трогается, и иначе замер вернулся
        # бы сам после полуночи с новой суточной квотой. Вернуть можно одной
        # переменной BLOOM_SHADOW_LEGS_OFF=0, ничего больше не правя.
        self.кэш_ног_снят = ST.env_int("BLOOM_SHADOW_LEGS_OFF", 1) == 1
        self.ног_бюджет = (0 if self.кэш_ног_снят
                            else ST.env_int("BLOOM_SHADOW_LEGS_CREDITS", 30000))
        self.ног_отключён = False
        self.ног_отключён_почему = ""
        # СЧЁТ ЗАМЕРА -- ЗА СУТКИ, А НЕ ЗА ПРОЦЕСС. Цена ошибки измерена
        # 24.09: бюджет 100 000 кредитов жил в памяти, каждый перезапуск
        # выдавал замеру новые 100 000, и за сутки детектор истратил 238 058
        # кредитов при собственном бюджете 150 000 -- при разрешении
        # владельца 100 000 на замер. Теперь израсходованное лежит на диске
        # рядом с состоянием и читается при старте.
        self.ног_кредитов_путь = self.состояние.base / "leg_cache_credits.json"
        self.ног_кредитов_ранее = 0
        self.ног_запись_почему = ""
        self.мест_в_блоке_догнано = 0
        self.ног_кредитов_день = ST.day_key()[0]
        self._прочитать_кредиты_ног()
        # ТЕНЬ ЗА ЧАС (слово владельца 25.09): сигналов / собрала / не собрала
        # по причинам / симуляция упала. Счётчики за процесс не годятся: после
        # перезапуска они обнуляются, и "за час собрано ноль" стало бы не видно.
        self.тень_окно = self._новое_окно_тени()
        self.тень_перезапусков = 0
        self.тень_последний_перезапуск = ""
        # СЧЁТ КРЕДИТОВ ТЕНИ -- ЗА СУТКИ И НА ДИСКЕ, по той же причине, что у
        # кэша ног: перезапуск не имеет права выдать замеру новую квоту.
        self.тень_кредитов_путь = self.состояние.base / "shadow_credits.json"
        self.тень_кредитов_ранее = 0
        self.тень_вызовов = 0
        self.тень_кредитов_день = ST.day_key()[0]
        self.тень_запись_почему = ""
        self._прочитать_кредиты_тени()
        if self.кэш_ног_снят:
            self.кэш_ног_почему = ("замер двухшаговой тени снят словом владельца "
                                    "25.09: 2 сигнала из 41 977 за сутки "
                                    "(BLOOM_SHADOW_LEGS_OFF=0 включает обратно)")
        elif self.тень_включена and ST.env_int("BLOOM_SHADOW_LEGS", 1) == 1:
            try:
                пулы = SB.load_leg_pools()
                self.кэш_ног = SB.LegCache(пулы, self.helius.call)
                self.кэш_ног_адреса = {п["q_vault"]: q for q, п in пулы.items()}
            except Exception as exc:  # noqa: BLE001
                self.кэш_ног_почему = f"{type(exc).__name__}: {str(exc)[:160]}"
        self._последняя_тревога_разбора = 0.0
        # Когда последний раз сказали в Telegram про запасной путь.
        self._последняя_тревога_запасного = 0.0
        # Сводка в основной чат раз в час. Считается ПО ОКНУ, а не с начала
        # службы: "за час ноль покупок" и "с утра три покупки" -- разные
        # сообщения, и путать их нельзя.
        self._решений_за_окно = 0
        self._покупок_за_окно = 0
        self._коды_за_окно: dict = {}
        self._сводка_ts = time.time()
        self.способ: str | None = None
        # Когда ушли на запасной путь. None -- мы на основном.
        self.запасной_путь_с: float | None = None
        # УЧЁТ ЗАПАСНОГО ПУТИ (слово владельца 25.09: "причина важнее шума").
        # Считаем ВСЕ переходы, включая короткие, а сообщаем только о долгих:
        # молчание о коротком переходе -- это не незнание, это учёт вместо шума.
        self.запасных_переходов = 0
        self.запасных_коротких = 0
        self.запасное_время_с = 0.0
        # Окно часа: сколько переходов и сколько секунд на запасном за час, и
        # когда это окно началось. Часовая строка считается по нему.
        self.запасной_час_с = time.time()
        self.запасной_час_переходов = 0
        self.запасной_час_секунд = 0.0
        self._запасной_доложен = False
        # Обрывы основного пути по причинам -- для таблицы владельцу и для
        # письма в поддержку Helius с числами, а не словами.
        self.обрывов_по_причинам: dict = {}
        # Пустые тики приёма: тишина в источниках. Число нужно ровно для
        # того, чтобы "мы висели на запасном" не путали с "рынок молчал".
        self.тиков_тишины = 0
        self.старт = time.time()
        self.по_кодам: dict = {}
        # Часы в слотах -- отдельной подпиской slotSubscribe, а НЕ вызовом
        # getSlot в горячем пути: вызов и стоит задержку, и меряет слот
        # уже ПОСЛЕ неё, то есть врёт в нашу пользу.
        self.слот_сети: int | None = None
        self.t_слот: float | None = None
        self.слот_уведомлений = 0
        # Баланс кошелька тоже обновляется в фоне: getBalance в горячем
        # пути -- это ещё один круг до сети перед отправкой ордера.
        self.баланс_sol: float | None = None
        self.t_баланс: float | None = None
        self.задержки_мс: list = []
        # Оповещатель стенда. Строки уходят в фоновом потоке и только по
        # тестовым источникам; сбой отправки живёт в признаке жизни, а не в
        # исключении -- см. bloom_notify.
        self.оповещатель = NT.Оповещатель() if NT is not None else None
        # Сколько решений принято по ПРОТУХШЕМУ курсу: если это число растёт,
        # значит фоновое обновление не справляется, и это видно числом.
        self.курс_протухший_использован = 0
        # Команды владельца из Telegram (/kill, /kill_sell, /status). Живут в
        # своём потоке: сеть Telegram не должна задерживать торговлю.
        self.команды = None

    def режим_денег(self) -> str:
        """Режим, который решают ДЕНЬГИ, а не переменная окружения.

        BLOOM_MODE описывает только журнал и в env прибит к "dry". Настоящие
        покупки включает исполнитель, и пока его режим не попадал ни в признак
        жизни, ни в строки решений, боевые записи читались как dry-run -- в
        докладе они и складывались в раздел dry_run. Теперь режим берётся у
        исполнителя, если он подключён.
        """
        исп = getattr(self, "исполнитель", None)
        реж = getattr(исп, "mode", None) if исп is not None else None
        return реж or self.режим

    def статус_путь(self) -> Path:
        return self.состояние.base / "detector_status.json"

    # ------------------------------------------------- тревога запасного пути

    def сказать_о_запасном(self, текст: str) -> None:
        """Строка в ОСНОВНОЙ чат. Сбой отправки торговлю не останавливает."""
        if self.оповещатель is None or NT is None:
            return
        try:
            self.оповещатель.послать(текст)
        except Exception:  # noqa: BLE001
            log.warning("строка о запасном пути не ушла")

    def тревога_ухода_на_запасной(self) -> None:
        """Уход на запасной СЧИТАЕТСЯ, но не сообщается.

        Слово владельца 25.09: сообщение только если запасной длится дольше
        30 с; короткие переходы -- только счётчик. Сообщение о долгом уходе
        посылает пульс (проверить_запасной_путь), потому что "дольше 30 с"
        становится известно не в момент ухода, а через 30 с.
        """
        self.запасных_переходов += 1
        self.запасной_час_переходов += 1
        self._запасной_доложен = False
        self.состояние.log_decision({
            "stage": "ws_fallback", "code": "WS_FALLBACK",
            "to": "logsSubscribe", "ts_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "breaks_by_reason": dict(self.обрывов_по_причинам)})

    def тревога_возврата_на_основной(self, через_с: float | None) -> None:
        """Возврат: учёт всегда, сообщение -- только если было дольше порога."""
        if через_с is not None:
            self.запасное_время_с += float(через_с)
            self.запасной_час_секунд += float(через_с)
            if float(через_с) < ПОРОГ_СООБЩЕНИЯ_ЗАПАСНОГО_S:
                self.запасных_коротких += 1
        self.состояние.log_decision({
            "stage": "ws_return", "code": "WS_RETURN",
            "fallback_seconds": (round(float(через_с), 2)
                                  if через_с is not None else None),
            "reported": self._запасной_доложен,
            "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        # СООБЩАЕМ ТОЛЬКО ПРО ДОЛГИЕ. Про короткий уход владелец просил не
        # писать вовсе: именно эти строки и были шумом.
        долгий = (через_с is not None
                   and float(через_с) >= ПОРОГ_СООБЩЕНИЯ_ЗАПАСНОГО_S)
        if NT is None or not (долгий or self._запасной_доложен):
            self._запасной_доложен = False
            return
        хвост = (f"был на запасном {через_с:.0f} с" if через_с is not None
                  else "путь основной")
        self.сказать_о_запасном(NT.строка_тревоги(
            "детектор вернулся на основной transactionSubscribe", хвост))
        self._запасной_доложен = False

    def учесть_обрыв(self, причина: str, подробно: str = "") -> None:
        """Обрыв основного пути -- в счётчик по причине И в журнал.

        Раньше причина жила только в логе службы, и на вопрос "почему рвётся"
        приходилось читать journalctl. Теперь она лежит в журнале решений:
        таблицу причин можно собрать без доступа к хосту.
        """
        self.обрывов_по_причинам[причина] = (
            self.обрывов_по_причинам.get(причина, 0) + 1)
        try:
            self.состояние.log_decision({
                "stage": "ws_break", "code": "WS_BREAK", "reason": причина,
                "detail": str(подробно)[:300],
                "on_fallback": self.запасной_путь_с is not None,
                "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        except Exception:  # noqa: BLE001
            log.warning("обрыв в журнал не лёг")

    def часовая_строка_запасного(self, *, сейчас: float | None = None) -> dict | None:
        """Раз в час: сколько переходов и какая доля времени на запасном.

        Это и есть замена шуму: вместо строки на каждый переход -- одна
        строка в час в ЖУРНАЛЬНЫЙ чат, и отдельная тревога в основной, если
        доля за час больше десятой части.
        """
        сейчас = сейчас if сейчас is not None else time.time()
        прошло = сейчас - self.запасной_час_с
        if прошло < ЧАС_ЗАПАСНОГО_S:
            return None
        # НЕЗАКОНЧЕННЫЙ ПЕРЕХОД тоже считается: если мы на запасном прямо
        # сейчас, его секунды принадлежат этому часу, иначе доля вышла бы
        # нулевой как раз в самый плохой час.
        сейчас_на_запасном = (сейчас - self.запасной_путь_с
                               if self.запасной_путь_с else 0.0)
        секунд = self.запасной_час_секунд + сейчас_на_запасном
        доля = (секунд / прошло) if прошло > 0 else None
        из_ = {"window_s": round(прошло, 1), "switches": self.запасной_час_переходов,
               "fallback_s": round(секунд, 1),
               "share": (round(доля, 4) if доля is not None else None),
               "on_fallback_now": self.запасной_путь_с is not None}
        if NT is not None and self.оповещатель is not None:
            текст = (f"путь подписки за час: переходов на запасной "
                      f"{из_['switches']}, на запасном {из_['fallback_s']:.0f} с "
                      f"из {из_['window_s']:.0f} ({из_['share']:.1%})")
            try:
                self.оповещатель.послать(текст, куда=NT.КУДА_ЖУРНАЛ)
            except Exception:  # noqa: BLE001
                log.warning("часовая строка о запасном не ушла")
            if доля is not None and доля > ДОЛЯ_ТРЕВОГИ_ЗАПАСНОГО:
                self.сказать_о_запасном(NT.строка_тревоги(
                    "доля времени на запасном пути выше порога",
                    f"{доля:.1%} за час при пороге "
                    f"{ДОЛЯ_ТРЕВОГИ_ЗАПАСНОГО:.0%}; переходов "
                    f"{из_['switches']}"))
        try:
            self.состояние.log_decision({"stage": "ws_hour", **из_})
        except Exception:  # noqa: BLE001
            pass
        self.запасной_час_с = сейчас
        self.запасной_час_переходов = 0
        self.запасной_час_секунд = 0.0
        return из_

    def проверить_запасной_путь(self, *, сейчас: float | None = None) -> float | None:
        """С пульса: если возврат не случился за порог -- тревога владельцу."""
        сейчас = сейчас if сейчас is not None else time.time()
        сколько, новое = тревога_запасного(
            запасной_с=self.запасной_путь_с, сейчас=сейчас,
            последняя=self._последняя_тревога_запасного,
            порог=ТРЕВОГА_ЗАПАСНОГО_S, повтор=ПОВТОР_ТРЕВОГИ_ЗАПАСНОГО_S)
        self._последняя_тревога_запасного = новое
        # ОДНО СООБЩЕНИЕ ПРО ДОЛГИЙ УХОД. Порог владельца -- 30 с; всё, что
        # короче, уже посчитано и в чат не идёт.
        if (self.запасной_путь_с is not None and not self._запасной_доложен
                and (сейчас - self.запасной_путь_с) >= ПОРОГ_СООБЩЕНИЯ_ЗАПАСНОГО_S):
            self._запасной_доложен = True
            if NT is not None:
                self.сказать_о_запасном(NT.строка_тревоги(
                    "детектор на запасном logsSubscribe дольше "
                    f"{ПОРОГ_СООБЩЕНИЯ_ЗАПАСНОГО_S:.0f} с",
                    f"уже {сейчас - self.запасной_путь_с:.0f} с; каждый сигнал "
                    f"стоит getTransaction. Возврат через "
                    f"{ОКНО_ЗАПАСНОГО_S:.0f} с"))
        # ПОВТОРНЫХ ТРЕВОГ БОЛЬШЕ НЕТ. Они и были шумом: строка на каждый
        # переход плюс повтор каждые две минуты. Застревание на запасном
        # теперь видно из часовой строки и её порога доли (10 % за час), а
        # одна строка про долгий уход уже послана выше. Возвращаемое число
        # осталось: его читают пульс и самопроверка.
        return сколько

    def сводка_за_окно(self, *, сейчас: float | None = None,
                        окно: float | None = None) -> dict | None:
        """Одна строка в час в ОСНОВНОЙ чат: сигналы, покупки, пропуски.

        Возвращает то, что послали (или None, если время ещё не пришло) --
        чтобы самопроверка смотрела на числа, а не на факт отправки.
        """
        сейчас = сейчас if сейчас is not None else time.time()
        окно = окно if окно is not None else СВОДКА_КАЖДЫЕ_S
        прошло = сейчас - self._сводка_ts
        if прошло < окно:
            return None
        данные = {"window_min": прошло / 60.0, "signals": self._решений_за_окно,
                   "buys": self._покупок_за_окно, "by_code": dict(self._коды_за_окно)}
        self._сводка_ts = сейчас
        self._решений_за_окно = 0
        self._покупок_за_окно = 0
        self._коды_за_окно = {}
        if self.оповещатель is not None and NT is not None:
            self.сказать_о_запасном(NT.строка_сводки(
                окно_мин=данные["window_min"], сигналов=данные["signals"],
                покупок=данные["buys"], по_кодам=данные["by_code"]))
        return данные

    def признак_жизни(self) -> dict:
        st = {ST.SCHEMA_VERSION_KEY: ST.SCHEMA_VERSION,
               "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "updated_ts": time.time(),
               "alive_since_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.старт)),
               "mode": self.режим_денег(),
               "sources": len(self.источники),
               # ГРУППЫ ИСТОЧНИКОВ числами: сколько адресов в какой группе, где
               # Bloom торгует, какой размер у полосы. Без этого "38 кошельков
               # добавлены" -- обещание, а не проверяемый факт, и нельзя
               # увидеть, что файл групп не прочитался.
               "source_groups": self.свод_групп(),
               # ВОЗРАСТ СОЕДИНЕНИЯ ПО КАЖДОМУ ОТПРАВИТЕЛЮ (слово владельца
               # 25.09). Без этого "соединение тёплое" -- обещание: по возрасту
               # видно, поднимается ли оно заново на каждой отправке.
               "sender_connections": self.свод_соединений(),
               "bloom_skipped_by_group": self.bloom_не_зван,
               "sources_from": self.откуда_источники,
               "sources_generation": self.поколение,
               "subscribe_method": self.способ,
               "subscribe_drops": self.обрывов,
               "signals_seen": self.обработано,
               "to_buy": self.к_покупке,
               "by_code": dict(self.по_кодам),
               "by_code_test_source": dict(self.по_кодам_теста),
               "test_sources": list(TEST_SOURCES),
               "test_source": TEST_SOURCE or None,
               "test_min_sol": TEST_MIN_SOL if TEST_SOURCES else None,
               "max_tx_version": ПОТОЛОК_ВЕРСИИ_TX,
               "rate_source": self.курс.источник,
               "rate_usd_sol": self.курс.значение,
               "rate_fresh": self.курс.свежий(),
               "rate_stale_used": self.курс_протухший_использован,
               "rate_failures": self.курс.отказы[-3:]}
        # Рубильник, который не читается, выглядит как включённый: служба
        # молча не торгует. Поэтому доступность пути пишется ОТДЕЛЬНО от
        # состояния -- чтобы почасовая проверка видела поломку, а не тишину.
        доступен, почему = self.состояние.kill_readable()
        убит, причина = self.состояние.kill_active()
        st["kill_path"] = str(self.состояние.kill_path)
        st["kill_readable"] = доступен
        st["kill_active"] = убит
        st["kill_note"] = (почему if доступен else почему) or причина
        st["net_slot"] = self.слот_сети
        st["slot_notifications"] = self.слот_уведомлений
        st["net_slot_age_s"] = (round(time.time() - self.t_слот, 2)
                                 if self.t_слот else None)
        st["balance_sol"] = self.баланс_sol
        st["balance_age_s"] = (round(time.time() - self.t_баланс, 2)
                                   if self.t_баланс else None)
        st["rpc_calls"] = self.helius.вызовов
        st["rpc_by_method"] = dict(self.helius.по_методам)
        st["session_credits"] = (getattr(self.helius.метр, "session_credits", None)
                                     if self.helius.метр else None)
        st["handler_crashes"] = self.сбоев_разбора
        st["telegram"] = (self.оповещатель.статус() if self.оповещатель is not None
                           else {"enabled": False,
                                 "off_reason": "модуль оповещений не загружен"})
        st["own_tx_seen"] = self.наших_транзакций
        # ЗАПАСНОЙ ПУТЬ ВИДЕН ЧИСЛОМ. 24.09 служба просидела на нём три часа,
        # и по признаку жизни это было незаметно: поле subscribe_method там
        # было, но никто на него не смотрел.
        st["chain_ok_backfilled"] = self.chain_ok_догнано
        st["block_position_backfilled"] = self.мест_в_блоке_догнано
        st["block_position"] = self.признак_места_в_блоке()
        st["fallback_seconds"] = (round(time.time() - self.запасной_путь_с, 1)
                                   if self.запасной_путь_с else 0)
        st["fallback_window_s"] = ОКНО_ЗАПАСНОГО_S
        st["subscribe_main_path"] = (self.способ == "transactionSubscribe")
        st["fallback"] = {
            "switches_total": self.запасных_переходов,
            "switches_short": self.запасных_коротких,
            "seconds_total": round(self.запасное_время_с, 1),
            "hour_switches": self.запасной_час_переходов,
            "hour_seconds": round(self.запасной_час_секунд, 1),
            "on_fallback_now": self.запасной_путь_с is not None,
            "report_threshold_s": ПОРОГ_СООБЩЕНИЯ_ЗАПАСНОГО_S,
            "silent_ticks": self.тиков_тишины,
            "tick_s": ТИК_ПОДПИСКИ_S,
            "breaks_by_reason": dict(self.обрывов_по_причинам)}
        st["shadow"] = {"enabled": self.тень_включена, "built": self.теней,
                         "would_pass": self.теней_прошло,
                         "over_cap": self.теней_дорогих,
                         "failed": self.теней_упало,
                         "module_loaded": SB is not None,
                         "module_why_not": globals().get("SHADOW_IMPORT_ERR", ""),
                         "passed_in_row_by_pool": dict(self.тени_подряд),
                         "hour": dict(self.тень_окно),
                         "restarts": self.тень_перезапусков,
                         "last_restart_utc": self.тень_последний_перезапуск,
                         "credits_day": self.тень_кредитов_всего(),
                         "credits_write_why_not": self.тень_запись_почему}
        st["route_tax_filter"] = {
            "price_curve_loaded": PC is not None,
            "price_curve_why_not": globals().get("PRICE_CURVE_IMPORT_ERR", ""),
            "module_loaded": RT is not None,
            "module_why_not": globals().get("ROUTE_TAX_IMPORT_ERR", ""),
            "code": КОД_НАЛОГ_МАРШРУТА,
            "skipped": self.по_кодам.get(КОД_НАЛОГ_МАРШРУТА, 0),
            "skipped_test": self.по_кодам_теста.get(КОД_НАЛОГ_МАРШРУТА, 0)}
        st["own_send"] = {
            "enabled": self.полоса_включена,
            "live": (OS is not None and OS.живьём()),
            "module_loaded": OS is not None,
            "module_why_not": globals().get("OWN_SEND_IMPORT_ERR", ""),
            "paths": self.полос_путей, "starts": self.полос_запусков,
            "threads_alive": self.полос_потоков_живых(),
            "sent": self.полос_отправлено,
            "by_stage": dict(self.полос_по_стадиям),
            "bought_backfilled": self.полос_куплено_догнано,
            "wallet": (OS.кошелёк_полосы() if OS is not None else None),
            "own_wallet": (OS.свой_кошелёк() if OS is not None else None),
            "wallet_ready": (OS.кошелёк_готов() if OS is not None else None),
            "balance_sol": self.баланс_полосы_sol,
            "balance_age_s": (round(time.time() - self.t_баланс_полосы, 1)
                               if self.t_баланс_полосы else None),
            "sim_shadow": {"done": self.полос_тень_сим,
                            "would_fail": self.полос_тень_сим_отказов},
            "blockhash_age_s": (round(time.time() - self.blockhash_ts, 1)
                                 if self.blockhash_ts else None),
            "blockhash_updates": self.blockhash_обновлений,
            "blockhash_why_not": self.blockhash_почему,
            "last": dict(self.полос_последняя),
            "lane_kill": (list(self.состояние.lane_kill_active())
                           if hasattr(self.состояние, "lane_kill_active") else None),
            "limits": ({"open": OS.ЛИМИТ_ОТКРЫТЫХ, "per_day": OS.ЛИМИТ_В_СУТКИ,
                         "size_sol": OS.размер_sol(),
                         "stop_failed_in_row": OS.СТОП_ПОДРЯД_УПАВШИХ,
                         "stop_loss_sol": OS.СТОП_УБЫТОК_SOL}
                        if OS is not None else {}),
            "lane_state": (OS.состояние_полосы(self.состояние.positions())
                            if OS is not None and self.полоса_включена else {}),
            # КОНТРОЛЬ ДОСТАВКИ: включён или нет, сколько уже стоило за сутки
            # и КТО ДОВОЗИТ БЫСТРЕЕ по цепи. Без этих строк в признаке жизни
            # веер контролей был бы расходом без видимого ответа.
            "control": ({"enabled": OS.контроль_включён(),
                          "senders_seen": self.контролей_сервисов_видно,
                          "spent_today": (OS.расход_контроля(self.состояние)
                                           if OS is not None else None),
                          "limits": {
                              "signals_per_day": OS.ЛИМИТ_КОНТРОЛЕЙ_В_СУТКИ,
                              "spend_sol_per_day": OS.ПОТОЛОК_РАСХОДА_КОНТРОЛЯ_SOL,
                              "per_sender_sol": OS.ПОТОЛОК_КОНТРОЛЯ_НА_СЕРВИС_SOL},
                          "by_sender": self.свод_контролей_сервисов()}
                         if OS is not None else {}),
            # БОЕВОЙ ПУЛ: включён ли и кто по факту довозил покупки полосы.
            # Без этого "пул работает" пришлось бы принимать на слово.
            "pool": ({"enabled": OS.пул_включён(),
                       "tip_cap_sol": OS.ПОТОЛОК_ЧАЕВЫХ_ПУЛА_SOL,
                       "winners": self.свод_победителей_пула()}
                      if OS is not None else {}),
            # ВАРИАНТ НА ДОЛГОВЕЧНОМ NONCE (шаг 2): включён ли, какой аккаунт,
            # и главное -- не висит ли неподтверждённый сдвиг. Висящий сдвиг
            # закрывает покупки на этом nonce, и это должно быть видно числом, а
            # не выясняться по журналу.
            "pool_nonce": (self.свод_нонса() if OS is not None else {})}
        st["leg_cache"] = self.признак_кэша_ног()
        st["telegram_commands"] = (self.команды.признак_жизни()
                                    if self.команды is not None
                                    else {"enabled": False, "why_not": "не запущены"})
        st["bloom_keepalive"] = (self.прогрев.признак_жизни()
                                  if getattr(self, "прогрев", None) is not None
                                  else {"enabled": False,
                                        "why_not": "исполнитель не подключён"})
        st["executor_attached"] = self.исполнитель is not None
        st["executed"] = self.исполнено
        st["exec_by_code"] = dict(self.по_кодам_исполнителя)
        if self.исполнитель is not None:
            st["executor"] = self.исполнитель.report()
        st["credits_logged"] = self.helius.учёт_пишется
        st["credits_log_error"] = self.helius.учёт_почему
        з = sorted(self.задержки_мс[-200:])
        st["decide_latency_ms"] = ({"n": len(з), "median": з[len(з) // 2],
                                       "min": з[0], "max": з[-1]} if з else None)
        ST.atomic_write_json(self.статус_путь(), st)
        return st

    def обновить_источники(self, задачи: tuple, снимок: Path) -> bool:
        """True, если список изменился -- тогда нужно переподписаться."""
        новые, откуда = источники(задачи, снимок)
        self.откуда_источники = откуда
        if set(новые) != set(self.источники):
            добавлены = sorted(set(новые) - set(self.источники))
            ушли = sorted(set(self.источники) - set(новые))
            self.источники = новые
            self.поколение += 1
            log.info("источники обновлены: всего %d (+%d %s, -%d %s), откуда: %s",
                     len(новые), len(добавлены), [a[:8] for a in добавлены],
                     len(ушли), [a[:8] for a in ушли], откуда)
            return True
        self.источники = новые
        return False

    def это_тестовый(self, источник: str) -> bool:
        return источник in TEST_SOURCES

    def порог_для(self, источник: str) -> float:
        """Порог входа: у тестового источника свой, у группы -- свой, иначе боевой.

        ПОЧЕМУ ПО ГРУППАМ. Общий порог 2 SOL -- это настройка задач владельца в
        DBot (targetMinAmountUI=2 у всех батчей). Для групп, добавленных 25.09
        только ради полосы, порог может быть другим: там смысл не в копировании
        крупных денег, а в замере скорости. Значение берётся ИЗ ФАЙЛА ГРУПП, а
        не выдумывается: нет его там -- порог прежний, общий.
        """
        if self.это_тестовый(источник):
            return TEST_MIN_SOL
        try:
            import bloom_source_groups as SG  # noqa: PLC0415

            св = SG.политика(SG.группа(источник)).get("min_target_sol")
            if св:
                return float(св)
        except Exception:  # noqa: BLE001
            pass
        return ПОРОГ_ВХОДА_SOL

    def свежий_баланс(self, предел_с: float = 60.0) -> float | None:
        """Баланс, если он не старше предела. Старый баланс -- это
        неизвестный баланс: can_open на None ответит запретом."""
        if self.баланс_sol is None or self.t_баланс is None:
            return None
        return self.баланс_sol if (time.time() - self.t_баланс) <= предел_с else None

    async def обработать_бережно(self, подпись, слот, источник, способ, tx,
                                  t_получено) -> dict | None:
        """Разбор ОДНОГО сообщения так, чтобы оно не могло порвать подписку.

        Цена ошибки измерена на живом стенде. 24.09 в 01:51:17 разбор упал с
        sqlite3.ProgrammingError (одно соединение на все рабочие потоки
        asyncio.to_thread), исключение вышло из цикла сообщений, подписка
        оборвалась и переключилась на запасную -- а сигнал п. 3 (покупка
        RED за 8 USDC) пропал целиком: ни решения в журнале, ни покупки, ни
        строки в Telegram. Сама причина устранена в bloom_exec_state, но
        живучесть не должна зависеть от того, устранена ли КАЖДАЯ причина:
        одно плохое сообщение -- это одна потерянная запись и никогда не
        потеря подписки.
        """
        try:
            return await asyncio.to_thread(self.обработать, подпись, слот, источник,
                                            способ, tx, t_получено)
        except Exception as exc:  # noqa: BLE001
            self.сбоев_разбора += 1
            log.exception("разбор сообщения %s упал -- подписка продолжает работать",
                          (подпись or "")[:12])
            try:
                self.состояние.log_decision({
                    "stage": "handler_crashed", "signature": подпись,
                    "source": источник, "via": способ, "slot": слот,
                    "code": КОД_РАЗБОР_УПАЛ,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "test_source": self.это_тестовый(источник or "")})
            except Exception:  # noqa: BLE001
                log.exception("и запись о падении разбора в журнал не легла")
            if self.оповещатель is not None and NT is not None:
                if (time.time() - self._последняя_тревога_разбора) > 60.0:
                    self._последняя_тревога_разбора = time.time()
                    self.оповещатель.послать(NT.строка_тревоги(
                        КОД_РАЗБОР_УПАЛ,
                        f"{type(exc).__name__} на {(подпись or '')[:10]} -- "
                        "подписка жива, решение потеряно"))
            return None

    def обработать(self, подпись: str, слот: int | None, источник: str | None,
                    как: str, tx: dict | None = None,
                    t_recv: float | None = None) -> dict | None:
        """Один сигнал: разобрать, решить, записать.

        tx, пришедший из сообщения вебсокета, используется КАК ЕСТЬ.
        getTransaction вызывается только если сообщение пришло без meta:
        он не поддерживает processed (только confirmed/finalized), то есть
        стоит слот-два ожидания -- ровно то, что мы и меряем.
        """
        t_recv = t_recv if t_recv is not None else time.time()
        if подпись in self.видели:
            return None
        self.видели.add(подпись)
        if not источник:
            return None
        # НАШ КОШЕЛЁК -- ТОЛЬКО ЗАМЕР. Он подписан отдельной подпиской ради
        # одного числа: когда наша же покупка появляется в потоке. Сигналом
        # он не становится никогда -- иначе мы копировали бы сами себя.
        if источник == ST.EXECUTOR_WALLET:
            return self.отметить_нашу_транзакцию(подпись, слот, tx, t_recv)
        # ХРАНИЛИЩА КОТИРОВОЧНЫХ ПУЛОВ -- ТОЛЬКО КОРМ КЭШУ ШАБЛОНОВ.
        # Это чужие сделки в чужих пулах: сигналом они не становятся, в счёт
        # сигналов не идут и до исполнителя не доходят.
        if источник in self.кэш_ног_адреса:
            return self.скормить_кэшу_ног(подпись, слот, источник, tx)
        self.обработано += 1

        откуда_разбор = РАЗБОР_ИЗ_СООБЩЕНИЯ
        if tx is None:
            откуда_разбор = РАЗБОР_ЧЕРЕЗ_RPC
            tx = self.helius.транзакция(подпись)
        if tx is None:
            строка = {"signature": подпись, "source": источник, "slot": слот,
                       "test_source": self.это_тестовый(источник),
                       "action": "skip", "code": КОД_НЕ_ДОСТАЛИ, "via": как,
                       "reason": "getTransaction не отдал транзакцию за отведённые попытки"}
            self.по_кодам[КОД_НЕ_ДОСТАЛИ] = self.по_кодам.get(КОД_НЕ_ДОСТАЛИ, 0) + 1
            self.состояние.log_decision(строка)
            return строка

        сиг = сигнал_из_транзакции(tx, источник, подпись=подпись, слот=слот)
        # НАЛОГ ПО МАРШРУТУ -- ДО РЕШЕНИЯ, потому что он это решение и меняет
        # (узкий фильтр владельца 25.09). Цена честная и названа числом в
        # записи: один getMultipleAccounts на все минты маршрута, которых нет
        # в кеше. Одиночные getAccountInfo дали бы круг до сети на каждый минт
        # -- именно это когда-то стояло между решением и отправкой и было
        # оттуда убрано. Считаем ТОЛЬКО для сигналов-покупок: на остальных
        # это были бы кредиты и задержка без решения.
        if RT is not None and сиг.get("kind") == "buy" and сиг.get("mint"):
            t_налог = time.perf_counter()
            try:
                передачи = RT.передачи_по_минтам(tx)
                минты_маршрута = [м for м in (передачи.get("by_mint") or {})
                                   if м not in RT.КОТИРОВОЧНЫЕ_ВСЕ]
                пакет = self.helius.налоги_минтов(минты_маршрута)
                сиг["route_tax"] = RT.налог_маршрута(
                    tx, сиг["mint"], self.helius.налог_минта, откуда="source")
                сиг["route_tax_filter"] = RT.фильтр_маршрута(сиг["route_tax"])
                сиг["route_tax_batch"] = пакет
            except Exception as exc:  # noqa: BLE001
                # Фильтр не смог посчитать -- покупка НЕ отменяется, но причина
                # идёт в запись: молча считать "налога нет" нельзя.
                сиг["route_tax"] = {"why_not": f"{type(exc).__name__}: {str(exc)[:160]}",
                                     "route_from": "source"}
                сиг["route_tax_filter"] = {}
            сиг["route_tax_ms"] = round((time.perf_counter() - t_налог) * 1000, 2)
        t_разбор = time.time()
        # Курс и баланс берутся из фоновых кешей: в горячем пути ни одного
        # обращения к сети, иначе замер задержки мерил бы нашу же сеть.
        курс_для, курс_оговорка, курс_возраст = self.курс.для_решения()
        if курс_возраст is not None and курс_возраст >= self.курс.ttl_s and курс_для:
            self.курс_протухший_использован += 1
        трата, пояснение = в_sol(сиг, курс_для)
        тестовый = self.это_тестовый(источник)
        строка = решение(сиг, состояние=self.состояние, трата_sol=трата,
                          баланс_sol=self.свежий_баланс(),
                          текущий_слот=self.слот_сети,
                          порог_sol=self.порог_для(источник))
        # Флаг стоит в КАЖДОМ решении по тестовому источнику: такие сигналы
        # не идут ни в живую сверку с DBot, ни в счётчик 30, ни в пары А/Б.
        строка["test_source"] = тестовый
        t_решение = time.time()
        задержка = round((t_решение - t_recv) * 1000.0, 1)
        self.задержки_мс.append(задержка)
        # Разложение задержки: разбор транзакции против наших лимитов.
        # Без него "11 мс" -- число без адреса, и чинить нечего.
        строка["parse_ms"] = round((t_разбор - t_recv) * 1000.0, 2)
        строка["gate_ms"] = round((t_решение - t_разбор) * 1000.0, 2)
        строка["via"] = как
        строка["parsed_from"] = откуда_разбор
        # Версия транзакции рядом с путём разбора: именно эта пара
        # проверяет гипотезу "сообщения без meta -- это версия 1".
        строка["tx_version"] = версия_транзакции(tx)
        строка["t_recv_ts"] = round(t_recv, 6)
        строка["t_recv_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_recv))
        строка["t_decide_ts"] = round(t_решение, 6)
        строка["decide_latency_ms"] = задержка
        строка["source_slot"] = сиг.get("slot")
        строка["net_slot_at_decision"] = self.слот_сети
        строка["net_slot_age_s"] = (round(t_решение - self.t_слот, 3)
                                                if self.t_слот else None)
        строка["rate_note"] = пояснение
        строка["rate_age_s"] = (round(курс_возраст, 1) if курс_возраст is not None else None)
        строка["rate_state"] = курс_оговорка
        строка["rate_source"] = self.курс.источник
        строка["source_task"] = self.источники.get(источник)
        строка["mode"] = self.режим_денег()
        if строка.get("action") == "buy":
            self.к_покупке += 1
        код = строка.get("code") or "?"
        # Разделы журнала разные: боевые источники и тестовый стенд нельзя
        # складывать в одну сводку, иначе стенд испортит статистику сверки.
        if строка.get("test_source"):
            self.по_кодам_теста[код] = self.по_кодам_теста.get(код, 0) + 1
        else:
            self.по_кодам[код] = self.по_кодам.get(код, 0) + 1
        self.состояние.log_decision(строка)

        # Исполнение -- ПОСЛЕ записи решения в журнал: если процесс упадёт
        # между решением и отправкой, решение уже на диске, и добор его
        # подхватит. Обратный порядок терял бы сигнал молча.
        # Налог минта -- ПОСЛЕ отправки ордера, а не до неё. Это признак
        # токена для журнала, а НЕ условие покупки, но getAccountInfo стоит
        # сотни миллисекунд, и стоял он ровно между готовым решением и
        # отправкой. На первом живом пункте стенда это и дало 11 мс в
        # записи и невидимую задержку дальше. Кеш минтов на процесс тут не
        # спасает: первый раз токен всегда новый.
        # ТЕНЬ -- НА КАЖДОМ СИГНАЛЕ, а не только там, где мы покупаем (слово
        # владельца 25.09: "тень v2 на КАЖДОМ сигнале всю ночь"). Пропущенные
        # сигналы -- как раз самое интересное: по ним видно, что мы потеряли
        # или сберегли. Тень денег не тратит и Bloom не задерживает: свой
        # поток, запускается и отпускается. Стоит ДО вызова Bloom: после
        # отправки она мерила бы уже другое состояние пула.
        if (сиг.get("kind") == "buy" and сиг.get("mint")
                and self.исполнитель is not None):
            self.запустить_тень(строка, tx)
        if строка.get("action") == "buy" and self.исполнитель is not None:
            # ПОЛОСА СВОЕЙ ОТПРАВКИ -- тем же порядком: свой поток, до вызова
            # Bloom. Сравнение "Bloom против нашей" честно только когда обе
            # стороны стартуют от одного решения; поток отпускается сразу и
            # Bloom его не ждёт.
            self.запустить_полосу(строка, tx)
        # BLOOM ТОРГУЕТ НЕ ПО ВСЕМ ИСТОЧНИКАМ. Решение владельца 25.09: по 38
        # кошелькам пункта 1.1 и по источникам скорости "Bloom НЕ торгует" --
        # там работает только полоса. Полоса при этом запущена выше: она и есть
        # смысл этих источников.
        торгует_bloom = self.группа_торгует_bloom(строка.get("source"))
        if строка.get("action") == "buy" and self.исполнитель is not None \
                and торгует_bloom:
            try:
                итог = self.исполнитель.execute(
                    строка, balance_sol=self.свежий_баланс(),
                    amount_sol=self.размер_bloom(строка.get("source")))
            except Exception as exc:  # noqa: BLE001
                # Падение исполнителя не должно валить детектор: он и дальше
                # обязан слушать источники и вести журнал.
                итог = {"exec_code": "EXECUTOR_CRASHED",
                         "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
                log.exception("исполнитель упал на %s", (строка.get("signature") or "")[:12])
            self.исполнено += 1
            ик = итог.get("exec_code") or "?"
            self.по_кодам_исполнителя[ик] = self.по_кодам_исполнителя.get(ик, 0) + 1
            строка["exec"] = итог
            self.состояние.log_decision({"stage": "exec_result",
                                          "signature": строка.get("signature"),
                                          "mint": строка.get("mint"), **итог})
            cid_исполнения = итог.get("client_order_id")
        else:
            cid_исполнения = None
            if (строка.get("action") == "buy" and self.исполнитель is not None
                    and not торгует_bloom):
                # В журнал -- одной строкой: почему Bloom не позван. Без неё
                # разбор не отличит "группа только для полосы" от "исполнитель
                # отвалился", а это разные беды.
                self.bloom_не_зван += 1
                self.состояние.log_decision(
                    {"stage": "bloom_skipped_by_group",
                      "signature": строка.get("signature"),
                      "source": строка.get("source"),
                      "group": self.группа_источника(строка.get("source")),
                      "why": "по этой группе источников Bloom не торгует"})

        # Налог минта спрашивается ЗДЕСЬ -- решение уже в журнале, ордер уже
        # отправлен, и эта сетевая задержка больше ничего не держит. Стоял он
        # МЕЖДУ готовым решением и отправкой, а getAccountInfo -- это сотни
        # миллисекунд. Кеш минтов на процесс тут не спасает: на новом токене
        # первый запрос всё равно идёт в сеть, а новый токен -- обычный
        # случай. Признак токена в журнал нужен всегда, даже когда
        # исполнителя нет, поэтому блок стоит отдельно от исполнения.
        if строка.get("action") == "buy":
            try:
                налог = self.helius.налог_минта(строка.get("mint"))
            except Exception as exc:  # noqa: BLE001
                налог = {"taxed": None, "fee_bps": None,
                          "why_not": f"{type(exc).__name__}: {str(exc)[:120]}"}
            строка["taxed"] = налог.get("taxed")
            строка["tax_bps"] = налог.get("fee_bps")
            строка["tax_why_not"] = налог.get("why_not")
            self.состояние.log_decision({
                "stage": "mint_tax", "signature": строка.get("signature"),
                "mint": строка.get("mint"), "taxed": налог.get("taxed"),
                "tax_bps": налог.get("fee_bps"),
                "tax_why_not": налог.get("why_not"),
                "token_program": налог.get("token_program")})
            if cid_исполнения:
                try:
                    self.состояние.update_position(
                        cid_исполнения, taxed=налог.get("taxed"),
                        tax_bps=налог.get("fee_bps"),
                        # ЕДИНЫЙ НОЛЬ ДЛЯ ПАРЫ: время прихода сигнала источника
                        # на наш узел. "От решения" у Bloom и у полосы считается
                        # от РАЗНЫХ нулей (полоса пишет свой ts_intent уже после
                        # сборки), и сравнивать их нельзя -- 25.09 это дало
                        # "мы раньше на 59 мс" при том, что мы были позже.
                        signal_recv_ts=строка.get("t_recv_ts"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("налог в позицию не записан: %s", type(exc).__name__)

        # Маршрут НАШЕЙ покупки по цепи -- рядом с маршрутом источника.
        # Зачем: на п. 1 стенда источник шёл одним пулом Meteora DLMM, а наша
        # покупка -- двумя хопами Raydium CLMM+CPMM через промежуточный
        # токен, и продажа упала на пуле без ликвидности в диапазоне. Пока
        # маршрут не записан рядом, такое расхождение видно только вручную.
        # Разбор идёт ПОСЛЕ отправки и в отдельном потоке: getTransaction --
        # это сотни миллисекунд плюс ожидание подтверждения, и держать этим
        # разбор следующих сигналов нельзя.
        подписи = (итог or {}).get("signatures") if cid_исполнения else None
        if cid_исполнения and подписи:
            self.назначить_разбор_нашей_покупки(
                cid=cid_исполнения, минт=строка.get("mint"),
                подпись=подписи[0], источник_маршрут=строка.get("route") or {},
                источник_пул=строка.get("source_pool"),
                exec_row=итог, слот_источника=строка.get("source_slot"),
                имя_источника=строка.get("source_task"))

        # Построчное решение -- В ЖУРНАЛЬНЫЙ ЧАТ, и только после журнала на
        # диске и отправки ордера. Слово владельца 25.09: сто решений в час в
        # основном чате не читает никто, поэтому туда идут покупки, продажи,
        # тревоги и одна сводка в час, а построчные решения живут отдельно.
        # Если ID журнала не задан, оповещатель молчит и это НЕ считается
        # сбоем: так решил владелец.
        if self.оповещатель is not None and NT is not None:
            self.оповещатель.послать(NT.строка_решения(строка),
                                      куда=NT.КУДА_ЖУРНАЛ)
        self._решений_за_окно += 1
        код_решения = строка.get("code") or строка.get("action") or "?"
        if строка.get("to_buy") or код_решения == КОД_КУПИТЬ:
            self._покупок_за_окно += 1
        else:
            self._коды_за_окно[код_решения] = self._коды_за_окно.get(код_решения, 0) + 1
        return строка

    # ------------------------------------------------- маршрут нашей покупки

    def скормить_кэшу_ног(self, подпись: str, слот: int | None,
                           хранилище: str, tx: dict | None) -> None:
        """Сделка котировочного пула -> шаблон первого шага. Сеть НЕ зовётся.

        Уведомление без meta (форма без транзакции) просто теряется: лезть
        за ней в узел ради кэша нельзя -- это опрос, а он выключен словом
        владельца. Шаблон от пропуска не портится, он лишь не подтверждён,
        и через 30 с тень сама откажет с причиной.
        """
        if self.кэш_ног is None or self.ног_отключён:
            return None
        if tx is None:
            self.ног_без_транзакции += 1
            return None
        try:
            итог = self.кэш_ног.ingest({"signature": подпись, "slot": слот,
                                          "transaction": tx})
            self.ног_скормлено += 1
        except Exception as exc:  # noqa: BLE001
            self.ног_упало += 1
            log.warning("кэш ног: %s на %s", type(exc).__name__, подпись[:10])
            return None
        # Первые события пишутся в журнал целиком -- ровно для сверки с
        # ingest_stats, как просил автор модуля. Дальше только счётчики.
        if len(self.ног_первые) < 20:
            запись = {"stage": "leg_cache", "signature": подпись, "slot": слот,
                       "q_vault": хранилище,
                       "quote": self.кэш_ног_адреса.get(хранилище),
                       "result": {к: з for к, з in (итог or {}).items()},
                       "fed": self.ног_скормлено}
            self.ног_первые.append(запись)
            try:
                self.состояние.log_decision(запись)
            except Exception:  # noqa: BLE001
                pass
        return None

    @staticmethod
    def _новое_окно_тени() -> dict:
        return {"since_ts": time.time(),
                 "since_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "signals": 0, "built": 0, "sim_failed": 0,
                 "not_built": {}, "crashed": 0}

    def _прочитать_кредиты_тени(self) -> None:
        """Сколько тень истратила СЕГОДНЯ. Ошибка чтения -- не ноль: считаем
        прочитанным нулём и говорим почему, но тень не выключаем: она не
        тратит деньги, только кредиты, и её выключение стоит владельцу
        наблюдения за ночь."""
        try:
            данные = json.loads(self.тень_кредитов_путь.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            self.тень_запись_почему = f"счёт тени не прочитан ({type(exc).__name__})"
            return
        if str(данные.get("day")) != self.тень_кредитов_день:
            return
        try:
            self.тень_кредитов_ранее = int(данные.get("credits") or 0)
        except (TypeError, ValueError):
            self.тень_кредитов_ранее = 0

    def _записать_кредиты_тени(self) -> None:
        try:
            ST.atomic_write_json(self.тень_кредитов_путь,
                                  {"day": self.тень_кредитов_день,
                                   "credits": int(self.тень_кредитов_всего()),
                                   "updated_utc": time.strftime(
                                       "%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        except Exception as exc:  # noqa: BLE001
            self.тень_запись_почему = f"{type(exc).__name__}"

    def тень_кредитов_всего(self) -> int:
        return int(self.тень_кредитов_ранее) + int(self.тень_вызовов)

    def _вызов_тени(self, метод, параметры=None, **кв):
        """Узел для тени с ЕЁ счётчиком. Считать по общему счётчику узла
        нельзя: в него же идут пульс и торговля, и расход тени в нём не
        отделить."""
        self.тень_вызовов += 1
        return self.helius.call(метод, параметры, **кв)

    def сводка_тени_за_час(self, *, сейчас: float | None = None,
                            предел_часа: float = 3600.0) -> dict:
        """С пульса: закрыть часовое окно тени, при нуле собранных -- тревога
        и перезапуск тени (детектор не трогаем).

        Условие владельца: если за 60 минут при трёх и более сигналах собрано
        НОЛЬ -- это поломка, а не тишина рынка.
        """
        сейчас = сейчас if сейчас is not None else time.time()
        окно = self.тень_окно
        итог = {"closed": False, "alarm": False, "restarted": False,
                 "window": dict(окно)}
        if сейчас - float(окно.get("since_ts") or 0) < предел_часа:
            return итог
        итог["closed"] = True
        плохо = (int(окно.get("signals") or 0) >= 3
                 and int(окно.get("built") or 0) == 0)
        if плохо:
            итог["alarm"] = True
            подробно = (f"сигналов {окно['signals']}, собрано 0, симуляция упала "
                         f"{окно.get('sim_failed')}, причины: "
                         f"{json.dumps(окно.get('not_built') or {}, ensure_ascii=False)[:300]}")
            if self.оповещатель is not None and NT is not None:
                self.оповещатель.послать(NT.строка_тревоги(
                    "тень за час не собрала ни одной покупки", подробно))
            итог["restarted"] = bool(self.перезапустить_тень().get("ok"))
        self.тень_окно = self._новое_окно_тени()
        return итог

    def перезапустить_тень(self) -> dict:
        """Перезапуск ТЕНИ, а не детектора: модуль перечитывается, кэш ног
        пересобирается. Торговля при этом не прерывается ни на миллисекунду --
        тень живёт в своих потоках."""
        из_ = {"ok": False, "why_not": None}
        global SB  # noqa: PLW0603
        try:
            import importlib  # noqa: PLC0415
            SB = importlib.reload(SB) if SB is not None else None
            if SB is None:
                import c2_shadow_build as SB2  # noqa: PLC0415
                SB = SB2
            if self.кэш_ног is not None:
                # ЗАМЕР СНЯТ -- кэш ног не пересобираем, а гасим: иначе
                # перезапуск тени возвращал бы снятый замер обратно. Функция
                # обязана вернуть словарь, поэтому здесь не return, а гашение.
                if self.кэш_ног_снят:
                    self.кэш_ног = None
                    self.кэш_ног_адреса = {}
                else:
                    пулы = SB.load_leg_pools()
                    self.кэш_ног = SB.LegCache(пулы, self._вызов_тени)
            self.тень_включена = SB is not None and ST.env_int("BLOOM_SHADOW", 1) == 1
            self.тень_перезапусков += 1
            self.тень_последний_перезапуск = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            из_["ok"] = True
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            log.exception("перезапуск тени не удался")
        return из_

    def _прочитать_кредиты_ног(self) -> None:
        """Сколько замер уже истратил СЕГОДНЯ. Ошибка чтения -- не повод
        считать, что ноль: считаем бюджет исчерпанным и говорим почему."""
        try:
            данные = json.loads(self.ног_кредитов_путь.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            self.ног_отключён = True
            self.ног_отключён_почему = (
                f"счёт замера за сутки не прочитан ({type(exc).__name__}) -- "
                "замер выключен, чтобы не удвоить расход")
            return
        if str(данные.get("day")) != self.ног_кредитов_день:
            return                      # другие сутки: счёт начинается заново
        try:
            self.ног_кредитов_ранее = int(данные.get("credits") or 0)
        except (TypeError, ValueError):
            self.ног_кредитов_ранее = 0
        if self.ног_бюджет > 0 and self.ног_кредитов_ранее >= self.ног_бюджет:
            self.ног_отключён = True
            self.ног_отключён_почему = (
                f"бюджет замера на сутки уже исчерпан до старта: "
                f"{self.ног_кредитов_ранее} из {self.ног_бюджет}")

    def _записать_кредиты_ног(self) -> None:
        """Израсходованное замером -- на диск. Сбой записи торговлю не
        останавливает, но виден в признаке жизни."""
        try:
            ST.atomic_write_json(self.ног_кредитов_путь,
                                  {"day": self.ног_кредитов_день,
                                   "credits": int(self.ног_кредитов_всего()),
                                   "updated_utc": time.strftime(
                                       "%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        except Exception as exc:  # noqa: BLE001
            self.ног_запись_почему = f"{type(exc).__name__}"

    def ног_кредитов_всего(self) -> int:
        """Кредиты замера за СУТКИ: прежние плюс свои за этот процесс."""
        return int(self.ног_кредитов_ранее) + int(self.ног_кредитов)

    def учесть_байты_ног(self, байт: int) -> None:
        """Байты уведомлений по хранилищам котировок -- в свой счёт.

        Тариф тот же, что у остальной подписки: 2 кредита за 0.1 МиБ с
        округлением вверх. Когда счёт доходит до бюджета замера, хранилища
        уходят из подписки: одношаговая тень и торговля от этого не
        меняются, а поток по кошельку и источникам остаётся нетронутым.
        """
        if байт <= 0 or self.ног_отключён:
            return
        self.ног_байт += байт
        ПОРЦИЯ = 104858                     # 0.1 МиБ, как и в общем учёте
        было = self.ног_кредитов
        self.ног_кредитов = -(-self.ног_байт // ПОРЦИЯ) * 2
        # Запись на диск -- не на каждый байт: раз в 200 кредитов замера,
        # иначе fsync встанет в поток подписки.
        if self.ног_кредитов - было and (self.ног_кредитов // 200) != (было // 200):
            self._записать_кредиты_ног()
        if self.ног_бюджет > 0 and self.ног_кредитов_всего() >= self.ног_бюджет:
            self.ног_отключён = True
            self.ног_отключён_почему = (
                f"бюджет замера на сутки исчерпан: {self.ног_кредитов_всего()} "
                f"кредитов из {self.ног_бюджет} (в этом процессе "
                f"{self.ног_кредитов} за {self.ног_байт // 1048576} МБ)")
            self._записать_кредиты_ног()
            # Поднятое поколение заставит подписку переподняться уже без
            # хранилищ котировок -- тем же путём, что и смена источников.
            self.поколение += 1
            log.warning("кэш ног отключён: %s", self.ног_отключён_почему)

    def прогреть_таблицы_ног(self) -> None:
        """Таблицы адресов -- ОДИН getMultipleAccounts при старте, вне
        горячего пути. Без прогрева сборка лезет за ними сама: замер автора
        модуля -- 82 мс против 0.65 мс."""
        if self.кэш_ног is None:
            self.ног_прогрев_почему = self.кэш_ног_почему or "кэша нет"
            return
        try:
            ключи = SB.warm_lut_keys(self.кэш_ног.pools)
            self.кэш_ног.load_luts(ключи)
            self.ног_прогрето = len(self.кэш_ног.luts)
            self.ног_прогрев_почему = ""
            log.info("таблицы адресов прогреты: %d из %d запрошенных",
                     self.ног_прогрето, len(ключи))
        except Exception as exc:  # noqa: BLE001
            self.ног_прогрев_почему = f"{type(exc).__name__}: {str(exc)[:160]}"
            log.warning("прогрев таблиц адресов не удался: %s",
                        self.ног_прогрев_почему)

    def догнать_chain_ok(self, *, предел: int = 5) -> dict:
        """Позиции без вердикта по цепи -- добрать отложенным getTransaction.

        ВНЕ горячего пути: вызывается из пульса. Зачем нужен догон, а не
        только разбор покупки:
          * замерная подписка идёт с "failed": False, поэтому УПАВШАЯ наша
            транзакция по ней не придёт никогда -- chain_ok=False из неё
            недостижим в принципе;
          * уведомление подписки часто приходит без meta (24.09: 547 216
            пустых против 4 123 с телом), и вердикта в нём нет;
          * если при разборе покупки узел транзакцию не отдал, поле
            оставалось пустым НАВСЕГДА -- повторной попытки не было.

        Берём не больше `предел` позиций за круг: это замер, а не гонка, и
        занимать узел им нельзя.
        """
        итог = {"looked": 0, "filled": 0, "still_unknown": 0, "why_not": ""}
        try:
            позиции = self.состояние.positions()
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"{type(exc).__name__}"
            return итог
        кандидаты = []
        for cid, p_ in (позиции or {}).items():
            if p_.get("chain_ok") is not None:
                continue
            if not ST.is_real_mode(p_.get("mode")):
                continue
            подписи = p_.get("signatures") or []
            # Полоса -- своей подписью (см. догнать_место_в_блоке).
            подпись = (подписи[0] if подписи else
                        (p_.get("lane_signature") or p_.get("lane_signature_local")))
            if not подпись:
                continue
            кандидаты.append((cid, подпись))
        кандидаты.sort(key=lambda x: x[0])
        for cid, подпись in кандидаты[:предел]:
            итог["looked"] += 1
            try:
                tx = self.helius.транзакция(подпись)
            except Exception as exc:  # noqa: BLE001
                log.warning("догон chain_ok: узел отказал на %s (%s)",
                            подпись[:10], type(exc).__name__)
                итог["still_unknown"] += 1
                continue
            мета = (tx or {}).get("meta") if isinstance(tx, dict) else None
            if not isinstance(мета, dict):
                # Узел не отдал транзакцию -- вердикта нет. Ничего не пишем:
                # выдумывать посадку по отсутствию ответа нельзя.
                итог["still_unknown"] += 1
                continue
            вердикт = мета.get("err") is None
            try:
                self.состояние.update_position(
                    cid, chain_ok=вердикт, own_tx_seen_chain_ok=вердикт,
                    chain_ok_from="догон getTransaction",
                    **({"chain_err": мета.get("err")} if not вердикт else {}))
                итог["filled"] += 1
                self.chain_ok_догнано += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("догон chain_ok: позиция не записана (%s)",
                            type(exc).__name__)
        return итог

    def догнать_место_в_блоке(self, *, предел: int = 3) -> dict:
        """Место нашей покупки в блоке -- отложенно, вне горячего пути.

        Зачем в детекторе, а не отдельным разбором: владелец спрашивает место
        рядом с bloom_ms и S+N по КАЖДОЙ боевой покупке, и считать его потом
        вручную значит каждый раз вспоминать, какие покупки уже посчитаны.
        Один getBlock уровня signatures на позицию -- это 1 кредит и ответ в
        десятки раз меньше, чем с счетами.

        Считаем по слоту, в котором транзакция СЕЛА (our_slot из разбора
        покупки), а не по слоту замерной подписки: processed может отличаться
        от окончательного. Если our_slot нет, берём own_tx_seen_slot и
        помечаем, откуда взяли.

        Попытки ограничены: блок мог уйти из доступных узлу, и долбить его
        вечно незачем.
        """
        итог = {"looked": 0, "filled": 0, "gave_up": 0, "why_not": ""}
        try:
            позиции = self.состояние.positions()
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"{type(exc).__name__}"
            return итог
        кандидаты = []
        for cid, p_ in (позиции or {}).items():
            if p_.get("block_index") is not None:
                continue
            if not ST.is_real_mode(p_.get("mode")):
                continue
            if int(p_.get("block_tries") or 0) >= ПОПЫТОК_МЕСТА_В_БЛОКЕ:
                continue
            подписи = p_.get("signatures") or []
            # У ПОЛОСЫ подпись лежит ОТДЕЛЬНЫМ полем: своя отправка не идёт
            # через площадку, и список signatures ей никто не заполняет. Без
            # этой ветки место в блоке у полосы не добиралось никогда -- ровно
            # это и стояло прочерком в паре 05:11Z.
            подпись = (подписи[0] if подписи else
                        (p_.get("lane_signature") or p_.get("lane_signature_local")))
            слот = p_.get("our_slot") or p_.get("own_tx_seen_slot")
            откуда = ("our_slot" if p_.get("our_slot") else "own_tx_seen_slot")
            if not подпись or not isinstance(слот, int):
                continue
            кандидаты.append((cid, подпись, слот, откуда))
        кандидаты.sort(key=lambda x: x[0])
        for cid, подпись, слот, откуда in кандидаты[:предел]:
            итог["looked"] += 1
            попытки = int((позиции.get(cid) or {}).get("block_tries") or 0) + 1
            try:
                # Импорт ЛЕНИВЫЙ и намеренно: bloom_block_position импортирует
                # сам детектор (ему нужен потолок версии транзакции), и
                # обычный импорт сверху дал бы круг.
                import bloom_block_position as BP  # noqa: PLC0415

                м = BP.место_по_подписям(self.helius, слот, подпись)
            except Exception as exc:  # noqa: BLE001
                # С ИМЕНЕМ: одно слово "ModuleNotFoundError" в позиции не
                # говорит, какого модуля не хватило, и починить по нему нечего.
                м = {"known": False,
                     "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
            поля = {"block_tries": попытки, "block_slot": слот,
                     "block_slot_from": откуда}
            if м.get("known"):
                поля.update(block_index=м.get("index"),
                             block_total=м.get("total"),
                             block_share=м.get("share"))
                итог["filled"] += 1
                self.мест_в_блоке_догнано += 1
            else:
                поля["block_why_not"] = str(м.get("why_not") or "")[:200]
                if попытки >= ПОПЫТОК_МЕСТА_В_БЛОКЕ:
                    итог["gave_up"] += 1
            try:
                self.состояние.update_position(cid, **поля)
            except Exception as exc:  # noqa: BLE001
                log.warning("место в блоке: позиция не записана (%s)",
                            type(exc).__name__)
        return итог

    def признак_места_в_блоке(self) -> dict:
        """Грузится ли модуль места в блоке ВООБЩЕ -- с именем недостающего.

        Ночь 25.09: в позициях стояло "block_why_not: ModuleNotFoundError"
        без имени, и починить по такой записи было нечего. Проверка один раз
        на процесс (результат запоминается) и попадает в признак жизни, то
        есть видна и в отчёте деплоя, ещё до первой покупки.
        """
        if self._место_модуль is None:
            try:
                import bloom_block_position as BP  # noqa: PLC0415,F401
                self._место_модуль = {"module_loaded": True, "why_not": ""}
            except Exception as exc:  # noqa: BLE001
                self._место_модуль = {
                    "module_loaded": False,
                    "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return dict(self._место_модуль)

    def догрузить_таблицы_ног(self) -> None:
        """Таблицы адресов, накопленные ingest. ВНЕ горячего пути.

        Один getMultipleAccounts на пульс и только когда очередь не пуста.
        Падение здесь ничего не ломает: без таблицы сборка тени просто
        дочитает её сама и будет медленнее.
        """
        if self.кэш_ног is None or self.ног_отключён:
            return
        try:
            сколько = self.кэш_ног.warm_luts()
            if сколько:
                self.ног_догружено += сколько
                log.info("таблицы адресов догружены: %d, в очереди осталось %d",
                         сколько, len(self.кэш_ног.pending_luts))
        except Exception as exc:  # noqa: BLE001
            self.ног_догрузка_почему = f"{type(exc).__name__}: {str(exc)[:120]}"
            log.warning("догрузка таблиц не удалась: %s", self.ног_догрузка_почему)

    def признак_кэша_ног(self) -> dict:
        """Раздел признака жизни: что в кэше есть и насколько оно свежее."""
        if self.кэш_ног is None:
            return {"enabled": False, "why_not": self.кэш_ног_почему or "не создан"}
        сейчас = time.time()
        шаблоны = {}
        for q, ent in (self.кэш_ног.entries or {}).items():
            возраст = ent.get("checked_at")
            шаблоны[q] = {
                "age_s": (round(сейчас - возраст, 1)
                           if isinstance(возраст, (int, float)) else None),
                "flipped": bool((ent.get("tpl") or {}).get("flipped")),
                "sig": ent.get("sig")}
        return {"enabled": True, "pools": len(self.кэш_ног.pools),
                 "templates": шаблоны,
                 "fed": self.ног_скормлено,
                 "no_tx": self.ног_без_транзакции,
                 "failed": self.ног_упало,
                 "luts": len(self.кэш_ног.luts),
                 "luts_pending": len(self.кэш_ног.pending_luts),
                 "luts_warmed": self.ног_прогрето,
                 "warm_why_not": self.ног_прогрев_почему,
                 "luts_topped_up": self.ног_догружено,
                 "top_up_why_not": self.ног_догрузка_почему,
                 "bytes": self.ног_байт,
                 "credits": self.ног_кредитов,
                 "credits_day": self.ног_кредитов_всего(),
                 "credits_before_start": self.ног_кредитов_ранее,
                 "credits_write_why_not": self.ног_запись_почему,
                 "credit_budget": self.ног_бюджет,
                 "dropped": self.ног_отключён,
                 "dropped_why": self.ног_отключён_почему,
                 "ingest_stats": dict(self.кэш_ног.ingest_stats)}

    def запустить_тень(self, строка: dict, tx: dict | None) -> None:
        """Теневая сборка и симуляция -- параллельно Bloom, НЕ задерживая его.

        Слово владельца: на каждый сигнал buy собрать покупку самим и
        проверить её simulateTransaction от адреса нашего кошелька
        (sigVerify=false), НИЧЕГО не отправляя. Торговля от этого не
        меняется ни в одном байте: модуль не умеет отправлять транзакции, а
        cap_usd и would_skip_cap идут в журнал пометкой.

        Поток отдельный и "умирающий": исключение внутри не может ни
        задержать Bloom, ни уронить детектор -- оно записывается в журнал.
        """
        if SB is None or not self.тень_включена:
            return
        self.тень_окно["signals"] = int(self.тень_окно.get("signals") or 0) + 1
        try:
            поток = threading.Thread(
                target=self._тень_внутри, args=(dict(строка), tx),
                name="shadow", daemon=True)
            поток.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("тень не запустилась: %s", type(exc).__name__)

    def _тень_внутри(self, строка: dict, tx: dict | None) -> None:
        t0 = time.time()
        запись = {"stage": "shadow", "signature": строка.get("signature"),
                   "mint": строка.get("mint"), "source": строка.get("source")}
        try:
            курс, _, _ = self.курс.для_решения()
            res = SB.shadow_build(
                tx or {}, строка.get("source") or "", строка.get("mint") or "",
                ST.EXECUTOR_WALLET,
                int(round(float(self.исполнитель.buy_sol) * 1e9))
                if self.исполнитель is not None else 0,
                # Узел тени -- со СВОИМ счётчиком кредитов: в общий счётчик
                # узла идут и пульс, и торговля, и расход тени в нём не отделить.
                self._вызов_тени,
                sol_usd=курс,
                spend_sol_equiv=строка.get("spend_sol"),
                slippage=(float(self.исполнитель.slippage_pct) / 100.0
                           if self.исполнитель is not None else 0.35),
                leg_cache=self.кэш_ног)
            запись.update(res or {})
            self.теней += 1
            # ЧАСОВОЕ ОКНО. Считаем то, что просил владелец: собрала, не
            # собрала (по причинам), симуляция упала.
            окно = self.тень_окно
            собрана = bool((res or {}).get("ok")) and (res or {}).get("tx_base64") \
                if isinstance(res, dict) and "tx_base64" in (res or {}) \
                else bool((res or {}).get("sim_verdict"))
            if собрана:
                окно["built"] = int(окно.get("built") or 0) + 1
            else:
                причина = str((res or {}).get("why_not") or "без причины")[:80]
                окно["not_built"][причина] = int(окно["not_built"].get(причина, 0)) + 1
            if (res or {}).get("sim_verdict") not in (None, "would_pass"):
                окно["sim_failed"] = int(окно.get("sim_failed") or 0) + 1
            прошла = (res or {}).get("sim_verdict") == "would_pass"
            тип_пула = (res or {}).get("pool_program")
            if тип_пула:
                было = self.тени_подряд.get(тип_пула, 0)
                self.тени_подряд[тип_пула] = (было + 1) if прошла else 0
            if прошла:
                self.теней_прошло += 1
            if (res or {}).get("would_skip_cap"):
                self.теней_дорогих += 1
        except Exception as exc:  # noqa: BLE001
            запись["why_not"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            self.теней_упало += 1
            self.тень_окно["crashed"] = int(self.тень_окно.get("crashed") or 0) + 1
            причина = f"{type(exc).__name__}"
            self.тень_окно["not_built"][причина] = int(
                self.тень_окно["not_built"].get(причина, 0)) + 1
        запись["shadow_total_ms"] = round((time.time() - t0) * 1000.0, 2)
        запись["shadow_credits_day"] = self.тень_кредитов_всего()
        # Счёт кредитов тени -- на диск после каждой тени: перезапуск не имеет
        # права выдать замеру новую суточную квоту.
        self._записать_кредиты_тени()
        try:
            self.состояние.log_decision(запись)
        except Exception:  # noqa: BLE001
            log.exception("запись тени в журнал не легла")

    # ------------------------------------------------------- полоса своей отправки

    def обновить_blockhash(self) -> dict:
        """Тёплый blockhash для полосы. ВНЕ горячего пути -- со своих часов.

        Один getLatestBlockhash (1 кредит) на обновление и только когда
        полоса включена. В горячем пути этот вызов стоил бы круг до сети --
        то самое, что полоса и меряет.
        """
        if not self.полоса_включена:
            self.blockhash_почему = "полоса выключена"
            return {"ok": False, "why_not": self.blockhash_почему}
        try:
            о = self.helius.call("getLatestBlockhash", [{"commitment": "confirmed"}])
            хеш = ((о or {}).get("value") or {}).get("blockhash")
        except Exception as exc:  # noqa: BLE001
            # Старый хеш НЕ стираем: он ещё может быть годен по возрасту, а
            # проверку возраста делает сама полоса перед подписью.
            self.blockhash_почему = f"{type(exc).__name__}: {str(exc)[:120]}"
            return {"ok": False, "why_not": self.blockhash_почему}
        if not хеш:
            self.blockhash_почему = "узел не вернул blockhash"
            return {"ok": False, "why_not": self.blockhash_почему}
        self.blockhash = хеш
        self.blockhash_ts = time.time()
        self.blockhash_почему = ""
        self.blockhash_обновлений += 1
        return {"ok": True, "blockhash": хеш}

    def обновить_нонс(self) -> dict:
        """Тёплый nonce для полосы. ВНЕ горячего пути -- с тех же часов, что хеш.

        Один getAccountInfo на обновление и только когда полоса включена и
        вариант на nonce вообще используется. В горячем пути этот вызов стоил
        круг до сети: замер 25.09 показал медиану 60.2 мс от решения до
        отправки при сборке 0.57 мс, и остаток сидел именно здесь.
        """
        if OS is None or not self.полоса_включена:
            return {"ok": False, "why_not": "полоса выключена"}
        try:
            if not OS.пул_нонсом_включён():
                return {"ok": False, "why_not": "вариант на nonce выключен"}
            return OS.обновить_тёплый_нонс(self.helius.call)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:120]}"}

    def свод_нонса(self) -> dict:
        """Состояние варианта пула на nonce: включён, аккаунт, висящий сдвиг."""
        try:
            из_ = {"enabled": OS.пул_нонсом_включён(),
                    "account": OS.нонс_аккаунт() or None,
                    "wait_slots": OS.СЛОТОВ_ЖДЁМ_ВАРИАНТЫ,
                    "wait_s": OS.ожидание_вариантов_s()}
            ож = OS.нонс_ожидает(self.состояние)
            из_["pending_shift"] = bool(ож.get("pending"))
            из_["pending_why"] = ож.get("why")
            из_["shift_signature"] = ож.get("signature")
            # ТЁПЛОЕ ЗНАЧЕНИЕ: есть ли, какого возраста, сколько раз путь взял
            # его тёплым и сколько раз пришлось идти по сети.
            из_["warm"] = OS.свод_тёплого_нонса()
            return из_
        except Exception as exc:  # noqa: BLE001
            return {"why_not": f"{type(exc).__name__}"}

    def свод_соединений(self) -> dict:
        """Возраст соединений и счёт пингов -- как их видит пул отправителей."""
        try:
            import bloom_senders as SS  # noqa: PLC0415

            из_ = {"by_sender": SS.состояние_соединений(),
                    "warm_period_s": OS_ПРОГРЕВ_S,
                    "last_warm_s": (round(time.time()
                                           - float(self.прогрев_отправителей_ts), 1)
                                     if self.прогрев_отправителей_ts else None),
                    "last_warm": {к: v for к, v in
                                   (self.прогрев_отправителей or {}).items()
                                   if к in ("ok", "failed", "why_not")}}
            return из_
        except Exception as exc:  # noqa: BLE001
            return {"why_not": f"{type(exc).__name__}"}

    def прогреть_отправителей(self) -> dict:
        """Держать соединения с отправителями тёплыми. Вне пути сделки.

        Владелец 25.09: "Холодное соединение -- десятки миллисекунд на сделку...
        пингует каждое раз в 20-30 с". Пинг не несёт ни транзакции, ни ключа: он
        нужен ровно для того, чтобы канал был открыт к моменту сделки.
        """
        из_ = {"ok": 0, "failed": 0, "why_not": None}
        if time.time() - float(self.прогрев_отправителей_ts or 0) < OS_ПРОГРЕВ_S:
            из_["why_not"] = "рано: греем раз в 25 с"
            return из_
        self.прогрев_отправителей_ts = time.time()
        try:
            import bloom_senders as SS  # noqa: PLC0415

            из_ = SS.прогреть_всех()
        except Exception as exc:  # noqa: BLE001
            из_ = {"ok": 0, "failed": 0,
                    "why_not": f"{type(exc).__name__}: {str(exc)[:120]}"}
        self.прогрев_отправителей = из_
        return из_

    def свод_групп(self) -> dict:
        """Сколько источников в какой группе и что группе позволено."""
        try:
            import bloom_source_groups as SG  # noqa: PLC0415

            с = SG.свод()
        except Exception as exc:  # noqa: BLE001
            return {"why_not": f"{type(exc).__name__}"}
        # Считаем по ТЕМ адресам, на которые детектор реально подписан: файл
        # может опережать подписку, и тогда важно видеть разницу.
        живьём: dict = {}
        for а in self.источники:
            г = SG.группа(а)
            живьём[г] = живьём.get(г, 0) + 1
        return {"file": с.get("file"), "why_not": с.get("why_not"),
                 "in_file": с.get("by_group"), "subscribed": живьём,
                 "policies": с.get("policies")}

    def группа_источника(self, источник: str | None) -> str:
        """Группа этого источника. Одно место на детектор: разные ответы на
        один адрес означали бы разный размер сделки от того, кто спросил."""
        try:
            import bloom_source_groups as SG  # noqa: PLC0415

            return SG.группа(источник)
        except Exception:  # noqa: BLE001
            return "bloom_lane"

    def группа_торгует_bloom(self, источник: str | None) -> bool:
        """Торгует ли Bloom по этому источнику (решение владельца 25.09)."""
        try:
            import bloom_source_groups as SG  # noqa: PLC0415

            return bool(SG.политика(SG.группа(источник)).get("bloom_trades", True))
        except Exception:  # noqa: BLE001
            return True

    def размер_bloom(self, источник: str | None) -> float | None:
        """Сколько SOL берёт BLOOM по этому источнику. None -- размер из окружения.

        Решение владельца 25.09 (вечер): "Bloom включить на 38 источников
        lane_only с размером 0.05 SOL (на BATCH-3/5 остаётся 0.2)". Размер
        приходит из файла групп (bloom_sol) и передаётся исполнителю на ОДНУ
        покупку -- его собственный buy_sol при этом не меняется, иначе одна
        группа молча переписала бы размер другой.
        """
        try:
            import bloom_source_groups as SG  # noqa: PLC0415

            з = SG.политика(SG.группа(источник)).get("bloom_sol")
            return float(з) if з else None
        except Exception:  # noqa: BLE001
            return None

    def запустить_полосу(self, строка: dict, tx: dict | None) -> None:
        """Своя отправка -- в своём потоке и НЕ задерживая Bloom.

        Поток отпускается сразу, как у тени. Исключение внутри не может ни
        задержать Bloom, ни уронить детектор: оно уходит в журнал.
        """
        if OS is None or not self.полоса_включена:
            return
        try:
            поток = threading.Thread(
                target=self._полоса_внутри, args=(dict(строка), tx),
                name="own-send", daemon=True)
            поток.start()
            self.полос_запусков += 1
            # Строка в журнал службы: без неё ни один разбор не отличает
            # "полосу не позвали" от "позвала и потеряла". Одна строка на
            # покупку -- это не шум.
            log.info("полоса: старт на %s", (строка.get("signature") or "")[:12])
        except Exception as exc:  # noqa: BLE001
            log.warning("полоса не запустилась: %s", type(exc).__name__)

    def полос_потоков_живых(self) -> int:
        """Сколько потоков полосы ещё не вернулись. Запусков больше путей и
        при этом живых потоков ноль -- значит поток умер, не дописав строку
        (например, службу перезапустили прямо в отправке)."""
        try:
            return sum(1 for п in threading.enumerate() if п.name == "own-send")
        except Exception:  # noqa: BLE001
            return -1

    def _тень_симуляции_в_журнал(self, запись: dict) -> None:
        """Вердикт симуляции, посчитанный ВНЕ пути сделки. Только в журнал:
        решения он не меняет и менять не должен -- защита сделки это min_out
        внутри самой транзакции."""
        try:
            self.полос_тень_сим += 1
            if not (запись or {}).get("would_pass", True):
                self.полос_тень_сим_отказов += 1
            self.состояние.log_decision(dict(запись or {}))
        except Exception:  # noqa: BLE001
            log.warning("тень симуляции полосы в журнал не легла")

    def _полоса_внутри(self, строка: dict, tx: dict | None) -> None:
        t0 = time.time()
        подпись_и = строка.get("signature") or ""
        запись = {"stage": "own_send", "signature": подпись_и,
                   "mint": строка.get("mint"), "source": строка.get("source")}
        try:
            рез = OS.провести(
                tx_источника=tx or {}, источник=строка.get("source") or "",
                минт=строка.get("mint") or "", состояние=self.состояние,
                blockhash=self.blockhash, blockhash_ts=self.blockhash_ts,
                проскальзывание=(float(self.исполнитель.slippage_pct) / 100.0
                                  if self.исполнитель is not None else 0.35),
                # КЛЮЧ ОПЕРАЦИИ -- от подписи источника, а не случайный: один
                # сигнал даёт одну отправку, даже если сигнал придёт дважды.
                ключ_операции=f"own-{подпись_и[:40]}",
                источник_подпись=подпись_и,
                источник_слот=строка.get("source_slot") or строка.get("slot"),
                rpc_call=self.helius.call,
                # ТЕНЬ СИМУЛЯЦИИ -- СВОИМ ПОТОКОМ И ТОЛЬКО В ЖУРНАЛ (решение
                # владельца 25.09). На пути сделки она стоила 52.95 мс из 89.39
                # и ровно из-за неё мы пришли в блок на 11.4 мс позже Bloom.
                тень_сим=self._тень_симуляции_в_журнал,
                # ГРУППА ИСТОЧНИКА решает размер сделки (0.05 или 0.01), нужен
                # ли веер и из какого суточного бюджета берётся расход.
                группа=self.группа_источника(строка.get("source")))
            запись.update(рез or {})
            # ЕДИНЫЙ НОЛЬ ДЛЯ ПАРЫ -- в позицию полосы, той же величиной, что и
            # у Bloom: время прихода сигнала источника на наш узел.
            цид = (рез or {}).get("cid")
            if цид:
                try:
                    self.состояние.update_position(
                        цид, signal_recv_ts=строка.get("t_recv_ts"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("ноль сигнала в позицию полосы не записан: %s",
                                type(exc).__name__)
            self.полос_путей += 1
            стадия = (рез or {}).get("stage") or "?"
            self.полос_по_стадиям[стадия] = self.полос_по_стадиям.get(стадия, 0) + 1
            if (рез or {}).get("sent"):
                self.полос_отправлено += 1
            # ПОЛОСА ВСТАЛА ИЗ-ЗА ДЕНЕЖНОЙ СТРАННОСТИ -- строкой владельцу
            # сразу, в основной чат. Молча остановившийся замер выглядит как
            # "сигналов нет", и это хуже, чем ошибка.
            стоп = (рез or {}).get("kill_set") or {}
            if стоп.get("ok") and not стоп.get("already"):
                if self.оповещатель is not None and NT is not None:
                    self.оповещатель.послать(NT.строка_тревоги(
                        "полоса своей отправки ОСТАНОВЛЕНА",
                        f"{стоп.get('why')}; снимать только владельцу: файл "
                        f"{self.состояние.kill_lane_path}"))
            self.полос_последняя = {
                "stage": стадия, "ok": bool((рез or {}).get("ok")),
                "why_not": (рез or {}).get("why_not"),
                "signature": (рез or {}).get("signature"),
                "pool_program": (рез or {}).get("pool_program"),
                "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        except Exception as exc:  # noqa: BLE001
            запись["why_not"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            self.полос_по_стадиям["crashed"] = (
                self.полос_по_стадиям.get("crashed", 0) + 1)
            log.exception("полоса упала на %s", подпись_и[:12])
        запись["own_send_total_ms"] = round((time.time() - t0) * 1000.0, 2)
        log.info("полоса: конец на %s -- %s %s", подпись_и[:12],
                 запись.get("stage") or "?",
                 (запись.get("why_not") or "")[:80])
        try:
            self.состояние.log_decision(запись)
        except Exception:  # noqa: BLE001
            log.exception("запись полосы в журнал не легла")

    def отметить_полосу_в_потоке(self, *, cid: str, поз: dict, подпись: str,
                                  слот, tx, t_recv: float,
                                  цепь_ок: bool | None = None) -> dict:
        """Транзакция ПОЛОСЫ увидена в потоке: её круг и купленное количество.

        Круг у полосы считается от ОТПРАВКИ (ts_sent), а не от ответа
        площадки: у своей отправки ответа площадки нет вовсе, и сравнивать
        надо путь "мы нажали -> видно в потоке" с чужим "Bloom ответил ->
        видно в потоке".

        Количество -- сырыми единицами из meta. Без него сторож продавать не
        станет: продать весь остаток минта нельзя, там лежит и покупка Bloom.
        """
        поля: dict = {"own_tx_seen_ts": round(t_recv, 6), "own_tx_seen_slot": слот}
        запись = {"stage": "lane_seen", "client_order_id": cid, "mint": поз.get("mint"),
                   "signature": подпись, "slot": слот, "lane": ST.МЕТКА_ПОЛОСЫ}
        отправлено_ts = поз.get("ts_sent")
        if отправлено_ts:
            поля["lane_send_to_seen_ms"] = round(
                (t_recv - float(отправлено_ts)) * 1000.0, 2)
            запись["lane_send_to_seen_ms"] = поля["lane_send_to_seen_ms"]
        решение_ts = поз.get("ts_intent")
        if решение_ts:
            поля["own_tx_seen_ms"] = round((t_recv - float(решение_ts)) * 1000.0, 2)
            запись["own_tx_seen_ms"] = поля["own_tx_seen_ms"]
        # КОШЕЛЁК ПОЛОСЫ, не исполнителя: с 25.09 полоса покупает своим
        # адресом, и приход токена надо искать на нём. По чужому адресу мы
        # получили бы "нашего счёта минта нет" и сторож не продал бы ничего.
        куплено = OS.купленное_raw(tx or {}, OS.кошелёк_полосы(),
                                    поз.get("mint") or "") if OS is not None else {}
        if куплено.get("ok"):
            поля["lane_bought_raw"] = куплено["raw"]
            поля["chain_ok"] = True
            запись["lane_bought_raw"] = куплено["raw"]
        else:
            # Замерная подписка часто приходит без meta: это НЕ повод писать
            # ноль. Количество догонит пульс по подписи, а пока его нет,
            # сторож продавать не станет -- и это правильно.
            запись["lane_bought_why_not"] = куплено.get("why_not")
            поля["lane_bought_why_not"] = куплено.get("why_not")
            if куплено.get("chain_ok") is False:
                поля["chain_ok"] = False
            elif цепь_ок is not None:
                # Вердикт цепи из meta известен и без количества: записать его
                # надо, иначе серия упавших у полосы не считается.
                поля["chain_ok"] = цепь_ок
        try:
            self.состояние.update_position(cid, **поля)
        except Exception as exc:  # noqa: BLE001
            log.warning("круг полосы в позицию не записан: %s", type(exc).__name__)
        try:
            self.состояние.log_decision(запись)
        except Exception:  # noqa: BLE001
            log.exception("запись круга полосы в журнал не легла")
        self.сообщить_пару(cid)
        return запись

    def догнать_купленное_полосы(self, *, предел: int = 3) -> dict:
        """Количество, купленное полосой, там где потока не хватило.

        ВНЕ горячего пути, с пульса: один getTransaction на позицию. Нужен,
        потому что замерная подписка часто приходит без meta, а сторожу нужно
        точное количество -- продавать остаток минта целиком нельзя.
        """
        итог = {"looked": 0, "filled": 0, "why_not": None}
        if OS is None or not self.полоса_включена:
            итог["why_not"] = "полоса выключена"
            return итог
        try:
            позиции = self.состояние.lane_positions()
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"позиции не прочитаны ({type(exc).__name__})"
            return итог
        for поз in позиции:
            if итог["looked"] >= предел:
                break
            if поз.get("lane_bought_raw") is not None:
                continue
            if поз.get("state") in (ST.STATE_CLOSED, "closed"):
                continue
            # СПРАШИВАТЬ НАДО ПРО СЕВШУЮ ПОДПИСЬ, А НЕ ПРО ПРИНЯТУЮ. Вариантов
            # на одном nonce шесть, садится один, а в lane_signature лежит тот,
            # чей сервис первым ответил "принял". За ночь 25->26.09 из-за этого
            # 61 пара из 63 осталась без количества и стала несчитаемой.
            варианты = [поз.get("lane_signature"),
                        поз.get("lane_signature_accepted_first")]
            варианты += list(поз.get("lane_pool_candidates") or [])
            варианты.append(поз.get("lane_signature_local"))
            варианты = list(dict.fromkeys([в for в in варианты if в]))
            if not варианты:
                continue
            попыток = int(поз.get("lane_bought_tries") or 0)
            if попыток >= ПОПЫТОК_МЕСТА_В_БЛОКЕ:
                continue
            итог["looked"] += 1
            cid = поз.get("client_order_id")
            подпись = варианты[0]
            села = {"signature": None, "err": None, "why_not": "одна подпись"}
            if len(варианты) > 1:
                try:
                    села = self.helius.севшая_подпись(варианты)
                except Exception as exc:  # noqa: BLE001
                    села = {"signature": None, "err": None,
                            "why_not": f"{type(exc).__name__}: {str(exc)[:80]}"}
                if села.get("signature"):
                    подпись = села["signature"]
            try:
                tx = self.helius.транзакция(подпись)
            except Exception as exc:  # noqa: BLE001
                tx = None
                причина = f"{type(exc).__name__}: {str(exc)[:120]}"
            else:
                причина = "" if tx else "узел транзакции не отдал"
            куплено = OS.купленное_raw(tx or {}, OS.кошелёк_полосы(),
                                        поз.get("mint") or "")
            поля = {"lane_bought_tries": попыток + 1}
            if села.get("signature"):
                поля["lane_landed_signature"] = села["signature"]
                if села.get("slot"):
                    поля["lane_landed_slot"] = села["slot"]
            # РАСХОД ПОКУПКИ ПО ЦЕПИ. Поля позиции не видят платы за создание
            # счетов: живая покупка 25.09 18:09:41Z стоила кошельку 0.053493440
            # при 0.052005 по полям. Раз транзакция уже в руках -- берём число
            # из неё, лишнего вызова это не стоит.
            натив = OS.натив_покупки(tx or {}, OS.кошелёк_полосы())
            if натив.get("ok"):
                поля["lane_buy_native_sol"] = натив["native_sol"]
                поля["lane_buy_fee_sol"] = натив["fee_sol"]
            if куплено.get("ok"):
                поля["lane_bought_raw"] = куплено["raw"]
                поля["chain_ok"] = True
                итог["filled"] += 1
                self.полос_куплено_догнано += 1
            else:
                поля["lane_bought_why_not"] = (причина or куплено.get("why_not"))
                if куплено.get("chain_ok") is False:
                    поля["chain_ok"] = False
                # ПОПЫТКИ ВЫШЛИ -- ИТОГ НЕСЧИТАЕМ (уточнение владельца 25.09).
                # Количество так и не добралось: посчитать итог этой пары
                # нечем. Это не утечка и не повод останавливать полосу --
                # пара помечается, и замер скорости у неё остаётся годным.
                if попыток + 1 >= ПОПЫТОК_МЕСТА_В_БЛОКЕ and not поз.get(
                        "result_uncountable"):
                    OS.отметить_итог_несчитаемым(
                        self.состояние, cid,
                        f"количество полосы не добралось за {попыток + 1} попыток: "
                        f"{поля['lane_bought_why_not']}")
                    итог["uncountable"] = int(итог.get("uncountable") or 0) + 1
            try:
                self.состояние.update_position(cid, **поля)
            except Exception as exc:  # noqa: BLE001
                log.warning("количество полосы в позицию не легло: %s",
                            type(exc).__name__)
        return итог

    def _хвост_журнала(self, путь, *, байт: int = 1_500_000) -> list:
        """Последние строки журнала. Читаем ХВОСТ, а не весь файл: журнал
        решений растёт весь день, и вычитывать его целиком на каждом пульсе
        значит тратить время процесса на то, что и так не нужно."""
        try:
            размер = путь.stat().st_size
        except OSError:
            return []
        try:
            with путь.open("rb") as f:
                if размер > байт:
                    f.seek(размер - байт)
                    f.readline()          # первая строка может быть обрезана
                куски = f.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        строки = []
        for с in куски.split("\n"):
            с = с.strip()
            if not с:
                continue
            try:
                строки.append(json.loads(с))
            except ValueError:
                continue
        return строки

    def догнать_цены_пропусков(self, *, предел: int = 2,
                                минимум_возраст_s: float = 35.0) -> dict:
        """ТЕНЬ ПРОПУСКОВ узкого фильтра: цена на S+1 и через 28.8 с.

        Слово владельца 25.09: "По каждому пропуску тень: цена на S+1 по цепи и
        через 28.8 с". Это единственный способ узнать, сберёг ли фильтр деньги
        или отнял сделку -- по самому факту пропуска не видно ничего.

        ВНЕ горячего пути, с пульса, и с пределом на круг: один пропуск стоит
        около пяти вызовов (транзакция источника, подписи пула, две сделки).
        """
        итог = {"looked": 0, "measured": 0, "why_not": None}
        if PC is None:
            итог["why_not"] = "модуль кривой цены не загружен"
            return итог
        строки = self._хвост_журнала(self.состояние.decisions_path)
        # ИЗМЕРЕН -- значит в записи есть точки цены. Прежде "измеренным"
        # считалась любая запись тени пропуска, в том числе неудачная: ночью
        # 25.09 первая же тень упала (TypeError в вызове пула), запись легла,
        # и пропуск больше никогда не измерялся. Неудачные попытки считаем и
        # после трёх прекращаем: каждая стоит около пяти кредитов.
        уже = {с.get("signature") for с in строки
               if с.get("stage") == "skip_price" and с.get("points")}
        попыток: dict = {}
        for с in строки:
            if с.get("stage") == "skip_price" and not с.get("points"):
                п = с.get("signature")
                попыток[п] = попыток.get(п, 0) + 1
        сейчас = time.time()
        пропуски = []
        for с in строки:
            if с.get("code") != КОД_НАЛОГ_МАРШРУТА or с.get("action") != "skip":
                continue
            подпись = с.get("signature")
            if not подпись or подпись in уже:
                continue
            if попыток.get(подпись, 0) >= ПОПЫТОК_ТЕНИ_ПРОПУСКА:
                continue
            ts = с.get("ts") or с.get("ts_utc")
            # Возраст берём по времени записи журнала: если его нет, считаем,
            # что ждать больше нечего -- запись всё равно старая.
            возраст = None
            if isinstance(ts, (int, float)):
                возраст = сейчас - float(ts)
            if возраст is not None and возраст < минимум_возраст_s:
                continue
            пропуски.append(с)
        for с in пропуски[:предел]:
            итог["looked"] += 1
            запись = {"stage": "skip_price", "signature": с.get("signature"),
                       "mint": с.get("mint"), "source": с.get("source"),
                       "skip_code": с.get("code"),
                       "route_transfer_fee_bps": с.get("route_transfer_fee_bps")}
            try:
                tx_и = self.helius.транзакция(с.get("signature"))
                if not tx_и:
                    запись["why_not"] = "узел не отдал транзакцию источника"
                else:
                    вх = PC.цена_входа_источника(tx_и, с.get("mint") or "",
                                                  кошелёк=с.get("source"))
                    # Пул: сначала тот, что уже назван в записи пропуска
                    # (тогда сети не нужно), иначе поиск по транзакции
                    # источника. ИМЯ ПАРАМЕТРА -- кошелёк_источника: с
                    # "кошелёк" вызов падал TypeError уже на живом пропуске
                    # 25.09, и тень пропуска молча ничего не мерила.
                    пул = с.get("source_pool")
                    if not пул:
                        выбор = PC.пул_для_кривой(
                            self.helius, минт=с.get("mint") or "",
                            tx_источника=tx_и,
                            кошелёк_источника=с.get("source")) or {}
                        # пул_для_кривой отдаёт СЛОВАРЬ, а не адрес: адрес
                        # лежит в "pool", и откуда он взят -- в "pool_from".
                        пул = выбор.get("pool")
                        запись["pool_from"] = выбор.get("pool_from")
                        запись["pool_kind"] = выбор.get("pool_kind")
                        запись["pool_why_not"] = выбор.get("why_not")
                    if not пул:
                        запись["why_not"] = ("пул для кривой не найден: "
                                              + str(запись.get("pool_why_not") or "")[:120])
                    else:
                        кр = PC.кривая(
                            self.helius, минт=с.get("mint") or "", пул=пул,
                            слот_источника=int(с.get("slot") or 0),
                            время_источника=(tx_и or {}).get("blockTime"),
                            цена_входа=вх.get("price") if вх.get("known") else None,
                            квота_входа=вх.get("quote") if вх.get("known") else None,
                            точки_блоков=(1,), точки_секунд=(28.8,))
                        запись["entry"] = {k: вх.get(k) for k in
                                            ("known", "price", "quote", "why_not")}
                        запись["points"] = кр.get("points")
                        запись["pool"] = пул
                        запись["curve_known"] = кр.get("known")
                        # ИЗМЕРЕНО -- только если точки есть. Кривая может
                        # честно отказать (нет сделок пула у цели, узел молчит),
                        # и тогда её причину надо ЗАПИСАТЬ: 25.09 она терялась,
                        # и в докладе стояло "неизвестна" без слова почему.
                        if кр.get("points"):
                            итог["measured"] += 1
                        else:
                            запись["why_not"] = str(
                                кр.get("why_not") or "кривая не дала точек")[:160]
            except Exception as exc:  # noqa: BLE001
                запись["why_not"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            try:
                self.состояние.log_decision(запись)
            except Exception:  # noqa: BLE001
                log.exception("запись тени пропуска не легла")
        return итог

    def сообщить_пару(self, cid_полосы: str) -> dict:
        """Пара "Bloom против нашей" -- строкой владельцу, один раз на пару.

        Сравниваются ДВА замера одного и того же сигнала: когда в потоке
        появилась покупка Bloom и когда -- наша. Раньше та, у которой
        own_tx_seen_ts меньше. Пока второй половины пары нет, строка не
        уходит: половина пары -- это не сравнение.
        """
        итог = {"sent": False, "why_not": None}
        try:
            позиции = self.состояние.positions() or {}
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"позиции не прочитаны ({type(exc).__name__})"
            return итог
        наша = позиции.get(cid_полосы) or {}
        if наша.get("lane_pair_reported"):
            итог["why_not"] = "пара уже доложена"
            return итог
        источник = наша.get("source_sig")
        if not источник:
            итог["why_not"] = "у позиции полосы нет подписи источника"
            return итог
        блум = None
        for п in позиции.values():
            if п.get("lane") or п.get("source_sig") != источник:
                continue
            if п.get("own_tx_seen_ts"):
                блум = п
                break
        if блум is None:
            итог["why_not"] = "покупки Bloom по этому сигналу в потоке ещё не видно"
            return итог
        if not наша.get("own_tx_seen_ts"):
            итог["why_not"] = "нашей транзакции в потоке ещё не видно"
            return итог
        разница = round((float(блум["own_tx_seen_ts"])
                          - float(наша["own_tx_seen_ts"])) * 1000.0, 2)
        кто = "мы раньше" if разница > 0 else "Bloom раньше"

        def от_сигнала(п):
            """Круг от ЕДИНОГО нуля -- прихода сигнала источника на наш узел.

            Поле own_tx_seen_ms у каждой стороны считается от СВОЕГО ts_intent,
            и сравнивать их между собой нельзя: полоса пишет свой ts_intent уже
            после сборки. 25.09 это выглядело как "мы раньше на 59 мс" при
            настоящем отставании 11.4 мс.
            """
            ноль, видно = п.get("signal_recv_ts"), п.get("own_tx_seen_ts")
            if not ноль or not видно:
                return None
            return round((float(видно) - float(ноль)) * 1000.0, 2)

        от_сигнала_наша = от_сигнала(наша)
        от_сигнала_блум = от_сигнала(блум)

        def нет(значение, единица=""):
            """Пусто -- это прочерк, а не ноль: ноль здесь читался бы числом."""
            return "—" if значение is None else f"{значение}{единица}"

        def место(п):
            if п.get("block_index") is None:
                return f"место {нет(п.get('block_why_not') or 'ещё не добрано')}"
            return f"место {п.get('block_index')}/{нет(п.get('block_total'))}"

        def отставание(п):
            их, наш = п.get("source_slot"), п.get("own_tx_seen_slot")
            if not isinstance(их, int) or not isinstance(наш, int):
                return "отставание от источника —"
            return f"отставание от источника {наш - их} слот(ов)"

        def цена(п, куплено_поле):
            """Цена входа: SOL за миллион сырых единиц. Сравнима у двух сторон
            только потому, что минт один и знаков после запятой у него одно
            число. Нет количества -- нет и цены, выдумывать её нельзя."""
            вх, куплено = п.get("sol_in"), п.get(куплено_поле)
            if not вх or not isinstance(куплено, int) or куплено <= 0:
                return "цена входа —"
            return (f"цена входа {round(float(вх) / куплено * 1_000_000, 9)} SOL"
                     f" за 1 млн ед. ({вх} SOL за {куплено})")

        текст = (f"🏁 пара «Bloom против нашей» по {(источник or '')[:12]}\n"
                  f"минт {(наша.get('mint') or '')[:12]}\n"
                  f"наша: слот {нет(наша.get('own_tx_seen_slot'))}, {место(наша)}, "
                  f"{отставание(наша)}\n"
                  f"  от прихода сигнала {нет(от_сигнала_наша, ' мс')}, "
                  f"от своего решения {нет(наша.get('own_tx_seen_ms'), ' мс')}, "
                  f"от отправки {нет(наша.get('lane_send_to_seen_ms'), ' мс')}, "
                  f"{цена(наша, 'lane_bought_raw')}\n"
                  f"  подпись {(наша.get('lane_signature') or '')[:12]}\n"
                  f"Bloom: слот {нет(блум.get('own_tx_seen_slot'))}, {место(блум)}, "
                  f"{отставание(блум)}\n"
                  f"  от прихода сигнала {нет(от_сигнала_блум, ' мс')}, "
                  f"от своего решения {нет(блум.get('own_tx_seen_ms'), ' мс')}, "
                  f"от ответа Bloom {нет(блум.get('bloom_to_seen_ms'), ' мс')}, "
                  f"bloom_ms {нет(блум.get('bloom_ms'))}\n"
                  f"  {цена(блум, 'bought_raw')}\n"
                  f"{кто} на {abs(разница)} мс (по часам нашего узла)")
        # Помечаем ДО отправки: вторая строка о той же паре хуже, чем ни
        # одной, а Telegram может ответить ошибкой уже после доставки.
        try:
            self.состояние.update_position(cid_полосы, lane_pair_reported=True,
                                            lane_pair_delta_ms=разница)
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"пометка пары не легла ({type(exc).__name__})"
            return итог
        self.состояние.log_decision({"stage": "lane_pair",
                                      "client_order_id": cid_полосы,
                                      "signature": источник,
                                      "lane_pair_delta_ms": разница,
                                      # Оба круга от ЕДИНОГО нуля -- прихода
                                      # сигнала: только их и можно сравнивать
                                      # между собой.
                                      "lane_from_signal_ms": от_сигнала_наша,
                                      "bloom_from_signal_ms": от_сигнала_блум,
                                      "lane_slot": наша.get("own_tx_seen_slot"),
                                      "bloom_slot": блум.get("own_tx_seen_slot")})
        if self.оповещатель is not None and NT is not None:
            self.оповещатель.послать(текст)
            итог["sent"] = True
        else:
            итог["why_not"] = "оповещатель не подключён"
        итог["delta_ms"] = разница
        итог["text"] = текст
        return итог

    def отметить_контроль_сервиса(self, *, cid: str, поз: dict, сервис: str,
                                   запись_сервиса: dict, подпись: str, слот,
                                   t_recv: float) -> dict:
        """Контроль ОДНОГО отправителя увиден в потоке (веер З2 по сервисам).

        Один и тот же пустой байт уходит разными путями в одну и ту же
        миллисекунду; здесь считается, за сколько он доехал у КАЖДОГО. Это и
        есть ответ на вопрос владельца "кто довозит быстрее" -- по цепи, а не
        по обещаниям документации.

        Пишется в отдельное поле позиции: путать это с основным контролем
        (Helius Sender на пути полосы) нельзя -- у него своя строка в паре.
        """
        поля_с: dict = {"seen_ts": round(t_recv, 6), "seen_slot": слот,
                         "signature": подпись}
        отправлен = (запись_сервиса or {}).get("ts_sent")
        if отправлен:
            поля_с["send_to_seen_ms"] = round(
                (t_recv - float(отправлен)) * 1000.0, 2)
        их = поз.get("source_slot")
        if isinstance(их, int) and isinstance(слот, int):
            поля_с["slot_offset"] = слот - их
        запись = {"stage": "lane_control_sender_seen", "client_order_id": cid,
                   "sender": сервис, "signature": подпись, "slot": слот,
                   "lane": ST.МЕТКА_ПОЛОСЫ, **поля_с}
        try:
            # ПОД ЗАМКОМ И ПО СВЕЖЕЙ ПОЗИЦИИ: словарь пишется целиком, а рядом
            # его пишет догон места в блоке.
            with self.замок_контролей:
                свежая = (self.состояние.positions() or {}).get(cid) or поз
                видно = dict(свежая.get("control_senders_seen") or {})
                видно[сервис] = {**(видно.get(сервис) or {}), **поля_с}
                self.состояние.update_position(cid, control_senders_seen=видно)
        except Exception as exc:  # noqa: BLE001
            запись["why_not"] = f"позиция не записана: {type(exc).__name__}"
        self.состояние.log_decision(запись)
        self.контролей_сервисов_видно += 1
        log.info("контроль через %s виден: %s, от отправки %s мс, слот %s",
                 сервис, (подпись or "")[:12], поля_с.get("send_to_seen_ms"),
                 слот)
        return запись

    @staticmethod
    def лучший_контроль(поз: dict) -> dict:
        """Самый быстрый контроль пары из веера -- как «нога сравнения».

        Отдельного «основного» контроля больше нет (решение владельца 25.09), а
        сравнивать покупку надо с ЧЕМ-ТО одним. Берём лучший путь: пустая
        транзакция по самому быстрому из доступных путей -- это и есть нижняя
        граница «сколько заняла бы дорога без пула».
        """
        видно = поз.get("control_senders_seen") or {}
        лучший, лучшее = None, None
        for сервис, з in видно.items():
            if not isinstance(з, dict):
                continue
            мс = з.get("send_to_seen_ms")
            if мс is None:
                continue
            if лучшее is None or float(мс) < лучшее:
                лучшее, лучший = float(мс), (сервис, з)
        if лучший is None:
            return {}
        сервис, з = лучший
        return {"sender": сервис, "ms": лучшее, "slot": з.get("seen_slot"),
                "block_index": з.get("block_index"),
                "block_total": з.get("block_total")}

    def свод_победителей_пула(self) -> dict:
        """Кто по факту довёз покупку полосы -- по позициям, а не по обещаниям.

        Победитель -- тот сервис, чей ответ пришёл первым; в цепь садится одна
        и та же подпись, кто бы её ни довёз, поэтому это замер путей, а не
        "кто купил".
        """
        из_: dict = {}
        try:
            позиции = self.состояние.positions() or {}
        except Exception:  # noqa: BLE001
            return из_
        for п in позиции.values():
            кто = п.get("lane_pool_winner")
            if кто:
                из_[кто] = int(из_.get(кто) or 0) + 1
        return из_

    def свод_контролей_сервисов(self) -> dict:
        """Кто довозит быстрее -- по всем парам, что уже увидены.

        Медиана, а не среднее: один медленный путь не имеет права выглядеть
        как общее правило, а один быстрый -- как заслуга.
        """
        по_сервисам: dict = {}
        try:
            позиции = self.состояние.positions() or {}
        except Exception:  # noqa: BLE001
            return {}
        for п in позиции.values():
            for сервис, з in (п.get("control_senders_seen") or {}).items():
                мс = (з or {}).get("send_to_seen_ms")
                if мс is None:
                    continue
                по_сервисам.setdefault(сервис, {"ms": [], "offsets": []})
                по_сервисам[сервис]["ms"].append(float(мс))
                см = (з or {}).get("slot_offset")
                if isinstance(см, int):
                    по_сервисам[сервис]["offsets"].append(см)
        из_: dict = {}
        for сервис, з in по_сервисам.items():
            ряд = sorted(з["ms"])
            n = len(ряд)
            медиана = (ряд[n // 2] if n % 2
                        else round((ряд[n // 2 - 1] + ряд[n // 2]) / 2, 2))
            из_[сервис] = {"n": n, "deliver_ms_median": round(медиана, 2),
                            "deliver_ms_best": round(ряд[0], 2),
                            "slot_offset_median": (
                                sorted(з["offsets"])[len(з["offsets"]) // 2]
                                if з["offsets"] else None)}
        return из_

    def догнать_места_контролей(self, *, предел: int = 3) -> dict:
        """Место КАЖДОГО контроля веера в его блоке -- тем же одним getBlock.

        Отдельным проходом, а не внутри места покупки: у каждого контроля своя
        подпись, свой слот и свои попытки. Место нужно ровно за тем же, зачем у
        покупки: "доехал раньше" и "сел раньше" -- разные вещи, и на второе
        отвечает только место в блоке.
        """
        итог = {"looked": 0, "filled": 0, "gave_up": 0, "why_not": ""}
        try:
            позиции = self.состояние.positions()
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"{type(exc).__name__}"
            return итог
        кандидаты = []
        for cid, p_ in (позиции or {}).items():
            видно = p_.get("control_senders_seen") or {}
            for сервис, з in видно.items():
                if not isinstance(з, dict) or з.get("block_index") is not None:
                    continue
                if int(з.get("block_tries") or 0) >= ПОПЫТОК_МЕСТА_В_БЛОКЕ:
                    continue
                подпись, слот = з.get("signature"), з.get("seen_slot")
                if not подпись or not isinstance(слот, int):
                    continue
                кандидаты.append((cid, сервис, подпись, слот))
        кандидаты.sort(key=lambda x: (x[0], x[1]))
        for cid, сервис, подпись, слот in кандидаты[:предел]:
            итог["looked"] += 1
            try:
                import bloom_block_position as BP  # noqa: PLC0415

                м = BP.место_по_подписям(self.helius, слот, подпись)
            except Exception as exc:  # noqa: BLE001
                м = {"known": False,
                     "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
            try:
                with self.замок_контролей:
                    свежие = (self.состояние.positions() or {}).get(cid) or {}
                    видно = dict(свежие.get("control_senders_seen") or {})
                    з = dict(видно.get(сервис) or {})
                    з["block_tries"] = int(з.get("block_tries") or 0) + 1
                    if м.get("known"):
                        з.update(block_index=м.get("index"),
                                  block_total=м.get("total"))
                        итог["filled"] += 1
                    else:
                        з["block_why_not"] = str(м.get("why_not") or "")[:200]
                        if з["block_tries"] >= ПОПЫТОК_МЕСТА_В_БЛОКЕ:
                            итог["gave_up"] += 1
                    видно[сервис] = з
                    self.состояние.update_position(cid,
                                                    control_senders_seen=видно)
            except Exception as exc:  # noqa: BLE001
                log.warning("место контроля %s: позиция не записана (%s)",
                            сервис, type(exc).__name__)
        return итог

    def догнать_очередь_пары(self, *, предел: int = 1) -> dict:
        """Толпа В ПУЛЕ в нашем слоте и следующем -- и чем она платила (З2).

        Вопрос владельца буквально: сколько транзакций пришло в пул в этом
        слоте и следующем и какой у них приоритет (медиана и 90-й процентиль)
        против нашего. Два getBlock на пару -- два кредита, и потому предел
        на круг пульса стоит единицей.
        """
        итог = {"looked": 0, "filled": 0, "why_not": ""}
        try:
            import bloom_queue_price as QP  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"модуль очереди не загружен: {type(exc).__name__}"
            return итог
        try:
            позиции = self.состояние.positions()
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"{type(exc).__name__}"
            return итог
        кандидаты = []
        for cid, p_ in (позиции or {}).items():
            if p_.get("lane") != ST.МЕТКА_ПОЛОСЫ or p_.get("queue_done"):
                continue
            if int(p_.get("queue_tries") or 0) >= ПОПЫТОК_МЕСТА_В_БЛОКЕ:
                continue
            подпись = p_.get("lane_signature") or p_.get("lane_signature_local")
            слот = p_.get("our_slot") or p_.get("own_tx_seen_slot")
            if not подпись or not isinstance(слот, int) or not p_.get("mint"):
                continue
            кандидаты.append((cid, подпись, слот, p_["mint"]))
        кандидаты.sort(key=lambda x: x[0])
        for cid, подпись, слот, минт in кандидаты[:предел]:
            итог["looked"] += 1
            попытки = int((позиции.get(cid) or {}).get("queue_tries") or 0) + 1
            поля = {"queue_tries": попытки}
            try:
                наша_tx = self.helius.транзакция(подпись)
                пул = QP.адреса_пула_из_покупки(наша_tx or {}, минт=минт,
                                                 наш_кошелёк=OS.кошелёк_полосы()
                                                 if OS is not None else "")
                if not пул.get("ok"):
                    raise RuntimeError(f"пул не выведен: {пул.get('why_not')}")
                адреса = set(пул["vaults"]) | set(пул["owners"])
                ряды = {}
                for сдвиг in (0, 1):
                    оч = QP.очередь_пула(QP.блок(self.helius.call, слот + сдвиг),
                                          минт=минт, адреса_пула=адреса)
                    ряды[сдвиг] = оч
                наша_строка = next(
                    (с for с in (ряды[0].get("rows") or [])
                     if с.get("signature") == подпись), None)
                поля.update(
                    queue_done=True,
                    queue_pool_tx_slot=ряды[0].get("n_pool_tx"),
                    queue_pool_tx_next_slot=ряды[1].get("n_pool_tx"),
                    queue_our_pay_lamports=(наша_строка or {}).get("pay_lamports"),
                    queue_our_micro_per_cu=(наша_строка or {}).get(
                        "paid_micro_per_cu_consumed"))
                цены = [с.get("paid_micro_per_cu_consumed")
                        for сд in (0, 1)
                        for с in (ряды[сд].get("rows") or [])
                        if с.get("paid_micro_per_cu_consumed") is not None
                        and с.get("signature") != подпись]
                if цены:
                    цены.sort()
                    поля["queue_micro_per_cu_median"] = цены[len(цены) // 2]
                    поля["queue_micro_per_cu_p90"] = QP._процентиль(цены, 0.9)
                    поля["queue_n_priced"] = len(цены)
                итог["filled"] += 1
            except Exception as exc:  # noqa: BLE001
                поля["queue_why_not"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            try:
                self.состояние.update_position(cid, **поля)
            except Exception as exc:  # noqa: BLE001
                log.warning("очередь пары: позиция не записана (%s)",
                            type(exc).__name__)
        return итог

    def догнать_место_источника(self, *, предел: int = 3) -> dict:
        """Место ИСТОЧНИКА в его блоке -- один getBlock уровня подписей.

        Владелец 25.09: в разложении доставки нужен и момент посадки
        источника -- слот и место. Слот у нас есть с самого сигнала, места не
        было: без него не видно, сел ли источник в начале своего блока или в
        конце, а это половина ответа на вопрос "сколько мы ждали слот".
        """
        итог = {"looked": 0, "filled": 0, "why_not": ""}
        try:
            позиции = self.состояние.positions()
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"{type(exc).__name__}"
            return итог
        кандидаты = []
        for cid, p_ in (позиции or {}).items():
            if p_.get("lane") != ST.МЕТКА_ПОЛОСЫ:
                continue
            if p_.get("source_block_index") is not None:
                continue
            if int(p_.get("source_block_tries") or 0) >= ПОПЫТОК_МЕСТА_В_БЛОКЕ:
                continue
            подпись, слот = p_.get("source_sig"), p_.get("source_slot")
            if not подпись or not isinstance(слот, int):
                continue
            кандидаты.append((cid, подпись, слот))
        кандидаты.sort(key=lambda x: x[0])
        for cid, подпись, слот in кандидаты[:предел]:
            итог["looked"] += 1
            попытки = int((позиции.get(cid) or {}).get("source_block_tries") or 0) + 1
            try:
                import bloom_block_position as BP  # noqa: PLC0415

                м = BP.место_по_подписям(self.helius, слот, подпись)
            except Exception as exc:  # noqa: BLE001
                м = {"known": False,
                     "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
            поля = {"source_block_tries": попытки}
            if м.get("known"):
                поля.update(source_block_index=м.get("index"),
                             source_block_total=м.get("total"))
                итог["filled"] += 1
            else:
                поля["source_block_why_not"] = str(м.get("why_not") or "")[:200]
            try:
                self.состояние.update_position(cid, **поля)
            except Exception as exc:  # noqa: BLE001
                log.warning("место источника: позиция не записана (%s)",
                            type(exc).__name__)
        return итог

    def разложение_пары(self, поз: dict) -> dict:
        """Разложение "источник сел -> мы сели" на измеримые куски (З2).

        ЧЕСТНО О ГРАНИЦАХ. Момент посадки источника в миллисекундах нашему
        узлу неизвестен: подписка отдаёт транзакцию уже после того, как слот
        обработан, и вычесть одно из другого нельзя. Поэтому у источника мы
        знаем СЛОТ и МЕСТО в блоке, а время раскладываем от той точки,
        которая у нас есть по своим часам, -- от прихода сигнала на наш узел:

          * обнаружение -- приход сигнала -> наше решение;
          * сборка и подпись -- решение -> отправка;
          * доставка -- отправка -> наша транзакция видна в подписке;
          * то же для контроля: он ушёл тем же путём и без касания пула;
          * ожидание слота -- разница слотов и мест в блоках.
        """
        def мс(а, б):
            if not а or not б:
                return None
            return round((float(б) - float(а)) * 1000.0, 2)

        сигнал = поз.get("signal_recv_ts")
        из_ = {
            "source_slot": поз.get("source_slot"),
            "source_block_index": поз.get("source_block_index"),
            "source_block_total": поз.get("source_block_total"),
            "detect_ms": мс(сигнал, поз.get("ts_intent")),
            "build_sign_ms": мс(поз.get("ts_intent"), поз.get("ts_sent")),
            "deliver_ms": поз.get("lane_send_to_seen_ms"),
            # КОНТРОЛЬ -- лучший путь веера (самый быстрый сервис): именно он
            # отвечает на вопрос "сколько заняла бы дорога без пула".
            "control_deliver_ms": (self.лучший_контроль(поз) or {}).get("ms"),
            "control_sender": (self.лучший_контроль(поз) or {}).get("sender"),
            "signal_to_seen_ms": мс(сигнал, поз.get("own_tx_seen_ts")),
            "our_slot": поз.get("own_tx_seen_slot"),
            "our_block_index": поз.get("block_index"),
            "our_block_total": поз.get("block_total"),
            "control_slot": (self.лучший_контроль(поз) or {}).get("slot"),
            "control_block_index": (self.лучший_контроль(поз) or {}).get("block_index"),
            "slots_behind": None,
            "control_slots_behind": None,
            "note": ("момент посадки источника в мс нашему узлу неизвестен: "
                      "у него известны слот и место в блоке"),
        }
        их = поз.get("source_slot")
        if isinstance(их, int):
            if isinstance(поз.get("own_tx_seen_slot"), int):
                из_["slots_behind"] = поз["own_tx_seen_slot"] - их
            слот_к = (self.лучший_контроль(поз) or {}).get("slot")
            if isinstance(слот_к, int):
                из_["control_slots_behind"] = слот_к - их
        return из_

    def свод_контролей(self, *, минимум_пар: int = 10) -> dict:
        """Итог по десяти и более парам с контролем -- медианами (З2).

        Один раз на каждые `минимум_пар`: доложили десять -- считаем и шлём,
        дальше копим следующие. Медиана, а не среднее: один выброс в путь на
        секунду не должен перекрасить вывод.
        """
        итог = {"sent": False, "pairs": 0, "why_not": None}
        try:
            позиции = self.состояние.positions() or {}
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"позиции не прочитаны ({type(exc).__name__})"
            return итог
        пары = [p for p in позиции.values()
                if p.get("lane") == ST.МЕТКА_ПОЛОСЫ
                and (self.лучший_контроль(p) or {}).get("ms") is not None
                and p.get("lane_send_to_seen_ms") is not None]
        итог["pairs"] = len(пары)
        if len(пары) < минимум_пар:
            итог["why_not"] = (f"пар с контролем {len(пары)} из {минимум_пар} -- "
                                "рано считать")
            return итог
        уже = int((self.состояние.counters() or {}).get(
            "lane_control_summary_at", 0) or 0)
        if len(пары) < уже + минимум_пар:
            итог["why_not"] = f"итог по {уже} парам уже доложен"
            return итог

        def медиана(поле):
            ряд = sorted(float(p[поле]) for p in пары if p.get(поле) is not None)
            if not ряд:
                return None
            с = len(ряд) // 2
            return round(ряд[с] if len(ряд) % 2 else (ряд[с - 1] + ряд[с]) / 2, 2)

        раскл = [self.разложение_пары(p) for p in пары]

        def медиана_раскл(поле):
            ряд = sorted(float(р[поле]) for р in раскл if р.get(поле) is not None)
            if not ряд:
                return None
            с = len(ряд) // 2
            return round(ряд[с] if len(ряд) % 2 else (ряд[с - 1] + ряд[с]) / 2, 2)

        покупка = медиана("lane_send_to_seen_ms")
        контроль = медиана_раскл("control_deliver_ms")
        разница = (None if покупка is None or контроль is None
                    else round(покупка - контроль, 2))
        вывод = "—"
        if разница is not None:
            вывод = ("ОЧЕРЕДЬ К ПУЛУ: контроль садится заметно быстрее покупки"
                      if разница > 50 else
                      ("ПУТЬ: контроль не быстрее покупки -- задержка не в пуле"
                       if разница <= 15 else
                       "ПОПОЛАМ: разница есть, но небольшая"))
        итог.update(
            buy_deliver_ms_median=покупка, control_deliver_ms_median=контроль,
            delta_ms=разница, verdict=вывод,
            detect_ms_median=медиана_раскл("detect_ms"),
            build_sign_ms_median=медиана_раскл("build_sign_ms"),
            signal_to_seen_ms_median=медиана_раскл("signal_to_seen_ms"),
            slots_behind_median=медиана_раскл("slots_behind"))
        текст = (f"📊 контроль доставки: итог по {len(пары)} парам\n"
                  f"доставка покупки, медиана {покупка} мс\n"
                  f"доставка контроля, медиана {контроль} мс\n"
                  f"разница {разница} мс -> {вывод}\n"
                  f"обнаружение (приход сигнала -> решение) {итог['detect_ms_median']} мс\n"
                  f"сборка и подпись {итог['build_sign_ms_median']} мс\n"
                  f"от прихода сигнала до нашей посадки {итог['signal_to_seen_ms_median']} мс\n"
                  f"отставание по слотам, медиана {итог['slots_behind_median']}")
        try:
            с = self.состояние.counters() or {}
            с["lane_control_summary_at"] = len(пары)
            self.состояние.save_counters(с)
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"пометка итога не легла ({type(exc).__name__})"
            return итог
        self.состояние.log_decision({"stage": "lane_control_summary",
                                      "pairs": len(пары), **{
                                          к: итог.get(к) for к in
                                          ("buy_deliver_ms_median",
                                           "control_deliver_ms_median", "delta_ms",
                                           "verdict", "detect_ms_median",
                                           "build_sign_ms_median",
                                           "signal_to_seen_ms_median",
                                           "slots_behind_median")}})
        if self.оповещатель is not None and NT is not None:
            self.оповещатель.послать(текст)
            итог["sent"] = True
        else:
            итог["why_not"] = "оповещатель не подключён"
        итог["text"] = текст
        return итог

    def досказать_контроли(self, *, предел: int = 5) -> dict:
        """Строки пар с контролем, которые в момент посадки были неполны.

        Контроль часто виден РАНЬШЕ, чем добраны места в блоке и толпа в
        пуле: тогда строка откладывается. Здесь она досылается -- иначе
        замер был бы, а владелец его не увидел.
        """
        итог = {"looked": 0, "sent": 0}
        try:
            позиции = self.состояние.positions() or {}
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"{type(exc).__name__}"
            return итог
        # Берём и пары, где не видно НИ ОДНОГО контроля: по истечении срока
        # строка всё равно должна уйти, с прочерками и списком не доехавших.
        # Раньше такая пара не попадала сюда вовсе, и доклада по ней не было
        # никогда -- при том, что деньги на веер уже потрачены.
        ждут = [cid for cid, п in позиции.items()
                if п.get("lane") == ST.МЕТКА_ПОЛОСЫ
                and not п.get("control_pair_reported")
                and ((п.get("control_senders_seen") or {})
                      or (п.get("own_tx_seen_ts")
                          and (п.get("control_services") or [])))]
        for cid in sorted(ждут)[:предел]:
            итог["looked"] += 1
            if (self.сообщить_контроль(cid) or {}).get("sent"):
                итог["sent"] += 1
        return итог

    def сообщить_контроль(self, cid_полосы: str) -> dict:
        """Строка КОНТРОЛЕЙ ПО СЕРВИСАМ -- владельцу, один раз на пару.

        Отвечает на вопрос владельца "кто довозит быстрее": одна и та же пустая
        транзакция ушла в одну и ту же миллисекунду разными путями, и здесь
        видно, у кого какой круг, какой слот и какое место в блоке. Рядом --
        покупка полосы тем же способом счёта: от СВОЕЙ отправки до появления в
        подписке; сравнивать можно только одно и то же.

        Отдельного "основного" контроля больше нет (решение владельца 25.09:
        Helius уже в веере), поэтому строка строится по вееру.
        """
        итог = {"sent": False, "why_not": None}
        try:
            поз = (self.состояние.positions() or {}).get(cid_полосы) or {}
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"позиции не прочитаны ({type(exc).__name__})"
            return итог
        if поз.get("control_pair_reported"):
            итог["why_not"] = "пара с контролем уже доложена"
            return итог
        if not поз.get("own_tx_seen_ts"):
            итог["why_not"] = "покупки полосы в потоке ещё не видно"
            return итог
        видно = поз.get("control_senders_seen") or {}
        отправлено = [з for з in (поз.get("control_services") or [])
                       if (з or {}).get("sent")]
        # СКОЛЬКО МЫ УЖЕ ЖДЁМ. Считается от момента, как в потоке увидели саму
        # покупку: контроли уходят рядом с ней, и к исходу срока они либо
        # сели, либо не сядут никогда. Срок нужен потому, что счёт попыток
        # может не кончиться: попытки растут только у ТЕХ контролей, что уже
        # видно, а не доехавший в видно не попадает вовсе.
        ждём_s = None
        try:
            ждём_s = time.time() - float(поз.get("own_tx_seen_ts") or 0)
        except (TypeError, ValueError):
            ждём_s = None
        пора = ждём_s is not None and ждём_s >= СРОК_ОЖИДАНИЯ_ПАРЫ_S
        if not видно and not пора:
            причины = "; ".join(f"{(з or {}).get('sender')}: {(з or {}).get('why_not')}"
                                 for з in (поз.get("control_services") or [])
                                 if (з or {}).get("why_not"))
            итог["why_not"] = (f"контролей в потоке ещё не видно ({причины})"
                                if причины else "контролей в потоке ещё не видно")
            return итог
        # Ждём, пока увидим ВСЕ отправленные контроли и их места, но не вечно:
        # попыток столько же, сколько у самого догона. Исчерпаны -- шлём с
        # прочерками, а не молчим.
        все_видны = len(видно) >= max(1, len(отправлено))
        мест_нет = (поз.get("block_index") is None
                     or any((з or {}).get("block_index") is None
                            for з in видно.values()))
        попыток = min([int(поз.get("block_tries") or 0)]
                      + [int((з or {}).get("block_tries") or 0)
                         for з in видно.values()])
        # СРОК (посчитан выше) обрывает ожидание: строка уходит с прочерками,
        # и в ней видно, кто не доехал. Молчать нельзя -- замер без доклада
        # владельцу это не замер.
        if (not все_видны or мест_нет) and попыток < ПОПЫТОК_МЕСТА_В_БЛОКЕ \
                and not пора:
            итог["why_not"] = ("ждём остальные контроли и места в блоке"
                                if not все_видны else "место в блоке ещё добирается")
            return итог

        def нет(значение, единица=""):
            return "—" if значение is None else f"{значение}{единица}"

        def смещение(слот):
            их = поз.get("source_slot")
            if not isinstance(их, int) or not isinstance(слот, int):
                return "—"
            return f"S+{слот - их}"

        покупка_мс = поз.get("lane_send_to_seen_ms")
        строки_сервисов = []
        # Порядок -- по кругу доставки: первым тот, кто довёз быстрее. Это и
        # есть ответ на вопрос владельца, и он должен читаться с первой строки.
        def круг_сервиса(пара):
            """Нет круга -- в конец списка: прочерк не имеет права выглядеть
            самым быстрым."""
            з = пара[1] if isinstance(пара[1], dict) else {}
            мс = з.get("send_to_seen_ms")
            return float(мс) if мс is not None else float("inf")

        for сервис, з in sorted(видно.items(), key=круг_сервиса):
            з = з or {}
            строки_сервисов.append(
                f"  {сервис}: {нет(з.get('send_to_seen_ms'), ' мс')}, "
                f"{смещение(з.get('seen_slot'))}, место "
                f"{нет(з.get('block_index'))}/{нет(з.get('block_total'))}")
        не_дошли = [f"  {(з or {}).get('sender')}: не доехал ({(з or {}).get('why_not')})"
                     for з in (поз.get("control_services") or [])
                     if (з or {}).get("sent") and (з or {}).get("sender") not in видно]
        быстрейший = None
        круги = {с: (з or {}).get("send_to_seen_ms") for с, з in видно.items()
                  if isinstance(з, dict) and (з or {}).get("send_to_seen_ms") is not None}
        if круги:
            быстрейший = min(круги, key=круги.get)
        довёз = поз.get("lane_pool_winner")
        текст = (f"🧪 контроли по сервисам по {(поз.get('source_sig') or '')[:12]}\n"
                  f"минт {(поз.get('mint') or '')[:12]}\n"
                  f"покупка полосы: {нет(покупка_мс, ' мс')} от отправки, "
                  f"{смещение(поз.get('own_tx_seen_slot'))}, место "
                  f"{нет(поз.get('block_index'))}/{нет(поз.get('block_total'))}"
                  + (f", довёз {довёз}" if довёз else "") + "\n"
                  + "\n".join(строки_сервисов + не_дошли)
                  + (f"\nбыстрее всех довёз {быстрейший}" if быстрейший else "")
                  + f"\nв пуле за наш слот {нет(поз.get('queue_pool_tx_slot'))} тх, "
                    f"за следующий {нет(поз.get('queue_pool_tx_next_slot'))}; "
                    f"приоритет мкл/CU: наш {нет(поз.get('queue_our_micro_per_cu'))}, "
                    f"медиана {нет(поз.get('queue_micro_per_cu_median'))}, "
                    f"90-й {нет(поз.get('queue_micro_per_cu_p90'))}")
        try:
            self.состояние.update_position(cid_полосы, control_pair_reported=True,
                                            control_fastest_sender=быстрейший)
        except Exception as exc:  # noqa: BLE001
            итог["why_not"] = f"пометка пары не легла ({type(exc).__name__})"
            return итог
        self.состояние.log_decision({"stage": "lane_control_pair",
                                      "client_order_id": cid_полосы,
                                      "signature": поз.get("source_sig"),
                                      "buy_send_to_seen_ms": покупка_мс,
                                      "buy_slot": поз.get("own_tx_seen_slot"),
                                      "pool_winner": довёз,
                                      "fastest_sender": быстрейший,
                                      "by_sender": {с: {
                                          "send_to_seen_ms": (з or {}).get("send_to_seen_ms"),
                                          "slot": (з or {}).get("seen_slot"),
                                          "block_index": (з or {}).get("block_index")}
                                          for с, з in видно.items()}})
        if self.оповещатель is not None and NT is not None:
            self.оповещатель.послать(текст)
            итог["sent"] = True
        else:
            итог["why_not"] = "оповещатель не подключён"
        итог["text"] = текст
        итог["fastest"] = быстрейший
        return итог

    def отметить_нашу_транзакцию(self, подпись: str, слот, tx, t_recv: float):
        """Наша покупка увидена в потоке: считаем два круга в миллисекундах.

        t_decision -> t_bloom_resp -- это наш bloom_ms, он уже записан
        исполнителем при ответе Bloom. Здесь добавляется второй:
        t_decision -> t_own_tx_seen, то есть от решения до появления нашей
        транзакции в подписке (processed). Разница между ними и есть путь
        Bloom от ответа до включения в блок.

        Упавшая покупка считается ТАК ЖЕ и помечается: она тоже доехала до
        блока, просто с ошибкой, и её круг -- такое же число.
        """
        self.наших_транзакций += 1
        поз = None
        cid = None
        try:
            for к, п in (self.состояние.positions() or {}).items():
                if подпись in (п.get("signatures") or []):
                    поз, cid = п, к
                    break
                # ПОЛОСА ПОДПИСЫВАЕТ САМА: её подпись лежит отдельным полем,
                # а signatures заполняет только ответ Bloom. Без этой ветки
                # своя же транзакция считалась бы "позиции нет" и её круг
                # пропадал бы вместе с количеством для сторожа.
                if подпись in (п.get("lane_signature"),
                                п.get("lane_signature_local")):
                    поз, cid = п, к
                    break
                # ВАРИАНТ ПУЛА НА NONCE: подписей несколько (по одной на
                # отправителя со своими чаевыми), а сядет РОВНО ОДНА -- первая
                # сдвигает nonce. Какая именно, знает только цепь, поэтому
                # узнаём её по списку кандидатов. Без этой ветки своя же
                # покупка считалась бы чужой транзакцией, и сторож не нашёл бы
                # что продавать.
                if подпись in (п.get("lane_pool_candidates") or []):
                    поз, cid = п, к
                    break
                # КОНТРОЛИ ДОСТАВКИ -- тоже наши транзакции, но НЕ покупки:
                # у каждой свой круг и свои поля, и считать их покупкой значило
                # бы затереть замер покупки замером пустышки. Отдельного
                # "основного" контроля больше нет (решение владельца 25.09:
                # Helius уже в веере), поэтому ветка одна -- по вееру, где у
                # каждого отправителя своя подпись.
                свой = None
                for зп in (п.get("control_services") or []):
                    if подпись and подпись == (зп or {}).get("signature"):
                        свой = зп
                        break
                if свой is not None:
                    return self.отметить_контроль_сервиса(
                        cid=к, поз=п, сервис=свой.get("sender") or "?",
                        запись_сервиса=свой, подпись=подпись, слот=слот,
                        t_recv=t_recv)
        except Exception as exc:  # noqa: BLE001
            log.warning("позиции для замера круга не прочитаны: %s",
                        type(exc).__name__)
        запись = {"stage": "own_tx_seen", "code": КОД_НАША_ТРАНЗАКЦИЯ,
                   "signature": подпись, "slot": слот,
                   "client_order_id": cid,
                   "t_own_tx_seen_ts": round(t_recv, 6)}
        мета = (tx or {}).get("meta") if isinstance(tx, dict) else None
        if isinstance(мета, dict):
            запись["chain_ok"] = мета.get("err") is None
            запись["chain_err"] = мета.get("err")
        if поз is None:
            # Транзакция наша, но позиции по ней нет: продажа, спасение или
            # ручная отправка. Круг тут не считается -- от чего его считать.
            запись["why_not"] = "позиции с такой подписью нет: круг не от чего считать"
            self.состояние.log_decision(запись)
            return запись
        # ПОЗИЦИЯ ПОЛОСЫ считается иначе: у неё нет ответа площадки, зато
        # есть своя отправка и своё количество. Строку в журнал пишет сама
        # ветка полосы -- два ряда на одно событие читать невозможно.
        if поз.get("lane") == ST.МЕТКА_ПОЛОСЫ:
            return self.отметить_полосу_в_потоке(
                cid=cid, поз=поз, подпись=подпись, слот=слот, tx=tx,
                t_recv=t_recv, цепь_ок=запись.get("chain_ok"))
        решение_ts = поз.get("ts_intent")
        ответ_ts = поз.get("ts_accepted")
        if решение_ts:
            запись["t_decision_ts"] = round(float(решение_ts), 6)
            запись["own_tx_seen_ms"] = round((t_recv - float(решение_ts)) * 1000.0, 2)
        if ответ_ts:
            запись["t_bloom_resp_ts"] = round(float(ответ_ts), 6)
            запись["bloom_to_seen_ms"] = round((t_recv - float(ответ_ts)) * 1000.0, 2)
        if поз.get("bloom_ms") is not None:
            запись["bloom_ms"] = поз.get("bloom_ms")
        self.состояние.log_decision(запись)
        try:
            # ИМЯ ОДНО. Прежде здесь писалось own_tx_seen_chain_ok, а все
            # потребители -- пределы полосы, доклад, таблица кругов -- читают
            # chain_ok. Вердикт физически лежал в позиции и ни до кого не
            # доходил. Второе имя оставлено как было: на него смотрят уже
            # написанные разборы, и ломать их незачем.
            поля = {"own_tx_seen_ts": round(t_recv, 6),
                     "own_tx_seen_ms": запись.get("own_tx_seen_ms"),
                     "bloom_to_seen_ms": запись.get("bloom_to_seen_ms"),
                     "own_tx_seen_slot": слот,
                     "own_tx_seen_chain_ok": запись.get("chain_ok")}
            # chain_ok пишется ТОЛЬКО когда он известен: замерная подписка
            # часто приходит без meta, и записать туда None значило бы
            # затереть вердикт, уже добытый разбором покупки.
            if "chain_ok" in запись:
                поля["chain_ok"] = запись["chain_ok"]
            # КУПЛЕННОЕ КОЛИЧЕСТВО у покупки Bloom. Нужно ровно для одного:
            # сравнить цену входа с полосой на том же минте. Считается из той
            # же meta, что уже пришла, ни одного вызова сети. Нет meta --
            # поля просто нет: ноль тут читался бы как "купили ноль".
            if OS is not None and isinstance(мета, dict):
                куплено_б = OS.купленное_raw(tx or {}, ST.EXECUTOR_WALLET,
                                              поз.get("mint") or "")
                if куплено_б.get("ok"):
                    поля["bought_raw"] = куплено_б["raw"]
                    запись["bought_raw"] = куплено_б["raw"]
            self.состояние.update_position(cid, **поля)
        except Exception as exc:  # noqa: BLE001
            log.warning("круг в позицию не записан: %s", type(exc).__name__)
        log.info("наша покупка в потоке: %s, от решения %s мс, от ответа Bloom "
                  "%s мс%s", (подпись or "")[:12], запись.get("own_tx_seen_ms"),
                  запись.get("bloom_to_seen_ms"),
                  "" if запись.get("chain_ok", True) else " (УПАЛА)")
        return запись

    def назначить_разбор_нашей_покупки(self, **кв) -> None:
        """В отдельном потоке, если есть цикл событий; иначе сразу.

        Синхронный вызов оставлен не для удобства, а чтобы самопроверка и
        разовый прогон из командной строки шли тем же кодом, что служба.
        """
        try:
            цикл = asyncio.get_running_loop()
        except RuntimeError:
            self.разобрать_нашу_покупку(**кв)
            return
        цикл.run_in_executor(None, lambda: self.разобрать_нашу_покупку(**кв))

    def разобрать_нашу_покупку(self, *, cid: str, минт: str, подпись: str,
                                источник_маршрут: dict,
                                источник_пул: str | None = None,
                                exec_row: dict | None = None,
                                слот_источника: int | None = None,
                                имя_источника: str | None = None) -> dict:
        """Пул и маршрут нашей покупки; метка при расхождении с источником."""
        запись = {"stage": "our_route", "client_order_id": cid, "mint": минт,
                   "signature": подпись}
        try:
            tx = self.helius.транзакция(подпись)
        except Exception as exc:  # noqa: BLE001
            tx = None
            запись["why_not"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        if not tx:
            запись.setdefault("why_not", "нашей транзакции узел не отдал")
            self.состояние.log_decision(запись)
            return запись

        # ПОСАДКА СЧИТАЕТСЯ ТОЛЬКО ПРИ meta.err == null. 24.09 в 14:10:08Z
        # владелец получил "🟢 покупка SENT ... S+0 по цепи", а транзакция в
        # цепи упала: токен не пришёл, позиция была пустой с рождения, и
        # сторож через 68 с доложил "продана" по нулевому остатку. Слот у
        # упавшей транзакции есть -- поэтому "S+N по цепи" считался и
        # печатался как доказательство посадки. Он им не является.
        мета = tx.get("meta") or {}
        ошибка_цепи = мета.get("err")
        запись["chain_ok"] = ошибка_цепи is None
        запись["our_slot"] = tx.get("slot")
        # НАЛОГ ПО НАШЕМУ МАРШРУТУ (слово владельца 25.09, пункт 3). До покупки
        # фильтр считал маршрут ИСТОЧНИКА -- иначе было нечего считать. Здесь
        # видно, каким маршрутом пошли МЫ и сколько налога взяли с нас. Разница
        # между этими двумя числами и есть то, чего фильтр знать не мог.
        if RT is not None:
            try:
                свой = RT.налог_маршрута(tx, минт, self.helius.налог_минта,
                                          откуда="ours")
                запись["our_route_transfer_fee_bps"] = свой.get("route_transfer_fee_bps")
                запись["our_route_transfers_of_token"] = свой.get("transfers_of_token")
                запись["our_route_taxed_intermediates"] = [
                    з.get("mint") for з in (свой.get("taxed_intermediates") or [])]
                запись["our_token_fee_bps"] = свой.get("token_fee_bps")
                if свой.get("why_not"):
                    запись["our_route_tax_why_not"] = свой["why_not"]
            except Exception as exc:  # noqa: BLE001
                запись["our_route_tax_why_not"] = f"{type(exc).__name__}"
        if ошибка_цепи is not None:
            запись["code"] = КОД_ПОКУПКА_УПАЛА
            запись["chain_err"] = ошибка_цепи
            журнал_прог = мета.get("logMessages") or []
            запись["logs"] = [с[:200] for с in журнал_прог
                               if any(сл in с.lower() for сл in
                                       ("error", "failed", "slippage",
                                        "insufficient", "exceeded"))][:6]
            self.состояние.log_decision(запись)
            # Позиция закрывается СРАЗУ и с честной причиной: иначе сторож
            # будет ждать продажи того, чего не покупали, и закроет её по
            # нулевому остатку как "продано".
            try:
                self.состояние.update_position(
                    cid, state=ST.STATE_CLOSED, chain_ok=False,
                    our_slot=tx.get("slot"),
                    closed_reason=("покупка упала по цепи: "
                                    + json.dumps(ошибка_цепи, ensure_ascii=False)[:120]))
            except Exception as exc:  # noqa: BLE001
                log.warning("позицию упавшей покупки не закрыть: %s",
                            type(exc).__name__)
            log.error("ПОКУПКА УПАЛА по цепи: %s, слот %s, ошибка %s",
                      (подпись or "")[:12], tx.get("slot"),
                      json.dumps(ошибка_цепи, ensure_ascii=False)[:120])
            if self.оповещатель is not None and NT is not None:
                try:
                    размер = self.состояние.positions().get(cid, {}).get("sol_in")
                except Exception:  # noqa: BLE001
                    размер = None
                self.оповещатель.послать(NT.строка_покупки_упала(
                    exec_row=exec_row or {}, наш_слот=tx.get("slot"),
                    слот_источника=слот_источника, ошибка=ошибка_цепи,
                    размер_sol=размер, источник=имя_источника))
            return запись

        наш = пул_и_прямизна(tx, минт=минт, кошелёк=ST.EXECUTOR_WALLET)
        источник_прямой = bool(источник_пул) and not (источник_маршрут or {}).get(
            "via_intermediate")
        флаги = []
        if источник_прямой and not наш["direct"]:
            флаги.append(ФЛАГ_МАРШРУТ_РАЗОШЁЛСЯ)
        запись.update(our_pool=наш["pool"], our_pool_direct=наш["direct"],
                       our_pool_why_not=наш["why_not"],
                       our_route={k: наш["route"].get(k) for k in
                                   ("programs", "intermediate_mints", "hops_by_mints",
                                    "dex_calls", "via_intermediate")},
                       our_slot=tx.get("slot"),
                       source_pool=источник_пул,
                       source_route={k: (источник_маршрут or {}).get(k) for k in
                                      ("programs", "intermediate_mints",
                                       "hops_by_mints", "dex_calls",
                                       "via_intermediate")},
                       source_direct=источник_прямой, flags=флаги)
        self.состояние.log_decision(запись)
        try:
            # chain_ok=True пишется ЗДЕСЬ, в ветке удачи. Прежде его писала
            # только ветка падения (chain_ok=False), и у всех севших покупок
            # поле оставалось пустым: 24.09 у пяти боевых покупок с 17:12Z
            # оно было null при meta.err=null по цепи. Пустое поле читается
            # как "неизвестно", и любой счёт, который ищет удачу, её не
            # находил -- в том числе обрыв серии упавших у полосы своей
            # отправки. Ошибка цепи выше уже вернула управление, значит сюда
            # приходят только транзакции с meta.err == null.
            self.состояние.update_position(
                cid,
                our_route_transfer_fee_bps=запись.get("our_route_transfer_fee_bps"),
                our_route_transfers_of_token=запись.get("our_route_transfers_of_token"),
                our_token_fee_bps=запись.get("our_token_fee_bps"),
                chain_ok=True, our_pool=наш["pool"],
                our_pool_direct=наш["direct"],
                our_route_programs=",".join(наш["route"].get("programs") or []),
                our_route_hops=наш["route"].get("hops_by_mints"),
                our_slot=tx.get("slot"), flags=",".join(флаги))
        except Exception as exc:  # noqa: BLE001
            log.warning("маршрут в позицию не записан: %s", type(exc).__name__)
        if флаги:
            log.warning("ROUTE_MISMATCH: источник одним пулом %s, наш маршрут "
                        "%s хопов через %s", источник_пул,
                        наш["route"].get("hops_by_mints"),
                        наш["route"].get("intermediate_mints"))
        # Строка о нашей покупке идёт ОТСЮДА, а не сразу после отправки:
        # только здесь известны наш слот по цепи и метка расхождения
        # маршрутов, а без них строка была бы наполовину пустой.
        if exec_row is not None and self.оповещатель is not None:
            try:
                размер = self.состояние.positions().get(cid, {}).get("sol_in")
            except Exception:  # noqa: BLE001
                размер = None
            self.оповещатель.послать(NT.строка_покупки(
                exec_row=exec_row, наш_слот=tx.get("slot"),
                слот_источника=слот_источника, размер_sol=размер, метки=флаги,
                источник=имя_источника))
        return запись


def тревога_запасного(*, запасной_с: float | None, сейчас: float,
                       последняя: float, порог: float, повтор: float) -> tuple:
    """Пора ли сказать владельцу, что детектор работает вполсилы.

    Слово владельца 25.09: три часа работы на запасном пути без единого
    сигнала больше быть не должно. Поэтому тревога идёт не при уходе (уход
    бывает на секунды и лечится сам), а когда возврат НЕ СЛУЧИЛСЯ за порог, и
    повторяется, пока не вернулись.

    Чистая функция: возвращает (сколько секунд на запасном или None, новое
    время последней тревоги). Решение о тексте и отправке -- снаружи.
    """
    if not запасной_с:
        return None, последняя
    на_запасном = сейчас - запасной_с
    if на_запасном < порог:
        return None, последняя
    if (сейчас - последняя) < повтор:
        return None, последняя
    return на_запасном, сейчас


def пора_вернуться_на_основной(atlas: bool, запасной_с: float | None, *,
                                сейчас: float, окно: float | None = None) -> bool:
    """Вышло ли окно запасного пути. Отдельной функцией -- чтобы проверялось.

    Решение НЕ зависит от здоровья запасного соединения: именно эта
    зависимость и держала службу на logsSubscribe три часа 24.09. Пока мы на
    основном пути (atlas) -- возвращаться некуда, ответ всегда нет.
    """
    if atlas or запасной_с is None:
        return False
    окно = ОКНО_ЗАПАСНОГО_S if окно is None else окно
    return (сейчас - запасной_с) >= окно


def адреса_подписки(детектор: "Детектор") -> list:
    """Полный список адресов подписки одной строкой, чтобы его можно было
    проверить без сети.

    Три разных назначения в одном списке, и путать их нельзя:
      * источники -- сигналы, по ним покупаем;
      * наш кошелёк и кошелёк полосы -- только замер круга; адрес полосы
        обязателен отдельной строкой: с 25.09 она покупает своим кошельком, и
        без подписки на него её транзакций в потоке не видно вовсе -- ни
        круга, ни купленного количества, ни места в блоке;
      * хранилища четырёх котировочных пулов -- только корм кэшу шаблонов
        первого шага для двухшаговой тени.
    Подписка и так идёт по адресу на запрос, поэтому каждый адрес приходит
    со своим номером подписки, и спутать назначение невозможно.
    """
    хранилища = (set() if getattr(детектор, "ног_отключён", False)
                  else set(детектор.кэш_ног_адреса))
    полоса = {OS.кошелёк_полосы()} if OS is not None else set()
    return sorted(set(детектор.источники) | {ST.EXECUTOR_WALLET} | полоса
                  | хранилища)


async def _подписка_транзакций(ws, адреса: list) -> None:
    for i, a in enumerate(адреса, 1):
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": i, "method": "transactionSubscribe",
            "params": [{"accountInclude": [a], "failed": False, "vote": False},
                        {"commitment": "processed", "transactionDetails": "full",
                         "encoding": "jsonParsed", "showRewards": False,
                         "maxSupportedTransactionVersion": ПОТОЛОК_ВЕРСИИ_TX}]}))


async def _подписка_логов(ws, адреса: list) -> None:
    for i, a in enumerate(адреса, 1):
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
            "params": [{"mentions": [a]}, {"commitment": "processed"}]}))


async def слушать(детектор: Детектор, ключ: str, *, стоп_через_s: float | None = None) -> None:
    if websockets is None:
        raise RuntimeError("нет модуля websockets")
    дедлайн = (time.time() + стоп_через_s) if стоп_через_s else None
    backoff, atlas = 1.0, True
    # ЗАПАСНОЙ ПУТЬ ОГРАНИЧЕН ПО ВРЕМЕНИ, А НЕ ПО ЧИСЛУ ПОПЫТОК. Раньше в
    # коде было написано "на одну попытку", но возврат стоял в ветке ОТКАЗА:
    # если запасное соединение держалось, atlas оставался False навсегда.
    # 24.09 между 18:48 и 19:12 служба так и залипла на logsSubscribe: за три
    # часа кэш шаблонов получил НОЛЬ пригодных уведомлений против 547 216
    # пустых, каждый сигнал стал стоить getTransaction (578 вызовов) на
    # commitment confirmed -- то есть слот-два задержки, и все четыре
    # вечерние покупки сели в S+2 вместо S+0/S+1. Теперь возврат идёт по
    # часам и не зависит от здоровья запасного соединения.
    запасной_с = None
    # КОГДА ОСНОВНОЙ ПУТЬ УПАЛ В ПЕРВЫЙ РАЗ В ЭТОЙ СЕРИИ. Решение владельца
    # 25.09 (вечер): при обрыве (1001 going away) переподключать
    # transactionSubscribe НЕМЕДЛЕННО, а запасной канал поднимать только если
    # основной не встал за СРОК_ПОДЪЁМА_ОСНОВНОГО_S. Обрыв обычно лечится
    # одним переподключением за доли секунды, а уход на запасной стоит слота
    # на каждом сигнале: 25.09 из 94 сигналов, разобранных через
    # getTransaction, все 94 пришлись на окна запасного пути.
    основной_упал_с = None
    while True:
        if дедлайн and time.time() > дедлайн:
            return
        # НАШ КОШЕЛЁК -- ОТДЕЛЬНОЙ ПОДПИСКОЙ в том же соединении: подписка
        # здесь и так идёт по одному адресу на запрос, и так надёжнее --
        # его сообщения приходят со своим номером подписки, спутать их с
        # источником невозможно. Стоимость -- несколько сообщений в час.
        адреса = адреса_подписки(детектор)
        поколение = детектор.поколение
        метод = "transactionSubscribe" if atlas else "logsSubscribe"
        if not адреса:
            log.warning("список источников пуст -- подписываться не на что")
            await asyncio.sleep(5)
            continue
        try:
            async with websockets.connect(ws_url(ключ, atlas), ping_interval=20,
                                           ping_timeout=30, max_size=16 * 1024 * 1024) as ws:
                if atlas:
                    await _подписка_транзакций(ws, адреса)
                else:
                    await _подписка_логов(ws, адреса)
                id_адреса = {i: a for i, a in enumerate(адреса, 1)}
                подписка_адреса: dict = {}
                подтверждено, отказы = 0, []
                ws_байт = ws_учтено = 0
                # ПРИЁМ С ТАЙМАУТОМ, А НЕ `async for`. Разница денежная:
                # в `async for` проверки ниже выполнялись ТОЛЬКО когда
                # приходило сообщение. Ночью источники молчат, и на запасном
                # пути мы висели часами, потому что окно возврата некому было
                # проверить: замер 25.09 по журналу за сутки -- 40 переходов,
                # 31 % времени на запасном, медиана эпизода 311 с при окне
                # 120 с и один эпизод длиной 3.1 часа. Теперь тишина -- это
                # просто тик: раз в ТИК_ПОДПИСКИ_S мы проверяем срок, смену
                # источников и окно возврата, ничего не получив.
                # ПОЧЕМУ ВЫШЛИ ИЗ ЦИКЛА -- ЗАПОМИНАЕМ. Без этого выход по
                # НАШЕМУ решению (сменились источники, вышло окно запасного)
                # попадал в строку ниже и поднимал RuntimeError "сервер закрыл
                # соединение". Дальше внешний обработчик считал это обрывом и
                # СНОВА уводил на запасной путь -- ровно поэтому 25.09 в
                # журнале за сутки 34 "сервера закрыл соединение", 40 переходов
                # и медиана эпизода 311 с при окне 120 с: возврат на основной
                # тут же отменялся собственным ложным обрывом.
                намеренно = None
                while True:
                    if дедлайн and time.time() > дедлайн:
                        return
                    if детектор.поколение != поколение:
                        log.info("список источников изменился -- переподписываюсь")
                        намеренно = "сменились источники"
                        break
                    if пора_вернуться_на_основной(atlas, запасной_с,
                                                    сейчас=time.time()):
                        был_на_запасном = time.time() - запасной_с
                        log.info("окно запасного logsSubscribe вышло (%.0f с) "
                                 "-- возвращаюсь на transactionSubscribe",
                                 был_на_запасном)
                        детектор.тревога_возврата_на_основной(был_на_запасном)
                        atlas = True
                        запасной_с = None
                        детектор.запасной_путь_с = None
                        намеренно = "вышло окно запасного"
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(),
                                                      timeout=ТИК_ПОДПИСКИ_S)
                    except asyncio.TimeoutError:
                        # ТИШИНА -- НЕ ОБРЫВ. Живость соединения держит сам
                        # websockets своими ping/pong (ping_interval=20,
                        # ping_timeout=30): если сервер молчит на пинг, recv
                        # поднимет ConnectionClosed, и это уже обрыв. Пустой
                        # тик значит только "сделок нет".
                        детектор.тиков_тишины += 1
                        continue
                    t_получено = time.time()   # до разбора json, а не после
                    ws_байт += len(raw) if isinstance(raw, (str, bytes)) else 0
                    if ws_байт - ws_учтено >= 0.1 * 1024 * 1024:
                        детектор.helius.учесть_вебсокет(ws_байт - ws_учтено)
                        ws_учтено = ws_байт
                    msg = json.loads(raw)
                    if "id" in msg and ("result" in msg or "error" in msg):
                        if "error" in msg:
                            отказы.append(msg["error"])
                        else:
                            подтверждено += 1
                            a = id_адреса.get(msg["id"])
                            if a and isinstance(msg.get("result"), int):
                                подписка_адреса[msg["result"]] = a
                        if подтверждено == 1:
                            детектор.способ = метод
                            if atlas:
                                детектор.запасной_путь_с = None
                                # Основной поднялся -- серия обрывов закрыта.
                                основной_упал_с = None
                            log.info("РАБОТАЕТ %s (хост %s), источников %d",
                                     метод, "atlas" if atlas else "mainnet", len(адреса))
                            детектор.признак_жизни()
                        if подтверждено == 0 and len(отказы) >= len(адреса):
                            raise RuntimeError(f"{метод}: все подписки отклонены: "
                                                f"{str(отказы[:1])[:200]}")
                        continue
                    m = msg.get("method")
                    if m not in ("transactionNotification", "logsNotification"):
                        continue
                    params = msg.get("params") or {}
                    res = params.get("result") or {}
                    источник = подписка_адреса.get(params.get("subscription"))
                    if источник in детектор.кэш_ног_адреса:
                        детектор.учесть_байты_ног(
                            len(raw) if isinstance(raw, (str, bytes)) else 0)
                    if m == "logsNotification":
                        val = res.get("value") or {}
                        if val.get("err") is not None:
                            continue
                        sig = val.get("signature")
                        слот = (res.get("context") or {}).get("slot")
                        if isinstance(sig, str) and SIG_RE.match(sig):
                            await детектор.обработать_бережно(
                                sig, слот if isinstance(слот, int) else None,
                                источник, "logsSubscribe", None, t_получено)
                        continue
                    sig, слот = подпись_и_слот(res)
                    tx = res.get("transaction") if isinstance(res.get("transaction"), dict) else None
                    if tx is not None and "meta" not in tx:
                        tx = None            # пришла форма без meta -- добираем по RPC
                    if sig:
                        await детектор.обработать_бережно(sig, слот, источник, метод,
                                                           tx, t_получено)
                backoff = 1.0
                if намеренно:
                    log.info("переподключаюсь по своему решению: %s", намеренно)
                    continue
                if детектор.поколение != поколение:
                    continue
                raise RuntimeError(f"{метод}: сервер закрыл соединение")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            детектор.обрывов += 1
            детектор.способ = None
            детектор.признак_жизни()
            log.warning("подписка (%s) оборвалась: %s: %s", метод,
                        type(exc).__name__, str(exc)[:200])
            детектор.учесть_обрыв(причина_обрыва(exc), f"{type(exc).__name__}: "
                                   f"{str(exc)[:200]}")
            # transactionSubscribe -- ОСНОВНОЙ способ: он несёт всю
            # транзакцию (разбор без RPC, отставание 0 слотов) и приходит
            # даже чуть раньше логов. logsSubscribe -- запасной НА ОДНУ
            # попытку: после неё снова пробуем основной, иначе один обрыв
            # навсегда сажал бы нас на путь, где каждый сигнал стоит слота.
            if atlas:
                # СПЕРВА НЕМЕДЛЕННОЕ ПЕРЕПОДКЛЮЧЕНИЕ ОСНОВНОГО (решение
                # владельца 25.09 вечера). Запасной путь не несёт транзакцию,
                # и каждый сигнал на нём стоит getTransaction и слота-двух
                # задержки -- уходить туда из-за обрыва, который лечится
                # переподключением за полсекунды, значит платить слотами за
                # ничто.
                if основной_упал_с is None:
                    основной_упал_с = time.time()
                прошло_с_обрыва = time.time() - основной_упал_с
                if прошло_с_обрыва < СРОК_ПОДЪЁМА_ОСНОВНОГО_S:
                    log.info("обрыв основного: переподключаюсь сразу "
                              "(с первого обрыва %.1f с из %.0f)",
                              прошло_с_обрыва, СРОК_ПОДЪЁМА_ОСНОВНОГО_S)
                    continue
                log.warning("основной не поднялся за %.0f с -- запасной "
                            "logsSubscribe не дольше %.0f с",
                            СРОК_ПОДЪЁМА_ОСНОВНОГО_S, ОКНО_ЗАПАСНОГО_S)
                atlas = False
                запасной_с = time.time()
                детектор.запасной_путь_с = запасной_с
                детектор.тревога_ухода_на_запасной()
                await asyncio.sleep(1)
                continue
            atlas = True
            был_на_запасном = (time.time() - запасной_с) if запасной_с else None
            запасной_с = None
            детектор.запасной_путь_с = None
            детектор.тревога_возврата_на_основной(был_на_запасном)
            log.info("возвращаюсь на основной transactionSubscribe")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# Сколько ДЕРЖИМ запасной logsSubscribe, прежде чем вернуться на основной
# путь. Было две минуты; решение владельца 25.09 (вечер) -- "120 с ожидания
# убрать". Пять секунд: запасной путь теперь мост на время, пока основной
# поднимается, а не режим работы. Жить на запасном дорого и медленно -- он не
# несёт транзакцию, и каждый сигнал стоит getTransaction на commitment
# confirmed, то есть слот-два задержки.
ОКНО_ЗАПАСНОГО_S = ST.env_float("BLOOM_FALLBACK_WINDOW_S", 5.0)

# СКОЛЬКО ДАЁМ ОСНОВНОМУ ПУТИ ПОДНЯТЬСЯ, прежде чем вообще идти на запасной.
# Решение владельца 25.09 (вечер): при обрыве переподключать
# transactionSubscribe немедленно, запасной -- только если основной не встал
# за пять секунд.
СРОК_ПОДЪЁМА_ОСНОВНОГО_S = ST.env_float("BLOOM_MAIN_REVIVE_S", 5.0)

# Через сколько секунд на запасном пути бить тревогу в основной чат и как
# часто её повторять, пока не вернулись. Две минуты -- слово владельца.
ТРЕВОГА_ЗАПАСНОГО_S = ST.env_float("BLOOM_FALLBACK_ALARM_S", 120.0)

# Как часто сводка решений в основной чат. Слово владельца: раз в час.
СВОДКА_КАЖДЫЕ_S = ST.env_float("BLOOM_SUMMARY_EVERY_S", 3600.0)

# Сколько раз пробуем добрать место в блоке. Блок может уйти из доступных
# узлу, и вечно его дёргать незачем -- в отчёте останется причина.
ПОПЫТОК_МЕСТА_В_БЛОКЕ = ST.env_int("BLOOM_BLOCK_POS_TRIES", 3)
# КАК ЧАСТО ГРЕЕМ СОЕДИНЕНИЯ ОТПРАВИТЕЛЕЙ. Владелец 25.09: раз в 20-30 с. У
# 0slot таймаут простоя 65 с (его письмо), поэтому 25 с -- с двойным запасом.
OS_ПРОГРЕВ_S = ST.env_float("BLOOM_SENDER_WARM_S", 25.0)
# СРОК ОЖИДАНИЯ СТРОКИ ПАРЫ. Дольше этого не ждём ни контролей, ни мест:
# строка уходит с прочерками. Полторы минуты -- это заведомо больше, чем
# нужно сети (контроль садится за секунды, место добирается за три попытки
# фонового круга), и заведомо меньше, чем "молчать до утра". Ноль в
# переменной окружения означает "ждать только по попыткам", как было раньше.
СРОК_ОЖИДАНИЯ_ПАРЫ_S = ST.env_int("BLOOM_PAIR_WAIT_S", 90) or 10 ** 9
# Сколько раз пробовать измерить цену одного пропуска, если попытка не дала
# точек. Три: неудача бывает и от молчания узла, и от ошибки в коде, но
# бесконечно платить кредитами за один пропуск нельзя.
ПОПЫТОК_ТЕНИ_ПРОПУСКА = ST.env_int("BLOOM_SKIP_PRICE_TRIES", 3)
ПОВТОР_ТРЕВОГИ_ЗАПАСНОГО_S = ST.env_float("BLOOM_FALLBACK_ALARM_REPEAT_S", 120.0)

# ТИШИНА ПРО КОРОТКИЕ ПЕРЕХОДЫ (слово владельца 25.09: "причина важнее шума").
# Короче порога -- только счётчик, никакой строки в чат. Порог в секундах: за
# 30 с на запасном пути мимо нас не проходит ни одного сигнала, который
# стоило бы будить владельца.
ПОРОГ_СООБЩЕНИЯ_ЗАПАСНОГО_S = ST.env_float("BLOOM_FALLBACK_REPORT_S", 30.0)
# КАК ЧАСТО ПРОВЕРЯТЬ СРОКИ, КОГДА СООБЩЕНИЙ НЕТ. Две секунды: дешевле
# некуда (это таймаут ожидания, а не запрос), а окно возврата 120 с при таком
# тике соблюдается с точностью до двух секунд.
ТИК_ПОДПИСКИ_S = ST.env_float("BLOOM_WS_TICK_S", 2.0)
# Часовая строка в ЖУРНАЛЬНЫЙ чат и тревога в основной, если доля времени на
# запасном за час выше десятой части.
ЧАС_ЗАПАСНОГО_S = ST.env_float("BLOOM_FALLBACK_HOUR_S", 3600.0)
ДОЛЯ_ТРЕВОГИ_ЗАПАСНОГО = ST.env_float("BLOOM_FALLBACK_SHARE_ALARM", 0.10)


def причина_обрыва(exc: Exception) -> str:
    """Причина обрыва основного пути ОДНИМ СЛОВОМ -- для таблицы по причинам.

    Группы названы так, чтобы по ним было видно, чья вина: наша (таймаут
    пинга, переподписка) или их (сервер закрыл, отказ подписки, предел).
    Незнакомое исключение так и называется незнакомым -- приписывать ему
    причину значило бы испортить таблицу, по которой пишут в поддержку.
    """
    имя = type(exc).__name__
    текст = str(exc).lower()
    if "все подписки отклонены" in текст:
        return "подписка отклонена сервером"
    if "сервер закрыл соединение" in текст:
        return "сервер закрыл соединение (без ошибки)"
    if имя in ("TimeoutError", "ConnectionClosedError", "ConnectionClosedOK",
               "ConnectionClosed"):
        if "ping" in текст or "keepalive" in текст or имя == "TimeoutError":
            return "таймаут пинга (наша сторона ждала ответа)"
        return f"соединение закрыто: {имя}"
    if "429" in текст or "too many" in текст or "rate" in текст and "limit" in текст:
        return "предел запросов (429)"
    if "401" in текст or "403" in текст or "unauthorized" in текст:
        return "ключ отклонён (401/403)"
    if "dns" in текст or "name or service" in текст or "getaddrinfo" in текст:
        return "имя хоста не разрешилось (DNS)"
    if имя in ("ConnectionResetError", "ConnectionRefusedError", "OSError"):
        return f"сеть: {имя}"
    return f"незнакомая ошибка: {имя}"

ПУЛЬС_S = ST.env_float("BLOOM_DETECTOR_PULSE_S", 60.0)

# КАК ЧАСТО ОБНОВЛЯТЬ ТЁПЛЫЙ BLOCKHASH. Сеть держит хеш около 150 слотов
# (~60 с), а полоса не подписывает хешем старше 30 с (bloom_own_send). Пульс
# в 60 с для этого не годится: половина сигналов пришлась бы на просроченный
# хеш. Свои часы в 12 с дают 1 кредит каждые 12 с -- 7200 в сутки -- и только
# когда полоса включена.
ТЁПЛЫЙ_ХЕШ_S = ST.env_float("BLOOM_OWN_SEND_BH_S", 12.0)
ОБНОВЛЕНИЕ_ИСТОЧНИКОВ_S = ST.env_float("BLOOM_SOURCES_REFRESH_S", 900.0)


async def биение(детектор: Детектор, стоп_через_s: float | None = None) -> None:
    """Признак жизни пишется независимо от потока событий: тишина в
    источниках -- это нормально, а вот тишина в файле состояния означает,
    что служба умерла, и почасовая проверка должна это увидеть."""
    дедлайн = (time.time() + стоп_через_s) if стоп_через_s else None
    while True:
        if дедлайн and time.time() > дедлайн:
            return
        try:
            # Курс обновляется здесь, а не только при сигнале. Две причины:
            # в горячем пути не приходится ждать сеть, и поломка источника
            # курса видна в признаке жизни СРАЗУ, а не после того, как
            # сигналы уже ушли в журнал с UNKNOWN_RATE.
            await asyncio.to_thread(детектор.курс.получить)
            # ТАБЛИЦЫ АДРЕСОВ, НАКОПЛЕННЫЕ ПОДПИСКОЙ. ingest складывает
            # таблицы новых шаблонов в очередь и сам их не грузит. Замер
            # 24.09: за полчаса накопилось 175 штук, и все они достались бы
            # горячему пути -- сборка тогда стоит 82 мс вместо 0.65 мс.
            # Грузим здесь: раз в пульс, в своём потоке, одним вызовом.
            await asyncio.to_thread(детектор.догрузить_таблицы_ног)
            # Вердикт по цепи у позиций, где его нет. Тоже вне горячего пути.
            await asyncio.to_thread(детектор.догнать_chain_ok)
            # Место в блоке -- туда же: один getBlock уровня signatures на
            # позицию, 1 кредит, и владелец видит место рядом с S+N.
            await asyncio.to_thread(детектор.догнать_место_в_блоке)
            # Количество, купленное полосой, там где поток пришёл без meta.
            # Сторож без него продавать не станет -- и правильно: остаток
            # минта продавать целиком нельзя, там же покупка Bloom.
            await asyncio.to_thread(детектор.догнать_купленное_полосы)
            # Место КОНТРОЛЯ в блоке и толпа в пуле -- вторая половина пары
            # З2. Без них строка пары уходит с прочерками, а вопрос "плата
            # или путь" остаётся без чисел.
            # ТЁПЛЫЕ СОЕДИНЕНИЯ ОТПРАВИТЕЛЕЙ -- здесь же, на пульсе: это
            # единственное место вне пути сделки, которое ходит регулярно.
            await asyncio.to_thread(детектор.прогреть_отправителей)
            await asyncio.to_thread(детектор.догнать_места_контролей)
            await asyncio.to_thread(детектор.догнать_очередь_пары)
            await asyncio.to_thread(детектор.догнать_место_источника)
            await asyncio.to_thread(детектор.досказать_контроли)
            детектор.свод_контролей()
            # Часовое окно тени: ноль собранных при трёх и более сигналах --
            # тревога владельцу и перезапуск ТЕНИ, детектор не трогаем.
            детектор.сводка_тени_за_час()
            # Тень пропусков узкого фильтра: цена на S+1 и через 28.8 с.
            # Предел на круг стоит в самом методе: пропуск стоит ~5 вызовов.
            await asyncio.to_thread(детектор.догнать_цены_пропусков)
            # Тревога о запасном пути -- на пульсе: подписка в это время
            # занята сообщениями, а пульс как раз для таких проверок.
            детектор.проверить_запасной_путь()
            # ЧАСОВАЯ СТРОКА ПРО ЗАПАСНОЙ ПУТЬ вместо строки на каждый переход.
            детектор.часовая_строка_запасного()
            # Сводка решений раз в час -- тоже с пульса.
            детектор.сводка_за_окно()
            детектор.признак_жизни()
        except Exception as exc:  # noqa: BLE001
            log.warning("признак жизни не записался: %s: %s",
                        type(exc).__name__, str(exc)[:160])
        await asyncio.sleep(ПУЛЬС_S)


async def тёплый_хеш(детектор: Детектор, стоп_через_s: float | None = None) -> None:
    """Часы тёплого blockhash для полосы своей отправки.

    Отдельные часы, а не пульс: пульс раз в минуту, а хеш нужен свежее 30 с.
    Полоса выключена -- часы не идут вовсе и кредитов не тратят.
    """
    if not детектор.полоса_включена:
        return
    дедлайн = (time.time() + стоп_через_s) if стоп_через_s else None
    while True:
        if дедлайн and time.time() > дедлайн:
            return
        try:
            await asyncio.to_thread(детектор.обновить_blockhash)
        except Exception as exc:  # noqa: BLE001
            log.warning("тёплый хеш не обновился: %s: %s",
                        type(exc).__name__, str(exc)[:160])
        # ТЁПЛЫЙ NONCE -- на тех же часах. Отдельные часы не нужны: и хеш, и
        # nonce нужны свежими к моменту сделки, а не по своему расписанию.
        try:
            await asyncio.to_thread(детектор.обновить_нонс)
        except Exception as exc:  # noqa: BLE001
            log.warning("тёплый nonce не обновился: %s: %s",
                        type(exc).__name__, str(exc)[:160])
        await asyncio.sleep(ТЁПЛЫЙ_ХЕШ_S)


async def часы_слотов(детектор: Детектор, ключ: str,
                       стоп_через_s: float | None = None) -> None:
    """slotSubscribe -- часы в слотах.

    У slotSubscribe нет параметра commitment: он и есть processed, узел
    шлёт слот как только его обработал. Так и фиксируем, а не делаем вид,
    что что-то передали. Нужны именно они: getSlot в горячем пути стоит
    круг до сети и меряет слот ПОСЛЕ задержки.
    """
    if websockets is None:
        return
    дедлайн = (time.time() + стоп_через_s) if стоп_через_s else None
    backoff = 1.0
    while True:
        if дедлайн and time.time() > дедлайн:
            return
        try:
            async with websockets.connect(ws_url(ключ, atlas=False), ping_interval=20,
                                           ping_timeout=30, max_size=1024 * 1024) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                                           "method": "slotSubscribe", "params": []}))
                ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if "error" in ack:
                    raise RuntimeError(f"slotSubscribe отклонён: {ack['error']}")
                log.info("slotSubscribe подтверждён (подписка %s)", ack.get("result"))
                backoff = 1.0
                async for raw in ws:
                    if дедлайн and time.time() > дедлайн:
                        return
                    msg = json.loads(raw)
                    if msg.get("method") != "slotNotification":
                        continue
                    слот = ((msg.get("params") or {}).get("result") or {}).get("slot")
                    if isinstance(слот, int):
                        детектор.слот_сети = слот
                        детектор.t_слот = time.time()
                        детектор.слот_уведомлений += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("часы слотов оборвались (%s: %s) -- переподключение через %.0f с",
                        type(exc).__name__, str(exc)[:160], backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def часы_баланса(детектор: Детектор, период_s: float = 10.0,
                        стоп_через_s: float | None = None) -> None:
    """Баланс кошелька исполнителя в фоне. В горячем пути используется
    кеш, и если он протух -- баланс считается НЕИЗВЕСТНЫМ, а неизвестный
    баланс запрещает покупку."""
    дедлайн = (time.time() + стоп_через_s) if стоп_через_s else None
    while True:
        if дедлайн and time.time() > дедлайн:
            return
        try:
            b = await asyncio.to_thread(детектор.helius.баланс_sol, ST.EXECUTOR_WALLET)
            if b is not None:
                детектор.баланс_sol = b
                детектор.t_баланс = time.time()
        except Exception as exc:  # noqa: BLE001
            log.warning("баланс не обновился: %s: %s", type(exc).__name__, str(exc)[:160])
        # БАЛАНС ПОЛОСЫ -- ОТДЕЛЬНО И СВОИМ ВЫЗОВОМ. Смешивать нельзя: у
        # полосы свой кошелёк и свои 0.1 SOL, и её пустой кошелёк не должен
        # выглядеть как полный кошелёк исполнителя. Вызов делается только
        # когда кошелёк действительно свой: иначе это второй раз тот же адрес.
        if OS is not None and OS.свой_кошелёк():
            try:
                бп = await asyncio.to_thread(детектор.helius.баланс_sol,
                                             OS.кошелёк_полосы())
                if бп is not None:
                    детектор.баланс_полосы_sol = бп
                    детектор.t_баланс_полосы = time.time()
            except Exception as exc:  # noqa: BLE001
                log.warning("баланс полосы не обновился: %s: %s",
                            type(exc).__name__, str(exc)[:160])
        await asyncio.sleep(период_s)


async def обновлятель(детектор: Детектор, задачи: tuple, снимок: Path) -> None:
    while True:
        await asyncio.sleep(ОБНОВЛЕНИЕ_ИСТОЧНИКОВ_S)
        try:
            await asyncio.to_thread(детектор.обновить_источники, задачи, снимок)
        except Exception as exc:  # noqa: BLE001
            log.warning("обновление источников не удалось: %s: %s",
                        type(exc).__name__, str(exc)[:200])


# ------------------------------------------------------------ самопроверка

def _врем_каталог():
    """Временный каталог для самопроверки, который не падает на уборке.

    Детектор держит фоновые потоки (часы баланса, разбор в потоке). Поток
    может дописать файл в каталог состояния ровно в тот момент, когда
    TemporaryDirectory его удаляет, и тогда уборка роняет процесс с
    OSError "Directory not empty" ПОСЛЕ того, как все проверки прошли
    (поймано 25.09 на каталоге s2). На хосте такой выход уронил бы гейт
    самопроверок (`set -e`) при пройденных проверках -- то есть соврал бы.
    Ошибку уборки глушим: каталог временный, его подчистит система; сами
    проверки от этого не становятся слабее.
    """
    import tempfile as _tf  # noqa: PLC0415

    try:
        return _tf.TemporaryDirectory(ignore_cleanup_errors=True)
    except TypeError:  # python до 3.10 такого ключа не знает
        return _tf.TemporaryDirectory()


def self_test() -> int:
    # СТРАЖ САМОПРОВЕРОК -- ПЕРВОЙ СТРОКОЙ. 25.09 самопроверки, запущенные на
    # хосте с боевым окружением, отправили строки с заглушками в БОЕВОЙ чат
    # владельца. Теперь тест физически не может ни написать в боевой путь, ни
    # выйти в сеть: страж падает исключением, а не предупреждает.
    import selftest_guard as _SG  # noqa: PLC0415

    _охрана = _SG.включить()
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    def tx(*, pre, post, native=(0, 0), fee=0, err=None, ключи=("SRC",),
            инстр=()):
        return {"slot": 100,
                "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in ключи],
                                             "instructions": [{"programId": p} for p in инстр]},
                                 "signatures": ["SIG"]},
                "meta": {"err": err, "fee": fee,
                          "preBalances": [native[0]], "postBalances": [native[1]],
                          "preTokenBalances": pre, "postTokenBalances": post,
                          "innerInstructions": []}}

    def бал(mint, raw, dec=6, owner="SRC", prog=TOKEN_CLASSIC, idx=1):
        return {"accountIndex": idx, "mint": mint, "owner": owner,
                "programId": prog,
                "uiTokenAmount": {"amount": str(raw), "decimals": dec,
                                   "uiAmount": raw / 10 ** dec}}

    # 1. покупка за USDC, первый вход
    t = tx(pre=[бал(USDC, 600_000000)], post=[бал(USDC, 100_000000), бал("MINTA", 5_000000, idx=2)])
    s = сигнал_из_транзакции(t, "SRC", подпись="SIG", слот=100)
    chk("покупка распознана", s["kind"] == "buy", s["kind"])
    chk("минт покупки верный", s["mint"] == "MINTA", s["mint"])
    chk("первый вход виден", s["first_entry"] is True, s["first_entry"])
    chk("трата в USDC = 500", abs((s["spend_ui"] or 0) - 500) < 1e-9, s["spend_ui"])
    chk("в SOL без курса не пересчитывается", s["spend"] is None, s["spend"])
    v, _ = в_sol(s, 200.0)
    chk("500 USDC при 200 USD/SOL = 2.5 SOL", abs(v - 2.5) < 1e-9, v)
    v2, поч = в_sol(s, None)
    chk("без курса -- None, а не догадка", v2 is None, поч)

    # 2. докупка: минт был в pre
    t2 = tx(pre=[бал(USDC, 600_000000), бал("MINTA", 1_000000, idx=2)],
             post=[бал(USDC, 100_000000), бал("MINTA", 6_000000, idx=2)])
    s2 = сигнал_из_транзакции(t2, "SRC", подпись="SIG2")
    chk("докупка: первый_вход False", s2["first_entry"] is False, s2["first_entry"])
    ок, код, _ = фильтры_dbot(s2, 2.5)
    chk("докупка отсекается кодом DBot", (not ок) and код == КОД_ДОКУПКА, код)

    # 3. маленький вход
    ок, код, _ = фильтры_dbot(s, 1.9)
    # Полоса у порога: где обычный отказ, а где граница
    ок_кр, код_кр, поч_кр = фильтры_dbot(
        {"kind": "buy", "first_entry": True}, 0.007, порог_sol=2.0)
    chk("вход 0.007 при пороге 2 -- обычный отказ, не граница",
        код_кр == КОД_МАЛО and "0.4 % от порога" in поч_кр, (код_кр, поч_кр))
    ок_гр, код_гр, поч_гр = фильтры_dbot(
        {"kind": "buy", "first_entry": True}, 1.95, порог_sol=2.0)
    chk("вход 1.95 при пороге 2 -- код у порога и доля названа",
        код_гр == КОД_У_ПОРОГА and "97.5 %" in поч_гр, (код_гр, поч_гр))
    chk("и код у порога НЕ покупает", ок_гр is False)
    ок_рв, код_рв, _ = фильтры_dbot({"kind": "buy", "first_entry": True}, 2.0,
                                      порог_sol=2.0)
    chk("ровно порог -- покупка", ок_рв is True and код_рв == КОД_КУПИТЬ, код_рв)

    chk("вход 1.9 SOL отсекается, но кодом у порога",
        (not ок) and код == КОД_У_ПОРОГА, код)
    ок, код, _ = фильтры_dbot(s, 2.0)
    chk("вход ровно 2 SOL проходит", ок and код == КОД_КУПИТЬ, код)

    # 4. продажа
    t3 = tx(pre=[бал("MINTA", 5_000000, idx=2)], post=[бал(USDC, 500_000000)])
    s3 = сигнал_из_транзакции(t3, "SRC", подпись="SIG3")
    chk("продажа распознана", s3["kind"] == "sell", s3["kind"])
    ок, код, поч = фильтры_dbot(s3, 5.0)
    chk("продажа не копируется", (not ок) and код == КОД_НЕ_ПОКУПКА, код)
    chk("причина называет only_pnl", "only_pnl" in поч, поч)

    # 5. покупка за нативный SOL, комиссия не считается тратой
    t4 = tx(pre=[], post=[бал("MINTB", 7_000000, idx=2)],
             native=(3 * LAMPORT, 1 * LAMPORT), fee=5000, ключи=("SRC",))
    s4 = сигнал_из_транзакции(t4, "SRC", подпись="SIG4")
    chk("трата в SOL посчитана", abs(s4["spend"] - (2 - 5000 / LAMPORT)) < 1e-12, s4["spend"])
    chk("комиссия вычтена из траты", s4["spend"] < 2.0, s4["spend"])
    chk("для SOL курс не нужен", в_sol(s4, None)[0] is not None, в_sol(s4, None))

    # 6. WSOL считается вместе с нативным
    t5 = tx(pre=[бал(WSOL, 3 * LAMPORT, dec=9)], post=[бал("MINTC", 1, idx=2)])
    s5 = сигнал_из_транзакции(t5, "SRC", подпись="SIG5")
    chk("WSOL-трата = 3 SOL", abs(s5["spend"] - 3.0) < 1e-9, s5["spend"])

    # 7. ошибка цепи
    t6 = tx(pre=[], post=[], err={"InstructionError": [0, "X"]})
    s6 = сигнал_из_транзакции(t6, "SRC", подпись="SIG6")
    chk("неуспешная транзакция -- не сигнал", s6["kind"] == "fail", s6["kind"])

    # 8. два выросших минта -- неоднозначно, а не угадываем
    t7 = tx(pre=[бал(USDC, 600_000000)],
             post=[бал(USDC, 0), бал("M1", 1, idx=2), бал("M2", 1, idx=3)])
    s7 = сигнал_из_транзакции(t7, "SRC", подпись="SIG7")
    chk("две покупки в одной транзакции -- ambiguous", s7["kind"] == "ambiguous", s7["kind"])
    ок, код, _ = фильтры_dbot(s7, 3.0)
    chk("ambiguous не исполняется", (not ок) and код == КОД_НЕЯСНО, код)

    # 8b. токен пришёл, но ничего не отдано -- это получение, не покупка
    t_пришёл = tx(pre=[], post=[бал("ПОДАРОК", 6_000000, idx=2)])
    s_пришёл = сигнал_из_транзакции(t_пришёл, "SRC", подпись="SIG8b")
    chk("получение токена -- отдельный тип, а не ambiguous",
        s_пришёл["kind"] == "received", s_пришёл["kind"])
    chk("в причине названа нулевая нативная дельта",
        "+0.000000000 SOL" in s_пришёл["decide_reason"], s_пришёл["decide_reason"])
    ок_, код_, _ = фильтры_dbot(s_пришёл, None)
    chk("и свой код, а не AMBIGUOUS_TX",
        (not ок_) and код_ == КОД_ПОЛУЧЕН_НЕ_КУПЛЕН, код_)
    chk("код отличается от кода неоднозначности", КОД_ПОЛУЧЕН_НЕ_КУПЛЕН != КОД_НЕЯСНО)

    # 9. чужой кошелёк в транзакции не путает разбор
    t8 = tx(pre=[бал(USDC, 600_000000, owner="OTHER")],
             post=[бал("MINTA", 5_000000, owner="OTHER", idx=2)])
    s8 = сигнал_из_транзакции(t8, "SRC", подпись="SIG8")
    chk("чужие балансы не считаются нашими", s8["kind"] == "other", s8["kind"])

    # 10. программы DEX -- факт, а не догадка
    t9 = tx(pre=[бал(USDC, 600_000000)], post=[бал(USDC, 0), бал("MINTA", 5_000000, idx=2)],
             инстр=("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "НЕИЗВЕСТНАЯ"))
    s9 = сигнал_из_транзакции(t9, "SRC", подпись="SIG9")
    chk("известная программа DEX названа", s9["dex_programs"] == ["Raydium AMM v4"], s9["dex_programs"])
    chk("неизвестная программа не выдумывается", "НЕИЗВЕСТНАЯ" not in str(s9["dex_programs"]))
    chk("адрес пула не придумывается", "пул" not in s9 and s9.get("pool") is None)

    # 11. Token-2022 виден из балансов
    t10 = tx(pre=[бал(USDC, 600_000000)],
              post=[бал(USDC, 0), бал("MINTA", 5_000000, idx=2, prog=TOKEN_2022)])
    s10 = сигнал_из_транзакции(t10, "SRC", подпись="SIG10")
    chk("программа токена определена", s10["token_program"] == TOKEN_2022, s10["token_program"])

    # 12. источники из конфига
    конфиг = {"body": {"res": [
        {"name": "BATCH-5", "enabled": True, "targetIds": ["Hn5gVKAApv69t5HX7Q77uX7o5ayhEwArgYx7kukMLVGn"]},
        {"name": "BATCH-3", "enabled": True, "targetIds": ["BMgsHTvcasRVtuevHJh8t6Vf5dmcWkDLAx6gSAQ3dsYm"]},
        {"name": "BATCH-7", "enabled": True, "targetIds": ["4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8"]},
        {"name": "BATCH-5", "enabled": False, "targetIds": ["ZZZZ"]}]}}
    ист = источники_из_конфига(конфиг, ("BATCH-5", "BATCH-3"))
    chk("взяты только нужные задачи", len(ист) == 2, ист)
    chk("BATCH-7 не попал", all(v != "BATCH-7" for v in ист.values()), ист)

    # 12б. снимок хранит тело под КИРИЛЛИЧЕСКИМ ключом, и это было
    # настоящей поломкой: откат на снимок отдавал ноль источников, а
    # заметно это стало бы только когда DBot откажет. Прежний тест
    # проверял форму {"body": ...}, которой у снимка нет, -- поэтому был
    # зелёным.
    ист_кир = источники_из_конфига(
        {"тело": {"res": [{"name": "BATCH-5", "enabled": True,
                            "targetIds": ["Hn5gVKAApv69t5HX7Q77uX7o5ayhEwArgYx7kukMLVGn"]}]}},
        ("BATCH-5",))
    chk("тело под ключом \"тело\" читается", len(ист_кир) == 1, ист_кир)
    chk("тело_ответа не путает конфиг с телом",
        тело_ответа({"res": [1]}) == {"res": [1]}, тело_ответа({"res": [1]}))
    chk("тело не словарём игнорируется",
        тело_ответа({"тело": "строка", "res": [2]}) == {"тело": "строка", "res": [2]})

    # 12в. проверка НА НАСТОЯЩЕМ снимке: именно она и ловит эту ошибку.
    снимок_репо = (Path(__file__).resolve().parent.parent / "data" / "final" /
                    "20260923T145755Z" / "konfig.json")
    if снимок_репо.exists():
        ист_снимок = загрузить_источники(снимок_репо, ("BATCH-5", "BATCH-3"))
        chk("настоящий снимок даёт 17 источников (9 BATCH-5 + 8 BATCH-3)",
            len(ист_снимок) == 17, len(ист_снимок))
        chk("и обе задачи представлены",
            set(ист_снимок.values()) == {"BATCH-5", "BATCH-3"},
            sorted(set(ист_снимок.values())))
    else:
        chk("снимок конфига на месте", False, f"нет файла {снимок_репо}")

    # 12г. потолок версии транзакции ОДИН у подписки и у getTransaction.
    # Асимметрия (подписка 0, getTransaction 1) была настоящей причиной
    # того, что разбор уходил на медленный путь, и вернуться она не должна.
    class WSЗапись:
        def __init__(self):
            self.отправлено = []

        async def send(self, тело):
            self.отправлено.append(json.loads(тело))

    async def _собрать_подписку():
        ws = WSЗапись()
        await _подписка_транзакций(ws, ["Hn5gVKAApv69t5HX7Q77uX7o5ayhEwArgYx7kukMLVGn"])
        return ws.отправлено

    отправлено = asyncio.run(_собрать_подписку())
    настройки = отправлено[0]["params"][1]
    chk("в подписке потолок версии равен нашей константе",
        настройки["maxSupportedTransactionVersion"] == ПОТОЛОК_ВЕРСИИ_TX,
        настройки.get("maxSupportedTransactionVersion"))
    chk("и он не нулевой: транзакции версии 1 в блоках есть",
        ПОТОЛОК_ВЕРСИИ_TX >= 1, ПОТОЛОК_ВЕРСИИ_TX)
    chk("подписка по-прежнему на processed",
        настройки["commitment"] == "processed", настройки.get("commitment"))

    class HeliusПотолок(Helius):
        def __init__(self):
            super().__init__(key="нет", служба="")
            self.параметры = None

        def call(self, метод, параметры, **kw):
            self.параметры = параметры
            return None

    hп = HeliusПотолок()
    hп.транзакция("ПОДПИСЬ", попыток=1, пауза_s=0)
    chk("у getTransaction тот же потолок, что у подписки",
        hп.параметры[1]["maxSupportedTransactionVersion"] == ПОТОЛОК_ВЕРСИИ_TX,
        hп.параметры[1].get("maxSupportedTransactionVersion"))

    # 12д. несколько тестовых источников и версия транзакции в записи
    chk("версия из конверта читается",
        версия_транзакции({"version": 1}) == 1)
    chk("версия из вложенной транзакции читается",
        версия_транзакции({"transaction": {"version": "legacy"}}) == "legacy")
    chk("отсутствие версии -- не ноль",
        версия_транзакции({}) == ВЕРСИЯ_НЕТ, версия_транзакции({}))
    chk("версия 0 не путается с отсутствием",
        версия_транзакции({"version": 0}) == 0)
    chk("не словарь -- тоже отсутствие",
        версия_транзакции(None) == ВЕРСИЯ_НЕТ)

    старое_окружение = (os.environ.get("BLOOM_TEST_SOURCES"),
                        os.environ.get("BLOOM_TEST_SOURCE"))
    os.environ["BLOOM_TEST_SOURCES"] = " A1 , B2 ,, A1 "
    os.environ["BLOOM_TEST_SOURCE"] = "C3"
    список = _тестовые_из_окружения()
    chk("список тестовых источников без повторов и пустых",
        список == ("A1", "B2", "C3"), список)
    for имя, знач in zip(("BLOOM_TEST_SOURCES", "BLOOM_TEST_SOURCE"), старое_окружение):
        if знач is None:
            os.environ.pop(имя, None)
        else:
            os.environ[имя] = знач

    # 13. решение с настоящим состоянием
    import tempfile  # noqa: PLC0415
    with _врем_каталог() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "kill")
        r = решение(s, состояние=st, трата_sol=2.5, баланс_sol=3.0, текущий_слот=101)
        chk("решение -- покупка", r["action"] == "buy", r)
        chk("код покупки", r["code"] == КОД_КУПИТЬ, r["code"])
        r2 = решение(s, состояние=st, трата_sol=2.5, баланс_sol=3.0, текущий_слот=105)
        chk("устаревший сигнал отсекается", r2["code"] == КОД_УСТАРЕЛ, r2["code"])
        chk("устаревание проверяется до наших лимитов", r2.get("filter") is None, r2.get("filter"))
        r3 = решение(s, состояние=st, трата_sol=2.5, баланс_sol=0.05, текущий_слот=101)
        chk("нехватка баланса -- наш лимит", r3["code"] == ST.КОД_БАЛАНС, r3["code"])
        chk("помечено, что DBot бы купил", r3.get("dbot_бы_купил") is True, r3.get("dbot_бы_купил"))

        # дубль по минту -- отдельный код, а не «расхождение»
        st.write_intent(client_order_id="c1", mint="MINTA", source_sig="ДРУГАЯ",
                         source_slot=1, sol_in=0.2, pool=None, program=None,
                         taxed=None, tax_bps=None, mode="dry", sell_after_s=28.8)
        r4 = решение(s, состояние=st, трата_sol=2.5, баланс_sol=3.0, текущий_слот=101)
        chk("дубль по минту -- SKIPPED_DUP_MINT", r4["code"] == "SKIPPED_DUP_MINT", r4["code"])
        chk("дубль помечен как наш лимит", r4.get("filter") == "наш лимит", r4.get("filter"))

        # рубильник сильнее всего остального
        st.kill_path.write_text("стоп", encoding="utf-8")
        r5 = решение(s, состояние=st, трата_sol=2.5, баланс_sol=3.0, текущий_слот=101)
        chk("рубильник запрещает покупку", r5["action"] == "skip", r5)
        chk("код рубильника", r5["code"] == ST.КОД_РУБИЛЬНИК, r5["code"])

    # 14. курс: без сети не выдумывается
    к = КурсSOL(ttl_s=1.0)
    chk("свежести нет у пустого курса", not к.свежий())
    к.значение, к.когда, к.источник = 200.0, time.time(), "тест"
    chk("свежий курс отдаётся", к.получить() == 200.0, к.значение)

    # 15. признак жизни, поколение источников, откат на снимок
    with _врем_каталог() as d:
        снимок = Path(d) / "konfig.json"
        снимок.write_text(json.dumps(конфиг, ensure_ascii=False), encoding="utf-8")
        было = os.environ.pop("DBOT_API_KEY", None)
        # ГРУППЫ ИСТОЧНИКОВ здесь отключены нарочно: этот блок проверяет откат
        # на снимок DBot, и боевой файл групп (60 адресов) сделал бы числа
        # проверок зависимыми от того, сколько кошельков владелец добавил
        # сегодня. Слияние с группами проверяется отдельно, тут же ниже.
        было_гр = os.environ.get("BLOOM_SOURCE_GROUPS")
        os.environ["BLOOM_SOURCE_GROUPS"] = str(Path(d) / "групп_нет.json")
        import bloom_source_groups as SGт  # noqa: PLC0415

        SGт.загрузить(заново=True)
        try:
            ист, откуда = источники(("BATCH-5", "BATCH-3"), снимок)
            chk("без ключа DBot берётся снимок", len(ист) == 2 and "снимок" in откуда, откуда)
            # А ТЕПЕРЬ С ГРУППАМИ: их адреса добавляются к списку DBot, а адрес,
            # уже пришедший из DBot, остаётся за своей задачей -- иначе мы молча
            # выключили бы торговлю Bloom по нему.
            файл_групп = Path(d) / "группы.json"
            свой_из_dbot = sorted(ист)[0]
            файл_групп.write_text(json.dumps({"groups": {
                "lane_only": {"lane_sol": 0.05, "bloom_trades": False,
                               "fanout": True,
                               "addresses": {"НОВЫЙ_ПОЛОСНЫЙ_АДРЕС": "BATCH-1",
                                              свой_из_dbot: "BATCH-1"}},
                "speed_only": {"lane_sol": 0.01, "bloom_trades": False,
                                "fanout": False, "day_cap_sol": 0.2,
                                "stop_loss_sol": 0.15,
                                "snipers": [{"address": "АДРЕС_СКОРОСТИ"}]},
            }}, ensure_ascii=False), encoding="utf-8")
            os.environ["BLOOM_SOURCE_GROUPS"] = str(файл_групп)
            SGт.загрузить(заново=True)
            ист_г, откуда_г = источники(("BATCH-5", "BATCH-3"), снимок)
            chk("адреса групп добавлены к списку DBot",
                len(ист_г) == 4 and "НОВЫЙ_ПОЛОСНЫЙ_АДРЕС" in ист_г
                and "АДРЕС_СКОРОСТИ" in ист_г and "групп источников: 2" in откуда_г,
                (sorted(ист_г), откуда_г))
            chk("адрес из DBot остался за своей задачей, а не за группой",
                ист_г[свой_из_dbot] == ист[свой_из_dbot], ист_г[свой_из_dbot])
            os.environ["BLOOM_SOURCE_GROUPS"] = str(Path(d) / "групп_нет.json")
            SGт.загрузить(заново=True)
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            det = Детектор(источники=dict(ист), состояние=st, helius=Helius(key="нет"),
                            курс=КурсSOL(), режим="dry")
            j = det.признак_жизни()
            chk("признак жизни записан на диск", det.статус_путь().exists())
            chk("в признаке жизни виден режим", j["mode"] == "dry", j["mode"])
            chk("в признаке жизни есть версия формата",
                j.get(ST.SCHEMA_VERSION_KEY) == ST.SCHEMA_VERSION,
                j.get(ST.SCHEMA_VERSION_KEY))
            chk("все ключи признака жизни латинские",
                all(k.isascii() for k in j), [k for k in j if not k.isascii()])
            chk("в признаке жизни число источников", j["sources"] == 2, j["sources"])
            chk("в признаке жизни видно, читается ли рубильник",
                j["kill_readable"] is True and j["kill_active"] is False,
                (j["kill_readable"], j["kill_active"]))
            det2 = Детектор(источники=dict(ист), состояние=ST.ExecState(
                base=Path(d) / "s3", kill=Path(d) / "нет_каталога" / "KILL"),
                helius=Helius(key="нет"), курс=КурсSOL(), режим="dry")
            j2 = det2.признак_жизни()
            chk("нечитаемый рубильник не выдаётся за выключенный",
                j2["kill_readable"] is False, j2["kill_readable"])
            снят = json.loads(det.статус_путь().read_text(encoding="utf-8"))
            chk("файл признака жизни читается", снят["sources"] == 2, снят)

            # смена списка поднимает поколение
            det.источники = {"ОДИН": "BATCH-5"}
            менялось = det.обновить_источники(("BATCH-5", "BATCH-3"), снимок)
            chk("смена списка замечена", менялось is True, менялось)
            chk("поколение выросло", det.поколение == 1, det.поколение)
            снова = det.обновить_источники(("BATCH-5", "BATCH-3"), снимок)
            chk("одинаковый список поколение не двигает",
                снова is False and det.поколение == 1, (снова, det.поколение))
        finally:
            if было is not None:
                os.environ["DBOT_API_KEY"] = было
            if было_гр is None:
                os.environ.pop("BLOOM_SOURCE_GROUPS", None)
            else:
                os.environ["BLOOM_SOURCE_GROUPS"] = было_гр
            SGт.загрузить(заново=True)

    # 15b. ветка "решение -- покупка" в обработать(): именно она сломалась
    # молча при переводе ключей на ASCII, потому что её не покрывал ни один
    # тест. Сравнение шло со строкой, которую переименовали в другом месте.
    with _врем_каталог() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "kill")

        class HeliusЗаглушка(Helius):
            def __init__(self):
                super().__init__(key="нет", служба="")
                self.спрошено = []

            def налог_минта(self, минт):
                self.спрошено.append(минт)
                return {"taxed": True, "fee_bps": 300}

        h = HeliusЗаглушка()
        det = Детектор(источники={"SRC": "BATCH-5"}, состояние=st, helius=h,
                        курс=КурсSOL(), режим="dry")
        det.курс.значение, det.курс.когда = 200.0, time.time()
        det.слот_сети, det.t_слот = 100, time.time()
        det.баланс_sol, det.t_баланс = 5.0, time.time()
        t_ок = tx(pre=[бал(USDC, 600_000000)],
                   post=[бал(USDC, 0), бал("MINT_OK", 5_000000, idx=2)])
        r = det.обработать("ПОДПИСЬ_ОК", 100, "SRC", "тест", t_ок)
        chk("решение о покупке доходит до ветки покупки",
            r["action"] == "buy", r.get("action"))
        # Налог минта спрашивается ДВАЖДЫ только у заглушки: у настоящего узла
        # второй вопрос попадает в кеш минтов и кредита не стоит. Первый
        # вопрос -- фильтр по налогу маршрута ДО решения, второй -- признак
        # токена в запись ПОСЛЕ отправки ордера.
        chk("налог минта спрошен именно в этой ветке",
            h.спрошено and set(h.спрошено) == {"MINT_OK"}, h.спрошено)
        chk("налог маршрута посчитан до решения и его цена названа числом",
            r.get("route_transfer_fee_bps") is not None
            and r.get("route_tax_ms") is not None
            and r.get("route_tax_from") == "source",
            (r.get("route_transfer_fee_bps"), r.get("route_tax_ms")))
        # ---- УЗКИЙ ФИЛЬТР ПО НАЛОГУ МАРШРУТА (владелец 25.09) ----
        # Цена ошибки прямая: пропустим PICKAXE-подобный маршрут -- потеряем
        # около 6 % на налоге; зарежем налоговый токен с прямым пулом к SOL --
        # потеряем сделки, которые владелец велел покупать.
        class HeliusНалоги(Helius):
            налоги = {"MINT_ЧИСТЫЙ": 0, "MINT_НАЛОГ": 250, "ПРОМЕЖ_НАЛОГ": 300}

            def __init__(self):
                super().__init__(key="нет", служба="")
                self.пакетов = 0

            def налоги_минтов(self, минты):
                self.пакетов += 1
                return {"asked": len(минты or []), "got": len(минты or []),
                        "ms": 1.0, "why_not": None}

            def налог_минта(self, минт):
                bps = HeliusНалоги.налоги.get(минт, 0)
                return {"mint": минт, "taxed": bool(bps), "fee_bps": bps}

        def передача_т(минт):
            return {"programId": TOKEN_CLASSIC,
                     "parsed": {"type": "transferChecked",
                                 "info": {"mint": минт, "authority": "SRC"}}}

        def tx_с_передачами(минт_покупки, передачи, *, промеж=()):
            т = tx(pre=[бал(USDC, 600_000000)],
                    post=[бал(USDC, 0), бал(минт_покупки, 5_000000, idx=2)])
            т["meta"]["innerInstructions"] = [{"instructions": передачи}]
            return т

        st_ф = ST.ExecState(base=Path(d) / "filter", kill=Path(d) / "kill")
        hф = HeliusНалоги()
        detф = Детектор(источники={"SRC": "BATCH-5"}, состояние=st_ф, helius=hф,
                         курс=КурсSOL(), режим="dry")
        detф.курс.значение, detф.курс.когда = 200.0, time.time()
        detф.слот_сети, detф.t_слот = 100, time.time()
        detф.баланс_sol, detф.t_баланс = 5.0, time.time()

        # PICKAXE-подобный случай: налоговый промежуточный и двойная передача.
        t_пик = tx_с_передачами("MINT_НАЛОГ", [
            передача_т(WSOL), передача_т(USDC), передача_т("ПРОМЕЖ_НАЛОГ"),
            передача_т("MINT_НАЛОГ"), передача_т("MINT_НАЛОГ")])
        r_пик = detф.обработать("ПОДПИСЬ_ПИК", 100, "SRC", "тест", t_пик)
        chk("маршрут с налоговым промежуточным и двойной передачей режется",
            r_пик.get("action") == "skip" and r_пик.get("code") == КОД_НАЛОГ_МАРШРУТА,
            (r_пик.get("action"), r_пик.get("code"), r_пик.get("reason")))
        chk("в записи пропуска есть налог маршрута, налог токена и число передач",
            r_пик.get("route_transfer_fee_bps") == 300 + 250 * 2
            and r_пик.get("token_fee_bps") == 250
            and r_пик.get("route_transfers_of_token") == 2
            and r_пик.get("route_taxed_intermediates") == ["ПРОМЕЖ_НАЛОГ"]
            and r_пик.get("would_skip_fee") is True, r_пик)
        chk("пропуск помечен как НАШ лимит: DBot такого фильтра не имеет",
            r_пик.get("filter") == "наш лимит" and r_пик.get("dbot_бы_купил") is True,
            (r_пик.get("filter"), r_пик.get("dbot_бы_купил")))
        chk("налоги маршрута спрошены ОДНИМ пакетом, а не по одному",
            hф.пакетов == 1, hф.пакетов)

        # Налоговый токен с прямым пулом к SOL -- покупаем (слово владельца).
        t_прям = tx_с_передачами("MINT_НАЛОГ", [передача_т(WSOL),
                                                 передача_т("MINT_НАЛОГ")])
        r_прям = detф.обработать("ПОДПИСЬ_ПРЯМ", 100, "SRC", "тест", t_прям)
        chk("налоговый токен с прямым пулом к SOL проходит фильтр",
            r_прям.get("action") == "buy", (r_прям.get("action"), r_прям.get("reason")))
        chk("и налог у него всё равно помечен в записи",
            r_прям.get("would_skip_fee") is True
            and r_прям.get("token_fee_bps") == 250
            and "по слову владельца" in (r_прям.get("route_tax_note") or ""),
            (r_прям.get("would_skip_fee"), r_прям.get("route_tax_note")))

        # Чистый токен одной передачей -- покупаем без пометок.
        t_чист = tx_с_передачами("MINT_ЧИСТЫЙ", [передача_т(WSOL),
                                                  передача_т("MINT_ЧИСТЫЙ")])
        r_чист = detф.обработать("ПОДПИСЬ_ЧИСТ", 100, "SRC", "тест", t_чист)
        chk("токен без налога покупается и пометки не получает",
            r_чист.get("action") == "buy"
            and r_чист.get("would_skip_fee") is False
            and r_чист.get("route_transfer_fee_bps") == 0, r_чист)

        # Налог не прочитался -- покупку НЕ отменяем, но причину пишем.
        class HeliusБезНалогов(HeliusНалоги):
            def налог_минта(self, минт):
                raise RuntimeError("узел молчит")

        detф2 = Детектор(источники={"SRC": "BATCH-5"}, состояние=st_ф,
                          helius=HeliusБезНалогов(), курс=КурсSOL(), режим="dry")
        detф2.курс.значение, detф2.курс.когда = 200.0, time.time()
        detф2.слот_сети, detф2.t_слот = 100, time.time()
        detф2.баланс_sol, detф2.t_баланс = 5.0, time.time()
        r_нет = detф2.обработать("ПОДПИСЬ_НЕТ_НАЛОГА", 100, "SRC", "тест",
                                  tx_с_передачами("MINT_ЧИСТЫЙ2",
                                                   [передача_т(WSOL),
                                                    передача_т("MINT_ЧИСТЫЙ2")]))
        chk("налог не прочитан -- покупка не отменяется, причина в записи",
            r_нет.get("action") == "buy" and r_нет.get("route_tax_why_not"),
            (r_нет.get("action"), r_нет.get("route_tax_why_not")))


        chk("признак налога попал в запись",
            r.get("taxed") is True and r.get("tax_bps") == 300, r.get("taxed"))
        chk("счётчик к покупке вырос", det.к_покупке == 1, det.к_покупке)
        chk("все ключи записи решения латинские",
            all(k.isascii() for k in r), [k for k in r if not k.isascii()])
        chk("значение action тоже латинское", r["action"].isascii(), r["action"])
        chk("версия транзакции попала в запись",
            "tx_version" in r, sorted(r))

    # 15а. сетевая ошибка НЕ проходит сквозь call и НЕ уносит решение.
    # Нашлось тем, что новая проверка двух тестовых источников впервые
    # довела дело до ветки покупки: оттуда полетел ProxyError, которого не
    # ловит ни один except RuntimeError.
    class ЗаглушкаRequests:
        class _Ошибка(Exception):
            pass

        def post(self, *a, **kw):
            raise self._Ошибка("туннель закрыт")

    # 15а-бис. Вызовы идут ЧЕРЕЗ СЕССИЮ, а не голым requests.post. Без этого
    # каждый вызов открывал новое соединение: замер с хоста 24.09 показал
    # 43.8-52.6 мс до первого байта по новому соединению против 9.9-18.5 мс
    # по уже открытому, и это при getTransaction до 12 попыток подряд.
    class СессияСчётчик:
        def __init__(self):
            self.постов = 0

        def mount(self, *a, **kw):
            pass

        @property
        def adapters(self):
            return {"https://": object()}

        def post(self, *a, **kw):
            self.постов += 1
            raise RuntimeError("дальше сети не идём: считаем только маршрут")

    class ЗаглушкаССессией:
        def __init__(self):
            self.сессии = []
            self.постов_мимо = 0

        def Session(self):
            с = СессияСчётчик()
            self.сессии.append(с)
            return с

        def post(self, *a, **kw):
            self.постов_мимо += 1
            raise RuntimeError("мимо сессии")

    было_req2 = globals().get("requests")
    заглушка = ЗаглушкаССессией()
    globals()["requests"] = заглушка
    try:
        h_сесс = Helius(key="нет", служба="")
        for _ in range(3):
            try:
                h_сесс.call("getSlot", [])
            except RuntimeError:
                pass
        chk("Helius ходит через сессию, а не голым requests.post",
            заглушка.постов_мимо == 0 and len(заглушка.сессии) == 1
            and заглушка.сессии[0].постов == 3,
            (заглушка.постов_мимо, len(заглушка.сессии)))
        chk("сессия заводится ОДИН раз на объект, а не на вызов",
            h_сесс.sess is заглушка.сессии[0])
    finally:
        if было_req2 is None:
            globals().pop("requests", None)
        else:
            globals()["requests"] = было_req2

    было_requests = globals().get("requests")
    globals()["requests"] = ЗаглушкаRequests()
    try:
        h_сеть = Helius(key="нет", служба="")
        try:
            h_сеть.call("getSlot", [])
            упало_как = None
        except RuntimeError as exc:
            упало_как = ("RuntimeError", str(exc))
        except Exception as exc:  # noqa: BLE001
            упало_как = (type(exc).__name__, str(exc))
        chk("сетевая ошибка приходит как RuntimeError",
            упало_как and упало_как[0] == "RuntimeError", упало_как)
        chk("и имя исходной ошибки не потеряно",
            упало_как and "_Ошибка" in упало_как[1], упало_как)
        chk("вызов всё равно посчитан", h_сеть.вызовов == 1, h_сеть.вызовов)
        chk("транзакция при сетевом отказе возвращает None, а не падает",
            h_сеть.транзакция("ПОДПИСЬ", попыток=1, пауза_s=0) is None)
        chk("слот при сетевом отказе -- None", h_сеть.слот() is None)
        chk("баланс при сетевом отказе -- None",
            h_сеть.баланс_sol("КОШЕЛЁК") is None)
    finally:
        if было_requests is None:
            globals().pop("requests", None)
        else:
            globals()["requests"] = было_requests

    # ТЕЛО 200, КОТОРОЕ НЕ JSON. Заглушка прокси отвечает кодом 200 и
    # html-страницей. Раньше r.json() стоял вне try, летел ValueError мимо
    # всех обработчиков -- и сигнал уходил в HANDLER_CRASHED вместо одной
    # потерянной попытки getTransaction.
    class ОтветНеJSON:
        ok = True
        status_code = 200
        content = b"<html>upstream connect error</html>"
        text = "<html>upstream connect error</html>"

        @staticmethod
        def json():
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    class ОтветНеОбъект(ОтветНеJSON):
        content = b"[1, 2]"
        text = "[1, 2]"

        @staticmethod
        def json():
            return [1, 2]

    class СессияОтвет:
        def __init__(self, ответ):
            self.ответ = ответ

        def post(self, *a, **kw):
            return self.ответ

    h_тело = Helius(key="нет", служба="")
    h_тело.sess = СессияОтвет(ОтветНеJSON())
    упало = None
    try:
        h_тело.call("getTransaction", ["S"])
    except RuntimeError as exc:
        упало = ("RuntimeError", str(exc))
    except Exception as exc:  # noqa: BLE001
        упало = (type(exc).__name__, str(exc))
    chk("тело 200, но не JSON -- это RuntimeError, а не ValueError",
        упало and упало[0] == "RuntimeError", упало)
    chk("и в тексте сказано, что тело не JSON",
        упало and "не JSON" in упало[1], упало)
    chk("транзакция на таком теле возвращает None, а не роняет разбор",
        h_тело.транзакция("ПОДПИСЬ", попыток=1, пауза_s=0) is None)

    h_список = Helius(key="нет", служба="")
    h_список.sess = СессияОтвет(ОтветНеОбъект())
    упало2 = None
    try:
        h_список.call("getSlot", [])
    except RuntimeError as exc:
        упало2 = ("RuntimeError", str(exc))
    except Exception as exc:  # noqa: BLE001
        упало2 = (type(exc).__name__, str(exc))
    chk("ответ-массив -- тоже RuntimeError, а не AttributeError",
        упало2 and упало2[0] == "RuntimeError", упало2)
    chk("слот на таком ответе -- None, а не падение", h_список.слот() is None)

    # 15б. ДВА тестовых источника: у каждого свой низкий порог, флаг
    # test_source стоит, счётчики ведутся отдельно от боевых. Без этого
    # теста весь стенд опирался бы на непроверенный код: покупка на 0.1
    # SOL у боевого источника должна отсекаться порогом 2, а у тестового --
    # проходить.
    with _врем_каталог() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "kill")
        было = TEST_SOURCES
        globals()["TEST_SOURCES"] = ("ТЕСТ_ОДИН", "ТЕСТ_ДВА")
        try:
            det = Детектор(источники={"SRC": "BATCH-5", "ТЕСТ_ОДИН": "TEST",
                                       "ТЕСТ_ДВА": "TEST"},
                            состояние=st, helius=Helius(key="нет", служба=""),
                            курс=КурсSOL(), режим="dry")
            det.курс.значение, det.курс.когда = 200.0, time.time()
            det.слот_сети, det.t_слот = 100, time.time()
            det.баланс_sol, det.t_баланс = 5.0, time.time()
            chk("оба тестовых источника опознаны",
                det.это_тестовый("ТЕСТ_ОДИН") and det.это_тестовый("ТЕСТ_ДВА"))
            chk("боевой источник тестовым не считается",
                not det.это_тестовый("SRC"))
            chk("порог у тестовых свой",
                det.порог_для("ТЕСТ_ДВА") == TEST_MIN_SOL, det.порог_для("ТЕСТ_ДВА"))
            chk("у боевого порог боевой",
                det.порог_для("SRC") == ПОРОГ_ВХОДА_SOL, det.порог_для("SRC"))
            мелкая = tx(pre=[бал(USDC, 20_000000, owner="ТЕСТ_ДВА")],
                         post=[бал(USDC, 0, owner="ТЕСТ_ДВА"),
                               бал("MINT_SMALL", 5_000000, owner="ТЕСТ_ДВА", idx=2)],
                         ключи=("ТЕСТ_ДВА",))
            rт = det.обработать("ПОДПИСЬ_ТЕСТ2", 100, "ТЕСТ_ДВА", "тест", мелкая)
            chk("вход 0.1 SOL у тестового источника проходит порог",
                rт["action"] == "buy", (rт.get("action"), rт.get("code")))
            chk("флаг test_source стоит", rт.get("test_source") is True, rт.get("test_source"))
            мелкая2 = tx(pre=[бал(USDC, 20_000000)],
                          post=[бал(USDC, 0),
                                бал("MINT_SMALL2", 5_000000, idx=2)])
            rб = det.обработать("ПОДПИСЬ_БОЕВАЯ", 100, "SRC", "тест", мелкая2)
            chk("тот же вход у боевого источника отсекается порогом",
                rб["code"] == КОД_МАЛО, rб.get("code"))
            chk("счётчики тестового и боевого раздельные",
                det.по_кодам_теста and det.по_кодам
                and КОД_МАЛО in det.по_кодам and КОД_МАЛО not in det.по_кодам_теста,
                (det.по_кодам_теста, det.по_кодам))
            # ---- ПОСТРОЧНЫЕ РЕШЕНИЯ -- В ЖУРНАЛЬНЫЙ ЧАТ (владелец 25.09) ----
            куда_ушло: list = []

            class ОповещательАдресный:
                def послать(self, текст, *, куда="main"):
                    куда_ушло.append((куда, текст))
                    return {"ok": True}

                def статус(self):
                    return {"enabled": True}

            det.оповещатель = ОповещательАдресный()
            было_решений = det._решений_за_окно
            мелкая3 = tx(pre=[бал(USDC, 20_000000)],
                          post=[бал(USDC, 0),
                                бал("MINT_SMALL3", 5_000000, idx=2)])
            det.обработать("ПОДПИСЬ_ЖУРНАЛ", 100, "SRC", "тест", мелкая3)
            chk("построчное решение боевого источника ушло В ЖУРНАЛ",
                куда_ушло and all(к == "log" for к, _ in куда_ушло), куда_ушло)
            chk("решение посчитано в окно сводки",
                det._решений_за_окно == было_решений + 1
                and det._коды_за_окно.get(КОД_МАЛО), (det._решений_за_окно,
                                                       det._коды_за_окно))
            мелкая4 = tx(pre=[бал(USDC, 20_000000, owner="ТЕСТ_ДВА")],
                          post=[бал(USDC, 0, owner="ТЕСТ_ДВА"),
                                бал("MINT_SMALL4", 5_000000, owner="ТЕСТ_ДВА", idx=2)],
                          ключи=("ТЕСТ_ДВА",))
            было_покупок = det._покупок_за_окно
            det.обработать("ПОДПИСЬ_ЖУРНАЛ2", 100, "ТЕСТ_ДВА", "тест", мелкая4)
            chk("решение стенда тоже в журнал, а не владельцу",
                all(к == "log" for к, _ in куда_ушло), куда_ушло)
            chk("покупка в окне сводки считается отдельно от пропусков",
                det._покупок_за_окно == было_покупок + 1, det._покупок_за_окно)
        finally:
            globals()["TEST_SOURCES"] = было

    # 15c. исполнитель подключён: решение о покупке доходит до него,
    # его падение НЕ валит детектор, а решение всё равно остаётся в журнале
    with _врем_каталог() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "kill")

        class ИсполнительЗаглушка:
            def __init__(self, падать=False):
                self.вызовы = []
                self.падать = падать

            def execute(self, решение, *, balance_sol, amount_sol=None):
                # amount_sol -- размер покупки по группе источника: заглушка
                # запоминает его, чтобы проверить, что 0.05 по lane_only
                # доходит до исполнителя, а не теряется по дороге.
                self.вызовы.append((решение.get("signature"), balance_sol))
                self.размеры = getattr(self, "размеры", [])
                self.размеры.append(amount_sol)
                if self.падать:
                    raise RuntimeError("притворное падение исполнителя")
                return {"exec_code": "DRY_RUN", "reason": "заглушка"}

            def report(self):
                return {"live_buy_enabled": False}

        class HeliusБезСети(Helius):
            def __init__(self):
                super().__init__(key="нет", служба="")

            def налог_минта(self, минт):
                return {"taxed": False, "fee_bps": None}

        исп = ИсполнительЗаглушка()
        det = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                        helius=HeliusБезСети(), курс=КурсSOL(),
                        режим="dry", исполнитель=исп)
        det.курс.значение, det.курс.когда = 200.0, time.time()
        det.слот_сети, det.t_слот = 100, time.time()
        det.баланс_sol, det.t_баланс = 5.0, time.time()
        t_ок = tx(pre=[бал(USDC, 600_000000)],
                   post=[бал(USDC, 0), бал("MINT_EX", 5_000000, idx=2)])
        r = det.обработать("ПОДПИСЬ_EX", 100, "SRC", "тест", t_ок)
        chk("решение о покупке дошло до исполнителя",
            исп.вызовы == [("ПОДПИСЬ_EX", 5.0)], исп.вызовы)
        chk("итог исполнителя лёг в запись решения",
            (r.get("exec") or {}).get("exec_code") == "DRY_RUN", r.get("exec"))
        chk("счётчик исполнений вырос", det.исполнено == 1, det.исполнено)
        chk("код исполнителя учтён",
            det.по_кодам_исполнителя == {"DRY_RUN": 1}, det.по_кодам_исполнителя)
        # ---- BLOOM НЕ ТОРГУЕТ ПО ПОЛОСНЫМ ГРУППАМ (решение владельца 25.09:
        # по 38 кошелькам пункта 1.1 и по источникам скорости "Bloom НЕ
        # торгует"). Это деньги: лишний вызов исполнителя -- покупка на 0.2 SOL
        # по источнику, по которому владелец покупать не велел.
        # Хозяин балансов в заглушке транзакции -- всегда SRC, поэтому покупка
        # узнаётся только у него: группу навешиваем на SRC и меняем её файлом.
        было_гр_и = os.environ.get("BLOOM_SOURCE_GROUPS")
        import bloom_source_groups as SGи  # noqa: PLC0415

        def одна_группа(имя_г, политика_г, минт_г, подпись_г):
            """Один сигнал по источнику SRC при заданной группе SRC."""
            ф = Path(d) / f"группы_{имя_г}.json"
            ф.write_text(json.dumps({"groups": {имя_г: политика_г}},
                                     ensure_ascii=False), encoding="utf-8")
            os.environ["BLOOM_SOURCE_GROUPS"] = str(ф)
            SGи.загрузить(заново=True)
            исп_о = ИсполнительЗаглушка()
            д_о = Детектор(источники={"SRC": имя_г},
                            состояние=ST.ExecState(base=Path(d) / f"s_{имя_г}",
                                                    kill=Path(d) / f"k_{имя_г}"),
                            helius=HeliusБезСети(), курс=КурсSOL(),
                            режим="dry", исполнитель=исп_о)
            д_о.курс.значение, д_о.курс.когда = 200.0, time.time()
            д_о.слот_сети, д_о.t_слот = 100, time.time()
            д_о.баланс_sol, д_о.t_баланс = 5.0, time.time()
            t_о = tx(pre=[бал(USDC, 600_000000)],
                      post=[бал(USDC, 0), бал(минт_г, 5_000000, idx=2)])
            р_о = д_о.обработать(подпись_г, 100, "SRC", "тест", t_о)
            return р_о, исп_о.вызовы, д_о.bloom_не_зван, д_о

        try:
            р_п, зовы_п_, не_зван_п, д_п = одна_группа(
                "lane_only", {"lane_sol": 0.05, "bloom_trades": False,
                               "fanout": True, "addresses": {"SRC": "BATCH-1"}},
                "MINT_ГР1", "ПОДПИСЬ_ГР1")
            chk("решение по полосному источнику -- покупка",
                р_п["action"] == "buy", (р_п.get("action"), р_п.get("code")))
            chk("но Bloom по нему НЕ позван ни разу",
                зовы_п_ == [] and не_зван_п == 1, (зовы_п_, не_зван_п))
            журнал_п = [json.loads(с) for с in д_п.состояние.decisions_path
                        .read_text(encoding="utf-8").strip().split("\n") if с]
            chk("в журнал легла причина: группа только для полосы",
                any(з.get("stage") == "bloom_skipped_by_group" for з in журнал_п),
                [з.get("stage") for з in журнал_п][-4:])
            р_с, зовы_с_, не_зван_с, _ = одна_группа(
                "speed_only", {"lane_sol": 0.01, "bloom_trades": False,
                                "fanout": False, "day_cap_sol": 0.2,
                                "stop_loss_sol": 0.15,
                                "snipers": [{"address": "SRC"}]},
                "MINT_ГР2", "ПОДПИСЬ_ГР2")
            chk("и по источнику скорости Bloom не позван",
                р_с["action"] == "buy" and зовы_с_ == [] and не_зван_с == 1,
                (р_с.get("action"), зовы_с_, не_зван_с))
            р_б, зовы_б_, не_зван_б, д_б = одна_группа(
                "bloom_lane", {"lane_sol": 0.05, "bloom_trades": True,
                                "fanout": True, "addresses": {"SRC": "BATCH-5"}},
                "MINT_ГР3", "ПОДПИСЬ_ГР3")
            chk("а там, где торговать разрешено, Bloom позван как раньше",
                [з[0] for з in зовы_б_] == ["ПОДПИСЬ_ГР3"] and не_зван_б == 0,
                (зовы_б_, не_зван_б, р_б.get("code")))
            chk("по bloom_lane размер Bloom не навязан: берётся из окружения",
                д_б.размер_bloom("SRC") is None, д_б.размер_bloom("SRC"))

            # РАЗМЕР BLOOM ПО ГРУППЕ -- ЭТО ДЕНЬГИ (решение владельца 25.09
            # вечером): по lane_only Bloom торгует и берёт 0.05, а не 0.2.
            # Проверяется и то, что Bloom позван, и то, что размер дошёл до
            # исполнителя: размер, потерянный по дороге, купил бы на 0.2.
            р_л2, зовы_л2, не_зван_л2, д_л2 = одна_группа(
                "lane_only", {"lane_sol": 0.05, "bloom_trades": True,
                               "bloom_sol": 0.05, "fanout": True,
                               "addresses": {"SRC": "BATCH-1"}},
                "MINT_ГР4", "ПОДПИСЬ_ГР4")
            chk("по lane_only Bloom позван, когда торговать разрешено",
                [з[0] for з in зовы_л2] == ["ПОДПИСЬ_ГР4"] and не_зван_л2 == 0,
                (зовы_л2, не_зван_л2, р_л2.get("code")))
            chk("и размер 0.05 дошёл до исполнителя, а не 0.2",
                д_л2.размер_bloom("SRC") == 0.05, д_л2.размер_bloom("SRC"))

            # ПОРОГ ВХОДА ПО ГРУППЕ. Это деньги: порог решает, покупаем мы по
            # этому сигналу или нет. Без своего порога у новых групп полоса на
            # них не торгует вовсе -- проверено живьём 25.09: 1489 сигналов ниже
            # общих 2 SOL за 50 минут и ни одной покупки.
            ф_п = Path(d) / "группы_порог.json"
            ф_п.write_text(json.dumps({"groups": {
                "lane_only": {"lane_sol": 0.05, "bloom_trades": False,
                               "fanout": True, "min_target_sol": 0.5,
                               "addresses": {"SRC": "BATCH-1"}},
            }}, ensure_ascii=False), encoding="utf-8")
            os.environ["BLOOM_SOURCE_GROUPS"] = str(ф_п)
            SGи.загрузить(заново=True)
            д_п2 = Детектор(источники={"SRC": "lane_only"},
                             состояние=ST.ExecState(base=Path(d) / "sp2",
                                                     kill=Path(d) / "kp2"),
                             helius=HeliusБезСети(), курс=КурсSOL(), режим="dry")
            chk("порог группы берётся из файла групп, а не общий",
                д_п2.порог_для("SRC") == 0.5, д_п2.порог_для("SRC"))
            chk("у источника не из групп порог прежний, общий",
                д_п2.порог_для("ЧУЖОЙ") == ПОРОГ_ВХОДА_SOL,
                д_п2.порог_для("ЧУЖОЙ"))
            ф_п.write_text(json.dumps({"groups": {
                "lane_only": {"lane_sol": 0.05, "bloom_trades": False,
                               "fanout": True,
                               "addresses": {"SRC": "BATCH-1"}},
            }}, ensure_ascii=False), encoding="utf-8")
            SGи.загрузить(заново=True)
            chk("порога в файле нет -- остаётся общий, ничего не занижается",
                д_п2.порог_для("SRC") == ПОРОГ_ВХОДА_SOL, д_п2.порог_для("SRC"))
        finally:
            if было_гр_и is None:
                os.environ.pop("BLOOM_SOURCE_GROUPS", None)
            else:
                os.environ["BLOOM_SOURCE_GROUPS"] = было_гр_и
            SGи.загрузить(заново=True)

        j = det.признак_жизни()
        chk("в признаке жизни видно, что исполнитель подключён",
            j["executor_attached"] is True and j["executed"] == 1, j.get("executed"))
        # Прогрев соединения к Bloom виден в признаке жизни: без этого
        # "соединение греется" было бы обещанием, а не проверяемым фактом.
        chk("без прогрева признак жизни говорит об этом прямо",
            j["bloom_keepalive"]["enabled"] is False
            and "исполнитель" in j["bloom_keepalive"]["why_not"],
            j.get("bloom_keepalive"))

        class ПрогревЗаглушка:
            def признак_жизни(self):
                return {"enabled": True, "period_s": 20.0, "ok": 3, "failed": 0,
                         "last_code": 200, "last_utc": "2026-09-24T12:00:00Z",
                         "last_error": ""}

        det.прогрев = ПрогревЗаглушка()
        chk("с прогревом в признаке жизни стоят числа",
            det.признак_жизни()["bloom_keepalive"]["ok"] == 3
            and det.признак_жизни()["bloom_keepalive"]["period_s"] == 20.0,
            det.признак_жизни().get("bloom_keepalive"))

        # падение исполнителя не валит детектор
        st2 = ST.ExecState(base=Path(d) / "s2", kill=Path(d) / "kill2")
        исп2 = ИсполнительЗаглушка(падать=True)
        det2 = Детектор(источники={"SRC": "BATCH-5"}, состояние=st2,
                         helius=HeliusБезСети(), курс=КурсSOL(),
                         режим="dry", исполнитель=исп2)
        det2.курс.значение, det2.курс.когда = 200.0, time.time()
        det2.слот_сети, det2.t_слот = 100, time.time()
        det2.баланс_sol, det2.t_баланс = 5.0, time.time()
        r2 = det2.обработать("ПОДПИСЬ_EX2", 100, "SRC", "тест", t_ок)
        chk("падение исполнителя не валит детектор",
            r2["action"] == "buy", r2.get("action"))
        chk("падение помечено отдельным кодом",
            (r2.get("exec") or {}).get("exec_code") == "EXECUTOR_CRASHED", r2.get("exec"))
        chk("решение всё равно записано в журнал",
            st2.decisions_path.exists() and st2.decisions_path.stat().st_size > 0)

        # без исполнителя детектор работает как прежде
        det3 = Детектор(источники={"SRC": "BATCH-5"}, состояние=st2,
                         helius=HeliusБезСети(), курс=КурсSOL(), режим="dry")
        j3 = det3.признак_жизни()
        chk("без исполнителя это видно в признаке жизни",
            j3["executor_attached"] is False and "executor" not in j3, j3.get("executor"))

        # Докупка без курса называется докупкой, а не "курса нет": курс тут
    # ничего не решает, и код должен говорить о причине, а не о нашей слепоте.
    ок_д, код_д, почему_д = фильтры_dbot(
        {"kind": "buy", "first_entry": False, "spend_mint": USDC, "spend_ui": 1000.0},
        None, порог_sol=2.0)
    chk("докупка без курса -- код докупки, а не UNKNOWN_RATE",
        ок_д is False and код_д == КОД_ДОКУПКА and "курса SOL при этом не было" in почему_д,
        (код_д, почему_д))
    ок_п, код_п, _ = фильтры_dbot(
        {"kind": "buy", "first_entry": True, "spend_mint": USDC, "spend_ui": 1000.0},
        None, порог_sol=2.0)
    chk("а первый вход без курса -- по-прежнему UNKNOWN_RATE",
        ок_п is False and код_п == КОД_НЕТ_КУРСА, код_п)

    # 15г. КУРС ПРОТУХ, А ПОКУПКУ ТЕРЯТЬ НЕЛЬЗЯ.
    #
    # Боевой случай 07:59:49Z: источник заплатил 1000 USDC (около 8.7 SOL при
    # пороге 2), а в горячем пути стояла проверка свежий() -- курс старше
    # одного TTL считался отсутствующим, и решение ушло в UNKNOWN_RATE.
    # Покупка потеряна на ровном месте: курс минутной давности отвечает на
    # вопрос "больше двух SOL или нет" так же верно, как секундный.
    к = КурсSOL(ttl_s=60.0)
    к.значение, к.когда = 100.0, time.time()
    v, оговорка, возраст = к.для_решения()
    chk("свежий курс отдаётся как есть", v == 100.0 and "свежий" in оговорка, (v, оговорка))
    к.когда = time.time() - 120          # два TTL: протух, но в пределах пяти
    v2, огов2, возр2 = к.для_решения()
    chk("протухший в пределах пяти TTL курс отдаётся с оговоркой",
        v2 == 100.0 and "протух" in огов2 and возр2 > 60, (v2, огов2, возр2))
    к.когда = time.time() - 400          # больше пяти TTL
    v3, огов3, _ = к.для_решения()
    chk("курс старше пяти TTL не годится", v3 is None and "не годится" in огов3,
        (v3, огов3))
    к_пустой = КурсSOL(ttl_s=60.0)
    v4, огов4, возр4 = к_пустой.для_решения()
    chk("курса нет вовсе -- сказано прямо",
        v4 is None and возр4 is None and "нет вовсе" in огов4, (v4, огов4))

    # И сквозная проверка: решение по покупке за стейбл при ПРОТУХШЕМ курсе --
    # покупка, а не UNKNOWN_RATE, и оговорка видна в записи.
    with _врем_каталог() as d_к:
        st_к = ST.ExecState(base=Path(d_к) / "s", kill=Path(d_к) / "k")

        class HeliusТих(Helius):
            def __init__(self):
                super().__init__(key="нет", служба="")

            def налог_минта(self, минт):
                return {"taxed": False, "fee_bps": None}

        дет_к = Детектор(источники={"SRC": "BATCH-5"}, состояние=st_к,
                          helius=HeliusТих(), курс=КурсSOL(ttl_s=60.0), режим="dry")
        дет_к.курс.значение = 100.0
        дет_к.курс.когда = time.time() - 120     # протух на два TTL
        дет_к.слот_сети, дет_к.t_слот = 100, time.time()
        дет_к.баланс_sol, дет_к.t_баланс = 5.0, time.time()
        дет_к.порог_для = lambda источник: 2.0  # noqa: ARG005
        t_стейбл = tx(pre=[бал(USDC, 1000_000000)],
                       post=[бал(USDC, 0), бал("МИНТ_С", 5_000000, idx=2)])
        r_к = дет_к.обработать("ПОДПИСЬ_КУРС", 100, "SRC", "тест", t_стейбл)
        chk("при протухшем курсе покупка за 1000 USDC всё равно BUY",
            r_к.get("action") == "buy", (r_к.get("code"), r_к.get("reason")))
        chk("и оговорка про курс лежит в записи решения",
            "протух" in (r_к.get("rate_state") or "") and r_к.get("rate_age_s") > 60,
            (r_к.get("rate_state"), r_к.get("rate_age_s")))
        chk("и такие решения считаются отдельно",
            дет_к.признак_жизни()["rate_stale_used"] == 1,
            дет_к.признак_жизни().get("rate_stale_used"))

        # А если курс старше пяти TTL -- по-прежнему честный отказ.
        дет_к2 = Детектор(источники={"SRC": "BATCH-5"}, состояние=st_к,
                           helius=HeliusТих(), курс=КурсSOL(ttl_s=60.0), режим="dry")
        дет_к2.курс.значение = 100.0
        дет_к2.курс.когда = time.time() - 400
        дет_к2.слот_сети, дет_к2.t_слот = 100, time.time()
        дет_к2.баланс_sol, дет_к2.t_баланс = 5.0, time.time()
        дет_к2.порог_для = lambda источник: 2.0  # noqa: ARG005
        r_к2 = дет_к2.обработать("ПОДПИСЬ_КУРС2", 100, "SRC", "тест", t_стейбл)
        chk("курс старше пяти TTL -- отказ UNKNOWN_RATE, а не выдуманный порог",
            r_к2.get("code") == КОД_НЕТ_КУРСА, (r_к2.get("code"), r_к2.get("reason")))

    # 15d. режим называют деньги: BLOOM_MODE в env прибит к "dry", а
        # покупки идут боевые. Если режим брать из env, боевые решения
        # попадают в докладе в раздел dry_run -- то есть не видны совсем.
        st4 = ST.ExecState(base=Path(d) / "s4", kill=Path(d) / "kill4")

        class ИсполнительБоевой(ИсполнительЗаглушка):
            mode = ST.MODE_LIVE

        det4 = Детектор(источники={"SRC": "BATCH-5"}, состояние=st4,
                         helius=HeliusБезСети(), курс=КурсSOL(),
                         режим="dry", исполнитель=ИсполнительБоевой())
        det4.курс.значение, det4.курс.когда = 200.0, time.time()
        det4.слот_сети, det4.t_слот = 100, time.time()
        det4.баланс_sol, det4.t_баланс = 5.0, time.time()
        chk("режим в признаке жизни -- боевой, а не из env",
            det4.признак_жизни()["mode"] == ST.MODE_LIVE,
            det4.признак_жизни()["mode"])
        r4 = det4.обработать("ПОДПИСЬ_EX4", 100, "SRC", "тест", t_ок)
        chk("режим в строке решения -- боевой",
            r4.get("mode") == ST.MODE_LIVE, r4.get("mode"))
        chk("без исполнителя режим остаётся из env",
            det3.признак_жизни()["mode"] == "dry", det3.признак_жизни()["mode"])

    # 16. маршрут: промежуточный токен виден, прямой -- нет
    def tx_маршрут(хоп=(), минты_чужие=(), подписанные=()):
        """Три разные вещи, которые прежнее правило путало.

        хоп -- минт ДВИГАЛСЯ на счёте источника (часть остатка ушла в
          маршрут): это настоящий хоп;
        минты_чужие -- двигались на ЧУЖИХ счетах в той же транзакции:
          чужая нога агрегатора, не наш хоп;
        подписанные -- источник только подписал перевод (он authority), а
          его собственный счёт этого минта создан пустым и остался нулём:
          раздача комиссий, не хоп. Ровно так выглядел служебный токен
          pump.fun 3NZ9JMVB... на живой сделке 24.09.
        """
        pre = [бал(USDC, 600_000000)]
        post = [бал(USDC, 0), бал("КУПЛЕН", 5_000000, idx=2)]
        for i, m in enumerate(хоп, start=10):
            pre.append(бал(m, 1_000000, owner="SRC", idx=i))
            post.append(бал(m, 900_000, owner="SRC", idx=i))
        for i, m in enumerate(минты_чужие, start=40):
            pre.append(бал(m, 1_000000, owner="ПУЛ", idx=i))
            post.append(бал(m, 2_000000, owner="ПУЛ", idx=i))
        for i, m in enumerate(подписанные, start=70):
            # счёт источника создан в этой же транзакции и остался нулём
            post.append(бал(m, 0, owner="SRC", idx=i))
        t = tx(pre=pre, post=post,
                инстр=("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
                        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"))
        t["meta"]["innerInstructions"] = [{"instructions": [
            {"programId": TOKEN_CLASSIC, "parsed": {
                "type": "transferChecked",
                "info": {"mint": m, "authority": "SRC", "amount": "44"}}}
            for m in подписанные]}]
        return t

    s_прямой = сигнал_из_транзакции(tx_маршрут(), "SRC", подпись="M1")
    chk("прямой маршрут: промежуточных нет",
        s_прямой["route"]["intermediate_mints"] == [], s_прямой["route"])
    chk("прямой маршрут не помечен как через промежуточный",
        s_прямой["route"]["via_intermediate"] is False)
    chk("вызовы DEX посчитаны", s_прямой["route"]["dex_calls"] == 2,
        s_прямой["route"]["dex_calls"])

    s_через = сигнал_из_транзакции(tx_маршрут(хоп=["ПРОМЕЖ"]), "SRC", подпись="M2")
    chk("промежуточный минт найден",
        s_через["route"]["intermediate_mints"] == ["ПРОМЕЖ"], s_через["route"])
    chk("помечен как через промежуточный",
        s_через["route"]["via_intermediate"] is True)
    chk("хопов больше, чем у прямого",
        s_через["route"]["hops_by_mints"]
        > s_прямой["route"]["hops_by_mints"],
        (s_через["route"]["hops_by_mints"],
         s_прямой["route"]["hops_by_mints"]))
    s_wsol = сигнал_из_транзакции(tx_маршрут(хоп=[WSOL, USDT]), "SRC", подпись="M3")
    chk("котировочные минты не считаются промежуточными",
        s_wsol["route"]["intermediate_mints"] == [], s_wsol["route"])

    # ЧУЖАЯ НОГА в той же транзакции -- не наш хоп. Ровно на этом 24.09
    # детектор дважды зарезал прямые покупки (pump.fun и Jupiter).
    s_чужая = сигнал_из_транзакции(tx_маршрут(минты_чужие=["ЧУЖОЙ_МИНТ"]), "SRC",
                                     подпись="M4")
    chk("минт с ЧУЖИХ счетов промежуточным не считается",
        s_чужая["route"]["intermediate_mints"] == []
        and s_чужая["route"]["via_intermediate"] is False, s_чужая["route"])
    chk("но он виден в записи отдельным полем",
        s_чужая["route"]["other_tx_mints"] == ["ЧУЖОЙ_МИНТ"], s_чужая["route"])
    chk("и сказано, каким признаком промежуточные искались",
        s_чужая["route"]["intermediate_from"] == "промежуточных нет",
        s_чужая["route"]["intermediate_from"])
    chk("а у настоящего хопа признак назван",
        s_через["route"]["intermediate_from"] == "движение на счетах источника",
        s_через["route"]["intermediate_from"])

    # Служебный токен, который источник только подписал: НЕ хоп.
    s_служ = сигнал_из_транзакции(tx_маршрут(подписанные=["СЛУЖЕБНЫЙ"]), "SRC",
                                   подпись="M7")
    chk("служебный токен, только подписанный источником, промежуточным не считается",
        s_служ["route"]["intermediate_mints"] == []
        and s_служ["route"]["via_intermediate"] is False, s_служ["route"])
    chk("но он виден отдельным полем записи",
        s_служ["route"]["authorized_only_mints"] == ["СЛУЖЕБНЫЙ"], s_служ["route"])

    # Инструкция memo: у неё parsed -- СТРОКА, а не объект. 24.09 в 14:10:50Z
    # боевой разбор упал на этом целиком (AttributeError у 'str'), сигнал
    # источника Beqv6dzT был потерян. Проверка повторяет ту форму дословно.
    tx_memo = tx_маршрут()
    tx_memo["transaction"]["message"]["instructions"].append(
        {"programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
         "program": "spl-memo", "parsed": "заметка строкой, а не объектом"})
    s_memo = сигнал_из_транзакции(tx_memo, "SRC", подпись="MEMO1")
    chk("инструкция memo с parsed-строкой не роняет разбор сигнала",
        s_memo is not None and "route" in s_memo, s_memo)
    tx_memo2 = tx_маршрут(подписанные=["СЛУЖЕБНЫЙ"])
    tx_memo2["meta"]["innerInstructions"].append(
        {"index": 0, "instructions": [{"programId": "MemoSq4", "parsed": "строка"}]})
    s_memo2 = сигнал_из_транзакции(tx_memo2, "SRC", подпись="MEMO2")
    chk("memo во ВНУТРЕННИХ инструкциях тоже не роняет разбор",
        s_memo2["route"]["authorized_only_mints"] == ["СЛУЖЕБНЫЙ"],
        s_memo2["route"])
    chk("и info-строка вместо объекта не роняет",
        сигнал_из_транзакции(
            (lambda t: (t["transaction"]["message"]["instructions"].append(
                {"programId": "P", "parsed": {"type": "transferChecked",
                                               "info": "строка"}}), t)[1])(
                tx_маршрут()), "SRC", подпись="MEMO3") is not None)

    # чужой перевод с явным минтом -- не наш: authority не источник
    t_чуж = tx_маршрут()
    t_чуж["meta"]["innerInstructions"] = [{"instructions": [
        {"programId": TOKEN_CLASSIC, "parsed": {
            "type": "transferChecked",
            "info": {"mint": "ЧУЖОЙ2", "authority": "КТО_ТО", "amount": "1"}}}]}]
    chk("перевод с чужим authority промежуточным не делает",
        сигнал_из_транзакции(t_чуж, "SRC", подпись="M6")["route"]
        ["intermediate_mints"] == [])

    with _врем_каталог() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "kill")
        r = решение(s_через, состояние=st, трата_sol=2.5, баланс_sol=3.0, текущий_слот=100)
        chk("маршрут через промежуточный -- пропуск", r["action"] == "skip", r)
        chk("код INTERMEDIATE_ROUTE", r["code"] == КОД_ПРОМЕЖУТОЧНЫЙ, r["code"])
        chk("это НАШ лимит, а не отказ DBot", r.get("filter") == "наш лимит", r.get("filter"))
        chk("помечено, что DBot бы купил", r.get("dbot_бы_купил") is True)
        r2 = решение(s_прямой, состояние=st, трата_sol=2.5, баланс_sol=3.0, текущий_слот=100)
        chk("прямой маршрут покупается", r2["action"] == "buy", r2)
        chk("в решении есть маршрут", (r2.get("route") or {}).get("dex_calls") == 2,
            r2.get("route"))

    # 16б. ID пула источника и маршрут нашей покупки
    DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
    CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"

    def tx_пул(*, владельцы, счета, программы=(DLMM,), подписанты=("SRC",),
                купленный="КУПЛЕН", сквозные=(), кто_переводит=None,
                движение=None):
        """движение={владелец: [минты]} -- счёт с РЕАЛЬНЫМ изменением остатка:
        именно так виден хоп маршрута на счетах своего кошелька."""
        балансы = []
        i = 0
        for вл, минты in владельцы.items():
            for м in минты:
                i += 1
                балансы.append(бал(м, 1_000000, owner=вл, idx=i))
        pre = list(балансы)
        post = балансы + [бал(купленный, 5_000000, owner="SRC", idx=90)]
        for вл, минты in (движение or {}).items():
            for м in минты:
                i += 1
                pre.append(бал(м, 1_000000, owner=вл, idx=i))
                post.append(бал(м, 400_000, owner=вл, idx=i))
        ключи = [{"pubkey": k, "signer": k in подписанты}
                  for k in list(подписанты) + list(счета)]
        return {"slot": 100,
                 "transaction": {"message": {
                     "accountKeys": ключи,
                     "instructions": [{"programId": pr, "accounts": list(счета)}
                                       for pr in программы]},
                     "signatures": ["SIG"]},
                 "meta": {"err": None, "fee": 0,
                           "preBalances": [10 ** 9], "postBalances": [10 ** 9 - 1],
                           "preTokenBalances": pre, "postTokenBalances": post,
                           "innerInstructions": [{"instructions": [
                               {"programId": TOKEN_CLASSIC, "parsed": {
                                   "type": "transferChecked",
                                   "info": {"mint": m,
                                             "authority": (кто_переводит
                                                           or list(подписанты)[0]),
                                             "amount": "1"}}}
                               for m in сквозные]}]}}

    t_пул = tx_пул(владельцы={"POOLA": ["КУПЛЕН", WSOL], "SRC": [WSOL]},
                    счета=("POOLA", "VAULT1"))
    s_пул = сигнал_из_транзакции(t_пул, "SRC", подпись="P1")
    chk("ID пула источника достаётся из его транзакции",
        s_пул.get("source_pool") == "POOLA", s_пул.get("source_pool"))
    chk("и метки об отсутствии пула нет",
        ФЛАГ_НЕТ_ПУЛА_SOL not in (s_пул.get("flags") or []), s_пул.get("flags"))

    # И ГЛАВНОЕ: ID пула должен доехать ДО ИСПОЛНИТЕЛЯ. Он покупает по
    # decision["source_pool"], а не по сигналу, и именно на этом стыке поле
    # терялось -- восемь покупок стенда ушли по минту при найденном пуле.
    with _врем_каталог() as d:
        stп = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        rп = решение(s_пул, состояние=stп, трата_sol=2.5, баланс_sol=3.0,
                      текущий_слот=100)
        chk("ID пула источника есть в ЗАПИСИ РЕШЕНИЯ, а не только в сигнале",
            rп.get("source_pool") == "POOLA", rп.get("source_pool"))
        chk("и кандидаты пулов тоже в записи",
            bool(rп.get("source_pool_candidates")), rп.get("source_pool_candidates"))

        class ИсполнительЗапоминает:
            mode = ST.MODE_LIVE_TEST

            def __init__(self):
                self.видел = []

            def execute(self, решение_вх, *, balance_sol, amount_sol=None):
                self.видел.append(решение_вх.get("source_pool"))
                return {"exec_code": "DRY_RUN", "reason": "заглушка"}

            def report(self):
                return {"live_buy_enabled": False}

        class HeliusТихий(Helius):
            def __init__(self):
                super().__init__(key="нет", служба="")

            def налог_минта(self, минт):
                return {"taxed": False, "fee_bps": None}

        испп = ИсполнительЗапоминает()
        stп2 = ST.ExecState(base=Path(d) / "s2", kill=Path(d) / "k2")
        детп = Детектор(источники={"SRC": "BATCH-5"}, состояние=stп2,
                         helius=HeliusТихий(), курс=КурсSOL(), режим="dry",
                         исполнитель=испп)
        детп.курс.значение, детп.курс.когда = 200.0, time.time()
        детп.слот_сети, детп.t_слот = 100, time.time()
        детп.баланс_sol, детп.t_баланс = 5.0, time.time()
        детп.порог_для = lambda источник: 0.0  # noqa: ARG005
        строка_п = детп.обработать("SIGPOOL", 100, "SRC", "тест", t_пул)
        chk("решение по этой транзакции -- покупка",
            строка_п.get("action") == "buy", (строка_п.get("code"), строка_п.get("reason")))
        chk("исполнитель ПОЛУЧИЛ ID пула источника, а не None",
            испп.видел == ["POOLA"], испп.видел)

    t_usdc = tx_пул(владельцы={"POOLB": ["КУПЛЕН", USDC], "SRC": [USDC]},
                     счета=("POOLB",))
    s_usdc = сигнал_из_транзакции(t_usdc, "SRC", подпись="P2")
    chk("пул только к USDC -- пула к SOL нет, и это помечено",
        s_usdc.get("source_pool") is None
        and ФЛАГ_НЕТ_ПУЛА_SOL in (s_usdc.get("flags") or []), s_usdc.get("flags"))
    chk("и причина названа словами", bool(s_usdc.get("pool_why_not")),
        s_usdc.get("pool_why_not"))

    t_кош = tx_пул(владельцы={"SRC": ["КУПЛЕН", WSOL]}, счета=("SRC",))
    s_кош = сигнал_из_транзакции(t_кош, "SRC", подпись="P3")
    chk("кошелёк источника за пул не выдаётся",
        s_кош.get("source_pool") is None, s_кош.get("source_pool"))

    t_служ = tx_пул(владельцы={"GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL":
                                ["КУПЛЕН", WSOL]},
                     счета=("GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",))
    s_служ = сигнал_из_транзакции(t_служ, "SRC", подпись="P4")
    chk("проверенный служебный адрес Raydium CPMM за пул не выдаётся",
        s_служ.get("source_pool") is None, s_служ.get("source_pool"))

    # прямизна нашей покупки
    наш_прямой = пул_и_прямизна(
        tx_пул(владельцы={"POOLA": ["КУПЛЕН", WSOL]}, счета=("POOLA",),
                подписанты=("НАШ",)), минт="КУПЛЕН", кошелёк="НАШ")
    chk("наша покупка одним пулом токен/WSOL -- прямая",
        наш_прямой["direct"] is True and наш_прямой["pool"] == "POOLA", наш_прямой)
    tx_хоп = tx_пул(владельцы={"POOL1": ["ПРОМЕЖ", WSOL], "POOL2": ["КУПЛЕН", "ПРОМЕЖ"]},
                     счета=("POOL1", "POOL2"), программы=(CLMM, DLMM),
                     подписанты=("НАШ",), движение={"НАШ": ["ПРОМЕЖ"]})
    наш_хоп = пул_и_прямизна(tx_хоп, минт="КУПЛЕН", кошелёк="НАШ")
    chk("двухшаговая покупка через промежуточный -- НЕ прямая",
        наш_хоп["direct"] is False, наш_хоп)
    chk("и промежуточный минт нашего маршрута виден по движению на нашем счёте",
        наш_хоп["route"]["intermediate_mints"] == ["ПРОМЕЖ"], наш_хоп["route"])
    # Даже без метки перевода маршрут не считается прямым: единственного
    # пула токен/WSOL в такой покупке нет, и этого признака достаточно.
    без_метки = пул_и_прямизна(
        tx_пул(владельцы={"POOL1": ["ПРОМЕЖ", WSOL], "POOL2": ["КУПЛЕН", "ПРОМЕЖ"]},
                счета=("POOL1", "POOL2"), программы=(CLMM, DLMM), подписанты=("НАШ",)),
        минт="КУПЛЕН", кошелёк="НАШ")
    chk("без метки перевода маршрут всё равно не прямой: нет пула токен/WSOL",
        без_метки["direct"] is False, без_метки)

    # метка расхождения маршрутов
    with _врем_каталог() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "kill")

        class HeliusНашаTx:
            def __init__(self, tx):
                self.tx = tx

            def транзакция(self, подпись, **kw):
                return self.tx

        детектор_р = Детектор(
            источники={"SRC": "BATCH-5"}, состояние=st, курс=КурсSOL(), режим="dry",
            # Кошелёк в этой проверке -- настоящая константа исполнителя:
            # разбор нашей покупки смотрит именно на него, и подставлять
            # условный "НАШ" значило бы проверять не то, что работает в бою.
            helius=HeliusНашаTx(tx_пул(
                владельцы={"POOL1": ["ПРОМЕЖ", WSOL], "POOL2": ["КУПЛЕН", "ПРОМЕЖ"]},
                счета=("POOL1", "POOL2"), программы=(CLMM, DLMM),
                подписанты=(ST.EXECUTOR_WALLET,),
                движение={ST.EXECUTOR_WALLET: ["ПРОМЕЖ"]})))
        st.write_intent(client_order_id="cr", mint="КУПЛЕН", source_sig="S",
                         source_slot=1, sol_in=0.001, pool="POOLA", program=None,
                         taxed=None, tax_bps=None, mode=ST.MODE_LIVE_TEST,
                         sell_after_s=28.8)
        зап = детектор_р.разобрать_нашу_покупку(
            cid="cr", минт="КУПЛЕН", подпись="НАШАПОДПИСЬ",
            источник_маршрут={"via_intermediate": False, "programs": ["Meteora DLMM"]},
            источник_пул="POOLA")
        chk("маршрут нашей покупки записан рядом с маршрутом источника",
            зап.get("our_route", {}).get("via_intermediate") is True
            and зап.get("source_route", {}).get("via_intermediate") is False, зап)
        chk("расхождение маршрутов помечено ROUTE_MISMATCH",
            ФЛАГ_МАРШРУТ_РАЗОШЁЛСЯ in (зап.get("flags") or []), зап.get("flags"))
        поз = st.positions()["cr"]
        chk("сторожу видно, что пула нашей покупки нет и маршрут не прямой",
            поз.get("our_pool") is None and поз.get("our_pool_direct") is False, поз)

        детектор_п = Детектор(
            источники={"SRC": "BATCH-5"}, состояние=st, курс=КурсSOL(), режим="dry",
            helius=HeliusНашаTx(tx_пул(владельцы={"POOLA": ["КУПЛЕН", WSOL]},
                                        счета=("POOLA",),
                                        подписанты=(ST.EXECUTOR_WALLET,))))
        st.write_intent(client_order_id="cp", mint="КУПЛЕН", source_sig="S2",
                         source_slot=2, sol_in=0.001, pool="POOLA", program=None,
                         taxed=None, tax_bps=None, mode=ST.MODE_LIVE_TEST,
                         sell_after_s=28.8)
        зап2 = детектор_п.разобрать_нашу_покупку(
            cid="cp", минт="КУПЛЕН", подпись="НАШАПОДПИСЬ2",
            источник_маршрут={"via_intermediate": False}, источник_пул="POOLA")
        chk("совпавший маршрут метки не получает",
            зап2.get("flags") == [], зап2.get("flags"))
        chk("и сторожу записан пул нашей покупки",
            st.positions()["cp"].get("our_pool") == "POOLA"
            and st.positions()["cp"].get("our_pool_direct") is True,
            st.positions()["cp"])

        class HeliusМолчит:
            def транзакция(self, подпись, **kw):
                return None

        детектор_м = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                               курс=КурсSOL(), режим="dry", helius=HeliusМолчит())
        зап3 = детектор_м.разобрать_нашу_покупку(
            cid="cq", минт="КУПЛЕН", подпись="НЕТ", источник_маршрут={},
            источник_пул=None)
        chk("узел не отдал нашу транзакцию -- сказано, а не выдумано",
            зап3.get("why_not") and зап3.get("our_pool") is None, зап3)

        # ЗАМЕР КРУГА по нашей же транзакции. Наш кошелёк подписан отдельно
        # и сигналом не становится: по нему ничего не покупается.
        детектор_з = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                               курс=КурсSOL(), режим="dry", helius=HeliusМолчит())
        st.write_intent(client_order_id="cz", mint="MINTZ", source_sig="SZ",
                         source_slot=5, sol_in=0.2, pool=None, program=None,
                         taxed=None, tax_bps=None, mode=ST.MODE_LIVE_TEST,
                         sell_after_s=28.8)
        т0 = st.positions()["cz"]["ts_intent"]
        st.update_position("cz", state="bought", signatures=["НАША_ПОКУПКА"],
                            ts_accepted=т0 + 0.4, bloom_ms=400.0)
        зк = детектор_з.отметить_нашу_транзакцию(
            "НАША_ПОКУПКА", 900, {"meta": {"err": None}}, т0 + 1.1)
        chk("круг от решения до появления в потоке посчитан",
            abs(зк["own_tx_seen_ms"] - 1100.0) < 1.0, зк)
        chk("и путь Bloom от ответа до включения -- отдельным числом",
            abs(зк["bloom_to_seen_ms"] - 700.0) < 1.0, зк)
        chk("bloom_ms взят из позиции, а не пересчитан",
            зк.get("bloom_ms") == 400.0, зк)
        chk("круг записан в позицию",
            abs(st.positions()["cz"]["own_tx_seen_ms"] - 1100.0) < 1.0,
            st.positions()["cz"])
        chk("код записи -- OWN_TX_SEEN, а не решение о покупке",
            зк["code"] == КОД_НАША_ТРАНЗАКЦИЯ and зк["stage"] == "own_tx_seen", зк)

        # Упавшая покупка меряется так же и помечается.
        st.write_intent(client_order_id="cu", mint="MINTU", source_sig="SU",
                         source_slot=6, sol_in=0.2, pool=None, program=None,
                         taxed=None, tax_bps=None, mode=ST.MODE_LIVE_TEST,
                         sell_after_s=28.8)
        т1 = st.positions()["cu"]["ts_intent"]
        st.update_position("cu", state="bought", signatures=["УПАВШАЯ_ПОКУПКА"],
                            ts_accepted=т1 + 0.3)
        зу = детектор_з.отметить_нашу_транзакцию(
            "УПАВШАЯ_ПОКУПКА", 901,
            {"meta": {"err": {"InstructionError": [8, {"Custom": 6004}]}}}, т1 + 0.9)
        chk("упавшая покупка меряется так же и помечена",
            зу["chain_ok"] is False and abs(зу["own_tx_seen_ms"] - 900.0) < 1.0, зу)

        # Наша транзакция без позиции -- продажа или спасение: круг не от чего.
        зп = детектор_з.отметить_нашу_транзакцию(
            "ЧУЖАЯ_НАША", 902, {"meta": {"err": None}}, time.time())
        chk("без позиции круг не выдумывается",
            "own_tx_seen_ms" not in зп and зп.get("why_not"), зп)
        chk("счётчик наших транзакций растёт",
            детектор_з.наших_транзакций == 3, детектор_з.наших_транзакций)

        # ТЕНЬ. Проверяем главное: она не задерживает Bloom, не может его
        # уронить и ничего не отправляет.
        class ТеньЗаглушка:
            вызовов = 0
            задержка = 0.0

            @staticmethod
            def shadow_build(*a, **kw):
                ТеньЗаглушка.вызовов += 1
                if ТеньЗаглушка.задержка:
                    time.sleep(ТеньЗаглушка.задержка)
                return {"ok": True, "sim_verdict": "would_pass",
                         "pool_label": "Pump AMM", "build_ms": 3.0,
                         "sim_ms": 40.0, "cap_usd": 250000.0,
                         "would_skip_cap": True}

        # Тень -- замер, и деплой она не блокирует. Но молчать о том, что
        # модуль не поднялся, нельзя: печатаем причину словами.
        print("модуль тени: " + ("загружен" if SB is not None
                                  else "НЕ загружен -- "
                                       + (SHADOW_IMPORT_ERR or "причина не записана")))
        подмена = ПодменаТени(ТеньЗаглушка)
        try:
            # У заглушки узла обязан быть call: тень получает его как
            # rpc_call. Без него падает построение аргументов, и тень
            # считается упавшей -- ровно это и поймала проверка.
            class HeliusДляТени(HeliusМолчит):
                @staticmethod
                def call(*a, **kw):
                    return None

            детектор_т = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                                   курс=КурсSOL(), режим="dry",
                                   helius=HeliusДляТени())
            детектор_т.тень_включена = True
            ТеньЗаглушка.задержка = 0.35
            t_до = time.time()
            детектор_т.запустить_тень({"signature": "S", "mint": "M",
                                        "source": "SRC", "spend_sol": 3.0}, {})
            прошло = time.time() - t_до
            chk("тень не задерживает горячий путь", прошло < 0.1, прошло)
            for _ in range(60):
                if детектор_т.теней:
                    break
                time.sleep(0.05)
            chk("тень отработала в своём потоке и посчитана",
                детектор_т.теней == 1 and детектор_т.теней_прошло == 1,
                (детектор_т.теней, детектор_т.теней_прошло,
                 детектор_т.теней_упало))
            chk("дорогая по капитализации помечена, но это только пометка",
                детектор_т.теней_дорогих == 1, детектор_т.теней_дорогих)

            class ТеньПадает:
                @staticmethod
                def shadow_build(*a, **kw):
                    raise RuntimeError("узел молчит")

            globals()["SB"] = ТеньПадает
            детектор_т2 = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                                    курс=КурсSOL(), режим="dry",
                                    helius=HeliusДляТени())
            детектор_т2.тень_включена = True
            детектор_т2.запустить_тень({"signature": "S2", "mint": "M"}, {})
            for _ in range(60):
                if детектор_т2.теней_упало:
                    break
                time.sleep(0.05)
            chk("падение тени не роняет детектор и записано",
                детектор_т2.теней_упало == 1, детектор_т2.теней_упало)

            детектор_т3 = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                                    курс=КурсSOL(), режим="dry",
                                    helius=HeliusДляТени())
            детектор_т3.тень_включена = False
            детектор_т3.запустить_тень({"signature": "S3", "mint": "M"}, {})
            chk("выключенная тень не запускается вовсе",
                детектор_т3.теней == 0, детектор_т3.теней)
            # ---- ТЕНЬ ЗА ЧАС И ЕЁ ПЕРЕЗАПУСК (владелец 25.09) ----
            # Условие: за 60 минут при трёх и более сигналах собрано ноль --
            # это поломка, а не тишина рынка. Молчать о ней нельзя.
            куда_тень: list = []

            class ОповещательТени:
                def послать(self, текст, *, куда="main"):
                    куда_тень.append((куда, текст))
                    return {"ok": True}

                def статус(self):
                    return {"enabled": True}

            детектор_ч = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                                   курс=КурсSOL(), режим="dry",
                                   helius=HeliusДляТени())
            детектор_ч.оповещатель = ОповещательТени()
            детектор_ч.тень_включена = True
            # Окно ещё не истекло -- ничего не закрываем и не тревожим.
            детектор_ч.тень_окно["signals"] = 5
            тихо = детектор_ч.сводка_тени_за_час()
            chk("окно тени раньше часа не закрывается",
                тихо["closed"] is False and not куда_тень, тихо)
            # Час прошёл, сигналы были, собрано ноль -- тревога и перезапуск.
            детектор_ч.тень_окно["since_ts"] = time.time() - 3601
            детектор_ч.тень_окно["not_built"] = {"нет участка SOL->Q": 5}
            плохо = детектор_ч.сводка_тени_за_час()
            chk("ноль собранных при пяти сигналах за час -- тревога владельцу",
                плохо["alarm"] is True and куда_тень
                and "не собрала" in куда_тень[0][1], (плохо, куда_тень))
            chk("и причина попала в текст тревоги",
                "нет участка" in куда_тень[0][1], куда_тень[0][1])
            chk("окно после закрытия обнулено, а не продолжено",
                детектор_ч.тень_окно["signals"] == 0
                and детектор_ч.тень_окно["built"] == 0, детектор_ч.тень_окно)
            # Час прошёл, но тень собирала -- тревоги нет.
            куда_тень.clear()
            детектор_ч.тень_окно["since_ts"] = time.time() - 3601
            детектор_ч.тень_окно["signals"] = 4
            детектор_ч.тень_окно["built"] = 2
            хорошо = детектор_ч.сводка_тени_за_час()
            chk("если тень собирала -- тревоги нет",
                хорошо["alarm"] is False and not куда_тень, (хорошо, куда_тень))
            # Меньше трёх сигналов за час -- это тишина рынка, а не поломка.
            детектор_ч.тень_окно["since_ts"] = time.time() - 3601
            детектор_ч.тень_окно["signals"] = 2
            детектор_ч.тень_окно["built"] = 0
            тишина = детектор_ч.сводка_тени_за_час()
            chk("два сигнала без сборки за час -- не тревога",
                тишина["alarm"] is False, тишина)

            # СЧЁТ КРЕДИТОВ ТЕНИ живёт на диске: перезапуск не даёт новой квоты.
            # СВОЙ каталог состояния, а не общий `st`: в общий пишут потоки
            # тени из предыдущих проверок (каждая тень дописывает свой счёт
            # кредитов), и на хосте 25.09 такой поток затёр файл уже ПОСЛЕ
            # нашей записи -- проверка падала не по делу, 405 из 407.
            st_кр = ST.ExecState(base=Path(d) / "shadow_credits",
                                  kill=Path(d) / "kill")
            детектор_кр = Детектор(источники={"SRC": "BATCH-5"}, состояние=st_кр,
                                    курс=КурсSOL(), режим="dry",
                                    helius=HeliusДляТени())
            детектор_кр.тень_вызовов = 7
            детектор_кр._записать_кредиты_тени()
            детектор_ч2 = Детектор(источники={"SRC": "BATCH-5"}, состояние=st_кр,
                                    курс=КурсSOL(), режим="dry",
                                    helius=HeliusДляТени())
            chk("кредиты тени за сутки переживают перезапуск",
                детектор_ч2.тень_кредитов_всего() == 7,
                детектор_ч2.тень_кредитов_всего())
            детектор_ч2.тень_вызовов = 3
            chk("и свои вызовы считаются поверх прежних",
                детектор_ч2.тень_кредитов_всего() == 10,
                детектор_ч2.тень_кредитов_всего())

            # ---- ТЕНЬ ПРОПУСКОВ УЗКОГО ФИЛЬТРА (владелец 25.09, пункт 4) ----
            # Без неё пропуск -- слово без числа: не видно, сберёг фильтр
            # деньги или отнял сделку.
            st_пр = ST.ExecState(base=Path(d) / "skips", kill=Path(d) / "kill")
            st_пр.log_decision({"stage": "decision", "action": "skip",
                                 "code": КОД_НАЛОГ_МАРШРУТА,
                                 "signature": "ПОДПИСЬ_ПРОПУСКА",
                                 "mint": "MINT_П", "source": "SRC",
                                 "slot": 500, "source_pool": "ПУЛ_П",
                                 "route_transfer_fee_bps": 800,
                                 "ts": time.time() - 100})

            class HeliusПропуск(HeliusДляТени):
                def транзакция(self, подпись, **kw):
                    return {"slot": 500, "blockTime": 1_790_000_000,
                            "meta": {"err": None, "preTokenBalances": [],
                                     "postTokenBalances": []}}

            детектор_пр = Детектор(источники={"SRC": "BATCH-5"}, состояние=st_пр,
                                    курс=КурсSOL(), режим="dry",
                                    helius=HeliusПропуск())
            было_PC = globals().get("PC")
            вызовы_пула: list = []

            def заглушка_пула(helius, *, минт: str, tx_источника,
                               кошелёк_источника, подпись_нашей=None,
                               кошелёк_наш=None):
                """Подпись ОДИН В ОДИН с bloom_price_curve.пул_для_кривой и
                словарь на выходе, как у настоящей функции.

                Прежняя заглушка брала любые **кв и отдавала строку -- и
                поэтому пропустила живую ошибку 25.09: вызов шёл с
                "кошелёк=", настоящая функция падала TypeError, и тень
                пропуска молча не мерила ничего."""
                вызовы_пула.append({"минт": минт,
                                     "кошелёк_источника": кошелёк_источника})
                return {"pool": "ПУЛ_НАЙДЕН", "pool_kind": "пул",
                        "pool_from": "транзакция источника", "why_not": None}

            класс_кривой = type("КриваяЗаглушка", (), {
                "цена_входа_источника": staticmethod(
                    lambda tx, минт, **кв: {"known": True, "price": 1.0e-6,
                                            "quote": WSOL}),
                "пул_для_кривой": staticmethod(заглушка_пула),
                "кривая": staticmethod(lambda *a, **кв: {
                    "known": True,
                    "points": [{"point": "+1 блок", "known": True, "price": 1.1e-6,
                                "vs_entry_pct": 10.0},
                               {"point": "+28.8 с", "known": True, "price": 0.8e-6,
                                "vs_entry_pct": -20.0}]})})
            globals()["PC"] = класс_кривой
            try:
                итог_пр = детектор_пр.догнать_цены_пропусков()
                строки_пр = [json.loads(с) for с in
                              st_пр.decisions_path.read_text(encoding="utf-8")
                              .strip().split("\n")]
                тени_пр = [с for с in строки_пр if с.get("stage") == "skip_price"]
                chk("тень пропуска посчитана и записана строкой",
                    итог_пр["measured"] == 1 and len(тени_пр) == 1
                    and тени_пр[0]["signature"] == "ПОДПИСЬ_ПРОПУСКА",
                    (итог_пр, тени_пр))
                chk("в тени пропуска есть точки S+1 и 28.8 с с процентом от входа",
                    [т["point"] for т in тени_пр[0]["points"]] == ["+1 блок", "+28.8 с"]
                    and тени_пр[0]["points"][1]["vs_entry_pct"] == -20.0,
                    тени_пр[0].get("points"))
                # Второй проход по тому же пропуску не должен считать заново:
                # каждый лишний проход -- это кредиты за уже известное.
                итог_пр2 = детектор_пр.догнать_цены_пропусков()
                chk("уже измеренный пропуск второй раз не считается",
                    итог_пр2["looked"] == 0, итог_пр2)
                # Свежий пропуск (моложе 35 с) ещё не считается: 28.8 с не прошли.
                st_пр.log_decision({"stage": "decision", "action": "skip",
                                     "code": КОД_НАЛОГ_МАРШРУТА,
                                     "signature": "ПОДПИСЬ_СВЕЖАЯ", "mint": "M2",
                                     "source": "SRC", "slot": 501,
                                     "ts": time.time()})
                итог_пр3 = детектор_пр.догнать_цены_пропусков()
                chk("свежий пропуск ждёт, пока пройдут 28.8 с",
                    итог_пр3["looked"] == 0, итог_пр3)
                # ПРОПУСК БЕЗ НАЗВАННОГО ПУЛА -- ровно тот случай, что был на
                # живом пропуске 25.09: адрес пула в записи не лежит, и его
                # надо искать по транзакции источника.
                st_пр.log_decision({"stage": "decision", "action": "skip",
                                     "code": КОД_НАЛОГ_МАРШРУТА,
                                     "signature": "ПОДПИСЬ_БЕЗ_ПУЛА", "mint": "M3",
                                     "source": "SRC", "slot": 502,
                                     "ts": time.time() - 100})
                итог_бп = детектор_пр.догнать_цены_пропусков()
                строки_бп = [json.loads(с) for с in
                              st_пр.decisions_path.read_text(encoding="utf-8")
                              .strip().split("\n")]
                тень_бп = [с for с in строки_бп
                            if с.get("stage") == "skip_price"
                            and с.get("signature") == "ПОДПИСЬ_БЕЗ_ПУЛА"]
                chk("пропуск без названного пула: пул найден по транзакции источника",
                    итог_бп["measured"] == 1 and len(тень_бп) == 1
                    and тень_бп[0].get("pool") == "ПУЛ_НАЙДЕН"
                    and тень_бп[0].get("pool_from") == "транзакция источника"
                    and bool(тень_бп[0].get("points")),
                    (итог_бп, тень_бп))
                chk("кошелёк источника передан пулу под своим именем",
                    len(вызовы_пула) == 1
                    and вызовы_пула[0]["кошелёк_источника"] == "SRC"
                    and вызовы_пула[0]["минт"] == "M3",
                    вызовы_пула)
                # НЕУДАЧНАЯ тень пропуска (без точек) должна пробоваться
                # снова: ночью 25.09 первая попытка упала, запись легла, и
                # пропуск считался измеренным навсегда.
                st_пр.log_decision({"stage": "decision", "action": "skip",
                                     "code": КОД_НАЛОГ_МАРШРУТА,
                                     "signature": "ПОДПИСЬ_ПОВТОР", "mint": "M4",
                                     "source": "SRC", "slot": 503,
                                     "ts": time.time() - 100})
                st_пр.log_decision({"stage": "skip_price",
                                     "signature": "ПОДПИСЬ_ПОВТОР",
                                     "why_not": "TypeError: притворная ошибка"})
                итог_пв = детектор_пр.догнать_цены_пропусков()
                chk("пропуск с неудачной тенью пробуется снова и измеряется",
                    итог_пв["looked"] == 1 and итог_пв["measured"] == 1,
                    итог_пв)
                # А после предела попыток -- больше не пробуется.
                st_пр.log_decision({"stage": "decision", "action": "skip",
                                     "code": КОД_НАЛОГ_МАРШРУТА,
                                     "signature": "ПОДПИСЬ_ТРИЖДЫ", "mint": "M5",
                                     "source": "SRC", "slot": 504,
                                     "ts": time.time() - 100})
                for _ in range(ПОПЫТОК_ТЕНИ_ПРОПУСКА):
                    st_пр.log_decision({"stage": "skip_price",
                                         "signature": "ПОДПИСЬ_ТРИЖДЫ",
                                         "why_not": "узел молчит"})
                итог_пт = детектор_пр.догнать_цены_пропусков()
                chk("после трёх неудач пропуск больше не пробуется",
                    итог_пт["looked"] == 0, итог_пт)
                # Кривая отказала -- причина обязана лежать в записи, и
                # "измерено" при этом не растёт.
                класс_кривой.кривая = staticmethod(
                    lambda *a, **кв: {"known": False,
                                       "why_not": "сделок пула у цели нет"})
                st_пр.log_decision({"stage": "decision", "action": "skip",
                                     "code": КОД_НАЛОГ_МАРШРУТА,
                                     "signature": "ПОДПИСЬ_БЕЗ_ТОЧЕК", "mint": "M6",
                                     "source": "SRC", "slot": 505,
                                     "ts": time.time() - 100})
                итог_бт = детектор_пр.догнать_цены_пропусков()
                строки_бт = [json.loads(с) for с in
                              st_пр.decisions_path.read_text(encoding="utf-8")
                              .strip().split("\n")]
                тень_бт = [с for с in строки_бт
                            if с.get("stage") == "skip_price"
                            and с.get("signature") == "ПОДПИСЬ_БЕЗ_ТОЧЕК"]
                chk("отказ кривой записан причиной и не считается измерением",
                    итог_бт["looked"] == 1 and итог_бт["measured"] == 0
                    and len(тень_бт) == 1
                    and "сделок пула" in (тень_бт[0].get("why_not") or ""),
                    (итог_бт, тень_бт))
            finally:
                if было_PC is None:
                    globals().pop("PC", None)
                else:
                    globals()["PC"] = было_PC
        finally:
            подмена.вернуть()
        # На хосте без модуля тени SB есть и равно None. Подмена обязана
        # оставить имя на месте, иначе следующий же Детектор() падает.
        нет_модуля = ПодменаТени(None)
        try:
            вложенная = ПодменаТени(ТеньЗаглушка)
            вложенная.вернуть()
            chk("имя SB переживает подмену, когда модуля тени нет",
                "SB" in globals() and globals()["SB"] is None,
                ("SB" in globals(), globals().get("SB")))
            Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                      курс=КурсSOL(), режим="dry", helius=HeliusМолчит())
            chk("детектор поднимается и без модуля тени", True)
        finally:
            нет_модуля.вернуть()
        chk("после проверок тени модуль на месте, как был",
            "SB" in globals(), "SB" in globals())

        # ---- ПОЛОСА СВОЕЙ ОТПРАВКИ В ДЕТЕКТОРЕ ----
        # Проверяем ровно то, что стоит денег: полоса не задерживает Bloom и
        # не может его уронить; тёплый хеш не тратит кредитов, когда полоса
        # выключена; своя подпись находит СВОЮ позицию; количество для
        # сторожа берётся из цепи, а не из предположения.
        print("модуль полосы: " + ("загружен" if OS is not None
                                    else "НЕ загружен -- "
                                         + (OWN_SEND_IMPORT_ERR
                                            or "причина не записана")))
        настоящая_полоса = globals().get("OS")
        # СВОЁ состояние: позиции полосы в общем каталоге путали счёт
        # следующим проверкам -- догон chain_ok считал их своими.
        st_п = ST.ExecState(base=Path(d) / "lane_det", kill=Path(d) / "kill")

        class ПолосаЗаглушка:
            МЕТКА = ST.МЕТКА_ПОЛОСЫ
            ЛИМИТ_ОТКРЫТЫХ = 1
            ЛИМИТ_В_СУТКИ = 20
            СТОП_ПОДРЯД_УПАВШИХ = 3
            СТОП_УБЫТОК_SOL = 0.1
            вызовы: list = []
            задержка = 0.0
            падать = False

            @staticmethod
            def живьём():
                return False

            @staticmethod
            def размер_sol():
                return 0.01

            @staticmethod
            def состояние_полосы(позиции, **кв):
                return {"open": 0, "today": 0, "failed_in_row": 0, "pnl_sol": 0.0}

            @staticmethod
            def купленное_raw(tx, кошелёк, минт):
                return настоящая_полоса.купленное_raw(tx, кошелёк, минт)

            @staticmethod
            def натив_покупки(tx, кошелёк):
                return настоящая_полоса.натив_покупки(tx, кошелёк)

            # ЗАГЛУШКА ПОВТОРЯЕТ ПОДПИСЬ НАСТОЯЩЕГО МОДУЛЯ, а не придумывает
            # свою: 25.09 заглушка с **кв спрятала настоящую ошибку вызова на
            # живых деньгах, и это стоило прогона.
            @staticmethod
            def кошелёк_полосы():
                return настоящая_полоса.кошелёк_полосы()

            @staticmethod
            def ключ_полосы():
                return настоящая_полоса.ключ_полосы()

            @staticmethod
            def свой_кошелёк():
                return настоящая_полоса.свой_кошелёк()

            # КОНТРОЛЬ ДОСТАВКИ. Заглушка зовёт НАСТОЯЩИЕ функции модуля --
            # так признак жизни проверяет то, что будет на хосте, а не то,
            # что удобно тесту.
            ЛИМИТ_КОНТРОЛЕЙ_В_СУТКИ = настоящая_полоса.ЛИМИТ_КОНТРОЛЕЙ_В_СУТКИ
            ПОТОЛОК_РАСХОДА_КОНТРОЛЯ_SOL = (
                настоящая_полоса.ПОТОЛОК_РАСХОДА_КОНТРОЛЯ_SOL)
            ПОТОЛОК_КОНТРОЛЯ_НА_СЕРВИС_SOL = (
                настоящая_полоса.ПОТОЛОК_КОНТРОЛЯ_НА_СЕРВИС_SOL)

            ПОТОЛОК_ЧАЕВЫХ_ПУЛА_SOL = настоящая_полоса.ПОТОЛОК_ЧАЕВЫХ_ПУЛА_SOL

            @staticmethod
            def контроль_включён():
                return настоящая_полоса.контроль_включён()

            @staticmethod
            def пул_включён():
                return настоящая_полоса.пул_включён()

            @staticmethod
            def расход_контроля(состояние, **кв):
                return настоящая_полоса.расход_контроля(состояние, **кв)

            @staticmethod
            def кошелёк_готов():
                return настоящая_полоса.кошелёк_готов()

            @staticmethod
            def отметить_итог_несчитаемым(состояние, cid, причина):
                return настоящая_полоса.отметить_итог_несчитаемым(
                    состояние, cid, причина)

            @staticmethod
            def провести(**кв):
                ПолосаЗаглушка.вызовы.append(кв)
                if ПолосаЗаглушка.падать:
                    raise RuntimeError("узел молчит")
                if ПолосаЗаглушка.задержка:
                    time.sleep(ПолосаЗаглушка.задержка)
                return {"stage": "dry", "ok": True, "dry": True, "sent": False,
                        "sim_verdict": "would_pass"}

        было_имя_OS = "OS" in globals()
        было_OS = globals().get("OS")
        globals()["OS"] = ПолосаЗаглушка
        try:
            class HeliusСчётный:
                вызовов = 0
                хеш = "ХЕШ_УЗЛА"
                падать = False
                tx_ответ = None
                # Признак жизни читает эти поля у настоящего узла.
                по_методам: dict = {}
                учёт_пишется = False
                учёт_почему = "заглушка самопроверки"
                метр = None

                @staticmethod
                def налог_минта(минт):
                    return {"taxed": False, "tax_bps": 0}

                def call(self, метод, параметры=None, **kw):
                    HeliusСчётный.вызовов += 1
                    if HeliusСчётный.падать:
                        raise RuntimeError("узел молчит")
                    if метод == "getLatestBlockhash":
                        return {"value": {"blockhash": HeliusСчётный.хеш}}
                    return None

                # Заглушка знает, КАКАЯ подпись села: без этого нельзя
                # проверить главное -- что догон спрашивает про севшую, а не
                # про принятую.
                севшая_ответ: dict = {}
                tx_по_подписи: dict = {}

                def транзакция(self, подпись, **kw):
                    if HeliusСчётный.tx_по_подписи:
                        return HeliusСчётный.tx_по_подписи.get(подпись)
                    return HeliusСчётный.tx_ответ

                def севшая_подпись(self, подписи):
                    HeliusСчётный.вызовов += 1
                    if HeliusСчётный.севшая_ответ:
                        return dict(HeliusСчётный.севшая_ответ)
                    return {"signature": None, "slot": None, "err": None,
                            "why_not": "заглушка не знает"}

            хел = HeliusСчётный()
            детектор_пп = Детектор(источники={"SRC": "BATCH-5"}, состояние=st_п,
                                    курс=КурсSOL(), режим="dry", helius=хел)

            # 1. ВЫКЛЮЧЕННАЯ ПОЛОСА: ни потока, ни кредита.
            детектор_пп.полоса_включена = False
            HeliusСчётный.вызовов = 0
            ПолосаЗаглушка.вызовы.clear()
            детектор_пп.запустить_полосу({"signature": "SP0", "mint": "M"}, {})
            х0 = детектор_пп.обновить_blockhash()
            chk("выключенная полоса не запускается и хеш не обновляет",
                not ПолосаЗаглушка.вызовы and х0["ok"] is False
                and HeliusСчётный.вызовов == 0,
                (ПолосаЗаглушка.вызовы, х0, HeliusСчётный.вызовов))

            # 2. ТЁПЛЫЙ ХЕШ: значение и время, а на ошибке узла старое не трём.
            детектор_пп.полоса_включена = True
            х1 = детектор_пп.обновить_blockhash()
            chk("тёплый хеш обновлён и время записано",
                х1["ok"] and детектор_пп.blockhash == "ХЕШ_УЗЛА"
                and детектор_пп.blockhash_ts is not None, (х1, детектор_пп.blockhash))
            HeliusСчётный.падать = True
            х2 = детектор_пп.обновить_blockhash()
            chk("узел молчит -- старый хеш НЕ стёрт, причина названа",
                х2["ok"] is False and детектор_пп.blockhash == "ХЕШ_УЗЛА"
                and детектор_пп.blockhash_почему, (х2, детектор_пп.blockhash))
            HeliusСчётный.падать = False

            # 3. ПОЛОСА НЕ ЗАДЕРЖИВАЕТ ГОРЯЧИЙ ПУТЬ и получает всё нужное.
            ПолосаЗаглушка.вызовы.clear()
            ПолосаЗаглушка.задержка = 0.35
            t_до_п = time.time()
            детектор_пп.запустить_полосу({"signature": "ПОДПИСЬ_ИСТОЧНИКА",
                                          "mint": "MINTL", "source": "SRC",
                                          "source_slot": 4242}, {"meta": {}})
            прошло_п = time.time() - t_до_п
            chk("полоса не задерживает горячий путь", прошло_п < 0.1, прошло_п)
            for _ in range(80):
                if детектор_пп.полос_путей:
                    break
                time.sleep(0.05)
            зов = (ПолосаЗаглушка.вызовы or [{}])[0]
            chk("полоса отработала в своём потоке и посчитана по стадии",
                детектор_пп.полос_путей == 1
                and детектор_пп.полос_по_стадиям.get("dry") == 1
                and детектор_пп.полос_отправлено == 0,
                (детектор_пп.полос_путей, детектор_пп.полос_по_стадиям))
            # Живых потоков тут может быть и один: путь считается ДО того,
            # как поток вернётся из функции, и требовать ноль -- значит
            # проверять скорость бегунка, а не код (на хосте так и вышло).
            chk("запуск полосы посчитан отдельно от пути",
                детектор_пп.полос_запусков == 1
                and детектор_пп.полос_потоков_живых() in (0, 1),
                (детектор_пп.полос_запусков, детектор_пп.полос_потоков_живых()))
            свежий_д = Детектор(источники={"SRC": "BATCH-5"}, состояние=st_п,
                                 курс=КурсSOL(), режим="dry", helius=хел)
            chk("у детектора без запусков полоса не считает ни путей, ни запусков",
                свежий_д.полос_запусков == 0 and свежий_д.полос_путей == 0,
                (свежий_д.полос_запусков, свежий_д.полос_путей))
            chk("полосе переданы состояние, тёплый хеш с его временем, минт и слот",
                зов.get("состояние") is st_п and зов.get("blockhash") == "ХЕШ_УЗЛА"
                and зов.get("blockhash_ts") == детектор_пп.blockhash_ts
                and зов.get("минт") == "MINTL"
                and зов.get("источник_слот") == 4242, зов)
            chk("ключ операции -- от подписи источника, а не случайный",
                зов.get("ключ_операции") == "own-ПОДПИСЬ_ИСТОЧНИКА"
                and зов.get("источник_подпись") == "ПОДПИСЬ_ИСТОЧНИКА", зов)
            # ТЕНЬ СИМУЛЯЦИИ ПЕРЕДАНА ОТДЕЛЬНЫМ ЖУРНАЛОМ (решение владельца
            # 25.09). Не передать её -- значит остаться без вердикта вовсе:
            # с пути сделки симуляция убрана, и другого места у неё нет.
            chk("полосе передан журнал тени симуляции",
                callable(зов.get("тень_сим")), зов.get("тень_сим"))
            зов["тень_сим"]({"stage": "lane_sim", "would_pass": False,
                              "sim_ms": 51.0})
            строки_тени = [json.loads(с_) for с_ in
                            st_п.decisions_path.read_text(encoding="utf-8")
                            .strip().split("\n") if с_.strip()]
            строки_тени = [р for р in строки_тени
                            if р.get("stage") == "lane_sim"]
            chk("вердикт тени лёг в журнал и посчитан отдельно от путей",
                len(строки_тени) == 1 and строки_тени[0].get("sim_ms") == 51.0
                and детектор_пп.полос_тень_сим == 1
                and детектор_пп.полос_тень_сим_отказов == 1,
                (строки_тени, детектор_пп.полос_тень_сим))
            ПолосаЗаглушка.задержка = 0.0

            # 3б. ОСТАНОВКА ПОЛОСЫ -- СТРОКОЙ ВЛАДЕЛЬЦУ. Молча встать нельзя:
            # остановившийся замер выглядит как "сигналов нет".
            куда_стоп: list = []

            class ОповещательСтопа:
                def послать(self, текст, *, куда="main"):
                    куда_стоп.append((куда, текст))
                    return {"ok": True}

                def статус(self):
                    return {"enabled": True}

            детектор_пп.оповещатель = ОповещательСтопа()
            ПолосаЗаглушка.вызовы.clear()
            прежнее_провести = ПолосаЗаглушка.провести
            ПолосаЗаглушка.провести = staticmethod(
                lambda **кв: {"stage": "gate", "ok": False, "sent": False,
                              "why_not": "сумма выше предела",
                              "kill_set": {"ok": True, "already": False,
                                           "why": "сумма выше предела: проверка"}})
            было_стадий = dict(детектор_пп.полос_по_стадиям)
            детектор_пп.запустить_полосу({"signature": "SP_STOP", "mint": "M"}, {})
            for _ in range(80):
                if куда_стоп:
                    break
                time.sleep(0.05)
            chk("остановка полосы уходит тревогой в основной чат",
                куда_стоп and куда_стоп[0][0] == "main"
                and "ОСТАНОВЛЕНА" in куда_стоп[0][1]
                and "сумма выше предела" in куда_стоп[0][1], куда_стоп)
            ПолосаЗаглушка.провести = прежнее_провести
            детектор_пп.полос_по_стадиям = было_стадий
            детектор_пп.оповещатель = None

            # 4. ПАДЕНИЕ ПОЛОСЫ НЕ РОНЯЕТ ДЕТЕКТОР.
            ПолосаЗаглушка.падать = True
            детектор_пп.запустить_полосу({"signature": "SP2", "mint": "M"}, {})
            for _ in range(80):
                if детектор_пп.полос_по_стадиям.get("crashed"):
                    break
                time.sleep(0.05)
            chk("падение полосы записано и детектор жив",
                детектор_пп.полос_по_стадиям.get("crashed") == 1,
                детектор_пп.полос_по_стадиям)
            ПолосаЗаглушка.падать = False

            # 5. СВОЯ ПОДПИСЬ НАХОДИТ СВОЮ ПОЗИЦИЮ, количество -- из цепи.
            st_п.write_intent(client_order_id="lane1", mint="MINTL", source_sig="SL",
                             source_slot=7, sol_in=0.01, pool=None, program=None,
                             taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                             sell_after_s=28.8, lane=ST.МЕТКА_ПОЛОСЫ)
            т_п = st_п.positions()["lane1"]["ts_intent"]
            st_п.update_position("lane1", state="bought",
                                lane_signature="ПОДПИСЬ_ПОЛОСЫ",
                                ts_sent=т_п + 0.05)
            tx_полосы = {"meta": {"err": None, "preTokenBalances": [],
                                   "postTokenBalances": [
                                       {"accountIndex": 3,
                                        "owner": ST.EXECUTOR_WALLET,
                                        "mint": "MINTL",
                                        "uiTokenAmount": {"amount": "8880000",
                                                          "decimals": 6,
                                                          "uiAmount": 8.88}}]}}
            зп1 = детектор_пп.отметить_нашу_транзакцию(
                "ПОДПИСЬ_ПОЛОСЫ", 950, tx_полосы, т_п + 0.35)
            поз_л = st_п.positions()["lane1"]
            chk("транзакция полосы нашлась по своей подписи и посчитана отдельно",
                зп1.get("stage") == "lane_seen"
                and abs(зп1.get("lane_send_to_seen_ms") - 300.0) < 5.0
                and поз_л.get("bloom_to_seen_ms") is None, (зп1, поз_л))
            chk("количество полосы записано СЫРЫМИ единицами и вердикт цепи тоже",
                поз_л.get("lane_bought_raw") == 8_880_000
                and поз_л.get("chain_ok") is True, поз_л)

            # 6. БЕЗ meta количество не выдумывается, его добирает пульс.
            st_п.write_intent(client_order_id="lane2", mint="MINTM", source_sig="SL2",
                             source_slot=8, sol_in=0.01, pool=None, program=None,
                             taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                             sell_after_s=28.8, lane=ST.МЕТКА_ПОЛОСЫ)
            st_п.update_position("lane2", state="bought",
                                lane_signature="ПОДПИСЬ_ПОЛОСЫ_2",
                                ts_sent=time.time())
            детектор_пп.отметить_нашу_транзакцию(
                "ПОДПИСЬ_ПОЛОСЫ_2", 951, {"meta": {"err": None}}, time.time())
            chk("без своих балансов количество остаётся неизвестным, а не нулём",
                st_п.positions()["lane2"].get("lane_bought_raw") is None
                and st_п.positions()["lane2"].get("lane_bought_why_not"),
                st_п.positions()["lane2"])
            HeliusСчётный.tx_ответ = {"meta": {
                "err": None, "preTokenBalances": [],
                "postTokenBalances": [{"accountIndex": 3,
                                       "owner": ST.EXECUTOR_WALLET,
                                       "mint": "MINTM",
                                       "uiTokenAmount": {"amount": "777",
                                                         "decimals": 9,
                                                         "uiAmount": 0.0}}]}}
            догон = детектор_пп.догнать_купленное_полосы()
            chk("пульс добрал количество по подписи",
                догон["filled"] == 1
                and st_п.positions()["lane2"].get("lane_bought_raw") == 777,
                (догон, st_п.positions()["lane2"]))
            # Догон не ходит дважды за уже известным количеством.
            догон2 = детектор_пп.догнать_купленное_полосы()
            chk("за известным количеством догон больше не ходит",
                догон2["filled"] == 0, догон2)
            # Узел молчит -- попытки считаются и не идут вечно.
            HeliusСчётный.tx_ответ = None
            st_п.write_intent(client_order_id="lane3", mint="MINTN", source_sig="SL3",
                             source_slot=9, sol_in=0.01, pool=None, program=None,
                             taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                             sell_after_s=28.8, lane=ST.МЕТКА_ПОЛОСЫ)
            st_п.update_position("lane3", state="bought",
                                lane_signature="ПОДПИСЬ_ПОЛОСЫ_3")
            for _ in range(ПОПЫТОК_МЕСТА_В_БЛОКЕ + 2):
                детектор_пп.догнать_купленное_полосы()
            chk("попытки догона количества ограничены и причина записана",
                int(st_п.positions()["lane3"].get("lane_bought_tries") or 0)
                == ПОПЫТОК_МЕСТА_В_БЛОКЕ
                and st_п.positions()["lane3"].get("lane_bought_why_not"),
                st_п.positions()["lane3"])

            # 6б. ДОГОН ИДЁТ ЗА СЕВШЕЙ ПОДПИСЬЮ, А НЕ ЗА ПРИНЯТОЙ. Вариантов
            # шесть, садится один; за ночь 25->26.09 догон спрашивал про
            # принятую (4UV1iMzo...), узел отвечал "нет такой", и 61 пара из 63
            # осталась без количества. Числа ниже -- с живой покупки
            # 25.09 18:09:41Z: кошелёк отдал 0.053493440 при входе 0.05.
            КОШ = OS.кошелёк_полосы()
            st_п.write_intent(client_order_id="lane4", mint="MINTP", source_sig="SL4",
                             source_slot=11, sol_in=0.05, pool=None, program=None,
                             taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                             sell_after_s=28.8, lane=ST.МЕТКА_ПОЛОСЫ)
            st_п.update_position("lane4", state="bought",
                                lane_signature="ПРИНЯТАЯ_НО_НЕ_СЕЛА",
                                lane_pool_candidates=["ПРИНЯТАЯ_НО_НЕ_СЕЛА",
                                                      "СЕВШАЯ"],
                                ts_sent=time.time())
            HeliusСчётный.tx_ответ = None
            HeliusСчётный.севшая_ответ = {"signature": "СЕВШАЯ", "slot": 4321,
                                          "err": None, "why_not": None}
            HeliusСчётный.tx_по_подписи = {"СЕВШАЯ": {
                "meta": {"err": None, "fee": 1_005_000,
                         "preBalances": [640_856_911, 0],
                         "postBalances": [587_363_471, 0],
                         "preTokenBalances": [],
                         "postTokenBalances": [
                             {"accountIndex": 3, "owner": КОШ, "mint": "MINTP",
                              "uiTokenAmount": {"amount": "12703485536",
                                                "decimals": 6,
                                                "uiAmount": 12703.485536}}]},
                "transaction": {"message": {"accountKeys": [
                    {"pubkey": КОШ}, {"pubkey": "ЧУЖОЙ"}]}}}}
            догон4 = детектор_пп.догнать_купленное_полосы()
            поз4 = st_п.positions()["lane4"]
            chk("догон нашёл севшую подпись и взял количество из неё",
                догон4["filled"] == 1 and поз4.get("lane_bought_raw") == 12_703_485_536
                and поз4.get("lane_landed_signature") == "СЕВШАЯ"
                and поз4.get("lane_landed_slot") == 4321, (догон4, поз4))
            chk("и записал расход покупки ПО ЦЕПИ: -0.052488440 плюс 0.001005 комиссии",
                abs((поз4.get("lane_buy_native_sol") or 0) + 0.05248844) < 1e-9
                and abs((поз4.get("lane_buy_fee_sol") or 0) - 0.001005) < 1e-9,
                (поз4.get("lane_buy_native_sol"), поз4.get("lane_buy_fee_sol")))
            # И ГЛАВНОЕ -- ИТОГ. Продажа вернула 0.048637848; честный итог
            # -0.004855592, а по полям вышло бы -0.003367152: разница 0.00148844
            # -- плата за счета, которую поля не видят.
            стр4 = st_п.update_position("lane4", state=ST.STATE_CLOSED,
                                        closed_sol_net=0.048637848,
                                        lane_tips_total_sol=0.001,
                                        lane_priority_lamports=1_000_000)
            chk("итог по цепи -0.004855592, а не -0.003367152 по полям",
                abs((стр4.get("pnl_counted_sol") or 0) + 0.004855592) < 1e-9,
                стр4.get("pnl_counted_sol"))
            HeliusСчётный.tx_по_подписи = {}
            HeliusСчётный.севшая_ответ = {}

            # 7. ПАРА "BLOOM ПРОТИВ НАШЕЙ" -- один раз и только когда есть обе.
            куда_пара: list = []

            class ОповещательПары:
                def послать(self, текст, *, куда="main"):
                    куда_пара.append((куда, текст))
                    return {"ok": True}

                def статус(self):
                    return {"enabled": True}

            детектор_пп.оповещатель = ОповещательПары()
            пара_рано = детектор_пп.сообщить_пару("lane1")
            chk("половина пары не докладывается",
                пара_рано["sent"] is False and not куда_пара
                and "Bloom" in (пара_рано["why_not"] or ""), пара_рано)
            st_п.write_intent(client_order_id="bl1", mint="MINTL", source_sig="SL",
                             source_slot=7, sol_in=0.2, pool=None, program=None,
                             taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                             sell_after_s=28.8)
            st_п.update_position("bl1", state="bought", signatures=["ПОДПИСЬ_BLOOM"],
                                own_tx_seen_ts=т_п + 0.9, own_tx_seen_slot=951,
                                bloom_to_seen_ms=520.0, bloom_ms=33.8)
            # ЕДИНЫЙ НОЛЬ у обеих сторон -- приход сигнала источника. У Bloom и у
            # полосы он ОДИН И ТОТ ЖЕ, поэтому разность кругов от него обязана
            # совпасть с разницей пары по часам узла.
            st_п.update_position("bl1", signal_recv_ts=т_п)
            st_п.update_position("lane1", signal_recv_ts=т_п)
            пара = детектор_пп.сообщить_пару("lane1")
            chk("пара доложена строкой в основной чат с разницей в мс",
                пара["sent"] and len(куда_пара) == 1
                and куда_пара[0][0] == "main"
                and abs(пара["delta_ms"] - 550.0) < 20.0
                and "мы раньше" in куда_пара[0][1], (пара, куда_пара))
            пара2 = детектор_пп.сообщить_пару("lane1")
            chk("вторая строка о той же паре не уходит",
                пара2["sent"] is False and len(куда_пара) == 1, пара2)
            chk("в строке пары есть место в блоке, отставание и цена входа обеих",
                all(с in куда_пара[0][1] for с in
                    ("место", "отставание от источника", "цена входа",
                     "от прихода сигнала", "от своего решения", "минт")),
                куда_пара[0][1])
            строки_пары = [json.loads(с) for с in
                            st_п.decisions_path.read_text(encoding="utf-8")
                            .strip().split("\n") if '"lane_pair"' in с]
            chk("круги от единого нуля записаны и согласованы с разницей пары",
                строки_пары
                and abs((строки_пары[-1]["bloom_from_signal_ms"]
                         - строки_пары[-1]["lane_from_signal_ms"])
                        - строки_пары[-1]["lane_pair_delta_ms"]) < 0.01,
                строки_пары[-1] if строки_пары else None)
            chk("пустое поле в строке -- прочерк, а не ноль",
                "место ещё не добрано" in куда_пара[0][1], куда_пара[0][1])
            # Цена входа у полосы считается из её купленного количества, а
            # у покупки Bloom -- из своего: перепутать их значит сравнить
            # 0.01 SOL с 0.2 SOL и решить, что полоса покупает вдесятеро дешевле.
            chk("цена входа полосы взята из её количества (0.01 SOL / 8 880 000)",
                "0.01 SOL за 8880000" in куда_пара[0][1], куда_пара[0][1])

            # 7б. ОДИНОЧНОГО КОНТРОЛЯ БОЛЬШЕ НЕТ. Решение владельца 25.09:
            # "убрать отдельный основной контроль через Helius -- он уже есть в
            # веере, это дубль". Проверяем, что мёртвого пути не осталось: ни
            # метода, ни ветки узнавания подписи "основного" контроля.
            chk("метода одиночного контроля в детекторе нет",
                not hasattr(детектор_пп, "отметить_контроль_в_потоке")
                and not hasattr(детектор_пп, "догнать_место_контроля"), "")
            рабочая_д = Path(__file__).read_text(encoding="utf-8").split("def self_test")[0]
            chk("в рабочей части нет чтения control_signature",
                'get("control_signature")' not in рабочая_д, "")

            # 7в. ВЕЕР КОНТРОЛЕЙ ПО СЕРВИСАМ: у каждого отправителя своя
            # подпись, свой круг и своя строка в своде. Это ответ на вопрос
            # владельца "кто довозит быстрее" -- по цепи.
            st_п.update_position("lane1", control_services=[
                {"sender": "jito", "signature": "ПОДПИСЬ_JITO",
                 "ts_sent": т_п + 0.06, "sent": True},
                {"sender": "blockrazor", "signature": "ПОДПИСЬ_BR",
                 "ts_sent": т_п + 0.06, "sent": True}])
            зj = детектор_пп.отметить_нашу_транзакцию(
                "ПОДПИСЬ_JITO", 949, {"meta": {"err": None}}, т_п + 0.16)
            зb = детектор_пп.отметить_нашу_транзакцию(
                "ПОДПИСЬ_BR", 951, {"meta": {"err": None}}, т_п + 0.26)
            поз_в = st_п.positions()["lane1"]
            видно_в = поз_в.get("control_senders_seen") or {}
            chk("контроль сервиса опознан как контроль СВОЕГО сервиса",
                зj.get("stage") == "lane_control_sender_seen"
                and зj.get("sender") == "jito"
                and зb.get("sender") == "blockrazor", (зj, зb))
            chk("круг каждого сервиса посчитан от ЕГО отправки",
                abs(видно_в["jito"]["send_to_seen_ms"] - 100.0) < 5.0
                and abs(видно_в["blockrazor"]["send_to_seen_ms"] - 200.0) < 5.0,
                видно_в)
            chk("смещение слота у каждого сервиса своё",
                видно_в["jito"]["slot_offset"] == 949 - 7
                and видно_в["blockrazor"]["slot_offset"] == 951 - 7, видно_в)
            chk("веер НЕ затирает замер покупки полосы",
                abs(поз_в.get("lane_send_to_seen_ms") - 300.0) < 5.0,
                поз_в.get("lane_send_to_seen_ms"))
            свод_в = детектор_пп.свод_контролей_сервисов()
            chk("свод по сервисам говорит, кто довёз быстрее",
                свод_в["jito"]["deliver_ms_median"] < свод_в["blockrazor"]["deliver_ms_median"]
                and свод_в["jito"]["n"] == 1, свод_в)
            пж_в = детектор_пп.признак_жизни().get("own_send", {}).get("control", {})
            chk("в признаке жизни виден веер и суточный расход контроля",
                пж_в.get("senders_seen") == 2
                and "jito" in (пж_в.get("by_sender") or {})
                and (пж_в.get("limits") or {}).get("per_sender_sol") == 0.001
                and (пж_в.get("limits") or {}).get("spend_sol_per_day") == 0.1,
                пж_в)

            # СТРОКА ПАРЫ ПО СЕРВИСАМ (решение владельца 25.09: в строку --
            # кто довёз, слот и место у Bloom и у полосы). Пока места в блоке
            # не добраны, строка не уходит: прочерк вместо места читался бы как
            # "сел первым".
            куда_пара.clear()
            chk("строка по сервисам НЕ уходит, пока места в блоке добираются",
                детектор_пп.сообщить_контроль("lane1")["sent"] is False,
                куда_пара)
            видно_м = dict(поз_в.get("control_senders_seen") or {})
            видно_м["jito"] = {**видно_м["jito"], "block_index": 12,
                                "block_total": 1391}
            видно_м["blockrazor"] = {**видно_м["blockrazor"], "block_index": 88,
                                      "block_total": 1391}
            st_п.update_position("lane1", control_senders_seen=видно_м,
                                  block_index=93, block_total=1391,
                                  lane_pool_winner="jito",
                                  queue_pool_tx_slot=7, queue_pool_tx_next_slot=4,
                                  queue_our_micro_per_cu=2500,
                                  queue_micro_per_cu_median=1800,
                                  queue_micro_per_cu_p90=9000)
            ск = детектор_пп.сообщить_контроль("lane1")
            chk("строка по сервисам ушла в основной чат",
                ск["sent"] and len(куда_пара) == 1 and куда_пара[0][0] == "main"
                and "контроли по сервисам" in куда_пара[0][1], (ск, куда_пара))
            chk("в строке есть КТО ДОВЁЗ покупку полосы",
                "довёз jito" in куда_пара[0][1], куда_пара[0][1])
            chk("в строке есть круг, слот и место КАЖДОГО сервиса",
                all(с in куда_пара[0][1] for с in ("jito:", "blockrazor:",
                                                    "S+", "место 12/1391",
                                                    "место 88/1391")),
                куда_пара[0][1])
            chk("первым в строке идёт тот, кто довёз быстрее",
                куда_пара[0][1].index("jito:") < куда_пара[0][1].index("blockrazor:")
                and "быстрее всех довёз jito" in куда_пара[0][1], куда_пара[0][1])
            chk("место и слот покупки полосы в строке есть",
                "покупка полосы:" in куда_пара[0][1]
                and "место 93/1391" in куда_пара[0][1], куда_пара[0][1])
            chk("толпа в пуле и приоритеты в строке есть",
                all(с in куда_пара[0][1] for с in ("в пуле за наш слот",
                                                    "мкл/CU", "медиана", "90-й")),
                куда_пара[0][1])
            chk("самый быстрый сервис записан в позицию",
                (st_п.positions()["lane1"] or {}).get("control_fastest_sender")
                == "jito", st_п.positions()["lane1"].get("control_fastest_sender"))
            chk("вторая строка о той же паре не уходит",
                детектор_пп.сообщить_контроль("lane1")["sent"] is False
                and len(куда_пара) == 1, "")

            # ПАРАЛЛЕЛЬНАЯ ЗАПИСЬ НЕ ТЕРЯЕТ ЗАМЕР. Словарь контролей пишут две
            # ветки целиком (узнавание подписи и догон места), и без замка одна
            # затирала бы другую: деньги на контроль потрачены, а замера нет.
            видно_до = dict(поз_в.get("control_senders_seen") or {})
            st_п.update_position("lane1", control_services=[
                *(поз_в.get("control_services") or []),
                {"sender": "astralane", "signature": "ПОДПИСЬ_AST",
                 "ts_sent": т_п + 0.06, "sent": True},
                {"sender": "nozomi", "signature": "ПОДПИСЬ_NOZ",
                 "ts_sent": т_п + 0.06, "sent": True}])
            потоки_к = [threading.Thread(
                target=детектор_пп.отметить_нашу_транзакцию,
                args=(подпись_к, слот_к, {"meta": {"err": None}}, т_п + 0.30))
                for подпись_к, слот_к in (("ПОДПИСЬ_AST", 952),
                                           ("ПОДПИСЬ_NOZ", 953))]
            for п_к in потоки_к:
                п_к.start()
            for п_к in потоки_к:
                п_к.join(timeout=10)
            видно_после = (st_п.positions()["lane1"] or {}).get("control_senders_seen") or {}
            chk("параллельные записи контролей не теряют друг друга",
                set(видно_после) == set(видно_до) | {"astralane", "nozomi"},
                sorted(видно_после))


            # СРОК ОЖИДАНИЯ: строка пары уходит даже тогда, когда контроль
            # НЕ ДОЕХАЛ ВОВСЕ. Раньше ждали только по попыткам, а попытки
            # растут лишь у тех контролей, что уже видно: один не доехавший
            # означал молчание навсегда. Деньги на веер при этом потрачены.
            st_п.write_intent(client_order_id="lane2", mint="MИНТ2",
                              source_sig="ИСТ2", source_slot=900,
                              sol_in=0.01, pool=None, program=None, taxed=None,
                              tax_bps=None, mode=ST.MODE_LIVE, sell_after_s=28.8,
                              lane=ST.МЕТКА_ПОЛОСЫ)
            st_п.update_position(
                "lane2", lane=ST.МЕТКА_ПОЛОСЫ, lane_signature="ПОДПИСЬ_ПОКУПКИ2",
                own_tx_seen_ts=time.time() - 5.0, own_tx_seen_slot=901,
                block_index=5, block_total=1000, lane_send_to_seen_ms=300.0,
                block_tries=1,
                control_services=[
                    {"sender": "jito", "signature": "ПОДПИСЬ_J2", "sent": True,
                     "ts_sent": time.time() - 5.1},
                    {"sender": "nozomi", "signature": "ПОДПИСЬ_N2", "sent": True,
                     "ts_sent": time.time() - 5.1}])
            куда_пара.clear()
            chk("пока срок не вышел -- строки нет (ни один контроль не виден)",
                детектор_пп.сообщить_контроль("lane2")["sent"] is False
                and куда_пара == [], куда_пара)
            # Досказыватель обязан такую пару ВИДЕТЬ: иначе доклад по ней не
            # состоится никогда, сколько бы ни прошло времени.
            ждущие = детектор_пп.досказать_контроли()
            chk("пара без ни одного виденного контроля в досказывателе есть",
                ждущие.get("looked", 0) >= 1, ждущие)
            # Переводим часы: покупку увидели давно.
            st_п.update_position("lane2",
                                 own_tx_seen_ts=time.time() - СРОК_ОЖИДАНИЯ_ПАРЫ_S - 1)
            куда_пара.clear()
            ск2 = детектор_пп.сообщить_контроль("lane2")
            chk("по истечении срока строка пары УХОДИТ",
                ск2["sent"] is True and len(куда_пара) == 1, (ск2, куда_пара))
            chk("и в ней сказано, кто не доехал",
                "не доехал" in куда_пара[0][1]
                and "jito" in куда_пара[0][1] and "nozomi" in куда_пара[0][1],
                куда_пара[0][1])
            chk("покупка полосы в такой строке всё равно с числами",
                "покупка полосы:" in куда_пара[0][1]
                and "место 5/1000" in куда_пара[0][1], куда_пара[0][1])
            chk("и второй раз та же пара не докладывается",
                детектор_пп.сообщить_контроль("lane2")["sent"] is False,
                "")

            # 8. ПРИЗНАК ЖИЗНИ показывает полосу числами, а не "включена".
            жив = детектор_пп.признак_жизни()
            chk("в признаке жизни у полосы стадии, пределы и возраст хеша",
                жив["own_send"]["enabled"] is True
                and жив["own_send"]["paths"] >= 1
                and жив["own_send"]["limits"]["open"] == 1
                and жив["own_send"]["blockhash_age_s"] is not None,
                жив.get("own_send"))
        finally:
            if было_имя_OS:
                globals()["OS"] = было_OS
            else:
                globals().pop("OS", None)
        chk("после проверок полосы модуль на месте, как был",
            "OS" in globals() and globals()["OS"] is настоящая_полоса,
            ("OS" in globals(), globals().get("OS") is настоящая_полоса))


        # ---- ДОГОН chain_ok ОТЛОЖЕННЫМ getTransaction ----
        # Замерная подписка идёт с "failed": False, поэтому упавшая наша
        # транзакция по ней не придёт НИКОГДА, а уведомление часто приходит
        # без meta. Догон закрывает и то, и другое -- вне горячего пути.
        class HeliusДогон:
            def __init__(self, ответы):
                self.ответы = ответы
                self.спрошено = []

            def транзакция(self, подпись, **kw):
                self.спрошено.append(подпись)
                return self.ответы.get(подпись)

        st.write_intent(client_order_id="dg1", mint="M1", source_sig="S1",
                         source_slot=1, sol_in=0.2, pool=None, program=None,
                         taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                         sell_after_s=28.8)
        st.update_position("dg1", signatures=["ПОДПИСЬ_СЕЛА"])
        st.write_intent(client_order_id="dg2", mint="M2", source_sig="S2",
                         source_slot=2, sol_in=0.2, pool=None, program=None,
                         taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                         sell_after_s=28.8)
        st.update_position("dg2", signatures=["ПОДПИСЬ_УПАЛА"])
        st.write_intent(client_order_id="dg3", mint="M3", source_sig="S3",
                         source_slot=3, sol_in=0.2, pool=None, program=None,
                         taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                         sell_after_s=28.8)
        st.update_position("dg3", signatures=["ПОДПИСЬ_МОЛЧИТ"])
        st.write_intent(client_order_id="dg4", mint="M4", source_sig="S4",
                         source_slot=4, sol_in=0.2, pool=None, program=None,
                         taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                         sell_after_s=28.8)
        st.update_position("dg4", signatures=["ПОДПИСЬ_УЖЕ"], chain_ok=True)
        h_дг = HeliusДогон({
            "ПОДПИСЬ_СЕЛА": {"slot": 10, "meta": {"err": None}},
            "ПОДПИСЬ_УПАЛА": {"slot": 11, "meta": {"err": {"Custom": 6001}}},
            "ПОДПИСЬ_МОЛЧИТ": None})
        д_дг = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                         курс=КурсSOL(), режим="dry", helius=h_дг)
        и_дг = д_дг.догнать_chain_ok()
        поз_дг = st.positions()
        chk("догон заполнил вердикт севшей покупки",
            поз_дг["dg1"].get("chain_ok") is True, поз_дг["dg1"].get("chain_ok"))
        chk("догон заполнил вердикт УПАВШЕЙ -- то, чего подписка дать не может",
            поз_дг["dg2"].get("chain_ok") is False
            and поз_дг["dg2"].get("chain_err") == {"Custom": 6001},
            поз_дг["dg2"].get("chain_ok"))
        chk("узел молчит -- поле остаётся пустым, посадка не выдумывается",
            поз_дг["dg3"].get("chain_ok") is None, поз_дг["dg3"].get("chain_ok"))
        chk("позицию с УЖЕ известным вердиктом догон не трогает и не спрашивает",
            "ПОДПИСЬ_УЖЕ" not in h_дг.спрошено, h_дг.спрошено)
        chk("итог догона назван числами",
            и_дг["looked"] == 3 and и_дг["filled"] == 2
            and и_дг["still_unknown"] == 1, и_дг)
        chk("оба имени поля теперь согласованы",
            поз_дг["dg2"].get("own_tx_seen_chain_ok") is False,
            поз_дг["dg2"].get("own_tx_seen_chain_ok"))
        chk("догон посчитан в признаке жизни",
            д_дг.chain_ok_догнано == 2, д_дг.chain_ok_догнано)
        предел = д_дг.догнать_chain_ok(предел=1)
        chk("за круг берётся не больше предела -- узел не занимаем",
            предел["looked"] <= 1, предел)

        # ---- ЗАПАСНОЙ ПУТЬ ОГРАНИЧЕН ПО ЧАСАМ, А НЕ ПО ЗДОРОВЬЮ ----
        # 24.09 между 18:48 и 19:12 служба ушла на запасной logsSubscribe и
        # просидела на нём три часа: возврат стоял в ветке ОТКАЗА, а запасное
        # соединение держалось. Цена: кэш шаблонов получил НОЛЬ пригодных
        # уведомлений против 547 216 пустых, каждый сигнал стал стоить
        # getTransaction на commitment confirmed, и все четыре вечерние
        # покупки сели в S+2 вместо S+0/S+1.
        chk("на основном пути возвращаться некуда",
            пора_вернуться_на_основной(True, None, сейчас=1000.0) is False)
        chk("на основном пути отметка запасного не мешает",
            пора_вернуться_на_основной(True, 100.0, сейчас=1_000_000.0) is False)
        chk("внутри окна на запасном сидим",
            пора_вернуться_на_основной(False, 1000.0, сейчас=1050.0,
                                        окно=120.0) is False)
        chk("окно вышло -- возвращаемся, даже если запасное живо",
            пора_вернуться_на_основной(False, 1000.0, сейчас=1121.0,
                                        окно=120.0) is True)
        chk("ровно на границе окна уже возвращаемся",
            пора_вернуться_на_основной(False, 1000.0, сейчас=1120.0,
                                        окно=120.0) is True)
        chk("без отметки времени перехода решение не принимается",
            пора_вернуться_на_основной(False, None, сейчас=1_000_000.0) is False)
        д_зп = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                         курс=КурсSOL(), режим="dry", helius=HeliusМолчит())
        chk("свежий детектор считает себя на основном пути",
            д_зп.запасной_путь_с is None, д_зп.запасной_путь_с)

        # ---- ТРЕВОГА О ЗАПАСНОМ ПУТИ (слово владельца 25.09) ----
        # Тревога идёт НЕ при уходе -- уход бывает на секунды и лечится сам, --
        # а когда возврат не случился за две минуты, и повторяется, пока
        # детектор работает вполсилы.
        chk("на основном пути тревоги нет",
            тревога_запасного(запасной_с=None, сейчас=1000.0, последняя=0.0,
                               порог=120.0, повтор=120.0)[0] is None)
        chk("первые две минуты на запасном тревоги нет",
            тревога_запасного(запасной_с=1000.0, сейчас=1100.0, последняя=0.0,
                               порог=120.0, повтор=120.0)[0] is None)
        сколько, когда = тревога_запасного(запасной_с=1000.0, сейчас=1121.0,
                                            последняя=0.0, порог=120.0, повтор=120.0)
        chk("после двух минут тревога с числом секунд",
            сколько == 121.0 and когда == 1121.0, (сколько, когда))
        chk("тревога не повторяется чаще повтора",
            тревога_запасного(запасной_с=1000.0, сейчас=1200.0, последняя=1121.0,
                               порог=120.0, повтор=120.0)[0] is None)
        chk("через повтор тревога снова идёт -- пока не вернулись",
            тревога_запасного(запасной_с=1000.0, сейчас=1250.0, последняя=1121.0,
                               порог=120.0, повтор=120.0)[0] == 250.0)
        сказанное: list = []

        class ОповещательЗаписной:
            def послать(self, текст, *, куда="main"):
                сказанное.append(текст)
                адресаты.append(куда)
                return {"ok": True}

        адресаты: list = []

        д_зп.оповещатель = ОповещательЗаписной()
        д_зп.запасной_путь_с = 1000.0
        # ОДНА СТРОКА НА ЭПИЗОД (слово владельца 25.09). Раньше здесь была
        # строка на каждый переход и повтор каждые две минуты -- это и был
        # шум, из-за которого владелец перестал читать чат.
        chk("на пульсе уходит ОДНА строка про долгий запасной",
            д_зп.проверить_запасной_путь(сейчас=1200.0) == 200.0
            and len(сказанное) == 1
            and "дольше 30" in сказанное[0], сказанное)
        chk("строка тревоги начинается с предупреждения",
            сказанное and сказанное[0].startswith("\u26a0"), сказанное[:1])
        chk("повторная проверка про тот же эпизод молчит",
            len(сказанное) == 1
            and (д_зп.проверить_запасной_путь(сейчас=1205.0) is None
                 or len(сказанное) == 1), сказанное)
        # ТИШИНА ПРО КОРОТКИЕ ПЕРЕХОДЫ (слово владельца 25.09). Длинный
        # возврат -- строка; короткий -- только счётчик.
        д_зп.тревога_возврата_на_основной(210.0)
        было_строк = len(сказанное)
        chk("долгий возврат на основной путь -- строка с длительностью",
            len(сказанное) == было_строк and "вернулся на основной" in сказанное[-1]
            and "210" in сказанное[-1], list(сказанное))
        было_переходов = д_зп.запасных_переходов
        д_зп.тревога_ухода_на_запасной()
        chk("уход на запасной СЧИТАЕТСЯ и молчит",
            len(сказанное) == 2
            and д_зп.запасных_переходов == было_переходов + 1, сказанное)
        д_зп.тревога_возврата_на_основной(5.0)
        chk("короткий переход не сообщается, а попадает в счётчик коротких",
            len(сказанное) == 2 and д_зп.запасных_коротких == 1
            and abs(д_зп.запасное_время_с - 215.0) < 0.01,
            (сказанное, д_зп.запасных_коротких, д_зп.запасное_время_с))
        # ОДНО сообщение про долгий уход -- с пульса, и ровно одно.
        д_зп.запасной_путь_с = 1000.0
        д_зп._запасной_доложен = False
        д_зп.проверить_запасной_путь(сейчас=1000.0 + 31.0)
        chk("уход дольше порога -- одна строка про запасной",
            len(сказанное) == 3 and "дольше 30" in сказанное[2], сказанное)
        д_зп.проверить_запасной_путь(сейчас=1000.0 + 60.0)
        chk("и второй раз про тот же уход не пишем",
            len(сказанное) == 3, сказанное)
        д_зп.запасной_путь_с = None
        chk("вернулись -- тревог больше нет",
            д_зп.проверить_запасной_путь(сейчас=9999.0) is None and len(сказанное) == 3)
        # ПРИЧИНЫ ОБРЫВОВ -- по группам, и таблица собирается без хоста.
        д_зп.учесть_обрыв(причина_обрыва(RuntimeError(
            "transactionSubscribe: все подписки отклонены: ...")), "детали")
        д_зп.учесть_обрыв(причина_обрыва(TimeoutError("keepalive ping timeout")))
        д_зп.учесть_обрыв(причина_обрыва(TimeoutError("keepalive ping timeout")))
        chk("обрывы сгруппированы по причинам с числами",
            д_зп.обрывов_по_причинам.get("подписка отклонена сервером") == 1
            and д_зп.обрывов_по_причинам.get(
                "таймаут пинга (наша сторона ждала ответа)") == 2,
            д_зп.обрывов_по_причинам)
        chk("незнакомая ошибка так и называется незнакомой",
            причина_обрыва(ValueError("что-то новое")).startswith("незнакомая"),
            причина_обрыва(ValueError("что-то новое")))
        # ЧАСОВАЯ СТРОКА: в журнальный чат, с долей; тревога -- если доля велика.
        сказанное.clear()
        адресаты.clear()
        д_зп.запасной_час_с = 0.0
        д_зп.запасной_час_переходов = 4
        д_зп.запасной_час_секунд = 600.0
        часовая = д_зп.часовая_строка_запасного(сейчас=3600.0)
        chk("часовая строка считает долю и уходит в ЖУРНАЛЬНЫЙ чат",
            часовая and часовая["switches"] == 4 and часовая["share"] == 0.1667
            and "log" in адресаты, (часовая, адресаты))
        chk("и при доле выше порога отдельная тревога в основной чат",
            "main" in адресаты and any("выше порога" in с_ for с_ in сказанное),
            (адресаты, сказанное))
        chk("после часовой строки окно обнулено",
            д_зп.запасной_час_переходов == 0 and д_зп.запасной_час_секунд == 0.0,
            (д_зп.запасной_час_переходов, д_зп.запасной_час_секунд))
        chk("до срока часовой строки её нет",
            д_зп.часовая_строка_запасного(сейчас=3600.0 + 10.0) is None, "")
        # АДРЕСАТЫ РАЗНЫЕ И НЕ ПЕРЕПУТАНЫ: тревоги -- в основной чат (их
        # читают), часовая строка про запасной путь -- в журнальный (её
        # читают, когда захотят).
        chk("тревоги идут в ОСНОВНОЙ чат, а часовая строка -- в журнальный",
            "main" in адресаты and "log" in адресаты
            and all(а in ("main", "log") for а in адресаты), адресаты)

        # ---- ДВА ЧАТА: построчные решения в журнал, сводка в основной ----
        # Слово владельца 25.09. Цена ошибки не в деньгах, а в том, что
        # владелец перестанет читать основной чат: сто решений в час.
        д_зп.запасной_путь_с = None
        сказанное.clear()
        адресаты.clear()
        д_зп._решений_за_окно = 0
        д_зп._покупок_за_окно = 0
        д_зп._коды_за_окно = {}
        д_зп._сводка_ts = 1000.0
        chk("до срока сводки её нет",
            д_зп.сводка_за_окно(сейчас=1100.0, окно=3600.0) is None and not сказанное)
        д_зп._решений_за_окно = 97
        д_зп._покупок_за_окно = 2
        д_зп._коды_за_окно = {"NOT_A_BUY": 70, "SKIP_DUP": 25}
        данные = д_зп.сводка_за_окно(сейчас=1000.0 + 3600.0, окно=3600.0)
        chk("сводка считает по ОКНУ и уходит в основной чат",
            данные == {"window_min": 60.0, "signals": 97, "buys": 2,
                        "by_code": {"NOT_A_BUY": 70, "SKIP_DUP": 25}}
            and адресаты == ["main"] and "сигналов 97" in сказанное[0], данные)
        chk("после сводки счётчики окна обнулены",
            д_зп._решений_за_окно == 0 and д_зп._покупок_за_окно == 0
            and д_зп._коды_за_окно == {})
        chk("второй раз подряд сводка молчит",
            д_зп.сводка_за_окно(сейчас=1000.0 + 3700.0, окно=3600.0) is None
            and len(сказанное) == 1)

        д_зп.оповещатель = None
        д_зп.запасной_путь_с = 1.0
        chk("без оповещателя проверка не падает",
            д_зп.проверить_запасной_путь(сейчас=1_000_000.0) is not None)

        # ---- СЕРИЯ УСПЕШНЫХ ТЕНЕЙ ПО ТИПУ ПУЛА ----
        # Условие владельца для полосы своей отправки: тень по этому типу
        # пула прошла симуляцию в пяти последних случаях ПОДРЯД. Серия
        # рвётся первой же неудачей по тому же типу -- иначе "пять из
        # последних двадцати" выдавалось бы за "пять подряд".
        class ТеньПоТипу:
            прошла = True
            тип = "ПУЛ_А"

            @staticmethod
            def shadow_build(*a, **kw):
                return {"ok": True, "pool_program": ТеньПоТипу.тип,
                         "sim_verdict": ("would_pass" if ТеньПоТипу.прошла
                                          else "slippage")}

        подмена_т = ПодменаТени(ТеньПоТипу)
        try:
            class HeliusДляСерии(HeliusМолчит):
                @staticmethod
                def call(*a, **kw):
                    return None

            д = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                          курс=КурсSOL(), режим="dry", helius=HeliusДляСерии())
            д.тень_включена = True
            for _ in range(3):
                д._тень_внутри({"signature": "S", "mint": "M"}, {})
            chk("три успешные тени подряд по одному типу -- серия три",
                д.тени_подряд.get("ПУЛ_А") == 3, д.тени_подряд)
            ТеньПоТипу.прошла = False
            д._тень_внутри({"signature": "S", "mint": "M"}, {})
            chk("одна неудача обнуляет серию этого типа",
                д.тени_подряд.get("ПУЛ_А") == 0, д.тени_подряд)
            ТеньПоТипу.прошла = True
            ТеньПоТипу.тип = "ПУЛ_Б"
            д._тень_внутри({"signature": "S", "mint": "M"}, {})
            chk("серия другого типа пула считается отдельно",
                д.тени_подряд.get("ПУЛ_Б") == 1
                and д.тени_подряд.get("ПУЛ_А") == 0, д.тени_подряд)
            chk("серии лежат отдельным словарём и уходят в признак жизни",
                isinstance(д.тени_подряд, dict)
                and set(д.тени_подряд) == {"ПУЛ_А", "ПУЛ_Б"}, д.тени_подряд)
        finally:
            подмена_т.вернуть()

        # ---- КЭШ ШАБЛОНОВ ПЕРВОГО ШАГА (двухшаговая тень, v2 Code-2) ----
        # Хранилища четырёх котировочных пулов подписаны рядом с источниками.
        # Главное, что проверяется: они НЕ сигнал -- по ним не считается
        # обработанный сигнал, не принимается решение и не зовётся узел.
        class HeliusСчётчик(HeliusМолчит):
            def __init__(self):
                self.вызовов = 0

            def call(self, метод, параметры):
                self.вызовов += 1
                return None

            def транзакция(self, подпись, **kw):
                self.вызовов += 1
                return None

        h_к = HeliusСчётчик()
        детектор_к = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                               курс=КурсSOL(), режим="dry", helius=h_к)
        # Модуль тени есть не на всякой машине (раннеру, например, не нужен
        # solders). Создание кэша проверяется только там, где модуль есть, а
        # вот РАЗВОДКА уведомлений -- всегда, на подставном кэше: она наша и
        # от чужого модуля зависеть не должна.
        # ЗАМЕР ДВУХШАГОВОЙ ТЕНИ СНЯТ ПО УМОЛЧАНИЮ (решение владельца 25.09
        # вечером: 2 сигнала из 41 977 за сутки). Это и проверяется первым: по
        # умолчанию кэша ног нет, бюджет 0, а причина названа словами.
        chk("по умолчанию замер двухшаговой тени снят, и сказано почему",
            детектор_к.кэш_ног is None and детектор_к.ног_бюджет == 0
            and "снят словом владельца" in детектор_к.кэш_ног_почему,
            (детектор_к.кэш_ног, детектор_к.ног_бюджет,
             детектор_к.кэш_ног_почему))
        # А САМ МЕХАНИЗМ ЖИВ и включается одной переменной: проверяем его при
        # BLOOM_SHADOW_LEGS_OFF=0, чтобы снятие замера не стало потерей кода.
        было_снят = os.environ.get("BLOOM_SHADOW_LEGS_OFF")
        os.environ["BLOOM_SHADOW_LEGS_OFF"] = "0"
        try:
            детектор_к = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                                   курс=КурсSOL(), режим="dry", helius=h_к)
        finally:
            if было_снят is None:
                os.environ.pop("BLOOM_SHADOW_LEGS_OFF", None)
            else:
                os.environ["BLOOM_SHADOW_LEGS_OFF"] = было_снят
        if SB is not None:
            chk("кэш шаблонов первого шага поднялся при включённом замере",
                детектор_к.кэш_ног is not None, детектор_к.кэш_ног_почему)
            chk("в кэше ровно четыре котировки, как назвал владелец",
                детектор_к.кэш_ног is not None
                and len(детектор_к.кэш_ног.pools) == 4
                and len(детектор_к.кэш_ног_адреса) == 4,
                (len(детектор_к.кэш_ног.pools) if детектор_к.кэш_ног else None,
                 len(детектор_к.кэш_ног_адреса)))
        else:
            chk("модуля тени нет -- кэш не создан, и это сказано словами",
                детектор_к.кэш_ног is None and детектор_к.кэш_ног_почему,
                детектор_к.кэш_ног_почему)

        class КэшПодставной:
            """Считает, что ему скормили, и ничего не разбирает."""

            def __init__(self):
                self.pools = {"Q": {"q_vault": "QVAULT"}}
                self.entries = {"Q": {"checked_at": time.time(),
                                       "tpl": {"flipped": False}, "sig": "SIGQ1"}}
                self.luts = {"LUT1": ["A"]}
                self.pending_luts: set = set()
                self.ingest_stats = {"новый шаблон": 1}
                self.скормлено: list = []

            def ingest(self, tx):
                self.скормлено.append(tx)
                return {"Q": "новый шаблон"}

        детектор_к.кэш_ног = КэшПодставной()
        детектор_к.кэш_ног_адреса = {"QVAULT": "Q"}
        адреса_к = адреса_подписки(детектор_к)
        chk("хранилища котировок стоят в списке подписки рядом с источниками",
            all(а in адреса_к for а in детектор_к.кэш_ног_адреса)
            and ST.EXECUTOR_WALLET in адреса_к and "SRC" in адреса_к,
            len(адреса_к))
        хранилище = sorted(детектор_к.кэш_ног_адреса)[0]
        было_обработано = детектор_к.обработано
        детектор_к.обработать("SIGQ1", 5, хранилище, "transactionSubscribe",
                               tx_пул(владельцы={"POOLQ": ["КУПЛЕН", WSOL]},
                                       счета=("POOLQ",)), time.time())
        chk("сделка котировочного пула сигналом не становится",
            детектор_к.обработано == было_обработано
            and детектор_к.к_покупке == 0, (детектор_к.обработано,
                                             детектор_к.к_покупке))
        chk("она скормлена кэшу и ни одного вызова узла не стоила",
            детектор_к.ног_скормлено == 1 and h_к.вызовов == 0,
            (детектор_к.ног_скормлено, h_к.вызовов))
        chk("первое событие кэша легло в журнал на сверку с ingest_stats",
            len(детектор_к.ног_первые) == 1
            and детектор_к.ног_первые[0]["stage"] == "leg_cache"
            and детектор_к.ног_первые[0]["quote"] == "Q",
            детектор_к.ног_первые[:1])
        скормлено = детектор_к.кэш_ног.скормлено[0]
        chk("кэшу уходит уведомление целиком: подпись, слот и транзакция",
            скормлено.get("signature") == "SIGQ1" and скормлено.get("slot") == 5
            and isinstance(скормлено.get("transaction"), dict)
            and "meta" in скормлено["transaction"], sorted(скормлено))
        детектор_к.обработать("SIGQ2", 6, хранилище, "transactionSubscribe",
                               None, time.time())
        chk("уведомление без транзакции считается, но узел ради кэша не зовём",
            детектор_к.ног_без_транзакции == 1 and h_к.вызовов == 0,
            (детектор_к.ног_без_транзакции, h_к.вызовов))
        # ДОГРУЗКА ТАБЛИЦ АДРЕСОВ. ingest копит их в очередь и сам не грузит;
        # без догрузки очередь достаётся горячему пути.
        class КэшСОчередью(КэшПодставной):
            def __init__(self):
                super().__init__()
                self.pending_luts = {"LUT2", "LUT3"}
                self.грели = 0

            def warm_luts(self):
                self.грели += 1
                n = len(self.pending_luts)
                self.pending_luts = set()
                return n

        было_кэш = детектор_к.кэш_ног
        детектор_к.кэш_ног = КэшСОчередью()
        детектор_к.догрузить_таблицы_ног()
        chk("очередь таблиц догружается вне горячего пути",
            детектор_к.ног_догружено == 2
            and детектор_к.кэш_ног.грели == 1, детектор_к.ног_догружено)
        детектор_к.ног_отключён = True
        детектор_к.догрузить_таблицы_ног()
        chk("выключенный замер таблицы не грузит",
            детектор_к.кэш_ног.грели == 1, детектор_к.кэш_ног.грели)
        детектор_к.ног_отключён = False

        class КэшПадаетНаГрелке(КэшПодставной):
            def warm_luts(self):
                raise RuntimeError("узел молчит")

        детектор_к.кэш_ног = КэшПадаетНаГрелке()
        детектор_к.догрузить_таблицы_ног()
        chk("падение догрузки не роняет службу и названо словами",
            "RuntimeError" in детектор_к.ног_догрузка_почему,
            детектор_к.ног_догрузка_почему)
        детектор_к.кэш_ног = было_кэш

        ж_к = детектор_к.признак_кэша_ног()
        chk("признак жизни показывает кэш словами и числами",
            ж_к.get("enabled") and ж_к.get("pools") == 1
            and ж_к.get("fed") == 1 and ж_к.get("no_tx") == 1
            and isinstance(ж_к.get("ingest_stats"), dict)
            and isinstance((ж_к.get("templates") or {}).get("Q", {}).get("age_s"),
                            float), ж_к)

        class КэшПадает:
            pools = {}
            entries: dict = {}
            luts: dict = {}
            pending_luts: set = set()
            ingest_stats: dict = {}

            def ingest(self, tx):
                raise RuntimeError("кэш сломался")

        детектор_к.кэш_ног = КэшПадает()
        детектор_к.обработать("SIGQ3", 7, хранилище, "transactionSubscribe",
                               tx_пул(владельцы={"POOLQ": ["КУПЛЕН", WSOL]},
                                       счета=("POOLQ",)), time.time())
        chk("падение кэша не роняет детектор и записано счётчиком",
            детектор_к.ног_упало == 1, детектор_к.ног_упало)

        # ЦЕНА ЗАМЕРА. Четыре котировочных пула стоят дороже всех двадцати
        # источников вместе, и предохранитель обязан выключать именно замер,
        # а не торговлю.
        детектор_б = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                               курс=КурсSOL(), режим="dry", helius=h_к)
        детектор_б.кэш_ног = КэшПодставной()
        детектор_б.кэш_ног_адреса = {"QVAULT": "Q"}
        детектор_б.ног_бюджет = 4            # 2 порции по 0.1 МиБ
        поколение_до = детектор_б.поколение
        детектор_б.учесть_байты_ног(104858)
        chk("байты замера считаются по тому же тарифу, 2 кредита за 0.1 МиБ",
            детектор_б.ног_кредитов == 2 and not детектор_б.ног_отключён,
            (детектор_б.ног_кредитов, детектор_б.ног_отключён))
        chk("хранилища котировок пока в подписке",
            "QVAULT" in адреса_подписки(детектор_б), адреса_подписки(детектор_б))
        детектор_б.учесть_байты_ног(104858)
        chk("бюджет замера исчерпан -- замер выключен, причина названа",
            детектор_б.ног_отключён and "бюджет" in детектор_б.ног_отключён_почему,
            детектор_б.ног_отключён_почему)
        chk("подписка переподнимется уже без хранилищ котировок",
            "QVAULT" not in адреса_подписки(детектор_б)
            and детектор_б.поколение == поколение_до + 1,
            (адреса_подписки(детектор_б), детектор_б.поколение))
        chk("источники и наш кошелёк в подписке остались",
            "SRC" in адреса_подписки(детектор_б)
            and ST.EXECUTOR_WALLET in адреса_подписки(детектор_б),
            адреса_подписки(детектор_б))
        # КОШЕЛЁК ПОЛОСЫ В ПОДПИСКЕ. С 25.09 полоса покупает своим адресом.
        # Нет его в подписке -- нет в потоке ни её транзакции, ни круга, ни
        # купленного количества: сторож не продаст, и замер выйдет пустым.
        было_кош_п = os.environ.get("OWN_SEND_WALLET")
        os.environ["OWN_SEND_WALLET"] = "КОШЕЛЁК_ПОЛОСЫ_ПОДПИСКА"
        try:
            chk("кошелёк полосы в подписке отдельной строкой",
                "КОШЕЛЁК_ПОЛОСЫ_ПОДПИСКА" in адреса_подписки(детектор_б)
                and ST.EXECUTOR_WALLET in адреса_подписки(детектор_б),
                адреса_подписки(детектор_б))
        finally:
            if было_кош_п is None:
                os.environ.pop("OWN_SEND_WALLET", None)
            else:
                os.environ["OWN_SEND_WALLET"] = было_кош_п
        было_скормлено = детектор_б.ног_скормлено
        детектор_б.обработать("SIGQ9", 9, "QVAULT", "transactionSubscribe",
                               tx_пул(владельцы={"POOLQ": ["КУПЛЕН", WSOL]},
                                       счета=("POOLQ",)), time.time())
        chk("после выключения кэш больше не кормится",
            детектор_б.ног_скормлено == было_скормлено, детектор_б.ног_скормлено)
        детектор_б.учесть_байты_ног(10 * 104858)
        chk("и байты после выключения больше не копятся",
            детектор_б.ног_кредитов == 4, детектор_б.ног_кредитов)
        ж_б = детектор_б.признак_кэша_ног()
        chk("признак жизни показывает цену замера и почему он выключен",
            ж_б.get("credits") == 4 and ж_б.get("dropped") is True
            and ж_б.get("credit_budget") == 4 and ж_б.get("dropped_why"), ж_б)
        # ---- МЕСТО В БЛОКЕ ДОБИРАЕТСЯ САМ ДЕТЕКТОР, ВНЕ ГОРЯЧЕГО ПУТИ ----
        # Владелец 25.09 просит место рядом с bloom_ms и S+N по каждой боевой
        # покупке. В горячем пути это сеть, поэтому догон на пульсе.
        class HeliusБлокПодписи:
            """Узел, который умеет только getBlock уровня signatures."""

            def __init__(self, блоки, падать=False):
                self.блоки = блоки
                self.падать = падать
                self.вызовов_getblock = 0
                # Признак жизни читает эти поля у настоящего узла.
                self.вызовов = 0
                self.по_методам: dict = {}
                self.учёт_пишется = False
                self.учёт_почему = "заглушка самопроверки"
                self.метр = None

            def транзакция(self, подпись, **kw):
                return None

            def налог_минта(self, минт):
                return {"taxed": False, "tax_bps": 0}

            def call(self, метод, параметры, **kw):
                if метод != "getBlock":
                    raise RuntimeError(f"в этом узле есть только getBlock, не {метод}")
                self.вызовов_getblock += 1
                self.вызовов += 1
                if self.падать:
                    raise RuntimeError("узел молчит")
                return self.блоки.get(параметры[0])

        st_м = ST.ExecState(base=Path(d) / "mesta", kill=Path(d) / "net_kill")
        h_м = HeliusБлокПодписи({
            5001: {"signatures": ["чужая1", "чужая2", "НАША", "чужая3", "чужая4"]}})
        детектор_м = Детектор(источники={"SRC": "BATCH-5"}, состояние=st_м,
                               курс=КурсSOL(), режим="dry", helius=h_м)
        st_м.write_intent(client_order_id="m1", mint="МИНТ", source_sig="S1",
                           source_slot=5000, sol_in=0.2, pool="POOL", program=None,
                           taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                           sell_after_s=28.8)
        st_м.update_position("m1", signatures=["НАША"], our_slot=5001)
        итог_м = детектор_м.догнать_место_в_блоке()
        поз_м = st_м.positions()["m1"]
        chk("место в блоке добрано: индекс, всего, доля",
            итог_м["filled"] == 1 and поз_м.get("block_index") == 2
            and поз_м.get("block_total") == 5 and поз_м.get("block_share") == 0.4,
            (итог_м, {к: поз_м.get(к) for к in
                       ("block_index", "block_total", "block_share")}))
        chk("слот взят из our_slot и это записано",
            поз_м.get("block_slot") == 5001
            and поз_м.get("block_slot_from") == "our_slot", поз_м.get("block_slot_from"))
        chk("второй круг ту же позицию не перечитывает",
            детектор_м.догнать_место_в_блоке()["looked"] == 0
            and h_м.вызовов_getblock == 1, h_м.вызовов_getblock)
        chk("догнано видно в признаке жизни",
            детектор_м.признак_жизни().get("block_position_backfilled") == 1)
        chk("модуль места в блоке назван в признаке жизни с причиной, а не одним словом",
            isinstance(детектор_м.признак_жизни().get("block_position"), dict)
            and "module_loaded" in детектор_м.признак_жизни()["block_position"]
            and (детектор_м.признак_жизни()["block_position"]["module_loaded"]
                 or ":" in детектор_м.признак_жизни()["block_position"]["why_not"]),
            детектор_м.признак_жизни().get("block_position"))
        st_м.write_intent(client_order_id="m2", mint="МИНТ2", source_sig="S2",
                           source_slot=6000, sol_in=0.2, pool="POOL", program=None,
                           taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                           sell_after_s=28.8)
        st_м.update_position("m2", signatures=["НЕТ_В_БЛОКЕ"], our_slot=5001)
        детектор_м.догнать_место_в_блоке()
        поз_м2 = st_м.positions()["m2"]
        chk("подписи в блоке нет -- причина и попытка, а не выдуманное место",
            поз_м2.get("block_index") is None and поз_м2.get("block_tries") == 1
            and "нет" in (поз_м2.get("block_why_not") or ""), поз_м2.get("block_why_not"))
        for _ in range(ПОПЫТОК_МЕСТА_В_БЛОКЕ + 2):
            детектор_м.догнать_место_в_блоке()
        chk("после предела попыток позиция больше не дёргается",
            st_м.positions()["m2"].get("block_tries") == ПОПЫТОК_МЕСТА_В_БЛОКЕ,
            st_м.positions()["m2"].get("block_tries"))
        st_м.write_intent(client_order_id="m3", mint="МИНТ3", source_sig="S3",
                           source_slot=7000, sol_in=0.2, pool="POOL", program=None,
                           taxed=None, tax_bps=None, mode="dry-run",
                           sell_after_s=28.8)
        st_м.update_position("m3", signatures=["НАША3"], our_slot=5001)
        было_вызовов = h_м.вызовов_getblock
        детектор_м.догнать_место_в_блоке()
        chk("позиция dry-run узел не тратит",
            h_м.вызовов_getblock == было_вызовов, h_м.вызовов_getblock)
        st_м.write_intent(client_order_id="m4", mint="МИНТ4", source_sig="S4",
                           source_slot=8000, sol_in=0.2, pool="POOL", program=None,
                           taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                           sell_after_s=28.8)
        st_м.update_position("m4", signatures=["ХВОСТ"], own_tx_seen_slot=5001)
        детектор_м.догнать_место_в_блоке()
        chk("без our_slot берём слот замерной подписки и помечаем откуда",
            st_м.positions()["m4"].get("block_slot_from") == "own_tx_seen_slot",
            st_м.positions()["m4"].get("block_slot_from"))
        h_тихий = HeliusБлокПодписи({}, падать=True)
        детектор_т = Детектор(источники={"SRC": "BATCH-5"}, состояние=st_м,
                               курс=КурсSOL(), режим="dry", helius=h_тихий)
        st_м.write_intent(client_order_id="m5", mint="МИНТ5", source_sig="S5",
                           source_slot=9000, sol_in=0.2, pool="POOL", program=None,
                           taxed=None, tax_bps=None, mode=ST.MODE_LIVE,
                           sell_after_s=28.8)
        st_м.update_position("m5", signatures=["НАША5"], our_slot=9001)
        детектор_т.догнать_место_в_блоке()
        chk("молчание узла не пишет место и не роняет службу",
            st_м.positions()["m5"].get("block_index") is None
            and st_м.positions()["m5"].get("block_why_not"),
            st_м.positions()["m5"].get("block_why_not"))

        # ---- БЮДЖЕТ ЗАМЕРА ДЕРЖИТСЯ ЗА СУТКИ, А НЕ ЗА ПРОЦЕСС ----
        # Цена ошибки измерена 24.09: счёт жил в памяти, каждый перезапуск
        # выдавал замеру новые 100 000 кредитов, и за сутки детектор истратил
        # 238 058 при своём бюджете 150 000 и разрешении владельца 100 000 на
        # замер. Проверяем именно переживание перезапуска.
        chk("израсходованное замером записано на диск",
            детектор_б.ног_кредитов_путь.exists()
            and json.loads(детектор_б.ног_кредитов_путь.read_text(
                encoding="utf-8")).get("credits") == 4,
            детектор_б.ног_кредитов_путь.read_text(encoding="utf-8")
            if детектор_б.ног_кредитов_путь.exists() else "файла нет")
        было_env = os.environ.get("BLOOM_SHADOW_LEGS_CREDITS")
        было_снят2 = os.environ.get("BLOOM_SHADOW_LEGS_OFF")
        os.environ["BLOOM_SHADOW_LEGS_CREDITS"] = "4"
        # Бюджет проверяется при ВКЛЮЧЁННОМ замере: снятый замер держит бюджет
        # нулём, и проверять на нём переживание квоты было бы проверкой снятия.
        os.environ["BLOOM_SHADOW_LEGS_OFF"] = "0"
        try:
            детектор_в = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                                   курс=КурсSOL(), режим="dry", helius=h_к)
            chk("после перезапуска замер знает, что бюджет суток уже съеден",
                детектор_в.ног_отключён is True
                and "до старта" in детектор_в.ног_отключён_почему
                and детектор_в.ног_кредитов_ранее == 4,
                (детектор_в.ног_отключён, детектор_в.ног_отключён_почему))
            ST.atomic_write_json(детектор_б.ног_кредитов_путь,
                                  {"day": "1999-01-01", "credits": 999999})
            детектор_г = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                                   курс=КурсSOL(), режим="dry", helius=h_к)
            chk("счёт другого дня не переносится на сегодня",
                детектор_г.ног_отключён is False
                and детектор_г.ног_кредитов_ранее == 0,
                (детектор_г.ног_отключён, детектор_г.ног_кредитов_ранее))
            детектор_б.ног_кредитов_путь.write_text("не json", encoding="utf-8")
            детектор_д = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                                   курс=КурсSOL(), режим="dry", helius=h_к)
            chk("битый счёт замера -- замер выключен, а не обнулён",
                детектор_д.ног_отключён is True
                and "не прочитан" in детектор_д.ног_отключён_почему,
                детектор_д.ног_отключён_почему)
            детектор_б.ног_кредитов_путь.unlink()
            детектор_е = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                                   курс=КурсSOL(), режим="dry", helius=h_к)
            детектор_е.кэш_ног = КэшПодставной()
            детектор_е.кэш_ног_адреса = {"QVAULT": "Q"}
            детектор_е.ног_кредитов_ранее = 3
            детектор_е.учесть_байты_ног(104858)
            chk("суточный счёт складывает прежние кредиты со своими",
                детектор_е.ног_кредитов_всего() == 5
                and детектор_е.ног_отключён is True,
                (детектор_е.ног_кредитов_всего(), детектор_е.ног_отключён))
            ж_е = детектор_е.признак_кэша_ног()
            chk("признак жизни показывает и суточный счёт, и прежний",
                ж_е.get("credits_day") == 5
                and ж_е.get("credits_before_start") == 3, ж_е)
        finally:
            if было_env is None:
                os.environ.pop("BLOOM_SHADOW_LEGS_CREDITS", None)
            else:
                os.environ["BLOOM_SHADOW_LEGS_CREDITS"] = было_env
            if было_снят2 is None:
                os.environ.pop("BLOOM_SHADOW_LEGS_OFF", None)
            else:
                os.environ["BLOOM_SHADOW_LEGS_OFF"] = было_снят2
            try:
                детектор_б.ног_кредитов_путь.unlink()
            except FileNotFoundError:
                pass

        chk("модуль тени не умеет отправлять транзакции",
            "sendTransaction" not in (REPO_ROOT / "analysis"
                                       / "c2_shadow_build.py").read_text(encoding="utf-8"))

        # УПАВШАЯ ПО ЦЕПИ ПОКУПКА. 24.09 в 14:10:08Z владелец получил
        # "🟢 покупка SENT ... S+0 по цепи" по транзакции, которая в цепи
        # упала: слот у упавшей есть, и S+N считался как доказательство
        # посадки. Позиция при этом оставалась открытой, а сторож через 68 с
        # доложил "продана" по остатку, которого не было никогда.
        упавшая = tx_пул(владельцы={"POOLA": ["КУПЛЕН", WSOL]}, счета=("POOLA",),
                          подписанты=(ST.EXECUTOR_WALLET,))
        упавшая.setdefault("meta", {})
        упавшая["meta"]["err"] = {"InstructionError": [3, {"Custom": 6001}]}
        упавшая["meta"]["logMessages"] = [
            "Program log: Error: slippage tolerance exceeded",
            "Program XYZ failed: custom program error: 0x1771"]
        упавшая["slot"] = 777
        послано_у: list = []

        class Оповещатель:
            def послать(self, текст):
                послано_у.append(текст)

        детектор_у = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                               курс=КурсSOL(), режим="dry",
                               helius=HeliusНашаTx(упавшая))
        детектор_у.оповещатель = Оповещатель()
        st.write_intent(client_order_id="cf", mint="КУПЛЕН", source_sig="S3",
                         source_slot=770, sol_in=0.2, pool="POOLA", program=None,
                         taxed=None, tax_bps=None, mode=ST.MODE_LIVE_TEST,
                         sell_after_s=28.8)
        зап4 = детектор_у.разобрать_нашу_покупку(
            cid="cf", минт="КУПЛЕН", подпись="УПАВШАЯ", источник_маршрут={},
            источник_пул="POOLA", exec_row={"signatures": ["УПАВШАЯ"],
                                             "exec_code": "SENT"},
            слот_источника=770)
        chk("упавшая по цепи покупка помечена кодом и chain_ok=False",
            зап4.get("code") == КОД_ПОКУПКА_УПАЛА and зап4.get("chain_ok") is False,
            зап4)
        chk("причина отказа сохранена целиком",
            зап4.get("chain_err") == {"InstructionError": [3, {"Custom": 6001}]},
            зап4.get("chain_err"))
        chk("строка журнала программ с причиной подобрана",
            any("slippage" in с for с in (зап4.get("logs") or [])), зап4.get("logs"))
        chk("позиция упавшей покупки закрыта, сторожу ждать нечего",
            st.positions()["cf"].get("state") == ST.STATE_CLOSED
            and st.positions()["cf"].get("chain_ok") is False,
            st.positions()["cf"])
        chk("в Telegram ушла строка о ПАДЕНИИ, а не зелёная о покупке",
            послано_у and "покупка УПАЛА" in послано_у[0]
            and "🟢" not in послано_у[0], послано_у)
        chk("маршрут упавшей покупки не считается",
            зап4.get("our_pool") is None and "our_route" not in зап4, зап4)

        # СЕВШАЯ ПОКУПКА ТОЖЕ ОБЯЗАНА ПОМЕТИТЬ ПОЗИЦИЮ. 24.09 у пяти боевых
        # покупок с 17:12Z chain_ok в позиции остался пустым при meta.err=null
        # по цепи: ветка падения поле писала, ветка удачи -- нет. Пустое поле
        # читается как "неизвестно", и всякий счёт, который ищет УДАЧУ, её не
        # находил: в том числе обрыв серии упавших у полосы своей отправки,
        # где три упавших подряд останавливают торговлю.
        севшая = tx_пул(владельцы={"POOLA": ["КУПЛЕН", WSOL]}, счета=("POOLA",),
                         подписанты=(ST.EXECUTOR_WALLET,))
        севшая.setdefault("meta", {})
        севшая["meta"]["err"] = None
        севшая["slot"] = 781
        детектор_с = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                               курс=КурсSOL(), режим="dry",
                               helius=HeliusНашаTx(севшая))
        st.write_intent(client_order_id="cs", mint="КУПЛЕН", source_sig="S4",
                         source_slot=780, sol_in=0.2, pool="POOLA", program=None,
                         taxed=None, tax_bps=None, mode=ST.MODE_LIVE_TEST,
                         sell_after_s=28.8)
        зап5 = детектор_с.разобрать_нашу_покупку(
            cid="cs", минт="КУПЛЕН", подпись="СЕВШАЯ", источник_маршрут={},
            источник_пул="POOLA", exec_row={"signatures": ["СЕВШАЯ"],
                                             "exec_code": "SENT"},
            слот_источника=780)
        chk("севшая покупка помечена chain_ok=True в ЗАПИСИ журнала",
            зап5.get("chain_ok") is True and зап5.get("code") != КОД_ПОКУПКА_УПАЛА,
            зап5.get("chain_ok"))
        chk("и chain_ok=True доехал до ПОЗИЦИИ, а не остался в журнале",
            st.positions()["cs"].get("chain_ok") is True,
            st.positions()["cs"].get("chain_ok"))
        chk("позиция севшей покупки НЕ закрыта -- её продаёт сторож",
            st.positions()["cs"].get("state") != ST.STATE_CLOSED,
            st.positions()["cs"].get("state"))

    # 16в. ЖИВЫЕ транзакции: разбор на настоящих данных.
    #
    # Файл РЕГРЕССИИ отдельный и постоянный. Раньше эти проверки читали
    # data/bloom_tx_raw.json -- свалку последнего прогона разбора, которую
    # каждый новый прогон перезаписывает. Регрессия при этом не падала, а
    # ТИХО ПРОПАДАЛА: нужной транзакции в файле нет -- блок просто не
    # выполнялся. Теперь случаи лежат в bloom_regression_txs.json, и их
    # отсутствие -- провал самопроверки, а не тишина.
    сырой_путь = REPO_ROOT / "data" / "bloom_regression_txs.json"
    РЕД = "65dw58ugEt3EN9uNuJ2CCyWz7SENe2hnVv9dNHyUjz8x"
    ФОМО = "8aTMUKspLnkm7jaHf21qfB1b5dzPzXgsmcjPPnJPaPtA"
    try:
        сырое = json.loads(сырой_путь.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        сырое = {}
        chk("файл живых случаев регрессии читается", False, str(exc))

    def найти_tx(подпись_начало: str) -> dict | None:
        for п, з in (сырое or {}).items():
            if п.startswith(подпись_начало):
                return ((з or {}).get("tx") or {}) or None
        return None

    живая = None
    for подпись, зап in (сырое or {}).items():
        tx_ж = (зап or {}).get("tx") or {}
        минты = {b.get("mint") for b in
                 ((tx_ж.get("meta") or {}).get("postTokenBalances") or [])}
        if РЕД in минты:
            живая = (подпись, tx_ж)
            break
    chk("живая транзакция п. 3 (RED) в файле регрессии есть", живая is not None)
    if живая:
        подпись_ж, tx_ж = живая
        s_ж = сигнал_из_транзакции(tx_ж, ФОМО, подпись=подпись_ж,
                                    слот=tx_ж.get("slot"))
        chk("на живой транзакции п. 3 разбор видит покупку",
            s_ж.get("kind") == "buy", s_ж.get("kind"))
        chk("и минт -- именно RED, а не остаток прошлого токена",
            s_ж.get("mint") == РЕД, s_ж.get("mint"))
        chk("и трата -- 8 USDC",
            s_ж.get("spend_mint") == USDC and abs((s_ж.get("spend_ui") or 0) - 8.0) < 1e-9,
            (s_ж.get("spend_mint"), s_ж.get("spend_ui")))
        chk("и это первый вход по этому минту", s_ж.get("first_entry") is True,
            s_ж.get("first_entry"))
        with _врем_каталог() as d:
            stж = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            # порог стенда 0.05 SOL-эквивалента, курс берём тот же, что в бою
            r_ж = решение(s_ж, состояние=stж, трата_sol=0.069,
                           баланс_sol=0.35, текущий_слот=tx_ж.get("slot"),
                           порог_sol=0.05)
            chk("решение по живой транзакции п. 3 -- BUY",
                r_ж.get("action") == "buy" and r_ж.get("code") == КОД_КУПИТЬ,
                (r_ж.get("action"), r_ж.get("code"), r_ж.get("reason")))

    # 16в-2. ЖИВЫЕ покупки за USDC, потерянные на пороге.
    #
    # Источник платил 1000 и 2317 USDC, но сам же платил комиссию и аренду
    # нового ATA -- и тратой считался остаток нативного SOL: 0.0067 и
    # 0.00055 SOL при пороге 2. Обе ушли в TARGET_AMOUNT_OUT_OF_RANGE, DBot
    # их купил. Проверка идёт на настоящих ответах узла.
    КУРС_ТОГДА = 115.16152492277841
    ПОТЕРИ = (
        ("2EwScfixkH1ZZ", "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv",
         "FLyzixQCC1osJxi3uhQAntuKvgDZ1pUAiDEobi8z9MNL", 1000.0, 8.68),
        ("33Kvehwq4LDNk", "4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8",
         "CvUX4G2tzq7J3ECmNvWZLayZYV6KpgQp6SJ5K8Uvsu5g", 2317.0, 20.11),
    )
    for начало, источник_п, минт_п, usdc, sol_ожид in ПОТЕРИ:
        tx_п = найти_tx(начало)
        chk(f"живой случай {начало} есть в файле регрессии", tx_п is not None)
        if not tx_п:
            continue
        s_п = сигнал_из_транзакции(tx_п, источник_п, подпись=начало,
                                    слот=tx_п.get("slot"))
        chk(f"{начало}: это покупка", s_п.get("kind") == "buy", s_п.get("kind"))
        chk(f"{начало}: минт покупки узнан", s_п.get("mint") == минт_п, s_п.get("mint"))
        трата_п, пояснение_п = в_sol(s_п, КУРС_ТОГДА)
        chk(f"{начало}: тратой признан USDC, а не остаток SOL",
            s_п.get("spend_mint") == USDC
            and abs((s_п.get("spend_ui") or 0) - usdc) < 1e-6,
            (s_п.get("spend_mint"), s_п.get("spend_ui"), пояснение_п))
        chk(f"{начало}: SOL-эквивалент около {sol_ожид}",
            трата_п is not None and abs(трата_п - sol_ожид) < 0.05, трата_п)
        ок_п, код_п, почему_п = фильтры_dbot(s_п, трата_п, порог_sol=2.0)
        chk(f"{начало}: при пороге 2 SOL это BUY, а не TARGET_AMOUNT_OUT_OF_RANGE",
            ок_п is True, (код_п, почему_п))

    # Аренда новых счетов вычитается по факту, а не по табличному значению.
    tx_рента = найти_tx("2EwScfixkH1ZZ")
    if tx_рента:
        s_р = сигнал_из_транзакции(
            tx_рента, "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv",
            подпись="2EwScfixkH1ZZ", слот=tx_рента.get("slot"))
        chk("аренда нового счёта посчитана и вычтена из SOL-ноги",
            (s_р.get("rent_new_accounts_sol") or 0) > 0
            and (s_р.get("spend_candidates") or {}).get(WSOL, 0)
                < (s_р.get("sol_out_gross") or 0),
            (s_р.get("rent_new_accounts_sol"), s_р.get("sol_out_gross"),
             (s_р.get("spend_candidates") or {}).get(WSOL)))

    # Без курса две ноги не сравнить -- и молча брать SOL-ногу нельзя.
    if tx_рента:
        s_бк = сигнал_из_транзакции(
            tx_рента, "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv",
            подпись="2EwScfixkH1ZZ", слот=tx_рента.get("slot"))
        трата_бк, пояснение_бк = в_sol(s_бк, None)
        chk("без курса две ноги -- честный отказ, а не SOL-нога",
            трата_бк is None and "курса" in пояснение_бк, (трата_бк, пояснение_бк))
        ок_бк, код_бк, _ = фильтры_dbot(s_бк, трата_бк, порог_sol=2.0)
        chk("и код при этом -- отсутствие курса", код_бк == КОД_НЕТ_КУРСА, код_бк)

    # 16в-3. ЖИВЫЕ пять покупок, срезанных СТАРЫМ правилом маршрута.
    # Все пять -- покупки за USDC, где комиссию платил сторонний кошелёк, а
    # лишние минты источник только ПОДПИСЫВАЛ. Новое правило их пропускает.
    МАРШРУТЫ = (
        ("3jYZjKVWjDuo", "BA3nKHc4DoSANRrx4FcCupExzs6cWzw1wkPpqjnqJaCN",
         "3c7mmVSyEH8jfZXgxvpLsETtko1Y16DyRJ5XYB4snhGt"),
        ("2kLHjtpm2e43", "4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8",
         "DtYzXe9cR6b6ExReMCn5yHzvbS4nUQZ1GkBQDqQoSTNK"),
        ("4kozkrJN8KVs", "4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8",
         "HpWGRTs5x2pmWrGKpD5cXpZhja1uuXbPBqf7kYo2mz86"),
        ("5gojtrRZK57X", "4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8",
         "74sHNXtVDHZVw4ADktjGFHPzNycV8iHvfHLPp6QZPdT4"),
        ("2j46i2LVmm5M", "4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8",
         "74sHNXtVDHZVw4ADktjGFHPzNycV8iHvfHLPp6QZPdT4"),
        # ещё три из тех же восьми: источник GAsnqm4X…, все три DBot купил
        ("4GuLGZozuvHb", "GAsnqm4XkNkPVgrAofNQ65jWf8f3tKCLHhE9ZqSy2AP1",
         "FYayBW6PMhzuNUVTJSr4ruk66p1nVhJ3bSmYueMr1ZMS"),
        ("4P9sC5ZHyXuY", "GAsnqm4XkNkPVgrAofNQ65jWf8f3tKCLHhE9ZqSy2AP1",
         "9GJQgnwKZL6oqsPcLmSohBb61w6cdj629b1RFhWvUe7j"),
        ("496iPttxBq73", "GAsnqm4XkNkPVgrAofNQ65jWf8f3tKCLHhE9ZqSy2AP1",
         "DoqY9DZPnP11ptofS2jX5EZSnq6Cvt1mSzw1XJscwGUK"),
    )
    for начало, источник_м, минт_м in МАРШРУТЫ:
        tx_м = найти_tx(начало)
        chk(f"живой случай маршрута {начало} есть в файле регрессии", tx_м is not None)
        if not tx_м:
            continue
        s_м = сигнал_из_транзакции(tx_м, источник_м, подпись=начало,
                                    слот=tx_м.get("slot"))
        трата_м, _ = в_sol(s_м, КУРС_ТОГДА)
        ок_м, код_м, почему_м = фильтры_dbot(s_м, трата_м, порог_sol=2.0)
        chk(f"{начало}: минт покупки тот самый", s_м.get("mint") == минт_м, s_м.get("mint"))
        chk(f"{начало}: промежуточных минтов по новому правилу нет",
            not (s_м.get("route") or {}).get("via_intermediate"),
            (s_м.get("route") or {}).get("intermediate_mints"))
        chk(f"{начало}: решение -- BUY", ок_м is True, (код_м, почему_м))

    # 16в-4. ДВА СТЕНДОВЫХ случая, на которых владелец и увидел ложный
    # INTERMEDIATE_ROUTE: прямая покупка на pump.fun (лишний минт источник
    # только подписывал) и покупка через Jupiter (лишние минты -- ноги
    # чужого агрегатора, к счетам источника отношения не имеют).
    СТЕНД_ИСТОЧНИК = "DNJeTYni5QQVCwNNt1SrHu1GjYR4ak5Zw7LeuFsKYmXX"
    СТЕНД_СЛУЧАИ = (
        ("K6k7PzMs2BiM", "AC1vvvYcG3Kqq61EQNVmRojDaokJ4xbpnSE5sw3pump", 0.072),
        ("fKySoSW4pXgj", "NeonTjSjsuo3rexg9o6vHuMXw62f9V7zvmu8M8Zut44", 0.07),
    )
    for начало, минт_с, трата_ожид in СТЕНД_СЛУЧАИ:
        tx_с = найти_tx(начало)
        chk(f"стендовый случай {начало} есть в файле регрессии", tx_с is not None)
        if not tx_с:
            continue
        s_с = сигнал_из_транзакции(tx_с, СТЕНД_ИСТОЧНИК, подпись=начало,
                                    слот=tx_с.get("slot"))
        трата_с, _ = в_sol(s_с, КУРС_ТОГДА)
        chk(f"{начало}: минт покупки тот самый", s_с.get("mint") == минт_с, s_с.get("mint"))
        chk(f"{начало}: трата около {трата_ожид} SOL",
            трата_с is not None and abs(трата_с - трата_ожид) < 0.005, трата_с)
        chk(f"{начало}: промежуточных минтов нет",
            not (s_с.get("route") or {}).get("via_intermediate"),
            (s_с.get("route") or {}).get("intermediate_mints"))
        ок_с, код_с, почему_с = фильтры_dbot(s_с, трата_с, порог_sol=0.05)
        chk(f"{начало}: при пороге стенда 0.05 это BUY", ок_с is True, (код_с, почему_с))

    # 16г. падение разбора не рвёт подписку
    with _врем_каталог() as d:
        stп = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        детп = Детектор(источники={"SRC": "BATCH-5"}, состояние=stп,
                         курс=КурсSOL(), режим="dry", helius=HeliusЗаглушка())

        def падающий_разбор(*a, **k):
            raise sqlite3.ProgrammingError("SQLite objects created in a thread")

        детп.обработать = падающий_разбор
        итог_п = asyncio.run(детп.обработать_бережно("ПОДПИСЬ_ПАДЕНИЯ", 1, "SRC",
                                                      "transactionSubscribe", None,
                                                      time.time()))
        chk("падение разбора наружу не выходит", итог_п is None)
        chk("и посчитано отдельным счётчиком", детп.сбоев_разбора == 1,
            детп.сбоев_разбора)
        записи_п = [json.loads(x) for x in
                    stп.decisions_path.read_text(encoding="utf-8").splitlines() if x.strip()]
        chk("и в журнале есть запись о падении с подписью",
            any(z.get("code") == КОД_РАЗБОР_УПАЛ
                and z.get("signature") == "ПОДПИСЬ_ПАДЕНИЯ" for z in записи_п),
            записи_п[-1] if записи_п else None)
        chk("и в признаке жизни виден счётчик падений",
            детп.признак_жизни().get("handler_crashes") == 1,
            детп.признак_жизни().get("handler_crashes"))

    # 17. свежесть баланса: протухший баланс -- это неизвестный баланс
    with _врем_каталог() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "kill")
        det = Детектор(источники={"SRC": "BATCH-5"}, состояние=st,
                        helius=Helius(key="нет"), курс=КурсSOL(), режим="dry")
        chk("без замера баланс неизвестен", det.свежий_баланс() is None)
        det.баланс_sol, det.t_баланс = 3.0, time.time()
        chk("свежий баланс отдаётся", det.свежий_баланс() == 3.0)
        det.t_баланс = time.time() - 120
        chk("протухший баланс -- None, а не последнее известное",
            det.свежий_баланс() is None, det.свежий_баланс())

    # 18. учёт кредитов пишется по службе и не роняет работу
    with _врем_каталог() as d:
        было = os.environ.get("BLOOM_STATE_DIR")
        os.environ["BLOOM_STATE_DIR"] = str(Path(d) / "st")
        try:
            h = Helius(key="нет", служба="bloom_detector")
            chk("счётчик кредитов создан", h.метр is not None)
            h._учесть("getTransaction", 1234)
            h._учесть("getTransaction", 1234)
            h._учесть("getBalance", 100)
            chk("вызовы считаются по методам",
                h.по_методам == {"getTransaction": 2, "getBalance": 1}, h.по_методам)
            путь = Path(d) / "st" / "helius_usage" / "bloom_detector.json"
            chk("осколок учёта записан на диск", путь.exists(), путь)
            данные = json.loads(путь.read_text(encoding="utf-8"))
            день = list((данные.get("дни") or {}).values())[0]
            chk("кредиты легли на имя службы", "bloom_detector" in день, list(день))
            chk("кредитов ровно по числу вызовов",
                день["bloom_detector"]["кредитов_за_день"] == 3,
                день["bloom_detector"]["кредитов_за_день"])
            # Потолок детектора поднят владельцем 25.09 до 1 000 000 в сутки:
            # 60 боевых источников дают трафик подписки в сотни МБ в час, а
            # сигналы нужнее кредитов. Число берём из одного места -- из
            # solana_rpc_client.DAILY_BUDGET, чтобы проверка не разошлась с
            # настройкой.
            import solana_rpc_client as RPCб  # noqa: PLC0415

            chk("бюджет службы -- тот, что назвал владелец (1 000 000 с 25.09)",
                день["bloom_detector"].get("бюджет_за_день")
                == RPCб.DAILY_BUDGET["bloom_detector"] == 1_000_000,
                день["bloom_detector"].get("бюджет_за_день"))
            h.учесть_вебсокет(int(0.2 * 1024 * 1024))
            данные2 = json.loads(путь.read_text(encoding="utf-8"))
            день2 = list((данные2.get("дни") or {}).values())[0]
            chk("вебсокетные байты тоже стоят кредитов",
                день2["bloom_detector"]["кредитов_за_день"] == 3 + 4,
                день2["bloom_detector"]["кредитов_за_день"])
            h.учесть_вебсокет(1)
            данные3 = json.loads(путь.read_text(encoding="utf-8"))
            день3 = list((данные3.get("дни") or {}).values())[0]
            chk("один байт -- уже порция, а не ноль",
                день3["bloom_detector"]["кредитов_за_день"] == 3 + 4 + 2,
                день3["bloom_detector"]["кредитов_за_день"])
            # Отказ записи учёта обязан быть ВИДЕН, а не проглочен.
            # Права каталога здесь не годятся: самопроверка идёт от root,
            # а root их обходит. Поэтому отказ задаётся прямо -- проверяем
            # контракт, а не способность ОС запретить запись.
            настоящий = h.метр

            class МетрОтказ:
                session_credits = 0

                def add(self, *_, **__):
                    raise PermissionError("каталог учёта закрыт для службы")

            h.метр = МетрОтказ()
            h._учесть("getSlot")
            chk("отказ записи учёта помечен", h.учёт_пишется is False, h.учёт_пишется)
            chk("и причина названа словами",
                "PermissionError" in h.учёт_почему, h.учёт_почему)
            chk("но счётчик вызовов всё равно вырос",
                h.по_методам.get("getSlot") == 1, h.по_методам)
            h.метр = настоящий
            h._учесть("getSlot")
            chk("после починки учёт снова пишется", h.учёт_пишется is True)

            h2 = Helius(key="нет", служба="")
            chk("без имени службы учёт не ведётся и это не падение", h2.метр is None)
            h2._учесть("getSlot")
            chk("и вызовы всё равно считаются", h2.по_методам == {"getSlot": 1}, h2.по_методам)
        finally:
            if было is None:
                os.environ.pop("BLOOM_STATE_DIR", None)
            else:
                os.environ["BLOOM_STATE_DIR"] = было

    # 19. slot_ok
    chk("слот 0 -- не слот", not slot_ok(0))
    chk("слот None -- не слот", not slot_ok(None))
    chk("слот 5 -- слот", slot_ok(5))

    # --- ТИШИНА НЕ ДЕРЖИТ НАС НА ЗАПАСНОМ ПУТИ. Замер 25.09 по журналу за
    # сутки: 40 переходов, 31 % времени на запасном, медиана эпизода 311 с при
    # окне 120 с и один эпизод 3.1 часа. Причина: окно возврата проверялось
    # только при ПРИХОДЕ сообщения, а ночью источники молчат -- проверять было
    # некому. Проверяем на поддельном соединении, без сети.
    import asyncio as _aio  # noqa: PLC0415
    import tempfile as _tmp2  # noqa: PLC0415

    class ЗакрытоСервером(Exception):
        pass

    class ВебсокетЗаглушка:
        def __init__(self, сценарий: list, подписки: list) -> None:
            self.сценарий = сценарий
            self.подписки = подписки

        async def send(self, данные):
            try:
                self.подписки.append(json.loads(данные).get("method"))
            except ValueError:
                self.подписки.append("не json")

        async def recv(self):
            if self.сценарий:
                что = self.сценарий.pop(0)
                if что == "обрыв":
                    raise ЗакрытоСервером("no close frame received or sent")
            # ТИШИНА: никогда ничего не отдаём. Именно на этом служба и висела.
            await _aio.sleep(3600)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *а):
            return False

    # МЕТОД НА КАЖДОЕ СОЕДИНЕНИЕ, а не на каждую подписку: подписка идёт по
    # адресу на запрос, и одних transactionSubscribe было бы столько же,
    # сколько адресов. Нас же интересует ПОРЯДОК путей: основной, запасной,
    # снова основной.
    подписки_сделаны: list = []
    сценарии: list = []

    class МодульWS:
        @staticmethod
        def connect(*а, **кв):
            сц = сценарии.pop(0) if сценарии else []
            подписки_сделаны.append("--соединение--")
            return ВебсокетЗаглушка(сц, подписки_сделаны)

    class HeliusДляСлушания(Helius):
        """НАСТОЯЩИЙ класс узла, но без сети.

        Признак жизни пишется при каждом обрыве и читает у узла десяток
        счётчиков. Заглушка, перечисляющая их руками, ломалась на каждом
        новом поле -- и мерила бы тогда заглушку, а не код. Наследуемся от
        настоящего класса и глушим ровно то, что ходит в сеть.
        """

        def __init__(self) -> None:
            super().__init__(key="КЛЮЧ", служба="")

        def учесть_вебсокет(self, байт):
            return None

        def call(self, метод, параметры, **кв):
            return None

        def транзакция(self, подпись, **кв):
            return None

    def прогон_подписки(сцен: list, *, срок_подъёма: float, окно: float,
                         стоп_через: float = 2.6):
        """Один прогон слушателя на поддельных соединениях. Возвращает
        (порядок путей по соединениям, детектор)."""
        подписки_сделаны.clear()
        сценарии.clear()
        сценарии.extend(сцен)
        пути: list = []
        with _врем_каталог() as врем_сл_:
            st_ = ST.ExecState(base=Path(врем_сл_) / "listen",
                                kill=Path(врем_сл_) / "listen" / "НЕТ")
            д_ = Детектор(источники={"SRCL": "BATCH-5"}, состояние=st_,
                           курс=КурсSOL(), режим="dry",
                           helius=HeliusДляСлушания())
            было_ws_ = globals().get("websockets")
            было_окно_ = globals()["ОКНО_ЗАПАСНОГО_S"]
            было_тик_ = globals()["ТИК_ПОДПИСКИ_S"]
            было_срок_ = globals()["СРОК_ПОДЪЁМА_ОСНОВНОГО_S"]
            globals()["websockets"] = МодульWS
            globals()["ОКНО_ЗАПАСНОГО_S"] = окно
            globals()["ТИК_ПОДПИСКИ_S"] = 0.05
            globals()["СРОК_ПОДЪЁМА_ОСНОВНОГО_S"] = срок_подъёма
            try:
                _aio.run(слушать(д_, "КЛЮЧ", стоп_через_s=стоп_через))
            except Exception as exc:  # noqa: BLE001
                подписки_сделаны.append(f"упало: {type(exc).__name__}: {exc}")
            finally:
                globals()["websockets"] = было_ws_
                globals()["ОКНО_ЗАПАСНОГО_S"] = было_окно_
                globals()["ТИК_ПОДПИСКИ_S"] = было_тик_
                globals()["СРОК_ПОДЪЁМА_ОСНОВНОГО_S"] = было_срок_
            for з_ in list(подписки_сделаны):
                if з_ == "--соединение--":
                    пути.append(None)
                elif пути and пути[-1] is None:
                    пути[-1] = з_
        return пути, д_

    # ОБРЫВ ОСНОВНОГО -- СПЕРВА НЕМЕДЛЕННОЕ ПЕРЕПОДКЛЮЧЕНИЕ (решение владельца
    # 25.09 вечера). Запасной путь не поднимается вовсе, если основной встал за
    # срок: все 94 сигнала, разобранных 25.09 через getTransaction, пришлись
    # ровно на окна запасного пути.
    пути_сразу, д_сразу = прогон_подписки([["обрыв"], [], [], [], []],
                                            срок_подъёма=5.0, окно=0.2)
    chk("обрыв основного -- переподключаем основной, на запасной не уходим",
        пути_сразу[:2] == ["transactionSubscribe", "transactionSubscribe"]
        and "logsSubscribe" not in пути_сразу, пути_сразу)
    chk("тишина считается тиками, а не обрывами",
        д_сразу.тиков_тишины > 0 and д_сразу.обрывов == 1,
        (д_сразу.тиков_тишины, д_сразу.обрывов))
    chk("обрыв назван причиной, а не потерян",
        any("соединение закрыто" in п or "незнакомая" in п
            for п in д_сразу.обрывов_по_причинам), д_сразу.обрывов_по_причинам)

    # А ВОТ ЕСЛИ ОСНОВНОЙ НЕ ПОДНЯЛСЯ ЗА СРОК -- запасной всё-таки нужен, и из
    # тишины на нём мы ВОЗВРАЩАЕМСЯ на основной по часам.
    # Срок подъёма 0 с -- это в точности прежнее поведение: обрыв сразу уводит
    # на запасной. Так проверяется вторая половина правила, не трогая первую.
    пути_зап, д_зап = прогон_подписки(
        [["обрыв"], [], [], [], []], срок_подъёма=0.0, окно=0.2)
    chk("основной не встал за срок -- идём на запасной и возвращаемся",
        "logsSubscribe" in пути_зап
        and пути_зап.index("logsSubscribe") >= 1
        and "transactionSubscribe" in пути_зап[пути_зап.index("logsSubscribe") + 1:],
        пути_зап)
    chk("время на запасном посчитано и эпизод закрыт",
        д_зап.запасное_время_с > 0 and д_зап.запасной_путь_с is None,
        (д_зап.запасное_время_с, д_зап.запасной_путь_с))

    прошло = sum(1 for _, ок, _ in проверки if ок)
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'ПЛОХО'}] {имя}" + (f" -- {факт}" if not ок else ""))
    print(f"самопроверка детектора: {прошло}/{len(проверки)} пройдено")
    return 0 if прошло == len(проверки) else 1


# ----------------------------------------------------------------- запуск

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--sig", help="разобрать одну подпись и показать решение")
    p.add_argument("--source", help="адрес источника для --sig")
    p.add_argument("--serve", action="store_true")
    p.add_argument("--check-only", action="store_true",
                    help="что вижу и чем считаю -- без подписки и без решений")
    p.add_argument("--config", default=str(REPO_ROOT / "data" / "final" /
                                            "20260923T145755Z" / "konfig.json"))
    p.add_argument("--tasks", default=os.environ.get("BLOOM_TASKS", "BATCH-5,BATCH-3"))
    p.add_argument("--seconds", type=float, default=None)
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if a.self_test:
        return self_test()

    задачи = tuple(x.strip() for x in a.tasks.split(",") if x.strip())
    ист, откуда = источники(задачи, Path(a.config))
    состояние = ST.ExecState()
    # В режиме --check-only счётчик кредитов НЕ создаётся: этот режим
    # запускается при деплое от root, и созданный им каталог учёта потом
    # закрыт для службы, работающей от bot. Один раз мы так уже потеряли
    # весь расход детектора.
    helius = Helius(служба="" if a.check_only else "bloom_detector")
    курс = КурсSOL()
    исполнитель = None
    if (os.environ.get("BLOOM_EXEC") or "").strip() == "1":
        if EXEC is None:
            log.error("BLOOM_EXEC=1, но модуль исполнителя не импортируется -- "
                      "детектор будет только вести журнал")
        else:
            исполнитель = EXEC.Executor(
                state=состояние,
                api=__import__("bloom_api").BloomApi(
                    os.environ.get("BLOOM_API_KEY", ""),
                    # dry_run строго по режиму: в live-test запросы РЕАЛЬНЫЕ.
                    dry_run=(EXEC.current_mode() == ST.MODE_DRY), state=состояние))
            log.info("исполнитель подключён, режим %s", EXEC.current_mode())
    детектор = Детектор(источники=ист, состояние=состояние,
                         helius=helius, курс=курс,
                         режим=os.environ.get("BLOOM_MODE", "dry"),
                         исполнитель=исполнитель)
    детектор.откуда_источники = откуда

    # Таблицы адресов для двухшаговой тени -- при старте и в своём потоке:
    # один getMultipleAccounts, и он не имеет права задержать подписку.
    if детектор.кэш_ног is not None and not a.check_only:
        threading.Thread(target=детектор.прогреть_таблицы_ног,
                          name="warm-luts", daemon=True).start()

    # Прогрев соединения к Bloom. Замер с хоста 24.09: первый запрос по
    # новому соединению отвечает за 54.8-65.1 мс, по уже открытому -- за
    # 15.4-22.6 мс. Между сигналами пауза бывает минутами, край (cloudflare)
    # закрывает соединение по простою, и боевая покупка платила рукопожатие
    # почти всегда. /ping бюджет запросов НЕ тратит, поэтому прогрев бесплатен.
    детектор.прогрев = None
    if исполнитель is not None and not a.check_only:
        try:
            период = float(os.environ.get("BLOOM_KEEPALIVE_S", "20") or 20)
        except ValueError:
            период = 20.0
        if период > 0:
            try:
                API_МОД = __import__("bloom_api")
                детектор.прогрев = API_МОД.Прогрев(исполнитель.api, период_s=период)
                детектор.прогрев.круг()          # первое соединение -- до сигнала
                детектор.прогрев.запустить_в_потоке()
                log.info("прогрев соединения к Bloom: раз в %.0f с", период)
            except Exception as exc:  # noqa: BLE001
                log.warning("прогрев соединения не запущен: %s: %s",
                            type(exc).__name__, str(exc)[:160])
                детектор.прогрев = None

    # Команды владельца из Telegram. Запускаются ПОСЛЕ проверки check_only:
    # в проверочном режиме служба ничего не слушает и не отвечает.
    if TGC is not None and not a.check_only:
        def статус_строкой() -> str:
            ж = детектор.признак_жизни()
            исп = ж.get("executor") or {}
            убит, почему = состояние.kill_active()
            стоп_прод, _ = состояние.sell_kill_active()
            открытых = len(состояние.open_positions())
            дн = состояние.report().get("day_pnl") or {}
            return ("\n".join([
                f"режим: {ж.get('mode')}, покупки {'ДА' if исп.get('live_buy_enabled') else 'нет'}"
                f", размер {исп.get('buy_sol')} SOL",
                f"рубильник: {'ВКЛЮЧЁН -- ' + почему if убит else 'выключен'}",
                f"наши продажи: {'ОСТАНОВЛЕНЫ' if стоп_прод else 'разрешены'}",
                f"баланс: {ж.get('balance_sol')} SOL, открытых позиций {открытых}",
                f"за сутки: покупок {дн.get('buys')}, продаж {дн.get('sells')}, "
                f"результат {дн.get('realized_sol')} SOL",
                f"сигналов {ж.get('signals_seen')}, к покупке {ж.get('to_buy')}, "
                f"сбоев разбора {ж.get('handler_crashes')}",
                f"курс {ж.get('rate_usd_sol')} ({ж.get('rate_source')}), "
                f"решений по протухшему курсу {ж.get('rate_stale_used')}",
            ]))

        детектор.команды = TGC.Команды(состояние, статус_фн=статус_строкой)
        поток = детектор.команды.запустить_в_потоке()
        ок_к, почему_к = TGC.настроено()
        log.info("команды Telegram: %s%s", "слушаю" if поток else "выключены",
                  "" if ок_к else f" ({почему_к})")

    if a.check_only:
        print(json.dumps({"sources": len(ист), "from_where": откуда,
                           "tasks": sorted(set(ист.values())),
                           "addresses": sorted(ист),
                           "rate_usd_sol": курс.получить(),
                           "rate_source": курс.источник,
                           "rate_failures": курс.отказы,
                           "slot": helius.слот(),
                           "kill_switch": состояние.kill_active(),
                           # ПОЛОСА СВОЕЙ ОТПРАВКИ -- в проверке перед
                           # запуском: по журналу деплоя должно быть видно, в
                           # каком она состоянии и есть ли ключ для подписи.
                           # Значение ключа не печатается и не читается --
                           # только факт его наличия.
                           "own_send": {
                               "enabled": детектор.полоса_включена,
                               "live": (OS is not None and OS.живьём()),
                               "module_loaded": OS is not None,
                               "module_why_not": globals().get("OWN_SEND_IMPORT_ERR", ""),
                               "size_sol": (OS.размер_sol() if OS is not None else None),
                               "sender": (OS.адрес_сендера() if OS is not None else None),
                               "key_present": bool(
                                   os.environ.get("EXEC_WALLET_KEY")
                                   or os.environ.get("BLOOM_WALLET_KEY")),
                               "limits": ({"open": OS.ЛИМИТ_ОТКРЫТЫХ,
                                            "per_day": OS.ЛИМИТ_В_СУТКИ,
                                            "stop_failed_in_row": OS.СТОП_ПОДРЯД_УПАВШИХ,
                                            "stop_loss_sol": OS.СТОП_УБЫТОК_SOL,
                                            "ceiling_sol": OS.ПОТОЛОК_РАЗМЕРА_SOL}
                                           if OS is not None else {})},
                           "heartbeat_path": str(детектор.статус_путь())},
                          ensure_ascii=False, indent=2))
        return 0

    if a.sig:
        источник = a.source
        if not источник:
            print("для --sig нужен --source: без него неизвестно, чьи балансы смотреть")
            return 2
        r = детектор.обработать(a.sig, None, источник, "вручную", None)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    if a.serve:
        log.info("источников: %d (%s), откуда: %s", len(детектор.источники),
                 ", ".join(sorted(set(детектор.источники.values()))),
                 детектор.откуда_источники)
        детектор.признак_жизни()

        async def прогон():
            задачи_фона = [
                asyncio.create_task(биение(детектор, a.seconds)),
                asyncio.create_task(часы_слотов(детектор, helius.key, a.seconds)),
                asyncio.create_task(часы_баланса(детектор, стоп_через_s=a.seconds)),
                asyncio.create_task(тёплый_хеш(детектор, стоп_через_s=a.seconds)),
                asyncio.create_task(обновлятель(детектор, задачи, Path(a.config))),
                asyncio.create_task(слушать(детектор, helius.key, стоп_через_s=a.seconds)),
            ]
            try:
                await задачи_фона[-1]
            finally:
                for t in задачи_фона[:-1]:
                    t.cancel()
        asyncio.run(прогон())
        log.info("обработано сигналов: %d, к покупке: %d",
                 детектор.обработано, детектор.к_покупке)
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
