#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ТРЕКЕР КАПИТАЛА КОШЕЛЬКА ПОЛОСЫ: ряд по цепи, страница, отдача. Без ключей.

КОШЕЛЁК 4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x, только ПУБЛИЧНЫЕ данные
цепи. Ни одного ключа, ни одной подписи, ни одной отправки: модуль читает и
считает. Живёт на lab-miami -- на машине, где по слову владельца не должно быть
ни ключей кошельков, ни env полосы, ни торговых служб; этот модуль ни одного из
них не требует.

ПОЧЕМУ КАПИТАЛ, А НЕ ГОЛЫЙ SOL (слово владельца). Голый баланс даёт ПИЛУ:
покупка роняет его на билет, продажа поднимает, и график показывает ритм
торговли вместо денег. Капитал считается так, чтобы ПОКУПКА ЕГО НЕ ДВИГАЛА:

    капитал = SOL на кошельке
            + WSOL (завёрнутое)
            + открытые позиции ПО ЦЕНЕ ВХОДА
            + рента в токен-счетах (пустых и с остатком)

ТРИ ТОЖДЕСТВА, НА КОТОРЫХ ЭТО ДЕРЖИТСЯ, И КАЖДОЕ ПРОВЕРЕНО ЧИСЛОМ:
  * ПОКУПКА капитал не двигает. SOL ушёл -- появилась позиция по цене входа и
    рента нового счёта. Отсюда определение цены входа: это ВСЁ, что ушло из
    кошелька в транзакции покупки, МИНУС рента, осевшая в наших токен-счетах
    (она считается отдельной частью капитала, и считать её дважды нельзя).
    Комиссия, чаевые и приоритет при этом ВХОДЯТ в цену входа -- они из системы
    ушли, и увидеть их обязана продажа, а не покупка.
  * ПРОДАЖА двигает капитал РОВНО НА ИТОГ СДЕЛКИ: пришло SOL минус цена входа.
    Это то же число, что у учёта полосы в поле «итог_po_cepi_sol».
  * ЗАКРЫТИЕ СЧЁТА капитал не двигает: рента переезжает из счёта в SOL.

ПЕРЕВОДЫ -- ОТДЕЛЬНЫМИ ОТМЕТКАМИ (слово владельца). Ввод и вывод SOL не
являются торговым итогом, поэтому в изменение за период НЕ входят:

    изменение = (капитал_конец - капитал_начало) - (ввод - вывод за период)

Транзакция считается переводом ТОЛЬКО когда наши токеновые остатки и лампорты
наших токен-счетов не изменились вовсе. Всё, что не своп, не перевод и не
закрытие счёта, получает вид «иное» И НАЗЫВАЕТСЯ: тихо сложить непонятное
изменение в капитал -- это и есть врать графиком.

ДВА СЧЁТА ВМЕСТО ОДНОГО -- ПРО WSOL. У счёта с нативным минтом лампорты
РАВНЫ завёрнутому плюс рента, поэтому «WSOL + рента этого счёта» -- это
ровно его лампорты, и прибавлять их дважды нельзя. Рента берётся из
rentExemptReserve разобранного счёта, а если узел её не отдал -- как
лампорты минус завёрнутое. Проверка этого тождества стоит в самопроверке.

ЧЕГО ЗДЕСЬ НЕТ. Ни одной сделки, ни одного ключа, ни одной подписи, ни одной
отправки, ни одного обращения к сети ВНУТРИ модуля: сеть приходит снаружи
функцией rpc_call(метод, параметры) -- ту же приёмку использует вся полоса,
и благодаря ей самопроверка идёт БЕЗ СЕТИ на поддельном узле.

РАСХОД КРЕДИТОВ. По умолчанию узел -- ПУБЛИЧНЫЙ (api.mainnet-beta.solana.com),
то есть кредитов Helius трекер не тратит вовсе. Если узел задан свой, вызовы
всё равно считаются по суткам и лежат в состоянии числом: «сколько чего
спрошено» -- это то, что можно сверить с панелью, а не оценка.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
КАТАЛОГ = КОРЕНЬ / "data" / "kapital"

КОШЕЛЕК = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"
WSOL = "So11111111111111111111111111111111111111112"
ПРОГ_ТОКЕНА = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ПРОГ_ТОКЕНА_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ЛАМПОРТОВ_В_SOL = 1_000_000_000

# УЗЕЛ ПО УМОЛЧАНИЮ -- ПУБЛИЧНЫЙ. На лаборатории не должно быть ни ключей, ни
# env полосы: ключ Helius -- это env полосы. Публичный узел отдаёт всё, что
# нужно трекеру (балансы, счета, подписи, транзакции), и стоит нуль кредитов.
УЗЕЛ_ПО_УМОЛЧАНИЮ = "https://api.mainnet-beta.solana.com"

# ОКНА СТРАНИЦЫ И ШАГ СВЁРТКИ -- слово владельца: 1Ч минутой, 24Ч десятью
# минутами, 3Д получасом; значение корзины -- ПОСЛЕДНЕЕ в ней.
ОКНА = (
    {"имя": "1Ч", "окно_сек": 3600, "корзина_сек": 60},
    {"имя": "24Ч", "окно_сек": 86400, "корзина_сек": 600},
    {"имя": "3Д", "окно_сек": 259200, "корзина_сек": 1800},
)
# Ряд держится 4 суток, а не 3: окно 3Д должно быть полным и в тот час, когда
# сборщик только что перезапустился. Старше -- отрезается при записи.
ДЕРЖИМ_СУТОК = 4
# ТОКЕН-СЧЕТА ЧИТАЮТСЯ РЕЖЕ БАЛАНСА, И ЭТО ЗАМЕР, А НЕ ЭКОНОМИЯ НА СПИЧКАХ.
# По снимку data/bloom_token_accounts.json (03.10T16:01Z) у кошелька полосы 404
# токен-счёта, и один ответ getTokenAccountsByOwner по ним -- около 300 КБ. При
# опросе раз в 20 с это больше гигабайта в сутки впустую: счета меняются ТОЛЬКО
# когда прошла транзакция, а новые подписи мы и так спрашиваем каждый тик.
# Поэтому счета перечитываются при первом тике, при новых подписях и не реже
# чем раз в СЧЕТА_НЕ_СТАРШЕ_СЕК -- последнее на случай, если подписи мы
# пропустили (узел молчал, окно подписей упёрлось в предел).
СЧЕТА_НЕ_СТАРШЕ_СЕК = int(os.environ.get("BLOOM_TREKKER_SCHETA_SEK", "300") or 300)
ФАЙЛ_РЯДОВ = "rjady.jsonl"
ФАЙЛ_СОСТОЯНИЯ = "sostojanie.json"
ФАЙЛ_ВЫГРУЗКИ = "kapital.json"
ФАЙЛ_СТРАНИЦЫ = "kapital.html"

# ПРОГРАММЫ, ПРИ КОТОРЫХ ПЕРЕМЕЩЕНИЕ SOL ЕЩЁ СЧИТАЕТСЯ ПРОСТЫМ ПЕРЕВОДОМ.
# Почему список, а не «нет токеновых изменений»: 03.10 Code-1 платил ренту за
# расширение таблицы адресов (0.0008178 SOL) -- токеновых изменений там нет
# вовсе, и без списка этот РАСХОД попал бы в «переводы» и тихо вышел из
# изменения за период. Теперь он попадает в «иное» и ЧЕСТНО уменьшает капитал,
# а переводом считается только то, что сделано системной программой.
ПРОГ_СИСТЕМЫ = "11111111111111111111111111111111"
ПРОГ_БЮДЖЕТА = "ComputeBudget111111111111111111111111111111"
ПРОГ_МЕМО = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
ПРОГ_МЕМО_СТАРАЯ = "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"
ПРОГРАММЫ_ПЕРЕВОДА = (ПРОГ_СИСТЕМЫ, ПРОГ_БЮДЖЕТА, ПРОГ_МЕМО, ПРОГ_МЕМО_СТАРАЯ)

ВИД_СВОП = "своп"
ВИД_ПЕРЕВОД = "перевод"
ВИД_ЗАКРЫТИЕ = "закрытие счёта"
ВИД_ИНОЕ = "иное"

WHY_NET_UZLA = "узел не задан -- читать нечем"
WHY_UZEL_MOLCHIT = "узел не ответил"
WHY_NET_RJADOV = "ряда ещё нет -- сборщик не прогонялся"


class ОшибкаТрекера(Exception):
    pass


# --------------------------------------------------------- чистая арифметика

def renta_scheta(счёт: dict) -> int:
    """Рента ЭТОГО токен-счёта в лампортах.

    У нативного (WSOL) счёта лампорты равны завёрнутому ПЛЮС рента, поэтому
    рента -- это rentExemptReserve, который узел отдаёт в разобранном счёте. Нет
    его -- лампорты минус завёрнутое, и это то же число. У обычного счёта рента
    -- все его лампорты: токены лежат не в лампортах.
    """
    лампорты = int(счёт.get("lamports") or 0)
    if str(счёт.get("mint")) != WSOL:
        return лампорты
    рез = счёт.get("renta_rezerv")
    if isinstance(рез, int) and рез >= 0:
        return min(рез, лампорты)
    return max(0, лампорты - int(счёт.get("amount") or 0))


def kapital(sol_lamports: int, scheta: list, pozicii: dict) -> dict:
    """Капитал одним числом и его четыре части. Чистая функция, без сети.

    scheta -- список словарей {"mint", "lamports", "amount", "decimals",
    "renta_rezerv"}; pozicii -- {минт: цена входа в лампортах}.
    """
    из_ = {"sol": int(sol_lamports), "wsol": 0, "renta": 0, "pozicii": 0,
            "schetov": len(scheta), "schetov_pustyh": 0, "schetov_wsol": 0,
            "pozicij": 0}
    for с in scheta:
        рента = renta_scheta(с)
        из_["renta"] += рента
        это_wsol = str(с.get("mint")) == WSOL
        if это_wsol:
            из_["schetov_wsol"] += 1
            # ЗАВЁРНУТОЕ -- ЭТО ЛАМПОРТЫ МИНУС РЕНТА, И НИ БАЙТОМ БОЛЬШЕ.
            из_["wsol"] += max(0, int(с.get("lamports") or 0) - рента)
        if not int(с.get("amount") or 0):
            из_["schetov_pustyh"] += 1
    for минт, вход in (pozicii or {}).items():
        if минт == WSOL:
            # WSOL -- не позиция: он уже посчитан завёрнутым. Иначе двойной счёт.
            continue
        из_["pozicii"] += int(вход or 0)
        из_["pozicij"] += 1
    из_["kapital"] = (из_["sol"] + из_["wsol"] + из_["renta"]
                      + из_["pozicii"])
    return из_


def nashi_scheta_tranzakcii(tx: dict, koshelek: str) -> dict:
    """Наши токен-счета в транзакции: индексы, минты и изменение остатков.

    Берётся из meta.pre/postTokenBalances -- там стоит owner, то есть «наш»
    определяется цепью, а не догадкой по именам счетов.
    """
    мета = (tx or {}).get("meta") or {}
    из_ = {"indeksy": {}, "tokeny_do": {}, "tokeny_posle": {}}
    for где, ключ in (("tokeny_do", "preTokenBalances"),
                      ("tokeny_posle", "postTokenBalances")):
        for б in (мета.get(ключ) or []):
            if str(б.get("owner")) != str(koshelek):
                continue
            и = int(б.get("accountIndex"))
            минт = str(б.get("mint"))
            из_["indeksy"][и] = минт
            сумма = int(((б.get("uiTokenAmount") or {}).get("amount")) or 0)
            из_[где][и] = {"mint": минт, "amount": сумма}
    return из_


def delta_lamportov(tx: dict, indeks: int) -> int:
    """Сколько лампортов прибыло на счёт с этим индексом (минус -- убыло)."""
    мета = (tx or {}).get("meta") or {}
    до = (мета.get("preBalances") or [])
    после = (мета.get("postBalances") or [])
    if indeks >= len(до) or indeks >= len(после):
        return 0
    return int(после[indeks]) - int(до[indeks])


def indeks_koshelka(tx: dict, koshelek: str) -> int | None:
    """Место кошелька в списке счетов транзакции (ключи сообщения)."""
    сообщение = (((tx or {}).get("transaction") or {}).get("message") or {})
    ключи = сообщение.get("accountKeys") or []
    for и, к in enumerate(ключи):
        адрес = k_adresu(к)
        if адрес == str(koshelek):
            return и
    return None


def k_adresu(ключ) -> str:
    """Адрес из ключа сообщения: он бывает строкой и словарём с pubkey."""
    if isinstance(ключ, dict):
        return str(ключ.get("pubkey") or "")
    return str(ключ or "")


def programmy_tranzakcii(tx: dict) -> list:
    """Программы верхнего уровня этой транзакции -- по именам, без догадок."""
    сообщение = (((tx or {}).get("transaction") or {}).get("message") or {})
    ключи = [k_adresu(к) for к in (сообщение.get("accountKeys") or [])]
    из_ = []
    for их in (сообщение.get("instructions") or []):
        прог = их.get("programId")
        if not прог and isinstance(их.get("programIdIndex"), int):
            и = int(их["programIdIndex"])
            прог = ключи[и] if и < len(ключи) else None
        if прог and прог not in из_:
            из_.append(str(прог))
    return из_


def razbor_tranzakcii(tx: dict, koshelek: str = КОШЕЛЕК) -> dict:  # noqa: C901, PLR0912
    """ЧТО ЭТА ТРАНЗАКЦИЯ СДЕЛАЛА С НАШИМ КАПИТАЛОМ. Чистая функция.

    Отдаёт:
      * `sol_delta`      -- сколько лампортов прибыло/убыло у самого кошелька;
      * `renta_delta`    -- на сколько изменились лампорты НАШИХ токен-счетов
                            (новый счёт -- плюс рента, закрытый -- минус);
      * `potracheno`     -- sol_delta со знаком «ушло», МИНУС renta_delta:
                            ровно та цена входа, при которой покупка не двигает
                            капитал (см. docstring модуля);
      * `tokeny`         -- {минт: изменение сырого остатка};
      * `vid`            -- своп / перевод / закрытие счёта / иное;
      * `pochemu`        -- словами, почему именно такой вид.
    """
    из_ = {"ok": False, "why_not": None, "sig": None, "slot": None,
            "utc": None, "oshibka_cepi": None, "sol_delta": 0,
            "renta_delta": 0, "potracheno": 0, "polucheno": 0,
            "tokeny": {}, "vid": ВИД_ИНОЕ, "pochemu": None, "fee": 0}
    if not isinstance(tx, dict) or not tx:
        из_["why_not"] = "транзакции нет"
        return из_
    мета = tx.get("meta") or {}
    из_["slot"] = tx.get("slot")
    из_["utc"] = tx.get("blockTime")
    из_["fee"] = int(мета.get("fee") or 0)
    подписи = ((tx.get("transaction") or {}).get("signatures") or [])
    из_["sig"] = подписи[0] if подписи else None
    if мета.get("err") is not None:
        # УПАВШАЯ ТРАНЗАКЦИЯ НЕ ДВИГАЕТ НИЧЕГО, КРОМЕ КОМИССИИ. Её нельзя
        # считать сделкой, но и выбрасывать нельзя: комиссия ушла.
        из_["oshibka_cepi"] = str(мета.get("err"))[:120]
    и_к = indeks_koshelka(tx, koshelek)
    if и_к is None:
        из_["why_not"] = "кошелька нет в счетах транзакции"
        return из_
    из_["sol_delta"] = delta_lamportov(tx, и_к)
    наши = nashi_scheta_tranzakcii(tx, koshelek)
    рента_дельта = 0
    for и in наши["indeksy"]:
        рента_дельта += delta_lamportov(tx, и)
    из_["renta_delta"] = рента_дельта
    # ИЗМЕНЕНИЕ ТОКЕНОВЫХ ОСТАТКОВ -- ПО МИНТАМ, а не по счетам: счёт мог быть
    # создан и закрыт в одной транзакции.
    по_минтам: dict = {}
    for и, минт in наши["indeksy"].items():
        до = (наши["tokeny_do"].get(и) or {}).get("amount", 0)
        после = (наши["tokeny_posle"].get(и) or {}).get("amount", 0)
        if после - до:
            по_минтам[минт] = по_минтам.get(минт, 0) + (после - до)
    из_["tokeny"] = по_минтам
    программы = programmy_tranzakcii(tx)
    из_["programmy"] = программы
    # ПОТРАЧЕНО И ПОЛУЧЕНО -- ОДНА ФОРМУЛА, РАЗНЫЕ ЗНАКИ, И ЗНАК У РЕНТЫ
    # ИМЕННО ПЛЮС. Считается чистый поток ЦЕННОСТИ из системы «кошелёк плюс наши
    # токен-счета»: рента, уехавшая в новый счёт, из системы НЕ ушла (значит
    # уменьшает трату), а рента, вернувшаяся при закрытии счёта, в систему НЕ
    # пришла (значит уменьшает выручку). Проверено числами на всех четырёх
    # случаях: покупка -1.0 SOL при ренте +0.00203928 даёт трату 0.99796072 и
    # капитал не двигает; продажа +1.1 при ренте -0.00203928 даёт выручку
    # 1.09796072; закрытие счёта даёт РОВНО НОЛЬ; перевод даёт себя целиком.
    чистое = из_["sol_delta"] + из_["renta_delta"]   # <0 ушло, >0 пришло
    из_["potracheno"] = -чистое if чистое < 0 else 0
    из_["polucheno"] = чистое if чистое > 0 else 0
    # --- ВИД. Своп -- когда изменились наши токеновые остатки. Перевод -- когда
    # не изменилось НИЧЕГО токенового и лампорты наших счетов стоят на месте.
    if по_минтам:
        из_["vid"] = ВИД_СВОП
        из_["pochemu"] = f"изменились остатки минтов: {sorted(по_минтам)[:3]}"
    elif рента_дельта < 0 and из_["sol_delta"] > 0:
        из_["vid"] = ВИД_ЗАКРЫТИЕ
        из_["pochemu"] = (f"лампорты наших счетов убыли на {-рента_дельта}, "
                          f"а у кошелька прибыли на {из_['sol_delta']}")
    elif рента_дельта == 0 and программы and all(
            п in ПРОГРАММЫ_ПЕРЕВОДА for п in программы):
        из_["vid"] = ВИД_ПЕРЕВОД
        из_["pochemu"] = ("токеновых изменений нет, лампорты наших счетов не "
                          "двигались, и программы только системные -- это ввод "
                          "или вывод SOL")
    else:
        из_["vid"] = ВИД_ИНОЕ
        из_["pochemu"] = (f"ни своп, ни перевод: токеновых изменений нет, "
                          f"лампорты наших счетов {рента_дельта:+d}, программы "
                          f"{программы[:3]} -- вид НЕ НАЗВАН, и этот расход "
                          f"остаётся в капитале как есть")
    из_["ok"] = True
    return из_


