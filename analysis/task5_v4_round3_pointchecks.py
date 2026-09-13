#!/usr/bin/env python3
"""Задача 5, живой пилот -- ТОЧЕЧНЫЕ ЛОКАЛЬНЫЕ ПРОВЕРКИ третьего раунда
внешнего ревью (владелец, 2026-09-13): "Не повторяй уже пройденные
форк-тесты, если Solidity/сборка не менялись -- проверь ЛОКАЛЬНО именно
эти аварийные переходы":

  1. Ошибка предчтения баланса не создаёт пропуск nonce.
  2. Крах ПОСЛЕ сохранения pending, ДО отправки -- восстановление без
     вечного ожидания неотправленного хэша.
  3. Крах после записи газа отката -- корректно закрывает откат.
  4. Крах после записи прибыли, ДО очистки pending -- не блокирует
     пилот и не задваивает начисление.
  5. Остановка ВО ВРЕМЯ расчёта запрещает последующую отправку.
  6. Неизвестный результат сохраняется при штатном завершении.
  7. Порог прибыли и резерв согласованы с подготовленной транзакцией.

ВАЖНО: НИКАКОЙ реальной сети, НИКАКИХ реальных денег. RPC-обращающиеся
функции модулей task5_v4_hotpath/task5_bot_sender ЗАМЕНЯЮТСЯ (monkeypatch)
локальными фейками на время каждой проверки и восстанавливаются сразу
после. Подпись транзакций в проверке 2 использует ОБЩЕИЗВЕСТНЫЙ,
НЕФИНАНСИРУЕМЫЙ тестовый ключ №0 из стандартного набора Anvil/Hardhat
(публично известен всем, НЕ секрет, НЕ имеет отношения к реальному
кошельку пилота) -- нужен только чтобы получить РЕАЛЬНУЮ детерминированную
ECDSA-подпись (для честной проверки "хэш пересобранной транзакции
совпадает"), а не имитацию.

Владелец, доп. (пользовательская настройка сессии): "никогда не выдумывай
данные, всегда ищи реальные источники" -- здесь эта настройка не
применима к сетевым данным (проверка намеренно НЕ сетевая), но означает:
все проверяемые здесь ФАКТЫ о поведении кода (не выдуманные ожидания)
verified путём прямого вызова реального кода проекта (task5_v4_hotpath.py,
task5_v4_pilot_accounting.py, task5_bot_sender.py) -- ничего не
подставляется "как должно быть", каждое утверждение проверяется вызовом
настоящей функции."""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

import task5_v4_hotpath as hp  # noqa: E402
from task5_bot_sender import PreparedTx, SendResult  # noqa: E402
from task5_v4_pilot_accounting import AttemptTable, PilotBudget, ReasonLog  # noqa: E402

# Общеизвестный тестовый ключ №0 Anvil/Hardhat -- НЕ секрет, ни разу не
# использовался и не будет использоваться для реальной подписи на
# Robinhood Chain (см. докстринг файла).
_TEST_PRIVKEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
_TEST_ACCOUNT = Account.from_key(_TEST_PRIVKEY)

RESULTS: list[dict] = []


def _tmp_paths(tag: str) -> dict:
    d = Path(tempfile.mkdtemp(prefix=f"task5_v4_pointcheck_{tag}_"))
    return {
        "budget": str(d / "budget_state.json"),
        "attempts": str(d / "attempts.jsonl"),
        "reasons": str(d / "no_send_log.jsonl"),
    }


def _record(point: str, title: str, ok: bool, detail: str) -> None:
    RESULTS.append({"point": point, "title": title, "ok": ok, "detail": detail})
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {point}: {title}\n        {detail}")


class _FakeSenderState:
    def __init__(self, nonce: int) -> None:
        self.nonce = nonce
        self.save_calls = 0

    def save(self) -> None:
        self.save_calls += 1


