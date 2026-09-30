#!/usr/bin/env python3
"""Зонд «шреды против WS по транзакции»: только замер, ничего не отправляет и не подписывает.

Задание владельца (29.09, для хоста NL, ставит Code-1): подписка gRPC SubscribeDeshred у Triton (адрес и
ключ придут) + Helius WS logsSubscribe на тех же адресах источников; по каждой транзакции -- время прихода
по обоим каналам на стенных часах одного хоста, разность, лидер слота и его регион из расписания. Вывод:
p50/p90 по всем и по регионам лидера (EU / US / Asia), доля «шреды раньше».

Откуда что (ничего не по памяти):
  * SubscribeDeshred -- analysis/proto/geyser.proto (Yellowstone): поток stream_stream, запрос
    SubscribeDeshredRequest{deshred_transactions: {имя: {vote, account_include, ...}}, ping, slots};
    ответ SubscribeUpdateDeshred{filters, deshred_transaction{transaction{signature, is_vote, transaction,
    loaded_writable_addresses, loaded_readonly_addresses}, slot} | ping | pong | slot, created_at}.
    Транзакция приходит ДО исполнения (статуса нет); таблицы адресов уже разрешены, фильтр их видит.
    Ключ -- метаданные "x-token", как у зонда преконфов (analysis/triton_preconfs_probe.py ветки Code-1).
  * logsSubscribe -- стандартный Solana PubSub: params [{"mentions": [адрес]}, {"commitment": "processed"}],
    ОДИН адрес на подписку; уведомление {"params": {"subscription", "result": {"context": {"slot"},
    "value": {"signature", "err", "logs"}}}}. Точка -- wss://mainnet.helius-rpc.com/?api-key=...
  * лидер слота -- getLeaderSchedule (Helius HTTP) на эпоху слота, 432 000 слотов в эпохе (как
    analysis/leader_geo.py Code-1); регион лидера -- карта data/leader_regions.json Code-1
    (deploy/checks/karta_regionov_liderov.py: getClusterNodes + ip-api.com, поле "по_лидеру"). Регионы
    карты EU / US-East / US-West / Asia / прочее (XX) сводятся в EU / US / Asia / прочее; лидера нет в
    карте -- «неизвестно», а не догадка.

Время прихода -- ДВУМЯ ЧАСАМИ, как у зонда преконфов: t_recv (стенные, time.time()) и t_mono
(time.monotonic()), метка ставится сразу по приходу сообщения, до разбора. Разность считается по
стенным часам (так просил владелец: «на стенных часах одного хоста»); оба канала в одном процессе, и
расхождение со счётом по моно-часам выводится отдельной строкой (поправка NTP посреди окна его выдаст).
Разность = t(WS) − t(шреды), мс: плюс -- шреды раньше.

Журнал -- JSONL в форме зонда преконфов: feed, region, kind, t_recv, t_mono, utc, signature, slot, ...;
плюс stream_error / reconnected. feed "deshred" или "helius_ws".

Запуск на хосте (как зонд преконфов, те же файлы ключей):
  TRITON_X_TOKEN_FILE=... HELIUS_API_KEY_FILE=... TRITON_DESHRED_URL=https://<точка Triton>
  python3 podbivka_zond_deshred.py --accounts-file accounts.txt --seconds 3600 \\
      --out deshred_<stamp>.jsonl --status status_deshred.json --state-dir <каталог>
  python3 podbivka_zond_deshred.py --report-log deshred_<stamp>.jsonl --state-dir <каталог> \\
      --report-out deshred_svod.json --report-md deshred_svod.md
  python3 podbivka_zond_deshred.py --self-test          # без сети, на записанном образце
Ключ Triton: TRITON_DESHRED_TOKEN(_FILE), иначе TRITON_X_TOKEN(_FILE). Адрес: --url или
TRITON_DESHRED_URL(_FILE). Ключ Helius: HELIUS_API_KEY2(_FILE), иначе HELIUS_API_KEY(_FILE). Ключи в журнал, признак и печать не идут:
только «задан» и длина; тексты ошибок проходят вычистку ключей и api-key=.
Нагрузка на Helius: getSlot + getLeaderSchedule раз в эпоху; подписки WS шлются не чаще --ws-temp в секунду
(по умолчанию 8, предел владельца -- 10 rps суммарно).
Зависимости: grpcio, protobuf (как у зонда преконфов); WS -- своя реализация на стандартной библиотеке.
"""
from __future__ import annotations

import argparse
import base64
import calendar
import gzip
import json
import os
import queue
import re
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
КАНАЛ_ШРЕДЫ = "deshred"
КАНАЛ_WS = "helius_ws"
СЛОТОВ_В_ЭПОХЕ = 432_000
ИМЕНА_КЛЮЧА = ("TRITON_DESHRED_TOKEN", "TRITON_X_TOKEN")
ИМЯ_АДРЕСА = "TRITON_DESHRED_URL"
ИМЯ_HELIUS = "HELIUS_API_KEY"
ИМЕНА_HELIUS = ("HELIUS_API_KEY2", ИМЯ_HELIUS, "HELIUS_API")  # на хосте NL -- второй ключ (слово владельца 30.09)
HELIUS_WS = "wss://mainnet.helius-rpc.com/?api-key={}"
HELIUS_HTTP = "https://mainnet.helius-rpc.com/?api-key={}"
КРУПНО = ("EU", "US", "Asia", "прочее", "неизвестно")
ОТКАЗ_НАСОВСЕМ = ("UNAUTHENTICATED", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED", "INVALID_ARGUMENT",
                  "UNIMPLEMENTED")


# ------------------------------------------------------------------ ключи и вычистка

def _из_окружения(имя: str, окр) -> str:
    з = (окр.get(имя) or "").strip()
    if not з:
        путь = (окр.get(имя + "_FILE") or "").strip()
        if путь:
            try:
                з = Path(путь).read_text(encoding="utf-8").strip()
            except Exception:  # noqa: BLE001
                з = ""
    return з


def токен(окружение=None) -> str | None:
    """Ключ Triton: свой для шредов, иначе общий TRITON_X_TOKEN; из окружения или файлом (_FILE)."""
    окр = os.environ if окружение is None else окружение
    for имя in ИМЕНА_КЛЮЧА:
        з = _из_окружения(имя, окр)
        if з:
            return з
    return None


def ключ_helius(окружение=None) -> str | None:
    окр = os.environ if окружение is None else окружение
    for имя in ИМЕНА_HELIUS:
        з = _из_окружения(имя, окр)
        if з:
            return з
    return None


def адрес_triton(окружение=None) -> str | None:
    окр = os.environ if окружение is None else окружение
    return _из_окружения(ИМЯ_АДРЕСА, окр) or None


def задан(з: str | None, имя: str) -> dict:
    """Есть ли ключ -- без его значения; длина отличает пустую строку от отсутствия."""
    return {"ok": bool(з), "key_env": имя, "length": len(з) if з else 0}


_СЕКРЕТЫ: list = []


def чисто(текст: str, секреты: list | None = None) -> str:
    """Текст ошибки без ключей: известные значения и всё после api-key= / x-token= / token= вычищаются."""
    т = str(текст)
    for с in (секреты if секреты is not None else _СЕКРЕТЫ):
        if с:
            т = т.replace(с, "***")
    return re.sub(r"(?i)((?:api[-_]?key|x-token|token)=)[^&\s'\"]+", r"\1***", т)


def адрес_grpc(url: str) -> tuple:
    """URL кабинета -> ("host:port", TLS, путь_отброшен). Схема снимается, порт дописывается (443/80):
    без порта канал grpc молча не отдаёт ничего (замечено у Code-1 на Shyft, analysis/grpc_feed_probe.py)."""
    сырое = (url or "").strip().rstrip("/")
    tls = True
    for схема, защ in (("http://", False), ("https://", True), ("grpc://", False), ("grpcs://", True)):
        if сырое.startswith(схема):
            tls, сырое = защ, сырое[len(схема):]
            break
    хост, _, путь = сырое.partition("/")
    без_порта = ("]:" not in хост) if хост.startswith("[") else not (":" in хост and хост.rsplit(":", 1)[1].isdigit())
    if без_порта:
        хост = f"{хост}:{443 if tls else 80}"
    return хост, tls, bool(путь)


# ------------------------------------------------------------------ base58

АЛФАВИТ58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(сырьё: bytes) -> str:
    if not сырьё:
        return ""
    ч = int.from_bytes(сырьё, "big")
    из_ = ""
    while ч > 0:
        ч, о = divmod(ч, 58)
        из_ = АЛФАВИТ58[о] + из_
    for б in сырьё:
        if б:
            break
        из_ = "1" + из_
    return из_


