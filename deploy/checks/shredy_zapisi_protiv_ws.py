#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ЗАПИСИ ШРЕДОВ ПРОТИВ WS: доказать, что транзакции из записей -- те же и раньше.

ЧТО ЭТО ОТВЕЧАЕТ. Прокси jito-shredstream-proxy в режиме ForwardOnly собирает из
принятых UDP-шредов записи Solana и отдаёт их по gRPC (`SubscribeEntries`). Вопрос
денежный и один: **лежат ли в этих записях ТЕ ЖЕ транзакции, что приходят нам по WS,
и насколько раньше**. Отсюда главное число проверки:

    подписей WS, которые НАШЛИСЬ в записях: N из M, и опережение записей: медиана / p90

Пока это число не измерено, «шреды работают» -- это то, что горит в журнале прокси, а
не то, что мы можем торговать.

ЧАСЫ ОДНИ. Оба потока (gRPC и WS) метят время ОДНИМИ монотонными часами внутри этого
процесса. Сравнивать настенное время двух источников на миллисекундах нельзя: NTP
правит стенные часы на десятки миллисекунд, и всё опережение утонуло бы.

ЧТО ЧИТАЕТСЯ И ЧЕГО НЕ ХРАНИТСЯ. Из записи берутся ТОЛЬКО подписи транзакций (по 64
байта) и номер слота; тела транзакций, ключи и данные инструкций не разбираются и не
пишутся. Ключ Helius берётся из окружения и наружу не отдаётся -- печатается только
имя переменной. На торговый путь проверка не влияет: отдельный процесс, только
чтение, ни одной записи в состояние исполнителя.

РАЗБОР ЗАПИСЕЙ -- СВОЙ, И ВОТ ПОЧЕМУ ЕГО МОЖНО ПРОВЕРИТЬ БЕЗ СЕТИ. По протоколу
(jito-labs/mev-protos, shredstream.proto) поле `entries` -- это bincode-сериализация
`Vec<solana_entry::entry::Entry>`. Нам нужны только подписи, но чтобы дойти до
следующей транзакции, приходится пройти её сообщение: у Solana это счёт байт, а не
догадка. Раскладка взята из формата, а самопроверка гоняет её на НАСТОЯЩИХ
транзакциях, собранных solders (legacy, v0, v0 с таблицами адресов): разбор обязан
съесть ровно столько байт, сколько их в транзакции, и вернуть ту же подпись, что у
solders. Сойтись случайно это не может.

Запуск на хосте (после того как прокси поднят):
    python3 deploy/checks/shredy_zapisi_protiv_ws.py --grpc 127.0.0.1:9999 --seconds 120
Без сети:
    python3 deploy/checks/shredy_zapisi_protiv_ws.py --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import struct
import sys
import threading
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(КОРЕНЬ / "analysis"))

# Имя метода gRPC -- из shredstream.proto (package shredstream, service
# ShredstreamProxy). Заглушки protoc НЕ НУЖНЫ: запрос SubscribeEntriesRequest
# пустой (ноль байт), а ответ Entry -- два поля, которые разбираются вручную.
МЕТОД_GRPC = "/shredstream.ShredstreamProxy/SubscribeEntries"
# Имена ключа Helius -- те же и в том же порядке, что у проверки Code-1
# (deploy/checks/shred_protiv_ws.py): свой ключ проверок первым.
ИМЕНА_КЛЮЧА = ("HELIUS_API_KEY2", "HELIUS_API_KEY", "HELIUS_API")
ФАЙЛ_ГРУПП = os.environ.get("BLOOM_SOURCE_GROUPS") or "/home/bot/data/sources_2026-09-25.json"


# ------------------------------------------------- разбор записей (без сети)

