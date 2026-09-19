#!/usr/bin/env python3
"""Владелец, 2026-09-19: пересчёт Фазы 2 -- гипотеза владельца (принята,
реальный баг): в многоминтовой транзакции старый код (classify_rows в
solana_phase2_build_events.py) при >1 "купленных" минтов ВЫБРАСЫВАЛ ВСЮ
транзакцию целиком (n_multi_mint_tx_skipped=808), включая настоящую
покупку, если рядом в той же транзакции сопутствующе капнул наградной
токен (STONK/$UBI и подобные с stonkfun.xyz). Это и есть основная
причина недосчёта (7.6/сутки первых входов у лидера вместо ожидаемых
по RPC-методу ~13.6/сутки на пороге >=4.3 SOL, и -25..-50% у 29
кандидатов к table_v2).

Фикс по инструкции владельца: в многоминтовой транзакции исключаем
ТОЛЬКО наградные минты, реальную покупку оставляем с ПОЛНОЙ (не
разделённой) тратой. Наградные минты определяются СИСТЕМНО, не
списком -- по способу появления в данных, два независимых сигнала (или):
  (a) сопутствующий признак (главный, по прямой инструкции владельца):
      минт, который у МНОГИХ разных кошельков (>=MIN_WALLETS) в
      БОЛЬШИНСТВЕ своих появлений (>=MIN_COMPANION_FRAC) идёт ВМЕСТЕ с
      другим минтом в той же транзакции (сопутствующее начисление на
      чужую сделку, а не отдельное решение купить);
  (b) старый сигнал (оставлен как подстраховка -- ловит ЧИСТО
      самостоятельные периодические начисления без сопутствующей
      покупки в той же транзакции, которые (a) не поймает): >=10
      уникальных кошельков ИЛИ >3 событий/кошелёк в среднем.
Оба сигнала report'ятся отдельно в диагностике, чтобы решение было
проверяемым, а не выдуманным.

ВАЖНАЯ ПОПРАВКА владельца (2026-09-19, после первой версии этого файла):
повторная выгрузка результата по execution_id -- НЕ бесплатна, тоже
списывает кредиты (билась о тот же лимит датапоинтов биллинг-цикла,
что и Фаза 3). Сырые строки НИКОГДА не сохранялись на диск (обработаны
в памяти и выброшены в исходном прогоне Фазы 2) -- это process-гэп,
не "уже скачанные данные". Этот скрипт поэтому НЕ запускается, пока
владелец явно не подтвердит снятие лимита Dune (тот же гейт, что и
Фаза 3) -- см. переписку. Как только выполняется -- ПЕРВЫМ делом после
успешного фетча сырые строки пишутся В РЕПОЗИТОРИЙ сжатыми (gzip), ДО
любой дальнейшей обработки -- чтобы повторная итерация классификации
больше никогда не требовала повторной оплаты."""
from __future__ import annotations

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
IN_EVENTS_PATH = REPO_ROOT / "data" / "solana_phase2_events.json"
OUT_PATH = REPO_ROOT / "data" / "solana_phase2_events_v2.json"
COMPARE_PATH = REPO_ROOT / "data" / "solana_phase2_reclassify_v2_comparison.json"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WSOL_MINT = "So11111111111111111111111111111111111111112"
STABLE_MINTS = {USDC_MINT, USDT_MINT, WSOL_MINT}

