#!/usr/bin/env python3
"""Задача 5, живой пилот -- пункт 6 (внешнее ревью, третий раунд):
"Подготовь ОТДЕЛЬНЫЙ, ограниченно финансируемый кошелёк пилота -- не
используемый в других задачах адрес 0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75."

Владелец, ЯВНОЕ разрешение (дословно): "Создание локальной конфигурации
и необходимые изменения кода разрешаю. Перевод средств, деплой и первая
LIVE-отправка — после моего отдельного подтверждения конкретных
параметров." Этот скрипт ТОЛЬКО:
  1. Генерирует НОВЫЙ ключ ЛОКАЛЬНО на Ohio (eth_account.Account.create()
     -- криптографически стойкий, НЕ выведен ни из чего предсказуемого).
  2. Сохраняет приватный ключ ТОЛЬКО в файл с правами 600 на Ohio
     (KEY_FILE ниже) -- НИКОГДА не печатает его в stdout/лог, НИКОГДА не
     включает в результат, который коммитится в git.
  3. Печатает/сохраняет в результат ТОЛЬКО публичный адрес (не секрет).
  4. Делает РЕАЛЬНЫЕ read-only RPC-запросы (текущий курс WETH/USDG,
     текущий baseFee, РЕАЛЬНЫЙ eth_estimateGas деплоя V4 С ЭТИМ новым
     адресом в качестве владельца) -- честный расчёт финансирования, НЕ
     выдуманные числа (пользовательская настройка: "никогда не выдумывай
     данные, всегда ищи реальные источники").
  5. НЕ переводит средства, НЕ деплоит контракт, НЕ шлёт ни одной
     транзакции -- только генерация ключа + read-only расчёт.

Повторный запуск (ключ уже существует): НЕ перезаписывает существующий
файл ключа (во избежание случайной потери уже сгенерированного кошелька)
-- просто пересчитывает актуальное финансирование для уже
сгенерированного адреса."""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from eth_abi import encode  # noqa: E402
from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

from alchemy_fallback import _rpc_call  # noqa: E402

# Отдельный, НЕ коммитящийся файл -- ограниченные права (600), доступен
# только пользователю bot (тому же, под которым бот и запускается).
# ОТДЕЛЬНЫЙ от /etc/bot/env (там -- PRIVATE_KEY_NOX для ДРУГИХ задач,
# этот файл его НЕ трогает и НЕ перезаписывает).
KEY_FILE = Path("/home/bot/data/task5_v4_pilot_wallet.env")
RESULT_FILE = Path(__file__).parent.parent / "data" / "task5_v4_pilot_wallet_prepare_result.json"
BYTECODE_FILE = Path(__file__).parent.parent / "contracts/build/ClosedCycleExecutorV4.bytecode.txt"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
WETH_USDG_POOL_V3 = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
WETH_DECIMALS, USDG_DECIMALS = 18, 6
CHAIN_ID = 4663
PRIORITY_FEE_WEI = int(1e8)  # та же величина, что task5_bot_sender.PRIORITY_FEE_WEI

TRADING_BUDGET_USD = 20.0  # владелец: часовой пилот на $20
DEPLOY_GAS_LIMIT_MARGIN_FRACTION = 0.25  # та же практика, что scripts/deploy_executor_v4.sh (gas_est*1.25)
# Запас СВЕРХ ($20 торгового бюджета + расход деплоя) -- НЕ "покрытие
# газа отдельных сделок" (это отдельно уже учтено внутри бота через
# reserve_for_send/compute_min_profit_raw) -- буфер на дрожание курса
# ETH/USD и baseFee МЕЖДУ моментом этой оценки и моментом реального
# перевода (честно объявлено, не скрытая надбавка).
FUNDING_MARGIN_ABOVE_BUDGET_AND_DEPLOY_FRACTION = 0.25


def current_weth_usdg_price() -> float | None:
    """ТА ЖЕ реализация, что task5_v4_hotpath.py::current_weth_usdg_price
    / task5_true_arbitrageur_scan.py -- реальная ТЕКУЩАЯ цена (slot0()),
    не переиспользуем старую константу."""
    selector = "0x3850c7bd"  # slot0()
    try:
        result = _rpc_call("eth_call", [{"to": WETH_USDG_POOL_V3, "data": selector}, "latest"])
        sqrt_price_x96 = int(result[2:66], 16)
        raw_ratio = (sqrt_price_x96 / (2 ** 96)) ** 2
        return raw_ratio * (10 ** (WETH_DECIMALS - USDG_DECIMALS))
    except Exception:  # noqa: BLE001
        return None


def _read_existing_address() -> str:
    text = KEY_FILE.read_text()
    for line in text.splitlines():
        if line.startswith("PRIVATE_KEY_TASK5_V4_PILOT="):
            key = line.split("=", 1)[1].strip()
            return Account.from_key(key).address
    raise RuntimeError(f"{KEY_FILE} существует, но не содержит ожидаемой переменной "
                        f"PRIVATE_KEY_TASK5_V4_PILOT -- ручной разбор")


