#!/usr/bin/env python3
"""Владелец, 2026-09-19: гипотеза "AgmLJBM... = спонсор газа Fomo"
(ПРИОРИТЕТ). Внешнее подтверждение УЖЕ ЕСТЬ (WebSearch, вне этого
скрипта): Solscan публично помечает адрес как "Fomo Co-signer",
независимо подтверждено двумя ветками в X (0x0kisuke, ThisIsNuse) --
это НЕ один трейдер, а общий ко-подписант всего приложения Fomo
(no-seed-phrase кошелёк, подписывает от имени всей базы пользователей).//
Прямой fetch solscan.io/x.com заблокирован сетевой политикой песочницы --
цитаты из WebSearch, не выдуманы.

Этот скрипт -- количественная часть: ВСЕ запросы к solana.transactions
СТРОГО ограничены по времени, начиная с окна в 1 час (эскалация до 6ч,
24ч). Room для проверки:
  1. Сколько разных "вторых подписантов" обслуживает Agm за 1ч/6ч/24ч.
  2. Из наших 27 отслеживаемых (BATCH-1/2/3) и 29 кандидатов -- у кого
     Agm стоит ПЛАТЕЛЬЩИКОМ (scalar signer) в их свопах за последние 24ч
     (27 кошельков) -- отдельно frank/DipWheeler: сами себе плательщик
     или нет. Для 29 кандидатов -- по УЖЕ ИЗВЕСТНЫМ сигнатурам их первых
     входов (bounded IN(...), не блуждающий скан).
  3. Кто пополняет Agm (через RPC -- дешевле и надёжнее, чем декодировать
     вложенные instructions в Dune SQL) -- и есть ли у ТОГО ЖЕ источника
     другие похожие кошельки-«братья».
  4. Честная стоимость в кредитах для окна в сутки."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_fomo_sponsor_check.json"
BASELINE_PATH = REPO_ROOT / "data" / "dbot_sieve_baseline.json"
FLOW_PATH = REPO_ROOT / "data" / "solana_29_candidates_flow_7d_sol_equiv.json"
SPONSOR = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
TX_TABLE, SIG_COL = "solana.transactions", "id"
FRESHNESS_LAG_S = 6 * 3600  # установлено в Task A этой сессии -- реальный лаг Dune на этих таблицах


def collect_batch_wallets() -> list[dict]:
    baseline = json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.exists() else {}
    out = []
    for name, b in (baseline.get("batches") or {}).items():
        for tw in b.get("tracked_wallets", []):
            if tw.get("address"):
                out.append({"address": tw["address"], "remark": tw.get("remark"), "batch": name})
    return out


def collect_candidate_signatures() -> list[dict]:
    """Известные сигнатуры первых входов 29 кандидатов -- используем
    IN(...) с УЖЕ известным диапазоном времени, не блуждающий скан по
    адресу за 7 суток (дорого)."""
    flow = json.loads(FLOW_PATH.read_text()) if FLOW_PATH.exists() else {}
    sigs = []
    for addr, v in (flow.get("wallets") or {}).items():
        if not v:
            continue
        for e in v.get("first_entry_events") or []:
            if e.get("signature") and e.get("block_time"):
                sigs.append({"address": addr, "signature": e["signature"], "block_time": e["block_time"]})
    return sigs


def find_sponsor_funding_sources(max_pages: int = 5) -> dict:
    """Кто пополняет Agm -- через RPC (Alchemy), не Dune: дешевле и
    надёжнее для декодирования вложенных System Transfer инструкций."""
    sigs, before = [], None
    for _ in range(max_pages):
        batch = fp.get_signatures_for_address(SPONSOR, before=before)
        if not batch:
            break
        sigs.extend(batch)
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break
    print(f"[fomo] Agm: просканировано {len(sigs)} последних подписей (RPC)", flush=True)

    funders = Counter()
    funding_events = []
    n_checked = 0
    for s in sigs[:800]:  # ограничиваем реальную работу с транзакциями, подписи уже все получены дёшево
        if s.get("err") is not None:
            continue
        tx = fp.get_transaction(s["signature"])
        if tx is None:
            continue
        n_checked += 1
        instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
        inner = (tx.get("meta") or {}).get("innerInstructions") or []

        def scan(ix_list):
            for ix in ix_list:
                if not isinstance(ix, dict):
                    continue
                parsed = ix.get("parsed") or {}
                if not isinstance(parsed, dict) or parsed.get("type") != "transfer" or ix.get("program") != "system":
                    continue
                info = parsed.get("info", {})
                if info.get("destination") == SPONSOR and info.get("source") != SPONSOR:
                    funders[info.get("source")] += 1
                    funding_events.append({"signature": s["signature"], "block_time": s.get("blockTime"),
                                            "source": info.get("source"), "lamports": info.get("lamports")})

        scan(instrs)
        for grp in inner:
            if isinstance(grp, dict):
                scan(grp.get("instructions", []))
    return {"n_signatures_scanned": len(sigs), "n_tx_checked_for_transfers": n_checked,
            "funders_by_count": dict(funders.most_common(10)),
            "n_funding_events_found": len(funding_events), "funding_events_sample": funding_events[:15]}


def main() -> None:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "headline_external_confirmation": (
                         "Solscan публично помечает AgmLJBM... как 'Fomo Co-signer' (WebSearch, "
                         "solscan.io/account/AgmLJBM... -- прямой fetch страницы заблокирован сетевой политикой "
                         "песочницы, но заголовок листинга поиска цитирует тег дословно). Независимо "
                         "подтверждено двумя ветками в X: 0x0kisuke описывает его как общий ко-подписант "
                         "приложения Fomo (не один трейдер -- вся база пользователей через один кошелёк "
                         "приложения без seed-фразы), ThisIsNuse -- как маршрутизатор/сервис, используемый "
                         "почти во всех pump.fun-подобных токенах."
                     )}

    print("[fomo] шаг 1/3: RPC -- кто пополняет Agm", flush=True)
    result["funding_sources"] = find_sponsor_funding_sources()
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[fomo] funders_by_count: {result['funding_sources']['funders_by_count']}", flush=True)

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result["step0_key_discovery"] = discovery
    if key_name is None:
        result["HONEST_ANSWER_dune"] = "DUNE_EXPLORER_API не живой -- количественная часть на Dune пропущена."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[fomo] " + result["HONEST_ANSWER_dune"], flush=True)
        return
    probe = DuneProbe(os.environ[key_name])
    now = int(time.time()) - FRESHNESS_LAG_S

    # --- Шаг 2: сколько разных "вторых подписантов" Agm обслуживает за 1ч/6ч/24ч ---
    windows = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600}
    result["sponsor_activity_by_window"] = {}
    for label, dur in windows.items():
        lo, hi = now - dur, now
        sql = (f"SELECT signers, block_time FROM {TX_TABLE} WHERE signer = '{SPONSOR}' "
               f"AND block_time BETWEEN from_unixtime({lo}) AND from_unixtime({hi})")
        r = probe.run_sql_sync(f"fomo_activity_{label}", sql, timeout_s=300)
        entry = {"dune_step": {k: v for k, v in r.items() if k != "rows"}}
        if r.get("status") == "ok":
            others = set()
            for row in r["rows"]:
                for s in (row.get("signers") or []):
                    if s != SPONSOR:
                        others.add(s)
            entry["n_tx"] = len(r["rows"])
            entry["n_distinct_other_signers"] = len(others)
        result["sponsor_activity_by_window"][label] = entry
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(f"[fomo] окно {label}: n_tx={entry.get('n_tx')} различных_вторых_подписантов={entry.get('n_distinct_other_signers')}", flush=True)

    # --- Шаг 3а: наши 27 отслеживаемых -- Agm плательщик? (24ч, VALUES+UNNEST join) ---
    batch_wallets = collect_batch_wallets()
    values_sql = ",".join(f"('{w['address']}')" for w in batch_wallets)
    lo24, hi24 = now - windows["24h"], now
    sql_batch = (
        f"WITH wallets(addr) AS (VALUES {values_sql}) "
        f"SELECT w.addr, tt.signer, tt.id AS tx_id, tt.block_time FROM {TX_TABLE} tt "
        f"CROSS JOIN UNNEST(tt.signers) AS s(signer_addr) JOIN wallets w ON w.addr = s.signer_addr "
        f"WHERE tt.block_time BETWEEN from_unixtime({lo24}) AND from_unixtime({hi24})"
    )
    r_batch = probe.run_sql_sync("fomo_check_27_batch_wallets", sql_batch, timeout_s=590)
    result["step3a_batch_wallets_dune_step"] = {k: v for k, v in r_batch.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if r_batch.get("status") == "ok":
        by_wallet: dict[str, list] = {}
        for row in r_batch["rows"]:
            by_wallet.setdefault(row["addr"], []).append(row)
        per_wallet = []
        for w in batch_wallets:
            rows = by_wallet.get(w["address"], [])
            n_agm_payer = sum(1 for row in rows if row.get("signer") == SPONSOR)
            n_self_payer = sum(1 for row in rows if row.get("signer") == w["address"])
            per_wallet.append({**w, "n_tx_found_24h": len(rows), "n_agm_is_payer": n_agm_payer,
                                "n_self_is_payer": n_self_payer,
                                "n_other_payer": len(rows) - n_agm_payer - n_self_payer})
        result["step3a_per_wallet"] = per_wallet
        n_any_agm = sum(1 for p in per_wallet if p["n_agm_is_payer"] > 0)
        result["step3a_summary"] = {"n_wallets_total": len(per_wallet), "n_wallets_with_agm_as_payer": n_any_agm}
        frank_dip = [p for p in per_wallet if p.get("remark") in ("frank", "DipWheeler")]
        result["step3a_frank_dipwheeler"] = frank_dip
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(f"[fomo] батч-кошельков с Agm-плательщиком за 24ч: {n_any_agm}/{len(per_wallet)}", flush=True)
        for p in frank_dip:
            print(f"[fomo]   {p['remark']}: agm_payer={p['n_agm_is_payer']} self_payer={p['n_self_is_payer']} n_tx={p['n_tx_found_24h']}", flush=True)

    # --- Шаг 3б: 29 кандидатов -- по известным сигнатурам их первых входов ---
    cand_sigs = collect_candidate_signatures()
    if cand_sigs:
        sig_list_sql = ",".join(f"'{c['signature']}'" for c in cand_sigs)
        lo_c, hi_c = min(c["block_time"] for c in cand_sigs) - 60, max(c["block_time"] for c in cand_sigs) + 60
        sql_cand = (f"SELECT id AS tx_id, signer, signers FROM {TX_TABLE} "
                    f"WHERE block_time BETWEEN from_unixtime({lo_c}) AND from_unixtime({hi_c}) "
                    f"AND {SIG_COL} IN ({sig_list_sql})")
        r_cand = probe.run_sql_sync("fomo_check_29_candidates", sql_cand, timeout_s=590)
        result["step3b_candidates_dune_step"] = {k: v for k, v in r_cand.items() if k != "rows"}
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if r_cand.get("status") == "ok":
            by_sig = {row["tx_id"]: row for row in r_cand["rows"]}
            per_cand: dict[str, dict] = {}
            for c in cand_sigs:
                row = by_sig.get(c["signature"])
                d = per_cand.setdefault(c["address"], {"address": c["address"], "n_events_checked": 0,
                                                         "n_found_in_dune": 0, "n_agm_is_payer": 0, "n_self_is_payer": 0})
                d["n_events_checked"] += 1
                if row:
                    d["n_found_in_dune"] += 1
                    if row.get("signer") == SPONSOR:
                        d["n_agm_is_payer"] += 1
                    elif row.get("signer") == c["address"]:
                        d["n_self_is_payer"] += 1
            result["step3b_per_candidate"] = list(per_cand.values())
            n_any_agm_cand = sum(1 for d in per_cand.values() if d["n_agm_is_payer"] > 0)
            result["step3b_summary"] = {"n_candidates_total": len(per_cand), "n_with_agm_as_payer": n_any_agm_cand}
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print(f"[fomo] кандидатов с Agm-плательщиком (по известным входам): {n_any_agm_cand}/{len(per_cand)}", flush=True)

    # --- Шаг 4: стоимость на сутки ---
    credits = []
    for key in ("1h", "6h", "24h"):
        meta = (result.get("sponsor_activity_by_window", {}).get(key, {}).get("dune_step") or {}).get("status_meta") or {}
        c = meta.get("execution_cost_credits")
        if c:
            credits.append({"window": key, "credits": c})
    batch_meta = (result.get("step3a_batch_wallets_dune_step") or {}).get("status_meta") or {}
    if batch_meta.get("execution_cost_credits"):
        credits.append({"window": "27_wallets_24h", "credits": batch_meta["execution_cost_credits"]})
    result["step4_cost_summary"] = {"credits_by_query": credits,
                                     "total_this_run": sum(c["credits"] for c in credits)}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    result["CONCLUSION"] = (
        "СПОНСОР FOMO -- вывод основан на: (1) внешнем публичном лейбле Solscan 'Fomo Co-signer' + два "
        "независимых разбора в X (headline_external_confirmation); (2) 100% (300/300) наших leader-транзакций "
        "имеют Agm первым подписантом (Task 1 прошлого сообщения); (3) он НЕ программа, обычный System-аккаунт "
        "(проверено RPC); (4) количественная картина в этом файле (активность за 1ч/6ч/24ч, доля наших "
        "27+29 кошельков с Agm-плательщиком, источники пополнения) -- см. соответствующие секции."
    )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[fomo] {result['CONCLUSION']}", flush=True)


if __name__ == "__main__":
    main()
