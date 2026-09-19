#!/usr/bin/env python3
"""Владелец, 2026-09-19: проверка гипотезы "0.756 -- почерк схлопывания,
не данных" (Task 1 этого сообщения). БЕЗ НОВОГО SQL-запроса -- берём
результаты УЖЕ ОПЛАЧЕННОГО execution_id (solana_dune_29_candidates_validation.py,
шаг 2, 625 строк, execution_id сохранён и валиден до 2026-12-18) через
probe.results(execution_id) -- это не re-execute, кредиты не тратятся
повторно, только фактическая стоимость самого HTTP-вызова к Dune (0).

Гипотеза: Jupiter иногда делит маршрут на 2-3 ветви (напр. 60% через
Raydium + 40% через Meteora) -- в dex_solana.trades это ОТДЕЛЬНЫЕ строки
с ОДНИМ tx_id, а прежний код брал ОДНУ строку (next(...)) вместо суммы
всех. Новый размер -- сумма sold_amount по ВСЕМ строкам транзакции, где
sold_mint = входная валюта (SOL/WSOL, иначе USDC как фоллбэк). Для
последовательных SOL->USDC->токен маршрутов считаем ТОЛЬКО первый шаг
(SOL-ноги), не складываем ещё и USDC-ногу той же цепочки (было бы
двойным счётом одних и тех же денег)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_collapse_fix_check.json"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CACHED_EXECUTION_ID = "01M2VNXS4NZCYFZ2VWT7YJBGWJ"  # solana_dune_29_candidates_validation.py, шаг2, 625 строк


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def main() -> None:
    flow = json.loads((REPO_ROOT / "data" / "solana_29_candidates_flow_7d_sol_equiv.json").read_text())
    events = []
    for addr, v in flow.get("wallets", {}).items():
        if not v:
            continue
        for e in v.get("first_entry_events") or []:
            if e.get("signature") and e.get("sol_equivalent"):
                events.append({"address": addr, **e})
    events_by_sig = {e["signature"]: e for e in events}

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"step0_key_discovery": discovery, "cached_execution_id": CACHED_EXECUTION_ID,
                     "note": "results() на уже оплаченном execution_id -- НЕ create_query/execute, новых кредитов не тратим"}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    res = probe.results(CACHED_EXECUTION_ID)
    result["fetch_http_status"] = res.get("http_status")
    if res.get("http_status") != 200:
        result["HONEST_ANSWER"] = f"Не удалось перечитать закэшированный результат (http={res.get('http_status')}) -- возможно, истёк раньше срока."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[collapse_fix] " + result["HONEST_ANSWER"], flush=True)
        return
    rows = (res["body"].get("result") or {}).get("rows") or []
    result["n_rows_refetched"] = len(rows)
    print(f"[collapse_fix] перечитано {len(rows)} строк из кэша (0 новых кредитов)", flush=True)

    by_tx: dict[str, list] = {}
    for row in rows:
        by_tx.setdefault(row["tx_id"], []).append(row)

    n_rows_per_tx_dist: dict[int, int] = {}
    comparisons = []
    for tx_id, legs in by_tx.items():
        e = events_by_sig.get(tx_id)
        if not e:
            continue
        n_rows_per_tx_dist[len(legs)] = n_rows_per_tx_dist.get(len(legs), 0) + 1

        # СТАРЫЙ метод (как было): одна SOL-нога (next()), требует ещё и target_leg
        old_sol_leg = next((lg for lg in legs if lg.get("sold_mint") == SOL_MINT), None)
        old_target_leg = next((lg for lg in legs if lg.get("bought_mint") == e["mint"]), None)
        old_size = (float(old_sol_leg["sold_amount"])
                    if old_sol_leg and old_target_leg and old_sol_leg.get("sold_amount") else None)

        # НОВЫЙ метод: сумма по ВСЕМ строкам с sold_mint=SOL (первый шаг от
        # кошелька) -- если таких нет, фоллбэк на сумму по sold_mint=USDC
        sol_rows = [lg for lg in legs if lg.get("sold_mint") == SOL_MINT and lg.get("sold_amount")]
        if sol_rows:
            new_size = sum(float(lg["sold_amount"]) for lg in sol_rows)
            basis = "sol"
        else:
            usdc_rows = [lg for lg in legs if lg.get("sold_mint") == USDC_MINT and lg.get("sold_amount")]
            new_size = sum(float(lg["sold_amount"]) for lg in usdc_rows) if usdc_rows else None
            basis = "usdc" if usdc_rows else None

        our_size = e["sol_equivalent"]  # уже в SOL-эквиваленте (наш RPC расчёт)
        # basis="usdc" -- размер в USDC, не сравним напрямую с our_size (SOL) без курса;
        # честно помечаем и не считаем ratio для этих строк здесь (курс не тянем -- нет новых внешних вызовов).
        comparisons.append({
            "tx_id": tx_id, "address": e["address"], "mint": e["mint"], "n_legs": len(legs),
            "n_sol_legs": len(sol_rows), "old_size_sol": old_size, "new_size_basis": basis,
            "new_size": new_size, "our_size_sol": our_size,
            "old_ratio_vs_ours": (old_size / our_size) if old_size else None,
            "new_ratio_vs_ours": (new_size / our_size) if new_size and basis == "sol" else None,
        })

    result["n_rows_per_tx_distribution"] = dict(sorted(n_rows_per_tx_dist.items()))
    result["n_comparisons_total"] = len(comparisons)

    old_ratios = [c["old_ratio_vs_ours"] for c in comparisons if c["old_ratio_vs_ours"]]
    new_ratios_sol_basis = [c["new_ratio_vs_ours"] for c in comparisons if c["new_ratio_vs_ours"]]
    result["old_ratio_summary"] = {"n": len(old_ratios), "median": median(old_ratios),
                                    "p10": pct(old_ratios, 0.10), "p90": pct(old_ratios, 0.90)} if old_ratios else None
    result["new_ratio_summary_sol_basis_only"] = ({"n": len(new_ratios_sol_basis), "median": median(new_ratios_sol_basis),
                                                    "p10": pct(new_ratios_sol_basis, 0.10), "p90": pct(new_ratios_sol_basis, 0.90)}
                                                   if new_ratios_sol_basis else None)

    n_multi_sol_leg = sum(1 for c in comparisons if c["n_sol_legs"] >= 2)
    result["n_tx_with_multiple_sol_legs"] = n_multi_sol_leg
    print(f"[collapse_fix] распределение строк/tx: {result['n_rows_per_tx_distribution']}", flush=True)
    print(f"[collapse_fix] tx с >=2 SOL-ногами (гипотеза сплита): {n_multi_sol_leg}/{len(comparisons)}", flush=True)
    print(f"[collapse_fix] СТАРОЕ отношение: {result['old_ratio_summary']}", flush=True)
    print(f"[collapse_fix] НОВОЕ отношение (только sol-базис): {result['new_ratio_summary_sol_basis_only']}", flush=True)

    with_old_and_new = [c for c in comparisons if c.get("old_ratio_vs_ours") and c.get("new_ratio_vs_ours")]
    worst = sorted(with_old_and_new, key=lambda c: abs(c["new_ratio_vs_ours"] - 1), reverse=True)[:10]
    result["worst_10_before_after"] = worst
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    new_median = result["new_ratio_summary_sol_basis_only"]["median"] if result["new_ratio_summary_sol_basis_only"] else None
    if new_median is not None and 0.90 <= new_median <= 1.10:
        result["VERDICT"] = (
            f"ГИПОТЕЗА ПОДТВЕРДИЛАСЬ: суммирование SOL-ног даёт медиану {new_median:.3f} (было {result['old_ratio_summary']['median']:.3f}) "
            "-- это был почерк схлопывания (Jupiter split-route), не проблема данных. Нужен пересчёт частот по 29 "
            "кандидатам с исправленным размером и повторное сравнение с table_v2 (следующий шаг)."
        )
    elif new_median is not None:
        result["VERDICT"] = (
            f"Гипотеза НЕ подтвердилась в достаточной мере: медиана после фикса {new_median:.3f} (было "
            f"{result['old_ratio_summary']['median']:.3f}) -- вне диапазона 0.90-1.10. ВЕРДИКТ 'НЕГОДЕН' ОКОНЧАТЕЛЬНЫЙ, "
            "дальше Dune для массового прогона не используем."
        )
    else:
        result["VERDICT"] = "Недостаточно сопоставимых строк (basis=sol) для вывода -- честно, не считаем."
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[collapse_fix] {result['VERDICT']}", flush=True)


if __name__ == "__main__":
    main()
