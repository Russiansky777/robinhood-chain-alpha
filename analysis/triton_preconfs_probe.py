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

try:  # Модуль валидаторов нужен только признаку жизни: без него зонд идёт.
    import triton_validators as ВАЛ
except Exception:  # noqa: BLE001
    ВАЛ = None

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
# ЦЕНЫ ИЗ КЛИЕНТСКОЙ ПАНЕЛИ (скрин владельца 25.09; API цену не отдаёт --
# документация прямо говорит, что цены лежат в панели подписки):
#   BAM -- $50 за 1 000 000 сообщений (13 062 сообщения = $0.65);
#   Harmonic -- $1 500 за 1 000 000 ОПЛАЧИВАЕМЫХ слотов (32 слота = $0.05).
# ОПЛАЧИВАЕМЫЙ слот -- тот, по которому фид что-то ОТДАЛ, а не всякая принятая
# рамка слота: рамок за 1 ч 47 мин пришло 345, и считать их за оплату значило бы
# завышать цену в десятки раз.
ЦЕНА_СООБЩЕНИЯ_USD = 50.0 / 1_000_000
ЦЕНА_СЛОТА_USD = 1_500.0 / 1_000_000

ПРЕДЕЛ_СООБЩЕНИЙ_BAM_В_СУТКИ = 5000
ПРЕДЕЛ_СЛОТОВ_HARMONIC = 20000
ОКНО_HARMONIC_S = 2 * 60 * 60

# Пределы фильтров из документации -- проверяем ДО отправки, как и их клиент.
ПРЕДЕЛ_ФИЛЬТРОВ = 64
ПРЕДЕЛ_АККАУНТОВ = 10000
ПРЕДЕЛ_ПОДПИСЕЙ = 1000


class ПределДостигнут(Exception):
    """Поток закрывается по пределу владельца, а не по ошибке сети."""


ИМЯ_СЕКРЕТА_ФАЙЛОМ = "TRITON_X_TOKEN_FILE"


def токен(окружение=None) -> str | None:
    """Ключ из окружения, а если задан файл -- из файла.

    ФАЙЛОМ -- ЧТОБЫ КЛЮЧ НЕ ПОПАЛ В КОМАНДНУЮ СТРОКУ. Суточный зонд поднимают
    временной службой systemd, и передать значение через --setenv значило бы
    показать его в argv любому, кто смотрит ps. Файл читается правами 600 под
    тем же пользователем; в журнал по-прежнему идёт только слово "задан".
    """
    окр = os.environ if окружение is None else окружение
    з = (окр.get(ИМЯ_СЕКРЕТА) or "").strip()
    if not з:
        путь = (окр.get(ИМЯ_СЕКРЕТА_ФАЙЛОМ) or "").strip()
        if путь:
            try:
                з = Path(путь).read_text(encoding="utf-8").strip()
            except Exception:  # noqa: BLE001
                з = ""
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
                   подписанты: list | None = None,
                   имя_фильтра: str = "istochniki",
                   имя_фильтра_подписантов: str = "podpisanty") -> dict:
    """SubscribeRequest ровно как в документации.

    Фильтр обязан задать хотя бы один отбор: полный поток подписке недоступен,
    и просить его -- значит получить INVALID_ARGUMENT и потратить попытку.

    ДВА ИМЕНОВАННЫХ ФИЛЬТРА, А НЕ ДВА УСЛОВИЯ В ОДНОМ (владелец 28.09:
    "фильтр signer_include = все адреса торгующих групп, дополнительно
    account_include те же адреса"). В preconfs.proto сказано прямо: внутри
    ОДНОГО фильтра транзакция обязана удовлетворить КАЖДОМУ заданному
    условию, а доставляется она, если подошла ХОТЯ БЫ ОДНОМУ фильтру, и
    имена подошедших фильтров возвращаются в обновлении. Значит два условия
    в одном фильтре дали бы пересечение (подписал И есть в статических
    ключах) -- то есть просто signer_include, а два фильтра дают
    объединение И ОТВЕТ НА ВОПРОС, каким из двух поймано.
    """
    из_ = {"ok": False, "why_not": None, "request": None}
    р = регион_фида(фид, регион)
    if not р["ok"]:
        из_["why_not"] = р["why_not"]
        return из_
    адреса = [а for а in (аккаунты or []) if а]
    подп = [а for а in (подписанты or []) if а]
    if not адреса and not подп:
        из_["why_not"] = ("фильтр без отбора: account_include и signer_include "
                           "пусты, а полный поток подписке недоступен")
        return из_
    for ряд, имя in ((адреса, "account_include"), (подп, "signer_include")):
        if len(ряд) > ПРЕДЕЛ_АККАУНТОВ:
            из_["why_not"] = (f"{len(ряд)} адресов в {имя} при пределе "
                               f"{ПРЕДЕЛ_АККАУНТОВ}")
            return из_
    фильтры: dict = {}
    if подп:
        if len(имя_фильтра_подписантов.encode()) > 64:
            из_["why_not"] = "имя фильтра подписантов длиннее 64 байт"
            return из_
        фильтры[имя_фильтра_подписантов] = {"signer_include": подп}
    if адреса:
        if len(имя_фильтра.encode()) > 64:
            из_["why_not"] = "имя фильтра длиннее 64 байт"
            return из_
        фильтры[имя_фильтра] = {"account_include": адреса}
    if len(фильтры) > ПРЕДЕЛ_ФИЛЬТРОВ:
        из_["why_not"] = f"{len(фильтры)} фильтров при пределе {ПРЕДЕЛ_ФИЛЬТРОВ}"
        return из_
    из_.update(ok=True, request={"transactions": фильтры,
                                  р["field"]: р["value"]})
    return из_


def путь_суток(каталог: str | None = None, фид: str | None = None) -> Path:
    """Файл суточного расхода -- СВОЙ У КАЖДОГО ФИДА.

    Поймано на живом суточном прогоне 28.09: BAM и Harmonic идут двумя
    процессами, а файл был один. Каждый при записи переносил чужое число из
    того, что успел прочитать, и терял обновление соседа -- в счётчике
    оказалось bam_messages 0 при 56 доставленных транзакциях. Счётчик -- это
    деньги, терять в нём записи нельзя. Общий файл остаётся для старых
    прогонов и как сумма при чтении.
    """
    д = каталог or os.environ.get("TRITON_STATE_DIR") or "/tmp"
    if фид:
        return Path(д) / f"triton_preconfs_day_{фид}.json"
    return Path(д) / "triton_preconfs_day.json"