def short_vec(b: bytes, i: int) -> tuple:
    """compact-u16 Solana (short_vec): 1--3 байта, по 7 бит, старший бит -- «есть ещё»."""
    n = 0
    сдвиг = 0
    while True:
        if i >= len(b):
            raise ValueError("short_vec вышел за конец данных")
        c = b[i]
        i += 1
        n |= (c & 0x7F) << сдвиг
        if not c & 0x80:
            return n, i
        сдвиг += 7
        if сдвиг > 14:
            raise ValueError("short_vec длиннее трёх байт")


def пройти_сообщение(b: bytes, i: int) -> int:
    """Пройти сообщение транзакции и вернуть смещение за ним.

    Старший бит первого байта -- признак версии (0x80 | версия). Версий, кроме 0,
    не бывает; встретив другую, отказываем, а не угадываем длину.
    """
    версия = None
    if b[i] & 0x80:
        версия = b[i] & 0x7F
        i += 1
        if версия != 0:
            raise ValueError(f"версия сообщения {версия} -- разбор не её")
    i += 3                                    # header: 3 байта
    n, i = short_vec(b, i)
    i += 32 * n                               # account_keys
    i += 32                                   # recent_blockhash
    n, i = short_vec(b, i)
    for _ in range(n):                        # instructions
        i += 1                                # program_id_index
        k, i = short_vec(b, i)
        i += k                                # accounts
        k, i = short_vec(b, i)
        i += k                                # data
    if версия == 0:
        n, i = short_vec(b, i)
        for _ in range(n):                    # address_table_lookups
            i += 32
            k, i = short_vec(b, i)
            i += k
            k, i = short_vec(b, i)
            i += k
    if i > len(b):
        raise ValueError("сообщение вышло за конец данных")
    return i


def пройти_транзакцию(b: bytes, i: int) -> tuple:
    """(первая подпись 64 байта, смещение за транзакцией)."""
    n, i = short_vec(b, i)
    if n < 1:
        raise ValueError("транзакция без подписей")
    первая = b[i:i + 64]
    if len(первая) != 64:
        raise ValueError("подпись обрезана")
    i += 64 * n
    return первая, пройти_сообщение(b, i)


def подписи_записей(blob: bytes) -> dict:
    """Подписи всех транзакций из bincode Vec<Entry>.

    bincode по умолчанию пишет длину Vec как u64 LE -- это касается и внешнего
    Vec<Entry>, и Entry.transactions. А вот внутри самой транзакции длины идут
    short_vec: это её собственный формат, и путать их нельзя.
    """
    из_ = {"ok": False, "why_not": None, "записей": 0, "транзакций": 0,
            "подписи": [], "съедено": 0}
    try:
        i = 0
        (записей,) = struct.unpack_from("<Q", blob, i)
        i += 8
        if записей > 100_000:
            raise ValueError(f"записей {записей} -- это не Vec<Entry>")
        for _ in range(записей):
            i += 8 + 32                       # num_hashes u64 + hash [32]
            (сколько,) = struct.unpack_from("<Q", blob, i)
            i += 8
            if сколько > 100_000:
                raise ValueError(f"транзакций в записи {сколько}")
            for _ in range(сколько):
                п, i = пройти_транзакцию(blob, i)
                из_["подписи"].append(п)
            из_["записей"] += 1
        из_.update(ok=True, транзакций=len(из_["подписи"]), съедено=i)
        if i != len(blob):
            из_.update(ok=False,
                        why_not=f"съедено {i} байт из {len(blob)} -- раскладка не сошлась")
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return из_


def base58(b: bytes) -> str:
    """Подпись строкой. Свой кодер: сторонней base58 в окружении службы нет."""
    алф = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = алф[r] + s
    ведущие = 0
    for x in b:
        if x != 0:
            break
        ведущие += 1
    return "1" * ведущие + s


# ------------------------------------------------- разбор protobuf Entry

