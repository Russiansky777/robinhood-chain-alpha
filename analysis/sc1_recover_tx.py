#!/usr/bin/env python3
"""Восстановление записи реестра для транзакции, которая РЕАЛЬНО ушла в
сеть, но чей `append_registry_and_commit` упал ДО коммита (найдено
2026-09-02, run 33612440836: git identity не была настроена внутри
джобы до этого шага, `git commit` вышел с exit 128 -- сама отправка
прошла успешно, но запись потерялась). Читает receipt по известному
tx_hash, декодирует TokenLaunched, дописывает `data/sc1_launches.json`
и коммитит -- той же логикой, что `sc1_launcher.py::_decode_receipt`/
`append_registry_and_commit`, не дублируется вслепую.

Использование: python analysis/sc1_recover_tx.py <symbol> <tx_hash> <image_file> <launch_fee_eth> <eth_usd_price_at_send>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pons_v2_common as v2  # noqa: E402
from sc1_launcher import _decode_receipt, _wait_for_receipt, append_registry_and_commit  # noqa: E402


def run() -> int:
    if len(sys.argv) != 6:
        print(__doc__)
        return 1
    symbol, tx_hash, image_file, launch_fee_eth_s, eth_usd_price_s = sys.argv[1:]
    launch_fee_eth = float(launch_fee_eth_s)
    eth_usd_price = float(eth_usd_price_s)

    print(f"[sc1_recover_tx] восстанавливаю запись для {symbol}, tx={tx_hash}")
    receipt = _wait_for_receipt(tx_hash, timeout_s=60, poll_s=5)  # уже должна быть замайнена -- короткий таймаут

    fake_report = {
        "eth_usd_price": eth_usd_price,
        "gas_price_wei": None,  # неизвестен точно здесь -- effectiveGasPrice из receipt используется напрямую, см. _decode_receipt fallback
    }
    result = _decode_receipt(receipt, fake_report, gas_price_wei_estimated=0)  # fallback только если effectiveGasPrice отсутствует в receipt -- не ожидается для замайненной tx

    print(f"[sc1_recover_tx] статус={result['status']} token={result['token_address']} curve={result['curve_address']}")

    import time
    entry = {
        "symbol": symbol,
        "token_address": result["token_address"],
        "pool_address": result["curve_address"],
        "pool_address_note": "V2 pre-graduation: адрес бондинг-кривой (curve), не AMM-пул -- реальный пул появляется только после градуации (createGraduatedPool)",
        "tx_hash": result["tx_hash"],
        "block_number": result["block_number"],
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),  # восстановлено постфактум -- не момент отправки, честно помечено ниже
        "timestamp_note": "восстановлено скриптом sc1_recover_tx.py постфактум -- timestamp_utc = время восстановления, НЕ время исходной отправки (см. block_number для точного времени блока)",
        "image_file": image_file,
        "has_description": False,
        "status": result["status"],
        "actual_gas_used": result["actual_gas_used"],
        "actual_gas_price_wei": result["actual_gas_price_wei"],
        "actual_gas_cost_eth": result["actual_gas_cost_eth"],
        "actual_gas_cost_usd": result["actual_gas_cost_usd"],
        "launch_fee_eth": launch_fee_eth,
        "launch_fee_usd": launch_fee_eth * eth_usd_price,
        "recovered": True,
    }
    append_registry_and_commit(entry)
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(run())
