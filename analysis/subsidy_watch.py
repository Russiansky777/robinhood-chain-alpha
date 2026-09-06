#!/usr/bin/env python3
"""Расширение сторожка (владелец, 2026-09-06, дословно): "Сейчас :51
ловит только пулы с оборотом > 20x TVL. Добавить, раз в сутки,
бесплатно:
  - новые перп-площадки и DEX с нулевой комиссией или программой
    поинтов -- по публичным источникам (DefiLlama incentives,
    анонсы бирж);
  - цепи с активным газовым вейвером или инсентив-программой;
  - пулы с apyReward > 50% при apyBase > 10% на DefiLlama, любые сети.
Пишет в data/subsidy_watch.jsonl, новое -- строкой в отчёт. Ничего не
считать, только ловить." Причина (владелец, дословно): "единственное,
что за три дня реально давало доходность выше порога, -- чужие
субсидии (P5, отношение 1.3-1.47 на накрученном объёме)."

ЧЕСТНОЕ ОГРАНИЧЕНИЕ метода (не гадаем, не скрываем): DefiLlama НЕ
публикует структурированное поле "нулевая комиссия"/"программа
поинтов"/"газовый вейвер" -- у нас нет чистого API-поля для этого.
Три реальных, дешёвых, честных ПРОКСИ-сигнала вместо прямого
детектора:
  1. Пулы apyReward>50% и apyBase>10% -- ПРЯМОЕ реальное поле
     DefiLlama `/pools` (тот же эндпоинт, что turnover_watch.py,
     ВСЕ сети, не только TARGET_CHAINS) -- точное совпадение
     формулировки владельца, без прокси.
  2. Новые протоколы в категориях Derivatives/Dexes (DefiLlama
     `/protocols`, поле `category`) -- поймает НОВЫЙ ЛИСТИНГ
     перп-площадки/DEX, НЕ доказывает нулевую комиссию/поинты САМО
     ПО СЕБЕ -- честно помечено как "новый листинг, требует ручной
     проверки условий".
  3. Цепи с резким ростом TVL за 7 дней (DefiLlama `/chains`,
     реальные поля tvl/tvlPrevWeek) -- ПРОКСИ на "что-то
     стимулируемое происходит", не прямое доказательство газового
     вейвера/инсентив-программы -- тоже помечено как требующее
     ручной проверки.
"Ничего не считать" -- пороги фиксированы владельцем/выбраны как
разумный порог для "аномалии", дальше -- честный список кандидатов
без ранжирования/оценки экономики."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/subsidy_watch.jsonl")
DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"
DEFILLAMA_PROTOCOLS_URL = "https://api.llama.fi/protocols"
DEFILLAMA_CHAINS_URL = "https://api.llama.fi/v2/chains"

APY_REWARD_THRESHOLD = 50.0  # % -- владелец, дословно
APY_BASE_THRESHOLD = 10.0  # % -- владелец, дословно
DERIVATIVE_DEX_CATEGORIES = {"derivatives", "dexs", "dexes", "perpetuals", "options"}  # см. докстринг -- реальные набл. категории логируются отдельно, не гадаем полный список заранее
CHAIN_7D_GROWTH_THRESHOLD = 0.30  # 30% рост TVL за 7 дней -- разумный порог "аномалии", не гарантия инсентив-программы


def fetch_reward_pools() -> list[dict]:
    resp = requests.get(DEFILLAMA_POOLS_URL, timeout=60)
    resp.raise_for_status()
    all_pools = resp.json().get("data", [])
    out = []
    for p in all_pools:
        apy_reward = p.get("apyReward")
        apy_base = p.get("apyBase")
        if apy_reward is None or apy_base is None:
            continue
        if apy_reward > APY_REWARD_THRESHOLD and apy_base > APY_BASE_THRESHOLD:
            out.append({
                "pool_id": p.get("pool"), "project": p.get("project"), "chain": p.get("chain"),
                "symbol": p.get("symbol"), "tvl_usd": p.get("tvlUsd"),
                "apyReward": apy_reward, "apyBase": apy_base, "apy": p.get("apy"),
                "rewardTokens": p.get("rewardTokens"),
            })
    return out


def fetch_new_derivative_dex_protocols() -> tuple[list[dict], set[str]]:
    resp = requests.get(DEFILLAMA_PROTOCOLS_URL, timeout=60)
    resp.raise_for_status()
    all_protocols = resp.json()
    real_categories_seen = {(p.get("category") or "").strip().lower() for p in all_protocols}
    matches = []
    for p in all_protocols:
        cat = (p.get("category") or "").strip().lower()
        if cat in DERIVATIVE_DEX_CATEGORIES:
            matches.append({
                "slug": p.get("slug"), "name": p.get("name"), "category": p.get("category"),
                "chains": p.get("chains"), "tvl": p.get("tvl"), "url": p.get("url"),
                "listedAt": p.get("listedAt"),
            })
    return matches, real_categories_seen


def fetch_high_growth_chains() -> list[dict]:
    resp = requests.get(DEFILLAMA_CHAINS_URL, timeout=60)
    resp.raise_for_status()
    all_chains = resp.json()
    out = []
    for c in all_chains:
        tvl = c.get("tvl")
        tvl_prev_week = c.get("tvlPrevWeek")
        if not tvl or not tvl_prev_week or tvl_prev_week <= 0:
            continue
        growth = tvl / tvl_prev_week - 1
        if growth > CHAIN_7D_GROWTH_THRESHOLD:
            out.append({"chain": c.get("name"), "tvl_usd": tvl, "tvl_prev_week_usd": tvl_prev_week,
                         "growth_7d_frac": growth})
    return sorted(out, key=lambda r: -r["growth_7d_frac"])


def load_previously_seen(key_field: str, category_key: str) -> set[str]:
    if not OUT_PATH.exists():
        return set()
    seen: set[str] = set()
    for line in OUT_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        for item in row.get(category_key, []):
            if item.get(key_field):
                seen.add(item[key_field])
    return seen


def run() -> int:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"=== subsidy_watch: {now} ===")

    print("\n--- 1. Пулы apyReward>50% и apyBase>10%, любые сети ---")
    reward_pools = fetch_reward_pools()
    print(f"[subsidy_watch] реальных пулов над порогом: {len(reward_pools)}")
    prev_pool_ids = load_previously_seen("pool_id", "reward_pools")
    new_reward_pools = [p for p in reward_pools if p["pool_id"] not in prev_pool_ids]

    print("\n--- 2. Новые протоколы в категориях Derivatives/Dexes ---")
    dex_protocols, real_categories = fetch_new_derivative_dex_protocols()
    print(f"[subsidy_watch] реальных категорий в ответе DefiLlama (для сверки списка DERIVATIVE_DEX_CATEGORIES): "
          f"{sorted(c for c in real_categories if c)}")
    print(f"[subsidy_watch] реальных протоколов в целевых категориях: {len(dex_protocols)}")
    prev_slugs = load_previously_seen("slug", "dex_protocols")
    new_dex_protocols = [p for p in dex_protocols if p["slug"] not in prev_slugs]

    print("\n--- 3. Цепи с ростом TVL >30% за 7 дней ---")
    high_growth_chains = fetch_high_growth_chains()
    print(f"[subsidy_watch] реальных цепей над порогом роста: {len(high_growth_chains)}")
    for c in high_growth_chains[:10]:
        print(f"    {c['chain']}: TVL=${c['tvl_usd']:,.0f} (7д рост {c['growth_7d_frac']:.1%})")

    row = {
        "date": now, "reward_pools": reward_pools, "n_reward_pools": len(reward_pools),
        "n_new_reward_pools": len(new_reward_pools),
        "dex_protocols": dex_protocols, "n_dex_protocols": len(dex_protocols),
        "n_new_dex_protocols": len(new_dex_protocols),
        "high_growth_chains": high_growth_chains, "n_high_growth_chains": len(high_growth_chains),
        "real_categories_seen_this_run": sorted(c for c in real_categories if c),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    print(f"\n[subsidy_watch] НОВОЕ с прошлого запуска: {len(new_reward_pools)} пул(ов) с наградами, "
          f"{len(new_dex_protocols)} DEX/дериватив-протокол(ов)")
    if new_reward_pools:
        print("[subsidy_watch] НОВЫЕ пулы с наградами:")
        for p in new_reward_pools:
            print(f"  - {p['chain']} {p['project']} {p['symbol']} apyReward={p['apyReward']:.1f}% apyBase={p['apyBase']:.1f}% TVL=${p.get('tvl_usd')}")
    if new_dex_protocols:
        print("[subsidy_watch] НОВЫЕ DEX/дериватив-протоколы (листинг, условия НЕ проверены):")
        for p in new_dex_protocols:
            print(f"  - {p['name']} ({p['category']}) на {p['chains']}, TVL=${p.get('tvl')}, {p.get('url')}")
    if not new_reward_pools and not new_dex_protocols:
        print("[subsidy_watch] новых кандидатов нет.")
    print(f"[subsidy_watch] дописано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