def разобрать_entry(сырое: bytes) -> dict:
    """Entry из shredstream.proto: 1 slot (varint), 2 entries (bytes). Без protoc.

    Полей всего два, и оба обязательны для нас; неизвестные поля пропускаются по
    их типу, как велит protobuf, а не считаются ошибкой -- иначе новое поле в
    протоколе сломало бы проверку на пустом месте.
    """
    из_ = {"ok": False, "why_not": None, "slot": None, "entries": b""}
    i = 0
    try:
        while i < len(сырое):
            тег = 0
            сдвиг = 0
            while True:
                c = сырое[i]
                i += 1
                тег |= (c & 0x7F) << сдвиг
                if not c & 0x80:
                    break
                сдвиг += 7
            номер, вид = тег >> 3, тег & 7
            if вид == 0:                       # varint
                зн = 0
                сдвиг = 0
                while True:
                    c = сырое[i]
                    i += 1
                    зн |= (c & 0x7F) << сдвиг
                    if not c & 0x80:
                        break
                    сдвиг += 7
                if номер == 1:
                    из_["slot"] = зн
            elif вид == 2:                     # length-delimited
                длина = 0
                сдвиг = 0
                while True:
                    c = сырое[i]
                    i += 1
                    длина |= (c & 0x7F) << сдвиг
                    if not c & 0x80:
                        break
                    сдвиг += 7
                if i + длина > len(сырое):
                    # ОБРЕЗАННОЕ ПОЛЕ -- ОТКАЗ, А НЕ КОРОТКОЕ ТЕЛО. Срез в Python
                    # молча отдал бы что есть, и обрезанная пачка записей ушла бы
                    # в разбор как настоящая: там она дала бы «сбой раскладки» и
                    # выглядела бы ошибкой разбора записей, а не обрыва потока.
                    raise ValueError(f"поле {номер}: объявлено {длина} байт, "
                                      f"осталось {len(сырое) - i}")
                тело = сырое[i:i + длина]
                i += длина
                if номер == 2:
                    из_["entries"] = тело
            elif вид == 5:
                i += 4
            elif вид == 1:
                i += 8
            else:
                raise ValueError(f"поле {номер}: неизвестный вид {вид}")
        из_["ok"] = True
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return из_


# ------------------------------------------------------------- замер

class Замер:
    """Первое время появления каждой подписи, ОДНИМИ монотонными часами."""

    def __init__(self) -> None:
        self.лок = threading.Lock()
        self.записи = {}          # подпись -> монотонное время
        self.ws = {}              # подпись -> монотонное время
        self.слоты = set()
        self.пачек = 0
        self.байт = 0
        self.сбоев_разбора = 0
        self.причины = {}

    def из_записей(self, подписи: list, слот, моно: float) -> None:
        with self.лок:
            if слот is not None:
                self.слоты.add(int(слот))
            for п in подписи:
                self.записи.setdefault(п, моно)

    def из_ws(self, подпись: str, моно: float) -> None:
        with self.лок:
            self.ws.setdefault(подпись, моно)

    def сбой(self, почему: str) -> None:
        with self.лок:
            self.сбоев_разбора += 1
            self.причины[почему[:90]] = self.причины.get(почему[:90], 0) + 1

    def свод(self) -> dict:
        with self.лок:
            общие = [п for п in self.ws if п in self.записи]
            опережение = [self.ws[п] - self.записи[п] for п in общие]
            из_ = {"пачек_записей": self.пачек, "байт_записей": self.байт,
                    "слотов": len(self.слоты),
                    "транзакций_в_записях": len(self.записи),
                    "подписей_ws": len(self.ws),
                    "подписей_ws_нашлись_в_записях": len(общие),
                    "сбоев_разбора": self.сбоев_разбора,
                    "причины_сбоев": dict(sorted(self.причины.items(),
                                                  key=lambda x: -x[1])[:3])}
            if опережение:
                р = sorted(опережение)
                из_.update(
                    опережение_медиана_мс=round(statistics.median(р) * 1000, 2),
                    опережение_p90_мс=round(р[min(len(р) - 1,
                                                   int(round(0.9 * (len(р) - 1))))] * 1000, 2),
                    опережение_мин_мс=round(р[0] * 1000, 2),
                    опережение_макс_мс=round(р[-1] * 1000, 2),
                    записи_раньше_ws=sum(1 for x in р if x > 0))
            return из_


