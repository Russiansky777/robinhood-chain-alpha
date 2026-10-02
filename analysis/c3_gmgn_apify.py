#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ВЫГРУЗКА ЛИДЕРБОРДОВ GMGN через Apify по API. Один скрипт, один прогон.

АКТОР: maximedupre/gmgn-copytrade-wallet-scraper (Apify Store), оплата за
результат -- 0.00375 $ за кошелёк. GMGN отдаёт не больше 100 строк на запрос,
поэтому ширина берётся КОМБИНАЦИЯМИ (тип трейдера x сортировка), а не одним
большим запросом.

ТОКЕН -- ТОЛЬКО ИЗ ОКРУЖЕНИЯ APIFY_TOKEN. Он не пишется в журнал, не попадает в
выход и не ставится в адрес запроса (только заголовок Authorization): адрес
печатают и журналы прогонов, и ошибки библиотек. Всё, что печатается или
сохраняется, проходит через zateret().

ЗНАЧЕНИЯ ПЕРЕЧИСЛЕНИЙ НЕ УГАДЫВАЮТСЯ. Перед запусками скрипт читает схему входа
актора (GET /v2/acts/{actor} -> сборка -> inputSchema) и СВЕРЯЕТ каждое значение
комбинаций с перечислением схемы. Значения нет в схеме -- отказ по имени со
списком того, что схема действительно принимает, а не запуск наугад за деньги.
Единственное значение, названное владельцем по факту консоли, -- sortBy
"tracked" (не "tracked_count"); оно тоже проверяется схемой, а не берётся на веру.

ПОТОЛОК ПРОХОДА -- СЧЁТЧИКОМ, НЕ ВЕРОЙ: не больше ПРЕДЕЛ_ЗАПРОСОВ запросов и не
дороже ПРЕДЕЛ_РАСХОДА долларов. Проверка стоит ПЕРЕД каждым запросом по худшему
случаю (100 строк), поэтому превысить потолок нельзя даже на последнем запросе.
Потолок считается НА ОДИН ЗАПУСК СКРИПТА: пробный запуск (5 кошельков, около
0.02 $) идёт отдельным запуском и в счётчик прохода не входит -- это сказано
здесь, чтобы 4.52 $ за недельный прогон не выглядели расхождением с 4.5 $.

ВЫХОД:
  data/gmgn/gmgn_<ДАТА>_<ЧАС>Z.csv -- один кошелёк одна строка;
  data/gmgn/raw/<комбинация>.json  -- сырые строки каждого запуска как есть;
  docs/gmgn_<ДАТА>.md             -- по каждому запуску строк, прирост новых
                                     адресов, итог уникальных и расход в $.

Ничего не отбирается и не оценивается: сито -- у Code-2 на архиве.

Запуск:  python3 analysis/c3_gmgn_apify.py --proba      (пробный, 5 кошельков)
         python3 analysis/c3_gmgn_apify.py --prohod      (первый проход, 12)
         python3 analysis/c3_gmgn_apify.py --self-test   (без сети)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
АКТОР = "maximedupre~gmgn-copytrade-wallet-scraper"
API = "https://api.apify.com/v2"
ЦЕНА_ЗА_КОШЕЛЁК = 0.00375
СТРОК_НА_ЗАПРОС = 100
ПРЕДЕЛ_ЗАПРОСОВ = 20
ПРЕДЕЛ_РАСХОДА = 8.0
ИМЯ_ТОКЕНА = "APIFY_TOKEN"
СРОК_ЗАПУСКА_С = 300.0
ЦЕПЬ = "sol"

# КОМБИНАЦИИ ПЕРВОГО ПРОХОДА (12 запусков, ~4.5 $).
#
# ЗНАЧЕНИЯ -- ИЗ СХЕМЫ АКТОРА, А НЕ ИЗ СПИСКА НА СЛОВАХ. Пробный прогон 01.10
# (сборка XrFNAu5yp4sOfbZ7G) прочитал перечисления, и половина названий из задания
# в схеме НЕ СУЩЕСТВУЕТ. Схема принимает:
#   traderType: all, smart_degen, pump_smart, launchpad_smart, kol, fresh_wallet,
#               sniper, top_tracked, top_renamed, top_dev, live
#   sortBy:     profit_1d/7d/30d, pnl_1d/7d/30d, winrate_1d/7d/30d, txs_1d/7d/30d,
#               volume_1d/7d/30d, net_inflow_1d/7d/30d, last_active, balance,
#               tracked, renamed
# Перевод списка задания в имена схемы (замена -- строкой, не молча):
#   pnl -> pnl_7d, win_rate -> winrate_7d, transactions -> txs_7d,
#   volume -> volume_7d, tracked и profit_7d совпали;
#   pump_smart_money -> pump_smart, launchpad_smart_money -> launchpad_smart,
#   kol_vc -> kol, sniper и smart_degen совпали;
#   smart_money -> all: равного имени в схеме НЕТ, и "all" -- самый широкий
#   список без фильтра типа, то есть замена, которая не может вернуть чужую
#   выборку. Это ЗАМЕНА, и она названа здесь и в странице выгрузки.
# Не задействованы (дешёвая ширина на следующий проход): fresh_wallet,
# top_renamed, top_dev, live, окна 1d и 30d, net_inflow_*, last_active, balance.
ТИП_ПО_УМОЛЧАНИЮ = "top_tracked"
СОРТИРОВКА_ПО_УМОЛЧАНИЮ = "tracked"
# ВТОРОЙ ПРОХОД (слово владельца 01.10 после первого). Убраны комбинации, давшие
# НОЛЬ новых адресов в первом проходе: top_tracked x pnl_7d / winrate_7d /
# volume_7d и all x tracked -- это 1.5 $ за те же 100 кошельков, что уже дал
# tracked. Добавлены четыре типа, которых не пробовали, и окна 1d/30d у двух
# типов, которые ширину дали: top_tracked (+100, +51, +19) и pump_smart (+99).
СОРТИРОВКИ = ("tracked", "profit_7d", "txs_7d")
ТИПЫ = ("pump_smart", "launchpad_smart", "sniper", "kol", "smart_degen",
         "fresh_wallet", "top_dev", "top_renamed", "live")
