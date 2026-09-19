#!/usr/bin/env python3
"""Владелец, 2026-09-20: Фаза 1, Источник A -- Helius Enhanced Transactions
API (GET /v0/addresses/{address}/transactions), без фильтра по типу --
классифицируем сами из accountData/tokenBalanceChanges. Секрет назван
владельцем HELIUS_API -- шаг 0 честно печатает, какие из кандидатных имён
реально непустые в окружении, прежде чем использовать любое из них.

Покупка: вырос token-баланс кошелька (mint != WSOL) и трата>0. Трата =
убыль SOL (nativeBalanceChange) + убыль WSOL + убыль USDC/USDT в SOL-экв.
Первый вход: до этой tx в ЗАГРУЖЕННОЙ истории (окно + 7 суток лукбэка
назад) не было более ранней покупки этого минта -- НЕ через pre/post
баланс (Helius Enhanced API их не даёт как таковые, только дельты), а
через порядок появления в истории, как и задано."""
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

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_source_a_helius_gate.json"
HELIUS_BASE = "https://api.helius.xyz"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
# Тот же ончейн-эталон, что и в selected_300.json (5.76 суток), + 7 суток лукбэка назад
WINDOW_LO = 1789174317 - 60
WINDOW_HI = 1789671989 + 60
LOOKBACK_S = 7 * 24 * 3600
CANDIDATES_3 = ["498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ",
                "BMgsHTvcasRVtuevHJh8t6Vf5dmcWkDLAx6gSAQ3dsYm",
                "9CNyLECt2j8tnDhqxtjYk5HUhZ2b8Nwnyb7sfYN7vND2"]

_ACTIVE_SECRETS: list[str] = []


def _scrub(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED]")
    return text


def discover_helius_key() -> tuple[str | None, dict]:
    candidates = ["HELIUS_API", "HELIUS_API_KEY", "HELIUS_KEY"]
    found = {k: bool(os.environ.get(k)) for k in candidates}
    print(f"[source_a] шаг0 -- непустые кандидаты имён секрета: {[k for k, v in found.items() if v]}", flush=True)
    for k in candidates:
        v = os.environ.get(k)
        if v and not any(c in v for c in ("\n", "\r")):
            return k, found
    return None, found


_request_timings: list[float] = []
_request_count = 0


