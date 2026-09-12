#!/usr/bin/env python3
"""Задача 5, стадия 2, Этап 2 (продолжение): локальная fork-симуляция
ClosedCycleExecutorV4 на маршруте ETH -> MOSIAI (хук) -> USDG -> ETH,
обе контрольные tx (0x658c2ab8..., 0x9976a38f...).

Особая проверка (владелец, 2026-09-12): нативный ETH через
settle{value}, хук с ПУСТЫМ hookData от НАШЕГО адреса (не от адреса
исходного исполнителя 0x1b357e7a...), итоговая прибыль -- реальная
измеренная, не оценка.

Реальные PoolKey всех трёх пулов -- из настоящих Initialize-событий
PoolManager (data/task5_v4_hook_route_audit_result.json, эта же
сессия), не предположены:
  ETH/MOSIAI:  currency0=NATIVE, currency1=MOSIAI, fee=0, tickSpacing=200, hooks=0xe5e702641ea86f4ae6cc3cdaed2b886f976be044
  USDG/MOSIAI: currency0=USDG,   currency1=MOSIAI, fee=75000, tickSpacing=2, hooks=0x0
  ETH/USDG:    currency0=NATIVE, currency1=USDG,   fee=100, tickSpacing=1, hooks=0x0

Пополнение контракта -- НАТИВНЫМ ETH напрямую через anvil_setBalance
(стандартный, безопасный приём локального форка -- не изображает
реального держателя, а прямо задаёт баланс тестового контракта на
локальной копии; для нативной валюты это эквивалентно
impersonate+transfer, но проще и надёжнее)."""
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
MOSIAI = "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
HOOK_ETH_MOSIAI = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"

MIN_SQRT_PRICE = 4295128739
MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342

