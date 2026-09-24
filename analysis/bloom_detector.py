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
        self.ног_первые: list = []          # первые события -- на сверку с ingest_stats
        # ЦЕНА ЗАМЕРА ОТДЕЛЬНОЙ СТРОКОЙ. Замер 24.09, первые две минуты
        # работы: четыре котировочных пула дали 421 уведомление и около
        # 17.8 МБ -- это ~535 МБ и ~10 000 кредитов в час, против ~390
        # кредитов в час на всех двадцати источниках вместе. Смешивать их с
        # расходом на торговлю нельзя: источники -- это сделки, а это замер,
        # и при перерасходе выключать надо именно замер.
        self.ног_байт = 0
        self.ног_кредитов = 0
        self.ног_бюджет = ST.env_int("BLOOM_SHADOW_LEGS_CREDITS", 30000)
        self.ног_отключён = False
        self.ног_отключён_почему = ""
        if self.тень_включена and ST.env_int("BLOOM_SHADOW_LEGS", 1) == 1:
            try:
                пулы = SB.load_leg_pools()
                self.кэш_ног = SB.LegCache(пулы, self.helius.call)
                self.кэш_ног_адреса = {п["q_vault"]: q for q, п in пулы.items()}
            except Exception as exc:  # noqa: BLE001
                self.кэш_ног_почему = f"{type(exc).__name__}: {str(exc)[:160]}"
        self._последняя_тревога_разбора = 0.0
        self.способ: str | None = None
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

    def признак_жизни(self) -> dict:
        st = {ST.SCHEMA_VERSION_KEY: ST.SCHEMA_VERSION,
               "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "updated_ts": time.time(),
               "alive_since_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.старт)),
               "mode": self.режим_денег(),
               "sources": len(self.источники),
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
        st["shadow"] = {"enabled": self.тень_включена, "built": self.теней,
                         "would_pass": self.теней_прошло,
                         "over_cap": self.теней_дорогих,
                         "failed": self.теней_упало,
                         "module_loaded": SB is not None,
                         "module_why_not": globals().get("SHADOW_IMPORT_ERR", "")}
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
        """Порог входа: у тестового источника свой, у остальных боевой."""
        return TEST_MIN_SOL if self.это_тестовый(источник) else ПОРОГ_ВХОДА_SOL

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
        if строка.get("action") == "buy" and self.исполнитель is not None:
            # ТЕНЬ -- ДО вызова Bloom, но в СВОЁМ потоке и без ожидания:
            # запускается и тут же отпускается, Bloom уходит следующей
            # строкой. Ставить её после отправки нельзя -- тогда она мерила
            # бы уже другое состояние пула.
            self.запустить_тень(строка, tx)
            try:
                итог = self.исполнитель.execute(строка, balance_sol=self.свежий_баланс())
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
                        tax_bps=налог.get("fee_bps"))
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
                exec_row=итог, слот_источника=строка.get("source_slot"))

        # Строка в Telegram -- ТОЛЬКО по тестовому источнику и только после
        # журнала и отправки ордера. Боевые источники молчат: сто решений в
        # час в телефоне не читает никто, а стенд владелец смотрит живьём.
        if self.оповещатель is not None and (строка.get("test_source")
                                             or NT.боевые_тоже()):
            self.оповещатель.послать(NT.строка_решения(строка))
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
        self.ног_кредитов = -(-self.ног_байт // ПОРЦИЯ) * 2
        if self.ног_бюджет > 0 and self.ног_кредитов >= self.ног_бюджет:
            self.ног_отключён = True
            self.ног_отключён_почему = (
                f"бюджет замера исчерпан: {self.ног_кредитов} кредитов из "
                f"{self.ног_бюджет} за {self.ног_байт // 1048576} МБ")
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
                 "bytes": self.ног_байт,
                 "credits": self.ног_кредитов,
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
                self.helius.call,
                sol_usd=курс,
                spend_sol_equiv=строка.get("spend_sol"),
                slippage=(float(self.исполнитель.slippage_pct) / 100.0
                           if self.исполнитель is not None else 0.35),
                leg_cache=self.кэш_ног)
            запись.update(res or {})
            self.теней += 1
            if (res or {}).get("sim_verdict") == "would_pass":
                self.теней_прошло += 1
            if (res or {}).get("would_skip_cap"):
                self.теней_дорогих += 1
        except Exception as exc:  # noqa: BLE001
            запись["why_not"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            self.теней_упало += 1
        запись["shadow_total_ms"] = round((time.time() - t0) * 1000.0, 2)
        try:
            self.состояние.log_decision(запись)
        except Exception:  # noqa: BLE001
            log.exception("запись тени в журнал не легла")

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
            self.состояние.update_position(
                cid, own_tx_seen_ts=round(t_recv, 6),
                own_tx_seen_ms=запись.get("own_tx_seen_ms"),
                bloom_to_seen_ms=запись.get("bloom_to_seen_ms"),
                own_tx_seen_slot=слот,
                own_tx_seen_chain_ok=запись.get("chain_ok"))
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
                                слот_источника: int | None = None) -> dict:
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
                    размер_sol=размер))
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
            self.состояние.update_position(
                cid, our_pool=наш["pool"], our_pool_direct=наш["direct"],
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
                слот_источника=слот_источника, размер_sol=размер, метки=флаги))
        return запись