def из_b58(т: str) -> bytes:
    ч = 0
    for c in т:
        ч = ч * 58 + АЛФАВИТ58.index(c)
    длина = (ч.bit_length() + 7) // 8
    нули = len(т) - len(т.lstrip("1"))
    return b"\x00" * нули + (ч.to_bytes(длина, "big") if длина else b"")


# ------------------------------------------------------------------ лидер и регион

def регион_крупно(р: str | None) -> str:
    if not р or р == "неизвестно":
        return "неизвестно"
    if р == "EU" or р == "Asia":
        return р
    if р.startswith("US"):
        return "US"
    return "прочее"


def найти_карту_регионов(явный: str | None = None) -> Path | None:
    """data/leader_regions.json Code-1: явный путь, LEADER_REGIONS_FILE, корень кода и каталоги PYTHONPATH."""
    кандидаты = [явный, os.environ.get("LEADER_REGIONS_FILE")]
    корни = [КОРЕНЬ, Path.cwd()] + [Path(p) for p in (os.environ.get("PYTHONPATH") or "").split(os.pathsep) if p]
    for к in корни:
        кандидаты += [str(к / "data" / "leader_regions.json"), str(к.parent / "data" / "leader_regions.json")]
    for к in кандидаты:
        if к and Path(к).is_file():
            return Path(к)
    return None


class Лидеры:
    """Слот -> (лидер, регион карты, регион крупно). Расписание -- getLeaderSchedule раз на эпоху, в памяти
    и файлом в каталоге состояния (отчёт добирает лидера по нему). Чужой эпохи в горячем пути не ждём:
    нет расписания -- поле пустое, догрузка идёт фоном."""

    def __init__(self, *, rpc=None, карта: dict | None = None, каталог: str | None = None):
        self.rpc = rpc
        self.карта = карта or {}
        self.каталог = Path(каталог) if каталог else None
        self.эпохи: dict = {}           # эпоха -> {относительный индекс: лидер}
        self.грузится: set = set()
        self.замок = threading.Lock()
        self.ошибки: list = []

    def _файл(self, эпоха: int) -> Path | None:
        return self.каталог / f"raspisanie_{эпоха}.json.gz" if self.каталог else None

    @staticmethod
    def _развернуть(сырое: dict) -> dict:
        из_ = {}
        for лидер, индексы in (сырое or {}).items():
            for и in индексы:
                из_[int(и)] = лидер
        return из_

    def загрузить(self, эпоха: int, *, сеть: bool = True) -> bool:
        ф = self._файл(эпоха)
        if ф and ф.is_file():
            try:
                self.эпохи[эпоха] = self._развернуть(json.loads(gzip.decompress(ф.read_bytes())))
                return True
            except Exception as exc:  # noqa: BLE001
                self.ошибки.append(f"файл расписания {эпоха}: {type(exc).__name__}")
        if not сеть or self.rpc is None:
            return False
        try:
            сырое = self.rpc("getLeaderSchedule", [эпоха * СЛОТОВ_В_ЭПОХЕ])
        except Exception as exc:  # noqa: BLE001
            self.ошибки.append(чисто(f"getLeaderSchedule {эпоха}: {type(exc).__name__}: {exc}")[:200])
            return False
        if not сырое:
            return False
        self.эпохи[эпоха] = self._развернуть(сырое)
        if ф:
            try:
                ф.parent.mkdir(parents=True, exist_ok=True)
                ф.write_bytes(gzip.compress(json.dumps(сырое).encode()))
            except Exception as exc:  # noqa: BLE001
                self.ошибки.append(f"запись расписания {эпоха}: {type(exc).__name__}")
        return True

    def _фоном(self, эпоха: int) -> None:
        with self.замок:
            if эпоха in self.грузится:
                return
            self.грузится.add(эпоха)

        def р():
            try:
                self.загрузить(эпоха)
            finally:
                with self.замок:
                    self.грузится.discard(эпоха)
        threading.Thread(target=р, name=f"raspisanie-{эпоха}", daemon=True).start()

    def лидер(self, слот, *, ждать: bool = False) -> dict:
        if not слот:
            return {"leader": None, "leader_region": None, "region_group": "неизвестно"}
        эпоха = int(слот) // СЛОТОВ_В_ЭПОХЕ
        if эпоха not in self.эпохи:
            if ждать:
                self.загрузить(эпоха)
            else:
                self._фоном(эпоха)
        л = (self.эпохи.get(эпоха) or {}).get(int(слот) - эпоха * СЛОТОВ_В_ЭПОХЕ)
        р = self.карта.get(л) if л else None
        return {"leader": л, "leader_region": р, "region_group": регион_крупно(р) if л else "неизвестно"}


