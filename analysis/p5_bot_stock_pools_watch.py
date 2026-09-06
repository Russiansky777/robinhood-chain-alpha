#!/usr/bin/env python3
"""Дневной индикатор жизни линии «сток-токены Robinhood Chain» (владелец,
2026-09-06, п.2 переклассификации): "присутствие и доля 0x65050a...c40dc
в свопах пулов сток-токенов за последние сутки — по одному часовому
срезу, не полному скану. Ушёл или доля резко упала — строка в отчёт.
Дёшево, без Dune."

Реальная причина: линия переклассифицирована как "эксплуатация чужого
бота" (не рыночная неэффективность) — `docs/PROJECT_STATE.md`, N=217618
реальных свопов тёмного окна, известный P5-бот `0x65050a9b...c40dc` даёт
33.9% всех role-slot. Раз линия зависит от ЭТОГО КОНКРЕТНОГО бота — её
жизнеспособность = его присутствие, не абстрактная "неэффективность".

Метод (0 кредитов Dune, только eth_getLogs, тот же список из 33
реальных v3-пулов, что уже использован в `task1_darkwindow_trader_
probe.py` -- НЕ дублируется, импортируется оттуда): ОДИН часовой срез
(последний полный час до момента запуска), НЕ полный скан суток --
полный скан одного 13.5-часового тёмного окна уже реально не уложился
ни в 25, ни в 45 минут (см. паспорт, run 34049672943/34051087886) --
часовой срез на порядок дешевле и достаточен как индикатор
присутствия/доли, не как точная экономика."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task1_darkwindow_trader_probe import (  # noqa: E402
    KNOWN_P5_BOT, POOLS_BY_SYMBOL, fetch_window_swaps, find_block_by_timestamp,
)

OUT_PATH = Path("data/p5_bot_stock_pools_watch.jsonl")
# Порог тревоги: доля P5-бота упала более чем в 2 раза относительно
# ПРЕДЫДУЩЕЙ реально записанной строки (не абстрактный порог -- сравнение
# с реальной историей этого же индикатора).
DROP_ALERT_RATIO = 0.5


def summarize(swaps: list[dict]) -> dict:
    from collections import Counter
    n = len(swaps)
    both = Counter()
    for s in swaps:
        both[s["sender"]] += 1
        both[s["recipient"]] += 1
    self_trade = sum(1 for s in swaps if s["sender"] == s["recipient"])
    p5_hits = sum(1 for s in swaps if KNOWN_P5_BOT in (s["sender"], s["recipient"]))
    top3_share = sum(c for _, c in both.most_common(3)) / (2 * n) if n else None
    return {
        "n_swaps": n, "n_distinct_addresses": len(both),
        "self_trade_fraction": self_trade / n if n else None,
        "p5_bot_hits": p5_hits, "p5_bot_fraction": p5_hits / n if n else None,
        "top3_share_of_role_slots": top3_share,
    }


def load_previous() -> dict | None:
    if not OUT_PATH.exists():
        return None
    lines = [ln for ln in OUT_PATH.read_text().splitlines() if ln.strip()]
    if not lines:
        return None
    return json.loads(lines[-1])


def run() -> int:
    now = datetime.now(timezone.utc)
    hour_end = now.replace(minute=0, second=0, microsecond=0)
    hour_start = hour_end - timedelta(hours=1)
    print(f"=== p5_bot_stock_pools_watch: часовой срез {hour_start.isoformat()} -> {hour_end.isoformat()} ===")

    lo = find_block_by_timestamp(int(hour_start.timestamp()))
    hi = find_block_by_timestamp(int(hour_end.timestamp()))
    print(f"реальные блоки: [{lo};{hi}] ({hi - lo} блоков), {len(POOLS_BY_SYMBOL)} реальных v3-пулов")

    swaps = fetch_window_swaps(lo, hi)
    s = summarize(swaps)
    print(f"реальных свопов: {s['n_swaps']}, P5-бот попаданий: {s['p5_bot_hits']} "
          f"({(s['p5_bot_fraction'] or 0) * 100:.1f}%), self-trade: {(s['self_trade_fraction'] or 0) * 100:.1f}%")

    prev = load_previous()
    alert = None
    if s["n_swaps"] == 0:
        alert = "НЕТ СВОПОВ В ЭТОМ ЧАСЕ (низкая активность или сбой RPC -- честно, не гадаем причину)"
    elif s["p5_bot_hits"] == 0:
        alert = "P5-БОТ ОТСУТСТВУЕТ в этом часовом срезе"
    elif prev and prev.get("p5_bot_fraction") is not None and s["p5_bot_fraction"] is not None:
        if s["p5_bot_fraction"] < prev["p5_bot_fraction"] * DROP_ALERT_RATIO:
            alert = (f"ДОЛЯ P5-БОТА РЕЗКО УПАЛА: {s['p5_bot_fraction']:.3f} против "
                     f"{prev['p5_bot_fraction']:.3f} в предыдущей записи ({prev.get('date')})")
    if alert:
        print(f"[p5_bot_watch] ТРЕВОГА: {alert}")
    else:
        print("[p5_bot_watch] бот на месте, доля в норме относительно предыдущей записи")

    row = {
        "date": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hour_window": [hour_start.isoformat(), hour_end.isoformat()],
        "blocks": [lo, hi], "alert": alert, **s,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print(f"[p5_bot_watch] дописано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
