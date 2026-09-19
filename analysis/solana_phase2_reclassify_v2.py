#!/usr/bin/env python3
"""Владелец, 2026-09-19: пересчёт Фазы 2 v2 -- ключ DUNE_JANA_API
(Plus, остаток 1480 кредитов на момент смены; DUNE_EXPLORER_API больше
не используем -- см. solana_dune_explorer_check.py).

Гипотеза владельца принята, реальный баг: старый classify_rows (в
solana_phase2_build_events.py) при >1 "купленных" немонетных минтов в
одной транзакции ВЫБРАСЫВАЛ ВСЮ транзакцию (n_multi_mint_tx_skipped=808),
включая настоящую покупку, если рядом сопутствующе капнул наградной
токен (STONK/$UBI и подобные с stonkfun.xyz).

ПОПРАВКА владельца (учтена): НЕ перевыгружаем всю неделю заново --
у нас уже есть 18526 одноминтовых событий (data/solana_phase2_events.
json). Выгружаем ТОЛЬКО транзакции, где у кошелька выросли >=2 минта
(кроме WSOL) -- фильтр (cardinality/filter по уже отфильтрованным на
трейдера pre_tb/post_tb массивам, БЕЗ UNNEST) ВНУТРИ SQL, минимум
столбцов (id, block_time, block_slot, trader, pre_sol, post_sol,
pre_tb, post_tb, sol_usd_price). Ожидаемо на 1-2 порядка меньше строк,
чем полный прогон (n_multi_mint_tx_skipped=808 -- ориентир по числу
транзакций, не событий). Результат сохраняется В РЕПОЗИТОРИЙ СЖАТЫМ
(data/solana_phase2_multimint_raw_v2.json.gz) СРАЗУ после получения,
до любой обработки.

Наградные минты определяются СИСТЕМНО на этом же (только многоминтовом)
датасете: минт, встретившийся как один из >=2 растущих минтов в
транзакции у >=MIN_WALLETS_COMPANION РАЗНЫХ кошельков -- по построению
каждая строка здесь уже многоминтовая, поэтому сам факт широкого
распространения по разным кошелькам ГОВОРИТ о сопутствующем начислении,
а не о независимом решении купить именно этот минт (независимая
двойная покупка у одного и того же человека -- redkost и не даёт
широкого разброса по чужим кошелькам). Для многоминтовой tx: если
ровно один из растущих минтов НЕ наградной -- это настоящая покупка,
берётся с ПОЛНОЙ (не разделённой) тратой транзакции; если 0 или 2+
"настоящих" -- честно выбрасывается отдельным счётчиком (не молча).

Итоговые события = 18526 старых одноминтовых (без реклассификации минтов
внутри них -- они и так однозначны) + спасённые из многоминтовых, с
пересчётом first_entry/k по кошельку+минту хронологически по
ОБЪЕДИНЁННому потоку. Сверка с контролем: лидер (>=4.3 SOL/сутки против
13.6 RPC-эталона, ±15%), 29 кандидатов (оба порога против table_v2,
±15%) -- до и после, отдельной таблицей."""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OLD_EVENTS_PATH = REPO_ROOT / "data" / "solana_phase2_events.json"
RAW_MULTIMINT_PATH = REPO_ROOT / "data" / "solana_phase2_multimint_raw_v2.json.gz"
OUT_PATH = REPO_ROOT / "data" / "solana_phase2_events_v2.json"
COMPARE_PATH = REPO_ROOT / "data" / "solana_phase2_reclassify_v2_comparison.json"

TX_TABLE = "solana.transactions"
FOMO_SPONSOR = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WSOL_MINT = "So11111111111111111111111111111111111111112"
STABLE_MINTS = {USDC_MINT, USDT_MINT, WSOL_MINT}

MIN_WALLETS_COMPANION = 5
MAX_QUERY_COST_CREDITS_WARN = 200.0  # честный предупредительный порог для ЭТОГО одного запроса


def sql_lit_list(addrs: list[str]) -> str:
    return ",".join(f"'{a}'" for a in addrs)


