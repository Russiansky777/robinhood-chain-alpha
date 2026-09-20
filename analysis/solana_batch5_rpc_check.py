#!/usr/bin/env python3
"""Владелец: RPC-скан кандидатов BATCH-5 (первые входы за 72ч), только RPC.

Метод классификации -- тот же, что solana_phase2_build_events.py
(Источник B, уже подтверждён на 300/300), но БЕЗ Dune: pre/post token
balances из getTransaction вместо solana.transactions, spend_sol_equiv
БЕЗ стейбл-ноги (USDC/USDT->SOL требует курса из Dune prices.usd --
не трогаем; сделки, профинансированные ТОЛЬКО в стейблах, честно
считаются отдельно, не входят в median/пороги SOL).

ИСПРАВЛЕНО (контроль на LEADER_WALLET/Brez): без проверки на реальную
DEX/AMM-программу в транзакции метод засчитывал в "первые входы" любые
непокупные транзакции (переводы, дасты) с крошечным spend_sol_equiv
(~сетевая комиссия) -- лидер и Brez показывали 0 входов >=2 SOL при
известной активной торговле. См. tx_program_ids/DEX_PROGRAMS ниже.
Валидация фикса на эталонах ЕЩЁ НЕ прогнана -- лидер имеет ~1600 подписей/72ч,
повторный скан конкурировал бы за RPC-бюджет с основным сканом
кандидатов (приоритет владельца), отложено до паузы/отдельного разрешения.

"Первый вход" -- буквально как просил владелец: баланс МИНТА у кошелька
ДО этой конкретной транзакции == 0 (или запись отсутствует), после > 0,
кошелёк -- подписант. Многоминтовые tx (вырос больше одного немонетного
минта) -- пропускаются как амбигуция (n_multi_mint_skipped), не
подмешиваются молча -- та же оговорка, что в phase2.

Приоритет: сначала 10 кошельков владельца (данные ночью, ещё не
проверены), затем остальные кандидаты из fomo_leaderboard_candidates.json
(final_table_sorted_by_followers), той же цепочкой, бюджетируемо и
возобновляемо."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_entry_log import tx_signers  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_PATH = REPO_ROOT / "data" / "fomo_leaderboard_candidates.json"
OUT_PATH = REPO_ROOT / "data" / "solana_batch5_rpc_check.json"

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLE_MINTS = {USDC, USDT}

# Владелец, контроль метода: лидер и Brez давали 0 входов >=2 SOL при
# известной активной торговле -- баланс-only классификация без проверки
# на реальный DEX/AMM ловила массу непокупных транзакций (переводы,
# дасты) с крошечным spend_sol_equiv (~сетевая комиссия), топя реальные
# свопы в шуме. Фикс: "первый вход" засчитывается ТОЛЬКО если tx также
# содержит известную DEX/AMM программу -- список из уже собранного в
# репозитории dex_labels.json (не выдумано здесь, использовался и
# раньше этой сессией).
DEX_LABELS_PATH = REPO_ROOT / "data" / "solana_buyer_200" / "prior" / "current" / "buyer_100" / "dex_labels.json"
DEX_PROGRAMS = set(json.loads(DEX_LABELS_PATH.read_text()).keys()) if DEX_LABELS_PATH.exists() else set()


def tx_program_ids(tx: dict) -> set[str]:
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    inner = (tx.get("meta") or {}).get("innerInstructions") or []
    ids = {ix.get("programId") for ix in instrs if isinstance(ix, dict)}
    for g in inner:
        for ix in g.get("instructions", []):
            if isinstance(ix, dict):
                ids.add(ix.get("programId"))
    return {i for i in ids if i}

LOOKBACK_HOURS = 72
LOOKBACK_S = LOOKBACK_HOURS * 3600
TIME_BUDGET_S = 40 * 60
COMMIT_INTERVAL_S = 60

PRIORITY_10 = [
    ("3qBVJLMAWXCbW3nhz99T99in82zZeUHkbDeSDJThbN4Z", "leo"),
    ("EVqxB3F6iUBeWsTpBFQqWwxpqUS8s4NrzgxBvQ2VRbTq", "Jack"),
    ("59mWSDjx5VFQz15FGJio8BJzupKorK5SVHSbaJaZGAxC", "Fartman"),
    ("EC2f5DnHzuNRit1ExqghSifDbp1wgrzktsRRZCtU92MJ", "Theo"),
    ("4ZZW5ePCAsHJdnHHUdgozdvLqYNVnkE9CgrW3iyZdskb", "bluntz"),
    ("7FJDJ1HaQfL3GAcMCzoBrJ9Accnf5GUYYwfEPCBCDcVK", "Crissy"),
    ("DmopudSGaQQenK4cW5NgmHUsexMB3sEJNn6n1EfLKs28", "Werey"),
    ("D9tPQeij7vSTZwkxzxZibso4GFuRW8aBMpCg5QhCSfVL", "Him"),
    ("HmLwYRdPE3QsSexagobJdjxcWFZgqrSXtsQUrRZLWqnB", "cosby"),
    ("Ak6gsstZwaRDYKnzdyNg2HvCXDFvv21afjVix9VGRMQv", "RugDalio"),
]


def fetch_signatures_last_n_hours(address: str, lookback_s: int) -> list[dict]:
    """Пагинация назад БЕЗ якоря (getSignaturesForAddress уже отдаёт от
    последней подписи, before= не нужен на первой странице) -- до тех
    пор, пока не пересечём cutoff по blockTime, либо кошелёк не
    исчерпался."""
    cutoff = int(time.time()) - lookback_s
    hist: list[dict] = []
    before = None
    while True:
        if fp.soft_deadline_exceeded():
            raise RuntimeError("fetch_signatures_last_n_hours: мягкий дедлайн тика истёк во время пагинации")
        page = fp.get_signatures_for_address(address, before=before, limit=1000)
        if not page:
            break
        hist.extend(page)
        oldest = page[-1].get("blockTime")
        before = page[-1]["signature"]
        if oldest is not None and oldest <= cutoff:
            break
        if len(page) < 1000:
            break
    return [h for h in hist if h.get("blockTime") is not None and h["blockTime"] >= cutoff]


def mint_balance_map(tb_list: list, owner: str) -> dict:
    out: dict = {}
    for b in tb_list or []:
        if b.get("owner") != owner:
            continue
        amt = D(b["uiTokenAmount"]["amount"]) / D(10) ** b["uiTokenAmount"]["decimals"]
        out[b["mint"]] = out.get(b["mint"], D(0)) + amt
    return out


def classify_tx(tx: dict, wallet: str) -> dict | None:
    """Возвращает событие первого входа, либо None (не покупка/не первый
    вход/амбигуция -- амбигуция помечена отдельным полем skipped_multi_mint)."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    if wallet not in tx_signers(tx):
        return None
    if not (tx_program_ids(tx) & DEX_PROGRAMS):
        return {"kind": "no_first_entry"}  # не своп -- перевод/даст/другое, честно не считаем покупкой
    # Владелец: ключи = static accountKeys + loadedAddresses (versioned tx с ALT) --
    # preBalances/postBalances индексируются по этому полному списку.
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

    bought = []
    for mint, post_amt in post_tb.items():
        if mint in STABLE_MINTS or mint == WSOL:
            continue
        pre_amt = pre_tb.get(mint, D(0))
        if pre_amt == 0 and post_amt > 0:
            bought.append((mint, post_amt))
    if not bought:
        return {"kind": "no_first_entry"}
    if len(bought) > 1:
        return {"kind": "multi_mint_skipped"}
    mint, tokens_received = bought[0]

    sol_decrease = max(D(0), (D(pre_sol) - D(post_sol)) / D(10) ** 9) if pre_sol is not None and post_sol is not None else D(0)
    wsol_decrease = max(D(0), pre_tb.get(WSOL, D(0)) - post_tb.get(WSOL, D(0)))
    usdc_decrease = max(D(0), pre_tb.get(USDC, D(0)) - post_tb.get(USDC, D(0)))
    usdt_decrease = max(D(0), pre_tb.get(USDT, D(0)) - post_tb.get(USDT, D(0)))
    stable_usd = usdc_decrease + usdt_decrease

    # Владелец: "как в Dune-шлюзе" -- USDC/USDT тоже переводятся в SOL по
    # курсу, не оставляются отдельно. Диагностика на известной покупке
    # лидера (egp1f5j9ld) подтвердила: это НЕ редкий случай -- Fomo-сделки
    # реально идут через USDC без WSOL-ноги вообще. Курс -- через уже
    # используемый в репозитории Gecko SOL/USDC (та же пара, что и везде
    # в проекте для этой конвертации, не Dune) -- ОДИН диапазон минутных
    # свечей на кэш, не по вызову на транзакцию.
    stable_sol_equiv = D(0)
    price_missing = False
    if stable_usd > 0:
        block_time = tx.get("blockTime")
        if block_time is None:
            price_missing = True
        else:
            try:
                price_row = fp.find_price_at_gecko(block_time, block_time - 3600, block_time + 3600)
                sol_usd = D(price_row["event"]["p1_per_0"])
                if sol_usd > 0:
                    stable_sol_equiv = stable_usd / sol_usd
                else:
                    price_missing = True
            except Exception:  # noqa: BLE001 -- честно: не удалось получить курс, не выдумываем
                price_missing = True

    spend_sol_equiv = sol_decrease + wsol_decrease + stable_sol_equiv
    if spend_sol_equiv <= 0:
        return {"kind": "no_first_entry"}  # ни SOL, ни WSOL, ни стейбл (с курсом) не потрачен -- не покупка

    return {
        "kind": "first_entry", "mint": mint, "tokens_received": str(tokens_received),
        "spend_sol_equiv": float(spend_sol_equiv),
        "sol_decrease": float(sol_decrease), "wsol_decrease": float(wsol_decrease),
        "stable_usd_spent": float(stable_usd) if stable_usd > 0 else None,
        "stable_sol_equiv": float(stable_sol_equiv) if stable_usd > 0 else None,
        "price_missing": price_missing,
        "slot": tx["slot"], "signature": tx["transaction"]["signatures"][0],
    }


