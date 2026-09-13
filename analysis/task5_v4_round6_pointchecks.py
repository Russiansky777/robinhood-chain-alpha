#!/usr/bin/env python3
"""Задача 5, живой пилот, шестой раунд -- ТОЧЕЧНЫЕ ЛОКАЛЬНЫЕ ПРОВЕРКИ
исправления пункта 3 ("профит(до газа) считается на блоке сигнала,
estimateGas -- на текущем состоянии, а 'свежая пере-котировка'
срабатывает только ПОСЛЕ отказа по газу, то есть никогда не спасает
кандидата, который уже отказал по первой, несогласованной паре") в
HotPath._evaluate_and_maybe_send.

ВАЖНО (тот же принцип, что round3/round4_pointchecks.py): НИКАКОЙ
реальной сети -- monkeypatch hp._rpc_call/hp.recompute_route/
hp.quote_route_at_size/hp.estimate_gas/hp.current_weth_usdg_price на
время каждой проверки, восстанавливаются в finally. Запускается
РЕАЛЬНЫЙ, непеределанный _evaluate_and_maybe_send.

Оба случая ниже -- block_number сигнала (1000) != eth_blockNumber на
момент оценки (1005), т.е. состояние РЕАЛЬНО успело сдвинуться между
детекцией и estimateGas (типичная задержка очереди/расчёта в пилоте).

R6.1 (ложный отказ БЕЗ правки): валовый профит на СИГНАЛЬНОМ блоке --
маленький (0.9 USDG, меньше стоимости газа 1.0 USDG) -- ДО правки
именно ЭТО значение сравнивалось бы с газом -> ложный отказ. РЕАЛЬНОЕ
согласованное состояние (блок 1005, тот же, на который резолвился
estimateGas) даёт БОЛЬШИЙ валовый профит (3.0 USDG) -- после газа
дейтвительно прибыльно. Правка обязана СЧИТАТЬ по согласованной паре и
ОТПРАВИТЬ.

R6.2 (ложная отправка БЕЗ правки, обратный случай -- более опасный):
валовый профит на СИГНАЛЬНОМ блоке -- большой (3.0 USDG) -- ДО правки
"выглядел бы" прибыльным после вычета газа (2.0 USDG после газа) и БЫЛ
БЫ отправлен. РЕАЛЬНОЕ согласованное состояние (блок 1005) даёт МЕНЬШИЙ
валовый профит (0.9 USDG) -- после газа УБЫТОЧНО. Правка обязана
пересчитать по согласованному состоянию и ОТКАЗАТЬ (не отправлять
потенциально убыточную сделку на основании рассинхронизированных
данных)."""
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
from task5_v4_pilot_accounting import AttemptTable, PilotBudget, ReasonLog  # noqa: E402

_TEST_PRIVKEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # тестовый ключ Anvil #0
_TEST_ACCOUNT = Account.from_key(_TEST_PRIVKEY)

RESULTS: list[dict] = []


