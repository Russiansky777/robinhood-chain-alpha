#!/usr/bin/env python3
"""Сторожок на переоткрытие линии "хеджированное LP" (владелец, 2026-09-05,
дополнение к закрытию): "cron раз в сутки, без капитала: по DefiLlama и
GT -- пулы с TVL >= $2M и суточным объёмом > 20x TVL. Список в
data/turnover_watch.jsonl, новые -- строкой в отчёт. Ничего не считать,
только ловить."

НЕ пересчитывает fee_apr/k/sigma/ratio (это дал бы полный
pool_screener_gt_recompute.py, платно по RPC/GT-throttle) -- только ЛОВИТ
кандидатов с аномально высоким объёмным сигналом (потенциальный признак
субсидированного/реального оборота, ради которого линия может быть
переоткрыта новым источником объёма, см. `docs/PROJECT_STATE.md`,
"## Закрытые линии").

Два независимых источника, оба реальные:
  1. DefiLlama `/pools` -- TVL и `volumeUsd1d` как есть, без пересчёта
     (тот же путь, что `pool_screener_defillama.py`, но с НОВЫМИ порогами:
     TVL>=$2M, объём/TVL>20x -- вместо $5M/20%).
  2. GeckoTerminal -- ТОЛЬКО для кандидатов, уже прошедших фильтр п.1
     (не тратим лимит GT на весь список DefiLlama), реюз
     `pool_screener_resolve_pools.py::resolve_pool()` -- находит реальный
     адрес пула по `underlying_tokens` и сравнивает `reserve_in_usd`/
     `volume_usd.h24` GT с DefiLlama тех же полей (та же честная сверка
     0.3x-3x, что уже была в скринере, не первый попавшийся результат).

`data/turnover_watch.jsonl` -- append-only, одна строка на запуск:
{"date": ..., "candidates": [...]}, где candidates -- ТЕКУЩИЙ список
прошедших порог (не только новые). "Новые" вычисляются сравнением с
объединением pool_id из ВСЕХ предыдущих строк файла -- печатаются
отдельно и попадают в текст коммита, чтобы быть заметными без
перечитывания всего файла."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from pool_screener_resolve_pools import resolve_pool  # noqa: E402 -- реюз реальной GT-сверки, не новый путь

OUT_PATH = Path("data/turnover_watch.jsonl")
DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"
TARGET_CHAINS = {"BSC", "Base", "Arbitrum", "Robinhood"}  # тот же охват, что основной скринер (23 кандидата) --
# сети, где у проекта реально есть исполнение; расширение на все сети DefiLlama осознанно не делается (сторожок
# ловит переоткрытие ДЕЙСТВУЮЩЕЙ линии, не новую вселенную).
MIN_TVL_USD = 2_000_000
MIN_TURNOVER_RATIO = 20.0  # объём/сутки > 20x TVL


def fetch_defillama_candidates() -> tuple[list[dict], int]:
    resp = requests.get(DEFILLAMA_POOLS_URL, timeout=60)
    resp.raise_for_status()
    all_pools = resp.json().get("data", [])
    candidates = []
    n_no_volume_field = 0
    for p in all_pools:
        chain = p.get("chain") or ""
        if not any(t.lower() in chain.lower() for t in TARGET_CHAINS):
            continue
        tvl = p.get("tvlUsd") or 0
        if tvl < MIN_TVL_USD:
            continue
        vol = p.get("volumeUsd1d")
        if vol is None:
            n_no_volume_field += 1
            continue
        ratio = vol / tvl if tvl else None
        if ratio is not None and ratio > MIN_TURNOVER_RATIO:
            candidates.append({
                "pool_id": p.get("pool"), "project": p.get("project"), "chain": chain,
                "symbol": p.get("symbol"), "tvl_usd": tvl, "volume_usd_1d": vol,
                "turnover_ratio_defillama": ratio, "underlying_tokens": p.get("underlyingTokens"),
                "apyBase": p.get("apyBase"),
            })
    return candidates, n_no_volume_field


def load_previously_seen_pool_ids() -> set[str]:
    if not OUT_PATH.exists():
        return set()
    seen: set[str] = set()
    for line in OUT_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        for c in row.get("candidates", []):
            if c.get("pool_id"):
                seen.add(c["pool_id"])
    return seen


def run() -> int:
    print(f"=== turnover_watch: TVL>=${MIN_TVL_USD:,} и объём/TVL>{MIN_TURNOVER_RATIO}x, сети {sorted(TARGET_CHAINS)} ===")
    candidates, n_no_vol = fetch_defillama_candidates()
    print(f"[turnover_watch] DefiLlama: {len(candidates)} кандидатов прошли порог (без поля объёма пропущено {n_no_vol})")

    for i, c in enumerate(candidates):
        print(f"  [{i+1}/{len(candidates)}] {c['chain']} {c['project']} {c['symbol']} "
              f"TVL=${c['tvl_usd']:,.0f} vol/TVL={c['turnover_ratio_defillama']:.1f}x -- сверка с GT...")
        gt = resolve_pool(c)
        c["gt_check"] = gt
        if gt.get("resolved_pool_address") and gt.get("reserve_usd"):
            gt_ratio = gt.get("volume_24h_usd", 0) / gt["reserve_usd"] if gt["reserve_usd"] else None
            c["turnover_ratio_gt"] = gt_ratio
            print(f"      GT: адрес={gt['resolved_pool_address']} match_quality={gt.get('match_quality')} "
                  f"turnover_ratio_gt={gt_ratio}")
        else:
            c["turnover_ratio_gt"] = None
            print(f"      GT: не резолвлено -- {gt.get('error')}")

    previously_seen = load_previously_seen_pool_ids()
    new_ones = [c for c in candidates if c["pool_id"] not in previously_seen]

    row = {"date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "candidates": candidates,
           "n_total": len(candidates), "n_new": len(new_ones)}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    print(f"\n[turnover_watch] всего сейчас над порогом: {len(candidates)}, из них НОВЫХ (не встречались ранее): {len(new_ones)}")
    if new_ones:
        print("[turnover_watch] НОВЫЕ кандидаты:")
        for c in new_ones:
            print(f"  - {c['chain']} {c['project']} {c['symbol']} (pool_id={c['pool_id']}) "
                  f"TVL=${c['tvl_usd']:,.0f} turnover_defillama={c['turnover_ratio_defillama']:.1f}x "
                  f"turnover_gt={c.get('turnover_ratio_gt')} gt_match={c.get('gt_check', {}).get('match_quality')}")
    else:
        print("[turnover_watch] новых кандидатов нет.")
    print(f"[turnover_watch] дописано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
