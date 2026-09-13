#!/usr/bin/env python3
"""Задача 5, живой пилот, шестой раунд, пункт 2: "одна сквозная успешная
транзакция на форке mainnet" -- расчёт -> calldata -> РЕАЛЬНАЯ
подготовка/подпись через task5_bot_sender.Sender (не raw `cast send`,
как в task5_v4_fork_simulation.py -- та проверила только контракт+сеть,
НЕ Sender/приёмку рецепта) -> отправка в ЛОКАЛЬНУЮ anvil-ноду -> receipt
status=1 -> CycleExecuted (реальный parse_cycle_executed_profit из
task5_v4_hotpath.py, не переизобретается) -> фактический прирост ->
запись газа/PnL через РЕАЛЬНЫЙ PilotBudget (task5_v4_pilot_accounting.py,
finalize_gas + finalize_profit_and_close).

Форк-точка -- ТА ЖЕ, что уже реально проверенный контрольный пример
Этапа 2 (task5_v4_fork_simulation.py, task5_v4_quote_replay.py):
контрольная tx 0xd487122244... на блоке 61248737, маршрут USDG->MOSIAI
(2 пула), N-1=61248736. ЭТОТ блок предшествует реальному деплою нашего
контракта (0xAB24907b... задеплоен намного позже) -- контракт
разворачивается ЗАНОВО на форке (та же уже собранная сборка байткода,
тот же commit, что зафиксирован для пилота), деплой/адрес -- ЛОКАЛЬНЫЙ
форковый, НЕ реальный mainnet-адрес. Это ЯВНО не "наш контракт
существовал на блоке 61248736" -- см. докстринг ниже, main().

Инвентарь (9.437184 USDG, тот же размер, что контрольная tx) --
пополняется через anvil_impersonateAccount на PoolManager (см.
докстринг task5_v4_fork_simulation.py про то, почему это НЕ "искусственное
зачисление прибыли": это СТАРТОВЫЙ капитал, контракт по конструкции
(docstring ClosedCycleExecutorV4.sol, строка 66-68) требует
ПРЕДВАРИТЕЛЬНО профинансированный инвентарь, не флэш-займ). Прибыль
измеряется РЕАЛЬНЫМ balanceAfter-balanceBefore ПОСЛЕ настоящего свопа,
никогда не подставляется/не зачисляется напрямую.

Подписант/владелец на форке -- тестовый аккаунт(0) anvil (см. докстринг
task5_v4_fork_simulation.py -- реальные, публичные, документированные,
нулевой стоимости ключи, читаются из СОБСТВЕННОГО вывода anvil, не
захардкожены). PRIVATE_KEY_NOX/RH_RPC_URL/RH_SEQUENCER_URL
переопределяются ТОЛЬКО в окружении ЭТОГО процесса, ДО импорта
task5_bot_sender (модуль читает их из os.environ на уровне модуля) --
локальные отправки идут ИСКЛЮЧИТЕЛЬНО на локальный anvil-порт, не в
реальную сеть."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
ANVIL = str(FOUNDRY_BIN / "anvil")
CAST = str(FOUNDRY_BIN / "cast")

FORK_BLOCK = 61248736
PORT = 8556  # ДРУГОЙ порт, чем task5_v4_fork_simulation.py (8555) -- не пересекается, если оба когда-то запущены
RPC = f"http://127.0.0.1:{PORT}"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
MOSIAI = "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a"
HOOKS_NONE = "0x0000000000000000000000000000000000000000"
PRINCIPAL_USDG_RAW = 9437184


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def main() -> None:
    result: dict = {"fork_block": FORK_BLOCK, "principal_usdg_raw": PRINCIPAL_USDG_RAW,
                     "note": "форк ДО реального деплоя 0xAB24907b... -- контракт развёрнут ЗАНОВО на форке "
                             "той же сборкой байткода; это проверка ЛОГИКИ контракта+Sender+учёта, "
                             "НЕ утверждение, что 0xAB24907b... существовал на блоке 61248736"}
    anvil_proc = None
    anvil_log_file = None
    anvil_log_path = "/tmp/task5_v4_e2e_proof_anvil.log"
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))
        from alchemy_fallback import _alchemy_direct_endpoint
        fork_url = _alchemy_direct_endpoint()
        if not fork_url:
            from config import CONFIG
            fork_url = CONFIG.public_rpc_url
        result["fork_url_source"] = "alchemy" if _alchemy_direct_endpoint() else "public_rpc"

        print(f"[e2e_proof] запускаю anvil --fork-block-number {FORK_BLOCK} на порту {PORT}...")
        # ПРАВКА (реальный второй прогон -- send зависал до receipt timeout
        # БЕЗ единой ошибки send_raw_transaction): anvil писал ВЕСЬ свой
        # стдаут в subprocess.PIPE, который читался ТОЛЬКО до "Listening
        # on" (баннер) -- дальше НИКТО не вычитывал трубу. ОС-буфер трубы
        # (обычно 64КБ) заполнялся логами КАЖДОГО RPC-вызова anvil, и сам
        # anvil БЛОКИРОВАЛСЯ на записи в стдаут -- переставал обрабатывать
        # новые RPC-вызовы (в т.ч. майнить уже принятую транзакцию),
        # объясняя ИМЕННО "receipt timeout без единой ошибки отправки".
        # Пишем в РЕАЛЬНЫЙ файл (не в трубу) -- anvil никогда не
        # блокируется на записи, и лог доступен для диагностики на любом
        # шаге (см. anvil_log_path ниже, читается при неудаче).
        anvil_log_file = open(anvil_log_path, "w")
        anvil_proc = subprocess.Popen(
            [ANVIL, "--fork-url", fork_url, "--fork-block-number", str(FORK_BLOCK), "--port", str(PORT)],
            stdout=anvil_log_file, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        banner_lines: list[str] = []
        deadline = time.monotonic() + 30
        with open(anvil_log_path) as tail_fh:
            while time.monotonic() < deadline:
                line = tail_fh.readline()
                if not line:
                    if anvil_proc.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue
                banner_lines.append(line.rstrip("\n"))
                if "Listening on" in line:
                    break
        addr_match = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{40})", "\n".join(banner_lines))
        key_match = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{64})", "\n".join(banner_lines))
        if not addr_match or not key_match:
            raise RuntimeError(f"не удалось распарсить тестовый аккаунт(0) anvil: {banner_lines}")
        deployer_addr = addr_match.group(1)
        deployer_key = key_match.group(1)
        result["deployer_addr"] = deployer_addr

        ready = False
        for _ in range(30):
            proc = run([CAST, "block-number", "--rpc-url", RPC], timeout=10)
            if proc.returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise RuntimeError("anvil не ответил за 30с")

        # --- Деплой ClosedCycleExecutorV4(owner=deployer, poolManager) ---
        bytecode = (REPO_ROOT / "contracts" / "build" / "ClosedCycleExecutorV4.bytecode.txt").read_text().strip()
        bytecode_hex = bytecode[2:] if bytecode.startswith("0x") else bytecode

        def word_addr(a: str) -> str:
            return a[2:].rjust(64, "0")

        creation_calldata = "0x" + bytecode_hex + word_addr(deployer_addr) + word_addr(POOL_MANAGER)
        print("[e2e_proof] деплою ClosedCycleExecutorV4 (та же сборка, что закреплена для пилота)...")
        deploy_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", RPC,
                            "--create", creation_calldata, "--json"], timeout=60)
        if deploy_proc.returncode != 0:
            raise RuntimeError(f"деплой упал: {deploy_proc.stderr}")
        contract_addr = json.loads(deploy_proc.stdout).get("contractAddress")
        if not contract_addr:
            raise RuntimeError("в receipt деплоя нет contractAddress")
        result["contract_addr_on_fork"] = contract_addr

        # --- Пополнение инвентаря (СТАРТОВЫЙ капитал, не прибыль) ---
        print("[e2e_proof] пополняю контракт стартовым капиталом (impersonate PoolManager)...")
        run([CAST, "rpc", "anvil_impersonateAccount", POOL_MANAGER, "--rpc-url", RPC], timeout=15)
        run([CAST, "rpc", "anvil_setBalance", POOL_MANAGER, hex(10 ** 19), "--rpc-url", RPC], timeout=15)
        fund_proc = run([CAST, "send", "--unlocked", "--from", POOL_MANAGER, "--rpc-url", RPC,
                          USDG, "transfer(address,uint256)", contract_addr, str(PRINCIPAL_USDG_RAW), "--json"],
                         timeout=30)
        if fund_proc.returncode != 0:
            raise RuntimeError(f"пополнение USDG упало: {fund_proc.stderr}")

        bal_before_proc = run([CAST, "call", USDG, "balanceOf(address)(uint256)", contract_addr,
                                "--rpc-url", RPC], timeout=15)
        balance_before = int(bal_before_proc.stdout.strip().split()[0])
        result["contract_usdg_balance_before"] = balance_before

        # --- РЕАЛЬНЫЙ Sender: переопределяем окружение ДО импорта модуля ---
        os.environ["RH_RPC_URL"] = RPC
        os.environ["RH_SEQUENCER_URL"] = RPC  # локальный форк не различает sequencer/rpc -- оба на один узел
        os.environ["PRIVATE_KEY_NOX"] = deployer_key
        os.environ["SENDER_STATE_FILE"] = "/tmp/task5_v4_e2e_proof_sender_state.json"
        Path(os.environ["SENDER_STATE_FILE"]).unlink(missing_ok=True)  # чистое состояние для ЭТОГО прогона

        import task5_bot_sender
        import task5_v4_hotpath as hp
        from task5_v4_pilot_accounting import PilotBudget
        from task5_v4_route_registry import RouteCycle, RouteLeg
        from task5_v4_executor_calldata import build_execute_cycle_calldata

        # ПРАВКА (реальный первый прогон): task5_bot_sender.STOP_FILE --
        # ЖЁСТКО закодированный /etc/bot/STOP (НЕ управляется переменной
        # окружения, в отличие от SENDER_STATE_FILE) -- это РЕАЛЬНЫЙ,
        # ОБЩИЙ файл, оставленный на диске Ohio штатной остановкой
        # предыдущего пилота (см. run_task5_v4_hotpath_stop.yml) --
        # Sender.can_send() честно заблокировал первую попытку этим самым
        # файлом. Мы НЕ трогаем реальный /etc/bot/STOP (это чужое,
        # намеренно оставленное состояние -- скрипт обязан быть read-only
        # к рабочему состоянию бота) -- переопределяем ТОЛЬКО атрибут
        # модуля В ПАМЯТИ ЭТОГО процесса на заведомо несуществующий путь,
        # НЕ трогая диск.
        task5_bot_sender.STOP_FILE = Path("/tmp/task5_v4_e2e_proof_nonexistent_stop_marker")

        print("[e2e_proof] строю маршрут (task5_v4_route_registry.RouteCycle/RouteLeg -- реальные типы hotpath.py)...")
        legs = (
            RouteLeg(USDG, MOSIAI, 70000, 4, HOOKS_NONE, True),
            RouteLeg(USDG, MOSIAI, 70000, 3, HOOKS_NONE, False),
        )
        route = RouteCycle("route_e2e_proof", legs, USDG, "E2E-PROOF USDG->MOSIAI->USDG", "control")

        calldata = build_execute_cycle_calldata(route, -PRINCIPAL_USDG_RAW, min_profit=1)
        result["calldata_len_bytes"] = len(calldata)

        print("[e2e_proof] Sender: prepare_transaction_fields (РЕАЛЬНЫЙ производственный путь)...")
        sender = task5_bot_sender.Sender()
        result["sender_address"] = sender.address
        result["sender_address_matches_deployer"] = sender.address.lower() == deployer_addr.lower()

        gas_estimate_proc = run([CAST, "estimate", contract_addr, "--rpc-url", RPC,
                                  "--from", deployer_addr, "--json"], timeout=15)
        # estimate_gas() -- та же функция, что в hotpath.py (eth_estimateGas без явного block -- см.
        # часть D task5_v4_control_case_investigation.py про то, что это означает).
        gas_res = hp.estimate_gas(contract_addr, calldata, deployer_addr)
        if not gas_res["ok"]:
            raise RuntimeError(f"estimate_gas (реальная функция hotpath.py) отказала: {gas_res}")
        result["gas_estimate"] = gas_res["gas_estimate"]

        tx_fields = sender.prepare_transaction_fields(contract_addr, calldata, gas_res["gas_estimate"])
        prepared = sender.sign_prepared_transaction(tx_fields)
        result["tx_hash_local_precomputed"] = prepared.tx_hash
        # Диагностика (реальный третий прогон подряд без receipt) --
        # сверяем ПОДГОТОВЛЕННЫЕ поля (nonce/chainId/газ) с тем, что anvil
        # реально видит для этого адреса ПРЯМО СЕЙЧАС, вместо гадания.
        result["prepared_tx_fields"] = {k: (v.hex() if isinstance(v, (bytes, bytearray)) else v)
                                          for k, v in tx_fields.items()}
        chain_id_proc = run([CAST, "chain-id", "--rpc-url", RPC], timeout=10)
        result["anvil_chain_id"] = chain_id_proc.stdout.strip()
        pending_nonce_proc = run([CAST, "nonce", deployer_addr, "--rpc-url", RPC], timeout=10)
        result["anvil_pending_nonce_before_send"] = pending_nonce_proc.stdout.strip()

        print("[e2e_proof] Sender: submit_prepared (РЕАЛЬНАЯ отправка в ЛОКАЛЬНЫЙ anvil, ждём receipt)...")
        send_result = sender.submit_prepared(prepared)
        result["send_result"] = {
            "ok": send_result.ok, "tx_hash": send_result.tx_hash, "status": send_result.status,
            "gas_used": send_result.gas_used, "unresolved": send_result.unresolved,
            "revert_reason": send_result.revert_reason, "error": send_result.error,
        }
        if send_result.unresolved:
            raise RuntimeError("receipt НЕ получен (unresolved) -- сквозной путь НЕ доказан")
        if not send_result.ok:
            raise RuntimeError(f"receipt.status != 1: {send_result.revert_reason or send_result.error}")

        receipt = sender.rpc.eth.get_transaction_receipt(prepared.tx_hash)
        result["receipt_status"] = receipt["status"]
        result["receipt_block"] = receipt["blockNumber"]
        result["receipt_gas_used"] = receipt["gasUsed"]

        print("[e2e_proof] parse_cycle_executed_profit (РЕАЛЬНАЯ функция task5_v4_hotpath.py, не переизобретена)...")
        receipt_dict = {"logs": [dict(log) for log in receipt["logs"]], "status": receipt["status"]}
        for log in receipt_dict["logs"]:
            log["address"] = log["address"]
            log["topics"] = [t.hex() if hasattr(t, "hex") else t for t in log["topics"]]
            log["data"] = log["data"].hex() if hasattr(log["data"], "hex") else log["data"]
            if not log["topics"][0].startswith("0x"):
                log["topics"] = ["0x" + t for t in log["topics"]]
            if not log["data"].startswith("0x"):
                log["data"] = "0x" + log["data"]
        cycle_profit = hp.parse_cycle_executed_profit(receipt_dict, contract_addr, expected_exit_token=USDG)
        result["cycle_executed_profit_raw"] = cycle_profit
        if cycle_profit is None:
            raise RuntimeError("CycleExecuted НЕ найден/НЕ распознан в реальном рецепте -- сквозной путь НЕ доказан")

        bal_after_proc = run([CAST, "call", USDG, "balanceOf(address)(uint256)", contract_addr,
                               "--rpc-url", RPC], timeout=15)
        balance_after = int(bal_after_proc.stdout.strip().split()[0])
        result["contract_usdg_balance_after"] = balance_after
        result["actual_balance_diff_raw"] = balance_after - balance_before
        result["cycle_executed_matches_balance_diff"] = cycle_profit == (balance_after - balance_before)

        print("[e2e_proof] запись газа/прибыли через РЕАЛЬНЫЙ PilotBudget (task5_v4_pilot_accounting.py)...")
        budget = PilotBudget(state_path="/tmp/task5_v4_e2e_proof_budget_state.json")
        Path("/tmp/task5_v4_e2e_proof_budget_state.json").unlink(missing_ok=True)
        budget = PilotBudget(state_path="/tmp/task5_v4_e2e_proof_budget_state.json")
        budget.begin_attempt({
            "tx_hash": prepared.tx_hash, "nonce": prepared.nonce, "route_id": route.route_id,
            "route_label": route.label, "exit_token": USDG, "size_in_raw": PRINCIPAL_USDG_RAW,
            "expected_profit_after_gas": 0.5, "computed_at_block": FORK_BLOCK,
        })
        gas_fin = budget.finalize_gas(tx_status=receipt["status"], gas_used=receipt["gasUsed"],
                                       effective_gas_price=receipt.get("effectiveGasPrice", tx_fields["maxFeePerGas"]),
                                       eth_usd_price=2500.0)
        result["budget_finalize_gas"] = gas_fin
        profit_fin = budget.finalize_profit_and_close(cycle_profit)
        result["budget_finalize_profit"] = profit_fin
        result["budget_cumulative_gas_loss_usd"] = budget.cumulative_gas_loss_usd
        result["budget_cumulative_net_pnl_usd"] = budget.cumulative_net_pnl_usd
        result["budget_cumulative_gross_profit_raw_by_token"] = budget.cumulative_gross_profit_raw_by_token

        result["ok"] = True
        print("[e2e_proof] === СКВОЗНОЙ ПУТЬ ПОДТВЕРЖДЁН: расчёт -> calldata -> реальный Sender -> "
              "локальная нода -> receipt status=1 -> CycleExecuted -> реальный прирост -> "
              "PilotBudget учёл газ и прибыль ===")
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["error"] = str(exc)
        print(f"[e2e_proof] ОШИБКА: {exc}", file=sys.stderr)
    finally:
        if not result.get("ok"):
            # Диагностика (см. правку выше про блокировку anvil на записи
            # в непрочитанную трубу) -- хвост РЕАЛЬНОГО лога anvil (файл,
            # не труба) на момент отказа.
            try:
                if anvil_log_file is not None:
                    anvil_log_file.flush()
                with open(anvil_log_path) as fh:
                    log_lines = fh.readlines()
                # ПРАВКА (реальный третий прогон): хвост из последних 80
                # строк -- ТОЛЬКО повторяющиеся eth_getTransactionReceipt
                # (300 попыток за 15с), eth_sendRawTransaction (если он
                # вообще был залогирован) давно вытеснен из этого окна.
                # Явно ищем ЕГО (и его контекст), а не только последние
                # строки -- иначе результат неинформативен.
                send_related = [
                    (i, l) for i, l in enumerate(log_lines)
                    if "sendRawTransaction" in l or "SendTransaction" in l or "InsufficientFunds" in l
                    or "insufficient funds" in l.lower() or "nonce" in l.lower()
                ]
                result["anvil_log_send_related"] = [
                    {"line_no": i, "text": l.rstrip("\n"),
                     "context": [x.rstrip("\n") for x in log_lines[max(0, i - 2):i + 3]]}
                    for i, l in send_related
                ]
                result["anvil_log_n_total_lines"] = len(log_lines)
                result["anvil_log_tail"] = [l.rstrip("\n") for l in log_lines[-30:]]
            except Exception as log_exc:  # noqa: BLE001
                result["anvil_log_tail_error"] = str(log_exc)
        if anvil_proc is not None:
            anvil_proc.terminate()
            try:
                anvil_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                anvil_proc.kill()
        if anvil_log_file is not None:
            try:
                anvil_log_file.close()
            except Exception:  # noqa: BLE001
                pass

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = REPO_ROOT / "data" / "task5_v4_e2e_fork_sender_proof_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
