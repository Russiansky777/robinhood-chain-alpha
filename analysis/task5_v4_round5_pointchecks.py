#!/usr/bin/env python3
"""Задача 5, живой пилот -- ТОЧЕЧНЫЕ ЛОКАЛЬНЫЕ ПРОВЕРКИ пятого раунда
внешнего ревью (владелец, 2026-09-13, после подтверждения коммита
83b2d030e6c98c86eae8369bf4a010c08c9d1c9e): "Проверки -- только затронутые
случаи":
  1. Ошибка чтения nonce (describe_nonce_state бросает исключение) --
     запрещает запуск (budget.halt), а не print+return.
  2. Отсутствующее/неверное событие CycleExecuted -- НЕ начисляет
     прибыль (разница балансов НЕ используется как замена), газ
     ОСТАЁТСЯ учтённым, pending сохраняется, halt с явной причиной.
  3. Ошибка большого размера в СЕТКЕ (100/300/1000 USDG) сохраняет уже
     найденный лучший МЕНЬШИЙ размер; выгодный больший размер МОЖЕТ
     быть выбран, если он реально котируется лучше.

ВАЖНО: НИКАКОЙ реальной сети, НИКАКИХ реальных денег -- сетевые функции
ЗАМЕНЯЮТСЯ (monkeypatch) на время каждой проверки. Тесты вызывают
РЕАЛЬНЫЙ код (verify_nonce_consistency, _recover_profit_half,
recompute_route), не переизобретают его логику."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import task5_v4_hotpath as hp  # noqa: E402
import task5_v4_route_registry as rr  # noqa: E402
from task5_v4_pilot_accounting import AttemptTable, PilotBudget  # noqa: E402

RESULTS: list[dict] = []


def _record(point: str, title: str, ok: bool, detail: str) -> None:
    RESULTS.append({"point": point, "title": title, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {point}: {title}\n        {detail}")


def _tmp_paths(tag: str) -> dict:
    d = Path(tempfile.mkdtemp(prefix=f"task5_v4_r5_{tag}_"))
    return {"budget": str(d / "budget.json"), "attempts": str(d / "attempts.jsonl")}


class _RaisingSender:
    """describe_nonce_state() бросает исключение -- имитирует реальный
    сбой RPC ПРИ проверке nonce на старте (не при отправке)."""

    def describe_nonce_state(self):
        raise RuntimeError("имитация: RPC недоступен для eth_getTransactionCount")


# ---------- Проверка 1: ошибка чтения nonce запрещает запуск ----------

def check1_nonce_read_failure_halts() -> None:
    paths = _tmp_paths("check1")
    budget = PilotBudget(state_path=paths["budget"])
    ok_before = budget.can_send()[0]
    hp.verify_nonce_consistency(_RaisingSender(), budget)
    can_send_after, why = budget.can_send()
    ok = ok_before is True and budget.halted is True and can_send_after is False
    _record("R5.1", "исключение describe_nonce_state() запрещает запуск (halt), не print+return", ok,
            f"ДО проверки can_send={ok_before}; ПОСЛЕ сбоя verify_nonce_consistency: budget.halted="
            f"{budget.halted} ('{budget.halt_reason}'), can_send()={can_send_after} -- пилот НЕ может начать "
            f"торговать со старым, непроверенным nonce.")


# ---------- Проверка 2: отсутствующее событие -- профит не начисляется, газ сохранён ----------

def check2_missing_event_no_profit_gas_kept() -> None:
    paths = _tmp_paths("check2")
    budget = PilotBudget(state_path=paths["budget"])
    attempt_table = AttemptTable(path=paths["attempts"])

    budget.begin_attempt({
        "tx_hash": "0xmissingevent1", "nonce": 3, "route_id": "r1", "route_label": "missing-event-route",
        "exit_token": rr.USDG, "size_in_raw": 5_000_000, "expected_profit_after_gas": 0.5,
        "latency_recv_to_send_s": 0.02, "computed_at_block": 500, "state_age_blocks": 0,
        "contract_address": "0x0000000000000000000000000000000000000009",
        "route_tokens": [rr.USDG.lower()], "pre_balances": {rr.USDG.lower(): 1_000_000},
        "reserved_price_used": 2500.0,
    })
    # Реальный finalize_gas -- успех (tx_status=1), газ учитывается ДО
    # какой-либо попытки определить прибыль (как в реальном коде).
    gas_res = budget.finalize_gas(tx_status=1, gas_used=180_000, effective_gas_price=2_000_000_000,
                                   eth_usd_price=2500.0)
    assert gas_res.get("resolved") and gas_res.get("awaiting_profit")
    gas_loss_before = budget.cumulative_gas_loss_usd
    pnl_before = budget.cumulative_net_pnl_usd

    # Рецепт БЕЗ CycleExecuted (logs пуст) -- ИМЕННО "отсутствующее событие".
    fake_receipt = {"status": "0x1", "gasUsed": "0x2bf20", "blockNumber": 500, "logs": []}

    orig_token_balance = hp._token_balance
    hp._token_balance = lambda token, account: 999_000_000  # "БОЛЬШОЙ" latest-баланс -- контаминирован другими tx

    try:
        hp._recover_profit_half(budget, sender=None, attempt_table=attempt_table, p=budget.pending, receipt=fake_receipt)
    finally:
        hp._token_balance = orig_token_balance

    n_rows = sum(1 for _ in Path(paths["attempts"]).open()) if Path(paths["attempts"]).exists() else 0
    profit_untouched = budget.cumulative_gross_profit_raw_by_token.get(rr.USDG, 0) == 0
    gas_kept = budget.cumulative_gas_loss_usd == gas_loss_before and gas_loss_before > 0
    pnl_only_gas = budget.cumulative_net_pnl_usd == pnl_before  # НЕ изменился доп. "профитом" от balance-diff
    ok = (budget.halted and "CycleExecuted" in (budget.halt_reason or "") and budget.pending is not None
          and profit_untouched and gas_kept and n_rows == 0)
    detail = (f"рецепт БЕЗ CycleExecuted (logs=[]), фейковый _token_balance вернул бы ОГРОМНУЮ 'прибыль' "
              f"({999_000_000 - 1_000_000} raw), если бы использовался как замена -- РЕАЛЬНЫЙ код НЕ должен "
              f"был её принять. budget.halted={budget.halted} ('{budget.halt_reason}'), "
              f"budget.pending СОХРАНЁН={budget.pending is not None}, "
              f"cumulative_gross_profit_raw_by_token[USDG]={budget.cumulative_gross_profit_raw_by_token.get(rr.USDG, 0)} "
              f"(ожидание 0 -- профит НЕ начислен), газ ОСТАЛСЯ учтён: "
              f"${budget.cumulative_gas_loss_usd:.4f} (было ${gas_loss_before:.4f}, > 0), "
              f"net_pnl_usd не изменился доп. профитом: {pnl_only_gas}, строк в attempt_table={n_rows} "
              f"(ожидание 0 -- не финализирована, дублирования при восстановлении не будет).")
    _record("R5.2", "отсутствующее CycleExecuted не начисляет прибыль по balance-diff, газ сохраняется", ok, detail)


# ---------- Проверка 3: ошибка большого размера сохраняет лучший меньший ----------

def check3_large_size_failure_keeps_best_smaller() -> None:
    # zero_for_one=False на leg1 -> input_currency=currency1=USDG (не
    # NATIVE) -- recompute_route выбирает грид ПО СТАРТОВОМУ токену
    # (route.legs[0].input_currency), для этой проверки он ОБЯЗАН быть
    # USDG, иначе используется ETH-сетка и все USDG-размеры фейка мимо.
    leg1 = rr.RouteLeg(rr.NATIVE, rr.USDG, 3000, 60, rr.NATIVE, False)
    leg2 = rr.RouteLeg(rr.NATIVE, rr.USDG, 3000, 60, rr.NATIVE, True)
    route = rr.RouteCycle("route_r5_grid_test", (leg1, leg2), rr.USDG, "r5-grid-test", "seed")
    assert route.legs[0].input_currency.lower() == rr.USDG.lower(), "фикстура теста должна стартовать с USDG"

    grid = hp.SIZE_GRID_BY_START_TOKEN[rr.USDG.lower()]
    ok_grid_extended = grid[:8] == [1_000_000, 3_000_000, 5_000_000, 7_000_000, 10_000_000, 15_000_000,
                                     20_000_000, 30_000_000] and 100_000_000 in grid and 300_000_000 in grid \
        and 1_000_000_000 in grid
    _record("R5.3a", "сетка USDG расширена (100/300/1000 USDG), прежние размеры сохранены", ok_grid_extended,
            f"SIZE_GRID_BY_START_TOKEN[USDG]={grid}")

    # Сценарий А: 5M -- лучший котируемый (профит 10), 100M -- "не
    # котируется" (NotEnoughLiquidity) -- best НЕ должен теряться.
    def fake_quote_a(route, amount_in, block_number):
        if amount_in == 5_000_000:
            return {"ok": True, "amount_in": amount_in, "amount_out": 5_000_010, "profit_raw": 10}
        if amount_in in (1_000_000, 3_000_000):
            return {"ok": True, "amount_in": amount_in, "amount_out": amount_in + 1, "profit_raw": 1}
        if amount_in == 100_000_000:
            return {"ok": False, "reason": hp.REASON_NO_LIQUIDITY, "detail": "execution reverted: NotEnoughLiquidity"}
        return {"ok": False, "reason": hp.REASON_NO_LIQUIDITY, "detail": "execution reverted: NotEnoughLiquidity"}

    orig_quote = hp.quote_route_at_size
    hp.quote_route_at_size = fake_quote_a
    try:
        res_a = hp.recompute_route(route, 100)
    finally:
        hp.quote_route_at_size = orig_quote
    ok_a = res_a["ok"] and res_a["amount_in"] == 5_000_000 and res_a["profit_raw"] == 10
    _record("R5.3b", "100 USDG не котируется (NotEnoughLiquidity) -- лучший меньший (5 USDG) не теряется",
            ok_a, f"recompute_route вернул: {res_a}")

    # Сценарий Б: 300M реально котируется ВЫГОДНЕЕ, чем все меньшие --
    # ДОЛЖЕН быть выбран (новые размеры позволяют выбрать крупный, если
    # он реально лучше, не обязывают к этому и не мешают).
    def fake_quote_b(route, amount_in, block_number):
        if amount_in == 300_000_000:
            return {"ok": True, "amount_in": amount_in, "amount_out": 300_050_000, "profit_raw": 50_000}
        if amount_in == 1_000_000_000:
            return {"ok": False, "reason": hp.REASON_NO_LIQUIDITY, "detail": "execution reverted: NotEnoughLiquidity"}
        return {"ok": True, "amount_in": amount_in, "amount_out": amount_in + 5, "profit_raw": 5}

    hp.quote_route_at_size = fake_quote_b
    try:
        res_b = hp.recompute_route(route, 100)
    finally:
        hp.quote_route_at_size = orig_quote
    ok_b = res_b["ok"] and res_b["amount_in"] == 300_000_000 and res_b["profit_raw"] == 50_000
    _record("R5.3c", "выгодный больший размер (300 USDG) реально выбирается, когда он лучше", ok_b,
            f"recompute_route вернул: {res_b}")


def main() -> None:
    check1_nonce_read_failure_halts()
    check2_missing_event_no_profit_gas_kept()
    check3_large_size_failure_keeps_best_smaller()

    n_fail = sum(1 for r in RESULTS if not r["ok"])
    print(f"\n=== ИТОГ: {len(RESULTS) - n_fail}/{len(RESULTS)} проверок пройдено ===")
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_round5_pointchecks_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"results": RESULTS, "all_passed": n_fail == 0}, indent=2, ensure_ascii=False))
    print(f"результат записан в {out_path}")
    if n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