def perevod_sol(razbor: dict) -> int:
    """Ввод (>0) или вывод (<0) SOL по разбору перевода, в лампортах.

    Комиссию платит отправитель, поэтому у ВЫВОДА она входит в убыль, а у ВВОДА
    её платит не наш кошелёк. Вывод отдаётся с комиссией вместе: владельцу важно
    «сколько ушло с кошелька», а не «сколько дошло».
    """
    if (razbor or {}).get("vid") != ВИД_ПЕРЕВОД:
        return 0
    return int(razbor.get("sol_delta") or 0)


# ------------------------------------------------------------------ сборщик

def uzel_iz_okruzheniya() -> str:
    """Узел для чтения цепи. По умолчанию ПУБЛИЧНЫЙ -- кредитов не тратит.

    СЕКРЕТ НЕ В ARGV. Если узел свой (с ключом в URL), он приходит ТОЛЬКО
    переменной окружения BLOOM_TREKKER_RPC: ключ в командной строке виден в
    списке процессов любому на машине, а в логах прогона -- всем, кто его
    откроет. Поэтому ключа командной строки для узла здесь нет вовсе.
    """
    return (os.environ.get("BLOOM_TREKKER_RPC") or УЗЕЛ_ПО_УМОЛЧАНИЮ).strip()


def zateret(строка) -> str:
    """Спрятать секрет в строке: из URL остаётся только хост.

    Любая строка, которая уйдёт в вывод, файл или журнал, проходит через это.
    """
    т = str(строка or "")
    if "://" not in т:
        return т
    голова, хвост = т.split("://", 1)
    хост = хвост.split("/", 1)[0]
    путь = ("/…" if "/" in хвост and хвост.split("/", 1)[1] else "")
    return f"{голова}://{хост}{путь}"


