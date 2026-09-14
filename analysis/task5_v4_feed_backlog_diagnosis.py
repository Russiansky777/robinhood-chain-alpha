#!/usr/bin/env python3
"""Задача 5 (владелец): разбор УЖЕ СОХРАНЁННЫХ 733 сообщений прошлого
замера -- БЕЗ нового подключения к фиду. Отвечает на конкретные пункты:

1. Первые/последние 5 сообщений: время получения, точный JSON-путь и
   единицы sequencer_timestamp, sequenceNumber, реальный blockHash (через
   RPC по сохранённому tx_hash -- в самих записях JSONL blockHash не
   сохранялся, только tx_hash), отставание timestamp от времени получения.
2. Монотонность sequenceNumber/timestamp по всему потоку (не только
   первым/последним 5), тренд отставания (уменьшается/нет).
3. Для нескольких сохранённых tx_hash -- реальный блок через RPC
   (eth_getTransactionByHash -> eth_getBlockByHash), сопоставление
   timestamp блока с sequencer_timestamp нашей группы. header.blockNumber
   Nitro-сообщений НЕ используется как номер L2-блока нигде в этом файле.
4. Честная оговорка точности: "время получения" -- это t_monotonic,
   переведённое в приблизительное wall-clock через HTTP Date-заголовок
   хендшейка (секундная точность) -- НЕ прямое измерение системных часов
   в момент каждого сообщения. Отдельная проверка часов Ohio --
   task5_v4_ohio_clock_check.py (сравнение с Date-заголовком RPC-ответа).
5. Не утверждает "всё это backlog" ТОЛЬКО по is_backlog (эвристика
   BACKLOG_THRESHOLD_S=10s, живой прогон) -- показывает и сырое отставание
   по каждой группе, и результат независимой RPC-проверки, честно
   помечая, где эвристика совпадает/расходится с реальным блоком."""
from __future__ import annotations

import json
import sys
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import _rpc_call, get_transaction_fast  # noqa: E402

OUT_DIR = Path(__file__).parent.parent / "data" / "task5_v4_feed_vs_alchemy_measurement"

# Реальный, локально измеренный (БЕЗ сети, task5_v4_feed_eventloop_diagnosis.py,
# eth_account.Account.recover_transaction на реально подписанной тестовой
# транзакции) стоимость decode+ecrecover ОДНОЙ транзакции -- используется
# ТОЛЬКО для оценки нагрузки на event loop по РЕАЛЬНО наблюдённому числу
# транзакций в кадре, не для утверждений о самих транзакциях фида.
MEASURED_DECODE_ECRECOVER_MS_PER_TX = 8.06


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def parse_http_date_to_epoch(date_str: str | None) -> float | None:
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str).timestamp()
    except Exception:
        return None


