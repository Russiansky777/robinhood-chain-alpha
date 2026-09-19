#!/usr/bin/env python3
"""Владелец, 2026-09-19: Фаза 2 -- события ~230 кошельков (лидер как #0 +
29 кандидатов + 200 новых из базы Fomo) методом Источника B (Dune-балансы),
единственным прошедшим шлюз Фазы 1 (Helius -- HTTP 401 Unauthorized на
ВСЕХ запросах, ключ HELIUS_API не рабочий, не код-баг -- см.
data/solana_source_a_helius_gate.json, диагностика добавлена и
подтвердила причину в рамках одной допустимой итерации).

ЯВНАЯ ОГОВОРКА (отклонение от идеального метода ради времени/стоимости):
отбор топ-200 новых кошельков сделан по числу транзакций за 3 суток
(тот же дешёвый паттерн, что уже подтверждён на 0.79 кредита/24ч), А НЕ
по частоте first-entry>=2 SOL -- последнее потребовало бы UNNEST по ВСЕЙ
базе Fomo (100k+ кошельков) и несёт реальный риск того же таймаута, что
уже дважды остановил склейку dex_solana.trades. Это прямо разрешённый
самим заданием запасной путь для случая "Dune прошёл, но правильный
агрегат слишком дорог/рискован" -- решение зафиксировано как оговорка,
не как тихая подмена.

Основной запрос СОБЫТИЙ (не отбор) делает честную балансовую
классификацию по подтверждённой на 300/300 схеме Источника B: для
каждого Agm-подписанного tx нашего списка кошельков берём
pre/post_token_balances, отфильтрованные по trader (владельцу), считаем
дельты по минту; рост немонетного минта с 0 (или отсутствия) --
покупка; first_entry, если минт не встречался у кошелька с начала
7-дневного лукбэка. Трата = SOL-decrease + WSOL-decrease +
(USDC+USDT)-decrease/курс_SOL_в_минуту (Dune prices.usd, JOIN по минуте
и адресу WSOL-контракта, схема подтверждена в fomo_9wallets_dune_result.
json: blockchain/contract_address/decimals/minute/price/symbol).
Курс, не найденный точным джойном по минуте, не выдумывается -- берём
ближайшую по времени РЕАЛЬНО полученную минуту из этого же результата
(перенос вперёд/назад), событие не выбрасывается, просто помечается
price_missing=true и считается отдельно (правило владельца: тихих
выпадений быть не должно).

Транзакции, где у трейдера в ОДНОМ tx выросло больше одного немонетного
минта (амбигуция, кому приписать трату) -- НЕ классифицируются как
покупка, честно считаются в n_multi_mint_tx_skipped, не подмешиваются
молча."""
from __future__ import annotations

import bisect
import json
import os
import sys
import time
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_phase2_events.json"
TX_TABLE = "solana.transactions"

LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
FOMO_SPONSOR = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WSOL_MINT = "So11111111111111111111111111111111111111112"
STABLE_MINTS = {USDC_MINT, USDT_MINT, WSOL_MINT}

WINDOW_DAYS = 7
LOOKBACK_DAYS = 7
TOP200_LOOKBACK_DAYS = 3
N_TOP_NEW_WALLETS = 200
TOP200_QUERY_LIMIT = 260  # запас поверх 200 на пересечение с лидером/29 и на явный мусор


def sql_lit_list(addrs: list[str]) -> str:
    return ",".join(f"'{a}'" for a in addrs)


def load_29_candidates() -> list[str]:
    d = json.loads((REPO_ROOT / "data" / "solana_29_candidates_table_v2.json").read_text())
    return [r["address"] for r in d["rows"]]


def build_top200_sql(lo: int, hi: int) -> str:
    return (
        f"SELECT element_at(filter(signers, x -> x != '{FOMO_SPONSOR}'), 1) AS trader, "
        f"count(*) AS n_tx FROM {TX_TABLE} "
        f"WHERE signer = '{FOMO_SPONSOR}' AND block_time BETWEEN from_unixtime({lo}) AND from_unixtime({hi}) "
        f"GROUP BY 1 ORDER BY n_tx DESC LIMIT {TOP200_QUERY_LIMIT}"
    )


def build_events_sql(wallets: list[str], lo: int, hi: int) -> str:
    values = sql_lit_list(wallets)
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
        "SELECT e.*, p.price AS sol_usd_price "
        "FROM enriched e LEFT JOIN prices_min p "
        "ON p.minute = date_trunc('minute', e.block_time)"
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