def build_multimint_sql(wallets: list[str], lo: int, hi: int) -> str:
    values = sql_lit_list(wallets)
    non_wsol_growth_expr = (
        "filter(e.post_tb, x -> x[2] != '{wsol}' AND "
        "try_cast(x[4] AS DOUBLE) > (CASE WHEN cardinality(filter(e.pre_tb, y -> y[2] = x[2])) > 0 "
        "THEN try_cast(element_at(filter(e.pre_tb, y -> y[2] = x[2]), 1)[4] AS DOUBLE) ELSE 0 END))"
    ).format(wsol=WSOL_MINT)
    return (
        "WITH base AS ("
        f"SELECT id, block_time, block_slot, "
        f"element_at(filter(signers, x -> x != '{FOMO_SPONSOR}'), 1) AS trader, "
        f"account_keys, pre_balances, post_balances, pre_token_balances, post_token_balances "
        f"FROM {TX_TABLE} WHERE signer = '{FOMO_SPONSOR}' AND success "
        f"AND block_time BETWEEN from_unixtime({lo}) AND from_unixtime({hi}) "
        f"AND element_at(filter(signers, x -> x != '{FOMO_SPONSOR}'), 1) IN ({values})"
        "), enriched AS ("
        "SELECT id AS tx_id, block_time, block_slot, trader, "
        "element_at(pre_balances, array_position(account_keys, trader)) AS pre_sol, "
        "element_at(post_balances, array_position(account_keys, trader)) AS post_sol, "
        "filter(pre_token_balances, x -> x[3] = trader) AS pre_tb, "
        "filter(post_token_balances, x -> x[3] = trader) AS post_tb "
        "FROM base), "
        "prices_min AS ("
        "SELECT minute, avg(price) AS price FROM prices.usd "
        f"WHERE blockchain = 'solana' AND symbol = 'SOL' "
        f"AND minute BETWEEN from_unixtime({lo}) AND from_unixtime({hi}) "
        "GROUP BY minute"
        ") "
        "SELECT e.tx_id, e.block_time, e.block_slot, e.trader, e.pre_sol, e.post_sol, "
        "e.pre_tb, e.post_tb, p.price AS sol_usd_price "
        "FROM enriched e LEFT JOIN prices_min p ON p.minute = date_trunc('minute', e.block_time) "
        f"WHERE cardinality({non_wsol_growth_expr}) >= 2"
    )


def mint_amount_map(tb_array) -> dict:
    out: dict = {}
    if not tb_array:
        return out
    for entry in tb_array:
        if not entry or len(entry) < 4:
            continue
        mint = entry[1]
        try:
            amt = float(entry[3])
        except (TypeError, ValueError):
            continue
        out[mint] = out.get(mint, 0.0) + amt
    return out


def candidates_for_row(row: dict) -> list[tuple[str, float, float]]:
    pre_map = mint_amount_map(row.get("pre_tb"))
    post_map = mint_amount_map(row.get("post_tb"))
    out = []
    for mint, post_amt in post_map.items():
        if mint in STABLE_MINTS:
            continue
        pre_amt = pre_map.get(mint, 0.0)
        delta = post_amt - pre_amt
        if delta > 1e-9:
            out.append((mint, delta, pre_amt))
    return out


def compute_spend(row: dict, pre_map: dict, post_map: dict) -> float:
    pre_sol, post_sol = row.get("pre_sol"), row.get("post_sol")
    sol_decrease = max(0.0, (pre_sol - post_sol) / 1e9) if pre_sol is not None and post_sol is not None else 0.0
    wsol_decrease = max(0.0, pre_map.get(WSOL_MINT, 0.0) - post_map.get(WSOL_MINT, 0.0))
    usdc_decrease = max(0.0, pre_map.get(USDC_MINT, 0.0) - post_map.get(USDC_MINT, 0.0))
    usdt_decrease = max(0.0, pre_map.get(USDT_MINT, 0.0) - post_map.get(USDT_MINT, 0.0))
    stable_usd = usdc_decrease + usdt_decrease
    stable_sol_equiv = 0.0
    if stable_usd > 1e-9:
        sol_usd = row.get("sol_usd_price")
        if sol_usd:
            stable_sol_equiv = stable_usd / sol_usd
    return sol_decrease + wsol_decrease + stable_sol_equiv


