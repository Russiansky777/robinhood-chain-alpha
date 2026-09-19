#!/usr/bin/env python3
"""Владелец, 2026-09-19: Фаза 3 v4 -- исправление найденного бага v3.
Диагностика (data/solana_phase3_zero_diag_gecko_result.json, бесплатно
через GeckoTerminal): 19/20 событий с "ровным нулём" у Dune реально
имели объём торгов в том же окне -- у 17/20 из них топ-пул по
ликвидности НЕ против WSOL (USDC или иной актив), а v3 матчил ТОЛЬКО
пары с WSOL (`token_sold_mint_address = WSOL_MINT` и наоборот) -- отсюда
и ложные нули, и заниженные медианы роста.

v4: убираем требование WSOL-контрагента совсем. Берём ЛЮБУЮ сделку
минта в dex_solana.trades (минт в token_bought_mint_address ИЛИ
token_sold_mint_address, любая вторая нога), цена = amount_usd (Dune
уже даёт в USD, не нужен курс SOL/USD) / количество ЭТОГО токена в
сделке. UNION ALL двух отдельных equi-джойнов (не OR по двум колонкам --
см. инцидент v2 с катастрофической стоимостью на голом OR). Дедуп по
tx_id -- ГРУППИРУЕМ по (tx_id, event_id) ПЕРЕД max_by-агрегацией, чтобы
многоногая транзакция, тронувшая минт больше одного раза, не давала
дублей.

Владелец подтвердил: день 09-19 в Dune НЕ материализован (текущий день)
-- полностью исключён из обработки, не тратим на него ничего.

Бюджет -- НОВЫЙ, по факту сверки с кабинетом Dune (использовано 1374 из
2500 на момент сверки; учёт execution_cost_credits в v2/v3 завышал
расход примерно в 3.3 раза, причину найти не смог -- см. отчёт
владельцу). GLOBAL_PRIOR_SPEND_OTHER_STEPS=0 -- это НОВЫЙ, отдельный
бюджет на пересчёт по исправленному запросу, не связанный с прежним
(уже неточным) учётом. Кэп 900, резерв 200 владелец держит СВЕРХ этого
кэпа сам (по кабинету) -- используем execution_cost_credits как
консервативный (возможно тоже завышенный) верхний предел: если поле
всё ещё завышает, реальная трата останется ниже -- бюджет не может быть
превышен независимо от того, разгадана ли природа завышения.

Контроль ПОСЛЕ КАЖДОГО дня (не только первого) -- бесплатно, по уже
скачанным данным: сравнение роста +30с по первым входам лидера против
RPC-эталона (data/solana_buyer_200/selected_300.json + step_extended,
опорное +20.70% на n=37) и против сигнатур лидера, за которыми реально
шёл пилот (data/solana_dbot_realized_ledger.json)."""
from __future__ import annotations

import gzip
import json
import os
import random
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
EVENTS_PATH = REPO_ROOT / "data" / "solana_phase2_events_v3.json"
OUT_PATH = REPO_ROOT / "data" / "solana_phase3_result_v4.json"
RAW_DIR = REPO_ROOT / "data" / "solana_phase3_raw_v4"
BASELINE_PATH = REPO_ROOT / "data" / "dbot_sieve_baseline.json"
LEDGER_PATH = REPO_ROOT / "data" / "solana_dbot_realized_ledger.json"
SELECTED_300_PATH = REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json"
STEP_EXTENDED_PATH = REPO_ROOT / "data" / "solana_buyer_200" / "step_extended_result.json"

