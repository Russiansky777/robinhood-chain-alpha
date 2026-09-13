#!/usr/bin/env python3
"""Задача 5, десятый раунд (пункт 3, реконструкция уже отобранного
контрольного примера -- владелец: "реконструкция одного подходящего
примера уже входила в задание, дополнительное подтверждение не
требуется").

Кандидат: tx 0x878fb998474230338a47a710d2c25689987a836074729ae61e112dcccca7cd1b
(блок 61636697) -- первый (по блоку) из 120 кандидатов (знак уже
исправлен, task5_v4_item3_round10_select_candidate.py), удовлетворяющий
ВСЕМ критериям владельца: внутри окна пилота, 3 последовательных V4-свопа
через НАШ PoolManager, начинается/заканчивается в native ETH, без V3/WETH-
обёртки, БЕЗ хуков ни на одном плече (hooks=null у всех трёх -- проверено
по уже сохранённым данным, task5_v4_item3_round10_full_row.py).

КРИТИЧНО (прямое указание владельца): "Нельзя сначала изменить пулы нашей
сделкой, затем на изменённом состоянии проверять конкурента." --
используется ОДИН исходный anvil-снимок (evm_snapshot СРАЗУ ПОСЛЕ реплея
всех предшествующих транзакций блока, ДО любых действий) -- (a) наша
котировка и (b) наше исполнение выполняются на этом снимке и МУТИРУЮТ
состояние; затем evm_revert ВОЗВРАЩАЕТ это же самое состояние (то, что
было СРАЗУ после предшествующих tx, ДО нашей сделки); ТОЛЬКО ПОСЛЕ этого
реплеится (c) реальная транзакция конкурента -- на состоянии, которое
НАША сделка не касалась.

Данные Initialize (currency0/currency1/fee/tick_spacing/hooks) для 3
pool_id этого ОДНОГО кандидата запрашиваются здесь напрямую (маленький,
точечный запрос по трём уже известным pool_id -- НЕ широкий скан)."""
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

from alchemy_fallback import _alchemy_direct_endpoint, _rpc_call, get_block  # noqa: E402
import alchemy_fallback as af  # noqa: E402
import task5_v4_hotpath as hp  # noqa: E402
from task5_v4_route_registry import RouteCycle, RouteLeg  # noqa: E402
from task5_v4_hook_route_audit import fetch_initialize_event  # noqa: E402
from task5_v4_executor_calldata import build_execute_cycle_calldata  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
ANVIL = str(FOUNDRY_BIN / "anvil")
CAST = str(FOUNDRY_BIN / "cast")
PORT = 8561
RPC = f"http://127.0.0.1:{PORT}"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
NATIVE = "0x0000000000000000000000000000000000000000"
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

RESULT_FILE = REPO_ROOT / "data" / "task5_v4_item3_in_window_control_trade_result.json"
REASON_LOG_FILE = Path("/home/bot/data/task5_v4_pilot_no_send_log.jsonl")
ATTEMPT_TABLE_FILE = Path("/home/bot/data/task5_v4_pilot_attempts.jsonl")

TARGET_TX = "0x878fb998474230338a47a710d2c25689987a836074729ae61e112dcccca7cd1b"
TARGET_BLOCK = 61636697


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def _leg_io(leg: dict) -> tuple[bool, str, str]:
    """ПРАВКА ЗНАКА (владелец): raw amount0/amount1 -- уже дельта
    ТРЕЙДЕРА (не пула). zero_for_one (платим currency0) <=> amount0<0."""
    zero_for_one = leg["amount0"] < 0
    input_currency = leg["currency0"] if zero_for_one else leg["currency1"]
    output_currency = leg["currency1"] if zero_for_one else leg["currency0"]
    return zero_for_one, input_currency, output_currency


def anvil_start(fork_block: int) -> tuple[subprocess.Popen, str, str]:
    fork_url = _alchemy_direct_endpoint()
    if not fork_url:
        raise RuntimeError("нет Alchemy-эндпоинта для форка")
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
    if not addr_m or not key_m:
        raise RuntimeError(f"не удалось распарсить тестовый аккаунт(0): {banner}")
    for _ in range(30):
        p = run([CAST, "block-number", "--rpc-url", RPC], timeout=10)
        if p.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("anvil не ответил за 30с")
    return proc, addr_m.group(1), key_m.group(1)


def impersonate_and_send(from_addr: str, to_addr: str | None, data: str, value_hex: str, gas_hex: str) -> dict:
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


