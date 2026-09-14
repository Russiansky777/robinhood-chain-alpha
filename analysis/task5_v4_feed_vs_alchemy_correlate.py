#!/usr/bin/env python3
"""Корреляция уже собранных JSONL из task5_v4_feed_vs_alchemy_measurement.py
-- НЕ сетевой скрипт, можно перезапускать сколько угодно без cooldown.

Сопоставление ТОЛЬКО по реальному tx_hash (не по sequenceNumber/блоку).
Раздельно: фид -> Alchemy pending-tx (получение транзакции) и
фид -> Alchemy log (готовые логи). Разница НЕ называется "задержкой от
секвенсора" -- это разница между ДВУМЯ НАШИМИ источниками наблюдения на
одном сервере, тем же монотонным таймером, что и запись."""
from __future__ import annotations

import json
import sys
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


def main() -> None:
    summary_path = OUT_DIR / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}

    feed_msgs = load_jsonl(OUT_DIR / "feed_messages.jsonl")
    alchemy_msgs = load_jsonl(OUT_DIR / "alchemy_messages.jsonl")

    feed_by_hash: dict[str, float] = {}
    for m in feed_msgs:
        h = m.get("tx_hash")
        if h and h not in feed_by_hash:  # первое появление этого хэша на фиде
            feed_by_hash[h] = m["t_monotonic"]

    pending_by_hash: dict[str, float] = {}
    log_by_hash: dict[str, float] = {}
    for m in alchemy_msgs:
        h = m.get("tx_hash")
        if not h:
            continue
        if m.get("kind") == "pending_tx" and h not in pending_by_hash:
            pending_by_hash[h] = m["t_monotonic"]
        elif m.get("kind") == "log" and h not in log_by_hash:
            log_by_hash[h] = m["t_monotonic"]

    result: dict = {
        "n_feed_unique_tx_hashes": len(feed_by_hash),
        "n_alchemy_pending_unique_tx_hashes": len(pending_by_hash),
        "n_alchemy_log_unique_tx_hashes": len(log_by_hash),
    }

    for label, other in (("feed_to_alchemy_pending", pending_by_hash), ("feed_to_alchemy_log", log_by_hash)):
        matched = sorted(set(feed_by_hash) & set(other))
        deltas = [other[h] - feed_by_hash[h] for h in matched]
        result[label] = {
            "n_matched": len(matched),
            "median_delta_s": pctl(deltas, 0.5),
            "p95_delta_s": pctl(deltas, 0.95),
            "min_delta_s": min(deltas) if deltas else None,
            "max_delta_s": max(deltas) if deltas else None,
            "n_negative_delta": sum(1 for d in deltas if d < 0),
            "note": ("negative_delta означает, что Alchemy показал этот хэш РАНЬШЕ фида на этом же "
                     "сервере/таймере -- честно посчитано, не отброшено"),
        }

    result["feed_diag"] = summary.get("feed_diag", {})
    result["alchemy_diag"] = summary.get("alchemy_diag", {})
    fd = summary.get("feed_diag", {})
    if fd.get("connected_at_monotonic") is not None and fd.get("ended_at_monotonic") is not None:
        result["feed_actual_connection_lifetime_s"] = fd["ended_at_monotonic"] - fd["connected_at_monotonic"]

    result["caveat"] = (
        "Разница выше -- между ДВУМЯ нашими источниками наблюдения (фид и Alchemy WS) на ОДНОМ "
        "сервере с ОДНИМ монотонным таймером записи. Это НЕ абсолютная задержка от секвенсора и "
        "НЕ выведено из секундных timestamp блоков. Время 'фид -> исполненное локальное состояние' "
        "(наш собственный узел/эмуляция исполнения) отдельно НЕ измерено -- локальной синхронизированной "
        "ноды нет."
    )

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    (OUT_DIR / "correlation_result.json").write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
