#!/usr/bin/env python3
"""Задача 5, живой бот -- снять противоречие: отдаёт ли фид секвенсера
транзакции ДО сборки блока или уже собранный блок целиком.

Владелец (заказчик), 2026-09-17, дословно (сокращённо): "Мы измерили
медианный интервал между сообщениями фида 97.3мс при темпе блока
~100-101мс -- похоже на 'одно сообщение = один готовый блок', то есть мы
узнаём о событии ПОСЛЕ закрытия блока. Но в данных по цепи есть 1452
сделки в блоке 0 -- попасть в уже закрытый блок физически нельзя.
Значит либо фид отдаёт транзакции ДО сборки, либо у конкурентов другой
вход, либо они вообще не реагируют (см. паттерн 8 'циклов' через один
пул, два постоянных адреса, ровные выплаты)."

Метод (буквально по заданию):
1. Подписаться на wss://feed.mainnet.chain.robinhood.com (тот же
   `SequencerFeedClient`/`decode_l2_message`, что и живой бот -- НЕ
   переписаны). Для каждого сообщения: t_wall, sequenceNumber,
   ДОПОЛНИТЕЛЬНО tx_hash каждой под-транзакции (keccak256 сырых байт
   подписанной tx -- decode_l2_message() САМ не возвращает tx_hash,
   здесь -- отдельный лёгкий разбор batch-структуры, копирующий уже
   проверенную логику `_decode_message_bytes`, только собирающий байты
   для хэширования, а не сами поля to/data).
2. ПАРАЛЛЕЛЬНО (отдельный поток, не блокирует asyncio-цикл фида) --
   опрос eth_blockNumber в максимально плотном ритме, какой реально
   выдерживает эндпоинт (честно замеряется по ходу, НЕ гарантируется
   ровно 20мс -- если round-trip самого запроса больше 20мс, цикл не
   может быть плотнее одного round-trip'а; реальный достигнутый
   интервал -- часть отчёта, не подгоняется под целевое число).
3. Для выборки до 300 tx_hash из фида -- eth_getTransactionReceipt,
   сравнить: t_wall получения из фида VS. время первого наблюдения
   ЭТОГО blockNumber нашим RPC-поллером. Разница со знаком:
   diff_ms = feed_t_wall_ms - block_first_seen_ms.
   Отрицательная = фид раньше блока (форы есть). Положительная/~0 =
   форы нет.
4. Доп., дёшево: распределение числа под-транзакций на сообщение фида
   (по одной -> поток транзакций; пачками -> блоки). Поиск полей с
   "block" в названии ГДЕ УГОДНО во вложенной структуре сообщения (не
   только известного header) -- если такое поле есть И появляется в
   фиде РАНЬШЕ, чем то же значение блока видно через RPC-поллер --
   прямое доказательство, что фид объявляет номер блока заранее.

Длительность -- `--duration-seconds` (по умолчанию 240с = 4 минуты, в
запрошенном диапазоне 3-5 минут). Никаких реальных транзакций не
отправляется, только чтение (фид + eth_blockNumber + eth_getTransactionReceipt)."""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests
from eth_utils import keccak

from task5_bot_config import RPC_URL_MAINNET, SEQUENCER_FEED_URL_MAINNET
from task5_bot_feed_client import FeedMessage, SequencerFeedClient, _decode_l2_msg_bytes
from config import CONFIG

RECEIPT_SAMPLE_TARGET = 300
BLOCK_POLL_TARGET_INTERVAL_S = 0.020  # запрошенная владельцем цель -- см. докстринг про честность реального интервала


def _resolve_rpc_endpoint() -> str:
    if CONFIG.alchemy_rpc_url:
        return CONFIG.alchemy_rpc_url
    if CONFIG.alchemy_api_key:
        return f"https://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}"
    return RPC_URL_MAINNET


RPC_ENDPOINT = _resolve_rpc_endpoint()