def main() -> None:
    summary_path = OUT_DIR / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    feed_diag = summary.get("feed_diag", {})

    date_header = feed_diag.get("handshake_response_headers", {}).get("Date")
    connected_at_monotonic = feed_diag.get("connected_at_monotonic")
    wall_offset = None
    if date_header and connected_at_monotonic is not None:
        wall_epoch = parse_http_date_to_epoch(date_header)
        if wall_epoch is not None:
            wall_offset = wall_epoch - connected_at_monotonic
    wall_offset_basis = (
        "monotonic->wall смещение реконструировано из HTTP 'Date' заголовка ответа "
        "хендшейка (секундная точность) и connected_at_monotonic ТОГО ЖЕ момента -- "
        "ПРИБЛИЗИТЕЛЬНОЕ (±1-2с: округление Date до секунды + задержка между TCP-accept "
        "и генерацией заголовка на сервере), НЕ точная синхронизация часов. Отдельная "
        "проверка часов Ohio -- task5_v4_ohio_clock_check.py."
    )

    records = load_jsonl(OUT_DIR / "feed_messages.jsonl")

    # --- Группировка по sequence_number (один L2-блок) ---
    groups: dict[int, dict] = {}
    for r in records:
        seq = r.get("sequence_number")
        if seq is None:
            continue
        g = groups.setdefault(seq, {
            "t_monotonic": r["t_monotonic"], "sequencer_timestamp": r.get("sequencer_timestamp"),
            "is_backlog_live_flag": r.get("is_backlog"), "tx_hashes": [],
        })
        g["t_monotonic"] = min(g["t_monotonic"], r["t_monotonic"])
        if r.get("tx_hash"):
            g["tx_hashes"].append(r["tx_hash"])
    ordered = sorted(groups.items(), key=lambda kv: kv[1]["t_monotonic"])

    def annotate(seq: int, g: dict) -> dict:
        wall_approx = (g["t_monotonic"] + wall_offset) if wall_offset is not None else None
        lag = (wall_approx - g["sequencer_timestamp"]) if (
            wall_approx is not None and g["sequencer_timestamp"] is not None) else None
        return {
            "sequence_number": seq,
            "t_monotonic": g["t_monotonic"],
            "sequencer_timestamp_raw": g["sequencer_timestamp"],
            "sequencer_timestamp_json_path": "messages[].message.message.header.timestamp (unix-СЕКУНДЫ, не мс)",
            "wall_clock_approx_at_receipt": wall_approx,
            "lag_wall_minus_sequencer_timestamp_s": lag,
            "is_backlog_flag_from_live_run": g["is_backlog_live_flag"],
            "n_tx_in_group": len(g["tx_hashes"]),
            "sample_tx_hash": g["tx_hashes"][0] if g["tx_hashes"] else None,
        }

    annotated_ordered = [annotate(seq, g) for seq, g in ordered]
    first_5 = annotated_ordered[:5]
    last_5 = annotated_ordered[-5:]

    # --- Монотонность по ВСЕМУ потоку, не только первым/последним 5 ---
    seqs = [seq for seq, _ in ordered]
    seq_ts_pairs = [(seq, g["sequencer_timestamp"]) for seq, g in ordered if g["sequencer_timestamp"] is not None]
    n_seq_decreases = sum(1 for i in range(len(seqs) - 1) if seqs[i + 1] < seqs[i])
    n_ts_decreases = sum(1 for i in range(len(seq_ts_pairs) - 1) if seq_ts_pairs[i + 1][1] < seq_ts_pairs[i][1])

    lags = [a["lag_wall_minus_sequencer_timestamp_s"] for a in annotated_ordered if
            a["lag_wall_minus_sequencer_timestamp_s"] is not None]
    lag_trend = None
    if len(lags) >= 10:
        first_10_avg = sum(lags[:10]) / 10
        last_10_avg = sum(lags[-10:]) / 10
        lag_trend = {
            "avg_lag_first_10_groups_s": first_10_avg, "avg_lag_last_10_groups_s": last_10_avg,
            "decreasing_over_connection_lifetime": last_10_avg < first_10_avg,
        }

    # --- Пакетирование по WS-кадрам (одинаковый t_monotonic = один recv()) ---
    by_frame: dict[float, dict] = {}
    for seq, g in ordered:
        fr = by_frame.setdefault(g["t_monotonic"], {"n_sequence_groups": 0, "n_tx": 0})
        fr["n_sequence_groups"] += 1
        fr["n_tx"] += len(g["tx_hashes"])
    frame_sizes_tx = sorted((f["n_tx"] for f in by_frame.values()), reverse=True)
    max_frame_tx = frame_sizes_tx[0] if frame_sizes_tx else 0
    frame_load_estimate = {
        "n_distinct_ws_frames_by_receipt_instant": len(by_frame),
        "max_tx_count_in_a_single_frame": max_frame_tx,
        "estimated_inline_decode_ecrecover_ms_for_largest_frame": (
            max_frame_tx * MEASURED_DECODE_ECRECOVER_MS_PER_TX),
        "estimation_basis": (
            f"ОЦЕНКА, не прямое измерение: реально наблюдённое число транзакций в самом "
            f"крупном кадре (по совпадению t_monotonic) x реально измеренная локально, "
            f"без сети, стоимость decode+ecrecover одной транзакции "
            f"({MEASURED_DECODE_ECRECOVER_MS_PER_TX:.2f} мс/tx, см. "
            "task5_v4_feed_eventloop_diagnosis.py). Сырые кадры этого прогона не "
            "сохранялись -- прямая реконструкция фактического времени обработки "
            "невозможна задним числом."
        ),
    }

    # --- RPC-сверка: несколько реальных tx_hash -> реальный блок ---
    rpc_cross_checks = []
    if ordered:
        sample_positions = sorted({0, len(ordered) // 2, len(ordered) - 1})
        for pos in sample_positions:
            seq, g = ordered[pos]
            if not g["tx_hashes"]:
                continue
            txh = g["tx_hashes"][0]
            entry: dict = {"sequence_number": seq, "sample_tx_hash": txh,
                            "sequencer_timestamp_for_this_group": g["sequencer_timestamp"]}
            try:
                tx = get_transaction_fast(txh)
                if not tx:
                    entry["rpc_result"] = "eth_getTransactionByHash вернул null -- узел RPC не знает эту транзакцию сейчас"
                else:
                    block_hash = tx.get("blockHash")
                    block_number_hex = tx.get("blockNumber")
                    entry["real_block_hash"] = block_hash
                    entry["real_block_number"] = int(block_number_hex, 16) if block_number_hex else None
                    if block_hash:
                        block = _rpc_call("eth_getBlockByHash", [block_hash, False])
                        if block and block.get("timestamp") is not None:
                            real_ts = int(block["timestamp"], 16)
                            entry["real_block_timestamp_unix"] = real_ts
                            if g["sequencer_timestamp"] is not None:
                                entry["diff_real_block_timestamp_minus_sequencer_timestamp_s"] = (
                                    real_ts - g["sequencer_timestamp"])
                        else:
                            entry["rpc_result"] = "eth_getBlockByHash вернул null/без timestamp"
            except Exception as exc:  # noqa: BLE001
                entry["rpc_error"] = f"{type(exc).__name__}: {exc}"
            rpc_cross_checks.append(entry)

    result = {
        "wall_offset_s_reconstructed": wall_offset,
        "wall_offset_basis": wall_offset_basis,
        "n_sequence_groups_total": len(ordered),
        "first_5_groups_by_receipt_order": first_5,
        "last_5_groups_by_receipt_order": last_5,
        "sequence_number_n_decreases_in_receipt_order": n_seq_decreases,
        "sequencer_timestamp_n_decreases_in_receipt_order": n_ts_decreases,
        "lag_trend": lag_trend,
        "frame_load_estimate": frame_load_estimate,
        "rpc_cross_checks_real_block_vs_sequencer_timestamp": rpc_cross_checks,
        "note_on_is_backlog_heuristic": (
            "is_backlog_flag_from_live_run был вычислен ВО ВРЕМЯ живого прогона по правилу "
            "(wall_time_at_processing - sequencer_timestamp) > 10s. Здесь для контроля "
            "показано и независимо реконструированное lag_wall_minus_sequencer_timestamp_s, "
            "и (где получилось) реальная сверка через RPC-блок -- НЕ утверждается, что все "
            "733 сообщения были backlog только на основании эвристики; см. числа выше."
        ),
    }
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    (OUT_DIR / "feed_backlog_diagnosis_result.json").write_text(
        json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
