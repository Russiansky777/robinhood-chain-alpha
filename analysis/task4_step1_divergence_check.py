#!/usr/bin/env python3
"""Задача 4 владельца (2026-09-10), УПРОЩЁННЫЙ дизайн: три шага, каждый
даёт да/нет сам по себе, без доводки до идеала. Это Шаг 1: "есть ли
вообще расхождение".

Хвостовой пул -- РЕАЛЬНЫЙ, уже найденный и подтверждённый на прежнем
Шаге 1b (TVL $406,232, в полосе $50k-$500k), Robinhood chain -- берём
готовым, не пересчитываем заново (владелец: "любой прошедший
TVL-фильтр из уже сделанного Шага 1b").

Референс-пул -- ПРОСТО самый крупный пул ТОЙ ЖЕ пары на ДРУГОЙ цепи
(Base/Arbitrum), без фильтра на wash-trading, без поиска идеального
эталона (владелец, буквально). Один день: минутный VWAP референса
против сырых свопов хвостового пула. Вопрос: было ли расхождение >1%
хотя бы раз.

Правило владельца: если код падает ДВАЖДЫ ПОДРЯД на НОВОЙ ошибке --
стоп с честным "не получилось технически", без третьей попытки
чинить. Это НЕ реализовано как автоматический ретрай внутри скрипта
(каждый реальный запуск -- это Действие исполнителя, а не цикл) --
здесь просто минимальный, простой код без лишних защит "на всякий
случай"."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task4_arb_slow_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")
sys.path.insert(0, str(Path(__file__).parent))

import credit_guard  # noqa: E402
import task1_pool_liquidity as gt  # noqa: E402
from dune_client import DuneClient  # noqa: E402
from run_pipeline import read_sql  # noqa: E402

BUDGET = 600.0
TASK_CEILING_CREDITS = 300.0
DIVERGENCE_THRESHOLD_PCT = 1.0

# Реальный, уже найденный и подтверждённый хвостовой пул (Шаг 1b, run
# 34493650608): Robinhood, TVL $406,232 (в полосе $50k-$500k).
TAIL_CHAIN = "robinhood"
TAIL_POOL_ADDRESS = "0xb2a6ad51b3ea3cdc8d3508cca147a43471382e53"
TAIL_BASE_TOKEN = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"

OTHER_CHAINS = ["base", "arbitrum"]
OUT_PATH = Path("data/p3_guard_cache/task4_step1_result.json")


def get_gt_pool_name(chain: str, pool_address: str) -> dict | None:
    r = gt.gt_get(f"{gt.GT_BASE}/networks/{chain}/pools/{pool_address}")
    if r is None or r.status_code == 404:
        return None
    attrs = r.json().get("data", {}).get("attributes", {})
    return {"name": attrs.get("name"), "reserve_usd": float(attrs.get("reserve_in_usd") or 0)}


def search_pools_on_chain(query: str, chain: str) -> list[dict]:
    r = gt.gt_get(f"{gt.GT_BASE}/search/pools", params={"query": query, "network": chain})
    if r is None or r.status_code == 404:
        return []
    out = []
    for c in r.json().get("data", []):
        attrs = c.get("attributes", {})
        rel = c.get("relationships", {})
        tokens = {}
        for side in ("base_token", "quote_token"):
            tid = rel.get(side, {}).get("data", {}).get("id", "")
            if "_" in tid:
                tokens[side] = tid.split("_", 1)[1].lower()
        reserve = attrs.get("reserve_in_usd")
        if not reserve or not attrs.get("address"):
            continue
        out.append({"address": attrs["address"], "reserve_usd": float(reserve), "name": attrs.get("name"),
                    "base_token": tokens.get("base_token"), "quote_token": tokens.get("quote_token")})
    return out


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step": 1, "divergence_threshold_pct": DIVERGENCE_THRESHOLD_PCT,
                     "tail_chain": TAIL_CHAIN, "tail_pool_address": TAIL_POOL_ADDRESS}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    ns = credit_guard.namespace()
    credit_guard.ensure_namespace(ns, BUDGET)

    print(f"=== Реальное имя пары хвостового пула ({TAIL_CHAIN}/{TAIL_POOL_ADDRESS}) ===")
    tail_info = get_gt_pool_name(TAIL_CHAIN, TAIL_POOL_ADDRESS)
    if not tail_info or not tail_info.get("name"):
        result["blocker"] = "Не удалось получить реальное имя пары хвостового пула через GT -- технически не получилось."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"[step1] {result['blocker']}")
        return 1
    pair_name = tail_info["name"]
    result["tail_pool_name"] = pair_name
    query_symbol = pair_name.split("/")[0].strip().split()[0]
    result["search_query_symbol"] = query_symbol
    print(f"[step1] реальное имя пары: '{pair_name}', поисковый символ: '{query_symbol}'")

    print(f"\n=== Реальный поиск референс-пула той же пары на {OTHER_CHAINS} (просто самый крупный, без фильтров) ===")
    candidates = []
    for chain in OTHER_CHAINS:
        try:
            found = search_pools_on_chain(query_symbol, chain)
        except Exception as exc:  # noqa: BLE001
            result.setdefault("search_errors", {})[chain] = str(exc)[:200]
            continue
        for f in found:
            f["chain"] = chain
        candidates.extend(found)
    result["n_candidates_found"] = len(candidates)
    result["candidates_sample"] = candidates[:10]
    if not candidates:
        result["blocker"] = (f"Реальный поиск GT (query='{query_symbol}') на {OTHER_CHAINS} не вернул ни одного "
                              "пула -- этой пары там либо нет, либо не находится поиском. Технически не получилось "
                              "найти референс -- останавливаюсь, не гадаю.")
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[step1] {result['blocker']}")
        return 1

    ref = max(candidates, key=lambda c: c["reserve_usd"])
    result["reference_pool"] = ref
    print(f"[step1] реальный референс-пул: {ref['chain']}/{ref['address']} (TVL ${ref['reserve_usd']:,.0f}, '{ref['name']}')")
    if not ref.get("base_token"):
        result["blocker"] = "Референс-пул найден, но GT не отдал base_token -- технически не получилось посчитать цену."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[step1] {result['blocker']}")
        return 1

    now = datetime.now(timezone.utc)
    day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = day_end - timedelta(days=1)
    result["day_start_utc"], result["day_end_utc"] = day_start.isoformat(), day_end.isoformat()

    client = DuneClient()

    print(f"\n=== Реальные сырые свопы хвостового пула, один день ===")
    sql_tail = (read_sql("task4/task4_arb_raw_swaps")
                .replace("{{chain}}", TAIL_CHAIN)
                .replace("{{pool_address_list}}", f"from_hex('{TAIL_POOL_ADDRESS[2:].lower()}')")
                .replace("{{day_start}}", day_start.strftime("%Y-%m-%d %H:%M:%S"))
                .replace("{{day_end}}", day_end.strftime("%Y-%m-%d %H:%M:%S")))
    qid_tail = client.create_query("task4_step1_tail_raw_swaps", sql_tail)
    df_tail = client.run_sql_cached("task4_step1_tail_raw_swaps", sql_tail, query_id=qid_tail,
                                     estimated_credits=5.0, expected_max_rows=20000, expected_columns=8)
    if df_tail is None or not len(df_tail):
        result["blocker"] = "Сырые свопы хвостового пула за этот день пусты -- нечего сравнивать."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[step1] {result['blocker']}")
        return 1
    result["n_tail_swaps_this_day"] = len(df_tail)
    print(f"[step1] реальных свопов хвостового пула за день: {len(df_tail)}")

    print(f"\n=== Реальный минутный VWAP референс-пула, тот же день ===")
    sql_ref = (read_sql("task4/task4_arb_reference_minute_vwap")
               .replace("{{chain}}", ref["chain"])
               .replace("{{pool_address_list}}", f"from_hex('{ref['address'][2:].lower()}')")
               .replace("{{token_address_list}}", f"from_hex('{ref['base_token'][2:].lower()}')")
               .replace("{{day_start}}", day_start.strftime("%Y-%m-%d %H:%M:%S"))
               .replace("{{day_end}}", day_end.strftime("%Y-%m-%d %H:%M:%S")))
    qid_ref = client.create_query("task4_step1_reference_minute_vwap", sql_ref)
    df_ref = client.run_sql_cached("task4_step1_reference_minute_vwap", sql_ref, query_id=qid_ref,
                                    estimated_credits=5.0, expected_max_rows=1500, expected_columns=4)
    if df_ref is None or not len(df_ref):
        result["blocker"] = "Минутный VWAP референс-пула за этот день пуст -- нечего сравнивать."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[step1] {result['blocker']}")
        return 1
    result["n_ref_minutes_this_day"] = len(df_ref)
    print(f"[step1] реальных минутных баров референса за день: {len(df_ref)}")

    # Цена хвостового пула по каждому свопу: amount_usd / qty(base_token).
    import pandas as pd
    df_tail["block_time"] = pd.to_datetime(df_tail["block_time"], utc=True)
    base_t = TAIL_BASE_TOKEN.lower().replace("0x", "")
    df_tail["qty_base"] = df_tail.apply(
        lambda r: r["token_bought_amount"] if str(r["token_bought_address"]).lower().replace("0x", "") == base_t
        else r["token_sold_amount"], axis=1)
    df_tail = df_tail[df_tail["qty_base"] > 0]
    df_tail["price_tail"] = df_tail["amount_usd"] / df_tail["qty_base"]

    df_ref["minute_utc"] = pd.to_datetime(df_ref["minute_utc"], utc=True)
    df_ref = df_ref[df_ref["token_qty"] > 0]
    df_ref["price_ref_raw"] = df_ref["vol_usd"] / df_ref["token_qty"]
    df_ref = df_ref.sort_values("minute_utc")

    # Честная (не идеальная) поправка на инверсию: GT может назвать
    # "base_token" по-разному на разных сетях -- сверяем порядок
    # величины референс-цены с хвостовой, берём обратную, если так
    # ближе (простая эвристика, не точная сверка конвенций GT).
    med_tail = df_tail["price_tail"].median()
    med_ref_raw = df_ref["price_ref_raw"].median()
    inverted = abs(__import__("math").log((med_ref_raw + 1e-9) / (med_tail + 1e-9))) > \
        abs(__import__("math").log((1 / (med_ref_raw + 1e-9) + 1e-9) / (med_tail + 1e-9)))
    df_ref["price_ref"] = (1 / df_ref["price_ref_raw"]) if inverted else df_ref["price_ref_raw"]
    result["reference_price_inverted"] = bool(inverted)
    result["median_tail_price"] = float(med_tail)
    result["median_reference_price_used"] = float(df_ref["price_ref"].median())

    # merge_asof: для каждого свопа хвоста -- последний известный минутный референс-бар.
    df_tail = df_tail.sort_values("block_time")
    merged = pd.merge_asof(df_tail, df_ref[["minute_utc", "price_ref"]],
                            left_on="block_time", right_on="minute_utc", direction="backward")
    merged = merged.dropna(subset=["price_ref"])
    merged["divergence_pct"] = (merged["price_tail"] / merged["price_ref"] - 1).abs() * 100

    n_matched = len(merged)
    n_divergent = int((merged["divergence_pct"] > DIVERGENCE_THRESHOLD_PCT).sum())
    max_div = float(merged["divergence_pct"].max()) if n_matched else None
    result["n_swaps_matched_to_reference"] = n_matched
    result["n_swaps_divergence_gt_1pct"] = n_divergent
    result["max_divergence_pct"] = max_div
    result["any_divergence_found"] = bool(n_divergent > 0)
    result["runtime_s_total"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    print(f"\n[step1] реальных совпавших свопов: {n_matched}, с расхождением >{DIVERGENCE_THRESHOLD_PCT}%: {n_divergent}, "
          f"максимальное расхождение: {max_div:.2f}%" if max_div is not None else "")
    if n_divergent > 0:
        print(f"\n[step1] ДА -- расхождение >1% реально было. Идём в Шаг 2.")
    else:
        print(f"\n[step1] НЕТ -- расхождений >1% не найдено за этот день. Задача 4 на этой паре закрыта.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
