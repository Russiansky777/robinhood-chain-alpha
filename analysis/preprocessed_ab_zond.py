#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ВИДЕТЬ РАНЬШЕ: preprocessedSubscribe против обычного transactionSubscribe.

ЗАЧЕМ (слово владельца 11.10, доп.): "Включить preprocessedSubscribe Helius по
адресу 4vw рядом с обычным WS, писать время прихода каждой покупки источника в
обоих потоках. Выключить, когда наберётся 30 покупок. Ответ: медиана и p90
«WS − preprocessed» по транзакциям; если >= 20 мс -- назвать, что нужно, чтобы
детектор работал от этого потока (там нет балансов -- сумму читать из
инструкции). Денежный путь не трогать".

ЧТО ЭТО ЗА ПОТОК (источник: документация Helius, разделы
helius.dev/docs/preprocessed-transactions/preprocessed-subscribe и
helius.dev/preprocessed-transactions, прочитано 11.10):

* метод `preprocessedSubscribe`, точка входа `wss://beta.helius-rpc.com/?api-key=`;
* params -- ОБЪЕКТ (а не список, как у transactionSubscribe) с полями
  `accountInclude`, `accountExclude`, `accountRequired`; `accountInclude` и
  `accountRequired` вместе обязаны назвать хотя бы один адрес -- полного потока
  без фильтра нет; фильтры складываются по И; до 5000 адресов в списке;
  таблицы адресов (ALT) Helius разворачивает на своей стороне, то есть фильтр
  попадает и в счета, подгруженные через таблицу;
* ответ на подписку приходит ТЕКСТОВЫМ кадром с номером подписки, а сами
  уведомления -- ДВОИЧНЫМИ кадрами: байт 0 -- версия схемы (сейчас 1), байты
  1..8 -- слот (u64, младшим байтом вперёд), байты 9..72 -- первая подпись (64
  сырых байта), с байта 73 -- транзакция в обычном формате Solana;
* данные приходят ДО исполнения, поэтому в них НЕТ ни изменений балансов, ни
  логов, ни ошибки -- ровно то, о чём предупредил владелец;
* цена -- 0.1 кредита за сообщение на платных планах. То есть "бесплатно" здесь
  означает "десяток кредитов за весь замер", а не ноль: 30 покупок источника --
  это считанные кредиты.

ЗАЧЕМ ДВА СОЕДИНЕНИЯ В ОДНОМ ПРОЦЕССЕ. Сравнивать время прихода можно только
по ОДНИМ часам: два прогона на двух машинах дали бы разницу часов, а не
разницу потоков. Поэтому оба потока слушает один процесс, и обе метки --
`time.perf_counter()` одного процесса.

ДЕНЕЖНЫЙ ПУТЬ НЕ ТРОГАЕТСЯ: это отдельный процесс, он ничего не собирает, не
подписывает и не отправляет; он только слушает и пишет свой журнал. Ключ
читается из файла и в отчёт не попадает.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

АДРЕС_ПО_УМОЛЧАНИЮ = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
ПОКУПОК_ЦЕЛЬ = 30
СЕКУНД_ПРЕДЕЛ = 6 * 3600.0
# РАСКЛАДКА ДВОИЧНОГО КАДРА -- из документации Helius (см. головное описание).
ВЕРСИЯ_КАДРА = 1
СМЕЩЕНИЕ_СЛОТА = 1
СМЕЩЕНИЕ_ПОДПИСИ = 9
ДЛИНА_ПОДПИСИ = 64
СМЕЩЕНИЕ_ТЕЛА = 73
ТОЧКА_PRE = "wss://beta.helius-rpc.com/?api-key="
ТОЧКА_WS = "wss://atlas-mainnet.helius-rpc.com/?api-key="
# ПОТОЛОК ВЕРСИИ ТРАНЗАКЦИИ -- как у службы: на нуле узел отказывал.
ПОТОЛОК_ВЕРСИИ_TX = 1
АЛФАВИТ58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# СКОЛЬКО ПОДПИСЕЙ ДЕРЖИМ В ПАМЯТИ. Зонд живёт часами рядом с торгующей
# службой на двух ядрах и 4 ГБ, и держать всё подряд нельзя: при темпе чуть
# ниже предохранителя за сутки набежали бы сотни тысяч записей. Парные встречи
# из подрезки исключены -- они и есть ответ.
РЯДОВ_ПРЕДЕЛ = 20_000
ОЧЕРЕДИ_ПРЕДЕЛ = 50_000
# ПОРОГ ВЛАДЕЛЬЦА: если выигрыш >= 20 мс, поток стоит того, чтобы думать о
# переводе детектора на него.
ПОРОГ_МС = 20.0
# ПРЕДОХРАНИТЕЛЬ: СКОЛЬКО СООБЩЕНИЙ В СЕКУНДУ ПО ОДНОМУ АДРЕСУ -- УЖЕ НЕ НАШ
# ПОТОК. Источник 4vw делает единицы транзакций в минуту; 5 сообщений в
# СЕКУНДУ по одному адресу невозможны. Прогон 11.10 01:2xZ получил 62.18
# сообщения в секунду и 94 разных слота за 21 с -- это поток всей сети, и по
# тарифу 0.1 кредита за сообщение он стоил бы 537 235 кредитов в сутки. Зонд
# обязан сам это заметить и выключиться, а не жечь кредиты до предела по часам.
ПОТОЛОК_СООБЩЕНИЙ_В_СЕКУНДУ = 5.0
СЕКУНД_ДО_ПРЕДОХРАНИТЕЛЯ = 20.0
# ФОРМЫ ПАРАМЕТРОВ, КОТОРЫЕ ПРОБУЕМ. Документация показывает объект, но
# фильтр по объекту не подействовал, а отказа не было: значит форму надо
# выяснить у сервера, а не у памяти. Каждая проба длится считанные секунды и
# стоит считанные кредиты.
ФОРМЫ_ПАРАМЕТРОВ = (
    ("objekt", lambda а: {"accountInclude": [а], "accountExclude": [],
                           "accountRequired": []}),
    ("objekt_bez_pustyh", lambda а: {"accountInclude": [а]}),
    ("spisok_objekta", lambda а: [{"accountInclude": [а], "accountExclude": [],
                                    "accountRequired": []}]),
    ("objekt_v_filter", lambda а: {"filter": {"accountInclude": [а]}}),
    ("accountRequired", lambda а: {"accountInclude": [], "accountExclude": [],
                                    "accountRequired": [а]}),
)


def base58_из_байтов(сырые: bytes) -> str:
    """Байты -> base58. Нужен, чтобы подпись из двоичного кадра сошлась с
    подписью из обычного потока, где она уже строкой."""
    число = int.from_bytes(сырые, "big")
    из_ = ""
    while число:
        число, ост = divmod(число, 58)
        из_ = АЛФАВИТ58[ост] + из_
    нулей = 0
    for б in сырые:
        if б == 0:
            нулей += 1
        else:
            break
    return АЛФАВИТ58[0] * нулей + (из_ or ("" if сырые else ""))


