#!/usr/bin/env python3
"""Регистратор Polymarket: снимки публичных данных, по строке на снимок.

БЕЗ ДЕНЕГ И БЕЗ КЛЮЧЕЙ. Ни одного ключа, ни кошелькового, ни API, скрипт не
просит и не читает -- проверено своей же проверкой (`--self-test`), которая
ищет в рабочей части этого файла любое обращение к `environ`/`getenv`, то есть
единственный путь, каким ключ мог бы сюда попасть. Всё, что здесь
опрашивается, Polymarket отдаёт без подписи; что именно требует подписи, а что
нет, взято не из гайдов, а из их собственного клиента `py-clob-client` 0.34.6
(PyPI): методы с `assert_level_1_auth` (приватный ключ) и
`assert_level_2_auth` (ключ API) -- это торговля, свои ордера, свои сделки,
баланс и уведомления; чтение рынков, книг, цен, спредов, последних сделок и
ставки комиссии не требует ничего. Ссылки и разбор -- docs/polymarket_put.md.

ЧТО ПИШЕТ. Одна строка JSON на снимок, дописыванием, в
data/polymarket/<список>/<YYYY-MM-DD>/<HH>.jsonl.gz. В строке: метка UTC,
список, метод, URL, тело запроса (для батчей), код ответа, заголовки лимитов,
время ответа, длина сырого тела, СЫРОЕ тело как пришло и рядом короткий
разбор. Сырое не выбрасывается никогда: разбор может отстать от смены
формата, снимок обязан остаться годным. Отказ пишется ТАКОЙ ЖЕ строкой -- с
кодом и причиной; пустой ответ -- тоже снимок.

РОТАЦИЯ ПО ЧАСАМ, А НЕ ПО СУТКАМ, И ЭТО НЕ ВКУС. При 100 рынках книги за
сутки дают около 158 МБ в gzip (расчёт и замер степени сжатия -- в
docs/polymarket_put.md), а предел GitHub на файл -- 100 МБ. Суточный файл в
ветку не лёг бы. Часовой -- около 6.6 МБ.

ЧАСТОТА КНИГ -- МИНУТА, И ЭТО ВЗЯТО ИЗ МЕХАНИКИ ВОЗНАГРАЖДЕНИЙ. Polymarket
считает награду поставщику ликвидности, выбирая книгу примерно раз в минуту
(до 1440 проб за сутки, со случайным сдвигом внутри минуты). Чаще минуты
опрашивать нечего: награда всё равно считается по минутной пробе.

Хост я не трогаю: ставит Code-1 по docs/polymarket_ustanovka.md.
"""
from __future__ import annotations

import argparse
import calendar
import gzip
import json
import math
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПАПКА = КОРЕНЬ / "data" / "polymarket"
ВЫБОР = ПАПКА / "vybor.json"
ТАЙМАУТ = 25
АГЕНТ = "hedge-polymarket/1.0 (+research, read-only, no trading)"

# Базовые адреса -- из собственной документации Polymarket
# (Polymarket/agent-skills, SKILL.md и market-data.md): Gamma и Data -- без
# подписи вовсе, CLOB -- без подписи на чтение.
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

# ПРЕДЕЛ БАТЧА -- 500 НА ЗАПРОС, их число, не моё: "All orderbook queries have
# batch variants (up to 500 tokens)". Из него СЧИТАЕТСЯ потолок рынков
# (predel_rynkov), и выше потолка прогон отказывается по имени ДО первого
# запроса. Дробления на части здесь нет нарочно: в заказанном диапазоне
# 50-100 рынков оно не нужно, а код, который не вызывается, но благословлён
# проверкой, хуже его отсутствия.
БАТЧ_ПРЕДЕЛ = 500

# ПРЕДЕЛ СТРАНИЦЫ `/markets` -- 100, И ЭТО ЗАМЕР, А НЕ ДОГАДКА. Наш же
# сохранённый настоящий ответ 200 на запрос с limit=500 содержит РОВНО 100
# записей (data/p3_guard_cache/taskC_polymarket_btc_probe_result.json,
# bulk_active_markets_scan.n_fetched = 100). Объявленного предела для
# /markets нет ни в их py-SDK, ни в ts-SDK -- это ЗАЖИМ НА СТОРОНЕ СЕРВЕРА, и
# просить больше нельзя не из вежливости: их же SDK предупреждает, что размер
# страницы выше серверного потолка заставляет сервер молча зажать запрос, и
# тогда пагинация ТЕРЯЕТ СТРОКИ ("pagination silently skips or drops rows").
# Поэтому просим ровно 100 и шагаем смещением на 100 -- тем способом, которым
# наш taskC_sports_matcher реально просмотрел 5 972 рынка (результат там же).
СТРАНИЦА_РЫНКОВ = 100
# Сколько страниц смотреть, пока не наберётся заказанное число рынков С
# НЕПУСТОЙ КНИГОЙ. Потолок нужен, чтобы отбор не ходил по всей площадке, но
# если он сработал, прогон говорит об этом ВСЛУХ: молчаливая обрезка выглядела
# бы как «набрали сколько есть».
ПОТОЛОК_СТРАНИЦ_ОТБОРА = 10

# Заголовки, по которым на ящике станет видно НАСТОЯЩИЕ лимиты. Из контейнера
# их не узнать: хосты закрыты политикой сети. Официальная страница лимитов
# (docs.polymarket.com/api-reference/rate-limits) на момент письма отдаёт
# /book, /price, /midpoint по 1500 за 10 с, /books и /prices по 500,
# gamma /markets 300, /events 500 -- но прочитать её своими руками я не смог,
# поэтому числа лежат в документе как ссылка, а мерить будет вот это.
ЗАГОЛОВКИ_ЛИМИТОВ = ("content-type", "x-ratelimit-limit", "x-ratelimit-remaining",
                     "x-ratelimit-reset", "ratelimit-remaining", "retry-after",
                     "cf-ray", "server", "date")

# (список, частота, метод, путь, что_подставить)
# «минута» -- то, что меняется от сделки к сделке; «пять» -- состав рынков,
# ставка комиссии и крупные держатели.
СПИСКИ = (
    ("gamma_events", "пять", "GET", "/events", "events"),
    ("clob_books", "минута", "POST", "/books", "tokens"),
    ("clob_prices", "минута", "POST", "/prices", "tokens_sides"),
    ("clob_midpoints", "минута", "POST", "/midpoints", "tokens"),
    ("clob_spreads", "минута", "POST", "/spreads", "tokens"),
    ("data_trades", "минута", "GET", "/trades", "trades"),
    ("clob_fee_rate", "пять", "GET", "/fee-rate", "token_odin"),
    ("data_holders", "пять", "GET", "/holders", "market_odin"),
    ("clob_time", "пять", "GET", "/time", None),
)

ХОСТ_СПИСКА = {"gamma": GAMMA, "clob": CLOB, "data": DATA}


def хост(список_: str) -> str:
    """Хост по приставке имени списка. Неизвестная приставка -- отказ по имени."""
    п = список_.split("_", 1)[0]
    if п not in ХОСТ_СПИСКА:
        raise ValueError(f"неизвестная приставка списка {список_!r}: "
                         f"ждал одну из {sorted(ХОСТ_СПИСКА)}")
    return ХОСТ_СПИСКА[п]


# ---------------------------------------------------- выбор рынков

def токены_рынка(рынок: dict) -> list:
    """Токены исходов одного рынка -- ВСЕМИ формами, какие Gamma отдаёт.

    Gamma кладёт их в `clobTokenIds`, и кладёт СТРОКОЙ с JSON внутри
    (`"[\\"123\\",\\"456\\"]"`), а CLOB в своих ответах -- списком словарей
    `tokens: [{token_id, outcome}]`. Разбираем обе формы и третью (готовый
    список строк). Чего не узнали -- не выдумываем: вернём пусто, и снимок
    запишется с причиной.
    """
    если = рынок.get("clobTokenIds")
    if isinstance(если, str):
        try:
            если = json.loads(если)
        except ValueError:
            если = None
    if isinstance(если, list) and если:
        return [str(т) for т in если if т]
    токены = рынок.get("tokens")
    if isinstance(токены, list) and токены:
        из_ = []
        for т in токены:
            if isinstance(т, dict) and т.get("token_id"):
                из_.append(str(т["token_id"]))
            elif isinstance(т, str) and т:
                из_.append(т)
        if из_:
            return из_
    return []


def выбор_из_events(тело) -> dict:
    """Из снимка `/events` -- токены, рынки и ярлыки для минутного круга."""
    события = тело if isinstance(тело, list) else (
        тело.get("data") if isinstance(тело, dict) else None)
    if not isinstance(события, list):
        return {"tokens": [], "markets": [], "slugs": [], "sobytij": 0}
    токены, рынки, ярлыки = [], [], []
    for с in события:
        if not isinstance(с, dict):
            continue
        for р in (с.get("markets") or []):
            if not isinstance(р, dict):
                continue
            # Книга есть только там, где она включена: иначе снимок пустой
            # по делу, а не по ошибке.
            if р.get("enableOrderBook") is False:
                continue
            т = токены_рынка(р)
            if not т:
                continue
            токены.extend(т)
            усл = р.get("conditionId") or р.get("condition_id")
            if усл:
                рынки.append(str(усл))
            if р.get("slug"):
                ярлыки.append(str(р["slug"]))
    # Порядок сохраняем (он задан сортировкой по объёму), дубли убираем.
    видели: set = set()
    токены = [т for т in токены if not (т in видели or видели.add(т))]
    видели = set()
    рынки = [р for р in рынки if not (р in видели or видели.add(р))]
    return {"tokens": токены, "markets": рынки, "slugs": ярлыки,
            "sobytij": len(события)}


# СУТОЧНЫЙ ОТБОР РЫНКОВ. Почему он вообще появился: раньше состав брался из
# `/events` -- сто самых объёмных СОБЫТИЙ разворачивались в рынки, и с них
# снимались первые 100 токенов. На ящике это дало 4086 рынков / 8172 токена, а
# первые 100 токенов оказались хвостом многоисходных событий, которые никогда
# не торговались: запрошено 200 токенов, книг пришло 10, и ВСЕ с `bids: []`
# `asks: []` при верных token_id и полном объекте рынка. «100 рынков по
# суточному объёму» и «рынки первых 100 событий по объёму» -- разные вещи, и
# документ имел в виду первое.


def kandidat_rynka(р: dict) -> dict:
    """Кандидат отбора из ОДНОЙ записи `/markets`, иначе пустой словарь.

    Условия ровно те, что названы в задании: книга включена
    (`enableOrderBook`), рынок `active`, не `closed`, и токены читаются.
    Пустая книга здесь НЕ проверяется -- её проверяет первый снимок
    `/books`: по полям Gamma этого надёжно не узнать, а именно пустая книга и
    была всей бедой.

    Поля ответа взяты не по памяти: у настоящего сохранённого ответа 200
    (`data/p3_guard_cache/taskC_prediction_markets_probe_result.json`,
    запрос `/markets?limit=5&active=true&closed=false`) у записи рынка
    `enableOrderBook`, `active`, `closed` -- булевы, `volume24hr` -- число,
    `conditionId` и `slug` -- строки, `clobTokenIds` -- строка с JSON внутри.
    """
    if not isinstance(р, dict):
        return {}
    if not р.get("enableOrderBook"):
        return {}
    if not р.get("active") or р.get("closed"):
        return {}
    т = токены_рынка(р)
    усл = р.get("conditionId") or р.get("condition_id")
    if not т or not усл:
        return {}
    объём = _чис(р.get("volume24hr"))
    return {"conditionId": str(усл), "slug": str(р.get("slug") or ""),
            "volume24hr": объём if объём is not None else 0.0,
            "tokens": т,
            # Эти три поля даром лежат в том же ответе и помогают разбору на
            # ящике; НА ОТБОР они не влияют -- решает снимок книги.
            "bestBid": _чис(р.get("bestBid")),
            "bestAsk": _чис(р.get("bestAsk")),
            "acceptingOrders": р.get("acceptingOrders")}


def kandidaty_iz_stranicy(тело) -> list:
    """Кандидаты из одной страницы `/markets`."""
    рынки = тело if isinstance(тело, list) else (
        тело.get("data") if isinstance(тело, dict) else None)
    if not isinstance(рынки, list):
        return []
    из_ = []
    for р in рынки:
        к = kandidat_rynka(р)
        if к:
            из_.append(к)
    return из_


def po_objomu(кандидаты: list) -> list:
    """Кандидаты по убыванию СУТОЧНОГО объёма; дубли по рынку сняты.

    Сортируем У СЕБЯ, числом из тела ответа. Серверной сортировке при этом
    есть основания верить: по их официальному py-SDK ТЕКСТОВЫМИ колонками
    сервис держит только `volume` и `liquidity` (поэтому `order="volume"`
    сортирует лексикографически, и SDK подменяет эти два имени на числовых
    двойников `volumeNum`/`liquidityNum`), а `volume24hr` -- числовой, и
    двойника `volume24hrNum` не существует ни в их моделях, ни в ответе.
    Но своя сортировка нужна всё равно, и не для недоверия: кандидаты
    СКЛЕИВАЮТСЯ ИЗ НЕСКОЛЬКИХ СТРАНИЦ, и на шве страниц порядок держится
    только если его задать заново.
    """
    видели: set = set()
    из_ = []
    for к in sorted(кандидаты,
                    key=lambda к: -(к.get("volume24hr") or 0.0)):
        усл = к.get("conditionId")
        if not усл or усл in видели:
            continue
        видели.add(усл)
        из_.append(к)
    return из_


def knigi_po_tokenam(тело) -> dict:
    """`{token_id: есть И бид, И аск}` из ответа `/books`.

    Сопоставляем ПО `asset_id` ИЗ ТЕЛА, а не по порядку запроса: на ящике на
    200 запрошенных токенов книг пришло 10, то есть сервер записи
    пропускает. При сопоставлении по порядку книга одного токена досталась бы
    другому -- и отбор принял бы пустые рынки за полные.
    """
    книги = тело if isinstance(тело, list) else (
        тело.get("data") if isinstance(тело, dict) else None)
    если: dict = {}
    if not isinstance(книги, list):
        return если
    for к in книги:
        if not isinstance(к, dict):
            continue
        т = к.get("asset_id") or к.get("token_id")
        if not т:
            continue
        # Пустая книга приходит пустыми массивами (замер с ящика: `bids: []`
        # `asks: []` при полном объекте). Отсутствие ключа считаем тем же
        # самым: ни бида, ни аска мы не видели.
        если[str(т)] = bool(к.get("bids")) and bool(к.get("asks"))
    return если