# ------------------------------------------------------------- потоки

def поток_grpc(замер: Замер, адрес: str, стоп: threading.Event) -> None:
    """SubscribeEntries: пустой запрос, поток Entry. Байты в байтах, без protoc."""
    try:
        import grpc  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        замер.сбой(f"grpcio не установлен: {type(exc).__name__}")
        return
    try:
        канал = grpc.insecure_channel(адрес)
        зов = канал.unary_stream(МЕТОД_GRPC,
                                  request_serializer=lambda _: b"",
                                  response_deserializer=lambda b: b)
        for сырое in зов(None):
            if стоп.is_set():
                break
            моно = time.monotonic()
            замер.пачек += 1
            замер.байт += len(сырое)
            e = разобрать_entry(сырое)
            if not e["ok"]:
                замер.сбой(f"protobuf: {e['why_not']}")
                continue
            р = подписи_записей(e["entries"])
            if not р["ok"]:
                замер.сбой(f"записи: {р['why_not']}")
                continue
            замер.из_записей([base58(п) for п in р["подписи"]], e["slot"], моно)
    except Exception as exc:  # noqa: BLE001
        замер.сбой(f"gRPC: {type(exc).__name__}: {str(exc)[:90]}")


def адреса_источников(файл: str, предел: int) -> list:
    """Наши источники из файла групп. Списка в коде нет намеренно (как у Code-1)."""
    try:
        д = json.loads(open(файл, encoding="utf-8").read())
    except Exception:  # noqa: BLE001
        return []
    из_ = []
    for _, г in (д.get("groups") or {}).items():
        адреса = (г or {}).get("addresses")
        if isinstance(адреса, dict):
            из_ += list(адреса)
        elif isinstance(адреса, list):
            из_ += [(з.get("address") if isinstance(з, dict) else з) for з in адреса]
        for поле in ("by_signal", "snipers"):
            for з in (г or {}).get(поле) or []:
                из_.append(з.get("address") if isinstance(з, dict) else з)
    return [а for а in dict.fromkeys(из_) if а][:предел]


def имя_и_ключ() -> tuple:
    for имя in ИМЕНА_КЛЮЧА:
        зн = (os.environ.get(имя) or "").strip()
        if зн:
            return имя, зн
    return "не задан", ""


def поток_ws(замер: Замер, источники: list, стоп: threading.Event) -> None:
    """transactionSubscribe по нашим источникам -- тот же запрос, что у слушателя."""
    имя, ключ = имя_и_ключ()
    if not ключ:
        замер.сбой(f"ключ Helius не задан (искали {', '.join(ИМЕНА_КЛЮЧА)})")
        return
    if not источники:
        замер.сбой("источников не нашлось -- сравнивать не с чем")
        return
    try:
        import asyncio  # noqa: PLC0415

        import websockets  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        замер.сбой(f"websockets не установлен: {type(exc).__name__}")
        return
    url = f"wss://mainnet.helius-rpc.com/?api-key={ключ}"

    async def жить() -> None:
        async with websockets.connect(url, ping_interval=20,
                                       max_queue=4096) as ws:
            for и, адрес in enumerate(источники, start=10):
                await ws.send(json.dumps(
                    {"jsonrpc": "2.0", "id": и, "method": "transactionSubscribe",
                     "params": [{"accountInclude": [адрес], "failed": False},
                                 {"commitment": "processed", "encoding": "jsonParsed",
                                  "transactionDetails": "signatures",
                                  "maxSupportedTransactionVersion": 0}]}))
            while not стоп.is_set():
                try:
                    сырое = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                моно = time.monotonic()
                try:
                    с = json.loads(сырое)
                except ValueError:
                    continue
                зн = ((с.get("params") or {}).get("result") or {})
                подпись = (зн.get("signature")
                            or ((зн.get("transaction") or {}).get("transaction") or {})
                            .get("signatures", [None])[0])
                if isinstance(подпись, str) and подпись:
                    замер.из_ws(подпись, моно)

    try:
        asyncio.run(жить())
    except Exception as exc:  # noqa: BLE001
        замер.сбой(f"WS: {type(exc).__name__}: {str(exc)[:90]}")


