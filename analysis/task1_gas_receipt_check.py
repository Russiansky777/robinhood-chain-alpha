"""Задача 1 (владелец, 2026-09-17): "что объявлено про газ после 29.09 --
читать, не гадать". Последний недостающий кусок -- ПРЯМАЯ проверка на
живых чек-квитанциях (eth_getTransactionReceipt) трёх РЕАЛЬНЫХ Robinhood
Chain транзакций (см. data/task5_arb_suspect_audit_3tx_result.json) на
предмет отдельных Arbitrum/OP-style полей L1-компоненты комиссии
(l1Fee/l1GasUsed/gasUsedForL1/l1BaseFeeScalar и т.п.) -- дополняет (не
заменяет) уже собранный из документации и Dune-данных ответ:
Dune `robinhood.transactions.l1_fee`/`l1_gas_used` структурно NULL для
ВСЕХ реальных строк (см. docs/PROJECT_STATE.md), объяснение -- Arbitrum
Orbit-роллап встраивает L1-издержки в динамическую L2 base fee, а не
выставляет отдельной строкой. Этот скрипт проверяет то же самое, но на
сырой JSON-RPC квитанции напрямую с ноды -- самый прямой источник,
какой только можно получить без документации/агрегатора-посредника.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alchemy_fallback import get_transaction, get_transaction_fast, _rpc_call  # noqa: E402

REAL_TX_HASHES = [
    "0x2264176a23d8ced039d386829c709ce3b3ec335427582179fb7aaedcff4de245",
    "0xd487122244e6c89a0c7b91d48111720575294a6cfeceedfaa3cf105b1fb996d0",
    "0xa1733a2816fe30fbd3de9c8fedbcbe43f54c3dfdbe274bf838eba1f66a2c2236",
]

# Поля, которыми Arbitrum Nitro/Orbit ИЛИ OP-stack чейны иногда отдают
# L1-компоненту отдельно (разные роллап-стеки называют по-разному --
# проверяем оба семейства терминов, честно, не гадая заранее какой
# применим к ЭТОЙ цепи).
KNOWN_L1_FIELD_NAMES = (
    "l1Fee", "l1GasUsed", "l1GasPrice", "l1BaseFeeScalar", "l1BlobBaseFee",
    "l1BlobBaseFeeScalar", "gasUsedForL1", "l1BaseFee", "l1FeeScalar",
)


def get_receipt(tx_hash: str) -> dict:
    return _rpc_call("eth_getTransactionReceipt", [tx_hash])


def main() -> None:
    out = {"checked_at": "2026-09-17", "tx_results": []}
    for tx_hash in REAL_TX_HASHES:
        entry: dict = {"tx_hash": tx_hash}
        try:
            tx = get_transaction_fast(tx_hash)
            entry["transaction_fields"] = sorted(tx.keys()) if isinstance(tx, dict) else None
            entry["transaction_l1_fields_present"] = [
                k for k in KNOWN_L1_FIELD_NAMES if isinstance(tx, dict) and k in tx
            ]
            entry["transaction_raw"] = tx
        except Exception as e:  # noqa: BLE001
            entry["transaction_error"] = str(e)
        try:
            receipt = get_receipt(tx_hash)
            entry["receipt_fields"] = sorted(receipt.keys()) if isinstance(receipt, dict) else None
            entry["receipt_l1_fields_present"] = [
                k for k in KNOWN_L1_FIELD_NAMES if isinstance(receipt, dict) and k in receipt
            ]
            entry["receipt_raw"] = receipt
        except Exception as e:  # noqa: BLE001
            entry["receipt_error"] = str(e)
        out["tx_results"].append(entry)

    any_l1_field = any(
        e.get("transaction_l1_fields_present") or e.get("receipt_l1_fields_present")
        for e in out["tx_results"]
    )
    out["verdict"] = (
        "L1-компонента отдана ОТДЕЛЬНЫМ полем хотя бы в одной транзакции/квитанции"
        if any_l1_field
        else "НИ ОДНОГО известного отдельного L1-поля не найдено ни в транзакции, ни в квитанции ни у одной из 3 -- "
        "согласуется с уже установленным по Dune-данным: L1-издержки встроены в единую L2-комиссию, "
        "отдельной строкой на этой цепи структурно не наблюдаются."
    )

    out_path = Path(__file__).resolve().parent.parent / "data" / "task1_gas_receipt_check_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, default=str, ensure_ascii=False))
    print(json.dumps({"verdict": out["verdict"], "out_path": str(out_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
