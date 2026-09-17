#!/usr/bin/env python3
"""Задача 5, живой бот -- финальный вопрос по линии: 530мс отставания
RPC от фида (`task5_feed_vs_block_timing.py`) -- это НАСТОЯЩАЯ фора
(секвенсер ещё принимает транзакции в текущий блок, RPC просто медленно
показывает) или ИЛЛЮЗИЯ (блок давно закрыт, RPC рассказывает об этом с
опозданием)?

Владелец (заказчик), 2026-09-17, дословно (сокращённо): "Отличить можно
прямо, только по фиду, без RPC как источника времени. Для транзакции,
увиденной в фиде в момент T, в какой блок она в итоге попала -- в тот,
что был текущим в момент T, или в следующий? Определять блок по receipt
(eth_getTransactionReceipt), но время -- ТОЛЬКО по фиду, RPC как часы
не использовать. Интервал 'появление транзакции в фиде -> появление
границы следующего блока' -- реальная длина окна. Темп по фиду (первое
сообщение блока N -> первое сообщение блока N+1)."

Метод: RPC используется ИСКЛЮЧИТЕЛЬНО как источник ДАННЫХ (какой номер
блока реально присвоен транзакции, по receipt) -- НИКОГДА как источник
ВРЕМЕНИ (все t_wall -- только из локальных часов в момент получения
сообщения фида, тот же принцип, что и в предыдущих замерах). Никакого
RPC-поллера eth_blockNumber в этом скрипте нет вообще -- в отличие от
`task5_feed_vs_block_timing.py`, здесь это намеренно устранено.

Ключевая диагностика: `delta_blocks = receipt.blockNumber -
message.sequenceNumber` (для транзакции, извлечённой ИЗ конкретного
сообщения фида с этим sequenceNumber). Если `sequenceNumber` -- это и
есть номер L2-блока (уже похоже подтверждено ранее, но не при полной
выборке) И каждое сообщение атомарно доставляет ПОЛНОСТЬЮ готовый,
уже решённый блок (что согласуется с тем, что сообщения приходят
ПАЧКАМИ по 8-10+ транзакций, не по одной) -- delta_blocks должна быть
ВСЕГДА 0, и тогда "530мс форы" -- иллюзия чистой задержки RPC-индексации,
а не окно приёма. Если найдутся случаи delta_blocks != 0 -- это прямое
свидетельство, что блок ещё дозаполняется ПОСЛЕ первого сообщения о нём,
и тогда есть реальное окно, которое можно измерить по разнице t_wall
между сообщениями."""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests
from eth_utils import keccak

from task5_bot_config import SEQUENCER_FEED_URL_MAINNET
from task5_bot_feed_client import FeedMessage, seconds_until_feed_connect_allowed
from task5_feed_vs_block_timing import (
    InspectingFeedClient,
    _extract_tx_bytes_list,
    _count_all_submessages,
    _resolve_rpc_endpoint,
)
from task5_bot_feed_client import _decode_l2_msg_bytes