# ------------------------------------------------------------- печать

def печать(с: dict, *, имя_ключа: str, источников: int, секунд: int) -> None:
    print("=== записи шредов против WS ===")
    print(f"  окно {секунд} с; ключ Helius из переменной {имя_ключа}; "
          f"источников в подписке {источников}")
    print(f"  записей получено: пачек {с['пачек_записей']}, байт {с['байт_записей']}, "
          f"слотов {с['слоты'] if 'слоты' in с else с['слотов']}, "
          f"транзакций в записях {с['транзакций_в_записях']}")
    м = с["подписей_ws_нашлись_в_записях"]
    всего = с["подписей_ws"]
    доля = f"{(100.0 * м / всего):.1f} %" if всего else "нет подписей WS"
    print(f"  ГЛАВНОЕ ЧИСЛО -- подписей WS нашлись в записях: {м} из {всего} ({доля})")
    if "опережение_медиана_мс" in с:
        print(f"  опережение записей над WS: медиана {с['опережение_медиана_мс']} мс, "
              f"p90 {с['опережение_p90_мс']} мс, мин {с['опережение_мин_мс']}, "
              f"макс {с['опережение_макс_мс']}; записи раньше WS у "
              f"{с['записи_раньше_ws']} из {м}")
    else:
        print("  опережение не посчитано: пересечения подписей нет")
    if с["сбоев_разбора"]:
        print(f"  сбоев разбора {с['сбоев_разбора']}: {с['причины_сбоев']}")
    if не_состоялось(с):
        print("  НЕ СОСТОЯЛОСЬ: числа нет. Причина выше -- смотри сбои разбора; "
              "пока пересечения нет, «записи идут» НЕ доказано")


def не_состоялось(с: dict) -> bool:
    return not с.get("подписей_ws_нашлись_в_записях")


# ------------------------------------------------------------- самопроверка

