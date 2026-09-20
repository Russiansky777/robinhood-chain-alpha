#!/usr/bin/env python3
"""Владелец: DBot не отбрасывать. Пересчёт размера покупки на уже
собранных (в прошлом прогоне) сырых данных DBot -- MAX(usdAmount) по
txHash (не сумма по легам маршрута, как раньше -- это и было причиной
провала первой попытки: DBot логирует каждый лег мульти-хопа отдельной
строкой и solAmount для USDC-котируемых лег на самом деле равен
usdAmount, т.е. это USD, а не SOL), делённое на курс SOL (usdRate из
строки этого же txHash с baseMint=WSOL; если такой строки нет в самом
txHash -- ближайший по времени usdRate из ДРУГИХ строк того же
уже полученного набора).

ВАЖНО: прошлый прогон (data/solana_dbot_full_scan.json, шаг step1_2) не
сохранил сырые строки DBot (только сумму solAmount по txHash) -- этого
пересчёта из уже сохранённых данных буквально не существует. Честно
дозапрашиваем ТОЛЬКО тот же самый уже покрытый скан лидера (те же ~5
курсорных страниц, то же окно) -- НЕ 217 кандидатов, НЕ новый более
широкий скан -- ровно чтобы сохранить сырые строки (txHash, mint,
baseMint, blockTime, solAmount, usdAmount, usdRate) и dex_program_ids
для сверки, раз их не было в кэше. Дальше пересчёт V2 делается только
на этих (и старых RPC-side, тоже пересохранённых) данных.

Критерий (владелец): сравнить пересчитанный V2 размер с RPC на общих
txHash, порог >=80% в пределах ±5%. Плюс отдельно полнота: сколько из
RPC-покупок лидера DBot вообще не нашёл (по txHash), 3 примера с типом
пула/маршрута (dex_program_ids -> имя из dex_labels.json).

Решение автоматически, без промпта:
  A) сошлось -> скан 217 (сначала 10 BATCH-5) через DBot с пересчётом V2:
     type=buy, cursor=_id (см. discover_pagination -- уже подтверждено,
     что это рабочая пагинация), до 5 страниц на кошелёк, ранняя
     остановка при 3 первых покупках (первая по минту в окне) >=2 SOL.
     В итог -- пометка о заниженной полноте DBot (см. completeness_pct
     на лидере).
  B) не сошлось -> сразу скан 217 через RPC (тот же метод, что уже
     провалидирован на LEADER_WALLET и Brez -- solana_batch5_rpc_check.
     scan_wallet), лимит 300 tx/кошелёк, честный coverage_status.

В обоих случаях -- батчи по 10 в порядке лучшего ранга Fomo (windows_present),
фильтр >=3 покупки >=2 SOL за 72ч (как в предыдущей явной спецификации
владельца, ничем не отменена), address/name/n_ge_2sol/медиана."""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path
from statistics import median

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_entry_log import tx_signers  # noqa: E402
from solana_batch5_rpc_check import (  # noqa: E402
    tx_program_ids, mint_balance_map, DEX_PROGRAMS, STABLE_MINTS, WSOL, USDC, USDT,
    PRIORITY_10, fetch_signatures_last_n_hours, scan_wallet as rpc_scan_wallet,
)
from solana_dbot_pilot_report import _ACTIVE_SECRETS, _scrub_all  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dbot_full_scan.json"
CANDIDATES_PATH = REPO_ROOT / "data" / "fomo_leaderboard_candidates.json"
DEX_LABELS_PATH = REPO_ROOT / "data" / "solana_buyer_200" / "prior" / "current" / "buyer_100" / "dex_labels.json"
DEX_LABELS = json.loads(DEX_LABELS_PATH.read_text()) if DEX_LABELS_PATH.exists() else {}

HOST = "https://api-data-v1.dbotx.com"
PATH_TRADES = "/kline/wallet/trades"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"

LOOKBACK_HOURS = 72
LOOKBACK_S = LOOKBACK_HOURS * 3600
MAX_PAGES_LEADER_PROBE = 5
MAX_PAGES_PER_WALLET = 5
TARGET_GE2SOL_PER_WALLET = 3
MATCH_TOLERANCE_V2 = 0.05
TIME_BUDGET_S = 35 * 60
COMMIT_INTERVAL_S = 60


