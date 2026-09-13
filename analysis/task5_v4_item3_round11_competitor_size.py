#!/usr/bin/env python3
"""Задача 5, одиннадцатый раунд -- продолжение НА УЖЕ ВЫБРАННОЙ tx и
УЖЕ СОХРАНЁННОЙ реконструкции (round10) -- новый поиск/скан НЕ
запускается, тот же кандидат: 0x878fb998... (блок 61636697).

По прямому указанию владельца:
  1) точный входной размер конкурента -- ИЗ РЕАЛЬНОГО Swap-события
     плеча 0 (leg0.amount0, native ETH, уже сохранён в round10 --
     -3799912185593856, трейдер ЗАПЛАТИЛ ровно это). Тип операции
     (exact input/output) -- пробуем подтвердить debug_traceTransaction
     НА РЕАЛЬНОЙ (не форкнутой) исторической tx через наш обычный
     production RPC (alchemy_fallback) -- ОДИН точечный вызов по уже
     известному хешу, не скан. Если недоступно -- честно фиксируем
     (та же причина тарифа, что и в round10), но это НЕ мешает
     сравнению: наш контракт поддерживает ТОЛЬКО exact-input первого
     плеча (см. ClosedCycleExecutorV4.sol), поэтому воспроизводим
     конкурента ИМЕННО с этим exact-input размером -- единственный
     режим, который наш контракт вообще умеет.
  2) ТОТ ЖЕ общий snapshot (форк на тот же блок-1, реплей тех же 2
     предшествующих tx) -- наша котировка/исполнение на размере
     конкурента, с восстановлением к тому же снимку перед каждым
     следующим сценарием (конкурент / 0.02 ETH). Реальный порядок
     пулов/направлений/fee/tick_spacing/hooks -- ИЗ round10 (сверены
     пересчётом pool_id). hookData: наш build_execute_cycle_calldata()
     ЖЁСТКО кодирует b\"\" для КАЖДОГО плеча (см. её исходник) -- если
     реальная tx конкурента использовала непустой hookData на плече 2
     (хук), это РЕАЛЬНОЕ, неустранимое (без изменения контракта, что
     запрещено) различие -- фиксируется явно, не молчим.
  3) Три результата в одной таблице: конкурент (реальный), наш бот на
     размере конкурента, наш бот на 0.02 ETH.
  4) Прибыль конкурента по получателю 0x11854ce1... (178895272651931
     wei, уже сверено round10) -- отдельно выплата хуку
     (118831530678813) -- реальный gas (из РЕАЛЬНОГО, не форкнутого
     рецепта) и итог после него.
  5) 'Причина -- сетка размеров' подтверждается ТОЛЬКО если наше
     исполнение на размере конкурента РЕАЛЬНО извлекает прибыль
     (CycleExecuted, status=1) -- иначе показываем первое расхождение.

Ничего не расширяет: контракт/торговая логика бота не меняются, новый
скан/форк ЧЕГО-ТО ЕЩЁ не запускается -- тот же единственный кандидат."""
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
PORT = 8562
RPC = f"http://127.0.0.1:{PORT}"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
NATIVE = "0x0000000000000000000000000000000000000000"
SWAP_SELECTOR = "0xf3cd914c"

RESULT_FILE = REPO_ROOT / "data" / "task5_v4_item3_in_window_control_trade_result.json"

TARGET_TX = "0x878fb998474230338a47a710d2c25689987a836074729ae61e112dcccca7cd1b"
TARGET_BLOCK = 61636697
COMPETITOR_LEG0_ETH_INPUT = 3799912185593856  # из реального Swap-события плеча 0 (round10, знак исправлен)
KNOWN_PROFIT_COLLECTOR = "0x11854ce19dcd63a7eccaa12ded5aa991c94c79c6"
HOOK_ADDR = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def _leg_io(leg: dict) -> tuple[bool, str, str]:
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


def _decode_swap_calls_from_trace(node: dict, out: list[dict], path: str = "root") -> None:
    if node.get("to", "").lower() == POOL_MANAGER.lower() and (node.get("input") or "").lower().startswith(
            SWAP_SELECTOR):
        out.append({"path": path, "input": node["input"]})
    for i, child in enumerate(node.get("calls", []) or []):
        _decode_swap_calls_from_trace(child, out, f"{path}.{i}")


