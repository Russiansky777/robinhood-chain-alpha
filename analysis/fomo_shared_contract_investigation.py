#!/usr/bin/env python3
"""Владелец (2026-09-17), Fomo, приоритет 1 -- разобрать общий непрозрачный
контракт 0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f, найденный (офлайн, по
уже собранной истории) минимум у 2/9 адресов -- проверка по ВСЕМ 9 (уже
сделана офлайн, см. data/fomo_shared_contract_wallet_overlap.json,
результат: 9 из 9). Здесь -- то, что требует сети:

  1. Полный байткод -- скан PUSH4 (0x63) на кандидатные 4-байтные
     селекторы + попытка расшифровать через публичную базу
     4byte.directory (реальный внешний сервис, не выдумываем структуру
     ответа -- если недоступен из GH Actions, честно фиксируем отказ).
  2. Деплой -- бисекция по eth_getCode(address, block) между 0 и latest,
     чтобы найти первый блок с байткодом -- 0 предположений, только
     бинарный поиск по РЕАЛЬНЫМ ответам RPC. Затем перебор транзакций
     найденного блока через eth_getTransactionReceipt в поисках
     receipt.contractAddress == TARGET -- даёт настоящего деплоера,
     хэш транзакции деплоя и (через сам блок) время.
  3. Сколько всего адресов взаимодействует -- alchemy_getAssetTransfers
     both directions на САМ контракт (тот же пагинированный метод, что
     analysis/fomo_9wallets_alchemy_full_history.py), честный потолок,
     подсчёт уникальных counterparty.
  4. Приоритет 2, уточнение: офлайн-подсчёт (уже сделан, ГИПОТЕЗА
     "одна нога = один tx" подтвердилась: 0 из 342 исходящих канонич.
     USDG/WETH платежей делят tx с входящим токеном) дал 0 -- но
     офлайн-выгрузка использовала ТОЛЬКО category=["erc20"], нативный
     газ-токен не проверялся вообще. Здесь -- реальная проверка гипотезы
     "платёж нативным токеном": eth_getTransactionByHash на выборке из
     tx, где кошелёк получил токен ОТ TARGET -- если tx.value > 0 И
     tx.from == кошелёк, это означает, что кошелёк САМ инициировал
     оплаченный нативным токеном вызов TARGET и получил токен в ответ
     в ТОЙ ЖЕ транзакции -- настоящая пара платёж/получение."""
from __future__ import annotations

import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

from config import CONFIG  # noqa: E402
from alchemy_fallback import _rpc_call as rpc_call, rpc_call_trading_path  # noqa: E402

OUT_PATH = Path("data/fomo_shared_contract_investigation_result.json")
IN_CSV = Path("data/fomo_9wallets_alchemy_raw_transfers.csv")

TARGET = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"
FAST = rpc_call_trading_path  # ~10 req/s, отдельный троттлинг-бюджет, см. alchemy_fallback.py

MAX_INTERACTOR_PAGES_PER_DIRECTION = 30  # тот же честный потолок, что в fomo_9wallets_alchemy_full_history.py
NATIVE_VALUE_SAMPLE_MAX = 400  # честный потолок на выборку eth_getTransactionByHash


def scan_push4_selectors(bytecode_hex: str) -> list[str]:
    """PUSH4-скан: opcode 0x63 всегда означает 'следующие 4 байта -- literal
    на стек' -- дисп etчер контрактов (Solidity/Vyper) кодирует сравнение с
    4-байтным селектором именно так. НЕ идеальный дизассемблер (не отличает
    0x63 внутри данных PUSH от опкода), но стандартная и общепринятая
    эвристика для быстрого извлечения кандидатов без полного disassembler."""
    data = bytes.fromhex(bytecode_hex[2:] if bytecode_hex.startswith("0x") else bytecode_hex)
    selectors: Counter[str] = Counter()
    i = 0
    while i < len(data):
        op = data[i]
        if op == 0x63 and i + 5 <= len(data):  # PUSH4
            sel = "0x" + data[i + 1:i + 5].hex()
            selectors[sel] += 1
            i += 5
        elif 0x60 <= op <= 0x7f:  # PUSH1..PUSH32 -- пропускаем immediate целиком
            n = op - 0x5f
            i += 1 + n
        else:
            i += 1
    # Селекторы дисп etчера обычно встречаются РОВНО 1 раз каждый (сравнение
    # с константой) -- сортируем по частоте встречи=1 сначала (самые
    # вероятные настоящие селекторы), затем по адресу появления не важно.
    return [sel for sel, _ in selectors.most_common()]


