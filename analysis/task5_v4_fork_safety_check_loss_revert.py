#!/usr/bin/env python3
"""Обязательная точечная проверка защиты денег ПЕРЕД любым живым пилотом
(владелец, 2026-09-12): "На форке: заведомо убыточный цикл при непустом
инвентаре контракта. Убедиться, что revert срабатывает и убыток не
покрывается нашим инвентарём. Если не откатывается -- стоп, чиним."

НЕ новый этап -- одна точечная проверка поверх уже готовой инфраструктуры
Этапа 2 (task5_v4_fork_simulation.py). Реальный локальный форк, реальный
задеплоенный байткод (contracts/build/ClosedCycleExecutorV4.*), НИКАКИХ
реальных транзакций.

Заведомо убыточный цикл -- не выдуман, а взят из УЖЕ РЕАЛЬНО измеренного
в этой сессии случая: task5_v4_quote_replay.py, шаг "самостоятельный
выбор направления", реверс порядка пулов (сперва pool B, потом pool A)
на блоке 61248736 дал РЕАЛЬНЫЙ убыток -3.204861 USDG на том же принципале
9.437184 USDG (data/task5_v4_quote_replay_result.json,
"n_minus_1_reversed_direction"). Тот же реверс здесь -- как аргументы
executeCycle.

Контракт ПРЕДВАРИТЕЛЬНО пополняется 20 USDG (больше принципала 9.437184)
-- чтобы отличить "принципал не потрачен" от "инвентарь тоже не тронут".
minProfit=0 -- проверяем защиту от ЛЮБОГО убытка, не только "меньше
целевого"."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _alchemy_direct_endpoint  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
ANVIL = str(FOUNDRY_BIN / "anvil")
CAST = str(FOUNDRY_BIN / "cast")

NATIVE = "0x0000000000000000000000000000000000000000"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
MOSIAI = "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

MIN_SQRT_PRICE = 4295128739
MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342

# Тот же исторический блок Этапа 2 -- РЕАЛЬНАЯ, уже проверенная в этой
# сессии ликвидность (текущее состояние сети для этих пулов, наоборот,
# сейчас NotEnoughLiquidity -- см. data/task5_v4_diag_current_liquidity_result.json,
# поэтому для чистой проверки логики revert-а используем блок, где сама
# механика свопа заведомо работает, а не гадаем на пересохших пулах).
FORK_BLOCK = 61248736
PRINCIPAL_USDG_RAW = 9437184
PREFUND_USDG_RAW = 20_000_000  # 20 USDG -- заведомо больше принципала
PORT = 8561


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def main() -> None:
    result: dict = {"fork_block": FORK_BLOCK, "principal_usdg_raw": PRINCIPAL_USDG_RAW,
                     "prefund_usdg_raw": PREFUND_USDG_RAW}
    rpc = f"http://127.0.0.1:{PORT}"
    anvil_proc = None
    try:
        fork_url = _alchemy_direct_endpoint()
        if not fork_url:
            raise RuntimeError("нет Alchemy-эндпоинта для форка")

        print(f"[safety_check] anvil --fork-block-number {FORK_BLOCK}...")
        anvil_proc = subprocess.Popen(
            [ANVIL, "--fork-url", fork_url, "--fork-block-number", str(FORK_BLOCK), "--port", str(PORT)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        banner = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = anvil_proc.stdout.readline()
            if not line:
                if anvil_proc.poll() is not None:
                    break
                continue
            banner.append(line.rstrip("\n"))
            if "Listening on" in line:
                break
        text = "\n".join(banner)
        addr_m = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{40})", text)
        key_m = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{64})", text)
        if not addr_m or not key_m:
            raise RuntimeError(f"не распарсил тестовый аккаунт anvil: {banner}")
        deployer_addr, deployer_key = addr_m.group(1), key_m.group(1)

        for _ in range(30):
            proc = run([CAST, "block-number", "--rpc-url", rpc], timeout=10)
            if proc.returncode == 0:
                break
            time.sleep(1)

        bytecode = (REPO_ROOT / "contracts" / "build" / "ClosedCycleExecutorV4.bytecode.txt").read_text().strip()
        bytecode_hex = bytecode[2:] if bytecode.startswith("0x") else bytecode

        def word_addr(a: str) -> str:
            return a[2:].rjust(64, "0")

        creation_calldata = "0x" + bytecode_hex + word_addr(deployer_addr) + word_addr(POOL_MANAGER)
        print("[safety_check] деплою ClosedCycleExecutorV4...")
        deploy_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", rpc,
                            "--create", creation_calldata, "--json"], timeout=60)
        if deploy_proc.returncode != 0:
            raise RuntimeError(f"деплой упал: {deploy_proc.stderr}")
        contract_addr = json.loads(deploy_proc.stdout).get("contractAddress")
        result["contract_addr"] = contract_addr
        if not contract_addr:
            raise RuntimeError("нет contractAddress в receipt деплоя")

        print(f"[safety_check] пополняю контракт {PREFUND_USDG_RAW} raw USDG (импersonate PoolManager)...")
        run([CAST, "rpc", "anvil_impersonateAccount", POOL_MANAGER, "--rpc-url", rpc], timeout=15)
        run([CAST, "rpc", "anvil_setBalance", POOL_MANAGER, hex(10**19), "--rpc-url", rpc], timeout=15)
        fund_proc = run([CAST, "send", "--unlocked", "--from", POOL_MANAGER, "--rpc-url", rpc,
                          USDG, "transfer(address,uint256)", contract_addr, str(PREFUND_USDG_RAW), "--json"], timeout=30)
        if fund_proc.returncode != 0:
            raise RuntimeError(f"пополнение упало: {fund_proc.stderr}")

        bal_before_proc = run([CAST, "call", USDG, "balanceOf(address)(uint256)", contract_addr, "--rpc-url", rpc], timeout=15)
        balance_before = int(bal_before_proc.stdout.strip().split()[0])
        result["contract_usdg_balance_before_attempt"] = balance_before
        print(f"[safety_check] баланс контракта ДО попытки: {balance_before} raw USDG")

        # --- ЗАВЕДОМО УБЫТОЧНЫЙ цикл: реверс порядка пулов (реально
        # измеренный убыток -3.204861 USDG на блоке 61248736, см.
        # docstring выше) -- minProfit=0, т.е. защита от ЛЮБОГО убытка.
        leg_reversed_1 = f"({USDG},{MOSIAI},70000,3,{NATIVE},true,{MIN_SQRT_PRICE + 1},0x)"   # pool B первым
        leg_reversed_2 = f"({USDG},{MOSIAI},70000,4,{NATIVE},false,{MAX_SQRT_PRICE - 1},0x)"  # pool A вторым
        cycle_params = f"([{leg_reversed_1},{leg_reversed_2}],-{PRINCIPAL_USDG_RAW},{USDG},0)"
        sig = "executeCycle(((address,address,uint24,int24,address,bool,uint160,bytes)[],int256,address,uint256))"

        print("[safety_check] вызываю executeCycle с ЗАВЕДОМО УБЫТОЧНЫМ (реверснутым) порядком плеч...")
        exec_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", rpc,
                          contract_addr, sig, cycle_params, "--json"], timeout=60)
        result["execute_cycle_returncode"] = exec_proc.returncode
        result["execute_cycle_stdout"] = exec_proc.stdout.strip()
        result["execute_cycle_stderr"] = exec_proc.stderr.strip()

        reverted = exec_proc.returncode != 0
        result["tx_reverted"] = reverted

        bal_after_proc = run([CAST, "call", USDG, "balanceOf(address)(uint256)", contract_addr, "--rpc-url", rpc], timeout=15)
        balance_after = int(bal_after_proc.stdout.strip().split()[0])
        result["contract_usdg_balance_after_attempt"] = balance_after
        result["balance_unchanged"] = balance_after == balance_before

        # Реальный текст ревёрта (селектор InsufficientProfit -- сверено
        # против task5_bot_sender.py::REVERT_SELECTORS, где 0xc39ba758 ==
        # InsufficientProfit(uint256,uint256,uint256) для V3-версии;
        # у V4-контракта тот же error name/сигнатура -- см. contracts/
        # ClosedCycleExecutorV4.sol -- селектор пересчитывается ниже
        # НЕЗАВИСИМО, не переиспользуется вслепую из V3-таблицы).
        import eth_utils
        insufficient_profit_selector = "0x" + eth_utils.function_signature_to_4byte_selector(
            "InsufficientProfit(uint256,uint256,uint256)").hex()
        result["insufficient_profit_selector_recomputed"] = insufficient_profit_selector
        result["stderr_contains_insufficient_profit_selector"] = insufficient_profit_selector[2:] in exec_proc.stderr

        print(f"[safety_check] tx_reverted={reverted}")
        print(f"[safety_check] баланс ДО={balance_before}, ПОСЛЕ={balance_after}, "
              f"balance_unchanged={result['balance_unchanged']}")
        print(f"[safety_check] stderr: {exec_proc.stderr.strip()[:2000]}")

        result["PASS"] = reverted and result["balance_unchanged"]
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["error"] = str(exc)
        result["PASS"] = False
        print(f"[safety_check] ОШИБКА: {exc}", file=sys.stderr)
    finally:
        if anvil_proc is not None:
            anvil_proc.terminate()
            try:
                anvil_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                anvil_proc.kill()

    print(f"\n[safety_check] ИТОГ: {'PASS' if result.get('PASS') else 'FAIL'}")
    out_path = REPO_ROOT / "data" / "task5_v4_fork_safety_check_loss_revert_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"[safety_check] сохранено: {out_path}")


if __name__ == "__main__":
    main()
