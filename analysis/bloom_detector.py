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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_exec_state as ST  # noqa: E402

try:
    import bloom_executor as EXEC
except ImportError:  # pragma: no cover
    EXEC = None

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


def маршрут_из_транзакции(tx: dict, *, минт_покупки: str | None,
                           трата_минт: str | None) -> dict:
    """Маршрут свопа источника: программы, хопы, промежуточные минты.

    Как считается и почему именно так. Пулы держат токены на своих
    счетах, поэтому КАЖДЫЙ минт маршрута появляется в
    pre/postTokenBalances транзакции -- даже тот, через который просто
    прошли. Значит промежуточные минты -- это все минты транзакции, кроме
    котировочных и кроме купленного. Ровно этот приём вскрыл -13.4% на
    SANTA: маршрут шёл через таксируемый промежуточный токен, и комиссия
    на перевод бралась на каждой ноге.

    Два измерения хопов даются отдельно, потому что это РАЗНЫЕ вещи:
      * минтов_в_маршруте - 1 -- оценка по числу задействованных токенов;
      * вызовов_dex -- сколько раз вызваны известные программы DEX.
    Ни одно из них не выдаётся за "точное число хопов".

    Оговорка, которую нельзя прятать: если источник в одной транзакции
    сделал несколько свопов, минты параллельной ноги попадут в
    промежуточные. Это всё равно многохоповый маршрут, но называть его
    "один своп через промежуточный токен" было бы неверно.
    """
    meta = (tx or {}).get("meta") or {}
    минты = set()
    for где in ("preTokenBalances", "postTokenBalances"):
        for b in meta.get(где) or []:
            if isinstance(b, dict) and b.get("mint"):
                минты.add(b["mint"])
    промежуточные = sorted(минты - set(КОТИРОВОЧНЫЕ) - {минт_покупки or ""})

    вызовов = 0
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
             "all_tx_mints": sorted(минты)}


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
        if sol_ушло > 0:
            сиг["spend_mint"] = WSOL
            сиг["spend_ui"] = sol_ушло
            сиг["spend"] = sol_ушло          # уже в SOL
        elif стабиль_ушло:
            m2 = max(стабиль_ушло, key=lambda k: стабиль_ушло[k])
            сиг["spend_mint"] = m2
            сиг["spend_ui"] = стабиль_ушло[m2]
            сиг["spend"] = None              # нужен курс
        else:
            сиг["kind"] = "received"
            сиг["decide_reason"] = (
                "минт вырос, но источник не отдал ни SOL, ни стейблов "
                f"(нативная дельта {б['native_delta_sol']:+.9f} SOL): это "
                "получение токена, а не покупка -- копировать нечего")
        сиг["route"] = маршрут_из_транзакции(
            tx, минт_покупки=сиг["mint"], трата_минт=сиг.get("spend_mint"))
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


def в_sol(сигнал: dict, курс_usd: float | None) -> tuple[float | None, str]:
    """Трата источника в SOL-эквиваленте. Возвращает (сумма, пояснение)."""
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
        return False, КОД_НЕТ_КУРСА, ("трата источника в стейблах, а курса SOL нет -- "
                                        "порог в SOL-эквиваленте не проверить")
    if трата_sol < порог_sol:
        return False, КОД_МАЛО, (f"вход источника {трата_sol:.3f} SOL-эквивалента "
                                   f"меньше targetMinAmountUI={порог_sol}")
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


class Helius:
    def __init__(self, key: str | None = None, служба: str = "bloom_detector") -> None:
        self.key = key or os.environ.get("HELIUS_API_KEY") or ""
        self.url = f"https://mainnet.helius-rpc.com/?api-key={self.key}"
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
            r = requests.post(self.url, json={"jsonrpc": "2.0", "id": 1,
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
        j = r.json() or {}
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
        info = (((val.get("data") or {}).get("parsed") or {}).get("info") or {})
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

    def статус_путь(self) -> Path:
        return self.состояние.base / "detector_status.json"

    def признак_жизни(self) -> dict:
        st = {ST.SCHEMA_VERSION_KEY: ST.SCHEMA_VERSION,
               "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "updated_ts": time.time(),
               "alive_since_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.старт)),
               "mode": self.режим,
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
        трата, пояснение = в_sol(сиг, self.курс.значение if self.курс.свежий() else None)
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
        строка["rate_source"] = self.курс.источник
        строка["source_task"] = self.источники.get(источник)
        строка["mode"] = self.режим
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
        return строка


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
        адреса = sorted(детектор.источники)
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
                    if m == "logsNotification":
                        val = res.get("value") or {}
                        if val.get("err") is not None:
                            continue
                        sig = val.get("signature")
                        слот = (res.get("context") or {}).get("slot")
                        if isinstance(sig, str) and SIG_RE.match(sig):
                            await asyncio.to_thread(детектор.обработать, sig,
                                                     слот if isinstance(слот, int) else None,
                                                     источник, "logsSubscribe", None, t_получено)
                        continue
                    sig, слот = подпись_и_слот(res)
                    tx = res.get("transaction") if isinstance(res.get("transaction"), dict) else None
                    if tx is not None and "meta" not in tx:
                        tx = None            # пришла форма без meta -- добираем по RPC
                    if sig:
                        await asyncio.to_thread(детектор.обработать, sig, слот,
                                                 источник, метод, tx, t_получено)
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
    chk("вход 1.9 SOL отсекается", (not ок) and код == КОД_МАЛО, код)
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

    # 16. маршрут: промежуточный токен виден, прямой -- нет
    def tx_маршрут(минты_пулов):
        pre = [бал(USDC, 600_000000)]
        post = [бал(USDC, 0), бал("КУПЛЕН", 5_000000, idx=2)]
        # балансы пулов -- чужие владельцы, но минты в транзакции есть
        for i, m in enumerate(минты_пулов, start=10):
            pre.append(бал(m, 1_000000, owner="ПУЛ", idx=i))
            post.append(бал(m, 2_000000, owner="ПУЛ", idx=i))
        return tx(pre=pre, post=post,
                   инстр=("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
                           "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"))

    s_прямой = сигнал_из_транзакции(tx_маршрут([]), "SRC", подпись="M1")
    chk("прямой маршрут: промежуточных нет",
        s_прямой["route"]["intermediate_mints"] == [], s_прямой["route"])
    chk("прямой маршрут не помечен как через промежуточный",
        s_прямой["route"]["via_intermediate"] is False)
    chk("вызовы DEX посчитаны", s_прямой["route"]["dex_calls"] == 2,
        s_прямой["route"]["dex_calls"])

    s_через = сигнал_из_транзакции(tx_маршрут(["ПРОМЕЖ"]), "SRC", подпись="M2")
    chk("промежуточный минт найден",
        s_через["route"]["intermediate_mints"] == ["ПРОМЕЖ"], s_через["route"])
    chk("помечен как через промежуточный",
        s_через["route"]["via_intermediate"] is True)
    chk("хопов больше, чем у прямого",
        s_через["route"]["hops_by_mints"]
        > s_прямой["route"]["hops_by_mints"],
        (s_через["route"]["hops_by_mints"],
         s_прямой["route"]["hops_by_mints"]))
    s_wsol = сигнал_из_транзакции(tx_маршрут([WSOL, USDT]), "SRC", подпись="M3")
    chk("котировочные минты не считаются промежуточными",
        s_wsol["route"]["intermediate_mints"] == [], s_wsol["route"])

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
            chk("бюджет службы -- тот, что назвал владелец (60 000)",
                день["bloom_detector"].get("бюджет_за_день") == 60_000,
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
