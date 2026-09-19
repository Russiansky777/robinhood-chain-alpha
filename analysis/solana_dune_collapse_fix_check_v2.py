#!/usr/bin/env python3
"""Владелец, 2026-09-19: v1 этой проверки показала ЛОЖНОЕ "подтверждение"
-- медиана улучшилась (0.837->0.998) чисто случайно: tx с n_sol_legs=2
СИСТЕМАТИЧЕСКИ дают new_ratio~1.98 (не 1.0!), это НЕ фикс, а НОВОЕ
искажение x2, которое в среднем по выборке взаимно погасилось с
корректными случаями. Гипотеза: одна из двух "SOL-ног" -- это
самоссылочная запись wrap/unwrap (sold_mint=SOL И bought_mint=SOL,
тот же минт), которую v1 ошибочно включил в сумму как реальную ногу
свопа. Проверяем на ТОМ ЖЕ закэшированном execution_id (0 новых
кредитов, тот же access pattern, что v1)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_collapse_fix_check_v2.json"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CACHED_EXECUTION_ID = "01M2VNXS4NZCYFZ2VWT7YJBGWJ"


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def main() -> None:
    flow = json.loads((REPO_ROOT / "data" / "solana_29_candidates_flow_7d_sol_equiv.json").read_text())
    events_by_sig = {}
    for addr, v in flow.get("wallets", {}).items():
        if not v:
            continue
        for e in v.get("first_entry_events") or []:
            if e.get("signature") and e.get("sol_equivalent"):
                events_by_sig[e["signature"]] = {"address": addr, **e}

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"step0_key_discovery": discovery, "cached_execution_id": CACHED_EXECUTION_ID}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    res = probe.results(CACHED_EXECUTION_ID)
    if res.get("http_status") != 200:
        result["HONEST_ANSWER"] = f"Не удалось перечитать -- http={res.get('http_status')}"
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    rows = (res["body"].get("result") or {}).get("rows") or []
    print(f"[v2] перечитано {len(rows)} строк (0 новых кредитов)", flush=True)

    by_tx: dict[str, list] = {}
    for row in rows:
        by_tx.setdefault(row["tx_id"], []).append(row)

    # --- Диагностика: сколько строк -- самоссылочные (sold_mint==bought_mint) ---
    n_self_ref = sum(1 for legs in by_tx.values() for lg in legs
                      if lg.get("sold_mint") and lg.get("sold_mint") == lg.get("bought_mint"))
    result["n_self_referential_rows_sold_eq_bought"] = n_self_ref
    print(f"[v2] самоссылочных строк (sold_mint==bought_mint) во ВСЕХ данных: {n_self_ref}", flush=True)

    examples_2sol = []
    comparisons = []
    for tx_id, legs in by_tx.items():
        e = events_by_sig.get(tx_id)
        if not e:
            continue
        sol_rows_all = [lg for lg in legs if lg.get("sold_mint") == SOL_MINT and lg.get("sold_amount")]
        sol_rows_real = [lg for lg in sol_rows_all if lg.get("bought_mint") != SOL_MINT]  # исключаем самоссылочные (wrap)
        if len(sol_rows_all) >= 2 and len(examples_2sol) < 10:
            examples_2sol.append({"tx_id": tx_id, "legs": legs})

        if sol_rows_real:
            new_size_v2 = sum(float(lg["sold_amount"]) for lg in sol_rows_real)
            basis = "sol"
        else:
            usdc_rows = [lg for lg in legs if lg.get("sold_mint") == USDC_MINT and lg.get("bought_mint") != USDC_MINT
                         and lg.get("sold_amount")]
            new_size_v2 = sum(float(lg["sold_amount"]) for lg in usdc_rows) if usdc_rows else None
            basis = "usdc" if usdc_rows else None

        our_size = e["sol_equivalent"]
        comparisons.append({
            "tx_id": tx_id, "n_legs": len(legs), "n_sol_rows_all": len(sol_rows_all),
            "n_sol_rows_real_excl_wrap": len(sol_rows_real), "new_size_v2": new_size_v2, "basis": basis,
            "our_size_sol": our_size,
            "new_v2_ratio_vs_ours": (new_size_v2 / our_size) if new_size_v2 and basis == "sol" else None,
        })

    result["examples_with_2plus_sol_legs_raw"] = examples_2sol[:5]
    ratios_v2 = [c["new_v2_ratio_vs_ours"] for c in comparisons if c["new_v2_ratio_vs_ours"]]
    result["n_comparisons"] = len(comparisons)
    result["new_v2_ratio_summary"] = ({"n": len(ratios_v2), "median": median(ratios_v2),
                                        "p10": pct(ratios_v2, 0.10), "p90": pct(ratios_v2, 0.90)}
                                       if ratios_v2 else None)
    print(f"[v2] новое отношение (исключены самоссылочные wrap-строки): {result['new_v2_ratio_summary']}", flush=True)

    # Отдельно -- та же подвыборка n_sol_rows_all>=2, чтобы честно увидеть, ушёл ли перекос x2
    multi_before = [c for c in comparisons if c["n_sol_rows_all"] >= 2 and c["new_v2_ratio_vs_ours"]]
    if multi_before:
        mratios = [c["new_v2_ratio_vs_ours"] for c in multi_before]
        result["multi_sol_leg_subset_after_fix"] = {"n": len(mratios), "median": median(mratios)}
        print(f"[v2] подвыборка исходно n_sol_rows_all>=2 ПОСЛЕ исключения wrap: {result['multi_sol_leg_subset_after_fix']}", flush=True)

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    new_median = result["new_v2_ratio_summary"]["median"] if result["new_v2_ratio_summary"] else None
    if new_median is not None and 0.90 <= new_median <= 1.10 and result.get("multi_sol_leg_subset_after_fix", {}).get("median") \
            and 0.85 <= result["multi_sol_leg_subset_after_fix"]["median"] <= 1.15:
        result["VERDICT"] = (
            f"ПОДТВЕРЖДЕНО ЧЕСТНО: медиана {new_median:.3f} по ВСЕЙ выборке И медиана "
            f"{result['multi_sol_leg_subset_after_fix']['median']:.3f} на подвыборке с несколькими SOL-ногами "
            "(перекос x2 устранён исключением самоссылочных wrap-строк) -- реальный фикс, не совпадение. "
            "Можно пересчитывать частоты по 29 кандидатам."
        )
    else:
        result["VERDICT"] = (
            f"Перекос x2 НЕ полностью устранён (медиана всей выборки={new_median}, "
            f"подвыборка multi-SOL-ног={result.get('multi_sol_leg_subset_after_fix')}) -- нужен ещё один взгляд "
            "на структуру строк, прежде чем доверять пересчёту 29 кандидатов."
        )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[v2] {result['VERDICT']}", flush=True)


if __name__ == "__main__":
    main()
