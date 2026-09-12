#!/usr/bin/env python3
"""Задача 5, живой бот -- владелец 2026-09-12 ('добавка', Путь А):
"Гистограмма по уже собранным 5919 сообщениям dry-run (диск, ноль сети)".

ЧЕСТНАЯ ОГОВОРКА, ПРОВЕРЕНО ПЕРЕД НАПИСАНИЕМ ЭТОГО СКРИПТА: тех 5919
сообщений прошлого dry-run НЕТ на диске -- тот прогон (`run_task5_bot_
dryrun_smoketest_ohio.yml`, `task5_bot_run.py`) писал только агрегатные
счётчики (число сообщений, число касаний пулов) и телеметрию попыток
(`attempts.jsonl`, пусто -- 0 детекций), НЕ сырые `to`/`data` по каждой
транзакции -- это подтверждено просмотром кода и содержимого
`data/task5_bot_live/`, `data/p3_guard_cache/task5_bot_dryrun_smoketest_
ohio_log.txt` перед этим шагом, не предположено. Поэтому этот скрипт --
РЕАЛЬНЫЙ, НОВЫЙ захват (не повторное использование несуществующего
дампа), через уже разрешённый локальный relay (loopback,
`ws://127.0.0.1:9642`, БЕЗ кулдауна на переподключение, см.
`_is_loopback_feed_url`) -- relay НЕ трогается (ни один Docker-вызов),
это ровно то же самое, что уже делает `task5_bot_run.py` каждый dry-run.

Пишет НА ДИСК decoded (`sequence_number`, `to`, `data`) по КАЖДОЙ
реально декодированной транзакции блока -- JSONL, по одной строке на
транзакцию -- чтобы дальнейший анализ (`task5_bot_router_histogram.py`)
был офлайн, без единого нового подключения к чему-либо, как и просил
владелец."""
from __future__ import annotations

import asyncio
import gzip
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import SEQUENCER_FEED_URL_LOCAL_RELAY
from task5_bot_feed_client import FeedMessage, SequencerFeedClient, decode_l2_message

DEFAULT_OUT = "data/task5_bot_router_capture/dump.jsonl.gz"


async def run(duration_s: float, feed_url: str, out_path: str) -> dict:
    client = SequencerFeedClient(feed_url)
    n_messages = 0
    n_tx_decoded = 0
    n_tx_skipped_no_to = 0
    diag_counter: Counter = Counter()

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    out_f = gzip.open(out_path, "wt")

    def on_message(msg: FeedMessage) -> None:
        nonlocal n_messages, n_tx_decoded, n_tx_skipped_no_to
        n_messages += 1
        if msg.raw_l2_msg_hex is None:
            diag_counter["no_l2msg_field"] += 1
            return
        entries = decode_l2_message(msg.raw_l2_msg_hex)
        for entry in entries:
            if "to" not in entry or entry.get("to") is None:
                n_tx_skipped_no_to += 1
                if "decode_error" in entry:
                    diag_counter["decode_error"] += 1
                elif "unhandled_msg_type" in entry:
                    diag_counter[f"unhandled_type_{entry['unhandled_msg_type']}"] += 1
                elif "unrecognized_first_byte" in entry:
                    diag_counter["unrecognized_first_byte"] += 1
                continue
            data_hex = entry.get("data") or "0x"
            out_f.write(json.dumps({
                "sequence_number": msg.sequence_number,
                "to": entry["to"].lower(),
                "data": data_hex,
            }) + "\n")
            n_tx_decoded += 1

    print(f"[router_capture] слушаю {feed_url} {duration_s:.0f}с, пишу decoded tx в {out_path} "
          f"(gzip JSONL, диск -- для офлайн-гистограммы без новых подключений)...")
    deadline = time.monotonic() + duration_s
    n_reconnects = 0
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(client.listen(on_message), timeout=remaining)
            except asyncio.TimeoutError:
                break
            except Exception as exc:
                n_reconnects += 1
                print(f"[router_capture] соединение оборвалось ({exc}) -- переподключение #{n_reconnects} "
                      f"(loopback relay -- без кулдауна)")
                client = SequencerFeedClient(feed_url)
                await asyncio.sleep(0.5)
    finally:
        out_f.close()

    return {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "feed_url": feed_url,
        "duration_s": duration_s,
        "n_reconnects": n_reconnects,
        "n_messages": n_messages,
        "n_tx_decoded": n_tx_decoded,
        "n_tx_skipped_no_to": n_tx_skipped_no_to,
        "diag": dict(diag_counter),
        "feed_client_diag": {k: v for k, v in client.diag.items() if isinstance(v, (int, float, str))},
        "out_path": out_path,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration-seconds", type=float, default=300.0)
    ap.add_argument("--feed-url", type=str, default=SEQUENCER_FEED_URL_LOCAL_RELAY)
    ap.add_argument("--out", type=str, default=DEFAULT_OUT)
    ap.add_argument("--summary-out", type=str, default="data/task5_bot_router_capture_summary.json")
    args = ap.parse_args()

    res = asyncio.run(run(args.duration_seconds, args.feed_url, args.out))
    text = json.dumps(res, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path(args.summary_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.summary_out).write_text(text)
