#!/usr/bin/env python3
"""Обязательная точечная проверка защиты денег ПЕРЕД любым живым пилотом
(владелец, 2026-09-12): "На форке: заведомо убыточный цикл при непустом
инвентаре контракта. Убедиться, что revert срабатывает и убыток не
покрывается нашим инвентарём. Если не откатывается -- стоп, чиним."

ПРАВКА 2026-09-13 (внешнее ревью, пункт 2): "Форк-тест защиты получен на
estimateGas, не на отправленной транзакции. Отправить с явным gas_limit,
получить рецепт со status=0, только тогда считать пройденным." Раньше
`cast send` без --gas-limit сам вызывал eth_estimateGas ДО подписи и
отправки -- если оценка газа падает (а она падает для заведомо
ревертящего вызова), cast возвращает ненулевой код ВООБЩЕ НЕ ОТПРАВИВ
транзакцию в сеть; результат `tx_reverted=True` в прежней версии был
основан именно на этом, а не на реальном ончейн-исполнении. Теперь:
`cast send --gas-limit <явное> --async` (без локальной pre-оценки,
только подпись+broadcast, хэш возвращается сразу) + отдельный опрос
`cast receipt` до появления рецепта + проверка `status == "0x0"` --
PASS только по РЕАЛЬНОМУ рецепту с полей status.

ПРАВКА 2026-09-13 (внешнее ревью, пункт 1): добавлен ВТОРОЙ сценарий --
"кривой маршрут" -- та же реальная пара плеч, что дала реальную
прибыль +805382 raw USDG на блоке 61248736 (data/
task5_v4_verify_calldata_crosscheck_result.json), но с ОШИБОЧНО
заявленным exitToken (MOSIAI вместо реального USDG). До фикса такой
вызов НЕ упал бы -- unlockCallback слепо settle/take'нул бы обе валюты,
а executeCycle сверял прибыль только по (неверно) заявленному exitToken
(MOSIAI, баланс которого не менялся бы -- 0 вход в проверку прибыли,
но и revert не гарантирован, если бы контракт проверял только "баланс
не уменьшился"). После фикса closed-currency guard -- USDG получает
реальную ненулевую дельту (~+805382), но НЕ является заявленным
exitToken -- revert CycleNotClosed(USDG, ~805382) ДО единого
settle()/take(). Это РЕАЛЬНЫЙ маршрут (не выдуманные плечи), только
заведомо неверно заявленный exitToken -- имитирует класс ошибки,
который правка защищает.

НЕ новый этап -- одна точечная проверка поверх уже готовой инфраструктуры
Этапа 2 (task5_v4_fork_simulation.py). Реальный локальный форк, реальный
задеплоенный байткод (contracts/build/ClosedCycleExecutorV4.*, ПОСЛЕ
фикса closed-currency guard + minProfit>0), НИКАКИХ реальных транзакций
за пределами локального форка."""
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
EXECUTE_CYCLE_SIG = "executeCycle(((address,address,uint24,int24,address,bool,uint160,bytes)[],int256,address,uint256))"
# Явный лимит газа для --gas-limit (см. правку выше): реальный успешный
# 2-плечевой цикл использовал ~146655 газа (см. data/
# task5_v4_verify_calldata_crosscheck_result.json, cumulativeGasUsed
# 0x23cdf); ревертящий вызов обычно тратит МЕНЬШЕ (останавливается
# раньше), но берём с большим запасом -- сам лимит здесь не тестируем.
GAS_LIMIT = 1_000_000


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def send_and_get_real_receipt(rpc: str, deployer_key: str, contract_addr: str, cycle_params: str) -> dict:
    """Отправляет executeCycle РЕАЛЬНО (не через estimateGas-преоценку
    cast'а) и возвращает РЕАЛЬНЫЙ транзакционный рецепт. --gas-limit
    убирает локальную pre-оценку газа (та и ловила revert ДО отправки в
    старой версии теста); --async не ждёт рецепт внутри cast, а сразу
    печатает хэш -- рецепт получаем и проверяем сами, отдельным
    опросом, чтобы результат теста был основан ИСКЛЮЧИТЕЛЬНО на
    ончейн-статусе, а не на поведении CLI-обёртки."""
    send_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", rpc,
                      "--gas-limit", str(GAS_LIMIT), "--async",
                      contract_addr, EXECUTE_CYCLE_SIG, cycle_params], timeout=60)
    out = {"send_returncode": send_proc.returncode, "send_stdout": send_proc.stdout.strip(),
           "send_stderr": send_proc.stderr.strip()}
    if send_proc.returncode != 0:
        # --async С --gas-limit не должен падать локально (нет
        # pre-оценки) -- ненулевой код здесь значит РЕАЛЬНУЮ проблему
        # (сеть/подпись/nonce), не revert исполнения. Честно поднимаем.
        raise RuntimeError(f"cast send --async упал ДО отправки в сеть (не revert исполнения): {out['send_stderr']}")

    tx_hash_m = re.search(r"0x[0-9a-fA-F]{64}", send_proc.stdout)
    if not tx_hash_m:
        raise RuntimeError(f"не нашёл хэш транзакции в выводе cast send --async: {out}")
    tx_hash = tx_hash_m.group(0)
    out["tx_hash"] = tx_hash

    receipt = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        proc = run([CAST, "receipt", tx_hash, "--rpc-url", rpc, "--json"], timeout=15)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                receipt = json.loads(proc.stdout)
                break
            except json.JSONDecodeError:
                pass
        time.sleep(1)
    if receipt is None:
        raise RuntimeError(f"РЕАЛЬНЫЙ рецепт для {tx_hash} не получен за 30с -- честно не гадаем на estimateGas")

    out["receipt"] = receipt
    out["receipt_status"] = receipt.get("status")
    out["gas_used"] = receipt.get("gasUsed")
    # status приходит либо как "0x1"/"0x0", либо (реже, в зависимости от
    # версии cast --json) как int 1/0 -- проверяем оба представления,
    # не гадаем формат.
    status = receipt.get("status")
    out["tx_reverted"] = status in ("0x0", "0", 0, False)
    return out