def vybrat_po_knigam(кандидаты: list, книги: dict, rynkov: int) -> dict:
    """Из кандидатов -- те, у кого книга НЕ ПУСТА, по убыванию объёма.

    Рынок проходит, если ХОТЯ БЫ У ОДНОГО его токена в первом снимке есть и
    бид, и аск. Сколько токенов рынка оказались с книгой, запоминается
    (`tokenov_s_knigoj`): проверке (а) нужны обе ноги, и из суточного списка
    это будет видно без нового запроса.

    Токенов берём не больше `rynkov * 2` -- столько минутный круг и
    запрашивает. Рынок, который в этот остаток не влезает ЦЕЛИКОМ, не
    берётся вовсе: иначе срез минутного круга разрезал бы его пополам, и у
    одной ноги книга собиралась бы, а у второй нет -- проверка (а) по такой
    паре не считается.
    """
    выбрано, пустые, неопрошенные = [], [], []
    токенов = 0
    for к in кандидаты:
        свои = [т for т in к["tokens"] if т in книги]
        if not свои:
            неопрошенные.append(к["conditionId"])
            continue
        с_книгой = [т for т in свои if книги[т]]
        if not с_книгой:
            пустые.append(к["conditionId"])
            continue
        if len(выбрано) >= rynkov or токенов + len(к["tokens"]) > rynkov * 2:
            break
        токенов += len(к["tokens"])
        выбрано.append(dict(к, tokenov_s_knigoj=len(с_книгой)))
    return {"rynki": выбрано, "pustyh": пустые, "neoprosheno": неопрошенные,
            "tokenov": токенов, "hvatilo": len(выбрано) >= rynkov}


def прочесть_выбор() -> dict:
    if not ВЫБОР.exists():
        return {"tokens": [], "markets": [], "slugs": [], "sobytij": 0}
    try:
        return json.loads(ВЫБОР.read_text(encoding="utf-8"))
    except ValueError:
        return {"tokens": [], "markets": [], "slugs": [], "sobytij": 0}


# ---------------------------------------------------- запросы

def predel_rynkov() -> int:
    """Сколько рынков влезает в батч БЕЗ дробления на части.

    Считается, а не назначается. На рынок два исхода, значит два токена.
    Самый тесный запрос -- `/prices`: он требует СТОРОНУ, то есть на токен
    ДВЕ записи (BUY и SELL). Итого 4 записи на рынок при пределе 500 ->
    125 рынков. У `/books` записей вдвое меньше, он упёрся бы на 250.
    Заказ владельца -- 50-100 рынков, то есть внутри потолка; выше -- отказ
    по имени, а не молчаливая обрезка тела запроса.
    """
    return БАТЧ_ПРЕДЕЛ // 4


def тело_запроса(что: str, выбор: dict, rynkov: int) -> tuple:
    """Что подставить в запрос: (url_хвост, тело_POST, подпись_для_строки).

    Возвращает None вместо тела для GET. Пустой выбор -- не ошибка здесь:
    снимок запишется с причиной «нет выбора рынков», и это видно в архиве.
    """
    токены = (выбор.get("tokens") or [])[: rynkov * 2]
    рынки = (выбор.get("markets") or [])[:rynkov]
    if что == "events":
        # Событие несёт свои рынки -- одним запросом вместо N (их же совет:
        # "Events > Markets: events contain their markets").
        # ИМЯ ПОЛЯ СОРТИРОВКИ -- volume24hr, И ЭТО ЗАМЕРЕНО, А НЕ УГАДАНО.
        # Было `volume_24hr`: Gamma отвечает 422 «order fields are not
        # valid», выбор остаётся пустым, и ВЕСЬ минутный круг молчит при
        # зелёной самопроверке. Проба Code-1 с lab-ams 16.10:
        # volume_24hr -> 422; volume24hr -> 200 (1 370 904 Б); volume -> 200;
        # liquidity -> 200; volume24Hr -> 422; без order -> 200. В теле
        # ответа поле зовётся так же -- `volume24hr` (настоящий ответ 200
        # лежит у нас в data/p3_guard_cache/taskC_prediction_markets_probe_
        # result.json: у объекта события volume24hr = 50997.064, а
        # volume_24hr нет вовсе).
        return (f"?active=true&closed=false&order=volume24hr"
                f"&ascending=false&limit={rynkov}"), None, {"limit": rynkov}
    if что == "markets_stranica":
        # Страница `/markets` для СУТОЧНОГО ОТБОРА (не для минутного круга).
        # Предел страницы -- 100 (замер, см. СТРАНИЦА_РЫНКОВ), поэтому идём
        # через offset. Фильтры active/closed -- серверные, остальное
        # отбирается у себя: по полям Gamma пустую книгу не узнать.
        предел = int(выбор.get("limit") or СТРАНИЦА_РЫНКОВ)
        смещение = int(выбор.get("offset") or 0)
        return (f"?active=true&closed=false&order=volume24hr"
                f"&ascending=false&limit={предел}&offset={смещение}"), None, {
            "limit": предел, "offset": смещение}
    if что == "tokens":
        return "", [{"token_id": т} for т in токены], {"tokenov": len(токены)}
    if что == "tokens_sides":
        тело = []
        for т in токены:
            тело.append({"token_id": т, "side": "BUY"})
            тело.append({"token_id": т, "side": "SELL"})
        return "", тело, {"tokenov": len(токены), "zapisej": len(тело)}
    if что == "trades":
        return "?limit=500", None, {"limit": 500}
    if что == "token_odin":
        if not токены:
            return None, None, {"prichina": "нет выбора рынков"}
        return f"?token_id={токены[0]}", None, {"token_id": токены[0]}
    if что == "market_odin":
        if not рынки:
            return None, None, {"prichina": "нет выбора рынков"}
        return f"?market={рынки[0]}&limit=100", None, {"market": рынки[0]}
    if что is None:
        return "", None, {}
    raise ValueError(f"неизвестная подстановка {что!r}")


def разбор_коротко(список_: str, тело) -> dict:
    """Короткий разбор рядом с сырым -- чтобы свод читался без распаковки."""
    если: dict = {}
    if тело is None:
        return если
    if список_ == "gamma_events":
        в = выбор_из_events(тело)
        если = {"sobytij": в["sobytij"], "tokenov": len(в["tokens"]),
                "rynkov": len(в["markets"])}
    elif список_ == "gamma_markets":
        рынки = тело if isinstance(тело, list) else (
            тело.get("data") if isinstance(тело, dict) else None)
        если = {"rynkov": len(рынки) if isinstance(рынки, list) else 0,
                "kandidatov": len(kandidaty_iz_stranicy(тело))}
    elif список_.startswith("clob_books"):
        # `startswith`, а не `==`: тем же разбором читается и список
        # `clob_books_otbor` (снимки книг суточного отбора). Отдельное имя
        # списка нарочно: в `clob_books` лежат минутные снимки ВЫБРАННОГО
        # состава, из них считаются производные ряды, и мешать туда пробные
        # книги чужих рынков нельзя.
        книги = тело if isinstance(тело, list) else []
        уровней = sum(len(к.get("bids") or []) + len(к.get("asks") or [])
                      for к in книги if isinstance(к, dict))
        с_обеими = sum(1 for к in книги if isinstance(к, dict)
                       and к.get("bids") and к.get("asks"))
        если = {"knig": len(книги), "urovnej": уровней,
                "knig_s_dvumja_storonami": с_обеими}
    elif список_ in ("clob_prices", "clob_midpoints", "clob_spreads"):
        если = {"otvetov": len(тело) if isinstance(тело, (list, dict)) else 0}
    elif список_ == "data_trades":
        сделки = тело if isinstance(тело, list) else (
            тело.get("data") if isinstance(тело, dict) else None)
        если = {"sdelok": len(сделки) if isinstance(сделки, list) else 0}
    elif список_ == "clob_fee_rate":
        если = {"base_fee": тело.get("base_fee")
                if isinstance(тело, dict) else None}
    elif список_ == "data_holders":
        д = тело if isinstance(тело, list) else (
            тело.get("holders") if isinstance(тело, dict) else None)
        если = {"derzhatelej": len(д) if isinstance(д, list) else 0}
    return если


def tokeny_dlja_sborov(выбор: dict, rynkov: int) -> list:
    """Токены, по которым спрашивается ставка сбора: РОВНО те, что ведёт
    минутный круг.

    Срез тот же, что в `тело_запроса` (`rynkov * 2`), и это не
    придирчивость: иначе суточный обход спрашивал бы ставку по токенам, чьих
    книг у нас нет, и в архиве появились бы сборы без книг -- проверка (а)
    по такой строке не считает ничего, а в счёте «спрошено N» она
    выглядела бы сделанной работой. Дубли снимаются: один токен -- один
    запрос.
    """
    видели: set = set()
    из_ = []
    for т in (выбор.get("tokens") or [])[: rynkov * 2]:
        т = str(т)
        if т and т not in видели:
            видели.add(т)
            из_.append(т)
    return из_


def разбор_s_tokenom(разбор: dict, подпись: dict) -> dict:
    """`token_id` из подписи запроса -- внутрь разбора.

    Нужно для проверки (а) (`docs/polymarket_put.md`, п. 10): ряды
    `clob_fee_rate` по РАЗНЫМ токенам должны различаться, не разбирая URL.
    Отказ (разбор пустой) получает `token_id` тоже: иначе из архива не
    видно, по какому токену ставка не вышла, и его молча сочли бы
    неспрошенным.
    """
    если = dict(разбор)
    т = подпись.get("token_id")
    if т:
        если["token_id"] = str(т)
    return если


def снимок(список_: str, метод: str, путь: str, что, выбор: dict,
           rynkov: int) -> dict:
    """Один снимок. Отказ -- такая же строка, с кодом и причиной."""
    стр = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "список": список_, "метод": метод}
    try:
        хвост, тело_post, подпись = тело_запроса(что, выбор, rynkov)
    except ValueError as сбой:
        стр.update({"url": путь, "код": None, "ошибка": str(сбой),
                    "байт": 0, "тело": None, "разбор": {}})
        return стр
    стр["запрос"] = подпись
    if хвост is None:
        стр.update({"url": хост(список_) + путь, "код": None,
                    "ошибка": подпись.get("prichina") or "нечего запрашивать",
                    "байт": 0, "тело": None,
                    "разбор": разбор_s_tokenom({}, подпись)})
        return стр
    url = хост(список_) + путь + хвост
    стр["url"] = url
    данные = None
    if метод == "POST":
        if not тело_post:
            стр.update({"код": None, "ошибка": "нет выбора рынков",
                        "байт": 0, "тело": None,
                        "разбор": разбор_s_tokenom({}, подпись)})
            return стр
        данные = json.dumps(тело_post).encode("utf-8")
    зап = urllib.request.Request(
        url, data=данные, method=метод,
        headers={"accept": "application/json", "user-agent": АГЕНТ,
                 **({"content-type": "application/json"} if данные else {})})
    нач = time.time()
    сырое = b""
    try:
        with urllib.request.urlopen(зап, timeout=ТАЙМАУТ) as о:
            сырое = о.read()
            стр["код"] = о.status
            стр["заголовки"] = {к.lower(): v for к, v in о.headers.items()
                                if к.lower() in ЗАГОЛОВКИ_ЛИМИТОВ}
    except urllib.error.HTTPError as e:
        сырое = e.read()[:20000]
        стр["код"] = e.code
        стр["ошибка"] = f"http {e.code}"
        стр["заголовки"] = {к.lower(): v for к, v in (e.headers or {}).items()
                            if к.lower() in ЗАГОЛОВКИ_ЛИМИТОВ}
    except Exception as e:  # noqa: BLE001
        стр["код"] = None
        стр["ошибка"] = f"{type(e).__name__}: {e}"[:300]
    стр["секунд"] = round(time.time() - нач, 3)
    стр["байт"] = len(сырое)
    тело = None
    if сырое:
        try:
            тело = json.loads(сырое.decode("utf-8", "replace"))
        except ValueError:
            тело = {"не_json": сырое[:4000].decode("utf-8", "replace")}
    стр["тело"] = тело
    стр["разбор"] = разбор_s_tokenom(разбор_коротко(список_, тело), подпись)
    return стр


# ---------------------------------------------------- запись и ротация

def путь_снимка(стр: dict) -> Path:
    """<список>/<YYYY-MM-DD>/<HH>.jsonl.gz -- ЧАС БЕРЁТСЯ ИЗ МЕТКИ СНИМКА.

    Не из локальных часов: иначе снимок, сделанный в 23:59:59Z и записанный
    в 00:00:00Z, лёг бы в чужой час, и сутки перестали бы сходиться.
    """
    метка = стр["utc"]
    return ПАПКА / стр["список"] / метка[:10] / f"{метка[11:13]}.jsonl.gz"


def записать(стр: dict) -> Path:
    ф = путь_снимка(стр)
    ф.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(ф, "at", encoding="utf-8") as о:
        о.write(json.dumps(стр, ensure_ascii=False) + "\n")
    return ф


# ---------------------------------------------------- производные ряды
#
# ЧТО ЭТО И ЗАЧЕМ. Слово владельца 16.10: сырое остаётся на ящике 30 суток
# плюс суточный артефакт Actions, а В ВЕТКУ едут ТОЛЬКО производные минутные
# ряды. Здесь они и считаются -- из сырых снимков, без сети.
#
# ГЛУБИНА СЧИТАЕТСЯ ОТ СЕРЕДИНЫ, А НЕ ОТ ЛУЧШЕЙ ЦЕНЫ, и это не вкус: награда
# за ликвидность считается по спреду ОТ СЕРЕДИНЫ (у каждого рынка свой
# максимальный спред от середины, внутри него ордер зачитывается). Мерить
# глубину от лучшей цены значило бы считать не то, что считает их формула:
# при широком спреде внутри +-1 цента от середины может не оказаться ни
# одного уровня, хотя от лучшей цены их там полно.
ГЛУБИНА_ЦЕНТОВ = (1, 5)