def _decode_swap_params(swap_calldata: str) -> dict:
    """swap(PoolKey key, SwapParams params, bytes hookData) -- 9 голов
    (5 PoolKey + 3 SwapParams + 1 offset hookData), затем сама hookData."""
    h = swap_calldata[2:] if swap_calldata.startswith("0x") else swap_calldata
    body = h[8:]
    words = [body[i:i + 64] for i in range(0, len(body), 64)]

    def addr(w):
        return "0x" + w[-40:]

    def to_signed(w):
        v = int(w, 16)
        return v - (1 << 256) if v >= (1 << 255) else v

    currency0, currency1 = addr(words[0]), addr(words[1])
    fee, tick_spacing, hooks = int(words[2], 16), to_signed(words[3]), addr(words[4])
    zero_for_one = int(words[5], 16) != 0
    amount_specified = to_signed(words[6])
    sqrt_price_limit = int(words[7], 16)
    hookdata_offset_words = int(words[8], 16) // 32
    hookdata_len = int(words[hookdata_offset_words], 16)
    hookdata_hex = body[(hookdata_offset_words + 1) * 64: (hookdata_offset_words + 1) * 64 + hookdata_len * 2]
    return {
        "currency0": currency0, "currency1": currency1, "fee": fee, "tick_spacing": tick_spacing, "hooks": hooks,
        "zero_for_one": zero_for_one, "amount_specified": amount_specified,
        "amount_specified_type": "exact_input (negative)" if amount_specified < 0 else "exact_output (positive)",
        "sqrt_price_limit": sqrt_price_limit, "hook_data_hex": "0x" + hookdata_hex, "hook_data_len": hookdata_len,
    }