def rpc_iz_url(url: str, *, tajmaut: float = 8.0):
    """Функция rpc_call(метод, параметры) поверх http. Сеть -- только здесь.

    Отдельной функцией -- чтобы ВСЁ остальное в модуле считалось без сети и
    проверялось поддельным узлом.
    """
    import urllib.request  # noqa: PLC0415

    счёт = {"вызовов": 0}

    def _зов(метод: str, параметры: list):
        счёт["вызовов"] += 1
        тело = json.dumps({"jsonrpc": "2.0", "id": счёт["вызовов"],
                           "method": метод, "params": параметры}).encode()
        зап = urllib.request.Request(  # noqa: S310
            url, data=тело, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(зап, timeout=tajmaut) as отв:  # noqa: S310
            return json.loads(отв.read().decode("utf-8", "replace"))

    _зов.счёт = счёт
    return _зов


class Schjotchik:
    """Счёт вызовов по суткам -- то, что сверяется с панелью узла.

    ЦЕНА ВЫЗОВА -- ПО ЕДИНСТВЕННОЙ ТАБЛИЦЕ РЕПОЗИТОРИЯ, а не по памяти:
    analysis/solana_rpc_client.py:106-109 -- CREDITS_DEFAULT = 1,
    CREDITS_BY_METHOD = {"getProgramAccounts": 10}, enhanced = 100, подписка --
    2 кредита за 0.1 МБ. Все пять методов трекера (getBalance,
    getTokenAccountsByOwner, getSignaturesForAddress, getTransaction,
    getMultipleAccounts) попадают под умолчание, то есть ОДИН кредит за вызов;
    поштучного замера по панели Helius в репозитории нет ни для одного из них,
    и это сказано прямо, а не спрятано за «примерно».

    ПО УМОЛЧАНИЮ ЖЕ УЗЕЛ ПУБЛИЧНЫЙ, и тогда кредитов НОЛЬ. Считать вызовы всё
    равно надо: по ним видно, что трекер не разогнался и не упёрся в предел
    узла (50 rps на тарифе Developer).
    """
    ЦЕНА_ВЫЗОВА = {"getProgramAccounts": 10}
    ЦЕНА_ПО_УМОЛЧАНИЮ = 1

    def __init__(self, было: dict | None = None):
        self.po_sutkam = dict(было or {})

    def zov(self, метод: str, когда: float | None = None) -> None:
        сутки = time.strftime("%Y-%m-%d",
                              time.gmtime(когда if когда else time.time()))
        д = self.po_sutkam.setdefault(сутки, {})
        д[метод] = int(д.get(метод) or 0) + 1
        д["всего"] = int(д.get("всего") or 0) + 1
        д["кредитов_если_helius"] = int(д.get("кредитов_если_helius") or 0) + \
            self.ЦЕНА_ВЫЗОВА.get(метод, self.ЦЕНА_ПО_УМОЛЧАНИЮ)

    def obrezat(self, суток: int = 14) -> None:
        for сутки in sorted(self.po_sutkam)[:-суток] if суток else []:
            self.po_sutkam.pop(сутки, None)


def _rezultat(отв: dict, метод: str) -> object:
    if not isinstance(отв, dict):
        raise ОшибкаТрекера(f"{WHY_UZEL_MOLCHIT}: {метод}: ответ не словарь")
    if отв.get("error"):
        raise ОшибкаТрекера(f"{метод}: узел вернул ошибку "
                            f"{str(отв['error'])[:120]}")
    if "result" not in отв:
        raise ОшибкаТрекера(f"{метод}: в ответе нет result")
    return отв["result"]


def sol_koshelka(rpc_call, koshelek: str, *, schjotchik=None) -> dict:
    """Лампорты кошелька и слот ответа."""
    if schjotchik:
        schjotchik.zov("getBalance")
    р = _rezultat(rpc_call("getBalance", [koshelek, {"commitment": "confirmed"}]),
                  "getBalance")
    зн = р.get("value") if isinstance(р, dict) else р
    слот = ((р.get("context") or {}).get("slot") if isinstance(р, dict) else None)
    return {"lamports": int(зн or 0), "slot": слот}


def tokennye_scheta(rpc_call, koshelek: str, *, schjotchik=None) -> list:
    """Все токен-счета кошелька -- по ОБЕИМ программам токена.

    Token-2022 -- отдельная программа, и счёт на ней getTokenAccountsByOwner по
    классической программе НЕ отдаст: пропустить её значило бы потерять часть
    ренты и часть позиций молча.
    """
    счета = []
    for прог in (ПРОГ_ТОКЕНА, ПРОГ_ТОКЕНА_2022):
        if schjotchik:
            schjotchik.zov("getTokenAccountsByOwner")
        р = _rezultat(rpc_call("getTokenAccountsByOwner",
                               [koshelek, {"programId": прог},
                                {"encoding": "jsonParsed",
                                 "commitment": "confirmed"}]),
                      "getTokenAccountsByOwner")
        for зап in ((р.get("value") if isinstance(р, dict) else р) or []):
            счёт = зап.get("account") or {}
            инфо = (((счёт.get("data") or {}).get("parsed") or {})
                    .get("info") or {})
            сумма = ((инфо.get("tokenAmount") or {}).get("amount"))
            рез = ((инфо.get("rentExemptReserve") or {}).get("amount")
                   if isinstance(инфо.get("rentExemptReserve"), dict)
                   else инфо.get("rentExemptReserve"))
            счета.append({
                "adres": зап.get("pubkey"),
                "mint": str(инфо.get("mint") or ""),
                "lamports": int(счёт.get("lamports") or 0),
                "amount": int(сумма or 0),
                "decimals": int(((инфо.get("tokenAmount") or {})
                                 .get("decimals")) or 0),
                "renta_rezerv": (int(рез) if рез not in (None, "") else None),
                "programma": прог,
            })
    return счета


def podpisi_koshelka(rpc_call, koshelek: str, *, do_sig: str | None = None,
                     predel: int = 1000, stranic: int = 10,
                     schjotchik=None) -> list:
    """Подписи кошелька от новых к старым. do_sig -- докуда (не включая).

    ОГРАНИЧЕНИЕ НАЗВАНО: узел отдаёт не больше 1000 подписей за страницу, и
    страниц берётся не больше `stranic`. Упёрлись в предел -- это видно полем
    `upjorlis`, а не молчанием.
    """
    из_ = []
    до = None
    for _ in range(max(1, int(stranic))):
        пар = {"limit": min(1000, int(predel)), "commitment": "confirmed"}
        if до:
            пар["before"] = до
        if do_sig:
            пар["until"] = do_sig
        if schjotchik:
            schjotchik.zov("getSignaturesForAddress")
        р = _rezultat(rpc_call("getSignaturesForAddress", [koshelek, пар]),
                      "getSignaturesForAddress")
        ряд = (р.get("value") if isinstance(р, dict) else р) or []
        из_ += [з for з in ряд if з.get("signature")]
        if len(ряд) < пар["limit"]:
            return из_
        до = ряд[-1].get("signature")
    return из_


def tranzakciya(rpc_call, sig: str, *, schjotchik=None) -> dict:
    """Разобранная транзакция. Версии 0 тоже нужны -- иначе узел откажет."""
    if schjotchik:
        schjotchik.zov("getTransaction")
    р = _rezultat(rpc_call("getTransaction",
                           [sig, {"encoding": "jsonParsed",
                                  "maxSupportedTransactionVersion": 0,
                                  "commitment": "confirmed"}]),
                  "getTransaction")
    return р or {}


# -------------------------------------------- состояние: позиции и переводы

def pustoe_sostojanie(koshelek: str = КОШЕЛЕК) -> dict:
    return {"koshelek": koshelek, "obnovleno_utc": None, "poslednjaja_sig": None,
            "pervaja_sig_istorii": None, "pozicii": {}, "sdelki": [],
            "metki": [], "vvod_vyvod_lamports": 0, "zakryto_schetov": 0,
            "neopoznannyh": 0, "kredity": {}, "podpisej_razobrano": 0}


def prinjat_tranzakciju(sostojanie: dict, tx: dict,
                        koshelek: str | None = None) -> dict:
    """Применить ОДНУ транзакцию к состоянию. Порядок -- от старых к новым.

    ЧТО СЧИТАЕТСЯ ПОЗИЦИЕЙ. Один своп в полосе покупает ОДИН минт. Если в
    транзакции выросли остатки ДВУХ минтов и больше, делить потраченное между
    ними нечем -- и делить наугад на деньгах нельзя. Такая транзакция
    называется непонятной (`neopoznannyh`), позиции не трогает, и тогда капитал
    честно показывает убыль SOL: это видно, а не спрятано.
    """
    кош = koshelek or sostojanie.get("koshelek") or КОШЕЛЕК
    р = razbor_tranzakcii(tx, кош)
    из_ = {"vid": р.get("vid"), "pochemu": р.get("pochemu"),
            "sig": р.get("sig"), "ok": bool(р.get("ok")),
            "otkryto": [], "zakryto": [], "metka": None}
    if not р["ok"]:
        return из_
    sostojanie["podpisej_razobrano"] = int(
        sostojanie.get("podpisej_razobrano") or 0) + 1
    if р["vid"] == ВИД_ПЕРЕВОД:
        дельта = perevod_sol(р)
        if дельта:
            метка = {"utc": р.get("utc"), "sig": р.get("sig"),
                      "vid": ("ввод" if дельта > 0 else "вывод"),
                      "lamports": abs(дельта)}
            sostojanie["metki"] = (sostojanie.get("metki") or []) + [метка]
            sostojanie["vvod_vyvod_lamports"] = int(
                sostojanie.get("vvod_vyvod_lamports") or 0) + дельта
            из_["metka"] = метка
        return из_
    if р["vid"] == ВИД_ЗАКРЫТИЕ:
        sostojanie["zakryto_schetov"] = int(
            sostojanie.get("zakryto_schetov") or 0) + 1
        return из_
    if р["vid"] != ВИД_СВОП:
        sostojanie["neopoznannyh"] = int(
            sostojanie.get("neopoznannyh") or 0) + 1
        return из_
    куплено = [(м, д) for м, д in (р["tokeny"] or {}).items()
               if д > 0 and м != WSOL]
    продано = [(м, д) for м, д in (р["tokeny"] or {}).items()
               if д < 0 and м != WSOL]
    позиции = sostojanie.setdefault("pozicii", {})
    # --- ПРОДАЖА ПЕРВОЙ: в одной транзакции может быть и то и другое.
    for минт, дельта in продано:
        п = позиции.get(минт)
        ушло = -дельта
        if not п:
            sostojanie["neopoznannyh"] = int(
                sostojanie.get("neopoznannyh") or 0) + 1
            continue
        было = int(п.get("ostatok") or 0)
        доля = (min(1.0, ушло / было) if было > 0 else 1.0)
        вход_части = int(round(int(п.get("vhod") or 0) * доля))
        итог = int(р.get("polucheno") or 0) - вход_части
        sostojanie["sdelki"] = (sostojanie.get("sdelki") or []) + [{
            "mint": минт, "vhod": вход_части, "polucheno": int(р.get("polucheno") or 0),
            "itog": итог, "chast": round(доля, 6),
            "otkryta_utc": п.get("utc"), "zakryta_utc": р.get("utc"),
            "sig_pokupki": п.get("sig"), "sig_prodazhi": р.get("sig")}]
        из_["zakryto"].append({"mint": минт, "itog": итог, "chast": доля})
        остаток = было - ушло
        if остаток > 0 and доля < 1.0:
            п["vhod"] = int(п.get("vhod") or 0) - вход_части
            п["ostatok"] = остаток
        else:
            позиции.pop(минт, None)
    # --- ПОКУПКА: ровно один минт, иначе делить потраченное нечем.
    if len(куплено) == 1:
        минт, дельта = куплено[0]
        п = позиции.get(минт)
        if п:
            п["vhod"] = int(п.get("vhod") or 0) + int(р.get("potracheno") or 0)
            п["ostatok"] = int(п.get("ostatok") or 0) + дельта
            п["dobavok"] = int(п.get("dobavok") or 0) + 1
        else:
            позиции[минт] = {"vhod": int(р.get("potracheno") or 0),
                              "ostatok": дельта, "sig": р.get("sig"),
                              "utc": р.get("utc"), "slot": р.get("slot"),
                              "dobavok": 0}
        из_["otkryto"].append({"mint": минт, "vhod": int(р.get("potracheno") or 0)})
    elif len(куплено) > 1:
        sostojanie["neopoznannyh"] = int(
            sostojanie.get("neopoznannyh") or 0) + 1
        из_["pochemu"] = (f"{len(куплено)} минтов выросли в одной транзакции -- "
                          f"потраченное между ними делить нечем")
    return из_


def rjad(rpc_call, *, koshelek: str = КОШЕЛЕК, sostojanie: dict | None = None,
         schjotchik=None, sejchas: float | None = None,
         schitat_scheta: bool = True) -> dict:
    """ОДИН РЯД капитала: чтение цепи плюс арифметика. Сети внутри нет.

    schitat_scheta=False -- взять счета из состояния (кэш), не спрашивая узел.
    Тогда в ряде стоит поле `scheta_vozrast_sek`: по нему видно, насколько
    старым снимком счетов посчитаны рента и позиции.
    """
    сост = sostojanie if sostojanie is not None else pustoe_sostojanie(koshelek)
    сейчас_ = float(sejchas if sejchas else time.time())
    б = sol_koshelka(rpc_call, koshelek, schjotchik=schjotchik)
    кэш = сост.get("scheta_snimok") or {}
    if schitat_scheta or not кэш.get("scheta"):
        счета = tokennye_scheta(rpc_call, koshelek, schjotchik=schjotchik)
        сост["scheta_snimok"] = {"utc": int(сейчас_), "scheta": счета}
        возраст = 0
    else:
        счета = кэш.get("scheta") or []
        возраст = int(сейчас_ - int(кэш.get("utc") or сейчас_))
    # ПОЗИЦИИ -- ТОЛЬКО ПО ТОМУ, ЧТО ДЕЙСТВИТЕЛЬНО ЛЕЖИТ НА СЧЕТАХ. Позиция из
    # состояния, которой на цепи уже нет, в капитал не идёт: иначе капитал
    # помнил бы проданное.
    остатки: dict = {}
    for с in счета:
        if int(с.get("amount") or 0) > 0 and str(с.get("mint")) != WSOL:
            остатки[str(с["mint"])] = (остатки.get(str(с["mint"]), 0)
                                       + int(с["amount"]))
    позиции_вход: dict = {}
    без_входа = []
    for минт in остатки:
        п = (сост.get("pozicii") or {}).get(минт)
        if п and int(п.get("vhod") or 0) > 0:
            позиции_вход[минт] = int(п["vhod"])
        else:
            # ЦЕНЫ ВХОДА НЕТ -- И ЭТО НАЗЫВАЕТСЯ ЧИСЛОМ. Такой минт даёт нуль в
            # капитал, значит капитал ЗАНИЖЕН на его вход, и прятать это нельзя.
            без_входа.append(минт)
    к = kapital(б["lamports"], счета, позиции_вход)
    из_ = {"utc": int(sejchas if sejchas else time.time()),
            "slot": б.get("slot"),
            "sol": к["sol"], "wsol": к["wsol"], "renta": к["renta"],
            "pozicii": к["pozicii"], "kapital": к["kapital"],
            "pozicij": к["pozicij"], "schetov": к["schetov"],
            "schetov_pustyh": к["schetov_pustyh"],
            "bez_vhoda": len(без_входа),
            "vvod_vyvod": int(сост.get("vvod_vyvod_lamports") or 0),
            "sdelok": len(сост.get("sdelki") or []),
            "zakrytyj_itog": sum(int(с.get("itog") or 0)
                                 for с in (сост.get("sdelki") or [])),
            "neopoznannyh": int(сост.get("neopoznannyh") or 0)}
    из_["bez_vhoda_minty"] = без_входа[:5]
    из_["scheta_vozrast_sek"] = возраст
    return из_


def dognat_cep(rpc_call, *, koshelek: str = КОШЕЛЕК, sostojanie: dict,
               sutok: float = 3.0, schjotchik=None,
               sejchas: float | None = None, predel_podpisej: int = 3000) -> dict:
    """Догнать цепь: разобрать НОВЫЕ подписи кошелька и обновить состояние.

    ПЕРВЫЙ ПРОГОН -- ВОССТАНОВЛЕНИЕ ИСТОРИИ ЗА `sutok` СУТОК по цепи: иначе у
    открытых позиций не будет цены входа, и капитал окажется занижен на всё, что
    куплено до запуска.

    ПОРЯДОК РАЗБОРА -- ОТ СТАРЫХ К НОВЫМ, и это не вкусовщина: позиция обязана
    открыться раньше, чем закроется, иначе продажа не найдёт входа.
    """
    из_ = {"ok": False, "why_not": None, "podpisej": 0, "razobrano": 0,
            "propushcheno_staryh": 0, "upjorlis_v_predel": False,
            "pervyj_progon": not bool(sostojanie.get("poslednjaja_sig"))}
    сейчас = float(sejchas if sejchas else time.time())
    порог = сейчас - float(sutok) * 86400.0
    подписи = podpisi_koshelka(
        rpc_call, koshelek, do_sig=sostojanie.get("poslednjaja_sig"),
        schjotchik=schjotchik,
        stranic=(max(1, int(predel_podpisej // 1000)) if из_["pervyj_progon"] else 2))
    из_["podpisej"] = len(подписи)
    if len(подписи) >= predel_podpisej:
        из_["upjorlis_v_predel"] = True
    # Новые идут первыми -- переворачиваем и отбрасываем то, что старше окна.
    годные = []
    for з in reversed(подписи):
        т = з.get("blockTime")
        if из_["pervyj_progon"] and isinstance(т, int) and т < порог:
            из_["propushcheno_staryh"] += 1
            continue
        годные.append(з)
    for з in годные:
        tx = tranzakciya(rpc_call, з["signature"], schjotchik=schjotchik)
        if not tx:
            continue
        prinjat_tranzakciju(sostojanie, tx, koshelek)
        из_["razobrano"] += 1
        sostojanie["poslednjaja_sig"] = з["signature"]
        if not sostojanie.get("pervaja_sig_istorii"):
            sostojanie["pervaja_sig_istorii"] = з["signature"]
    sostojanie["obnovleno_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime(сейчас))
    из_["ok"] = True
    return из_


# ------------------------------------------------- свёртка и монотонная кривая

def korziny(rjady: list, *, okno_sek: int, korzina_sek: int,
            sejchas: float | None = None) -> dict:
    """Свёртка ряда в корзины: значение корзины -- ПОСЛЕДНЕЕ в ней.

    ПУСТЫЕ КОРЗИНЫ НЕ ЗАПОЛНЯЮТСЯ. Протянуть последнее значение вперёд -- значит
    скрыть простой сборщика: график будет ровным там, где данных нет вовсе.
    Поэтому пустая корзина просто отсутствует, а число пропусков и самый долгий
    пропуск отдаются ЧИСЛОМ -- по ним видно, что сборщик стоял.
    """
    сейчас = float(sejchas if sejchas else time.time())
    порог = сейчас - float(okno_sek)
    по_корзинам: dict = {}
    for р in rjady:
        т = float(р.get("utc") or 0)
        if т < порог or т > сейчас + 1:
            continue
        к = int(т // korzina_sek)
        прежний = по_корзинам.get(к)
        if прежний is None or float(прежний.get("utc") or 0) <= т:
            по_корзинам[к] = р
    ключи = sorted(по_корзинам)
    точки = [{"utc": int(по_корзинам[к].get("utc") or 0),
               "lamports": int(по_корзинам[к].get("kapital") or 0)}
              for к in ключи]
    # ЧИСЛО КОРЗИН ОКНА -- ПО ЕГО РАЗМАХУ, А НЕ ДЕЛЕНИЕМ. Окно не выровнено по
    # границам корзин (сейчас -- произвольная секунда), поэтому делением
    # получалось на одну меньше, и «пропусков» уходило в ноль там, где их не
    # было, и наоборот.
    всего_корзин = int(сейчас // korzina_sek) - int(порог // korzina_sek) + 1
    пропусков = max(0, всего_корзин - len(ключи))
    долгий = 0
    for i in range(1, len(ключи)):
        долгий = max(долгий, (ключи[i] - ключи[i - 1] - 1) * korzina_sek)
    return {"tochki": точки, "korzin_vsego": всего_корзин,
            "korzin_est": len(ключи), "propuskov": пропусков,
            "dolgij_propusk_sek": долгий}


def monotonnye_tangensy(xs: list, ys: list) -> list:
    """Наклоны по Фричу--Карлсону: кривая не вылезает выше и ниже точек.

    ЗАЧЕМ ИМЕННО ЭТО (слово владельца: «кривая монотонная, без выбросов
    выше/ниже точек»). Обычный сглаженный сплайн на денежном графике ВЫДУМЫВАЕТ
    экстремумы: между двумя близкими точками он уходит выше максимума и ниже
    минимума, и владелец видит на картинке просадку, которой не было. Условие
    Фрича--Карлсона (ограничение наклонов тремя секущими) это запрещает, и
    запрет проверен числом: самопроверка сэмплирует готовую кривую и требует,
    чтобы на каждом отрезке она лежала между его концами.
    """
    n = len(xs)
    if n < 2:
        return [0.0] * n
    секущие = []
    for i in range(n - 1):
        dx = float(xs[i + 1] - xs[i]) or 1e-9
        секущие.append((float(ys[i + 1]) - float(ys[i])) / dx)
    m = [секущие[0]] + [0.0] * (n - 2) + [секущие[-1]]
    for i in range(1, n - 1):
        if секущие[i - 1] * секущие[i] <= 0:
            m[i] = 0.0
        else:
            m[i] = (секущие[i - 1] + секущие[i]) / 2.0
    for i in range(n - 1):
        if секущие[i] == 0:
            m[i] = 0.0
            m[i + 1] = 0.0
            continue
        a = m[i] / секущие[i]
        b = m[i + 1] / секущие[i]
        s = a * a + b * b
        if s > 9.0:
            t = 3.0 / (s ** 0.5)
            m[i] = t * a * секущие[i]
            m[i + 1] = t * b * секущие[i]
    return m


def put_krivoj(tochki: list, *, shirina: int = 1000, vysota: int = 320,
               pole: int = 8) -> dict:
    """SVG-путь линии и заливки по точкам. Чистая арифметика, без рисования.

    ПОЧЕМУ ПУТЬ СЧИТАЕТСЯ ЗДЕСЬ, А НЕ В СТРАНИЦЕ. Одна и та же арифметика в
    питоне и в JS однажды разойдётся, и разойдётся молча -- на картинке.
    Здесь она ОДНА, и её проверяет самопроверка; страница только подставляет
    готовую строку.
    """
    из_ = {"liniya": "", "zalivka": "", "tochek": len(tochki),
            "min": None, "max": None, "x": [], "y": []}
    if not tochki:
        return из_
    зн = [float(т["lamports"]) for т in tochki]
    вр = [float(т["utc"]) for т in tochki]
    низ, верх = min(зн), max(зн)
    из_["min"], из_["max"] = int(низ), int(верх)
    разброс = (верх - низ) or 1.0
    t0, t1 = вр[0], вр[-1]
    промежуток = (t1 - t0) or 1.0
    вн_ш = shirina - 2 * pole
    вн_в = vysota - 2 * pole
    xs = [pole + вн_ш * ((т - t0) / промежуток) for т in вр]
    ys = [pole + вн_в * (1.0 - (з - низ) / разброс) for з in зн]
    из_["x"], из_["y"] = [round(x, 2) for x in xs], [round(y, 2) for y in ys]
    if len(tochki) == 1:
        из_["liniya"] = f"M {xs[0]:.2f} {ys[0]:.2f} L {xs[0] + 0.01:.2f} {ys[0]:.2f}"
        из_["zalivka"] = (f"{из_['liniya']} L {xs[0] + 0.01:.2f} {vysota} "
                          f"L {xs[0]:.2f} {vysota} Z")
        return из_
    m = monotonnye_tangensy(xs, ys)
    части = [f"M {xs[0]:.2f} {ys[0]:.2f}"]
    for i in range(len(xs) - 1):
        dx = xs[i + 1] - xs[i]
        c1x, c1y = xs[i] + dx / 3.0, ys[i] + m[i] * dx / 3.0
        c2x, c2y = xs[i + 1] - dx / 3.0, ys[i + 1] - m[i + 1] * dx / 3.0
        части.append(f"C {c1x:.2f} {c1y:.2f} {c2x:.2f} {c2y:.2f} "
                      f"{xs[i + 1]:.2f} {ys[i + 1]:.2f}")
    из_["liniya"] = " ".join(части)
    из_["zalivka"] = (из_["liniya"] + f" L {xs[-1]:.2f} {vysota} "
                      f"L {xs[0]:.2f} {vysota} Z")
    return из_


def tochka_krivoj(put_x: list, put_y: list, tochki: list, dolya: float) -> tuple:
    """Значение кривой в доле от начала -- для проверки выбросов самопроверкой."""
    if not tochki:
        return (0.0, 0.0)
    i = min(len(tochki) - 2, max(0, int(dolya * (len(tochki) - 1))))
    т = (dolya * (len(tochki) - 1)) - i
    xs, ys = put_x, put_y
    m = monotonnye_tangensy(xs, ys)
    dx = xs[i + 1] - xs[i]
    p0, p1 = ys[i], ys[i + 1]
    m0, m1 = m[i] * dx, m[i + 1] * dx
    h00 = 2 * т ** 3 - 3 * т ** 2 + 1
    h10 = т ** 3 - 2 * т ** 2 + т
    h01 = -2 * т ** 3 + 3 * т ** 2
    h11 = т ** 3 - т ** 2
    return (i, h00 * p0 + h10 * m0 + h01 * p1 + h11 * m1)


# --------------------------------------------------------- файлы и выгрузка

def prochitat_rjady(put_: str | Path) -> list:
    """Ряд из jsonl. Битая строка не роняет прогон, но и не молчит."""
    п = Path(put_)
    из_, плохих = [], 0
    if not п.exists():
        return из_
    for строка in п.read_text(encoding="utf-8", errors="replace").splitlines():
        строка = строка.strip()
        if not строка:
            continue
        try:
            р = json.loads(строка)
        except ValueError:
            плохих += 1
            continue
        if isinstance(р, dict) and isinstance(р.get("utc"), int):
            из_.append(р)
    if плохих:
        print(f"битых строк ряда: {плохих} (пропущены, прогон не остановлен)",
              file=sys.stderr)
    return sorted(из_, key=lambda р: р["utc"])


def dopisat_rjad(put_: str | Path, р: dict, *, derzhim_sutok: int = ДЕРЖИМ_СУТОК,
                 sejchas: float | None = None) -> dict:
    """Дописать ряд и ОБРЕЗАТЬ старое. Диск на лаборатории уже кончался раз.

    03.10 на lab-miami кончился диск («No space left on device»), и суточный
    прогон не стартовал вовсе. Ряд пишется строкой в сутки около 4 МБ при
    опросе раз в 20 с, поэтому обрезка -- часть записи, а не отдельная забота.
    """
    п = Path(put_)
    п.parent.mkdir(parents=True, exist_ok=True)
    сейчас = float(sejchas if sejchas else time.time())
    порог = сейчас - derzhim_sutok * 86400.0
    строка = json.dumps(р, ensure_ascii=False, separators=(",", ":"))
    with п.open("a", encoding="utf-8") as ф:
        ф.write(строка + "\n")
    # Обрезка -- переписыванием, и только когда есть что отрезать.
    ряды = prochitat_rjady(п)
    свежие = [x for x in ряды if float(x.get("utc") or 0) >= порог]
    отрезано = len(ряды) - len(свежие)
    if отрезано > 0:
        п.write_text("".join(json.dumps(x, ensure_ascii=False,
                                        separators=(",", ":")) + "\n"
                             for x in свежие), encoding="utf-8")
    return {"rjadov": len(свежие), "otrezano": отрезано,
            "bajt": п.stat().st_size if п.exists() else 0}


def okno_svodka(rjady: list, sostojanie: dict, окно: dict,
                *, sejchas: float | None = None) -> dict:
    """Числа одного окна: изменение БЕЗ вводов-выводов, сделки, отметки."""
    сейчас = float(sejchas if sejchas else time.time())
    к = korziny(rjady, okno_sek=окно["окно_сек"],
                korzina_sek=окно["корзина_сек"], sejchas=сейчас)
    порог = сейчас - окно["окно_сек"]
    в_окне = [р for р in rjady if float(р.get("utc") or 0) >= порог]
    из_ = {"imja": окно["имя"], "okno_sek": окно["окно_сек"],
            "korzina_sek": окно["корзина_сек"], "tochki": [], "putь": None}
    из_.update({k: v for k, v in к.items() if k != "tochki"})
    из_["tochki"] = [[т["utc"], т["lamports"]] for т in к["tochki"]]
    п = put_krivoj(к["tochki"])
    из_["put_linii"], из_["put_zalivki"] = п["liniya"], п["zalivka"]
    из_["min_lamports"], из_["max_lamports"] = п["min"], п["max"]
    из_["posledniaja_x"] = (п["x"][-1] if п["x"] else None)
    из_["posledniaja_y"] = (п["y"][-1] if п["y"] else None)
    из_.pop("putь", None)
    if в_окне:
        начало, конец = в_окне[0], в_окне[-1]
        дельта_кап = int(конец.get("kapital") or 0) - int(начало.get("kapital") or 0)
        дельта_вв = int(конец.get("vvod_vyvod") or 0) - int(начало.get("vvod_vyvod") or 0)
        # ИЗМЕНЕНИЕ -- БЕЗ ВВОДОВ И ВЫВОДОВ (слово владельца). Ввод не заработан,
        # вывод не потерян; складывать их в изменение -- врать себе.
        из_["izmenenie_lamports"] = дельта_кап - дельта_вв
        из_["vvod_vyvod_lamports"] = дельта_вв
        основа = int(начало.get("kapital") or 0)
        из_["izmenenie_pct"] = (round(из_["izmenenie_lamports"] / основа * 100.0, 4)
                                if основа else None)
        из_["kapital_nachala"] = основа
    else:
        из_.update(izmenenie_lamports=None, vvod_vyvod_lamports=None,
                   izmenenie_pct=None, kapital_nachala=None)
    сделки = [с for с in (sostojanie.get("sdelki") or [])
              if isinstance(с.get("zakryta_utc"), int)
              and float(с["zakryta_utc"]) >= порог]
    из_["sdelok"] = len(сделки)
    из_["zakrytyj_itog_lamports"] = sum(int(с.get("itog") or 0) for с in сделки)
    из_["metki"] = [м for м in (sostojanie.get("metki") or [])
                    if isinstance(м.get("utc"), int)
                    and float(м["utc"]) >= порог][-20:]
    return из_


def vygruzka(rjady: list, sostojanie: dict, *, sejchas: float | None = None) -> dict:
    """JSON для страницы: капитал, части, три окна с готовыми путями."""
    сейчас = float(sejchas if sejchas else time.time())
    последний = (rjady[-1] if rjady else None)
    из_ = {
        "что": ("капитал кошелька полосы по цепи: SOL + WSOL + открытые позиции "
                "по цене входа + рента в токен-счетах. Переводы -- отметками, в "
                "изменение за период не входят."),
        "koshelek": sostojanie.get("koshelek") or КОШЕЛЕК,
        "obnovleno_utc": int(сейчас),
        "rjadov": len(rjady),
        "ok": bool(последний),
        "why_not": (None if последний else WHY_NET_RJADOV),
    }
    if последний:
        из_.update({
            "kapital_lamports": int(последний.get("kapital") or 0),
            "kapital_sol": round(int(последний.get("kapital") or 0) / ЛАМПОРТОВ_В_SOL, 6),
            "slot": последний.get("slot"),
            "rjad_utc": последний.get("utc"),
            "chasti": {"sol": int(последний.get("sol") or 0),
                        "wsol": int(последний.get("wsol") or 0),
                        "pozicii": int(последний.get("pozicii") or 0),
                        "renta": int(последний.get("renta") or 0)},
            "pozicij": int(последний.get("pozicij") or 0),
            "schetov": int(последний.get("schetov") or 0),
            "schetov_pustyh": int(последний.get("schetov_pustyh") or 0),
            "bez_vhoda": int(последний.get("bez_vhoda") or 0),
            "bez_vhoda_minty": последний.get("bez_vhoda_minty") or [],
            "neopoznannyh": int(последний.get("neopoznannyh") or 0),
        })
    из_["sdelok_vsego"] = len(sostojanie.get("sdelki") or [])
    из_["zakryto_schetov"] = int(sostojanie.get("zakryto_schetov") or 0)
    из_["podpisej_razobrano"] = int(sostojanie.get("podpisej_razobrano") or 0)
    из_["kredity_sutki"] = dict(sostojanie.get("kredity") or {})
    из_["okna"] = {о["имя"]: okno_svodka(rjady, sostojanie, о, sejchas=сейчас)
                    for о in ОКНА}
    return из_


# ------------------------------------------------------------------- страница

СТРАНИЦА_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Капитал полосы</title>
<style>
/* ВИД -- КАК У ДОМАШНЕЙ ПАНЕЛИ ВЛАДЕЛЬЦА (ветка dashboard, index.html): те же
   имена ролей, те же числа, те же цвета состояний. Своей палитры не изобретаю:
   владелец смотрит обе страницы одними глазами, и вторая не должна выглядеть
   чужой. Тёмная тема объявлена ТРИЖДЫ, как там же: :root, системная настройка
   и переключатель -- и переключатель бьёт систему в обе стороны.
   ОТЛИЧИЕ ОДНО, И ОНО НАРОЧНО: шрифты НЕ тянутся из сети. Страница живёт за
   ссылкой с секретом, и каждый внешний адрес на ней -- это и утечка самого
   факта, и чужой код в нашей странице. Имена семейств оставлены первыми: есть
   на телефоне -- возьмутся, нет -- системные. */
:root {
  color-scheme: light;
  --bg: #f2f4f1;
  --surface: #ffffff; --surface-2: #eef0ec; --surface-3: #e4e7e2;
  --ink: #131619; --ink-2: #596068; --ink-3: #8a9199;
  --line: #e2e5e0; --line-2: #cdd2cb;
  --shadow: 0 1px 2px rgba(19,22,25,.05), 0 10px 28px -14px rgba(19,22,25,.16);
  --good: #0f8f2e; --good-bg: rgba(15,143,46,.11);
  --bad: #c93a3a; --bad-bg: rgba(201,58,58,.11);
  --accent: #2a78d6;
  --tip-bg: #131619; --tip-ink: #f4f6f3;
  --sans: "Manrope", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  padding-top: env(safe-area-inset-top, 0px);
  padding-bottom: env(safe-area-inset-bottom, 0px);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #0e1110;
    --surface: #171a19; --surface-2: #1f2321; --surface-3: #292e2b;
    --ink: #f1f3ef; --ink-2: #a3aaa3; --ink-3: #6f766f;
    --line: #262b29; --line-2: #363c39;
    --shadow: 0 1px 2px rgba(0,0,0,.35), 0 14px 34px -16px rgba(0,0,0,.6);
    --good: #3ccb5a; --good-bg: rgba(60,203,90,.13);
    --bad: #ef6363; --bad-bg: rgba(239,99,99,.14);
    --accent: #3987e5;
    --tip-bg: #f1f3ef; --tip-ink: #131619;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #0e1110;
  --surface: #171a19; --surface-2: #1f2321; --surface-3: #292e2b;
  --ink: #f1f3ef; --ink-2: #a3aaa3; --ink-3: #6f766f;
  --line: #262b29; --line-2: #363c39;
  --shadow: 0 1px 2px rgba(0,0,0,.35), 0 14px 34px -16px rgba(0,0,0,.6);
  --good: #3ccb5a; --good-bg: rgba(60,203,90,.13);
  --bad: #ef6363; --bad-bg: rgba(239,99,99,.14);
  --accent: #3987e5;
  --tip-bg: #f1f3ef; --tip-ink: #131619;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--ink);
  font: 15px/1.45 var(--sans);
  -webkit-text-size-adjust: 100%;
}
.obolochka { max-width: 820px; margin: 0 auto; padding: 16px 16px 32px; }
header { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
         margin: 2px 0 14px; }
.znak { width: 30px; height: 30px; border-radius: 9px; flex: none;
  background: linear-gradient(135deg, var(--accent), #1baf7a);
  display: grid; place-items: center; color: #fff; font-weight: 800;
  font-size: 14px; letter-spacing: -.02em; box-shadow: var(--shadow); }
header h1 { font-size: 14px; font-weight: 700; color: var(--ink-2);
            letter-spacing: .01em; margin: 0; }
.koshelek { font-size: 12px; color: var(--ink-3); font-family: var(--mono);
            font-variant-numeric: tabular-nums; }
.tema { margin-left: auto; background: var(--surface);
        border: 1px solid var(--line-2); color: var(--ink-2);
        border-radius: 10px; padding: 8px 13px; font: 700 13px/1 var(--sans);
        cursor: pointer; min-height: 36px; box-shadow: 0 1px 0 rgba(0,0,0,.03); }
/* КАРТА -- КАК .card ПАНЕЛИ: радиус 16, тонкая рамка, тень и полоска цвета
   слева. Полоска -- не украшение: по ней карта узнаётся как «наша». */
.karta { background: var(--surface); border: 1px solid var(--line);
         border-radius: 16px; padding: 16px 16px 12px; box-shadow: var(--shadow);
         position: relative; overflow: hidden; }
.karta::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0;
                 width: 4px; background: var(--accent); }
.metka { font-size: 12px; font-weight: 700; color: var(--ink-3);
         letter-spacing: .04em; text-transform: uppercase; margin: 0 0 4px; }
/* ЧИСЛА -- МОНО И ТАБЛИЧНЫЕ, как у панели: там так все балансы, и прыгающая
   ширина цифр на обновлении раз в 30 с заметна сразу. */
.geroj { font-family: var(--mono); font-size: 38px; line-height: 1.02;
         font-weight: 600; margin: 0; letter-spacing: -.01em;
         font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.geroj .ed { font-family: var(--sans); font-size: 17px; font-weight: 600;
             color: var(--ink-2); margin-left: 7px; letter-spacing: 0; }
.izmenenie { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
             margin: 9px 0 0; font-size: 14px; }
.izmenenie .chip { display: inline-flex; align-items: center; gap: 5px;
  border-radius: 999px; padding: 5px 11px; font-weight: 700;
  font-family: var(--mono); font-variant-numeric: tabular-nums; }
.rost { color: var(--good); background: var(--good-bg); }
.padenie { color: var(--bad); background: var(--bad-bg); }
.nol { color: var(--ink-2); background: var(--surface-2); }
.izmenenie .za { color: var(--ink-3); font-weight: 600; }
.okna { display: flex; gap: 6px; margin: 16px 0 8px; }
.okna button { flex: 0 0 auto; min-width: 58px; min-height: 36px;
  background: var(--surface); color: var(--ink-2);
  border: 1px solid var(--line-2); border-radius: 10px;
  font: 700 13px/1 var(--sans); cursor: pointer;
  box-shadow: 0 1px 0 rgba(0,0,0,.03); }
.okna button[aria-pressed="true"] { background: var(--accent); color: #fff;
  border-color: var(--accent); }
.grafik { position: relative; margin: 0 -2px;
          -webkit-tap-highlight-color: transparent; }
.grafik svg:focus { outline: none; }
.grafik svg:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.grafik svg { display: block; width: 100%; height: 190px; touch-action: pan-y; }
.liniya { fill: none; stroke: var(--accent); stroke-width: 3;
          stroke-linejoin: round; stroke-linecap: round;
          vector-effect: non-scaling-stroke; }
.setka { stroke: var(--line); stroke-width: 1; vector-effect: non-scaling-stroke; }
.osnova { stroke: var(--line-2); stroke-width: 1; vector-effect: non-scaling-stroke; }
.krest { stroke: var(--ink-3); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
.metka-vvoda { stroke: var(--ink-3); stroke-width: 1; stroke-dasharray: 2 3;
               vector-effect: non-scaling-stroke; }
/* ТОЧКА И ПОДПИСЬ -- НАЛОЖЕНИЕМ HTML, А НЕ ВНУТРИ РАСТЯНУТОГО SVG: круг в нём
   стал бы овалом, а буквы -- сжатыми вдвое с лишним. */
.konec-tochka { position: absolute; width: 11px; height: 11px;
  margin: -5.5px 0 0 -5.5px; border-radius: 50%; background: var(--accent);
  box-shadow: 0 0 0 3px var(--surface); pointer-events: none; }
.konec-podpis { position: absolute; transform: translate(-50%, -150%);
  font-family: var(--mono); font-size: 12px; font-weight: 600; color: var(--ink);
  font-variant-numeric: tabular-nums; white-space: nowrap; pointer-events: none;
  text-shadow: 0 0 3px var(--surface), 0 0 3px var(--surface),
               0 0 3px var(--surface); }
.vremya-osi { display: flex; justify-content: space-between; gap: 12px;
  margin: 5px 2px 0; font-size: 11px; color: var(--ink-3);
  font-family: var(--mono); font-variant-numeric: tabular-nums; }
/* ПОДСКАЗКА -- ТЁМНАЯ ПЛАШКА МОНО, как .tip панели. */
.podskazka { position: absolute; pointer-events: none; z-index: 3;
  background: var(--tip-bg); color: var(--tip-ink); border-radius: 8px;
  padding: 6px 9px; font-family: var(--mono); font-size: 12px;
  white-space: nowrap; box-shadow: var(--shadow); opacity: 0;
  transition: opacity .12s; font-variant-numeric: tabular-nums; }
.podskazka b { font-size: 13px; font-weight: 600; }
.podskazka .vremya { color: var(--ink-3); font-size: 11px; display: block; }
.chasti { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 8px; margin: 14px 0 0; }
.chasti div { background: var(--surface-2); border-radius: 12px;
              padding: 9px 11px; min-width: 0; }
.chasti dt { color: var(--ink-3); font-size: 11px; font-weight: 600;
             margin: 0 0 3px; }
.chasti dd { margin: 0; font-family: var(--mono); font-size: 15px;
             font-weight: 600; font-variant-numeric: tabular-nums;
             overflow-wrap: anywhere; }
.pod-grafikom { margin: 14px 0 0; background: var(--surface);
  border: 1px solid var(--line); border-radius: 16px; padding: 4px 14px;
  box-shadow: var(--shadow); }
.ryad { display: flex; justify-content: space-between; gap: 12px;
        padding: 10px 0; border-top: 1px solid var(--line); font-size: 14px; }
.ryad:first-child { border-top: 0; }
.ryad span:first-child { color: var(--ink-2); }
.ryad span:last-child { font-weight: 700; font-family: var(--mono);
                        font-variant-numeric: tabular-nums; }
.otmetki { margin: 4px 0 8px; padding: 0; list-style: none; }
.otmetki li { display: flex; justify-content: space-between; gap: 10px;
  font-size: 12px; color: var(--ink-2); padding: 7px 8px; margin: 4px 0;
  border-radius: 8px; background: var(--surface-2); font-family: var(--mono);
  font-variant-numeric: tabular-nums; }
.slovami { margin: 14px 2px 0; font-size: 13px; color: var(--ink-2);
           background: var(--bad-bg); border-radius: 10px; padding: 10px 12px; }
.slovami:empty { display: none; }
.slovami b { color: var(--bad); }
footer { margin: 16px 2px 0; font-size: 11px; color: var(--ink-3);
         font-family: var(--mono); }
.ustarelo { opacity: .55; transition: opacity .2s; }
@media (min-width: 560px) {
  .chasti { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .grafik svg { height: 260px; }
  .geroj { font-size: 46px; }
}
</style>
</head>
<body>
<div class="obolochka">
  <header>
    <div class="znak" aria-hidden="true">H</div>
    <h1>КАПИТАЛ ПОЛОСЫ</h1>
    <span class="koshelek" id="koshelek"></span>
    <button class="tema" id="tema" type="button">тема</button>
  </header>

  <section class="karta" id="karta">
    <p class="geroj"><span id="kapital">—</span><span class="ed">SOL</span></p>
    <p class="izmenenie" id="izmenenie"><span class="nol">—</span></p>

    <div class="okna" id="okna" role="group" aria-label="период"></div>

    <div class="grafik" id="grafik">
      <svg id="svg" viewBox="0 0 1000 320" preserveAspectRatio="none"
           role="img" aria-label="капитал по времени">
        <defs>
          <linearGradient id="zaliv" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%"  stop-color="var(--series)" stop-opacity=".22"/>
            <stop offset="100%" stop-color="var(--series)" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <g id="setka"></g>
        <path id="zalivka" fill="url(#zaliv)" d=""></path>
        <path id="liniya" class="liniya" d=""></path>
        <g id="metki"></g>
        <line id="krest" class="krest" x1="0" y1="8" x2="0" y2="312"
              style="display:none"></line>
      </svg>
      <div class="konec-tochka" id="konec" style="display:none"></div>
      <div class="konec-podpis" id="podpis" style="display:none"></div>
      <div class="podskazka" id="podskazka"></div>
    </div>
    <div class="vremya-osi"><span id="os-levo"></span><span id="os-pravo"></span></div>

    <dl class="chasti" id="chasti"></dl>
  </section>

  <section class="pod-grafikom" id="pod"></section>
  <p class="slovami" id="slovami"></p>
  <footer id="podval"></footer>
</div>

<script>
"use strict";
var ЛАМП = 1000000000;
var окноТек = "24Ч";
var данные = null;
var ОКНА_ИМЕНА = ["1Ч", "24Ч", "3Д"];

/* ВРЕМЯ -- ПО МАДРИДУ, И ЗОНУ ЗНАЕТ БРАУЗЕР. На хосте базы зон может не быть
   вовсе, а у телефона она есть всегда; поэтому время приходит метками UTC, а
   Мадрид считается здесь. Не получилось -- показываем UTC с Z, а не врём
   мадридской подписью. */
function время(секунды, сСекундами) {
  var д = new Date(секунды * 1000);
  try {
    return д.toLocaleTimeString("es-ES", {
      timeZone: "Europe/Madrid", hour: "2-digit", minute: "2-digit",
      second: сСекундами ? "2-digit" : undefined
    });
  } catch (e) {
    return д.toISOString().slice(11, сСекундами ? 19 : 16) + "Z";
  }
}
function датаВремя(секунды) {
  var д = new Date(секунды * 1000);
  try {
    return д.toLocaleString("es-ES", {
      timeZone: "Europe/Madrid", day: "2-digit", month: "2-digit",
      hour: "2-digit", minute: "2-digit"
    });
  } catch (e) { return д.toISOString().slice(0, 16).replace("T", " ") + "Z"; }
}
function sol(лампорты, знаков) {
  var з = (знаков === undefined ? 4 : знаков);
  return (лампорты / ЛАМП).toLocaleString("ru-RU",
    { minimumFractionDigits: з, maximumFractionDigits: з });
}
function текст(узел, строка) { узел.textContent = строка; }

function нарисоватьОкна() {
  var к = document.getElementById("okna");
  к.innerHTML = "";
  ОКНА_ИМЕНА.forEach(function (имя) {
    var б = document.createElement("button");
    б.type = "button";
    б.setAttribute("aria-pressed", имя === окноТек ? "true" : "false");
    текст(б, имя);
    б.addEventListener("click", function () { окноТек = имя; нарисовать(); });
    к.appendChild(б);
  });
}

function нарисоватьСетку(низ, верх) {
  var g = document.getElementById("setka");
  g.innerHTML = "";
  for (var i = 1; i <= 3; i++) {
    var y = 8 + (304 - 16) * (i / 4);
    var л = document.createElementNS("http://www.w3.org/2000/svg", "line");
    л.setAttribute("x1", 8); л.setAttribute("x2", 992);
    л.setAttribute("y1", y); л.setAttribute("y2", y);
    л.setAttribute("class", "setka");
    g.appendChild(л);
  }
  var осн = document.createElementNS("http://www.w3.org/2000/svg", "line");
  осн.setAttribute("x1", 8); осн.setAttribute("x2", 992);
  осн.setAttribute("y1", 312); осн.setAttribute("y2", 312);
  осн.setAttribute("class", "osnova");
  g.appendChild(осн);
}

function нарисовать() {
  if (!данные) { return; }
  нарисоватьОкна();
  var о = (данные.okna || {})[окноТек] || {};
  текст(document.getElementById("koshelek"),
        (данные.koshelek || "").slice(0, 4) + "…" + (данные.koshelek || "").slice(-4));
  текст(document.getElementById("kapital"),
        данные.kapital_lamports === undefined ? "—"
          : sol(данные.kapital_lamports, 4));

  /* ИЗМЕНЕНИЕ -- СО ЗНАКОМ И СО СЛОВОМ, А НЕ ОДНИМ ЦВЕТОМ. Красный и зелёный
     на одном графике неразличимы для части людей, поэтому направление несут
     знак, стрелка и подпись периода, а цвет только помогает. */
  var и = document.getElementById("izmenenie");
  и.innerHTML = "";
  var пр = document.createElement("span");
  if (о.izmenenie_lamports === null || о.izmenenie_lamports === undefined) {
    пр.className = "chip nol"; текст(пр, "ряда за период нет");
  } else {
    var в = о.izmenenie_lamports;
    пр.className = "chip " + (в > 0 ? "rost" : (в < 0 ? "padenie" : "nol"));
    var стрелка = в > 0 ? "▲" : (в < 0 ? "▼" : "•");
    var проц = (о.izmenenie_pct === null || о.izmenenie_pct === undefined)
      ? "" : "  (" + (в > 0 ? "+" : "") + о.izmenenie_pct.toFixed(2) + " %)";
    текст(пр, стрелка + " " + (в > 0 ? "+" : "") + sol(в, 4) + " SOL" + проц);
  }
  и.appendChild(пр);
  var за = document.createElement("span");
  за.className = "za"; текст(за, "за " + окноТек);
  и.appendChild(за);
  if (о.vvod_vyvod_lamports) {
    var вв = document.createElement("span");
    вв.className = "za";
    текст(вв, "· ввод/вывод " + (о.vvod_vyvod_lamports > 0 ? "+" : "")
              + sol(о.vvod_vyvod_lamports, 4) + " SOL вне изменения");
    и.appendChild(вв);
  }

  document.getElementById("liniya").setAttribute("d", о.put_linii || "");
  document.getElementById("zalivka").setAttribute("d", о.put_zalivki || "");
  нарисоватьСетку(о.min_lamports, о.max_lamports);
  var конец = document.getElementById("konec");
  var подпись = document.getElementById("podpis");
  var точки = о.tochki || [];
  if (о.posledniaja_x !== null && о.posledniaja_x !== undefined && точки.length) {
    var дx = (о.posledniaja_x / 1000) * 100;
    var дy = (о.posledniaja_y / 320) * 100;
    конец.style.display = ""; подпись.style.display = "";
    конец.style.left = дx + "%"; конец.style.top = дy + "%";
    подпись.style.left = Math.min(92, дx) + "%"; подпись.style.top = дy + "%";
    текст(подпись, sol(точки[точки.length - 1][1], 3));
  } else {
    конец.style.display = "none"; подпись.style.display = "none";
  }
  /* ВРЕМЯ ПОД ГРАФИКОМ -- ДВУМЯ ПОДПИСЯМИ: начало окна и последняя точка.
     Больше подписей на телефоне только мешают, а без них непонятно, какой
     кусок суток перед тобой. */
  текст(document.getElementById("os-levo"),
        точки.length ? датаВремя(точки[0][0]) : "");
  текст(document.getElementById("os-pravo"),
        точки.length ? датаВремя(точки[точки.length - 1][0]) : "");

  /* ОТМЕТКИ ВВОДА И ВЫВОДА -- ПУНКТИРОМ НА ГРАФИКЕ, чтобы ступенька капитала
     не читалась как заработок. */
  var gm = document.getElementById("metki");
  gm.innerHTML = "";
  var окноСек = о.okno_sek || 86400;
  var сейчас = данные.obnovleno_utc || (Date.now() / 1000);
  (о.metki || []).forEach(function (м) {
    var доля = 1 - (сейчас - м.utc) / окноСек;
    if (доля < 0 || доля > 1) { return; }
    var x = 8 + 984 * доля;
    var л = document.createElementNS("http://www.w3.org/2000/svg", "line");
    л.setAttribute("x1", x); л.setAttribute("x2", x);
    л.setAttribute("y1", 8); л.setAttribute("y2", 312);
    л.setAttribute("class", "metka-vvoda");
    gm.appendChild(л);
  });

  var ч = document.getElementById("chasti");
  ч.innerHTML = "";
  [["SOL", (данные.chasti || {}).sol], ["WSOL", (данные.chasti || {}).wsol],
   ["позиции по входу", (данные.chasti || {}).pozicii],
   ["рента счетов", (данные.chasti || {}).renta]].forEach(function (п) {
    var д = document.createElement("div");
    var dt = document.createElement("dt"); текст(dt, п[0]);
    var dd = document.createElement("dd");
    текст(dd, п[1] === undefined ? "—" : sol(п[1], 4));
    д.appendChild(dt); д.appendChild(dd); ч.appendChild(д);
  });

  var под = document.getElementById("pod");
  под.innerHTML = "";
  function ряд(левое, правое) {
    var р = document.createElement("div"); р.className = "ryad";
    var a = document.createElement("span"); текст(a, левое);
    var b = document.createElement("span"); текст(b, правое);
    р.appendChild(a); р.appendChild(b); под.appendChild(р);
  }
  ряд("сделок закрыто за " + окноТек, String(о.sdelok === undefined ? "—" : о.sdelok));
  ряд("закрытый итог за " + окноТек,
      (о.zakrytyj_itog_lamports === undefined || о.zakrytyj_itog_lamports === null)
        ? "—" : ((о.zakrytyj_itog_lamports > 0 ? "+" : "")
                 + sol(о.zakrytyj_itog_lamports, 4) + " SOL"));
  ряд("позиций открыто сейчас", String(данные.pozicij === undefined ? "—" : данные.pozicij));
  ряд("токен-счетов (из них пустых)",
      (данные.schetov === undefined ? "—" : данные.schetov)
        + " (" + (данные.schetov_pustyh === undefined ? "—" : данные.schetov_pustyh) + ")");
  ряд("точек в графике", (о.korzin_est === undefined ? "—" : о.korzin_est)
      + " из " + (о.korzin_vsego === undefined ? "—" : о.korzin_vsego));

  if ((о.metki || []).length) {
    var сп = document.createElement("ul"); сп.className = "otmetki";
    (о.metki || []).slice().reverse().forEach(function (м) {
      var li = document.createElement("li");
      var a = document.createElement("span");
      текст(a, (м.vid === "ввод" ? "ввод " : "вывод ") + sol(м.lamports, 4) + " SOL");
      var b = document.createElement("span"); текст(b, датаВремя(м.utc));
      li.appendChild(a); li.appendChild(b); сп.appendChild(li);
    });
    под.appendChild(сп);
  }

  /* ЧЕГО НЕ ЗНАЕМ -- СЛОВАМИ И ЧИСЛОМ, А НЕ ТИХИМ НУЛЁМ. */
  var сл = [];
  if (данные.bez_vhoda) {
    сл.push("позиций без цены входа: " + данные.bez_vhoda
            + " -- капитал на их вход ЗАНИЖЕН");
  }
  if (данные.neopoznannyh) {
    сл.push("непонятных транзакций: " + данные.neopoznannyh);
  }
  if (о.propuskov) {
    сл.push("пропусков в ряде: " + о.propuskov
            + (о.dolgij_propusk_sek ? " (самый долгий "
               + Math.round(о.dolgij_propusk_sek / 60) + " мин)" : ""));
  }
  if (данные.why_not) { сл.push(данные.why_not); }
  var slovami = document.getElementById("slovami");
  slovami.innerHTML = "";
  if (сл.length) {
    var b = document.createElement("b"); текст(b, "ЧЕГО НЕ ЗНАЕМ: ");
    slovami.appendChild(b);
    slovami.appendChild(document.createTextNode(сл.join("; ")));
  }
  текст(document.getElementById("podval"),
        "снято " + (данные.rjad_utc ? датаВремя(данные.rjad_utc) : "—")
        + " по Мадриду · слот " + (данные.slot || "—")
        + " · рядов " + (данные.rjadov || 0)
        + " · подписей разобрано " + (данные.podpisej_razobrano || 0));
  document.getElementById("karta").classList.remove("ustarelo");
}

/* КРЕСТ ИЩЕТ ВРЕМЯ, А НЕ ЛИНИЮ: читатель наводит на минуту, а не на два
   пикселя кривой. Подсказка показывает значение крупно, время мелко. */
(function подсказка() {
  var граф = document.getElementById("grafik");
  var svg = document.getElementById("svg");
  var крест = document.getElementById("krest");
  var окно = document.getElementById("podskazka");
  function скрыть() {
    крест.style.display = "none"; окно.style.opacity = 0;
  }
  function вести(e) {
    if (!данные) { return; }
    var о = (данные.okna || {})[окноТек] || {};
    var точки = о.tochki || [];
    if (точки.length < 2) { скрыть(); return; }
    var п = граф.getBoundingClientRect();
    var x = e.clientX - п.left;
    var доля = Math.max(0, Math.min(1, x / п.width));
    var и = Math.round(доля * (точки.length - 1));
    var вx = 8 + 984 * (и / (точки.length - 1));
    крест.setAttribute("x1", вx); крест.setAttribute("x2", вx);
    крест.style.display = "";
    окно.innerHTML = "";
    var b = document.createElement("b");
    текст(b, sol(точки[и][1], 4) + " SOL");
    var вр = document.createElement("span");
    вр.className = "vremya";
    текст(вр, время(точки[и][0], окноТек === "1Ч"));
    окно.appendChild(b); окно.appendChild(вр);
    var левое = Math.max(4, Math.min(п.width - 130, x - 60));
    окно.style.left = левое + "px";
    // ПОДСКАЗКА СТАНОВИТСЯ НАД ТОЧКОЙ, А НЕ ПОВЕРХ ЛИНИИ В УГЛУ: так видно и
    // число, и то место кривой, о котором оно говорит.
    var вy = (точки.length > 1)
      ? (о.tochki_y ? о.tochki_y[и] : null) : null;
    var верх = 6;
    if (о.posledniaja_y !== null && о.posledniaja_y !== undefined) {
      var доля_y = (точки[и][1] - (о.min_lamports || 0))
        / (((о.max_lamports || 0) - (о.min_lamports || 0)) || 1);
      верх = Math.max(2, Math.min(п.height - 54,
        (1 - доля_y) * (п.height - 16) + 8 - 52));
    }
    окно.style.top = верх + "px";
    окно.style.opacity = 1;
  }
  /* НА ТЕЛЕФОНЕ ПОДСКАЗКА НЕ ГАСНЕТ СРАЗУ. Касание -- это down, up и сразу
     leave, поэтому на pointerleave от МЫШИ подсказка прячется, а от пальца --
     живёт ещё 3 секунды: иначе она мигает и прочитать её нельзя. */
  var таймер = null;
  function поздно_скрыть() {
    if (таймер) { clearTimeout(таймер); }
    таймер = setTimeout(скрыть, 3000);
  }
  граф.addEventListener("pointermove", вести);
  граф.addEventListener("pointerdown", function (e) { вести(e); поздно_скрыть(); });
  граф.addEventListener("pointerup", поздно_скрыть);
  граф.addEventListener("pointerleave", function (e) {
    if (e.pointerType === "mouse") { скрыть(); } else { поздно_скрыть(); }
  });
  svg.addEventListener("blur", скрыть);
})();

/* ТЕМА: системная по умолчанию, выбор запоминается на этом телефоне. */
(function тема() {
  var кн = document.getElementById("tema");
  try {
    var было = localStorage.getItem("tema-kapitala");
    if (было === "dark" || было === "light") {
      document.documentElement.setAttribute("data-theme", было);
    }
  } catch (e) { /* приватное окно -- тема просто системная */ }
  кн.addEventListener("click", function () {
    var д = document.documentElement;
    var было = д.getAttribute("data-theme");
    var стало = (было === "dark") ? "light" : "dark";
    д.setAttribute("data-theme", стало);
    try { localStorage.setItem("tema-kapitala", стало); } catch (e) {}
  });
})();

/* ОБНОВЛЕНИЕ РАЗ В 30 с. Пока данные едут, прежняя картинка остаётся на месте
   приглушённой: мигать пустотой на телефоне -- худшее, что можно сделать. */
function обновить() {
  document.getElementById("karta").classList.add("ustarelo");
  fetch("kapital.json?t=" + Date.now(), { cache: "no-store" })
    .then(function (о) { return о.json(); })
    .then(function (д) { данные = д; нарисовать(); })
    .catch(function () {
      var sl = document.getElementById("slovami");
      sl.innerHTML = "";
      var b = document.createElement("b"); текст(b, "выгрузка не прочиталась");
      sl.appendChild(b);
    });
}
обновить();
setInterval(обновить, 30000);
</script>
</body>
</html>
"""


def stranica() -> str:
    """HTML страницы. Без единого внешнего ресурса: ни шрифта, ни библиотеки.

    ПОЧЕМУ БЕЗ ВНЕШНИХ РЕСУРСОВ. Страница отдаётся по ссылке с секретом, и
    каждый внешний адрес в ней -- это утечка самого факта и возможность подмены
    кода на чужой стороне. Всё, что ей нужно, лежит внутри.
    """
    return СТРАНИЦА_HTML


# ---------------------------------------------------------------- один прогон

def progon(kat: str | Path | None = None, *, rpc_call=None,
           koshelek: str = КОШЕЛЕК, sutok: float = 3.0,
           sejchas: float | None = None, pisat_stranicu: bool = True,
           derzhim_sutok: int = ДЕРЖИМ_СУТОК) -> dict:
    """ОДИН ТИК СБОРЩИКА: догнать цепь, посчитать ряд, записать всё.

    Вызывается раз в 15--30 с бегунком или циклом; состояние и ряд лежат на
    диске, поэтому перезапуск ничего не теряет, кроме времени простоя (и простой
    виден числом пропусков в выгрузке).
    """
    к = Path(kat) if kat else КАТАЛОГ
    к.mkdir(parents=True, exist_ok=True)
    сейчас = float(sejchas if sejchas else time.time())
    п_сост = к / ФАЙЛ_СОСТОЯНИЯ
    из_ = {"ok": False, "why_not": None, "kat": str(к)}
    сост = pustoe_sostojanie(koshelek)
    if п_сост.exists():
        try:
            прежнее = json.loads(п_сост.read_text(encoding="utf-8"))
            if isinstance(прежнее, dict) and прежнее.get("koshelek") == koshelek:
                сост = прежнее
            else:
                из_["why_not_sostojanie"] = ("состояние не того кошелька -- "
                                             "начато заново")
        except (ValueError, OSError) as сбой:
            из_["why_not_sostojanie"] = (f"состояние не прочиталось: "
                                          f"{type(сбой).__name__} -- начато заново")
    счётчик = Schjotchik(сост.get("kredity"))
    if rpc_call is None:
        url = uzel_iz_okruzheniya()
        if not url:
            из_["why_not"] = WHY_NET_UZLA
            return из_
        rpc_call = rpc_iz_url(url)
        из_["uzel"] = zateret(url)
    try:
        из_["dognali"] = dognat_cep(rpc_call, koshelek=koshelek, sostojanie=сост,
                                     sutok=sutok, schjotchik=счётчик,
                                     sejchas=сейчас)
        кэш = сост.get("scheta_snimok") or {}
        возраст = сейчас - float(кэш.get("utc") or 0)
        читать_счета = bool(
            int(из_["dognali"].get("razobrano") or 0)       # прошла транзакция
            or not кэш.get("scheta")                        # первый тик
            or возраст >= СЧЕТА_НЕ_СТАРШЕ_СЕК)              # страховка
        из_["scheta_chitali"] = читать_счета
        р = rjad(rpc_call, koshelek=koshelek, sostojanie=сост,
                 schjotchik=счётчик, sejchas=сейчас,
                 schitat_scheta=читать_счета)
    except (ОшибкаТрекера, OSError, ValueError) as сбой:
        # УЗЕЛ МОЛЧИТ -- ЭТО НЕ ПОТЕРЯ РЯДА, А ПРОПУСК, И ОН НАЗЫВАЕТСЯ.
        из_["why_not"] = f"{type(сбой).__name__}: {zateret(str(сбой))[:160]}"
        сост["kredity"] = счётчик.po_sutkam
        п_сост.write_text(json.dumps(сост, ensure_ascii=False, indent=1),
                          encoding="utf-8")
        return из_
    счётчик.obrezat()
    сост["kredity"] = счётчик.po_sutkam
    п_сост.write_text(json.dumps(сост, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8")
    из_["zapis"] = dopisat_rjad(к / ФАЙЛ_РЯДОВ, р, derzhim_sutok=derzhim_sutok,
                                 sejchas=сейчас)
    ряды = prochitat_rjady(к / ФАЙЛ_РЯДОВ)
    в = vygruzka(ряды, сост, sejchas=сейчас)
    (к / ФАЙЛ_ВЫГРУЗКИ).write_text(json.dumps(в, ensure_ascii=False, indent=1)
                                   + "\n", encoding="utf-8")
    if pisat_stranicu:
        (к / ФАЙЛ_СТРАНИЦЫ).write_text(stranica(), encoding="utf-8")
    из_.update(ok=True, rjad=р, vygruzka_bajt=len(json.dumps(в)),
               kapital_sol=round(int(р["kapital"]) / ЛАМПОРТОВ_В_SOL, 6),
               vyzovov_za_sutki=(счётчик.po_sutkam.get(
                   time.strftime("%Y-%m-%d", time.gmtime(сейчас))) or {}))
    return из_


# ------------------------------------------------------------------- отдача

def sekret_iz_okruzheniya() -> str:
    """Секрет пути -- ТОЛЬКО из окружения.

    КЛЮЧОМ КОМАНДНОЙ СТРОКИ ЕГО ЗДЕСЬ НЕТ ВОВСЕ, и это не придирка: аргументы
    процесса видит любой на машине (ps), а в логе прогона -- каждый, кто
    откроет лог. Нет секрета -- служба не поднимается и говорит словами.
    """
    return (os.environ.get("BLOOM_TREKKER_SEKRET") or "").strip()


def servis(kat: str | Path | None = None, *, sekret: str | None = None,
           port: int = 8787, adres: str = "127.0.0.1",
           odin_zapros: bool = False, ne_sluzhit: bool = False):
    """Отдача страницы и выгрузки по ссылке с секретом в пути.

    ЧТО ЗДЕСЬ СДЕЛАНО РАДИ СЕКРЕТА, А НЕ РАДИ УДОБСТВА:
      * секрет берётся из окружения (см. выше) и сравнивается постоянным по
        времени сравнением -- чтобы по времени ответа его нельзя было угадать
        побайтово;
      * ПУТЬ НЕ ПОПАДАЕТ В ЖУРНАЛ НИКОГДА: log_message переписан и печатает
        только метод и код. Обычный http.server пишет путь целиком, то есть
        положил бы секрет в лог прогона и в терминал;
      * неверный секрет и неверный путь отвечают ОДИНАКОВО (404 и одна строка):
        иначе ответ подсказывал бы, что секрет угадан.
    """
    import hmac  # noqa: PLC0415
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: PLC0415

    к = Path(kat) if kat else КАТАЛОГ
    с = (sekret if sekret is not None else sekret_iz_okruzheniya())
    if not с or len(с) < 16:
        raise ОшибкаТрекера(
            "секрет пути не задан или короче 16 знаков: задайте окружением "
            "BLOOM_TREKKER_SEKRET (ключа командной строки для него нет нарочно)")

    class Ruchka(BaseHTTPRequestHandler):
        server_version = "kapital"
        sys_version = ""

        def log_message(self, format, *args):  # noqa: A002, ARG002
            # ПУТИ ЗДЕСЬ НЕТ. Только метод и код -- остальное секрет.
            sys.stderr.write(f"{self.command} -> {args[1] if len(args) > 1 else '?'}\n")

        def _otkaz(self):
            тело = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(тело)))
            self.end_headers()
            self.wfile.write(тело)

        def _otdat(self, тело: bytes, тип: str):
            self.send_response(200)
            self.send_header("Content-Type", тип)
            self.send_header("Content-Length", str(len(тело)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(тело)

        def do_GET(self):  # noqa: N802
            путь = (self.path or "/").split("?", 1)[0]
            части = [ч for ч in путь.split("/") if ч]
            if not части or not hmac.compare_digest(части[0], с):
                self._otkaz()
                return
            хвост = "/".join(части[1:])
            if хвост in ("", "index.html", ФАЙЛ_СТРАНИЦЫ):
                self._otdat(stranica().encode("utf-8"),
                            "text/html; charset=utf-8")
                return
            if хвост == ФАЙЛ_ВЫГРУЗКИ:
                п = к / ФАЙЛ_ВЫГРУЗКИ
                if not п.exists():
                    self._otdat(json.dumps({"ok": False,
                                            "why_not": WHY_NET_RJADOV},
                                           ensure_ascii=False).encode(),
                                "application/json; charset=utf-8")
                    return
                self._otdat(п.read_bytes(), "application/json; charset=utf-8")
                return
            self._otkaz()

    сервер = ThreadingHTTPServer((adres, int(port)), Ruchka)
    if ne_sluzhit:
        # Самопроверке нужен ЖИВОЙ сервер на свободном порту, но служить он
        # должен не здесь: иначе проверка повисла бы навсегда.
        return сервер
    if odin_zapros:
        сервер.handle_request()
        сервер.server_close()
        return сервер
    try:
        сервер.serve_forever()
    finally:
        сервер.server_close()
    return сервер


def sluzhba(kat: str | Path | None = None, *, koshelek: str = КОШЕЛЕК,
            pauza: float = 20.0, sutok: float = 3.0, port: int = 8787,
            adres: str = "127.0.0.1", tikov: int = 0,
            sejchas=None) -> dict:
    """СБОРЩИК И ОТДАЧА В ОДНОМ ПРОЦЕССЕ -- чтобы на хосте была ОДНА служба.

    Две службы (сборщик и сервер) означали бы два юнита, два перезапуска и два
    места, где можно забыть секрет. Сервер живёт в побочном потоке, сборщик --
    в главном; упал сборщик -- упала вся служба, и systemd поднимет её целиком.

    tikov > 0 -- столько тиков и выйти (для проверки); 0 -- навсегда.
    """
    import threading  # noqa: PLC0415

    к = Path(kat) if kat else КАТАЛОГ
    к.mkdir(parents=True, exist_ok=True)
    сервер = servis(к, port=port, adres=adres, ne_sluzhit=True)
    поток = threading.Thread(target=сервер.serve_forever, daemon=True)
    поток.start()
    из_ = {"port": сервер.server_address[1], "adres": adres, "tikov": 0,
            "sboev": 0, "kat": str(к)}
    print(json.dumps({"служба": "поднята", "порт": из_["port"],
                      "адрес": adres, "каталог": str(к),
                      "узел": zateret(uzel_iz_okruzheniya()),
                      "пауза_сек": pauza}, ensure_ascii=False), flush=True)
    try:
        while True:
            р = progon(к, koshelek=koshelek, sutok=sutok)
            из_["tikov"] += 1
            из_["sboev"] += (not р.get("ok"))
            # ТИХИМ СБОЙ НЕ БЫВАЕТ: каждая неудача тика печатается строкой, её
            # видно в journalctl, и по ней считается пропуск в ряде.
            print(json.dumps({"тик": из_["tikov"], "ok": bool(р.get("ok")),
                              "капитал_sol": р.get("kapital_sol"),
                              "why_not": р.get("why_not"),
                              "счета_читали": р.get("scheta_chitali"),
                              "вызовов_за_сутки": (р.get("vyzovov_za_sutki")
                                                   or {}).get("всего")},
                             ensure_ascii=False), flush=True)
            if tikov and из_["tikov"] >= tikov:
                return из_
            time.sleep(max(1.0, float(pauza)))
    finally:
        сервер.shutdown()
        сервер.server_close()


# ------------------------------------------------------------- самопроверка

# ЧИСЛО ПРОВЕРОК ОБЪЯВЛЕНО ЗАРАНЕЕ: меньше -- значит что-то пропущено молча, и
# это считается провалом, а не мелочью.
ZHDEM_PROVEROK = 60
# СУТОЧНЫЕ ИТОГИ УЧЁТА ПОЛОСЫ -- ЗАМЕР ПО ФАЙЛУ data/sdelki_polosy_vse_s_2709.json
# (снят 03.10T17:07Z, 578 рядов), поле «итог_po_cepi_sol» КИРИЛЛИЦЕЙ. Числа
# объявлены, чтобы смена файла была видна числом, а не молчанием.
ЖДЁМ_УЧЁТА = {
    "2026-10-01": {"рядов": 183, "числом": 173, "сумма": 1.337476},
    "2026-10-02": {"рядов": 183, "числом": 178, "сумма": 0.323866},
    "2026-10-03": {"рядов": 68, "числом": 65, "сумма": 0.914344},
}
ФАЙЛ_УЧЁТА = "sdelki_polosy_vse_s_2709.json"
# СУТКИ У УЧЁТА ПОЛОСЫ -- МАДРИДСКИЕ, А НЕ UTC (bloom_exec_state.py:48
# DAY_TZ="Europe/Madrid"; у суточного счёта «начало_суток_utc» 22:00:00Z). И
# владелец просил страницу по Мадриду. Поэтому вторая сверка идёт по ОКНУ
# ХОСТОВОЙ ВЫГРУЗКИ: это число опубликовано самой полосой, а не посчитано мной.
ФАЙЛ_СУТОК_МАДРИД = "sdelki_polosy_2026-10-02_sutki-madrid-02-10.json"
ЖДЁМ_СУТОК_МАДРИД = {"s": "2026-10-01T22:00:00", "do": "2026-10-02T22:00:00",
                     "sdelok": 171, "chislom": 166, "summa": 0.657385977}
# В окне 01--03.10 все ряды -- кошелька полосы: второй кошелёк
# (21DqHDDPEf...) встречается только 27.09, 13 рядов из 578.
ЖДЁМ_KOSHELKOV = {"4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x": 565,
                  "21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w": 13}
# ЖИВАЯ ТРАНЗАКЦИЯ НАШЕГО КОШЕЛЬКА -- одна в репозитории, и она названа.
ФАЙЛ_ЖИВОЙ_TX = "bloom_tx_raw.json"
ЖДЁМ_ЖИВОЙ_TX = {"sig": "3wDEDSUUFhv7PP11", "vse_sol": -0.065084518,
                 "renta": 1513840, "vid": "своп"}
ЖДЁМ_СВЕРКИ_С_ЧУЖОЙ = 138


def _tx(*, kljuchi: list, do: list, posle: list, token_do=None,
        token_posle=None, programmy=None, fee: int = 5000,
        slot: int = 1, utc: int = 1_700_000_000, sig: str = "sig",
        err=None) -> dict:
    """Поддельная транзакция -- ровно той формы, что отдаёт узел. Для фикстур."""
    def токены(лист):
        из_ = []
        for и, владелец, минт, сумма in (лист or []):
            из_.append({"accountIndex": и, "owner": владелец, "mint": минт,
                         "uiTokenAmount": {"amount": str(сумма), "decimals": 6}})
        return из_
    сообщение = {"accountKeys": [{"pubkey": к} for к in kljuchi],
                  "instructions": [{"programId": п} for п in (programmy or [])]}
    return {"slot": slot, "blockTime": utc,
            "transaction": {"signatures": [sig], "message": сообщение},
            "meta": {"err": err, "fee": fee, "preBalances": do,
                      "postBalances": posle,
                      "preTokenBalances": токены(token_do),
                      "postTokenBalances": токены(token_posle)}}


def _poddelnyj_uzel(*, sol: int, scheta: list, podpisi=None, tranzakcii=None):
    """Узел, которого нет: отдаёт заданное состояние. Ни одного байта по сети."""
    зовы = []

    def _зов(метод, параметры):
        зовы.append(метод)
        if метод == "getBalance":
            return {"result": {"context": {"slot": 777}, "value": sol}}
        if метод == "getTokenAccountsByOwner":
            прог = (параметры[1] or {}).get("programId")
            значения = []
            for с in scheta:
                if с.get("programma", ПРОГ_ТОКЕНА) != прог:
                    continue
                инфо = {"mint": с["mint"],
                         "tokenAmount": {"amount": str(с.get("amount") or 0),
                                          "decimals": с.get("decimals", 6)}}
                if с.get("renta_rezerv") is not None:
                    инфо["rentExemptReserve"] = {"amount": str(с["renta_rezerv"])}
                значения.append({"pubkey": с.get("adres") or "ата",
                                  "account": {"lamports": с["lamports"],
                                               "data": {"parsed": {"info": инфо}}}})
            return {"result": {"context": {"slot": 777}, "value": значения}}
        if метод == "getSignaturesForAddress":
            return {"result": list(podpisi or [])}
        if метод == "getTransaction":
            return {"result": (tranzakcii or {}).get(параметры[0])}
        return {"result": None}

    _зов.зовы = зовы
    return _зов


def self_test() -> int:  # noqa: C901, PLR0912, PLR0915
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import threading  # noqa: PLC0415

    было, плохо = 0, 0

    def chk(имя, усл, факт=None):
        nonlocal было, плохо
        было += 1
        if усл:
            print(f"  ok   {имя}")
        else:
            плохо += 1
            print(f" ПЛОХО {имя} -- {факт!r}")

    print("c3_kapital_trekker: самопроверка")
    врем = Path(tempfile.mkdtemp(prefix="kapital-proverka-"))
    try:
        КОШ = "KoshelekProverki1111111111111111111111111111"
        АТА = "AtaProverki111111111111111111111111111111111"
        МИНТ = "MintProverki11111111111111111111111111111111"
        РЕНТА = 2_039_280
        SOL0 = 10 * ЛАМПОРТОВ_В_SOL
        БИЛЕТ = ЛАМПОРТОВ_В_SOL  # 1 SOL ушёл из кошелька на покупке

        # ---------------------------------- 1. ТРИ ТОЖДЕСТВА КАПИТАЛА
        к0 = kapital(SOL0, [], {})
        chk("пустой кошелёк: капитал равен SOL", к0["kapital"] == SOL0, к0)
        tx_покупки = _tx(kljuchi=[КОШ, АТА, "pool"],
                          do=[SOL0, 0, 0],
                          posle=[SOL0 - БИЛЕТ, РЕНТА, 0],
                          token_do=[], token_posle=[(1, КОШ, МИНТ, 1000)],
                          programmy=["Pump1111111111111111111111111111111111111111"],
                          sig="buy")
        р_п = razbor_tranzakcii(tx_покупки, КОШ)
        chk("покупка: вид «своп» и потрачено = ушедшее МИНУС рента нового счёта",
            р_п["vid"] == ВИД_СВОП and р_п["potracheno"] == БИЛЕТ - РЕНТА,
            (р_п["vid"], р_п["potracheno"], БИЛЕТ - РЕНТА))
        к1 = kapital(SOL0 - БИЛЕТ,
                      [{"mint": МИНТ, "lamports": РЕНТА, "amount": 1000}],
                      {МИНТ: р_п["potracheno"]})
        chk("ПОКУПКА КАПИТАЛ НЕ ДВИГАЕТ -- до лампорта",
            к1["kapital"] == к0["kapital"], (к1["kapital"], к0["kapital"]))
        # продажа: пришло 1.1 SOL, счёт закрыт -- рента вернулась
        пришло = int(1.1 * ЛАМПОРТОВ_В_SOL)
        tx_продажи = _tx(kljuchi=[КОШ, АТА, "pool"],
                          do=[SOL0 - БИЛЕТ, РЕНТА, 0],
                          posle=[SOL0 - БИЛЕТ + пришло, 0, 0],
                          token_do=[(1, КОШ, МИНТ, 1000)],
                          token_posle=[(1, КОШ, МИНТ, 0)],
                          programmy=["Pump1111111111111111111111111111111111111111"],
                          sig="sell")
        р_пр = razbor_tranzakcii(tx_продажи, КОШ)
        chk("продажа: получено = пришедшее МИНУС вернувшаяся рента",
            р_пр["polucheno"] == пришло - РЕНТА,
            (р_пр["polucheno"], пришло - РЕНТА))
        к2 = kapital(SOL0 - БИЛЕТ + пришло, [], {})
        итог_сделки = р_пр["polucheno"] - р_п["potracheno"]
        chk("ПРОДАЖА ДВИГАЕТ КАПИТАЛ РОВНО НА ИТОГ СДЕЛКИ",
            к2["kapital"] - к1["kapital"] == итог_сделки,
            (к2["kapital"] - к1["kapital"], итог_сделки))
        # закрытие пустого счёта отдельной транзакцией
        tx_закрытия = _tx(kljuchi=[КОШ, АТА], do=[SOL0, РЕНТА],
                           posle=[SOL0 + РЕНТА, 0],
                           token_do=[(1, КОШ, МИНТ, 0)],
                           token_posle=[],
                           programmy=[ПРОГ_ТОКЕНА], sig="close")
        р_з = razbor_tranzakcii(tx_закрытия, КОШ)
        к3а = kapital(SOL0, [{"mint": МИНТ, "lamports": РЕНТА, "amount": 0}], {})
        к3б = kapital(SOL0 + РЕНТА, [], {})
        chk("ЗАКРЫТИЕ СЧЁТА КАПИТАЛ НЕ ДВИГАЕТ: рента переехала в SOL",
            р_з["vid"] == ВИД_ЗАКРЫТИЕ and р_з["potracheno"] == 0
            and р_з["polucheno"] == 0 and к3а["kapital"] == к3б["kapital"],
            (р_з["vid"], р_з["potracheno"], р_з["polucheno"]))
        # перевод: только системная программа
        tx_ввода = _tx(kljuchi=["Chuzhoj1111111111111111111111111111111111111", КОШ],
                        do=[5 * ЛАМПОРТОВ_В_SOL, SOL0],
                        posle=[4 * ЛАМПОРТОВ_В_SOL, SOL0 + ЛАМПОРТОВ_В_SOL],
                        programmy=[ПРОГ_СИСТЕМЫ], sig="vvod")
        р_в = razbor_tranzakcii(tx_ввода, КОШ)
        chk("ввод SOL: вид «перевод» и число равно самому вводу",
            р_в["vid"] == ВИД_ПЕРЕВОД and perevod_sol(р_в) == ЛАМПОРТОВ_В_SOL,
            (р_в["vid"], perevod_sol(р_в)))
        # РАСХОД НЕ ПЕРЕВОДОМ: рента таблицы адресов -- та самая живая ловушка
        tx_alt = _tx(kljuchi=[КОШ, "Alt11111111111111111111111111111111111111111"],
                      do=[SOL0, 0], posle=[SOL0 - 817800, 817800],
                      programmy=["AddressLookupTab1e1111111111111111111111111"],
                      sig="alt")
        р_alt = razbor_tranzakcii(tx_alt, КОШ)
        chk("рента таблицы адресов -- НЕ перевод, а «иное»: расход остаётся в "
            "капитале, а не уходит из изменения отметкой",
            р_alt["vid"] == ВИД_ИНОЕ and perevod_sol(р_alt) == 0,
            (р_alt["vid"], р_alt["pochemu"]))

        # ---------------------------------- 2. WSOL СЧИТАЕТСЯ РОВНО ОДИН РАЗ
        завёрнуто = int(0.5 * ЛАМПОРТОВ_В_SOL)
        счёт_wsol = {"mint": WSOL, "lamports": завёрнуто + РЕНТА,
                      "amount": завёрнуто, "renta_rezerv": РЕНТА}
        кw = kapital(SOL0, [счёт_wsol], {})
        chk("WSOL: завёрнутое плюс рента этого счёта равны его лампортам "
            "(двойного счёта нет)",
            кw["wsol"] == завёрнуто and кw["renta"] == РЕНТА
            and кw["wsol"] + кw["renta"] == счёт_wsol["lamports"], кw)
        chk("WSOL без rentExemptReserve считается так же: лампорты минус остаток",
            renta_scheta({"mint": WSOL, "lamports": завёрнуто + РЕНТА,
                           "amount": завёрнуто}) == РЕНТА, None)
        кw2 = kapital(SOL0, [счёт_wsol], {WSOL: 999})
        chk("позиция с минтом WSOL в капитал НЕ идёт: он уже посчитан завёрнутым",
            кw2["kapital"] == кw["kapital"] and кw2["pozicij"] == 0, кw2)
        chk("рента обычного счёта -- все его лампорты",
            renta_scheta({"mint": МИНТ, "lamports": РЕНТА, "amount": 1000})
            == РЕНТА, None)

        # ---------------------------------- 3. СВЕРКА С ЧУЖОЙ ФОРМУЛОЙ
        # Мой разбор обязан совпасть с c2_itog_po_cepi.дельта_транзакции НА
        # ЖИВЫХ транзакциях: это чужой модуль и каноническое число учёта.
        сверено = сошлось = 0
        try:
            import c2_common as C  # noqa: PLC0415
            import c2_itog_po_cepi as IC  # noqa: PLC0415
            import c2_swap_build as B  # noqa: PLC0415
            for имя in ("CPMM", "PUMP_AMM", "LAUNCHLAB", "BONDING", "DLMM", "CLMM"):
                прог = getattr(B, имя, None)
                if not прог:
                    continue
                for r in B.load_samples(прог)[:25]:
                    tx = r.get("tx")
                    if not isinstance(tx, dict):
                        continue
                    подписанты = sorted(C.signers(tx) or [])
                    if not подписанты:
                        continue
                    их = IC.дельта_транзакции(tx, подписанты[0])
                    мой = razbor_tranzakcii(tx, подписанты[0])
                    if not их.get("ok") or not мой.get("ok"):
                        continue
                    сверено += 1
                    чистое = (мой["sol_delta"] + мой["renta_delta"]) / ЛАМПОРТОВ_В_SOL
                    сошлось += (round(чистое, 9) == их["все_sol"])
        except Exception as сбой:  # noqa: BLE001
            chk("сверка с чужой формулой прошла без исключения", False,
                f"{type(сбой).__name__}: {сбой}")
        chk(f"моя арифметика сошлась с c2_itog_po_cepi.дельта_транзакции на "
            f"{ЖДЁМ_СВЕРКИ_С_ЧУЖОЙ} живых транзакциях",
            сверено == ЖДЁМ_СВЕРКИ_С_ЧУЖОЙ and сошлось == сверено,
            (сверено, сошлось))
        п_жив = КОРЕНЬ / "data" / ФАЙЛ_ЖИВОЙ_TX
        chk(f"живая транзакция нашего кошелька на месте: data/{ФАЙЛ_ЖИВОЙ_TX}",
            п_жив.exists(), str(п_жив))
        if п_жив.exists():
            д_ж = json.loads(п_жив.read_text(encoding="utf-8"))
            сиг_ж = sorted(д_ж)[0]
            tx_ж = (д_ж[сиг_ж] or {}).get("tx") or {}
            м_ж = razbor_tranzakcii(tx_ж, КОШЕЛЕК)
            chk(f"на ней: вид «{ЖДЁМ_ЖИВОЙ_TX['vid']}», рента нового счёта "
                f"{ЖДЁМ_ЖИВОЙ_TX['renta']}, чистое {ЖДЁМ_ЖИВОЙ_TX['vse_sol']}",
                м_ж["vid"] == ЖДЁМ_ЖИВОЙ_TX["vid"]
                and м_ж["renta_delta"] == ЖДЁМ_ЖИВОЙ_TX["renta"]
                and round((м_ж["sol_delta"] + м_ж["renta_delta"])
                          / ЛАМПОРТОВ_В_SOL, 9) == ЖДЁМ_ЖИВОЙ_TX["vse_sol"],
                (м_ж["vid"], м_ж["renta_delta"],
                 (м_ж["sol_delta"] + м_ж["renta_delta"]) / ЛАМПОРТОВ_В_SOL))

        # ---------------------------------- 4. СОСТОЯНИЕ: ПОЗИЦИИ И СДЕЛКИ
        сост = pustoe_sostojanie(КОШ)
        prinjat_tranzakciju(сост, tx_покупки, КОШ)
        chk("покупка открыла позицию с ценой входа",
            (сост["pozicii"].get(МИНТ) or {}).get("vhod") == БИЛЕТ - РЕНТА,
            сост["pozicii"])
        prinjat_tranzakciju(сост, tx_продажи, КОШ)
        chk("продажа закрыла позицию и записала ИТОГ сделки",
            not сост["pozicii"] and len(сост["sdelki"]) == 1
            and сост["sdelki"][0]["itog"] == итог_сделки,
            (сост["pozicii"], сост["sdelki"]))
        # ЧАСТИЧНАЯ ПРОДАЖА: вход делится по доле, остаток остаётся позицией
        сост2 = pustoe_sostojanie(КОШ)
        prinjat_tranzakciju(сост2, tx_покупки, КОШ)
        tx_часть = _tx(kljuchi=[КОШ, АТА, "pool"],
                        do=[SOL0 - БИЛЕТ, РЕНТА, 0],
                        posle=[SOL0 - БИЛЕТ + пришло // 2, РЕНТА, 0],
                        token_do=[(1, КОШ, МИНТ, 1000)],
                        token_posle=[(1, КОШ, МИНТ, 400)],
                        programmy=["Pump1111111111111111111111111111111111111111"],
                        sig="sell-part")
        prinjat_tranzakciju(сост2, tx_часть, КОШ)
        ост = (сост2["pozicii"].get(МИНТ) or {})
        chk("частичная продажа: вход поделён по доле, остаток остался позицией",
            len(сост2["sdelki"]) == 1
            and сост2["sdelki"][0]["chast"] == 0.6
            and ост.get("ostatok") == 400
            and ост.get("vhod") == (БИЛЕТ - РЕНТА) - int(round((БИЛЕТ - РЕНТА) * 0.6)),
            (сост2["sdelki"], ост))
        # ПРОДАЖА БЕЗ ИЗВЕСТНОГО ВХОДА -- НЕ ВЫДУМАННЫЙ ИТОГ, А ЧИСЛО НЕПОНЯТНЫХ
        сост3 = pustoe_sostojanie(КОШ)
        prinjat_tranzakciju(сост3, tx_продажи, КОШ)
        chk("продажа без известной покупки: сделка НЕ выдумывается, растёт "
            "число непонятных",
            not сост3["sdelki"] and сост3["neopoznannyh"] == 1, сост3)
        # ДВА МИНТА В ОДНОЙ ПОКУПКЕ -- ДЕЛИТЬ ПОТРАЧЕННОЕ НЕЧЕМ
        tx_два = _tx(kljuchi=[КОШ, АТА, "ata2"],
                      do=[SOL0, 0, 0],
                      posle=[SOL0 - БИЛЕТ, РЕНТА, РЕНТА],
                      token_do=[],
                      token_posle=[(1, КОШ, МИНТ, 1000),
                                   (2, КОШ, "Minт2222222222222222222222222222222222222222", 5)],
                      programmy=["Pump1111111111111111111111111111111111111111"],
                      sig="two")
        сост4 = pustoe_sostojanie(КОШ)
        prinjat_tranzakciju(сост4, tx_два, КОШ)
        chk("два минта в одной покупке: позиции НЕ трогаются, число непонятных "
            "растёт -- и капитал честно покажет убыль SOL",
            not сост4["pozicii"] and сост4["neopoznannyh"] == 1, сост4)

        # ---------------------------------- 5. РЯД ПО ПОДДЕЛЬНОМУ УЗЛУ
        узел = _poddelnyj_uzel(
            sol=SOL0 - БИЛЕТ,
            scheta=[{"mint": МИНТ, "lamports": РЕНТА, "amount": 1000,
                      "adres": АТА},
                     {"mint": WSOL, "lamports": завёрнуто + РЕНТА,
                      "amount": завёрнуто, "renta_rezerv": РЕНТА,
                      "adres": "wsol", "programma": ПРОГ_ТОКЕНА}],
            podpisi=[{"signature": "buy", "blockTime": 1_700_000_000}],
            tranzakcii={"buy": tx_покупки})
        сост5 = pustoe_sostojanie(КОШ)
        счёт = Schjotchik()
        dognat_cep(узел, koshelek=КОШ, sostojanie=сост5, sutok=3,
                   schjotchik=счёт, sejchas=1_700_000_100)
        р5 = rjad(узел, koshelek=КОШ, sostojanie=сост5, schjotchik=счёт,
                  sejchas=1_700_000_100)
        chk("ряд по поддельному узлу: капитал = SOL + WSOL + позиция + рента",
            р5["kapital"] == (SOL0 - БИЛЕТ) + завёрнуто + (БИЛЕТ - РЕНТА)
            + 2 * РЕНТА,
            (р5["kapital"], (SOL0 - БИЛЕТ) + завёрнуто + (БИЛЕТ - РЕНТА) + 2 * РЕНТА))
        chk("в ряде названы части, позиции и счета -- а не только итог",
            р5["pozicij"] == 1 and р5["schetov"] == 2 and р5["bez_vhoda"] == 0
            and р5["wsol"] == завёрнуто, р5)
        # ПОЗИЦИЯ БЕЗ ЦЕНЫ ВХОДА -- ЧИСЛОМ, А НЕ ТИХИМ НУЛЁМ
        р6 = rjad(узел, koshelek=КОШ, sostojanie=pustoe_sostojanie(КОШ),
                  sejchas=1_700_000_100)
        chk("позиция без цены входа: капитал занижен, и это названо числом",
            р6["bez_vhoda"] == 1 and р6["pozicii"] == 0
            and р6["bez_vhoda_minty"] == [МИНТ], р6)
        chk("кредиты считаются по методам и по суткам",
            any("getBalance" in (v or {}) for v in счёт.po_sutkam.values())
            and any("getTransaction" in (v or {}) for v in счёт.po_sutkam.values()),
            счёт.po_sutkam)
        сутки_счёта = next(iter(счёт.po_sutkam.values()))
        chk("цена вызова взята из таблицы репозитория: все методы трекера по "
            "одному кредиту, и это отдельное число рядом со счётом вызовов",
            сутки_счёта.get("кредитов_если_helius") == сутки_счёта.get("всего"),
            сутки_счёта)
        # СЧЕТА ЧИТАЮТСЯ РЕЖЕ БАЛАНСА: 404 счёта -- это 300 КБ на вызов.
        кат_к = врем / "kesh"
        узел_к = _poddelnyj_uzel(
            sol=SOL0, scheta=[{"mint": МИНТ, "lamports": РЕНТА, "amount": 1000,
                                "adres": АТА}],
            podpisi=[], tranzakcii={})
        р_к1 = progon(кат_к, rpc_call=узел_к, koshelek=КОШ, sejchas=1_700_000_000)
        было_счетов = узел_к.зовы.count("getTokenAccountsByOwner")
        р_к2 = progon(кат_к, rpc_call=узел_к, koshelek=КОШ, sejchas=1_700_000_040)
        стало_счетов = узел_к.зовы.count("getTokenAccountsByOwner")
        chk("второй тик БЕЗ новых подписей счета не перечитывает, и возраст "
            "снимка стоит числом в ряде",
            р_к1["ok"] and р_к2["ok"] and стало_счетов == было_счетов
            and р_к2["scheta_chitali"] is False
            and р_к2["rjad"]["scheta_vozrast_sek"] == 40,
            (было_счетов, стало_счетов, р_к2.get("scheta_chitali"),
             р_к2["rjad"].get("scheta_vozrast_sek")))
        р_к3 = progon(кат_к, rpc_call=_poddelnyj_uzel(
            sol=SOL0, scheta=[{"mint": МИНТ, "lamports": РЕНТА, "amount": 1000,
                                "adres": АТА}],
            podpisi=[{"signature": "buy", "blockTime": 1_700_000_000}],
            tranzakcii={"buy": tx_покупки}), koshelek=КОШ, sejchas=1_700_000_080)
        chk("новая подпись заставляет перечитать счета: позиция без цены входа "
            "жить не должна",
            р_к3["ok"] and р_к3["scheta_chitali"] is True
            and р_к3["rjad"]["pozicij"] == 1, р_к3.get("scheta_chitali"))

        # ---------------------------------- 6. СВЁРТКА И КРИВАЯ
        основа = 1_700_000_000
        ряды = [{"utc": основа + i * 20, "kapital": 10 * ЛАМПОРТОВ_В_SOL + i,
                  "vvod_vyvod": 0} for i in range(180)]
        к1ч = korziny(ряды, okno_sek=3600, korzina_sek=60,
                      sejchas=основа + 180 * 20)
        # ЧИСЛА ЗДЕСЬ -- ЗАМЕР, А НЕ ОЖИДАНИЕ «ПО ЛОГИКЕ». Окно 3600 с при
        # шаге 20 с и невыровненном начале даёт 61 минутную корзину (границы
        # корзин кратны минуте, начало окна -- нет), а первая корзина держит
        # ряды 1700000000 и 1700000020, то есть последний в ней -- второй.
        chk("свёртка 1Ч: корзина минутная, значение -- ПОСЛЕДНЕЕ в корзине "
            "(61 корзина, первая -- второй ряд)",
            к1ч["korzin_vsego"] == 61 and к1ч["korzin_est"] == 61
            and к1ч["tochki"][0]["lamports"] == 10 * ЛАМПОРТОВ_В_SOL + 1,
            (к1ч["korzin_vsego"], к1ч["korzin_est"], к1ч["tochki"][:2]))
        ряды_дыра = [р for р in ряды if not (60 <= (р["utc"] - основа) // 20 < 120)]
        к_дыра = korziny(ряды_дыра, okno_sek=3600, korzina_sek=60,
                         sejchas=основа + 180 * 20)
        # Вырезано 60 рядов подряд (20 минут): корзин без данных 19, самый
        # долгий пропуск 1140 с. Числа замерены на этой же фикстуре.
        chk("пропуск в ряде -- числом и самым долгим пропуском, а не ровной "
            "линией (19 корзин, 1140 с)",
            к_дыра["propuskov"] == 19 and к_дыра["dolgij_propusk_sek"] == 1140,
            (к_дыра["propuskov"], к_дыра["dolgij_propusk_sek"]))
        chk("окна и корзины -- те, что названы владельцем (1Ч/24Ч/3Д)",
            [(о["имя"], о["окно_сек"], о["корзина_сек"]) for о in ОКНА]
            == [("1Ч", 3600, 60), ("24Ч", 86400, 600), ("3Д", 259200, 1800)],
            ОКНА)
        # МОНОТОННОСТЬ: кривая не вылезает выше и ниже своих точек
        злые = [{"utc": основа + i * 60, "lamports": з} for i, з in enumerate(
            [100, 100, 900, 100, 500, 500, 101, 900, 100, 100])]
        п_злые = put_krivoj(злые)
        xs, ys = п_злые["x"], п_злые["y"]
        выбросов = 0
        for i in range(len(ys) - 1):
            низ, верх = min(ys[i], ys[i + 1]), max(ys[i], ys[i + 1])
            for ш in range(1, 20):
                доля = (i + ш / 20.0) / (len(ys) - 1)
                _, y = tochka_krivoj(xs, ys, злые, доля)
                if y < низ - 0.01 or y > верх + 0.01:
                    выбросов += 1
        chk("кривая МОНОТОННА: ни одного выброса выше или ниже точек на злом "
            "ряде (190 выборок)", выбросов == 0, выбросов)
        chk("путь линии и заливки собраны, заливка замкнута к основанию",
            п_злые["liniya"].startswith("M ") and " C " in п_злые["liniya"]
            and п_злые["zalivka"].rstrip().endswith("Z"), п_злые["liniya"][:60])
        п_одна = put_krivoj([{"utc": основа, "lamports": 5}])
        chk("одна точка не роняет кривую", п_одна["liniya"].startswith("M "),
            п_одна)

        # ---------------------------------- 7. ВЫГРУЗКА: ПЕРЕВОДЫ ВНЕ ИЗМЕНЕНИЯ
        сост_в = pustoe_sostojanie(КОШ)
        сост_в["metki"] = [{"utc": основа + 100, "vid": "ввод",
                             "lamports": 2 * ЛАМПОРТОВ_В_SOL, "sig": "v"}]
        сост_в["sdelki"] = [{"mint": МИНТ, "itog": 50_000_000,
                              "zakryta_utc": основа + 200, "vhod": 1,
                              "sig_prodazhi": "s"}]
        ряды_в = [
            {"utc": основа, "kapital": 10 * ЛАМПОРТОВ_В_SOL, "vvod_vyvod": 0,
             "sol": 10 * ЛАМПОРТОВ_В_SOL, "wsol": 0, "renta": 0, "pozicii": 0},
            {"utc": основа + 300,
             "kapital": 12 * ЛАМПОРТОВ_В_SOL + 50_000_000,
             "vvod_vyvod": 2 * ЛАМПОРТОВ_В_SOL,
             "sol": 12 * ЛАМПОРТОВ_В_SOL + 50_000_000, "wsol": 0, "renta": 0,
             "pozicii": 0}]
        в = vygruzka(ряды_в, сост_в, sejchas=основа + 300)
        о1ч = в["okna"]["1Ч"]
        chk("ИЗМЕНЕНИЕ ЗА ПЕРИОД НЕ СОДЕРЖИТ ВВОДА: капитал вырос на 2.05 SOL, "
            "а изменение -- ровно 0.05",
            о1ч["izmenenie_lamports"] == 50_000_000
            and о1ч["vvod_vyvod_lamports"] == 2 * ЛАМПОРТОВ_В_SOL,
            (о1ч["izmenenie_lamports"], о1ч["vvod_vyvod_lamports"]))
        chk("процент считается от капитала НАЧАЛА окна, а не от конца",
            abs(о1ч["izmenenie_pct"] - 0.5) < 1e-9, о1ч["izmenenie_pct"])
        chk("сделки и закрытый итог -- за период, и отметки тоже",
            о1ч["sdelok"] == 1 and о1ч["zakrytyj_itog_lamports"] == 50_000_000
            and len(о1ч["metki"]) == 1, о1ч)
        chk("в выгрузке есть все три окна с готовыми путями",
            set(в["okna"]) == {"1Ч", "24Ч", "3Д"}
            and all(в["okna"][и].get("put_linii") is not None for и in в["okna"]),
            list(в["okna"]))

        # ---------------------------------- 8. СТРАНИЦА
        html = stranica()
        # xmlns SVG -- это ИМЯ ПРОСТРАНСТВА, а не адрес, по которому браузер
        # куда-то ходит; его и только его из проверки вычитаем.
        html_без = html.replace("http://www.w3.org/2000/svg", "")
        chk("страница без единого внешнего адреса: ни шрифта, ни библиотеки",
            "http://" not in html_без and "https://" not in html_без,
            [с for с in html_без.split()
             if с.startswith(("http://", "https://"))][:3])
        chk("в странице нет ни секрета, ни адреса узла, ни ключей",
            "BLOOM_TREKKER_SEKRET" not in html
            and "BLOOM_TREKKER_RPC" not in html
            and "api.mainnet" not in html, None)
        chk("обе темы объявлены: и настройкой системы, и переключателем",
            "prefers-color-scheme: dark" in html
            and '[data-theme="dark"]' in html
            and ':not([data-theme="light"])' in html, None)
        chk("герой РОВНО ОДИН, и у него не табличные цифры",
            html.count('class="geroj"') == 1
            and "font-variant-numeric: tabular-nums" in html, None)
        chk("на странице есть переключатель окон, заливка градиентом, метка "
            "последней точки и крест подсказки",
            'id="okna"' in html and "linearGradient" in html
            and 'id="podpis"' in html and 'id="krest"' in html, None)
        chk("время считается по Мадриду, и есть честный откат на UTC с Z",
            'timeZone: "Europe/Madrid"' in html and '+ "Z"' in html, None)
        chk("обновление раз в 30 с, и прежняя картинка не мигает пустотой",
            "setInterval(обновить, 30000)" in html
            and "ustarelo" in html, None)

        # ---------------------------------- 9. ОТДАЧА С СЕКРЕТОМ В ПУТИ
        кат_с = врем / "otdacha"
        кат_с.mkdir(parents=True, exist_ok=True)
        (кат_с / ФАЙЛ_ВЫГРУЗКИ).write_text(
            json.dumps({"ok": True, "kapital_lamports": 1}), encoding="utf-8")
        СЕКРЕТ = "sekret-proverki-0123456789"
        сервер = servis(кат_с, sekret=СЕКРЕТ, port=0, ne_sluzhit=True)
        порт = сервер.server_address[1]
        поток = threading.Thread(target=lambda: [сервер.handle_request()
                                                 for _ in range(3)], daemon=True)
        поток.start()
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        def _взять(путь):
            try:
                with urllib.request.urlopen(  # noqa: S310
                        f"http://127.0.0.1:{порт}{путь}", timeout=5) as отв:
                    return отв.status, отв.read()
            except urllib.error.HTTPError as сбой:
                return сбой.code, b""
        код_стр, тело_стр = _взять(f"/{СЕКРЕТ}/")
        код_json, тело_json = _взять(f"/{СЕКРЕТ}/{ФАЙЛ_ВЫГРУЗКИ}")
        код_чужой, _ = _взять("/chuzhoj-sekret-0123456789/")
        сервер.server_close()
        chk("по верному секрету отдаётся страница, а по нему же -- выгрузка",
            код_стр == 200 and b"<!doctype html>" in тело_стр[:40].lower()
            and код_json == 200 and b"kapital_lamports" in тело_json,
            (код_стр, код_json))
        chk("по чужому секрету -- 404 и ни слова подсказки",
            код_чужой == 404, код_чужой)
        chk("служба без секрета НЕ поднимается и говорит словами",
            _ne_vstala(кат_с, ""), None)
        ист_журнала = istochnik_zhurnala()
        chk("путь в журнал службы НЕ пишется: в log_message нет self.path вовсе",
            ист_журнала and "self.path" not in ист_журнала
            and "self.command" in ист_журнала, ист_журнала)

        # ---------------------------------- 10. РЯД НА ДИСКЕ И ОБРЕЗКА
        п_ряда = врем / "rjady" / ФАЙЛ_РЯДОВ
        старый = {"utc": int(основа - 10 * 86400), "kapital": 1}
        зап1 = dopisat_rjad(п_ряда, старый, sejchas=основа)
        зап2 = dopisat_rjad(п_ряда, {"utc": int(основа), "kapital": 2},
                            sejchas=основа)
        chk("ряд дописывается строкой, а старое ОБРЕЗАЕТСЯ той же записью: "
            "диск на лаборатории уже кончался раз",
            зап1["otrezano"] == 1 and зап1["rjadov"] == 0
            and зап2["rjadov"] == 1 and зап2["otrezano"] == 0
            and len(prochitat_rjady(п_ряда)) == 1, (зап1, зап2))

        # ---------------------------------- 11. ПРОГОН ЦЕЛИКОМ, БЕЗ СЕТИ
        кат_п = врем / "progon"
        р_прогон = progon(кат_п, rpc_call=узел, koshelek=КОШ,
                          sejchas=1_700_000_200)
        chk("прогон целиком: ряд, состояние, выгрузка и страница на диске",
            р_прогон["ok"]
            and (кат_п / ФАЙЛ_РЯДОВ).exists()
            and (кат_п / ФАЙЛ_СОСТОЯНИЯ).exists()
            and (кат_п / ФАЙЛ_ВЫГРУЗКИ).exists()
            and (кат_п / ФАЙЛ_СТРАНИЦЫ).exists(), р_прогон.get("why_not"))
        chk("в выгрузке и состоянии НЕТ ни секрета, ни адреса узла",
            all(("sekret" not in (кат_п / ф).read_text(encoding="utf-8").lower()
                 and "mainnet" not in (кат_п / ф).read_text(encoding="utf-8"))
                for ф in (ФАЙЛ_ВЫГРУЗКИ, ФАЙЛ_СОСТОЯНИЯ)), None)
        # СЛУЖБА: СБОРЩИК И ОТДАЧА ОДНИМ ПРОЦЕССОМ
        кат_сл = врем / "sluzhba"
        было_сек = os.environ.get("BLOOM_TREKKER_SEKRET")
        os.environ["BLOOM_TREKKER_SEKRET"] = "sekret-sluzhby-0123456789"
        try:
            узел_сл = _poddelnyj_uzel(sol=SOL0, scheta=[], podpisi=[],
                                       tranzakcii={})
            прежний_узел = globals()["rpc_iz_url"]
            globals()["rpc_iz_url"] = lambda url, **кв: узел_сл  # noqa: ARG005
            из_сл = sluzhba(кат_сл, koshelek=КОШ, pauza=1.0, port=0,
                            tikov=2)
        finally:
            globals()["rpc_iz_url"] = прежний_узел
            if было_сек is None:
                os.environ.pop("BLOOM_TREKKER_SEKRET", None)
            else:
                os.environ["BLOOM_TREKKER_SEKRET"] = было_сек
        chk("служба делает тики и отдаёт страницу ОДНИМ процессом: два юнита на "
            "хосте -- два места, где можно забыть секрет",
            из_сл["tikov"] == 2 and из_сл["sboev"] == 0
            and (кат_сл / ФАЙЛ_ВЫГРУЗКИ).exists(), из_сл)

        chk("затирание секрета в адресе узла оставляет только хост",
            zateret("https://mainnet.helius-rpc.com/?api-key=СЕКРЕТ")
            == "https://mainnet.helius-rpc.com/…", zateret(
                "https://mainnet.helius-rpc.com/?api-key=СЕКРЕТ"))

        # ---------------------------------- 12. СВЕРКА С УЧЁТОМ ПОЛОСЫ
        п_учёта = КОРЕНЬ / "data" / ФАЙЛ_УЧЁТА
        chk(f"учёт полосы на месте: data/{ФАЙЛ_УЧЁТА}", п_учёта.exists(),
            str(п_учёта))
        if п_учёта.exists():
            д_у = json.loads(п_учёта.read_text(encoding="utf-8"))
            ряды_у = д_у.get("ряды") or []
            по_суткам: dict = {}
            части_сошлись = части_всего = 0
            for р in ряды_у:
                сутки = str(р.get("utc") or "")[:10]
                if сутки not in ЖДЁМ_УЧЁТА:
                    continue
                к = по_суткам.setdefault(сутки, {"рядов": 0, "числом": 0,
                                                  "сумма": 0.0})
                к["рядов"] += 1
                зн = р.get("итог_po_cepi_sol")
                if isinstance(зн, (int, float)):
                    к["числом"] += 1
                    к["сумма"] += float(зн)
                части = р.get("итог_po_cepi_chasti") or {}
                п_ = (части.get("покупка") or {}).get("все_sol")
                пр_ = (части.get("продажа") or {}).get("все_sol")
                if isinstance(зн, (int, float)) and isinstance(п_, (int, float)) \
                        and isinstance(пр_, (int, float)):
                    части_всего += 1
                    части_сошлись += (abs((п_ + пр_) - зн) < 5e-9)
            плохие = []
            for сутки, ждём in ЖДЁМ_УЧЁТА.items():
                факт = по_суткам.get(сутки) or {}
                if (факт.get("рядов") != ждём["рядов"]
                        or факт.get("числом") != ждём["числом"]
                        or round(факт.get("сумма", 0.0), 6) != ждём["сумма"]):
                    плохие.append((сутки, факт, ждём))
            chk("суточные итоги учёта полосы за 01--03.10 те, что объявлены "
                "(поле «итог_po_cepi_sol» КИРИЛЛИЦЕЙ)", not плохие, плохие)
            chk("итог сделки у учёта -- это СУММА ДВУХ транзакций (покупка плюс "
                "продажа) по всем нашим счетам: ровно моя формула, "
                f"{части_сошлись} из {части_всего}",
                части_всего > 400 and части_сошлись == части_всего,
                (части_сошлись, части_всего))
            # СУТКИ ПО МАДРИДУ -- СВЕРКА С ЧИСЛОМ, КОТОРОЕ ОПУБЛИКОВАЛА САМА
            # ПОЛОСА. Хостовая выгрузка за мадридские сутки 02.10 лежит
            # отдельным файлом; мой свод по ТОМУ ЖЕ окну обязан дать ровно её
            # числа -- иначе трекер и учёт считают разные сутки.
            import calendar as _кал  # noqa: PLC0415
            п_м = КОРЕНЬ / "data" / ФАЙЛ_СУТОК_МАДРИД
            if п_м.exists():
                д_м = json.loads(п_м.read_text(encoding="utf-8"))
                сумма_м = sum(float(р["итог_po_cepi_sol"]) for р in д_м["ряды"]
                              if isinstance(р.get("итог_po_cepi_sol"),
                                            (int, float)))
                числом_м = sum(1 for р in д_м["ряды"]
                               if isinstance(р.get("итог_po_cepi_sol"),
                                             (int, float)))
                с_ts = _кал.timegm(time.strptime(ЖДЁМ_СУТОК_МАДРИД["s"],
                                                 "%Y-%m-%dT%H:%M:%S"))
                до_ts = _кал.timegm(time.strptime(ЖДЁМ_СУТОК_МАДРИД["do"],
                                                  "%Y-%m-%dT%H:%M:%S"))
                в_окне = [р for р in ряды_у
                          if с_ts <= _кал.timegm(time.strptime(
                              str(р.get("utc"))[:19], "%Y-%m-%dT%H:%M:%S")) < до_ts]
                сумма_св = sum(float(р["итог_po_cepi_sol"]) for р in в_окне
                               if isinstance(р.get("итог_po_cepi_sol"),
                                             (int, float)))
                chk("мадридские сутки 02.10: мой свод по окну 22:00Z..22:00Z "
                    f"даёт то же, что ВЫГРУЗКА ХОСТА ({ЖДЁМ_СУТОК_МАДРИД['sdelok']} "
                    f"сделок, {ЖДЁМ_СУТОК_МАДРИД['chislom']} числом, "
                    f"{ЖДЁМ_СУТОК_МАДРИД['summa']} SOL)",
                    len(д_м["ряды"]) == ЖДЁМ_СУТОК_МАДРИД["sdelok"]
                    and числом_м == ЖДЁМ_СУТОК_МАДРИД["chislom"]
                    and abs(сумма_м - ЖДЁМ_СУТОК_МАДРИД["summa"]) < 1e-9
                    and len(в_окне) == len(д_м["ряды"])
                    and abs(сумма_св - сумма_м) < 1e-9,
                    (len(д_м["ряды"]), числом_м, round(сумма_м, 9),
                     len(в_окне), round(сумма_св, 9)))
                chk("и сама выгрузка называет каноническим ТО ЖЕ поле: «итог_po_"
                    "cepi_sol (сумма изменений ВСЕХ наших счетов по двум "
                    "подписям)»",
                    "итог_po_cepi_sol" in str(д_м.get("итог_канонический") or "")
                    and "ВСЕХ наших счетов" in str(д_м.get("итог_канонический")
                                                   or ""),
                    д_м.get("итог_канонический"))
            else:
                chk(f"хостовая выгрузка за мадридские сутки на месте: "
                    f"data/{ФАЙЛ_СУТОК_МАДРИД}", False, str(п_м))
            # КОШЕЛЁК РЯДА ЛЕЖИТ В zapis.wallet, А ПОЛЕ wallet ПУСТОЕ У ВСЕХ.
            кош: dict = {}
            for р in ряды_у:
                з = (р.get("zapis") or {}).get("wallet")
                кош[з] = кош.get(з, 0) + 1
            chk("кошелёк ряда читается из zapis.wallet: кошелёк полосы "
                f"{ЖДЁМ_KOSHELKOV['4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x']} "
                "рядов, второй -- 13 и только 27.09",
                кош == ЖДЁМ_KOSHELKOV, кош)
            # ЛОВУШКА НАЗВАНА ЧИСЛОМ: латинское поле -- это НЕ итог.
            лат = sum(float(р["itog_po_cepi_sol"]) for р in ряды_у
                      if isinstance(р.get("itog_po_cepi_sol"), (int, float)))
            кир = sum(float(р["итог_po_cepi_sol"]) for р in ряды_у
                      if isinstance(р.get("итог_po_cepi_sol"), (int, float)))
            chk("латинское «itog_po_cepi_sol» -- НЕ итог, а выручка продажи: "
                f"его сумма {лат:+.2f} против {кир:+.2f} у кириллического",
                лат > кир * 10, (round(лат, 2), round(кир, 2)))
    finally:
        shutil.rmtree(врем, ignore_errors=True)

    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным -- "
              "молчаливый пропуск считается провалом")
        return 1
    return 1 if плохо else 0


def _ne_vstala(kat, sekret) -> bool:
    """Поднимается ли служба без секрета. Должна НЕ подниматься -- словами."""
    try:
        servis(kat, sekret=sekret, port=0, ne_sluzhit=True)
    except ОшибкаТрекера as сбой:
        return "секрет" in str(сбой)
    return False


def istochnik_zhurnala() -> str:
    """Исходник log_message службы -- чтобы проверить, что пути там нет."""
    import inspect  # noqa: PLC0415

    ист = ""
    собираем = False

    for строка in inspect.getsource(servis).splitlines():
        if "def log_message" in строка:
            собираем = True
            ист += строка + "\n"
            continue
        if собираем:
            if строка.strip().startswith("def ") and "log_message" not in строка:
                break
            ист += строка + "\n"
    return ист


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--kat", default=None,
                   help=f"каталог ряда и выгрузки (по умолчанию {КАТАЛОГ})")
    p.add_argument("--koshelek", default=КОШЕЛЕК)
    p.add_argument("--progon", action="store_true",
                   help="один тик сборщика: догнать цепь, записать ряд")
    p.add_argument("--cikl", type=int, default=0,
                   help="тиков подряд с паузой --pauza (0 -- один тик)")
    p.add_argument("--pauza", type=float, default=20.0,
                   help="пауза между тиками, секунд (владелец просил 15--30)")
    p.add_argument("--sutok", type=float, default=3.0,
                   help="восстановление истории за столько суток при первом тике")
    p.add_argument("--servis", action="store_true",
                   help="поднять ТОЛЬКО отдачу (секрет -- BLOOM_TREKKER_SEKRET)")
    p.add_argument("--sluzhba", action="store_true",
                   help="сборщик И отдача в одном процессе -- то, что ставится "
                        "на хост одним юнитом")
    p.add_argument("--tikov", type=int, default=0,
                   help="сколько тиков службы сделать и выйти (0 -- навсегда)")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--adres", default="127.0.0.1")
    p.add_argument("--stranica", default=None,
                   help="записать страницу в этот файл и выйти")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if a.stranica:
        Path(a.stranica).write_text(stranica(), encoding="utf-8")
        print(f"страница записана: {a.stranica}")
        return 0
    if a.servis:
        # СЕКРЕТА В КОМАНДНОЙ СТРОКЕ НЕТ: он приходит только окружением.
        servis(a.kat, port=a.port, adres=a.adres)
        return 0
    if a.sluzhba:
        из_ = sluzhba(a.kat, koshelek=a.koshelek, pauza=a.pauza, sutok=a.sutok,
                      port=a.port, adres=a.adres, tikov=a.tikov)
        print(json.dumps(из_, ensure_ascii=False))
        return 0 if из_["tikov"] and из_["sboev"] < из_["tikov"] else 2
    if a.progon or a.cikl:
        тиков = max(1, int(a.cikl))
        последний = {}
        for и in range(тиков):
            последний = progon(a.kat, koshelek=a.koshelek, sutok=a.sutok)
            кратко = {k: v for k, v in последний.items()
                      if k in ("ok", "why_not", "kapital_sol", "uzel",
                               "vyzovov_za_sutki")}
            кратко["rjad"] = {k: последний.get("rjad", {}).get(k)
                              for k in ("utc", "kapital", "pozicij", "schetov",
                                        "bez_vhoda")} if последний.get("rjad") else None
            print(json.dumps(кратко, ensure_ascii=False))
            if и + 1 < тиков:
                time.sleep(max(1.0, float(a.pauza)))
        return 0 if последний.get("ok") else 2
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
