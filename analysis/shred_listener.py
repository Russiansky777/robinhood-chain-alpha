#!/usr/bin/env python3
"""Слушатель шредов: когда слот пришёл по UDP и когда его увидел WS Helius.

ЗАЧЕМ (пункт 4 владельца 26.09). Владелец покупает день BlockRazor (Amsterdam),
шреды идут UDP на наш хост. Вопрос один и денежный: на сколько WS Helius
ОТСТАЁТ от шредов. Отсюда и замер: по каждому слоту время первого и последнего
шреда и время, когда тот же слот впервые появился в WS -- по всем слотам и
отдельно по слотам с НАШИМИ сигналами.

ЧЕГО ЗДЕСЬ НЕТ. Сырые шреды не хранятся вовсе: из заголовка берутся только слот
и индекс, остальное отбрасывается сразу. Подписи, тела транзакций, ключи -- не
читаются и не пишутся. На торговый путь служба не влияет: отдельный процесс,
низкий приоритет, свой каталог, ни одной записи в состояние исполнителя.

ЧАСЫ ОДНИ. Все три потока (UDP, slotSubscribe, transactionSubscribe) метят
время ОДНИМИ монотонными часами внутри этого процесса: сравнивать настенное
время двух источников на миллисекундах нельзя.

ЗАГОЛОВОК ШРЕДА. Первые 64 байта -- подпись, затем байт разновидности, затем
слот (u64 LE, смещение 65) и индекс (u32 LE, смещение 73). Больше ничего не
разбирается.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import threading
import time
from collections import OrderedDict

СМЕЩЕНИЕ_СЛОТА = 65
СМЕЩЕНИЕ_ИНДЕКСА = 73
МИНИМУМ_ПАКЕТА = 83
# Сколько слотов держать в памяти. Слот идёт ~0.4 с, 20 000 слотов -- это больше
# двух часов; при сбросе на диск старые уходят из памяти.
ПРЕДЕЛ_СЛОТОВ = 20_000
ОТСТАВАНИЕ_СБРОСА = 300  # слотов: насколько позади слота WS пишем на диск
ОКНО_СЛОТА = 2000  # насколько слот из шреда может отличаться от слота WS


def разобрать_шред(пакет: bytes) -> tuple:
    """(слот, индекс) или (None, None). Ничего больше из пакета не берём."""
    if not пакет or len(пакет) < МИНИМУМ_ПАКЕТА:
        return None, None
    try:
        слот = struct.unpack_from("<Q", пакет, СМЕЩЕНИЕ_СЛОТА)[0]
        индекс = struct.unpack_from("<I", пакет, СМЕЩЕНИЕ_ИНДЕКСА)[0]
    except struct.error:
        return None, None
    return слот, индекс


class Замер:
    """Состояние замера: по слоту -- времена, счётчики. Без сырых данных."""

    def __init__(self, каталог: str) -> None:
        self.каталог = каталог
        os.makedirs(каталог, exist_ok=True)
        self.замок = threading.Lock()
        self.слоты: OrderedDict = OrderedDict()
        self.шредов = 0
        self.пакетов_мимо = 0
        self.байт = 0
        self.слот_ws = None
        self.ws_сообщений = 0
        self.ws_сигналов = 0
        self.ws_обрывов = 0
        self.t0_моно = time.monotonic()
        self.t0_стенных = time.time()
        self.путь_слотов = os.path.join(каталог, "slots.jsonl")
        self.путь_признака = os.path.join(каталог, "heartbeat.json")

    def _запись(self, слот: int) -> dict:
        з = self.слоты.get(слот)
        if з is None:
            з = {"slot": слот, "shreds": 0, "first_mono": None, "last_mono": None,
                 "index_min": None, "index_max": None,
                 "ws_slot_mono": None, "ws_signal_mono": None}
            self.слоты[слот] = з
            if len(self.слоты) > ПРЕДЕЛ_СЛОТОВ:
                старый, стар_з = self.слоты.popitem(last=False)
                self._сбросить(стар_з)
        return з

    def шред(self, слот: int, индекс: int, моно: float, размер: int) -> None:
        with self.замок:
            self.шредов += 1
            self.байт += размер
            if self.слот_ws is not None and abs(слот - self.слот_ws) > ОКНО_СЛОТА:
                self.пакетов_мимо += 1
                return
            з = self._запись(слот)
            з["shreds"] += 1
            if з["first_mono"] is None:
                з["first_mono"] = моно
            з["last_mono"] = моно
            з["index_min"] = индекс if з["index_min"] is None else min(з["index_min"], индекс)
            з["index_max"] = индекс if з["index_max"] is None else max(з["index_max"], индекс)

    def ws_слот(self, слот: int, моно: float) -> None:
        with self.замок:
            self.ws_сообщений += 1
            self.слот_ws = слот
            з = self._запись(слот)
            if з["ws_slot_mono"] is None:
                з["ws_slot_mono"] = моно

    def ws_сигнал(self, слот: int, моно: float) -> None:
        with self.замок:
            self.ws_сигналов += 1
            з = self._запись(слот)
            if з["ws_signal_mono"] is None:
                з["ws_signal_mono"] = моно

    def _сбросить(self, з: dict) -> None:
        try:
            with open(self.путь_слотов, "a", encoding="utf-8") as ф:
                ф.write(json.dumps(з, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def сбросить_старые(self, младше_слота: int) -> int:
        """Слоты старше порога уходят на диск: в памяти держим только свежие."""
        сброшено = 0
        with self.замок:
            for слот in list(self.слоты):
                if слот < младше_слота:
                    self._сбросить(self.слоты.pop(слот))
                    сброшено += 1
        return сброшено

    def признак(self) -> dict:
        with self.замок:
            покрыто = len(self.слоты)
            с_шредами = sum(1 for з in self.слоты.values() if з["shreds"])
            с_ws = sum(1 for з in self.слоты.values() if з["ws_slot_mono"] is not None)
            с_сигналом = sum(1 for з in self.слоты.values() if з["ws_signal_mono"] is not None)
            из_ = {"updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "uptime_s": round(time.monotonic() - self.t0_моно, 1),
                    "shreds": self.шредов, "bytes": self.байт,
                    "packets_out_of_window": self.пакетов_мимо,
                    "slots_in_memory": покрыто, "slots_with_shreds": с_шредами,
                    "slots_with_ws": с_ws, "slots_with_our_signal": с_сигналом,
                    "ws_messages": self.ws_сообщений, "ws_signals": self.ws_сигналов,
                    "ws_breaks": self.ws_обрывов, "last_ws_slot": self.слот_ws,
                    "mono_to_wall_offset_s": round(self.t0_стенных - self.t0_моно, 6)}
        try:
            врем = self.путь_признака + ".tmp"
            with open(врем, "w", encoding="utf-8") as ф:
                json.dump(из_, ф, ensure_ascii=False, indent=1)
            os.replace(врем, self.путь_признака)
        except OSError:
            pass
        return из_


def поток_udp(замер: Замер, порт: int, стоп: threading.Event) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
    except OSError:
        pass
    s.bind(("0.0.0.0", порт))
    s.settimeout(1.0)
    буфер = bytearray(2048)
    while not стоп.is_set():
        try:
            размер, _ = s.recvfrom_into(буфер)
        except socket.timeout:
            continue
        except OSError:
            time.sleep(0.05)
            continue
        моно = time.monotonic()
        слот, индекс = разобрать_шред(bytes(буфер[:размер]))
        if слот is None:
            замер.пакетов_мимо += 1
            continue
        замер.шред(слот, индекс, моно, размер)
    s.close()


def адреса_источников(файл_групп: str, предел: int) -> list:
    """Наши источники из файла групп. Списка в коде нет намеренно."""
    try:
        д = json.loads(open(файл_групп, encoding="utf-8").read())
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
    из_ = [а for а in dict.fromkeys(из_) if а]
    return из_[:предел]


def поток_ws(замер: Замер, url: str, источники: list, стоп: threading.Event) -> None:
    """Часы WS рядом с часами шредов: slotSubscribe, slotsUpdates и наши источники.

    БИБЛИОТЕКА -- websockets (асинхронная), та же, что у детектора и в
    requirements. Первая версия этого потока звала синхронный websocket-client,
    которого на хосте нет: поток умирал на импорте МОЛЧА (импорт стоял выше
    try), признак жизни показывал ws_messages 0 и ws_breaks 0, и замер 18:11Z
    собрал 586 287 шредов без единой отметки WS -- то есть был бесполезен.
    Поэтому импорт теперь внутри try, а сбой виден и в журнале, и счётчиком.
    """
    import asyncio  # noqa: PLC0415

    try:
        import websockets  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        замер.ws_обрывов += 1
        print(f"СБОЙ: нет модуля websockets ({type(exc).__name__}) -- часов WS не будет",
              file=sys.stderr, flush=True)
        return

    подписки = {}

    async def подписаться(ws) -> None:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                                   "method": "slotSubscribe", "params": []}))
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2,
                                   "method": "slotsUpdatesSubscribe", "params": []}))
        for и, адрес in enumerate(источники, start=10):
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": и,
                                       "method": "transactionSubscribe",
                                       "params": [{"accountInclude": [адрес],
                                                    "failed": False},
                                                   {"commitment": "processed",
                                                    "encoding": "jsonParsed",
                                                    "transactionDetails": "signatures",
                                                    "maxSupportedTransactionVersion": 0}]}))

    def разобрать(сырое: str, моно: float) -> None:
        try:
            с = json.loads(сырое)
        except ValueError:
            return
        метод = с.get("method") or ""
        рез = ((с.get("params") or {}).get("result") or {})
        слот = рез.get("slot") if isinstance(рез, dict) else None
        if метод in ("slotNotification", "slotsUpdatesNotification"):
            if isinstance(слот, int):
                замер.ws_слот(слот, моно)
        elif метод == "transactionNotification":
            if isinstance(слот, int):
                замер.ws_сигнал(слот, моно)
        elif isinstance(с.get("result"), int):
            подписки[с.get("id")] = с["result"]

    async def круг() -> None:
        while not стоп.is_set():
            try:
                async with websockets.connect(url, ping_interval=20,
                                              ping_timeout=20,
                                              max_size=8 * 1024 * 1024) as ws:
                    await подписаться(ws)
                    print(f"WS подключён, подписок на источники: {len(источники)}",
                          file=sys.stderr, flush=True)
                    while not стоп.is_set():
                        сырое = await asyncio.wait_for(ws.recv(), timeout=45)
                        разобрать(сырое, time.monotonic())
            except Exception as exc:  # noqa: BLE001
                замер.ws_обрывов += 1
                print(f"WS обрыв ({type(exc).__name__}) -- переподключаюсь",
                      file=sys.stderr, flush=True)
                await asyncio.sleep(2)

    try:
        asyncio.run(круг())
    except Exception as exc:  # noqa: BLE001
        замер.ws_обрывов += 1
        print(f"СБОЙ потока WS: {type(exc).__name__}", file=sys.stderr, flush=True)


def зависимости() -> int:
    """Есть ли на этом питоне ровно то, что зовёт служба. Печатает и падает."""
    плохо = []
    for имя in ("websockets",):
        try:
            __import__(имя)
            print(f"{имя}: есть")
        except Exception as exc:  # noqa: BLE001
            плохо.append(f"{имя} ({type(exc).__name__})")
    if плохо:
        print("СБОЙ: не хватает " + ", ".join(плохо), file=sys.stderr)
        return 1
    return 0


def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    # РАЗБОР ЗАГОЛОВКА. Собираем пакет руками: подпись 64 байта, байт
    # разновидности, слот, индекс -- и проверяем, что берутся именно они.
    пакет = bytearray(b"\x01" * 64 + b"\xa5" + struct.pack("<Q", 450_666_873)
                      + struct.pack("<I", 17) + b"\x00" * 10)
    слот, индекс = разобрать_шред(bytes(пакет))
    chk("слот и индекс берутся из заголовка", слот == 450_666_873 and индекс == 17,
        (слот, индекс))
    chk("короткий пакет -- отказ, а не мусорный слот",
        разобрать_шред(b"\x00" * 40) == (None, None), разобрать_шред(b"\x00" * 40))
    chk("пустой пакет -- отказ", разобрать_шред(b"") == (None, None))

    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as д:
        з = Замер(д)
        з.ws_слот(1000, 10.0)
        з.шред(1000, 0, 9.5, 1200)
        з.шред(1000, 1, 9.7, 1200)
        з.ws_сигнал(1000, 10.2)
        с = з.слоты[1000]
        chk("первый и последний шред слота -- по монотонным часам",
            с["first_mono"] == 9.5 and с["last_mono"] == 9.7, с)
        chk("время WS по слоту записано один раз", с["ws_slot_mono"] == 10.0, с)
        chk("время нашего сигнала записано отдельно", с["ws_signal_mono"] == 10.2, с)
        chk("шреды посчитаны, сырых данных нет",
            с["shreds"] == 2 and "payload" not in с and "data" not in с, с)
        # Слот далеко от WS -- пакет мимо окна, в замер не идёт.
        з.шред(1000 + ОКНО_СЛОТА + 5, 0, 11.0, 1200)
        chk("слот вне окна не попадает в замер и посчитан отдельно",
            з.пакетов_мимо == 1 and (1000 + ОКНО_СЛОТА + 5) not in з.слоты,
            (з.пакетов_мимо, len(з.слоты)))
        пр = з.признак()
        chk("признак жизни считает шреды и слоты",
            пр["shreds"] == 3 and пр["slots_with_shreds"] == 1
            and пр["slots_with_our_signal"] == 1, пр)
        сброшено = з.сбросить_старые(1001)
        chk("старые слоты уходят на диск строкой JSON", сброшено == 1
            and os.path.exists(з.путь_слотов), сброшено)

    плохо = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}" + ("" if ок else f"  -> {факт}"))
    print(f"самопроверка слушателя шредов: {len(проверки) - len(плохо)}/{len(проверки)} пройдено")
    return 0 if not плохо else 1


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--zavisimosti", action="store_true",
                   help="проверить модули, которые зовёт служба")
    р.add_argument("--port", type=int,
                   default=int(os.environ.get("SHRED_UDP_PORT", "0")))
    р.add_argument("--out-dir", default=os.environ.get("SHRED_OUT_DIR")
                   or "/home/bot/shred_probe_data")
    р.add_argument("--ws-url", default=os.environ.get("SHRED_WS_URL") or "")
    р.add_argument("--groups-file", default=os.environ.get("BLOOM_SOURCE_GROUPS")
                   or "/home/bot/data/sources_2026-09-25.json")
    р.add_argument("--max-sources", type=int,
                   default=int(os.environ.get("SHRED_MAX_SOURCES", "70")))
    р.add_argument("--seconds", type=float,
                   default=float(os.environ.get("SHRED_SECONDS", "0")),
                   help="0 -- работать до сигнала остановки")
    а = р.parse_args()
    if а.self_test:
        return self_test()
    if а.zavisimosti:
        return зависимости()
    if not а.port:
        print("СТОП: порт UDP не задан (SHRED_UDP_PORT или --port)", file=sys.stderr)
        return 2
    ключ = (os.environ.get("HELIUS_API_KEY") or "").strip()
    url = а.ws_url or (f"wss://atlas-mainnet.helius-rpc.com/?api-key={ключ}" if ключ else "")
    замер = Замер(а.out_dir)
    стоп = threading.Event()
    потоки = [threading.Thread(target=поток_udp, args=(замер, а.port, стоп), daemon=True)]
    источники = адреса_источников(а.groups_file, а.max_sources)
    if url:
        потоки.append(threading.Thread(target=поток_ws,
                                        args=(замер, url, источники, стоп), daemon=True))
    else:
        print("ВНИМАНИЕ: WS не настроен (нет HELIUS_API_KEY и --ws-url) -- "
              "будут только шреды", file=sys.stderr)
    for п in потоки:
        п.start()
    начало = time.monotonic()
    print(json.dumps({"порт": а.port, "каталог": а.out_dir,
                      "источников_в_подписке": len(источники),
                      "ws": bool(url)}, ensure_ascii=False))
    try:
        while True:
            time.sleep(10)
            пр = замер.признак()
            if замер.слот_ws:
                # НА ДИСК СРАЗУ, А НЕ ЧЕРЕЗ ДВА ЧАСА. Отставание в 10 000 слотов
                # держало бы в памяти всю ночь измерений, и падение службы
                # унесло бы их целиком: в файл слоты попадают только при
                # вытеснении. 300 слотов -- это около двух минут, за которые
                # опоздавший шред или отметка WS ещё успевают прийти.
                замер.сбросить_старые(замер.слот_ws - ОТСТАВАНИЕ_СБРОСА)
            if а.seconds and (time.monotonic() - начало) >= а.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        стоп.set()
        time.sleep(1.2)
        замер.признак()
        замер.сбросить_старые(2 ** 63)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