def main() -> None:
    old = json.loads(OLD_EVENTS_PATH.read_text())
    window_lo, window_hi, scan_lo = old["window_lo_epoch"], old["window_hi_epoch"], old["scan_lo_epoch"]
    roles = old["wallet_roles"]
    full_wallets = roles["leader"] + roles["candidates_29"] + roles["new_from_funnel"]

    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    discovery = step0_discover_keys()
    result["step0_key_discovery"] = discovery
    key_name = pick_working_key(discovery)
    result["key_name_used"] = key_name
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_JANA_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    sql = build_multimint_sql(full_wallets, scan_lo, window_hi)
    r = probe.run_sql_sync("phase2_multimint_targeted_fetch", sql, timeout_s=900)
    step_meta = {k: v for k, v in r.items() if k != "rows"}
    result["multimint_fetch_step"] = step_meta
    cost = (step_meta.get("status_meta") or {}).get("execution_cost_credits")
    result["cost_credits_this_step"] = cost
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[reclassify_v2] многоминтовый запрос: status={r.get('status')} n_rows={r.get('n_rows')} "
          f"стоимость={cost} кредитов", flush=True)
    if r.get("status") != "ok":
        result["FINAL_ANSWER"] = f"Многоминтовый запрос не выполнился ({r.get('status')})."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[reclassify_v2] " + result["FINAL_ANSWER"], flush=True)
        return
    if cost and cost > MAX_QUERY_COST_CREDITS_WARN:
        print(f"[reclassify_v2] ВНИМАНИЕ: стоимость ({cost}) выше ожидаемого предупредительного порога "
              f"({MAX_QUERY_COST_CREDITS_WARN}) -- не останавливаюсь (данные уже получены и оплачены), "
              f"но честно отмечаю для отчёта владельцу.", flush=True)

    rows = r["rows"]
    for row in rows:
        bt = row.get("block_time")
        try:
            row["block_time_epoch"] = int(time.mktime(time.strptime(bt[:19], "%Y-%m-%d %H:%M:%S"))) if isinstance(bt, str) else None
        except Exception:  # noqa: BLE001
            row["block_time_epoch"] = None
    rows = [row for row in rows if row.get("block_time_epoch") is not None]

    # --- немедленно сохраняем сырьё сжатым, ДО обработки ---
    with gzip.open(RAW_MULTIMINT_PATH, "wt", encoding="utf-8") as fh:
        json.dump({"sql": sql, "n_rows": len(rows), "rows": rows}, fh, default=str)
    result["raw_multimint_saved_to"] = str(RAW_MULTIMINT_PATH.relative_to(REPO_ROOT))
    result["raw_multimint_saved_bytes"] = RAW_MULTIMINT_PATH.stat().st_size
    result["n_multimint_tx_fetched"] = len(rows)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[reclassify_v2] сырьё сохранено: {RAW_MULTIMINT_PATH} "
          f"({RAW_MULTIMINT_PATH.stat().st_size} байт, {len(rows)} строк)", flush=True)

    # --- системное определение наградных/маршрутных минтов на многоминтовом датасете ---
    # Два независимых сигнала (или): (a) >=MIN_WALLETS_COMPANION разных
    # кошельков видят минт СОПУТСТВУЮЩЕ в многоминтовой tx; (b) минт НИ РАЗУ
    # не встречается как САМОСТОЯТЕЛЬНАЯ (одноминтовая) покупка во всём
    # датасете (data/solana_phase2_events.json, 18526 событий) -- находка
    # при разборе: помимо явных наградных токенов (STONK/$UBI), то же самое
    # делают токенизированные акции (Xs-префикс, xStocks/Backed Finance,
    # подтверждено WebSearch -- напр. Tesla xStock) как побочный
    # маршрутный хоп в некоторых свопах, не будучи целью покупки НИ РАЗУ.
    mint_wallets: dict[str, set] = defaultdict(set)
    row_candidates = []
    for row in rows:
        cands = candidates_for_row(row)
        row_candidates.append(cands)
        for mint, _, _ in cands:
            mint_wallets[mint].add(row["trader"])
    solo_mint_counts = Counter()
    for wallet, evs in old["events_by_wallet"].items():
        for e in evs:
            solo_mint_counts[e["mint"]] += 1
    reward_mints_companion = {m for m, wset in mint_wallets.items() if len(wset) >= MIN_WALLETS_COMPANION}
    reward_mints_never_solo = {m for m in mint_wallets if solo_mint_counts.get(m, 0) == 0}
    reward_mints = reward_mints_companion | reward_mints_never_solo
    result["n_reward_mints_companion_signal"] = len(reward_mints_companion)
    result["n_reward_mints_never_solo_signal"] = len(reward_mints_never_solo)
    result["n_reward_mints_detected"] = len(reward_mints)
    result["reward_mints_sample"] = sorted(
        [{"mint": m, "n_distinct_wallets": len(mint_wallets[m]), "n_solo_elsewhere": solo_mint_counts.get(m, 0)}
         for m in reward_mints], key=lambda x: -x["n_distinct_wallets"])[:40]
    print(f"[reclassify_v2] наградных/маршрутных минтов: companion-сигнал={len(reward_mints_companion)}, "
          f"никогда-не-соло-сигнал={len(reward_mints_never_solo)}, объединение={len(reward_mints)}", flush=True)

    # --- спасаем настоящие покупки из многоминтовых tx ---
    rescued_by_wallet: dict[str, list] = defaultdict(list)
    n_all_reward, n_ambiguous, n_rescued = 0, 0, 0
    for row, cands in zip(rows, row_candidates):
        genuine = [c for c in cands if c[0] not in reward_mints]
        if not genuine:
            n_all_reward += 1
            continue
        if len(genuine) > 1:
            n_ambiguous += 1
            continue
        mint, tokens_received, _ = genuine[0]
        pre_map = mint_amount_map(row.get("pre_tb"))
        post_map = mint_amount_map(row.get("post_tb"))
        spend = compute_spend(row, pre_map, post_map)
        if spend <= 0:
            continue
        n_rescued += 1
        rescued_by_wallet[row["trader"]].append({
            "tx_id": row["tx_id"], "block_time_epoch": row["block_time_epoch"], "block_slot": row["block_slot"],
            "mint": mint, "tokens_received": tokens_received, "spend_sol_equiv": spend,
            "source_price_sol_per_token": spend / tokens_received, "rescued_from_multimint": True,
        })
    result["n_all_reward_dropped"] = n_all_reward
    result["n_ambiguous_multi_genuine_dropped"] = n_ambiguous
    result["n_genuine_rescued_from_multimint"] = n_rescued
    print(f"[reclassify_v2] спасено настоящих покупок: {n_rescued}, "
          f"полностью наградных tx: {n_all_reward}, неоднозначных (2+ настоящих): {n_ambiguous}", flush=True)

    # --- объединяем со старыми 18526 одноминтовыми событиями, пересчитываем first_entry/k ---
    combined_by_wallet: dict[str, list] = defaultdict(list)
    for wallet, evs in old["events_by_wallet"].items():
        for e in evs:
            combined_by_wallet[wallet].append({**e, "rescued_from_multimint": False})
    for wallet, evs in rescued_by_wallet.items():
        combined_by_wallet[wallet].extend(evs)

    final_events_by_wallet = {}
    for wallet, evs in combined_by_wallet.items():
        evs.sort(key=lambda e: e["block_time_epoch"])
        seen: dict[str, int] = {}
        out = []
        for e in evs:
            is_first = e["mint"] not in seen
            k = 1 if is_first else seen[e["mint"]] + 1
            seen[e["mint"]] = k
            if e["block_time_epoch"] < window_lo:
                continue
            out.append({**e, "first_entry": is_first, "k": k})
        if out:
            final_events_by_wallet[wallet] = out

    n_total = sum(len(v) for v in final_events_by_wallet.values())
    result["n_total_events_after_fix"] = n_total
    out_full = {**{k: v for k, v in old.items() if k != "events_by_wallet"},
                "reclassify_v2_meta": result, "events_by_wallet": final_events_by_wallet}
    OUT_PATH.write_text(json.dumps(out_full, ensure_ascii=False, indent=2, default=str))
    print(f"[reclassify_v2] итого событий после фикса: {n_total} по {len(final_events_by_wallet)} кошелькам "
          f"(было 18526 одноминтовых)", flush=True)

    # --- сверка с контролем: до/после ---
    leader = roles["leader"][0]
    candidates_29 = roles["candidates_29"]
    days = 7.0

    def freq(evs, thr):
        return sum(1 for e in evs if e["first_entry"] and e["spend_sol_equiv"] >= thr) / days

    old_leader_ge43_before = freq(old["events_by_wallet"].get(leader, []), 4.3)
    leader_ge43_after = freq(final_events_by_wallet.get(leader, []), 4.3)
    leader_target = 13.6
    ratio_after = leader_ge43_after / leader_target if leader_target else None

    table_v2 = json.loads((REPO_ROOT / "data" / "solana_29_candidates_table_v2.json").read_text())
    tv2_by_addr = {row_["address"]: row_ for row_ in table_v2["rows"]}
    cand_rows = []
    for addr in candidates_29:
        before_evs, after_evs = old["events_by_wallet"].get(addr, []), final_events_by_wallet.get(addr, [])
        b2, b43 = freq(before_evs, 2), freq(before_evs, 4.3)
        a2, a43 = freq(after_evs, 2), freq(after_evs, 4.3)
        ref = tv2_by_addr.get(addr, {})
        r2, r43 = ref.get("per_day_ge_2"), ref.get("per_day_ge_4.3")
        row_ = {"address": addr, "before_ge2": round(b2, 3), "after_ge2": round(a2, 3), "ref_ge2": r2,
                "before_ge43": round(b43, 3), "after_ge43": round(a43, 3), "ref_ge43": r43}
        if r2:
            row_["within_15pct_ge2_after"] = abs(a2 / r2 - 1) <= 0.15
        if r43 is not None:
            row_["within_15pct_ge43_after"] = (abs(a43 / r43 - 1) <= 0.15) if r43 > 0 else (a43 == 0)
        cand_rows.append(row_)

    n_comparable = sum(1 for row_ in cand_rows if "within_15pct_ge2_after" in row_)
    n_within_ge2 = sum(1 for row_ in cand_rows if row_.get("within_15pct_ge2_after"))
    n_within_ge43 = sum(1 for row_ in cand_rows if row_.get("within_15pct_ge43_after"))

    leader_within = abs(ratio_after - 1) <= 0.15 if ratio_after is not None else None
    verdict_ok = bool(leader_within) and n_comparable > 0 and n_within_ge2 == n_comparable and n_within_ge43 == n_comparable
    comparison = {
        "leader_before_after": {"before_ge43_per_day": round(old_leader_ge43_before, 3),
                                 "after_ge43_per_day": round(leader_ge43_after, 3),
                                 "rpc_target_ge43_per_day": leader_target,
                                 "ratio_after_vs_target": round(ratio_after, 3) if ratio_after else None,
                                 "within_15pct": leader_within},
        "candidates_29": cand_rows,
        "n_candidates_comparable": n_comparable,
        "n_candidates_within_15pct_ge2": n_within_ge2, "n_candidates_within_15pct_ge43": n_within_ge43,
        "VERDICT": "СОШЛОСЬ (в пределах ±15% везде)" if verdict_ok else "НЕ СОШЛОСЬ -- частотам пока не верим",
    }
    COMPARE_PATH.write_text(json.dumps(comparison, ensure_ascii=False, indent=2, default=str))
    print(f"[reclassify_v2] лидер: было {old_leader_ge43_before:.2f}/д -> стало {leader_ge43_after:.2f}/д "
          f"(цель {leader_target}/д, ratio={ratio_after})", flush=True)
    print(f"[reclassify_v2] кандидаты в пределах ±15%: ge2={n_within_ge2}/{n_comparable}, "
          f"ge43={n_within_ge43}/{n_comparable}", flush=True)
    print(f"[reclassify_v2] ВЕРДИКТ: {comparison['VERDICT']}", flush=True)


if __name__ == "__main__":
    main()
