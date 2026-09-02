"""Общий модуль для наблюдения за Across Protocol SpokePool на Robinhood
Chain (только чтение -- НИКАКИХ транзакций, ключ не используется нигде
в этом модуле). Задача A дозапроса владельца (2026-09-02): "Релеер
Across -- прогон в режиме наблюдения, без капитала".

Источники (дословные запросы к первоисточнику, не WebSearch-агрегат,
не предположено):

- Адрес SpokePool на chain 4663 -- `broadcast/deployed-addresses.json`
  в `github.com/across-protocol/contracts` (source of truth по README
  того же репозитория): `SpokePool.address =
  0xD29C85F15DF544bA632C9E25829fd29d767d7978`, `block_number = 156309`
  (блок деплоя -- самый ранний осмысленный from_block для полного
  скана истории).
- Схема событий -- дословный запрос `contracts/interfaces/
  V3SpokePoolInterface.sol` и `contracts/spoke-pools/SpokePool.sol`
  того же репозитория (см. докстринги ниже у каждой сигнатуры).

**Честная оговорка про полноту картины**: этот модуль наблюдает ТОЛЬКО
SpokePool на Robinhood Chain. Для маршрута USDC->USDG (депозит на
другом чейне, филл на Robinhood) отсюда видна ПОЛНАЯ картина филла
(релеер, время, repaymentChainId, все параметры relayData -- они
дублируются в событии филла) -- см. `fetch_filled_relay_logs`. Для
обратного маршрута USDG->USDC (депозит НА Robinhood, филл на другом
чейне) отсюда видна ТОЛЬКО сторона депозита (маршрут/размер/спред/
эксклюзивность) -- see `fetch_deposit_logs` -- КТО и ЗА СКОЛЬКО СЕКУНД
заполнил такую заявку с этого чейна не видно, это требует RPC второго
(counterparty) чейна, который в этом проекте до сих пор не был
задействован. Если разведка (`relayer_recon.py`) установит, какой
именно чейн является контрагентом, и это окажется чейном с широко
доступным бесплатным публичным RPC (Ethereum mainnet/Arbitrum/Base и
т.п.) -- добавляется отдельно, не выдумывается заранее.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_abi import decode as abi_decode  # noqa: E402

from alchemy_fallback import _chunked_get_logs, _rpc_call, topic0  # noqa: E402

SPOKE_POOL = "0xD29C85F15DF544bA632C9E25829fd29d767d7978"
SPOKE_POOL_DEPLOY_BLOCK = 156309  # broadcast/deployed-addresses.json, across-protocol/contracts

# --- Сигнатуры событий (дословно из V3SpokePoolInterface.sol / SpokePool.sol) ---

FUNDS_DEPOSITED_SIG = (
    "FundsDeposited(bytes32,bytes32,uint256,uint256,uint256,uint256,uint32,uint32,uint32,bytes32,bytes32,bytes32,bytes)"
)
# indexed: inputToken(bytes32), outputToken(bytes32), destinationChainId(uint256)
# data: inputAmount, outputAmount, depositId, quoteTimestamp, fillDeadline,
#       exclusivityDeadline, depositor, recipient, exclusiveRelayer, message

FILLED_RELAY_SIG = (
    "FilledRelay(bytes32,bytes32,uint256,uint256,uint256,uint256,uint256,uint32,uint32,"
    "bytes32,bytes32,bytes32,bytes32,bytes32,(bytes32,bytes32,uint256,uint8))"
)
# indexed: originChainId(uint256), depositId(uint256), relayer(bytes32)
# data: inputToken, outputToken, inputAmount, outputAmount, repaymentChainId,
#       fillDeadline, exclusivityDeadline, exclusiveRelayer, depositor,
#       recipient, messageHash, relayExecutionInfo(updatedRecipient,
#       updatedMessageHash, updatedOutputAmount, fillType[FastFill=0/
#       ReplacedSlowFill=1/SlowFill=2])

ENABLED_DEPOSIT_ROUTE_SIG = "EnabledDepositRoute(address,uint256,bool)"
# indexed: originToken(address), destinationChainId(uint256); data: enabled(bool)

REQUESTED_SPEED_UP_DEPOSIT_SIG = "RequestedSpeedUpDeposit(uint256,uint256,bytes32,bytes32,bytes,bytes)"
# indexed: depositId(uint256), depositor(bytes32)
# data: updatedOutputAmount, updatedRecipient, updatedMessage, depositorSignature


def _b32_to_addr(b32: str | bytes) -> str:
    """bytes32-адрес (обобщённая cross-VM адресация Across V3+) ->
    обычный 20-байтный EVM-адрес (нижние 20 байт), тот же принцип, что
    везде в проекте для topics[]. Принимает ОБА представления: hex-строку
    (индексированные поля -- `log["topics"][i]`, как приходят из
    JSON-RPC) и `bytes` (неиндексированные поля -- то, что возвращает
    `eth_abi.decode` для данных из `data`)."""
    if isinstance(b32, bytes):
        return "0x" + b32.hex()[-40:]
    return "0x" + b32[-40:]


def fetch_enabled_deposit_route_logs(from_block: int, to_block: int, chunk_size: int = 20_000):
    """Редкое админ-событие -- намеренно БОЛЬШОЙ chunk_size (в 10 раз
    больше дефолтного 2000) относительно остальных сканов этого
    проекта, чтобы не тратить тысячи базовых вызовов на разреженный
    топик по всей истории чейна (SpokePool задеплоен на блоке 156309,
    сейчас блок >52M) -- адаптивная бисекция при устойчивом таймауте
    (см. alchemy_fallback._chunked_get_logs) подстрахует, если чанк
    всё равно окажется велик для ноды."""
    n_calls = 0

    def _count(lo, hi, n):
        nonlocal n_calls
        n_calls += 1

    logs = list(_chunked_get_logs(
        from_block, to_block, [topic0(ENABLED_DEPOSIT_ROUTE_SIG)],
        chunk_size=chunk_size, address=SPOKE_POOL, on_call=_count,
    ))
    print(f"[across_common] EnabledDepositRoute: {n_calls} вызовов eth_getLogs, {len(logs)} событий")
    return logs


def decode_enabled_deposit_route(log: dict) -> dict:
    origin_token = "0x" + log["topics"][1][-40:]
    destination_chain_id = int(log["topics"][2], 16)
    (enabled,) = abi_decode(["bool"], bytes.fromhex(log["data"][2:]))
    return {
        "block_number": int(log["blockNumber"], 16),
        "tx_hash": log["transactionHash"],
        "origin_token": origin_token,
        "destination_chain_id": destination_chain_id,
        "enabled": enabled,
    }


def fetch_deposit_logs(from_block: int, to_block: int, chunk_size: int = 2000, on_call=None):
    return _chunked_get_logs(
        from_block, to_block, [topic0(FUNDS_DEPOSITED_SIG)],
        chunk_size=chunk_size, address=SPOKE_POOL, on_call=on_call,
    )


def decode_funds_deposited(log: dict) -> dict:
    input_token_b32, output_token_b32, destination_chain_id = (
        log["topics"][1], log["topics"][2], int(log["topics"][3], 16),
    )
    (
        input_amount, output_amount, deposit_id, quote_timestamp, fill_deadline,
        exclusivity_deadline, depositor_b32, recipient_b32, exclusive_relayer_b32, message,
    ) = abi_decode(
        ["uint256", "uint256", "uint256", "uint32", "uint32", "uint32", "bytes32", "bytes32", "bytes32", "bytes"],
        bytes.fromhex(log["data"][2:]),
    )
    return {
        "block_number": int(log["blockNumber"], 16),
        "tx_index": int(log["transactionIndex"], 16),
        "log_index": int(log["logIndex"], 16),
        "tx_hash": log["transactionHash"],
        "origin_chain_id": 4663,
        "input_token": _b32_to_addr(input_token_b32),
        "output_token": _b32_to_addr(output_token_b32),
        "destination_chain_id": destination_chain_id,
        "input_amount": input_amount,
        "output_amount": output_amount,
        "deposit_id": deposit_id,
        "quote_timestamp": quote_timestamp,
        "fill_deadline": fill_deadline,
        "exclusivity_deadline": exclusivity_deadline,
        "depositor": _b32_to_addr(depositor_b32),
        "recipient": _b32_to_addr(recipient_b32),
        "exclusive_relayer": _b32_to_addr(exclusive_relayer_b32),
        "has_message": len(message) > 0,
    }


def fetch_filled_relay_logs(from_block: int, to_block: int, chunk_size: int = 2000, on_call=None):
    return _chunked_get_logs(
        from_block, to_block, [topic0(FILLED_RELAY_SIG)],
        chunk_size=chunk_size, address=SPOKE_POOL, on_call=on_call,
    )


def decode_filled_relay(log: dict) -> dict:
    origin_chain_id = int(log["topics"][1], 16)
    deposit_id = int(log["topics"][2], 16)
    relayer_b32 = log["topics"][3]
    (
        input_token_b32, output_token_b32, input_amount, output_amount, repayment_chain_id,
        fill_deadline, exclusivity_deadline, exclusive_relayer_b32, depositor_b32, recipient_b32,
        message_hash, relay_exec_info,
    ) = abi_decode(
        ["bytes32", "bytes32", "uint256", "uint256", "uint256", "uint32", "uint32",
         "bytes32", "bytes32", "bytes32", "bytes32", "(bytes32,bytes32,uint256,uint8)"],
        bytes.fromhex(log["data"][2:]),
    )
    updated_recipient_b32, updated_message_hash, updated_output_amount, fill_type = relay_exec_info
    return {
        "block_number": int(log["blockNumber"], 16),
        "tx_index": int(log["transactionIndex"], 16),
        "log_index": int(log["logIndex"], 16),
        "tx_hash": log["transactionHash"],
        "destination_chain_id": 4663,  # это событие эмиттится ЗДЕСЬ, на Robinhood Chain
        "origin_chain_id": origin_chain_id,
        "deposit_id": deposit_id,
        "relayer": _b32_to_addr(relayer_b32),
        "input_token": _b32_to_addr(input_token_b32),
        "output_token": _b32_to_addr(output_token_b32),
        "input_amount": input_amount,
        "output_amount": output_amount,
        "repayment_chain_id": repayment_chain_id,
        "fill_deadline": fill_deadline,
        "exclusivity_deadline": exclusivity_deadline,
        "exclusive_relayer": _b32_to_addr(exclusive_relayer_b32),
        "depositor": _b32_to_addr(depositor_b32),
        "recipient": _b32_to_addr(recipient_b32),
        "updated_output_amount": updated_output_amount,
        "fill_type": ["FastFill", "ReplacedSlowFill", "SlowFill"][fill_type],
    }


def erc20_symbol(token_address: str) -> str | None:
    try:
        result = _rpc_call("eth_call", [{"to": token_address, "data": "0x95d89b41"}, "latest"])
        raw = bytes.fromhex(result[2:])
        length = int.from_bytes(raw[32:64], "big")
        return raw[64:64 + length].decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 -- диагностика, не блокирует
        print(f"[across_common] symbol() на {token_address} не удался: {e}")
        return None


def erc20_decimals(token_address: str) -> int | None:
    try:
        result = _rpc_call("eth_call", [{"to": token_address, "data": "0x313ce567"}, "latest"])
        return int(result, 16)
    except Exception as e:  # noqa: BLE001
        print(f"[across_common] decimals() на {token_address} не удался: {e}")
        return None