def _extract_tx_bytes_list(raw: bytes, _depth: int = 0) -> list[bytes]:
    """Копия обхода batch-структуры из `task5_bot_feed_client.
    _decode_message_bytes`, но собирает СЫРЫЕ байты каждой type-4
    под-транзакции (для хэширования), а не декодированные поля --
    та же, уже проверенная логика структуры (тип 3 = batch, рекурсивно
    разворачиваем [uint64 big-endian длина][под-сообщение]; тип 4 =
    подписанная транзакция, байты после префикса типа -- ровно то,
    что канонически хэшируется как tx_hash)."""
    if not raw:
        return []
    msg_type = raw[0]
    if msg_type == 3:
        if _depth > 4:
            return []
        out: list[bytes] = []
        pos = 1
        while pos + 8 <= len(raw):
            size = int.from_bytes(raw[pos:pos + 8], "big")
            pos += 8
            if size < 0 or pos + size > len(raw):
                break
            out.extend(_extract_tx_bytes_list(raw[pos:pos + size], _depth=_depth + 1))
            pos += size
        return out
    if msg_type != 4:
        return []
    return [raw[1:]]


def _find_block_like_fields(obj, path: str = "", out: list | None = None) -> list:
    """Рекурсивный поиск ЛЮБОГО ключа, содержащего 'block' (без учёта
    регистра), где угодно во вложенной структуре сообщения -- честно,
    без предположения заранее, что такого поля нет/есть."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_path = f"{path}.{k}" if path else k
            if "block" in str(k).lower():
                out.append({"path": new_path, "value": v if not isinstance(v, (dict, list)) else "<nested>"})
            _find_block_like_fields(v, new_path, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):  # честно ограничиваем -- длинные списки (напр. transactions) не нужны целиком
            _find_block_like_fields(v, f"{path}[{i}]", out)
    return out


class InspectingFeedClient(SequencerFeedClient):
    """Тот же `_consume`, что в базовом классе (см. `task5_bot_feed_client.
    SequencerFeedClient._consume`) -- НЕ переписан, только добавлен захват
    СЫРОГО словаря `m` (до извлечения в `FeedMessage`) для первых
    `raw_sample_limit` сообщений, чтобы честно поискать любые
    'block'-подобные поля во внешнем JSON-конверте (п.6 задания) -- этого
    нет в `FeedMessage`, который сохраняет только уже извлечённые
    sequenceNumber/l2Msg/timestamp."""

    def __init__(self, *args, raw_sample_limit: int = 5, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.raw_sample_limit = raw_sample_limit
        self.raw_samples: list[dict] = []

    async def _consume(self, ws, on_message) -> None:
        async for raw in ws:
            t_wall = time.time()
            self.diag["n_messages_total"] += 1
            try:
                payload = json.loads(raw)
            except Exception:
                self.diag["n_unparsed"] += 1
                continue
            msgs = payload.get("messages") if isinstance(payload, dict) else None
            if not msgs:
                if "confirmedSequenceNumberMessage" in (payload or {}):
                    self.diag["n_confirmation_only"] += 1
                continue
            for m in msgs:
                seq = m.get("sequenceNumber")
                if seq is None:
                    continue
                self.diag["n_with_seq"] += 1
                if len(self.raw_samples) < self.raw_sample_limit:
                    self.raw_samples.append(m)
                l2_msg_hex = self._extract_l2_msg_hex(m)
                seq_ts = self._extract_sequencer_timestamp(m)
                on_message(FeedMessage(t_wall=t_wall, sequence_number=seq, raw_l2_msg_hex=l2_msg_hex,
                                        sequencer_timestamp=seq_ts))


class BlockNumberPoller:
    """Плотный опрос eth_blockNumber в отдельном потоке -- НЕ через
    общий throttled-путь `alchemy_fallback` (его встроенный минимум
    ~100мс между запросами не даст приблизиться к запрошенным ~20мс) --
    прямой HTTP с keep-alive сессией, честный self-adaptive backoff
    ТОЛЬКО на реальную ошибку (429/5xx/сетевой сбой), не на пустом
    месте. Реальный достигнутый интервал -- то, что получилось, не то,
    что заказано."""

    def __init__(self, rpc_url: str) -> None:
        self.rpc_url = rpc_url
        self.session = requests.Session()
        self.first_seen: dict[int, float] = {}
        self.poll_intervals_ms: list[float] = []
        self.poll_round_trip_ms: list[float] = []
        self.n_polls = 0
        self.n_errors = 0
        self.errors_sample: list[str] = []
        self._stop = threading.Event()
        self._backoff_s = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self, duration_s: float) -> None:
        t_end = time.monotonic() + duration_s
        t_prev_start = None
        while not self._stop.is_set() and time.monotonic() < t_end:
            t0 = time.monotonic()
            if t_prev_start is not None:
                self.poll_intervals_ms.append((t0 - t_prev_start) * 1000.0)
            t_prev_start = t0
            try:
                resp = self.session.post(
                    self.rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                    timeout=5.0,
                )
                t1 = time.monotonic()
                self.poll_round_trip_ms.append((t1 - t0) * 1000.0)
                self.n_polls += 1
                if resp.status_code == 200:
                    body = resp.json()
                    if "result" in body:
                        block_num = int(body["result"], 16)
                        t_wall = time.time()
                        if block_num not in self.first_seen:
                            self.first_seen[block_num] = t_wall
                        self._backoff_s = 0.0
                    elif "error" in body:
                        self.n_errors += 1
                        if len(self.errors_sample) < 10:
                            self.errors_sample.append(str(body["error"]))
                elif resp.status_code == 429 or 500 <= resp.status_code < 600:
                    self.n_errors += 1
                    self._backoff_s = min(max(self._backoff_s * 2, 0.05), 2.0)
                    if len(self.errors_sample) < 10:
                        self.errors_sample.append(f"HTTP {resp.status_code}")
            except Exception as exc:  # noqa: BLE001
                self.n_errors += 1
                self._backoff_s = min(max(self._backoff_s * 2, 0.05), 2.0)
                if len(self.errors_sample) < 10:
                    self.errors_sample.append(str(exc))
            elapsed = time.monotonic() - t0
            remaining_target = BLOCK_POLL_TARGET_INTERVAL_S - elapsed
            sleep_s = max(remaining_target, 0.0) + self._backoff_s
            if sleep_s > 0:
                time.sleep(sleep_s)


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration-seconds", type=float, default=240.0)
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    print(f"[feed_vs_block] RPC endpoint (для поллера и receipt-lookup): {RPC_ENDPOINT}", file=sys.stderr)

    poller = BlockNumberPoller(RPC_ENDPOINT)
    poller_thread = threading.Thread(target=poller.run, args=(args.duration_seconds + 15.0,), daemon=True)

    feed_messages: list[dict] = []

    client = InspectingFeedClient(SEQUENCER_FEED_URL_MAINNET)

    def on_message(msg: FeedMessage) -> None:
        n_sub_total = 0
        n_sub_type4 = 0
        tx_hashes: list[str] = []
        if msg.raw_l2_msg_hex:
            raw_bytes = _decode_l2_msg_bytes(msg.raw_l2_msg_hex)
            if raw_bytes:
                tx_bytes_list = _extract_tx_bytes_list(raw_bytes)
                n_sub_type4 = len(tx_bytes_list)
                for tb in tx_bytes_list:
                    if tb:
                        tx_hashes.append("0x" + keccak(tb).hex())
                # честная оценка n_sub_total (включая нераспознанные типы) --
                # повторный, отдельный проход тем же алгоритмом обхода batch,
                # но считающий ВСЕ под-сообщения, не только type=4.
                n_sub_total = _count_all_submessages(raw_bytes)
        feed_messages.append({
            "t_wall": msg.t_wall,
            "sequence_number": msg.sequence_number,
            "sequencer_timestamp": msg.sequencer_timestamp,
            "n_sub_total": n_sub_total,
            "n_sub_type4_tx": n_sub_type4,
            "tx_hashes": tx_hashes,
        })

    poller_thread.start()
    t_start_wall = time.time()

    async def run_feed() -> None:
        listen_task = asyncio.create_task(client.listen(on_message))
        await asyncio.sleep(args.duration_seconds)
        listen_task.cancel()
        try:
            await listen_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    asyncio.run(run_feed())
    poller.stop()
    poller_thread.join(timeout=5.0)

    print(f"[feed_vs_block] фид: {len(feed_messages)} сообщений, поллер: {poller.n_polls} опросов, "
          f"{len(poller.first_seen)} уникальных блоков, {poller.n_errors} ошибок поллера", file=sys.stderr)

    # --- п.4: распределение под-транзакций на сообщение ---
    n_sub_type4_list = [m["n_sub_type4_tx"] for m in feed_messages]
    n_sub_total_list = [m["n_sub_total"] for m in feed_messages]

    # --- п.3: сопоставление tx_hash -> receipt.blockNumber -> block_first_seen ---
    all_tx_candidates: list[tuple[str, float]] = []
    for m in feed_messages:
        for h in m["tx_hashes"]:
            all_tx_candidates.append((h, m["t_wall"]))
    step = max(1, len(all_tx_candidates) // RECEIPT_SAMPLE_TARGET) if all_tx_candidates else 1
    sampled = all_tx_candidates[::step][:RECEIPT_SAMPLE_TARGET]

    print(f"[feed_vs_block] выборка для eth_getTransactionReceipt: {len(sampled)} из {len(all_tx_candidates)} "
          f"уникальных tx-хэшей, увиденных в фиде", file=sys.stderr)

    receipt_session = requests.Session()
    diffs_ms: list[float] = []
    n_receipt_not_found = 0
    n_block_not_in_poller = 0
    per_tx_detail: list[dict] = []
    for i, (tx_hash, feed_t_wall) in enumerate(sampled):
        try:
            resp = receipt_session.post(
                RPC_ENDPOINT,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_hash]},
                timeout=10.0,
            )
            body = resp.json()
            result = body.get("result")
        except Exception as exc:  # noqa: BLE001
            per_tx_detail.append({"tx_hash": tx_hash, "error": str(exc)})
            continue
        if not result:
            n_receipt_not_found += 1
            per_tx_detail.append({"tx_hash": tx_hash, "receipt_found": False})
            continue
        block_num = int(result["blockNumber"], 16)
        block_first_seen = poller.first_seen.get(block_num)
        if block_first_seen is None:
            n_block_not_in_poller += 1
            per_tx_detail.append({"tx_hash": tx_hash, "receipt_found": True, "block_number": block_num,
                                   "block_first_seen_by_poller": False})
            continue
        diff_ms = (feed_t_wall - block_first_seen) * 1000.0
        diffs_ms.append(diff_ms)
        per_tx_detail.append({"tx_hash": tx_hash, "receipt_found": True, "block_number": block_num,
                               "block_first_seen_by_poller": True, "feed_t_wall": feed_t_wall,
                               "block_first_seen_wall": block_first_seen, "diff_ms": diff_ms})
        if (i + 1) % 20 == 0:
            print(f"[feed_vs_block] receipt lookup {i + 1}/{len(sampled)}...", file=sys.stderr)

    def summarize(values: list[float]) -> dict:
        if not values:
            return {"n": 0}
        sv = sorted(values)
        return {
            "n": len(values), "median_ms": statistics.median(values),
            "p90_ms": sv[max(0, int(round(0.9 * (len(sv) - 1))))],
            "min_ms": min(values), "max_ms": max(values), "mean_ms": statistics.fmean(values),
        }

    n_feed_before_block = sum(1 for d in diffs_ms if d < 0)

    # --- п.6: поиск block-подобных полей во внешнем JSON-конверте фида ---
    # Реальные первые до 5 сырых сообщений (client.raw_samples, захвачены
    # InspectingFeedClient._consume ДО извлечения в FeedMessage) -- рекурсивный
    # поиск любого ключа, содержащего "block", честно на реальной структуре,
    # не предполагая заранее, что там есть/нет такое поле.
    block_like_fields_found: list[dict] = []
    for sample in client.raw_samples:
        found = _find_block_like_fields(sample)
        if found:
            block_like_fields_found.append({"sequence_number": sample.get("sequenceNumber"), "fields": found})
    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_duration_s": args.duration_seconds,
        "feed_url": SEQUENCER_FEED_URL_MAINNET,
        "rpc_endpoint": RPC_ENDPOINT,
        "feed_diag": client.diag,
        "n_feed_messages": len(feed_messages),
        "n_unique_tx_hashes_seen_in_feed": len(all_tx_candidates),
        "block_poller": {
            "n_polls": poller.n_polls,
            "n_unique_blocks_seen": len(poller.first_seen),
            "n_errors": poller.n_errors,
            "errors_sample": poller.errors_sample,
            "achieved_poll_interval_ms": summarize(poller.poll_intervals_ms),
            "achieved_round_trip_ms": summarize(poller.poll_round_trip_ms),
            "note": "Реальный достигнутый интервал опроса -- см. achieved_poll_interval_ms; "
                    "целевые ~20мс из задания НЕ гарантированы, если round-trip самого запроса "
                    "к RPC больше -- honest report, не подгонка под цель.",
        },
        "message_batch_size_distribution": {
            "n_sub_type4_tx_per_message": summarize([float(x) for x in n_sub_type4_list]),
            "n_sub_total_per_message": summarize([float(x) for x in n_sub_total_list]),
            "histogram_n_sub_type4_tx": _histogram(n_sub_type4_list),
        },
        "feed_vs_block_timing": {
            "n_sampled_for_receipt_lookup": len(sampled),
            "n_receipt_not_found": n_receipt_not_found,
            "n_block_not_seen_by_poller": n_block_not_in_poller,
            "n_diffs_computed": len(diffs_ms),
            "diff_ms_stats": summarize(diffs_ms),
            "n_feed_before_block_strictly_negative": n_feed_before_block,
            "frac_feed_before_block": (n_feed_before_block / len(diffs_ms)) if diffs_ms else None,
            "interpretation": "diff_ms = feed_t_wall_ms - block_first_seen_ms. Отрицательное = фид "
                               "раньше блока (форы есть). Положительное/~0 = форы нет.",
        },
        "per_tx_detail_sample": per_tx_detail[:50],
        "block_like_field_search": {
            "n_raw_samples_inspected": len(client.raw_samples),
            "findings": block_like_fields_found,
            "note": "Рекурсивный поиск любого ключа, содержащего 'block' (без учёта регистра), "
                    "в первых нескольких сырых сообщениях фида (до извлечения в FeedMessage). "
                    "Пусто -- значит такого поля реально не найдено в осмотренных сэмплах, не "
                    "то же самое, что 'поля точно нет нигде' (осмотрено n_raw_samples_inspected "
                    "сообщений, не все).",
        },
        "raw_message_samples": client.raw_samples,
    }

    out_path = Path(args.out) if args.out else Path(__file__).resolve().parent.parent / "data" / "task5_feed_vs_block_timing_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[feed_vs_block] написано {out_path}", file=sys.stderr)
    print(json.dumps({
        "diff_ms_stats": result["feed_vs_block_timing"]["diff_ms_stats"],
        "frac_feed_before_block": result["feed_vs_block_timing"]["frac_feed_before_block"],
        "n_diffs_computed": result["feed_vs_block_timing"]["n_diffs_computed"],
        "achieved_poll_interval_ms": result["block_poller"]["achieved_poll_interval_ms"],
        "n_sub_type4_tx_per_message": result["message_batch_size_distribution"]["n_sub_type4_tx_per_message"],
    }, indent=2, ensure_ascii=False))


def _count_all_submessages(raw: bytes, _depth: int = 0) -> int:
    if not raw:
        return 0
    msg_type = raw[0]
    if msg_type == 3:
        if _depth > 4:
            return 0
        count = 0
        pos = 1
        while pos + 8 <= len(raw):
            size = int.from_bytes(raw[pos:pos + 8], "big")
            pos += 8
            if size < 0 or pos + size > len(raw):
                break
            count += _count_all_submessages(raw[pos:pos + size], _depth=_depth + 1)
            pos += size
        return count
    return 1


def _histogram(values: list[int]) -> dict:
    hist: dict[str, int] = {}
    for v in values:
        key = str(v)
        hist[key] = hist.get(key, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: int(kv[0])))


if __name__ == "__main__":
    main()
