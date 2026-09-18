#!/usr/bin/env python3
"""Задача 5 -- фондированный микро-тест доставки. Владелец дал РАЗОВОЕ
разрешение использовать `PRIVATE_KEY_NOX`/`0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75`
ТОЛЬКО для этой проверки (см. docs/PROJECT_STATE.md, "РАЗОВОЕ разрешение...").

Цель: предыдущий замер (`task5_delivery_latency_n50.py`, 237мс тёплая
медиана) отправлял транзакцию с НУЛЕВЫМ балансом -- измерил время ответа
ОБ ОШИБКЕ ("insufficient funds"), не время реального приёма/включения.
Владелец справедливо снял вывод "145мс обработки -- неустранимы" по этой
причине. Здесь -- РЕАЛЬНЫЕ, исполняемые транзакции (себе же, 1 wei/0,
21000 газа), серия A (обычный газ) и серия B (заметно повышенный газ) --
чтобы (1) увидеть настоящее время до включения в блок, не до ответа об
ошибке, (2) проверить, влияет ли приоритетная плата на позицию внутри
блока (гипотеза: Arbitrum-секвенсер берёт по порядку поступления,
приоритетная плата не покупает позицию).

ЖЁСТКИЕ РАМКИ (буквально по заданию владельца):
- Получатель = отправитель = 0x893f4a7e... (перевод себе), НИКАКИХ
  контрактов.
- value <= 1 wei, gas == 21000, data == "0x" -- проверяется ПЕРЕД КАЖДОЙ
  подписью, несовпадение -- немедленный abort всего скрипта.
- nonce -- актуальный на момент отправки (eth_getTransactionCount,
  "pending"), не хардкожен.
- Пауза 2-3с между отправками.
- Ключ (`PRIVATE_KEY_NOX`) нигде не печатается и не попадает в вывод.
- Всего 8 отправок (5 серия A + 3 серия B), не больше -- жёсткий предел
  в коде, не только по инструкции.
- Если серия A завершилась НЕ полностью успешно (любая ошибка на любой
  из 5 отправок, или receipt не найден в разумное время) -- серия B НЕ
  запускается.

БЮДЖЕТ: баланс на момент проверки владельца -- ~$0.056 (0.0000226
нативного токена) -- ЭТОГО МОЖЕТ НЕ ХВАТИТЬ на буквальные "×10 и ×100"
множители из примера владельца (честный арифметический факт, посчитан
и напечатан явно ДО отправки, см. `compute_safe_multipliers()`) --
скрипт САМ вычисляет максимальный БЕЗОПАСНЫЙ (с запасом) множитель для
серии B по РЕАЛЬНОМУ балансу и РЕАЛЬНОЙ текущей `gasPrice` на момент
запуска, и печатает/сохраняет эти цифры как часть результата -- реально
использованные значения, не подогнанные под пример."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests
from eth_account import Account
from eth_utils import to_checksum_address

from task5_bot_config import CHAIN_ID_MAINNET, SEQUENCER_SUBMIT_URL_MAINNET
from alchemy_fallback import _rpc_call

WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
GAS_LIMIT = 21000
N_SERIES_A = 5
N_SERIES_B = 3
MAX_TOTAL_SENDS = N_SERIES_A + N_SERIES_B  # 8, жёсткий потолок
PAUSE_BETWEEN_SENDS_S = 2.5
SAFETY_BUDGET_FRACTION = 0.85  # используем не больше 85% текущего баланса на ВСЮ серию -- честный запас,
# но не переусердствуем: gasPrice на этой цепи был стабилен (60,106,000 wei) на протяжении
# нескольких проверок за последний час этой сессии -- риск скачка цены между расчётом и
# отправкой реально низкий, оставляем разумный запас, а не выжигаем всё до нуля.
RECEIPT_POLL_INTERVAL_S = 0.5
RECEIPT_POLL_TIMEOUT_S = 20.0


def rpc(method: str, params: list):
    return _rpc_call(method, params)


def get_account() -> Account:
    priv_hex = os.environ.get("PRIVATE_KEY_NOX", "")
    if not priv_hex:
        raise RuntimeError("PRIVATE_KEY_NOX не задан в окружении -- СТОП.")
    account = Account.from_key(priv_hex)
    address = to_checksum_address(account.address)
    if address != to_checksum_address(WALLET):
        raise RuntimeError(f"PRIVATE_KEY_NOX даёт {address}, ожидался {WALLET} -- СТОП.")
    return account


def compute_safe_multipliers(balance_wei: int, base_gas_price_wei: int) -> dict:
    """Реальный, честный расчёт (не выдуманный) максимально безопасного
    множителя для серии B, исходя из ФАКТИЧЕСКОГО баланса и ТЕКУЩЕЙ
    gasPrice на момент запуска -- НЕ подгоняется под иллюстративные
    "x10/x100" из примера владельца, если баланс этого не позволяет."""
    unit_cost_wei = GAS_LIMIT * base_gas_price_wei  # цена ОДНОЙ отправки при множителе 1
    budget_wei = int(balance_wei * SAFETY_BUDGET_FRACTION)
    budget_units = budget_wei / unit_cost_wei if unit_cost_wei > 0 else 0.0
    units_for_series_a = float(N_SERIES_A)  # серия A -- множитель 1 каждая
    units_remaining_for_b = budget_units - units_for_series_a

    if units_remaining_for_b <= 0.3:  # меньше 0.1 множителя на отправку -- нет смысла/небезопасно
        return {
            "affordable": False,
            "unit_cost_wei": unit_cost_wei,
            "budget_wei": budget_wei,
            "budget_units": budget_units,
            "units_remaining_for_series_b": units_remaining_for_b,
            "reason": "Баланса не хватает даже на минимальную повышенную серию B "
                      "с честным запасом -- см. budget_units/units_remaining_for_series_b.",
        }

    # Честный градиент внутри доступного бюджета -- 3 РАЗНЫХ множителя (не три одинаковых),
    # чтобы увидеть, реагирует ли позиция монотонно на рост газа, а не только бинарно.
    # Целимся в верхнюю границу, но с запасом (0.9 от максимума), чтобы не упереться
    # в реальный рост gasPrice между расчётом и фактической отправкой.
    max_uniform = units_remaining_for_b / 3.0
    target_max = max_uniform * 0.9
    multipliers_b = [round(target_max * f, 3) for f in (0.4, 0.7, 1.0)]
    multipliers_b = [max(m, 1.5) for m in multipliers_b]  # честный пол -- ниже 1.5x разница может потеряться в шуме

    total_units_used = float(N_SERIES_A) + sum(multipliers_b)
    total_cost_wei = int(total_units_used * unit_cost_wei)

    return {
        "affordable": True,
        "unit_cost_wei": unit_cost_wei,
        "budget_wei": budget_wei,
        "budget_units": budget_units,
        "units_remaining_for_series_b": units_remaining_for_b,
        "series_b_multipliers": multipliers_b,
        "total_units_used_estimate": total_units_used,
        "total_cost_wei_estimate": total_cost_wei,
        "balance_remaining_after_estimate_wei": balance_wei - total_cost_wei,
        "balance_remaining_after_estimate_native": (balance_wei - total_cost_wei) / 1e18,
        "note_on_owner_example": (
            "Владелец привёл x10/x100 как ИЛЛЮСТРАТИВНЫЙ пример -- реальный баланс "
            "не позволяет столько при честном запасе (см. budget_units выше). "
            "Использованы реально доступные множители, посчитанные здесь, а не x10/x100."
        ),
    }


def verify_and_build_tx(account: Account, gas_price_wei: int) -> dict:
    """ВСЕ проверки ПЕРЕД подписью -- то же самое требование владельца,
    буквально: to == from == WALLET, value <= 1 wei, gas == 21000,
    data == '0x'. AssertionError -- немедленный abort вызывающим кодом."""
    from_addr = to_checksum_address(account.address)
    to_addr = to_checksum_address(WALLET)
    assert from_addr == to_addr == to_checksum_address(WALLET), "to/from должны быть == WALLET"
    value = 0
    assert value <= 1, "value должно быть <= 1 wei"
    nonce = int(rpc("eth_getTransactionCount", [from_addr, "pending"]), 16)
    tx = {
        "to": to_addr, "value": value, "gas": GAS_LIMIT, "gasPrice": int(gas_price_wei),
        "nonce": nonce, "chainId": CHAIN_ID_MAINNET, "data": "0x",
    }
    assert tx["gas"] == GAS_LIMIT, "gas должен быть ровно 21000"
    assert tx["data"] == "0x", "data должно быть пустым"
    assert tx["to"] == to_checksum_address(WALLET), "получатель должен быть WALLET"
    return tx


def send_one(account: Account, gas_price_wei: int, label: str, multiplier: float) -> dict:
    tx = verify_and_build_tx(account, gas_price_wei)
    block_at_send = int(rpc("eth_blockNumber", []), 16)
    signed = Account.sign_transaction(tx, account.key)
    raw_hex = signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex

    t_send = time.time()
    entry: dict = {
        "label": label, "multiplier": multiplier, "gas_price_wei": gas_price_wei,
        "nonce": tx["nonce"], "block_at_send": block_at_send, "t_send_wall": t_send,
    }
    try:
        resp = requests.post(
            SEQUENCER_SUBMIT_URL_MAINNET,
            json={"jsonrpc": "2.0", "method": "eth_sendRawTransaction", "params": [raw_hex], "id": 1},
            timeout=15.0,
        )
        t_resp = time.time()
        body = resp.json()
        entry["response_time_ms"] = (t_resp - t_send) * 1000.0
        entry["response_body"] = body
        entry["tx_hash"] = body.get("result")
        if entry["tx_hash"] is None:
            entry["send_error"] = body.get("error")
    except Exception as exc:  # noqa: BLE001
        t_resp = time.time()
        entry["response_time_ms"] = (t_resp - t_send) * 1000.0
        entry["send_error"] = str(exc)
        entry["tx_hash"] = None
    return entry


def wait_for_receipt(tx_hash: str, timeout_s: float = RECEIPT_POLL_TIMEOUT_S) -> dict | None:
    t_end = time.monotonic() + timeout_s
    while time.monotonic() < t_end:
        try:
            result = rpc("eth_getTransactionReceipt", [tx_hash])
        except Exception:  # noqa: BLE001
            result = None
        if result:
            return result
        time.sleep(RECEIPT_POLL_INTERVAL_S)
    return None


def enrich_with_receipt(entry: dict) -> None:
    if not entry.get("tx_hash"):
        entry["receipt_found"] = False
        return
    receipt = wait_for_receipt(entry["tx_hash"])
    if not receipt:
        entry["receipt_found"] = False
        return
    block_number = int(receipt["blockNumber"], 16)
    tx_index = int(receipt["transactionIndex"], 16)
    block = rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
    n_txs_in_block = len(block.get("transactions", [])) if block else None
    entry.update({
        "receipt_found": True,
        "receipt_block_number": block_number,
        "blocks_elapsed_from_send": block_number - entry["block_at_send"],
        "transaction_index_in_block": tx_index,
        "n_txs_in_block": n_txs_in_block,
        "receipt_status": receipt.get("status"),
    })


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    account = get_account()
    address = to_checksum_address(account.address)
    print(f"[funded_test] адрес подтверждён: {address}", file=sys.stderr)

    balance_wei = int(rpc("eth_getBalance", [address, "latest"]), 16)
    base_gas_price_wei = int(rpc("eth_gasPrice", []), 16)
    print(f"[funded_test] РЕАЛЬНЫЙ баланс сейчас: {balance_wei} wei ({balance_wei / 1e18:.10f} нативного)",
          file=sys.stderr)
    print(f"[funded_test] РЕАЛЬНАЯ текущая gasPrice: {base_gas_price_wei} wei", file=sys.stderr)

    budget = compute_safe_multipliers(balance_wei, base_gas_price_wei)
    print(f"[funded_test] Расчёт бюджета: {json.dumps(budget, indent=2, ensure_ascii=False)}", file=sys.stderr)

    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wallet": address,
        "balance_before_wei": balance_wei,
        "balance_before_native": balance_wei / 1e18,
        "base_gas_price_wei": base_gas_price_wei,
        "budget_calculation": budget,
        "attempts": [],
    }

    if not budget.get("affordable"):
        result["aborted"] = True
        result["abort_reason"] = "Бюджета не хватает даже на минимальную серию B с честным запасом -- см. budget_calculation."
        _write(result)
        print(json.dumps({"aborted": True, "reason": result["abort_reason"]}, indent=2, ensure_ascii=False))
        return

    series_b_multipliers = budget["series_b_multipliers"]
    print(f"[funded_test] Реально используемые множители серии B: {series_b_multipliers} "
          f"(вместо иллюстративных x10/x100 из примера владельца -- баланс не позволяет)", file=sys.stderr)
    print(f"[funded_test] Ожидаемый остаток после всей серии: "
          f"{budget['balance_remaining_after_estimate_native']:.10f} нативного", file=sys.stderr)

    n_sent = 0
    series_a_ok = True

    # --- Серия A: 5 отправок, обычная цена газа (множитель 1) ---
    for i in range(N_SERIES_A):
        if n_sent >= MAX_TOTAL_SENDS:
            break
        label = f"A{i + 1}"
        print(f"[funded_test] отправка {label} (multiplier=1.0)...", file=sys.stderr)
        entry = send_one(account, base_gas_price_wei, label, 1.0)
        n_sent += 1
        if entry.get("tx_hash"):
            time.sleep(PAUSE_BETWEEN_SENDS_S)
            enrich_with_receipt(entry)
        else:
            series_a_ok = False
        result["attempts"].append(entry)
        print(f"[funded_test] {label}: tx_hash={entry.get('tx_hash')} "
              f"blocks_elapsed={entry.get('blocks_elapsed_from_send')} "
              f"tx_index={entry.get('transaction_index_in_block')}", file=sys.stderr)
        if not entry.get("receipt_found", True):
            series_a_ok = False
        if not series_a_ok:
            print("[funded_test] СЕРИЯ A: проблема на этой отправке -- останавливаюсь, "
                  "серия B НЕ будет запущена.", file=sys.stderr)
            break

    result["series_a_ok"] = series_a_ok

    # --- Серия B: 3 отправки, повышенная цена газа -- ТОЛЬКО если серия A полностью успешна ---
    if series_a_ok:
        for i, mult in enumerate(series_b_multipliers):
            if n_sent >= MAX_TOTAL_SENDS:
                break
            label = f"B{i + 1}"
            gas_price_b = int(base_gas_price_wei * mult)
            print(f"[funded_test] отправка {label} (multiplier={mult}, gasPrice={gas_price_b})...", file=sys.stderr)
            entry = send_one(account, gas_price_b, label, mult)
            n_sent += 1
            if entry.get("tx_hash"):
                time.sleep(PAUSE_BETWEEN_SENDS_S)
                enrich_with_receipt(entry)
            result["attempts"].append(entry)
            print(f"[funded_test] {label}: tx_hash={entry.get('tx_hash')} "
                  f"blocks_elapsed={entry.get('blocks_elapsed_from_send')} "
                  f"tx_index={entry.get('transaction_index_in_block')}", file=sys.stderr)
    else:
        result["series_b_skipped"] = True

    balance_after_wei = int(rpc("eth_getBalance", [address, "latest"]), 16)
    result["balance_after_wei"] = balance_after_wei
    result["balance_after_native"] = balance_after_wei / 1e18
    result["n_sends_total"] = n_sent

    out_path = Path(__file__).resolve().parent.parent / "data" / "task5_delivery_funded_test_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[funded_test] написано {out_path}", file=sys.stderr)

    summary = [
        {
            "label": a["label"], "multiplier": a.get("multiplier"), "gas_price_wei": a.get("gas_price_wei"),
            "response_time_ms": a.get("response_time_ms"), "blocks_elapsed": a.get("blocks_elapsed_from_send"),
            "tx_index": a.get("transaction_index_in_block"), "n_txs_in_block": a.get("n_txs_in_block"),
        }
        for a in result["attempts"]
    ]
    print(json.dumps({"summary": summary, "balance_before": result["balance_before_native"],
                       "balance_after": result["balance_after_native"]}, indent=2, ensure_ascii=False))


def _write(result: dict) -> None:
    out_path = Path(__file__).resolve().parent.parent / "data" / "task5_delivery_funded_test_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