CASES = [
    {"label": "0x658c2ab8...", "tx_hash": "0x658c2ab808cd0fa30f5dc14a5ea0e6259fe11714b842efcb5721c3acd320274f",
     "tx_block": 61248715, "principal_eth_wei": 324259173170675712, "port": 8556},
    {"label": "0x9976a38f...", "tx_hash": "0x9976a38fec4f4d2be228118da68b1276a26e7f33927ea606d11348d31e2ed98c",
     "tx_block": 61248775, "principal_eth_wei": 81064793292668928, "port": 8557},
]


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def simulate_one(case: dict) -> dict:
    fork_block = case["tx_block"] - 1  # N-1: состояние ПЕРЕД контрольной tx
    port = case["port"]
    rpc = f"http://127.0.0.1:{port}"
    result: dict = {"label": case["label"], "control_tx_hash": case["tx_hash"],
                     "fork_block": fork_block, "principal_eth_wei": case["principal_eth_wei"],
                     "principal_eth": case["principal_eth_wei"] / 1e18}
    anvil_proc = None
    try:
        fork_url = _alchemy_direct_endpoint()
        if not fork_url:
            raise RuntimeError("нет Alchemy-эндпоинта")

        print(f"[fork_sim_hook][{case['label']}] anvil --fork-block-number {fork_block} на порту {port}...")
        anvil_proc = subprocess.Popen(
            [ANVIL, "--fork-url", fork_url, "--fork-block-number", str(fork_block), "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )

        banner_lines: list[str] = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = anvil_proc.stdout.readline()
            if not line:
                if anvil_proc.poll() is not None:
                    break
                continue
            banner_lines.append(line.rstrip("\n"))
            if "Listening on" in line:
                break
        result["anvil_banner_tail"] = banner_lines[-5:]

        text = "\n".join(banner_lines)
        addr_match = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{40})", text)
        key_match = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{64})", text)
        if not addr_match or not key_match:
            raise RuntimeError(f"не распарсил тестовый аккаунт(0) из баннера anvil: {banner_lines}")
        deployer_addr, deployer_key = addr_match.group(1), key_match.group(1)
        result["deployer_addr"] = deployer_addr

        ready = False
        for _ in range(30):
            proc = run([CAST, "block-number", "--rpc-url", rpc], timeout=10)
            if proc.returncode == 0:
                result["anvil_block_after_fork"] = proc.stdout.strip()
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise RuntimeError("anvil не ответил за 30с")

        # --- Деплой ---
        bytecode = (REPO_ROOT / "contracts" / "build" / "ClosedCycleExecutorV4.bytecode.txt").read_text().strip()
        bytecode_hex = bytecode[2:] if bytecode.startswith("0x") else bytecode

        def word_addr(a: str) -> str:
            return a[2:].rjust(64, "0")

        POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
        creation_calldata = "0x" + bytecode_hex + word_addr(deployer_addr) + word_addr(POOL_MANAGER)

        print(f"[fork_sim_hook][{case['label']}] деплою...")
        deploy_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", rpc,
                            "--create", creation_calldata, "--json"], timeout=60)
        result["deploy_returncode"] = deploy_proc.returncode
        result["deploy_stderr"] = deploy_proc.stderr.strip()
        if deploy_proc.returncode != 0:
            raise RuntimeError(f"деплой упал: {deploy_proc.stderr}")
        contract_addr = json.loads(deploy_proc.stdout).get("contractAddress")
        result["contract_addr"] = contract_addr
        if not contract_addr:
            raise RuntimeError("нет contractAddress в receipt деплоя")

        # --- Пополнение НАТИВНЫМ ETH напрямую (anvil_setBalance) ---
        fund_amount_wei = case["principal_eth_wei"] + 10**17  # запас на возможную неточность + не нужен для settle, но не помешает
        set_bal = run([CAST, "rpc", "anvil_setBalance", contract_addr, hex(fund_amount_wei), "--rpc-url", rpc], timeout=15)
        result["set_balance_returncode"] = set_bal.returncode
        bal_before_proc = run([CAST, "balance", contract_addr, "--rpc-url", rpc], timeout=15)
        balance_before = int(bal_before_proc.stdout.strip())
        result["contract_eth_balance_before_cycle"] = balance_before
        result["contract_eth_balance_before_cycle_eth"] = balance_before / 1e18

        # --- executeCycle: ETH -> MOSIAI (хук, hookData ПУСТОЕ) -> USDG -> ETH ---
        leg_a = f"({NATIVE},{MOSIAI},0,200,{HOOK_ETH_MOSIAI},true,{MIN_SQRT_PRICE + 1},0x)"
        leg_b = f"({USDG},{MOSIAI},75000,2,{NATIVE},false,{MAX_SQRT_PRICE - 1},0x)"
        leg_c = f"({NATIVE},{USDG},100,1,{NATIVE},false,{MAX_SQRT_PRICE - 1},0x)"
        cycle_params = f"([{leg_a},{leg_b},{leg_c}],-{case['principal_eth_wei']},{NATIVE},0)"
        sig = "executeCycle(((address,address,uint24,int24,address,bool,uint160,bytes)[],int256,address,uint256))"

        print(f"[fork_sim_hook][{case['label']}] вызываю executeCycle (3 плеча, хук на плече A, hookData=0x)...")
        exec_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", rpc,
                          contract_addr, sig, cycle_params, "--json"], timeout=60)
        result["execute_cycle_returncode"] = exec_proc.returncode
        result["execute_cycle_stderr"] = exec_proc.stderr.strip()
        result["execute_cycle_stdout"] = exec_proc.stdout.strip()

        bal_after_proc = run([CAST, "balance", contract_addr, "--rpc-url", rpc], timeout=15)
        balance_after = int(bal_after_proc.stdout.strip())
        result["contract_eth_balance_after_cycle"] = balance_after
        result["contract_eth_balance_after_cycle_eth"] = balance_after / 1e18

        result["execute_cycle_succeeded"] = exec_proc.returncode == 0
        if exec_proc.returncode == 0:
            receipt = json.loads(exec_proc.stdout)
            gas_used = int(receipt.get("gasUsed", "0x0"), 16)
            effective_gas_price = int(receipt.get("effectiveGasPrice", "0x0"), 16)
            gas_cost_wei = gas_used * effective_gas_price
            result["execute_cycle_tx_hash"] = receipt.get("transactionHash")
            result["execute_cycle_status"] = receipt.get("status")
            result["execute_cycle_gas_used"] = gas_used
            result["gas_cost_eth"] = gas_cost_wei / 1e18
            # Баланс "после" уже включает списание газа (это ETH-баланс
            # контракта, а не EOA-плательщика газа) -- т.е. profit здесь
            # УЖЕ net от прибыли цикла, но контракт газ не платит (платит
            # deployer/msg.sender) -- разница balance_after-before это
            # ЧИСТАЯ прибыль цикла (без вычета газа, который списался с
            # deployer'а, а не с контракта).
            result["profit_eth_wei"] = balance_after - balance_before
            result["profit_eth"] = (balance_after - balance_before) / 1e18
        else:
            result["profit_eth_wei"] = balance_after - balance_before
            result["profit_eth"] = (balance_after - balance_before) / 1e18

        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["error"] = str(exc)
        print(f"[fork_sim_hook][{case['label']}] ОШИБКА: {exc}", file=sys.stderr)
    finally:
        if anvil_proc is not None:
            anvil_proc.terminate()
            try:
                anvil_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                anvil_proc.kill()
    return result


def main() -> None:
    all_results = []
    for case in CASES:
        r = simulate_one(case)
        print(json.dumps(r, indent=2, default=str))
        all_results.append(r)

    out_path = REPO_ROOT / "data" / "task5_v4_fork_simulation_hook_route_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"cases": all_results}, indent=2, default=str))
    print(f"[fork_sim_hook] сохранено: {out_path}")


if __name__ == "__main__":
    main()