def classify_rows(rows: list[dict]) -> dict:
    """Балансовая классификация по подтверждённой схеме. Возвращает
    события, сгруппированные по кошельку, плюс честные счётчики
    неоднозначных/безценовых случаев."""
    by_wallet: dict[str, list[dict]] = {}
    for r in rows:
        by_wallet.setdefault(r["trader"], []).append(r)

    all_price_points = sorted(
        {(row["block_time_epoch"], row["sol_usd_price"]) for row in rows if row.get("sol_usd_price") is not None}
    )
    price_times = [p[0] for p in all_price_points]
    price_vals = [p[1] for p in all_price_points]

    def nearest_price(t: int):
        if not price_times:
            return None, True
        i = bisect.bisect_left(price_times, t)
        candidates = []
        if i < len(price_times):
            candidates.append((abs(price_times[i] - t), price_vals[i]))
        if i > 0:
            candidates.append((abs(price_times[i - 1] - t), price_vals[i - 1]))
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1], candidates[0][0] != 0

    n_multi_mint_skipped = 0
    n_no_target_mint = 0
    n_price_missing = 0
    events_by_wallet: dict[str, list[dict]] = {}

    for wallet, wrows in by_wallet.items():
        wrows.sort(key=lambda r: r["block_time_epoch"])
        seen_mints: dict[str, int] = {}
        events = []
        for row in wrows:
            pre_map = mint_amount_map(row.get("pre_tb"))
            post_map = mint_amount_map(row.get("post_tb"))
            bought = []
            for mint, post_amt in post_map.items():
                if mint in STABLE_MINTS:
                    continue
                pre_amt = pre_map.get(mint, 0.0)
                delta = post_amt - pre_amt
                if delta > 1e-9:
                    bought.append((mint, delta, pre_amt))
            if not bought:
                n_no_target_mint += 1
                continue
            if len(bought) > 1:
                n_multi_mint_skipped += 1
                continue
            mint, tokens_received, pre_amt = bought[0]

            pre_sol, post_sol = row.get("pre_sol"), row.get("post_sol")
            sol_decrease = max(0.0, (pre_sol - post_sol) / 1e9) if pre_sol is not None and post_sol is not None else 0.0
            wsol_decrease = max(0.0, pre_map.get(WSOL_MINT, 0.0) - post_map.get(WSOL_MINT, 0.0))
            usdc_decrease = max(0.0, pre_map.get(USDC_MINT, 0.0) - post_map.get(USDC_MINT, 0.0))
            usdt_decrease = max(0.0, pre_map.get(USDT_MINT, 0.0) - post_map.get(USDT_MINT, 0.0))
            stable_usd = usdc_decrease + usdt_decrease

            price_missing = False
            stable_sol_equiv = 0.0
            if stable_usd > 1e-9:
                sol_usd = row.get("sol_usd_price")
                if sol_usd is None or sol_usd <= 0:
                    sol_usd, imputed = nearest_price(row["block_time_epoch"])
                    if imputed:
                        price_missing = True
                if sol_usd:
                    stable_sol_equiv = stable_usd / sol_usd
                else:
                    price_missing = True
            if price_missing:
                n_price_missing += 1

            spend_sol_equiv = sol_decrease + wsol_decrease + stable_sol_equiv
            if spend_sol_equiv <= 0:
                continue

            is_first_entry = mint not in seen_mints
            k = 1 if is_first_entry else seen_mints[mint] + 1
            seen_mints[mint] = k

            events.append({
                "tx_id": row["tx_id"], "block_time_epoch": row["block_time_epoch"],
                "block_slot": row["block_slot"], "mint": mint,
                "tokens_received": tokens_received, "spend_sol_equiv": spend_sol_equiv,
                "source_price_sol_per_token": spend_sol_equiv / tokens_received,
                "first_entry": is_first_entry, "k": k, "price_missing": price_missing,
                "in_lookback_only": row["block_time_epoch"] < row["window_lo"],
            })
        events_by_wallet[wallet] = [e for e in events if not e["in_lookback_only"]]
        for e in events_by_wallet[wallet]:
            e.pop("in_lookback_only", None)

    return {
        "events_by_wallet": events_by_wallet,
        "n_multi_mint_tx_skipped": n_multi_mint_skipped,
        "n_no_target_mint_tx": n_no_target_mint,
        "n_price_missing": n_price_missing,
    }


