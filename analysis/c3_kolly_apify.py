#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""КОЛЛЫ KOL: посты X/Twitter пятидесяти KOL за 24.09--01.10 и адреса Solana из них.

ОДИН СКРИПТ, ТРИ ШАГА, И ТОЛЬКО ОДИН ИЗ НИХ ТРАТИТ ДЕНЬГИ:

    --spisok     офлайн, 0 $: пятьдесят handle из СЫРЫХ ответов GMGN;
    --vybor      сеть, 0 $:   выбор актора в Apify Store и чтение его схемы входа;
    --sbor       сеть, платно: посты этих пятидесяти и адреса из них.

ОТКУДА HANDLE. Из сырых ответов GMGN, которые уже лежат в репозитории
(data/gmgn/raw/*.json, категории kol и top_tracked, поле profile.twitterUsername).
Поле заполнено у всех 1149 строк, уникальных handle в этих двух категориях 112 --
значит пятьдесят по followCount берутся ОФЛАЙН, и ни лидерборд Kolscan, ни
консольный CSV владельца для этого не нужны. Оба запасных пути всё равно
написаны и включаются сами, если handle окажется меньше пятидесяти: сначала любой
CSV в data/gmgn/ со столбцом twitterUsername, затем актор лидерборда Kolscan в
Store (ищется по имени; не нашёлся -- ОТКАЗ ПО ИМЕНИ, а не тихий недобор).
Консольного CSV владельца на 25 строк в репозитории НЕТ -- это сказано, а не
обойдено молчанием.

АКТОР ВЫБИРАЕТСЯ КОДОМ, А НЕ ПАМЯТЬЮ. Store спрашивается по API, из ответа
берётся модель оплаты и цена за результат, считается цена за 1000 постов, и
выбирается самый дешёвый среди свежих (дата обновления -- вторым ключом). Берутся
ТОЛЬКО акторы с оплатой за результат (PRICE_PER_DATASET_ITEM): лишь с ними
счётчик расхода работает ДО запроса, а с подпиской или с оплатой за событие
потолок проверять нечем. Схема входа читается у выбранной сборки
(GET /v2/acts/{id} -> taggedBuilds -> GET /v2/actor-builds/{id} -> inputSchema), и
имена полей берутся ИЗ НЕЁ по списку кандидатов: нет поля аккаунтов, окна дат или
предела строк -- отказ по имени, а не запуск наугад за деньги.

ПОТОЛОК -- СЧЁТЧИКОМ ДО ЗАПРОСА, ПО ХУДШЕМУ СЛУЧАЮ (полный предел строк), как у
выгрузки GMGN: не дороже ПРЕДЕЛ_РАСХОДА долларов и не больше ПРЕДЕЛ_ЗАПРОСОВ
запросов. Сколько постов вообще влезает в бюджет, считается из прочитанной цены:
меньше МИНИМУМ_НА_АККАУНТ постов на аккаунт -- отказ, а не молчаливое урезание.

ТОКЕН -- ТОЛЬКО ИЗ ОКРУЖЕНИЯ APIFY_TOKEN, только в заголовке Authorization,
никогда в адресе и никогда в выводе: всё печатаемое и записываемое идёт через
zateret().

ОКНО ВРЕМЕНИ. Запрашивается с запасом (конец -- следующая дата), потому что у
многих акторов дата задаётся без времени; ТОЧНАЯ граница 2026-10-01T23:59:59Z
ставится своим фильтром по времени каждого поста, а не доверяется актору.

ЧТО СЧИТАЕТСЯ КОЛЛОМ. Любая подстрока base58 длиной 32--44 знака (в том числе с
окончанием pump), которая РАЗБИРАЕТСЯ как 32-байтовый ключ Solana
(solders.Pubkey.from_string). Длина 32--44 сама отсекает подписи транзакций (они
87--88 знаков), а разбор ключа -- слова, случайно похожие на base58.

НИЧЕГО НЕ АНАЛИЗИРУЕТСЯ И НЕ ОТБИРАЕТСЯ: ни первого коллера, ни веса, ни
совпадений с покупками полосы -- это Code-2. Здесь только сбор.

СВЯЗКА TWITTER -> КОШЕЛЁК: ОНА УЖЕ ЕСТЬ В СВОИХ ЖЕ ВЫГРУЗКАХ, Kolscan НЕ НУЖЕН.
Актор GMGN отдаёт профиль по API сам, без особого параметра, -- ВЛОЖЕННЫМ объектом
`profile` каждой строки набора (ключи nickname, name, avatarUrl, twitterUsername,
twitterName, twitterDescription, twitchChannelName; объект есть у всех 1149 сырых
строк, непустой twitterUsername -- у 622). Пустыми столбцы nickname и
twitterUsername вышли в CSV по НАШЕЙ вине: разбор смотрел только верхний уровень
строки. Исправлено в c3_gmgn_apify._достать, CSV пересобран из сырых ответов без
сети и без денег (--peresobrat): заполнено 159 строк, уникальных handle 153, и у
пяти из них кошельков больше одного (у одного -- три), что в карте видно, а не
склеено. У всех пятидесяти выбранных аккаунтов кошелёк известен.

ВЫХОД:
  data/kolly/kol_50.json                   -- пятьдесят handle (шаг --spisok);
  data/kolly/aktor_vybor.json              -- выбор актора и разбор схемы (--vybor);
  data/kolly/kolly_2026-09-24_10-01.json   -- строки {mint, t_utc, author_handle,
                                              post_url, author_wallet, text};
  data/kolly/syrye/<handle|пачка>.json     -- сырые строки ответов как есть;
  docs/kolly_sbor.md                       -- аккаунтов, постов, коллов, расход $.

Запуск:  python3 analysis/c3_kolly_apify.py --spisok
         python3 analysis/c3_kolly_apify.py --vybor
         python3 analysis/c3_kolly_apify.py --sbor
         python3 analysis/c3_kolly_apify.py --self-test
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
API = "https://api.apify.com/v2"
ИМЯ_ТОКЕНА = "APIFY_TOKEN"

# --- бюджет и потолки
ПРЕДЕЛ_РАСХОДА = 5.0
ПРЕДЕЛ_ЗАПРОСОВ = 60
МИНИМУМ_НА_АККАУНТ = 20
ПОТОЛОК_ЦЕНЫ_ЗА_1000 = 1.0          # дороже 1 $ за 1000 постов не берём
СРОК_ЗАПУСКА_С = 600.0

# --- окно задания
НАЧАЛО = "2026-09-24T00:00:00Z"
КОНЕЦ = "2026-10-01T23:59:59Z"
КОНЕЦ_ЗАПРОСА_ДАТА = "2026-10-02"   # с запасом: точную границу ставит свой фильтр

# --- сколько аккаунтов и откуда
НУЖНО_АККАУНТОВ = 50
КАТЕГОРИИ_GMGN = ("kol", "top_tracked")
ПАПКА_RAW_GMGN = КОРЕНЬ / "data" / "gmgn" / "raw"
ПАПКА_GMGN = КОРЕНЬ / "data" / "gmgn"
ПАПКА_ВЫХОДА = КОРЕНЬ / "data" / "kolly"
ФАЙЛ_СПИСКА = ПАПКА_ВЫХОДА / "kol_50.json"
ФАЙЛ_ВЫБОРА = ПАПКА_ВЫХОДА / "aktor_vybor.json"
ФАЙЛ_КОЛЛОВ = ПАПКА_ВЫХОДА / "kolly_2026-09-24_10-01.json"
ПАПКА_СЫРЫХ = ПАПКА_ВЫХОДА / "syrye"
СТРАНИЦА = КОРЕНЬ / "docs" / "kolly_sbor.md"

# --- поиск актора в Store
СЛОВА_ПОИСКА = ("tweet scraper", "twitter scraper", "x scraper")
СЛОВА_KOLSCAN = ("kolscan", "kol scan")
МОДЕЛЬ_ЗА_РЕЗУЛЬТАТ = "PRICE_PER_DATASET_ITEM"
# МОДЕЛЬ "ЗА СОБЫТИЕ" -- ТОЖЕ СЧИТАЕМАЯ ДО ЗАПРОСА, ЕСЛИ ЕСТЬ ЦЕНА СОБЫТИЯ
# РЕЗУЛЬТАТА. У Apify это pricingPerEvent.actorChargeEvents: {имя: {eventPriceUsd}}.
# Имя события у акторов своё, поэтому берётся САМОЕ ДОРОГОЕ из событий, похожих на
# "один результат" (их имена ниже), а если ни одно не похоже -- самое дорогое
# вообще: потолок обязан считаться по худшему случаю, а не по удобному.
МОДЕЛЬ_ЗА_СОБЫТИЕ = "PAY_PER_EVENT"
СОБЫТИЯ_РЕЗУЛЬТАТА = ("tweet", "post", "item", "result", "record", "row", "output")

# ИМЕНА ПОЛЕЙ СХЕМЫ -- КАНДИДАТАМИ, А НЕ ОДНИМ УГАДАННЫМ ИМЕНЕМ. У каждого актора
# они свои; скрипт берёт первое, которое ЕСТЬ В СХЕМЕ, и называет выбранное.
# Поля аккаунтов бывают ДВУХ видов: handle списком и ССЫЛКИ на профили. Вид
# определяется по имени поля (в нём есть "url") и по примеру из самой схемы --
# прогон 37009941214 нашёл у настоящего актора именно accountUrls, которого в
# первом списке кандидатов не было вовсе.
КАНДИДАТЫ_АККАУНТОВ = ("twitterHandles", "handles", "userNames", "usernames",
                        "authors", "profiles", "from",
                        "accountUrls", "profileUrls", "twitterUrls", "userUrls",
                        "urls", "startUrls", "searchTerms", "queries")
КАНДИДАТЫ_НАЧАЛА = ("start", "startDate", "since", "sinceDate", "fromDate",
                     "minDate", "tweetsSince")
КАНДИДАТЫ_КОНЦА = ("end", "endDate", "until", "untilDate", "toDate", "maxDate",
                    "tweetsUntil")
# ПРЕДЕЛ -- ТОЛЬКО ТО, ЧТО ОГРАНИЧИВАЕТ ЧИСЛО РЕЗУЛЬТАТОВ. "maxRequestsPerCrawl" и
# "maxCollections" сюда не входят: ими бюджет не ограничить, а подставить их значило
# бы считать потолок по чужой величине.
КАНДИДАТЫ_ПРЕДЕЛА = ("maxItems", "maxTweets", "maxPostsPerQuery", "maxResults",
                      "maxPosts", "tweetsDesired", "resultsLimit", "limit",
                      "maxTweetsPerQuery", "maxTweetsPerProfile")
# Поля разбора ответа -- тоже кандидатами: что именно взято, печатается и пишется.
КАНДИДАТЫ_HANDLE_ОТВЕТА = ("userName", "username", "handle", "screenName",
                            "screen_name", "authorUsername")
КАНДИДАТЫ_ВРЕМЕНИ = ("createdAt", "created_at", "timestamp", "date", "time",
                      "postedAt")
КАНДИДАТЫ_ТЕКСТА = ("text", "full_text", "fullText", "content", "rawContent",
                     "tweetText")
КАНДИДАТЫ_ССЫЛКИ = ("url", "twitterUrl", "tweetUrl", "permalink", "link",
                     "postUrl", "statusUrl")
КАНДИДАТЫ_ИД = ("id", "id_str", "tweetId", "conversationId", "restId")

# ГРАНИЦЫ ОБЯЗАТЕЛЬНЫ. Без них 88-значная ПОДПИСЬ транзакции даёт совпадение
# первыми 44 знаками, и подпись попала бы в коллы: проверено в самопроверке.
BASE58 = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}"
                     r"(?![1-9A-HJ-NP-Za-km-z])")


class ОшибкаСбора(Exception):
    pass


# ----------------------------------------------------------------- токен и печать

def токен() -> str:
    т = (os.environ.get(ИМЯ_ТОКЕНА) or "").strip()
    if not т:
        raise ОшибкаСбора(f"{ИМЯ_ТОКЕНА} не задан в окружении -- без него в Apify "
                           f"не ходим (в аргументах и в адресе токен не передаём)")
    return т


def zateret(текст) -> str:
    """Затереть токен в любой строке перед печатью или записью."""
    с = str(текст)
    т = (os.environ.get(ИМЯ_ТОКЕНА) or "").strip()
    if т and len(т) >= 8:
        с = с.replace(т, "<APIFY_TOKEN>")
    return с


def печать(*части) -> None:
    print(zateret(" ".join(str(ч) for ч in части)), flush=True)


# ------------------------------------------------------------------------ сеть

def _зов(метод: str, путь: str, тело=None, *, срок: float = 60.0) -> tuple:
    """(код, разобранное_тело). Токен -- только в заголовке Authorization."""
    данные = None if тело is None else json.dumps(тело).encode("utf-8")
    зап = urllib.request.Request(f"{API}{путь}", data=данные, method=метод)
    зап.add_header("Authorization", f"Bearer {токен()}")
    зап.add_header("Accept", "application/json")
    if данные is not None:
        зап.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(зап, timeout=срок) as отв:  # noqa: S310
            сырое = отв.read().decode("utf-8", "replace")
            код = отв.getcode()
    except urllib.error.HTTPError as сбой:
        сырое = сбой.read().decode("utf-8", "replace")
        код = сбой.code
    except (urllib.error.URLError, TimeoutError, OSError) as сбой:
        raise ОшибкаСбора(zateret(f"сеть не ответила: {type(сбой).__name__}: "
                                   f"{сбой}")) from None
    try:
        return код, json.loads(сырое) if сырое else None
    except ValueError:
        return код, сырое


# -------------------------------------------------- шаг 1: пятьдесят handle, офлайн

def handle_из_gmgn(папка: Path = ПАПКА_RAW_GMGN,
                    категории: tuple = КАТЕГОРИИ_GMGN) -> dict:
    """Handle из СЫРЫХ ответов GMGN: категории kol и top_tracked, followCount.

    Имя категории -- из имени файла до "__" (так их писала выгрузка GMGN).
    Один handle встречается в нескольких категориях -- берётся наибольший
    followCount и все категории, в которых он встретился.
    """
    из_ = {"ok": False, "why_not": None, "аккаунты": [], "файлов": 0,
            "строк": 0, "без_handle": 0}
    if not папка.is_dir():
        из_["why_not"] = f"папки сырых ответов GMGN нет: {папка}"
        return из_
    лучшие: dict = {}
    for файл in sorted(папка.glob("*.json")):
        кат = файл.name.split("__")[0]
        if кат not in категории:
            continue
        из_["файлов"] += 1
        try:
            строки = json.loads(файл.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for с in строки if isinstance(строки, list) else []:
            if not isinstance(с, dict):
                continue
            из_["строк"] += 1
            h = ((с.get("profile") or {}).get("twitterUsername") or "").strip()
            if not h:
                из_["без_handle"] += 1
                continue
            fc = с.get("followCount") or 0
            ключ = h.lower()
            было = лучшие.get(ключ)
            if было is None or int(fc) > было["followCount"]:
                лучшие[ключ] = {"handle": h, "followCount": int(fc),
                                 "категории": set(), "кошелёк": с.get("walletAddress")}
            лучшие[ключ]["категории"].add(кат)
    for зн in лучшие.values():
        зн["категории"] = sorted(зн["категории"])
    из_["аккаунты"] = sorted(лучшие.values(), key=lambda з: -з["followCount"])
    из_["ok"] = bool(из_["аккаунты"])
    if not из_["ok"]:
        из_["why_not"] = "ни одного непустого twitterUsername в этих категориях"
    return из_


def связка_handle_кошелёк(папка: Path = ПАПКА_RAW_GMGN) -> dict:
    """handle (в нижнем регистре) -> адрес кошелька. Из СЫРЫХ ответов GMGN.

    ОТКУДА ОНА ВООБЩЕ БЕРЁТСЯ. Актор GMGN отдаёт профиль по API -- отдельным
    вложенным объектом `profile` каждой строки набора (ключи nickname, name,
    avatarUrl, twitterUsername, twitterName, twitterDescription,
    twitchChannelName). Никакого особого параметра для этого не нужно: профиль
    приходит сам. Пустыми столбцы nickname/twitterUsername вышли в CSV потому, что
    наш разбор смотрел ТОЛЬКО верхний уровень строки; это исправлено в
    c3_gmgn_apify._достать, и CSV пересобран из сырых ответов без сети и без денег
    (python3 analysis/c3_gmgn_apify.py --peresobrat): заполнено 159 из 493.
    Поэтому Kolscan для связки не нужен -- она уже есть в своих же выгрузках.
    """
    из_ = {"ok": False, "why_not": None, "карта": {}, "строк": 0, "с_профилем": 0}
    if not папка.is_dir():
        из_["why_not"] = f"папки сырых ответов GMGN нет: {папка}"
        return из_
    for файл in sorted(папка.glob("*.json")):
        try:
            строки = json.loads(файл.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for с in строки if isinstance(строки, list) else []:
            if not isinstance(с, dict):
                continue
            из_["строк"] += 1
            h = ((с.get("profile") or {}).get("twitterUsername") or "").strip()
            к = (с.get("walletAddress") or "").strip()
            if not h or not к:
                continue
            из_["с_профилем"] += 1
            # ОДИН HANDLE -- НЕСКОЛЬКО КОШЕЛЬКОВ БЫВАЕТ. Берётся тот, у которого
            # followCount больше (он же выбран в список), а число разных кошельков
            # этого handle сохраняется: молча склеивать их нельзя.
            было = из_["карта"].get(h.lower())
            fc = int(с.get("followCount") or 0)
            if было is None:
                из_["карта"][h.lower()] = {"кошелёк": к, "followCount": fc,
                                            "кошельков": {к}}
            else:
                было["кошельков"].add(к)
                if fc > было["followCount"]:
                    было.update(кошелёк=к, followCount=fc)
    for зн in из_["карта"].values():
        зн["кошельков"] = sorted(зн["кошельков"])
    из_["ok"] = bool(из_["карта"])
    if not из_["ok"]:
        из_["why_not"] = "ни одной пары handle+кошелёк в сырых ответах"
    return из_


def handle_из_csv(папка: Path = ПАПКА_GMGN) -> dict:
    """Запасной путь: handle из любого CSV в data/gmgn/ со столбцом twitterUsername."""
    из_ = {"ok": False, "why_not": None, "аккаунты": [], "файлы": []}
    if not папка.is_dir():
        из_["why_not"] = f"папки {папка} нет"
        return из_
    лучшие: dict = {}
    for файл in sorted(папка.glob("*.csv")):
        try:
            строки = list(csv.DictReader(файл.open(encoding="utf-8")))
        except (OSError, csv.Error):
            continue
        if not строки or "twitterUsername" not in строки[0]:
            continue
        из_["файлы"].append(файл.name)
        for с in строки:
            h = (с.get("twitterUsername") or "").strip()
            if not h:
                continue
            try:
                fc = int(float(с.get("followCount") or 0))
            except ValueError:
                fc = 0
            ключ = h.lower()
            если = лучшие.get(ключ)
            if если is None or fc > если["followCount"]:
                лучшие[ключ] = {"handle": h, "followCount": fc,
                                 "категории": ["csv"], "кошелёк": с.get("walletAddress")}
    из_["аккаунты"] = sorted(лучшие.values(), key=lambda з: -з["followCount"])
    из_["ok"] = bool(из_["аккаунты"])
    if not из_["ok"]:
        из_["why_not"] = "в CSV data/gmgn/ непустых twitterUsername нет"
    return из_


def актор_kolscan() -> dict:
    """Актор лидерборда Kolscan в Store -- если он там есть. Денег не тратит."""
    из_ = {"ok": False, "why_not": None, "акторы": []}
    for слово in СЛОВА_KOLSCAN:
        н = store_поиск(слово)
        if not н["ok"]:
            из_["why_not"] = н["why_not"]
            return из_
        для = [а for а in н["акторы"]
               if "kolscan" in (а.get("имя") or "").lower().replace(" ", "")
               or "kolscan" in (а.get("название") or "").lower().replace(" ", "")]
        из_["акторы"] += для
    if not из_["акторы"]:
        из_["why_not"] = ("актора лидерборда Kolscan в Store не нашлось по словам "
                           f"{', '.join(СЛОВА_KOLSCAN)} -- добирать список нечем")
        return из_
    из_["ok"] = True
    return из_


def собрать_список(*, нужно: int = НУЖНО_АККАУНТОВ, с_сетью: bool = False) -> dict:
    """Пятьдесят handle по followCount. Сеть нужна только если их меньше нужного."""
    из_ = {"ok": False, "why_not": None, "аккаунты": [], "источники": {},
            "нужно": int(нужно)}
    g = handle_из_gmgn()
    из_["источники"]["gmgn_raw"] = {к: g[к] for к in
                                     ("ok", "why_not", "файлов", "строк", "без_handle")}
    из_["источники"]["gmgn_raw"]["handle"] = len(g["аккаунты"])
    аккаунты = list(g["аккаунты"])
    видели = {а["handle"].lower() for а in аккаунты}
    if len(аккаунты) < нужно:
        c = handle_из_csv()
        из_["источники"]["csv"] = {"ok": c["ok"], "why_not": c["why_not"],
                                    "файлы": c["файлы"], "handle": len(c["аккаунты"])}
        for а in c["аккаунты"]:
            if а["handle"].lower() not in видели:
                аккаунты.append(а)
                видели.add(а["handle"].lower())
    else:
        из_["источники"]["csv"] = {"ok": None,
                                    "why_not": "не понадобился: сырых GMGN хватило"}
    if len(аккаунты) < нужно:
        if с_сетью:
            к = актор_kolscan()
            из_["источники"]["kolscan"] = {"ok": к["ok"], "why_not": к["why_not"],
                                            "акторы": [а["имя"] for а in к["акторы"]]}
            # Самого прогона Kolscan здесь нет НАМЕРЕННО: он тратит деньги, а
            # понадобиться может только при недоборе. Нашёлся актор -- печатаем
            # его имя и останавливаемся, чтобы владелец решил, платить ли.
        else:
            из_["источники"]["kolscan"] = {
                "ok": None, "why_not": "не спрашивали: шаг --spisok идёт без сети"}
    аккаунты.sort(key=lambda а: -а["followCount"])
    из_["аккаунты"] = аккаунты[:нужно]
    if len(из_["аккаунты"]) < нужно:
        из_["why_not"] = (f"handle набралось {len(из_['аккаунты'])} из {нужно}: "
                           f"сырые GMGN дали {len(g['аккаунты'])}, CSV добавил "
                           f"{len(аккаунты) - len(g['аккаунты'])}")
        return из_
    из_["ok"] = True
    return из_


# -------------------------------------------- шаг 2: выбор актора и его схема

def store_поиск(слово: str, *, предел: int = 100) -> dict:
    """Акторы Store по слову: имя, цена за результат, модель оплаты, дата правки."""
    из_ = {"ok": False, "why_not": None, "акторы": []}
    путь = "/store?" + urllib.parse.urlencode({"search": слово, "limit": предел})
    код, тело = _зов("GET", путь)
    if код != 200 or not isinstance(тело, dict):
        из_["why_not"] = f"Store не прочитан: код {код}, ответ {str(тело)[:160]}"
        return из_
    строки = ((тело.get("data") or {}).get("items") or [])
    for с in строки if isinstance(строки, list) else []:
        if not isinstance(с, dict):
            continue
        цены = с.get("currentPricingInfo") or {}
        из_["акторы"].append({
            "id": с.get("id"),
            "имя": f"{(с.get('username') or '')}/{(с.get('name') or '')}",
            "название": с.get("title"),
            "модель": цены.get("pricingModel"),
            "цена_за_результат": цены.get("pricePerUnitUsd"),
            "цена_за_1000": (round(float(цены["pricePerUnitUsd"]) * 1000, 4)
                              if isinstance(цены.get("pricePerUnitUsd"), (int, float))
                              else None),
            "правлен": с.get("modifiedAt"),
            "запусков_за_месяц": (с.get("stats") or {}).get("totalRuns"),
            # СЫРАЯ цена -- как её отдаёт Store, без нашего истолкования. Нужна
            # ровно для того, чтобы не гадать о модели оплаты: прогон 37009723478
            # показал, что ВСЕ 124 найденных актора отдают модель, которую мы не
            # приняли, и без сырых полей причина осталась бы догадкой.
            "цена_сырая": цены,
        })
    из_["ok"] = True
    return из_


def _цена_одного_события(зн: dict) -> float | None:
    """Цена события по ХУДШЕМУ случаю: eventPriceUsd и все ярусы тарифа.

    ЦЕНА ЛЕЖИТ НЕ ТАМ, ГДЕ Я СНАЧАЛА ИСКАЛ. Прогон 37009941214 (0 $) показал
    фактом: у настоящих акторов X цена результата стоит в eventTieredPricingUsd
    (ярусы FREE/BRONZE/.../DIAMOND), а eventPriceUsd есть только у служебного
    события запуска -- и первый отбор ранжировал акторов по ПЛАТЕ ЗА ЗАПУСК, то
    есть ни по чему. Берётся МАКСИМУМ по всем ярусам и по eventPriceUsd: потолок
    обязан считаться по худшему случаю, а на каком ярусе счёт владельца -- отсюда
    не видно.
    """
    if not isinstance(зн, dict):
        return None
    цены = []
    if isinstance(зн.get("eventPriceUsd"), (int, float)):
        цены.append(float(зн["eventPriceUsd"]))
    ярусы = зн.get("eventTieredPricingUsd")
    if isinstance(ярусы, dict):
        for я in ярусы.values():
            ц = (я or {}).get("tieredEventPriceUsd") if isinstance(я, dict) else None
            if isinstance(ц, (int, float)):
                цены.append(float(ц))
    return max(цены) if цены else None


СОБЫТИЯ_ЗАПУСКА = ("actor-start", "start", "run-start", "initialization")


def цена_актора(цены: dict) -> dict:
    """Цена РЕЗУЛЬТАТА и цена ЗАПУСКА отдельно. Нет цены результата -- отказ.

    Плата за запуск -- разовая на запрос, и её нельзя путать с ценой строки: с
    этой путаницы первый отбор и выбрал актора по 0.00005 $ за запуск. В потолок
    она входит слагаемым, а не множителем.
    """
    из_ = {"ok": False, "why_not": None, "за_результат": None, "за_запуск": 0.0,
            "событие_результата": None, "события": {}}
    пс = (цены or {}).get("pricingPerEvent") or {}
    соб = пс.get("actorChargeEvents") or {}
    if not isinstance(соб, dict) or not соб:
        из_["why_not"] = "у модели за событие нет списка событий с ценой"
        return из_
    все = {}
    for имя, зн in соб.items():
        ц = _цена_одного_события(зн)
        if ц is None:
            continue
        загл = str((зн or {}).get("eventTitle") or "")
        все[имя] = {"цена": ц, "заголовок": загл,
                     "главное": bool((зн or {}).get("isPrimaryEvent")),
                     "запуск": any(сл in имя.lower() for сл in СОБЫТИЯ_ЗАПУСКА),
                     "результат": (bool((зн or {}).get("isPrimaryEvent"))
                                   or any(сл in (имя + " " + загл).lower()
                                          for сл in СОБЫТИЯ_РЕЗУЛЬТАТА))}
    из_["события"] = {и: з["цена"] for и, з in все.items()}
    if not все:
        из_["why_not"] = "ни у одного события нет цены ни в ярусах, ни в eventPriceUsd"
        return из_
    из_["за_запуск"] = round(sum(з["цена"] for з in все.values() if з["запуск"]), 8)
    результаты = {и: з["цена"] for и, з in все.items()
                  if з["результат"] and not з["запуск"] and з["цена"] > 0}
    if not результаты:
        из_["why_not"] = (f"среди событий нет цены за ОДИН РЕЗУЛЬТАТ (есть только "
                           f"{sorted(все)}): считать потолок строками нечем")
        return из_
    имя = max(результаты, key=lambda и: результаты[и])
    из_.update(ok=True, за_результат=результаты[имя], событие_результата=имя)
    return из_


def выбрать_актора(*, потолок_за_1000: float = ПОТОЛОК_ЦЕНЫ_ЗА_1000,
                    читать_схему=None, предел_проб: int = 8) -> dict:
    """Самый дешёвый за 1000 постов среди свежих, с оплатой ЗА РЕЗУЛЬТАТ.

    Оплата за результат -- условие, а не предпочтение: только зная цену одной
    строки, можно проверить потолок ДО запроса. Подписка и оплата за событие
    отсеиваются по имени модели, а не по догадке о цене.
    """
    из_ = {"ok": False, "why_not": None, "выбран": None, "кандидатов": 0,
            "отсеяно": {}, "список": [], "разбор": None, "build": None,
            "поля_схемы": None, "пропущены": []}
    все: dict = {}
    for слово in СЛОВА_ПОИСКА:
        н = store_поиск(слово)
        if not н["ok"]:
            из_["why_not"] = н["why_not"]
            return из_
        for а in н["акторы"]:
            все[а.get("id") or а["имя"]] = а
    отсев = {"модель не считается до запроса": 0, "без цены результата": 0,
              "дороже потолка": 0}
    модели = {}
    годные = []
    for а in все.values():
        модели[а["модель"]] = модели.get(а["модель"], 0) + 1
        if а["модель"] == МОДЕЛЬ_ЗА_РЕЗУЛЬТАТ:
            а["за_запуск"] = 0.0
        elif а["модель"] == МОДЕЛЬ_ЗА_СОБЫТИЕ:
            ца = цена_актора(а.get("цена_сырая") or {})
            а["событие"] = ца.get("событие_результата")
            а["события"] = ца.get("события")
            а["за_запуск"] = ца.get("за_запуск") or 0.0
            if not ца["ok"]:
                отсев["без цены результата"] += 1
                continue
            а["цена_за_результат"] = ца["за_результат"]
            а["цена_за_1000"] = round(ца["за_результат"] * 1000, 4)
        else:
            отсев["модель не считается до запроса"] += 1
            continue
        if not isinstance(а["цена_за_1000"], (int, float)):
            отсев["без цены результата"] += 1
            continue
        if а["цена_за_1000"] > потолок_за_1000:
            отсев["дороже потолка"] += 1
            continue
        годные.append(а)
    из_.update(кандидатов=len(все), отсеяно=отсев, модели=модели)
    # СЫРЫЕ ЦЕНЫ ПЕРВЫХ КАНДИДАТОВ -- В ОТВЕТ, чтобы причина отказа была видна
    # фактом, а не догадкой (и чтобы следующий выбор не стоил ещё одного прогона).
    из_["цены_как_их_отдаёт_store"] = [
        {"имя": а["имя"], "модель": а["модель"], "цена_сырая": а.get("цена_сырая")}
        for а in list(все.values())[:12]]
    # ЦЕНА ПЕРВЫМ КЛЮЧОМ, ДАТА ПРАВКИ ВТОРЫМ -- как сказано в задании. Дата у
    # Store бывает пустой (у всех найденных акторов modifiedAt пустой), и тогда
    # вторым ключом идёт число запусков за месяц -- это НАЗВАНО, а не подменено
    # молча.
    из_["дат_правки_нет_у"] = len([а for а in годные if not а["правлен"]])
    годные.sort(key=lambda а: (float(а["цена_за_1000"]), _обратно(а["правлен"]),
                                -int(а["запусков_за_месяц"] or 0)))
    из_["список"] = [{к: в for к, в in а.items() if к != "цена_сырая"}
                     for а in годные[:10]]
    if not годные:
        из_["why_not"] = (f"ни одного актора с ценой, считаемой ДО запроса, дешевле "
                           f"{потолок_за_1000} $ за 1000 постов: отсеяно {отсев}, "
                           f"модели {модели}")
        return из_
    # СХЕМА РЕШАЕТ НЕ МЕНЬШЕ ЦЕНЫ: актор без поля аккаунтов, окна дат или предела
    # строк не годится ни за какие деньги. Поэтому кандидаты перебираются ПО
    # ПОРЯДКУ ЦЕНЫ, и берётся первый, чья схема подходит; почему пропущен каждый
    # предыдущий -- в ответе. Чтение схемы денег не стоит.
    если = читать_схему or схема_актора
    из_["пропущены"] = []
    for а in годные[:предел_проб]:
        с = если(а["имя"])
        if not с.get("ok"):
            из_["пропущены"].append({"имя": а["имя"], "почему": с.get("why_not")})
            continue
        р = разобрать_схему(с.get("поля") or {})
        if not р.get("ok"):
            из_["пропущены"].append({"имя": а["имя"], "почему": р.get("why_not")})
            continue
        из_.update(ok=True, выбран=а, разбор=р, build=с.get("build"),
                   поля_схемы=sorted(с.get("поля") or {}))
        return из_
    из_["why_not"] = (f"из {min(len(годные), предел_проб)} самых дешёвых акторов ни у "
                       f"одного схема не даёт всех четырёх полей (аккаунты, начало, "
                       f"конец, предел строк); пропущены: "
                       f"{json.dumps(из_['пропущены'], ensure_ascii=False)[:600]}")
    return из_


def _обратно(дата) -> str:
    """Ключ сортировки «свежее -- раньше» для строки даты (пустая -- в конец."""
    с = str(дата or "")
    return "".join(chr(0x10FFFF - ord(з)) if ord(з) < 0x10FFFF else з for з in с) or "~"


def схема_актора(актор: str) -> dict:
    """Схема входа актора из его сборки. Схемы нет -- отказ по имени."""
    из_ = {"ok": False, "why_not": None, "поля": {}, "build": None, "сырая": None}
    код, тело = _зов("GET", f"/acts/{урл_актора(актор)}")
    if код != 200 or not isinstance(тело, dict):
        из_["why_not"] = f"актор не прочитан: код {код}, ответ {str(тело)[:160]}"
        return из_
    д = тело.get("data") or {}
    сборки = д.get("taggedBuilds") or {}
    айди = ((сборки.get("latest") or {}).get("buildId")
            or next((((v or {}).get("buildId")) for v in сборки.values()
                     if isinstance(v, dict) and v.get("buildId")), None))
    if not айди:
        из_["why_not"] = f"у актора нет помеченных сборок: {sorted(сборки)}"
        return из_
    из_["build"] = айди
    код2, тело2 = _зов("GET", f"/actor-builds/{айди}")
    if код2 != 200 or not isinstance(тело2, dict):
        из_["why_not"] = f"сборка не прочитана: код {код2}"
        return из_
    сырая = (тело2.get("data") or {}).get("inputSchema")
    if isinstance(сырая, str):
        try:
            сырая = json.loads(сырая)
        except ValueError:
            из_["why_not"] = "inputSchema сборки не разбирается как JSON"
            return из_
    if not isinstance(сырая, dict):
        из_["why_not"] = "в сборке нет inputSchema"
        return из_
    из_.update(ok=True, сырая=сырая, поля=сырая.get("properties") or {})
    return из_


def урл_актора(имя: str) -> str:
    """username/name -> username~name (так адресует акторов API)."""
    return str(имя).replace("/", "~")


def _первое_поле(поля: dict, кандидаты: tuple):
    for к in кандидаты:
        if к in поля:
            return к
    return None


def разобрать_схему(поля: dict) -> dict:
    """Какие имена полей схемы отвечают за аккаунты, окно дат и предел строк."""
    из_ = {"ok": False, "why_not": None, "аккаунты": None, "начало": None,
            "конец": None, "предел": None, "тип_аккаунтов": None,
            "дата_со_временем": False, "ссылками": False}
    из_["аккаунты"] = _первое_поле(поля, КАНДИДАТЫ_АККАУНТОВ)
    из_["начало"] = _первое_поле(поля, КАНДИДАТЫ_НАЧАЛА)
    из_["конец"] = _первое_поле(поля, КАНДИДАТЫ_КОНЦА)
    из_["предел"] = _первое_поле(поля, КАНДИДАТЫ_ПРЕДЕЛА)
    нет = [и for и, зн in (("аккаунтов", из_["аккаунты"]), ("начала окна", из_["начало"]),
                            ("конца окна", из_["конец"]), ("предела строк", из_["предел"]))
           if not зн]
    if нет:
        из_["why_not"] = (f"в схеме актора нет поля {', нет поля '.join(нет)}; "
                           f"схема принимает: {', '.join(sorted(поля))}")
        return из_
    п = поля[из_["аккаунты"]] or {}
    из_["тип_аккаунтов"] = п.get("type")
    образец_акк = " ".join(str((п or {}).get(к) or "") for к in
                           ("prefill", "example", "description", "placeholderValue"))
    из_["ссылками"] = ("url" in из_["аккаунты"].lower()
                        or "http" in образец_акк.lower())
    # СО ВРЕМЕНЕМ ИЛИ БЕЗ -- ПО ПРИМЕРУ ИЗ САМОЙ СХЕМЫ, а не по нашей догадке.
    образец = ""
    for ключ in ("prefill", "example", "default", "pattern", "description"):
        зн = (поля[из_["начало"]] or {}).get(ключ)
        if isinstance(зн, str) and зн:
            образец += " " + зн
    из_["дата_со_временем"] = ("T" in образец and ":" in образец)
    из_["ok"] = True
    return из_


def значение_даты(строка_iso: str, *, со_временем: bool) -> str:
    return строка_iso if со_временем else строка_iso[:10]


# --------------------------------------------------- шаг 3: сбор постов и коллов

def тело_запроса(разбор: dict, handle_пачка: list, *, предел: int) -> dict:
    """Вход актора: только поля, которые ЕСТЬ В СХЕМЕ, и ничего больше."""
    куски = [f"https://x.com/{h}" if разбор.get("ссылками") else h
             for h in handle_пачка]
    значение = (list(куски) if разбор["тип_аккаунтов"] == "array" else куски[0])
    return {
        разбор["аккаунты"]: значение,
        разбор["начало"]: значение_даты(НАЧАЛО, со_временем=разбор["дата_со_временем"]),
        разбор["конец"]: значение_даты(КОНЕЦ_ЗАПРОСА_ДАТА + "T00:00:00Z",
                                        со_временем=разбор["дата_со_временем"]),
        разбор["предел"]: int(предел),
    }


def запуск(актор: str, тело: dict, *, срок: float = СРОК_ЗАПУСКА_С) -> dict:
    """Один синхронный запуск актора: {ok, строки, why_not, мс}."""
    из_ = {"ok": False, "строки": [], "why_not": None, "мс": None}
    т0 = time.perf_counter()
    код, отв = _зов("POST", f"/acts/{урл_актора(актор)}/run-sync-get-dataset-items",
                     тело=тело, срок=срок)
    из_["мс"] = round((time.perf_counter() - т0) * 1000)
    if код not in (200, 201):
        из_["why_not"] = f"код {код}, ответ {str(отв)[:200]}"
        return из_
    if not isinstance(отв, list):
        из_["why_not"] = f"ответ не список строк: {str(отв)[:200]}"
        return из_
    из_.update(ok=True, строки=отв)
    return из_


def _глубоко(строка: dict, кандидаты: tuple):
    """Значение первого найденного ключа -- на верхнем уровне или в author/user."""
    for к in кандидаты:
        if isinstance(строка.get(к), (str, int, float)) and строка[к] != "":
            return строка[к], к
    for гнездо in ("author", "user", "account"):
        в = строка.get(гнездо)
        if isinstance(в, dict):
            for к in кандидаты:
                if isinstance(в.get(к), (str, int, float)) and в[к] != "":
                    return в[к], f"{гнездо}.{к}"
    return None, None


def время_utc(значение) -> str | None:
    """Время поста в UTC ISO. Два живых вида: ISO и «Wed Oct 01 12:00:00 +0000 2026».

    Ничего не додумывается: вид не распознан -- None, и строка попадает в счёт
    «время не разобрано», а не выбрасывается молча.
    """
    if isinstance(значение, (int, float)):
        сек = float(значение)
        if сек > 1e11:          # миллисекунды
            сек /= 1000.0
        return (datetime.fromtimestamp(сек, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"))
    с = str(значение or "").strip()
    if not с:
        return None
    try:
        д = datetime.fromisoformat(с.replace("Z", "+00:00"))
        if д.tzinfo is None:
            д = д.replace(tzinfo=timezone.utc)
        return д.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    try:
        д = datetime.strptime(с, "%a %b %d %H:%M:%S %z %Y")
        return д.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


АЛФАВИТ58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _байты_своим_разбором(строка: str) -> bytes | None:
    """base58 -> байты СВОИМ разбором, без зависимостей. Не base58 -- None.

    ЗАЧЕМ СВОЙ, ЕСЛИ ЕСТЬ solders. На бегунке GitHub solders не установлен (прогон
    37009388355 отказал на самопроверке именно этим -- и не потратил ни цента,
    ровно для чего гейт и стоит), а ставить зависимость ради проверки длины ключа
    незачем. Поэтому разбор свой, а самопроверка СВЕРЯЕТ его с solders там, где тот
    есть: оба обязаны дать один и тот же ответ на одних и тех же строках.
    """
    н = 0
    for з in строка:
        место = АЛФАВИТ58.find(з)
        if место < 0:
            return None
        н = н * 58 + место
    тело = н.to_bytes((н.bit_length() + 7) // 8, "big") if н else b""
    нулей = len(строка) - len(строка.lstrip(АЛФАВИТ58[0]))
    return b"\x00" * нулей + тело


def _ключ_32_байта(кусок: str) -> bool:
    """Это 32-байтовый ключ Solana? solders, если он есть, иначе свой разбор."""
    try:
        from solders.pubkey import Pubkey  # noqa: PLC0415
    except ImportError:
        б = _байты_своим_разбором(кусок)
        return bool(б) and len(б) == 32
    try:
        return len(bytes(Pubkey.from_string(кусок))) == 32
    except Exception:  # noqa: BLE001
        return False


def адреса_из_текста(текст: str) -> list:
    """Адреса Solana из текста: base58 32--44 знака И разбор как 32-байтовый ключ."""
    из_, видели = [], set()
    for кусок in BASE58.findall(текст or ""):
        if кусок in видели:
            continue
        видели.add(кусок)
        if _ключ_32_байта(кусок):
            из_.append(кусок)
    return из_


def в_окне(время: str) -> bool:
    """Точная граница окна -- НАША, а не актора: 24.09 00:00:00Z .. 01.10 23:59:59Z."""
    return bool(время) and НАЧАЛО <= время <= КОНЕЦ


def ссылка_поста(с: dict, handle: str) -> tuple:
    """Ссылка на пост: из ответа актора, иначе собранная из handle и id. ("", None) -- нет."""
    з, ключ = _глубоко(с, КАНДИДАТЫ_ССЫЛКИ)
    if isinstance(з, str) and з.startswith("http"):
        return з, ключ
    ид, ключ_ид = _глубоко(с, КАНДИДАТЫ_ИД)
    if ид not in (None, "") and handle:
        return f"https://x.com/{handle}/status/{ид}", f"собрана из {ключ_ид}"
    return "", None


def разобрать_посты(строки: list, *, handle_запроса: str | None = None,
                     связка: dict | None = None) -> dict:
    """Посты -> коллы в формате Code-2. Ничего не оценивается: разбор и окно.

    Строка выхода -- ровно {mint, t_utc, author_handle, post_url, author_wallet,
    text}. author_wallet пустой, если связки twitter -> кошелёк для этого handle
    нет: пустое поле лучше чужого кошелька.
    """
    связка = связка or {}
    из_ = {"посты": 0, "в_окне": 0, "коллов": 0, "строки": [],
            "без_времени": 0, "без_текста": 0, "вне_окна": 0, "без_ссылки": 0,
            "без_кошелька": 0, "ключи": {}}
    for с in строки:
        if not isinstance(с, dict):
            continue
        из_["посты"] += 1
        т, ключ_т = _глубоко(с, КАНДИДАТЫ_ТЕКСТА)
        в, ключ_в = _глубоко(с, КАНДИДАТЫ_ВРЕМЕНИ)
        h, ключ_h = _глубоко(с, КАНДИДАТЫ_HANDLE_ОТВЕТА)
        for имя, ключ in (("текст", ключ_т), ("время", ключ_в), ("handle", ключ_h)):
            if ключ:
                из_["ключи"].setdefault(имя, ключ)
        if т is None:
            из_["без_текста"] += 1
            continue
        когда = время_utc(в)
        if не_время(когда):
            из_["без_времени"] += 1
            continue
        if not в_окне(когда):
            из_["вне_окна"] += 1
            continue
        из_["в_окне"] += 1
        автор = str(h or handle_запроса or "")
        ссылка, ключ_с = ссылка_поста(с, автор)
        if ключ_с:
            из_["ключи"].setdefault("ссылка", ключ_с)
        if not ссылка:
            из_["без_ссылки"] += 1
        кош = (связка.get(автор.lower()) or {}).get("кошелёк") or ""
        if not кош:
            из_["без_кошелька"] += 1
        for адрес in адреса_из_текста(str(т)):
            из_["коллов"] += 1
            из_["строки"].append({"mint": адрес, "t_utc": когда,
                                   "author_handle": автор, "post_url": ссылка,
                                   "author_wallet": кош, "text": str(т)})
    return из_


def не_время(когда) -> bool:
    return not когда


# ---------------------------------------------------------------------- проход

def пачки(аккаунты: list, *, по: int) -> list:
    return [аккаунты[и:и + по] for и in range(0, len(аккаунты), max(1, по))]


def сбор(*, аккаунты: list, актор: str, разбор: dict, цена: float,
          за_запуск: float = 0.0,
          предел_расхода: float = ПРЕДЕЛ_РАСХОДА,
          предел_запросов: int = ПРЕДЕЛ_ЗАПРОСОВ, зовун=None,
          папка_сырых: Path = ПАПКА_СЫРЫХ) -> dict:
    """Запросы со счётчиком расхода ДО каждого, разбор, сырые ответы на диск."""
    зовун = зовун or (lambda т: запуск(актор, т))
    из_ = {"ok": False, "why_not": None, "запросы": [], "строки": [],
            "расход": 0.0, "постов": 0, "в_окне": 0, "коллов": 0,
            "аккаунтов": len(аккаунты), "ключи": {}, "на_аккаунт": None,
            "предел_постов": None, "связка": {}}
    св = связка_handle_кошелёк()
    карта = св.get("карта") or {}
    из_["связка"] = {"ok": св["ok"], "why_not": св.get("why_not"),
                      "handle_с_кошельком": len(карта),
                      "из_наших": sum(1 for а in аккаунты
                                       if а["handle"].lower() in карта)}
    if цена <= 0:
        из_["why_not"] = f"цена строки не положительна: {цена}"
        return из_
    # ПЛАТА ЗА ЗАПУСК -- СЛАГАЕМОЕ, А НЕ МНОЖИТЕЛЬ. Она берётся с каждого запроса,
    # поэтому вычитается из бюджета по худшему числу запросов (по одному на аккаунт).
    запас_на_запуски = за_запуск * max(1, len(аккаунты))
    всего_постов = int(max(0.0, предел_расхода - запас_на_запуски) / цена)
    на_аккаунт = всего_постов // max(1, len(аккаунты))
    из_.update(предел_постов=всего_постов, на_аккаунт=на_аккаунт,
               за_запуск=за_запуск, запас_на_запуски=round(запас_на_запуски, 6))
    if на_аккаунт < МИНИМУМ_НА_АККАУНТ:
        из_["why_not"] = (f"бюджета {предел_расхода} $ при цене {цена} $ за пост "
                           f"хватает на {всего_постов} постов -- это {на_аккаунт} на "
                           f"аккаунт, меньше {МИНИМУМ_НА_АККАУНТ}: урезать окно молча "
                           f"нельзя, нужен другой актор или другой бюджет")
        return из_
    пачка_по = len(аккаунты) if разбор["тип_аккаунтов"] == "array" else 1
    группы = пачки(аккаунты, по=пачка_по)
    предел_строк = (всего_постов if пачка_по > 1 else на_аккаунт)
    папка_сырых.mkdir(parents=True, exist_ok=True)
    расход, сделано = 0.0, 0
    for группа in группы:
        # ПОТОЛОК ПРОВЕРЯЕТСЯ ДО ЗАПРОСА И ПО ХУДШЕМУ СЛУЧАЮ (полный предел строк):
        # после ответа деньги уже потрачены, и проверять там -- проверять веру.
        худший = расход + за_запуск + предел_строк * цена
        if сделано >= предел_запросов:
            печать(f"СТОП по числу запросов: {сделано} из {предел_запросов}")
            break
        if худший > предел_расхода:
            печать(f"СТОП по расходу: следующий запрос дал бы {худший:.4f} $ при "
                    f"потолке {предел_расхода} $")
            break
        имя = "pachka" if пачка_по > 1 else группа[0]["handle"]
        з = зовун(тело_запроса(разбор, [а["handle"] for а in группа],
                                предел=предел_строк))
        сделано += 1
        строки = [с for с in (з.get("строки") or []) if isinstance(с, dict)]
        расход = round(расход + за_запуск + len(строки) * цена, 6)
        (папка_сырых / f"{_безопасно(имя)}.json").write_text(
            json.dumps(строки, ensure_ascii=False, indent=1), encoding="utf-8")
        р = разобрать_посты(строки, связка=карта,
                            handle_запроса=(None if пачка_по > 1
                                            else группа[0]["handle"]))
        из_["строки"] += р["строки"]
        из_["постов"] += р["посты"]
        из_["в_окне"] += р["в_окне"]
        из_["коллов"] += р["коллов"]
        из_["ключи"].update({к: в for к, в in р["ключи"].items()
                             if к not in из_["ключи"]})
        из_["запросы"].append({"пачка": имя, "аккаунтов": len(группа),
                                "постов": р["посты"], "в_окне": р["в_окне"],
                                "коллов": р["коллов"], "расход": расход,
                                "без_времени": р["без_времени"],
                                "без_текста": р["без_текста"],
                                "вне_окна": р["вне_окна"],
                                "без_ссылки": р["без_ссылки"],
                                "без_кошелька": р["без_кошелька"],
                                "мс": з.get("мс"), "why_not": з.get("why_not")})
        печать(f"[{сделано}/{len(группы)}] {имя}: постов {р['посты']}, в окне "
                f"{р['в_окне']}, коллов {р['коллов']}, расход всего {расход} $"
                + (f", отказ: {з.get('why_not')}" if not з.get("ok") else ""))
    из_.update(ok=True, расход=round(расход, 6))
    return из_


def _безопасно(имя: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(имя))[:64]


# ---------------------------------------------------------------- запись выхода

def записать_коллы(итог: dict, путь: Path = ФАЙЛ_КОЛЛОВ) -> Path:
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text(json.dumps(итог["строки"], ensure_ascii=False, indent=1),
                     encoding="utf-8")
    return путь


def записать_страницу(итог: dict, выбор: dict, список: dict, *,
                       путь: Path = СТРАНИЦА) -> Path:
    путь.parent.mkdir(parents=True, exist_ok=True)
    а = выбор.get("выбран") or {}
    адресов = len({с["mint"] for с in итог["строки"]})
    с_кошельком = len([с for с in итог["строки"] if с["author_wallet"]])
    со_ссылкой = len([с for с in итог["строки"] if с["post_url"]])
    стр = [f"# Коллы KOL: сбор {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())}",
            "",
            f"Окно: **{НАЧАЛО} -- {КОНЕЦ}** (точную границу ставит свой фильтр по "
            f"времени каждого поста; у актора запрашивалось до {КОНЕЦ_ЗАПРОСА_ДАТА} "
            f"с запасом).", "",
            "## Числа", "",
            "| что | сколько |", "|---|---|",
            f"| аккаунтов в списке | {итог['аккаунтов']} |",
            f"| запросов к актору | {len(итог['запросы'])} |",
            f"| постов получено | {итог['постов']} |",
            f"| постов в окне | {итог['в_окне']} |",
            f"| коллов (пост x адрес) | {итог['коллов']} |",
            f"| разных mint | {адресов} |",
            f"| строк с author_wallet | {с_кошельком} из {итог['коллов']} |",
            f"| строк с post_url | {со_ссылкой} из {итог['коллов']} |",
            f"| расход | **{итог['расход']} $** из потолка {ПРЕДЕЛ_РАСХОДА} $ |",
            "", "## Актор", "",
            f"* выбран: `{а.get('имя')}` ({а.get('название')})",
            f"* цена: {а.get('цена_за_результат')} $ за строку, "
            f"{а.get('цена_за_1000')} $ за 1000 постов, модель {а.get('модель')}",
            f"* правлен: {а.get('правлен')}",
            f"* кандидатов в Store просмотрено: {выбор.get('кандидатов')}, "
            f"отсеяно {выбор.get('отсеяно')}",
            f"* поля входа ИЗ СХЕМЫ: аккаунты `{(выбор.get('разбор') or {}).get('аккаунты')}`, "
            f"начало `{(выбор.get('разбор') or {}).get('начало')}`, "
            f"конец `{(выбор.get('разбор') or {}).get('конец')}`, "
            f"предел `{(выбор.get('разбор') or {}).get('предел')}`",
            f"* поля ответа, которые нашлись: {итог.get('ключи')}",
            f"* бюджет: {итог.get('предел_постов')} постов всего, "
            f"{итог.get('на_аккаунт')} на аккаунт",
            "", "## Откуда список аккаунтов", ""]
    for имя, зн in (список.get("источники") or {}).items():
        стр.append(f"* `{имя}`: {json.dumps(зн, ensure_ascii=False)}")
    стр += ["", "## По запросам", "",
            "| запрос | аккаунтов | постов | в окне | коллов | расход $ | отказ |",
            "|---|---|---|---|---|---|---|"]
    for з in итог["запросы"]:
        стр.append(f"| `{з['пачка']}` | {з['аккаунтов']} | {з['постов']} | "
                    f"{з['в_окне']} | {з['коллов']} | {з['расход']} | "
                    f"{з.get('why_not') or '--'} |")
    стр += ["", "Выброшено при разборе (суммарно): "
                f"без времени {sum(з['без_времени'] for з in итог['запросы'])}, "
                f"без текста {sum(з['без_текста'] for з in итог['запросы'])}, "
                f"вне окна {sum(з['вне_окна'] for з in итог['запросы'])}.",
            "",
            f"Строки: `{ФАЙЛ_КОЛЛОВ.relative_to(КОРЕНЬ)}` -- "
            "{mint, t_utc, author_handle, post_url, author_wallet, text}; `t_utc` "
            "всегда с секундами, `author_wallet` пустой, если связки для этого "
            "handle нет (пустое поле лучше чужого кошелька). Сырые ответы как есть: "
            f"`{ПАПКА_СЫРЫХ.relative_to(КОРЕНЬ)}/`.", "",
            "Адрес считается коллом, если это base58 32--44 знака И он разбирается "
            "как 32-байтовый ключ Solana. Ничего не отбиралось и не оценивалось: "
            "первый коллер, вес и совпадения с покупками полосы -- у Code-2.", ""]
    путь.write_text("\n".join(стр), encoding="utf-8")
    return путь


# ------------------------------------------------------------------ самопроверка

ЖДЁМ_ПРОВЕРОК = 65


def шаг_выбор_без_токена() -> bool:
    """Сетевой шаг без токена обязан вернуть 2 и назвать причину, а не упасть."""
    import io  # noqa: PLC0415
    import contextlib  # noqa: PLC0415

    буфер = io.StringIO()
    with contextlib.redirect_stdout(буфер):
        код = main_с(["--vybor"])
    текст = буфер.getvalue()
    return код == 2 and ИМЯ_ТОКЕНА in текст and "ОТКАЗ" in текст


def self_test() -> int:  # noqa: C901, PLR0912, PLR0915
    проверки = []

    def chk(что, ок, факт=None):
        проверки.append((что, bool(ок), факт))

    # ----------------------------------------- 1. список аккаунтов, офлайн
    g = handle_из_gmgn()
    chk("сырые ответы GMGN прочитаны и handle в них есть", g["ok"], g["why_not"])
    chk("файлов категорий kol и top_tracked ровно 7", g["файлов"] == 7, g["файлов"])
    chk("строк в них 700", g["строк"] == 700, g["строк"])
    # ЗАМЕР, А НЕ ОЖИДАНИЕ: профиль есть у ВСЕХ строк (объект `profile`), но сам
    # twitterUsername внутри него пустой у части кошельков -- у 223 из 700 в этих
    # двух категориях. Пятидесяти это не мешает: непустых handle 112.
    chk("пустых twitterUsername 223 из 700 -- и это не мешает набрать пятьдесят",
        g["без_handle"] == 223, g["без_handle"])
    chk("уникальных handle 112 -- больше пятидесяти",
        len(g["аккаунты"]) == 112, len(g["аккаунты"]))
    chk("список отсортирован по followCount по убыванию",
        all(g["аккаунты"][и]["followCount"] >= g["аккаунты"][и + 1]["followCount"]
            for и in range(len(g["аккаунты"]) - 1)), None)
    сп = собрать_список()
    chk("пятьдесят аккаунтов собрались без сети", сп["ok"] and len(сп["аккаунты"]) == 50,
        (сп.get("why_not"), len(сп["аккаунты"])))
    chk("CSV и Kolscan НЕ понадобились, и это сказано",
        сп["источники"]["csv"]["ok"] is None
        and "не понадобился" in сп["источники"]["csv"]["why_not"],
        сп["источники"]["csv"])
    chk("у каждого из пятидесяти есть handle и followCount",
        all(а["handle"] and isinstance(а["followCount"], int) for а in сп["аккаунты"]),
        None)
    chk("handle не повторяются",
        len({а["handle"].lower() for а in сп["аккаунты"]}) == 50, None)
    chk("недобор отказывает по имени, а не возвращает меньше молча",
        (lambda о: not о["ok"] and "набралось" in (о["why_not"] or ""))(
            собрать_список(нужно=10_000)), None)
    c = handle_из_csv()
    chk("запасной путь через CSV работает и находит столбец twitterUsername "
        "(CSV пересобран из сырых -- до правки разбора он был пустой)",
        c["ok"] and c["файлы"] and len(c["аккаунты"]) == 153,
        (c.get("why_not"), c.get("файлы"), len(c["аккаунты"])))

    # ------------------------- 1a. связка twitter -> кошелёк (поле profile)
    св = связка_handle_кошелёк()
    chk("связка handle -> кошелёк взялась из поля profile сырых ответов",
        св["ok"], св["why_not"])
    # ЗАМЕР: строк с непустым профилем 622 из 1149 по всем категориям, уникальных
    # handle 153, и у ПЯТИ из них кошельков больше одного (у одного -- три).
    # Поэтому "один handle = один кошелёк" здесь не утверждается: в карте лежит
    # кошелёк с наибольшим followCount и список ВСЕХ его кошельков.
    chk("уникальных handle в связке 153 при 622 строках с профилем",
        len(св["карта"]) == 153 and св["с_профилем"] == 622,
        (len(св["карта"]), св["с_профилем"]))
    chk("у пяти handle кошельков больше одного -- и это видно в карте, а не склеено",
        len([1 for з in св["карта"].values() if len(з["кошельков"]) > 1]) == 5,
        [h for h, з in св["карта"].items() if len(з["кошельков"]) > 1])
    chk("у каждого handle связки есть кошелёк и список всех его кошельков",
        all(з["кошелёк"] and з["кошельков"] for з in св["карта"].values()), None)
    из_50 = [а for а in сп["аккаунты"] if а["handle"].lower() in св["карта"]]
    chk("у всех пятидесяти выбранных кошелёк известен", len(из_50) == 50,
        len(из_50))

    # --------------------------------------------- 2. разбор схемы актора
    схема_ок = {"twitterHandles": {"type": "array"},
                 "start": {"type": "string", "prefill": "2026-01-01"},
                 "end": {"type": "string"}, "maxItems": {"type": "integer"}}
    р = разобрать_схему(схема_ок)
    chk("поля схемы найдены по кандидатам",
        р["ok"] and (р["аккаунты"], р["начало"], р["конец"], р["предел"])
        == ("twitterHandles", "start", "end", "maxItems"), р)
    chk("тип поля аккаунтов взят из схемы", р["тип_аккаунтов"] == "array",
        р["тип_аккаунтов"])
    chk("дата без времени -- по примеру из схемы", р["дата_со_временем"] is False,
        р["дата_со_временем"])
    р2 = разобрать_схему({"handles": {"type": "string"},
                           "since": {"type": "string",
                                      "prefill": "2026-01-01T00:00:00Z"},
                           "until": {"type": "string"},
                           "maxTweets": {"type": "integer"}})
    chk("другие имена полей тоже находятся",
        р2["ok"] and (р2["аккаунты"], р2["начало"], р2["конец"], р2["предел"])
        == ("handles", "since", "until", "maxTweets"), р2)
    chk("дата со временем -- тоже по примеру из схемы", р2["дата_со_временем"] is True,
        р2["дата_со_временем"])
    р3 = разобрать_схему({"twitterHandles": {"type": "array"},
                           "maxItems": {"type": "integer"}})
    chk("нет окна дат -- отказ по имени со списком полей схемы",
        not р3["ok"] and "нет поля начала окна" in (р3["why_not"] or ""), р3["why_not"])
    т = тело_запроса(р, ["a", "b"], предел=7)
    chk("в теле запроса только поля схемы и ничего больше",
        set(т) == {"twitterHandles", "start", "end", "maxItems"}, sorted(т))
    chk("даты без времени обрезаны до YYYY-MM-DD",
        т["start"] == "2026-09-24" and т["end"] == КОНЕЦ_ЗАПРОСА_ДАТА,
        (т["start"], т["end"]))
    chk("массив аккаунтов идёт списком, а не строкой", т["twitterHandles"] == ["a", "b"],
        т["twitterHandles"])
    т2 = тело_запроса(р2, ["a", "b"], предел=7)
    chk("строковое поле аккаунтов получает ОДИН handle, а не список",
        т2["handles"] == "a", т2["handles"])
    chk("даты со временем идут полностью",
        т2["since"] == НАЧАЛО and т2["until"].endswith("T00:00:00Z"),
        (т2["since"], т2["until"]))

    # ------------------- 2a. цена у модели «за событие» -- КАК ЕЁ ОТДАЁТ STORE.
    # Образец ниже -- не выдумка: это раскладка живого актора apidojo/tweet-scraper
    # из прогона 37009941214 (цена результата в ЯРУСАХ, а eventPriceUsd только у
    # служебного события запуска).
    живой = {"pricingPerEvent": {"actorChargeEvents": {
        "apify-actor-start": {"eventTitle": "Actor start",
                               "eventPriceUsd": 0.00005},
        "apify-default-dataset-item": {
            "eventTitle": "tweet", "isPrimaryEvent": True,
            "eventTieredPricingUsd": {
                "FREE": {"tieredEventPriceUsd": 0.0004},
                "GOLD": {"tieredEventPriceUsd": 0.0003}}}}}}
    ца = цена_актора(живой)
    chk("цена РЕЗУЛЬТАТА берётся из ярусов, по худшему (самому дорогому) ярусу",
        ца["ok"] and ца["за_результат"] == 0.0004
        and ца["событие_результата"] == "apify-default-dataset-item", ца)
    chk("плата за ЗАПУСК не путается с ценой строки и выделена отдельно",
        ца["за_запуск"] == 0.00005, ца["за_запуск"])
    ца2 = цена_актора({"pricingPerEvent": {"actorChargeEvents": {
        "apify-actor-start": {"eventPriceUsd": 0.00005}}}})
    chk("есть только плата за запуск -- ОТКАЗ: потолок строками считать нечем",
        not ца2["ok"] and "ОДИН РЕЗУЛЬТАТ" in (ца2["why_not"] or ""), ца2["why_not"])
    ца3 = цена_актора({"pricingPerEvent": {"actorChargeEvents": {
        "tweet": {"eventTitle": "tweet"}}}})
    chk("событие без цены ни в ярусах, ни в eventPriceUsd -- отказ по имени",
        not ца3["ok"] and "цены" in (ца3["why_not"] or ""), ца3["why_not"])
    chk("модели оплаты без событий -- отказ по имени", not цена_актора({})["ok"], None)

    # --------- 2b. выбор перебирает кандидатов по цене до подходящей схемы
    схемы = {
        "дорогой/актор": {"ok": True, "build": "b1", "поля": {
            "twitterHandles": {"type": "array"}, "start": {}, "end": {},
            "maxItems": {}}},
        "дешёвый/без-предела": {"ok": True, "build": "b2", "поля": {
            "accountUrls": {"type": "array"}, "startDate": {}, "endDate": {},
            "maxCollections": {}}},
    }
    кандидаты = [
        {"имя": "дешёвый/без-предела", "цена_за_1000": 0.1, "правлен": None,
         "запусков_за_месяц": 10, "цена_за_результат": 0.0001, "за_запуск": 0.0,
         "модель": МОДЕЛЬ_ЗА_РЕЗУЛЬТАТ, "цена_сырая": {}},
        {"имя": "дорогой/актор", "цена_за_1000": 0.4, "правлен": None,
         "запусков_за_месяц": 20, "цена_за_результат": 0.0004, "за_запуск": 0.0,
         "модель": МОДЕЛЬ_ЗА_РЕЗУЛЬТАТ, "цена_сырая": {}},
    ]

    def _поиск_заглушка(_слово, *, предел=100):  # noqa: ARG001
        return {"ok": True, "why_not": None, "акторы": кандидаты}

    настоящий_поиск = globals()["store_поиск"]
    globals()["store_поиск"] = _поиск_заглушка
    try:
        в = выбрать_актора(читать_схему=lambda имя: схемы[имя])
    finally:
        globals()["store_поиск"] = настоящий_поиск
    chk("дешёвый без поля предела ПРОПУЩЕН, взят следующий по цене -- и причина "
        "пропуска названа",
        в["ok"] and в["выбран"]["имя"] == "дорогой/актор"
        and в["пропущены"] and "предела строк" in в["пропущены"][0]["почему"],
        (в.get("выбран") or {}).get("имя"), )
    chk("у выбранного разобраны все четыре поля схемы",
        (в.get("разбор") or {}).get("ok")
        and в["разбор"]["аккаунты"] == "twitterHandles", в.get("разбор"))
    chk("поле аккаунтов по имени с url помечается как ссылки",
        разобрать_схему({"accountUrls": {"type": "array"}, "startDate": {},
                          "endDate": {}, "maxItems": {}})["ссылками"] is True, None)
    chk("ссылками -- значит в тело идут https://x.com/<handle>, а не handle",
        тело_запроса(разобрать_схему({"accountUrls": {"type": "array"},
                                       "startDate": {}, "endDate": {},
                                       "maxItems": {}}), ["kol1"],
                      предел=5)["accountUrls"] == ["https://x.com/kol1"], None)

    # ------------------------------------------------ 3. адреса и время
    настоящий = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    pump = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    подпись = ("5Ks1u9aJpMUvvBw1cXKLDTrcnpTkVDNCn7Uu1DkKDBmPQbMFsnbEtb6rDMU1N6qLv"
               "dZTkFSsmtTUxFJcYPhkqpHm")
    chk("адрес Solana из текста находится",
        адреса_из_текста(f"берём {настоящий} сейчас") == [настоящий], None)
    chk("адрес с окончанием pump находится",
        адреса_из_текста(f"ca: {pump}") == [pump], None)
    chk("подпись транзакции коллом НЕ считается (87--88 знаков вне 32--44)",
        адреса_из_текста(подпись) == [], адреса_из_текста(подпись))
    chk("слово из обычного текста коллом не считается",
        адреса_из_текста("этот токен полетит на луну, заходим") == [], None)
    chk("один адрес дважды в одном посте -- один раз",
        адреса_из_текста(f"{настоящий} и снова {настоящий}") == [настоящий], None)
    # ДВА РАЗБОРА base58 ОБЯЗАНЫ СОВПАДАТЬ. На бегунке GitHub solders нет, и
    # проверка ключа идёт своим разбором; здесь solders есть -- значит здесь и
    # сверяем, иначе расхождение нашлось бы только в прогоне, за деньги.
    try:
        from solders.pubkey import Pubkey as _Pk  # noqa: PLC0415
        есть_solders = True
    except ImportError:
        есть_solders = False
    # НАЛИЧИЕ solders -- ФАКТ СРЕДЫ, А НЕ ТРЕБОВАНИЕ. На бегунке GitHub его нет, и
    # требовать его значило бы валить самопроверку на ровном месте (так и вышло в
    # прогоне 37009583564). Поэтому здесь ФАКТ печатается, а проверяются две вещи:
    # свой разбор сам по себе отвечает верно, и там, где solders есть, два разбора
    # не расходятся.
    chk(f"свой разбор сам по себе верен (solders в этой среде "
        f"{'есть' if есть_solders else 'НЕТ'}): адрес найден, подпись отвергнута",
        (lambda б: bool(б) and len(б) == 32)(_байты_своим_разбором(настоящий))
        and not (lambda б: bool(б) and len(б) == 32)(_байты_своим_разбором(подпись)),
        None)
    образцы = [настоящий, pump, подпись, подпись[:44], "коллы", "1" * 32,
               "1" * 44, настоящий[:31], настоящий + "x",
               "11111111111111111111111111111111",
               "So11111111111111111111111111111111111111112"]
    расхождения = []
    for о_ in образцы:
        свой = _байты_своим_разбором(о_)
        свой_ок = bool(свой) and len(свой) == 32
        try:
            их_ок = есть_solders and len(bytes(_Pk.from_string(о_))) == 32
        except Exception:  # noqa: BLE001
            их_ок = False
        if есть_solders and свой_ок != их_ок:
            расхождения.append((о_[:12], свой_ок, их_ок))
    chk(f"свой разбор base58 и solders дают один ответ на {len(образцы)} образцах"
        + ("" if есть_solders else " (solders нет -- сверка идёт там, где он есть)"),
        not расхождения, расхождения)
    chk("свой разбор: ведущие единицы -- это ведущие нули байтов",
        _байты_своим_разбором("11111111111111111111111111111111") == b"\x00" * 32,
        None)
    chk("свой разбор: знак вне алфавита -- None, а не частичный разбор",
        _байты_своим_разбором("0OIl" + "1" * 28) is None, None)
    chk("время ISO с Z приводится к UTC",
        время_utc("2026-09-24T10:11:12Z") == "2026-09-24T10:11:12Z", None)
    chk("время со сдвигом приводится к UTC",
        время_utc("2026-09-24T13:11:12+03:00") == "2026-09-24T10:11:12Z", None)
    chk("время видом Twitter разбирается",
        время_utc("Wed Oct 01 12:00:00 +0000 2026") == "2026-10-01T12:00:00Z", None)
    chk("время в миллисекундах разбирается",
        время_utc(1790208000000) == "2026-09-24T00:00:00Z", время_utc(1790208000000))
    chk("непонятное время -- None, а не догадка", время_utc("вчера") is None, None)
    chk("границы окна включительно, а сутки после -- вне",
        в_окне("2026-09-24T00:00:00Z") and в_окне("2026-10-01T23:59:59Z")
        and not в_окне("2026-10-02T00:00:00Z")
        and not в_окне("2026-09-23T23:59:59Z"), None)

    # ------------------------------------- 4. разбор постов и счётчик расхода
    посты = [
        {"userName": "kol1", "createdAt": "2026-09-25T10:00:00Z", "id": "111",
         "text": f"call {настоящий}"},
        {"author": {"userName": "kol2"}, "created_at": "Wed Oct 01 23:59:59 +0000 2026",
         "id": "222", "full_text": f"two {настоящий} and {pump}"},
        {"userName": "kol3", "createdAt": "2026-10-03T10:00:00Z", "text": f"late {pump}"},
        {"userName": "kol4", "createdAt": "вчера", "text": f"bad time {pump}"},
        {"userName": "kol5", "createdAt": "2026-09-26T10:00:00Z", "text": "без адреса"},
    ]
    р4 = разобрать_посты(посты)
    chk("постов пять, в окне три, коллов три",
        (р4["посты"], р4["в_окне"], р4["коллов"]) == (5, 3, 3),
        (р4["посты"], р4["в_окне"], р4["коллов"]))
    chk("пост вне окна и пост с неразобранным временем посчитаны отдельно",
        р4["вне_окна"] == 1 and р4["без_времени"] == 1,
        (р4["вне_окна"], р4["без_времени"]))
    chk("handle берётся и из верхнего уровня, и из author",
        {с["author_handle"] for с in р4["строки"]} == {"kol1", "kol2"},
        {с["author_handle"] for с in р4["строки"]})
    chk("у каждой строки РОВНО шесть полей формата Code-2",
        all(set(с) == {"mint", "t_utc", "author_handle", "post_url",
                        "author_wallet", "text"} for с in р4["строки"]), None)
    chk("время в строках -- с секундами",
        all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", с["t_utc"])
            for с in р4["строки"]), None)
    chk("ссылка берётся из ответа, а при отсутствии собирается из handle и id",
        [с["post_url"] for с in р4["строки"]]
        == ["https://x.com/kol1/status/111",
            "https://x.com/kol2/status/222", "https://x.com/kol2/status/222"],
        [с["post_url"] for с in р4["строки"]])
    chk("без ссылки и без id post_url пустой, и такие посты посчитаны",
        разобрать_посты([{"userName": "k", "createdAt": НАЧАЛО,
                           "text": f"x {настоящий}"}])["без_ссылки"] == 1, None)
    р5 = разобрать_посты(посты, связка={"kol1": {"кошелёк": "W1"}})
    chk("author_wallet ставится из связки, а без связки остаётся ПУСТЫМ",
        [с["author_wallet"] for с in р5["строки"]] == ["W1", "", ""],
        [с["author_wallet"] for с in р5["строки"]])
    chk("посты без известного кошелька посчитаны отдельно",
        р5["без_кошелька"] == 2, р5["без_кошелька"])

    def зовун_пустой(_тело):
        return {"ok": True, "строки": [], "why_not": None, "мс": 1}

    из_ = сбор(аккаунты=[{"handle": f"h{и}"} for и in range(50)], актор="x/y",
                разбор=р, цена=0.0004, зовун=зовун_пустой,
                папка_сырых=Path("/tmp/kolly_self_test"))
    chk("при цене 0.0004 $ бюджет даёт 12500 постов и 250 на аккаунт",
        (из_["предел_постов"], из_["на_аккаунт"]) == (12500, 250),
        (из_["предел_постов"], из_["на_аккаунт"]))
    дорого = сбор(аккаунты=[{"handle": f"h{и}"} for и in range(50)], актор="x/y",
                   разбор=р, цена=0.01, зовун=зовун_пустой,
                   папка_сырых=Path("/tmp/kolly_self_test"))
    chk("дорогой актор -- ОТКАЗ по имени, а не урезанное окно молча",
        not дорого["ok"] and "меньше" in (дорого["why_not"] or ""),
        дорого.get("why_not"))

    считано = {"n": 0}

    def зовун_полный(_тело):
        считано["n"] += 1
        return {"ok": True, "строки": [{"userName": "h", "createdAt": НАЧАЛО,
                                         "text": "x"}] * 1000, "why_not": None, "мс": 1}

    стоп = сбор(аккаунты=[{"handle": f"h{и}"} for и in range(50)], актор="x/y",
                 разбор=разобрать_схему({"handles": {"type": "string"},
                                          "since": {"type": "string"},
                                          "until": {"type": "string"},
                                          "maxItems": {"type": "integer"}}),
                 цена=0.001, зовун=зовун_полный,
                 папка_сырых=Path("/tmp/kolly_self_test"))
    chk("счётчик остановил проход ДО превышения потолка",
        стоп["расход"] <= ПРЕДЕЛ_РАСХОДА and считано["n"] < 50,
        (стоп["расход"], считано["n"]))

    # ---------------------------------- 5. отказ без токена -- по имени
    было_т = os.environ.pop(ИМЯ_ТОКЕНА, None)
    try:
        chk("без APIFY_TOKEN сетевой шаг отказывает по имени, а не падает "
            "трассировкой", шаг_выбор_без_токена(), None)
    finally:
        if было_т is not None:
            os.environ[ИМЯ_ТОКЕНА] = было_т

    плохих = [п for п in проверки if not п[1]]
    for что, ок, факт in проверки:
        print(("  ok  " if ок else " ПЛОХО") + f" {что}" + ("" if ок else f" -- {факт}"))
    print(f"\nпроверок {len(проверки)}, ждали {ЖДЁМ_ПРОВЕРОК}, не прошло {len(плохих)}")
    if len(проверки) != ЖДЁМ_ПРОВЕРОК:
        print("ЧИСЛО ПРОВЕРОК НЕ СОВПАЛО -- молчаливый пропуск считается провалом")
        return 1
    return 1 if плохих else 0


# -------------------------------------------------------------------------- шаги

def шаг_список() -> int:
    сп = собрать_список()
    ПАПКА_ВЫХОДА.mkdir(parents=True, exist_ok=True)
    ФАЙЛ_СПИСКА.write_text(json.dumps(сп, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    печать(f"аккаунтов {len(сп['аккаунты'])} из {сп['нужно']}, файл "
            f"{ФАЙЛ_СПИСКА.relative_to(КОРЕНЬ)}")
    for а in сп["аккаунты"][:10]:
        печать(f"  {а['handle']:<22} followCount {а['followCount']:>7} "
                f"{','.join(а['категории'])}")
    if not сп["ok"]:
        печать("ОТКАЗ:", сп["why_not"])
        return 1
    return 0


def шаг_выбор() -> int:
    в = выбрать_актора()
    ФАЙЛ_ВЫБОРА.parent.mkdir(parents=True, exist_ok=True)
    ФАЙЛ_ВЫБОРА.write_text(json.dumps(в, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    печать("выбор записан в", ФАЙЛ_ВЫБОРА.relative_to(КОРЕНЬ))
    for п in в.get("пропущены") or []:
        печать(f"  пропущен {п['имя']}: {п['почему']}")
    if not в["ok"]:
        печать("ОТКАЗ выбора актора:", в["why_not"])
        return 1
    печать(f"выбран {в['выбран']['имя']}: {в['выбран']['цена_за_1000']} $ за 1000 "
            f"постов (+{в['выбран'].get('за_запуск')} $ за запуск), событие "
            f"{в['выбран'].get('событие')}, правлен {в['выбран']['правлен']}, "
            f"запусков за месяц {в['выбран'].get('запусков_за_месяц')}")
    печать("сборка", в.get("build"), "| поля входа:",
            json.dumps(в.get("разбор"), ensure_ascii=False))
    return 0


def шаг_сбор(*, предел_расхода: float, предел_запросов: int) -> int:
    сп = собрать_список()
    if not сп["ok"]:
        печать("ОТКАЗ:", сп["why_not"])
        return 1
    в = выбрать_актора()
    if not в["ok"]:
        печать("ОТКАЗ выбора актора:", в["why_not"])
        for п in в.get("пропущены") or []:
            печать(f"  пропущен {п['имя']}: {п['почему']}")
        return 1
    р = в["разбор"]
    цена = float(в["выбран"]["цена_за_результат"])
    за_запуск = float(в["выбран"].get("за_запуск") or 0.0)
    печать(f"актор {в['выбран']['имя']}, цена {цена} $ за пост, плюс "
            f"{за_запуск} $ за запуск, потолок {предел_расхода} $")
    итог = сбор(аккаунты=сп["аккаунты"], актор=в["выбран"]["имя"], разбор=р,
                 цена=цена, за_запуск=за_запуск, предел_расхода=предел_расхода,
                 предел_запросов=предел_запросов)
    if not итог["ok"]:
        печать("ОТКАЗ сбора:", итог["why_not"])
        return 1
    записать_коллы(итог)
    записать_страницу(итог, в, сп)
    печать(f"постов {итог['постов']}, в окне {итог['в_окне']}, коллов "
            f"{итог['коллов']}, расход {итог['расход']} $")
    печать("файлы:", ФАЙЛ_КОЛЛОВ.relative_to(КОРЕНЬ), "и",
            СТРАНИЦА.relative_to(КОРЕНЬ))
    return 0


def main_с(аргументы: list) -> int:
    """main с заданными аргументами -- чтобы самопроверка могла его позвать."""
    return main(аргументы)


def main(аргументы: list | None = None) -> int:
    п = argparse.ArgumentParser(description="Коллы KOL: посты X и адреса Solana")
    п.add_argument("--spisok", action="store_true", help="офлайн: пятьдесят handle")
    п.add_argument("--vybor", action="store_true", help="сеть, 0 $: выбор актора")
    п.add_argument("--sbor", action="store_true", help="сеть, платно: посты и коллы")
    п.add_argument("--self-test", action="store_true")
    п.add_argument("--predel-rashoda", type=float, default=ПРЕДЕЛ_РАСХОДА)
    п.add_argument("--predel-zaprosov", type=int, default=ПРЕДЕЛ_ЗАПРОСОВ)
    а = п.parse_args(аргументы)
    if а.self_test:
        return self_test()
    if а.spisok:
        return шаг_список()
    # СЕТЕВЫЕ ШАГИ: отказ по имени, а не трассировка. Нет токена -- нет похода.
    try:
        if а.vybor:
            return шаг_выбор()
        if а.sbor:
            return шаг_сбор(предел_расхода=а.predel_rashoda,
                             предел_запросов=а.predel_zaprosov)
    except ОшибкаСбора as сбой:
        печать(f"ОТКАЗ: {сбой}")
        return 2
    п.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
