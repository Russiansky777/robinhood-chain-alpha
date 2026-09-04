#!/usr/bin/env python3
"""Скринер пулов, шаг 2: резолвинг реального адреса AMM-пула для каждого
кандидата из data/p3_guard_cache/pool_screener_defillama_result.json.

DefiLlama `pool_id` -- ВНУТРЕННИЙ UUID агрегатора, НЕ ончейн-адрес (это
и было нерешённой проблемой предыдущего прохода). Но DefiLlama отдаёт
реальные `underlying_tokens` (адреса ERC20 обеих ног пары) -- этого
достаточно, чтобы найти реальный адрес пула через GeckoTerminal:
`GET /networks/{network}/tokens/{token0}/pools` -- список топ-пулов по
этому токену, каждый с полным списком token0/token1 -- фильтруем по
совпадению ВТОРОГО адреса (underlying_tokens[1]), не угадываем.

Дополнительная сверка (не угадывание): сравниваем `reserve_in_usd` и
`volume_usd.h24` найденного GT-пула с `tvl_usd`/`volume_usd_1d`
DefiLlama для того же кандидата -- если оба совпадают в пределах
разумного порядка величины, это подтверждает, что найден ТОТ ЖЕ пул
(а не другой fee-tier/похожая пара). Несовпадение -- явно помечается,
не скрывается тихим выбором первого попавшегося результата.

Rate limit GT: 30/мин заявлено, эмпирически ниже -- интервал/backoff
скопированы из analysis/p5_gt_pool_history.py (уже откалибровано)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

GT_BASE = "https://api.geckoterminal.com/api/v2"
MIN_REQUEST_INTERVAL_S = 2.6
RATE_LIMIT_BACKOFF_S = 65.0
RATE_LIMIT_MAX_RETRIES = 2
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "robinhood-chain-alpha-screener/1.0"}

# DefiLlama chain name -> GeckoTerminal network slug (проверено ранее в
# проекте: analysis/p5_gt_pool_history.py для 'robinhood'/'robinhood-chain';
# base/arbitrum/bsc -- стандартные, широко используемые слаги GT).
CHAIN_TO_GT_NETWORK = {"Base": "base", "Arbitrum": "arbitrum", "BSC": "bsc", "Robinhood Chain": "robinhood"}

AMM_PROJECTS = {"uniswap-v3", "uniswap-v4", "aerodrome-slipstream", "aerodrome-v1"}

_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _last_call + MIN_REQUEST_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _get(url: str, params: dict | None = None) -> tuple[int, dict | str]:
    status, body = None, None
    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        _throttle()
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        try:
            status, body = r.status_code, r.json()
        except ValueError:
            status, body = r.status_code, r.text[:500]
        if status == 429 and attempt < RATE_LIMIT_MAX_RETRIES:
            print(f"    429 на {url}, жду {RATE_LIMIT_BACKOFF_S:.0f}с и повторяю")
            time.sleep(RATE_LIMIT_BACKOFF_S)
            continue
        return status, body
    return status, body


def resolve_pool(candidate: dict) -> dict:
    chain = candidate["chain"]
    network = CHAIN_TO_GT_NETWORK.get(chain)
    tokens = candidate.get("underlying_tokens") or []
    result = {"network": network, "resolved_pool_address": None, "match_quality": None, "error": None,
              "gt_candidates_checked": 0}
    if network is None:
        result["error"] = f"нет маппинга chain->GT network для '{chain}'"
        return result
    if len(tokens) != 2:
        result["error"] = f"underlying_tokens не пара (найдено {len(tokens)}) -- пропуск"
        return result

    token0, token1 = tokens[0].lower(), tokens[1].lower()
    status, body = _get(f"{GT_BASE}/networks/{network}/tokens/{token0}/pools")
    if status != 200 or not isinstance(body, dict):
        result["error"] = f"GT /tokens/{{addr}}/pools вернул HTTP {status}: {str(body)[:200]}"
        return result

    data = body.get("data", [])
    result["gt_candidates_checked"] = len(data)
    best = None
    for pool in data:
        attrs = pool.get("attributes", {})
        name = attrs.get("name", "")
        addr = attrs.get("address", "").lower()
        relationships = pool.get("relationships", {})
        base_addr = (relationships.get("base_token", {}).get("data", {}).get("id", "") or "").split("_")[-1].lower()
        quote_addr = (relationships.get("quote_token", {}).get("data", {}).get("id", "") or "").split("_")[-1].lower()
        pair_addrs = {base_addr, quote_addr}
        if {token0, token1} == pair_addrs:
            # Сверка правдоподобия -- сравниваем TVL/объём с DefiLlama,
            # не берём первый попавшийся результат по паре тем же двум
            # токенам (может быть несколько fee-tier пулов).
            reserve_usd = float(attrs.get("reserve_in_usd") or 0)
            vol_24h = float((attrs.get("volume_usd") or {}).get("h24") or 0)
            tvl_ratio = reserve_usd / candidate["tvl_usd"] if candidate["tvl_usd"] else None
            vol_ratio = vol_24h / candidate["volume_usd_1d"] if candidate.get("volume_usd_1d") else None
            score = abs((tvl_ratio or 999) - 1) if tvl_ratio else 999
            entry = {"address": addr, "name": name, "reserve_usd": reserve_usd, "volume_24h_usd": vol_24h,
                      "tvl_ratio_gt_over_defillama": tvl_ratio, "volume_ratio_gt_over_defillama": vol_ratio}
            if best is None or score < best[0]:
                best = (score, entry)
    if best is None:
        result["error"] = f"среди {len(data)} пулов токена {token0} на {network} ни один не содержит также {token1}"
        return result
    score, entry = best
    result["resolved_pool_address"] = entry["address"]
    result["gt_pool_name"] = entry["name"]
    result["tvl_ratio_gt_over_defillama"] = entry["tvl_ratio_gt_over_defillama"]
    result["volume_ratio_gt_over_defillama"] = entry["volume_ratio_gt_over_defillama"]
    # Разумный порядок величины -- НЕ строгое совпадение (DefiLlama и GT
    # обновляются в разное время, могут по-разному считать TVL для v3/v4
    # диапазонных позиций) -- 0.3x-3x пропускаем как правдоподобное,
    # иначе явно помечаем как сомнительное соответствие.
    ratio = entry["tvl_ratio_gt_over_defillama"]
    result["match_quality"] = "plausible" if (ratio is not None and 0.3 <= ratio <= 3.0) else "SUSPECT -- TVL расходится с DefiLlama больше чем в 3 раза"
    return result


def run() -> int:
    d = json.load(open("data/p3_guard_cache/pool_screener_defillama_result.json"))
    amm_candidates = [p for p in d["candidates"] if p["project"] in AMM_PROJECTS]
    print(f"[resolve] {len(amm_candidates)} AMM-кандидатов из {len(d['candidates'])} всего (лендинг-протоколы исключены)")

    results = []
    for i, c in enumerate(amm_candidates):
        print(f"\n=== {i+1}/{len(amm_candidates)}: {c['chain']} {c['project']} {c['symbol']} (TVL=${c['tvl_usd']:,.0f}) ===")
        r = resolve_pool(c)
        print(f"    -> {r}")
        results.append({**c, "gt_resolution": r})

    n_resolved = sum(1 for r in results if r["gt_resolution"]["resolved_pool_address"])
    n_plausible = sum(1 for r in results if r["gt_resolution"].get("match_quality") == "plausible")
    print(f"\n[resolve] адрес найден: {n_resolved}/{len(amm_candidates)}, из них правдоподобных (TVL 0.3x-3x DefiLlama): {n_plausible}")

    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "n_amm_candidates": len(amm_candidates), "n_resolved": n_resolved, "n_plausible": n_plausible,
           "candidates": results}
    Path("data/p3_guard_cache/pool_screener_resolve_pools_result.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
