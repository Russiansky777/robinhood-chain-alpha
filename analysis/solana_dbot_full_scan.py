#!/usr/bin/env python3
"""Владелец: отмена решения "остаёмся на RPC" -- цель быстрый метод через
DBot, скан RPC по 217 не запускать.

Документация docs.dbotx.com заблокирована прокси и из этой песочницы
(подтверждено повторно -- WebFetch/requests на *.dbotx.com возвращают
EGRESS_BLOCKED/404 из прошлых попыток в этой сессии), поэтому курсор
пагинации НЕ берём из документации по памяти (нельзя выдумывать) --
ищем эмпирически: пробуем несколько вероятных имён параметра
(cursor/before/beforeId/endTime -- по blockTime и по _id последнего
элемента страницы 1) и оставляем только тот, что реально даёт более
старые новые записи.

Порядок (ровно как просил владелец):
  1. Пагинация: 2-3 доп. страницы лидера (type=buy) эмпирически найденным
     способом. Отчёт -- сколько часов назад реально дошли.
  2. Сверка БЕЗ старых эталонов: за окно, которое реально перекрыто
     ответом DBot, берём покупки лидера через уже провалидированный RPC-
     конвейер (classify_tx_total_spend ниже -- тот же баланс-метод, что
     в solana_batch5_rpc_check.classify_tx, но БЕЗ ограничения
     "первый вход по минту", т.к. DBot type=buy отдаёт ВСЕ покупки, не
     только первые). Сравнение по txHash, solAmount (у DBot уже в SOL)
     против spend_sol_equiv, допуск ±2%.
  3. Сошлось -> сразу считаем все 217 (сначала 10 BATCH-5) через DBot:
     на кошелёк -- страницы type=buy, пока не наберём 3 первых покупки
     (первая по минту в пределах уже полученного окна) >=2 SOL, либо не
     кончится окно 72ч, либо не кончится лимит в 5 страниц. Покрытие
     часов пишем всегда, даже когда фильтр не набрался.
  4. Не сошлось -> стоп, 3 примера расхождений, RPC-скан 217 НЕ
     запускаем без прямого разрешения владельца."""
from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal as D
from pathlib import Path
from statistics import median

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_entry_log import tx_signers  # noqa: E402
from solana_batch5_rpc_check import (  # noqa: E402
    tx_program_ids, mint_balance_map, DEX_PROGRAMS, STABLE_MINTS, WSOL, USDC, USDT,
    PRIORITY_10, fetch_signatures_last_n_hours,
)
from solana_dbot_pilot_report import _ACTIVE_SECRETS, _scrub_all  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dbot_full_scan.json"
CANDIDATES_PATH = REPO_ROOT / "data" / "fomo_leaderboard_candidates.json"

HOST = "https://api-data-v1.dbotx.com"
PATH_TRADES = "/kline/wallet/trades"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"

LOOKBACK_HOURS = 72
LOOKBACK_S = LOOKBACK_HOURS * 3600
MAX_PAGES_LEADER_PROBE = 5
MAX_PAGES_PER_WALLET = 5
TARGET_GE2SOL_PER_WALLET = 3
MATCH_TOLERANCE = 0.02
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


# ---------- эмпирическое обнаружение пагинации ----------

PAGINATION_CANDIDATES = [
    ("cursor_id", "cursor", "_id"),
    ("before_id", "before", "_id"),
    ("beforeId_id", "beforeId", "_id"),
    ("endTime_bt", "endTime", "blockTime"),
    ("before_bt", "before", "blockTime"),
]


def discover_pagination(account: str, api_key: str, page1_items: list) -> tuple[str | None, dict]:
    """Пробует кандидатов на page1's oldest item -- возвращает (имя_стратегии, доп.лог)."""
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
    """Тянет до max_pages страниц type=buy для account, используя strategy
    (если None -- только страница 1). stop_when(all_items)->bool -- ранняя остановка."""
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


# ---------- RPC-сторона сверки (без старых эталонов -- любые покупки лидера в окне) ----------