def self_test() -> int:  # noqa: C901
    пройдено = провалено = 0

    def chk(что, ок, факт=None):
        nonlocal пройдено, провалено
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {что}"
              + (f" -> {факт!r}" if факт is not None and not ок else ""))
        пройдено += bool(ок)
        провалено += (not ок)

    # --- short_vec
    chk("short_vec: 1 байт", short_vec(bytes([5]), 0) == (5, 1))
    chk("short_vec: 2 байта (0x80 0x01 = 128)",
        short_vec(bytes([0x80, 0x01]), 0) == (128, 2))
    chk("short_vec: 3 байта (0x80 0x80 0x01 = 16384)",
        short_vec(bytes([0x80, 0x80, 0x01]), 0) == (16384, 3))
    try:
        short_vec(bytes([0x80, 0x80, 0x80, 0x01]), 0)
        чет = False
    except ValueError:
        чет = True
    chk("short_vec длиннее трёх байт -- отказ, а не молчаливое число", чет)

    # --- protobuf Entry
    тело = b"\x08\xd2\x09\x12\x03abc"          # поле 1 varint 1234, поле 2 bytes "abc"
    e = разобрать_entry(тело)
    chk("protobuf Entry: slot и entries разобраны",
        e["ok"] and e["slot"] == 1234 and e["entries"] == b"abc", e)
    чужое = b"\x08\x01\x1a\x02zz\x12\x01x"     # неизвестное поле 3 -- пропустить
    e2 = разобрать_entry(чужое)
    chk("неизвестное поле протокола пропускается, а не ломает разбор",
        e2["ok"] and e2["slot"] == 1 and e2["entries"] == b"x", e2)
    chk("обрезанный protobuf -- отказ словами",
        разобрать_entry(b"\x12\x10ab")["ok"] is False)

    # --- base58
    chk("base58 ведущие нули -- единицы",
        base58(b"\x00\x00\x01") == "112")

    # --- РАЗБОР НА НАСТОЯЩИХ ТРАНЗАКЦИЯХ solders
    try:
        from solders.address_lookup_table_account import AddressLookupTableAccount  # noqa: PLC0415
        from solders.hash import Hash  # noqa: PLC0415
        from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
        from solders.keypair import Keypair  # noqa: PLC0415
        from solders.message import Message, MessageV0  # noqa: PLC0415
        from solders.pubkey import Pubkey  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415

        кп = Keypair.from_seed(bytes([3] * 32))
        прог = Pubkey(bytes([9] * 32))
        бх = Hash(bytes([5] * 32))
        их1 = Instruction(прог, b"\x01\x02\x03",
                           [AccountMeta(кп.pubkey(), True, True),
                            AccountMeta(Pubkey(bytes([4] * 32)), False, False)])
        табл = AddressLookupTableAccount(key=Pubkey(bytes([11] * 32)),
                                          addresses=[Pubkey(bytes([12] * 32)),
                                                      Pubkey(bytes([13] * 32))])
        их2 = Instruction(прог, bytes(range(40)),
                           [AccountMeta(Pubkey(bytes([12] * 32)), False, True),
                            AccountMeta(кп.pubkey(), True, True)])
        случаи = {
            "legacy": VersionedTransaction(
                Message.new_with_blockhash([их1], кп.pubkey(), бх), [кп]),
            "v0 без таблиц": VersionedTransaction(
                MessageV0.try_compile(кп.pubkey(), [их1], [], бх), [кп]),
            "v0 с таблицей адресов": VersionedTransaction(
                MessageV0.try_compile(кп.pubkey(), [их2], [табл], бх), [кп]),
            "v0 две инструкции": VersionedTransaction(
                MessageV0.try_compile(кп.pubkey(), [их1, их2], [табл], бх), [кп]),
        }
        for имя, т in случаи.items():
            b = bytes(т)
            п, к = пройти_транзакцию(b, 0)
            chk(f"разбор транзакции ({имя}): съедено ровно {len(b)} и подпись та же",
                к == len(b) and base58(п) == str(т.signatures[0]),
                (к, len(b), base58(п), str(т.signatures[0])))

        # --- Vec<Entry> целиком: пустая запись посередине не должна ломать счёт
        def blob(пачки):
            out = struct.pack("<Q", len(пачки))
            for txs in пачки:
                out += struct.pack("<Q", 7) + bytes([6]) * 32
                out += struct.pack("<Q", len(txs))
                for т in txs:
                    out += bytes(т)
            return out

        b = blob([[случаи["v0 без таблиц"], случаи["legacy"]], [],
                   [случаи["v0 с таблицей адресов"]]])
        р = подписи_записей(b)
        ждём = [str(случаи["v0 без таблиц"].signatures[0]),
                str(случаи["legacy"].signatures[0]),
                str(случаи["v0 с таблицей адресов"].signatures[0])]
        chk("Vec<Entry>: три записи (одна пустая), подписи и порядок те же",
            р["ok"] and р["записей"] == 3 and р["транзакций"] == 3
            and [base58(x) for x in р["подписи"]] == ждём, р.get("why_not") or р)
        chk("Vec<Entry> съеден БЕЗ ОСТАТКА -- иначе раскладка не сошлась",
            р["съедено"] == len(b))
        chk("лишний байт в конце -- ОТКАЗ, а не «почти сошлось»",
            подписи_записей(b + b"\x00")["ok"] is False)
        chk("обрезанный Vec<Entry> -- отказ словами, без исключения наружу",
            подписи_записей(b[:len(b) - 20])["ok"] is False)
        chk("пустой Vec<Entry> -- ноль транзакций и это НЕ сбой",
            (lambda р_: р_["ok"] and р_["транзакций"] == 0)(
                подписи_записей(struct.pack("<Q", 0))))
    except Exception as exc:  # noqa: BLE001
        chk("разбор проверен на настоящих транзакциях solders", False,
            f"{type(exc).__name__}: {exc}")

    # --- ЗАМЕР: часы одни, первое время не перетирается
    з = Замер()
    з.из_записей(["A", "B"], 100, 1.000)
    з.из_записей(["A"], 100, 1.500)
    з.из_ws("A", 1.250)
    з.из_ws("C", 1.300)
    с = з.свод()
    chk("первое время появления не перетирается вторым",
        с["транзакций_в_записях"] == 2 and с["подписей_ws"] == 2
        and с["подписей_ws_нашлись_в_записях"] == 1, с)
    chk("опережение считается только по пересечению и в мс",
        с["опережение_медиана_мс"] == 250.0 and с["записи_раньше_ws"] == 1, с)
    chk("нет пересечения -- «не состоялось», а не ноль опережения",
        не_состоялось(Замер().свод()) is True)
    chk("слот записи запомнен", с["слотов"] == 1)

    # --- ИМЯ КЛЮЧА, А НЕ КЛЮЧ
    было = {и: os.environ.pop(и, None) for и in ИМЕНА_КЛЮЧА}
    try:
        chk("без ключа имя переменной -- «не задан», значение не печатается",
            имя_и_ключ() == ("не задан", ""))
        os.environ[ИМЕНА_КЛЮЧА[1]] = "вторичный"
        os.environ[ИМЕНА_КЛЮЧА[0]] = "свой-ключ-проверок"
        chk("свой ключ проверок берётся ПЕРВЫМ",
            имя_и_ключ()[0] == ИМЕНА_КЛЮЧА[0])
    finally:
        for и, зн in было.items():
            os.environ.pop(и, None)
            if зн is not None:
                os.environ[и] = зн

    print(f"самопроверка записей против WS: {пройдено}/{пройдено + провалено} пройдено")
    return 0 if провалено == 0 else 1


