#!/usr/bin/env python3
"""Перекрёстная проверка: калдата executeCycle, собранная НАШИМ Python-
кодом (task5_v4_executor_calldata.py, через eth_abi), должна дать РОВНО
ТОТ ЖЕ результат, что и калдата, которую строил `cast` через
sig+args в Этапе 2 (task5_v4_fork_simulation.py) -- известный реальный
результат: 0.805382 USDG прибыли на блоке 61248736, маршрут
USDG<->MOSIAI, принципал 9.437184 USDG.

Если совпадает побайтово по итоговой прибыли -- наш собственный
ABI-кодировщик калдаты для горячего пути подтверждён реальным
исполнением на форке, не только локальным round-trip decode."""
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
from task5_v4_executor_calldata import build_execute_cycle_calldata  # noqa: E402
from task5_v4_route_registry import USDG, seed_routes  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
ANVIL = str(FOUNDRY_BIN / "anvil")
CAST = str(FOUNDRY_BIN / "cast")

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
FORK_BLOCK = 61248736
PRINCIPAL_USDG_RAW = 9437184
EXPECTED_PROFIT_USDG_RAW = 805382  # реальный, известный этой сессии результат (Этап 2)
PORT = 8562


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def main() -> None:
    result: dict = {"fork_block": FORK_BLOCK, "principal_usdg_raw": PRINCIPAL_USDG_RAW}
    rpc = f"http://127.0.0.1:{PORT}"
    anvil_proc = None
    try:
        fork_url = _alchemy_direct_endpoint()
        if not fork_url:
            raise RuntimeError("нет Alchemy-эндпоинта")

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
        deploy_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", rpc,
                            "--create", creation_calldata, "--json"], timeout=60)
        if deploy_proc.returncode != 0:
            raise RuntimeError(f"деплой упал: {deploy_proc.stderr}")
        contract_addr = json.loads(deploy_proc.stdout).get("contractAddress")
        result["contract_addr"] = contract_addr

        run([CAST, "rpc", "anvil_impersonateAccount", POOL_MANAGER, "--rpc-url", rpc], timeout=15)
        run([CAST, "rpc", "anvil_setBalance", POOL_MANAGER, hex(10**19), "--rpc-url", rpc], timeout=15)
        fund_proc = run([CAST, "send", "--unlocked", "--from", POOL_MANAGER, "--rpc-url", rpc,
                          USDG, "transfer(address,uint256)", contract_addr, str(PRINCIPAL_USDG_RAW), "--json"], timeout=30)
        if fund_proc.returncode != 0:
            raise RuntimeError(f"пополнение упало: {fund_proc.stderr}")

        bal_before = int(run([CAST, "call", USDG, "balanceOf(address)(uint256)", contract_addr,
                               "--rpc-url", rpc], timeout=15).stdout.strip().split()[0])
        result["balance_before"] = bal_before

        # --- НАША калдата (eth_abi), не cast'овская sig+args ---
        route = seed_routes()[0]  # USDG<->MOSIAI
        calldata = build_execute_cycle_calldata(route, -PRINCIPAL_USDG_RAW, 0)
        calldata_hex = "0x" + calldata.hex()
        result["our_calldata_hex"] = calldata_hex
        result["our_calldata_selector"] = calldata_hex[:10]

        exec_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", rpc,
                          contract_addr, calldata_hex, "--json"], timeout=60)
        result["execute_cycle_returncode"] = exec_proc.returncode
        result["execute_cycle_stdout"] = exec_proc.stdout.strip()
        result["execute_cycle_stderr"] = exec_proc.stderr.strip()

        bal_after = int(run([CAST, "call", USDG, "balanceOf(address)(uint256)", contract_addr,
                              "--rpc-url", rpc], timeout=15).stdout.strip().split()[0])
        result["balance_after"] = bal_after
        result["profit_raw"] = bal_after - bal_before
        result["expected_profit_raw"] = EXPECTED_PROFIT_USDG_RAW
        result["MATCHES_KNOWN_RESULT"] = (result["profit_raw"] == EXPECTED_PROFIT_USDG_RAW
                                           and exec_proc.returncode == 0)
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["error"] = str(exc)
        result["MATCHES_KNOWN_RESULT"] = False
        print(f"[calldata_crosscheck] ОШИБКА: {exc}", file=sys.stderr)
    finally:
        if anvil_proc is not None:
            anvil_proc.terminate()
            try:
                anvil_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                anvil_proc.kill()

    print(json.dumps(result, indent=2, default=str))
    print(f"\n[calldata_crosscheck] ИТОГ: {'MATCH' if result.get('MATCHES_KNOWN_RESULT') else 'MISMATCH/FAIL'}")
    out_path = REPO_ROOT / "data" / "task5_v4_verify_calldata_crosscheck_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
