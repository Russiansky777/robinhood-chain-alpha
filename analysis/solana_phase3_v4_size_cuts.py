#!/usr/bin/env python3
"""Владелец: три локальных (без Dune) разреза поверх уже посчитанных
цен Фазы 3 v4 -- дни, которые УЖЕ покрыты (см. data/solana_phase3_result_v4.json
days_covered), честно ограничиваемся ими, не выдумываем непосчитанные дни.

1. Лидер, первые входы (k=1) по корзинам размера входа -- рост +30/+60с.
2. Докупки лидера (k=2), только spend_sol_equiv >= 4.3 SOL.
3. Доля событий БЕЗ единой сделки в (t0, t0+30с] -- по группам: лидер,
   остальные, 29 отслеживаемых кандидатов. Определение (честно, это
   ПРОКСИ по уже скачанным 5 точкам цены, не прямой подсчёт сделок):
   событие считается "без сделки в окне" если
   (а) минт вообще не появился в выдаче Dune для этого дня, ИЛИ
   (б) price_before есть и price_plus30 == price_before (цена ни разу
       не обновилась от последней сделки ДО входа до +30с включительно), ИЛИ
   (в) price_before нет (в истории до входа вообще не было сделок в
       окне -300..0с) И price_plus30 тоже нет.
4. Таблица по 27 отслеживаемым кошелькам BATCH-1/2/3 (10+10+7,
   data/dbot_sieve_baseline.json) -- сигналы/сутки, медиана роста +30с,
   доля без сделок, n."""
from __future__ import annotations

import gzip
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_phase3_prices_and_answers_v4 import build_sample, growth as _growth, EVENTS_PATH, RAW_DIR, OUT_PATH  # noqa: E402

def growth(row, horizon):
    return _growth(row, horizon) if row else None


REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "data" / "dbot_sieve_baseline.json"
OUT_REPORT_PATH = REPO_ROOT / "data" / "solana_phase3_v4_size_cuts_result.json"
SIZE_BUCKETS = [("2-4.3", 2, 4.3), ("4.3-8", 4.3, 8), ("8-15", 8, 15), ("15+", 15, float("inf"))]


def day_str(epoch: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


def no_trade_in_30s(row: dict | None) -> bool:
    """См. докстринг модуля -- прокси по уже скачанным точкам, не прямой count."""
    if row is None:
        return True
    pb, p30 = row.get("price_before"), row.get("price_plus30")
    if pb is not None:
        return p30 is None or p30 == pb
    return p30 is None


def main() -> None:
    d = json.loads(EVENTS_PATH.read_text())
    leader = d["wallet_roles"]["leader"][0]
    candidates_29 = set(d["wallet_roles"]["candidates_29"])
    sample = build_sample(d["events_by_wallet"], d["wallet_roles"])

    result = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}
    days_covered = set(result.get("days_covered") or [])
    print(f"[size_cuts] дни в покрытии: {sorted(days_covered)}", flush=True)

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
    print(f"[size_cuts] событий в покрытых днях: {len(scoped)} из {len(sample)} всего в выборке", flush=True)

    out: dict = {"days_covered": sorted(days_covered), "n_events_in_covered_days": len(scoped)}

    # --- 1. Лидер, k=1, по корзинам размера ---
    leader_k1 = [e for e in scoped if e["wallet"] == leader and e["k"] == 1]
    bucket_rows = []
    for name, lo, hi in SIZE_BUCKETS:
        evs = [e for e in leader_k1 if lo <= (e.get("spend_sol_equiv") or -1) < hi]
        g30 = [x for x in (growth(prices_by_event.get(e["event_id"]), "price_plus30") for e in evs) if x is not None]
        g60 = [x for x in (growth(prices_by_event.get(e["event_id"]), "price_plus60") for e in evs) if x is not None]
        bucket_rows.append({"bucket_sol": name, "n": len(evs), "n_priced_30s": len(g30), "n_priced_60s": len(g60),
                             "median_growth_30s_pct": round(median(g30) * 100, 3) if g30 else None,
                             "median_growth_60s_pct": round(median(g60) * 100, 3) if g60 else None})
    out["leader_k1_by_size_bucket"] = bucket_rows

    # --- 2. Докупки лидера (k=2), только >=4.3 SOL ---
    leader_k2_big = [e for e in scoped if e["wallet"] == leader and e["k"] == 2 and (e.get("spend_sol_equiv") or -1) >= 4.3]
    g30 = [x for x in (growth(prices_by_event.get(e["event_id"]), "price_plus30") for e in leader_k2_big) if x is not None]
    out["leader_k2_ge_4_3_sol"] = {"n": len(leader_k2_big), "n_priced_30s": len(g30),
                                    "median_growth_30s_pct": round(median(g30) * 100, 3) if g30 else None}

    # --- 3. Доля событий без сделки в (t0,t0+30с] по группам ---
    def group_no_trade_share(evs: list) -> dict:
        flags = [no_trade_in_30s(prices_by_event.get(e["event_id"])) for e in evs]
        n_no_trade = sum(flags)
        return {"n": len(evs), "n_no_trade_in_30s": n_no_trade,
                "share_no_trade_in_30s": round(n_no_trade / len(evs), 4) if evs else None}

    leader_all = [e for e in scoped if e["wallet"] == leader]
    others_all = [e for e in scoped if e["wallet"] != leader]
    candidates_all = [e for e in scoped if e["wallet"] in candidates_29]
    out["no_trade_in_30s_by_group"] = {
        "leader": group_no_trade_share(leader_all),
        "others_all": group_no_trade_share(others_all),
        "candidates_29": group_no_trade_share(candidates_all),
        "note": "прокси по 5 точкам цены (price_before==price_plus30 или обе стороны отсутствуют), не прямой подсчёт сделок -- см. докстринг",
    }

    # --- 4. 27 кошельков BATCH-1/2/3 ---
    baseline = json.loads(BASELINE_PATH.read_text())
    wallets27 = []
    for b in ("BATCH-1", "BATCH-2", "BATCH-3"):
        for tw in (baseline["batches"][b].get("tracked_wallets") or []):
            wallets27.append((tw.get("address"), tw.get("remark"), b))

    by_wallet_evs: dict[str, list] = defaultdict(list)
    for e in scoped:
        by_wallet_evs[e["wallet"]].append(e)

    n_days_covered = len(days_covered)
    wallet_table = []
    for addr, remark, batch in wallets27:
        evs = by_wallet_evs.get(addr, [])
        fe = [e for e in evs if e["k"] == 1]
        n_ge2 = sum(1 for e in fe if (e.get("spend_sol_equiv") or 0) >= 2)
        g30 = [x for x in (growth(prices_by_event.get(e["event_id"]), "price_plus30") for e in fe) if x is not None]
        nt = group_no_trade_share(fe)
        wallet_table.append({
            "address": addr, "remark": remark, "source_batch": batch,
            "n_first_entries_in_covered_days": len(fe),
            "freq_ge2_per_covered_day": round(n_ge2 / n_days_covered, 3) if n_days_covered else None,
            "n_priced_30s": len(g30),
            "median_growth_30s_pct": round(median(g30) * 100, 3) if g30 else None,
            "share_no_trade_in_30s": nt["share_no_trade_in_30s"],
        })
    wallet_table.sort(key=lambda r: (-(r["median_growth_30s_pct"] or -999) if r["n_priced_30s"] >= 5 else -999))
    out["batch123_27_wallets"] = wallet_table

    OUT_REPORT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