MAX_TX_PER_WALLET = 300  # владелец: цена скана ограничена, честный статус "частично" при превышении


def scan_wallet(address: str) -> dict:
    sigs = fetch_signatures_last_n_hours(address, LOOKBACK_S)  # уже newest-first, отфильтровано по 72ч
    ok_sigs_full = [s for s in sigs if s.get("err") is None]
    capped = len(ok_sigs_full) > MAX_TX_PER_WALLET
    ok_sigs = [s["signature"] for s in ok_sigs_full[:MAX_TX_PER_WALLET]]

    entries, n_multi_mint_skipped, n_tx_fetch_failed, n_price_missing = [], 0, 0, 0
    # Владелец: было по одной getTransaction на подпись (до 300 за
    # кошелёк) -- главный тормоз скана. Пачками по 20, как в скане 217
    # (fp.get_transactions_batch, честный одиночный повтор только на
    # null внутри батча).
    tx_by_sig = fp.get_transactions_batch(ok_sigs)
    for sig in ok_sigs:
        tx = tx_by_sig.get(sig)
        if tx is None:
            n_tx_fetch_failed += 1
            continue
        ev = classify_tx(tx, address)
        if ev is None or ev["kind"] == "no_first_entry":
            continue
        if ev["kind"] == "multi_mint_skipped":
            n_multi_mint_skipped += 1
            continue
        if ev.get("price_missing"):
            n_price_missing += 1
        entries.append(ev)

    sol_sizes = [e["spend_sol_equiv"] for e in entries]
    now = int(time.time())
    oldest_covered_bt = ok_sigs_full[len(ok_sigs) - 1].get("blockTime") if ok_sigs else None
    coverage_hours_actual = round((now - oldest_covered_bt) / 3600, 2) if oldest_covered_bt else LOOKBACK_HOURS
    coverage_status = ("partial_capped_at_300_tx" if capped else
                        ("full_72h" if coverage_hours_actual >= LOOKBACK_HOURS * 0.95 else "wallet_history_shorter_than_72h"))
    return {
        "n_tx_scanned": len(ok_sigs), "n_tx_in_window_before_cap": len(ok_sigs_full),
        "n_tx_fetch_failed": n_tx_fetch_failed, "n_multi_mint_skipped": n_multi_mint_skipped,
        "n_price_missing": n_price_missing,
        "coverage_status": coverage_status, "coverage_hours_actual": coverage_hours_actual,
        "n_first_entries_72h": len(entries),
        "n_first_entries_ge_2sol": sum(1 for s in sol_sizes if s >= 2),
        "n_first_entries_ge_4_3sol": sum(1 for s in sol_sizes if s >= 4.3),
        "median_spend_sol_equiv": round(median(sol_sizes), 4) if sol_sizes else None,
        "n_priced_sol_equiv": len(sol_sizes),
        "entries": entries,
    }


