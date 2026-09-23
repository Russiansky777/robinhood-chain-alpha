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

# Порог DBot: targetMinAmountUI = 2 (SOL-эквивалент). Верхней границы
# нет: targetMaxAmountUI = null в обеих задачах.
ПОРОГ_ВХОДА_SOL = ST.env_float("BLOOM_MIN_TARGET_SOL", 2.0)
МАКС_ОТСТАВАНИЕ_СЛОТОВ = ST.env_int("BLOOM_STALE_SLOTS", 3)
КУРС_TTL_S = ST.env_float("BLOOM_RATE_TTL_S", 60.0)


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
    out = {"нативный_delta_sol": 0.0, "по_минтам": {}, "плательщик": False}

    pre_n = meta.get("preBalances") or []
    post_n = meta.get("postBalances") or []
    if кошелёк in ключи:
        i = ключи.index(кошелёк)
        if i < len(pre_n) and i < len(post_n):
            d = int(post_n[i]) - int(pre_n[i])
            if i == 0:
                out["плательщик"] = True
                d += int(meta.get("fee") or 0)
            out["нативный_delta_sol"] = d / LAMPORT

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
            з = out["по_минтам"].setdefault(
                m, {"pre_raw": 0, "post_raw": 0, "decimals": ui.get("decimals"),
                    "программа": b.get("programId"), "было_в_pre": False})
            з[куда] = з[куда] + raw
            if куда == "pre_raw":
                з["было_в_pre"] = True
            if з.get("decimals") is None:
                з["decimals"] = ui.get("decimals")
            if not з.get("программа"):
                з["программа"] = b.get("programId")

    свод(meta.get("preTokenBalances"), "pre_raw")
    свод(meta.get("postTokenBalances"), "post_raw")

    for m, з in out["по_минтам"].items():
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


def сигнал_из_транзакции(tx: dict, источник: str, *, подпись: str,
                          слот: int | None = None) -> dict:
    """Чистый разбор: что именно сделал источник. Без сети и состояния."""
    meta = (tx or {}).get("meta") or {}
    сиг = {"подпись": подпись, "источник": источник,
            "слот": слот if slot_ok(слот) else (tx or {}).get("slot"),
            "тип": None, "минт": None, "трата": None, "трата_минт": None,
            "трата_ui": None, "первый_вход": None, "программы_dex": программы_dex(tx),
            "программа_токена": None, "решение_причина": None}

    if meta.get("err") is not None:
        сиг["тип"] = "fail"
        сиг["решение_причина"] = "транзакция источника с ошибкой"
        return сиг

    б = балансы_кошелька(tx, источник)
    сиг["нативный_delta_sol"] = б["нативный_delta_sol"]

    вошли = [(m, з) for m, з in б["по_минтам"].items()
              if m not in КОТИРОВОЧНЫЕ and (з["delta_raw"] or 0) > 0]
    вышли = [(m, з) for m, з in б["по_минтам"].items()
              if m not in КОТИРОВОЧНЫЕ and (з["delta_raw"] or 0) < 0]

    # трата в котировочных: WSOL и нативный SOL считаются вместе
    sol_ушло = -min(0.0, б["нативный_delta_sol"])
    wsol = б["по_минтам"].get(WSOL)
    if wsol and (wsol.get("delta_ui") or 0) < 0:
        sol_ушло += -wsol["delta_ui"]
    стабиль_ушло = {}
    for m in СТАБИЛЬНЫЕ:
        з = б["по_минтам"].get(m)
        if з and (з.get("delta_ui") or 0) < 0:
            стабиль_ушло[m] = -з["delta_ui"]

    if len(вошли) > 1:
        сиг["тип"] = "ambiguous"
        сиг["решение_причина"] = (f"в транзакции выросло {len(вошли)} некотировочных "
                                   f"минтов -- какой из них покупка, из балансов не видно")
        return сиг

    if вошли:
        m, з = вошли[0]
        сиг["тип"] = "buy"
        сиг["минт"] = m
        сиг["первый_вход"] = not з["было_в_pre"] or з["pre_raw"] == 0
        сиг["программа_токена"] = з.get("программа")
        сиг["получено_ui"] = з.get("delta_ui")
        if sol_ушло > 0:
            сиг["трата_минт"] = WSOL
            сиг["трата_ui"] = sol_ушло
            сиг["трата"] = sol_ушло          # уже в SOL
        elif стабиль_ушло:
            m2 = max(стабиль_ушло, key=lambda k: стабиль_ушло[k])
            сиг["трата_минт"] = m2
            сиг["трата_ui"] = стабиль_ушло[m2]
            сиг["трата"] = None              # нужен курс
        else:
            сиг["тип"] = "ambiguous"
            сиг["решение_причина"] = ("минт вырос, но ни SOL, ни стейблов не потрачено -- "
                                       "это не покупка за котировочный актив")
        return сиг

    if вышли:
        сиг["тип"] = "sell"
        сиг["минт"] = вышли[0][0]
        сиг["решение_причина"] = "продажа источника"
        return сиг

    сиг["тип"] = "other"
    сиг["решение_причина"] = "ни один некотировочный минт не изменился"
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
            self.отказы = [{"источник": "нет requests"}]
            return None
        for имя, fn in (("geckoterminal", self._gecko), ("jupiter", self._jup)):
            try:
                v = fn()
            except Exception as exc:  # noqa: BLE001
                self.отказы.append({"источник": имя, "ошибка": f"{type(exc).__name__}: {str(exc)[:120]}"})
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
    if сигнал.get("трата") is not None:
        return float(сигнал["трата"]), "трата в SOL/WSOL, курс не нужен"
    if сигнал.get("трата_минт") in СТАБИЛЬНЫЕ and сигнал.get("трата_ui"):
        if not курс_usd or курс_usd <= 0:
            return None, "курс SOL/USD недоступен"
        return float(сигнал["трата_ui"]) / float(курс_usd), f"по курсу {курс_usd:.2f} USD/SOL"
    return None, "трата не определена"