MIN_WALLETS_COMPANION = 5
MIN_COMPANION_FRAC = 0.5
MIN_DISTINCT_WALLETS_SOLO_HEURISTIC = 10
MAX_AVG_EVENTS_PER_WALLET_SOLO_HEURISTIC = 3.0


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
    """[(mint, tokens_delta, pre_amt), ...] -- немонетные минты с ростом баланса трейдера в этом tx."""
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
    src = json.loads(IN_EVENTS_PATH.read_text())
    exec_id = src["events_step"].get("execution_id")
    window_lo = src["window_lo_epoch"]
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "source_execution_id": exec_id,
                     "method": "ПЛАТНЫЙ повторный probe.results() -- см. поправку владельца в докстринге"}

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    rr = probe.results(exec_id)
    result["refetch_http_status"] = rr["http_status"]
    if rr["http_status"] != 200:
        result["FINAL_ANSWER"] = f"Повторная выдача кэша упала (http={rr['http_status']}) -- {json.dumps(rr.get('body'), default=str)[:500]}"
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[reclassify_v2] " + result["FINAL_ANSWER"], flush=True)
        return
    rows = ((rr.get("body") or {}).get("result") or {}).get("rows") or []
    result["n_raw_rows_refetched"] = len(rows)
    print(f"[reclassify_v2] пере-получено {len(rows)} сырых строк (ПЛАТНО) -- немедленно сохраняю в репозиторий сжатыми", flush=True)

    # Немедленно, ДО любой обработки -- сохраняем сырьё в репозиторий сжатым,
    # чтобы повторная итерация классификации больше НИКОГДА не требовала
    # повторной оплаты (прямая инструкция владельца после инцидента с этим файлом).
    import gzip
    raw_dump_path = REPO_ROOT / "data" / "solana_phase2_raw_rows_v2.json.gz"
    with gzip.open(raw_dump_path, "wt", encoding="utf-8") as fh:
        json.dump({"source_execution_id": exec_id, "n_rows": len(rows), "rows": rows}, fh, default=str)
    result["raw_rows_saved_to"] = str(raw_dump_path.relative_to(REPO_ROOT))
    result["raw_rows_saved_bytes"] = raw_dump_path.stat().st_size
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[reclassify_v2] сырьё сохранено: {raw_dump_path} ({raw_dump_path.stat().st_size} байт)", flush=True)

    # --- эпоха времени ---
    for row in rows:
        bt = row.get("block_time")
        try:
            row["block_time_epoch"] = int(time.mktime(time.strptime(bt[:19], "%Y-%m-%d %H:%M:%S"))) if isinstance(bt, str) else None
        except Exception:  # noqa: BLE001
            row["block_time_epoch"] = None
    rows = [r for r in rows if r.get("block_time_epoch") is not None]

    # --- проход 1: статистика per-минт (solo vs companion) по ВСЕМ строкам ---
    mint_n_solo, mint_n_companion = Counter(), Counter()
    mint_companion_wallets: dict[str, set] = defaultdict(set)
    row_candidates_cache = []
    for row in rows:
        cands = candidates_for_row(row)
        row_candidates_cache.append(cands)
        if len(cands) == 1:
            mint_n_solo[cands[0][0]] += 1
        elif len(cands) > 1:
            for mint, _, _ in cands:
                mint_n_companion[mint] += 1
                mint_companion_wallets[mint].add(row["trader"])

    reward_mints_companion, reward_mints_solo_heuristic = set(), set()
    mint_diag = {}
    all_mints = set(mint_n_solo) | set(mint_n_companion)
    for m in all_mints:
        n_solo, n_comp = mint_n_solo[m], mint_n_companion[m]
        total = n_solo + n_comp
        comp_frac = n_comp / total if total else 0.0
        n_comp_wallets = len(mint_companion_wallets[m])
        is_companion_reward = n_comp_wallets >= MIN_WALLETS_COMPANION and comp_frac >= MIN_COMPANION_FRAC
        if is_companion_reward:
            reward_mints_companion.add(m)
        # solo-heuristic на основе ВСЕХ появлений этого минта как solo-события (старый сигнал,
        # приблизительно -- используем n_solo и число разных кошельков среди solo)
        if n_solo:
            mint_diag[m] = {"n_solo": n_solo, "n_companion": n_comp, "companion_wallets": n_comp_wallets,
                            "companion_frac": round(comp_frac, 3), "flagged_companion": is_companion_reward}

    # старый сигнал -- считаем на SOLO-событиях (эквивалент прежнего фильтра, для сравнения/подстраховки)
    solo_wallets_per_mint: dict[str, set] = defaultdict(set)
    for row, cands in zip(rows, row_candidates_cache):
        if len(cands) == 1:
            solo_wallets_per_mint[cands[0][0]].add(row["trader"])
    for m, n_solo in mint_n_solo.items():
        nw = len(solo_wallets_per_mint[m])
        avg = n_solo / nw if nw else 0
        if nw >= MIN_DISTINCT_WALLETS_SOLO_HEURISTIC or avg > MAX_AVG_EVENTS_PER_WALLET_SOLO_HEURISTIC:
            reward_mints_solo_heuristic.add(m)

    reward_mints_all = reward_mints_companion | reward_mints_solo_heuristic
    result["n_reward_mints_companion_signal"] = len(reward_mints_companion)
    result["n_reward_mints_solo_heuristic_signal"] = len(reward_mints_solo_heuristic)
    result["n_reward_mints_union"] = len(reward_mints_all)
    result["reward_mints_sample"] = sorted(
        [{"mint": m, **mint_diag.get(m, {})} for m in list(reward_mints_all)[:40]],
        key=lambda x: -(x.get("n_companion", 0) + x.get("n_solo", 0))
    )
    print(f"[reclassify_v2] наградных минтов: companion-сигнал={len(reward_mints_companion)}, "
          f"solo-эвристика={len(reward_mints_solo_heuristic)}, объединение={len(reward_mints_all)}", flush=True)

    # --- проход 2: классификация с фиксом -- в multi-mint tx убираем ТОЛЬКО наградные минты ---
    by_wallet: dict[str, list[dict]] = defaultdict(list)
    n_multi_genuine_ambiguous = 0
    n_all_reward_dropped = 0
    n_solo_reward_dropped = 0
    n_genuine_kept_from_multi = 0

    for row, cands in zip(rows, row_candidates_cache):
        if not cands:
            continue
        genuine = [c for c in cands if c[0] not in reward_mints_all]
        if len(cands) == 1:
            if cands[0][0] in reward_mints_all:
                n_solo_reward_dropped += 1
                continue
        else:
            if not genuine:
                n_all_reward_dropped += 1
                continue
            if len(genuine) > 1:
                n_multi_genuine_ambiguous += 1
                continue
            n_genuine_kept_from_multi += 1
        mint, tokens_received, pre_amt = genuine[0] if genuine else cands[0]

        pre_map = mint_amount_map(row.get("pre_tb"))
        post_map = mint_amount_map(row.get("post_tb"))
        spend = compute_spend(row, pre_map, post_map)
        if spend <= 0:
            continue
        by_wallet[row["trader"]].append({
            "tx_id": row["tx_id"], "block_time_epoch": row["block_time_epoch"], "block_slot": row["block_slot"],
            "mint": mint, "tokens_received": tokens_received, "spend_sol_equiv": spend,
            "source_price_sol_per_token": spend / tokens_received, "pre_amt": pre_amt,
            "was_multi_candidate_tx": len(cands) > 1,
        })

    result["n_solo_reward_dropped"] = n_solo_reward_dropped
    result["n_all_reward_dropped_multi"] = n_all_reward_dropped
    result["n_genuine_kept_from_multi_mint_tx"] = n_genuine_kept_from_multi
    result["n_multi_genuine_ambiguous_dropped"] = n_multi_genuine_ambiguous
    print(f"[reclassify_v2] спасено настоящих покупок из многоминтовых tx: {n_genuine_kept_from_multi}, "
          f"полностью наградных tx выброшено: {n_all_reward_dropped}, "
          f"неоднозначных (2+ настоящих минта в одном tx) выброшено: {n_multi_genuine_ambiguous}", flush=True)

    # --- first_entry / k по кошельку по минту, хронологически, с учётом лукбэка ---
    events_by_wallet = {}
    for wallet, evs in by_wallet.items():
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
        for e in out:
            e.pop("pre_amt", None)
        if out:
            events_by_wallet[wallet] = out

    n_total_events = sum(len(v) for v in events_by_wallet.values())
    result["n_wallets_with_events"] = len(events_by_wallet)
    result["n_total_events_in_window"] = n_total_events

    out_full = {**{k: v for k, v in src.items() if k not in ("events_by_wallet",)},
                "reclassify_v2_meta": result, "events_by_wallet": events_by_wallet}
    OUT_PATH.write_text(json.dumps(out_full, ensure_ascii=False, indent=2, default=str))
    print(f"[reclassify_v2] итого событий после фикса: {n_total_events} по {len(events_by_wallet)} кошелькам "
          f"(было 18526/222 в v1, 5811/209 после старой отсечки)", flush=True)

    # --- сверка с контролем ---
    leader = src["wallet_roles"]["leader"][0]
    candidates_29 = src["wallet_roles"]["candidates_29"]
    days = 7.0

    def freq(evs, thr, first_only=True):
        return sum(1 for e in evs if (not first_only or e["first_entry"]) and e["spend_sol_equiv"] >= thr) / days

    leader_evs = events_by_wallet.get(leader, [])
    leader_ge43 = freq(leader_evs, 4.3)
    leader_target = 13.6
    leader_ratio = leader_ge43 / leader_target if leader_target else None

    table_v2 = json.loads((REPO_ROOT / "data" / "solana_29_candidates_table_v2.json").read_text())
    tv2_by_addr = {r["address"]: r for r in table_v2["rows"]}
    cand_rows = []
    for addr in candidates_29:
        evs = events_by_wallet.get(addr, [])
        our_ge2, our_ge43 = freq(evs, 2), freq(evs, 4.3)
        ref = tv2_by_addr.get(addr, {})
        ref_ge2, ref_ge43 = ref.get("per_day_ge_2"), ref.get("per_day_ge_4.3")
        row = {"address": addr, "our_ge2_per_day": round(our_ge2, 3), "ref_ge2_per_day": ref_ge2,
               "our_ge43_per_day": round(our_ge43, 3), "ref_ge43_per_day": ref_ge43}
        if ref_ge2:
            row["within_15pct_ge2"] = abs(our_ge2 / ref_ge2 - 1) <= 0.15
        if ref_ge43:
            row["within_15pct_ge43"] = abs(our_ge43 / ref_ge43 - 1) <= 0.15 if ref_ge43 > 0 else our_ge43 == 0
        cand_rows.append(row)

    n_within_ge2 = sum(1 for r in cand_rows if r.get("within_15pct_ge2"))
    n_within_ge43 = sum(1 for r in cand_rows if r.get("within_15pct_ge43"))
    n_comparable = sum(1 for r in cand_rows if "within_15pct_ge2" in r)

    # старая (v1, уже сохранённая) цифра лидера для "до/после"
    old_clean = json.loads((REPO_ROOT / "data" / "solana_phase2_events_clean.json").read_text())
    old_leader_evs = old_clean["events_by_wallet"].get(leader, [])
    old_leader_ge43 = sum(1 for e in old_leader_evs if e["first_entry"] and e["spend_sol_equiv"] >= 4.3) / days

    comparison = {
        "leader_before_after": {"before_ge43_per_day": round(old_leader_ge43, 3),
                                 "after_ge43_per_day": round(leader_ge43, 3),
                                 "rpc_target_ge43_per_day": leader_target,
                                 "ratio_after_vs_target": round(leader_ratio, 3) if leader_ratio else None,
                                 "within_15pct": abs(leader_ratio - 1) <= 0.15 if leader_ratio else None},
        "candidates_29": cand_rows,
        "n_candidates_within_15pct_ge2": n_within_ge2, "n_candidates_within_15pct_ge43": n_within_ge43,
        "n_candidates_comparable": n_comparable,
        "VERDICT": None,
    }
    verdict_ok = (comparison["leader_before_after"]["within_15pct"] is True
                  and n_comparable > 0 and n_within_ge2 == n_comparable and n_within_ge43 == n_comparable)
    comparison["VERDICT"] = "СОШЛОСЬ (в пределах ±15% везде)" if verdict_ok else "НЕ СОШЛОСЬ -- частотам пока не верим"
    COMPARE_PATH.write_text(json.dumps(comparison, ensure_ascii=False, indent=2, default=str))
    print(f"[reclassify_v2] лидер: было {old_leader_ge43:.2f}/д -> стало {leader_ge43:.2f}/д "
          f"(цель RPC {leader_target}/д, ratio={leader_ratio})", flush=True)
    print(f"[reclassify_v2] кандидаты в пределах ±15%: ge2={n_within_ge2}/{n_comparable}, ge43={n_within_ge43}/{n_comparable}", flush=True)
    print(f"[reclassify_v2] ВЕРДИКТ: {comparison['VERDICT']}", flush=True)


if __name__ == "__main__":
    main()