def classify_tx_total_spend(tx: dict, wallet: str) -> dict | None:
    """Как classify_tx в solana_batch5_rpc_check, но БЕЗ требования
    "первый вход по минту" -- просто общий SOL-эквивалент потраченного в
    этой транзакции на DEX/AMM, для сверки с DBot type=buy (который
    отдаёт ВСЕ покупки, не только первые)."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    if wallet not in tx_signers(tx):
        return None
    if not (tx_program_ids(tx) & DEX_PROGRAMS):
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
            "spend_sol_equiv": float(spend_sol_equiv), "bought_mints": bought_mints}


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


# ---------- шаг 1+2: пагинация лидера + сверка ----------

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
        out["MATCH_DECISION"] = "не сошлось (нет данных)"
        return out

    print("[dbot_full] тяну RPC ground truth за то же окно...", flush=True)
    rpc_events = rpc_ground_truth_window(LEADER_WALLET, window_start, window_end)
    out["n_rpc_ground_truth_events"] = len(rpc_events)

    dbot_by_hash: dict[str, float] = {}
    for i in all_items:
        h = item_hash(i)
        if h is None:
            continue
        sa = i.get("solAmount")
        if sa is None:
            continue
        dbot_by_hash[h] = dbot_by_hash.get(h, 0.0) + float(sa)

    comparison = []
    for ev in rpc_events:
        sig = ev["signature"]
        dbot_sol = dbot_by_hash.get(sig)
        row = {"signature": sig, "rpc_spend_sol_equiv": ev["spend_sol_equiv"],
               "found_in_dbot": dbot_sol is not None, "dbot_sol_amount_sum": dbot_sol}
        if dbot_sol is not None and ev["spend_sol_equiv"] > 0:
            row["match_within_2pct"] = abs(dbot_sol - ev["spend_sol_equiv"]) / ev["spend_sol_equiv"] < MATCH_TOLERANCE
        else:
            row["match_within_2pct"] = None
        comparison.append(row)

    out["comparison_table"] = comparison
    n_comparable = sum(1 for r in comparison if r["found_in_dbot"])
    n_matched = sum(1 for r in comparison if r.get("match_within_2pct"))
    out["n_rpc_events_in_window"] = len(comparison)
    out["n_found_in_dbot"] = n_comparable
    out["n_matched_within_2pct"] = n_matched
    frac_matched = (n_matched / n_comparable) if n_comparable else 0.0
    out["fraction_matched"] = round(frac_matched, 3)

    decision = "не сошлось (недостаточно пересечения)"
    if n_comparable >= 3 and frac_matched >= 0.8:
        decision = "сошлось"
    elif n_comparable >= 3:
        decision = "не сошлось (расхождение сумм)"
    out["MATCH_DECISION"] = decision
    out["discrepancy_examples"] = [r for r in comparison if r.get("match_within_2pct") is False][:3] or \
        [r for r in comparison if not r["found_in_dbot"]][:3]
    print(f"[dbot_full] сверка: rpc_events_in_window={len(comparison)} found_in_dbot={n_comparable} "
          f"matched_2pct={n_matched} decision={decision}", flush=True)
    return out


# ---------- шаг 3: скан кошелька через DBot (после "сошлось") ----------

def dbot_scan_wallet(address: str, api_key: str, strategy: str | None) -> dict:
    now = int(time.time())
    cutoff = now - LOOKBACK_S

    def enough(items):
        by_mint = {}
        for i in items:
            if (i.get("blockTime") or 0) < cutoff:
                continue
            m = i.get("mint")
            if m is None:
                continue
            if m not in by_mint or (i.get("blockTime") or 0) < (by_mint[m].get("blockTime") or 0):
                by_mint[m] = i
        n_ge2 = sum(1 for p in by_mint.values() if (p.get("solAmount") or 0) >= 2)
        oldest = min((i.get("blockTime") or now for i in items), default=now)
        return n_ge2 >= TARGET_GE2SOL_PER_WALLET or oldest <= cutoff

    all_items, fetch_log = fetch_dbot_buys(address, api_key, strategy, MAX_PAGES_PER_WALLET, stop_when=enough)

    by_mint = {}
    for i in all_items:
        if (i.get("blockTime") or 0) < cutoff:
            continue
        m = i.get("mint")
        if m is None:
            continue
        if m not in by_mint or (i.get("blockTime") or 0) < (by_mint[m].get("blockTime") or 0):
            by_mint[m] = i
    first_purchases = list(by_mint.values())
    sizes = [p.get("solAmount") for p in first_purchases if p.get("solAmount") is not None]

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
        "n_first_purchases_in_window": len(first_purchases),
        "n_ge_2sol": sum(1 for s in sizes if s >= 2),
        "n_ge_4_3sol": sum(1 for s in sizes if s >= 4.3),
        "median_spend_sol": round(median(sizes), 4) if sizes else None,
    }


def main() -> None:
    api_key = os.environ.get("DBOT_API_KEY", "")
    result: dict = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}
    if not api_key:
        result["HONEST_ANSWER"] = "DBOT_API_KEY пуст в окружении."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[dbot_full] " + result["HONEST_ANSWER"], flush=True)
        return
    if not any(c in api_key for c in ("\n", "\r")):
        _ACTIVE_SECRETS.append(api_key)

    if "step1_2" not in result:
        result["step1_2"] = step1_2(api_key)
        OUT_PATH.write_text(_scrub_all(json.dumps(result, ensure_ascii=False, indent=2, default=str)))
        fp._git_commit_progress("dbot_full_scan_step1_2", [OUT_PATH])

    decision = result["step1_2"].get("MATCH_DECISION")
    if decision != "сошлось":
        result["STOP"] = True
        result["reason"] = decision
        OUT_PATH.write_text(_scrub_all(json.dumps(result, ensure_ascii=False, indent=2, default=str)))
        fp._git_commit_progress("dbot_full_scan_stop", [OUT_PATH])
        print(f"[dbot_full] СТОП: {decision}. RPC-скан 217 не запускаю без прямого разрешения.", flush=True)
        return

    strategy = result["step1_2"].get("pagination_strategy_found")
    result.setdefault("priority_10", {})
    result.setdefault("remaining_scan", {})

    print(f"[dbot_full] сошлось -- считаю приоритетные 10 через DBot (strategy={strategy})", flush=True)
    for addr, name in PRIORITY_10:
        if addr in result["priority_10"]:
            continue
        row = dbot_scan_wallet(addr, api_key, strategy)
        row["name"] = name
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
        if addr in scanned:
            continue
        if time.monotonic() - started_at > TIME_BUDGET_S:
            print("[dbot_full] бюджет времени исчерпан -- остальное на следующий прогон", flush=True)
            break
        r = dbot_scan_wallet(addr, api_key, strategy)
        r["name"] = row.get("nickname")
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
    print(f"[dbot_full] квалифицировано (>=3 покупки >=2SOL/72ч): {len(qualified)}, "
          f"батчей по 10: {len(batches)}", flush=True)


if __name__ == "__main__":
    main()