# --------------------------------------------------------------- решение

def фильтры_dbot(сигнал: dict, трата_sol: float | None, *,
                  порог_sol: float = ПОРОГ_ВХОДА_SOL) -> tuple[bool, str, str]:
    """Воспроизведение фильтров задач BATCH-5/BATCH-3.

    Порядок сознательно совпадает с тем, что видно в follow_trades:
    сначала «не покупка», потом размер, потом докупка.
    """
    т = сигнал.get("тип")
    if т == "sell":
        return False, КОД_НЕ_ПОКУПКА, ("продажа источника: sellSettings.mode=only_pnl, "
                                        "DBot копирует только покупки")
    if т != "buy":
        return False, (КОД_НЕЯСНО if т == "ambiguous" else
                        КОД_ОШИБКА_ЦЕПИ if т == "fail" else КОД_НЕ_ПОКУПКА), \
            сигнал.get("решение_причина") or f"тип сигнала {т}"
    if трата_sol is None:
        return False, КОД_НЕТ_КУРСА, ("трата источника в стейблах, а курса SOL нет -- "
                                        "порог в SOL-эквиваленте не проверить")
    if трата_sol < порог_sol:
        return False, КОД_МАЛО, (f"вход источника {трата_sol:.3f} SOL-эквивалента "
                                   f"меньше targetMinAmountUI={порог_sol}")
    if сигнал.get("первый_вход") is False:
        return False, КОД_ДОКУПКА, "источник докупает уже имеющийся токен (skipTargetIncreasePosition=true)"
    if сигнал.get("первый_вход") is None:
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
    строка = {"подпись": сигнал.get("подпись"), "источник": сигнал.get("источник"),
               "минт": сигнал.get("минт"), "слот": сигнал.get("слот"),
               "тип": сигнал.get("тип"), "трата_sol_экв": трата_sol,
               "трата_минт": сигнал.get("трата_минт"), "трата_ui": сигнал.get("трата_ui"),
               "первый_вход": сигнал.get("первый_вход"),
               "программы_dex": сигнал.get("программы_dex"),
               "программа_токена": сигнал.get("программа_токена")}

    отставание = None
    if slot_ok(текущий_слот) and slot_ok(сигнал.get("слот")):
        отставание = текущий_слот - сигнал["слот"]
    строка["отставание_слотов"] = отставание
    if отставание is not None and отставание > макс_отставание:
        строка.update({"действие": "пропуск", "код": КОД_УСТАРЕЛ,
                        "причина": (f"сигнал отстал на {отставание} слотов при пороге "
                                    f"{макс_отставание} -- цена уже не та")})
        return строка

    ок, код, причина = фильтры_dbot(сигнал, трата_sol, порог_sol=порог_sol)
    if not ок:
        строка.update({"действие": "пропуск", "код": код, "причина": причина,
                        "фильтр": "задача DBot"})
        return строка

    можно, почему, код2 = состояние.can_open_detailed(
        mint=сигнал["минт"], source_sig=сигнал["подпись"], balance_sol=баланс_sol)
    if not можно:
        строка.update({"действие": "пропуск", "код": код2, "причина": почему,
                        "фильтр": "наш лимит",
                        "dbot_бы_купил": True})
        return строка

    строка.update({"действие": "покупка", "код": КОД_КУПИТЬ,
                    "причина": "фильтры задачи и наши лимиты пройдены"})
    return строка


