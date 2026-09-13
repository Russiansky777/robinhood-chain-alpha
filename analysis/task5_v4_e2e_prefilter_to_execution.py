#!/usr/bin/env python3
"""Задача 5, сквозной результат нового быстрого отбора (владелец:
"сузим цель... доведи новый быстрый отбор до одного сквозного
результата на уже известном контрольном примере: состояние перед
сделкой конкурента -> обновление кэша -> кандидат от фильтра ->
автоматический подбор размера -> успешное исполнение нашим контрактом
на форке").

Контрольный пример -- ТОТ ЖЕ единственный кандидат, что и round10/11/12
(tx 0x878fb998..., блок 61636697, маршрут ETH/USDG -> USDG/0x35a791 ->
ETH/0x35a791). Состояние -- ИСТОРИЧЕСКОЕ (форк на TARGET_BLOCK-1 +
реплей предшествующих tx ТОГО ЖЕ блока, та же методология round11/12),
НЕЗАВИСИМО от сегодняшнего is_live реестра (владелец: "сегодняшний
is_live не должен подменять историческое состояние").

Цепочка:
  1. Анвил-форк на историческое состояние (ДО сделки конкурента).
  2. Кэш заполняется extsload'ом ТРЁХ пулов маршрута НА ЭТОМ форке
     (не событиями -- это ХОЛОДНАЯ инициализация на конкретном блоке,
     та же формула, что уже дважды независимо подтверждена).
  3. cheap_filter_route() -- решение фильтра (кандидат или явно
     "модель не поддерживается").
  4. Если кандидат -- hp.recompute_route() (СУЩЕСТВУЮЩИЙ, НЕТРОНУТЫЙ
     перебор сетки) сам выбирает размер.
  5. Развёртывание/финансирование/отправка через НАШ контракт на этом
     же форке, реальный чек рецепта.

Ничего в контракте/Sender/бюджете не меняется. Не открывает новый скан."""
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
from task5_v4_pool_state_cache import PoolStateCache, cheap_filter_route, extsload_pool_state  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
ANVIL = str(FOUNDRY_BIN / "anvil")
CAST = str(FOUNDRY_BIN / "cast")
PORT = 8567
RPC = f"http://127.0.0.1:{PORT}"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
NATIVE = "0x0000000000000000000000000000000000000000"
RESULT_FILE = REPO_ROOT / "data" / "task5_v4_item3_in_window_control_trade_result.json"
TARGET_TX = "0x878fb998474230338a47a710d2c25689987a836074729ae61e112dcccca7cd1b"
TARGET_BLOCK = 61636697


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def anvil_start(fork_block: int):
    fork_url = _alchemy_direct_endpoint()
    if not fork_url:
        raise RuntimeError("нет Alchemy-эндпоинта для форка")
    proc = subprocess.Popen([ANVIL, "--fork-url", fork_url, "--fork-block-number", str(fork_block),
                              "--port", str(PORT)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
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
    if not addr_m or not key_m:
        raise RuntimeError(f"не удалось распарсить тестовый аккаунт(0): {banner}")
    for _ in range(30):
        if run([CAST, "block-number", "--rpc-url", RPC], timeout=10).returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("anvil не ответил за 30с")
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
    return {"returncode": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip(), "tx_hash": tx_hash}


def main() -> None:
    result: dict = {"target_tx_hash": TARGET_TX, "target_block": TARGET_BLOCK}
    chain_t0 = time.monotonic()
    hp._reset_rpc_call_count()

    # --- Маршрут -- ТА ЖЕ реконструкция, что round10/11 (реюз, не пересчёт) ---
    d = json.loads(RESULT_FILE.read_text())
    row = next(r for r in d["fund_flow_checks"] if r["tx_hash"].lower() == TARGET_TX.lower())
    route_legs = []
    for leg in row["legs"]:
        init = fetch_initialize_event(leg["pool_id"], TARGET_BLOCK)
        zero_for_one = leg["amount0"] < 0
        route_legs.append(RouteLeg(init["currency0"], init["currency1"], init["fee"], init["tick_spacing"],
                                    init["hooks"], zero_for_one))
    route = RouteCycle("route_e2e_878fb998", tuple(route_legs), route_legs[0].input_currency,
                        "e2e-prefilter", "discovered")
    result["route_legs"] = [{"pool_id": leg.pool_id_hex, "hooks": leg.hooks, "zero_for_one": leg.zero_for_one}
                             for leg in route.legs]

    anvil_proc = None
    orig_config, orig_checked, orig_url = af.CONFIG, af._alchemy_direct_checked, af._alchemy_direct_url
    try:
        t_fork0 = time.monotonic()
        anvil_proc, deployer_addr, deployer_key = anvil_start(TARGET_BLOCK - 1)
        result["fork_block"] = TARGET_BLOCK - 1

        block_full = _rpc_call("eth_getBlockByNumber", [hex(TARGET_BLOCK), True])
        txs = block_full["transactions"]
        tx_index = next(i for i, t in enumerate(txs) if t["hash"].lower() == TARGET_TX.lower())
        for i in range(tx_index):
            t = txs[i]
            r = impersonate_and_send(t["from"], t.get("to"), t.get("input", "0x"), t.get("value", "0x0"),
                                      hex(5_000_000))
            if r["returncode"] != 0:
                result["ok"] = False
                result["error"] = f"предшествующая tx #{i} не реплеилась: {r}"
                print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
                return
        result["t_fork_and_replay_s"] = time.monotonic() - t_fork0

        af._alchemy_direct_checked = True
        af._alchemy_direct_url = None
        af.CONFIG = dataclasses.replace(af.CONFIG, public_rpc_url=RPC, alchemy_rpc_url="", alchemy_api_key="")
        local_block = int(run([CAST, "block-number", "--rpc-url", RPC], timeout=10).stdout.strip())
        result["local_block_before_competitor_tx"] = local_block
        result["note_historical_state"] = ("Состояние -- РЕАЛЬНОЕ форкнутое+реплеенное историческое (ДО tx "
                                            "конкурента), НЕЗАВИСИМО от сегодняшнего registry.is_live -- маршрут "
                                            "здесь вообще не берётся из текущего реестра пилота.")

        # --- Шаг 2: кэш -- ХОЛОДНАЯ инициализация extsload'ом НА ЭТОМ форке/блоке ---
        t_cache0 = time.monotonic()
        cache = PoolStateCache()
        cold_states = {}
        for leg in route.legs:
            pid = leg.pool_id_hex
            state = extsload_pool_state(pid, hex(local_block), _rpc_call, POOL_MANAGER)
            cache.seed_cold(pid, local_block, state)
            cold_states[pid] = state
        result["cold_extsload_states"] = cold_states
        result["cache_all_pools_initialized"] = all(cache.is_initialized(leg.pool_id_hex) for leg in route.legs)
        result["cache_all_pools_fresh"] = all(cache.is_fresh(leg.pool_id_hex) for leg in route.legs)
        result["t_cache_init_s"] = time.monotonic() - t_cache0

        # --- Шаг 3: решение фильтра ---
        t_filter0 = time.monotonic()
        filter_result = cheap_filter_route(route, cache, local_block)
        result["cheap_filter_result"] = dataclasses.asdict(filter_result)
        result["t_filter_s"] = time.monotonic() - t_filter0

        if filter_result.verdict == "hook_model_absent":
            result["ok"] = False
            result["end_to_end_achieved"] = False
            result["blocker"] = ("модель хука не подтверждена для одного из плеч -- см. cheap_filter_result."
                                  "uncovered_pool_ids; кандидат честно не подан фильтром дальше по цепочке")
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return
        if filter_result.verdict == "not_initialized":
            result["ok"] = False
            result["end_to_end_achieved"] = False
            result["blocker"] = "кэш не инициализирован для одного из пулов после extsload -- см. detail"
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return

        # --- Шаг 4: автоматический подбор размера -- СУЩЕСТВУЮЩИЙ, НЕТРОНУТЫЙ recompute_route ---
        t_recompute0 = time.monotonic()
        recompute = hp.recompute_route(route, local_block)
        result["recompute_route_result"] = recompute
        result["t_recompute_s"] = time.monotonic() - t_recompute0

        if not recompute.get("ok") or recompute.get("profit_raw", -1) <= 0:
            result["ok"] = False
            result["end_to_end_achieved"] = False
            result["blocker"] = f"фильтр пропустил маршрут, но recompute_route не нашёл прибыльный размер: {recompute}"
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return

        amount_in = recompute["amount_in"]
        result["auto_selected_amount_in"] = amount_in

        # --- Шаг 5: развёртывание + отправка НАШИМ контрактом ---
        t_exec0 = time.monotonic()
        bytecode = (REPO_ROOT / "contracts" / "build" / "ClosedCycleExecutorV4.bytecode.txt").read_text().strip()
        bytecode_hex = bytecode[2:] if bytecode.startswith("0x") else bytecode

        def word_addr(a: str) -> str:
            return a[2:].rjust(64, "0")

        creation_calldata = "0x" + bytecode_hex + word_addr(deployer_addr) + word_addr(POOL_MANAGER)
        deploy_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", RPC,
                            "--create", creation_calldata, "--json"], timeout=60)
        result["deploy_returncode"] = deploy_proc.returncode
        if deploy_proc.returncode != 0:
            result["ok"] = False
            result["end_to_end_achieved"] = False
            result["blocker"] = f"деплой контракта на форк не удался: {deploy_proc.stderr}"
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return
        contract_addr = json.loads(deploy_proc.stdout).get("contractAddress")
        result["contract_addr_on_fork"] = contract_addr

        start_token = route.legs[0].input_currency
        if start_token.lower() != NATIVE:
            run([CAST, "rpc", "anvil_impersonateAccount", POOL_MANAGER, "--rpc-url", RPC], timeout=15)
            run([CAST, "rpc", "anvil_setBalance", POOL_MANAGER, hex(10 ** 20), "--rpc-url", RPC], timeout=15)
            fund_proc = run([CAST, "send", "--unlocked", "--from", POOL_MANAGER, "--rpc-url", RPC,
                              start_token, "transfer(address,uint256)", contract_addr, str(amount_in),
                              "--json"], timeout=30)
            result["fund_contract_returncode"] = fund_proc.returncode
        else:
            run([CAST, "rpc", "anvil_setBalance", contract_addr, hex(amount_in + 10 ** 17), "--rpc-url", RPC],
                timeout=15)

        calldata = build_execute_cycle_calldata(route, -amount_in, min_profit=1)
        gas_res = hp.estimate_gas(contract_addr, calldata, deployer_addr)
        result["gas_estimate"] = gas_res
        send_gas_limit = max(gas_res.get("gas_estimate", 300000) * 2, 300000)
        send_res = impersonate_and_send(deployer_addr, contract_addr, "0x" + calldata.hex(), "0x0",
                                          hex(send_gas_limit))
        result["execute_send"] = send_res
        if send_res["returncode"] == 0 and send_res["tx_hash"]:
            receipt = _rpc_call("eth_getTransactionReceipt", [send_res["tx_hash"]])
            result["execute_receipt_status"] = int(receipt["status"], 16) if receipt else None
        result["t_execute_s"] = time.monotonic() - t_exec0

        result["end_to_end_achieved"] = result.get("execute_receipt_status") == 1
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["end_to_end_achieved"] = False
        result["error"] = str(exc)
    finally:
        result["t_chain_total_s"] = time.monotonic() - chain_t0
        result["n_rpc_calls_total"] = hp._read_rpc_call_count()
        if anvil_proc is not None:
            anvil_proc.terminate()
            try:
                anvil_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                anvil_proc.kill()
        af.CONFIG, af._alchemy_direct_checked, af._alchemy_direct_url = orig_config, orig_checked, orig_url

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = REPO_ROOT / "data" / "task5_v4_e2e_prefilter_to_execution_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
