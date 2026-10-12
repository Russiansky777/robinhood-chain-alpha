#!/usr/bin/env python3
"""Регистратор трендингов: снимки публичных списков, по строке на снимок.

Что пишет. Для каждого источника -- одна строка JSON на снимок в
data/trending/<источник>/<YYYY-MM-DD>.jsonl.gz. В строке: метка UTC, источник, список,
URL, код ответа, время ответа, сколько элементов отдано, СЫРОЕ тело как оно пришло
(`тело`), и рядом разбор `строки` -- ранг, адрес минта/пары, объём, ликвидность теми же
именами полей, какие отдаёт источник. Сырое не выбрасывается никогда: разбор может
отстать от смены формата, а снимок должен остаться годным.

Ничего не "причёсывается": отказ пишется такой же строкой с кодом и причиной, пустой
список -- тоже снимок. Кошельковых ключей нет и не нужно: все запросы -- публичные GET.
Единственный ключ, какой может понадобиться, -- бесплатный Birdeye из переменной
окружения BIRDEYE_API_KEY; без него источник birdeye помечается «нет ключа» и не
опрашивается.

Частоты (флаг --kazhdye): «минута» -- Jupiter (toptrending и toptraded, 5m и 1h),
GeckoTerminal (trending_pools, new_pools), DexScreener (token-boosts latest и top,
token-profiles latest), GMGN (trending, new pairs), pump.fun (KOTH, currently-live);
«пять» -- CoinGecko search/trending, CoinMarketCap spotlight, Birdeye trending.

Запускается таймером systemd на lab-miami (установка -- docs/podbivka_trending_ustanovka.md).
Хост я не трогаю: ставит Code-1, скрипт лежит в ветке.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПАПКА = КОРЕНЬ / "data" / "trending"
ТАЙМАУТ = 20
АГЕНТ = "podbivka-trending/1.0 (+chain analytics, read-only)"

# (источник, список, частота, url, где_массив)
ИСТОЧНИКИ = (
    ("jupiter", "toptrending_5m", "минута",
     "https://lite-api.jup.ag/tokens/v2/toptrending/5m?limit=100", None),
    ("jupiter", "toptrending_1h", "минута",
     "https://lite-api.jup.ag/tokens/v2/toptrending/1h?limit=100", None),
    ("jupiter", "toptraded_5m", "минута",
     "https://lite-api.jup.ag/tokens/v2/toptraded/5m?limit=100", None),
    ("jupiter", "toptraded_1h", "минута",
     "https://lite-api.jup.ag/tokens/v2/toptraded/1h?limit=100", None),
    ("geckoterminal", "trending_pools", "минута",
     "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
     "?include=base_token&page=1", "data"),
    ("geckoterminal", "new_pools", "минута",
     "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
     "?include=base_token&page=1", "data"),
    ("dexscreener", "token_boosts_latest", "минута",
     "https://api.dexscreener.com/token-boosts/latest/v1", None),
    ("dexscreener", "token_boosts_top", "минута",
     "https://api.dexscreener.com/token-boosts/top/v1", None),
    ("dexscreener", "token_profiles_latest", "минута",
     "https://api.dexscreener.com/token-profiles/latest/v1", None),
    ("gmgn", "trending_5m", "минута",
     "https://gmgn.ai/defi/quotation/v1/rank/sol/swaps/5m"
     "?orderby=swaps&direction=desc", "data"),
    ("gmgn", "new_pairs", "минута",
     "https://gmgn.ai/defi/quotation/v1/pairs/sol/new_pairs?limit=50", "data"),
    ("pumpfun", "koth", "минута",
     "https://frontend-api-v3.pump.fun/coins/king-of-the-hill?includeNsfw=false", None),
    ("pumpfun", "currently_live", "минута",
     "https://frontend-api-v3.pump.fun/coins/currently-live"
     "?offset=0&limit=50&includeNsfw=false", None),
    ("coingecko", "search_trending", "пять",
     "https://api.coingecko.com/api/v3/search/trending", "coins"),
    ("coinmarketcap", "spotlight", "пять",
     "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/spotlight"
     "?dataType=4&limit=30", None),
    ("birdeye", "token_trending", "пять",
     "https://public-api.birdeye.so/defi/token_trending"
     "?sort_by=rank&sort_type=asc&offset=0&limit=20", "data"),
)

# Имена полей, какими источники зовут ранг, адрес, объём и ликвидность. Берём первое
# встретившееся -- и кладём рядом с сырым, не вместо него.
ПОЛЯ_РАНГА = ("rank", "trendingRank", "trending_rank", "position", "index")
ПОЛЯ_АДРЕСА = ("id", "address", "mint", "tokenAddress", "token_address",
               "baseTokenAddress", "pairAddress", "pair_address", "mint_address")
ПОЛЯ_ОБЪЁМА = ("volume24h", "volume_usd", "volumeUsd", "volume", "v24hUSD",
               "volume_24h", "volume24hUSD", "swaps", "txns24h")
ПОЛЯ_ЛИКВ = ("liquidity", "liquidityUsd", "liquidity_usd", "reserve_in_usd",
             "reserveUsd", "liquidityUSD", "pool_size")


def достать(d: dict, имена: tuple):
    """Первое поле из `имена`, какое есть у элемента -- с его собственным именем."""
    if not isinstance(d, dict):
        return None, None
    for к in имена:
        if к in d and d[к] is not None:
            return к, d[к]
    атр = d.get("attributes")
    if isinstance(атр, dict):
        for к in имена:
            if к in атр and атр[к] is not None:
                return f"attributes.{к}", атр[к]
    return None, None


def разбор(тело, где) -> list:
    """Ранг, адрес, объём, ликвидность -- именами источника; порядок = ранг, если его нет."""
    массив = тело
    if где and isinstance(тело, dict):
        массив = тело.get(где)
    if isinstance(массив, dict):
        массив = (массив.get("data") or массив.get("items")
                  or массив.get("tokens") or массив.get("coins") or [])
    if not isinstance(массив, list):
        return []
    из_ = []
    for н, э in enumerate(массив, 1):
        если = э.get("item") if isinstance(э, dict) and "item" in э else э
        имя_р, ранг = достать(если, ПОЛЯ_РАНГА)
        имя_а, адрес = достать(если, ПОЛЯ_АДРЕСА)
        имя_о, объём = достать(если, ПОЛЯ_ОБЪЁМА)
        имя_л, ликв = достать(если, ПОЛЯ_ЛИКВ)
        из_.append({"по_порядку": н,
                    "ранг": ранг, "ранг_поле": имя_р,
                    "адрес": адрес, "адрес_поле": имя_а,
                    "объём": объём, "объём_поле": имя_о,
                    "ликвидность": ликв, "ликвидность_поле": имя_л})
    return из_


def снимок(источник: str, список_: str, url: str, где) -> dict:
    зап = urllib.request.Request(url, headers={
        "accept": "application/json", "user-agent": АГЕНТ})
    if источник == "birdeye":
        ключ = os.environ.get("BIRDEYE_API_KEY")
        if not ключ:
            return {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "источник": источник, "список": список_, "url": url,
                    "код": None, "ошибка": "нет ключа BIRDEYE_API_KEY",
                    "элементов": None, "тело": None, "строки": []}
        зап.add_header("X-API-KEY", ключ)
        зап.add_header("x-chain", "solana")
    нач = time.time()
    стр = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "источник": источник, "список": список_, "url": url}
    try:
        with urllib.request.urlopen(зап, timeout=ТАЙМАУТ) as о:
            сырое = о.read()
            стр["код"] = о.status
            стр["заголовки"] = {к.lower(): v for к, v in о.headers.items()
                                if к.lower() in ("content-type", "x-ratelimit-remaining",
                                                 "retry-after", "cf-ray", "server")}
    except urllib.error.HTTPError as e:
        сырое = e.read()[:20000]
        стр["код"] = e.code
        стр["ошибка"] = f"http {e.code}"
    except Exception as e:  # noqa: BLE001
        сырое, стр["код"], стр["ошибка"] = b"", None, f"{type(e).__name__}: {e}"[:200]
    стр["секунд"] = round(time.time() - нач, 3)
    стр["байт"] = len(сырое)
    тело = None
    if сырое:
        try:
            тело = json.loads(сырое.decode("utf-8", "replace"))
        except ValueError:
            тело = {"не_json": сырое[:4000].decode("utf-8", "replace")}
    стр["тело"] = тело
    стр["строки"] = разбор(тело, где) if тело is not None else []
    стр["элементов"] = len(стр["строки"])
    return стр


def записать(стр: dict) -> Path:
    д = стр["utc"][:10]
    п = ПАПКА / стр["источник"]
    п.mkdir(parents=True, exist_ok=True)
    ф = п / f"{д}.jsonl.gz"
    with gzip.open(ф, "at", encoding="utf-8") as о:   # дописываем, снимок = строка
        о.write(json.dumps(стр, ensure_ascii=False) + "\n")
    return ф


def свод() -> int:
    всего = 0
    for п in sorted(ПАПКА.glob("*")):
        if not п.is_dir():
            continue
        for ф in sorted(п.glob("*.jsonl.gz")):
            n = sum(1 for _ in gzip.open(ф, "rt", encoding="utf-8"))
            мб = ф.stat().st_size / 1e6
            всего += ф.stat().st_size
            print(f"{п.name:16} {ф.stem} снимков {n:5d} файл {мб:7.2f} МБ", flush=True)
    print(f"всего {всего / 1e6:.2f} МБ", flush=True)
    return 0


def главное(а) -> int:
    if а.svodka:
        return свод()
    что = [x for x in ИСТОЧНИКИ
           if (а.kazhdye == "все" or x[2] == а.kazhdye)
           and (not а.istochnik or x[0] == а.istochnik)]
    итог = []
    for источник, список_, _, url, где in что:
        стр = снимок(источник, список_, url, где)
        if not а.probno:
            записать(стр)
        итог.append(стр)
        print(f"{источник}/{список_}: код {стр.get('код')} элементов "
              f"{стр.get('элементов')} байт {стр.get('байт')} "
              f"{стр.get('ошибка') or ''}".rstrip(), flush=True)
        time.sleep(а.pauza)
    ок = sum(1 for с in итог if с.get("код") == 200 and с.get("элементов"))
    print(f"итого: отдали список {ок} из {len(итог)}", flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--kazhdye", default="минута", choices=("минута", "пять", "все"))
    р.add_argument("--istochnik", default="", help="только этот источник")
    р.add_argument("--pauza", type=float, default=0.4,
                   help="пауза между запросами, секунд (бережём лимиты)")
    р.add_argument("--probno", action="store_true", help="не писать файлы, только показать")
    р.add_argument("--svodka", action="store_true", help="сколько снимков и места по источникам")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
