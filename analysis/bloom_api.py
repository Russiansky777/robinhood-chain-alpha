#!/usr/bin/env python3
"""Клиент Bloom API: единственная точка, откуда может уйти ордер.

Схема запроса взята из документации dev.bloombot.app (OpenAPI 3.1, Bloom
API 1.0.0), прочитанной владельцем 2026-09-23 17:15Z, и зафиксирована
здесь буквально -- вместе с единицами измерения, потому что ошибка в
единицах одного поля стоит денег напрямую:

  * wallets[].amount -- СТРОКА с десятичным числом; для Buy это
    абсолютная сумма quote-актива (SOL), "0.2" = 0.2 SOL, НЕ доля;
  * slippage -- ПРОЦЕНТЫ: 30 = 30 %;
  * priority_fee, processor_tip -- в SOL (Solana-хост);
  * auto_tip: true отдаёт fee и tip серверу и ИГНОРИРУЕТ processor_tip --
    поэтому у нас всегда false;
  * в авто-ордере все числа -- JSON numbers, не строки.

Три запрета, встроенные в код и доказанные самопроверкой:

  1. ОТПРАВКА ТОЛЬКО С НАШЕГО КОШЕЛЬКА. Адрес -- константа; в аккаунте
     Bloom есть второй кошелёк (тестовый W1 владельца), и попадание его в
     тело запроса должно быть невозможно, а не маловероятно.
  2. auto_orders ВСЕГДА ЯВНО. Опущенное или null поле означает у Bloom
     "прикрепить сохранённую стратегию аккаунта из Manager" -- то есть
     чужие TP/SL поверх нашей позиции. Тело без ключа auto_orders не
     уходит вообще.
  3. POST НЕ ПОВТОРЯЕТСЯ СЛЕПО. Эндпоинта статуса ордера у Bloom нет
     (их всего четыре: /swap, /deploy, /wallets, /ping), поэтому повтор
     покупки допустим только после проверки по цепи -- и решение об этом
     принимает вызывающий код, а не клиент.

Ответ 200 не означает, что сделка есть: подписи в ответе -- отправленные,
а не подтверждённые, и массив может быть пустым. Подтверждение -- только
по цепи.

Ключ не печатается нигде: всё, что уходит наружу, проходит через scrub().
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
# Путь к соседним модулям добавляется ДО их импорта: иначе модуль нельзя
# импортировать ниоткуда, кроме собственного каталога, и учёт с докладом
# об этом узнают только в бою.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bloom_exec_state import (  # noqa: E402
    EXECUTOR_WALLET, FOREIGN_WALLET_W1, ExecState)

HOST_EU = "https://eu.solana.bloombot.app"
PING_PATH = "/api/v1/ping"
WALLETS_PATH = "/api/v1/wallets"
SWAP_PATH = "/api/v1/swap"

# Обязательные поля /swap по документации.
SWAP_REQUIRED = ("address", "side", "wallets", "slippage", "priority_fee",
                  "processor_tip", "anti_mev", "auto_tip")
# Поля, которых в схеме запроса НЕТ или которые не наши. Отправка любого из
# них -- риск INVALID_REQUEST, а skip_if_bought вообще существует только на
# маркетинговой странице.
SWAP_FORBIDDEN = ("skip_if_bought", "quote_asset", "chain", "max_tax")
ORDER_REQUIRED = ("target_type", "target_value", "amount", "slippage", "priority_fee")
# target_type -- строка ("time"), остальные поля ордера -- JSON numbers.
ORDER_NUMERIC = ("target_value", "amount", "slippage", "priority_fee", "processor_tip")
MAX_AUTO_ORDERS = 20

SECRET_LIKE = re.compile(r"key|secret|token|password|mnemonic|private|auth|bearer|seed",
                          re.I)
RATE_HEADERS = re.compile(r"rate.?limit|retry.?after", re.I)

# Коды ошибок из документации. Делятся по тому, что с ними делать.
ERR_RATE_LIMITED = "RATE_LIMITED"
# Свой код на "ответ не JSON": по нему ветвится исполнитель, значит это
# машинный код, а не фраза для человека.
ERR_NOT_JSON = "RESPONSE_NOT_JSON"
ERR_RETRYABLE = ("INTERNAL_ERROR",)
ERR_SKIP_NOT_FAILURE = ("NO_ROUTE", "TOKEN_OR_POOL_NOT_FOUND",
                         "LIQUIDITY_OUT_OF_RANGE", "MARKET_CAP_OUT_OF_RANGE")
ERR_FATAL = ("NO_WALLETS", "INVALID_REQUEST", "WALLET_NOT_OWNED", "UNAUTHORIZED",
              "INSUFFICIENT_BALANCE")


def scrub(text: str, key: str) -> str:
    if not key:
        return text
    out = text.replace(key, "<КЛЮЧ>")
    if len(key) > 8:
        out = out.replace(key[:8], "<КЛЮЧ>").replace(key[-8:], "<КЛЮЧ>")
    return out


class BloomRefusal(Exception):
    """Клиент отказался отправлять запрос. Это не ошибка сети, а наш запрет."""


def build_timer_order(*, seconds: float, slippage: float, priority_fee: float,
                       processor_tip: float, amount_percent: int = 100,
                       anti_mev: bool = False) -> dict:
    """Таймерный авто-ордер: продать 100 % позиции через seconds секунд.

    Отсчёт у Bloom идёт от ПРИНЯТИЯ запроса, а не от подтверждения покупки --
    это записано в документации и важно для сторожа: его срок ожидания
    считается от того же момента.
    Все числа -- JSON numbers, не строки.
    """
    if not (1 <= amount_percent <= 100):
        raise BloomRefusal(f"amount авто-ордера {amount_percent} вне 1..100 %")
    if seconds <= 0:
        raise BloomRefusal(f"target_value {seconds} должен быть больше нуля")
    if priority_fee <= 0:
        raise BloomRefusal("priority_fee авто-ордера обязателен и должен быть > 0")
    if processor_tip < 0:
        raise BloomRefusal("processor_tip авто-ордера не может быть отрицательным")
    return {"target_type": "time", "target_value": seconds,
             "amount": amount_percent, "slippage": slippage,
             "priority_fee": priority_fee, "processor_tip": processor_tip,
             "anti_mev": anti_mev}


def build_buy_body(*, address: str, amount_sol: float, slippage_pct: float,
                    priority_fee: float, processor_tip: float,
                    auto_orders: list, anti_mev: bool = False,
                    wallet: str = EXECUTOR_WALLET) -> dict:
    """Тело покупки. amount -- СТРОКА, абсолютная сумма SOL."""
    return {"address": address, "side": "Buy",
             "wallets": [{"address": wallet, "amount": f"{amount_sol}"}],
             "slippage": slippage_pct, "priority_fee": priority_fee,
             "processor_tip": processor_tip, "anti_mev": anti_mev,
             "auto_tip": False, "auto_orders": list(auto_orders)}


def build_sell_body(*, address: str, percent: int, slippage_pct: float,
                     priority_fee: float, processor_tip: float,
                     anti_mev: bool = False, wallet: str = EXECUTOR_WALLET) -> dict:
    """Тело продажи. sell_mode по умолчанию percent, поэтому amount -- доля
    позиции строкой. auto_orders ПУСТОЙ СПИСОК: на продаже никакая
    сохранённая стратегия аккаунта нам не нужна."""
    if not (1 <= percent <= 100):
        raise BloomRefusal(f"percent {percent} вне 1..100")
    return {"address": address, "side": "Sell",
             "wallets": [{"address": wallet, "amount": f"{percent}"}],
             "slippage": slippage_pct, "priority_fee": priority_fee,
             "processor_tip": processor_tip, "anti_mev": anti_mev,
             "auto_tip": False, "auto_orders": []}


def validate_swap_body(body: dict) -> None:
    """Проверки ДО сети. Каждая существует из-за конкретной цены ошибки."""
    if not isinstance(body, dict):
        raise BloomRefusal("тело запроса должно быть объектом")
    # 1. auto_orders обязателен ЯВНО: опущенное поле = чужая стратегия из Manager.
    if "auto_orders" not in body:
        raise BloomRefusal(
            "в теле нет ключа auto_orders: у Bloom опущенное или null поле означает "
            "'прикрепить сохранённую стратегию аккаунта', то есть чужие TP/SL поверх "
            "нашей позиции. Отправка запрещена")
    if body["auto_orders"] is None:
        raise BloomRefusal("auto_orders=null означает чужую стратегию аккаунта -- "
                            "нужен либо явный список, либо []")
    if not isinstance(body["auto_orders"], list):
        raise BloomRefusal("auto_orders должен быть списком")
    if len(body["auto_orders"]) > MAX_AUTO_ORDERS:
        raise BloomRefusal(f"авто-ордеров {len(body['auto_orders'])} при пределе "
                            f"{MAX_AUTO_ORDERS}")
    for i, o in enumerate(body["auto_orders"]):
        нет = [k for k in ORDER_REQUIRED if k not in o]
        if нет:
            raise BloomRefusal(f"в авто-ордере {i} нет обязательных полей: {нет}")
        if not isinstance(o.get("target_type"), str):
            raise BloomRefusal(f"в авто-ордере {i} target_type должен быть строкой")
        for k in ORDER_NUMERIC:
            if k in o and isinstance(o[k], str):
                raise BloomRefusal(f"в авто-ордере {i} поле {k} -- строка; по схеме "
                                    "это JSON number")
    # 2. Обязательные поля запроса.
    нет = [k for k in SWAP_REQUIRED if k not in body]
    if нет:
        raise BloomRefusal(f"в теле нет обязательных полей: {нет}")
    # 3. Запрещённые поля.
    лишние = [k for k in SWAP_FORBIDDEN if k in body]
    if лишние:
        raise BloomRefusal(f"в теле есть поля, которых в схеме запроса нет или которые "
                            f"не наши: {лишние}")
    # 4. Кошелёк -- только наш.
    ws = body.get("wallets")
    if not isinstance(ws, list) or not ws:
        raise BloomRefusal("wallets должен быть непустым списком")
    for w in ws:
        addr = (w or {}).get("address")
        if addr != EXECUTOR_WALLET:
            raise BloomRefusal(
                f"в wallets адрес {addr!r}, а разрешён только кошелёк исполнителя "
                f"{EXECUTOR_WALLET}"
                + (" (это тестовый кошелёк W1 владельца, торговать им нельзя)"
                   if addr == FOREIGN_WALLET_W1 else ""))
        if not isinstance((w or {}).get("amount"), str):
            raise BloomRefusal("wallets[].amount по схеме -- строка с десятичным числом")
    # 5. Сторона и проскальзывание.
    if body.get("side") not in ("Buy", "Sell"):
        raise BloomRefusal(f"side {body.get('side')!r} должен быть Buy или Sell")
    s = body.get("slippage")
    if not isinstance(s, (int, float)) or isinstance(s, bool) or not (0 < s <= 100):
        raise BloomRefusal(f"slippage {s!r} -- проценты в (0, 100]")
    # 6. auto_tip=true игнорирует processor_tip, значит наши значения пропадут.
    if body.get("auto_tip") is not False:
        raise BloomRefusal("auto_tip должен быть false: при true сервер ставит свои fee "
                            "и tip и ИГНОРИРУЕТ processor_tip")
    for k in ("priority_fee", "processor_tip"):
        v = body.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            raise BloomRefusal(f"{k} {v!r} -- число в SOL, не меньше нуля")


class СчётчикСоединений(logging.Handler):
    """Сколько НОВЫХ соединений открыл urllib3 -- прямое доказательство, а не
    вывод из миллисекунд.

    urllib3 пишет в логгер urllib3.connectionpool строку "Starting new HTTPS
    connection" ровно на каждое новое соединение. Ловим её обработчиком и
    считаем. Разница счётчика до и после POST: 0 -- соединение было тёплым,
    1 и больше -- рукопожатие оплачено заново.

    Обработчик вешается один раз на процесс и ничего не печатает.
    """

    ФРАЗА = "Starting new HTTP"

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.новых = 0

    def emit(self, record) -> None:
        try:
            if self.ФРАЗА in str(record.getMessage()):
                self.новых += 1
        except Exception:  # noqa: BLE001
            pass


_СЧЁТЧИК_СОЕДИНЕНИЙ = None


def счётчик_соединений() -> "СчётчикСоединений | None":
    """Один счётчик на процесс. Ставится лениво и молча."""
    global _СЧЁТЧИК_СОЕДИНЕНИЙ  # noqa: PLW0603
    if _СЧЁТЧИК_СОЕДИНЕНИЙ is None:
        try:
            лог = logging.getLogger("urllib3.connectionpool")
            h = СчётчикСоединений()
            лог.addHandler(h)
            # Уровень нужен ровно такой, иначе сообщение до обработчика не
            # дойдёт. Распространение наверх выключаем, чтобы отладка urllib3
            # не полезла в общий журнал службы.
            if лог.level == logging.NOTSET or лог.level > logging.DEBUG:
                лог.setLevel(logging.DEBUG)
            лог.propagate = False
            _СЧЁТЧИК_СОЕДИНЕНИЙ = h
        except Exception:  # noqa: BLE001
            return None
    return _СЧЁТЧИК_СОЕДИНЕНИЙ


def сессия_с_пулом(соединений: int = 4, размер: int = 8):
    """Сессия с пулом соединений: TCP и TLS платятся один раз, а не на вызов.

    Замер с NL-хоста 24.09.2026 (curl 8.5, два ОДИНАКОВЫХ запроса в одном
    вызове, решающее поле num_connects):

      Bloom /ping, новое соединение: tcp 2.3-2.5 мс, tls 19.9-21.5 мс,
        первый байт 54.8-65.1 мс;
      он же по уже открытому: tcp 0, tls 0, первый байт 15.4-22.6 мс.

    Разница 39-45 мс на запрос. Около 20 мс из них -- доказанно рукопожатие
    (tcp+tls), остальное -- более быстрый первый байт на прогретом пути.
    """
    s = requests.Session()
    try:
        from requests.adapters import HTTPAdapter  # noqa: PLC0415
        адаптер = HTTPAdapter(pool_connections=соединений, pool_maxsize=размер,
                               max_retries=0)
        s.mount("https://", адаптер)
        s.mount("http://", адаптер)
    except Exception:  # noqa: BLE001
        # Без адаптера сессия всё равно держит keep-alive -- это не повод
        # падать, но и молчать об этом не надо: пул просто будет стандартный.
        pass
    return s


class Прогрев:
    """Держит соединение к Bloom открытым: /ping раз в N секунд.

    Зачем это нужно именно здесь. Сессия у клиента одна на весь процесс, но
    между сигналами пауза бывает минутами, а keep-alive у края (в ответе
    server: cloudflare) живёт десятки секунд. Без прогрева боевая покупка
    почти всегда платит рукопожатие заново -- те самые 39-45 мс из замера.

    Цена прогрева: /ping и /wallets бюджет запросов Bloom НЕ тратят (по
    документации), значит платим только трафиком.

    Поток отдельный: задержка сети в прогреве не должна задерживать
    торговлю, а его отказ -- не должен её ронять.
    """

    def __init__(self, api, период_s: float = 20.0) -> None:
        self.api = api
        self.период_s = float(период_s)
        self.успехов = 0
        self.отказов = 0
        self.пропущено = 0
        self.последний_код = None
        self.последняя_ошибка = ""
        self.последний_utc = ""
        self.поток = None
        self.стоп = False

    def круг(self) -> bool:
        """Один прогрев. Наружу не бросает: это фон, а не решение."""
        if getattr(self.api, "занят_покупкой", False):
            # Покупка в полёте: свой пинг пропускаем, чтобы пул не отдал ей
            # второе, холодное соединение. Пропуск считается отдельно.
            self.пропущено += 1
            return False
        try:
            r = self.api.ping()
        except Exception as exc:  # noqa: BLE001
            self.отказов += 1
            self.последняя_ошибка = f"{type(exc).__name__}: {str(exc)[:120]}"
            return False
        self.последний_код = r.get("code")
        self.последний_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if r.get("ok"):
            self.успехов += 1
            self.последняя_ошибка = ""
            return True
        self.отказов += 1
        self.последняя_ошибка = str(r.get("why_not") or r.get("code"))[:120]
        return False

    def цикл(self, сон=None) -> None:
        сон = сон or time.sleep
        while not self.стоп:
            self.круг()
            сон(self.период_s)

    def запустить_в_потоке(self):
        import threading  # noqa: PLC0415
        self.поток = threading.Thread(target=self.цикл, name="bloom-keepalive",
                                       daemon=True)
        self.поток.start()
        return self.поток

    def признак_жизни(self) -> dict:
        return {"enabled": True, "period_s": self.период_s,
                 "ok": self.успехов, "failed": self.отказов,
                 "skipped_while_buying": self.пропущено,
                 "last_code": self.последний_код,
                 "last_utc": self.последний_utc,
                 "last_error": self.последняя_ошибка}


class BloomApi:
    """Тонкий клиент. Сеть только здесь, POST только один."""

    def __init__(self, key: str, *, dry_run: bool = True, state: ExecState | None = None,
                  host: str = HOST_EU, timeout: int = 20,
                  session: requests.Session | None = None) -> None:
        self.key = key or ""
        self.dry_run = bool(dry_run)
        self.state = state
        self.host = host
        self.timeout = timeout
        self.session = session or сессия_с_пулом()
        # Покупка в полёте. Прогрев обязан пропустить свой круг, пока флаг
        # стоит: иначе пул отдаст покупке ВТОРОЕ соединение, а оно холодное.
        # Покупка при этом НИЧЕГО не ждёт -- флаг только для прогрева.
        self.занят_покупкой = False

    # ------------------------------------------------------------- служебное

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}",
                 "Content-Type": "application/json"}

    def _note_headers(self, headers) -> dict:
        h = {k: v for k, v in (headers or {}).items() if RATE_HEADERS.search(k)}
        if self.state is not None:
            self.state.note_rate_headers(headers or {})
        return h

    def _log_call(self, row: dict) -> None:
        if self.state is None:
            return
        from bloom_exec_state import append_jsonl_fsync  # noqa: PLC0415
        append_jsonl_fsync(self.state.api_path,
                            {"ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             **row})

    # ------------------------------------------------------------------- GET

    def ping(self) -> dict:
        """Бюджет запросов НЕ тратит (по документации)."""
        return self._get(PING_PATH)

    def wallets(self) -> dict:
        """Бюджет запросов НЕ тратит (по документации)."""
        return self._get(WALLETS_PATH)

    def _get(self, path: str) -> dict:
        try:
            r = self.session.get(self.host + path, headers=self._headers(),
                                  timeout=self.timeout)
        except requests.RequestException as exc:
            return {"ok": False, "code": None, "network": True,
                     "why_not": scrub(f"{type(exc).__name__}: {exc}", self.key)[:300]}
        out = {"code": r.status_code, "rate_headers": self._note_headers(r.headers)}
        try:
            out["body"] = r.json()
        except ValueError:
            out["body_not_json"] = scrub(r.text[:300], self.key)
        out["ok"] = r.status_code == 200
        return out

    # ------------------------------------------------------------------ POST

    def swap(self, body: dict, *, client_order_id: str, why: str = "") -> dict:
        """ЕДИНСТВЕННЫЙ POST во всём проекте. Повторов внутри нет.

        Возвращает разобранный результат; решение о повторе принимает
        вызывающий код -- и только после проверки по цепи, потому что
        эндпоинта статуса ордера у Bloom не существует.
        """
        validate_swap_body(body)
        # Тело можно печатать целиком: ключа в нём нет, он только в заголовке.
        запись = {"stage": "request", "path": SWAP_PATH, "client_order_id": client_order_id,
                   "why": why, "mode": "dry-run" if self.dry_run else "live",
                   "body": body}
        if self.dry_run:
            запись["result"] = "dry-run: запрос НЕ отправлен"
            self._log_call(запись)
            return {"ok": True, "dry_run": True, "code": None,
                     "order_id": None, "signatures": [],
                     "why_not": "dry-run: запрос не отправлялся"}
        self._log_call(запись)
        # Время площадки измеряется ЗДЕСЬ, вокруг единственного POST: иначе в
        # "сколько съедает Bloom" попадает и наша сборка тела, и запись в
        # журнал. Владелец спрашивает про площадку -- значит мерить надо
        # площадку.
        t_отправлено = time.time()
        # Сколько НОВЫХ соединений открылось за время этого POST. 0 -- ушёл по
        # тёплому, и это факт из urllib3, а не вывод из миллисекунд.
        сч = счётчик_соединений()
        новых_до = сч.новых if сч is not None else None
        self.занят_покупкой = True
        try:
            r = self.session.post(self.host + SWAP_PATH, headers=self._headers(),
                                   json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            self.занят_покупкой = False
            out = {"ok": False, "code": None, "network": True, "order_id": None,
                    "signatures": [],
                    "bloom_ms": round((time.time() - t_отправлено) * 1000.0, 1),
                    "sent_ts": t_отправлено,
                    "why_not": scrub(f"{type(exc).__name__}: {exc}", self.key)[:300],
                    "new_connections": ((сч.новых - новых_до)
                                         if сч is not None else None),
                    "retry_only_after_chain_check": True}
            if self.state is not None:
                self.state.note_api_result(ok=False, code="СЕТЬ")
            self._log_call({"stage": "response", "client_order_id": client_order_id, **out})
            return out
        self.занят_покупкой = False
        итог = self._parse_swap(r, client_order_id)
        итог["bloom_ms"] = round((time.time() - t_отправлено) * 1000.0, 1)
        итог["sent_ts"] = t_отправлено
        итог["new_connections"] = ((сч.новых - новых_до) if сч is not None else None)
        return итог

    def _parse_swap(self, r, client_order_id: str) -> dict:
        заг = self._note_headers(r.headers)
        out = {"code": r.status_code, "rate_headers": заг, "order_id": None,
                "signatures": []}
        try:
            body = r.json()
        except ValueError:
            out.update(ok=False, why_not=scrub(r.text[:300], self.key),
                        error_code=ERR_NOT_JSON)
            if self.state is not None:
                self.state.note_api_result(ok=False, code=ERR_NOT_JSON)
            self._log_call({"stage": "response", "client_order_id": client_order_id, **out})
            return out
        if r.status_code == 200 and body.get("success"):
            data = body.get("data") or {}
            out.update(ok=True, order_id=data.get("order_id"),
                        signatures=list(data.get("signatures") or []))
            # 200 -- это принято, а не исполнено. Подписи отправленные.
            out["caveat"] = ("200 не означает сделку: подписи отправленные, "
                               "не подтверждённые, и массив может быть пустым")
            if self.state is not None:
                self.state.note_api_result(ok=True)
            self._log_call({"stage": "response", "client_order_id": client_order_id, **out})
            return out
        err = (body.get("error") or {}) if isinstance(body, dict) else {}
        код = err.get("code") or f"HTTP_{r.status_code}"
        out.update(ok=False, error_code=код,
                    why_not=scrub(str(err.get("message") or "")[:300], self.key),
                    details=err.get("details"))
        rl = код == ERR_RATE_LIMITED or r.status_code == 429
        out["rate_limited"] = rl
        if rl:
            out["retry_after_s"] = self._retry_after(r, err)
            out["caveat"] = "отклонённый по лимиту запрос в бюджет не идёт"
        out["skip_not_failure"] = код in ERR_SKIP_NOT_FAILURE
        out["retry_safe_per_docs"] = код in ERR_RETRYABLE
        out["retry_only_after_chain_check"] = True
        if self.state is not None:
            # Пропуск маршрута -- не отказ сервиса: серию ошибок не растит.
            if out["skip_not_failure"]:
                self.state.note_api_result(ok=True)
            else:
                self.state.note_api_result(ok=False, rate_limited=rl, code=код)
        self._log_call({"stage": "response", "client_order_id": client_order_id, **out})
        return out

    @staticmethod
    def _retry_after(r, err: dict) -> float | None:
        for источник in (r.headers.get("Retry-After"),
                          (err.get("details") or {}).get("retry_after")
                          if isinstance(err.get("details"), dict) else None):
            if источник is None:
                continue
            try:
                return float(str(источник).strip())
            except (TypeError, ValueError):
                continue
        return None


def self_test() -> None:
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    src = Path(__file__).read_text(encoding="utf-8")
    тело = src.split("def self_test")[0]
    chk("POST ровно один во всём модуле", тело.count("session.post") == 1,
        str(тело.count("session.post")))
    chk("никаких requests.post мимо сессии", "requests.post" not in тело)
    chk("никаких put/patch/delete", not any(f"session.{m}" in тело
                                             for m in ("put", "patch", "delete")))
    chk("внутри клиента нет цикла повторов POST",
        "for попытка" not in тело and "while" not in тело.split("def swap")[1].split("def ")[0])

    # --- тело покупки
    # Замер времени площадки: он должен быть в ответе и вокруг ОДНОГО POST.
    тело_кл = Path(__file__).read_text(encoding="utf-8").split("def self_test")[0]
    chk("время площадки мерится вокруг единственного POST",
        тело_кл.count("t_отправлено = time.time()") == 1
        and "bloom_ms" in тело_кл, "")
    chk("и в сетевом отказе тоже есть -- отказ бывает медленным",
        тело_кл.count("\"bloom_ms\"") >= 2, тело_кл.count("\"bloom_ms\""))

    order = build_timer_order(seconds=29, slippage=40, priority_fee=0.001,
                               processor_tip=0.001)
    chk("таймерный ордер: тип time", order["target_type"] == "time")
    chk("и значение в секундах числом", order["target_value"] == 29
        and not isinstance(order["target_value"], str))
    chk("продажа 100 % позиции", order["amount"] == 100)
    body = build_buy_body(address="MINT", amount_sol=0.2, slippage_pct=30,
                           priority_fee=0.001, processor_tip=0.001,
                           auto_orders=[order])
    chk("amount покупки -- СТРОКА", body["wallets"][0]["amount"] == "0.2")
    chk("и это абсолютная сумма SOL, а не доля", body["wallets"][0]["amount"] == "0.2")
    chk("auto_tip всегда false", body["auto_tip"] is False)
    chk("auto_orders присутствует явно", "auto_orders" in body)
    validate_swap_body(body)
    chk("правильное тело покупки проходит проверку", True)

    # --- запреты
    def отказ(b, что):
        try:
            validate_swap_body(b)
        except BloomRefusal as exc:
            return что in str(exc), str(exc)
        return False, "проверка НЕ отказала"

    b = dict(body); b.pop("auto_orders")
    ok, msg = отказ(b, "auto_orders")
    chk("тело без auto_orders не уходит", ok, msg)
    ok, msg = отказ(dict(body, auto_orders=None), "null")
    chk("auto_orders=null не уходит", ok, msg)
    ok, msg = отказ(dict(body, skip_if_bought=True), "skip_if_bought")
    chk("skip_if_bought не отправляется", ok, msg)
    ok, msg = отказ(dict(body, quote_asset="SOL"), "quote_asset")
    chk("quote_asset не отправляется", ok, msg)
    ok, msg = отказ(dict(body, chain="solana"), "chain")
    chk("chain (только EVM) не отправляется", ok, msg)
    ok, msg = отказ(dict(body, auto_tip=True), "auto_tip")
    chk("auto_tip=true запрещён: он игнорирует наш processor_tip", ok, msg)
    ok, msg = отказ(dict(body, slippage=0), "slippage")
    chk("нулевое проскальзывание не проходит", ok, msg)
    ok, msg = отказ(dict(body, slippage=150), "slippage")
    chk("проскальзывание больше 100 % не проходит", ok, msg)
    ok, msg = отказ(dict(body, side="buy"), "side")
    chk("side в неверном регистре не проходит", ok, msg)

    чужой = build_buy_body(address="MINT", amount_sol=0.2, slippage_pct=30,
                            priority_fee=0.001, processor_tip=0.001,
                            auto_orders=[order], wallet=FOREIGN_WALLET_W1)
    ok, msg = отказ(чужой, "разрешён только кошелёк исполнителя")
    chk("чужой кошелёк W1 в теле -- отказ", ok, msg)
    chk("и в причине сказано, что это тестовый кошелёк владельца",
        "W1" in msg, msg)

    плохой_ордер = dict(order); плохой_ордер["target_value"] = "29"
    ok, msg = отказ(dict(body, auto_orders=[плохой_ордер]), "JSON number")
    chk("строка вместо числа в авто-ордере -- отказ", ok, msg)
    без_поля = {k: v for k, v in order.items() if k != "priority_fee"}
    ok, msg = отказ(dict(body, auto_orders=[без_поля]), "обязательных полей")
    chk("авто-ордер без priority_fee -- отказ", ok, msg)
    ok, msg = отказ(dict(body, auto_orders=[order] * 21), "пределе")
    chk("больше 20 авто-ордеров -- отказ", ok, msg)
    ok, msg = отказ(dict(body, wallets=[{"address": EXECUTOR_WALLET, "amount": 0.2}]),
                     "строка")
    chk("число вместо строки в amount -- отказ", ok, msg)

    # --- тело продажи
    s = build_sell_body(address="MINT", percent=100, slippage_pct=40,
                         priority_fee=0.001, processor_tip=0.001)
    validate_swap_body(s)
    chk("продажа: auto_orders пустой список", s["auto_orders"] == [])
    chk("продажа: 100 % строкой", s["wallets"][0]["amount"] == "100")
    chk("продажа: сторона Sell", s["side"] == "Sell")
    try:
        build_sell_body(address="M", percent=0, slippage_pct=40, priority_fee=0.001,
                         processor_tip=0.001)
        chk("0 % продажи не проходит", False)
    except BloomRefusal:
        chk("0 % продажи не проходит", True)

    try:
        build_timer_order(seconds=0, slippage=40, priority_fee=0.001, processor_tip=0.001)
        chk("нулевой таймер не проходит", False)
    except BloomRefusal:
        chk("нулевой таймер не проходит", True)
    try:
        build_timer_order(seconds=29, slippage=40, priority_fee=0, processor_tip=0.001)
        chk("нулевой priority_fee в ордере не проходит", False)
    except BloomRefusal:
        chk("нулевой priority_fee в ордере не проходит", True)

    # --- dry-run не ходит в сеть
    import tempfile  # noqa: PLC0415
    st = ExecState(base=Path(tempfile.mkdtemp()) / "s",
                    kill=Path(tempfile.mkdtemp()) / "KILL")

    class СетьЗапрещена:
        def post(self, *a, **k):
            raise AssertionError("dry-run не имеет права ходить в сеть")

        def get(self, *a, **k):
            raise AssertionError("в этом тесте сеть не нужна")

    api = BloomApi("КЛЮЧ", dry_run=True, state=st, session=СетьЗапрещена())
    res = api.swap(body, client_order_id="cid1", why="тест")
    chk("dry-run возвращает ok без сети", res["ok"] is True and res["dry_run"] is True)
    chk("и пишет тело запроса в журнал вызовов", st.api_path.exists())
    журнал = [json.loads(x) for x in st.api_path.read_text().splitlines() if x.strip()]
    chk("в журнале есть полное тело", журнал[0]["body"]["side"] == "Buy")
    chk("и ключа в журнале нет", "КЛЮЧ" not in st.api_path.read_text())

    # --- разбор ответов
    class Ответ:
        def __init__(self, code, payload, headers=None, text=""):
            self.status_code = code
            self._p = payload
            self.headers = headers or {}
            self.text = text

        def json(self):
            if self._p is None:
                raise ValueError("не json")
            return self._p

    api2 = BloomApi("КЛЮЧ", dry_run=False, state=st, session=СетьЗапрещена())
    ok200 = api2._parse_swap(Ответ(200, {"success": True, "data": {
        "order_id": "oid", "signatures": ["SIG1"]}},
        {"X-RateLimit-Remaining-Week": "9000"}), "cid2")
    chk("200 разобран", ok200["ok"] is True and ok200["order_id"] == "oid")
    chk("подписи вынуты", ok200["signatures"] == ["SIG1"])
    chk("и сказано, что 200 не равно сделке", "не означает сделку" in ok200["caveat"])
    chk("остаток недельного лимита сохранён из заголовка",
        st.week_budget_state()["remaining_week"] == 9000)

    e429 = api2._parse_swap(Ответ(429, {"success": False, "error": {
        "code": "RATE_LIMITED", "message": "slow down",
        "details": {"retry_after": 7}}}, {"Retry-After": "5"}), "cid3")
    chk("429 опознан как rate_limited", e429["rate_limited"] is True)
    chk("Retry-After прочитан", e429["retry_after_s"] == 5.0, str(e429.get("retry_after_s")))
    chk("и сказано, что отклонённый запрос в бюджет не идёт",
        "в бюджет не идёт" in e429["caveat"])

    noroute = api2._parse_swap(Ответ(400, {"success": False, "error": {
        "code": "NO_ROUTE", "message": "no route"}}), "cid4")
    chk("NO_ROUTE -- пропуск, а не ошибка", noroute["skip_not_failure"] is True)
    до = st.counters().get("api_error_streak", 0)
    api2._parse_swap(Ответ(400, {"success": False, "error": {"code": "NO_ROUTE"}}), "cid5")
    chk("и серию ошибок API он не растит",
        st.counters().get("api_error_streak", 0) == до, str(st.counters()))

    fatal = api2._parse_swap(Ответ(400, {"success": False, "error": {
        "code": "INVALID_REQUEST", "message": "bad field",
        "details": {"field": "slippage"}}}), "cid6")
    chk("INVALID_REQUEST -- не пропуск", fatal["skip_not_failure"] is False)
    chk("details сохранены, чтобы видеть поле", fatal["details"] == {"field": "slippage"})
    chk("и серия ошибок выросла", st.counters().get("api_error_streak", 0) >= 1)

    внутр = api2._parse_swap(Ответ(500, {"success": False, "error": {
        "code": "INTERNAL_ERROR"}}), "cid7")
    chk("INTERNAL_ERROR помечен как безопасный к повтору по докам",
        внутр["retry_safe_per_docs"] is True)
    chk("но повтор всё равно только после проверки цепи",
        внутр["retry_only_after_chain_check"] is True)

    нежсон = api2._parse_swap(Ответ(200, None, {}, "<html>ошибка</html>"), "cid8")
    chk("ответ не-json не считается успехом", нежсон["ok"] is False)

    chk("ключ вычищается из текста", "СЕКРЕТНЫЙКЛЮЧ" not in
        scrub("упало с ключом СЕКРЕТНЫЙКЛЮЧ внутри", "СЕКРЕТНЫЙКЛЮЧ"))

    # --- счётчик новых соединений: прямое доказательство тёплого пути ---
    сч_т = СчётчикСоединений()

    class ЗаписьЛога:
        def __init__(self, текст):
            self.текст = текст

        def getMessage(self):
            return self.текст

    сч_т.emit(ЗаписьЛога("Starting new HTTPS connection (1): eu.solana.bloombot.app:443"))
    chk("новое соединение посчитано", сч_т.новых == 1, сч_т.новых)
    сч_т.emit(ЗаписьЛога("https://eu.solana.bloombot.app:443 \"POST /api/v1/swap HTTP/1.1\" 200"))
    chk("обычная строка журнала соединением не считается", сч_т.новых == 1, сч_т.новых)
    сч_т.emit(ЗаписьЛога("Starting new HTTP connection (2): example:80"))
    chk("http без s тоже считается", сч_т.новых == 2, сч_т.новых)

    # В ответе swap должно стоять число новых соединений за время POST.
    class СессияСчёт:
        def post(self, *a, **kw):
            class О:
                status_code = 200
                headers = {}

                @staticmethod
                def json():
                    return {"order_id": "1", "signatures": []}
            return О()

    st_сч = ExecState(base=Path(tempfile.mkdtemp()) / "s", kill=Path("/нет"))
    api_сч = BloomApi("КЛЮЧ", dry_run=False, state=st_сч, session=СессияСчёт())
    р_сч = api_сч.swap(body, client_order_id="сч", why="тест")
    chk("в ответе swap стоит число новых соединений за время POST",
        "new_connections" in р_сч and р_сч["new_connections"] == 0, р_сч.get("new_connections"))

    # --- прогрев соединения: считает, не бросает и не ходит мимо клиента ---
    class ПингОтвечает:
        def __init__(self, ответы):
            self.ответы = list(ответы)
            self.вызовов = 0

        def ping(self):
            self.вызовов += 1
            return self.ответы[min(self.вызовов - 1, len(self.ответы) - 1)]

    пр = Прогрев(ПингОтвечает([{"ok": True, "code": 200}]), период_s=20.0)
    chk("прогрев засчитывает успех", пр.круг() is True and пр.успехов == 1)
    ж = пр.признак_жизни()
    chk("прогрев виден в признаке жизни числами",
        ж["ok"] == 1 and ж["failed"] == 0 and ж["period_s"] == 20.0, ж)

    # Пинг и покупка не должны совпасть: пока покупка в полёте, прогрев
    # пропускает круг, иначе пул отдаст покупке второе холодное соединение.
    class КлиентЗанят:
        занят_покупкой = True
        вызовов = 0

        def ping(self):
            КлиентЗанят.вызовов += 1
            return {"ok": True, "code": 200}

    з = КлиентЗанят()
    пр_з = Прогрев(з)
    chk("прогрев молчит, пока покупка в полёте",
        пр_з.круг() is False and КлиентЗанят.вызовов == 0
        and пр_з.признак_жизни()["skipped_while_buying"] == 1,
        пр_з.признак_жизни())
    з.занят_покупкой = False
    chk("и возобновляется, когда покупка ушла",
        пр_з.круг() is True and КлиентЗанят.вызовов == 1)

    # Флаг снимается после POST, иначе прогрев умолкнет навсегда.
    chk("после покупки флаг занятости снят", api_сч.занят_покупкой is False)

    пр2 = Прогрев(ПингОтвечает([{"ok": False, "code": 503, "why_not": "мимо"}]))
    chk("неуспех прогрева посчитан, а не проглочен",
        пр2.круг() is False and пр2.отказов == 1
        and пр2.признак_жизни()["last_code"] == 503, пр2.признак_жизни())

    class ПингПадает:
        def ping(self):
            raise OSError("туннель закрыт")

    пр3 = Прогрев(ПингПадает())
    chk("исключение в прогреве наружу НЕ идёт: это фон, а не решение",
        пр3.круг() is False and "OSError" in пр3.последняя_ошибка,
        пр3.последняя_ошибка)

    # Цикл обязан останавливаться по флагу, иначе служба не выключится.
    пр4 = Прогрев(ПингОтвечает([{"ok": True, "code": 200}]), период_s=0)
    сны = []

    def сон(t):
        сны.append(t)
        пр4.стоп = True

    пр4.цикл(сон=сон)
    chk("цикл прогрева останавливается по флагу стоп",
        пр4.api.вызовов == 1 and сны == [0], (пр4.api.вызовов, сны))

    # Сессия с пулом -- именно сессия, и адаптер к https примонтирован.
    сесс = сессия_с_пулом()
    chk("клиент ходит сессией с пулом, а не голым requests",
        hasattr(сесс, "post") and hasattr(сесс, "adapters")
        and "https://" in getattr(сесс, "adapters", {}), type(сесс).__name__)

    bad = 0
    for n, ok_, got in checks:
        print(f"  [{'ok  ' if ok_ else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok_ else ""))
        bad += (not ok_)
    print(f"самопроверка клиента Bloom: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
    else:
        ap.print_help()
        raise SystemExit(1)