def lookup_4byte(selector: str) -> dict:
    """Публичная база сигнатур 4byte.directory -- реальный внешний сервис,
    НЕ гарантирован доступным из GH Actions egress -- честно фиксируем
    отказ, если недоступен, не выдумываем структуру ответа."""
    try:
        resp = requests.get(
            "https://www.4byte.directory/api/v1/signatures/",
            params={"hex_signature": selector}, timeout=10,
        )
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}"}
        body = resp.json()
        results = body.get("results", [])
        return {"n_candidates": len(results), "candidates": [r.get("text_signature") for r in results[:5]]}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def find_deployment_block(target: str) -> dict:
    """Бисекция eth_getCode(target, block) между 0 и latest -- находит
    ПЕРВЫЙ блок, где байткод уже присутствует. Реальные RPC-ответы, не
    предположение о дате запуска чейна."""
    latest_hex = FAST("eth_blockNumber", [])
    latest = int(latest_hex, 16)

    def has_code(block: int) -> bool:
        code = FAST("eth_getCode", [target, hex(block)])
        return code not in (None, "0x", "0x0")

    if not has_code(latest):
        return {"error": "байткод отсутствует даже на latest -- контракт не существует?"}
    lo, hi = 0, latest
    n_calls = 0
    while lo < hi:
        mid = (lo + hi) // 2
        n_calls += 1
        if has_code(mid):
            hi = mid
        else:
            lo = mid + 1
    return {"deployment_block": lo, "n_bisection_calls": n_calls, "latest_block_at_probe": latest}


def find_deploy_tx(deployment_block: int, target: str) -> dict:
    """Перебор транзакций найденного блока через receipt.contractAddress --
    даёт настоящего деплоера (receipt.from), хэш и время (через blockтайм)."""
    block = FAST("eth_getBlockByNumber", [hex(deployment_block), False])
    tx_hashes = block.get("transactions", []) if isinstance(block, dict) else []
    for txh in tx_hashes:
        try:
            receipt = FAST("eth_getTransactionReceipt", [txh])
        except Exception:  # noqa: BLE001
            continue
        if not receipt:
            continue
        contract_addr = (receipt.get("contractAddress") or "").lower()
        if contract_addr == target.lower():
            return {
                "found": True, "deploy_tx_hash": txh, "deployer": receipt.get("from"),
                "block_timestamp_hex": block.get("timestamp"),
                "block_timestamp_utc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(block.get("timestamp", "0x0"), 16))
                ) if block.get("timestamp") else None,
                "n_txs_in_block_scanned": len(tx_hashes),
            }
    return {"found": False, "n_txs_in_block_scanned": len(tx_hashes),
            "note": "ни одна receipt.contractAddress не совпала -- возможно контракт создан ВНУТРЕННЕЙ "
                    "транзакцией (CREATE изнутри другого контракта), не видно через receipt верхнего уровня"}


def _alchemy_url() -> str | None:
    if CONFIG.alchemy_rpc_url:
        return CONFIG.alchemy_rpc_url
    if CONFIG.alchemy_api_key:
        return f"https://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}"
    return None


_last_call_ts = 0.0


def _throttled_post(url: str, payload: dict) -> requests.Response:
    global _last_call_ts
    wait = _last_call_ts + 0.35 - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.monotonic()
    return requests.post(url, json=payload, timeout=30)


def count_interactors(target: str) -> dict:
    url = _alchemy_url()
    if not url:
        return {"error": "Alchemy не настроен"}
    counterparties: set[str] = set()
    per_direction: dict = {}
    for direction_key, direction_label in (("fromAddress", "out"), ("toAddress", "in")):
        page_key = None
        n_pages = 0
        n_transfers = 0
        capped = False
        while n_pages < MAX_INTERACTOR_PAGES_PER_DIRECTION:
            params: dict = {direction_key: target, "category": ["erc20"], "maxCount": "0x64", "order": "asc"}
            if page_key:
                params["pageKey"] = page_key
            try:
                resp = _throttled_post(url, {"jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers", "params": [params]})
                body = resp.json()
            except Exception as exc:  # noqa: BLE001
                per_direction[direction_label] = {"error": str(exc)[:200]}
                break
            if "error" in body:
                per_direction[direction_label] = {"error": str(body["error"])[:200]}
                break
            result = body.get("result", {})
            transfers = result.get("transfers", [])
            n_transfers += len(transfers)
            for t in transfers:
                other = t["to"] if direction_label == "out" else t["from"]
                if other:
                    counterparties.add(other.lower())
            n_pages += 1
            page_key = result.get("pageKey")
            if not page_key:
                break
        else:
            capped = True
        per_direction[direction_label] = {"n_transfers": n_transfers, "n_pages": n_pages, "capped": capped}
    return {"per_direction": per_direction, "n_unique_counterparties_total": len(counterparties),
            "counterparties_sample": list(counterparties)[:20]}