RPC_ENDPOINT = _resolve_rpc_endpoint()
RECEIPT_SAMPLE_TARGET = 300


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    sv = sorted(values)
    return {
        "n": len(values), "median_ms": statistics.median(values),
        "p90_ms": sv[max(0, int(round(0.9 * (len(sv) - 1))))],
        "min_ms": min(values), "max_ms": max(values), "mean_ms": statistics.fmean(values),
    }


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration-seconds", type=float, default=240.0)
    args = ap.parse_args()

    wait_s = seconds_until_feed_connect_allowed()
    if wait_s > 0:
        print(json.dumps({"error": "feed_cooldown_active", "seconds_until_allowed": wait_s}), file=sys.stderr)
        raise SystemExit(
            f"Cooldown фида ещё активен -- нужно ещё {wait_s:.0f}с. НЕ подключаемся раньше времени "
            f"(владелец: 'фид доступен раз в 30 минут')."
        )

    print(f"[sequencer_window] RPC endpoint (ТОЛЬКО для номера блока, НЕ для времени): {RPC_ENDPOINT}",
          file=sys.stderr)

    feed_messages: list[dict] = []
    client = InspectingFeedClient(SEQUENCER_FEED_URL_MAINNET, raw_sample_limit=3)

    def on_message(msg: FeedMessage) -> None:
        tx_hashes: list[str] = []
        n_sub_total = 0
        if msg.raw_l2_msg_hex:
            raw_bytes = _decode_l2_msg_bytes(msg.raw_l2_msg_hex)
            if raw_bytes:
                for tb in _extract_tx_bytes_list(raw_bytes):
                    if tb:
                        tx_hashes.append("0x" + keccak(tb).hex())
                n_sub_total = _count_all_submessages(raw_bytes)
        feed_messages.append({
            "t_wall": msg.t_wall,
            "sequence_number": msg.sequence_number,
            "n_sub_total": n_sub_total,
            "tx_hashes": tx_hashes,
        })

    async def run_feed() -> None:
        listen_task = asyncio.create_task(client.listen(on_message))
        await asyncio.sleep(args.duration_seconds)
        listen_task.cancel()
        try:
            await listen_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    asyncio.run(run_feed())

    print(f"[sequencer_window] фид: {len(feed_messages)} сообщений", file=sys.stderr)

    # --- п.4: реальный темп по фиду (первое сообщение блока N -> первое сообщение блока N+1) ---
    # Только ПОСЛЕДОВАТЕЛЬНЫЕ sequenceNumber (seq[i+1] == seq[i]+1) -- пропуски/дубликаты
    # (если реально случатся) честно исключаются из этой конкретной статистики, не
    # притворяемся, что знаем интервал через пропущенный номер.
    feed_messages.sort(key=lambda m: m["sequence_number"])
    consecutive_intervals_ms: list[float] = []
    seq_gaps = 0
    for i in range(len(feed_messages) - 1):
        a, b = feed_messages[i], feed_messages[i + 1]
        if b["sequence_number"] == a["sequence_number"] + 1:
            consecutive_intervals_ms.append((b["t_wall"] - a["t_wall"]) * 1000.0)
        elif b["sequence_number"] > a["sequence_number"]:
            seq_gaps += 1

    # --- сэмпл транзакций для receipt lookup (RPC -- ТОЛЬКО номер блока) ---
    all_tx_candidates: list[tuple[str, int, float]] = []  # (tx_hash, source_seq, source_t_wall)
    for m in feed_messages:
        for h in m["tx_hashes"]:
            all_tx_candidates.append((h, m["sequence_number"], m["t_wall"]))
    step = max(1, len(all_tx_candidates) // RECEIPT_SAMPLE_TARGET) if all_tx_candidates else 1
    sampled = all_tx_candidates[::step][:RECEIPT_SAMPLE_TARGET]
    print(f"[sequencer_window] выборка для eth_getTransactionReceipt: {len(sampled)} из "
          f"{len(all_tx_candidates)} уникальных tx-хэшей", file=sys.stderr)

    session = requests.Session()
    per_tx: list[dict] = []
    for i, (tx_hash, source_seq, source_t_wall) in enumerate(sampled):
        entry = {"tx_hash": tx_hash, "source_seq": source_seq, "source_t_wall": source_t_wall}
        try:
            resp = session.post(RPC_ENDPOINT,
                                 json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt",
                                       "params": [tx_hash]}, timeout=10.0)
            body = resp.json()
            result = body.get("result")
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
            per_tx.append(entry)
            continue
        if not result:
            entry["receipt_found"] = False
            per_tx.append(entry)
            continue
        receipt_block = int(result["blockNumber"], 16)
        entry.update({
            "receipt_found": True,
            "receipt_block_number": receipt_block,
            "delta_blocks": receipt_block - source_seq,
        })
        per_tx.append(entry)
        if (i + 1) % 25 == 0:
            print(f"[sequencer_window] receipt lookup {i + 1}/{len(sampled)}...", file=sys.stderr)

    resolved = [e for e in per_tx if e.get("receipt_found")]
    delta_values = [e["delta_blocks"] for e in resolved]
    delta_histogram: dict[str, int] = {}
    for d in delta_values:
        key = str(d)
        delta_histogram[key] = delta_histogram.get(key, 0) + 1

    # --- прямая проверка (задание, "Прямая проверка, если возможна") ---
    # Для КАЖДОГО delta_blocks != 0 -- ищем, когда (по ФИДУ) реально появилось ПЕРВОЕ
    # сообщение с sequence_number == receipt_block_number (т.е. "официальное" объявление
    # того блока, куда транзакция реально попала) -- и сравниваем с source_t_wall (когда
    # мы впервые увидели САМУ транзакцию, в более раннем сообщении). Если receipt_block_number
    # уже был среди НАШИХ собранных сообщений -- честно вычисляем; если нет (блок объявлен
    # после нашего окна прослушивания) -- честно помечаем как "не проверено в этом окне".
    seq_to_t_wall = {m["sequence_number"]: m["t_wall"] for m in feed_messages}
    late_insertion_cases: list[dict] = []
    for e in resolved:
        if e["delta_blocks"] == 0:
            continue
        target_seq = e["receipt_block_number"]
        target_t_wall = seq_to_t_wall.get(target_seq)
        case = {**e}
        if target_t_wall is not None:
            case["target_block_first_seen_in_feed_t_wall"] = target_t_wall
            case["elapsed_ms_from_tx_seen_to_target_block_message"] = (target_t_wall - e["source_t_wall"]) * 1000.0
        else:
            case["note"] = "receipt_block_number не встретился среди sequence_number, собранных в этом окне " \
                            "прослушивания -- честно не проверено (блок объявлен до начала или после конца окна)."
        late_insertion_cases.append(case)

    n_delta_zero = sum(1 for d in delta_values if d == 0)
    frac_delta_zero = (n_delta_zero / len(delta_values)) if delta_values else None

    if not delta_values:
        verdict = "НЕТ ДАННЫХ -- ни одной транзакции не удалось сопоставить с receipt в этом прогоне."
    elif frac_delta_zero == 1.0:
        verdict = (
            "delta_blocks == 0 ДЛЯ ВСЕХ сопоставленных транзакций -- каждое сообщение фида атомарно "
            "доставляет уже полностью решённый блок (sequenceNumber == реальный номер блока receipt, "
            "без исключений). Прямых свидетельств 'продолжающегося приёма после broadcast' НЕ найдено -- "
            "530мс отставания RPC от фида, по всей видимости, ИЛЛЮЗИЯ чистой задержки индексации на "
            "стороне RPC-провайдера, а НЕ реальное окно приёма секвенсера. Реальный релевантный бюджет -- "
            "это ТЕМП блока ПО ФИДУ (см. achieved_feed_block_interval_ms), а не 530мс разрыв с RPC."
        )
    elif frac_delta_zero is not None and frac_delta_zero > 0.95:
        verdict = (
            f"delta_blocks == 0 для {frac_delta_zero:.1%} случаев -- подавляющее большинство подтверждает "
            f"атомарность блока, но есть {len(delta_values) - n_delta_zero} реальных исключений -- см. "
            f"late_insertion_cases для деталей (окно приёма НЕ равно нулю в этих случаях, но редко)."
        )
    else:
        verdict = (
            f"delta_blocks == 0 только для {frac_delta_zero:.1%} случаев -- заметная доля транзакций "
            f"реально попадает в блок, ОТЛИЧНЫЙ от того, где мы их впервые увидели в фиде -- это ПРЯМОЕ "
            f"свидетельство реального окна приёма после broadcast. См. late_insertion_cases."
        )

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_duration_s": args.duration_seconds,
        "rpc_endpoint_used_only_for_block_number_lookup": RPC_ENDPOINT,
        "feed_diag": client.diag,
        "n_feed_messages": len(feed_messages),
        "n_unique_tx_hashes_seen_in_feed": len(all_tx_candidates),
        "achieved_feed_block_interval_ms": {
            "note": "Интервал t_wall между ПОСЛЕДОВАТЕЛЬНЫМИ (seq+1) сообщениями фида -- реальный темп "
                    "блока ПО ФИДУ, RPC не используется как часы нигде в этом расчёте.",
            "n_consecutive_pairs": len(consecutive_intervals_ms),
            "n_sequence_gaps_excluded": seq_gaps,
            "stats": summarize(consecutive_intervals_ms),
        },
        "delta_blocks_diagnostic": {
            "note": "delta_blocks = receipt.blockNumber - sequenceNumber сообщения, из которого извлечена "
                    "транзакция. 0 = блок уже был полностью решён в момент broadcast (атомарность "
                    "подтверждена). !=0 = реальное позднее включение (окно приёма существует).",
            "n_sampled": len(sampled),
            "n_receipt_resolved": len(resolved),
            "n_receipt_not_found_or_error": len(sampled) - len(resolved),
            "n_delta_zero": n_delta_zero,
            "frac_delta_zero": frac_delta_zero,
            "histogram": dict(sorted(delta_histogram.items(), key=lambda kv: int(kv[0]))),
        },
        "late_insertion_cases": late_insertion_cases,
        "verdict": verdict,
    }

    out_path = Path(__file__).resolve().parent.parent / "data" / "task5_sequencer_window_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[sequencer_window] написано {out_path}", file=sys.stderr)
    print(json.dumps({
        "achieved_feed_block_interval_ms": result["achieved_feed_block_interval_ms"]["stats"],
        "delta_blocks_diagnostic": {k: v for k, v in result["delta_blocks_diagnostic"].items() if k != "histogram"},
        "delta_blocks_histogram": result["delta_blocks_diagnostic"]["histogram"],
        "verdict": verdict,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
