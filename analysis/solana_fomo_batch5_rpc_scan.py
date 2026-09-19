#!/usr/bin/env python3
"""Владелец, п.4 (главное): RPC-скан кандидатов рейтинга Fomo -> топ-10 -> BATCH-5.

Неоднозначность, разрешена по умолчанию (без ожидания ответа):
  - "~230 кошельков рейтинга Fomo" не совпадает ТОЧНО ни с одним уже
    посчитанным числом в data/fomo_leaderboard_candidates.json (union=261,
    followers>100=257, активны на Solana за 3 суток=247) -- берём САМЫЙ
    отфильтрованный и уже RPC-подтверждённый список (247, поле
    final_table_sorted_by_followers), честно, не подгоняем цифру под 230.
  - "RPC-скан" -- прошлый фильтр (onchain_filter.py) уже делал ОДИН
    getSignaturesForAddress на кошелёк (только "была ли вообще активность
    за 3 суток", не подтверждал именно свопы). Этот скан идёт на шаг
    дальше: пробует до 5 последних подписей КАЖДОГО кошелька через
    getTransaction и проверяет, есть ли среди программ известный
    DEX/AMM (Jupiter/Raydium/Meteora/pump.fun/Orca) -- честное
    подтверждение реальной торговли, не просто "любая активность".
  - Ранжирование топ-10 -- лучший (минимальный) rank среди окон рейтинга
    Fomo (24ч/7д/30д/all), tie-break по pnlUsd в том же окне -- это и
    есть "рейтинг Fomo" в буквальном смысле; RPC-скан здесь ФИЛЬТР
    (только подтверждённо торгующие), не альтернативная метрика.

Бюджетируется и возобновляем, как onchain_filter.py -- не гарантируем
охватить все 247 за один прогон."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_PATH = REPO_ROOT / "data" / "fomo_leaderboard_candidates.json"
OUT_PATH = REPO_ROOT / "data" / "solana_fomo_batch5_rpc_scan_result.json"

TIME_BUDGET_S = 40 * 60
COMMIT_INTERVAL_S = 60
N_RECENT_SAMPLE = 5

KNOWN_DEX_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump.fun AMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
}


def program_ids_in_tx(tx: dict) -> set[str]:
    meta = tx.get("meta") or {}
    msg = tx["transaction"]["message"]
    ids = {ix.get("programId") for ix in msg.get("instructions", []) if isinstance(ix, dict)}
    for g in meta.get("innerInstructions") or []:
        for ix in g.get("instructions", []):
            if isinstance(ix, dict):
                ids.add(ix.get("programId"))
    return {i for i in ids if i}


def scan_wallet(address: str) -> dict:
    sigs = fp.get_signatures_for_address(address, limit=N_RECENT_SAMPLE)
    ok_sigs = [s["signature"] for s in sigs if s.get("err") is None]
    n_swap_like = 0
    dex_seen: set[str] = set()
    for sig in ok_sigs:
        tx = fp.get_transaction(sig)
        if tx is None or (tx.get("meta") or {}).get("err") is not None:
            continue
        pids = program_ids_in_tx(tx)
        hit = pids & KNOWN_DEX_PROGRAMS.keys()
        if hit:
            n_swap_like += 1
            dex_seen.update(KNOWN_DEX_PROGRAMS[p] for p in hit)
    return {"n_sampled": len(ok_sigs), "n_swap_like": n_swap_like,
            "swap_ratio_recent5": round(n_swap_like / len(ok_sigs), 3) if ok_sigs else None,
            "dex_programs_seen": sorted(dex_seen)}


def best_rank_entry(row: dict) -> tuple[int, float]:
    """(лучший rank среди окон, -pnlUsd того же окна) -- для сортировки
    по возрастанию (меньший rank = лучше, tie-break большим pnl)."""
    windows = row.get("windows_present") or []
    if not windows:
        return (10**9, 0.0)
    best = min(windows, key=lambda w: w.get("rank") if w.get("rank") is not None else 10**9)
    return (best.get("rank") if best.get("rank") is not None else 10**9, -(best.get("pnlUsd") or 0))


def main() -> None:
    if not CANDIDATES_PATH.exists():
        print("[fomo_batch5] нет data/fomo_leaderboard_candidates.json -- нечего сканировать.", flush=True)
        return
    candidates = json.loads(CANDIDATES_PATH.read_text())
    rows = candidates.get("final_table_sorted_by_followers") or []
    print(f"[fomo_batch5] кандидатов (followers>100, активны за 3д): {len(rows)}", flush=True)

    result = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {
        "source_n_candidates": len(rows), "note_230_vs_247": (
            "владелец упомянул ~230 кошельков, в data/fomo_leaderboard_candidates.json "
            "самый отфильтрованный (followers>100 + активность на Solana за 3 сут, "
            "уже подтверждено RPC) список -- 247, не 230; используем его как есть, "
            "не подгоняем."),
        "scanned": {},
    }
    scanned = result["scanned"]

    started_at = time.monotonic()
    last_commit_at = started_at
    n_done_this_run = 0
    for row in rows:
        addr = row["solana_address"]
        if addr in scanned:
            continue
        if time.monotonic() - started_at > TIME_BUDGET_S:
            print("[fomo_batch5] бюджет времени исчерпан -- остальное на следующий прогон", flush=True)
            break
        try:
            scanned[addr] = scan_wallet(addr)
        except RuntimeError as exc:
            scanned[addr] = {"error": str(exc)[:300]}
            print(f"[fomo_batch5] {addr[:12]}.. ошибка: {exc}", flush=True)
            continue
        n_done_this_run += 1
        print(f"[fomo_batch5] {addr[:12]}.. ({row.get('nickname')}) swap_ratio={scanned[addr].get('swap_ratio_recent5')} "
              f"dex={scanned[addr].get('dex_programs_seen')}", flush=True)
        if time.monotonic() - last_commit_at > COMMIT_INTERVAL_S:
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            fp._git_commit_progress("fomo_batch5_rpc_scan", [OUT_PATH])
            last_commit_at = time.monotonic()

    n_scanned = len(scanned)
    n_confirmed_trading = sum(1 for v in scanned.values() if (v.get("n_swap_like") or 0) > 0)
    print(f"[fomo_batch5] прогон: {n_done_this_run} просканировано сейчас; всего просканировано "
          f"{n_scanned}/{len(rows)}; подтверждённо торгуют (>=1 своп в выборке из {N_RECENT_SAMPLE}) = {n_confirmed_trading}", flush=True)

    # Топ-10 -- только среди УЖЕ просканированных и подтверждённо торгующих
    by_addr = {r["solana_address"]: r for r in rows}
    confirmed = [addr for addr, v in scanned.items() if (v.get("n_swap_like") or 0) > 0]
    confirmed.sort(key=lambda a: best_rank_entry(by_addr[a]))
    top10 = []
    for addr in confirmed[:10]:
        r = by_addr[addr]
        v = scanned[addr]
        best_w = min((w for w in (r.get("windows_present") or [])), key=lambda w: w.get("rank") if w.get("rank") is not None else 10**9, default=None)
        top10.append({
            "address": addr, "name": r.get("nickname"),
            "followers": r.get("followers"),
            "best_window": best_w.get("window") if best_w else None,
            "best_rank": best_w.get("rank") if best_w else None,
            "pnlUsd_in_best_window": best_w.get("pnlUsd") if best_w else None,
            "swap_ratio_recent5": v.get("swap_ratio_recent5"),
            "dex_programs_seen": v.get("dex_programs_seen"),
        })

    result["n_scanned_total"] = n_scanned
    result["n_confirmed_trading"] = n_confirmed_trading
    result["top10_batch5_candidates"] = top10
    result["top10_address_name_csv"] = "\n".join(f"{t['address']},{t['name']}" for t in top10)
    result["complete"] = n_scanned >= len(rows)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print("=== ТОП-10 (address,name) для BATCH-5 ===", flush=True)
    print(result["top10_address_name_csv"], flush=True)
    print(f"[fomo_batch5] complete={result['complete']} ({n_scanned}/{len(rows)})", flush=True)


if __name__ == "__main__":
    main()
