#!/usr/bin/env python3
"""Задача 5, двенадцатый раунд -- ПРОДОЛЖЕНИЕ на уже выбранной tx и уже
подтверждённом контрольном примере (round10/round11) -- владелец: "новый
скан и повторную реконструкцию не запускай". Это НЕ новая реконструкция
(не новый кандидат, не новый маршрут, не новый скан) -- ТОЧЕЧНЫЙ повтор
ОДНОГО уже проверенного, детерминированного сценария (форк на тот же
блок-1, реплей тех же 2 предшествующих tx, тот же размер 3799912185593856
wei native ETH, что дал status=1/CycleExecuted в round11) -- нужен только
чтобы забрать поле gasUsed из рецепта, которое round11 не сохранял.

Пункт 1: gasUsed нашего успешного исполнения + прибыль после газа ПО
ЦЕНЕ ГАЗА КОНКУРЕНТА (90630000 wei, реальный effectiveGasPrice его
транзакции, round11)."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _alchemy_direct_endpoint, _rpc_call  # noqa: E402
import alchemy_fallback as af  # noqa: E402
import task5_v4_hotpath as hp  # noqa: E402
from task5_v4_route_registry import RouteCycle, RouteLeg  # noqa: E402
from task5_v4_hook_route_audit import fetch_initialize_event  # noqa: E402
from task5_v4_executor_calldata import build_execute_cycle_calldata  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
ANVIL = str(FOUNDRY_BIN / "anvil")
CAST = str(FOUNDRY_BIN / "cast")
PORT = 8563
RPC = f"http://127.0.0.1:{PORT}"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
NATIVE = "0x0000000000000000000000000000000000000000"
RESULT_FILE = REPO_ROOT / "data" / "task5_v4_item3_in_window_control_trade_result.json"

TARGET_TX = "0x878fb998474230338a47a710d2c25689987a836074729ae61e112dcccca7cd1b"
TARGET_BLOCK = 61636697
COMPETITOR_LEG0_ETH_INPUT = 3799912185593856
COMPETITOR_REAL_GAS_PRICE_WEI = 90630000  # эффективная цена газа реальной tx конкурента (round11)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def _leg_io(leg: dict) -> tuple[bool, str, str]:
    zero_for_one = leg["amount0"] < 0
    return zero_for_one, (leg["currency0"] if zero_for_one else leg["currency1"]), None


def anvil_start(fork_block: int) -> tuple[subprocess.Popen, str, str]:
    fork_url = _alchemy_direct_endpoint()
    proc = subprocess.Popen(
        [ANVIL, "--fork-url", fork_url, "--fork-block-number", str(fork_block), "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    banner: list[str] = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        banner.append(line.rstrip("\n"))
        if "Listening on" in line:
            break
    addr_m = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{40})", "\n".join(banner))
    key_m = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{64})", "\n".join(banner))
    for _ in range(30):
        p = run([CAST, "block-number", "--rpc-url", RPC], timeout=10)
        if p.returncode == 0:
            break
        time.sleep(1)
    return proc, addr_m.group(1), key_m.group(1)


def impersonate_and_send(from_addr, to_addr, data, value_hex, gas_hex) -> dict:
    run([CAST, "rpc", "anvil_impersonateAccount", from_addr, "--rpc-url", RPC], timeout=15)
    run([CAST, "rpc", "anvil_setBalance", from_addr, hex(10 ** 20), "--rpc-url", RPC], timeout=15)
    tx_obj = {"from": from_addr, "data": data, "value": value_hex, "gas": gas_hex}
    if to_addr:
        tx_obj["to"] = to_addr
    p = run([CAST, "rpc", "eth_sendTransaction", json.dumps(tx_obj), "--rpc-url", RPC], timeout=30)
    tx_hash = None
    if p.returncode == 0:
        try:
            tx_hash = json.loads(p.stdout.strip())
        except (ValueError, json.JSONDecodeError):
            tx_hash = p.stdout.strip()
    return {"returncode": p.returncode, "tx_hash": tx_hash, "stderr": p.stderr.strip()}


def main() -> None:
    result: dict = {"target_tx_hash": TARGET_TX, "amount_in": COMPETITOR_LEG0_ETH_INPUT}
    d = json.loads(RESULT_FILE.read_text())
    ffc = d.get("fund_flow_checks") or []
    row = next(r for r in ffc if r["tx_hash"].lower() == TARGET_TX.lower())

    route_legs = []
    for leg in row["legs"]:
        init = fetch_initialize_event(leg["pool_id"], TARGET_BLOCK)
        zero_for_one = leg["amount0"] < 0
        route_legs.append(RouteLeg(init["currency0"], init["currency1"], init["fee"], init["tick_spacing"],
                                    init["hooks"], zero_for_one))
    route = RouteCycle("route_round12", tuple(route_legs), route_legs[0].input_currency, "round12-gas", "discovered")

    anvil_proc = None
    orig_config, orig_checked, orig_url = af.CONFIG, af._alchemy_direct_checked, af._alchemy_direct_url
    try:
        anvil_proc, deployer_addr, deployer_key = anvil_start(TARGET_BLOCK - 1)
        block_full = _rpc_call("eth_getBlockByNumber", [hex(TARGET_BLOCK), True])
        txs = block_full["transactions"]
        tx_index = next(i for i, t in enumerate(txs) if t["hash"].lower() == TARGET_TX.lower())
        for i in range(tx_index):
            t = txs[i]
            impersonate_and_send(t["from"], t.get("to"), t.get("input", "0x"), t.get("value", "0x0"),
                                  hex(5_000_000))

        af._alchemy_direct_checked = True
        af._alchemy_direct_url = None
        af.CONFIG = dataclasses.replace(af.CONFIG, public_rpc_url=RPC, alchemy_rpc_url="", alchemy_api_key="")
        local_block = int(run([CAST, "block-number", "--rpc-url", RPC], timeout=10).stdout.strip())

        bytecode = (REPO_ROOT / "contracts" / "build" / "ClosedCycleExecutorV4.bytecode.txt").read_text().strip()
        bytecode_hex = bytecode[2:] if bytecode.startswith("0x") else bytecode

        def word_addr(a):
            return a[2:].rjust(64, "0")

        creation_calldata = "0x" + bytecode_hex + word_addr(deployer_addr) + word_addr(POOL_MANAGER)
        deploy_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", RPC, "--create",
                            creation_calldata, "--json"], timeout=60)
        contract_addr = json.loads(deploy_proc.stdout).get("contractAddress")

        run([CAST, "rpc", "anvil_setBalance", contract_addr, hex(COMPETITOR_LEG0_ETH_INPUT + 10 ** 17),
              "--rpc-url", RPC], timeout=15)
        calldata = build_execute_cycle_calldata(route, -COMPETITOR_LEG0_ETH_INPUT, min_profit=1)
        gas_res = hp.estimate_gas(contract_addr, calldata, deployer_addr)
        send_gas_limit = max(gas_res.get("gas_estimate", 300000) * 2, 300000)
        send_res = impersonate_and_send(deployer_addr, contract_addr, "0x" + calldata.hex(), "0x0",
                                          hex(send_gas_limit))
        result["execute_send"] = send_res
        if send_res["returncode"] == 0 and send_res["tx_hash"]:
            receipt = _rpc_call("eth_getTransactionReceipt", [send_res["tx_hash"]])
            result["receipt_status"] = int(receipt["status"], 16) if receipt else None
            result["receipt_gas_used"] = int(receipt["gasUsed"], 16) if receipt else None
            profit_raw = None
            for log in (receipt.get("logs", []) if receipt else []):
                if log.get("address", "").lower() == contract_addr.lower():
                    words = [log["data"][2:][i:i+64] for i in range(0, len(log["data"][2:]), 64)]
                    profit_raw = int(words[1], 16)
            result["cycle_profit_raw"] = profit_raw
            if result["receipt_gas_used"] is not None:
                gas_cost_at_competitor_price = result["receipt_gas_used"] * COMPETITOR_REAL_GAS_PRICE_WEI
                result["gas_cost_at_competitor_gas_price_wei"] = gas_cost_at_competitor_price
                result["competitor_real_gas_price_wei_used"] = COMPETITOR_REAL_GAS_PRICE_WEI
                if profit_raw is not None:
                    result["profit_after_gas_wei"] = profit_raw - gas_cost_at_competitor_price
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["error"] = str(exc)
    finally:
        if anvil_proc is not None:
            anvil_proc.terminate()
            try:
                anvil_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                anvil_proc.kill()
        af.CONFIG, af._alchemy_direct_checked, af._alchemy_direct_url = orig_config, orig_checked, orig_url

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
