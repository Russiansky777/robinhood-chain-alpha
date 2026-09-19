#!/usr/bin/env python3
"""Владелец, п.4: ретроспектива -- сколько из событий лидера (182,
k1+k2 за 5 покрытых дней) и 29 кандидатов приходятся на пулы Pump.fun
AMM (pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA), и какая у НИХ доля
«без сделок за 30 с» (прокси по уже скачанным ценам Dune) против
остальных пулов. Если у Pump.fun AMM событий эта доля заметно выше --
цифра 59.9% для лидера была искажена тем, что Dune v4 SQL (dex_solana.trades)
и/или наш RPC-декодер (engine.py, до этой правки) не видели сделки на
Pump.fun AMM вообще, а не тем, что там реально не было сделок.

Метод: та же когорта событий, что и solana_phase3_v4_size_cuts.py
(build_sample + days_covered), но для каждого события напрямую по RPC
(getTransaction по tx_id) смотрим top-level+inner программы -- если
среди них pAMMBay6... -- это Pump.fun AMM. Только RPC, Dune не
трогаем (уже скачанные price_before/plus30 берём из локального кэша
data/solana_phase3_raw_v4/*.json.gz)."""
from __future__ import annotations

import gzip
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_phase3_prices_and_answers_v4 import build_sample, EVENTS_PATH, RAW_DIR, OUT_PATH  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_REPORT_PATH = REPO_ROOT / "data" / "solana_phase3_pool_type_retro_result.json"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def day_str(epoch: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


def no_trade_in_30s(row: dict | None) -> bool:
    if row is None:
        return True
    pb, p30 = row.get("price_before"), row.get("price_plus30")
    if pb is not None:
        return p30 is None or p30 == pb
    return p30 is None


def tx_program_ids(tx: dict) -> set[str]:
    meta = tx.get("meta") or {}
    msg = tx["transaction"]["message"]
    ids = {ix.get("programId") for ix in msg.get("instructions", []) if isinstance(ix, dict)}
    for g in meta.get("innerInstructions") or []:
        for ix in g.get("instructions", []):
            if isinstance(ix, dict):
                ids.add(ix.get("programId"))
    return {i for i in ids if i}


def is_pump_amm_tx(tx_id: str, cache: dict) -> bool | None:
    """None -- транзакцию не удалось получить (честно, не считаем её ни тем ни другим)."""
    if tx_id in cache:
        return cache[tx_id]
    tx = fp.get_transaction(tx_id)
    if tx is None or (tx.get("meta") or {}).get("err") is not None:
        cache[tx_id] = None
        return None
    result = PUMP_AMM_PROGRAM in tx_program_ids(tx)
    cache[tx_id] = result
    return result


def group_stats(evs: list[dict], prices_by_event: dict, cache: dict) -> dict:
    rows = []
    for e in evs:
        is_pamm = is_pump_amm_tx(e["tx_id"], cache)
        nt = no_trade_in_30s(prices_by_event.get(e["event_id"]))
        rows.append({"event_id": e["event_id"], "tx_id": e["tx_id"], "is_pump_amm": is_pamm, "no_trade_in_30s": nt})

    def share(subset):
        if not subset:
            return {"n": 0, "n_no_trade_in_30s": 0, "share_no_trade_in_30s": None}
        n_nt = sum(1 for r in subset if r["no_trade_in_30s"])
        return {"n": len(subset), "n_no_trade_in_30s": n_nt, "share_no_trade_in_30s": round(n_nt / len(subset), 4)}

    pamm_rows = [r for r in rows if r["is_pump_amm"] is True]
    other_rows = [r for r in rows if r["is_pump_amm"] is False]
    unresolved_rows = [r for r in rows if r["is_pump_amm"] is None]
    return {
        "n_total": len(rows),
        "pump_amm": share(pamm_rows),
        "non_pump_amm": share(other_rows),
        "unresolved_tx_fetch_failed": len(unresolved_rows),
    }


def main() -> None:
    d = json.loads(EVENTS_PATH.read_text())
    leader = d["wallet_roles"]["leader"][0]
    candidates_29 = set(d["wallet_roles"]["candidates_29"])
    sample = build_sample(d["events_by_wallet"], d["wallet_roles"])

    result_v4 = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}
    days_covered = set(result_v4.get("days_covered") or [])
    print(f"[pool_type_retro] дни в покрытии: {sorted(days_covered)}", flush=True)

    prices_by_event: dict[int, dict] = {}
    for day in days_covered:
        raw_path = RAW_DIR / f"{day}.json.gz"
        if not raw_path.exists():
            continue
        with gzip.open(raw_path, "rt", encoding="utf-8") as fh:
            cached = json.load(fh)
        for row in cached["rows"]:
            prices_by_event[row["event_id"]] = row

    scoped = [e for e in sample if day_str(e["block_time_epoch"]) in days_covered]
    leader_all = [e for e in scoped if e["wallet"] == leader]
    candidates_all = [e for e in scoped if e["wallet"] in candidates_29]
    print(f"[pool_type_retro] лидер: {len(leader_all)} событий, кандидаты: {len(candidates_all)} событий -- "
          f"итого {len(leader_all) + len(candidates_all)} RPC getTransaction", flush=True)

    cache: dict = {}
    out = {"days_covered": sorted(days_covered)}
    t0 = time.time()
    out["leader"] = group_stats(leader_all, prices_by_event, cache)
    print(f"[pool_type_retro] лидер готов за {time.time()-t0:.0f}с: {json.dumps(out['leader'], ensure_ascii=False)}", flush=True)
    t1 = time.time()
    out["candidates_29"] = group_stats(candidates_all, prices_by_event, cache)
    print(f"[pool_type_retro] кандидаты готовы за {time.time()-t1:.0f}с: {json.dumps(out['candidates_29'], ensure_ascii=False)}", flush=True)
    out["note"] = ("pump_amm/non_pump_amm определены прямым RPC getTransaction по каждому tx_id события "
                    "(наличие pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA среди top-level+inner программ). "
                    "no_trade_in_30s -- тот же прокси, что в solana_phase3_v4_size_cuts.py (price_before==price_plus30 "
                    "или обе стороны отсутствуют), НЕ прямой подсчёт сделок.")
    OUT_REPORT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