def main() -> None:
    result: dict = {"target_tx_hash": TARGET_TX, "target_block": TARGET_BLOCK,
                     "competitor_leg0_eth_input_from_swap_event": COMPETITOR_LEG0_ETH_INPUT}

    # --- П.1: тип операции -- пробуем РЕАЛЬНУЮ (не форкнутую) историческую
    # tx через debug_traceTransaction на нашем обычном RPC. Один точечный
    # вызов по уже известному хешу -- НЕ скан. ---
    real_tx = _rpc_call("eth_getTransactionByHash", [TARGET_TX])
    real_receipt = _rpc_call("eth_getTransactionReceipt", [TARGET_TX])
    result["real_tx_input_hex_head"] = (real_tx.get("input") or "")[:10]
    result["real_receipt_gas_used"] = int(real_receipt["gasUsed"], 16) if real_receipt else None
    eff_gas_price = real_receipt.get("effectiveGasPrice") if real_receipt else None
    result["real_receipt_effective_gas_price"] = int(eff_gas_price, 16) if eff_gas_price else None
    if result["real_receipt_gas_used"] is not None and result["real_receipt_effective_gas_price"] is not None:
        result["real_gas_cost_wei"] = result["real_receipt_gas_used"] * result["real_receipt_effective_gas_price"]

    real_trace_error = None
    swap_params_by_leg = []
    try:
        trace_proc = run([CAST, "rpc", "debug_traceTransaction", TARGET_TX,
                           json.dumps({"tracer": "callTracer"}), "--rpc-url", os.environ.get("RPC_URL_PROVIDER", "")
                           or _alchemy_direct_endpoint()], timeout=30)
        if trace_proc.returncode == 0:
            trace = json.loads(trace_proc.stdout.strip())
            swap_calls: list[dict] = []
            _decode_swap_calls_from_trace(trace, swap_calls)
            for sc in swap_calls:
                swap_params_by_leg.append({"path": sc["path"], **_decode_swap_params(sc["input"])})
        else:
            real_trace_error = trace_proc.stderr.strip()
    except Exception as exc:  # noqa: BLE001
        real_trace_error = str(exc)
    result["real_tx_debug_trace_error"] = real_trace_error
    result["real_tx_swap_params_from_trace"] = swap_params_by_leg or None
    if swap_params_by_leg:
        result["leg0_operation_type_confirmed_from_calldata"] = swap_params_by_leg[0]["amount_specified_type"]
    else:
        result["leg0_operation_type_note"] = (
            "debug_traceTransaction РЕАЛЬНОЙ (не форкнутой) исторической tx недоступен на этом RPC "
            f"({real_trace_error}) -- тип операции НЕ подтверждён из калдаты; используем размер ИЗ реального "
            "Swap-события (единственный источник истины о РЕАЛИЗОВАННОМ входе) как exact-input для нашего "
            "контракта -- это ЕДИНСТВЕННЫЙ режим, который ClosedCycleExecutorV4 вообще поддерживает для "
            "первого плеча (firstAmountSpecified, всегда отрицательное = точный вход)."
        )

    # --- Реконструкция маршрута (та же методология round10) ---
    d = json.loads(RESULT_FILE.read_text())
    ffc = d.get("fund_flow_checks") or []
    row = next(r for r in ffc if r["tx_hash"].lower() == TARGET_TX.lower())
    legs_saved = row["legs"]

    route_legs = []
    for leg in legs_saved:
        pid = leg["pool_id"]
        init = fetch_initialize_event(pid, TARGET_BLOCK)
        if init is None:
            result["ok"] = False
            result["error"] = f"Initialize не найден для {pid}"
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return
        zero_for_one, _, _ = _leg_io(leg)
        route_legs.append(RouteLeg(init["currency0"], init["currency1"], init["fee"], init["tick_spacing"],
                                    init["hooks"], zero_for_one))
        if route_legs[-1].pool_id_hex.lower() != pid.lower():
            result["ok"] = False
            result["error"] = f"pool_id не сошёлся для {pid}"
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return
    exit_token = route_legs[0].input_currency
    route = RouteCycle("route_round11_878fb998", tuple(route_legs), exit_token, "round11-competitor-size", "discovered")
    result["route_legs_order_directions"] = [
        {"pool_id": l.pool_id_hex, "currency0": l.currency0, "currency1": l.currency1, "fee": l.fee,
         "tick_spacing": l.tick_spacing, "hooks": l.hooks, "zero_for_one": l.zero_for_one,
         "our_hook_data_used": "0x (ПУСТО -- build_execute_cycle_calldata() жёстко кодирует b'' для КАЖДОГО "
                                 "плеча, вне зависимости от реального hookData конкурента)" if l.hooks != NATIVE
                                 else None}
        for l in route.legs
    ]
    if swap_params_by_leg:
        result["real_hook_data_per_swap_call_from_trace"] = [
            {"path": sp["path"], "hooks": sp["hooks"], "hook_data_hex": sp["hook_data_hex"],
             "hook_data_len": sp["hook_data_len"]}
            for sp in swap_params_by_leg
        ]

    anvil_proc = None
    orig_config = af.CONFIG
    orig_checked = af._alchemy_direct_checked
    orig_url = af._alchemy_direct_url
    try:
        anvil_proc, deployer_addr, deployer_key = anvil_start(TARGET_BLOCK - 1)
        result["fork_block"] = TARGET_BLOCK - 1

        block_full = _rpc_call("eth_getBlockByNumber", [hex(TARGET_BLOCK), True])
        txs = block_full["transactions"]
        tx_index = next(i for i, t in enumerate(txs) if t["hash"].lower() == TARGET_TX.lower())
        target_tx = txs[tx_index]

        GENEROUS_GAS = hex(5_000_000)
        replayed = []
        for i in range(tx_index):
            t = txs[i]
            r = impersonate_and_send(t["from"], t.get("to"), t.get("input", "0x"), t.get("value", "0x0"),
                                      GENEROUS_GAS)
            replayed.append(r)
        failed = [r for r in replayed if r["returncode"] != 0]
        result["all_preceding_replayed_ok"] = len(failed) == 0
        if failed:
            result["ok"] = False
            result["error"] = "предшествующие tx не реплеились"
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return

        shared_snapshot = anvil_snapshot()
        result["shared_snapshot_id"] = shared_snapshot

        af._alchemy_direct_checked = True
        af._alchemy_direct_url = None
        af.CONFIG = dataclasses.replace(af.CONFIG, public_rpc_url=RPC, alchemy_rpc_url="", alchemy_api_key="")
        local_block = int(run([CAST, "block-number", "--rpc-url", RPC], timeout=10).stdout.strip())
        result["local_block_immediately_after_preceding_txs"] = local_block

        def run_our_bot_at_size(amount_in: int, label: str) -> dict:
            out: dict = {}
            quote = hp.quote_route_at_size(route, amount_in, local_block)
            out["quote"] = quote

            bytecode = (REPO_ROOT / "contracts" / "build" / "ClosedCycleExecutorV4.bytecode.txt").read_text().strip()
            bytecode_hex = bytecode[2:] if bytecode.startswith("0x") else bytecode

            def word_addr(a: str) -> str:
                return a[2:].rjust(64, "0")

            creation_calldata = "0x" + bytecode_hex + word_addr(deployer_addr) + word_addr(POOL_MANAGER)
            deploy_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", RPC,
                                "--create", creation_calldata, "--json"], timeout=60)
            out["deploy_returncode"] = deploy_proc.returncode
            if deploy_proc.returncode != 0:
                return out
            contract_addr = json.loads(deploy_proc.stdout).get("contractAddress")
            out["contract_addr_on_fork"] = contract_addr

            run([CAST, "rpc", "anvil_impersonateAccount", POOL_MANAGER, "--rpc-url", RPC], timeout=15)
            run([CAST, "rpc", "anvil_setBalance", POOL_MANAGER, hex(10 ** 20), "--rpc-url", RPC], timeout=15)
            start_token = route.legs[0].input_currency
            if start_token.lower() != NATIVE:
                fund_proc = run([CAST, "send", "--unlocked", "--from", POOL_MANAGER, "--rpc-url", RPC,
                                  start_token, "transfer(address,uint256)", contract_addr, str(amount_in),
                                  "--json"], timeout=30)
                out["fund_contract_returncode"] = fund_proc.returncode
            else:
                run([CAST, "rpc", "anvil_setBalance", contract_addr, hex(amount_in + 10 ** 17), "--rpc-url", RPC],
                    timeout=15)

            calldata = build_execute_cycle_calldata(route, -amount_in, min_profit=1)
            gas_res = hp.estimate_gas(contract_addr, calldata, deployer_addr)
            out["gas_estimate"] = gas_res
            send_gas_limit = max(gas_res.get("gas_estimate", 300000) * 2, 300000)
            send_res = impersonate_and_send(deployer_addr, contract_addr, "0x" + calldata.hex(), "0x0",
                                              hex(send_gas_limit))
            out["execute_send"] = send_res
            if send_res["returncode"] == 0 and send_res["tx_hash"]:
                receipt = _rpc_call("eth_getTransactionReceipt", [send_res["tx_hash"]])
                out["execute_receipt_status"] = int(receipt["status"], 16) if receipt else None
                if receipt and int(receipt["status"], 16) == 1:
                    # ищем CycleExecuted для честного факта прибыли (не только status=1)
                    for log in receipt.get("logs", []):
                        if log.get("address", "").lower() == contract_addr.lower():
                            out["cycle_executed_log_data"] = log.get("data")
            return out

        # --- (i) наш бот на РАЗМЕРЕ КОНКУРЕНТА ---
        our_at_competitor_size = run_our_bot_at_size(COMPETITOR_LEG0_ETH_INPUT, "competitor_size")
        result["our_bot_at_competitor_size"] = our_at_competitor_size
        revert_ok_1 = anvil_revert(shared_snapshot)
        result["revert_after_competitor_size_ok"] = revert_ok_1
        # ПРАВКА: anvil ИНВАЛИДИРУЕТ id снимка после ОДНОГО использования
        # (evm_revert потребляет snapshot) -- пересоздаём снимок СРАЗУ
        # после revert, на ТОМ ЖЕ восстановленном состоянии, чтобы
        # следующий revert указывал на ДЕЙСТВИТЕЛЬНЫЙ id (сама точка
        # состояния при этом РОВНО та же -- сразу после предшествующих tx,
        # см. общий докстринг).
        shared_snapshot = anvil_snapshot()
        result["shared_snapshot_id_2"] = shared_snapshot

        # --- (ii) наш бот на 0.02 ETH (для прямого сравнения В ЭТОМ ЖЕ прогоне) ---
        our_at_0_02_eth = run_our_bot_at_size(20_000_000_000_000_000, "0.02_eth")
        result["our_bot_at_0_02_eth"] = our_at_0_02_eth
        revert_ok_2 = anvil_revert(shared_snapshot)
        result["revert_after_0_02_eth_ok"] = revert_ok_2
        shared_snapshot = anvil_snapshot()
        result["shared_snapshot_id_3"] = shared_snapshot

        if not (revert_ok_1 and revert_ok_2):
            result["ok"] = False
            result["error"] = "evm_revert к общему снимку не подтверждён между сценариями -- останавливаемся"
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return

        # --- (iii) реплей РЕАЛЬНОЙ tx конкурента -- на том же снимке ---
        target_replay = impersonate_and_send(target_tx["from"], target_tx.get("to"), target_tx.get("input", "0x"),
                                              target_tx.get("value", "0x0"), target_tx.get("gas", "0x2dc6c0"))
        result["competitor_target_tx_replay"] = target_replay
        if target_replay["returncode"] == 0 and target_replay["tx_hash"]:
            comp_receipt = None
            for _ in range(10):
                comp_receipt = _rpc_call("eth_getTransactionReceipt", [target_replay["tx_hash"]])
                if comp_receipt is not None:
                    break
                time.sleep(1)
            result["competitor_replay_receipt_status"] = int(comp_receipt["status"], 16) if comp_receipt else None

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

    # --- П.4: прибыль конкурента по получателю + хук + газ ---
    recipient_share = 178895272651931
    hook_share = 118831530678813
    result["competitor_profit_breakdown"] = {
        "recipient_0x11854ce1_share_wei_before_gas": recipient_share,
        "hook_share_wei": hook_share,
        "sum_check": recipient_share + hook_share,
        "real_gas_used": result.get("real_receipt_gas_used"),
        "real_effective_gas_price_wei": result.get("real_receipt_effective_gas_price"),
        "real_gas_cost_wei": result.get("real_gas_cost_wei"),
        "recipient_share_minus_real_gas_cost": (
            recipient_share - result["real_gas_cost_wei"] if result.get("real_gas_cost_wei") is not None else None
        ),
        "note": ("газ реальной tx платил tx.from (relayer), НЕ получатель 0x11854ce1... -- 'итог после газа' "
                  "здесь означает совокупную экономику операции (доля получателя минус реальная стоимость газа "
                  "всей транзакции), а не изменение баланса самого получателя"),
    }

    # --- П.5: подтверждение причины "сетка размеров" ---
    comp_size_exec = (result.get("our_bot_at_competitor_size") or {})
    comp_size_profit_ok = comp_size_exec.get("execute_receipt_status") == 1
    if comp_size_profit_ok:
        result["grid_size_hypothesis_confirmed"] = True
        result["grid_size_hypothesis_conclusion"] = (
            "ПОДТВЕРЖДЕНО: на размере конкурента наш контракт РЕАЛЬНО извлекает прибыль (status=1) -- прежнее "
            "расхождение (0.02 ETH убыточен) объясняется ИМЕННО выбором размера сеткой, не иной причиной."
        )
    else:
        result["grid_size_hypothesis_confirmed"] = False
        first_divergence = None
        our_quote_at_comp_size = comp_size_exec.get("quote") or {}
        if not our_quote_at_comp_size.get("ok") or our_quote_at_comp_size.get("profit_raw", -1) <= 0:
            first_divergence = (
                f"наша КОТИРОВКА на размере конкурента уже не находит прибыли: {our_quote_at_comp_size} -- "
                "расхождение возникает ДО исполнения, на уровне котировки (возможные причины: наш контракт "
                "всегда шлёт ПУСТОЙ hookData, тогда как реальный hook мог получать непустой hookData от "
                "конкурента -- см. real_hook_data_per_swap_call_from_trace/route_legs_order_directions)"
            )
        elif comp_size_exec.get("execute_receipt_status") != 1:
            first_divergence = (
                f"наша котировка на размере конкурента показывает прибыль ({our_quote_at_comp_size}), но РЕАЛЬНОЕ "
                f"исполнение всё равно не проходит (execute_receipt_status="
                f"{comp_size_exec.get('execute_receipt_status')}, gas_estimate="
                f"{comp_size_exec.get('gas_estimate')}) -- расхождение МЕЖДУ котировкой и исполнением, не в "
                "размере как таковом"
            )
        result["first_divergence"] = first_divergence

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = REPO_ROOT / "data" / "task5_v4_item3_round11_competitor_size_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[round11] сохранено: {out_path}")


if __name__ == "__main__":
    main()