def _чис(x):
    """Число из строки или числа; что не число -- None, а не ноль.

    Ноль вместо None здесь врал бы: пустая сторона книги и сторона с нулевой
    ценой -- разные вещи.
    """
    if x is None or isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def уровни(книга: dict, сторона: str) -> list:
    """Уровни одной стороны как (цена, размер), мусор отброшен."""
    из_ = []
    for у in (книга.get(сторона) or []):
        if not isinstance(у, dict):
            continue
        ц, р = _чис(у.get("price")), _чис(у.get("size"))
        if ц is not None and р is not None:
            из_.append((ц, р))
    return из_


# ДОПУСК НА ДВОИЧНУЮ АРИФМЕТИКУ, И ОН ОБЯЗАТЕЛЕН.
# Цены приходят строками в центах, но сравнение идёт в double: 0.46 - 0.45
# даёт 0.010000000000000009, то есть БОЛЬШЕ 0.01. Без допуска уровень у самой
# лучшей цены выпадал бы из «+-1 цент» -- ровно тот уровень, который для
# награды важнее всех. Допуск взят на порядки меньше тика (0.01), поэтому
# лишнего уровня он захватить не может.
ДОПУСК_ЦЕНЫ = 1e-9


def глубина_от_середины(урв: list, mid: float, центов: int,
                        сторона: str) -> float:
    """Сумма размеров в `центов` от СЕРЕДИНЫ. Сторона задаёт направление."""
    порог = центов / 100.0 + ДОПУСК_ЦЕНЫ
    если = 0.0
    for ц, р in урв:
        внутри = (mid - ц <= порог) if сторона == "bids" else (ц - mid <= порог)
        своя = (ц <= mid + ДОПУСК_ЦЕНЫ) if сторона == "bids" \
            else (ц >= mid - ДОПУСК_ЦЕНЫ)
        if внутри and своя:
            если += р
    return если


def ряд_из_книги(книга: dict) -> dict:
    """Производная строка из ОДНОЙ книги: лучшие, середина, спред, глубина."""
    бид = sorted(уровни(книга, "bids"), key=lambda у: -у[0])
    аск = sorted(уровни(книга, "asks"), key=lambda у: у[0])
    л_бид = бид[0][0] if бид else None
    л_аск = аск[0][0] if аск else None
    mid = ((л_бид + л_аск) / 2) if (л_бид is not None and л_аск is not None) \
        else None
    если = {"token_id": книга.get("asset_id"),
            "conditionId": книга.get("market"),
            "luchshij_bid": л_бид, "luchshij_ask": л_аск, "mid": mid,
            "spred": (л_аск - л_бид) if mid is not None else None,
            "urovnej_bid": len(бид), "urovnej_ask": len(аск),
            "razmer_luchshego_bid": бид[0][1] if бид else None,
            "razmer_luchshego_ask": аск[0][1] if аск else None,
            "tick_size": _чис(книга.get("tick_size")),
            "min_order_size": _чис(книга.get("min_order_size")),
            "poslednjaja_sdelka": _чис(книга.get("last_trade_price"))}
    for ц in ГЛУБИНА_ЦЕНТОВ:
        если[f"glubina_bid_{ц}c"] = (
            глубина_от_середины(бид, mid, ц, "bids") if mid is not None else None)
        если[f"glubina_ask_{ц}c"] = (
            глубина_от_середины(аск, mid, ц, "asks") if mid is not None else None)
    return если


# ПАРАМЕТРЫ ВОЗНАГРАЖДЕНИЙ. Gamma кладёт их то ПЛОСКО рядом с полями рынка,
# то внутрь объекта `rewards` -- по открытым разборам встречаются обе формы, и
# где именно сидит `rewardsDailyRate` (в записи `clobRewards` или наверху),
# тоже расходится. Поэтому разбираем ОБЕ формы и ЗАПИСЫВАЕМ, какая встретилась
# (`nagrada_forma`): на ящике сразу станет видно правду, а догадка в архив не
# попадёт. Чего не нашли -- None, не ноль.
ПОЛЯ_НАГРАДЫ = ("rewardsMinSize", "rewardsMaxSpread", "clobRewards",
                "rewardsDailyRate")


def награда_рынка(рынок: dict) -> dict:
    """Параметры вознаграждения -- обеими формами, с отметкой какой."""
    если: dict = {"nagrada_forma": None}
    вложено = рынок.get("rewards")
    источники = []
    if isinstance(вложено, dict):
        источники.append(("rewards", вложено))
    источники.append(("плоско", рынок))
    нашли = {}
    формы = []
    for имя, где in источники:
        for к in ПОЛЯ_НАГРАДЫ:
            if к in где and где[к] is not None and к not in нашли:
                нашли[к] = где[к]
                формы.append(имя)
    если["nagrada_min_size"] = _чис(нашли.get("rewardsMinSize"))
    если["nagrada_max_spread"] = _чис(нашли.get("rewardsMaxSpread"))
    ставка = _чис(нашли.get("rewardsDailyRate"))
    котёл = нашли.get("clobRewards")
    if ставка is None and isinstance(котёл, list):
        # Ставка может сидеть ВНУТРИ записи clobRewards -- складываем все.
        сумма = 0.0
        было = False
        for з in котёл:
            if isinstance(з, dict):
                с = _чис(з.get("rewardsDailyRate"))
                if с is not None:
                    сумма += с
                    было = True
        ставка = сумма if было else None
    если["nagrada_sutochnaja_stavka"] = ставка
    если["nagrada_zapisej"] = len(котёл) if isinstance(котёл, list) else None
    если["nagrada_forma"] = ",".join(sorted(set(формы))) if формы else None
    return если


def _минута(метка) -> str | None:
    """Минута UTC из метки сделки. Секунды и МИЛЛИсекунды -- обе формы.

    Метки приходят и в секундах, и в миллисекундах; принять миллисекунды за
    секунды значит уехать в 56-тысячный год и свалить всю минутную разбивку.
    Порог 1e11 лежит между 1973 годом в миллисекундах и 5138 годом в
    секундах, то есть ошибиться на живых данных нечем.
    """
    ч = _чис(метка)
    if ч is None:
        if isinstance(метка, str) and len(метка) >= 16 and метка[4] == "-":
            return метка[:16] + ":00Z" if метка.endswith("Z") else метка[:16]
        return None
    if ч > 1e11:
        ч /= 1000.0
    try:
        return time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime(ч))
    except (OverflowError, OSError, ValueError):
        return None


ПОЛЯ_ID_СДЕЛКИ = ("id", "transactionHash", "transaction_hash", "tradeId",
                  "trade_id", "hash")
ПОЛЯ_МЕТКИ_СДЕЛКИ = ("timestamp", "matchTime", "match_time", "created_at",
                     "time")
ПОЛЯ_ТОКЕНА_СДЕЛКИ = ("asset_id", "assetId", "token_id", "tokenId", "asset")


def ключ_сделки(с: dict) -> tuple:
    """Опознание сделки для снятия дублей.

    `/trades?limit=500` каждую минуту отдаёт ПЕРЕКРЫВАЮЩИЕСЯ окна: одна и та
    же сделка попадёт в несколько снимков. Без снятия дублей объём за минуту
    был бы завышен кратно числу снимков, в которых сделка засветилась. Если
    своего id нет вовсе -- опознаём по совокупности полей, а не пропускаем.
    """
    for к in ПОЛЯ_ID_СДЕЛКИ:
        if с.get(к):
            return ("id", str(с[к]))
    return ("свод", str(с.get("price")), str(с.get("size")),
            str(next((с[к] for к in ПОЛЯ_МЕТКИ_СДЕЛКИ if с.get(к)), "")),
            str(next((с[к] for к in ПОЛЯ_ТОКЕНА_СДЕЛКИ if с.get(к)), "")))


def сделки_по_минутам(снимки) -> dict:
    """{(минута, token_id): {sdelok, objom}} из снимков `data_trades`.

    Минута берётся ИЗ МЕТКИ САМОЙ СДЕЛКИ, а не из метки снимка: снимок в
    12:01 несёт сделки, случившиеся раньше, и отнести их к 12:01 значило бы
    сдвинуть весь ряд.
    """
    видели: set = set()
    из_: dict = {}
    for стр in снимки:
        тело = стр.get("тело")
        спис = тело if isinstance(тело, list) else (
            тело.get("data") if isinstance(тело, dict) else None)
        if not isinstance(спис, list):
            continue
        for с in спис:
            if not isinstance(с, dict):
                continue
            к = ключ_сделки(с)
            if к in видели:
                continue
            видели.add(к)
            мин = _минута(next((с[п] for п in ПОЛЯ_МЕТКИ_СДЕЛКИ if с.get(п)),
                               None))
            если = next((с[п] for п in ПОЛЯ_ТОКЕНА_СДЕЛКИ if с.get(п)), None)
            if мин is None or если is None:
                continue
            яч = из_.setdefault((мин, str(если)),
                                {"sdelok": 0, "objom": 0.0})
            яч["sdelok"] += 1
            р = _чис(с.get("size"))
            ц = _чис(с.get("price"))
            if р is not None:
                яч["objom"] += р * (ц if ц is not None else 1.0)
    return из_


def читать_снимки(список_: str, сутки: str):
    """Снимки одного списка за сутки, по порядку часов."""
    п = ПАПКА / список_ / сутки
    if not п.is_dir():
        return
    for ф in sorted(п.glob("*.jsonl.gz")):
        # Через `with`, а не голым gzip.open: это ГЕНЕРАТОР, и читающий может
        # оборвать обход на половине файла. Без `with` открытый поток дожил бы
        # до сборки мусора, а на суточном обходе это 24 файла на список и
        # девять списков -- две сотни дескрипторов разом.
        with gzip.open(ф, "rt", encoding="utf-8") as поток:
            for стрка in поток:
                try:
                    yield json.loads(стрка)
                except ValueError:
                    continue


def proizvodnye_za_sutki(сутки: str) -> dict:
    """Минутный ряд за сутки: по строке на (минуту, токен). Пишет файлы."""
    награды: dict = {}
    for стр in читать_снимки("gamma_events", сутки):
        тело = стр.get("тело")
        события = тело if isinstance(тело, list) else (
            тело.get("data") if isinstance(тело, dict) else None)
        for с in (события or []):
            if not isinstance(с, dict):
                continue
            for р in (с.get("markets") or []):
                if not isinstance(р, dict):
                    continue
                н = награда_рынка(р)
                н["slug"] = р.get("slug")
                for т in токены_рынка(р):
                    награды[т] = н
    торговля = сделки_по_минутам(читать_снимки("data_trades", сутки))
    строк = 0
    файлов = 0
    # ОДНА ЗАПИСЬ НА ЧАСОВОЙ ФАЙЛ, А НЕ НА КАЖДУЮ КНИГУ. Сначала здесь стояло
    # дописывание на каждый ряд: это 200 книг * 1440 снимков = 288 тысяч
    # открытий gzip за сутки, и столько же ОТДЕЛЬНЫХ gzip-членов в файле --
    # читается такое, но и медленно, и файл толще без всякой пользы.
    # Часовые файлы сырого идут по порядку, поэтому часовой выход копится в
    # памяти и пишется один раз (12 тысяч строк на час, около 5 МБ).
    час_папка = ПАПКА / "clob_books" / сутки
    часы = sorted(час_папка.glob("*.jsonl.gz")) if час_папка.is_dir() else []
    for чф in часы:
        накопили = []
        with gzip.open(чф, "rt", encoding="utf-8") as поток:
            for стрка in поток:
                try:
                    стр = json.loads(стрка)
                except ValueError:
                    continue
                if стр.get("код") != 200:
                    continue
                книги = стр.get("тело")
                if not isinstance(книги, list):
                    continue
                мин = стр["utc"][:17] + "00Z"
                for к in книги:
                    if not isinstance(к, dict):
                        continue
                    ряд = ряд_из_книги(к)
                    ряд["utc"] = мин
                    ряд["snimok_utc"] = стр["utc"]
                    т = str(ряд.get("token_id") or "")
                    ряд.update(награды.get(т) or {"nagrada_forma": None})
                    тр = торговля.get((мин, т)) or {"sdelok": 0, "objom": 0.0}
                    ряд["sdelok_za_minutu"] = тр["sdelok"]
                    ряд["objom_za_minutu"] = round(тр["objom"], 6)
                    накопили.append(ряд)
        if not накопили:
            continue
        ф = ПАПКА / "proizvodnye" / сутки / чф.name
        ф.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(ф, "wt", encoding="utf-8") as о:
            for ряд in накопили:
                о.write(json.dumps(ряд, ensure_ascii=False) + "\n")
        файлов += 1
        строк += len(накопили)
    return {"sutki": сутки, "strok": строк, "fajlov": файлов,
            "rynkov_s_nagradoj": sum(
                1 for н in награды.values()
                if н.get("nagrada_max_spread") is not None),
            "tokenov_s_nagradoj": len(награды),
            "minut_s_torgovlej": len({м for м, _т in торговля})}


def predel_knig() -> int:
    """Сколько рынков влезает в ОДИН пакетный `/books` без дробления.

    У `/books` одна запись на ТОКЕН (сторона не нужна), значит на рынок две
    записи при их пределе 500 -- 250 рынков. Это вдвое больше, чем у
    минутного круга (`predel_rynkov` = 125), где самый тесный запрос
    `/prices` требует четыре записи на рынок.
    """
    return БАТЧ_ПРЕДЕЛ // 2


def chasti_po_tokenam(кандидаты: list, предел: int) -> list:
    """Кандидаты по частям так, чтобы в части было не больше `предел`
    ТОКЕНОВ.

    Делим по токенам, а не по числу рынков: `predel_knig()` считает 250
    рынков из расчёта «два токена на рынок», и для бинарных рынков это
    верно, но `clobTokenIds` -- это список, и рынок с тремя исходами в нём
    уже бывал. Делёж по рынкам тогда отправил бы в пакет больше их предела
    500 записей, а обрезала бы его ТА сторона -- молча, и часть кандидатов
    осталась бы без книги, то есть выглядела бы пустой. Рынок целиком в
    одной части: книга обеих ног нужна проверке (а).
    """
    части: list = []
    часть: list = []
    в_части = 0
    for к in кандидаты:
        сколько = len(к["tokens"])
        if часть and в_части + сколько > предел:
            части.append(часть)
            часть, в_части = [], 0
        часть.append(к)
        в_части += сколько
    if часть:
        части.append(часть)
    return части