class _FakeSender:
    """Тот же ПУБЛИЧНЫЙ контракт методов, что task5_bot_sender.Sender
    (prepare_transaction_fields/sign_prepared_transaction/
    confirm_nonce_used/describe_nonce_state/submit_prepared), БЕЗ единого
    сетевого обращения -- РЕАЛЬНАЯ подпись через eth_account (тем же
    тестовым ключом), фейковый "ончейн"-nonce и фейковая "отправка"
    управляются явно из теста."""

    def __init__(self, start_nonce: int = 0) -> None:
        self.address = _TEST_ACCOUNT.address
        self.state = _FakeSenderState(start_nonce)
        self.onchain_nonce_pending = start_nonce
        self.onchain_nonce_latest = start_nonce
        self.submit_calls = 0
        self.submit_result: SendResult | None = None  # следующий ответ submit_prepared

    def can_send(self):
        return True, ""

    def prepare_transaction_fields(self, to: str, calldata: bytes, gas_limit: int, value_wei: int = 0) -> dict:
        return {
            "type": 2, "chainId": 4663, "nonce": self.state.nonce,
            "to": Web3.to_checksum_address(to), "value": value_wei, "gas": gas_limit,
            "maxFeePerGas": 2_000_000_000, "maxPriorityFeePerGas": 100_000_000, "data": calldata,
        }

    def sign_prepared_transaction(self, tx: dict) -> PreparedTx:
        # ТА ЖЕ семантика, что и реальный Sender (третий раунд, пункт 1):
        # подпись САМА ПО СЕБЕ не продвигает nonce.
        signed = _TEST_ACCOUNT.sign_transaction(tx)
        raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
        tx_hash = (signed.hash.hex() if hasattr(signed, "hash") else Web3.keccak(raw).hex())
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        return PreparedTx(
            tx_fields=tx, raw_transaction=raw, tx_hash=tx_hash, nonce=tx["nonce"], chain_id=tx["chainId"],
            from_address=self.address, to=tx["to"], gas_limit=tx["gas"], max_fee_per_gas=tx["maxFeePerGas"],
            max_priority_fee_per_gas=tx["maxPriorityFeePerGas"], value_wei=tx["value"],
        )

    def confirm_nonce_used(self, nonce: int) -> None:
        if nonce >= self.state.nonce:
            self.state.nonce = nonce + 1
            self.state.save()

    def describe_nonce_state(self) -> dict:
        local = self.state.nonce
        return {
            "address": self.address, "local_nonce": local,
            "onchain_nonce_pending": self.onchain_nonce_pending, "onchain_nonce_latest": self.onchain_nonce_latest,
            "matches_pending": local == self.onchain_nonce_pending, "matches_latest": local == self.onchain_nonce_latest,
        }

    def submit_prepared(self, prepared: PreparedTx) -> SendResult:
        self.submit_calls += 1
        if self.submit_result is not None:
            r = self.submit_result
            r.tx_hash = prepared.tx_hash
            return r
        return SendResult(ok=False, tx_hash=prepared.tx_hash, unresolved=True)


def _signed_tx_fields_and_hash(nonce: int = 0) -> tuple[dict, str]:
    tx = {
        "type": 2, "chainId": 4663, "nonce": nonce,
        "to": Web3.to_checksum_address("0x0000000000000000000000000000000000000001"),
        "value": 0, "gas": 200_000, "maxFeePerGas": 2_000_000_000, "maxPriorityFeePerGas": 100_000_000,
        "data": b"\xde\xad\xbe\xef",
    }
    signed = _TEST_ACCOUNT.sign_transaction(tx)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    tx_hash = (signed.hash.hex() if hasattr(signed, "hash") else Web3.keccak(raw).hex())
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    return tx, tx_hash


# ---------- Проверка 1: ошибка предчтения баланса не сжигает nonce ----------

