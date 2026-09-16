#!/usr/bin/env python3
"""Владелец (2026-09-16): перепись дешевле, чем полный скан -- сначала
взять живые пулы из УЖЕ СОХРАНЁННЫХ файлов, БЕЗ единого нового вызова
eth_getLogs (сейчас ещё возможен rate limit после двух зависших
прогонов census).

Источник 1: data/task_arc_top_pools_symbols_result.json (в git) -- 30
топ-пулов по объёму за 1ч, уже содержит fee_initialize_pips и hooks
(decode'ено в предыдущем раунде, task_arc_hour_zero_audit.py).

Источник 2: data/task_arc_closed_cycles_result.json -- ПОЛНЫЙ результат
детектора циклов с маршрутом (currency0/1/fee/hooks на pool_id), но этот
файл существует ТОЛЬКО на VPS (не в git, см. паспорт) -- читаем его
ЛОКАЛЬНО на VPS (обычный файловый I/O, НЕ RPC), если он там есть.

Источник 3: data/task_arc_recon_pools_result.json (полный реестр,
24567 пулов, тоже на VPS) -- используется ТОЛЬКО чтобы посчитать общее
число уникальных hooks-адресов и топ-3 концентрацию -- НО этот файл НЕ
содержит hooks (только topics: pool_id/currency0/currency1/block_number,
подтверждено ранее) -- честно отмечаем, что этот конкретный вопрос
(уникальность хуков по ВСЕМУ реестру) этим файлом не закрывается, нужны
данные из источников 1/2 (частичное покрытие) как единственная база."""
from __future__ import annotations

import json
from pathlib import Path
from collections import Counter

