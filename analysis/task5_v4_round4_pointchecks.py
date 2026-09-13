#!/usr/bin/env python3
"""Задача 5, живой пилот -- ТОЧЕЧНЫЕ ЛОКАЛЬНЫЕ ПРОВЕРКИ четвёртого раунда
внешнего ревью (владелец, 2026-09-13): "используй локальные проверки
ПОЛНОГО затронутого пути с подменой сетевых ответов -- не ограничивайся
прямым вызовом gate или формулы, именно поэтому предыдущие тесты
пропустили остановку между gate и send, и изменение комиссии между двумя
prepare".

Проверяются ИМЕННО перечисленные владельцем воспроизведения:
  1. Работа фона при ПОСТОЯННОМ опросе (детектор отмечает активность
     каждые 1.5с) -- фон обязан гарантированно продвигаться.
  2. Два пула одной пары, обнаруженные в РАЗНЫХ вызовах -- вместе
     образуют цикл, по отдельности НЕ теряются.
  3. Nonce после деплоя/рестарта -- безопасная резинхронизация, когда
     это объяснимо, halt -- когда нет.
  4. STOP, наступивший ПОСЛЕ первого гейта (во время чтения баланса), но
     ДО сетевой отправки -- submit_prepared НЕ вызывается.
  5. Завершение пилота с НЕРАЗРЕШЁННОЙ pending-попыткой -- главный цикл
     не виснет (evaluator_finished_current_work не требует pending is None).
  6. Изменение комиссии МЕЖДУ двумя prepare -- согласованный minProfit
     покрывает ИМЕННО итоговую (последнюю) комиссию, не первую.
  7. Повторная запись строки попытки (тот же tx_hash) -- НЕ создаёт
     дубликат.

ВАЖНО: НИКАКОЙ реальной сети, НИКАКИХ реальных денег -- все сетевые
функции ЗАМЕНЯЮТСЯ (monkeypatch) на время каждой проверки и
восстанавливаются сразу после. Тесты ЗАПУСКАЮТ РЕАЛЬНЫЙ, непеределанный
код (HotPath._evaluate_and_maybe_send, RouteRegistry.discover_new_
arbitrageur_routes, verify_nonce_consistency, AttemptTable.write,
_RpcPriorityHint), а НЕ переизобретают его логику в тесте."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

import task5_v4_hotpath as hp  # noqa: E402
import task5_v4_route_registry as rr  # noqa: E402
from task5_bot_sender import PreparedTx, SendResult  # noqa: E402
from task5_v4_pilot_accounting import AttemptTable, AttemptTableRow, PilotBudget, ReasonLog  # noqa: E402

_TEST_PRIVKEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # общеизвестный тестовый ключ Anvil #0
_TEST_ACCOUNT = Account.from_key(_TEST_PRIVKEY)

RESULTS: list[dict] = []


def _record(point: str, title: str, ok: bool, detail: str) -> None:
    RESULTS.append({"point": point, "title": title, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {point}: {title}\n        {detail}")


def _tmp_paths(tag: str) -> dict:
    d = Path(tempfile.mkdtemp(prefix=f"task5_v4_r4_{tag}_"))
    return {"budget": str(d / "budget.json"), "attempts": str(d / "attempts.jsonl"),
            "reasons": str(d / "reasons.jsonl"), "stop": str(d / "STOP")}


class _FakeSender:
    """Тот же публичный контракт, что task5_bot_sender.Sender, БЕЗ
    сети -- РЕАЛЬНАЯ подпись (eth_account), фейковые RPC-зависимые части
    управляются явно тестом."""

    def __init__(self, start_nonce: int = 5) -> None:
        self.address = _TEST_ACCOUNT.address
        self.state_nonce = start_nonce
        self.onchain_pending = start_nonce
        self.onchain_latest = start_nonce
        self.prepare_calls: list[dict] = []
        self.submit_calls = 0
        self._next_max_fee_sequence: list[int] = [2_000_000_000]
        self._fee_call_idx = 0
        self.submit_result: SendResult | None = None

    def can_send(self):
        return True, ""

    def prepare_transaction_fields(self, to, calldata, gas_limit, value_wei=0):
        fee = self._next_max_fee_sequence[min(self._fee_call_idx, len(self._next_max_fee_sequence) - 1)]
        self._fee_call_idx += 1
        tx = {"type": 2, "chainId": 4663, "nonce": self.state_nonce, "to": Web3.to_checksum_address(to),
              "value": value_wei, "gas": gas_limit, "maxFeePerGas": fee, "maxPriorityFeePerGas": 100_000_000,
              "data": calldata}
        self.prepare_calls.append(dict(tx))
        return tx

    def sign_prepared_transaction(self, tx):
        signed = _TEST_ACCOUNT.sign_transaction(tx)
        raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
        tx_hash = (signed.hash.hex() if hasattr(signed, "hash") else Web3.keccak(raw).hex())
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        return PreparedTx(tx_fields=tx, raw_transaction=raw, tx_hash=tx_hash, nonce=tx["nonce"],
                           chain_id=tx["chainId"], from_address=self.address, to=tx["to"], gas_limit=tx["gas"],
                           max_fee_per_gas=tx["maxFeePerGas"], max_priority_fee_per_gas=tx["maxPriorityFeePerGas"],
                           value_wei=tx["value"])

    def confirm_nonce_used(self, nonce):
        if nonce >= self.state_nonce:
            self.state_nonce = nonce + 1

    def describe_nonce_state(self):
        return {"address": self.address, "local_nonce": self.state_nonce,
                "onchain_nonce_pending": self.onchain_pending, "onchain_nonce_latest": self.onchain_latest,
                "matches_pending": self.state_nonce == self.onchain_pending,
                "matches_latest": self.state_nonce == self.onchain_latest}

    def resync_nonce_to_chain(self, source="pending"):
        self.state_nonce = self.onchain_pending if source == "pending" else self.onchain_latest
        return self.state_nonce

    def submit_prepared(self, prepared):
        self.submit_calls += 1
        if self.submit_result is not None:
            r = self.submit_result
            r.tx_hash = prepared.tx_hash
            return r
        return SendResult(ok=True, tx_hash=prepared.tx_hash, status=1, gas_used=150_000)


def _fake_route():
    # PoolKey требует currency0 < currency1 (лексикографически по int) --
    # NATIVE (0x0...0) < USDG всегда.
    leg1 = rr.RouteLeg(rr.NATIVE, rr.USDG, 3000, 60, rr.NATIVE, True)   # input=currency0=NATIVE
    leg2 = rr.RouteLeg(rr.NATIVE, rr.USDG, 3000, 60, rr.NATIVE, False)  # input=currency1=USDG
    return rr.RouteCycle("route_r4_test", (leg1, leg2), rr.USDG, "r4-test-route", "seed")


# ---------- Проверка 1 (пункт 2): фон гарантированно продвигается ----------

def check1_background_guaranteed_progress() -> None:
    fake_clock = [0.0]
    orig_monotonic = hp.time.monotonic
    hp.time.monotonic = lambda: fake_clock[0]  # ДО конструирования -- __init__ тоже читает time.monotonic()
    try:
        hint = hp._RpcPriorityHint()
        yields = []
        for i in range(20):
            hint.mark_trading_active()  # ИМИТИРУЕТ poll_once() каждые 1.5с -- владелец, репро: "20 раз в 1.5с"
            yields.append(hint.should_background_yield())
            fake_clock[0] += 1.5
        n_progress = sum(1 for y in yields if not y)  # False == фону разрешено продвинуться
        ok = n_progress > 0
        detail = (f"20 отметок торговой активности раз в 1.5с (ТОТ ЖЕ репро, что у владельца) -- фон "
                  f"получил разрешение продвинуться {n_progress} раз из 20 (было 0 из 20 ДО правки, "
                  f"MAX_BACKGROUND_STARVATION_S={hp.MAX_BACKGROUND_STARVATION_S}с)")
        _record("R4.2", "фон гарантированно продвигается при постоянном опросе детектора", ok, detail)
    finally:
        hp.time.monotonic = orig_monotonic


# ---------- Проверка 2 (пункт 3): два пула из разных вызовов образуют цикл ----------

def check2_two_pools_different_batches_form_cycle() -> None:
    registry = rr.RouteRegistry()
    pool_a = rr.PoolKey(rr.USDG, rr.MOSIAI, 70000, 4, rr.NATIVE)
    pool_b = rr.PoolKey(rr.USDG, rr.MOSIAI, 70000, 3, rr.NATIVE)
    pid_a, pid_b = rr.pool_id(pool_a), rr.pool_id(pool_b)

    calls = {"n": 0}

    def fake_find_ids(from_block, to_block, arbitrageur=rr.KNOWN_ARBITRAGEUR):
        calls["n"] += 1
        return {pid_a} if calls["n"] == 1 else {pid_b}

    def fake_fetch_init(pid, block):
        key = pool_a if pid == pid_a else pool_b
        return {"currency0": key.currency0, "currency1": key.currency1, "fee": key.fee,
                "tick_spacing": key.tick_spacing, "hooks": key.hooks}

    orig_find, orig_fetch = rr.find_arbitrageur_pool_ids, rr.fetch_initialize_event
    rr.find_arbitrageur_pool_ids = fake_find_ids
    rr.fetch_initialize_event = fake_fetch_init
    try:
        cycles_1 = registry.discover_new_arbitrageur_routes(100, 200)
        pool_a_persisted_alone = pid_a in registry.known_pools
        cycles_2 = registry.discover_new_arbitrageur_routes(201, 300)
    finally:
        rr.find_arbitrageur_pool_ids, rr.fetch_initialize_event = orig_find, orig_fetch

    ok = (len(cycles_1) == 0 and pool_a_persisted_alone and len(cycles_2) >= 1
          and pid_a in registry.known_pools and pid_b in registry.known_pools)
    detail = (f"вызов 1 (пул A, без пары): новых циклов={len(cycles_1)} (ожидаемо 0), пул A СОХРАНЁН в "
              f"known_pools={pool_a_persisted_alone} (раньше -- терялся, т.к. known_pools пополнялся ТОЛЬКО "
              f"через add_route, вызываемый только при готовом цикле); вызов 2 (пул B той же пары, ПОЗЖЕ): "
              f"новых циклов={len(cycles_2)} (ожидаемо >=1 -- A+B вместе образуют цикл); оба пула теперь в "
              f"known_pools={pid_a in registry.known_pools and pid_b in registry.known_pools}.")
    _record("R4.3a", "два пула одной пары из разных вызовов вместе образуют цикл, не теряются", ok, detail)

    # Пункт 3, доп.: временная ошибка получения Initialize -- пул НЕ теряется.
    registry2 = rr.RouteRegistry()
    pid_c = pool_id_c = rr.pool_id(rr.PoolKey(rr.USDG, rr.MOSIAI, 500, 10, rr.NATIVE))
    fetch_calls = {"n": 0}

    def flaky_fetch(pid, block):
        fetch_calls["n"] += 1
        if fetch_calls["n"] == 1:
            return None  # первая попытка -- "временная ошибка"
        key = rr.PoolKey(rr.USDG, rr.MOSIAI, 500, 10, rr.NATIVE)
        return {"currency0": key.currency0, "currency1": key.currency1, "fee": key.fee,
                "tick_spacing": key.tick_spacing, "hooks": key.hooks}

    rr.find_arbitrageur_pool_ids = lambda *a, **kw: {pid_c}
    rr.fetch_initialize_event = flaky_fetch
    try:
        registry2.discover_new_arbitrageur_routes(1, 10)
        still_pending = pid_c in registry2._pending_retry_pool_ids
        registry2.discover_new_arbitrageur_routes(11, 20)  # повторная попытка -- ДАЖЕ без новых Swap-логов пула C
        recovered = pid_c in registry2.known_pools
    finally:
        rr.find_arbitrageur_pool_ids, rr.fetch_initialize_event = orig_find, orig_fetch

    ok2 = still_pending and recovered
    _record("R4.3b", "пул с временной ошибкой получения Initialize не теряется, повторяется на следующем вызове",
            ok2, f"после 1-й (неудачной) попытки pending_retry={still_pending}; после 2-й попытки "
                 f"(БЕЗ нового Swap-сигнала пула C) known_pools содержит его={recovered}")


# ---------- Проверка 3 (пункт 4): nonce после деплоя/рестарта ----------

def check3_nonce_resync_after_deploy() -> None:
    paths = _tmp_paths("check3a")
    budget = PilotBudget(state_path=paths["budget"])  # pending is None -- чистый старт
    sender = _FakeSender(start_nonce=10)
    sender.onchain_pending = 15  # "после деплоя" -- 5 транзакций ушли ВНЕ Sender
    sender.onchain_latest = 15
    hp.verify_nonce_consistency(sender, budget)
    ok1 = sender.state_nonce == 15 and not budget.halted
    _record("R4.4a", "безопасная резинхронизация nonce после деплоя (pending==latest, нет своих pending)",
            ok1, f"локальный nonce после verify_nonce_consistency={sender.state_nonce} (ожидание 15), "
                 f"halted={budget.halted}")

    paths2 = _tmp_paths("check3b")
    budget2 = PilotBudget(state_path=paths2["budget"])
    sender2 = _FakeSender(start_nonce=10)
    sender2.onchain_pending = 12  # НЕ равен latest -- чья-то ЧУЖАЯ неподтверждённая транзакция
    sender2.onchain_latest = 11
    hp.verify_nonce_consistency(sender2, budget2)
    ok2 = sender2.state_nonce == 10 and budget2.halted  # nonce НЕ тронут, пилот остановлен
    _record("R4.4b", "необъяснимое расхождение (чужая неподтверждённая tx) -- halt, НЕ резинхронизируем",
            ok2, f"локальный nonce НЕ изменён={sender2.state_nonce == 10}, halted={budget2.halted} "
                 f"('{budget2.halt_reason}')")


# ---------- Проверка 4 (пункт 5): STOP между гейтом и отправкой ----------

def check4_stop_between_gate_and_send() -> None:
    paths = _tmp_paths("check4")
    budget = PilotBudget(state_path=paths["budget"])
    reason_log = ReasonLog(path=paths["reasons"])
    attempt_table = AttemptTable(path=paths["attempts"])
    sender = _FakeSender(start_nonce=7)
    route = _fake_route()
    registry = rr.RouteRegistry()
    registry.add_route(route)

    stop_path = Path(paths["stop"])
    orig_stop_file = hp.STOP_FILE_PATH
    hp.STOP_FILE_PATH = stop_path  # подменяем на временный путь -- НЕ трогаем реальный /etc/bot/STOP

    orig_rpc_call = hp._rpc_call
    orig_rpc_call_trading_path = hp.rpc_call_trading_path  # ПРАВКА (шестой раунд, пункт 5A): отдельная быстрая полоса
    orig_recompute = hp.recompute_route
    orig_estimate_gas = hp.estimate_gas
    orig_quote_at_size = hp.quote_route_at_size  # ПРАВКА (седьмой раунд): _quote_and_estimate_gas_consistent зовёт её всегда
    orig_weth_price = hp.current_weth_usdg_price
    orig_token_balance = hp._token_balance
    orig_build_calldata = hp.build_execute_cycle_calldata

    def fake_rpc_call(method, params):
        if method == "eth_blockNumber":
            return hex(1000)  # тот же блок, что передаём ниже -- финальная ре-котировка не запускается
        if method == "eth_gasPrice":
            return hex(2_000_000_000)
        raise AssertionError(f"неожиданный _rpc_call в тесте: {method}")

    def fake_recompute(route, block_number):
        return {"ok": True, "amount_in": 1_000_000, "amount_out": 21_000_000, "profit_raw": 20_000_000}

    def fake_estimate_gas(contract_address, calldata, from_address, block_number=None):
        return {"ok": True, "gas_estimate": 200_000}

    def fake_quote_at_size(route, amount_in, block_number):
        # ПРАВКА (седьмой раунд): блок ВСЕГДА 1000 в этом тесте (fake_rpc_call
        # не двигает eth_blockNumber) -- тот же результат, что recompute.
        return {"ok": True, "amount_in": amount_in, "amount_out": amount_in + 20_000_000, "profit_raw": 20_000_000}

    token_balance_calls = {"n": 0}

    def fake_token_balance(token, account):
        token_balance_calls["n"] += 1
        if token_balance_calls["n"] == 1:
            # ИМЕННО репро владельца: "STOP установлен ПРИ ЧТЕНИИ БАЛАНСА".
            stop_path.parent.mkdir(parents=True, exist_ok=True)
            stop_path.touch()
        return 0

    hp._rpc_call = fake_rpc_call
    hp.rpc_call_trading_path = fake_rpc_call  # _evaluate_and_maybe_send теперь зовёт именно эту функцию
    hp.recompute_route = fake_recompute
    hp.estimate_gas = fake_estimate_gas
    hp.quote_route_at_size = fake_quote_at_size
    hp.current_weth_usdg_price = lambda: 2500.0
    hp._token_balance = fake_token_balance
    hp.build_execute_cycle_calldata = lambda route, amt, min_profit, sqrt_price_limits=None: b"\x00\x00\x00\x00" + str(min_profit).encode()

    try:
        hotpath = hp.HotPath(registry, "0x0000000000000000000000000000000000000002", sender.address,
                              budget, attempt_table, reason_log, hp._RpcPriorityHint(), sender=sender, dry_run=False)
        hotpath._evaluate_and_maybe_send(route, 1000, hp.time.monotonic())
    finally:
        hp.STOP_FILE_PATH = orig_stop_file
        hp._rpc_call = orig_rpc_call
        hp.rpc_call_trading_path = orig_rpc_call_trading_path
        hp.recompute_route = orig_recompute
        hp.estimate_gas = orig_estimate_gas
        hp.quote_route_at_size = orig_quote_at_size
        hp.current_weth_usdg_price = orig_weth_price
        hp._token_balance = orig_token_balance
        hp.build_execute_cycle_calldata = orig_build_calldata

    ok = sender.submit_calls == 0 and budget.halted and budget.pending is not None
    detail = (f"STOP-файл создан ВНУТРИ первого вызова _token_balance (имитирует репро владельца: 'STOP "
              f"установлен при чтении баланса') -- ПОСЛЕ первого гейта, ДО подготовки/резерва/подписи/"
              f"begin_attempt/submit_prepared. submit_prepared вызван {sender.submit_calls} раз (ожидание 0), "
              f"budget.halted={budget.halted}, budget.pending={'сохранён' if budget.pending else 'None'} "
              f"(hash={budget.pending.get('tx_hash') if budget.pending else None}) -- транзакция ТОЧНО "
              f"НЕ отправлена после остановки.")
    _record("R4.5a", "STOP между гейтом и submit_prepared -- отправка НЕ происходит", ok, detail)


# ---------- Проверка 5 (пункт 5): завершение с неразрешённой pending ----------

def check5_completion_with_unresolved_pending() -> None:
    paths = _tmp_paths("check5")
    budget = PilotBudget(state_path=paths["budget"])
    budget.pending = {"tx_hash": "0xunresolved", "nonce": 1}  # НЕ разрешена
    reason_log = ReasonLog(path=paths["reasons"])
    attempt_table = AttemptTable(path=paths["attempts"])
    hotpath = hp.HotPath(rr.RouteRegistry(), "0x0", "0x0", budget, attempt_table, reason_log,
                          hp._RpcPriorityHint(), sender=None, dry_run=True)
    hotpath.busy = False
    ok = hotpath.evaluator_finished_current_work() is True
    _record("R4.5b", "поток 'закончил работу' НЕ требует pending is None -- главный цикл не виснет",
            ok, f"budget.pending={budget.pending is not None} (НЕ разрешена), busy=False -- "
                f"evaluator_finished_current_work()={hotpath.evaluator_finished_current_work()} (ожидание True; "
                f"старый is_idle() дал бы False здесь, и главный цикл ждал бы вечно)")


# ---------- Проверка 6 (пункт 6): комиссия меняется МЕЖДУ двумя prepare ----------

def check6_fee_changes_between_prepares() -> None:
    paths = _tmp_paths("check6")
    budget = PilotBudget(state_path=paths["budget"])
    reason_log = ReasonLog(path=paths["reasons"])
    attempt_table = AttemptTable(path=paths["attempts"])
    sender = _FakeSender(start_nonce=9)
    sender._next_max_fee_sequence = [2_000_000_000, 3_000_000_000, 3_000_000_000]  # комиссия РАСТЁТ, потом стабилизируется
    route = _fake_route()
    registry = rr.RouteRegistry()
    registry.add_route(route)

    orig_rpc_call = hp._rpc_call
    orig_rpc_call_trading_path = hp.rpc_call_trading_path  # ПРАВКА (шестой раунд, пункт 5A): отдельная быстрая полоса
    orig_recompute = hp.recompute_route
    orig_quote_at_size = hp.quote_route_at_size  # ПРАВКА (седьмой раунд): _quote_and_estimate_gas_consistent зовёт её всегда
    orig_estimate_gas = hp.estimate_gas
    orig_weth_price = hp.current_weth_usdg_price
    orig_token_balance = hp._token_balance
    orig_build_calldata = hp.build_execute_cycle_calldata
    orig_fetch_receipt = hp.fetch_real_receipt

    encoded_min_profits: list[int] = []

    def fake_rpc_call(method, params):
        if method == "eth_blockNumber":
            return hex(2000)
        if method == "eth_gasPrice":
            return hex(2_000_000_000)
        raise AssertionError(f"неожиданный _rpc_call в тесте: {method}")

    def fake_quote_at_size(route, amount_in, block_number):
        # ПРАВКА (седьмой раунд): блок ВСЕГДА 2000 в этом тесте (fake_rpc_call
        # не двигает eth_blockNumber) -- тот же результат, что recompute.
        return {"ok": True, "amount_in": amount_in, "amount_out": amount_in + 20_000_000, "profit_raw": 20_000_000}

    def fake_build_calldata(route, amt, min_profit, sqrt_price_limits=None):
        encoded_min_profits.append(min_profit)
        return b"\x00\x00\x00\x00" + str(min_profit).encode()

    hp._rpc_call = fake_rpc_call
    hp.rpc_call_trading_path = fake_rpc_call  # _evaluate_and_maybe_send теперь зовёт именно эту функцию
    hp.recompute_route = lambda route, block: {"ok": True, "amount_in": 1_000_000, "amount_out": 21_000_000,
                                                "profit_raw": 20_000_000}
    hp.quote_route_at_size = fake_quote_at_size
    hp.estimate_gas = lambda addr, calldata, frm, block_number=None: {"ok": True, "gas_estimate": 200_000}
    hp.current_weth_usdg_price = lambda: 2500.0
    hp._token_balance = lambda token, account: 0
    hp.build_execute_cycle_calldata = fake_build_calldata
    hp.fetch_real_receipt = lambda tx_hash: {"status": "0x1", "gasUsed": "0x249f0", "blockNumber": 2000, "logs": []}

    try:
        hotpath = hp.HotPath(registry, "0x0000000000000000000000000000000000000003", sender.address,
                              budget, attempt_table, reason_log, hp._RpcPriorityHint(), sender=sender, dry_run=False)
        hotpath._evaluate_and_maybe_send(route, 2000, hp.time.monotonic())
    finally:
        hp._rpc_call = orig_rpc_call
        hp.rpc_call_trading_path = orig_rpc_call_trading_path
        hp.recompute_route = orig_recompute
        hp.quote_route_at_size = orig_quote_at_size
        hp.estimate_gas = orig_estimate_gas
        hp.current_weth_usdg_price = orig_weth_price
        hp._token_balance = orig_token_balance
        hp.build_execute_cycle_calldata = orig_build_calldata
        hp.fetch_real_receipt = orig_fetch_receipt

    final_fee = sender._next_max_fee_sequence[-1]
    required_final = hp.compute_min_profit_raw(200_000, final_fee, route.exit_token, 2500.0)
    final_encoded = encoded_min_profits[-1] if encoded_min_profits else None
    n_prepares = len(sender.prepare_calls)
    ok = (n_prepares >= 2 and final_encoded is not None and final_encoded >= required_final
          and sender.submit_calls == 1)
    detail = (f"комиссия росла между подготовками ({sender._next_max_fee_sequence} wei) -- потребовалось "
              f"{n_prepares} подготовок (>1 -- значит согласование ДЕЙСТВИТЕЛЬНО пересобирало calldata). "
              f"Закодированные minProfit по кругам: {encoded_min_profits}. ИТОГОВЫЙ закодированный "
              f"minProfit={final_encoded}, требуемый ПО ИТОГОВОЙ (последней) комиссии={required_final} -- "
              f"закодированный >= требуемого: {final_encoded is not None and final_encoded >= required_final} "
              f"(до правки: закодированный мог остаться от ПЕРВОЙ, более низкой комиссии). Отправка "
              f"состоялась: submit_calls={sender.submit_calls}.")
    _record("R4.6", "согласованный minProfit покрывает ИТОГОВУЮ (не первую) комиссию", ok, detail)


# ---------- Проверка 7 (пункт 7): повторная запись строки не дублирует ----------

def check7_duplicate_row_write() -> None:
    paths = _tmp_paths("check7")
    row = AttemptTableRow(
        ts_wall=1.0, route_label="r", route_id="rid", size_in_raw=1000, exit_token=rr.USDG,
        expected_profit_after_gas=0.1, latency_recv_to_send_s=0.01, tx_hash="0xduplicatetest",
        result="success", actual_gain_base_asset=0.1, gas_used=100_000, gas_cost_native=0.001,
        cumulative_gas_loss_usd=0.1, cumulative_net_pnl_usd=0.0, computed_at_block=100, state_age_blocks=0,
    )
    table1 = AttemptTable(path=paths["attempts"])
    wrote1 = table1.write(row)  # имитирует запись ДО краха
    # "рестарт" -- НОВЫЙ экземпляр AttemptTable на ТОМ ЖЕ файле (перечитывает уже записанные хэши)
    table2 = AttemptTable(path=paths["attempts"])
    wrote2 = table2.write(row)  # имитирует ПОВТОРНУЮ попытку записи ТОГО ЖЕ tx_hash после рестарта
    n_lines = sum(1 for _ in Path(paths["attempts"]).open())
    ok = wrote1 is True and wrote2 is False and n_lines == 1
    _record("R4.7", "повторная запись строки с тем же tx_hash после 'рестарта' не создаёт дубликат",
            ok, f"первая запись: {wrote1} (ожидание True), повторная (после 'рестарта'): {wrote2} "
                f"(ожидание False), строк в файле: {n_lines} (ожидание 1, НЕ 2)")

    # Пункт 7, доп.: парсер CycleExecuted проверяет exitToken и структуру.
    good_data = "0x" + rr.USDG[2:].rjust(64, "0") + format(123456, "064x") + format(2, "064x")
    receipt = {"logs": [{"address": "0xcontract0000000000000000000000000000001",
                          "topics": [hp.CYCLE_EXECUTED_TOPIC0], "data": good_data}]}
    ok_match = hp.parse_cycle_executed_profit(receipt, "0xcontract0000000000000000000000000000001", rr.USDG) == 123456
    ok_wrong_token = hp.parse_cycle_executed_profit(receipt, "0xcontract0000000000000000000000000000001", rr.NATIVE) is None
    bad_words_data = "0x" + rr.USDG[2:].rjust(64, "0") + format(123456, "064x")  # только 2 слова, не 3
    ok_bad_structure = hp.parse_cycle_executed_profit(
        {"logs": [{"address": "0xcontract0000000000000000000000000000001", "topics": [hp.CYCLE_EXECUTED_TOPIC0],
                   "data": bad_words_data}]}, "0xcontract0000000000000000000000000000001", rr.USDG) is None
    ok2 = ok_match and ok_wrong_token and ok_bad_structure
    _record("R4.7b", "парсер CycleExecuted проверяет exitToken и структуру события", ok2,
            f"совпадающий exitToken -> profit найден: {ok_match}; НЕсовпадающий exitToken -> None: "
            f"{ok_wrong_token}; неверное число слов (2 вместо 3) -> None: {ok_bad_structure}")


def main() -> None:
    check1_background_guaranteed_progress()
    check2_two_pools_different_batches_form_cycle()
    check3_nonce_resync_after_deploy()
    check4_stop_between_gate_and_send()
    check5_completion_with_unresolved_pending()
    check6_fee_changes_between_prepares()
    check7_duplicate_row_write()

    n_fail = sum(1 for r in RESULTS if not r["ok"])
    print(f"\n=== ИТОГ: {len(RESULTS) - n_fail}/{len(RESULTS)} проверок пройдено ===")
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_round4_pointchecks_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"results": RESULTS, "all_passed": n_fail == 0}, indent=2, ensure_ascii=False))
    print(f"результат записан в {out_path}")
    if n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