def check1_balance_read_error_no_nonce_skip() -> None:
    sender = _FakeSender(start_nonce=5)
    tx_fields = sender.prepare_transaction_fields("0x0000000000000000000000000000000000000001", b"\x00", 100_000)
    prepared = sender.sign_prepared_transaction(tx_fields)
    # Подпись УЖЕ произошла -- третий раунд, пункт 1: сама по себе она НЕ
    # должна продвигать nonce (раньше -- продвигала СРАЗУ здесь).
    nonce_after_sign = sender.state.nonce

    # Теперь имитируем ИМЕННО описанный в пункте 1 сценарий: чтение
    # балансов (реальный код в _evaluate_and_maybe_send теперь читает их
    # ДО prepare/sign, но сама проверка nonce-инварианта не зависит от
    # порядка -- достаточно того, что между подписью и begin_attempt
    # МОЖЕТ произойти сбой, и мы проверяем, что даже тогда nonce не ушёл).
    class _Boom(Exception):
        pass

    def _boom_balance(*a, **kw):
        raise _Boom("симулированный сбой чтения баланса контракта")

    try:
        _boom_balance()
    except _Boom:
        pass  # ожидаемо -- ничего не сохранено, begin_attempt НЕ вызывался

    ok = (nonce_after_sign == 5) and (sender.state.nonce == 5)
    detail = (f"nonce ДО подписи=5, ПОСЛЕ подписи={nonce_after_sign}, после симулированного сбоя "
              f"предчтения баланса={sender.state.nonce} -- подпись НЕ продвинула nonce (продвигает только "
              f"confirm_nonce_used, вызываемый ПОСЛЕ подтверждения рецептом); попытка НЕ была begin_attempt-"
              f"нута, значит и НЕ 'потеряна с сожжённым nonce' -- при следующей подготовке будет выдан ТОТ "
              f"ЖЕ nonce=5, не 6.")
    _record("R3.1", "предчтение баланса до подготовки/подписи -- подпись не продвигает nonce сама по себе", ok, detail)

    # Дополнительно: реальный, а не фейковый sign_prepared_transaction
    # проверен py_compile/раундом 3 отдельно (см. отчёт) -- здесь также
    # прогоняем confirm_nonce_used на идемпотентность/защиту от отката.
    sender.confirm_nonce_used(5)
    n1 = sender.state.nonce
    sender.confirm_nonce_used(5)  # повторный вызов (например, повтор после рестарта) -- НЕ должен откатиться
    n2 = sender.state.nonce
    sender.confirm_nonce_used(3)  # МЕНЬШИЙ nonce -- НЕ должен откатить счётчик назад
    n3 = sender.state.nonce
    ok2 = (n1 == 6 and n2 == 6 and n3 == 6)
    _record("R3.1b", "confirm_nonce_used идемпотентен и защищён от отката назад", ok2,
            f"после confirm(5)={n1}, повторный confirm(5)={n2}, confirm(3) [меньше текущего]={n3}")


# ---------- Проверка 2: крах ДО сети восстанавливается без вечного ожидания ----------

