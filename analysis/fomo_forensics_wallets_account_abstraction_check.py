#!/usr/bin/env python3
"""Форензика fomo -- гипотеза владельца: taker != tx_from на dex.trades
(taker дал ~$53k/90д/118 сделок, tx_from дал $0) объясняется account
abstraction -- смарт-кошельками терминала fomo.family (ERC-4337 или
похожий паттерн: пользователь подписывает намерение, смарт-кошелёк
исполняет транзакцию, поэтому в качестве taker/экономического
бенефициара декодируется владелец, а tx_from -- адрес смарт-кошелька
или бандлера, не совпадающий с ним напрямую).

Проверка -- один `eth_getCode` на любой из шести адресов: если есть
bytecode, это контракт (согласуется с account abstraction), не EOA.
Бесплатно, без Dune."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import _rpc_call  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_wallets_account_abstraction_check_result.json")

WALLETS = {
    "unipcs": "0x0a6EBEd0155EDB4b21D92AD02897A626CD90119E",
    "ogle": "0x1Bcc5f67CD17e13770F199fA03bC043b0cde1143",
    "avast": "0xcc0C581613DFd4ACe7c8686668427236f8BD5cC5",
    "frogman": "0x14AA2A71dbb5eF87b81F92205E2699AA4aa65794",
    "DumbCrayonEater": "0x8f62a08537cede87d511aca6436274ab4ca080a3",
    "vee": "0xa0670863bd5cd0d60022bab2eed78e81e1a06bce",
}


def run() -> int:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "wallets": {}}
    # Владелец: "проверяется одним eth_getCode на любом из шести адресов" --
    # но раз бесплатно, проверяем все шесть, а не один -- полнее сигнал
    # без дополнительной цены.
    for name, addr in WALLETS.items():
        code = _rpc_call("eth_getCode", [addr, "latest"])
        has_code = code not in (None, "0x", "0x0")
        print(f"[aa_check] {name} ({addr}): bytecode present={has_code} (len={len(code or '')})")
        out["wallets"][name] = {"address": addr, "has_bytecode": has_code, "code_len_hex_chars": len(code or "")}

    n_contracts = sum(1 for w in out["wallets"].values() if w["has_bytecode"])
    out["n_contracts_of_6"] = n_contracts
    out["account_abstraction_hypothesis_supported"] = n_contracts > 0
    print(f"\n[aa_check] {n_contracts}/6 адресов -- реальные контракты (bytecode есть) -- "
          f"{'ПОДТВЕРЖДАЕТ' if n_contracts > 0 else 'НЕ подтверждает'} гипотезу account abstraction "
          f"(смарт-кошельки терминала)")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[aa_check] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
