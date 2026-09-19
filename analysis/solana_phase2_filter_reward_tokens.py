#!/usr/bin/env python3
"""Владелец, 2026-09-19: Фаза 2, пост-обработка (без нового запроса к
Dune, только локальная фильтрация уже полученных данных) -- реальная
находка при разборе сырых событий: у лидера топ-минт по числу "покупок"
(651 сырых строк, k доходил до 513) -- 6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rU
JHacpMpUNgx, это STONK, нативный токен лаунчпада StonkFun (подтверждено
WebSearch: solscan + описание StonkFun). Второй по объёму --
Lyi47medADEVDd5hxJo1mbxhnBct841sFpcGRyHTuwp = $UBI, тоже с stonkfun.xyz,
"распределяет 4% каждой сделки держателям в USDC" -- то есть прирост
баланса этого минта на кошельке лидера -- это АВТОМАТИЧЕСКАЯ выплата
ревеню-шеринга от ЧУЖИХ сделок на платформе, а не решение купить, но
по балансовой схеме (рост баланса минта с 0/меньшего значения)
неотличимо от настоящей покупки.

Эвристика отсечки (раскрыта, не выдумана "чтобы сошлось"): у 2572
уникальных минтов в сырых 18526 событиях МЕДИАНА -- 1 уникальный
кошелёк на минт (обычная память-коин-покупка идиосинкратична). Минт
исключается, если (уникальных_кошельков>=10) ИЛИ (событий/уникальных_
кошельков>3) -- оба сигнала независимо ловят механику
"дрип-начисление всем активным трейдерам платформы", а не осознанную
покупку. Это отсекло 306 минтов / 12715 из 18526 событий (68.6%) --
после чего частота лидера падает с 3920/7д=560/день (абсурд) до
189/7д=27/день, что ХОРОШО совпадает с независимо установленной ранее
канонической ставкой 31.6-33.8/день (data/solana_leader_signal_
reconciliation.py, data/solana_pilot_signal_capture_rate_v2.py) --
сильное независимое подтверждение, что эвристика ловит именно
контаминацию, а не выбрасывает настоящий сигнал.

ОГОВОРКА: порог (>=10 кошельков ИЛИ >3 событий/кошелёк) выбран по
распределению этого конкретного датасета, не проверен на 100% (не
исключено, что часть отсечённых минтов -- реально вирусные
memecoin-хайпы, куда несколько наших 230 кошельков зашли независимо в
одно окно; и не исключено, что часть НЕ отсечённых минтов -- всё ещё
скрытая контаминация другого типа). Раскрыто как допущение, не как
факт."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_PATH = REPO_ROOT / "data" / "solana_phase2_events.json"
OUT_PATH = REPO_ROOT / "data" / "solana_phase2_events_clean.json"

MIN_DISTINCT_WALLETS_FOR_EXCLUSION = 10
MAX_AVG_EVENTS_PER_WALLET = 3.0


def main() -> None:
    d = json.loads(IN_PATH.read_text())
    events_by_wallet = d["events_by_wallet"]

    mint_wallets: dict[str, set] = defaultdict(set)
    mint_counts: Counter = Counter()
    for wallet, events in events_by_wallet.items():
        for e in events:
            mint_wallets[e["mint"]].add(wallet)
            mint_counts[e["mint"]] += 1

    excluded_mints = {}
    for m, cnt in mint_counts.items():
        nw = len(mint_wallets[m])
        avg = cnt / nw
        if nw >= MIN_DISTINCT_WALLETS_FOR_EXCLUSION or avg > MAX_AVG_EVENTS_PER_WALLET:
            excluded_mints[m] = {"n_distinct_wallets": nw, "n_total_events": cnt, "avg_events_per_wallet": round(avg, 2)}

    total_events_before = sum(mint_counts.values())
    total_excluded_events = sum(v["n_total_events"] for v in excluded_mints.values())

    clean_events_by_wallet = {}
    for wallet, events in events_by_wallet.items():
        kept = [e for e in events if e["mint"] not in excluded_mints]
        if kept:
            clean_events_by_wallet[wallet] = kept

    top_excluded = sorted(excluded_mints.items(), key=lambda kv: -kv[1]["n_total_events"])[:30]

    out = {
        "generated_at_utc": d["generated_at_utc"],
        "source_file": str(IN_PATH.name),
        "method": "post-processing локальной фильтрацией уже полученных данных, БЕЗ нового запроса к Dune",
        "exclusion_rule": f"nw>={MIN_DISTINCT_WALLETS_FOR_EXCLUSION} OR avg_events_per_wallet>{MAX_AVG_EVENTS_PER_WALLET}",
        "n_mints_total": len(mint_counts),
        "n_mints_excluded": len(excluded_mints),
        "n_events_before": total_events_before,
        "n_events_excluded": total_excluded_events,
        "n_events_after": total_events_before - total_excluded_events,
        "top_excluded_mints_sample": [{"mint": m, **v} for m, v in top_excluded],
        "wallet_roles": d["wallet_roles"],
        "window_lo_epoch": d["window_lo_epoch"], "window_hi_epoch": d["window_hi_epoch"],
        "events_by_wallet": clean_events_by_wallet,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))

    leader = d["wallet_roles"]["leader"][0]
    n_leader_before = len(events_by_wallet.get(leader, []))
    n_leader_after = len(clean_events_by_wallet.get(leader, []))
    print(f"[filter] минтов всего={len(mint_counts)}, исключено={len(excluded_mints)} "
          f"({total_excluded_events}/{total_events_before}={total_excluded_events/total_events_before:.1%} событий)")
    print(f"[filter] лидер: {n_leader_before} -> {n_leader_after} событий за 7д "
          f"({n_leader_before/7:.1f} -> {n_leader_after/7:.1f}/день; ориентир из ранее найденного канона: 31.6-33.8/день)")
    print(f"[filter] итого событий после очистки: {out['n_events_after']} по {len(clean_events_by_wallet)} кошелькам")


if __name__ == "__main__":
    main()