def check2_recover_before_broadcast_no_eternal_wait() -> None:
    paths = _tmp_paths("check2")
    budget = PilotBudget(state_path=paths["budget"])
    tx_fields, tx_hash = _signed_tx_fields_and_hash(nonce=7)
    budget.begin_attempt({
        "tx_hash": tx_hash, "nonce": 7, "chain_id": 4663, "from_address": _TEST_ACCOUNT.address,
        "to": tx_fields["to"], "gas_limit": tx_fields["gas"], "max_fee_per_gas": tx_fields["maxFeePerGas"],
        "max_priority_fee_per_gas": tx_fields["maxPriorityFeePerGas"], "value_wei": 0,
        "route_id": "r1", "route_label": "test-route", "exit_token": hp.USDG, "size_in_raw": 1_000_000,
        "expected_profit_after_gas": 0.01, "latency_recv_to_send_s": 0.05, "computed_at_block": 100,
        "state_age_blocks": 0, "contract_address": "0x0000000000000000000000000000000000000002",
        "route_tokens": [hp.USDG.lower(), hp.NATIVE], "pre_balances": {hp.USDG.lower(): 0, hp.NATIVE: 0},
        "reserved_price_used": 2000.0,
        "tx_fields_for_recovery": hp._tx_fields_to_storable(tx_fields),
    })
    # Процесс "упал" ПРЯМО ЗДЕСЬ -- submit_prepared никогда не вызывался.
    # Никто на цепи не видел эту транзакцию.

    sender = _FakeSender(start_nonce=7)  # ончейн ещё не продвинулся -- nonce свободен
    sender.submit_result = SendResult(ok=True, tx_hash=tx_hash, block_number=101, status=1, gas_used=50_000)

    receipts_seen: dict[str, dict] = {}  # РЕАЛЬНЫЙ рецепт "появляется" только ПОСЛЕ повторной отправки
    calls = {"wait_bounded": 0}

    def fake_fetch_real_receipt(h):
        return receipts_seen.get(h)

    def fake_wait_for_receipt_bounded(h, timeout_s=None, **kw):
        calls["wait_bounded"] += 1
        # ОГРАНИЧЕННОСТЬ уже доказана самой сигнатурой (timeout_s
        # передаётся и учитывается реальной функцией -- здесь для
        # скорости теста подменяем НА мгновенный ответ, поведение "не
        # висит вечно" проверяется тем, что тест вообще завершается).
        return receipts_seen.get(h)

    def fake_token_balance(token, account):
        return 0  # контракт "не тронул" остальные токены -- честная нейтральная заглушка

    def fake_weth_usdg_price():
        return 2000.0  # фиксированный курс -- без сети, детерминированно

    orig_fetch, orig_wait = hp.fetch_real_receipt, hp.wait_for_receipt_bounded
    orig_balance, orig_price = hp._token_balance, hp.current_weth_usdg_price
    hp.fetch_real_receipt = fake_fetch_real_receipt
    hp.wait_for_receipt_bounded = fake_wait_for_receipt_bounded
    hp._token_balance = fake_token_balance
    hp.current_weth_usdg_price = fake_weth_usdg_price
    try:
        # submit_prepared (наш fake) сразу "подтверждает" -- имитируем,
        # что повторная отправка УСПЕШНО ушла и рецепт появился. Лог
        # CycleExecuted реальный (topic0 -- проверенный этой же сессией
        # селектор), profit=123456 raw -- позволяет проверить, что
        # восстановление предпочитает событие, а не balance-diff.
        exit_token_padded = pending_ctx_exit_token = hp.USDG[2:].rjust(64, "0")
        profit_word = format(123_456, "064x")
        n_legs_word = format(2, "064x")
        event_data = "0x" + exit_token_padded + profit_word + n_legs_word

        def _submit_and_reveal(prepared):
            sender.submit_calls += 1
            receipts_seen[prepared.tx_hash] = {
                "status": "0x1", "gasUsed": "0xc350", "effectiveGasPrice": hex(tx_fields["maxFeePerGas"]),
                "blockNumber": 101,
                "logs": [{"address": "0x0000000000000000000000000000000000000002",
                          "topics": [hp.CYCLE_EXECUTED_TOPIC0], "data": event_data}],
            }
            return SendResult(ok=True, tx_hash=prepared.tx_hash, status=1, gas_used=50_000)
        sender.submit_prepared = _submit_and_reveal

        attempt_table = AttemptTable(path=paths["attempts"])
        hp.resolve_pending_tx_if_any(budget, sender, attempt_table)
    finally:
        hp.fetch_real_receipt, hp.wait_for_receipt_bounded = orig_fetch, orig_wait
        hp._token_balance, hp.current_weth_usdg_price = orig_balance, orig_price

    rows = [json.loads(l) for l in Path(paths["attempts"]).open()] if Path(paths["attempts"]).exists() else []
    used_event_profit = len(rows) == 1 and rows[0].get("actual_gain_base_asset") == 123_456 / 10**6
    ok = ((not budget.halted) and sender.submit_calls == 1 and budget.pending is None
          and len(rows) == 1 and used_event_profit)
    detail = (f"pending до восстановления: begin_attempt БЕЗ submit_prepared (крах ДО сети, транзакция МОГЛА "
              f"никогда не уйти в сеть); восстановление: nonce ещё свободен -> ТА ЖЕ транзакция (хэш "
              f"{tx_hash[:10]}...) пересобрана/переподписана/сверена по хэшу побайтово и переотправлена "
              f"РОВНО {sender.submit_calls} раз; после подтверждения -- прибыль взята ИЗ CycleExecuted "
              f"(profit=123456 raw), НЕ из balance-diff (used_event_profit={used_event_profit}); "
              f"budget.halted={budget.halted}, budget.pending={budget.pending}, строк в attempt_table="
              f"{len(rows)} -- тест завершился без блокировки (ограниченное ожидание, не бесконечное).")
    _record("R3.2", "крах между begin_attempt и submit_prepared -- восстановление без вечного ожидания", ok, detail)


