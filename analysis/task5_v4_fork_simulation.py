#!/usr/bin/env python3
"""Задача 5, стадия 2, Этап 2: локальная fork-симуляция
ClosedCycleExecutorV4 на контрольном маршруте USDG -> MOSIAI -> USDG,
блок 61248736 (N-1 относительно контрольной tx 0xd487122244...).

Реальный локальный форк (anvil --fork-url ... --fork-block-number
61248736), не py-модель. Деплой и вызов executeCycle идут ТОЛЬКО через
встроенные тестовые аккаунты anvil (публичные, документированные,
нулевая реальная стоимость -- их приватные ключи читаются из
собственного вывода anvil при старте, не захардкожены заранее).
НИКАКИХ реальных ключей/транзакций/цепочки не касается -- это
изолированный локальный процесс, который убивается в конце.

Пополнение контракта 9.437184 USDG -- через anvil_impersonateAccount
на PoolManager (0x8366a39cc670b4001a1121b8f6a443a643e40951), который
реально держит USDG всех пулов (подтверждено во всех Transfer-логах
этой сессии) -- обычный ERC20.transfer() с подставного sender'а,
работает независимо от внутренней бухгалтерии PoolManager (это просто
ERC20-леджер), только на локальном форке.

sqrtPriceLimitX96 -- реальные MIN/MAX_SQRT_PRICE из
Uniswap/v4-core/libraries/TickMath.sol (сверено WebFetch в этой же
сессии), не 0 (ноль вне допустимого диапазона -- своп сам по себе
ревертнул бы)."""
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

FORK_BLOCK = 61248736
PORT = 8555
RPC = f"http://127.0.0.1:{PORT}"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
MOSIAI = "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a"
HOOKS_NONE = "0x0000000000000000000000000000000000000000"

# Uniswap/v4-core TickMath.sol -- сверено WebFetch в этой сессии.
MIN_SQRT_PRICE = 4295128739
MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342