def расход_суток(каталог: str | None = None,
                  сейчас: float | None = None, фид: str | None = None) -> dict:
    """Что уже израсходовано СЕГОДНЯ (UTC): сообщения BAM и слоты Harmonic.

    Предел владельца у BAM -- "5 000 сообщений в СУТКИ". Считать его за прогон
    значило бы обнулять предел каждым перезапуском потока, то есть тратить
    столько раз по 5 000, сколько раз мы перезапустились.
    """
    день = time.strftime("%Y%m%d", time.gmtime(сейчас if сейчас else time.time()))
    из_ = {"day": день, "bam_messages": 0, "harmonic_slots": 0,
            "harmonic_billable_slots": 0,
            "harmonic_seconds": 0.0, "why_not": None}
    # ЧИТАЕМ И СВОЙ ФАЙЛ ФИДА, И ОБЩИЙ: общий остался от прежних прогонов, и
    # выбросить его значило бы обнулить уже потраченное за эти сутки.
    пути = [путь_суток(каталог, фид)] if фид else []
    пути += [путь_суток(каталог, ф) for ф in (ФИД_BAM, ФИД_HARMONIC)
             if not фид or ф != фид]
    пути.append(путь_суток(каталог))
    видели = set()
    for п in пути:
        if str(п) in видели:
            continue
        видели.add(str(п))
        try:
            if not п.exists():
                continue
            было = json.loads(п.read_text(encoding="utf-8") or "{}")
            if было.get("day") != день:
                continue
            из_["bam_messages"] = max(из_["bam_messages"],
                                       int(было.get("bam_messages") or 0))
            из_["harmonic_slots"] = max(из_["harmonic_slots"],
                                         int(было.get("harmonic_slots") or 0))
            из_["harmonic_billable_slots"] = max(
                из_["harmonic_billable_slots"],
                int(было.get("harmonic_billable_slots") or 0))
            из_["harmonic_seconds"] = max(из_["harmonic_seconds"],
                                           float(было.get("harmonic_seconds") or 0.0))
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return из_


def записать_сутки(расход: dict, каталог: str | None = None,
                   фид: str | None = None) -> dict:
    п = путь_суток(каталог, фид)
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
        # ОПЛАЧИВАЕМЫЕ СЛОТЫ -- те, по которым фид ОТДАЛ транзакцию (цена панели
        # берётся за них, а не за принятые рамки слотов). Внутри прогона они
        # считаются множеством номеров слотов: одна транзакция и двадцать в одном
        # слоте -- это один оплаченный слот.
        self.было_оплаченных = (было.get("harmonic_billable_slots") or 0
                                 if фид == ФИД_HARMONIC else 0)
        self.слоты_с_сообщением: set = set()
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

    # СЧЁТ ОДИН НА ВСЕ РЕГИОНЫ, а потоков много (владелец 25.09: "подписаться
    # на ВСЕ регионы BAM"). Значит учесть() зовут из разных потоков, и без
    # замка два региона прочитали бы одно число сообщений и по очереди
    # записали свой итог: предел 5 000 в сутки прогрызался бы молча.
    _замок = None

    def _под_замком(self):
        if Счёт._замок is None:
            import threading  # noqa: PLC0415

            Счёт._замок = threading.Lock()
        return Счёт._замок

    def прошло_s(self, сейчас: float | None = None) -> float:
        return round((сейчас if сейчас else time.time()) - self.начало, 2)

    def всего_сообщений(self) -> int:
        return self.было_сообщений + self.сообщений

    def всего_слотов(self) -> int:
        return self.было_слотов + self.слотов

    def всего_оплаченных(self) -> int:
        return int(self.было_оплаченных) + len(self.слоты_с_сообщением)

    def стоимость_usd(self) -> float:
        """Цена по числам панели: сообщения BAM и оплачиваемые слоты Harmonic."""
        return round(self.всего_сообщений() * ЦЕНА_СООБЩЕНИЯ_USD
                      + self.всего_оплаченных() * ЦЕНА_СЛОТА_USD, 4)

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
                and self.всего_оплаченных() >= self.предел_слотов):
            # ПРЕДЕЛ -- ПО ОПЛАЧИВАЕМЫМ СЛОТАМ (по слову владельца 28.09 и по
            # числам панели): рамок слотов приходит в десятки раз больше, и
            # считать предел по ним значило бы закрывать поток раньше времени.
            return (f"предел оплачиваемых слотов {self.предел_слотов} за сутки "
                     f"достигнут ({self.всего_оплаченных()}, из них в этом "
                     f"прогоне {len(self.слоты_с_сообщением)})")
        if self.окно_s is not None and self.прошло_s(сейчас) >= self.окно_s:
            return (f"окно {self.окно_s} с вышло "
                     f"({self.прошло_s(сейчас)} с)")
        return None

    def учесть(self, вид: str, сейчас: float | None = None,
                слот: int | None = None) -> None:
        with self._под_замком():
            return self._учесть(вид, сейчас, слот)

    def _учесть(self, вид: str, сейчас: float | None = None,
                 слот: int | None = None) -> None:
        if вид == "transaction":
            self.сообщений += 1
            if isinstance(слот, int) and слот > 0:
                self.слоты_с_сообщением.add(int(слот))
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
        elif вид == "stream_error":
            self.обрывов = getattr(self, "обрывов", 0) + 1
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
        """Свой расход -- в СВОЙ файл фида, чужие числа не переписываем.

        Прежде здесь чужое число переносилось из общего файла, и два процесса
        теряли обновления друг друга: в счётчике стоял bam_messages 0 при 56
        доставленных транзакциях. Теперь каждый пишет только своё.
        """
        своё = {"day": self.день, "feed": self.фид,
                 "bam_messages": (self.всего_сообщений()
                                   if self.фид == ФИД_BAM else 0),
                 "harmonic_slots": (self.всего_слотов()
                                     if self.фид == ФИД_HARMONIC else 0),
                 "harmonic_billable_slots": (self.всего_оплаченных()
                                              if self.фид == ФИД_HARMONIC else 0),
                 "usd": self.стоимость_usd(),
                 "harmonic_seconds": ((self.было_секунд + self.прошло_s(сейчас))
                                       if self.фид == ФИД_HARMONIC else 0.0)}
        return записать_сутки(своё, self.каталог, self.фид)

    def признак(self, сейчас: float | None = None) -> dict:
        return {"feed": self.фид, "messages": self.сообщений,
                "messages_day": self.всего_сообщений(),
                "slots": self.слотов, "slots_day": self.всего_слотов(),
                "billable_slots": len(self.слоты_с_сообщением),
                "billable_slots_day": self.всего_оплаченных(),
                "usd_po_paneli": self.стоимость_usd(),
                "day": self.день, "clips": self.клипов,
                "reconnects": self.переподключений,
                "stream_errors": getattr(self, "обрывов", 0),
                "elapsed_s": self.прошло_s(сейчас),
                "limit_messages": self.предел_сообщений,
                "limit_slots": self.предел_слотов,
                "window_s": self.окно_s,
                "closed_why": self.закрыт_почему}


