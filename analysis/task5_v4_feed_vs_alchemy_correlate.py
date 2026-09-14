#!/usr/bin/env python3
"""Корреляция уже собранных JSONL из task5_v4_feed_vs_alchemy_measurement.py
-- НЕ сетевой скрипт, можно перезапускать сколько угодно без cooldown.

Сопоставление ТОЛЬКО по реальному tx_hash (для pending-tx/log) -- не по
sequenceNumber/blockNumber. Стартовый бэклог (is_backlog=true) ИСКЛЮЧЁН
из сопоставления, но его объём показан отдельно, не скрыт. Пропуски
(хэш виден только в одном источнике) -- отдельные честные счётчики, не
исключены из выборки молча. Разница НЕ называется "задержкой от
секвенсора" и не доказывает "раньше получено -- раньше исполнено
секвенсором" -- это разница между НАШИМИ источниками наблюдения на
одном сервере, тем же монотонным таймером, что и запись."""
from __future__ import annotations

import json
from pathlib import Path

OUT_DIR = Path(__file__).parent.parent / "data" / "task5_v4_feed_vs_alchemy_measurement"


def pctl(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


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


def first_seen_by_hash(msgs: list[dict], hash_field: str = "tx_hash",
                        kind_filter: str | None = None) -> tuple[dict[str, float], int]:
    """Возвращает (первое t_monotonic по хэшу СРЕДИ НЕ-бэклога, число
    исключённых как бэклог записей) -- бэклог не подменяет живые данные
    молча, просто не участвует в сопоставлении."""
    by_hash: dict[str, float] = {}
    n_backlog = 0
    for m in msgs:
        if kind_filter is not None and m.get("kind") != kind_filter:
            continue
        h = m.get(hash_field)
        if not h:
            continue
        if m.get("is_backlog"):
            n_backlog += 1
            continue
        if h not in by_hash:
            by_hash[h] = m["t_monotonic"]
    return by_hash, n_backlog


def group_feed_by_sequence(feed_msgs: list[dict]) -> dict[int, dict]:
    """Одно сообщение фида (sequenceNumber) = один L2-блок (документированное
    предположение Nitro-протокола, task5_bot_feed_client.py). Возвращает
    seq -> {"t_monotonic": ПЕРВОЕ время прихода этой пачки, "tx_hashes":
    set(...), "is_backlog": bool}. Бэклог (is_backlog=true) группируется
    отдельно и НЕ участвует в сопоставлении (см. main()), но не отбрасывается
    молча -- считается отдельно."""
    groups: dict[int, dict] = {}
    for m in feed_msgs:
        seq = m.get("sequence_number")
        if seq is None:
            continue
        g = groups.setdefault(seq, {"t_monotonic": m["t_monotonic"], "tx_hashes": set(), "is_backlog": False})
        g["t_monotonic"] = min(g["t_monotonic"], m["t_monotonic"])
        if m.get("is_backlog"):
            g["is_backlog"] = True
        h = m.get("tx_hash")
        if h:
            g["tx_hashes"].add(h)
    return groups


def build_tx_hash_to_block_hash(alchemy_msgs: list[dict]) -> dict[str, str]:
    """ТОЛЬКО из Alchemy Swap-логов (topic0 отфильтрован на подписке, п.5) --
    tx_hash -> blockHash. Первое увиденное значение побеждает (для честности
    порядка, не имеет значения для самого маппинга -- один tx лежит в одном
    блоке)."""
    out: dict[str, str] = {}
    for m in alchemy_msgs:
        if m.get("kind") != "log":
            continue
        h, bh = m.get("tx_hash"), m.get("block_hash")
        if h and bh and h not in out:
            out[h] = bh
    return out


def first_swap_log_time_by_block_hash(alchemy_msgs: list[dict]) -> dict[str, float]:
    """block_hash -> МИНИМАЛЬНОЕ t_monotonic среди Swap-логов этого блока
    ("приход ПЕРВОГО Swap-лога этого блока через Alchemy", владелец, п.5)."""
    out: dict[str, float] = {}
    for m in alchemy_msgs:
        if m.get("kind") != "log":
            continue
        bh = m.get("block_hash")
        if not bh:
            continue
        t = m["t_monotonic"]
        if bh not in out or t < out[bh]:
            out[bh] = t
    return out


def correlate_feed_blocks_vs_alchemy_swap_logs(feed_msgs: list[dict], alchemy_msgs: list[dict]) -> dict:
    """ГЛАВНАЯ метрика этого замера (владелец, п.5), названа ЯВНО: "приход
    блока в feed -> приход первого Swap-лога этого блока через Alchemy".
    Это разные стадии обработки (получение батча транзакций фидом vs.
    получение уже вычисленного события лога через Alchemy), НЕ чистое
    сравнение сетевых каналов -- см. caveats. Считаются ТОЛЬКО блоки,
    реально содержащие Swap-событие (подписка Alchemy уже отфильтрована по
    topic0=Swap V4 PoolManager) -- "не Swap-блоки" не в знаменателе."""
    tx_to_block_hash = build_tx_hash_to_block_hash(alchemy_msgs)
    first_swap_log_time = first_swap_log_time_by_block_hash(alchemy_msgs)
    feed_groups = group_feed_by_sequence(feed_msgs)

    feed_block_hash_by_seq: dict[int, str] = {}
    n_backlog_seq_excluded = 0
    n_seq_no_matching_tx = 0
    n_seq_multiple_block_hashes_anomaly = 0
    anomaly_examples = []
    for seq, g in feed_groups.items():
        if g["is_backlog"]:
            n_backlog_seq_excluded += 1
            continue
        found = {tx_to_block_hash[h] for h in g["tx_hashes"] if h in tx_to_block_hash}
        if not found:
            n_seq_no_matching_tx += 1
            continue
        if len(found) > 1:
            # Нарушение предположения "одно сообщение фида = один L2-блок" --
            # честно фиксируется, НЕ угадывается какой из block_hash "правильный".
            n_seq_multiple_block_hashes_anomaly += 1
            if len(anomaly_examples) < 5:
                anomaly_examples.append({"sequence_number": seq, "block_hashes_found": sorted(found)})
            continue
        feed_block_hash_by_seq[seq] = next(iter(found))

    feed_time_by_block_hash: dict[str, float] = {}
    for seq, bh in feed_block_hash_by_seq.items():
        t = feed_groups[seq]["t_monotonic"]
        if bh not in feed_time_by_block_hash or t < feed_time_by_block_hash[bh]:
            feed_time_by_block_hash[bh] = t

    matched_block_hashes = sorted(set(feed_time_by_block_hash) & set(first_swap_log_time))
    only_feed_no_swap_log_seen = sorted(set(feed_time_by_block_hash) - set(first_swap_log_time))
    only_alchemy_no_feed_block = sorted(set(first_swap_log_time) - set(feed_time_by_block_hash))

    deltas = [first_swap_log_time[bh] - feed_time_by_block_hash[bh] for bh in matched_block_hashes]
    n_feed_first = sum(1 for d in deltas if d > 0)
    n_alchemy_first = sum(1 for d in deltas if d < 0)
    n_ties = sum(1 for d in deltas if d == 0)

    return {
        "metric_name": "приход блока в feed -> приход первого Swap-лога этого блока через Alchemy",
        "n_feed_sequence_groups_total": len(feed_groups),
        "n_feed_sequence_groups_backlog_excluded": n_backlog_seq_excluded,
        "n_feed_sequence_groups_no_matching_tx_in_alchemy_swap_logs": n_seq_no_matching_tx,
        "n_feed_sequence_groups_multiple_block_hashes_anomaly": n_seq_multiple_block_hashes_anomaly,
        "anomaly_examples": anomaly_examples,
        "n_matched_blocks_with_swap_event": len(matched_block_hashes),
        "n_only_in_feed_no_alchemy_swap_log_for_that_block": len(only_feed_no_swap_log_seen),
        "n_only_in_alchemy_swap_log_no_matching_feed_block": len(only_alchemy_no_feed_block),
        "median_delta_s": pctl(deltas, 0.5), "p95_delta_s": pctl(deltas, 0.95), "p99_delta_s": pctl(deltas, 0.99),
        "min_delta_s": min(deltas) if deltas else None, "max_delta_s": max(deltas) if deltas else None,
        "win_share_feed_first": (n_feed_first / len(deltas)) if deltas else None,
        "win_share_alchemy_swap_log_first": (n_alchemy_first / len(deltas)) if deltas else None,
        "n_ties": n_ties,
        "delta_convention": "delta = t_alchemy_first_swap_log - t_feed_block_arrival; "
                             "положительное -- фид пришёл раньше первого Swap-лога Alchemy",
        "caveat": (
            "Это НЕ сравнение двух одинаковых стадий: фид отдаёт СЫРЫЕ входные данные для "
            "исполнения (батч транзакций блока), Alchemy log -- УЖЕ вычисленное событие после "
            "локального исполнения на стороне Alchemy. Приход блока в feed НЕ означает, что "
            "состояние пулов уже пересчитано на нашей стороне."
        ),
    }


def compare(name_a: str, a: dict[str, float], name_b: str, b: dict[str, float]) -> dict:
    """a/b: hash -> t_monotonic. Считает медиану/p95/p99 разницы (b-a),
    долю побед каждой стороны, и ЧЕСТНО -- пропуски (хэш только у одной
    стороны), не исключённые из общего счёта."""
    common = sorted(set(a) & set(b))
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    deltas = [b[h] - a[h] for h in common]
    n_a_wins = sum(1 for d in deltas if d > 0)   # a раньше b
    n_b_wins = sum(1 for d in deltas if d < 0)   # b раньше a
    n_ties = sum(1 for d in deltas if d == 0)
    return {
        f"n_only_in_{name_a}": len(only_a), f"n_only_in_{name_b}": len(only_b),
        "n_matched": len(common),
        "median_delta_s": pctl(deltas, 0.5), "p95_delta_s": pctl(deltas, 0.95), "p99_delta_s": pctl(deltas, 0.99),
        "min_delta_s": min(deltas) if deltas else None, "max_delta_s": max(deltas) if deltas else None,
        f"win_share_{name_a}_first": (n_a_wins / len(deltas)) if deltas else None,
        f"win_share_{name_b}_first": (n_b_wins / len(deltas)) if deltas else None,
        "n_ties": n_ties,
        "delta_convention": f"delta = t_{name_b} - t_{name_a}; положительное -- {name_a} раньше",
    }


def main() -> None:
    summary_path = OUT_DIR / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}

    feed_msgs = load_jsonl(OUT_DIR / "feed_messages.jsonl")
    alchemy_msgs = load_jsonl(OUT_DIR / "alchemy_messages.jsonl")
    blockrazor_msgs = load_jsonl(OUT_DIR / "blockrazor_messages.jsonl")

    feed_by_hash, feed_n_backlog = first_seen_by_hash(feed_msgs)
    pending_by_hash, _ = first_seen_by_hash(alchemy_msgs, kind_filter="pending_tx")
    log_by_hash, _ = first_seen_by_hash(alchemy_msgs, kind_filter="log")
    blockrazor_by_hash, blockrazor_n_backlog = first_seen_by_hash(blockrazor_msgs)

    # blockHash -- ключ для сопоставления НА УРОВНЕ БЛОКА, когда оно
    # уместно (не header.blockNumber Nitro-сообщений -- он может быть
    # номером L1). Пока реально доступен только у Alchemy logs.
    log_by_blockhash: dict[str, float] = {}
    n_logs_without_blockhash = 0
    for m in alchemy_msgs:
        if m.get("kind") != "log":
            continue
        bh = m.get("block_hash")
        if not bh:
            n_logs_without_blockhash += 1
            continue
        if bh not in log_by_blockhash:
            log_by_blockhash[bh] = m["t_monotonic"]

    result: dict = {
        "n_feed_unique_tx_hashes_live": len(feed_by_hash), "n_feed_backlog_excluded": feed_n_backlog,
        "n_alchemy_pending_unique_tx_hashes": len(pending_by_hash),
        "n_alchemy_log_unique_tx_hashes": len(log_by_hash),
        "n_alchemy_log_unique_block_hashes": len(log_by_blockhash),
        "n_alchemy_logs_without_block_hash_field": n_logs_without_blockhash,
        "n_blockrazor_unique_tx_hashes_live": len(blockrazor_by_hash), "n_blockrazor_backlog_excluded": blockrazor_n_backlog,
    }

    if feed_by_hash and pending_by_hash:
        result["feed_vs_alchemy_pending"] = compare("feed", feed_by_hash, "alchemy_pending", pending_by_hash)
    if feed_by_hash and log_by_hash:
        result["feed_vs_alchemy_log"] = compare("feed", feed_by_hash, "alchemy_log", log_by_hash)
    # ГЛАВНАЯ метрика этого замера (владелец, п.5/6) -- сопоставление по
    # blockHash, только блоки с реальным Swap-событием, явно поимённая
    # метрика (не "сравнение каналов").
    if feed_msgs and alchemy_msgs:
        result["feed_block_arrival_vs_first_swap_log"] = correlate_feed_blocks_vs_alchemy_swap_logs(
            feed_msgs, alchemy_msgs)
    if feed_by_hash and blockrazor_by_hash:
        result["feed_vs_blockrazor"] = compare("feed", feed_by_hash, "blockrazor", blockrazor_by_hash)
    if blockrazor_by_hash and pending_by_hash:
        result["blockrazor_vs_alchemy_pending"] = compare("blockrazor", blockrazor_by_hash, "alchemy_pending", pending_by_hash)
    if not blockrazor_by_hash:
        result["blockrazor_note"] = "нет данных BlockRazor в этом прогоне (см. summary.blockrazor_diag -- токен отсутствовал/не подключался)"

    result["feed_diag"] = summary.get("feed_diag", {})
    result["alchemy_diag"] = summary.get("alchemy_diag", {})
    result["blockrazor_diag"] = summary.get("blockrazor_diag", {})
    fd = summary.get("feed_diag", {})
    if fd.get("connected_at_monotonic") is not None and fd.get("ended_at_monotonic") is not None:
        result["feed_actual_connection_lifetime_s"] = fd["ended_at_monotonic"] - fd["connected_at_monotonic"]
    bd = summary.get("blockrazor_diag", {})
    if bd.get("connected_at_monotonic") is not None and bd.get("ended_at_monotonic") is not None:
        result["blockrazor_actual_connection_lifetime_s"] = bd["ended_at_monotonic"] - bd["connected_at_monotonic"]

    result["caveats"] = [
        "Разница выше -- между НАШИМИ источниками наблюдения на ОДНОМ сервере с ОДНИМ монотонным "
        "таймером записи (сразу при получении, до разбора сообщения). Это НЕ абсолютная задержка от "
        "секвенсора и НЕ выведено из секундных timestamp блоков.",
        "'Раньше получено' НЕ равно 'раньше исполнено секвенсором' -- порядок получения на нашей "
        "стороне не доказывает порядок исполнения на стороне секвенсора.",
        "Время 'фид -> исполненное локальное состояние' (наш собственный узел/эмуляция исполнения) "
        "отдельно НЕ измерено -- локальной синхронизированной ноды нет.",
        "n_only_in_* -- реальные пропуски (хэш видел только один источник за время наблюдения), "
        "НЕ исключены из общего счёта, показаны явно.",
        "header.blockNumber сообщений Nitro-фида НЕ используется как номер L2-блока (может быть "
        "номером L1) -- сопоставление идёт по tx_hash/blockHash.",
        "feed_block_arrival_vs_first_swap_log -- ГЛАВНАЯ метрика (владелец, п.5/6): 'приход блока "
        "в feed -> приход первого Swap-лога этого блока через Alchemy'. Это НЕ чистое сравнение "
        "сетевых каналов -- разные стадии обработки (сырые входные данные фида vs. уже вычисленное "
        "Alchemy событие). Считаются ТОЛЬКО блоки с реальным Swap-событием (подписка Alchemy logs "
        "отфильтрована по topic0=Swap V4 PoolManager).",
        "Приход блока в feed НЕ означает, что состояние пулов уже пересчитано на нашей стороне -- "
        "фид несёт входные данные ДЛЯ исполнения, а не результат исполнения.",
    ]

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    (OUT_DIR / "correlation_result.json").write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