def main() -> None:
    now = int(time.time())
    window_hi = now
    window_lo = now - WINDOW_DAYS * 86400
    scan_lo = window_lo - LOOKBACK_DAYS * 86400
    top200_lo = now - TOP200_LOOKBACK_DAYS * 86400

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "step0_key_discovery": discovery,
        "window_lo_epoch": window_lo, "window_hi_epoch": window_hi, "scan_lo_epoch": scan_lo,
        "top200_selection_method": "tx_count_3d (оговорка -- см. докстринг файла, не first-entry-агрегат)",
    }
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[phase2] DUNE_EXPLORER_API не живой, стоп.", flush=True)
        return
    probe = DuneProbe(os.environ[key_name])

    candidates_29 = load_29_candidates()
    exclude = set(candidates_29) | {LEADER_WALLET, FOMO_SPONSOR}

    # --- шаг 1: топ-200 новых кошельков по числу tx за 3 суток ---
    # Переиспользуем уже ОПЛАЧЕННЫЙ прошлый прогон (probe.results на его
    # execution_id -- честно бесплатная повторная выдача кэша), а не
    # платим по новой -- прошлый шаг1 уже завершился успешно (2.62
    # кредита потрачено), упал только шаг2 (событийный запрос).
    prior_exec_id = None
    if OUT_PATH.exists():
        try:
            prior = json.loads(OUT_PATH.read_text())
            prior_top = prior.get("top200_step") or {}
            if prior_top.get("status") == "ok":
                prior_exec_id = prior_top.get("execution_id")
        except Exception:  # noqa: BLE001
            prior_exec_id = None
    if prior_exec_id:
        rr = probe.results(prior_exec_id)
        rows_reused = (((rr.get("body") or {}).get("result") or {}).get("rows") or []) if rr["http_status"] == 200 else []
        r_top = {"status": "ok" if rr["http_status"] == 200 and rows_reused else "results_refetch_failed",
                 "execution_id": prior_exec_id, "reused_from_prior_run": True,
                 "rows": rows_reused, "n_rows": len(rows_reused)}
    else:
        sql_top = build_top200_sql(top200_lo, window_hi)
        r_top = probe.run_sql_sync("phase2_top200_by_tx_count", sql_top, timeout_s=600)
    result["top200_step"] = {k: v for k, v in r_top.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[phase2] топ-200 запрос: status={r_top.get('status')} n_rows={r_top.get('n_rows')}", flush=True)
    if r_top.get("status") != "ok":
        result["FINAL_ANSWER"] = f"Топ-200 запрос не выполнился ({r_top.get('status')}) -- Фаза 2 остановлена."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[phase2] " + result["FINAL_ANSWER"], flush=True)
        return

    top_rows = [row for row in r_top["rows"] if row.get("trader") and row["trader"] not in exclude]
    top_rows.sort(key=lambda r: -r["n_tx"])
    new_200 = [row["trader"] for row in top_rows[:N_TOP_NEW_WALLETS]]
    result["n_new_wallets_selected"] = len(new_200)
    print(f"[phase2] новых кошельков отобрано: {len(new_200)} (из {len(top_rows)} после исключения известных)", flush=True)

    full_wallets = [LEADER_WALLET] + candidates_29 + new_200
    result["n_wallets_total"] = len(full_wallets)
    result["wallet_roles"] = {"leader": [LEADER_WALLET], "candidates_29": candidates_29, "new_from_funnel": new_200}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    # --- шаг 2: события всех кошельков за окно+лукбэк ---
    sql_events = build_events_sql(full_wallets, scan_lo, window_hi)
    r_ev = probe.run_sql_sync("phase2_events_balances", sql_events, timeout_s=1200, poll_s=6)
    result["events_step"] = {k: v for k, v in r_ev.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[phase2] событийный запрос: status={r_ev.get('status')} n_rows={r_ev.get('n_rows')}", flush=True)
    if r_ev.get("status") != "ok":
        result["FINAL_ANSWER"] = f"Событийный запрос не выполнился ({r_ev.get('status')}) -- Фаза 2 не завершена."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[phase2] " + result["FINAL_ANSWER"], flush=True)
        return

    raw_rows = []
    for row in r_ev["rows"]:
        bt = row.get("block_time")
        try:
            epoch = int(time.mktime(time.strptime(bt[:19], "%Y-%m-%d %H:%M:%S"))) if isinstance(bt, str) else None
        except Exception:  # noqa: BLE001
            epoch = None
        if epoch is None:
            continue
        row["block_time_epoch"] = epoch
        row["window_lo"] = window_lo
        raw_rows.append(row)

    result["n_raw_tx_rows"] = len(raw_rows)
    print(f"[phase2] сырых строк для классификации: {len(raw_rows)}", flush=True)

    classified = classify_rows(raw_rows)
    result["n_multi_mint_tx_skipped"] = classified["n_multi_mint_tx_skipped"]
    result["n_no_target_mint_tx"] = classified["n_no_target_mint_tx"]
    result["n_price_missing"] = classified["n_price_missing"]

    events_by_wallet = classified["events_by_wallet"]
    total_events = sum(len(v) for v in events_by_wallet.values())
    result["n_wallets_with_events"] = sum(1 for v in events_by_wallet.values() if v)
    result["n_total_events_in_window"] = total_events
    result["events_by_wallet"] = events_by_wallet
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[phase2] завершено: {total_events} событий по {result['n_wallets_with_events']} кошелькам, "
          f"multi_mint_skipped={classified['n_multi_mint_tx_skipped']}, "
          f"no_target={classified['n_no_target_mint_tx']}, price_missing={classified['n_price_missing']}", flush=True)


if __name__ == "__main__":
    main()