def main() -> None:
    result = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {
        "lookback_hours": LOOKBACK_HOURS, "priority_10": {}, "remaining_scan": {},
    }
    result.setdefault("priority_10", {})
    result.setdefault("remaining_scan", {})

    print(f"[batch5] приоритет: {len(PRIORITY_10)} кошельков владельца", flush=True)
    for addr, name in PRIORITY_10:
        if addr in result["priority_10"]:
            continue
        try:
            row = scan_wallet(addr)
        except RuntimeError as exc:
            result["priority_10"][addr] = {"name": name, "error": str(exc)[:300]}
            print(f"[batch5] {name} ({addr[:10]}..) ошибка: {exc}", flush=True)
            continue
        row["name"] = name
        result["priority_10"][addr] = row
        print(f"[batch5] {name} ({addr[:10]}..): вход72ч={row['n_first_entries_72h']} "
              f">=2SOL={row['n_first_entries_ge_2sol']} >=4.3SOL={row['n_first_entries_ge_4_3sol']} "
              f"медиана={row['median_spend_sol_equiv']}", flush=True)
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        fp._git_commit_progress("batch5_rpc_check_priority10", [OUT_PATH])

    priority_table = [{"address": a, "name": r.get("name"), "n_first_entries_72h": r.get("n_first_entries_72h"),
                        "n_ge_2sol": r.get("n_first_entries_ge_2sol"), "n_ge_4_3sol": r.get("n_first_entries_ge_4_3sol"),
                        "median_spend_sol_equiv": r.get("median_spend_sol_equiv")}
                       for a, r in result["priority_10"].items()]
    result["priority_10_table"] = priority_table
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print("[batch5] таблица приоритетных 10 готова, переходим к остальным кандидатам", flush=True)

    if not CANDIDATES_PATH.exists():
        print("[batch5] нет fomo_leaderboard_candidates.json -- остальные кандидаты не сканируем.", flush=True)
        return
    candidates = json.loads(CANDIDATES_PATH.read_text())
    rows = candidates.get("final_table_sorted_by_followers") or []
    priority_addrs = {a for a, _ in PRIORITY_10}
    remaining = [r for r in rows if r["solana_address"] not in priority_addrs]
    print(f"[batch5] остальных кандидатов (без пересечения с приоритетными 10): {len(remaining)}", flush=True)

    scanned = result["remaining_scan"]
    started_at = time.monotonic()
    last_commit_at = started_at
    n_done_this_run = 0
    for row in remaining:
        addr = row["solana_address"]
        if addr in scanned:
            continue
        if time.monotonic() - started_at > TIME_BUDGET_S:
            print("[batch5] бюджет времени исчерпан -- остальное на следующий прогон", flush=True)
            break
        try:
            r = scan_wallet(addr)
        except RuntimeError as exc:
            scanned[addr] = {"name": row.get("nickname"), "error": str(exc)[:300]}
            print(f"[batch5] {addr[:10]}.. ошибка: {exc}", flush=True)
            continue
        r["name"] = row.get("nickname")
        r.pop("entries", None)  # для остальных ~237 -- не храним построчные события, только агрегаты (объём)
        scanned[addr] = r
        n_done_this_run += 1
        rate = round(r["n_first_entries_ge_2sol"] / (LOOKBACK_HOURS / 24), 3)
        print(f"[batch5] {row.get('nickname')} ({addr[:10]}..): вход72ч={r['n_first_entries_72h']} "
              f">=2SOL/сутки={rate}", flush=True)
        if time.monotonic() - last_commit_at > COMMIT_INTERVAL_S:
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            fp._git_commit_progress("batch5_rpc_check_remaining", [OUT_PATH])
            last_commit_at = time.monotonic()

    n_scanned = len(scanned)
    print(f"[batch5] прогон: {n_done_this_run} просканировано сейчас; всего {n_scanned}/{len(remaining)}", flush=True)

    ranked = [(a, v) for a, v in scanned.items() if "error" not in v]
    ranked.sort(key=lambda kv: kv[1]["n_first_entries_ge_2sol"] / (LOOKBACK_HOURS / 24), reverse=True)
    top10 = [{"address": a, "name": v.get("name"), "n_first_entries_72h": v["n_first_entries_72h"],
              "n_ge_2sol_per_day": round(v["n_first_entries_ge_2sol"] / (LOOKBACK_HOURS / 24), 3),
              "n_ge_2sol": v["n_first_entries_ge_2sol"], "n_ge_4_3sol": v["n_first_entries_ge_4_3sol"],
              "median_spend_sol_equiv": v["median_spend_sol_equiv"]}
             for a, v in ranked[:10]]
    result["n_remaining_total"] = len(remaining)
    result["n_remaining_scanned"] = n_scanned
    result["remaining_scan_complete"] = n_scanned >= len(remaining)
    result["top10_replacement_candidates"] = top10
    result["top10_replacement_address_name_csv"] = "\n".join(f"{t['address']},{t['name']}" for t in top10)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print("=== ТОП-10 замены (address,name) ===", flush=True)
    print(result["top10_replacement_address_name_csv"], flush=True)
    print(f"[batch5] remaining_scan_complete={result['remaining_scan_complete']} ({n_scanned}/{len(remaining)})", flush=True)


if __name__ == "__main__":
    main()
