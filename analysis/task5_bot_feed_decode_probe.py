#!/usr/bin/env python3
"""Задача 5, живой бот -- владелец 2026-09-12: "Декодер L2-сообщений
против живого фида -- первое и главное." Владелец также попросил
использовать готовый декодер (Offchain Labs Nitro / сторонние клиенты)
вместо своего -- см. ЧЕСТНУЮ ОГОВОРКУ ниже: реальная, готовая, судя по
описанию рабочая Python-библиотека НАЙДЕНА (`rhfeed`,
https://github.com/chainstacklabs/robinhood-chain-sequencer-feed,
подтверждено WebFetch реального README: `FeedConsumer(url=...).live()`,
поля `tx.to`/`tx.selector`/`tx.value` и т.д.), НО автоматический
инструмент безопасности этой сессии отказался позволить закоммитить
файл, устанавливающий стороннюю git-зависимость в проект живого
торгового бота ("Untrusted Code Integration") -- НЕ потому что
библиотека не работает, а потому что среда не даёт МНЕ её интегрировать
без ручного участия владельца. Установочная команда и код интеграции --
в docs/PROJECT_STATE.md, чтобы владелец мог применить её сам (на VPS,
вне ограничений этой сессии) при желании.

Этот скрипт -- собственный best-effort декодер (та же логика, что была
изначально), используется как ПЕРВЫЙ реальный шаг проверки, пока
интеграция готовой библиотеки не сделана вручную владельцем."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import SEQUENCER_FEED_URL_MAINNET
from task5_bot_feed_client import FeedMessage, SequencerFeedClient, decode_l2_message

KNOWN_SELECTORS = {
    "128acb08": "Uniswap V3 swap(address,bool,int256,uint160,bytes)",
    "414bf389": "Uniswap V3 SwapRouter exactInputSingle(...)",
    "c04b8d59": "Uniswap V3 SwapRouter exactInput(...)",
    "3593564c": "Universal Router execute(bytes,bytes[],uint256)",
    "24856bc3": "Uniswap V4 PoolManager execute (unlock-based, если применимо)",
}


async def run(duration_s: float, max_examples: int) -> dict:
    client = SequencerFeedClient(SEQUENCER_FEED_URL_MAINNET)
    type_counter: Counter = Counter()
    tx_kind_counter: Counter = Counter()
    selector_counter: Counter = Counter()
    examples: list[dict] = []
    decode_errors: list[str] = []

    def on_message(msg: FeedMessage) -> None:
        if msg.raw_l2_msg_hex is None:
            type_counter["no_l2msg_field"] += 1
            return
        result = decode_l2_message(msg.raw_l2_msg_hex)
        if result is None:
            type_counter["decode_returned_none"] += 1
            return
        if "unhandled_msg_type" in result:
            type_counter[f"unhandled_type_{result['unhandled_msg_type']}"] += 1
            return
        if "unrecognized_first_byte" in result:
            type_counter["unrecognized_first_byte"] += 1
            return
        if "decode_error" in result:
            type_counter["decode_error"] += 1
            if len(decode_errors) < 5:
                decode_errors.append(result["decode_error"])
            return

        type_counter["decoded_ok"] += 1
        tx_kind_counter[result.get("tx_kind", "?")] += 1
        data_hex = result.get("data") or "0x"
        selector = data_hex[2:10] if len(data_hex) >= 10 else None
        if selector:
            selector_counter[selector] += 1
        if len(examples) < max_examples:
            examples.append({
                "sequence_number": msg.sequence_number,
                "to": result.get("to"),
                "data_len_bytes": (len(data_hex) - 2) // 2,
                "selector": selector,
                "selector_known_as": KNOWN_SELECTORS.get(selector) if selector else None,
                "data_prefix": data_hex[:74],
            })

    print(f"[decode_probe] слушаю {SEQUENCER_FEED_URL_MAINNET} {duration_s:.0f} секунд "
          "(собственный декодер -- готовая библиотека rhfeed НЕ интегрирована в этой сессии, "
          "см. docstring и PROJECT_STATE.md)...")
    try:
        await asyncio.wait_for(client.listen(on_message), timeout=duration_s)
    except asyncio.TimeoutError:
        pass

    return {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "decoder_used": "собственный best-effort (см. docstring про rhfeed)",
        "duration_s": duration_s,
        "feed_diag": client.diag,
        "message_type_counts": dict(type_counter),
        "tx_kind_counts": dict(tx_kind_counter),
        "top_selectors": dict(selector_counter.most_common(20)),
        "known_selector_hits": {
            sel: {"count": n, "meaning": KNOWN_SELECTORS[sel]}
            for sel, n in selector_counter.items() if sel in KNOWN_SELECTORS
        },
        "sample_decode_errors": decode_errors,
        "examples": examples,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=180.0)
    ap.add_argument("--max-examples", type=int, default=15)
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    res = asyncio.run(run(args.duration, args.max_examples))
    text = json.dumps(res, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
