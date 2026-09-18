#!/usr/bin/env python3
"""Владелец, 2026-09-18: движется ли цена после первого входа кандидата --
раздельно для двух групп (сито уже разбито в fomo_leaderboard_candidates.json
на group_leader_like_le_150_per_week и group_hyperactive_gt_150_per_week,
см. solana_fomo_onchain_filter.py). Владелец явно отменил верхнюю границу
по числу первых входов -- люди копируют топ рейтинга не разбираясь, бот
это или человек; если у активных кошельков цена после входа тоже растёт,
это на порядок больше сигналов, и это надо показать данными, а не
догадкой "за ботом не идут".

Метод -- ТОТ ЖЕ, что весь основной конвейер (buyer_100/buyer_200): для
каждого сэмпла первого входа (сигнатура/минт/время, уже собраны
solana_fomo_onchain_filter.py в first_entry_samples) строим маршрут до
USDC (plan_route_for_purchase, BFS по пулам ИЗ ТРАНЗАКЦИИ + накопленные
pool_meta/route_meta) и берём цену в конце маршрута на +5с/+30с
(find_price_at). Честно пропускаем сэмплы без найденного маршрута --
не считаем их отсутствие сигналом.

По каждой группе -- медиана роста, доля положительных. Сэмплинг ограничен
(до SAMPLES_PER_GROUP кандидатов x до SAMPLES_PER_WALLET сделок), чтобы
не растягивать на весь список -- если понадобится точнее, досчитать
отдельным резюмируемым проходом по образцу остального конвейера."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_buyer200_select_extend import plan_route_for_purchase, POOL_META_PATH, ROUTE_META_PATH  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_PATH = REPO_ROOT / "data" / "fomo_leaderboard_candidates.json"
OUT_PATH = REPO_ROOT / "data" / "solana_fomo_price_after_entry_result.json"

HORIZONS = [5, 30]
SAMPLES_PER_GROUP = 6   # кандидатов на группу -- самые активные (первыми в списке, уже отсортированы)
SAMPLES_PER_WALLET = 3  # сделок на кандидата


def price_growth(mint: str, tx: dict, global_meta: dict, entry_time: int) -> dict:
    route, _ = plan_route_for_purchase(mint, tx, global_meta)
    if not route:
        return {"status": "no_route"}
    result: dict = {"status": "ok", "route_len": len(route)}
    for sec in HORIZONS:
        t = entry_time + sec
        try:
            value = D(1)
            ok = True
            for leg in route:
                p = fp.find_price_at(leg["pool"], t, entry_time, t)
                if p.get("status") != "ok":
                    ok = False
                    result[f"plus{sec}s_status"] = p.get("status")
                    break
                e = p["event"]
                v = D(e["p1_per_0"])
                if leg["from"] == e["m0"] and leg["to"] == e["m1"]:
                    value *= v
                elif leg["from"] == e["m1"] and leg["to"] == e["m0"]:
                    value /= v
                else:
                    ok = False
                    break
            if ok:
                # Цена в момент входа (t=entry_time) -- та же цепочка на t0, чтобы
                # сравнение было "рост от цены входа", а не от абстрактной единицы.
                value0 = D(1)
                for leg in route:
                    p0 = fp.find_price_at(leg["pool"], entry_time, entry_time - 5, entry_time)
                    if p0.get("status") != "ok":
                        ok = False
                        break
                    e0 = p0["event"]
                    v0 = D(e0["p1_per_0"])
                    value0 = value0 * v0 if leg["from"] == e0["m0"] else value0 / v0
                if ok and value0 > 0:
                    result[f"plus{sec}s_growth_pct"] = float((value / value0 - 1) * 100)
        except RuntimeError as exc:
            result[f"plus{sec}s_error"] = str(exc)[:200]
    return result


def process_group(rows: list[dict], global_meta: dict) -> dict:
    samples_out = []
    for row in rows[:SAMPLES_PER_GROUP]:
        address = row["solana_address"]
        for sample in (row.get("first_entry_samples") or [])[:SAMPLES_PER_WALLET]:
            tx = fp.get_transaction(sample["signature"])
            if tx is None:
                continue
            g = price_growth(sample["mint"], tx, global_meta, sample["block_time"])
            g.update({"wallet": address, "signature": sample["signature"], "mint": sample["mint"]})
            samples_out.append(g)
            print(f"[fomo_price] {address[:8]}.. mint={sample['mint'][:8]}.. {g.get('status')} "
                  f"+5s={g.get('plus5s_growth_pct')} +30s={g.get('plus30s_growth_pct')}", flush=True)

    agg = {}
    for sec in HORIZONS:
        vals = [s[f"plus{sec}s_growth_pct"] for s in samples_out if f"plus{sec}s_growth_pct" in s]
        if vals:
            vals_sorted = sorted(vals)
            n = len(vals_sorted)
            median = vals_sorted[n // 2] if n % 2 else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2
            agg[f"plus{sec}s"] = {"n": n, "median_pct": median, "n_positive": sum(1 for v in vals if v > 0),
                                   "min_pct": min(vals), "max_pct": max(vals)}
        else:
            agg[f"plus{sec}s"] = {"n": 0}
    return {"n_wallets_sampled": min(len(rows), SAMPLES_PER_GROUP), "n_trades_sampled": len(samples_out),
            "n_trades_with_route": sum(1 for s in samples_out if s.get("status") == "ok"),
            "aggregate": agg, "samples": samples_out}


def main() -> None:
    if not CANDIDATES_PATH.exists():
        print("[fomo_price] нет fomo_leaderboard_candidates.json.", flush=True)
        return
    data = json.loads(CANDIDATES_PATH.read_text())
    group_a = data.get("group_leader_like_le_150_per_week") or []
    group_b = data.get("group_hyperactive_gt_150_per_week") or []
    print(f"[fomo_price] группа <=150/неделю: {len(group_a)} кандидатов; "
          f">150/неделю: {len(group_b)} кандидатов", flush=True)

    global_meta: dict = json.loads(POOL_META_PATH.read_text()) if POOL_META_PATH.exists() else {}
    global_meta.update(json.loads(ROUTE_META_PATH.read_text()) if ROUTE_META_PATH.exists() else {})

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "group_leader_like_le_150_per_week": process_group(group_a, global_meta),
        "group_hyperactive_gt_150_per_week": process_group(group_b, global_meta),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[fomo_price] Записано {OUT_PATH}", flush=True)
    print(f"[fomo_price] leader-like agg: {out['group_leader_like_le_150_per_week']['aggregate']}", flush=True)
    print(f"[fomo_price] hyperactive agg: {out['group_hyperactive_gt_150_per_week']['aggregate']}", flush=True)


if __name__ == "__main__":
    main()
