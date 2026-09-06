#!/usr/bin/env python3
"""Задание владельца, 2026-09-06, п.2: где торгуются токены fomo -- по
RPC, БЕСПЛАТНО (0 кредитов Dune). Для BUDDY/PICKAZO/AH42: `eth_getLogs`
по Transfer за последние сутки, топ-3 контрагента по числу событий.
Самый частый адрес = пул (в bonding-curve/AMM-модели пул участвует
почти в каждом Transfer: он либо `from` при покупке, либо `to` при
продаже).

Переиспользует уже готовую, проверенную инфраструктуру этой сессии
(analysis/alchemy_fallback.py): `_chunked_get_logs` (пагинация по
диапазону блоков, публичный RPC Robinhood Chain первым в приоритете,
см. `_endpoints()`) и `topic0()` (считаем сигнатуру события, не
хардкодим хэш)."""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import _chunked_get_logs, get_block, get_block_number, topic0  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_trading_venue_result.json")

TRANSFER_TOPIC = topic0("Transfer(address,address,uint256)")
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

TOKENS = {
    "BUDDY": "0x01600e4fb15591452f13c64fb849e70c4d4ac899",
    "PICKAZO": "0xb3629f80e40500121f2353174ab867b2710a6d7f",
    "AH42": "0x4c2f292a9a63cb083337688811bb029409569d05",
}


def topic_to_addr(topic_hex: str) -> str:
    return "0x" + topic_hex[-40:]


def estimate_from_block(latest_block: int, latest_ts: int, lookback_hours: float = 24.0) -> tuple[int, float]:
    """Реальная оценка блока ~lookback_hours назад -- считаем блоктайм
    ЭМПИРИЧЕСКИ по двум реальным заголовкам (не по памяти/допущению)."""
    ref_block = max(0, latest_block - 100_000)
    ref = get_block(ref_block)
    ref_ts = int(ref["timestamp"], 16)
    seconds_per_block = (latest_ts - ref_ts) / (latest_block - ref_block)
    print(f"[venue] реальный блоктайм (по {latest_block - ref_block} блокам): {seconds_per_block:.4f} с/блок")
    blocks_back = int((lookback_hours * 3600) / seconds_per_block)
    return max(0, latest_block - blocks_back), seconds_per_block


def run() -> int:
    latest_block = get_block_number()
    latest_header = get_block(latest_block)
    latest_ts = int(latest_header["timestamp"], 16)
    print(f"[venue] реальный latest_block={latest_block}, ts={latest_ts} ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(latest_ts))})")

    from_block, spb = estimate_from_block(latest_block, latest_ts, 24.0)
    print(f"[venue] диапазон последних ~24ч: [{from_block}, {latest_block}] ({latest_block - from_block} блоков)")

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "latest_block": latest_block, "from_block": from_block, "seconds_per_block": spb,
        "tokens": {},
    }

    for name, addr in TOKENS.items():
        print(f"\n[venue] === {name} ({addr}) ===")
        counter: Counter[str] = Counter()
        n_logs = 0
        for log in _chunked_get_logs(from_block, latest_block, [TRANSFER_TOPIC], address=addr, chunk_size=2000):
            n_logs += 1
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            frm = topic_to_addr(topics[1]).lower()
            to = topic_to_addr(topics[2]).lower()
            if frm != ZERO_ADDR:
                counter[frm] += 1
            if to != ZERO_ADDR:
                counter[to] += 1
        top3 = counter.most_common(3)
        print(f"[venue] {name}: {n_logs} реальных Transfer-логов за ~24ч, топ-3 контрагента: {top3}")
        out["tokens"][name] = {"address": addr, "n_transfer_logs_24h": n_logs, "top3_counterparties": top3}

    # Пересечение топ-1 по всем трём токенам -- если совпадает, это
    # сильный сигнал общего пула/роутера, не совпадения на одном токене.
    top1s = [info["top3_counterparties"][0][0] for info in out["tokens"].values() if info["top3_counterparties"]]
    common = Counter(top1s)
    out["top1_common_across_tokens"] = common.most_common()
    print(f"\n[venue] топ-1 адрес по каждому токену, пересечение: {common.most_common()}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[venue] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
