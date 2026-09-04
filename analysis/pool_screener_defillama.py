#!/usr/bin/env python3
"""Владелец, 2026-09-04: скринер пулов (главная задача из блока
"пассивное LP" альтернатив). Шаг 1 -- реальный список кандидатов через
DefiLlama yields API (бесплатно, без ключа): TVL >= $5M, объём/сутки
>= 20% TVL, сети BNB/Base/Arbitrum/Robinhood Chain.

DefiLlama /pools отдаёт apyBase/apyReward уже раздельно -- ничего не
пересчитываем, берём как есть. Объём 24ч DefiLlama /pools НЕ отдаёт
напрямую (только TVL+APY) -- используем /pools вместе с volume, если
поле есть в ответе (`volumeUsd1d` в некоторых версиях API); если нет --
честно помечаем "объём не подтверждён этим источником" и не
фильтруем по нему (не выдумываем число).

Только чтение (HTTP GET). Ончейн/Lighter/P5-позицию не трогает.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/pool_screener_defillama_result.json")
DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"
TARGET_CHAINS = {"BSC", "Base", "Arbitrum", "Robinhood"}  # DefiLlama chain-name variants проверяются по факту ниже
MIN_TVL_USD = 5_000_000
MIN_VOLUME_TVL_RATIO = 0.20  # объём/сутки >= 20% TVL


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    resp = requests.get(DEFILLAMA_POOLS_URL, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    all_pools = payload.get("data", [])
    print(f"[screener] всего пулов в DefiLlama /pools: {len(all_pools)}")

    real_chains = sorted(set(p.get("chain") for p in all_pools if p.get("chain")))
    matched_chains = [c for c in real_chains if any(t.lower() in c.lower() for t in TARGET_CHAINS)]
    print(f"[screener] реальные названия сетей в DefiLlama, похожие на целевые: {matched_chains}")
    result["real_chain_names_matched"] = matched_chains

    volume_field_present = any("volumeUsd1d" in p for p in all_pools[:50])
    print(f"[screener] поле volumeUsd1d присутствует в ответе: {volume_field_present}")
    result["volume_field_present_in_api"] = volume_field_present

    candidates = []
    excluded_no_volume_field = 0
    for p in all_pools:
        chain = p.get("chain") or ""
        if not any(t.lower() in chain.lower() for t in TARGET_CHAINS):
            continue
        tvl = p.get("tvlUsd") or 0
        if tvl < MIN_TVL_USD:
            continue
        vol = p.get("volumeUsd1d")
        entry = {
            "pool_id": p.get("pool"), "project": p.get("project"), "chain": chain,
            "symbol": p.get("symbol"), "tvl_usd": tvl,
            "apyBase": p.get("apyBase"), "apyReward": p.get("apyReward"), "apy": p.get("apy"),
            "volume_usd_1d": vol,
            "volume_tvl_ratio": (vol / tvl) if (vol is not None and tvl) else None,
            "underlying_tokens": p.get("underlyingTokens"),
            "il_risk": p.get("ilRisk"), "stablecoin": p.get("stablecoin"),
        }
        if vol is None:
            excluded_no_volume_field += 1
            entry["note"] = "volumeUsd1d отсутствует в ответе API для этого пула -- не можем проверить порог объёма, не выдумываем"
            candidates.append(entry)
            continue
        if (vol / tvl) >= MIN_VOLUME_TVL_RATIO:
            candidates.append(entry)

    print(f"[screener] TVL>=${MIN_TVL_USD:,} на целевых сетях: далее фильтр по объёму")
    print(f"[screener] пулов без поля объёма (не отфильтрованы, помечены): {excluded_no_volume_field}")
    print(f"[screener] итоговых кандидатов (TVL+объём, где известен): {len(candidates)}")

    candidates.sort(key=lambda c: (c["volume_tvl_ratio"] is None, -(c["volume_tvl_ratio"] or 0)))
    result["candidates"] = candidates
    result["n_candidates"] = len(candidates)
    result["filters"] = {"min_tvl_usd": MIN_TVL_USD, "min_volume_tvl_ratio": MIN_VOLUME_TVL_RATIO,
                          "target_chains_requested": sorted(TARGET_CHAINS)}
    result["runtime_s"] = time.time() - t0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[screener] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
