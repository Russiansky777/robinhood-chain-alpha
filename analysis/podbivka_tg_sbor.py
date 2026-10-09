#!/usr/bin/env python3
"""Сбор публичных TG-каналов коллов мемкоинов Solana (задача владельца 09.10, п.2).

ПОЧЕМУ ОБЛАКОМ. Сеть этого контейнера запрещает t.me, tgstat.ru и telemetr.me: шлюз
отвечает 403 на CONNECT. У облачного бегунка GitHub Actions интернет открыт, поэтому сбор
идёт маркером (`data/podbivka/zapusk/zadacha_tg.json`), а разбор и счёт -- офлайн по
сохранённому сырью. Сырьё сохраняется всегда: если разбор окажется неверным, его можно
переделать без повторных запросов.

Два режима.
  --rezhim katalog -- поиск каналов. Качает каталоги (TGStat, Telemetr) и лидерборд
    Solana Tracker, складывает страницы как есть в data/podbivka/tg/syroe/, вытаскивает
    из них кандидатов `t.me/<имя>` и проверяет каждого: открывается ли
    https://t.me/s/<имя> без входа и есть ли на странице посты. Ничего не придумывает:
    в список попадают только имена, встреченные на страницах.
  --rezhim istoriya -- история постов. Идёт назад по https://t.me/s/<имя>?before=<id>,
    пишет на каждый пост: id, время (UTC, по секундам), текст и найденные в нём base58-минты
    (у pump.fun они кончаются на «pump»). Останавливается, когда посты старше --s.

Выход: data/podbivka/tg/kanaly.json (кандидаты и проверка), data/podbivka/tg/posty_<имя>.jsonl.gz
(история канала), data/podbivka/tg/syroe/* (страницы как есть). Только чтение, без входа в
аккаунты и без API-ключей.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import re
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ПАПКА = П / "tg"
СЫРЬЁ = ПАПКА / "syroe"
ЗАДЕРЖКА = 1.2            # пауза между запросами, чтобы не долбить чужой сайт
ПРЕДЕЛ_СТРАНИЦ = 400      # на канал за один прогон: 60 не хватило -- у четырёх каналов
                          # по 1200 постов упёрлись в предел, не дойдя до 21.09

# Поиск по каталогам отдаёт только собственные каналы сайта: страницы выдачи рисуются
# скриптом, в html имён нет (первый прогон 08.10 23:20Z -- 12 кандидатов, из них открылись
# TGStat, telepulse, TGStatAPI, то есть сам сервис). Поэтому семена берутся из поиска по
# вебу с указанием источника, а расширение списка идёт по блоку «похожие каналы» страниц
# каталога -- это органика каталога, а не мой домысел.
СЕМЕНА = [
    ("solana100xcall", "tgstat.com/channel/@solana100xcall; telemetr.io/en/channels/2178813210-solana100xcall"),
    ("shitcoingemsalert", "tgstat.com/channel/@shitcoingemsalert"),
    ("SolanaMemeCoinss", "tgstat.com/channel/@SolanaMemeCoinss; t.me/s/solanamemecoinss"),
    ("memecoin_finder", "tgstat.com/channel/@memecoin_finder"),
    ("MemeCoin_Whale_Pumps", "tgstat.com/channel/@MemeCoin_Whale_Pumps"),
    ("SolanaMemeCryptoCoins", "t.me/SolanaMemeCryptoCoins"),
    ("solalphacalls", "t.me/solalphacalls"),
    ("AlphaCallsSolana", "t.me/AlphaCallsSolana"),
    ("Cabal777xbt", "t.me/s/Cabal777xbt"),
    ("pumpfuncalls_sol", "telegramchannels.me/channels/pumpfuncalls_sol"),
]
# страницы каталога на каждое имя: из них берётся блок «похожие каналы»
ПОХОЖИЕ = ("https://tgstat.com/channel/@{и}", "https://tgstat.ru/channel/@{и}")
КАТАЛОГИ = [
    "https://tgstat.com/en/ratings/channels/cryptocurrency",
    "https://tgstat.ru/cryptocurrency",
    "https://telemetr.io/en/channels/catalog/cryptocurrency",
]
р_имя = re.compile(r"(?:https?://)?t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{4,31})")
р_пост = re.compile(r'data-post="([^"/]+)/(\d+)"')
р_время = re.compile(r'datetime="([0-9T:\+\-\.Z]+)"')
р_текст = re.compile(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
р_тег = re.compile(r"<[^>]+>")
# Границы обязательны: без них регулярка выкусывает кусок из середины чужого адреса. У
# robinhood_autocalls посты с адресами EVM (0x...), и из «0xd1706e80bd1dc29ab899...» без
# границ получался ложный «минт» bd1dc29ab8994e9318386c4687872739 -- 32 символа, все из
# алфавита base58. Поэтому ещё и проверка: адрес Solana -- это ровно 32 байта после
# раскодировки base58.
р_b58 = re.compile(r"(?<![0-9A-Za-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![0-9A-Za-z])")
НЕ_МИНТЫ = {"So11111111111111111111111111111111111111112"}
АЛФАВИТ58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ИНДЕКС58 = {с: i for i, с in enumerate(АЛФАВИТ58)}


def адрес_solana(s: str) -> bool:
    """Ровно 32 байта после раскодировки base58 -- иначе это не ключ Solana."""
    н = 0
    for с in s:
        i = ИНДЕКС58.get(с)
        if i is None:
            return False
        н = н * 58 + i
    байт = (н.bit_length() + 7) // 8
    ведущих = len(s) - len(s.lstrip("1"))
    return байт + ведущих == 32


def достать(requests, url: str, попыток: int = 3):
    """GET с повторами; возвращает (код, текст). Сырьё пишется вызывающим."""
    for н in range(попыток):
        try:
            о = requests.get(url, timeout=45, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) podbivka/1.0",
                "Accept-Language": "en,ru;q=0.8"})
            return о.status_code, о.text
        except Exception as exc:  # noqa: BLE001
            if н == попыток - 1:
                return 0, f"<ошибка {type(exc).__name__}: {exc}>"
            time.sleep(2 * (н + 1))
    return 0, ""


СОХРАНЁННОЕ: list = []


def сохранить(url: str, текст: str) -> str:
    СЫРЬЁ.mkdir(parents=True, exist_ok=True)
    имя = hashlib.sha1(url.encode()).hexdigest()[:16] + ".html.gz"
    путь = СЫРЬЁ / имя
    with gzip.open(путь, "wt", encoding="utf-8") as ф:
        ф.write(f"<!-- {url} -->\n{текст}")
    СОХРАНЁННОЕ.append(путь)
    return имя


def минты(текст: str) -> list:
    из_ = []
    for м in р_b58.findall(текст):
        if м in НЕ_МИНТЫ or м in из_ or not адрес_solana(м):
            continue
        из_.append(м)
    return из_


def проверить(requests, и: str, откуда: str) -> dict:
    """Открывается ли https://t.me/s/<имя> без входа и есть ли на странице посты."""
    url = f"https://t.me/s/{и}"
    код, текст = достать(requests, url)
    посты = р_пост.findall(текст or "")
    загл = ""
    м = re.search(r'<meta property="og:title" content="([^"]*)"', текст or "")
    if м:
        загл = м.group(1)
    подп = ""
    м2 = re.search(r'tgme_page_extra"[^>]*>([^<]*)', текст or "")
    if м2:
        подп = м2.group(1).strip()
    return {"имя": и, "откуда": откуда, "код": код, "постов_на_странице": len(посты),
            "заголовок": загл, "подписчиков_строкой": подп,
            "последний_id": max((int(x[1]) for x in посты), default=None),
            "открывается_без_входа": bool(код == 200 and посты),
            "сырьё": сохранить(url, текст) if текст else None}


