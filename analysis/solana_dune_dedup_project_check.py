#!/usr/bin/env python3
"""Владелец, 2026-09-20: Section A -- один вопрос перед сменой метода.
В подмножестве с удвоением размера (n_sol_legs>=2, медиана ratio=1.84)
лежат ли ОДНИ И ТЕ ЖЕ tx_id двумя строками с РАЗНЫМИ project, один из
которых агрегатор (jupiter/...)? Если да -- это и есть причина 1.84:
dex_solana.trades хранит и агрегированную (route-level), и физическую
(pool-level) запись одного и того же экономического перевода под одним
tx_id, наша сумма по sold_mint=SOL их складывает как будто это два
разных перевода.

Шаг 1 (0 новых кредитов): перечитываем УЖЕ ОПЛАЧЕННЫЙ execution_id
(solana_dune_29_candidates_validation.py, 625 строк) через results(),
находим tx_id с >=2 строками sold_mint=SOL -- это и есть подозреваемое
подмножество (~82 tx по предыдущему прогону).
Шаг 2 (новый, но маленький и целевой запрос -- только под эти ~82 tx_id,
с колонкой project, которой не было в исходном запросе): смотрим,
сколько tx_id имеют >=2 РАЗНЫХ project среди своих строк, и есть ли
среди них известные агрегаторы."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_dedup_project_check.json"
SOL_MINT = "So11111111111111111111111111111111111111112"
CACHED_EXECUTION_ID = "01M2VNXS4NZCYFZ2VWT7YJBGWJ"
KNOWN_AGGREGATORS = {"jupiter", "jupiter_v6", "jupiter_dca", "jupiter_limit_order", "okx_dex", "titan"}


def main() -> None:
    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"step0_key_discovery": discovery, "cached_execution_id": CACHED_EXECUTION_ID}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    res = probe.results(CACHED_EXECUTION_ID)
    if res.get("http_status") != 200:
        result["HONEST_ANSWER"] = f"Не удалось перечитать закэшированный результат -- http={res.get('http_status')}"
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    rows = (res["body"].get("result") or {}).get("rows") or []
    print(f"[dedup] перечитано {len(rows)} строк (0 новых кредитов)", flush=True)

    by_tx: dict[str, list] = {}
    for row in rows:
        by_tx.setdefault(row["tx_id"], []).append(row)

    suspect_tx_ids = [tx_id for tx_id, legs in by_tx.items()
                       if sum(1 for lg in legs if lg.get("sold_mint") == SOL_MINT and lg.get("sold_amount")) >= 2]
    result["n_suspect_tx_ids"] = len(suspect_tx_ids)
    print(f"[dedup] tx с >=2 SOL-ногами: {len(suspect_tx_ids)}", flush=True)

    tx_list_sql = ",".join(f"'{t}'" for t in suspect_tx_ids)
    sql = (f"SELECT tx_id, project, token_sold_mint_address AS sold_mint, "
           f"token_bought_mint_address AS bought_mint, token_sold_amount AS sold_amount "
           f"FROM dex_solana.trades WHERE tx_id IN ({tx_list_sql})")
    r = probe.run_sql_sync("dedup_project_check", sql, timeout_s=120)
    result["dune_step"] = {k: v for k, v in r.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if r.get("status") != "ok":
        result["HONEST_ANSWER"] = f"Запрос не выполнился ({r.get('status')})"
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[dedup] " + result["HONEST_ANSWER"], flush=True)
        return

    by_tx2: dict[str, list] = {}
    for row in r["rows"]:
        by_tx2.setdefault(row["tx_id"], []).append(row)

    n_multi_project = 0
    n_with_aggregator = 0
    project_pair_examples = []
    all_project_counts = Counter()
    for tx_id, legs in by_tx2.items():
        projects = {lg.get("project") for lg in legs}
        all_project_counts.update(projects)
        if len(projects) >= 2:
            n_multi_project += 1
            has_agg = bool(projects & KNOWN_AGGREGATORS) or any("jupiter" in (p or "") for p in projects)
            if has_agg:
                n_with_aggregator += 1
            if len(project_pair_examples) < 10:
                project_pair_examples.append({"tx_id": tx_id, "projects": sorted(projects),
                                               "legs": [{"project": lg.get("project"), "sold_mint": lg.get("sold_mint"),
                                                         "bought_mint": lg.get("bought_mint"),
                                                         "sold_amount": lg.get("sold_amount")} for lg in legs]})

    result["n_tx_checked"] = len(by_tx2)
    result["n_tx_with_multiple_distinct_project"] = n_multi_project
    result["n_tx_with_aggregator_among_projects"] = n_with_aggregator
    result["all_project_value_counts"] = dict(all_project_counts.most_common(20))
    result["examples"] = project_pair_examples

    if n_multi_project and n_with_aggregator / max(n_multi_project, 1) >= 0.5:
        result["ONE_LINE_ANSWER"] = (
            f"ДА: {n_with_aggregator}/{len(by_tx2)} подозрительных tx_id несут >=2 разных project, включая "
            "агрегатор (jupiter-семейство) -- это и есть причина x2: dex_solana.trades хранит и route-level "
            "(агрегатор), и pool-level (физическая нога) запись ОДНОГО перевода под одним tx_id; для дедупликации "
            "на стадии 2 нужно ИСКЛЮЧАТЬ строки агрегаторских project или брать только pool-level project."
        )
    else:
        result["ONE_LINE_ANSWER"] = (
            f"НЕТ / не подтверждено на уровне большинства: только {n_with_aggregator}/{len(by_tx2)} tx_id с "
            "агрегатором среди project-дублей -- причина x2, вероятно, другая (см. all_project_value_counts/examples)."
        )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[dedup] {result['ONE_LINE_ANSWER']}", flush=True)


if __name__ == "__main__":
    main()
