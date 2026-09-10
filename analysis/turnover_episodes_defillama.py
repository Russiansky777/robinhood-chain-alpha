#!/usr/bin/env python3
"""Задача 3 владельца (2026-09-10), п.2: "Ретро-сторожок ... по истории
DefiLlama (объём и TVL по дням, все сети, 90 дней): все эпизоды, где
пул с TVL >= $2M держал оборот > 20x TVL." Ноль кредитов (публичный
DefiLlama API, не Dune).

РЕАЛЬНАЯ проверка перед полным прогоном (не гадаем схему API):
`/yields/pools` (yields.llama.fi) -- текущий снимок 20000+ пулов,
реально содержит `tvlUsd` и `volumeUsd1d` (подтверждено WebSearch,
2026-09-10). НО это ТЕКУЩИЙ снимок одного дня, не история. Историю
даёт `/yields/chart/{pool}` -- подтверждено WebSearch как источник
исторических APY/TVL, но НЕ подтверждено, есть ли там volume --
ЭТОТ скрипт сначала проверяет это живьём на реальном ответе (Шаг 1),
и только если volume реально есть в чарте -- переходит к полному
90-дневному скану (Шаг 2). Если нет -- честно останавливается и
докладывает реальное ограничение, не пытается притвориться, что
метод работает."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

YIELDS_BASE = "https://yields.llama.fi"
OUT_PATH = Path("data/p3_guard_cache/turnover_episodes_defillama_result.json")
TVL_MIN_USD = 2_000_000.0
TURNOVER_MULT_MIN = 20.0
WINDOW_DAYS = 90
REQUEST_DELAY_S = 0.15  # честная вежливая пауза, публичный бесплатный API


def get(url: str, **kw) -> requests.Response:
    return requests.get(url, timeout=30, **kw)


def run() -> int:
    t0 = time.time()
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "purpose": "Ретро-сторожок: эпизоды пул TVL>=$2M с оборотом >20x TVL, 90 дней, 0 кредитов (публичный DefiLlama)",
        "tvl_min_usd": TVL_MIN_USD, "turnover_mult_min": TURNOVER_MULT_MIN, "window_days": WINDOW_DAYS,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("=== Шаг 1: реальный снимок /yields/pools -- сколько пулов с TVL>=$2M, и есть ли volume в /yields/chart ===")
    r = get(f"{YIELDS_BASE}/pools")
    r.raise_for_status()
    pools = r.json().get("data", [])
    print(f"[turnover_episodes] реальных пулов в снимке: {len(pools)}")
    big_pools = [p for p in pools if (p.get("tvlUsd") or 0) >= TVL_MIN_USD]
    print(f"[turnover_episodes] пулов с TVL>=${TVL_MIN_USD/1e6:.0f}M сейчас: {len(big_pools)}")
    result["n_pools_total_snapshot"] = len(pools)
    result["n_pools_tvl_ge_2m_now"] = len(big_pools)

    # РЕАЛЬНАЯ проверка схемы чарта -- пробуем ОДИН реальный пул
    probe_pool = big_pools[0] if big_pools else None
    if probe_pool is None:
        result["blocker"] = "нет ни одного пула с TVL>=$2M в текущем снимке -- странно, останавливаемся честно"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1
    probe_id = probe_pool["pool"]
    rc = get(f"{YIELDS_BASE}/chart/{probe_id}")
    rc.raise_for_status()
    chart_data = rc.json().get("data", [])
    result["probe_pool"] = {"pool": probe_id, "project": probe_pool.get("project"), "symbol": probe_pool.get("symbol"),
                             "chain": probe_pool.get("chain"), "n_chart_points": len(chart_data)}
    if chart_data:
        result["probe_chart_sample"] = chart_data[-1]
        print(f"[turnover_episodes] реальный последний элемент чарта пула {probe_id}: {json.dumps(chart_data[-1], ensure_ascii=False)}")
    has_volume_field = bool(chart_data) and any(
        k for k in chart_data[-1].keys() if "volume" in k.lower()
    )
    result["chart_has_volume_field"] = has_volume_field
    print(f"[turnover_episodes] есть ли поле volume в /chart/{{pool}}: {has_volume_field}")

    if not has_volume_field:
        result["blocker"] = (
            "РЕАЛЬНОЕ ОГРАНИЧЕНИЕ: /yields/chart/{pool} не содержит поля volume (только TVL/APY) -- "
            "исторического объёма по пулу за 90 дней через этот публичный эндпоинт нет. "
            "volumeUsd1d есть только в ТЕКУЩЕМ снимке /yields/pools (один день, не история). "
            "Полный 90-дневный скан оборота НЕВОЗМОЖЕН через публичный DefiLlama API как задумано -- "
            "останавливаюсь здесь, не гадаю на суррогатных метриках без явного решения владельца."
        )
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[turnover_episodes] ОСТАНОВЛЕНО: {result['blocker']}")
        return 1

    print("\n=== Шаг 2: реальный полный скан -- volume реально есть, продолжаем на всех пулах TVL>=$2M ===")
    episodes_by_pool: dict = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    for i, p in enumerate(big_pools):
        pid = p["pool"]
        try:
            rc = get(f"{YIELDS_BASE}/chart/{pid}")
            rc.raise_for_status()
            series = rc.json().get("data", [])
        except Exception as exc:  # noqa: BLE001
            continue
        time.sleep(REQUEST_DELAY_S)
        vol_key = next((k for k in (series[-1].keys() if series else []) if "volume" in k.lower()), None)
        if not vol_key:
            continue
        pool_episodes = []
        in_episode = False
        ep_start = None
        ep_peak = 0.0
        ep_days = 0
        for pt in series:
            ts = pt.get("timestamp") or pt.get("date")
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")) if isinstance(ts, str) else datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, TypeError):
                continue
            if dt < cutoff:
                continue
            tvl = pt.get("tvlUsd") or 0
            vol = pt.get(vol_key) or 0
            if tvl < TVL_MIN_USD or tvl <= 0:
                mult = 0
            else:
                mult = vol / tvl
            if mult >= TURNOVER_MULT_MIN:
                if not in_episode:
                    in_episode = True
                    ep_start = dt.isoformat()
                    ep_peak = mult
                    ep_days = 1
                else:
                    ep_peak = max(ep_peak, mult)
                    ep_days += 1
            else:
                if in_episode:
                    pool_episodes.append({"start": ep_start, "duration_days": ep_days, "peak_turnover_mult": ep_peak})
                in_episode = False
        if in_episode:
            pool_episodes.append({"start": ep_start, "duration_days": ep_days, "peak_turnover_mult": ep_peak,
                                   "still_active": True})
        if pool_episodes:
            episodes_by_pool[pid] = {"project": p.get("project"), "symbol": p.get("symbol"), "chain": p.get("chain"),
                                      "tvl_usd_now": p.get("tvlUsd"), "episodes": pool_episodes}
        if (i + 1) % 50 == 0:
            print(f"[turnover_episodes] обработано {i+1}/{len(big_pools)} пулов, найдено эпизодов у {len(episodes_by_pool)}")
            result["episodes_by_pool"] = episodes_by_pool
            result["n_pools_scanned_so_far"] = i + 1
            OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    result["episodes_by_pool"] = episodes_by_pool
    result["n_pools_scanned_so_far"] = len(big_pools)
    all_episodes = [{"pool": pid, **e, **{k: v for k, v in info.items() if k != "episodes"}}
                     for pid, info in episodes_by_pool.items() for e in info["episodes"]]
    durations = [e["duration_days"] for e in all_episodes]
    result["summary"] = {
        "n_episodes_total": len(all_episodes),
        "median_duration_days": sorted(durations)[len(durations) // 2] if durations else None,
        "n_still_active": sum(1 for e in all_episodes if e.get("still_active")),
    }
    result["runtime_s_total"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[turnover_episodes] ИТОГО: {json.dumps(result['summary'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