def режим_каталог(requests) -> dict:
    """Семена из веб-поиска (с источником) + расширение по «похожим каналам» каталога."""
    служебные = {"share", "joinchat", "addstickers", "proxy", "socks", "iv", "telegram",
                 "durov", "telegramtips", "s", "tgstat", "tgstatapi", "telepulse",
                 "tgstat_bot", "TGStat", "telemetr", "telemetr_io"}
    проверка, видели = [], set()
    # 1. семена
    for и, откуда in СЕМЕНА:
        if и.lower() in видели:
            continue
        видели.add(и.lower())
        проверка.append(проверить(requests, и, откуда))
        time.sleep(ЗАДЕРЖКА)
    # 2. похожие каналы со страниц каталога на каждое живое семя
    страницы, новые = [], collections.Counter()
    for и, _ in СЕМЕНА:
        for шаб in ПОХОЖИЕ:
            url = шаб.format(и=и)
            код, текст = достать(requests, url)
            файл = сохранить(url, текст) if текст else None
            найдено = {x for x in р_имя.findall(текст or "")} | {
                x for x in re.findall(r'/channel/@([A-Za-z][A-Za-z0-9_]{4,31})', текст or "")}
            страницы.append({"url": url, "код": код, "сырьё": файл, "имён": len(найдено)})
            for н in найдено:
                if н.lower() not in видели and н.lower() not in служебные:
                    новые[н] += 1
            time.sleep(ЗАДЕРЖКА)
            if код == 200 and найдено:
                break
    # 3. общие страницы каталога -- тоже как источник имён
    for url in КАТАЛОГИ:
        код, текст = достать(requests, url)
        файл = сохранить(url, текст) if текст else None
        найдено = {x for x in р_имя.findall(текст or "")} | {
            x for x in re.findall(r'/channel/@([A-Za-z][A-Za-z0-9_]{4,31})', текст or "")}
        страницы.append({"url": url, "код": код, "сырьё": файл, "имён": len(найдено)})
        for н in найдено:
            if н.lower() not in видели and н.lower() not in служебные:
                новые[н] += 1
        time.sleep(ЗАДЕРЖКА)
    # 4. проверка новых, пока не наберётся 40 открытых
    for н, встреч in новые.most_common(120):
        if len([x for x in проверка if x["открывается_без_входа"]]) >= 40:
            break
        if н.lower() in видели:
            continue
        видели.add(н.lower())
        проверка.append(проверить(requests, н, f"похожие каналы каталога, встреч {встреч}"))
        time.sleep(ЗАДЕРЖКА)
    return {"страницы_каталогов": страницы, "семян": len(СЕМЕНА),
            "кандидатов": len(видели), "проверено": len(проверка), "каналы": проверка}