def адреса_подписки(детектор: "Детектор") -> list:
    """Полный список адресов подписки одной строкой, чтобы его можно было
    проверить без сети.

    Три разных назначения в одном списке, и путать их нельзя:
      * источники -- сигналы, по ним покупаем;
      * наш кошелёк -- только замер круга;
      * хранилища четырёх котировочных пулов -- только корм кэшу шаблонов
        первого шага для двухшаговой тени.
    Подписка и так идёт по адресу на запрос, поэтому каждый адрес приходит
    со своим номером подписки, и спутать назначение невозможно.
    """
    хранилища = (set() if getattr(детектор, "ног_отключён", False)
                  else set(детектор.кэш_ног_адреса))
    return sorted(set(детектор.источники) | {ST.EXECUTOR_WALLET} | хранилища)


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
                async for raw in ws:
                    if дедлайн and time.time() > дедлайн:
                        return
                    if детектор.поколение != поколение:
                        log.info("список источников изменился -- переподписываюсь")
                        break
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
            # transactionSubscribe -- ОСНОВНОЙ способ: он несёт всю
            # транзакцию (разбор без RPC, отставание 0 слотов) и приходит
            # даже чуть раньше логов. logsSubscribe -- запасной НА ОДНУ
            # попытку: после неё снова пробуем основной, иначе один обрыв
            # навсегда сажал бы нас на путь, где каждый сигнал стоит слота.
            if atlas:
                log.warning("перехожу на запасной logsSubscribe на одну попытку")
                atlas = False
                await asyncio.sleep(1)
                continue
            atlas = True
            log.info("возвращаюсь на основной transactionSubscribe")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


ПУЛЬС_S = ST.env_float("BLOOM_DETECTOR_PULSE_S", 60.0)
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
            детектор.признак_жизни()
        except Exception as exc:  # noqa: BLE001
            log.warning("признак жизни не записался: %s: %s",
                        type(exc).__name__, str(exc)[:160])
        await asyncio.sleep(ПУЛЬС_S)


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

def self_test() -> int:
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
    with tempfile.TemporaryDirectory() as d:
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
    with tempfile.TemporaryDirectory() as d:
        снимок = Path(d) / "konfig.json"
        снимок.write_text(json.dumps(конфиг, ensure_ascii=False), encoding="utf-8")
        было = os.environ.pop("DBOT_API_KEY", None)
        try:
            ист, откуда = источники(("BATCH-5", "BATCH-3"), снимок)
            chk("без ключа DBot берётся снимок", len(ист) == 2 and "снимок" in откуда, откуда)
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

    # 15b. ветка "решение -- покупка" в обработать(): именно она сломалась
    # молча при переводе ключей на ASCII, потому что её не покрывал ни один
    # тест. Сравнение шло со строкой, которую переименовали в другом месте.
    with tempfile.TemporaryDirectory() as d:
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
        chk("налог минта спрошен именно в этой ветке",
            h.спрошено == ["MINT_OK"], h.спрошено)
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
    with tempfile.TemporaryDirectory() as d:
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
        finally:
            globals()["TEST_SOURCES"] = было

    # 15c. исполнитель подключён: решение о покупке доходит до него,
    # его падение НЕ валит детектор, а решение всё равно остаётся в журнале
    with tempfile.TemporaryDirectory() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "kill")

        class ИсполнительЗаглушка:
            def __init__(self, падать=False):
                self.вызовы = []
                self.падать = падать

            def execute(self, решение, *, balance_sol):
                self.вызовы.append((решение.get("signature"), balance_sol))
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
    with tempfile.TemporaryDirectory() as d_к:
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

    with tempfile.TemporaryDirectory() as d:
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
    with tempfile.TemporaryDirectory() as d:
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

            def execute(self, решение_вх, *, balance_sol):
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
    with tempfile.TemporaryDirectory() as d:
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
        if SB is not None:
            chk("кэш шаблонов первого шага поднялся",
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
        with tempfile.TemporaryDirectory() as d:
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
    with tempfile.TemporaryDirectory() as d:
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
    with tempfile.TemporaryDirectory() as d:
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
    with tempfile.TemporaryDirectory() as d:
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
            chk("бюджет службы -- тот, что назвал владелец (150 000 с 24.09)",
                день["bloom_detector"].get("бюджет_за_день") == 150_000,
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