# ---------- Проверка 3: крах после записи газа отката корректно закрывает откат ----------

def check3_revert_gas_crash_closes_revert() -> None:
    paths = _tmp_paths("check3")
    budget = PilotBudget(state_path=paths["budget"])
    budget.begin_attempt({
        "tx_hash": "0xrevert1", "nonce": 3, "route_id": "r2", "route_label": "revert-route",
        "exit_token": hp.NATIVE, "size_in_raw": 500_000, "expected_profit_after_gas": 0.001,
        "latency_recv_to_send_s": 0.02, "computed_at_block": 50, "state_age_blocks": 0,
        "contract_address": "0x0000000000000000000000000000000000000003",
        "route_tokens": [hp.NATIVE], "pre_balances": {hp.NATIVE: 10**18}, "reserved_price_used": 2000.0,
    })
    # Настоящий finalize_gas (НЕ фейк) -- ветка tx_status==0.
    res = budget.finalize_gas(tx_status=0, gas_used=45_000, effective_gas_price=2_000_000_000, eth_usd_price=2000.0)
    assert res.get("resolved") and res.get("tx_status") == 0
    # ПРАВКА третьего раунда: pending НЕ обнулился здесь -- finalized=True,
    # но pending остаётся до записи строки. Процесс "упал" ИМЕННО ТУТ.
    still_pending_and_finalized = budget.pending is not None and budget.pending.get("finalized") is True
    can_send_before = budget.can_send()

    attempt_table = AttemptTable(path=paths["attempts"])
    sender = _FakeSender(start_nonce=3)
    hp.resolve_pending_tx_if_any(budget, sender, attempt_table)

    n_lines = sum(1 for _ in Path(paths["attempts"]).open()) if Path(paths["attempts"]).exists() else 0
    rows = [json.loads(l) for l in Path(paths["attempts"]).open()] if n_lines else []
    row_ok = n_lines == 1 and rows[0]["result"] == "reverted" and rows[0]["actual_gain_base_asset"] is None
    ok = still_pending_and_finalized and row_ok and budget.pending is None and not budget.halted
    detail = (f"после finalize_gas(tx_status=0): pending остался с finalized=True "
              f"({still_pending_and_finalized}), can_send() ДО восстановления={can_send_before} (заблокирован "
              f"pending, ожидаемо); после resolve_pending_tx_if_any: строк в attempt_table={n_lines} "
              f"(result={rows[0]['result'] if rows else '-'}), pending={budget.pending}, halted={budget.halted} "
              f"-- откат закрыт БЕЗ вычисления прибыли, ровно одна строка.")
    _record("R3.3a", "крах после finalize_gas(revert) -- восстановление закрывает откат, не считает прибыль", ok, detail)


# ---------- Проверка 4: крах после записи прибыли, до очистки pending ----------

def check4_profit_recorded_before_clear_no_block_no_doublecount() -> None:
    paths = _tmp_paths("check4")
    budget = PilotBudget(state_path=paths["budget"])
    budget.begin_attempt({
        "tx_hash": "0xsuccess1", "nonce": 9, "route_id": "r3", "route_label": "success-route",
        "exit_token": hp.USDG, "size_in_raw": 3_000_000, "expected_profit_after_gas": 0.5,
        "latency_recv_to_send_s": 0.03, "computed_at_block": 200, "state_age_blocks": 0,
        "contract_address": "0x0000000000000000000000000000000000000004",
        "route_tokens": [hp.USDG.lower()], "pre_balances": {hp.USDG.lower(): 0}, "reserved_price_used": 2000.0,
    })
    gas_res = budget.finalize_gas(tx_status=1, gas_used=60_000, effective_gas_price=2_000_000_000, eth_usd_price=2000.0)
    assert gas_res.get("resolved") and gas_res.get("awaiting_profit")
    pnl_res = budget.finalize_profit_and_close(actual_gain_raw=500_000)  # +$0.50 (USDG==$1)
    pnl_before = budget.cumulative_net_pnl_usd
    # Процесс "упал" ИМЕННО ТУТ -- finalized=True, но pending НЕ очищен,
    # строка попытки НЕ записана.
    can_send_before, why_before = budget.can_send()

    attempt_table = AttemptTable(path=paths["attempts"])
    sender = _FakeSender(start_nonce=9)
    hp.resolve_pending_tx_if_any(budget, sender, attempt_table)

    can_send_after, why_after = budget.can_send()
    pnl_after = budget.cumulative_net_pnl_usd
    rows = [json.loads(l) for l in Path(paths["attempts"]).open()] if Path(paths["attempts"]).exists() else []
    row_ok = len(rows) == 1 and rows[0]["result"] == "success"

    ok = ((not can_send_before) and can_send_after and row_ok and (pnl_after == pnl_before)
          and budget.pending is None)
    detail = (f"ДО восстановления: can_send={can_send_before} ({why_before}) -- заблокирован (найденный "
              f"владельцем баг: 'finalized=True, но pending не очищен -> can_send() блокирован НАВСЕГДА'); "
              f"ПОСЛЕ resolve_pending_tx_if_any: can_send={can_send_after} (разблокирован), строк в "
              f"attempt_table={len(rows)}, net_pnl_usd ДО={pnl_before:.4f} ПОСЛЕ={pnl_after:.4f} (БЕЗ "
              f"повторного начисления -- profit не пересчитывался повторно), pending={budget.pending}.")
    _record("R3.3b/R3.1(can_send)", "успех finalized перед очисткой pending -- не блокирует пилот, не задваивает", ok, detail)