def check_native_value_payment_hypothesis() -> dict:
    """Приоритет 2, уточнение: берём выборку tx, где кошелёк получил токен
    ОТ TARGET (из уже собранной data/fomo_9wallets_alchemy_raw_transfers.csv,
    category=erc20), проверяем tx.value (нативный токен, приложенный К
    самой транзакции) реальным eth_getTransactionByHash -- если >0 И
    tx.from == кошелёк, это настоящая пара 'нативный платёж + получение
    токена' в ОДНОЙ транзакции, которую erc20-категория Alchemy не увидела
    бы как 'исходящий перевод' (нативный ETH -- не ERC-20 Transfer)."""
    if not IN_CSV.exists():
        return {"error": f"{IN_CSV} не найден"}
    with IN_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    candidates = [r for r in rows if r["direction"] == "in" and (r["from_addr"] or "").lower() == TARGET.lower()]
    # Равномерная выборка по всем кошелькам, не только по первому с наибольшим объёмом.
    by_wallet: dict[str, list] = {}
    for r in candidates:
        by_wallet.setdefault(r["wallet_name"], []).append(r)
    sample = []
    per_wallet_quota = max(1, NATIVE_VALUE_SAMPLE_MAX // max(len(by_wallet), 1))
    for wallet, lst in by_wallet.items():
        sample.extend(lst[:per_wallet_quota])
    sample = sample[:NATIVE_VALUE_SAMPLE_MAX]

    n_checked = 0
    n_native_value_found = 0
    n_tx_from_matches_wallet = 0
    examples = []
    errors = 0
    for r in sample:
        try:
            tx = FAST("eth_getTransactionByHash", [r["tx_hash"]])
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        if not tx:
            errors += 1
            continue
        n_checked += 1
        value_wei = int(tx.get("value", "0x0"), 16)
        tx_from = (tx.get("from") or "").lower()
        wallet_addr = r["wallet_address"].lower()
        if value_wei > 0:
            n_native_value_found += 1
        if tx_from == wallet_addr:
            n_tx_from_matches_wallet += 1
        if value_wei > 0 and tx_from == wallet_addr and len(examples) < 10:
            examples.append({
                "wallet": r["wallet_name"], "tx_hash": r["tx_hash"], "value_wei": value_wei,
                "value_native_human": value_wei / 1e18, "tx_from": tx_from, "tx_to": tx.get("to"),
                "token_received": r["asset_symbol"], "amount_received": r["value_human"],
            })
    return {
        "sample_size_requested": len(sample), "n_checked": n_checked, "n_rpc_errors": errors,
        "n_with_nonzero_native_value": n_native_value_found,
        "n_where_tx_from_equals_wallet": n_tx_from_matches_wallet,
        "n_true_native_payment_pairs": len(examples) if len(examples) < 10 else "10+ (обрезано примерами)",
        "examples": examples,
        "honest_note": (
            "Если n_with_nonzero_native_value мало/ноль -- гипотеза 'платёж нативным газ-токеном в той же "
            "транзакции' НЕ подтверждается на этой выборке (не значит невозможно в принципе, значит не видно "
            "здесь). Если tx_from систематически НЕ равен кошельку -- значит кошелёк НЕ САМ инициирует эти "
            "транзакции (согласуется с ERC-4337/EIP-7702: транзакцию мог инициировать бандлер/relayer, а не "
            "сам EOA), что окончательно исключает 'платёж внутри одной адресованной кошельком транзакции' как "
            "объяснение механизма для ЭТИХ конкретных событий."
        ),
    }


def main() -> int:
    out: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "target": TARGET}

    print(f"[shared_contract] Шаг 1: полный байткод + PUSH4-скан {TARGET}")
    code_hex = rpc_call("eth_getCode", [TARGET, "latest"])
    out["bytecode_len_bytes"] = (len(code_hex or "0x") - 2) // 2
    selectors = scan_push4_selectors(code_hex or "0x")
    out["n_push4_candidate_selectors"] = len(selectors)
    out["push4_candidate_selectors_sample"] = selectors[:40]
    print(f"[shared_contract] {out['bytecode_len_bytes']} байт, {len(selectors)} кандидатных PUSH4-селекторов")

    lookups = {}
    for sel in selectors[:40]:
        lookups[sel] = lookup_4byte(sel)
        time.sleep(0.3)  # вежливый троттлинг к внешнему бесплатному сервису
    out["selector_4byte_lookups"] = lookups
    n_matched = sum(1 for v in lookups.values() if v.get("n_candidates", 0) > 0)
    print(f"[shared_contract] 4byte.directory: {n_matched}/{len(lookups)} селекторов дали хотя бы 1 кандидата")

    print("[shared_contract] Шаг 2: бисекция блока деплоя")
    deploy_block_info = find_deployment_block(TARGET)
    out["deployment_block_search"] = deploy_block_info
    if "deployment_block" in deploy_block_info:
        print(f"[shared_contract] Блок деплоя: {deploy_block_info['deployment_block']} "
              f"({deploy_block_info['n_bisection_calls']} вызовов бисекции)")
        deploy_tx_info = find_deploy_tx(deploy_block_info["deployment_block"], TARGET)
        out["deploy_tx"] = deploy_tx_info
        print(f"[shared_contract] Деплой-tx: {deploy_tx_info}")

    print("[shared_contract] Шаг 3: подсчёт уникальных адресов-контрагентов (Alchemy, честный потолок)")
    out["interactor_count"] = count_interactors(TARGET)
    print(f"[shared_contract] {out['interactor_count'].get('n_unique_counterparties_total')} уникальных контрагентов "
          f"(в пределах честного потолка страниц)")

    print("[shared_contract] Шаг 4: проверка гипотезы нативного платежа в той же транзакции")
    out["native_value_payment_check"] = check_native_value_payment_hypothesis()
    print(f"[shared_contract] {out['native_value_payment_check']}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[shared_contract] Результат: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