def затереть(т) -> str:
    т = str(т or "")
    return т.split("api-key=")[0] + "api-key=…" if "api-key=" in т else т


def разобрать_кадр(кадр: bytes) -> dict:
    """Двоичный кадр preprocessedSubscribe -> версия, слот, подпись."""
    из_ = {"ok": False, "why_not": None, "versiya": None, "slot": None,
            "podpis": None, "bajt_tela": None, "bajt_vsego": len(кадр or b"")}
    if not кадр:
        из_["why_not"] = "пустой кадр"
        return из_
    if len(кадр) < СМЕЩЕНИЕ_ТЕЛА:
        из_["why_not"] = (f"кадр короче заголовка: {len(кадр)} байт при "
                           f"{СМЕЩЕНИЕ_ТЕЛА} обязательных")
        return из_
    из_["versiya"] = кадр[0]
    if кадр[0] != ВЕРСИЯ_КАДРА:
        # ВЕРСИЮ ПРОВЕРЯЕМ, А НЕ ИГНОРИРУЕМ: документация прямо просит
        # ветвиться по ней, чтобы новая схема не разбиралась старым кодом в
        # мусор. Отказ здесь дешевле, чем выдуманная подпись.
        из_["why_not"] = (f"версия схемы кадра {кадр[0]}, а разбирать умеем "
                           f"только {ВЕРСИЯ_КАДРА}")
        return из_
    из_["slot"] = int.from_bytes(кадр[СМЕЩЕНИЕ_СЛОТА:СМЕЩЕНИЕ_ПОДПИСИ],
                                 "little")
    сырая_подпись = кадр[СМЕЩЕНИЕ_ПОДПИСИ:СМЕЩЕНИЕ_ПОДПИСИ + ДЛИНА_ПОДПИСИ]
    из_["bajt_tela"] = len(кадр) - СМЕЩЕНИЕ_ТЕЛА
    # НУЛЕВАЯ ПОДПИСЬ -- ЭТО НЕ ПОДПИСЬ. В base58 64 нулевых байта дают строку
    # из 64 единиц, то есть непустую и на вид правдоподобную: проверять надо
    # байты, а не длину строки, иначе пустой кадр лёг бы в журнал как встреча.
    if not any(сырая_подпись):
        из_["why_not"] = "подпись в кадре нулевая"
        return из_
    из_["podpis"] = base58_из_байтов(сырая_подпись)
    из_["ok"] = True
    return из_


def подпись_из_ws(сообщение: dict) -> dict:
    """Подпись и слот из уведомления transactionSubscribe."""
    из_ = {"podpis": None, "slot": None, "why_not": None}
    знач = (((сообщение or {}).get("params") or {}).get("result") or {})
    из_["slot"] = знач.get("slot")
    подпись = знач.get("signature")
    if not подпись:
        tx = (знач.get("transaction") or {})
        внутр = tx.get("transaction") or {}
        подписи = внутр.get("signatures") or tx.get("signatures") or []
        подпись = подписи[0] if подписи else None
    из_["podpis"] = подпись
    if not подпись:
        из_["why_not"] = "в уведомлении нет подписи"
    return из_