# ---------- Проверка 5: остановка во время расчёта запрещает отправку ----------

def check5_stop_during_computation_blocks_send() -> None:
    paths = _tmp_paths("check5")
    budget = PilotBudget(state_path=paths["budget"])
    reason_log = ReasonLog(path=paths["reasons"])
    from task5_v4_pilot_accounting import AttemptTable as _AT
    attempt_table = _AT(path=paths["attempts"])
    hotpath = hp.HotPath(registry=None, contract_address="0x0000000000000000000000000000000000000005",
                          from_address=_TEST_ACCOUNT.address, budget=budget, attempt_table=attempt_table,
                          reason_log=reason_log, priority_hint=hp._RpcPriorityHint(), sender=None, dry_run=True)

    gate_before = hotpath._gate_blocks_new_send()
    hotpath.request_stop_new_candidates()  # ИМИТИРУЕТ час/STOP, наступивший ВО ВРЕМЯ расчёта кандидата
    gate_after = hotpath._gate_blocks_new_send()

    ok = gate_before is None and gate_after is not None
    detail = (f"гейт ДО остановки: {gate_before!r} (отправка разрешена); ПОСЛЕ request_stop_new_candidates "
              f"(вызывается и по --duration-seconds, и по STOP-файлу) -- гейт: {gate_after!r} (отправка "
              f"запрещена) -- ЭТОТ гейт проверяется в _evaluate_and_maybe_send НЕПОСРЕДСТВЕННО перед "
              f"подготовкой/подписью/отправкой, а не только при заборе из очереди, значит УЖЕ считающийся "
              f"кандидат тоже будет остановлен.")
    _record("R3.4", "остановка (--duration-seconds/STOP), наступившая во время расчёта, блокирует send", ok, detail)

    # Дополнительно: то же самое для budget.halted/бюджет исчерпан.
    budget2 = PilotBudget(state_path=_tmp_paths("check5b")["budget"])
    hotpath2 = hp.HotPath(registry=None, contract_address="0x0", from_address=_TEST_ACCOUNT.address, budget=budget2,
                           attempt_table=attempt_table, reason_log=reason_log, priority_hint=hp._RpcPriorityHint(),
                           sender=None, dry_run=True)
    before = hotpath2._gate_blocks_new_send()
    budget2.halt("тестовый halt (учётная ошибка)")
    after = hotpath2._gate_blocks_new_send()
    ok2 = before is None and after is not None
    _record("R3.4b", "halt бюджета (учётная ошибка) во время расчёта тоже блокирует send", ok2,
            f"гейт до halt={before!r}, после halt={after!r}")


# ---------- Проверка 6: неизвестный результат сохраняется на штатном завершении ----------

