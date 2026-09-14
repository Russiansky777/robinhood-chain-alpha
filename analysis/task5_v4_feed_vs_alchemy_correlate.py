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
    ]

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    (OUT_DIR / "correlation_result.json").write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