def rpc_helius(ключ: str):
    url = HELIUS_HTTP.format(ключ)

    def вызов(метод: str, параметры: list):
        тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод, "params": параметры}).encode()
        зап = urllib.request.Request(url, data=тело, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(зап, timeout=60) as о:
            д = json.loads(о.read().decode())
        if "error" in д:
            raise RuntimeError(f"{метод}: {д['error']}")
        return д.get("result")
    return вызов


# ------------------------------------------------------------------ разбор сообщений

def _pb():
    """Стабы Yellowstone: analysis/proto рядом с модулем, корень кода, GEYSER_PROTO_DIR или PYTHONPATH."""
    for к in (os.environ.get("GEYSER_PROTO_DIR"), str(Path(__file__).resolve().parent / "proto"),
              str(КОРЕНЬ / "analysis" / "proto")):
        if к and Path(к).is_dir() and к not in sys.path:
            sys.path.insert(0, к)
    import geyser_pb2 as pb  # noqa: PLC0415
    return pb


def событие_из_шреда(обн, наши: set | None = None) -> dict:
    """SubscribeUpdateDeshred -> событие. Наши адреса в транзакции (статические + из таблиц) -- поле
    matched: по нему видно, какой источник поймал фильтр."""
    вид = обн.WhichOneof("update_oneof") or "unknown"
    из_: dict = {"kind": вид, "filters": list(обн.filters)}
    if обн.HasField("created_at"):
        из_["created_at"] = обн.created_at.seconds + обн.created_at.nanos / 1e9
    if вид == "deshred_transaction":
        из_["kind"] = "transaction"
        т = обн.deshred_transaction
        инф = т.transaction
        из_["slot"] = т.slot
        из_["signature"] = b58(инф.signature) if инф.signature else (
            b58(инф.transaction.signatures[0]) if инф.transaction.signatures else None)
        из_["is_vote"] = инф.is_vote
        if наши:
            ключи = list(инф.transaction.message.account_keys) + list(инф.loaded_writable_addresses) + \
                list(инф.loaded_readonly_addresses)
            из_["matched"] = sorted({b58(k) for k in ключи} & наши)
    elif вид == "slot":
        из_["slot"] = обн.slot.slot
    return из_


def событие_из_ws(сырое: str, по_номеру: dict, по_id: dict) -> dict:
    """Сообщение logsSubscribe -> событие: подтверждение подписки, отказ или транзакция."""
    try:
        j = json.loads(сырое)
    except ValueError:
        return {"kind": "unknown"}
    if j.get("id") is not None and "params" not in j:
        адрес = по_id.get(j.get("id"))
        if isinstance(j.get("result"), int):
            по_номеру[j["result"]] = адрес
            return {"kind": "subscribed", "address": адрес}
        return {"kind": "subscribe_rejected", "address": адрес, "details": str(j.get("error"))[:200]}
    п = j.get("params") or {}
    р = п.get("result") or {}
    з = р.get("value") or {}
    if not з.get("signature"):
        return {"kind": "unknown"}
    return {"kind": "transaction", "signature": з["signature"], "slot": (р.get("context") or {}).get("slot"),
            "err": з.get("err") is not None, "address": по_номеру.get(п.get("subscription"))}


def событие_в_строку(событие: dict, *, фид: str, регион: str, t_recv: float, t_mono: float | None = None,
                     лидеры: Лидеры | None = None) -> dict:
    """Строка журнала -- форма зонда преконфов (feed, region, kind, t_recv, t_mono, utc, signature, slot...)."""
    вид = событие.get("kind")
    из_ = {"feed": фид, "region": регион, "kind": вид, "t_recv": round(t_recv, 6),
           "t_mono": round(t_mono, 6) if t_mono is not None else None,
           "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_recv))}
    if вид == "transaction":
        из_.update(signature=событие.get("signature"), slot=событие.get("slot"))
        for к in ("filters", "matched", "is_vote", "created_at", "address", "err"):
            if к in событие:
                из_[к] = событие[к]
        if лидеры is not None:
            из_.update(лидеры.лидер(событие.get("slot")))
    elif вид in ("stream_error", "subscribe_rejected"):
        из_["code"] = событие.get("code")
        из_["details"] = чисто(событие.get("details") or "")[:200]
        if событие.get("address"):
            из_["address"] = событие["address"]
    elif вид == "reconnected":
        из_["attempts"] = событие.get("attempts")
    elif вид == "subscribed":
        из_["address"] = событие.get("address")
    return из_


# ------------------------------------------------------------------ журнал и счёт

class Журнал:
    """Один писатель на оба канала: строка на событие, счёт и пары по подписи -- для признака жизни."""

    def __init__(self, путь: str | None = None, *, лидеры: Лидеры | None = None, поток_записи=None):
        self.ф = поток_записи if поток_записи is not None else (open(путь, "a", encoding="utf-8") if путь else None)  # noqa: SIM115
        self.лидеры = лидеры
        self.замок = threading.Lock()
        self.счёт = {КАНАЛ_ШРЕДЫ: {}, КАНАЛ_WS: {}}
        self.первые: dict = {}                    # подпись -> {канал: t}
        self.пар = 0
        self.шреды_раньше = 0
        self.подписок = {"requested": 0, "confirmed": 0, "rejected": 0}

    def событие(self, событие: dict, *, фид: str, регион: str, t: float, моно: float) -> dict:
        стр = событие_в_строку(событие, фид=фид, регион=регион, t_recv=t, t_mono=моно, лидеры=self.лидеры)
        with self.замок:
            с = self.счёт[фид]
            с[стр["kind"]] = с.get(стр["kind"], 0) + 1
            if стр["kind"] == "subscribed":
                self.подписок["confirmed"] += 1
            elif стр["kind"] == "subscribe_rejected":
                self.подписок["rejected"] += 1
            if стр["kind"] == "transaction" and стр.get("signature"):
                п = self.первые.setdefault(стр["signature"], {})
                if фид not in п:
                    п[фид] = t
                    if len(п) == 2:
                        self.пар += 1
                        self.шреды_раньше += п[КАНАЛ_ШРЕДЫ] < п[КАНАЛ_WS]
                if len(self.первые) > 200_000:        # память: старые подписи для признака не нужны
                    for к in list(self.первые)[:100_000]:
                        del self.первые[к]
            if self.ф is not None and стр["kind"] not in ("ping", "pong"):
                self.ф.write(json.dumps(стр, ensure_ascii=False) + "\n")
                self.ф.flush()
        return стр

    def признак(self) -> dict:
        with self.замок:
            return {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "counters": json.loads(json.dumps(self.счёт)), "pairs": self.пар,
                    "deshred_first": self.шреды_раньше, "ws_subscriptions": dict(self.подписок)}

    def закрыть(self) -> None:
        if self.ф is not None:
            self.ф.close()


# ------------------------------------------------------------------ WS (стандартная библиотека)

class ПростойWS:
    """Клиент RFC 6455 на socket+ssl: текстовые кадры с маской, ответ на ping, склейка фрагментов.
    Своя реализация -- чтобы в окружении зонда (grpcio, protobuf) не понадобился ещё один пакет."""

    def __init__(self, url: str | None = None, *, таймаут: float = 30.0, сокет=None):
        self.замок = threading.Lock()
        self.буфер = b""
        if сокет is not None:                         # самопроверка: уже открытый сокет, без рукопожатия
            self.с = сокет
            return
        у = urllib.parse.urlsplit(url)
        порт = у.port or (443 if у.scheme == "wss" else 80)
        сырой = socket.create_connection((у.hostname, порт), timeout=таймаут)
        self.с = ssl.create_default_context().wrap_socket(сырой, server_hostname=у.hostname) if у.scheme == "wss" else сырой
        self.с.settimeout(таймаут)
        ключ = base64.b64encode(os.urandom(16)).decode()
        путь = (у.path or "/") + (f"?{у.query}" if у.query else "")
        self.с.sendall((f"GET {путь} HTTP/1.1\r\nHost: {у.hostname}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {ключ}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.буфер:
            к = self.с.recv(4096)
            if not к:
                raise ConnectionError("WS: соединение закрыто при рукопожатии")
            self.буфер += к
        голова, _, self.буфер = self.буфер.partition(b"\r\n\r\n")
        первая = голова.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in первая + " ":
            raise ConnectionError(f"WS: рукопожатие отклонено: {первая[:80]}")

    @staticmethod
    def кадр(код: int, данные: bytes, маска: bytes | None = None) -> bytes:
        м = маска if маска is not None else os.urandom(4)
        n = len(данные)
        голова = bytes([0x80 | код])
        if n < 126:
            голова += bytes([0x80 | n])
        elif n < 65536:
            голова += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            голова += bytes([0x80 | 127]) + struct.pack(">Q", n)
        return голова + м + bytes(б ^ м[i % 4] for i, б in enumerate(данные))

    def отправить(self, текст: str, код: int = 0x1) -> None:
        with self.замок:
            self.с.sendall(self.кадр(код, текст.encode() if isinstance(текст, str) else текст))

    def _читать(self, n: int) -> bytes:
        while len(self.буфер) < n:
            к = self.с.recv(65536)
            if not к:
                raise ConnectionError("WS: соединение закрыто")
            self.буфер += к
        из_, self.буфер = self.буфер[:n], self.буфер[n:]
        return из_

    def принять(self) -> str:
        """Следующее текстовое сообщение. socket.timeout пробрасывается: вызывающий шлёт ping и ждёт дальше."""
        части = []
        while True:
            б0, б1 = self._читать(2)
            код, конец = б0 & 0x0F, б0 & 0x80
            n = б1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._читать(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._читать(8))[0]
            м = self._читать(4) if б1 & 0x80 else None
            данные = self._читать(n)
            if м:
                данные = bytes(б ^ м[i % 4] for i, б in enumerate(данные))
            if код == 0x9:
                self.отправить(данные, 0xA)
                continue
            if код == 0xA:
                continue
            if код == 0x8:
                raise ConnectionError(f"WS: сервер закрыл соединение {данные[:2].hex()} {данные[2:80]!r}")
            части.append(данные)
            if конец:
                return b"".join(части).decode(errors="replace")

    def закрыть(self) -> None:
        try:
            self.отправить(b"\x03\xe8", 0x8)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.с.close()
        except Exception:  # noqa: BLE001, S110
            pass


class Темп:
    """Общий темп запросов к Helius (подписки WS): не чаще n в секунду на все соединения."""

    def __init__(self, в_секунду: float):
        self.шаг = 1.0 / max(0.1, в_секунду)
        self.след = 0.0
        self.замок = threading.Lock()

    def ждать(self, стоп: threading.Event | None = None) -> None:
        with self.замок:
            сейчас = time.monotonic()
            мой = max(сейчас, self.след)
            self.след = мой + self.шаг
        пауза = мой - time.monotonic()
        if пауза > 0:
            (стоп.wait(пауза) if стоп is not None else time.sleep(пауза))


def слушать_ws(журнал: Журнал, *, адреса: list, ключ: str, стоп: threading.Event, темп: Темп,
               имя: str = "helius", фабрика=None) -> None:
    """Одно соединение Helius на пачку адресов, по подписке logsSubscribe на адрес; переподключение с
    повторной подпиской. Метка времени -- сразу по приходу сообщения, до разбора."""
    попыток = 0
    while not стоп.is_set():
        по_номеру: dict = {}
        по_id = {i: а for i, а in enumerate(адреса, 1)}
        ws = None
        # своя остановка у каждого соединения: при обрыве поток подписки гасится и присоединяется ДО нового
        # соединения -- иначе он шлёт logsSubscribe в закрытый сокет (Bad file descriptor, Code-1 30.09 13:30Z)
        конец_соединения = threading.Event()
        нить_подписки = None
        try:
            ws = (фабрика or ПростойWS)(HELIUS_WS.format(ключ))
            if попыток:
                журнал.событие({"kind": "reconnected", "attempts": попыток}, фид=КАНАЛ_WS, регион=имя,
                               t=time.time(), моно=time.monotonic())

            def подписать(ws=ws, конец=конец_соединения):
                for i, а in по_id.items():
                    if стоп.is_set() or конец.is_set():
                        return
                    темп.ждать(конец)
                    if стоп.is_set() or конец.is_set():
                        return
                    try:
                        ws.отправить(json.dumps({"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                                                 "params": [{"mentions": [а]}, {"commitment": "processed"}]}))
                    except Exception:  # noqa: BLE001 -- сокет закрыт на обрыве; обрыв пишет читающий цикл
                        return
                    with журнал.замок:
                        журнал.подписок["requested"] += 1
            нить_подписки = threading.Thread(target=подписать, name=f"ws-podpiska-{имя}", daemon=True)
            нить_подписки.start()
            while not стоп.is_set():
                try:
                    сырое = ws.принять()
                except (socket.timeout, TimeoutError):
                    ws.отправить(b"", 0x9)             # держим соединение: тишина источников -- не обрыв
                    continue
                t, моно = time.time(), time.monotonic()
                журнал.событие(событие_из_ws(сырое, по_номеру, по_id), фид=КАНАЛ_WS, регион=имя, t=t, моно=моно)
        except Exception as exc:  # noqa: BLE001
            журнал.событие({"kind": "stream_error", "code": type(exc).__name__, "details": чисто(str(exc))},
                           фид=КАНАЛ_WS, регион=имя, t=time.time(), моно=time.monotonic())
        finally:
            конец_соединения.set()
            if ws is not None:
                ws.закрыть()
            if нить_подписки is not None:
                нить_подписки.join(timeout=10)
        попыток += 1
        стоп.wait(min(30.0, 2.0 * попыток))


# ------------------------------------------------------------------ gRPC SubscribeDeshred

def запрос_шредов(pb, аккаунты: list, пинг_id: int | None = None):
    зп = pb.SubscribeDeshredRequest()
    if пинг_id is not None:
        зп.ping.id = пинг_id
        return зп
    ф = зп.deshred_transactions["src"]
    ф.vote = False
    ф.account_include.extend(аккаунты)
    return зп


def поток_шредов(*, url: str, ключ: str, аккаунты: list, наши: set, стоп: threading.Event, до: float):
    """События SubscribeDeshred с переподключением (как _поток_grpc зонда преконфов): отказ по ключу/
    правам/пределу/аргументу закрывает зонд честно; обрыв связи -- stream_error и новая подписка.
    Отдаёт (событие, t, моно); t ставится сразу по получении сообщения из потока."""
    import grpc  # noqa: PLC0415
    pb = _pb()
    import geyser_pb2_grpc as pbg  # noqa: PLC0415
    цель, tls, _ = адрес_grpc(url)
    мета = (("x-token", ключ),) if ключ else ()
    попыток = 0
    while not стоп.is_set():
        осталось = до - time.time()
        if осталось <= 5:
            return
        исходящие: queue.Queue = queue.Queue()
        исходящие.put(запрос_шредов(pb, аккаунты))

        def запросы():
            while not стоп.is_set():
                try:
                    з = исходящие.get(timeout=0.5)
                except queue.Empty:
                    continue
                if з is None:
                    return
                yield з
        канал = grpc.secure_channel(цель, grpc.ssl_channel_credentials()) if tls else grpc.insecure_channel(цель)
        вызов = None
        try:
            клиент = pbg.GeyserStub(канал)
            вызов = клиент.SubscribeDeshred(запросы(), metadata=мета, timeout=осталось)
            if попыток:
                yield {"kind": "reconnected", "attempts": попыток}, time.time(), time.monotonic()
            пинг = 0
            for обн in вызов:
                t, моно = time.time(), time.monotonic()
                if стоп.is_set():
                    break
                вид = обн.WhichOneof("update_oneof")
                if вид == "ping":                      # сервер проверяет, жив ли клиент: отвечаем ping
                    пинг += 1
                    исходящие.put(запрос_шредов(pb, [], пинг_id=пинг))
                yield событие_из_шреда(обн, наши), t, моно
        except grpc.RpcError as exc:
            код = exc.code().name if exc.code() else "?"
            if код in ОТКАЗ_НАСОВСЕМ:
                raise RuntimeError(f"{код}: {чисто(str(exc.details()))[:200]}") from None
            if код != "DEADLINE_EXCEEDED":
                yield {"kind": "stream_error", "code": код, "details": чисто(str(exc.details()))[:200]}, \
                    time.time(), time.monotonic()
        finally:
            исходящие.put(None)
            if вызов is not None:
                вызов.cancel()
            канал.close()
        попыток += 1
        if попыток > 500:
            return
        стоп.wait(min(30.0, 1.0 * попыток))


def слушать_шреды(журнал: Журнал, *, url: str, ключ: str, аккаунты: list, стоп: threading.Event, до: float,
                  регион: str, итог: dict, поток=None) -> None:
    наши = set(аккаунты)
    try:
        for событие, t, моно in (поток if поток is not None else
                                 поток_шредов(url=url, ключ=ключ, аккаунты=аккаунты, наши=наши, стоп=стоп, до=до)):
            журнал.событие(событие, фид=КАНАЛ_ШРЕДЫ, регион=регион, t=t, моно=моно)
            if стоп.is_set():
                break
    except Exception as exc:  # noqa: BLE001
        итог["stopped_why"] = чисто(f"поток шредов оборвался: {type(exc).__name__}: {exc}")[:300]
        журнал.событие({"kind": "stream_error", "code": type(exc).__name__, "details": str(exc)},
                       фид=КАНАЛ_ШРЕДЫ, регион=регион, t=time.time(), моно=time.monotonic())
        стоп.set()


# ------------------------------------------------------------------ свод

def _медиана(ряд: list):
    if not ряд:
        return None
    р = sorted(ряд)
    n = len(р)
    return round(р[n // 2] if n % 2 else (р[n // 2 - 1] + р[n // 2]) / 2, 2)


def _процентиль(ряд: list, доля: float):
    """Как _p90 зонда преконфов: элемент с индексом round(доля·(n−1)) упорядоченного ряда."""
    if not ряд:
        return None
    р = sorted(ряд)
    return round(р[min(len(р) - 1, int(round(доля * (len(р) - 1))))], 2)


def читать_журнал(пути: list) -> list:
    """Журнал(ы) зонда: .jsonl или сжатый gzip (.jsonl.gz, как велит страница запуска) -- по первым байтам, не по имени."""
    строки = []
    for путь in пути:
        with open(путь, "rb") as сырой:
            сжат = сырой.read(2) == b"\x1f\x8b"
        with (gzip.open(путь, "rt", encoding="utf-8") if сжат else open(путь, encoding="utf-8")) as ф:
            for с in ф:
                с = с.strip()
                if с:
                    try:
                        строки.append(json.loads(с))
                    except ValueError:
                        continue
    return строки


def пары(строки: list, лидеры: Лидеры | None = None) -> dict:
    """Первый приход каждой подписи по каждому каналу; пары -- где пришло по обоим."""
    первые: dict = {}
    for с in строки:
        if с.get("kind") != "transaction" or not с.get("signature") or с.get("feed") not in (КАНАЛ_ШРЕДЫ, КАНАЛ_WS):
            continue
        п = первые.setdefault(с["signature"], {})
        if с["feed"] not in п or с["t_recv"] < п[с["feed"]]["t_recv"]:
            п[с["feed"]] = с
    ряды, только = [], {КАНАЛ_ШРЕДЫ: 0, КАНАЛ_WS: 0}
    for подпись, п in первые.items():
        if len(п) < 2:
            только[next(iter(п))] += 1
            continue
        ш, w = п[КАНАЛ_ШРЕДЫ], п[КАНАЛ_WS]
        слот = ш.get("slot") or w.get("slot")
        л = {к: ш.get(к) if ш.get("leader") else w.get(к) for к in ("leader", "leader_region", "region_group")}
        if not л.get("leader") and лидеры is not None:
            л = лидеры.лидер(слот, ждать=True)
        моно = ((w["t_mono"] - ш["t_mono"]) * 1000.0 if w.get("t_mono") is not None and ш.get("t_mono") is not None
                else None)
        ряды.append({"signature": подпись, "slot": слот, "slot_ws": w.get("slot"),
                     "leader": л.get("leader"), "leader_region": л.get("leader_region"),
                     "region_group": л.get("region_group") or "неизвестно",
                     "t_deshred": ш["t_recv"], "t_ws": w["t_recv"],
                     "разность_мс": round((w["t_recv"] - ш["t_recv"]) * 1000.0, 3),
                     "разность_моно_мс": round(моно, 3) if моно is not None else None,
                     "ws_err": w.get("err"), "address": w.get("address"),
                     "created_at_до_прихода_мс": (round((ш["t_recv"] - ш["created_at"]) * 1000.0, 3)
                                                  if ш.get("created_at") else None)})
    ряды.sort(key=lambda р: р["t_deshred"])
    return {"ряды": ряды, "только": только}


def _стат(ряды: list) -> dict:
    р = [x["разность_мс"] for x in ряды]
    return {"пар": len(р), "p50_мс": _медиана(р), "p90_мс": _процентиль(р, 0.9), "p10_мс": _процентиль(р, 0.1),
            "шреды_раньше": sum(1 for x in р if x > 0), "доля_шреды_раньше": round(sum(1 for x in р if x > 0) / len(р), 4) if р else None,
            "ничья": sum(1 for x in р if x == 0)}


def свод(строки: list, лидеры: Лидеры | None = None, метки: dict | None = None) -> dict:
    """метки -- {лидер: "BAM" | "Harmonic" | …} (файл --metki-liderov); лидер без метки -- «прочие», без лидера -- «неизвестно»."""
    пп = пары(строки, лидеры)
    ряды = пп["ряды"]
    по_движку: dict = {}
    for x in ряды:
        к = (метки or {}).get(x["leader"], "прочие") if x["leader"] else "неизвестно"
        по_движку.setdefault(к, []).append(x)
    по_крупно = {г: _стат([x for x in ряды if x["region_group"] == г]) for г in КРУПНО}
    подробно = {}
    for x in ряды:
        подробно.setdefault(x["leader_region"] or "неизвестно", []).append(x)
    обрывы = {}
    for с in строки:
        if с.get("kind") in ("stream_error", "reconnected", "subscribe_rejected"):
            к = f"{с.get('feed')}:{с['kind']}"
            обрывы[к] = обрывы.get(к, 0) + 1
    t = [с["t_recv"] for с in строки if с.get("t_recv")]
    расх = [abs(x["разность_мс"] - x["разность_моно_мс"]) for x in ряды if x["разность_моно_мс"] is not None]
    ca = [x["created_at_до_прихода_мс"] for x in ряды if x["created_at_до_прихода_мс"] is not None]
    return {"окно_utc": [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(min(t))),
                         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(t)))] if t else None,
            "подписей_шреды": sum(1 for с in строки if с.get("feed") == КАНАЛ_ШРЕДЫ and с.get("kind") == "transaction"),
            "подписей_ws": sum(1 for с in строки if с.get("feed") == КАНАЛ_WS and с.get("kind") == "transaction"),
            "только_шреды": пп["только"][КАНАЛ_ШРЕДЫ], "только_ws": пп["только"][КАНАЛ_WS],
            "все": _стат(ряды), "по_регионам": по_крупно,
            "по_регионам_подробно": {к: _стат(v) for к, v in sorted(подробно.items())},
            "по_движку_лидера": {к: _стат(v) for к, v in sorted(по_движку.items())} if метки is not None else None,
            "слот_шредов_не_равен_слоту_ws": sum(1 for x in ряды if x["slot_ws"] and x["slot"] != x["slot_ws"]),
            "ws_упавших_в_парах": sum(1 for x in ряды if x["ws_err"]),
            "стенные_против_моно_макс_мс": round(max(расх), 3) if расх else None,
            "created_at_до_прихода_p50_мс": _медиана(ca),
            "обрывы": обрывы, "ряды": ряды}


def таблица_md(св: dict) -> str:
    def стр(имя, с):
        д = "—" if с["доля_шреды_раньше"] is None else f"{100 * с['доля_шреды_раньше']:.1f} %"
        return (f"| {имя} | {с['пар']} | {'—' if с['p50_мс'] is None else с['p50_мс']} | "
                f"{'—' if с['p90_мс'] is None else с['p90_мс']} | {д} |")
    md = ["# Зонд «шреды против WS по транзакции»", "",
          f"Окно {св['окно_utc']}. Разность = t(WS) − t(шреды) по стенным часам хоста, мс; плюс -- шреды раньше. "
          f"Подписей: шреды {св['подписей_шреды']}, WS {св['подписей_ws']}; только шреды {св['только_шреды']}, "
          f"только WS {св['только_ws']}. Стенные против моно, наибольшее расхождение: {св['стенные_против_моно_макс_мс']} мс. "
          f"Обрывы: {св['обрывы'] or 'нет'}.", "",
          "| лидер слота | пар | p50, мс | p90, мс | шреды раньше |", "|---|---|---|---|---|",
          стр("все", св["все"])]
    md += [стр(г, св["по_регионам"][г]) for г in КРУПНО]
    md += ["", "Подробно по карте регионов:", "", "| регион карты | пар | p50, мс | p90, мс | шреды раньше |",
           "|---|---|---|---|---|"] + [стр(к, v) for к, v in св["по_регионам_подробно"].items()]
    if св.get("по_движку_лидера") is not None:
        md += ["", "По движку лидера (метки -- файл --metki-liderov; BAM -- получатели Jito BAM Boost и лидеры слотов нашего "
               "журнала BAM; **метка Harmonic частичная** -- только лидеры слотов наших журналов 25–26.09, полного списка нет; "
               "«прочие» могут содержать лидеров Harmonic):", "", "| движок лидера | пар | p50, мс | p90, мс | шреды раньше |",
               "|---|---|---|---|---|"] + [стр(к, v) for к, v in св["по_движку_лидера"].items()]
    return "\n".join(md) + "\n"


# ------------------------------------------------------------------ самопроверка

ОБРАЗЕЦ = КОРЕНЬ / "data" / "podbivka" / "zond_deshred" / "obrazec.jsonl"


def записать_образец(путь: Path = ОБРАЗЕЦ) -> Path:
    """Записанный образец для самопроверки: сырые сообщения обоих каналов в том виде, в каком они приходят
    (protobuf SubscribeUpdateDeshred в base64; текст logsSubscribe), и время прихода каждого.
    Подписи, слоты и кошелёк -- настоящие (сделки нашего кошелька 28.09, data/podbivka/zabor_s0_2026-09-29.json);
    лидеры слотов и времена прихода ЗАДАНЫ для проверки счёта и помечены так в первой строке -- это не замер."""
    pb = _pb()
    д = json.loads((КОРЕНЬ / "data" / "podbivka" / "zabor_s0_2026-09-29.json").read_text(encoding="utf-8"))
    кош = д["кошелёк"]
    сделки = sorted(((s, v["slot"]) for s, v in д["разбор"].items() if v.get("slot")), key=lambda x: x[1])[:12]
    лидеры = ["11AMA4mnNbsrPQeuoNN7uiZVJZtqEzQHrTfa5vnbcjk", "1KXvrkPXwkGF6NK1zyzVuJqbXfpenPVPP6hoiK9bsK3"]
    # (сдвиг прихода шредов от начала, сдвиг WS от прихода шредов в мс или None, регион лидера для проверки)
    план = [(0.0, 180, "EU"), (0.3, 95, "EU"), (0.9, -20, "US-East"), (1.4, 310, "Asia"), (2.0, 60, "US-West"),
            (2.2, None, "EU"), (2.6, 140, "Asia"), (3.1, -5, "EU"), (3.3, 40, "прочее (BR)"), (3.9, 0, "EU"),
            (4.4, 220, "неизвестно"), (None, 0, "EU")]
    t0 = 1_790_000_000.0
    строки = [{"образец": "для самопроверки podbivka_zond_deshred.py; подписи, слоты и кошелёк настоящие, "
                          "лидеры и времена прихода заданы вручную -- не замер", "кошелёк": кош,
               "карта_регионов": {}, "расписание": {}}]
    for i, ((подпись, слот), (сш, дw, рег)) in enumerate(zip(сделки, план)):
        лидер = f"{лидеры[i % 2][:-3]}{i:03d}"   # свой лидер на каждый слот -- чтобы регион задавался строкой
        строки[0]["карта_регионов"][лидер] = рег if рег != "неизвестно" else None
        строки[0]["расписание"][str(слот)] = лидер
        if сш is not None:
            обн = pb.SubscribeUpdateDeshred()
            обн.filters.append("src")
            обн.created_at.seconds = int(t0 + сш - 0.004)
            обн.created_at.nanos = int(((t0 + сш - 0.004) % 1) * 1e9)
            т = обн.deshred_transaction
            т.slot = слот
            т.transaction.signature = из_b58(подпись)
            т.transaction.transaction.signatures.append(из_b58(подпись))
            т.transaction.transaction.message.account_keys.append(из_b58(кош))
            строки.append({"канал": КАНАЛ_ШРЕДЫ, "t": round(t0 + сш, 6), "моно": round(1000.0 + сш, 6),
                           "b64": base64.b64encode(обн.SerializeToString()).decode()})
        if дw is not None:
            tw = (t0 + сш + дw / 1000.0) if сш is not None else t0 + 5.0
            строки.append({"канал": КАНАЛ_WS, "t": round(tw, 6), "моно": round(1000.0 + (tw - t0), 6),
                           "сырое": json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {
                               "subscription": 777, "result": {"context": {"slot": слот}, "value": {
                                   "signature": подпись, "err": None if i != 1 else {"InstructionError": [0, "Custom"]},
                                   "logs": ["Program 11111111111111111111111111111111 invoke [1]"]}}}})})
    # дубль WS (та же подпись по второй подписке позже) и пинг сервера
    строки.append({"канал": КАНАЛ_WS, "t": round(t0 + 0.5, 6), "моно": 1000.5, "сырое": строки[2]["сырое"]})
    пинг = pb.SubscribeUpdateDeshred()
    пинг.ping.SetInParent()
    строки.append({"канал": КАНАЛ_ШРЕДЫ, "t": t0 + 0.1, "моно": 1000.1, "b64": base64.b64encode(пинг.SerializeToString()).decode()})
    строки[1:] = sorted(строки[1:], key=lambda с: с["t"])
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("\n".join(json.dumps(с, ensure_ascii=False) for с in строки) + "\n", encoding="utf-8")
    return путь


def self_test() -> int:
    ошибок = []

    def chk(имя, ок, факт=""):
        print(f"  [{'ok' if ок else 'СБОЙ'}] {имя}" + ("" if ок else f" -- {str(факт)[:300]}"))
        if not ок:
            ошибок.append(имя)

    pb = _pb()
    chk("стабы Yellowstone знают SubscribeDeshred",
        hasattr(pb, "SubscribeDeshredRequest") and "created_at" in pb.SubscribeUpdateDeshred.DESCRIPTOR.fields_by_name)
    import geyser_pb2_grpc as pbg  # noqa: PLC0415
    chk("у клиента есть метод SubscribeDeshred", "SubscribeDeshred" in (pbg.GeyserStub.__init__.__code__.co_names
                                                                           + tuple(dir(pbg.GeyserServicer))))
    зп = запрос_шредов(pb, ["4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"])
    chk("запрос: фильтр src, без голосований, адрес в account_include",
        list(зп.deshred_transactions) == ["src"] and зп.deshred_transactions["src"].vote is False
        and зп.deshred_transactions["src"].HasField("vote")
        and list(зп.deshred_transactions["src"].account_include) == ["4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"], зп)
    chk("ответ на ping сервера -- запрос с ping.id", запрос_шредов(pb, [], пинг_id=3).ping.id == 3)
    п = "5DagYZdnYkS1E14YHy59HgJaCZkt53ARbNC5eGbtEueeF4ZhcQXgTkhpJgaajEzYgt57Nph7BdttR75ke88Pgjqd"
    chk("base58 туда и обратно", b58(из_b58(п)) == п and len(из_b58(п)) == 64)
    chk("адрес gRPC: схема снята, порт дописан, путь отмечен",
        адрес_grpc("https://x.rpcpool.com/abc") == ("x.rpcpool.com:443", True, True)
        and адрес_grpc("http://h:10000") == ("h:10000", False, False), адрес_grpc("https://x.rpcpool.com/abc"))
    # ключи: из окружения и файлом, в печать не попадают
    окр = {"TRITON_X_TOKEN": "sekret-123", "HELIUS_API_KEY": "hk-456"}
    chk("ключ Triton из TRITON_X_TOKEN, свой имеет приоритет",
        токен(окр) == "sekret-123" and токен({**окр, "TRITON_DESHRED_TOKEN": "d-1"}) == "d-1")
    chk("вычистка ключей и api-key=",
        "hk-456" not in чисто("wss://mainnet.helius-rpc.com/?api-key=hk-456 упал", [])
        and "sekret-123" not in чисто("x sekret-123 y", ["sekret-123"]))
    chk("признак «задан» без значения", задан("sekret-123", "TRITON_X_TOKEN") ==
        {"ok": True, "key_env": "TRITON_X_TOKEN", "length": 10})
    chk("регион крупно", [регион_крупно(р) for р in ("EU", "US-East", "US-West", "Asia", "прочее (BR)", None)] ==
        ["EU", "US", "US", "Asia", "прочее", "неизвестно"])
    # WS: кадр с маской туда-обратно через socketpair, ping -> pong, фрагменты
    а, б = socket.socketpair()
    try:
        клиент = ПростойWS(сокет=а)
        клиент.отправить("привет")
        сырой = б.recv(1024)
        сервер = ПростойWS(сокет=б)
        сервер.буфер = сырой
        chk("кадр клиента с маской читается", сервер.принять() == "привет")
        б.sendall(bytes([0x89, 0]) + bytes([0x01, 3]) + b"abc" + bytes([0x80, 2]) + b"de")
        chk("ping сервера -> pong, фрагменты склеены", клиент.принять() == "abcde")
        понг = б.recv(64)
        chk("pong ушёл с маской", понг[0] == 0x8A and понг[1] & 0x80, понг)
        длинное = "x" * 70000
        б.sendall(bytes([0x81, 127]) + struct.pack(">Q", len(длинное)) + длинное.encode())
        chk("длинный кадр (64-битная длина)", клиент.принять() == длинное)
    finally:
        а.close()
        б.close()
    # записанный образец -> те же разборщики, журнал, свод
    if not ОБРАЗЕЦ.is_file():
        записать_образец()
    строки_обр = [json.loads(с) for с in ОБРАЗЕЦ.read_text(encoding="utf-8").splitlines() if с.strip()]
    шапка, сообщения = строки_обр[0], строки_обр[1:]
    карта = {к: v for к, v in шапка["карта_регионов"].items() if v}
    лидеры = Лидеры(карта=карта)
    for слот, лидер in шапка["расписание"].items():
        э = int(слот) // СЛОТОВ_В_ЭПОХЕ
        лидеры.эпохи.setdefault(э, {})[int(слот) - э * СЛОТОВ_В_ЭПОХЕ] = лидер
    import io  # noqa: PLC0415
    буф = io.StringIO()
    ж = Журнал(лидеры=лидеры, поток_записи=буф)
    по_номеру, по_id = {}, {1: шапка["кошелёк"]}
    ж.событие(событие_из_ws(json.dumps({"jsonrpc": "2.0", "id": 1, "result": 777}), по_номеру, по_id),
              фид=КАНАЛ_WS, регион="helius", t=1_790_000_000.0 - 1, моно=999.0)
    наши = {шапка["кошелёк"]}
    for с in сообщения:
        if с["канал"] == КАНАЛ_ШРЕДЫ:
            обн = pb.SubscribeUpdateDeshred.FromString(base64.b64decode(с["b64"]))
            ж.событие(событие_из_шреда(обн, наши), фид=КАНАЛ_ШРЕДЫ, регион="triton", t=с["t"], моно=с["моно"])
        else:
            ж.событие(событие_из_ws(с["сырое"], по_номеру, по_id), фид=КАНАЛ_WS, регион="helius", t=с["t"], моно=с["моно"])
    строки = [json.loads(с) for с in буф.getvalue().splitlines()]
    тх = [с for с in строки if с["kind"] == "transaction"]
    chk("строка журнала в форме зонда преконфов",
        all({"feed", "region", "kind", "t_recv", "t_mono", "utc", "signature", "slot", "leader", "region_group"} <= set(с)
            for с in тх), тх[:1])
    chk("пинг сервера в журнал не пишется, но считается",
        not any(с["kind"] == "ping" for с in строки) and ж.признак()["counters"][КАНАЛ_ШРЕДЫ].get("ping") == 1)
    chk("шреды: наш кошелёк найден в ключах транзакции",
        all(с.get("matched") == [шапка["кошелёк"]] for с in тх if с["feed"] == КАНАЛ_ШРЕДЫ))
    chk("WS: подписка подтверждена, адрес подписки в строке",
        ж.подписок["confirmed"] == 1 and all(с.get("address") == шапка["кошелёк"] for с in тх if с["feed"] == КАНАЛ_WS))
    св = свод(строки)
    # ожидаемое -- отдельно, из образца: разность WS − шреды по первым приходам
    первые: dict = {}
    for с in сообщения:
        if с["канал"] == КАНАЛ_ШРЕДЫ and "b64" in с:
            обн = pb.SubscribeUpdateDeshred.FromString(base64.b64decode(с["b64"]))
            if обн.WhichOneof("update_oneof") != "deshred_transaction":
                continue
            s = b58(обн.deshred_transaction.transaction.signature)
        elif с["канал"] == КАНАЛ_WS:
            s = json.loads(с["сырое"])["params"]["result"]["value"]["signature"]
        else:
            continue
        первые.setdefault(s, {}).setdefault(с["канал"], с["t"])
    ожид = sorted(round((v[КАНАЛ_WS] - v[КАНАЛ_ШРЕДЫ]) * 1000, 3) for v in первые.values() if len(v) == 2)
    факт = sorted(x["разность_мс"] for x in св["ряды"])
    chk("пары и разности совпадают с образцом", len(факт) == len(ожид) == 10
        and all(abs(a - b) < 0.01 for a, b in zip(факт, ожид)), (факт, ожид))
    chk("p50/p90 по всем", св["все"]["p50_мс"] == _медиана(ожид) and св["все"]["p90_мс"] == _процентиль(ожид, 0.9)
        and abs(св["все"]["p50_мс"] - 77.5) < 0.01 and abs(св["все"]["p90_мс"] - 220.0) < 0.01, св["все"])
    chk("доля «шреды раньше»: 7 из 10 (−20, −5 и 0 -- нет)",
        св["все"]["шреды_раньше"] == 7 and св["все"]["доля_шреды_раньше"] == 0.7 and св["все"]["ничья"] == 1, св["все"])
    пр = св["по_регионам"]
    chk("по регионам лидера: EU 4, US 2, Asia 2, прочее 1, неизвестно 1",
        [пр[г]["пар"] for г in КРУПНО] == [4, 2, 2, 1, 1], {г: пр[г]["пар"] for г in КРУПНО})
    chk("EU: p50 47.5, p90 180, раньше 2 из 4",
        abs(пр["EU"]["p50_мс"] - 47.5) < 0.01 and abs(пр["EU"]["p90_мс"] - 180.0) < 0.01 and пр["EU"]["шреды_раньше"] == 2, пр["EU"])
    chk("US складывает US-East и US-West", св["по_регионам_подробно"].get("US-East", {}).get("пар") == 1
        and св["по_регионам_подробно"].get("US-West", {}).get("пар") == 1 and пр["US"]["шреды_раньше"] == 1)
    chk("только шреды 1, только WS 1; дубль WS не сдвинул пару", св["только_шреды"] == 1 and св["только_ws"] == 1
        and sum(1 for x in св["ряды"] if x["разность_мс"] == 180.0) == 1, (св["только_шреды"], св["только_ws"]))
    chk("упавшая транзакция WS помечена", св["ws_упавших_в_парах"] == 1)
    chk("стенные и моно сходятся", св["стенные_против_моно_макс_мс"] is not None and св["стенные_против_моно_макс_мс"] < 0.01,
        св["стенные_против_моно_макс_мс"])
    chk("created_at сервера до прихода -- 4 мс", св["created_at_до_прихода_p50_мс"] is not None
        and abs(св["created_at_до_прихода_p50_мс"] - 4.0) < 0.01, св["created_at_до_прихода_p50_мс"])
    md = таблица_md(св)
    chk("таблица: строки все / EU / US / Asia", all(f"| {г} |" in md for г in ("все", "EU", "US", "Asia")))
    # лидер, которого нет в строке журнала, добирается из расписания при сводке
    без = [dict(с, leader=None, leader_region=None, region_group=None) for с in строки]
    chk("лидер добирается из расписания при сводке", свод(без, лидеры)["по_регионам"]["EU"]["пар"] == 4)
    chk("без расписания -- «неизвестно», не догадка", свод(без)["по_регионам"]["неизвестно"]["пар"] == 10)
    # поток шредов с заглушкой: отказ по ключу закрывает зонд, текст без ключа
    стоп = threading.Event()
    итог: dict = {}

    def плохой_поток():
        yield {"kind": "transaction", "signature": п, "slot": 1}, 1.0, 1.0
        raise RuntimeError("UNAUTHENTICATED: bad token sekret-123")
    _СЕКРЕТЫ.append("sekret-123")
    ж2 = Журнал(поток_записи=io.StringIO())
    слушать_шреды(ж2, url="", ключ="", аккаунты=[], стоп=стоп, до=0, регион="triton", итог=итог, поток=плохой_поток())
    chk("отказ ключа: зонд останавливается, причина без ключа",
        стоп.is_set() and "UNAUTHENTICATED" in итог.get("stopped_why", "") and "sekret-123" not in итог["stopped_why"]
        and "sekret-123" not in ж2.ф.getvalue(), итог)
    _СЕКРЕТЫ.remove("sekret-123")
    # настоящий поток_шредов против локального сервера gRPC (127.0.0.1): ключ в метаданных, фильтр, ответ на ping
    try:
        import grpc  # noqa: PLC0415
        from concurrent import futures  # noqa: PLC0415
        видел: dict = {"ключ": None, "запросы": []}

        class Сервер(pbg.GeyserServicer):
            def SubscribeDeshred(self, request_iterator, context):  # noqa: N802
                видел["ключ"] = dict(context.invocation_metadata()).get("x-token")
                видел["запросы"].append(next(request_iterator))
                п_ = pb.SubscribeUpdateDeshred()
                п_.ping.SetInParent()
                yield п_
                видел["запросы"].append(next(request_iterator))
                о = pb.SubscribeUpdateDeshred()
                о.deshred_transaction.slot = 451225771
                о.deshred_transaction.transaction.signature = из_b58(п)
                о.deshred_transaction.transaction.transaction.message.account_keys.append(из_b58("4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"))
                yield о
                context.abort(grpc.StatusCode.UNAUTHENTICATED, "конец проверки")
        срв = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        pbg.add_GeyserServicer_to_server(Сервер(), срв)
        порт = срв.add_insecure_port("127.0.0.1:0")
        срв.start()
        стоп4, итог4 = threading.Event(), {}
        ж4 = Журнал(поток_записи=io.StringIO())
        слушать_шреды(ж4, url=f"http://127.0.0.1:{порт}", ключ="t-789", аккаунты=["4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"],
                      стоп=стоп4, до=time.time() + 30, регион="local", итог=итог4)
        срв.stop(0)
        ст4 = [json.loads(с) for с in ж4.ф.getvalue().splitlines()]
        тх4 = [с for с in ст4 if с["kind"] == "transaction"]
        chk("gRPC: ключ в x-token, фильтр ушёл, на ping ответ ping.id=1, транзакция разобрана, отказ закрыл",
            видел["ключ"] == "t-789" and list(видел["запросы"][0].deshred_transactions["src"].account_include)
            and len(видел["запросы"]) == 2 and видел["запросы"][1].ping.id == 1 and len(тх4) == 1
            and тх4[0]["signature"] == п and тх4[0]["matched"] == ["4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"]
            and "UNAUTHENTICATED" in (итог4.get("stopped_why") or ""), (видел, ст4, итог4))
    except ImportError as exc:
        chk("gRPC установлен", False, exc)
    # WS-цикл с заглушкой соединения: подписка уходит, сообщения идут в журнал, обрыв -> stream_error
    class Заглушка:
        def __init__(self, url):
            self.ушло, self.шаг = [], 0

        def отправить(self, т, код=0x1):
            self.ушло.append(т)

        def принять(self):
            self.шаг += 1
            if self.шаг == 1:
                time.sleep(0.05)
                return json.dumps({"jsonrpc": "2.0", "id": 1, "result": 5})
            if self.шаг == 2:
                return json.dumps({"params": {"subscription": 5, "result": {"context": {"slot": 9},
                                   "value": {"signature": п, "err": None}}}})
            стоп3.set()
            raise ConnectionError("WS: сервер закрыл соединение api-key=hk-456")

        def закрыть(self):
            pass
    стоп3 = threading.Event()
    ж3 = Журнал(поток_записи=io.StringIO())
    слушать_ws(ж3, адреса=["A1"], ключ="hk-456", стоп=стоп3, темп=Темп(100), фабрика=Заглушка)
    ст3 = [json.loads(с) for с in ж3.ф.getvalue().splitlines()]
    chk("WS-цикл: подписка, транзакция с адресом, обрыв в журнале без ключа",
        [с["kind"] for с in ст3][:3] == ["subscribed", "transaction", "stream_error"]
        and ст3[1]["address"] == "A1" and "hk-456" not in ж3.ф.getvalue(), ст3)
    # свод читает журнал и сжатым: .jsonl и .jsonl.gz с одними строками дают одно и то же
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as кат:
        текст = "\n".join(json.dumps({"feed": КАНАЛ_ШРЕДЫ, "kind": "transaction", "signature": f"s{i}", "t_recv": 1.0 + i})
                          for i in range(3)) + "\n{битая строка\n"
        простой, сжатый = Path(кат) / "ж.jsonl", Path(кат) / "ж.jsonl.gz"
        простой.write_text(текст, encoding="utf-8")
        сжатый.write_bytes(gzip.compress(текст.encode("utf-8")))
        а1, а2 = читать_журнал([str(простой)]), читать_журнал([str(сжатый)])
    chk("свод читает .jsonl.gz так же, как .jsonl (битая строка пропускается)", а1 == а2 and len(а1) == 3, (len(а1), len(а2)))
    # обрыв посреди подписки: старый поток подписки не шлёт в закрытый сокет и кончается до нового соединения
    соединения: list = []

    class Обрыв:
        def __init__(self, url):
            self.закрыт, self.после_закрытия, self.ушло = False, 0, 0
            self.номер = len(соединения)
            self.живых_потоков_при_создании = sum(1 for н in threading.enumerate() if н.name.startswith("ws-podpiska-t5"))
            соединения.append(self)

        def отправить(self, т, код=0x1):
            if self.закрыт:
                self.после_закрытия += 1
                raise OSError(9, "Bad file descriptor")
            self.ушло += 1

        def принять(self):
            if self.номер == 0:
                time.sleep(0.05)                # подписка первого соединения ещё идёт (темп 20/с, 50 адресов)
                raise ConnectionError("обрыв")
            стоп5.set()
            raise ConnectionError("конец теста")

        def закрыть(self):
            self.закрыт = True
    стоп5 = threading.Event()
    ж5 = Журнал(поток_записи=io.StringIO())
    слушать_ws(ж5, адреса=[f"A{i}" for i in range(50)], ключ="k", стоп=стоп5, темп=Темп(20), имя="t5", фабрика=Обрыв)
    time.sleep(0.3)
    chk("WS-обрыв посреди подписки: в закрытый сокет не ушло ни одной подписки, старый поток кончился до нового",
        len(соединения) == 2 and all(с.после_закрытия == 0 for с in соединения)
        and соединения[0].ушло < 50 and соединения[1].живых_потоков_при_создании == 0,
        [(с.ушло, с.после_закрытия, с.живых_потоков_при_создании) for с in соединения])
    print(f"самопроверка зонда шредов: {'СБОЙ ' + str(len(ошибок)) if ошибок else 'всё сошлось'} "
          f"({len(ошибок)} ошибок)")
    return 1 if ошибок else 0


# ------------------------------------------------------------------ запуск

def адреса_из_групп(файл: str | None, группы: tuple) -> list:
    """Адреса торгующих групп тем же модулем, что читает их в бою (bloom_source_groups Code-1)."""
    import bloom_source_groups as BSG  # noqa: PLC0415
    return sorted(а for а, г in (BSG.адреса_всех_групп(файл) or {}).items() if г in группы)


def main() -> int:
    р = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--zapisat-obrazec", action="store_true", help="перезаписать образец самопроверки и выйти")
    р.add_argument("--url", default=None, help="точка Triton (иначе TRITON_DESHRED_URL / _FILE)")
    р.add_argument("--accounts", default="", help="адреса источников через запятую")
    р.add_argument("--accounts-file", default=None)
    р.add_argument("--groups-file", default=None, help="файл групп: адреса берутся модулем bloom_source_groups")
    р.add_argument("--iz-grupp", action="store_true", help="адреса из файла групп по BLOOM_SOURCE_GROUPS")
    р.add_argument("--gruppy", default="leader,batch5,lane_s0,sniper_src,kandidaty")
    р.add_argument("--seconds", type=float, default=3600.0, help="сколько держать оба потока")
    р.add_argument("--out", default=None, help="журнал зонда, jsonl")
    р.add_argument("--status", default=None, help="признак жизни, json (раз в --status-kazhdye с)")
    р.add_argument("--status-kazhdye", type=float, default=30.0)
    р.add_argument("--state-dir", default=os.environ.get("TRITON_STATE_DIR"), help="каталог расписаний лидеров")
    р.add_argument("--leader-regions", default=None, help="карта лидер -> регион (data/leader_regions.json Code-1)")
    р.add_argument("--ws-na-soedinenie", type=int, default=100, help="адресов на одно соединение Helius")
    р.add_argument("--ws-temp", type=float, default=8.0, help="подписок Helius в секунду, на все соединения")
    р.add_argument("--report-log", nargs="*", default=None, help="только свод: журнал(ы) зонда")
    р.add_argument("--report-out", default=None)
    р.add_argument("--report-md", default=None)
    р.add_argument("--vyrezat", action="append", default=[], help="только свод: окно UTC «YYYY-MM-DDTHH:MM:SS,YYYY-MM-DDTHH:MM:SS» -- "
                   "строки обоих каналов с t_recv внутри выбрасываются (дыра в записи); можно несколько раз")
    р.add_argument("--metki-liderov", default=None, help="json {\"по_лидеру\": {лидер: BAM|Harmonic}} -- разбивка свода по движку лидера")
    а = р.parse_args()
    if а.self_test:
        return self_test()
    if а.zapisat_obrazec:
        print(f"записано: {записать_образец()}")
        return 0

    карта_путь = найти_карту_регионов(а.leader_regions)
    карта = json.loads(карта_путь.read_text(encoding="utf-8")).get("по_лидеру") if карта_путь else {}
    кл_h = ключ_helius()
    if кл_h:
        _СЕКРЕТЫ.append(кл_h)
    лидеры = Лидеры(rpc=rpc_helius(кл_h) if кл_h else None, карта=карта, каталог=а.state_dir)

    if а.report_log is not None:
        метки = (json.loads(Path(а.metki_liderov).read_text(encoding="utf-8")).get("по_лидеру") or {}) if а.metki_liderov else None
        строки = читать_журнал(а.report_log)
        вырезано = []
        for окно in а.vyrezat:
            н, к = (calendar.timegm(time.strptime(x.strip(), "%Y-%m-%dT%H:%M:%S")) for x in окно.split(","))
            было = len(строки)
            строки = [с for с in строки if not (с.get("t_recv") and н <= с["t_recv"] <= к)]
            вырезано.append({"окно": окно, "строк": было - len(строки)})
        св = свод(строки, лидеры, метки)
        св["вырезано"] = вырезано
        св["карта_регионов"] = str(карта_путь) if карта_путь else None
        print(json.dumps({к: v for к, v in св.items() if к != "ряды"}, ensure_ascii=False, indent=2))
        if а.report_out:
            Path(а.report_out).write_text(json.dumps(св, ensure_ascii=False, indent=1), encoding="utf-8")
        if а.report_md:
            Path(а.report_md).write_text(таблица_md(св), encoding="utf-8")
        return 0

    кл_t, url = токен(), (а.url or адрес_triton())
    for с in (кл_t, url):
        if с:
            _СЕКРЕТЫ.append(с)
    цель = адрес_grpc(url) if url else None
    print(json.dumps({"triton_key": задан(кл_t, "/".join(ИМЕНА_КЛЮЧА)), "helius_key": задан(кл_h, "/".join(ИМЕНА_HELIUS)),
                      "triton_host": цель[0] if цель else None, "triton_path_dropped": цель[2] if цель else None,
                      "leader_regions": str(карта_путь) if карта_путь else None,
                      "leaders_in_map": len(карта or {})}, ensure_ascii=False))
    if not (кл_t and кл_h and url):
        print("СБОЙ: нужны ключ Triton, адрес Triton и ключ Helius (значения не печатаются)")
        return 1
    аккаунты = [с.strip() for с in а.accounts.split(",") if с.strip()]
    if а.accounts_file:
        аккаунты += Path(а.accounts_file).read_text(encoding="utf-8").split()
    if а.groups_file or а.iz_grupp:
        аккаунты += адреса_из_групп(а.groups_file, tuple(г.strip() for г in а.gruppy.split(",") if г.strip()))
    аккаунты = sorted(set(аккаунты))
    if not аккаунты:
        print("СБОЙ: нет адресов источников")
        return 1
    print(json.dumps({"adresov": len(аккаунты), "ws_soedinenij": -(-len(аккаунты) // а.ws_na_soedinenie)}))
    try:                                            # расписание текущей эпохи -- до потоков
        слот = лидеры.rpc("getSlot", [{"commitment": "processed"}])
        лидеры.загрузить(int(слот) // СЛОТОВ_В_ЭПОХЕ)
    except Exception as exc:  # noqa: BLE001
        лидеры.ошибки.append(чисто(f"getSlot: {type(exc).__name__}: {exc}")[:200])

    import signal as _sig  # noqa: PLC0415
    стоп = threading.Event()
    for _с in (_sig.SIGTERM, _sig.SIGINT):
        try:
            _sig.signal(_с, lambda *_: стоп.set())
        except Exception:  # noqa: BLE001, S110
            pass
    журнал = Журнал(а.out, лидеры=лидеры)
    до = time.time() + а.seconds
    итог: dict = {"stopped_why": None}
    темп = Темп(а.ws_temp)
    нити = [threading.Thread(target=слушать_шреды, name="deshred", daemon=True,
                             kwargs=dict(журнал=журнал, url=url, ключ=кл_t, аккаунты=аккаунты, стоп=стоп, до=до,
                                         регион=цель[0].split(":")[0], итог=итог))]
    for i in range(0, len(аккаунты), а.ws_na_soedinenie):
        нити.append(threading.Thread(target=слушать_ws, name=f"ws-{i}", daemon=True,
                                     kwargs=dict(журнал=журнал, адреса=аккаунты[i:i + а.ws_na_soedinenie], ключ=кл_h,
                                                 стоп=стоп, темп=темп, имя=f"helius-{i // а.ws_na_soedinenie}")))
    for н in нити:
        н.start()

    def признак(конец: bool = False):
        if а.status:
            з = журнал.признак() | {"seconds_left": max(0, round(до - time.time())), "stopped_why": итог["stopped_why"],
                                    "schedule_errors": лидеры.ошибки[-5:], "epochs_loaded": sorted(лидеры.эпохи),
                                    "finished": конец}
            Path(а.status).write_text(json.dumps(з, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        while not стоп.is_set() and time.time() < до:
            признак()
            стоп.wait(min(а.status_kazhdye, max(0.1, до - time.time())))
    finally:
        стоп.set()
        for н in нити:
            н.join(timeout=10)
        признак(конец=True)
        журнал.закрыть()
    print(json.dumps(журнал.признак() | {"stopped_why": итог["stopped_why"]}, ensure_ascii=False, indent=2))
    if итог["stopped_why"]:
        print(f"СБОЙ: {итог['stopped_why']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