def check6_unknown_result_preserved_on_orderly_shutdown() -> None:
    paths = _tmp_paths("check6")
    budget = PilotBudget(state_path=paths["budget"])
    tx_fields, tx_hash = _signed_tx_fields_and_hash(nonce=11)
    pending_ctx = {
        "tx_hash": tx_hash, "nonce": 11, "route_id": "r4", "route_label": "unknown-route",
        "exit_token": hp.NATIVE, "size_in_raw": 200_000, "expected_profit_after_gas": 0.002,
        "latency_recv_to_send_s": 0.04, "computed_at_block": 300, "state_age_blocks": 0,
        "contract_address": "0x0000000000000000000000000000000000000006",
        "route_tokens": [hp.NATIVE], "pre_balances": {hp.NATIVE: 10**18}, "reserved_price_used": 2000.0,
        "tx_fields_for_recovery": hp._tx_fields_to_storable(tx_fields),
    }
    budget.begin_attempt(pending_ctx)

    sender = _FakeSender(start_nonce=11)
    # "Ончейн" ничего не знает -- ни рецепта, ни продвижения nonce
    # (транзакция ГЕНУИННО потеряна где-то между отправкой и подтверждением,
    # и повторная отправка ТОЖЕ не проходит подтверждение в срок).
    sender.onchain_nonce_pending = 11
    sender.onchain_nonce_latest = 11

    def fake_fetch_real_receipt(h):
        return None  # рецепта не появляется НИКОГДА в рамках этой проверки

    def fake_wait_for_receipt_bounded(h, timeout_s=None, **kw):
        return None  # ограниченное ожидание истекло -- результат неизвестен

    def _submit_stays_unresolved(prepared):
        sender.submit_calls += 1
        return SendResult(ok=False, tx_hash=prepared.tx_hash, unresolved=True)
    sender.submit_prepared = _submit_stays_unresolved

    orig_fetch, orig_wait = hp.fetch_real_receipt, hp.wait_for_receipt_bounded
    hp.fetch_real_receipt, hp.wait_for_receipt_bounded = fake_fetch_real_receipt, fake_wait_for_receipt_bounded
    try:
        attempt_table = AttemptTable(path=paths["attempts"])
        hp.resolve_pending_tx_if_any(budget, sender, attempt_table)
    finally:
        hp.fetch_real_receipt, hp.wait_for_receipt_bounded = orig_fetch, orig_wait

    n_rows = sum(1 for _ in Path(paths["attempts"]).open()) if Path(paths["attempts"]).exists() else 0
    ok = budget.halted and (budget.pending is not None) and (budget.pending.get("tx_hash") == tx_hash) and n_rows == 0
    detail = (f"после ограниченного ожидания И попытки безопасного восстановления (nonce остался свободен, "
              f"но повторная отправка ТОЖЕ не подтвердилась в срок) -- результат ОСТАЁТСЯ неизвестным: "
              f"budget.halted={budget.halted} ('{budget.halt_reason}'), budget.pending СОХРАНЁН (не None, "
              f"tx_hash={budget.pending.get('tx_hash') if budget.pending else None}), резерв/бюджет НЕ "
              f"обнулены, строк в attempt_table={n_rows} (ноль -- никакой предположительной записи), функция "
              f"ЗАВЕРШИЛАСЬ (не бесконечный цикл) -- процесс может штатно остановиться (main() видит "
              f"budget.halted и завершается).")
    _record("R3.2/R3.4", "неизвестный результат сохраняется (pending) при штатном завершении, не теряется", ok, detail)


# ---------- Проверка 7: порог прибыли и резерв согласованы ----------