def otbor_rynkov(rynkov: int, pauza: float, probno: bool = False,
                 segodnja: str = "") -> dict:
    """Суточный отбор: `rynkov` рынков по `volume24hr` с НЕПУСТОЙ книгой.

    Порядок: страницы `/markets` (серверный фильтр active/closed, сортировка
    по суточному объёму) -> свои фильтры и своя сортировка числом ->
    пакетный снимок `/books` по кандидатам -> берём тех, у кого книга не
    пуста, пока не наберётся `rynkov`.

    Состав фиксируется РАЗ В СУТКИ и держится сутки: проверкам (а) и (б)
    нужен устойчивый набор на 7 суток, а не состав, который едет каждые пять
    минут. Рынок, разрешившийся днём, просто даёт пустую книгу -- это видно
    в архиве и правильнее, чем подмена состава на ходу.
    """
    сутки = segodnja or time.strftime("%Y-%m-%d", time.gmtime())
    кандидаты: list = []
    итог = {"sutki": сутки, "stranic": 0, "rynkov_prosmotreno": 0,
            "kandidatov": 0, "knig_sprosheno": 0, "rynki": [], "pustyh": 0,
            "neoprosheno": 0, "hvatilo": False, "prichina": None,
            "potolok_stranic": ПОТОЛОК_СТРАНИЦ_ОТБОРА}
    for н in range(ПОТОЛОК_СТРАНИЦ_ОТБОРА):
        стр = снимок("gamma_markets", "GET", "/markets", "markets_stranica",
                     {"limit": СТРАНИЦА_РЫНКОВ, "offset": н * СТРАНИЦА_РЫНКОВ},
                     rynkov)
        if not probno:
            записать(стр)
        итог["stranic"] += 1
        if стр.get("код") != 200:
            итог["prichina"] = (f"страница {н}: код {стр.get('код')} "
                                f"({стр.get('ошибка')})")
            break
        тело = стр.get("тело")
        всего = len(тело) if isinstance(тело, list) else 0
        итог["rynkov_prosmotreno"] += всего
        кандидаты.extend(kandidaty_iz_stranicy(тело))
        time.sleep(pauza)
        # Страница короче предела -- рынки кончились, дальше ходить незачем.
        if всего < СТРАНИЦА_РЫНКОВ:
            break
    кандидаты = po_objomu(кандидаты)
    итог["kandidatov"] = len(кандидаты)
    книги: dict = {}
    выбор_книг: dict = {"rynki": [], "pustyh": [], "neoprosheno": [],
                        "tokenov": 0, "hvatilo": False}
    части = chasti_po_tokenam(кандидаты, БАТЧ_ПРЕДЕЛ)
    for н, часть in enumerate(части):
        токены = [т for к in часть for т in к["tokens"]]
        # rynkov здесь -- predel_knig(), чтобы срез в `тело_запроса`
        # (rynkov * 2) не обрезал пакет: в части не больше БАТЧ_ПРЕДЕЛ
        # токенов по построению.
        стр = снимок("clob_books_otbor", "POST", "/books", "tokens",
                     {"tokens": токены, "markets": []}, predel_knig())
        if not probno:
            записать(стр)
        итог["knig_sprosheno"] += len(токены)
        if стр.get("код") != 200:
            итог["prichina"] = (f"книги, часть {н + 1} из {len(части)} "
                                f"({len(токены)} токенов): код "
                                f"{стр.get('код')} ({стр.get('ошибка')})")
            break
        книги.update(knigi_po_tokenam(стр.get("тело")))
        выбор_книг = vybrat_po_knigam(кандидаты, книги, rynkov)
        time.sleep(pauza)
        if выбор_книг["hvatilo"]:
            break
    итог["rynki"] = выбор_книг["rynki"]
    итог["pustyh"] = len(выбор_книг["pustyh"])
    итог["neoprosheno"] = len(выбор_книг["neoprosheno"])
    итог["hvatilo"] = выбор_книг["hvatilo"]
    итог["tokenov"] = выбор_книг["tokenov"]
    if not итог["rynki"]:
        итог["prichina"] = итог["prichina"] or (
            "ни одного рынка с непустой книгой среди "
            f"{len(кандидаты)} кандидатов")
        return итог
    когда = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    выбор = {"tokens": [т for к in итог["rynki"] for т in к["tokens"]],
             "markets": [к["conditionId"] for к in итог["rynki"]],
             "slugs": [к["slug"] for к in итог["rynki"] if к["slug"]],
             "sobytij": 0, "когда": когда, "otbor_sutki": сутки}
    if not probno:
        ВЫБОР.parent.mkdir(parents=True, exist_ok=True)
        ВЫБОР.write_text(json.dumps(выбор, ensure_ascii=False),
                         encoding="utf-8")
        # Суточный список -- В ПРОИЗВОДНЫЕ, то есть в ветку: по нему видно,
        # на каком составе считаны ряды этих суток. Без него производные
        # нечем объяснить через неделю.
        сп = ПАПКА / "proizvodnye" / сутки / "otbor.json"
        сп.parent.mkdir(parents=True, exist_ok=True)
        сп.write_text(json.dumps(
            {"sutki": сутки, "kogda": когда, "rynkov": len(итог["rynki"]),
             "tokenov": итог["tokenov"],
             "rynki": [{"sutki": сутки, "conditionId": к["conditionId"],
                        "slug": к["slug"], "volume24hr": к["volume24hr"],
                        "token_ids": к["tokens"],
                        "tokenov_s_knigoj": к["tokenov_s_knigoj"],
                        "bestBid": к["bestBid"], "bestAsk": к["bestAsk"],
                        "acceptingOrders": к["acceptingOrders"]}
                       for к in итог["rynki"]],
             "prosmotreno": {"stranic": итог["stranic"],
                             "rynkov": итог["rynkov_prosmotreno"],
                             "kandidatov": итог["kandidatov"],
                             "knig_sprosheno": итог["knig_sprosheno"],
                             "pustyh": итог["pustyh"],
                             "neoprosheno": итог["neoprosheno"]},
             "hvatilo": итог["hvatilo"], "prichina": итог["prichina"]},
            ensure_ascii=False, indent=1), encoding="utf-8")
        итог["spisok"] = str(сп.relative_to(КОРЕНЬ))
    return итог


СБОРЫ_ПОДРЯД_БЕЗ_ОТВЕТА = 5


def sbory_po_tokenam(выбор: dict, rynkov: int, pauza: float,
                     probno: bool = False) -> dict:
    """Ставка сбора по КАЖДОМУ токену выбора. Раз в сутки, суточным кругом.

    Пятиминутный список спрашивает `/fee-rate` только по ПЕРВОМУ токену --
    это проба живости, и для проверки (а) её мало: там нужна ставка КАЖДОЙ
    ноги полного набора, иначе одна намеренная ставка выдаётся за ставку
    всех рынков. Пакетной формы у `/fee-rate` нет: пакеты («up to 500
    tokens») есть только у запросов стакана -- `/books`, `/prices`,
    `/midpoints`, `/spreads`. Поэтому это N отдельных запросов, по одному на
    токен, с паузой между ними.

    Расход: при 100 рынках -- 200 запросов ОДИН раз в сутки. Против
    заявленного предела CLOB (9 000 за 10 секунд на всё) это доли процента
    даже без паузы; с паузой 0.4 с обход занимает около 80 секунд, с 1.0 с --
    около 200. Настоящие лимиты покажут заголовки, которые сохраняются у
    каждого снимка, а не пересказ страницы.

    Строки идут в тот же список `clob_fee_rate`, и у каждой в разборе есть
    `token_id` -- иначе ряды по разным токенам в архиве не различить.
    """
    токены = tokeny_dlja_sborov(выбор, rynkov)
    итог = {"tokenov": len(токены), "sprosheno": 0, "otvetili": 0,
            "bez_otveta": 0, "stavki": {}, "ostanovlen": None}
    подряд = 0
    for т in токены:
        стр = снимок("clob_fee_rate", "GET", "/fee-rate", "token_odin",
                     {"tokens": [т], "markets": выбор.get("markets") or []},
                     rynkov)
        if not probno:
            записать(стр)
        итог["sprosheno"] += 1
        if стр.get("код") == 200:
            итог["otvetili"] += 1
            итог["stavki"][т] = (стр.get("разбор") or {}).get("base_fee")
            подряд = 0
        else:
            итог["bez_otveta"] += 1
            # Ответа НЕ БЫЛО ВОВСЕ (код None) -- это закрытая сеть, а не
            # ответ источника. Писать 200 одинаковых отказных строк каждые
            # сутки незачем: обход останавливается и называет причину. Код
            # ответа (404, 429) ответом СЧИТАЕТСЯ и обход не останавливает --
            # иначе один отказ по одному токену гасил бы остальные.
            подряд = подряд + 1 if стр.get("код") is None else 0
            if подряд >= СБОРЫ_ПОДРЯД_БЕЗ_ОТВЕТА:
                итог["ostanovlen"] = (
                    f"{подряд} подряд без ответа вовсе (последняя причина: "
                    f"{стр.get('ошибка')}) -- остановился на {итог['sprosheno']} "
                    f"из {len(токены)}, чтобы не писать сутки одинаковых "
                    f"отказов")
                break
        time.sleep(pauza)
    return итог