LOOKBACK_S = 300
HORIZONS = [("plus5", 5), ("plus30", 30), ("plus60", 60), ("plus180", 180)]
MAX_HORIZON_S = 180
FETCH_HI_BUFFER_S = 5
CHUNK_SIZE = 100  # владелец: день 09-15 (340 событий, 1 чанк по старому 700) упал с
# FAILED_TYPE_RESOURCES_CAP_REACHED, реально списав 101.59 кредита -- уменьшаем размер
# запроса, чтобы держаться под лимитом ресурсов на исполнение, не по длине SQL-текста
MAX_TOTAL_COST_CREDITS = 1000.0  # предохранитель за один прогон -- только от катастрофического сбоя,
# НЕ реальный бюджетный лимит (владелец: решения об остановке -- только по кабинету Dune, собственный
# счётчик execution_cost_credits завышал ~3.3x в прошлых прогонах и не сверен с кабинетом)
MAX_DAYS_PER_RUN = 1
EXCLUDED_DAYS = {"2026-09-19"}  # владелец подтвердил: текущий день не материализован в Dune, не трогаем
GLOBAL_PRIOR_SPEND_OTHER_STEPS = 0.0  # новый, отдельный бюджет на пересчёт по исправленному запросу
GLOBAL_BUDGET_CAP = 5000.0  # см. комментарий у MAX_TOTAL_COST_CREDITS -- только катастрофический предохранитель
FATAL_ERROR_MARKERS = ("RESOURCES_CAP_REACHED", "exceed your configured", "datapoint limit")
ROUND_TRIP_COST_FRAC = 0.06
SIZE_BUCKETS = [("2-4.3", 2, 4.3), ("4.3-8", 4.3, 8), ("8-15", 8, 15), ("15+", 15, float("inf"))]
SEED = 20260919
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"