# ---------- DBot REST ----------

def dbot_get(params: dict, api_key: str) -> dict:
    if any(c in api_key for c in ("\n", "\r")):
        raise RuntimeError("DBOT_API_KEY содержит перевод строки -- не отправляю как есть.")
    try:
        resp = requests.get(f"{HOST}{PATH_TRADES}", params=params, headers={"X-API-KEY": api_key}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"exception": _scrub_all(f"{type(exc).__name__}: {exc}")}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"non_json_body": _scrub_all(resp.text[:1500])}
    return {"http_status": resp.status_code, "body": body}


def extract_items(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("res", "data", "results", "trades", "items", "list"):
            v = body.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for kk in ("res", "data", "results", "trades", "items", "list"):
                    if isinstance(v.get(kk), list):
                        return v[kk]
    return []


def item_hash(item: dict) -> str | None:
    return item.get("txHash") or item.get("tx_hash") or item.get("signature")


# ---------- пагинация: cursor=_id последней записи (подтверждено рабочим прошлым прогоном) ----------

PAGINATION_CANDIDATES = [
    ("cursor_id", "cursor", "_id"),
    ("before_id", "before", "_id"),
    ("beforeId_id", "beforeId", "_id"),
    ("endTime_bt", "endTime", "blockTime"),
    ("before_bt", "before", "blockTime"),
]


def discover_pagination(account: str, api_key: str, page1_items: list) -> tuple[str | None, dict]:
    if not page1_items:
        return None, {"reason": "page1 пуста"}
    oldest = min(page1_items, key=lambda i: i.get("blockTime") or 0)
    oldest_bt = oldest.get("blockTime")
    seen_hashes = {item_hash(i) for i in page1_items}
    trials = []
    for name, param_name, ref_field in PAGINATION_CANDIDATES:
        ref_val = oldest.get(ref_field)
        if ref_val is None:
            trials.append({"strategy": name, "skipped": "нет поля-ссылки в page1"})
            continue
        r = dbot_get({"account": account, "chain": "solana", "type": "buy", param_name: ref_val}, api_key)
        items = extract_items(r.get("body"))
        new_hashes = {item_hash(i) for i in items} - seen_hashes
        works = bool(items) and bool(new_hashes) and (
            max((i.get("blockTime") or 0) for i in items) <= (oldest_bt or 0)
        )
        trials.append({"strategy": name, "param": param_name, "http_status": r.get("http_status"),
                        "n_items": len(items), "n_new_hashes": len(new_hashes), "works": works})
        if works:
            return name, {"trials": trials}
    return None, {"trials": trials}


def fetch_dbot_buys(account: str, api_key: str, strategy: str | None, max_pages: int,
                     stop_when=None) -> tuple[list, dict]:
    params_base = {"account": account, "chain": "solana", "type": "buy"}
    r1 = dbot_get(params_base, api_key)
    items1 = extract_items(r1.get("body"))
    pages_log = [{"page": 1, "http_status": r1.get("http_status"), "n_items": len(items1)}]
    all_items = list(items1)
    seen = {item_hash(i) for i in all_items}
    if not items1 or strategy is None:
        return all_items, {"pages": pages_log}
    strategy_map = {n: (p, f) for n, p, f in PAGINATION_CANDIDATES}
    param_name, ref_field = strategy_map[strategy]
    cur_batch = items1
    for page_n in range(2, max_pages + 1):
        if stop_when and stop_when(all_items):
            break
        oldest = min(cur_batch, key=lambda i: i.get("blockTime") or 0)
        ref_val = oldest.get(ref_field)
        if ref_val is None:
            break
        r = dbot_get({**params_base, param_name: ref_val}, api_key)
        items = extract_items(r.get("body"))
        new_items = [i for i in items if item_hash(i) not in seen]
        pages_log.append({"page": page_n, "http_status": r.get("http_status"),
                           "n_items": len(items), "n_new": len(new_items)})
        if not new_items:
            break
        all_items.extend(new_items)
        seen.update(item_hash(i) for i in new_items)
        cur_batch = new_items
    return all_items, {"pages": pages_log}


# ---------- пересчёт V2: MAX(usdAmount) по txHash / курс SOL ----------

def sol_rate_series(items: list) -> list[tuple[int, float]]:
    pts = [(i.get("blockTime"), i.get("usdRate")) for i in items
           if i.get("baseMint") == WSOL and i.get("blockTime") is not None and i.get("usdRate")]
    pts.sort(key=lambda p: p[0])
    return pts


def nearest_rate(pts: list[tuple[int, float]], bt) -> float | None:
    if not pts or bt is None:
        return None
    return min(pts, key=lambda p: abs(p[0] - bt))[1]


def dbot_buy_size_sol_v2(rows_for_hash: list[dict], rate_pts: list[tuple[int, float]]) -> float | None:
    usd_amounts = [r.get("usdAmount") for r in rows_for_hash if r.get("usdAmount") is not None]
    if not usd_amounts:
        return None
    amt_usd = max(usd_amounts)
    own_wsol_rows = [r for r in rows_for_hash if r.get("baseMint") == WSOL and r.get("usdRate")]
    if own_wsol_rows:
        rate = own_wsol_rows[0]["usdRate"]
    else:
        bt = next((r.get("blockTime") for r in rows_for_hash if r.get("blockTime") is not None), None)
        rate = nearest_rate(rate_pts, bt)
    if not rate:
        return None
    return amt_usd / rate


def dex_names(program_ids: list[str] | None) -> list[str]:
    if not program_ids:
        return []
    return [DEX_LABELS.get(p, p) for p in program_ids]


def recompute_v2(all_items: list, rpc_events: list) -> dict:
    by_hash: dict[str, list] = defaultdict(list)
    for i in all_items:
        h = item_hash(i)
        if h:
            by_hash[h].append(i)
    rate_pts = sol_rate_series(all_items)
    size_by_hash = {h: dbot_buy_size_sol_v2(rows, rate_pts) for h, rows in by_hash.items()}

    comparison = []
    for ev in rpc_events:
        sig = ev["signature"]
        dbot_sol = size_by_hash.get(sig)
        row = {"signature": sig, "rpc_spend_sol_equiv": ev["spend_sol_equiv"],
               "found_in_dbot": sig in by_hash, "dbot_size_sol_v2": dbot_sol}
        if dbot_sol is not None and ev["spend_sol_equiv"] > 0:
            row["match_within_5pct"] = abs(dbot_sol - ev["spend_sol_equiv"]) / ev["spend_sol_equiv"] < MATCH_TOLERANCE_V2
        else:
            row["match_within_5pct"] = None
        comparison.append(row)

    n_common = sum(1 for r in comparison if r["found_in_dbot"])
    n_matched = sum(1 for r in comparison if r.get("match_within_5pct"))
    frac_matched = (n_matched / n_common) if n_common else 0.0
    decision = "сошлось" if (n_common >= 3 and frac_matched >= 0.8) else "не сошлось"

    n_total = len(rpc_events)
    completeness_pct = round(100 * n_common / n_total, 1) if n_total else None
    not_found = [ev for ev in rpc_events if ev["signature"] not in by_hash]
    not_found_examples = [
        {"signature": ev["signature"], "blockTime": ev["blockTime"],
         "spend_sol_equiv": ev["spend_sol_equiv"], "bought_mints": ev.get("bought_mints"),
         "dex_program_ids": ev.get("dex_program_ids"), "dex_names": dex_names(ev.get("dex_program_ids"))}
        for ev in not_found[:3]
    ]

    return {
        "comparison_table_v2": comparison, "n_common_txhash": n_common,
        "n_matched_within_5pct": n_matched, "fraction_matched_5pct": round(frac_matched, 3),
        "n_rpc_events_total": n_total, "completeness_pct": completeness_pct,
        "n_not_found_in_dbot": len(not_found), "not_found_examples": not_found_examples,
        "MATCH_DECISION_V2": decision,
    }


# ---------- RPC-сторона сверки (без старых эталонов -- любые покупки лидера в окне) ----------

def classify_tx_total_spend(tx: dict, wallet: str) -> dict | None:
    """Как classify_tx в solana_batch5_rpc_check, но БЕЗ требования
    "первый вход по минту" -- просто общий SOL-эквивалент потраченного в
    этой транзакции на DEX/AMM, для сверки с DBot type=buy."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    if wallet not in tx_signers(tx):
        return None
    dex_ids = tx_program_ids(tx) & DEX_PROGRAMS
    if not dex_ids:
        return None
    loaded = meta.get("loadedAddresses") or {}
    keys = ([k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
            + list(loaded.get("writable") or []) + list(loaded.get("readonly") or []))
    if wallet not in keys:
        return None
    idx = keys.index(wallet)
    pre_balances, post_balances = meta.get("preBalances") or [], meta.get("postBalances") or []
    pre_sol = pre_balances[idx] if idx < len(pre_balances) else None
    post_sol = post_balances[idx] if idx < len(post_balances) else None
    pre_tb = mint_balance_map(meta.get("preTokenBalances"), wallet)
    post_tb = mint_balance_map(meta.get("postTokenBalances"), wallet)

    sol_decrease = max(D(0), (D(pre_sol) - D(post_sol)) / D(10) ** 9) if pre_sol is not None and post_sol is not None else D(0)
    wsol_decrease = max(D(0), pre_tb.get(WSOL, D(0)) - post_tb.get(WSOL, D(0)))
    usdc_decrease = max(D(0), pre_tb.get(USDC, D(0)) - post_tb.get(USDC, D(0)))
    usdt_decrease = max(D(0), pre_tb.get(USDT, D(0)) - post_tb.get(USDT, D(0)))
    stable_usd = usdc_decrease + usdt_decrease

    stable_sol_equiv = D(0)
    if stable_usd > 0:
        block_time = tx.get("blockTime")
        if block_time is not None:
            try:
                price_row = fp.find_price_at_gecko(block_time, block_time - 3600, block_time + 3600)
                sol_usd = D(price_row["event"]["p1_per_0"])
                if sol_usd > 0:
                    stable_sol_equiv = stable_usd / sol_usd
            except Exception:  # noqa: BLE001 -- честно: курс не найден, не выдумываем
                pass

    spend_sol_equiv = sol_decrease + wsol_decrease + stable_sol_equiv
    if spend_sol_equiv <= 0:
        return None
    bought_mints = [m for m, post_amt in post_tb.items()
                    if m not in STABLE_MINTS and m != WSOL and post_amt > pre_tb.get(m, D(0))]
    return {"signature": tx["transaction"]["signatures"][0], "blockTime": tx.get("blockTime"),
            "spend_sol_equiv": float(spend_sol_equiv), "bought_mints": bought_mints,
            "dex_program_ids": sorted(dex_ids)}


def rpc_ground_truth_window(wallet: str, window_start: int, window_end: int) -> list[dict]:
    now = int(time.time())
    lookback_s = max(0, now - window_start) + 3600
    sigs = fetch_signatures_last_n_hours(wallet, lookback_s)
    in_window = [s for s in sigs if s.get("err") is None and window_start - 60 <= (s.get("blockTime") or 0) <= window_end + 60]
    out = []
    for s in in_window:
        tx = fp.get_transaction(s["signature"])
        if tx is None:
            continue
        ev = classify_tx_total_spend(tx, wallet)
        if ev is not None:
            out.append(ev)
    return out


# ---------- шаг 1+2: пагинация лидера + сверка (с сохранением сырых данных для V2) ----------

def step1_2(api_key: str) -> dict:
    out: dict = {}
    r1 = dbot_get({"account": LEADER_WALLET, "chain": "solana", "type": "buy"}, api_key)
    items1 = extract_items(r1.get("body"))
    out["page1_http_status"] = r1.get("http_status")
    out["page1_n_items"] = len(items1)
    if not items1:
        out["HONEST_ANSWER"] = "page1 пуста или ошибка -- дальше не идём"
        return out

    strategy, disc_log = discover_pagination(LEADER_WALLET, api_key, items1)
    out["pagination_strategy_found"] = strategy
    out["pagination_discovery_log"] = disc_log

    all_items, fetch_log = fetch_dbot_buys(LEADER_WALLET, api_key, strategy, MAX_PAGES_LEADER_PROBE)
    out["fetch_log"] = fetch_log
    out["n_items_total"] = len(all_items)
    out["all_dbot_items"] = all_items  # сырые строки -- нужны для пересчёта V2 без новых запросов в будущем

    now = int(time.time())
    blocktimes = [i.get("blockTime") for i in all_items if i.get("blockTime") is not None]
    window_start, window_end = (min(blocktimes), max(blocktimes)) if blocktimes else (None, None)
    out["window_start_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_start)) if window_start else None
    out["window_end_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_end)) if window_end else None
    out["hours_back_reached"] = round((now - window_start) / 3600, 2) if window_start else None
    print(f"[dbot_full] лидер: {len(all_items)} записей, стратегия={strategy}, "
          f"глубина={out['hours_back_reached']}ч ({out['window_start_utc']}..{out['window_end_utc']})", flush=True)

    if window_start is None:
        out["HONEST_ANSWER"] = "нет ни одной записи с blockTime -- сверку строить не на чем"
        out["rpc_events"] = []
        return out

    print("[dbot_full] тяну RPC ground truth за то же окно...", flush=True)
    rpc_events = rpc_ground_truth_window(LEADER_WALLET, window_start, window_end)
    out["rpc_events"] = rpc_events  # сырые RPC-события -- тоже нужны для пересчёта без новых запросов
    out["n_rpc_ground_truth_events"] = len(rpc_events)
    print(f"[dbot_full] RPC ground truth: {len(rpc_events)} событий", flush=True)
    return out


# ---------- шаг 3: скан кошелька через DBot V2 (после "сошлось") ----------

def first_purchase_sizes_v2(items: list, cutoff: int) -> tuple[list[float], int, dict]:
    by_hash: dict[str, list] = defaultdict(list)
    for i in items:
        h = item_hash(i)
        if h:
            by_hash[h].append(i)
    rate_pts = sol_rate_series(items)
    by_mint_first: dict[str, dict] = {}
    for i in items:
        bt = i.get("blockTime")
        if bt is None or bt < cutoff:
            continue
        m = i.get("mint")
        if m is None:
            continue
        if m not in by_mint_first or bt < by_mint_first[m]["blockTime"]:
            by_mint_first[m] = i
    sizes, n_price_missing = [], 0
    for item in by_mint_first.values():
        h = item_hash(item)
        sz = dbot_buy_size_sol_v2(by_hash.get(h, [item]), rate_pts)
        if sz is None:
            n_price_missing += 1
        else:
            sizes.append(sz)
    return sizes, n_price_missing, by_mint_first


def dbot_scan_wallet_v2(address: str, api_key: str, strategy: str | None) -> dict:
    now = int(time.time())
    cutoff = now - LOOKBACK_S

    def enough(items):
        sizes, _, _ = first_purchase_sizes_v2(items, cutoff)
        n_ge2 = sum(1 for s in sizes if s >= 2)
        oldest = min((i.get("blockTime") or now for i in items), default=now)
        return n_ge2 >= TARGET_GE2SOL_PER_WALLET or oldest <= cutoff

    all_items, fetch_log = fetch_dbot_buys(address, api_key, strategy, MAX_PAGES_PER_WALLET, stop_when=enough)
    sizes, n_price_missing, by_mint_first = first_purchase_sizes_v2(all_items, cutoff)

    blocktimes_in_window = [i.get("blockTime") for i in all_items if i.get("blockTime") and i["blockTime"] >= cutoff]
    oldest_bt = min(blocktimes_in_window) if blocktimes_in_window else None
    coverage_hours_actual = round((now - oldest_bt) / 3600, 2) if oldest_bt else 0.0
    n_pages = len(fetch_log["pages"])
    if oldest_bt is not None and oldest_bt <= cutoff + 300:
        coverage_status = "full_72h"
    elif n_pages >= MAX_PAGES_PER_WALLET:
        coverage_status = f"partial_capped_at_{MAX_PAGES_PER_WALLET}_pages"
    else:
        coverage_status = "wallet_history_shorter_than_72h"

    return {
        "n_pages_fetched": n_pages, "n_items_total": len(all_items),
        "coverage_status": coverage_status, "coverage_hours_actual": coverage_hours_actual,
        "n_first_purchases_in_window": len(by_mint_first), "n_price_missing": n_price_missing,
        "n_ge_2sol": sum(1 for s in sizes if s >= 2),
        "n_ge_4_3sol": sum(1 for s in sizes if s >= 4.3),
        "median_spend_sol": round(median(sizes), 4) if sizes else None,
    }


def scan_wallet_adapter(method: str, address: str, api_key: str, strategy: str | None) -> dict:
    if method == "dbot_v2":
        return dbot_scan_wallet_v2(address, api_key, strategy)
    r = rpc_scan_wallet(address)
    return {
        "n_tx_scanned": r["n_tx_scanned"], "coverage_status": r["coverage_status"],
        "coverage_hours_actual": r["coverage_hours_actual"],
        "n_ge_2sol": r["n_first_entries_ge_2sol"], "n_ge_4_3sol": r["n_first_entries_ge_4_3sol"],
        "median_spend_sol": r["median_spend_sol_equiv"],
    }


def main() -> None:
    api_key = os.environ.get("DBOT_API_KEY", "")
    result: dict = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}

    need_raw = ("step1_2" not in result or "all_dbot_items" not in result["step1_2"]
                or "rpc_events" not in result["step1_2"])
    if need_raw:
        if not api_key:
            result["HONEST_ANSWER"] = "DBOT_API_KEY пуст в окружении."
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print("[dbot_full] " + result["HONEST_ANSWER"], flush=True)
            return
        if not any(c in api_key for c in ("\n", "\r")):
            _ACTIVE_SECRETS.append(api_key)
        print("[dbot_full] в кэше нет сырых строк DBot/RPC для V2-пересчёта -- "
              "дозапрашиваю ТОЛЬКО уже покрытое окно лидера (не 217, не шире)", flush=True)
        result.pop("STOP", None)
        result.pop("reason", None)
        result["step1_2"] = step1_2(api_key)
        OUT_PATH.write_text(_scrub_all(json.dumps(result, ensure_ascii=False, indent=2, default=str)))
        fp._git_commit_progress("dbot_full_scan_raw_refetch", [OUT_PATH])
    elif not any(c in api_key for c in ("\n", "\r")) and api_key:
        _ACTIVE_SECRETS.append(api_key)

    s = result["step1_2"]
    if "MATCH_DECISION_V2" not in s:
        v2 = recompute_v2(s["all_dbot_items"], s["rpc_events"])
        s.update(v2)
        result["step1_2"] = s
        OUT_PATH.write_text(_scrub_all(json.dumps(result, ensure_ascii=False, indent=2, default=str)))
        fp._git_commit_progress("dbot_full_scan_recompute_v2", [OUT_PATH])
        print(f"[dbot_full] V2: common_txhash={v2['n_common_txhash']} matched_5pct={v2['n_matched_within_5pct']} "
              f"frac={v2['fraction_matched_5pct']} completeness={v2['completeness_pct']}% "
              f"decision={v2['MATCH_DECISION_V2']}", flush=True)

    decision = s["MATCH_DECISION_V2"]
    method = "dbot_v2" if decision == "сошлось" else "rpc"
    result["scan_method"] = method
    strategy = s.get("pagination_strategy_found")
    if method == "dbot_v2":
        result["dbot_completeness_note"] = (
            f"частота по DBot занижена (полнота ~{s.get('completeness_pct')}% на лидере)")
    print(f"[dbot_full] решение V2={decision} -> метод скана 217 = {method}", flush=True)

    result.setdefault("priority_10", {})
    result.setdefault("remaining_scan", {})

    print(f"[dbot_full] считаю приоритетные 10 (метод={method})", flush=True)
    for addr, name in PRIORITY_10:
        if addr in result["priority_10"] and result["priority_10"][addr].get("_method") == method:
            continue
        row = scan_wallet_adapter(method, addr, api_key, strategy)
        row["name"] = name
        row["_method"] = method
        result["priority_10"][addr] = row
        print(f"[dbot_full] {name} ({addr[:10]}..): >=2SOL={row['n_ge_2sol']} >=4.3SOL={row['n_ge_4_3sol']} "
              f"медиана={row['median_spend_sol']} coverage={row['coverage_status']} ({row['coverage_hours_actual']}ч)", flush=True)
        OUT_PATH.write_text(_scrub_all(json.dumps(result, ensure_ascii=False, indent=2, default=str)))
        fp._git_commit_progress("dbot_full_scan_priority10", [OUT_PATH])

    result["priority_10_table"] = [
        {"address": a, "name": r.get("name"), "n_ge_2sol": r.get("n_ge_2sol"),
         "n_ge_4_3sol": r.get("n_ge_4_3sol"), "median_spend_sol": r.get("median_spend_sol"),
         "coverage_status": r.get("coverage_status"), "coverage_hours_actual": r.get("coverage_hours_actual")}
        for a, r in result["priority_10"].items()
    ]
    OUT_PATH.write_text(_scrub_all(json.dumps(result, ensure_ascii=False, indent=2, default=str)))
    print("[dbot_full] приоритетные 10 готовы -- перехожу к остальным кандидатам", flush=True)

    if not CANDIDATES_PATH.exists():
        print("[dbot_full] нет fomo_leaderboard_candidates.json -- остальное не сканирую.", flush=True)
        return
    candidates = json.loads(CANDIDATES_PATH.read_text())
    rows = candidates.get("final_table_sorted_by_followers") or []
    priority_addrs = {a for a, _ in PRIORITY_10}
    remaining = [r for r in rows if r["solana_address"] not in priority_addrs]
    print(f"[dbot_full] остальных кандидатов: {len(remaining)}", flush=True)

    scanned = result["remaining_scan"]
    started_at = time.monotonic()
    last_commit_at = started_at
    n_done = 0
    for row in remaining:
        addr = row["solana_address"]
        if addr in scanned and scanned[addr].get("_method") == method:
            continue
        if time.monotonic() - started_at > TIME_BUDGET_S:
            print("[dbot_full] бюджет времени исчерпан -- остальное на следующий прогон", flush=True)
            break
        r = scan_wallet_adapter(method, addr, api_key, strategy)
        r["name"] = row.get("nickname")
        r["_method"] = method
        scanned[addr] = r
        n_done += 1
        print(f"[dbot_full] {row.get('nickname')} ({addr[:10]}..): >=2SOL={r['n_ge_2sol']} "
              f"coverage={r['coverage_status']} ({r['coverage_hours_actual']}ч)", flush=True)
        if time.monotonic() - last_commit_at > COMMIT_INTERVAL_S:
            OUT_PATH.write_text(_scrub_all(json.dumps(result, ensure_ascii=False, indent=2, default=str)))
            fp._git_commit_progress("dbot_full_scan_remaining", [OUT_PATH])
            last_commit_at = time.monotonic()

    n_scanned = len(scanned)
    print(f"[dbot_full] прогон: {n_done} сейчас, всего {n_scanned}/{len(remaining)}", flush=True)

    qualified = [(a, v) for a, v in scanned.items() if v.get("n_ge_2sol", 0) >= 3]
    fomo_rank = {r["solana_address"]: min((w["rank"] for w in r.get("windows_present", [])), default=10**9)
                 for r in rows}
    qualified.sort(key=lambda kv: fomo_rank.get(kv[0], 10**9))
    result["n_remaining_total"] = len(remaining)
    result["n_remaining_scanned"] = n_scanned
    result["remaining_scan_complete"] = n_scanned >= len(remaining)
    result["n_qualified_ge3_purchases_ge2sol"] = len(qualified)
    batches = [qualified[i:i + 10] for i in range(0, len(qualified), 10)]
    result["batches_by_fomo_rank"] = [
        [{"address": a, "name": v.get("name"), "n_ge_2sol": v.get("n_ge_2sol"),
          "median_spend_sol": v.get("median_spend_sol")} for a, v in batch]
        for batch in batches
    ]
    OUT_PATH.write_text(_scrub_all(json.dumps(result, ensure_ascii=False, indent=2, default=str)))
    print(f"[dbot_full] метод={method} квалифицировано (>=3 покупки >=2SOL/72ч): {len(qualified)}, "
          f"батчей по 10: {len(batches)}", flush=True)


if __name__ == "__main__":
    main()