def main() -> int:
    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--grpc", default="127.0.0.1:9999",
                   help="адрес gRPC прокси (только localhost: наружу он не открыт)")
    р.add_argument("--seconds", type=int, default=120)
    р.add_argument("--groups-file", default=ФАЙЛ_ГРУПП)
    р.add_argument("--max-sources", type=int,
                   default=int(os.environ.get("SHRED_MAX_SOURCES", "70")))
    р.add_argument("--out", default=None)
    р.add_argument("--self-test", dest="self_test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return self_test()
    источники = адреса_источников(а.groups_file, а.max_sources)
    замер = Замер()
    стоп = threading.Event()
    потоки = [threading.Thread(target=поток_grpc, args=(замер, а.grpc, стоп),
                                daemon=True),
              threading.Thread(target=поток_ws, args=(замер, источники, стоп),
                                daemon=True)]
    for т in потоки:
        т.start()
    время_конца = time.monotonic() + max(5, int(а.seconds))
    while time.monotonic() < время_конца:
        time.sleep(0.5)
    стоп.set()
    с = замер.свод()
    печать(с, имя_ключа=имя_и_ключ()[0], источников=len(источники),
            секунд=int(а.seconds))
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(с, ф, ensure_ascii=False, indent=1)
        print(f"  отчёт: {а.out}")
    return 0 if not не_состоялось(с) else 2


if __name__ == "__main__":
    raise SystemExit(main())