def day_str(epoch: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


def size_bucket(spend: float) -> str | None:
    for name, lo, hi in SIZE_BUCKETS:
        if lo <= spend < hi:
            return name
    return None


def build_sample(events_by_wallet: dict, wallet_roles: dict) -> list[dict]:
    """Полная выборка (не сокращённая) -- та же логика, что v3
    (reduced=False): лидер -- все k1/k2; остальные -- стратифицированный
    пул (800 k1/300 k2/150 k3+) + топ-30 по частоте до 30 k1/кошелёк."""
    rng = random.Random(SEED)
    leader = wallet_roles["leader"][0]
    candidates_29 = set(wallet_roles["candidates_29"])

    flat = []
    eid = 0
    for wallet, events in events_by_wallet.items():
        for e in events:
            flat.append({**e, "wallet": wallet, "event_id": eid})
            eid += 1

    leader_evs = [e for e in flat if e["wallet"] == leader and e["k"] in (1, 2)]
    other_evs = [e for e in flat if e["wallet"] != leader]
    k1 = [e for e in other_evs if e["k"] == 1]
    k2 = [e for e in other_evs if e["k"] == 2]
    k3p = [e for e in other_evs if e["k"] >= 3]

    by_bucket: dict[str, list] = defaultdict(list)
    for e in k1:
        b = size_bucket(e["spend_sol_equiv"])
        if b:
            by_bucket[b].append(e)
    target_per_bucket = 800 // len(SIZE_BUCKETS)
    k1_sample = []
    for name, _, _ in SIZE_BUCKETS:
        pool = by_bucket.get(name, [])
        n_take = max(60, target_per_bucket) if len(pool) >= max(60, target_per_bucket) else len(pool)
        k1_sample.extend(rng.sample(pool, n_take) if len(pool) >= n_take else pool)
    k2_sample = rng.sample(k2, min(300, len(k2)))
    k3p_sample = rng.sample(k3p, min(150, len(k3p)))

    by_wallet_new: dict[str, list] = defaultdict(list)
    for e in other_evs:
        if e["wallet"] not in candidates_29:
            by_wallet_new[e["wallet"]].append(e)
    freqs = []
    for w, evs in by_wallet_new.items():
        fe = [e for e in evs if e["k"] == 1]
        n_ge2 = sum(1 for e in fe if e["spend_sol_equiv"] >= 2)
        freqs.append((w, n_ge2 / 7.0))
    freqs.sort(key=lambda x: -x[1])
    top30_wallets = {w for w, _ in freqs[:30]}
    top30_extra = []
    for w in top30_wallets:
        wk1 = [e for e in by_wallet_new[w] if e["k"] == 1]
        top30_extra.extend(wk1[:30])

    seen_ids = set()
    sample = []
    for e in leader_evs + k1_sample + k2_sample + k3p_sample + top30_extra:
        if e["event_id"] not in seen_ids:
            seen_ids.add(e["event_id"])
            sample.append(e)
    return sample


def build_price_sql(batch: list[dict]) -> str:
    """v4 -- любая сделка минта, любая вторая нога, цена из amount_usd
    (Dune уже даёт в USD -- не нужен отдельный курс SOL/USD). UNION ALL
    двух equi-джойнов (bought=mint / sold=mint) -- не OR по двум колонкам.
    Дедуп по tx_id: группируем (tx_id,event_id,t0) в отдельной агрегации
    ПЕРЕД финальным max_by по времени, чтобы многоногая транзакция не
    давала лишних строк на один и тот же event_id."""
    values = ",".join(
        f"({e['event_id']}, '{e['mint']}', {e['block_time_epoch'] - LOOKBACK_S}, "
        f"{e['block_time_epoch'] + MAX_HORIZON_S + FETCH_HI_BUFFER_S}, {e['block_time_epoch']})"
        for e in batch
    )
    sql = f"""
WITH ev(event_id, mint, lo, hi, t0) AS (VALUES {values}),
raw AS (
  SELECT dt.tx_id, dt.block_time, dt.token_bought_amount AS token_amount, dt.amount_usd,
         e.event_id, e.t0
  FROM dex_solana.trades dt JOIN ev e
    ON dt.token_bought_mint_address = e.mint
   AND dt.block_time BETWEEN from_unixtime(e.lo) AND from_unixtime(e.hi)
  UNION ALL
  SELECT dt.tx_id, dt.block_time, dt.token_sold_amount AS token_amount, dt.amount_usd,
         e.event_id, e.t0
  FROM dex_solana.trades dt JOIN ev e
    ON dt.token_sold_mint_address = e.mint
   AND dt.block_time BETWEEN from_unixtime(e.lo) AND from_unixtime(e.hi)
),
priced_raw AS (
  SELECT tx_id, event_id, t0, block_time,
         try_cast(amount_usd AS double) / NULLIF(try_cast(token_amount AS double), 0) AS price
  FROM raw
  WHERE try_cast(token_amount AS double) IS NOT NULL AND try_cast(token_amount AS double) != 0
),
priced AS (
  SELECT tx_id, event_id, t0, max(block_time) AS block_time, max(price) AS price
  FROM priced_raw GROUP BY tx_id, event_id, t0
)
SELECT event_id,
  max_by(price, block_time) FILTER (WHERE block_time < from_unixtime(t0)) AS price_before,
  max_by(price, block_time) FILTER (WHERE block_time <= from_unixtime(t0+5)) AS price_plus5,
  max_by(price, block_time) FILTER (WHERE block_time <= from_unixtime(t0+30)) AS price_plus30,
  max_by(price, block_time) FILTER (WHERE block_time <= from_unixtime(t0+60)) AS price_plus60,
  max_by(price, block_time) FILTER (WHERE block_time <= from_unixtime(t0+180)) AS price_plus180
FROM priced GROUP BY event_id
"""
    return sql


def growth(prices: dict, horizon: str):
    base, val = prices.get("price_plus5"), prices.get(horizon)
    if not base or not val or base <= 0:
        return None
    return val / base - 1.0


def control_check(sample: list[dict], prices_by_event: dict, leader: str) -> dict:
    """Бесплатный контроль на уже скачанных данных: рост +30с по первым
    входам лидера -- Dune (v4) против RPC-эталона (selected_300 +
    step_extended, opорное +20.70% n=37) и против сигнатур лидера, за
    которыми реально шёл пилот (ведомость)."""
    out: dict = {}
    if SELECTED_300_PATH.exists() and STEP_EXTENDED_PATH.exists():
        selected = {r["signature"]: r for r in json.loads(SELECTED_300_PATH.read_text())}
        short = {}
        for r in json.loads(STEP_EXTENDED_PATH.read_text()):
            if "seconds" in r:
                short[(r["signature"], r["seconds"])] = r

        def rpc_price(sig, sec):
            e = short.get((sig, sec))
            if not e or e.get("mine_status") != "ok" or e.get("mine_price") is None:
                return None
            return float(e["mine_price"])

        leader_events = [e for e in sample if e["wallet"] == leader]
        first_entry_joined = [e for e in leader_events
                               if e["tx_id"] in selected and selected[e["tx_id"]].get("zero_balance")]
        rows = []
        for e in first_entry_joined:
            sig = e["tx_id"]
            row = prices_by_event.get(e["event_id"])
            dune_g30 = growth(row, "price_plus30") if row else None
            rpc_base, rpc_30 = rpc_price(sig, 5), rpc_price(sig, 30)
            rpc_g30 = (rpc_30 / rpc_base - 1.0) if (rpc_base and rpc_30) else None
            if dune_g30 is not None or rpc_g30 is not None:
                rows.append({"tx_id": sig, "dune_g30": dune_g30, "rpc_g30": rpc_g30})
        d30 = [r["dune_g30"] for r in rows if r["dune_g30"] is not None]
        r30 = [r["rpc_g30"] for r in rows if r["rpc_g30"] is not None]
        out["leader_vs_rpc_reference"] = {
            "n_intersection_total": len(first_entry_joined), "n_dune_priced": len(d30), "n_rpc_priced": len(r30),
            "median_dune_g30_pct": round(median(d30) * 100, 3) if d30 else None,
            "median_rpc_g30_pct": round(median(r30) * 100, 3) if r30 else None,
            "reference_pct": 20.70,
        }

    if LEDGER_PATH.exists():
        ledger = json.loads(LEDGER_PATH.read_text())
        by_tx = defaultdict(list)
        for e in sample:
            if e["wallet"] == leader:
                by_tx[e["tx_id"]].append(e)
        pilot_trades = [t for t in ledger["trades"] if t.get("label") == "pilot" and t.get("status") == "ok"]
        rows = []
        for t in pilot_trades:
            lsig = t.get("leader_signature")
            evs = by_tx.get(lsig, [])
            match = next((e for e in evs if e["event_id"] in prices_by_event), None)
            dune_g30 = growth(prices_by_event[match["event_id"]], "price_plus30") if match else None
            rows.append({"mint": t["mint"], "leader_signature": lsig,
                         "our_markup_at_entry_pct": t.get("markup_at_entry_pct"),
                         "our_gross_pct": t.get("gross_pct"),
                         "dune_g30_pct": (dune_g30 * 100) if dune_g30 is not None else None,
                         "found_in_dune_output": match is not None})
        out["pilot_followed_leader_signals"] = rows
    return out


def build_answer_tables(sample: list[dict], prices_by_event: dict, wallet_roles: dict) -> dict:
    leader = wallet_roles["leader"][0]

    def bucket_k(k):
        return "k1" if k == 1 else ("k2" if k == 2 else "k3plus")

    rebuys = {"leader": {}, "others": {}}
    for group_name, is_leader in (("leader", True), ("others", False)):
        by_k = {"k1": [], "k2": [], "k3plus": []}
        for e in sample:
            if (e["wallet"] == leader) != is_leader:
                continue
            p = prices_by_event.get(e["event_id"])
            if p:
                by_k[bucket_k(e["k"])].append(e)
        for kname, evs in by_k.items():
            gs = {h: [x for x in (growth(prices_by_event[e["event_id"]], h) for e in evs) if x is not None]
                  for h in ("price_plus30", "price_plus60", "price_plus180")}
            rebuys[group_name][kname] = {"n": len(evs),
                                          **{f"n_priced_{h.replace('price_', '')}": len(v) for h, v in gs.items()},
                                          **{f"median_growth_{h.replace('price_', '')}": (median(v) if v else None) for h, v in gs.items()}}
        k2 = rebuys[group_name]["k2"]
        k2["take_k2_rule"] = (k2.get("median_growth_plus30") is not None and k2["n_priced_plus30"] >= 30
                               and (k2["median_growth_plus30"] - ROUND_TRIP_COST_FRAC) > 0)

    threshold_table = []
    for name, lo, hi in SIZE_BUCKETS:
        evs = [e for e in sample if e["k"] == 1 and e["wallet"] != leader and lo <= e["spend_sol_equiv"] < hi
               and e["event_id"] in prices_by_event]
        g30 = [x for x in (growth(prices_by_event[e["event_id"]], "price_plus30") for e in evs) if x is not None]
        g60 = [x for x in (growth(prices_by_event[e["event_id"]], "price_plus60") for e in evs) if x is not None]
        threshold_table.append({"bucket_sol": name, "n": len(evs), "n_priced_30s": len(g30), "n_priced_60s": len(g60),
                                 "median_growth_30s": median(g30) if g30 else None,
                                 "median_growth_60s": median(g60) if g60 else None})
    threshold_sol = next((r["bucket_sol"] for r in threshold_table
                          if r["median_growth_30s"] is not None and r["median_growth_30s"] >= 2 * ROUND_TRIP_COST_FRAC), None)

    by_wallet_new: dict[str, list] = defaultdict(list)
    candidates_29 = set(wallet_roles["candidates_29"])
    for e in sample:
        if e["wallet"] != leader and e["wallet"] not in candidates_29:
            by_wallet_new[e["wallet"]].append(e)

    def wallet_row(w: str, evs: list) -> dict:
        fe = [e for e in evs if e["k"] == 1]
        n_ge2 = sum(1 for e in fe if e["spend_sol_equiv"] >= 2)
        g30 = [x for x in (growth(prices_by_event[e["event_id"]], "price_plus30") for e in fe if e["event_id"] in prices_by_event) if x is not None]
        row = {"address": w, "freq_ge2_per_day": round(n_ge2 / 7.0, 3), "n_priced_30s": len(g30),
               "median_growth_30s": median(g30) if g30 else None}
        row["rank_score"] = (row["freq_ge2_per_day"] * row["median_growth_30s"]
                              if len(g30) >= 20 and row["median_growth_30s"] is not None else None)
        return row

    wallet_rows = [wallet_row(w, evs) for w, evs in by_wallet_new.items()]
    ranked = sorted([r for r in wallet_rows if r["rank_score"] is not None], key=lambda r: -r["rank_score"])

    batch4_verdict = []
    baseline = json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.exists() else {}
    batch4_wallets = (baseline.get("batches", {}).get("BATCH-4", {}) or {}).get("tracked_wallets") or []
    for tw in batch4_wallets:
        addr = tw.get("address") if isinstance(tw, dict) else tw
        evs = by_wallet_new.get(addr, [])
        row = wallet_row(addr, evs)
        row["remark"] = tw.get("remark") if isinstance(tw, dict) else None
        row["n_ge20_priced_met"] = row["n_priced_30s"] >= 20
        batch4_verdict.append(row)

    return {"q1_rebuys": rebuys,
            "q2_threshold_and_size": {"table": threshold_table, "threshold_sol_lower_bound": threshold_sol},
            "q3_new_wallets": {"n_wallets_evaluated": len(wallet_rows), "top10": ranked[:10]},
            "batch4_10_wallets_verdict": batch4_verdict}


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    d = json.loads(EVENTS_PATH.read_text())
    leader = d["wallet_roles"]["leader"][0]
    sample = build_sample(d["events_by_wallet"], d["wallet_roles"])

    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in sample:
        ds = day_str(e["block_time_epoch"])
        if ds in EXCLUDED_DAYS:
            continue
        by_day[ds].append(e)
    days = sorted(by_day.keys())

    prev_result = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}
    already_ok_days = {m["day"] for m in (prev_result.get("days_processed") or []) if m.get("status") in ("ok", "partial_chunks_failed")}
    prices_by_event: dict[int, dict] = {}
    prior_cost_total = 0.0
    for day in already_ok_days:
        raw_path = RAW_DIR / f"{day}.json.gz"
        if not raw_path.exists():
            continue
        with gzip.open(raw_path, "rt", encoding="utf-8") as fh:
            cached = json.load(fh)
        for row in cached["rows"]:
            prices_by_event[row["event_id"]] = row
    for m in (prev_result.get("days_processed") or []):
        if m.get("status") in ("ok", "partial_chunks_failed"):
            prior_cost_total += m.get("cost_credits", 0) or 0
    remaining_days = [day for day in days if day not in already_ok_days]
    # Владелец: темп трат на новом (исправленном) джойне заметно выше
    # старого (дни 1-3: 100.25/239.82/191.08 против 103.57/152.31/107.45
    # на WSOL-only) -- 900 может не хватить на все 7 дней хронологически.
    # Переключаемся на порядок "от дешёвых к дорогим" ПО СТАРОМУ джойну
    # как единственному доступному проксирующему сигналу (v3:
    # 09-17=106.34 самый дешёвый, 09-16=124.62, 09-15=217.66 дороже
    # всех измеренных, 09-18 не измерен вообще -- ставим последним,
    # осторожно) -- чтобы получить максимум покрытых дней в рамках
    # бюджета, а не упереться в потолок посередине хронологии.
    # Владелец, после 4 дней: 09-18 первым (32 сигнала лидера + все 8
    # сделок пилота для сверки модель/реальность), затем 09-15, 09-16.
    # Бюджетные решения об остановке -- ТОЛЬКО по кабинету владельца
    # (собственный счётчик execution_cost_credits завышал ~3.3x в
    # прошлых прогонах, не сверен -- не останавливаемся по нему).
    day_priority = {"2026-09-18": 0, "2026-09-15": 1, "2026-09-16": 2}
    remaining_days.sort(key=lambda dday: day_priority.get(dday, 99))
    days_todo = remaining_days[:MAX_DAYS_PER_RUN]
    print(f"[phase3v4] дней всего={len(days)}, уже готово={len(already_ok_days)}, "
          f"порядок оставшихся: {remaining_days}, в этом прогоне обработаю: {days_todo}", flush=True)

    if GLOBAL_PRIOR_SPEND_OTHER_STEPS + prior_cost_total >= GLOBAL_BUDGET_CAP:
        result_stop = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "FINAL_ANSWER": (f"ОСТАНОВЛЕНО -- новый бюджет v4 исчерпан "
                                          f"({GLOBAL_PRIOR_SPEND_OTHER_STEPS + prior_cost_total:.2f}/{GLOBAL_BUDGET_CAP})."),
                        "days_already_done": sorted(already_ok_days)}
        OUT_PATH.write_text(json.dumps(result_stop, ensure_ascii=False, indent=2, default=str))
        print("[phase3v4] " + result_stop["FINAL_ANSWER"], flush=True)
        return

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery, "n_events_sampled": len(sample),
                     "n_events_per_day": {k: len(v) for k, v in by_day.items()},
                     "days_all": days, "days_already_done": sorted(already_ok_days),
                     "days_this_run": days_todo, "excluded_days": sorted(EXCLUDED_DAYS)}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_JANA_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    day_meta = [m for m in (prev_result.get("days_processed") or []) if m["day"] in already_ok_days]
    running_cost = prior_cost_total
    for day in days_todo:
        day_events = by_day[day]
        chunks = [day_events[j:j + CHUNK_SIZE] for j in range(0, len(day_events), CHUNK_SIZE)]
        day_cost, day_rows_all, failed_chunks, hit_fatal_block = 0.0, [], [], False
        for ci, chunk in enumerate(chunks):
            sql = build_price_sql(chunk)
            r = probe.run_sql_sync(f"phase3v4_{day}_c{ci}", sql, timeout_s=600)
            meta = {k: v for k, v in r.items() if k != "rows"}
            cost = (meta.get("status_meta") or {}).get("execution_cost_credits", 0) or 0
            day_cost += cost
            running_cost += cost
            if r.get("status") != "ok":
                # Владелец, найдено на дне 09-15: одна чанка может упасть
                # (напр. FAILED_TYPE_RESOURCES_CAP_REACHED из-за одного
                # тяжёлого минта в этой чанке), не бросаем весь день --
                # остальные чанки могут пройти нормально, их данные не
                # выбрасываем. Текст ошибки может быть в status_meta.error
                # (упало исполнение) ИЛИ в execute_body/create_body (упало
                # ДО исполнения, напр. execute_failed по datapoint limit --
                # именно так провалился повтор 09-15) -- проверяем все три.
                err_text = json.dumps({k: meta.get(k) for k in ("status_meta", "execute_body", "create_body")}, default=str)
                is_fatal = any(m in err_text for m in FATAL_ERROR_MARKERS)
                failed_chunks.append({"chunk_index": ci, "n_events": len(chunk), "status": r.get("status"),
                                       "cost_credits": cost, "is_fatal_external_block": is_fatal,
                                       "meta": meta})
                print(f"[phase3v4] день {day} чанк {ci+1}/{len(chunks)}: status={r.get('status')} "
                      f"is_fatal={is_fatal} -- тело: {json.dumps(meta, default=str)[:800]}", flush=True)
                if is_fatal:
                    # Внешний жёсткий блок аккаунта Dune (напр. datapoint
                    # limit per billing cycle) -- следующие чанки/дни
                    # упрутся в то же самое, не тратим время/вызовы впустую.
                    hit_fatal_block = True
                    break
                continue  # нефатальная ошибка конкретной чанки -- пробуем остальные чанки этого дня
            day_rows_all.extend(r["rows"])
            print(f"[phase3v4] день {day} чанк {ci+1}/{len(chunks)}: status=ok n_rows={r.get('n_rows')} "
                  f"cost={cost} running_total={running_cost:.2f}", flush=True)

        day_status = "ok" if not failed_chunks else "partial_chunks_failed"
        day_meta.append({"day": day, "n_events": len(day_events), "n_chunks": len(chunks),
                          "n_chunks_failed": len(failed_chunks), "failed_chunks": failed_chunks,
                          "status": day_status, "n_rows": len(day_rows_all), "cost_credits": day_cost})
        result["days_processed"] = day_meta
        result["total_cost_credits_so_far"] = running_cost
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(f"[phase3v4] день {day} итог: {day_cost:.2f} кредита, "
              f"чанков упало={len(failed_chunks)}/{len(chunks)}", flush=True)

        if hit_fatal_block:
            result["FINAL_ANSWER"] = (f"ОСТАНОВЛЕНО на дне {day} -- внешний жёсткий блок аккаунта Dune "
                                       f"(см. failed_chunks[-1] для точного сообщения). Дальше запускать бессмысленно "
                                       f"до решения владельца (изменить лимит на dune.com или дождаться нового billing cycle).")
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print("[phase3v4] " + result["FINAL_ANSWER"], flush=True)
            return

        raw_path = RAW_DIR / f"{day}.json.gz"
        with gzip.open(raw_path, "wt", encoding="utf-8") as fh:
            json.dump({"day": day, "n_chunks": len(chunks), "rows": day_rows_all}, fh, default=str)
        for row in day_rows_all:
            prices_by_event[row["event_id"]] = row
        result["n_events_priced_so_far"] = len(prices_by_event)

        result["control_check"] = control_check(sample, prices_by_event, leader)
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

        if running_cost - prior_cost_total > MAX_TOTAL_COST_CREDITS:
            result["FINAL_ANSWER"] = (f"ОСТАНОВЛЕНО в этом прогоне -- потолок {MAX_TOTAL_COST_CREDITS} кредитов "
                                       f"на прогон превышен (потрачено в прогоне {running_cost - prior_cost_total:.2f}).")
            break
        if GLOBAL_PRIOR_SPEND_OTHER_STEPS + running_cost >= GLOBAL_BUDGET_CAP:
            result["FINAL_ANSWER"] = (f"ОСТАНОВЛЕНО -- новый бюджет v4 исчерпан "
                                       f"({GLOBAL_PRIOR_SPEND_OTHER_STEPS + running_cost:.2f}/{GLOBAL_BUDGET_CAP}).")
            break

    result["total_cost_credits"] = running_cost
    done_days = {m["day"] for m in day_meta if m["status"] in ("ok", "partial_chunks_failed")}
    remaining = [day for day in days if day not in done_days]
    result["ALL_DAYS_DONE"] = not remaining
    result["days_covered"] = sorted(done_days)
    result["days_not_covered"] = remaining
    result["answers"] = build_answer_tables(sample, prices_by_event, d["wallet_roles"])
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not remaining:
        print(f"[phase3v4] ВСЕ ДНИ ЗАВЕРШЕНЫ: {len(prices_by_event)}/{len(sample)} событий получили цены, "
              f"стоимость всего={running_cost:.2f} кредитов", flush=True)
    else:
        print(f"[phase3v4] прогон завершён, дни в таблицах: {sorted(done_days)}, "
              f"непокрытые: {remaining}, потрачено всего={running_cost:.2f}", flush=True)


if __name__ == "__main__":
    main()
