#!/usr/bin/env python3
"""Задача 5, живой пилот, восьмой раунд (разбор владельца, пункт 2) --
ТОЧЕЧНЫЕ ЛОКАЛЬНЫЕ ПРОВЕРКИ измерений времени: "для каждого реально
начатого расчёта -- route_id, блок сигнала, время получения/постановки
в очередь, начало и конец расчёта, итоговую причину, число RPC-вызовов;
для отправки -- время передачи; длительности -- монотонными часами;
пиши результат ТАКЖЕ при раннем отказе и исключении".

НИКАКОЙ реальной сети -- monkeypatch на время каждой проверки,
восстанавливается в finally. Запускается РЕАЛЬНЫЙ, непеределанный
HotPath._evaluate_and_maybe_send / HotPath.evaluator_loop (второй тест
реально гоняет evaluator_loop в отдельном потоке -- не копирует его
логику в тесте)."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import task5_v4_hotpath as hp  # noqa: E402
import task5_v4_route_registry as rr  # noqa: E402
from task5_v4_pilot_accounting import AttemptTable, PilotBudget, ReasonLog  # noqa: E402

RESULTS: list[dict] = []


def _record(point: str, title: str, ok: bool, detail: str) -> None:
    RESULTS.append({"point": point, "title": title, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {point}: {title}\n        {detail}")


def _tmp_paths(tag: str) -> dict:
    d = Path(tempfile.mkdtemp(prefix=f"task5_v4_r8_{tag}_"))
    return {"budget": str(d / "budget.json"), "attempts": str(d / "attempts.jsonl"), "reasons": str(d / "reasons.jsonl")}


def _fake_route() -> rr.RouteCycle:
    fake_token = "0x0000000000000000000000000000000000000abc"
    leg_a = rr.RouteLeg(fake_token, rr.USDG, 3000, 60, rr.NATIVE, False)
    leg_b = rr.RouteLeg(fake_token, rr.USDG, 3000, 60, rr.NATIVE, True)
    return rr.RouteCycle("route_test_r8", (leg_a, leg_b), rr.USDG, "USDG<->TEST", "seed")


def check1_early_reject_records_full_timing() -> None:
    """recompute_route сразу отказывает (не прибыльно) -- САМЫЙ ранний
    выход из _evaluate_and_maybe_send. Проверяем, что reason_log-запись
    всё равно несёт signal_block/queue_wait_s/calc_duration_s/
    rpc_call_count -- не только "поздние" отказы."""
    paths = _tmp_paths("check1")
    budget = PilotBudget(state_path=paths["budget"])
    reason_log = ReasonLog(path=paths["reasons"])
    attempt_table = AttemptTable(path=paths["attempts"])
    route = _fake_route()
    registry = rr.RouteRegistry()
    registry.add_route(route)

    orig_recompute = hp.recompute_route
    hp.recompute_route = lambda route, block: {"ok": True, "amount_in": 1_000_000, "amount_out": 999_000,
                                                "profit_raw": -1_000}  # убыточно -- самый ранний выход
    try:
        hotpath = hp.HotPath(registry, "0x0000000000000000000000000000000000000009", "0x0",
                              budget, attempt_table, reason_log, hp._RpcPriorityHint(), sender=None, dry_run=True)
        recv_t = time.monotonic()
        time.sleep(0.05)
        t_dequeued = time.monotonic()
        time.sleep(0.03)
        hotpath._evaluate_and_maybe_send(route, 4242, recv_t, t_dequeued)
    finally:
        hp.recompute_route = orig_recompute

    lines = Path(paths["reasons"]).read_text().splitlines()
    rec = json.loads(lines[-1])
    ok = (rec.get("signal_block") == 4242 and rec.get("queue_wait_s") is not None
          and rec.get("queue_wait_s") > 0 and rec.get("calc_duration_s") is not None
          and rec.get("calc_duration_s") > 0 and rec.get("rpc_call_count") is not None
          and rec.get("size_in_raw") == 1_000_000)
    _record("R8.1", "самый ранний отказ (recompute_route<=0) -- всё равно несёт полную временную инструментацию",
            ok, f"signal_block={rec.get('signal_block')} (ожидание 4242), "
                f"queue_wait_s={rec.get('queue_wait_s')} (ожидание >0, ~0.05с), "
                f"calc_duration_s={rec.get('calc_duration_s')} (ожидание >0, ~0.03с), "
                f"rpc_call_count={rec.get('rpc_call_count')}, size_in_raw={rec.get('size_in_raw')} "
                f"(ожидание 1000000) -- запись: {rec}")


def check2_exception_during_evaluation_is_recorded() -> None:
    """Пункт 2: "пиши результат ТАКЖЕ при исключении" -- реально гоняем
    evaluator_loop (не копия его логики) в отдельном потоке, форсируем
    исключение внутри recompute_route, проверяем, что reason_log
    получил запись (а не только stderr, как было ДО правки)."""
    paths = _tmp_paths("check2")
    budget = PilotBudget(state_path=paths["budget"])
    reason_log = ReasonLog(path=paths["reasons"])
    attempt_table = AttemptTable(path=paths["attempts"])
    route = _fake_route()
    registry = rr.RouteRegistry()
    registry.add_route(route)

    orig_recompute = hp.recompute_route

    def raising_recompute(route, block):
        raise RuntimeError("тестовое необработанное исключение (имитация сетевого сбоя внутри расчёта)")

    hp.recompute_route = raising_recompute
    hotpath = hp.HotPath(registry, "0x0000000000000000000000000000000000000009", "0x0",
                          budget, attempt_table, reason_log, hp._RpcPriorityHint(), sender=None, dry_run=True)
    thread = threading.Thread(target=hotpath.evaluator_loop, daemon=True)
    try:
        thread.start()
        hotpath._queue.mark(route.route_id, 5050, time.monotonic())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if Path(paths["reasons"]).exists() and Path(paths["reasons"]).read_text().strip():
                break
            time.sleep(0.05)
    finally:
        hotpath.stop()
        thread.join(timeout=3.0)
        hp.recompute_route = orig_recompute

    reasons_path = Path(paths["reasons"])
    ok = reasons_path.exists() and bool(reasons_path.read_text().strip())
    rec = json.loads(reasons_path.read_text().splitlines()[-1]) if ok else {}
    ok = ok and rec.get("reason") == hp.REASON_CALC_ERROR and "тестовое необработанное исключение" in rec.get("detail", "")
    ok = ok and rec.get("signal_block") == 5050 and rec.get("queue_wait_s") is not None
    _record("R8.2", "необработанное исключение внутри оценки -- РЕАЛЬНЫЙ evaluator_loop пишет в reason_log "
                    "(раньше уходило ТОЛЬКО в stderr, молча для журнала)",
            ok, f"reason_log содержит запись: {ok}, запись: {rec}")


def main() -> None:
    check1_early_reject_records_full_timing()
    check2_exception_during_evaluation_is_recorded()

    n_fail = sum(1 for r in RESULTS if not r["ok"])
    print(f"\n=== ИТОГ: {len(RESULTS) - n_fail}/{len(RESULTS)} проверок пройдено ===")
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_round8_pointchecks_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"results": RESULTS, "all_passed": n_fail == 0}, indent=2, ensure_ascii=False))
    print(f"результат записан в {out_path}")
    if n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
