#!/usr/bin/env python3
"""Пул отправителей: один и тот же подписанный байт -- разными путями.

Владелец 25.09: "Кандидаты: Helius Sender (есть), Nozomi/Temporal, 0slot,
BlockRazor, Astralane" и отдельно: "добавь Jito Block Engine (Амстердам /
Франкфурт, одиночная транзакция с чаевыми на tip-счёт Jito) -- по З1 те, кто
впереди нас, платят именно Jito".

ЧТО ЗДЕСЬ ЕСТЬ. Реестр отправителей (data/senders.json, собран из
СОХРАНЁННОЙ документации) и одна функция отправки, которая знает разницу
между их протоколами. Ничего больше: сборка, подпись и пределы -- в
bloom_own_send, и дублировать их здесь нельзя.

ЧЕГО ЗДЕСЬ НЕТ.
  * Ключей. Имя секрета лежит в реестре (key_env), значение читается из
    окружения на хосте и никуда не печатается -- ни в журнал, ни в ответ.
  * Догадок. Нет адреса точки входа или не задан нужный секрет -- отправитель
    считается НЕПОДКЛЮЧЁННЫМ и в пул не идёт. Это честнее, чем послать
    транзакцию "куда-нибудь".
  * Чаевых мимо документации. Адрес чаевых берётся только из реестра: перевод
    на адрес не из списка -- это перевод неизвестно кому.

ПОЧЕМУ ЧАЕВЫЕ У КАЖДОГО СВОИ. В документации Nozomi и Helius прямо сказано:
каждая транзакция обязана содержать свой tip, а ниже минимума она молча
отбрасывается. Значит "одни чаевые на всех" не работает: для пула боевой
покупки в транзакцию кладутся чаевые КАЖДОМУ подключённому отправителю, и
их сумма ограничена отдельно (владелец: не более 0.005 SOL на сделку).
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from pathlib import Path

ФАЙЛ_РЕЕСТРА = "data/senders.json"
_РЕЕСТР: dict | None = None

# Форматы запроса. Разница только в теле и в том, куда идёт ключ.
ФОРМАТ_JSONRPC = "jsonrpc_sendTransaction"
ФОРМАТ_BLOCKRAZOR = "blockrazor_sendTransaction"


def путь_реестра(путь: str | None = None) -> Path:
    п = Path(путь or ФАЙЛ_РЕЕСТРА)
    if п.is_absolute() or п.exists():
        return п
    # Модуль зовут и из analysis/, и из корня репозитория, и со хоста из
    # каталога кода службы -- ищем рядом и на уровень выше.
    рядом = Path(__file__).resolve().parent / п.name
    выше = Path(__file__).resolve().parent.parent / п
    return выше if выше.exists() else рядом


def реестр(путь: str | None = None, *, заново: bool = False) -> dict:
    global _РЕЕСТР  # noqa: PLW0603
    if _РЕЕСТР is not None and not заново and путь is None:
        return _РЕЕСТР
    п = путь_реестра(путь)
    try:
        данные = json.loads(п.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        данные = {"senders": {}, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
    из_ = данные.get("senders") or {}
    if путь is None and not заново:
        _РЕЕСТР = из_
    return из_


def ключ_отправителя(з: dict) -> str | None:
    """Значение секрета из окружения. Наружу не возвращается ничего, кроме
    самого значения -- и оно используется только внутри запроса."""
    имя = з.get("key_env")
    if not имя:
        return None
    к = (os.environ.get(имя) or "").strip()
    return к or None


def готов(имя: str, з: dict) -> dict:
    """Подключён ли отправитель. Ответ -- со словами, а не только флагом."""
    из_ = {"sender": имя, "name": з.get("name") or имя, "ok": False,
            "why_not": None, "needs_key": bool(з.get("key_env")),
            "key_env": з.get("key_env")}
    if not з.get("url"):
        из_["why_not"] = (з.get("url_note")
                           or "точка входа не задана в реестре -- отправлять некуда")
        return из_
    if з.get("key_env") and not ключ_отправителя(з):
        из_["why_not"] = (f"секрет {з['key_env']} не задан в окружении -- "
                           "отправитель не подключён")
        return из_
    if not (з.get("tip_accounts") or []):
        из_["why_not"] = "в реестре нет ни одного счёта чаевых"
        return из_
    из_["ok"] = True
    return из_


def подключённые(путь: str | None = None) -> list:
    """Имена отправителей, которыми МОЖНО отправлять прямо сейчас."""
    return [и for и, з in sorted(реестр(путь).items()) if готов(и, з)["ok"]]


def состояние_пула(путь: str | None = None) -> dict:
    """Кто подключён, кто нет и почему -- для признака жизни и доклада."""
    р = реестр(путь)
    строки = [готов(и, з) for и, з in sorted(р.items())]
    return {"total": len(строки),
            "connected": [с["sender"] for с in строки if с["ok"]],
            "not_connected": [{к: с[к] for к in ("sender", "why_not", "key_env")}
                               for с in строки if not с["ok"]]}


def счёт_чаевых(имя: str, *, семя: str | None = None,
                 путь: str | None = None) -> str | None:
    """Адрес чаевых этого отправителя. Разный от сделки к сделке -- один и тот
    же счёт на каждой отправке это лишняя точка соперничества за запись (так
    прямо написано и в документации BlockRazor, и в документации Nozomi)."""
    счета = (реестр(путь).get(имя) or {}).get("tip_accounts") or []
    if not счета:
        return None
    if not семя:
        return random.choice(счета)
    return счета[sum(bytearray(семя.encode("utf-8"))) % len(счета)]


def минимум_чаевых(имя: str, *, путь: str | None = None) -> int:
    """Минимум чаевых из документации. Не назван -- берём минимум Helius
    (0.001 SOL): платить меньше неподтверждённого минимума значит рисковать
    молча отброшенной транзакцией."""
    з = реестр(путь).get(имя) or {}
    м = з.get("min_tip_lamports")
    return int(м) if isinstance(м, int) and м > 0 else 1_000_000


def _тело_и_адрес(имя: str, з: dict, tx_base64: str) -> tuple:
    """Куда и что посылать. Ключ подставляется здесь и только здесь."""
    url = з.get("url") or ""
    заголовки = {"Content-Type": "application/json"}
    ключ = ключ_отправителя(з)
    куда_ключ = (з.get("key_in") or "")
    if ключ and куда_ключ.startswith("query:"):
        имя_поля = куда_ключ.split(":", 1)[1]
        соединитель = "&" if "?" in url else "?"
        url = f"{url}{соединитель}{имя_поля}={ключ}"
    elif ключ and куда_ключ.startswith("header:"):
        заголовки[куда_ключ.split(":", 1)[1]] = ключ
    формат = з.get("format") or ФОРМАТ_JSONRPC
    if формат == ФОРМАТ_BLOCKRAZOR:
        # Документация BlockRazor: POST /sendTransaction со своим телом,
        # base64 рекомендован, mode=fast -- быстрый путь.
        тело = {"transaction": tx_base64, "mode": "fast",
                 "revertProtection": False}
    else:
        тело = {"jsonrpc": "2.0", "id": str(int(time.time() * 1000)),
                 "method": "sendTransaction",
                 "params": [tx_base64, {"encoding": "base64",
                                         "skipPreflight": True,
                                         "maxRetries": 0}]}
    return url, тело, заголовки


def подпись_из_ответа(имя: str, з: dict, ответ) -> tuple:
    """Подпись из ответа -- по формату отправителя. Нет подписи -- нет успеха:
    иначе мы будем искать в цепи то, чего, возможно, нет."""
    формат = з.get("format") or ФОРМАТ_JSONRPC
    if not isinstance(ответ, dict):
        return None, f"ответ не объект: {str(ответ)[:120]}"
    if ответ.get("error"):
        return None, f"отказ: {json.dumps(ответ['error'], ensure_ascii=False)[:200]}"
    if формат == ФОРМАТ_BLOCKRAZOR:
        # У BlockRazor подпись лежит в data/signature/result -- принимаем любое
        # из названий, но ТОЛЬКО строку: пустой объект успехом не считаем.
        for поле in ("signature", "data", "result", "txHash"):
            з_ = ответ.get(поле)
            if isinstance(з_, str) and з_:
                return з_, None
        return None, f"в ответе нет подписи: {json.dumps(ответ, ensure_ascii=False)[:160]}"
    рез = ответ.get("result")
    if isinstance(рез, str) and рез:
        return рез, None
    return None, f"в ответе нет подписи: {json.dumps(ответ, ensure_ascii=False)[:160]}"


# ЧТО МОЖЕТ ВЫНЕСТИ КЛЮЧ НАРУЖУ. У nozomi, astralane и 0slot ключ уходит
# ПАРАМЕТРОМ СТРОКИ ЗАПРОСА (?c=..., ?api-key=...). Значит ключ есть в самом
# адресе, а адрес попадает:
#   * в текст сетевого исключения requests -- там он целиком:
#     "HTTPSConnectionPool(host=..., port=443): Max retries exceeded with url:
#      /?c=КЛЮЧ (Caused by ...)";
#   * в тело ответа сервера, если сервер его эхом возвращает -- ровно это и
#     наблюдалось 25.09 на зонде ключей.
# Дальше этот текст уходит в why_not, в журнал решений, в позиции и в Telegram.
# Правило владельца: "В чат, логи, коммиты, тела запросов в журнале --
# никогда". Поэтому ЛЮБОЙ текст, идущий наружу из отправки, проходит вычистку.
_ХВОСТ_ЗАПРОСА = re.compile(r"\?[^\s\"\']+")


# ------------------------------------------------- тёплые соединения (keep-alive)

# ЗАЧЕМ. Владелец 25.09: "Холодное соединение -- десятки миллисекунд на сделку".
# Так и было: отправка звала requests.post напрямую, то есть на КАЖДУЮ отправку
# поднималось новое соединение -- TCP-рукопожатие плюс TLS у всех, кроме 0slot
# (у него HTTP). На пути сделки это чистая потеря места в блоке.
#
# ЧТО ТЕПЕРЬ. Одна сессия requests НА ОТПРАВИТЕЛЯ, живущая всё время работы
# службы: пул соединений держит канал открытым, и вторая отправка идёт по уже
# готовому. Сессия греется пингом раз в 20-30 с (слово владельца), потому что
# сервер закрывает простаивающее соединение: 0slot прямо пишет про таймаут 65 с.
#
# ВОЗРАСТ СОЕДИНЕНИЯ -- В ПРИЗНАК ЖИЗНИ. Без него "соединение тёплое" остаётся
# обещанием: по возрасту видно, поднималось ли оно заново.
_СЕССИИ: dict = {}
_ЗАМОК_СЕССИЙ = threading.Lock()
ПЕРИОД_ПРОГРЕВА_S = 25.0
# Сколько соединений держать на отправителя. Больше одного нужно веером: пять
# контролей уходят одновременно, и на одном соединении они встанут в очередь.
СОЕДИНЕНИЙ_НА_ОТПРАВИТЕЛЯ = 4


def сессия(имя: str):
    """Сессия requests этого отправителя. Одна на весь процесс.

    Создаётся при первой надобности и живёт дальше. Возвращается вместе с
    состоянием (когда создана, когда последний раз использована) -- это и есть
    возраст соединения для признака жизни.
    """
    with _ЗАМОК_СЕССИЙ:
        з = _СЕССИИ.get(имя)
        if з is not None:
            return з
        try:
            import requests  # noqa: PLC0415
            from requests.adapters import HTTPAdapter  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "why_not": f"requests не загружен: {type(exc).__name__}"}
        с = requests.Session()
        # Повторов на уровне адаптера НЕТ намеренно: повтор отправки транзакции
        # -- это возможная вторая покупка, и решать про повтор имеет право только
        # денежный путь, а не сетевая библиотека.
        адаптер = HTTPAdapter(pool_connections=СОЕДИНЕНИЙ_НА_ОТПРАВИТЕЛЯ,
                              pool_maxsize=СОЕДИНЕНИЙ_НА_ОТПРАВИТЕЛЯ,
                              max_retries=0)
        с.mount("https://", адаптер)
        с.mount("http://", адаптер)
        с.headers.update({"Connection": "keep-alive"})
        з = {"ok": True, "session": с, "created": time.time(), "used": None,
              "pings_ok": 0, "pings_failed": 0, "last_ping": None,
              "last_error": "", "reopened": 0}
        _СЕССИИ[имя] = з
        return з


def состояние_соединений(путь: str | None = None) -> dict:
    """Возраст соединения и счёт пингов по каждому отправителю -- в признак жизни."""
    сейчас = time.time()
    из_ = {}
    with _ЗАМОК_СЕССИЙ:
        для = dict(_СЕССИИ)
    for имя, з in для.items():
        из_[имя] = {
            "age_s": (round(сейчас - з["created"], 1) if з.get("created") else None),
            "last_use_s": (round(сейчас - з["used"], 1) if з.get("used") else None),
            "last_ping_s": (round(сейчас - з["last_ping"], 1)
                             if з.get("last_ping") else None),
            "pings_ok": з.get("pings_ok"), "pings_failed": з.get("pings_failed"),
            "reopened": з.get("reopened"),
            "last_error": (з.get("last_error") or "")[:120],
        }
    # Подключённый отправитель БЕЗ сессии -- это отправитель, у которого
    # соединение поднимется на первой же сделке. Такое надо видеть.
    for имя in подключённые(путь):
        из_.setdefault(имя, {"age_s": None, "note": "сессии ещё нет -- соединение "
                                                     "поднимется на первой отправке"})
    return из_


def адрес_прогрева(имя: str, з: dict | None = None, путь: str | None = None) -> str:
    """Куда стучаться для прогрева. Только то, что есть в реестре.

    У 0slot документация прямо даёт /health (письмо 25.09). У остальных пути
    проверки здоровья в сохранённой документации нет, поэтому греем САМУ точку
    входа методом GET: соединение от этого открывается, а транзакция не уходит --
    отправка бывает только POST со sendTransaction.
    """
    з = з if з is not None else ((реестр(путь) or {}).get(имя) or {})
    url = (з.get("url") or "").rstrip("/")
    if not url:
        return ""
    путь_здоровья = з.get("health_path")
    return f"{url}{путь_здоровья}" if путь_здоровья else url


def прогреть(имя: str, *, таймаут: float = 3.0, путь: str | None = None,
              запрос=None) -> dict:
    """Один пинг: открыть или подтвердить соединение. Ключ НЕ передаётся.

    Прогрев не несёт ни транзакции, ни ключа: он нужен ровно для того, чтобы
    соединение было открыто. Ответ сервера может быть любым (404 на GET -- тоже
    ответ): важно, что канал жив.
    """
    из_ = {"sender": имя, "ok": False, "http": None, "ms": None, "why_not": None}
    з_реестра = (реестр(путь) or {}).get(имя) or {}
    адрес = адрес_прогрева(имя, з_реестра, путь)
    if not адрес:
        из_["why_not"] = f"у {имя} в реестре нет адреса точки входа"
        return из_
    с = сессия(имя)
    if not с.get("ok"):
        из_["why_not"] = с.get("why_not")
        return из_
    t0 = time.perf_counter()
    try:
        if запрос is None:
            от = с["session"].get(адрес, timeout=таймаут)
            код = от.status_code
        else:
            код = запрос(адрес, таймаут)
    except Exception as exc:  # noqa: BLE001
        с["pings_failed"] = с.get("pings_failed", 0) + 1
        с["last_error"] = вычистить_секреты(f"{type(exc).__name__}: {exc}",
                                             путь=путь)[:160]
        из_["why_not"] = с["last_error"]
        из_["ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return из_
    с["pings_ok"] = с.get("pings_ok", 0) + 1
    с["last_ping"] = time.time()
    из_.update(ok=True, http=код,
               ms=round((time.perf_counter() - t0) * 1000, 2))
    return из_


def прогреть_всех(*, путь: str | None = None, таймаут: float = 3.0,
                   запрос=None) -> dict:
    """Пинг КАЖДОМУ подключённому. Зовётся по расписанию, вне пути сделки."""
    из_ = {"rows": [], "ok": 0, "failed": 0}
    for имя in подключённые(путь):
        с = прогреть(имя, таймаут=таймаут, путь=путь, запрос=запрос)
        из_["rows"].append(с)
        из_["ok" if с.get("ok") else "failed"] += 1
    из_["connections"] = состояние_соединений(путь)
    return из_


def значения_ключей(путь: str | None = None) -> list:
    """Значения всех ключей отправителей из окружения -- чтобы их вычистить.

    Читаются имена переменных ИЗ РЕЕСТРА (key_env), а не угадываются: реестр
    и есть единственное место, где сказано, чем платит каждый сервис.
    """
    из_ = []
    for _, з in (реестр(путь) or {}).items():
        имя_пер = (з or {}).get("key_env")
        if not имя_пер:
            continue
        знач = os.environ.get(имя_пер)
        if знач and len(знач) >= 4:
            из_.append(знач)
    return из_


def вычистить_секреты(текст, *, путь: str | None = None,
                       ключи: list | None = None) -> str:
    """Текст наружу без ключей: ни значением, ни строкой запроса.

    Два уровня, потому что одного мало:
      1. прямая замена известных значений (и регистровых вариантов: сервер
         может вернуть ключ приведённым к регистру);
      2. срезание ВСЕЙ строки запроса у любого адреса в тексте -- на случай
         ключа, которого мы не знаем (чужой редирект, другая переменная).
    Адрес без строки запроса и так возвращается отдельным полем url, так что
    диагностика от этого не беднеет.
    """
    из_ = "" if текст is None else str(текст)
    ряд = ключи if ключи is not None else значения_ключей(путь)
    for к in ряд:
        if not к or len(str(к)) < 4:
            continue
        for вар in (str(к), str(к).lower(), str(к).upper()):
            из_ = из_.replace(вар, "***")
    return _ХВОСТ_ЗАПРОСА.sub("?***", из_)


def отправить_через(имя: str, tx_base64: str, *, отправитель=None,
                     таймаут: float = 5.0, путь: str | None = None) -> dict:
    """Одна отправка ОДНИМ отправителем. Возвращает одинаковую форму для всех.

    Ключ в ответе не попадает: в поле url он вырезан, а заголовки не
    возвращаются вовсе.
    """
    з = реестр(путь).get(имя)
    из_ = {"sender": имя, "ok": False, "sent": False, "signature": None,
            "http": None, "send_ms": None, "why_not": None}
    if not з:
        из_["why_not"] = f"отправителя {имя} нет в реестре"
        return из_
    г = готов(имя, з)
    if not г["ok"]:
        из_["why_not"] = г["why_not"]
        return из_
    if not tx_base64:
        из_["why_not"] = "нечего отправлять: пустая транзакция"
        return из_
    url, тело, заголовки = _тело_и_адрес(имя, з, tx_base64)
    # В отчёт -- адрес БЕЗ ключа: он мог попасть в строку запроса.
    из_["url"] = (url.split("?")[0] if "?" in url else url)
    t0 = time.perf_counter()
    try:
        if отправитель is None:
            # ТЁПЛОЕ СОЕДИНЕНИЕ, А НЕ НОВОЕ НА КАЖДУЮ СДЕЛКУ. requests.post
            # поднимает соединение заново: TCP-рукопожатие плюс TLS -- десятки
            # миллисекунд на пути сделки (слово владельца 25.09). Сессия
            # отправителя живёт весь процесс и греется пингом.
            с_сессия = сессия(имя)
            if not с_сессия.get("ok"):
                из_["why_not"] = с_сессия.get("why_not") or "сессия не создана"
                return из_

            def отправитель(адрес, данные, заг, таймаут_):  # noqa: E306
                r = с_сессия["session"].post(адрес, json=данные, headers=заг,
                                              timeout=таймаут_)
                return r.status_code, r.text
        код, текст = отправитель(url, тело, заголовки, таймаут)
        # Отметка использования -- для возраста соединения в признаке жизни.
        сесс = _СЕССИИ.get(имя)
        if сесс is not None:
            сесс["used"] = time.time()
    except Exception as exc:  # noqa: BLE001
        из_["send_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        # Текст исключения может содержать адрес С КЛЮЧОМ в строке запроса.
        из_["why_not"] = вычистить_секреты(
            f"сеть: {type(exc).__name__}: {str(exc)[:200]}", путь=путь)[:200]
        return из_
    из_["send_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    из_["http"] = код
    try:
        ответ = json.loads(текст)
    except ValueError:
        из_["why_not"] = вычистить_секреты(
            f"ответ не JSON: {str(текст)[:200]}", путь=путь)[:200]
        return из_
    подпись, почему = подпись_из_ответа(имя, з, ответ)
    if подпись is None:
        # В почему лежит тело ответа сервера -- его тоже вычищаем.
        из_["why_not"] = вычистить_секреты(почему, путь=путь)[:240]
        return из_
    if код != 200:
        из_["why_not"] = f"код {код}, а не 200 (подпись {подпись[:12]})"
        return из_
    из_.update(ok=True, sent=True, signature=подпись)
    return из_


def отправить_всеми(tx_base64: str, *, имена: list | None = None,
                     отправитель=None, таймаут: float = 5.0,
                     путь: str | None = None) -> dict:
    """Один и тот же байт -- всеми подключёнными, ПАРАЛЛЕЛЬНО.

    Параллельно, а не по кругу: последовательная отправка добавляла бы
    каждому следующему отправителю задержку предыдущего, и сравнение "кто
    довёз" вышло бы про наш цикл, а не про них. Кто довёз -- видно по ответу;
    кто довёз ПЕРВЫМ -- по цепи (подпись у всех одна и та же).
    """
    import threading  # noqa: PLC0415

    ряд = имена if имена is not None else подключённые(путь)
    итоги: dict = {}
    потоки = []

    def один(имя):
        try:
            итоги[имя] = отправить_через(имя, tx_base64, отправитель=отправитель,
                                          таймаут=таймаут, путь=путь)
        except Exception as exc:  # noqa: BLE001
            итоги[имя] = {"sender": имя, "ok": False, "sent": False,
                           "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}

    t0 = time.perf_counter()
    for имя in ряд:
        п = threading.Thread(target=один, args=(имя,), name=f"send-{имя}",
                              daemon=True)
        п.start()
        потоки.append(п)
    for п in потоки:
        п.join(timeout=таймаут + 2.0)
    успехи = [и for и, р in итоги.items() if р.get("ok")]
    подписи = {р.get("signature") for р in итоги.values() if р.get("signature")}
    return {"senders": ряд, "results": итоги, "ok_senders": успехи,
            "ok": bool(успехи), "total_ms": round((time.perf_counter() - t0) * 1000, 2),
            # ОДНА ПОДПИСЬ НА ВСЕХ -- это нормально и ожидаемо: байт один.
            # Разные подписи означали бы, что кто-то из отправителей нам
            # подменил транзакцию, и это повод остановиться.
            "signatures": sorted(с for с in подписи if с),
            "one_signature": len(подписи) <= 1}


def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    р = реестр(заново=True)
    chk("реестр читается и в нём есть Helius и Jito",
        "helius" in р and "jito" in р, sorted(р))
    chk("у Jito адрес Амстердама и путь одиночной транзакции из документации",
        "amsterdam.mainnet.block-engine.jito.wtf" in (р["jito"]["url"] or "")
        and р["jito"]["url"].endswith("/api/v1/transactions"), р["jito"]["url"])
    chk("Jito не требует ключа, а 0slot и BlockRazor требуют",
        р["jito"]["key_env"] is None and р["zeroslot"]["key_env"] == "ZEROX_API"
        and р["blockrazor"]["key_env"] == "BLOCKRAZOR_AUTH_TOKEN", "")
    chk("восемь счетов чаевых Jito -- ровно те, что в его документации",
        len(р["jito"]["tip_accounts"]) == 8
        and "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5" in р["jito"]["tip_accounts"],
        р["jito"]["tip_accounts"][:2])
    chk("минимум чаевых Jito 1000 лампортов, BlockRazor 100000",
        минимум_чаевых("jito") == 1000 and минимум_чаевых("blockrazor") == 100_000, "")
    chk("минимум без подтверждения документацией -- как у Helius, а не ноль",
        минимум_чаевых("astralane") == 1_000_000, минимум_чаевых("astralane"))

    было = {}
    for имя_п in ("ZEROX_API", "BLOCKRAZOR_AUTH_TOKEN", "NOZOMI_API_KEY",
                   "ASTRALANE_API_KEY"):
        было[имя_п] = os.environ.pop(имя_п, None)
    try:
        связь = состояние_пула()
        chk("без секретов подключены ровно бесключевые: Helius и Jito",
            связь["connected"] == ["helius", "jito"], связь["connected"])
        chk("у неподключённых названа причина и имя нужного секрета",
            all(н["why_not"] for н in связь["not_connected"])
            and any(н.get("key_env") == "ZEROX_API"
                    for н in связь["not_connected"]), связь["not_connected"])
        # NOZOMI. Адрес точки входа появился 25.09: Амстердам ams1 из
        # ОФИЦИАЛЬНОГО endpoints.json пакета nozomi-sdk (файл сохранён в
        # data/docs/senders/nozomi_endpoints.json). Без секрета он всё равно
        # не подключён -- и причина названа именно секретом, а не адресом.
        nz = (реестр().get("nozomi") or {})
        chk("у Nozomi точка входа -- Амстердам из его же endpoints.json",
            nz.get("url") == "https://ams1.nozomi.temporal.xyz/", nz.get("url"))
        chk("ключ Nozomi идёт параметром ?c= -- как в его документации",
            nz.get("key_in") == "query:c", nz.get("key_in"))
        chk("без секрета Nozomi не подключён, и причина -- секрет",
            any(н["sender"] == "nozomi" and "NOZOMI_API_KEY" in (н["why_not"] or "")
                for н in связь["not_connected"]), связь["not_connected"])

        # --- ОТПРАВКА. Проверяем то, что стоит денег: адрес, тело, ключ,
        # разбор ответа и то, что подпись не выдумывается.
        зовы: list = []

        def сендер(адрес, данные, заг, таймаут):
            зовы.append({"url": адрес, "body": данные, "headers": dict(заг)})
            return 200, json.dumps({"jsonrpc": "2.0", "result": "ПОДПИСЬ_СЕТИ"})

        р1 = отправить_через("jito", "AAAA", отправитель=сендер)
        chk("Jito: тело -- jsonrpc sendTransaction с base64 и skipPreflight",
            р1["ok"] and зовы[-1]["body"]["method"] == "sendTransaction"
            and зовы[-1]["body"]["params"][1]["encoding"] == "base64"
            and зовы[-1]["body"]["params"][1]["skipPreflight"] is True, зовы[-1])
        chk("Jito: подпись из ответа и адрес без ключа в отчёте",
            р1["signature"] == "ПОДПИСЬ_СЕТИ" and "?" not in р1["url"], р1)

        os.environ["BLOCKRAZOR_AUTH_TOKEN"] = "СЕКРЕТ_BR"
        зовы.clear()

        def сендер_br(адрес, данные, заг, таймаут):
            зовы.append({"url": адрес, "body": данные, "headers": dict(заг)})
            return 200, json.dumps({"signature": "ПОДПИСЬ_BR"})

        р2 = отправить_через("blockrazor", "BBBB", отправитель=сендер_br)
        chk("BlockRazor: своё тело с mode=fast, ключ в заголовке apikey",
            р2["ok"] and зовы[-1]["body"] == {"transaction": "BBBB", "mode": "fast",
                                               "revertProtection": False}
            and зовы[-1]["headers"].get("apikey") == "СЕКРЕТ_BR", зовы[-1])
        chk("BlockRazor: подпись берётся из его поля signature",
            р2["signature"] == "ПОДПИСЬ_BR", р2)
        chk("ключ не попадает ни в url отчёта, ни в саму запись результата",
            "СЕКРЕТ_BR" not in json.dumps(р2, ensure_ascii=False), р2)

        os.environ["ZEROX_API"] = "СЕКРЕТ_0S"
        зовы.clear()
        р3 = отправить_через("zeroslot", "CCCC", отправитель=сендер)
        chk("0slot: ключ идёт в строку запроса, а в отчёте адреса без него",
            р3["ok"] and "api-key=СЕКРЕТ_0S" in зовы[-1]["url"]
            and "СЕКРЕТ_0S" not in json.dumps(р3, ensure_ascii=False), р3)

        def сендер_пусто(адрес, данные, заг, таймаут):
            return 200, json.dumps({"jsonrpc": "2.0", "id": "1"})

        р4 = отправить_через("jito", "DDDD", отправитель=сендер_пусто)
        chk("200 без подписи -- НЕ успех: искать в цепи было бы нечего",
            р4["ok"] is False and "нет подписи" in (р4["why_not"] or ""), р4)

        def сендер_отказ(адрес, данные, заг, таймаут):
            return 200, json.dumps({"error": {"code": -32602, "message": "нет"}})

        р5 = отправить_через("jito", "EEEE", отправитель=сендер_отказ)
        chk("отказ отправителя назван словами", р5["ok"] is False
            and "отказ" in (р5["why_not"] or ""), р5)

        def сендер_падает(адрес, данные, заг, таймаут):
            raise TimeoutError("сеть молчит")

        р6 = отправить_через("jito", "FFFF", отправитель=сендер_падает)
        chk("падение сети -- причина, а не исключение наружу",
            р6["ok"] is False and "TimeoutError" in (р6["why_not"] or ""), р6)

        # --- КЛЮЧ НЕ ВЫХОДИТ НАРУЖУ НИ ОДНИМ ПУТЁМ. У nozomi ключ идёт
        # параметром строки запроса, и текст сетевой ошибки requests содержит
        # адрес целиком. Этот текст уходит в why_not -> журнал решений ->
        # позиции -> Telegram. Правило владельца: ключей там не бывает.
        os.environ["NOZOMI_API_KEY"] = "КЛЮЧ_НОЗОМИ_ЖИВОЙ"

        def сендер_рвётся_с_адресом(адрес, данные, заг, таймаут):
            raise ConnectionError(
                "HTTPSConnectionPool(host='ams1.nozomi.temporal.xyz', port=443):"
                f" Max retries exceeded with url: {адрес}"
                " (Caused by NewConnectionError)")

        р7 = отправить_через("nozomi", "IIII",
                              отправитель=сендер_рвётся_с_адресом)
        chk("ключа нет в причине сетевой ошибки",
            "КЛЮЧ_НОЗОМИ_ЖИВОЙ" not in (р7["why_not"] or ""), р7["why_not"])
        chk("строка запроса в причине срезана целиком",
            "?***" in (р7["why_not"] or "") and "c=" not in (р7["why_not"] or ""),
            р7["why_not"])
        chk("сама причина при этом осталась читаемой",
            "ConnectionError" in (р7["why_not"] or ""), р7["why_not"])
        chk("ключа нет и в поле url",
            "КЛЮЧ_НОЗОМИ_ЖИВОЙ" not in (р7.get("url") or ""), р7.get("url"))

        def сендер_эхом_ключа(адрес, данные, заг, таймаут):
            # Ровно то, что наблюдалось 25.09 на зонде: сервер возвращает
            # адрес запроса эхом, а в адресе -- ключ.
            return 401, f"<html>unauthorized for {адрес}</html>"

        р8 = отправить_через("nozomi", "JJJJ", отправитель=сендер_эхом_ключа)
        chk("ключа нет в теле ответа, ушедшем в причину",
            "КЛЮЧ_НОЗОМИ_ЖИВОЙ" not in (р8["why_not"] or ""), р8["why_not"])

        def сендер_отказ_с_ключом(адрес, данные, заг, таймаут):
            return 200, json.dumps({"error": {"code": -32000,
                                               "message": f"bad key for {адрес}"}})

        р9 = отправить_через("nozomi", "KKKK", отправитель=сендер_отказ_с_ключом)
        chk("ключа нет в причине отказа сервера",
            "КЛЮЧ_НОЗОМИ_ЖИВОЙ" not in (р9["why_not"] or ""), р9["why_not"])
        chk("вычистка работает и для ключа, которого мы не знаем",
            "секрет" not in вычистить_секреты(
                "url: https://x.y/?api-key=секрет", ключи=[]),
            вычистить_секреты("url: https://x.y/?api-key=секрет", ключи=[]))
        os.environ.pop("NOZOMI_API_KEY", None)

        # --- ТЁПЛЫЕ СОЕДИНЕНИЯ (владелец 25.09: "холодное соединение --
        # десятки миллисекунд на сделку"; чинить первым). Проверяем то, что
        # решает скорость: сессия ОДНА и та же, отправка идёт через неё, пинг
        # её держит, а возраст видно числом.
        с1 = сессия("jito")
        с2 = сессия("jito")
        chk("сессия отправителя одна и та же, а не новая на каждый зов",
            с1.get("ok") and с1 is с2, (с1.get("ok"), с1 is с2))
        chk("у разных отправителей сессии РАЗНЫЕ",
            сессия("helius") is not сессия("jito"), "")
        # Отправка обязана идти ЧЕРЕЗ сессию: без этого всё остальное -- слова.
        рабочая = Path(__file__).read_text(encoding="utf-8").split("def self_test")[0]
        chk("в рабочей части нет requests.post напрямую -- только сессия",
            "requests.post(" not in рабочая, "requests.post найден")
        chk("сессия несёт заголовок keep-alive",
            (с1["session"].headers.get("Connection") or "").lower() == "keep-alive",
            с1["session"].headers.get("Connection"))
        # Пинг: сети в самопроверке нет, поэтому запрос подменяем.
        стук: list = []

        def запрос_п(адрес, таймаут):
            стук.append(адрес)
            return 200

        п1 = прогреть("jito", запрос=запрос_п)
        chk("пинг идёт на точку входа отправителя и считается",
            п1["ok"] and стук and стук[0].startswith("https://amsterdam"),
            (п1, стук[:1]))
        п2 = прогреть("zeroslot", запрос=запрос_п)
        chk("у 0slot пинг идёт на /health из его письма",
            п2["ok"] and стук[-1].endswith("/health")
            and стук[-1].startswith("http://ams1.0slot.trade"), стук[-1:])
        сост = состояние_соединений()
        chk("возраст соединения и счёт пингов видны числами",
            сост.get("jito", {}).get("age_s") is not None
            and сост["jito"]["pings_ok"] >= 1, сост.get("jito"))
        # Ошибка пинга не должна ронять прогрев: она считается и называется.
        def запрос_рвётся(адрес, таймаут):
            raise TimeoutError("сеть молчит")

        п3 = прогреть("jito", запрос=запрос_рвётся)
        chk("упавший пинг -- причина и счётчик, а не исключение наружу",
            п3["ok"] is False and "TimeoutError" in (п3["why_not"] or "")
            and состояние_соединений()["jito"]["pings_failed"] >= 1, п3)
        все_п = прогреть_всех(запрос=запрос_п)
        chk("прогрев обходит ВСЕХ подключённых",
            все_п["ok"] >= 2 and len(все_п["rows"]) == len(подключённые()),
            (все_п["ok"], len(все_п["rows"]), подключённые()))
        chk("в прогреве есть состояние соединений по именам",
            set(все_п["connections"]) >= set(подключённые()),
            sorted(все_п["connections"]))

        # --- 0SLOT ИЗ ПИСЬМА ВЛАДЕЛЬЦА 25.09. Это деньги: минимум чаевых,
        # получатели и адрес точки входа.
        р0 = реестр()["zeroslot"]
        chk("0slot: точка входа по HTTP, Амстердам -- как в письме",
            р0["url"] == "http://ams1.0slot.trade", р0["url"])
        chk("0slot: минимум чаевых 0.001 SOL, а не 0.0001",
            минимум_чаевых("zeroslot") == 1_000_000, минимум_чаевых("zeroslot"))
        chk("0slot: ровно пять счетов чаевых из письма",
            len(р0["tip_accounts"]) == 5
            and "4HiwLEP2Bzqj3hM2ENxJuzhcPCdsafwiet3oGkMkuQY4" in р0["tip_accounts"],
            р0["tip_accounts"])
        chk("0slot: адреса из примера кода (Eb2KpSC8) в списке НЕТ",
            not any(а.startswith("Eb2KpSC8") for а in р0["tip_accounts"]),
            р0["tip_accounts"])
        chk("0slot: чаевые ставятся в начало транзакции",
            р0.get("tip_first") is True, р0.get("tip_first"))
        chk("0slot: темп 5 вызовов в секунду записан",
            р0.get("rate_limit_rps") == 5, р0.get("rate_limit_rps"))
        chk("0slot: известные ошибки названы по именам",
            set(р0.get("errors_known") or {}) >= {"403 API key has expired",
                                                   "403 Invalid method",
                                                   "419 Rate limit exceeded"},
            sorted(р0.get("errors_known") or {}))

        chk("неизвестный отправитель -- отказ, а не отправка куда попало",
            отправить_через("несуществующий", "GGGG",
                             отправитель=сендер)["ok"] is False, "")
        os.environ.pop("ZEROX_API", None)
        chk("пропал секрет -- отправитель сразу неподключён",
            отправить_через("zeroslot", "HHHH", отправитель=сендер)["ok"] is False, "")

        # --- ВСЕМИ СРАЗУ. Один байт, одна подпись, параллельно.
        os.environ["BLOCKRAZOR_AUTH_TOKEN"] = "СЕКРЕТ_BR"
        порядок: list = []

        def сендер_медленный(адрес, данные, заг, таймаут):
            порядок.append(адрес)
            time.sleep(0.15)
            return 200, json.dumps({"jsonrpc": "2.0", "result": "ОДНА_ПОДПИСЬ",
                                     "signature": "ОДНА_ПОДПИСЬ"})

        t0 = time.perf_counter()
        все = отправить_всеми("IIII", отправитель=сендер_медленный)
        прошло = time.perf_counter() - t0
        chk("отправка всеми идёт ПАРАЛЛЕЛЬНО, а не по кругу",
            прошло < 0.15 * len(все["senders"]) and len(все["senders"]) >= 3,
            (прошло, все["senders"]))
        chk("довезли все подключённые и подпись у всех одна",
            все["ok"] and set(все["ok_senders"]) == set(все["senders"])
            and все["one_signature"] and все["signatures"] == ["ОДНА_ПОДПИСЬ"], все)

        def сендер_разные(адрес, данные, заг, таймаут):
            return 200, json.dumps({"jsonrpc": "2.0",
                                     "result": f"ПОДПИСЬ_{len(адрес)}",
                                     "signature": f"ПОДПИСЬ_{len(адрес)}"})

        разн = отправить_всеми("JJJJ", отправитель=сендер_разные)
        chk("разные подписи от разных отправителей -- это ПОВОД ОСТАНОВИТЬСЯ",
            разн["one_signature"] is False and len(разн["signatures"]) > 1,
            разн["signatures"])
    finally:
        for имя_п, знач in было.items():
            if знач is None:
                os.environ.pop(имя_п, None)
            else:
                os.environ[имя_п] = знач
        os.environ.pop("BLOCKRAZOR_AUTH_TOKEN", None)

    # --- СЧЁТ ЧАЕВЫХ: только из реестра, и разный от сделки к сделке.
    chk("счёт чаевых берётся из реестра этого отправителя",
        счёт_чаевых("jito", семя="A") in р["jito"]["tip_accounts"]
        and счёт_чаевых("helius", семя="A") in р["helius"]["tip_accounts"], "")
    chk("у неизвестного отправителя счёта чаевых нет, а не случайный",
        счёт_чаевых("несуществующий", семя="A") is None, "")
    разные = {счёт_чаевых("jito", семя=f"семя{и}") for и in range(20)}
    chk("счёт чаевых меняется от сделки к сделке", len(разные) > 1, разные)

    плохих = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'нет '}] {имя}" + ("" if ок else f" -- {факт}"))
    print(f"самопроверка пула отправителей: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


def main() -> int:
    import argparse  # noqa: PLC0415

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--status", action="store_true", help="кто подключён и почему нет")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    с = состояние_пула()
    print(f"отправителей в реестре: {с['total']}")
    print(f"подключены: {', '.join(с['connected']) or 'никто'}")
    for н in с["not_connected"]:
        print(f"  не подключён {н['sender']}: {н['why_not']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
