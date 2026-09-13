#!/usr/bin/env python3
"""Точечная read-only проверка для итогового отчёта живого пилота
(внешнее ревью, второй раунд, пункт 5): "Адрес фактического кошелька
пилота и подтверждение, что его финансирование ограничено. Равенство
owner = Sender = --from-address само по себе не доказывает, что это
отдельный кошелёк."

Эта сессия НЕ знает, какой адрес владелец реально использует как
"кошелёк пилота" (PRIVATE_KEY_NOX на Ohio, вне доступа этой сессии).
0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75 -- адрес, прописанный как
owner в contracts/build/deploy_params_v4.json -- используется тем же
адресом и в МНОГИХ других задачах этого проекта (V3-деплой, сверка
инвентаря, живые шаги вне Задачи 5) -- честно НЕ доказывает, что это
ОТДЕЛЬНЫЙ, специально ограниченный по финансированию кошелёк, а не
основной операционный. Только сам владелец может подтвердить факт
"сколько реально переведено" -- эта проверка даёт ТОЛЬКО текущий
баланс ETH по факту на цепи, ничего не решает и не переводит."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call  # noqa: E402

ADDRESS = sys.argv[1] if len(sys.argv) > 1 else "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"


def main() -> None:
    balance_wei = int(_rpc_call("eth_getBalance", [ADDRESS, "latest"]), 16)
    block = int(_rpc_call("eth_blockNumber", []), 16)
    result = {
        "address": ADDRESS,
        "balance_wei": balance_wei,
        "balance_eth": balance_wei / 1e18,
        "checked_at_block": block,
        "note": ("ТОЛЬКО факт ончейн-баланса ETH на момент проверки. НЕ доказывает и НЕ опровергает, что это "
                 "отдельный, специально ограниченный по финансированию 'кошелёк пилота' -- это внешний по "
                 "отношению к коду факт, подтверждается только владельцем."),
    }
    print(json.dumps(result, indent=2))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_pilot_wallet_balance_check_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