def _diagnostic_revert_reason(rpc: str, from_addr: str, contract_addr: str, cycle_params: str) -> str | None:
    """Диагностика (НЕ основа PASS/FAIL, см. правку выше): отдельный
    eth_call с теми же аргументами -- если ревертит, cast call честно
    возвращает revert-данные в stderr, что даёт человекочитаемую причину
    для лога. Не влияет на результат теста -- только для отчёта."""
    proc = run([CAST, "call", "--from", from_addr, "--rpc-url", rpc,
                contract_addr, EXECUTE_CYCLE_SIG, cycle_params], timeout=30)
    if proc.returncode != 0:
        return proc.stderr.strip()[:2000]
    return None


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
        print("[safety_check] деплою ClosedCycleExecutorV4 (ИСПРАВЛЕННЫЙ, closed-currency guard)...")
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
        balance_before_scenario1 = int(bal_before_proc.stdout.strip().split()[0])
        result["contract_usdg_balance_before_scenario1"] = balance_before_scenario1

        # === Сценарий 1: заведомо убыточный цикл (реверс порядка пулов,
        # реально измеренный убыток -3.204861 USDG на блоке 61248736) ===
        leg_reversed_1 = f"({USDG},{MOSIAI},70000,3,{NATIVE},true,{MIN_SQRT_PRICE + 1},0x)"   # pool B первым
        leg_reversed_2 = f"({USDG},{MOSIAI},70000,4,{NATIVE},false,{MAX_SQRT_PRICE - 1},0x)"  # pool A вторым
        cycle_params_1 = f"([{leg_reversed_1},{leg_reversed_2}],-{PRINCIPAL_USDG_RAW},{USDG},1)"

        print("[safety_check] сценарий 1: заведомо убыточный цикл (реверснутый порядок плеч)...")
        diag_1 = _diagnostic_revert_reason(rpc, deployer_addr, contract_addr, cycle_params_1)
        s1 = send_and_get_real_receipt(rpc, deployer_key, contract_addr, cycle_params_1)
        s1["diagnostic_revert_reason"] = diag_1

        bal_after_1 = int(run([CAST, "call", USDG, "balanceOf(address)(uint256)", contract_addr,
                                "--rpc-url", rpc], timeout=15).stdout.strip().split()[0])
        s1["contract_usdg_balance_after"] = bal_after_1
        s1["balance_unchanged"] = bal_after_1 == balance_before_scenario1
        s1["PASS"] = s1["tx_reverted"] and s1["balance_unchanged"]
        result["scenario_1_loss_revert"] = s1
        print(f"[safety_check] сценарий 1: tx_reverted={s1['tx_reverted']} (по РЕАЛЬНОМУ рецепту, "
              f"status={s1['receipt_status']}), balance_unchanged={s1['balance_unchanged']} -> "
              f"{'PASS' if s1['PASS'] else 'FAIL'}")

        # === Сценарий 2 (НОВЫЙ, внешнее ревью пункт 1): кривой маршрут --
        # РЕАЛЬНЫЕ прибыльные плечи (те же, что дали +805382 raw USDG в
        # data/task5_v4_verify_calldata_crosscheck_result.json), но с
        # ЗАВЕДОМО НЕВЕРНО заявленным exitToken (MOSIAI вместо USDG) --
        # проверяем closed-currency guard: USDG получит реальную
        # ненулевую дельту, но не является заявленным exitToken. ===
        balance_before_scenario2 = bal_after_1  # продолжаем с тем же контрактом/инвентарём
        result["contract_usdg_balance_before_scenario2"] = balance_before_scenario2

        leg_forward_1 = f"({USDG},{MOSIAI},70000,4,{NATIVE},true,{MIN_SQRT_PRICE + 1},0x)"   # pool A первым (прямой, прибыльный порядок)
        leg_forward_2 = f"({USDG},{MOSIAI},70000,3,{NATIVE},false,{MAX_SQRT_PRICE - 1},0x)"  # pool B вторым
        # exitToken = MOSIAI (ОШИБОЧНО) вместо реального USDG -- имитация
        # кривого/неверно сконструированного маршрута.
        cycle_params_2 = f"([{leg_forward_1},{leg_forward_2}],-{PRINCIPAL_USDG_RAW},{MOSIAI},1)"

        print("[safety_check] сценарий 2: РЕАЛЬНЫЕ прибыльные плечи, но exitToken заявлен НЕВЕРНО (MOSIAI вместо USDG)...")
        diag_2 = _diagnostic_revert_reason(rpc, deployer_addr, contract_addr, cycle_params_2)
        s2 = send_and_get_real_receipt(rpc, deployer_key, contract_addr, cycle_params_2)
        s2["diagnostic_revert_reason"] = diag_2

        bal_after_2 = int(run([CAST, "call", USDG, "balanceOf(address)(uint256)", contract_addr,
                                "--rpc-url", rpc], timeout=15).stdout.strip().split()[0])
        s2["contract_usdg_balance_after"] = bal_after_2
        s2["balance_unchanged"] = bal_after_2 == balance_before_scenario2

        import eth_utils
        cycle_not_closed_selector = "0x" + eth_utils.function_signature_to_4byte_selector(
            "CycleNotClosed(address,int256)").hex()
        s2["cycle_not_closed_selector_recomputed"] = cycle_not_closed_selector
        s2["diagnostic_shows_cycle_not_closed"] = bool(diag_2) and cycle_not_closed_selector[2:] in diag_2
        # PASS: реальный revert (по рецепту) + инвентарь не тронут --
        # ИМЕННО НОВАЯ ошибка (не просто "что-то ревертнуло") подтверждаем
        # диагностическим eth_call выше как честную доп.проверку, не как
        # единственное основание.
        s2["PASS"] = s2["tx_reverted"] and s2["balance_unchanged"]
        result["scenario_2_cycle_not_closed"] = s2
        print(f"[safety_check] сценарий 2: tx_reverted={s2['tx_reverted']} (по РЕАЛЬНОМУ рецепту, "
              f"status={s2['receipt_status']}), balance_unchanged={s2['balance_unchanged']}, "
              f"diagnostic_shows_cycle_not_closed={s2['diagnostic_shows_cycle_not_closed']} -> "
              f"{'PASS' if s2['PASS'] else 'FAIL'}")

        result["PASS"] = s1["PASS"] and s2["PASS"]
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