# Окна 1d и 30d -- только у сортировок, которые себя показали (profit и txs), и
# только у двух типов: остальным они стоили бы по 0.375 $ за непроверенную догадку.
ТИПЫ_ОКОН = ("top_tracked", "pump_smart")
СОРТИРОВКИ_ОКОН = ("profit_1d", "profit_30d", "txs_1d", "txs_30d")
# Что убрано из первого прохода -- в страницу выгрузки, чтобы это не забылось.
УБРАНО_ПОСЛЕ_ПЕРВОГО = {
    "top_tracked__pnl_7d": "0 новых адресов в первом проходе",
    "top_tracked__winrate_7d": "0 новых адресов в первом проходе",
    "top_tracked__volume_7d": "0 новых адресов в первом проходе",
    "all__tracked": "0 новых адресов: тот же список, что top_tracked",
}
# Чем заменено имя из первого задания -- в страницу выгрузки.
ЗАМЕНЫ_ИМЁН = {"pnl": "pnl_7d", "win_rate": "winrate_7d",
                "transactions": "txs_7d", "volume": "volume_7d",
                "smart_money": "all", "pump_smart_money": "pump_smart",
                "launchpad_smart_money": "launchpad_smart", "kol_vc": "kol"}


def комбинации_прохода() -> list:
    """Комбинации прохода: сортировки, типы и окна. Ровно под потолок запросов."""
    из_ = [{"traderType": ТИП_ПО_УМОЛЧАНИЮ, "sortBy": с} for с in СОРТИРОВКИ]
    из_ += [{"traderType": т, "sortBy": СОРТИРОВКА_ПО_УМОЛЧАНИЮ} for т in ТИПЫ]
    из_ += [{"traderType": т, "sortBy": с}
            for т in ТИПЫ_ОКОН for с in СОРТИРОВКИ_ОКОН]
    return из_


def имя_комбинации(к: dict) -> str:
    return f"{к['traderType']}__{к['sortBy']}"


# СТОЛБЦЫ ВЫХОДА -- в порядке владельца. Значение -- имена, под которыми поле
# может прийти от актора: имя из его схемы заранее не известно, поэтому берётся
# первое непустое из списка, а какие столбцы остались пустыми -- печатается
# числом (это и есть проверка пробного запуска).
СТОЛБЦЫ = (
    ("walletAddress", ("walletAddress", "wallet_address", "address", "wallet")),
    ("followCount", ("followCount", "follow_count", "followers", "trackedCount",
                      "tracked_count")),
    ("sourceRankLuchshij", ()),
    ("sourceKombinacii", ()),
    ("traderTypeTegi", ()),
    ("buys7d", ("buys7d", "buy7d", "buys_7d", "buy_7d")),
    ("sells7d", ("sells7d", "sell7d", "sells_7d", "sell_7d")),
    ("transactions7d", ("transactions7d", "txs7d", "transactions_7d", "tx7d")),
    ("winRate7d", ("winRate7d", "winrate7d", "win_rate_7d", "winRate")),
    ("pnl7d", ("pnl7d", "pnl_7d", "pnl")),
    ("realizedProfit7d", ("realizedProfit7d", "realized_profit_7d",
                           "realizedProfit", "profit7d", "profit_7d")),
    ("volume7d", ("volume7d", "volume_7d", "volume")),
    ("solBalance", ("solBalance", "sol_balance", "balance")),
    ("lastActiveAt", ("lastActiveAt", "last_active_at", "lastActiveTimestamp",
                       "last_active")),
    ("tags", ("tags", "tagList", "tag")),
    # ПРОФИЛЬ ЛЕЖИТ ВЛОЖЕННЫМ ОБЪЕКТОМ `profile` -- см. _достать. Верхний уровень
    # оставлен первым кандидатом: если актор когда-нибудь поднимет поле наверх,
    # разбор не сломается.
    ("nickname", ("nickname", "profile.nickname", "profile.name", "name", "nick")),
    ("twitterUsername", ("twitterUsername", "profile.twitterUsername",
                          "twitter_username", "twitter")),
    ("twitterName", ("twitterName", "profile.twitterName")),
)
СТОЛБЕЦ_АДРЕСА = "walletAddress"
# Столбцы, которые пробный запуск обязан увидеть заполненными (слово владельца).
ПРОБНЫЕ_СТОЛБЦЫ = ("walletAddress", "followCount", "buys7d", "winRate7d",
                    "pnl7d", "tags")


class ОшибкаВыгрузки(Exception):
    pass


# ----------------------------------------------------------------- секрет

def токен() -> str:
    """Токен из окружения. Нет -- отказ по имени, без подсказок о значении."""
    т = (os.environ.get(ИМЯ_ТОКЕНА) or "").strip()
    if not т:
        raise ОшибкаВыгрузки(
            f"{ИМЯ_ТОКЕНА} не задан в окружении -- выгрузка не идёт. Токен "
            f"ставится секретом рабочего процесса, в командную строку он не "
            f"передаётся никогда")
    return т


def zateret(текст: str) -> str:
    """Затереть токен в любой строке перед печатью или записью.

    Токен попадает в текст не только нашими руками: его подставляют сообщения
    urllib об ошибке и трассировки. Поэтому через эту функцию идёт ВСЁ, что
    печатается и записывается.
    """
    с = str(текст)
    т = (os.environ.get(ИМЯ_ТОКЕНА) or "").strip()
    if т and len(т) >= 8:
        с = с.replace(т, "<APIFY_TOKEN>")
    return с


def печать(*части) -> None:
    print(zateret(" ".join(str(ч) for ч in части)), flush=True)


# -------------------------------------------------------------- сеть