def anvil_snapshot() -> str:
    p = run([CAST, "rpc", "evm_snapshot", "--rpc-url", RPC], timeout=15)
    return json.loads(p.stdout.strip())


def anvil_revert(snap_id: str) -> bool:
    p = run([CAST, "rpc", "evm_revert", snap_id, "--rpc-url", RPC], timeout=15)
    try:
        return bool(json.loads(p.stdout.strip()))
    except (ValueError, json.JSONDecodeError):
        return False


def decode_receipt_transfers_all(receipt: dict) -> list[dict]:
    out = []
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if not topics or topics[0].lower() != TRANSFER_TOPIC0 or len(topics) < 3:
            continue
        frm = "0x" + topics[1][-40:]
        to = "0x" + topics[2][-40:]
        amount = int(log["data"], 16) if log["data"] not in ("0x", "") else 0
        out.append({"token": log["address"].lower(), "from": frm.lower(), "to": to.lower(), "amount": amount})
    return out


def cross_reference_own_logs(pool_ids: list[str], target_block: int, target_ts: int) -> dict:
    result: dict = {"pool_ids": pool_ids, "target_block": target_block}
    window_s = 3600
    short_hashes = [pid[2:10] for pid in pool_ids]

    def _scan_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        out = []
        for line in path.open():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            ts = rec.get("ts_wall")
            if ts is None or abs(ts - target_ts) > window_s:
                continue
            route_id = rec.get("route_id", "")
            if any(h in route_id for h in short_hashes):
                out.append(rec)
        return out

    result["reason_log_file_exists"] = REASON_LOG_FILE.exists()
    result["reason_log_matches"] = _scan_jsonl(REASON_LOG_FILE)
    result["attempt_table_file_exists"] = ATTEMPT_TABLE_FILE.exists()
    result["attempt_table_matches"] = _scan_jsonl(ATTEMPT_TABLE_FILE)
    if not result["reason_log_matches"] and not result["attempt_table_matches"]:
        result["conclusion"] = (
            f"НЕ найдено записей (ни отказа, ни попытки) для этих pool_id в окне ±{window_s}с вокруг "
            f"блока {target_block} -- честно 'не зафиксировано'"
        )
    else:
        result["conclusion"] = "найдены записи -- см. reason_log_matches/attempt_table_matches"
    return result


def _sum_value_transfers(trace_node: dict, acc: dict[str, int]) -> None:
    """Рекурсивно суммирует value (native ETH), реально перешедшую между
    адресами по call-трассе -- Transfer-логов для native ETH не бывает,
    это единственный способ честно проследить, куда делось native ETH."""
    frm = trace_node.get("from", "").lower()
    to = trace_node.get("to", "").lower()
    value_hex = trace_node.get("value") or "0x0"
    try:
        value = int(value_hex, 16)
    except (ValueError, TypeError):
        value = 0
    if value and frm and to:
        acc[frm] = acc.get(frm, 0) - value
        acc[to] = acc.get(to, 0) + value
    for child in trace_node.get("calls", []) or []:
        _sum_value_transfers(child, acc)