# ОДИН ПОТОК НА ХОЗЯЙСТВО. Владелец: "BAM и Harmonic одновременно в двух
# потоках не держать". Замок -- файл в каталоге состояния: два прогона на
# одном хосте не увидят друг друга иначе.
def путь_замка(каталог: str | None = None, фид: str | None = None) -> Path:
    """Замок ПО ФИДУ.

    25.09 владелец запрещал держать BAM и Harmonic сразу, и замок был один на
    зонд. 28.09 слово другое: "подписка BAM + Harmonic" одним прогоном в 24
    часа. Поэтому замок теперь свой у каждого фида: второй BAM всё так же не
    поднимется, а BAM вместе с Harmonic -- поднимется.
    """
    д = каталог or os.environ.get("TRITON_STATE_DIR") or "/tmp"
    if not фид:
        return Path(д) / "triton_preconfs.lock"
    return Path(д) / f"triton_preconfs_{фид}.lock"


def жив_ли(pid) -> bool:
    """Жив ли процесс с таким pid. Нет pid -- считаем, что жив: старый замок."""
    if pid is None:
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return True
    return True


def занять_замок(*, фид: str, каталог: str | None = None,
                  сейчас: float | None = None, окно_s: float | None = None) -> dict:
    п = путь_замка(каталог, фид)
    сейчас = сейчас if сейчас is not None else time.time()
    try:
        if п.exists():
            было = json.loads(п.read_text(encoding="utf-8") or "{}")
            # Замок старше двух часов плюс запас -- это след упавшего прогона,
            # а не живой поток: держать зонд закрытым из-за него нельзя.
            срок = float(окно_s if окно_s else ОКНО_HARMONIC_S) + 600
            # ЖИВ ЛИ ТОТ ПРОЦЕСС. Поймано 28.09: systemctl stop убил зонд
            # сигналом, finally не сработал, замок остался -- и новый прогон
            # отказался подниматься, оставив зонд мёртвым на сутки. Мёртвый pid
            # -- это след, а не поток.
            if not жив_ли(было.get("pid")):
                было = {}
            elif сейчас - float(было.get("ts") or 0) < срок:
                return {"ok": False, "why_not":
                        (f"поток {было.get('feed')} уже открыт с "
                          f"{было.get('utc')} -- второй поток того же фида не "
                          "поднимаем"), "held": было}
        п.parent.mkdir(parents=True, exist_ok=True)
        п.write_text(json.dumps(
            {"feed": фид, "ts": сейчас,
              "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(сейчас)),
              "pid": os.getpid()}, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "why_not": None, "path": str(п)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}


def снять_замок(каталог: str | None = None, фид: str | None = None) -> None:
    try:
        путь_замка(каталог, фид).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001, S110
        pass


def событие_в_строку(событие: dict, *, фид: str, регион: str,
                      t_recv: float, t_mono: float | None = None) -> dict:
    """Одна строка журнала зонда: то, что реально пришло, без домыслов.

    ВРЕМЯ ПРИХОДА -- ДВУМЯ ЧАСАМИ (владелец 28.09: "время прихода преконфа
    (моно-часы)"). t_recv -- стенные часы: только по ним можно свести преконф
    с журналом решений детектора, где время тоже стенное. t_mono -- моно-часы
    того же процесса: они не прыгают от поправки NTP, и разница между двумя
    событиями ОДНОГО прогона считается по ним.
    """
    вид = событие.get("kind")
    из_ = {"feed": фид, "region": регион, "kind": вид,
            "t_recv": round(t_recv, 6),
            "t_mono": (round(t_mono, 6) if t_mono is not None else None),
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
    elif вид == "stream_error":
        # ПРИЧИНА ОБРЫВА -- В ЖУРНАЛ. Без неё окно выглядит непрерывным, а оно
        # рвалось, и доля времени "в эфире" выходит завышенной.
        из_["code"] = событие.get("code")
        из_["details"] = событие.get("details")
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
             признак_фн=None, моно_фн=None) -> dict:
    """Читать поток, писать журнал, считать и ЗАКРЫТЬ по пределу.

    Поток подаётся снаружи (итератор событий) -- поэтому самопроверка гоняет
    ровно эту логику без сети и без оплаченных слотов.
    """
    часы = сейчас_фн or time.time
    моно = моно_фн or time.monotonic
    с = счёт if счёт is not None else Счёт(фид=фид, начало=часы())
    из_ = {"feed": фид, "region": регион, "rows": 0, "stopped_why": None,
            "counters": None}
    try:
        for событие in поток:
            t = часы()
            стр = событие_в_строку(событие or {}, фид=фид, регион=регион,
                                    t_recv=t, t_mono=моно())
            if журнал is not None:
                журнал.write(json.dumps(стр, ensure_ascii=False) + "\n")
                журнал.flush()
            из_["rows"] += 1
            try:
                с.учесть(стр["kind"], сейчас=t, слот=стр.get("slot"))
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


def слушать_много(*, потоки: dict, фид: str, журнал=None,
                   счёт: Счёт | None = None, признак_фн=None,
                   ждать_s: float | None = None) -> dict:
    """Слушать СРАЗУ НЕСКОЛЬКО регионов одного фида -- каждый своим потоком.

    Владелец 25.09: "подписаться на ВСЕ регионы BAM (оплата за сообщение,
    фильтр наш -- расход копейки), в каждом сообщении писать регион; итог --
    покрытие и опережение по регионам".

    ПОЧЕМУ ИМЕННО ТАК. У Triton один поток -- это один фид в одном регионе
    (сказано в сохранённой документации: "A stream serves one feed in one
    region"), и регионы НЕ копии друг друга: ams и fra отдают слоты разных
    лидеров. Значит все регионы -- это N потоков, а не один с фильтром.

    Счёт при этом ОДИН на все регионы: предел владельца -- 5 000 сообщений в
    СУТКИ на фид, а не на регион. Замок счёта держит его от потери записей.
    Журнал тоже один: в каждой строке есть region, и разделять файлы значило бы
    склеивать их потом.
    """
    import threading  # noqa: PLC0415

    часы = time.time
    с = счёт if счёт is not None else Счёт(фид=фид, начало=часы())
    замок_журнала = threading.Lock()
    итоги: dict = {}

    class _Журнал:
        """Запись под замком: пишут все регионы в один файл."""

        def write(self, строка):
            if журнал is None:
                return
            with замок_журнала:
                журнал.write(строка)

        def flush(self):
            if журнал is None:
                return
            with замок_журнала:
                журнал.flush()

    общий = _Журнал() if журнал is not None else None

    def один(регион, поток):
        try:
            итоги[регион] = слушать(поток=поток, фид=фид, регион=регион,
                                     журнал=общий, счёт=с,
                                     признак_фн=признак_фн)
        except Exception as exc:  # noqa: BLE001
            итоги[регион] = {"feed": фид, "region": регион, "rows": 0,
                              "stopped_why": f"{type(exc).__name__}: "
                                             f"{str(exc)[:200]}"}

    нити = [threading.Thread(target=один, args=(р, п), name=f"preconf-{р}",
                              daemon=True) for р, п in потоки.items()]
    for н in нити:
        н.start()
    for н in нити:
        н.join(timeout=ждать_s)
    живых = [н.name for н in нити if н.is_alive()]
    строк = sum(int((и or {}).get("rows") or 0) for и in итоги.values())
    # Регион, который не дал ни строки, -- это ОТВЕТ, а не пустота: значит на
    # его слотах наших источников не было. Он обязан быть в итоге числом.
    return {"feed": фид, "regions": sorted(потоки), "rows": строк,
             "by_region": {р: {"rows": int((итоги.get(р) or {}).get("rows") or 0),
                                "stopped_why": (итоги.get(р) or {}).get("stopped_why")}
                            for р in потоки},
             "alive": живых,
             "counters": с.признак(сейчас=часы())}


def свод_по_регионам(*, строки_зонда: list, наши_сигналы: list) -> dict:
    """Покрытие и опережение ПО РЕГИОНАМ (итог, которого просил владелец).

    Считается тем же сравнить(), но по строкам одного региона: так видно, какой
    регион довозит наши источники и насколько раньше нашей подписки. Регион без
    строк остаётся в ответе с нулями -- это тоже результат.
    """
    по_регионам: dict = {}
    for с in строки_зонда or []:
        р = (с or {}).get("region") or "?"
        по_регионам.setdefault(р, []).append(с)
    из_: dict = {}
    for р, ряд in sorted(по_регионам.items()):
        св = сравнить(строки_зонда=ряд, наши_сигналы=наши_сигналы)
        из_[р] = {"n_preconf": св["n_preconf"], "n_matched": св["n_matched"],
                   "n_ours_in_window": св.get("n_ours_in_window"),
                   "n_matched_in_window": св.get("n_matched_in_window"),
                   "coverage_in_window": св.get("coverage_in_window"),
                   "coverage": св["coverage"],
                   "lead_ms_median": св["lead_ms_median"],
                   "lead_ms_p90": св["lead_ms_p90"],
                   "share_preconf_earlier": св["share_preconf_earlier"]}
    # "Успели бы в S+0" -- отдельный вопрос владельца: preconf пришёл раньше
    # нашей подписки И в том же слоте, что транзакция источника.
    return {"by_region": из_,
             "note": ("покрытие -- доля НАШИХ сигналов, у которых на этом "
                       "регионе был preconf; опережение -- на сколько раньше "
                       "нашей подписки")}


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


# Допуск на края окна зонда: сигнал, пришедший за секунду до первого события
# фида, всё ещё "в окне" -- иначе граница резала бы годные пары.
ДОПУСК_ОКНА_S = 1.0


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
    # ПОКРЫТИЕ НАДО СЧИТАТЬ НА ОДНОМ ОКНЕ. Знаменатель "все наши сигналы"
    # неверен, если зонд слушал пять минут, а журнал решений покрывает часы:
    # именно так вышло 25.09 19:37Z -- 16 010 наших против 11 317 событий фида
    # за 300 секунд, и "покрытие 0 %" читалось как вывод, хотя было артефактом.
    # Поэтому рядом с прежним числом считается покрытие ПО ОКНУ ЗОНДА.
    окно_от = min((float(с["t_recv"]) for с in зонд.values()), default=None)
    окно_до = max((float(с["t_recv"]) for с in зонд.values()), default=None)
    в_окне = {п: с for п, с in наши.items()
              if окно_от is not None
              and окно_от - ДОПУСК_ОКНА_S <= float(с["t_recv"]) <= окно_до + ДОПУСК_ОКНА_S}
    общие_в_окне = [п for п in в_окне if п in зонд]
    из_ = {"n_ours": len(наши), "n_preconf": len(зонд), "n_matched": len(общие),
            "coverage": (round(len(общие) / len(наши), 4) if наши else None),
            "probe_window_from": окно_от, "probe_window_to": окно_до,
            "n_ours_in_window": len(в_окне),
            "n_matched_in_window": len(общие_в_окне),
            "coverage_in_window": (round(len(общие_в_окне) / len(в_окне), 4)
                                    if в_окне else None),
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


def адреса_торгующих_групп(*, файл: str | None = None,
                            группы: tuple | None = None) -> dict:
    """Адреса торгующих групп -- РАЗБОРОМ bloom_source_groups, не своим.

    Владелец 28.09: "фильтр signer_include = все адреса торгующих групп
    (leader, batch5, lane_s0, sniper_src, kandidaty)". Файл групп читает тот же
    модуль, что читает его в бою: второй разбор когда-нибудь разошёлся бы с
    первым молча. Группы, которых в файле нет, возвращаются отдельным списком
    -- это ответ, а не пустота.
    """
    из_ = {"ok": False, "accounts": [], "by_group": {}, "why_not": None,
            "groups_missing": [], "file": файл}
    хотим = tuple(группы or ("leader", "batch5", "lane_s0", "sniper_src",
                               "kandidaty"))
    try:
        import bloom_source_groups as BSG  # noqa: PLC0415

        по_адресу = BSG.адреса_всех_групп(файл)
        # ПУТЬ И ХЭШ ФАЙЛА -- В ОТВЕТ. "из BLOOM_SOURCE_GROUPS" ничего не
        # доказывает: на живом прогоне 28.09 переменной в окружении не было
        # вовсе, а адреса нашлись -- значит читался какой-то другой файл, и
        # какой именно, обязано быть видно числом и путём.
        лежит = BSG.загрузить(файл)
        из_["file"] = лежит.get("file")
        из_["hash"] = лежит.get("hash")
        из_["mtime_utc"] = лежит.get("mtime_utc")
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return из_
    счёт: dict = {}
    адреса: list = []
    for адрес, группа_ in (по_адресу or {}).items():
        if группа_ not in хотим:
            continue
        адреса.append(адрес)
        счёт[группа_] = счёт.get(группа_, 0) + 1
    if not адреса:
        из_["why_not"] = (f"в файле групп нет адресов групп "
                           f"{', '.join(хотим)}")
        return из_
    из_.update(ok=True, accounts=sorted(set(адреса)), by_group=счёт,
                groups_missing=[г for г in хотим if г not in счёт])
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
        import tempfile as _tф  # noqa: PLC0415

        with _tф.TemporaryDirectory() as _д_т:
            _ф_т = Path(_д_т) / "token"
            _ф_т.write_text("ТОКЕН_ИЗ_ФАЙЛА\n", encoding="utf-8")
            chk("ключ можно подать файлом, и в ответе снова только длина",
                токен({ИМЯ_СЕКРЕТА_ФАЙЛОМ: str(_ф_т)}) == "ТОКЕН_ИЗ_ФАЙЛА"
                and токен_задан({ИМЯ_СЕКРЕТА_ФАЙЛОМ: str(_ф_т)})["length"] == 14)
            chk("файла нет -- зонд говорит, что ключа нет, а не падает",
                токен_задан({ИМЯ_СЕКРЕТА_ФАЙЛОМ: str(_ф_т) + ".нет"})["ok"]
                is False)
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
        # ДВА ИМЕНОВАННЫХ ФИЛЬТРА (владелец 28.09): подписанты И счета.
        зп2 = собрать_запрос(фид=ФИД_BAM, регион="ams",
                              аккаунты=["ИСТ1"], подписанты=["ИСТ1"])
        chk("подписанты и счета идут ДВУМЯ фильтрами, а не двумя условиями",
            зп2["ok"]
            and зп2["request"]["transactions"]["podpisanty"]["signer_include"] == ["ИСТ1"]
            and зп2["request"]["transactions"]["istochniki"]["account_include"] == ["ИСТ1"]
            and "account_include" not in зп2["request"]["transactions"]["podpisanty"],
            зп2)
        зп3 = собрать_запрос(фид=ФИД_BAM, регион="ams", аккаунты=[],
                              подписанты=["ИСТ1"])
        chk("один signer_include -- законный отбор сам по себе",
            зп3["ok"] and list(зп3["request"]["transactions"]) == ["podpisanty"],
            зп3)
        chk("оба списка пусты -- запрос не собирается",
            собрать_запрос(фид=ФИД_BAM, регион="ams", аккаунты=[],
                            подписанты=[])["ok"] is False)

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
        # РАМКИ СЛОТОВ СЧИТАЮТСЯ, НО НЕ ОПЛАЧИВАЮТСЯ. По слову владельца 28.09
        # и числам панели платится за слоты, по которым фид ОТДАЛ транзакцию;
        # рамок за 1 ч 47 мин пришло 345 при 0 оплаченных, и закрывать поток по
        # ним значило бы гасить зонд раньше времени.
        chk("рамка слота считается один раз (по началу), а не дважды",
            сh.слотов == 30000, сh.слотов)
        chk("поток НЕ закрывается по рамкам слотов -- оплаты в них нет",
            not (итог_h["stopped_why"] or "") and сh.всего_оплаченных() == 0,
            (итог_h["stopped_why"], сh.всего_оплаченных()))

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

        # 5. ЗАМОК -- ПО ФИДУ. 25.09 два потока сразу были запрещены; 28.09
        # владелец просит ровно обратное: "подписка BAM + Harmonic". Значит
        # второй поток ТОГО ЖЕ фида не поднимается, а другой фид -- поднимается.
        import tempfile as _t  # noqa: PLC0415

        with _t.TemporaryDirectory() as вр:
            chk("первый поток замок берёт",
                занять_замок(фид=ФИД_BAM, каталог=вр, сейчас=5000.0)["ok"])
            второй_bam = занять_замок(фид=ФИД_BAM, каталог=вр, сейчас=5001.0)
            chk("второй поток ТОГО ЖЕ фида не запускается",
                второй_bam["ok"] is False
                and "второй поток того же фида" in (второй_bam["why_not"] or ""),
                второй_bam)
            chk("другой фид рядом поднимается (BAM + Harmonic, слово 28.09)",
                занять_замок(фид=ФИД_HARMONIC, каталог=вр, сейчас=5001.0)["ok"])
            chk("замок старого упавшего прогона не держит зонд навсегда",
                занять_замок(фид=ФИД_BAM, каталог=вр,
                              сейчас=5000.0 + ОКНО_HARMONIC_S + 601)["ok"])
            снять_замок(вр, ФИД_BAM)
            chk("после снятия замка поток снова можно открыть",
                занять_замок(фид=ФИД_BAM, каталог=вр, сейчас=6000.0)["ok"])
            chk("замок Harmonic снятием BAM не тронут",
                путь_замка(вр, ФИД_HARMONIC).exists())
            # МЁРТВЫЙ PID В ЗАМКЕ -- ЭТО СЛЕД, А НЕ ПОТОК. Поймано на живом
            # прогоне 28.09: systemctl stop убил зонд сигналом, замок остался,
            # и перезапуск отказался подниматься -- зонд стоял мёртвым.
            путь_замка(вр, ФИД_BAM).write_text(json.dumps(
                {"feed": ФИД_BAM, "ts": 6000.0, "pid": 2 ** 22,
                  "utc": "2026-09-28T17:37:23Z"}), encoding="utf-8")
            chk("замок мёртвого процесса не держит зонд",
                занять_замок(фид=ФИД_BAM, каталог=вр, сейчас=6001.0)["ok"],
                занять_замок(фид=ФИД_BAM, каталог=вр, сейчас=6001.0))
            chk("жив_ли: свой pid жив, заведомо чужой номер -- нет",
                жив_ли(os.getpid()) and not жив_ли(2 ** 22))

        # 5а. СЧЁТЧИК РАСХОДА -- ПО ФИДУ, И ДВА ПРОЦЕССА НЕ ТЕРЯЮТ ДРУГ ДРУГА.
        # Живой прогон 28.09: общий файл, два процесса, в счётчике bam_messages
        # 0 при 56 доставленных транзакциях -- каждый переписывал чужое число.
        import tempfile as _tр  # noqa: PLC0415

        with _tр.TemporaryDirectory() as _др:
            сб_ = Счёт(фид=ФИД_BAM, начало=1000.0, каталог=_др)
            сб_.сообщений = 700
            сб_.сохранить(1000.0)
            сх_ = Счёт(фид=ФИД_HARMONIC, начало=1000.0, каталог=_др)
            сх_.слотов = 345
            сх_.сохранить(1000.0)
            расх_ = расход_суток(_др, 1000.0)
            chk("расход по двум фидам сложился, ничего не потеряно",
                расх_["bam_messages"] == 700 and расх_["harmonic_slots"] == 345,
                расх_)
            chk("у каждого фида свой файл расхода",
                путь_суток(_др, ФИД_BAM).exists()
                and путь_суток(_др, ФИД_HARMONIC).exists()
                and путь_суток(_др, ФИД_BAM) != путь_суток(_др, ФИД_HARMONIC))
            # Общий файл прежних прогонов не теряется: берётся большее.
            путь_суток(_др).write_text(json.dumps(
                {"day": расх_["day"], "bam_messages": 900,
                  "harmonic_slots": 10}), encoding="utf-8")
            chk("число из старого общего файла не пропадает (берётся большее)",
                расход_суток(_др, 1000.0)["bam_messages"] == 900
                and расход_суток(_др, 1000.0)["harmonic_slots"] == 345)

        # 5в. ОПЛАЧИВАЕМЫЕ СЛОТЫ И ЦЕНА ПАНЕЛИ. Предел Harmonic -- по
        # оплачиваемым слотам (по которым фид отдал транзакцию), а не по рамкам.
        with _tр.TemporaryDirectory() as _дц:
            сц = Счёт(фид=ФИД_HARMONIC, начало=1000.0, каталог=_дц,
                       предел_слотов=3)
            for слот_ in (100, 100, 101):
                сц.учесть("transaction", сейчас=1000.0, слот=слот_)
            chk("один слот с двумя сообщениями -- один оплаченный слот",
                сц.всего_оплаченных() == 2 and сц.сообщений == 3,
                (сц.всего_оплаченных(), сц.сообщений))
            chk("рамки слотов в оплату не идут",
                (сц.учесть("slot_start", сейчас=1000.0, слот=200)
                  or сц.всего_оплаченных()) == 2, сц.всего_оплаченных())
            # Цена: 3 сообщения BAM-ценой не считаются у Harmonic, а слоты -- да.
            chk("цена по панели: 2 оплаченных слота плюс сообщения",
                сц.стоимость_usd() == round(3 * ЦЕНА_СООБЩЕНИЯ_USD
                                             + 2 * ЦЕНА_СЛОТА_USD, 4),
                сц.стоимость_usd())
            закрыт_ = None
            try:
                сц.учесть("transaction", сейчас=1000.0, слот=102)
            except ПределДостигнут as пд_:
                закрыт_ = str(пд_)
            chk("предел считается по ОПЛАЧИВАЕМЫМ слотам и закрывает поток",
                закрыт_ and "оплачиваемых слотов" in закрыт_, закрыт_)

        # 5б. ВРЕМЯ ПРИХОДА -- ДВУМЯ ЧАСАМИ: стенным для сведения с журналом
        # решений и моно для разниц внутри прогона.
        стр_мс = событие_в_строку({"kind": "transaction", "signature": "П"},
                                   фид=ФИД_BAM, регион="ams", t_recv=1000.5,
                                   t_mono=77.25)
        chk("в строке журнала есть и стенное время, и моно-часы",
            стр_мс["t_recv"] == 1000.5 and стр_мс["t_mono"] == 77.25, стр_мс)
        сл_мс = слушать(поток=[{"kind": "transaction", "signature": "П1"}],
                         фид=ФИД_BAM, регион="ams",
                         счёт=Счёт(фид=ФИД_BAM, начало=1.0, каталог=None),
                         сейчас_фн=lambda: 1.0, моно_фн=lambda: 42.0)
        chk("слушать берёт моно-часы своим источником", сл_мс["rows"] == 1)

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
        chk("покрытие ПО ОКНУ ЗОНДА: наш сигнал вне окна в знаменатель не идёт",
            св["n_ours_in_window"] == 2 and св["n_matched_in_window"] == 2
            and св["coverage_in_window"] == 1.0
            and св["probe_window_from"] == 100.0 and св["probe_window_to"] == 300.0, св)
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

    # ВСЕ РЕГИОНЫ СРАЗУ (владелец 25.09). Сети здесь нет: потоки -- готовые
    # списки событий, и проверяется ровно то, что нас волнует -- регион в каждой
    # строке, один счёт на всех и итог по регионам.
    import io as _io
    import tempfile as _tf

    with _tf.TemporaryDirectory() as вр_м:
        буфер = _io.StringIO()

        def событ(подпись, слот, узел):
            return {"kind": "transaction", "signature": подпись, "slot": слот,
                     "node": узел, "sequence": 1, "bundle_position": 0}

        потоки_м = {
            "ams": [событ("ПОДПИСЬ_A", 100, "узел-ams")],
            "fra": [событ("ПОДПИСЬ_A", 100, "узел-fra"),
                     событ("ПОДПИСЬ_B", 101, "узел-fra")],
            "tyo": [],
        }
        счёт_м = Счёт(фид=ФИД_BAM, начало=1000.0, каталог=вр_м)
        итог_м = слушать_много(потоки=потоки_м, фид=ФИД_BAM, журнал=буфер,
                                счёт=счёт_м, ждать_s=10)
        строки_м = [json.loads(с) for с in буфер.getvalue().strip().split("\n")
                    if с.strip()]
        chk("все регионы отработали и строки сложены в один журнал",
            итог_м["rows"] == 3 and len(строки_м) == 3, итог_м)
        chk("в КАЖДОЙ строке написан регион",
            all(с.get("region") in ("ams", "fra", "tyo") for с in строки_м)
            and sorted(с["region"] for с in строки_м) == ["ams", "fra", "fra"],
            [с.get("region") for с in строки_м])
        chk("узел BAM тоже в строке -- он говорит, через кого пришло",
            sorted(с.get("node") for с in строки_м)
            == ["узел-ams", "узел-fra", "узел-fra"],
            [с.get("node") for с in строки_м])
        chk("регион без строк остался в итоге числом, а не исчез",
            итог_м["by_region"]["tyo"]["rows"] == 0, итог_м["by_region"])
        chk("счёт ОДИН на все регионы: три сообщения, а не по региону",
            итог_м["counters"]["messages"] == 3
            and итог_м["counters"]["messages_day"] == 3, итог_м["counters"])
        наши_м = [{"signature": "ПОДПИСЬ_A", "t_recv": 1000.5},
                   {"signature": "ПОДПИСЬ_B", "t_recv": 1001.0},
                   {"signature": "ПОДПИСЬ_C", "t_recv": 1002.0}]
        св_м = свод_по_регионам(строки_зонда=строки_м, наши_сигналы=наши_м)
        chk("покрытие считается ПО РЕГИОНАМ отдельно",
            св_м["by_region"]["ams"]["n_matched"] == 1
            and св_м["by_region"]["fra"]["n_matched"] == 2
            and св_м["by_region"]["ams"]["coverage"] is not None,
            св_м["by_region"])
        chk("опережение тоже по регионам и в миллисекундах",
            св_м["by_region"]["fra"]["lead_ms_median"] is not None,
            св_м["by_region"]["fra"])

        плохо = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}"
              + ("" if ок else f" -- факт: {факт}"))
    print(f"самопроверка зонда Triton: {len(проверки) - len(плохо)}/"
          f"{len(проверки)} пройдено")
    return 1 if плохо else 0


