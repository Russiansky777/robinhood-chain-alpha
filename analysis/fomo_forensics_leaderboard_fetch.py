#!/usr/bin/env python3
"""Задача «форензика fomo» -- реальный источник топ-10 найден
(fomo_forensics_api_guess_probe_result.json, 2026-09-05):
`https://robinscan.io/api/leaderboard` отдаёт реальный JSON, 300
отслеживаемых адресов, `realizedEth`/`trades`/`tokensTraded`/
`transfersAnalyzed`/`complete`/`computedAt` на запись. Только чтение.

РЕАЛЬНАЯ НАХОДКА: query-параметры `period=30d`/`window=30d` НЕ меняли
ответ (байт-в-байт тот же content_length) -- либо эндпоинт всегда
отдаёт один и тот же (уже посчитанный) снимок без периодной
фильтрации через эти конкретные имена параметров, либо период
встроен в саму методику расчёта на бэкенде robinscan, не выбирается
клиентом. Проверяем это здесь -- не предполагаем."""
from __future__ import annotations

import json
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-fomo-forensics-recon/1.0", "Accept": "application/json"}
OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_leaderboard_result.json")


def run() -> int:
    r = requests.get("https://robinscan.io/api/leaderboard", headers=HEADERS, timeout=30)
    r.raise_for_status()
    body = r.json()

    entries = body.get("entries", [])
    print(f"[leaderboard] generatedAt={body.get('generatedAt')}, tracked={body.get('tracked')}, ethUsd={body.get('ethUsd')}")
    print(f"[leaderboard] реальных записей в ответе: {len(entries)}")

    n_complete = sum(1 for e in entries if e.get("complete"))
    print(f"[leaderboard] complete=true: {n_complete}/{len(entries)} -- у остальных PnL посчитан по НЕПОЛНОЙ истории (transfersAnalyzed достиг потолка)")

    computed_ats = sorted(e.get("computedAt") for e in entries if e.get("computedAt"))
    if computed_ats:
        print(f"[leaderboard] computedAt диапазон: {computed_ats[0]} .. {computed_ats[-1]}")

    top10 = entries[:10]
    print("\n=== ТОП-10 (реальные адреса, как отданы API) ===")
    for e in top10:
        print(f"  #{e['rank']}: {e['address']} realizedEth={e['realizedEth']:.4f} trades={e['trades']} "
              f"tokensTraded={e['tokensTraded']} complete={e['complete']} computedAt={e['computedAt']}")

    out = {
        "generatedAt": body.get("generatedAt"), "tracked": body.get("tracked"), "ethUsd": body.get("ethUsd"),
        "n_entries_total": len(entries), "n_complete": n_complete,
        "computed_at_range": [computed_ats[0], computed_ats[-1]] if computed_ats else None,
        "top10": top10,
        "full_raw_body_keys": list(body.keys()),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[leaderboard] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