def main() -> None:
    if KEY_FILE.exists():
        address = _read_existing_address()
        print(f"[prepare-wallet] {KEY_FILE} уже существует -- НЕ перезаписываю (во избежание потери уже "
              f"сгенерированного ключа). Использую существующий адрес: {address}")
    else:
        acct = Account.create()
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(f"PRIVATE_KEY_TASK5_V4_PILOT={acct.key.hex()}\n")
        os.chmod(KEY_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 600 -- владелец файла (bot) читает/пишет, остальные -- ничего
        address = acct.address
        print(f"[prepare-wallet] СГЕНЕРИРОВАН новый ключ -> {KEY_FILE} (права 600). "
              f"Приватный ключ НЕ печатается и НЕ будет закоммичен. Публичный адрес: {address}")

    checksum_address = Web3.to_checksum_address(address)

    # ---------- РЕАЛЬНЫЕ read-only данные для расчёта финансирования ----------
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    weth_usdg_price = current_weth_usdg_price()

    block = _rpc_call("eth_getBlockByNumber", ["latest", False])
    base_fee_hex = block.get("baseFeePerGas")
    base_fee_wei = int(base_fee_hex, 16) if base_fee_hex else int(_rpc_call("eth_gasPrice", []), 16)
    max_fee_per_gas_wei = base_fee_wei * 2 + PRIORITY_FEE_WEI

    deploy_gas_estimate = None
    deploy_gas_estimate_error = None
    try:
        bytecode = BYTECODE_FILE.read_text().strip()
        ctor_args_hex = encode(["address", "address"], [checksum_address, Web3.to_checksum_address(POOL_MANAGER)]).hex()
        deploy_data = bytecode if bytecode.startswith("0x") else "0x" + bytecode
        deploy_data = deploy_data + ctor_args_hex
        deploy_gas_estimate = int(_rpc_call("eth_estimateGas", [{"from": checksum_address, "data": deploy_data}]), 16)
    except Exception as exc:  # noqa: BLE001
        deploy_gas_estimate_error = str(exc)

    deploy_gas_limit = int(deploy_gas_estimate * (1.0 + DEPLOY_GAS_LIMIT_MARGIN_FRACTION)) if deploy_gas_estimate else None
    deploy_cost_wei = deploy_gas_limit * max_fee_per_gas_wei if deploy_gas_limit else None
    deploy_cost_eth = deploy_cost_wei / 1e18 if deploy_cost_wei is not None else None
    deploy_cost_usd = (deploy_cost_eth * weth_usdg_price) if (deploy_cost_eth is not None and weth_usdg_price) else None

    trading_budget_eth = (TRADING_BUDGET_USD / weth_usdg_price) if weth_usdg_price else None

    result: dict = {
        "address": checksum_address,
        "note_key_storage": (f"приватный ключ сохранён ИСКЛЮЧИТЕЛЬНО в {KEY_FILE} на Ohio (права 600) -- "
                              f"НЕ включён в этот файл, НЕ печатается, НЕ коммитится"),
        "chain_id": CHAIN_ID,
        "estimated_at_utc": now_utc,
        "weth_usdg_price_used": weth_usdg_price,
        "weth_usdg_price_source": ("РЕАЛЬНЫЙ slot0() пула WETH/USDG V3 " + WETH_USDG_POOL_V3 +
                                    " на момент запуска этого скрипта; USDG допущено ==$1 (проектное "
                                    "допущение, см. task5_v4_pilot_accounting.py::_raw_to_usd)"),
        "base_fee_wei": base_fee_wei, "priority_fee_wei": PRIORITY_FEE_WEI, "max_fee_per_gas_wei": max_fee_per_gas_wei,
        "deploy_gas_estimate_real_eth_estimateGas": deploy_gas_estimate,
        "deploy_gas_estimate_error": deploy_gas_estimate_error,
        "deploy_gas_limit_with_margin": deploy_gas_limit,
        "deploy_gas_limit_margin_fraction": DEPLOY_GAS_LIMIT_MARGIN_FRACTION,
        "deploy_cost_eth_worst_case": deploy_cost_eth,
        "deploy_cost_usd_worst_case_at_estimate_time": deploy_cost_usd,
        "trading_budget_usd": TRADING_BUDGET_USD,
        "trading_budget_eth_at_estimate_time_price": trading_budget_eth,
        "funding_margin_above_budget_and_deploy_fraction": FUNDING_MARGIN_ABOVE_BUDGET_AND_DEPLOY_FRACTION,
    }

    if trading_budget_eth is not None and deploy_cost_eth is not None:
        total_needed_eth = trading_budget_eth + deploy_cost_eth
        recommended_funding_eth = total_needed_eth * (1.0 + FUNDING_MARGIN_ABOVE_BUDGET_AND_DEPLOY_FRACTION)
        result["total_needed_eth_no_margin"] = total_needed_eth
        result["recommended_funding_eth"] = recommended_funding_eth
        result["recommended_funding_usd_equivalent_at_estimate_time"] = (
            recommended_funding_eth * weth_usdg_price if weth_usdg_price else None)
    else:
        result["recommended_funding_eth"] = None
        result["recommended_funding_note"] = ("не удалось рассчитать (курс WETH/USDG или оценка газа деплоя "
                                               "недоступны -- см. deploy_gas_estimate_error/weth_usdg_price_used "
                                               "выше) -- честно НЕ гадаем итоговую сумму")

    result["explicit_caveats"] = [
        "Это НЕ перевод средств -- только расчёт. Перевод/деплой/первая LIVE-отправка -- ТОЛЬКО после "
        "отдельного подтверждения владельцем конкретных параметров.",
        "НЕ переводится весь предыдущий баланс основного кошелька -- это НОВЫЙ, отдельный, пустой (0 ETH) "
        "адрес.",
        "deploy_gas_estimate получен РЕАЛЬНЫМ eth_estimateGas (read-only, без подписи и отправки) С ЭТИМ "
        "новым адресом в качестве constructor-аргумента _owner -- не выдуман и не взят с чужого деплоя "
        "другого размера контракта.",
        "weth_usdg_price/base_fee -- реальные значения НА МОМЕНТ ЗАПУСКА этого скрипта (см. "
        "estimated_at_utc) -- курс/газ могут измениться к моменту фактического перевода, перепроверить "
        "перед реальным переводом.",
        "USDG == $1 -- явное, ранее объявленное допущение всего проекта, не реальный рыночный курс USDG "
        "к доллару.",
    ]

    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