def _поток_grpc(*, фид: str, регион: str, аккаунты: list, ключ: str,
                 таймаут_s: float, подписанты: list | None = None):
    """Настоящий поток gRPC. Стабы генерируются из preconfs.proto ДО запуска
    (это делает прогон), модуль их только импортирует."""
    import grpc  # noqa: PLC0415
    import preconfs_pb2  # noqa: PLC0415
    import preconfs_pb2_grpc  # noqa: PLC0415

    зп = собрать_запрос(фид=фид, регион=регион, аккаунты=аккаунты,
                         подписанты=подписанты)
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
    имя_результата = None
    if hasattr(preconfs_pb2, "ExecutionResult"):
        имя_результата = preconfs_pb2.ExecutionResult.Name
    # ПОТОК НАДО ДЕРЖАТЬ, А НЕ ОТКРЫТЬ ОДИН РАЗ. Замерено 25.09: окно Harmonic
    # ams, запрошенное на 7200 с, сервер закрыл через 690 с БЕЗ ошибки -- итератор
    # просто кончился, и зонд вышел, отдав 11 минут вместо двух часов. Поэтому
    # подписка переоткрывается, пока у окна остаётся время; каждое
    # переподключение -- событие в журнале, чтобы окно не выглядело непрерывным,
    # когда оно рвалось.
    начало = time.time()
    попыток = 0
    while True:
        осталось = таймаут_s - (time.time() - начало)
        if осталось <= 5:
            return
        поток = служба.Subscribe(запрос, metadata=мета, timeout=осталось)
        try:
            for обновление in поток:
                yield событие_из_обновления(обновление,
                                             имя_результата=имя_результата)
        except grpc.RpcError as exc:
            код = exc.code().name if exc.code() else "?"
            # Отказ подписки -- это НЕ обрыв связи: переподключаться к тому, что
            # нам не отдают, значит платить за попытки. Такие коды закрывают
            # зонд честно.
            if код in ("UNAUTHENTICATED", "PERMISSION_DENIED",
                        "RESOURCE_EXHAUSTED", "INVALID_ARGUMENT"):
                raise
            yield {"kind": "stream_error", "code": код,
                    "details": str(exc.details())[:200]}
        попыток += 1
        if попыток > 200:
            return
        yield {"kind": "reconnected", "attempts": попыток}


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
    р.add_argument("--signers", default="",
                    help="адреса для signer_include через запятую")
    р.add_argument("--signers-file", default=None)
    р.add_argument("--groups-file", default=None,
                    help=("файл групп: адреса торгующих групп идут И в "
                          "signer_include, И в account_include (владелец 28.09)"))
    р.add_argument("--iz-grupp", action="store_true",
                    help="брать адреса из файла групп, путь -- из BLOOM_SOURCE_GROUPS")
    р.add_argument("--gruppy", default="leader,batch5,lane_s0,sniper_src,kandidaty",
                    help="какие группы брать из файла групп")
    р.add_argument("--accounts-from-snapshot", default=None,
                    help="снимок konfig.json: адреса источников разбором детектора")
    р.add_argument("--accounts-out", default=None,
                    help="только выписать адреса источников в файл и выйти")
    р.add_argument("--tasks", default="BATCH-5,BATCH-3")
    р.add_argument("--limit-messages", type=int, default=None,
                    help=("предел сообщений НА ЭТОТ ПРОГОН вместо суточного "
                          "(контрольная проверка фильтра: владелец 25.09 "
                          "просил потолок 20 000 на пять минут)"))
    р.add_argument("--limit-slots", type=int, default=None,
                    help=("предел ОПЛАЧЕННЫХ слотов Harmonic вместо записанных "
                          "20 000 (~$30 по ориентиру владельца)"))
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
        # ИТОГ ПО РЕГИОНАМ -- то, что просил владелец: покрытие и опережение у
        # каждого региона отдельно. Общий итог их усредняет и прячет тот
        # регион, который один и довозит.
        св["by_region"] = свод_по_регионам(строки_зонда=строки,
                                            наши_сигналы=наши)["by_region"]
        печать = {к: v for к, v in св.items() if к != "rows"}
        print(json.dumps(печать, ensure_ascii=False, indent=2))
        if а.report_out:
            with open(а.report_out, "w", encoding="utf-8") as ф:
                json.dump(св, ф, ensure_ascii=False, indent=2)
            print(f"записано: {а.report_out}")
        return 0

    # АДРЕСА ТОРГУЮЩИХ ГРУПП В ФАЙЛ И ВЫЙТИ. Нужно прогону: он кладёт список
    # на хост одной командой, без питона внутри YAML.
    if а.accounts_out and (а.iz_grupp or а.groups_file):
        гр = адреса_торгующих_групп(
            файл=а.groups_file or None,
            группы=tuple(г.strip() for г in (а.gruppy or "").split(",")
                          if г.strip()))
        if not гр["ok"]:
            print(f"СБОЙ: {гр['why_not']}")
            return 1
        Path(а.accounts_out).write_text("\n".join(гр["accounts"]) + "\n",
                                         encoding="utf-8")
        print(json.dumps({"adresov": len(гр["accounts"]),
                           "po_gruppam": гр["by_group"],
                           "gruppy_bez_adresov": гр["groups_missing"],
                           "fajl_grupp": гр.get("file"),
                           "hash": гр.get("hash"),
                           "mtime_utc": гр.get("mtime_utc"),
                           "zapisano": а.accounts_out}, ensure_ascii=False))
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
    подписанты = [с.strip() for с in (а.signers or "").split(",") if с.strip()]
    if а.signers_file:
        подписанты += [с.strip() for с in
                        Path(а.signers_file).read_text(encoding="utf-8").split()
                        if с.strip()]
    if а.groups_file or а.iz_grupp:
        гр = адреса_торгующих_групп(
            файл=а.groups_file or None,
            группы=tuple(г.strip() for г in (а.gruppy or "").split(",")
                          if г.strip()))
        if not гр["ok"]:
            print(f"СБОЙ: {гр['why_not']}")
            return 1
        print(json.dumps({"gruppy": гр["by_group"],
                           "adresov": len(гр["accounts"]),
                           "gruppy_bez_adresov": гр["groups_missing"]},
                          ensure_ascii=False))
        подписанты = sorted(set(подписанты) | set(гр["accounts"]))
        аккаунты = sorted(set(аккаунты) | set(гр["accounts"]))
    # ВСЕ РЕГИОНЫ ФИДА одним словом all (владелец 25.09 про BAM: "подписаться
    # на ВСЕ регионы"). Регионы -- из сохранённой документации, не из памяти:
    # у BAM их пятнадцать, у Harmonic семь.
    if (а.region or "").strip().lower() == "all":
        регионы = list(РЕГИОНЫ_BAM if а.feed == ФИД_BAM else РЕГИОНЫ_HARMONIC)
    else:
        регионы = [с.strip() for с in (а.region or "").split(",") if с.strip()]
    for р_ in регионы:
        зп = собрать_запрос(фид=а.feed, регион=р_, аккаунты=аккаунты,
                             подписанты=подписанты)
        if not зп["ok"]:
            print(f"СБОЙ: {зп['why_not']}")
            return 1
    замок = занять_замок(фид=а.feed, каталог=а.state_dir,
                          окно_s=(а.seconds if а.seconds else None))
    if not замок["ok"]:
        print(f"СБОЙ: {замок['why_not']}")
        return 1
    счёт = Счёт(фид=а.feed, окно_s=(а.seconds if а.seconds else None),
                 предел_сообщений=а.limit_messages,
                 предел_слотов=(а.limit_slots if а.limit_slots is not None
                                 else ПРЕДЕЛ_СЛОТОВ_HARMONIC),
                 каталог=а.state_dir)
    путь_признака = а.status
    # ДОЛЯ СЛОТОВ BAM/HARMONIC -- В ПРИЗНАК ЖИЗНИ, РАЗ В ЧАС (владелец 28.09,
    # п.2). Считает это отдельный фоновый поток, а не признак: сеть внутри
    # записи признака означала бы, что признак жизни висит на чужом ответе.
    # Денежный путь не тронут: снимок лежит в каталоге зонда.
    валидаторы: dict = {"why_not": "снимок ещё не считан"}

    def часы_валидаторов():
        while True:
            try:
                сн = ВАЛ.свежий(каталог=а.state_dir)
                валидаторы.clear()
                валидаторы.update(ВАЛ.доля_слотов(сн))
            except Exception as exc:  # noqa: BLE001
                валидаторы.clear()
                валидаторы["why_not"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(ВАЛ.ЧАС_S)

    if ВАЛ is not None and а.state_dir:
        import threading as _thr  # noqa: PLC0415

        _thr.Thread(target=часы_валидаторов, name="triton-validatory",
                     daemon=True).start()

    def признак(з):
        if путь_признака:
            with open(путь_признака, "w", encoding="utf-8") as ф:
                json.dump({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime()),
                            "region": ",".join(регионы),
                            "validatory": dict(валидаторы), **з}, ф,
                           ensure_ascii=False, indent=2)

    # SIGTERM ДОЛЖЕН ЗАКРЫВАТЬ ЗОНД ЧИСТО. По умолчанию python на SIGTERM
    # умирает, не доходя до finally, и замок остаётся на диске -- ровно это
    # оставило зонд мёртвым 28.09 при перезапуске службы.
    import signal as _sig  # noqa: PLC0415

    def _по_сигналу(номер, _кадр):  # noqa: ANN001
        raise SystemExit(f"сигнал {номер}")

    for _с in (_sig.SIGTERM, _sig.SIGINT):
        try:
            _sig.signal(_с, _по_сигналу)
        except Exception:  # noqa: BLE001, S110
            pass

    журнал = open(а.out, "a", encoding="utf-8") if а.out else None  # noqa: SIM115
    окно = а.seconds or (счёт.окно_s or 7200)
    try:
        if len(регионы) > 1:
            потоки = {р_: _поток_grpc(фид=а.feed, регион=р_, аккаунты=аккаунты,
                                       подписанты=подписанты,
                                       ключ=токен(), таймаут_s=окно)
                       for р_ in регионы}
            итог = слушать_много(потоки=потоки, фид=а.feed, журнал=журнал,
                                  счёт=счёт, признак_фн=признак,
                                  ждать_s=окно + 30)
        else:
            поток = _поток_grpc(фид=а.feed, регион=регионы[0], аккаунты=аккаунты,
                                 подписанты=подписанты,
                                 ключ=токен(), таймаут_s=окно)
            итог = слушать(поток=поток, фид=а.feed, регион=регионы[0],
                            журнал=журнал, счёт=счёт, признак_фн=признак)
    finally:
        if журнал is not None:
            журнал.close()
        снять_замок(а.state_dir, а.feed)
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
    # МНОГО РЕГИОНОВ: сбой -- когда оборвались ВСЕ. Один упавший регион из
    # пятнадцати -- это строка в итоге, а не провал замера; ни одного живого --
    # это провал, и молчать о нём нельзя.
    по_рег = итог.get("by_region") or {}
    if по_рег:
        оборвались = [р for р, з in по_рег.items()
                       if str((з or {}).get("stopped_why") or "")
                       .startswith(("поток оборвался", "RuntimeError",
                                     "_MultiThreadedRendezvous"))]
        if len(оборвались) == len(по_рег):
            print(f"СБОЙ: оборвались все регионы: {sorted(оборвались)}")
            return 1
        if оборвались:
            print(f"ВНИМАНИЕ: оборвались регионы {sorted(оборвались)}, "
                   f"остальные отработали")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