def _record(point: str, title: str, ok: bool, detail: str) -> None:
    RESULTS.append({"point": point, "title": title, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {point}: {title}\n        {detail}")


def _tmp_paths(tag: str) -> dict:
    d = Path(tempfile.mkdtemp(prefix=f"task5_v4_r6_{tag}_"))
    return {"budget": str(d / "budget.json"), "attempts": str(d / "attempts.jsonl"), "reasons": str(d / "reasons.jsonl")}


class _FakeSender:
    def __init__(self, start_nonce: int = 5) -> None:
        self.address = _TEST_ACCOUNT.address
        self.state_nonce = start_nonce
        self.prepare_calls: list[dict] = []
        self.submit_calls = 0

    def can_send(self):
        return True, ""

    def prepare_transaction_fields(self, to, calldata, gas_limit, value_wei=0):
        tx = {"type": 2, "chainId": 4663, "nonce": self.state_nonce, "to": Web3.to_checksum_address(to),
              "value": value_wei, "gas": gas_limit, "maxFeePerGas": 2_000_000_000, "maxPriorityFeePerGas": 100_000_000,
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

    def submit_prepared(self, prepared):
        self.submit_calls += 1
        return SendResult(ok=True, tx_hash=prepared.tx_hash, status=1, gas_used=200_000, block_number=1005)


def _fake_route() -> rr.RouteCycle:
    fake_token = "0x0000000000000000000000000000000000000abc"
    assert len(fake_token) == 42, len(fake_token)  # < USDG numerically -- currency0
    leg_a = rr.RouteLeg(fake_token, rr.USDG, 3000, 60, rr.NATIVE, False)  # input=currency1=USDG, output=fake_token
    leg_b = rr.RouteLeg(fake_token, rr.USDG, 3000, 60, rr.NATIVE, True)   # input=currency0=fake_token, output=USDG
    return rr.RouteCycle("route_test_r6", (leg_a, leg_b), rr.USDG, "USDG<->TEST", "seed")


def _run_evaluate(stale_profit_raw: int, fresh_profit_raw: int, sender: _FakeSender, budget: PilotBudget,
                   attempt_table: AttemptTable, reason_log: ReasonLog) -> None:
    """Общий каркас: сигнал на блоке 1000, состояние на момент estimateGas
    (и последующей пере-котировки) -- РЕАЛЬНО сдвинулось на 1005."""
    route = _fake_route()
    registry = rr.RouteRegistry()
    registry.add_route(route)

    orig_rpc_call = hp._rpc_call
    orig_rpc_call_trading_path = hp.rpc_call_trading_path  # ПРАВКА (шестой раунд, пункт 5A): отдельная быстрая полоса
    orig_recompute = hp.recompute_route
    orig_quote_at_size = hp.quote_route_at_size
    orig_estimate_gas = hp.estimate_gas
    orig_weth_price = hp.current_weth_usdg_price
    orig_token_balance = hp._token_balance
    orig_build_calldata = hp.build_execute_cycle_calldata
    orig_fetch_receipt = hp.fetch_real_receipt

    quote_at_size_calls: list[tuple[int, int]] = []

    def fake_rpc_call(method, params):
        if method == "eth_blockNumber":
            return hex(1005)  # РЕАЛЬНО сдвинулось относительно сигнального блока 1000
        if method == "eth_gasPrice":
            return hex(2_000_000_000)  # 2 gwei
        raise AssertionError(f"неожиданный _rpc_call в тесте: {method}")

    def fake_recompute(route, block_number):
        assert block_number == 1000, "recompute_route обязан использовать блок СИГНАЛА, не latest"
        return {"ok": True, "amount_in": 1_000_000, "amount_out": 1_000_000 + stale_profit_raw,
                "profit_raw": stale_profit_raw}

    def fake_quote_at_size(route, amount_in, block_number):
        quote_at_size_calls.append((amount_in, block_number))
        assert block_number == 1005, "согласованная пере-котировка обязана идти на ТОМ ЖЕ блоке, что estimateGas"
        return {"ok": True, "amount_in": amount_in, "amount_out": amount_in + fresh_profit_raw,
                "profit_raw": fresh_profit_raw}

    hp._rpc_call = fake_rpc_call
    hp.rpc_call_trading_path = fake_rpc_call  # _evaluate_and_maybe_send теперь зовёт именно эту функцию
    hp.recompute_route = fake_recompute
    hp.quote_route_at_size = fake_quote_at_size
    hp.estimate_gas = lambda addr, calldata, frm, block_number=None: {"ok": True, "gas_estimate": 200_000}  # -> 1.0 USDG газа при 2500 WETH/USDG
    hp.current_weth_usdg_price = lambda: 2500.0
    hp._token_balance = lambda token, account: 0
    hp.build_execute_cycle_calldata = lambda route, amt, min_profit, sqrt_price_limits=None: (
        b"\x00\x00\x00\x00" + str(min_profit).encode())
    hp.fetch_real_receipt = lambda tx_hash: {"status": "0x1", "gasUsed": "0x30d40", "blockNumber": 1005, "logs": []}

    try:
        hotpath = hp.HotPath(registry, "0x0000000000000000000000000000000000000009", sender.address,
                              budget, attempt_table, reason_log, hp._RpcPriorityHint(), sender=sender, dry_run=False)
        hotpath._evaluate_and_maybe_send(route, 1000, hp.time.monotonic(), hp.time.monotonic())
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
    return quote_at_size_calls


def check1_stale_negative_fresh_positive_sends() -> None:
    paths = _tmp_paths("check1")
    budget = PilotBudget(state_path=paths["budget"])
    reason_log = ReasonLog(path=paths["reasons"])
    attempt_table = AttemptTable(path=paths["attempts"])
    sender = _FakeSender(start_nonce=1)

    # газ = 200000 * 2e9 / 1e18 ETH * 2500 USDG/ETH = 1.0 USDG-эквивалент
    stale_profit_raw = 900_000     # 0.9 USDG -- 0.9-1.0 = -0.1 (ложный отказ БЕЗ правки)
    fresh_profit_raw = 3_000_000   # 3.0 USDG -- 3.0-1.0 = +2.0 (реально прибыльно на согласованном блоке)

    calls = _run_evaluate(stale_profit_raw, fresh_profit_raw, sender, budget, attempt_table, reason_log)
    ok = len(calls) >= 1 and sender.submit_calls == 1
    _record("R6.1", "профит(до газа) на сигнальном блоке мал/отрицателен относительно газа с latest, "
                     "но СОГЛАСОВАННАЯ пере-котировка на том же latest реально прибыльна -- ОТПРАВЛЯЕМ "
                     "(до правки: сравнение stale-профита с latest-газом дало бы ложный отказ)",
            ok, f"quote_route_at_size вызван на согласованном блоке {calls} раз(а); "
                f"submit_prepared вызван {sender.submit_calls} раз (ожидание 1) -- кандидат, который БЕЗ "
                f"правки был бы ложно отклонён (0.9 USDG gross - 1.0 USDG газа = -0.1), с правкой "
                f"пересчитан на согласованном состоянии (3.0 USDG gross - 1.0 USDG газа = +2.0) и отправлен.")


def check2_stale_positive_fresh_negative_rejects() -> None:
    paths = _tmp_paths("check2")
    budget = PilotBudget(state_path=paths["budget"])
    reason_log = ReasonLog(path=paths["reasons"])
    attempt_table = AttemptTable(path=paths["attempts"])
    sender = _FakeSender(start_nonce=1)

    stale_profit_raw = 3_000_000   # 3.0 USDG -- "выглядело бы" прибыльным (3.0-1.0=+2.0) БЕЗ правки -> отправили бы
    fresh_profit_raw = 900_000     # 0.9 USDG -- РЕАЛЬНО на согласованном состоянии убыточно (0.9-1.0=-0.1)

    reason_log_entries_before = len(list(Path(paths["reasons"]).open())) if Path(paths["reasons"]).exists() else 0
    calls = _run_evaluate(stale_profit_raw, fresh_profit_raw, sender, budget, attempt_table, reason_log)

    reason_lines = Path(paths["reasons"]).read_text().splitlines() if Path(paths["reasons"]).exists() else []
    last_reason = json.loads(reason_lines[-1]) if reason_lines else {}
    mentions_consistent_block = "1005" in last_reason.get("detail", "") and "1000" in last_reason.get("detail", "")

    ok = len(calls) >= 1 and sender.submit_calls == 0 and mentions_consistent_block
    _record("R6.2", "профит(до газа) на сигнальном блоке выглядит выгодным относительно latest-газа, но "
                     "СОГЛАСОВАННАЯ пере-котировка на том же latest реально убыточна -- ОТКАЗЫВАЕМ (до правки: "
                     "отправили бы потенциально убыточную сделку на рассинхронизированных данных)",
            ok, f"quote_route_at_size вызван на согласованном блоке {calls} раз(а); submit_prepared вызван "
                f"{sender.submit_calls} раз (ожидание 0). Причина отказа честно называет ОБА блока: "
                f"{last_reason.get('detail', '')!r} (согласованный блок 1005 и сигнальный 1000 оба упомянуты: "
                f"{mentions_consistent_block}) -- кандидат, который БЕЗ правки выглядел бы прибыльным "
                f"(3.0 USDG gross - 1.0 USDG газа = +2.0) и был бы отправлен, с правкой честно пересчитан на "
                f"согласованном состоянии (0.9 USDG gross - 1.0 USDG газа = -0.1) и НЕ отправлен.")


# ---------- Проверка 3 (пункт 5B): multihop -- только для hookless-маршрутов ----------

def check3_multihop_only_for_hookless_routes() -> None:
    """quote_route_at_size (РЕАЛЬНЫЙ, непеределанный) обязан идти
    ОДНИМ eth_call (quoteExactInput) для маршрута БЕЗ hooks, и СТАРЫМ
    последовательным путём (quote_exact_input_single на каждое плечо)
    для маршрута С hooks -- НЕ провалидированная многоходовая
    котировка не должна тихо применяться к hook-маршрутам."""
    HOOK_ADDR = "0x00000000000000000000000000000000009999"
    fake_token = "0x0000000000000000000000000000000000000abc"

    hookless_route = rr.RouteCycle(
        "route_test_hookless", (
            rr.RouteLeg(fake_token, rr.USDG, 3000, 60, rr.NATIVE, False),
            rr.RouteLeg(fake_token, rr.USDG, 3000, 60, rr.NATIVE, True),
        ), rr.USDG, "hookless", "seed")
    hooked_route = rr.RouteCycle(
        "route_test_hooked", (
            rr.RouteLeg(fake_token, rr.USDG, 3000, 60, HOOK_ADDR, False),
            rr.RouteLeg(fake_token, rr.USDG, 3000, 60, rr.NATIVE, True),
        ), rr.USDG, "hooked", "seed")

    orig_rpc_call_trading_path = hp.rpc_call_trading_path
    orig_quote_single = hp.quote_exact_input_single
    orig_decode = hp.decode_quote_result

    multihop_calls: list[str] = []
    sequential_calls: list[str] = []

    def fake_rpc_call_trading_path(method, params):
        multihop_calls.append(method)
        assert method == "eth_call"
        return "0xdeadbeef"

    def fake_quote_single(pool_key, zero_for_one, amount_in, block_number):
        sequential_calls.append(pool_key.currency0)
        return amount_in + 1

    hp.rpc_call_trading_path = fake_rpc_call_trading_path
    hp.quote_exact_input_single = fake_quote_single
    hp.decode_quote_result = lambda raw: (2_000_000, 100_000)

    try:
        res_hookless = hp.quote_route_at_size(hookless_route, 1_000_000, 5000)
        multihop_after_hookless, sequential_after_hookless = len(multihop_calls), len(sequential_calls)
        res_hooked = hp.quote_route_at_size(hooked_route, 1_000_000, 5000)
        multihop_after_hooked, sequential_after_hooked = len(multihop_calls), len(sequential_calls)
    finally:
        hp.rpc_call_trading_path = orig_rpc_call_trading_path
        hp.quote_exact_input_single = orig_quote_single
        hp.decode_quote_result = orig_decode

    ok = (multihop_after_hookless == 1 and sequential_after_hookless == 0 and res_hookless["ok"]
          and res_hookless["amount_out"] == 2_000_000)
    _record("R6.3a", "hookless-маршрут -- ОДИН eth_call (quoteExactInput), НЕ последовательные quoteExactInputSingle",
            ok, f"multihop-вызовов={multihop_after_hookless} (ожидание 1), sequential-вызовов="
                f"{sequential_after_hookless} (ожидание 0), результат={res_hookless}")

    ok2 = (sequential_after_hooked - sequential_after_hookless == 2
           and multihop_after_hooked == multihop_after_hookless  # hook-маршрут НЕ добавил ни одного multihop-вызова
           and res_hooked["ok"] and res_hooked["amount_out"] == 1_000_002)
    _record("R6.3b", "маршрут С hooks -- ПРЕЖНИЙ последовательный путь (multihop НЕ провалидирован для hooks)",
            ok2, f"sequential-вызовов ОТ ЭТОГО вызова={sequential_after_hooked - sequential_after_hookless} "
                 f"(ожидание 2, по одному на плечо), multihop-вызовов ОТ ЭТОГО вызова="
                 f"{multihop_after_hooked - multihop_after_hookless} (ожидание 0), результат={res_hooked}")


def main() -> None:
    check1_stale_negative_fresh_positive_sends()
    check2_stale_positive_fresh_negative_rejects()
    check3_multihop_only_for_hookless_routes()

    n_fail = sum(1 for r in RESULTS if not r["ok"])
    print(f"\n=== ИТОГ: {len(RESULTS) - n_fail}/{len(RESULTS)} проверок пройдено ===")
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_round6_pointchecks_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"results": RESULTS, "all_passed": n_fail == 0}, indent=2, ensure_ascii=False))
    print(f"результат записан в {out_path}")
    if n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