REPO_ROOT_CANDIDATES = [
    Path("/home/bot/robinhood-chain-alpha"),
    Path(__file__).parent.parent,
]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"repo_root_used": str(root)}

    # === Источник 1: top_pools_symbols (в git, 30 пулов, fee+hooks уже есть) ===
    top_pools_path = data_dir.joinpath("task_arc_top_pools_symbols_result.json")
    top_pools = []
    if top_pools_path.exists():
        d1 = json.loads(top_pools_path.read_text())
        top_pools = d1.get("top_pools_enriched", [])
    result["source1_top_pools_symbols"] = {"path_exists": top_pools_path.exists(), "n_pools": len(top_pools)}

    # === Источник 2: closed_cycles_result (ТОЛЬКО на VPS, полный, с маршрутом) ===
    cycles_full_path = data_dir.joinpath("task_arc_closed_cycles_result.json")
    cycles_pools = []  # list of {pool_id, currency0, currency1, fee, hooks} если найдём
    cycles_pool_ids_seen = set()
    cycles_full_found = cycles_full_path.exists()
    result["source2_closed_cycles_full"] = {"path_exists": cycles_full_found, "path": str(cycles_full_path)}
    if cycles_full_found:
        d2 = json.loads(cycles_full_path.read_text())
        result["source2_closed_cycles_full"]["top_level_keys"] = list(d2.keys())
        # Структура заранее неизвестна с точностью (полный файл не читался
        # в этой сессии) -- ищем pool_id/fee/hooks в разумных местах без
        # падения на неожиданной форме.
        def walk_collect(obj):
            if isinstance(obj, dict):
                if "pool_id" in obj and ("fee" in obj or "fee_pips" in obj or "hooks" in obj):
                    pid = obj.get("pool_id")
                    if pid and pid not in cycles_pool_ids_seen:
                        cycles_pool_ids_seen.add(pid)
                        cycles_pools.append({
                            "pool_id": pid, "currency0": obj.get("currency0"), "currency1": obj.get("currency1"),
                            "fee_pips": obj.get("fee") if obj.get("fee") is not None else obj.get("fee_pips"),
                            "hooks": obj.get("hooks"),
                        })
                for v in obj.values():
                    walk_collect(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk_collect(v)
        walk_collect(d2)
    result["source2_closed_cycles_full"]["n_pools_with_fee_hooks_found"] = len(cycles_pools)

    # === Источник 3: реестр 24567 (для честной оговорки по охвату, БЕЗ fee/hooks) ===
    registry_path = data_dir.joinpath("task_arc_recon_pools_result.json")
    n_registry_pools = None
    if registry_path.exists():
        d3 = json.loads(registry_path.read_text())
        n_registry_pools = len(d3.get("initialize_events", {}).get("pools", []))
    result["source3_full_registry"] = {"path_exists": registry_path.exists(), "n_pools_total": n_registry_pools,
                                        "has_fee_hooks": False,
                                        "note": "реестр содержит только pool_id/currency0/currency1/block_number -- fee/hooks НЕ decode'ены в исходном скане (подтверждено чтением analysis/task_arc_recon_pools.py), эти 24567 НЕ входят в перепись по fee/hooks ниже"}

    # === Объединение живых пулов (источники 1+2) ===
    by_pool = {}
    for p in top_pools:
        by_pool[p["pool_id"]] = {"pool_id": p["pool_id"], "fee_pips": p["fee_initialize_pips"],
                                  "hooks": p["hooks"].lower() if p.get("hooks") else None,
                                  "n_swaps_1h": p.get("n_swaps"), "pair": p.get("pair_symbols"),
                                  "source": "top_pools_symbols"}
    for p in cycles_pools:
        if p["pool_id"] in by_pool:
            continue  # уже учтён из источника 1
        if p.get("fee_pips") is None and p.get("hooks") is None:
            continue
        by_pool[p["pool_id"]] = {"pool_id": p["pool_id"], "fee_pips": p.get("fee_pips"),
                                  "hooks": p["hooks"].lower() if p.get("hooks") else None,
                                  "n_swaps_1h": None, "pair": None, "source": "closed_cycles_routes"}

    live_pools = list(by_pool.values())
    result["n_unique_live_pools_with_fee_hooks_from_local_files"] = len(live_pools)

    fee_known = [p for p in live_pools if p["fee_pips"] is not None]
    fee_zero = [p for p in fee_known if p["fee_pips"] == 0]
    fee_nonzero = [p for p in fee_known if p["fee_pips"] > 0]
    result["fee_census"] = {
        "n_with_known_fee": len(fee_known),
        "n_fee_zero": len(fee_zero), "pct_fee_zero": (len(fee_zero) / len(fee_known) * 100) if fee_known else None,
        "n_fee_nonzero": len(fee_nonzero), "pct_fee_nonzero": (len(fee_nonzero) / len(fee_known) * 100) if fee_known else None,
    }
    result["nonzero_fee_histogram_pips"] = dict(sorted(Counter(p["fee_pips"] for p in fee_nonzero).items()))

    hooks_known = [p for p in live_pools if p["hooks"]]
    hook_counts = Counter(p["hooks"] for p in hooks_known)
    top_hooks = hook_counts.most_common(10)
    hook_breakdown = []
    for hook_addr, n in top_hooks:
        fees_for_hook = Counter(p["fee_pips"] for p in hooks_known if p["hooks"] == hook_addr and p["fee_pips"] is not None)
        hook_breakdown.append({"hook": hook_addr, "n_pools_in_this_sample": n,
                                "fee_pips_used": dict(sorted(fees_for_hook.items()))})
    result["top_hooks_in_sample"] = hook_breakdown
    result["n_unique_hooks_in_sample"] = len(hook_counts)
    top3_n = sum(n for _, n in top_hooks[:3])
    result["top3_hooks_share_of_sample"] = (top3_n / len(hooks_known) * 100) if hooks_known else None

    receiver = "0x47e7936ae9891e61c5123db720593c05de7120cc"
    receiver_pools = [p for p in live_pools if p["hooks"] == receiver]
    result["profit_receiver_hook_in_sample"] = {
        "hook": receiver, "n_pools": len(receiver_pools),
        "pct_of_sample": (len(receiver_pools) / len(hooks_known) * 100) if hooks_known else None,
    }

    result["all_live_pools"] = live_pools
    result["honest_caveat"] = (
        f"Это перепись ТОЛЬКО по {len(live_pools)} пулам, доступным без нового eth_getLogs (30 топ-пулов по объёму "
        f"+ пулы из маршрутов 267 циклов, если source2 найден). Полный реестр -- {n_registry_pools} пулов, "
        f"из них fee/hooks известны сейчас только для {len(live_pools)} ({(len(live_pools)/n_registry_pools*100) if n_registry_pools else 0:.2f}%). "
        f"Заявление 'топ-3 хука = вся перепись' НЕ проверяемо на полном реестре без нового скана -- "
        f"проверено только на этой выборке живых/торгуемых пулов."
    )

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_lp_census_from_local_files_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
