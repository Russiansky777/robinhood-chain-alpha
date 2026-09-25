#!/usr/bin/env python3
"""Зонд Triton Preconfs: только замер, детектор не трогаем.

Владелец 25.09: "TRITON PRECONFS -- ЗОНД, как с Shyft: только измерение,
детектор не трогать... Endpoint https://preconfs.rpcpool.com, заголовок x-token
-- секрет TRITON_X_TOKEN... Регионы: только ams (fra -- если ams недоступен).
Фильтр: account_include = адреса наших источников... Итог после 30 сигналов:
медиана, p90, доля, где preconfs раньше; покрытие; доля 'preconf был, а
транзакция не села'. BAM: поток открыт постоянно. Предел 5 000 сообщений в
сутки, дальше закрыть и доложить. Harmonic: тестовое окно -- один поток,
регион ams, 2 часа в активное время... Жёсткий предел: закрыть при 20 000
оплаченных слотов (~$30) или по истечении 2 часов, что раньше; счётчик слотов и
сообщений в признак жизни... BAM и Harmonic одновременно в двух потоках не
держать."

ВСЁ ЗДЕСЬ -- ИЗ СОХРАНЁННОЙ ДОКУМЕНТАЦИИ (data/docs/triton/), не по памяти:
  * точка входа preconfs.rpcpool.com:443, anycast, gRPC;
  * ключ -- метаданные gRPC "x-token" на КАЖДОМ вызове, включая GetVersion;
  * службы preconfs.Harmonic и preconfs.BAM, метод Subscribe (поток) и
    GetVersion; запрос SubscribeRequest = {transactions: {имя: фильтр},
    ровно одно из harmonic_region / bam_region};
  * фильтр обязан задать хотя бы одно из account_include / account_required /
    signature; полный поток подписке недоступен;
  * счёт: messages -- доставленные транзакции; slots -- слоты, пока поток
    Harmonic открыт, СОСТАВЛЯЕТ ОСНОВНУЮ ЦЕНУ и капает, даже если ни одна
    транзакция не подошла. Поэтому предел владельца в 20 000 слотов -- это
    предел ВРЕМЕНИ потока, а не удачи фильтра;
  * у Harmonic есть рамки слота (SlotStart/SlotEnd) и результат исполнения, у
    BAM рамок нет и результата нет;
  * адреса из таблиц (v0 ALT) фильтром НЕ ловятся -- только статические ключи.
    Это прямо названо главной причиной "вижу меньше, чем в Geyser".

ЧЕГО ЗДЕСЬ НЕТ. Торговли: зонд ничего не отправляет и не подписывает. Ключа в
выводе: он живёт в окружении и в метаданных запроса, а в журнал идёт только
слово "задан". Двух потоков сразу: BAM и Harmonic одновременно не держим --
это прямое слово владельца, и проверка на это падает.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ТОЧКА_ВХОДА = "preconfs.rpcpool.com:443"
ССЫЛКА_ДОКОВ = "https://docs.triton.one/chains/solana/preconfirmations-grpc"
ИМЯ_СЕКРЕТА = "TRITON_X_TOKEN"

# РЕГИОНЫ. Владелец: "только ams (fra -- если ams недоступен)". Имена -- из
# таблиц документации; у Harmonic регионов 7, у BAM 15, нам нужны два.
РЕГИОН_ОСНОВНОЙ = "ams"
РЕГИОН_ЗАПАСНОЙ = "fra"
РЕГИОНЫ_HARMONIC = ("ams", "ewr", "fra", "lon", "tyo", "sgp", "slc")
РЕГИОНЫ_BAM = ("ams", "dfw", "dub", "ewr", "fra", "hkg", "iad", "lax", "lon",
                "pit", "sea", "sin", "slc", "sqq", "tyo")

ФИД_HARMONIC = "harmonic"
ФИД_BAM = "bam"

# ПРЕДЕЛЫ ВЛАДЕЛЬЦА. Жёсткие: при достижении поток закрывается и пишется
# строка, а не "ещё чуть-чуть".
ПРЕДЕЛ_СООБЩЕНИЙ_BAM_В_СУТКИ = 5000
ПРЕДЕЛ_СЛОТОВ_HARMONIC = 20000
ОКНО_HARMONIC_S = 2 * 60 * 60

# Пределы фильтров из документации -- проверяем ДО отправки, как и их клиент.
ПРЕДЕЛ_ФИЛЬТРОВ = 64
ПРЕДЕЛ_АККАУНТОВ = 10000
ПРЕДЕЛ_ПОДПИСЕЙ = 1000


class ПределДостигнут(Exception):
    """Поток закрывается по пределу владельца, а не по ошибке сети."""


def токен(окружение=None) -> str | None:
    окр = os.environ if окружение is None else окружение
    з = (окр.get(ИМЯ_СЕКРЕТА) or "").strip()
    return з or None


def токен_задан(окружение=None) -> dict:
    """Есть ли ключ -- БЕЗ его значения. Длина полезна: пустая строка в
    секрете и отсутствие секрета выглядят одинаково, а это разные беды."""
    з = токен(окружение)
    return {"ok": bool(з), "key_env": ИМЯ_СЕКРЕТА,
            "length": (len(з) if з else 0),
            "why_not": None if з else
            f"секрета {ИМЯ_СЕКРЕТА} в окружении нет -- зонд не пойдёт"}


def регион_фида(фид: str, регион: str) -> dict:
    """Регион обязан принадлежать ЭТОМУ фиду: регион другого фида сервер
    отвергает с INVALID_ARGUMENT, и узнать об этом лучше до подписки."""
    список = РЕГИОНЫ_HARMONIC if фид == ФИД_HARMONIC else РЕГИОНЫ_BAM
    if фид not in (ФИД_HARMONIC, ФИД_BAM):
        return {"ok": False, "why_not": f"неизвестный фид {фид}"}
    if регион not in список:
        return {"ok": False,
                "why_not": (f"регион {регион} не из списка {фид}: "
                             f"{', '.join(список)}")}
    ключ = ("harmonic_region" if фид == ФИД_HARMONIC else "bam_region")
    значение = (f"HARMONIC_REGION_{регион.upper()}" if фид == ФИД_HARMONIC
                else f"BAM_REGION_{регион.upper()}")
    return {"ok": True, "field": ключ, "value": значение, "why_not": None}


def собрать_запрос(*, фид: str, регион: str, аккаунты: list,
                   имя_фильтра: str = "istochniki") -> dict:
    """SubscribeRequest ровно как в документации.

    Фильтр обязан задать хотя бы один отбор: полный поток подписке недоступен,
    и просить его -- значит получить INVALID_ARGUMENT и потратить попытку.
    """
    из_ = {"ok": False, "why_not": None, "request": None}
    р = регион_фида(фид, регион)
    if not р["ok"]:
        из_["why_not"] = р["why_not"]
        return из_
    адреса = [а for а in (аккаунты or []) if а]
    if not адреса:
        из_["why_not"] = ("фильтр без отбора: account_include пуст, а полный "
                           "поток подписке недоступен")
        return из_
    if len(адреса) > ПРЕДЕЛ_АККАУНТОВ:
        из_["why_not"] = (f"{len(адреса)} аккаунтов при пределе "
                           f"{ПРЕДЕЛ_АККАУНТОВ}")
        return из_
    if len(имя_фильтра.encode()) > 64:
        из_["why_not"] = "имя фильтра длиннее 64 байт"
        return из_
    из_.update(ok=True, request={
        "transactions": {имя_фильтра: {"account_include": адреса}},
        р["field"]: р["value"]})
    return из_


def путь_суток(каталог: str | None = None) -> Path:
    д = каталог or os.environ.get("TRITON_STATE_DIR") or "/tmp"
    return Path(д) / "triton_preconfs_day.json"


def расход_суток(каталог: str | None = None,
                  сейчас: float | None = None) -> dict:
    """Что уже израсходовано СЕГОДНЯ (UTC): сообщения BAM и слоты Harmonic.

    Предел владельца у BAM -- "5 000 сообщений в СУТКИ". Считать его за прогон
    значило бы обнулять предел каждым перезапуском потока, то есть тратить
    столько раз по 5 000, сколько раз мы перезапустились.
    """
    день = time.strftime("%Y%m%d", time.gmtime(сейчас if сейчас else time.time()))
    из_ = {"day": день, "bam_messages": 0, "harmonic_slots": 0,
            "harmonic_seconds": 0.0, "why_not": None}
    п = путь_суток(каталог)
    try:
        if п.exists():
            было = json.loads(п.read_text(encoding="utf-8") or "{}")
            if было.get("day") == день:
                из_.update(bam_messages=int(было.get("bam_messages") or 0),
                            harmonic_slots=int(было.get("harmonic_slots") or 0),
                            harmonic_seconds=float(
                                было.get("harmonic_seconds") or 0.0))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return из_


def записать_сутки(расход: dict, каталог: str | None = None) -> dict:
    п = путь_суток(каталог)
    try:
        п.parent.mkdir(parents=True, exist_ok=True)
        п.write_text(json.dumps(расход, ensure_ascii=False), encoding="utf-8")
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:120]}"}


class Счёт:
    """Счётчики и ЖЁСТКИЕ пределы. Деньги здесь -- слоты Harmonic.

    Правило владельца: закрыть при 20 000 оплаченных слотов или по истечении
    двух часов, что раньше; у BAM -- при 5 000 сообщений в сутки. Поэтому
    предел проверяется ПОСЛЕ каждого события, и первое же превышение закрывает
    поток исключением, а не предупреждением.
    """

    def __init__(self, *, фид: str, начало: float | None = None,
                  предел_сообщений: int | None = None,
                  предел_слотов: int = ПРЕДЕЛ_СЛОТОВ_HARMONIC,
                  окно_s: float | None = None,
                  каталог: str | None = None):
        self.фид = фид
        self.каталог = каталог
        # УЖЕ ИЗРАСХОДОВАННОЕ ЗА СУТКИ прибавляется к пределу: перезапуск
        # потока не обнуляет ни сообщения BAM, ни слоты Harmonic.
        было = расход_суток(каталог, начало)
        self.день = было["day"]
        self.было_сообщений = (было["bam_messages"] if фид == ФИД_BAM else 0)
        self.было_слотов = (было["harmonic_slots"] if фид == ФИД_HARMONIC else 0)
        self.было_секунд = (было["harmonic_seconds"] if фид == ФИД_HARMONIC
                             else 0.0)
        self.начало = начало if начало is not None else time.time()
        self.сообщений = 0
        self.слотов = 0
        self.клипов = 0
        self.переподключений = 0
        self.предел_сообщений = (предел_сообщений if предел_сообщений is not None
                                  else (ПРЕДЕЛ_СООБЩЕНИЙ_BAM_В_СУТКИ
                                        if фид == ФИД_BAM else None))
        self.предел_слотов = (предел_слотов if фид == ФИД_HARMONIC else None)
        self.окно_s = (окно_s if окно_s is not None
                        else (ОКНО_HARMONIC_S if фид == ФИД_HARMONIC else None))
        self.закрыт_почему: str | None = None

    def прошло_s(self, сейчас: float | None = None) -> float:
        return round((сейчас if сейчас else time.time()) - self.начало, 2)

    def всего_сообщений(self) -> int:
        return self.было_сообщений + self.сообщений

    def всего_слотов(self) -> int:
        return self.было_слотов + self.слотов

    def проверить(self, сейчас: float | None = None) -> str | None:
        """Что именно закрывает поток. None -- можно продолжать.

        Пределы считаются ЗА СУТКИ, вместе с уже израсходованным до этого
        прогона: иначе перезапуск обнулял бы предел владельца.
        """
        if (self.предел_сообщений is not None
                and self.всего_сообщений() >= self.предел_сообщений):
            return (f"предел сообщений {self.предел_сообщений} за сутки "
                     f"достигнут ({self.всего_сообщений()}, из них в этом "
                     f"прогоне {self.сообщений})")
        if (self.предел_слотов is not None
                and self.всего_слотов() >= self.предел_слотов):
            return (f"предел слотов {self.предел_слотов} за сутки достигнут "
                     f"({self.всего_слотов()}, из них в этом прогоне "
                     f"{self.слотов})")
        if self.окно_s is not None and self.прошло_s(сейчас) >= self.окно_s:
            return (f"окно {self.окно_s} с вышло "
                     f"({self.прошло_s(сейчас)} с)")
        return None

    def учесть(self, вид: str, сейчас: float | None = None) -> None:
        if вид == "transaction":
            self.сообщений += 1
        elif вид in ("slot_start", "slot_end"):
            # Слот считается ОДИН раз -- по началу: у Harmonic на слот
            # приходятся и SlotStart, и SlotEnd, и счёт по обоим удвоил бы
            # цену вдвое против правды.
            if вид == "slot_start":
                self.слотов += 1
        elif вид == "clip":
            self.клипов += 1
        elif вид == "reconnected":
            self.переподключений += 1
        # ЗАПИСЬ РАСХОДА -- по ходу, а не в конце: прогон могут убить, а
        # потраченные слоты и сообщения от этого потраченными быть не
        # перестанут. Раз в 50 событий -- чтобы не писать файл на каждое.
        if (self.сообщений + self.слотов) % 50 == 0:
            self.сохранить(сейчас)
        почему = self.проверить(сейчас)
        if почему:
            self.закрыт_почему = почему
            self.сохранить(сейчас)
            raise ПределДостигнут(почему)

    def сохранить(self, сейчас: float | None = None) -> dict:
        return записать_сутки(
            {"day": self.день, "bam_messages": self.всего_сообщений()
              if self.фид == ФИД_BAM else
              расход_суток(self.каталог, сейчас)["bam_messages"],
              "harmonic_slots": self.всего_слотов() if self.фид == ФИД_HARMONIC
              else расход_суток(self.каталог, сейчас)["harmonic_slots"],
              "harmonic_seconds": (self.было_секунд + self.прошло_s(сейчас))
              if self.фид == ФИД_HARMONIC else
              расход_суток(self.каталог, сейчас)["harmonic_seconds"]},
            self.каталог)

    def признак(self, сейчас: float | None = None) -> dict:
        return {"feed": self.фид, "messages": self.сообщений,
                "messages_day": self.всего_сообщений(),
                "slots": self.слотов, "slots_day": self.всего_слотов(),
                "day": self.день, "clips": self.клипов,
                "reconnects": self.переподключений,
                "elapsed_s": self.прошло_s(сейчас),
                "limit_messages": self.предел_сообщений,
                "limit_slots": self.предел_слотов,
                "window_s": self.окно_s,
                "closed_why": self.закрыт_почему}


# ОДИН ПОТОК НА ХОЗЯЙСТВО. Владелец: "BAM и Harmonic одновременно в двух
# потоках не держать". Замок -- файл в каталоге состояния: два прогона на
# одном хосте не увидят друг друга иначе.
def путь_замка(каталог: str | None = None) -> Path:
    д = каталог or os.environ.get("TRITON_STATE_DIR") or "/tmp"
    return Path(д) / "triton_preconfs.lock"


def занять_замок(*, фид: str, каталог: str | None = None,
                  сейчас: float | None = None) -> dict:
    п = путь_замка(каталог)
    сейчас = сейчас if сейчас is not None else time.time()
    try:
        if п.exists():
            было = json.loads(п.read_text(encoding="utf-8") or "{}")
            # Замок старше двух часов плюс запас -- это след упавшего прогона,
            # а не живой поток: держать зонд закрытым из-за него нельзя.
            if сейчас - float(было.get("ts") or 0) < ОКНО_HARMONIC_S + 600:
                return {"ok": False, "why_not":
                        (f"поток {было.get('feed')} уже открыт с "
                          f"{было.get('utc')} -- два потока сразу владелец "
                          "запретил"), "held": было}
        п.parent.mkdir(parents=True, exist_ok=True)
        п.write_text(json.dumps(
            {"feed": фид, "ts": сейчас,
              "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(сейчас)),
              "pid": os.getpid()}, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "why_not": None, "path": str(п)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}


def снять_замок(каталог: str | None = None) -> None:
    try:
        путь_замка(каталог).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001, S110
        pass


def событие_в_строку(событие: dict, *, фид: str, регион: str,
                      t_recv: float) -> dict:
    """Одна строка журнала зонда: то, что реально пришло, без домыслов."""
    вид = событие.get("kind")
    из_ = {"feed": фид, "region": регион, "kind": вид,
            "t_recv": round(t_recv, 6),
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_recv))}
    if вид == "transaction":
        из_.update(signature=событие.get("signature"),
                   slot=событие.get("slot"),
                   seq=событие.get("seq"),
                   sequence=событие.get("sequence"),
                   bundle_position=событие.get("bundle_position"),
                   result=событие.get("result"),
                   node=событие.get("node"),
                   filters=событие.get("filters"))
    elif вид in ("slot_start", "slot_end"):
        из_["slot"] = событие.get("slot")
    elif вид == "clip":
        из_["clipped"] = событие.get("transactions")
    elif вид == "reconnected":
        из_["attempts"] = событие.get("attempts")
    return из_


# ИМЯ ГРУППЫ ПОЛЕЙ В ОБНОВЛЕНИИ. В preconfs.proto (сохранён в
# data/docs/triton/preconfs.proto) у HarmonicUpdate и BamUpdate это
# "oneof payload", а НЕ "update": первая версия зонда спрашивала "update" и
# поток падал на первом же сообщении с ValueError, а прогон при этом выглядел
# успешным (0 сообщений -- как будто фид молчит). Поэтому имя вынесено в
# константу, а самопроверка сверяет её с сохранённым протоколом.
ГРУППА_ОБНОВЛЕНИЯ = "payload"


def событие_из_обновления(обновление, *, имя_результата=None) -> dict:
    """Обновление gRPC -> та же форма, что у самопроверочного потока.

    Отдельной функцией, потому что именно здесь ошибаются: имена полей у
    Harmonic и BAM разные (seq и result против sequence, bundle_position и
    node), и проверять это надо на заглушках, а не на оплаченном потоке.
    """
    вид = обновление.WhichOneof(ГРУППА_ОБНОВЛЕНИЯ) or "unknown"
    из_: dict = {"kind": вид, "filters": list(getattr(обновление, "filters", []))}
    if вид == "transaction":
        т = обновление.transaction
        из_["slot"] = getattr(т, "slot", None)
        сырое = bytes(getattr(т, "transaction", b"") or b"")
        из_["signature"] = _подпись_из_байтов(сырое) if сырое else None
        # Harmonic: seq и result. BAM: sequence, bundle_position, node,
        # is_revert_on_error. Спрашиваем только то, что у этого типа есть.
        for поле in ("seq", "sequence", "bundle_position", "node",
                      "is_revert_on_error", "region"):
            if hasattr(т, поле):
                из_[поле] = getattr(т, поле)
        if hasattr(т, "result"):
            из_["result"] = (имя_результата(т.result) if имя_результата
                              else т.result)
    elif вид in ("slot_start", "slot_end"):
        из_["slot"] = getattr(getattr(обновление, вид), "slot", None)
    elif вид == "clip":
        из_["transactions"] = getattr(обновление.clip, "transactions", None)
    return из_


def слушать(*, поток, фид: str, регион: str, журнал=None, счёт: Счёт | None = None,
             сейчас_фн=None, признак_каждые: int = 200,
             признак_фн=None) -> dict:
    """Читать поток, писать журнал, считать и ЗАКРЫТЬ по пределу.

    Поток подаётся снаружи (итератор событий) -- поэтому самопроверка гоняет
    ровно эту логику без сети и без оплаченных слотов.
    """
    часы = сейчас_фн or time.time
    с = счёт if счёт is not None else Счёт(фид=фид, начало=часы())
    из_ = {"feed": фид, "region": регион, "rows": 0, "stopped_why": None,
            "counters": None}
    try:
        for событие in поток:
            t = часы()
            стр = событие_в_строку(событие or {}, фид=фид, регион=регион,
                                    t_recv=t)
            if журнал is not None:
                журнал.write(json.dumps(стр, ensure_ascii=False) + "\n")
                журнал.flush()
            из_["rows"] += 1
            try:
                с.учесть(стр["kind"], сейчас=t)
            except ПределДостигнут as пд:
                из_["stopped_why"] = str(пд)
                break
            if признак_фн is not None and из_["rows"] % признак_каждые == 0:
                признак_фн(с.признак(сейчас=t))
    except Exception as exc:  # noqa: BLE001
        из_["stopped_why"] = f"поток оборвался: {type(exc).__name__}: {str(exc)[:200]}"
    с.сохранить(часы())
    из_["counters"] = с.признак(сейчас=часы())
    if признак_фн is not None:
        признак_фн(из_["counters"])
    return из_


# ------------------------------------------------------- сравнение с нашим путём

def _медиана(ряд: list):
    if not ряд:
        return None
    р = sorted(ряд)
    n = len(р)
    return round(р[n // 2] if n % 2 else (р[n // 2 - 1] + р[n // 2]) / 2, 2)


def _p90(ряд: list):
    if not ряд:
        return None
    р = sorted(ряд)
    из_ = р[min(len(р) - 1, int(round(0.9 * (len(р) - 1))))]
    return round(из_, 2)


def сравнить(*, строки_зонда: list, наши_сигналы: list) -> dict:
    """Опережает ли preconf нашу подписку -- по одним и тем же подписям.

    НАШИ СИГНАЛЫ -- это то, что детектор увидел в своей подписке: подпись
    источника и время получения (t_recv из журнала решений). Сравниваем только
    те подписи, что есть в обоих списках: "у нас не было" и "на фиде не было" --
    это разные ответы, и оба важны.
    """
    зонд: dict = {}
    for с in строки_зонда or []:
        if (с or {}).get("kind") != "transaction":
            continue
        п = с.get("signature")
        if not п:
            continue
        # Первое появление -- самое раннее: в этом и смысл preconf.
        if п not in зонд or float(с["t_recv"]) < float(зонд[п]["t_recv"]):
            зонд[п] = с
    наши = {}
    for с in наши_сигналы or []:
        п = (с or {}).get("signature")
        if п and с.get("t_recv") is not None:
            if п not in наши or float(с["t_recv"]) < float(наши[п]["t_recv"]):
                наши[п] = с
    общие = [п for п in наши if п in зонд]
    разницы = []
    строки = []
    for п in общие:
        мс = round((float(наши[п]["t_recv"]) - float(зонд[п]["t_recv"]))
                    * 1000.0, 2)
        разницы.append(мс)
        строки.append({"signature": п, "lead_ms": мс,
                        "preconf_slot": зонд[п].get("slot"),
                        "feed": зонд[п].get("feed"),
                        "result": зонд[п].get("result")})
    раньше = [м for м in разницы if м > 0]
    из_ = {"n_ours": len(наши), "n_preconf": len(зонд), "n_matched": len(общие),
            "coverage": (round(len(общие) / len(наши), 4) if наши else None),
            "lead_ms_median": _медиана(разницы),
            "lead_ms_p90": _p90(разницы),
            "share_preconf_earlier": (round(len(раньше) / len(разницы), 4)
                                       if разницы else None),
            # "preconf был, а транзакция не села" -- по нашим данным это те
            # подписи, что фид отдал, а наша подписка не увидела вовсе. Слово
            # "не села" здесь строго значит "мы её в цепи не видели": проверять
            # цепь отдельными запросами -- отдельные деньги и отдельный шаг.
            "preconf_not_seen_by_us": len([п for п in зонд if п not in наши]),
            "rows": sorted(строки, key=lambda з: з["lead_ms"])}
    из_["share_preconf_not_seen"] = (
        round(из_["preconf_not_seen_by_us"] / len(зонд), 4) if зонд else None)
    return из_


def наши_сигналы_из_журнала(путь: str, *, ключ_времени: str = "t_recv_ts",
                             предел: int = 20000) -> list:
    """Подписи источников и время их получения -- из журнала решений детектора.

    Читаем ровно то, что записал детектор, и ничего не достраиваем: строка без
    времени получения в сравнение не идёт.
    """
    из_: list = []
    п = Path(путь)
    if not п.exists():
        return из_
    with п.open(encoding="utf-8") as ф:
        for строка in ф:
            строка = строка.strip()
            if not строка:
                continue
            try:
                з = json.loads(строка)
            except ValueError:
                continue
            подпись = з.get("signature")
            t = з.get(ключ_времени) or з.get("t_recv") or з.get("t_ok")
            if подпись and t is not None:
                из_.append({"signature": подпись, "t_recv": float(t),
                             "stage": з.get("stage")})
            if len(из_) >= предел:
                break
    return из_


def адреса_источников(*, снимок: str, задачи: tuple = ("BATCH-5", "BATCH-3")
                       ) -> dict:
    """Адреса наших источников -- РАЗБОРОМ ДЕТЕКТОРА, а не своим.

    Второй разбор того же снимка означал бы два списка источников, которые
    когда-нибудь разойдутся молча. Поэтому зовём загрузить_источники из
    bloom_detector: тот же код, что слушает эти адреса в бою. DBot здесь не
    спрашивается вовсе -- зонд только читает снимок из репозитория.
    """
    из_ = {"ok": False, "accounts": [], "by_task": {}, "why_not": None}
    try:
        from pathlib import Path as _P  # noqa: PLC0415

        import bloom_detector as BD  # noqa: PLC0415

        ист = BD.загрузить_источники(_P(снимок), tuple(задачи))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return из_
    if not ист:
        из_["why_not"] = (f"в снимке {снимок} нет адресов задач "
                           f"{', '.join(задачи)}")
        return из_
    из_.update(ok=True, accounts=sorted(ист.keys()), by_task=dict(ист))
    return из_


def self_test() -> int:
    import selftest_guard as _SG  # noqa: PLC0415

    _охрана = _SG.включить()
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    try:
        # 1. Ключ: наружу не выходит, отсутствие названо словами.
        chk("без секрета зонд не идёт и говорит, какого секрета нет",
            токен_задан({})["ok"] is False
            and ИМЯ_СЕКРЕТА in токен_задан({})["why_not"])
        з = токен_задан({ИМЯ_СЕКРЕТА: "СЕКРЕТНЫЙ_ТОКЕН"})
        chk("с секретом -- только длина, без значения",
            з["ok"] and з["length"] == len("СЕКРЕТНЫЙ_ТОКЕН")
            and "СЕКРЕТНЫЙ_ТОКЕН" not in json.dumps(з, ensure_ascii=False), з)

        # 2. Регионы: только свои, и владелец разрешил ams (fra запасным).
        chk("ams у Harmonic -- HARMONIC_REGION_AMS",
            регион_фида(ФИД_HARMONIC, "ams")["value"] == "HARMONIC_REGION_AMS")
        chk("ams у BAM -- BAM_REGION_AMS",
            регион_фида(ФИД_BAM, "ams")["value"] == "BAM_REGION_AMS")
        chk("регион другого фида отвергается ДО подписки",
            регион_фида(ФИД_HARMONIC, "dfw")["ok"] is False,
            регион_фида(ФИД_HARMONIC, "dfw"))
        chk("запасной регион владельца -- fra, и он законный",
            РЕГИОН_ЗАПАСНОЙ == "fra"
            and регион_фида(ФИД_HARMONIC, РЕГИОН_ЗАПАСНОЙ)["ok"])

        # 3. Запрос -- ровно по документации.
        зп = собрать_запрос(фид=ФИД_HARMONIC, регион="ams",
                             аккаунты=["ИСТОЧНИК1", "ИСТОЧНИК2"])
        chk("в запросе фильтр по account_include и регион одним полем",
            зп["ok"]
            and зп["request"]["transactions"]["istochniki"]["account_include"]
            == ["ИСТОЧНИК1", "ИСТОЧНИК2"]
            and зп["request"]["harmonic_region"] == "HARMONIC_REGION_AMS"
            and "bam_region" not in зп["request"], зп)
        chk("пустой фильтр не отправляется: полного потока нам не дадут",
            собрать_запрос(фид=ФИД_BAM, регион="ams", аккаунты=[])["ok"] is False,
            собрать_запрос(фид=ФИД_BAM, регион="ams", аккаунты=[]))
        chk("список аккаунтов сверх предела 10 000 не отправляется",
            собрать_запрос(фид=ФИД_BAM, регион="ams",
                            аккаунты=["А"] * (ПРЕДЕЛ_АККАУНТОВ + 1))["ok"]
            is False)

        # 4. ПРЕДЕЛЫ ВЛАДЕЛЬЦА -- то, что стоит денег. Каталог расхода у
        # каждой проверки СВОЙ временный: суточный счёт на то и суточный, что
        # он переживает прогоны, и общий каталог связал бы проверки между собой
        # (поймано самопроверкой: предел слотов из прошлой проверки закрывал
        # следующую).
        import tempfile as _tп  # noqa: PLC0415

        _вр_дни = _tп.TemporaryDirectory()
        _д = _вр_дни.name
        с = Счёт(фид=ФИД_BAM, начало=1000.0, каталог=f"{_д}/bam")
        chk("у BAM предел -- 5 000 сообщений в сутки и нет предела слотов",
            с.предел_сообщений == 5000 and с.предел_слотов is None, с.признак())
        поток_bam = ({"kind": "transaction", "signature": f"П{i}", "slot": i}
                      for i in range(6000))
        итог = слушать(поток=поток_bam, фид=ФИД_BAM, регион="ams",
                        счёт=с, сейчас_фн=lambda: 1000.0)
        chk("поток BAM закрылся РОВНО на 5 000 сообщений",
            с.сообщений == 5000 and "предел сообщений" in (итог["stopped_why"] or ""),
            (с.сообщений, итог["stopped_why"]))

        сh = Счёт(фид=ФИД_HARMONIC, начало=2000.0, каталог=f"{_д}/harm")
        chk("у Harmonic пределы -- 20 000 слотов и окно 2 часа",
            сh.предел_слотов == 20000 and сh.окно_s == 7200, сh.признак())
        поток_h = ({"kind": ("slot_start" if i % 2 == 0 else "slot_end"),
                     "slot": i // 2} for i in range(60000))
        итог_h = слушать(поток=поток_h, фид=ФИД_HARMONIC, регион="ams",
                          счёт=сh, сейчас_фн=lambda: 2000.0)
        chk("слот считается один раз (по началу), а не дважды",
            сh.слотов == 20000 and "предел слотов" in (итог_h["stopped_why"] or ""),
            (сh.слотов, итог_h["stopped_why"]))

        # ОКНО ВРЕМЕНИ закрывает поток, даже если слотов мало.
        часы = {"t": 3000.0}
        сw = Счёт(фид=ФИД_HARMONIC, начало=3000.0, каталог=f"{_д}/win")

        def тикающий():
            for i in range(100):
                часы["t"] = 3000.0 + i * 100.0
                yield {"kind": "transaction", "signature": f"W{i}", "slot": i}

        итог_w = слушать(поток=тикающий(), фид=ФИД_HARMONIC, регион="ams",
                          счёт=сw, сейчас_фн=lambda: часы["t"])
        chk("окно 2 часа закрывает поток Harmonic по времени",
            "окно" in (итог_w["stopped_why"] or "")
            and сw.прошло_s(часы["t"]) >= ОКНО_HARMONIC_S,
            (итог_w["stopped_why"], сw.прошло_s(часы["t"])))
        chk("в признаке видно и сообщения, и слоты, и причину закрытия",
            all(к in сw.признак() for к in ("messages", "slots", "closed_why")),
            сw.признак())

        # СУТОЧНОСТЬ ПРЕДЕЛА: перезапуск потока НЕ обнуляет расход.
        с2 = Счёт(фид=ФИД_BAM, начало=1000.0, каталог=f"{_д}/bam")
        chk("новый прогон видит уже израсходованное за сутки",
            с2.было_сообщений == 5000 and с2.всего_сообщений() == 5000,
            с2.признак())
        закрыт = слушать(поток=({"kind": "transaction", "signature": "ещё"}
                                 for _ in range(3)),
                          фид=ФИД_BAM, регион="ams", счёт=с2,
                          сейчас_фн=lambda: 1000.0)
        chk("после предела новый прогон закрывается СРАЗУ, а не тратит ещё 5 000",
            с2.сообщений <= 1 and "за сутки" in (закрыт["stopped_why"] or ""),
            (с2.сообщений, закрыт["stopped_why"]))
        _вр_дни.cleanup()

        # 5. ДВА ПОТОКА СРАЗУ -- ЗАПРЕЩЕНЫ.
        import tempfile as _t  # noqa: PLC0415

        with _t.TemporaryDirectory() as вр:
            chk("первый поток замок берёт",
                занять_замок(фид=ФИД_BAM, каталог=вр, сейчас=5000.0)["ok"])
            второй = занять_замок(фид=ФИД_HARMONIC, каталог=вр, сейчас=5001.0)
            chk("второй поток не запускается, пока первый жив",
                второй["ok"] is False and "два потока" in (второй["why_not"] or ""),
                второй)
            chk("замок старого упавшего прогона не держит зонд навсегда",
                занять_замок(фид=ФИД_HARMONIC, каталог=вр,
                              сейчас=5000.0 + ОКНО_HARMONIC_S + 601)["ok"])
            снять_замок(вр)
            chk("после снятия замка поток снова можно открыть",
                занять_замок(фид=ФИД_BAM, каталог=вр, сейчас=6000.0)["ok"])

        # 6. Строка журнала: что пришло, то и записано.
        стр = событие_в_строку(
            {"kind": "transaction", "signature": "ПОДПИСЬ", "slot": 7,
              "seq": 3, "result": "success", "filters": ["istochniki"]},
            фид=ФИД_HARMONIC, регион="ams", t_recv=1790000000.123456)
        chk("в строке журнала есть подпись, слот, номер партии и результат",
            стр["signature"] == "ПОДПИСЬ" and стр["slot"] == 7
            and стр["seq"] == 3 and стр["result"] == "success"
            and стр["utc"].endswith("Z"), стр)
        chk("clip записывается как clip, а не теряется",
            событие_в_строку({"kind": "clip", "transactions": 12},
                              фид=ФИД_BAM, регион="ams",
                              t_recv=1.0)["clipped"] == 12)

        # 7. СРАВНЕНИЕ. Считаем на цифрах, где ответ известен заранее.
        зонд_строки = [
            {"kind": "transaction", "signature": "A", "t_recv": 100.0, "slot": 1},
            {"kind": "transaction", "signature": "B", "t_recv": 200.0, "slot": 2},
            {"kind": "transaction", "signature": "C", "t_recv": 300.0, "slot": 3},
            {"kind": "slot_start", "slot": 1, "t_recv": 99.0},
        ]
        наши = [{"signature": "A", "t_recv": 100.4},
                 {"signature": "B", "t_recv": 199.9},
                 {"signature": "D", "t_recv": 400.0}]
        св = сравнить(строки_зонда=зонд_строки, наши_сигналы=наши)
        chk("сравниваются только общие подписи", св["n_matched"] == 2, св)
        chk("опережение считается в миллисекундах и со знаком",
            sorted(з["lead_ms"] for з in св["rows"]) == [-100.0, 400.0], св["rows"])
        chk("медиана и p90 посчитаны", св["lead_ms_median"] == 150.0
            and св["lead_ms_p90"] == 400.0, св)
        chk("доля, где preconf раньше -- половина",
            св["share_preconf_earlier"] == 0.5, св)
        chk("покрытие: две наши подписи из трёх нашлись на фиде",
            св["coverage"] == round(2 / 3, 4), св)
        chk("preconf был, а мы его не видели -- посчитано отдельно",
            св["preconf_not_seen_by_us"] == 1
            and св["share_preconf_not_seen"] == round(1 / 3, 4), св)
        chk("рамки слота в сравнение не идут: это не транзакции",
            all(з["signature"] in ("A", "B") for з in св["rows"]), св["rows"])

        # 6б. РАЗБОР ОБНОВЛЕНИЯ. Именно здесь зонд уже ошибся 25.09: он
        # спрашивал oneof "update", а в протоколе он называется "payload", и
        # поток падал на первом сообщении, выглядя как молчащий фид.
        прото = Path("data/docs/triton/preconfs.proto")
        if not прото.exists():
            прото = Path("../data/docs/triton/preconfs.proto")
        if прото.exists():
            текст_прото = прото.read_text(encoding="utf-8")
            chk("имя группы полей сверено с сохранённым протоколом",
                f"oneof {ГРУППА_ОБНОВЛЕНИЯ}" in текст_прото
                and "oneof update" not in текст_прото, ГРУППА_ОБНОВЛЕНИЯ)
            chk("в протоколе есть поля Harmonic (seq, result) и BAM (sequence, node)",
                all(п in текст_прото for п in ("uint64 seq", "ExecutionResult result",
                                                "uint64 sequence", "string node")),
                "")
        else:
            chk("протокол не скачан -- сверка имён пропущена", True, str(прото))

        class ОбновлениеЗаглушка:
            """Ведёт себя как сообщение protobuf: WhichOneof и поля."""

            def __init__(self, вид, полезное, фильтры=()):
                self._вид = вид
                self.filters = list(фильтры)
                setattr(self, вид, полезное)

            def WhichOneof(self, имя):  # noqa: N802
                return self._вид if имя == ГРУППА_ОБНОВЛЕНИЯ else None

        class ТхH:
            transaction = b""
            slot = 321
            seq = 4
            result = 0
            region = "ams"

        class ТхB:
            transaction = b""
            slot = 654
            sequence = 99
            bundle_position = 1
            node = "ams-node"
            is_revert_on_error = False

        сh = событие_из_обновления(
            ОбновлениеЗаглушка("transaction", ТхH(), ["istochniki"]),
            имя_результата=lambda з: "EXECUTION_RESULT_SUCCESS")
        chk("Harmonic: слот, партия seq и результат разобраны",
            сh["kind"] == "transaction" and сh["slot"] == 321
            and сh["seq"] == 4 and сh["result"] == "EXECUTION_RESULT_SUCCESS"
            and сh["filters"] == ["istochniki"], сh)
        сb = событие_из_обновления(ОбновлениеЗаглушка("transaction", ТхB()))
        chk("BAM: слот, sequence, позиция в бандле и узел разобраны",
            сb["slot"] == 654 and сb["sequence"] == 99
            and сb["bundle_position"] == 1 and сb["node"] == "ams-node", сb)
        chk("у BAM нет ни seq, ни result -- и мы их не придумываем",
            "seq" not in сb and "result" not in сb, сb)

        class Слот:
            slot = 777

        сс = событие_из_обновления(ОбновлениеЗаглушка("slot_start", Слот()))
        chk("рамка слота разобрана как рамка",
            сс["kind"] == "slot_start" and сс["slot"] == 777, сс)

        class Клип:
            transactions = 5

        ск = событие_из_обновления(ОбновлениеЗаглушка("clip", Клип()))
        chk("clip разобран и число удержанных видно",
            ск["kind"] == "clip" and ск["transactions"] == 5, ск)
        chk("незнакомая группа не роняет разбор, а называется unknown",
            событие_из_обновления(
                ОбновлениеЗаглушка("ping", object()))["kind"] == "ping"
            and событие_из_обновления(
                ОбновлениеЗаглушка("что-то", object()))["kind"] == "что-то")

        # 7б. АДРЕСА ИСТОЧНИКОВ -- разбором детектора, без второго списка.
        сн = "data/final/20260923T145755Z/konfig.json"
        if not Path(сн).exists():
            сн = "../" + сн
        if Path(сн).exists():
            зд = адреса_источников(снимок=сн)
            chk("адреса источников читаются разбором детектора и их больше нуля",
                зд["ok"] and len(зд["accounts"]) >= 5, зд.get("why_not"))
            chk("в списке только base58-адреса нужной длины",
                all(32 <= len(а) <= 44 for а in зд["accounts"]),
                зд["accounts"][:3])
            chk("фильтр для них собирается и укладывается в пределы",
                собрать_запрос(фид=ФИД_BAM, регион="ams",
                                аккаунты=зд["accounts"])["ok"], "")
        else:
            chk("снимок задач не найден -- проверка адресов пропущена", True, сн)
        chk("отсутствие задачи в снимке названо словами, а не пустым списком",
            адреса_источников(снимок=сн, задачи=("НЕТ_ТАКОЙ",))["why_not"]
            is not None)

        # 8. Чтение наших сигналов из журнала решений -- только со временем.
        with _t.TemporaryDirectory() as вр2:
            ж = Path(вр2) / "decisions.jsonl"
            ж.write_text(
                json.dumps({"stage": "signal", "signature": "X",
                             "t_recv_ts": 1.5}) + "\n"
                + json.dumps({"stage": "signal", "signature": "Y"}) + "\n"
                + "не json\n"
                + json.dumps({"stage": "signal", "signature": "Z",
                               "t_recv_ts": 2.5}) + "\n", encoding="utf-8")
            ряд = наши_сигналы_из_журнала(str(ж))
            chk("из журнала берутся только строки с подписью И временем",
                [з["signature"] for з in ряд] == ["X", "Z"], ряд)
            chk("битая строка журнала не роняет разбор", len(ряд) == 2)
    finally:
        _SG.выключить(_охрана)

    плохо = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}"
              + ("" if ок else f" -- факт: {факт}"))
    print(f"самопроверка зонда Triton: {len(проверки) - len(плохо)}/"
          f"{len(проверки)} пройдено")
    return 1 if плохо else 0


def _поток_grpc(*, фид: str, регион: str, аккаунты: list, ключ: str,
                 таймаут_s: float):
    """Настоящий поток gRPC. Стабы генерируются из preconfs.proto ДО запуска
    (это делает прогон), модуль их только импортирует."""
    import grpc  # noqa: PLC0415
    import preconfs_pb2  # noqa: PLC0415
    import preconfs_pb2_grpc  # noqa: PLC0415

    зп = собрать_запрос(фид=фид, регион=регион, аккаунты=аккаунты)
    if not зп["ok"]:
        raise RuntimeError(зп["why_not"])
    канал = grpc.secure_channel(ТОЧКА_ВХОДА, grpc.ssl_channel_credentials())
    мета = (("x-token", ключ),)
    служба = (preconfs_pb2_grpc.HarmonicStub(канал) if фид == ФИД_HARMONIC
              else preconfs_pb2_grpc.BAMStub(канал))
    # GetVersion -- первым: он дёшев, требует того же ключа и сразу говорит,
    # какая точка присутствия ответила. Ключ не печатаем.
    версия = служба.GetVersion(preconfs_pb2.VersionRequest(), metadata=мета,
                               timeout=20)
    print(f"GetVersion: версия {версия.version}, точка присутствия "
          f"{версия.region}")
    запрос = preconfs_pb2.SubscribeRequest()
    from google.protobuf import json_format  # noqa: PLC0415

    json_format.ParseDict(зп["request"], запрос)
    поток = служба.Subscribe(запрос, metadata=мета, timeout=таймаут_s)
    имя_результата = None
    if hasattr(preconfs_pb2, "ExecutionResult"):
        имя_результата = preconfs_pb2.ExecutionResult.Name
    for обновление in поток:
        yield событие_из_обновления(обновление, имя_результата=имя_результата)


def _подпись_из_байтов(сырое: bytes) -> str | None:
    """Первая подпись -- 64 байта после compact-u16 счётчика подписей.

    Ровно как в документации ("The first signature is the 64 bytes after the
    compact-u16 signature count"). base58 берём у solders, чтобы не завести в
    репозитории вторую реализацию кодировки.
    """
    try:
        i, сдвиг, счёт = 0, 0, 0
        while i < len(сырое):
            б = сырое[i]
            счёт |= (б & 0x7F) << сдвиг
            i += 1
            if not (б & 0x80):
                break
            сдвиг += 7
        if счёт < 1 or len(сырое) < i + 64:
            return None
        from solders.signature import Signature  # noqa: PLC0415

        return str(Signature.from_bytes(сырое[i:i + 64]))
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    import argparse

    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--feed", default=ФИД_BAM, choices=[ФИД_BAM, ФИД_HARMONIC])
    р.add_argument("--region", default=РЕГИОН_ОСНОВНОЙ)
    р.add_argument("--accounts", default="",
                    help="адреса источников через запятую")
    р.add_argument("--accounts-file", default=None)
    р.add_argument("--accounts-from-snapshot", default=None,
                    help="снимок konfig.json: адреса источников разбором детектора")
    р.add_argument("--accounts-out", default=None,
                    help="только выписать адреса источников в файл и выйти")
    р.add_argument("--tasks", default="BATCH-5,BATCH-3")
    р.add_argument("--seconds", type=float, default=None,
                    help="сколько держать поток (по умолчанию -- предел фида)")
    р.add_argument("--out", default=None, help="журнал зонда, jsonl")
    р.add_argument("--status", default=None, help="признак жизни зонда, json")
    р.add_argument("--state-dir", default=None)
    р.add_argument("--report-log", default=None,
                    help="только отчёт: журнал зонда")
    р.add_argument("--report-decisions", default=None,
                    help="только отчёт: журнал решений детектора")
    р.add_argument("--report-out", default=None)
    а = р.parse_args()
    if а.self_test:
        return self_test()

    if а.report_log:
        строки = []
        with open(а.report_log, encoding="utf-8") as ф:
            for с in ф:
                с = с.strip()
                if с:
                    try:
                        строки.append(json.loads(с))
                    except ValueError:
                        continue
        наши = (наши_сигналы_из_журнала(а.report_decisions)
                if а.report_decisions else [])
        св = сравнить(строки_зонда=строки, наши_сигналы=наши)
        печать = {к: v for к, v in св.items() if к != "rows"}
        print(json.dumps(печать, ensure_ascii=False, indent=2))
        if а.report_out:
            with open(а.report_out, "w", encoding="utf-8") as ф:
                json.dump(св, ф, ensure_ascii=False, indent=2)
            print(f"записано: {а.report_out}")
        return 0

    if а.accounts_out or а.accounts_from_snapshot:
        зд = адреса_источников(
            снимок=(а.accounts_from_snapshot
                    or "data/final/20260923T145755Z/konfig.json"),
            задачи=tuple(з.strip() for з in а.tasks.split(",") if з.strip()))
        if not зд["ok"]:
            print(f"СБОЙ: {зд['why_not']}")
            return 1
        print(f"адресов источников: {len(зд['accounts'])} "
              f"(задачи: {а.tasks})")
        if а.accounts_out:
            Path(а.accounts_out).write_text("\n".join(зд["accounts"]) + "\n",
                                             encoding="utf-8")
            print(f"записано: {а.accounts_out}")
            return 0

    т = токен_задан()
    print(json.dumps(т, ensure_ascii=False))
    if not т["ok"]:
        return 1
    аккаунты = [с.strip() for с in (а.accounts or "").split(",") if с.strip()]
    if а.accounts_file:
        аккаунты += [с.strip() for с in
                      Path(а.accounts_file).read_text(encoding="utf-8").split()
                      if с.strip()]
    зп = собрать_запрос(фид=а.feed, регион=а.region, аккаунты=аккаунты)
    if not зп["ok"]:
        print(f"СБОЙ: {зп['why_not']}")
        return 1
    замок = занять_замок(фид=а.feed, каталог=а.state_dir)
    if not замок["ok"]:
        print(f"СБОЙ: {замок['why_not']}")
        return 1
    счёт = Счёт(фид=а.feed, окно_s=(а.seconds if а.seconds else None),
                 каталог=а.state_dir)
    путь_признака = а.status

    def признак(з):
        if путь_признака:
            with open(путь_признака, "w", encoding="utf-8") as ф:
                json.dump({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime()),
                            "region": а.region, **з}, ф, ensure_ascii=False,
                           indent=2)

    журнал = open(а.out, "a", encoding="utf-8") if а.out else None  # noqa: SIM115
    try:
        поток = _поток_grpc(фид=а.feed, регион=а.region, аккаунты=аккаунты,
                             ключ=токен(), таймаут_s=(а.seconds or
                                                      (счёт.окно_s or 7200)))
        итог = слушать(поток=поток, фид=а.feed, регион=а.region, журнал=журнал,
                        счёт=счёт, признак_фн=признак)
    finally:
        if журнал is not None:
            журнал.close()
        снять_замок(а.state_dir)
    print(json.dumps({к: v for к, v in итог.items()}, ensure_ascii=False,
                      indent=2))
    # ОБРЫВ -- ЭТО СБОЙ, А НЕ ТИШИНА ФИДА. 25.09 зонд упал на первом
    # сообщении (спрашивал не то имя группы полей), прогон при этом был
    # "успешным" с нулём сообщений, и это выглядело как молчащий фид. Предел
    # владельца -- другое дело: он закрывает поток штатно.
    почему = итог.get("stopped_why") or ""
    if почему.startswith("поток оборвался"):
        print(f"СБОЙ: {почему}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
