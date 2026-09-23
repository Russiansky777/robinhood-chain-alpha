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

import bloom_api as API  # noqa: E402
import bloom_exec_state as ST  # noqa: E402

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


def live_buy_enabled() -> bool:
    """Живые покупки -- только по явному 1 в окружении.

    Проверка отдельной функцией, чтобы её можно было назвать в отчёте и
    проверить в самопроверке: "по умолчанию не покупаем" -- это свойство,
    а не надежда.
    """
    return (os.environ.get("BLOOM_LIVE_BUY") or "").strip() == "1"


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
        self.buy_sol = buy_sol if buy_sol is not None else state.buy_sol
        self.slippage_pct = slippage_pct
        self.priority_fee = priority_fee
        self.processor_tip = processor_tip
        self.sell_after_s = sell_after_s
        self.sell_slippage_pct = sell_slippage_pct
        self.sent = 0
        self.refused = 0
        self.last_error = ""

    # --------------------------------------------------------------- тело

    def build_body(self, mint: str) -> dict:
        """Тело покупки с ОБЯЗАТЕЛЬНЫМ таймерным авто-ордером.

        auto_orders непустой -- принципиально: при отсутствии поля Bloom
        подставит сохранённую в кабинете стратегию аккаунта, и мы получим
        чужие условия выхода вместо своих 28.8 с.
        """
        order = API.build_timer_order(
            seconds=self.sell_after_s, slippage=self.sell_slippage_pct,
            priority_fee=self.priority_fee, processor_tip=self.processor_tip,
            amount_percent=100)
        return API.build_buy_body(
            address=mint, amount_sol=self.buy_sol, slippage_pct=self.slippage_pct,
            priority_fee=self.priority_fee, processor_tip=self.processor_tip,
            auto_orders=[order])

    # ------------------------------------------------------------ покупка

    def execute(self, decision: dict, *, balance_sol: float | None) -> dict:
        """Исполнить решение детектора. Возвращает запись о попытке."""
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

        # 1. Гейт ПОВТОРНО, непосредственно перед отправкой.
        ok, why, code = self.state.can_open_detailed(
            mint=mint, source_sig=sig, balance_sol=balance_sol)
        if not ok:
            self.refused += 1
            out.update(exec_code=EXEC_GATE, reason=why, gate_code=code)
            self.state.log_decision({"stage": "exec_gate", "mint": mint,
                                     "signature": sig, "code": code, "reason": why})
            return out

        # 2. Тело и его проверка ДО записи намерения: отказ в теле -- это не
        #    попытка покупки, и класть о ней намерение в журнал неверно.
        try:
            body = self.build_body(mint)
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
            source_slot=decision.get("source_slot"), sol_in=self.buy_sol,
            pool=None, program=(route.get("programs") or [None])[0],
            taxed=decision.get("taxed"), tax_bps=decision.get("tax_bps"),
            mode=("live" if not self.api.dry_run else "dry"),
            sell_after_s=self.sell_after_s)

        # 4. Один POST. Повторов нет.
        res = self.api.swap(body, client_order_id=cid,
                            why=f"копия покупки источника {sig[:12]}")

        # 5. Отметки и разбор ответа.
        self.state.mark_signature(sig, source="executor")
        self.state.mark_mint_buy(mint)

        if res.get("ok"):
            self.sent += 1
            self.state.update_position(
                cid, state="bought", order_id=res.get("order_id"),
                signatures=res.get("signatures") or [],
                ts_accepted=time.time(),
                caveat=res.get("caveat"))
            out.update(exec_code=(EXEC_DRY_RUN if res.get("dry_run") else EXEC_SENT),
                       order_id=res.get("order_id"),
                       signatures=res.get("signatures") or [],
                       reason=res.get("caveat") or "принято")
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
        return out

    # --------------------------------------------------------------- отчёт

    def report(self) -> dict:
        return {ST.SCHEMA_VERSION_KEY: ST.SCHEMA_VERSION,
                "live_buy_enabled": live_buy_enabled(),
                "dry_run": self.api.dry_run,
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
        chk("подпись помечена виденной", st.seen_signature("SIG1"))
        _, покупок = st.mint_state("MINT1")
        chk("покупка по минту учтена", покупок == 1, покупок)
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
                       dry_run=not live_buy_enabled(), state=state)
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