def main() -> None:
    result: dict = {"target_tx_hash": TARGET_TX, "target_block": TARGET_BLOCK}

    d = json.loads(RESULT_FILE.read_text())
    ffc = d.get("fund_flow_checks") or []
    row = next(r for r in ffc if r["tx_hash"].lower() == TARGET_TX.lower())
    legs_saved = row["legs"]
    result["saved_row_tx_from"] = row.get("tx_from")
    result["saved_row_tx_to"] = row.get("tx_to")
    result["saved_row_all_transfers_in_receipt"] = row.get("all_transfers_in_receipt")
    result["saved_row_positive_net_transfer_addresses"] = row.get("positive_net_transfer_addresses")
    result["saved_row_other_token_spend_by_executor"] = row.get("other_token_spend_by_executor")

    # --- Точечный запрос Initialize (fee/tick_spacing/hooks) ДЛЯ ЭТИХ
    # ТРЁХ, УЖЕ ИЗВЕСТНЫХ pool_id -- НЕ широкий скан. ---
    route_legs = []
    pool_init_by_id = {}
    for leg in legs_saved:
        pid = leg["pool_id"]
        init = fetch_initialize_event(pid, TARGET_BLOCK)
        if init is None:
            result["ok"] = False
            result["error"] = f"Initialize не найден для {pid} -- маршрут не реконструирован"
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return
        pool_init_by_id[pid] = init
        zero_for_one, _, _ = _leg_io(leg)
        route_legs.append(RouteLeg(init["currency0"], init["currency1"], init["fee"], init["tick_spacing"],
                                    init["hooks"], zero_for_one))
        # сверка: пересчитанный pool_id ДОЛЖЕН совпасть с реально сохранённым (см. RouteLeg.pool_id_hex)
        recomputed_pid = route_legs[-1].pool_id_hex
        if recomputed_pid.lower() != pid.lower():
            result["ok"] = False
            result["error"] = (f"пересчитанный pool_id ({recomputed_pid}) НЕ совпал с сохранённым ({pid}) для "
                                "плеча -- Initialize/fee/tick_spacing не соответствуют реальному пулу, "
                                "останавливаемся, не подгоняем")
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return

    exit_token = route_legs[0].input_currency
    if route_legs[-1].output_currency.lower() != exit_token.lower():
        result["ok"] = False
        result["error"] = f"маршрут не замкнут: старт={exit_token}, конец={route_legs[-1].output_currency}"
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return
    route = RouteCycle("route_round10_878fb998", tuple(route_legs), exit_token, "round10-selected-candidate",
                        "discovered")
    result["reconstructed_route"] = {
        "route_id": route.route_id, "exit_token": exit_token,
        "legs": [{"currency0": l.currency0, "currency1": l.currency1, "fee": l.fee,
                   "tick_spacing": l.tick_spacing, "hooks": l.hooks, "zero_for_one": l.zero_for_one}
                  for l in route.legs],
    }

    anvil_proc = None
    orig_config = af.CONFIG
    orig_checked = af._alchemy_direct_checked
    orig_url = af._alchemy_direct_url
    try:
        anvil_proc, deployer_addr, deployer_key = anvil_start(TARGET_BLOCK - 1)
        result["fork_block"] = TARGET_BLOCK - 1
        result["deployer_addr"] = deployer_addr

        block_full = _rpc_call("eth_getBlockByNumber", [hex(TARGET_BLOCK), True])
        txs = block_full["transactions"]
        tx_index = next(i for i, t in enumerate(txs) if t["hash"].lower() == TARGET_TX.lower())
        result["n_preceding_txs"] = tx_index

        GENEROUS_GAS = hex(5_000_000)
        replayed = []
        for i in range(tx_index):
            t = txs[i]
            r = impersonate_and_send(t["from"], t.get("to"), t.get("input", "0x"), t.get("value", "0x0"),
                                      GENEROUS_GAS)
            replayed.append({"orig_hash": t["hash"], "from": t["from"], "send_result": r})
        failed = [r for r in replayed if r["send_result"]["returncode"] != 0]
        result["all_preceding_replayed_ok"] = len(failed) == 0
        if failed:
            result["ok"] = False
            result["error"] = f"{len(failed)} из {tx_index} предшествующих tx не реплеились даже со щедрым газом"
            result["failed_preceding"] = [(r["orig_hash"], r["send_result"]["stderr"]) for r in failed]
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return

        # === ОБЩИЙ ИСХОДНЫЙ СНИМОК -- состояние СРАЗУ после предшествующих
        # tx, ДО чего-либо ещё. И наша сделка, и сделка конкурента стартуют
        # РОВНО отсюда, независимо друг от друга (см. докстринг). ===
        shared_snapshot = anvil_snapshot()
        result["shared_snapshot_id"] = shared_snapshot

        af._alchemy_direct_checked = True
        af._alchemy_direct_url = None
        af.CONFIG = dataclasses.replace(af.CONFIG, public_rpc_url=RPC, alchemy_rpc_url="", alchemy_api_key="")

        local_block_before_target = int(run([CAST, "block-number", "--rpc-url", RPC], timeout=10).stdout.strip())
        result["local_block_immediately_after_preceding_txs"] = local_block_before_target

        # --- (a) НАША котировка (чтение, но по дисциплине идёт "на нашей
        # стороне" снимка) ---
        recompute = hp.recompute_route(route, local_block_before_target)
        result["our_quote"] = recompute

        our_exec: dict = {}
        if recompute.get("ok"):
            bytecode = (REPO_ROOT / "contracts" / "build" / "ClosedCycleExecutorV4.bytecode.txt").read_text().strip()
            bytecode_hex = bytecode[2:] if bytecode.startswith("0x") else bytecode

            def word_addr(a: str) -> str:
                return a[2:].rjust(64, "0")

            creation_calldata = "0x" + bytecode_hex + word_addr(deployer_addr) + word_addr(POOL_MANAGER)
            deploy_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", RPC,
                                "--create", creation_calldata, "--json"], timeout=60)
            our_exec["deploy_returncode"] = deploy_proc.returncode
            if deploy_proc.returncode == 0:
                contract_addr = json.loads(deploy_proc.stdout).get("contractAddress")
                our_exec["our_contract_addr_on_fork"] = contract_addr
                amount_in = recompute["amount_in"]
                run([CAST, "rpc", "anvil_impersonateAccount", POOL_MANAGER, "--rpc-url", RPC], timeout=15)
                run([CAST, "rpc", "anvil_setBalance", POOL_MANAGER, hex(10 ** 20), "--rpc-url", RPC], timeout=15)
                start_token = route.legs[0].input_currency
                if start_token.lower() != NATIVE:
                    fund_proc = run([CAST, "send", "--unlocked", "--from", POOL_MANAGER, "--rpc-url", RPC,
                                      start_token, "transfer(address,uint256)", contract_addr, str(amount_in),
                                      "--json"], timeout=30)
                    our_exec["fund_contract_returncode"] = fund_proc.returncode
                else:
                    run([CAST, "rpc", "anvil_setBalance", contract_addr, hex(amount_in + 10 ** 17),
                          "--rpc-url", RPC], timeout=15)
                calldata = build_execute_cycle_calldata(route, -amount_in, min_profit=1)
                gas_res = hp.estimate_gas(contract_addr, calldata, deployer_addr)
                our_exec["gas_estimate"] = gas_res
                send_gas_limit = max(gas_res.get("gas_estimate", 300000) * 2, 300000)
                send_res = impersonate_and_send(deployer_addr, contract_addr, "0x" + calldata.hex(), "0x0",
                                                  hex(send_gas_limit))
                our_exec["execute_send"] = send_res
                if send_res["returncode"] == 0 and send_res["tx_hash"]:
                    our_receipt = _rpc_call("eth_getTransactionReceipt", [send_res["tx_hash"]])
                    our_exec["execute_receipt_status"] = int(our_receipt["status"], 16) if our_receipt else None
        result["our_execution"] = our_exec

        # === ВОССТАНОВЛЕНИЕ: откат к ОБЩЕМУ снимку ДО того, как трогать
        # состояние сделкой конкурента -- конкурент реплеится НЕ на
        # состоянии, изменённом нашей сделкой. ===
        revert_ok = anvil_revert(shared_snapshot)
        result["snapshot_revert_ok"] = revert_ok
        if not revert_ok:
            result["ok"] = False
            result["error"] = "evm_revert к общему снимку НЕ подтверждён -- честно останавливаемся, не продолжаем на потенциально загрязнённом состоянии"
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return

        # --- (c) реплей РЕАЛЬНОЙ tx конкурента -- на снимке ДО нашей сделки ---
        # Баланс исполнителя и отправителя ДО отправки (тот же локальный блок,
        # что и общий снимок) -- обычный eth_getBalance с явным номером блока,
        # НЕ требует debug_* и не проксируется на платный тариф апстрима.
        target_tx = txs[tx_index]
        executor_addr_native = row.get("tx_to") or row.get("tx_from")
        # ПРАВКА (баланс исполнителя оказался 0 ДО и ПОСЛЕ -- контракт явно
        # НЕ держит ETH в себе, пересылает дальше В ТОЙ ЖЕ tx, как и в
        # разобранном ранее примере с USDG-получателем). debug_traceTransaction
        # заблокирован тарифом апстрима -- проверяем баланс РЕАЛЬНЫХ адресов-
        # кандидатов на получение (хуки маршрута, PoolManager, и уже реально
        # встреченный в этой же сессии адрес-получатель 0x11854ce19d... из
        # ДРУГОЙ, ранее разобранной tx конкурента) -- честная проверка
        # гипотезы, не трасса, но тоже обычный eth_getBalance.
        KNOWN_PROFIT_COLLECTOR = "0x11854ce19dcd63a7eccaa12ded5aa991c94c79c6"
        addresses_of_interest = {"executor": executor_addr_native, "tx_from": target_tx["from"],
                                   "pool_manager": POOL_MANAGER, "known_profit_collector": KNOWN_PROFIT_COLLECTOR}
        for leg in route.legs:
            if leg.hooks and leg.hooks.lower() != NATIVE:
                addresses_of_interest[f"hook_{leg.hooks.lower()}"] = leg.hooks
        result["addresses_of_interest"] = addresses_of_interest
        balances_before = {
            name: int(_rpc_call("eth_getBalance", [addr, hex(local_block_before_target)]), 16)
            for name, addr in addresses_of_interest.items()
        }
        result["balances_before_competitor_tx"] = balances_before

        target_replay = impersonate_and_send(target_tx["from"], target_tx.get("to"), target_tx.get("input", "0x"),
                                              target_tx.get("value", "0x0"), target_tx.get("gas", "0x2dc6c0"))
        result["competitor_target_tx_replay"] = target_replay
        if target_replay["returncode"] == 0 and target_replay["tx_hash"]:
            local_hash = target_replay["tx_hash"]
            comp_receipt = None
            for _ in range(10):
                comp_receipt = _rpc_call("eth_getTransactionReceipt", [local_hash])
                if comp_receipt is not None:
                    break
                time.sleep(1)
            result["competitor_replay_receipt_status"] = int(comp_receipt["status"], 16) if comp_receipt else None
            result["competitor_replay_matches_real_status"] = (
                comp_receipt is not None and int(comp_receipt["status"], 16) == row.get("status")
            )

            local_block_after_target = int(run([CAST, "block-number", "--rpc-url", RPC], timeout=10).stdout.strip())
            balances_after = {
                name: int(_rpc_call("eth_getBalance", [addr, hex(local_block_after_target)]), 16)
                for name, addr in addresses_of_interest.items()
            }
            result["balances_after_competitor_tx"] = balances_after
            balance_diff = {f"{name}_native_eth_delta": balances_after[name] - balances_before[name]
                              for name in addresses_of_interest}
            balance_diff["tx_from_delta_note"] = (
                "НЕ показательно -- impersonate_and_send() искусственно выставляет tx.from баланс в 10**20 "
                "перед отправкой (anvil_setBalance) для гарантии газа/value; ВСЕ остальные (executor, "
                "pool_manager, known_profit_collector, hook_*) -- органические, честные показатели "
                "(impersonate не трогает их балансы)"
            )
            result["balance_diff_competitor_tx"] = balance_diff

            # value-трасса локально СМАЙНЕННОЙ tx -- debug_traceTransaction на
            # УЖЕ существующей tx (не гипотетический блок) -- см. итем4, тот
            # же обходной путь проблемы "Excess blob gas not set". ПРАВКА
            # (этот прогон): здесь debug_traceTransaction упал с ДРУГОЙ,
            # честно зафиксированной причиной -- "not available on the Free
            # tier" апстрим-провайдера форка (не hardfork/blob-gas) -- трасса
            # необязательна, т.к. balance_diff выше уже даёт честный ответ
            # на native-ETH профит без неё.
            trace_proc = run([CAST, "rpc", "debug_traceTransaction", local_hash,
                               json.dumps({"tracer": "callTracer"}), "--rpc-url", RPC], timeout=30)
            if trace_proc.returncode == 0:
                try:
                    trace = json.loads(trace_proc.stdout.strip())
                    value_transfers: dict[str, int] = {}
                    _sum_value_transfers(trace, value_transfers)
                    result["competitor_replay_value_transfers_by_address"] = value_transfers
                    result["competitor_replay_native_eth_net_by_address"] = {
                        a: v for a, v in value_transfers.items() if v != 0
                    }
                except (ValueError, json.JSONDecodeError):
                    result["competitor_replay_trace_error"] = "не удалось разобрать debug_traceTransaction JSON"
            else:
                result["competitor_replay_trace_error"] = trace_proc.stderr.strip()

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
        af.CONFIG = orig_config
        af._alchemy_direct_checked = orig_checked
        af._alchemy_direct_url = orig_url

    # --- сверка с нашими собственными логами (reason_log/attempts) ---
    pool_ids = [leg["pool_id"] for leg in legs_saved]
    target_blk = get_block(TARGET_BLOCK)
    target_ts = int(target_blk["timestamp"], 16) if target_blk else int(time.time())
    result["own_log_cross_reference"] = cross_reference_own_logs(pool_ids, TARGET_BLOCK, target_ts)

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = REPO_ROOT / "data" / "task5_v4_item3_round10_reconstruct_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[round10_reconstruct] сохранено: {out_path}")


if __name__ == "__main__":
    main()
