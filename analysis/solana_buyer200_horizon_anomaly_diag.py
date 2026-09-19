#!/usr/bin/env python3
"""Владелец, 2026-09-19: проверить гипотезу "деление на почти ноль" для
аномальных средних на длинных горизонтах (900с/1ч/6ч/24ч).

Для каждой сделки с аномальным отношением цена-на-горизонте/база (>50x
или <1/50x): базовая цена (+5с), цена на горизонте, и ЛИКВИДНОСТЬ пула
на МОМЕНТ ЗАМЕРА -- честно: GeckoTerminal `/pools/{address}` отдаёт
ТЕКУЩУЮ (на момент этого запроса) reserve_in_usd, не историческую на
момент сделки -- если пул с тех пор набрал/потерял ликвидность, это не
то же самое число, что было при исполнении. Помечено явно в выводе,
не выдаётся за историческую величину."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = REPO_ROOT / "data" / "solana_buyer_200"
OUT_PATH = OUT_ROOT / "horizon_anomaly_diag.json"

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
ANOMALY_RATIO_HIGH = 50.0
ANOMALY_RATIO_LOW = 1.0 / 50.0


def gecko_get(path: str, params: dict | None = None) -> dict:
    try:
        resp = requests.get(f"{GECKO_BASE}{path}", params=params or {}, timeout=20)
        return {"http_status": resp.status_code, "body": resp.json() if resp.ok else None}
    except Exception as exc:  # noqa: BLE001
        return {"http_status": None, "exception": str(exc)[:200]}


def current_pool_liquidity_usd(pool_address: str) -> float | None:
    r = gecko_get(f"/networks/solana/pools/{pool_address}")
    attrs = ((r.get("body") or {}).get("data") or {}).get("attributes") or {}
    try:
        return float(attrs.get("reserve_in_usd")) if attrs.get("reserve_in_usd") is not None else None
    except (TypeError, ValueError):
        return None


def main() -> None:
    sel = {r["signature"]: r for r in json.loads((OUT_ROOT / "selected_300.json").read_text())}
    short = {(r["signature"], r["seconds"]): r for r in json.loads((OUT_ROOT / "step_extended_result.json").read_text())
             if "seconds" in r}
    long_raw = json.loads((OUT_ROOT / "long_horizons_result.json").read_text())

    all_bases = []
    for sig in sel:
        e5 = short.get((sig, 5))
        if e5 and e5.get("mine_status") == "ok" and e5.get("mine_price"):
            all_bases.append(float(e5["mine_price"]))
    median_base = sorted(all_bases)[len(all_bases) // 2] if all_bases else None

    anomalies = []
    for sig, by_sec in long_raw.items():
        e5 = short.get((sig, 5))
        if not e5 or e5.get("mine_status") != "ok" or not e5.get("mine_price"):
            continue
        base = float(e5["mine_price"])
        for sec_str, v in by_sec.items():
            if v.get("status") != "ok" or v.get("price_usd") is None:
                continue
            ratio = v["price_usd"] / base if base else None
            if ratio is None:
                continue
            if ratio > ANOMALY_RATIO_HIGH or ratio < ANOMALY_RATIO_LOW:
                anomalies.append({"signature": sig, "mint": sel.get(sig, {}).get("mint"),
                                   "horizon_seconds": int(sec_str), "base_price_at_5s": base,
                                   "price_at_horizon": v["price_usd"], "ratio": ratio,
                                   "base_vs_median_base_ratio": (base / median_base) if median_base else None,
                                   "pool": v.get("pool")})

    print(f"[anomaly_diag] медианная база (+5с) по всем сделкам: {median_base}", flush=True)
    print(f"[anomaly_diag] найдено {len(anomalies)} аномальных точек (ratio>{ANOMALY_RATIO_HIGH} или <{ANOMALY_RATIO_LOW})", flush=True)

    pool_liq_cache: dict[str, float | None] = {}
    for a in anomalies:
        pool = a.get("pool")
        if pool and pool not in pool_liq_cache:
            pool_liq_cache[pool] = current_pool_liquidity_usd(pool)
            print(f"[anomaly_diag] пул {pool[:12]}.. текущая ликвидность=${pool_liq_cache[pool]}", flush=True)
        a["pool_liquidity_usd_CURRENT_not_historical"] = pool_liq_cache.get(pool) if pool else None
        ratio_to_median = a.get("base_vs_median_base_ratio")
        a["base_price_orders_of_magnitude_below_median"] = (
            round(-math.log10(ratio_to_median), 2) if ratio_to_median else None
        )

    hypothesis_supported = sum(1 for a in anomalies if a.get("base_price_orders_of_magnitude_below_median")
                                and a["base_price_orders_of_magnitude_below_median"] >= 2)
    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "median_base_price_all_trades": median_base,
        "n_anomalies_found": len(anomalies),
        "n_anomalies_with_base_2plus_orders_below_median": hypothesis_supported,
        "hypothesis_divide_by_near_zero": (
            f"ПОДТВЕРЖДЕНА для {hypothesis_supported}/{len(anomalies)}: у этих сделок база (+5с) "
            "на 2+ порядка меньше типичной (медианы по всем сделкам) -- любое небольшое абсолютное "
            "движение цены даёт огромный процент." if hypothesis_supported > len(anomalies) / 2 and anomalies
            else f"НЕ подтверждена как основная причина для большинства ({hypothesis_supported}/{len(anomalies)} "
                 "имеют аномально низкую базу) -- нужна другая причина (см. anomalies по отдельности)."
        ) if anomalies else "аномалий не найдено в текущих данных",
        "anomalies": anomalies,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[anomaly_diag] записано в {OUT_PATH}: {out['hypothesis_divide_by_near_zero']}", flush=True)


if __name__ == "__main__":
    main()