PRINCIPAL_USDG_RAW = 9437184  # 9.437184 USDG, тот же размер, что контрольная tx


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def main() -> None:
    result: dict = {"fork_block": FORK_BLOCK, "principal_usdg_raw": PRINCIPAL_USDG_RAW}
    anvil_proc = None
    try:
        fork_url = _alchemy_direct_endpoint()
        result["fork_url_is_alchemy_direct"] = bool(fork_url)
        if not fork_url:
            raise RuntimeError("нет Alchemy-эндпоинта -- публичный RPC не хранит state такой давности (см. task5_v4_quote_replay)")

        print(f"[fork_sim] запускаю anvil --fork-block-number {FORK_BLOCK} на порту {PORT}...")
        anvil_proc = subprocess.Popen(
            [ANVIL, "--fork-url", fork_url, "--fork-block-number", str(FORK_BLOCK), "--port", str(PORT)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )

        # Читаем баннер анвила -- реальные тестовые ключи/адреса, НЕ
        # захардкожены заранее (см. докстринг).
        banner_lines: list[str] = []
        deployer_addr = None
        deployer_key = None
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
        result["anvil_banner"] = banner_lines

        addr_match = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{40})", "\n".join(banner_lines))
        key_match = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{64})", "\n".join(banner_lines))
        if addr_match:
            deployer_addr = addr_match.group(1)
        if key_match:
            deployer_key = key_match.group(1)
        result["deployer_addr"] = deployer_addr
        result["deployer_key_found"] = deployer_key is not None
        if not deployer_addr or not deployer_key:
            raise RuntimeError(f"не удалось распарсить тестовый аккаунт(0) из баннера anvil: {banner_lines}")

        # Готовность узла.
        ready = False
        for _ in range(30):
            proc = run([CAST, "block-number", "--rpc-url", RPC], timeout=10)
            if proc.returncode == 0:
                result["anvil_block_after_fork"] = proc.stdout.strip()
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise RuntimeError("anvil не ответил на cast block-number за 30с")

        # --- Деплой ClosedCycleExecutorV4(owner=deployer, poolManager) ---
        bytecode = (REPO_ROOT / "contracts" / "build" / "ClosedCycleExecutorV4.bytecode.txt").read_text().strip()
        bytecode_hex = bytecode[2:] if bytecode.startswith("0x") else bytecode

        def word_addr(a: str) -> str:
            return a[2:].rjust(64, "0")

        creation_calldata = "0x" + bytecode_hex + word_addr(deployer_addr) + word_addr(POOL_MANAGER)

        print("[fork_sim] деплою ClosedCycleExecutorV4...")
        deploy_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", RPC,
                            "--create", creation_calldata, "--json"], timeout=60)
        result["deploy_stdout"] = deploy_proc.stdout.strip()
        result["deploy_stderr"] = deploy_proc.stderr.strip()
        result["deploy_returncode"] = deploy_proc.returncode
        if deploy_proc.returncode != 0:
            raise RuntimeError(f"деплой упал: {deploy_proc.stderr}")
        deploy_receipt = json.loads(deploy_proc.stdout)
        contract_addr = deploy_receipt.get("contractAddress")
        result["contract_addr"] = contract_addr
        if not contract_addr:
            raise RuntimeError(f"в receipt деплоя нет contractAddress: {deploy_receipt}")

        # --- Пополнение контракта USDG через impersonate PoolManager ---
        print("[fork_sim] пополняю контракт 9.437184 USDG через impersonate PoolManager...")
        imp = run([CAST, "rpc", "anvil_impersonateAccount", POOL_MANAGER, "--rpc-url", RPC], timeout=15)
        result["impersonate_stdout"] = imp.stdout.strip()
        result["impersonate_returncode"] = imp.returncode
        bal = run([CAST, "rpc", "anvil_setBalance", POOL_MANAGER, hex(10**19), "--rpc-url", RPC], timeout=15)
        result["set_balance_returncode"] = bal.returncode

        fund_proc = run([CAST, "send", "--unlocked", "--from", POOL_MANAGER, "--rpc-url", RPC,
                          USDG, "transfer(address,uint256)", contract_addr, str(PRINCIPAL_USDG_RAW), "--json"],
                         timeout=30)
        result["fund_stdout"] = fund_proc.stdout.strip()
        result["fund_stderr"] = fund_proc.stderr.strip()
        result["fund_returncode"] = fund_proc.returncode
        if fund_proc.returncode != 0:
            raise RuntimeError(f"пополнение USDG упало: {fund_proc.stderr}")

        bal_before_proc = run([CAST, "call", USDG, "balanceOf(address)(uint256)", contract_addr, "--rpc-url", RPC], timeout=15)
        balance_before = int(bal_before_proc.stdout.strip().split()[0])
        result["contract_usdg_balance_before_cycle"] = balance_before

        # --- executeCycle ---
        leg_a = f"({USDG},{MOSIAI},70000,4,{HOOKS_NONE},true,{MIN_SQRT_PRICE + 1},0x)"
        leg_b = f"({USDG},{MOSIAI},70000,3,{HOOKS_NONE},false,{MAX_SQRT_PRICE - 1},0x)"
        cycle_params = f"([{leg_a},{leg_b}],-{PRINCIPAL_USDG_RAW},{USDG},0)"
        sig = "executeCycle(((address,address,uint24,int24,address,bool,uint160,bytes)[],int256,address,uint256))"

        print("[fork_sim] вызываю executeCycle...")
        exec_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", RPC,
                          contract_addr, sig, cycle_params, "--json"], timeout=60)
        result["execute_cycle_stdout"] = exec_proc.stdout.strip()
        result["execute_cycle_stderr"] = exec_proc.stderr.strip()
        result["execute_cycle_returncode"] = exec_proc.returncode

        bal_after_proc = run([CAST, "call", USDG, "balanceOf(address)(uint256)", contract_addr, "--rpc-url", RPC], timeout=15)
        balance_after = int(bal_after_proc.stdout.strip().split()[0])
        result["contract_usdg_balance_after_cycle"] = balance_after
        result["profit_usdg_raw"] = balance_after - balance_before
        result["profit_usdg"] = (balance_after - balance_before) / 1e6
        result["execute_cycle_succeeded"] = exec_proc.returncode == 0

        if exec_proc.returncode == 0:
            receipt = json.loads(exec_proc.stdout)
            result["execute_cycle_tx_hash"] = receipt.get("transactionHash")
            result["execute_cycle_status"] = receipt.get("status")
            result["execute_cycle_gas_used"] = receipt.get("gasUsed")

        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["error"] = str(exc)
        print(f"[fork_sim] ОШИБКА: {exc}", file=sys.stderr)
    finally:
        if anvil_proc is not None:
            anvil_proc.terminate()
            try:
                anvil_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                anvil_proc.kill()

    print(json.dumps(result, indent=2, default=str))
    out_path = REPO_ROOT / "data" / "task5_v4_fork_simulation_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
