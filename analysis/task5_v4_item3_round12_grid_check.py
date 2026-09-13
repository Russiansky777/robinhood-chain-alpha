#!/usr/bin/env python3
"""Задача 5, двенадцатый раунд (пункт 3): ОДНА точечная проверка на
уже сохранённом контрольном состоянии -- подтверждает ли обновлённый
(расширенный вниз) hp.recompute_route() ПРИБЫЛЬНЫЙ размер на ТОМ ЖЕ
уже проверенном форк-состоянии (блок 61636697-1 + реплей тех же 2
предшествующих tx). НЕ новый скан, НЕ новая реконструкция кандидата --
тот же единственный маршрут/блок, что и round10/11/12."""
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

REPO_ROOT = Path(__file__).parent.parent
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
ANVIL = str(FOUNDRY_BIN / "anvil")
CAST = str(FOUNDRY_BIN / "cast")
PORT = 8564
RPC = f"http://127.0.0.1:{PORT}"
RESULT_FILE = REPO_ROOT / "data" / "task5_v4_item3_in_window_control_trade_result.json"
TARGET_TX = "0x878fb998474230338a47a710d2c25689987a836074729ae61e112dcccca7cd1b"
TARGET_BLOCK = 61636697


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def anvil_start(fork_block):
    fork_url = _alchemy_direct_endpoint()
    proc = subprocess.Popen([ANVIL, "--fork-url", fork_url, "--fork-block-number", str(fork_block),
                              "--port", str(PORT)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    banner = []
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
    for _ in range(30):
        if run([CAST, "block-number", "--rpc-url", RPC], timeout=10).returncode == 0:
            break
        time.sleep(1)
    return proc


def impersonate_and_send(from_addr, to_addr, data, value_hex, gas_hex):
    run([CAST, "rpc", "anvil_impersonateAccount", from_addr, "--rpc-url", RPC], timeout=15)
    run([CAST, "rpc", "anvil_setBalance", from_addr, hex(10 ** 20), "--rpc-url", RPC], timeout=15)
    tx_obj = {"from": from_addr, "data": data, "value": value_hex, "gas": gas_hex}
    if to_addr:
        tx_obj["to"] = to_addr
    return run([CAST, "rpc", "eth_sendTransaction", json.dumps(tx_obj), "--rpc-url", RPC], timeout=30)


def main():
    result = {"target_tx_hash": TARGET_TX, "eth_grid_used": hp.SIZE_GRID_BY_START_TOKEN[hp.NATIVE]}
    d = json.loads(RESULT_FILE.read_text())
    row = next(r for r in d["fund_flow_checks"] if r["tx_hash"].lower() == TARGET_TX.lower())
    route_legs = []
    for leg in row["legs"]:
        init = fetch_initialize_event(leg["pool_id"], TARGET_BLOCK)
        zero_for_one = leg["amount0"] < 0
        route_legs.append(RouteLeg(init["currency0"], init["currency1"], init["fee"], init["tick_spacing"],
                                    init["hooks"], zero_for_one))
    route = RouteCycle("route_round12_gridcheck", tuple(route_legs), route_legs[0].input_currency,
                        "round12-grid-check", "discovered")

    anvil_proc = None
    orig_config, orig_checked, orig_url = af.CONFIG, af._alchemy_direct_checked, af._alchemy_direct_url
    try:
        anvil_proc = anvil_start(TARGET_BLOCK - 1)
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

        recompute = hp.recompute_route(route, local_block)
        result["recompute_route_result"] = recompute
        result["finds_profitable_size"] = bool(recompute.get("ok") and recompute.get("profit_raw", -1) > 0)
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