# ------------------------------------------------------------------- RPC

class Helius:
    def __init__(self, key: str | None = None) -> None:
        self.key = key or os.environ.get("HELIUS_API_KEY") or ""
        self.url = f"https://mainnet.helius-rpc.com/?api-key={self.key}"
        self.вызовов = 0
        self._кеш_минтов: dict = {}

    def call(self, метод: str, параметры: list, *, таймаут: float = 10.0):
        if requests is None:
            raise RuntimeError("нет requests")
        self.вызовов += 1
        r = requests.post(self.url, json={"jsonrpc": "2.0", "id": 1,
                                           "method": метод, "params": параметры},
                           timeout=таймаут)
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
                    "maxSupportedTransactionVersion": 1}])
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
        out = {"минт": минт, "программа_токена": None, "ставка_bps": None,
                "таксируемый": None}
        try:
            r = self.call("getAccountInfo", [минт, {"encoding": "jsonParsed"}])
        except RuntimeError as exc:
            out["почему"] = str(exc)[:120]
            return out                      # НЕ кешируем неудачу
        val = (r or {}).get("value") or {}
        out["программа_токена"] = val.get("owner")
        info = (((val.get("data") or {}).get("parsed") or {}).get("info") or {})
        out["десятичных"] = info.get("decimals")
        for e in info.get("extensions") or []:
            if isinstance(e, dict) and e.get("extension") == "transferFeeConfig":
                st = (e.get("state") or {})
                out["ставка_bps"] = (st.get("newerTransferFee") or {}).get("transferFeeBasisPoints")
        out["таксируемый"] = bool(out.get("ставка_bps"))
        self._кеш_минтов[минт] = out
        return out


# --------------------------------------------------------------- источники

