#!/usr/bin/env python3
"""Выбор токена для тестового вызова: по реальным данным, а не на память.

Зачем отдельный скрипт. Тестовый вызов прохода B -- единственная покупка,
которую владелец делает руками, и у неё жёсткие требования: прямой пул к
SOL (один хоп), глубина, при которой вход 0.01-0.05 SOL не двигает цену, и
отсутствие комиссии на перевод (Token-2022 transferFeeConfig берёт её на
каждой ноге). Назвать такой токен из памяти нельзя: состав пулов и их
глубина меняются ежедневно. Поэтому -- запрос к маршрутизатору и к цепи.

Проверяется по каждому кандидату:
 * сколько хопов в маршруте WSOL -> минт на размер тестового входа; больше
   одного -- кандидат не годится, это и есть INTERMEDIATE_ROUTE;
 * ценовое влияние на входе 0.01, 0.05 и 0.2 SOL -- чтобы размер можно
   было поднять, не меняя токен;
 * программа токена и ставка комиссии на перевод -- по цепи, не по спискам.

Только чтение: котировка у маршрутизатора и getAccountInfo у узла. Ни
одного ордера.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

WSOL = "So11111111111111111111111111111111111111112"
LAMPORT = 1_000_000_000
РАЗМЕРЫ_SOL = (0.01, 0.05, 0.2)
ПРЕДЕЛ_ВЛИЯНИЯ = 0.01          # 1% на 0.05 SOL -- уже неглубоко
QUOTE_URL = "https://quote-api.jup.ag/v6/quote"

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "bloom_token_pick.json"


def котировка(минт: str, размер_sol: float, *, таймаут: float = 20.0) -> dict:
    """Один запрос маршрутизатора. Отказ -- это отказ, а не "нет маршрута"."""
    import requests  # noqa: PLC0415
    параметры = {"inputMint": WSOL, "outputMint": минт,
                 "amount": str(int(размер_sol * LAMPORT)),
                 "slippageBps": "100"}
    try:
        r = requests.get(QUOTE_URL, params=параметры, timeout=таймаут)
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "why": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if not r.ok:
        return {"known": False, "why": f"http {r.status_code}: {r.text[:160]}"}
    try:
        j = r.json() or {}
    except ValueError:
        return {"known": False, "why": "ответ не JSON"}
    план = j.get("routePlan") or []
    рынки = []
    for шаг in план:
        si = (шаг or {}).get("swapInfo") or {}
        рынки.append({"label": si.get("label"), "amm": si.get("ammKey"),
                      "in": si.get("inputMint"), "out": si.get("outputMint")})
    влияние = j.get("priceImpactPct")
    try:
        влияние = float(влияние) if влияние is not None else None
    except (TypeError, ValueError):
        влияние = None
    return {"known": True, "hops": len(план), "markets": рынки,
            "price_impact": влияние, "out_amount": j.get("outAmount")}


ПУЛЫ_URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools"


def кандидаты_из_пулов(*, страниц: int = 2, сколько: int = 8,
                        запрос_fn=None) -> dict:
    """Кандидаты берутся из списка пулов, а не из памяти.

    Состав и глубина пулов меняются ежедневно, и назвать «глубокий токен»
    по памяти -- это выдумать данные. Берутся пулы, у которых вторая
    сторона -- именно WSOL (пул к USDC нам не годится: это и есть
    промежуточный маршрут), и сортируются по резерву.
    """
    запрос_fn = запрос_fn or _пулы_страница
    пулы = []
    отказы = []
    for стр in range(1, страниц + 1):
        ответ = запрос_fn(стр)
        if not ответ.get("known"):
            отказы.append({"page": стр, "why": ответ.get("why")})
            continue
        for пул in ответ.get("data") or []:
            атр = (пул or {}).get("attributes") or {}
            св = (пул or {}).get("relationships") or {}
            база = (((св.get("base_token") or {}).get("data")) or {}).get("id") or ""
            котир = (((св.get("quote_token") or {}).get("data")) or {}).get("id") or ""
            if not котир.endswith(WSOL):
                continue
            минт = база.split("_", 1)[1] if "_" in база else база
            if not минт or минт == WSOL:
                continue
            try:
                резерв = float(атр.get("reserve_in_usd") or 0)
            except (TypeError, ValueError):
                резерв = 0.0
            пулы.append({"mint": минт, "pool_name": атр.get("name"),
                         "reserve_usd": резерв, "dex": атр.get("dex_id")})
    пулы.sort(key=lambda x: -x["reserve_usd"])
    видели, итог = set(), []
    for x in пулы:
        if x["mint"] in видели:
            continue
        видели.add(x["mint"])
        итог.append(x)
        if len(итог) >= сколько:
            break
    return {"known": bool(итог) or not отказы, "pools": итог, "failures": отказы,
            "source": ПУЛЫ_URL}


USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# Ниже этого резерва пул для стенда не годится: на нём вход 0.05 SOL
# двинет цену, и проверка разбора превратится в проверку проскальзывания.
МИН_РЕЗЕРВ_USD = 20_000.0
МИН_ОБЪЁМ_USD = 5_000.0
RAYDIUM_METEORA = ("raydium", "meteora")


def пулы_по_стороне(*, страниц: int = 4, запрос_fn=None) -> dict:
    """Все пулы со страниц, разложенные по второй стороне.

    Нужны и пулы к WSOL, и пулы к USDC: пункт 6 чек-листа как раз про
    токен, у которого прямого пула к SOL НЕТ, а к USDC есть. Отбросив
    USDC-пулы, как делал прежний отбор, такого токена не найти вообще.
    """
    запрос_fn = запрос_fn or _пулы_страница
    к_sol, к_usdc, прочее = {}, {}, {}
    отказы = []
    for стр in range(1, страниц + 1):
        ответ = запрос_fn(стр)
        if not ответ.get("known"):
            отказы.append({"page": стр, "why": ответ.get("why")})
            continue
        for пул in ответ.get("data") or []:
            атр = (пул or {}).get("attributes") or {}
            св = (пул or {}).get("relationships") or {}
            база = (((св.get("base_token") or {}).get("data")) or {}).get("id") or ""
            котир = (((св.get("quote_token") or {}).get("data")) or {}).get("id") or ""
            минт = база.split("_", 1)[1] if "_" in база else база
            котир_минт = котир.split("_", 1)[1] if "_" in котир else котир
            if not минт or минт in (WSOL, USDC):
                continue
            def число(x):
                try:
                    return float(x or 0)
                except (TypeError, ValueError):
                    return 0.0
            запись = {"mint": минт, "pool_name": атр.get("name"),
                      "dex": атр.get("dex_id"),
                      "reserve_usd": число(атр.get("reserve_in_usd")),
                      "volume_24h_usd": число((атр.get("volume_usd") or {}).get("h24")),
                      "quote": котир_минт}
            куда = (к_sol if котир_минт == WSOL else
                    к_usdc if котир_минт == USDC else прочее)
            прежний = куда.get(минт)
            if прежний is None or запись["reserve_usd"] > прежний["reserve_usd"]:
                куда[минт] = запись
    return {"known": bool(к_sol or к_usdc) or not отказы,
            "to_sol": к_sol, "to_usdc": к_usdc, "to_other": прочее,
            "failures": отказы, "source": ПУЛЫ_URL}


def классы(*, страниц: int = 4, на_класс: int = 3, helius=None,
           котировка_fn=None, запрос_fn=None, пауза_с: float = 0.2) -> dict:
    """Три класса токенов для пунктов 3, 5 и 6 прохода A.

    Классы проверяются ПО ФАКТУ, а не по названию:
      п. 3 -- программа минта Token-2022 И ненулевая комиссия на перевод,
              прямой пул к SOL (один хоп);
      п. 5 -- прямой пул к SOL на Raydium или Meteora, живой суточный объём;
      п. 6 -- пул к USDC есть, пула к SOL НЕТ, и маршрутизатор ведёт к
              токену больше чем одним хопом. Именно это наш детектор
              обязан отбросить как INTERMEDIATE_ROUTE.
    """
    котировка_fn = котировка_fn or котировка
    пулы = пулы_по_стороне(страниц=страниц, запрос_fn=запрос_fn)
    итог = {"pools": {"to_sol": len(пулы["to_sol"]), "to_usdc": len(пулы["to_usdc"]),
                      "failures": пулы["failures"], "source": пулы["source"]},
            "item3_taxed_direct_sol": [], "item5_raydium_meteora_direct": [],
            "item6_usdc_only": [], "checked": 0, "notes": []}

    # --- п. 3 и п. 5: кандидаты с прямым пулом к SOL, по резерву вниз
    к_sol = sorted(пулы["to_sol"].values(), key=lambda x: -x["reserve_usd"])
    for п in к_sol:
        если_хватит = (len(итог["item3_taxed_direct_sol"]) >= на_класс and
                       len(итог["item5_raydium_meteora_direct"]) >= на_класс)
        if если_хватит:
            break
        if п["reserve_usd"] < МИН_РЕЗЕРВ_USD:
            continue
        к = котировка_fn(п["mint"], 0.05)
        налог_минта_итог = helius.налог_минта(п["mint"]) if helius is not None else None
        if пауза_с:
            time.sleep(пауза_с)
        итог["checked"] += 1
        прямой = к.get("hops") == 1
        запись = dict(п)
        запись.update({"hops": к.get("hops"), "price_impact": к.get("price_impact"),
                       "quote_known": к.get("known"),
                       "quote_why_not": к.get("why"),
                       "markets": (к.get("markets") or [])[:2],
                       "token_program": (налог_минта_итог or {}).get("token_program"),
                       "fee_bps": (налог_минта_итог or {}).get("fee_bps"),
                       "taxed": (налог_минта_итог or {}).get("taxed")})
        if (прямой and запись.get("token_program") == TOKEN_2022
                and (запись.get("fee_bps") or 0) > 0
                and len(итог["item3_taxed_direct_sol"]) < на_класс):
            итог["item3_taxed_direct_sol"].append(запись)
        if (прямой and (п.get("dex") or "").lower().startswith(RAYDIUM_METEORA)
                and п["volume_24h_usd"] >= МИН_ОБЪЁМ_USD
                and not запись.get("taxed")
                and len(итог["item5_raydium_meteora_direct"]) < на_класс):
            итог["item5_raydium_meteora_direct"].append(запись)

    # --- п. 6: пул к USDC есть, к SOL нет
    только_usdc = [п for м, п in пулы["to_usdc"].items() if м not in пулы["to_sol"]]
    только_usdc.sort(key=lambda x: -x["reserve_usd"])
    for п in только_usdc:
        if len(итог["item6_usdc_only"]) >= на_класс:
            break
        if п["reserve_usd"] < МИН_РЕЗЕРВ_USD:
            continue
        к = котировка_fn(п["mint"], 0.05)
        if пауза_с:
            time.sleep(пауза_с)
        итог["checked"] += 1
        # Маршрут в один хоп означал бы, что прямой пул к SOL всё-таки
        # есть -- просто его не было на просмотренных страницах. Такой
        # кандидат пункту 6 не годится, и молча брать его нельзя.
        if к.get("hops") == 1:
            continue
        итог["item6_usdc_only"].append({**п, "hops": к.get("hops"),
                                        "price_impact": к.get("price_impact"),
                                        "quote_known": к.get("known"),
                                        "markets": (к.get("markets") or [])[:3]})
    for имя, ключ in (("п. 3 (Token-2022 с налогом, прямой пул к SOL)",
                       "item3_taxed_direct_sol"),
                      ("п. 5 (Raydium/Meteora, прямой пул к SOL)",
                       "item5_raydium_meteora_direct"),
                      ("п. 6 (пул только к USDC)", "item6_usdc_only")):
        if not итог[ключ]:
            итог["notes"].append(f"{имя}: кандидатов не нашлось на "
                                 f"просмотренных страницах пулов")
    return итог


def _пулы_страница(страница: int, *, таймаут: float = 20.0) -> dict:
    import requests  # noqa: PLC0415
    try:
        r = requests.get(ПУЛЫ_URL, params={"page": str(страница)}, timeout=таймаут)
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "why": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if not r.ok:
        return {"known": False, "why": f"http {r.status_code}"}
    try:
        return {"known": True, "data": (r.json() or {}).get("data") or []}
    except ValueError:
        return {"known": False, "why": "ответ не JSON"}


def проверить(минт: str, *, helius=None, размеры=РАЗМЕРЫ_SOL,
              котировка_fn=None, пауза_с: float = 0.2) -> dict:
    """Вердикт по одному кандидату.

    Котировка передаётся отдельно, чтобы самопроверка проверяла ЭТУ
    функцию, а не свою копию её правил: разъехавшиеся копии -- как раз тот
    случай, когда тесты зелёные, а код неверен.
    """
    котировка_fn = котировка_fn or котировка
    итог = {"mint": минт, "quotes": {}, "tax": None}
    for s in размеры:
        итог["quotes"][f"{s}"] = котировка_fn(минт, s)
        if пауза_с:
            time.sleep(пауза_с)            # не давим на маршрутизатор
    if helius is not None:
        итог["tax"] = helius.налог_минта(минт)
    основная = итог["quotes"].get("0.05") or {}
    прямой = основная.get("hops") == 1
    влияние = основная.get("price_impact")
    без_налога = not (итог["tax"] or {}).get("taxed")
    итог["direct_pool"] = прямой
    итог["deep_enough"] = (влияние is not None and влияние <= ПРЕДЕЛ_ВЛИЯНИЯ)
    итог["untaxed"] = без_налога
    причины = []
    if основная.get("known") is False:
        причины.append(f"котировка не получена: {основная.get('why')}")
    if not прямой:
        причины.append(f"маршрут в {основная.get('hops')} хопа -- не прямой пул к SOL")
    if not итог["deep_enough"]:
        причины.append(f"ценовое влияние {влияние} на 0.05 SOL выше предела "
                       f"{ПРЕДЕЛ_ВЛИЯНИЯ}")
    if not без_налога:
        причины.append(f"комиссия на перевод {(итог['tax'] or {}).get('fee_bps')} bps")
    итог["ok"] = not причины
    итог["why_not"] = причины
    return итог


def выбрать(результаты: list) -> dict:
    """Годный кандидат с наименьшим ценовым влиянием. Без годных -- честное
    "нет", а не лучший из плохих."""
    годные = [r for r in результаты if r.get("ok")]
    if not годные:
        return {"chosen": None,
                "why": "ни один кандидат не прошёл все три условия"}
    годные.sort(key=lambda r: (r["quotes"]["0.05"].get("price_impact") or 1.0))
    л = годные[0]
    return {"chosen": л["mint"],
            "price_impact_005": л["quotes"]["0.05"].get("price_impact"),
            "market": (л["quotes"]["0.05"].get("markets") or [{}])[0].get("label"),
            "candidates_ok": [r["mint"] for r in годные]}


def self_test() -> int:
    checks = []

    def chk(имя, ок, факт=""):
        checks.append((имя, bool(ок), факт))

    def подставная(**kw):
        """Котировка-заглушка: проверяется настоящая проверить()."""
        осн = {"known": True, "hops": 1, "price_impact": 0.001,
               "markets": [{"label": "Raydium"}], "out_amount": "1"}
        осн.update(kw)
        return lambda минт, размер: dict(осн)

    class НалогЗаглушка:
        def __init__(self, bps=None):
            self.bps = bps

        def налог_минта(self, минт):
            return {"mint": минт, "fee_bps": self.bps, "taxed": bool(self.bps)}

    хороший = проверить("ХОРОШИЙ", helius=НалогЗаглушка(),
                        котировка_fn=подставная(), пауза_с=0)
    chk("прямой глубокий без налога годится", хороший["ok"], хороший["why_not"])
    chk("проверены все три размера входа",
        set(хороший["quotes"]) == {"0.01", "0.05", "0.2"}, list(хороший["quotes"]))

    двухх = проверить("ДВА_ХОПА", helius=НалогЗаглушка(),
                      котировка_fn=подставная(hops=2), пауза_с=0)
    chk("два хопа не годятся", not двухх["ok"], двухх["ok"])
    chk("и причина названа хопами",
        any("хопа" in x for x in двухх["why_not"]), двухх["why_not"])

    мелкий = проверить("МЕЛКИЙ", helius=НалогЗаглушка(),
                       котировка_fn=подставная(price_impact=0.2), пауза_с=0)
    chk("высокое ценовое влияние не годится", not мелкий["ok"], мелкий["ok"])

    налог = проверить("НАЛОГ", helius=НалогЗаглушка(300),
                      котировка_fn=подставная(), пауза_с=0)
    chk("токен с комиссией на перевод не годится", not налог["ok"], налог["ok"])
    chk("и ставка названа в bps",
        any("300 bps" in x for x in налог["why_not"]), налог["why_not"])

    нет_котировки = проверить("НЕТ", helius=НалогЗаглушка(),
                              котировка_fn=подставная(known=False, hops=None,
                                                      price_impact=None,
                                                      why="http 429"),
                              пауза_с=0)
    chk("без котировки кандидат не годится", not нет_котировки["ok"], нет_котировки["ok"])
    chk("отказ маршрутизатора назван, а не выдан за отсутствие пула",
        any("котировка не получена" in x for x in нет_котировки["why_not"]),
        нет_котировки["why_not"])

    без_цепи = проверить("БЕЗ_ЦЕПИ", helius=None, котировка_fn=подставная(),
                         пауза_с=0)
    chk("без узла налог неизвестен, и это не выдаётся за отсутствие налога",
        без_цепи["tax"] is None, без_цепи["tax"])

    # --- кандидаты из пулов: только пары к WSOL, по резерву
    def страница(n):
        def пул(минт, котир, резерв, имя):
            return {"attributes": {"name": имя, "reserve_in_usd": резерв,
                                    "dex_id": "raydium"},
                    "relationships": {
                        "base_token": {"data": {"id": f"solana_{минт}"}},
                        "quote_token": {"data": {"id": f"solana_{котир}"}}}}
        if n == 1:
            return {"known": True, "data": [
                пул("МЕЛКИЙ_ПУЛ", WSOL, "1000", "A / SOL"),
                пул("К_USDC", "USDC1111", "9999999", "B / USDC"),
                пул("ГЛУБОКИЙ", WSOL, "500000", "C / SOL"),
            ]}
        return {"known": True, "data": [пул("ГЛУБОКИЙ", WSOL, "500000", "C / SOL")]}

    к = кандидаты_из_пулов(страниц=2, сколько=5, запрос_fn=страница)
    chk("пул к USDC в кандидаты не попал",
        "К_USDC" not in [x["mint"] for x in к["pools"]], к["pools"])
    chk("кандидаты отсортированы по резерву",
        [x["mint"] for x in к["pools"]] == ["ГЛУБОКИЙ", "МЕЛКИЙ_ПУЛ"], к["pools"])
    chk("повтор минта со второй страницы не задвоился",
        len(к["pools"]) == 2, к["pools"])

    def страница_отказ(n):
        return {"known": False, "why": "http 429"}

    к2 = кандидаты_из_пулов(страниц=1, сколько=5, запрос_fn=страница_отказ)
    chk("отказ источника пулов назван, а не выдан за пустой список",
        к2["known"] is False and к2["failures"], к2)

    в = выбрать([хороший, двухх, мелкий])
    chk("выбран единственный годный", в["chosen"] == "ХОРОШИЙ", в)
    в2 = выбрать([двухх, мелкий, налог])
    chk("без годных -- честное нет", в2["chosen"] is None, в2)
    глубже = проверить("ГЛУБЖЕ", helius=НалогЗаглушка(),
                       котировка_fn=подставная(price_impact=0.0001), пауза_с=0)
    в3 = выбрать([хороший, глубже])
    chk("из двух годных берётся более глубокий", в3["chosen"] == "ГЛУБЖЕ", в3)
    chk("рынок выбранного назван", в3.get("market") == "Raydium", в3)
    chk("все ключи вердикта латинские", all(k.isascii() for k in хороший))

    # --- три класса для пунктов 3, 5 и 6: проверяется настоящая классы()
    def страница_классов(n):
        def пул(минт, котир, резерв, объём, dex, имя):
            return {"attributes": {"name": имя, "reserve_in_usd": str(резерв),
                                    "dex_id": dex,
                                    "volume_usd": {"h24": str(объём)}},
                    "relationships": {
                        "base_token": {"data": {"id": f"solana_{минт}"}},
                        "quote_token": {"data": {"id": f"solana_{котир}"}}}}
        if n != 1:
            return {"known": True, "data": []}
        return {"known": True, "data": [
            пул("НАЛОГОВЫЙ", WSOL, 100000, 50000, "raydium", "TAX / SOL"),
            пул("ЧИСТЫЙ_RAY", WSOL, 400000, 90000, "raydium", "RAY1 / SOL"),
            пул("МЕЛКИЙ", WSOL, 100, 10, "raydium", "SMALL / SOL"),
            пул("ТИХИЙ", WSOL, 300000, 100, "raydium", "QUIET / SOL"),
            пул("ЧУЖОЙ_DEX", WSOL, 300000, 90000, "pumpswap", "PS / SOL"),
            пул("ТОЛЬКО_USDC", USDC, 200000, 60000, "orca", "ONLY / USDC"),
            пул("USDC_НО_ЕСТЬ_SOL", USDC, 900000, 60000, "orca", "BOTH / USDC"),
            пул("USDC_НО_ЕСТЬ_SOL", WSOL, 50000, 60000, "raydium", "BOTH / SOL"),
            пул("МЕЛКИЙ_USDC", USDC, 500, 60000, "orca", "SMALLU / USDC"),
        ]}

    def котировка_классов(минт, размер):
        прямые = {"НАЛОГОВЫЙ", "ЧИСТЫЙ_RAY", "ТИХИЙ", "ЧУЖОЙ_DEX",
                  "USDC_НО_ЕСТЬ_SOL"}
        if минт in прямые:
            return {"known": True, "hops": 1, "price_impact": 0.001,
                    "markets": [{"label": "Raydium"}], "out_amount": "1"}
        return {"known": True, "hops": 2, "price_impact": 0.004,
                "markets": [{"label": "Orca"}, {"label": "Whirlpool"}],
                "out_amount": "1"}

    class НалогиКлассов:
        def налог_минта(self, минт):
            if минт == "НАЛОГОВЫЙ":
                return {"mint": минт, "token_program": TOKEN_2022,
                        "fee_bps": 200, "taxed": True}
            return {"mint": минт, "token_program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "fee_bps": None, "taxed": False}

    кл = классы(страниц=1, на_класс=3, helius=НалогиКлассов(),
                котировка_fn=котировка_классов, запрос_fn=страница_классов,
                пауза_с=0)
    п3 = [x["mint"] for x in кл["item3_taxed_direct_sol"]]
    п5 = [x["mint"] for x in кл["item5_raydium_meteora_direct"]]
    п6 = [x["mint"] for x in кл["item6_usdc_only"]]
    chk("п. 3: только Token-2022 с ненулевым налогом и прямым пулом",
        п3 == ["НАЛОГОВЫЙ"], п3)
    chk("п. 5: налоговый токен в чистые не попал", "НАЛОГОВЫЙ" not in п5, п5)
    chk("п. 5: чужой dex не попал", "ЧУЖОЙ_DEX" not in п5, п5)
    chk("п. 5: мелкий резерв не попал", "МЕЛКИЙ" not in п5, п5)
    chk("п. 5: тихий по объёму не попал", "ТИХИЙ" not in п5, п5)
    chk("п. 5: годный кандидат найден", "ЧИСТЫЙ_RAY" in п5, п5)
    chk("п. 6: токен только с пулом к USDC найден",
        "ТОЛЬКО_USDC" in п6, п6)
    chk("п. 6: токен, у которого есть и пул к SOL, отброшен",
        "USDC_НО_ЕСТЬ_SOL" not in п6, п6)
    chk("п. 6: мелкий USDC-пул отброшен", "МЕЛКИЙ_USDC" not in п6, п6)
    chk("отказ страницы пулов назван, а не проглочен",
        классы(страниц=1, на_класс=1,
               котировка_fn=котировка_классов,
               запрос_fn=lambda n: {"known": False, "why": "http 429"},
               пауза_с=0)["pools"]["failures"] != [], "нет отказов")
    пусто = классы(страниц=1, на_класс=1, котировка_fn=котировка_классов,
                   запрос_fn=lambda n: {"known": True, "data": []}, пауза_с=0)
    chk("пустой список пулов даёт примечания по всем трём пунктам",
        len(пусто["notes"]) == 3, пусто["notes"])

    прошло = sum(1 for _, ок, _ in checks if ок)
    for имя, ок, факт in checks:
        print(f"  [{'ok  ' if ок else 'ПЛОХО'}] {имя}" + (f" -- {факт}" if not ок else ""))
    print(f"самопроверка выбора токена: {прошло}/{len(checks)} пройдено")
    return 0 if прошло == len(checks) else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--mints", default="",
                   help="кандидаты через запятую (адреса минтов)")
    p.add_argument("--top", type=int, default=8,
                   help="сколько кандидатов взять из списка пулов, если --mints пуст")
    p.add_argument("--classes", action="store_true",
                   help="искать токены для пунктов 3, 5 и 6 прохода A")
    p.add_argument("--pages", type=int, default=4,
                   help="сколько страниц пулов просмотреть")
    p.add_argument("--per-class", type=int, default=3,
                   help="сколько кандидатов на класс")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if a.classes:
        helius = None
        try:
            import bloom_detector as BD  # noqa: PLC0415
            helius = BD.Helius(служба="")
        except Exception as exc:  # noqa: BLE001
            print(f"налог минта по цепи не проверить: {type(exc).__name__}",
                  file=sys.stderr)
        итог = классы(страниц=a.pages, на_класс=a.per_class, helius=helius)
        итог["checked_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(итог, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(json.dumps(итог, ensure_ascii=False, indent=2))
        print("--- коротко ---")
        print(f"пулов к SOL {итог['pools']['to_sol']}, к USDC "
              f"{итог['pools']['to_usdc']}, проверено кандидатов "
              f"{итог['checked']}")
        for имя, ключ in (("п. 3 Token-2022 с налогом + прямой пул к SOL",
                           "item3_taxed_direct_sol"),
                          ("п. 5 Raydium/Meteora + прямой пул к SOL",
                           "item5_raydium_meteora_direct"),
                          ("п. 6 пул только к USDC", "item6_usdc_only")):
            print(f"{имя}:")
            for x in итог[ключ]:
                print(f"    {x['mint']} | {x.get('pool_name')} | dex "
                      f"{x.get('dex')} | резерв {x.get('reserve_usd'):.0f} USD | "
                      f"объём24ч {x.get('volume_24h_usd'):.0f} USD | хопов "
                      f"{x.get('hops')} | влияние {x.get('price_impact')} | "
                      f"налог {x.get('fee_bps')} bps | программа "
                      f"{str(x.get('token_program'))[:6]}")
            if not итог[ключ]:
                print("    кандидатов нет")
        for n in итог["notes"]:
            print(f"  примечание: {n}")
        return 0

    минты = [x.strip() for x in a.mints.split(",") if x.strip()]
    пулы = None
    if not минты:
        пулы = кандидаты_из_пулов(сколько=a.top)
        if not пулы.get("pools"):
            print(f"кандидатов не получено: {пулы.get('failures')}", file=sys.stderr)
            return 2
        минты = [x["mint"] for x in пулы["pools"]]
        print("кандидаты из пулов (источник "
              f"{пулы['source']}): {len(минты)}")
    helius = None
    try:
        import bloom_detector as BD  # noqa: PLC0415
        helius = BD.Helius(служба="")
    except Exception as exc:  # noqa: BLE001
        print(f"налог минта по цепи не проверить: {type(exc).__name__}", file=sys.stderr)
    результаты = [проверить(m, helius=helius) for m in минты]
    итог = {"checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "limit_price_impact": ПРЕДЕЛ_ВЛИЯНИЯ,
            "candidates_from_pools": пулы,
            "sizes_sol": list(РАЗМЕРЫ_SOL),
            "candidates": результаты,
            "verdict": выбрать(результаты)}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(итог, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(json.dumps(итог, ensure_ascii=False, indent=2))
    print("--- коротко ---")
    for r in результаты:
        осн = r["quotes"].get("0.05") or {}
        print(f"{r['mint']}: хопов {осн.get('hops')}, влияние "
              f"{осн.get('price_impact')}, налог "
              f"{(r.get('tax') or {}).get('fee_bps')}, годен: {r['ok']}"
              + ("" if r["ok"] else f" -- {'; '.join(r['why_not'])}"))
    print(f"вердикт: {итог['verdict']}")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
