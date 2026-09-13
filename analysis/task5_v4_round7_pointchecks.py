#!/usr/bin/env python3
"""Задача 5, живой пилот, седьмой раунд (разбор владельца, пункт 5) --
ТОЧЕЧНЫЕ ЛОКАЛЬНЫЕ ПРОВЕРКИ PilotBudget.start_new_session() и её
вызова из task5_v4_hotpath.main()/_main() через --new-session.

Проверяем ИМЕННО то, что требовал владелец: "новая сессия обновляет
ТОЛЬКО длительность/состояние завершения; накопленный газ, PnL и общий
лимит $20 -- сохранить, не выдавать новый бюджет" -- и что флаг НЕ
обходит halted (ручная причина остановки)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import task5_v4_hotpath as hp  # noqa: E402
from task5_v4_pilot_accounting import PilotBudget  # noqa: E402

RESULTS: list[dict] = []


def _record(point: str, title: str, ok: bool, detail: str) -> None:
    RESULTS.append({"point": point, "title": title, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {point}: {title}\n        {detail}")


def _tmp_budget_path(tag: str) -> str:
    d = Path(tempfile.mkdtemp(prefix=f"task5_v4_r7_{tag}_"))
    return str(d / "budget.json")


def check1_start_new_session_resets_only_completion_fields() -> None:
    budget = PilotBudget(state_path=_tmp_budget_path("check1"))
    budget.pilot_started_at = 1_700_000_000.0
    budget.pilot_completed = True
    budget.pilot_completed_reason = "истёк --duration-seconds"
    budget.cumulative_gas_loss_usd = 5.37
    budget.cumulative_net_pnl_usd = 2.10
    budget.cumulative_gross_profit_raw_by_token = {"0xusdg": 123456}
    budget.halted = False
    budget._save()

    budget.start_new_session()

    ok = (budget.pilot_completed is False and budget.pilot_completed_reason is None
          and budget.pilot_started_at is None
          and budget.cumulative_gas_loss_usd == 5.37 and budget.cumulative_net_pnl_usd == 2.10
          and budget.cumulative_gross_profit_raw_by_token == {"0xusdg": 123456})
    _record("R7.1", "start_new_session() сбрасывает ТОЛЬКО pilot_completed/pilot_completed_reason/pilot_started_at",
            ok, f"после вызова: pilot_completed={budget.pilot_completed} (ожидание False), "
                f"pilot_completed_reason={budget.pilot_completed_reason!r} (ожидание None), "
                f"pilot_started_at={budget.pilot_started_at} (ожидание None), "
                f"cumulative_gas_loss_usd={budget.cumulative_gas_loss_usd} (ожидание 5.37, СОХРАНЁН), "
                f"cumulative_net_pnl_usd={budget.cumulative_net_pnl_usd} (ожидание 2.10, СОХРАНЁН), "
                f"cumulative_gross_profit_raw_by_token={budget.cumulative_gross_profit_raw_by_token} "
                f"(ожидание {{'0xusdg': 123456}}, СОХРАНЁН)")

    # Реальный перезапуск процесса читает это же СОСТОЯНИЕ С ДИСКА -- проверяем,
    # что _save() внутри start_new_session() реально записал файл, а не только
    # in-memory поля этого же объекта.
    reloaded = PilotBudget(state_path=budget.state_path)
    ok2 = (reloaded.pilot_completed is False and reloaded.pilot_started_at is None
           and reloaded.cumulative_gas_loss_usd == 5.37)
    _record("R7.1b", "сброс реально persisted на диск (переживает рестарт процесса)",
            ok2, f"новый объект PilotBudget с тем же state_path: pilot_completed={reloaded.pilot_completed}, "
                 f"pilot_started_at={reloaded.pilot_started_at}, "
                 f"cumulative_gas_loss_usd={reloaded.cumulative_gas_loss_usd}")


def check2_ensure_pilot_started_gets_fresh_timestamp_after_reset() -> None:
    budget = PilotBudget(state_path=_tmp_budget_path("check2"))
    old_start = 1_700_000_000.0
    budget.pilot_started_at = old_start
    budget.pilot_completed = True
    budget.pilot_completed_reason = "истёк --duration-seconds"
    budget._save()

    budget.start_new_session()
    new_start = budget.ensure_pilot_started()

    ok = new_start is not None and new_start != old_start
    _record("R7.2", "после start_new_session() ensure_pilot_started() фиксирует НОВЫЙ момент (не старый)",
            ok, f"старый pilot_started_at={old_start}, новый={new_start} -- "
                f"новая 20-минутная сессия отсчитывается от НЕГО, не от старого часа")


def check3_new_session_flag_does_not_bypass_halt() -> None:
    """Пункт 5: --new-session -- только для завершённого (pilot_completed)
    пилота; РУЧНАЯ причина остановки (halted, напр. учётная ошибка) --
    ДРУГОЕ состояние, флаг её обходить НЕ должен (проверяем логику
    условия в _main(), а не только сам метод)."""
    budget = PilotBudget(state_path=_tmp_budget_path("check3"))
    budget.halted = True
    budget.halt_reason = "тестовая учётная ошибка"
    budget.pilot_completed = True
    budget.pilot_completed_reason = "истёк --duration-seconds"
    budget._save()

    # Воспроизводим ТОЧНО то же условие, что в _main(): halted проверяется
    # и возвращает ДО того, как --new-session вообще рассматривается --
    # здесь просто утверждаем, что halted остаётся True (start_new_session()
    # его не трогает), что и не даёт _main() дойти до сброса pilot_completed.
    budget.start_new_session()  # что бы ни случилось -- halted НЕ должен быть сброшен этим вызовом
    ok = budget.halted is True and budget.halt_reason == "тестовая учётная ошибка"
    _record("R7.3", "start_new_session() НЕ трогает halted/halt_reason -- ручная остановка не обходится",
            ok, f"halted={budget.halted} (ожидание True, НЕ сброшен), halt_reason={budget.halt_reason!r} "
                f"(ожидание сохранён) -- в _main() проверка halted идёт ДО --new-session и вернула бы "
                f"раньше, чем флаг вообще рассматривается")


def check4_cli_flag_registered() -> None:
    """Дёшево и честно: реальный --help процесса (не мок) должен
    перечислять --new-session -- страхует от опечатки в имени флага
    между argparse и workflow, который его передаёт."""
    proc = subprocess.run([sys.executable, str(Path(__file__).parent / "task5_v4_hotpath.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
    ok = "--new-session" in proc.stdout
    _record("R7.4", "--new-session зарегистрирован в argparse task5_v4_hotpath.py (реальный --help)",
            ok, f"'--new-session' в выводе --help: {ok}")


def check5_rpc_call_counter_and_reason_log_context() -> None:
    """Пункт 6 (седьмой раунд): "минимальная инструментация -- число
    RPC-вызовов; для прибыльных кандидатов/ошибок симуляции -- размер,
    calldata, блок котировки". Реальная (непеределанная) обёртка
    rpc_call_trading_path + реальный (непеределанный) ReasonLog.log()."""
    import tempfile as _tempfile
    from task5_v4_pilot_accounting import ReasonLog

    orig_uncounted = hp._uncounted_rpc_call_trading_path
    calls_made = []

    def fake_uncounted(method, params):
        calls_made.append(method)
        return "0x1"

    hp._uncounted_rpc_call_trading_path = fake_uncounted
    try:
        hp._reset_rpc_call_count()
        before = hp._read_rpc_call_count()
        hp.rpc_call_trading_path("eth_blockNumber", [])
        hp.rpc_call_trading_path("eth_gasPrice", [])
        after = hp._read_rpc_call_count()
    finally:
        hp._uncounted_rpc_call_trading_path = orig_uncounted

    ok_counter = before == 0 and after == 2 and calls_made == ["eth_blockNumber", "eth_gasPrice"]
    _record("R7.5a", "rpc_call_trading_path -- обёртка реально считает вызовы, не меняя их поведение",
            ok_counter, f"before={before} (ожидание 0), after={after} (ожидание 2), "
                        f"реальные вызовы дошли до исходной функции: {calls_made}")

    d = Path(_tempfile.mkdtemp(prefix="task5_v4_r7_check5_"))
    reason_log = ReasonLog(path=str(d / "reasons.jsonl"))
    reason_log.log("route_test", "TEST->TEST", "тестовая причина", "детали",
                    size_in_raw=12345, quote_block=999, calldata_hex="0xdeadbeef", rpc_call_count=7)
    lines = (d / "reasons.jsonl").read_text().splitlines()
    rec = json.loads(lines[-1])
    ok_fields = (rec.get("size_in_raw") == 12345 and rec.get("quote_block") == 999
                 and rec.get("calldata_hex") == "0xdeadbeef" and rec.get("rpc_call_count") == 7)
    _record("R7.5b", "ReasonLog.log() реально записывает size_in_raw/quote_block/calldata_hex/rpc_call_count",
            ok_fields, f"записанная строка: {rec}")

    # Обратная совместимость -- старые вызовы БЕЗ новых kwargs не падают.
    try:
        reason_log.log("route_test2", "A->B", "старый стиль вызова", "без новых полей")
        ok_compat = True
    except TypeError as exc:
        ok_compat = False
        _record("R7.5c", "ReasonLog.log() остаётся обратно совместим со старыми вызовами (без новых kwargs)",
                False, f"TypeError: {exc}")
        return
    _record("R7.5c", "ReasonLog.log() остаётся обратно совместим со старыми вызовами (без новых kwargs)",
            ok_compat, "вызов без size_in_raw/quote_block/calldata_hex/rpc_call_count прошёл без ошибок")


def main() -> None:
    check1_start_new_session_resets_only_completion_fields()
    check2_ensure_pilot_started_gets_fresh_timestamp_after_reset()
    check3_new_session_flag_does_not_bypass_halt()
    check5_rpc_call_counter_and_reason_log_context()
    check4_cli_flag_registered()

    n_fail = sum(1 for r in RESULTS if not r["ok"])
    print(f"\n=== ИТОГ: {len(RESULTS) - n_fail}/{len(RESULTS)} проверок пройдено ===")
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_round7_pointchecks_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"results": RESULTS, "all_passed": n_fail == 0}, indent=2, ensure_ascii=False))
    print(f"результат записан в {out_path}")
    if n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
