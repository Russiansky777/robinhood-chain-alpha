#!/usr/bin/env python3
"""Владелец, 2026-09-18: шаги 2-3 сита кандидатов с Fomo -- для каждого
кандидата из пересечения 7д/30д (data/fomo_leaderboard_candidates.json,
собран solana_fomo_leaderboard_collect.py): торгует ли на Solana НАПРЯМУЮ
(реальные свопы у его solana-адреса за последнюю неделю, не только
маршрутизатор вроде Robinhood) и число ПЕРВЫХ входов в новые токены за
неделю (баланс по минту был 0 до покупки).

Дорого по RPC (до 7 дней истории на кандидата) -- резюмируемо, по образцу
основного конвейера 300 покупок (там была потеря прогресса при
перезапуске без чекпоинта -- здесь чекпоинт с первого прохода: обновляем
data/fomo_leaderboard_candidates.json построчно и коммитим периодически,
не только в конце).

Переиспользует RPC-инфраструктуру (кэш, троттлинг, ретраи) из
solana_buyer200_fast_price.py -- не дублируем её."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_PATH = REPO_ROOT / "data" / "fomo_leaderboard_candidates.json"

USDC_MINT = fp.USDC
SOL_MINT = "So11111111111111111111111111111111111111112"
LOOKBACK_DAYS = 7
MAX_PAGES_PER_WALLET = 15  # 15*1000 подписей -- честный предел, не бесконечный
PER_WALLET_TIME_BUDGET_S = 45  # обрезает гиперактивные кошельки (боты/маркет-мейкеры), см. analyze_wallet();
# первые 2 реальных кандидата (1830 и 1122 подписей/7д) заняли весь 18-минутный
# бюджет прохода вдвоём -- при таком темпе 56 кандидатов потребовали бы ~9
# перезапусков; 45с достаточно, чтобы честно отличить бота (сотни сделок за
# минуту) от обычного трейдера, не тратя на выброс весь прогон
TIME_BUDGET_S = 18 * 60  # оставляем запас под 25-минутный workflow timeout
COMMIT_INTERVAL_S = 90
MIN_FIRST_ENTRIES_WEEK = 20  # владелец: верхней границы нет, только минимум -- людей за ботом всё равно копируют
GROUP_BOUNDARY = 150  # владелец: "как наш лидер" -- см. отдельную сверку реального темпа лидера в диалоге
MAX_FIRST_ENTRY_SAMPLES = 8  # сигнатур первых входов на кошелёк, для проверки движения цены следующим шагом


def wallet_mint_deltas(tx: dict, wallet: str) -> dict:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return {"increased": [], "decreased": [], "pre_balances": {}}
    pre_tb, post_tb = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []

    def bals(rows):
        a: dict[str, D] = {}
        for b in rows:
            if b.get("owner") == wallet:
                amt = b["uiTokenAmount"]
                a[b["mint"]] = a.get(b["mint"], D(0)) + D(amt["amount"]) / D(10) ** amt["decimals"]
        return a

    pre, post = bals(pre_tb), bals(post_tb)
    delta = {k: post.get(k, D(0)) - pre.get(k, D(0)) for k in set(pre) | set(post)}
    return {
        "increased": [k for k, v in delta.items() if v > 0],
        "decreased": [k for k, v in delta.items() if v < 0],
        "pre_balances": {k: str(v) for k, v in pre.items()},
    }


def analyze_wallet(address: str, cutoff_time: int) -> dict:
    """Первый реальный прогон нашёл кошелёк с 1830 подписями за 7 дней
    (257 первых входов -- явно бот/маркет-мейкер, не дискреционный
    "снайпер" вроде нашего лидера) -- он один съел почти весь бюджет
    времени прохода на 56 кандидатов. PER_WALLET_TIME_BUDGET_S обрезает
    разбор такого кошелька частичным сканированием (честно помечено
    partial_scan=True), а не блокирует весь проход на одном выбросе."""
    before = None
    n_sigs = n_swap_events = n_first_entries = 0
    first_entry_samples: list[dict] = []  # для последующей проверки движения цены -- см. solana_fomo_price_after_entry.py
    started_at = time.monotonic()
    partial = False
    for _ in range(MAX_PAGES_PER_WALLET):
        batch = fp.get_signatures_for_address(address, before=before)
        if not batch:
            break
        stop = False
        for s in batch:
            bt = s.get("blockTime")
            if bt is None:
                continue
            if bt < cutoff_time:
                stop = True
                break
            if time.monotonic() - started_at > PER_WALLET_TIME_BUDGET_S:
                partial = True
                stop = True
                break
            n_sigs += 1
            if s.get("err") is not None:
                continue
            tx = fp.get_transaction(s["signature"])
            if tx is None:
                continue
            deltas = wallet_mint_deltas(tx, address)
            touched = [m for m in deltas["increased"] + deltas["decreased"] if m not in (USDC_MINT, SOL_MINT)]
            if touched:
                n_swap_events += 1
            for m in deltas["increased"]:
                if m in (USDC_MINT, SOL_MINT):
                    continue
                if D(deltas["pre_balances"].get(m, "0")) == 0:
                    n_first_entries += 1
                    if len(first_entry_samples) < MAX_FIRST_ENTRY_SAMPLES:
                        first_entry_samples.append({"signature": s["signature"], "mint": m, "block_time": bt})
        if stop:
            break
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break
    return {
        "n_signatures_checked_7d": n_sigs,
        "n_swap_events_7d": n_swap_events,
        "trades_solana_directly": n_swap_events > 0,
        "first_entries_last_week": n_first_entries,
        "partial_scan": partial,
        "first_entry_samples": first_entry_samples,
    }


def main() -> None:
    if not CANDIDATES_PATH.exists():
        print("[fomo_onchain] data/fomo_leaderboard_candidates.json ещё нет -- сначала solana_fomo_leaderboard_collect.py.", flush=True)
        return
    data = json.loads(CANDIDATES_PATH.read_text())
    candidates = data.get("primary_candidates") or []
    cutoff_time = int(time.time()) - LOOKBACK_DAYS * 86400
    print(f"[fomo_onchain] alchemy_available={fp.alchemy_available()} кандидатов всего={len(candidates)}", flush=True)

    started_at = time.monotonic()
    last_commit_at = started_at
    n_done_this_run = 0
    for row in candidates:
        if row.get("trades_solana_directly") is not None:
            continue
        if time.monotonic() - started_at > TIME_BUDGET_S:
            print(f"[fomo_onchain] бюджет времени исчерпан -- остальное на следующий прогон", flush=True)
            break
        address = row["solana_address"]
        try:
            result = analyze_wallet(address, cutoff_time)
        except RuntimeError as exc:
            row["trades_solana_directly"] = "error"
            row["_error"] = str(exc)[:300]
            print(f"[fomo_onchain] {address[:12]}.. ошибка: {exc}", flush=True)
            continue
        row.update(result)
        n_done_this_run += 1
        print(f"[fomo_onchain] {address[:12]}.. direct={result['trades_solana_directly']} "
              f"first_entries_week={result['first_entries_last_week']} sigs={result['n_signatures_checked_7d']} "
              f"partial={result['partial_scan']}", flush=True)

        if time.monotonic() - last_commit_at > COMMIT_INTERVAL_S:
            CANDIDATES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
            fp._git_commit_progress("fomo_onchain_filter", [CANDIDATES_PATH])
            last_commit_at = time.monotonic()

    n_direct = sum(1 for r in candidates if r.get("trades_solana_directly") is True)
    n_pending = sum(1 for r in candidates if r.get("trades_solana_directly") is None)
    # Владелец: убрал верхнюю границу по первым входам -- люди копируют топ
    # рейтинга не разбираясь, бот это или человек. Только минимум (20/неделю),
    # дальше -- две группы для отдельной проверки движения цены, не отсев.
    qualified = [r for r in candidates if r.get("trades_solana_directly") is True
                 and (r.get("first_entries_last_week") or 0) >= MIN_FIRST_ENTRIES_WEEK]
    qualified.sort(key=lambda r: r.get("first_entries_last_week") or 0, reverse=True)
    group_leader_like = [r for r in qualified if (r.get("first_entries_last_week") or 0) <= GROUP_BOUNDARY]
    group_hyperactive = [r for r in qualified if (r.get("first_entries_last_week") or 0) > GROUP_BOUNDARY]
    data["n_candidates_direct_solana"] = n_direct
    data["n_candidates_still_pending_onchain_check"] = n_pending
    data["n_qualified_ge_20_per_week"] = len(qualified)
    data["group_leader_like_le_150_per_week"] = group_leader_like
    data["group_hyperactive_gt_150_per_week"] = group_hyperactive
    CANDIDATES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    print(f"[fomo_onchain] прогон: {n_done_this_run} обработано, всего готово={len(candidates) - n_pending}/{len(candidates)}, "
          f"прямых={n_direct}", flush=True)


if __name__ == "__main__":
    main()