def helius_get(path: str, api_key: str, params: dict) -> dict:
    global _request_count
    backoff = 1.0
    last: dict = {"http_status": None}
    for _ in range(8):
        t0 = time.monotonic()
        try:
            resp = requests.get(f"{HELIUS_BASE}{path}", params={**params, "api-key": api_key}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            last = {"http_status": None, "exception": _scrub(str(exc)[:200])}
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        dt = time.monotonic() - t0
        _request_count += 1
        _request_timings.append(dt)
        if resp.status_code == 429:
            last = {"http_status": 429}
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = None
        return {"http_status": resp.status_code, "body": body, "elapsed_s": dt}
    return last


_sol_cache: dict[int, float | None] = {}


def gecko_get_retry(path: str, params: dict) -> dict:
    backoff = 1.0
    last: dict = {"http_status": None}
    for _ in range(10):
        try:
            resp = requests.get(f"{GECKO_BASE}{path}", params=params, timeout=30, headers={"Accept": "application/json"})
        except Exception as exc:  # noqa: BLE001
            last = {"http_status": None, "exception": str(exc)[:200]}
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        if resp.status_code == 429:
            last = {"http_status": 429}
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        return {"http_status": resp.status_code, "body": resp.json() if resp.ok else None}
    return last


def sol_usd_price_at(t: int) -> float | None:
    if t in _sol_cache:
        return _sol_cache[t]
    r = gecko_get_retry("/networks/solana/pools/3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv/ohlcv/minute",
                        {"aggregate": 1, "before_timestamp": t + 3600, "limit": 200, "currency": "usd"})
    rows = (((r.get("body") or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    if not rows:
        return None  # честно НЕ кэшируем None -- следующий вызов может повториться успешно
    rows = sorted(rows, key=lambda c: c[0])
    import bisect
    times = [c[0] + 60 for c in rows]
    idx = bisect.bisect_right(times, t)
    if idx == 0:
        v = rows[0][4]
    elif idx >= len(rows):
        v = rows[-1][4]
    else:
        t0, c0 = times[idx - 1], D(str(rows[idx - 1][4]))
        t1, c1 = times[idx], D(str(rows[idx][4]))
        v = float(c0) if t1 == t0 else float(c0 + (c1 - c0) * (D(t - t0) / D(t1 - t0)))
    _sol_cache[t] = v
    return v


def fetch_history(wallet: str, api_key: str, lo_time: int, hi_time: int, max_pages: int = 300) -> tuple[list[dict], dict]:
    """Пагинация before= назад по времени до lo_time. Возвращает
    (транзакции по возрастанию времени, метрики пагинации)."""
    all_tx = []
    before = None
    pages = 0
    page_sizes = []
    for _ in range(max_pages):
        r = helius_get(f"/v0/addresses/{wallet}/transactions", api_key,
                        {"before": before} if before else {})
        pages += 1
        if r.get("http_status") != 200 or not isinstance(r.get("body"), list):
            break
        batch = r["body"]
        page_sizes.append(len(batch))
        if not batch:
            break
        all_tx.extend(batch)
        oldest_ts = batch[-1].get("timestamp")
        before = batch[-1].get("signature")
        if oldest_ts is not None and oldest_ts < lo_time:
            break
        if len(batch) < 2:
            break
    all_tx.sort(key=lambda t: t.get("timestamp") or 0)
    metrics = {"n_pages": pages, "page_sizes": page_sizes,
               "avg_page_size": round(sum(page_sizes) / len(page_sizes), 1) if page_sizes else None}
    return [t for t in all_tx if t.get("timestamp") is not None and lo_time <= t["timestamp"] <= hi_time + 1], metrics


def classify_tx(tx: dict, wallet: str, running_holdings: set[str]) -> dict | None:
    """Возвращает событие покупки (или None) -- та же логика, что задана
    владельцем, без pre/post: покупка = рост token-баланса (mint!=WSOL) +
    трата>0 (SOL+WSOL+USDC/USDT в SOL-экв.)."""
    if tx.get("transactionError"):
        return None
    account_data = tx.get("accountData") or []
    my_entry = next((a for a in account_data if a.get("account") == wallet), None)
    native_change = (my_entry or {}).get("nativeBalanceChange", 0) / 1e9

    token_deltas: dict[str, float] = {}
    for a in account_data:
        for tbc in (a.get("tokenBalanceChanges") or []):
            if tbc.get("userAccount") != wallet:
                continue
            mint = tbc.get("mint")
            raw = tbc.get("rawTokenAmount") or {}
            try:
                amt = float(raw.get("tokenAmount", 0)) / (10 ** int(raw.get("decimals", 0)))
            except (TypeError, ValueError):
                continue
            token_deltas[mint] = token_deltas.get(mint, 0.0) + amt

    increased_mints = [m for m, d in token_deltas.items() if d > 0 and m != WSOL_MINT]
    if not increased_mints:
        return None

    wsol_delta = token_deltas.get(WSOL_MINT, 0.0)
    usdc_delta = token_deltas.get(USDC_MINT, 0.0) + token_deltas.get(USDT_MINT, 0.0)

    sol_spend = -native_change if native_change < 0 else 0.0
    wsol_spend = -wsol_delta if wsol_delta < 0 else 0.0
    usdc_spend_raw = -usdc_delta if usdc_delta < 0 else 0.0
    spend_sol_equiv = sol_spend + wsol_spend
    price_missing = False
    if usdc_spend_raw > 0:
        sol_usd = sol_usd_price_at(tx.get("timestamp"))
        if sol_usd:
            spend_sol_equiv += usdc_spend_raw / sol_usd
        else:
            price_missing = True

    if spend_sol_equiv <= 0 and not price_missing:
        return None  # без траты -- аирдроп/перевод, не сигнал

    mint = increased_mints[0]
    is_first_entry = mint not in running_holdings
    return {"signature": tx.get("signature"), "block_time": tx.get("timestamp"), "slot": tx.get("slot"),
            "mint": mint, "tokens_received": token_deltas[mint], "spend_sol_equiv": spend_sol_equiv if not price_missing else None,
            "price_missing": price_missing, "is_first_entry": is_first_entry,
            "effective_price_source": (spend_sol_equiv / token_deltas[mint]) if not price_missing and token_deltas[mint] else None}


def scan_wallet(wallet: str, api_key: str, lo_time: int, hi_time: int) -> dict:
    history, metrics = fetch_history(wallet, api_key, lo_time - LOOKBACK_S, hi_time)
    running_holdings: set[str] = set()
    events = []
    n_price_missing = 0
    for tx in history:
        ev = classify_tx(tx, wallet, running_holdings)
        if ev is None:
            continue
        if ev["mint"] not in running_holdings:
            running_holdings.add(ev["mint"])
        if ev["price_missing"]:
            n_price_missing += 1
        if ev["block_time"] >= lo_time:
            events.append(ev)
    return {"events": events, "pagination_metrics": metrics, "n_history_tx": len(history),
            "n_price_missing": n_price_missing}


def main() -> None:
    api_key, key_discovery = discover_helius_key()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": key_discovery}
    if api_key is None:
        result["HONEST_ANSWER"] = "Ни одно из кандидатных имён (HELIUS_API/HELIUS_API_KEY/HELIUS_KEY) не непусто."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[source_a] " + result["HONEST_ANSWER"], flush=True)
        return
    _ACTIVE_SECRETS.append(api_key)
    result["key_name_used"] = [k for k, v in key_discovery.items() if v][0]

    t_start = time.monotonic()
    leader_scan = scan_wallet(LEADER_WALLET, api_key, WINDOW_LO, WINDOW_HI)
    t_leader = time.monotonic() - t_start
    result["leader_scan_seconds"] = round(t_leader, 1)
    result["leader_pagination_metrics"] = leader_scan["pagination_metrics"]
    result["leader_n_history_tx"] = leader_scan["n_history_tx"]
    result["leader_n_events"] = len(leader_scan["events"])
    result["leader_n_price_missing"] = leader_scan["n_price_missing"]
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[source_a] лидер: {leader_scan['n_history_tx']} tx истории, {len(leader_scan['events'])} событий, "
          f"{t_leader:.1f}с, {result['leader_pagination_metrics']}", flush=True)

    # --- Сверка с нашим RPC-эталоном (300 покупок) ---
    sel_rows = json.loads((REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json").read_text())
    sel_by_sig = {r["signature"]: r for r in sel_rows}
    events_by_sig = {e["signature"]: e for e in leader_scan["events"]}
    n_found = sum(1 for s in sel_by_sig if s in events_by_sig)
    result["coverage_vs_300"] = round(n_found / len(sel_by_sig), 4)

    ratios = []
    first_entry_agree = 0
    first_entry_comparable = 0
    per_tx = []
    for sig, ref in sel_by_sig.items():
        ev = events_by_sig.get(sig)
        if not ev or ev.get("price_missing") or ev.get("effective_price_source") is None:
            continue
        our_price_sol = None
        sol_usd = sol_usd_price_at(ref["time"])
        if sol_usd:
            our_price_sol = float(ref["entry_usdc"]) / sol_usd
        if not our_price_sol:
            continue
        ratio = ev["effective_price_source"] / our_price_sol
        ratios.append(ratio)
        first_entry_comparable += 1
        if bool(ev["is_first_entry"]) == bool(ref.get("zero_balance")):
            first_entry_agree += 1
        per_tx.append({"signature": sig, "ratio": ratio, "helius_first_entry": ev["is_first_entry"],
                        "ref_first_entry": ref.get("zero_balance")})

    def pct(vals, p):
        if not vals:
            return None
        s = sorted(vals)
        k = (len(s) - 1) * p
        f, c = int(k), min(int(k) + 1, len(s) - 1)
        return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)

    result["n_comparable_price"] = len(ratios)
    if ratios:
        result["ratio_summary"] = {"n": len(ratios), "median": median(ratios), "p10": pct(ratios, 0.10), "p90": pct(ratios, 0.90),
                                    "pct_within_0.95_1.05": round(sum(1 for x in ratios if 0.95 <= x <= 1.05) / len(ratios), 4)}
    result["first_entry_agreement_rate"] = round(first_entry_agree / first_entry_comparable, 4) if first_entry_comparable else None
    result["per_tx_sample"] = per_tx[:20]
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[source_a] покрытие_vs_300={result['coverage_vs_300']}, отношение={result.get('ratio_summary')}, "
          f"согласие_первого_входа={result['first_entry_agreement_rate']}", flush=True)

    # --- 3 кандидата -- частоты, сравнение с table_v2 ---
    table_v2 = json.loads((REPO_ROOT / "data" / "solana_29_candidates_table_v2.json").read_text())
    table_v2_by_addr = {row["address"]: row for row in table_v2.get("rows", [])}
    flow = json.loads((REPO_ROOT / "data" / "solana_29_candidates_flow_7d_sol_equiv.json").read_text())

    cand_summary = []
    for addr in CANDIDATES_3:
        v = flow["wallets"].get(addr)
        if not v or not v.get("first_entry_events"):
            cand_summary.append({"address": addr, "status": "no_local_reference_window"})
            continue
        events_ref = v["first_entry_events"]
        lo_c, hi_c = min(e["block_time"] for e in events_ref), max(e["block_time"] for e in events_ref)
        days = v.get("days_covered") or 7
        cs = scan_wallet(addr, api_key, lo_c, hi_c)
        n_ge2 = sum(1 for e in cs["events"] if e.get("is_first_entry") and e.get("spend_sol_equiv") and e["spend_sol_equiv"] >= 2)
        n_ge43 = sum(1 for e in cs["events"] if e.get("is_first_entry") and e.get("spend_sol_equiv") and e["spend_sol_equiv"] >= 4.3)
        our_per_day_2 = round(n_ge2 / days, 3)
        our_per_day_43 = round(n_ge43 / days, 3)
        ref_2 = table_v2_by_addr.get(addr, {}).get("per_day_ge_2")
        ref_43 = table_v2_by_addr.get(addr, {}).get("per_day_ge_4.3")
        entry = {"address": addr, "n_price_missing": cs["n_price_missing"],
                  "helius_per_day_ge_2": our_per_day_2, "table_v2_per_day_ge_2": ref_2,
                  "helius_per_day_ge_4.3": our_per_day_43, "table_v2_per_day_ge_4.3": ref_43,
                  "within_15pct_ge_2": (abs(our_per_day_2 - ref_2) / ref_2 <= 0.15) if ref_2 else None,
                  "within_15pct_ge_4.3": (abs(our_per_day_43 - ref_43) / ref_43 <= 0.15) if ref_43 else None}
        cand_summary.append(entry)
        OUT_PATH.write_text(json.dumps({**result, "candidates_3": cand_summary}, ensure_ascii=False, indent=2, default=str))
        print(f"[source_a] кандидат {addr[:10]}..: {entry}", flush=True)

    result["candidates_3"] = cand_summary

    # --- Стоимость/скорость на нашем тарифе ---
    result["cost_estimate"] = {
        "n_requests_total": _request_count,
        "avg_request_latency_s": round(sum(_request_timings) / len(_request_timings), 3) if _request_timings else None,
        "implied_max_req_per_sec": round(1 / (sum(_request_timings) / len(_request_timings)), 2) if _request_timings else None,
        "avg_tx_per_page": leader_scan["pagination_metrics"].get("avg_page_size"),
        "note": "Helius free-план биллит по числу запросов, не по 'кредитам за запрос' в ответе -- честно не выдумываем "
                "цифру кредитов, которую API не возвращает. Оценка стоимости истории на 230 кошельков ниже -- по "
                "числу запросов, экстраполированному от факта на лидере+3 кандидатах.",
    }
    n_req_per_wallet_avg = _request_count / (1 + len(CANDIDATES_3))
    result["cost_estimate"]["extrapolated_requests_for_230_wallets_7d_history"] = round(n_req_per_wallet_avg * 230)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[source_a] стоимость: {result['cost_estimate']}", flush=True)
    print(f"[source_a] завершено", flush=True)


if __name__ == "__main__":
    main()