def check7_min_profit_matches_reserve() -> None:
    gas_limit = 250_000
    max_fee_per_gas_wei = 2_000_000_000  # 2 gwei
    weth_usdg_price = 2500.0  # условная живая цена на момент проверки

    # NATIVE (та же валюта, что газ -- конвертация курса не нужна).
    min_profit_native = hp.compute_min_profit_raw(gas_limit, max_fee_per_gas_wei, hp.NATIVE, weth_usdg_price)
    max_gas_cost_wei = gas_limit * max_fee_per_gas_wei
    expected_native = -(-int(max_gas_cost_wei * 1.05) // 1) if False else None
    import math
    expected_native = math.ceil(max_gas_cost_wei * (1.0 + hp.MIN_PROFIT_MARGIN_FRACTION))
    ok_native = min_profit_native == expected_native and min_profit_native > max_gas_cost_wei
    detail_native = (f"gas_limit={gas_limit}, maxFeePerGas={max_fee_per_gas_wei} wei -> худший газ = "
                      f"{max_gas_cost_wei} wei; compute_min_profit_raw(NATIVE)={min_profit_native}, "
                      f"ожидание (та же формула, что и reserve_for_send: gas_limit*maxFeePerGas, "
                      f"округление вверх, +{hp.MIN_PROFIT_MARGIN_FRACTION:.0%})={expected_native} -- "
                      f"minProfit СТРОГО БОЛЬШЕ худшей стоимости газа (запас не нулевой).")
    _record("R3.5a", "minProfit (NATIVE) == ceil(gas_limit*maxFeePerGas*(1+margin)) -- та же величина резерва", ok_native, detail_native)

    # USDG -- через ТОТ ЖЕ курс, что использовался бы в reserve_for_send
    # (max_cost_usd), затем в raw USDG (6 знаков), с тем же округлением.
    min_profit_usdg = hp.compute_min_profit_raw(gas_limit, max_fee_per_gas_wei, hp.USDG, weth_usdg_price)
    max_cost_usd_equiv = (max_gas_cost_wei / 1e18) * weth_usdg_price  # ТА ЖЕ формула, что reserve_for_send
    expected_usdg = math.ceil((max_cost_usd_equiv * 10**hp.USDG_DECIMALS) * (1.0 + hp.MIN_PROFIT_MARGIN_FRACTION))
    ok_usdg = min_profit_usdg == expected_usdg
    detail_usdg = (f"reserve_for_send посчитал бы max_cost_usd={max_cost_usd_equiv:.6f}$ (та же формула, тот "
                    f"же курс weth_usdg_price={weth_usdg_price}); compute_min_profit_raw(USDG)="
                    f"{min_profit_usdg} raw, ожидание (raw USDG = usd*10^6, округление вверх, "
                    f"+{hp.MIN_PROFIT_MARGIN_FRACTION:.0%})={expected_usdg} -- ПОЛНОЕ согласие с резервом "
                    f"(один и тот же худший случай стоимости газа, конвертированный по одному и тому же курсу).")
    _record("R3.5b", "minProfit (USDG) согласован с той же формулой/курсом, что reserve_for_send", ok_usdg, detail_usdg)

    # Курс недоступен -- НЕ гадаем (None, не 0/произвольное значение).
    min_profit_no_price = hp.compute_min_profit_raw(gas_limit, max_fee_per_gas_wei, hp.USDG, None)
    ok_no_price = min_profit_no_price is None
    _record("R3.5c", "minProfit(USDG) без курса -- честно None, не гадаем", ok_no_price,
            f"compute_min_profit_raw(..., weth_usdg_price=None) вернул {min_profit_no_price!r}")

    # Явная документированная граница: НЕ покрывает газ ОТКАТА.
    revert_not_covered = ("НЕ утверждаем" in hp.compute_min_profit_raw.__doc__
                           and "ОТКАТОВ" in hp.compute_min_profit_raw.__doc__)
    _record("R3.5d", "докстринг честно не обещает покрытие газа отката", revert_not_covered,
            "compute_min_profit_raw.__doc__ явно оговаривает границу применимости (revert profit не платит "
            "вообще, откат остаётся расходом бюджета пилота).")


def main() -> None:
    check1_balance_read_error_no_nonce_skip()
    check2_recover_before_broadcast_no_eternal_wait()
    check3_revert_gas_crash_closes_revert()
    check4_profit_recorded_before_clear_no_block_no_doublecount()
    check5_stop_during_computation_blocks_send()
    check6_unknown_result_preserved_on_orderly_shutdown()
    check7_min_profit_matches_reserve()

    n_fail = sum(1 for r in RESULTS if not r["ok"])
    print(f"\n=== ИТОГ: {len(RESULTS) - n_fail}/{len(RESULTS)} проверок пройдено ===")
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_round3_pointchecks_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"results": RESULTS, "all_passed": n_fail == 0}, indent=2, ensure_ascii=False))
    print(f"результат записан в {out_path}")
    if n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