def _зов(метод: str, путь: str, тело=None, *, срок: float = 60.0) -> tuple:
    """(код, разобранное_тело). Токен -- только в заголовке Authorization."""
    данные = None if тело is None else json.dumps(тело).encode("utf-8")
    зап = urllib.request.Request(f"{API}{путь}", data=данные, method=метод)
    зап.add_header("Authorization", f"Bearer {токен()}")
    зап.add_header("Accept", "application/json")
    if данные is not None:
        зап.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(зап, timeout=срок) as отв:
            сырое = отв.read().decode("utf-8", "replace")
            код = отв.getcode()
    except urllib.error.HTTPError as сбой:
        сырое = сбой.read().decode("utf-8", "replace")
        код = сбой.code
    except (urllib.error.URLError, TimeoutError, OSError) as сбой:
        raise ОшибкаВыгрузки(zateret(f"сеть не ответила: {type(сбой).__name__}: "
                                      f"{сбой}")) from None
    try:
        return код, json.loads(сырое) if сырое else None
    except ValueError:
        return код, сырое


def схема_актора() -> dict:
    """Схема входа актора: перечисления полей. Берётся из СБОРКИ, а не из памяти.

    Путь по API: GET /v2/acts/{actor} -> taggedBuilds.latest.buildId ->
    GET /v2/actor-builds/{buildId} -> inputSchema (строкой JSON). Схемы нет --
    отказ по имени: запускать за деньги с неизвестными значениями нельзя.
    """
    из_ = {"ok": False, "why_not": None, "polya": {}, "build": None,
            "сырая": None}
    код, тело = _зов("GET", f"/acts/{АКТОР}")
    if код != 200 or not isinstance(тело, dict):
        из_["why_not"] = f"актор не прочитан: код {код}, ответ {str(тело)[:160]}"
        return из_
    д = тело.get("data") or {}
    сборки = д.get("taggedBuilds") or {}
    айди = ((сборки.get("latest") or {}).get("buildId")
            or (сборки.get("version-0") or {}).get("buildId"))
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
    из_.update(ok=True, сырая=сырая, polya=сырая.get("properties") or {})
    return из_


def сверить_комбинации(поля: dict, комбинации: list) -> dict:
    """Каждое значение комбинаций -- из перечисления схемы. Иначе отказ по имени."""
    из_ = {"ok": True, "why_not": None, "перечисления": {}, "плохие": []}
    for имя in ("traderType", "sortBy", "chain"):
        п = поля.get(имя) or {}
        сп = п.get("enum")
        if isinstance(сп, list) and сп:
            из_["перечисления"][имя] = list(сп)
    for к in комбинации:
        for имя, знач in list(к.items()) + [("chain", ЦЕПЬ)]:
            сп = из_["перечисления"].get(имя)
            if сп is not None and знач not in сп:
                из_["плохие"].append((имя, знач, сп))
    if из_["плохие"]:
        имя, знач, сп = из_["плохие"][0]
        из_.update(ok=False,
                   why_not=(f"{имя}={знач!r} нет в перечислении схемы актора; "
                             f"схема принимает: {', '.join(map(str, сп))}"))
    return из_


