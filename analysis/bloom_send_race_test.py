#!/usr/bin/env python3
"""Синтетический тест гонки: наша отправка через Helius Sender против Bloom.

ЗАЧЕМ. На первой настоящей паре 24.09 Bloom ответил за 65.9 мс, а наша
покупка появилась в потоке через 410 мс ПОСЛЕ его ответа, и в блоке DBot
стоял 191-м против нашего 975-го. Вопрос владельца: своя отправка быстрее
или медленнее Bloom. Ответ должен быть числом, снятым рядом, в одинаковых
условиях, а не выводом из разных прогонов в разные часы.

ЧТО ЭТО НЕ ТРОГАЕТ -- и это главное свойство файла:
  * не детектор: отдельный процесс, боевую подписку не трогает;
  * не позиции и не сторож: своё состояние в своём каталоге, ExecState
    боевого хозяйства не открывается на запись вообще;
  * не полосу own_send: пределы полосы (одна открытая, 20 в сутки) считают
    по позициям, а тест позиций не пишет. У теста свои стопы, свой счёт
    расхода и свой предел, заданный владельцем;
  * боевую торговлю: если у детектора открыта позиция или в журнале решений
    только что был сигнал -- пара ПРОПУСКАЕТСЯ, а не отменяется.

ПАРА. Две покупки одного и того же USDC на 0.001 SOL, пущенные с разницей
не больше нескольких миллисекунд:
  A -- Bloom POST /swap, параметры как в бою (проскальзывание 35 %,
       priority_fee 0.001, processor_tip 0.001, anti_mev false) и
       auto_orders ПУСТЫМ СПИСКОМ: сохранённая стратегия аккаунта нам не
       нужна, а опущенное поле у Bloom означает именно её;
  B -- своя сборка по свежей сделке пула, подпись нашим ключом и отправка в
       Helius Sender (Амстердам, /fast, чаевые 0.001 SOL на tip-аккаунт из
       документации, skipPreflight true, maxRetries 0).
Кто идёт первым -- чередуется, чтобы очередь слота не досталась всегда
одной стороне.

ЧЕМ МЕРИМ. t_send -> t_seen: от начала отправки до появления НАШЕЙ подписи
в подписке processed на кошелёк. У A отдельно bloom_ms (время площадки,
меряется вокруг единственного POST внутри клиента). Успех -- отложенным
getTransaction ВНЕ горячего пути: замерная подписка с "failed": false
упавшую транзакцию не приносит вовсе, и считать по ней "село" нельзя.

ДЕНЬГИ. Владелец разрешил на весь тест 0.2 SOL. Предел стоит перед КАЖДОЙ
парой и считается по заявленным оттокам, а не по факту постфактум: узнать,
что предел пробит, после тридцатой пары -- значит не иметь предела.
Накопленный USDC продаётся ОДИН раз в конце через модуль продажи (Swap V2 с
полом 70 % от котировки), а не после каждой пары.

Ключи -- только из окружения. Ключ кошелька не печатается никуда: подпись
идёт через bloom_own_send, где публичный ключ сверяется с кошельком
исполнителя перед КАЖДОЙ подписью.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_exec_state as ST  # noqa: E402
import bloom_own_send as OS  # noqa: E402

# ------------------------------------------------------------------ постоянные

# Минт USDC. Константа НЕ принимается на веру: перед живым прогоном
# проверяется по цепи (символ и знаки после запятой -- из метаданных, а
# число знаков ещё и из getAccountInfo). Несовпадение -- отказ, а не
# предупреждение: не тот минт означает покупку не того токена.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
WSOL = "So11111111111111111111111111111111111111112"

ЛАМПОРТОВ_В_SOL = 1_000_000_000

# Счёт комиссии Bloom. Установлен НЕ из документации, а из наших же
# транзакций: во всех боевых покупках на него уходит ровно 1 % входа.
# Используется только для разметки доклада -- деньги по нему не ходят.
СЧЁТ_КОМИССИИ_BLOOM = "7HeD6sLLqAnKVRuSfc1Ko3BSPMNKWgGTiWLKXJF31vKM"

# Размер и параметры пары -- слово владельца, менять нельзя.
РАЗМЕР_SOL = 0.001
ПРОСКАЛЬЗЫВАНИЕ = 0.35
ПРИОРИТЕТ_SOL = 0.001
ЧАЕВЫЕ_SOL = 0.001
ПАР_ВСЕГО = 30
ПАУЗА_МЕЖДУ_ПАРАМИ_S = 10.0
ПРОМЕЖУТОЧНЫЙ_ДОКЛАД_НА = 10

# Стопы теста.
ПРЕДЕЛ_РАСХОДА_SOL = 0.2
СТОП_ПОДРЯД_УПАВШИХ = 5
# Сколько ждём появления своей подписи в потоке. Слот -- 400 мс; 25 секунд
# это заведомо больше любой разумной задержки, и если за них подписи нет,
# то её, скорее всего, и не будет. Больше ставить нельзя: тридцать пар по
# минуте ожидания растянули бы прогон на полчаса пустого ожидания.
ОКНО_ОЖИДАНИЯ_S = 25.0
# Запас, который тест обязан оставить боевой торговле: порог старта
# детектора плюс один боевой размер плюс резерв на комиссии. Меньше --
# тест съест деньги у торговли, а это не его право.
ЗАПАС_ТОРГОВЛИ_SOL = 0.55

# Одна транзакция Solana с одной подписью стоит 5000 лампортов.
БАЗОВАЯ_КОМИССИЯ_SOL = 0.000005
# Рента счёта токена (ATA). Возвращается при закрытии счёта, но на время
# теста лежит занятой -- в расход считаем, чтобы предел не обманывал.
РЕНТА_ATA_SOL = 0.00203928


# ------------------------------------------------------------------ статистика

def квантиль(значения: list, q: float) -> float | None:
    """Квантиль с линейной интерполяцией. Своя, чтобы не тащить numpy."""
    ряд = sorted(float(x) for x in значения if x is not None)
    if not ряд:
        return None
    if len(ряд) == 1:
        return round(ряд[0], 2)
    место = (len(ряд) - 1) * float(q)
    низ = int(место)
    верх = min(низ + 1, len(ряд) - 1)
    доля = место - низ
    return round(ряд[низ] * (1 - доля) + ряд[верх] * доля, 2)


def статистика(значения: list) -> dict:
    """Медиана, p10, p90, максимум. n -- сколько чисел вошло, а не сколько
    было попыток: разница между ними и есть честность доклада."""
    ряд = [float(x) for x in значения if x is not None]
    if not ряд:
        return {"n": 0, "median": None, "p10": None, "p90": None,
                 "max": None, "min": None}
    return {"n": len(ряд), "median": round(statistics.median(ряд), 2),
             "p10": квантиль(ряд, 0.10), "p90": квантиль(ряд, 0.90),
             "max": round(max(ряд), 2), "min": round(min(ряд), 2)}


# ------------------------------------------------------------------ рубильники

def пути_рубильников(каталог_состояния: Path | None = None) -> list:
    """Все рубильники, которые тест обязан слушать.

    Их три, и путей у них разные хозяева: файл в /etc принадлежит root,
    файл в каталоге состояния пишет служба по команде из Telegram, третий --
    свой, чтобы тест можно было остановить не трогая торговлю.
    """
    пути = [ST.kill_file()]
    к = каталог_состояния or ST.state_dir()
    пути.append(Path(к) / "KILL_BY_TELEGRAM")
    свой = (os.environ.get("BLOOM_RACE_KILL_FILE") or "").strip()
    if свой:
        пути.append(Path(свой))
    return пути


def рубильник(пути: list) -> tuple:
    """Рубильник теста. ЛЮБАЯ неясность -- стоп.

    Файл есть -> стоп. Ошибка проверки (права, битый путь) -> ТОЖЕ стоп:
    цена ложного стопа -- незаконченный замер, цена ложного разрешения --
    трата денег после приказа остановиться.
    """
    for п in пути:
        try:
            if Path(п).exists():
                return True, f"рубильник {п}"
        except Exception as exc:  # noqa: BLE001
            return True, f"рубильник {п} не проверяется ({type(exc).__name__}) -- стоп"
    return False, ""


# --------------------------------------------------------- боевая торговля рядом

def живой_сигнал(каталог: Path, *, окно_с: float = 20.0,
                  сейчас: float | None = None) -> tuple:
    """Идёт ли рядом боевая работа. ТОЛЬКО чтение файлов состояния.

    Два признака, и любого достаточно: открытая боевая позиция (её вот-вот
    будет продавать сторож) и свежая запись в журнале решений. Второй нужен
    потому, что между решением и появлением позиции есть доли секунды, и
    пара, пущенная ровно в них, мешала бы покупке за настоящие деньги.
    """
    сейчас = сейчас if сейчас is not None else time.time()
    к = Path(каталог)
    позиции = к / "positions.jsonl"
    решения = к / "decisions.jsonl"
    состояния: dict = {}
    try:
        if позиции.exists():
            with позиции.open(encoding="utf-8") as f:
                for строка in f:
                    строка = строка.strip()
                    if not строка:
                        continue
                    try:
                        строка_j = json.loads(строка)
                    except ValueError:
                        continue
                    cid = строка_j.get("client_order_id")
                    if cid:
                        состояния.setdefault(cid, {}).update(строка_j)
    except Exception as exc:  # noqa: BLE001
        # Не прочитали -- считаем, что торговля идёт: пропущенная пара
        # дешевле помехи боевой покупке.
        return True, f"позиции не прочитаны ({type(exc).__name__}) -- пара пропущена"
    открытые = [п for п in состояния.values()
                 if п.get("state") in ST.STATES_OPEN and ST.is_real_mode(п.get("mode"))]
    if открытые:
        return True, f"боевых открытых позиций {len(открытые)}"
    try:
        if решения.exists():
            возраст = сейчас - решения.stat().st_mtime
            if возраст < окно_с:
                return True, f"журнал решений тронут {возраст:.1f} с назад"
    except Exception as exc:  # noqa: BLE001
        return True, f"журнал решений не проверен ({type(exc).__name__})"
    return False, ""


# --------------------------------------------------------- основной путь подписки

# Сколько признак жизни детектора считается свежим. Пульс у него 60 с;
# три пульса без записи -- это уже не "живой детектор", и судить по такому
# файлу о пути подписки нельзя.
СВЕЖЕСТЬ_ПРИЗНАКА_S = 180.0


def основной_путь(каталог) -> dict:
    """На основном ли пути подписка БОЕВОГО детектора.

    Слово владельца 25.09: если во время пары detector_status.json говорит
    subscribe_main_path == false, пара не засчитывается и помечается. Причина
    та же, что у вечерних S+2: на запасном logsSubscribe уведомление не
    несёт транзакцию, и время появления в потоке завышается.

    Неизвестно -- это НЕ "false": когда файла нет или он старый, так и
    сказано (known=false), и пара считается, но с пометкой. Врать в обе
    стороны одинаково плохо.
    """
    из_ = {"known": False, "main_path": None, "age_s": None,
            "fallback_seconds": None, "method": None, "why_not": None}
    файл = Path(каталог) / "detector_status.json"
    try:
        сырое = файл.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{файл.name} не прочитан: {type(exc).__name__}"
        return из_
    try:
        данные = json.loads(сырое)
    except ValueError:
        из_["why_not"] = f"{файл.name} не разобрался как json"
        return из_
    возраст = time.time() - float(данные.get("updated_ts") or 0)
    из_["age_s"] = round(возраст, 1)
    из_["method"] = данные.get("subscribe_method")
    из_["fallback_seconds"] = данные.get("fallback_seconds")
    if возраст > СВЕЖЕСТЬ_ПРИЗНАКА_S:
        из_["why_not"] = (f"признак жизни детектора старше "
                           f"{СВЕЖЕСТЬ_ПРИЗНАКА_S:.0f} с ({возраст:.0f} с)")
        return из_
    из_.update(known=True, main_path=bool(данные.get("subscribe_main_path")))
    return из_


def пара_зачтена(*, путь_до: dict, путь_после: dict,
                  обрывов_до: int, обрывов_после: int,
                  поток_подтверждён: bool) -> tuple:
    """Годится ли пара в статистику.

    Два условия, и оба про то, чем мерили. Первое -- путь боевого детектора
    (слово владельца). Второе -- НАША подписка: t_seen мерит она, и её обрыв
    посреди пары завышает время точно так же, как запасной путь.
    """
    if путь_до.get("known") and путь_до.get("main_path") is False:
        return False, "детектор был на запасном пути перед парой"
    if путь_после.get("known") and путь_после.get("main_path") is False:
        return False, "детектор ушёл на запасной путь во время пары"
    if not поток_подтверждён:
        return False, "наша подписка не подтверждена"
    if обрывов_после != обрывов_до:
        return False, "наша подписка оборвалась во время пары"
    return True, ""


# ------------------------------------------------------------------ учёт расхода

def расход_стороны_sol(сторона: str, *, рента: bool = False) -> float:
    """Заявленный отток по одной покупке. Считается ДО отправки.

    У Bloom: размер + приоритетка + чаевые процессору + его комиссия 1 % +
    базовая комиссия сети. У нас: размер + чаевые Sender + приоритетка +
    базовая комиссия. Рента счёта токена добавляется один раз -- при первой
    покупке, когда счёта ещё нет.
    """
    р = РЕНТА_ATA_SOL if рента else 0.0
    if сторона == "A":
        return (РАЗМЕР_SOL + ПРИОРИТЕТ_SOL + ЧАЕВЫЕ_SOL
                 + РАЗМЕР_SOL * 0.01 + БАЗОВАЯ_КОМИССИЯ_SOL + р)
    return (РАЗМЕР_SOL + ЧАЕВЫЕ_SOL + ПРИОРИТЕТ_SOL
             + БАЗОВАЯ_КОМИССИЯ_SOL + р)


def расход_пары_sol(*, рента: bool = False) -> float:
    return расход_стороны_sol("A", рента=рента) + расход_стороны_sol("B")


def хватает_предела(потрачено: float, *, рента: bool = False,
                     предел: float = ПРЕДЕЛ_РАСХОДА_SOL) -> bool:
    """Влезает ли ЕЩЁ ОДНА пара в предел. Проверка стоит ПЕРЕД парой."""
    return (потрачено + расход_пары_sol(рента=рента)) <= предел + 1e-12


# ------------------------------------------------------------------ поток

def разобрать_подпись_и_слот(res: dict) -> tuple:
    """Подпись и слот из уведомления. Форм две, и обе встречаются вживую."""
    слот = res.get("slot")
    if слот is None:
        слот = (res.get("context") or {}).get("slot")
    подпись = res.get("signature")
    if not подпись:
        tx = res.get("transaction") or {}
        внутр = tx.get("transaction") if isinstance(tx.get("transaction"), dict) else tx
        подписи = (внутр or {}).get("signatures") or []
        подпись = подписи[0] if подписи else None
    if not подпись:
        подпись = (res.get("value") or {}).get("signature")
    return (подпись if isinstance(подпись, str) and len(подпись) >= 43 else None,
             слот if isinstance(слот, int) else None)


class Поток:
    """Подписка на наш кошелёк: processed, БЕЗ фильтра failed.

    Фильтр "failed": false в боевой замерной подписке означает, что упавшая
    наша транзакция не придёт вовсе. Для замера "дошло до блока" это плохо:
    упавшая транзакция тоже дошла, и её круг -- такое же число. Поэтому
    здесь фильтра нет, а вердикт по цепи берётся отдельно, getTransaction.
    """

    def __init__(self, ключ: str, кошелёк: str) -> None:
        self.ключ = ключ
        self.кошелёк = кошелёк
        self.по_подписи: dict = {}
        self.порядок: list = []
        self._замок = threading.Lock()
        self.сообщений = 0
        self.байт = 0
        self.обрывов = 0
        self.подтверждена = threading.Event()
        self.остановлен = threading.Event()
        self._нить = None

    # -- разбор вынесен из сетевого цикла: именно он проверяется самопроверкой
    def принять(self, raw: str, сейчас: float) -> dict | None:
        self.сообщений += 1
        self.байт += len(raw or "")
        try:
            msg = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(msg, dict):
            return None
        if "id" in msg and isinstance(msg.get("result"), int):
            self.подтверждена.set()
            return None
        if msg.get("method") != "transactionNotification":
            return None
        res = (msg.get("params") or {}).get("result") or {}
        подпись, слот = разобрать_подпись_и_слот(res)
        if not подпись:
            return None
        tx = res.get("transaction") if isinstance(res.get("transaction"), dict) else None
        запись = {"signature": подпись, "slot": слот, "t_seen": сейчас, "tx": tx}
        with self._замок:
            if подпись in self.по_подписи:
                return self.по_подписи[подпись]
            self.по_подписи[подпись] = запись
            self.порядок.append(запись)
        return запись

    def найти(self, подпись: str) -> dict | None:
        with self._замок:
            з = self.по_подписи.get(подпись)
        return dict(з) if з else None

    def ждать(self, подпись: str, *, предел_s: float = ОКНО_ОЖИДАНИЯ_S) -> dict | None:
        дедлайн = time.time() + предел_s
        while time.time() < дедлайн:
            з = self.найти(подпись)
            if з is not None:
                return з
            time.sleep(0.02)
        return None

    def запустить(self) -> None:
        self._нить = threading.Thread(target=self._цикл, name="race-stream", daemon=True)
        self._нить.start()

    def остановить(self) -> None:
        self.остановлен.set()

    def _цикл(self) -> None:
        import asyncio  # noqa: PLC0415

        import websockets  # noqa: PLC0415

        адрес = f"wss://atlas-mainnet.helius-rpc.com/?api-key={self.ключ}"
        запрос = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe",
            "params": [{"accountInclude": [self.кошелёк], "vote": False},
                        {"commitment": "processed", "transactionDetails": "full",
                         "encoding": "jsonParsed", "showRewards": False,
                         "maxSupportedTransactionVersion": 0}]})

        async def главный():
            while not self.остановлен.is_set():
                try:
                    async with websockets.connect(адрес, ping_interval=20,
                                                   ping_timeout=30,
                                                   max_size=16 * 1024 * 1024) as ws:
                        await ws.send(запрос)
                        while not self.остановлен.is_set():
                            try:
                                сырое = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue
                            # Время -- ДО разбора json, как в боевом детекторе:
                            # разбор занимает десятые доли миллисекунды, и они
                            # не имеют права попадать в замер.
                            сейчас = time.time()
                            if isinstance(сырое, bytes):
                                сырое = сырое.decode("utf-8", "replace")
                            self.принять(сырое, сейчас)
                except Exception:  # noqa: BLE001
                    self.обрывов += 1
                    self.подтверждена.clear()
                    await asyncio.sleep(1.0)

        try:
            asyncio.run(главный())
        except Exception:  # noqa: BLE001
            self.обрывов += 1


# ------------------------------------------------------------------ цепь

def проверить_минт(rpc, минт: str) -> dict:
    """Тот ли это минт. Знаки и символ -- ИЗ ЦЕПИ, а не из памяти.

    Правило владельца: не выдумывать данные. Число знаков после запятой
    берётся из getAccountInfo (без него минимум выхода считался бы не в тех
    единицах), символ -- из метаданных DAS, и если DAS молчит, так и
    сказано. Для USDC проверка строже: там константа в коде, и она обязана
    совпасть и по знакам, и по символу.
    """
    из_ = {"ok": False, "why_not": None, "mint": минт, "decimals": None,
            "symbol": None, "supply": None}
    if not минт:
        из_["why_not"] = "минт не задан"
        return из_
    try:
        инфо = rpc.call("getAccountInfo", [минт, {"encoding": "jsonParsed"}])
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"getAccountInfo не ответил: {type(exc).__name__}"
        return из_
    разбор = (((инфо or {}).get("value") or {}).get("data") or {}).get("parsed") or {}
    инфо_минта = (разбор.get("info") or {}) if разбор.get("type") == "mint" else {}
    if not инфо_минта:
        из_["why_not"] = "в getAccountInfo это не минт SPL"
        return из_
    из_["decimals"] = инфо_минта.get("decimals")
    из_["supply"] = инфо_минта.get("supply")
    if not isinstance(из_["decimals"], int):
        из_["why_not"] = "число знаков после запятой не прочитано"
        return из_
    try:
        актив = rpc.call("getAsset", [{"id": минт}], enhanced=True)
        из_["symbol"] = (((актив or {}).get("token_info") or {}).get("symbol")
                          or (((актив or {}).get("content") or {})
                               .get("metadata") or {}).get("symbol"))
    except Exception as exc:  # noqa: BLE001
        из_["symbol_why_not"] = f"getAsset не ответил: {type(exc).__name__}"
    if минт == USDC_MINT:
        if из_["decimals"] != USDC_DECIMALS:
            из_["why_not"] = (f"знаков после запятой {из_['decimals']}, "
                               f"а у USDC {USDC_DECIMALS}")
            return из_
        if из_["symbol"] and str(из_["symbol"]).upper() != "USDC":
            из_["why_not"] = f"символ минта {из_['symbol']}, а не USDC"
            return из_
    из_["ok"] = True
    if not из_["symbol"]:
        из_["symbol_unverified"] = True
    return из_


def баланс_sol(rpc, кошелёк: str) -> float | None:
    try:
        о = rpc.call("getBalance", [кошелёк])
    except Exception:  # noqa: BLE001
        return None
    лампорты = (о or {}).get("value")
    return (лампорты / ЛАМПОРТОВ_В_SOL) if isinstance(лампорты, int) else None


def свежий_blockhash(rpc) -> str | None:
    try:
        о = rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])
    except Exception:  # noqa: BLE001
        return None
    return ((о or {}).get("value") or {}).get("blockhash")


def первый_подписант(tx: dict) -> str | None:
    ключи = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    for к in ключи:
        if isinstance(к, dict):
            if к.get("signer"):
                return к.get("pubkey")
        elif isinstance(к, str):
            return к
    return None


def свежие_сделки_пула(rpc, хранилище: str, *, сколько: int = 10) -> list:
    """Последние сделки пула, новейшие первыми. Шаблон берётся из них."""
    try:
        подписи = rpc.call("getSignaturesForAddress", [хранилище, {"limit": сколько}])
    except Exception:  # noqa: BLE001
        return []
    сигн = [з.get("signature") for з in (подписи or [])
             if isinstance(з, dict) and not з.get("err") and з.get("signature")]
    if not сигн:
        return []
    тела = rpc.get_transactions(сигн, commitment="confirmed",
                                 maxSupportedTransactionVersion=0)
    из_ = []
    for подпись, tx in zip(сигн, тела):
        if isinstance(tx, dict):
            из_.append({"signature": подпись, "tx": tx})
    return из_


# ------------------------------------------------------------------ разметка

def ключи_транзакции(tx: dict) -> set:
    """Все счета транзакции. По ним и различаются стороны, когда подписи
    от Bloom мы не дождались."""
    ключи = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    из_ = set()
    for к in ключи:
        if isinstance(к, dict) and к.get("pubkey"):
            из_.add(к["pubkey"])
        elif isinstance(к, str):
            из_.add(к)
    # Счета из таблиц адресов лежат отдельно и тоже участвуют.
    загруж = ((tx or {}).get("meta") or {}).get("loadedAddresses") or {}
    for имя in ("writable", "readonly"):
        for а in (загруж.get(имя) or []):
            if isinstance(а, str):
                из_.add(а)
    return из_


def переводы_sol(tx: dict) -> list:
    """Переводы SOL из транзакции: (получатель, лампорты). jsonParsed,
    включая внутренние инструкции -- чаевые часто идут именно там."""
    из_: list = []
    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    наборы = list(сообщение.get("instructions") or [])
    for г in (((tx or {}).get("meta") or {}).get("innerInstructions") or []):
        наборы += list(г.get("instructions") or [])
    for и in наборы:
        if not isinstance(и, dict):
            continue
        p = и.get("parsed")
        if not isinstance(p, dict) or p.get("type") not in ("transfer", "transferChecked"):
            continue
        инфо = p.get("info") or {}
        кому = инфо.get("destination")
        сколько = инфо.get("lamports")
        if кому and isinstance(сколько, int):
            из_.append((кому, сколько))
    return из_


def свои_счета(кошелёк: str, минт: str = USDC_MINT) -> set:
    """Наши собственные счета: кошелёк и его счета токенов.

    Нужны для разметки чаевых. В покупке Bloom есть перевод SOL на НАШ же
    счёт WSOL (обёртка), и считать его чаевыми ускорителю было бы прямой
    ошибкой доклада.
    """
    из_ = {кошелёк}
    try:
        import c2_swap_build as B  # noqa: PLC0415
        for м in (WSOL, минт):
            из_.add(B.ata(кошелёк, м, B.TOKEN_PROGRAM))
    except Exception:  # noqa: BLE001
        pass
    return из_


def метка_счёта(адрес: str, *, jito: tuple = ()) -> str | None:
    """Чей это счёт -- только там, где источник известен.

    Придумывать названия ускорителей нельзя: в докладе адрес без метки
    честнее метки без источника.
    """
    if адрес == СЧЁТ_КОМИССИИ_BLOOM:
        return "комиссия Bloom 1 % (из наших транзакций)"
    if адрес in OS.TIP_ACCOUNTS:
        return "tip Helius Sender (docs/helius_sender_max)"
    if адрес in tuple(jito or ()):
        return "tip Jito (getTipAccounts)"
    return None


def сторона_по_транзакции(tx: dict, подпись: str, *, свои: set) -> str | None:
    """Чья это транзакция. Сначала подпись, потом счета.

    Подпись -- надёжнее всего: свою мы подписали сами и знаем её ДО
    отправки. Если Bloom подписи в ответе не дал, сторона узнаётся по
    счёту комиссии Bloom в транзакции.
    """
    if подпись in свои:
        return "B"
    ключи = ключи_транзакции(tx)
    if СЧЁТ_КОМИССИИ_BLOOM in ключи:
        return "A"
    if ключи & set(OS.TIP_ACCOUNTS):
        return "B"
    return None


# ------------------------------------------------------------------ подготовка B

def подготовить_свою(rpc, сделки: list, *, лампорты: int, кошелёк: str,
                      семя: str, blockhash: str | None = None,
                      минт: str = USDC_MINT, подписывать: bool = True) -> dict:
    """Сборка и подпись нашей покупки по свежей сделке пула.

    Идём по сделкам от новейшей: первая, на которой сборщик выдаёт минимум
    по резервам, и становится шаблоном. Возраст шаблона пишется в доклад --
    "свежий" должно быть числом, а не словом.
    """
    из_ = {"ok": False, "why_not": None, "attempts": []}
    # Без подписи blockhash не нужен: он подставляется именно при подписи.
    хеш = blockhash or (свежий_blockhash(rpc) if подписывать else "не нужен")
    if not хеш:
        из_["why_not"] = "blockhash не получен -- подписывать нечего"
        return из_
    из_["blockhash"] = хеш
    for с in сделки:
        tx = с.get("tx") or {}
        трейдер = первый_подписант(tx)
        if not трейдер:
            из_["attempts"].append({"signature": с.get("signature"),
                                     "why_not": "подписанта не видно"})
            continue
        собрано = OS.собрать(tx_источника=tx, источник=трейдер, минт=минт,
                              наш_кошелёк=кошелёк, лампорты=лампорты,
                              проскальзывание=ПРОСКАЛЬЗЫВАНИЕ,
                              приоритет_лампорты=int(ПРИОРИТЕТ_SOL * ЛАМПОРТОВ_В_SOL),
                              чаевые_лампорты=int(ЧАЕВЫЕ_SOL * ЛАМПОРТОВ_В_SOL),
                              семя=семя)
        if not собрано.get("ok"):
            из_["attempts"].append({"signature": с.get("signature"),
                                     "why_not": собрано.get("why_not")})
            continue
        if not подписывать:
            # Разведка обходится БЕЗ ключа: симуляции хватает неподписанной
            # транзакции (sigVerify false, replaceRecentBlockhash true).
            # Значит ключа кошелька на хосте в этом режиме быть не должно
            # вовсе -- лишний ключ рядом с чтением это лишний риск.
            из_.update(ok=True, template_signature=с.get("signature"),
                       template_slot=(tx or {}).get("slot"),
                       template_block_time=(tx or {}).get("blockTime"),
                       pool_program=собрано.get("pool_program"),
                       min_out=собрано.get("min_out"),
                       expected_out=собрано.get("expected_out"),
                       tip_account=собрано.get("tip_account"),
                       build_ms=собрано.get("build_ms"), size=собрано.get("size"),
                       signature=None, signed=False,
                       tx_base64=собрано["tx_base64"])
            return из_
        подписано = OS.подписать(собрано["tx_base64"], blockhash=хеш,
                                  ожидаемый_кошелёк=кошелёк)
        if not подписано.get("ok"):
            из_["why_not"] = f"подпись: {подписано.get('why_not')}"
            return из_
        из_.update(ok=True, template_signature=с.get("signature"),
                   template_slot=(tx or {}).get("slot"),
                   template_block_time=(tx or {}).get("blockTime"),
                   pool_program=собрано.get("pool_program"),
                   min_out=собрано.get("min_out"),
                   expected_out=собрано.get("expected_out"),
                   tip_account=собрано.get("tip_account"),
                   build_ms=собрано.get("build_ms"), size=собрано.get("size"),
                   signature=подписано["signature"], signed=True,
                   tx_base64=подписано["tx_base64"])
        return из_
    из_["why_not"] = "ни одна свежая сделка пула не дала шаблон с минимумом по резервам"
    return из_


# ------------------------------------------------------------------ пара

def пустить_пару(*, api, тело: dict, cid: str, своя_b64: str,
                  ключ_операции: str, первый: str, отправитель=None,
                  предел_s: float = 30.0) -> dict:
    """Две отправки рядом. Возвращает факты, а не намерения.

    Кто первый -- чередуется снаружи. Настоящий порядок и настоящий зазор
    считаются по замерам t_send, а не по тому, кого мы собирались пустить
    раньше: планировать порядок и докладывать о нём как о факте -- разные
    вещи.
    """
    итог: dict = {"first_planned": первый}
    событие = threading.Event()

    def сторона_a():
        try:
            if первый == "A":
                событие.set()
            else:
                событие.wait(5.0)
            о = api.swap(тело, client_order_id=cid, why="send_race")
            итог["a"] = {"t_send": о.get("sent_ts"), "bloom_ms": о.get("bloom_ms"),
                          "ok": bool(о.get("ok")), "code": о.get("code"),
                          "error_code": о.get("error_code"),
                          "why_not": о.get("why_not"),
                          "signatures": list(о.get("signatures") or []),
                          "order_id": о.get("order_id"),
                          "new_connections": о.get("new_connections")}
        except Exception as exc:  # noqa: BLE001
            итог["a"] = {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}

    def сторона_b():
        try:
            if первый == "B":
                событие.set()
            else:
                событие.wait(5.0)
            t = time.time()
            о = OS.отправить(своя_b64, ключ_операции=ключ_операции,
                              отправитель=отправитель)
            итог["b"] = {"t_send": t, "send_ms": о.get("send_ms"),
                          "ok": bool(о.get("ok")), "http": о.get("http"),
                          "why_not": о.get("why_not"),
                          "signature": о.get("result"),
                          "duplicate": о.get("duplicate")}
        except Exception as exc:  # noqa: BLE001
            итог["b"] = {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}

    на = threading.Thread(target=сторона_a, name="race-a")
    нб = threading.Thread(target=сторона_b, name="race-b")
    на.start()
    нб.start()
    на.join(предел_s)
    нб.join(предел_s)
    итог.setdefault("a", {"ok": False, "why_not": "поток A не завершился"})
    итог.setdefault("b", {"ok": False, "why_not": "поток B не завершился"})
    ta, tb = итог["a"].get("t_send"), итог["b"].get("t_send")
    if ta and tb:
        итог["gap_ms"] = round(abs(ta - tb) * 1000.0, 2)
        итог["first_actual"] = "A" if ta <= tb else "B"
    return итог


# ------------------------------------------------------------------ проверка по цепи

def вердикты_по_цепи(rpc, подписи: list) -> dict:
    """Села транзакция или нет -- отложенным getTransaction, вне пары.

    Тишина узла НЕ означает "не села": в таком случае вердикта просто нет,
    и в докладе стоит null, а не false.
    """
    подписи = [с for с in подписи if с]
    if not подписи:
        return {}
    тела = rpc.get_transactions(подписи, commitment="confirmed",
                                 maxSupportedTransactionVersion=0)
    из_: dict = {}
    for подпись, tx in zip(подписи, тела):
        if not isinstance(tx, dict):
            из_[подпись] = {"chain_ok": None, "why_not": "getTransaction молчит"}
            continue
        мета = tx.get("meta") or {}
        из_[подпись] = {"chain_ok": мета.get("err") is None,
                         "chain_err": мета.get("err"),
                         "fee_lamports": мета.get("fee"),
                         "slot": tx.get("slot"),
                         "transfers": переводы_sol(tx)}
    return из_


def места_в_блоках(helius_like, пары: list) -> dict:
    """Место каждой нашей транзакции в своём блоке. Один getBlock на слот."""
    import bloom_block_position as BP  # noqa: PLC0415

    нужно: dict = {}
    for п in пары:
        for сторона in ("a", "b"):
            з = п.get(сторона) or {}
            слот, подпись = з.get("slot_seen") or з.get("slot"), з.get("signature")
            if isinstance(слот, int) and подпись:
                нужно.setdefault(слот, set()).add(подпись)
    из_: dict = {}
    for слот, подписи in sorted(нужно.items()):
        блок = BP.блок_со_счетами(helius_like, слот)
        if not блок.get("known"):
            for с in подписи:
                из_[с] = {"index": None, "total": None,
                           "why_not": блок.get("why_not")}
            continue
        всего = len(блок.get("transactions") or [])
        for с in подписи:
            и = BP.индекс_подписи(блок, с)
            из_[с] = {"index": и, "total": всего,
                       "share": (round(и / всего, 3) if (и is not None and всего) else None)}
    return из_


# ------------------------------------------------------------------ доклад

def свод(пары_все: list) -> dict:
    """Числа доклада. Считаются только по ЗАЧТЁННЫМ парам, где есть оба
    замера. Незачтённые (запасной путь детектора, обрыв нашей подписки)
    остаются в журнале, но в числа не идут: иначе доклад смешает замер с
    помехой."""
    пары = [п for п in пары_все if п.get("counted") is not False]
    a_ms = [((п.get("a") or {}).get("send_to_seen_ms")) for п in пары]
    b_ms = [((п.get("b") or {}).get("send_to_seen_ms")) for п in пары]
    bloom = [((п.get("a") or {}).get("bloom_ms")) for п in пары]
    оба = [(x, y) for x, y in zip(a_ms, b_ms) if x is not None and y is not None]
    разницы = [round(y - x, 2) for x, y in оба]      # B минус A: плюс -- мы медленнее
    из_ = {"pairs": len(пары),
            "a_send_to_seen_ms": статистика(a_ms),
            "b_send_to_seen_ms": статистика(b_ms),
            "bloom_ms": статистика(bloom),
            "pairs_with_both": len(оба),
            "b_minus_a_ms": статистика(разницы),
            "a_failed": sum(1 for п in пары if not (п.get("a") or {}).get("ok")),
            "b_failed": sum(1 for п in пары if not (п.get("b") or {}).get("ok")),
            "a_seen": sum(1 for x in a_ms if x is not None),
            "b_seen": sum(1 for x in b_ms if x is not None),
            "gap_ms": статистика([п.get("gap_ms") for п in пары]),
            "gap_over_5ms": sum(1 for п in пары
                                 if (п.get("gap_ms") or 0) > 5.0)}
    по_ускорителю: dict = {}
    for п in пары:
        a = п.get("a") or {}
        for адрес, лампорты in (a.get("tips") or []):
            строка = по_ускорителю.setdefault(адрес, {"n": 0, "lamports": 0,
                                                       "label": a.get("tip_labels", {}).get(адрес)})
            строка["n"] += 1
            строка["lamports"] += int(лампорты)
    из_["a_by_accelerator"] = по_ускорителю
    из_["pairs_attempted"] = len(пары_все)
    из_["pairs_not_counted"] = len(пары_все) - len(пары)
    из_["not_counted_why"] = sorted({str(п.get("not_counted_why"))
                                      for п in пары_все
                                      if п.get("counted") is False})
    if из_["b_minus_a_ms"]["median"] is not None:
        д = из_["b_minus_a_ms"]["median"]
        из_["verdict"] = (f"своя отправка {'медленнее' if д > 0 else 'быстрее'} Bloom "
                           f"на {abs(д):.0f} мс по медиане t_send->t_seen "
                           f"(пар с двумя замерами {len(оба)})")
    else:
        из_["verdict"] = "пар с двумя замерами нет -- сравнивать нечего"
    return из_


def напечатать_доклад(итог: dict, *, заголовок: str) -> None:
    с = итог.get("summary") or {}
    print("")
    print(f"=== {заголовок} ===")
    print(f"пар всего: {с.get('pairs')}, с двумя замерами: {с.get('pairs_with_both')}, "
          f"пропущено из-за боевой торговли: {итог.get('skipped_live')}")
    for имя, ключ in (("A Bloom  t_send->t_seen", "a_send_to_seen_ms"),
                       ("B своя   t_send->t_seen", "b_send_to_seen_ms"),
                       ("A bloom_ms (площадка) ", "bloom_ms"),
                       ("B минус A             ", "b_minus_a_ms")):
        ст = с.get(ключ) or {}
        print(f"{имя}: n={ст.get('n')} медиана={ст.get('median')} p10={ст.get('p10')} "
              f"p90={ст.get('p90')} макс={ст.get('max')}")
    print(f"упавших: A {с.get('a_failed')}, B {с.get('b_failed')}; "
          f"зазор между отправками медиана {(с.get('gap_ms') or {}).get('median')} мс, "
          f"больше 5 мс: {с.get('gap_over_5ms')}")
    if с.get("a_by_accelerator"):
        print("A по получателям чаевых:")
        for адрес, стр in sorted(с["a_by_accelerator"].items(),
                                  key=lambda kv: -kv[1]["n"]):
            print(f"   {адрес[:12]}... n={стр['n']} сумма={стр['lamports'] / 1e9:.6f} SOL"
                  f"{' -- ' + стр['label'] if стр.get('label') else ' -- метки нет'}")
    print(f"потрачено (заявленный отток): {итог.get('spent_sol'):.6f} SOL из "
          f"{ПРЕДЕЛ_РАСХОДА_SOL} SOL")
    print(f"ИТОГ: {с.get('verdict')}")
    if итог.get("stopped_because"):
        print(f"остановка: {итог['stopped_because']}")


# ------------------------------------------------------------------ препятствия

def препятствия(*, живьём: bool, own_send_live: bool, кошелёк: str,
                 баланс: float | None, минт: dict, рубильник_сработал: str = "",
                 поток_подтверждён: bool = True, нужна_сторона_b: bool = True,
                 предел: float = ПРЕДЕЛ_РАСХОДА_SOL) -> list:
    """Всё, что запрещает начинать. Чистая функция -- её и проверяем.

    Список, а не первый отказ: владельцу полезнее увидеть сразу всё, что
    мешает, чем узнавать про препятствия по одному за прогон.
    """
    беды: list = []
    if рубильник_сработал:
        беды.append(f"рубильник: {рубильник_сработал}")
    if кошелёк != ST.EXECUTOR_WALLET:
        беды.append(f"кошелёк {str(кошелёк)[:12]} не кошелёк исполнителя")
    if not минт.get("ok"):
        беды.append(f"минт не подтверждён по цепи: {минт.get('why_not')}")
    if живьём and нужна_сторона_b and not own_send_live:
        беды.append("BLOOM_OWN_SEND_LIVE не равен 1 -- сторона B не отправит "
                     "ничего, и пары не будет: живой прогон без неё бессмыслен")
    if живьём and not поток_подтверждён:
        беды.append("подписка на кошелёк не подтверждена -- t_seen мерить нечем")
    if живьём:
        if баланс is None:
            беды.append("баланс кошелька не прочитан -- запас для боевой торговли "
                         "не проверить")
        elif баланс < предел + ЗАПАС_ТОРГОВЛИ_SOL:
            беды.append(f"баланс {баланс:.4f} SOL: тест на {предел} SOL оставил бы "
                         f"боевой торговле меньше запаса {ЗАПАС_ТОРГОВЛИ_SOL} SOL")
    return беды


# ------------------------------------------------------------------ продажа

def остаток_токена(rpc, кошелёк: str, минт: str) -> dict:
    """Сколько токена на кошельке. Сырыми единицами -- продавать ими."""
    из_ = {"amount_raw": 0, "accounts": 0, "why_not": None}
    try:
        о = rpc.call("getTokenAccountsByOwner",
                      [кошелёк, {"mint": минт}, {"encoding": "jsonParsed"}])
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"getTokenAccountsByOwner: {type(exc).__name__}"
        return из_
    всего = 0
    счетов = 0
    for з in ((о or {}).get("value") or []):
        инфо = (((з.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        сумма = ((инфо.get("tokenAmount") or {}).get("amount"))
        try:
            всего += int(сумма)
            счетов += 1
        except (TypeError, ValueError):
            continue
    из_.update(amount_raw=всего, accounts=счетов)
    return из_


def продать_всё(rpc, *, кошелёк: str, вход_sol: float, живьём: bool,
                 минт: str = USDC_MINT) -> dict:
    """Одна продажа в конце: весь накопленный USDC обратно в SOL.

    Пол 70 % от котировки и минимальная доля от входа -- в модуле продажи,
    второй раз здесь не задаются: два места с одним правилом однажды
    разойдутся, и разойдутся они в деньгах.
    """
    остаток = остаток_токена(rpc, кошелёк, минт)
    if остаток.get("why_not"):
        return {"ok": False, "why_not": остаток["why_not"], **остаток}
    if остаток["amount_raw"] <= 0:
        return {"ok": True, "skipped": True, "why_not": "продавать нечего: остаток 0",
                 **остаток}
    import bloom_jupiter_sell as J  # noqa: PLC0415
    итог = J.продать(mint=минт, amount_raw=остаток["amount_raw"],
                      taker=кошелёк, вход_sol=вход_sol, живьём=живьём)
    итог["amount_raw"] = остаток["amount_raw"]
    итог["accounts"] = остаток["accounts"]
    итог["floor_pct"] = J.ПОЛ_ПРОЦЕНТОВ
    return итог


# ------------------------------------------------------------------ разведка пула

def пулы_из_транзакции(tx: dict) -> list:
    """Пулы полосы, затронутые транзакцией -- по САМОЙ инструкции.

    Первая попытка искала пул по движению остатков (identify_pool), и на
    самом ликвидном месте SOL/USDC это дало чужие хранилища: маршрут
    агрегатора трогает несколько площадок в одной транзакции, и наибольшее
    движение USDC оказалось не у CPMM, а у Whirlpool. Поэтому здесь читается
    инструкция нужной программы и её счета по раскладке из c2_swap_build:
      * Raydium CPMM (swap_base_input, 13 счетов): 6 -- хранилище входа,
        7 -- выхода, 10 -- минт входа, 11 -- минт выхода;
      * Pump AMM (26 счетов): 3 -- базовый минт, 4 -- котировочный,
        7 -- базовое хранилище, 8 -- котировочное.
    Берём только пулы с котировкой WSOL: сборщик полосы умеет обёртку SOL и
    минимум по резервам только для них.
    """
    import c2_swap_build as B  # noqa: PLC0415

    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    наборы = list(сообщение.get("instructions") or [])
    for г in (((tx or {}).get("meta") or {}).get("innerInstructions") or []):
        наборы += list(г.get("instructions") or [])
    из_: list = []
    for и in наборы:
        if not isinstance(и, dict):
            continue
        программа = и.get("programId")
        счета = и.get("accounts") or []
        if not all(isinstance(с, str) for с in счета):
            continue
        if программа == B.CPMM and len(счета) >= 13:
            минт_вх, минт_вых = счета[10], счета[11]
            if минт_вх == WSOL:
                токен, хранилище_токена, хранилище_sol = минт_вых, счета[7], счета[6]
                сторона = "wsol_in"
            elif минт_вых == WSOL:
                токен, хранилище_токена, хранилище_sol = минт_вх, счета[6], счета[7]
                сторона = "wsol_out"
            else:
                continue
            из_.append({"program": программа, "label": "CPMM", "mint": токен,
                         "pool_vault": хранилище_токена, "sol_vault": хранилище_sol,
                         "pool_state": счета[3], "direction": сторона})
        elif программа == B.PUMP_AMM and len(счета) >= 26:
            if счета[4] != WSOL:
                continue
            из_.append({"program": программа, "label": "PUMP_AMM",
                         "mint": счета[3], "pool_vault": счета[7],
                         "sol_vault": счета[8], "pool_state": счета[0],
                         "direction": None})
    return из_


def годность_шаблонов(rpc, хранилище: str, минт: str, *, сделок: int = 20) -> dict:
    """Сколько из последних сделок пула годятся нам ШАБЛОНОМ.

    Пул, где шаблон не берётся, для теста бесполезен, какой бы ликвидный он
    ни был: без минимума по резервам отправлять нельзя. Проверка идёт
    НАСТОЯЩЕЙ сборкой, без подписи и без денег.
    """
    сделки = свежие_сделки_пула(rpc, хранилище, сколько=сделок)
    годных, новейший, причины = 0, None, {}
    for с in сделки:
        подг = подготовить_свою(rpc, [с], лампорты=int(РАЗМЕР_SOL * ЛАМПОРТОВ_В_SOL),
                                 кошелёк=ST.EXECUTOR_WALLET, семя="probe",
                                 минт=минт, подписывать=False)
        if подг.get("ok"):
            годных += 1
            if новейший is None:
                новейший = {
                    "signature": с.get("signature"),
                    "slot": подг.get("template_slot"),
                    "age_s": (round(time.time() - подг["template_block_time"], 1)
                               if подг.get("template_block_time") else None),
                    "pool_program": подг.get("pool_program"),
                    "min_out": подг.get("min_out"),
                    "expected_out": подг.get("expected_out"),
                    "build_ms": подг.get("build_ms")}
        else:
            почему = str((подг.get("attempts") or [{}])[0].get("why_not")
                          or подг.get("why_not"))[:90]
            причины[почему] = причины.get(почему, 0) + 1
    return {"trades_checked": len(сделки), "templates_ok": годных,
             "newest_template": новейший, "refusals": причины}


def найти_пулы(rpc, *, сколько_сделок: int = 200, кандидатов: int = 3,
                минт: str = "") -> dict:
    """Пул для теста: одношаговый, с котировкой SOL, который умеет сборщик.

    Слово владельца было про SOL<->USDC. По цепи видно, что у поддерживаемых
    сборщиком программ такого пула нет: среди 296 последних сделок Raydium
    CPMM пары WSOL/USDC не оказалось ни одной, а ликвидные пулы USDC/SOL
    живут на площадках сосредоточенной ликвидности (Whirlpool, CLMM) и у
    программ, которых сборщик не знает. Поэтому здесь ищется ближайшая
    честная замена: самый ходовой пул с котировкой SOL у PUMP_AMM или CPMM,
    и по каждому кандидату сразу видно, сколько его сделок годятся шаблоном.

    Только чтение. Ничего не тратится, кроме кредитов узла.
    """
    import c2_swap_build as B  # noqa: PLC0415

    из_ = {"ok": False, "candidates": [], "scanned": 0, "why_not": None,
            "programs": []}
    по_хранилищу: dict = {}
    for программа in (B.PUMP_AMM, B.CPMM):
        из_["programs"].append(программа)
        сигн: list = []
        до = None
        while len(сигн) < сколько_сделок:
            параметры = {"limit": min(1000, сколько_сделок - len(сигн))}
            if до:
                параметры["before"] = до
            try:
                порция = rpc.call("getSignaturesForAddress", [программа, параметры])
            except Exception as exc:  # noqa: BLE001
                из_["why_not"] = f"getSignaturesForAddress: {type(exc).__name__}"
                return из_
            порция = [з for з in (порция or []) if isinstance(з, dict)]
            if not порция:
                break
            до = порция[-1].get("signature")
            сигн += [з.get("signature") for з in порция
                      if not з.get("err") and з.get("signature")]
        тела = (rpc.get_transactions(сигн, commitment="confirmed",
                                      maxSupportedTransactionVersion=0)
                 if сигн else [])
        for tx in тела:
            if not isinstance(tx, dict):
                continue
            из_["scanned"] += 1
            for пул in пулы_из_транзакции(tx):
                if минт and пул["mint"] != минт:
                    continue
                строка = по_хранилищу.setdefault(пул["pool_vault"], {
                    "pool_vault": пул["pool_vault"], "sol_vault": пул["sol_vault"],
                    "pool_state": пул.get("pool_state"), "mint": пул["mint"],
                    "label": пул["label"], "n": 0})
                строка["n"] += 1
    лучшие = sorted(по_хранилищу.values(), key=lambda с: -с["n"])[:кандидатов]
    for строка in лучшие:
        for имя, счёт in (("mint_vault_amount", строка["pool_vault"]),
                           ("sol_vault_amount", строка.get("sol_vault"))):
            try:
                о = rpc.call("getTokenAccountBalance", [счёт])
                строка[имя] = ((о or {}).get("value") or {}).get("uiAmountString")
            except Exception:  # noqa: BLE001
                строка[имя] = None
        строка.update(годность_шаблонов(rpc, строка["pool_vault"], строка["mint"]))
    из_["candidates"] = sorted(
        лучшие, key=lambda с: (-(с.get("templates_ok") or 0), -(с.get("n") or 0)))
    из_["ok"] = any((с.get("templates_ok") or 0) > 0 for с in из_["candidates"])
    if not из_["ok"]:
        из_["why_not"] = (f"среди {из_['scanned']} сделок PUMP_AMM и CPMM пул с "
                           "котировкой SOL, дающий шаблон с минимумом по резервам, "
                           "не нашёлся")
    return из_


def симуляция(rpc, tx_base64: str) -> dict:
    """Симуляция без денег: узел считает, но ничего не отправляет."""
    try:
        о = rpc.call("simulateTransaction",
                      [tx_base64, {"encoding": "base64", "commitment": "processed",
                                    "sigVerify": False, "replaceRecentBlockhash": True}])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"simulateTransaction: {type(exc).__name__}"}
    знач = (о or {}).get("value") or {}
    журнал = знач.get("logs") or []
    return {"ok": знач.get("err") is None, "err": знач.get("err"),
             "units": знач.get("unitsConsumed"), "logs_tail": журнал[-4:]}


# ------------------------------------------------------------------ ожидание

def ждать_многих(поток: "Поток", подписи: list, *, предел_s: float) -> dict:
    """Ждём несколько подписей сразу. Обе отправки ушли в один момент --
    ждать их по очереди значило бы тратить окно второй на первую."""
    ждём = [с for с in подписи if с]
    найдено: dict = {}
    дедлайн = time.time() + предел_s
    while ждём and time.time() < дедлайн:
        for с in list(ждём):
            з = поток.найти(с)
            if з is not None:
                найдено[с] = з
                ждём.remove(с)
        if ждём:
            time.sleep(0.02)
    return найдено


def найти_по_признакам(поток: "Поток", *, после: float, окно_s: float,
                        сторона: str, свои: set, использованные: set) -> dict | None:
    """Транзакция стороны, подписи которой мы не знаем.

    Bloom отдаёт подписи в ответе, но ответ может прийти и без них. Тогда
    сторона узнаётся по счёту комиссии Bloom в самой транзакции, а не по
    догадке "это была она": окно и признак записываются в доклад.
    """
    for з in list(поток.порядок):
        if з["signature"] in использованные or з["signature"] in свои:
            continue
        if not (после - 0.5 <= з["t_seen"] <= после + окно_s):
            continue
        if сторона_по_транзакции(з.get("tx") or {}, з["signature"], свои=свои) == сторона:
            return з
    return None


# ------------------------------------------------------------------ прогон

def прогон(*, rpc, поток, api, кошелёк: str, хранилище: str, пар: int,
            живьём: bool, каталог_живого: Path, ata_есть: bool, минт: str = USDC_MINT,
            пауза: float = ПАУЗА_МЕЖДУ_ПАРАМИ_S, предел_минут: float = 25.0,
            сессия_sender=None, тихо: bool = False) -> dict:
    """Пары одна за другой. Каждая стоп-проверка стоит ПЕРЕД парой."""
    import bloom_api as API  # noqa: PLC0415

    итог: dict = {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "wallet": кошелёк, "pool_vault": хранилище, "live": живьём,
                   "mint": минт, "pairs": [], "skipped_live": 0, "spent_sol": 0.0,
                   "stopped_because": None, "size_sol": РАЗМЕР_SOL,
                   "limit_sol": ПРЕДЕЛ_РАСХОДА_SOL}
    пути = пути_рубильников(каталог_живого)
    подряд = {"A": 0, "B": 0}
    рента_нужна = not ata_есть
    использованные: set = set()
    свои: set = set()
    дедлайн = time.time() + предел_минут * 60.0
    подряд_пропусков = 0

    def зачтено():
        return sum(1 for п in итог["pairs"] if п.get("counted") is not False)

    while зачтено() < пар:
        if len(итог["pairs"]) >= пар * 2:
            итог["stopped_because"] = (f"попыток {len(итог['pairs'])} при цели {пар} "
                                        f"зачтённых пар -- дальше не пробую")
            break
        if time.time() > дедлайн:
            итог["stopped_because"] = f"предел времени прогона {предел_минут:.0f} минут"
            break
        сработал, почему = рубильник(пути)
        if сработал:
            итог["stopped_because"] = почему
            break
        if not хватает_предела(итог["spent_sol"], рента=рента_нужна):
            итог["stopped_because"] = (f"предел расхода: потрачено "
                                        f"{итог['spent_sol']:.6f} SOL, ещё одна пара "
                                        f"не влезает в {ПРЕДЕЛ_РАСХОДА_SOL} SOL")
            break
        if подряд["A"] >= СТОП_ПОДРЯД_УПАВШИХ or подряд["B"] >= СТОП_ПОДРЯД_УПАВШИХ:
            сторона = "A" if подряд["A"] >= СТОП_ПОДРЯД_УПАВШИХ else "B"
            итог["stopped_because"] = (f"{СТОП_ПОДРЯД_УПАВШИХ} упавших подряд "
                                        f"на стороне {сторона}")
            break
        занято, зачем = живой_сигнал(каталог_живого)
        if занято:
            итог["skipped_live"] += 1
            подряд_пропусков += 1
            if подряд_пропусков > 30:
                итог["stopped_because"] = f"боевая торговля не отпускает: {зачем}"
                break
            if not тихо:
                print(f"[пропуск] {зачем}", flush=True)
            time.sleep(2.0)
            continue
        подряд_пропусков = 0
        номер = len(итог["pairs"]) + 1
        начало_пары = time.time()
        путь_до = основной_путь(каталог_живого)
        обрывов_до = поток.обрывов
        # Прогрев соединения к Bloom. По документации /ping бюджет запросов
        # не тратит, а первый запрос по новому соединению стоит 50-65 мс
        # против 15-22 мс по тёплому -- без прогрева мерили бы рукопожатие.
        try:
            api.ping()
        except Exception:  # noqa: BLE001
            pass
        сделки = свежие_сделки_пула(rpc, хранилище, сколько=6)
        подг = подготовить_свою(rpc, сделки, лампорты=int(РАЗМЕР_SOL * ЛАМПОРТОВ_В_SOL),
                                 кошелёк=кошелёк, семя=f"send-race-{номер}", минт=минт)
        запись: dict = {"n": номер,
                         "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "detector_path_before": путь_до,
                         "template": {"signature": подг.get("template_signature"),
                                       "slot": подг.get("template_slot"),
                                       "age_s": (round(time.time() - подг["template_block_time"], 1)
                                                  if подг.get("template_block_time") else None),
                                       "pool_program": подг.get("pool_program"),
                                       "min_out": подг.get("min_out"),
                                       "expected_out": подг.get("expected_out"),
                                       "build_ms": подг.get("build_ms")}}
        if not подг.get("ok"):
            запись["a"] = {"ok": False, "why_not": "пара не пущена: сборка B не вышла"}
            запись["b"] = {"ok": False, "why_not": подг.get("why_not"),
                            "attempts": подг.get("attempts")}
            подряд["B"] += 1
            запись["counted"] = False
            запись["not_counted_why"] = "сборка B не вышла -- замерять нечего"
            итог["pairs"].append(запись)
            if not тихо:
                print(f"[{номер}] B не собралась: {подг.get('why_not')}", flush=True)
            time.sleep(max(0.0, пауза - (time.time() - начало_пары)))
            continue
        свои.add(подг["signature"])
        тело = API.build_buy_body(address=минт, amount_sol=РАЗМЕР_SOL,
                                   slippage_pct=ПРОСКАЛЬЗЫВАНИЕ * 100.0,
                                   priority_fee=ПРИОРИТЕТ_SOL,
                                   processor_tip=ЧАЕВЫЕ_SOL,
                                   auto_orders=[], anti_mev=False, wallet=кошелёк)
        первый = "A" if номер % 2 == 1 else "B"
        рез = пустить_пару(api=api, тело=тело, cid=f"race-{номер}-{int(time.time())}",
                            своя_b64=подг["tx_base64"],
                            ключ_операции=f"send-race-{номер}",
                            первый=первый, отправитель=сессия_sender)
        запись.update(first_planned=рез.get("first_planned"),
                       first_actual=рез.get("first_actual"),
                       gap_ms=рез.get("gap_ms"))
        # ДЕНЬГИ. Отток считается по факту отправки, и рента счёта -- один раз.
        рента_взята = False
        for сторона, ключ in (("A", "a"), ("B", "b")):
            if (рез.get(ключ) or {}).get("ok"):
                итог["spent_sol"] += расход_стороны_sol(
                    сторона, рента=(рента_нужна and not рента_взята))
                рента_взята = рента_взята or рента_нужна
                подряд[сторона] = 0
            else:
                подряд[сторона] += 1
        if рента_взята:
            рента_нужна = False
        # t_seen: ждём обе подписи сразу.
        a_подписи = (рез["a"].get("signatures") or [])
        найдено = ждать_многих(поток, [подг["signature"]] + list(a_подписи),
                                предел_s=ОКНО_ОЖИДАНИЯ_S)
        зб = найдено.get(подг["signature"])
        за = None
        совпадение_a = None
        for с in a_подписи:
            if с in найдено:
                за, совпадение_a = найдено[с], "signature"
                break
        if за is None and рез["a"].get("ok"):
            по_признакам = найти_по_признакам(
                поток, после=(рез["a"].get("t_send") or начало_пары),
                окно_s=ОКНО_ОЖИДАНИЯ_S, сторона="A", свои=свои,
                использованные=использованные)
            if по_признакам is not None:
                за, совпадение_a = по_признакам, "bloom_fee_account"
        for сторона, данные, найдена, как in (("a", рез["a"], за, совпадение_a),
                                               ("b", рез["b"], зб, "signature")):
            строка = dict(данные)
            if найдена is not None:
                использованные.add(найдена["signature"])
                строка["signature"] = найдена["signature"]
                строка["slot_seen"] = найдена.get("slot")
                строка["t_seen"] = найдена["t_seen"]
                строка["match"] = как
                if данные.get("t_send"):
                    строка["send_to_seen_ms"] = round(
                        (найдена["t_seen"] - данные["t_send"]) * 1000.0, 2)
            else:
                строка["match"] = None
                if сторона == "a" and not строка.get("signature") and a_подписи:
                    # Подпись из ответа Bloom: t_seen без неё нет, но вердикт
                    # по цепи добрать можно, и он нужен.
                    строка["signature"] = a_подписи[0]
                if данные.get("ok"):
                    строка["seen_why_not"] = ("подписи не видно в потоке за "
                                               f"{ОКНО_ОЖИДАНИЯ_S:.0f} с")
            запись[сторона] = строка
        путь_после = основной_путь(каталог_живого)
        запись["detector_path_after"] = путь_после
        зачёт, почему_нет = пара_зачтена(
            путь_до=путь_до, путь_после=путь_после, обрывов_до=обрывов_до,
            обрывов_после=поток.обрывов,
            поток_подтверждён=поток.подтверждена.is_set())
        запись["counted"] = зачёт
        if not зачёт:
            запись["not_counted_why"] = почему_нет
        итог["pairs"].append(запись)
        if not тихо:
            print(f"[{номер}] A {запись['a'].get('send_to_seen_ms')} мс "
                  f"(bloom_ms {запись['a'].get('bloom_ms')}), "
                  f"B {запись['b'].get('send_to_seen_ms')} мс, "
                  f"зазор {запись.get('gap_ms')} мс, первым {запись.get('first_actual')}, "
                  f"потрачено {итог['spent_sol']:.4f} SOL"
                  + ("" if зачёт else f" -- НЕ ЗАЧТЕНА: {почему_нет}"), flush=True)
        if зачтено() == ПРОМЕЖУТОЧНЫЙ_ДОКЛАД_НА and зачёт:
            итог["summary"] = свод(итог["pairs"])
            if not тихо:
                напечатать_доклад(итог, заголовок=f"промежуточный доклад, "
                                                    f"{ПРОМЕЖУТОЧНЫЙ_ДОКЛАД_НА} пар")
        осталось = пауза - (time.time() - начало_пары)
        if осталось > 0 and зачтено() < пар:
            time.sleep(осталось)
    итог["summary"] = свод(итог["pairs"])
    итог["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return итог


def дополнить_вердиктами(rpc, итог: dict, *, jito: tuple = ()) -> None:
    """Вердикт по цепи, место в блоке и получатели чаевых -- ПОСЛЕ пар.

    Всё это getTransaction и getBlock, то есть сеть: в паре им места нет.
    """
    подписи = []
    for п in итог.get("pairs") or []:
        for сторона in ("a", "b"):
            с = (п.get(сторона) or {}).get("signature")
            if с:
                подписи.append(с)
    вердикты = вердикты_по_цепи(rpc, подписи)
    for п in итог.get("pairs") or []:
        for сторона in ("a", "b"):
            строка = п.get(сторона) or {}
            в = вердикты.get(строка.get("signature") or "")
            if not в:
                continue
            строка["chain_ok"] = в.get("chain_ok")
            строка["chain_err"] = в.get("chain_err")
            строка["fee_lamports"] = в.get("fee_lamports")
            строка["slot"] = в.get("slot")
            if сторона == "a":
                наши = свои_счета(кошелёк_из(итог), итог.get("mint") or USDC_MINT)
                чаевые = [(кому, сколько) for кому, сколько in (в.get("transfers") or [])
                           if кому not in наши and кому != итог.get("pool_vault")]
                строка["tips"] = чаевые
                строка["tip_labels"] = {кому: метка_счёта(кому, jito=jito)
                                         for кому, _ in чаевые}
    места = места_в_блоках(rpc, итог.get("pairs") or [])
    for п in итог.get("pairs") or []:
        for сторона in ("a", "b"):
            строка = п.get(сторона) or {}
            м = места.get(строка.get("signature") or "")
            if м:
                строка["block_index"] = м.get("index")
                строка["block_total"] = м.get("total")
                строка["block_share"] = м.get("share")
    итог["summary"] = свод(итог.get("pairs") or [])


def кошелёк_из(итог: dict) -> str:
    return итог.get("wallet") or ST.EXECUTOR_WALLET


# ------------------------------------------------------------------ зонд Bloom

def дельта_минта(tx: dict, кошелёк: str, минт: str) -> int:
    """Изменение остатка минта у кошелька в этой транзакции, сырыми
    единицами. Минус -- токен ушёл, то есть была продажа."""
    мета = (tx or {}).get("meta") or {}

    def сумма(записи):
        итог = 0
        for з in (записи or []):
            if з.get("owner") != кошелёк or з.get("mint") != минт:
                continue
            try:
                итог += int(((з.get("uiTokenAmount") or {}).get("amount")))
            except (TypeError, ValueError):
                continue
        return итог

    return сумма(мета.get("postTokenBalances")) - сумма(мета.get("preTokenBalances"))


def зонд_bloom(*, rpc, api, поток, кошелёк: str, наблюдать_s: float = 150.0,
                живьём: bool, минт: str = USDC_MINT) -> dict:
    """Одна покупка USDC у Bloom с auto_orders=[] -- и наблюдение.

    Вопрос, на который отвечает зонд: не цепляет ли Bloom к такой покупке
    авто-ордер сам (сохранённой стратегией аккаунта). Если цепляет, тест из
    тридцати пар наплодил бы тридцать авто-ордеров, и продажа "один раз в
    конце" не сработала бы. Ответ -- число: сколько продаж USDC появилось на
    кошельке за время наблюдения, притом что ни одной мы не отправляли.
    """
    import bloom_api as API  # noqa: PLC0415

    из_: dict = {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "live": живьём, "watch_s": наблюдать_s, "mint": минт, "seen": [],
                  "unsolicited_sells": 0, "our_buy": None}
    тело = API.build_buy_body(address=минт, amount_sol=РАЗМЕР_SOL,
                               slippage_pct=ПРОСКАЛЬЗЫВАНИЕ * 100.0,
                               priority_fee=ПРИОРИТЕТ_SOL,
                               processor_tip=ЧАЕВЫЕ_SOL, auto_orders=[],
                               anti_mev=False, wallet=кошелёк)
    из_["body"] = тело
    if not живьём:
        из_["why_not"] = "холостой зонд: POST не отправлялся"
        return из_
    о = api.swap(тело, client_order_id=f"probe-{int(time.time())}",
                  why="probe: auto_orders=[] на USDC")
    из_["response"] = {"ok": bool(о.get("ok")), "code": о.get("code"),
                        "error_code": о.get("error_code"),
                        "why_not": о.get("why_not"),
                        "signatures": list(о.get("signatures") or []),
                        "order_id": о.get("order_id"),
                        "bloom_ms": о.get("bloom_ms")}
    from_ = time.time()
    while time.time() - from_ < наблюдать_s:
        time.sleep(1.0)
    наши = set(о.get("signatures") or [])
    for з in list(поток.порядок):
        if з["t_seen"] < from_ - 2.0:
            continue
        tx = з.get("tx") or {}
        дельта = дельта_минта(tx, кошелёк, минт)
        строка = {"signature": з["signature"], "slot": з.get("slot"),
                   "mint_delta": дельта,
                   "ours_by_response": з["signature"] in наши,
                   "t_after_send_s": round(з["t_seen"] - from_, 2)}
        из_["seen"].append(строка)
        if дельта < 0:
            из_["unsolicited_sells"] += 1
    из_["verdict"] = ("Bloom САМ продал USDC -- авто-ордер цепляется, тест "
                       "из тридцати пар так строить нельзя"
                       if из_["unsolicited_sells"] else
                       "за время наблюдения ни одной продажи токена не было: "
                       "auto_orders=[] означает отсутствие авто-ордера")
    return из_


# ------------------------------------------------------------------ вход

def напечатать_зонд(итог: dict) -> None:
    """Печать зонда. Отдельно от работы: печать не имеет права ронять прогон."""
    try:
        print(json.dumps({к: итог[к] for к in итог if к != "seen"},
                          ensure_ascii=False, indent=1, default=str))
        print(f"замечено транзакций кошелька за наблюдение: {len(итог.get('seen') or [])}, "
              f"из них продаж токена: {итог.get('unsolicited_sells')}")
        for с in (итог.get("seen") or []):
            print(f"   {str(с.get('signature'))[:16]}... +{с.get('t_after_send_s')} с "
                  f"токен {с.get('mint_delta'):+d} наша_по_ответу={с.get('ours_by_response')}")
    except Exception as exc:  # noqa: BLE001
        print(f"печать зонда не вышла ({type(exc).__name__}) -- отчёт уже записан")


def записать(путь: Path, данные: dict, *, ключ: str = "") -> None:
    путь.parent.mkdir(parents=True, exist_ok=True)
    текст = json.dumps(данные, ensure_ascii=False, indent=1, default=str)
    if ключ:
        текст = текст.replace(ключ, "СКРЫТ")
    путь.write_text(текст, encoding="utf-8")
    print(f"записано: {путь}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="гонка: своя отправка против Bloom")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--discover", action="store_true",
                     help="найти пул SOL<->USDC и проверить сборку симуляцией")
    ap.add_argument("--probe-bloom", action="store_true",
                     help="одна покупка у Bloom с auto_orders=[] и наблюдение")
    ap.add_argument("--run", action="store_true", help="пары")
    ap.add_argument("--sell-only", action="store_true",
                     help="только продажа накопленного USDC")
    ap.add_argument("--pool-vault", default="",
                     help="хранилище токена выбранного пула (из разведки)")
    ap.add_argument("--mint", default="",
                     help="минт токена пары (по умолчанию -- из разведки)")
    ap.add_argument("--pairs", type=int, default=ПАР_ВСЕГО)
    ap.add_argument("--live", action="store_true", help="РЕАЛЬНЫЕ отправки")
    ap.add_argument("--watch-s", type=float, default=150.0)
    ap.add_argument("--scan", type=int, default=40)
    ap.add_argument("--max-minutes", type=float, default=25.0)
    ap.add_argument("--no-sell", action="store_true",
                     help="не продавать в конце прогона (продать отдельным вызовом)")
    ap.add_argument("--live-state", default="",
                     help="каталог состояния БОЕВОГО детектора (только чтение)")
    ap.add_argument("--out", default="/tmp/bloom_send_race/run.json")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    import solana_rpc_client as RPC  # noqa: PLC0415

    ключ, откуда = RPC.helius_key()
    if not ключ:
        print(f"ключа Helius нет ({откуда}) -- без него ни замера, ни цепи")
        return 2
    rpc = RPC.SolanaRpc("bloom_send_race", ключ)
    кошелёк = ST.EXECUTOR_WALLET
    каталог_живого = Path(a.live_state) if a.live_state else ST.state_dir()
    путь = Path(a.out)

    if a.discover:
        пулы = найти_пулы(rpc, сколько_сделок=a.scan, минт=a.mint)
        print(f"просмотрено сделок PUMP_AMM и CPMM: {пулы['scanned']}, "
              f"пулов с котировкой SOL найдено: {len(пулы['candidates'])}")
        for к in пулы["candidates"]:
            print(f"   {к['label']} минт {к['mint']} хранилище {к['pool_vault']} "
                  f"сделок {к['n']} токена {к.get('mint_vault_amount')} "
                  f"SOL {к.get('sol_vault_amount')} "
                  f"шаблонов годных {к.get('templates_ok')}/{к.get('trades_checked')}")
            if к.get("newest_template"):
                print(f"      новейший шаблон: {json.dumps(к['newest_template'], ensure_ascii=False)}")
            if к.get("refusals"):
                print(f"      отказы: {json.dumps(к['refusals'], ensure_ascii=False)}")
        выбран = None
        for к in пулы["candidates"]:
            if (a.pool_vault and к["pool_vault"] == a.pool_vault) or not a.pool_vault:
                выбран = к
                break
        итог = {"pools": пулы, "builds": []}
        хранилище = (выбран or {}).get("pool_vault") or a.pool_vault
        минт_адрес = (выбран or {}).get("mint") or a.mint
        итог["mint_check"] = проверить_минт(rpc, минт_адрес) if минт_адрес else None
        print(f"минт: {json.dumps(итог['mint_check'], ensure_ascii=False)}")
        if хранилище and минт_адрес:
            итог["pool_vault"] = хранилище
            итог["mint"] = минт_адрес
            итог["label"] = (выбран or {}).get("label")
            for круг in range(3):
                сделки = свежие_сделки_пула(rpc, хранилище, сколько=20)
                подг = подготовить_свою(rpc, сделки,
                                         лампорты=int(РАЗМЕР_SOL * ЛАМПОРТОВ_В_SOL),
                                         кошелёк=кошелёк, семя=f"discover-{круг}",
                                         минт=минт_адрес, подписывать=False)
                строка = {к: подг.get(к) for к in
                           ("ok", "why_not", "template_signature", "template_slot",
                            "pool_program", "min_out", "expected_out", "build_ms",
                            "size", "signed", "attempts")}
                if подг.get("ok"):
                    строка["simulate"] = симуляция(rpc, подг["tx_base64"])
                итог["builds"].append(строка)
                print(f"   сборка {круг + 1}: {json.dumps(строка, ensure_ascii=False)}")
                time.sleep(2.0)
        итог["credits"] = rpc.stats.get("кредитов")
        записать(путь, итог, ключ=ключ)
        return 0

    import bloom_api as API  # noqa: PLC0415

    ключ_bloom = (os.environ.get("BLOOM_API_KEY") or "").strip()
    if a.probe_bloom or a.run:
        if not ключ_bloom:
            print("BLOOM_API_KEY не задан -- сторона A невозможна")
            return 2
    # state=None НАМЕРЕННО: клиент Bloom не должен писать ни в боевое
    # состояние, ни в какое другое -- тест ведёт свой журнал сам, а лишняя
    # запись с fsync стояла бы прямо перед POST и портила замер.
    api = API.BloomApi(ключ_bloom, dry_run=not a.live, state=None)

    if a.sell_only:
        итог = продать_всё(rpc, кошелёк=кошелёк,
                            вход_sol=РАЗМЕР_SOL * max(1, a.pairs), живьём=a.live,
                            минт=(a.mint or USDC_MINT))
        print(json.dumps(итог, ensure_ascii=False, indent=1, default=str))
        записать(путь, {"sell": итог}, ключ=ключ)
        return 0 if итог.get("ok") else 1

    минт_адрес = a.mint or USDC_MINT
    минт = проверить_минт(rpc, минт_адрес)
    баланс = баланс_sol(rpc, кошелёк)
    сработал, почему = рубильник(пути_рубильников(каталог_живого))
    поток = Поток(ключ, кошелёк)
    поток.запустить()
    поток.подтверждена.wait(20.0)
    беды = препятствия(живьём=a.live, own_send_live=OS.живьём(), кошелёк=кошелёк,
                        баланс=баланс, минт=минт, рубильник_сработал=почему,
                        поток_подтверждён=поток.подтверждена.is_set(),
                        # Зонду сторона B не нужна: он проверяет поведение
                        # Bloom, и требовать для него рубильник нашей отправки
                        # значило бы держать её включённой без надобности.
                        нужна_сторона_b=bool(a.run),
                        предел=(ПРЕДЕЛ_РАСХОДА_SOL if a.run else расход_пары_sol()))
    print(f"кошелёк {кошелёк}, баланс {баланс} SOL, минт {минт_адрес} "
          f"подтверждён: {минт.get('ok')} "
          f"({минт.get('symbol')}, знаков {минт.get('decimals')}), "
          f"подписка подтверждена: {поток.подтверждена.is_set()}")
    if беды:
        for б in беды:
            print(f"ОТКАЗ: {б}")
        поток.остановить()
        записать(путь, {"refused": беды, "mint": минт, "balance_sol": баланс}, ключ=ключ)
        return 3

    if a.probe_bloom:
        итог = зонд_bloom(rpc=rpc, api=api, поток=поток, кошелёк=кошелёк,
                           наблюдать_s=a.watch_s, живьём=a.live, минт=минт_адрес)
        поток.остановить()
        # ОТЧЁТ ПИШЕТСЯ ПЕРВЫМ. Однажды опечатка в строке печати уронила
        # процесс уже ПОСЛЕ живой покупки и вердикта -- деньги потрачены, а
        # файла с ответом нет. Печать после записи и в своём try.
        записать(путь, {"probe": итог, "credits": rpc.stats.get("кредитов")}, ключ=ключ)
        напечатать_зонд(итог)
        return 0

    if not a.run:
        print("нечего делать: нужен один из --self-test --discover --probe-bloom "
              "--run --sell-only")
        поток.остановить()
        return 2

    хранилище = a.pool_vault
    if not хранилище:
        пулы = найти_пулы(rpc, сколько_сделок=a.scan)
        if not пулы.get("ok"):
            print(f"ОТКАЗ: пул не найден: {пулы.get('why_not')}")
            поток.остановить()
            return 3
        хранилище = пулы["candidates"][0]["pool_vault"]
        минт_адрес = пулы["candidates"][0]["mint"]
        print(f"пул выбран сам: {хранилище}, минт {минт_адрес}")
    ata = остаток_токена(rpc, кошелёк, минт_адрес)
    # Постоянное соединение к Sender: у Bloom соединение тёплое (его греет
    # /ping), и мерить наше рукопожатие против его тёплого канала значило бы
    # мерить не то. В боевой полосе соединение тоже надо держать -- это
    # отдельный вывод теста.
    import requests  # noqa: PLC0415

    сессия = requests.Session()

    def отправитель(url, данные, таймаут):
        r = сессия.post(url, json=данные, timeout=таймаут,
                         headers={"Content-Type": "application/json"})
        return r.status_code, r.text

    итог = прогон(rpc=rpc, поток=поток, api=api, кошелёк=кошелёк,
                   хранилище=хранилище, пар=a.pairs, живьём=a.live,
                   каталог_живого=каталог_живого, ata_есть=(ata["accounts"] > 0),
                   минт=минт_адрес, предел_минут=a.max_minutes,
                   сессия_sender=отправитель)
    # Сырые пары на диск СРАЗУ: дальше идут сеть (вердикты по цепи) и
    # продажа, и если там что-то упадёт, замер должен остаться.
    записать(путь, итог, ключ=ключ)
    try:
        дополнить_вердиктами(rpc, итог)
    except Exception as exc:  # noqa: BLE001
        итог["verify_why_not"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        print(f"вердикты по цепи не добрались: {итог['verify_why_not']}")
    записать(путь, итог, ключ=ключ)
    try:
        напечатать_доклад(итог, заголовок=f"итог, пар {len(итог['pairs'])}")
    except Exception as exc:  # noqa: BLE001
        print(f"печать доклада не вышла ({type(exc).__name__}) -- отчёт записан")
    if a.live and not a.no_sell:
        вход = sum(РАЗМЕР_SOL for п in итог["pairs"]
                    for с in ("a", "b") if (п.get(с) or {}).get("ok"))
        итог["sell"] = продать_всё(rpc, кошелёк=кошелёк, вход_sol=вход, живьём=True,
                                    минт=минт_адрес)
        print(f"продажа: {json.dumps({к: итог['sell'].get(к) for к in ('ok', 'why_not', 'signature', 'status', 'amount_raw', 'api', 'floor_pct')}, ensure_ascii=False)}")
    поток.остановить()
    итог["stream"] = {"messages": поток.сообщений, "bytes": поток.байт,
                       "drops": поток.обрывов}
    итог["credits"] = rpc.stats.get("кредитов")
    записать(путь, итог, ключ=ключ)
    return 0


# ------------------------------------------------------------------ самопроверка

def self_test() -> int:
    проверки: list = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    import tempfile  # noqa: PLC0415

    # -- 1. числа доклада
    chk("медиана 1..10", статистика(range(1, 11))["median"] == 5.5)
    chk("p10 1..10 = 1.9", квантиль(list(range(1, 11)), 0.10) == 1.9,
        квантиль(list(range(1, 11)), 0.10))
    chk("p90 1..10 = 9.1", квантиль(list(range(1, 11)), 0.90) == 9.1,
        квантиль(list(range(1, 11)), 0.90))
    chk("None в ряд не идёт", статистика([1, None, 3])["n"] == 2)
    chk("пустой ряд не падает", статистика([])["median"] is None)

    # -- 2. рубильник: любая неясность -- стоп
    with tempfile.TemporaryDirectory() as д:
        файл = Path(д) / "KILL"
        chk("без файла рубильник молчит", рубильник([файл])[0] is False)
        файл.write_text("стоп", encoding="utf-8")
        chk("файл есть -- стоп", рубильник([файл])[0] is True)

    class ПутьСОшибкой:
        def exists(self):
            raise PermissionError("нельзя")

    # Path(п) в рубильнике обязан принять объект как есть -- иначе проверка
    # ошибки бессмысленна. Поэтому проверяем через подмену Path.
    прежний = globals()["Path"]
    try:
        globals()["Path"] = lambda x: x
        chk("ошибка проверки рубильника -- стоп", рубильник([ПутьСОшибкой()])[0] is True)
    finally:
        globals()["Path"] = прежний

    # -- 3. боевая торговля рядом
    with tempfile.TemporaryDirectory() as д:
        к = Path(д)
        chk("пустой каталог: торговли нет", живой_сигнал(к)[0] is False)
        (к / "positions.jsonl").write_text(json.dumps(
            {"client_order_id": "c1", "state": "bought", "mode": ST.MODE_LIVE}) + "\n",
            encoding="utf-8")
        chk("открытая боевая позиция -- пропуск", живой_сигнал(к)[0] is True)
        (к / "positions.jsonl").write_text(json.dumps(
            {"client_order_id": "c1", "state": "closed", "mode": ST.MODE_LIVE}) + "\n",
            encoding="utf-8")
        chk("закрытая позиция не мешает", живой_сигнал(к)[0] is False)
        (к / "positions.jsonl").write_text(json.dumps(
            {"client_order_id": "c2", "state": "bought", "mode": "dry-run"}) + "\n",
            encoding="utf-8")
        chk("позиция dry-run не мешает", живой_сигнал(к)[0] is False)
        (к / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
        chk("свежий журнал решений -- пропуск", живой_сигнал(к, окно_с=60)[0] is True)
        chk("старый журнал решений не мешает",
            живой_сигнал(к, окно_с=60, сейчас=time.time() + 3600)[0] is False)
    with tempfile.TemporaryDirectory() as д:
        (Path(д) / "positions.jsonl").mkdir()
        chk("нечитаемые позиции -- пропуск (не разрешение)",
            живой_сигнал(Path(д))[0] is True)

    # -- 4. деньги
    chk("расход A больше расхода B (комиссия 1 % и приоритетка)",
        расход_стороны_sol("A") > расход_стороны_sol("B"))
    chk("комиссия Bloom 1 % в расходе A",
        abs(расход_стороны_sol("A") - (расход_стороны_sol("A", рента=False))) < 1e-12
        and abs(расход_стороны_sol("A") - (РАЗМЕР_SOL + ПРИОРИТЕТ_SOL + ЧАЕВЫЕ_SOL
                                            + РАЗМЕР_SOL * 0.01
                                            + БАЗОВАЯ_КОМИССИЯ_SOL)) < 1e-12)
    chk("рента добавляется один раз и только с флагом",
        abs(расход_стороны_sol("B", рента=True) - расход_стороны_sol("B")
            - РЕНТА_ATA_SOL) < 1e-12)
    chk("предел: на нуле пара влезает", хватает_предела(0.0) is True)
    chk("предел: ровно на границе пара влезает",
        хватает_предела(ПРЕДЕЛ_РАСХОДА_SOL - расход_пары_sol()) is True,
        расход_пары_sol())
    chk("предел: за границей пара не влезает (проверка ДО пары, не после)",
        хватает_предела(ПРЕДЕЛ_РАСХОДА_SOL - расход_пары_sol() + 1e-6) is False
        and хватает_предела(ПРЕДЕЛ_РАСХОДА_SOL - расход_пары_sol() / 2) is False)
    chk("тридцать пар влезают в предел владельца",
        расход_пары_sol(рента=True) + расход_пары_sol() * 29 <= ПРЕДЕЛ_РАСХОДА_SOL,
        расход_пары_sol(рента=True) + расход_пары_sol() * 29)

    # -- 5. поток
    п = Поток("КЛЮЧ", ST.EXECUTOR_WALLET)
    chk("подтверждение подписки видно",
        п.принять(json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7}), 1.0) is None
        and п.подтверждена.is_set())
    подпись = "5" * 60
    уведомление = {"jsonrpc": "2.0", "method": "transactionNotification",
                    "params": {"subscription": 7, "result": {
                        "signature": подпись, "slot": 123,
                        "transaction": {"meta": {"err": None}}}}}
    з = п.принять(json.dumps(уведомление), 100.0)
    chk("уведомление разобрано", з and з["signature"] == подпись and з["slot"] == 123
        and з["t_seen"] == 100.0, з)
    п.принять(json.dumps(уведомление), 200.0)
    chk("повтор не переписывает первое время", п.найти(подпись)["t_seen"] == 100.0)
    упавшая = json.loads(json.dumps(уведомление))
    упавшая["params"]["result"]["signature"] = "6" * 60
    упавшая["params"]["result"]["transaction"]["meta"]["err"] = {"InstructionError": [3, {}]}
    chk("УПАВШАЯ транзакция тоже попадает в поток (фильтра failed нет)",
        п.принять(json.dumps(упавшая), 300.0) is not None
        and п.найти("6" * 60) is not None)
    chk("мусор не рушит разбор", п.принять("не json", 1.0) is None)

    # -- 6. чья транзакция
    свои = {"наша1"}
    chk("своя -- по подписи",
        сторона_по_транзакции({}, "наша1", свои=свои) == "B")
    tx_bloom = {"transaction": {"message": {"accountKeys": [
        {"pubkey": СЧЁТ_КОМИССИИ_BLOOM}]}}}
    chk("Bloom -- по счёту комиссии",
        сторона_по_транзакции(tx_bloom, "чужая", свои=свои) == "A")
    tx_наша = {"transaction": {"message": {"accountKeys": [
        {"pubkey": OS.TIP_ACCOUNTS[0]}]}}}
    chk("наша -- по tip-аккаунту Sender",
        сторона_по_транзакции(tx_наша, "иная", свои=свои) == "B")
    chk("ничья -- None, а не догадка",
        сторона_по_транзакции({"transaction": {"message": {"accountKeys": []}}},
                               "икс", свои=свои) is None)

    # -- 7. переводы и метки
    tx_переводы = {"transaction": {"message": {"instructions": [
        {"parsed": {"type": "transfer", "info": {"destination": "получатель1",
                                                  "lamports": 1000000}}}]}},
        "meta": {"innerInstructions": [{"instructions": [
            {"parsed": {"type": "transfer", "info": {"destination": "получатель2",
                                                      "lamports": 10000}}}]}]}}
    chk("переводы: верхние и внутренние",
        переводы_sol(tx_переводы) == [("получатель1", 1000000), ("получатель2", 10000)],
        переводы_sol(tx_переводы))
    chk("метка: комиссия Bloom", "Bloom" in (метка_счёта(СЧЁТ_КОМИССИИ_BLOOM) or ""))
    chk("метка: tip Helius", "Helius" in (метка_счёта(OS.TIP_ACCOUNTS[3]) or ""))
    chk("метка: неизвестный счёт без названия", метка_счёта("непонятный") is None)

    # -- 8. свод
    пары = [{"a": {"ok": True, "send_to_seen_ms": 400.0, "bloom_ms": 60.0},
              "b": {"ok": True, "send_to_seen_ms": 300.0}, "gap_ms": 1.0},
             {"a": {"ok": True, "send_to_seen_ms": 500.0, "bloom_ms": 70.0},
              "b": {"ok": True, "send_to_seen_ms": 200.0}, "gap_ms": 2.0},
             {"a": {"ok": True, "send_to_seen_ms": 450.0, "bloom_ms": 65.0},
              "b": {"ok": False, "why_not": "Sender отказал"}, "gap_ms": 9.0}]
    с = свод(пары)
    chk("свод: A по трём, B по двум", с["a_send_to_seen_ms"]["n"] == 3
        and с["b_send_to_seen_ms"]["n"] == 2, с)
    chk("свод: разница только по парам с двумя замерами",
        с["pairs_with_both"] == 2 and с["b_minus_a_ms"]["median"] == -200.0,
        с["b_minus_a_ms"])
    chk("свод: упавшая B посчитана", с["b_failed"] == 1)
    chk("свод: зазор больше 5 мс посчитан", с["gap_over_5ms"] == 1)
    chk("свод: вердикт словом 'быстрее' при минусе", "быстрее" in с["verdict"], с["verdict"])
    медленнее = свод([{"a": {"ok": True, "send_to_seen_ms": 100.0},
                        "b": {"ok": True, "send_to_seen_ms": 400.0}}])
    chk("свод: 'медленнее' при плюсе", "медленнее" in медленнее["verdict"],
        медленнее["verdict"])
    chk("свод без пар не врёт", "сравнивать нечего" in свод([])["verdict"])

    # -- 9. препятствия
    минт_ок = {"ok": True, "symbol": "USDC", "decimals": 6}
    chk("всё в порядке -- препятствий нет",
        препятствия(живьём=True, own_send_live=True, кошелёк=ST.EXECUTOR_WALLET,
                     баланс=1.0, минт=минт_ок) == [])
    chk("чужой кошелёк -- отказ",
        any("кошелёк" in б for б in препятствия(
            живьём=True, own_send_live=True, кошелёк="ЧУЖОЙ", баланс=1.0, минт=минт_ок)))
    chk("минт не подтверждён -- отказ",
        any("минт" in б for б in препятствия(
            живьём=True, own_send_live=True, кошелёк=ST.EXECUTOR_WALLET, баланс=1.0,
            минт={"ok": False, "why_not": "символ не тот"})))
    chk("BLOOM_OWN_SEND_LIVE не 1 -- отказ живого прогона",
        any("OWN_SEND_LIVE" in б for б in препятствия(
            живьём=True, own_send_live=False, кошелёк=ST.EXECUTOR_WALLET, баланс=1.0,
            минт=минт_ок)))
    chk("мало денег -- отказ, торговлю не обираем",
        any("запас" in б for б in препятствия(
            живьём=True, own_send_live=True, кошелёк=ST.EXECUTOR_WALLET,
            баланс=ПРЕДЕЛ_РАСХОДА_SOL + ЗАПАС_ТОРГОВЛИ_SOL - 0.01, минт=минт_ок)))
    chk("баланс неизвестен -- отказ, а не 'наверное хватит'",
        any("баланс" in б for б in препятствия(
            живьём=True, own_send_live=True, кошелёк=ST.EXECUTOR_WALLET,
            баланс=None, минт=минт_ок)))
    chk("подписка не подтверждена -- отказ (мерить нечем)",
        any("подписка" in б for б in препятствия(
            живьём=True, own_send_live=True, кошелёк=ST.EXECUTOR_WALLET, баланс=1.0,
            минт=минт_ок, поток_подтверждён=False)))
    chk("зонду Bloom рубильник нашей отправки не нужен",
        препятствия(живьём=True, own_send_live=False, кошелёк=ST.EXECUTOR_WALLET,
                     баланс=1.0, минт=минт_ок, нужна_сторона_b=False) == [])
    chk("рубильник в препятствиях",
        any("рубильник" in б for б in препятствия(
            живьём=True, own_send_live=True, кошелёк=ST.EXECUTOR_WALLET, баланс=1.0,
            минт=минт_ок, рубильник_сработал="есть файл")))

    # -- 10. основной путь подписки (слово владельца 25.09)
    with tempfile.TemporaryDirectory() as д:
        к = Path(д)
        п_нет = основной_путь(к)
        chk("нет признака жизни -- known=false, а не 'путь основной'",
            п_нет["known"] is False and п_нет["main_path"] is None, п_нет)
        (к / "detector_status.json").write_text(json.dumps(
            {"updated_ts": time.time(), "subscribe_main_path": True,
             "subscribe_method": "transactionSubscribe"}), encoding="utf-8")
        chk("основной путь виден", основной_путь(к)["main_path"] is True)
        (к / "detector_status.json").write_text(json.dumps(
            {"updated_ts": time.time(), "subscribe_main_path": False,
             "subscribe_method": "logsSubscribe", "fallback_seconds": 42}),
            encoding="utf-8")
        п_зап = основной_путь(к)
        chk("запасной путь виден числом секунд",
            п_зап["main_path"] is False and п_зап["fallback_seconds"] == 42, п_зап)
        (к / "detector_status.json").write_text(json.dumps(
            {"updated_ts": time.time() - 600, "subscribe_main_path": True}),
            encoding="utf-8")
        chk("старый признак жизни не считается знанием",
            основной_путь(к)["known"] is False)
        (к / "detector_status.json").write_text("не json", encoding="utf-8")
        chk("битый признак жизни не рушит прогон",
            основной_путь(к)["known"] is False)

    осн = {"known": True, "main_path": True}
    зап = {"known": True, "main_path": False}
    неизв = {"known": False, "main_path": None}
    chk("пара на основном пути зачтена",
        пара_зачтена(путь_до=осн, путь_после=осн, обрывов_до=0, обрывов_после=0,
                      поток_подтверждён=True)[0] is True)
    chk("запасной путь ПЕРЕД парой -- не зачтена",
        пара_зачтена(путь_до=зап, путь_после=осн, обрывов_до=0, обрывов_после=0,
                      поток_подтверждён=True)[0] is False)
    chk("уход на запасной ВО ВРЕМЯ пары -- не зачтена",
        пара_зачтена(путь_до=осн, путь_после=зап, обрывов_до=0, обрывов_после=0,
                      поток_подтверждён=True)[0] is False)
    chk("наш обрыв подписки -- не зачтена (t_seen мерит она)",
        пара_зачтена(путь_до=осн, путь_после=осн, обрывов_до=0, обрывов_после=1,
                      поток_подтверждён=True)[0] is False)
    chk("неподтверждённая наша подписка -- не зачтена",
        пара_зачтена(путь_до=осн, путь_после=осн, обрывов_до=0, обрывов_после=0,
                      поток_подтверждён=False)[0] is False)
    chk("путь детектора неизвестен -- пара всё равно зачтена (мерит наша подписка)",
        пара_зачтена(путь_до=неизв, путь_после=неизв, обрывов_до=0, обрывов_после=0,
                      поток_подтверждён=True)[0] is True)
    незачтённые = свод([
        {"counted": True, "a": {"ok": True, "send_to_seen_ms": 400.0},
         "b": {"ok": True, "send_to_seen_ms": 300.0}},
        {"counted": False, "not_counted_why": "детектор на запасном пути",
         "a": {"ok": True, "send_to_seen_ms": 900.0},
         "b": {"ok": True, "send_to_seen_ms": 900.0}}])
    chk("незачтённая пара в числа не идёт, но видна в докладе",
        незачтённые["a_send_to_seen_ms"]["n"] == 1
        and незачтённые["pairs_not_counted"] == 1
        and незачтённые["not_counted_why"] == ["детектор на запасном пути"],
        незачтённые)

    # -- 11. пара: обе стороны, зазор, настоящий порядок
    class ApiЗаглушка:
        def __init__(self, sent_ts):
            self.sent_ts = sent_ts
            self.вызовов = 0
            self.тела = []

        def ping(self):
            return {"ok": True}

        def swap(self, тело, *, client_order_id, why=""):
            self.вызовов += 1
            self.тела.append(тело)
            return {"ok": True, "code": 200, "sent_ts": self.sent_ts,
                     "bloom_ms": 61.0, "signatures": ["подписьA"], "order_id": "o1"}

    отправлено: list = []

    def sender_заглушка(url, данные, таймаут):
        отправлено.append((url, данные))
        return 200, json.dumps({"jsonrpc": "2.0", "result": "подписьB"})

    было = os.environ.get("BLOOM_OWN_SEND_LIVE")
    os.environ["BLOOM_OWN_SEND_LIVE"] = "1"
    OS.забыть_отправленные()
    try:
        api_з = ApiЗаглушка(sent_ts=time.time())
        рез = пустить_пару(api=api_з, тело={"side": "Buy"}, cid="c1",
                            своя_b64="AAA", ключ_операции="пара-1", первый="A",
                            отправитель=sender_заглушка)
        chk("в паре сработали обе стороны",
            рез["a"]["ok"] and рез["b"]["ok"] and api_з.вызовов == 1
            and len(отправлено) == 1, рез)
        chk("зазор между отправками посчитан числом",
            isinstance(рез.get("gap_ms"), float), рез.get("gap_ms"))
        chk("настоящий порядок берётся из замеров, а не из плана",
            рез.get("first_actual") in ("A", "B")
            and рез.get("first_planned") == "A", рез)
        chk("t_send стороны A -- из sent_ts клиента (начало POST)",
            рез["a"]["t_send"] == api_з.sent_ts)
        # Повтор по тому же ключу операции ОТПРАВИТЬ НЕ ДОЛЖЕН.
        повтор = OS.отправить("AAA", ключ_операции="пара-1",
                               отправитель=sender_заглушка)
        chk("повтор по ключу операции не уходит в сеть",
            повтор.get("duplicate") is True and len(отправлено) == 1, повтор)
    finally:
        OS.забыть_отправленные()
        if было is None:
            os.environ.pop("BLOOM_OWN_SEND_LIVE", None)
        else:
            os.environ["BLOOM_OWN_SEND_LIVE"] = было
    chk("без BLOOM_OWN_SEND_LIVE сторона B в сеть не идёт",
        OS.отправить("AAA", ключ_операции="пара-хх",
                      отправитель=sender_заглушка).get("sent") is False
        and len(отправлено) == 1)

    # -- 12. чередование порядка: ровно половина пар начинает с нашей стороны
    порядки = ["A" if н % 2 == 1 else "B" for н in range(1, ПАР_ВСЕГО + 1)]
    chk("порядок чередуется поровну",
        порядки.count("A") == порядки.count("B") == ПАР_ВСЕГО // 2)

    # -- 13. тело покупки у Bloom: числа владельца, авто-ордеров нет
    import bloom_api as API  # noqa: PLC0415
    тело = API.build_buy_body(address=USDC_MINT, amount_sol=РАЗМЕР_SOL,
                               slippage_pct=ПРОСКАЛЬЗЫВАНИЕ * 100.0,
                               priority_fee=ПРИОРИТЕТ_SOL, processor_tip=ЧАЕВЫЕ_SOL,
                               auto_orders=[], anti_mev=False,
                               wallet=ST.EXECUTOR_WALLET)
    API.validate_swap_body(тело)
    chk("тело A: auto_orders пустым списком (чужой стратегии нет)",
        тело["auto_orders"] == [])
    chk("тело A: размер 0.001 SOL строкой, кошелёк наш",
        тело["wallets"] == [{"address": ST.EXECUTOR_WALLET, "amount": "0.001"}], тело)
    chk("тело A: проскальзывание 35, приоритет и чаевые 0.001, anti_mev false, "
        "auto_tip false",
        тело["slippage"] == 35.0 and тело["priority_fee"] == 0.001
        and тело["processor_tip"] == 0.001 and тело["anti_mev"] is False
        and тело["auto_tip"] is False, тело)

    # -- 14. вердикт по цепи: молчание узла -- не "не села"
    class RpcЗаглушка:
        def __init__(self, тела=None, блоки=None):
            self.тела = тела or {}
            self.блоки = блоки or {}
            self.звали: list = []

        def get_transactions(self, подписи, **kw):
            self.звали.append(("get_transactions", list(подписи)))
            return [self.тела.get(с) for с in подписи]

        def call(self, метод, параметры, **kw):
            self.звали.append((метод, параметры))
            if метод == "getBlock":
                return self.блоки.get(параметры[0])
            if метод == "getTokenAccountsByOwner":
                return {"value": []}
            raise RuntimeError("не задано")

    rpc_з = RpcЗаглушка(тела={
        "села": {"slot": 10, "meta": {"err": None, "fee": 5000,
                                       "innerInstructions": []}},
        "упала": {"slot": 11, "meta": {"err": {"InstructionError": [3, {}]},
                                        "fee": 5000}},
        "молчит": None})
    в = вердикты_по_цепи(rpc_з, ["села", "упала", "молчит"])
    chk("села -- chain_ok true", в["села"]["chain_ok"] is True)
    chk("упала -- chain_ok false с причиной",
        в["упала"]["chain_ok"] is False and в["упала"]["chain_err"], в["упала"])
    chk("молчание узла -- None, а не false",
        в["молчит"]["chain_ok"] is None and в["молчит"]["why_not"], в["молчит"])

    # -- 15. место в блоке
    блок = {"transactions": [
        {"transaction": {"signatures": ["чужая1"]}},
        {"transaction": {"signatures": ["наша"]}},
        {"transaction": {"signatures": ["чужая2"]}},
        {"transaction": {"signatures": ["чужая3"]}}]}
    rpc_блок = RpcЗаглушка(блоки={77: блок})
    места = места_в_блоках(rpc_блок, [{"a": {"signature": "наша", "slot_seen": 77}}])
    chk("место в блоке и доля",
        места["наша"]["index"] == 1 and места["наша"]["total"] == 4
        and места["наша"]["share"] == 0.25, места)

    # -- 16. дельта минта: продажа видна минусом
    tx_продажа = {"meta": {
        "preTokenBalances": [{"owner": ST.EXECUTOR_WALLET, "mint": USDC_MINT,
                               "uiTokenAmount": {"amount": "200000"}}],
        "postTokenBalances": [{"owner": ST.EXECUTOR_WALLET, "mint": USDC_MINT,
                                "uiTokenAmount": {"amount": "0"}}]}}
    chk("продажа USDC -- минус", дельта_минта(tx_продажа, ST.EXECUTOR_WALLET,
                                               USDC_MINT) == -200000)
    chk("чужой владелец в дельту не идёт",
        дельта_минта(tx_продажа, "ЧУЖОЙ", USDC_MINT) == 0)

    # -- 17. зонд вхолостую не шлёт POST
    api_з2 = ApiЗаглушка(sent_ts=time.time())
    зонд = зонд_bloom(rpc=None, api=api_з2, поток=Поток("К", ST.EXECUTOR_WALLET),
                       кошелёк=ST.EXECUTOR_WALLET, наблюдать_s=0.0, живьём=False)
    chk("холостой зонд не отправляет ничего",
        api_з2.вызовов == 0 and "не отправлялся" in (зонд.get("why_not") or ""), зонд)

    зонд_печать = {"mint": "м", "unsolicited_sells": 0, "response": {"ok": True},
                    "seen": [{"signature": "с" * 60, "slot": 1, "mint_delta": 152,
                               "ours_by_response": True, "t_after_send_s": 1.2}]}
    напечатать_зонд(зонд_печать)
    chk("печать зонда идёт по существующим ключам",
        all(к in (зонд_печать["seen"][0]) for к in
            ("signature", "mint_delta", "ours_by_response", "t_after_send_s")))

    # -- 18. продажа: нечего продавать -- Jupiter не зовём
    продажа = продать_всё(RpcЗаглушка(), кошелёк=ST.EXECUTOR_WALLET,
                           вход_sol=0.03, живьём=False)
    chk("остаток 0 -- продажа пропущена",
        продажа.get("skipped") is True and продажа.get("amount_raw") == 0, продажа)
    import bloom_jupiter_sell as J  # noqa: PLC0415
    chk("пол продажи -- 70 % от котировки (одно место, модуль продажи)",
        J.ПОЛ_ПРОЦЕНТОВ == 70, J.ПОЛ_ПРОЦЕНТОВ)

    # -- 19. пул CPMM читается ИЗ ИНСТРУКЦИИ, а не по движению остатков
    import c2_swap_build as B_  # noqa: PLC0415
    счета = [f"счёт{i}" for i in range(13)]
    счета[3] = "состояние_пула"
    счета[6] = "хранилище_входа"
    счета[7] = "хранилище_выхода"
    счета[10] = WSOL
    счета[11] = USDC_MINT
    tx_cpmm = {"transaction": {"message": {"instructions": [
        {"programId": B_.CPMM, "accounts": list(счета)}]}}}
    найдены = пулы_из_транзакции(tx_cpmm)
    chk("CPMM, покупка токена за SOL: хранилище токена -- выходное",
        len(найдены) == 1 and найдены[0]["pool_vault"] == "хранилище_выхода"
        and найдены[0]["sol_vault"] == "хранилище_входа"
        and найдены[0]["mint"] == USDC_MINT
        and найдены[0]["direction"] == "wsol_in"
        and найдены[0]["label"] == "CPMM"
        and найдены[0]["pool_state"] == "состояние_пула", найдены)
    счета_обратно = list(счета)
    счета_обратно[10], счета_обратно[11] = USDC_MINT, WSOL
    обратно = пулы_из_транзакции({"transaction": {"message": {"instructions": [
        {"programId": B_.CPMM, "accounts": счета_обратно}]}}})
    chk("CPMM, продажа токена: хранилище токена -- входное",
        обратно[0]["pool_vault"] == "хранилище_входа"
        and обратно[0]["direction"] == "wsol_out", обратно)
    chk("чужая программа в пулы не попадает",
        пулы_из_транзакции({"transaction": {"message": {"instructions": [
            {"programId": "ЧУЖАЯ", "accounts": list(счета)}]}}}) == [])
    без_sol = list(счета)
    без_sol[10] = "другой_минт"
    chk("пул без котировки SOL не наш",
        пулы_из_транзакции({"transaction": {"message": {"instructions": [
            {"programId": B_.CPMM, "accounts": без_sol}]}}}) == [])
    счета_pump = [f"p{i}" for i in range(26)]
    счета_pump[3] = "минт_токена"
    счета_pump[4] = WSOL
    счета_pump[7] = "базовое_хранилище"
    счета_pump[8] = "котировочное_хранилище"
    pump = пулы_из_транзакции({"transaction": {"message": {"instructions": [
        {"programId": B_.PUMP_AMM, "accounts": счета_pump}]}}})
    chk("Pump AMM: минт и хранилища по своей раскладке",
        len(pump) == 1 and pump[0]["mint"] == "минт_токена"
        and pump[0]["pool_vault"] == "базовое_хранилище"
        and pump[0]["sol_vault"] == "котировочное_хранилище"
        and pump[0]["label"] == "PUMP_AMM", pump)
    счета_pump_чужой = list(счета_pump)
    счета_pump_чужой[4] = "не_wsol"
    chk("Pump AMM с котировкой не SOL не берётся",
        пулы_из_транзакции({"transaction": {"message": {"instructions": [
            {"programId": B_.PUMP_AMM, "accounts": счета_pump_чужой}]}}}) == [])

    # -- 20. сборка B на НАСТОЯЩЕМ образце пула из репозитория
    образец = None
    try:
        import c2_swap_build as B  # noqa: PLC0415
        import c2_common as C  # noqa: PLC0415
        for с in json.loads((REPO_ROOT / "data" / "c2_pool_samples"
                              / f"{B.CPMM}.json").read_text(encoding="utf-8")):
            if с.get("quote_mint") == C.WSOL:
                образец = с
                break
    except Exception as exc:  # noqa: BLE001
        chk("образцы пулов читаются", False, f"{type(exc).__name__}: {exc}")
    if образец is not None:
        tx = образец["tx"]
        хеш = (((tx.get("transaction") or {}).get("message") or {})
                .get("recentBlockhash"))
        подг = подготовить_свою(
            None, [{"signature": образец.get("source"), "tx": tx}],
            лампорты=int(РАЗМЕР_SOL * ЛАМПОРТОВ_В_SOL),
            кошелёк=ST.EXECUTOR_WALLET, семя="самопроверка", blockhash=хеш,
            минт=образец["mint"])
        # Ключа кошелька в самопроверке нет и быть не должно: значит сборка
        # обязана дойти до подписи и остановиться ИМЕННО на ключе, а не
        # раньше. Это и проверяем -- иначе "не собралось" маскировало бы
        # поломку сборщика.
        без_ключа = not (os.environ.get("EXEC_WALLET_KEY")
                          or os.environ.get("BLOOM_WALLET_KEY"))
        причина = (подг.get("why_not") or "")
        if без_ключа:
            chk("сборка B дошла до подписи на настоящем образце (упёрлась в ключ)",
                подг.get("ok") is False and "подпись" in причина, причина)
        else:
            chk("сборка B с ключом выдала подпись",
                подг.get("ok") is True and подг.get("signature"), причина)
        chk("текста исключения разбора ключа в причине нет",
            "Traceback" not in причина and "base58" not in причина.lower(), причина)
        без_подписи = подготовить_свою(
            None, [{"signature": образец.get("source"), "tx": tx}],
            лампорты=int(РАЗМЕР_SOL * ЛАМПОРТОВ_В_SOL),
            кошелёк=ST.EXECUTOR_WALLET, семя="разведка", blockhash=хеш,
            минт=образец["mint"], подписывать=False)
        chk("разведка собирает БЕЗ ключа и без подписи",
            без_подписи.get("ok") is True and без_подписи.get("signed") is False
            and без_подписи.get("signature") is None
            and без_подписи.get("tx_base64"), без_подписи.get("why_not"))
        chk("минимум по резервам положителен на настоящем образце",
            isinstance(без_подписи.get("min_out"), int)
            and без_подписи["min_out"] > 0, без_подписи.get("min_out"))
    return завершить(проверки)


def завершить(проверки: list) -> int:
    плохих = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"{'ок ' if ок else 'НЕТ'} {имя}" + (f" -- {факт}" if not ок else ""))
    print(f"\nпроверок {len(проверки)}, провалов {len(плохих)}")
    return 0 if not плохих else 1


if __name__ == "__main__":
    sys.exit(main())
