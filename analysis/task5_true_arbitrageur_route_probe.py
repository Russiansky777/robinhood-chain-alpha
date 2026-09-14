#!/usr/bin/env python3
"""Владелец (уточняющий запрос по уже сохранённой выборке): "Найди до
трёх кандидатов с маршрутами, поддерживаемыми нашим текущим
исполнителем... Покажи txHash, маршрут, индекс в блоке, прибыль и её
достоверность." Разрешение на ЭТОТ конкретный точечный запрос (не
скан, не live, не benchmark) -- через AskUserQuestion: "Да, точечные
receipt по нескольким tx".

ВХОД: фиксированный список из 10 tx_hash -- ПО ОДНОМУ представителю
(с максимальной net_after_gas_usd среди видимых записей) на каждый из
10 УНИКАЛЬНЫХ contract-адресов, встреченных в уже восстановленном
частичном хвосте (294 из 1083, см. data/task5_true_arbitrageur_scan_
result_RECOVERED_PARTIAL_last294.json) -- ЭТО НЕ полная выборка, но
выбрана так, чтобы максимизировать разнообразие sender/executor при
минимуме новых RPC-вызовов (10 точечных eth_getTransactionReceipt,
не диапазонный eth_getLogs, никакого нового скана блоков).

Для каждой tx: получить реальный receipt, восстановить маршрут (та же
логика классификации, что и в task5_true_arbitrageur_scan_enrich.py --
V4 PoolManager Swap vs standalone V3 Swap по topic0), определить
поддержку нашим исполнителем (только V4-only маршруты поддержаны --
build_execute_cycle_calldata/RouteLeg не моделируют V3), напечатать
blockNumber/transactionIndex для честной оценки достоверности прибыли
(prices/decimals те же, что уже посчитал скан -- здесь НЕ пересчитываем
gain, только берём его из уже сохранённой строки скана)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import UNISWAP_V3_SWAP_SIG, UNISWAP_V4_SWAP_SIG, _rpc_call, topic0  # noqa: E402

V4_SWAP_TOPIC0 = topic0(UNISWAP_V4_SWAP_SIG).lower()
V3_SWAP_TOPIC0 = topic0(UNISWAP_V3_SWAP_SIG).lower()
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

# Известный конкурент (из предыдущих разборов этого проекта) -- сверяем
# точное совпадение адресов, ничего не объединяем/не разделяем без
# доказательств.
KNOWN_COMPETITOR_EXECUTOR = "0x1b357e7acd2a32aebfa2de286c9e8e617d39a251"
KNOWN_COMPETITOR_BENEFICIARY = "0x11854ce19dcd63a7eccaa12ded5aa991c94c79c6"

# 10 представителей -- по одному на уникальный contract, максимум
# net_after_gas_usd среди уже восстановленных 294 строк для этого
# contract. Взято из data/task5_true_arbitrageur_scan_result_RECOVERED_
# PARTIAL_last294.json (уже сохранённые данные, не выдумано).
CANDIDATES = [
    {"tx_hash": "0xb06998a96e2f7ccbd52f8d0f5ba21a7f4ad6ab1afef79cb887e2935621ccad79",
     "contract": "0x8e3d7e6ae1c00006caad127b8902657f770b7a16",
     "initiator": "0x86209bd212ee68c4e1f10d0eaf1f04498abe2f15", "net_after_gas_usd": 3.6014647325526763},
    {"tx_hash": "0xc20d074216a0c63a0907672b87892f1fe6cd67af79f1e407328a00568dae9af8",
     "contract": "0x56d9e14e1dc231ed99dafe0e1d383702699420ca",
     "initiator": "0x0001a7a75bfe2acba8d3e52ab7cd80dfcb16d6dc", "net_after_gas_usd": 3.083496596478616},
    {"tx_hash": "0xf481730bc7ccfb821d120108ba10b9d8bb164e9536b745c0a8f3e0f7a8490ca7",
     "contract": "0xace1016b6527b75fc4c4e106bc79e45a387e085f",
     "initiator": "0xcf853bdec5c5833b95b5d88c6b98b8cd0c6ec2f0", "net_after_gas_usd": 2.633087061605102},
    {"tx_hash": "0x95f9811359bc01b5649a4d70a57f337d74aeb09352743613c59672a13b0e0a7c",
     "contract": "0x8f83df36480755e795d5796640254f0d9dc47c09",
     "initiator": "0x01878cdcb3ceead8962cd73874ecc5ec3c85fa11", "net_after_gas_usd": 2.57894662383626},
    {"tx_hash": "0x5b9043f7446cc3e2731befaba5536abbd364b16bfd68273a3d40f9ffb2169ab8",
     "contract": "0xed4728d89bd4ef81177d8f5448f9bd1bae4e23e3",
     "initiator": "0x75f0e301d5f3dff849143edb756adc40c06661f1", "net_after_gas_usd": 2.323176948308353},
    {"tx_hash": "0x5097ff156f76bd6c17dd69a01fccb34f66484e0f39eb4aecce147655396b4ebd",
     "contract": "0x18ab2e9a7c97935f026bb451d2bc7c4ab3b2050d",
     "initiator": "0x405b2c7696ed81636d30d9fa4846aac3e541dc29", "net_after_gas_usd": 0.7881821780181272},
    {"tx_hash": "0xc8bcb509074ee0474b3e1c378d7e6ba6cb97c61571add7f4c540ec69a3267cb3",
     "contract": "0xd73c75a140c026fdd516f5ab50e8884914585d7f",
     "initiator": "0x991a34cea68bf53b208cce80ed3dbb902bc9c94f", "net_after_gas_usd": 0.5184557965533256},
    {"tx_hash": "0x8f6de90aec85b844a7b6cc3ecd938c318a6e2361a2075d1f8b84882e6ce782be",
     "contract": "0x4904f1c3d68403351c203e208f7c70e95e7e48a9",
     "initiator": "0x0a45dcd332083d412c047f7a20ce804a3fc32c25", "net_after_gas_usd": 0.2967267008043959},
    {"tx_hash": "0xa65a15c394766fe5cbce21935f6dac8783613115e24e3b70b9d4358ff0d2321d",
     "contract": "0x72901a86a8470cb55c97b450d76ca07da2179768",
     "initiator": "0xc1e2267b887e53dde9a8e5f9cb2c959b7b189937", "net_after_gas_usd": 0.29570384971737834},
    {"tx_hash": "0x6220373c5afae122ac698ef4b565d1a9ce7862b0893141bd466932ba0a76b3b9",
     "contract": "0x1e93a79be11f39a72bae9a0a6c695428cfd50e25",
     "initiator": "0xfc1b9b257f9bb2a54f93cc65c256a54126bbe371", "net_after_gas_usd": 0.2860501629456811},
]


def get_receipt(tx_hash: str) -> dict:
    body = _rpc_call("eth_getTransactionReceipt", [tx_hash])
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(f"eth_getTransactionReceipt error: {body['error']}")
    return body if isinstance(body, dict) else {}


def extract_route_legs(receipt: dict) -> list[dict]:
    legs = []
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if not topics:
            continue
        t0 = topics[0].lower()
        addr = (log.get("address") or "").lower()
        if t0 == V4_SWAP_TOPIC0 and addr == POOL_MANAGER.lower():
            legs.append({"kind": "v4_pool_manager", "log_index": log.get("logIndex"),
                         "pool_manager_address": log.get("address"),
                         "pool_id": topics[1] if len(topics) > 1 else None})
        elif t0 == V3_SWAP_TOPIC0:
            legs.append({"kind": "v3_standalone_pool", "log_index": log.get("logIndex"),
                         "pool_address": log.get("address")})
    legs.sort(key=lambda l: int(l["log_index"], 16) if isinstance(l["log_index"], str) else (l["log_index"] or 0))
    return legs


def route_supported(legs: list[dict]) -> dict:
    if not legs:
        return {"supported": None, "reason": "не удалось извлечь ни одной Swap-ноги из логов рецепта"}
    has_v3 = any(l["kind"] == "v3_standalone_pool" for l in legs)
    if has_v3:
        return {"supported": False, "reason": "маршрут включает standalone V3-пул -- адаптера нет"}
    return {"supported": True, "reason": "все ноги -- V4 PoolManager Swap -- совместимо с RouteLeg/"
                                          "build_execute_cycle_calldata (currency0/1/fee/tickSpacing/hooks "
                                          "конкретных ног отдельно НЕ проверялись против RouteRegistry)"}


def main() -> None:
    out = []
    for cand in CANDIDATES:
        tx_hash = cand["tx_hash"]
        try:
            receipt = get_receipt(tx_hash)
        except Exception as exc:  # noqa: BLE001
            out.append({"tx_hash": tx_hash, "error": f"{type(exc).__name__}: {exc}"})
            continue
        legs = extract_route_legs(receipt)
        support = route_supported(legs)
        matches_known_competitor = (
            cand["contract"] == KNOWN_COMPETITOR_EXECUTOR or cand["initiator"] == KNOWN_COMPETITOR_EXECUTOR
            or cand["contract"] == KNOWN_COMPETITOR_BENEFICIARY or cand["initiator"] == KNOWN_COMPETITOR_BENEFICIARY)
        out.append({
            "tx_hash": tx_hash,
            "contract": cand["contract"], "initiator": cand["initiator"],
            "net_after_gas_usd_from_scan": cand["net_after_gas_usd"],
            "block_number": receipt.get("blockNumber"),
            "transaction_index": receipt.get("transactionIndex"),
            "n_route_legs": len(legs),
            "route_legs": legs,
            "route_supported_by_our_executor": support,
            "matches_known_competitor_address": matches_known_competitor,
        })
    result = {"n_candidates_probed": len(CANDIDATES), "probes": out}
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path(__file__).parent.parent.joinpath("data", "task5_true_arbitrageur_route_probe_result.json").write_text(text)


if __name__ == "__main__":
    main()
