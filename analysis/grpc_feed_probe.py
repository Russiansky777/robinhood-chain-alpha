#!/usr/bin/env python3
"""Зонд: кто раньше видит сделку источника -- EU-gRPC или наш Helius WS.

Вопрос владельца один и измеримый: на сколько миллисекунд поток Yellowstone
gRPC опережает (или отстаёт от) transactionSubscribe, которым сейчас живёт
детектор. Ответ даёт только одновременное слушание ОБОИХ каналов на одном
хосте с одними часами.

Как меряется, и почему именно так:

  * время берётся МОНОТОННЫМИ часами (time.monotonic) СРАЗУ по приходу
    сообщения, ДО разбора. Разбор gRPC и разбор JSON стоят разного, и если
    ставить метку после него, замеряется наш парсер, а не канал;
  * пары сводятся по ПОДПИСИ. Подпись -- единственное, что у двух каналов
    совпадает буквально; слот и время блока приходят с задержкой сети;
  * сообщение, пришедшее только по одному каналу, НЕ выбрасывается: это
    надёжность канала, и она в докладе отдельной строкой;
  * два канала считаются по двум группам адресов: наши 19 источников (это
    боевой вопрос) и "разгонные" адреса, взятые ПО ЦЕПИ для набора выборки.
    Разгонные в торговлю не идут никогда и в отчёте названы поимённо.

Границы, заданные владельцем и встроенные в код:
  * зонд НИЧЕГО не покупает и не продаёт: ни импорта исполнителя, ни клиента
    Bloom здесь нет вовсе, и самопроверка это сторожит;
  * состояние детектора не трогается: свой каталог, свой JSONL;
  * подписка детектора не трогается: у зонда своё соединение и свой ключ из
    окружения.

Адрес и токен gRPC берутся из GRPC_FEED_URL и GRPC_FEED_TOKEN (второй поток
-- GRPC_FEED2_URL и GRPC_FEED2_TOKEN). Имя заголовка авторизации задаётся
GRPC_FEED_AUTH_HEADER (по умолчанию x-token, как у Shyft). Токен не
печатается никогда.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402
import bloom_exec_state as ST  # noqa: E402

КАНАЛ_WS = "helius_ws"
КАНАЛ_GRPC = "grpc"
КАНАЛ_GRPC2 = "grpc2"
ГРУППА_НАША = "источники"
ГРУППА_РАЗГОН = "разгонные"

# Сколько держать записи в памяти для сводки за окно.
ОКНО_S = 3600.0


def _процентиль(значения: list, доля: float) -> float | None:
    if not значения:
        return None
    з = sorted(значения)
    i = min(len(з) - 1, max(0, int(round(доля * (len(з) - 1)))))
    return з[i]


class Гонка:
    """Учёт двух каналов и сведение пар по подписи.

    Держит только окно последних записей: зонд живёт сутками, и память не
    должна расти линейно.
    """

    def __init__(self, окно_s: float = ОКНО_S) -> None:
        self.окно_s = float(окно_s)
        self.первый: dict = {}          # подпись -> (канал, t, группа, слот)
        self.пары: list = []            # (t_свед, группа, канал_первый, дельта_мс)
        self.по_каналам: dict = {}      # канал -> счётчик сообщений
        self.дубли: dict = {}           # канал -> повторы той же подписи
        self.порядок: list = []         # (t, подпись) для выселения старых

    def пришло(self, канал: str, подпись: str, *, слот: int | None = None,
                t: float | None = None, группа: str = ГРУППА_НАША) -> dict | None:
        """Одно сообщение. Возвращает пару, если она только что сошлась."""
        t = time.monotonic() if t is None else float(t)
        self.по_каналам[канал] = self.по_каналам.get(канал, 0) + 1
        if not подпись:
            return None
        было = self.первый.get(подпись)
        if было is None:
            # Пятое поле -- сошлась ли уже пара. Без него "без пары" считало
            # бы и сведённые подписи: они остаются в памяти до выселения.
            self.первый[подпись] = [канал, t, группа, слот, False]
            self.порядок.append((t, подпись))
            self._выселить(t)
            return None
        if было[0] == канал or было[4]:
            # Тот же канал прислал ту же подпись второй раз, либо пара уже
            # сошлась и это третье сообщение. И то и другое -- дубль канала,
            # а не опережение, и считается отдельно.
            self.дубли[канал] = self.дубли.get(канал, 0) + 1
            return None
        дельта = (t - было[1]) * 1000.0
        пара = {"signature": подпись, "first": было[0], "second": канал,
                 "delta_ms": round(дельта, 3), "group": было[2] or группа,
                 "slot": было[3] if было[3] is not None else слот}
        было[4] = True
        self.пары.append((t, пара))
        self._выселить(t)
        return пара

    def _выселить(self, t: float) -> None:
        граница = t - self.окно_s
        while self.порядок and self.порядок[0][0] < граница:
            _, подпись = self.порядок.pop(0)
            self.первый.pop(подпись, None)
        while self.пары and self.пары[0][0] < граница:
            self.пары.pop(0)

    def без_пары(self) -> dict:
        """Сколько подписей так и не дождались второго канала."""
        из_: dict = {}
        for з in self.первый.values():
            if з[4]:
                continue
            из_[з[0]] = из_.get(з[0], 0) + 1
        return из_

    def сводка(self, *, каналы: tuple = (КАНАЛ_GRPC, КАНАЛ_GRPC2)) -> dict:
        """Числа, и только наши: доля первых, медиана, p10, p90, без пары."""
        из_ = {"pairs_total": len(self.пары),
                "messages": dict(self.по_каналам),
                "duplicates": dict(self.дубли),
                "unpaired_pending": self.без_пары(),
                "by_channel": {}, "by_group": {}}
        for канал in каналы:
            дельты, первых = [], 0
            for _, п in self.пары:
                if канал not in (п["first"], п["second"]):
                    continue
                # Дельта ОТНОСИТЕЛЬНО Helius WS: плюс -- канал раньше.
                знак = 1.0 if п["first"] == канал else -1.0
                дельты.append(знак * п["delta_ms"])
                первых += 1 if п["first"] == канал else 0
            if not дельты:
                continue
            из_["by_channel"][канал] = {
                "pairs": len(дельты),
                "first_share": round(первых / len(дельты), 4),
                "median_ms": round(statistics.median(дельты), 2),
                "p10_ms": round(_процентиль(дельты, 0.1), 2),
                "p90_ms": round(_процентиль(дельты, 0.9), 2),
                "mean_ms": round(sum(дельты) / len(дельты), 2)}
        for группа in (ГРУППА_НАША, ГРУППА_РАЗГОН):
            дельты = [(1.0 if п["first"] != КАНАЛ_WS else -1.0) * п["delta_ms"]
                       for _, п in self.пары if п["group"] == группа]
            if дельты:
                из_["by_group"][группа] = {
                    "pairs": len(дельты),
                    "median_ms": round(statistics.median(дельты), 2),
                    "p10_ms": round(_процентиль(дельты, 0.1), 2),
                    "p90_ms": round(_процентиль(дельты, 0.9), 2)}
        return из_


def разгонные_по_цепи(helius, *, сколько: int = 3, исключить: set | None = None,
                       назад_слотов: int = 30) -> dict:
    """Самые активные плательщики недавнего блока -- ПО ЦЕПИ, не по памяти.

    Зонду нужна выборка: 19 источников дают несколько сделок в час, а для
    честной медианы нужны сотни пар. Разгонные адреса берутся из свежего
    блока: кто чаще всех платит за транзакции, тот и даст поток. Имена
    выписываются в отчёт -- в торговлю они не идут никогда.
    """
    исключить = исключить or set()
    try:
        слот = helius.call("getSlot", [{"commitment": "confirmed"}])
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "why_not": f"getSlot: {type(exc).__name__}: {exc}"}
    if not isinstance(слот, int):
        return {"known": False, "why_not": "getSlot не отдал число"}
    цель = слот - назад_слотов
    try:
        блок = helius.call("getBlock", [цель, {
            "encoding": "jsonParsed", "transactionDetails": "accounts",
            "rewards": False, "maxSupportedTransactionVersion": 0}])
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "why_not": f"getBlock {цель}: {type(exc).__name__}: {exc}"}
    счёт: dict = {}
    for tx in ((блок or {}).get("transactions") or []):
        ключи = ((tx or {}).get("transaction") or {}).get("accountKeys") or []
        for k in ключи:
            if not isinstance(k, dict) or not k.get("signer"):
                continue
            адрес = k.get("pubkey")
            if адрес and адрес not in исключить:
                счёт[адрес] = счёт.get(адрес, 0) + 1
    топ = sorted(счёт.items(), key=lambda kv: -kv[1])[:сколько]
    return {"known": True, "slot": цель, "accounts": [a for a, _ in топ],
             "counts": {a: n for a, n in топ},
             "note": ("разгонные адреса взяты по цепи из блока "
                       f"{цель}: самые частые подписанты. В торговлю не идут")}


def запись_строки(путь: Path, строка: dict) -> None:
    ST.append_jsonl_fsync(путь, строка)


# ------------------------------------------------------------------ каналы

def слушать_ws(гонка: Гонка, *, ключ: str, адреса: list, группы: dict,
                стоп: threading.Event, журнал: Path | None = None,
                учёт=None) -> None:
    """Вторая, ОТДЕЛЬНАЯ подписка Helius. Соединение детектора не трогается."""
    import asyncio  # noqa: PLC0415

    import websockets  # noqa: PLC0415

    async def круг():
        url = BD.ws_url(ключ, True)
        async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                       max_queue=None) as ws:
            for i, a in enumerate(адреса, 1):
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": i, "method": "transactionSubscribe",
                    "params": [{"accountInclude": [a], "failed": False,
                                 "vote": False},
                                {"commitment": "processed",
                                 "transactionDetails": "signatures",
                                 "encoding": "jsonParsed",
                                 "showRewards": False,
                                 "maxSupportedTransactionVersion": 0}]}))
            while not стоп.is_set():
                сырое = await asyncio.wait_for(ws.recv(), timeout=60)
                t = time.monotonic()          # метка ДО разбора
                if учёт is not None:
                    учёт(len(сырое or ""))
                try:
                    j = json.loads(сырое)
                except ValueError:
                    continue
                п = ((j.get("params") or {}).get("result") or {})
                tx = (п.get("transaction") or {})
                подписи = ((tx.get("transaction") or {}).get("signatures") or [])
                подпись = подписи[0] if подписи else None
                if not подпись:
                    continue
                пара = гонка.пришло(КАНАЛ_WS, подпись, слот=п.get("slot"), t=t,
                                     группа=группы.get(подпись, ГРУППА_НАША))
                if журнал is not None:
                    запись_строки(журнал, {"channel": КАНАЛ_WS, "signature": подпись,
                                            "slot": п.get("slot"), "t": t,
                                            "pair": пара})

    while not стоп.is_set():
        try:
            asyncio.run(круг())
        except Exception as exc:  # noqa: BLE001
            if журнал is not None:
                запись_строки(журнал, {"channel": КАНАЛ_WS, "error":
                                        f"{type(exc).__name__}: {str(exc)[:200]}"})
            time.sleep(2.0)


def слушать_grpc(гонка: Гонка, *, канал_имя: str, url: str, токен: str,
                  адреса: list, стоп: threading.Event,
                  заголовок: str = "x-token", журнал: Path | None = None) -> None:
    """Подписка Yellowstone. Токен уходит В МЕТАДАННЫХ и нигде не печатается."""
    import grpc  # noqa: PLC0415

    sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))
    import geyser_pb2 as pb  # noqa: PLC0415
    import geyser_pb2_grpc as pbg  # noqa: PLC0415

    цель = url.replace("https://", "").replace("http://", "").rstrip("/")
    безопасно = not url.startswith("http://")

    def запрос():
        req = pb.SubscribeRequest()
        подписка = req.transactions["probe"]
        подписка.vote = False
        подписка.failed = False
        for a in адреса:
            подписка.account_include.append(a)
        req.commitment = pb.CommitmentLevel.PROCESSED
        yield req

    while not стоп.is_set():
        try:
            creds = grpc.ssl_channel_credentials() if безопасно else None
            канал = (grpc.secure_channel(цель, creds) if безопасно
                      else grpc.insecure_channel(цель))
            with канал:
                клиент = pbg.GeyserStub(канал)
                мета = ((заголовок, токен),) if токен else ()
                for ответ in клиент.Subscribe(запрос(), metadata=мета):
                    t = time.monotonic()      # метка ДО разбора
                    if стоп.is_set():
                        break
                    if not ответ.HasField("transaction"):
                        continue
                    сд = ответ.transaction
                    подпись = None
                    try:
                        сырьё = сд.transaction.signature
                        подпись = _base58(сырьё)
                    except Exception:  # noqa: BLE001
                        подпись = None
                    if not подпись:
                        continue
                    пара = гонка.пришло(канал_имя, подпись, слот=сд.slot, t=t)
                    if журнал is not None:
                        запись_строки(журнал, {"channel": канал_имя,
                                                "signature": подпись,
                                                "slot": сд.slot, "t": t,
                                                "pair": пара})
        except Exception as exc:  # noqa: BLE001
            if журнал is not None:
                запись_строки(журнал, {"channel": канал_имя, "error":
                                        f"{type(exc).__name__}: {str(exc)[:200]}"})
            time.sleep(2.0)


АЛФАВИТ58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58(сырьё: bytes) -> str:
    """Подпись у gRPC приходит байтами, а у WS -- строкой base58.

    Без перевода пары не сойдутся ни разу, и зонд молча показал бы "оба
    канала не пересекаются". Перевод здесь свой, чтобы не тащить зависимость.
    """
    if not сырьё:
        return ""
    число = int.from_bytes(сырьё, "big")
    из_ = ""
    while число > 0:
        число, остаток = divmod(число, 58)
        из_ = АЛФАВИТ58[остаток] + из_
    for b in сырьё:
        if b == 0:
            из_ = "1" + из_
        else:
            break
    return из_


# ------------------------------------------------------------- самопроверка

def self_test() -> None:
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    # 1. Границы: зонд не умеет торговать вообще.
    тело = Path(__file__).read_text(encoding="utf-8").split("def self_test")[0]
    chk("в зонде нет клиента Bloom", "bloom_api" not in тело)
    chk("в зонде нет исполнителя", "bloom_executor" not in тело)
    chk("в зонде нет ни одного POST", "post(" not in тело)

    # 2. Пары и дельты.
    г = Гонка()
    chk("первое сообщение пары не даёт", г.пришло(КАНАЛ_GRPC, "S1", t=1.000) is None)
    п = г.пришло(КАНАЛ_WS, "S1", t=1.040)
    chk("пара сошлась по подписи, первым назван gRPC",
        п and п["first"] == КАНАЛ_GRPC and abs(п["delta_ms"] - 40.0) < 0.01, п)
    п2 = г.пришло(КАНАЛ_WS, "S2", t=2.000)
    п3 = г.пришло(КАНАЛ_GRPC, "S2", t=2.015)
    chk("обратный порядок тоже пара, первым назван WS",
        п3 and п3["first"] == КАНАЛ_WS and abs(п3["delta_ms"] - 15.0) < 0.01, п3)
    chk("дубль того же канала парой не считается",
        г.пришло(КАНАЛ_GRPC, "S3", t=3.0) is None
        and г.пришло(КАНАЛ_GRPC, "S3", t=3.1) is None
        and г.дубли.get(КАНАЛ_GRPC) == 1, г.дубли)

    с = г.сводка()
    chk("в сводке две пары", с["pairs_total"] == 2, с["pairs_total"])
    к = с["by_channel"][КАНАЛ_GRPC]
    chk("доля первых у gRPC -- половина", abs(к["first_share"] - 0.5) < 1e-9, к)
    chk("медиана дельты относительно WS: +40 и -15 дают 12.5",
        abs(к["median_ms"] - 12.5) < 0.01, к)
    chk("сообщение без пары посчитано отдельно",
        с["unpaired_pending"].get(КАНАЛ_GRPC) == 1, с["unpaired_pending"])

    # 2б. Третье сообщение по уже сведённой паре -- дубль, а не новая пара.
    г_т = Гонка()
    г_т.пришло(КАНАЛ_GRPC, "T", t=1.0)
    г_т.пришло(КАНАЛ_WS, "T", t=1.01)
    chk("третье сообщение по сведённой подписи -- дубль, а не вторая пара",
        г_т.пришло(КАНАЛ_WS, "T", t=1.2) is None
        and len(г_т.пары) == 1 and г_т.дубли.get(КАНАЛ_WS) == 1,
        (len(г_т.пары), г_т.дубли))
    chk("сведённая подпись в 'без пары' не попадает",
        г_т.без_пары() == {}, г_т.без_пары())

    # 3. Группы считаются раздельно.
    г2 = Гонка()
    г2.пришло(КАНАЛ_GRPC, "A", t=1.0, группа=ГРУППА_НАША)
    г2.пришло(КАНАЛ_WS, "A", t=1.02, группа=ГРУППА_НАША)
    г2.пришло(КАНАЛ_GRPC, "B", t=2.0, группа=ГРУППА_РАЗГОН)
    г2.пришло(КАНАЛ_WS, "B", t=2.1, группа=ГРУППА_РАЗГОН)
    с2 = г2.сводка()
    chk("наши источники и разгонные считаются отдельно",
        с2["by_group"][ГРУППА_НАША]["pairs"] == 1
        and abs(с2["by_group"][ГРУППА_РАЗГОН]["median_ms"] - 100.0) < 0.01,
        с2["by_group"])

    # 4. Окно выселяет старое, память не растёт.
    г3 = Гонка(окно_s=10.0)
    г3.пришло(КАНАЛ_GRPC, "СТАРОЕ", t=0.0)
    г3.пришло(КАНАЛ_GRPC, "НОВОЕ", t=100.0)
    chk("старая подпись выселена по окну",
        "СТАРОЕ" not in г3.первый and "НОВОЕ" in г3.первый, sorted(г3.первый))

    # 5. Подпись gRPC переводится в base58, иначе пары не сойдутся никогда.
    chk("base58: нули в начале дают единицы", _base58(b"\x00\x00\x01") == "112")
    chk("base58: пустое остаётся пустым", _base58(b"") == "")
    эталон = bytes([0x2b, 0x1f, 0x48])
    chk("base58 обратим по значению",
        sum(АЛФАВИТ58.index(c) * 58 ** i
            for i, c in enumerate(reversed(_base58(эталон))))
        == int.from_bytes(эталон, "big"), _base58(эталон))

    # 6. Разгонные берутся ПО ЦЕПИ, а отказ узла остаётся отказом.
    class HeliusБлок:
        def call(self, метод, параметры):
            if метод == "getSlot":
                return 1000
            assert метод == "getBlock"
            def tx(подписанты):
                return {"transaction": {"accountKeys": [
                    {"pubkey": p, "signer": True} for p in подписанты]}}
            return {"transactions": [tx(["БОТ", "ЧУЖОЙ"]), tx(["БОТ"]),
                                      tx(["БОТ", "НАШ"]), tx(["ЧУЖОЙ"])]}

    р = разгонные_по_цепи(HeliusБлок(), сколько=2, исключить={"НАШ"})
    chk("разгонные -- самые частые подписанты блока, наши исключены",
        р["known"] and р["accounts"][0] == "БОТ" and "НАШ" not in р["accounts"],
        р)
    chk("и назван слот, из которого они взяты", р["slot"] == 970, р.get("slot"))

    class HeliusМолчит:
        def call(self, *a, **kw):
            raise RuntimeError("узел молчит")

    р2 = разгонные_по_цепи(HeliusМолчит())
    chk("отказ узла -- причина, а не пустой список",
        р2["known"] is False and "getSlot" in р2["why_not"], р2)

    print(f"самопроверка зонда потоков: {всего[1]}/{всего[0]}"
           f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    if всего[1] != всего[0]:
        raise SystemExit(1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--seconds", type=float, default=None,
                    help="сколько работать (по умолчанию бесконечно)")
    p.add_argument("--state-dir", default="/home/bot/grpc_feed_probe_data")
    p.add_argument("--config", default=str(Path(__file__).resolve().parents[1]
                                            / "data" / "final" / "20260923T145755Z"
                                            / "konfig.json"))
    p.add_argument("--tasks", default=os.environ.get("BLOOM_TASKS", "BATCH-5,BATCH-3"))
    p.add_argument("--boosters", type=int, default=3)
    a = p.parse_args()
    if a.self_test:
        self_test()
        return 0

    каталог = Path(a.state_dir)
    каталог.mkdir(parents=True, exist_ok=True)
    журнал = каталог / "feed_race.jsonl"
    признак = каталог / "heartbeat.json"

    задачи = tuple(x.strip() for x in a.tasks.split(",") if x.strip())
    ист, откуда = BD.источники(задачи, Path(a.config))
    источники = sorted(ист)
    helius = BD.Helius(служба="grpc_feed_probe")
    разгон = разгонные_по_цепи(helius, сколько=a.boosters,
                                исключить=set(источники))
    адреса = list(источники) + list(разгон.get("accounts") or [])
    группы: dict = {}

    гонка = Гонка()
    стоп = threading.Event()
    потоки = []

    ключ = os.environ.get("HELIUS_API_KEY", "")
    if ключ:
        потоки.append(threading.Thread(
            target=слушать_ws, args=(гонка,),
            kwargs={"ключ": ключ, "адреса": адреса, "группы": группы,
                     "стоп": стоп, "журнал": журнал,
                     "учёт": helius.учесть_вебсокет},
            name="ws", daemon=True))
    for имя, префикс in ((КАНАЛ_GRPC, "GRPC_FEED"), (КАНАЛ_GRPC2, "GRPC_FEED2")):
        url = os.environ.get(f"{префикс}_URL", "")
        if not url:
            continue
        потоки.append(threading.Thread(
            target=слушать_grpc, args=(гонка,),
            kwargs={"канал_имя": имя, "url": url,
                     "токен": os.environ.get(f"{префикс}_TOKEN", ""),
                     "адреса": адреса, "стоп": стоп,
                     "заголовок": os.environ.get("GRPC_FEED_AUTH_HEADER", "x-token"),
                     "журнал": журнал},
            name=имя, daemon=True))

    for t in потоки:
        t.start()
    print(f"зонд запущен: каналов {len(потоки)}, адресов {len(адреса)} "
           f"(источников {len(источники)}, разгонных "
           f"{len(разгон.get('accounts') or [])}), откуда источники: {откуда}")
    if not разгон.get("known"):
        print(f"разгонные НЕ добавлены: {разгон.get('why_not')}")

    дедлайн = (time.time() + a.seconds) if a.seconds else None
    try:
        while not стоп.is_set():
            time.sleep(10.0)
            с = гонка.сводка()
            ST.atomic_write_json(признак, {
                "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "channels": len(потоки), "addresses": len(адреса),
                "sources": len(источники),
                "boosters": разгон.get("accounts") or [],
                "boosters_why_not": (None if разгон.get("known")
                                      else разгон.get("why_not")),
                "summary": с})
            if дедлайн and time.time() > дедлайн:
                break
    except KeyboardInterrupt:
        pass
    стоп.set()
    print(json.dumps(гонка.сводка(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