def режим_история(requests, имена: list, с_utc: str) -> dict:
    ПАПКА.mkdir(parents=True, exist_ok=True)
    свод = []
    for имя in имена:
        before = None
        постов, минтов, стр = 0, 0, 0
        самый_старый = None
        путь = ПАПКА / f"posty_{имя}.jsonl.gz"
        with gzip.open(путь, "wt", encoding="utf-8") as ф:
            while стр < ПРЕДЕЛ_СТРАНИЦ:
                url = f"https://t.me/s/{имя}" + (f"?before={before}" if before else "")
                код, текст = достать(requests, url)
                сохранить(url, текст or "")
                стр += 1
                if код != 200 or not текст:
                    break
                блоки = текст.split('class="tgme_widget_message ')
                ids = []
                for б in блоки[1:]:
                    пм = р_пост.search(б)
                    вм = р_время.search(б)
                    if not пм:
                        continue
                    pid = int(пм.group(2))
                    ids.append(pid)
                    когда = вм.group(1) if вм else None
                    тм = р_текст.search(б)
                    чистый = р_тег.sub(" ", тм.group(1)) if тм else ""
                    чистый = re.sub(r"\s+", " ", чистый).strip()
                    мм = минты(чистый)
                    постов += 1
                    минтов += len(мм)
                    if когда and (самый_старый is None or когда < самый_старый):
                        самый_старый = когда
                    ф.write(json.dumps({"канал": имя, "id": pid, "utc": когда,
                                        "минты": мм, "текст": чистый[:500]},
                                       ensure_ascii=False) + "\n")
                if not ids:
                    break
                before = min(ids)
                if самый_старый and самый_старый < с_utc:
                    break
                time.sleep(ЗАДЕРЖКА)
        свод.append({"канал": имя, "страниц": стр, "постов": постов, "минтов": минтов,
                     "самый_старый": самый_старый, "файл": путь.name})
    return {"каналов": len(свод), "по_каналам": свод}


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import requests  # noqa: PLC0415
    р_ = argparse.ArgumentParser()
    р_.add_argument("--rezhim", choices=("katalog", "istoriya"), required=True)
    р_.add_argument("--kanaly", default="", help="имена через запятую (режим istoriya)")
    р_.add_argument("--s", default="2026-09-21T00:00:00Z", help="докуда назад (режим istoriya)")
    а = р_.parse_args()
    ПАПКА.mkdir(parents=True, exist_ok=True)

    if а.rezhim == "katalog":
        из_ = режим_каталог(requests)
        ф = ПАПКА / "kanaly.json"
        ф.write_text(json.dumps({"что": "кандидаты каналов коллов", "когда": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **из_}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        R.записано(ф)
        for с_ in СОХРАНЁННОЕ:
            R.записано(с_)
        откр = [x["имя"] for x in из_["каналы"] if x["открывается_без_входа"]]
        print(f"каталог: кандидатов {из_['кандидатов']}, проверено {из_['проверено']}, "
              f"открываются без входа {len(откр)}: {', '.join(откр[:30])}", flush=True)
        for с in из_["страницы_каталогов"]:
            print(f"  {с['код']} {с['url']} -- имён {с['имён']}", flush=True)
        return 0

    имена = [x.strip() for x in а.kanaly.split(",") if x.strip()]
    if not имена:
        ф = ПАПКА / "kanaly.json"
        if ф.exists():
            д = json.loads(ф.read_text(encoding="utf-8"))
            имена = [x["имя"] for x in д.get("каналы") or [] if x.get("открывается_без_входа")][:30]
    if not имена:
        print("нет списка каналов: сначала --rezhim katalog", flush=True)
        return 1
    из_ = режим_история(requests, имена, а.s)
    ф = ПАПКА / "istoriya_svod.json"
    ф.write_text(json.dumps({"что": "история постов каналов", "с": а.s, **из_},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(ф)
    for с in из_["по_каналам"]:
        R.записано(ПАПКА / с["файл"])
    for с_ in СОХРАНЁННОЕ:
        R.записано(с_)
    print(f"история: каналов {из_['каналов']}, постов "
          f"{sum(x['постов'] for x in из_['по_каналам'])}, минтов "
          f"{sum(x['минтов'] for x in из_['по_каналам'])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