def источники_из_конфига(конфиг: dict, задачи: tuple) -> dict:
    """{адрес: имя задачи} из ответа GET /automation/follow_orders.

    Берётся targetIds -- это и есть адреса источников задачи (проверено
    на снимке data/final/.../konfig.json, где targetIds у BATCH-5 --
    девять base58-адресов, а targetNames -- их прозвища).
    """
    out = {}
    res = ((конфиг or {}).get("тело") or конфиг or {}).get("res") or []
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
                  курс: КурсSOL, режим: str = "dry") -> None:
        self.источники = источники
        self.состояние = состояние
        self.helius = helius
        self.курс = курс
        self.режим = режим
        self.обработано = 0
        self.к_покупке = 0
        self.видели: set = set()
        self.последний_слот: int | None = None

    def обработать(self, подпись: str, слот: int | None, источник: str | None,
                    как: str, tx: dict | None = None) -> dict | None:
        """Один сигнал: достать транзакцию, разобрать, решить, записать."""
        if подпись in self.видели:
            return None
        self.видели.add(подпись)
        if not источник:
            return None
        self.обработано += 1

        if tx is None:
            tx = self.helius.транзакция(подпись)
        if tx is None:
            строка = {"подпись": подпись, "источник": источник, "слот": слот,
                       "действие": "пропуск", "код": КОД_НЕ_ДОСТАЛИ, "как": как,
                       "причина": "getTransaction не отдал транзакцию за отведённые попытки"}
            self.состояние.log_decision(строка)
            return строка

        сиг = сигнал_из_транзакции(tx, источник, подпись=подпись, слот=слот)
        трата, пояснение = в_sol(сиг, self.курс.получить())
        текущий = self.helius.слот() if сиг.get("тип") == "buy" else None
        баланс = (self.helius.баланс_sol(ST.EXECUTOR_WALLET)
                   if сиг.get("тип") == "buy" else None)
        строка = решение(сиг, состояние=self.состояние, трата_sol=трата,
                          баланс_sol=баланс, текущий_слот=текущий)
        строка["как"] = как
        строка["курс_пояснение"] = пояснение
        строка["курс_источник"] = self.курс.источник
        строка["задача_источника"] = self.источники.get(источник)
        строка["режим"] = self.режим
        if строка.get("действие") == "покупка":
            налог = self.helius.налог_минта(строка["минт"])
            строка["таксируемый"] = налог.get("таксируемый")
            строка["ставка_налога_bps"] = налог.get("ставка_bps")
            self.к_покупке += 1
        self.состояние.log_decision(строка)
        return строка


