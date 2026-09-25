#!/usr/bin/env python3
"""Исполнитель Bloom: единственный код, который может КУПИТЬ.

Устройство и почему именно так
------------------------------
Исполнитель вызывается детектором В ТОМ ЖЕ ПРОЦЕССЕ, сразу после решения
о покупке. Отдельная служба, вычитывающая журнал решений, добавила бы к
каждому сигналу задержку опроса -- а весь смысл опыта в слотах: по
замеру решение готово за 0.4 мс медианы и в слоте источника, и отдавать
этот запас процессу-посреднику нельзя. При этом исполнитель -- отдельный
файл со своими самопроверками, и у него есть режим службы, который
добирает из журнала решения, оставшиеся без отправки (путь восстановления
после падения).

Порядок действий на покупке жёсткий и обсуждению не подлежит:
  1. ПОВТОРНАЯ проверка can_open непосредственно перед отправкой -- между
     решением детектора и отправкой могло измениться всё: баланс,
     рубильник, число открытых позиций, 429;
  2. запись намерения на диск с fsync ДО сетевого вызова -- если запрос
     уйдёт и служба упадёт до записи ответа, позиция всё равно будет
     известна, и сторож найдёт токен по цепи и продаст;
  3. один POST через bloom_api, без цикла повторов;
  4. запись ответа, отметка подписи и покупки по минту.

Что исполнитель НЕ делает
-------------------------
* не продаёт -- продажа у сторожа и у таймерного авто-ордера;
* не повторяет запрос сам: 200 у Bloom означает "принято", а не
  "исполнено", и повтор без проверки цепи может купить дважды;
* не трогает чужие кошельки: адрес проверяется в bloom_api и здесь.

Режимы: по умолчанию dry-run. Живые покупки только при BLOOM_LIVE_BUY=1.

Запуск:
  python3 analysis/bloom_executor.py --self-test
  python3 analysis/bloom_executor.py --check-only
  python3 analysis/bloom_executor.py --serve      # добор из журнала
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import re  # noqa: E402

import bloom_api as API  # noqa: E402
import bloom_exec_state as ST  # noqa: E402

try:
    import bloom_notify as NT
except ImportError:  # pragma: no cover
    NT = None

REPO_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("bloom_executor")

DEFAULT_BUY_SLIPPAGE_PCT = ST.env_float("BLOOM_BUY_SLIPPAGE_PCT", 35.0)
DEFAULT_PRIORITY_FEE = ST.env_float("BLOOM_PRIORITY_FEE", 0.001)
DEFAULT_PROCESSOR_TIP = ST.env_float("BLOOM_PROCESSOR_TIP", 0.001)
DEFAULT_SELL_AFTER_S = ST.env_float("BLOOM_SELL_AFTER_S", 28.8)
DEFAULT_SELL_SLIPPAGE_PCT = ST.env_float("BLOOM_SELL_SLIPPAGE_PCT", 40.0)

# Коды причин, по которым исполнитель отказался. Машинные, ASCII.
EXEC_SENT = "SENT"
EXEC_DRY_RUN = "DRY_RUN"
EXEC_GATE = "GATE_CLOSED"
EXEC_REFUSED = "BODY_REFUSED"
EXEC_API_ERROR = "API_ERROR"
EXEC_SKIP_NOT_FAILURE = "SKIP_NOT_FAILURE"
EXEC_RATE_LIMITED = "RATE_LIMITED"
EXEC_NOT_A_BUY = "NOT_A_BUY_DECISION"


# Размер стенда по слову владельца 23.09: 0.001 SOL, при отказе по
# минимуму -- 0.005. Цель прогона -- проверить путь, а не заработать.
DEFAULT_TEST_BUY_SOL = ST.env_float("BLOOM_TEST_BUY_SOL", 0.001)
DEFAULT_TEST_BUY_SOL_BUMP = ST.env_float("BLOOM_TEST_BUY_SOL_BUMP", 0.005)

# Срок авто-ордера 28.8 с -- дробный, и Bloom может не принять его как
# целое число секунд. Тогда РАЗРЕШЕНА одна замена на 29: это тоже отказ
# тела (INVALID_REQUEST), значит в цепь ничего не ушло.
SELL_AFTER_FALLBACK_S = ST.env_float("BLOOM_SELL_AFTER_FALLBACK_S", 29.0)
TARGET_HINT = re.compile(r"target_value|target_type|auto_order|integer|"
                          r"whole|целое|секунд", re.I)
EXEC_SENT_AFTER_TARGET = "SENT_AFTER_TARGET_FIX"
EXEC_SENT_AFTER_ADDRESS = "SENT_AFTER_ADDRESS_FIX"

# Один и только один повтор с УВЕЛИЧЕННОЙ суммой -- и только если запрос
# был отвергнут до отправки. INVALID_REQUEST означает, что Bloom тело не
# принял, то есть в цепь ничего не ушло, и повтор не может купить дважды.
# Повторять на любом другом коде нельзя: 200 у Bloom -- это "принято", и
# слепой повтор способен купить второй раз.
BUMP_SAFE_CODES = ("INVALID_REQUEST",)
# Коды гейта, о которых владельцу сообщается отдельной строкой с пометкой:
# это не рядовой отказ (маленькая сумма, докупка), а остановка торговли.
ГЕЙТ_КОДЫ_ТРЕВОГИ = (ST.КОД_РУБИЛЬНИК, ST.КОД_СЧЁТЧИКИ_БИТЫ, ST.КОД_ПАУЗА_API,
                      ST.КОД_ПАУЗА_НЕПРОДАНО, ST.КОД_ПАУЗА_429)
BUMP_HINT = re.compile(r"min|minimum|too\s*small|мал", re.I)

EXEC_LIVE_TEST_SKIP = "LIVE_TEST_SKIP_REAL_SOURCE"
EXEC_BUMPED = "SENT_AFTER_BUMP"


def live_buy_enabled() -> bool:
    """Живые покупки по РЕАЛЬНЫМ источникам -- только по явному 1.

    Проверка отдельной функцией, чтобы её можно было назвать в отчёте и
    проверить в самопроверке: "по умолчанию не покупаем" -- это свойство,
    а не надежда.
    """
    return (os.environ.get("BLOOM_LIVE_BUY") or "").strip() == "1"


def live_test_enabled() -> bool:
    """Режим стенда: покупки ТОЛЬКО по тестовому источнику."""
    return (os.environ.get("BLOOM_LIVE_TEST") or "").strip() == "1"


def current_mode() -> str:
    """Режим исполнителя. live сильнее live-test, dry-run -- по умолчанию."""
    if live_buy_enabled():
        return ST.MODE_LIVE
    if live_test_enabled():
        return ST.MODE_LIVE_TEST
    return ST.MODE_DRY


class Executor:
    def __init__(self, *, state: ST.ExecState, api: API.BloomApi,
                 buy_sol: float | None = None,
                 slippage_pct: float = DEFAULT_BUY_SLIPPAGE_PCT,
                 priority_fee: float = DEFAULT_PRIORITY_FEE,
                 processor_tip: float = DEFAULT_PROCESSOR_TIP,
                 sell_after_s: float = DEFAULT_SELL_AFTER_S,
                 sell_slippage_pct: float = DEFAULT_SELL_SLIPPAGE_PCT) -> None:
        self.state = state
        self.api = api
        self.mode = current_mode()
        if buy_sol is not None:
            self.buy_sol = buy_sol
        elif self.mode == ST.MODE_LIVE_TEST:
            # На стенде размер свой и маленький: цель -- проверить путь, а
            # не заработать.
            self.buy_sol = DEFAULT_TEST_BUY_SOL
        else:
            self.buy_sol = state.buy_sol
        self.bump_sol = DEFAULT_TEST_BUY_SOL_BUMP
        self.slippage_pct = slippage_pct
        self.priority_fee = priority_fee
        self.processor_tip = processor_tip
        self.sell_after_s = sell_after_s
        self.sell_slippage_pct = sell_slippage_pct
        self.sent = 0
        self.refused = 0
        self.last_error = ""

    # --------------------------------------------------------------- тело

    def build_body(self, mint: str, *, amount_sol: float | None = None,
                   sell_after_s: float | None = None,
                   address: str | None = None) -> dict:
        """Тело покупки с ОБЯЗАТЕЛЬНЫМ таймерным авто-ордером.

        auto_orders непустой -- принципиально: при отсутствии поля Bloom
        подставит сохранённую в кабинете стратегию аккаунта, и мы получим
        чужие условия выхода вместо своих 28.8 с.

        address отличается от mint, когда покупаем ПО ID ПУЛА источника.
        Смысл: по минту маршрут выбирает Bloom, и на п. 1 стенда он выбрал
        двухшаговый через пул без ликвидности в диапазоне, тогда как
        источник шёл одним пулом. Минт при этом остаётся минтом: он нужен
        для учёта позиции, налога и остатка по цепи.
        """
        order = API.build_timer_order(
            seconds=(self.sell_after_s if sell_after_s is None else sell_after_s),
            slippage=self.sell_slippage_pct,
            priority_fee=self.priority_fee, processor_tip=self.processor_tip,
            amount_percent=100)
        return API.build_buy_body(
            address=(address or mint),
            amount_sol=self.buy_sol if amount_sol is None else amount_sol,
            slippage_pct=self.slippage_pct,
            priority_fee=self.priority_fee, processor_tip=self.processor_tip,
            auto_orders=[order])

    # ------------------------------------------------------------ покупка

    def execute(self, decision: dict, *, balance_sol: float | None,
                amount_sol: float | None = None) -> dict:
        """Исполнить решение детектора. Возвращает запись о попытке.

        amount_sol -- размер ИМЕННО ЭТОЙ покупки. Решение владельца 25.09
        (вечер): по 38 кошелькам lane_only Bloom берёт 0.05 SOL, а на BATCH-3/5
        остаётся 0.2. Размер приходит снаружи по группе источника, а не меняет
        self.buy_sol: иначе одна группа молча переписывала бы размер другой.
        """
        out = {"client_order_id": None, "mint": decision.get("mint"),
               "source_sig": decision.get("signature"),
               "exec_code": None, "reason": "", "live": live_buy_enabled(),
               "dry_run": self.api.dry_run}

        if decision.get("action") != "buy":
            out.update(exec_code=EXEC_NOT_A_BUY,
                       reason=f"решение не о покупке: action={decision.get('action')}")
            return out

        mint = decision.get("mint")
        sig = decision.get("signature")
        if not mint or not sig:
            out.update(exec_code=EXEC_NOT_A_BUY,
                       reason="в решении нет минта или подписи источника")
            return out

        out["mode"] = self.mode
        out["test_source"] = bool(decision.get("test_source"))
        # На стенде покупаем ТОЛЬКО по тестовому источнику. Сигналы реальных
        # источников в этом режиме идут исключительно в журнал -- иначе
        # стенд незаметно превратился бы в боевой запуск.
        if self.mode == ST.MODE_LIVE_TEST and not decision.get("test_source"):
            out.update(exec_code=EXEC_LIVE_TEST_SKIP,
                       reason="режим стенда: покупки только по тестовому источнику")
            self.state.log_decision({"stage": "exec_skip_real_source",
                                     "mint": mint, "signature": sig,
                                     "mode": self.mode})
            return out

        # 1. Гейт ПОВТОРНО, непосредственно перед отправкой.
        ok, why, code = self.state.can_open_detailed(
            mint=mint, source_sig=sig, balance_sol=balance_sol)
        if not ok:
            self.refused += 1
            out.update(exec_code=EXEC_GATE, reason=why, gate_code=code)
            # Автопауза и рубильник -- это не рядовой отказ гейта, а стоп
            # торговли: владелец должен узнать сразу, а не из доклада утром.
            if code in ГЕЙТ_КОДЫ_ТРЕВОГИ and NT is not None:
                NT.Оповещатель().послать(NT.строка_тревоги(code, why))
            self.state.log_decision({"stage": "exec_gate", "mint": mint,
                                     "signature": sig, "code": code, "reason": why})
            return out

        # 2. Тело и его проверка ДО записи намерения: отказ в теле -- это не
        #    попытка покупки, и класть о ней намерение в журнал неверно.
        # Чем покупаем: ID пула источника, если он в его транзакции виден,
        # иначе минт. Пул не выдумывается: детектор отдаёт его только когда
        # в транзакции нашёлся владелец хранилищ пары токен/WSOL, и помечает
        # NO_SOL_POOL_IN_TX, когда такого владельца нет (пул к USDC, RFQ,
        # маршрут без видимого пула).
        размер_sol = float(self.buy_sol if amount_sol is None else amount_sol)
        out["buy_sol"] = размер_sol
        адрес = (decision.get("source_pool") or "").strip() or mint
        вид_адреса = "pool" if адрес != mint else "mint"
        out["buy_address"] = адрес
        out["buy_address_kind"] = вид_адреса
        if decision.get("pool_why_not"):
            out["pool_why_not"] = decision.get("pool_why_not")
        try:
            body = self.build_body(mint, address=адрес, amount_sol=размер_sol)
            API.validate_swap_body(body)
        except API.BloomRefusal as exc:
            self.refused += 1
            self.last_error = str(exc)
            out.update(exec_code=EXEC_REFUSED, reason=str(exc))
            self.state.log_decision({"stage": "exec_refused", "mint": mint,
                                     "signature": sig, "reason": str(exc)})
            return out

        # 3. Намерение НА ДИСК до сети.
        cid = self.state.new_client_order_id()
        out["client_order_id"] = cid
        route = decision.get("route") or {}
        self.state.write_intent(
            client_order_id=cid, mint=mint, source_sig=sig,
            source_slot=decision.get("source_slot"), sol_in=размер_sol,
            pool=(адрес if вид_адреса == "pool" else None),
            program=(route.get("programs") or [None])[0],
            taxed=decision.get("taxed"), tax_bps=decision.get("tax_bps"),
            mode=self.mode, sell_after_s=self.sell_after_s)

        # 4. Один POST. Повторов нет.
        res = self.api.swap(body, client_order_id=cid,
                            why=f"копия покупки источника {sig[:12]}")

        # 5. Отметки и разбор ответа.
        self.state.mark_signature(sig, source="executor")
        self.state.mark_mint_buy(mint, mode=self.mode)

        if res.get("ok"):
            self.sent += 1
            self.state.update_position(
                cid, state="bought", order_id=res.get("order_id"),
                signatures=res.get("signatures") or [],
                ts_accepted=time.time(),
                buy_address=адрес, buy_address_kind=вид_адреса,
                bloom_ms=res.get("bloom_ms"), ts_sent=res.get("sent_ts"),
                # Сколько НОВЫХ соединений открылось за время POST:
                # 0 значит ушло по тёплому. Факт из urllib3, не оценка.
                new_connections=res.get("new_connections"),
                caveat=res.get("caveat"))
            out.update(exec_code=(EXEC_DRY_RUN if res.get("dry_run") else EXEC_SENT),
                       order_id=res.get("order_id"),
                       signatures=res.get("signatures") or [],
                       reason=res.get("caveat") or "принято")
            return out

        # Безопасные повторы -- ТОЛЬКО когда Bloom отверг само тело
        # (INVALID_REQUEST). Это значит, что в цепь ничего не ушло, и повтор
        # не может купить дважды. На любом другом коде повтора нет: 200 у
        # Bloom -- это "принято", и слепой повтор способен купить второй раз.
        #
        # Поправок две, каждая применяется не больше одного раза:
        #   * сумма ниже минимума площадки -> поднять до bump_sol;
        #   * срок авто-ордера 28.8 не принят как целое -> заменить на 29.
        # Решение владельца: первый живой вызов идёт с 28.8, и только при
        # INVALID_REQUEST -- 29.
        сумма_тек = размер_sol
        срок_тек = self.sell_after_s
        адрес_тек = адрес
        поднимали = False
        правили_срок = False
        правили_адрес = False
        код_первый = res.get("error_code") or "?"
        текст_первый = str(res.get("why_not") or "")
        for _ in range(3):
            если_можно = (self.mode in (ST.MODE_LIVE_TEST, ST.MODE_LIVE)
                          and (res.get("error_code") or "?") in BUMP_SAFE_CODES)
            if not если_можно:
                break
            текст = str(res.get("why_not") or "")
            код = res.get("error_code") or "?"
            новая_сумма, новый_срок, новый_адрес = сумма_тек, срок_тек, адрес_тек
            что = None
            # Адрес правится ПЕРВЫМ и без разбора текста: принимает ли Bloom
            # ID пула в поле address -- пока не проверено ни одной живой
            # сделкой, а INVALID_REQUEST означает, что в цепь не ушло
            # ничего. Поэтому отказ на пуле -- повтор по минту, и покупка не
            # теряется из-за непроверенной догадки.
            if not правили_адрес and адрес_тек != mint:
                новый_адрес, что = mint, "адрес"
            elif (not поднимали and BUMP_HINT.search(текст)
                    and сумма_тек < self.bump_sol):
                новая_сумма, что = self.bump_sol, "сумма"
            elif (not правили_срок and TARGET_HINT.search(текст)
                    and float(срок_тек) != float(int(срок_тек))):
                новый_срок, что = SELL_AFTER_FALLBACK_S, "срок"
            if что is None:
                break
            было = {"сумма": сумма_тек, "срок": срок_тек, "адрес": адрес_тек}[что]
            стало = {"сумма": новая_сумма, "срок": новый_срок,
                      "адрес": новый_адрес}[что]
            log.warning("Bloom отверг тело (%s: %s) -- одна попытка: %s "
                        "%s -> %s", код, текст[:120], что, было, стало)
            self.state.log_decision({"stage": "exec_retry", "what": что,
                                     "mint": mint, "signature": sig,
                                     "from_sol": сумма_тек, "to_sol": новая_сумма,
                                     "from_sell_after_s": срок_тек,
                                     "to_sell_after_s": новый_срок,
                                     "from_address": адрес_тек,
                                     "to_address": новый_адрес,
                                     "error_code": код,
                                     "why_not": текст[:200]})
            try:
                body2 = self.build_body(mint, amount_sol=новая_сумма,
                                        sell_after_s=новый_срок,
                                        address=новый_адрес)
                API.validate_swap_body(body2)
            except API.BloomRefusal as exc:
                self.refused += 1
                out.update(exec_code=EXEC_REFUSED, reason=str(exc),
                           first_error_code=код_первый)
                self.state.update_position(cid, state=ST.STATE_CLOSED,
                                           close_reason="retry_body_refused",
                                           error_code=код_первый)
                return out
            сумма_тек, срок_тек, адрес_тек = новая_сумма, новый_срок, новый_адрес
            поднимали = поднимали or что == "сумма"
            правили_срок = правили_срок or что == "срок"
            правили_адрес = правили_адрес or что == "адрес"
            res = self.api.swap(body2, client_order_id=cid,
                                why=(f"повтор: {что} {сумма_тек} SOL / "
                                     f"{срок_тек} с после {код}"))
            if res.get("ok"):
                self.sent += 1
                правки = ([n for n, было in (("сумма", поднимали),
                                               ("срок", правили_срок),
                                               ("адрес", правили_адрес)) if было])
                итоговый_код = (EXEC_BUMPED if поднимали else
                                 EXEC_SENT_AFTER_TARGET if правили_срок else
                                 EXEC_SENT_AFTER_ADDRESS)
                self.state.update_position(
                    cid, state="bought", order_id=res.get("order_id"),
                    signatures=res.get("signatures") or [],
                    ts_accepted=time.time(), sol_in=сумма_тек,
                    sell_after_s=срок_тек,
                    bumped_from_sol=(размер_sol if поднимали else None),
                    target_from_s=(self.sell_after_s if правили_срок else None),
                    bloom_ms=res.get("bloom_ms"), ts_sent=res.get("sent_ts"),
                # Сколько НОВЫХ соединений открылось за время POST:
                # 0 значит ушло по тёплому. Факт из urllib3, не оценка.
                new_connections=res.get("new_connections"),
                    pool=(адрес_тек if адрес_тек != mint else None),
                    buy_address=адрес_тек,
                    buy_address_kind=("pool" if адрес_тек != mint else "mint"),
                    address_from=(адрес if правили_адрес else None),
                    fixes=",".join(правки),
                    first_error_code=код_первый,
                    caveat=res.get("caveat"))
                out.update(exec_code=итоговый_код, order_id=res.get("order_id"),
                           signatures=res.get("signatures") or [],
                           sol_in=сумма_тек, sell_after_s=срок_тек,
                           buy_address=адрес_тек,
                           buy_address_kind=("pool" if адрес_тек != mint else "mint"),
                           first_error_code=код_первый,
                           fixes=",".join(правки),
                           reason=res.get("caveat") or f"принято после правки: {что}")
                return out

        # Отказ. Позиция НЕ остаётся в intent навсегда: помечаем её
        # закрытой с причиной, иначе сторож будет вечно искать токен,
        # которого никто не покупал.
        код = res.get("error_code") or "?"
        пропуск = bool(res.get("skip_not_failure"))
        лимит = bool(res.get("rate_limited"))
        self.state.update_position(
            cid, state=ST.STATE_CLOSED, close_reason="api_refused",
            error_code=код, why_not=res.get("why_not"))
        self.refused += 1
        self.last_error = f"{код}: {res.get('why_not')}"
        out.update(exec_code=(EXEC_RATE_LIMITED if лимит else
                              EXEC_SKIP_NOT_FAILURE if пропуск else EXEC_API_ERROR),
                   error_code=код, reason=res.get("why_not") or "",
                   retry_after_s=res.get("retry_after_s"))
        if код_первый != код:
            out["first_error_code"] = код_первый
        return out

    # --------------------------------------------------------------- отчёт

    def report(self) -> dict:
        return {ST.SCHEMA_VERSION_KEY: ST.SCHEMA_VERSION,
                "mode": self.mode,
                "live_buy_enabled": live_buy_enabled(),
                "live_test_enabled": live_test_enabled(),
                "dry_run": self.api.dry_run,
                "bump_sol": self.bump_sol,
                "buy_sol": self.buy_sol,
                "slippage_pct": self.slippage_pct,
                "priority_fee": self.priority_fee,
                "processor_tip": self.processor_tip,
                "sell_after_s": self.sell_after_s,
                "sell_slippage_pct": self.sell_slippage_pct,
                "sent": self.sent, "refused": self.refused,
                "last_error": self.last_error,
                "wallet": ST.EXECUTOR_WALLET}


# ------------------------------------------------------- добор из журнала

def pending_from_journal(state: ST.ExecState) -> list:
    """Решения о покупке, по которым не появилось позиции.

    Путь восстановления: если служба упала между решением и отправкой,
    сигнал не должен потеряться молча. Сопоставление по подписи источника
    -- она уникальна и уже лежит и в решении, и в позиции.
    """
    решения = []
    if state.decisions_path.exists():
        for line in state.decisions_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("action") == "buy" and r.get("signature"):
                решения.append(r)
    с_позицией = {p.get("source_sig") for p in state.positions().values()}
    return [r for r in решения if r["signature"] not in с_позицией]


def self_test() -> int:
    import tempfile  # noqa: PLC0415
    checks = []

    def chk(имя, ок, факт=""):
        checks.append((имя, bool(ок), факт))

    class ОтветЗаглушка:
        def __init__(self, code, body, headers=None):
            self.status_code = code
            self._body = body
            self.headers = headers or {}
            self.text = json.dumps(body)

        def json(self):
            return self._body

    class СессияЗаглушка:
        def __init__(self, ответы):
            self.ответы = list(ответы)
            self.запросы = []

        def post(self, url, **kw):
            self.запросы.append({"url": url, "json": kw.get("json")})
            return self.ответы.pop(0)

    было = os.environ.pop("BLOOM_LIVE_BUY", None)
    try:
        chk("по умолчанию живые покупки выключены", live_buy_enabled() is False)
        os.environ["BLOOM_LIVE_BUY"] = "0"
        chk("нуля недостаточно для живых покупок", live_buy_enabled() is False)
        os.environ["BLOOM_LIVE_BUY"] = "1"
        chk("включается только явной единицей", live_buy_enabled() is True)
    finally:
        os.environ.pop("BLOOM_LIVE_BUY", None)
        if было is not None:
            os.environ["BLOOM_LIVE_BUY"] = было

    решение_buy = {"action": "buy", "mint": "MINT1", "signature": "SIG1",
                   "source_slot": 100, "taxed": True, "tax_bps": 300,
                   "route": {"programs": ["Raydium AMM v4"]}}

    # --- тело покупки
    with tempfile.TemporaryDirectory() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        api = API.BloomApi("КЛЮЧ", dry_run=True, state=st)
        ex = Executor(state=st, api=api)
        body = ex.build_body("MINT1")
        chk("сторона -- покупка", body["side"] == "Buy", body["side"])
        chk("адрес -- минт", body["address"] == "MINT1")
        chk("сумма строкой", isinstance(body["wallets"][0]["amount"], str),
            body["wallets"][0]["amount"])
        chk("кошелёк -- исполнителя",
            body["wallets"][0]["address"] == ST.EXECUTOR_WALLET)
        chk("auto_tip выключен", body["auto_tip"] is False)
        chk("ровно один авто-ордер", len(body["auto_orders"]) == 1, body["auto_orders"])
        o = body["auto_orders"][0]
        chk("авто-ордер таймерный", o["target_type"] == "time", o)
        chk("срок 28.8 с", o["target_value"] == 28.8, o["target_value"])
        chk("продаётся 100 %", o["amount"] == 100, o["amount"])
        chk("числа в ордере -- числа, не строки",
            all(isinstance(o[k], (int, float)) for k in
                ("target_value", "amount", "slippage", "priority_fee")), o)
        API.validate_swap_body(body)   # не должно бросить
        chk("тело проходит проверку клиента", True)

    # --- dry-run: сети нет, но журнал полон
    with tempfile.TemporaryDirectory() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        сессия = СессияЗаглушка([])
        api = API.BloomApi("КЛЮЧ", dry_run=True, state=st, session=сессия)
        ex = Executor(state=st, api=api)
        r = ex.execute(решение_buy, balance_sol=5.0)
        chk("dry-run помечен", r["exec_code"] == EXEC_DRY_RUN, r["exec_code"])
        chk("в dry-run ни одного запроса в сеть", сессия.запросы == [], сессия.запросы)
        поз = st.positions()
        chk("позиция создана", len(поз) == 1, поз)
        одна = list(поз.values())[0]
        chk("позиция в состоянии bought", одна["state"] == "bought", одна["state"])
        chk("минт записан", одна["mint"] == "MINT1")
        chk("подпись источника записана", одна["source_sig"] == "SIG1")
        chk("налог минта перенесён в позицию",
            одна.get("taxed") is True and одна.get("tax_bps") == 300, одна)
        chk("срок продажи записан", одна.get("sell_after_s") == 28.8)
        chk("размер покупки без amount_sol -- свой, из настроек",
            одна.get("sol_in") == ex.buy_sol, (одна.get("sol_in"), ex.buy_sol))

        # РАЗМЕР ПОКУПКИ ПО ГРУППЕ ИСТОЧНИКА -- ЭТО ДЕНЬГИ (решение владельца
        # 25.09 вечером: по lane_only Bloom берёт 0.05, на BATCH-3/5 остаётся
        # 0.2). Размер, потерянный между детектором и телом запроса, купил бы
        # на 0.2 там, где владелец разрешил 0.05.
        сессия2 = СессияЗаглушка([])
        api2 = API.BloomApi("КЛЮЧ", dry_run=True, state=st, session=сессия2)
        ex2 = Executor(state=st, api=api2, buy_sol=0.2)
        r2 = ex2.execute({**решение_buy, "mint": "MINT_РАЗМЕР",
                           "signature": "SIG_РАЗМЕР"},
                          balance_sol=5.0, amount_sol=0.05)
        поз2 = [п for п in st.positions().values() if п.get("mint") == "MINT_РАЗМЕР"]
        chk("переданный размер записан в позицию, а не свой 0.2",
            r2.get("buy_sol") == 0.05 and поз2 and поз2[0].get("sol_in") == 0.05,
            (r2.get("buy_sol"), поз2[0].get("sol_in") if поз2 else None))
        # Сумма в теле Bloom -- СТРОКА (так требует их клиент), поэтому
        # сравнение числом, а не текстом: "0.05" и 0.05 это одно и то же число.
        chk("и в теле запроса стоит он же",
            float(ex2.build_body("MINT_РАЗМЕР", amount_sol=0.05)["wallets"][0]
                   ["amount"]) == 0.05,
            ex2.build_body("MINT_РАЗМЕР", amount_sol=0.05)["wallets"][0]["amount"])
        chk("свой размер исполнителя при этом не изменился",
            ex2.buy_sol == 0.2, ex2.buy_sol)
        chk("подпись помечена виденной", st.seen_signature("SIG1"))
        _, покупок = st.mint_state("MINT1")
        chk("в dry-run покупка по минту НЕ учитывается: иначе придуманная "
            "покупка закроет минт для настоящей", покупок == 0, покупок)
        chk("режим позиции -- dry-run", одна.get("mode") == ST.MODE_DRY, одна.get("mode"))
        chk("dry-run позиция не считается открытой",
            st.open_positions() == [], st.open_positions())
        # Позиций dry-run теперь две: своя и та, что проверяет размер по группе.
        chk("но видна в разделе dry-run", len(st.dry_positions()) == 2,
            len(st.dry_positions()))
        chk("все ключи позиции латинские",
            all(k.isascii() for k in одна), [k for k in одна if not k.isascii()])

    # --- гейт закрыт: ни намерения, ни запроса
    with tempfile.TemporaryDirectory() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        st.kill_path.write_text("стоп", encoding="utf-8")
        сессия = СессияЗаглушка([])
        api = API.BloomApi("КЛЮЧ", dry_run=True, state=st, session=сессия)
        ex = Executor(state=st, api=api)
        r = ex.execute(решение_buy, balance_sol=5.0)
        chk("рубильник закрывает гейт", r["exec_code"] == EXEC_GATE, r["exec_code"])
        chk("код гейта назван", r.get("gate_code") == ST.КОД_РУБИЛЬНИК, r.get("gate_code"))
        chk("намерение НЕ записано", st.positions() == {}, st.positions())
        chk("подпись НЕ помечена виденной", not st.seen_signature("SIG1"))
        chk("в сеть ничего не ушло", сессия.запросы == [])

    # --- баланса нет: тот же отказ, но по своей причине
    with tempfile.TemporaryDirectory() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        api = API.BloomApi("КЛЮЧ", dry_run=True, state=st, session=СессияЗаглушка([]))
        ex = Executor(state=st, api=api)
        r = ex.execute(решение_buy, balance_sol=None)
        chk("неизвестный баланс закрывает гейт", r["exec_code"] == EXEC_GATE)
        chk("и код именно про баланс", r.get("gate_code") == ST.КОД_БАЛАНС,
            r.get("gate_code"))

    # --- живой отказ API: позиция закрывается, а не висит в intent
    with tempfile.TemporaryDirectory() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        отв = ОтветЗаглушка(400, {"success": False,
                                  "error": {"code": "NO_ROUTE", "message": "нет маршрута"}})
        сессия = СессияЗаглушка([отв])
        api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
        ex = Executor(state=st, api=api)
        r = ex.execute(решение_buy, balance_sol=5.0)
        chk("отсутствие маршрута -- пропуск, а не сбой",
            r["exec_code"] == EXEC_SKIP_NOT_FAILURE, r["exec_code"])
        chk("код ошибки назван", r.get("error_code") == "NO_ROUTE", r.get("error_code"))
        одна = list(st.positions().values())[0]
        chk("позиция закрыта, а не висит в intent",
            одна["state"] == ST.STATE_CLOSED, одна["state"])
        chk("причина закрытия записана",
            одна.get("close_reason") == "api_refused", одна.get("close_reason"))
        chk("запрос был ровно один", len(сессия.запросы) == 1, len(сессия.запросы))
        c = st.counters()
        chk("пропуск маршрута не растит серию ошибок",
            int(c.get("api_error_streak", 0)) == 0, c)

    # --- 429: отдельный код и время ожидания
    with tempfile.TemporaryDirectory() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        отв = ОтветЗаглушка(429, {"success": False,
                                  "error": {"code": "RATE_LIMITED", "message": "слишком часто",
                                            "details": {"retry_after": 7}}},
                            {"Retry-After": "7"})
        api = API.BloomApi("КЛЮЧ", dry_run=False, state=st,
                           session=СессияЗаглушка([отв]))
        ex = Executor(state=st, api=api)
        r = ex.execute(решение_buy, balance_sol=5.0)
        chk("429 -- свой код", r["exec_code"] == EXEC_RATE_LIMITED, r["exec_code"])
        chk("время ожидания перенесено", r.get("retry_after_s") == 7.0,
            r.get("retry_after_s"))
        c = st.counters()
        chk("429 отмечен в счётчиках", c.get("rate_limited_since"), c)

    # --- успех живьём
    with tempfile.TemporaryDirectory() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        отв = ОтветЗаглушка(200, {"success": True,
                                  "data": {"order_id": "o1", "signatures": ["s1"]}})
        сессия = СессияЗаглушка([отв])
        api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
        ex = Executor(state=st, api=api)
        r = ex.execute(решение_buy, balance_sol=5.0)
        chk("успех помечен как отправлено", r["exec_code"] == EXEC_SENT, r["exec_code"])
        chk("order_id перенесён", r.get("order_id") == "o1")
        chk("оговорка про 200 не потеряна", "не означает сделку" in (r.get("reason") or ""),
            r.get("reason"))
        одна = list(st.positions().values())[0]
        chk("позиция bought с подписями",
            одна["state"] == "bought" and одна.get("signatures") == ["s1"], одна)
        chk("время принятия записано", одна.get("ts_accepted"), одна)
        тело = сессия.запросы[0]["json"]
        chk("в сеть ушло тело с одним авто-ордером",
            len(тело["auto_orders"]) == 1, тело.get("auto_orders"))
        chk("ключ в тело не попал",
            not any("КЛЮЧ" in str(v) for v in тело.values()), тело)

    # --- не-покупка отвергается до всего остального
    with tempfile.TemporaryDirectory() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        сессия = СессияЗаглушка([])
        api = API.BloomApi("КЛЮЧ", dry_run=True, state=st, session=сессия)
        ex = Executor(state=st, api=api)
        r = ex.execute({"action": "skip", "mint": "M", "signature": "S"},
                       balance_sol=5.0)
        chk("решение-пропуск не исполняется", r["exec_code"] == EXEC_NOT_A_BUY)
        chk("и позиции не создаёт", st.positions() == {})
        r2 = ex.execute({"action": "buy", "mint": None, "signature": "S"},
                        balance_sol=5.0)
        chk("покупка без минта отвергается", r2["exec_code"] == EXEC_NOT_A_BUY)

    # --- тревожные коды гейта уходят строкой, рядовые -- нет
    import bloom_notify as NT_T  # noqa: PLC0415
    было_вкл = os.environ.get("BLOOM_TELEGRAM_LIVE_TEST")
    os.environ["BLOOM_TELEGRAM_LIVE_TEST"] = "1"
    посланное = []
    старый_класс = NT_T.Оповещатель
    try:
        class ЛовушкаОповещений(старый_класс):
            def послать(self_, текст):
                посланное.append(текст)
                return {"ok": True}

        NT_T.Оповещатель = ЛовушкаОповещений
        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            (Path(d) / "k").write_text("стоп", encoding="utf-8")
            сессия = СессияЗаглушка([])
            api = API.BloomApi("КЛЮЧ", dry_run=True, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            r = ex.execute(решение_buy, balance_sol=5.0)
            chk("рубильник остановил покупку", r["exec_code"] == EXEC_GATE, r)
            chk("и о нём ушла строка с пометкой",
                посланное and "⚠️" in посланное[0] and "KILL_SWITCH" in посланное[0],
                посланное)
        посланное.clear()
        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            сессия = СессияЗаглушка([])
            api = API.BloomApi("КЛЮЧ", dry_run=True, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            ex.execute(решение_buy, balance_sol=0.0001)
            chk("про нехватку баланса тревожной строки НЕ шлём: это рядовой отказ",
                посланное == [], посланное)
    finally:
        NT_T.Оповещатель = старый_класс
        if было_вкл is None:
            os.environ.pop("BLOOM_TELEGRAM_LIVE_TEST", None)
        else:
            os.environ["BLOOM_TELEGRAM_LIVE_TEST"] = было_вкл

    # --- добор из журнала
    with tempfile.TemporaryDirectory() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        st.log_decision({"action": "buy", "mint": "M1", "signature": "S1"})
        st.log_decision({"action": "skip", "mint": "M2", "signature": "S2"})
        st.log_decision({"action": "buy", "mint": "M3", "signature": "S3"})
        chk("к добору только покупки",
            [r["signature"] for r in pending_from_journal(st)] == ["S1", "S3"],
            [r["signature"] for r in pending_from_journal(st)])
        st.write_intent(client_order_id="c", mint="M1", source_sig="S1",
                        source_slot=1, sol_in=0.2, pool=None, program=None,
                        taxed=None, tax_bps=None, mode="dry", sell_after_s=28.8)
        chk("уже исполненное из добора уходит",
            [r["signature"] for r in pending_from_journal(st)] == ["S3"],
            [r["signature"] for r in pending_from_journal(st)])

    # --- режим стенда live-test
    было_lt = os.environ.pop("BLOOM_LIVE_TEST", None)
    os.environ["BLOOM_LIVE_TEST"] = "1"
    try:
        chk("режим стенда распознан", current_mode() == ST.MODE_LIVE_TEST, current_mode())
        chk("и это НЕ боевой режим", live_buy_enabled() is False)

        # реальный источник в режиме стенда -- только журнал
        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            сессия = СессияЗаглушка([])
            api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            chk("на стенде размер входа свой", ex.buy_sol == DEFAULT_TEST_BUY_SOL,
                ex.buy_sol)
            r = ex.execute({**решение_buy, "test_source": False}, balance_sol=5.0)
            chk("реальный источник на стенде не покупается",
                r["exec_code"] == EXEC_LIVE_TEST_SKIP, r["exec_code"])
            chk("и в сеть ничего не ушло", сессия.запросы == [], сессия.запросы)
            chk("и позиции не создано", st.positions() == {}, st.positions())

        # покупка ПО ID ПУЛА источника и откат к минту
        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            отв = ОтветЗаглушка(200, {"success": True,
                                      "data": {"order_id": "op", "signatures": ["sp1"]}})
            сессия = СессияЗаглушка([отв])
            api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            r = ex.execute({**решение_buy, "test_source": True,
                            "source_pool": "POOLSRC"}, balance_sol=5.0)
            chk("покупка уходит по ID пула источника",
                сессия.запросы[0]["json"]["address"] == "POOLSRC",
                сессия.запросы[0]["json"]["address"])
            chk("и это видно в записи попытки", r.get("buy_address_kind") == "pool", r)
            одна = list(st.positions().values())[0]
            chk("пул записан в позицию", одна.get("pool") == "POOLSRC", одна.get("pool"))
            chk("а минт остался минтом -- по нему учёт и остаток",
                одна.get("mint") == "MINT1", одна.get("mint"))

        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            отказ = ОтветЗаглушка(400, {"error": {"code": "INVALID_REQUEST",
                                                  "message": "address is not a token"}})
            удача = ОтветЗаглушка(200, {"success": True,
                                        "data": {"order_id": "om", "signatures": ["sm1"]}})
            сессия = СессияЗаглушка([отказ, удача])
            api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            r = ex.execute({**решение_buy, "test_source": True,
                            "source_pool": "POOLSRC"}, balance_sol=5.0)
            chk("Bloom не принял пул -- повтор по минту, покупка не потеряна",
                r["exec_code"] == EXEC_SENT_AFTER_ADDRESS, r["exec_code"])
            chk("и код честно называет правку адресом, а не срока",
                r.get("fixes") == "адрес", r.get("fixes"))
            chk("второе тело ушло с минтом",
                сессия.запросы[1]["json"]["address"] == "MINT1",
                сессия.запросы[1]["json"]["address"])
            chk("и всего два запроса, не больше", len(сессия.запросы) == 2,
                len(сессия.запросы))
            одна = list(st.positions().values())[0]
            chk("в позиции видно, что адрес правился",
                одна.get("address_from") == "POOLSRC"
                and одна.get("buy_address_kind") == "mint", одна)

        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            отв = ОтветЗаглушка(200, {"success": True,
                                      "data": {"order_id": "on", "signatures": ["sn1"]}})
            сессия = СессияЗаглушка([отв])
            api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            r = ex.execute({**решение_buy, "test_source": True,
                            "pool_why_not": "пул к USDC, к WSOL пула нет"},
                           balance_sol=5.0)
            chk("пула нет -- покупаем по минту",
                сессия.запросы[0]["json"]["address"] == "MINT1"
                and r.get("buy_address_kind") == "mint", r)
            chk("и причина отсутствия пула в записи попытки",
                "USDC" in str(r.get("pool_why_not")), r.get("pool_why_not"))

        # тестовый источник -- покупается по-настоящему
        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            отв = ОтветЗаглушка(200, {"success": True,
                                      "data": {"order_id": "ot", "signatures": ["st1"]}})
            сессия = СессияЗаглушка([отв])
            api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            r = ex.execute({**решение_buy, "test_source": True}, balance_sol=5.0)
            chk("тестовый источник на стенде покупается",
                r["exec_code"] == EXEC_SENT, r["exec_code"])
            тело = сессия.запросы[0]["json"]
            chk("сумма на стенде -- из настройки стенда",
                тело["wallets"][0]["amount"] == f"{DEFAULT_TEST_BUY_SOL}",
                тело["wallets"][0]["amount"])
            одна = list(st.positions().values())[0]
            chk("режим позиции -- live-test",
                одна.get("mode") == ST.MODE_LIVE_TEST, одна.get("mode"))
            chk("позиция стенда считается открытой: её надо сторожить",
                len(st.open_positions()) == 1, st.open_positions())
            _, покупок = st.mint_state("MINT1")
            chk("и лимит покупок по минту она занимает", покупок == 1, покупок)

        # отказ по минимуму -> ОДИН повтор с 0.02
        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            отказ = ОтветЗаглушка(400, {"success": False, "error": {
                "code": "INVALID_REQUEST", "message": "amount below minimum"}})
            успех = ОтветЗаглушка(200, {"success": True,
                                        "data": {"order_id": "o2", "signatures": ["s2"]}})
            сессия = СессияЗаглушка([отказ, успех])
            api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            r = ex.execute({**решение_buy, "test_source": True}, balance_sol=5.0)
            chk("после отказа по минимуму сумма поднята и покупка прошла",
                r["exec_code"] == EXEC_BUMPED, r["exec_code"])
            chk("первый код ошибки сохранён для доклада",
                r.get("first_error_code") == "INVALID_REQUEST", r.get("first_error_code"))
            chk("запросов ровно два, не больше", len(сессия.запросы) == 2,
                len(сессия.запросы))
            chk("второй запрос на поднятую сумму",
                сессия.запросы[1]["json"]["wallets"][0]["amount"]
                == f"{DEFAULT_TEST_BUY_SOL_BUMP}",
                сессия.запросы[1]["json"]["wallets"][0]["amount"])
            одна = list(st.positions().values())[0]
            chk("в позиции записана поднятая сумма",
                одна.get("sol_in") == DEFAULT_TEST_BUY_SOL_BUMP, одна.get("sol_in"))
            chk("и то, с чего подняли",
                одна.get("bumped_from_sol") == DEFAULT_TEST_BUY_SOL,
                одна.get("bumped_from_sol"))

        # отказ по сроку авто-ордера -> ОДНА замена 28.8 -> 29
        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            отказ = ОтветЗаглушка(400, {"success": False, "error": {
                "code": "INVALID_REQUEST",
                "message": "target_value must be an integer number of seconds"}})
            успех = ОтветЗаглушка(200, {"success": True,
                                        "data": {"order_id": "o3", "signatures": ["s3"]}})
            сессия = СессияЗаглушка([отказ, успех])
            api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            r = ex.execute({**решение_buy, "test_source": True}, balance_sol=5.0)
            chk("после отказа по сроку срок заменён и покупка прошла",
                r["exec_code"] == EXEC_SENT_AFTER_TARGET, r["exec_code"])
            chk("запросов ровно два", len(сессия.запросы) == 2, len(сессия.запросы))
            chk("первый запрос шёл с 28.8",
                сессия.запросы[0]["json"]["auto_orders"][0]["target_value"] == 28.8,
                сессия.запросы[0]["json"]["auto_orders"][0]["target_value"])
            chk("второй запрос -- с 29",
                сессия.запросы[1]["json"]["auto_orders"][0]["target_value"]
                == SELL_AFTER_FALLBACK_S,
                сессия.запросы[1]["json"]["auto_orders"][0]["target_value"])
            chk("сумма при правке срока НЕ менялась",
                сессия.запросы[1]["json"]["wallets"][0]["amount"]
                == f"{DEFAULT_TEST_BUY_SOL}",
                сессия.запросы[1]["json"]["wallets"][0]["amount"])
            одна = list(st.positions().values())[0]
            chk("в позиции записан новый срок",
                одна.get("sell_after_s") == SELL_AFTER_FALLBACK_S,
                одна.get("sell_after_s"))
            chk("и то, с чего срок правили",
                одна.get("target_from_s") == DEFAULT_SELL_AFTER_S,
                одна.get("target_from_s"))

        # обе поправки подряд: сначала минимум, потом срок -- и не больше
        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            о1 = ОтветЗаглушка(400, {"success": False, "error": {
                "code": "INVALID_REQUEST", "message": "amount below minimum"}})
            о2 = ОтветЗаглушка(400, {"success": False, "error": {
                "code": "INVALID_REQUEST", "message": "target_value integer required"}})
            успех = ОтветЗаглушка(200, {"success": True,
                                        "data": {"order_id": "o4", "signatures": ["s4"]}})
            сессия = СессияЗаглушка([о1, о2, успех])
            api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            r = ex.execute({**решение_buy, "test_source": True}, balance_sol=5.0)
            chk("две поправки подряд доводят до принятия",
                r["exec_code"] == EXEC_BUMPED, r["exec_code"])
            chk("запросов ровно три, не больше", len(сессия.запросы) == 3,
                len(сессия.запросы))
            тело = сессия.запросы[2]["json"]
            chk("в третьем запросе и сумма поднята, и срок целый",
                тело["wallets"][0]["amount"] == f"{DEFAULT_TEST_BUY_SOL_BUMP}"
                and тело["auto_orders"][0]["target_value"] == SELL_AFTER_FALLBACK_S,
                (тело["wallets"][0]["amount"],
                 тело["auto_orders"][0]["target_value"]))

        # третий отказ подряд повтора уже не даёт
        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            о = ОтветЗаглушка(400, {"success": False, "error": {
                "code": "INVALID_REQUEST", "message": "amount below minimum"}})
            о2 = ОтветЗаглушка(400, {"success": False, "error": {
                "code": "INVALID_REQUEST", "message": "target_value integer required"}})
            о3 = ОтветЗаглушка(400, {"success": False, "error": {
                "code": "INVALID_REQUEST", "message": "amount below minimum again"}})
            сессия = СессияЗаглушка([о, о2, о3])
            api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            r = ex.execute({**решение_buy, "test_source": True}, balance_sol=5.0)
            chk("после двух поправок третьей попытки нет",
                len(сессия.запросы) == 3, len(сессия.запросы))
            chk("итог -- отказ, а не молчание",
                r["exec_code"] in (EXEC_API_ERROR, EXEC_REFUSED), r["exec_code"])

        # НЕ поднимаем на других кодах: 200 у Bloom -- это "принято"
        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            отказ = ОтветЗаглушка(500, {"success": False, "error": {
                "code": "INTERNAL_ERROR", "message": "minimum something"}})
            сессия = СессияЗаглушка([отказ])
            api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            r = ex.execute({**решение_buy, "test_source": True}, balance_sol=5.0)
            chk("на INTERNAL_ERROR повтора нет даже со словом minimum",
                len(сессия.запросы) == 1, len(сессия.запросы))
            chk("и это отмечено как ошибка API",
                r["exec_code"] == EXEC_API_ERROR, r["exec_code"])

        # отказ по минимуму БЕЗ упоминания минимума -- тоже без повтора
        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
            отказ = ОтветЗаглушка(400, {"success": False, "error": {
                "code": "INVALID_REQUEST", "message": "wallet not owned"}})
            сессия = СессияЗаглушка([отказ])
            api = API.BloomApi("КЛЮЧ", dry_run=False, state=st, session=сессия)
            ex = Executor(state=st, api=api)
            ex.execute({**решение_buy, "test_source": True}, balance_sol=5.0)
            chk("INVALID_REQUEST не про минимум повтора не вызывает",
                len(сессия.запросы) == 1, len(сессия.запросы))
    finally:
        os.environ.pop("BLOOM_LIVE_TEST", None)
        if было_lt is not None:
            os.environ["BLOOM_LIVE_TEST"] = было_lt

    # --- отчёт
    with tempfile.TemporaryDirectory() as d:
        st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        ex = Executor(state=st, api=API.BloomApi("КЛЮЧ", dry_run=True, state=st))
        rep = ex.report()
        chk("в отчёте есть версия формата",
            rep.get(ST.SCHEMA_VERSION_KEY) == ST.SCHEMA_VERSION)
        chk("в отчёте видно, что живые покупки выключены",
            rep["live_buy_enabled"] is False)
        chk("все ключи отчёта латинские",
            all(k.isascii() for k in rep), [k for k in rep if not k.isascii()])

    прошло = sum(1 for _, ок, _ in checks if ок)
    for имя, ок, факт in checks:
        print(f"  [{'ok  ' if ок else 'ПЛОХО'}] {имя}" + (f" -- {факт}" if not ок else ""))
    print(f"самопроверка исполнителя: {прошло}/{len(checks)} пройдено")
    return 0 if прошло == len(checks) else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--serve", action="store_true", help="добор решений из журнала")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.self_test:
        return self_test()

    state = ST.ExecState()
    api = API.BloomApi(os.environ.get("BLOOM_API_KEY", ""),
                       dry_run=(current_mode() == ST.MODE_DRY), state=state)
    ex = Executor(state=state, api=api)

    if a.check_only:
        print(json.dumps({**ex.report(),
                          "pending_in_journal": len(pending_from_journal(state)),
                          "state": state.report()},
                         ensure_ascii=False, indent=2))
        return 0

    if a.serve:
        к_добору = pending_from_journal(state)
        log.info("к добору из журнала: %d", len(к_добору))
        for r in к_добору:
            итог = ex.execute(r, balance_sol=None)   # баланс неизвестен -> гейт
            log.info("добор %s: %s", (r.get("signature") or "")[:12], итог["exec_code"])
        print(json.dumps(ex.report(), ensure_ascii=False, indent=2))
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