def сутки_в_syrom() -> list:
    """Какие сутки есть в сырых снимках книг."""
    п = ПАПКА / "clob_books"
    if not п.is_dir():
        return []
    return sorted(д.name for д in п.iterdir()
                  if д.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", д.name))


# ---------------------------------------------------- ротация сырого

ПАПКА_ОТМЕТОК = ПАПКА / "artefakty"
СУТОК_СЫРОГО = 30


def отметить_артефакт(сутки: str, артефакт: str) -> Path:
    """Отметка «сырое за эти сутки уехало в артефакт». Ставит прогон Actions."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", сутки):
        raise ValueError(f"сутки должны быть YYYY-MM-DD, а не {сутки!r}")
    if not артефакт:
        raise ValueError("нужно имя или id артефакта: отметка без него "
                         "ничего не подтверждает")
    ПАПКА_ОТМЕТОК.mkdir(parents=True, exist_ok=True)
    ф = ПАПКА_ОТМЕТОК / f"{сутки}.json"
    ф.write_text(json.dumps(
        {"sutki": сутки, "artefakt": артефакт,
         "kogda": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        ensure_ascii=False), encoding="utf-8")
    return ф


def артефакт_подтверждён(сутки: str) -> bool:
    ф = ПАПКА_ОТМЕТОК / f"{сутки}.json"
    if not ф.exists():
        return False
    try:
        д = json.loads(ф.read_text(encoding="utf-8"))
    except ValueError:
        return False
    return bool(д.get("artefakt"))


def rotacija(*, dnej: int = СУТОК_СЫРОГО, segodnja: str | None = None,
             probno: bool = True) -> dict:
    """СЫРОЕ СТАРШЕ `dnej` УДАЛЯЕТСЯ ТОЛЬКО ПОСЛЕ ПОДТВЕРЖДЁННОГО АРТЕФАКТА.

    Без отметки в data/polymarket/artefakty/<сутки>.json не удаляется ничего
    и сутки называются вслух. Это и есть смысл строки ротации: потерять сырое
    молча нельзя, а «наверное уехало» подтверждением не считается.
    """
    если_сегодня = segodnja or time.strftime("%Y-%m-%d", time.gmtime())
    # ПОРОГ СЧИТАЕТСЯ В UTC, через calendar.timegm. time.mktime взял бы
    # МЕСТНОЕ время, и на ящике с другим часовым поясом порог съехал бы на
    # сутки -- то есть удалил бы на день больше или меньше, чем сказано.
    порог = time.strftime(
        "%Y-%m-%d",
        time.gmtime(calendar.timegm(time.strptime(если_сегодня, "%Y-%m-%d"))
                    - dnej * 86400))
    удалить, ждут = [], []
    for список_ in sorted(п.name for п in ПАПКА.iterdir()
                          if п.is_dir() and п.name not in
                          ("proizvodnye", "artefakty")):
        for д in sorted((ПАПКА / список_).iterdir()):
            if not д.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", д.name):
                continue
            if д.name >= порог:
                continue
            if артефакт_подтверждён(д.name):
                удалить.append(д)
            else:
                ждут.append(д)
    снято = 0
    if not probno:
        for д in удалить:
            for ф in д.glob("*"):
                ф.unlink()
            д.rmdir()
            снято += 1
    return {"porog": порог, "dnej": dnej,
            "udalit": [str(д.relative_to(ПАПКА)) for д in удалить],
            "zhdut_artefakta": [str(д.relative_to(ПАПКА)) for д in ждут],
            "snjato": снято, "probno": probno}


# ---------------------------------------------------- оценка объёма

# ЗАМЕР, А НЕ ДОГАДКА. Степень сжатия взята на НАШИХ ЖЕ файлах того же
# формата (data/trending/*/2026-10-12.jsonl.gz, ветка claude/podbivka):
# jupiter 160 903 544 -> 45 670 122 (3.52x), geckoterminal 13 344 504 ->
# 2 682 250 (4.98x), dexscreener 13 307 636 -> 2 935 509 (4.53x).
# Берём нижнюю из трёх -- так оценка не окажется приятнее правды.
СЖАТИЕ_ИЗМЕРЕНО = (3.52, 4.98, 4.53)
СЖАТИЕ = min(СЖАТИЕ_ИЗМЕРЕНО)

# Размер одной книги считается по СХЕМЕ ОТВЕТА их клиента
# (py_clob_client.clob_types.OrderBookSummary): market, asset_id, timestamp,
# bids, asks, min_order_size, neg_risk, tick_size, last_trade_price, hash.
# Постоянная часть -- 376 знаков при компактной разделке (адрес условия 66,
# id токена ~77 цифр, hash 66); уровень `{"price":"0.482","size":"1234.56"}`
# с запятой -- 35.
КНИГА_ПОСТОЯННАЯ = 376
КНИГА_УРОВЕНЬ = 35


def ocenka_objoma(rynkov: int, urovnej_na_storonu: int, *,
                  minut_v_sutkah: int = 1440) -> dict:
    """Сколько в сутки при `rynkov` рынках и такой глубине книги.

    Глубина -- ЕДИНСТВЕННОЕ, чего я не знаю из документации, и выдумывать её
    не буду: она приходит параметром, а настоящую покажет `--svodka` на
    ящике (поле `urovnej` в разборе каждого снимка книг).
    """
    if rynkov < 1 or urovnej_na_storonu < 0:
        raise ValueError("рынков должно быть хотя бы 1, уровней -- не меньше 0")
    книг = rynkov * 2                      # два исхода на рынок, Yes и No
    байт_книги = КНИГА_ПОСТОЯННАЯ + КНИГА_УРОВЕНЬ * urovnej_na_storonu * 2
    за_круг = книг * байт_книги
    сырое = за_круг * minut_v_sutkah
    return {"rynkov": rynkov, "knig_v_krugu": книг,
            "bajt_knigi": байт_книги, "bajt_za_krug": за_круг,
            "syroe_v_sutki": сырое,
            "gz_v_sutki": int(сырое / СЖАТИЕ),
            "gz_v_chas": int(сырое / СЖАТИЕ / 24),
            "sutochnyj_vljazet_v_100mb": сырое / СЖАТИЕ <= 100e6}


def pokazat_ocenku() -> int:
    print(f"степень сжатия {СЖАТИЕ:.2f}x -- НИЖНЯЯ из замеренных на наших же "
          f"файлах {СЖАТИЕ_ИЗМЕРЕНО}")
    print(f"книга = {КНИГА_ПОСТОЯННАЯ} + {КНИГА_УРОВЕНЬ} * уровней * 2 "
          "(схема OrderBookSummary их клиента)")
    print(f"\n{'рынков':>7s} {'уровней':>8s} {'книг/круг':>10s} "
          f"{'сырое/сут':>12s} {'gz/сут':>11s} {'gz/час':>10s} {'в 100 МБ?':>10s}")
    for rynkov in (50, 100):
        for ур in (10, 30, 60):
            о = ocenka_objoma(rynkov, ур)
            print(f"{rynkov:7d} {ур:8d} {о['knig_v_krugu']:10d} "
                  f"{о['syroe_v_sutki'] / 1e6:10.0f} МБ "
                  f"{о['gz_v_sutki'] / 1e6:8.1f} МБ "
                  f"{о['gz_v_chas'] / 1e6:7.2f} МБ "
                  f"{'да' if о['sutochnyj_vljazet_v_100mb'] else 'НЕТ':>10s}")
    print("\nтолько книги. Остальные списки -- в docs/polymarket_put.md")
    return 0


# ---------------------------------------------------- пуш

# В ВЕТКУ ЕДУТ ТОЛЬКО ПРОИЗВОДНЫЕ И ОТМЕТКИ ОБ АРТЕФАКТАХ -- слово владельца
# 16.10. Сырое остаётся на ящике 30 суток плюс суточный артефакт Actions
# (retention 90). Поэтому в коммит идут ровно эти два пути, а не вся
# data/polymarket: иначе 340 МБ в сутки уехали бы в историю git.
ПУТИ_В_ВЕТКУ = ("data/polymarket/proizvodnye", "data/polymarket/artefakty")


def push(vetka: str, *, probno: bool = False) -> int:
    """Суточный пуш: в коммит идут ТОЛЬКО производные, и только со своей ветки."""
    текущая = subprocess.run(
        ["git", "-C", str(КОРЕНЬ), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=False).stdout.strip()
    if текущая != vetka:
        print(f"ОТКАЗ: checkout на {текущая!r}, а пуш настроен на {vetka!r}. "
              "Чужую ветку не трогаю", file=sys.stderr)
        return 2
    if probno:
        print(f"пробно: запушил бы {', '.join(ПУТИ_В_ВЕТКУ)} в {vetka}; "
              "сырое НЕ пушится вовсе")
        return 0
    subprocess.run(["git", "-C", str(КОРЕНЬ), "add", "--", *ПУТИ_В_ВЕТКУ],
                   check=True)
    есть = subprocess.run(
        ["git", "-C", str(КОРЕНЬ), "diff", "--cached", "--quiet"],
        check=False).returncode
    if есть == 0:
        print("нечего пушить")
        return 0
    subprocess.run(["git", "-C", str(КОРЕНЬ), "commit", "-q", "-m",
                    "Polymarket: proizvodnye minutnye rjady [automated]"],
                   check=True)
    for н, пауза in enumerate((2, 4, 8, 16), 1):
        к = subprocess.run(["git", "-C", str(КОРЕНЬ), "push", "-q", "origin",
                            f"HEAD:{vetka}"], check=False).returncode
        if к == 0:
            print(f"запушено с попытки {н}")
            return 0
        time.sleep(пауза)
    print("ОТКАЗ: четыре попытки пуша не прошли", file=sys.stderr)
    return 1


# ---------------------------------------------------- свод

def свод() -> int:
    всего = 0
    for п in sorted(ПАПКА.glob("*")):
        if not п.is_dir():
            continue
        снимков = байт = 0
        уровней = книг = 0
        for ф in sorted(п.rglob("*.jsonl.gz")):
            байт += ф.stat().st_size
            with gzip.open(ф, "rt", encoding="utf-8") as поток:
                for стрка in поток:
                    снимков += 1
                    try:
                        с = json.loads(стрка)
                    except ValueError:
                        continue
                    р = с.get("разбор") or {}
                    уровней += int(р.get("urovnej") or 0)
                    книг += int(р.get("knig") or 0)
        всего += байт
        хвост = (f" книг {книг} уровней {уровней} "
                 f"(в среднем {уровней / книг:.1f} на книгу)"
                 if книг else "")
        print(f"{п.name:16} снимков {снимков:6d} файлов "
              f"{байт / 1e6:8.2f} МБ{хвост}", flush=True)
    print(f"всего {всего / 1e6:.2f} МБ", flush=True)
    return 0


# ---------------------------------------------------- прогон

def главное(а) -> int:
    if а.svodka:
        return свод()
    if а.ocenka:
        return pokazat_ocenku()
    if а.push:
        return push(а.vetka, probno=а.probno)
    if а.otmetit_artefakt:
        try:
            ф = отметить_артефакт(а.otmetit_artefakt, а.artefakt)
        except ValueError as сбой:
            print(f"ОТКАЗ: {сбой}", file=sys.stderr)
            return 2
        print(f"отметка поставлена: {ф.relative_to(КОРЕНЬ)}")
        return 0
    if а.otbor:
        # ПОТОЛОК ПРОВЕРЯЕТСЯ ДО ПЕРВОГО ЗАПРОСА, как и в минутном круге:
        # отобрать больше рынков, чем круг потом сможет опросить одним
        # батчем, значит собрать состав, половина которого не ведётся.
        if а.rynkov > predel_rynkov():
            print(f"ОТКАЗ: рынков {а.rynkov}, а минутный круг ведёт без "
                  f"дробления {predel_rynkov()} (на рынок четыре записи в "
                  f"/prices при их пределе {БАТЧ_ПРЕДЕЛ})", file=sys.stderr)
            return 2
        из_ = otbor_rynkov(а.rynkov, а.pauza, probno=а.probno,
                           segodnja=а.sutki)
        print(f"отбор за {из_['sutki']}: страниц {из_['stranic']}, "
              f"рынков просмотрено {из_['rynkov_prosmotreno']}, кандидатов "
              f"{из_['kandidatov']}, книг спрошено {из_['knig_sprosheno']}")
        print(f"выбрано рынков {len(из_['rynki'])} (токенов "
              f"{из_.get('tokenov', 0)}), с пустой книгой отброшено "
              f"{из_['pustyh']}, не опрошено {из_['neoprosheno']}")
        if из_.get("spisok"):
            print(f"суточный список: {из_['spisok']}")
        for к in из_["rynki"][:5]:
            print(f"   {к['volume24hr']:>14.2f}  {к['slug'][:60]}")
        if not из_["hvatilo"]:
            # Не набрали заказанное -- говорим ВСЛУХ и называем потолок:
            # молчаливое «сколько есть» потом читалось бы как «столько и
            # было».
            print(f"НЕ НАБРАЛОСЬ {а.rynkov}: выбрано {len(из_['rynki'])}; "
                  f"потолок страниц {из_['potolok_stranic']}; причина: "
                  f"{из_['prichina']}", file=sys.stderr)
        return 0 if из_["rynki"] else 2
    if а.sbory:
        выбор = прочесть_выбор()
        # ПУСТОЙ ВЫБОР -- НЕ ОТКАЗ, И КОД ВОЗВРАТА ЗДЕСЬ НОЛЬ НАРОЧНО. Обход
        # стоит в суточном круге как ExecStartPre перед --proizvodnye: верни
        # он ненулевой код, systemd оборвал бы весь юнит, и производные ряды
        # (они важнее сборов) не посчитались бы вовсе. А выбор пуст ровно
        # тогда, когда ещё ни один снимок gamma_events не прошёл, -- это
        # состояние первого запуска, а не поломка. Причина называется вслух.
        if not tokeny_dlja_sborov(выбор, а.rynkov):
            print("сборы: в выборе нет ни одного токена -- нечего спрашивать. "
                  "Сначала должен пройти удачный снимок gamma_events "
                  f"({ВЫБОР.relative_to(КОРЕНЬ)}); производные это не "
                  "останавливает", file=sys.stderr)
            return 0
        если = sbory_po_tokenam(выбор, а.rynkov, а.pauza, probno=а.probno)
        # Сортировка ПО СТРОКЕ нарочно: у их типизированного ответа base_fee
        # целое (u32), но живого тела я не видел, а если источник отдаст где
        # число, где строку, sorted по смешанным типам бросил бы TypeError --
        # и уронил бы ExecStartPre, то есть весь суточный круг вместе с
        # производными. Печатаем значения, а не считаем ими.
        знач = sorted({str(с) for с in если["stavki"].values() if с is not None})
        print(f"сборы: спрошено {если['sprosheno']} из {если['tokenov']}, "
              f"ответили {если['otvetili']}, без ответа {если['bez_otveta']}")
        print(f"разных ставок: {len(знач)} {знач[:10]}")
        if если["ostanovlen"]:
            print(f"ОСТАНОВЛЕН: {если['ostanovlen']}", file=sys.stderr)
        return 0
    if а.rotacija:
        из_ = rotacija(dnej=а.dnej, probno=а.probno)
        print(f"порог {из_['porog']} ({из_['dnej']} суток)")
        print(f"удалить (артефакт подтверждён): {len(из_['udalit'])}")
        for п in из_["udalit"]:
            print(f"   {п}")
        print(f"ЖДУТ АРТЕФАКТА, НЕ УДАЛЯЮ: {len(из_['zhdut_artefakta'])}")
        for п in из_["zhdut_artefakta"]:
            print(f"   {п}")
        print(f"снято {из_['snjato']}"
              + ("  (пробно -- ничего не удалено)" if из_["probno"] else ""))
        if из_["zhdut_artefakta"]:
            print("сырое без подтверждённого артефакта не удаляется НИКОГДА: "
                  "сначала прогон Actions, потом --otmetit-artefakt")
        return 0
    if а.proizvodnye:
        # БЕЗ --sutki ИДЁМ ОТ НОВЫХ К СТАРЫМ. Суточный юнит зовёт
        # `--sutki "$(date -u -d yesterday +%F)"` и считает одни сутки -- так
        # задумано. Но если `--sutki` не передали, обход от СТАРЫХ к новым при
        # 30 сутках сырого на ящике умер бы по TimeoutStartSec=1800, не дойдя
        # до свежих суток, то есть самые нужные ряды не посчитались бы вовсе.
        # Обратный порядок делает обрыв по таймауту безвредным: свежее уже
        # готово.
        сутки = [а.sutki] if а.sutki else list(reversed(сутки_в_syrom()))
        if not сутки:
            print("ОТКАЗ: сырых снимков книг нет ни за одни сутки "
                  f"({ПАПКА / 'clob_books'}): производные считать не из чего",
                  file=sys.stderr)
            return 2
        for д in сутки:
            из_ = proizvodnye_za_sutki(д)
            print(f"{из_['sutki']}: строк {из_['strok']}, файлов "
                  f"{из_['fajlov']}, токенов с наградой "
                  f"{из_['tokenov_s_nagradoj']} (из них с max_spread "
                  f"{из_['rynkov_s_nagradoj']}), минут с торговлей "
                  f"{из_['minut_s_torgovlej']}", flush=True)
        return 0
    что = [x for x in СПИСКИ
           if (а.kazhdye == "все" or x[1] == а.kazhdye)
           and (not а.spisok or x[0] == а.spisok)]
    if not что:
        print(f"ОТКАЗ: по отбору (--kazhdye {а.kazhdye} --spisok "
              f"{а.spisok!r}) не осталось ни одного списка. Ждал один из "
              f"{[x[0] for x in СПИСКИ]}", file=sys.stderr)
        return 2
    # ПОТОЛОК БАТЧА ПРОВЕРЯЕТСЯ ДО ПЕРВОГО ЗАПРОСА. Иначе тело `/prices`
    # молча ушло бы длиннее их предела, и мы не узнали бы, обрезали его на
    # той стороне или нет.
    if а.rynkov > predel_rynkov():
        print(f"ОТКАЗ: рынков {а.rynkov}, а в батч без дробления влезает "
              f"{predel_rynkov()}: на рынок два токена, а /prices требует "
              f"сторону, то есть ЧЕТЫРЕ записи на рынок при их пределе "
              f"{БАТЧ_ПРЕДЕЛ}. Дробления на части здесь нет -- заказ 50-100 "
              "рынков в потолок укладывается", file=sys.stderr)
        return 2
    выбор = прочесть_выбор()
    итог = []
    for список_, _частота, метод, путь, подст in что:
        стр = снимок(список_, метод, путь, подст, выбор, а.rynkov)
        if not а.probno:
            записать(стр)
        # СОСТАВ РЫНКОВ ЗДЕСЬ НЕ МЕНЯЕТСЯ -- и это главная правка 16.10.
        # Раньше удачный снимок `/events` перезаписывал выбор: сто самых
        # объёмных СОБЫТИЙ разворачивались в 4086 рынков, с них снимались
        # первые 100 токенов, и это оказывался хвост многоисходных событий,
        # которые никогда не торговались -- на ящике из 200 запрошенных
        # токенов книг пришло 10, все пустые. Состав теперь задаёт ТОЛЬКО
        # суточный `--otbor` (100 рынков по volume24hr с проверенной непустой
        # книгой), и держится сутки: проверкам (а) и (б) нужен устойчивый
        # набор на 7 суток. Снимок `/events` остаётся как есть -- он сырое, и
        # его разбор по-прежнему пишется в строку.
        итог.append(стр)
        разб = стр.get("разбор") or {}
        print(f"{список_}: код {стр.get('код')} байт {стр.get('байт')} "
              f"{разб or ''} {стр.get('ошибка') or ''}".rstrip(), flush=True)
        time.sleep(а.pauza)
    ок = sum(1 for с in итог if с.get("код") == 200)
    print(f"итого: ответили {ок} из {len(итог)}", flush=True)
    if not ок:
        print("ни один список не ответил -- смотри причины выше: это может "
              "быть и сетевая политика окружения, и сам источник", flush=True)
    return 0


# ---------------------------------------------------- самопроверка

# Было 35 -> 42 -> 54, и каждая прибавка пришла с ящика, а не из головы:
# 42 -- имя поля сортировки (Gamma отбивала 422, а самопроверка была зелёная,
#       потому что сверяла запрос с тем же неверным именем) и суточный обход
#       сборов по каждому токену;
# 54 -- суточный отбор состава: запрошено 200 токенов, книг пришло 10, все с
#       пустыми bids/asks, потому что состав брался из хвоста многоисходных
#       событий. Двенадцать проверок на отбор, в том числе «в выборе нет
#       рынка с пустой книгой».
ZHDEM_PROVEROK = 55


def self_test() -> int:
    было = плохо = 0
    упавшие = []

    def chk(имя, условие, факт=None):
        nonlocal было, плохо
        было += 1
        if условие:
            print(f"  ok   {имя}")
        else:
            плохо += 1
            упавшие.append(имя)
            print(f" ПЛОХО {имя}" + (f" -- {факт}" if факт is not None else ""))

    # ----- 1. НИ ОДНОГО КЛЮЧА В КОДЕ
    # ИЩЕМ ФОРМУ ОБРАЩЕНИЯ, А НЕ СЛОВО. Сначала здесь стоял поиск слов
    # `environ`/`getenv` по всему файлу -- и проверка упала на СВОЁМ ЖЕ
    # описании, где эти слова названы прозой. Прочитать переменную окружения
    # можно только одной из форм ниже: индексом, .get или вызовом getenv.
    # Прозу они не задевают, а настоящее обращение не пропустят.
    свой = Path(__file__).read_text(encoding="utf-8")
    тело_кода = свой.split("def self_test", 1)[0]
    ключи = re.findall(
        r"os\.environ|environ\s*\[|environ\s*\.\s*get|getenv\s*\(",
        тело_кода)
    chk("НИ ОДНОГО КЛЮЧА: в рабочей части файла нет ни одной ФОРМЫ обращения "
        "к переменным окружения (os.environ, environ[...], environ.get, "
        "getenv(...)) -- значит ни кошелькового, ни API-ключа скрипт не "
        f"читает и прочитать не может (найдено: {ключи})",
        not ключи, ключи)

    # ----- 2. ХОСТЫ ПО ПРИСТАВКЕ
    chk("ХОСТ БЕРЁТСЯ ПО ПРИСТАВКЕ ИМЕНИ СПИСКА, а неизвестная приставка -- "
        "отказ по имени, а не запрос наугад",
        хост("gamma_events") == GAMMA and хост("clob_books") == CLOB
        and хост("data_trades") == DATA)
    упало = None
    try:
        хост("neizvestno_chto")
    except ValueError as сбой:
        упало = str(сбой)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: приставка не из трёх известных -- отказ с "
        "перечислением ожидаемых, а не молчаливый GET в пустоту",
        упало and "неизвестная приставка" in упало, упало)

    # ----- 3. ТОКЕНЫ ИЗ GAMMA: СТРОКА С JSON ВНУТРИ
    chk("ТОКЕНЫ ЧИТАЮТСЯ ИЗ `clobTokenIds`, ДАЖЕ КОГДА ЭТО СТРОКА С JSON "
        "ВНУТРИ -- именно так их отдаёт Gamma, и список бы иначе вышел пустым",
        токены_рынка({"clobTokenIds": '["111","222"]'}) == ["111", "222"])
    chk("та же форма списком и форма CLOB `tokens:[{token_id}]` -- тоже "
        "читаются: формат я не выбираю",
        токены_рынка({"clobTokenIds": ["7", "8"]}) == ["7", "8"]
        and токены_рынка({"tokens": [{"token_id": "9", "outcome": "Yes"}]})
        == ["9"])
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: форма незнакомая -- возвращаем ПУСТО, а не "
        "догадку; снимок тогда запишется с причиной",
        токены_рынка({"clobTokenIds": "ne json"}) == []
        and токены_рынка({}) == [])

    # ----- 4. ВЫБОР ИЗ events
    события = [{"markets": [
        {"clobTokenIds": '["1","2"]', "conditionId": "0xaa", "slug": "a"},
        {"clobTokenIds": '["3","4"]', "conditionId": "0xbb", "slug": "b",
         "enableOrderBook": False},
        {"clobTokenIds": '["1","2"]', "conditionId": "0xaa", "slug": "a"}]}]
    в = выбор_из_events(события)
    chk("ВЫБОР ИЗ events: рынок С ОТКЛЮЧЁННОЙ КНИГОЙ пропускается (книги у "
        "него нет по делу), дубли убираются, порядок по объёму сохраняется: "
        f"токены {в['tokens']}, рынки {в['markets']}",
        в["tokens"] == ["1", "2"] and в["markets"] == ["0xaa"]
        and в["sobytij"] == 1, в)

    # ----- 5. ПОТОЛОК БАТЧА СЧИТАЕТСЯ, А НЕ НАЗНАЧАЕТСЯ
    chk(f"ПОТОЛОК РЫНКОВ СЧИТАЕТСЯ ИЗ ИХ ПРЕДЕЛА: {predel_rynkov()} = "
        f"{БАТЧ_ПРЕДЕЛ} / 4, потому что на рынок два токена, а /prices "
        "требует сторону -- четыре записи на рынок. Заказ 50-100 рынков "
        "внутри потолка",
        predel_rynkov() == 125 and 100 <= predel_rynkov(), predel_rynkov())
    _, т_предел, _ = тело_запроса(
        "tokens_sides", {"tokens": [f"t{и}" for и in range(400)],
                         "markets": []}, predel_rynkov())
    chk(f"И ЭТО НЕ НА СЛОВАХ: при {predel_rynkov()} рынках тело /prices "
        f"выходит {len(т_предел)} записей -- ровно в предел {БАТЧ_ПРЕДЕЛ}, "
        "а не больше",
        len(т_предел) == БАТЧ_ПРЕДЕЛ, len(т_предел))

    # ----- 6. ТЕЛА ЗАПРОСОВ
    выбор_проб = {"tokens": ["t1", "t2"], "markets": ["0xm1"], "slugs": []}
    _, т_книг, п_книг = тело_запроса("tokens", выбор_проб, 50)
    chk("ТЕЛО /books -- список {token_id}, по токену на запись: "
        f"{т_книг}", т_книг == [{"token_id": "t1"}, {"token_id": "t2"}]
        and п_книг["tokenov"] == 2, т_книг)
    _, т_цен, п_цен = тело_запроса("tokens_sides", выбор_проб, 50)
    chk("ТЕЛО /prices ТРЕБУЕТ СТОРОНУ: на каждый токен ДВЕ записи, BUY и "
        f"SELL -- иначе вернулась бы половина цены ({п_цен})",
        len(т_цен) == 4
        and {з["side"] for з in т_цен} == {"BUY", "SELL"}
        and п_цен["zapisej"] == 4, т_цен)
    х_ev, т_ev, _ = тело_запроса("events", выбор_проб, 100)
    chk("ЗАПРОС events: active=true, closed=false, сортировка по суточному "
        f"объёму, limit=100 -- и это GET без тела: {х_ev}",
        т_ev is None and "active=true" in х_ev and "closed=false" in х_ev
        and "order=volume24hr" in х_ev and "limit=100" in х_ev, х_ev)
    # ДОКАЗАННЫЙ КРАСНЫЙ НА ЖИВОЙ ОШИБКЕ 16.10. Здесь стояло
    # `order=volume_24hr`, и ЭТА САМАЯ проверка была зелёной -- она сверяла
    # запрос с тем же неверным именем. Gamma на него отвечает 422 «order
    # fields are not valid», выбор рынков остаётся пустым, и минутный круг
    # пишет нули при 35 из 35. Поэтому проверка теперь не только сверяет
    # верное имя, но и ищет неверные написания по ВСЕЙ рабочей части файла:
    # одного места мало, если имя однажды уже разъехалось.
    плохие = re.findall(r"order=volume_24hr|order=volume24Hr|"
                        r"order=volume_24h\b", тело_кода)
    chk("ИМЯ ПОЛЯ СОРТИРОВКИ: во всей рабочей части файла нет ни одного "
        "написания, которое Gamma отбивает 422 (volume_24hr, volume24Hr, "
        f"volume_24h) -- найдено {плохие}; с ящика замерено: volume24hr "
        "-> 200, volume_24hr -> 422, volume24Hr -> 422",
        not плохие and "order=volume24hr" in тело_кода, плохие)
    х_пусто, _, п_пусто = тело_запроса(
        "token_odin", {"tokens": [], "markets": []}, 50)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: выбора рынков нет -- запрос НЕ собирается, и "
        "причина называется («нет выбора рынков»), а не уходит пустой "
        "token_id в URL",
        х_пусто is None and п_пусто["prichina"] == "нет выбора рынков")

    # ----- 6а. СБОРЫ ПО КАЖДОМУ ТОКЕНУ, А НЕ ПО ПЕРВОМУ
    выбор_сб = {"tokens": ["t1", "t2", "t2", "t3"], "markets": ["0xm1"]}
    сб = tokeny_dlja_sborov(выбор_сб, 50)
    chk("СБОРЫ СПРАШИВАЮТСЯ ПО КАЖДОМУ ТОКЕНУ ВЫБОРА, не по первому: из "
        f"['t1','t2','t2','t3'] выходит {сб} -- дубль снят, порядок выбора "
        "(он задан сортировкой по объёму) сохранён",
        сб == ["t1", "t2", "t3"], сб)
    длинный = {"tokens": [f"t{и}" for и in range(400)], "markets": []}
    сб_дл = tokeny_dlja_sborov(длинный, predel_rynkov())
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: срез ТОТ ЖЕ, что у минутного круга -- при "
        f"{predel_rynkov()} рынках спрашивается {len(сб_дл)} токенов "
        f"({predel_rynkov()} x 2), а не все 400. Иначе в архиве появились бы "
        "сборы по токенам, чьих книг у нас нет, и проверка (а) по такой "
        "строке не посчитала бы ничего",
        len(сб_дл) == predel_rynkov() * 2 and сб_дл[-1] == "t249", len(сб_дл))
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: выбор пуст -- обход сборов НЕ делает ни одного "
        "запроса (пустой список токенов), а не уходит с пустым token_id",
        tokeny_dlja_sborov({"tokens": []}, 50) == []
        and tokeny_dlja_sborov({}, 50) == [])
    chk("token_id ЛОЖИТСЯ В РАЗБОР -- иначе ряды /fee-rate по разным токенам "
        "в архиве не различить, не разбирая URL: "
        f"{разбор_s_tokenom({'base_fee': 0}, {'token_id': 'T7'})}",
        разбор_s_tokenom({"base_fee": 0}, {"token_id": "T7"})
        == {"base_fee": 0, "token_id": "T7"})
    пустой_обход = sbory_po_tokenam({"tokens": []}, 50, 0.0, probno=True)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: обход сборов при пустом выборе НЕ УХОДИТ В "
        f"СЕТЬ ни разу ({пустой_обход['sprosheno']} запросов) и не пишет "
        "файлов -- иначе суточный круг каждый день стучался бы с пустым "
        "token_id",
        пустой_обход == {"tokenov": 0, "sprosheno": 0, "otvetili": 0,
                         "bez_otveta": 0, "stavki": {}, "ostanovlen": None},
        пустой_обход)
    # ----- 6б. СУТОЧНЫЙ ОТБОР СОСТАВА: 100 РЫНКОВ, А НЕ ХВОСТ СОБЫТИЙ
    х_стр, т_стр, п_стр = тело_запроса(
        "markets_stranica", {"limit": СТРАНИЦА_РЫНКОВ, "offset": 200}, 100)
    chk("СТРАНИЦА /markets: сортировка по volume24hr, предел страницы "
        f"{СТРАНИЦА_РЫНКОВ} (замер: limit=500 вернул 100), смещение в URL -- "
        f"{х_стр}",
        т_стр is None and "order=volume24hr" in х_стр
        and f"limit={СТРАНИЦА_РЫНКОВ}" in х_стр and "offset=200" in х_стр
        and "active=true" in х_стр and "closed=false" in х_стр
        and п_стр == {"limit": СТРАНИЦА_РЫНКОВ, "offset": 200}, х_стр)
    chk(f"В ОДИН ПАКЕТНЫЙ /books ВЛЕЗАЕТ {predel_knig()} РЫНКОВ -- считается "
        f"из их предела {БАТЧ_ПРЕДЕЛ}: у /books одна запись на токен, то есть "
        f"две на рынок (вдвое больше, чем {predel_rynkov()} у минутного "
        "круга, где /prices требует четыре)",
        predel_knig() == 250 and predel_knig() == 2 * predel_rynkov())
    хорош = {"conditionId": "0xaa", "slug": "horoshij", "volume24hr": 100.0,
             "clobTokenIds": '["t1","t2"]', "enableOrderBook": True,
             "active": True, "closed": False, "bestBid": 0.44,
             "bestAsk": 0.46, "acceptingOrders": True}
    chk("КАНДИДАТ ИЗ ЗАПИСИ /markets: читаются conditionId, slug, volume24hr "
        f"числом и оба токена -- {kandidat_rynka(хорош)}",
        kandidat_rynka(хорош)["tokens"] == ["t1", "t2"]
        and kandidat_rynka(хорош)["volume24hr"] == 100.0
        and kandidat_rynka(хорош)["conditionId"] == "0xaa", kandidat_rynka(хорош))
    отсев = {
        "книга выключена": dict(хорош, enableOrderBook=False),
        "не active": dict(хорош, active=False),
        "closed": dict(хорош, closed=True),
        "нет токенов": dict(хорош, clobTokenIds="[]"),
        "нет conditionId": {к: в for к, в in хорош.items() if к != "conditionId"},
    }
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: в кандидаты НЕ попадают рынки с выключенной "
        "книгой, не active, closed, без токенов и без conditionId -- "
        f"{ {и: bool(kandidat_rynka(р)) for и, р in отсев.items()} }",
        not any(kandidat_rynka(р) for р in отсев.values()),
        {и: kandidat_rynka(р) for и, р in отсев.items()})
    порядок = po_objomu([
        {"conditionId": "0x9", "slug": "девять", "volume24hr": 9.0,
         "tokens": ["a"]},
        {"conditionId": "0x100", "slug": "сто", "volume24hr": 100.0,
         "tokens": ["b"]},
        {"conditionId": "0x9", "slug": "девять ещё раз", "volume24hr": 9.0,
         "tokens": ["a"]}])
    chk("ДОКАЗАННЫЙ КРАСНЫЙ НА ЛЕКСИКОГРАФИИ: сортируем ЧИСЛОМ у себя, а не "
        "порядком ответа -- 100 впереди 9 "
        f"({[к['volume24hr'] for к in порядок]}), и дубль рынка снят "
        f"({len(порядок)} из 3). По строкам «9» шло бы впереди «100», а их "
        "сервис volume и liquidity держит ТЕКСТОВЫМИ колонками (их же py-SDK "
        "потому и подменяет имена на volumeNum/liquidityNum)",
        [к["volume24hr"] for к in порядок] == [100.0, 9.0]
        and len(порядок) == 2, порядок)
    # ПУСТАЯ КНИГА -- ТО, ИЗ-ЗА ЧЕГО ВСЁ И ПЕРЕДЕЛАНО. Подставной ответ
    # /books: у первого рынка обе стороны, у второго только бид, у третьего
    # пусто -- ровно то, что пришло с ящика (bids: [] asks: [] при полном
    # объекте).
    книги_проб = knigi_po_tokenam([
        {"asset_id": "t2", "bids": [{"price": "0.44", "size": "100"}],
         "asks": [{"price": "0.46", "size": "50"}], "tick_size": "0.001",
         "min_order_size": "5"},
        {"asset_id": "t1", "bids": [], "asks": []},
        {"asset_id": "t3", "bids": [{"price": "0.10", "size": "7"}],
         "asks": []},
        {"asset_id": "t4", "bids": [], "asks": []},
        {"asset_id": "t5", "bids": [], "asks": []}])
    chk("КНИГИ СОПОСТАВЛЯЮТСЯ ПО asset_id ИЗ ТЕЛА, А НЕ ПО ПОРЯДКУ ЗАПРОСА: "
        f"{книги_проб} -- на ящике из 200 запрошенных токенов книг пришло 10, "
        "и по порядку книга одного токена досталась бы другому",
        книги_проб == {"t2": True, "t1": False, "t3": False, "t4": False,
                       "t5": False}, книги_проб)
    канд_проб = po_objomu([
        {"conditionId": "0xпусто", "slug": "nikogda-ne-torgovalsja",
         "volume24hr": 999.0, "tokens": ["t4", "t5"]},
        {"conditionId": "0xживой", "slug": "zhivoj", "volume24hr": 50.0,
         "tokens": ["t1", "t2"]},
        {"conditionId": "0xодносторонний", "slug": "odna-storona",
         "volume24hr": 40.0, "tokens": ["t3"]}])
    выб_проб = vybrat_po_knigam(канд_проб, книги_проб, 100)
    chk("В ВЫБОРЕ НЕТ РЫНКА С ПУСТОЙ КНИГОЙ, даже если он самый объёмный: "
        f"выбрано {[к['slug'] for к in выб_проб['rynki']]}, отброшено по "
        f"пустой книге {выб_проб['pustyh']}. Рынок с объёмом 999 и пустой "
        "книгой -- это ровно то, что собиралось раньше: 200 токенов "
        "запрошено, 10 книг, все пустые",
        [к["conditionId"] for к in выб_проб["rynki"]] == ["0xживой"]
        and выб_проб["pustyh"] == ["0xпусто", "0xодносторонний"], выб_проб)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: рынок проходит по НЕПУСТОЙ СТОРОНЕ, и число "
        "ног с книгой запоминается -- проверке (а) нужны обе: "
        f"tokenov_s_knigoj = {выб_проб['rynki'][0]['tokenov_s_knigoj']} из "
        f"{len(выб_проб['rynki'][0]['tokens'])}; односторонний рынок (бид "
        "без аска) в выбор НЕ попал",
        выб_проб["rynki"][0]["tokenov_s_knigoj"] == 1
        and "0xодносторонний" in выб_проб["pustyh"], выб_проб["rynki"][0])
    пара = [{"conditionId": f"0x{и}", "slug": f"s{и}", "volume24hr": 100.0 - и,
             "tokens": [f"p{и}a", f"p{и}b"]} for и in range(3)]
    книги_пара = {т: True for к in пара for т in к["tokens"]}
    выб_пара = vybrat_po_knigam(пара, книги_пара, 2)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: ТОКЕНОВ НЕ БОЛЬШЕ rynkov*2, и рынок берётся "
        f"ЦЕЛИКОМ -- при заказе 2 рынков выбрано {len(выб_пара['rynki'])} и "
        f"{выб_пара['tokenov']} токенов. Иначе срез минутного круга разрезал "
        "бы рынок пополам: у одной ноги книга собиралась бы, у второй нет, и "
        "проверка (а) по такой паре не считается",
        len(выб_пара["rynki"]) == 2 and выб_пара["tokenov"] == 4
        and выб_пара["hvatilo"], выб_пара)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: не набрали заказанное -- hvatilo False, а не "
        "молчаливое «сколько есть»",
        vybrat_po_knigam(пара, книги_пара, 100)["hvatilo"] is False)
    дв = [{"conditionId": f"0x{и}", "tokens": [f"{и}a", f"{и}b"], "slug": "",
           "volume24hr": 1.0} for и in range(250)]
    части_дв = chasti_po_tokenam(дв, БАТЧ_ПРЕДЕЛ)
    три = [{"conditionId": f"0y{и}", "tokens": [f"{и}a", f"{и}b", f"{и}c"],
            "slug": "", "volume24hr": 1.0} for и in range(250)]
    части_три = chasti_po_tokenam(три, БАТЧ_ПРЕДЕЛ)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: пакеты книг делятся ПО ТОКЕНАМ, а не по числу "
        f"рынков -- 250 бинарных рынков дают {len(части_дв)} часть "
        f"({[sum(len(к['tokens']) for к in ч) for ч in части_дв]} токенов), а "
        f"250 ТРЁХИСХОДНЫХ -- {len(части_три)} "
        f"({[sum(len(к['tokens']) for к in ч) for ч in части_три]}). Делёж по "
        f"рынкам отправил бы 750 записей при их пределе {БАТЧ_ПРЕДЕЛ}, и "
        "обрезала бы ТА сторона -- молча, а недоспрошенные рынки выглядели "
        "бы пустыми",
        len(части_дв) == 1
        and all(sum(len(к["tokens"]) for к in ч) <= БАТЧ_ПРЕДЕЛ
                for ч in части_дв + части_три)
        and len(части_три) == 2
        and sum(len(ч) for ч in части_три) == 250,
        [sum(len(к["tokens"]) for к in ч) for ч in части_три])
    # СОСТАВ БОЛЬШЕ НЕ ПЕРЕЗАПИСЫВАЕТСЯ ПЯТИМИНУТНЫМ СНИМКОМ. Ровно это и
    # сломалось: выбор ехал от /events каждые пять минут.
    куски = {}
    for кусок in свой.split("\ndef "):
        куски[кусок.split("(", 1)[0]] = кусок
    chk("ВЫБОР ПИШЕТ ТОЛЬКО СУТОЧНЫЙ ОТБОР: ВЫБОР.write_text встречается в "
        f"рабочей части {тело_кода.count('ВЫБОР.write_text')} раз, и это "
        "внутри otbor_rynkov; в главное() его нет, и выбор_из_events оттуда "
        "не зовётся -- иначе состав снова ехал бы каждые пять минут",
        тело_кода.count("ВЫБОР.write_text") == 1
        and "ВЫБОР.write_text" in куски.get("otbor_rynkov", "")
        and "ВЫБОР.write_text" not in куски.get("главное", "")
        and "выбор_из_events" not in куски.get("главное", ""),
        тело_кода.count("ВЫБОР.write_text"))
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: пробные книги отбора идут в ОТДЕЛЬНЫЙ список "
        "clob_books_otbor, а не в clob_books -- из clob_books считаются "
        "производные ряды выбранного состава, и чужие книги дали бы там "
        f"лишние строки. Разбор у обоих общий: {разбор_коротко('clob_books_otbor', [{'bids': [1], 'asks': [1]}])}",
        "clob_books_otbor" in куски.get("otbor_rynkov", "")
        and разбор_коротко("clob_books_otbor",
                           [{"bids": [1], "asks": [1]}])["knig"] == 1
        and разбор_коротко("clob_books_otbor",
                           [{"bids": [], "asks": []}]
                           )["knig_s_dvumja_storonami"] == 0)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: ОТКАЗНАЯ строка тоже получает token_id "
        f"({разбор_s_tokenom({}, {'token_id': 'T7'})}) -- без этого из "
        "архива не видно, по какому токену ставка не вышла, и его молча "
        "сочли бы неспрошенным; а где токена в запросе нет, разбор не "
        "дополняется выдуманным полем",
        разбор_s_tokenom({}, {"token_id": "T7"}) == {"token_id": "T7"}
        and разбор_s_tokenom({"knig": 2}, {"tokenov": 2}) == {"knig": 2})

    # ----- 7. РОТАЦИЯ ПО ЧАСУ ИЗ МЕТКИ СНИМКА
    п1 = путь_снимка({"utc": "2026-10-15T23:59:59Z", "список": "clob_books"})
    п2 = путь_снимка({"utc": "2026-10-16T00:00:01Z", "список": "clob_books"})
    chk("РОТАЦИЯ ПО ЧАСУ ИЗ МЕТКИ СНИМКА, А НЕ ИЗ ЛОКАЛЬНЫХ ЧАСОВ: 23:59:59Z "
        f"-> {п1.parent.name}/{п1.name}, 00:00:01Z -> {п2.parent.name}/"
        f"{п2.name}. Иначе снимок конца суток лёг бы в чужой час",
        п1.name == "23.jsonl.gz" and п1.parent.name == "2026-10-15"
        and п2.name == "00.jsonl.gz" and п2.parent.name == "2026-10-16",
        (str(п1), str(п2)))

    # ----- 8. ОЦЕНКА ОБЪЁМА
    о100 = ocenka_objoma(100, 30)
    ожид_книга = КНИГА_ПОСТОЯННАЯ + КНИГА_УРОВЕНЬ * 60
    chk(f"ОЦЕНКА ОБЪЁМА СЧИТАЕТСЯ ПО СХЕМЕ, А НЕ НА ГЛАЗ: книга при 30 "
        f"уровнях на сторону = {о100['bajt_knigi']} Б (= {КНИГА_ПОСТОЯННАЯ} + "
        f"{КНИГА_УРОВЕНЬ}*60), 100 рынков дают {о100['gz_v_sutki'] / 1e6:.0f} "
        f"МБ gz в сутки при сжатии {СЖАТИЕ:.2f}x",
        о100["bajt_knigi"] == ожид_книга
        and о100["knig_v_krugu"] == 200
        and math.isclose(о100["gz_v_sutki"],
                         о100["syroe_v_sutki"] / СЖАТИЕ, rel_tol=1e-6), о100)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: при 100 рынках СУТОЧНЫЙ файл книг НЕ влезает в "
        f"предел GitHub 100 МБ ({о100['gz_v_sutki'] / 1e6:.0f} МБ), а ЧАСОВОЙ "
        f"влезает ({о100['gz_v_chas'] / 1e6:.2f} МБ) -- отсюда и ротация по "
        "часам, это расчёт, а не вкус",
        not о100["sutochnyj_vljazet_v_100mb"]
        and о100["gz_v_chas"] < 100e6, о100)
    chk("степень сжатия взята НИЖНЯЯ из трёх ЗАМЕРЕННЫХ на наших же файлах "
        f"того же формата {СЖАТИЕ_ИЗМЕРЕНО} -- чтобы оценка не оказалась "
        "приятнее правды",
        math.isclose(СЖАТИЕ, 3.52) and СЖАТИЕ == min(СЖАТИЕ_ИЗМЕРЕНО))

    # ----- 9. ОТКАЗ -- ТАКАЯ ЖЕ СТРОКА
    стр_отказ = снимок("clob_books", "POST", "/books", "tokens",
                       {"tokens": [], "markets": []}, 50)
    chk("ОТКАЗ ПИШЕТСЯ ТАКОЙ ЖЕ СТРОКОЙ, с кодом None и причиной -- снимок, "
        f"а не пропуск: код {стр_отказ['код']}, причина "
        f"{стр_отказ['ошибка']!r}, байт {стр_отказ['байт']}",
        стр_отказ["код"] is None and стр_отказ["ошибка"] == "нет выбора рынков"
        and стр_отказ["байт"] == 0 and "utc" in стр_отказ, стр_отказ)

    # ----- 10. ЗАГОЛОВКИ ЛИМИТОВ
    chk("ЗАГОЛОВКИ ЛИМИТОВ СОХРАНЯЮТСЯ -- настоящие лимиты померит ящик, а не "
        f"перескажет гайд: {list(ЗАГОЛОВКИ_ЛИМИТОВ)}",
        {"x-ratelimit-remaining", "retry-after", "cf-ray"}
        <= set(ЗАГОЛОВКИ_ЛИМИТОВ))

    # ----- 11. ПРОИЗВОДНЫЕ: ЛУЧШИЕ, СЕРЕДИНА, СПРЕД
    книга_проб = {"asset_id": "T1", "market": "0xm1", "tick_size": "0.01",
                  "min_order_size": "5", "last_trade_price": "0.47",
                  "bids": [{"price": "0.45", "size": "100"},
                           {"price": "0.44", "size": "200"},
                           {"price": "0.40", "size": "900"}],
                  "asks": [{"price": "0.47", "size": "150"},
                           {"price": "0.48", "size": "250"},
                           {"price": "0.55", "size": "800"}]}
    р = ряд_из_книги(книга_проб)
    chk(f"РЯД ИЗ КНИГИ: лучший бид {р['luchshij_bid']}, лучший аск "
        f"{р['luchshij_ask']}, середина {р['mid']}, спред {р['spred']:.2f} -- "
        "и уровни отсортированы, а не взяты первыми из массива",
        р["luchshij_bid"] == 0.45 and р["luchshij_ask"] == 0.47
        and abs(р["mid"] - 0.46) < 1e-12 and abs(р["spred"] - 0.02) < 1e-12, р)
    chk("ГЛУБИНА СЧИТАЕТСЯ ОТ СЕРЕДИНЫ: при середине 0.46 в +-1 цент входят "
        f"бид 0.45 (100) и аск 0.47 (150) -- {р['glubina_bid_1c']} и "
        f"{р['glubina_ask_1c']}; в +-5 центов добавляются 0.44 и 0.48 -- "
        f"{р['glubina_bid_5c']} и {р['glubina_ask_5c']} (0.40 и 0.55 дальше "
        "пяти центов и не входят)",
        р["glubina_bid_1c"] == 100 and р["glubina_ask_1c"] == 150
        and р["glubina_bid_5c"] == 300 and р["glubina_ask_5c"] == 400, р)
    широкая = {"asset_id": "T2",
               "bids": [{"price": "0.30", "size": "500"}],
               "asks": [{"price": "0.70", "size": "500"}]}
    рш = ряд_из_книги(широкая)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: при ШИРОКОМ спреде (0.30/0.70, середина 0.50) в "
        f"+-1 цент от середины НЕ входит ни одного уровня "
        f"({рш['glubina_bid_1c']} и {рш['glubina_ask_1c']}), а счёт от лучшей "
        "цены показал бы по 500 с каждой стороны. Награда считается по спреду "
        "ОТ СЕРЕДИНЫ, значит и глубина -- тоже",
        рш["glubina_bid_1c"] == 0 and рш["glubina_ask_1c"] == 0
        and рш["glubina_bid_5c"] == 0, рш)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ НА ДВОИЧНОЙ АРИФМЕТИКЕ: 0.46 - 0.45 в double даёт "
        f"{0.46 - 0.45!r}, то есть БОЛЬШЕ 0.01. Без допуска уровень у самой "
        "лучшей цены выпал бы из «+-1 цент» -- именно тот, что для награды "
        f"важнее всех. С допуском он на месте: {р['glubina_bid_1c']}",
        0.46 - 0.45 > 0.01 and р["glubina_bid_1c"] == 100
        and ДОПУСК_ЦЕНЫ < 0.01 / 1000, (0.46 - 0.45, р["glubina_bid_1c"]))
    пустая = ряд_из_книги({"asset_id": "T3", "bids": [], "asks": []})
    chk("ПУСТАЯ СТОРОНА -- None, А НЕ НОЛЬ: нет котировки и котировка по нулю "
        "-- разные вещи, и середина со спредом тогда тоже None",
        пустая["luchshij_bid"] is None and пустая["mid"] is None
        and пустая["spred"] is None and пустая["glubina_bid_1c"] is None,
        пустая)

    # ----- 12. СДЕЛКИ ЗА МИНУТУ
    chk("МЕТКА СДЕЛКИ ЧИТАЕТСЯ И В СЕКУНДАХ, И В МИЛЛИСЕКУНДАХ: "
        f"1791417600 -> {_минута(1791417600)}, 1791417600000 -> "
        f"{_минута(1791417600000)} -- одна и та же минута. Принять "
        "миллисекунды за секунды значило бы уехать в 56-тысячный год и "
        "развалить всю минутную разбивку",
        _минута(1791417600) == _минута(1791417600000)
        and _минута(1791417600).endswith(":00Z"), _минута(1791417600000))
    снимки_сделок = [
        {"utc": "2026-10-16T12:01:00Z", "тело": [
            {"id": "a", "asset_id": "T1", "price": "0.5", "size": "10",
             "timestamp": 1791417600},
            {"id": "b", "asset_id": "T1", "price": "0.5", "size": "20",
             "timestamp": 1791417601}]},
        # ТОТ ЖЕ снимок минутой позже: окна /trades ПЕРЕКРЫВАЮТСЯ
        {"utc": "2026-10-16T12:02:00Z", "тело": [
            {"id": "b", "asset_id": "T1", "price": "0.5", "size": "20",
             "timestamp": 1791417601},
            {"id": "c", "asset_id": "T1", "price": "0.5", "size": "30",
             "timestamp": 1791417660}]}]
    т = сделки_по_минутам(снимки_сделок)
    мин1 = _минута(1791417600)
    мин2 = _минута(1791417660)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: ОКНА /trades ПЕРЕКРЫВАЮТСЯ, и дубль снимается по "
        f"id -- сделка 'b' встретилась в двух снимках, но учтена ОДИН раз: "
        f"{мин1} -> {т[(мин1, 'T1')]}, {мин2} -> {т[(мин2, 'T1')]}. Без "
        "снятия дублей объём был бы завышен кратно числу снимков",
        т[(мин1, "T1")]["sdelok"] == 2
        and abs(т[(мин1, "T1")]["objom"] - 15.0) < 1e-9
        and т[(мин2, "T1")]["sdelok"] == 1, т)
    chk("МИНУТА БЕРЁТСЯ ИЗ МЕТКИ САМОЙ СДЕЛКИ, А НЕ ИЗ МЕТКИ СНИМКА: сделка "
        f"'c' пришла снимком 12:02, но легла в {мин2} по своей метке. Иначе "
        "весь ряд сдвинулся бы на минуту вперёд",
        (мин2, "T1") in т and мин2 != "2026-10-16T12:02:00Z", sorted(т))

    # ----- 13. ПАРАМЕТРЫ ВОЗНАГРАЖДЕНИЯ -- ОБЕИМИ ФОРМАМИ
    н_плоско = награда_рынка(
        {"rewardsMinSize": 50, "rewardsMaxSpread": 3.5,
         "clobRewards": [{"rewardsDailyRate": 10},
                         {"rewardsDailyRate": 5}]})
    chk("НАГРАДА ЧИТАЕТСЯ ПЛОСКОЙ ФОРМОЙ, и суточная ставка СКЛАДЫВАЕТСЯ из "
        f"записей clobRewards, когда наверху её нет: min_size "
        f"{н_плоско['nagrada_min_size']}, max_spread "
        f"{н_плоско['nagrada_max_spread']}, ставка "
        f"{н_плоско['nagrada_sutochnaja_stavka']}, форма "
        f"{н_плоско['nagrada_forma']!r}",
        н_плоско["nagrada_min_size"] == 50
        and н_плоско["nagrada_max_spread"] == 3.5
        and н_плоско["nagrada_sutochnaja_stavka"] == 15
        and н_плоско["nagrada_zapisej"] == 2
        and н_плоско["nagrada_forma"] == "плоско", н_плоско)
    н_вложено = награда_рынка(
        {"rewards": {"rewardsMinSize": 10, "rewardsMaxSpread": 2,
                     "rewardsDailyRate": 7}})
    chk("И ВЛОЖЕННОЙ ФОРМОЙ (внутри `rewards`) -- по открытым разборам Gamma "
        "отдаёт то так, то так, и форма ЗАПИСЫВАЕТСЯ в ряд "
        f"({н_вложено['nagrada_forma']!r}), чтобы на ящике стало видно правду",
        н_вложено["nagrada_min_size"] == 10
        and н_вложено["nagrada_sutochnaja_stavka"] == 7
        and н_вложено["nagrada_forma"] == "rewards", н_вложено)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: награды нет вовсе -- поля None и форма None, а "
        "НЕ нули: ноль суточной ставки и отсутствие программы -- разные вещи",
        награда_рынка({})["nagrada_min_size"] is None
        and награда_рынка({})["nagrada_sutochnaja_stavka"] is None
        and награда_рынка({})["nagrada_forma"] is None)

    # ----- 14. РОТАЦИЯ: БЕЗ ПОДТВЕРЖДЁННОГО АРТЕФАКТА НЕ УДАЛЯЕТ НИЧЕГО
    # global нарочно: проверка ротации работает в tempfile, а не в рабочей
    # папке, и возвращает обе на место в finally. (Директивы noqa тут не
    # стоит: при наборе правил без PLW0603 она сама стала бы замечанием.)
    global ПАПКА, ПАПКА_ОТМЕТОК
    было_п, было_о = ПАПКА, ПАПКА_ОТМЕТОК
    try:
        врем = Path(tempfile.mkdtemp(prefix="pmput-"))
        ПАПКА, ПАПКА_ОТМЕТОК = врем, врем / "artefakty"
        for д in ("2026-09-01", "2026-10-15"):
            (врем / "clob_books" / д).mkdir(parents=True)
            (врем / "clob_books" / д / "00.jsonl.gz").write_bytes(b"")
        р1 = rotacija(dnej=30, segodnja="2026-10-16", probno=True)
        chk(f"ПОРОГ РОТАЦИИ СЧИТАЕТСЯ В UTC: сегодня 2026-10-16 минус 30 "
            f"суток = {р1['porog']}. Через time.mktime он взял бы МЕСТНОЕ "
            "время и на ящике с другим поясом съехал бы на сутки",
            р1["porog"] == "2026-09-16", р1["porog"])
        chk("ДОКАЗАННЫЙ КРАСНЫЙ: сутки старше порога ЕСТЬ, но артефакт НЕ "
            f"подтверждён -- удалять нечего, и сутки названы вслух: удалить "
            f"{р1['udalit']}, ждут артефакта {р1['zhdut_artefakta']}",
            р1["udalit"] == []
            and р1["zhdut_artefakta"] == ["clob_books/2026-09-01"], р1)
        отметить_артефакт("2026-09-01", "c3-polymarket-syroe-2026-09-01")
        р2 = rotacija(dnej=30, segodnja="2026-10-16", probno=False)
        chk("ПОСЛЕ ОТМЕТКИ ОБ АРТЕФАКТЕ -- и только эти сутки: удалено "
            f"{р2['snjato']}, свежие сутки 2026-10-15 не тронуты "
            f"({(врем / 'clob_books' / '2026-10-15').is_dir()})",
            р2["snjato"] == 1 and р2["zhdut_artefakta"] == []
            and not (врем / "clob_books" / "2026-09-01").exists()
            and (врем / "clob_books" / "2026-10-15").is_dir(), р2)
        упало_о = None
        try:
            отметить_артефакт("2026-09-02", "")
        except ValueError as сбой:
            упало_о = str(сбой)
        chk("ДОКАЗАННЫЙ КРАСНЫЙ: отметка БЕЗ имени артефакта -- отказ по "
            "имени: пустая отметка открыла бы удаление, ничего не подтвердив",
            упало_о and "ничего не подтверждает" in упало_о, упало_о)
    finally:
        ПАПКА, ПАПКА_ОТМЕТОК = было_п, было_о

    # ----- 15. В ВЕТКУ -- ТОЛЬКО ПРОИЗВОДНЫЕ
    chk("В ВЕТКУ ПУШАТСЯ ТОЛЬКО ПРОИЗВОДНЫЕ И ОТМЕТКИ, сырого там нет: "
        f"{list(ПУТИ_В_ВЕТКУ)}. Иначе 340 МБ в сутки уехали бы в историю git",
        set(ПУТИ_В_ВЕТКУ) == {"data/polymarket/proizvodnye",
                              "data/polymarket/artefakty"}
        and not any(п.endswith("clob_books") for п in ПУТИ_В_ВЕТКУ))

    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    for и in упавшие:
        print(f"УПАЛИ: {и}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным")
        плохо += 1
    return плохо


def main() -> int:
    р = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    р.add_argument("--kazhdye", default="минута",
                   choices=("минута", "пять", "все"))
    р.add_argument("--spisok", default="", help="только этот список")
    р.add_argument("--rynkov", type=int, default=100,
                   help="сколько рынков вести (100 по слову владельца 16.10)")
    р.add_argument("--pauza", type=float, default=0.4,
                   help="пауза между запросами, секунд (бережём лимиты)")
    р.add_argument("--probno", action="store_true",
                   help="не писать файлы, только показать, кто ответил")
    р.add_argument("--svodka", action="store_true",
                   help="сколько снимков, места и уровней в книгах")
    р.add_argument("--ocenka", action="store_true",
                   help="оценка объёма в сутки при 50 и 100 рынках")
    р.add_argument("--push", action="store_true",
                   help="суточный пуш: в коммит ТОЛЬКО производные и отметки")
    р.add_argument("--vetka", default="claude/stroiteli",
                   help="ветка, в которую пушить (и только с неё)")
    р.add_argument("--proizvodnye", action="store_true",
                   help="минутный ряд из сырых книг: лучшие, середина, спред, "
                        "глубина, сделки за минуту, параметры награды")
    р.add_argument("--sutki", default="",
                   help="только эти сутки (YYYY-MM-DD); по умолчанию все")
    р.add_argument("--rotacija", action="store_true",
                   help="сырое старше --dnej удалить, НО только там, где "
                        "артефакт подтверждён отметкой")
    р.add_argument("--dnej", type=int, default=СУТОК_СЫРОГО,
                   help=f"сколько суток сырого держать (по умолчанию "
                        f"{СУТОК_СЫРОГО})")
    р.add_argument("--otmetit-artefakt", dest="otmetit_artefakt", default="",
                   help="сутки YYYY-MM-DD: отметить, что сырое уехало в "
                        "артефакт (ставит прогон Actions ПОСЛЕ загрузки)")
    р.add_argument("--artefakt", default="",
                   help="имя или id артефакта для отметки")
    р.add_argument("--otbor", action="store_true",
                   help="СУТОЧНЫЙ ОТБОР состава: --rynkov рынков по "
                        "volume24hr с ПРОВЕРЕННО непустой книгой. Пишет "
                        "data/polymarket/vybor.json и суточный список в "
                        "производные. Состав держится сутки")
    р.add_argument("--sbory", action="store_true",
                   help="ставка сбора по КАЖДОМУ токену выбора (/fee-rate, "
                        "по запросу на токен, с --pauza): суточный обход для "
                        "проверки (а). Пятиминутный список спрашивает только "
                        "первый токен -- для полного набора этого мало")
    р.add_argument("--self-test", dest="self_test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return 1 if self_test() else 0
    return главное(а)


if __name__ == "__main__":
    raise SystemExit(main())