def запуск(к: dict, *, максимум: int, срок: float = СРОК_ЗАПУСКА_С) -> dict:
    """Один синхронный запуск актора. Возвращает {ok, строки, why_not, мс}."""
    из_ = {"ok": False, "строки": [], "why_not": None, "мс": None,
            "комбинация": имя_комбинации(к)}
    тело = {"chain": ЦЕПЬ, "traderType": к["traderType"], "sortBy": к["sortBy"],
             "maxWallets": int(максимум)}
    т0 = time.perf_counter()
    код, отв = _зов("POST", f"/acts/{АКТОР}/run-sync-get-dataset-items",
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


# ------------------------------------------------------- разбор и объединение

def _достать(строка: dict, имя: str):
    """Значение по имени, в том числе ВЛОЖЕННОМУ через точку ("profile.nickname").

    ПОЧЕМУ ВЛОЖЕННОЕ. Актор отдаёт профиль не на верхнем уровне, а отдельным
    объектом `profile` каждой строки набора (ключи nickname, name, avatarUrl,
    twitterUsername, twitterName, twitterDescription, twitchChannelName -- они
    есть у ВСЕХ 1149 сырых строк первого прохода). Прежний разбор смотрел только
    верхний уровень, поэтому столбцы nickname и twitterUsername вышли пустыми у
    всех 493 кошельков CSV, хотя в сырых ответах они были. Это та самая связка
    twitter -> кошелёк, и терялась она здесь.
    """
    если = строка
    for часть in str(имя).split("."):
        if not isinstance(если, dict):
            return None
        если = если.get(часть)
    return если if если not in (None, "") else None


def _первое(строка: dict, имена: tuple):
    for и in имена:
        з = _достать(строка, и)
        if з is not None:
            return з
    return None


def _список_в_строку(знач) -> str:
    if isinstance(знач, (list, tuple, set)):
        return ";".join(str(x) for x in знач if x not in (None, ""))
    return "" if знач is None else str(знач)


def строка_кошелька(сырая: dict, *, комбинация: str, место: int) -> dict:
    """Строка актора -> наша строка. Имена полей -- по списку, не по одному."""
    из_ = {}
    for столбец, имена in СТОЛБЦЫ:
        if not имена:
            continue
        з = _первое(сырая, имена)
        из_[столбец] = (_список_в_строку(з) if столбец == "tags"
                        else ("" if з is None else з))
    из_["sourceRankLuchshij"] = место
    из_["sourceKombinacii"] = комбинация
    из_["traderTypeTegi"] = комбинация.split("__")[0]
    return из_


def объединить(запуски: list) -> dict:
    """Один кошелёк -- одна строка. {строки, прирост по запускам, уникальных}.

    sourceRankLuchshij -- МЕНЬШЕЕ место (первое место лучше последнего),
    sourceKombinacii и traderTypeTegi -- через ';' в порядке встречи. Остальные
    поля берутся от ПЕРВОЙ встречи: это тот же кошелёк, и числа у него те же;
    расхождение между комбинациями, если оно будет, видно в сырых выгрузках.
    """
    по_адресу: dict = {}
    прирост = []
    for з in запуски:
        было = len(по_адресу)
        строк, без_адреса = 0, 0
        for место, сырая in enumerate(з.get("строки") or [], start=1):
            if not isinstance(сырая, dict):
                без_адреса += 1
                continue
            н = строка_кошелька(сырая, комбинация=з["комбинация"], место=место)
            адрес = н.get(СТОЛБЕЦ_АДРЕСА)
            if not адрес:
                без_адреса += 1
                continue
            строк += 1
            если = по_адресу.get(адрес)
            if если is None:
                по_адресу[адрес] = н
                continue
            если["sourceRankLuchshij"] = min(если["sourceRankLuchshij"],
                                              н["sourceRankLuchshij"])
            for поле in ("sourceKombinacii", "traderTypeTegi"):
                части = [ч for ч in если[поле].split(";") if ч]
                for ч in н[поле].split(";"):
                    if ч and ч not in части:
                        части.append(ч)
                если[поле] = ";".join(части)
            for столбец, имена in СТОЛБЦЫ:
                if имена and если.get(столбец) in (None, "") and н.get(столбец) != "":
                    если[столбец] = н[столбец]
        прирост.append({"комбинация": з["комбинация"], "строк": строк,
                         "без_адреса": без_адреса,
                         "прирост": len(по_адресу) - было,
                         "итог_уникальных": len(по_адресу),
                         "расход": round(строк * ЦЕНА_ЗА_КОШЕЛЁК, 5),
                         "мс": з.get("мс"), "why_not": з.get("why_not")})
    return {"строки": list(по_адресу.values()), "прирост": прирост,
             "уникальных": len(по_адресу)}


def пустые_столбцы(строки: list) -> dict:
    """Сколько строк заполнено по каждому столбцу -- проверка пробного запуска."""
    из_ = {}
    for столбец, _имена in СТОЛБЦЫ:
        из_[столбец] = sum(1 for с in строки if с.get(столбец) not in (None, ""))
    return из_


# ------------------------------------------------------------------- запись

def записать_csv(строки: list, путь: Path) -> Path:
    путь.parent.mkdir(parents=True, exist_ok=True)
    имена = [с for с, _ in СТОЛБЦЫ]
    with open(путь, "w", encoding="utf-8", newline="") as ф:
        п = csv.DictWriter(ф, fieldnames=имена, extrasaction="ignore")
        п.writeheader()
        for с in sorted(строки, key=lambda x: (x.get("sourceRankLuchshij") or 10**9,
                                                str(x.get(СТОЛБЕЦ_АДРЕСА)))):
            п.writerow({и: с.get(и, "") for и in имена})
    return путь


def _под_корнем(путь: Path) -> str:
    """Путь относительно корня репозитория, а вне него -- как есть (самопроверка)."""
    try:
        return str(путь.relative_to(КОРЕНЬ))
    except ValueError:
        return str(путь)


def записать_страницу(свод: dict, путь: Path, *, csv_путь: Path,
                       столбцы: dict, схема: dict) -> Path:
    путь.parent.mkdir(parents=True, exist_ok=True)
    всего_расход = round(sum(п["расход"] for п in свод["прирост"]), 5)
    стр = [f"# GMGN через Apify: выгрузка {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())}",
            "",
            f"Актор `{АКТОР}`, цена {ЦЕНА_ЗА_КОШЕЛЁК} $ за кошелёк, не больше "
            f"{СТРОК_НА_ЗАПРОС} строк на запрос. Потолок прохода: "
            f"{ПРЕДЕЛ_ЗАПРОСОВ} запросов / {ПРЕДЕЛ_РАСХОДА} $.",
            f"Сборка актора: `{схема.get('build') or 'не прочитана'}`.", "",
            f"Файл: `{_под_корнем(csv_путь)}`. Сырые выгрузки: "
            f"`data/gmgn/raw/<комбинация>.json`.", "",
            "| запуск | комбинация | строк | прирост новых адресов | итог уникальных | расход $ |",
            "|---|---|---|---|---|---|"]
    for и, п in enumerate(свод["прирост"], start=1):
        отказ = f" (отказ: {п['why_not']})" if п.get("why_not") else ""
        стр.append(f"| {и} | `{п['комбинация']}`{отказ} | {п['строк']} | "
                    f"{п['прирост']} | {п['итог_уникальных']} | {п['расход']} |")
    стр += ["", f"**Итог: уникальных кошельков {свод['уникальных']}, запусков "
                f"{len(свод['прирост'])}, расход {всего_расход} $.**", "",
            "## Заполненность столбцов", "",
            "| столбец | строк заполнено |", "|---|---|"]
    for столбец, н in столбцы.items():
        стр.append(f"| `{столбец}` | {н} из {свод['уникальных']} |")
    стр += ["", "## Убрано после первого прохода", "",
            "| комбинация | почему |", "|---|---|"]
    for имя, почему in sorted(УБРАНО_ПОСЛЕ_ПЕРВОГО.items()):
        стр.append(f"| `{имя}` | {почему} |")
    стр += ["", "## Имена из задания, которых в схеме актора нет", "",
            "| в задании | в схеме |", "|---|---|"]
    for было, стало in sorted(ЗАМЕНЫ_ИМЁН.items()):
        стр.append(f"| `{было}` | `{стало}` |")
    стр += ["", "Остальные имена задания совпали со схемой. Не задействованы: "
                "`fresh_wallet`, `top_renamed`, `top_dev`, `live`, окна `1d` и "
                "`30d`, `net_inflow_*`, `last_active`, `balance`.",
            "", "Ничего не отбиралось и не оценивалось: сито -- на архиве у Code-2.",
            ""]
    путь.write_text("\n".join(стр), encoding="utf-8")
    return путь


# -------------------------------------------------------------------- проход

def проход(*, комбинации: list, максимум: int, папка_raw: Path,
            предел_запросов: int = ПРЕДЕЛ_ЗАПРОСОВ,
            предел_расхода: float = ПРЕДЕЛ_РАСХОДА, зовун=None) -> dict:
    """Запуски по комбинациям со счётчиком запросов и расхода. Сеть -- через зовун."""
    зовун = зовун or запуск
    запуски, расход, сделано = [], 0.0, 0
    for к in комбинации:
        # ПОТОЛОК ПРОВЕРЯЕТСЯ ДО ЗАПРОСА И ПО ХУДШЕМУ СЛУЧАЮ (полные 100 строк):
        # после ответа деньги уже потрачены, и проверять там -- проверять веру.
        худший = расход + максимум * ЦЕНА_ЗА_КОШЕЛЁК
        if сделано >= предел_запросов:
            печать(f"СТОП по числу запросов: {сделано} из {предел_запросов}")
            break
        if худший > предел_расхода:
            печать(f"СТОП по расходу: следующий запрос дал бы {худший:.3f} $ при "
                    f"потолке {предел_расхода} $")
            break
        з = зовун(к, максимум=максимум)
        сделано += 1
        строк = len([с for с in (з.get("строки") or []) if isinstance(с, dict)])
        расход = round(расход + строк * ЦЕНА_ЗА_КОШЕЛЁК, 5)
        печать(f"[{сделано}] {имя_комбинации(к)}: строк {строк}, расход всего "
                f"{расход} $" + (f", отказ: {з.get('why_not')}" if not з.get("ok") else ""))
        if з.get("строки"):
            папка_raw.mkdir(parents=True, exist_ok=True)
            (папка_raw / f"{имя_комбинации(к)}.json").write_text(
                json.dumps(з["строки"], ensure_ascii=False, indent=1),
                encoding="utf-8")
        запуски.append(з)
    св = объединить(запуски)
    св["расход"] = расход
    св["запросов"] = сделано
    return св


# ------------------------------------------------------------- самопроверка

# Число проверок объявлено: меньше -- значит что-то не прошло молча.
ЖДЁМ_ПРОВЕРОК = 42


def self_test() -> int:  # noqa: C901, PLR0915
    проверки = []

    def chk(что, ок, факт=None):
        проверки.append((что, bool(ок), факт))

    # ------------------------------------------------- 1. комбинации прохода
    к = комбинации_прохода()
    chk("комбинаций прохода 20 -- ровно под потолок запросов",
        len(к) == 20 == ПРЕДЕЛ_ЗАПРОСОВ, len(к))
    chk("три сортировки на top_tracked -- те, что дали новые адреса",
        [x["sortBy"] for x in к[:3]] == list(СОРТИРОВКИ)
        and all(x["traderType"] == ТИП_ПО_УМОЛЧАНИЮ for x in к[:3]), к[:3])
    chk("девять типов трейдера на сортировке tracked",
        [x["traderType"] for x in к[3:12]] == list(ТИПЫ)
        and all(x["sortBy"] == СОРТИРОВКА_ПО_УМОЛЧАНИЮ for x in к[3:12]), к[3:12])
    chk("окна 1d и 30d -- у двух типов и только у profit и txs",
        {(x["traderType"], x["sortBy"]) for x in к[12:]}
        == {(т, с) for т in ТИПЫ_ОКОН for с in СОРТИРОВКИ_ОКОН}, к[12:])
    chk("убранные комбинации первого прохода в проход НЕ попали",
        not ({имя_комбинации(x) for x in к} & set(УБРАНО_ПОСЛЕ_ПЕРВОГО)),
        sorted({имя_комбинации(x) for x in к} & set(УБРАНО_ПОСЛЕ_ПЕРВОГО)))
    chk("имена комбинаций не повторяются",
        len({имя_комбинации(x) for x in к}) == 20, len({имя_комбинации(x) for x in к}))
    chk("20 запросов по 100 строк -- 7.5 $, и это не выше потолка 8 $",
        round(20 * 100 * ЦЕНА_ЗА_КОШЕЛЁК, 2) == 7.5
        and 20 * 100 * ЦЕНА_ЗА_КОШЕЛЁК <= ПРЕДЕЛ_РАСХОДА,
        round(20 * 100 * ЦЕНА_ЗА_КОШЕЛЁК, 3))

    # ------------------------------------------ 2. сверка значений со схемой
    поля_ок = {"traderType": {"enum": list(ТИПЫ) + list(ТИПЫ_ОКОН)},
                "sortBy": {"enum": list(СОРТИРОВКИ) + list(СОРТИРОВКИ_ОКОН)},
                "chain": {"enum": ["sol", "eth"]}}
    с1 = сверить_комбинации(поля_ок, к)
    chk("все 12 комбинаций проходят сверку со схемой, где значения есть",
        с1["ok"] and not с1["плохие"], с1.get("why_not"))
    поля_плохо = dict(поля_ок, sortBy={"enum": ["tracked_count", "pnl"]})
    с2 = сверить_комбинации(поля_плохо, к)
    chk("схема называет tracked_count вместо tracked -- отказ по имени со списком "
        "того, что схема принимает",
        not с2["ok"] and "tracked" in (с2["why_not"] or "")
        and "tracked_count" in (с2["why_not"] or ""), с2.get("why_not"))
    с3 = сверить_комбинации({}, к)
    chk("перечислений в схеме нет -- сверять нечем, отказа нет",
        с3["ok"] and not с3["перечисления"], с3)
    с4 = сверить_комбинации(dict(поля_ок, chain={"enum": ["eth"]}), к[:1])
    chk("цепь sol отсутствует в перечислении -- тоже отказ",
        not с4["ok"] and "chain" in (с4["why_not"] or ""), с4.get("why_not"))

    # -------------------------------------------------- 3. разбор строки
    сырая = {"wallet_address": "A" * 32, "follow_count": 12, "buy_7d": 3,
              "sell_7d": 1, "txs7d": 4, "win_rate_7d": 0.75, "pnl_7d": 1.5,
              "realized_profit_7d": 2.0, "volume_7d": 30.0, "sol_balance": 9.5,
              "last_active_at": "2026-10-01T00:00:00Z",
              "tags": ["smart_money", "kol"], "name": "ник", "twitter": "x"}
    н = строка_кошелька(сырая, комбинация="top_tracked__tracked", место=3)
    chk("имена полей берутся вариантами: snake_case разобран",
        н["walletAddress"] == "A" * 32 and н["followCount"] == 12
        and н["buys7d"] == 3 and н["winRate7d"] == 0.75, н)
    chk("tags списком -- через точку с запятой",
        н["tags"] == "smart_money;kol", н["tags"])
    chk("место и комбинация встают в свои столбцы",
        н["sourceRankLuchshij"] == 3
        and н["sourceKombinacii"] == "top_tracked__tracked"
        and н["traderTypeTegi"] == "top_tracked", н)
    пустая = строка_кошелька({"walletAddress": "B" * 32}, комбинация="a__b", место=1)
    chk("поля, которых в строке нет, пустые, а не None",
        пустая["pnl7d"] == "" and пустая["tags"] == "", пустая)

    # -------------------------------------------------- 4. объединение
    def _стр(адрес, **прочее):
        д = {"walletAddress": адрес, "followCount": 1}
        д.update(прочее)
        return д
    запуски = [
        {"комбинация": "top_tracked__tracked", "строки":
            [_стр("W1"), _стр("W2"), "мусор", {"нет": "адреса"}], "мс": 10},
        {"комбинация": "top_tracked__pnl", "строки":
            [_стр("W2", pnl7d=5), _стр("W3")], "мс": 11},
        {"комбинация": "sniper__tracked", "строки": [_стр("W3")], "мс": 12},
    ]
    св = объединить(запуски)
    chk("один кошелёк -- одна строка", св["уникальных"] == 3
        and len(св["строки"]) == 3, св["уникальных"])
    w2 = next(с for с in св["строки"] if с["walletAddress"] == "W2")
    chk("лучший sourceRank -- МЕНЬШЕЕ место", w2["sourceRankLuchshij"] == 1,
        w2["sourceRankLuchshij"])
    chk("комбинации встречи -- через ';' и без повторов",
        w2["sourceKombinacii"] == "top_tracked__tracked;top_tracked__pnl",
        w2["sourceKombinacii"])
    w3 = next(с for с in св["строки"] if с["walletAddress"] == "W3")
    chk("теги типов трейдера склеиваются без повторов",
        w3["traderTypeTegi"] == "top_tracked;sniper", w3["traderTypeTegi"])
    chk("пустое поле дозаполняется из следующей встречи",
        w2["pnl7d"] == 5, w2["pnl7d"])
    пр = св["прирост"]
    chk("прирост по запускам: 2, затем 1, затем 0",
        [x["прирост"] for x in пр] == [2, 1, 0], [x["прирост"] for x in пр])
    chk("строки без адреса считаются отдельно, а не как кошельки",
        пр[0]["строк"] == 2 and пр[0]["без_адреса"] == 2, пр[0])
    chk("расход запуска -- строки x цена",
        пр[0]["расход"] == round(2 * ЦЕНА_ЗА_КОШЕЛЁК, 5), пр[0]["расход"])
    chk("итог уникальных растёт монотонно",
        [x["итог_уникальных"] for x in пр] == [2, 3, 3],
        [x["итог_уникальных"] for x in пр])

    # ------------------------------------------------- 5. потолки прохода
    зовов = {"n": 0}

    def зовун(к_, *, максимум):
        зовов["n"] += 1
        return {"ok": True, "комбинация": имя_комбинации(к_),
                 "строки": [_стр(f"X{зовов['n']}_{и}") for и in range(максимум)],
                 "мс": 1}
    зовов["n"] = 0
    св_п = проход(комбинации=комбинации_прохода(), максимум=100,
                   папка_raw=Path("/tmp/нет-такой-папки-самопроверки"),
                   предел_запросов=3, предел_расхода=ПРЕДЕЛ_РАСХОДА, зовун=зовун)
    chk("потолок по числу запросов останавливает проход",
        зовов["n"] == 3 and св_п["запросов"] == 3, (зовов["n"], св_п["запросов"]))
    chk("собранное до стопа не теряется", св_п["уникальных"] == 300,
        св_п["уникальных"])
    зовов["n"] = 0
    св_р = проход(комбинации=комбинации_прохода(), максимум=100,
                   папка_raw=Path("/tmp/нет-такой-папки-самопроверки"),
                   предел_запросов=ПРЕДЕЛ_ЗАПРОСОВ, предел_расхода=1.0, зовун=зовун)
    chk("потолок по расходу считается ДО запроса и по худшему случаю: 1 $ при "
        "0.375 $ за запрос -- два запроса, не три",
        зовов["n"] == 2 and св_р["расход"] <= 1.0, (зовов["n"], св_р["расход"]))
    chk("расход прохода -- сумма расходов запусков",
        св_р["расход"] == round(200 * ЦЕНА_ЗА_КОШЕЛЁК, 5), св_р["расход"])

    # ------------------------------------------------------- 6. секрет
    сохр = os.environ.get(ИМЯ_ТОКЕНА)
    os.environ[ИМЯ_ТОКЕНА] = "apify_api_SEKRETNOEZNACHENIE123"
    chk("токен затирается в любой строке",
        zateret("ошибка с apify_api_SEKRETNOEZNACHENIE123 внутри")
        == "ошибка с <APIFY_TOKEN> внутри",
        zateret("apify_api_SEKRETNOEZNACHENIE123"))
    os.environ.pop(ИМЯ_ТОКЕНА, None)
    chk("без токена -- отказ по имени, без намёка на значение",
        _отказ_без_токена(), None)
    if сохр is None:
        os.environ.pop(ИМЯ_ТОКЕНА, None)
    else:
        os.environ[ИМЯ_ТОКЕНА] = сохр
    # ГДЕ ИМЕННО ЕДЕТ ТОКЕН -- ПРОВЕРЯЕТСЯ ЗАПРОСОМ, А НЕ ПОИСКОМ ПО ТЕКСТУ:
    # поиск по тексту ловит и собственные пояснения, и правду про запрос не
    # говорит. Здесь запрос перехватывается и осматривается.
    os.environ[ИМЯ_ТОКЕНА] = "apify_api_SEKRETNOEZNACHENIE123"
    пойманный = {}

    class _Отв:
        def read(self):
            return b"[]"

        def getcode(self):
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    настоящий = urllib.request.urlopen
    try:
        urllib.request.urlopen = lambda зап, timeout=None: (
            пойманный.update(url=зап.full_url, hdr=dict(зап.headers),
                             тело=зап.data) or _Отв())
        _зов("POST", "/acts/x/run-sync-get-dataset-items", тело={"chain": ЦЕПЬ})
    finally:
        urllib.request.urlopen = настоящий
    chk("в адресе запроса токена нет",
        "SEKRETNOEZNACHENIE" not in пойманный.get("url", ""), пойманный.get("url"))
    загол = {к.lower(): з for к, з in (пойманный.get("hdr") or {}).items()}
    chk("токен едет заголовком Authorization: Bearer",
        загол.get("authorization") == "Bearer apify_api_SEKRETNOEZNACHENIE123",
        sorted(загол))
    chk("в теле запроса токена нет",
        b"SEKRETNOEZNACHENIE" not in (пойманный.get("тело") or b""),
        пойманный.get("тело"))
    os.environ.pop(ИМЯ_ТОКЕНА, None)

    # --------------------------------------------- 7. запись csv и страницы
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as врем:
        п_csv = Path(врем) / "gmgn_проверка.csv"
        записать_csv(св["строки"], п_csv)
        текст = п_csv.read_text(encoding="utf-8").strip().splitlines()
        chk("заголовок csv -- столбцы в порядке владельца",
            текст[0] == ",".join(с for с, _ in СТОЛБЦЫ), текст[0])
        chk("строк в csv столько же, сколько уникальных кошельков",
            len(текст) - 1 == св["уникальных"], len(текст) - 1)
        chk("первой идёт строка с лучшим местом",
            текст[1].startswith("W1,") or текст[1].startswith("W2,"), текст[1])
        п_стр = записать_страницу(св, Path(врем) / "gmgn.md", csv_путь=п_csv,
                                   столбцы=пустые_столбцы(св["строки"]),
                                   схема={"build": "build-проверка"})
        стр = п_стр.read_text(encoding="utf-8")
        chk("страница: таблица приростов по всем запускам",
            all(x["комбинация"] in стр for x in пр), None)
        chk("страница: итог уникальных и расход",
            f"уникальных кошельков {св['уникальных']}" in стр and " $." in стр, None)
        chk("страница: сборка актора названа", "build-проверка" in стр, None)

    # -------------------------------------- 8. проверка пробного запуска
    ст = пустые_столбцы(св["строки"])
    chk("заполненность столбцов считается по строкам",
        ст["walletAddress"] == 3 and ст["followCount"] == 3, ст)
    ст_пусто = пустые_столбцы([{"walletAddress": "W", "followCount": ""}])
    chk("пустой столбец виден нулём -- на этом пробный запуск и отказывает",
        ст_пусто["followCount"] == 0 and ст_пусто["pnl7d"] == 0, ст_пусто)
    chk("столбцы пробной проверки -- названные владельцем",
        ПРОБНЫЕ_СТОЛБЦЫ == ("walletAddress", "followCount", "buys7d", "winRate7d",
                             "pnl7d", "tags"), ПРОБНЫЕ_СТОЛБЦЫ)

    плохих = [p for p in проверки if not p[1]]
    for что, ок, факт in проверки:
        print(("ok  " if ок else "НЕТ ") + что + ("" if ок else f"  -- {факт!r}"))
    print(f"\nпроверок {len(проверки)}, ждали {ЖДЁМ_ПРОВЕРОК}, не прошло {len(плохих)}")
    if len(проверки) != ЖДЁМ_ПРОВЕРОК:
        print("ЧИСЛО ПРОВЕРОК НЕ СОВПАЛО -- молчаливый пропуск считается провалом")
        return 1
    return 1 if плохих else 0


def _отказ_без_токена() -> bool:
    try:
        токен()
    except ОшибкаВыгрузки as сбой:
        return ИМЯ_ТОКЕНА in str(сбой) and "командную строку" in str(сбой)
    return False


def пересобрать_из_сырых(папка: Path, *, путь_csv: Path) -> dict:
    """CSV заново ИЗ СЫРЫХ ОТВЕТОВ, без сети и без денег.

    Понадобилось потому, что столбцы nickname и twitterUsername вышли пустыми у
    всех 493 кошельков: профиль актор отдаёт вложенным объектом `profile`, а
    разбор смотрел только верхний уровень (см. _достать). Сырые ответы на диске --
    те же самые, что пришли от актора, поэтому пересборка ничего не стоит и ничего
    не выдумывает: это ТОТ ЖЕ разбор, только исправленный.
    """
    из_ = {"ok": False, "why_not": None, "запуски": [], "уникальных": 0}
    if not папка.is_dir():
        из_["why_not"] = f"папки сырых ответов нет: {папка}"
        return из_
    запуски = []
    for файл in sorted(папка.glob("*.json")):
        имя = файл.stem
        try:
            строки = json.loads(файл.read_text(encoding="utf-8"))
        except ValueError as сбой:
            из_["why_not"] = f"{файл.name} не разбирается: {сбой}"
            return из_
        if not isinstance(строки, list):
            continue
        запуски.append({"ok": True, "комбинация": имя, "строки": строки,
                         "why_not": None})
    if not запуски:
        из_["why_not"] = f"в {папка} нет сырых выгрузок"
        return из_
    св = объединить(запуски)
    записать_csv(св["строки"], путь_csv)
    из_.update(ok=True, запуски=[з["комбинация"] for з in запуски],
               уникальных=св["уникальных"], столбцы=пустые_столбцы(св["строки"]),
               строк=len(св["строки"]))
    return из_


def main() -> int:
    п = argparse.ArgumentParser()
    п.add_argument("--proba", action="store_true",
                    help="пробный запуск: 5 кошельков на top_tracked/tracked")
    п.add_argument("--prohod", action="store_true",
                    help="первый проход: 12 комбинаций по 100 кошельков")
    п.add_argument("--max-wallets", type=int, default=СТРОК_НА_ЗАПРОС)
    п.add_argument("--predel-zaprosov", type=int, default=ПРЕДЕЛ_ЗАПРОСОВ)
    п.add_argument("--predel-rashoda", type=float, default=ПРЕДЕЛ_РАСХОДА)
    п.add_argument("--out", default=None, help="куда писать CSV при --peresobrat")
    п.add_argument("--peresobrat", action="store_true",
                    help="пересобрать CSV из сырых ответов, без сети и без денег")
    п.add_argument("--self-test", action="store_true")
    а = п.parse_args()
    if а.self_test:
        return self_test()
    if а.peresobrat:
        # ПИШЕТСЯ В ФАЙЛ ТОГО ЖЕ ПРОХОДА, А НЕ В НОВЫЙ. Данные те же самые --
        # сырые ответы того прохода, -- и плодить вторую выгрузку той же даты
        # значило бы оставить рядом правильную и неправильную. Файлов несколько --
        # отказ по имени: выбирать за владельца, какой переписать, нельзя.
        были = sorted((КОРЕНЬ / "data" / "gmgn").glob("gmgn_*.csv"))
        if len(были) > 1:
            печать(f"СБОЙ: выгрузок несколько ({', '.join(б.name for б in были)}) -- "
                    f"какую переписать, решает владелец; передайте --out")
            return 1
        путь = (были[0] if были else
                КОРЕНЬ / "data" / "gmgn"
                / f"gmgn_{time.strftime('%Y-%m-%d_%HZ', time.gmtime())}.csv")
        if а.out:
            путь = Path(а.out)
        о = пересобрать_из_сырых(КОРЕНЬ / "data" / "gmgn" / "raw", путь_csv=путь)
        if not о["ok"]:
            печать(f"СБОЙ: {о['why_not']}")
            return 1
        печать(f"пересобрано из {len(о['запуски'])} сырых выгрузок: строк "
                f"{о['строк']}, уникальных {о['уникальных']}")
        for с, н in (о.get("столбцы") or {}).items():
            печать(f"  {с}: заполнено {н} из {о['строк']}")
        печать(f"файл: {_под_корнем(путь)}")
        return 0
    if not (а.proba or а.prohod):
        п.print_help()
        return 2
    try:
        схема = схема_актора()
    except ОшибкаВыгрузки as сбой:
        печать(f"СБОЙ: {сбой}")
        return 2
    if not схема.get("ok"):
        печать(f"СБОЙ: схема входа актора не прочитана: {схема.get('why_not')}")
        return 2
    перечисления = сверить_комбинации(схема["polya"], [])["перечисления"]
    печать(f"сборка актора {схема.get('build')}; перечисления схемы: "
            f"{json.dumps(перечисления, ensure_ascii=False)}")
    комб = ([{"traderType": ТИП_ПО_УМОЛЧАНИЮ, "sortBy": СОРТИРОВКА_ПО_УМОЛЧАНИЮ}]
            if а.proba else комбинации_прохода())
    св_сверки = сверить_комбинации(схема["polya"], комб)
    if not св_сверки["ok"]:
        печать(f"СБОЙ: {св_сверки['why_not']}")
        return 2
    макс = 5 if а.proba else int(а.max_wallets)
    дата = time.strftime("%Y-%m-%d", time.gmtime())
    час = time.strftime("%H", time.gmtime())
    raw = КОРЕНЬ / "data" / "gmgn" / ("raw_proba" if а.proba else "raw")
    св = проход(комбинации=комб, максимум=макс, папка_raw=raw,
                 предел_запросов=int(а.predel_zaprosov),
                 предел_расхода=float(а.predel_rashoda))
    столбцы = пустые_столбцы(св["строки"])
    if а.proba:
        печать(f"пробный запуск: строк {len(св['строки'])}, расход {св['расход']} $")
        пусто = [с for с in ПРОБНЫЕ_СТОЛБЦЫ if столбцы.get(с, 0) == 0]
        for с, н in столбцы.items():
            печать(f"  {с}: заполнено {н} из {len(св['строки'])}")
        if пусто:
            печать(f"СБОЙ: столбцы пустые у всех строк: {', '.join(пусто)} -- "
                    f"имена полей актора другие, проход не запускать")
            return 1
        печать("все названные столбцы заполнены -- проход можно запускать")
        return 0
    путь_csv = КОРЕНЬ / "data" / "gmgn" / f"gmgn_{дата}_{час}Z.csv"
    записать_csv(св["строки"], путь_csv)
    путь_стр = записать_страницу(св, КОРЕНЬ / "docs" / f"gmgn_{дата}.md",
                                  csv_путь=путь_csv, столбцы=столбцы, схема=схема)
    печать(f"уникальных {св['уникальных']}, запросов {св['запросов']}, расход "
            f"{св['расход']} $")
    печать(f"файл: {_под_корнем(путь_csv)}")
    печать(f"страница: {_под_корнем(путь_стр)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