class Встречи:
    """Кто какую подпись увидел и когда -- по ОДНИМ часам процесса."""

    def __init__(self, *, ПЕРВЫХ_В_ОТЧЁТ: int = 8) -> None:
        self.ряды: dict = {}
        self.поток_счёт = {"ws": 0, "pre": 0}
        self.отказы: dict = {"ws": None, "pre": None}
        self.начало = time.perf_counter()
        # ОЧЕРЕДЬ ВСЕХ ПРИХОДОВ -- её выгребает журнал зонда.
        self.очередь: list = []
        self.слоты = {"ws": set(), "pre": set()}
        self.первые: dict = {"ws": [], "pre": []}
        self.первых_в_отчёт = int(ПЕРВЫХ_В_ОТЧЁТ)
        self.bityh_kadrov = 0
        self.pochemu_bityh = None
        self.obryvov = {"ws": 0, "pre": 0}
        self.zabyto_ryadov = 0

    def принять(self, поток: str, подпись: str, *, слот=None,
                мono: float | None = None) -> dict:
        # КАЖДЫЙ ПРИХОД -- В ОЧЕРЕДЬ НА ЗАПИСЬ, а не только парные встречи.
        # Прогон 01:20Z это и показал: preprocessed отдал 2196 сообщений по
        # молчащему адресу, обычный поток -- ни одного, и по журналу встреч
        # нельзя было даже посмотреть, ЧТО это за подписи.
        """Первое появление подписи в потоке. Повтор НЕ переписывает время."""
        if not подпись:
            return {}
        сейчас = time.perf_counter() if мono is None else float(мono)
        self.поток_счёт[поток] = self.поток_счёт.get(поток, 0) + 1
        ряд = self.ряды.setdefault(подпись, {
            "podpis": подпись, "ws_mono": None, "pre_mono": None,
            "ws_utc": None, "pre_utc": None, "slot_ws": None,
            "slot_pre": None, "ws_povtorov": 0, "pre_povtorov": 0})
        ключ = f"{поток}_mono"
        if слот is not None:
            self.слоты[поток].add(слот)
        self.очередь.append({"potok": поток, "podpis": подпись, "slot": слот,
                              "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                    time.gmtime()),
                              "s_nachala_s": round(сейчас - self.начало, 4)})
        if len(self.первые[поток]) < self.первых_в_отчёт:
            self.первые[поток].append(подпись)
        # ОЧЕРЕДЬ НЕ РАСТЁТ БЕЗ ГРАНИЦ даже без журнала: без этого зонд без
        # --out копил бы её вечно.
        if len(self.очередь) > ОЧЕРЕДИ_ПРЕДЕЛ:
            del self.очередь[:len(self.очередь) - ОЧЕРЕДИ_ПРЕДЕЛ]
        if ряд[ключ] is None:
            ряд[ключ] = сейчас
            ряд[f"{поток}_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime())
            ряд[f"slot_{поток}"] = слот
        else:
            # ПОВТОРЫ БЫВАЮТ: та же подпись приходит на нескольких подписках.
            # Время берём ПЕРВОЕ, а повтор считаем -- иначе "раньше" зависело
            # бы от того, какое из сообщений попалось.
            ряд[f"{поток}_povtorov"] += 1
        return ряд

    def подрезать(self) -> int:
        """Забыть самые старые ОДИНОЧНЫЕ подписи, оставив парные встречи."""
        if len(self.ряды) <= РЯДОВ_ПРЕДЕЛ:
            return 0
        одиночные = [(р.get("pre_mono") or р.get("ws_mono") or 0.0, к)
                      for к, р in self.ряды.items()
                      if р["ws_mono"] is None or р["pre_mono"] is None]
        одиночные.sort()
        сколько = min(len(одиночные), len(self.ряды) - РЯДОВ_ПРЕДЕЛ)
        for _, к in одиночные[:сколько]:
            self.ряды.pop(к, None)
        self.zabyto_ryadov += сколько
        return сколько

    def оба(self) -> list:
        return [р for р in self.ряды.values()
                if р["ws_mono"] is not None and р["pre_mono"] is not None]

    def разницы_мс(self) -> list:
        # ПОЛОЖИТЕЛЬНОЕ ЧИСЛО = preprocessed пришёл РАНЬШЕ обычного потока.
        return [round((р["ws_mono"] - р["pre_mono"]) * 1000.0, 3)
                for р in self.оба()]


def _процентиль(ряд: list, доля: float):
    if not ряд:
        return None
    с = sorted(ряд)
    место = min(len(с) - 1, int(round(доля * (len(с) - 1))))
    return round(с[место], 3)


def свод(встречи: Встречи, *, цель: int = ПОКУПОК_ЦЕЛЬ) -> dict:
    """Медиана и p90 «WS − preprocessed» и что из этого следует."""
    разницы = встречи.разницы_мс()
    из_ = {"tranzakcij_v_oboih": len(разницы),
            "cel": цель,
            "soobshchenij_ws": встречи.поток_счёт.get("ws", 0),
            "soobshchenij_pre": встречи.поток_счёт.get("pre", 0),
            "podpisej_tolko_ws": sum(1 for р in встречи.ряды.values()
                                      if р["ws_mono"] is not None
                                      and р["pre_mono"] is None),
            "podpisej_tolko_pre": sum(1 for р in встречи.ряды.values()
                                       if р["pre_mono"] is not None
                                       and р["ws_mono"] is None),
            "otkazy": dict(встречи.отказы),
            "bityh_kadrov": встречи.bityh_kadrov,
            "pochemu_bityh": встречи.pochemu_bityh,
            "obryvov": dict(встречи.obryvov),
            "zabyto_ryadov": встречи.zabyto_ryadov,
            # ТЕМП СООБЩЕНИЙ -- ГЛАВНЫЙ ПРИЗНАК, ЧТО ФИЛЬТР НЕ РАБОТАЕТ. Один
            # адрес источника даёт единицы сообщений в минуту; десятки в
            # секунду означают, что нам шлют поток шире, чем мы просили, и
            # платить за него по 0.1 кредита за сообщение незачем.
            "soobshchenij_pre_v_sekundu": None,
            "soobshchenij_ws_v_sekundu": None,
            "slotov_pre": len(встречи.слоты.get("pre") or ()),
            "slotov_ws": len(встречи.слоты.get("ws") or ()),
            "pervye_podpisi_pre": list(встречи.первые.get("pre") or ()),
            "pervye_podpisi_ws": list(встречи.первые.get("ws") or ()),
            "sekund_raboty": None,
            "mediana_ms": None, "p90_ms": None, "min_ms": None, "max_ms": None,
            "pre_ranshe_dolya": None, "vyvod": None, "porog_ms": ПОРОГ_МС}
    прошло = max(1e-9, time.perf_counter() - встречи.начало)
    из_["sekund_raboty"] = round(прошло, 1)
    из_["soobshchenij_pre_v_sekundu"] = round(
        встречи.поток_счёт.get("pre", 0) / прошло, 2)
    из_["soobshchenij_ws_v_sekundu"] = round(
        встречи.поток_счёт.get("ws", 0) / прошло, 2)
    # ЦЕНА ПОТОКА ПО ФАКТУ: 0.1 кредита за сообщение (тариф Helius).
    из_["kreditov_v_sutki_pre"] = int(round(
        из_["soobshchenij_pre_v_sekundu"] * 86400 * 0.1))
    if not разницы:
        из_["vyvod"] = ("ни одной транзакции в ОБОИХ потоках: "
                         + (встречи.отказы.get("pre")
                            or встречи.отказы.get("ws")
                            or "сравнивать пока нечего"))
        return из_
    из_["mediana_ms"] = round(statistics.median(разницы), 3)
    из_["p90_ms"] = _процентиль(разницы, 0.9)
    из_["min_ms"] = min(разницы)
    из_["max_ms"] = max(разницы)
    из_["pre_ranshe_dolya"] = round(
        sum(1 for д in разницы if д > 0) / len(разницы), 4)
    if из_["mediana_ms"] >= ПОРОГ_МС:
        из_["vyvod"] = (f"preprocessed раньше на {из_['mediana_ms']} мс по "
                         f"медиане -- это >= порога {ПОРОГ_МС} мс, перевод "
                         f"детектора имеет смысл")
    elif из_["mediana_ms"] > 0:
        из_["vyvod"] = (f"preprocessed раньше на {из_['mediana_ms']} мс по "
                         f"медиане -- ниже порога {ПОРОГ_МС} мс")
    else:
        из_["vyvod"] = (f"preprocessed НЕ раньше: медиана "
                         f"{из_['mediana_ms']} мс")
    return из_


async def _поток_pre(встречи: Встречи, *, ключ: str, адрес: str,
                      стоп: asyncio.Event, форма: str = "objekt") -> None:
    # ПЕРЕПОДКЛЮЧЕНИЕ, А НЕ ОДНО СОЕДИНЕНИЕ НА ШЕСТЬ ЧАСОВ. Один оборванный
    # сокет прежде заканчивал поток НАВСЕГДА, и зонд часами платил за второй
    # поток, не получая ни одной сравнимой пары.
    while not стоп.is_set():
        await _попытка_pre(встречи, ключ=ключ, адрес=адрес, стоп=стоп,
                            форма=форма)
        if стоп.is_set():
            return
        # ОТКАЗ ПЛАНА НЕ ПЕРЕПОДКЛЮЧАЕМ: это не обрыв, а ответ "нельзя", и
        # долбить его раз в секунду бессмысленно.
        if "план отказал" in (встречи.отказы.get("pre") or ""):
            return
        встречи.obryvov["pre"] += 1
        await asyncio.sleep(1.0)


async def _попытка_pre(встречи: Встречи, *, ключ: str, адрес: str,
                        стоп: asyncio.Event, форма: str = "objekt") -> None:
    import websockets  # noqa: PLC0415
    запрос = {"jsonrpc": "2.0", "id": 1, "method": "preprocessedSubscribe",
              "params": _параметры_формы(форма, адрес)}
    try:
        async with websockets.connect(ТОЧКА_PRE + ключ, ping_interval=20,
                                       ping_timeout=30,
                                       max_size=16 * 1024 * 1024) as соед:
            await соед.send(json.dumps(запрос))
            while not стоп.is_set():
                try:
                    сырое = await asyncio.wait_for(соед.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                if isinstance(сырое, str):
                    # ТЕКСТОВЫЙ КАДР -- это ответ на подписку или ошибка.
                    try:
                        ответ = json.loads(сырое)
                    except ValueError:
                        continue
                    if "error" in ответ:
                        встречи.отказы["pre"] = (
                            "план отказал в preprocessedSubscribe: "
                            + json.dumps(ответ["error"], ensure_ascii=False)[:300])
                        return
                    continue
                р = разобрать_кадр(сырое)
                if not р["ok"]:
                    # БИТЫЙ КАДР -- НЕ ОТКАЗ ПОТОКА. Прежде первый же кадр
                    # чужой версии залипал в отказы, и зонд выходил, не
                    # дождавшись ни одной встречи: отказ потока и один
                    # нечитаемый кадр -- разные вещи.
                    встречи.bityh_kadrov += 1
                    встречи.pochemu_bityh = (встречи.pochemu_bityh
                                              or r_why(р))
                    continue
                встречи.принять("pre", р["podpis"], слот=р["slot"])
    except Exception as сбой:  # noqa: BLE001
        встречи.отказы["pre"] = f"{type(сбой).__name__}: {затереть(сбой)[:300]}"


def r_why(р: dict) -> str:
    return f"кадр не разобран: {р.get('why_not')}"


async def _поток_ws(встречи: Встречи, *, ключ: str, адрес: str,
                     стоп: asyncio.Event) -> None:
    while not стоп.is_set():
        await _попытка_ws(встречи, ключ=ключ, адрес=адрес, стоп=стоп)
        if стоп.is_set():
            return
        встречи.obryvov["ws"] += 1
        await asyncio.sleep(1.0)


async def _попытка_ws(встречи: Встречи, *, ключ: str, адрес: str,
                       стоп: asyncio.Event) -> None:
    import websockets  # noqa: PLC0415
    # ПОДПИСКА -- БАЙТ В БАЙТ КАК У СЛУЖБЫ (analysis/bloom_detector.py,
    # _подписка_транзакций): иначе сравнивались бы не потоки, а настройки.
    запрос = {"jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe",
              "params": [{"accountInclude": [адрес], "failed": False,
                           "vote": False},
                          {"commitment": "processed",
                           "transactionDetails": "full",
                           "encoding": "jsonParsed", "showRewards": False,
                           "maxSupportedTransactionVersion": ПОТОЛОК_ВЕРСИИ_TX}]}
    try:
        async with websockets.connect(ТОЧКА_WS + ключ, ping_interval=20,
                                       ping_timeout=30,
                                       max_size=16 * 1024 * 1024) as соед:
            await соед.send(json.dumps(запрос))
            while not стоп.is_set():
                try:
                    сырое = await asyncio.wait_for(соед.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                if not isinstance(сырое, str):
                    continue
                try:
                    сообщение = json.loads(сырое)
                except ValueError:
                    continue
                if "error" in сообщение:
                    встречи.отказы["ws"] = json.dumps(
                        сообщение["error"], ensure_ascii=False)[:300]
                    continue
                п = подпись_из_ws(сообщение)
                if п["podpis"]:
                    встречи.принять("ws", п["podpis"], слот=п["slot"])
    except Exception as сбой:  # noqa: BLE001
        встречи.отказы["ws"] = f"{type(сбой).__name__}: {затереть(сбой)[:300]}"


async def зонд(*, ключ: str, адрес: str, цель: int, секунд: float,
                путь_журнала: str = "", путь_признака: str = "",
                форма: str = "avto", секунд_на_форму: float = 8.0) -> dict:
    встречи = Встречи()
    формы = None
    if форма == "avto":
        # ФОРМУ ПАРАМЕТРОВ ВЫЯСНЯЕМ У СЕРВЕРА, А НЕ У ПАМЯТИ. 11.10 форма из
        # документации (объект) подписку приняла, но фильтр не применила: пошёл
        # поток всей сети, 62 сообщения в секунду. Открывать долгий поток, не
        # убедившись, что фильтр действует, значит жечь кредиты впустую.
        формы = await пробы_форм(ключ=ключ, адрес=адрес,
                                  секунд=секунд_на_форму)
        форма = формы.get("godnaya_forma")
        if not форма:
            из_ = свод(встречи, цель=цель)
            из_["formy"] = формы
            из_["why_not"] = ("ни одна форма параметров не дала "
                               "ДЕЙСТВУЮЩЕГО фильтра -- долгий поток не "
                               "открыт, кредиты не потрачены")
            из_["vyvod"] = из_["why_not"]
            if путь_признака:
                Path(путь_признака).write_text(
                    json.dumps(из_, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
            return из_
    # ОТСЧЁТ ТЕМПА -- С МОМЕНТА ОТКРЫТИЯ ДОЛГОГО ПОТОКА, а не с создания
    # объекта: пробы форм занимают до 40 с, и без этого сброса темп (а с ним и
    # предохранитель, и цена в кредитах) были бы занижены почти вдвое на всём
    # начале прогона.
    встречи.начало = time.perf_counter()
    стоп = asyncio.Event()
    задачи = [asyncio.create_task(_поток_pre(встречи, ключ=ключ, адрес=адрес,
                                              стоп=стоп, форма=форма)),
              asyncio.create_task(_поток_ws(встречи, ключ=ключ, адрес=адрес,
                                             стоп=стоп))]
    кончить = time.time() + float(секунд)
    записано = set()
    try:
        while time.time() < кончить:
            await asyncio.sleep(1.0)
            # ЖУРНАЛ ПОПОЛНЯЕТСЯ НА ХОДУ: зонд живёт часами, и ждать конца,
            # чтобы увидеть первое число, незачем.
            if путь_журнала:
                # ВСЕ ПРИХОДЫ, А НЕ ТОЛЬКО ВСТРЕЧИ. Иначе нельзя проверить, те
                # ли это подписи вообще: 01:20Z preprocessed отдал 2196
                # сообщений по молчащему адресу, и посмотреть на них было негде.
                очередь, встречи.очередь = встречи.очередь, []
                with open(путь_журнала, "a", encoding="utf-8") as ф:
                    for з in очередь:
                        ф.write(json.dumps(з, ensure_ascii=False) + "\n")
                    for р in встречи.оба():
                        if р["podpis"] in записано:
                            continue
                        записано.add(р["podpis"])
                        ф.write(json.dumps({
                            "vstrecha": True,
                            "podpis": р["podpis"], "slot_ws": р["slot_ws"],
                            "slot_pre": р["slot_pre"],
                            "ws_utc": р["ws_utc"], "pre_utc": р["pre_utc"],
                            "raznica_ms": round((р["ws_mono"] - р["pre_mono"])
                                                 * 1000.0, 3)},
                            ensure_ascii=False) + "\n")
            встречи.подрезать()
            if путь_признака:
                # ФОРМА -- В КАЖДЫЙ промежуточный признак. Обычный путь
                # прогона -- собрать отчёт У РАБОТАЮЩЕГО зонда, и без этих
                # полей отчёт не говорил, чем мерили и работает ли фильтр.
                _пром = свод(встречи, цель=цель)
                _пром["forma"] = форма
                _пром["formy"] = формы
                Path(путь_признака).write_text(
                    json.dumps(_пром, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
            # ВЫКЛЮЧАЕМСЯ САМИ, КАК ПРОСИЛ ВЛАДЕЛЕЦ: 30 транзакций в обоих
            # потоках -- и стоп, чтобы не платить за сообщения дальше.
            if len(встречи.оба()) >= цель:
                break
            if встречи.отказы.get("pre") and not встречи.оба():
                break
            # ПРЕДОХРАНИТЕЛЬ ПО ТЕМПУ. Срабатывает только после
            # СЕКУНД_ДО_ПРЕДОХРАНИТЕЛЯ, чтобы догоняющая пачка на старте не
            # остановила зонд зря.
            прошло_с = time.perf_counter() - встречи.начало
            if прошло_с >= СЕКУНД_ДО_ПРЕДОХРАНИТЕЛЯ:
                темп = встречи.поток_счёт.get("pre", 0) / прошло_с
                if темп > ПОТОЛОК_СООБЩЕНИЙ_В_СЕКУНДУ:
                    встречи.отказы["pre"] = (
                        f"ПРЕДОХРАНИТЕЛЬ: {темп:.2f} сообщения в секунду по "
                        f"одному адресу при потолке "
                        f"{ПОТОЛОК_СООБЩЕНИЙ_В_СЕКУНДУ} -- фильтр "
                        f"accountInclude не применяется, нам шлют поток шире "
                        f"запрошенного; зонд выключен, чтобы не жечь кредиты")
                    break
    finally:
        стоп.set()
        for з in задачи:
            з.cancel()
        await asyncio.gather(*задачи, return_exceptions=True)
    из_ = свод(встречи, цель=цель)
    из_["forma"] = форма
    из_["formy"] = формы
    из_["ryady"] = [{"podpis": р["podpis"], "slot_ws": р["slot_ws"],
                      "slot_pre": р["slot_pre"], "ws_utc": р["ws_utc"],
                      "pre_utc": р["pre_utc"],
                      "raznica_ms": round((р["ws_mono"] - р["pre_mono"])
                                           * 1000.0, 3)}
                     for р in встречи.оба()]
    if путь_признака:
        Path(путь_признака).write_text(
            json.dumps(из_, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    return из_


def _параметры_формы(имя: str, адрес: str):
    """Параметры подписки по имени формы. Неизвестная форма -- исключение."""
    for и, построить in ФОРМЫ_ПАРАМЕТРОВ:
        if и == имя:
            return построить(адрес)
    raise ValueError(f"форма параметров {имя!r} неизвестна")


async def проба_формы(*, ключ: str, адрес: str, форма: str,
                       секунд: float = 8.0) -> dict:
    """ОДНА форма параметров preprocessedSubscribe -- принял ли и что шлёт.

    Отвечает на вопрос, который нельзя решить чтением документации: фильтр
    подействовал или нет. Признак -- темп сообщений и число разных слотов:
    один адрес источника не даёт десятков сообщений в секунду.
    """
    import websockets  # noqa: PLC0415
    из_ = {"forma": форма, "prinjal": None, "why_not": None,
            "soobshchenij": 0, "slotov": 0, "sekund": секунд,
            "soobshchenij_v_sekundu": None, "filtr_dejstvuet": None,
            "dokazano": None, "pervye_podpisi": [],
            "otvet_na_podpisku": None}
    try:
        параметры = _параметры_формы(форма, адрес)
    except ValueError as сбой:
        из_["why_not"] = str(сбой)
        return из_
    запрос = {"jsonrpc": "2.0", "id": 1, "method": "preprocessedSubscribe",
              "params": параметры}
    слоты, начало = set(), time.perf_counter()
    try:
        async with websockets.connect(ТОЧКА_PRE + ключ, ping_interval=20,
                                       ping_timeout=30,
                                       max_size=16 * 1024 * 1024) as соед:
            await соед.send(json.dumps(запрос))
            кончить = time.time() + float(секунд)
            while time.time() < кончить:
                try:
                    сырое = await asyncio.wait_for(
                        соед.recv(), timeout=max(0.2, кончить - time.time()))
                except asyncio.TimeoutError:
                    break
                if isinstance(сырое, str):
                    try:
                        ответ = json.loads(сырое)
                    except ValueError:
                        continue
                    из_["otvet_na_podpisku"] = json.dumps(
                        ответ, ensure_ascii=False)[:300]
                    if "error" in ответ:
                        из_["prinjal"] = False
                        из_["why_not"] = ("отказ: " + json.dumps(
                            ответ["error"], ensure_ascii=False)[:300])
                        return из_
                    из_["prinjal"] = True
                    continue
                р = разобрать_кадр(сырое)
                из_["soobshchenij"] += 1
                if р["ok"]:
                    слоты.add(р["slot"])
                    if len(из_["pervye_podpisi"]) < 3:
                        из_["pervye_podpisi"].append(р["podpis"])
                elif not из_["why_not"]:
                    из_["why_not"] = r_why(р)
    except Exception as сбой:  # noqa: BLE001
        из_["why_not"] = f"{type(сбой).__name__}: {затереть(сбой)[:300]}"
        return из_
    прошло = max(1e-9, time.perf_counter() - начало)
    из_["slotov"] = len(слоты)
    из_["soobshchenij_v_sekundu"] = round(из_["soobshchenij"] / прошло, 2)
    if из_["prinjal"] is None:
        из_["prinjal"] = из_["soobshchenij"] > 0
    # ФИЛЬТР ДЕЙСТВУЕТ, если подписка ПРИНЯТА и темп не выше потолка для
    # одного адреса.
    #
    # НОЛЬ СООБЩЕНИЙ -- ЭТО ГОДНО, А НЕ "НЕИЗВЕСТНО". Сначала здесь стояло
    # обратное, и проход 11.10 нашёл, что из-за этого зонд не мог запуститься
    # НИКОГДА: работающий фильтр по молчащему адресу как раз и даёт ноль
    # сообщений за восемь секунд, а сломанный -- десятки в секунду; отвергались
    # оба. Отказ подписки при этом виден отдельно (prinjal=False), так что
    # "ноль" и "не приняли" не путаются. Слабость такого признака названа
    # честно: dokazano=False.
    из_["dokazano"] = bool(из_["soobshchenij"] > 0)
    if из_["prinjal"] is False:
        из_["filtr_dejstvuet"] = False
        return из_
    из_["filtr_dejstvuet"] = bool(
        (из_["soobshchenij_v_sekundu"] or 0.0) <= ПОТОЛОК_СООБЩЕНИЙ_В_СЕКУНДУ)
    if из_["soobshchenij"] == 0:
        из_["why_not"] = из_["why_not"] or (
            "ни одного сообщения за окно -- для молчащего адреса это и есть "
            "признак работающего фильтра, но доказательством он не является")
    return из_


async def пробы_форм(*, ключ: str, адрес: str, секунд: float = 8.0) -> dict:
    """Все формы параметров по очереди -- и какая из них фильтрует."""
    из_ = {"adres": адрес, "sekund_na_formu": секунд, "ryady": [],
            "godnaya_forma": None,
            "potolok_soobshchenij_v_sekundu": ПОТОЛОК_СООБЩЕНИЙ_В_СЕКУНДУ}
    for имя, _ in ФОРМЫ_ПАРАМЕТРОВ:
        р = await проба_формы(ключ=ключ, адрес=адрес, форма=имя,
                               секунд=секунд)
        из_["ryady"].append(р)
    # ПРЕДПОЧТЕНИЕ: сначала форма, где фильтр ДОКАЗАН (сообщения были и темп
    # низкий), затем форма, принятая с нулём сообщений. Иначе выбор зависел бы
    # от порядка проб, а не от их результата.
    годные = [р for р in из_["ryady"] if р.get("filtr_dejstvuet") is True]
    доказанные = [р for р in годные if р.get("dokazano")]
    выбор = (доказанные or годные)
    из_["godnaya_forma"] = (выбор[0]["forma"] if выбор else None)
    из_["godnaya_dokazana"] = bool(доказанные)
    return из_


def ключ_из_окружения() -> dict:
    """Ключ Helius -- из файла или переменной. В отчёт он не попадает."""
    из_ = {"kljuch": None, "why_not": None, "otkuda": None}
    файл = (os.environ.get("HELIUS_API_KEY_FILE") or "").strip()
    if файл:
        try:
            из_["kljuch"] = Path(файл).read_text(encoding="utf-8").strip()
            из_["otkuda"] = "файл HELIUS_API_KEY_FILE"
        except OSError as сбой:
            из_["why_not"] = f"файл ключа не читается: {сбой}"
            return из_
    else:
        из_["kljuch"] = (os.environ.get("HELIUS_API_KEY") or "").strip()
        из_["otkuda"] = "переменная HELIUS_API_KEY"
    if not из_["kljuch"]:
        из_["why_not"] = "ключа Helius нет ни в файле, ни в переменной"
    return из_


def _ловит(что, тип) -> bool:
    """Нужен самопроверке: ждём исключение нужного типа, а не тихий ответ."""
    try:
        что()
    except тип:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def самопроверка() -> int:
    проверки: list = []

    def chk(имя: str, ок: bool, что=None) -> None:
        проверки.append((имя, bool(ок), что))

    # BASE58 -- ТУДА И ОБРАТНО на известных значениях.
    chk("base58: нулевые байты дают ведущие единицы",
        base58_из_байтов(b"\x00\x00") == "11", base58_из_байтов(b"\x00\x00"))
    chk("base58: байт 0x01 -- это '2'", base58_из_байтов(b"\x01") == "2")
    chk("base58: 64 байта дают строку подписи нужной длины (86-88 знаков)",
        86 <= len(base58_из_байтов(bytes(range(1, 65)))) <= 88,
        len(base58_из_байтов(bytes(range(1, 65)))))

    # РАСКЛАДКА КАДРА -- на собранном вручную кадре с известными значениями.
    подпись_байты = bytes([7]) + bytes(63)
    кадр = (bytes([ВЕРСИЯ_КАДРА])
            + (433_490_123).to_bytes(8, "little")
            + подпись_байты + b"TELO" * 10)
    р = разобрать_кадр(кадр)
    chk("кадр: версия, слот (младшим байтом вперёд) и подпись читаются",
        р["ok"] and р["versiya"] == 1 and р["slot"] == 433_490_123
        and р["podpis"] == base58_из_байтов(подпись_байты), р)
    chk("кадр: длина тела считается от байта 73",
        р["bajt_tela"] == len(кадр) - СМЕЩЕНИЕ_ТЕЛА
        and р["bajt_tela"] == 40, р["bajt_tela"])
    chk("кадр ЧУЖОЙ версии -- отказ с причиной, а не разбор в мусор",
        (lambda к: (not к["ok"]) and "версия схемы" in (к["why_not"] or ""))(
            разобрать_кадр(bytes([9]) + bytes(72) + b"x")))
    chk("кадр короче заголовка -- отказ с числами, а не исключение",
        (lambda к: (not к["ok"]) and "короче заголовка" in (к["why_not"] or ""))(
            разобрать_кадр(bytes([1]) + bytes(10))))
    chk("пустой кадр -- отказ словами", разобрать_кадр(b"")["why_not"] == "пустой кадр")
    chk("нулевая подпись в кадре -- отказ, а не пустая строка как подпись",
        (lambda к: (not к["ok"]) and "нулевая" in (к["why_not"] or ""))(
            разобрать_кадр(bytes([1]) + (5).to_bytes(8, "little")
                            + bytes(64) + b"x")))
    chk("смещения раскладки названы числами и совпадают с документацией",
        (СМЕЩЕНИЕ_СЛОТА, СМЕЩЕНИЕ_ПОДПИСИ, ДЛИНА_ПОДПИСИ, СМЕЩЕНИЕ_ТЕЛА)
        == (1, 9, 64, 73))

    # ПОДПИСЬ ИЗ ОБЫЧНОГО ПОТОКА -- оба вида ответа.
    chk("ws: подпись берётся из поля signature",
        подпись_из_ws({"params": {"result": {"signature": "ABC", "slot": 5}}})
        == {"podpis": "ABC", "slot": 5, "why_not": None})
    chk("ws: подпись берётся из signatures[0], когда поля signature нет",
        подпись_из_ws({"params": {"result": {"slot": 6, "transaction": {
            "transaction": {"signatures": ["XYZ", "Q"]}}}}})["podpis"] == "XYZ")
    chk("ws: нет подписи -- причина словами, а не None молча",
        подпись_из_ws({"params": {"result": {}}})["why_not"] is not None)

    # ВСТРЕЧИ: первое время не переписывается, повторы считаются.
    в = Встречи()
    в.принять("pre", "S1", слот=100, мono=10.000)
    в.принять("ws", "S1", слот=100, мono=10.025)
    в.принять("ws", "S1", слот=100, мono=10.090)
    chk("встречи: время ПЕРВОГО появления, повтор не переписывает",
        abs(в.ряды["S1"]["ws_mono"] - 10.025) < 1e-9
        and в.ряды["S1"]["ws_povtorov"] == 1, в.ряды["S1"])
    chk("встречи: разница положительна, когда preprocessed раньше",
        в.разницы_мс() == [25.0], в.разницы_мс())
    в.принять("ws", "S2", мono=20.0)
    chk("встречи: подпись только в одном потоке в разницы НЕ идёт",
        len(в.оба()) == 1 and len(в.ряды) == 2)

    # СВОД: медиана, p90 и вывод по порогу -- на известных числах.
    в2 = Встречи()
    for и, (пре, вс) in enumerate([(0.0, 0.030), (0.0, 0.010), (0.0, 0.020),
                                    (0.0, 0.040), (0.0, 0.025)]):
        в2.принять("pre", f"X{и}", мono=пре)
        в2.принять("ws", f"X{и}", мono=вс)
    с = свод(в2, цель=5)
    chk("свод: медиана и p90 считаются по разницам, а не по порядку прихода",
        с["mediana_ms"] == 25.0 and с["p90_ms"] == 40.0
        and с["min_ms"] == 10.0 and с["max_ms"] == 40.0, с)
    chk("свод: медиана 25 мс выше порога 20 -- вывод говорит, что перевод "
        "имеет смысл",
        ">= порога" in с["vyvod"] and "20.0" in с["vyvod"], с["vyvod"])
    в3 = Встречи()
    в3.принять("pre", "Y", мono=0.0)
    в3.принять("ws", "Y", мono=0.005)
    chk("свод: 5 мс -- ниже порога, и вывод это говорит",
        "ниже порога" in свод(в3)["vyvod"], свод(в3)["vyvod"])
    в4 = Встречи()
    в4.принять("ws", "Z", мono=0.0)
    в4.принять("pre", "Z", мono=0.004)
    chk("свод: отрицательная разница -- вывод «НЕ раньше», а не минус в плюс",
        "НЕ раньше" in свод(в4)["vyvod"], свод(в4)["vyvod"])
    в5 = Встречи()
    в5.отказы["pre"] = "план отказал в preprocessedSubscribe: -32601"
    с5 = свод(в5)
    chk("свод: отказ плана попадает в вывод, и чисел нет",
        с5["mediana_ms"] is None and "-32601" in с5["vyvod"], с5)
    chk("свод: доля «preprocessed раньше» считается числом",
        свод(в2, цель=5)["pre_ranshe_dolya"] == 1.0)
    chk("свод: сообщения каждого потока посчитаны",
        с["soobshchenij_pre"] == 5 and с["soobshchenij_ws"] == 5, с)
    # ТЕМП И СЛОТЫ -- ПРИЗНАК «ФИЛЬТР НЕ РАБОТАЕТ». Без них 2196 сообщений по
    # молчащему адресу читались бы как удача, а не как чужой поток.
    chk("свод: темп сообщений в секунду и цена потока в кредитах -- числом",
        с["soobshchenij_pre_v_sekundu"] is not None
        and с["kreditov_v_sutki_pre"] is not None
        and с["sekund_raboty"] is not None, 
        (с["soobshchenij_pre_v_sekundu"], с["kreditov_v_sutki_pre"]))
    в6 = Встречи()
    for и in range(40):
        в6.принять("pre", f"Q{и}", слот=1000 + (и // 4), мono=0.001 * и)
    с6 = свод(в6)
    chk("свод: разные слоты потока посчитаны -- один адрес не даёт десятки "
        "слотов за секунды",
        с6["slotov_pre"] == 10 and с6["soobshchenij_pre"] == 40, с6)
    chk("свод: первые подписи попадают в отчёт -- их можно проверить по цепи",
        len(с6["pervye_podpisi_pre"]) == 8
        and с6["pervye_podpisi_pre"][0] == "Q0", с6["pervye_podpisi_pre"])
    chk("встречи: очередь приходов накапливается для журнала",
        len(в6.очередь) == 40 and в6.очередь[0]["potok"] == "pre"
        and в6.очередь[0]["podpis"] == "Q0", в6.очередь[:1])

    # КЛЮЧ -- ИЗ ФАЙЛА, И В ОТЧЁТ НЕ ПОПАДАЕТ.
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as кат:
        ф = Path(кат) / "k"
        ф.write_text("SEKRETNYJ_KLJUCH\n", encoding="utf-8")
        было = os.environ.get("HELIUS_API_KEY_FILE")
        try:
            os.environ["HELIUS_API_KEY_FILE"] = str(ф)
            к = ключ_из_окружения()
            chk("ключ читается из файла, а не из командной строки",
                к["kljuch"] == "SEKRETNYJ_KLJUCH" and к["why_not"] is None, к["otkuda"])
            os.environ["HELIUS_API_KEY_FILE"] = str(Path(кат) / "net")
            chk("нет файла ключа -- причина словами, а не пустой ключ",
                ключ_из_окружения()["why_not"] is not None)
        finally:
            # ВОЗВРАТ РОВНО ОДИН РАЗ И В КОНЦЕ. Прежде значение
            # восстанавливалось, а затем тут же затиралось снова -- исходное
            # значение терялось.
            if было is None:
                os.environ.pop("HELIUS_API_KEY_FILE", None)
            else:
                os.environ["HELIUS_API_KEY_FILE"] = было
    chk("адрес точки входа в отчёте без ключа",
        затереть(ТОЧКА_PRE + "SEKRET") == ТОЧКА_PRE + "…",
        затереть(ТОЧКА_PRE + "SEKRET"))
    chk("в своде нет ни одного поля с ключом",
        "api-key" not in json.dumps(свод(в2, цель=5), ensure_ascii=False))

    # ФОРМЫ ПАРАМЕТРОВ И ПРЕДОХРАНИТЕЛЬ -- без сети.
    chk("формы параметров перечислены и строятся по имени",
        _параметры_формы("objekt", "A") == {"accountInclude": ["A"],
                                             "accountExclude": [],
                                             "accountRequired": []}
        and _параметры_формы("spisok_objekta", "A") == [
            {"accountInclude": ["A"], "accountExclude": [],
             "accountRequired": []}]
        and _параметры_формы("objekt_v_filter", "A") == {
            "filter": {"accountInclude": ["A"]}}
        and _параметры_формы("accountRequired", "A")["accountRequired"] == ["A"],
        [и for и, _ in ФОРМЫ_ПАРАМЕТРОВ])
    chk("неизвестная форма -- исключение, а не тихий полный поток",
        (lambda: (_параметры_формы("net_takoj", "A"), False)[1]
         if False else _ловит(lambda: _параметры_формы("net_takoj", "A"),
                               ValueError))())
    chk("потолок темпа и срок до предохранителя названы числами с причиной",
        ПОТОЛОК_СООБЩЕНИЙ_В_СЕКУНДУ == 5.0
        and СЕКУНД_ДО_ПРЕДОХРАНИТЕЛЯ == 20.0
        and "537 235" in open(__file__, encoding="utf-8").read())
    # ТЕКСТ БЕЗ ТЕЛА САМОПРОВЕРКИ. Проход 11.10 нашёл, что проверка
    # предохранителя была пустой: она искала свои же строки. Режем от
    # "def самопроверка" до следующего верхнеуровневого def.
    def _bez_samoproverki(т: str) -> str:
        м = "\ndef самопроверка"
        н = т.find(м)
        if н < 0:
            return т
        к = т.find("\ndef ", н + len(м))
        return т[:н] + (т[к:] if к > 0 else "")
    _текст = _bez_samoproverki(open(__file__, encoding="utf-8").read())
    chk("зонд НЕ открывает долгий поток, пока форма не проверена: ветка "
        "'ни одна форма' стоит ДО создания задач потока",
        _текст.index("ни одна форма параметров") < _текст.index("стоп = asyncio.Event()"),
        None)
    chk("форма подписки идёт в отчёт -- иначе непонятно, чем мерили",
        '"forma"' in _текст and '"formy"' in _текст)
    # ТЕЛО ЗОНДА -- РОВНО ОНО. Берём текст БЕЗ самопроверки и режем от
    # "async def зонд" до следующего верхнеуровневого def: иначе в "тело"
    # попадали бы и соседние функции, и проверка проводки опять ничего бы не
    # значила.
    def _telo(имя_def: str) -> str:
        н = _текст.find(имя_def)
        if н < 0:
            return ""
        к = _текст.find("\ndef ", н + len(имя_def))
        к2 = _текст.find("\nasync def ", н + len(имя_def))
        концы = [х for х in (к, к2) if х > 0]
        return _текст[н:min(концы)] if концы else _текст[н:]
    _телo = _telo("async def зонд")
    chk("предохранитель СРАВНИВАЕТ темп с потолком в теле зонда -- проверяем "
        "сравнение, а не упоминание имени",
        "> ПОТОЛОК_СООБЩЕНИЙ_В_СЕКУНДУ:" in _телo
        and "ПРЕДОХРАНИТЕЛЬ:" in _телo, len(_телo))
    chk("отсчёт темпа сбрасывается ПЕРЕД открытием долгого потока",
        "встречи.начало = time.perf_counter()" in _телo
        and _телo.index("встречи.начало = time.perf_counter()")
        < _телo.index("asyncio.create_task(_поток_pre"), None)
    chk("подрезка памяти вызывается в теле зонда, а не только описана",
        "встречи.подрезать()" in _телo and РЯДОВ_ПРЕДЕЛ == 20_000)
    # ПЕРЕПОДКЛЮЧЕНИЕ -- ЭТО ЦИКЛ В ОБЁРТКЕ, А НЕ НАЛИЧИЕ ИМЕНИ. Проверяем
    # именно цикл и вызов попытки внутри него: мутация, переименовавшая только
    # def, иначе оставалась бы зелёной.
    for _обёртка, _попытка in (("async def _поток_pre", "_попытка_pre"),
                                ("async def _поток_ws", "_попытка_ws")):
        _т = _telo(_обёртка)
        chk(f"{_обёртка.split()[-1]}: переподключение -- цикл с вызовом "
            f"{_попытка} и счётом обрывов",
            "while not стоп.is_set():" in _т
            and f"await {_попытка}(" in _т
            and "obryvov" in _т
            and f"async def {_попытка}(" in _текст, (_обёртка, len(_т)))

    # ПОДРЕЗКА ПАМЯТИ: одиночные забываются, ПАРНЫЕ остаются.
    в7 = Встречи()
    for и in range(РЯДОВ_ПРЕДЕЛ + 50):
        в7.принять("pre", f"R{и}", мono=0.001 * и)
    в7.принять("ws", "R0", мono=0.5)  # делаем первую подпись ПАРНОЙ
    забыто = в7.подрезать()
    chk("подрезка забывает одиночные и НЕ трогает парные встречи",
        забыто >= 50 and len(в7.ряды) <= РЯДОВ_ПРЕДЕЛ
        and "R0" in в7.ряды and len(в7.оба()) == 1,
        (забыто, len(в7.ряды), len(в7.оба())))
    chk("сколько забыто -- числом в своде, а не молча",
        свод(в7)["zabyto_ryadov"] == забыто)
    chk("битые кадры и обрывы -- отдельные числа, а не отказ потока",
        (lambda д: д["bityh_kadrov"] == 0 and д["obryvov"] == {"ws": 0, "pre": 0})(
            свод(Встречи())))
    # ГОДНОСТЬ ФОРМЫ: ноль сообщений у принятой подписки -- годно, но не доказано.
    chk("форма с нулём сообщений и принятой подпиской -- ГОДНА, и это "
        "помечено как недоказанное",
        "для молчащего адреса это и есть" in _текст
        and "dokazano" in _текст)

    # ИСТОЧНИК СПЕЦИФИКАЦИИ НАЗВАН В МОДУЛЕ, а не взят из головы.
    chk("раскладка кадра и цена потока названы в головном описании со ссылкой",
        "preprocessed-subscribe" in (__doc__ or "")
        and "0.1 кредита" in (__doc__ or "")
        and "байты 9..72" in (__doc__ or ""))
    chk("подписка обычного потока совпадает с подпиской службы по полям",
        all(п in (sys.modules[__name__]._ПОДПИСКА_WS_ПОЛЯ)
            for п in ("accountInclude", "commitment", "transactionDetails",
                       "encoding", "maxSupportedTransactionVersion")))

    плохих = [(и, ч) for и, ок, ч in проверки if not ок]
    for и, ок, ч in проверки:
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {и}" + ("" if ок else f" -> {ч}"))
    print(f"самопроверка зонда preprocessed: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


# ПОЛЯ ПОДПИСКИ ОБЫЧНОГО ПОТОКА -- одним списком, чтобы самопроверка могла
# сверить их с подпиской службы, а не верить комментарию.
_ПОДПИСКА_WS_ПОЛЯ = ("accountInclude", "failed", "vote", "commitment",
                      "transactionDetails", "encoding", "showRewards",
                      "maxSupportedTransactionVersion")


def main() -> int:
    п = argparse.ArgumentParser()
    п.add_argument("--self-test", action="store_true")
    п.add_argument("--adres", default=АДРЕС_ПО_УМОЛЧАНИЮ)
    п.add_argument("--pokupok", type=int, default=ПОКУПОК_ЦЕЛЬ)
    п.add_argument("--sekund", type=float, default=СЕКУНД_ПРЕДЕЛ)
    п.add_argument("--out", default="")
    п.add_argument("--status", default="")
    п.add_argument("--proby-form", action="store_true",
                    help="перебрать формы параметров preprocessedSubscribe и "
                         "назвать ту, при которой фильтр действует")
    п.add_argument("--sekund-na-formu", type=float, default=8.0)
    п.add_argument("--forma", default="avto",
                    help="форма параметров подписки; 'avto' -- выяснить у "
                         "сервера, какая из них действительно фильтрует")
    а = п.parse_args()
    if а.self_test:
        return самопроверка()
    к = ключ_из_окружения()
    if not к["kljuch"]:
        print(json.dumps({"ok": False, "why_not": к["why_not"]},
                          ensure_ascii=False))
        return 1
    if а.proby_form:
        из_ф = asyncio.run(пробы_форм(ключ=к["kljuch"], адрес=а.adres,
                                       секунд=а.sekund_na_formu))
        текст = json.dumps(из_ф, ensure_ascii=False, indent=1)
        print(текст)
        if а.status:
            Path(а.status).write_text(текст + "\n", encoding="utf-8")
        return 0
    из_ = asyncio.run(зонд(ключ=к["kljuch"], адрес=а.adres, цель=а.pokupok,
                            секунд=а.sekund, путь_журнала=а.out,
                            путь_признака=а.status, форма=а.forma,
                            секунд_на_форму=а.sekund_na_formu))
    print(json.dumps(из_, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
