#!/usr/bin/env python3
"""Владелец, 2026-09-03: быстрая диагностика (только чтение, минимум
вызовов) -- проверить, дошли ли 0.0039 WETH от Across MulticallHandler
до кошелька владельца 0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75, или
они зависли на самом handler-контракте.

1. eth_getTransactionReceipt(tx) -- ВСЕ Transfer-логи (from -> to ->
   amount), чтобы увидеть весь путь средств внутри транзакции. Полный
   адрес handler'а НЕ угадывается (владелец дал только префикс/суффикс
   0xa8aD...0ab6BD) -- берётся ИЗ РЕАЛЬНЫХ логов транзакции (адрес,
   подходящий под этот паттерн), не изобретается.
2. eth_call balanceOf(handler) на WETH -- текущий баланс, если завис.
3. Текущие балансы кошелька: native ETH (eth_getBalance), WETH и USDG
   (balanceOf).

Только чтение (eth_getTransactionReceipt/eth_call/eth_getBalance),
транзакций не отправляет.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_abi import decode as abi_decode  # noqa: E402

from alchemy_fallback import _rpc_call, topic0  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/across_handler_probe_result.json")
TX_HASH = "0x653c585e774f43ff0dad930c97dccffca806c0704591cd3bb0d9ed73fb6e58aa"
WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
HANDLER_PREFIX_SUFFIX = ("0xa8ad", "0ab6bd")  # владелец: "0xa8aD...0ab6BD" -- для поиска в логах, не для угадывания
TRANSFER_TOPIC0 = topic0("Transfer(address,address,uint256)")


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def _addr_from_topic(topic_hex: str) -> str:
    return "0x" + topic_hex[-40:]


def erc20_balance_of(token: str, holder: str) -> int:
    calldata = "0x" + _selector("balanceOf(address)")[2:] + holder[2:].rjust(64, "0").lower()
    raw = _rpc_call("eth_call", [{"to": token, "data": calldata}, "latest"])
    return int(raw, 16)


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tx_hash": TX_HASH}

    print(f"=== eth_getTransactionReceipt({TX_HASH}) ===")
    receipt = _rpc_call("eth_getTransactionReceipt", [TX_HASH])
    if receipt is None:
        result["error"] = "receipt is None -- транзакция не найдена (неверный хэш или ещё не замайнилась)"
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(f"[across_handler_probe] {result['error']}")
        return 1

    result["receipt_status"] = receipt.get("status")
    result["receipt_to"] = receipt.get("to")
    result["receipt_from"] = receipt.get("from")
    result["block_number"] = int(receipt["blockNumber"], 16)
    print(f"[across_handler_probe] status={receipt.get('status')} to={receipt.get('to')} from={receipt.get('from')} "
          f"block={result['block_number']}")

    transfers = []
    candidate_handler = None
    for log in receipt.get("logs", []):
        if len(log.get("topics", [])) == 3 and log["topics"][0].lower() == TRANSFER_TOPIC0.lower():
            token = log["address"]
            frm = _addr_from_topic(log["topics"][1])
            to = _addr_from_topic(log["topics"][2])
            amount_raw = abi_decode(["uint256"], bytes.fromhex(log["data"][2:]))[0]
            entry = {"token": token, "from": frm, "to": to, "amount_raw": amount_raw,
                      "amount_human_if_18dec": amount_raw / 1e18}
            transfers.append(entry)
            for addr in (frm, to):
                if addr.lower().startswith(HANDLER_PREFIX_SUFFIX[0]) and addr.lower().endswith(HANDLER_PREFIX_SUFFIX[1]):
                    candidate_handler = addr

    result["all_transfer_logs"] = transfers
    result["candidate_handler_address_from_logs"] = candidate_handler
    print(f"[across_handler_probe] найдено Transfer-логов: {len(transfers)}")
    for e in transfers:
        print(f"  token={e['token']} {e['from']} -> {e['to']}  amount_raw={e['amount_raw']} "
              f"(~{e['amount_human_if_18dec']:.8f} если 18 decimals)")
    print(f"[across_handler_probe] handler-адрес по паттерну владельца (0xa8aD...0ab6BD) из логов: {candidate_handler}")

    # НАЙДЕНО (реальный первый прогон этого скрипта, 2026-09-03): последний
    # Transfer в цепочке -- handler -> address(0) -- это НЕ утечка/сжигание
    # средств, а канонический паттерн WETH9.withdraw() (WETH сжигается
    # Transfer-логом на address(0), встроенным в сам ERC20-контракт WETH,
    # РЕЗУЛЬТАТ -- native ETH зачисляется вызывающему, т.е. handler'у).
    # Значит "завис ли на handler'е" нужно проверять по NATIVE ETH
    # балансу handler'а СЕЙЧАС, не по WETH (который тривиально уже 0 --
    # он только что сожжён в этой же транзакции). Также ищем отдельный
    # Withdrawal(address,uint256) лог того же WETH-контракта -- он
    # выставляется WETH9 ТОЛЬКО из withdraw(), не бывает у произвольного
    # Transfer-to-zero -- если он есть, это подтверждает withdraw(),
    # не что-то более странное.
    withdrawal_topic0 = topic0("Withdrawal(address,uint256)")
    withdrawal_logs = []
    for log in receipt.get("logs", []):
        if log["address"].lower() == WETH.lower() and log["topics"][0].lower() == withdrawal_topic0.lower():
            src = _addr_from_topic(log["topics"][1])
            amount_raw = abi_decode(["uint256"], bytes.fromhex(log["data"][2:]))[0]
            withdrawal_logs.append({"src": src, "amount_raw": amount_raw, "amount_human": amount_raw / 1e18})
    result["weth_withdrawal_logs"] = withdrawal_logs
    print(f"[across_handler_probe] WETH.Withdrawal-логов (подтверждает withdraw(), не просто Transfer-to-zero): "
          f"{withdrawal_logs}")

    if candidate_handler:
        print(f"\n=== native ETH баланс handler'а СЕЙЧАС ({candidate_handler}) ===")
        handler_native_eth = int(_rpc_call("eth_getBalance", [candidate_handler, "latest"]), 16)
        result["handler_native_eth_balance_raw"] = handler_native_eth
        result["handler_native_eth_balance_human"] = handler_native_eth / 1e18
        print(f"[across_handler_probe] native ETH на handler'е: {handler_native_eth} raw "
              f"(~{handler_native_eth/1e18:.8f} ETH) -- это и есть проверка \"зависли ли\" после withdraw()")

    if candidate_handler:
        print(f"\n=== balanceOf(WETH, {candidate_handler}) ===")
        handler_weth_balance = erc20_balance_of(WETH, candidate_handler)
        result["handler_weth_balance_raw"] = handler_weth_balance
        result["handler_weth_balance_human"] = handler_weth_balance / 1e18
        print(f"[across_handler_probe] WETH на handler'е: {handler_weth_balance} raw (~{handler_weth_balance/1e18:.8f} WETH)")
    else:
        result["handler_weth_balance_note"] = ("Адрес, подходящий под паттерн 0xa8aD...0ab6BD, НЕ найден среди "
                                                 "from/to в Transfer-логах этой транзакции -- не считаю баланс "
                                                 "по неподтверждённому адресу.")
        print(f"[across_handler_probe] {result['handler_weth_balance_note']}")

    print(f"\n=== Реальные балансы кошелька {WALLET} ===")
    eth_raw = int(_rpc_call("eth_getBalance", [WALLET, "latest"]), 16)
    weth_raw = erc20_balance_of(WETH, WALLET)
    usdg_raw = erc20_balance_of(USDG, WALLET)
    result["wallet_balances"] = {
        "native_eth_raw": eth_raw, "native_eth_human": eth_raw / 1e18,
        "weth_raw": weth_raw, "weth_human": weth_raw / 1e18,
        "usdg_raw": usdg_raw, "usdg_human": usdg_raw / 1e6,
    }
    print(f"[across_handler_probe] native ETH={eth_raw/1e18:.8f}  WETH={weth_raw/1e18:.8f}  USDG={usdg_raw/1e6:.6f}")

    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[across_handler_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