async def _подписка_транзакций(ws, адреса: list) -> None:
    for i, a in enumerate(адреса, 1):
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": i, "method": "transactionSubscribe",
            "params": [{"accountInclude": [a], "failed": False, "vote": False},
                        {"commitment": "processed", "transactionDetails": "full",
                         "encoding": "jsonParsed", "showRewards": False,
                         "maxSupportedTransactionVersion": 1}]}))


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
        метод = "transactionSubscribe" if atlas else "logsSubscribe"
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
                async for raw in ws:
                    if дедлайн and time.time() > дедлайн:
                        return
                    msg = json.loads(raw)
                    if "id" in msg and ("result" in msg or "error" in msg):
                        if "error" in msg:
                            отказы.append(msg["error"])
                        else:
                            подтверждено += 1
                            a = id_адреса.get(msg["id"])
                            if a and isinstance(msg.get("result"), int):
                                подписка_адреса[msg["result"]] = a
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
                                                     источник, "logsSubscribe", None)
                        continue
                    sig, слот = подпись_и_слот(res)
                    tx = res.get("transaction") if isinstance(res.get("transaction"), dict) else None
                    if tx is not None and "meta" not in tx:
                        tx = None            # пришла форма без meta -- добираем по RPC
                    if sig:
                        await asyncio.to_thread(детектор.обработать, sig, слот,
                                                 источник, метод, tx)
                backoff = 1.0
                raise RuntimeError(f"{метод}: сервер закрыл соединение")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("подписка (%s) оборвалась: %s: %s", метод,
                        type(exc).__name__, str(exc)[:200])
            if atlas:
                log.warning("перехожу на запасной logsSubscribe")
                atlas = False
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


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
    chk("покупка распознана", s["тип"] == "buy", s["тип"])
    chk("минт покупки верный", s["минт"] == "MINTA", s["минт"])
    chk("первый вход виден", s["первый_вход"] is True, s["первый_вход"])
    chk("трата в USDC = 500", abs((s["трата_ui"] or 0) - 500) < 1e-9, s["трата_ui"])
    chk("в SOL без курса не пересчитывается", s["трата"] is None, s["трата"])
    v, _ = в_sol(s, 200.0)
    chk("500 USDC при 200 USD/SOL = 2.5 SOL", abs(v - 2.5) < 1e-9, v)
    v2, поч = в_sol(s, None)
    chk("без курса -- None, а не догадка", v2 is None, поч)

    # 2. докупка: минт был в pre
    t2 = tx(pre=[бал(USDC, 600_000000), бал("MINTA", 1_000000, idx=2)],
             post=[бал(USDC, 100_000000), бал("MINTA", 6_000000, idx=2)])
    s2 = сигнал_из_транзакции(t2, "SRC", подпись="SIG2")
    chk("докупка: первый_вход False", s2["первый_вход"] is False, s2["первый_вход"])
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
    chk("продажа распознана", s3["тип"] == "sell", s3["тип"])
    ок, код, поч = фильтры_dbot(s3, 5.0)
    chk("продажа не копируется", (not ок) and код == КОД_НЕ_ПОКУПКА, код)
    chk("причина называет only_pnl", "only_pnl" in поч, поч)

    # 5. покупка за нативный SOL, комиссия не считается тратой
    t4 = tx(pre=[], post=[бал("MINTB", 7_000000, idx=2)],
             native=(3 * LAMPORT, 1 * LAMPORT), fee=5000, ключи=("SRC",))
    s4 = сигнал_из_транзакции(t4, "SRC", подпись="SIG4")
    chk("трата в SOL посчитана", abs(s4["трата"] - (2 - 5000 / LAMPORT)) < 1e-12, s4["трата"])
    chk("комиссия вычтена из траты", s4["трата"] < 2.0, s4["трата"])
    chk("для SOL курс не нужен", в_sol(s4, None)[0] is not None, в_sol(s4, None))

    # 6. WSOL считается вместе с нативным
    t5 = tx(pre=[бал(WSOL, 3 * LAMPORT, dec=9)], post=[бал("MINTC", 1, idx=2)])
    s5 = сигнал_из_транзакции(t5, "SRC", подпись="SIG5")
    chk("WSOL-трата = 3 SOL", abs(s5["трата"] - 3.0) < 1e-9, s5["трата"])

    # 7. ошибка цепи
    t6 = tx(pre=[], post=[], err={"InstructionError": [0, "X"]})
    s6 = сигнал_из_транзакции(t6, "SRC", подпись="SIG6")
    chk("неуспешная транзакция -- не сигнал", s6["тип"] == "fail", s6["тип"])

    # 8. два выросших минта -- неоднозначно, а не угадываем
    t7 = tx(pre=[бал(USDC, 600_000000)],
             post=[бал(USDC, 0), бал("M1", 1, idx=2), бал("M2", 1, idx=3)])
    s7 = сигнал_из_транзакции(t7, "SRC", подпись="SIG7")
    chk("две покупки в одной транзакции -- ambiguous", s7["тип"] == "ambiguous", s7["тип"])
    ок, код, _ = фильтры_dbot(s7, 3.0)
    chk("ambiguous не исполняется", (not ок) and код == КОД_НЕЯСНО, код)

    # 9. чужой кошелёк в транзакции не путает разбор
    t8 = tx(pre=[бал(USDC, 600_000000, owner="OTHER")],
             post=[бал("MINTA", 5_000000, owner="OTHER", idx=2)])
    s8 = сигнал_из_транзакции(t8, "SRC", подпись="SIG8")
    chk("чужие балансы не считаются нашими", s8["тип"] == "other", s8["тип"])

    # 10. программы DEX -- факт, а не догадка
    t9 = tx(pre=[бал(USDC, 600_000000)], post=[бал(USDC, 0), бал("MINTA", 5_000000, idx=2)],
             инстр=("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "НЕИЗВЕСТНАЯ"))
    s9 = сигнал_из_транзакции(t9, "SRC", подпись="SIG9")
    chk("известная программа DEX названа", s9["программы_dex"] == ["Raydium AMM v4"], s9["программы_dex"])
    chk("неизвестная программа не выдумывается", "НЕИЗВЕСТНАЯ" not in str(s9["программы_dex"]))
    chk("адрес пула не придумывается", "пул" not in s9 and s9.get("pool") is None)

    # 11. Token-2022 виден из балансов
    t10 = tx(pre=[бал(USDC, 600_000000)],
              post=[бал(USDC, 0), бал("MINTA", 5_000000, idx=2, prog=TOKEN_2022)])
    s10 = сигнал_из_транзакции(t10, "SRC", подпись="SIG10")
    chk("программа токена определена", s10["программа_токена"] == TOKEN_2022, s10["программа_токена"])

    # 12. источники из конфига
    конфиг = {"тело": {"res": [
        {"name": "BATCH-5", "enabled": True, "targetIds": ["Hn5gVKAApv69t5HX7Q77uX7o5ayhEwArgYx7kukMLVGn"]},
        {"name": "BATCH-3", "enabled": True, "targetIds": ["BMgsHTvcasRVtuevHJh8t6Vf5dmcWkDLAx6gSAQ3dsYm"]},
        {"name": "BATCH-7", "enabled": True, "targetIds": ["4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8"]},
        {"name": "BATCH-5", "enabled": False, "targetIds": ["ZZZZ"]}]}}
    ист = источники_из_конфига(конфиг, ("BATCH-5", "BATCH-3"))
    chk("взяты только нужные задачи", len(ист) == 2, ист)
    chk("BATCH-7 не попал", all(v != "BATCH-7" for v in ист.values()), ист)

    # 13. решение с настоящим состоянием
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "kill")
        r = решение(s, состояние=st, трата_sol=2.5, баланс_sol=3.0, текущий_слот=101)
        chk("решение -- покупка", r["действие"] == "покупка", r)
        chk("код покупки", r["код"] == КОД_КУПИТЬ, r["код"])
        r2 = решение(s, состояние=st, трата_sol=2.5, баланс_sol=3.0, текущий_слот=105)
        chk("устаревший сигнал отсекается", r2["код"] == КОД_УСТАРЕЛ, r2["код"])
        chk("устаревание проверяется до наших лимитов", r2.get("фильтр") is None, r2.get("фильтр"))
        r3 = решение(s, состояние=st, трата_sol=2.5, баланс_sol=0.05, текущий_слот=101)
        chk("нехватка баланса -- наш лимит", r3["код"] == ST.КОД_БАЛАНС, r3["код"])
        chk("помечено, что DBot бы купил", r3.get("dbot_бы_купил") is True, r3.get("dbot_бы_купил"))

        # дубль по минту -- отдельный код, а не «расхождение»
        st.write_intent(client_order_id="c1", mint="MINTA", source_sig="ДРУГАЯ",
                         source_slot=1, sol_in=0.2, pool=None, program=None,
                         taxed=None, tax_bps=None, mode="dry", sell_after_s=28.8)
        r4 = решение(s, состояние=st, трата_sol=2.5, баланс_sol=3.0, текущий_слот=101)
        chk("дубль по минту -- SKIPPED_DUP_MINT", r4["код"] == "SKIPPED_DUP_MINT", r4["код"])
        chk("дубль помечен как наш лимит", r4.get("фильтр") == "наш лимит", r4.get("фильтр"))

        # рубильник сильнее всего остального
        st.kill_path.write_text("стоп", encoding="utf-8")
        r5 = решение(s, состояние=st, трата_sol=2.5, баланс_sol=3.0, текущий_слот=101)
        chk("рубильник запрещает покупку", r5["действие"] == "пропуск", r5)
        chk("код рубильника", r5["код"] == ST.КОД_РУБИЛЬНИК, r5["код"])

    # 14. курс: без сети не выдумывается
    к = КурсSOL(ttl_s=1.0)
    chk("свежести нет у пустого курса", not к.свежий())
    к.значение, к.когда, к.источник = 200.0, time.time(), "тест"
    chk("свежий курс отдаётся", к.получить() == 200.0, к.значение)

    # 15. slot_ok
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
    источники = загрузить_источники(Path(a.config), задачи)
    состояние = ST.ExecState()
    helius = Helius()
    курс = КурсSOL()
    детектор = Детектор(источники=источники, состояние=состояние,
                         helius=helius, курс=курс,
                         режим=os.environ.get("BLOOM_MODE", "dry"))

    if a.sig:
        источник = a.source
        if not источник:
            print("для --sig нужен --source: без него неизвестно, чьи балансы смотреть")
            return 2
        r = детектор.обработать(a.sig, None, источник, "вручную", None)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    if a.serve:
        log.info("источников: %d (%s)", len(источники), ", ".join(sorted(set(источники.values()))))
        asyncio.run(слушать(детектор, helius.key, стоп_через_s=a.seconds))
        log.info("обработано сигналов: %d, к покупке: %d",
                 детектор.обработано, детектор.к_покупке)
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
